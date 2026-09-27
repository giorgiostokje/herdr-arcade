"""Unit tests for the pure game logic, AI, layout and rendering in tron.py (no
terminal needed)."""

import json
import os
import random
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common  # noqa: E402
import tron  # noqa: E402
from common import AgentSnapshot  # noqa: E402
from tron import (  # noqa: E402
    DOWN,
    FIELD_H,
    FIELD_W,
    LEFT,
    LIVES,
    MAX_QUEUE,
    RIGHT,
    ROUND_BONUS,
    SAVE_VERSION,
    STARTS,
    UP,
    Cycle,
    Game,
    ai_count,
    flood_count,
    layout_plan,
    step_interval,
)


def new_game(seed=1):
    """A fresh game past its opening 'ready' beat."""
    g = Game(random.Random(seed))
    g.resolve_phases()
    return g


def tick(game, n=1):
    """Advance exactly n cycle steps (outside any phase)."""
    for _ in range(n):
        game.update(step_interval(game.level))


def finish_phase(game):
    """Let the current phase run out (and the next round's ready beat)."""
    game.update(10.0)
    if game.phase is not None and game.phase.kind == "ready":
        game.update(10.0)


def place(game, *cycles):
    """Replace the cycles with (pos, dir) pairs; index 0 is the player."""
    game.cycles = [Cycle(pos, d) for pos, d in cycles]
    game.queue = []
    game.phase = None
    game.rebuild_grid()


def block(game, cells):
    """Mark cells occupied (owned by the player) without touching trails."""
    for x, y in cells:
        game.grid[y][x] = 0


def straight_ai(game):
    """Patch the AI to ride straight on, so collision rules test alone."""
    return mock.patch.object(game, "ai_choose", lambda i: game.cycles[i].dir)


class MovementTest(unittest.TestCase):
    def test_fresh_round(self):
        g = Game(random.Random(1))
        self.assertEqual(g.phase.kind, "ready")
        self.assertEqual(len(g.cycles), 1 + ai_count(1))
        self.assertEqual(len(g.ai), 1)
        self.assertEqual(g.lives, LIVES)
        pos, d = STARTS[0]
        self.assertEqual((g.player.pos, g.player.dir), (pos, d))
        # The ready beat does not move anything.
        g.update(0.1)
        self.assertEqual(g.player.pos, pos)

    def test_step_moves_one_cell_and_leaves_trail(self):
        g = new_game()
        x, y = g.player.pos
        tick(g)
        self.assertEqual(g.player.pos, (x + 1, y))
        self.assertEqual(g.player.trail, [(x, y), (x + 1, y)])
        self.assertEqual(g.grid[y][x], 0)
        self.assertEqual(g.grid[y][x + 1], 0)
        tick(g, 3)
        self.assertEqual(g.player.pos, (x + 4, y))
        self.assertEqual(len(g.player.trail), 5)
        for c in g.ai:
            self.assertEqual(len(c.trail), 5)
            self.assertEqual(g.grid[c.pos[1]][c.pos[0]], g.cycles.index(c))

    def test_partial_interval_does_not_step(self):
        g = new_game()
        pos = g.player.pos
        g.update(step_interval(1) * 0.5)
        self.assertEqual(g.player.pos, pos)
        g.update(step_interval(1) * 0.6)
        self.assertNotEqual(g.player.pos, pos)

    def test_speed_and_ai_count_scale_with_level(self):
        self.assertLess(step_interval(3), step_interval(1))
        self.assertGreaterEqual(step_interval(100), 0.04)
        self.assertEqual([ai_count(n) for n in (1, 2, 3, 4, 9)], [1, 2, 3, 3, 3])

    def test_scoring_per_step_survived(self):
        g = new_game()
        tick(g, 5)
        self.assertEqual(g.score, 5 * g.level)
        self.assertEqual(g.round_steps, 5)


