"""Unit tests for the pure game logic, layout and rendering in pengo.py (no
terminal needed)."""

import json
import os
import random
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pengo  # noqa: E402
from common import AgentSnapshot  # noqa: E402
from pengo import (  # noqa: E402
    BREAK_POINTS,
    DIAMOND,
    DOWN,
    EGG_POINTS,
    EMPTY,
    FIELD_H,
    FIELD_W,
    ICE,
    LEFT,
    LIVES,
    RIGHT,
    START,
    STUN_KILL_POINTS,
    UP,
    Bee,
    Game,
    diamonds_aligned,
    generate_maze,
    layout_plan,
    reachable,
)


def new_game(seed=1):
    return Game(random.Random(seed))


def blank_game(seed=1):
    """A game on an empty board (no ice, no bees, no eggs) with the three
    diamonds tucked away in the bottom row, far apart; tests then place
    exactly what they need."""
    g = new_game(seed)
    g.grid = [[EMPTY] * FIELD_W for _ in range(FIELD_H)]
    for x in (0, 5, 10):
        g.grid[FIELD_H - 1][x] = DIAMOND
    g.bees = []
    g.eggs = set()
    g.pengo = (6, 6)
    g.facing = DOWN
    return g


def put(g, x, y, kind=ICE):
    g.grid[y][x] = kind


