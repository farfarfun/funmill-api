import os
import signal
import subprocess
import time
from pathlib import Path


def _pid_path(name: str, directory: Path) -> Path:
    return directory / f"{name}.pid"


def _read_pid(path: Path) -> int:
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
        if pid <= 1:
            raise ValueError
        return pid
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"invalid service PID file: {path}") from exc


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def start_background(
    name: str, command: list[str], environment: dict[str, str], directory: Path
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    pid_path = _pid_path(name, directory)
    if pid_path.exists():
        pid = _read_pid(pid_path)
        if _is_running(pid):
            raise RuntimeError(f"{name} is already running with pid {pid}")
        pid_path.unlink()

    log_path = directory / f"{name}.log"
    with log_path.open("ab") as output:
        process = subprocess.Popen(
            command,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    pid_path.write_text(f"{process.pid}\n", encoding="utf-8")
    try:
        return_code = process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        print(f"started {name}: pid={process.pid}, log={log_path}")
        return
    pid_path.unlink(missing_ok=True)
    raise RuntimeError(
        f"{name} exited during startup with code {return_code}; see {log_path}"
    )


def status_background(name: str, directory: Path) -> bool:
    pid_path = _pid_path(name, directory)
    if not pid_path.exists():
        print(f"{name} is stopped")
        return False
    pid = _read_pid(pid_path)
    if not _is_running(pid):
        pid_path.unlink()
        print(f"{name} is stopped")
        return False
    print(f"{name} is running: pid={pid}")
    return True


def stop_background(name: str, directory: Path, timeout: float = 10) -> None:
    pid_path = _pid_path(name, directory)
    if not pid_path.exists():
        print(f"{name} is already stopped")
        return
    pid = _read_pid(pid_path)
    if not _is_running(pid):
        pid_path.unlink()
        print(f"{name} is already stopped")
        return
    try:
        group_id = os.getpgid(pid)
    except ProcessLookupError:
        pid_path.unlink()
        print(f"{name} is already stopped")
        return
    if group_id != pid:
        raise RuntimeError(
            f"refusing to stop {name}: pid {pid} is not its process group"
        )

    os.killpg(group_id, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while _is_running(pid):
        if time.monotonic() >= deadline:
            raise RuntimeError(f"timed out waiting for {name} pid {pid} to stop")
        time.sleep(0.1)
    pid_path.unlink(missing_ok=True)
    print(f"stopped {name}: pid={pid}")
