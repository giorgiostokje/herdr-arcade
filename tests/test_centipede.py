"""Unit tests for the pure Centipede logic in centipede.py (no terminal needed)."""

import json
import os
import random
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import centipede
from centipede import (
    BODY_POINTS,
    FIELD_H,
    FIELD_W,
    FLEA_POINTS,
    HEAD_POINTS,
    MUSHROOM_HP,
    PLAYER_START,
    SEGMENTS,
    START_LIVES,
    ZONE_TOP,
    Game,
    centipede_interval,
    layout_plan,
    spawn_centipede,
    step_centipede,
)
from common import AgentSnapshot


def empty_game(seed=1):
    """A game with no mushrooms, a single parked centipede out of the way,
    and spider/flea spawns pushed far into the future."""
    g = Game(random.Random(seed))
    g.mushrooms = {}
    g.centipedes = [[[0, 0, 1, 1]]]
    g.spider_wait = g.flea_wait = 1e9
    return g


class CentipedeMovementTest(unittest.TestCase):
    def test_moves_one_cell_sideways_and_body_follows(self):
        segs = [[5, 3, 1, 1], [4, 3, 1, 1], [3, 3, 1, 1]]
        out = step_centipede(segs, {})
        self.assertEqual(out, [[6, 3, 1, 1], [5, 3, 1, 1], [4, 3, 1, 1]])

    def test_wall_drops_one_row_and_reverses(self):
        segs = [[FIELD_W - 1, 3, 1, 1], [FIELD_W - 2, 3, 1, 1]]
        out = step_centipede(segs, {})
        self.assertEqual(out[0], [FIELD_W - 1, 4, -1, 1])
        self.assertEqual(out[1], [FIELD_W - 1, 3, 1, 1])
        out = step_centipede(out, {})
        self.assertEqual(out[0], [FIELD_W - 2, 4, -1, 1])
        self.assertEqual(out[1], [FIELD_W - 1, 4, -1, 1])  # followed the drop
        left = step_centipede([[0, 7, -1, 1]], {})
        self.assertEqual(left[0], [0, 8, 1, 1])

    def test_mushroom_drops_one_row_and_reverses(self):
        out = step_centipede([[5, 3, 1, 1]], {(6, 3): 4})
        self.assertEqual(out[0], [5, 4, -1, 1])
        # Vertical moves ignore mushrooms (no stall): drops onto one below.
        out = step_centipede([[5, 3, 1, 1]], {(6, 3): 4, (5, 4): 4})
        self.assertEqual(out[0], [5, 4, -1, 1])

    def test_bottom_turns_back_up_and_roams_in_the_zone(self):
        out = step_centipede([[FIELD_W - 1, FIELD_H - 1, 1, 1]], {})
        self.assertEqual(out[0], [FIELD_W - 1, FIELD_H - 2, -1, -1])
        out = step_centipede([[0, ZONE_TOP, -1, -1]], {})
        self.assertEqual(out[0], [0, ZONE_TOP + 1, 1, 1])  # never leaves the zone

    def test_spawn_enters_at_the_top(self):
        segs = spawn_centipede(SEGMENTS)
        self.assertEqual(len(segs), SEGMENTS)
        self.assertTrue(all(s[1] == 0 for s in segs))
        self.assertEqual(segs[0][0], SEGMENTS - 1)

    def test_game_steps_on_the_wave_interval(self):
        g = empty_game()
        g.centipedes = [spawn_centipede(3)]
        g.update(centipede_interval(1) * 1.01)
        self.assertEqual(g.centipedes[0][0][:2], [3, 0])