class MazeTest(unittest.TestCase):
    def test_every_empty_cell_reachable(self):
        for seed in range(30):
            grid = generate_maze(random.Random(seed))
            empty = {(x, y) for y in range(FIELD_H) for x in range(FIELD_W)
                     if grid[y][x] == EMPTY}
            self.assertIn(START, empty)
            self.assertEqual(reachable(grid, START), empty, seed)

    def test_perfect_maze_has_no_loops(self):
        # A perfect maze over the room lattice is a spanning tree: 7x8 rooms
        # joined by exactly rooms-1 passages, so empty = 2*rooms - 1.
        grid = generate_maze(random.Random(3))
        empty = sum(row.count(EMPTY) for row in grid)
        rooms = ((FIELD_W + 1) // 2) * ((FIELD_H + 1) // 2)
        self.assertEqual(empty, 2 * rooms - 1)

    def test_deterministic_for_seed(self):
        a = generate_maze(random.Random(42))
        b = generate_maze(random.Random(42))
        self.assertEqual(a, b)
        self.assertNotEqual(a, generate_maze(random.Random(43)))
        g1, g2 = new_game(7), new_game(7)
        self.assertEqual(g1.to_save(), g2.to_save())

    def test_level_setup(self):
        for seed in range(20):
            g = new_game(seed)
            dias = g.diamonds()
            self.assertEqual(len(dias), 3)
            for i, a in enumerate(dias):
                for b in dias[i + 1:]:
                    self.assertGreaterEqual(abs(a[0] - b[0]) + abs(a[1] - b[1]),
                                            pengo.DIAMOND_MIN_APART)
            self.assertEqual(len(g.bees), pengo.active_target(1))
            self.assertEqual(len(g.eggs), pengo.egg_count(1))
            for x, y in g.eggs:
                self.assertEqual(g.grid[y][x], ICE)
            for b in g.bees:
                self.assertEqual(g.grid[b.y][b.x], EMPTY)
                self.assertNotEqual(b.pos, g.pengo)
            self.assertEqual(g.pengo, START)


class MovementTest(unittest.TestCase):
    def test_moves_one_cell_and_faces(self):
        g = blank_game()
        self.assertTrue(g.move(RIGHT))
        self.assertEqual(g.pengo, (7, 6))
        self.assertEqual(g.facing, RIGHT)
        self.assertTrue(g.move(UP))
        self.assertEqual((g.pengo, g.facing), ((7, 5), UP))

    def test_blocks_and_walls_stop_but_turn(self):
        g = blank_game()
        put(g, 6, 5)
        self.assertFalse(g.move(UP))
        self.assertEqual((g.pengo, g.facing), ((6, 6), UP))
        g.pengo = (0, 0)
        self.assertFalse(g.move(LEFT))
        self.assertFalse(g.move(UP))
        self.assertEqual((g.pengo, g.facing), ((0, 0), UP))
        g.pengo = (FIELD_W - 1, FIELD_H - 2)
        self.assertFalse(g.move(RIGHT))
        self.assertEqual(g.pengo, (FIELD_W - 1, FIELD_H - 2))

    def test_no_move_while_paused(self):
        g = blank_game()
        g.paused = True
        self.assertFalse(g.move(RIGHT))
        self.assertEqual(g.pengo, (6, 6))


class PushTest(unittest.TestCase):
    def test_slide_until_block(self):
        g = blank_game()
        put(g, 7, 6)
        put(g, 11, 6)
        g.facing = RIGHT
        self.assertEqual(g.push(), "slide")
        self.assertEqual(g.grid[6][7], EMPTY)
        self.assertEqual(g.grid[6][10], ICE)

    def test_slide_until_wall(self):
        g = blank_game()
        put(g, 6, 5)
        g.facing = UP
        self.assertEqual(g.push(), "slide")
        self.assertEqual(g.grid[0][6], ICE)
        self.assertEqual(g.grid[5][6], EMPTY)

    def test_push_nothing(self):
        g = blank_game()
        g.facing = LEFT
        self.assertEqual(g.push(), "none")
        self.assertEqual(g.score, 0)

    def test_egg_moves_with_block(self):
        g = blank_game()
        put(g, 5, 6)
        g.eggs = {(5, 6)}
        g.bees = [Bee(12, 0)]
        g.facing = LEFT
        g.push()
        self.assertEqual(g.eggs, {(0, 6)})
        self.assertEqual(g.grid[6][0], ICE)

    def test_crush_single(self):
        g = blank_game()
        put(g, 7, 6)
        g.bees = [Bee(9, 6), Bee(0, 0)]
        g.facing = RIGHT
        g.push()
        self.assertEqual([b.pos for b in g.bees], [(0, 0)])
        self.assertEqual(g.score, 400)
        self.assertEqual(g.grid[6][FIELD_W - 1], ICE)

    def test_crush_multiple_scores(self):
        for n, points in ((2, 1600), (3, 3200), (4, 6400)):
            g = blank_game()
            put(g, 7, 6)
            g.bees = [Bee(8 + i, 6) for i in range(n)] + [Bee(0, 0)]
            g.facing = RIGHT
            g.push()
            self.assertEqual(len(g.bees), 1, n)
            self.assertEqual(g.score, points, n)

    def test_bee_directly_behind_block_is_crushed_not_blocking(self):
        g = blank_game()
        put(g, 7, 6)
        g.bees = [Bee(8, 6), Bee(0, 0)]
        g.facing = RIGHT
        self.assertEqual(g.push(), "slide")
        self.assertEqual(g.score, 400)

    def test_bee_beyond_stop_survives(self):
        g = blank_game()
        put(g, 7, 6)
        put(g, 9, 6)
        g.bees = [Bee(10, 6)]
        g.facing = RIGHT
        g.push()
        self.assertEqual(g.grid[6][8], ICE)
        self.assertEqual(len(g.bees), 1)

    def test_blocked_push_destroys_ice(self):
        g = blank_game()
        put(g, 7, 6)
        put(g, 8, 6)
        g.bees = [Bee(0, 0)]
        g.facing = RIGHT
        self.assertEqual(g.push(), "break")
        self.assertEqual(g.grid[6][7], EMPTY)
        self.assertEqual(g.grid[6][8], ICE)
        self.assertEqual(g.score, BREAK_POINTS)

    def test_block_against_wall_destroyed(self):
        g = blank_game()
        g.pengo = (1, 3)
        put(g, 0, 3)
        g.bees = [Bee(12, 0)]
        g.facing = LEFT
        self.assertEqual(g.push(), "break")
        self.assertEqual(g.grid[3][0], EMPTY)

    def test_egg_block_destroyed_scores_and_removes_egg(self):
        g = blank_game()
        put(g, 7, 6)
        put(g, 8, 6)
        g.eggs = {(7, 6), (2, 2)}
        put(g, 2, 2)
        g.bees = [Bee(0, 0)]
        g.facing = RIGHT
        self.assertEqual(g.push(), "break")
        self.assertEqual(g.eggs, {(2, 2)})
        self.assertEqual(g.score, EGG_POINTS)
        self.assertEqual(g.remaining(), 2)

    def test_diamond_never_breaks(self):
        g = blank_game()
        put(g, 7, 6, DIAMOND)
        put(g, 8, 6)
        g.facing = RIGHT
        self.assertEqual(g.push(), "none")
        self.assertEqual(g.grid[6][7], DIAMOND)


class ShakeTest(unittest.TestCase):
    def test_shake_stuns_only_bees_touching_that_wall(self):
        g = blank_game()
        g.pengo = (FIELD_W - 1, 6)
        g.facing = RIGHT
        touching = Bee(FIELD_W - 1, 1)
        touching2 = Bee(FIELD_W - 1, 12)
        other_wall = Bee(0, 3)
        inner = Bee(FIELD_W - 2, 3)
        g.bees = [touching, touching2, other_wall, inner]
        self.assertEqual(g.push(), "shake")
        self.assertGreater(touching.stun, 2.5)
        self.assertGreater(touching2.stun, 2.5)
        self.assertEqual(other_wall.stun, 0)
        self.assertEqual(inner.stun, 0)

    def test_top_wall_shake(self):
        g = blank_game()
        g.pengo = (4, 0)
        g.facing = UP
        top, side = Bee(9, 0), Bee(0, 5)
        g.bees = [top, side]
        g.push()
        self.assertGreater(top.stun, 0)
        self.assertEqual(side.stun, 0)

    def test_stunned_bees_do_not_move_then_recover(self):
        g = blank_game()
        b = Bee(0, 0, stun=pengo.STUN_TIME)
        g.bees = [b, Bee(12, 0)]
        g.update(1.0)
        self.assertEqual(b.pos, (0, 0))
        g.update(2.5)
        self.assertEqual(b.stun, 0)

    def test_walking_over_stunned_kills(self):
        g = blank_game()
        g.bees = [Bee(7, 6, stun=2.0), Bee(0, 0)]
        self.assertTrue(g.move(RIGHT))
        self.assertEqual(g.pengo, (7, 6))
        self.assertEqual(len(g.bees), 1)
        self.assertEqual(g.score, STUN_KILL_POINTS)
        self.assertIsNone(g.phase)

    def test_walking_into_active_bee_dies(self):
        g = blank_game()
        g.bees = [Bee(7, 6)]
        g.move(RIGHT)
        self.assertEqual(g.phase.kind, "dying")
        self.assertEqual(g.lives, LIVES - 1)


class DiamondTest(unittest.TestCase):
    def test_alignment_detection(self):
        self.assertEqual(diamonds_aligned([(3, 4), (4, 4), (5, 4)]), "row")
        self.assertEqual(diamonds_aligned([(5, 4), (3, 4), (4, 4)]), "row")
        self.assertEqual(diamonds_aligned([(2, 7), (2, 5), (2, 6)]), "col")
        self.assertIsNone(diamonds_aligned([(3, 4), (4, 4), (6, 4)]))
        self.assertIsNone(diamonds_aligned([(3, 4), (4, 4), (5, 5)]))
        self.assertIsNone(diamonds_aligned([(3, 4), (4, 4)]))

    def diamond_game(self):
        g = blank_game()
        g.grid[FIELD_H - 1] = [EMPTY] * FIELD_W
        g.bees = [Bee(0, 0), Bee(12, 12)]
        return g

    def test_row_bonus_away_from_wall_and_stun(self):
        g = self.diamond_game()
        put(g, 3, 4, DIAMOND)
        put(g, 4, 4, DIAMOND)
        put(g, 5, 8, DIAMOND)   # slides up from (5, 8) to (5, 4)? no: block at (5,3)
        put(g, 5, 3)
        g.pengo, g.facing = (5, 9), UP
        self.assertEqual(g.push(), "slide")
        self.assertEqual(g.grid[4][5], DIAMOND)
        self.assertEqual(g.score, pengo.DIAMOND_BONUS_FREE)
        self.assertTrue(all(b.stun > 0 for b in g.bees))
        self.assertTrue(g.diamond_done)

    def test_column_bonus_touching_wall(self):
        g = self.diamond_game()
        put(g, 0, 5, DIAMOND)
        put(g, 0, 6, DIAMOND)
        put(g, 0, 10, DIAMOND)
        g.pengo, g.facing = (0, 11), UP
        g.push()
        self.assertEqual(g.grid[7][0], DIAMOND)
        self.assertEqual(g.score, pengo.DIAMOND_BONUS_WALL)

    def test_bonus_only_once(self):
        g = self.diamond_game()
        put(g, 3, 4, DIAMOND)
        put(g, 4, 4, DIAMOND)
        put(g, 5, 4, DIAMOND)
        g.diamond_done = True
        g._check_diamonds()
        self.assertEqual(g.score, 0)


class BeeTest(unittest.TestCase):
    def test_greedy_step_toward_pengo(self):
        for seed in range(20):
            g = blank_game(seed)
            b = Bee(6, 1)
            g.bees = [b]
            with mock.patch.object(g.rng, "random", return_value=0.0):
                g.step_bees()
            self.assertEqual(b.pos, (6, 2), seed)

    def test_greedy_around_ice(self):
        g = blank_game()
        g.pengo = (6, 6)
        put(g, 6, 3)
        b = Bee(6, 2)
        g.bees = [b]
        g.level = 1  # never breaks ice at level 1
        with mock.patch.object(g.rng, "random", return_value=0.0):
            g.step_bees()
        self.assertIn(b.pos, ((5, 2), (7, 2)))
        self.assertEqual(g.grid[3][6], ICE)

    def test_bee_breaks_ice_in_later_levels(self):
        g = blank_game()
        g.level = pengo.BREAK_FROM_LEVEL
        put(g, 6, 3)
        b = Bee(6, 2)
        g.bees = [b]
        with mock.patch.object(g.rng, "random", return_value=0.0):
            g.step_bees()
        self.assertEqual(g.grid[3][6], EMPTY)
        self.assertEqual(b.pos, (6, 2))

    def test_bees_move_at_level_rate(self):
        g = blank_game()
        b = Bee(6, 0)
        g.bees = [b]
        g.update(pengo.bee_interval(1) * 0.9)
        self.assertEqual(b.pos, (6, 0))
        g.update(pengo.bee_interval(1) * 0.2)
        self.assertNotEqual(b.pos, (6, 0))
        self.assertLess(pengo.bee_interval(5), pengo.bee_interval(1))

    def test_bee_reaching_pengo_kills(self):
        g = blank_game()
        g.bees = [Bee(6, 5)]
        with mock.patch.object(g.rng, "random", return_value=0.0):
            g.step_bees()
        self.assertEqual(g.phase.kind, "dying")

    def test_egg_hatches_when_below_target(self):
        g = blank_game()
        put(g, 2, 2)
        g.eggs = {(2, 2)}
        g.bees = [Bee(0, 0)]
        g.bee_timer = -100  # keep the bee still
        g.update(pengo.HATCH_DELAY + 0.01)
        self.assertEqual(g.eggs, set())
        self.assertEqual(g.grid[2][2], EMPTY)
        self.assertIn((2, 2), [b.pos for b in g.bees])


class LevelTest(unittest.TestCase):
    def test_level_clear_when_all_gone(self):
        g = blank_game()
        put(g, 7, 6)
        g.bees = [Bee(9, 6)]
        g.level_time = 10.0
        g.facing = RIGHT
        g.push()
        self.assertEqual(g.phase.kind, "level_clear")
        self.assertEqual(g.score, 400 + 5000)
        self.assertEqual(g.last_time_bonus, 5000)
        g.update(pengo.LEVEL_CLEAR_TIME + 0.1)
        self.assertIsNone(g.phase)
        self.assertEqual(g.level, 2)
        self.assertEqual(len(g.diamonds()), 3)
        self.assertEqual(len(g.eggs), pengo.egg_count(2))

    def test_not_clear_while_eggs_remain(self):
        g = blank_game()
        put(g, 7, 6)
        put(g, 2, 2)
        g.eggs = {(2, 2)}
        g.bees = [Bee(9, 6)]
        g.facing = RIGHT
        g.push()
        self.assertIsNone(g.phase)

    def test_time_bonus_table(self):
        self.assertEqual(pengo.time_bonus(5), 5000)
        self.assertEqual(pengo.time_bonus(25), 2000)
        self.assertEqual(pengo.time_bonus(55), 10)
        self.assertEqual(pengo.time_bonus(90), 0)


class DeathTest(unittest.TestCase):
    def test_death_phase_then_respawn(self):
        g = blank_game()
        g.bees = [Bee(7, 6), Bee(0, 0)]
        g.move(RIGHT)
        self.assertEqual(g.phase.kind, "dying")
        self.assertFalse(g.move(LEFT))  # frozen while dying
        g.update(0.5)
        self.assertIsNotNone(g.phase)
        g.update(0.6)
        self.assertIsNone(g.phase)
        self.assertEqual(g.lives, LIVES - 1)
        self.assertNotIn(g.pengo, [b.pos for b in g.bees])
        self.assertTrue(all(b.stun > 0 for b in g.bees))

    def test_lives_and_game_over(self):
        g = blank_game()
        for i in range(LIVES):
            g.bees = [Bee(g.pengo[0] + 1, g.pengo[1])]
            g.grid[g.pengo[1]][g.pengo[0] + 1] = EMPTY
            g.move(RIGHT)
            self.assertEqual(g.lives, LIVES - 1 - i)
            g.resolve_phases()
        self.assertTrue(g.game_over)
        self.assertEqual(g.lives, 0)
        self.assertFalse(g.update(1.0))
        self.assertFalse(g.move(LEFT))
        g.reset()
        self.assertFalse(g.game_over)
        self.assertEqual((g.lives, g.score, g.level), (LIVES, 0, 1))


class DisplayKeyTest(unittest.TestCase):
    def test_changes_with_displayed_second_and_flash(self):
        g = blank_game()
        g.bees = [Bee(0, 0, stun=100.0)]
        k0 = pengo.display_key(g, 0.0)
        g.update(0.5)
        self.assertEqual(pengo.display_key(g, 0.0), k0)
        g.update(0.6)
        self.assertNotEqual(pengo.display_key(g, 0.0), k0)
        g.diamond_done = False
        self.assertEqual(pengo.display_key(g, 0.0), pengo.display_key(g, 0.3))
        g.diamond_done = True
        self.assertNotEqual(pengo.display_key(g, 0.0), pengo.display_key(g, 0.3))


class SaveTest(unittest.TestCase):
    def advanced(self):
        g = new_game(5)
        for _ in range(20):
            g.update(0.1)
        g.resolve_phases()
        g.score = 1234
        return g

    def test_round_trip_including_rng(self):
        g = self.advanced()
        data = json.loads(json.dumps(g.to_save()))
        r = Game.from_save(data)
        self.assertIsNotNone(r)
        self.assertEqual(r.to_save(), g.to_save())
        for attr in ("score", "level", "lives", "grid", "eggs", "pengo", "facing",
                     "diamond_done"):
            self.assertEqual(getattr(r, attr), getattr(g, attr), attr)
        self.assertEqual([b.pos for b in r.bees], [b.pos for b in g.bees])
        # Same RNG state: play continues identically.
        for _ in range(30):
            g.update(0.1)
            r.update(0.1)
        self.assertEqual([b.pos for b in r.bees], [b.pos for b in g.bees])
        self.assertEqual(g.rng.random(), r.rng.random())

    def test_corrupt_or_incompatible_is_none(self):
        good = self.advanced().to_save()
        self.assertIsNotNone(Game.from_save(good))
        self.assertIsNone(Game.from_save(None))
        self.assertIsNone(Game.from_save({}))
        self.assertIsNone(Game.from_save("junk"))
        self.assertIsNone(Game.from_save(dict(good, version=pengo.SAVE_VERSION + 1)))
        self.assertIsNone(Game.from_save(dict(good, rng=[3, [1, 2], None])))
        self.assertIsNone(Game.from_save(dict(good, grid=good["grid"][:-1])))
        self.assertIsNone(Game.from_save(dict(good, grid=["?" * FIELD_W] * FIELD_H)))
        self.assertIsNone(Game.from_save(dict(good, pengo=[FIELD_W, 0])))
        self.assertIsNone(Game.from_save(dict(good, facing="sideways")))
        self.assertIsNone(Game.from_save(dict(good, bees=[[0, 0]])))
        ice = next([x, y] for y, row in enumerate(good["grid"])
                   for x, c in enumerate(row) if c == ICE)
        self.assertIsNone(Game.from_save(dict(good, pengo=ice)))
        bad = dict(good)
        del bad["lives"]
        self.assertIsNone(Game.from_save(bad))

    def test_phase_resolved_before_save(self):
        g = blank_game()
        g.bees = [Bee(7, 6), Bee(0, 0)]
        g.move(RIGHT)
        g.resolve_phases()
        r = Game.from_save(json.loads(json.dumps(g.to_save())))
        self.assertIsNotNone(r)
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


class LayoutPlanTest(unittest.TestCase):
    def assert_fits(self, plan, cols, rows):
        for name in REGIONS:
            region = plan[name]
            if region is None:
                continue
            y, x, h, w = region
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 1)
            self.assertLessEqual(x + w, cols, name)
            self.assertLessEqual(y + h, rows, name)

    def assert_centred(self, plan, cols, rows):
        y, x, h, w = block(plan)
        bottom = rows - (1 if plan["hint"] else 0)
        self.assertLessEqual(abs(x - (cols - x - w)), 1, (cols, rows, plan))
        self.assertLessEqual(abs((y - 1) - (bottom - y - h)), 1, (cols, rows, plan))

    def test_full_at_popup_size(self):
        plan = layout_plan(93, 28)
        self.assertEqual(plan["tier"], "full")
        self.assert_fits(plan, 93, 28)
        self.assert_centred(plan, 93, 28)
        fy, fx, fh, fw = plan["field"]
        self.assertEqual((fh, fw), (FIELD_H + 2, FIELD_W * 2 + 2))
        sy, sx, sh, sw = plan["stats"]
        cy, cx, ch, cw = plan["controls"]
        ay, ax, ah, aw = plan["agents"]
        self.assertEqual((sw, cw, aw), (30, 30, 30))
        self.assertEqual(sx, fx + fw + 1)
        self.assertEqual(sy, fy)
        self.assertEqual(cy, sy + sh)
        self.assertEqual(ay, cy + ch)
        self.assertEqual(ch, pengo.controls_height(pengo.CONTROLS, 30))
        self.assertGreaterEqual(ah, pengo.AGENTS_MIN_H)
        self.assertIsNone(plan["hint"])

    def test_full_at_77x24(self):
        plan = layout_plan(77, 24)
        self.assertEqual(plan["tier"], "full")
        self.assert_fits(plan, 77, 24)
        self.assert_centred(plan, 77, 24)
        self.assertEqual(plan["stats"][3], 30)
        self.assertIsNotNone(plan["controls"])
        self.assertIsNotNone(plan["agents"])

    def test_degrading_tiers(self):
        plan = layout_plan(77, 20)
        self.assertEqual(plan["tier"], "stats")
        self.assertIsNone(plan["agents"])
        plan = layout_plan(50, 20)
        self.assertEqual(plan["tier"], "controls")
        self.assertIsNone(plan["stats"])
        plan = layout_plan(40, 24)
        self.assertEqual(plan["tier"], "field")
        self.assertIsNone(plan["stats"])
        self.assertEqual(plan["hint"], (23, 0, 1, 40))
        self.assertIsNone(layout_plan(40, 18)["hint"])  # header carries ? help
        self.assertTrue(layout_plan(27, 24)["too_small"])
        self.assertTrue(layout_plan(77, 17)["too_small"])
        for cols in range(20, 120, 3):
            for rows in range(10, 40):
                plan = layout_plan(cols, rows)
                if not plan["too_small"]:
                    self.assert_fits(plan, cols, rows)
                    self.assert_centred(plan, cols, rows)
                    if plan["controls"] is None:
                        self.assertEqual(plan["tier"], "field")

    def test_hint_leads_with_help(self):
        for w in (12, 28, 40, 200):
            text = pengo.hint_text(w)
            self.assertIn("? help", text)
            self.assertLess(len(text), w)


