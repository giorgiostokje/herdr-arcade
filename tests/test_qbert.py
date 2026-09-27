"""Unit tests for the pure game logic in qbert.py (no terminal needed)."""

import collections
import json
import os
import random
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qbert
from common import AgentSnapshot, controls_height, controls_lines
from qbert import (
    CATCH_POINTS,
    CHANGE_POINTS,
    CLEAR_TIME,
    DEATH_TIME,
    DISC_BONUS,
    DISC_TIME,
    DL,
    DR,
    ENEMY_HOP,
    FREEZE_TIME,
    GREEN_POINTS,
    LIVES,
    LURE_POINTS,
    N_CUBES,
    QB_HOP,
    QUEUE_MAX,
    ROUND_BONUS,
    ROUND_BONUS_STEP,
    ROWS,
    SAVE_VERSION,
    UL,
    UR,
    Enemy,
    Game,
    Phase,
    handle_key,
    layout_plan,
    valid,
)


def new_game(seed=1, spawning=False):
    g = Game(random.Random(seed))
    g.spawning = spawning
    return g


def run(game, seconds, step=0.02):
    for _ in range(int(round(seconds / step))):
        game.update(step)


def hop(game, d):
    """One complete hop (and nothing queued)."""
    game.press(d)
    run(game, QB_HOP + 0.04)


def place(game, r, c):
    game._place_qbert(r, c)


def fill_all_but(game, keep):
    top = game.stages - 1
    for r in range(ROWS):
        for c in range(r + 1):
            game.cubes[r][c] = top
    for r, c in keep:
        game.cubes[r][c] = 0


def settled_enemy(game, kind, r, c, wait=99.0):
    e = Enemy(kind, r, c, wait=wait)
    game.enemies.append(e)
    return e


class GeometryTest(unittest.TestCase):
    def test_pyramid(self):
        cells = [(r, c) for r in range(-2, 10) for c in range(-2, 10) if valid(r, c)]
        self.assertEqual(len(cells), N_CUBES)
        self.assertEqual(N_CUBES, 28)
        self.assertEqual(qbert.neighbour(3, 1, UR), (2, 1))
        self.assertEqual(qbert.neighbour(3, 1, UL), (2, 0))
        self.assertEqual(qbert.neighbour(3, 1, DR), (4, 2))
        self.assertEqual(qbert.neighbour(3, 1, DL), (4, 1))

    def test_hops_between(self):
        self.assertEqual(qbert.hops_between((0, 0), (6, 3)), 6)
        self.assertEqual(qbert.hops_between((6, 0), (6, 6)), 12)
        self.assertEqual(qbert.hops_between((2, 1), (2, 1)), 0)

    def test_drawing_geometry(self):
        # Row r's cubes sit edge to edge, half a cube left of the row above,
        # and the bottom row spans the whole interior.
        self.assertEqual(qbert.tile_left(ROWS - 1, 0), 0)
        self.assertEqual(qbert.tile_left(ROWS - 1, ROWS - 1) + qbert.CUBE_W, qbert.FIELD_IW)
        for r in range(1, ROWS):
            self.assertEqual(qbert.tile_left(r, 0), qbert.tile_left(r - 1, 0) - qbert.HALF)
        # Every disc slot lies inside the interior.
        for q in qbert.DISC_ROWS:
            for c in (-1, q + 1):
                x = qbert.tile_left(q, c)
                self.assertGreaterEqual(x, 0)
                self.assertLessEqual(x + qbert.CUBE_W, qbert.FIELD_IW)
        self.assertEqual(qbert.tile_top(ROWS - 1) + 3, qbert.FIELD_IH)


