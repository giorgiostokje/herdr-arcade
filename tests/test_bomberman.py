"""Unit tests for the pure game logic, layout and rendering in bomberman.py
(no terminal needed)."""

import json
import os
import random
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bomberman  # noqa: E402
from common import AgentSnapshot  # noqa: E402
from bomberman import (  # noqa: E402
    BURN,
    DOWN,
    EMPTY,
    FIELD_H,
    FIELD_W,
    FLAME_TIME,
    FUSE,
    ITEM_SPAWN,
    LEFT,
    LIVES,
    PILLAR,
    RIGHT,
    SAFE_CELLS,
    SOFT,
    START,
    UP,
    Bomb,
    Enemy,
    Game,
    base_grid,
    generate_stage,
    layout_plan,
)


def new_game(seed=1):
    return Game(random.Random(seed))


def blank_game(seed=1):
    """A game on a bare pillar grid, in play (no intro): no soft blocks, no
    monsters, no power-up, the exit tucked under a soft block in the far
    corner. Tests then place exactly what they need."""
    g = new_game(seed)
    g.grid = base_grid()
    g.exit = (FIELD_W - 1, FIELD_H - 1)
    g.grid[FIELD_H - 1][FIELD_W - 1] = SOFT
    g.enemies = []
    g.powerups = {}
    g.player = (4, 4)
    g.phase = None
    return g


def put(g, x, y, kind=SOFT):
    g.grid[y][x] = kind


def run_for(g, seconds, dt=0.01):
    t = 0.0
    while t < seconds - 1e-9:
        g.update(dt)
        t += dt


def far_enemy(kind="drifter"):
    """A monster boxed in far away, so the stage is never clear by accident."""
    return Enemy(FIELD_W - 1, 0, kind)


def box_in(g, e):
    """Soft blocks around a monster so it cannot wander."""
    for dx, dy in bomberman.DELTAS.values():
        x, y = e.x + dx, e.y + dy
        if bomberman.in_bounds(x, y) and g.grid[y][x] == EMPTY:
            put(g, x, y)


def with_far_enemy(g):
    e = far_enemy()
    g.enemies = [e]
    box_in(g, e)
    return e


class StageTest(unittest.TestCase):
    def test_pillar_lattice(self):
        grid = base_grid()
        for y in range(FIELD_H):
            for x in range(FIELD_W):
                self.assertEqual(grid[y][x] == PILLAR, x % 2 == 1 and y % 2 == 1)
        # Odd dimensions: the outer ring is open all round.
        self.assertEqual(FIELD_W % 2, 1)
        self.assertEqual(FIELD_H % 2, 1)

    def test_generated_stages(self):
        for seed in range(30):
            for stage in (1, 4, 9):
                rng = random.Random(seed)
                grid, enemies, powerups, exit_cell = generate_stage(rng, stage)
                for x, y in SAFE_CELLS:
                    self.assertEqual(grid[y][x], EMPTY)
                self.assertEqual(sorted(e.kind for e in enemies),
                                 sorted(bomberman.stage_roster(stage)))
                cells = [e.pos for e in enemies]
                self.assertEqual(len(set(cells)), len(cells))
                for x, y in cells:
                    self.assertEqual(grid[y][x], EMPTY)
                    self.assertGreaterEqual(abs(x) + abs(y), bomberman.ENEMY_MIN_DIST)
                (item, kind), = powerups.items()
                self.assertEqual(kind, bomberman.stage_powerup(stage))
                self.assertNotEqual(item, exit_cell)
                self.assertEqual(grid[item[1]][item[0]], SOFT)
                self.assertEqual(grid[exit_cell[1]][exit_cell[0]], SOFT)
                soft = sum(row.count(SOFT) for row in grid)
                self.assertGreater(soft, 40)

    def test_deterministic_for_seed(self):
        self.assertEqual(new_game(7).to_save(), new_game(7).to_save())
        self.assertNotEqual(new_game(7).to_save()["grid"], new_game(8).to_save()["grid"])

    def test_rosters_grow(self):
        order = bomberman.ENEMY_ORDER
        tough = [max(order.index(k) for k in bomberman.stage_roster(s)) for s in range(1, 12)]
        self.assertEqual(tough, sorted(tough))
        self.assertGreater(tough[-1], tough[0])
        self.assertGreaterEqual(len(bomberman.stage_roster(20)), len(bomberman.stage_roster(1)))
        self.assertLessEqual(len(bomberman.stage_roster(200)), 10)
        self.assertNotIn("reaper", bomberman.stage_roster(50))
        self.assertIn("remote", bomberman.POWERUP_CYCLE)
        self.assertNotIn("remote", bomberman.POWERUP_CYCLE[:3])  # a later prize

    def test_new_game_opens_with_intro(self):
        g = new_game()
        self.assertEqual(g.phase.kind, "stage_start")
        self.assertFalse(g.move(RIGHT))
        g.update(bomberman.STAGE_START_TIME + 0.01)
        self.assertIsNone(g.phase)
        self.assertEqual(g.player, START)


