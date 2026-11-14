import tempfile
import threading
import unittest
from pathlib import Path

from gpta import storage
from gpta.config import ConfigStore
from gpta.logbus import LogBus
from gpta.module_api import (
    Document,
    FetchError,
    Heading,
    Module,
    ModuleStore,
    Rule,
    StoreError,
    Text,
    markdown_blocks,
)
from gpta.renderer import build_receipt, render_image
from gpta.service import Service
from gpta.storage import Database


class ModuleStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "gpta.db")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def run_with(self, module_id="a", commit=True, **values):
        store = ModuleStore(self.db, module_id)
        for key, value in values.items():
            store.set(key, value)
        store.commit() if commit else store.discard()

    def test_values_round_trip_after_commit(self):
        self.run_with(seen=[1, 2], cursor={"page": 3, "done": False}, name="é")
        store = ModuleStore(self.db, "a")
        self.assertEqual(store.get("seen"), [1, 2])
        self.assertEqual(store.get("cursor"), {"page": 3, "done": False})
        self.assertEqual(store.get("name"), "é")
        self.assertEqual(store.get("missing", 7), 7)
        self.assertIn("seen", store)
        self.assertNotIn("missing", store)

    def test_run_sees_its_own_writes_before_commit(self):
        self.run_with(count=1, old=True)
        store = ModuleStore(self.db, "a")
        store.set("count", 2)
        store.set("new", "x")
        store.delete("old")
        self.assertEqual(store.get("count"), 2)
        self.assertEqual(store.keys(), ["count", "new"])
        self.assertEqual(ModuleStore(self.db, "a").get("count"), 1)  # not saved yet
        store.commit()
        self.assertEqual(
            ModuleStore(self.db, "a").items(), [("count", 2), ("new", "x")]
        )

    def test_discard_saves_nothing_and_closes(self):
        self.run_with(commit=False, count=1)
        store = ModuleStore(self.db, "a")
        self.assertIsNone(store.get("count"))
        store.discard()
        with self.assertRaises(StoreError):
            store.set("count", 2)

    def test_modules_are_isolated(self):
        self.run_with("a", key="from a")
        self.run_with("b", key="from b")
        self.assertEqual(ModuleStore(self.db, "a").get("key"), "from a")
        self.assertEqual(ModuleStore(self.db, "b").keys(), ["key"])

    def test_prefix_is_literal(self):
        self.run_with(**{"seen/1": 1, "seen/2": 2, "seen%": 3, "other": 4})
        store = ModuleStore(self.db, "a")
        self.assertEqual(store.keys("seen/"), ["seen/1", "seen/2"])
        self.assertEqual(store.keys("seen%"), ["seen%"])

    def test_returned_values_are_copies(self):
        self.run_with(items=[1])
        store = ModuleStore(self.db, "a")
        store.get("items").append(2)
        self.assertEqual(store.get("items"), [1])

    def test_invalid_keys_and_values_raise(self):
        store = ModuleStore(self.db, "a")
        for key in ("", 3, "k" * (storage.MAX_KEY_CHARS + 1)):
            with self.subTest(key=key), self.assertRaises(StoreError):
                store.set(key, 1)
        for value in (object(), float("nan"), "x" * (storage.MAX_VALUE_BYTES + 1)):
            with self.assertRaises(StoreError):
                store.set("k", value)

    def test_key_count_is_bounded(self):
        original = storage.MAX_KEYS_PER_MODULE
        storage.MAX_KEYS_PER_MODULE = 3
        try:
            self.run_with(a=1, b=2, c=3)
            store = ModuleStore(self.db, "a")
            store.set("d", 4)
            with self.assertRaises(StoreError):
                store.commit()
            self.assertEqual(ModuleStore(self.db, "a").keys(), ["a", "b", "c"])
        finally:
            storage.MAX_KEYS_PER_MODULE = original

    def test_data_survives_reopening(self):
        self.run_with(count=5)
        self.db.close()
        self.db = Database(Path(self.tmp.name) / "gpta.db")
        self.assertEqual(ModuleStore(self.db, "a").get("count"), 5)

    def test_concurrent_commits(self):
        def work(n):
            self.run_with("a", **{f"k{n}": n})

        threads = [threading.Thread(target=work, args=(n,)) for n in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(ModuleStore(self.db, "a").keys()), 20)

    def test_newer_schema_is_not_touched(self):
        path = Path(self.tmp.name) / "future.db"
        db = Database(path)
        with db.transaction() as conn:
            conn.execute("PRAGMA user_version=99")
        db.close()
        with self.assertLogs("gpta.storage", "WARNING"):
            Database(path).close()
        self.assertEqual(len(list(path.parent.glob("future.db.corrupt-*"))), 1)


