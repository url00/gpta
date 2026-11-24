# Module authoring

A module is a data source that returns receipt blocks; the host lays them
out as an image and prints it. Modules run in-process
in the server. The host enforces **time and size** bounds; it does not sandbox
code.

Everything a module needs is imported from `gpta.module_api`: the block
kinds, `Document` (layout and print size), `markdown_blocks`, and the types
of the context's helpers. Don't import other `gpta.*` files from a module;
they are host internals and may move.

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
on 58 mm), in DejaVu Sans Mono at four sizes (small, body, large, huge). Text is
wrapped to the paper width; don't pre-wrap or pad it.

| Block | Fields | Prints |
|---|---|---|
| `Block(title, lines, meta={})` | `str`, `tuple[str, ...]`, `dict` | A large bold title over body-text lines; `""` adds a half-line gap |
| `Heading(text)` | `str` | Large bold text |
| `Text(text, size="body", bold=False, align="left", indent=0)` | `size`: `small`, `body`, `large`, `huge`; `align`: `left`, `center`, `right`; `indent`: 0 to 4 | A wrapped paragraph; `\n` starts a new line. `indent` moves it in by that many levels, each marked with a bar down the left (replies in a thread, quoted text); bars join up between neighbouring blocks at the same depth |
| `Rule(heavy=False)` | `bool` | A horizontal line (2 dots, or 4 when heavy) |
| `KeyValue(label, value)` | `str`, `str` | Label on the left, bold value right-aligned; wraps onto two lines if both don't fit |
| `Checkbox(text, checked=False, value="", note="")` | `str`, `bool`, `str`, `str` | A drawn box (filled when checked), `text` with a hanging indent, bold `value` right-aligned on the first line (e.g. a due date), and `note` in small text underneath |
| `BarChart(bars, title="", max_value=None)` | `tuple[Bar, ...]`, `str`, number | One row per bar: label, black bar, value. Bars scale to `max_value` (default: the largest value) |
| `Bar(label, value, display="")` | `str`, number, `str` | `display` replaces the printed number, e.g. `"1.2k"` |
| `QrCode(data)` | `str`, up to 1000 characters | A centred QR code, about 25 mm wide (larger for long data), e.g. a link to the full story |
| `QrItem(data, title, note="", label="", side="right", qr="medium", indent=0)` | `side`: `left`, `right`; `qr`: `small`, `medium`, `large` (about 15, 20, 25 mm) | A list row: a QR code for `data` on one side, with `label` and bold `title` beside it and `note` in small text underneath. Always full width, also in columns. For codes that select something on this server, use `ctx.selections` (below) rather than building these by hand |

`BarChart` also accepts `(label, value)` tuples in place of `Bar`s. Blocks are
frozen dataclasses with plain fields, so the host can save them as JSON and
re-render them later (for example at a new paper width); `Block.meta` is
saved only if it is JSON-serialisable and never printed. Returning anything
other than blocks (or a `Document` of blocks, below) from `fetch()` fails the
run.

`Text` also takes `bold_spans`: `(start, end)` character ranges of the text
to print in bold, end exclusive, e.g. `Text("Due today", bold_spans=((4, 9),))`.

## Selection lists (scan to choose)

A module can print a list where each item has a QR code, and act when one is
scanned: print the chosen article, mark a task done, and so on.
`ctx.selections.offer()` turns choices into blocks, and `on_select()`
receives the chosen one's payload:

```python
from gpta.module_api import Choice, Heading, Module, markdown_blocks


class StoriesModule(Module):
    ...

    def fetch(self, ctx):
        stories = load_stories(ctx)
        return [
            Heading("Top stories"),
            *ctx.selections.offer(
                [
                    Choice(s["title"], {"id": s["id"]}, note=f"{s['score']} points")
                    for s in stories
                ],
                expires_hours=72,
            ),
        ]

    def on_select(self, ctx, payload):
        story = load_story(ctx, payload["id"])
        return [Heading(story["title"]), *markdown_blocks(story["text"])]
```

- **`Choice(title, payload=None, note="")`**: `title` prints beside the code
  and `note` under it. `payload` is any JSON value up to 4,000 bytes; store an
  id, not the content. A `(title, payload[, note])` tuple works too.
- **`offer(choices, *, expires_hours=168, numbered=True, side="right",
  qr="medium", rules=True)`** returns `QrItem` blocks (with `Rule`s between
  them when `rules` is on). At most 50 choices per call; a module's newest
  500 codes are kept. `expires_hours` is 1 to 2160 (90 days).
- **`on_select(ctx, payload)`** returns blocks or a `Document`, like
  `fetch()`, and the host prints them under the module's name. Return a
  list of `Document`s (up to 5) to print each as its own job, in order: an
  article, then its comments. Every part is laid out before any is queued. `ctx.trigger`
  is `"select"`. `max_chars` is not applied (a chosen article prints whole,
  up to 150,000 characters, and a long print waits for approval like any
  other). `ctx.store` writes are saved if it succeeds. Raise `Skip` to print
  nothing. It runs under the same timeout as `fetch()`, separately from the
  module's scheduled runs: it does not change the receipt shown in the
  console or count towards backoff. It may call `ctx.selections.offer()`
  itself, for menus that lead to menus.
