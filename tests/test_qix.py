"""Unit tests for the pure game logic, layout and rendering in qix.py (no
terminal needed)."""

import json
import os
import random
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common  # noqa: E402
import qix  # noqa: E402
from common import AgentSnapshot  # noqa: E402
from qix import (  # noqa: E402
    BONUS_PER_PERCENT,
    DOWN,
    EDGE,
    EMPTY,
    FAST,
    FIELD_H,
    FIELD_W,
    FILL_FAST,
    FILL_SLOW,
    FUSE_DELAY,
    INTERIOR_TOTAL,
    LEFT,
    LIVES,
    POINTS_PER_CELL,
    RIGHT,
    SLOW,
    SLOW_STEP,
    SPARX_SPAWN_EVERY,
    START_MARKER,
    SUPER_TIME,
    UP,
    Game,
    layout_plan,
    sparx_interval,
)


class NoJitter:
    """RNG stand-in that never triggers the Qix's random course changes."""

    def random(self):
        return 0.99


def new_game(seed=1):
    return Game(random.Random(seed))


def calm(game, x=45.0, y=15.0):
    """Park the Qix (no velocity, no spin, short) far from the action and
    freeze it, and remove the Sparx: rules tests drive time explicitly."""
    game.qix.update(x=x, y=y, vx=0.0, vy=0.0, a=0.0, va=0.0, len=2)
    game.step_qix = lambda: None
    game.sparx = []
    return game


def moves(game, d, n):
    return [game.move(d) for _ in range(n)]


def finish_phase(game):
    game.update(10.0)


class MovementTest(unittest.TestCase):
    def test_restricted_to_safe_lines_when_not_drawing(self):
        g = calm(new_game())
        self.assertEqual(g.marker, START_MARKER)
        self.assertFalse(g.move(UP))  # unclaimed space
        self.assertFalse(g.move(DOWN))  # off the field
        self.assertEqual(g.marker, START_MARKER)
        self.assertTrue(g.move(LEFT))
        self.assertEqual(g.marker, (START_MARKER[0] - 1, START_MARKER[1]))
        self.assertEqual(g.trail, [])
        # Along the border and round the corner.
        moves(g, LEFT, FIELD_W)
        self.assertEqual(g.marker, (0, FIELD_H - 1))
        self.assertTrue(g.move(UP))
        self.assertEqual(g.marker, (0, FIELD_H - 2))
        self.assertFalse(g.move(RIGHT))

    def test_trail_starts_only_in_draw_mode(self):
        g = calm(new_game())
        x, y = START_MARKER
        self.assertFalse(g.move(UP))
        self.assertTrue(g.toggle_draw(FAST))
        self.assertEqual(g.mode, FAST)
        self.assertTrue(g.move(UP))
        self.assertEqual(g.trail, [(x, y - 1)])
        self.assertEqual(g.trail_start, (x, y))
        self.assertEqual(g.marker, (x, y - 1))
        # Draw mode is locked while a trail is out.
        self.assertFalse(g.toggle_draw(FAST))
        self.assertEqual(g.mode, FAST)

    def test_toggle_off_and_slow_upgrade(self):
        g = calm(new_game())
        g.toggle_draw(FAST)
        g.toggle_draw(FAST)
        self.assertIsNone(g.mode)
        g.toggle_draw(SLOW)
        self.assertEqual(g.mode, SLOW)
        g.move(UP)
        self.assertFalse(g.toggle_draw(SLOW))  # can't stop mid-trail
        self.assertTrue(g.toggle_draw(FAST))  # slow may switch to fast
        self.assertEqual(g.mode, FAST)

    def test_cannot_cross_own_trail_or_backtrack(self):
        g = calm(new_game())
        x, y = START_MARKER
        g.toggle_draw(FAST)
        g.move(UP)
        self.assertFalse(g.move(DOWN))  # back onto the start point
        moves(g, UP, 2)
        g.move(RIGHT)
        g.move(DOWN)
        self.assertEqual(g.marker, (x + 1, y - 2))
        self.assertFalse(g.move(LEFT))  # (x, y-2) is trail
        self.assertEqual(g.marker, (x + 1, y - 2))
        self.assertEqual(len(g.trail), 5)

    def test_slow_draw_is_rate_limited(self):
        g = calm(new_game())
        g.toggle_draw(SLOW)
        self.assertTrue(g.move(UP))
        self.assertFalse(g.move(UP))  # too soon
        g.update(SLOW_STEP)
        self.assertTrue(g.move(UP))
        # Fast draw has no such limit.
        g2 = calm(new_game())
        g2.toggle_draw(FAST)
        self.assertEqual(moves(g2, UP, 3), [True, True, True])


