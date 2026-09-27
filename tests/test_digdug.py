"""Unit tests for the pure game logic, layout and rendering in digdug.py (no
terminal needed)."""

import json
import os
import random
import sys
import types
import unittest
from collections import deque
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import digdug  # noqa: E402
from common import AgentSnapshot  # noqa: E402
from digdug import (  # noqa: E402
    DIG_POINTS,
    DIRT,
    DOWN,
    EXIT,
    FIELD_H,
    FIELD_W,
    FIRE,
    FYGAR,
    GHOST,
    LEFT,
    LIVES,
    OPEN,
    POOKA,
    RIGHT,
    START,
    UP,
    WALK,
    WINDUP,
    Enemy,
    Game,
    Rock,
    generate_level,
    layout_plan,
)


def new_game(seed=1):
    return Game(random.Random(seed))


def blank_game(seed=1):
    """A game on solid dirt under the sky, with nothing in it but Dig Dug at
    START (in an open cell); tests then dig and place exactly what they
    need."""
    g = new_game(seed)
    g.grid = [[OPEN] * FIELD_W] + [[DIRT] * FIELD_W for _ in range(FIELD_H - 1)]
    g.grid[START[1]][START[0]] = OPEN
    g.enemies = []
    g.rocks = []
    g.player = START
    g.facing = DOWN
    return g


def dig(g, *cells):
    for x, y in cells:
        g.grid[y][x] = OPEN


def row(y, x0, x1):
    return [(x, y) for x in range(x0, x1 + 1)]


def col(x, y0, y1):
    return [(x, y) for y in range(y0, y1 + 1)]


def enemy(g, x, y, kind=POOKA, eid=None):
    """A monster at (x, y) that will not move on its own (large negative
    step accumulator) unless a test lets it."""
    e = Enemy(len(g.enemies) if eid is None else eid, kind, x, y)
    e.acc = -1000.0
    g.grid[y][x] = OPEN
    g.enemies.append(e)
    return e


def anchor(g):
    """A second, motionless monster in the far corner, so the monster under
    test is not the last one (which would run for the exit)."""
    e = enemy(g, FIELD_W - 1, FIELD_H - 1)
    e.acc = -1e9
    return e


def rock(g, x, y, state=None, timer=0.0, crushed=0):
    """A rock at (x, y); rocks sit in an open cell of their own."""
    g.grid[y][x] = OPEN
    r = Rock(x, y, state or digdug.REST, timer, crushed)
    g.rocks.append(r)
    return r


def settle(g, seconds, dt=0.05):
    for _ in range(int(round(seconds / dt))):
        g.update(dt)


def pump_ready(g):
    g.pump_cd = 0.0
    return g.pump()


class LevelGenTest(unittest.TestCase):
    def test_invariants_across_seeds_and_rounds(self):
        for seed in range(25):
            for level in (1, 2, 5, 9):
                rng = random.Random(seed)
                grid, spawns, rocks = generate_level(rng, level)
                self.assertEqual(grid[0], [OPEN] * FIELD_W)       # sky untouched
                self.assertEqual(grid[START[1]][START[0]], OPEN)
                for y in range(1, START[1] + 1):                   # the entry shaft
                    self.assertEqual(grid[y][START[0]], OPEN)
                self.assertEqual(len(spawns), digdug.enemy_count(level))
                self.assertEqual(sum(k == FYGAR for k, _, _ in spawns),
                                 digdug.fygar_count(level))
                for kind, x, y in spawns:
                    self.assertEqual(grid[y][x], OPEN)
                    self.assertGreaterEqual(abs(x - START[0]) + abs(y - START[1]), 5)
                    self.assertNotIn((x, y), rocks)
                self.assertEqual(len(rocks), len(set(rocks)))
                for x, y in rocks:
                    self.assertNotEqual(x, START[0])               # never on the shaft
                    self.assertEqual(grid[y + 1][x], DIRT)         # dirt below it
                    for dx, dy in digdug.DELTAS.values():          # no tunnel touching
                        nx, ny = x + dx, y + dy
                        if digdug.in_bounds(nx, ny):
                            self.assertEqual(grid[ny][nx], DIRT, (seed, level, x, y))

    def test_no_rock_moves_at_round_start(self):
        for seed in range(20):
            g = new_game(seed)
            settle(g, 0.5)
            self.assertTrue(all(r.state == digdug.REST for r in g.rocks), seed)

    def test_every_monster_reachable_by_digging(self):
        # Dig Dug can dig anywhere a rock is not, so each den must connect
        # to START around the rocks.
        for seed in range(20):
            g = new_game(seed)
            rocks = {r.pos for r in g.rocks}
            seen, todo = {START}, deque([START])
            while todo:
                x, y = todo.popleft()
                for dx, dy in digdug.DELTAS.values():
                    n = (x + dx, y + dy)
                    if digdug.in_bounds(*n) and n not in rocks and n not in seen:
                        seen.add(n)
                        todo.append(n)
            for e in g.enemies:
                self.assertIn(e.pos, seen)

    def test_deterministic_for_seed(self):
        self.assertEqual(new_game(42).to_save(), new_game(42).to_save())
        self.assertNotEqual(new_game(42).to_save()["grid"], new_game(43).to_save()["grid"])

    def test_rounds_get_harder(self):
        self.assertLess(digdug.enemy_interval(5), digdug.enemy_interval(1))
        self.assertGreater(digdug.enemy_count(9), digdug.enemy_count(1))
        self.assertGreater(digdug.fygar_count(7), digdug.fygar_count(1))
        self.assertLessEqual(digdug.enemy_count(99), 8)

    def test_layers(self):
        self.assertEqual([digdug.layer_of(y) for y in (0, 1, 4, 5, 8, 9, 12, 13, 16)],
                         [1, 1, 1, 2, 2, 3, 3, 4, 4])


