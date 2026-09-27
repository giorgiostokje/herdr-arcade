"""Unit tests for the pure Donkey Kong logic in donkeykong.py (no terminal)."""

import json
import os
import random
import sys
import types
import unittest
from collections import deque
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import donkeykong
from common import AgentSnapshot
from donkeykong import (
    BARREL_STEP,
    BONUS_DROP,
    BONUS_START,
    BONUS_TICK,
    CLEAR_TIME,
    DEATH_TIME,
    EMPTY,
    EXTRA_LIFE_AT,
    FALL_STEP,
    GIRDER,
    GIRDERISH,
    GRID_H,
    GRID_W,
    HAMMER_TIME,
    JUMP_ARC,
    JUMP_SCORE,
    JUMP_STEP,
    LADDER,
    LTOP,
    MOVE_STEP,
    RIVET,
    RIVET_SCORE,
    RUN_WINDOW,
    SAFE_FALL,
    SAVE_VERSION,
    SMASH_POINTS,
    STAGES,
    START_LIVES,
    Barrel,
    Fireball,
    Game,
    Phase,
    handle_key,
    layout_plan,
    parse_stage,
)

DT = 0.01


def build(rows, kind="barrels"):
    """A 28x24 test stage: `rows` sit at the bottom, right above a full
    girder floor (y = 23); Kong sits harmlessly in the top-left corner and
    throws nothing. Returns (game, top) where rows[i] is at y = top + i."""
    rows = [r.ljust(GRID_W) for r in rows]
    top = GRID_H - 1 - len(rows)
    full = ["K".ljust(GRID_W)] + [" " * GRID_W] * (top - 1) + rows \
        + ["=" * GRID_W]
    g = Game(random.Random(0), stages=[(kind, "9m", full)])
    quiet(g)
    return g, top


def quiet(g):
    """No new barrels or fireballs."""
    g.throw_t = 1e9
    g.spawn_t = 1e9
    g.barrels = []
    g.fireballs = []


def run_for(game, seconds, step=DT, until=None):
    t = 0.0
    while t < seconds:
        game.update(step)
        t += step
        if until is not None and until(game):
            return True
    return False


def settle(game, limit=5.0):
    """Let a jump/fall and the step cooldown play out."""
    p = game.player
    run_for(game, limit, until=lambda g: g.phase is not None or (
        p.jump < 0 and not p.falling and p.cool <= 0))


# --------------------------------------------------------------------------
# Walking, slopes, ladders
# --------------------------------------------------------------------------

class MovementTest(unittest.TestCase):
    def test_walk_one_cell_per_press_with_cooldown(self):
        g, top = build(["  M  "])
        p = g.player
        self.assertTrue(g.move(1))
        self.assertEqual((p.x, p.y), (3, top))
        self.assertFalse(g.move(1))            # too soon after the last step
        g.update(MOVE_STEP + 0.001)
        self.assertTrue(g.move(-1))
        self.assertEqual(p.x, 2)
        self.assertEqual(p.facing, -1)

    def test_grid_edge_and_drum_block(self):
        g, top = build(["M  O"])
        self.assertFalse(g.move(-1))
        g.move(1)
        g.update(MOVE_STEP)
        g.move(1)
        g.update(MOVE_STEP)
        self.assertFalse(g.move(1))            # the oil drum is solid
        self.assertEqual(g.player.x, 2)

    def test_staggered_girder_steps_up_and_down(self):
        g, top = build([" M     ",
                        "===    ",
                        "   ====",
                        "       "])
        p = g.player
        g.player.y = top  # standing on the upper segment
        for x in range(2, 5):
            g.update(MOVE_STEP)
            self.assertTrue(g.move(1))
        self.assertEqual((p.x, p.y), (4, top + 1))  # stepped down a row
        self.assertFalse(p.falling)
        for _ in range(2):
            g.update(MOVE_STEP)
            g.move(-1)
        self.assertEqual((p.x, p.y), (2, top))      # and back up
        run_for(g, 1.0)
        self.assertIsNone(g.phase)

    def test_walking_off_an_edge_falls_and_long_falls_kill(self):
        # SAFE_FALL rows down: survives.
        rows = ["M "] + ["=  "] + [" "] * (SAFE_FALL - 1)
        g, top = build(rows)
        g.move(1)
        self.assertTrue(g.player.falling)
        self.assertFalse(g.move(1))            # no control while falling
        run_for(g, 1.0)
        self.assertIsNone(g.phase)
        self.assertEqual(g.player.y, top + SAFE_FALL)
        # One row more: fatal.
        g, top = build(["M "] + ["=  "] + [" "] * SAFE_FALL)
        g.move(1)
        run_for(g, 1.0)
        self.assertEqual(g.phase.kind, "dying")

    def test_ladder_up_through_its_top_and_back_down(self):
        g, top = build(["       ",
                        "==#====",
                        "  H    ",
                        "  HM   "])
        p = g.player
        g.move(-1)
        for _ in range(3):
            g.update(MOVE_STEP)
            self.assertTrue(g.climb(-1))
        self.assertEqual((p.x, p.y), (2, top))     # on top of the girder
        self.assertTrue(g.standing())
        g.update(MOVE_STEP)
        self.assertFalse(g.climb(-1))              # nothing above
        g.update(MOVE_STEP)
        self.assertTrue(g.climb(1))                # into the ladder top
        self.assertEqual(g.grid[p.y][p.x], LTOP)
        self.assertFalse(g.standing())
        g.update(MOVE_STEP)
        self.assertFalse(g.move(1))                # no walking off a ladder
        run_for(g, 0.5)
        self.assertEqual(p.y, top + 1)             # ladders hold you

    def test_broken_ladder_cannot_be_climbed_all_the_way(self):
        g, top = build(["      ",
                        "======",
                        "  h   ",
                        "      ",
                        "  H   ",
                        "  HM  "])
        p = g.player
        g.move(-1)
        g.update(MOVE_STEP)
        self.assertTrue(g.climb(-1))
        g.update(MOVE_STEP)
        self.assertFalse(g.climb(-1))              # the gap
        self.assertEqual(p.y, top + 4)
        # and from above there is no way in
        g2, top2 = build(["  M   ",
                          "======",
                          "  h   ",
                          "      ",
                          "  H   ",
                          "  H   "])
        self.assertFalse(g2.climb(1))

    def test_no_climbing_without_a_ladder(self):
        g, top = build(["  M  "])
        self.assertFalse(g.climb(-1))
        self.assertFalse(g.climb(1))


