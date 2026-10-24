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
| Print loop / runaway | Min spacing + hourly cap | Extra prints are skipped (auto) or rejected `429` (manual test) | Adjust `printer.*` config |
| Repeated print failures | `failure_threshold` consecutive failures | Printer auto-disabled, error logged, `disabled_reason` exposed | Re-enable in the UI/API |
| Repeated module failures | Non-zero `consecutive_failures` | Effective interval doubles, capped at 7 days | One success resets it |
| Wall-clock jump | Scheduling uses `time.monotonic()` | No burst or stall; wall clock is display-only | Automatic |
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
- **Printer device:** configuration is restricted to normalised paths under
  `/dev/`; the transport resolves symlinks, requires the target to stay under
  `/dev/`, and refuses non-character devices.
- **Bind address:** the default is `127.0.0.1`. Binding elsewhere logs a warning
  about the missing authentication boundary but still starts.
- **Lock hierarchy:** service lock → config lock. Never take the service lock
  while holding the config lock.

## Scheduler liveness

`/api/state.server` reports `scheduler_running`, `last_tick_at`,
`last_tick_age_seconds`, and `scheduler_stale` (no tick for more than 10s while
the thread is alive). A stalled scheduler is visible even though a Python
thread still reports itself alive.
