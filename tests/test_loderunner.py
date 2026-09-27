"""Unit tests for the pure Lode Runner logic in loderunner.py (no terminal)."""

import json
import os
import random
import sys
import types
import unittest
from collections import deque
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import loderunner
from common import AgentSnapshot
from loderunner import (
    BAR,
    BRICK,
    CLEAR_TIME,
    DEATH_TIME,
    EMPTY,
    GOLD,
    GOLD_SCORE,
    GRID_H,
    GRID_W,
    GUARD_FALL_STEP,
    GUARD_STEP,
    HIDDEN,
    HOLE_TIME,
    KILL_SCORE,
    LADDER,
    LEVEL_SCORE,
    LEVELS,
    PLAYER_FALL_STEP,
    SAVE_VERSION,
    START_LIVES,
    TRAP_SCORE,
    TRAP_TIME,
    Game,
    Guard,
    Phase,
    handle_key,
    layout_plan,
    parse_level,
)


def build(rows):
    """A 26x16 test level: `rows` sit at the bottom, right above a solid
    floor row (y = 15); everything above is empty. Returns (game, top) where
    rows[i] is at y = top + i."""
    rows = [r.ljust(GRID_W) for r in rows]
    top = GRID_H - 1 - len(rows)
    full = [" " * GRID_W] * top + rows + ["@" * GRID_W]
    return Game(random.Random(0), levels=[("test", full)]), top


def run_for(game, seconds, step=0.02, until=None):
    t = 0.0
    while t < seconds:
        game.update(step)
        t += step
        if until is not None and until():
            return True
    return False


def explore(game, starts):
    """BFS over the player's movement rules (guards ignored, no digging).
    Returns (settled cells, every cell passed through incl. falls)."""
    def fall(x, y):
        path = [(x, y)]
        while not game.supported(x, y) and game.passable(x, y + 1):
            y += 1
            path.append((x, y))
        return (x, y), path

    seen, cells, queue = set(), set(), deque()
    for s in starts:
        n, path = fall(*s)
        cells.update(path)
        if n not in seen:
            seen.add(n)
            queue.append(n)
    while queue:
        x, y = queue.popleft()
        nxt = [(x + dx, y) for dx in (-1, 1) if game.passable(x + dx, y)]
        k = game.kind(x, y)
        if k == LADDER and game.passable(x, y - 1):
            nxt.append((x, y - 1))
        if game.passable(x, y + 1) and (
                k in (LADDER, BAR) or game.kind(x, y + 1) == LADDER):
            nxt.append((x, y + 1))
        for c in nxt:
            n, path = fall(*c)
            cells.update(path)
            if n not in seen:
                seen.add(n)
                queue.append(n)
    return seen, cells


# --------------------------------------------------------------------------
# Movement
# --------------------------------------------------------------------------

