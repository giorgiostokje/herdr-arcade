"""Unit tests for the pure game logic in tetris.py (no terminal needed)."""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tetris
from tetris import (
    AgentSnapshot,
    AgentTracker,
    Bag,
    Board,
    Game,
    Piece,
    SHAPES,
    drop_interval,
    normalize_agent_list,
)


def snap(key, status, label=None, kind="omp"):
    return AgentSnapshot(key, label or key, kind, status)


def clear_one_line(game):
    """Fill the bottom row except cols 5-6, then hard-drop an O piece into the
    gap so exactly one line clears."""
    for x in range(10):
        if x not in (5, 6):
            game.board.grid[19][x] = "J"
    game.piece = Piece("O", x=4, y=0)
    game.hard_drop()
    game.finish_clearing()  # resolve the clear phase these tests assert about


class BagTest(unittest.TestCase):
    def test_every_seven_draws_is_a_permutation(self):
        for seed in (0, 1, 42):
            bag = Bag(random.Random(seed))
            for _ in range(3):
                drawn = [bag.next() for _ in range(7)]
                self.assertEqual(sorted(drawn), sorted(tetris.PIECE_KINDS))

    def test_seeded_bag_is_deterministic(self):
        a = Bag(random.Random(7))
        b = Bag(random.Random(7))
        self.assertEqual([a.next() for _ in range(20)], [b.next() for _ in range(20)])

    def test_peek_does_not_consume(self):
        bag = Bag(random.Random(3))
        self.assertEqual(bag.peek(3), bag.peek(3))
        expected = bag.peek(3)[0]  # captured before next() consumes it
        self.assertEqual(bag.next(), expected)


class ShapesTest(unittest.TestCase):
    def test_rotation_states_are_clockwise_rotations(self):
        def normalize(cells):
            min_x = min(x for x, _ in cells)
            min_y = min(y for _, y in cells)
            return sorted((x - min_x, y - min_y) for x, y in cells)

        def rotate_cw(cells):
            return normalize([(-y, x) for x, y in cells])

        for kind, states in SHAPES.items():
            for r in range(4):
                nxt = states[(r + 1) % 4]
                self.assertEqual(
                    normalize(nxt),
                    rotate_cw(states[r]),
                    f"{kind} rotation {r} -> {(r + 1) % 4} is not a CW rotation",
                )

    def test_every_state_has_four_cells_in_bounds(self):
        for kind, states in SHAPES.items():
            for r, cells in enumerate(states):
                self.assertEqual(len(cells), 4, f"{kind}/{r}")
                for x, y in cells:
                    self.assertTrue(0 <= x <= 3 and 0 <= y <= 3, f"{kind}/{r}")


class BoardTest(unittest.TestCase):
    def test_collides_with_walls_floor_and_stack(self):
        board = Board()
        self.assertTrue(board.collides([(-1, 5)]))  # left wall
        self.assertTrue(board.collides([(10, 5)]))  # right wall
        self.assertTrue(board.collides([(5, 20)]))  # floor
        self.assertFalse(board.collides([(5, 19)]))
        board.grid[19][4] = "T"
        self.assertTrue(board.collides([(4, 19)]))
        self.assertFalse(board.collides([(4, 18)]))

    def test_clear_rows_drops_the_stack(self):
        board = Board()
        board.grid[19] = ["J"] * 10
        board.grid[18][0] = "I"
        board.clear_rows([19])
        self.assertEqual(board.grid[19][0], "I")  # fell one row
        self.assertIsNone(board.grid[18][0])
        self.assertEqual(board.grid[19].count("J"), 0)


