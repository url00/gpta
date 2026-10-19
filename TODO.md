# GPTA Codebase Audit — TODO

**Date:** 2026-10-18
**Scope:** Structural and system-level review ahead of expanding the number and
types of modules. Code quality / style items are deferred to Phase 5 by design.

---

## Verdict

**The foundation is sound and can be built on — but not yet safely expanded.**

The architecture is clean and well-layered: registry discovery with strict
validation, atomic config persistence, schema-driven limits/options, a shared
HTTP egress point, a bounded log ring, and a test suite that runs hermetically.
The module ABI (`gpta/module_api.py`) is a genuinely good contract for growth.

However, the runtime currently provides **cooperative isolation only**. There is
no enforcement of time, memory, or device-access bounds on a running module.
One misbehaving module can freeze the scheduler for the entire server, wedge
itself permanently in the "running" state, or interleave garbage on the
printer. These gaps are all fixable within the current architecture, and the
sequenced plan below does so **before** any new modules are added.

### Headline risks (all reproducible from code, no hypotheticals)

| # | Risk | Evidence |
|---|---|---|
| 1 | **A hung `fetch()` freezes the whole server's scheduler.** `tick()` calls `run_module()` synchronously on the single scheduler thread; `module.fetch(ctx)` has no timeout. An infinite loop, a hung file read, or a slow-drip HTTP response blocks the loop forever. The HTTP server stays up and `scheduler_running` keeps reporting `true` (the thread is alive, just stuck), so the failure is silent. | `gpta/service.py:102-131` (`_loop`, `tick`), `gpta/service.py:165` (`module.fetch(ctx)`), `gpta/service.py:275-277` (`is_alive()` check) |
| 2 | **A hung module wedges itself permanently.** `runtime.running = True` is set before `fetch` and cleared in `finally` — which never runs if the thread is stuck. The module then 409s on every manual run and is skipped by the scheduler forever. Recovery currently requires a server restart. | `gpta/service.py:140-142`, `gpta/service.py:213-220` |
| 3 | **A hung module also hangs the HTTP request thread.** `POST /api/modules/<id>/run` executes the fetch inline on the handler thread with no timeout. | `gpta/httpserver.py:99`, `gpta/service.py:135` |
| 4 | **Printer writes are unserialized and untimeouted.** `test_printer()` (HTTP thread) and a scheduled auto-print (scheduler thread) can both call `send_to_device()` concurrently → interleaved ESC/POS garbage. A jammed device blocks `handle.write()` forever, which (via risk 1) freezes the scheduler. | `gpta/printer.py:26-28`, `gpta/service.py:188-205`, `gpta/service.py:244-260` |
| 5 | **No runaway-print guard.** `interval_seconds` may be as low as 5s with `auto_print` on. N modules × 5s interval = an unbounded print rate; there is no global rate limit, no failure circuit breaker, and no paper-out consideration. | `gpta/config.py:83-88`, `gpta/service.py:188` |
| 6 | **Unbounded response body for JSON.** `request_json()` calls `_request()` without `read_limit` → `resp.read()` with no cap. A hostile or misbehaving API endpoint (i.e., exactly what new modules will talk to) can exhaust memory. `get_text()` caps at `max_bytes`; JSON does not. | `gpta/http.py:49`, `gpta/http.py:59-76` |
| 7 | **Wall-clock scheduling.** `clock=time.time` governs due-ness. An NTP/DST jump backwards makes every module instantly due → burst of simultaneous runs and prints; a jump forwards stalls everything until real time catches up. | `gpta/service.py:61`, `gpta/service.py:119-125` |
| 8 | **One broken module prevents server startup.** Any import/ validation error in any `module.py` raises `RegistryError` and the whole server refuses to boot. Bad for an appliance, and worse as module count grows. | `gpta/registry.py:40-41`, `gpta/registry.py:82-83` |
| 9 | **Unbounded log file growth, no rate limit.** The LogBus ring is bounded (good), but every module emit also goes to stderr → `data/server.log` (manual start) with no rotation and no per-module emit cap. A module in a fast error loop at the 5s minimum interval spams the disk. | `gpta/logbus.py:13`, `gpta/__main__.py:38-41`, `scripts/start.sh` |
| 10 | **No documented safety model.** The module-authoring contract (README "Adding a module") does not mention timeouts, output bounds, blocking rules, or what the host enforces. Guards that do exist (truncation, single-flight, body caps) are undocumented. | `README.md:128-137` |

---

## Phase 0 — Document the intended safety model first (cheap, guides all else)

- [ ] **0.1 Write `docs/module-authoring.md` — the module contract.** Specify:
  what the host guarantees (fetch timeout, single-flight per module, output
  truncation by `max_chars`, bounded HTTP bodies, bounded log ring, printer
  serialization) and what the module must guarantee (no infinite loops, use
  `ctx.http` only for egress, no background threads/subprocesses at import or
  in fetch, bounded `ctx.emit` usage, return `Block`s promptly). State the
  trust boundary explicitly: **modules run in-process and are trusted code**;
  isolation is enforced-by-host for time and size, not sandboxed. If third-party
  modules are ever planned, subprocess isolation becomes a hard requirement
  (see Phase 4 decision point).