# --------------------------------------------------------------------------
# Jumping
# --------------------------------------------------------------------------

class JumpTest(unittest.TestCase):
    def test_straight_up_arc_returns_to_the_same_cell(self):
        g, top = build(["   M   "])
        p = g.player
        self.assertTrue(g.jump(0))
        self.assertEqual((p.x, p.y), (3, top - 1))  # up on the keypress
        self.assertFalse(g.move(1))                  # no mid-air control
        self.assertFalse(g.jump(1))
        run_for(g, JUMP_STEP * (len(JUMP_ARC) - 1) - 0.015)
        self.assertEqual(p.y, top - 1)
        run_for(g, 0.02)
        self.assertEqual((p.x, p.y), (3, top))
        self.assertEqual(p.jump, -1)
        self.assertFalse(p.falling)

    def test_running_jump_covers_three_cells(self):
        for dx in (-1, 1):
            g, top = build(["          M          "])
            self.assertTrue(g.jump(dx))
            settle(g)
            self.assertEqual((g.player.x, g.player.y), (10 + 3 * dx, top))
            self.assertEqual(g.player.facing, dx)

    def test_space_takes_the_direction_of_a_recent_step(self):
        g, top = build(["          M          "])
        g.move(1)
        g.update(0.1)
        self.assertTrue(g.jump())
        settle(g)
        self.assertEqual(g.player.x, 14)
        # after RUN_WINDOW without a step, space jumps straight up
        run_for(g, RUN_WINDOW + 0.05)
        g.jump()
        settle(g)
        self.assertEqual(g.player.x, 14)

    def test_jump_across_a_gap(self):
        g, top = build(["   M      ",
                        "==== =====",
                        "          ",
                        "          ",
                        "          "])
        g.player.y = top
        self.assertTrue(g.jump(1))
        settle(g)
        self.assertIsNone(g.phase)
        self.assertEqual((g.player.x, g.player.y), (6, top))

    def test_jump_down_one_step_is_safe_off_an_edge_is_not(self):
        g, top = build(["    M        ",
                        "=====        ",
                        "     ========",
                        "             "])
        g.player.y = top
        g.jump(1)
        settle(g)
        self.assertIsNone(g.phase)
        self.assertEqual((g.player.x, g.player.y), (7, top + 1))
        g, top = build(["    M        ",
                        "=====        ",
                        "             ",
                        "             ",
                        "             "])
        g.player.y = top
        g.jump(1)
        run_for(g, 2.0)
        self.assertEqual(g.phase.kind, "dying")

    def test_jumping_a_barrel_scores_and_survives_at_every_speed(self):
        for stage_no in (1, 21):   # loop 1 and a loop at the speed floor
            for frac in (0.0, 0.5, 0.99):
                g, top = build(["          M          "])
                g.stage_no = stage_no
                b = Barrel(99, 11, top, -1)
                b.t = frac * BARREL_STEP * g.speed
                g.barrels.append(b)
                self.assertTrue(g.jump(0))
                run_for(g, 1.0)
                self.assertIsNone(g.phase, (stage_no, frac))
                self.assertEqual(g.score, JUMP_SCORE, (stage_no, frac))
                self.assertLess(b.x, 10)

    def test_standing_still_a_barrel_kills(self):
        g, top = build(["          M          "])
        g.barrels.append(Barrel(1, 12, top, -1))
        run_for(g, 1.0)
        self.assertEqual(g.phase.kind, "dying")

    def test_no_jump_from_a_ladder(self):
        g, top = build(["  H ",
                        "  HM"])
        g.move(-1)
        g.update(MOVE_STEP)
        g.climb(-1)
        self.assertFalse(g.jump(0))


# --------------------------------------------------------------------------
# Barrels, the oil drum and fireballs
# --------------------------------------------------------------------------

