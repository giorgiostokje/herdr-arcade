"""Unit tests for the pure Boulder Dash logic in boulderdash.py (no terminal)."""

import json
import os
import random
import sys
import types
import unittest
from collections import deque
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import boulderdash
from common import AgentSnapshot
from boulderdash import (
    AMOEBA,
    AMOEBA_MAX,
    BOOM,
    BOOM_TICKS,
    BOULDER,
    BUTTERFLY,
    CAVES,
    CLEAR_TIME,
    DEATH_TIME,
    DELTAS,
    DIAMOND,
    DIRT,
    DOWN,
    EMPTY,
    EXIT,
    EXTRA_LIFE,
    FIREFLY,
    GRID_H,
    GRID_W,
    LEFT,
    MAGIC,
    MAGIC_TIME,
    MAX_QUEUE,
    RIGHT,
    ROCKFORD,
    SAVE_VERSION,
    START_LIVES,
    STEEL,
    TICK,
    TIME_POINTS,
    UP,
    WALL,
    Game,
    LoopState,
    Phase,
    dispatch_key,
    handle_key,
    layout_plan,
    parse_cave,
)


def build(rows, quota=1, seconds=100, value=10, extra=15):
    """A test cave: `rows` at the top-left, padded with steel to 30x22. An
    exit goes in the bottom-right corner unless the rows have one."""
    full = [r.ljust(GRID_W, STEEL) for r in rows]
    full += [STEEL * GRID_W] * (GRID_H - len(full))
    if not any(EXIT in r for r in full):
        full[-1] = full[-1][:-1] + EXIT
    cave = {"name": "test", "quota": quota, "time": seconds, "value": value,
            "extra": extra, "map": full}
    return Game(random.Random(0), caves=[cave])


def ticks(game, n):
    for _ in range(n):
        game.step()


def rows_of(game, n):
    return ["".join(r).rstrip(STEEL + EMPTY) for r in game.grid[:n]]


# --------------------------------------------------------------------------
# A solver that plays a cave through with the real physics
# --------------------------------------------------------------------------

WALK = (EMPTY, DIRT, DIAMOND)


def static_reach(game):
    """(diamonds, exit reachable) walking only through empty/dirt/diamond
    cells from Rockford's start: boulders, walls and enemies are opaque."""
    start = (game.rx, game.ry)
    seen, queue, found, exit_ok = {start}, deque([start]), 0, False
    while queue:
        x, y = queue.popleft()
        for dx, dy in DELTAS:
            n = (x + dx, y + dy)
            if n in seen:
                continue
            t = game.cell(*n)
            exit_ok = exit_ok or t == EXIT
            if t in WALK:
                seen.add(n)
                queue.append(n)
                found += t == DIAMOND
    return found, exit_ok


def _danger(game):
    """Cells on or next to an enemy (touching one is death)."""
    return {(x + dx, y + dy) for x, y in game.enemies
            for dx, dy in ((0, 0),) + DELTAS}


def _path(game, targets, banned):
    bad = _danger(game)
    start = (game.rx, game.ry)
    prev = {start: None}
    queue = deque([start])
    while queue:
        c = queue.popleft()
        if c in targets:
            path = []
            while prev[c] is not None:
                path.append(c)
                c = prev[c]
            return path[::-1]
        for dx, dy in DELTAS:
            n = (c[0] + dx, c[1] + dy)
            if n in prev or n in bad or (c, n) in banned:
                continue
            t = game.cell(*n)
            if t in WALK or (t == EXIT and game.exit_open):
                prev[n] = c
                queue.append(n)
    return None


def _first_death(game, path, idle=6):
    """Walk `path` on a copy with the real physics (then stand still a few
    ticks); None if Rockford survives, else the index of the fatal step."""
    c = game.clone()
    cur = (c.rx, c.ry)
    for i, n in enumerate(path):
        c.queue = [(DELTAS.index((n[0] - cur[0], n[1] - cur[1])), False)]
        c.step()
        if c.phase is not None and c.phase.kind == "cave_clear":
            return None
        if not c.alive or (c.rx, c.ry) != n:
            return i
        cur = n
    for _ in range(idle):
        c.step()
        if not c.alive:
            return len(path) - 1
    return None


def _survives_idle(game, move, n=4):
    c = game.clone()
    c.queue = [move] if move else []
    for _ in range(n):
        c.step()
        if not c.alive:
            return False
    return True


def autoplay(game, max_ticks=3000):
    """Greedy player: each tick, the shortest path to the nearest resting
    diamond (the exit once open) that survives being walked with the real
    physics; a step that gets Rockford killed is banned and the path
    replanned. Boulders are treated as walls (no pushing, no snapping).
    Returns (cleared, ticks used)."""
    for tick in range(max_ticks):
        if game.phase is not None:
            return game.phase.kind == "cave_clear", tick
        if game.exit_open:
            targets = {game.exit_pos}
        else:
            targets = {(x, y) for y in range(GRID_H) for x in range(GRID_W)
                       if game.grid[y][x] == DIAMOND
                       and (x, y) not in game.falling}
        banned, move = set(), None
        for _ in range(25):
            path = _path(game, targets, banned)
            if not path:
                break
            k = _first_death(game, path)
            if k is None:
                n = path[0]
                move = (DELTAS.index((n[0] - game.rx, n[1] - game.ry)), False)
                break
            banned.add((path[k - 1] if k else (game.rx, game.ry), path[k]))
        if move is None and not _survives_idle(game, None):
            move = next(((d, False) for d in range(4)
                         if _survives_idle(game, (d, False), 2)), None)
        game.queue = [move] if move else []
        game.step()
    return False, max_ticks


