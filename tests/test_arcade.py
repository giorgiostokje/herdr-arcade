"""Tests for the arcade menu layout and game registry (no terminal needed)."""

import importlib
import os
import sys
import unittest

try:
    import tomllib
except ImportError:  # Python < 3.11 (system python3 is 3.9)
    tomllib = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import arcade


class MenuLayoutTest(unittest.TestCase):
    def test_full_popup_fits_everything(self):
        plan = arcade.menu_layout(93, 28)
        for name in ("title", "list", "agents", "help"):
            self.assertIsNotNone(plan[name], name)
        for name in ("title", "list", "agents"):
            y, x, h, w = plan[name]
            self.assertGreaterEqual(x, 0)
            self.assertLessEqual(x + w, 93, name)
            self.assertLessEqual(y + h, 27, name)  # clear of the help row
        # title, list and agents never overlap
        ty, _, th, _ = plan["title"]
        self.assertLessEqual(ty + th, plan["list"][0])
        ly, lx, lh, lw = plan["list"]
        self.assertLess(lx + lw, plan["agents"][1])
        # the list + agents block is centred
        ax, aw = plan["agents"][1], plan["agents"][3]
        self.assertLessEqual(abs(lx - (93 - (ax + aw))), 1)

    def test_degrades_by_size(self):
        narrow = arcade.menu_layout(50, 28)
        self.assertIsNone(narrow["agents"])
        self.assertIsNotNone(narrow["title"])
        short = arcade.menu_layout(77, 12)
        self.assertIsNone(short["title"])
        self.assertIsNotNone(short["list"])
        self.assertTrue(arcade.menu_layout(20, 5)["too_small"])

    def test_list_holds_every_game_at_popup_size(self):
        _, _, h, _ = arcade.menu_layout(93, 28)["list"]
        self.assertEqual(h, len(arcade.GAMES) + 4)

    def test_list_scrolls_when_short(self):
        plan = arcade.menu_layout(77, 12, n_games=11)
        self.assertIsNone(plan["title"])
        y, _, h, _ = plan["list"]
        self.assertLessEqual(y + h, 11)  # clear of the help row
        self.assertEqual(arcade.scroll_offset(0, 11, 5), 0)
        self.assertEqual(arcade.scroll_offset(6, 11, 5), 4)
        self.assertEqual(arcade.scroll_offset(10, 11, 5), 6)
        for sel in range(11):
            top = arcade.scroll_offset(sel, 11, 5)
            self.assertTrue(top <= sel < top + 5)

    def test_hotkeys_continue_past_nine_without_menu_keys(self):
        self.assertEqual(arcade.hotkey_label(0), "1")
        self.assertEqual(arcade.hotkey_label(8), "9")
        self.assertEqual(arcade.hotkey_label(9), "0")
        self.assertEqual(arcade.hotkey_label(10), "a")
        for used in "wsjklq?":
            self.assertNotIn(used, arcade.HOTKEYS)
        self.assertEqual(len(set(arcade.HOTKEYS)), len(arcade.HOTKEYS))
        for i in range(len(arcade.GAMES)):
            self.assertEqual(arcade.hotkey_index(ord(arcade.hotkey_label(i))), i)
        self.assertIsNone(arcade.hotkey_index(ord("q")))
        self.assertIsNone(arcade.hotkey_index(ord("z"), n_games=11))


class RegistryTest(unittest.TestCase):
    def test_games_ordered_by_release_year(self):
        years = [g[4] for g in arcade.GAMES]
        self.assertEqual(years, sorted(years))

    def test_menu_help_uses_game_help(self):
        for i, game in enumerate(arcade.GAMES):
            view = arcade.game_help(i)
            self.assertIn(str(game[4]), view.sections[0][0])

    def test_every_game_module_has_run(self):
        for gid, _, module, _, _ in arcade.GAMES:
            self.assertTrue(callable(importlib.import_module(module).run), gid)

    @unittest.skipIf(tomllib is None, "tomllib needs Python 3.11+")
    def test_manifest_has_pane_and_action_per_game(self):
        with open(os.path.join(ROOT, "herdr-plugin.toml"), "rb") as f:
            manifest = tomllib.load(f)
        panes = {p["id"]: p for p in manifest["panes"]}
        actions = {a["id"]: a for a in manifest["actions"]}
        self.assertIn("arcade", panes)
        for gid in arcade.GAME_IDS:
            self.assertIn(f"--game {gid}", " ".join(panes[gid]["command"]))
            self.assertEqual(actions[gid]["command"][-2:], ["game", gid])
        for aid in ("toggle", "open", "close"):
            self.assertIn(aid, actions)


if __name__ == "__main__":
    unittest.main()