- A code can be scanned again until it expires; a second scan within 30
  seconds is ignored (phones often fetch a link before opening it), unless
  the first one failed.
- Codes link to `server.public_url` (Settings tab). Without it `offer()`
  raises `SelectionError` and the run fails with a message saying so; check
  `ctx.selections.available` to fall back to a plain list instead.
- The payload is never in the code, only a random token, so it cannot be
  forged or read from the paper. Treat `payload` as your own data, but
  remember the item may have changed or gone since it was printed.
- In tests, the default `ModuleContext` has a working in-memory
  `ctx.selections` whose links start with `http://gpta.test/s/`.

The Settings tab has a test screen that prints a sample list and shows every
live code with its scans and last result.

## Web pages and paper estimates

`ctx.reader` is reader mode for modules, with the same guards as the
console's web page tab (public http/https sites only, HTML only, 3 MB):

- `ctx.reader.read(url)` downloads a page and returns a `Reading`. Its
  `blocks` are the article: a header (title, site, address, a QR code to
  the original), a heavy rule, then the text.
- `ctx.reader.read_html(html, url, title="")` does the same for HTML the
  module already has, such as a post body from an API.
- `ctx.reader.article(url)` and `ctx.reader.html(...)` return just the
  blocks.

All raise `FetchError` when the page cannot be fetched or has no readable
text (a PDF, say).

A `Reading` also says what the page is made of, so a module can tell prose
from a page that will not survive a receipt printer: `words` (all text),
`code_words` (the part of it in code listings), `table_rows`, and `media`
(pictures, videos, embeds and formulas that were left out; icons and page
furniture are not counted). These are counts, not a verdict; Hacker News
turns them into one in its `unprintable()`.

`ctx.paper_mm(content)` says roughly how many millimetres of paper blocks
(or a `Document`) would use if `on_select()` returned them now: it lays them
out with the module's current size and layout and the printer's paper. Use
it to tell the reader what a choice costs before they scan it, as Hacker
News does. It is an estimate: the page, or the settings, may change before
the scan.

In tests the default context's `reader` raises `FetchError` (there is no
network); pass a fake with `dataclasses.replace(ctx, reader=...)` whose
`read()` returns `Reading(blocks, words=...)`.

## Markdown

`markdown_blocks(text)` turns Markdown into blocks, the same way the Instant
tab does: headings, paragraphs, bullet, numbered and task lists, quotes,
code, tables and rules; `**bold**` prints bold, and links print as their text
with the address in brackets. Use it for content that arrives as Markdown
(notes, issue bodies, LLM output) and mix the result with your own blocks:

```python
from gpta.module_api import Heading, Rule, markdown_blocks


def fetch(self, ctx):
    note = ctx.http.get_text(NOTE_URL)
    return [Heading("Today's note"), Rule(), *markdown_blocks(note)]
```

The text is still untrusted data: Markdown can only produce the block kinds
above, and `max_chars` applies to the result.

## Layout and print size

To choose how the receipt is set on the paper, return a `Document` instead
of a list:

```python
from gpta.module_api import Document, Heading, Rule, Text


def fetch(self, ctx):
    return Document(
        [Heading(title), Text(byline, size="small"), Rule(heavy=True), *body],
        layout="double",  # "single", "double", "triple"
        size="small",  # "tiny", "small", "compact", "body", "large", "huge"
    )
```

Leave `layout` or `size` out (or `None`) to use the operator's default for
module output; a plain list does the same for both. The operator can also
override either per module on its card, which wins over what the module asks
for, so treat them as a suggestion: don't rely on a particular width.

- `layout` sets body text in one, two or three columns. Headings, rules, QR
  codes and centred or right-aligned text always span the full width, and so
  do the first `lead` blocks. By default the lead is everything up to and
  including the first `Rule(heavy=True)`, so a header written as above stays
  full width; pass `lead=0` (or another count) to override.
- `size` scales every font on the receipt; `Text(size=...)` stays relative
  to it.
- The names are exported as `LAYOUTS` and `PRINT_SIZES`, so a module can
  offer them as a `select` option:
  `{"type": "select", "label": "Layout", "choices": list(LAYOUTS)}`.

The layout and size are saved with the receipt, so the preview, the print
and the receipt restored after a restart all match.

Text keeps its Unicode characters. Characters the font has no glyph for
(e.g. CJK, emoji) print as `�`; control characters become spaces.
`max_chars` counts the characters of your blocks' text (titles, lines,
labels, values) plus one per rule and per bar, not the header and footer. When a receipt is over the limit,
the block that crosses it is cut short, later blocks are dropped, and
`[truncated]` is printed.

Images are not available yet. They will be a new block kind alongside these,
so existing modules won't change.

## The context

