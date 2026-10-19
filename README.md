# GPTA

GPTA is a local web console and background server that gathers data from
pluggable modules, renders receipt-width text, and optionally prints it to a
thermal printer. The browser console controls modules, limits, debug output,
and a printer simulator that shows exactly what the physical printer would
receive.

The runtime uses **only the Python standard library**. Nix provides the
development environment and an optional packaged executable.

## Architecture

- `gpta/` - HTTP server, scheduler, config, renderer, ESC/POS transport.
- `modules/<id>/module.py` - one module per folder, discovered at startup.
- `web/` - hand-written HTML/CSS/JS console (no CDN, no build step).
- `data/config.json` - atomically written runtime configuration.
- `data/server.log`, `data/server.out`, `data/server.pid` - rotated log,
  early-start stderr, and manual-start pid.
- `docs/` - [module contract](docs/module-authoring.md),
  [safety model](docs/safety.md), and [decisions](docs/decisions.md).

One process runs a threaded HTTP server and a scheduler thread. Scheduled
module runs fetch data on a bounded worker, render a receipt, store the latest
output, and print if the printer is enabled. Runs are asynchronous and use a
monotonic clock; a hung or slow module cannot stall the scheduler. The UI polls
`/api/state` and `/api/logs?since=N` every two seconds.

## Quick start

With Nix:

```bash
nix develop
python -m gpta --port 8787
```

Without Nix, using the system Python 3.11+:

```bash
python3 -m gpta --port 8787
```

Open <http://127.0.0.1:8787>. Data is written under `./data` by default; set
`GPTA_DATA_DIR` to override.

Scripts:

```bash
./scripts/start.sh
./scripts/restart.sh
./scripts/stop.sh
sudo ./scripts/install-systemd.sh
```

`start.sh` uses the system unit when it is registered; otherwise it launches a
background process and writes `data/server.pid`. `install-systemd.sh` registers
the service system-wide at `/etc/systemd/system/gpta.service`, enables it in
`multi-user.target`, and escalates with `sudo` when needed. Use
`./scripts/install-systemd.sh --dry-run` to print the unit without changing the
system.

The unit runs as `$SUDO_USER` when installed through `sudo`, otherwise as
`${GPTA_RUN_USER:-$USER}`. It adds `SupplementaryGroups=lp` when that group
exists so the service can open `/dev/usb/lp0`. Override the run user with
`GPTA_RUN_USER`, the unit name with `GPTA_UNIT_NAME`, and the executable with
`GPTA_EXEC` (for example a Nix-built wrapper).

## Modules

All modules start disabled. Enable them in the UI or via the API. Limits:

| Module | Default interval | Limit | Options |
|---|---|---|---|
| `affirmations` | 86400s | 1 item, 500 chars | include date |
| `hackernews` | 604800s | 5 items, 1500 chars | window days, min score, scan limit |
| `gutenberg` | 86400s | 1 item, 1800 chars | book id, language, cache |
| `google_tasks` | 3600s | 15 items, 2000 chars | credentials file, list id, completed |

- **Daily Affirmation** selects a deterministic affirmation from
  `modules/affirmations/data/affirmations.txt`; edit that file to customize.
- **Hacker News Weekly** scans top stories, keeps those from the configured
  window, sorts by score, and prints the top `max_items`.
- **Project Gutenberg** picks a random plain-text book from Project Gutenberg's OPDS feed,
  caching downloaded text under `data/cache/gutenberg/`. Set `book_id` for a
  fixed book.
- **Google Tasks** refreshes an OAuth access token and prints pending tasks.

### Google Tasks setup

1. In Google Cloud, enable the **Tasks API**.
2. Create an OAuth client of type **Desktop app**.
3. Run the one-time helper:

```bash
GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... python3 scripts/google_oauth.py
```

It writes `data/google_credentials.json` with mode `0600`. The module reads
that file; credentials are never returned by the HTTP API.