class ShotTest(unittest.TestCase):
    def test_one_shot_at_a_time(self):
        g = empty_game()
        self.assertTrue(g.fire())
        first = g.bullet[:]
        self.assertFalse(g.fire())
        self.assertEqual(g.bullet, first)
        g.update(1.0)  # flies off the top
        self.assertIsNone(g.bullet)
        self.assertTrue(g.fire())

    def test_no_cell_skipping_even_with_a_large_dt(self):
        g = empty_game()
        px, py = g.px, g.py
        for y in range(1, py):
            g.mushrooms[(px, y)] = MUSHROOM_HP
        g.fire()  # immediately enters the cell right above: nearest mushroom
        self.assertIsNone(g.bullet)
        self.assertEqual(g.mushrooms[(px, py - 1)], MUSHROOM_HP - 1)
        g.mushrooms[(px, py - 1)] = MUSHROOM_HP
        del g.mushrooms[(px, py - 1)]
        g.fire()
        g.update(1.0)
        self.assertEqual(g.mushrooms[(px, py - 2)], MUSHROOM_HP - 1)
        for y in range(1, py - 2):
            self.assertEqual(g.mushrooms[(px, y)], MUSHROOM_HP, y)

    def test_segment_moving_onto_the_shot_is_hit(self):
        g = empty_game()
        g.bullet = [5, 3]
        g.shot_timer = -10.0  # hold the shot still
        g.centipedes = [[[4, 3, 1, 1], [3, 3, 1, 1]]]
        g.update(centipede_interval(1) * 1.01)
        self.assertIsNone(g.bullet)
        self.assertEqual(g.mushrooms.get((5, 3)), MUSHROOM_HP)
        self.assertEqual(g.score, HEAD_POINTS)


class MushroomTest(unittest.TestCase):
    def test_four_hit_points_then_destroyed_for_one_point(self):
        g = empty_game()
        target = (g.px, g.py - 1)
        g.mushrooms[target] = MUSHROOM_HP
        for hp in (3, 2, 1):
            g.fire()
            self.assertEqual(g.mushrooms[target], hp)
            self.assertEqual(g.score, 0)
        g.fire()
        self.assertNotIn(target, g.mushrooms)
        self.assertEqual(g.score, 1)

    def test_random_field_is_seedable(self):
        a = Game(random.Random(4)).mushrooms
        b = Game(random.Random(4)).mushrooms
        self.assertEqual(a, b)
        self.assertEqual(len(a), centipede.MUSHROOM_COUNT)
        self.assertNotIn(PLAYER_START, a)
        self.assertTrue(all(0 < y < FIELD_H - 1 for _, y in a))


class SplitTest(unittest.TestCase):
    def setUp(self):
        self.g = empty_game()
        # Head at x=9 heading right; body trails to the left on row 5.
        self.g.centipedes = [[[9 - i, 5, 1, 1] for i in range(6)]]

    def test_body_hit_splits_into_two_independent_centipedes(self):
        g = self.g
        self.assertTrue(g._hit_at(6, 5))  # index 3
        self.assertEqual(g.score, BODY_POINTS)
        self.assertEqual(g.mushrooms[(6, 5)], MUSHROOM_HP)
        self.assertEqual(len(g.centipedes), 2)
        front, rear = g.centipedes
        self.assertEqual([s[0] for s in front], [9, 8, 7])
        self.assertEqual([s[0] for s in rear], [5, 4])
        self.assertEqual(rear[0], [5, 5, 1, 1])  # new head, same heading
        # The rear head now meets the mushroom its sibling left: it drops.
        g.centipedes = [step_centipede(c, g.mushrooms) for c in g.centipedes]
        self.assertEqual(g.centipedes[0][0], [10, 5, 1, 1])
        self.assertEqual(g.centipedes[1][0], [5, 6, -1, 1])
        self.assertEqual(g.centipedes[1][1], [5, 5, 1, 1])

    def test_head_hit_scores_100_and_next_segment_leads(self):
        g = self.g
        g._hit_at(9, 5)
        self.assertEqual(g.score, HEAD_POINTS)
        self.assertEqual(len(g.centipedes), 1)
        self.assertEqual(g.centipedes[0][0][:2], [8, 5])
        self.assertEqual(len(g.centipedes[0]), 5)

    def test_tail_hit_just_shortens(self):
        g = self.g
        g._hit_at(4, 5)
        self.assertEqual(len(g.centipedes), 1)
        self.assertEqual(len(g.centipedes[0]), 5)
        self.assertEqual(g.score, BODY_POINTS)

    def test_split_rear_after_a_drop_keeps_travelling_the_old_way(self):
        g = empty_game()
        segs = [[FIELD_W - 3 + i, 2, 1, 1] for i in range(3)][::-1]
        segs = step_centipede(segs, {})
        # Head dropped to row 3 and reversed; the rest still on row 2.
        g.centipedes = [segs]
        self.assertEqual(segs[0], [FIELD_W - 1, 3, -1, 1])
        g._hit_at(*segs[0][:2])
        rear = g.centipedes[0]
        self.assertEqual(rear[0], [FIELD_W - 1, 2, 1, 1])

    def test_shooting_every_segment_clears_the_wave(self):
        g = self.g
        for x in range(4, 10):  # tail first: five body hits, then the head
            g._hit_at(x, 5)
        self.assertEqual(g.centipedes, [])
        self.assertEqual(g.score, HEAD_POINTS + 5 * BODY_POINTS)


