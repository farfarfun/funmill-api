import os
import secrets
from contextlib import asynccontextmanager
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from typing import Annotated

from fastapi import APIRouter, Body, Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader

from funmill.api.backends import (
    DEFAULT_BACKEND,
    BackendError,
    TaskBackend,
    get_backend,
)
from funmill.api.models import (
    CancelRequest,
    TaskAccepted,
    TaskInfo,
    TaskLogs,
    TaskProgress,
    TaskResult,
    TaskSubmit,
    WorkflowBatchCreateRequest,
    WorkflowBatchDeleteItemResult,
    WorkflowBatchDeleteRequest,
    WorkflowBatchDeleteResult,
    WorkflowBatchGetRequest,
    WorkflowBatchItemResult,
    WorkflowBatchResult,
    WorkflowBatchRunItemResult,
    WorkflowBatchRunRequest,
    WorkflowBatchRunResult,
    WorkflowBatchUpdateRequest,
    WorkflowCreate,
    WorkflowInfo,
    WorkflowList,
    WorkflowRunRequest,
    WorkflowSubmit,
    WorkflowUpdate,
)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(api_key: Annotated[str | None, Depends(api_key_header)]) -> None:
    expected = os.getenv("FUNMILL_API_KEY", "")
    if not expected:
        raise BackendError("FUNMILL_API_KEY is not configured", 503)
    if api_key is None or not secrets.compare_digest(api_key, expected):
        raise BackendError("invalid API key", 401)


@lru_cache
def backend_dependency() -> TaskBackend:
    return get_backend()


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    if backend_dependency.cache_info().currsize:
        backend_dependency().close()
        backend_dependency.cache_clear()


def _app_version() -> str:
    try:
        return version("funmill-api")
    except PackageNotFoundError:
        return "0.0.0"


app = FastAPI(title="Funmill", version=_app_version(), lifespan=lifespan)
router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])
Backend = Annotated[TaskBackend, Depends(backend_dependency)]


@app.exception_handler(BackendError)
async def backend_error_handler(_, exc: BackendError):
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


@app.get("/health")
def health(backend: Backend):
    backend.health_check()
    return {
        "status": "ok",
        "backend": os.getenv("FUNMILL_BACKEND", DEFAULT_BACKEND).lower(),
    }


def _logs_url(request: Request, task_id: str) -> str:
    return str(request.url_for("get_logs", task_id=task_id))


@router.post("/tasks", response_model=TaskAccepted, status_code=202)
def submit_task(task: TaskSubmit, backend: Backend, request: Request):
    result = backend.submit_task(task)
    return TaskAccepted(
        task_id=result.task_id,
        logs_url=_logs_url(request, result.task_id),
        ui_url=result.ui_url,
    )


@router.post("/workflows", response_model=TaskAccepted, status_code=202)
def submit_workflow(workflow: WorkflowSubmit, backend: Backend, request: Request):
    result = backend.submit_workflow(workflow)
    return TaskAccepted(
        task_id=result.task_id,
        logs_url=_logs_url(request, result.task_id),
        ui_url=result.ui_url,
    )


@router.get("/tasks/{task_id}", response_model=TaskInfo)
def get_task(task_id: str, backend: Backend):
    return backend.get_task(task_id)


@router.get("/tasks/{task_id}/progress", response_model=TaskProgress)
def get_progress(task_id: str, backend: Backend):
    return backend.get_progress(task_id)


@router.get("/tasks/{task_id}/logs", response_model=TaskLogs, name="get_logs")
def get_logs(task_id: str, backend: Backend):
    return backend.get_logs(task_id)


@router.get("/tasks/{task_id}/result", response_model=TaskResult)
def get_result(task_id: str, backend: Backend):
    return backend.get_result(task_id)


@router.post("/tasks/{task_id}/cancel", status_code=204)
def cancel(task_id: str, request: CancelRequest, backend: Backend) -> None:
    backend.cancel(task_id, request.reason)


@router.post("/tasks/{task_id}/rerun", response_model=TaskAccepted, status_code=202)
def rerun(task_id: str, backend: Backend, request: Request):
    result = backend.rerun(task_id)
    return TaskAccepted(
        task_id=result.task_id,
        rerun_of=task_id,
        logs_url=_logs_url(request, result.task_id),
        ui_url=result.ui_url,
    )


@router.post(
    "/workflow-definitions/batch",
    response_model=WorkflowBatchResult,
    status_code=200,
)
def batch_create_workflows(
    batch: WorkflowBatchCreateRequest, backend: Backend
) -> WorkflowBatchResult:
    results: list[WorkflowBatchItemResult] = []
    for item in batch.items:
        try:
            created = backend.create_workflow(item)
            results.append(
                WorkflowBatchItemResult(
                    workflow_id=item.workflow_id, ok=True, workflow=created
                )
            )
        except BackendError as exc:
            results.append(
                WorkflowBatchItemResult(
                    workflow_id=item.workflow_id,
                    ok=False,
                    error=str(exc),
                    status_code=exc.status_code,
                )
            )
    succeeded = sum(item.ok for item in results)
    return WorkflowBatchResult(
        total=len(results),
        succeeded=succeeded,
        failed=len(results) - succeeded,
        items=results,
    )