class MovementTest(unittest.TestCase):
    def test_step_and_rate_limit(self):
        g = blank_game()
        self.assertTrue(g.move(RIGHT))
        self.assertEqual(g.player, (5, 4))
        # A second key mid-step is buffered, then taken when the step ends.
        self.assertFalse(g.move(RIGHT))
        self.assertEqual(g.player, (5, 4))
        g.update(g.step_interval() + 0.01)
        self.assertEqual(g.player, (6, 4))

    def test_pillars_soft_blocks_and_walls_block(self):
        g = blank_game()
        g.player = (4, 4)
        put(g, 4, 3)
        self.assertFalse(g.move(UP))       # soft block
        g.player = (5, 4)
        self.assertFalse(g.move(UP))       # pillar at (5, 3)
        g.player = (0, 0)
        self.assertFalse(g.move(LEFT))
        self.assertFalse(g.move(UP))
        self.assertEqual(g.player, (0, 0))
        self.assertEqual(g.move_cd, 0)     # a blocked key costs no time

    def test_speed_power_up_shortens_steps(self):
        g = blank_game()
        slow = g.step_interval()
        g.speed = 1
        self.assertLess(g.step_interval(), slow)
        self.assertGreater(bomberman.PLAYER_INTERVALS[-1], 0)

    def test_wallpass(self):
        g = blank_game()
        put(g, 4, 3)
        g.wallpass = True
        self.assertTrue(g.move(UP))
        self.assertEqual(g.player, (4, 3))

    def test_no_move_while_paused(self):
        g = blank_game()
        g.paused = True
        self.assertFalse(g.move(RIGHT))
        self.assertFalse(g.drop_bomb())
        self.assertEqual(g.player, (4, 4))


class BombTest(unittest.TestCase):
    def test_drop_limits(self):
        g = blank_game()
        with_far_enemy(g)
        self.assertTrue(g.drop_bomb())
        self.assertFalse(g.drop_bomb())     # one bomb at a time
        g.max_bombs = 2
        self.assertFalse(g.drop_bomb())     # never two on one cell
        g.move(RIGHT)
        g.move_cd = 0
        self.assertTrue(g.drop_bomb())
        self.assertEqual(len(g.bombs), 2)

    def test_can_leave_own_bomb_but_not_return(self):
        g = blank_game()
        with_far_enemy(g)
        g.drop_bomb()
        self.assertTrue(g.move(RIGHT))
        g.move_cd = 0
        self.assertFalse(g.move(LEFT))
        self.assertEqual(g.player, (5, 4))
        g.bombpass = True
        self.assertTrue(g.move(LEFT))

    def test_fuse_then_cross_of_flames(self):
        g = blank_game()
        with_far_enemy(g)
        g.player = (2, 2)
        g.flame = 2
        g.drop_bomb()
        g.player = (0, 0)  # out of the way
        g.update(FUSE - 0.05)
        self.assertEqual(len(g.bombs), 1)
        g.update(0.1)
        self.assertEqual(g.bombs, [])
        flames = set(g.flames)
        # Row 2 is open: two cells each way. Column 2 as well.
        self.assertEqual(flames, {(2, 2), (0, 2), (1, 2), (3, 2), (4, 2),
                                  (2, 0), (2, 1), (2, 3), (2, 4)})
        self.assertEqual(g.flames[(2, 2)][1], "c")
        self.assertEqual(g.flames[(1, 2)][1], "h")
        self.assertEqual(g.flames[(2, 1)][1], "v")
        g.update(FLAME_TIME + 0.05)
        self.assertEqual(g.flames, {})

    def test_flames_stop_at_pillars(self):
        g = blank_game()
        with_far_enemy(g)
        g.flame = 4
        g.bombs = [Bomb(3, 2, 0.001)]
        g.player = (0, 0)
        g.update(0.01)
        # (3, 1) and (3, 3) are pillars: no flames above or below.
        self.assertNotIn((3, 1), g.flames)
        self.assertNotIn((3, 0), g.flames)
        self.assertIn((7, 2), g.flames)

    def test_first_soft_block_burns_the_next_survives(self):
        g = blank_game()
        with_far_enemy(g)
        g.flame = 5
        put(g, 4, 2)
        put(g, 6, 2)
        g.bombs = [Bomb(2, 2, 0.001)]
        g.player = (0, 0)
        g.update(0.01)
        self.assertEqual(g.grid[2][4], BURN)
        self.assertEqual(g.grid[2][6], SOFT)
        self.assertNotIn((5, 2), g.flames)
        g.update(FLAME_TIME + 0.05)
        self.assertEqual(g.grid[2][4], EMPTY)

    def test_chain_reaction_is_one_blast(self):
        g = blank_game()
        with_far_enemy(g)
        g.flame = 2
        g.bombs = [Bomb(2, 2, 0.001), Bomb(4, 2, 10.0), Bomb(4, 4, 10.0)]
        g.player = (0, 0)
        g.update(0.01)
        self.assertEqual(g.bombs, [])
        self.assertIn((6, 2), g.flames)   # from the chained (4, 2)
        self.assertIn((4, 6), g.flames)   # from the chained (4, 4)
        self.assertEqual(len({f[2] for f in g.flames.values()}), 1)

    def test_flames_kill_the_player(self):
        g = blank_game()
        with_far_enemy(g)
        g.drop_bomb()
        g.update(FUSE + 0.05)
        self.assertEqual(g.phase.kind, "dying")
        self.assertEqual(g.lives, LIVES - 1)

    def test_walking_into_flames_kills(self):
        g = blank_game()
        with_far_enemy(g)
        g.flames[(5, 4)] = [FLAME_TIME, "h", 1]
        g.groups[1] = 0
        g.move(RIGHT)
        self.assertEqual(g.phase.kind, "dying")

    def test_remote_detonator(self):
        g = blank_game()
        with_far_enemy(g)
        g.remote = True
        g.max_bombs = 2
        g.drop_bomb()
        g.player = (0, 0)
        g.drop_bomb()
        g.player = (0, 4)
        run_for(g, FUSE * 2, 0.05)
        self.assertEqual(len(g.bombs), 2)       # no fuse: they wait
        self.assertTrue(g.detonate())
        self.assertEqual([b.pos for b in g.bombs], [(0, 0)])  # oldest first
        self.assertIn((4, 4), g.flames)
        self.assertFalse(blank_game().detonate())  # nothing to set off