class HopTest(unittest.TestCase):
    def test_spawn(self):
        g = new_game()
        self.assertEqual((g.qr, g.qc), (0, 0))
        self.assertEqual(g.lives, LIVES)
        self.assertEqual(len(g.discs), 2)
        self.assertEqual(g.remaining(), N_CUBES)

    def test_hop_takes_time_then_lands(self):
        g = new_game()
        self.assertTrue(g.press(DR))
        self.assertEqual((g.qr, g.qc), (1, 1))
        self.assertEqual(g.qbert_cell(), (0, 0))      # still leaving the top
        self.assertEqual(g.cubes[1][1], 0)             # not landed yet
        run(g, QB_HOP / 2 + 0.02)
        self.assertEqual(g.qbert_cell(), (1, 1))
        run(g, QB_HOP)
        self.assertEqual(g.q_t, 0)
        self.assertEqual(g.cubes[1][1], 1)
        self.assertEqual(g.score, CHANGE_POINTS)

    def test_all_four_directions(self):
        g = new_game()
        place(g, 3, 1)
        for d, want in ((UR, (2, 1)), (DR, (3, 2)), (DL, (4, 2)), (UL, (3, 1))):
            hop(g, d)
            self.assertEqual((g.qr, g.qc), want, d)

    def test_input_queued_during_hop(self):
        g = new_game()
        self.assertTrue(g.press(DR))
        self.assertTrue(g.press(DL))
        self.assertTrue(g.press(DR))
        self.assertFalse(g.press(DL))        # queue full: dropped
        self.assertEqual(len(g.queue), QUEUE_MAX)
        run(g, 3 * QB_HOP + 0.1)
        self.assertEqual((g.qr, g.qc), (3, 2))
        self.assertEqual(g.queue, [])
        self.assertEqual(g.score, 3 * CHANGE_POINTS)

    def test_keys(self):
        g = new_game()
        mapping = {qbert.curses.KEY_UP: UR, qbert.curses.KEY_RIGHT: DR,
                   qbert.curses.KEY_DOWN: DL, qbert.curses.KEY_LEFT: UL,
                   ord("w"): UR, ord("d"): DR, ord("s"): DL, ord("a"): UL,
                   ord("k"): UR, ord("l"): DR, ord("j"): DL, ord("h"): UL,
                   ord("u"): UR, ord("n"): DR, ord("b"): DL, ord("y"): UL,
                   ord("N"): DR}
        for ch, d in mapping.items():
            with mock.patch.object(g, "press", return_value=True) as press:
                self.assertTrue(handle_key(g, ch))
                press.assert_called_once_with(d)
        self.assertFalse(handle_key(g, ord("x")))
        handle_key(g, ord("p"))
        self.assertTrue(g.paused)
        self.assertFalse(handle_key(g, ord("n")))
        self.assertEqual(g.q_t, 0)

    def test_no_input_during_phases(self):
        g = new_game()
        g.phase = Phase("dying", DEATH_TIME, "fall")
        self.assertFalse(g.press(DR))


class ColourTest(unittest.TestCase):
    def game_at(self, level):
        g = new_game()
        g.level = level
        g._start_round()
        return g

    def bounce(self, g, times):
        """Hop (0,0) -> (1,1) and back `times` times; (1,1)'s states."""
        seen = []
        for _ in range(times):
            hop(g, DR)
            seen.append(g.cubes[1][1])
            hop(g, UL)
        return seen

    def test_level1_once(self):
        g = self.game_at(1)
        self.assertEqual(self.bounce(g, 3), [1, 1, 1])
        self.assertEqual(g.cube_colour(1), g.scheme[2])
        self.assertEqual(g.score, 2 * CHANGE_POINTS)   # (1,1) once, (0,0) once

    def test_level2_intermediate(self):
        g = self.game_at(2)
        self.assertEqual(g.stages, 3)
        self.assertEqual(self.bounce(g, 3), [1, 2, 2])
        self.assertEqual(g.cube_colour(1), g.scheme[1])
        self.assertEqual(g.cube_colour(2), g.scheme[2])

    def test_level3_toggles_back(self):
        g = self.game_at(3)
        self.assertEqual(self.bounce(g, 4), [1, 0, 1, 0])

    def test_level4_drops_to_intermediate(self):
        g = self.game_at(4)
        self.assertEqual(self.bounce(g, 4), [1, 2, 1, 2])
        self.assertEqual(qbert.rule_for(9), "twice_back")

    def test_colours_change_per_round(self):
        schemes = set()
        g = new_game()
        for _ in range(4):
            schemes.add(g.scheme)
            fill_all_but(g, [(1, 1)])
            hop(g, DR)
            g.resolve_phases()
        self.assertEqual(len(schemes), 4)


class RoundTest(unittest.TestCase):
    def test_round_clear_bonus_and_flash(self):
        g = new_game()
        fill_all_but(g, [(1, 1)])
        before = g.score
        hop(g, DR)
        self.assertEqual(g.phase.kind, "clear")
        bonus = ROUND_BONUS + DISC_BONUS * 1 * 2
        self.assertEqual(g.score, before + CHANGE_POINTS + bonus)
        run(g, CLEAR_TIME + 0.1)
        self.assertIsNone(g.phase)
        self.assertEqual((g.level, g.round), (1, 2))
        self.assertEqual(g.remaining(), N_CUBES)
        self.assertEqual((g.qr, g.qc), (0, 0))

    def test_later_round_bonus_grows(self):
        g = new_game()
        g.round = 3
        self.assertEqual(g.round_bonus(), ROUND_BONUS + 2 * ROUND_BONUS_STEP)
        g.level = 30
        self.assertEqual(g.round_bonus(), qbert.ROUND_BONUS_MAX)

    def test_four_rounds_make_a_level(self):
        g = new_game()
        for _ in range(4):
            fill_all_but(g, [(1, 1)])
            hop(g, DR)
            g.resolve_phases()
        self.assertEqual((g.level, g.round), (2, 1))
        self.assertEqual(g.stages, 3)
        self.assertEqual(g.rule, "twice")

    def test_bot_clears_levels_without_enemies(self):
        """A greedy BFS bot proves every round is completable by hopping
        alone: clear all of level 1 and 2 (8 rounds) and reach level 3."""
        g = new_game(5)
        for _ in range(20000):
            if g.level == 3:
                break
            if g.phase is not None:
                g.update(0.1)
                continue
            if g.q_t == 0 and not g.queue:
                g.press(bot_move(g))
            g.update(0.05)
        self.assertEqual((g.level, g.round), (3, 1))
        self.assertGreaterEqual(g.lives, LIVES)   # never fell; extra lives earned


