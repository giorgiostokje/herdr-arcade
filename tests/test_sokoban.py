"""Unit tests for the pure game logic, levels, layout and rendering in
sokoban.py (no terminal needed). A push solver below proves every level
solvable, and each solution is replayed through the game's own move rules."""

import heapq
import json
import os
import sys
import time
import types
import unittest
from collections import deque
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sokoban  # noqa: E402
from common import AgentSnapshot  # noqa: E402
from sokoban import (  # noqa: E402
    DOWN,
    FIELD_H,
    FIELD_W,
    LEFT,
    LEVEL_SET,
    RIGHT,
    SOLVED_TIME,
    UP,
    Game,
    Level,
    layout_plan,
    level_points,
)

LETTER_DIR = {"u": UP, "d": DOWN, "l": LEFT, "r": RIGHT}
STEPS = ((0, -1, "u"), (0, 1, "d"), (-1, 0, "l"), (1, 0, "r"))


# --------------------------------------------------------------------------
# Solver: weighted A* over push states (keeper normalised to its reachable
# region), with dead-square and 2x2 freeze pruning. Returns a full move
# string in the game's notation (lower case walk, upper case push), None if
# the level has no solution, or False if the node budget ran out. The
# search is complete, so None really means unsolvable.
# --------------------------------------------------------------------------

HEURISTIC_WEIGHT = 3

class Solver:
    def __init__(self, level: Level):
        self.floor = level.floor
        self.goals = frozenset(level.goals)
        self.start = (frozenset(level.crates), level.start)
        self.dist = self._goal_distances()
        self.nodes = 0

    def _goal_distances(self):
        """Pushes from each cell to the nearest goal ignoring other crates;
        cells missing from the map are dead squares (a crate there can never
        reach a goal). Built by pulling a crate backwards from every goal."""
        dist = {g: 0 for g in self.goals}
        todo = deque(self.goals)
        while todo:
            x, y = todo.popleft()
            for dx, dy, _ in STEPS:
                prev, stand = (x + dx, y + dy), (x + 2 * dx, y + 2 * dy)
                if prev in self.floor and stand in self.floor and prev not in dist:
                    dist[prev] = dist[(x, y)] + 1
                    todo.append(prev)
        return dist

    def _reach(self, crates, keeper):
        seen = {keeper}
        todo = [keeper]
        while todo:
            x, y = todo.pop()
            for dx, dy, _ in STEPS:
                n = (x + dx, y + dy)
                if n in self.floor and n not in crates and n not in seen:
                    seen.add(n)
                    todo.append(n)
        return seen

    def _frozen(self, crates, c):
        """A crate off-goal locked in a 2x2 square of walls and crates."""
        x, y = c
        for ox, oy in ((0, 0), (-1, 0), (0, -1), (-1, -1)):
            square = [(x + ox + i, y + oy + j) for i in (0, 1) for j in (0, 1)]
            if all(s not in self.floor or s in crates for s in square):
                if any(s in crates and s not in self.goals for s in square):
                    return True
        return False

    def _h(self, crates):
        # Weighted: finds a (not necessarily push-optimal) solution fast.
        return HEURISTIC_WEIGHT * sum(self.dist[c] for c in crates)

    def solve(self, max_nodes=300000):
        crates0, keeper0 = self.start
        if any(c not in self.dist for c in crates0):
            return None
        key0 = (crates0, min(self._reach(crates0, keeper0)))
        best_g = {key0: 0}
        parent = {key0: None}
        heap = [(self._h(crates0), 0, 0, key0, keeper0)]
        tie = 1
        while heap:
            _, g, _, key, keeper = heapq.heappop(heap)
            if best_g[key] < g:
                continue
            crates = key[0]
            if crates == self.goals:
                return self._moves(parent, key)
            self.nodes += 1
            if self.nodes > max_nodes:
                return False
            reach = self._reach(crates, keeper)
            for c in crates:
                for dx, dy, letter in STEPS:
                    stand, dest = (c[0] - dx, c[1] - dy), (c[0] + dx, c[1] + dy)
                    if stand not in reach or dest not in self.dist or dest in crates:
                        continue
                    nc = (crates - {c}) | {dest}
                    if self._frozen(nc, dest):
                        continue
                    nkey = (nc, min(self._reach(nc, c)))
                    if g + 1 < best_g.get(nkey, 1 << 30):
                        best_g[nkey] = g + 1
                        parent[nkey] = (key, stand, letter)
                        heapq.heappush(heap, (g + 1 + self._h(nc), g + 1, tie, nkey, c))
                        tie += 1
        return None

    def _walk(self, crates, src, dst):
        prev = {src: None}
        todo = deque([src])
        while todo:
            c = todo.popleft()
            if c == dst:
                out = []
                while prev[c] is not None:
                    c, letter = prev[c]
                    out.append(letter)
                return "".join(reversed(out))
            for dx, dy, letter in STEPS:
                n = (c[0] + dx, c[1] + dy)
                if n in self.floor and n not in crates and n not in prev:
                    prev[n] = (c, letter)
                    todo.append(n)
        raise AssertionError("keeper cannot reach %r" % (dst,))

    def _moves(self, parent, key):
        pushes = []
        while parent[key] is not None:
            pkey, stand, letter = parent[key]
            pushes.append((pkey[0], stand, letter))
            key = pkey
        keeper, out = self.start[1], []
        for crates, stand, letter in reversed(pushes):
            out.append(self._walk(crates, keeper, stand))
            out.append(letter.upper())
            dx, dy = sokoban.DELTAS[LETTER_DIR[letter]]
            keeper = (stand[0] + dx, stand[1] + dy)
        return "".join(out)