class PlayerTest(unittest.TestCase):
    def test_moves_one_cell_in_all_four_directions(self):
        g = empty_game()
        x, y = g.px, g.py
        self.assertTrue(g.move(0, -1))
        self.assertTrue(g.move(-1, 0))
        self.assertTrue(g.move(1, 0))
        self.assertTrue(g.move(0, 1))
        self.assertEqual((g.px, g.py), (x, y))

    def test_confined_to_the_zone(self):
        g = empty_game()
        g.px, g.py = 0, ZONE_TOP
        self.assertFalse(g.move(0, -1))
        self.assertFalse(g.move(-1, 0))
        g.px, g.py = FIELD_W - 1, FIELD_H - 1
        self.assertFalse(g.move(0, 1))
        self.assertFalse(g.move(1, 0))
        self.assertEqual((g.px, g.py), (FIELD_W - 1, FIELD_H - 1))

    def test_blocked_by_mushrooms(self):
        g = empty_game()
        g.mushrooms[(g.px + 1, g.py)] = 1
        self.assertFalse(g.move(1, 0))
        self.assertEqual((g.px, g.py), PLAYER_START)

    def test_no_moves_while_paused(self):
        g = empty_game()
        g.paused = True
        self.assertFalse(g.move(-1, 0))
        self.assertFalse(g.fire())


class SpiderTest(unittest.TestCase):
    def shoot_spider_at(self, dx, dy):
        g = empty_game()
        g.spider = [g.px + dx, g.py + dy, 1, 1]
        g._hit_at(g.px + dx, g.py + dy)
        self.assertIsNone(g.spider)
        return g.score

    def test_scores_by_distance(self):
        self.assertEqual(self.shoot_spider_at(0, -1), 900)
        self.assertEqual(self.shoot_spider_at(2, -2), 900)
        self.assertEqual(self.shoot_spider_at(-3, -1), 600)
        self.assertEqual(self.shoot_spider_at(0, -4), 600)
        self.assertEqual(self.shoot_spider_at(7, -1), 300)

    def test_bounces_in_the_zone_and_eats_mushrooms(self):
        g = empty_game()
        g.spider = [3, ZONE_TOP, 1, -1]
        with mock.patch.object(centipede, "SPIDER_MOVE_X", 1.0):
            g._step_spider()
        self.assertEqual(g.spider, [4, ZONE_TOP + 1, 1, 1])  # bounced down
        g.mushrooms[(5, ZONE_TOP + 2)] = 4
        with mock.patch.object(centipede, "SPIDER_MOVE_X", 1.0):
            g._step_spider()
        self.assertNotIn((5, ZONE_TOP + 2), g.mushrooms)

    def test_spider_never_spawns_on_or_next_to_the_player(self):
        for px, py in ((0, FIELD_H - 1), (FIELD_W - 1, ZONE_TOP), (1, ZONE_TOP + 2),
                       (FIELD_W - 2, FIELD_H - 1)):
            for seed in range(60):
                g = empty_game(seed)
                g.px, g.py = px, py
                g._spawn_spider()
                sx, sy = g.spider[0], g.spider[1]
                self.assertGreater(max(abs(sx - px), abs(sy - py)), 1, (px, py, seed))
                self.assertIn(sx, (0, FIELD_W - 1))
                self.assertTrue(ZONE_TOP <= sy < FIELD_H)
                self.assertEqual(g.spider[2], 1 if sx == 0 else -1)

    def test_spider_spawns_after_its_delay(self):
        g = empty_game()
        g.spider_wait = 0.5
        g.update(0.6)
        self.assertIsNotNone(g.spider)
        self.assertGreaterEqual(g.spider[1], ZONE_TOP)