@router.post(
    "/workflow-definitions/batch/get",
    response_model=WorkflowBatchResult,
    status_code=200,
)
def batch_get_workflows(
    batch: WorkflowBatchGetRequest, backend: Backend
) -> WorkflowBatchResult:
    results: list[WorkflowBatchItemResult] = []
    for workflow_id in batch.workflow_ids:
        try:
            found = backend.get_workflow(workflow_id)
            results.append(
                WorkflowBatchItemResult(
                    workflow_id=workflow_id, ok=True, workflow=found
                )
            )
        except BackendError as exc:
            results.append(
                WorkflowBatchItemResult(
                    workflow_id=workflow_id,
                    ok=False,
                    error=str(exc),
                    status_code=exc.status_code,
                )
            )
    succeeded = sum(item.ok for item in results)
    return WorkflowBatchResult(
        total=len(results),
        succeeded=succeeded,
        failed=len(results) - succeeded,
        items=results,
    )


@router.put(
    "/workflow-definitions/batch",
    response_model=WorkflowBatchResult,
    status_code=200,
)
def batch_update_workflows(
    batch: WorkflowBatchUpdateRequest, backend: Backend
) -> WorkflowBatchResult:
    results: list[WorkflowBatchItemResult] = []
    for item in batch.items:
        try:
            update = WorkflowUpdate(
                tasks=item.tasks,
                name=item.name,
                description=item.description,
                schedule=item.schedule,
                labels=item.labels,
                callback_url=item.callback_url,
            )
            updated = backend.update_workflow(item.workflow_id, update)
            results.append(
                WorkflowBatchItemResult(
                    workflow_id=item.workflow_id, ok=True, workflow=updated
                )
            )
        except BackendError as exc:
            results.append(
                WorkflowBatchItemResult(
                    workflow_id=item.workflow_id,
                    ok=False,
                    error=str(exc),
                    status_code=exc.status_code,
                )
            )
    succeeded = sum(item.ok for item in results)
    return WorkflowBatchResult(
        total=len(results),
        succeeded=succeeded,
        failed=len(results) - succeeded,
        items=results,
    )


@router.post(
    "/workflow-definitions/batch/delete",
    response_model=WorkflowBatchDeleteResult,
    status_code=200,
)
def batch_delete_workflows(
    batch: WorkflowBatchDeleteRequest, backend: Backend
) -> WorkflowBatchDeleteResult:
    results: list[WorkflowBatchDeleteItemResult] = []
    for workflow_id in batch.workflow_ids:
        try:
            backend.delete_workflow(workflow_id)
            results.append(
                WorkflowBatchDeleteItemResult(workflow_id=workflow_id, ok=True)
            )
        except BackendError as exc:
            results.append(
                WorkflowBatchDeleteItemResult(
                    workflow_id=workflow_id,
                    ok=False,
                    error=str(exc),
                    status_code=exc.status_code,
                )
            )
    succeeded = sum(item.ok for item in results)
    return WorkflowBatchDeleteResult(
        total=len(results),
        succeeded=succeeded,
        failed=len(results) - succeeded,
        items=results,
    )


@router.post(
    "/workflow-definitions/batch/run",
    response_model=WorkflowBatchRunResult,
    status_code=200,
)
def batch_run_workflows(
    batch: WorkflowBatchRunRequest, backend: Backend, request: Request
) -> WorkflowBatchRunResult:
    results: list[WorkflowBatchRunItemResult] = []
    for item in batch.items:
        try:
            run_req = WorkflowRunRequest(task_id=item.task_id, params=item.params)
            submitted = backend.run_workflow(item.workflow_id, run_req)
            accepted = TaskAccepted(
                task_id=submitted.task_id,
                logs_url=_logs_url(request, submitted.task_id),
                ui_url=submitted.ui_url,
            )
            results.append(
                WorkflowBatchRunItemResult(
                    workflow_id=item.workflow_id, ok=True, accepted=accepted
                )
            )
        except BackendError as exc:
            results.append(
                WorkflowBatchRunItemResult(
                    workflow_id=item.workflow_id,
                    ok=False,
                    error=str(exc),
                    status_code=exc.status_code,
                )
            )
    succeeded = sum(item.ok for item in results)
    return WorkflowBatchRunResult(
        total=len(results),
        succeeded=succeeded,
        failed=len(results) - succeeded,
        items=results,
    )


@router.post(
    "/workflow-definitions",
    response_model=WorkflowInfo,
    status_code=201,
)
def create_workflow(workflow: WorkflowCreate, backend: Backend) -> WorkflowInfo:
    return backend.create_workflow(workflow)


@router.get("/workflow-definitions", response_model=WorkflowList)
def list_workflows(
    backend: Backend,
    name: str | None = None,
    label: str | None = None,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=100, ge=1, le=500),
) -> WorkflowList:
    return backend.list_workflows(name=name, label=label, page=page, per_page=per_page)


@router.get("/workflow-definitions/{workflow_id}", response_model=WorkflowInfo)
def get_workflow(workflow_id: str, backend: Backend) -> WorkflowInfo:
    return backend.get_workflow(workflow_id)


@router.put("/workflow-definitions/{workflow_id}", response_model=WorkflowInfo)
def update_workflow(
    workflow_id: str, workflow: WorkflowUpdate, backend: Backend
) -> WorkflowInfo:
    return backend.update_workflow(workflow_id, workflow)


@router.delete("/workflow-definitions/{workflow_id}", status_code=204)
def delete_workflow(workflow_id: str, backend: Backend) -> None:
    backend.delete_workflow(workflow_id)


@router.post(
    "/workflow-definitions/{workflow_id}/run",
    response_model=TaskAccepted,
    status_code=202,
)
def run_workflow(
    workflow_id: str,
    backend: Backend,
    request: Request,
    run_request: Annotated[WorkflowRunRequest | None, Body()] = None,
) -> TaskAccepted:
    result = backend.run_workflow(workflow_id, run_request or WorkflowRunRequest())
    return TaskAccepted(
        task_id=result.task_id,
        logs_url=_logs_url(request, result.task_id),
        ui_url=result.ui_url,
    )


app.include_router(router)