def play(game, moves):
    """Feed a move string through Game.move; returns how many were taken."""
    taken = 0
    for letter in moves:
        if game.move(LETTER_DIR[letter.lower()]):
            taken += 1
    return taken


def tiny_levels():
    """A three-level set for fast progression tests."""
    return tuple(Level(i + 1, rows) for i, rows in enumerate((
        ("#####",
         "#@$.#",
         "#####"),
        ("######",
         "#@ $.#",
         "######"),
        ("#######",
         "#@ $ .#",
         "#######"),
    )))


def new_game(levels=None):
    return Game(levels if levels is not None else tiny_levels())


def open_level(rows):
    """A one-level game built from `rows` (XSB)."""
    return Game((Level(1, rows),))


# --------------------------------------------------------------------------
# Levels
# --------------------------------------------------------------------------

class LevelSetTest(unittest.TestCase):
    def test_enough_levels(self):
        self.assertGreaterEqual(len(sokoban.LEVELS), 30)
        self.assertEqual(len(LEVEL_SET), len(sokoban.LEVELS))
        self.assertEqual(len({lvl.rows for lvl in LEVEL_SET}), len(LEVEL_SET))

    def test_every_level_well_formed(self):
        for lvl in LEVEL_SET:
            with self.subTest(level=lvl.number):
                text = "".join(lvl.rows)
                self.assertTrue(set(text) <= set("# .$*@+"), set(text))
                self.assertEqual(text.count("@") + text.count("+"), 1)  # one keeper
                self.assertEqual(len(lvl.crates), len(lvl.goals))
                self.assertGreaterEqual(len(lvl.crates), 1)
                self.assertNotEqual(lvl.crates, lvl.goals)    # not pre-solved
                # Enclosed: the flood from the keeper never reaches the grid
                # edge, so it never escapes the walls.
                for x, y in lvl.floor:
                    self.assertTrue(0 < x < lvl.width - 1 and 0 < y < lvl.height - 1,
                                    (x, y))
                    self.assertTrue(x < len(lvl.rows[y]), (x, y))
                self.assertTrue(lvl.crates <= lvl.floor)
                self.assertTrue(lvl.goals <= lvl.floor)
                # Nothing but blank outside: no stray crates/goals/floor.
                for y, row in enumerate(lvl.rows):
                    for x, c in enumerate(row):
                        if (x, y) not in lvl.floor:
                            self.assertIn(c, "# ", (x, y))
                # Fits the playfield at two columns per cell.
                self.assertLessEqual(lvl.width, FIELD_W)
                self.assertLessEqual(lvl.height, FIELD_H)

    def test_difficulty_ramps(self):
        crates = [len(lvl.crates) for lvl in LEVEL_SET]
        self.assertEqual(crates, sorted(crates))
        self.assertEqual(crates[0], 1)
        self.assertGreaterEqual(crates[-1], 6)

    def test_leaky_level_detected(self):
        lvl = Level(1, ("#####", "#@$. ", "#####"))
        self.assertTrue(any(x == lvl.width - 1 for x, _ in lvl.floor))