class ConstantsTest(unittest.TestCase):
    def test_controls_cover_keys(self):
        keys = " ".join(k for k, _ in pengo.CONTROLS)
        for k in ("?", "q", "p", "r", "arrows", "space", "enter",
                  "w", "a", "s", "d", "h", "j", "k", "l"):
            self.assertIn(k, keys)
        self.assertEqual(pengo.CONTROLS[-1], ("q", "menu"))
        for k, _ in pengo.CONTROLS:
            self.assertLessEqual(len(k), 9)   # never truncated in the box

    def test_help_sections(self):
        help = dict(pengo.HELP)
        for want in ("Goal", "Controls", "Scoring", "Tips", "Legend",
                     "Push, break and shake", "Stunned Sno-Bees",
                     "Eggs hidden in the ice", "Diamond line-up"):
            self.assertIn(want, help)
        for head, body in pengo.HELP:
            self.assertTrue(body.strip(), head)
            body.encode("ascii")
        for glyph in (pengo.ICE_GLYPH, pengo.DIAMOND_GLYPH, pengo.BEE_GLYPH,
                      pengo.STUN_GLYPH, pengo.SPLAT_GLYPH, pengo.DEAD_GLYPH,
                      *pengo.PENGO_GLYPH.values()):
            self.assertIn(glyph, help["Legend"])
        self.assertIn("10000", help["Diamond line-up"])
        self.assertIn("5000", help["Diamond line-up"])


