"""Command line entry point."""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import signal
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import DEFAULT_MODULES_DIR, DEFAULT_WEB_ROOT, __version__
from .config import ConfigStore
from .http import HttpClient
from .httpserver import create_server
from .logbus import BusLogHandler, LogBus
from .registry import discover
from .service import Service

LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gpta", description=__doc__)
    parser.add_argument("--host", help="listen address (default from config)")
    parser.add_argument("--port", type=int, help="listen port (default from config)")
    parser.add_argument("--data-dir", help="data directory (default: ./data)")
    parser.add_argument("--modules-dir", help="module directory override")
    parser.add_argument("--web-root", help="web asset directory override")
    parser.add_argument(
        "--no-scheduler", action="store_true", help="serve the UI without scheduling"
    )
    parser.add_argument("--version", action="version", version=f"gpta {__version__}")
    return parser


def _configure_logging(data_dir: Path) -> None:
    root = logging.getLogger()
    if getattr(root, "_gpta_configured", False):
        return
    root.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    stream = logging.StreamHandler()
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    data_dir = Path(
        args.data_dir or os.environ.get("GPTA_DATA_DIR") or (Path.cwd() / "data")
    ).expanduser().resolve()
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
    modules = discover(modules_dir, errors=registry_errors)
    for name, message in sorted(registry_errors.items()):
        log.error("module %s quarantined: %s", name, message)

    config = ConfigStore(data_dir / "config.json", modules)
    config.load()
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
