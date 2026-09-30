import base64
import json
import os
import re
import shlex
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

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

from ..base import BackendError, SubmitResult, TaskBackend

_DAG_NAME = "funmill"
_TASK_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_TERMINAL_NODE_STATES = {"succeeded", "failed", "aborted", "skipped", "rejected"}

_DEPENDENCY_SOURCE = """#!/usr/bin/env python3
import base64
import json
import os
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

base_url = base64.b64decode("__BASE_URL__").decode()
dependencies = json.loads(base64.b64decode("__DEPENDENCIES__"))
deadline = time.monotonic() + __TIMEOUT_SECONDS__
username = os.getenv("DAGU_AUTH_BASIC_USERNAME", "")
password = os.getenv("DAGU_AUTH_BASIC_PASSWORD", "")
headers = {}
if username or password:
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
    headers["Authorization"] = f"Basic {credentials}"
dag_names = {}


def resolve_dag_name(task_id):
    request = Request(
        f"{base_url}/dag-runs?dagRunId={quote(task_id, safe='')}",
        headers=headers,
    )
    with urlopen(request, timeout=30) as response:
        runs = json.load(response).get("dagRuns") or []
    if not runs:
        raise RuntimeError(f"dependency {task_id} was not found")
    return runs[0].get("name") or "funmill"


def dag_run_status(task_id):
    # A dependency may have been submitted with a custom name (top-level DAG
    # name), so the shared "funmill" name is only a fast-path guess.
    name = dag_names.get(task_id, "funmill")
    request = Request(
        f"{base_url}/dag-runs/{quote(name, safe='')}/{quote(task_id, safe='')}",
        headers=headers,
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)["dagRunDetails"]["statusLabel"]
    except HTTPError as exc:
        if exc.code != 404 or task_id in dag_names:
            raise
        dag_names[task_id] = resolve_dag_name(task_id)
        return dag_run_status(task_id)


while True:
    waiting = False
    for task_id in dependencies:
        status = dag_run_status(task_id)
        if status in {"failed", "partially_succeeded", "rejected"}:
            raise RuntimeError(f"dependency {task_id} failed")
        if status == "aborted":
            raise RuntimeError(f"dependency {task_id} was canceled")
        waiting |= status != "succeeded"
    if not waiting:
        break
    if time.monotonic() >= deadline:
        raise TimeoutError("dependency wait timed out")
    time.sleep(2)
"""

_CALLBACK_SOURCE = """#!/usr/bin/env python3
import base64
import json
import os
from urllib.request import Request, urlopen

def decode(value):
    if isinstance(value, str):
        if value.startswith("json:"):
            return json.loads(value[5:])
        if value.startswith("text:"):
            return value[5:]
        try:
            return decode(json.loads(value))
        except (json.JSONDecodeError, TypeError):
            return value
    if isinstance(value, dict):
        return {key: decode(item) for key, item in value.items()}
    return value

body = {
    "task_id": os.environ["FUNMILL_CALLBACK_TASK_ID"],
    "status": os.environ["FUNMILL_CALLBACK_STATUS"],
    "payload": decode(os.environ["FUNMILL_CALLBACK_PAYLOAD"]),
}
request = Request(
    base64.b64decode("__CALLBACK_URL__").decode(),
    data=json.dumps(body, default=str).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urlopen(request, timeout=10):
    pass
"""