class FleaTest(unittest.TestCase):
    def test_flea_falls_straight_down_leaving_mushrooms(self):
        g = empty_game()
        g._spawn_flea(3)
        with mock.patch.object(centipede, "FLEA_DROP_CHANCE", 1.0):
            while g.flea is not None:
                self.assertEqual(g.flea[0], 3)
                g._step_flea()
        dropped = sorted(y for (x, y) in g.mushrooms if x == 3)
        self.assertEqual(dropped, list(range(1, FIELD_H - 1)))
        self.assertEqual(len(g.mushrooms), FIELD_H - 2)

    def test_flea_only_comes_when_zone_is_sparse(self):
        g = empty_game()
        g.flea_wait = 0.1
        for x in range(centipede.FLEA_THRESHOLD):
            g.mushrooms[(x, ZONE_TOP + 1)] = 4
        g.update(0.5)
        self.assertIsNone(g.flea)
        g.mushrooms.pop((0, ZONE_TOP + 1))
        g.update(0.15)
        self.assertIsNotNone(g.flea)

    def test_flea_scores_200(self):
        g = empty_game()
        g.flea = [4, 6]
        g._hit_at(4, 6)
        self.assertIsNone(g.flea)
        self.assertEqual(g.score, FLEA_POINTS)


class WaveTest(unittest.TestCase):
    def test_clearing_all_segments_starts_the_next_faster_wave(self):
        g = empty_game()
        g.mushrooms[(7, 7)] = 2
        g.centipedes = [[[5, 5, 1, 1]]]
        g._hit_at(5, 5)
        g.update(0.01)
        self.assertIsNotNone(g.phase)
        self.assertEqual(g.phase.kind, "wave_clear")
        g.update(centipede.WAVE_CLEAR_TIME + 0.01)
        self.assertIsNone(g.phase)
        self.assertEqual(g.wave, 2)
        self.assertEqual(len(g.centipedes), 1)
        self.assertEqual(len(g.centipedes[0]), SEGMENTS)
        self.assertEqual(g.mushrooms[(7, 7)], 2)  # mushrooms persist
        self.assertEqual(g.mushrooms[(5, 5)], MUSHROOM_HP)
        self.assertLess(centipede_interval(2), centipede_interval(1))


class DeathTest(unittest.TestCase):
    def kill(self, g):
        g.centipedes = [[[g.px - 1, g.py, 1, 1], [g.px - 2, g.py, 1, 1]]]
        g.update(centipede_interval(g.wave) * 1.01)
        self.assertIsNotNone(g.phase)
        self.assertEqual(g.phase.kind, "dying")

    def test_collision_costs_a_life_and_resets_the_round(self):
        g = empty_game()
        g.mushrooms = {(1, 1): 2, (2, 2): 4, (3, 3): 1}
        g.px -= 3
        g.move(1, 0)
        g.move(1, 0)
        g.move(1, 0)
        self.kill(g)
        frozen = [s[:] for s in g.centipedes[0]]
        g.update(0.3)  # nothing moves during the death phase
        self.assertEqual(g.centipedes[0], frozen)
        self.assertEqual(g.lives, START_LIVES)
        g.update(centipede.DEATH_TIME)
        self.assertIsNone(g.phase)
        self.assertEqual(g.lives, START_LIVES - 1)
        self.assertEqual((g.px, g.py), PLAYER_START)
        self.assertEqual(g.mushrooms, {(1, 1): 4, (2, 2): 4, (3, 3): 4})
        self.assertEqual(g.score, 2 * centipede.RESTORE_POINTS)
        self.assertEqual(len(g.centipedes), 1)
        self.assertEqual(len(g.centipedes[0]), 2)  # survivors restart on top
        self.assertTrue(all(s[1] == 0 for s in g.centipedes[0]))

    def test_spider_and_flea_kill_too(self):
        for attr, val in (("spider", "s"), ("flea", "f")):
            g = empty_game()
            if val == "s":
                g.spider = [g.px, g.py, 1, 1]
            else:
                g.flea = [g.px, g.py]
            g.update(0.01)
            self.assertIsNotNone(g.phase, attr)
            self.assertEqual(g.phase.kind, "dying")

    def test_last_life_is_game_over(self):
        g = empty_game()
        g.lives = 1
        self.kill(g)
        g.finish_phase()
        self.assertTrue(g.game_over)
        self.assertEqual(g.lives, 0)
        self.assertFalse(g.update(1.0))
        self.assertFalse(g.move(-1, 0))
        self.assertFalse(g.fire())

    def test_reset_restores_a_fresh_game(self):
        g = empty_game()
        g.score = 55
        g.lives = 1
        g.game_over = True
        g.reset()
        self.assertEqual((g.score, g.lives, g.wave), (0, START_LIVES, 1))
        self.assertFalse(g.game_over)
        self.assertEqual(len(g.centipedes[0]), SEGMENTS)


