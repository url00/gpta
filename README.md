# GPTA

GPTA is a local web console and background server that gathers data from
pluggable modules, renders receipt-width text, and optionally prints it on a
thermal printer. The console controls modules and the printer, shows debug
logs, and has a simulator that shows exactly what the printer would receive.

The runtime uses **only the Python standard library** (3.11+). Nix provides
the development shell and an optional packaged executable.

## Quick start

```bash
python3 -m gpta          # or: nix develop, then python -m gpta
```

Open <http://127.0.0.1:8787>. All modules start disabled; enable them in the
console. Data lives in `./data` (set `GPTA_DATA_DIR` or `--data-dir` to move
it).

## Configuration

`data/config.json` is created on first start and rewritten when you save
settings in the console. Settings it doesn't recognise, or values that are no
longer valid, are logged and replaced by defaults; only a file that isn't
valid JSON stops startup (exit status 78).

| Setting | Default | Notes |
|---|---|---|
| `server.host`, `server.port` | `127.0.0.1`, `8787` | `--host`/`--port` override for one run |
| `server.allowed_hosts` | `localhost`, `127.0.0.1`, `::1` | Hostnames the server answers to; add a tunnel's public hostname here |
| `server.run_timeout_seconds` | `120` | Per-run fetch timeout |
| `printer.*` | disabled, `/dev/usb/lp0`, 32 columns | Device, width, auto-print, rate limits; see [Printer](#printer) |
| `modules.<id>.*` | per module | `enabled`, `interval_seconds`, `limits`, `options` |

For example, behind a Cloudflare tunnel:

```json
"server": {"allowed_hosts": ["localhost", "127.0.0.1", "::1", "gpta.example.com"]}
```

## Running as a service

```bash
./scripts/start.sh        # systemd unit if installed, else a background process
./scripts/restart.sh
./scripts/stop.sh
./scripts/install-systemd.sh [--dry-run]
```

The scripts find the server's address with `gpta --print-url`, which reads the
same config. `install-systemd.sh` installs `/etc/systemd/system/gpta.service`
(escalating with `sudo`), enables it, and restarts it when the unit changes;
`--dry-run` prints the unit only. The unit runs as `$SUDO_USER` (or
`GPTA_RUN_USER`) and joins the `lp` group when it exists so it can open the
printer. `GPTA_UNIT_NAME` and `GPTA_EXEC` (e.g. a Nix-built wrapper) override
the unit name and command.

Logs: `data/server.log` (rotated) for manual starts, the journal under systemd.

## Modules

| Module | Interval | Limits | Options |
|---|---|---|---|
| `affirmations` | 1 day | 500 chars | include date |
| `hackernews` | 7 days | 5 items, 1500 chars | window days, min score, scan limit |
| `gutenberg` | 1 day | 1800 chars | book id, language, cache |
| `google_tasks` | 1 hour | 15 items, 2000 chars | credentials file, list id, completed |

- **Daily Affirmation** picks one line per day from
  `modules/affirmations/data/affirmations.txt`; edit that file to customise.
- **Hacker News Weekly** scans the current top stories, keeps those inside the
  window, and prints the highest-scoring `max_items`.
- **Project Gutenberg** prints the opening of a random plain-text book (or a
  fixed `book_id`), caching downloads under `data/cache/gutenberg/`.
- **Google Tasks** prints pending tasks from one list. One-time setup: enable
  the Tasks API in Google Cloud, create a **Desktop app** OAuth client, then
  run `GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... python3 scripts/google_oauth.py`.
  It writes `data/google_credentials.json` (mode 0600), which the HTTP API
  never exposes.

To write a module, see [docs/module-authoring.md](docs/module-authoring.md).

## Printer

GPTA writes ESC/POS to a character device under `/dev/` (default
`/dev/usb/lp0`, usually needing the `lp` group). Text is reduced to printable
ASCII. With the printer disabled, runs still update the simulator but never
touch the device.

Writes are serialized and time-limited. Prints are spaced at least
`printer.min_print_spacing_seconds` apart (default 30) and capped at
`printer.max_prints_per_hour` (default 60): throttled auto-prints are skipped
and logged; a throttled test print returns 429. After
`printer.failure_threshold` consecutive device failures (default 3) the printer
disables itself; enabling it again clears the count.

## Security

**GPTA has no authentication.** Anywhere beyond loopback it must sit behind an
authenticating layer such as a Cloudflare Tunnel with Access, an
authenticating reverse proxy, or a VPN.

It answers only requests whose `Host` is in `server.allowed_hosts` (blocks DNS
rebinding), and every `POST`/`PATCH` needs `Content-Type: application/json`
and, from a browser, an allowed `Origin` (blocks cross-site requests).
Rejections return 403 and are logged. Modules are trusted code running
in-process. See [docs/safety.md](docs/safety.md) for failure handling and known
limits.

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/state` | Server, printer, modules (config, runtime, latest output), total receipt, quarantined modules |
| GET | `/api/logs?since=N` | Log entries after sequence `N`, plus `next` |
| GET | `/api/output/<id>`, `/api/output/total` | Plain-text receipt |
| PATCH | `/api/modules/<id>` | Update `enabled`, `interval_seconds`, `limits`, `options` |
| POST | `/api/modules/<id>/run` | Run now (prints if auto-print is on) |
| PATCH | `/api/printer` | Update printer settings |
| POST | `/api/printer/test` | Print a test page |

Writes need a JSON body (`{}` or empty is fine). Errors: 400 invalid request or
settings, 403 disallowed host/origin, 404 unknown module or path, 405 wrong
method, 409 module already running or printer disabled, 429 test print rate
limited.

## Development

```bash
make test       # python3 -m unittest discover -s tests -v
make lint       # ruff (in the Nix dev shell)
make format
nix build       # packaged executable at ./result/bin/gpta
```

Tests use fake HTTP clients and need no network or printer.

| Path | Contents |
|---|---|
| `gpta/service.py` | Facade used by the HTTP layer; builds `/api/state` |
| `gpta/runner.py`, `gpta/scheduler.py` | Running modules; deciding when |
| `gpta/printing/` | Print policy (`printer.py`) and device I/O (`transport.py`) |
| `gpta/config.py`, `gpta/registry.py` | Settings; module discovery and validation |
| `gpta/httpserver.py`, `gpta/http.py` | Console/API server; outbound HTTP client for modules |
| `gpta/module_api.py`, `gpta/renderer.py` | Module interface; receipt layout |
| `modules/<id>/` | One module per folder |
| `web/` | Console (plain HTML/CSS/JS, no build step) |
| `docs/` | [Module authoring](docs/module-authoring.md), [safety](docs/safety.md), [decisions](docs/decisions.md) |