- [ ] **0.2 Write `docs/safety.md` (or a README section) — error-condition
  behavior matrix.** For each failure mode, state detection, mitigation, and
  recovery: module hangs (timeout → marked failed, scheduler unaffected),
  module raises (run failed, next run per interval), module returns nothing
  ("No content" block), printer offline (run still succeeds, `print_error`
  recorded), printer busy (queued/serialized), repeated failures (backoff),
  config corrupt (startup error), unknown module (404), concurrent run (409).
  Several of these behaviors exist today — they just aren't written down
  anywhere.
- [ ] **0.3 Add a "Known limits of isolation" section** covering: Python
  threads cannot be killed (a timed-out fetch's thread is abandoned but the
  process survives; single-flight bounds zombies to one per module), no memory
  caps on a module, module import code runs with full process privileges.

## Phase 1 — Execution isolation (the core "before expanding" work)

- [ ] **1.1 Add a per-run timeout around `module.fetch()`.** Run each fetch in
  a worker (e.g., `ThreadPoolExecutor` submit + `future.result(timeout=T)`).
  On timeout: mark the module failed with `"timeout after Ts"`, clear
  `running`, and let the scheduler/HTTP caller proceed. Keep per-module
  single-flight so a timed-out-but-stuck thread cannot pile up (max one
  abandoned thread per module — document this in 0.3). Default `T` ~120s,
  configurable. This closes headline risks 1, 2, and 3.
- [ ] **1.2 Make the scheduler non-blocking.** `tick()` should *dispatch* runs
  (async, via the executor from 1.1) rather than execute them inline, so one
  slow-but-not-hung module (e.g., Gutenberg's up-to-8 × 1MB downloads at a 10s
  timeout each ≈ 80s worst case today) cannot delay every other module's
  schedule.
- [ ] **1.3 Serialize and timeout printer access.** Add a dedicated printer
  lock (or a single writer thread with a queue) so `test_printer` and
  auto-print can never interleave device writes. Bound the device write
  (write to a temp buffer + `select`/deadline, or a writer thread with a
  join timeout) so a jammed device degrades to a `print_error` instead of a
  frozen scheduler. Closes risks 4 and the second half of 1.
- [ ] **1.4 Add a global print rate limiter / circuit breaker.** Enforce a
  configurable minimum spacing between physical prints (e.g., ≥30s) and a max
  prints-per-hour; on N consecutive device failures, auto-disable the printer
  with a loud log + UI state. This is the explicit runaway-print-loop guard.
- [ ] **1.5 Cap response bodies for *all* HTTP egress.** Give `request_json`
  (and `_request` generally) a default `read_limit` (e.g., 2MB, overridable).
  Closes risk 6.
- [ ] **1.6 Switch due-ness scheduling to a monotonic clock.** Keep wall-clock
  only for display timestamps. Closes risk 7 (burst runs / print storms after
  clock adjustments).
- [ ] **1.7 Add failure backoff.** A module whose fetch fails should back off
  exponentially (interval → 2× → … capped) until it succeeds once, preventing
  tight error loops at the 5s minimum interval from spamming logs and the
  printer. Pairs with 1.4.

## Phase 2 — Availability and scale as module count grows

- [ ] **2.1 Fail-soft registry.** On a broken module (import error, invalid
  ABI), quarantine it: log a clear error, expose it in `/api/state` with an
  error state, and start the server with the remaining modules. Today one bad
  folder bricks the appliance. Keep strict validation for modules that do
  load. Closes risk 8.
- [ ] **2.2 Log hygiene.** `RotatingFileHandler` (or journald-only under
  systemd) for the file sink, plus a per-run cap on `ctx.emit` calls (e.g.,
  100/run, then a single "emits truncated" notice). The in-memory ring is
  already bounded; the disk sink is not. Closes risk 9.
- [ ] **2.3 Cache eviction.** `data/cache/gutenberg/` grows without bound.
  Add an eviction policy (LRU size cap or max age) — ideally a generic
  `ctx.cache` helper in the module API so future modules get bounded caching
  for free rather than each rolling its own.
- [ ] **2.4 Slim `/api/state`.** It embeds the last 200 log entries on every
  2s poll even though `/api/logs?since=N` exists for incremental fetch, and
  it deep-copies the whole config plus renders the total receipt under the
  global service lock. Split logs out of state, and avoid holding the service
  lock across `config.snapshot()` deepcopy + `render_total` (also document the
  lock hierarchy: service → config, never the reverse).
