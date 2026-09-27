"""Unit tests for the pure game logic, layout and rendering in snake.py (no
terminal needed)."""

import json
import os
import random
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import snake  # noqa: E402
from common import AgentSnapshot  # noqa: E402
from snake import (  # noqa: E402
    DOWN,
    FIELD_H,
    FIELD_W,
    FOOD_POINTS,
    FOODS_PER_LEVEL,
    GROW,
    LAYOUTS,
    LEFT,
    LIVES,
    RIGHT,
    START_HEAD,
    START_LEN,
    UP,
    Game,
    layout_plan,
    step_interval,
    walls_for,
)


def new_game(seed=1):
    return Game(random.Random(seed))


def tick(game, n=1):
    """Advance exactly n snake steps (outside any phase)."""
    for _ in range(n):
        game.update(step_interval(game.level))


def finish_phase(game):
    game.update(10.0)


class LayoutsTest(unittest.TestCase):
    def test_layouts_have_field_size_and_clear_start(self):
        self.assertGreaterEqual(len(LAYOUTS), 4)
        for name, rows in LAYOUTS:
            self.assertEqual(len(rows), FIELD_H, name)
            for row in rows:
                self.assertEqual(len(row), FIELD_W, name)
                self.assertTrue(set(row) <= {"#", "."}, name)
        for level in range(1, len(LAYOUTS) + 1):
            walls = walls_for(level)
            hx, hy = START_HEAD
            for x in range(hx - START_LEN, hx + 6):  # snake plus runway
                self.assertNotIn((x, hy), walls, level)

    def test_level_one_is_open(self):
        self.assertEqual(walls_for(1), frozenset())


class MovementTest(unittest.TestCase):
    def test_one_cell_per_tick(self):
        g = new_game()
        self.assertEqual(len(g.snake), START_LEN)
        head = g.snake[0]
        g.update(step_interval(1) / 2)
        self.assertEqual(g.snake[0], head)  # half a tick: no move
        g.update(step_interval(1) / 2 + 1e-9)
        self.assertEqual(g.snake[0], (head[0] + 1, head[1]))
        tick(g, 3)
        self.assertEqual(g.snake[0], (head[0] + 4, head[1]))
        self.assertEqual(len(g.snake), START_LEN)

    def test_paused_does_not_move(self):
        g = new_game()
        head = g.snake[0]
        g.paused = True
        self.assertFalse(g.update(1.0))
        self.assertEqual(g.snake[0], head)


class TurnQueueTest(unittest.TestCase):
    def test_two_quick_turns_both_apply(self):
        g = new_game()
        hx, hy = g.snake[0]
        self.assertTrue(g.set_direction(UP))
        self.assertTrue(g.set_direction(LEFT))  # left vs queued up: legal
        tick(g)
        self.assertEqual(g.snake[0], (hx, hy - 1))
        tick(g)
        self.assertEqual(g.snake[0], (hx - 1, hy - 1))
        self.assertEqual(g.dir, LEFT)
        self.assertIsNone(g.phase)

    def test_reverse_ignored(self):
        g = new_game()
        hx, hy = g.snake[0]
        self.assertFalse(g.set_direction(LEFT))  # straight back into the neck
        tick(g)
        self.assertEqual(g.snake[0], (hx + 1, hy))
        self.assertIsNone(g.phase)

    def test_reverse_of_queued_turn_ignored(self):
        g = new_game()
        self.assertTrue(g.set_direction(UP))
        self.assertFalse(g.set_direction(DOWN))
        self.assertFalse(g.set_direction(UP))  # repeat
        self.assertEqual(g.queue, [UP])

    def test_queue_is_capped(self):
        g = new_game()
        for d in (UP, LEFT, DOWN):
            self.assertTrue(g.set_direction(d))
        self.assertFalse(g.set_direction(RIGHT))
        self.assertEqual(len(g.queue), snake.MAX_QUEUE)