class MovementTest(unittest.TestCase):
    def test_digging_moves_scores_and_opens(self):
        g = blank_game()
        self.assertTrue(g.move(RIGHT))
        self.assertEqual((g.player, g.facing), ((8, 8), RIGHT))
        self.assertEqual(g.grid[8][8], OPEN)
        self.assertEqual(g.score, DIG_POINTS)

    def test_one_step_per_press_with_queue(self):
        g = blank_game()
        dig(g, *row(8, 8, 12))
        self.assertTrue(g.move(RIGHT))
        self.assertFalse(g.move(RIGHT))        # still mid-step: queued
        self.assertFalse(g.move(RIGHT))        # queue is one deep
        self.assertEqual(g.player, (8, 8))
        settle(g, 0.5)
        self.assertEqual(g.player, (9, 8))     # exactly one queued step taken
        self.assertEqual(g.score, 0)           # open tunnel: no dig points

    def test_digging_is_slower(self):
        self.assertGreater(digdug.DIG_TIME, digdug.MOVE_TIME)
        g = blank_game()
        g.move(LEFT)
        self.assertAlmostEqual(g.move_cd, digdug.DIG_TIME)

    def test_rock_and_edge_turn_without_moving(self):
        g = blank_game()
        rock(g, 7, 7)
        self.assertFalse(g.move(UP))
        self.assertEqual((g.player, g.facing), (START, UP))
        g.player = (0, 0)
        self.assertFalse(g.move(LEFT))
        self.assertEqual((g.player, g.facing), ((0, 0), LEFT))

    def test_sky_is_walkable(self):
        g = blank_game()
        g.player = (3, 0)
        self.assertTrue(g.move(RIGHT))
        self.assertEqual(g.score, 0)
        self.assertTrue(g.move_cd > 0)

    def test_no_move_while_paused(self):
        g = blank_game()
        g.paused = True
        self.assertFalse(g.move(RIGHT))
        self.assertEqual(g.player, START)
        self.assertIsNone(g.queued)