class HelpKeyTest(unittest.TestCase):
    def setUp(self):
        self.game = new_game()
        self.help = pengo.HelpOverlay(pengo.TITLE, pengo.HELP)
        self.st = pengo.LoopState()
        self.session = types.SimpleNamespace(attention=None, dismiss=lambda: None)

    def key(self, ch):
        if isinstance(ch, str):
            ch = ord(ch)
        return pengo.dispatch_key(ch, self.game, self.help, self.st, self.session)

    def test_help_pauses_and_closing_resumes(self):
        self.key("?")
        self.assertTrue(self.help.is_open)
        self.assertTrue(self.game.paused)
        t = self.game.level_time
        self.assertFalse(self.game.update(0.2))
        self.assertEqual(self.game.level_time, t)     # timers frozen
        self.key("j")                                  # scrolls, stays open
        self.assertTrue(self.help.is_open)
        self.assertIsNone(self.key("q"))              # q closes help only
        self.assertFalse(self.help.is_open)
        self.assertFalse(self.game.paused)

    def test_help_keeps_a_manual_pause(self):
        self.key("p")
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

    def test_resume_banner_survives_help(self):
        st = self.st = pengo.LoopState(resume=True)
        self.game.paused = True
        self.key("?")
        self.key("x")
        self.assertTrue(self.game.paused)      # banner still up
        self.key("x")                          # dismisses the banner
        self.assertFalse(st.resume)
        self.assertFalse(self.game.paused)

    def test_game_keys(self):
        self.assertEqual(self.key("q"), "quit")
        self.key("h")
        self.assertEqual(self.game.facing, LEFT)
        self.key("p")
        self.assertTrue(self.game.paused)


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
            raise pengo.curses.error()
        child = FakeWin(h, w, self.root, self.y0 + y, self.x0 + x)
        self.root.children.append(child)
        return child

    def erase(self):
        self.writes = []

    def addstr(self, y, x, text, attr=0):
        if y < 0 or y >= self.rows or x < 0 or x + len(text) > self.cols:
            self.root.violations.append((y, x, text))
            raise pengo.curses.error()
        self.writes.append((y, x, text))


