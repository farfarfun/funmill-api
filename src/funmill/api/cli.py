import argparse
import importlib
from collections.abc import Sequence
from types import ModuleType

from funmill.api import config as api_config
from funmill.api import service as api_service
from funmill.api.backends import BACKEND_SPECS


def _service_names() -> list[str]:
    return [name for name, spec in BACKEND_SPECS.items() if spec.service]


def _service(name: str) -> ModuleType:
    spec = BACKEND_SPECS[name]
    return importlib.import_module(spec.service, "funmill.api.backends")


def _resolve_service(name: str) -> ModuleType:
    if name == "api":
        return api_service
    return _service(name)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="funmill")
    parser.add_argument(
        "--config",
        help="配置文件路径,默认 ~/.farfarfun/funmill/api/api.env,也可用 FUNMILL_CONFIG",
    )
    commands = parser.add_subparsers(dest="command")

    commands.add_parser("services", help="列出可安装的第三方服务")

    install = commands.add_parser("install", help="安装第三方服务")
    install.add_argument("service", choices=_service_names())
    install.add_argument("--force", action="store_true", help="覆盖现有安装")

    start = commands.add_parser("start", help="后台启动 Funmill 或第三方服务")
    start.add_argument(
        "service", nargs="?", choices=["api", *_service_names()], default="api"
    )

    commands.add_parser("run", help="在前台启动 Funmill API")

    for command in ("stop", "status", "restart"):
        service_command = commands.add_parser(
            command,
            help={
                "stop": "停止 Funmill 或第三方服务",
                "status": "查看 Funmill 或第三方服务状态",
                "restart": "重启 Funmill 或第三方服务",
            }[command],
        )
        service_command.add_argument(
            "service", nargs="?", choices=["api", *_service_names()], default="api"
        )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = _parser()
    args = parser.parse_args(argv)

    try:
        loaded = api_config.load(args.config)
        if loaded:
            print(f"loaded config: {loaded}")

        if args.command == "services":
            print("\n".join(_service_names()))
        elif args.command == "install":
            path = _service(args.service).install(force=args.force)
            print(f"installed {args.service}: {path}")
        elif args.command == "run":
            api_service.run()
        elif args.command == "start":
            _resolve_service(args.service).start()
        elif args.command == "stop":
            _resolve_service(args.service).stop()
        elif args.command == "status":
            if not _resolve_service(args.service).status():
                raise SystemExit(1)
        elif args.command == "restart":
            service = _resolve_service(args.service)
            service.stop()
            service.start()
        else:
            parser.print_help()
    except (OSError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"error: {exc}\n")


if __name__ == "__main__":
    main()