class SaveTest(unittest.TestCase):
    def build(self):
        g = Game(random.Random(11))
        g.score = 4321
        g.lives = 2
        g.wave = 3
        g.mushrooms[(4, 4)] = 2
        g.centipedes = [[[9, 6, -1, 1], [10, 6, -1, 1]], [[3, 17, 1, -1]]]
        g.bullet = [g.px, 10]
        g.spider = [2, ZONE_TOP + 1, 1, -1]
        g.flea = [7, 4]
        return g

    def test_round_trip_including_rng(self):
        g = self.build()
        data = json.loads(json.dumps(g.to_save()))
        r = Game.from_save(data, random.Random(99))
        self.assertIsNotNone(r)
        self.assertEqual((r.score, r.lives, r.wave), (4321, 2, 3))
        self.assertEqual(r.mushrooms, g.mushrooms)
        self.assertEqual(r.centipedes, g.centipedes)
        self.assertEqual((r.px, r.py), (g.px, g.py))
        self.assertEqual((r.bullet, r.spider, r.flea), (g.bullet, g.spider, g.flea))
        self.assertEqual([g.rng.random() for _ in range(5)],
                         [r.rng.random() for _ in range(5)])
        # And the simulation continues identically.
        g2 = Game.from_save(data)
        r2 = Game.from_save(data)
        for _ in range(100):
            g2.update(0.05)
            r2.update(0.05)
        self.assertEqual(g2.to_save(), r2.to_save())

    def test_save_resolves_pending_death(self):
        g = self.build()
        g.phase = centipede.Phase("dying", centipede.DEATH_TIME)
        data = g.to_save()
        self.assertEqual(data["lives"], 1)
        self.assertIsNone(g.phase)

    def test_rejects_corrupt_or_incompatible(self):
        self.assertIsNone(Game.from_save(None))
        self.assertIsNone(Game.from_save({}))
        self.assertIsNone(Game.from_save("junk"))
        good = self.build().to_save()
        bad = dict(good, version=99)
        self.assertIsNone(Game.from_save(bad))
        for key, value in (
            ("score", "lots"),
            ("lives", 0),
            ("mushrooms", [[1, 1, 9]]),
            ("mushrooms", [[FIELD_W, 1, 2]]),
            ("centipedes", [[[1, 1, 0, 1]]]),
            ("centipedes", []),
            ("player", [3, 2]),  # outside the zone
            ("spider", [1, 2, 1, 1]),
            ("rng", [3, [1, 2, 3], None]),
        ):
            data = json.loads(json.dumps(good))
            data[key] = value
            self.assertIsNone(Game.from_save(data), key)
        data = json.loads(json.dumps(good))
        del data["flea"]
        self.assertIsNone(Game.from_save(data))


REGIONS = ("field", "stats", "controls", "agents", "hint")