def bot_move(g):
    """First hop of a shortest on-pyramid path to the nearest unfinished
    cube (any legal hop once none is left besides the current one)."""
    top = g.stages - 1
    start = (g.qr, g.qc)
    first = {start: None}
    todo = collections.deque([start])
    while todo:
        cell = todo.popleft()
        if cell != start and g.cubes[cell[0]][cell[1]] != top:
            return first[cell]
        for d in (UR, UL, DR, DL):
            n = qbert.neighbour(cell[0], cell[1], d)
            if valid(*n) and n not in first:
                first[n] = d if cell == start else first[cell]
                todo.append(n)
    for d in (DR, DL, UR, UL):
        if valid(*qbert.neighbour(g.qr, g.qc, d)):
            return d


class FallAndDiscTest(unittest.TestCase):
    def test_fall_off_loses_life_and_restarts_on_top(self):
        g = new_game()
        place(g, 6, 3)
        hop(g, DL)
        self.assertEqual(g.phase.kind, "dying")
        self.assertEqual(g.phase.cause, "fall")
        self.assertIsNone(g.qbert_cell())
        run(g, DEATH_TIME + 0.1)
        self.assertEqual(g.lives, LIVES - 1)
        self.assertEqual((g.qr, g.qc), (0, 0))

    def test_hop_off_the_top_falls(self):
        g = new_game()
        hop(g, UR)
        self.assertEqual(g.phase.kind, "dying")

    def test_disc_carries_to_top(self):
        g = new_game()
        g.discs = [[2, -1], [3, 4]]
        place(g, 3, 0)
        settled_enemy(g, "red", 5, 2)
        hop(g, UL)
        self.assertEqual(g.phase.kind, "disc")
        self.assertEqual(g.discs, [[3, 4]])
        self.assertEqual(g.enemies, [])          # the others leave
        run(g, DISC_TIME + 0.1)
        self.assertIsNone(g.phase)
        self.assertEqual((g.qr, g.qc), (0, 0))
        self.assertEqual(g.lives, LIVES)

    def test_used_disc_is_gone(self):
        g = new_game()
        g.discs = [[2, -1]]
        place(g, 3, 0)
        hop(g, UL)
        g.resolve_phases()
        place(g, 3, 0)
        hop(g, UL)
        self.assertEqual(g.phase.kind, "dying")

    def test_right_side_disc(self):
        g = new_game()
        g.discs = [[3, 4]]
        place(g, 4, 4)
        hop(g, UR)
        self.assertEqual(g.phase.kind, "disc")

    def test_coily_lured_off(self):
        g = new_game()
        g.discs = [[2, -1]]
        place(g, 3, 0)
        coily = settled_enemy(g, "coily", 5, 1, wait=0.5)
        hop(g, UL)
        self.assertEqual(g.phase.kind, "disc")
        self.assertEqual(g.lure, [2, -1])
        self.assertEqual(g.enemies, [coily])
        score = g.score
        run(g, 6.0)
        self.assertEqual(g.enemies, [])
        self.assertIsNone(g.lure)
        self.assertEqual(g.score, score + LURE_POINTS)
        self.assertEqual(g.lives, LIVES)


