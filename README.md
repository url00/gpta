# GPTA

GPTA is a local web console and background server that gathers data from
pluggable modules, renders each receipt as an image the width of the paper,
and optionally prints it on a thermal printer. The console controls modules
and the printer, shows debug logs, and previews the exact image the printer
would receive.

The runtime is Python 3.11+ with one dependency, **Pillow**, which draws the
receipts. Nix provides it: the development shell, the scripts, and the
packaged executable all use the flake's `python3.withPackages (ps: [ps.pillow])`.
The receipt font (DejaVu Sans Mono, in `gpta/fonts/`) ships with the code.

## Quick start

```bash
nix develop -c python -m gpta     # or any python3 that has Pillow: python3 -m gpta
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
| `printer.*` | disabled, `/dev/usb/lp0`, 80 mm paper | CUPS queue or device, paper size, auto-print, rate limits; see [Printer](#printer) |
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

The scripts run this checkout with the flake's Python (`nix build .#python`,
linked at `.nix-python` so garbage collection keeps it), or, without Nix, with
any `python3` that can import Pillow. `GPTA_PYTHON` picks another interpreter;
`GPTA_EXEC` replaces the whole command (e.g. the packaged `result/bin/gpta`).
They find the server's address with `gpta --print-url`, which reads the
same config. `install-systemd.sh` installs `/etc/systemd/system/gpta.service`
(escalating with `sudo`), enables it, and restarts it when the unit changes;
`--dry-run` prints the unit only. The unit runs as `$SUDO_USER` (or
`GPTA_RUN_USER`) and joins the `lp` group when it exists so it can open the
printer. Its command is `<checkout>/.nix-python/bin/python3 -m gpta` unless
`GPTA_PYTHON` or `GPTA_EXEC` says otherwise; `GPTA_UNIT_NAME` overrides the
unit name.

Logs: `data/server.log` (rotated) for manual starts, the journal under systemd.

Restarts are safe: `data/state.json` records when each module last ran, how
it went, and its latest receipt (as blocks, re-rendered on load), so a restart
only runs modules that are due (for example, one whose daily run was missed
while the server was down runs once). Receipts saved as text by older versions
show as plain text until the module next runs. Delete the file to make every
enabled module run on the next start.

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

Each receipt is rendered as a 1-bit image as wide as the paper
(`printer.paper`: `80mm`, 576 dots, the default; or `58mm`, 384 dots, both at
203 dpi) and sent as ESC/POS raster graphics (`GS v 0`, in bands of 256
rows). The console preview is the same image. GPTA sends it in one of two ways:

- **CUPS queue** (`printer.cups_queue`, e.g. `POS-80`). Use this when CUPS
  owns the USB printer and there is no `/dev/usb/lp0`. The queue must be a
  raw queue (CUPS model "Local Raw Printer") so the bytes reach the printer
  unchanged; GPTA submits with `lp -o raw` and waits for the job to finish.
  `lpstat -v` lists queues.
- **Device node** (`printer.device`, default `/dev/usb/lp0`), used when
  `cups_queue` is empty. Must be a character device under `/dev/`; usually
  needs the `lp` group.

Text keeps its Unicode characters; anything the font has no glyph for prints
as `�`. Each receipt ends with ESC/POS "feed to cutter, then cut", so the last
lines are never cut off; `printer.feed_lines` adds extra blank space before
it. With the printer disabled, runs still update the preview but never print.
Changing the paper size re-renders the existing previews at the new width.
The test page shows the font sizes, a ruler with a tick every millimetre
across the full width (to check the printable edges), and a bar chart.

Writes are serialized and time-limited. Scheduled prints are spaced at least
`printer.min_print_spacing_seconds` apart (default 30); prints you ask for
(**Run now**, **Test print**, **Instant** and web page prints) skip the spacing. All prints count toward
`printer.max_prints_per_hour` (default 60). A skipped print is logged and the
module card shows why ("Not printed: ..."); a test print over the hourly cap
returns 429. After `printer.failure_threshold` consecutive failures (default
3) the printer disables itself; enabling it again clears the count.

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
| GET | `/api/state` | Server, printer (incl. `paper` and its `width_px`), modules (config, runtime, latest output), total receipt, quarantined modules |
| GET | `/api/logs?since=N` | Log entries after sequence `N`, plus `next` |
| GET | `/api/output/<id>.png`, `/api/output/total.png` | The receipt image exactly as it prints (`printer.width_px` wide; 404 before any output) |
| GET | `/api/output/<id>`, `/api/output/total` | The same receipt as plain text, for copying and debugging |
| PATCH | `/api/modules/<id>` | Update `enabled`, `interval_seconds`, `limits`, `options` |
| POST | `/api/modules/<id>/run` | Run now (prints if auto-print is on) |
| PATCH | `/api/printer` | Update printer settings |
| POST | `/api/instant/preview` | Image of `{"text", "size", "layout", "format"}` as an instant print would draw it (`png` data URL), with `chars` and `paper_mm` (paper length) |
| POST | `/api/instant/print` | Print `{"text", "size", "layout", "format"}` once; text up to 150,000 characters (requests up to 1 MB). `format` is `plain` (default) or `markdown`: headings, lists, task lists, quotes, code, tables, rules and **bold**. `size` (here and for web pages) is `tiny`, `small`, `compact`, `body`, `large` or `huge`: 12–36 px body text. `layout` (both too) is `single`, `double` (two columns with a centre rule) or `triple` (three, no rules): columns are split between lines, in bands of at most about 15 cm, with headings, rules and a web page's header kept full width |
| POST | `/api/reader/fetch` | Fetch `{"url", "size", "layout"}` reader-mode style and lay out the whole page. Returns an `id` for printing, a `png` preview, `chars` (characters that will print), `total` (the page's characters; more than `chars` only past the 150,000 safety cap) and `paper_mm` (paper length). Public http(s) sites only |
| POST | `/api/reader/print` | Print `{"id", "size", "layout"}` from a recent fetch, exactly as previewed. The last 8 fetches are kept, until a restart |
| POST | `/api/printer/test` | Print a test page |

Writes need a JSON body (`{}` or empty is fine). Errors: 400 invalid request or
settings, 403 disallowed host/origin, 404 unknown module or path, 405 wrong
method, 409 module already running or printer disabled, 429 test print rate
limited.

## Development

```bash
nix develop -c make test    # python3 -m unittest discover -s tests -v
nix develop -c make lint    # ruff
nix develop -c make format
nix build                   # packaged executable at ./result/bin/gpta
```

Tests use fake HTTP clients and need no network or printer. `nix build` only
sees files Git tracks, so `git add` new files first.

| Path | Contents |
|---|---|
| `gpta/service.py` | Facade used by the HTTP layer; builds `/api/state` |
| `gpta/runner.py`, `gpta/scheduler.py` | Running modules; deciding when |
| `gpta/printing/` | Print policy (`printer.py`), device I/O (`transport.py`), ESC/POS raster encoding (`escpos.py`) |
| `gpta/config.py`, `gpta/registry.py` | Settings; module discovery and validation |
| `gpta/httpserver.py`, `gpta/http.py` | Console/API server; outbound HTTP client for modules |
| `gpta/module_api.py`, `gpta/blocks.py` | Module interface; receipt block kinds |
| `gpta/renderer.py`, `gpta/fonts/` | Receipt layout (image and plain text); the bundled font and its license |
| `modules/<id>/` | One module per folder |
| `web/` | Console (plain HTML/CSS/JS, no build step) |
| `docs/` | [Module authoring](docs/module-authoring.md), [safety](docs/safety.md), [decisions](docs/decisions.md) |