def draw_corner_box(g, mode):
    """From (5, 0) draw down to (5, 4), then left to the left border at
    (0, 4), enclosing x 1..4, y 1..3 (the Qix sits far to the right)."""
    g.marker = (5, 0)
    g.toggle_draw(mode)
    for d, n in ((DOWN, 4), (LEFT, 5)):
        for _ in range(n):
            if mode == SLOW:
                g.update(SLOW_STEP)
            assert g.move(d)


class ClaimTest(unittest.TestCase):
    INTERIOR = {(x, y) for x in range(1, 5) for y in range(1, 4)}
    TRAIL = [(5, 1), (5, 2), (5, 3), (5, 4), (4, 4), (3, 4), (2, 4), (1, 4)]
    RETIRED = {(1, 0), (2, 0), (3, 0), (4, 0), (0, 0), (0, 1), (0, 2), (0, 3)}

    def filled(self, g, kind):
        return {(x, y) for y in range(FIELD_H) for x in range(FIELD_W)
                if g.grid[y][x] == kind}

    def test_closing_claims_side_without_qix_exactly(self):
        g = calm(new_game())
        draw_corner_box(g, FAST)
        self.assertEqual(g.marker, (0, 4))
        self.assertEqual(g.trail, [])
        self.assertIsNone(g.phase)
        self.assertEqual(self.filled(g, FILL_FAST), self.INTERIOR | self.RETIRED)
        self.assertEqual(self.filled(g, FILL_SLOW), set())
        for x, y in self.TRAIL + [(5, 0), (0, 4)]:
            self.assertEqual(g.grid[y][x], EDGE, (x, y))
        # Everything else untouched: the Qix side is still open.
        self.assertEqual(g.grid[15][45], EMPTY)
        self.assertEqual(g.empty_count(), INTERIOR_TOTAL - 20)
        # Fast: 12 enclosed + 8 trail cells, single points.
        self.assertEqual(g.score, 20 * POINTS_PER_CELL)

    def test_side_with_qix_is_the_one_kept(self):
        g = calm(new_game(), x=2.0, y=2.0)  # the Qix is inside the box
        draw_corner_box(g, FAST)
        for x, y in self.INTERIOR:
            self.assertEqual(g.grid[y][x], EMPTY)
        self.assertEqual(g.grid[15][45], FILL_FAST)
        self.assertEqual(g.empty_count(), 12)

    def test_slow_scores_double_and_fills_slow(self):
        g = calm(new_game())
        draw_corner_box(g, SLOW)
        self.assertEqual(self.filled(g, FILL_SLOW), self.INTERIOR | self.RETIRED)
        self.assertEqual(g.score, 2 * 20 * POINTS_PER_CELL)

    def test_claimed_interior_is_no_longer_a_safe_line(self):
        g = calm(new_game())
        draw_corner_box(g, FAST)
        g.toggle_draw(FAST)  # back to plain moving
        self.assertIsNone(g.mode)
        self.assertFalse(g.move(UP))  # (0, 3): old border, now interior
        self.assertTrue(g.move(RIGHT))  # (1, 4): the new edge
        self.assertFalse(g.move(UP))  # (1, 3): claimed fill
        g.marker = (5, 0)
        self.assertFalse(g.move(LEFT))  # (4, 0): retired border
        self.assertTrue(g.move(RIGHT))
        # Even in draw mode, claimed cells can't be entered.
        g.marker = (1, 4)
        g.toggle_draw(FAST)
        self.assertFalse(g.move(UP))

    def test_sparx_on_retired_line_is_moved_to_a_safe_line(self):
        g = calm(new_game())
        g.sparx = [{"x": 2, "y": 0, "dir": RIGHT, "cw": True}]
        draw_corner_box(g, FAST)
        s = g.sparx[0]
        self.assertEqual(g.grid[s["y"]][s["x"]], EDGE)


    def test_closing_into_a_dead_end_leaves_marker_on_a_safe_line(self):
        g = calm(new_game())
        g.marker = (2, 0)
        g.toggle_draw(FAST)
        for d, n in ((DOWN, 10), (RIGHT, 8), (UP, 10)):
            self.assertTrue(all(moves(g, d, n)))
        # A one-wide corridor at x=1 remains; close it off from below.
        g.marker = (2, 10)
        self.assertTrue(g.move(LEFT))
        self.assertTrue(all(moves(g, UP, 10)))
        self.assertEqual(g.grid[0][1], FILL_FAST)  # landing cell retired
        mx, my = g.marker
        self.assertEqual(g.grid[my][mx], EDGE)
        self.assertTrue(any(g.move(d) for d in (UP, DOWN, LEFT, RIGHT)))
        self.assertIsNotNone(Game.from_save(json.loads(json.dumps(g.to_save()))))