class MovementTest(unittest.TestCase):
    def test_walk_one_cell_per_press_and_walls_block(self):
        g, top = build(["@P  "])
        p = g.player
        self.assertEqual((p.x, p.y), (1, top))
        self.assertTrue(g.move(1))
        self.assertEqual(p.x, 2)
        self.assertTrue(g.move(-1))
        self.assertFalse(g.move(-1))  # solid block
        self.assertEqual(p.x, 1)

    def test_ladder_climb_up_and_down(self):
        g, top = build(["  H", "  H", "  HP"])
        p = g.player
        g.move(-1)
        self.assertEqual((p.x, p.y), (2, top + 2))
        self.assertTrue(g.climb(-1))
        self.assertTrue(g.climb(-1))
        self.assertEqual(p.y, top)
        self.assertTrue(g.climb(-1))  # onto the empty cell above the top
        self.assertEqual(p.y, top - 1)
        self.assertFalse(p.falling)    # standing on the ladder top
        self.assertFalse(g.climb(-1))  # no ladder here
        self.assertTrue(g.climb(1))    # back down onto the ladder
        self.assertEqual(p.y, top)

    def test_cannot_climb_without_ladder(self):
        g, top = build(["P "])
        self.assertFalse(g.climb(-1))
        self.assertFalse(g.climb(1))  # solid floor below

    def test_bar_hang_traverse_and_drop(self):
        g, top = build([" H---", " H", " H", " HP"])
        p = g.player
        g.move(-1)
        for _ in range(3):
            g.climb(-1)
        self.assertEqual((p.x, p.y), (1, top))
        self.assertTrue(g.move(1))
        self.assertEqual(g.kind(p.x, p.y), BAR)
        g.update(0.5)
        self.assertEqual((p.x, p.y), (2, top))  # hanging, not falling
        self.assertTrue(g.move(1))
        self.assertTrue(g.climb(1))  # drop off the bar
        self.assertTrue(p.falling)
        run_for(g, 1.0)
        self.assertEqual(p.y, top + 3)
        self.assertFalse(p.falling)

    def test_gravity_is_timed_and_blocks_control(self):
        g, top = build(["  P", "###", "", ""])
        p = g.player
        self.assertTrue(g.move(1))  # off the ledge
        self.assertTrue(p.falling)
        self.assertEqual(p.y, top)
        self.assertFalse(g.move(1))   # no control while falling
        self.assertFalse(g.climb(1))
        self.assertFalse(g.dig(-1))
        g.update(PLAYER_FALL_STEP * 0.5)
        self.assertEqual(p.y, top)  # not instant
        g.update(PLAYER_FALL_STEP * 0.6)
        self.assertEqual(p.y, top + 1)
        run_for(g, 1.0)
        self.assertEqual(p.y, top + 3)
        self.assertFalse(p.falling)
        self.assertTrue(g.move(1))

    def test_falling_catches_a_bar(self):
        g, top = build(["  P", "###", "   ---", ""])
        g.move(1)
        run_for(g, 1.0)
        self.assertEqual((g.player.y, g.kind(3, top + 2)), (top + 2, BAR))
        self.assertFalse(g.player.falling)

    def test_false_brick_is_a_trapdoor(self):
        g, top = build(["P ", "#X", "  "])
        g.move(1)
        self.assertTrue(g.player.falling)
        run_for(g, 1.0)
        self.assertEqual(g.player.y, top + 2)


# --------------------------------------------------------------------------
# Digging and holes
# --------------------------------------------------------------------------

class DigTest(unittest.TestCase):
    def test_dig_both_sides(self):
        g, top = build([" P ", "#####"])
        self.assertTrue(g.dig(1))
        self.assertTrue(g.dig(-1))
        self.assertIn((2, top + 1), g.holes)
        self.assertIn((0, top + 1), g.holes)
        self.assertEqual(g.kind(2, top + 1), EMPTY)
        self.assertEqual(g.grid[top + 1][2], BRICK)  # the brick comes back

    def test_only_brick(self):
        g, top = build([" P ", "@#@X"])
        self.assertFalse(g.dig(-1))  # solid
        self.assertFalse(g.dig(1))   # solid
        g2, _ = build([" P ", "##X"])
        self.assertFalse(g2.dig(1))  # false brick

    def test_needs_empty_above(self):
        for above in (LADDER, BAR, GOLD, BRICK, "E"):
            g, top = build([" P" + above, "####"])
            self.assertFalse(g.dig(1), above)
            self.assertTrue(g.dig(-1), above)

    def test_not_twice_and_not_while_falling(self):
        g, top = build([" P ", "####"])
        self.assertTrue(g.dig(1))
        self.assertFalse(g.dig(1))
        g.player.falling = True
        self.assertFalse(g.dig(-1))

    def test_hole_refills_after_timer(self):
        g, top = build([" P ", "####"])
        g.dig(1)
        run_for(g, HOLE_TIME - 0.2)
        self.assertIn((2, top + 1), g.holes)
        run_for(g, 0.4)
        self.assertNotIn((2, top + 1), g.holes)
        self.assertEqual(g.kind(2, top + 1), BRICK)

    def test_refill_kills_player_inside(self):
        g, top = build([" P ", "####"])
        g.dig(1)
        g.move(1)
        run_for(g, 0.5)
        self.assertEqual((g.player.x, g.player.y), (2, top + 1))  # in the hole
        run_for(g, HOLE_TIME, until=lambda: g.phase is not None)
        self.assertEqual(g.phase.kind, "dying")
        g.finish_phase()
        self.assertEqual(g.lives, START_LIVES - 1)


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------