class EnemyTest(unittest.TestCase):
    def test_moves_at_its_own_rate(self):
        g = blank_game()
        e = Enemy(10, 10, "drifter", RIGHT)
        g.enemies = [e]
        g.update(bomberman.ENEMY_TYPES["drifter"]["interval"] * 0.9)
        self.assertEqual(e.pos, (10, 10))
        g.update(bomberman.ENEMY_TYPES["drifter"]["interval"] * 0.2)
        self.assertNotEqual(e.pos, (10, 10))
        fast = [bomberman.ENEMY_TYPES[k]["interval"] for k in ("drifter", "darter")]
        self.assertLess(fast[1], fast[0])

    def test_hunter_chases_when_close(self):
        g = blank_game()
        g.player = (4, 4)
        e = Enemy(8, 4, "hunter", UP)
        g.enemies = [e]
        with mock.patch.object(g.rng, "random", return_value=0.0):
            g._enemy_step(e)
        self.assertEqual(e.pos, (7, 4))

    def test_hunter_wanders_when_far(self):
        g = blank_game()
        g.player = (0, 0)
        e = Enemy(20, 10, "hunter", RIGHT)
        g.enemies = [e]
        with mock.patch.object(g.rng, "random", return_value=0.99):
            g._enemy_step(e)
        self.assertEqual(e.pos, (21, 10))  # kept going, not toward the player

    def test_blocked_by_soft_blocks_and_bombs(self):
        g = blank_game()
        g.player = (0, 0)
        e = Enemy(10, 10, "drifter", RIGHT)
        put(g, 11, 10)
        put(g, 10, 9)
        g.bombs = [Bomb(10, 11, 99.0), Bomb(9, 10, 99.0)]
        g.enemies = [e]
        g._enemy_step(e)
        self.assertEqual(e.pos, (10, 10))

    def test_phantom_floats_through_soft_blocks(self):
        g = blank_game()
        g.player = (4, 4)
        e = Enemy(6, 4, "phantom", LEFT)
        put(g, 5, 4)
        g.enemies = [e]
        with mock.patch.object(g.rng, "random", return_value=0.0):
            g._enemy_step(e)
        self.assertEqual(e.pos, (5, 4))

    def test_monster_touch_kills(self):
        g = blank_game()
        e = Enemy(5, 4, "hunter", LEFT)
        g.enemies = [e]
        with mock.patch.object(g.rng, "random", return_value=0.0):
            g._enemy_step(e)
        self.assertEqual(g.phase.kind, "dying")
        g = blank_game()
        g.enemies = [Enemy(5, 4, "drifter", UP)]
        g.move(RIGHT)
        self.assertEqual(g.phase.kind, "dying")

    def test_monster_walking_into_flames_dies(self):
        g = blank_game()
        g.player = (0, 0)
        e = Enemy(10, 10, "drifter", RIGHT)
        keep = far_enemy()
        g.enemies = [e, keep]
        box_in(g, keep)
        g.flames[(11, 10)] = [FLAME_TIME, "h", 1]
        g.groups[1] = 0
        with mock.patch.object(g.rng, "random", return_value=0.99):
            g._enemy_step(e)
        self.assertNotIn(e, g.enemies)
        self.assertEqual(g.score, 100)