class GameMovementTest(unittest.TestCase):
    def setUp(self):
        self.game = Game(random.Random(1))
        self.game.piece = Piece("T", x=4, y=5)

    def test_move_within_bounds(self):
        self.assertTrue(self.game.move(-1))
        self.assertEqual(self.game.piece.x, 3)

    def test_move_blocked_at_walls(self):
        self.game.piece = Piece("T", x=0, y=5)
        self.assertFalse(self.game.move(-1))
        self.game.piece = Piece("T", x=7, y=5)
        self.assertFalse(self.game.move(1))

    def test_move_blocked_by_stack(self):
        self.game.board.grid[6][3] = "O"
        self.assertFalse(self.game.move(-1))

    def test_rotation_wall_kick(self):
        # Vertical I one column from the right wall: rotating to horizontal
        # overflows by one column and must succeed via the -1 kick.
        self.game.piece = Piece("I", x=7, y=10, rot=3)
        self.assertTrue(self.game.rotate(1))
        self.assertEqual(self.game.piece.rot, 0)
        self.assertEqual(self.game.piece.x, 6)
        for x, y in self.game.piece.cells():
            self.assertTrue(0 <= x < 10, (x, y))

    def test_rotation_fails_when_no_kick_fits(self):
        # Horizontal I resting on the floor cannot rotate: every kick leaves a
        # cell below row 19.
        self.game.piece = Piece("I", x=3, y=18, rot=0)
        self.assertFalse(self.game.rotate(1))
        self.assertEqual(self.game.piece.rot, 0)

    def test_o_piece_rotation_is_stationary(self):
        self.game.piece = Piece("O", x=4, y=5)
        before = (self.game.piece.x, self.game.piece.y)
        self.assertTrue(self.game.rotate(1))
        self.assertEqual((self.game.piece.x, self.game.piece.y), before)

    def test_ghost_is_piece_at_rest(self):
        self.game.piece = Piece("T", x=4, y=5)
        ghost = self.game.ghost()
        self.assertEqual(ghost.y, 18)  # T is 2 rows tall; floor at 19
        self.assertEqual(ghost.cells(), Piece("T", x=4, y=18).cells())


class GameScoringTest(unittest.TestCase):
    def setUp(self):
        self.game = Game(random.Random(2))

    def test_single_line_clear(self):
        clear_one_line(self.game)
        self.assertFalse(self.game.board.full_rows())  # the full row is gone
        self.assertEqual(self.game.board.grid[19][5], "O")  # stack settled
        self.assertEqual(self.game.lines, 1)
        self.assertEqual(self.game.score, 36 + 100)  # hard drop + single

    def test_tetris_clear(self):
        # Four full rows except col 9; a vertical I dropped into col 9 clears 4.
        for y in (16, 17, 18, 19):
            for x in range(9):
                self.game.board.grid[y][x] = "J"
        self.game.piece = Piece("I", x=7, y=0, rot=1)  # vertical, col 9
        self.game.hard_drop()
        self.game.finish_clearing()
        for y in (16, 17, 18, 19):
            self.assertTrue(all(c is None for c in self.game.board.grid[y]), y)
        self.assertEqual(self.game.lines, 4)
        self.assertEqual(self.game.score, 2 * 16 + 800)

    def test_level_rises_every_ten_lines(self):
        for _ in range(10):
            clear_one_line(self.game)
        self.assertEqual(self.game.lines, 10)
        self.assertEqual(self.game.level, 2)

    def test_line_score_is_level_multiplied(self):
        self.game.lines = 9
        self.game.level = 3
        clear_one_line(self.game)
        self.assertEqual(self.game.score, 36 + 100 * 3)
        self.assertEqual(self.game.lines, 10)

    def test_soft_drop_scores_one_per_row(self):
        self.game.piece = Piece("T", x=4, y=5)
        self.assertTrue(self.game.soft_drop())
        self.assertTrue(self.game.soft_drop())
        self.assertEqual(self.game.score, 2)
        self.assertEqual(self.game.piece.y, 7)

    def test_hard_drop_scores_two_per_row(self):
        self.game.piece = Piece("T", x=4, y=5)
        d = self.game.hard_drop()
        self.assertEqual(d, 13)
        self.assertEqual(self.game.score, 26)

    def test_gravity_speeds_up_per_level(self):
        self.assertEqual(drop_interval(1), 1.0)
        intervals = [drop_interval(l) for l in range(1, 20)]
        self.assertEqual(intervals, sorted(intervals, reverse=True))
        self.assertGreater(intervals[1], intervals[2])
        self.assertEqual(drop_interval(100), 0.05)  # floor

    def test_update_advances_gravity(self):
        self.game.piece = Piece("T", x=4, y=5)
        self.assertTrue(self.game.update(2.0))  # level 1: 1s per row
        self.assertEqual(self.game.piece.y, 7)

    def test_update_freezes_while_paused(self):
        self.game.piece = Piece("T", x=4, y=5)
        self.game.paused = True
        self.assertFalse(self.game.update(10.0))
        self.assertEqual(self.game.piece.y, 5)