class GuardTest(unittest.TestCase):
    def trap_setup(self, gold=False):
        g, top = build(["P    E", "######"])
        guard = g.guards[0]
        guard.gold = gold
        guard.drop_t = 100.0  # no spontaneous drop on the way
        g.holes[(3, top + 1)] = HOLE_TIME
        trapped = run_for(g, 2.0, until=lambda: guard.trap is not None)
        self.assertTrue(trapped)
        self.assertEqual((guard.x, guard.y), (3, top + 1))
        return g, top, guard

    def test_guard_falls_into_hole_and_is_trapped(self):
        g, top, guard = self.trap_setup()
        self.assertEqual(g.score, TRAP_SCORE)
        run_for(g, TRAP_TIME * 0.8)
        self.assertEqual((guard.x, guard.y), (3, top + 1))  # still stuck
        self.assertEqual(g.score, TRAP_SCORE)  # scored once

    def test_guard_climbs_out_diagonally_toward_player(self):
        g, top, guard = self.trap_setup()
        out = run_for(g, TRAP_TIME + GUARD_STEP + 0.2,
                      until=lambda: guard.trap is None)
        self.assertTrue(out)
        self.assertEqual((guard.x, guard.y), (2, top))
        self.assertEqual(g.score, TRAP_SCORE)
        self.assertIn((3, top + 1), g.holes)  # hole still open

    def test_trapped_guard_drops_gold_above_hole(self):
        g, top, guard = self.trap_setup(gold=True)
        self.assertFalse(guard.gold)
        self.assertEqual(g.grid[top][3], GOLD)
        self.assertEqual(g.gold_left(), 1)

    def test_refill_kills_guard_and_respawns_at_top(self):
        g, top, guard = self.trap_setup()
        guard.trap = 100.0  # keep it in the hole
        g.holes[(3, top + 1)] = 0.01
        g.update(0.02)
        self.assertEqual(guard.y, 0)
        self.assertIsNone(guard.trap)
        self.assertEqual(g.score, TRAP_SCORE + KILL_SCORE)
        self.assertEqual(g.kind(3, top + 1), BRICK)

    def test_walk_over_trapped_guards_head(self):
        g, top, guard = self.trap_setup()
        g.player.x = 2
        self.assertTrue(g.move(1))
        self.assertFalse(g.player.falling)
        g.update(0.05)
        self.assertEqual(g.player.y, top)
        self.assertTrue(g.move(1))
        self.assertIsNone(g.phase)

    def test_guard_reaches_player_row_via_ladder_down(self):
        g, top = build([
            "              E",
            "   ########H#######",
            "           H",
            "  P        H",
            "##########################",
        ])
        guard = g.guards[0]
        self.assertEqual(g.guard_choice(guard), (-1, 0))  # toward the ladder
        ok = run_for(g, 10.0, until=lambda: guard.y == g.player.y)
        self.assertTrue(ok)
        self.assertEqual(guard.x, 11)

    def test_guard_reaches_player_row_via_ladder_up(self):
        g, top = build([
            "  P",
            "###########H",
            "           H",
            "           H  E",
            "##########################",
        ])
        guard = g.guards[0]
        ok = run_for(g, 10.0, until=lambda: guard.y == g.player.y)
        self.assertTrue(ok)
        self.assertEqual(guard.x, 11)

    def test_guard_on_same_row_walks_at_player_and_kills(self):
        g, top = build(["P      E", "########"])
        guard = g.guards[0]
        self.assertEqual(g.guard_choice(guard), (-1, 0))
        died = run_for(g, 5.0, until=lambda: g.phase is not None)
        self.assertTrue(died)
        self.assertEqual(g.phase.kind, "dying")

    def test_guards_are_slower_than_a_step(self):
        g, top = build(["P      E", "########"])
        guard = g.guards[0]
        g.update(GUARD_STEP * 0.9)
        self.assertEqual(guard.x, 7)
        g.update(GUARD_STEP * 0.2)
        self.assertEqual(guard.x, 6)

    def test_guard_picks_up_and_later_drops_gold(self):
        g, top = build(["P    $E", "#######"])
        guard = g.guards[0]
        g.update(GUARD_STEP + 0.01)
        self.assertTrue(guard.gold)
        self.assertEqual(g.gold_left(), 1)
        guard.drop_t = 0.0
        g._maybe_drop_gold(guard)
        self.assertFalse(guard.gold)
        self.assertEqual(g.grid[guard.y][guard.x], GOLD)