class ScoringTest(unittest.TestCase):
    def test_multiplier_table(self):
        self.assertEqual([bomberman.multiplier(k) for k in range(1, 7)],
                         [1, 2, 4, 8, 8, 8])

    def test_one_blast_multiplies(self):
        g = blank_game()
        with_far_enemy(g)
        g.flame = 4
        g.player = (0, 0)
        g.enemies += [Enemy(4, 2, "drifter"), Enemy(6, 2, "drifter"),
                      Enemy(2, 4, "darter")]
        g.bombs = [Bomb(2, 2, 0.001)]
        g.update(0.01)
        self.assertEqual(len(g.enemies), 1)
        # Kills are credited in list order: 100 x1, 100 x2, 200 x4.
        self.assertEqual(g.score, 100 + 200 + 800)

    def test_separate_blasts_do_not_multiply(self):
        g = blank_game()
        with_far_enemy(g)
        g.player = (0, 0)
        g.enemies += [Enemy(3, 2, "drifter"), Enemy(3, 6, "drifter")]
        g.bombs = [Bomb(2, 2, 0.001), Bomb(2, 6, 0.3)]
        run_for(g, 0.5)
        self.assertEqual(g.score, 200)

    def test_power_up_pickup(self):
        g = blank_game()
        for kind, check in (("bomb", lambda: g.max_bombs == 2),
                            ("flame", lambda: g.flame == 2),
                            ("speed", lambda: g.speed == 1),
                            ("remote", lambda: g.remote),
                            ("bombpass", lambda: g.bombpass),
                            ("wallpass", lambda: g.wallpass)):
            g.player, g.move_cd = (4, 4), 0
            g.powerups = {(5, 4): kind}
            before = g.score
            g.move(RIGHT)
            self.assertTrue(check(), kind)
            self.assertEqual(g.powerups, {})
            self.assertEqual(g.score - before, bomberman.POWERUP_POINTS)

    def test_hidden_power_up_is_not_taken(self):
        g = blank_game()
        g.wallpass = True
        put(g, 5, 4)
        g.powerups = {(5, 4): "flame"}
        g.move(RIGHT)
        self.assertEqual(g.flame, 1)
        self.assertIn((5, 4), g.powerups)


class ItemBombingTest(unittest.TestCase):
    def test_bombing_an_exposed_exit_spawns_monsters(self):
        g = blank_game()
        with_far_enemy(g)
        g.exit = (4, 2)
        g.flame = 2
        g.player = (0, 0)
        g.bombs = [Bomb(2, 2, 0.001)]
        g.update(0.01)
        self.assertEqual(g.remaining(), 1 + ITEM_SPAWN)
        self.assertEqual(len(g.enemies), 1)          # not yet: flames first
        run_for(g, bomberman.SPAWN_DELAY + 0.02)
        spawned = [e for e in g.enemies if e.kind == bomberman.spawn_kind(1)]
        self.assertEqual(len(spawned), ITEM_SPAWN)
        self.assertEqual(g.exit, (4, 2))              # the exit survives

    def test_bombing_an_exposed_power_up_destroys_it(self):
        g = blank_game()
        with_far_enemy(g)
        g.powerups = {(2, 3): "bomb"}
        g.player = (0, 0)
        g.bombs = [Bomb(2, 2, 0.001)]
        g.update(0.01)
        self.assertEqual(g.powerups, {})
        self.assertEqual(len(g.pending), 1)

    def test_blast_that_uncovers_items_does_not_spawn(self):
        g = blank_game()
        with_far_enemy(g)
        g.exit = (4, 2)
        put(g, 4, 2)
        g.flame = 2
        g.player = (0, 0)
        g.bombs = [Bomb(2, 2, 0.001)]
        g.update(0.01)
        run_for(g, FLAME_TIME + 0.1)
        self.assertEqual(g.pending, [])
        self.assertEqual(g.grid[2][4], EMPTY)        # the exit is uncovered
        self.assertEqual(g.remaining(), 1)

    def test_spawn_kind_is_tougher(self):
        order = bomberman.ENEMY_ORDER
        for stage in range(1, 15):
            top = max(order.index(k) for k in bomberman.stage_roster(stage))
            self.assertGreaterEqual(order.index(bomberman.spawn_kind(stage)), top)
            self.assertNotEqual(bomberman.spawn_kind(stage), "reaper")


class ExitTest(unittest.TestCase):
    def test_exit_shut_while_monsters_live(self):
        g = blank_game()
        with_far_enemy(g)
        g.exit = (5, 4)
        g.move(RIGHT)
        self.assertEqual(g.player, (5, 4))
        self.assertIsNone(g.phase)

    def test_full_stage_clear(self):
        """Bomb the last monster from around a corner, then walk onto the
        exit: the stage clears and the next one starts."""
        g = blank_game()
        g.exit = (8, 4)
        g.flame = 2
        e = Enemy(4, 2, "drifter", UP)
        g.enemies = [e]
        for x, y in ((4, 1), (3, 2), (5, 2)):   # it can only reach (4, 3)
            put(g, x, y)
        g.player = (4, 4)
        self.assertTrue(g.drop_bomb())
        for d in (LEFT, LEFT, UP):             # round the corner to (2, 3)
            g.move(d)
            run_for(g, 0.2)
        self.assertEqual(g.player, (2, 3))
        run_for(g, FUSE)
        self.assertIsNone(g.phase)
        self.assertEqual(g.enemies, [])
        self.assertEqual(g.score, 100)
        self.assertTrue(g.exit_open())
        run_for(g, FLAME_TIME)
        for d in (DOWN, RIGHT, RIGHT, RIGHT, RIGHT, RIGHT, RIGHT):
            g.move(d)
            run_for(g, 0.2)
        self.assertEqual(g.phase.kind, "stage_clear")
        run_for(g, bomberman.STAGE_CLEAR_TIME + 0.05)
        self.assertEqual(g.stage, 2)
        self.assertEqual(g.phase.kind, "stage_start")    # intro of stage 2
        self.assertEqual(sorted(e.kind for e in g.enemies),
                         sorted(bomberman.stage_roster(2)))
        self.assertEqual(g.player, START)
        self.assertEqual(g.flame, 2)                     # power carries over


