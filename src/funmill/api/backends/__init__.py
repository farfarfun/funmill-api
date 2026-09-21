import importlib
import os
from typing import Any, NamedTuple

from .base import BackendError, TaskBackend


class BackendSpec(NamedTuple):
    module: str
    cls: str
    service: str | None = None


BACKEND_SPECS = {
    "dagu": BackendSpec(".dagu", "DaguBackend", ".dagu.service"),
    "windmill": BackendSpec(".windmill", "WindmillBackend", ".windmill.service"),
}


def get_backend(backend_type: str | None = None, **kwargs: Any) -> TaskBackend:
    key = (backend_type or os.getenv("FUNMILL_BACKEND", "windmill")).lower()
    spec = BACKEND_SPECS.get(key)
    if spec is None:
        raise ValueError(
            f"unsupported backend {key!r}; available: {', '.join(BACKEND_SPECS)}"
        )
    module = importlib.import_module(spec.module, __name__)
    backend_class = getattr(module, spec.cls)
    return backend_class(**kwargs) if kwargs else backend_class.from_env()


def list_backends() -> list[str]:
    return list(BACKEND_SPECS)


__all__ = [
    "BACKEND_SPECS",
    "BackendError",
    "TaskBackend",
    "get_backend",
    "list_backends",
]