class LayoutPlanTest(unittest.TestCase):
    def fits(self, plan, cols, rows):
        for name in REGIONS:
            if plan[name] is None:
                continue
            y, x, h, w = plan[name]
            self.assertGreaterEqual(y, 0, name)
            self.assertGreaterEqual(x, 0, name)
            self.assertLessEqual(x + w, cols, name)
            self.assertLessEqual(y + h, rows, name)

    def assert_centred(self, plan, cols, rows):
        boxes = [plan[n] for n in ("field", "stats", "controls", "agents")
                 if plan[n] is not None]
        top = min(b[0] for b in boxes)
        left = min(b[1] for b in boxes)
        bottom = max(b[0] + b[2] for b in boxes)
        right = max(b[1] + b[3] for b in boxes)
        self.assertLessEqual(abs(left - (cols - right)), 1, (cols, rows))
        last = rows
        if plan["hint"] is not None and plan["hint"][0] == rows - 1:
            last = rows - 1  # the hint has its own bottom row
        self.assertLessEqual(abs((top - 1) - (last - bottom)), 1, (cols, rows))

    def test_full_tier_at_the_new_popup(self):
        plan = layout_plan(93, 28)
        self.assertEqual(plan["tier"], "full")
        for name in ("stats", "controls", "agents"):
            self.assertIsNotNone(plan[name], name)
            self.assertEqual(plan[name][3], 30, name)
        self.assertIsNone(plan["hint"])
        self.fits(plan, 93, 28)
        self.assert_centred(plan, 93, 28)
        fy, fx, fh, fw = plan["field"]
        self.assertEqual((fh, fw), (FIELD_H + 2, FIELD_W * 2 + 2))
        sy, sx, sh, _ = plan["stats"]
        cy, _, ch, _ = plan["controls"]
        ay, _, ah, _ = plan["agents"]
        self.assertEqual(sx, fx + fw + 1)
        self.assertEqual((sy, cy, ay), (fy, sy + sh, cy + ch))
        self.assertEqual(ay + ah, fy + fh)  # agents fill down to the field edge
        self.assertEqual(ch, centipede.controls_height(centipede.CONTROLS, 30))

    def test_full_tier_fits_the_old_popup(self):
        plan = layout_plan(77, 24)
        self.assertEqual(plan["tier"], "full")
        self.fits(plan, 77, 24)
        self.assert_centred(plan, 77, 24)
        self.assertEqual(plan["field"], (1, 2, FIELD_H + 2, FIELD_W * 2 + 2))
        self.assertEqual(plan["controls"][3], 30)

    def test_tiers_degrade(self):
        p = layout_plan(62, 24)  # 62 - 43 = 19: no side column
        self.assertEqual(p["tier"], "field")
        self.assertIsNone(p["stats"])
        self.assertIsNone(p["controls"])
        self.assertEqual(p["hint"], (0, 0, 1, 62))  # shares the header row
        self.assertEqual(layout_plan(63, 24)["controls"][3], 20)
        p = layout_plan(50, 26)
        self.assertEqual(p["hint"], (25, 0, 1, 50))  # a row of its own
        self.assertTrue(layout_plan(41, 24)["too_small"])
        self.assertTrue(layout_plan(77, 23)["too_small"])
        self.assertIsNone(layout_plan(30, 10)["field"])

    def test_every_size_fits_and_is_centred(self):
        for cols in range(42, 130):
            for rows in (24, 25, 26, 28, 30, 40):
                plan = layout_plan(cols, rows)
                self.fits(plan, cols, rows)
                self.assert_centred(plan, cols, rows)
                if plan["controls"] is None:
                    self.assertIsNotNone(plan["hint"], (cols, rows))
                else:
                    self.assertIsNotNone(plan["stats"], (cols, rows))


class ConstantsTest(unittest.TestCase):
    def test_controls_cover_the_keys(self):
        keys = " ".join(k for k, _ in centipede.CONTROLS).split()
        for key in ("?", "q", "p", "r", "space", "arrows", "wasd", "hjkl"):
            self.assertIn(key, keys, key)
        self.assertEqual([k for k, _ in centipede.CONTROLS[-4:]],
                         ["?", "p", "r", "q"])
        # Every movement key the handler knows is covered by the listing.
        for ch in centipede.KEY_MOVES:
            if ch < 256:
                self.assertIn(chr(ch).lower(), "wasdhjkl")

    def test_help_sections(self):
        heads = [h for h, _ in centipede.HELP]
        for need in ("Goal", "Controls", "How it works", "Scoring", "Tips", "Splitting",
                     "The player zone", "Spider (XX)", "Flea (VV)"):
            self.assertIn(need, heads)
        for head, body in centipede.HELP:
            self.assertTrue(body.strip(), head)
            body.encode("ascii")
        scoring = dict(centipede.HELP)["Scoring"]
        for pts in (HEAD_POINTS, BODY_POINTS, FLEA_POINTS, 300, 600, 900):
            self.assertIn(str(pts), scoring)

    def test_key_hint_always_offers_help(self):
        for width in (20, 30, 41, 60, 200):
            hint = centipede.key_hint(width)
            self.assertLessEqual(len(hint), width)
            self.assertIn("? help", hint)