class PumpTest(unittest.TestCase):
    def test_hit_inflate_pop_scores_by_layer(self):
        g = blank_game()
        g.facing = RIGHT
        dig(g, *row(8, 8, 9))
        e = enemy(g, 10, 8)
        self.assertEqual(pump_ready(g), "hit")
        self.assertEqual((e.inflate, g.pump_target), (1, e.id))
        self.assertEqual(pump_ready(g), "inflate")
        self.assertEqual(pump_ready(g), "inflate")
        self.assertEqual(e.inflate, 3)
        self.assertEqual(pump_ready(g), "pop")
        self.assertNotIn(e, g.enemies)
        self.assertEqual(g.score, digdug.POP_POINTS[1])   # row 8 is stratum 2
        self.assertIsNone(g.pump_target)

    def test_pop_points_table(self):
        self.assertEqual(digdug.pop_points(POOKA, 2, True), 200)
        self.assertEqual(digdug.pop_points(POOKA, 16, False), 500)
        self.assertEqual(digdug.pop_points(FYGAR, 16, True), 1000)   # side pump
        self.assertEqual(digdug.pop_points(FYGAR, 16, False), 500)
        self.assertEqual(digdug.pop_points(FYGAR, 0, False), 200)    # sky = top

    def test_fygar_double_when_pumped_sideways(self):
        g = blank_game()
        g.facing = LEFT
        dig(g, *row(8, 5, 6))
        f = enemy(g, 5, 8, FYGAR)
        for _ in range(4):
            pump_ready(g)
        self.assertNotIn(f, g.enemies)
        self.assertEqual(g.score, 2 * digdug.POP_POINTS[1])
        g2 = blank_game()
        g2.facing = DOWN
        dig(g2, *col(7, 9, 12))
        f2 = enemy(g2, 7, 10, FYGAR)
        for _ in range(4):
            pump_ready(g2)
        self.assertNotIn(f2, g2.enemies)
        self.assertEqual(g2.score, digdug.POP_POINTS[2])  # row 10: stratum 3

    def test_harpoon_blocked_by_dirt_rock_and_range(self):
        g = blank_game()
        g.facing = RIGHT
        enemy(g, 9, 8)                              # dirt at (8, 8) between
        self.assertEqual(pump_ready(g), "miss")
        g = blank_game()
        g.facing = RIGHT
        dig(g, *row(8, 8, 11))
        enemy(g, 11, 8)                             # 4 cells away: out of range
        self.assertEqual(pump_ready(g), "miss")
        self.assertEqual(len(g.harpoon_flash[0]), digdug.PUMP_RANGE)
        g = blank_game()
        g.facing = RIGHT
        dig(g, *row(8, 8, 10))
        rock(g, 9, 8)
        enemy(g, 10, 8)
        self.assertEqual(pump_ready(g), "miss")

    def test_ghost_cannot_be_harpooned(self):
        g = blank_game()
        g.facing = RIGHT
        dig(g, *row(8, 8, 9))
        e = enemy(g, 9, 8)
        e.state = GHOST
        self.assertEqual(pump_ready(g), "miss")

    def test_pump_cooldown(self):
        g = blank_game()
        g.facing = RIGHT
        dig(g, (8, 8))
        e = enemy(g, 9, 8)
        g.pump()
        self.assertEqual(g.pump(), "none")          # too soon
        self.assertEqual(e.inflate, 1)

    def test_deflates_and_frees(self):
        g = blank_game()
        g.facing = RIGHT
        dig(g, (8, 8))
        e = enemy(g, 9, 8)
        pump_ready(g)
        pump_ready(g)
        self.assertEqual(e.inflate, 2)
        settle(g, digdug.DEFLATE_TIME + 0.05)
        self.assertEqual(e.inflate, 1)
        settle(g, digdug.DEFLATE_TIME)
        self.assertEqual(e.inflate, 0)
        self.assertIsNone(g.pump_target)

    def test_inflated_monster_is_harmless_and_still(self):
        g = blank_game()
        g.facing = RIGHT
        dig(g, *row(8, 8, 12))
        e = enemy(g, 8, 8)
        pump_ready(g)
        e.acc = 0.0
        settle(g, 0.3)
        self.assertEqual(e.pos, (8, 8))
        g.pump_target = None
        g.move(RIGHT)                   # walk into the inflated monster
        self.assertIsNone(g.phase)

    def test_pump_drops_a_queued_step(self):
        g = blank_game()
        dig(g, *row(8, 8, 9))
        g.move(RIGHT)
        g.move(RIGHT)                   # queued behind the step cooldown
        e = enemy(g, 9, 8)
        self.assertEqual(pump_ready(g), "hit")
        settle(g, 0.2)
        self.assertEqual(g.pump_target, e.id)
        self.assertEqual(g.player, (8, 8))

    def test_deflating_under_player_kills(self):
        g = blank_game()
        g.facing = RIGHT
        dig(g, (8, 8))
        anchor(g)
        enemy(g, 8, 8)
        pump_ready(g)
        g.pump_target = None
        g.move(RIGHT)                   # onto the inflated monster: safe
        self.assertIsNone(g.phase)
        settle(g, digdug.DEFLATE_TIME + 0.05)
        self.assertEqual(g.phase.kind, "dying")

    def test_moving_or_turning_lets_go(self):
        g = blank_game()
        g.facing = RIGHT
        dig(g, (8, 8))
        e = enemy(g, 9, 8)
        pump_ready(g)
        rock(g, 7, 7)
        g.move(UP)                      # a turn only, into a rock
        self.assertIsNone(g.pump_target)
        self.assertEqual(e.inflate, 1)
        g.facing = RIGHT
        self.assertEqual(pump_ready(g), "hit")      # re-harpoon, stage 1 again

    def test_pump_cancels_fire_windup(self):
        g = blank_game()
        g.facing = RIGHT
        dig(g, (8, 8))
        f = enemy(g, 9, 8, FYGAR)
        f.state, f.timer, f.hdir = WINDUP, 0.5, LEFT
        pump_ready(g)
        self.assertEqual(f.state, WALK)
        self.assertEqual(f.inflate, 1)