def _encoded(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def _dag_run_name(name: str | None) -> str:
    # "/" is rejected defensively: an encoded slash inside a single path
    # segment is a well-known cross-router gotcha, and it can't be verified
    # against every Dagu deployment's HTTP framework. Every other character
    # (including unicode, "#", "?", spaces) is safe once percent-encoded.
    name = (name or "").strip()
    if not name or "/" in name:
        return _DAG_NAME
    return name


def _decode_result(value: Any) -> Any:
    if isinstance(value, str):
        if value.startswith("json:"):
            return json.loads(value[5:])
        if value.startswith("text:"):
            return value[5:]
        try:
            return _decode_result(json.loads(value))
        except (json.JSONDecodeError, TypeError):
            return value
    if isinstance(value, dict):
        return {key: _decode_result(item) for key, item in value.items()}
    return value


def _python_script(task: TaskDefinition) -> str:
    source = _encoded(task.source)
    args = _encoded(json.dumps(task.args, separators=(",", ":")))
    return f"""#!/usr/bin/env python3
import base64
import json
import os

namespace = {{"__name__": "__funmill__"}}
source = base64.b64decode({source!r}).decode()
args = json.loads(base64.b64decode({args!r}))
exec(compile(source, "<funmill>", "exec"), namespace)
result = namespace["main"](**args)
with open(os.environ["DAGU_OUTPUT_FILE"], "a", encoding="utf-8") as output:
    output.write("result=json:" + json.dumps(result, default=str) + "\\n")
"""


def _bash_script(task: TaskDefinition) -> str:
    args = " ".join(
        shlex.quote(value if isinstance(value, str) else json.dumps(value))
        for value in task.args.values()
    )
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"__funmill_source={shlex.quote(_encoded(task.source))}\n"
        '__funmill_script="$(mktemp "${TMPDIR:-/tmp}/funmill.XXXXXX")"\n'
        '__funmill_result="$(mktemp "${TMPDIR:-/tmp}/funmill.XXXXXX")"\n'
        'trap \'rm -f "$__funmill_script" "$__funmill_result"\' EXIT\n'
        "if ! printf '%s' \"$__funmill_source\" | base64 --decode "
        '>"$__funmill_script" 2>/dev/null; then\n'
        '  printf \'%s\' "$__funmill_source" | base64 -D >"$__funmill_script"\n'
        "fi\n"
        'source "$__funmill_script"\n'
        "set -euo pipefail\n"
        f'main {args} | tee "$__funmill_result"\n'
        '__funmill_delimiter="FUNMILL_RESULT_$$"\n'
        "{\n"
        "  printf 'result<<%s\\n' \"$__funmill_delimiter\"\n"
        "  printf 'text:'\n"
        '  cat "$__funmill_result"\n'
        "  printf '\\n%s\\n' \"$__funmill_delimiter\"\n"
        '} >>"$DAGU_OUTPUT_FILE"\n'
    )