class PercentTest(unittest.TestCase):
    def test_percentage(self):
        g = new_game()
        self.assertEqual(g.claimed_percent(), 0.0)
        for x in range(1, 15):  # 14 of 56 interior columns
            for y in range(1, FIELD_H - 1):
                g.grid[y][x] = FILL_FAST
        self.assertAlmostEqual(g.claimed_percent(), 25.0)
        g.grid[1][20] = EDGE  # interior safe line counts as claimed
        self.assertAlmostEqual(g.claimed_percent(), 100.0 * 295 / INTERIOR_TOTAL)

    def prepared(self):
        """Columns 1..39 claimed, column 40 a safe line: 40*21 = 71.4%."""
        g = calm(new_game(), x=52.0, y=10.0)
        for y in range(1, FIELD_H - 1):
            for x in range(1, 40):
                g.grid[y][x] = FILL_FAST
            g.grid[y][40] = EDGE
        return g

    def test_level_clear_at_75_percent_with_bonus(self):
        g = self.prepared()
        self.assertLess(g.claimed_percent(), 75)
        g.marker = (46, 0)
        g.toggle_draw(FAST)
        self.assertTrue(all(moves(g, DOWN, FIELD_H - 1)))  # to the bottom
        # 5*21 enclosed + 21 trail = 126 new cells -> 966/1176 = 82.1%.
        self.assertAlmostEqual(g.claimed_percent(), 100.0 * 966 / INTERIOR_TOTAL)
        self.assertIsNotNone(g.phase)
        self.assertEqual(g.phase.kind, "level_clear")
        self.assertEqual(g.last_bonus, 7 * BONUS_PER_PERCENT)
        self.assertEqual(g.score, 126 * POINTS_PER_CELL + 7 * BONUS_PER_PERCENT)
        finish_phase(g)
        self.assertEqual(g.level, 2)
        self.assertEqual(g.claimed_percent(), 0.0)
        self.assertEqual(g.marker, START_MARKER)
        self.assertEqual(len(g.sparx), 2)
        self.assertLess(qix.qix_interval(2), qix.qix_interval(1))
        self.assertLess(sparx_interval(2), sparx_interval(1))

    def test_below_target_no_clear(self):
        g = self.prepared()
        g.marker = (41, 0)  # a trail hugging the x=40 line: 21 cells
        g.toggle_draw(FAST)
        moves(g, DOWN, FIELD_H - 1)
        self.assertAlmostEqual(g.claimed_percent(), 100.0 * 861 / INTERIOR_TOTAL)
        self.assertLess(g.claimed_percent(), 75)
        self.assertIsNone(g.phase)


class QixTest(unittest.TestCase):
    def drawing(self):
        g = calm(new_game())
        g.toggle_draw(FAST)
        moves(g, UP, 5)  # trail x=29, y 21..17
        return g

    def test_body_stays_in_unclaimed_space(self):
        g = new_game(3)
        g.sparx = []
        for _ in range(300):
            g.step_qix()
            body, hit = g.qix_body()
            self.assertFalse(hit)
            for x, y in body:
                self.assertEqual(g.grid[y][x], EMPTY)

    def test_qix_touching_trail_kills(self):
        g = self.drawing()
        x = START_MARKER[0]
        g.qix.update(x=float(x - 2), y=19.0, a=0.0, len=6)
        body, hit = g.qix_body()
        self.assertTrue(hit)
        self.assertNotIn((x, 19), body)  # the arm stops at the trail
        del g.step_qix  # the real step, with no random course change
        g.rng = NoJitter()
        g.step_qix()
        self.assertIsNotNone(g.phase)
        self.assertEqual(g.phase.kind, "dying")
        self.assertEqual(g.lives, LIVES - 1)

    def test_drawing_into_the_qix_kills(self):
        g = calm(new_game())
        x = START_MARKER[0]
        g.qix.update(x=float(x - 2), y=17.0, a=0.0, len=6)
        g.toggle_draw(FAST)
        moves(g, UP, 4)
        self.assertIsNone(g.phase)
        g.move(UP)  # onto (x, 17): the Qix body
        self.assertIsNotNone(g.phase)
        self.assertEqual(g.phase.kind, "dying")

    def test_qix_ignores_marker_on_safe_line(self):
        g = calm(new_game())
        g.qix.update(x=float(START_MARKER[0]), y=float(FIELD_H - 2), len=6)
        g.toggle_draw(FAST)
        self.assertFalse(g.qix_body()[1])
        g._check_qix_hit()
        self.assertIsNone(g.phase)