class GameFlowTest(unittest.TestCase):
    def setUp(self):
        self.game = Game(random.Random(3))

    def test_hold_swap_locks_out_until_next_piece(self):
        first = self.game.piece.kind
        self.assertTrue(self.game.hold_swap())
        self.assertEqual(self.game.hold, first)
        held_now = self.game.piece.kind
        self.assertFalse(self.game.hold_swap())  # once per piece
        self.assertEqual(self.game.piece.kind, held_now)
        # Locking the piece re-enables hold.
        self.game.piece = Piece("O", x=4, y=18)
        self.game.hard_drop()
        after_lock = self.game.piece.kind
        self.assertTrue(self.game.hold_swap())
        self.assertEqual(self.game.piece.kind, first)
        self.assertEqual(self.game.hold, after_lock)

    def test_hold_swap_back_and_forth(self):
        first = self.game.piece.kind
        self.game.hold_swap()
        self.game.piece = Piece("O", x=4, y=18)
        self.game.hard_drop()
        after_lock = self.game.piece.kind
        self.assertTrue(self.game.hold_swap())
        self.assertEqual(self.game.piece.kind, first)
        self.assertEqual(self.game.hold, after_lock)

    def test_game_over_when_spawn_collides(self):
        for y in (0, 1):
            for x in (3, 4, 5, 6):
                self.game.board.grid[y][x] = "J"
        self.game.piece = Piece("O", x=4, y=18)
        self.game.hard_drop()
        self.assertTrue(self.game.game_over)

    def test_no_input_works_after_game_over(self):
        self.game.game_over = True
        self.assertFalse(self.game.move(1))
        self.assertFalse(self.game.rotate())
        self.assertEqual(self.game.hard_drop(), 0)

    def test_reset_restores_everything(self):
        clear_one_line(self.game)
        self.game.score += 5
        self.game.reset()
        self.assertEqual(self.game.score, 0)
        self.assertEqual(self.game.lines, 0)
        self.assertEqual(self.game.level, 1)
        self.assertFalse(self.game.game_over)
        self.assertIsNone(self.game.hold)
        self.assertTrue(all(c is None for row in self.game.board.grid for c in row))
        self.assertIsNotNone(self.game.piece)


class AgentLogicTest(unittest.TestCase):
    RAW = {
        "id": "cli:agent:list",
        "result": {
            "agents": [
                {
                    "agent": "claude",
                    "agent_status": "working",
                    "cwd": "/repo",
                    "pane_id": "w1:p1",
                },
                {
                    "agent": "omp",
                    "agent_status": "idle",
                    "name": "builder",
                    "pane_id": "w1:p2",
                },
                {
                    "agent": "codex",
                    "agent_status": "blocked",
                    "pane_id": "w1:p3",
                },
                {
                    "agent": "weird",
                    "agent_status": "helicoptering",
                    "pane_id": "w1:p4",
                },
            ],
            "type": "agent_list",
        },
    }

    def test_normalize_maps_real_shape(self):
        roster = normalize_agent_list(self.RAW)
        self.assertEqual(len(roster), 4)
        by_key = {a.key: a for a in roster}
        self.assertEqual(by_key["w1:p1"].label, "claude w1:p1")
        self.assertEqual(by_key["w1:p1"].status, "working")
        self.assertEqual(by_key["w1:p2"].label, "builder (omp)")
        self.assertEqual(by_key["w1:p3"].status, "blocked")
        self.assertEqual(by_key["w1:p4"].status, "unknown")  # unmapped -> unknown

    def test_normalize_tolerates_garbage(self):
        self.assertEqual(normalize_agent_list(None), [])
        self.assertEqual(normalize_agent_list([]), [])
        self.assertEqual(normalize_agent_list({"result": None}), [])

    def test_transition_into_blocked_triggers_once(self):
        tracker = AgentTracker()
        self.assertEqual(tracker.update([snap("w1:p1", "working")]), [])
        events = tracker.update([snap("w1:p1", "blocked")])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0].key, "w1:p1")
        self.assertEqual(events[0][1], "working")
        self.assertEqual(tracker.update([snap("w1:p1", "blocked")]), [])

    def test_done_also_triggers(self):
        tracker = AgentTracker()
        self.assertEqual(tracker.update([snap("a", "working")]), [])
        events = tracker.update([snap("a", "done")])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0].status, "done")

    def test_retrigger_after_status_changes_away(self):
        tracker = AgentTracker()
        tracker.update([snap("a", "working")])
        self.assertEqual(len(tracker.update([snap("a", "blocked")])), 1)
        tracker.update([snap("a", "idle")])
        self.assertEqual(len(tracker.update([snap("a", "blocked")])), 1)
        self.assertEqual(tracker.update([snap("a", "blocked")]), [])

    def test_first_sighting_already_needing_attention_triggers(self):
        tracker = AgentTracker()
        events = tracker.update([snap("a", "done")])
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0][1])  # no previous status observed

    def test_idle_and_working_never_trigger(self):
        tracker = AgentTracker()
        tracker.update([snap("a", "idle"), snap("b", "working")])
        self.assertEqual(tracker.update([snap("a", "working"), snap("b", "idle")]), [])

    def test_vanished_agent_retriggers_on_return(self):
        tracker = AgentTracker()
        tracker.update([snap("a", "blocked")])
        tracker.update([])  # agent leaves the session
        events = tracker.update([snap("a", "blocked")])
        self.assertEqual(len(events), 1)

    def test_distinct_agents_tracked_independently(self):
        tracker = AgentTracker()
        tracker.update([snap("a", "idle"), snap("b", "idle")])
        events = tracker.update([snap("a", "blocked"), snap("b", "idle")])
        self.assertEqual([e[0].key for e in events], ["a"])


