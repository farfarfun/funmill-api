import os
import sys
from pathlib import Path

from funmill.api.backends.service import (
    start_background,
    status_background,
    stop_background,
)
from funmill.api.ports import FUNMILL_API_PORT, SERVICE_BIND_HOST

_NAME = "api"


def _home() -> Path:
    return Path(os.getenv("FUNMILL_HOME", Path.home() / ".farfarfun" / "funmill"))


def _directory() -> Path:
    return _home() / "services" / _NAME


def start() -> None:
    start_background(
        _NAME,
        [
            sys.executable,
            "-m",
            "uvicorn",
            "funmill.api:app",
            "--host",
            SERVICE_BIND_HOST,
            "--port",
            str(FUNMILL_API_PORT),
        ],
        os.environ.copy(),
        _directory(),
    )


def run() -> None:
    import uvicorn

    uvicorn.run("funmill.api:app", host=SERVICE_BIND_HOST, port=FUNMILL_API_PORT)


def stop() -> None:
    stop_background(_NAME, _directory())


def status() -> bool:
    return status_background(_NAME, _directory())
