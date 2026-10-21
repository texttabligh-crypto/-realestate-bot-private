"""Offline regressions: manual intake -> public attributes -> editor -> caption."""
import ast
import hashlib
import re
import unittest
from collections import defaultdict
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).parent / "app"
NOTE = "پیش فروش-تحویل ۱۲ ماهه-پرداخت ۵۰ درصد اول"


def load_functions(filename, names, namespace):
    tree = ast.parse((ROOT / filename).read_text())
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), filename, "exec"), namespace)
    return namespace


class Cursor:
    def __init__(self):
        self.calls = []
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def execute(self, sql, params):
        # psycopg interprets all percent signs in parameterized SQL, even
        # inside SQL string literals. Values must remain separate parameters.
        invalid = re.search(r"%(?![%sbt])", sql.replace("%%", ""))
        if invalid:
            raise AssertionError("Invalid psycopg placeholder: " + sql)
        self.calls.append((sql, params))
    def fetchone(self):
        return (388,)


class Connection:
    def __init__(self, cur):
        self.cur = cur
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def cursor(self):
        return self.cur
    def commit(self):
        pass


class ManualDetailsTests(unittest.TestCase):
    def create(self, notes):
        cur = Cursor()
        ns = dict(hashlib=hashlib, SKIP="__SKIP__", FIELDS=[
            ("special_notes", "توضیحات ویژه / توضیح آزاد", True)],
            db_connect=lambda: Connection(cur), audit_event=lambda *a, **kw: None)
        load_functions("final_features.py", {"_create_manual", "_bool_text"}, ns)
        ns["_create_manual"]({"current_price_toman": 32_000_000_000,
                             "special_notes": notes}, 1)
        return [p[1:] for sql, p in cur.calls if "INSERT INTO property_attributes" in sql]

    def editor(self):
        return load_functions("channel_file_editor.py",
            {"_execute_db_changes", "_current_value", "_apply_draft_to_snapshot"},
            dict(hashlib=hashlib, deepcopy=deepcopy))

    def test_intake_notes_visible_and_editable(self):
        attrs = self.create(NOTE + "\nپنجره رو به حیاط")
        self.assertEqual([a[2] for a in attrs], [NOTE, "پنجره رو به حیاط"])
        snapshot = {"property": {}, "attributes": [
            dict(attribute_key=a[0], attribute_label=a[1], attribute_value=a[2],
                 display_order=a[3], id=i) for i, a in enumerate(attrs)]}
        self.assertEqual(self.editor()["_current_value"]("details", snapshot),
                         NOTE + "\nپنجره رو به حیاط")

    def test_skipped_and_empty_notes(self):
        for value in ("__SKIP__", None, "", "   "):
            self.assertEqual(self.create(value), [])

    def test_editor_sql_and_preview_match(self):
        ns = self.editor()
        for value in (NOTE, "پرداخت 50%\nمالک's note", ""):
            before = {"property": {}, "attributes": [
                dict(attribute_key="source_detail_old", attribute_value="قبلی")]}
            after = ns["_apply_draft_to_snapshot"](before, {"details": value})
            cur = Cursor()
            ns["_execute_db_changes"](cur, 388, before, after, {"details": value})
            self.assertEqual(cur.calls[0][1], (388, "source_detail_"))
            self.assertIn("starts_with(attribute_key, %s)", cur.calls[0][0])
            inserted = [p[2] for sql, p in cur.calls if "INSERT INTO" in sql]
            self.assertEqual(inserted, [a["attribute_value"] for a in after["attributes"]])
            self.assertEqual(before["attributes"][0]["attribute_value"], "قبلی")

    def test_intake_details_reach_public_caption(self):
        # Test the base formatter, not its runtime DB-dependent wrapper.
        tree = ast.parse((ROOT / "publisher.py").read_text())
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == "build_publish_text")
        ns = dict(get_publish_settings=lambda: defaultdict(str),
                  clean_publish_location=lambda x: x or "",
                  format_price=lambda x: "32 میلیارد تومان",
                  format_price_per_meter=lambda *a: "")
        exec(compile(ast.Module(body=[node], type_ignores=[]), "publisher.py", "exec"), ns)
        row = (388, "38832", "ولیعصر", "", 120, 2, "1", None,
               True, True, None, None, 32_000_000_000, 32,
               "#آپارتمان_32_33", "", "ACTIVE", "MANUAL")
        caption = ns["build_publish_text"](row, dynamic_attributes_override=self.create(NOTE))
        self.assertIn(NOTE, caption)
        self.assertEqual(caption.count(NOTE), 1)


if __name__ == "__main__":
    unittest.main()
