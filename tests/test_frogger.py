"""Unit tests for the pure game logic in frogger.py (no terminal needed)."""

import json
import os
import random
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import frogger
from common import AgentSnapshot
from frogger import (
    BAY_X,
    CLEAR_DURATION,
    COLS,
    DEATH_DURATION,
    FLY_POINTS,
    HOME_POINTS,
    LANES,
    LEVEL_POINTS,
    LIVES,
    MEDIAN_ROW,
    ROW_POINTS,
    SAVE_VERSION,
    START_ROW,
    START_X,
    TIME_BONUS,
    TIME_LIMIT,
    Game,
    handle_key,
    layout_plan,
)


def new_game(seed=1):
    return Game(random.Random(seed))


def solo(row, kind, speed, length, dive=()):
    """Patch one lane down to a single object (index 0) for isolation."""
    return mock.patch.dict(LANES, {row: (kind, speed, ((0, length),), dive)})


def place(game, row, fx):
    game.row = row
    game.fx = float(fx)


class HopTest(unittest.TestCase):
    def test_spawn(self):
        g = new_game()
        self.assertEqual((g.row, g.fx), (START_ROW, START_X))
        self.assertEqual(g.lives, LIVES)
        self.assertEqual(g.time_left, TIME_LIMIT)

    def test_hops_move_one_cell(self):
        g = new_game()
        place(g, MEDIAN_ROW, 5)
        self.assertTrue(g.hop(-1, 0))
        self.assertEqual(g.fx, 4)
        self.assertTrue(g.hop(1, 0))
        self.assertTrue(g.hop(1, 0))
        self.assertEqual(g.fx, 6)
        with solo(7, "truck", -0.9, 3):
            g.set_object_x(7, 0, 12)
            self.assertTrue(g.hop(0, 1))
            self.assertEqual(g.row, 7)
            self.assertIsNone(g.phase)

    def test_bounds(self):
        g = new_game()
        self.assertFalse(g.hop(0, 1))           # below the start bank
        self.assertEqual(g.row, START_ROW)
        place(g, START_ROW, 0)
        self.assertFalse(g.hop(-1, 0))
        self.assertEqual(g.fx, 0)
        place(g, START_ROW, COLS - 1)
        self.assertFalse(g.hop(1, 0))
        self.assertEqual(g.fx, COLS - 1)

    def test_partial_hop_clamps_to_the_field(self):
        g = new_game()
        place(g, MEDIAN_ROW, 0.4)
        self.assertTrue(g.hop(-1, 0))
        self.assertEqual(g.fx, 0)

    def test_keys(self):
        g = new_game()
        place(g, MEDIAN_ROW, 5)
        for key in (frogger.curses.KEY_LEFT, ord("h"), ord("a"), ord("A")):
            place(g, MEDIAN_ROW, 5)
            self.assertTrue(handle_key(g, key))
            self.assertEqual(g.fx, 4)
        for key in (frogger.curses.KEY_RIGHT, ord("l"), ord("d")):
            place(g, MEDIAN_ROW, 5)
            handle_key(g, key)
            self.assertEqual(g.fx, 6)
        with solo(5, "log", 0.0, 6):
            g.set_object_x(5, 0, 2)
            for key in (frogger.curses.KEY_UP, ord("k"), ord("w")):
                place(g, MEDIAN_ROW, 5)
                g.best_row = MEDIAN_ROW
                handle_key(g, key)
                self.assertEqual(g.row, 5)
        handle_key(g, ord("p"))
        self.assertTrue(g.paused)
        self.assertFalse(handle_key(g, ord("s")))