class SolvabilityTest(unittest.TestCase):
    def test_every_level_solvable_by_the_game_rules(self):
        """Solve each level, then replay the solution through Game.move: the
        game must accept every move and end solved."""
        started = time.monotonic()
        for i, lvl in enumerate(LEVEL_SET):
            with self.subTest(level=lvl.number):
                solver = Solver(lvl)
                moves = solver.solve()
                self.assertTrue(moves, "level %d: no solution found" % lvl.number)
                game = Game()
                game.unlocked = i
                if i:
                    game.goto_level(i)
                self.assertEqual(play(game, moves), len(moves))
                self.assertTrue(game.is_solved())
                self.assertEqual(game.bests[i], [len(moves),
                                                 sum(c.isupper() for c in moves)])
        self.assertLess(time.monotonic() - started, 60)

    def test_solver_rejects_impossible(self):
        corner = Level(1, ("#####",
                           "#$  #",
                           "#  .#",
                           "#@  #",
                           "#####"))
        self.assertIsNone(Solver(corner).solve())
        self.assertEqual(Solver(tiny_levels()[0]).solve(), "R")


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------

ROOM = ("#######",
        "#     #",
        "# $$ .#",
        "#@  $.#",
        "#    .#",
        "#######")


class MovementTest(unittest.TestCase):
    def test_walk_and_walls(self):
        g = open_level(ROOM)
        self.assertEqual(g.player, (1, 3))
        self.assertFalse(g.move(LEFT))            # wall
        self.assertEqual(g.moves, 0)
        self.assertTrue(g.move(UP))
        self.assertEqual(g.player, (1, 2))
        self.assertEqual(g.history, ["u"])
        self.assertEqual((g.moves, g.pushes), (1, 0))

    def test_push_one_crate(self):
        g = open_level(ROOM)
        g.move(UP)
        self.assertFalse(g.move(RIGHT))
        # (2,2) crate with (3,2) crate behind: two crates never move.
        self.assertEqual(g.player, (1, 2))
        self.assertEqual(g.moves, 1)
        g.move(DOWN)
        g.move(RIGHT)
        g.move(RIGHT)
        self.assertEqual(g.player, (3, 3))
        self.assertTrue(g.move(RIGHT))            # push (4,3) onto the goal (5,3)
        self.assertIn((5, 3), g.crates)
        self.assertEqual(g.history[-1], "R")
        self.assertEqual(g.pushes, 1)
        self.assertEqual(g.on_goal, 1)
        self.assertFalse(g.move(RIGHT))           # crate against the wall

    def test_counts_and_glyph_states(self):
        g = open_level(ROOM)
        play(g, "rrR")
        self.assertEqual((g.moves, g.pushes), (3, 1))

    def test_no_move_while_paused(self):
        g = open_level(ROOM)
        g.paused = True
        self.assertFalse(g.move(UP))
        self.assertFalse(g.undo())
        t = g.level_time
        self.assertFalse(g.update(1.0))
        self.assertEqual(g.level_time, t)
        g.paused = False
        g.update(1.0)
        self.assertEqual(g.level_time, t + 1.0)


class UndoRedoTest(unittest.TestCase):
    def test_undo_walk_and_push(self):
        g = open_level(ROOM)
        start = (g.player, set(g.crates))
        play(g, "rrRu")
        after = (g.player, set(g.crates))
        for _ in range(4):
            self.assertTrue(g.undo())
        self.assertFalse(g.undo())                # nothing left
        self.assertEqual((g.player, g.crates), start)
        self.assertEqual(g.moves, 0)
        for _ in range(4):
            self.assertTrue(g.redo())
        self.assertFalse(g.redo())
        self.assertEqual((g.player, g.crates), after)
        self.assertEqual("".join(g.history), "rrRu")

    def test_new_move_forgets_redo(self):
        g = open_level(ROOM)
        play(g, "rr")
        g.undo()
        self.assertEqual(g.redo_stack, ["r"])
        g.move(DOWN)
        self.assertEqual(g.redo_stack, [])
        self.assertFalse(g.redo())

    def test_restart_keeps_moves_for_redo(self):
        g = open_level(ROOM)
        play(g, "rrRd")
        g.level_time = 12.0
        pos = (g.player, set(g.crates))
        g.restart_level()
        self.assertEqual(g.moves, 0)
        self.assertEqual(g.player, (1, 3))
        self.assertEqual(g.crates, set(g.level.crates))
        self.assertEqual(g.level_time, 0.0)
        while g.redo():
            pass
        self.assertEqual((g.player, g.crates), pos)

    def test_undo_out_of_solved(self):
        g = new_game()
        g.move(RIGHT)
        self.assertTrue(g.is_solved())
        self.assertIsNotNone(g.phase)
        self.assertFalse(g.undo())                # the banner is showing
        g.phase = None                            # (a solved last level stays)
        self.assertTrue(g.undo())
        self.assertFalse(g.is_solved())


