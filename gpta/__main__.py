"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from . import DEFAULT_MODULES_DIR, DEFAULT_WEB_ROOT, __version__
from .config import ConfigStore
from .http import HttpClient
from .httpserver import create_server
from .logbus import BusLogHandler, LogBus
from .registry import discover
from .service import Service


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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

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

    logbus = LogBus()
    gpta_logger = logging.getLogger("gpta")
    gpta_logger.setLevel(logging.INFO)
    gpta_logger.addHandler(BusLogHandler(logbus))

    modules = discover(modules_dir)
    config = ConfigStore(data_dir / "config.json", modules)
    config.load()
    settings = config.snapshot()
    host = args.host or settings["server"]["host"]
    port = args.port if args.port is not None else settings["server"]["port"]

    service = Service(
        config,
        modules,
        logbus,
        http=HttpClient(),
        modules_dir=modules_dir,
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
        f"with modules: {', '.join(sorted(modules))}",
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
