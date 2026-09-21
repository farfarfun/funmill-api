import argparse
import importlib
from collections.abc import Sequence
from types import ModuleType

import uvicorn

from funmill.api.backends import BACKEND_SPECS
from funmill.api.ports import FUNMILL_API_PORT, SERVICE_BIND_HOST


def _service_names() -> list[str]:
    return [name for name, spec in BACKEND_SPECS.items() if spec.service]


def _service(name: str) -> ModuleType:
    spec = BACKEND_SPECS[name]
    return importlib.import_module(spec.service, "funmill.api.backends")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="funmill")
    commands = parser.add_subparsers(dest="command")

    commands.add_parser("services", help="列出可安装的第三方服务")

    install = commands.add_parser("install", help="安装第三方服务")
    install.add_argument("service", choices=_service_names())
    install.add_argument("--force", action="store_true", help="覆盖现有安装")

    start = commands.add_parser("start", help="启动 Funmill 或第三方服务")
    start.add_argument(
        "service", nargs="?", choices=["api", *_service_names()], default="api"
    )

    for command in ("stop", "status", "restart"):
        service_command = commands.add_parser(
            command,
            help={
                "stop": "停止第三方服务",
                "status": "查看第三方服务状态",
                "restart": "重启第三方服务",
            }[command],
        )
        service_command.add_argument("service", choices=_service_names())
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "services":
            print("\n".join(_service_names()))
        elif args.command == "install":
            path = _service(args.service).install(force=args.force)
            print(f"installed {args.service}: {path}")
        elif args.command == "start" and args.service != "api":
            _service(args.service).start()
        elif args.command == "stop":
            _service(args.service).stop()
        elif args.command == "status":
            if not _service(args.service).status():
                raise SystemExit(1)
        elif args.command == "restart":
            service = _service(args.service)
            service.stop()
            service.start()
        elif args.command == "start":
            uvicorn.run(
                "funmill.api:app",
                host=SERVICE_BIND_HOST,
                port=FUNMILL_API_PORT,
            )
        else:
            parser.print_help()
    except (OSError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"error: {exc}\n")


if __name__ == "__main__":
    main()