class EnemyTest(unittest.TestCase):
    def test_red_ball_kills(self):
        g = new_game()
        place(g, 2, 1)
        settled_enemy(g, "red", 1, 1, wait=0.0)
        g.rng = mock.Mock()
        g.rng.choice = lambda options: DL if DL in options else options[0]
        run(g, ENEMY_HOP + 0.05)
        self.assertEqual(g.phase.kind, "dying")
        self.assertEqual(g.phase.cause, "red")
        run(g, DEATH_TIME + 0.1)
        self.assertEqual(g.lives, LIVES - 1)
        self.assertEqual((g.qr, g.qc), (2, 1))   # a hit keeps Q*bert in place
        self.assertEqual(g.enemies, [])

    def test_hopping_into_a_ball_kills(self):
        g = new_game()
        settled_enemy(g, "red", 1, 1)
        hop(g, DR)
        self.assertEqual(g.phase.kind, "dying")

    def test_no_tunnelling_through_each_other(self):
        g = new_game()
        place(g, 2, 1)
        e = settled_enemy(g, "red", 1, 1)
        g.press(UR)                               # (2,1) -> (1,1)
        e.fr, e.fc, e.r, e.c = 1, 1, 2, 1        # ball (1,1) -> (2,1)
        e.t = e.dur = QB_HOP
        g._check_collisions()
        self.assertIsNotNone(g.phase)

    def test_balls_bounce_down_and_off(self):
        g = new_game()
        place(g, 0, 0)
        e = g.spawn("red")
        rows = []
        for _ in range(400):
            g.update(0.02)
            if e not in g.enemies:
                break
            if e.t == 0:
                rows.append(e.r)
        self.assertNotIn(e, g.enemies)
        self.assertEqual(sorted(set(rows)), list(range(1, ROWS)))
        self.assertEqual(rows, sorted(rows))      # only ever downhill

    def test_purple_hatches_into_coily_who_chases(self):
        g = new_game()
        place(g, 2, 1)
        e = settled_enemy(g, "purple", ROWS - 1, 3, wait=0.0)
        run(g, 0.05)
        self.assertEqual(e.kind, "coily")
        d0 = qbert.hops_between((e.r, e.c), (2, 1))
        run(g, 1.5)
        self.assertLess(qbert.hops_between((e.r, e.c), (2, 1)), d0)
        for _ in range(300):                      # he catches a still Q*bert
            g.update(0.02)
            if g.phase is not None:
                break
        self.assertEqual(g.phase.cause, "coily")

    def test_coily_stays_on_pyramid_without_lure(self):
        g = new_game()
        place(g, 6, 0)
        e = settled_enemy(g, "coily", 0, 0)
        for _ in range(12):
            d = g.coily_dir(e)
            e.r, e.c = qbert.neighbour(e.r, e.c, d)
            self.assertTrue(valid(e.r, e.c))

    def test_green_ball_freezes(self):
        g = new_game()
        settled_enemy(g, "green", 1, 1)
        red = settled_enemy(g, "red", 4, 2, wait=0.5)
        hop(g, DR)
        self.assertIsNone(g.phase)
        self.assertEqual(g.score, CHANGE_POINTS + GREEN_POINTS)
        self.assertGreater(g.freeze_t, FREEZE_TIME - 0.5)
        run(g, 2.0)
        self.assertEqual((red.r, red.c, red.t), (4, 2, 0))   # frozen in place
        run(g, FREEZE_TIME)
        self.assertEqual(g.freeze_t, 0)
        self.assertNotEqual((red.r, red.t), (4, 0))

    def test_slick_reverts_and_can_be_caught(self):
        g = new_game()
        g.cubes[2][1] = 1
        s = settled_enemy(g, "slick", 1, 1, wait=0.0)
        g.rng = mock.Mock()
        g.rng.choice = lambda options: DL if DL in options else options[0]
        run(g, ENEMY_HOP + 0.05)
        self.assertEqual((s.r, s.c), (2, 1))
        self.assertEqual(g.cubes[2][1], 0)
        place(g, 3, 1)
        s.wait = 99
        hop(g, UR)
        self.assertIsNone(g.phase)
        self.assertNotIn(s, g.enemies)
        self.assertEqual(g.score, CATCH_POINTS + CHANGE_POINTS)

    def test_ugg_and_wrongway_climb_and_kill(self):
        g = new_game()
        for kind, start, allowed in (("ugg", (6, 6), ((0, -1), (-1, -1))),
                                     ("wrongway", (6, 0), ((0, 1), (-1, 0)))):
            g.enemies = []
            e = g.spawn(kind)
            self.assertEqual((e.r, e.c), start)
            pos = start
            for _ in range(600):
                g.update(0.02)
                if e not in g.enemies:
                    break
                if (e.r, e.c) != pos:
                    self.assertIn((e.r - pos[0], e.c - pos[1]), allowed)
                    pos = (e.r, e.c)
            self.assertNotIn(e, g.enemies)      # stepped off the side
        g.enemies = []
        settled_enemy(g, "ugg", 1, 1)
        hop(g, DR)
        self.assertEqual(g.phase.cause, "ugg")

    def test_spawn_mix_by_round(self):
        g = new_game(spawning=True)
        kinds = set()
        for _ in range(200):
            g.enemies = []
            kinds.add(g._pick_kind())
        self.assertLessEqual(kinds, {"red", "purple"})
        g.level = 2
        g.round = 1
        kinds = set()
        for _ in range(400):
            g.enemies = []
            g.spawned = 2
            kinds.add(g._pick_kind())
        self.assertEqual(kinds, set(qbert.KINDS) - {"coily"})

    def test_second_spawn_is_purple_and_only_one_coily(self):
        g = new_game(spawning=True)
        g.spawned = 1
        self.assertEqual(g._pick_kind(), "purple")
        g.spawn("purple")
        for _ in range(100):
            self.assertNotEqual(g._pick_kind(), "purple")
        g = Game(random.Random(2))
        most = 0
        for _ in range(3000):
            g.update(0.02)
            most = max(most, sum(e.kind in ("purple", "coily") for e in g.enemies))
            if g.phase is not None:
                g.resolve_phases()
        self.assertEqual(most, 1)


class LifeTest(unittest.TestCase):
    def test_game_over_after_last_life(self):
        g = new_game()
        g.lives = 1
        hop(g, UR)
        run(g, DEATH_TIME + 0.1)
        self.assertTrue(g.game_over)
        self.assertEqual(g.lives, 0)
        self.assertFalse(g.update(0.1))
        self.assertFalse(g.press(DR))

    def test_extra_life(self):
        g = new_game()
        g.score = qbert.EXTRA_LIFE_FIRST - 10
        hop(g, DR)
        self.assertEqual(g.lives, LIVES + 1)
        self.assertEqual(g.next_extra, qbert.EXTRA_LIFE_FIRST + qbert.EXTRA_LIFE_EVERY)

    def test_pause_freezes_everything(self):
        g = new_game(spawning=True)
        g.press(DR)
        g.paused = True
        before = g.to_save()
        self.assertFalse(g.update(0.5))
        self.assertEqual(g.to_save(), before)

    def test_reset(self):
        g = new_game()
        hop(g, DR)
        g.level = 3
        g.reset()
        self.assertEqual((g.score, g.level, g.round, g.lives), (0, 1, 1, LIVES))