class VehicleTest(unittest.TestCase):
    def test_hop_into_a_car(self):
        g = new_game()
        with solo(11, "car2", -1.4, 1):
            g.set_object_x(11, 0, START_X)
            g.hop(0, -1)
        self.assertEqual(g.phase.kind, "dying")
        self.assertEqual(g.phase.cause, "vehicle")

    def test_car_drives_into_frog(self):
        g = new_game()
        with solo(11, "car2", -1.4, 1):
            place(g, 11, 5)
            g.set_object_x(11, 0, 9)
            for _ in range(200):
                g.update(0.02)
                if g.phase:
                    break
        self.assertEqual(g.phase.cause, "vehicle")
        self.assertEqual(g.lives, LIVES)  # decremented when the phase ends

    def test_fast_vehicle_cannot_tunnel(self):
        g = new_game()
        g.level = 5                      # 2x speed: 7.2 cells/s
        with solo(8, "racer", 3.6, 1):
            place(g, 8, 8)
            g.set_object_x(8, 0, 6.5)
            v = g.lane_speed(8)
            x1 = 6.5 + v * 0.4
            # Neither the start nor the end position touches the frog...
            self.assertFalse(g._overlaps(6.5, 7.5))
            self.assertFalse(g._overlaps(x1, x1 + 1))
            # ...but the car passed over it during the step.
            g.update(0.4)
        self.assertIsNotNone(g.phase)
        self.assertEqual(g.phase.cause, "vehicle")

    def test_speed_cap_keeps_sweep_exact(self):
        fastest = max(abs(lane[1]) for lane in LANES.values())
        for level in (1, 5, 10, 50, 1000):
            self.assertLess(fastest * frogger.speed_mult(level) * frogger.MAX_DT,
                            frogger.MARGIN)

    def test_near_miss_survives(self):
        g = new_game()
        with solo(8, "racer", 3.6, 1):
            place(g, 8, 8)
            g.set_object_x(8, 0, 10)     # already past, moving away
            g.update(0.3)
        self.assertIsNone(g.phase)


class RiverTest(unittest.TestCase):
    def test_log_carries_frog(self):
        g = new_game()
        with solo(4, "log", 1.0, 4):
            g.set_object_x(4, 0, 5)
            place(g, 4, 6)
            for _ in range(5):
                g.update(0.1)
        self.assertIsNone(g.phase)
        self.assertAlmostEqual(g.fx, 6.5, places=6)
        self.assertEqual(int(round(g.fx * 2)), 13)  # drawn on the nearest column

    def test_hop_onto_log(self):
        g = new_game()
        with solo(5, "log", 1.0, 4):
            g.set_object_x(5, 0, 5)
            place(g, MEDIAN_ROW, 6)
            g.hop(0, -1)
        self.assertIsNone(g.phase)
        self.assertEqual(g.row, 5)

    def test_water_death(self):
        g = new_game()
        with solo(5, "log", 1.0, 3):
            g.set_object_x(5, 0, 12)
            place(g, MEDIAN_ROW, 5)
            g.hop(0, -1)
        self.assertEqual(g.phase.cause, "water")

    def test_log_drifts_away_from_frog(self):
        g = new_game()
        with solo(4, "log", 1.0, 2):
            g.set_object_x(4, 0, 5)
            place(g, 4, 5)
            g.update(0.1)
            self.assertIsNone(g.phase)
            g.hop(-1, 0)                 # hop off the log into the water
        self.assertEqual(g.phase.cause, "water")

    def test_carried_off_edge(self):
        g = new_game()
        with solo(3, "log", 1.8, 6):
            g.set_object_x(3, 0, 12)
            place(g, 3, COLS - 1)
            for _ in range(100):
                g.update(0.05)
                if g.phase:
                    break
        self.assertEqual(g.phase.cause, "edge")
        self.assertGreater(g.fx + 0.5, COLS)

    def test_diving_turtles_become_unsafe(self):
        g = new_game()
        with solo(2, "turtle", 0.0, 2, dive=(0,)):
            g.set_object_x(2, 0, 5)
            place(g, 2, 5)
            g.clock = 0.0
            self.assertEqual(g.turtle_state(2, 0), "up")
            g.update(0.1)
            self.assertIsNone(g.phase)
            g.clock = frogger.DIVE_UP + 0.1
            self.assertEqual(g.turtle_state(2, 0), "sinking")
            g.update(0.01)
            self.assertIsNone(g.phase)           # sinking is still safe
            g.clock = frogger.DIVE_SINK + 0.1
            self.assertEqual(g.turtle_state(2, 0), "under")
            g.update(0.01)
        self.assertEqual(g.phase.cause, "water")

    def test_non_diving_turtles_stay_up(self):
        g = new_game()
        for t in range(0, 60):
            g.clock = t / 10
            self.assertEqual(g.turtle_state(2, 0), "up")
        states = set()
        for t in range(0, 60):
            g.clock = t / 10
            states.add(g.turtle_state(2, 2))
        self.assertEqual(states, {"up", "sinking", "under", "rising"})