# --------------------------------------------------------------------------
# Gold, escape ladder, level completion, death
# --------------------------------------------------------------------------

def escape_level():
    rows = ["      S".ljust(GRID_W)] * 13
    rows += ["  P $ S".ljust(GRID_W), "#" * GRID_W, "@" * GRID_W]
    return Game(random.Random(0), levels=[("esc", rows)])


class GoldAndExitTest(unittest.TestCase):
    def test_gold_escape_ladder_and_completion(self):
        g = escape_level()
        self.assertFalse(g.revealed)
        self.assertEqual(g.kind(6, 0), EMPTY)
        g.move(1)
        self.assertTrue(g.move(1))  # onto the gold
        self.assertEqual(g.score, GOLD_SCORE)
        self.assertEqual(g.gold_left(), 0)
        self.assertTrue(g.revealed)
        self.assertEqual(g.kind(6, 0), LADDER)
        g.move(1)
        g.move(1)
        for _ in range(12):
            self.assertTrue(g.climb(-1))
            self.assertIsNone(g.phase)
        self.assertTrue(g.climb(-1))
        self.assertEqual(g.player.y, 0)
        self.assertEqual(g.phase.kind, "level_clear")
        self.assertEqual(g.score, GOLD_SCORE + LEVEL_SCORE)
        run_for(g, CLEAR_TIME + 0.1)
        self.assertIsNone(g.phase)
        self.assertEqual(g.level, 2)
        self.assertEqual(g.lives, START_LIVES + 1)
        self.assertEqual((g.player.x, g.player.y), (2, 13))
        self.assertEqual(g.grid[13][4], GOLD)  # fresh level
        self.assertFalse(g.revealed)

    def test_hidden_ladder_is_air_before_reveal(self):
        g = escape_level()
        g.move(1)
        g.move(1)
        g.revealed = False  # pretend gold remains
        g.move(1)
        g.move(1)
        self.assertFalse(g.climb(-1))

    def test_guard_holding_last_gold_blocks_reveal(self):
        g = escape_level()
        g.grid[13][4] = EMPTY
        g.revealed = False
        guard = Guard(10, 13)
        guard.gold = True
        g.guards.append(guard)
        g._check_reveal()
        self.assertFalse(g.revealed)
        self.assertEqual(g.gold_left(), 1)


class DeathTest(unittest.TestCase):
    def test_touching_guard_death_lives_and_game_over(self):
        g, top = build(["P E", "###"])
        for lives_left in (2, 1):
            g.move(1)
            g.move(1)
            self.assertEqual(g.phase.kind, "dying")
            self.assertFalse(g.move(-1))  # frozen during the death phase
            run_for(g, DEATH_TIME * 0.5)
            self.assertIsNotNone(g.phase)
            run_for(g, DEATH_TIME)
            self.assertIsNone(g.phase)
            self.assertEqual(g.lives, lives_left)
            self.assertEqual((g.player.x, g.player.y), (0, top))  # restarted
        g.move(1)
        g.move(1)
        g.finish_phase()
        self.assertTrue(g.game_over)
        self.assertEqual(g.lives, 0)
        self.assertFalse(g.update(1.0))
        self.assertFalse(g.move(1))


# --------------------------------------------------------------------------
# Levels
# --------------------------------------------------------------------------