class HelpKeyTest(unittest.TestCase):
    def setUp(self):
        self.help = centipede.HelpOverlay(centipede.TITLE, centipede.HELP)

    def test_help_pauses_and_closing_resumes(self):
        g = empty_game()
        g.centipedes = [spawn_centipede(3)]
        consumed, pbh = centipede.help_key(self.help, g, ord("?"), False)
        self.assertTrue(consumed and pbh and g.paused and self.help.is_open)
        before = [s[:] for s in g.centipedes[0]]
        self.assertFalse(g.update(1.0))  # timers frozen under the help
        self.assertEqual(g.centipedes[0], before)
        consumed, pbh = centipede.help_key(self.help, g, ord(" "), pbh)  # page
        self.assertTrue(consumed and self.help.is_open)
        self.assertIsNone(g.bullet)  # space scrolled; it did not fire
        consumed, pbh = centipede.help_key(self.help, g, ord("q"), pbh)
        self.assertTrue(consumed)
        self.assertFalse(self.help.is_open or pbh or g.paused)
        self.assertTrue(g.update(centipede_interval(1) * 1.01))

    def test_resize_passes_through_and_paused_stays_paused(self):
        g = empty_game()
        centipede.help_key(self.help, g, ord("?"), False)
        consumed, _ = centipede.help_key(self.help, g, centipede.curses.KEY_RESIZE, True)
        self.assertFalse(consumed)
        self.assertTrue(self.help.is_open)
        self.help.close()
        g.paused = True
        consumed, pbh = centipede.help_key(self.help, g, ord("?"), False)
        self.assertFalse(pbh)
        centipede.help_key(self.help, g, ord("x"), pbh)
        self.assertTrue(g.paused)

    def test_help_from_game_over_and_fallthrough(self):
        g = empty_game()
        g.game_over = True
        consumed, pbh = centipede.help_key(self.help, g, ord("?"), False)
        self.assertTrue(consumed and self.help.is_open)
        self.assertFalse(pbh or g.paused)
        self.help.close()
        consumed, _ = centipede.help_key(self.help, g, ord("r"), False)
        self.assertFalse(consumed)


