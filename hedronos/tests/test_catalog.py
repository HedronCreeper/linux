"""data/apps.toml through hedronlib.catalog: the app list the store and installer share.
SPDX-License-Identifier: MIT"""
from __future__ import annotations

import os
import unittest

from helpers import DATA_DIR

from hedronlib.catalog import Catalog

APPS = os.path.join(DATA_DIR, "apps.toml")


class TestCatalog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = Catalog.load(APPS)

    def test_catalog_validates_clean(self):
        self.assertEqual(self.cat.validate(), [])

    def test_has_a_useful_number_of_apps(self):
        self.assertGreaterEqual(len(self.cat.apps), 40)

    def test_apps_is_a_dict_keyed_by_id(self):
        # Every consumer (hstore, hedron-setup, the installer) looks apps up by id, so the shape of
        # this container is part of the contract even though no schema file says so.
        self.assertEqual(set(self.cat.apps) , {a.id for a in self.cat.apps.values()})

    def test_every_app_has_the_fields_the_store_renders(self):
        for app in self.cat.apps.values():
            for field in ("id", "name", "summary", "category"):
                self.assertTrue(getattr(app, field, None), "%s is missing %s" % (app.id, field))

    def test_categories_cover_the_catalog(self):
        cats = {a.category for a in self.cat.apps.values()}
        declared = {c["id"] if isinstance(c, dict) else c for c in self.cat.categories}
        self.assertGreaterEqual(len(declared), 5)
        # An app in an undeclared category disappears from the picker: the grid is built by
        # category, so the reference has to go one way and be checked the other.
        self.assertEqual(sorted(cats - declared), [])

    def test_steam_is_locked_and_not_installable(self):
        locked = self.cat.locked_ids()
        self.assertIn("steam", locked)

    def test_bundles_expand_to_real_apps(self):
        names = set(self.cat.apps)
        for bundle in self.cat.bundles:
            ids = self.cat.bundle(bundle)
            self.assertTrue(ids, "bundle %s expands to nothing" % bundle)
            unknown = [i for i in ids if i not in names]
            self.assertEqual(unknown, [], "bundle %s references unknown apps" % bundle)

    def test_expand_buckets_by_source_and_reports_nothing_broken(self):
        # expand() is what hstore and the installer both use to turn "the user picked these" into
        # "run these commands", so its buckets are the contract: five provider sources plus the two
        # that must always be empty in a shipping catalog.
        valid = {"pacman", "flatpak", "appimage", "hedron", "aur", "missing_provider", "unknown_refs"}
        for label, ids in (("a slice", list(self.cat.apps)[:12]),
                          ("every app", list(self.cat.apps)),
                          ("every bundle", [i for b in self.cat.bundles for i in self.cat.bundle(b)])):
            expanded = self.cat.expand(ids)
            self.assertTrue(set(expanded) <= valid, "%s: unexpected bucket %s" % (label, set(expanded) - valid))
            self.assertEqual(expanded.get("unknown_refs", []), [],
                             "%s: an app references a provider that is not in the catalog" % label)
            self.assertEqual(expanded.get("missing_provider", []), [],
                             "%s: an app resolves to no package at all" % label)
            for group, members in expanded.items():
                self.assertEqual(len(members), len(set(members)), "duplicate entry in source %s" % group)

    def test_every_app_resolves_to_at_least_one_source(self):
        # An app with no back end is a button that cannot work.
        empty = [a.id for a in self.cat.apps.values()
                 if not any(getattr(a, src, None) for src in ("pacman", "flatpak", "appimage", "hedron", "aur"))]
        self.assertEqual(empty, [])

    def test_gaming_list_is_not_empty(self):
        self.assertGreaterEqual(len(self.cat.gaming()), 5)

    def test_size_estimate_is_positive_mib(self):
        ids = list(self.cat.apps)[:5]
        self.assertGreater(self.cat.size_estimate(ids), 0)

    def test_post_install_commands_name_tools_we_ship(self):
        # The store runs these after installing. A command here that points at a tool no package
        # provides is not a cosmetic bug — the install reports a failure the user cannot fix, so
        # this test is the reason those strings are data and not prose.
        import os
        bins = set()
        for pkg in os.listdir(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "packages")):
            bindir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                  "packages", pkg, "src", "usr", "bin")
            if os.path.isdir(bindir):
                bins |= set(os.listdir(bindir))
        external = {"systemctl", "fwupdmgr", "pacman", "flatpak", "sh", "bash"}
        for app in self.cat.apps.values():
            for cmd in app.post_install or []:
                self.assertTrue(cmd.strip(), "empty command for %s" % app.id)
                self.assertFalse(cmd.startswith("#"), "comment as a command for %s" % app.id)
                tok = cmd.split()[0]
                self.assertIn(tok, bins | external,
                              "%s: post_install runs %r, which no package provides" % (app.id, tok))

    def test_default_selection_for_every_profile(self):
        for profile in ("gamerte", "desktop", "handheld", "studio", "core"):
            self.assertIsInstance(self.cat.default_selection(profile), list)

    def test_locked_apps_are_still_visible_but_marked(self):
        # The picker shows Steam and says it is not installable through us; hiding it would make
        # people think the edition has no Steam at all.
        visible = {a.id: a for a in self.cat.visible()}
        self.assertIn("steam", visible)
        self.assertTrue(visible["steam"].locked)

    def test_tiers_are_the_ones_the_ui_colours(self):
        from hedronlib.catalog import TIERS
        seen = {a.tier for a in self.cat.apps.values() if a.tier}
        self.assertTrue(seen <= set(TIERS), "unknown tier(s): %s" % sorted(seen - set(TIERS)))


if __name__ == "__main__":
    unittest.main()