class BarrelTest(unittest.TestCase):
    def test_roll_directions_follow_the_slopes(self):
        g = Game(random.Random(0))
        self.assertEqual(g.kind, "barrels")
        # Kong's girder rolls right, then they alternate; the floor rolls
        # right into the drum.
        self.assertEqual(g.rolls[(0, 4)], 1)
        self.assertEqual(g.rolls[(20, 9)], -1)
        self.assertEqual(g.rolls[(3, 14)], 1)
        self.assertEqual(g.rolls[(20, 19)], -1)
        self.assertEqual(g.rolls[(0, 23)], 1)
        # and every sloped girder's open end is its low end
        for y in (5, 15):
            self.assertEqual(g.grid[y][GRID_W - 1], EMPTY)
        for y in (10, 20):
            self.assertEqual(g.grid[y][0], EMPTY)

    def test_barrel_rolls_down_off_the_end_and_turns(self):
        g, top = build(["             ",
                        "=====        ",
                        "     ===     ",
                        "             ",
                        "             ",
                        "  =========  ",
                        "M            "])
        # the lower girder slopes neither way: it rolls right (flat)
        g.rolls = donkeykong.roll_directions(g.grid)
        b = Barrel(1, 1, top, 1)
        g.barrels.append(b)
        run_for(g, BARREL_STEP * 4 + 0.01)
        self.assertEqual((b.x, b.y), (5, top + 1))   # stepped down
        run_for(g, 2.0, until=lambda _: b.y == top + 4)
        self.assertEqual(b.y, top + 4)               # dropped onto the girder
        self.assertEqual(b.dx, 1)

    def test_barrel_takes_a_full_ladder_down(self):
        g, top = build(["               ",
                        "=====#=========",
                        "     H         ",
                        "     H         ",
                        "     H         ",
                        "===============",
                        "M              "])
        b = Barrel(1, 3, top, 1)
        g.barrels.append(b)
        with mock.patch.object(g.rng, "random", return_value=0.0):
            run_for(g, 1.0, until=lambda _: b.y == top + 4)
        self.assertEqual((b.x, b.y), (5, top + 4))
        self.assertEqual(b.mode, "roll")
        # a barrel that declines keeps rolling along the girder
        g2, top2 = build(["               ",
                          "=====#=========",
                          "     H         ",
                          "M    H         "])
        b2 = Barrel(1, 3, top2, 1)
        g2.barrels.append(b2)
        with mock.patch.object(g2.rng, "random", return_value=0.99):
            run_for(g2, BARREL_STEP * 4)
        self.assertEqual(b2.y, top2)
        self.assertGreater(b2.x, 5)

    def test_drum_burns_barrels_and_lets_out_fireballs(self):
        g, top = build(["M         O "])
        g.barrels.append(Barrel(1, 8, top, 1))
        run_for(g, BARREL_STEP * 2 + 0.01)
        self.assertEqual(g.barrels, [])
        self.assertTrue(g.drum_lit)
        self.assertEqual(len(g.fireballs), 1)
        self.assertEqual((g.fireballs[0].x, g.fireballs[0].y), (9, top))

    def test_kong_throws_and_barrels_never_get_stuck(self):
        g = Game(random.Random(5))
        g.player = donkeykong.Jumpman(27, 8)
        g.bonus = 10 ** 6
        seen = {}
        thrown = set()
        for i in range(6000):
            g.update(0.02)
            if g.phase is not None:
                g.phase = None
            for b in g.barrels:
                thrown.add(b.uid)
                last = seen.get(b.uid)
                seen[b.uid] = ((b.x, b.y), i if last is None or
                               last[0] != (b.x, b.y) else last[1])
                self.assertLess(i - seen[b.uid][1], 60, "barrel stuck")
                self.assertTrue(g.in_bounds(b.x, b.y))
        self.assertGreater(len(thrown), 20)
        self.assertTrue(g.drum_lit)
        self.assertLessEqual(len(g.fireballs), donkeykong.MAX_FIRE_BARRELS)

    def test_fireballs_roam_but_never_fall_or_cross_gaps(self):
        g, top = build(["             ",
                        "===== =======",
                        "             ",
                        "M            "], kind="rivets")
        f = Fireball(1, 2, top, 1)
        g.fireballs.append(f)
        for _ in range(400):
            g.update(0.05)
            self.assertEqual(f.y, top)
            self.assertLess(f.x, 5)

    def test_fireball_climbs_ladders(self):
        g, top = build(["               ",
                        "==#============",
                        "  H            ",
                        "  H            ",
                        "  H            ",
                        "M H            "])
        g.player.x, g.player.y = 12, top  # Jumpman above: fireballs go up
        f = Fireball(1, 3, top + 5, -1)
        g.fireballs.append(f)
        ys = set()
        with mock.patch.object(g.rng, "random", return_value=0.3):
            for _ in range(200):
                g.update(0.05)
                ys.add(f.y)
                if g.phase is not None:
                    break
        self.assertIn(top, ys)

    def test_fireballs_skip_broken_ladders_and_paulines_ladder(self):
        # a broken stub: the fireball stays on its walking row
        g, top = build(["               ",
                        "===============",
                        "               ",
                        "  H            ",
                        "M H            "], kind="rivets")
        g.player.x, g.player.y = 12, top  # Jumpman above
        f = Fireball(1, 3, top + 4, -1)
        g.fireballs.append(f)
        with mock.patch.object(g.rng, "random", return_value=0.3):
            for _ in range(200):
                g.update(0.05)
                self.assertEqual(f.y, top + 4)
        # the real stages: no fireball ever climbs above Kong's row
        for stage_no in (1, 2):
            g = Game(random.Random(stage_no), stage_no)
            g.player = donkeykong.Jumpman(13, 0)   # lure them upward
            g.bonus = 10 ** 6
            for _ in range(5000):
                g.update(0.02)
                if g.phase is not None:
                    g.phase = None
                for f in g.fireballs:
                    self.assertGreaterEqual(f.y, g.kong[1], stage_no)


# --------------------------------------------------------------------------
# Hammers, rivets, items, bonus, lives, stages
# --------------------------------------------------------------------------

class HammerTest(unittest.TestCase):
    def test_grab_by_jumping_smash_and_expire(self):
        g, top = build(["   T      ",
                        "   M      "])
        g.player.y = top + 1
        self.assertTrue(g.jump(0))
        self.assertEqual(g.hammer_t, HAMMER_TIME)
        self.assertEqual(g.hammers, [])
        settle(g)
        self.assertFalse(g.jump(0))           # no jumping with a hammer
        b = Barrel(7, 8, top + 1, -1)
        g.barrels.append(b)
        run_for(g, 1.0)
        self.assertIsNone(g.phase)
        self.assertEqual(g.barrels, [])
        self.assertIn(g.score, SMASH_POINTS)
        run_for(g, HAMMER_TIME)
        self.assertEqual(g.hammer_t, 0)
        self.assertTrue(g.jump(0))

    def test_no_climbing_with_a_hammer(self):
        g, top = build(["  H ",
                        "  HM"])
        g.move(-1)
        g.update(MOVE_STEP)
        g.hammer_t = 3.0
        self.assertFalse(g.climb(-1))

    def test_smash_points_come_from_the_rng(self):
        seen = set()
        for seed in range(30):
            g, top = build(["   M "])
            g.rng.seed(seed)
            g.hammer_t = 5.0
            g.fireballs.append(Fireball(1, 4, top, -1))
            g.update(DT)
            seen.add(g.score)
        self.assertEqual(seen, set(SMASH_POINTS))