class TimerTest(unittest.TestCase):
    def test_time_up_brings_reapers(self):
        g = blank_game()
        with_far_enemy(g)
        g.player = (14, 7)
        g.time_left = 0.05
        g.update(0.1)
        self.assertTrue(g.timeout)
        reapers = [e for e in g.enemies if e.kind == "reaper"]
        self.assertEqual(len(reapers), bomberman.TIMEOUT_WAVE)
        for e in reapers:
            self.assertGreaterEqual(bomberman.manhattan(e.pos, g.player), 3)
        g.enemies = [e for e in g.enemies if e.kind != "reaper"]
        g.flood_timer = 0.01
        g.update(0.02)
        self.assertEqual(sum(e.kind == "reaper" for e in g.enemies), 1)

    def test_timer_stops_while_paused(self):
        g = blank_game()
        g.paused = True
        g.update(5.0)
        self.assertEqual(g.time_left, bomberman.STAGE_TIME)


class DeathTest(unittest.TestCase):
    def test_death_restarts_the_stage_keeping_basic_power(self):
        g = blank_game()
        g.max_bombs, g.flame, g.speed = 3, 4, 1
        g.remote = g.bombpass = g.wallpass = True
        g.enemies = [Enemy(5, 4, "drifter")]
        g.move(RIGHT)
        self.assertEqual(g.phase.kind, "dying")
        self.assertFalse(g.move(LEFT))   # frozen while dying
        run_for(g, bomberman.DEATH_TIME + 0.05)
        self.assertEqual(g.phase.kind, "stage_start")
        self.assertEqual(g.lives, LIVES - 1)
        self.assertEqual(g.player, START)
        self.assertEqual((g.max_bombs, g.flame, g.speed), (3, 4, 1))
        self.assertFalse(g.remote or g.bombpass or g.wallpass)
        self.assertEqual(len(g.enemies), len(bomberman.stage_roster(1)))

    def test_lives_and_game_over(self):
        g = blank_game()
        for i in range(LIVES):
            g.phase = None
            g.player, g.move_cd = (4, 4), 0
            g.grid[4][5] = EMPTY
            g.enemies = [Enemy(5, 4, "drifter")]
            g.move(RIGHT)
            self.assertEqual(g.lives, LIVES - 1 - i)
            g.resolve_phases()
        self.assertTrue(g.game_over)
        self.assertEqual(g.lives, 0)
        self.assertFalse(g.update(1.0))
        self.assertFalse(g.move(LEFT))
        g.reset()
        self.assertFalse(g.game_over)
        self.assertEqual((g.lives, g.score, g.stage, g.max_bombs), (LIVES, 0, 1, 1))


class PlayTest(unittest.TestCase):
    def test_random_play_never_crashes(self):
        for seed in range(6):
            g = new_game(seed)
            r = random.Random(seed)
            for i in range(400):
                a = r.random()
                if a < 0.5:
                    g.move(r.choice(bomberman.DIRS))
                elif a < 0.56:
                    g.drop_bomb()
                elif a < 0.58:
                    g.detonate()
                g.update(r.choice((0.01, 0.05, 0.1, 0.25)))
                if i % 40 == 0 and g.phase is None and not g.game_over:
                    self.assertIsNotNone(
                        Game.from_save(json.loads(json.dumps(g.to_save()))), (seed, i))
                if g.game_over:
                    break

    def test_timeout_flood_is_capped(self):
        g = blank_game()
        with_far_enemy(g)
        g.player = (0, 0)
        g.time_left = 0.01
        g._die = lambda: None          # an immortal player waits it out
        for _ in range(300):
            g.update(1.0)
        reapers = sum(e.kind == "reaper" for e in g.enemies)
        self.assertEqual(reapers, bomberman.FLOOD_MAX)


class DisplayKeyTest(unittest.TestCase):
    def test_changes_with_second_fuse_and_blink(self):
        g = blank_game()
        with_far_enemy(g)
        k0 = bomberman.display_key(g, 0.0)
        g.update(0.5)
        self.assertEqual(bomberman.display_key(g, 0.0), k0)
        g.update(0.6)
        self.assertNotEqual(bomberman.display_key(g, 0.0), k0)
        g.drop_bomb()
        k1 = bomberman.display_key(g, 0.0)
        self.assertEqual(bomberman.bomb_glyph(g.bombs[0]), "O3")
        g.update(0.6)
        self.assertEqual(bomberman.bomb_glyph(g.bombs[0]), "O2")
        self.assertNotEqual(bomberman.display_key(g, 0.0), k1)
        self.assertNotEqual(bomberman.display_key(g, 0.0), bomberman.display_key(g, 0.3))