class SaveTest(unittest.TestCase):
    def played(self):
        g = Game(random.Random(7))
        rng = random.Random(3)
        for i in range(600):
            if i % 7 == 0 and g.alive and g.q_t == 0:
                g.press(bot_move(g) if rng.random() < 0.8 else rng.choice((DL, DR)))
            g.update(0.03)
        g.resolve_phases()
        g.press(DR if valid(g.qr + 1, g.qc + 1) else DL)
        g.update(0.05)                            # save mid-hop, with a queue
        g.press(DL)
        self.assertFalse(g.game_over)
        return g

    def test_round_trip_including_rng(self):
        g = self.played()
        data = json.loads(json.dumps(g.to_save()))
        r = Game.from_save(data)
        self.assertIsNotNone(r)
        self.assertEqual(r.to_save(), g.to_save())
        self.assertEqual(r.rng.random(), g.rng.random())
        # Deterministic continuation, spawns and bounces included.
        for game in (g, r):
            for i in range(400):
                if i % 9 == 0 and game.alive and game.q_t == 0:
                    game.press(bot_move(game))
                game.update(0.03)
        self.assertEqual(r.to_save(), g.to_save())
        self.assertGreater(len(g.to_save()["enemies"]) + g.spawned, 0)

    def test_corrupt_or_incompatible(self):
        good = self.played().to_save()
        self.assertIsNotNone(Game.from_save(json.loads(json.dumps(good))))
        bad = [None, {}, [], "x"]
        top_all = [[1] * (r + 1) for r in range(ROWS)]
        for key, value in (("version", SAVE_VERSION + 1), ("rng", [1, 2]),
                           ("cubes", top_all), ("cubes", [[0]] * ROWS),
                           ("cubes", [[9] * (r + 1) for r in range(ROWS)]),
                           ("lives", 0), ("q", [9, 9]), ("q", "a"),
                           ("discs", [[0, 0]]), ("queue", ["up"]),
                           ("queue", [DR, DR, DR]), ("facing", "north"),
                           ("enemies", [["dragon", 1, 1, None, None, 0, 0, 0]]),
                           ("enemies", [["red", 1, 1]]),
                           ("q_t", float("nan")), ("round", 5), ("level", 0),
                           ("freeze_t", 99.0), ("score", True)):
            d = dict(good)
            d[key] = value
            bad.append(d)
        d = dict(good)
        del d["score"]
        bad.append(d)
        for data in bad:
            self.assertIsNone(Game.from_save(data), repr(data)[:80])

    def test_phases_resolved_before_save(self):
        g = new_game()
        g.discs = [[2, -1]]
        place(g, 3, 0)
        hop(g, UL)
        self.assertEqual(g.phase.kind, "disc")
        g.resolve_phases()
        r = Game.from_save(json.loads(json.dumps(g.to_save())))
        self.assertEqual((r.qr, r.qc), (0, 0))
        g = new_game()
        hop(g, UR)
        g.resolve_phases()
        r = Game.from_save(json.loads(json.dumps(g.to_save())))
        self.assertEqual(r.lives, LIVES - 1)
        g = new_game()
        fill_all_but(g, [(1, 1)])
        hop(g, DR)
        g.resolve_phases()
        r = Game.from_save(json.loads(json.dumps(g.to_save())))
        self.assertEqual((r.level, r.round, r.remaining()), (1, 2, N_CUBES))


class FuzzTest(unittest.TestCase):
    def test_random_play_never_crashes(self):
        for seed in range(6):
            g = Game(random.Random(seed))
            rng = random.Random(100 + seed)
            steps = 0
            for _ in range(4000):
                if g.game_over:
                    break
                if rng.random() < 0.15:
                    handle_key(g, rng.choice([ord(k) for k in "wasdhjklyubn"]))
                g.update(rng.choice((0.0, 0.01, 0.03, 0.1, 0.3)))
                steps += 1
                for e in g.enemies:
                    self.assertIn(e.kind, qbert.KINDS)
                if g.alive:
                    self.assertTrue(valid(g.qr, g.qc) or g.q_t > 0)
            self.assertGreater(steps, 50)

    def test_bot_with_enemies_scores_and_progresses(self):
        g = Game(random.Random(11))
        for _ in range(30000):
            if g.game_over:
                break
            if g.alive and g.q_t == 0 and not g.queue:
                g.press(bot_move(g))
            g.update(0.05)
        self.assertGreater(g.score, 28 * CHANGE_POINTS)
        self.assertTrue(g.game_over or g.round_index > 0)


REGIONS = ("field", "stats", "controls", "agents", "hint")