class TurnQueueTest(unittest.TestCase):
    def setUp(self):
        self.g = new_game()
        place(self.g, ((20, 10), RIGHT), ((50, 2), LEFT))

    def test_reverse_ignored(self):
        self.assertFalse(self.g.turn(LEFT))
        self.assertFalse(self.g.turn(RIGHT))  # same heading: no-op
        tick(self.g)
        self.assertEqual(self.g.player.pos, (21, 10))
        self.assertEqual(self.g.player.dir, RIGHT)

    def test_double_turn_applies_on_successive_ticks(self):
        g = self.g
        self.assertTrue(g.turn(UP))
        self.assertTrue(g.turn(LEFT))  # legal relative to the queued UP
        self.assertEqual(g.queue, [UP, LEFT])
        tick(g)
        self.assertEqual((g.player.pos, g.player.dir), ((20, 9), UP))
        tick(g)
        self.assertEqual((g.player.pos, g.player.dir), ((19, 9), LEFT))

    def test_reverse_of_queued_turn_ignored_and_queue_capped(self):
        g = self.g
        self.assertTrue(g.turn(UP))
        self.assertFalse(g.turn(DOWN))  # reverses the queued UP
        self.assertTrue(g.turn(RIGHT))
        self.assertFalse(g.turn(UP))    # queue full
        self.assertEqual(len(g.queue), MAX_QUEUE)

    def test_turn_during_ready_is_queued(self):
        g = Game(random.Random(3))
        self.assertTrue(g.turn(UP))
        g.update(10.0)  # ready runs out
        tick(g)
        self.assertEqual(g.player.dir, UP)


class CrashTest(unittest.TestCase):
    def test_wall_crash(self):
        g = new_game()
        place(g, ((FIELD_W - 1, 10), RIGHT), ((5, 2), DOWN))
        tick(g)
        self.assertFalse(g.player.alive)
        self.assertEqual(g.lives, LIVES - 1)
        self.assertEqual(g.phase.kind, "dying")

    def test_each_wall(self):
        for pos, d in (((0, 5), LEFT), ((5, 0), UP), ((5, FIELD_H - 1), DOWN)):
            g = new_game()
            place(g, (pos, d), ((40, 10), LEFT))
            tick(g)
            self.assertFalse(g.player.alive, d)

    def test_own_trail_crash(self):
        g = new_game()
        place(g, ((20, 10), RIGHT), ((50, 2), LEFT))
        tick(g)                 # (21, 10)
        g.turn(UP); tick(g)     # (21, 9)
        g.turn(LEFT); tick(g)   # (20, 9)
        g.turn(DOWN); tick(g)   # (20, 10): own trail
        self.assertFalse(g.player.alive)
        self.assertEqual(g.phase.kind, "dying")

    def test_other_trail_crash(self):
        g = new_game()
        place(g, ((20, 10), RIGHT), ((22, 12), UP))
        g.cycles[1].trail = [(22, 12), (22, 11), (22, 10), (22, 9)]
        g.cycles[1].pos = (22, 9)
        g.rebuild_grid()
        with straight_ai(g):
            tick(g)  # (21, 10)
            self.assertTrue(g.player.alive)
            tick(g)  # (22, 10): the AI's trail
        self.assertFalse(g.player.alive)

    def test_ai_crash_vanishes_trail(self):
        g = new_game()
        place(g, ((5, 10), RIGHT), ((40, 10), LEFT), ((50, 1), UP))
        g.cycles[2].trail = [(50, 3), (50, 2), (50, 1)]
        g.rebuild_grid()
        with straight_ai(g):
            tick(g, 2)  # AI 2 hits the top wall on its second step
        self.assertFalse(g.cycles[2].alive)
        for x, y in g.cycles[2].trail:
            self.assertEqual(g.grid[y][x], -1)
        self.assertTrue(g.player.alive)
        self.assertIsNone(g.phase)  # AI 1 still riding

    def test_head_on_swap_kills_both(self):
        g = new_game()
        place(g, ((10, 10), RIGHT), ((11, 10), LEFT))
        with straight_ai(g):
            tick(g)
        self.assertFalse(g.player.alive)
        self.assertFalse(g.cycles[1].alive)
        # Player death wins over the round win.
        self.assertEqual(g.phase.kind, "dying")
        self.assertEqual(g.lives, LIVES - 1)

    def test_same_cell_kills_both(self):
        g = new_game()
        place(g, ((10, 10), RIGHT), ((12, 10), LEFT))
        with straight_ai(g):
            tick(g)
        self.assertFalse(g.player.alive)
        self.assertFalse(g.cycles[1].alive)
        self.assertEqual(g.phase.kind, "dying")

    def test_two_ai_same_cell_both_die_round_won(self):
        g = new_game()
        g.level = 2
        place(g, ((5, 20), RIGHT), ((30, 10), RIGHT), ((32, 10), LEFT))
        with straight_ai(g):
            tick(g)
        self.assertTrue(g.player.alive)
        self.assertFalse(g.cycles[1].alive)
        self.assertFalse(g.cycles[2].alive)
        self.assertEqual(g.phase.kind, "round_won")

    def test_dying_freezes_board(self):
        g = new_game()
        place(g, ((FIELD_W - 1, 10), RIGHT), ((20, 2), DOWN))
        tick(g)
        ai_pos = g.cycles[1].pos
        g.update(0.5)
        self.assertEqual(g.phase.kind, "dying")
        self.assertEqual(g.cycles[1].pos, ai_pos)
        self.assertFalse(g.turn(UP))


