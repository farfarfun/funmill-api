from abc import ABC, abstractmethod
from dataclasses import dataclass

from funmill.api.models import (
    TaskInfo,
    TaskLogs,
    TaskProgress,
    TaskResult,
    TaskSubmit,
    WorkflowSubmit,
)


class BackendError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class SubmitResult:
    task_id: str
    # Best-effort deep link into the backend's own web UI for this run, for
    # humans who need more than what Funmill's API surfaces (live graph,
    # raw logs, etc). Funmill does not verify it is reachable or authorized.
    ui_url: str | None = None


class TaskBackend(ABC):
    name: str

    @abstractmethod
    def health_check(self) -> None:
        """Raise BackendError if the backend is unreachable or misconfigured."""

    @abstractmethod
    def submit_task(self, task: TaskSubmit) -> SubmitResult: ...

    @abstractmethod
    def submit_workflow(self, workflow: WorkflowSubmit) -> SubmitResult: ...

    @abstractmethod
    def get_task(self, task_id: str) -> TaskInfo: ...

    @abstractmethod
    def get_progress(self, task_id: str) -> TaskProgress: ...

    @abstractmethod
    def get_logs(self, task_id: str) -> TaskLogs: ...

    @abstractmethod
    def get_result(self, task_id: str) -> TaskResult: ...

    @abstractmethod
    def cancel(self, task_id: str, reason: str) -> None: ...

    @abstractmethod
    def rerun(self, task_id: str) -> SubmitResult: ...

    @abstractmethod
    def close(self) -> None: ...