class LevelTest(unittest.TestCase):
    def test_at_least_five_levels(self):
        self.assertGreaterEqual(len(LEVELS), 5)

    def test_levels_parse_and_are_sane(self):
        for i, (name, rows) in enumerate(LEVELS):
            grid, (px, py), guards = parse_level(rows)
            g = Game(random.Random(0), level=i + 1)
            self.assertTrue(g.passable(px, py), name)
            self.assertTrue(1 <= len(guards) <= 5, name)
            self.assertGreater(g.gold_left(), 0, name)
            self.assertFalse(g.revealed, name)
            self.assertTrue(any(grid[0][x] == HIDDEN for x in range(GRID_W)),
                            name)
            for gx, gy in guards:
                self.assertTrue(g.passable(gx, gy), name)
                self.assertNotEqual((gx, gy), (px, py), name)

    def test_levels_solvable_without_digging(self):
        for i, (name, rows) in enumerate(LEVELS):
            g = Game(random.Random(0), level=i + 1)
            g.guards = []
            start = (g.player.x, g.player.y)
            gold = [(x, y) for y in range(GRID_H) for x in range(GRID_W)
                    if g.grid[y][x] == GOLD]
            _, cells = explore(g, [start])
            for c in gold:
                self.assertIn(c, cells, "%s: gold %r unreachable" % (name, c))
            self.assertFalse(any(y == 0 for _, y in cells),
                             "%s: top reachable before the escape" % name)
            g.revealed = True
            for c in gold + [start]:
                _, cells = explore(g, [c])
                self.assertTrue(any(y == 0 for _, y in cells),
                                "%s: no exit from %r" % (name, c))

    def test_level_cycle_wraps(self):
        g = Game(random.Random(0), level=len(LEVELS) + 1)
        self.assertEqual(g.level_index, 0)

    def test_parse_rejects_malformed(self):
        rows = list(LEVELS[0][1])
        with self.assertRaises(ValueError):
            parse_level(rows[:-1])
        with self.assertRaises(ValueError):
            parse_level([rows[0][:-1]] + rows[1:])
        with self.assertRaises(ValueError):
            parse_level([r.replace("P", " ") for r in rows])
        with self.assertRaises(ValueError):
            parse_level(["?" + rows[0][1:]] + rows[1:])


# --------------------------------------------------------------------------
# Save / restore
# --------------------------------------------------------------------------

class SaveTest(unittest.TestCase):
    def busy_game(self):
        g = Game(random.Random(42), level=1)
        g.score = 1234
        g.lives = 2
        g.holes[(3, 3)] = 2.5
        guard = g.guards[0]
        guard.x, guard.y, guard.trap, guard.gold = 3, 2, 1.25, True
        guard.drop_t = 3.5
        g.player.falling = True
        g.player.fall_t = 0.03
        g.revealed = True
        g.rng.random()
        return g

    def test_round_trip_through_json(self):
        g = self.busy_game()
        data = json.loads(json.dumps(g.to_save()))
        r = Game.from_save(data)
        self.assertIsNotNone(r)
        self.assertEqual(r.to_save(), g.to_save())
        self.assertEqual(r.rng.random(), g.rng.random())
        self.assertEqual(r.holes, {(3, 3): 2.5})
        self.assertEqual(r.guards[0].trap, 1.25)
        self.assertTrue(r.revealed and r.player.falling)

    def test_phase_resolved_before_save(self):
        g, top = build(["P E", "###"])
        g.move(1)
        g.move(1)
        g.finish_phase()
        self.assertIsNone(g.phase)
        self.assertEqual(g.lives, START_LIVES - 1)

    def test_corrupt_or_incompatible_is_none(self):
        good = json.loads(json.dumps(self.busy_game().to_save()))

        def variant(**kw):
            d = json.loads(json.dumps(good))
            d.update(kw)
            return d

        bad_grid = list(good["grid"])
        bad_grid[0] = bad_grid[0][:-1]
        wall_grid = list(good["grid"])
        wall_grid[good["player"][1]] = "@" * GRID_W
        cases = [
            None, [], {}, "x",
            variant(version=SAVE_VERSION + 1),
            variant(grid=bad_grid),
            variant(grid=good["grid"][:-1]),
            variant(grid=["?" * GRID_W] * GRID_H),
            variant(holes=[[0, 15, 1.0]]),        # solid, not brick
            variant(holes=[["a", 1, 1.0]]),
            variant(grid=wall_grid),               # player inside a wall
            variant(player=[1, 2]),
            variant(guards=[[1, 2, 3]]),
            variant(rng=[1, 2, 3]),
            variant(lives=0),
            variant(level=0),
            variant(score="lots"),
        ]
        no_rng = json.loads(json.dumps(good))
        del no_rng["rng"]
        cases.append(no_rng)
        for d in cases:
            self.assertIsNone(Game.from_save(d), repr(d)[:80])


# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------