def block(plan):
    """Bounding box (y, x, h, w) of the field plus side column."""
    boxes = [plan[n] for n in ("field", "stats", "controls", "agents") if plan[n]]
    y0 = min(b[0] for b in boxes)
    x0 = min(b[1] for b in boxes)
    y1 = max(b[0] + b[2] for b in boxes)
    x1 = max(b[1] + b[3] for b in boxes)
    return y0, x0, y1 - y0, x1 - x0


class LayoutTest(unittest.TestCase):
    def check_fits(self, plan, cols, rows):
        for name in REGIONS:
            box = plan[name]
            if box is None:
                continue
            y, x, h, w = box
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 1)
            self.assertLessEqual(x + w, cols, name)
            self.assertLessEqual(y + h, rows, name)

    def check_centred(self, plan, cols, rows):
        y, x, h, w = block(plan)
        bottom = rows - (1 if plan["hint"] else 0)
        self.assertLessEqual(abs(x - (cols - x - w)), 1, (cols, rows, plan))
        self.assertLessEqual(abs((y - 1) - (bottom - y - h)), 1, (cols, rows, plan))

    def test_full_at_popup_size(self):
        plan = layout_plan(93, 28)
        self.assertEqual(plan["tier"], "full")
        self.check_fits(plan, 93, 28)
        self.check_centred(plan, 93, 28)
        fy, fx, fh, fw = plan["field"]
        self.assertEqual((fh, fw), (qbert.FIELD_H, qbert.FIELD_W))
        self.assertEqual((fh, fw), (24, 58))
        sy, sx, sh, sw = plan["stats"]
        cy, cx, ch, cw = plan["controls"]
        ay, ax, ah, aw = plan["agents"]
        self.assertEqual((sw, cw, aw), (30, 30, 30))
        self.assertEqual(sx, fx + fw + 1)
        self.assertEqual(sy, fy)
        self.assertEqual(cy, sy + sh)
        self.assertEqual(ay, cy + ch)
        self.assertEqual(ch, controls_height(qbert.CONTROLS, 30))
        self.assertGreaterEqual(ah, qbert.AGENTS_MIN_H)
        self.assertIsNone(plan["hint"])

    def test_no_agents_tier(self):
        # The field is tall; stats + controls nearly match it, so a pane one
        # row short of the popup drops the agents first.
        self.assertGreater(qbert.STATS_H + controls_height(qbert.CONTROLS, 30)
                           + qbert.AGENTS_MIN_H, qbert.FIELD_H)
        plan = layout_plan(93, 25)
        self.assertEqual(plan["tier"], "stats")
        self.assertIsNone(plan["agents"])
        self.assertIsNotNone(plan["stats"])
        self.assertIsNotNone(plan["controls"])
        self.check_fits(plan, 93, 25)
        self.check_centred(plan, 93, 25)

    def test_no_side_column_tier(self):
        plan = layout_plan(70, 28)
        self.assertEqual(plan["tier"], "field")
        self.assertIsNone(plan["stats"])
        self.assertIsNone(plan["controls"])
        self.assertEqual(plan["hint"], (27, 0, 1, 70))
        self.check_fits(plan, 70, 28)
        self.check_centred(plan, 70, 28)
        self.assertIsNone(layout_plan(70, 25)["hint"])   # no spare row: header says ? help

    def test_too_small(self):
        self.assertTrue(layout_plan(57, 28)["too_small"])
        self.assertTrue(layout_plan(93, 24)["too_small"])
        self.assertTrue(layout_plan(40, 12)["too_small"])

    def test_every_size_fits_and_centres(self):
        for cols in range(40, 130, 3):
            for rows in range(15, 45):
                plan = layout_plan(cols, rows)
                if not plan["too_small"]:
                    self.check_fits(plan, cols, rows)
                    self.check_centred(plan, cols, rows)
                    if plan["controls"] is None:
                        self.assertEqual(plan["tier"], "field")

    def test_hint_leads_with_help(self):
        for w in (12, 58, 70, 200):
            text = qbert.hint_text(w)
            self.assertIn("? help", text)
            self.assertLess(len(text), w)