## Printer

The printer transport writes ESC/POS bytes to a character device. The default
is `/dev/usb/lp0`, width 32 columns, cp437 encoding, with a cut command. Change
the device, width (32/48), auto-print, and enabled state in the console.

The simulator shows the exact ASCII receipt rendered for the printer, both per
module and as a total of enabled modules. When the printer is disabled, runs
still render and update the simulator but never touch the device. **Test
print** writes a short page and reports any device error.

Writes are serialized on a single writer thread and bounded by
`printer.write_timeout_seconds`. A global rate limiter enforces
`printer.min_print_spacing_seconds` (default 30) and
`printer.max_prints_per_hour` (default 60); throttled auto-prints are skipped,
while a throttled manual test returns `429`. After
`printer.failure_threshold` consecutive device failures (default 3) the printer
is disabled automatically and the reason is shown in the console. Enabling the
printer again clears the failure count.

The device must be a normalised path under `/dev/` and a character device; the
transport resolves symlinks and refuses to write anywhere else.

Printer access usually requires membership in the `lp` group, or the system
unit's `SupplementaryGroups=lp`.

## Safety and security

The server provides reliability guards, not a sandbox: modules run in-process
and are trusted code. The host enforces a fetch timeout (default 120s), one
active run per module, truncated receipts and HTTP bodies, bounded cache/logs,
failure backoff, and printer serialization. See
[docs/safety.md](docs/safety.md) for the full failure matrix and known limits.

The console validates the `Host` header against the configured host and
localhost/IP literals (DNS-rebinding protection) and rejects state-changing
requests without `Content-Type: application/json` (CSRF protection). It binds
to `127.0.0.1` by default; if you bind elsewhere, set `GPTA_AUTH_TOKEN` (or
`server.auth_token`) to require `Authorization: Bearer <token>` on `/api/*`.
Do not expose the console to untrusted networks.

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/state` | Server, modules, runtime, outputs, registry errors |
| GET | `/api/logs?since=N` | Incremental log entries |
| GET | `/api/output/<id>` or `/api/output/total` | Plain-text receipt |
| PATCH | `/api/modules/<id>` | `enabled`, `interval_seconds`, `limits`, `options` |
| POST | `/api/modules/<id>/run` | Run now (also prints when enabled) |
| PATCH | `/api/printer` | Printer config |
| POST | `/api/printer/test` | Test page |

JSON request bodies require `Content-Type: application/json`. Unknown modules
and outputs return 404, invalid configuration returns 400, a concurrent
manual run returns 409, a rate-limited test print returns 429, and a missing
token returns 401. `/api/state` no longer embeds logs; fetch them
incrementally with `/api/logs?since=N`. It reports
`server.last_tick_age_seconds`/`scheduler_stale`, per-module
`consecutive_failures`/`effective_interval_seconds`, printer
`circuit_open`/`disabled_reason`, and quarantined modules in
`registry_errors`.

## Adding a module

Read [docs/module-authoring.md](docs/module-authoring.md) first; it is the
contract for timeouts, output bounds, logging, caching, and the trust boundary.

1. Create `modules/<id>/module.py` with exactly one `Module` subclass whose
   `id` equals the folder name.
2. Define `name`, `description`, `defaults` (`enabled`, `interval_seconds`,
   `limits`, `options`), optional `options_schema`, and `fetch(ctx)`.
3. Return `Block` objects. Use `ctx.http` for network access, `ctx.cache` for
   downloads, and `ctx.emit()` for bounded debug logs.
4. Add a hermetic test in `tests/test_modules.py` named `test_<id>...`.
   `tests/test_module_coverage.py` enforces this.
5. Restart the server. The registry scans folders at startup; there is no hot
   reload. A broken module is quarantined and reported in `/api/state`, and the
   remaining modules still start.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The suite uses fake HTTP transports and does not require network access or a
printer.