class HomeTest(unittest.TestCase):
    def test_fill_bay_scores_and_respawns(self):
        g = new_game()
        place(g, 1, BAY_X[2])
        g.time_left = 12.7
        g.score = 0
        g.hop(0, -1)
        self.assertIsNone(g.phase)
        self.assertTrue(g.homes[2])
        self.assertEqual(g.score, HOME_POINTS + 12 * TIME_BONUS)
        self.assertEqual((g.row, g.fx), (START_ROW, START_X))
        self.assertEqual(g.time_left, TIME_LIMIT)
        self.assertEqual(g.best_row, START_ROW)

    def test_occupied_bay_kills(self):
        g = new_game()
        g.homes[1] = True
        place(g, 1, BAY_X[1] + 1)
        g.hop(0, -1)
        self.assertEqual(g.phase.cause, "occupied")

    def test_bank_between_bays_kills(self):
        g = new_game()
        place(g, 1, BAY_X[0] + 2)       # the gap after bay 0
        g.hop(0, -1)
        self.assertEqual(g.phase.cause, "bank")

    def test_fly_bonus(self):
        g = new_game()
        g.fly = 3
        place(g, 1, BAY_X[3])
        g.time_left = 0.5
        g.score = 0
        g.hop(0, -1)
        self.assertEqual(g.score, HOME_POINTS + FLY_POINTS)
        self.assertIsNone(g.fly)

    def test_fly_appears_in_empty_bay(self):
        g = new_game()
        g.homes = [True, True, False, True, True]
        g.fly_timer = 0.01
        g.update(0.02)
        self.assertEqual(g.fly, 2)

    def test_level_advance_on_five_homes(self):
        g = new_game()
        g.homes = [True, True, True, True, False]
        g.score = 0
        speed = g.lane_speed(8)
        place(g, 1, BAY_X[4])
        g.time_left = 10.2
        g.hop(0, -1)
        self.assertEqual(g.score, HOME_POINTS + 10 * TIME_BONUS + LEVEL_POINTS)
        self.assertEqual(g.phase.kind, "cleared")
        g.update(CLEAR_DURATION / 2)
        self.assertEqual(g.level, 1)
        for _ in range(10):
            g.update(0.1)
        self.assertIsNone(g.phase)
        self.assertEqual(g.level, 2)
        self.assertEqual(g.homes, [False] * 5)
        self.assertGreater(g.lane_speed(8), speed)
        self.assertEqual((g.row, g.fx), (START_ROW, START_X))
        self.assertEqual(g.lives, LIVES)


class ScoringTest(unittest.TestCase):
    def test_forward_row_scores_once(self):
        g = new_game()
        with solo(11, "car2", 0.0, 1), solo(10, "car", 0.0, 1):
            g.set_object_x(11, 0, 1)
            g.set_object_x(10, 0, 1)
            g.hop(0, -1)
            self.assertEqual(g.score, ROW_POINTS)
            g.hop(0, 1)
            g.hop(0, -1)
            self.assertEqual(g.score, ROW_POINTS)
            g.hop(0, -1)
            self.assertEqual(g.score, 2 * ROW_POINTS)
            g.hop(-1, 0)
            self.assertEqual(g.score, 2 * ROW_POINTS)
        self.assertIsNone(g.phase)

    def test_furthest_row_resets_each_life(self):
        g = new_game()
        with solo(11, "car2", 0.0, 1):
            g.set_object_x(11, 0, 1)
            g.hop(0, -1)
            g._die("test")
            g.finish_phase()
            g.hop(0, -1)
        self.assertEqual(g.score, 2 * ROW_POINTS)