class AITest(unittest.TestCase):
    def test_avoids_immediate_death(self):
        for seed in range(30):
            g = new_game(seed)
            place(g, ((5, 18), RIGHT), ((20, 5), RIGHT))
            block(g, [(21, 5), (20, 4)])  # straight and up are fatal
            self.assertEqual(g.ai_choose(1), DOWN, seed)

    def test_avoids_wall(self):
        for seed in range(30):
            g = new_game(seed)
            place(g, ((5, 18), RIGHT), ((FIELD_W - 1, 0), RIGHT))
            self.assertEqual(g.ai_choose(1), DOWN, seed)

    def test_boxed_in_rides_straight(self):
        g = new_game()
        place(g, ((5, 18), RIGHT), ((20, 5), RIGHT))
        block(g, [(21, 5), (20, 4), (20, 6)])
        self.assertEqual(g.ai_choose(1), RIGHT)

    def test_prefers_larger_region_over_pocket(self):
        # A wall along row 7 and two posts make UP a 2-cell pocket (rows 8-9
        # at x=10); DOWN opens onto the whole lower arena. Aggression alone
        # would pick UP (the player is up there).
        for flip in (False, True):
            for seed in range(30):
                g = new_game(seed)
                if not flip:
                    place(g, ((30, 2), LEFT), ((10, 10), RIGHT))
                    walls = [(x, 7) for x in range(FIELD_W)]
                    walls += [(9, 8), (9, 9), (11, 8), (11, 9), (11, 10)]
                    want = DOWN
                else:
                    place(g, ((30, 18), LEFT), ((10, 10), RIGHT))
                    walls = [(x, 13) for x in range(FIELD_W)]
                    walls += [(9, 12), (9, 11), (11, 12), (11, 11), (11, 10)]
                    want = UP
                block(g, walls)
                self.assertEqual(g.ai_choose(1), want, (flip, seed))

    def test_aggression_breaks_ties(self):
        for seed in range(30):
            g = new_game(seed)
            place(g, ((45, 10), LEFT), ((30, 10), UP))
            self.assertEqual(g.ai_choose(1), RIGHT, seed)

    def test_flood_count(self):
        g = new_game()
        place(g, ((5, 5), RIGHT), ((50, 15), LEFT))
        block(g, [(x, 2) for x in range(FIELD_W)])
        self.assertEqual(flood_count(g.grid, (0, 0), limit=10_000), 2 * FIELD_W)
        self.assertEqual(flood_count(g.grid, (0, 10), limit=50), 50)

    def test_ai_survives_a_while(self):
        # The player rides down the left edge out of the way; the AI, left to
        # its own look-ahead, must not crash in the open arena.
        for seed in range(5):
            g = new_game(seed)
            place(g, ((0, 0), DOWN), ((30, 10), LEFT))
            for _ in range(18):
                tick(g)
            self.assertTrue(g.player.alive, seed)
            self.assertTrue(g.cycles[1].alive, seed)


