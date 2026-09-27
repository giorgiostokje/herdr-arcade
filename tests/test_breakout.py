"""Unit tests for the pure game logic in breakout.py (no terminal needed)."""

import json
import math
import os
import random
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import breakout
from breakout import (
    BRICK_COLS,
    BRICK_ROWS,
    BRICK_TOP,
    BRICK_W,
    DEATH_TIME,
    FIELD_H,
    FIELD_W,
    Game,
    LEVEL_CLEAR_TIME,
    PADDLE_SMALL_W,
    PADDLE_W,
    PADDLE_Y,
    ROW_POINTS,
    SAVE_VERSION,
    base_speed,
    layout_plan,
)
from common import AgentSnapshot


def game(seed=1):
    return Game(random.Random(seed))


def place(g, x, y, vx, vy):
    """Free the ball at (x, y) with velocity (vx, vy)."""
    g.stuck = False
    g.ball_x, g.ball_y, g.vx, g.vy = float(x), float(y), float(vx), float(vy)


def clear_all(g):
    g.bricks = [[False] * BRICK_COLS for _ in range(BRICK_ROWS)]


BELOW_WALL = BRICK_TOP + BRICK_ROWS  # first open row under the bricks


class WallTest(unittest.TestCase):
    def test_left_wall_reflects_x(self):
        g = game()
        g.paddle_x = 30
        place(g, 1.5, 14.5, -10, 0)
        g.update(0.3)
        self.assertGreater(g.vx, 0)
        self.assertEqual(g.vy, 0)
        self.assertTrue(0 <= g.ball_x < FIELD_W)

    def test_right_wall_reflects_x(self):
        g = game()
        place(g, FIELD_W - 1.5, 14.5, 10, 0)
        g.update(0.3)
        self.assertLess(g.vx, 0)
        self.assertTrue(0 <= g.ball_x < FIELD_W)

    def test_ceiling_reflects_y_and_shrinks_paddle(self):
        g = game()
        place(g, 10.5, 1.5, 0, -10)
        g.update(0.3)
        self.assertGreater(g.vy, 0)
        self.assertEqual(g.vx, 0)
        self.assertGreaterEqual(g.ball_y, 0)
        self.assertEqual(g.paddle_w, PADDLE_SMALL_W)
        self.assertTrue(g.shrunk)
        self.assertTrue(0 <= g.paddle_x <= FIELD_W - g.paddle_w)

    def test_paddle_restored_on_serve(self):
        g = game()
        place(g, 10.5, 1.5, 0, -10)
        g.update(0.3)
        g.serve()
        self.assertEqual(g.paddle_w, PADDLE_W)
        self.assertFalse(g.shrunk)


class BrickTest(unittest.TestCase):
    def test_hit_from_below_removes_scores_flips_y(self):
        g = game()
        place(g, 5.5, BELOW_WALL + 0.5, 0, -10)
        g.update(0.1)
        bottom = BRICK_ROWS - 1
        self.assertFalse(g.bricks[bottom][1])
        self.assertEqual(g.bricks_left(), BRICK_ROWS * BRICK_COLS - 1)
        self.assertEqual(g.score, ROW_POINTS[bottom])
        self.assertGreater(g.vy, 0)
        self.assertEqual(g.vx, 0)
        self.assertGreaterEqual(g.ball_y, BELOW_WALL)

    def test_side_hit_flips_x_only(self):
        g = game()
        bottom = BRICK_ROWS - 1
        g.bricks[bottom][1] = False  # a pocket in the bottom row, cols 4-7
        y = BRICK_TOP + bottom + 0.5
        place(g, 6.5, y, 10, 0)
        g.update(0.2)
        self.assertFalse(g.bricks[bottom][2])  # the brick to the right
        self.assertLess(g.vx, 0)
        self.assertEqual(g.vy, 0)
        self.assertEqual(g.ball_y, y)
        self.assertEqual(g.score, ROW_POINTS[bottom])
        self.assertEqual(g.bricks_left(), BRICK_ROWS * BRICK_COLS - 2)

    def test_points_per_row(self):
        self.assertEqual(ROW_POINTS, (7, 7, 4, 4, 1, 1))  # top to bottom
        for r in range(BRICK_ROWS):
            g = game()
            clear_all(g)
            g.bricks[r][0] = True
            g.bricks[r][5] = True  # keep the level from clearing
            place(g, 1.5, BELOW_WALL + 0.5, 0, -10)
            g.update(1.0)
            self.assertFalse(g.bricks[r][0])
            self.assertEqual(g.score, ROW_POINTS[r])