class FoodTest(unittest.TestCase):
    def test_growth_and_score(self):
        g = new_game()
        hx, hy = g.snake[0]
        g.food = (hx + 1, hy)
        tick(g)
        self.assertEqual(g.score, FOOD_POINTS * 1)
        self.assertEqual(g.foods, 1)
        self.assertEqual(g.pending_growth, GROW)
        self.assertNotIn(g.food, g.snake)
        tick(g, GROW)
        self.assertEqual(len(g.snake), START_LEN + GROW)
        tick(g, 2)
        self.assertEqual(len(g.snake), START_LEN + GROW)

    def test_points_scale_with_level(self):
        g = new_game()
        g.level = 3
        hx, hy = g.snake[0]
        g.food = (hx + 1, hy)
        g.step()
        self.assertEqual(g.score, FOOD_POINTS * 3)

    def test_food_never_on_snake_or_walls(self):
        for level in range(1, len(LAYOUTS) + 1):
            g = new_game(level)
            g.level = level
            g._start_level()
            # A long snake across the field makes collisions likely.
            g.snake = [(x, 10) for x in range(FIELD_W - 1, -1, -1)]
            for _ in range(300):
                g.spawn_food()
                self.assertIsNotNone(g.food)
                self.assertNotIn(g.food, g.snake)
                self.assertNotIn(g.food, g.walls)

    def test_food_spawn_is_seedable(self):
        a, b = new_game(9), new_game(9)
        self.assertEqual(a.food, b.food)
        for _ in range(20):
            a.spawn_food()
            b.spawn_food()
            self.assertEqual(a.food, b.food)

    def test_bonus_appears_and_times_out(self):
        g = new_game()
        g.eaten_total = snake.BONUS_EVERY - 1
        hx, hy = g.snake[0]
        g.food = (hx + 1, hy)
        g.step()
        self.assertIsNotNone(g.bonus)
        self.assertNotIn((g.bonus[0], g.bonus[1]), g.snake)
        g.bonus[2] = 0.01
        g.snake = [(0, 20), (1, 20)]  # out of harm's way
        g.dir = UP
        g.update(0.02)
        self.assertIsNone(g.bonus)


class DeathTest(unittest.TestCase):
    def test_wall_death_at_border(self):
        g = new_game()
        g.snake = [(FIELD_W - 1, 10), (FIELD_W - 2, 10), (FIELD_W - 3, 10)]
        tick(g)
        self.assertIsNotNone(g.phase)
        self.assertEqual(g.phase.kind, "dying")
        self.assertEqual(g.lives, LIVES - 1)

    def test_interior_wall_death(self):
        g = new_game()
        g.level = 2
        g._start_level()
        wall = min(g.walls)
        x, y = wall
        g.snake = [(x - 1, y), (x - 2, y), (x - 3, y)]
        g.dir = RIGHT
        g.food = None
        g.step()
        self.assertEqual(g.phase.kind, "dying")

    def test_self_death(self):
        g = new_game()
        # Head at (5,5) moving down into its own body at (5,6).
        g.snake = [(5, 5), (6, 5), (6, 6), (5, 6), (4, 6), (3, 6)]
        g.dir = DOWN
        g.food = None
        g.step()
        self.assertEqual(g.phase.kind, "dying")

    def test_moving_into_vacating_tail_is_legal(self):
        g = new_game()
        # A 2x2 loop: head (5,5) down to (5,6), which is the tail's cell.
        g.snake = [(5, 5), (6, 5), (6, 6), (5, 6)]
        g.dir = DOWN
        g.food = None
        g.step()
        self.assertIsNone(g.phase)
        self.assertEqual(g.snake[0], (5, 6))
        self.assertEqual(len(g.snake), 4)

    def test_moving_into_tail_while_growing_kills(self):
        g = new_game()
        g.snake = [(5, 5), (6, 5), (6, 6), (5, 6)]
        g.dir = DOWN
        g.food = None
        g.pending_growth = 1
        g.step()
        self.assertEqual(g.phase.kind, "dying")

    def test_death_flash_then_respawn_at_start(self):
        g = new_game()
        g.snake = [(FIELD_W - 1, 3), (FIELD_W - 2, 3), (FIELD_W - 3, 3),
                   (FIELD_W - 4, 3), (FIELD_W - 5, 3), (FIELD_W - 6, 3)]
        g.set_direction(UP)
        g.queue = []
        tick(g)
        self.assertEqual(g.phase.kind, "dying")
        self.assertFalse(g.set_direction(UP))  # input ignored while dying
        g.update(snake.DEATH_TIME / 2)
        self.assertIsNotNone(g.phase)
        g.update(snake.DEATH_TIME)
        self.assertIsNone(g.phase)
        self.assertEqual(g.snake, snake.start_snake())
        self.assertEqual(g.dir, RIGHT)
        self.assertNotIn(g.food, g.snake)

    def test_lives_and_game_over(self):
        g = new_game()
        for expected in range(LIVES - 1, -1, -1):
            g.snake = [(FIELD_W - 1, 10), (FIELD_W - 2, 10)]
            g.dir = RIGHT
            tick(g)
            self.assertEqual(g.lives, expected)
            finish_phase(g)
        self.assertTrue(g.game_over)
        self.assertFalse(g.update(1.0))


