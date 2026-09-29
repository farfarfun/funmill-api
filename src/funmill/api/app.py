import os
import secrets
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader

from funmill.api.backends import BackendError, TaskBackend, get_backend
from funmill.api.models import (
    CancelRequest,
    TaskAccepted,
    TaskInfo,
    TaskLogs,
    TaskProgress,
    TaskResult,
    TaskSubmit,
    WorkflowSubmit,
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


app = FastAPI(title="Funmill", version="0.1.0", lifespan=lifespan)
router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])
Backend = Annotated[TaskBackend, Depends(backend_dependency)]


@app.exception_handler(BackendError)
async def backend_error_handler(_, exc: BackendError):
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


@app.get("/health")
def health(backend: Backend):
    backend.health_check()
    return {"status": "ok", "backend": os.getenv("FUNMILL_BACKEND", "windmill")}


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


app.include_router(router)
