import os
from pathlib import Path


def _home() -> Path:
    # FUNMILL_HOME cannot come from the config file: it decides where that file
    # lives, so it stays an environment variable only.
    return Path(os.getenv("FUNMILL_HOME", Path.home() / ".farfarfun" / "funmill"))


def config_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    override = os.getenv("FUNMILL_CONFIG")
    if override:
        return Path(override).expanduser()
    return _home() / "api" / "api.env"


def load(explicit: str | None = None) -> Path | None:
    path = config_path(explicit)
    if not path.is_file():
        if explicit:
            raise RuntimeError(f"config file not found: {path}")
        return None

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ[key] = value
    return path