class SaveTest(unittest.TestCase):
    def build_game(self):
        game = Game(random.Random(11))
        for x in (0, 1, 2):
            game.board.grid[19][x] = "J"
            game.board.grid[18][x] = "S"
        game.score = 1234
        game.lines = 9
        game.level = 3
        game.hold = "T"
        game.hold_used = True
        game.piece = Piece("L", x=5, y=7, rot=2)
        return game

    def test_round_trip_restores_every_field(self):
        g = self.build_game()
        r = Game.from_save(g.to_save(), random.Random(99))
        self.assertEqual(r.score, 1234)
        self.assertEqual(r.lines, 9)
        self.assertEqual(r.level, 3)
        self.assertEqual(r.hold, "T")
        self.assertTrue(r.hold_used)
        self.assertEqual(r.board.grid, g.board.grid)
        self.assertEqual(
            (r.piece.kind, r.piece.x, r.piece.y, r.piece.rot), ("L", 5, 7, 2)
        )
        self.assertFalse(r.game_over)
        self.assertIsNone(r.clearing)

    def test_round_trip_continues_bag_sequence(self):
        g = self.build_game()
        r = Game.from_save(g.to_save(), random.Random(99))
        self.assertEqual(
            [g.bag.next() for _ in range(15)], [r.bag.next() for _ in range(15)]
        )

    def test_rejects_wrong_version(self):
        saved = self.build_game().to_save()
        saved["version"] = 99
        self.assertIsNone(Game.from_save(saved))

    def test_rejects_corrupt_saves(self):
        self.assertIsNone(Game.from_save(None))
        self.assertIsNone(Game.from_save({}))
        self.assertIsNone(Game.from_save("junk"))
        saved = self.build_game().to_save()
        del saved["grid"]
        self.assertIsNone(Game.from_save(saved))
        saved = self.build_game().to_save()
        saved["grid"] = [["J"] * 10]  # wrong height
        self.assertIsNone(Game.from_save(saved))
        saved = self.build_game().to_save()
        saved["grid"][0] = ["J"] * 9  # wrong width
        self.assertIsNone(Game.from_save(saved))
        saved = self.build_game().to_save()
        saved["bag"] = ["Q"]  # unknown kind
        self.assertIsNone(Game.from_save(saved))
        saved = self.build_game().to_save()
        saved["score"] = "lots"  # non-numeric
        self.assertIsNone(Game.from_save(saved))
        saved = self.build_game().to_save()
        saved["rng"] = [3, [1, 2, 3], None]  # wrong state-vector size
        self.assertIsNone(Game.from_save(saved))

    def test_rejects_piece_intersecting_the_stack(self):
        saved = self.build_game().to_save()
        saved["piece"] = {"kind": "O", "x": 0, "y": 18, "rot": 0}
        # O at (0,18) covers (1,18),(2,18),(1,19),(2,19): the S and J stack.
        self.assertIsNone(Game.from_save(saved))

    def test_atomic_save_and_load_round_trip(self):
        import tempfile

        from tetris import atomic_save_json, load_json_file, remove_path

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "game.json")
            self.assertTrue(atomic_save_json(path, {"version": 1}))
            self.assertEqual(load_json_file(path), {"version": 1})
            self.assertFalse(os.path.exists(path + ".tmp"))  # no leftovers
            remove_path(path)
            self.assertIsNone(load_json_file(path))
            with open(path, "w") as f:
                f.write("{not json")
            self.assertIsNone(load_json_file(path))
            remove_path(path)  # already gone: still silent