class SaveTest(unittest.TestCase):
    def advanced(self):
        g = new_game(5)
        g.resolve_phases()
        g.move(RIGHT)
        g.drop_bomb()
        g.max_bombs = 2
        g.move_cd = 0
        g.move(RIGHT)
        g.move(DOWN)  # buffered
        for _ in range(12):
            g.update(0.1)
        g.resolve_phases()
        g.score = 1234
        return g

    def test_round_trip_including_rng(self):
        g = self.advanced()
        data = json.loads(json.dumps(g.to_save()))
        r = Game.from_save(data)
        self.assertIsNotNone(r)
        self.assertEqual(r.to_save(), g.to_save())
        # Same RNG and timers: play continues identically.
        for i in range(300):
            if i % 25 == 0:
                g.drop_bomb()
                r.drop_bomb()
            if i % 7 == 0:
                d = bomberman.DIRS[i % 4]
                g.move(d)
                r.move(d)
            g.update(0.05)
            r.update(0.05)
        self.assertEqual(r.to_save(), g.to_save())

    def test_mid_blast_round_trip(self):
        g = blank_game()
        with_far_enemy(g)
        g.enemies.append(Enemy(8, 8, "darter"))
        g.player = (0, 0)
        g.bombs = [Bomb(2, 2, 0.001), Bomb(10, 10, 1.0)]
        g.exit = (3, 2)
        g.update(0.01)
        self.assertTrue(g.flames and g.pending)
        r = Game.from_save(json.loads(json.dumps(g.to_save())))
        self.assertIsNotNone(r)
        for _ in range(60):
            g.update(0.05)
            r.update(0.05)
        self.assertEqual(r.to_save(), g.to_save())

    def test_corrupt_or_incompatible_is_none(self):
        good = self.advanced().to_save()
        self.assertIsNotNone(Game.from_save(good))
        self.assertIsNone(Game.from_save(None))
        self.assertIsNone(Game.from_save({}))
        self.assertIsNone(Game.from_save("junk"))
        self.assertIsNone(Game.from_save(dict(good, version=bomberman.SAVE_VERSION + 1)))
        self.assertIsNone(Game.from_save(dict(good, rng=[3, [1, 2], None])))
        self.assertIsNone(Game.from_save(dict(good, grid=good["grid"][:-1])))
        self.assertIsNone(Game.from_save(dict(good, grid=["?" * FIELD_W] * FIELD_H)))
        self.assertIsNone(Game.from_save(dict(good, grid=["." * FIELD_W] * FIELD_H)))
        self.assertIsNone(Game.from_save(dict(good, player=[FIELD_W, 0])))
        self.assertIsNone(Game.from_save(dict(good, player=[1, 1])))    # a pillar
        self.assertIsNone(Game.from_save(dict(good, queued="sideways")))
        self.assertIsNone(Game.from_save(dict(good, enemies=[[0, 0]])))
        self.assertIsNone(Game.from_save(dict(good, enemies=[[0, 0, "dragon", "up", 0]])))
        self.assertIsNone(Game.from_save(dict(good, flame=0)))
        self.assertIsNone(Game.from_save(dict(good, speed=9)))
        self.assertIsNone(Game.from_save(dict(good, time_left=float("nan"))))
        soft = next([x, y] for y, row in enumerate(good["grid"])
                    for x, c in enumerate(row) if c == SOFT)
        self.assertIsNone(Game.from_save(dict(good, player=soft)))
        bad = dict(good)
        del bad["lives"]
        self.assertIsNone(Game.from_save(bad))

    def test_phase_resolved_before_save(self):
        g = blank_game()
        g.enemies = [Enemy(5, 4, "drifter")]
        g.move(RIGHT)
        g.resolve_phases()
        self.assertIsNone(g.phase)
        r = Game.from_save(json.loads(json.dumps(g.to_save())))
        self.assertIsNotNone(r)
        self.assertEqual(r.lives, LIVES - 1)


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
        self.assertEqual(ch, bomberman.controls_height(bomberman.CONTROLS, 30))
        self.assertGreaterEqual(ah, bomberman.AGENTS_MIN_H)
        self.assertIsNone(plan["hint"])

    def test_no_agents_tier(self):
        plan = layout_plan(93, 20)
        self.assertEqual(plan["tier"], "stats")
        self.assertIsNone(plan["agents"])
        self.assertIsNotNone(plan["stats"])
        self.assert_fits(plan, 93, 20)
        self.assert_centred(plan, 93, 20)
        plan = layout_plan(85, 18)
        self.assertEqual(plan["tier"], "controls")
        self.assertIsNone(plan["stats"])

    def test_no_side_column_tier(self):
        plan = layout_plan(80, 24)
        self.assertEqual(plan["tier"], "field")
        self.assertIsNone(plan["stats"])
        self.assertIsNone(plan["controls"])
        self.assertEqual(plan["hint"], (23, 0, 1, 80))
        self.assert_fits(plan, 80, 24)
        self.assert_centred(plan, 80, 24)
        self.assertIsNone(layout_plan(60, 18)["hint"])  # header carries ? help

    def test_too_small_tier(self):
        self.assertTrue(layout_plan(59, 28)["too_small"])
        self.assertTrue(layout_plan(93, 17)["too_small"])
        self.assertFalse(layout_plan(60, 18)["too_small"])

    def test_every_size_fits_and_is_centred(self):
        for cols in range(40, 130, 3):
            for rows in range(10, 40):
                plan = layout_plan(cols, rows)
                if not plan["too_small"]:
                    self.assert_fits(plan, cols, rows)
                    self.assert_centred(plan, cols, rows)
                    if plan["controls"] is None:
                        self.assertEqual(plan["tier"], "field")

    def test_hint_leads_with_help(self):
        for w in (12, 28, 40, 200):
            text = bomberman.hint_text(w)
            self.assertIn("? help", text)
            self.assertLess(len(text), w)