class ProgressTest(unittest.TestCase):
    def test_solve_unlocks_and_advances(self):
        g = new_game()
        self.assertFalse(g.goto_level(1))         # locked
        self.assertTrue(g.move(RIGHT))
        self.assertTrue(g.is_solved())
        self.assertEqual(g.phase.kind, "solved")
        self.assertEqual(g.unlocked, 1)
        self.assertEqual(g.bests, {0: [1, 1]})
        self.assertFalse(g.move(LEFT))            # frozen during the banner
        self.assertFalse(g.update(SOLVED_TIME / 2))
        self.assertTrue(g.update(SOLVED_TIME))
        self.assertIsNone(g.phase)
        self.assertEqual(g.index, 1)
        self.assertEqual(g.moves, 0)

    def test_step_between_unlocked_levels(self):
        g = new_game()
        g.move(RIGHT)
        g.resolve_phases()
        self.assertEqual(g.index, 1)
        self.assertFalse(g.step_level(1))         # level 3 still locked
        self.assertTrue(g.step_level(-1))
        self.assertEqual(g.index, 0)
        self.assertFalse(g.step_level(-1))        # no level 0
        self.assertTrue(g.step_level(1))
        self.assertEqual(g.index, 1)

    def test_bests_keep_minimum_moves_and_pushes_separately(self):
        g = new_game()
        g.move(RIGHT)
        g.resolve_phases()
        play(g, "rR")                             # level 2 in 2 moves, 1 push
        g.resolve_phases()
        g.goto_level(1)
        play(g, "rlrR")                           # worse: 4 moves
        self.assertEqual(g.bests[1], [2, 1])
        self.assertIsNone(g.last_record)
        g.bests[1] = [2, 5]                       # pretend an older push record
        g.phase = None
        g.restart_level()
        play(g, "rR")
        self.assertEqual(g.bests[1], [2, 1])
        self.assertEqual(g.last_record, "best")

    def test_score(self):
        self.assertEqual(level_points(1), 999)
        self.assertEqual(level_points(950), 100)
        g = new_game()
        self.assertEqual(g.score, 0)
        g.move(RIGHT)
        self.assertEqual(g.score, 999)
        g.resolve_phases()
        play(g, "rR")
        self.assertEqual(g.score, 999 + 998)
        self.assertEqual(g.solved_count, 2)

    def test_last_level_then_all_solved(self):
        g = new_game()
        for moves in ("R", "rR", "rrR"):
            play(g, moves)
            g.resolve_phases()
        self.assertTrue(g.all_solved())
        self.assertEqual(g.index, 2)              # stays on the last level
        self.assertTrue(g.is_solved())
        self.assertEqual(g.unlocked, 2)
        self.assertFalse(g.move(LEFT))

    def test_skipped_level_leads_back_to_unsolved(self):
        g = new_game()
        g.unlocked = 2
        g.bests = {1: [2, 1]}
        g.goto_level(2)
        play(g, "rrR")
        self.assertEqual(g.next_index(), 0)       # wraps to the unsolved level 1
        g.resolve_phases()
        self.assertEqual(g.index, 0)

    def test_random_play_keeps_invariants(self):
        """A few thousand random keys (moves, undo, redo, restart, level
        steps) with ticks between: never a crash, crates never merge or
        leave the floor, and a save taken anywhere restores identically."""
        import random
        rng = random.Random(7)
        g = Game()
        g.unlocked = 8
        help = sokoban.HelpOverlay(sokoban.TITLE, sokoban.HELP)
        st = sokoban.LoopState()
        session = types.SimpleNamespace(attention=None, dismiss=lambda: None)
        keys = "hjklwasduzyr[]bnp" + "\x00" * 8
        for i in range(3000):
            ch = rng.choice(keys)
            ch = ord(ch) if ch != "\x00" else rng.choice(
                (sokoban.curses.KEY_UP, sokoban.curses.KEY_DOWN,
                 sokoban.curses.KEY_LEFT, sokoban.curses.KEY_RIGHT))
            if sokoban.dispatch_key(ch, g, help, st, session) == "restart":
                g.restart_level()
            g.update(0.05)
            lvl = g.level
            self.assertEqual(len(g.crates), len(lvl.crates))
            self.assertTrue(g.crates <= lvl.floor)
            self.assertIn(g.player, lvl.floor)
            self.assertNotIn(g.player, g.crates)
            self.assertEqual(g.moves, len(g.history))
            if i % 250 == 0:
                g.resolve_phases()
                r = Game.from_save(json.loads(json.dumps(g.to_save())))
                self.assertIsNotNone(r)
                self.assertEqual((r.player, r.crates), (g.player, g.crates))

    def test_reset(self):
        g = new_game()
        g.move(RIGHT)
        g.resolve_phases()
        g.reset()
        self.assertEqual((g.index, g.unlocked, g.bests, g.moves), (0, 0, {}, 0))