| `ctx.` | Use |
|---|---|
| `limits`, `options` | The validated settings for this run |
| `now` | Run time (UTC `datetime`) |
| `http` | `get_json`, `get_text`, `request_json`, `post_form_json` |
| `cache` | `read_text(key)` / `write_text(key, text)`, e.g. key `"<id>/item.txt"`. For downloads: entries may be evicted |
| `store` | The module's saved state: `get(key, default=None)`, `set(key, value)`, `delete(key)`, `keys(prefix="")`, `items(prefix="")`, `key in store` |
| `emit(message, level)` | Log to the console; level `debug`, `info`, `warning`, `error` |
| `data_dir` | The data directory (for credentials files; prefer `cache` for downloads) |
| `trigger` | What started the run: `"schedule"` or `"manual"` (the Run button or the API) |
| `before_work` | The global before work time (`datetime.time`, Settings tab). A module with a daily print should offer a `bool` option labelled "Auto print before work" and, when it is on, make that print at the first run at or after this time |

Raise `gpta.module_api.FetchError` when source data is unavailable.
`gpta.http.FetchError` subclasses it, so `except FetchError` covers both your
errors and network failures. Any exception fails the run and triggers backoff.

## Skipping a run

A module that polls for changes can end a run without printing by raising
`Skip`:

```python
from gpta.module_api import Skip


def fetch(self, ctx):
    items = self._items(ctx)
    if ctx.trigger != "manual" and items == ctx.store.get("printed"):
        raise Skip("no changes")
    ctx.store.set("printed", items)
    return self._blocks(items)
```

To compare what would print rather than the source data, save
`fingerprint(blocks)`: a hash of the blocks that stays the same across GPTA
releases, since fields left at their defaults do not count. Do not hash
`dataclasses.astuple()` or `repr()` of a block; those change whenever a
release adds a field, and every list would print again after an upgrade.

A skipped run is a successful one: `ctx.store` writes are saved, the
previous receipt stays in the console, nothing is queued for the printer,
and the message is logged. Print anyway when `ctx.trigger` is `"manual"`, so
the Run button always produces a receipt. Returning an empty list is not a
skip; it prints "No content".

## Saved state

`ctx.store` is a key/value store that belongs to the module and survives
restarts (it lives in `data/gpta.db`). Keys are strings; values are anything
JSON can hold (dicts, lists, strings, numbers, booleans, `None`).

```python
def fetch(self, ctx):
    seen = set(ctx.store.get("seen", []))
    fresh = [item for item in self._items(ctx) if item["id"] not in seen]
    ctx.store.set("seen", sorted(seen | {item["id"] for item in fresh})[-500:])
    ctx.store.set("last_fetch", ctx.now.isoformat())
    return [Text(item["title"]) for item in fresh]
```

- **Writes are saved when the run succeeds.** They are held in memory during
  `fetch()` (the run reads its own writes back) and saved together once the
  receipt has been built. If `fetch()` raises or times out, or the receipt
  cannot be laid out, nothing is saved, so "seen" items are not lost to a
  failed run. What happens to the print afterwards (printer off, job
  cancelled or failed in the queue) does not undo them: the receipt still
  appears in the console.
- **Each module sees only its own keys.** Use prefixes for families of keys
  (`"seen/123"`) and `keys("seen/")` to list them.
- **Values are copies.** Change a value, then `set` it again.
- **Bounds:** keys up to 200 characters, values up to 1 MB of JSON, 10,000
  keys per module. Going over raises `StoreError`, which fails the run. Trim
  lists you append to, as above.
- **Changing a stored value's shape** is the module's job: read old values
  defensively (`get` with a default, check types), as with any saved data.

Use `store` for facts the module must not lose (what was printed, cursors,
counters), `cache` for anything that can be downloaded again, and options
for what the user chooses.

In tests, `ModuleContext(...)` without `store=` gets an empty in-memory
store; read it back with `ctx.store.get(...)` after calling `fetch(ctx)`.

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
| Store bound | 10,000 keys per module, 1 MB per value (`ctx.store`) |
| Store atomicity | A run's writes are saved together, only if the run succeeds |
| Log ring | 1000 entries in memory |
| Emit bound | 100 `ctx.emit` calls per run |
| Printer | One queue for every print: serialized writes, deadline, hourly cap, approval over 2.5 m, pause |
| Print queue | A module's newer scheduled receipt replaces its waiting one; jobs expire after 24 h |
| Failure backoff | Interval doubles after each failure, capped at 7 days |

The scheduler dispatches runs asynchronously and uses a monotonic clock, so a
slow module cannot delay other modules and wall-clock jumps do not cause run
storms.

## Module responsibilities

A module **must**:

- Return from `fetch()` promptly and avoid unbounded loops.
- Use `ctx.http` for all network access, so timeouts and body caps apply.
- Use `ctx.cache` for downloads and `ctx.store` for state rather than
  writing into `data/` directly.
- Join any thread it starts before returning. Do not spawn subprocesses.
- Treat input data as untrusted text. It is printed literally (blocks have no
  markup), so it cannot change the layout beyond its own length. Pass it
  through `markdown_blocks` only when it is meant to be Markdown.

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
