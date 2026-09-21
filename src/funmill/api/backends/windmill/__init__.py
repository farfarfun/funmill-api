import json
import os
from typing import Any
from uuid import UUID

import httpx

from funmill.api.models import (
    TaskDefinition,
    TaskInfo,
    TaskLanguage,
    TaskLogs,
    TaskProgress,
    TaskResult,
    TaskStatus,
    TaskSubmit,
    WorkflowSubmit,
)
from funmill.api.ports import THIRD_PARTY_WEB_PORT

from ..base import BackendError, TaskBackend

_CALLBACK_SOURCE = """import json
import os
from urllib.request import Request, urlopen

def main(callback_url: str, status: str, payload):
    body = {
        "task_id": os.environ["WM_ROOT_JOB_ID"],
        "status": status,
        "payload": payload,
    }
    request = Request(
        callback_url,
        data=json.dumps(body, default=str).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=10):
        return payload
"""

_DEPENDENCY_SOURCE = """from datetime import datetime, timezone
import os
from wmill import get_job

def main(dependencies: list[str], timeout_seconds: int):
    root = get_job(os.environ["WM_ROOT_JOB_ID"])
    created_at = datetime.fromisoformat(root["created_at"].replace("Z", "+00:00"))
    if (datetime.now(timezone.utc) - created_at).total_seconds() > timeout_seconds:
        raise TimeoutError("dependency wait timed out")

    for task_id in dependencies:
        job = get_job(task_id)
        if "success" not in job:
            return False
        if job.get("canceled"):
            raise RuntimeError(f"dependency {task_id} was canceled")
        if not job["success"]:
            raise RuntimeError(f"dependency {task_id} failed")
    return True
"""

_RESULT_SOURCE = """def main(results):
    return results
"""

_LANGUAGES = {
    TaskLanguage.PYTHON: "python3",
    TaskLanguage.BASH: "bash",
}