class MonsterTest(unittest.TestCase):
    def test_touching_kills(self):
        g = blank_game()
        dig(g, (8, 8))
        enemy(g, 8, 8)
        g.move(RIGHT)
        self.assertEqual(g.phase.kind, "dying")
        self.assertEqual(g.lives, LIVES - 1)

    def test_walks_tunnels_toward_player(self):
        g = blank_game()
        anchor(g)
        dig(g, *row(8, 1, 6))
        e = enemy(g, 1, 8)
        e.acc = 0.0
        e.walk_time = -100.0           # no ghosting in this test
        settle(g, digdug.enemy_interval(1) * 3 + 0.01, dt=0.01)
        self.assertGreater(e.x, 1)
        self.assertEqual(e.y, 8)
        self.assertEqual(g.grid[e.y][e.x], OPEN)

    def test_ghost_crosses_dirt_and_resolidifies(self):
        g = blank_game()
        anchor(g)
        e = enemy(g, 2, 8)
        e.acc = 0.0
        e.state = GHOST
        g.update(digdug.enemy_interval(1) * 1.5 + 0.001)
        self.assertEqual(e.pos, (3, 8))        # into the dirt
        self.assertEqual(g.grid[8][3], DIRT)   # eyes do not dig
        for _ in range(200):
            g.update(0.05)
            if g.phase is not None or e.state == WALK:
                break
        self.assertTrue(g.phase is not None or g.grid[e.y][e.x] == OPEN)

    def test_ghosting_starts_after_walking_a_while(self):
        g = blank_game()
        anchor(g)
        dig(g, *row(12, 1, 3))
        e = enemy(g, 2, 12)
        e.acc = 0.0
        seen = set()
        for _ in range(400):
            g.update(0.05)
            seen.add(e.state)
            if g.phase:
                break
        self.assertIn(GHOST, seen)

    def test_fygar_windup_then_flame_through_dirt(self):
        g = blank_game()
        f = enemy(g, 4, 8, FYGAR)
        dig(g, (3, 8))
        f.state, f.timer, f.hdir = WINDUP, digdug.FIRE_WINDUP, RIGHT
        self.assertEqual(g.flame_cells(f), [(5, 8), (6, 8), (7, 8)])
        settle(g, digdug.FIRE_WINDUP - 0.1)
        self.assertEqual(f.state, WINDUP)       # the telegraph, no harm yet
        self.assertIsNone(g.phase)
        settle(g, 0.2)
        self.assertEqual(f.state, FIRE)
        self.assertEqual(g.phase.kind, "dying")  # Dig Dug at (7, 8) is 3 cells away

    def test_flame_stopped_by_rock_and_short(self):
        g = blank_game()
        f = enemy(g, 3, 8, FYGAR)
        f.hdir = RIGHT
        rock(g, 5, 8)
        self.assertEqual(g.flame_cells(f), [(4, 8)])
        f.hdir = LEFT
        self.assertEqual(g.flame_cells(f), [(2, 8), (1, 8), (0, 8)])
        f.x = 1
        self.assertEqual(g.flame_cells(f), [(0, 8)])
        self.assertEqual(g.flame_cells(Enemy(9, POOKA, 3, 3)), [])

    def test_fygar_starts_fire_when_in_row(self):
        g = blank_game()
        anchor(g)
        dig(g, *row(8, 2, 6))
        f = enemy(g, 3, 8, FYGAR)
        f.acc = 0.0
        with mock.patch.object(digdug, "FIRE_CHANCE", 1.0):
            g.update(digdug.enemy_interval(1) + 0.001)
        self.assertEqual(f.state, WINDUP)       # 4 cells away, same row
        self.assertEqual(f.hdir, RIGHT)
        self.assertEqual(f.pos, (3, 8))         # it stops to breathe

    def test_fire_ends_and_cools_down(self):
        g = blank_game()
        g.player = (7, 0)
        f = enemy(g, 3, 8, FYGAR)
        f.state, f.timer = FIRE, 0.1
        settle(g, 0.15)
        self.assertEqual(f.state, WALK)
        self.assertGreater(f.fire_cd, 0)


class RockTest(unittest.TestCase):
    def setup_rock(self):
        """Rock at (7, 4) right above a shaft (7, 5..8) Dig Dug dug up."""
        g = blank_game()
        dig(g, *col(7, 5, 8))
        rock(g, 7, 4)
        return g

    def test_waits_while_player_beneath(self):
        g = self.setup_rock()
        g.player = (7, 5)
        settle(g, 2.0)
        self.assertEqual(g.rocks[0].state, digdug.REST)
        g.move(DOWN)
        g.update(0.01)
        self.assertEqual(g.rocks[0].state, digdug.WOBBLE)

    def test_wobble_fall_break_and_count(self):
        g = self.setup_rock()
        g.player = (7, 8)
        dig(g, (8, 8))
        g.update(0.01)
        self.assertEqual(g.rocks[0].state, digdug.WOBBLE)
        settle(g, digdug.ROCK_WOBBLE - 0.1)
        self.assertEqual(g.rocks[0].pos, (7, 4))       # still wobbling in place
        g.move(RIGHT)                                  # out of the way
        settle(g, 1.0)
        self.assertEqual(g.rocks, [])                  # landed on (7, 9) dirt and broke
        self.assertEqual(g.rocks_dropped, 1)
        self.assertEqual(g.grid[4][7], OPEN)           # leaves a hole
        self.assertIsNone(g.phase)

    def test_crushes_player(self):
        g = self.setup_rock()
        g.player = (7, 8)
        settle(g, 2.0)
        self.assertEqual(g.phase.kind, "dying")

    def test_multi_crush_bonus(self):
        g = self.setup_rock()
        g.player = (0, 0)
        a = enemy(g, 7, 6)
        b = enemy(g, 7, 7)
        c = enemy(g, 7, 8)
        enemy(g, 0, 16)                                # a fourth, far away
        settle(g, 2.0)
        for e in (a, b, c):
            self.assertNotIn(e, g.enemies)
        self.assertEqual(g.score, digdug.crush_points(3))
        self.assertEqual(g.score, 4000)
        self.assertIn("CRUSH x3", g.message[0])

    def test_crush_table(self):
        self.assertEqual([digdug.crush_points(n) for n in range(1, 9)],
                         [1000, 2500, 4000, 6000, 8000, 10000, 12000, 15000])
        self.assertEqual(digdug.crush_points(20), 15000)

    def test_vegetable_after_two_rocks(self):
        g = blank_game()
        g.player = (0, 0)
        enemy(g, 0, 16)
        dig(g, (2, 5), (4, 5))
        rock(g, 2, 4)
        rock(g, 4, 4, digdug.WOBBLE, 5.0)
        g.update(0.01)
        settle(g, 1.5)
        self.assertEqual(g.rocks_dropped, 1)
        self.assertIsNone(g.veg)
        settle(g, 5.0)
        self.assertEqual(g.rocks_dropped, 2)
        self.assertEqual(g.veg[:2], list(START))
        before = g.score
        g.player = (START[0], START[1] - 1)
        g.move(DOWN)
        self.assertIsNone(g.veg)
        self.assertEqual(g.score - before, digdug.veg_points(1))
        self.assertEqual(digdug.veg_points(1), 400)
        self.assertEqual(digdug.veg_points(99), 8000)

    def test_vegetable_times_out(self):
        g = blank_game()
        g.player = (0, 0)
        enemy(g, 0, 16)
        g.veg = [START[0], START[1], 1.0]
        settle(g, 1.1)
        self.assertIsNone(g.veg)