class RivetTest(unittest.TestCase):
    def test_rivet_pops_when_you_step_off_it(self):
        g, top = build(["  M       ",
                        "=====R=== ",
                        "          ",
                        "          ",
                        "          "], kind="rivets")
        g.player.y = top
        for _ in range(3):
            g.update(MOVE_STEP)
            g.move(1)
        self.assertEqual(g.player.x, 5)
        self.assertEqual(g.grid[top + 1][5], RIVET)   # standing on it
        g.update(MOVE_STEP)
        g.move(1)
        self.assertEqual(g.grid[top + 1][5], EMPTY)
        # the last rivet brings Kong down: stage clear with the bonus
        self.assertEqual(g.phase.kind, "clear")
        self.assertEqual(g.score, RIVET_SCORE + BONUS_START)

    def test_falling_into_a_rivet_gap_kills(self):
        g, top = build(["  M        ",
                        "=====R==R= ",
                        "           ",
                        "           ",
                        "           "], kind="rivets")
        g.player.y = top
        g.player.x = 4
        for _ in range(2):
            g.update(MOVE_STEP)
            g.move(1)
        g.update(MOVE_STEP)
        g.move(-1)                 # back into the gap
        self.assertTrue(g.player.falling)
        run_for(g, 1.0)
        self.assertEqual(g.phase.kind, "dying")

    def test_jumping_off_a_rivet_also_pulls_it(self):
        g, top = build(["  M        ",
                        "=====R==R= ",
                        "           "], kind="rivets")
        g.player.y, g.player.x = top, 5
        g.jump(1)
        self.assertEqual(g.grid[top + 1][5], EMPTY)


class ItemsAndBonusTest(unittest.TestCase):
    def test_items_score_by_loop(self):
        g, top = build([" M$ "], kind="rivets")
        g.move(1)
        self.assertEqual(g.score, 300)
        g, top = build([" M$ "], kind="rivets")
        g.stage_no = 5  # third loop of a one-stage list
        g.move(1)
        self.assertEqual(g.score, 800)

    def test_bonus_counts_down_and_zero_costs_a_life(self):
        g, top = build(["  M  "])
        run_for(g, BONUS_TICK * 3 + 0.005)
        self.assertEqual(g.bonus, BONUS_START - 3 * BONUS_DROP)
        g.bonus = BONUS_DROP
        run_for(g, BONUS_TICK)
        self.assertEqual(g.bonus, 0)
        self.assertEqual(g.phase.kind, "dying")
        g.finish_phase()
        self.assertEqual(g.lives, START_LIVES - 1)
        self.assertEqual(g.bonus, BONUS_START)

    def test_extra_life_once(self):
        g, top = build(["  M  "])
        g._add(EXTRA_LIFE_AT)
        self.assertEqual(g.lives, START_LIVES + 1)
        g._add(EXTRA_LIFE_AT)
        self.assertEqual(g.lives, START_LIVES + 1)


class StageTest(unittest.TestCase):
    def test_stages_parse_and_are_sane(self):
        self.assertEqual([s[0] for s in STAGES], ["barrels", "rivets"])
        for kind, height, rows in STAGES:
            grid, start, kong, pauline, hammers, items = parse_stage(rows)
            g = Game(random.Random(0))
            self.assertEqual(len(hammers), 2, kind)
            self.assertIsNotNone(pauline)
            sx, sy = start
            self.assertIn(grid[sy + 1][sx], GIRDERISH)
            for hx, hy in hammers:
                # reachable by a straight-up jump from the girder below
                self.assertEqual(grid[hy][hx], EMPTY)
                self.assertIn(grid[hy + 2][hx], GIRDERISH, (kind, hx, hy))
            # every ladder column: a ladder top sits on a run of ladder
            # cells ending on a girder
            for y in range(GRID_H):
                for x in range(GRID_W):
                    if grid[y][x] == LTOP:
                        yy = y + 1
                        while grid[yy][x] == LADDER:
                            yy += 1
                        self.assertGreater(yy, y + 1, (kind, x, y))
                        self.assertEqual(grid[yy][x], GIRDER, (kind, x, y))
            del g
        rivets = parse_stage(STAGES[1][2])[0]
        self.assertEqual(sum(r.count(RIVET) for r in rivets), 8)
        self.assertEqual(sum(r.count("O") for r in parse_stage(
            STAGES[0][2])[0]), 1)

    def test_parse_rejects_malformed(self):
        good = STAGES[0][2]
        with self.assertRaises(ValueError):
            parse_stage(good[:-1])
        with self.assertRaises(ValueError):
            parse_stage([good[0][:-1]] + good[1:])
        with self.assertRaises(ValueError):
            parse_stage(["?" * GRID_W] + good[1:])
        with self.assertRaises(ValueError):
            parse_stage([r.replace("M", " ") for r in good])

    def test_loop_alternates_and_speeds_up(self):
        g = Game(random.Random(0))
        kinds = []
        for _ in range(5):
            kinds.append((g.kind, g.loop))
            g.phase = Phase("clear", CLEAR_TIME)
            g.finish_phase()
        self.assertEqual(kinds, [("barrels", 1), ("rivets", 1),
                                 ("barrels", 2), ("rivets", 2),
                                 ("barrels", 3)])
        self.assertLess(donkeykong.speed_factor(2), donkeykong.speed_factor(1))
        self.assertEqual(donkeykong.speed_factor(50), donkeykong.SPEED_MIN)
        self.assertEqual(donkeykong.bonus_start(2), BONUS_START + 1000)

    def test_reaching_pauline_clears_barrels(self):
        g = Game(random.Random(0))
        quiet(g)
        g.player = donkeykong.Jumpman(16, 2)
        g.climb(-1)
        g.update(MOVE_STEP)
        g.climb(-1)
        self.assertEqual(g.player.y, 0)
        self.assertEqual(g.phase.kind, "clear")
        self.assertEqual(g.score, BONUS_START)
        run_for(g, CLEAR_TIME + 0.05)
        self.assertEqual(g.kind, "rivets")
        self.assertEqual(g.score, BONUS_START)

    def test_death_lives_and_game_over(self):
        g = Game(random.Random(0))
        for lives in range(START_LIVES, 0, -1):
            self.assertEqual(g.lives, lives)
            quiet(g)
            p = g.player
            g.fireballs.append(Fireball(1, p.x + 1, p.y, -1))
            run_for(g, 1.0, until=lambda gg: gg.phase is not None)
            self.assertEqual(g.phase.kind, "dying")
            run_for(g, DEATH_TIME + 0.05)
        self.assertTrue(g.game_over)
        self.assertEqual(g.lives, 0)
        self.assertFalse(g.update(1.0))
        self.assertFalse(g.move(1))

    def test_touching_kong_kills(self):
        g = Game(random.Random(0))
        quiet(g)
        g.player = donkeykong.Jumpman(4, 3)
        g.move(-1)
        self.assertIsNone(g.phase)
        g.update(MOVE_STEP)
        g.move(-1)
        self.assertEqual(g.phase.kind, "dying")