class ConstantsTest(unittest.TestCase):
    def test_controls_cover_keys(self):
        keys = " ".join(k for k, _ in qbert.CONTROLS)
        for k in ("?", "q", "p", "r", "arrows", "left", "up", "down", "right",
                  "wasd", "hjkl", "y", "u", "b", "n"):
            self.assertIn(k, keys + " " + " ".join(a for _, a in qbert.CONTROLS))
        self.assertEqual(qbert.CONTROLS[-1], ("q", "menu"))
        for keys, action in qbert.CONTROLS:
            self.assertLessEqual(len(keys), 9)
            keys.encode("ascii")
            action.encode("ascii")
        # The diagram rows stay one line each in the full-width box.
        lines = controls_lines(qbert.CONTROLS, 28)
        self.assertEqual(len(lines), len(qbert.CONTROLS))
        self.assertIn("left y \\  / u up", lines)
        self.assertIn("down b /  \\ n right", lines)

    def test_diagram_matches_keys(self):
        # Each labelled corner of the X hops the way its stroke points.
        self.assertEqual(qbert.KEYS[qbert.curses.KEY_LEFT], UL)
        self.assertEqual(qbert.KEYS[ord("y")], UL)
        self.assertEqual(qbert.KEYS[qbert.curses.KEY_UP], UR)
        self.assertEqual(qbert.KEYS[ord("u")], UR)
        self.assertEqual(qbert.KEYS[qbert.curses.KEY_DOWN], DL)
        self.assertEqual(qbert.KEYS[ord("b")], DL)
        self.assertEqual(qbert.KEYS[qbert.curses.KEY_RIGHT], DR)
        self.assertEqual(qbert.KEYS[ord("n")], DR)
        for ch in (ord("q"), ord("p"), ord("r"), ord("?")):
            self.assertNotIn(ch, qbert.KEYS)

    def test_help_sections(self):
        heads = [h for h, _ in qbert.HELP]
        for want in ("Goal", "Controls", "Levels and colours", "Discs", "Enemies",
                     "What kills you", "Scoring", "Legend", "Tips"):
            self.assertIn(want, heads)
        for head, body in qbert.HELP:
            self.assertTrue(body.strip(), head)
            body.encode("ascii")
        self.assertIn("up-right", dict(qbert.HELP)["Controls"])
        self.assertIn("500", dict(qbert.HELP)["Scoring"])


class HelpKeyTest(unittest.TestCase):
    def setUp(self):
        self.game = new_game()
        self.help = qbert.HelpOverlay(qbert.TITLE, qbert.HELP)
        self.st = qbert.LoopState()
        self.session = types.SimpleNamespace(attention=None, dismiss=lambda: None)

    def key(self, ch):
        if isinstance(ch, str):
            ch = ord(ch)
        return qbert.dispatch_key(ch, self.game, self.help, self.st, self.session)

    def test_help_pauses_and_closing_resumes(self):
        self.assertTrue(self.key("?"))
        self.assertTrue(self.help.is_open)
        self.assertTrue(self.game.paused)
        self.game.press(DR)
        self.assertEqual(self.game.q_t, 0)            # no hops while paused
        self.key("j")                                   # scrolls, stays open
        self.assertTrue(self.help.is_open)
        self.assertEqual(self.key("q"), True)          # q closes help, not the game
        self.assertFalse(self.help.is_open)
        self.assertFalse(self.game.paused)

    def test_help_keeps_a_manual_pause(self):
        self.key("p")
        self.assertTrue(self.game.paused)
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

    def test_resume_banner_swallows_first_key(self):
        self.game.paused = True
        self.st = qbert.LoopState(resume_banner=True)
        self.assertTrue(self.key("n"))
        self.assertFalse(self.game.paused)
        self.assertEqual(self.game.q_t, 0)            # the key only dismissed
        self.key("n")
        self.assertGreater(self.game.q_t, 0)

    def test_other_keys(self):
        self.assertEqual(self.key("q"), "quit")
        self.assertEqual(self.key("r"), "restart")
        self.key("n")
        self.assertEqual((self.game.qr, self.game.qc), (1, 1))


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
            raise qbert.curses.error()
        child = FakeWin(h, w, self.root, self.y0 + y, self.x0 + x)
        self.root.children.append(child)
        return child

    def erase(self):
        self.writes = []

    def addstr(self, y, x, text, attr=0):
        if y < 0 or y >= self.rows or x < 0 or x + len(text) > self.cols:
            self.root.violations.append((y, x, text))
            raise qbert.curses.error()
        self.writes.append((y, x, text))