class DaguBackend(TaskBackend):
    name = "dagu"

    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
        timeout: float = 30,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.client = client or httpx.Client(
            base_url=f"{self.base_url}/api/v1/", timeout=timeout
        )

    @classmethod
    def from_env(cls) -> "DaguBackend":
        return cls(
            base_url=os.getenv("DAGU_URL", f"http://127.0.0.1:{THIRD_PARTY_WEB_PORT}"),
            username=os.getenv("DAGU_AUTH_BASIC_USERNAME", ""),
            password=os.getenv("DAGU_AUTH_BASIC_PASSWORD", ""),
            timeout=float(os.getenv("DAGU_TIMEOUT", "30")),
        )

    def _ui_url(self, dag_name: str, task_id: str) -> str:
        return f"{self.base_url}/dag-runs/{quote(dag_name, safe='')}/{task_id}"

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = kwargs.pop("headers", {})
        if self.username or self.password:
            credentials = base64.b64encode(
                f"{self.username}:{self.password}".encode()
            ).decode()
            headers["Authorization"] = f"Basic {credentials}"
        try:
            response = self.client.request(method, path, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise BackendError("Dagu request timed out", 504) from exc
        except httpx.HTTPError as exc:
            raise BackendError(f"Dagu is unavailable: {exc}", 502) from exc
        if response.is_error:
            status_code = 404 if response.status_code == 404 else 502
            detail = response.text.strip()[:500] or f"HTTP {response.status_code}"
            raise BackendError(f"Dagu rejected the request: {detail}", status_code)
        return response

    def health_check(self) -> None:
        response = self._request("GET", "health")
        status = response.json().get("status")
        if status not in {"healthy", "ok"}:
            raise BackendError(f"Dagu reports status {status!r}", 502)

    @staticmethod
    def _task_id(value: Any) -> str:
        if not isinstance(value, str):
            raise BackendError(f"Dagu returned an invalid task ID: {value!r}")
        task_id = value
        if task_id == "latest" or not _TASK_ID.fullmatch(task_id):
            raise BackendError(f"Dagu returned an invalid task ID: {task_id!r}")
        return task_id

    def _task_step(
        self, step_id: str, task: TaskDefinition, dependencies: list[str]
    ) -> dict[str, Any]:
        step: dict[str, Any] = {
            "id": step_id,
            "run": (
                _python_script(task)
                if task.language == TaskLanguage.PYTHON
                else _bash_script(task)
            ),
            "outputs": [{"name": "result"}],
        }
        if dependencies:
            step["depends"] = dependencies
        if task.name:
            step["name"] = task.name
        if task.description:
            step["description"] = task.description
        if task.retry.attempts:
            step["retry_policy"] = {
                "limit": task.retry.attempts,
                "interval_sec": task.retry.delay_seconds,
            }
        if task.timeout_seconds:
            step["timeout_sec"] = task.timeout_seconds
        return step

    def _dependency_step(
        self, dependencies: list[str], timeout_seconds: int
    ) -> dict[str, Any]:
        dependencies = [self._task_id(task_id) for task_id in dependencies]
        source = (
            _DEPENDENCY_SOURCE.replace(
                "__BASE_URL__", _encoded(f"{self.base_url}/api/v1")
            )
            .replace("__DEPENDENCIES__", _encoded(json.dumps(dependencies)))
            .replace("__TIMEOUT_SECONDS__", str(timeout_seconds))
        )
        return {"id": "funmill_wait", "run": source}

    @staticmethod
    def _result_step(task_ids: list[str]) -> dict[str, Any]:
        result: str | dict[str, str]
        if task_ids == ["task"]:
            result = "${steps.task.outputs.result}"
        else:
            result = {
                task_id: f"${{steps.{task_id}.outputs.result}}" for task_id in task_ids
            }
        return {
            "id": "funmill_result",
            "depends": task_ids,
            "action": "outputs.write",
            "with": {"values": {"result": result}},
        }

    @staticmethod
    def _callback(status: str, callback_url: str) -> dict[str, Any]:
        payload = (
            "${steps.funmill_result.outputs.result}"
            if status == "succeeded"
            else f"text:Dagu run {status}"
        )
        return {
            "run": _CALLBACK_SOURCE.replace("__CALLBACK_URL__", _encoded(callback_url)),
            "env": {
                "FUNMILL_CALLBACK_TASK_ID": "${context.run.id}",
                "FUNMILL_CALLBACK_STATUS": status,
                "FUNMILL_CALLBACK_PAYLOAD": payload,
            },
            "retry_policy": {"limit": 3, "interval_sec": 2},
        }

    def _submit(
        self,
        steps: list[dict[str, Any]],
        callback_url: str | None,
        dag_name: str = _DAG_NAME,
    ) -> str:
        spec: dict[str, Any] = {"name": dag_name, "steps": steps}
        if callback_url:
            success_callback = self._callback("succeeded", callback_url)
            success_callback.update(
                {"id": "funmill_callback", "depends": ["funmill_result"]}
            )
            steps.append(success_callback)
            spec["handler_on"] = {
                "failure": self._callback("failed", callback_url),
                "abort": self._callback("canceled", callback_url),
            }
        response = self._request(
            "POST", "dag-runs", json={"spec": json.dumps(spec, separators=(",", ":"))}
        )
        return self._task_id(response.json().get("dagRunId"))

    def submit_task(self, task: TaskSubmit) -> SubmitResult:
        steps = []
        dependencies = []
        if task.depends_on:
            steps.append(
                self._dependency_step(task.depends_on, task.dependency_timeout_seconds)
            )
            dependencies.append("funmill_wait")
        steps.append(self._task_step("task", task, dependencies))
        steps.append(self._result_step(["task"]))
        dag_name = _dag_run_name(task.name)
        task_id = self._submit(
            steps,
            str(task.callback_url) if task.callback_url else None,
            dag_name=dag_name,
        )
        return SubmitResult(task_id=task_id, ui_url=self._ui_url(dag_name, task_id))

    def submit_workflow(self, workflow: WorkflowSubmit) -> SubmitResult:
        steps = []
        if workflow.depends_on:
            steps.append(
                self._dependency_step(
                    workflow.depends_on, workflow.dependency_timeout_seconds
                )
            )
        for task in workflow.tasks:
            dependencies = list(task.depends_on)
            if workflow.depends_on and not dependencies:
                dependencies.append("funmill_wait")
            steps.append(self._task_step(task.key, task, dependencies))
        task_ids = [task.key for task in workflow.tasks]
        steps.append(self._result_step(task_ids))
        task_id = self._submit(
            steps, str(workflow.callback_url) if workflow.callback_url else None
        )
        return SubmitResult(task_id=task_id, ui_url=self._ui_url(_DAG_NAME, task_id))

    def _resolve_dag_name(self, task_id: str) -> str:
        response = self._request("GET", "dag-runs", params={"dagRunId": task_id}).json()
        runs = response.get("dagRuns") or []
        if not runs:
            raise BackendError(f"Dagu has no run with ID {task_id!r}", 404)
        return runs[0].get("name") or _DAG_NAME

    def _with_dag_name(
        self, task_id: str, request: Callable[[str], Any]
    ) -> tuple[str, Any]:
        # "funmill" is a fast-path guess correct for every unnamed task and
        # every workflow; only a named single-task submission needs the
        # extra list-filter round trip, and only on the first 404.
        try:
            return _DAG_NAME, request(quote(_DAG_NAME, safe=""))
        except BackendError as exc:
            if exc.status_code != 404:
                raise
            name = self._resolve_dag_name(task_id)
            return name, request(quote(name, safe=""))

    def _details(self, task_id: str) -> tuple[str, dict[str, Any]]:
        task_id = self._task_id(task_id)

        def fetch(encoded_name: str) -> dict[str, Any]:
            response = self._request("GET", f"dag-runs/{encoded_name}/{task_id}").json()
            try:
                return response["dagRunDetails"]
            except (KeyError, TypeError) as exc:
                raise BackendError("Dagu returned invalid task details") from exc

        return self._with_dag_name(task_id, fetch)

    @staticmethod
    def _status(details: dict[str, Any]) -> TaskStatus:
        states = {
            "not_started": TaskStatus.QUEUED,
            "queued": TaskStatus.QUEUED,
            "running": TaskStatus.RUNNING,
            "waiting": TaskStatus.RUNNING,
            "succeeded": TaskStatus.SUCCEEDED,
            "failed": TaskStatus.FAILED,
            "partially_succeeded": TaskStatus.FAILED,
            "rejected": TaskStatus.FAILED,
            "aborted": TaskStatus.CANCELED,
        }
        try:
            return states[details["statusLabel"]]
        except KeyError as exc:
            raise BackendError("Dagu returned an unknown task status") from exc

    @staticmethod
    def _task_step_node(details: dict[str, Any]) -> dict[str, Any]:
        for node in details.get("nodes", []):
            step = node.get("step", {})
            if step.get("id") == "task":
                return step
        return {}

    def get_task(self, task_id: str) -> TaskInfo:
        dag_name, details = self._details(task_id)
        step = self._task_step_node(details)
        name = step.get("name")
        if name == step.get("id"):
            # Dagu auto-generates the step name from its id when none was set.
            name = None
        return TaskInfo(
            task_id=task_id,
            status=self._status(details),
            created_at=details.get("queuedAt") or details.get("startedAt") or None,
            started_at=details.get("startedAt") or None,
            completed_at=details.get("finishedAt") or None,
            name=name,
            description=step.get("description"),
            ui_url=self._ui_url(dag_name, task_id),
        )

    def get_progress(self, task_id: str) -> TaskProgress:
        _, details = self._details(task_id)
        if self._status(details) == TaskStatus.SUCCEEDED:
            return TaskProgress(task_id=task_id, progress=100)
        nodes = [
            node
            for node in details.get("nodes", [])
            if not (node.get("step", {}).get("id") or "").startswith("funmill_")
        ]
        completed = sum(
            node.get("statusLabel") in _TERMINAL_NODE_STATES for node in nodes
        )
        progress = int(completed * 100 / len(nodes)) if nodes else None
        return TaskProgress(task_id=task_id, progress=progress)

    def get_logs(self, task_id: str) -> TaskLogs:
        task_id = self._task_id(task_id)
        name, details = self._details(task_id)
        encoded_name = quote(name, safe="")
        parts = []
        for node in details.get("nodes", []):
            step = node.get("step", {}).get("name")
            if not step or node.get("statusLabel") == "not_started":
                continue
            encoded_step = quote(step, safe="")
            # ponytail: Dagu lacks combined logs; use one request per stream.
            for stream in ("stdout", "stderr"):
                try:
                    content = (
                        self._request(
                            "GET",
                            f"dag-runs/{encoded_name}/{task_id}/steps/{encoded_step}/log",
                            params={"stream": stream},
                        )
                        .json()
                        .get("content", "")
                    )
                except BackendError as exc:
                    if exc.status_code == 404:
                        continue
                    raise
                if content:
                    parts.append(f"[{step} {stream}]\n{content}")
        return TaskLogs(task_id=task_id, logs="\n".join(parts))

    def get_result(self, task_id: str) -> TaskResult:
        task_id = self._task_id(task_id)
        name, details = self._details(task_id)
        encoded_name = quote(name, safe="")
        terminal = self._status(details) not in {
            TaskStatus.QUEUED,
            TaskStatus.RUNNING,
        }
        for attempt in range(10):
            payload = self._request(
                "GET", f"dag-runs/{encoded_name}/{task_id}/outputs"
            ).json()
            if (
                not terminal
                or payload.get("metadata", {}).get("status")
                or attempt == 9
            ):
                break
            # Dagu marks a run complete just before publishing outputs.json.
            time.sleep(0.05)
        outputs = payload.get("outputs", {})
        return TaskResult(task_id=task_id, result=_decode_result(outputs.get("result")))

    def cancel(self, task_id: str, reason: str) -> None:
        del reason
        task_id = self._task_id(task_id)

        def stop(encoded_name: str) -> None:
            self._request("POST", f"dag-runs/{encoded_name}/{task_id}/stop")

        self._with_dag_name(task_id, stop)

    def rerun(self, task_id: str) -> SubmitResult:
        task_id = self._task_id(task_id)

        def reschedule(encoded_name: str) -> str:
            response = self._request(
                "POST", f"dag-runs/{encoded_name}/{task_id}/reschedule", json={}
            )
            return self._task_id(response.json().get("dagRunId"))

        name, new_task_id = self._with_dag_name(task_id, reschedule)
        return SubmitResult(task_id=new_task_id, ui_url=self._ui_url(name, new_task_id))

    def close(self) -> None:
        self.client.close()