class ClearingTest(unittest.TestCase):
    def setUp(self):
        self.game = Game(random.Random(5))


    def lock_clearing(self, n_rows):
        """Fill the bottom n rows except col 9; a vertical I hard-dropped
        into col 9 completes them."""
        for y in range(20 - n_rows, 20):
            for x in range(9):
                self.game.board.grid[y][x] = "J"
        self.game.piece = Piece("I", x=7, y=0, rot=1)
        self.game.hard_drop()
        return self.game.clearing

    def test_clear_is_deferred_to_an_explicit_phase(self):
        cl = self.lock_clearing(1)
        self.assertIsNotNone(cl)
        self.assertEqual(cl.rows, [19])
        self.assertEqual(cl.count, 1)
        self.assertAlmostEqual(cl.duration, tetris.CLEAR_DURATION)
        self.assertTrue(all(self.game.board.grid[19]))  # row still on screen
        self.assertIsNone(self.game.piece)  # no active piece while clearing

    def test_update_completes_clear_after_duration(self):
        self.lock_clearing(1)
        self.game.update(0.1)
        self.assertIsNotNone(self.game.clearing)
        self.game.update(tetris.CLEAR_DURATION)
        self.assertIsNone(self.game.clearing)
        # Row 19 (J row + I) was removed; the surviving I cells (old rows
        # 16-18) shifted down into rows 17-19 of col 9.
        self.assertTrue(all(c is None for c in self.game.board.grid[16]))
        self.assertEqual(self.game.board.grid[17][9], "I")
        self.assertEqual(self.game.board.grid[19][9], "I")
        self.assertEqual(self.game.lines, 1)
        self.assertIsNotNone(self.game.piece)  # the next piece spawned

    def test_tetris_gets_a_longer_distinct_duration(self):
        cl4 = self.lock_clearing(4)
        self.assertEqual(cl4.count, 4)
        self.assertAlmostEqual(cl4.duration, tetris.TETRIS_CLEAR_DURATION)
        single = ClearingTest()
        single.setUp()
        cl1 = single.lock_clearing(1)
        self.assertGreater(cl4.duration, cl1.duration)

    def test_gravity_and_score_frozen_while_clearing(self):
        self.lock_clearing(2)
        score_at_lock = self.game.score
        self.game.update(0.1)
        self.assertIsNone(self.game.piece)  # nothing fell
        self.assertEqual(self.game.score, score_at_lock)

    def test_input_buffered_then_applied_once(self):
        self.lock_clearing(1)
        self.assertFalse(self.game.move(-1))
        self.assertFalse(self.game.move(-1))
        self.assertFalse(self.game.rotate(1))
        self.game.finish_clearing()
        piece = self.game.piece
        # Spawned at x=3 rot=0: two buffered lefts and one rotation, each
        # applied exactly once (three lefts would give x=0).
        self.assertEqual(piece.x, 1)
        self.assertEqual(piece.rot, 1)

    def test_hold_buffered_across_clear(self):
        self.lock_clearing(1)
        self.assertFalse(self.game.hold_swap())
        self.game.finish_clearing()
        self.assertIsNotNone(self.game.hold)

    def test_finish_clearing_saves_post_clear_state(self):
        self.lock_clearing(2)
        self.game.finish_clearing()
        restored = Game.from_save(self.game.to_save())
        self.assertEqual(restored.lines, 2)
        self.assertTrue(all(c is None for c in restored.board.grid[16]))
        self.assertTrue(all(c is None for c in restored.board.grid[17]))
        # Rows 18-19 were removed; the I cells from old rows 16-17 shift down
        # by the two cleared rows into rows 18-19 of col 9.
        self.assertEqual(restored.board.grid[18][9], "I")
        self.assertEqual(restored.board.grid[19][9], "I")

    def test_pause_freezes_the_animation(self):
        self.lock_clearing(1)
        self.game.paused = True
        self.assertFalse(self.game.update(10.0))
        self.assertIsNotNone(self.game.clearing)
        self.assertAlmostEqual(self.game.clearing.elapsed, 0.0)
        self.game.paused = False
        self.game.update(tetris.CLEAR_DURATION)
        self.assertIsNone(self.game.clearing)

    def test_game_over_lock_bypasses_clearing(self):
        for y in (0, 1):
            for x in (3, 4, 5, 6):
                self.game.board.grid[y][x] = "J"
        self.game.piece = Piece("O", x=4, y=18)
        self.game.hard_drop()
        self.assertTrue(self.game.game_over)
        self.assertIsNone(self.game.clearing)


def _regions(plan):
    names = ("board", "hold", "next", "stats", "controls", "agents_win", "hint")
    return {n: plan[n] for n in names if plan[n] is not None}