# --------------------------------------------------------------------------
# Caves
# --------------------------------------------------------------------------

class CaveTest(unittest.TestCase):
    def test_at_least_six_caves_rising_difficulty(self):
        self.assertGreaterEqual(len(CAVES), 6)
        quotas = [c["quota"] for c in CAVES]
        times = [c["time"] for c in CAVES]
        self.assertEqual(quotas, sorted(quotas))
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertLess(quotas[0], quotas[-1])
        self.assertGreater(times[0], times[-1])
        self.assertEqual(len({c["name"] for c in CAVES}), len(CAVES))
        text = "".join("".join(c["map"]) for c in CAVES)
        for t in (FIREFLY, BUTTERFLY, AMOEBA, MAGIC):
            self.assertIn(t, text)

    def test_caves_parse_and_start_at_rest(self):
        for i, cave in enumerate(CAVES):
            grid, start, exit_pos = parse_cave(cave["map"])
            self.assertEqual(len(grid), GRID_H)
            self.assertGreater(cave["extra"], cave["value"])
            for y in range(GRID_H - 1):       # nothing rests on an enemy
                for x in range(GRID_W):
                    if cave["map"][y][x] in (BOULDER, DIAMOND):
                        self.assertNotIn(cave["map"][y + 1][x],
                                         (FIREFLY, BUTTERFLY), (cave["name"], x, y))
            g = Game(random.Random(0), level=i + 1)
            before = [row[:] for row in g.grid]
            g.step()  # no input: only enemies may move on the first tick
            for y in range(GRID_H):
                for x in range(GRID_W):
                    if before[y][x] != g.grid[y][x]:
                        self.assertIn(before[y][x] + g.grid[y][x],
                                      (EMPTY + FIREFLY, FIREFLY + EMPTY,
                                       EMPTY + BUTTERFLY, BUTTERFLY + EMPTY),
                                      (cave["name"], x, y))
            self.assertTrue(g.alive)
            ticks(g, 4)
            self.assertEqual(g.falling, set(), cave["name"])
            self.assertFalse(any(BOOM in row for row in g.grid), cave["name"])

    def test_quota_and_exit_reachable_statically(self):
        """Walking only through dirt, space and diamonds (every boulder,
        wall and enemy treated as solid), each cave's quota and exit are
        reachable, with some spare diamonds."""
        for i, cave in enumerate(CAVES):
            g = Game(random.Random(0), level=i + 1)
            found, exit_ok = static_reach(g)
            self.assertGreaterEqual(found, cave["quota"] + 3, cave["name"])
            self.assertTrue(exit_ok, cave["name"])

    def test_every_cave_completable_with_real_physics(self):
        """The autoplayer clears every cave through the real Game (falling
        boulders, rolling, fireflies, butterflies, amoeba), for a few RNG
        seeds, well inside the cave's time."""
        for seed in (0, 1):
            for i, cave in enumerate(CAVES):
                g = Game(random.Random(seed), level=i + 1)
                cleared, used = autoplay(g)
                self.assertTrue(cleared, (seed, cave["name"]))
                self.assertGreaterEqual(g.collected, cave["quota"])
                self.assertLess(used * TICK, cave["time"] * 0.5, cave["name"])
                g.finish_phase()
                self.assertEqual(g.level, i + 2)

    def test_parse_rejects_malformed(self):
        good = list(CAVES[0]["map"])
        parse_cave(good)
        bad = [
            good[:-1],
            [good[0][:-1]] + good[1:],
            ["?" + good[0][1:]] + good[1:],
            [good[0].replace(ROCKFORD, DIRT)] + good[1:],
            [good[0].replace(DIRT, ROCKFORD, 1)] + good[1:],
            [r.replace(EXIT, DIRT) for r in good],
        ]
        for rows in bad:
            with self.assertRaises(ValueError):
                parse_cave(rows)

    def test_caves_wrap_with_less_time(self):
        n = len(CAVES)
        g = Game(random.Random(0), level=n + 1)
        self.assertEqual(g.cave_index, 0)
        self.assertLess(g.time_left, CAVES[0]["time"])
        self.assertEqual(Game(random.Random(0), level=1).time_left,
                         CAVES[0]["time"])


# --------------------------------------------------------------------------
# Rockford
# --------------------------------------------------------------------------