class FleeTest(unittest.TestCase):
    def test_last_monster_flees_and_escapes(self):
        g = blank_game()
        g.player = (14, 16)
        dig(g, *col(3, 1, 5))
        e = enemy(g, 3, 5)
        e.acc = 0.0
        g.update(0.01)
        self.assertTrue(e.flee)
        for _ in range(400):
            g.update(0.05)
            if not g.enemies:
                break
        self.assertEqual(g.enemies, [])
        self.assertTrue(g.escaped)
        self.assertEqual(g.phase.kind, "level_clear")
        self.assertEqual(g.score, 0)                   # no points for it

    def test_sealed_flee_ghosts_through_dirt(self):
        g = blank_game()
        g.player = (14, 16)
        e = enemy(g, 5, 12)
        e.acc = 0.0
        g.update(0.01)
        g.update(digdug.enemy_interval(1))
        self.assertEqual(e.state, GHOST)
        for _ in range(600):
            g.update(0.05)
            if not g.enemies:
                break
        self.assertEqual(g.enemies, [])

    def test_two_left_do_not_flee(self):
        g = blank_game()
        enemy(g, 0, 16)
        enemy(g, 14, 16)
        g.update(0.01)
        self.assertFalse(any(e.flee for e in g.enemies))


class LevelTest(unittest.TestCase):
    def test_clear_advances_round(self):
        g = blank_game()
        g.facing = RIGHT
        dig(g, (8, 8))
        enemy(g, 9, 8)
        for _ in range(4):
            pump_ready(g)
        self.assertEqual(g.phase.kind, "level_clear")
        settle(g, digdug.LEVEL_CLEAR_TIME + 0.1)
        self.assertEqual(g.level, 2)
        self.assertEqual(len(g.enemies), digdug.enemy_count(2))
        self.assertEqual(g.player, START)
        self.assertEqual(g.rocks_dropped, 0)

    def test_extra_life(self):
        g = blank_game()
        g.add_score(9990)
        self.assertEqual(g.lives, LIVES)
        g.add_score(10)
        self.assertEqual(g.lives, LIVES + 1)
        g.add_score(40000)
        self.assertEqual(g.lives, LIVES + 2)
        self.assertEqual(g.next_extra, 90000)

    def test_bot_clears_rounds(self):
        """Headless play: a simple bot (dig toward a monster, pump when lined
        up) clears round 1 and round 2 without deadlock."""
        g = new_game(3)
        g.lives = 99
        for _ in range(5000):
            act = bot_action(g)
            if act is not None:
                g.pump() if act[0] == "pump" else g.move(act[1])
            g.update(0.05)
            if g.level >= 3 or g.game_over:
                break
        self.assertGreaterEqual(g.level, 3)
        self.assertFalse(g.game_over)


def bot_action(g):
    if g.pump_target is not None:
        return ("pump",)
    px, py = g.player
    for d in digdug.DIRS:
        dx, dy = digdug.DELTAS[d]
        x, y = px, py
        for i in range(1, digdug.PUMP_RANGE + 1):
            x, y = x + dx, y + dy
            if not g.is_open(x, y):
                break
            if any(e.pos == (x, y) and e.state != GHOST for e in g.enemies):
                if g.facing == d:
                    return ("pump",)
                if i >= 2:
                    return ("move", d)
                break
    goals = {}
    for e in g.enemies:
        for d in digdug.DIRS:
            dx, dy = digdug.DELTAS[d]
            t = (e.x - 2 * dx, e.y - 2 * dy)
            if digdug.in_bounds(*t):
                goals[t] = d
    if g.player in goals:
        return ("move", goals[g.player])
    danger = {e.pos for e in g.enemies if e.inflate == 0}
    prev = {g.player: None}
    todo = deque([g.player])
    found = None
    while todo:
        c = todo.popleft()
        if c in goals:
            found = c
            break
        for d in digdug.DIRS:
            dx, dy = digdug.DELTAS[d]
            n = (c[0] + dx, c[1] + dy)
            if n in prev or not digdug.in_bounds(*n) or g.rock_at(n) or n in danger:
                continue
            prev[n] = (c, d)
            todo.append(n)
    if found is None:
        return None
    c = found
    while prev[c][0] != g.player:
        c = prev[c][0]
    return ("move", prev[c][1])