class RenderTest(unittest.TestCase):
    def session(self, attention=None):
        roster = [AgentSnapshot("p%d" % i, "agent%d-with-a-long-name" % i, "omp", s)
                  for i, s in enumerate(["idle", "working", "blocked", "done"] * 5)]
        return types.SimpleNamespace(
            attention=attention, roster=roster,
            status_pairs={s: i + 1 for i, s in
                          enumerate(("idle", "working", "blocked", "done", "unknown"))},
        )

    def render(self, game, cols=93, rows=28, attention=None, resume=False, help=False):
        scr = FakeWin(rows, cols)
        overlay = qbert.HelpOverlay(qbert.TITLE, qbert.HELP)
        if help:
            overlay.open()
        with mock.patch("curses.color_pair", return_value=0):
            lay = qbert.Layout(scr)
            qbert.draw(game, lay, self.session(attention), resume, 999, overlay)
        return scr, lay

    @staticmethod
    def text(scr):
        out = []
        for win in [scr] + scr.children:
            out.extend(t for _, _, t in win.writes)
        return "\n".join(out)

    def games(self):
        yield "fresh", new_game()
        mid = new_game()
        mid.press(DR)
        mid.update(QB_HOP / 2)
        yield "mid-hop", mid
        for d, where in ((DL, (6, 0)), (DR, (6, 6)), (UL, (3, 0)), (UR, (3, 3))):
            fall = new_game()
            place(fall, *where)
            hop(fall, d)
            for t in (0.0, 0.5, 1.5):
                fall.phase.elapsed = t
                yield "falling %s %s" % (where, t), fall
        disc = new_game()
        disc.discs = [[4, -1], [4, 5]]
        place(disc, 5, 5)
        hop(disc, UR)
        for t in (0.0, 0.9, 1.7):
            disc.phase.elapsed = t
            yield "disc %s" % t, disc
        hit = new_game()
        place(hit, 6, 6)
        hit._die("red")
        yield "hit at the edge", hit
        crowd = new_game()
        for kind, r, c in (("red", 1, 0), ("purple", 6, 3), ("coily", 3, 1),
                           ("green", 2, 2), ("slick", 4, 0), ("sam", 5, 5),
                           ("ugg", 6, 6), ("wrongway", 6, 0)):
            settled_enemy(crowd, kind, r, c)
        crowd.freeze_t = 2.0
        yield "crowd", crowd
        moving = new_game()
        for kind in ("red", "ugg", "wrongway"):
            moving.spawn(kind)
        moving.update(0.1)
        e = settled_enemy(moving, "red", 6, 6)
        e.fr, e.fc, e.r, e.c, e.t, e.dur = 6, 6, 7, 7, 0.1, 0.28
        e = settled_enemy(moving, "wrongway", 6, 6)
        e.fr, e.fc, e.r, e.c, e.t, e.dur = 6, 6, 6, 7, 0.1, 0.28
        yield "entering and leaving", moving
        cl = new_game()
        fill_all_but(cl, [(1, 1)])
        hop(cl, DR)
        cl.phase.elapsed = 1.0
        yield "cleared", cl
        go = new_game()
        go.game_over = True
        go.lives = 0
        yield "game over", go
        pa = new_game()
        pa.paused = True
        yield "paused", pa
        lt = Game(random.Random(4))
        for _ in range(400):
            lt.update(0.05)
        yield "later", lt

    def test_full_frame_in_bounds(self):
        for name, game in self.games():
            scr, lay = self.render(game)
            self.assertEqual(scr.violations, [], name)
            field = lay.field
            self.assertEqual((field.rows, field.cols), (qbert.FIELD_H, qbert.FIELD_W))
            for child in scr.children:
                self.assertLessEqual(child.x0 + child.cols, 93)
                self.assertLessEqual(child.y0 + child.rows, 28)
            if name == "fresh":
                q = [(y, x) for y, x, t in field.writes if t in ("Q>", "<Q")]
                y, x = qbert.sprite_pos(0, 0)
                self.assertEqual(q, [(1 + y, 1 + x)])
                tiles = [t for _, _, t in field.writes if t == qbert.TILE]
                self.assertEqual(len(tiles), N_CUBES)
                discs = [t for _, _, t in field.writes if t in qbert.DISC_GLYPHS]
                self.assertEqual(len(discs), 2)
                self.assertTrue(any(" agents " in t for _, _, t in lay.agents.writes))
                self.assertTrue(any("score" in t for _, _, t in lay.stats.writes))

    def test_pyramid_is_centred_in_field(self):
        scr, lay = self.render(new_game())
        tiles = [(y, x) for y, x, t in lay.field.writes if t == qbert.TILE]
        top = min(tiles)
        left = top[1]
        right = qbert.FIELD_W - (top[1] + len(qbert.TILE))
        self.assertEqual(left, right)

    def test_banner_and_other_sizes(self):
        att = (AgentSnapshot("p1", "agent", "omp", "blocked"), "working")
        scr, _ = self.render(new_game(), attention=att)
        self.assertEqual(scr.violations, [])
        self.assertTrue(any("blocked" in t for y, _, t in scr.writes if y == 0))
        for cols, rows in ((93, 28), (93, 25), (80, 26), (70, 28), (70, 25),
                           (58, 25), (40, 12), (20, 8), (120, 40)):
            for name, game in self.games():
                scr, _ = self.render(game, cols, rows, resume=True)
                self.assertEqual(scr.violations, [], (name, cols, rows))
        scr, lay = self.render(new_game(), 40, 12)
        self.assertTrue(any("pane too small" in t for _, _, t in scr.writes))

    def test_controls_visible_and_help_overlay(self):
        for cols, rows in ((93, 28), (93, 25), (80, 26), (70, 28), (70, 25)):
            for help in (False, True):
                scr, lay = self.render(new_game(), cols, rows, help=help)
                self.assertEqual(scr.violations, [], (cols, rows, help))
                if help:
                    self.assertIn("Q*bert - help", self.text(scr), (cols, rows))
                    continue
                if lay.controls is not None:
                    keys = "\n".join(t for _, _, t in lay.controls.writes)
                    for want in ("up", "help", "pause", "menu", "y u b n"):
                        self.assertIn(want, keys, (cols, rows))
                elif lay.plan["hint"] is not None:
                    hint = [t for y, _, t in scr.writes if y == rows - 1]
                    self.assertTrue(any("? help" in t for t in hint), (cols, rows))
                else:
                    head = [t for y, _, t in scr.writes if y == 0]
                    self.assertTrue(any("? help" in t for t in head), (cols, rows))


if __name__ == "__main__":
    unittest.main()