class LifeTest(unittest.TestCase):
    def test_timer_expiry(self):
        g = new_game()
        for _ in range(1000):
            g.update(0.1)
            if g.phase:
                break
        self.assertEqual(g.phase.cause, "timeout")
        self.assertEqual(g.time_left, 0)

    def test_pause_freezes_timer(self):
        g = new_game()
        g.paused = True
        self.assertFalse(g.update(5))
        self.assertEqual(g.time_left, TIME_LIMIT)

    def test_death_phase_lasts_about_a_second(self):
        g = new_game()
        place(g, 5, 3)
        g._die("water")
        self.assertFalse(g.hop(0, 1))           # no input while dying
        g.update(DEATH_DURATION * 0.6)
        self.assertIsNotNone(g.phase)
        g.update(DEATH_DURATION * 0.5)
        self.assertIsNone(g.phase)
        self.assertEqual(g.lives, LIVES - 1)
        self.assertEqual((g.row, g.fx), (START_ROW, START_X))

    def test_game_over_after_last_life(self):
        g = new_game()
        for n in range(LIVES):
            self.assertFalse(g.game_over)
            g._die("water")
            g.finish_phase()
        self.assertTrue(g.game_over)
        self.assertEqual(g.lives, 0)
        self.assertFalse(g.hop(0, -1))
        self.assertFalse(g.update(0.1))
        self.assertFalse(handle_key(g, ord("w")))

    def test_reset(self):
        g = new_game()
        g.score = 500
        g.game_over = True
        g.reset()
        self.assertFalse(g.game_over)
        self.assertEqual((g.score, g.lives, g.level), (0, LIVES, 1))


class SaveTest(unittest.TestCase):
    def played(self):
        g = new_game(7)
        g.score = 1234
        g.level = 2
        g.lives = 2
        g.homes = [True, False, True, False, False]
        g.fly = 3
        with solo(4, "log", 1.0, 4):
            pass
        for _ in range(30):
            g.update(0.05)
        place(g, MEDIAN_ROW, 4)
        g.best_row = MEDIAN_ROW
        return g

    def test_round_trip_including_rng(self):
        g = self.played()
        data = json.loads(json.dumps(g.to_save()))
        r = Game.from_save(data)
        self.assertIsNotNone(r)
        self.assertEqual(r.to_save(), g.to_save())
        self.assertEqual(r.rng.random(), g.rng.random())
        # Deterministic continuation.
        for game in (g, r):
            for _ in range(200):
                game.update(0.05)
        self.assertEqual(r.to_save(), g.to_save())

    def test_corrupt_or_incompatible(self):
        good = self.played().to_save()
        self.assertIsNotNone(Game.from_save(json.loads(json.dumps(good))))
        bad = [None, {}, [], "x"]
        for key, value in (("version", SAVE_VERSION + 1), ("rng", [1, 2]),
                           ("homes", [True] * 5), ("homes", [1, 0, 0, 0, 0]),
                           ("lives", 0), ("row", 99), ("fx", "a"),
                           ("offsets", {"1": 0.0}), ("fly", 0),
                           ("time_left", float("nan")), ("best_row", START_ROW + 1)):
            d = dict(good)
            d[key] = value
            bad.append(d)
        d = dict(good)
        del d["score"]
        bad.append(d)
        for data in bad:
            self.assertIsNone(Game.from_save(data), repr(data)[:80])

    def test_phase_resolved_before_save(self):
        g = new_game()
        g._die("water")
        g.finish_phase()
        self.assertIsNone(g.phase)
        r = Game.from_save(json.loads(json.dumps(g.to_save())))
        self.assertEqual(r.lives, LIVES - 1)


REGIONS = ("field", "stats", "controls", "agents", "hint")


def block(plan):
    """Bounding box (y, x, h, w) of the field plus side column."""
    boxes = [plan[n] for n in ("field", "stats", "controls", "agents") if plan[n]]
    y0 = min(b[0] for b in boxes)
    x0 = min(b[1] for b in boxes)
    y1 = max(b[0] + b[2] for b in boxes)
    x1 = max(b[1] + b[3] for b in boxes)
    return y0, x0, y1 - y0, x1 - x0