class TunnellingTest(unittest.TestCase):
    def test_fast_ball_cannot_skip_single_brick_row(self):
        g = game()
        clear_all(g)
        row = 3
        g.bricks[row] = [True] * BRICK_COLS
        place(g, 13.5, BRICK_TOP + row + 8.5, 0, -1000)
        g.update(0.005)  # 5 cells of travel
        g.update(0.005)  # and 5 more: past the row, if it could tunnel
        self.assertEqual(sum(g.bricks[row]), BRICK_COLS - 1)
        self.assertFalse(g.bricks[row][13 // BRICK_W])
        self.assertGreater(g.vy, 0)
        self.assertGreater(g.ball_y, BRICK_TOP + row)

    def test_fast_ball_huge_dt_hits_exactly_one_brick_first(self):
        g = game()
        clear_all(g)
        row = 3
        g.bricks[row] = [True] * BRICK_COLS
        place(g, 13.5, PADDLE_Y - 1.5, 0, -2000)
        # 0.0075s = 15 cells: up 10.5 to the row, back down 4.5.
        g.update(0.0075)
        self.assertEqual(sum(g.bricks[row]), BRICK_COLS - 1)
        self.assertGreater(g.ball_y, BRICK_TOP + row)

    def test_fast_ball_cannot_pass_paddle(self):
        g = game()
        clear_all(g)
        g.bricks[0][0] = True
        centre = g.paddle_x + g.paddle_w / 2.0
        place(g, centre, 10.5, 0, 800)
        g.update(0.02)  # 16 cells of travel: down 8.5 then back up
        self.assertEqual(g.lives, 3)
        self.assertIsNone(g.phase)
        self.assertLess(g.vy, 0)
        self.assertLess(g.ball_y, PADDLE_Y)

    def test_diagonal_fast_ball_stays_in_field(self):
        g = game()
        place(g, 20.5, 15.5, 700, -700)
        for _ in range(50):
            g.update(0.01)
            if g.phase is not None or g.game_over:
                break
            self.assertTrue(0 <= g.ball_x < FIELD_W)
            self.assertTrue(0 <= g.ball_y < FIELD_H)
            cell = g.brick_at(math.floor(g.ball_x), math.floor(g.ball_y))
            self.assertIsNone(cell)  # never inside a live brick


class PaddleTest(unittest.TestCase):
    def bounce_at(self, offset):
        g = game()
        centre = g.paddle_x + g.paddle_w / 2.0
        place(g, centre + offset, PADDLE_Y - 0.2, 0, 10)
        g.update(0.05)
        self.assertLess(g.vy, 0)
        self.assertAlmostEqual(math.hypot(g.vx, g.vy), g.speed)
        return g.vx

    def test_offset_changes_angle(self):
        left = self.bounce_at(-3.0)
        mid = self.bounce_at(0.0)
        right = self.bounce_at(3.0)
        self.assertLess(left, 0)
        self.assertAlmostEqual(mid, 0.0)
        self.assertGreater(right, 0)
        self.assertLess(self.bounce_at(-1.0), 0)
        self.assertLess(abs(self.bounce_at(1.0)), abs(right))

    def test_paddle_clamped(self):
        g = game()
        for _ in range(30):
            g.move_paddle(-1)
        self.assertEqual(g.paddle_x, 0)
        self.assertFalse(g.move_paddle(-1))
        for _ in range(30):
            g.move_paddle(1)
        self.assertEqual(g.paddle_x, FIELD_W - g.paddle_w)
        self.assertFalse(g.move_paddle(1))

    def test_step_per_press(self):
        g = game()
        x0 = g.paddle_x
        g.move_paddle(1)
        self.assertEqual(g.paddle_x, x0 + breakout.PADDLE_STEP)

    def test_miss_beside_paddle_is_not_caught(self):
        g = game()
        g.paddle_x = 30
        place(g, 5.5, PADDLE_Y - 0.5, 0, 10)
        g.update(0.1)
        self.assertGreater(g.vy, 0)
        self.assertGreater(g.ball_y, PADDLE_Y)


class ServeTest(unittest.TestCase):
    def test_ball_rides_paddle_until_launch(self):
        g = game()
        self.assertTrue(g.stuck)
        centre = g.paddle_x + g.paddle_w / 2.0
        self.assertEqual(g.ball_x, centre)
        self.assertLess(g.ball_y, PADDLE_Y)
        self.assertFalse(g.update(1.0))
        self.assertEqual(g.ball_x, centre)
        g.move_paddle(-1)
        g.move_paddle(-1)
        self.assertEqual(g.ball_x, g.paddle_x + g.paddle_w / 2.0)

    def test_launch(self):
        g = game()
        self.assertTrue(g.launch())
        self.assertFalse(g.stuck)
        self.assertLess(g.vy, 0)
        self.assertNotEqual(g.vx, 0)
        self.assertAlmostEqual(math.hypot(g.vx, g.vy), g.speed)
        self.assertFalse(g.launch())  # already in play
        y0 = g.ball_y
        g.update(0.1)
        self.assertLess(g.ball_y, y0)
        x0 = g.ball_x
        g.move_paddle(1)
        self.assertEqual(g.ball_x, x0)  # a free ball ignores the paddle

    def test_paused_game_does_not_move_or_launch(self):
        g = game()
        g.paused = True
        self.assertFalse(g.launch())
        self.assertFalse(g.move_paddle(1))


class LifeTest(unittest.TestCase):
    def lose(self, g):
        g.paddle_x = 30
        place(g, 2.5, PADDLE_Y + 0.5, 0, 10)
        g.update(0.2)

    def test_ball_lost_costs_life_then_reserves(self):
        g = game()
        self.lose(g)
        self.assertEqual(g.lives, 2)
        self.assertIsNotNone(g.phase)
        self.assertEqual(g.phase.kind, "death")
        self.assertFalse(g.game_over)
        g.update(DEATH_TIME / 2)
        self.assertIsNotNone(g.phase)
        g.update(DEATH_TIME)
        self.assertIsNone(g.phase)
        self.assertTrue(g.stuck)
        self.assertEqual(g.paddle_w, PADDLE_W)
        self.assertEqual(g.ball_x, g.paddle_x + g.paddle_w / 2.0)

    def test_game_over_on_last_life(self):
        g = game()
        g.lives = 1
        self.lose(g)
        self.assertTrue(g.game_over)
        self.assertEqual(g.lives, 0)
        self.assertIsNone(g.phase)
        self.assertFalse(g.update(0.5))
        self.assertFalse(g.launch())
        self.assertFalse(g.move_paddle(1))

    def test_three_losses_end_the_game(self):
        g = game()
        for _ in range(3):
            self.lose(g)
            g.finish_phase()
        self.assertTrue(g.game_over)

    def test_reset(self):
        g = game()
        g.lives = 1
        self.lose(g)
        g.reset()
        self.assertFalse(g.game_over)
        self.assertEqual(g.lives, 3)
        self.assertEqual(g.score, 0)
        self.assertEqual(g.bricks_left(), BRICK_ROWS * BRICK_COLS)


class LevelTest(unittest.TestCase):
    def test_level_clear_then_next_level(self):
        g = game()
        clear_all(g)
        g.bricks[BRICK_ROWS - 1][0] = True
        place(g, 1.5, BELOW_WALL + 0.5, 0, -10)
        g.update(0.2)
        self.assertEqual(g.bricks_left(), 0)
        self.assertIsNotNone(g.phase)
        self.assertEqual(g.phase.kind, "level")
        self.assertEqual(g.level, 1)
        g.update(LEVEL_CLEAR_TIME + 0.01)
        self.assertIsNone(g.phase)
        self.assertEqual(g.level, 2)
        self.assertEqual(g.bricks_left(), BRICK_ROWS * BRICK_COLS)
        self.assertTrue(g.stuck)
        self.assertEqual(g.lives, 3)
        self.assertGreater(base_speed(2), base_speed(1))
        g.launch()
        self.assertAlmostEqual(math.hypot(g.vx, g.vy), base_speed(2))

    def test_speed_up_after_hits_and_back_rows(self):
        g = game()
        g.launch()
        speeds = [g.speed]
        bottom = BRICK_ROWS - 1
        for c in range(4):
            g._hit_brick(bottom, c)
            speeds.append(g.speed)
        self.assertEqual(speeds[0], speeds[3])   # hits 1-3: no change
        self.assertGreater(speeds[4], speeds[3])  # 4th hit
        g._hit_brick(3, 0)  # first orange-row hit
        self.assertGreater(g.speed, speeds[4])
        s = g.speed
        g._hit_brick(3, 1)  # second orange hit: no further boost
        self.assertEqual(g.speed, s)
        g._hit_brick(0, 0)  # first red-row hit
        self.assertGreater(g.speed, s)
        self.assertAlmostEqual(math.hypot(g.vx, g.vy), g.speed)


class SaveRestoreTest(unittest.TestCase):
    def played(self):
        g = game(7)
        g.launch()
        for _ in range(40):
            g.update(0.03)
        g.finish_phase()
        return g

    def roundtrip(self, g):
        return Game.from_save(json.loads(json.dumps(g.to_save())))

    def test_round_trip_is_deterministic(self):
        g = self.played()
        r = self.roundtrip(g)
        self.assertIsNotNone(r)
        self.assertEqual(r.to_save(), g.to_save())
        for _ in range(60):
            g.update(0.02)
            r.update(0.02)
            g.finish_phase()
            r.finish_phase()
        self.assertEqual(r.to_save(), g.to_save())
        # RNG state carried over: the next launch angle matches too.
        g.serve()
        r.serve()
        g.launch()
        r.launch()
        self.assertEqual((g.vx, g.vy), (r.vx, r.vy))

    def test_stuck_ball_round_trip(self):
        g = game(3)
        g.move_paddle(1)
        r = self.roundtrip(g)
        self.assertTrue(r.stuck)
        self.assertEqual(r.ball_x, r.paddle_x + r.paddle_w / 2.0)

    def test_corrupt_or_incompatible_is_none(self):
        good = self.played().to_save()
        self.assertIsNotNone(Game.from_save(good))

        def bad(**changes):
            d = json.loads(json.dumps(good))
            d.update(changes)
            return d

        cases = [
            None, [], "x", {},
            bad(version=SAVE_VERSION + 1),
            bad(version=0),
            bad(rng=[3, [1, 2], None]),
            bad(rng="nope"),
            bad(bricks=[[True] * BRICK_COLS] * (BRICK_ROWS - 1)),
            bad(bricks=[[True] * (BRICK_COLS + 1)] * BRICK_ROWS),
            bad(bricks=[[1] * BRICK_COLS] * BRICK_ROWS),
            bad(bricks=[[False] * BRICK_COLS] * BRICK_ROWS),
            bad(lives=0),
            bad(level=0),
            bad(score="12"),
            bad(paddle_x=FIELD_W),
            bad(paddle_w=9),
            bad(ball=[FIELD_W + 1, 5, 1, 1]),
            bad(ball=[5, 5, 1]),
            bad(ball=[5, 5, 0, 0], stuck=False),
            bad(stuck="yes"),
        ]
        missing = json.loads(json.dumps(good))
        del missing["hits"]
        cases.append(missing)
        for data in cases:
            self.assertIsNone(Game.from_save(data), data if not isinstance(
                data, dict) else {k: data[k] for k in data if k != "rng"})

    def test_finish_phase_resolves_death_before_save(self):
        g = game()
        g.paddle_x = 30
        place(g, 2.5, PADDLE_Y + 0.5, 0, 10)
        g.update(0.2)
        self.assertIsNotNone(g.phase)
        g.finish_phase()
        r = self.roundtrip(g)
        self.assertIsNotNone(r)
        self.assertTrue(r.stuck)
        self.assertEqual(r.lives, 2)


REGIONS = ("field", "stats", "controls", "agents", "hint")


class LayoutPlanTest(unittest.TestCase):
    def fits(self, plan, cols, rows):
        for name in REGIONS:
            region = plan[name]
            if region is None:
                continue
            y, x, h, w = region
            self.assertGreaterEqual(y, 0, name)
            self.assertGreaterEqual(x, 0, name)
            self.assertLessEqual(x + w, cols, name)
            self.assertLessEqual(y + h, rows, name)

    def composition(self, plan):
        """(top, left, bottom, right) of the field plus side column."""
        boxes = [plan[n] for n in ("field", "stats", "controls", "agents")
                 if plan[n] is not None]
        return (min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[0] + b[2] for b in boxes), max(b[1] + b[3] for b in boxes))

    def assert_centred(self, plan, cols, rows):
        top, left, bottom, right = self.composition(plan)
        self.assertLessEqual(abs(left - (cols - right)), 1, (cols, rows))
        # Vertically: centred below the header row, and above the hint row
        # when the hint has a row of its own at the bottom.
        first = 1 if plan["header"] else 0
        last = rows
        if plan["hint"] is not None and plan["hint"][0] == rows - 1 and \
                plan["hint"][0] >= bottom:
            last = rows - 1
        self.assertLessEqual(abs((top - first) - (last - bottom)), 1, (cols, rows))

    def test_full_at_new_popup_size(self):
        plan = layout_plan(93, 28)
        self.assertEqual(plan["tier"], "full")
        self.assertTrue(plan["header"])
        for name in ("stats", "controls", "agents"):
            self.assertIsNotNone(plan[name], name)
            self.assertEqual(plan[name][3], 30, name)
        self.assertIsNone(plan["hint"])
        self.fits(plan, 93, 28)
        self.assert_centred(plan, 93, 28)
        # Stacked stats / controls / agents down to the field's bottom edge.
        fy, fx, fh, fw = plan["field"]
        sy, sx, sh, _ = plan["stats"]
        cy, _, ch, _ = plan["controls"]
        ay, _, ah, _ = plan["agents"]
        self.assertEqual(sx, fx + fw + 1)
        self.assertEqual((sy, cy, ay), (fy, sy + sh, cy + ch))
        self.assertEqual(ay + ah, fy + fh)
        self.assertEqual(ch, breakout.controls_height(breakout.CONTROLS, 30))

    def test_full_at_old_popup_size(self):
        plan = layout_plan(77, 24)
        self.assertEqual(plan["tier"], "full")
        self.assertTrue(plan["header"])
        for name in ("stats", "controls", "agents"):
            self.assertIsNotNone(plan[name], name)
        self.assertEqual(plan["field"], (1, 0, FIELD_H + 2, FIELD_W + 2))
        self.fits(plan, 77, 24)
        self.assert_centred(plan, 77, 24)

    def test_degrading_tiers(self):
        p = layout_plan(66, 24)  # 66 - 47 = 19: too narrow for a side column
        self.assertEqual(p["tier"], "field")
        self.assertIsNone(p["stats"])
        self.assertIsNone(p["controls"])
        self.assertEqual(p["hint"][0], 0)  # shares the header row
        self.assertEqual(layout_plan(67, 24)["controls"][3], 20)
        p = layout_plan(50, 26)
        self.assertEqual(p["tier"], "field")
        self.assertEqual(p["hint"], (25, 0, 1, 50))  # a row of its own
        p = layout_plan(46, 23)
        self.assertEqual(p["tier"], "field")
        self.assertFalse(p["header"])
        self.assertEqual(p["hint"], (22, 0, 1, 46))
        self.assertTrue(layout_plan(45, 24)["too_small"])
        self.assertTrue(layout_plan(77, 22)["too_small"])
        self.assertTrue(layout_plan(40, 20)["too_small"])

    def test_every_size_fits_and_is_centred(self):
        for cols in range(46, 130):
            for rows in (23, 24, 25, 26, 28, 30, 40):
                plan = layout_plan(cols, rows)
                self.fits(plan, cols, rows)
                self.assert_centred(plan, cols, rows)
                if plan["controls"] is None:
                    self.assertIsNotNone(plan["hint"], (cols, rows))
                else:
                    self.assertIsNotNone(plan["stats"], (cols, rows))


class ConstantsTest(unittest.TestCase):
    def test_controls_cover_the_keys(self):
        keys = " ".join(k for k, _ in breakout.CONTROLS)
        for key in ("?", "q", "p", "r", "space", "<-", "->", "a", "d", "h",
                    "l", "up", "w", "k"):
            self.assertIn(key, keys.split(), key)
        self.assertEqual([k for k, _ in breakout.CONTROLS[-4:]],
                         ["?", "p", "r", "q"])

    def test_help_sections(self):
        heads = [h for h, _ in breakout.HELP]
        for need in ("Goal", "Controls", "How it works", "Scoring", "Tips"):
            self.assertIn(need, heads)
        for head, body in breakout.HELP:
            self.assertTrue(body.strip(), head)
            body.encode("ascii")  # plain ASCII

    def test_key_hint_always_offers_help(self):
        for width in (20, 30, 45, 60, 200):
            hint = breakout.key_hint(width)
            self.assertLessEqual(len(hint), width)
            self.assertIn("? help", hint)


class HelpKeyTest(unittest.TestCase):
    def setUp(self):
        self.help = breakout.HelpOverlay(breakout.TITLE, breakout.HELP)

    def test_help_pauses_and_closing_resumes(self):
        g = game()
        g.launch()
        consumed, pbh = breakout.help_key(self.help, g, ord("?"), False)
        self.assertTrue(consumed)
        self.assertTrue(pbh)
        self.assertTrue(self.help.is_open)
        self.assertTrue(g.paused)
        pos = (g.ball_x, g.ball_y)
        self.assertFalse(g.update(0.5))  # no time passes under the help
        self.assertEqual((g.ball_x, g.ball_y), pos)
        # Scroll keys keep it open; game keys never reach the game.
        consumed, pbh = breakout.help_key(self.help, g, breakout.curses.KEY_DOWN, pbh)
        self.assertTrue(consumed and self.help.is_open and g.paused)
        consumed, pbh = breakout.help_key(self.help, g, ord("r"), pbh)
        self.assertTrue(consumed)
        self.assertFalse(self.help.is_open)
        self.assertFalse(pbh)
        self.assertFalse(g.paused)
        self.assertTrue(g.update(0.05))

    def test_resize_passes_through(self):
        g = game()
        breakout.help_key(self.help, g, ord("?"), False)
        consumed, pbh = breakout.help_key(self.help, g, breakout.curses.KEY_RESIZE, True)
        self.assertFalse(consumed)
        self.assertTrue(self.help.is_open and pbh and g.paused)

    def test_help_from_paused_stays_paused(self):
        g = game()
        g.paused = True
        consumed, pbh = breakout.help_key(self.help, g, ord("?"), False)
        self.assertTrue(consumed and self.help.is_open)
        self.assertFalse(pbh)
        breakout.help_key(self.help, g, ord("x"), pbh)
        self.assertFalse(self.help.is_open)
        self.assertTrue(g.paused)

    def test_help_from_game_over(self):
        g = game()
        g.game_over = True
        consumed, pbh = breakout.help_key(self.help, g, ord("?"), False)
        self.assertTrue(consumed and self.help.is_open)
        self.assertFalse(pbh)
        self.assertFalse(g.paused)

    def test_other_keys_fall_through_when_closed(self):
        g = game()
        consumed, pbh = breakout.help_key(self.help, g, ord("p"), False)
        self.assertFalse(consumed)
        self.assertFalse(g.paused)


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
            raise breakout.curses.error()
        child = FakeWin(h, w, self.root, self.y0 + y, self.x0 + x)
        self.root.children.append(child)
        return child

    def erase(self):
        self.writes = []

    def addstr(self, y, x, text, attr=0):
        # The bottom-right cell is the one legal "overflow": curses writes it
        # but raises; frame() relies on safe_addstr there.
        if y < 0 or y >= self.rows or x < 0 or x + len(text) > self.cols:
            self.root.violations.append((y, x, text))
            raise breakout.curses.error()
        self.writes.append((y, x, text))

    def text(self):
        wins = [self] + self.children
        return "\n".join(t for w in wins for _, _, t in w.writes)


class RenderTest(unittest.TestCase):
    def session(self, attention=None):
        roster = [AgentSnapshot("p%d" % i, "agent%d" % i, "omp", s)
                  for i, s in enumerate(["idle", "working", "blocked", "done"] * 5)]
        return types.SimpleNamespace(
            attention=attention, roster=roster,
            status_pairs={s: i + 1 for i, s in
                          enumerate(("idle", "working", "blocked", "done", "unknown"))},
        )

    def render(self, g, cols=77, rows=24, attention=None, resume=False,
               help_open=False):
        scr = FakeWin(rows, cols)
        s = self.session(attention)
        overlay = breakout.HelpOverlay(breakout.TITLE, breakout.HELP)
        if help_open:
            overlay.open()
        with mock.patch("curses.color_pair", return_value=0):
            lay = breakout.Layout(scr)
            breakout.draw(s.status_pairs, g, lay, s.roster, s.attention,
                          resume, best=123, help_overlay=overlay)
        self.assertEqual(scr.violations, [])
        return scr

    def test_full_frame_states(self):
        g = game()
        scr = self.render(g)
        self.assertIn("space: launch", scr.text())
        self.assertIn("score 0", scr.text())
        self.assertIn(" agents ", scr.text())
        g.launch()
        g.update(0.2)
        self.render(g)
        g.paused = True
        self.assertIn("PAUSED", self.render(g).text())
        g.paused = False
        g.phase = breakout.Phase("death", DEATH_TIME)
        g.phase.elapsed = 0.5
        self.render(g)
        g.phase = breakout.Phase("level", LEVEL_CLEAR_TIME)
        self.render(g)
        g.phase = None
        g.game_over = True
        text = self.render(g).text()
        self.assertIn("GAME OVER", text)
        self.assertIn("r restart | q menu", text)

    def test_ball_at_field_corners(self):
        g = game()
        for x, y in ((0.0, 0.0), (FIELD_W - 0.01, 0.0), (0.0, FIELD_H - 0.01),
                     (FIELD_W - 0.01, FIELD_H - 0.01)):
            place(g, x, y, 1, 1)
            self.render(g)

    def test_small_paddle_at_right_edge(self):
        g = game()
        g.paddle_w = PADDLE_SMALL_W
        g.paddle_x = FIELD_W - PADDLE_SMALL_W
        self.render(g)

    def test_banners_and_tiers(self):
        g = game()
        att = (AgentSnapshot("p1", "agent1", "omp", "blocked"), "working")
        self.render(g, attention=att)
        self.render(g, resume=True)
        for cols, rows in ((62, 24), (50, 24), (46, 23), (100, 40), (30, 10)):
            self.render(g, cols, rows)
        self.assertIn("pane too small", self.render(g, 30, 10).text())
        self.assertIn("0 pts", self.render(g, 50, 24).text())

    def test_help_open_at_every_tier(self):
        g = game()
        for cols, rows in ((93, 28), (77, 24), (50, 24), (50, 26), (46, 23),
                           (30, 10)):
            scr = self.render(g, cols, rows, help_open=True)
            text = scr.text()
            self.assertIn("Breakout - help", text, (cols, rows))
            self.assertIn("Goal", text, (cols, rows))
            if (cols, rows) != (30, 10):
                self.assertRegex(text, r"\?\s+help", (cols, rows))

    def test_controls_box_and_hint_on_screen(self):
        g = game()
        scr = self.render(g, 93, 28)
        text = scr.text()
        self.assertIn(" keys ", text)
        self.assertIn("launch ball", text)
        self.assertIn(" breakout ", text)  # stats box titled with the game
        for cols, rows in ((50, 24), (50, 26), (46, 23)):
            text = self.render(g, cols, rows).text()
            self.assertIn("? help", text, (cols, rows))


if __name__ == "__main__":
    unittest.main()