# --------------------------------------------------------------------------
# Save / restore
# --------------------------------------------------------------------------

class SaveTest(unittest.TestCase):
    def advanced(self):
        """Real level set: level 1 solved, level 2 partly played with some
        undone moves waiting to be redone."""
        g = Game()
        play(g, Solver(LEVEL_SET[0]).solve())
        g.resolve_phases()
        play(g, "Rrur")
        g.undo()
        g.update(3.5)
        return g

    def test_round_trip(self):
        g = self.advanced()
        data = json.loads(json.dumps(g.to_save()))
        r = Game.from_save(data)
        self.assertIsNotNone(r)
        self.assertEqual(r.to_save(), g.to_save())
        for attr in ("index", "unlocked", "bests", "player", "crates", "history",
                     "redo_stack", "level_time"):
            self.assertEqual(getattr(r, attr), getattr(g, attr), attr)
        # Continuing the restored game plays out identically.
        for game in (g, r):
            game.redo()
            game.undo()
            game.undo()
            play(game, "dRR")
        self.assertEqual(r.to_save(), g.to_save())
        self.assertEqual((r.player, r.crates), (g.player, g.crates))

    def test_solution_continues_after_restore(self):
        g = Game()
        moves = Solver(LEVEL_SET[2]).solve()
        g.unlocked = 2
        g.goto_level(2)
        play(g, moves[:10])
        r = Game.from_save(json.loads(json.dumps(g.to_save())))
        play(r, moves[10:])
        self.assertTrue(r.is_solved())

    def test_corrupt_or_incompatible_is_none(self):
        good = self.advanced().to_save()
        self.assertIsNotNone(Game.from_save(good))
        self.assertIsNone(Game.from_save(None))
        self.assertIsNone(Game.from_save({}))
        self.assertIsNone(Game.from_save("junk"))
        self.assertIsNone(Game.from_save(dict(good, version=sokoban.SAVE_VERSION + 1)))
        self.assertIsNone(Game.from_save(dict(good, levels=5)))
        self.assertIsNone(Game.from_save(dict(good, level=5)))      # not unlocked
        self.assertIsNone(Game.from_save(dict(good, unlocked=999)))
        self.assertIsNone(Game.from_save(dict(good, unlocked=-1)))
        self.assertIsNone(Game.from_save(dict(good, history="xyz")))
        self.assertIsNone(Game.from_save(dict(good, history="LLLLLLLL")))  # no replay
        self.assertIsNone(Game.from_save(dict(good, redo=7)))
        self.assertIsNone(Game.from_save(dict(good, bests={"99": [1, 1]})))
        self.assertIsNone(Game.from_save(dict(good, bests={"0": [1, 5]})))
        self.assertIsNone(Game.from_save(dict(good, bests={"0": "x"})))
        self.assertIsNone(Game.from_save(dict(good, level_time=-1)))
        bad = dict(good)
        del bad["history"]
        self.assertIsNone(Game.from_save(bad))

    def test_phase_resolved_before_save(self):
        g = Game()
        play(g, Solver(LEVEL_SET[0]).solve())
        self.assertIsNotNone(g.phase)
        g.resolve_phases()
        r = Game.from_save(json.loads(json.dumps(g.to_save())))
        self.assertEqual((r.index, r.moves, r.unlocked), (1, 0, 1))
        self.assertIsNone(r.phase)


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------

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
        self.assertEqual(ch, sokoban.controls_height(sokoban.CONTROLS, 30))
        self.assertGreaterEqual(ah, sokoban.AGENTS_MIN_H)
        self.assertIsNone(plan["hint"])

    def test_no_agents_tier(self):
        plan = layout_plan(80, 24)
        self.assertEqual(plan["tier"], "stats")
        self.assertIsNone(plan["agents"])
        self.assertIsNotNone(plan["stats"])
        self.assert_fits(plan, 80, 24)
        self.assert_centred(plan, 80, 24)

    def test_no_side_column_tier(self):
        plan = layout_plan(50, 24)
        self.assertEqual(plan["tier"], "field")
        self.assertIsNone(plan["stats"])
        self.assertIsNone(plan["controls"])
        self.assertEqual(plan["hint"], (23, 0, 1, 50))
        self.assert_fits(plan, 50, 24)
        self.assert_centred(plan, 50, 24)
        self.assertIsNone(layout_plan(40, 15)["hint"])  # header carries ? help

    def test_too_small_tier(self):
        self.assertTrue(layout_plan(sokoban.MIN_COLS - 1, 28)["too_small"])
        self.assertTrue(layout_plan(93, sokoban.MIN_ROWS - 1)["too_small"])
        self.assertFalse(layout_plan(sokoban.MIN_COLS, sokoban.MIN_ROWS)["too_small"])

    def test_degrading_tiers_sweep(self):
        self.assertEqual(layout_plan(60, 16)["tier"], "controls")
        for cols in range(20, 120, 3):
            for rows in range(8, 40):
                plan = layout_plan(cols, rows)
                if not plan["too_small"]:
                    self.assert_fits(plan, cols, rows)
                    self.assert_centred(plan, cols, rows)
                    if plan["controls"] is None:
                        self.assertEqual(plan["tier"], "field")

    def test_levels_centred_in_field(self):
        for lvl in LEVEL_SET:
            ox, oy = sokoban.level_origin(lvl)
            self.assertGreaterEqual(ox, 0)
            self.assertGreaterEqual(oy, 0)
            self.assertLessEqual(abs(ox - (FIELD_W - lvl.width - ox)), 1)
            self.assertLessEqual(abs(oy - (FIELD_H - lvl.height - oy)), 1)
            # Banner rows sit inside the frame, clear of the level.
            above, below = sokoban.banner_rows(lvl)
            self.assertTrue(1 <= above < 1 + oy, lvl.number)
            self.assertTrue(1 + oy + lvl.height <= below <= FIELD_H, lvl.number)

    def test_hint_leads_with_help(self):
        for w in (12, 28, 40, 200):
            text = sokoban.hint_text(w)
            self.assertIn("? help", text)
            self.assertLess(len(text), w)