class SparxTest(unittest.TestCase):
    def test_travel_border_in_opposite_directions(self):
        g = new_game()
        cw, ccw = g.sparx
        self.assertTrue(cw["cw"])
        self.assertFalse(ccw["cw"])
        self.assertEqual((cw["x"], cw["y"]), (ccw["x"], ccw["y"]))
        x0 = cw["x"]
        for _ in range(5):
            g.step_sparx()
        self.assertEqual((cw["x"], cw["y"]), (x0 + 5, 0))
        self.assertEqual((ccw["x"], ccw["y"]), (x0 - 5, 0))
        for _ in range(30):
            g.step_sparx()
        # Clockwise turned down the right side, counter-clockwise the left.
        right = FIELD_W - 1 - (x0 + 5)
        self.assertEqual((cw["x"], cw["y"]), (FIELD_W - 1, 30 - right))
        self.assertEqual((ccw["x"], ccw["y"]), (0, 30 - (x0 - 5)))
        # Full laps return them to the start, always on safe lines.
        lap = 2 * (FIELD_W - 1) + 2 * (FIELD_H - 1)
        for _ in range(lap - 35):
            g.step_sparx()
            for s in g.sparx:
                self.assertEqual(g.grid[s["y"]][s["x"]], EDGE)
        self.assertEqual((cw["x"], cw["y"]), (x0, 0))
        self.assertEqual((ccw["x"], ccw["y"]), (x0, 0))

    def test_follow_new_safe_lines(self):
        g = calm(new_game())
        draw_corner_box(g, FAST)
        g.marker = (FIELD_W // 2, FIELD_H - 1)
        g.sparx = [{"x": 10, "y": 0, "dir": LEFT, "cw": False}]
        path = []
        for _ in range(12):
            g.step_sparx()
            s = g.sparx[0]
            path.append((s["x"], s["y"]))
            self.assertEqual(g.grid[s["y"]][s["x"]], EDGE)
        self.assertEqual(path[:5], [(9, 0), (8, 0), (7, 0), (6, 0), (5, 0)])
        self.assertEqual(path[5:9], [(5, 1), (5, 2), (5, 3), (5, 4)])
        self.assertEqual(path[9], (4, 4))

    def test_sparx_reaching_marker_kills(self):
        g = calm(new_game())
        x, y = START_MARKER
        g.sparx = [{"x": x - 1, "y": y, "dir": RIGHT, "cw": True}]
        g.step_sparx()
        self.assertEqual(g.phase.kind, "dying")

    def test_marker_walking_into_sparx_kills(self):
        g = calm(new_game())
        x, y = START_MARKER
        g.sparx = [{"x": x - 2, "y": y, "dir": LEFT, "cw": False}]
        g.move(LEFT)
        self.assertIsNone(g.phase)
        g.move(LEFT)
        self.assertEqual(g.phase.kind, "dying")

    def test_more_sparx_over_time_and_super(self):
        g = calm(new_game())
        g.sparx = [{"x": 20, "y": 0, "dir": RIGHT, "cw": True}]
        g.spawn_timer = SPARX_SPAWN_EVERY - 0.01
        g.update(0.02)
        self.assertEqual(len(g.sparx), 2)
        s = g.sparx[1]
        self.assertEqual(g.grid[s["y"]][s["x"]], EDGE)
        self.assertNotEqual((s["x"], s["y"]), g.marker)
        self.assertFalse(g.is_super())
        g.level_time = SUPER_TIME
        self.assertTrue(g.is_super())
        self.assertLess(sparx_interval(1, True), sparx_interval(1))


class FuseAndDeathTest(unittest.TestCase):
    def test_fuse_starts_after_idle_and_kills_at_marker(self):
        g = calm(new_game())
        x, y = START_MARKER
        g.toggle_draw(FAST)
        moves(g, UP, 5)
        g.update(1.0)
        self.assertIsNone(g.fuse)
        g.update(FUSE_DELAY - 1.0 + 0.01)
        self.assertEqual(g.fuse, 0.0)
        g.update(0.5)
        self.assertGreater(g.fuse, 0)
        self.assertIsNone(g.phase)
        # Moving again puts the fuse out.
        self.assertTrue(g.move(LEFT))
        self.assertIsNone(g.fuse)
        self.assertEqual(g.idle, 0.0)
        # Idle once more: it burns the whole trail up to the marker.
        g.update(FUSE_DELAY)
        self.assertIsNotNone(g.fuse)
        for _ in range(100):
            if g.phase is not None:
                break
            self.assertLess(g.fuse, len(g.trail))
            g.update(0.05)
        self.assertEqual(g.phase.kind, "dying")
        self.assertEqual(g.lives, LIVES - 1)
        # Death: trail gone, marker back at the start of its trail.
        finish_phase(g)
        self.assertIsNone(g.phase)
        self.assertEqual(g.trail, [])
        self.assertIsNone(g.fuse)
        self.assertIsNone(g.mode)
        self.assertEqual(g.marker, (x, y))
        self.assertEqual(g.grid[y - 1][x], EMPTY)  # trail left no line

    def test_no_fuse_off_the_trail(self):
        g = calm(new_game())
        g.update(5.0)
        self.assertIsNone(g.fuse)
        self.assertIsNone(g.phase)

    def test_death_on_safe_line_keeps_marker_and_moves_sparx_away(self):
        g = calm(new_game())
        x, y = START_MARKER
        g.sparx = [{"x": x - 1, "y": y, "dir": RIGHT, "cw": True}]
        g.step_sparx()
        finish_phase(g)
        self.assertEqual(g.marker, (x, y))
        s = g.sparx[0]
        self.assertEqual(g.grid[s["y"]][s["x"]], EDGE)
        self.assertGreater(abs(s["x"] - x) + abs(s["y"] - y), 20)

    def test_lives_and_game_over(self):
        g = calm(new_game())
        for i in range(LIVES):
            self.assertFalse(g.game_over)
            g._die()
            self.assertEqual(g.lives, LIVES - 1 - i)
            finish_phase(g)
        self.assertTrue(g.game_over)
        self.assertEqual(g.lives, 0)
        self.assertFalse(g.move(LEFT))
        self.assertFalse(g.update(1.0))
        g.reset()
        self.assertFalse(g.game_over)
        self.assertEqual(g.lives, LIVES)
        self.assertEqual(g.score, 0)


class SaveTest(unittest.TestCase):
    def advanced(self):
        g = new_game(5)
        g.sparx = g.sparx[:1]
        g.qix.update(x=45.0, y=15.0, vx=0.0, vy=0.0)
        draw_corner_box(g, SLOW)
        g.marker = START_MARKER
        g.toggle_draw(SLOW)
        g.toggle_draw(FAST)
        moves(g, UP, 3)
        g.idle = 1.7
        g.fuse = 1.25
        g.level_time = 12.5
        return g

    def test_round_trip_including_rng(self):
        g = self.advanced()
        data = json.loads(json.dumps(g.to_save()))
        r = Game.from_save(data)
        self.assertIsNotNone(r)
        self.assertEqual(r.to_save(), g.to_save())
        self.assertEqual(r.grid, g.grid)
        self.assertEqual(r.trail, g.trail)
        self.assertEqual(r.trail_set, g.trail_set)
        # Same RNG state: the Qix wanders identically from here on.
        for _ in range(40):
            g.step_qix()
            r.step_qix()
        self.assertEqual(r.qix, g.qix)
        self.assertEqual(r.rng.random(), g.rng.random())

    def test_corrupt_or_incompatible_is_none(self):
        good = self.advanced().to_save()
        self.assertIsNotNone(Game.from_save(json.loads(json.dumps(good))))
        self.assertIsNone(Game.from_save(None))
        self.assertIsNone(Game.from_save({}))
        self.assertIsNone(Game.from_save("junk"))
        cases = [
            dict(good, version=qix.SAVE_VERSION + 1),
            dict(good, rng=[3, [1, 2], None]),
            dict(good, grid=good["grid"][:-1]),
            dict(good, grid=["?" * FIELD_W] * FIELD_H),
            dict(good, trail=[[0, 0]]),  # on the border
            dict(good, trail=good["trail"][:1] + good["trail"][2:]),  # gap
            dict(good, marker=[1, 1]),  # not at the trail's end
            dict(good, mode="sideways"),
            dict(good, fuse=99.0),
            dict(good, sparx=[{"x": 10, "y": 10, "dir": UP, "cw": True}]),
            dict(good, qix=dict(good["qix"], x=0.0, y=0.0)),  # not open space
            dict(good, lives=0),
            dict(good, grid=["." * FIELD_W] + good["grid"][1:]),  # open ring
        ]
        for i, bad in enumerate(cases):
            self.assertIsNone(Game.from_save(bad), i)
        bad = dict(good)
        del bad["score"]
        self.assertIsNone(Game.from_save(bad))

    def test_phase_resolved_before_save(self):
        g = calm(new_game())
        g.toggle_draw(FAST)
        moves(g, UP, 3)
        g._die()
        g.resolve_phases()
        r = Game.from_save(json.loads(json.dumps(g.to_save())))
        self.assertIsNotNone(r)
        self.assertEqual(r.trail, [])
        self.assertEqual(r.marker, START_MARKER)
        self.assertEqual(r.lives, LIVES - 1)


REGIONS = ("field", "stats", "controls", "agents", "hint")


class LayoutPlanTest(unittest.TestCase):
    def assert_fits(self, plan, cols, rows):
        for name in REGIONS:
            region = plan[name]
            if region is None:
                continue
            y, x, h, w = region
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 1, name)
            self.assertLessEqual(x + w, cols, name)
            self.assertLessEqual(y + h, rows, name)

    def assert_centred(self, plan, cols, rows):
        fy, fx, fh, fw = plan["field"]
        right, bottom = fx + fw, fy + fh
        for name in ("stats", "controls", "agents"):
            if plan[name] is not None:
                y, x, h, w = plan[name]
                right = max(right, x + w)
                bottom = max(bottom, y + h)
        if plan["hint"] is not None:
            bottom = plan["hint"][0] + 1
        self.assertLessEqual(abs(fx - (cols - right)), 1, (cols, rows))
        self.assertLessEqual(abs((fy - 1) - (rows - bottom)), 1, (cols, rows))

    def test_popup_size_93x28(self):
        plan = layout_plan(93, 28)
        self.assertEqual(plan["tier"], "full")
        self.assert_fits(plan, 93, 28)
        self.assert_centred(plan, 93, 28)
        self.assertEqual(plan["field"][2:], (FIELD_H, FIELD_W))
        for name in ("stats", "controls", "agents"):
            self.assertIsNotNone(plan[name], name)
            self.assertEqual(plan[name][3], 30, name)
        sy, _, sh, _ = plan["stats"]
        cy, _, ch, _ = plan["controls"]
        ay, _, ah, _ = plan["agents"]
        self.assertEqual(sy, plan["field"][0])
        self.assertEqual(sy + sh, cy)
        self.assertEqual(cy + ch, ay)
        self.assertEqual(ay + ah, plan["field"][0] + FIELD_H)
        self.assertEqual(ch, common.controls_height(qix.CONTROLS, 30))
        self.assertIsNone(plan["hint"])

    def test_degrading_tiers(self):
        plan = layout_plan(79, 24)
        self.assertIn(plan["tier"], ("full", "side"))
        self.assertEqual(plan["controls"][3], 20)
        self.assertIsNotNone(plan["stats"])
        for cols, rows in ((78, 24), (77, 24), (74, 24), (65, 24), (58, 24), (58, 25)):
            plan = layout_plan(cols, rows)
            self.assertEqual(plan["tier"], "field", (cols, rows))
            self.assertIsNone(plan["stats"])
            self.assertIsNone(plan["controls"])
            self.assertTrue(plan["hint"] is not None or plan["hint_in_header"])
        self.assertTrue(layout_plan(77, 24)["hint_in_header"])
        self.assertIsNotNone(layout_plan(58, 25)["hint"])
        self.assertTrue(layout_plan(FIELD_W - 1, 24)["too_small"])
        self.assertTrue(layout_plan(77, 23)["too_small"])
        for cols, rows in ((93, 28), (79, 24), (77, 24), (74, 24), (65, 24),
                           (58, 24), (58, 25), (70, 30), (100, 40), (140, 50)):
            plan = layout_plan(cols, rows)
            self.assert_fits(plan, cols, rows)
            self.assert_centred(plan, cols, rows)


