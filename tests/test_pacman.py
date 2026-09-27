"""Unit tests for the pure Pac-Man logic in pacman.py (no terminal needed)."""

import json
import os
import random
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pacman
from common import AgentSnapshot
from pacman import (
    ACTIVE,
    DIR_ORDER,
    DOWN,
    EYES,
    FRIGHT_GLYPH,
    GHOST_NAMES,
    HOUSE,
    HOUSE_CENTER,
    HOUSE_EXIT,
    LEAVING,
    LEFT,
    MAZE,
    MAZE_COLS,
    MAZE_ROWS,
    MODE_SCHEDULE,
    OPPOSITE,
    RIGHT,
    SCATTER_TARGETS,
    UP,
    Game,
    Phase,
    fright_duration,
    ghost_speed,
    layout_plan,
    neighbour,
    pac_can_enter,
    pac_speed,
)


def new_game(seed=0):
    return Game(random.Random(seed), ready=False)


def park_ghosts(game):
    """Lock every ghost in the house so pac-only tests are undisturbed."""
    cx, cy = HOUSE_CENTER
    for i, g in enumerate(game.ghosts):
        g.x, g.y, g.mode, g.frightened = cx - 1 + i % 3, cy, HOUSE, False
    game.round_time = -1e9
    game.round_dots = -10**9


def place_pac(game, x, y, d):
    game.pac_x, game.pac_y, game.pac_dir = x, y, d
    game.pac_want = None


def chase(game):
    game.mode_index = 1  # odd index: chase


class MazeTest(unittest.TestCase):
    def test_dimensions_fit_the_budget(self):
        self.assertLessEqual(MAZE_ROWS, 21)
        self.assertLessEqual(MAZE_COLS, 19)
        for row in MAZE:
            self.assertEqual(len(row), MAZE_COLS)
        # 1 header row + framed maze fits 24 rows; the maze window leaves
        # >= 36 columns of 77 for the side boxes.
        self.assertLessEqual(1 + MAZE_ROWS + 2, 24)
        self.assertLessEqual(MAZE_COLS * 2 + 2, 40)

    def test_symmetric_with_four_pellets_and_house(self):
        for row in MAZE:
            self.assertEqual(row, row[::-1])
        self.assertEqual(len(pacman.PELLET_CELLS), 4)
        self.assertEqual(MAZE[HOUSE_EXIT[1] + 1][HOUSE_EXIT[0]], "=")
        self.assertEqual(MAZE[HOUSE_CENTER[1]][HOUSE_CENTER[0]], "H")

    def test_tunnel_rows_wrap(self):
        self.assertTrue(pacman.WRAP_ROWS)
        for y in pacman.WRAP_ROWS:
            self.assertEqual(neighbour(0, y, LEFT), (MAZE_COLS - 1, y))
            self.assertEqual(neighbour(MAZE_COLS - 1, y, RIGHT), (0, y))
        self.assertIsNone(neighbour(0, 1, LEFT))  # non-tunnel edge

    def test_every_corridor_reachable_and_no_dead_ends(self):
        walk = {(x, y) for y in range(MAZE_ROWS) for x in range(MAZE_COLS)
                if pac_can_enter((x, y))}
        seen = {pacman.PAC_START}
        todo = [pacman.PAC_START]
        while todo:
            c = todo.pop()
            for d in DIR_ORDER:
                n = neighbour(*c, d)
                if pac_can_enter(n) and n not in seen:
                    seen.add(n)
                    todo.append(n)
        self.assertEqual(walk, seen)
        for c in walk:
            exits = sum(pac_can_enter(neighbour(*c, d)) for d in DIR_ORDER)
            self.assertGreaterEqual(exits, 2, c)