class MovementTest(unittest.TestCase):
    def test_keypress_applied_on_next_tick_one_cell(self):
        g = build(["P..."])
        self.assertTrue(handle_key(g, ord("l")))
        self.assertEqual((g.rx, g.ry), (0, 0))   # queued, not yet applied
        g.update(TICK * 0.5)
        self.assertEqual((g.rx, g.ry), (0, 0))
        g.update(TICK * 0.6)
        self.assertEqual((g.rx, g.ry), (1, 0))
        ticks(g, 5)
        self.assertEqual((g.rx, g.ry), (1, 0))   # never doubled
        self.assertEqual(g.grid[0][:2], [EMPTY, ROCKFORD])  # dirt dug

    def test_presses_between_ticks_are_never_lost(self):
        g = build(["P....."])
        for ch in (ord("d"), curses_key("KEY_RIGHT"), ord("l")):
            handle_key(g, ch)
        g.step()
        self.assertEqual(g.rx, 1)
        ticks(g, 4)
        self.assertEqual(g.rx, 3)

    def test_queue_caps_auto_repeat(self):
        g = build(["P........."])
        for _ in range(10):
            handle_key(g, ord("l"))
        self.assertEqual(len(g.queue), MAX_QUEUE)
        ticks(g, 10)
        self.assertEqual(g.rx, MAX_QUEUE)

    def test_all_direction_keys(self):
        g = build(["...", ".P.", "..."])
        for ch, pos in ((ord("w"), (1, 0)), (ord("s"), (1, 1)),
                        (ord("a"), (0, 1)), (ord("d"), (1, 1)),
                        (ord("k"), (1, 0)), (ord("j"), (1, 1)),
                        (ord("h"), (0, 1)), (ord("l"), (1, 1))):
            handle_key(g, ch)
            g.step()
            self.assertEqual((g.rx, g.ry), pos, chr(ch))
        self.assertEqual(g.facing, RIGHT)

    def test_walls_steel_and_closed_exit_block(self):
        g = build(["P#", "@X"], quota=1)
        for d in (RIGHT, DOWN, UP, LEFT):
            g.queue_move(d)
            g.step()
            self.assertEqual((g.rx, g.ry), (0, 0))

    def test_snap_digs_and_grabs_without_moving(self):
        g = build(["P.*"])
        self.assertTrue(handle_key(g, ord(" ")))
        self.assertTrue(g.snap_armed)
        handle_key(g, ord("l"))
        self.assertFalse(g.snap_armed)
        g.step()
        self.assertEqual((g.rx, g.ry), (0, 0))
        self.assertEqual(g.grid[0][1], EMPTY)
        g.queue_move(RIGHT)
        g.step()
        g.queue_move(RIGHT, snap=True)
        g.step()
        self.assertEqual((g.rx, g.collected), (1, 1))
        self.assertEqual(g.grid[0][2], EMPTY)
        # space twice disarms
        handle_key(g, ord(" "))
        handle_key(g, ord(" "))
        self.assertFalse(g.snap_armed)

    def test_push_boulder_sideways_into_space_only(self):
        g = build(["PO ", "###"])
        g.queue_move(RIGHT)
        g.step()
        self.assertEqual(rows_of(g, 1), [" PO"])
        g = build(["PO.", "###"])      # dirt behind: no push
        g.queue_move(RIGHT)
        g.step()
        self.assertEqual(rows_of(g, 1), ["PO."])
        g = build(["P", "O", " ", "#"])  # never vertically
        g.queue_move(DOWN)
        g.step()
        self.assertEqual((g.rx, g.ry), (0, 0))
        g = build(["POO ", "####"])    # never two at once
        g.queue_move(RIGHT)
        g.step()
        self.assertEqual(rows_of(g, 1), ["POO"])

    def test_no_input_while_paused_or_dead(self):
        g = build(["P.."])
        g.paused = True
        self.assertFalse(handle_key(g, ord("l")))
        self.assertEqual(g.queue, [])
        g.paused = False
        g.alive = False
        self.assertFalse(g.queue_move(RIGHT))


def curses_key(name):
    return getattr(boulderdash.curses, name)


# --------------------------------------------------------------------------
# Physics
# --------------------------------------------------------------------------