class KeyTest(unittest.TestCase):
    def test_dig_and_move_keys(self):
        g, top = build([" P ", "#####"])
        self.assertTrue(handle_key(g, ord("z")))
        self.assertTrue(handle_key(g, ord(".")))
        self.assertEqual(set(g.holes), {(0, top + 1), (2, top + 1)})
        g2, _ = build([" P ", "#####"])
        self.assertTrue(handle_key(g2, ord(",")))
        self.assertTrue(handle_key(g2, ord("x")))
        self.assertTrue(handle_key(g2, ord("d")))
        self.assertEqual(g2.player.x, 2)
        self.assertFalse(handle_key(g2, ord("q")))  # q belongs to the loop

    def test_pause_works_even_mid_phase(self):
        g, top = build(["P E", "###"])
        g.move(1)
        g.move(1)
        self.assertIsNotNone(g.phase)
        handle_key(g, ord("p"))
        self.assertTrue(g.paused)
        self.assertFalse(g.update(5.0))
        self.assertIsNotNone(g.phase)
        self.assertFalse(handle_key(g, ord("l")))
        handle_key(g, ord("p"))
        self.assertFalse(g.paused)


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
        # nothing overlaps
        regions = [plan[k] for k in self.REGIONS if plan[k] is not None]
        for i, (ay, ax, ah, aw) in enumerate(regions):
            for by, bx, bh, bw in regions[i + 1:]:
                overlap = (ay < by + bh and by < ay + ah
                           and ax < bx + bw and bx < ax + aw)
                self.assertFalse(overlap, (cols, rows, plan))

    def composition(self, plan):
        """(top, left, bottom, right) of board + side column."""
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
            self.assertEqual(plan[key][3], 30, key)  # side column width
        self.assertIsNone(plan["hint"])
        self.fits(plan, 93, 28)
        self.centred(plan, 93, 28)
        # stats over controls over agents in one column, flush with the
        # board's top and bottom edges
        sy, sx, sh, sw = plan["stats"]
        cy, cx, ch, cw = plan["controls"]
        ay, ax, ah, aw = plan["agents"]
        by, bx, bh, bw = plan["board"]
        self.assertEqual(sx, cx)
        self.assertEqual(sx, ax)
        self.assertEqual(sx, bx + bw + 1)
        self.assertEqual((sy, sy + sh, cy + ch), (by, cy, ay))
        self.assertEqual(ay + ah, by + bh)
        self.assertGreaterEqual(ch, loderunner.controls_height(
            loderunner.CONTROLS, 30))

    def test_side_column_at_77x24(self):
        plan = layout_plan(77, 24)
        self.assertIn(plan["tier"], ("full", "side"))
        self.assertIsNotNone(plan["controls"])
        self.assertIsNotNone(plan["stats"])
        self.fits(plan, 77, 24)
        self.centred(plan, 77, 24)
        # controls get their full height first
        self.assertEqual(plan["controls"][2], loderunner.controls_height(
            loderunner.CONTROLS, plan["controls"][3]))

    def test_degrading_tiers(self):
        for cols in (74, 68, 60, 54):
            plan = layout_plan(cols, 24)
            self.assertEqual(plan["tier"], "board", cols)
            self.assertIsNone(plan["stats"])
            self.assertIsNone(plan["controls"])
            self.assertIsNotNone(plan["hint"], cols)
            self.assertEqual(plan["hint"][0], 23)
        plan = layout_plan(60, 19)  # no spare row: the hint joins the header
        self.assertIsNone(plan["hint"])
        self.assertTrue(plan["hint_in_header"])
        self.assertTrue(layout_plan(50, 24)["too_small"])
        self.assertTrue(layout_plan(77, 18)["too_small"])
        for cols, rows in ((93, 28), (93, 24), (80, 26), (77, 24), (75, 20),
                           (74, 24), (68, 24), (60, 20), (60, 19), (54, 19),
                           (120, 40), (93, 19)):
            plan = layout_plan(cols, rows)
            self.assertFalse(plan["too_small"], (cols, rows))
            self.fits(plan, cols, rows)
            self.centred(plan, cols, rows)
            if plan["controls"] is None:
                self.assertTrue(plan["hint"] is not None
                                or plan["hint_in_header"], (cols, rows))

    def test_key_hint_leads_with_help(self):
        self.assertTrue(loderunner.key_hint().startswith("? help"))


