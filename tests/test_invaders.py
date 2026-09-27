"""Unit tests for the pure game logic in invaders.py (no terminal needed)."""

import json
import os
import random
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import invaders
from invaders import (
    CANNON_Y,
    COL_PITCH,
    DYING,
    FIELD_H,
    FIELD_W,
    FORM_COLS,
    FORM_ROWS,
    FORMATION_W,
    Formation,
    Game,
    PLAY,
    Projectile,
    ROW_PITCH,
    SAVE_VERSION,
    SPRITE_W,
    Ufo,
    WAVE,
    layout_plan,
    march_interval,
)


def new_game(seed=1):
    return Game(random.Random(seed))


def quiet(game):
    """Stop timers that would fire on their own during a focused test."""
    game.bomb_timer = 1e9
    game.ufo_timer = 1e9
    game.march_timer = -1e9
    return game


def kill_all_but(game, keep):
    f = game.formation
    for r in range(FORM_ROWS):
        for c in range(FORM_COLS):
            f.alive[r][c] = (r, c) in keep


class FormationTest(unittest.TestCase):
    def test_initial_layout_and_bounds(self):
        g = new_game()
        f = g.formation
        self.assertEqual(f.count(), FORM_ROWS * FORM_COLS)
        left, right, top, bottom = f.bounds()
        self.assertEqual(right - left + 1, FORMATION_W)
        self.assertEqual(left, (FIELD_W - FORMATION_W) // 2)
        self.assertEqual(top, invaders.FORMATION_TOP)
        self.assertEqual(bottom, top + (FORM_ROWS - 1) * ROW_PITCH)
        self.assertLess(bottom, invaders.SHIELD_Y)
        self.assertEqual(f.at(left, top), (0, 0))
        self.assertEqual(f.at(left + SPRITE_W - 1, top), (0, 0))
        self.assertIsNone(f.at(left + SPRITE_W, top))  # gap column
        self.assertIsNone(f.at(left, top + 1))          # gap row

    def test_bounds_recompute_as_columns_die(self):
        f = new_game().formation
        left0, right0, _, _ = f.bounds()
        for r in range(FORM_ROWS):
            f.alive[r][0] = False
            f.alive[r][FORM_COLS - 1] = False
        left, right, _, _ = f.bounds()
        self.assertEqual(left, left0 + COL_PITCH)
        self.assertEqual(right, right0 - COL_PITCH)
        for r in range(1, FORM_ROWS):
            f.alive[r] = [False] * FORM_COLS
        _, _, top, bottom = f.bounds()
        self.assertEqual(top, bottom)

    def test_invader_types_score_30_20_10(self):
        self.assertEqual([invaders.KIND_POINTS[k] for k in invaders.ROW_KINDS],
                         [30, 20, 20, 10, 10])


class MarchTest(unittest.TestCase):
    def test_step_moves_sideways(self):
        g = quiet(new_game())
        x0, y0 = g.formation.x, g.formation.y
        g.march_step()
        self.assertEqual((g.formation.x, g.formation.y), (x0 + 1, y0))
        self.assertEqual(g.formation.frame, 1)

    def test_edge_drops_and_reverses(self):
        g = quiet(new_game())
        f = g.formation
        x0 = f.x
        steps = 0
        while f.dir == 1:
            g.march_step()
            steps += 1
        self.assertEqual(steps, (FIELD_W - FORMATION_W - x0) + 1)  # moves + drop
        _, right, top, _ = f.bounds()
        self.assertEqual(right, FIELD_W - 1)
        self.assertEqual(top, invaders.FORMATION_TOP + 1)
        g.march_step()
        self.assertEqual(f.bounds()[1], FIELD_W - 2)  # now marching left

    def test_narrower_formation_marches_further(self):
        g = quiet(new_game())
        for r in range(FORM_ROWS):
            g.formation.alive[r][FORM_COLS - 1] = False
        x0 = g.formation.x
        while g.formation.dir == 1:
            g.march_step()
        self.assertEqual(g.formation.x - x0,
                         (FIELD_W - 1) - (x0 + FORMATION_W - 1 - COL_PITCH))

    def test_speeds_up_as_invaders_die_and_per_wave(self):
        full = march_interval(40, 1)
        self.assertLess(march_interval(20, 1), full)
        self.assertLess(march_interval(1, 1), march_interval(20, 1))
        self.assertLess(march_interval(40, 3), full)
        self.assertGreaterEqual(march_interval(1, 20), invaders.MARCH_MIN)

    def test_update_marches_faster_with_fewer_invaders(self):
        def steps_in(game, secs):
            x0, y0 = game.formation.x, game.formation.y
            n = 0
            for _ in range(int(secs * 100)):
                before = (game.formation.x, game.formation.y)
                game.update(0.01)
                if (game.formation.x, game.formation.y) != before:
                    n += 1
            return n

        a = new_game(); a.bomb_timer = a.ufo_timer = 1e9
        b = new_game(); b.bomb_timer = b.ufo_timer = 1e9
        kill_all_but(b, {(4, 0), (4, 1)})
        self.assertGreater(steps_in(b, 2.0), steps_in(a, 2.0))

    def test_invaders_erase_shields_they_reach(self):
        g = quiet(new_game())
        kill_all_but(g, {(4, 0)})
        row = invaders.SHIELD_Y + 1  # the bunkers' full-width row
        g.formation.y = row - 4 * ROW_PITCH
        g.formation.x = 1
        g.formation.dir = 1
        before = len(g.shields)
        g.march_step()  # sideways onto the first bunker (x 2..7)
        self.assertEqual(g.formation.pos(4, 0), (2, row))
        self.assertEqual(len(g.shields), before - SPRITE_W)
        for dx in range(SPRITE_W):
            self.assertNotIn((2 + dx, row), g.shields)
        self.assertFalse(g.game_over)


class ShotTest(unittest.TestCase):
    def test_single_player_shot(self):
        g = quiet(new_game())
        self.assertTrue(g.fire())
        self.assertFalse(g.fire())
        g.shot = None
        self.assertTrue(g.fire())

    def test_shot_kills_invader_and_scores_per_type(self):
        for row, points in ((0, 30), (1, 20), (2, 20), (3, 10), (4, 10)):
            g = quiet(new_game())
            g.shields = set()
            kill_all_but(g, {(row, 3), (0, 0)})
            x, _ = g.formation.pos(row, 3)
            g.cannon_x = x
            g.fire()
            g.update(1.0)
            self.assertFalse(g.formation.alive[row][3], row)
            self.assertEqual(g.score, points, row)
            self.assertIsNone(g.shot)

    def test_bottom_invader_shields_the_ones_above(self):
        g = quiet(new_game())
        g.shields = set()
        x, _ = g.formation.pos(4, 2)
        g.cannon_x = x
        g.fire()
        g.update(1.0)
        self.assertFalse(g.formation.alive[4][2])
        self.assertTrue(g.formation.alive[3][2])
        self.assertEqual(g.score, 10)

    def test_no_tunnelling_at_high_speed(self):
        # One huge tick sweeps the whole column: the first target (a
        # one-row invader) must still be hit, not skipped over.
        g = quiet(new_game())
        kill_all_but(g, {(0, 5)})
        x, _ = g.formation.pos(0, 5)
        g.cannon_x = x
        g.shields = set()
        g.fire()
        with mock.patch.object(invaders, "SHOT_SPEED", 5000.0):
            g._advance_projectiles(0.01)  # 50 rows in one tick
        self.assertFalse(g.formation.alive[0][5])

    def test_shot_leaves_the_top(self):
        g = quiet(new_game())
        kill_all_but(g, {(0, 0)})
        g.shields = set()
        g.cannon_x = FIELD_W - 3
        g.fire()
        g.update(1.0)
        self.assertIsNone(g.shot)
        self.assertEqual(g.score, 0)

    def test_shot_erodes_shield(self):
        g = quiet(new_game())
        cell = min(g.shields, key=lambda c: (c[0], -c[1]))  # a bunker's left column
        x = cell[0]
        g.cannon_x = x - 1
        g.fire()
        lowest = max(y for cx, y in g.shields if cx == x)
        g.update(0.5)
        self.assertNotIn((x, lowest), g.shields)
        self.assertIsNone(g.shot)
        self.assertEqual(sum(1 for cx, _ in g.shields if cx == x),
                         sum(1 for cx, _ in invaders.shield_cells() if cx == x) - 1)

    def test_shot_cancels_bomb(self):
        for gap in (0, 1, 2, 5):  # adjacent / crossing / meeting mid-flight
            g = quiet(new_game())
            g.shields = set()
            g.cannon_x = 30
            g.fire()
            sx = g.shot.x
            g.bombs = [Projectile(sx, CANNON_Y - 1 - gap)]
            g.update(0.2)
            self.assertEqual(g.bombs, [], gap)
            self.assertIsNone(g.shot, gap)
            self.assertEqual(g.phase, PLAY)
            self.assertEqual(g.formation.count(), FORM_ROWS * FORM_COLS)

    def test_ufo_scores(self):
        g = quiet(new_game())
        g.shields = set()
        kill_all_but(g, {(4, 7)})
        g.formation.x = 0
        g.ufo = Ufo(10.0, 1)
        with mock.patch.object(invaders, "UFO_SPEED", 0.0):
            g.cannon_x = 11
            g.fire()
            for _ in range(100):
                g.update(0.01)
        self.assertIsNone(g.ufo)
        self.assertIn(g.score, invaders.UFO_SCORES)
        self.assertTrue(50 <= g.score <= 300)
        self.assertIsNotNone(g.ufo_popup)

    def test_ufo_crosses_and_leaves(self):
        g = new_game()
        g.bomb_timer = 1e9
        g.march_timer = -1e9
        g.ufo_timer = 0.001
        g.update(0.01)
        self.assertIsNotNone(g.ufo)
        for _ in range(1000):
            g.update(0.01)
            if g.ufo is None:
                break
        self.assertIsNone(g.ufo)


class BombTest(unittest.TestCase):
    def test_bombs_come_from_bottom_invader_and_are_capped(self):
        g = quiet(new_game(3))
        for _ in range(20):
            g._fire_bomb()
        self.assertEqual(len(g.bombs), invaders.bomb_cap(1))
        bottoms = {g.formation.pos(FORM_ROWS - 1, c)[0] + 1 for c in range(FORM_COLS)}
        for b in g.bombs:
            self.assertIn(b.x, bottoms)
            self.assertEqual(b.y, g.formation.pos(FORM_ROWS - 1, 0)[1] + 1)

    def test_bomb_erodes_shield(self):
        g = quiet(new_game())
        x, top = min(g.shields, key=lambda c: (c[1], c[0]))
        g.bombs = [Projectile(x, top - 3)]
        g.update(1.0)
        self.assertNotIn((x, top), g.shields)
        self.assertEqual(g.bombs, [])

    def test_cannon_hit_death_phase_then_respawn(self):
        g = quiet(new_game())
        g.shields = set()
        g.cannon_x = 5
        g.bombs = [Projectile(6, CANNON_Y - 4)]
        g.update(1.0)
        self.assertEqual(g.phase, DYING)
        self.assertEqual(g.lives, 3)  # lost when the phase completes
        self.assertFalse(g.fire())
        self.assertFalse(g.move(1))
        g.update(invaders.DEATH_DURATION / 2)
        self.assertEqual(g.phase, DYING)
        g.update(invaders.DEATH_DURATION)
        self.assertEqual(g.phase, PLAY)
        self.assertEqual(g.lives, 2)
        self.assertEqual(g.cannon_x, (FIELD_W - 3) // 2)
        self.assertEqual(g.bombs, [])
        self.assertFalse(g.game_over)

    def test_moving_into_a_bomb_on_the_cannon_row(self):
        g = quiet(new_game())
        g.cannon_x = 10
        g.bombs = [Projectile(14, CANNON_Y)]
        g.move(1)
        self.assertEqual(g.phase, DYING)

    def test_game_over_by_lives(self):
        g = quiet(new_game())
        g.lives = 1
        g.shields = set()
        g.bombs = [Projectile(g.cannon_x, CANNON_Y - 1)]
        g.update(0.5)
        self.assertEqual(g.phase, DYING)
        g.finish_phase()
        self.assertTrue(g.game_over)
        self.assertEqual(g.lives, 0)
        self.assertFalse(g.update(1.0))

    def test_game_over_by_invasion(self):
        g = quiet(new_game())
        kill_all_but(g, {(4, 0)})
        g.formation.y = CANNON_Y - 4 * ROW_PITCH - 1
        g.formation.x = 0
        g.formation.dir = -1
        g.march_step()
        self.assertTrue(g.game_over)
        self.assertTrue(g.invaded)

    def test_extra_life_at_1500_once(self):
        g = quiet(new_game())
        g.add_score(1499)
        self.assertEqual(g.lives, 3)
        g.add_score(1)
        self.assertEqual(g.lives, 4)
        g.add_score(5000)
        self.assertEqual(g.lives, 4)


class WaveTest(unittest.TestCase):
    def test_wave_clear_then_next_wave_lower(self):
        g = quiet(new_game())
        kill_all_but(g, {(2, 2)})
        g.shields = set()
        top1 = g.formation.bounds()[2]
        x, _ = g.formation.pos(2, 2)
        g.cannon_x = x
        g.fire()
        g.update(1.0)
        self.assertEqual(g.phase, WAVE)
        self.assertEqual(g.wave, 1)
        g.update(invaders.WAVE_DURATION + 0.01)
        self.assertEqual(g.phase, PLAY)
        self.assertEqual(g.wave, 2)
        self.assertEqual(g.formation.count(), FORM_ROWS * FORM_COLS)
        self.assertGreater(g.formation.y, invaders.FORMATION_TOP)
        self.assertEqual(g.shields, invaders.shield_cells())
        self.assertEqual(g.score, 20)
        self.assertLess(march_interval(40, 2), march_interval(40, 1))


class SaveTest(unittest.TestCase):
    def played(self, seed=5):
        g = new_game(seed)
        for i in range(300):
            if i % 7 == 0:
                g.fire()
            if i % 5 == 0:
                g.move(1 if (i // 40) % 2 else -1)
            g.update(0.02)
            if g.phase != PLAY:
                g.finish_phase()
        self.assertFalse(g.game_over)
        return g

    def test_round_trip_restores_every_field(self):
        g = self.played()
        g.formation.alive[0][0] = False
        g.ufo = Ufo(7.5, -1)
        data = json.loads(json.dumps(g.to_save()))
        r = Game.from_save(data)
        self.assertIsNotNone(r)
        self.assertEqual(r.to_save(), g.to_save())
        self.assertEqual(r.shields, g.shields)
        self.assertEqual(r.formation.alive, g.formation.alive)

    def test_rng_restored_so_bombs_repeat(self):
        g = self.played()
        r = Game.from_save(json.loads(json.dumps(g.to_save())))
        for _ in range(400):
            g.update(0.02)
            r.update(0.02)
            if g.phase != PLAY or r.phase != PLAY:
                g.finish_phase()
                r.finish_phase()
        self.assertEqual(r.to_save(), g.to_save())
        self.assertEqual([(b.x, b.y) for b in r.bombs], [(b.x, b.y) for b in g.bombs])
        self.assertEqual(g.rng.random(), r.rng.random())

    def test_rejects_wrong_version(self):
        data = new_game().to_save()
        data["version"] = SAVE_VERSION + 1
        self.assertIsNone(Game.from_save(data))

    def test_rejects_corrupt_saves(self):
        base = new_game().to_save()

        def mutated(fn):
            d = json.loads(json.dumps(base))
            fn(d)
            return d

        bad = [
            None, [], "x", {},
            mutated(lambda d: d.pop("rng")),
            mutated(lambda d: d.__setitem__("rng", [3, [1, 2], None])),
            mutated(lambda d: d.__setitem__("score", "10")),
            mutated(lambda d: d.__setitem__("cannon_x", 99)),
            mutated(lambda d: d["formation"].__setitem__("alive", [[True] * 3])),
            mutated(lambda d: d["formation"].__setitem__("dir", 0)),
            mutated(lambda d: d["formation"].__setitem__(
                "alive", [[False] * FORM_COLS for _ in range(FORM_ROWS)])),
            mutated(lambda d: d.__setitem__("bombs", [[1, 2]])),
            mutated(lambda d: d.__setitem__("shot", [1, 99, 0.0])),
            mutated(lambda d: d.__setitem__("shields", [[1, 2, 3]])),
            mutated(lambda d: d.__setitem__("ufo", [1.0, 5])),
        ]
        for d in bad:
            self.assertIsNone(Game.from_save(d), d if not isinstance(d, dict) else "")
        self.assertIsNotNone(Game.from_save(base))

    def test_atomic_save_and_load_round_trip(self):
        g = self.played()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "invaders-game.json")
            self.assertTrue(invaders.atomic_save_json(path, g.to_save()))
            r = Game.from_save(invaders.load_json_file(path))
            self.assertEqual(r.to_save(), g.to_save())
            with open(path, "w") as f:
                f.write("{not json")
            self.assertIsNone(Game.from_save(invaders.load_json_file(path)))

    def test_finish_phase_resolves_before_save(self):
        g = quiet(new_game())
        g._hit_cannon()
        g.finish_phase()
        self.assertEqual(g.phase, PLAY)
        self.assertEqual(g.lives, 2)
        self.assertIsNotNone(Game.from_save(g.to_save()))


class LayoutPlanTest(unittest.TestCase):
    BOXES = ("field", "stats_win", "controls_win", "agents_win")

    def regions(self, plan):
        return [plan[k] for k in self.BOXES + ("hint",) if plan[k]]

    def assert_fits(self, plan, cols, rows):
        rects = self.regions(plan)
        for y, x, h, w in rects:
            self.assertGreaterEqual(y, 0)
            self.assertGreaterEqual(x, 0)
            self.assertLessEqual(x + w, cols, (cols, rows))
            self.assertLessEqual(y + h, rows, (cols, rows))
        boxes = [plan[k] for k in self.BOXES if plan[k]]
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                overlap = (a[0] < b[0] + b[2] and b[0] < a[0] + a[2]
                           and a[1] < b[1] + b[3] and b[1] < a[1] + a[3])
                self.assertFalse(overlap, (cols, rows, a, b))

    def assert_centred(self, plan, cols, rows):
        """Field + side column centred horizontally, and vertically in the
        rows below the header (and above a bottom hint row)."""
        boxes = [plan[k] for k in self.BOXES if plan[k]]
        left = min(x for _, x, _, _ in boxes)
        right = cols - max(x + w for _, x, _, w in boxes)
        top_row = 1 if plan["header"] else 0
        bottom_edge = rows
        hint = plan["hint"]
        if hint and hint[0] == rows - 1 and hint[3] == cols:
            bottom_edge = rows - 1
        top = min(y for y, _, _, _ in boxes) - top_row
        bottom = bottom_edge - max(y + h for y, _, h, _ in boxes)
        self.assertLessEqual(abs(left - right), 1, (cols, rows, left, right))
        self.assertLessEqual(abs(top - bottom), 1, (cols, rows, top, bottom))

    def test_popup_size_93x28(self):
        plan = layout_plan(93, 28)
        self.assertFalse(plan["too_small"])
        self.assertTrue(plan["header"] and plan["stats"] and plan["agents"])
        self.assertIsNone(plan["hint"])
        self.assert_fits(plan, 93, 28)
        self.assert_centred(plan, 93, 28)
        fy, fx, fh, fw = plan["field"]
        self.assertEqual((fh, fw), (FIELD_H + 2, FIELD_W + 2))
        sy, sx, sh, sw = plan["stats_win"]
        cy, cx, ch, cw = plan["controls_win"]
        ay, ax, ah, aw = plan["agents_win"]
        self.assertEqual({sw, cw, aw}, {30})
        self.assertEqual({sx, cx, ax}, {fx + fw + 1})
        # stats over controls over agents, down to the field's bottom edge
        self.assertEqual((sy, cy, ay), (fy, sy + sh, cy + ch))
        self.assertEqual(ay + ah, fy + fh)
        self.assertEqual(ch, invaders.controls_height(invaders.CONTROLS, 30))

    def test_old_popup_77x24(self):
        plan = layout_plan(77, 24)
        self.assertTrue(plan["header"] and plan["stats"] and plan["agents"])
        self.assertEqual(plan["stats_win"][3], 30)
        self.assertIsNotNone(plan["controls_win"])
        self.assert_fits(plan, 77, 24)
        self.assert_centred(plan, 77, 24)

    def test_degrading_tiers(self):
        p = layout_plan(63, 24)  # narrowest side column (common.SIDE_MIN_W)
        self.assertEqual(p["controls_win"][3], 20)
        self.assertIsNone(p["hint"])
        p = layout_plan(62, 24)  # no side column: hint on the last row
        self.assertFalse(p["stats"] or p["agents"])
        self.assertIsNone(p["controls_win"])
        self.assertEqual(p["hint"], (23, 0, 1, 62))
        p = layout_plan(50, 23)  # no spare row: the hint joins the header
        self.assertTrue(p["header"] and p["hint_in_header"])
        self.assertEqual(p["hint"][0], 0)
        p = layout_plan(invaders.MIN_COLS, invaders.MIN_ROWS)
        self.assertFalse(p["too_small"] or p["header"])
        self.assertEqual(p["field"][:2], (0, 0))
        # no header row at all: the hint sits in the field's bottom border
        fy, fx, fh, fw = p["field"]
        self.assertEqual(p["hint"][0], fy + fh - 1)
        self.assertGreater(p["hint"][1], fx)
        self.assertLess(p["hint"][1] + p["hint"][3], fx + fw)
        for cols, rows in ((invaders.MIN_COLS - 1, 24), (77, invaders.MIN_ROWS - 1)):
            p = layout_plan(cols, rows)
            self.assertTrue(p["too_small"])
            self.assertIsNone(p["field"])

    def test_every_tier_stays_in_bounds_and_centred(self):
        for cols in range(invaders.MIN_COLS, 130):
            for rows in range(invaders.MIN_ROWS, 40):
                p = layout_plan(cols, rows)
                self.assert_fits(p, cols, rows)
                self.assert_centred(p, cols, rows)
                self.assertTrue(p["controls_win"] or p["hint"], (cols, rows))

    def test_key_hint_always_has_help(self):
        self.assertEqual(invaders.key_hint(500), invaders.controls_hint(invaders.CONTROLS))
        for width in range(6, 80):
            text = invaders.key_hint(width)
            self.assertLessEqual(len(text), width)
            self.assertIn("? help", text)


class ConstantsTest(unittest.TestCase):
    def test_controls_cover_every_key(self):
        keys = " ".join(k for k, _ in invaders.CONTROLS)
        for k in ("?", "q", "p", "r", "space", "<-", "->", "up"):
            self.assertIn(k, keys)
        self.assertEqual([k for k, _ in invaders.CONTROLS][-3:], ["?", "p / r", "q"])
        for code in invaders.LEFT_KEYS + invaders.RIGHT_KEYS + invaders.FIRE_KEYS:
            if 32 < code < 256:
                self.assertIn(chr(code).lower(), keys)

    def test_help_sections(self):
        heads = [h for h, _ in invaders.HELP]
        for need in ("Goal", "Controls", "How it works", "Scoring", "Tips"):
            self.assertIn(need, heads)
        for h, body in invaders.HELP:
            self.assertTrue(body.strip(), h)
            body.encode("ascii")
        self.assertEqual(invaders.TITLE, "Space Invaders")


class HelpKeyTest(unittest.TestCase):
    def setUp(self):
        self.help = invaders.HelpOverlay(invaders.TITLE, invaders.HELP)
        self.game = new_game()

    def route(self, ch, by_help):
        return invaders.route_help_key(self.help, self.game, ch, by_help)

    def test_help_pauses_and_closing_resumes(self):
        consumed, by_help = self.route(ord("?"), False)
        self.assertTrue(consumed and by_help and self.help.is_open and self.game.paused)
        consumed, by_help = self.route(invaders.curses.KEY_DOWN, by_help)
        self.assertTrue(consumed and self.help.is_open)
        snap = self.game.to_save()
        self.assertFalse(self.game.update(1.0))  # frozen while help is open
        self.assertEqual(snap, self.game.to_save())
        consumed, by_help = self.route(ord("q"), by_help)  # closes only
        self.assertTrue(consumed)
        self.assertFalse(self.help.is_open or self.game.paused or by_help)

    def test_help_keeps_an_existing_pause_and_game_over(self):
        self.game.paused = True
        _, by_help = self.route(ord("?"), False)
        self.assertFalse(by_help)
        self.route(ord(" "), by_help)  # page down
        self.route(ord("x"), by_help)
        self.assertFalse(self.help.is_open)
        self.assertTrue(self.game.paused)
        g = new_game()
        g.game_over = True
        self.game = g
        consumed, by_help = self.route(ord("?"), False)
        self.assertTrue(consumed and self.help.is_open)
        self.assertFalse(by_help)

    def test_resize_and_other_keys_pass_through(self):
        consumed, _ = self.route(ord("p"), False)
        self.assertFalse(consumed)
        self.route(ord("?"), False)
        consumed, _ = self.route(invaders.curses.KEY_RESIZE, True)
        self.assertFalse(consumed)
        self.assertTrue(self.help.is_open)


class FakeWin:
    """curses window stub with curses-accurate bounds checking. Writes out of
    bounds are recorded (safe_addstr would otherwise swallow the error)."""

    def __init__(self, rows, cols, y0=0, x0=0, log=None):
        self.rows, self.cols = rows, cols
        self.y0, self.x0 = y0, x0
        self.writes = []
        self.oob = log if log is not None else []

    def getmaxyx(self):
        return self.rows, self.cols

    def erase(self):
        pass

    def derwin(self, h, w, y, x):
        if y < 0 or x < 0 or y + h > self.rows or x + w > self.cols:
            raise invaders.curses.error()
        child = FakeWin(h, w, self.y0 + y, self.x0 + x, self.oob)
        child.writes = self.writes
        return child

    def addstr(self, y, x, text, attr=0):
        if y < 0 or y >= self.rows or x < 0 or x + len(text) > self.cols:
            self.oob.append((self.y0 + y, self.x0 + x, text))
            raise invaders.curses.error()
        self.writes.append((self.y0 + y, self.x0 + x, text))


class RenderTest(unittest.TestCase):
    def render(self, game, cols=77, rows=24, **kw):
        scr = FakeWin(rows, cols)
        with mock.patch("curses.color_pair", lambda n: n << 8):
            lay = invaders.Layout(scr)
            pairs = {name: 40 + i for i, name in enumerate(invaders.COLORS)}
            status_pairs = {s: 1 + i for i, s in enumerate(invaders.__dict__.get(
                "AGENT_STATUSES", ("idle", "working", "blocked", "done", "unknown")))}
            invaders.draw(pairs, status_pairs, game, lay, kw.get("roster"),
                          kw.get("attention"), kw.get("resume", False), 0,
                          kw.get("help"))
        return scr

    def screen(self, scr):
        grid = [[" "] * scr.cols for _ in range(scr.rows)]
        for y, x, text in scr.writes:
            for i, ch in enumerate(text):
                grid[y][x + i] = ch
        return ["".join(r) for r in grid]

    def test_full_frame_has_no_out_of_bounds_writes(self):
        from common import AgentSnapshot

        g = new_game()
        g.ufo = Ufo(-2.0, 1)        # partly off-screen: must be clipped
        g.bombs = [Projectile(5, 12)]
        g.fire()
        roster = [AgentSnapshot("p%d" % i, "a-rather-long-agent-name-%d (claude)" % i,
                                "claude", "working") for i in range(30)]
        scr = self.render(g, roster=roster)
        self.assertEqual(scr.oob, [])
        lines = self.screen(scr)
        self.assertTrue(lines[0].startswith(" space invaders"))
        self.assertIn("/o\\", "\n".join(lines))
        self.assertIn(" space invaders ", lines[1])
        self.assertIn(" agents ", "\n".join(lines))
        self.assertIn(" keys ", "\n".join(lines))
        self.assertIn("pause / restart", "\n".join(lines))
        for y, x, text in scr.writes:
            self.assertLessEqual(x + len(text), 77)
            self.assertLess(y, 24)

    def test_overlays_and_small_tiers_render_in_bounds(self):
        g = new_game()
        g.game_over = True
        for cols, rows in ((77, 24), (60, 24), (50, 23), (42, 22), (30, 10)):
            scr = self.render(g, cols, rows)
            self.assertEqual(scr.oob, [], (cols, rows))
        text = "\n".join(self.screen(self.render(g)))
        self.assertIn("GAME OVER", text)
        self.assertIn("r restart | q menu", text)
        small = "\n".join(self.screen(self.render(g, 30, 10)))
        self.assertIn("pane too small", small)
        header = self.screen(self.render(new_game(), 50, 23))[0]
        self.assertIn("score 0", header)
        self.assertIn("lives 3", header)

    def test_controls_and_help_overlay_at_every_tier(self):
        for cols, rows in ((93, 28), (77, 24), (64, 24), (60, 25), (50, 23),
                           (42, 22)):
            for help_open in (False, True):
                overlay = invaders.HelpOverlay(invaders.TITLE, invaders.HELP)
                if help_open:
                    overlay.open()
                    overlay.handle_key(ord(" "))
                scr = self.render(new_game(), cols, rows, help=overlay)
                self.assertEqual(scr.oob, [], (cols, rows, help_open))
                text = "\n".join(self.screen(scr))
                if help_open:
                    self.assertIn("Space Invaders - help", text, (cols, rows))
                else:
                    self.assertIn("? help", text.replace("?       help", "? help"),
                                  (cols, rows))
        lines = self.screen(self.render(new_game(), 93, 28))
        self.assertIn("space   fire", "\n".join(lines))
        # hint in the field's bottom border when there is no header row
        bottom = self.screen(self.render(new_game(), 42, 22))[21]
        self.assertTrue(bottom.startswith("+-"))
        self.assertIn("? help", bottom)
        # the too-small screen still draws the help when it fits
        overlay = invaders.HelpOverlay(invaders.TITLE, invaders.HELP)
        overlay.open()
        scr = self.render(new_game(), 30, 12, help=overlay)
        self.assertEqual(scr.oob, [])
        self.assertIn("help", "\n".join(self.screen(scr)))


if __name__ == "__main__":
    unittest.main()