class LayoutTest(unittest.TestCase):
    def check_fits(self, plan, cols, rows):
        for name in REGIONS:
            box = plan[name]
            if box is None:
                continue
            y, x, h, w = box
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 1)
            self.assertLessEqual(x + w, cols, name)
            self.assertLessEqual(y + h, rows, name)

    def check_centred(self, plan, cols, rows):
        y, x, h, w = block(plan)
        bottom = rows - (1 if plan["hint"] else 0)
        self.assertLessEqual(abs(x - (cols - x - w)), 1, (cols, rows, plan))
        self.assertLessEqual(abs((y - 1) - (bottom - y - h)), 1, (cols, rows, plan))

    def test_full_at_popup_size(self):
        plan = layout_plan(93, 28)
        self.assertEqual(plan["tier"], "full")
        self.check_fits(plan, 93, 28)
        self.check_centred(plan, 93, 28)
        fy, fx, fh, fw = plan["field"]
        self.assertEqual((fh, fw), (frogger.FIELD_H, COLS * 2 + 2))
        sy, sx, sh, sw = plan["stats"]
        cy, cx, ch, cw = plan["controls"]
        ay, ax, ah, aw = plan["agents"]
        self.assertEqual(sw, 30)
        self.assertEqual((cw, aw), (30, 30))
        self.assertEqual(sx, fx + fw + 1)
        self.assertEqual(sy, fy)                 # side column starts level with the field
        self.assertEqual(cy, sy + sh)            # stats, controls, agents stacked
        self.assertEqual(ay, cy + ch)
        self.assertEqual(ch, frogger.controls_height(frogger.CONTROLS, 30))
        self.assertGreaterEqual(ah, frogger.AGENTS_MIN_H)
        self.assertIsNone(plan["hint"])

    def test_full_at_77x24(self):
        plan = layout_plan(77, 24)
        self.assertEqual(plan["tier"], "full")
        self.check_fits(plan, 77, 24)
        self.check_centred(plan, 77, 24)
        self.assertEqual(plan["stats"][3], 30)
        self.assertIsNotNone(plan["controls"])
        self.assertIsNotNone(plan["agents"])

    def test_degrading_tiers(self):
        self.assertEqual(layout_plan(77, 19)["tier"], "stats")
        self.assertIsNone(layout_plan(77, 19)["agents"])
        self.assertEqual(layout_plan(60, 16)["tier"], "controls")
        self.assertIsNotNone(layout_plan(60, 16)["controls"])
        self.assertEqual(layout_plan(40, 20)["tier"], "field")
        self.assertIsNone(layout_plan(40, 20)["stats"])
        self.assertEqual(layout_plan(40, 20)["hint"], (19, 0, 1, 40))
        self.assertIsNone(layout_plan(40, 16)["hint"])   # no spare row: header says ? help
        self.assertTrue(layout_plan(30, 20)["too_small"])
        self.assertTrue(layout_plan(77, 15)["too_small"])
        for cols in range(20, 120, 3):
            for rows in range(10, 40):
                plan = layout_plan(cols, rows)
                if not plan["too_small"]:
                    self.check_fits(plan, cols, rows)
                    self.check_centred(plan, cols, rows)
                    # Controls are always on screen: a box or the hint row
                    # (or, with neither, '? help' in the header).
                    if plan["controls"] is None:
                        self.assertEqual(plan["tier"], "field")

    def test_hint_leads_with_help(self):
        for w in (12, 34, 40, 200):
            text = frogger.hint_text(w)
            self.assertIn("? help", text)
            self.assertLess(len(text), w)


class ConstantsTest(unittest.TestCase):
    def test_controls_cover_keys(self):
        keys = " ".join(k for k, _ in frogger.CONTROLS)
        for k in ("?", "q", "p", "r", "arrows", "w", "a", "s", "d", "h", "j", "k", "l"):
            self.assertIn(k, keys)
        self.assertEqual(frogger.CONTROLS[-1], ("q", "menu"))
        for keys, action in frogger.CONTROLS:
            self.assertLessEqual(len(keys), 9)   # never truncated in the box

    def test_help_sections(self):
        heads = [h for h, _ in frogger.HELP]
        for want in ("Goal", "Controls", "Scoring", "Tips", "Legend",
                     "Diving turtles", "Home bays", "What kills you"):
            self.assertIn(want, heads)
        for head, body in frogger.HELP:
            self.assertTrue(body.strip(), head)
            body.encode("ascii")
        self.assertIn("30 seconds", dict(frogger.HELP)["Timer and lives"])


