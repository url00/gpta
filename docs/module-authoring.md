# Module authoring

A module is a data source that returns receipt blocks. Modules run in-process
in the server. The host enforces **time and size** bounds; it does not sandbox
code.

## Adding a module

1. Create `modules/<id>/module.py` with exactly one `Module` subclass whose
   `id` equals the folder name. Static files the module needs go in
   `modules/<id>/data/`.
2. Define the class (see below) and its `fetch(ctx)`.
3. Add a hermetic test to `tests/test_modules.py` named `test_<id>...`, using
   the fake HTTP client there. `tests/test_module_coverage.py` fails when a
   module has no matching test.
4. Restart the server. Modules are discovered at startup; there is no hot
   reload. A module that fails to import or validate is quarantined: it is
   listed in `/api/state` under `registry_errors` and the others still start.

## The module class

```python
from gpta.module_api import MAX_ITEMS_LIMIT, Block, FetchError, Module, ModuleContext


class ExampleModule(Module):
    id = "example"
    name = "Example"
    description = "Prints a few lines."

    defaults = {
        "enabled": False,
        "interval_seconds": 3600,
        "limits": {"max_chars": 500, "max_items": 3},
        "options": {"greeting": "hello"},
    }
    limits_schema = {"max_items": MAX_ITEMS_LIMIT}
    options_schema = {
        "greeting": {"type": "str", "label": "Greeting", "max_length": 40},
    }

    def fetch(self, ctx: ModuleContext) -> list[Block]:
        count = ctx.limits["max_items"]
        ctx.emit(f"printing {count} lines", "debug")
        return [Block("EXAMPLE", tuple([ctx.options["greeting"]] * count))]
```

- **`defaults`** must contain `enabled`, `interval_seconds` (5-604800),
  `limits`, and `options`, with a value for every limit and option.
- **Limits.** Every module has the core limit `max_chars`; the host truncates
  the receipt to it. `limits_schema` declares only the *extra* limits the
  module applies, such as `max_items`. Don't declare limits the module
  ignores; they would show up as settings that do nothing.
- **Options.** `options_schema` maps each option to a field schema:
  `type` (`int`, `bool`, `str`, `select`), `label`, and optionally `min`/`max`
  (int), `max_length` (str), `choices` (select), and `optional: true` to allow
  `null`.
- Defaults are checked against the schemas at startup; a mismatch
  quarantines the module.

### Changing a module's settings later

Saved settings are merged field by field with the module's defaults. A newly
added limit or option takes its default; a removed or now-invalid one is
dropped with a warning in the log. Renaming an option therefore resets it.

## The context

| `ctx.` | Use |
|---|---|
| `limits`, `options` | The validated settings for this run |
| `now` | Run time (UTC `datetime`) |
| `http` | `get_json`, `get_text`, `request_json`, `post_form_json` |
| `cache` | `read_text(key)` / `write_text(key, text)`, e.g. key `"<id>/item.txt"` |
| `emit(message, level)` | Log to the console; level `debug`, `info`, `warning`, `error` |
| `data_dir` | The data directory (for credentials files; prefer `cache` for downloads) |

Raise `gpta.module_api.FetchError` when source data is unavailable.
`gpta.http.FetchError` subclasses it, so `except FetchError` covers both your
errors and network failures. Any exception fails the run and triggers backoff.

## Host guarantees

| Guarantee | Value |
|---|---|
| Fetch timeout | `server.run_timeout_seconds` (default 120s) |
| Single-flight | At most one active run per module |
| Abandoned workers | At most one timed-out fetch thread per module |
| Output bound | Receipt truncated to `limits.max_chars` |
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

A module **must**:

- Return from `fetch()` promptly and avoid unbounded loops.
- Use `ctx.http` for all network access, so timeouts and body caps apply.
- Use `ctx.cache` for downloads rather than writing into `data/` directly.
- Join any thread it starts before returning. Do not spawn subprocesses.
- Treat input data as untrusted text; the renderer reduces it to ASCII.

A module **must not**:

- Access the printer, the config store, or another module's files.
- Try to outlive the timeout by moving work to a thread that outlives
  `fetch()`.
- Assume the process is sandboxed or that secrets are isolated.

## Trust boundary

Modules are **trusted first-party code**. Import-time and fetch-time code runs
with full process privileges: filesystem, network, and the printer device. The
host's guards are reliability controls, not a security boundary. Accepting
third-party or generated modules would first need process isolation; see
[decisions.md](decisions.md).
