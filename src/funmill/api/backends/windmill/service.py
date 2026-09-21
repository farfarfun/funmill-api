import hashlib
import os
import platform
import re
import shutil
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

from funmill.api.ports import SERVICE_BIND_HOST, THIRD_PARTY_WEB_PORT

from ..service import start_background, status_background, stop_background

VERSION = "v1.808.0"
URL = f"https://github.com/windmill-labs/windmill/releases/download/{VERSION}/windmill-amd64"
SHA256 = "ed48bfb9a391daa437f0c867376f009c7186855530de7fe2f58cf557ff1f7c3a"
_ENV_TEMPLATE = f"""DATABASE_URL=
MODE=standalone
SERVER_BIND_ADDR={SERVICE_BIND_HOST}
"""


def _home() -> Path:
    return Path(os.getenv("FUNMILL_HOME", Path.home() / ".farfarfun" / "funmill"))


def _target() -> Path:
    return _home() / "services" / "windmill" / "windmill"


def _config_path() -> Path:
    return _target().parent / ".env"


def _ensure_config() -> Path:
    path = _config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_ENV_TEMPLATE, encoding="utf-8")
        path.chmod(0o600)
        print(f"created config: {path}")
    return path


def _read_config(path: Path) -> dict[str, str]:
    values = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key.isidentifier():
            raise RuntimeError(f"invalid config at {path}:{line_number}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def _sha256(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def _environment() -> dict[str, str]:
    path = _config_path()
    environment = _read_config(path) if path.exists() else {}
    environment.update(os.environ)
    return environment


def _instance_name(environment: dict[str, str]) -> str:
    if environment.get("MODE", "standalone") != "worker":
        return "windmill"
    suffix = environment.get("WORKER_SUFFIX", "worker")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", suffix):
        raise RuntimeError("WORKER_SUFFIX must contain only letters, numbers, _ or -")
    return f"windmill-{suffix}"


def install(force: bool = False) -> Path:
    if platform.system() != "Linux" or platform.machine().lower() not in {
        "x86_64",
        "amd64",
    }:
        raise RuntimeError("Windmill v1.808.0 installer only supports Linux x86_64")

    target = _target()
    _ensure_config()
    if target.exists() and _sha256(target) == SHA256:
        return target
    if target.exists() and not force:
        raise RuntimeError(
            f"{target} exists but has an unexpected checksum; use --force"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=target.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        print(f"downloading Windmill {VERSION}...", flush=True)
        request = Request(URL, headers={"User-Agent": "funmill"})
        with urlopen(request, timeout=30) as response, temporary.open("wb") as file:
            shutil.copyfileobj(response, file)
        if _sha256(temporary) != SHA256:
            raise RuntimeError("downloaded Windmill binary failed SHA-256 verification")
        temporary.chmod(0o755)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def start() -> None:
    executable = _target()
    if not executable.exists():
        system_executable = shutil.which("windmill")
        if system_executable is None:
            raise RuntimeError(
                "Windmill is not installed; run: funmill install windmill"
            )
        executable = Path(system_executable)

    config = _ensure_config()
    environment = _environment()
    if not environment.get("DATABASE_URL"):
        raise RuntimeError(f"DATABASE_URL is required; edit {config}")

    mode = environment.setdefault("MODE", "standalone")
    if mode in {"standalone", "server"}:
        environment["PORT"] = str(THIRD_PARTY_WEB_PORT)
        environment.setdefault("BASE_URL", f"http://127.0.0.1:{THIRD_PARTY_WEB_PORT}")
    environment["SERVER_BIND_ADDR"] = SERVICE_BIND_HOST
    start_background(
        _instance_name(environment), [str(executable)], environment, _target().parent
    )


def stop() -> None:
    environment = _environment()
    stop_background(_instance_name(environment), _target().parent)


def status() -> bool:
    environment = _environment()
    return status_background(_instance_name(environment), _target().parent)