class HelpKeyTest(unittest.TestCase):
    def setUp(self):
        self.game = new_game()
        self.help = frogger.HelpOverlay(frogger.TITLE, frogger.HELP)
        self.st = frogger.LoopState()
        self.session = types.SimpleNamespace(attention=None, dismiss=lambda: None)

    def key(self, ch):
        if isinstance(ch, str):
            ch = ord(ch)
        return frogger.dispatch_key(ch, self.game, self.help, self.st, self.session)

    def test_help_pauses_and_closing_resumes(self):
        self.assertTrue(self.key("?"))
        self.assertTrue(self.help.is_open)
        self.assertTrue(self.game.paused)
        t = self.game.time_left
        self.game.update(0.3)
        self.assertEqual(self.game.time_left, t)       # timers frozen
        self.key("j")                                   # scrolls, stays open
        self.assertTrue(self.help.is_open)
        self.assertEqual(self.key("q"), True)          # q closes help, not the game
        self.assertFalse(self.help.is_open)
        self.assertFalse(self.game.paused)

    def test_help_keeps_a_manual_pause(self):
        self.key("p")
        self.assertTrue(self.game.paused)
        self.key("?")
        self.key("x")
        self.assertFalse(self.help.is_open)
        self.assertTrue(self.game.paused)

    def test_help_from_game_over(self):
        self.game.game_over = True
        self.key("?")
        self.assertTrue(self.help.is_open)
        self.key("x")
        self.assertFalse(self.help.is_open)
        self.assertEqual(self.key("r"), "restart")

    def test_other_keys(self):
        self.assertEqual(self.key("q"), "quit")
        self.assertEqual(self.key("r"), "restart")
        row = self.game.row
        self.key("w")
        self.assertEqual(self.game.row, row - 1)


class FakeWin:
    """curses window stub with curses-accurate bounds; out-of-bounds writes
    are recorded as violations (then raise, as curses does)."""

    def __init__(self, rows, cols, root=None, y0=0, x0=0):
        self.rows, self.cols = rows, cols
        self.y0, self.x0 = y0, x0
        self.root = root or self
        self.writes = []
        if root is None:
            self.violations = []
            self.children = []

    def getmaxyx(self):
        return self.rows, self.cols

    def derwin(self, h, w, y, x):
        if y < 0 or x < 0 or y + h > self.rows or x + w > self.cols:
            self.root.violations.append(("derwin", h, w, y, x))
            raise frogger.curses.error()
        child = FakeWin(h, w, self.root, self.y0 + y, self.x0 + x)
        self.root.children.append(child)
        return child

    def erase(self):
        self.writes = []

    def addstr(self, y, x, text, attr=0):
        if y < 0 or y >= self.rows or x < 0 or x + len(text) > self.cols:
            self.root.violations.append((y, x, text))
            raise frogger.curses.error()
        self.writes.append((y, x, text))