class DeathTest(unittest.TestCase):
    def test_death_phase_then_respawn_sends_monsters_home(self):
        g = blank_game()
        dig(g, (8, 8), (9, 8))
        e = enemy(g, 9, 8)
        e.x = 8
        g.move(RIGHT)
        self.assertEqual(g.phase.kind, "dying")
        self.assertFalse(g.move(LEFT))           # no input while dying
        settle(g, digdug.DEATH_TIME + 0.1)
        self.assertIsNone(g.phase)
        self.assertEqual(g.player, START)
        self.assertEqual(e.pos, (9, 8))
        self.assertLess(e.acc, 0)                # holds still a moment

    def test_lives_and_game_over(self):
        g = blank_game()
        g.lives = 1
        dig(g, (8, 8))
        enemy(g, 8, 8)
        g.move(RIGHT)
        g.resolve_phases()
        self.assertTrue(g.game_over)
        self.assertEqual(g.lives, 0)
        self.assertFalse(g.update(0.1))
        self.assertFalse(g.move(LEFT))


class DisplayKeyTest(unittest.TestCase):
    def test_changes_with_second_and_animation(self):
        g = blank_game()
        enemy(g, 0, 16)
        k0 = digdug.display_key(g, 0.0)
        g.update(0.5)
        self.assertEqual(digdug.display_key(g, 0.0), k0)
        g.update(0.6)
        self.assertNotEqual(digdug.display_key(g, 0.0), k0)
        self.assertEqual(digdug.display_key(g, 0.0), digdug.display_key(g, 0.13))
        g.rocks = [Rock(3, 3, digdug.WOBBLE, 1.0)]
        self.assertNotEqual(digdug.display_key(g, 0.0), digdug.display_key(g, 0.13))


class SaveTest(unittest.TestCase):
    def advanced(self):
        g = new_game(5)
        for i in range(60):
            act = bot_action(g)
            if act is not None and i % 3 == 0:
                g.pump() if act[0] == "pump" else g.move(act[1])
            g.update(0.1)
        g.resolve_phases()
        g.score += 1234
        return g

    def test_round_trip_including_rng(self):
        g = self.advanced()
        data = json.loads(json.dumps(g.to_save()))
        r = Game.from_save(data)
        self.assertIsNotNone(r)
        self.assertEqual(r.to_save(), g.to_save())
        # Same RNG state and timers: play continues identically.
        for i in range(200):
            for game in (g, r):
                act = bot_action(game)
                if act is not None and i % 4 == 0:
                    game.pump() if act[0] == "pump" else game.move(act[1])
                game.update(0.05)
        self.assertEqual(r.to_save(), g.to_save())
        self.assertEqual(g.rng.random(), r.rng.random())

    def test_round_trip_mid_action(self):
        g = blank_game()
        dig(g, *col(7, 5, 7), (8, 8))
        rock(g, 7, 4, digdug.FALL, 0.05, 1)
        g.facing = RIGHT
        e = enemy(g, 9, 8)
        dig(g, (9, 8))
        f = enemy(g, 3, 12, FYGAR)
        f.state, f.timer = WINDUP, 0.3
        e2 = enemy(g, 12, 3)
        e2.state = GHOST
        e2.hx, e2.hy = 9, 8
        g.grid[3][12] = DIRT
        pump_ready(g)
        g.veg = [7, 8, 3.0]
        g.queued = LEFT
        data = json.loads(json.dumps(g.to_save()))
        r = Game.from_save(data)
        self.assertIsNotNone(r)
        self.assertEqual(r.to_save(), data)
        self.assertEqual(r.pump_target, e.id)

    def test_corrupt_or_incompatible_is_none(self):
        good = self.advanced().to_save()
        self.assertIsNotNone(Game.from_save(good))
        self.assertIsNone(Game.from_save(None))
        self.assertIsNone(Game.from_save({}))
        self.assertIsNone(Game.from_save("junk"))
        self.assertIsNone(Game.from_save(dict(good, version=digdug.SAVE_VERSION + 1)))
        self.assertIsNone(Game.from_save(dict(good, rng=[3, [1, 2], None])))
        self.assertIsNone(Game.from_save(dict(good, grid=good["grid"][:-1])))
        self.assertIsNone(Game.from_save(dict(good, grid=["?" * FIELD_W] * FIELD_H)))
        self.assertIsNone(Game.from_save(dict(good, grid=[DIRT * FIELD_W] * FIELD_H)))
        self.assertIsNone(Game.from_save(dict(good, player=[FIELD_W, 0])))
        self.assertIsNone(Game.from_save(dict(good, facing="sideways")))
        self.assertIsNone(Game.from_save(dict(good, enemies=[[0, 0]])))
        self.assertIsNone(Game.from_save(dict(good, enemies=[])))
        self.assertIsNone(Game.from_save(dict(good, pump_target=999)))
        self.assertIsNone(Game.from_save(dict(good, rocks=[[0, 0, "rest", 0, 0]])))
        bad_enemy = list(good["enemies"][0])
        bad_enemy[1] = "dragon"
        self.assertIsNone(Game.from_save(dict(good, enemies=[bad_enemy])))
        bad_enemy = list(good["enemies"][0])
        bad_enemy[10] = 7
        self.assertIsNone(Game.from_save(dict(good, enemies=[bad_enemy])))
        dirt = next([x, y] for y, r in enumerate(good["grid"])
                    for x, c in enumerate(r) if c == DIRT)
        self.assertIsNone(Game.from_save(dict(good, player=dirt)))
        bad = dict(good)
        del bad["lives"]
        self.assertIsNone(Game.from_save(bad))

    def test_phase_resolved_before_save(self):
        g = blank_game()
        dig(g, (8, 8))
        enemy(g, 8, 8)
        enemy(g, 0, 16)
        g.move(RIGHT)
        g.resolve_phases()
        r = Game.from_save(json.loads(json.dumps(g.to_save())))
        self.assertIsNotNone(r)
        self.assertEqual(r.lives, LIVES - 1)
        self.assertEqual(r.player, START)


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
        self.assertEqual(ch, digdug.controls_height(digdug.CONTROLS, 30))
        self.assertGreaterEqual(ah, digdug.AGENTS_MIN_H)
        self.assertIsNone(plan["hint"])

    def test_degrading_tiers(self):
        plan = layout_plan(93, 20)                # no agents
        self.assertEqual(plan["tier"], "stats")
        self.assertIsNone(plan["agents"])
        self.assert_fits(plan, 93, 20)
        self.assert_centred(plan, 93, 20)
        plan = layout_plan(56, 24)                # narrower side column
        self.assertEqual(plan["tier"], "full")
        self.assertEqual(plan["stats"][3], 23)
        self.assert_centred(plan, 56, 24)
        plan = layout_plan(45, 24)                # no side column: key hint
        self.assertEqual(plan["tier"], "field")
        self.assertIsNone(plan["stats"])
        self.assertEqual(plan["hint"], (23, 0, 1, 45))
        self.assert_fits(plan, 45, 24)
        self.assert_centred(plan, 45, 24)
        self.assertIsNone(layout_plan(45, 20)["hint"])  # header carries ? help
        self.assertTrue(layout_plan(31, 24)["too_small"])
        self.assertTrue(layout_plan(93, 19)["too_small"])
        self.assertFalse(layout_plan(32, 20)["too_small"])
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
            text = digdug.hint_text(w)
            self.assertIn("? help", text)
            self.assertLess(len(text), w)