class PhysicsTest(unittest.TestCase):
    def test_boulder_and_diamond_fall_one_cell_per_tick(self):
        g = build(["P O*", "#   ", "#   ", "####"])
        g.step()
        self.assertEqual(rows_of(g, 3), ["P", "# O*", "#"])
        self.assertIn((2, 1), g.falling)
        ticks(g, 2)
        self.assertEqual(rows_of(g, 3), ["P", "#", "# O*"])
        self.assertEqual(g.falling, set())  # landed

    def test_resting_boulder_above_is_safe(self):
        g = build(["O  ", ".  ", "P  ", "###"])
        g.queue_move(UP)
        g.step()
        self.assertEqual((g.rx, g.ry), (0, 1))
        ticks(g, 10)
        self.assertTrue(g.alive)
        self.assertEqual(g.grid[0][0], BOULDER)
        # stepping away sideways lets it fall into the cell just left
        g.queue_move(RIGHT)
        ticks(g, 3)
        self.assertTrue(g.alive)
        self.assertEqual(g.grid[2][0], BOULDER)

    def test_falling_boulder_kills(self):
        g = build(["O", " ", " ", "P", "#"])
        ticks(g, 3)
        self.assertFalse(g.alive)
        self.assertEqual(g.phase.kind, "dying")
        self.assertEqual(g.grid[3][0], BOOM)

    def test_stepping_down_from_under_a_boulder_and_stopping_kills(self):
        g = build(["O  ", "P  ", "   ", "   ", "###"])
        g.queue_move(DOWN)
        ticks(g, 3)
        self.assertFalse(g.alive)
        # ...but outrunning it sideways is fine
        g = build(["O  ", "P  ", "   ", "   ", "###"])
        g.queue_move(DOWN)
        g.queue_move(RIGHT)
        ticks(g, 6)
        self.assertTrue(g.alive)

    def test_roll_off_round_things_left_first(self):
        for below in (BOULDER, DIAMOND, WALL):
            g = build(["P    ", "  O  ", "  " + below + "  ", "#####"])
            g.step()
            self.assertEqual(g.grid[1][1], BOULDER, below)
            g.step()
            self.assertEqual(g.grid[2][1], BOULDER, below)
        g = build(["P    ", " .O  ", "  O  ", "#####"])  # left blocked
        g.step()
        self.assertEqual(g.grid[1][3], BOULDER)
        g = build(["P    ", "  O  ", " .O. ", "#####"])  # below-sides blocked
        g.step()
        self.assertEqual(g.grid[1][2], BOULDER)
        g = build(["P    ", "  O  ", "  .  ", "#####"])  # dirt is not round
        g.step()
        self.assertEqual(g.grid[1][2], BOULDER)

    def test_boulder_rolls_off_onto_rockford_only_when_falling(self):
        g = build(["P  ", " O ", " O ", "###"])
        # rockford at (0, 0); left of the top boulder is (0, 1) - empty,
        # below-left (0, 2) empty: it rolls left, then falls to the floor.
        ticks(g, 3)
        self.assertTrue(g.alive)
        self.assertEqual(g.grid[2][0], BOULDER)

    def test_crushing_firefly_and_butterfly(self):
        g = build(["P  O  ", "@@@ @@", "@@@F@@", "@@@@@@"])
        ticks(g, 2)
        self.assertEqual(g.grid[2][3], BOOM)
        self.assertEqual(g.enemies, {})
        self.assertEqual(g.grid[1][0], STEEL)          # steel survives
        ticks(g, BOOM_TICKS)
        self.assertEqual(g.grid[2][3], EMPTY)
        g = build(["P  O  ", "   .  ", "  .B. ", "   .  ", "######"])
        g.grid[1][3] = EMPTY
        ticks(g, 2)
        ticks(g, BOOM_TICKS)
        self.assertTrue(g.alive)
        self.assertEqual(sum(r.count(DIAMOND) for r in g.grid), 9)

    def test_explosion_spares_exit_and_kills_rockford_in_blast(self):
        g = build(["P X", " F "])
        g.enemies[(1, 1)] = UP
        g.explode(1, 1)
        self.assertEqual(g.grid[0][2], EXIT)
        self.assertEqual(g.grid[0][0], BOOM)
        self.assertFalse(g.alive)


class EnemyTest(unittest.TestCase):
    def test_firefly_follows_walls_turning_left(self):
        # a 3x3 ring of space around a wall block: the firefly circles it
        g = build(["P@@@@@", "@   @@", "@ # @@", "@   @@", "@@@@@@"])
        g.grid[1][1] = FIREFLY
        g.enemies = {(1, 1): LEFT}
        seen = []
        for _ in range(8):
            g.step()
            seen.append(next(iter(g.enemies)))
        self.assertEqual(len(set(seen)), 8)       # visits every ring cell
        self.assertEqual(seen[0], (1, 2))          # left of facing LEFT is DOWN

    def test_butterfly_turns_right(self):
        g = build(["P@@@@@", "@   @@", "@ # @@", "@   @@", "@@@@@@"])
        g.grid[1][1] = BUTTERFLY
        g.enemies = {(1, 1): UP}
        g.step()
        self.assertEqual(g.enemies, {(2, 1): RIGHT})

    def test_blocked_enemy_turns_without_moving(self):
        g = build(["P@@", "@F@", "@@@"])
        g.enemies = {(1, 1): LEFT}
        g.step()
        self.assertEqual(g.enemies, {(1, 1): UP})

    def test_touching_an_enemy_explodes_it_and_rockford(self):
        g = build(["P. F"])
        g.enemies = {(3, 0): UP}
        g.queue_move(RIGHT)
        g.step()
        g.queue_move(RIGHT)
        g.step()
        self.assertFalse(g.alive)
        self.assertEqual(g.enemies, {})
        # a butterfly leaves diamonds (Rockford's cell included)
        g = build(["P B ", "    "])
        g.enemies = {(2, 0): UP}
        g.grid[0][1] = EMPTY
        g.queue_move(RIGHT)
        ticks(g, 1 + BOOM_TICKS)
        self.assertFalse(g.alive)
        self.assertEqual(sum(r.count(DIAMOND) for r in g.grid), 6)

    def test_amoeba_grows_and_suffocates_into_diamonds(self):
        g = build(["P@@@@", "@A  @", "@@@@@"])
        g.rng = random.Random(1)
        for _ in range(2000):
            g.step()
            if AMOEBA not in "".join(g.grid[1]):
                break
        self.assertEqual(g.grid[1][1:4], [DIAMOND] * 3)

    def test_big_amoeba_turns_to_boulders(self):
        g = build(["P" + "." * 29] + ["A" * 30] * 2 + ["." * 30])
        g.grid[1][0] = AMOEBA
        count = sum(r.count(AMOEBA) for r in g.grid)
        self.assertGreaterEqual(count, AMOEBA_MAX)
        g.step()
        self.assertEqual(sum(r.count(AMOEBA) for r in g.grid), 0)
        self.assertGreaterEqual(sum(r.count(BOULDER) for r in g.grid), AMOEBA_MAX)

    def test_enemy_touching_amoeba_explodes(self):
        g = build(["P@@@", "@FA@", "@@@@"])
        g.enemies = {(1, 1): UP}
        g.step()
        self.assertEqual(g.enemies, {})

    def test_magic_wall_converts_then_expires(self):
        g = build(["PO  ", " .  ", " M  ", "    ", "@@@@"])
        g.grid[1][1] = EMPTY
        ticks(g, 2)
        self.assertEqual(g.magic, "active")
        self.assertEqual(g.grid[3][1], DIAMOND)
        self.assertEqual(g.grid[0][1], EMPTY)
        ticks(g, int(MAGIC_TIME / TICK) + 2)
        self.assertEqual(g.magic, "expired")
        # an expired wall is just a wall
        g.grid[0][2] = DIAMOND
        g.grid[1][2] = EMPTY
        g.grid[2][2] = MAGIC
        g.grid[3][2] = EMPTY
        ticks(g, 3)
        self.assertEqual(g.grid[1][2], DIAMOND)

    def test_resting_boulder_does_not_trigger_magic_wall(self):
        g = build(["PO", " M", "  ", "@@"])
        ticks(g, 5)
        self.assertEqual(g.magic, "dormant")
        self.assertEqual(g.grid[0][1], BOULDER)


