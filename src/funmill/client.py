import os
from typing import Any

import httpx

from funmill.models import (
    TaskAccepted,
    TaskInfo,
    TaskLogs,
    TaskProgress,
    TaskResult,
    TaskSubmit,
    WorkflowSubmit,
)
from funmill.ports import FUNMILL_API_PORT


class FunmillAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class FunmillClient:
    """HTTP client for the Funmill task API, for use by external services."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 30,
        client: httpx.Client | None = None,
    ) -> None:
        self.client = client or httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={"X-API-Key": api_key},
        )

    @classmethod
    def from_env(cls) -> "FunmillClient":
        return cls(
            base_url=os.getenv("FUNMILL_URL", f"http://127.0.0.1:{FUNMILL_API_PORT}"),
            api_key=os.getenv("FUNMILL_API_KEY", ""),
            timeout=float(os.getenv("FUNMILL_TIMEOUT", "30")),
        )

    def __enter__(self) -> "FunmillClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        self.client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise FunmillAPIError("Funmill request timed out", 504) from exc
        except httpx.HTTPError as exc:
            raise FunmillAPIError(f"Funmill is unavailable: {exc}", 502) from exc
        if response.is_error:
            try:
                detail = response.json().get("detail")
            except ValueError:
                detail = None
            raise FunmillAPIError(
                detail or f"HTTP {response.status_code}", response.status_code
            )
        return response

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health").json()

    def submit_task(self, task: TaskSubmit) -> TaskAccepted:
        response = self._request("POST", "/v1/tasks", json=task.model_dump(mode="json"))
        return TaskAccepted.model_validate(response.json())

    def submit_workflow(self, workflow: WorkflowSubmit) -> TaskAccepted:
        response = self._request(
            "POST", "/v1/workflows", json=workflow.model_dump(mode="json")
        )
        return TaskAccepted.model_validate(response.json())

    def get_task(self, task_id: str) -> TaskInfo:
        response = self._request("GET", f"/v1/tasks/{task_id}")
        return TaskInfo.model_validate(response.json())

    def get_progress(self, task_id: str) -> TaskProgress:
        response = self._request("GET", f"/v1/tasks/{task_id}/progress")
        return TaskProgress.model_validate(response.json())

    def get_logs(self, task_id: str) -> TaskLogs:
        response = self._request("GET", f"/v1/tasks/{task_id}/logs")
        return TaskLogs.model_validate(response.json())

    def get_result(self, task_id: str) -> TaskResult:
        response = self._request("GET", f"/v1/tasks/{task_id}/result")
        return TaskResult.model_validate(response.json())

    def cancel(self, task_id: str, reason: str = "canceled through Funmill") -> None:
        self._request("POST", f"/v1/tasks/{task_id}/cancel", json={"reason": reason})

    def rerun(self, task_id: str) -> TaskAccepted:
        response = self._request("POST", f"/v1/tasks/{task_id}/rerun")
        return TaskAccepted.model_validate(response.json())
