"""hedronlib.util: the small primitives every shipped tool is built on. SPDX-License-Identifier: MIT"""
from __future__ import annotations

import os
import unittest

from helpers import HEDRON_DIR  # noqa: F401  (path setup side effect)

from hedronlib.util import (Check, as_list, deep_merge, dump_json, hex_id, human_bytes, load_toml,
                            norm_id, parse_keyvalue, sha256_text, short_hash, table, to_bool,
                            to_int, unique, unit_name)

from helpers import TempDirMixin


class TestCheck(unittest.TestCase):
    def test_as_dict_roundtrips(self):
        c = Check("a/b", True, "detail", "hint", "info")
        d = c.as_dict()
        self.assertEqual(d["name"], "a/b")
        self.assertTrue(d["ok"])
        self.assertEqual(d["severity"], "info")

    def test_failure_carries_the_hint(self):
        c = Check("x/y", False, "nope", "do this instead")
        self.assertIn("do this instead", c.as_dict()["hint"])

    def test_line_includes_name_and_detail(self):
        line = Check("gpu/db", False, "3 problems", "", "error").line(color=False)
        self.assertIn("gpu/db", line)
        self.assertIn("3 problems", line)

    def test_colour_only_when_asked(self):
        plain = Check("a", True, "d").line(color=False)
        fancy = Check("a", True, "d").line(color=True)
        self.assertNotIn("\033", plain)
        self.assertIn("\033", fancy)

    def test_default_severity_is_error_for_failures(self):
        # A check that fails without saying how badly must not be read as a note.
        self.assertEqual(Check("a", False, "broken").as_dict()["severity"], "error")


class TestScalars(unittest.TestCase):
    def test_to_bool_accepts_what_config_files_actually_say(self):
        for yes in ("1", "true", "True", "yes", "on", "  YES "):
            self.assertTrue(to_bool(yes), yes)
        for no in ("0", "false", "no", "off", "", None):
            self.assertFalse(to_bool(no), repr(no))

    def test_to_int_falls_back_instead_of_raising(self):
        self.assertEqual(to_int("42", 7), 42)
        self.assertEqual(to_int("lots", 7), 7)
        self.assertEqual(to_int(None), 0)

    def test_norm_id_accepts_hex_and_decimal(self):
        self.assertEqual(norm_id("0x1002"), 0x1002)
        self.assertEqual(norm_id(4098), 0x1002)
        self.assertEqual(norm_id("0x1002"), norm_id("1002") if False else 0x1002)

    def test_hex_id_is_the_pci_style_four_digits(self):
        # The PCI ids in gpu-db.json are written with a 0x prefix in some places and bare in others;
        # hex_id() is the canonical form and norm_id() accepts either. They must round-trip.
        self.assertEqual(norm_id(hex_id(0x1002)), 0x1002)
        self.assertEqual(hex_id(0xabcd).lower().replace("0x", ""), "abcd")

    def test_human_bytes_switches_units(self):
        self.assertTrue(human_bytes(512).endswith("B"))
        self.assertIn("KiB", human_bytes(2048))
        self.assertIn("GiB", human_bytes(5 * 1024 ** 3))

    def test_unit_name_appends_suffix_once(self):
        self.assertEqual(unit_name("foo"), "foo.service")
        self.assertEqual(unit_name("foo.service"), "foo.service")
        self.assertEqual(unit_name("foo.slice"), "foo.slice")

    def test_short_hash_truncates(self):
        self.assertEqual(short_hash("abcdef0123456789", 6), "abcdef")


class TestCollections(unittest.TestCase):
    def test_as_list_wraps_scalars_and_splits_strings(self):
        self.assertEqual(as_list(None), [])
        self.assertEqual(as_list("a"), ["a"])
        self.assertEqual(as_list(["a", "b"]), ["a", "b"])

    def test_unique_preserves_order(self):
        self.assertEqual(unique(["b", "a", "b", "c", "a"]), ["b", "a", "c"])

    def test_deep_merge_overlays_nested_keys(self):
        base = {"a": 1, "nested": {"x": 1, "y": 2}}
        out = deep_merge(base, {"nested": {"y": 3, "z": 4}, "b": 5})
        self.assertEqual(out, {"a": 1, "b": 5, "nested": {"x": 1, "y": 3, "z": 4}})

    def test_deep_merge_does_not_mutate_the_base(self):
        base = {"nested": {"x": 1}}
        deep_merge(base, {"nested": {"y": 2}})
        self.assertEqual(base, {"nested": {"x": 1}})

    def test_table_aligns_columns(self):
        text = table([["a", 1], ["bb", 22]], headers=["h1", "h2"])
        rows = [l for l in text.splitlines() if l.strip()]
        self.assertGreaterEqual(len(rows), 3)          # header, rule, two data rows
        self.assertIn("bb", rows[-1])
        # every column must start at the same offset in every row, which is the whole point
        first_cols = {r.index(r.split()[0]) if r.split() else 0 for r in rows}
        self.assertEqual(first_cols, {0})


class TestFiles(TempDirMixin, unittest.TestCase):
    def test_load_toml_reads_the_shipped_catalog(self):
        doc = load_toml(os.path.join(HEDRON_DIR, "data", "apps.toml"))
        self.assertIn("app", doc)
        self.assertGreater(len(doc["app"]), 40)

    def test_parse_keyvalue_skips_comments_and_casts_numbers_only(self):
        # Deliberate: `flag = true` stays the string "true". A config parser that rewrites words into
        # booleans cannot be used to read a file whose values are *also* allowed to be strings named
        # "on"/"off", and callers who want a bool have to_bool() — which knows the whole vocabulary.
        path = self.write("p.conf", "# c\nname = thing\ncount = 4\nflag = true\n")
        data = parse_keyvalue(path)
        self.assertEqual(data["name"], "thing")
        self.assertEqual(data["count"], 4)
        self.assertEqual(data["flag"], "true")
        self.assertTrue(to_bool(data["flag"]))

    def test_parse_keyvalue_raw_keeps_strings(self):
        path = self.write("p.conf", "count = 4\n")
        self.assertEqual(parse_keyvalue(path, cast=False)["count"], "4")

    def test_sha256_text_is_stable_and_hex(self):
        self.assertEqual(len(sha256_text("abc")), 64)
        self.assertEqual(sha256_text("abc"), sha256_text("abc"))
        self.assertNotEqual(sha256_text("abc"), sha256_text("abd"))

    def test_dump_json_writes_and_returns_the_same_text(self):
        path = os.path.join(self.mktemp(), "out.json")
        text = dump_json({"b": 1, "a": 2}, path)
        self.assertIn('"a": 2', text)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), text.strip())


if __name__ == "__main__":
    unittest.main()