# --------------------------------------------------------------------------
# Scoring, exit, lives, time
# --------------------------------------------------------------------------

class ScoreTest(unittest.TestCase):
    def test_diamonds_worth_more_after_quota_and_exit_opens(self):
        g = build(["P***X"], quota=2, value=10, extra=15)
        g.queue_move(RIGHT)
        g.step()
        self.assertEqual((g.score, g.exit_open), (10, False))
        g.queue_move(RIGHT)
        g.step()
        self.assertEqual((g.score, g.exit_open), (20, True))
        g.queue_move(RIGHT)
        g.step()
        self.assertEqual(g.score, 35)
        self.assertEqual(g.diamond_value(), 15)

    def test_exit_gives_time_bonus_and_next_cave(self):
        g = build(["P*X"], quota=1, seconds=50)
        g.queue_move(RIGHT)
        g.step()
        left = g.seconds_left()
        g.queue_move(RIGHT)
        g.step()
        self.assertEqual(g.phase.kind, "cave_clear")
        self.assertEqual(g.score, 10 + left * TIME_POINTS)
        self.assertEqual(g.last_bonus, left * TIME_POINTS)
        g.update(CLEAR_TIME + 0.01)
        self.assertIsNone(g.phase)
        self.assertEqual(g.level, 2)
        self.assertEqual(g.collected, 0)

    def test_extra_life(self):
        g = build(["P*"], quota=1)
        g.score = EXTRA_LIFE - 5
        g.queue_move(RIGHT)
        g.step()
        self.assertEqual(g.lives, START_LIVES + 1)
        self.assertEqual(g.next_life, 2 * EXTRA_LIFE)

    def test_out_of_time_kills(self):
        g = build(["P.."], seconds=1)
        g.update(1.2)
        self.assertFalse(g.alive)
        self.assertEqual(g.phase.kind, "dying")
        self.assertEqual(g.seconds_left(), 0)

    def test_death_restarts_cave_then_game_over(self):
        g = build(["P.*"], seconds=1)
        g.queue_move(RIGHT)
        g.step()
        for life in range(START_LIVES, 0, -1):
            self.assertEqual(g.lives, life)
            g.update(1.2)
            self.assertIsNotNone(g.phase)
            for _ in range(int(DEATH_TIME / 0.1) + 2):
                g.update(0.1)
            self.assertIsNone(g.phase)
            if not g.game_over:
                self.assertEqual((g.rx, g.ry), (0, 0))  # cave reloaded
                self.assertEqual(g.grid[0][1], DIRT)
        self.assertTrue(g.game_over)
        self.assertEqual(g.lives, 0)
        self.assertFalse(g.update(1.0))

    def test_world_keeps_ticking_while_dying(self):
        g = build(["O", " ", " ", "P", "#"])
        ticks(g, 3)
        before = g.booms[(0, 3)][0]
        g.update(TICK * 1.01)
        self.assertEqual(g.booms[(0, 3)][0], before - 1)


# --------------------------------------------------------------------------
# Save / restore
# --------------------------------------------------------------------------