class ConstantsTest(unittest.TestCase):
    def test_controls_cover_keys(self):
        keys = " ".join(k for k, _ in sokoban.CONTROLS)
        for k in ("?", "q", "p", "r", "arrows", "u", "z", "y", "[", "]", "x",
                  "w", "a", "s", "d", "h", "j", "k", "l"):
            self.assertIn(k, keys)
        self.assertEqual(sokoban.CONTROLS[-1], ("q", "menu"))
        for k, _ in sokoban.CONTROLS:
            self.assertLessEqual(len(k), 9)   # never truncated in the box

    def test_help_sections(self):
        help = dict(sokoban.HELP)
        for want in ("Goal", "Controls", "Rules", "Scoring", "Tips", "Legend",
                     "Progress and records"):
            self.assertIn(want, help)
        for head, body in sokoban.HELP:
            self.assertTrue(body.strip(), head)
            body.encode("ascii")
        for glyph in (sokoban.CRATE_GLYPH, sokoban.GOAL_GLYPH, sokoban.DONE_GLYPH,
                      sokoban.KEEPER_GLYPH):
            self.assertIn(glyph, help["Legend"])
        self.assertIn("1000", help["Scoring"])
        self.assertIn("r: restart the level", help["Controls"])
        self.assertIn("x: new game", help["Controls"])

    def test_glyphs_distinct_and_two_columns(self):
        glyphs = (sokoban.WALL_GLYPH, sokoban.CRATE_GLYPH, sokoban.GOAL_GLYPH,
                  sokoban.DONE_GLYPH, sokoban.KEEPER_GLYPH)
        self.assertEqual(len(set(glyphs)), len(glyphs))
        for g in glyphs:
            self.assertEqual(len(g), 2)
        pairs = (sokoban.PAIR_CRATE, sokoban.PAIR_GOAL, sokoban.PAIR_DONE)
        self.assertEqual(len(set(pairs)), 3)
        self.assertTrue(all(40 <= p < 60 for p in pairs))


# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------