class WindmillBackend(TaskBackend):
    name = "windmill"

    def __init__(
        self,
        base_url: str,
        workspace: str,
        token: str,
        timeout: float = 30,
        client: httpx.Client | None = None,
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(
            base_url=f"{self.base_url}/api/w/{workspace}/",
            timeout=timeout,
        )

    @classmethod
    def from_env(cls) -> "WindmillBackend":
        return cls(
            base_url=os.getenv(
                "WINDMILL_URL", f"http://127.0.0.1:{THIRD_PARTY_WEB_PORT}"
            ),
            workspace=os.getenv("WINDMILL_WORKSPACE", "admins"),
            token=os.getenv("WINDMILL_TOKEN", ""),
            timeout=float(os.getenv("WINDMILL_TIMEOUT", "30")),
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if not self.token:
            raise BackendError("WINDMILL_TOKEN is not configured", 503)
        try:
            response = self.client.request(
                method,
                path,
                headers={"Authorization": f"Bearer {self.token}"},
                **kwargs,
            )
        except httpx.TimeoutException as exc:
            raise BackendError("Windmill request timed out", 504) from exc
        except httpx.HTTPError as exc:
            raise BackendError(f"Windmill is unavailable: {exc}", 502) from exc
        if response.is_error:
            status_code = 404 if response.status_code == 404 else 502
            detail = response.text.strip()[:500] or f"HTTP {response.status_code}"
            raise BackendError(f"Windmill rejected the request: {detail}", status_code)
        return response

    def health_check(self) -> None:
        if not self.token:
            raise BackendError("WINDMILL_TOKEN is not configured", 503)
        try:
            response = self.client.get(f"{self.base_url}/api/health/status")
        except httpx.TimeoutException as exc:
            raise BackendError("Windmill health check timed out", 504) from exc
        except httpx.HTTPError as exc:
            raise BackendError(f"Windmill is unavailable: {exc}", 502) from exc
        if response.is_error:
            raise BackendError(
                f"Windmill health check failed: HTTP {response.status_code}", 502
            )
        status = response.json().get("status")
        if status not in {"healthy", "ok"}:
            raise BackendError(f"Windmill reports status {status!r}", 502)

    def _submit_flow(self, value: dict[str, Any], args: dict[str, Any]) -> str:
        response = self._request(
            "POST", "jobs/run/preview_flow", json={"value": value, "args": args}
        )
        return self._response_task_id(response)

    @staticmethod
    def _response_task_id(response: httpx.Response) -> str:
        lines = response.text.strip().splitlines()
        if not lines:
            raise BackendError("Windmill returned an empty task ID")
        try:
            return str(UUID(lines[0]))
        except ValueError as exc:
            raise BackendError(
                f"Windmill returned an invalid task ID: {lines[0]!r}"
            ) from exc

    @staticmethod
    def _task_module(task_id: str, task: TaskDefinition) -> dict[str, Any]:
        module: dict[str, Any] = {
            "id": task_id,
            "value": {
                "type": "rawscript",
                "language": _LANGUAGES[task.language],
                "content": task.source,
                "input_transforms": {
                    key: {"type": "static", "value": value}
                    for key, value in task.args.items()
                },
            },
        }
        if task.retry.attempts:
            module["retry"] = {
                "constant": {
                    "attempts": task.retry.attempts,
                    "seconds": task.retry.delay_seconds,
                }
            }
        if task.timeout_seconds:
            module["timeout"] = {"type": "static", "value": task.timeout_seconds}
        return module

    @staticmethod
    def _dependency_module(
        dependencies: list[str], timeout_seconds: int
    ) -> dict[str, Any]:
        # ponytail: polling creates one short Windmill job per interval; replace with
        # backend completion events when dependency volume makes that measurable.
        return {
            "id": "funmill_wait",
            "value": {
                "type": "whileloopflow",
                "skip_failures": False,
                "modules": [
                    {
                        "id": "funmill_check_dependencies",
                        "value": {
                            "type": "rawscript",
                            "language": "python3",
                            "content": _DEPENDENCY_SOURCE,
                            "input_transforms": {
                                "dependencies": {
                                    "type": "static",
                                    "value": dependencies,
                                },
                                "timeout_seconds": {
                                    "type": "static",
                                    "value": timeout_seconds,
                                },
                            },
                        },
                        "sleep": {"type": "static", "value": 2},
                        "stop_after_if": {"expr": "result === true"},
                    }
                ],
            },
        }

    @staticmethod
    def _add_callback(
        value: dict[str, Any], callback_url: str | None, result_id: str
    ) -> None:
        if callback_url is None:
            return

        def callback_module(module_id: str, status: str, payload_expr: str):
            return {
                "id": module_id,
                "value": {
                    "type": "rawscript",
                    "language": "python3",
                    "content": _CALLBACK_SOURCE,
                    "input_transforms": {
                        "callback_url": {"type": "static", "value": callback_url},
                        "status": {"type": "static", "value": status},
                        "payload": {"type": "javascript", "expr": payload_expr},
                    },
                },
                "retry": {"constant": {"attempts": 3, "seconds": 2}},
            }

        result_expr = f"results[{json.dumps(result_id)}]"
        value["modules"].append(
            callback_module("funmill_callback", "succeeded", result_expr)
        )
        value["failure_module"] = callback_module("failure", "failed", "error")

    @staticmethod
    def _result_module(task_results: dict[str, str]) -> dict[str, Any]:
        entries = ", ".join(
            f"{json.dumps(task_id)}: {result_expr}"
            for task_id, result_expr in task_results.items()
        )
        return {
            "id": "funmill_result",
            "value": {
                "type": "rawscript",
                "language": "python3",
                "content": _RESULT_SOURCE,
                "input_transforms": {
                    "results": {
                        "type": "javascript",
                        "expr": f"({{{entries}}})",
                    }
                },
            },
        }

    def submit_task(self, task: TaskSubmit) -> str:
        modules = []
        if task.depends_on:
            modules.append(
                self._dependency_module(
                    task.depends_on, task.dependency_timeout_seconds
                )
            )
        modules.append(self._task_module("task", task))
        value = {"modules": modules}
        self._add_callback(
            value,
            str(task.callback_url) if task.callback_url else None,
            "task",
        )
        return self._submit_flow(value, {})

    def submit_workflow(self, workflow: WorkflowSubmit) -> str:
        modules = []
        task_results = {}
        if workflow.depends_on:
            modules.append(
                self._dependency_module(
                    workflow.depends_on, workflow.dependency_timeout_seconds
                )
            )

        for index, layer in enumerate(workflow.topological_layers()):
            if len(layer) == 1:
                module = self._task_module(layer[0].key, layer[0])
                task_results[layer[0].key] = f"results[{json.dumps(layer[0].key)}]"
            else:
                layer_id = f"funmill_layer_{index}"
                module = {
                    "id": layer_id,
                    "value": {
                        "type": "branchall",
                        "parallel": True,
                        "branches": [
                            {
                                "summary": task.key,
                                "modules": [self._task_module(task.key, task)],
                            }
                            for task in layer
                        ],
                    },
                }
                for task_index, task in enumerate(layer):
                    task_results[task.key] = (
                        f"results[{json.dumps(layer_id)}][{task_index}]"
                    )
            modules.append(module)

        result_module = self._result_module(task_results)
        modules.append(result_module)

        value = {"modules": modules}
        self._add_callback(
            value,
            str(workflow.callback_url) if workflow.callback_url else None,
            result_module["id"],
        )
        return self._submit_flow(value, {})

    def _get_job(self, task_id: str) -> dict[str, Any]:
        return self._request("GET", f"jobs_u/get/{task_id}").json()

    @staticmethod
    def _status(job: dict[str, Any]) -> TaskStatus:
        if job.get("canceled"):
            return TaskStatus.CANCELED
        if "success" in job:
            return TaskStatus.SUCCEEDED if job["success"] else TaskStatus.FAILED
        return TaskStatus.RUNNING if job.get("running") else TaskStatus.QUEUED

    def get_task(self, task_id: str) -> TaskInfo:
        job = self._get_job(task_id)
        return TaskInfo(
            task_id=task_id,
            status=self._status(job),
            created_at=job.get("created_at"),
            started_at=job.get("started_at"),
            completed_at=job.get("completed_at"),
            duration_ms=job.get("duration_ms"),
        )

    def get_progress(self, task_id: str) -> TaskProgress:
        job = self._get_job(task_id)
        status = self._status(job)
        if status == TaskStatus.SUCCEEDED:
            return TaskProgress(task_id=task_id, progress=100)

        modules = (job.get("flow_status") or {}).get("modules") or []
        scores = []
        for module in modules:
            if module.get("type") == "Success":
                scores.append(100)
            elif module.get("type") == "InProgress":
                scores.append(module.get("progress") or 0)
            else:
                scores.append(0)
        progress = int(sum(scores) / len(scores)) if scores else None
        return TaskProgress(task_id=task_id, progress=progress)

    def get_logs(self, task_id: str) -> TaskLogs:
        job = self._get_job(task_id)
        if job.get("job_kind") in {"flow", "flowpreview", "singlestepflow"}:
            response = self._request("GET", f"jobs_u/get_flow_all_logs/{task_id}")
            logs = response.text
        else:
            logs = self._request("GET", f"jobs_u/get_logs/{task_id}").text
        return TaskLogs(task_id=task_id, logs=logs)

    def get_result(self, task_id: str) -> TaskResult:
        result = self._request("GET", f"jobs_u/completed/get_result/{task_id}").json()
        return TaskResult(task_id=task_id, result=result)

    def cancel(self, task_id: str, reason: str) -> None:
        self._request("POST", f"jobs_u/queue/cancel/{task_id}", json={"reason": reason})

    def rerun(self, task_id: str) -> str:
        job = self._get_job(task_id)
        raw_flow = job.get("raw_flow")
        if raw_flow:
            return self._submit_flow(raw_flow, job.get("args") or {})

        raw_code = job.get("raw_code")
        if raw_code:
            response = self._request(
                "POST",
                "jobs/run/preview",
                json={
                    "content": raw_code,
                    "language": job.get("language"),
                    "kind": "code",
                    "args": job.get("args") or {},
                },
            )
            return self._response_task_id(response)

        raise BackendError(
            "task cannot be rerun because its source is unavailable", 409
        )

    def close(self) -> None:
        self.client.close()