class RenderTest(unittest.TestCase):
    def session(self, attention=None):
        roster = [AgentSnapshot("p%d" % i, "agent%d" % i, "omp", s)
                  for i, s in enumerate(["idle", "working", "blocked", "done"] * 8)]
        return types.SimpleNamespace(
            attention=attention, roster=roster,
            status_pairs={s: i + 1 for i, s in
                          enumerate(("idle", "working", "blocked", "done", "unknown"))},
        )

    def render(self, game, cols=77, rows=24, attention=None, resume=False, help=False):
        scr = FakeWin(rows, cols)
        overlay = pengo.HelpOverlay(pengo.TITLE, pengo.HELP)
        if help:
            overlay.open()
        with mock.patch("curses.color_pair", return_value=0):
            lay = pengo.Layout(scr)
            pengo.draw(game, lay, self.session(attention), resume, 1234, overlay)
        return scr, lay

    def games(self):
        yield "fresh", new_game()
        s = blank_game()
        s.bees = [Bee(0, 0, 2.0), Bee(12, 14)]
        s.pengo, s.facing = (12, 0), RIGHT
        s.push()
        s.splats.append([5, 5, 0.3])
        s.message = ["DIAMONDS +10000", 1.0]
        yield "effects", s
        for d in (UP, LEFT):
            w = blank_game()
            w.pengo, w.facing = (0, 0), d
            w.push()
            yield "shake-" + d, w
        d = blank_game()
        d.bees = [Bee(7, 6)]
        d.move(RIGHT)
        yield "dying", d
        lc = new_game()
        lc.phase = pengo.Phase("level_clear", 1.0)
        lc.phase.elapsed = 0.2
        lc.last_time_bonus = 5000
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
            for size in ((93, 28), (77, 24), (60, 24), (50, 20), (40, 20), (30, 18),
                         (10, 3)):
                for att, res in ((None, False), (attention, False), (None, True)):
                    scr, lay = self.render(game, *size, attention=att, resume=res)
                    self.assertEqual(scr.violations, [], (name, size))

    def test_full_frame_contents(self):
        g = new_game()
        scr, lay = self.render(g)
        self.assertEqual(lay.plan["tier"], "full")
        text = "".join(t for c in scr.children for _, _, t in c.writes)
        self.assertIn("score 0", text)
        self.assertIn("agents", text)
        self.assertIn("[]", text)
        self.assertIn("<>", text)
        self.assertIn(pengo.PENGO_GLYPH[g.facing], text)
        self.assertIn(pengo.BEE_GLYPH, text)
        header = "".join(t for _, _, t in scr.writes)
        self.assertIn("pengo", header)
        go = new_game()
        go.game_over = True
        scr, _ = self.render(go)
        text = "".join(t for c in scr.children for _, _, t in c.writes)
        self.assertIn("GAME OVER", text)
        self.assertIn("r restart | q menu", text)

    def test_controls_visible_and_help_overlay(self):
        for cols, rows in ((93, 28), (77, 24), (50, 20), (40, 24), (40, 18), (30, 18)):
            for help in (False, True):
                scr, lay = self.render(new_game(), cols, rows, help=help)
                self.assertEqual(scr.violations, [], (cols, rows, help))
                if help:
                    text = "".join(t for c in scr.children for _, _, t in c.writes)
                    self.assertIn("Pengo - help", text, (cols, rows))
                    continue
                if lay.controls is not None:
                    keys = "\n".join(t for _, _, t in lay.controls.writes)
                    for want in ("walk", "push", "help", "pause", "menu"):
                        self.assertIn(want, keys, (cols, rows))
                elif lay.plan["hint"] is not None:
                    hint = [t for y, _, t in scr.writes if y == rows - 1]
                    self.assertTrue(any("? help" in t for t in hint), (cols, rows))
                else:
                    head = [t for y, _, t in scr.writes if y == 0]
                    self.assertTrue(any("? help" in t for t in head), (cols, rows))
        _, lay = self.render(new_game(), 93, 28)
        self.assertTrue(any(" Pengo " in t for _, _, t in lay.stats.writes))

    def test_too_small_message(self):
        scr, lay = self.render(new_game(), 27, 20)
        self.assertTrue(lay.too_small)
        text = " ".join(t for _, _, t in scr.writes)
        self.assertIn("pane too small", text)


if __name__ == "__main__":
    unittest.main()