# --------------------------------------------------------------------------
# Every stage can be cleared: a search over Jumpman's moves (no hazards)
# --------------------------------------------------------------------------

ACTIONS = (("L", lambda g: g.move(-1)), ("R", lambda g: g.move(1)),
           ("U", lambda g: g.climb(-1)), ("D", lambda g: g.climb(1)),
           ("JL", lambda g: g.jump(-1)), ("JR", lambda g: g.jump(1)))


def solver_game(stage_no):
    g = Game(random.Random(3), stage_no)
    quiet(g)
    g.hammers = []   # a hammer would stop Jumpman climbing mid-route
    g.items = []
    return g


def snapshot(g):
    return (g.player.x, g.player.y, tuple("".join(r) for r in g.grid))


def restore(g, snap):
    g.player = donkeykong.Jumpman(snap[0], snap[1])
    g.grid = [list(r) for r in snap[2]]
    g.phase = None
    g.bonus = BONUS_START


def act(g, action):
    """Apply an action and let it play out; True if it did anything."""
    if not dict(ACTIONS)[action](g):
        return False
    settle(g)
    return True


def search(g, goal):
    """Shortest action list from g's state to one where goal(g) holds."""
    start = snapshot(g)
    seen = {start}
    todo = deque([(start, [])])
    while todo:
        snap, path = todo.popleft()
        for name, _ in ACTIONS:
            restore(g, snap)
            if not act(g, name):
                continue
            if g.phase is not None and g.phase.kind == "dying":
                continue
            if goal(g):
                return path + [name]
            nxt = snapshot(g)
            if g.phase is None and nxt not in seen:
                seen.add(nxt)
                todo.append((nxt, path + [name]))
    return None


class SolvableTest(unittest.TestCase):
    def replay(self, stage_no, path):
        g = solver_game(stage_no)
        for name in path:
            self.assertTrue(act(g, name), name)
            self.assertFalse(g.phase is not None and g.phase.kind == "dying")
        return g

    def test_barrels_stage_reaches_pauline(self):
        g = solver_game(1)
        path = search(g, lambda gg: gg.phase is not None
                      and gg.phase.kind == "clear")
        self.assertIsNotNone(path)
        g = self.replay(1, path)
        self.assertEqual(g.phase.kind, "clear")
        self.assertGreater(g.bonus, 0)

    def test_rivets_stage_can_pull_every_rivet(self):
        g = solver_game(2)
        self.assertEqual(g.kind, "rivets")
        path = []
        for _ in range(8):
            left = g.rivets_left()
            part = search(g, lambda gg, n=left: gg.rivets_left() < n)
            self.assertIsNotNone(part, "stranded with %d rivets" % left)
            restore(g, snapshot(g))
            g2 = self.replay(2, path + part)
            path += part
            g = g2
            if g.phase is not None:
                break
        g = self.replay(2, path)
        self.assertEqual(g.rivets_left(), 0)
        self.assertEqual(g.phase.kind, "clear")
        self.assertGreater(g.bonus, 0)


# --------------------------------------------------------------------------
# Save / restore
# --------------------------------------------------------------------------

def busy_game():
    g = Game(random.Random(42))
    g.bonus = 10 ** 6
    g.lives = 9
    keys = [ord(c) for c in "llllkkk xxhhh  z"] * 3
    rnd = random.Random(7)
    for i in range(400):
        if i % 7 == 0:
            handle_key(g, rnd.choice(keys))
        g.update(0.02)
        if g.phase is not None:
            g.finish_phase()
    return g


class SaveTest(unittest.TestCase):
    def test_round_trip_through_json(self):
        g = busy_game()
        self.assertTrue(g.barrels)
        data = json.loads(json.dumps(g.to_save()))
        r = Game.from_save(data)
        self.assertIsNotNone(r)
        self.assertEqual(r.to_save(), g.to_save())
        self.assertEqual(r.rng.random(), g.rng.random())

    def test_restored_game_plays_identically(self):
        for stage_no in (1, 2):
            g = Game(random.Random(11), stage_no)
            g.bonus = 10 ** 6
            run_for(g, 3.0, step=0.02)
            g.hammer_t = 4.0
            g.player.run_dir, g.player.run_t = 1, 0.2
            r = Game.from_save(json.loads(json.dumps(g.to_save())))
            rnd = random.Random(3)
            keys = [ord(c) for c in "hjkl zx"]
            for i in range(600):
                if i % 5 == 0:
                    ch = rnd.choice(keys)
                    handle_key(g, ch)
                    handle_key(r, ch)
                g.update(0.02)
                r.update(0.02)
                if g.phase is not None and g.phase.elapsed > 0.5:
                    g.finish_phase()
                    r.finish_phase()
            self.assertEqual(r.to_save(), g.to_save(), stage_no)
            self.assertEqual(r.score, g.score)

    def test_mid_jump_and_rivets_survive(self):
        g = Game(random.Random(1), 2)
        g.grid[4][7] = EMPTY
        g.player = donkeykong.Jumpman(10, 22)
        g.jump(1)
        g.fireballs.append(Fireball(9, 3, 11, 1))
        g.fireballs[0].climb = -1
        r = Game.from_save(json.loads(json.dumps(g.to_save())))
        self.assertEqual(r.grid[4][7], EMPTY)
        self.assertEqual(r.rivets_left(), 7)
        self.assertEqual(r.player.jump, 1)
        self.assertEqual(r.fireballs[0].climb, -1)

    def test_phase_resolved_before_save(self):
        g = Game(random.Random(0))
        g.phase = Phase("dying", DEATH_TIME)
        g.finish_phase()
        self.assertIsNone(g.phase)
        self.assertEqual(g.lives, START_LIVES - 1)

    def test_corrupt_or_incompatible_is_none(self):
        good = json.loads(json.dumps(busy_game().to_save()))

        def variant(**kw):
            d = json.loads(json.dumps(good))
            d.update(kw)
            return d

        bad_grid = list(good["grid"])
        bad_grid[0] = bad_grid[0][:-1]
        moved = list(good["grid"])
        moved[23] = " " * GRID_W                    # floor gone
        player = list(good["player"])
        player[0], player[1] = 0, 23                # inside the floor
        cases = [
            None, [], {}, "x",
            variant(version=SAVE_VERSION + 1),
            variant(grid=bad_grid),
            variant(grid=good["grid"][:-1]),
            variant(grid=moved),
            variant(player=player),
            variant(player=[1, 2]),
            variant(barrels=[[1, 2, 3]]),
            variant(barrels=[[1, 5, 3, 1, "fly", 0.0]]),
            variant(fireballs=[[1, 5, 3, 2, 0, 0.0, 0]]),
            variant(hammers=[["a", 1]]),
            variant(rng=[1, 2, 3]),
            variant(lives=0),
            variant(stage_no=0),
            variant(score="lots"),
            variant(bonus=-5),
        ]
        no_rng = json.loads(json.dumps(good))
        del no_rng["rng"]
        cases.append(no_rng)
        for d in cases:
            self.assertIsNone(Game.from_save(d), repr(d)[:80])