class CounterModule(Module):
    """Counts its runs in ``ctx.store`` and prints Markdown in two columns."""

    id = "counter"
    name = "Counter"
    description = "counts runs"
    defaults = {
        "enabled": False,
        "interval_seconds": 3600,
        "limits": {"max_chars": 2000},
        "options": {},
    }
    fail = False

    def fetch(self, ctx):
        count = ctx.store.get("count", 0) + 1
        ctx.store.set("count", count)
        if self.fail:
            raise FetchError("down")
        body = markdown_blocks(f"Run **{count}**\n\n- one\n- two\n\n" + "word " * 80)
        return Document(
            [Heading("Counter"), Rule(heavy=True), *body], layout="double", size="small"
        )


class ModuleRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.module = CounterModule()
        registry = {"counter": self.module}
        config = ConfigStore(Path(self.tmp.name) / "config.json", registry)
        config.load()
        self.service = Service(config, registry, LogBus(), fetch_timeout=2.0)

    def tearDown(self):
        self.service.stop()
        self.tmp.cleanup()

    def count(self):
        return ModuleStore(self.service.db, "counter").get("count")

    def test_store_is_saved_only_when_the_run_succeeds(self):
        self.assertTrue(self.service.run_module("counter")["ok"])
        self.assertTrue(self.service.run_module("counter")["ok"])
        self.assertEqual(self.count(), 2)
        self.module.fail = True
        self.assertFalse(self.service.run_module("counter")["ok"])
        self.assertEqual(self.count(), 2)
        self.module.fail = False
        self.service.run_module("counter")
        self.assertEqual(self.count(), 3)
        self.assertIn("Run 3", self.service.output_text("counter"))

    def test_document_sets_layout_size_and_lead(self):
        self.service.run_module("counter")
        receipt = self.service.outputs.get("counter").receipt
        self.assertEqual(
            (receipt.layout, receipt.size, receipt.lead), ("double", "small", 2)
        )
        # The stored receipt renders in columns without being told again.
        plain = render_image(receipt, 576, layout="single")
        self.assertLess(render_image(receipt, 576).height, plain.height)
        self.assertTrue(self.service.output_png("counter").startswith(b"\x89PNG"))


class DocumentTests(unittest.TestCase):
    def test_invalid_documents_raise(self):
        for kwargs in ({"layout": "quad"}, {"size": "giant"}, {"lead": -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Document([Text("x")], **kwargs)
        with self.assertRaises(TypeError):
            Document("text")
        with self.assertRaises(TypeError):
            build_receipt("M", Document(["not a block"]))

    def test_lead_defaults_to_the_header(self):
        blocks = [Heading("h"), Rule(heavy=True), Text("a"), Text("b")]
        self.assertEqual(build_receipt("M", Document(blocks)).lead, 2)
        self.assertEqual(build_receipt("M", Document(blocks, lead=0)).lead, 0)
        self.assertEqual(build_receipt("M", Document(blocks, lead=9)).lead, 4)
        self.assertEqual(build_receipt("M", [Text("a")]).lead, 0)


if __name__ == "__main__":
    unittest.main()
