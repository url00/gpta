# Module authoring

A module is a data source that returns receipt blocks; the host lays them
out as an image and prints it. Modules run in-process
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
from gpta.module_api import (
    MAX_ITEMS_LIMIT,
    Element,
    FetchError,
    Heading,
    KeyValue,
    Module,
    ModuleContext,
    Text,
)


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

    def fetch(self, ctx: ModuleContext) -> list[Element]:
        count = ctx.limits["max_items"]
        ctx.emit(f"printing {count} lines", "debug")
        return [
            Heading("Example"),
            *[Text(ctx.options["greeting"]) for _ in range(count)],
            KeyValue("Lines", str(count)),
        ]
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

## Receipt blocks

`fetch()` returns a list of blocks, all importable from `gpta.module_api`. The
host adds a header (the module name) and a footer (the run time) and lays
everything out on a 1-bit image as wide as the paper (576 dots on 80 mm, 384
on 58 mm), in DejaVu Sans Mono at three sizes (small, body, large). Text is
wrapped to the paper width; don't pre-wrap or pad it.

| Block | Fields | Prints |
|---|---|---|
| `Block(title, lines, meta={})` | `str`, `tuple[str, ...]`, `dict` | A large bold title over body-text lines; `""` adds a half-line gap |
| `Heading(text)` | `str` | Large bold text |
| `Text(text, size="body", bold=False, align="left")` | `size`: `small`, `body`, `large`; `align`: `left`, `center`, `right` | A wrapped paragraph; `\n` starts a new line |
| `Rule(heavy=False)` | `bool` | A horizontal line (2 dots, or 4 when heavy) |
| `KeyValue(label, value)` | `str`, `str` | Label on the left, bold value right-aligned; wraps onto two lines if both don't fit |
| `Checkbox(text, checked=False, value="", note="")` | `str`, `bool`, `str`, `str` | A drawn box (filled when checked), `text` with a hanging indent, bold `value` right-aligned on the first line (e.g. a due date), and `note` in small text underneath |
| `BarChart(bars, title="", max_value=None)` | `tuple[Bar, ...]`, `str`, number | One row per bar: label, black bar, value. Bars scale to `max_value` (default: the largest value) |
| `Bar(label, value, display="")` | `str`, number, `str` | `display` replaces the printed number, e.g. `"1.2k"` |

`BarChart` also accepts `(label, value)` tuples in place of `Bar`s. Blocks are
frozen dataclasses with plain fields, so the host can save them as JSON and
re-render them later (for example at a new paper width); `Block.meta` is
saved only if it is JSON-serialisable and never printed. Returning anything
else from `fetch()` fails the run.

Text keeps its Unicode characters. Characters the font has no glyph for
(e.g. CJK, emoji) print as `�`; control characters become spaces.
`max_chars` counts the characters of your blocks' text (titles, lines,
labels, values) plus one per rule and per bar, not the header and footer. When a receipt is over the limit,
the block that crosses it is cut short, later blocks are dropped, and
`[truncated]` is printed.

QR codes and images are not available yet. They will be new block kinds
alongside these, so existing modules won't change.

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
| Output bound | Receipt text truncated to `limits.max_chars` |
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
- Treat input data as untrusted text. It is printed literally (blocks have no
  markup), so it cannot change the layout beyond its own length.

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
