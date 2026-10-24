"""Command line entry point."""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import signal
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import DEFAULT_MODULES_DIR, DEFAULT_WEB_ROOT, __version__
from .config import ConfigError, ConfigStore, read_server_settings
from .http import HttpClient
from .httpserver import create_server
from .logbus import BusLogHandler, LogBus
from .registry import RegistryError, discover
from .service import Service

LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 3
# EX_CONFIG from sysexits.h. The systemd unit does not restart on this code,
# so a broken config.json fails once with a clear message instead of looping.
EXIT_CONFIG = 78


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gpta", description=__doc__)
    parser.add_argument(
        "--host", help="listen address (overrides server.host in config.json)"
    )
    parser.add_argument(
        "--port", type=int, help="listen port (overrides server.port in config.json)"
    )
    parser.add_argument("--data-dir", help="data directory (default: ./data)")
    parser.add_argument("--modules-dir", help="module directory override")
    parser.add_argument("--web-root", help="web asset directory override")
    parser.add_argument(
        "--no-scheduler", action="store_true", help="serve the UI without scheduling"
    )
    parser.add_argument(
        "--print-url",
        action="store_true",
        help="print the local URL the server will answer on, then exit",
    )
    parser.add_argument("--version", action="version", version=f"gpta {__version__}")
    return parser


class ModuleFormatter(logging.Formatter):
    """Prefix messages logged for a module with ``[module_id]``.

    The module id travels as a record attribute so the console log ring can
    show it as a separate field; only text sinks need the prefix.
    """

    def formatMessage(self, record: logging.LogRecord) -> str:
        module = getattr(record, "gpta_module", None)
        if module:
            record.message = f"[{module}] {record.message}"
        return super().formatMessage(record)


def _configure_logging(data_dir: Path) -> None:
    root = logging.getLogger()
    if getattr(root, "_gpta_configured", False):
        return
    root.setLevel(logging.INFO)
    # GPTA's own debug records reach the console log ring (BusLogHandler);
    # the stderr and file sinks below stay at INFO. Per-request access logs
    # stay off so the 2-second UI poll cannot flush the ring.
    logging.getLogger("gpta").setLevel(logging.DEBUG)
    logging.getLogger("gpta.httpserver").setLevel(logging.INFO)
    formatter = ModuleFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    stream = logging.StreamHandler()
    stream.setLevel(logging.INFO)
    stream.setFormatter(formatter)
    root.addHandler(stream)
    # Under systemd stderr already goes to the journal; avoid a second sink.
    if not os.environ.get("JOURNAL_STREAM"):
        file_handler = RotatingFileHandler(
            data_dir / "server.log",
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    root._gpta_configured = True  # type: ignore[attr-defined]


def _is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def local_url(host: str, port: int) -> str:
    """A URL for reaching ``host:port`` from this machine."""
    if host in ("", "0.0.0.0"):
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{port}"


def _print_url(data_dir: Path, args: argparse.Namespace) -> int:
    try:
        server = read_server_settings(data_dir / "config.json")
    except ConfigError as exc:
        print(f"gpta: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    host = args.host or server["host"]
    port = args.port if args.port is not None else server["port"]
    print(local_url(host, port))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    data_dir = (
        Path(args.data_dir or os.environ.get("GPTA_DATA_DIR") or (Path.cwd() / "data"))
        .expanduser()
        .resolve()
    )
    if args.print_url:
        return _print_url(data_dir, args)
    modules_dir = (
        Path(args.modules_dir).expanduser().resolve()
        if args.modules_dir
        else DEFAULT_MODULES_DIR
    )
    web_root = (
        Path(args.web_root).expanduser().resolve()
        if args.web_root
        else DEFAULT_WEB_ROOT
    )
    data_dir.mkdir(parents=True, exist_ok=True)

    _configure_logging(data_dir)
    log = logging.getLogger("gpta")
    logbus = LogBus()
    log.addHandler(BusLogHandler(logbus))

    registry_errors: dict[str, str] = {}
    try:
        modules = discover(modules_dir, errors=registry_errors)
        config = ConfigStore(data_dir / "config.json", modules)
        config.load()
    except (ConfigError, RegistryError) as exc:
        log.error("cannot start: %s", exc)
        return EXIT_CONFIG
    settings = config.snapshot()
    host = args.host or settings["server"]["host"]
    port = args.port if args.port is not None else settings["server"]["port"]

    if not _is_loopback(host):
        log.warning(
            "HTTP API bound to %s; GPTA has no authentication. Put it behind "
            "an authenticating reverse proxy or tunnel such as Cloudflare Access",
            host,
        )

    service = Service(
        config,
        modules,
        logbus,
        http=HttpClient(),
        modules_dir=modules_dir,
        registry_errors=registry_errors,
    )
    service.bound_host = host
    service.bound_port = port
    server = create_server(host, port, service, web_root)

    stop_event = threading.Event()

    def shutdown(signum, _frame):
        if stop_event.is_set():
            return
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    if not args.no_scheduler:
        service.start()

    print(
        f"GPTA {__version__} listening on http://{host}:{port} "
        f"with modules: {', '.join(sorted(modules)) or '(none)'}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - signal path normally handles it
        pass
    finally:
        service.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