class RoundTest(unittest.TestCase):
    def kill_ai(self, g):
        """Put the AI in a dead end so it crashes on the next step."""
        place(g, ((5, 10), RIGHT), ((FIELD_W - 1, 0), RIGHT))
        block(g, [(FIELD_W - 1, 1), (FIELD_W - 2, 0)])

    def test_round_win_next_level_more_ai(self):
        g = new_game()
        g.score = 100
        self.kill_ai(g)
        tick(g)
        self.assertEqual(g.phase.kind, "round_won")
        self.assertEqual(g.score, 100 + 1 + ROUND_BONUS * 1)
        finish_phase(g)
        self.assertIsNone(g.phase)
        self.assertEqual(g.level, 2)
        self.assertEqual(len(g.ai), 2)
        self.assertTrue(all(c.alive for c in g.cycles))
        self.assertTrue(all(len(c.trail) == 1 for c in g.cycles))
        self.assertEqual(g.lives, LIVES)
        # And a third AI from level 3 on.
        g.level = 3
        g._start_round()
        self.assertEqual(len(g.ai), 3)

    def test_player_crash_loses_life_and_restarts_round(self):
        g = new_game()
        g.level = 2
        g.score = 500
        place(g, ((FIELD_W - 1, 10), RIGHT), ((20, 2), DOWN), ((30, 2), DOWN))
        tick(g)
        self.assertEqual(g.lives, LIVES - 1)
        finish_phase(g)
        self.assertIsNone(g.phase)
        self.assertEqual(g.level, 2)
        self.assertEqual(g.score, 500)
        self.assertEqual(len(g.ai), 2)
        for c, (pos, d) in zip(g.cycles, STARTS):
            self.assertEqual((c.pos, c.dir, c.alive, c.trail), (pos, d, True, [pos]))
        self.assertEqual(g.queue, [])

    def test_game_over(self):
        g = new_game()
        g.lives = 1
        place(g, ((FIELD_W - 1, 10), RIGHT), ((20, 2), DOWN))
        tick(g)
        self.assertFalse(g.game_over)  # death phase first
        finish_phase(g)
        self.assertTrue(g.game_over)
        self.assertEqual(g.lives, 0)
        self.assertFalse(g.update(1.0))
        self.assertFalse(g.turn(UP))
        g.reset()
        self.assertFalse(g.game_over)
        self.assertEqual((g.lives, g.level, g.score), (LIVES, 1, 0))

    def test_paused_does_not_advance(self):
        g = new_game()
        pos = g.player.pos
        g.paused = True
        self.assertFalse(g.update(1.0))
        self.assertEqual(g.player.pos, pos)


