# Decisions

## 4.1 Module isolation tier

**Decision (2026-10-18):** keep modules in-process for first-party, trusted
code, with the Phase-1 host guards (fetch timeout, single-flight, bounded
output/HTTP/cache/logs, serialized printer, rate limit, backoff, quarantine).

Rationale: the ABI and single egress path make these guards enforceable in one
place; a subprocess worker would add IPC, packaging, credential, and printer
complexity now, before a concrete third-party need exists.

**Revisit when** any of these becomes true:

- Third-party or LLM-generated modules are accepted.
- A module needs a hard resource cap (memory/CPU) rather than a deadline.
- Modules need crash isolation.

Required model at that point: a child process per module, `Block` data over
JSON IPC or stdout, hard kill and restart on timeout, and explicit credential
passing. That changes the ABI, so decide before module count grows further.

## 4.2 Context extension point

**Decision:** extend `ModuleContext` with a bounded `cache` helper now (used by
Gutenberg and available to future modules) instead of adding a `fetch2` API.

- Cached storage: `ctx.cache.read_text/write_text`, 50 MB / 30 day LRU cap.
- Parallel fan-out: stays module-local (Hacker News uses a bounded pool). The
  host cannot safely genericize module-specific fan-out.
- Credential loading: stays module-local for now; credentials files are
  module-specific and must never be exposed by the HTTP API.

`fetch2` would create two parallel ABIs without evidence of a shared need.
Revisit if three or more modules duplicate the same logic.

## 4.3 Logging sink

**Decision:** the process owns a rotating file sink (`data/server.log`, 1 MB ×
3). Under systemd (`JOURNAL_STREAM` set), only stderr/journal is used. The
manual start script redirects early stderr to `data/server.out` so it cannot
interleave with the rotating handler.

## 4.4 Authentication boundary

**Decision:** GPTA does not implement authentication. Deployments behind an
authenticating layer (Cloudflare Tunnel with Access/OAuth, an authenticating
reverse proxy, or a private network) do not need a second credential, and an
in-process token check cannot protect a directly exposed port on its own.
Binding beyond loopback logs a warning but still starts. The JSON content-type
CSRF rule remains as a browser-side defense, not user authentication; `Host`
is not restricted so tunneled public hostnames work.
