# Safety model

The server assumes trusted modules and a trusted local operator. This document
records what happens when things go wrong.

## Failure matrix

| Failure | Detection | Mitigation | Recovery |
|---|---|---|---|
| Module fetch hangs | Worker thread exceeds `server.run_timeout_seconds` | Run fails with `timeout after Ns`; `running` cleared; scheduler keeps ticking; a new manual run returns `ok: false` ("still executing"), never a permanent 409 | A stuck thread is abandoned (bounded to one per module); restart only if it never returns |
| Module raises | Exception from `fetch()` | Run marked failed, error recorded, previous output kept | Next run after backoff |
| Module returns nothing | `fetch()` returns `[]` | Renders a "No content" block | Next run |
| Module import/validation fails | Registry validation, including defaults checked against the module's own schema | Module quarantined, error exposed in `/api/state.registry_errors`, other modules start | Fix the folder and restart |
| Printer offline | Open/write error, or write deadline | Run still succeeds; `print_error` recorded; consecutive failures counted | Fix device; failures reset on success |
| Printer busy | Single writer thread | Writes are serialized, never interleaved | Automatic |
| Printer jammed | `write_timeout_seconds` deadline | Caller gets `PrinterError`; device thread may remain blocked; jobs that time out while still queued are cancelled and never print | Restart if the kernel never returns |
| Print loop / runaway | One queue: min spacing for automatic prints, hourly cap, 50 waiting jobs at most, a module's newer scheduled receipt replaces its waiting one | Jobs wait for a slot; over 50 waiting, new prints are refused (`429`, or "not printed" on the module) | Pause, cancel jobs, or adjust `printer.*` |
| Very long print | `printer.warn_length_mm` (default 2.5 m) | The job waits at the head of the queue, holding the jobs behind it, until approved or cancelled. A print already confirmed in the console is not held again | Approve or cancel on the Queue tab |
| Panic | Pause printing (every tab) / `POST /api/queue/pause` | No further job starts, even after a restart. The job on the printer can be stopped: between raster bands on a device, by cancelling the job on CUPS; paper already printed stays | Resume when ready |
| Forgotten jobs | `printer.job_expiry_hours` (default 24) | A job still waiting after that long is dropped and logged | Automatic |
| Restart while printing | Jobs are stored in `data/gpta.db` | Waiting jobs print after the restart; the job that was printing is marked failed, not printed twice | Retry it on the Queue tab if needed |
| Repeated print failures | `failure_threshold` consecutive failures | Printer auto-disabled, error logged, `disabled_reason` exposed; waiting jobs stay queued | Re-enable in the UI/API; retry failed jobs |
| Repeated module failures | Non-zero `consecutive_failures` | Effective interval doubles, capped at 7 days | One success resets it |
| Wall-clock jump | Scheduling uses `time.monotonic()` | No burst or stall; wall clock is only used to carry last-run times across restarts | Automatic |
| Server restart | `data/gpta.db` (last run, result, failure count, latest receipt per module; each module's `ctx.store`) | Only modules that are due run; backoff continues; a saved run time in the future counts as "just ran" | Automatic |
| Database missing | `Database` | A new one is created; modules start as never run (each enabled module runs once) and with empty stores | Automatic |
| Database corrupt or from a newer GPTA | `Database` | Warning logged; the file is moved to `gpta.db.corrupt-<time>` and a new one started. If that fails too, state is kept in memory until the next restart (error logged) | Automatic; inspect or delete the moved file |
| Run state cannot be saved (disk full) | `StateStore.save_module()` | Error logged; the run still counts | Free space; saved after the next run |
| Module store cannot be saved, or is over its bounds | `ModuleStore.commit()` | The run fails before anything is shown or printed; nothing is saved; backoff applies | Fix the module or free space |
| Module run fails or times out after writing to its store | Writes are held until the run succeeds | Nothing is saved; a timed-out worker's later writes raise | Automatic |
| Stored setting unknown or invalid (e.g. after a module schema change) | `ConfigStore.load()` | Warning logged; the setting falls back to its default; other settings are kept | Automatic; the file is rewritten on the next save |
| Config file unreadable (not JSON / not an object) | `ConfigStore.load()` | Startup exits with status 78 and a one-line error; systemd does not restart on 78 | Fix/remove `data/config.json` |
| Unknown module/output | Route lookup | `404` | — |
| Concurrent manual run | Runtime flag | `409` | Wait for the run or timeout |
| Timed-out worker still alive | Abandoned-thread check | New run returns `ok: false` with a clear error (not `409`) | Recovers when the worker returns; restart if it never does |
| Oversized JSON response | `read_limit` exceeded | `FetchError`, no unbounded read | — |
| Oversized request to API | `MAX_BODY` | `400` | — |
| Log storm | Rotating file (1 MB × 3), 1000-entry ring, 100 emits/run | Bounded disk/memory | Automatic |

## Known limits of isolation

- **Threads cannot be killed.** A timed-out fetch is abandoned, not stopped.
  Single-flight bounds abandoned workers to one per module. A truly infinite
  loop keeps one thread alive until process exit.
- **No memory cap.** A module can allocate the whole heap; process limits
  (`systemd` `MemoryMax=`, cgroups, `ulimit`) are the control.
- **Import-time code is privileged.** A module can run arbitrary code with the
  server's filesystem, network, and device permissions.
- **Printer writes can wedge.** The deadline protects callers, but the daemon
  writer thread can stay blocked in the kernel. No further jobs are written.
- **In-process isolation is cooperative.** These guards protect availability,
  not against malicious code.

## Security posture

- **No application authentication:** GPTA does not authenticate users. It MUST
  be hosted behind another authentication layer (for example a Cloudflare
  Tunnel with Access/OAuth, an authenticating reverse proxy, or a VPN) whenever
  it is reachable beyond loopback.
- **CSRF:** every `POST`/`PATCH` requires `Content-Type: application/json`
  (cross-site form posts cannot send it), and a browser `Origin` header must
  name an allowed host. Both checks run centrally in the request dispatcher,
  so new endpoints inherit them.
- **`Host` allowlist:** requests are answered only when the `Host` header is
  in `server.allowed_hosts` (plus the bind address). This is the DNS-rebinding
  guard. Tunneled public hostnames must be listed explicitly.
- **Web page fetches (reader mode):** `/api/reader/fetch` downloads a URL from
  the server, so it is guarded against reaching the local network: only
  http/https, no credentials in the URL, no proxies, and a host whose
  addresses are all loopback, private, link-local, shared or reserved is
  refused before connecting. The connected socket's peer is checked again
  (covering DNS answers that change), and so is every redirect. Pages are
  capped at 3 MB and must be HTML.
- **Printer device:** configuration is restricted to normalised paths under
  `/dev/`; the transport resolves symlinks, requires the target to stay under
  `/dev/`, and refuses non-character devices.
- **Printer data:** module text is only ever drawn into the receipt image;
  the printer receives raster dots plus GPTA's own fixed commands, so text
  from a source cannot smuggle ESC/POS commands to the printer.
- **CUPS queue:** the queue name is restricted to CUPS name characters and
  passed to `lp`/`lpstat`/`cancel` as a separate argument (no shell). A job
  still unfinished at the write deadline is cancelled so it cannot print
  after being reported as failed.
- **Bind address:** the default is `127.0.0.1`. Binding elsewhere logs a warning
  about the missing authentication boundary but still starts.
- **Lock order:** runner lock → config lock, never the reverse. The printer,
  scheduler, and output store locks guard only their own state and are never
  held while calling into another component.

## Scheduler liveness

`/api/state.server` reports `scheduler_running`, `last_tick_at`,
`last_tick_age_seconds`, and `scheduler_stale` (no tick for more than 10s while
the thread is alive). A stalled scheduler is visible even though a Python
thread still reports itself alive.
