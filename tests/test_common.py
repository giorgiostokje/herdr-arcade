"""Tests for the shared screen helpers in common.py (no terminal needed)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import curses

import common


class LayoutHelpersTest(unittest.TestCase):
    def test_center_origin_centres_below_header(self):
        y, x = common.center_origin(93, 28, 89, 24)
        self.assertEqual(x, 2)
        self.assertEqual(y, 1 + (27 - 24) // 2)
        self.assertEqual(common.center_origin(10, 5, 40, 40), (1, 0))

    def test_side_width_tiers(self):
        self.assertEqual(common.side_width(93, 58), common.SIDE_W)
        self.assertEqual(common.side_width(80, 58), 21)
        self.assertEqual(common.side_width(77, 58), 0)


class ControlsTest(unittest.TestCase):
    CONTROLS = [("arrows", "move"), ("space", "fast draw, a long action text"),
                ("?", "help"), ("q", "menu")]

    def test_lines_fit_and_align(self):
        lines = common.controls_lines(self.CONTROLS, 20)
        self.assertTrue(all(len(line) <= 20 for line in lines))
        self.assertTrue(lines[0].startswith("arrows move"))
        # wrapped continuation lines are indented under the action column
        self.assertTrue(any(line.startswith(" " * 7) for line in lines))
        self.assertEqual(common.controls_height(self.CONTROLS, 22), len(lines) + 2)

    def test_hint_mentions_every_key(self):
        hint = common.controls_hint(self.CONTROLS)
        for keys, _ in self.CONTROLS:
            self.assertIn(keys, hint)


class HelpOverlayTest(unittest.TestCase):
    SECTIONS = [("Goal", "Do the thing.\n- one\n- two"),
                ("Scoring", " ".join(["word"] * 200))]

    def test_wrap_keeps_bullets_and_width(self):
        lines = common.help_lines(self.SECTIONS, 30)
        self.assertEqual(lines[0], ("Goal", True))
        self.assertIn(("- one", False), lines)
        self.assertTrue(all(len(t) <= 30 for t, _ in lines))

    def test_keys_scroll_then_close(self):
        h = common.HelpOverlay("X", self.SECTIONS)
        self.assertFalse(h.handle_key(ord("j")))  # closed: not consumed
        h.open()
        h._view, h._total = 5, 40
        self.assertTrue(h.handle_key(curses.KEY_DOWN))
        self.assertEqual(h.offset, 1)
        h.handle_key(curses.KEY_UP)
        h.handle_key(curses.KEY_UP)
        self.assertEqual(h.offset, 0)
        self.assertFalse(h.handle_key(curses.KEY_RESIZE))
        self.assertTrue(h.is_open)
        self.assertTrue(h.handle_key(ord("x")))
        self.assertFalse(h.is_open)

    def test_box_fits_small_and_popup_screens(self):
        h = common.HelpOverlay("X", self.SECTIONS)
        for cols, rows in ((93, 28), (77, 24), (30, 10)):
            y, x, hh, w = h.box(cols, rows)
            self.assertGreaterEqual(y, 1)
            self.assertLessEqual(x + w, cols)
            self.assertLessEqual(y + hh, rows)


if __name__ == "__main__":
    unittest.main()