# --------------------------------------------------------------------------
# Keys and a random-input soak
# --------------------------------------------------------------------------

class KeyTest(unittest.TestCase):
    def test_move_and_jump_keys(self):
        g, top = build(["          M          "])
        self.assertTrue(handle_key(g, ord("d")))
        self.assertEqual(g.player.x, 11)
        g.update(MOVE_STEP)
        self.assertTrue(handle_key(g, ord("h")))
        settle(g)
        self.assertTrue(handle_key(g, ord("x")))
        settle(g)
        self.assertEqual(g.player.x, 13)
        self.assertTrue(handle_key(g, ord(",")))
        settle(g)
        self.assertEqual(g.player.x, 10)
        self.assertTrue(handle_key(g, ord(" ")))
        self.assertFalse(handle_key(g, ord("q")))  # q belongs to the loop

    def test_pause_works_even_mid_phase(self):
        g = Game(random.Random(0))
        g.phase = Phase("dying", DEATH_TIME)
        handle_key(g, ord("p"))
        self.assertTrue(g.paused)
        self.assertFalse(g.update(5.0))
        self.assertIsNotNone(g.phase)
        self.assertFalse(handle_key(g, ord("l")))
        handle_key(g, ord("p"))
        self.assertFalse(g.paused)


class SoakTest(unittest.TestCase):
    def test_random_play_never_crashes_or_stalls(self):
        keys = [ord(c) for c in "hjklwasd zx,."] + [
            donkeykong.curses.KEY_LEFT, donkeykong.curses.KEY_UP]
        for seed in range(4):
            g = Game(random.Random(seed))
            rnd = random.Random(seed)
            phases = 0
            for i in range(4000):
                if rnd.random() < 0.3:
                    handle_key(g, rnd.choice(keys))
                was = g.phase
                g.update(0.02)
                if was is not None and g.phase is None:
                    phases += 1
                if g.game_over:
                    g.reset()
                p = g.player
                self.assertTrue(g.in_bounds(p.x, p.y))
                self.assertNotIn(g.grid[p.y][p.x], (GIRDER, RIVET, "O"))
                for h in g.hazards():
                    self.assertTrue(g.in_bounds(h.x, h.y))
                self.assertLessEqual(len(g.barrels), donkeykong.MAX_BARRELS)
            self.assertGreater(phases, 0, seed)  # deaths come and resolve


# --------------------------------------------------------------------------
# Layout and rendering
# --------------------------------------------------------------------------

class LayoutTest(unittest.TestCase):
    REGIONS = ("board", "stats", "controls", "agents", "hint")

    def fits(self, plan, cols, rows):
        for key in self.REGIONS:
            region = plan[key]
            if region is None:
                continue
            y, x, h, w = region
            self.assertGreaterEqual(min(x, y), 0, key)
            self.assertGreaterEqual(min(h, w), 1, key)
            self.assertLessEqual(x + w, cols, key)
            self.assertLessEqual(y + h, rows, key)
        regions = [plan[k] for k in self.REGIONS if plan[k] is not None]
        for i, (ay, ax, ah, aw) in enumerate(regions):
            for by, bx, bh, bw in regions[i + 1:]:
                overlap = (ay < by + bh and by < ay + ah
                           and ax < bx + bw and bx < ax + aw)
                self.assertFalse(overlap, (cols, rows, plan))

    def composition(self, plan):
        regions = [plan[k] for k in ("board", "stats", "controls", "agents")
                   if plan[k] is not None]
        return (min(r[0] for r in regions), min(r[1] for r in regions),
                max(r[0] + r[2] for r in regions),
                max(r[1] + r[3] for r in regions))

    def centred(self, plan, cols, rows):
        top, left, bottom, right = self.composition(plan)
        self.assertLessEqual(abs(left - (cols - right)), 1, (cols, rows))
        last = rows - 1 if plan["hint"] is not None else rows
        self.assertGreaterEqual(top, 1)
        self.assertLessEqual(abs((top - 1) - (last - bottom)), 1, (cols, rows))

    def test_full_at_popup_93x28(self):
        plan = layout_plan(93, 28)
        self.assertEqual(plan["tier"], "full")
        for key in ("stats", "controls", "agents"):
            self.assertIsNotNone(plan[key], key)
            self.assertEqual(plan[key][3], 30, key)
        self.assertIsNone(plan["hint"])
        self.fits(plan, 93, 28)
        self.centred(plan, 93, 28)
        sy, sx, sh, sw = plan["stats"]
        cy, cx, ch, cw = plan["controls"]
        ay, ax, ah, aw = plan["agents"]
        by, bx, bh, bw = plan["board"]
        self.assertEqual((by, bx, bh, bw), (1, 2, 26, 58))
        self.assertEqual(sx, cx)
        self.assertEqual(sx, ax)
        self.assertEqual(sx, bx + bw + 1)
        self.assertEqual((sy, sy + sh, cy + ch), (by, cy, ay))
        self.assertEqual(ay + ah, by + bh)
        self.assertEqual(ch, donkeykong.controls_height(
            donkeykong.CONTROLS, 30))

    def test_no_agents_tier(self):
        for cols in (80, 83):
            plan = layout_plan(cols, 28)
            self.assertEqual(plan["tier"], "side", cols)
            self.assertIsNone(plan["agents"])
            self.assertIsNotNone(plan["stats"])
            self.assertEqual(plan["controls"][2], donkeykong.controls_height(
                donkeykong.CONTROLS, plan["controls"][3]))
            self.fits(plan, cols, 28)
            self.centred(plan, cols, 28)

    def test_no_side_column_tier(self):
        for cols in (78, 70, 58):
            plan = layout_plan(cols, 28)
            self.assertEqual(plan["tier"], "board", cols)
            self.assertIsNone(plan["stats"])
            self.assertIsNone(plan["controls"])
            self.assertEqual(plan["hint"][0], 27)
            self.fits(plan, cols, 28)
            self.centred(plan, cols, 28)
        plan = layout_plan(70, 27)  # no spare row: the hint joins the header
        self.assertIsNone(plan["hint"])
        self.assertTrue(plan["hint_in_header"])
        self.fits(plan, 70, 27)
        self.centred(plan, 70, 27)

    def test_too_small(self):
        for cols, rows in ((57, 28), (93, 26), (80, 24), (40, 10)):
            self.assertTrue(layout_plan(cols, rows)["too_small"], (cols, rows))

    def test_many_sizes_fit_and_centre(self):
        for cols, rows in ((93, 28), (93, 27), (89, 28), (85, 30), (84, 28),
                           (79, 28), (78, 27), (60, 27), (58, 27),
                           (120, 40), (200, 60)):
            plan = layout_plan(cols, rows)
            self.assertFalse(plan["too_small"], (cols, rows))
            self.fits(plan, cols, rows)
            self.centred(plan, cols, rows)
            if plan["controls"] is None:
                self.assertTrue(plan["hint"] is not None
                                or plan["hint_in_header"], (cols, rows))

    def test_key_hint_leads_with_help(self):
        self.assertTrue(donkeykong.key_hint().startswith("? help"))