class LevelTest(unittest.TestCase):
    def test_level_progression_and_layout_switch(self):
        g = new_game()
        walls1 = g.walls
        for i in range(FOODS_PER_LEVEL):
            hx, hy = g.snake[0]
            g.food = (hx + 1, hy)
            if hx + 1 >= FIELD_W - 1:
                break
            tick(g)
        self.assertEqual(g.foods, FOODS_PER_LEVEL)
        self.assertEqual(g.phase.kind, "level_clear")
        score = g.score
        finish_phase(g)
        self.assertEqual(g.level, 2)
        self.assertEqual(g.foods, 0)
        self.assertEqual(g.score, score)
        self.assertNotEqual(g.walls, walls1)
        self.assertEqual(g.walls, walls_for(2))
        self.assertEqual(len(g.snake), START_LEN)
        self.assertEqual(g.pending_growth, 0)
        self.assertEqual(g.snake, snake.start_snake())
        self.assertNotIn(g.food, g.walls)
        self.assertNotIn(g.food, g.snake)

    def test_every_level_has_a_distinct_layout_then_cycles(self):
        seen = [walls_for(level) for level in range(1, len(LAYOUTS) + 1)]
        self.assertEqual(len(set(seen)), len(LAYOUTS))
        self.assertEqual(walls_for(len(LAYOUTS) + 1), walls_for(1))

    def test_speed_increases(self):
        for level in range(1, 10):
            self.assertLess(step_interval(level + 1), step_interval(level))
        self.assertGreaterEqual(step_interval(100), 0.05)


class SaveTest(unittest.TestCase):
    def advanced(self):
        g = new_game(5)
        g.set_direction(DOWN)
        tick(g, 3)
        hx, hy = g.snake[0]
        g.food = (hx, hy + 1)
        tick(g)
        g.set_direction(RIGHT)
        return g

    def test_round_trip_including_rng(self):
        g = self.advanced()
        data = json.loads(json.dumps(g.to_save()))
        r = Game.from_save(data)
        self.assertIsNotNone(r)
        for attr in ("score", "level", "lives", "foods", "eaten_total", "snake",
                     "dir", "queue", "pending_growth", "food", "bonus"):
            self.assertEqual(getattr(r, attr), getattr(g, attr), attr)
        self.assertAlmostEqual(r.step_timer, g.step_timer)
        # Same RNG state: every future food spawn matches.
        for _ in range(10):
            g.spawn_food()
            r.spawn_food()
            self.assertEqual(g.food, r.food)

    def test_corrupt_or_incompatible_is_none(self):
        good = self.advanced().to_save()
        self.assertIsNone(Game.from_save(None))
        self.assertIsNone(Game.from_save({}))
        self.assertIsNone(Game.from_save("junk"))
        bad = dict(good, version=snake.SAVE_VERSION + 1)
        self.assertIsNone(Game.from_save(bad))
        bad = dict(good, rng=[3, [1, 2], None])
        self.assertIsNone(Game.from_save(bad))
        bad = dict(good, snake=[[0, 0], [5, 5]])  # not contiguous
        self.assertIsNone(Game.from_save(bad))
        bad = dict(good, snake=[[FIELD_W, 0]])  # out of bounds
        self.assertIsNone(Game.from_save(bad))
        bad = dict(good, dir="sideways")
        self.assertIsNone(Game.from_save(bad))
        bad = dict(good, level=2, snake=[[4, 5], [5, 5]])  # on a level-2 wall
        self.assertIsNone(Game.from_save(bad))
        bad = dict(good, food=list(good["snake"][0]))  # food on the snake
        self.assertIsNone(Game.from_save(bad))
        bad = dict(good)
        del bad["lives"]
        self.assertIsNone(Game.from_save(bad))

    def test_phase_resolved_before_save(self):
        g = new_game()
        g.snake = [(FIELD_W - 1, 10), (FIELD_W - 2, 10)]
        tick(g)
        self.assertIsNotNone(g.phase)
        g.resolve_phases()
        r = Game.from_save(json.loads(json.dumps(g.to_save())))
        self.assertIsNotNone(r)
        self.assertEqual(r.snake, snake.start_snake())
        self.assertEqual(r.lives, LIVES - 1)