class ColourPairTest(unittest.TestCase):
    def test_pairs_stay_in_the_shared_game_range(self):
        pairs = [v for k, v in vars(centipede).items() if k.startswith("PAIR_")]
        self.assertTrue(pairs)
        self.assertTrue(all(40 <= p <= 59 for p in pairs), pairs)
        self.assertEqual(len(set(pairs)), len(pairs))


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
            raise centipede.curses.error()
        child = FakeWin(h, w, self.root, self.y0 + y, self.x0 + x)
        self.root.children.append(child)
        return child

    def erase(self):
        self.writes = []

    def addstr(self, y, x, text, attr=0):
        if y < 0 or y >= self.rows or x < 0 or x + len(text) > self.cols:
            self.root.violations.append((y, x, text))
            raise centipede.curses.error()
        self.writes.append((y, x, text))

    def text(self):
        wins = [self] + self.children
        return "\n".join(t for w in wins for _, _, t in w.writes)


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
        scr = FakeWin(rows, cols)
        overlay = centipede.HelpOverlay(centipede.TITLE, centipede.HELP)
        if help_open:
            overlay.open()
        with mock.patch("curses.color_pair", return_value=0):
            lay = centipede.Layout(scr)
            centipede.draw(game, lay, self.session(attention), resume, 1234,
                           overlay)
        return scr, lay

    def games(self):
        yield "fresh", Game(random.Random(1))
        busy = SaveTest().build()
        yield "busy", busy
        edge = Game(random.Random(2))
        edge.centipedes = [[[FIELD_W - 1, FIELD_H - 1, 1, 1], [0, 0, 1, 1]]]
        edge.px, edge.py = FIELD_W - 1, FIELD_H - 1
        edge.spider = [0, FIELD_H - 1, 1, 1]
        yield "edges", edge
        dy = Game(random.Random(3))
        dy.phase = centipede.Phase("dying", 1.0)
        dy.phase.elapsed = 0.6
        yield "dying", dy
        wc = Game(random.Random(4))
        wc.phase = centipede.Phase("wave_clear", 1.0)
        yield "wave_clear", wc
        go = Game(random.Random(5))
        go.game_over = True
        yield "game_over", go
        pa = Game(random.Random(6))
        pa.paused = True
        yield "paused", pa

    def test_full_frame_in_bounds_with_two_column_cells(self):
        for name, game in self.games():
            scr, lay = self.render(game)
            self.assertEqual(scr.violations, [], name)
            field = lay.field
            self.assertEqual((field.y0, field.x0), (1, 2))
            cells = [(y, x, t) for y, x, t in field.writes
                     if len(t) == 2 and 0 < y < field.rows - 1]
            self.assertTrue(cells, name)
            for y, x, t in cells:
                self.assertEqual(x % 2, 1, (name, y, x, t))  # cell-aligned
                self.assertLessEqual(x + 2, field.cols - 1)  # inside the frame
            for child in scr.children:
                self.assertLessEqual(child.x0 + child.cols, 77)
                self.assertLessEqual(child.y0 + child.rows, 24)

    def test_full_frame_contents(self):
        scr, lay = self.render(SaveTest().build())
        text = [t for _, _, t in lay.field.writes]
        self.assertIn(centipede.HEAD_GLYPH, text)
        self.assertIn(centipede.BODY_GLYPH, text)
        self.assertIn(centipede.PLAYER_GLYPH, text)
        self.assertIn(centipede.SPIDER_GLYPH, text)
        self.assertIn(centipede.FLEA_GLYPH, text)
        self.assertIn(centipede.SHOT_GLYPH, text)
        self.assertIn(centipede.MUSHROOM_GLYPHS[2], text)
        stats = " ".join(t for _, _, t in lay.stats.writes)
        self.assertIn("score 4321", stats)
        self.assertIn("best  4321", stats)
        self.assertIn("lives 2", stats)
        agents = " ".join(t for _, _, t in lay.agents.writes)
        self.assertIn("agents", agents)

    def test_banners_and_game_over_overlay(self):
        agent = AgentSnapshot("p1", "claude", "claude", "blocked")
        scr, _ = self.render(Game(random.Random(1)), attention=(agent, "working"))
        self.assertIn("claude is blocked", scr.writes[0][2])
        scr, _ = self.render(Game(random.Random(1)), resume=True)
        self.assertIn("resumed", scr.writes[0][2])
        go = Game(random.Random(1))
        go.game_over = True
        _, lay = self.render(go)
        text = " ".join(t for _, _, t in lay.field.writes)
        self.assertIn("GAME OVER", text)
        self.assertIn("r restart | q menu", text)

    def test_smaller_tiers_and_too_small(self):
        for cols, rows in ((58, 24), (50, 24), (42, 24), (120, 40)):
            scr, _ = self.render(Game(random.Random(1)), cols, rows)
            self.assertEqual(scr.violations, [], (cols, rows))
        scr, _ = self.render(Game(random.Random(1)), 50, 24)
        self.assertIn("0 pts", scr.writes[0][2])
        self.assertIn("? help", scr.text())
        scr, _ = self.render(Game(random.Random(1)), 30, 10)
        self.assertEqual(scr.violations, [])
        text = " ".join(t for _, _, t in scr.writes)
        self.assertIn("pane too small", text)
        self.assertIn("need 42x24", text)
        self.assertIn("have 30x10", text)
        self.assertIn("q menu", text)

    def test_help_open_at_every_tier(self):
        for cols, rows in ((93, 28), (77, 24), (50, 24), (50, 26), (30, 10)):
            scr, lay = self.render(Game(random.Random(1)), cols, rows,
                                   help_open=True)
            self.assertEqual(scr.violations, [], (cols, rows))
            text = scr.text()
            self.assertIn("Centipede - help", text, (cols, rows))
            if not lay.too_small:
                self.assertRegex(text, r"\?\s+help", (cols, rows))

    def test_controls_box_at_the_new_popup(self):
        scr, lay = self.render(SaveTest().build(), 93, 28)
        self.assertEqual(scr.violations, [])
        controls = " ".join(t for _, _, t in lay.controls.writes)
        self.assertIn(" keys ", controls)
        self.assertIn("fire", controls)
        self.assertIn("wasd hjkl", controls)
        self.assertIn(" centipede ", " ".join(t for _, _, t in lay.stats.writes))
        self.assertIn("agents", " ".join(t for _, _, t in lay.agents.writes))


if __name__ == "__main__":
    unittest.main()