class ConstantsTest(unittest.TestCase):
    def test_controls_cover_every_key(self):
        keys = " ".join(k for k, _ in qix.CONTROLS)
        for k in ("?", "q", "p", "r", "space", "f", "arrows"):
            self.assertIn(k, keys)
        text = " ".join(a for _, a in qix.CONTROLS)
        for k in ("wasd", "hjkl", "fast draw", "slow draw"):
            self.assertIn(k, text)
        for code in qix.KEY_DIRS:
            if 0 < code < 256:
                self.assertIn(chr(code).lower(), "wasdhjkl")

    def test_help_explains_the_confusing_parts(self):
        heads = [h for h, _ in qix.HELP]
        for h in ("Goal", "Controls", "Scoring", "Tips", "Legend",
                  "Safe lines vs unclaimed space", "Dangers"):
            self.assertIn(h, heads)
        text = " ".join(b for _, b in qix.HELP)
        text.encode("ascii")
        for phrase in ("space", "f:", "STAYS on", "does NOT contain", "fuse",
                       "Sparx", "75%", "Super", "x2"):
            self.assertIn(phrase, text)
        for h, body in qix.HELP:
            self.assertTrue(body.strip(), h)

    def test_hint_keeps_help(self):
        for w in (6, 10, 20, 57, 200):
            self.assertIn("? help", qix.hint_text(w))
            self.assertLessEqual(len(qix.hint_text(w)), w)


