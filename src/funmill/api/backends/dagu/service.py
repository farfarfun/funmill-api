import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

from funmill.api.ports import SERVICE_BIND_HOST, THIRD_PARTY_WEB_PORT

from ..service import start_background, status_background, stop_background

VERSION = "2.17.2"
_PLATFORM_PACKAGES = {
    ("darwin", "x86_64"): "@dagucloud/dagu-darwin-x64",
    ("darwin", "arm64"): "@dagucloud/dagu-darwin-arm64",
    ("linux", "x86_64"): "@dagucloud/dagu-linux-x64",
    ("linux", "arm64"): "@dagucloud/dagu-linux-arm64",
}
_MACHINES = {"amd64": "x86_64", "aarch64": "arm64"}


def _home() -> Path:
    return Path(os.getenv("FUNMILL_HOME", Path.home() / ".farfarfun" / "funmill"))


def _target() -> Path:
    return _home() / "services" / "dagu" / "dagu"


def _npm_dir() -> Path:
    return _target().parent / "npm"


def _platform_package() -> str:
    key = (
        platform.system().lower(),
        _MACHINES.get(platform.machine().lower(), platform.machine().lower()),
    )
    try:
        return _PLATFORM_PACKAGES[key]
    except KeyError as exc:
        raise RuntimeError(
            "Dagu installer only supports macOS/Linux amd64/arm64"
        ) from exc


def install(force: bool = False) -> Path:
    target = _target()
    if target.exists() and not force:
        return target

    if shutil.which("pnpm") is None:
        raise RuntimeError(
            "pnpm is required to install Dagu but was not found on PATH; "
            "install Node.js and pnpm (https://pnpm.io/installation) first. "
            "If pnpm itself times out reaching the npm registry, configure a "
            "mirror, e.g.: pnpm config set registry https://registry.npmmirror.com"
        )

    package_name = _platform_package()
    version_spec = "latest" if force else VERSION
    npm_dir = _npm_dir()
    if force and npm_dir.exists():
        shutil.rmtree(npm_dir)
    npm_dir.mkdir(parents=True, exist_ok=True)
    (npm_dir / "package.json").write_text(
        json.dumps(
            {
                "name": "funmill-dagu-install",
                "private": True,
                "dependencies": {package_name: version_spec},
            }
        )
    )

    print(f"installing Dagu ({version_spec}) via pnpm ({package_name})...", flush=True)
    subprocess.run(
        ["pnpm", "install", "--prod", "--ignore-scripts"],
        cwd=npm_dir,
        check=True,
    )

    scope, name = package_name.split("/", 1)
    installed_binary = npm_dir / "node_modules" / scope / name / "bin" / "dagu"
    if not installed_binary.exists():
        raise RuntimeError(f"pnpm install did not produce {installed_binary}")

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(installed_binary, target)
    target.chmod(0o755)
    return target


def start() -> None:
    executable = _target()
    if not executable.exists():
        system_executable = shutil.which("dagu")
        if system_executable is None:
            raise RuntimeError("Dagu is not installed; run: funmill install dagu")
        executable = Path(system_executable)

    environment = os.environ.copy()
    environment.setdefault("DAGU_AUTH_MODE", "basic")
    environment.setdefault("DAGU_AUTH_BASIC_USERNAME", "funmill")
    environment.setdefault("DAGU_AUTH_BASIC_PASSWORD", "funmill")
    service_directory = _target().parent
    environment["DAGU_HOME"] = str(service_directory / "data")
    environment.setdefault("DAGU_COORDINATOR_ENABLED", "false")
    environment.setdefault(
        "FUNMILL_DAGU_URL", f"http://127.0.0.1:{THIRD_PARTY_WEB_PORT}/api/v1"
    )
    start_background(
        "dagu",
        [
            str(executable),
            "start-all",
            "--dagu-home",
            str(service_directory / "data"),
            "--host",
            SERVICE_BIND_HOST,
            "--port",
            str(THIRD_PARTY_WEB_PORT),
            "--coordinator.host",
            SERVICE_BIND_HOST,
        ],
        environment,
        service_directory,
    )


def stop() -> None:
    stop_background("dagu", _target().parent)


def status() -> bool:
    return status_background("dagu", _target().parent)