class ConstantsTest(unittest.TestCase):
    def test_controls_cover_every_key(self):
        keys = " ".join(k for k, _ in donkeykong.CONTROLS)
        for token in ("?", "q", "p", "r", "z", "x", ",", ".", "arrows",
                      "wasd", "hjkl", "space"):
            self.assertIn(token, keys)
        self.assertEqual(donkeykong.CONTROLS[-1], ("q", "menu"))

    def test_help_sections(self):
        heads = {h: b for h, b in donkeykong.HELP}
        for need in ("Goal", "Controls", "Scoring", "Tips", "Legend",
                     "Barrels (25m)", "Rivets (100m)", "Hammers",
                     "Bonus timer"):
            self.assertIn(need, heads)
            self.assertTrue(heads[need].strip(), need)
        text = " ".join(h + " " + b for h, b in donkeykong.HELP)
        self.assertTrue(all(ord(c) < 128 for c in text))
        for word in ("ladder", "broken", "jump", "fall", "rivet", "hammer",
                     "fireball", "oil drum", "@>", "()", "**", "[]"):
            self.assertIn(word, text)
        self.assertIn(str(JUMP_SCORE), heads["Scoring"])
        self.assertIn(str(RIVET_SCORE), heads["Scoring"])
        self.assertIn("%d seconds" % HAMMER_TIME, heads["Hammers"])
        self.assertIn(str(BONUS_START), heads["Bonus timer"])
        self.assertIn("%d rows" % SAFE_FALL, heads["Jumping and falling"])