class PacMovementTest(unittest.TestCase):
    def setUp(self):
        self.g = new_game()
        park_ghosts(self.g)

    def test_eats_dot_and_pellet(self):
        g = self.g
        place_pac(g, 2, 1, RIGHT)
        self.assertIn((3, 1), g.dots)
        g._pac_step()
        self.assertEqual(g.pac_pos, (3, 1))
        self.assertNotIn((3, 1), g.dots)
        self.assertEqual(g.score, 10)
        place_pac(g, 1, 3, UP)
        g._pac_step()  # (1, 2) is a power pellet
        self.assertEqual(g.pac_pos, (1, 2))
        self.assertEqual(g.score, 60)
        self.assertGreater(g.fright_timer, 0)

    def test_buffered_turn_applies_at_the_corner(self):
        g = self.g
        place_pac(g, 2, 3, RIGHT)
        g.set_direction(UP)  # pressed early: (2,2) and (3,2) are walls
        g._pac_step()
        self.assertEqual((g.pac_pos, g.pac_dir), ((3, 3), RIGHT))
        g._pac_step()
        self.assertEqual((g.pac_pos, g.pac_dir), ((4, 3), RIGHT))
        g._pac_step()  # (4,2) is open: the buffered turn applies
        self.assertEqual((g.pac_pos, g.pac_dir), ((4, 2), UP))

    def test_walls_stop_pac(self):
        g = self.g
        place_pac(g, 1, 1, UP)
        self.assertFalse(g._pac_step())
        self.assertEqual(g.pac_pos, (1, 1))
        self.assertFalse(g.pac_moving)

    def test_pac_cannot_enter_ghost_house(self):
        g = self.g
        place_pac(g, *HOUSE_EXIT, DOWN)
        self.assertFalse(g._pac_step())
        self.assertEqual(g.pac_pos, HOUSE_EXIT)

    def test_tunnel_wraps(self):
        g = self.g
        y = next(iter(pacman.WRAP_ROWS))
        place_pac(g, 0, y, LEFT)
        g._pac_step()
        self.assertEqual(g.pac_pos, (MAZE_COLS - 1, y))
        g.pac_dir = RIGHT
        g._pac_step()
        self.assertEqual(g.pac_pos, (0, y))

    def test_fixed_cell_rate(self):
        g = self.g
        place_pac(g, 1, 19, RIGHT)
        g.pac_timer = 0.0
        g.update(1.0)
        moved = g.pac_x - 1
        self.assertIn(moved, (int(pac_speed(1)), int(pac_speed(1)) + 1))
        self.assertGreater(pac_speed(3), pac_speed(1))