class RenderTest(unittest.TestCase):
    def session(self, attention=None):
        roster = [AgentSnapshot("p%d" % i, "agent%d-with-a-long-name" % i, "omp", s)
                  for i, s in enumerate(["idle", "working", "blocked", "done"] * 5)]
        return types.SimpleNamespace(
            attention=attention, roster=roster,
            status_pairs={s: i + 1 for i, s in
                          enumerate(("idle", "working", "blocked", "done", "unknown"))},
        )

    def render(self, game, cols=77, rows=24, attention=None, resume=False, help=False):
        scr = FakeWin(rows, cols)
        overlay = frogger.HelpOverlay(frogger.TITLE, frogger.HELP)
        if help:
            overlay.open()
        with mock.patch("curses.color_pair", return_value=0):
            lay = frogger.Layout(scr)
            frogger.draw(game, lay, self.session(attention), resume, 999, overlay)
        return scr, lay

    @staticmethod
    def text(scr):
        out = []
        for win in [scr] + scr.children:
            out.extend(t for _, _, t in win.writes)
        return "\n".join(out)

    def games(self):
        yield "fresh", new_game()
        dy = new_game()
        place(dy, 3, COLS - 0.2)
        dy._die("edge")
        dy.phase.elapsed = 0.3
        yield "dying off edge", dy
        cl = new_game()
        cl.homes = [True] * 5
        cl.phase = frogger.Phase("cleared", CLEAR_DURATION)
        yield "cleared", cl
        go = new_game()
        go.game_over = True
        go.lives = 0
        yield "game over", go
        pa = new_game()
        pa.paused = True
        pa.fly = 1
        pa.homes[0] = True
        yield "paused", pa
        lt = new_game()
        for _ in range(100):
            lt.update(0.13)
        yield "later", lt

    def test_full_frame_in_bounds(self):
        for name, game in self.games():
            scr, lay = self.render(game)
            self.assertEqual(scr.violations, [], name)
            field = lay.field
            self.assertEqual((field.rows, field.cols), (frogger.FIELD_H, frogger.FIELD_W))
            for child in scr.children:
                self.assertLessEqual(child.x0 + child.cols, 77)
                self.assertLessEqual(child.y0 + child.rows, 24)
            # The frog is drawn two columns wide inside the frame.
            if name == "fresh":
                frog = [(y, x) for y, x, t in field.writes if t == frogger.FROG]
                self.assertEqual(frog, [(1 + START_ROW, 1 + START_X * 2)])
                self.assertTrue(any(" agents " in t for _, _, t in lay.agents.writes))
                self.assertTrue(any("score" in t for _, _, t in lay.stats.writes))

    def test_banner_and_other_sizes(self):
        att = (AgentSnapshot("p1", "agent", "omp", "blocked"), "working")
        scr, _ = self.render(new_game(), attention=att)
        self.assertEqual(scr.violations, [])
        self.assertTrue(any("blocked" in t for y, _, t in scr.writes if y == 0))
        for cols, rows in ((93, 28), (50, 24), (40, 18), (34, 16), (20, 8), (77, 16),
                           (120, 40)):
            for name, game in self.games():
                scr, _ = self.render(game, cols, rows, resume=True)
                self.assertEqual(scr.violations, [], (name, cols, rows))
        scr, lay = self.render(new_game(), 20, 8)
        self.assertTrue(any("pane too small" in t for _, _, t in scr.writes))

    def test_controls_visible_and_help_overlay(self):
        for cols, rows in ((93, 28), (77, 24), (60, 18), (40, 20), (40, 16), (34, 16)):
            for help in (False, True):
                scr, lay = self.render(new_game(), cols, rows, help=help)
                self.assertEqual(scr.violations, [], (cols, rows, help))
                if help:
                    self.assertIn("Frogger - help", self.text(scr), (cols, rows))
                    continue
                if lay.controls is not None:
                    keys = "\n".join(t for _, _, t in lay.controls.writes)
                    for want in ("hop", "help", "pause", "menu"):
                        self.assertIn(want, keys, (cols, rows))
                elif lay.plan["hint"] is not None:
                    hint = [t for y, _, t in scr.writes if y == rows - 1]
                    self.assertTrue(any("? help" in t for t in hint), (cols, rows))
                else:
                    head = [t for y, _, t in scr.writes if y == 0]
                    self.assertTrue(any("? help" in t for t in head), (cols, rows))
        scr, lay = self.render(new_game(), 93, 28)
        self.assertEqual(lay.controls.getmaxyx()[1], 30)
        self.assertTrue(any("Frogger" in t for _, _, t in lay.stats.writes))
        self.assertTrue(any("help" in t for _, _, t in lay.controls.writes))


if __name__ == "__main__":
    unittest.main()


class ObjectGlyphTest(unittest.TestCase):
    def test_every_glyph_is_two_columns_per_cell(self):
        # A glyph shorter than its object would hide part of the hit area.
        for kind in ("car", "log", "turtle"):
            for length in range(1, 7):
                for speed in (-1.0, 1.0):
                    glyph = frogger.object_glyph(kind, length, speed)
                    self.assertEqual(len(glyph), 2 * length, (kind, length, speed))