class SaveTest(unittest.TestCase):
    def busy_game(self):
        # The amoeba cave, a few seconds in: the amoeba's growth uses the RNG.
        level = next(i for i, c in enumerate(CAVES)
                     if AMOEBA in "".join(c["map"])) + 1
        g = Game(random.Random(42), level=level)
        autoplay(g, max_ticks=40)       # alive, some diamonds taken
        self.assertTrue(g.alive)
        g.update(TICK * 0.4)
        g.queue_move(DOWN)
        g.snap_armed = True
        return g

    def test_round_trip_through_json(self):
        g = self.busy_game()
        data = json.loads(json.dumps(g.to_save()))
        r = Game.from_save(data)
        self.assertIsNotNone(r)
        self.assertEqual(r.to_save(), g.to_save())
        self.assertEqual(r.rng.random(), g.rng.random())

    def test_restored_game_plays_identically(self):
        g = self.busy_game()
        r = Game.from_save(json.loads(json.dumps(g.to_save())))
        script = random.Random(9)
        for i in range(400):
            ch = script.choice([ord(c) for c in "wasd  "] + [-1] * 4)
            for game in (g, r):
                if ch != -1:
                    handle_key(game, ch)
                game.update(0.05)
        self.assertEqual(r.to_save(), g.to_save())
        self.assertGreater(sum(row.count(AMOEBA) for row in g.grid), 1)

    def test_phase_resolved_before_save(self):
        g = build(["O", " ", " ", "P", "#"])
        ticks(g, 3)
        self.assertIsNotNone(g.phase)
        g.finish_phase()
        self.assertIsNone(g.phase)
        self.assertEqual(g.lives, START_LIVES - 1)
        self.assertIsNotNone(Game.from_save(g.to_save()))

    def test_explosions_and_magic_survive_a_save(self):
        g = Game(random.Random(3), level=1)
        g.explode(10, 10)
        g.magic, g.magic_t = "active", 4.5
        r = Game.from_save(json.loads(json.dumps(g.to_save())))
        self.assertEqual(r.booms, g.booms)
        self.assertEqual((r.magic, r.magic_t), ("active", 4.5))

    def test_corrupt_or_incompatible_is_none(self):
        g = self.busy_game()
        good = json.loads(json.dumps(g.to_save()))

        def variant(**kw):
            d = json.loads(json.dumps(good))
            d.update(kw)
            return d

        grid = good["grid"]
        short = list(grid)
        short[0] = short[0][:-1]
        no_rock = [row.replace(ROCKFORD, DIRT) for row in grid]
        cases = [
            None, [], {}, "x",
            variant(version=SAVE_VERSION + 1),
            variant(grid=short),
            variant(grid=grid[:-1]),
            variant(grid=["?" * GRID_W] * GRID_H),
            variant(grid=no_rock),
            variant(rockford=[good["rockford"][0] + 1, good["rockford"][1]]),
            variant(enemies=[[0, 0, 1]]),
            variant(falling=[[good["rockford"][0], good["rockford"][1]]]),
            variant(booms=[[0, 0, 2, EMPTY]]),
            variant(magic="sparkly"),
            variant(queue=[[5, False]]),
            variant(queue=[[1, False]] * (MAX_QUEUE + 1)),
            variant(facing=UP),
            variant(rng=[1, 2, 3]),
            variant(lives=0),
            variant(level=0),
            variant(score="lots"),
            variant(collected=-1),
        ]
        no_rng = json.loads(json.dumps(good))
        del no_rng["rng"]
        cases.append(no_rng)
        for d in cases:
            self.assertIsNone(Game.from_save(d), repr(d)[:80])


# --------------------------------------------------------------------------
# Keys and the run loop's dispatch
# --------------------------------------------------------------------------

