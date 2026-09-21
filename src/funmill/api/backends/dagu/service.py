import hashlib
import os
import platform
import shutil
import tarfile
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

from funmill.api.ports import SERVICE_BIND_HOST, THIRD_PARTY_WEB_PORT

from ..service import start_background, status_background, stop_background

VERSION = "v2.16.3"
_RELEASE = f"https://github.com/dagucloud/dagu/releases/download/{VERSION}"
_BUILDS = {
    ("darwin", "x86_64"): (
        "darwin_amd64",
        "09bd9122f9db02bc4247cb23d0a8a8a033482c381acfc701dc6cb248849e4d36",
        "b99d0bb23e33c6f4a5ad22094d40acdbbc209ec3d801c99bf82fe072fd336306",
    ),
    ("darwin", "arm64"): (
        "darwin_arm64",
        "191ed4edc217680eae24ce348c29d6d4e2f10a6940f0281a7d1f9ca15c451555",
        "ad9f1bdff3c1813f7caf39bc8cef72fa80435111f3980ecefc78de0a8d6b4918",
    ),
    ("linux", "x86_64"): (
        "linux_amd64",
        "2237cdd6287db00857af3471bcad7cea0bf786055bbd24f1bc53a02ceb9d4b06",
        "fea2f1330644da0e54be983d34c1819f6158001eac23485ea9a476f26a8aba44",
    ),
    ("linux", "arm64"): (
        "linux_arm64",
        "e1f5fd611b003ee73e4846bad24455e02e052ff60314723e44aa067522c71762",
        "6c0068970f6fcf678601566fb1e165aa4fb8f785d27b86677d227aaa32b1d0e3",
    ),
}
_MACHINES = {"amd64": "x86_64", "aarch64": "arm64"}


def _home() -> Path:
    return Path(os.getenv("FUNMILL_HOME", Path.home() / ".farfarfun" / "funmill"))


def _target() -> Path:
    return _home() / "services" / "dagu" / "dagu"


def _sha256(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def _build() -> tuple[str, str, str]:
    key = (
        platform.system().lower(),
        _MACHINES.get(platform.machine().lower(), platform.machine().lower()),
    )
    try:
        return _BUILDS[key]
    except KeyError as exc:
        raise RuntimeError(
            "Dagu installer only supports macOS/Linux amd64/arm64"
        ) from exc


def install(force: bool = False) -> Path:
    suffix, archive_sha256, binary_sha256 = _build()
    target = _target()
    if target.exists() and _sha256(target) == binary_sha256:
        return target
    if target.exists() and not force:
        raise RuntimeError(
            f"{target} exists but has an unexpected checksum; use --force"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    archive_name = f"dagu_{VERSION.removeprefix('v')}_{suffix}.tar.gz"
    with tempfile.TemporaryDirectory(dir=target.parent) as temporary_dir:
        archive_path = Path(temporary_dir) / archive_name
        request = Request(
            f"{_RELEASE}/{archive_name}", headers={"User-Agent": "funmill"}
        )
        print(f"downloading Dagu {VERSION}...", flush=True)
        with urlopen(request, timeout=30) as response, archive_path.open("wb") as file:
            shutil.copyfileobj(response, file)
        if _sha256(archive_path) != archive_sha256:
            raise RuntimeError("downloaded Dagu archive failed SHA-256 verification")

        executable = Path(temporary_dir) / "dagu"
        with tarfile.open(archive_path, "r:gz") as archive:
            member = archive.getmember("dagu")
            if not member.isfile():
                raise RuntimeError("downloaded Dagu archive does not contain a binary")
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError("downloaded Dagu archive does not contain a binary")
            with source, executable.open("wb") as file:
                shutil.copyfileobj(source, file)
        if _sha256(executable) != binary_sha256:
            raise RuntimeError("downloaded Dagu binary failed SHA-256 verification")
        executable.chmod(0o755)
        executable.replace(target)
    return target


def start() -> None:
    executable = _target()
    if not executable.exists():
        system_executable = shutil.which("dagu")
        if system_executable is None:
            raise RuntimeError("Dagu is not installed; run: funmill install dagu")
        executable = Path(system_executable)

    environment = os.environ.copy()
    environment.setdefault("DAGU_AUTH_MODE", "none")
    service_directory = _target().parent
    environment["DAGU_HOME"] = str(service_directory / "data")
    environment.setdefault("DAGU_COORDINATOR_ENABLED", "false")
    environment.setdefault(
        "FUNMILL_DAGU_URL", f"http://127.0.0.1:{THIRD_PARTY_WEB_PORT}/api/v1"
    )
    if environment.get("DAGU_TOKEN"):
        environment.setdefault("FUNMILL_DAGU_TOKEN", environment["DAGU_TOKEN"])
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