class HelpKeyTest(unittest.TestCase):
    def test_help_pauses_and_close_resumes(self):
        g = new_game()
        h = common.HelpOverlay(qix.TITLE, qix.HELP)
        consumed, pbh, hand = qix.help_key(h, g, ord("?"), False)
        self.assertTrue(consumed and pbh and h.is_open and g.paused)
        # Game keys scroll or close the help; they never reach the game.
        before = g.to_save()
        consumed, pbh, _ = qix.help_key(h, g, qix.curses.KEY_DOWN, pbh)
        self.assertTrue(consumed and h.is_open and g.paused)
        self.assertFalse(g.update(5.0))
        self.assertEqual(g.to_save(), before)
        consumed, pbh, hand = qix.help_key(h, g, ord("f"), pbh)
        self.assertTrue(consumed)
        self.assertFalse(h.is_open or g.paused or pbh or hand)
        self.assertIsNone(g.mode)  # the closing key was not a draw toggle

    def test_already_paused_and_game_over_stay_put(self):
        g = new_game()
        g.paused = True
        h = common.HelpOverlay(qix.TITLE, qix.HELP)
        _, pbh, _ = qix.help_key(h, g, ord("?"), False)
        self.assertFalse(pbh)
        qix.help_key(h, g, ord("x"), pbh)
        self.assertTrue(g.paused)
        go = new_game()
        go.game_over = True
        h = common.HelpOverlay(qix.TITLE, qix.HELP)
        consumed, pbh, _ = qix.help_key(h, go, ord("?"), False)
        self.assertTrue(consumed and h.is_open)
        self.assertFalse(pbh or go.paused)

    def test_banner_handover_and_resize(self):
        g = new_game()
        h = common.HelpOverlay(qix.TITLE, qix.HELP)
        _, pbh, _ = qix.help_key(h, g, ord("?"), False)
        self.assertFalse(qix.help_key(h, g, qix.curses.KEY_RESIZE, pbh)[0])
        _, pbh, hand = qix.help_key(h, g, ord("x"), pbh, hold=True)
        self.assertTrue(hand and g.paused)
        self.assertFalse(qix.help_key(h, g, ord("x"), False)[0])


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
            raise qix.curses.error()
        child = FakeWin(h, w, self.root, self.y0 + y, self.x0 + x)
        self.root.children.append(child)
        return child

    def erase(self):
        self.writes = []

    def addstr(self, y, x, text, attr=0):
        if y < 0 or y >= self.rows or x < 0 or x + len(text) > self.cols:
            self.root.violations.append((y, x, text))
            raise qix.curses.error()
        self.writes.append((y, x, text))