class ConstantsTest(unittest.TestCase):
    def test_controls_cover_keys(self):
        keys = " ".join(k for k, _ in bomberman.CONTROLS)
        for k in ("?", "q", "p", "r", "arrows", "space", "x",
                  "w", "a", "s", "d", "h", "j", "k", "l"):
            self.assertIn(k, keys)
        self.assertEqual(bomberman.CONTROLS[-1], ("q", "menu"))
        for k, _ in bomberman.CONTROLS:
            self.assertLessEqual(len(k), 9)   # never truncated in the box
        self.assertTrue(set(bomberman.DETONATE_KEYS).isdisjoint(bomberman.KEY_DIRS))
        for reserved in "qprQPR?":
            self.assertNotIn(ord(reserved), bomberman.KEY_DIRS)

    def test_help_sections(self):
        help = dict(bomberman.HELP)
        for want in ("Goal", "Controls", "Scoring", "Tips", "Legend",
                     "Bombs and flames", "Power-ups", "The exit", "Monsters",
                     "Stage timer"):
            self.assertIn(want, help)
        for head, body in bomberman.HELP:
            self.assertTrue(body.strip(), head)
            body.encode("ascii")
        glyphs = [bomberman.BURN_GLYPH, bomberman.PLAYER_GLYPH, bomberman.DEAD_GLYPH,
                  bomberman.EXIT_GLYPH, bomberman.REMOTE_BOMB_GLYPH, "O3",
                  *bomberman.FLAME_GLYPHS.values(), *bomberman.POWERUP_GLYPHS.values(),
                  *bomberman.ENEMY_GLYPHS.values()]
        for glyph in glyphs:
            self.assertIn(glyph, help["Legend"])
        for glyph in bomberman.ENEMY_GLYPHS.values():
            self.assertIn(glyph, help["Monsters"])

    def test_field_glyphs_are_two_single_width_columns(self):
        glyphs = [bomberman.PILLAR_GLYPH, bomberman.SOFT_GLYPH, bomberman.BURN_GLYPH,
                  bomberman.PLAYER_GLYPH, bomberman.DEAD_GLYPH, bomberman.EXIT_GLYPH,
                  bomberman.REMOTE_BOMB_GLYPH, *bomberman.FLAME_GLYPHS.values(),
                  *bomberman.POWERUP_GLYPHS.values(), *bomberman.ENEMY_GLYPHS.values()]
        for glyph in glyphs:
            self.assertEqual(len(glyph), 2, glyph)
            for ch in glyph:
                self.assertTrue(ch.isascii() or ch in "█░", glyph)