class ConstantsTest(unittest.TestCase):
    def test_controls_cover_every_key(self):
        keys = " ".join(k for k, _ in loderunner.CONTROLS)
        for token in ("?", "q", "p", "r", "z", "x", ",", ".", "arrows",
                      "wasd", "hjkl"):
            self.assertIn(token, keys)
        self.assertEqual(loderunner.CONTROLS[-1], ("q", "menu"))
        # spot check: keys the handler reacts to are documented
        for ch in "zx,.wasdhjkl":
            self.assertIn(ch, keys)

    def test_help_sections(self):
        heads = {h: b for h, b in loderunner.HELP}
        for need in ("Goal", "Controls", "Scoring", "Tips", "Digging",
                     "Escape ladder", "False bricks", "Legend"):
            self.assertIn(need, heads)
            self.assertTrue(heads[need].strip(), need)
        text = " ".join(h + " " + b for h, b in loderunner.HELP)
        self.assertTrue(all(ord(c) < 128 for c in text))
        for word in ("ladder", "bar", "fall", "refill", "head", "gold",
                     "trapdoor", "<$", "[]"):
            self.assertIn(word, text)
        # numbers in the help match the code
        self.assertIn(str(GOLD_SCORE), heads["Scoring"])
        self.assertIn(str(LEVEL_SCORE), heads["Scoring"])
        self.assertIn("%d seconds" % HOLE_TIME, heads["Digging"])