class RenderTest(unittest.TestCase):
    def session(self, attention=None):
        roster = [AgentSnapshot("p%d" % i, "agent%d" % i, "omp", s)
                  for i, s in enumerate(["idle", "working", "blocked", "done"] * 5)]
        return types.SimpleNamespace(
            attention=attention, roster=roster,
            status_pairs={s: i + 1 for i, s in
                          enumerate(("idle", "working", "blocked", "done", "unknown"))},
        )

    def render(self, game, cols=93, rows=28, attention=None, resume=False,
               help=None):
        scr = FakeWin(rows, cols)
        with mock.patch("curses.color_pair", return_value=0):
            lay = qix.Layout(scr)
            qix.draw(game, lay, self.session(attention), resume, 1234, help)
        return scr, lay

    def games(self):
        yield "fresh", new_game()
        t = new_game(2)
        draw_corner_box(calm(t), SLOW)
        t.marker = START_MARKER
        t.toggle_draw(SLOW)
        t.toggle_draw(FAST)
        moves(t, UP, 6)
        t.fuse = 2.5
        t.level_time = SUPER_TIME + 1
        for _ in range(8):
            qix.Game.step_qix(t)
        yield "trail+fuse", t
        d = calm(new_game())
        d.toggle_draw(FAST)
        moves(d, UP, 3)
        d._die()
        d.phase.elapsed = 0.2
        yield "dying", d
        lc = new_game()
        lc.phase = qix.Phase("level_clear", 1.0)
        lc.last_bonus = 3000
        yield "level_clear", lc
        p = new_game()
        p.paused = True
        yield "paused", p
        go = new_game()
        go.lives = 0
        go.game_over = True
        yield "game_over", go

    def test_full_frame_has_no_out_of_bounds_writes(self):
        attention = (AgentSnapshot("p1", "a very long agent label indeed", "omp",
                                   "blocked"), "working")
        for name, game in self.games():
            for size in ((93, 28), (79, 24), (77, 24), (74, 24), (65, 24),
                         (58, 25), (40, 10), (10, 3)):
                for att, res in ((None, False), (attention, False), (None, True)):
                    scr, lay = self.render(game, *size, attention=att, resume=res)
                    self.assertEqual(scr.violations, [], (name, size))

    def test_full_frame_contents(self):
        scr, lay = self.render(new_game())
        self.assertEqual(lay.plan["tier"], "full")
        text = "".join(t for c in scr.children for _, _, t in c.writes)
        self.assertIn("score 0", text)
        self.assertIn("area  0% / 75%", text)
        self.assertIn("MOVING", text)
        self.assertIn("agents", text)
        self.assertIn("@", text)
        header = "".join(t for _, _, t in scr.writes)
        self.assertIn("qix", header)
        go = new_game()
        go.game_over = True
        scr, _ = self.render(go)
        text = "".join(t for c in scr.children for _, _, t in c.writes)
        self.assertIn("GAME OVER", text)
        self.assertIn("r restart | q menu", text)

    def test_field_rows_are_full_width(self):
        scr, lay = self.render(new_game())
        field = lay.field
        for y in range(FIELD_H):
            row = sorted((x, t) for yy, x, t in field.writes if yy == y)
            self.assertEqual(sum(len(t) for _, t in row), FIELD_W, y)

    def all_text(self, scr):
        return " ".join(t for w in [scr] + scr.children for _, _, t in w.writes)

    def test_controls_box_and_side_column(self):
        scr, lay = self.render(new_game())
        ctrl = " ".join(t for _, _, t in lay.controls.writes)
        for k, _ in qix.CONTROLS:
            self.assertIn(k, ctrl)
        self.assertIn("fast draw on/off", ctrl)
        self.assertNotIn("q menu", " ".join(t for _, _, t in lay.stats.writes))

    def test_mode_is_visible_in_every_tier(self):
        for mode, label in ((None, "MOVING"), (FAST, "FAST DRAW"),
                            (SLOW, "SLOW DRAW x2")):
            g = new_game()
            if mode is not None:
                g.toggle_draw(mode)
            for cols, rows in ((93, 28), (79, 24), (77, 24), (58, 24)):
                scr, lay = self.render(g, cols, rows)
                header = "".join(t for y, _, t in scr.writes if y == 0)
                self.assertIn(label, header, (cols, rows))
                if lay.stats is not None:
                    stats = " ".join(t for _, _, t in lay.stats.writes)
                    self.assertIn(label, stats)
        g = new_game()
        g.toggle_draw(FAST)
        scr, lay = self.render(g)
        header = "".join(t for y, _, t in scr.writes if y == 0)
        self.assertNotIn("MOVING", header)

    def test_hint_in_field_tier(self):
        scr, lay = self.render(new_game(), 77, 24)
        header = "".join(t for y, _, t in scr.writes if y == 0)
        self.assertIn("? help", header)
        self.assertIn("score", header)
        scr, lay = self.render(new_game(), 60, 26)
        row = lay.plan["hint"][0]
        hint = "".join(t for y, _, t in scr.writes if y == row)
        self.assertIn("? help", hint)
        self.assertIn("q menu", hint)

    def test_help_overlay_renders_in_bounds(self):
        for cols, rows in ((93, 28), (77, 24), (58, 25), (58, 24), (30, 10)):
            h = common.HelpOverlay(qix.TITLE, qix.HELP)
            h.open()
            for name, game in self.games():
                scr, lay = self.render(game, cols, rows, help=h)
                self.assertEqual(scr.violations, [], (name, cols, rows))
            text = self.all_text(scr)
            self.assertIn("Qix - help", text)
            self.assertIn("Goal", text)
            if not lay.too_small:
                self.assertIn("? help", text)
            for _ in range(300):
                h.handle_key(qix.curses.KEY_DOWN)
            scr, lay = self.render(new_game(), cols, rows, help=h)
            self.assertEqual(scr.violations, [], (cols, rows))
            # Scrolled to the very end: the last help line is on screen.
            self.assertIn("lines.", self.all_text(scr))

    def test_too_small_message(self):
        scr, lay = self.render(new_game(), 50, 20)
        self.assertTrue(lay.too_small)
        text = " ".join(t for _, _, t in scr.writes)
        self.assertIn("pane too small", text)


if __name__ == "__main__":
    unittest.main()