class HelpKeyTest(unittest.TestCase):
    def setUp(self):
        self.game = new_game()
        self.game.resolve_phases()
        self.game.phase = None
        self.help = bomberman.HelpOverlay(bomberman.TITLE, bomberman.HELP)
        self.st = bomberman.LoopState()
        self.session = types.SimpleNamespace(attention=None, dismiss=lambda: None)

    def key(self, ch):
        if isinstance(ch, str):
            ch = ord(ch)
        return bomberman.dispatch_key(ch, self.game, self.help, self.st, self.session)

    def test_help_pauses_and_closing_resumes(self):
        self.key("?")
        self.assertTrue(self.help.is_open)
        self.assertTrue(self.game.paused)
        t = self.game.time_left
        self.assertFalse(self.game.update(0.2))
        self.assertEqual(self.game.time_left, t)       # timers frozen
        self.key("j")                                  # scrolls, stays open
        self.assertTrue(self.help.is_open)
        self.assertIsNone(self.key("q"))               # q closes help only
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
        st = self.st = bomberman.LoopState(resume=True)
        self.game.paused = True
        self.key("?")
        self.key("x")
        self.assertTrue(self.game.paused)      # banner still up
        self.key("x")                          # dismisses the banner
        self.assertFalse(st.resume)
        self.assertFalse(self.game.paused)

    def test_game_keys(self):
        self.assertEqual(self.key("q"), "quit")
        self.key("l")
        self.assertEqual(self.game.player, (1, 0))
        self.game.move_cd = 0
        self.key(" ")
        self.assertEqual(len(self.game.bombs), 1)
        self.game.remote = True
        self.game.bombs[0].remote = True
        self.key("x")
        self.assertEqual(self.game.bombs, [])
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
            raise bomberman.curses.error()
        child = FakeWin(h, w, self.root, self.y0 + y, self.x0 + x)
        self.root.children.append(child)
        return child

    def erase(self):
        self.writes = []

    def addstr(self, y, x, text, attr=0):
        if y < 0 or y >= self.rows or x < 0 or x + len(text) > self.cols:
            self.root.violations.append((y, x, text))
            raise bomberman.curses.error()
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
               now=0.3):
        scr = FakeWin(rows, cols)
        overlay = bomberman.HelpOverlay(bomberman.TITLE, bomberman.HELP)
        if help:
            overlay.open()
        with mock.patch("curses.color_pair", return_value=0):
            lay = bomberman.Layout(scr)
            bomberman.draw(game, lay, self.session(attention), resume, 1234, overlay, now)
        return scr, lay

    def games(self):
        yield "fresh", new_game()
        b = blank_game()
        with_far_enemy(b)
        b.enemies += [Enemy(k * 2, 6, kind) for k, kind in enumerate(bomberman.ENEMY_ORDER)]
        b.remote = True
        b.max_bombs = 3
        b.drop_bomb()
        b.remote = False
        b.player = (0, 0)
        b.drop_bomb()
        b.bombs.append(Bomb(10, 10, 0.001))
        b.powerups = {(12, 12): "wallpass"}
        b.exit = (14, 12)
        b.update(0.01)
        b.timeout = True
        b.message = ["+800 x4", 1.0]
        yield "busy", b
        d = blank_game()
        d.enemies = [Enemy(5, 4, "drifter")]
        d.move(RIGHT)
        yield "dying", d
        sc = blank_game()
        sc.phase = bomberman.Phase("stage_clear", 1.0)
        yield "stage_clear", sc
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
            for size in ((93, 28), (93, 20), (85, 18), (80, 24), (60, 18), (59, 28),
                         (10, 3)):
                for att, res in ((None, False), (attention, False), (None, True)):
                    scr, lay = self.render(game, *size, attention=att, resume=res)
                    self.assertEqual(scr.violations, [], (name, size))

    def test_field_glyphs_land_on_even_columns(self):
        for name, game in self.games():
            scr, lay = self.render(game)
            for y, x, text in lay.field.writes:
                if 1 <= y <= FIELD_H and len(text) == 2:
                    self.assertEqual((x - 1) % 2, 0, (name, y, x, text))

    def test_full_frame_contents(self):
        g = new_game()
        scr, lay = self.render(g)
        self.assertEqual(lay.plan["tier"], "full")
        text = "".join(t for c in scr.children for _, _, t in c.writes)
        self.assertIn("score 0", text)
        self.assertIn("agents", text)
        self.assertIn(bomberman.PILLAR_GLYPH, text)
        self.assertIn(bomberman.SOFT_GLYPH, text)
        self.assertIn(bomberman.PLAYER_GLYPH, text)
        self.assertIn(bomberman.ENEMY_GLYPHS["drifter"], text)
        self.assertIn("STAGE 1", text)
        header = "".join(t for _, _, t in scr.writes)
        self.assertIn("bomberman", header)
        go = new_game()
        go.game_over = True
        scr, _ = self.render(go)
        text = "".join(t for c in scr.children for _, _, t in c.writes)
        self.assertIn("GAME OVER", text)
        self.assertIn("r restart | q menu", text)

    def test_controls_visible_and_help_overlay(self):
        for cols, rows in ((93, 28), (93, 20), (85, 18), (80, 24), (60, 18)):
            for help in (False, True):
                scr, lay = self.render(new_game(), cols, rows, help=help)
                self.assertEqual(scr.violations, [], (cols, rows, help))
                if help:
                    text = "".join(t for c in scr.children for _, _, t in c.writes)
                    self.assertIn("Bomberman - help", text, (cols, rows))
                    continue
                if lay.controls is not None:
                    keys = "\n".join(t for _, _, t in lay.controls.writes)
                    for want in ("move", "bomb", "detonate", "help", "pause", "menu"):
                        self.assertIn(want, keys, (cols, rows))
                elif lay.plan["hint"] is not None:
                    hint = [t for y, _, t in scr.writes if y == rows - 1]
                    self.assertTrue(any("? help" in t for t in hint), (cols, rows))
                else:
                    head = [t for y, _, t in scr.writes if y == 0]
                    self.assertTrue(any("? help" in t for t in head), (cols, rows))
        _, lay = self.render(new_game(), 93, 28)
        self.assertTrue(any(" Bomberman " in t for _, _, t in lay.stats.writes))

    def test_too_small_message(self):
        scr, lay = self.render(new_game(), 59, 28)
        self.assertTrue(lay.too_small)
        text = " ".join(t for _, _, t in scr.writes)
        self.assertIn("pane too small", text)


if __name__ == "__main__":
    unittest.main()