def play(game, n):
    """Deterministic scripted play: a turn every few ticks."""
    turns = [UP, LEFT, DOWN, RIGHT]
    for i in range(n):
        if game.game_over:
            break
        if i % 7 == 3:
            game.turn(turns[(i // 7) % 4])
        game.update(step_interval(game.level) if game.phase is None else 0.3)


class DeterminismTest(unittest.TestCase):
    def test_same_seed_same_game(self):
        a, b = Game(random.Random(42)), Game(random.Random(42))
        play(a, 300)
        play(b, 300)
        self.assertEqual(a.to_save(), b.to_save())

    def test_different_seeds_diverge_somewhere(self):
        outs = set()
        for seed in range(6):
            g = Game(random.Random(seed))
            g.level = 3
            g._start_round()
            play(g, 120)
            outs.add(json.dumps(g.to_save()["cycles"]))
        self.assertGreater(len(outs), 1)


class SaveTest(unittest.TestCase):
    def saved(self, seed=9, n=40):
        g = Game(random.Random(seed))
        play(g, n)
        g.resolve_phases()
        return g

    def test_round_trip_including_rng(self):
        g = self.saved()
        data = json.loads(json.dumps(g.to_save()))
        r = Game.from_save(data)
        self.assertIsNotNone(r)
        self.assertEqual(r.to_save(), g.to_save())
        self.assertEqual(r.grid, g.grid)
        # Continuing both gives identical games (AI randomness included).
        play(g, 150)
        play(r, 150)
        self.assertEqual(r.to_save(), g.to_save())

    def test_round_trip_with_dead_ai_and_queue(self):
        g = new_game()
        g.level = 2
        place(g, ((5, 10), RIGHT), ((FIELD_W - 1, 0), RIGHT), ((30, 5), DOWN))
        block(g, [(FIELD_W - 1, 1), (FIELD_W - 2, 0)])
        tick(g)
        self.assertFalse(g.cycles[1].alive)
        g.turn(UP)
        r = Game.from_save(json.loads(json.dumps(g.to_save())))
        self.assertEqual(r.to_save(), g.to_save())
        self.assertEqual(r.grid, g.grid)

    def test_resolves_to_consistent_state(self):
        g = new_game()
        place(g, ((FIELD_W - 1, 10), RIGHT), ((20, 2), DOWN))
        tick(g)
        g.resolve_phases()
        r = Game.from_save(g.to_save())
        self.assertIsNotNone(r)
        self.assertEqual(r.lives, LIVES - 1)

    def test_corrupt_or_incompatible(self):
        good = self.saved().to_save()

        def bad(mutate):
            data = json.loads(json.dumps(good))
            mutate(data)
            return Game.from_save(data)

        self.assertIsNone(Game.from_save(None))
        self.assertIsNone(Game.from_save("junk"))
        self.assertIsNone(Game.from_save({}))
        self.assertIsNone(bad(lambda d: d.update(version=SAVE_VERSION + 1)))
        self.assertIsNone(bad(lambda d: d.update(rng=[3, [1, 2], None])))
        self.assertIsNone(bad(lambda d: d.pop("rng")))
        self.assertIsNone(bad(lambda d: d.update(score=True)))
        self.assertIsNone(bad(lambda d: d.update(score=-1)))
        self.assertIsNone(bad(lambda d: d.update(lives=0)))
        self.assertIsNone(bad(lambda d: d.update(level="2")))
        self.assertIsNone(bad(lambda d: d.update(queue=[UP, LEFT, DOWN])))
        self.assertIsNone(bad(lambda d: d.update(queue=["sideways"])))
        self.assertIsNone(bad(lambda d: d["cycles"].pop()))
        self.assertIsNone(bad(lambda d: d["cycles"][0].update(alive=False)))
        self.assertIsNone(bad(lambda d: d["cycles"][0].update(dir="nope")))
        self.assertIsNone(bad(lambda d: d["cycles"][0].update(pos=[FIELD_W, 0])))
        self.assertIsNone(bad(lambda d: d["cycles"][0].update(pos=[0, 0])))
        self.assertIsNone(bad(lambda d: d["cycles"][0].update(trail=[])))

        def gap(d):
            c = d["cycles"][0]
            c["trail"] = [[0, 0], [5, 5]]
            c["pos"] = [5, 5]
        self.assertIsNone(bad(gap))

        def overlap(d):
            d["cycles"][1]["trail"] = [list(d["cycles"][0]["pos"])]
            d["cycles"][1]["pos"] = list(d["cycles"][0]["pos"])
            d["cycles"][1]["alive"] = True
        self.assertIsNone(bad(overlap))


REGIONS = ("field", "stats", "controls", "agents", "hint")


class LayoutTest(unittest.TestCase):
    def assert_fits(self, plan, cols, rows):
        for name in REGIONS:
            if plan[name] is None:
                continue
            y, x, h, w = plan[name]
            self.assertGreaterEqual(y, 1, name)
            self.assertGreaterEqual(x, 0, name)
            self.assertLessEqual(x + w, cols, name)
            self.assertLessEqual(y + h, rows, name)

    def assert_centred(self, plan, cols, rows):
        fy, fx, fh, fw = plan["field"]
        right = fx + fw
        bottom = fy + fh
        for name in ("stats", "controls", "agents"):
            if plan[name] is not None:
                y, x, h, w = plan[name]
                right = max(right, x + w)
                bottom = max(bottom, y + h)
        if plan["hint"] is not None:
            bottom = plan["hint"][0] + 1
        self.assertLessEqual(abs(fx - (cols - right)), 1, (cols, rows))
        self.assertLessEqual(abs((fy - 1) - (rows - bottom)), 1, (cols, rows))

    def test_popup_size_93x28(self):
        plan = layout_plan(93, 28)
        self.assertEqual(plan["tier"], "full")
        self.assert_fits(plan, 93, 28)
        self.assert_centred(plan, 93, 28)
        self.assertEqual(plan["field"][2:], (FIELD_H + 2, FIELD_W + 2))
        for name in ("stats", "controls", "agents"):
            self.assertIsNotNone(plan[name], name)
            self.assertEqual(plan[name][3], 30, name)
        # Stacked stats / controls / agents, flush with the field's edges.
        sy, _, sh, _ = plan["stats"]
        cy, _, ch, _ = plan["controls"]
        ay, _, ah, _ = plan["agents"]
        self.assertEqual(sy, plan["field"][0])
        self.assertEqual(sy + sh, cy)
        self.assertEqual(cy + ch, ay)
        self.assertEqual(ay + ah, plan["field"][0] + plan["field"][2])
        self.assertEqual(ch, common.controls_height(tron.CONTROLS, 30))
        self.assertIsNone(plan["hint"])
        self.assertFalse(plan["hint_in_header"])

    def test_degrading_tiers(self):
        self.assertEqual(layout_plan(80, 30)["tier"], "full")
        self.assertEqual(layout_plan(79, 24)["tier"], "full")
        self.assertEqual(layout_plan(79, 24)["stats"][3], 20)
        # Below a 20-column side column there is none: field tier.
        for cols, rows in ((78, 24), (77, 24), (64, 24), (58, 24), (58, 25), (70, 30)):
            plan = layout_plan(cols, rows)
            self.assertEqual(plan["tier"], "field", (cols, rows))
            self.assertIsNone(plan["stats"])
            self.assertIsNone(plan["controls"])
            # The key hint is always somewhere: its own row or the header.
            self.assertTrue(plan["hint"] is not None or plan["hint_in_header"])
        self.assertTrue(layout_plan(77, 24)["hint_in_header"])
        self.assertIsNotNone(layout_plan(58, 25)["hint"])
        self.assertTrue(layout_plan(57, 24)["too_small"])
        self.assertTrue(layout_plan(77, 23)["too_small"])
        for cols, rows in ((93, 28), (79, 24), (77, 24), (74, 24), (64, 24),
                           (58, 24), (58, 25), (70, 30), (100, 40), (140, 50)):
            plan = layout_plan(cols, rows)
            self.assert_fits(plan, cols, rows)
            self.assert_centred(plan, cols, rows)


class ConstantsTest(unittest.TestCase):
    def test_controls_cover_every_key(self):
        keys = " ".join(k for k, _ in tron.CONTROLS)
        for k in ("?", "q", "p", "r", "arrows"):
            self.assertIn(k, keys)
        text = " ".join(a for _, a in tron.CONTROLS)
        for k in ("wasd", "hjkl"):
            self.assertIn(k, text)
        # Every letter the handler maps is listed.
        for code in tron.KEY_DIRS:
            if 0 < code < 256:
                self.assertIn(chr(code).lower(), "wasdhjkl")

    def test_help_sections(self):
        heads = [h for h, _ in tron.HELP]
        for h in ("Goal", "Controls", "How it works", "Scoring", "Tips"):
            self.assertIn(h, heads)
        for h, body in tron.HELP:
            self.assertTrue(body.strip(), h)
            body.encode("ascii")  # plain ASCII

    def test_hint_keeps_help(self):
        self.assertIn("? help", tron.hint_text(200))
        for w in (6, 10, 20, 40):
            self.assertIn("? help", tron.hint_text(w))
            self.assertLessEqual(len(tron.hint_text(w)), w)


class HelpKeyTest(unittest.TestCase):
    def test_help_pauses_and_close_resumes(self):
        g = new_game()
        h = common.HelpOverlay(tron.TITLE, tron.HELP)
        consumed, pbh, hand = tron.help_key(h, g, ord("?"), False)
        self.assertTrue(consumed and pbh and h.is_open and g.paused)
        # Scrolling keeps it open and paused; time does not advance.
        consumed, pbh, _ = tron.help_key(h, g, tron.curses.KEY_DOWN, pbh)
        self.assertTrue(consumed and h.is_open and g.paused)
        before = g.to_save()
        self.assertFalse(g.update(5.0))
        self.assertEqual(g.to_save(), before)
        # Any other key (even a game key) closes it and resumes.
        consumed, pbh, hand = tron.help_key(h, g, ord("q"), pbh)
        self.assertTrue(consumed)
        self.assertFalse(h.is_open or g.paused or pbh or hand)

    def test_already_paused_stays_paused(self):
        g = new_game()
        g.paused = True
        h = common.HelpOverlay(tron.TITLE, tron.HELP)
        _, pbh, _ = tron.help_key(h, g, ord("?"), False)
        self.assertFalse(pbh)
        _, pbh, _ = tron.help_key(h, g, ord("x"), pbh)
        self.assertTrue(g.paused)

    def test_game_over_and_banner_handover(self):
        go = new_game()
        go.game_over = True
        h = common.HelpOverlay(tron.TITLE, tron.HELP)
        consumed, pbh, _ = tron.help_key(h, go, ord("?"), False)
        self.assertTrue(consumed and h.is_open)
        self.assertFalse(pbh)
        g = new_game()
        h = common.HelpOverlay(tron.TITLE, tron.HELP)
        _, pbh, _ = tron.help_key(h, g, ord("?"), False)
        _, pbh, hand = tron.help_key(h, g, ord("x"), pbh, hold=True)
        self.assertTrue(hand and g.paused)

    def test_resize_passes_through_and_other_keys_ignored(self):
        g = new_game()
        h = common.HelpOverlay(tron.TITLE, tron.HELP)
        self.assertFalse(tron.help_key(h, g, ord("x"), False)[0])
        tron.help_key(h, g, ord("?"), False)
        self.assertFalse(tron.help_key(h, g, tron.curses.KEY_RESIZE, True)[0])
        self.assertTrue(h.is_open)


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
            raise tron.curses.error()
        child = FakeWin(h, w, self.root, self.y0 + y, self.x0 + x)
        self.root.children.append(child)
        return child

    def erase(self):
        self.writes = []

    def addstr(self, y, x, text, attr=0):
        # The bottom-right cell raises in real curses too (cursor advance).
        if (y < 0 or y >= self.rows or x < 0 or x + len(text) > self.cols
                or (y == self.rows - 1 and x + len(text) == self.cols
                    and self.root is self)):
            self.root.violations.append((y, x, text))
            raise tron.curses.error()
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
               help=None):
        scr = FakeWin(rows, cols)
        with mock.patch("curses.color_pair", return_value=0):
            lay = tron.Layout(scr)
            tron.draw(game, lay, self.session(attention), resume, 1234, help)
        return scr, lay

    def games(self):
        yield "ready", Game(random.Random(1))
        mid = Game(random.Random(2))
        mid.level = 3
        mid._start_round()
        play(mid, 60)
        yield "mid", mid
        d = new_game()
        place(d, ((FIELD_W - 1, 10), RIGHT), ((20, 2), DOWN))
        tick(d)
        d.phase.elapsed = 0.2  # flash frame
        yield "dying", d
        w = new_game()
        place(w, ((5, 10), RIGHT), ((FIELD_W - 1, 0), RIGHT))
        block(w, [(FIELD_W - 1, 1), (FIELD_W - 2, 0)])
        tick(w)
        w.phase.elapsed = 0.2
        yield "round_won", w
        p = new_game()
        p.paused = True
        yield "paused", p
        go = new_game()
        go.lives = 0
        go.game_over = True
        yield "game_over", go
        edge = new_game()
        place(edge, ((0, 0), RIGHT), ((FIELD_W - 1, FIELD_H - 1), LEFT))
        edge.cycles[0].trail = [(x, 0) for x in range(FIELD_W - 1, -1, -1)]
        edge.cycles[0].pos = (0, 0)
        edge.cycles[0].dir = LEFT
        yield "edges", edge

    def test_full_frame_no_out_of_bounds(self):
        for name, game in self.games():
            for kw in ({}, {"resume": True},
                       {"attention": (AgentSnapshot("k", "agent-x " * 20, "omp",
                                                    "blocked"), "working")}):
                scr, lay = self.render(game, **kw)
                self.assertEqual(scr.violations, [], (name, kw))
                self.assertEqual(lay.plan["tier"], "full")

    def all_text(self, scr):
        return " ".join(t for w in [scr] + scr.children for _, _, t in w.writes)

    def test_controls_box_and_side_column(self):
        scr, lay = self.render(new_game())
        self.assertEqual(lay.plan["tier"], "full")
        ctrl = " ".join(t for _, _, t in lay.controls.writes)
        for k, a in tron.CONTROLS:
            self.assertIn(k, ctrl)
        self.assertIn("keys", ctrl)
        self.assertIn("agents", " ".join(t for _, _, t in lay.agents.writes))
        # The old key lines are gone from the stats box.
        self.assertNotIn("q menu", " ".join(t for _, _, t in lay.stats.writes))

    def test_help_overlay_renders_in_bounds(self):
        for cols, rows in ((93, 28), (77, 24), (58, 25), (58, 24), (30, 10)):
            h = common.HelpOverlay(tron.TITLE, tron.HELP)
            h.open()
            for name, game in self.games():
                scr, lay = self.render(game, cols, rows, help=h)
                self.assertEqual(scr.violations, [], (name, cols, rows))
            text = self.all_text(scr)
            self.assertIn("Tron - help", text)
            self.assertIn("Goal", text)
            if not lay.too_small:
                self.assertIn("? help", text)
            # Scroll to the end: still in bounds, later sections visible.
            for _ in range(200):
                h.handle_key(tron.curses.KEY_DOWN)
            scr, lay = self.render(new_game(), cols, rows, help=h)
            self.assertEqual(scr.violations, [], (cols, rows))
            self.assertTrue(h.is_open)

    def test_field_contents(self):
        g = new_game()
        scr, lay = self.render(g)
        field = lay.field
        text = {(y, x): t for y, x, t in field.writes}
        px, py = g.player.pos
        self.assertEqual(text[(1 + py, 1 + px)], tron.HEADS[g.player.dir])
        tick(g, 2)
        scr, lay = self.render(g)
        text = {(y, x): t for y, x, t in lay.field.writes}
        self.assertEqual(text[(1 + py, 1 + px)], tron.TRAIL)

    def test_overlays(self):
        go = new_game()
        go.game_over = True
        scr, lay = self.render(go)
        joined = " ".join(t for _, _, t in lay.field.writes)
        self.assertIn("GAME OVER", joined)
        self.assertIn("r restart | q menu", joined)
        p = new_game()
        p.paused = True
        scr, lay = self.render(p)
        self.assertIn("PAUSED", " ".join(t for _, _, t in lay.field.writes))
        stats = " ".join(t for _, _, t in lay.stats.writes)
        self.assertIn("lives 3", stats)
        self.assertIn("best  1234", stats)

    def test_smaller_tiers_and_too_small(self):
        g = new_game()
        for cols, rows in ((77, 24), (74, 24), (64, 24), (58, 24), (58, 25),
                           (40, 10), (20, 5), (77, 23)):
            scr, lay = self.render(g, cols, rows)
            self.assertEqual(scr.violations, [], (cols, rows))
        scr, lay = self.render(g, 64, 24)
        header = "".join(t for y, _, t in scr.writes if y == 0)
        self.assertIn("score", header)
        self.assertIn("lives", header)
        self.assertIn("? help", header)
        scr, lay = self.render(g, 58, 26)
        hint = "".join(t for y, _, t in scr.writes if y == lay.plan["hint"][0])
        self.assertIn("? help", hint)
        self.assertIn("q menu", hint)
        scr, lay = self.render(g, 57, 24)
        text = " ".join(t for _, _, t in scr.writes)
        self.assertIn("pane too small", text)
        self.assertIn("need 58x24, have 57x24", text)
        self.assertIn("resize the pane (q menu)", text)


if __name__ == "__main__":
    unittest.main()