class GhostTest(unittest.TestCase):
    def setUp(self):
        self.g = new_game()
        chase(self.g)

    def test_blinky_targets_pac(self):
        place_pac(self.g, 5, 13, LEFT)
        self.assertEqual(self.g.ghost_target(self.g.ghost("blinky")), (5, 13))

    def test_pinky_targets_four_ahead(self):
        place_pac(self.g, 5, 13, RIGHT)
        self.assertEqual(self.g.ghost_target(self.g.ghost("pinky")), (9, 13))
        self.g.pac_dir = UP
        self.assertEqual(self.g.ghost_target(self.g.ghost("pinky")), (5, 9))

    def test_inky_doubles_blinky_vector(self):
        place_pac(self.g, 5, 13, RIGHT)
        b = self.g.ghost("blinky")
        b.x, b.y = 3, 10
        # pivot (7,13); target = pivot + (pivot - blinky) = (11, 16)
        self.assertEqual(self.g.ghost_target(self.g.ghost("inky")), (11, 16))

    def test_clyde_chases_far_scatters_near(self):
        c = self.g.ghost("clyde")
        c.x, c.y, c.mode = 1, 1, ACTIVE
        place_pac(self.g, 17, 19, LEFT)
        self.assertEqual(self.g.ghost_target(c), (17, 19))
        place_pac(self.g, 4, 3, LEFT)
        self.assertEqual(self.g.ghost_target(c), SCATTER_TARGETS["clyde"])

    def test_scatter_targets_corners(self):
        self.g.mode_index = 0
        for g in self.g.ghosts:
            self.assertEqual(self.g.ghost_target(g), SCATTER_TARGETS[g.name])

    def test_min_distance_with_classic_tie_break(self):
        b = self.g.ghost("blinky")
        b.x, b.y, b.dir = 4, 3, RIGHT  # junction: up, down, right (left = reverse)
        with mock.patch.object(Game, "ghost_target", return_value=(4, 3)):
            self.assertEqual(self.g._choose_dir(b), UP)  # all tie: up first
        with mock.patch.object(Game, "ghost_target", return_value=(5, 4)):
            self.assertEqual(self.g._choose_dir(b), DOWN)  # down/right tie
        with mock.patch.object(Game, "ghost_target", return_value=(0, 3)):
            # the target is behind, but reversing is not allowed
            self.assertNotEqual(self.g._choose_dir(b), LEFT)

    def test_no_reverse_except_on_mode_change(self):
        g = self.g
        park_ghosts(g)
        b = g.ghost("blinky")
        b.x, b.y, b.dir, b.mode = 1, 19, RIGHT, ACTIVE
        for i in range(400):
            place_pac(g, *[(1, 1), (17, 19), (9, 3), (1, 13)][i // 100], LEFT)
            before = b.dir
            self.assertTrue(g._ghost_step(b))
            self.assertNotEqual(b.dir, OPPOSITE[before])
        g.mode_index, g.mode_timer = 0, 0.0
        before = b.dir
        g._advance_mode(MODE_SCHEDULE[0])
        self.assertEqual(g.mode_index, 1)
        self.assertEqual(b.dir, OPPOSITE[before])

    def test_active_ghost_cannot_enter_door_but_eyes_can(self):
        b = self.g.ghost("blinky")
        door = neighbour(*HOUSE_EXIT, DOWN)
        b.mode = ACTIVE
        self.assertFalse(b.can_enter(door))
        b.mode = EYES
        self.assertTrue(b.can_enter(door))

    def test_eyes_return_home_and_re_emerge(self):
        g = self.g
        park_ghosts(g)
        b = g.ghost("blinky")
        b.x, b.y, b.dir, b.mode = 17, 19, LEFT, EYES
        seen_center = False
        for _ in range(200):
            g._ghost_step(b)
            seen_center = seen_center or b.pos == HOUSE_CENTER
            if b.mode == ACTIVE:
                break
        self.assertTrue(seen_center)
        self.assertEqual((b.mode, b.pos, b.frightened), (ACTIVE, HOUSE_EXIT, False))

    def test_staggered_release(self):
        g = new_game()
        place_pac(g, 9, 19, DOWN)  # parked against a wall, far from scatter corners
        g.update(0.1)
        self.assertNotEqual(g.ghost("pinky").mode, HOUSE)
        self.assertEqual(g.ghost("inky").mode, HOUSE)
        self.assertEqual(g.ghost("clyde").mode, HOUSE)
        g.update(pacman.RELEASE_TIMES["inky"])
        self.assertIsNone(g.phase)
        self.assertNotEqual(g.ghost("inky").mode, HOUSE)
        self.assertEqual(g.ghost("clyde").mode, HOUSE)
        g2 = new_game()
        g2.round_dots = pacman.release_dots("inky", 1)
        g2.update(pacman.RELEASE_MIN_TIMES["inky"] + 0.05)
        self.assertNotEqual(g2.ghost("inky").mode, HOUSE)  # by dot count
        self.assertEqual(g2.ghost("clyde").mode, HOUSE)

    def test_release_stays_staggered_at_high_levels(self):
        g = new_game()
        g.level = 5  # dot thresholds are all zero here
        place_pac(g, 9, 19, DOWN)
        g.update(0.5)
        self.assertEqual([g.ghost(n).mode == HOUSE for n in ("pinky", "inky", "clyde")],
                         [False, True, True])
        g.update(1.0)
        self.assertEqual(g.ghost("inky").mode != HOUSE, True)
        self.assertEqual(g.ghost("clyde").mode, HOUSE)
        g.update(1.0)
        self.assertNotEqual(g.ghost("clyde").mode, HOUSE)


class FrightTest(unittest.TestCase):
    def setUp(self):
        self.g = new_game()
        park_ghosts(self.g)

    def test_pellet_frightens_reverses_and_slows(self):
        g = self.g
        b = g.ghost("blinky")
        b.x, b.y, b.dir, b.mode = 1, 19, RIGHT, ACTIVE
        normal = g.ghost_interval(b)
        g._start_fright()
        self.assertTrue(all(x.frightened for x in g.ghosts))
        self.assertEqual(b.dir, LEFT)
        self.assertGreater(g.ghost_interval(b), normal)
        self.assertEqual(g.fright_timer, fright_duration(1))
        g.update(fright_duration(1) + 0.05)
        self.assertFalse(any(x.frightened for x in g.ghosts))

    def test_eating_chain_200_400_800_1600(self):
        g = self.g
        g._start_fright()
        for i, gh in enumerate(g.ghosts):
            gh.mode, gh.frightened = ACTIVE, True
        scores = []
        for gh in g.ghosts:
            before = g.score
            g._touch(gh)
            scores.append(g.score - before)
            self.assertEqual(gh.mode, EYES)
        self.assertEqual(scores, [200, 400, 800, 1600])
        self.assertIsNone(g.phase)  # nobody died
        g._start_fright()  # a new pellet restarts the chain
        gh = g.ghost("blinky")
        gh.mode, gh.frightened = ACTIVE, True
        before = g.score
        g._touch(gh)
        self.assertEqual(g.score - before, 200)


class CollisionTest(unittest.TestCase):
    def setUp(self):
        self.g = new_game()
        park_ghosts(self.g)
        chase(self.g)
        self.b = self.g.ghost("blinky")
        self.b.mode = ACTIVE

    def run_until_phase(self, limit=3.0):
        t = 0.0
        while self.g.phase is None and t < limit:
            self.g.update(0.01)
            t += 0.01

    def test_ghost_moving_onto_pac_kills(self):
        place_pac(self.g, 1, 1, UP)  # against a wall: stationary
        self.b.x, self.b.y, self.b.dir = 4, 1, LEFT
        self.run_until_phase()
        self.assertEqual(self.g.phase.kind, "dying")

    def test_pac_moving_onto_ghost_kills(self):
        place_pac(self.g, 1, 19, RIGHT)
        self.b.x, self.b.y, self.b.dir = 3, 19, RIGHT
        self.b.timer = -100.0  # ghost frozen
        self.run_until_phase()
        self.assertEqual(self.g.phase.kind, "dying")

    def test_swap_in_same_tick_kills(self):
        g = self.g
        place_pac(g, 5, 19, RIGHT)
        self.b.x, self.b.y, self.b.dir = 6, 19, LEFT
        # both are due to move within the very same tick: they would pass
        # through each other without a swap-aware check
        g.pac_timer = 1.0 / pac_speed(g.level)
        self.b.timer = g.ghost_interval(self.b)
        g.update(0.001)
        self.assertIsNotNone(g.phase)
        self.assertEqual(g.phase.kind, "dying")

    def test_frightened_swap_eats_instead(self):
        g = self.g
        place_pac(g, 5, 19, RIGHT)
        self.b.x, self.b.y, self.b.dir = 6, 19, LEFT
        g._start_fright()
        self.b.dir = LEFT
        g.pac_timer = 1.0 / pac_speed(g.level)
        self.b.timer = g.ghost_interval(self.b)
        g.update(0.001)
        self.assertIsNone(g.phase)
        self.assertEqual(self.b.mode, EYES)
        self.assertEqual(g.score, 10 + 200)

    def test_death_phase_decrements_lives_and_resets(self):
        g = self.g
        eaten = len(pacman.DOT_CELLS) - len(g.dots)
        place_pac(g, 1, 1, UP)
        self.b.x, self.b.y = 1, 1
        g._touch(self.b)
        self.assertEqual(g.phase.kind, "dying")
        g.update(pacman.DEATH_TIME / 2)  # nothing moves while dying
        self.assertEqual((g.phase.kind, g.pac_pos, g.lives), ("dying", (1, 1), 3))
        g.update(pacman.DEATH_TIME / 2)
        self.assertEqual(g.lives, 2)
        self.assertEqual(g.pac_pos, pacman.PAC_START)
        self.assertEqual(g.ghost("blinky").pos, HOUSE_EXIT)
        self.assertEqual(g.ghost("pinky").mode, HOUSE)
        self.assertEqual(len(pacman.DOT_CELLS) - len(g.dots), eaten)
        self.assertEqual(g.phase.kind, "ready")
        g.update(pacman.READY_TIME)
        self.assertIsNone(g.phase)

    def test_game_over_at_zero_lives(self):
        g = self.g
        g.lives = 1
        g._touch(self.b)
        g.finish_phase()
        self.assertTrue(g.game_over)
        self.assertEqual(g.lives, 0)
        self.assertFalse(g.update(1.0))


class LevelTest(unittest.TestCase):
    def test_clearing_all_dots_advances_level(self):
        g = new_game()
        park_ghosts(g)
        g.dots = {(2, 1)}
        g.pellets = set()
        place_pac(g, 1, 1, RIGHT)
        g._pac_step()
        self.assertEqual(g.phase.kind, "level_clear")
        g.update(pacman.LEVEL_CLEAR_TIME)
        self.assertEqual(g.level, 2)
        self.assertEqual(g.dots, set(pacman.DOT_CELLS))
        self.assertEqual(g.pellets, set(pacman.PELLET_CELLS))
        self.assertEqual(g.pac_pos, pacman.PAC_START)
        self.assertLess(fright_duration(2), fright_duration(1))
        self.assertGreater(ghost_speed(2), ghost_speed(1))

    def test_extra_life_at_10000(self):
        g = new_game()
        g.score = 9995
        g._add_score(10)
        self.assertEqual(g.lives, 4)
        g._add_score(10000)
        self.assertEqual(g.lives, 4)


class SaveRestoreTest(unittest.TestCase):
    def played(self, seed=5):
        g = Game(random.Random(seed))
        for i in range(300):
            if i % 25 == 0:
                g.set_direction(DIR_ORDER[(i // 25) % 4])
            g.update(0.02)
        return g

    def test_round_trip(self):
        g = self.played()
        data = json.loads(json.dumps(g.to_save()))
        again = Game.from_save(data)
        self.assertIsNotNone(again)
        self.assertEqual(again.to_save(), data)

    def test_rng_restored_so_frightened_moves_match(self):
        g = new_game(11)
        for name, pos in zip(GHOST_NAMES, [(1, 19), (17, 19), (1, 1), (17, 1)]):
            gh = g.ghost(name)
            gh.x, gh.y, gh.mode, gh.dir = pos[0], pos[1], ACTIVE, LEFT
        place_pac(g, *pacman.PAC_START, UP)
        g.lives = 99
        g._start_fright()
        g.fright_timer = 100.0
        g.rng.random()  # advance past the seed state
        restored = Game.from_save(json.loads(json.dumps(g.to_save())))
        self.assertIsNotNone(restored)
        for _ in range(300):
            g.update(0.02)
            restored.update(0.02)
            self.assertEqual([x.pos for x in g.ghosts], [x.pos for x in restored.ghosts])
        self.assertEqual(g.rng.random(), restored.rng.random())

    def test_save_mid_death_resolves_the_phase(self):
        g = new_game()
        g._touch(g.ghost("blinky"))
        data = g.to_save()
        self.assertEqual(data["lives"], 2)
        self.assertEqual((data["pac"]["x"], data["pac"]["y"]), pacman.PAC_START)
        self.assertIsNone(g.phase)
        g.lives = 1
        g._touch(g.ghost("blinky"))
        self.assertTrue(g.to_save()["game_over"])

    def test_save_mid_level_clear_resolves_the_phase(self):
        g = new_game()
        g.phase = Phase("level_clear", 1.0)
        g.dots, g.pellets = set(), set()
        data = g.to_save()
        self.assertEqual(data["level"], 2)
        self.assertEqual(len(data["dots"]), len(pacman.DOT_CELLS))

    def test_corrupt_or_incompatible_returns_none(self):
        good = self.played().to_save()
        self.assertIsNotNone(Game.from_save(good))
        bad = [None, [], {}, "x", {"version": 999}]
        for mutate in (
            lambda d: d.update(version=pacman.SAVE_VERSION + 1),
            lambda d: d.update(rng=[1, 2, 3]),
            lambda d: d.update(dots=[[0, 0]]),
            lambda d: d.update(dots=[], pellets=[]),
            lambda d: d.update(lives=0),
            lambda d: d.update(score="lots"),
            lambda d: d["pac"].update(x=0, y=0),
            lambda d: d["pac"].update(dir="sideways"),
            lambda d: d["ghosts"].pop(),
            lambda d: d["ghosts"][0].update(x=0, y=0),
            lambda d: d["ghosts"][0].update(mode="dancing"),
            lambda d: d.pop("fright_timer"),
        ):
            d = json.loads(json.dumps(good))
            mutate(d)
            bad.append(d)
        for d in bad:
            self.assertIsNone(Game.from_save(d), d if not isinstance(d, dict) else list(d)[:3])


class LayoutPlanTest(unittest.TestCase):
    KEYS = ("maze", "stats", "controls", "agents", "hint")

    def rects(self, plan):
        return [plan[k] for k in self.KEYS if plan[k] is not None]

    def assert_fits(self, plan, cols, rows):
        rects = self.rects(plan)
        for y, x, h, w in rects:
            self.assertGreaterEqual(y, 0)
            self.assertGreaterEqual(x, 0)
            self.assertLessEqual(x + w, cols)
            self.assertLessEqual(y + h, rows)
        boxes = [plan[k] for k in ("maze", "stats", "controls", "agents")
                 if plan[k] is not None]
        for y, x, h, w in boxes:
            self.assertGreaterEqual(y, 1)  # row 0 is the header/banner
        for i, a in enumerate(rects):
            for b in rects[i + 1:]:
                overlap = (a[0] < b[0] + b[2] and b[0] < a[0] + a[2]
                           and a[1] < b[1] + b[3] and b[1] < a[1] + a[3])
                self.assertFalse(overlap, (a, b))

    def assert_centred(self, plan, cols, rows):
        """The composition (maze + side column) is centred horizontally and
        vertically in the rows below the header (above a bottom hint row)."""
        boxes = [plan[k] for k in ("maze", "stats", "controls", "agents")
                 if plan[k] is not None]
        left = min(x for _, x, _, _ in boxes)
        right = cols - max(x + w for _, x, _, w in boxes)
        top = min(y for y, _, _, _ in boxes) - 1
        bottom_edge = rows
        if plan["hint"] is not None and not plan["hint_in_header"]:
            bottom_edge = plan["hint"][0]
        bottom = bottom_edge - max(y + h for y, _, h, _ in boxes)
        self.assertLessEqual(abs(left - right), 1, (cols, rows, left, right))
        self.assertLessEqual(abs(top - bottom), 1, (cols, rows, top, bottom))

    def test_popup_size_93x28(self):
        plan = layout_plan(93, 28)
        self.assertEqual(plan["tier"], "full")
        self.assert_fits(plan, 93, 28)
        self.assert_centred(plan, 93, 28)
        self.assertIsNone(plan["hint"])
        my, mx, mh, mw = plan["maze"]
        self.assertEqual((mh, mw), (MAZE_ROWS + 2, MAZE_COLS * 2 + 2))
        sy, sx, sh, sw = plan["stats"]
        cy, cx, ch, cw = plan["controls"]
        ay, ax, ah, aw = plan["agents"]
        self.assertEqual({sw, cw, aw}, {30})
        self.assertEqual({sx, cx, ax}, {mx + mw + 1})
        # stats over controls over agents, down to the maze's bottom edge
        self.assertEqual((sy, cy, ay), (my, sy + sh, cy + ch))
        self.assertEqual(ay + ah, my + mh)
        self.assertEqual(ch, pacman.controls_height(pacman.CONTROLS, 30))

    def test_old_popup_77x24_and_every_size_in_bounds(self):
        plan = layout_plan(77, 24)
        self.assertEqual(plan["tier"], "full")
        self.assertEqual(plan["stats"][3], 30)
        for cols in range(pacman.MIN_COLS, 130):
            for rows in range(pacman.MIN_ROWS, 40):
                plan = layout_plan(cols, rows)
                self.assertFalse(plan["too_small"])
                self.assert_fits(plan, cols, rows)
                self.assert_centred(plan, cols, rows)
                self.assertIsNotNone(plan["controls"] or plan["hint"], (cols, rows))

    def test_degrading_tiers(self):
        # side column down to common.SIDE_MIN_W, then the maze alone
        self.assertEqual(layout_plan(61, 24)["tier"], "full")
        self.assertEqual(layout_plan(61, 24)["stats"][3], 20)
        plan = layout_plan(60, 24)
        self.assertEqual(plan["tier"], "maze")
        self.assertIsNone(plan["stats"])
        self.assertIsNone(plan["controls"])
        self.assertEqual(plan["hint"], (0, 0, 1, 60))  # no spare row: header
        self.assertTrue(plan["hint_in_header"])
        plan = layout_plan(50, 26)
        self.assertEqual(plan["hint"], (25, 0, 1, 50))  # last row
        self.assertFalse(plan["hint_in_header"])
        self.assertEqual(layout_plan(39, 24)["tier"], "too_small")
        self.assertEqual(layout_plan(77, 23)["tier"], "too_small")
        self.assertTrue(layout_plan(10, 5)["too_small"])

    def test_key_hint_always_has_help(self):
        full = pacman.key_hint(500)
        self.assertEqual(full, pacman.controls_hint(pacman.CONTROLS))
        for width in range(6, 80):
            text = pacman.key_hint(width)
            self.assertLessEqual(len(text), width)
            self.assertIn("? help", text)


class ConstantsTest(unittest.TestCase):
    def test_controls_cover_every_key(self):
        keys = " ".join(k for k, _ in pacman.CONTROLS)
        for k in ("?", "q", "p", "r", "arrows", "wasd", "hjkl"):
            self.assertIn(k, keys)
        self.assertEqual([k for k, _ in pacman.CONTROLS][-3:], ["?", "p / r", "q"])
        # every letter the handler steers with is listed
        for code in pacman.KEY_DIRS:
            if code < 256:
                self.assertIn(chr(code).lower(), keys)

    def test_help_sections(self):
        heads = [h for h, _ in pacman.HELP]
        for need in ("Goal", "Controls", "How it works", "Scoring", "Tips"):
            self.assertIn(need, heads)
        for h, body in pacman.HELP:
            self.assertTrue(body.strip(), h)
            body.encode("ascii")  # plain ASCII
        self.assertEqual(pacman.TITLE, "Pac-Man")


class HelpKeyTest(unittest.TestCase):
    def setUp(self):
        self.help = pacman.HelpOverlay(pacman.TITLE, pacman.HELP)
        self.game = new_game()

    def test_help_pauses_and_closing_resumes(self):
        consumed, by_help = pacman.route_help_key(self.help, self.game, ord("?"), False)
        self.assertTrue(consumed and by_help and self.help.is_open and self.game.paused)
        # scrolling keeps it open and consumed; the game stays frozen
        consumed, by_help = pacman.route_help_key(
            self.help, self.game, pacman.curses.KEY_DOWN, by_help)
        self.assertTrue(consumed and self.help.is_open and self.game.paused)
        before = (self.game.pac_pos, self.game.round_time)
        self.assertFalse(self.game.update(0.5))
        self.assertEqual(before, (self.game.pac_pos, self.game.round_time))
        # 'q' while open only closes the help (it is consumed)
        consumed, by_help = pacman.route_help_key(self.help, self.game, ord("q"), by_help)
        self.assertTrue(consumed)
        self.assertFalse(self.help.is_open or self.game.paused or by_help)

    def test_help_keeps_an_existing_pause(self):
        self.game.paused = True
        _, by_help = pacman.route_help_key(self.help, self.game, ord("?"), False)
        self.assertFalse(by_help)
        _, by_help = pacman.route_help_key(self.help, self.game, ord("x"), by_help)
        self.assertTrue(self.game.paused)  # the user's own pause survives

    def test_help_from_game_over_and_resize_passthrough(self):
        self.game.game_over = True
        consumed, by_help = pacman.route_help_key(self.help, self.game, ord("?"), False)
        self.assertTrue(consumed and self.help.is_open)
        self.assertFalse(by_help)
        consumed, _ = pacman.route_help_key(
            self.help, self.game, pacman.curses.KEY_RESIZE, by_help)
        self.assertFalse(consumed)  # the loop rebuilds the layout
        self.assertTrue(self.help.is_open)

    def test_other_keys_pass_through_when_closed(self):
        consumed, by_help = pacman.route_help_key(self.help, self.game, ord("p"), False)
        self.assertFalse(consumed or by_help or self.help.is_open)


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
            raise pacman.curses.error()
        child = FakeWin(h, w, self.root, self.y0 + y, self.x0 + x)
        self.root.children.append(child)
        return child

    def erase(self):
        self.writes = []

    def addstr(self, y, x, text, attr=0):
        if y < 0 or y >= self.rows or x < 0 or x + len(text) > self.cols:
            self.root.violations.append((y, x, text))
            raise pacman.curses.error()
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
               help=None):
        scr = FakeWin(rows, cols)
        with mock.patch("curses.color_pair", return_value=0):
            lay = pacman.Layout(scr)
            pacman.draw(game, lay, self.session(attention), resume, 1234, help)
        return scr, lay

    def text(self, scr):
        """Everything written, anywhere on screen (root and child windows)."""
        return "\n".join(t for w in [scr] + scr.children for _, _, t in w.writes)

    def games(self):
        base = new_game()
        yield "fresh", Game(random.Random(1))
        yield "base", base
        fr = new_game()
        for gh in fr.ghosts:
            gh.mode = ACTIVE
        fr._start_fright()
        fr.fright_timer = 1.0
        fr.ghost("inky").mode = EYES
        fr.popup = [MAZE_COLS - 1, 5, 1600, 0.5]
        yield "fright", fr
        dy = new_game()
        dy._touch(dy.ghost("blinky"))
        dy.phase.elapsed = 0.9
        yield "dying", dy
        lc = new_game()
        lc.phase = Phase("level_clear", 1.5)
        lc.phase.elapsed = 0.2
        yield "level_clear", lc
        go = new_game()
        go.game_over = True
        yield "game_over", go
        pa = new_game()
        pa.paused = True
        yield "paused", pa

    def test_full_frame_in_bounds_with_two_column_cells(self):
        for name, game in self.games():
            scr, lay = self.render(game)
            self.assertEqual(scr.violations, [], name)
            maze = lay.maze
            self.assertEqual((maze.y0, maze.x0), (1, 3))  # centred at 77x24
            cells = [(y, x, t) for y, x, t in maze.writes
                     if len(t) == 2 and 0 < y < maze.rows - 1]
            self.assertTrue(cells)
            for y, x, t in cells:
                self.assertEqual(x % 2, 1, (name, y, x, t))  # cell-aligned
                self.assertLessEqual(x + 2, maze.cols - 1)  # inside the frame
            for child in scr.children:
                self.assertLessEqual(child.x0 + child.cols, 77)
                self.assertLessEqual(child.y0 + child.rows, 24)

    def test_full_frame_contents(self):
        scr, lay = self.render(new_game())
        maze_text = [t for _, _, t in lay.maze.writes]
        self.assertIn(pacman.WALL, maze_text)
        self.assertIn(pacman.PAC_GLYPHS[LEFT], maze_text)
        self.assertEqual(maze_text.count(pacman.GHOST_GLYPH), 4)
        stats_text = " ".join(t for _, _, t in lay.stats.writes)
        self.assertIn("score 0", stats_text)
        self.assertIn("best  1234", stats_text)
        agents_text = " ".join(t for _, _, t in lay.agents.writes)
        self.assertIn("agents", agents_text)
        self.assertIn("more", agents_text)  # roster overflow is summarized
        fr = new_game()
        for gh in fr.ghosts:
            gh.mode = ACTIVE
        fr._start_fright()
        _, lay = self.render(fr)
        self.assertEqual([t for _, _, t in lay.maze.writes].count(FRIGHT_GLYPH), 4)

    def test_banners_and_game_over_overlay(self):
        agent = AgentSnapshot("p1", "claude", "claude", "blocked")
        scr, _ = self.render(new_game(), attention=(agent, "working"))
        self.assertIn("claude is blocked", scr.writes[0][2])
        scr, _ = self.render(new_game(), resume=True)
        self.assertIn("resumed", scr.writes[0][2])
        go = new_game()
        go.game_over = True
        scr, lay = self.render(go)
        text = " ".join(t for _, _, t in lay.maze.writes)
        self.assertIn("GAME OVER", text)
        self.assertIn("r restart | q menu", text)

    def test_smaller_tiers_and_too_small(self):
        for cols, rows in ((58, 24), (50, 24), (40, 24), (120, 40)):
            scr, lay = self.render(new_game(), cols, rows)
            self.assertEqual(scr.violations, [], (cols, rows))
        scr, lay = self.render(new_game(), 50, 24)
        self.assertIn("score 0", scr.writes[0][2])  # header carries the score
        self.assertIn("? help", scr.writes[0][2])  # ... and the key hint
        scr, lay = self.render(new_game(), 30, 10)
        self.assertEqual(scr.violations, [])
        text = " ".join(t for _, _, t in scr.writes)
        self.assertIn("pane too small", text)
        self.assertIn("need 40x24", text)
        self.assertIn("have 30x10", text)
        self.assertIn("q menu", text)

    def test_controls_box_and_help_overlay_at_every_tier(self):
        for cols, rows in ((93, 28), (77, 24), (62, 24), (50, 26), (50, 24),
                           (40, 24)):
            for help_open in (False, True):
                overlay = pacman.HelpOverlay(pacman.TITLE, pacman.HELP)
                if help_open:
                    overlay.open()
                    overlay.handle_key(pacman.curses.KEY_DOWN)
                scr, lay = self.render(new_game(), cols, rows, help=overlay)
                self.assertEqual(scr.violations, [], (cols, rows, help_open))
                text = self.text(scr)
                if lay.controls is not None:
                    self.assertIn("restart", text)
                    self.assertIn("keys", text)
                else:
                    self.assertIn("? help", text, (cols, rows))
                if help_open:
                    self.assertIn("Pac-Man - help", text, (cols, rows))
        scr, lay = self.render(new_game(), 93, 28)
        self.assertEqual(lay.controls.cols, 30)
        self.assertIn("wasd/hjkl steer", self.text(scr))
        # the too-small screen still shows the help when it fits
        overlay = pacman.HelpOverlay(pacman.TITLE, pacman.HELP)
        overlay.open()
        scr, _ = self.render(new_game(), 30, 12, help=overlay)
        self.assertEqual(scr.violations, [])
        self.assertIn("help", self.text(scr))


if __name__ == "__main__":
    unittest.main()
