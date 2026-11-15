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
| `server.public_url` | (empty) | The address phones reach the server at, e.g. `https://gpta.example.com`. Printed QR selection codes link to it, and its host is answered too. Set on the Settings tab; modules cannot print selection lists without it |
| `server.run_timeout_seconds` | `120` | Per-run fetch timeout |
| `printer.*` | disabled, `/dev/usb/lp0`, 80 mm paper | CUPS queue or device, paper size, auto-print, queue limits, module output defaults; see [Printer](#printer) |
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

Restarts are safe: `data/gpta.db` (SQLite) records when each module last ran,
how it went, and its latest receipt (as blocks, re-rendered on load), so a
restart only runs modules that are due (for example, one whose daily run was
missed while the server was down runs once). It also holds the state modules
save for themselves (`ctx.store`). A `data/state.json` from an older version
is imported on the first start and renamed `state.json.migrated`. Delete
`gpta.db` (with the server stopped) to make every enabled module run on the
next start and forget its saved state.

## Modules

| Module | Interval | Limits | Options |
|---|---|---|---|
| `affirmations` | 1 day | 500 chars | include date |
| `hackernews` | 7 days | 5 items, 1500 chars | window days, min score, scan limit, paper estimates |
| `google_tasks` | 5 minutes | 30 tasks per list, 3000 chars | morning time, time zone, list, completed today, credentials file |
| `google_calendar` | 5 minutes | 30 events, 3000 chars | morning time, time zone, calendar, 12/24-hour, credentials file |
| `weather` | 5 minutes | 3000 chars | morning time, time zone, latitude, longitude, units, 12/24-hour, alerts |

- **Daily Affirmation** picks one line per day from
  `modules/affirmations/data/affirmations.txt`; edit that file to customise.
- **Hacker News Weekly** scans the current top stories, keeps those inside the
  window, and prints the highest-scoring `max_items`, each beside a QR code.
  Scanning a code prints that article (reader mode of the linked page, or
  the post's own text). Each row also says roughly how much paper the
  article would use with the module's current size and layout; turn
  "Show paper estimates" off to skip the page downloads that needs. Needs
  `server.public_url`.
- **Google Tasks** checks your tasks every 5 minutes. The first check at or
  after the morning time (08:00, server-local unless a time zone is set)
  prints the day's list; after that a new list prints only when something
  changed (a task added, edited or ticked off). Tasks completed today print
  as ticked boxes. Nothing is fetched before the morning time, and **Run**
  always prints. Setup is below.

- **Google Calendar** works the same way for today's events: the itinerary
  prints at the morning time, then again whenever an event for today is
  added, moved, renamed or removed. All-day events come first; events you
  declined are left out. It reads your main calendar unless you name
  another, or `*` for every calendar ticked in Google Calendar's sidebar.
- **Weather** prints today's forecast at the morning time: conditions, high
  and low, precipitation, wind, UV, sunrise and sunset, and the rest of the
  day in three-hour steps, with any weather alerts in effect. Every 5
  minutes, day and night, it also checks for alerts and prints each new one
  once, in full; the `alerts` option limits that to moderate or severe ones,
  or turns it off. The forecast comes from [Open-Meteo](https://open-meteo.com/)
  and alerts from the US National Weather Service (so alerts are US-only);
  neither needs an account. With latitude and longitude blank it uses the
  server's location, looked up once a day from its public IP address by
  [ipapi.co](https://ipapi.co/); set both to choose a place or to skip that
  lookup.

### Google Tasks and Calendar setup

A personal Google account can only be reached through OAuth with your own
consent (service accounts cannot see a personal account's tasks, and Google's
TV/device sign-in does not allow the Tasks scope). GPTA uses a **Desktop app**
OAuth client and stores a refresh token, read-only (`tasks.readonly` and
`calendar.readonly`). Both modules share the one sign-in.

1. In the [Google Cloud console](https://console.cloud.google.com/), create a
   project (any name).
2. **APIs & Services → Library**: enable **Google Tasks API** and **Google
   Calendar API**.
3. **Google Auth Platform → Branding**: fill in an app name and your email.
   If it asks for a privacy policy, GPTA serves one at `/privacy` (edit
   `web/privacy.html` to taste); the address must be reachable publicly.
   **Audience**: choose **External**, then **Publish app** so the status is
   **In production**. This matters: while the app is in *Testing*, Google
   expires the sign-in every 7 days. No verification is needed for your own
   use.
4. **Google Auth Platform → Clients → Create client**: type **Desktop app**.
   Download its JSON file.
5. On the GPTA machine, run:

   ```bash
   python3 scripts/google_oauth.py --client-file ~/Downloads/client_secret_XXXX.json
   ```

   Open the link, pick your account, and at "Google hasn't verified this app"
   choose **Advanced → Go to (app name)**. If the GPTA machine has no browser,
   add `--paste`: open the link on another device and paste back the
   `http://127.0.0.1:8765/?...` address the browser ends on (the page itself
   fails to load; that is expected).
6. The script writes `data/google_credentials.json` (mode 0600, never exposed
   by the HTTP API) and lists your task lists and calendars. Enable the
   modules in the console and press **Run** for a first print.

If the module later reports that Google rejected the saved sign-in, run the
script again. That happens if you revoke access, or after 6 months without
use, and once after upgrading from 0.4.0 to add Calendar to the sign-in
(tick every permission box on Google's screen).

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

### Print queue

Every print (module runs, **Run now**, **Test print**, **Instant** and web
pages) becomes a job in one queue, kept in `data/gpta.db`, and one job prints
at a time, in order. The **Queue** tab shows what is printing, what is
waiting and what printed recently; waiting jobs can be reordered, cancelled
or viewed, and the job on the printer can be stopped (as soon as the printer
allows; what is already on paper stays). The queue is held, with the reason
shown on every tab, when:

- **Printing is paused.** The **Pause printing** button at the top of every
  tab is the panic switch: nothing more starts until you resume, even across
  a restart.
- **The next job is longer than `printer.warn_length_mm`** (default 2500,
  2.5 m). It waits, holding the jobs behind it, until you approve or cancel
  it; shorter jobs ahead of it print first, and you can move other jobs past
  it. A long Instant or web page print you already confirmed in its dialog
  is not held again.
- **The printer is disabled**, by you or automatically after
  `printer.failure_threshold` consecutive failures (default 3). Enabling it
  again clears the count and releases the queue. While it is disabled new
  prints are refused rather than queued.
- **A limit applies:** at most `printer.max_prints_per_hour` (default 60)
  jobs start per hour, and automatic prints start at least
  `printer.min_print_spacing_seconds` apart (default 30). Jobs wait for a
  slot; they are no longer skipped.

A module's newer scheduled receipt replaces its older one if that is still
waiting, at most 50 jobs wait at once, and a job still waiting after
`printer.job_expiry_hours` (default 24) is dropped. A failed job can be
retried from the Queue tab. A job that was printing when the server stopped
is marked failed, not printed again.

### Module output defaults

`printer.module_size` and `printer.module_layout` (Printer tab) set the print
size and column layout of module receipts. Each module card has its own
**Print size** and **Layout**, which override both the default and what the
module itself asks for; "Use default" leaves the choice to the module, then
to the printer default. Changes apply from the module's next run.

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
| PATCH | `/api/modules/<id>` | Update `enabled`, `interval_seconds`, `limits`, `options`, `style` (`size` and `layout`, each `default` or a value) |
| POST | `/api/modules/<id>/run` | Run now; queues a print if auto-print is on (`queued`, `job`) |
| PATCH | `/api/printer` | Update printer settings |
| POST | `/api/instant/preview` | Image of `{"text", "size", "layout", "format"}` as an instant print would draw it (`png` data URL), with `chars` and `paper_mm` (paper length) |
| POST | `/api/instant/print` | Print `{"text", "size", "layout", "format"}` once; text up to 150,000 characters (requests up to 1 MB). `format` is `plain` (default) or `markdown`: headings, lists, task lists, quotes, code, tables, rules and **bold**. `size` (here and for web pages) is `tiny`, `small`, `compact`, `body`, `large` or `huge`: 12–36 px body text. `layout` (both too) is `single`, `double` (two columns with a centre rule) or `triple` (three, no rules): columns are split between lines, in bands of at most about 15 cm, with headings, rules and a web page's header kept full width |
| POST | `/api/reader/fetch` | Fetch `{"url", "size", "layout"}` reader-mode style and lay out the whole page. Returns an `id` for printing, a `png` preview, `chars` (characters that will print), `total` (the page's characters; more than `chars` only past the 150,000 safety cap) and `paper_mm` (paper length). Public http(s) sites only |
| POST | `/api/reader/print` | Print `{"id", "size", "layout"}` from a recent fetch, exactly as previewed. The last 8 fetches are kept, until a restart |
| POST | `/api/printer/test` | Queue a test page |
| PATCH | `/api/server` | Update `public_url`, the address printed QR selection codes link to (`""` clears it). Other server settings need a restart |
| GET | `/s/<token>` | A scanned QR selection code: runs the module's `on_select()` for that item and prints the result; answers with a small HTML page. Repeats within 30 seconds are ignored; `410` once expired |
| GET | `/api/selections` | `public_url` and the 50 newest selection codes: `path`, `module`, `title`, `expires_at`, `expired`, `use_count`, `last_used_at`, `last_result` |
| POST | `/api/selections/test` | Make a sample selection list from `{"count", "side", "qr", "numbered", "rules", "expires_hours"}` and return its `png` preview and the code list; `"print": true` also prints it. Scanning a test code prints a confirmation |
| GET | `/api/queue` | The print queue: `paused`, `blocked` (why it is held, or null), `current`, `pending`, `history`, `warn_length_mm`. Each job has `id`, `source`, `kind`, `state`, `length_mm`, `needs_approval`, `can_retry`, times and `error` |
| GET | `/api/queue/<id>.png` | A waiting, printing or failed job's image |
| POST | `/api/queue/pause`, `/api/queue/resume` | Pause or resume all printing |
| POST | `/api/queue/clear` | Cancel every waiting job |
| POST | `/api/queue/<id>/approve` | Let a job over the length limit print |
| POST | `/api/queue/<id>/cancel` | Cancel a waiting job, or stop the one printing |
| POST | `/api/queue/<id>/move` | Reorder a waiting job: `{"to": "top" \| "up" \| "down" \| "bottom"}` |
| POST | `/api/queue/<id>/retry` | Queue a failed job again |

Writes need a JSON body (`{}` or empty is fine). Errors: 400 invalid request or
settings, 403 disallowed host/origin, 404 unknown module or path, 405 wrong
method, 409 module already running or printer disabled, 429 print queue
full. Print requests return once the job is queued, with the `job`; pass
`"confirmed": true` to `/api/instant/print` or `/api/reader/print` to queue a
job over the length limit already approved.

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
| `gpta/printing/` | Print queue and policy (`printer.py`), queued jobs (`jobs.py`), device I/O (`transport.py`), ESC/POS raster encoding (`escpos.py`) |
| `gpta/config.py`, `gpta/registry.py` | Settings; module discovery and validation |
| `gpta/httpserver.py`, `gpta/http.py` | Console/API server; outbound HTTP client for modules |
| `gpta/module_api.py` | Module interface: a facade re-exporting everything a module may import |
| `gpta/blocks.py`, `gpta/markdown.py` | Receipt block kinds and `Document` (layout, print size); Markdown to blocks |
| `gpta/selections.py` | QR selection lists (`ctx.selections`): codes a phone scans to choose a printed item |
| `gpta/storage.py`, `gpta/state.py`, `gpta/cache.py` | SQLite database and module stores (`ctx.store`); run state kept across restarts; download cache (`ctx.cache`) |
| `gpta/renderer.py`, `gpta/fonts/` | Receipt layout (image and plain text); the bundled font and its license |
| `modules/<id>/` | One module per folder |
| `web/` | Console (plain HTML/CSS/JS, no build step) |
| `docs/` | [Module authoring](docs/module-authoring.md), [safety](docs/safety.md), [decisions](docs/decisions.md) |
