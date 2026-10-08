from __future__ import annotations

import importlib
import shutil
import subprocess
from collections.abc import Sequence
from types import ModuleType
from typing import Annotated

import typer

from funmill.api import config as api_config
from funmill.api import service as api_service
from funmill.api.backends import BACKEND_SPECS

PACKAGE_NAME = "funmill-api"

app = typer.Typer(help="Funmill command-line tools", no_args_is_help=True)
server_app = typer.Typer(help="Managed service lifecycle", no_args_is_help=True)
app.add_typer(server_app, name="server")


def _service_names() -> list[str]:
    return [name for name, spec in BACKEND_SPECS.items() if spec.service]


def _service(name: str) -> ModuleType:
    try:
        spec = BACKEND_SPECS[name]
    except KeyError as exc:
        raise ValueError(f"unknown service: {name}") from exc
    return importlib.import_module(spec.service, "funmill.api.backends")


def _resolve_service(name: str) -> ModuleType:
    return api_service if name == "api" else _service(name)


def _manage(action: str, service: str) -> None:
    target = _resolve_service(service)
    if action == "restart":
        target.stop()
        target.start()
    elif action == "status":
        if not target.status():
            raise SystemExit(1)
    else:
        getattr(target, action)()


def _run_uv_tool(arguments: list[str]) -> None:
    executable = shutil.which("uv")
    if executable is None:
        raise RuntimeError("uv is required for package lifecycle commands")
    result = subprocess.run([executable, "tool", *arguments], check=False)
    if result.returncode:
        raise RuntimeError(f"uv tool failed with exit code {result.returncode}")


@app.callback()
def configure(
    config: Annotated[
        str | None,
        typer.Option(
            "--config",
            help="Config file path (default: ~/.farfarfun/funmill/api/api.env)",
        ),
    ] = None,
) -> None:
    loaded = api_config.load(config)
    if loaded:
        print(f"loaded config: {loaded}")


@app.command("services")
def services() -> None:
    """List installable third-party services."""
    print("\n".join(_service_names()))


@app.command("install")
def install(
    service: Annotated[str, typer.Argument(help="Third-party service name")],
    force: Annotated[
        bool, typer.Option("--force", help="Replace an existing install")
    ] = False,
) -> None:
    """Install a third-party service."""
    path = _service(service).install(force=force)
    print(f"installed {service}: {path}")


@server_app.command("run")
def server_run() -> None:
    """Run the Funmill API in the foreground."""
    api_service.run()


@server_app.command("start")
def server_start(
    service: Annotated[str, typer.Argument(help="api, dagu, or windmill")] = "api",
) -> None:
    """Start a managed service in the background."""
    _manage("start", service)


@server_app.command("stop")
def server_stop(
    service: Annotated[str, typer.Argument(help="api, dagu, or windmill")] = "api",
) -> None:
    """Stop a managed service."""
    _manage("stop", service)


@server_app.command("restart")
def server_restart(
    service: Annotated[str, typer.Argument(help="api, dagu, or windmill")] = "api",
) -> None:
    """Restart a managed service."""
    _manage("restart", service)


@server_app.command("status")
def server_status(
    service: Annotated[str, typer.Argument(help="api, dagu, or windmill")] = "api",
) -> None:
    """Show managed service status."""
    _manage("status", service)


@app.command("upgrade")
def upgrade(
    version: Annotated[
        str | None, typer.Argument(help="Version to install; latest when omitted")
    ] = None,
) -> None:
    """Upgrade Funmill to the latest or requested version."""
    target = f"{PACKAGE_NAME}=={version}" if version else PACKAGE_NAME
    _run_uv_tool(["install", "--upgrade", target])


@app.command("rollback")
def rollback(
    version: Annotated[str, typer.Argument(help="Version to restore")],
) -> None:
    """Install a specific previous version."""
    _run_uv_tool(["install", "--force", f"{PACKAGE_NAME}=={version}"])


@app.command("uninstall")
def uninstall() -> None:
    """Stop the API then uninstall Funmill."""
    _manage("stop", "api")
    _run_uv_tool(["uninstall", PACKAGE_NAME])


# Keep existing operator commands working while documentation and setup use server.
@app.command("run", hidden=True)
def legacy_run() -> None:
    server_run()


@app.command("start", hidden=True)
def legacy_start(service: str = "api") -> None:
    _manage("start", service)


@app.command("stop", hidden=True)
def legacy_stop(service: str = "api") -> None:
    _manage("stop", service)


@app.command("restart", hidden=True)
def legacy_restart(service: str = "api") -> None:
    _manage("restart", service)


@app.command("status", hidden=True)
def legacy_status(service: str = "api") -> None:
    _manage("status", service)


def main(argv: Sequence[str] | None = None) -> None:
    try:
        app(
            args=list(argv) if argv is not None else None,
            standalone_mode=argv is None,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


if __name__ == "__main__":
    main()