class ConstantsTest(unittest.TestCase):
    def test_controls_cover_keys(self):
        keys = " ".join(k for k, _ in digdug.CONTROLS)
        for k in ("?", "q", "p", "r", "arrows", "space", "enter",
                  "w", "a", "s", "d", "h", "j", "k", "l"):
            self.assertIn(k, keys)
        self.assertEqual(digdug.CONTROLS[-1], ("q", "menu"))
        for k, _ in digdug.CONTROLS:
            self.assertLessEqual(len(k), 9)   # never truncated in the box

    def test_help_sections(self):
        help = dict(digdug.HELP)
        for want in ("Goal", "Controls", "Scoring", "Tips", "Legend", "The pump",
                     "Monsters", "Fygar's fire", "Rocks", "Bonus vegetable",
                     "The last monster"):
            self.assertIn(want, help)
        for head, body in digdug.HELP:
            self.assertTrue(body.strip(), head)
            body.encode("ascii")
        glyphs = (list(digdug.DIRT_GLYPHS) + [
            digdug.ROCK_GLYPH, digdug.WOBBLE_GLYPH, digdug.POOKA_GLYPH,
            digdug.FLAME_GLYPH, digdug.EYES_GLYPH, digdug.VEG_GLYPH,
            digdug.SPLAT_GLYPH, digdug.DEAD_GLYPH]
            + list(digdug.FYGAR_GLYPH.values()) + list(digdug.WINDUP_GLYPH.values())
            + list(digdug.INFLATE_GLYPHS) + list(digdug.HARPOON_GLYPH.values())
            + list(digdug.PLAYER_GLYPH.values()))
        for glyph in glyphs:
            self.assertEqual(len(glyph), 2, glyph)
            glyph.encode("ascii")
            self.assertIn(glyph, help["Legend"])
        # Strata, entities and pickups never share a glyph.
        unique = (list(digdug.DIRT_GLYPHS) + [
            digdug.ROCK_GLYPH, digdug.POOKA_GLYPH, digdug.FLAME_GLYPH,
            digdug.EYES_GLYPH, digdug.VEG_GLYPH, digdug.SPLAT_GLYPH]
            + list(digdug.INFLATE_GLYPHS))
        self.assertEqual(len(unique), len(set(unique)))


