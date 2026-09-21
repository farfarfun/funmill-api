from abc import ABC, abstractmethod

from funmill.models import (
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


class TaskBackend(ABC):
    name: str

    @abstractmethod
    def health_check(self) -> None:
        """Raise BackendError if the backend is unreachable or misconfigured."""

    @abstractmethod
    def submit_task(self, task: TaskSubmit) -> str: ...

    @abstractmethod
    def submit_workflow(self, workflow: WorkflowSubmit) -> str: ...

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
    def rerun(self, task_id: str) -> str: ...

    @abstractmethod
    def close(self) -> None: ...