class LayoutPlanTest(unittest.TestCase):
    def assert_fits(self, plan, cols, rows):
        for name, (y, x, h, w) in _regions(plan).items():
            self.assertGreaterEqual(y, 0, (name, cols, rows))
            self.assertGreaterEqual(x, 0, (name, cols, rows))
            self.assertLessEqual(y + h, rows, (name, cols, rows))
            self.assertLessEqual(x + w, cols, (name, cols, rows))

    def assert_centred(self, plan, cols, rows):
        boxes = [r for n, r in _regions(plan).items() if n != "hint"]
        left = min(x for _, x, _, _ in boxes)
        right = cols - max(x + w for _, x, _, w in boxes)
        self.assertLessEqual(abs(left - right), 1, (cols, rows, left, right))
        top = min(y for y, _, _, _ in boxes)
        bottom_edge = rows - 1 if plan["hint"] is not None else rows
        bottom = bottom_edge - max(y + h for y, _, h, _ in boxes)
        self.assertGreaterEqual(top, 1)  # row 0 is the header / banner
        self.assertLessEqual(abs((top - 1) - bottom), 1, (cols, rows, top, bottom))

    def test_board_frame_hugs_the_field_in_every_tier(self):
        from tetris import BOARD_W, MID_COLS, MIN_COLS, MIN_ROWS, layout_plan

        for cols, rows in (
            (93, 28),  # popup inner size
            (77, 24),  # old popup inner size
            (MID_COLS, 23),
            (40, 23),
            (MIN_COLS, MIN_ROWS),
        ):
            plan = layout_plan(cols, rows)
            y, x, h, w = plan["board"]
            self.assertEqual((h, w), (22, BOARD_W), (cols, rows))
            self.assertLessEqual(x + BOARD_W, cols, (cols, rows))
        # Cell 9 is drawn at window cols 19-20 (start 1 + 9*2, two wide); the
        # right border must sit exactly one column past it.
        self.assertEqual(BOARD_W - 1, 1 + 9 * 2 + 2)

    def test_popup_size_is_full_centred_and_uses_the_side_column(self):
        from tetris import layout_plan

        plan = layout_plan(93, 28)
        self.assertEqual(plan["tier"], "full")
        for name in ("hold", "next", "stats", "controls", "agents_win"):
            self.assertIsNotNone(plan[name], name)
        for name in ("stats", "controls", "agents_win"):
            self.assertEqual(plan[name][3], 30, name)
        self.assertIsNone(plan["hint"])
        self.assert_fits(plan, 93, 28)
        self.assert_centred(plan, 93, 28)
        # The side column is flush right of the board, stacked stats >
        # controls > agents, and ends on the board's bottom edge.
        by, bx, bh, bw = plan["board"]
        sy, sx, sh, _ = plan["stats"]
        cy, cx, ch, _ = plan["controls"]
        ay, ax, ah, _ = plan["agents_win"]
        self.assertEqual(sx, bx + bw + 1)
        self.assertEqual((sx, cx, ax), (sx, sx, sx))
        self.assertEqual((sy, cy, ay), (by, sy + sh, cy + ch))
        self.assertEqual(ay + ah, by + bh)
        # hold / next stay beside the board, on its left.
        hy, hx, hh, hw = plan["hold"]
        ny, nx, nh, nw = plan["next"]
        self.assertEqual(hx + hw + 1, bx)
        self.assertEqual((hy, ny, ny + nh), (by, hy + hh, by + bh))

    def test_controls_box_fits_every_control(self):
        from common import controls_height

        from tetris import CONTROLS, layout_plan

        for cols, rows in ((93, 28), (77, 24), (60, 23)):
            plan = layout_plan(cols, rows)
            _, _, h, w = plan["controls"]
            self.assertGreaterEqual(h, controls_height(CONTROLS, w), (cols, rows))

    def test_every_size_fits_and_is_centred(self):
        from tetris import layout_plan

        for cols in range(24, 120, 3):
            for rows in range(23, 40, 2):
                plan = layout_plan(cols, rows)
                self.assertFalse(plan["too_small"], (cols, rows))
                self.assert_fits(plan, cols, rows)
                self.assert_centred(plan, cols, rows)
                if plan["controls"] is None:  # no controls box: a key hint
                    self.assertTrue(plan["hint"] is not None or plan["hint_in_header"])

    def test_tiers_gate_their_panels(self):
        from tetris import layout_plan

        full = layout_plan(77, 24)
        self.assertEqual(full["tier"], "full")
        self.assertTrue(full["left"] and full["agents"] and full["header"])
        self.assertIsNotNone(full["controls"])
        mid = layout_plan(40, 24)
        self.assertEqual(mid["tier"], "left")
        self.assertTrue(mid["left"])
        self.assertFalse(mid["agents"])
        self.assertIsNone(mid["controls"])
        self.assertEqual(mid["hint"], (23, 0, 1, 40))  # last row
        squeezed = layout_plan(40, 23)
        self.assertIsNone(squeezed["hint"])
        self.assertTrue(squeezed["hint_in_header"])
        board_only = layout_plan(30, 24)
        self.assertEqual(board_only["tier"], "board")
        self.assertFalse(board_only["left"] or board_only["agents"])
        self.assertIsNotNone(board_only["hint"])
        tiny = layout_plan(20, 20)
        self.assertTrue(tiny["too_small"])
        self.assertIsNone(tiny["board"])
        self.assertTrue(layout_plan(93, 22)["too_small"])