class HelpKeyTest(unittest.TestCase):
    def setUp(self):
        self.game = new_game()
        self.help = sokoban.HelpOverlay(sokoban.TITLE, sokoban.HELP)
        self.st = sokoban.LoopState()
        self.session = types.SimpleNamespace(attention=None, dismiss=lambda: None)

    def key(self, ch):
        if isinstance(ch, str):
            ch = ord(ch)
        return sokoban.dispatch_key(ch, self.game, self.help, self.st, self.session)

    def test_help_pauses_and_closing_resumes(self):
        self.key("?")
        self.assertTrue(self.help.is_open)
        self.assertTrue(self.game.paused)
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

    def test_resume_banner_survives_help(self):
        st = self.st = sokoban.LoopState(resume=True)
        self.game.paused = True
        self.key("?")
        self.key("x")
        self.assertTrue(self.game.paused)      # banner still up
        self.key("l")                          # dismisses the banner only
        self.assertFalse(st.resume)
        self.assertFalse(self.game.paused)
        self.assertEqual(self.game.moves, 0)

    def test_attention_banner_consumes_key(self):
        dismissed = []
        self.session = types.SimpleNamespace(attention=("a", "b"),
                                             dismiss=lambda: dismissed.append(1))
        self.game.paused = True
        self.st.paused_by_attention = True
        self.key("l")
        self.assertEqual(dismissed, [1])
        self.assertFalse(self.game.paused)
        self.assertEqual(self.game.moves, 0)

    def test_game_keys(self):
        self.assertEqual(self.key("r"), "restart")
        self.key("l")
        self.assertTrue(self.game.is_solved())
        self.key("]")                          # skips the solved banner
        self.assertEqual(self.game.index, 1)
        self.key("[")
        self.assertEqual(self.game.index, 0)
        self.key("n")
        self.assertEqual(self.game.index, 1)
        self.key("d")                          # wasd: right
        self.assertEqual(self.game.moves, 1)
        self.key("u")
        self.assertEqual(self.game.moves, 0)
        self.key("y")
        self.assertEqual(self.game.moves, 1)
        self.key("z")
        self.assertEqual(self.game.moves, 0)
        self.key("p")
        self.assertTrue(self.game.paused)
        self.key("l")
        self.assertEqual(self.game.moves, 0)   # paused: ignored
        self.assertEqual(self.key("q"), "quit")

    def test_undecoded_escape_sequences_are_swallowed(self):
        def feed(text):
            keys = [ord(c) for c in text]
            return lambda: keys.pop(0) if keys else -1
        get = feed("\x1b[D\x1b[I\x1bOA\x1b[1;5C]")
        curses = sokoban.curses
        self.assertEqual(sokoban.read_key(get), curses.KEY_LEFT)  # ESC [ D
        self.assertEqual(sokoban.read_key(get), -1)           # focus report
        self.assertEqual(sokoban.read_key(get), curses.KEY_UP)    # ESC O A
        self.assertEqual(sokoban.read_key(get), -1)           # ctrl+right
        self.assertEqual(sokoban.read_key(get), ord("]"))     # a real key
        self.assertEqual(sokoban.read_key(get), -1)
        self.assertEqual(sokoban.read_key(feed("\x1b")), 27)  # lone ESC
        self.assertEqual(sokoban.read_key(feed("\x1bn")), ord("n"))
        self.assertEqual(sokoban.read_key(feed("[")), ord("["))

    def test_new_game_asks_first(self):
        self.assertIsNone(self.key("x"))
        self.assertTrue(self.st.confirm)
        self.assertIsNone(self.key("l"))       # cancels, and does not move
        self.assertFalse(self.st.confirm)
        self.assertEqual(self.game.moves, 0)
        self.key("x")
        self.assertIsNone(self.key("n"))
        self.key("x")
        self.assertEqual(self.key("y"), "new")
        self.key("x")
        self.assertEqual(self.key("q"), "quit")  # q still leaves


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

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
            raise sokoban.curses.error()
        child = FakeWin(h, w, self.root, self.y0 + y, self.x0 + x)
        self.root.children.append(child)
        return child

    def erase(self):
        self.writes = []

    def addstr(self, y, x, text, attr=0):
        if y < 0 or y >= self.rows or x < 0 or x + len(text) > self.cols:
            self.root.violations.append((y, x, text))
            raise sokoban.curses.error()
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

    def render(self, game, cols=93, rows=28, attention=None, resume=False, help=False,
               confirm=False):
        scr = FakeWin(rows, cols)
        overlay = sokoban.HelpOverlay(sokoban.TITLE, sokoban.HELP)
        if help:
            overlay.open()
        with mock.patch("curses.color_pair", return_value=0):
            lay = sokoban.Layout(scr)
            sokoban.draw(game, lay, self.session(attention), resume, 1234, overlay,
                         confirm)
        return scr, lay

    def games(self):
        yield "fresh", Game()
        big = Game()
        big.unlocked = len(LEVEL_SET) - 1
        for i in range(len(LEVEL_SET)):
            big.goto_level(i)
            yield "level-%d" % (i + 1), big
        s = new_game()
        s.move(RIGHT)
        yield "solved", s
        a = new_game()
        for moves in ("R", "rR", "rrR"):
            play(a, moves)
            a.resolve_phases()
        yield "all-solved", a
        p = Game()
        p.paused = True
        yield "paused", p

    def test_full_frame_has_no_out_of_bounds_writes(self):
        attention = (AgentSnapshot("p1", "a very long agent label indeed", "omp",
                                   "blocked"), "working")
        for name, game in self.games():
            for size in ((93, 28), (80, 24), (60, 20), (50, 24), (40, 15), (10, 3)):
                for att, res in ((None, False), (attention, False), (None, True)):
                    scr, lay = self.render(game, *size, attention=att, resume=res)
                    self.assertEqual(scr.violations, [], (name, size))
            scr, _ = self.render(game, confirm=True)
            self.assertEqual(scr.violations, [], name)

    def test_field_contents(self):
        g = Game()
        g.unlocked = 5
        g.goto_level(5)
        scr, lay = self.render(g)
        self.assertEqual(lay.plan["tier"], "full")
        field = lay.field.writes
        text = "".join(t for _, _, t in field)
        self.assertIn(sokoban.WALL_GLYPH, text)
        self.assertEqual(text.count(sokoban.CRATE_GLYPH), len(g.crates - g.level.goals))
        self.assertEqual(text.count(sokoban.GOAL_GLYPH), len(g.level.goals - g.crates))
        self.assertEqual(text.count(sokoban.KEEPER_GLYPH), 1)
        ox, oy = sokoban.level_origin(g.level)
        px, py = g.player
        self.assertIn((1 + oy + py, 1 + 2 * (ox + px), sokoban.KEEPER_GLYPH), field)
        # Crates on goals get their own glyph.
        s = new_game()
        s.move(RIGHT)
        _, lay = self.render(s)
        text = "".join(t for _, _, t in lay.field.writes)
        self.assertIn(sokoban.DONE_GLYPH, text)
        self.assertNotIn(sokoban.CRATE_GLYPH, text)
        self.assertIn("LEVEL 1 SOLVED", text)

    def test_stats_show_live_counters(self):
        g = Game()
        play(g, "rr")
        g.bests = {0: [3, 1]}
        _, lay = self.render(g)
        text = "\n".join(t for _, _, t in lay.stats.writes)
        for want in ("level  1/%d" % len(LEVEL_SET), "moves  2", "pushes 1",
                     "best   3/1", "score", "solved 1/"):
            self.assertIn(want, text)
        _, lay = self.render(g, 50, 24)                # no stats box: header
        scr, _ = self.render(g, 50, 24)
        head = "".join(t for y, _, t in scr.writes if y == 0)
        self.assertIn("moves 2", head)
        self.assertIn("pushes 1", head)

    def test_confirm_prompt(self):
        _, lay = self.render(Game(), confirm=True)
        text = "".join(t for _, _, t in lay.field.writes)
        self.assertIn("NEW GAME?", text)

    def test_controls_visible_and_help_overlay(self):
        for cols, rows in ((93, 28), (80, 24), (60, 16), (50, 24), (40, 15)):
            for help in (False, True):
                scr, lay = self.render(Game(), cols, rows, help=help)
                self.assertEqual(scr.violations, [], (cols, rows, help))
                if help:
                    text = "".join(t for c in scr.children for _, _, t in c.writes)
                    self.assertIn("Sokoban - help", text, (cols, rows))
                    continue
                if lay.controls is not None:
                    keys = "\n".join(t for _, _, t in lay.controls.writes)
                    for want in ("walk", "undo", "redo", "restart level", "help",
                                 "pause", "menu"):
                        self.assertIn(want, keys, (cols, rows))
                elif lay.plan["hint"] is not None:
                    hint = [t for y, _, t in scr.writes if y == rows - 1]
                    self.assertTrue(any("? help" in t for t in hint), (cols, rows))
                else:
                    head = [t for y, _, t in scr.writes if y == 0]
                    self.assertTrue(any("? help" in t for t in head), (cols, rows))

    def test_too_small_message(self):
        scr, lay = self.render(Game(), sokoban.MIN_COLS - 1, 20)
        self.assertTrue(lay.too_small)
        text = " ".join(t for _, _, t in scr.writes)
        self.assertIn("pane too small", text)


if __name__ == "__main__":
    unittest.main()