- [ ] **2.5 Scheduler liveness signal.** `scheduler_running` is
  `thread.is_alive()`, which reports `true` for a stuck thread. Add a
  last-tick heartbeat timestamp to `/api/state` so a frozen scheduler is
  visible in the UI (defense-in-depth behind 1.1/1.2).

## Phase 3 — Security hardening for an unauthenticated local console

- [ ] **3.1 Validate the `Host` header.** Without it, a DNS-rebinding page can
  reach the console same-origin from a browser. Accept only the configured
  host/localhost literals; 403 otherwise.
- [ ] **3.2 Restrict the printer device path.** `printer.device` accepts any
  absolute path and the transport opens it `wb` — as-is, the API is an
  arbitrary-file-write primitive for any local process (or via 3.1). Restrict
  to paths under `/dev/` (and ideally character devices only).
- [ ] **3.3 Optional token auth** for deployments that bind beyond
  `127.0.0.1`, so the safety story doesn't depend solely on the bind address.
  (README already warns about this — make it enforceable.)
- [ ] **3.4** Confirm the existing CSRF posture holds as endpoints are added:
  the JSON `Content-Type` requirement currently blocks cross-origin
  state-changing simple requests; keep that invariant and note it in
  `docs/safety.md`.

## Phase 4 — Structural decision point before module expansion

- [ ] **4.1 Decide the isolation tier for future module types.** In-process
  (current) with Phase-1 guards is fine for first-party, trusted modules.
  Write the decision down. If third-party or LLM-generated modules are
  planned, spec the subprocess-worker model now (module in a child process,
  stdout/IPC for Blocks, hard kill on timeout) because it changes the ABI —
  better to decide before 10 more modules exist than after.
- [ ] **4.2 Consider a module API `fetch2`/context extension point** for
  common needs observed in the current four (cached storage, parallel fan-out
  as used by hackernews, credential loading as used by google_tasks) so each
  new module doesn't hand-roll them with varying safety properties.

## Phase 5 — Code quality (lower priority, per audit scope)

- [ ] **5.1** Unify the two `FetchError` classes (`gpta/module_api.py:18` and
  `gpta/http.py:19`) or document their distinct roles; modules currently
  import one while the client raises the other.
- [ ] **5.2** `describe(module)` is recomputed (with deepcopies) per module on
  every `/api/state` poll; compute once at registry time.
- [ ] **5.3** `gpta/httpserver.py:23` uses logger name `"gpta.http"` which
  collides conceptually with `gpta/http.py`'s client logger; rename to
  `"gpta.httpserver"`.
- [ ] **5.4** Add a `tests/` guard so new modules can't be added without a
  hermetic test (CI convention; the existing fake-HTTP pattern is a good
  template).

## Phase 6 — Tests proving the guards (add alongside Phases 1–3)

- [ ] **6.1** Hung module: `fetch` sleeps forever → run fails with timeout,
  scheduler keeps ticking, other modules still run, manual run no longer 409s
  forever.
- [ ] **6.2** Concurrent printer access: scheduled print + `test_printer`
  cannot interleave writes (assert serialization under the writer lock).
- [ ] **6.3** Print rate limiter: rapid module runs produce bounded physical
  prints; consecutive device failures trip the circuit breaker.
- [ ] **6.4** Oversized JSON response: `request_json` raises/truncates at the
  default cap instead of reading unbounded bytes.
- [ ] **6.5** Clock jump: simulating a backwards wall-clock jump does not
  trigger a burst of runs (monotonic scheduling).
- [ ] **6.6** Registry quarantine: a broken `module.py` yields a degraded but
  running server with the module marked errored.
- [ ] **6.7** Failure backoff: consecutive failures increase effective
  interval; success resets it.

---

## What is already good (validated — do not regress)

These are the reasons the codebase *is* buildable-on; preserve them while
making the changes above:

- **Clean layering and ABI.** `module_api.py` is small, explicit, and
  schema-driven; the registry enforces one-class-per-folder and id-matching.
- **Atomic, validated config.** tmp+fsync+rename writes, unknown-key and
  type rejection, limits bounded by schema (`max_chars` ≤ 10000 bounds stored
  output per module).
- **Output bounding.** `render_receipt` truncates by `max_chars`; the log
  ring is a bounded deque; HTTP request bodies capped at 64KB.
- **Single egress path.** All modules must use `ctx.http`, which makes the
  Phase-1 body/time caps enforceable in one place.
- **Sane HTTP hygiene.** Content-Type enforcement, static path traversal
  blocked (tested), CSP on HTML, no-store, nosniff, localhost bind by default.
- **Hermetic tests** with fake transports covering modules, service, config,
  registry, renderer, and the API end-to-end.

## Suggested sequencing

Phases 0 and 1 together are the gate: **do not add new modules until 1.1–1.7
land** (0.1–0.3 can be written in parallel as they only document intent).
Phases 2–3 harden for scale and exposure. Phase 4 is a decision, not code —
make it early. Phases 5–6 are continuous.