class KeyTest(unittest.TestCase):
    def session(self, attention=None):
        s = types.SimpleNamespace(attention=attention)
        s.dismiss = lambda: setattr(s, "attention", None)
        return s

    def test_loop_keys(self):
        g = Game(random.Random(1))
        h = boulderdash.HelpOverlay(boulderdash.TITLE, boulderdash.HELP)
        st = LoopState()
        s = self.session()
        self.assertEqual(dispatch_key(ord("q"), g, h, st, s), "quit")
        self.assertEqual(dispatch_key(ord("r"), g, h, st, s), "restart")
        self.assertIsNone(dispatch_key(ord("p"), g, h, st, s))
        self.assertTrue(g.paused)
        dispatch_key(ord("p"), g, h, st, s)
        self.assertFalse(g.paused)
        self.assertFalse(handle_key(g, ord("q")))  # q belongs to the loop
        dispatch_key(ord("l"), g, h, st, s)
        self.assertEqual(g.queue, [(RIGHT, False)])

    def test_help_pauses_and_closing_resumes(self):
        g = Game(random.Random(1))
        h = boulderdash.HelpOverlay(boulderdash.TITLE, boulderdash.HELP)
        st = LoopState()
        s = self.session()
        dispatch_key(ord("?"), g, h, st, s)
        self.assertTrue(h.is_open and g.paused)
        self.assertFalse(g.update(1.0))
        dispatch_key(curses_key("KEY_DOWN"), g, h, st, s)
        self.assertTrue(h.is_open)
        dispatch_key(ord("l"), g, h, st, s)       # closes, swallowed
        self.assertFalse(h.is_open or g.paused)
        self.assertEqual(g.queue, [])

    def test_help_keeps_a_manual_pause(self):
        g = Game(random.Random(1))
        g.paused = True
        h = boulderdash.HelpOverlay(boulderdash.TITLE, boulderdash.HELP)
        st = LoopState()
        dispatch_key(ord("?"), g, h, st, self.session())
        dispatch_key(ord("x"), g, h, st, self.session())
        self.assertTrue(g.paused)

    def test_resume_banner_takes_first_key(self):
        g = Game(random.Random(1))
        g.paused = True
        h = boulderdash.HelpOverlay(boulderdash.TITLE, boulderdash.HELP)
        st = LoopState(resume=True)
        self.assertIsNone(dispatch_key(ord("q"), g, h, st, self.session()))
        self.assertFalse(g.paused or st.resume)
        agent = boulderdash.AgentSnapshot("p1", "claude", "claude", "blocked")
        s = self.session((agent, "working"))
        g.paused = True
        st.paused_by_attention = True
        dispatch_key(ord("l"), g, h, st, s)
        self.assertIsNone(s.attention)
        self.assertFalse(g.paused)
        self.assertEqual(g.queue, [])


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

    def centred(self, plan, cols, rows):
        regions = [plan[k] for k in ("board", "stats", "controls", "agents")
                   if plan[k] is not None]
        top = min(r[0] for r in regions)
        left = min(r[1] for r in regions)
        bottom = max(r[0] + r[2] for r in regions)
        right = max(r[1] + r[3] for r in regions)
        self.assertLessEqual(abs(left - (cols - right)), 1, (cols, rows))
        last = rows - 1 if plan["hint"] is not None else rows
        self.assertGreaterEqual(top, 1)
        self.assertLessEqual(abs((top - 1) - (last - bottom)), 1, (cols, rows))

    def test_full_at_popup_93x28(self):
        plan = layout_plan(93, 28)
        self.assertEqual(plan["tier"], "full")
        for key in ("stats", "controls", "agents"):
            self.assertIsNotNone(plan[key], key)
        self.assertEqual(plan["board"][2:], (GRID_H + 2, GRID_W * 2 + 2))
        self.assertEqual(plan["board"][1], 0)        # 62 + 1 + 30 = 93
        self.assertEqual(plan["controls"][1] + plan["controls"][3], 93)
        self.fits(plan, 93, 28)
        self.centred(plan, 93, 28)

    def test_no_agents_tier(self):
        plan = layout_plan(86, 28)
        self.assertEqual(plan["tier"], "side")
        self.assertIsNone(plan["agents"])
        self.assertIsNotNone(plan["stats"])
        self.assertIsNotNone(plan["controls"])
        self.fits(plan, 86, 28)
        self.centred(plan, 86, 28)

    def test_no_side_column_tier(self):
        plan = layout_plan(80, 28)
        self.assertEqual(plan["tier"], "board")
        for key in ("stats", "controls", "agents"):
            self.assertIsNone(plan[key])
        self.assertIsNotNone(plan["hint"])
        self.fits(plan, 80, 28)
        self.centred(plan, 80, 28)
        plan = layout_plan(70, 25)   # no spare row: the hint joins the header
        self.assertIsNone(plan["hint"])
        self.assertTrue(plan["hint_in_header"])
        self.centred(plan, 70, 25)

    def test_too_small(self):
        for cols, rows in ((61, 28), (93, 24), (40, 10)):
            self.assertTrue(layout_plan(cols, rows)["too_small"], (cols, rows))

    def test_every_size_fits_and_is_centred(self):
        for cols in range(62, 130, 3):
            for rows in range(25, 40, 2):
                plan = layout_plan(cols, rows)
                self.assertFalse(plan["too_small"], (cols, rows))
                self.fits(plan, cols, rows)
                self.centred(plan, cols, rows)

    def test_key_hint_leads_with_help(self):
        self.assertTrue(boulderdash.key_hint().startswith("? help"))