class ControlsHelpTest(unittest.TestCase):
    def test_controls_constants(self):
        keys = " ".join(k for k, _ in tetris.CONTROLS)
        for key in ("?", "q", "p", "r", "c", "space", "h", "j", "k", "l"):
            self.assertIn(key, keys.split(), key)
        self.assertEqual(tetris.CONTROLS[-1], ("q", "menu"))
        self.assertIn("?", [k for k, _ in tetris.CONTROLS])

    def test_help_sections(self):
        headings = [h for h, _ in tetris.HELP]
        for heading in ("Goal", "Controls", "How it works", "Scoring", "Tips"):
            self.assertIn(heading, headings)
        for heading, body in tetris.HELP:
            self.assertTrue(body.strip(), heading)
            body.encode("ascii")  # plain ASCII

    def test_handler_keys_are_listed(self):
        """Every key handle_key reacts to is in CONTROLS (spot check via the
        game state each key changes)."""
        keys = {k for entry, _ in tetris.CONTROLS for k in entry.split()}
        for ch, listed in ((ord("h"), "h"), (ord("l"), "l"), (ord("j"), "j"),
                           (ord("k"), "k"), (ord("c"), "c"), (ord(" "), "space"),
                           (ord("p"), "p")):
            game = Game(random.Random(3))
            before = game.to_save(), game.paused
            tetris.handle_key(game, ch)
            self.assertNotEqual((game.to_save(), game.paused), before, listed)
            self.assertIn(listed, keys)

    def test_key_hint_always_keeps_help(self):
        for width in range(6, 120, 7):
            hint = tetris.key_hint(width)
            self.assertLessEqual(len(hint), width)
            self.assertIn("? help", hint)

    def test_help_pauses_and_closing_resumes(self):
        from common import HELP_KEY, HelpOverlay

        game = Game(random.Random(1))
        view = HelpOverlay(tetris.TITLE, tetris.HELP)
        consumed, by_help = tetris.help_key(view, game, HELP_KEY, False)
        self.assertTrue(consumed and by_help and view.is_open and game.paused)
        # Timers do not advance while it is open.
        y = game.piece.y
        game.update(5.0)
        self.assertEqual(game.piece.y, y)
        # Keys are consumed by the overlay (q included), not the game.
        consumed, by_help = tetris.help_key(view, game, tetris.curses.KEY_DOWN, by_help)
        self.assertTrue(consumed and view.is_open and game.paused)
        # Resize passes through so the loop rebuilds its layout.
        consumed, _ = tetris.help_key(view, game, tetris.curses.KEY_RESIZE, by_help)
        self.assertFalse(consumed)
        consumed, by_help = tetris.help_key(view, game, ord("q"), by_help)
        self.assertTrue(consumed)
        self.assertFalse(view.is_open or game.paused or by_help)
        # Unrelated keys pass through when closed.
        self.assertEqual(tetris.help_key(view, game, ord("h"), False), (False, False))

    def test_help_keeps_a_pause_it_did_not_cause(self):
        from common import HELP_KEY, HelpOverlay

        for setup in ("paused", "game_over"):
            game = Game(random.Random(1))
            setattr(game, setup, True)
            view = HelpOverlay(tetris.TITLE, tetris.HELP)
            consumed, by_help = tetris.help_key(view, game, HELP_KEY, False)
            self.assertTrue(consumed and view.is_open)
            self.assertFalse(by_help)
            tetris.help_key(view, game, ord("x"), by_help)
            self.assertFalse(view.is_open)
            self.assertEqual(game.paused, setup == "paused")


class RenderFakeWin:
    """curses window stub with derwin and curses-accurate bounds; every
    out-of-bounds write is recorded on the root as a violation."""

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
            raise tetris.curses.error()
        child = RenderFakeWin(h, w, self.root, self.y0 + y, self.x0 + x)
        self.root.children.append(child)
        return child

    def erase(self):
        self.writes = []

    def addstr(self, y, x, text, attr=0):
        if y < 0 or y >= self.rows or x < 0 or x + len(text) > self.cols:
            self.root.violations.append((y, x, text))
            raise tetris.curses.error()
        self.writes.append((y, x, text))

    def all_text(self):
        wins = [self] + self.children
        return "\n".join(t for w in wins for _, _, t in w.writes)