class HelpKeyTest(unittest.TestCase):
    def setUp(self):
        self.game = new_game()
        self.help = digdug.HelpOverlay(digdug.TITLE, digdug.HELP)
        self.st = digdug.LoopState()
        self.session = types.SimpleNamespace(attention=None, dismiss=lambda: None)

    def key(self, ch):
        if isinstance(ch, str):
            ch = ord(ch)
        return digdug.dispatch_key(ch, self.game, self.help, self.st, self.session)

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
        st = self.st = digdug.LoopState(resume=True)
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
            raise digdug.curses.error()
        child = FakeWin(h, w, self.root, self.y0 + y, self.x0 + x)
        self.root.children.append(child)
        return child

    def erase(self):
        self.writes = []

    def addstr(self, y, x, text, attr=0):
        if y < 0 or y >= self.rows or x < 0 or x + len(text) > self.cols:
            self.root.violations.append((y, x, text))
            raise digdug.curses.error()
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

    def render(self, game, cols=93, rows=28, attention=None, resume=False, help=False):
        scr = FakeWin(rows, cols)
        overlay = digdug.HelpOverlay(digdug.TITLE, digdug.HELP)
        if help:
            overlay.open()
        with mock.patch("curses.color_pair", return_value=0):
            lay = digdug.Layout(scr)
            digdug.draw(game, lay, self.session(attention), resume, 1234, overlay)
        return scr, lay

    def games(self):
        yield "fresh", new_game()
        s = blank_game()
        s.facing = RIGHT
        dig(s, *row(8, 8, 10))
        enemy(s, 10, 8)
        pump_ready(s)
        f = enemy(s, 2, 12, FYGAR)
        f.state = FIRE
        gh = enemy(s, 12, 14)
        gh.state = GHOST
        rock(s, 3, 3, digdug.WOBBLE, 0.5)
        rock(s, 0, 16)
        s.splats.append([5, 5, 0.3])
        s.veg = [7, 8, 3.0]
        s.message = ["CRUSH x2 +2500", 1.0]
        s.harpoon_flash = [[(1, 1)], 0.1]
        yield "effects", s
        d = blank_game()
        dig(d, (8, 8))
        enemy(d, 8, 8)
        d.move(RIGHT)
        yield "dying", d
        lc = new_game()
        lc.phase = digdug.Phase("level_clear", 1.0)
        lc.escaped = True
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
            for size in ((93, 28), (93, 20), (56, 24), (45, 24), (45, 20), (32, 20),
                         (10, 3)):
                for att, res in ((None, False), (attention, False), (None, True)):
                    scr, lay = self.render(game, *size, attention=att, resume=res)
                    self.assertEqual(scr.violations, [], (name, size))

    def test_field_frame_hugs_the_cells(self):
        for size in ((93, 28), (93, 20), (45, 24)):
            scr, lay = self.render(new_game(), *size)
            fy, fx, fh, fw = lay.plan["field"]
            writes = lay.field.writes
            self.assertTrue(any(x == fw - 1 and t == "|" for _, x, t in writes))
            self.assertTrue(all(x + len(t) <= fw for _, x, t in writes))

    def test_full_frame_contents(self):
        g = new_game()
        scr, lay = self.render(g)
        self.assertEqual(lay.plan["tier"], "full")
        text = "".join(t for c in scr.children for _, _, t in c.writes)
        self.assertIn("score 0", text)
        self.assertIn("agents", text)
        for glyph in digdug.DIRT_GLYPHS:
            self.assertIn(glyph, text)
        self.assertIn(digdug.ROCK_GLYPH, text)
        self.assertIn(digdug.POOKA_GLYPH, text)
        self.assertIn(digdug.PLAYER_GLYPH[g.facing], text)
        header = "".join(t for _, _, t in scr.writes)
        self.assertIn("digdug", header)
        go = new_game()
        go.game_over = True
        scr, _ = self.render(go)
        text = "".join(t for c in scr.children for _, _, t in c.writes)
        self.assertIn("GAME OVER", text)
        self.assertIn("r restart | q menu", text)

    def test_controls_visible_and_help_overlay(self):
        for cols, rows in ((93, 28), (93, 20), (56, 24), (45, 24), (45, 20), (32, 20)):
            for help in (False, True):
                scr, lay = self.render(new_game(), cols, rows, help=help)
                self.assertEqual(scr.violations, [], (cols, rows, help))
                if help:
                    text = "".join(t for c in scr.children for _, _, t in c.writes)
                    self.assertIn("Dig Dug - help", text, (cols, rows))
                    continue
                if lay.controls is not None:
                    keys = "\n".join(t for _, _, t in lay.controls.writes)
                    for want in ("walk", "pump", "help", "pause", "menu"):
                        self.assertIn(want, keys, (cols, rows))
                elif lay.plan["hint"] is not None:
                    hint = [t for y, _, t in scr.writes if y == rows - 1]
                    self.assertTrue(any("? help" in t for t in hint), (cols, rows))
                else:
                    head = [t for y, _, t in scr.writes if y == 0]
                    self.assertTrue(any("? help" in t for t in head), (cols, rows))
        _, lay = self.render(new_game(), 93, 28)
        self.assertTrue(any(" Dig Dug " in t for _, _, t in lay.stats.writes))

    def test_too_small_message(self):
        scr, lay = self.render(new_game(), 31, 20)
        self.assertTrue(lay.too_small)
        text = " ".join(t for _, _, t in scr.writes)
        self.assertIn("pane too small", text)


if __name__ == "__main__":
    unittest.main()