REGION_NAMES = ("field", "stats", "controls", "agents", "hint")


class LayoutPlanTest(unittest.TestCase):
    def assert_fits(self, plan, cols, rows):
        for name in REGION_NAMES:
            region = plan[name]
            if region is None:
                continue
            y, x, h, w = region
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(x + w, cols, name)
            self.assertLessEqual(y + h, rows, name)

    def assert_centred(self, plan, cols, rows):
        boxes = [plan[n] for n in REGION_NAMES[:-1] if plan[n] is not None]
        left = min(x for _, x, _, _ in boxes)
        right = cols - max(x + w for _, x, _, w in boxes)
        self.assertLessEqual(abs(left - right), 1, (cols, rows, left, right))
        top = min(y for y, _, _, _ in boxes)
        bottom_edge = rows - 1 if plan["hint"] is not None else rows
        bottom = bottom_edge - max(y + h for y, _, h, _ in boxes)
        self.assertGreaterEqual(top, 1)  # row 0 is the header / banner
        self.assertLessEqual(abs((top - 1) - bottom), 1, (cols, rows, top, bottom))

    def test_full_at_popup_size(self):
        plan = layout_plan(93, 28)
        self.assertEqual(plan["tier"], "full")
        for name in ("stats", "controls", "agents"):
            self.assertIsNotNone(plan[name], name)
            self.assertEqual(plan[name][3], 30, name)
        self.assertIsNone(plan["hint"])
        self.assert_fits(plan, 93, 28)
        self.assert_centred(plan, 93, 28)
        fy, fx, fh, fw = plan["field"]
        sy, sx, sh, _ = plan["stats"]
        cy, cx, ch, _ = plan["controls"]
        ay, ax, ah, _ = plan["agents"]
        self.assertEqual(sx, fx + fw + 1)
        self.assertEqual((sy, cy, ay), (fy, sy + sh, cy + ch))
        self.assertEqual(ay + ah, fy + fh)  # down to the field's bottom edge
        self.assertGreaterEqual(ch, snake.controls_height(snake.CONTROLS, 30))

    def test_full_at_77x24(self):
        plan = layout_plan(77, 24)
        self.assertEqual(plan["tier"], "full")
        self.assertIsNotNone(plan["stats"])
        self.assertIsNotNone(plan["controls"])
        self.assertIsNotNone(plan["agents"])
        self.assertGreaterEqual(plan["agents"][2], 4)  # room for one agent
        self.assert_fits(plan, 77, 24)
        self.assert_centred(plan, 77, 24)
        _, _, fh, fw = plan["field"]
        self.assertEqual((fh, fw), (FIELD_H + 2, FIELD_W * 2 + 2))
        _, _, ch, cw = plan["controls"]
        self.assertGreaterEqual(ch, snake.controls_height(snake.CONTROLS, cw))
        self.assertLessEqual(FIELD_H, 21)
        self.assertLessEqual(FIELD_W * 2, 52)

    def test_degrading_tiers(self):
        plan = layout_plan(70, 24)
        self.assertEqual(plan["tier"], "field")
        self.assertIsNone(plan["stats"])
        self.assertIsNone(plan["controls"])
        self.assertTrue(plan["hint_in_header"])  # no spare row for the hint
        self.assert_fits(plan, 70, 24)
        self.assert_centred(plan, 70, 24)
        plan = layout_plan(60, 26)
        self.assertEqual(plan["tier"], "field")
        self.assertEqual(plan["hint"], (25, 0, 1, 60))
        self.assert_fits(plan, 60, 26)
        self.assert_centred(plan, 60, 26)
        self.assertTrue(layout_plan(53, 24)["too_small"])
        self.assertTrue(layout_plan(77, 23)["too_small"])

    def test_every_size_fits_and_is_centred(self):
        for cols in range(54, 130, 3):
            for rows in range(24, 40, 2):
                plan = layout_plan(cols, rows)
                self.assertFalse(plan["too_small"], (cols, rows))
                self.assert_fits(plan, cols, rows)
                self.assert_centred(plan, cols, rows)
                if plan["controls"] is None:
                    self.assertTrue(plan["hint"] is not None or plan["hint_in_header"])