class HelpKeyTest(unittest.TestCase):
    def test_help_pauses_and_closing_resumes(self):
        g = Game(random.Random(1))
        h = donkeykong.HelpOverlay(donkeykong.TITLE, donkeykong.HELP)
        consumed, pbh = donkeykong.help_key(g, h, ord("?"), False)
        self.assertTrue(consumed and pbh and h.is_open and g.paused)
        self.assertFalse(g.update(1.0))
        x = g.player.x
        consumed, pbh = donkeykong.help_key(g, h, donkeykong.curses.KEY_DOWN, pbh)
        self.assertTrue(consumed and h.is_open and g.paused)
        consumed, pbh = donkeykong.help_key(
            g, h, donkeykong.curses.KEY_RESIZE, pbh)
        self.assertFalse(consumed)
        self.assertTrue(h.is_open)
        consumed, pbh = donkeykong.help_key(g, h, ord("l"), pbh)
        self.assertTrue(consumed)
        self.assertFalse(h.is_open or g.paused or pbh)
        self.assertEqual(g.player.x, x)
        self.assertEqual(donkeykong.help_key(g, h, ord("l"), False),
                         (False, False))

    def test_help_keeps_an_existing_pause(self):
        g = Game(random.Random(1))
        g.paused = True
        h = donkeykong.HelpOverlay(donkeykong.TITLE, donkeykong.HELP)
        consumed, pbh = donkeykong.help_key(g, h, ord("?"), False)
        self.assertTrue(consumed and h.is_open)
        donkeykong.help_key(g, h, ord("x"), pbh)
        self.assertFalse(h.is_open)
        self.assertTrue(g.paused)


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
            raise donkeykong.curses.error()
        child = FakeWin(h, w, self.root, self.y0 + y, self.x0 + x)
        self.root.children.append(child)
        return child

    def erase(self):
        self.writes = []

    def addstr(self, y, x, text, attr=0):
        if y < 0 or y >= self.rows or x < 0 or x + len(text) > self.cols:
            self.root.violations.append((y, x, text))
            raise donkeykong.curses.error()
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
               help_open=False):
        scr = FakeWin(rows, cols)
        overlay = donkeykong.HelpOverlay(donkeykong.TITLE, donkeykong.HELP)
        if help_open:
            overlay.open()
        with mock.patch("curses.color_pair", return_value=0):
            lay = donkeykong.Layout(scr)
            donkeykong.draw(game, lay, self.session(attention), resume, 999,
                            overlay)
        return scr, lay

    @staticmethod
    def all_text(scr):
        out = [t for _, _, t in scr.writes]
        for child in scr.children:
            out.extend(t for _, _, t in child.writes)
        return "\n".join(out)

    def games(self):
        yield "fresh", Game(random.Random(1))
        busy = busy_game()
        busy.hammer_t = 5.0
        busy.popups.append([27, 0, "800", 0.5])
        yield "busy", busy
        rv = Game(random.Random(1), 2)
        rv.grid[4][7] = EMPTY
        rv.fireballs.append(Fireball(1, 3, 10, 1))
        yield "rivets", rv
        for kind_no in (1, 2):
            for elapsed in (0.0, 1.2, 2.4):
                cl = Game(random.Random(1), kind_no)
                cl.phase = Phase("clear", CLEAR_TIME)
                cl.phase.elapsed = elapsed
                yield "clear", cl
        dy = Game(random.Random(1))
        dy.phase = Phase("dying", DEATH_TIME)
        yield "dying", dy
        go = Game(random.Random(1))
        go.game_over = True
        go.lives = 0
        yield "game_over", go
        pa = Game(random.Random(1), 2)
        pa.paused = True
        yield "paused", pa

    def test_full_frame_in_bounds(self):
        for name, game in self.games():
            scr, lay = self.render(game)
            self.assertEqual(scr.violations, [], name)
            board = lay.board
            self.assertEqual((board.y0, board.x0), (1, 2))
            self.assertEqual(board.getmaxyx(), (GRID_H + 2, GRID_W * 2 + 2))
            self.assertIsNotNone(lay.agents)
            self.assertIsNotNone(lay.controls)
            for child in scr.children:
                self.assertLessEqual(child.x0 + child.cols, 93)
                self.assertLessEqual(child.y0 + child.rows, 28)
            # field cells are drawn as 2-column glyphs on the cell grid
            # (ladder tops as two 1-column halves; popups are score text)
            popups = {t for _, _, t, _ in game.popups}
            for y, x, t in board.writes:
                if 0 < y < board.rows - 1 and 0 < x < board.cols - 1 \
                        and t not in popups and t.strip() \
                        and not t.startswith(" "):
                    if len(t) == 1:
                        self.assertIn(t, "H=", name)
                    else:
                        self.assertEqual(len(t), 2, (name, t))
                        self.assertEqual((x - 1) % 2, 0, (name, t))

    def test_actors_and_overlays_drawn(self):
        g = busy_game()
        scr, lay = self.render(g)
        p = g.player
        writes = lay.board.writes
        if g.phase is None:
            self.assertIn((1 + p.y, 1 + p.x * 2,
                           donkeykong.PLAYER_GLYPHS[p.facing]), writes)
        for b in g.barrels:
            self.assertIn((1 + b.y, 1 + b.x * 2, "()"), writes)
        fresh = Game(random.Random(1))
        scr, lay = self.render(fresh)
        texts = [t for _, _, t in lay.board.writes]
        self.assertIn("{(", texts)          # Kong
        self.assertIn("UU", texts)          # the oil drum
        self.assertIn("T ", texts)          # hammers
        go = Game(random.Random(1))
        go.game_over = True
        scr, lay = self.render(go)
        texts = [t for _, _, t in lay.board.writes]
        self.assertIn(" GAME OVER ", texts)
        self.assertIn(" r restart | ? help | q menu ", texts)
        rv = Game(random.Random(1), 2)
        scr, lay = self.render(rv)
        texts = [t for _, _, t in lay.board.writes]
        self.assertEqual(texts.count("[]"), 8)

    def test_banners_and_smaller_tiers(self):
        agent = AgentSnapshot("p1", "claude", "claude", "blocked")
        for kw in ({"attention": (agent, "working")}, {"resume": True}):
            scr, lay = self.render(Game(random.Random(1)), **kw)
            self.assertEqual(scr.violations, [])
        for cols, rows in ((82, 28), (70, 28), (70, 27), (40, 10)):
            scr, lay = self.render(Game(random.Random(1)), cols, rows)
            self.assertEqual(scr.violations, [], (cols, rows))
        scr, lay = self.render(Game(random.Random(1)), 82, 28)
        self.assertIsNone(lay.agents)
        self.assertIsNotNone(lay.controls)
        scr, lay = self.render(Game(random.Random(1)), 70, 28)
        header = [t for y, _, t in scr.writes if y == 0]
        self.assertTrue(any("score" in t for t in header))
        hint = [t for y, _, t in scr.writes if y == 27]
        self.assertTrue(hint and hint[0].startswith("? help"), hint)
        scr, lay = self.render(Game(random.Random(1)), 70, 27)
        header = [t for y, _, t in scr.writes if y == 0]
        self.assertTrue(any("? help" in t for t in header))
        scr, lay = self.render(Game(random.Random(1)), 40, 10)
        self.assertTrue(any("pane too small" in t for _, _, t in scr.writes))

    def test_controls_box_and_stats_at_popup_size(self):
        scr, lay = self.render(Game(random.Random(1)))
        text = self.all_text(scr)
        for keys, action in donkeykong.CONTROLS:
            self.assertIn(keys, text)
        self.assertIn("donkey kong", text)
        self.assertIn("score 0", text)
        self.assertIn("bonus %d" % BONUS_START, text)
        self.assertIn("agents", text)
        # every stat fits the stats box at the popup size
        sh, sw = lay.stats.getmaxyx()
        worst = Game(random.Random(1), 2)
        worst.score, worst.hammer_t = 123456, 5.0
        lines = donkeykong.pack_items(
            donkeykong.stats_items(worst, 999999), sw - 2)
        self.assertLessEqual(len(lines), sh - 2)

    def test_help_overlay_renders_at_every_tier(self):
        for cols, rows in ((93, 28), (82, 28), (70, 28), (70, 27), (40, 10)):
            for name, game in self.games():
                scr, lay = self.render(game, cols, rows, help_open=True)
                self.assertEqual(scr.violations, [], (cols, rows, name))
            text = self.all_text(scr)
            self.assertIn("Donkey Kong - help", text)
            if not lay.too_small:
                self.assertIn("Goal", text, (cols, rows))
                self.assertIn("? help" if lay.controls is None else " keys ",
                              text, (cols, rows))


if __name__ == "__main__":
    unittest.main()
