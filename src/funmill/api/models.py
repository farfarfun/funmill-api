from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Self

from pydantic import AnyHttpUrl, BaseModel, Field, model_validator

DependencyId = Annotated[str, Field(min_length=1, max_length=200)]


class TaskLanguage(StrEnum):
    PYTHON = "python"
    BASH = "bash"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class RetryPolicy(BaseModel):
    attempts: int = Field(default=0, ge=0, le=10)
    delay_seconds: int = Field(default=1, ge=0, le=3600)


class TaskDefinition(BaseModel):
    language: TaskLanguage
    source: str = Field(min_length=1, max_length=1_000_000)
    args: dict[str, Any] = Field(default_factory=dict)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    timeout_seconds: int | None = Field(default=None, ge=1, le=86_400)


class TaskSubmit(TaskDefinition):
    depends_on: list[DependencyId] = Field(default_factory=list, max_length=100)
    dependency_timeout_seconds: int = Field(default=86_400, ge=1, le=604_800)
    callback_url: AnyHttpUrl | None = None


class WorkflowTask(TaskDefinition):
    key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    depends_on: list[str] = Field(default_factory=list, max_length=100)


class WorkflowSubmit(BaseModel):
    tasks: list[WorkflowTask] = Field(min_length=1, max_length=100)
    depends_on: list[DependencyId] = Field(default_factory=list, max_length=100)
    dependency_timeout_seconds: int = Field(default=86_400, ge=1, le=604_800)
    callback_url: AnyHttpUrl | None = None

    @model_validator(mode="after")
    def validate_dependencies(self) -> Self:
        keys = [task.key for task in self.tasks]
        if len(keys) != len(set(keys)):
            raise ValueError("workflow task keys must be unique")
        if any(key == "failure" or key.startswith("funmill_") for key in keys):
            raise ValueError("task keys cannot be 'failure' or start with 'funmill_'")

        known = set(keys)
        for task in self.tasks:
            unknown = set(task.depends_on) - known
            if unknown:
                raise ValueError(
                    f"task {task.key!r} has unknown dependencies: {sorted(unknown)}"
                )
        self.topological_layers()
        return self

    def topological_layers(self) -> list[list[WorkflowTask]]:
        by_key = {task.key: task for task in self.tasks}
        remaining = {task.key: set(task.depends_on) for task in self.tasks}
        layers: list[list[WorkflowTask]] = []
        while remaining:
            ready = [key for key, dependencies in remaining.items() if not dependencies]
            if not ready:
                raise ValueError("workflow contains a dependency cycle")
            layers.append([by_key[key] for key in ready])
            ready_set = set(ready)
            remaining = {
                key: dependencies - ready_set
                for key, dependencies in remaining.items()
                if key not in ready_set
            }
        return layers


class TaskAccepted(BaseModel):
    task_id: str
    status: TaskStatus = TaskStatus.QUEUED
    rerun_of: str | None = None


class TaskInfo(BaseModel):
    task_id: str
    status: TaskStatus
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None


class TaskProgress(BaseModel):
    task_id: str
    progress: int | None


class TaskLogs(BaseModel):
    task_id: str
    logs: str


class TaskResult(BaseModel):
    task_id: str
    result: Any


class CancelRequest(BaseModel):
    reason: str = Field(default="canceled through Funmill", max_length=500)
