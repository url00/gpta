# Decisions

Short records of design choices, why they were made, and when to revisit them.

## 1. Module isolation

**Decision (2026-10-18):** keep modules in-process for first-party, trusted
code, protected by host guards (fetch timeout, single-flight, bounded
output/HTTP/cache/logs, serialized printer, rate limit, backoff, quarantine).

Rationale: the module interface and single egress path make these guards
enforceable in one place; a subprocess worker would add IPC, packaging,
credential, and printer complexity before a concrete third-party need exists.

**Revisit when** any of these becomes true:

- Third-party or LLM-generated modules are accepted.
- A module needs a hard resource cap (memory/CPU) rather than a deadline.
- Modules need crash isolation.

Required model at that point: a child process per module, `Block` data over
JSON IPC or stdout, hard kill and restart on timeout, and explicit credential
passing. That changes the module interface, so decide before the number of
modules grows much further.

## 2. Context extension point

**Decision (2026-10-18):** extend `ModuleContext` with a bounded `cache`
helper (used by Gutenberg, available to all modules) instead of adding a second
fetch API.

- Cached storage: `ctx.cache.read_text/write_text`, 50 MB / 30 day LRU cap.
- Parallel fan-out: stays module-local (Hacker News uses a bounded pool). The
  host cannot safely genericize module-specific fan-out.
- Credential loading: stays module-local for now; credentials files are
  module-specific and must never be exposed by the HTTP API.

A second fetch API would create two parallel interfaces without evidence of a
shared need. Revisit if three or more modules duplicate the same logic.

## 3. Logging sinks

**Decision:** the process owns a rotating file sink (`data/server.log`, 1 MB ×
3). When stderr is the systemd journal (`JOURNAL_STREAM` matches stderr's
device and inode), only the journal is used. Logs are echoed to stderr only
when it is a terminal; the manual start script redirects stderr to
`data/server.out`, which therefore holds only crashes and early failures. Text sinks log at INFO and above; the
in-memory ring shown in the console also keeps GPTA's DEBUG messages.

## 4. Authentication boundary

**Decision:** GPTA does not implement authentication. Deployments behind an
authenticating layer (Cloudflare Tunnel with Access/OAuth, an authenticating
reverse proxy, or a private network) do not need a second credential, and an
in-process token check cannot protect a directly exposed port on its own.
Binding beyond loopback logs a warning but still starts.

Browser-side defenses are kept because the console may run on a machine that
is also used for everyday browsing: a `Host` allowlist (`server.allowed_hosts`)
blocks DNS rebinding, and JSON-only writes plus an `Origin` check block CSRF.
A tunneled public hostname is added to the allowlist rather than disabling it.

## 5. Config evolution

**Decision (2026-10-24):** loading `config.json` repairs rather than rejects.
Saved settings are merged field by field with defaults; unknown or invalid
values are logged and replaced by defaults. API updates stay strict (400).
Only an unreadable file is fatal, with exit status 78, which the systemd unit
does not restart on.

Rationale: modules gain and lose options as they evolve. Rejecting the whole
file made any schema change stop the server from starting.

## 6. Core limits

**Decision (2026-10-24):** `max_chars` is a core limit every module has, since
the host enforces it. It counts the characters of the module's blocks (not the
header and footer the host adds). Modules declare only the extra limits they apply (for
example `max_items`), so the console never shows settings that do nothing.

## 7. Code layout

**Decision (2026-10-24):** `gpta/service.py` is a thin facade over separate
parts: `runner.py` (module runs), `scheduler.py` (when to run),
`outputs.py` (latest receipts), and `printing/` (print policy and device I/O).
Every physical print goes through `printing.Printer.submit()`, so new print
sources get the same enabled check, rate limits, and failure cut-off.

## 8. Image receipts and the Pillow dependency

**Decision (2026-10-24):** receipts are rendered as 1-bit images and printed
as ESC/POS raster graphics (`GS v 0`), replacing ASCII text. Pillow becomes
GPTA's one runtime dependency; until now the runtime was standard library
only. The font (DejaVu Sans Mono) is vendored in `gpta/fonts/`.

Rationale: text mode limited receipts to the printer's built-in font and code
page (so Unicode was reduced to ASCII) and could not draw rules, charts,
QR codes, or pictures. Drawing those needs font rasterisation and image
encoding; writing that on the standard library would mean a TrueType
renderer and a PNG encoder of our own. Pillow is mature, packaged by Nix
(which already provides the dev shell, scripts, and package), and used only
by the renderer and the raster encoder.

- The raw paths stay (`lp -o raw` and `/dev` device nodes); the CUPS raster
  driver is not used, so GPTA controls every dot, the bands, and the cut.
- Modules still return data, not images: small frozen, JSON-serialisable
  block dataclasses (`gpta/blocks.py`), so receipts can be saved and
  re-rendered at a new paper width, and the IPC route in decision 1 stays
  open. `state.json` stores blocks, not PNGs.
- The preview PNG is the printed image, pixel for pixel.

**Revisit when** the target hardware needs a dependency-free build (Pillow
not available), or a printer cannot handle `GS v 0` raster data.
