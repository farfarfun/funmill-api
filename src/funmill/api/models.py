from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import AfterValidator, AnyHttpUrl, BaseModel, Field, model_validator

DependencyId = Annotated[str, Field(min_length=1, max_length=200)]


def _reject_reserved_task_id(value: str) -> str:
    # Dagu treats "latest" as a magic run selector in its own URLs.
    if value == "latest":
        raise ValueError("task_id 'latest' is reserved by the backend")
    return value


def _reject_reserved_workflow_id(value: str) -> str:
    if value in {"batch", "latest"}:
        raise ValueError(f"workflow_id {value!r} is reserved")
    return value


TaskId = Annotated[
    str,
    Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_-]+$"),
    AfterValidator(_reject_reserved_task_id),
]
WorkflowId = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$"),
    AfterValidator(_reject_reserved_workflow_id),
]
WorkflowLabel = Annotated[
    str,
    Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:=-]+$"),
]
EnvName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=100)]
EnvValue = Annotated[str, Field(max_length=10_000)]
ResourceName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
ResourceQuantity = Annotated[int, Field(strict=True, ge=1)]
WaitingReason = Literal[
    "dependency", "resource_unavailable", "external_resource_in_use"
]


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
    env: dict[EnvName, EnvValue] = Field(default_factory=dict, max_length=100)
    resources: dict[ResourceName, ResourceQuantity] = Field(
        default_factory=dict, max_length=16
    )
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    timeout_seconds: int | None = Field(default=None, ge=1, le=86_400)
    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2_000)


class TaskSubmit(TaskDefinition):
    task_id: TaskId | None = None
    depends_on: list[DependencyId] = Field(default_factory=list, max_length=100)
    dependency_timeout_seconds: int | None = Field(default=None, ge=1, le=604_800)
    callback_url: AnyHttpUrl | None = None


class WorkflowTask(TaskDefinition):
    key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    depends_on: list[str] = Field(default_factory=list, max_length=100)


class WorkflowSubmit(BaseModel):
    tasks: list[WorkflowTask] = Field(min_length=1, max_length=100)
    task_id: TaskId | None = None
    depends_on: list[DependencyId] = Field(default_factory=list, max_length=100)
    dependency_timeout_seconds: int | None = Field(default=None, ge=1, le=604_800)
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
    logs_url: str
    ui_url: str | None = None


class TaskInfo(BaseModel):
    task_id: str
    status: TaskStatus
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None
    name: str | None = None
    description: str | None = None
    ui_url: str | None = None
    waiting_reason: WaitingReason | None = None
    waiting_resource: str | None = None
    worker_id: str | None = None
    resource_allocations: dict[str, list[str]] | None = None


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


class WorkflowDefinitionBase(BaseModel):
    tasks: list[WorkflowTask] = Field(min_length=1, max_length=100)
    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2_000)
    schedule: list[str] = Field(default_factory=list, max_length=20)
    labels: list[WorkflowLabel] = Field(default_factory=list, max_length=32)
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


class WorkflowCreate(WorkflowDefinitionBase):
    workflow_id: WorkflowId


class WorkflowUpdate(WorkflowDefinitionBase):
    pass


class WorkflowInfo(BaseModel):
    workflow_id: str
    name: str | None = None
    description: str | None = None
    tasks: list[WorkflowTask] = Field(default_factory=list)
    schedule: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    callback_url: AnyHttpUrl | None = None
    suspended: bool = False
    ui_url: str | None = None


class WorkflowSummary(BaseModel):
    workflow_id: str
    name: str | None = None
    description: str | None = None
    schedule: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    suspended: bool = False
    ui_url: str | None = None


class WorkflowList(BaseModel):
    items: list[WorkflowSummary]
    total: int
    page: int = 1
    per_page: int = 100


class WorkflowRunRequest(BaseModel):
    task_id: TaskId | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class WorkflowBatchCreateRequest(BaseModel):
    items: list[WorkflowCreate] = Field(min_length=1, max_length=100)


class WorkflowBatchGetRequest(BaseModel):
    workflow_ids: list[WorkflowId] = Field(min_length=1, max_length=100)


class WorkflowBatchUpdateItem(WorkflowUpdate):
    workflow_id: WorkflowId


class WorkflowBatchUpdateRequest(BaseModel):
    items: list[WorkflowBatchUpdateItem] = Field(min_length=1, max_length=100)


class WorkflowBatchDeleteRequest(BaseModel):
    workflow_ids: list[WorkflowId] = Field(min_length=1, max_length=100)


class WorkflowBatchRunItem(WorkflowRunRequest):
    workflow_id: WorkflowId


class WorkflowBatchRunRequest(BaseModel):
    items: list[WorkflowBatchRunItem] = Field(min_length=1, max_length=100)


class WorkflowBatchItemResult(BaseModel):
    workflow_id: str
    ok: bool
    workflow: WorkflowInfo | None = None
    error: str | None = None
    status_code: int | None = None


class WorkflowBatchResult(BaseModel):
    total: int
    succeeded: int
    failed: int
    items: list[WorkflowBatchItemResult]


class WorkflowBatchDeleteItemResult(BaseModel):
    workflow_id: str
    ok: bool
    error: str | None = None
    status_code: int | None = None


class WorkflowBatchDeleteResult(BaseModel):
    total: int
    succeeded: int
    failed: int
    items: list[WorkflowBatchDeleteItemResult]


class WorkflowBatchRunItemResult(BaseModel):
    workflow_id: str
    ok: bool
    accepted: TaskAccepted | None = None
    error: str | None = None
    status_code: int | None = None


class WorkflowBatchRunResult(BaseModel):
    total: int
    succeeded: int
    failed: int
    items: list[WorkflowBatchRunItemResult]
