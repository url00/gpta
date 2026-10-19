# Module authoring contract

A module is a data source that returns receipt blocks. Modules run in-process
in the server. The host enforces **time and size** bounds; it does not sandbox
code.

## Layout

```
modules/<id>/
  module.py        # exactly one Module subclass, id == folder name
  data/            # optional static files shipped with the module
```

Create a class with:

- `id`, `name`, `description` strings; `id` must equal the folder name.
- `defaults` containing `enabled`, `interval_seconds`, `limits`, `options`.
- `limits_schema` / `options_schema` describing editable fields.
- `fetch(ctx) -> list[Block]` returning promptly.

```python
from gpta.module_api import Block, Module, ModuleContext


class ExampleModule(Module):
    id = "example"
    name = "Example"
    description = "Prints one line."

    defaults = {
        "enabled": False,
        "interval_seconds": 3600,
        "limits": {"max_items": 1, "max_chars": 500},
        "options": {},
    }

    def fetch(self, ctx: ModuleContext) -> list[Block]:
        return [Block("EXAMPLE", ("hello",))]
```

Add a hermetic test in `tests/test_modules.py` named `test_<id>...`.
`tests/test_module_coverage.py` fails when a module has no matching test.

## Host guarantees

| Guarantee | Value |
|---|---|
| Fetch timeout | `server.run_timeout_seconds` (default 120s) |
| Single-flight | At most one active run per module |
| Abandoned workers | At most one timed-out fetch thread per module |
| Output bound | Receipt truncated to the module's `limits.max_chars` |
| HTTP body bound | 2 MB per response; JSON raises, text truncates |
| HTTP timeout | 10s per request (`ctx.http`) |
| Cache bound | 50 MB / 30 days, LRU eviction (`ctx.cache`) |
| Log ring | 1000 entries in memory |
| Emit bound | 100 `ctx.emit` calls per run |
| Printer | Serialized writes, deadline, 30s spacing, hourly cap |
| Failure backoff | Interval doubles after each failure, capped at 7 days |

The scheduler dispatches runs asynchronously and uses a monotonic clock, so a
slow module cannot delay other modules and wall-clock jumps do not cause run
storms.

## Module responsibilities

A module **MUST**:

- Return from `fetch()` promptly and avoid unbounded loops.
- Use `ctx.http` for all network access, so timeouts and body caps apply.
- Use `ctx.cache` for downloads rather than writing into `data/` directly.
- Keep `ctx.emit` bounded; only the first 100 messages per run are kept.
- Join any thread it starts before returning. Do not spawn subprocesses.
- Treat input data as untrusted text; the renderer sanitizes to ASCII.

A module **MUST NOT**:

- Access the printer, the config store, or another module's files.
- Attempt to bypass the timeout by moving work to another thread that outlives
  `fetch()`.
- Assume the process is sandboxed or that secrets are isolated.

## Trust boundary

Modules are **trusted first-party code**. Import-time and fetch-time code runs
with full process privileges: filesystem, network, and the printer device. The
host's guards are reliability controls, not a security boundary.

If third-party or LLM-generated modules are ever accepted, subprocess isolation
(a hard kill on timeout) is a prerequisite. See
[decisions.md](decisions.md).

## Exceptions

Raise `gpta.module_api.FetchError` when source data is unavailable.
`gpta.http.FetchError` subclasses it, so `except FetchError` covers both your
errors and network failures. Any other exception is recorded as a failed run
and triggers backoff.