class ControlsHelpTest(unittest.TestCase):
    def test_controls_constants(self):
        keys = [k for entry, _ in snake.CONTROLS for k in entry.split()]
        for key in ("?", "q", "p", "r", "arrows", "h", "j", "k", "l",
                    "w", "a", "s", "d"):
            self.assertIn(key, keys, key)
        self.assertEqual(snake.CONTROLS[-1], ("q", "menu"))

    def test_every_handled_key_is_listed(self):
        keys = {k for entry, _ in snake.CONTROLS for k in entry.split()}
        for ch in snake.KEY_DIRS:
            if ch < 256:
                self.assertIn(chr(ch).lower(), keys, chr(ch))

    def test_help_sections(self):
        headings = [h for h, _ in snake.HELP]
        for heading in ("Goal", "Controls", "How it works", "Scoring", "Tips"):
            self.assertIn(heading, headings)
        for heading, body in snake.HELP:
            self.assertTrue(body.strip(), heading)
            body.encode("ascii")

    def test_key_hint_always_keeps_help(self):
        for width in range(6, 120, 7):
            hint = snake.key_hint(width)
            self.assertLessEqual(len(hint), width)
            self.assertIn("? help", hint)

    def test_help_pauses_and_closing_resumes(self):
        from common import HELP_KEY, HelpOverlay

        game = new_game()
        view = HelpOverlay(snake.TITLE, snake.HELP)
        consumed, by_help = snake.help_key(view, game, HELP_KEY, False)
        self.assertTrue(consumed and by_help and view.is_open and game.paused)
        head = game.snake[0]
        game.update(1.0)
        self.assertEqual(game.snake[0], head)  # frozen while open
        consumed, by_help = snake.help_key(view, game, snake.curses.KEY_DOWN, by_help)
        self.assertTrue(consumed and view.is_open and game.paused)
        self.assertEqual(game.queue, [])  # the key never reached the game
        consumed, _ = snake.help_key(view, game, snake.curses.KEY_RESIZE, by_help)
        self.assertFalse(consumed)
        consumed, by_help = snake.help_key(view, game, ord("q"), by_help)
        self.assertTrue(consumed)
        self.assertFalse(view.is_open or game.paused or by_help)
        self.assertEqual(snake.help_key(view, game, ord("h"), False), (False, False))

    def test_help_keeps_a_pause_it_did_not_cause(self):
        from common import HELP_KEY, HelpOverlay

        for setup in ("paused", "game_over"):
            game = new_game()
            setattr(game, setup, True)
            view = HelpOverlay(snake.TITLE, snake.HELP)
            consumed, by_help = snake.help_key(view, game, HELP_KEY, False)
            self.assertTrue(consumed and view.is_open)
            self.assertFalse(by_help)
            snake.help_key(view, game, ord("x"), by_help)
            self.assertFalse(view.is_open)
            self.assertEqual(game.paused, setup == "paused")


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
            raise snake.curses.error()
        child = FakeWin(h, w, self.root, self.y0 + y, self.x0 + x)
        self.root.children.append(child)
        return child

    def erase(self):
        self.writes = []

    def addstr(self, y, x, text, attr=0):
        if y < 0 or y >= self.rows or x < 0 or x + len(text) > self.cols:
            self.root.violations.append((y, x, text))
            raise snake.curses.error()
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

    def render(self, game, cols=77, rows=24, attention=None, resume=False,
               help_open=False):
        from common import HelpOverlay

        scr = FakeWin(rows, cols)
        view = HelpOverlay(snake.TITLE, snake.HELP)
        if help_open:
            view.open()
        with mock.patch("curses.color_pair", return_value=0):
            lay = snake.Layout(scr)
            snake.draw(game, lay, self.session(attention), resume, 1234, view)
        return scr, lay

    @staticmethod
    def text(scr):
        return "\n".join(t for w in [scr] + scr.children for _, _, t in w.writes)

    def games(self):
        yield "fresh", new_game()
        b = new_game()
        b.level = 4
        b._start_level()
        b.bonus = [0, 20, 5.0]
        b.snake = [(x, 10) for x in range(FIELD_W - 1, -1, -1)]
        yield "long", b
        d = new_game()
        d.snake = [(FIELD_W - 1, 0), (FIELD_W - 2, 0)]
        d.dir = UP
        tick(d)
        yield "dying", d
        lc = new_game()
        lc.phase = snake.Phase("level_clear", 1.0)
        lc.phase.elapsed = 0.2
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
            for size in ((93, 28), (77, 24), (70, 24), (60, 26), (60, 24),
                         (40, 10), (10, 3)):
                for att, res in ((None, False), (attention, False), (None, True)):
                    for help_open in (False, True):
                        scr, lay = self.render(game, *size, attention=att,
                                               resume=res, help_open=help_open)
                        self.assertEqual(scr.violations, [], (name, size))

    def test_controls_on_screen_at_every_tier(self):
        for size in ((93, 28), (77, 24)):
            scr, lay = self.render(new_game(), *size)
            self.assertIsNotNone(lay.controls)
            text = self.text(scr)
            self.assertIn(" keys ", text)
            for keys, _ in snake.CONTROLS:
                self.assertIn(keys, text, (size, keys))
        for size in ((70, 24), (60, 26), (54, 24)):
            scr, lay = self.render(new_game(), *size)
            self.assertIsNone(lay.controls)
            self.assertIn("? help", self.text(scr), size)

    def test_help_overlay_renders(self):
        for size in ((93, 28), (77, 24), (60, 24)):
            scr, _ = self.render(new_game(), *size, help_open=True)
            text = self.text(scr)
            self.assertIn("Snake - help", text, size)
            self.assertIn("Goal", text, size)

    def test_full_frame_contents(self):
        scr, lay = self.render(new_game())
        self.assertEqual(lay.plan["tier"], "full")
        text = "".join(t for c in scr.children for _, _, t in c.writes)
        self.assertIn("score 0", text)
        self.assertIn("agents", text)
        header = "".join(t for _, _, t in scr.writes)
        self.assertIn("snake", header)
        go = new_game()
        go.game_over = True
        scr, _ = self.render(go)
        text = "".join(t for c in scr.children for _, _, t in c.writes)
        self.assertIn("GAME OVER", text)
        self.assertIn("r restart | q menu", text)

    def test_too_small_message(self):
        scr, lay = self.render(new_game(), 50, 20)
        self.assertTrue(lay.too_small)
        text = " ".join(t for _, _, t in scr.writes)
        self.assertIn("pane too small", text)


if __name__ == "__main__":
    unittest.main()