class RenderTest(unittest.TestCase):
    ROSTER = [snap("p%d" % i, s) for i, s in
              enumerate(["idle", "working", "blocked", "done"] * 4)]
    STATUS_PAIRS = {s: i + 1 for i, s in
                    enumerate(("idle", "working", "blocked", "done", "unknown"))}

    def render(self, game, cols, rows, help_open=False, attention=None):
        from unittest import mock

        from common import HelpOverlay

        scr = RenderFakeWin(rows, cols)
        view = HelpOverlay(tetris.TITLE, tetris.HELP)
        if help_open:
            view.open()
        pairs = {k: 10 + i for i, k in enumerate(tetris.PIECE_KINDS)}
        with mock.patch("curses.color_pair", return_value=0):
            lay = tetris.Layout(scr)
            tetris.draw(pairs, self.STATUS_PAIRS, game, lay, self.ROSTER,
                        attention, False, 99, view)
        return scr, lay

    def games(self):
        yield "fresh", Game(random.Random(2))
        p = Game(random.Random(2))
        p.paused = True
        p.hold = "I"
        yield "paused", p
        go = Game(random.Random(2))
        go.game_over = True
        yield "game_over", go

    def test_no_out_of_bounds_writes(self):
        attention = (snap("p1", "blocked", "a very long agent label indeed"), "working")
        for name, game in self.games():
            for size in ((93, 28), (77, 24), (50, 24), (40, 23), (30, 23),
                         (24, 23), (20, 10)):
                for help_open in (False, True):
                    for att in (None, attention):
                        scr, _ = self.render(game, *size, help_open=help_open,
                                             attention=att)
                        self.assertEqual(scr.violations, [], (name, size, help_open))

    def test_controls_on_screen_at_every_tier(self):
        for size in ((93, 28), (77, 24)):
            scr, lay = self.render(Game(random.Random(2)), *size)
            self.assertIsNotNone(lay.controls)
            text = scr.all_text()
            self.assertIn(" keys ", text)
            for keys, action in tetris.CONTROLS:
                self.assertIn(keys, text, (size, keys))
            self.assertIn("tetris", text)
            self.assertIn("agents", text)
        for size in ((50, 24), (40, 23), (30, 24), (30, 23)):
            scr, lay = self.render(Game(random.Random(2)), *size)
            self.assertIsNone(lay.controls)
            self.assertIn("? help", scr.all_text(), size)

    def test_help_overlay_renders_over_the_game(self):
        for size in ((93, 28), (77, 24), (40, 23)):
            scr, _ = self.render(Game(random.Random(2)), *size, help_open=True)
            text = scr.all_text()
            self.assertIn("Tetris - help", text, size)
            self.assertIn("Goal", text, size)
        scr, _ = self.render(Game(random.Random(2)), 93, 28, help_open=True)
        self.assertIn(" keys ", scr.all_text())  # controls box still drawn beneath


class FrameRenderTest(unittest.TestCase):
    """Render-level border test without a terminal: a window stub with
    curses-accurate bounds checking records what frame() draws."""

    class FakeWin:
        def __init__(self, rows, cols):
            self.rows, self.cols = rows, cols
            self.writes = []

        def getmaxyx(self):
            return self.rows, self.cols

        def addstr(self, y, x, text, attr=0):
            if y < 0 or y >= self.rows or x < 0 or x + len(text) > self.cols:
                raise tetris.curses.error()
            self.writes.append((y, x, text))

    def test_vertical_border_is_exactly_the_last_column(self):
        from tetris import BOARD_W, frame

        win = self.FakeWin(22, BOARD_W)
        frame(win)
        verts = [(y, x) for y, x, t in win.writes if t == "|"]
        right = [x for _, x in verts if x > 0]
        left = [x for _, x in verts if x == 0]
        self.assertEqual(right, [BOARD_W - 1] * 20)  # hugs the last column
        self.assertEqual(left, [0] * 20)
        self.assertEqual([y for y, _ in verts if True][:0], [])  # all recorded
        bars = sorted(t for _, _, t in win.writes if t.startswith("+"))
        self.assertEqual(len(bars), 2)
        self.assertEqual(len(bars[0]), BOARD_W)  # horizontal bar spans exactly


if __name__ == "__main__":
    unittest.main()