class HelpKeyTest(unittest.TestCase):
    def test_help_pauses_and_closing_resumes(self):
        g = Game(random.Random(1))
        h = loderunner.HelpOverlay(loderunner.TITLE, loderunner.HELP)
        consumed, pbh = loderunner.help_key(g, h, ord("?"), False)
        self.assertTrue(consumed and pbh and h.is_open and g.paused)
        # timers do not advance while help is open
        self.assertFalse(g.update(1.0))
        # scrolling keys keep it open, and do not move the runner
        x = g.player.x
        consumed, pbh = loderunner.help_key(g, h, loderunner.curses.KEY_DOWN, pbh)
        self.assertTrue(consumed and h.is_open and g.paused)
        # resize is passed through
        consumed, pbh = loderunner.help_key(
            g, h, loderunner.curses.KEY_RESIZE, pbh)
        self.assertFalse(consumed)
        self.assertTrue(h.is_open)
        # any other key closes it (and is swallowed) and resumes
        consumed, pbh = loderunner.help_key(g, h, ord("l"), pbh)
        self.assertTrue(consumed)
        self.assertFalse(h.is_open or g.paused or pbh)
        self.assertEqual(g.player.x, x)
        # other keys are not touched while help is closed
        self.assertEqual(loderunner.help_key(g, h, ord("l"), False),
                         (False, False))

    def test_help_keeps_an_existing_pause(self):
        for setup in ("paused", "game_over"):
            g = Game(random.Random(1))
            if setup == "paused":
                g.paused = True
            else:
                g.game_over = True
            h = loderunner.HelpOverlay(loderunner.TITLE, loderunner.HELP)
            consumed, pbh = loderunner.help_key(g, h, ord("?"), False)
            self.assertTrue(consumed and h.is_open, setup)
            consumed, pbh = loderunner.help_key(g, h, ord("x"), pbh)
            self.assertFalse(h.is_open)
            if setup == "paused":
                self.assertTrue(g.paused)  # still paused by the player
                self.assertFalse(pbh)


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
            raise loderunner.curses.error()
        child = FakeWin(h, w, self.root, self.y0 + y, self.x0 + x)
        self.root.children.append(child)
        return child

    def erase(self):
        self.writes = []

    def addstr(self, y, x, text, attr=0):
        if y < 0 or y >= self.rows or x < 0 or x + len(text) > self.cols:
            self.root.violations.append((y, x, text))
            raise loderunner.curses.error()
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
        overlay = loderunner.HelpOverlay(loderunner.TITLE, loderunner.HELP)
        if help_open:
            overlay.open()
        with mock.patch("curses.color_pair", return_value=0):
            lay = loderunner.Layout(scr)
            loderunner.draw(game, lay, self.session(attention), resume, 999,
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
        rv = Game(random.Random(1), level=3)
        rv.revealed = True
        rv.holes[(3, 3)] = 0.5
        rv.holes[(4, 3)] = 3.0
        rv.guards[0].gold = True
        rv.guards[1].trap = 1.0
        yield "revealed", rv
        for elapsed in (0.0, 0.15):
            dy = Game(random.Random(1))
            dy.phase = Phase("dying", DEATH_TIME)
            dy.phase.elapsed = elapsed
            yield "dying", dy
        lc = Game(random.Random(1), level=5)
        lc.phase = Phase("level_clear", CLEAR_TIME)
        yield "level_clear", lc
        go = Game(random.Random(1))
        go.game_over = True
        go.lives = 0
        yield "game_over", go
        pa = Game(random.Random(1), level=2)
        pa.paused = True
        yield "paused", pa

    def test_full_frame_in_bounds(self):
        for name, game in self.games():
            scr, lay = self.render(game)
            self.assertEqual(scr.violations, [], name)
            board = lay.board
            self.assertEqual((board.y0, board.x0), (5, 4))
            self.assertEqual(board.getmaxyx(), (GRID_H + 2, GRID_W * 2 + 2))
            self.assertIsNotNone(lay.agents)
            self.assertIsNotNone(lay.controls)
            for child in scr.children:
                self.assertLessEqual(child.x0 + child.cols, 93)
                self.assertLessEqual(child.y0 + child.rows, 28)
            cells = [t for y, x, t in board.writes
                     if 0 < y < board.rows - 1 and 0 < x < board.cols - 1]
            self.assertTrue(all(len(t) == 2 for t in cells
                                if t.strip() and not t.startswith(" ")), name)

    def test_player_and_overlays_drawn(self):
        g = Game(random.Random(1))
        scr, lay = self.render(g)
        p = g.player
        self.assertIn((1 + p.y, 1 + p.x * 2, "[]"), lay.board.writes)
        go = Game(random.Random(1))
        go.game_over = True
        scr, lay = self.render(go)
        texts = [t for _, _, t in lay.board.writes]
        self.assertIn(" GAME OVER ", texts)
        self.assertIn(" r restart | ? help | q menu ", texts)

    def test_banners_and_smaller_tiers(self):
        agent = AgentSnapshot("p1", "claude", "claude", "blocked")
        for kw in ({"attention": (agent, "working")}, {"resume": True}):
            scr, lay = self.render(Game(random.Random(1)), **kw)
            self.assertEqual(scr.violations, [])
        for cols, rows in ((77, 24), (68, 24), (60, 24), (54, 19), (40, 10)):
            scr, lay = self.render(Game(random.Random(1)), cols, rows)
            self.assertEqual(scr.violations, [], (cols, rows))
        scr, lay = self.render(Game(random.Random(1)), 60, 24)
        header = [t for y, _, t in scr.writes if y == 0]
        self.assertTrue(any("score" in t for t in header))
        hint = [t for y, _, t in scr.writes if y == 23]
        self.assertTrue(hint and hint[0].startswith("? help"), hint)
        scr, lay = self.render(Game(random.Random(1)), 54, 19)
        header = [t for y, _, t in scr.writes if y == 0]
        self.assertTrue(any("? help" in t for t in header))
        scr, lay = self.render(Game(random.Random(1)), 40, 10)
        self.assertTrue(any("pane too small" in t for _, _, t in scr.writes))

    def test_controls_box_and_stats_at_popup_size(self):
        scr, lay = self.render(Game(random.Random(1)))
        text = self.all_text(scr)
        for keys, action in loderunner.CONTROLS:
            self.assertIn(keys, text)
        self.assertIn("lode runner", text)
        self.assertIn("score 0", text)
        self.assertIn("agents", text)

    def test_help_overlay_renders_at_every_tier(self):
        for cols, rows in ((93, 28), (77, 24), (60, 24), (54, 19), (40, 10)):
            for name, game in self.games():
                scr, lay = self.render(game, cols, rows, help_open=True)
                self.assertEqual(scr.violations, [], (cols, rows, name))
            text = self.all_text(scr)
            if cols >= 54:
                self.assertIn("Goal", text, (cols, rows))
                self.assertIn("Lode Runner - help", text)
                # the controls stay on screen: box or hint row/header
                self.assertIn("? help" if lay.controls is None else " keys ",
                              text, (cols, rows))
                if not lay.plan["hint_in_header"]:
                    self.assertIn("wasd hjkl", text, (cols, rows))
            else:
                self.assertIn("Lode Runner - help", text)


if __name__ == "__main__":
    unittest.main()