class ConstantsTest(unittest.TestCase):
    def test_controls_cover_every_key(self):
        keys = " ".join(k for k, _ in boulderdash.CONTROLS)
        for token in ("?", "q", "p", "r", "arrows", "wasd", "hjkl", "space"):
            self.assertIn(token, keys)
        self.assertEqual(boulderdash.CONTROLS[-1], ("q", "menu"))
        actions = " ".join(a for _, a in boulderdash.CONTROLS)
        self.assertIn("snap", actions)

    def test_help_sections(self):
        heads = {h: b for h, b in boulderdash.HELP}
        for need in ("Goal", "Controls", "Scoring", "Tips", "Legend",
                     "Falling and rolling", "Fireflies and butterflies",
                     "Amoeba and magic wall", "Ticks and the move queue"):
            self.assertIn(need, heads)
            self.assertTrue(heads[need].strip(), need)
        text = " ".join(h + " " + b for h, b in boulderdash.HELP)
        self.assertTrue(all(ord(c) < 128 for c in text))
        for word in ("snap", "space", "quota", "exit", "firefly", "butterfly",
                     "{}", "}{", "()", "<>", "%%", "~~", "stand right under"):
            self.assertIn(word, text)
        self.assertIn("%d points per second" % TIME_POINTS, heads["Scoring"])
        self.assertIn("every %d points" % EXTRA_LIFE, heads["Scoring"])
        self.assertIn("%d caves" % len(CAVES), text)
        self.assertIn("Up to %d presses" % MAX_QUEUE,
                      heads["Ticks and the move queue"])


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
            raise boulderdash.curses.error()
        child = FakeWin(h, w, self.root, self.y0 + y, self.x0 + x)
        self.root.children.append(child)
        return child

    def erase(self):
        self.writes = []

    def addstr(self, y, x, text, attr=0):
        if y < 0 or y >= self.rows or x < 0 or x + len(text) > self.cols:
            self.root.violations.append((y, x, text))
            raise boulderdash.curses.error()
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
        overlay = boulderdash.HelpOverlay(boulderdash.TITLE, boulderdash.HELP)
        if help_open:
            overlay.open()
        with mock.patch("curses.color_pair", return_value=0):
            lay = boulderdash.Layout(scr)
            boulderdash.draw(game, lay, self.session(attention), resume, 999,
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
        busy = Game(random.Random(1), level=5)
        busy.collected = busy.quota
        busy.exit_open = True
        busy.magic = "active"
        busy.snap_armed = True
        busy.explode(15, 12, DIAMOND)
        yield "busy", busy
        for elapsed in (0.0, 0.15):
            dy = Game(random.Random(1))
            dy.explode(dy.rx, dy.ry)
            dy.phase.elapsed = elapsed
            yield "dying", dy
        cc = Game(random.Random(1), level=8)
        cc.phase = Phase("cave_clear", CLEAR_TIME)
        yield "cave_clear", cc
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
            self.assertEqual((board.y0, board.x0), (2, 0))
            self.assertEqual(board.getmaxyx(), (GRID_H + 2, GRID_W * 2 + 2))
            self.assertIsNotNone(lay.agents)
            for child in scr.children:
                self.assertLessEqual(child.x0 + child.cols, 93)
                self.assertLessEqual(child.y0 + child.rows, 28)
            cells = [(x, t) for y, x, t in board.writes
                     if 0 < y < board.rows - 1 and 0 < x < board.cols - 1
                     and not t.startswith(" ")]
            self.assertTrue(all(len(t) == 2 and x % 2 == 1 for x, t in cells),
                            name)

    def test_rockford_exit_and_overlays_drawn(self):
        g = Game(random.Random(1))
        scr, lay = self.render(g)
        self.assertIn((1 + g.ry, 1 + g.rx * 2, "@>"), lay.board.writes)
        go = Game(random.Random(1))
        go.game_over = True
        scr, lay = self.render(go)
        texts = [t for _, _, t in lay.board.writes]
        self.assertIn(" GAME OVER ", texts)
        self.assertIn(" r restart | ? help | q menu ", texts)
        op = Game(random.Random(1))
        op.exit_open = True
        op.ticks = 0
        scr, lay = self.render(op)
        ex, ey = op.exit_pos
        self.assertIn((1 + ey, 1 + ex * 2, "[]"), lay.board.writes)

    def test_banners_and_smaller_tiers(self):
        agent = AgentSnapshot("p1", "claude", "claude", "blocked")
        for kw in ({"attention": (agent, "working")}, {"resume": True}):
            scr, lay = self.render(Game(random.Random(1)), **kw)
            self.assertEqual(scr.violations, [])
        for cols, rows in ((86, 28), (80, 28), (70, 25), (62, 25), (40, 10)):
            scr, lay = self.render(Game(random.Random(1)), cols, rows)
            self.assertEqual(scr.violations, [], (cols, rows))
        scr, lay = self.render(Game(random.Random(1)), 80, 28)
        header = [t for y, _, t in scr.writes if y == 0]
        self.assertTrue(any("score" in t for t in header))
        hint = [t for y, _, t in scr.writes if y == 27]
        self.assertTrue(hint and hint[0].startswith("? help"), hint)
        scr, lay = self.render(Game(random.Random(1)), 70, 25)
        header = [t for y, _, t in scr.writes if y == 0]
        self.assertTrue(any("? help" in t for t in header))
        scr, lay = self.render(Game(random.Random(1)), 40, 10)
        self.assertTrue(any("pane too small" in t for _, _, t in scr.writes))

    def test_controls_box_and_stats_at_popup_size(self):
        g = Game(random.Random(1))
        g.snap_armed = True
        scr, lay = self.render(g)
        text = self.all_text(scr)
        for keys, action in boulderdash.CONTROLS:
            self.assertIn(keys, text)
        for word in ("boulder dash", "score 0", "gems 0/%d" % g.quota,
                     "time %d" % CAVES[0]["time"], "SNAP", "agents", "lives 3"):
            self.assertIn(word, text)

    def test_help_overlay_renders_at_every_tier(self):
        for cols, rows in ((93, 28), (86, 28), (80, 28), (70, 25), (40, 10)):
            for name, game in self.games():
                scr, lay = self.render(game, cols, rows, help_open=True)
                self.assertEqual(scr.violations, [], (cols, rows, name))
            text = self.all_text(scr)
            self.assertIn("Boulder Dash - help", text)
            if cols >= 62:
                self.assertIn("Goal", text)
                self.assertIn("? help" if lay.controls is None else " keys ",
                              text, (cols, rows))


if __name__ == "__main__":
    unittest.main()
