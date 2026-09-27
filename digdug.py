#!/usr/bin/env python3
"""Dig Dug for the Herdr arcade (Namco 1982; see arcade.py, which runs it via
`run()`).

A side view of the underground: a sky row over four strata of dirt. Dig Dug
tunnels through the dirt one cell per keypress. Pookas and Fygars prowl the
tunnels and now and then drift through the dirt as a pair of eyes; Fygars
also breathe fire. Harpoon a monster with the pump and keep pumping until it
pops, or dig the dirt out from under a rock and let it fall on them. Clear
the round by getting rid of every monster; the last one runs for the
surface and escapes if it reaches the top-left corner.

Structure: everything above the "curses UI" divider is pure game logic --
level generation, digging, the pump, rocks, monster AI, scoring, lives,
rounds, save/restore and the layout plan -- with no terminal dependency, so
`tests/test_digdug.py` imports this module without a terminal.
"""

from __future__ import annotations

import curses
import random
import time
from collections import deque

from common import (
    HELP_KEY,
    HelpOverlay,
    atomic_save_json,
    center_origin,
    controls_height,
    controls_hint,
    default_bg,
    draw_agents,
    draw_banner,
    draw_controls,
    frame,
    load_hiscore,
    load_json_file,
    remove_path,
    safe_addstr,
    save_hiscore,
    side_width,
)

# --------------------------------------------------------------------------
# Pure game logic
# --------------------------------------------------------------------------

FIELD_W = 15  # cells; each cell is two terminal columns
FIELD_H = 17  # row 0 is the sky, rows 1-16 are four strata of dirt
LAYER_ROWS = 4
N_LAYERS = 4

DIRT, OPEN = "#", "."

UP, DOWN, LEFT, RIGHT = "up", "down", "left", "right"
DELTAS = {UP: (0, -1), DOWN: (0, 1), LEFT: (-1, 0), RIGHT: (1, 0)}
DIRS = (UP, DOWN, LEFT, RIGHT)
REVERSE = {UP: DOWN, DOWN: UP, LEFT: RIGHT, RIGHT: LEFT}

START = (7, 8)          # Dig Dug's start cell: the foot of the entry shaft
EXIT = (0, 0)           # where the last monster escapes (top-left sky cell)
LIVES = 3
EXTRA_LIFE_FIRST = 10000
EXTRA_LIFE_EVERY = 40000  # further extra lives after the first

POOKA, FYGAR = "pooka", "fygar"
KINDS = (POOKA, FYGAR)
WALK, GHOST, WINDUP, FIRE = "walk", "ghost", "windup", "fire"
STATES = (WALK, GHOST, WINDUP, FIRE)
REST, WOBBLE, FALL = "rest", "wobble", "fall"
ROCK_STATES = (REST, WOBBLE, FALL)

DIG_POINTS = 10
POP_POINTS = (200, 300, 400, 500)   # by stratum 1 (top) .. 4 (bottom)
FYGAR_SIDE_MULT = 2                 # Fygar popped with a horizontal pump
CRUSH_POINTS = (0, 1000, 2500, 4000, 6000, 8000, 10000, 12000, 15000)
VEG_POINTS = (400, 600, 800, 1000, 1000, 2000, 2000, 3000, 3000, 4000,
              5000, 6000, 7000, 8000)
ROCKS_FOR_VEG = 2

MOVE_TIME = 0.09        # seconds between steps through open tunnel
DIG_TIME = 0.16         # ... and when the step digs dirt
PUMP_COOLDOWN = 0.12
PUMP_RANGE = 3          # harpoon reach in cells
INFLATE_STAGES = 3      # the pump after the third stage pops the monster
DEFLATE_TIME = 0.7      # seconds per stage an unpumped monster shrinks back
ROCK_WOBBLE = 0.8
ROCK_FALL_STEP = 0.09
VEG_TIME = 10.0
FIRE_RANGE = 3          # cells of flame in front of a Fygar
FIRE_WINDUP = 0.7       # the telegraph: Fygar flashes before breathing
FIRE_TIME = 0.6
FIRE_COOLDOWN = 3.0
FIRE_CHANCE = 0.5       # per step, when Dig Dug is in the Fygar's row and near
GHOST_CHANCE = 0.2      # per step, once a monster has walked ghost_after() s
WALK_GREEDY = 0.6       # chance a walking monster takes the step closest to you
START_GRACE = 1.2       # monsters hold still this long at round start/respawn
DEATH_TIME = 1.2
LEVEL_CLEAR_TIME = 2.0

SAVE_VERSION = 1


def enemy_interval(level: int) -> float:
    """Seconds per monster step at `level`: faster each round, 0.16s floor."""
    return max(0.16, 0.45 * 0.93 ** (level - 1))


def ghost_after(level: int) -> float:
    """Seconds a monster walks the tunnels before it may turn into eyes."""
    return max(2.5, 7.0 - 0.5 * (level - 1))


def enemy_count(level: int) -> int:
    return min(4 + (level - 1) // 2, 8)


def fygar_count(level: int) -> int:
    n = enemy_count(level)
    return min(n // 2, 1 + (level - 1) // 3)


def rock_count(level: int) -> int:
    return min(3 + (level - 1) // 3, 6)


def veg_points(level: int) -> int:
    return VEG_POINTS[min(level, len(VEG_POINTS)) - 1]


def crush_points(n: int) -> int:
    return CRUSH_POINTS[min(n, len(CRUSH_POINTS) - 1)]


def layer_of(y: int) -> int:
    """Stratum 1-4 of row `y`; the sky counts as the top stratum."""
    if y <= 0:
        return 1
    return min(N_LAYERS, (y - 1) // LAYER_ROWS + 1)


def pop_points(kind: str, y: int, horizontal: bool) -> int:
    pts = POP_POINTS[layer_of(y) - 1]
    if kind == FYGAR and horizontal:
        pts *= FYGAR_SIDE_MULT
    return pts


def in_bounds(x: int, y: int) -> bool:
    return 0 <= x < FIELD_W and 0 <= y < FIELD_H


def manhattan(a, b) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def generate_level(rng: random.Random, level: int):
    """One round's underground: (grid, enemy spawns, rock cells).

    The layout is generated, not transcribed: an entry shaft from the sky
    down to START with a short cross-tunnel, one sealed pocket per monster
    (a straight 3-5 cell tunnel well away from the start and from other
    tunnels), and rocks buried in solid dirt with dirt below them. Spawns
    are [(kind, x, y)], Fygars first."""
    grid = [[OPEN] * FIELD_W] + [[DIRT] * FIELD_W for _ in range(FIELD_H - 1)]
    sx, sy = START
    for y in range(1, sy + 1):
        grid[y][sx] = OPEN
    for x in (sx - 1, sx + 1):
        grid[sy][x] = OPEN

    def clear_ring(cells) -> bool:
        """Every cell and its 8 neighbours is dirt (or off the field)."""
        for x, y in cells:
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    nx, ny = x + dx, y + dy
                    if in_bounds(nx, ny) and ny > 0 and grid[ny][nx] != DIRT:
                        return False
        return True

    n = enemy_count(level)
    kinds = [FYGAR] * fygar_count(level) + [POOKA] * (n - fygar_count(level))
    spawns = []
    for kind in kinds:
        placed = False
        for _ in range(300):
            length = rng.randint(3, 5)
            horizontal = rng.random() < 0.6
            if horizontal:
                x0 = rng.randint(0, FIELD_W - length)
                y0 = rng.randint(2, FIELD_H - 2)
                cells = [(x0 + i, y0) for i in range(length)]
            else:
                x0 = rng.randint(0, FIELD_W - 1)
                y0 = rng.randint(2, FIELD_H - 1 - length)
                cells = [(x0, y0 + i) for i in range(length)]
            if min(manhattan(c, START) for c in cells) < 5:
                continue
            if not clear_ring(cells):
                continue
            for x, y in cells:
                grid[y][x] = OPEN
            x, y = rng.choice(cells)
            spawns.append((kind, x, y))
            placed = True
            break
        if not placed:  # crowded field: a one-cell pocket anywhere far enough
            far = [(x, y) for y in range(2, FIELD_H) for x in range(FIELD_W)
                   if grid[y][x] == DIRT and manhattan((x, y), START) >= 5]
            x, y = rng.choice(far)
            grid[y][x] = OPEN
            spawns.append((kind, x, y))

    rocks = []
    for _ in range(rock_count(level)):
        for _ in range(300):
            x = rng.randint(0, FIELD_W - 1)
            y = rng.randint(2, FIELD_H - 5)
            if x == sx or grid[y][x] != DIRT or grid[y + 1][x] != DIRT:
                continue
            around = [(x + dx, y + dy) for dx, dy in DELTAS.values()]
            if any(in_bounds(*c) and grid[c[1]][c[0]] != DIRT for c in around):
                continue
            if any(manhattan(r, (x, y)) < 3 for r in rocks):
                continue
            rocks.append((x, y))
            break
    for x, y in rocks:
        grid[y][x] = OPEN  # the rock fills it; once it falls it is a hole
    return grid, spawns, rocks


class Phase:
    """A transient animation as an explicit logic state: 'dying' or
    'level_clear'. The state change happens when it completes -- via
    update() or resolve_phases()."""

    __slots__ = ("kind", "duration", "elapsed")

    def __init__(self, kind: str, duration: float):
        self.kind = kind
        self.duration = duration
        self.elapsed = 0.0


class Rock:
    __slots__ = ("x", "y", "state", "timer", "crushed")

    def __init__(self, x: int, y: int, state: str = REST, timer: float = 0.0,
                 crushed: int = 0):
        self.x, self.y = x, y
        self.state, self.timer, self.crushed = state, timer, crushed

    @property
    def pos(self):
        return (self.x, self.y)


class Enemy:
    """A Pooka or Fygar. `inflate` 0 means free; 1-3 are pump stages (it
    neither moves nor hurts while inflated). `acc` is the step accumulator
    (negative while holding still), `walk_time` counts toward ghosting."""

    __slots__ = ("id", "kind", "x", "y", "hx", "hy", "dir", "hdir", "state",
                 "timer", "inflate", "deflate", "acc", "walk_time",
                 "ghost_steps", "fire_cd", "flee")

    def __init__(self, eid: int, kind: str, x: int, y: int):
        self.id, self.kind = eid, kind
        self.x, self.y = x, y
        self.hx, self.hy = x, y
        self.dir = LEFT
        self.hdir = LEFT
        self.state = WALK
        self.timer = 0.0
        self.inflate = 0
        self.deflate = 0.0
        self.acc = -START_GRACE
        self.walk_time = 0.0
        self.ghost_steps = 0
        self.fire_cd = 0.0
        self.flee = False

    @property
    def pos(self):
        return (self.x, self.y)

    def to_list(self) -> list:
        return [self.id, self.kind, self.x, self.y, self.hx, self.hy, self.dir,
                self.hdir, self.state, self.timer, self.inflate, self.deflate,
                self.acc, self.walk_time, self.ghost_steps, self.fire_cd,
                self.flee]


def _rng_to_json(rng):
    version, state, gauss = rng.getstate()
    return [version, list(state), gauss]


def _rng_from_json(data):
    """Random state from saved data, or None if absent/ill-formed."""
    if (
        isinstance(data, list)
        and len(data) == 3
        and data[0] == 3
        and isinstance(data[1], list)
        and len(data[1]) == 625
        and all(isinstance(n, int) for n in data[1])
    ):
        return (data[0], tuple(data[1]), data[2])
    return None


class Game:
    """Full game state and every state transition. No I/O."""

    def __init__(self, rng: random.Random | None = None):
        self.rng = rng if rng is not None else random.Random()
        self.score = 0
        self.level = 1
        self.lives = LIVES
        self.next_extra = EXTRA_LIFE_FIRST
        self.paused = False
        self.game_over = False
        self.phase: Phase | None = None
        # Cosmetic, never saved: recent effects for the renderer.
        self.splats: list = []      # [x, y, seconds left]
        self.harpoon_flash = None   # [cells, seconds left] after a miss
        self.message = None         # [text, seconds left]
        self.escaped = False
        self._start_level()

    def reset(self) -> None:
        self.__init__(self.rng)

    # -- level setup -------------------------------------------------------

    def _start_level(self) -> None:
        grid, spawns, rocks = generate_level(self.rng, self.level)
        self.grid = grid
        self.enemies = [Enemy(i, k, x, y) for i, (k, x, y) in enumerate(spawns)]
        self.rocks = [Rock(x, y) for x, y in rocks]
        self.player = START
        self.facing = DOWN
        self.move_cd = 0.0
        self.pump_cd = 0.0
        self.queued = None
        self.pump_target = None   # id of the harpooned monster
        self.rocks_dropped = 0
        self.veg = None           # [x, y, seconds left]
        self.veg_done = False
        self.level_time = 0.0
        self.splats = []
        self.harpoon_flash = None
        self.message = None
        self.escaped = False

    # -- queries -----------------------------------------------------------

    def rock_at(self, pos):
        for rock in self.rocks:
            if rock.pos == pos:
                return rock
        return None

    def enemy_by_id(self, eid):
        for e in self.enemies:
            if e.id == eid:
                return e
        return None

    def is_open(self, x: int, y: int) -> bool:
        """Tunnel or sky, and no rock in it."""
        return (in_bounds(x, y) and self.grid[y][x] == OPEN
                and self.rock_at((x, y)) is None)

    def harpoon_path(self):
        """(cells, enemy hit or None) for a harpoon fired now: it flies up to
        PUMP_RANGE cells through open tunnel and stops at dirt, rocks and the
        edge. Eyes drifting through dirt cannot be hit."""
        if self.pump_target is not None:
            target = self.enemy_by_id(self.pump_target)
            if target is not None:
                return self._cells_to(target.pos), target
        dx, dy = DELTAS[self.facing]
        x, y = self.player
        cells = []
        for _ in range(PUMP_RANGE):
            x, y = x + dx, y + dy
            if not self.is_open(x, y):
                break
            cells.append((x, y))
            for e in self.enemies:
                if e.pos == (x, y) and e.state != GHOST:
                    return cells, e
        return cells, None

    def _cells_to(self, pos) -> list:
        dx, dy = DELTAS[self.facing]
        x, y = self.player
        cells = []
        for _ in range(PUMP_RANGE):
            x, y = x + dx, y + dy
            cells.append((x, y))
            if (x, y) == pos:
                break
        return cells

    def flame_cells(self, e) -> list:
        """Cells a Fygar's breath covers: FIRE_RANGE cells ahead, through
        dirt, stopped by rocks, the sky and the edge."""
        if e.kind != FYGAR:
            return []
        dx = -1 if e.hdir == LEFT else 1
        cells = []
        for i in range(1, FIRE_RANGE + 1):
            x = e.x + i * dx
            if not in_bounds(x, e.y) or e.y == 0 or self.rock_at((x, e.y)) is not None:
                break
            cells.append((x, e.y))
        return cells

    def _can_act(self) -> bool:
        return not (self.paused or self.game_over or self.phase is not None)

    # -- scoring -----------------------------------------------------------

    def add_score(self, points: int) -> None:
        """The one way points are earned, so no branch misses an extra life."""
        self.score += points
        while self.score >= self.next_extra:
            self.lives += 1
            self.next_extra += EXTRA_LIFE_EVERY
            self.message = ["EXTRA LIFE", 1.5]

    # -- Dig Dug -----------------------------------------------------------

    def move(self, d: str) -> bool:
        """Face `d` and step one cell, digging through dirt. A press that
        comes while the last step is still finishing is queued (one deep)
        and taken as soon as it can be. True if Dig Dug moved now."""
        if d not in DELTAS or not self._can_act():
            return False
        if self.move_cd > 0:
            self.queued = d
            return False
        return self._step(d)

    def _step(self, d: str) -> bool:
        self.queued = None
        self.pump_target = None      # moving or turning lets go of the pump
        self.facing = d
        dx, dy = DELTAS[d]
        nx, ny = self.player[0] + dx, self.player[1] + dy
        if not in_bounds(nx, ny) or self.rock_at((nx, ny)) is not None:
            return False
        dug = self.grid[ny][nx] == DIRT
        if dug:
            self.grid[ny][nx] = OPEN
            self.add_score(DIG_POINTS)
        self.player = (nx, ny)
        self.move_cd = DIG_TIME if dug else MOVE_TIME
        self._check_veg()
        self._check_contacts()
        return True

    def pump(self) -> str:
        """Space: fire the harpoon, or pump the monster already on it.
        Returns 'none', 'miss', 'hit', 'inflate' or 'pop'."""
        if not self._can_act() or self.pump_cd > 0:
            return "none"
        self.pump_cd = PUMP_COOLDOWN
        self.queued = None           # a stale step must not drop the harpoon
        target = self.enemy_by_id(self.pump_target) if self.pump_target is not None else None
        if target is not None and target.inflate > 0:
            target.inflate += 1
            target.deflate = DEFLATE_TIME
            if target.inflate > INFLATE_STAGES:
                horizontal = self.facing in (LEFT, RIGHT)
                self.add_score(pop_points(target.kind, target.y, horizontal))
                self._remove_enemy(target)
                return "pop"
            return "inflate"
        self.pump_target = None
        cells, hit = self.harpoon_path()
        if hit is None:
            self.harpoon_flash = [cells, 0.2]
            return "miss"
        self.pump_target = hit.id
        hit.inflate = 1
        hit.deflate = DEFLATE_TIME
        if hit.state in (WINDUP, FIRE):   # the pump cuts off the fire
            hit.state, hit.timer = WALK, 0.0
            hit.fire_cd = FIRE_COOLDOWN
        return "hit"

    def _check_veg(self) -> None:
        if self.veg is not None and (self.veg[0], self.veg[1]) == self.player:
            pts = veg_points(self.level)
            self.add_score(pts)
            self.splats.append([self.veg[0], self.veg[1], 0.4])
            self.message = [f"VEGETABLE +{pts}", 1.5]
            self.veg = None

    def _check_contacts(self) -> None:
        """A free monster in Dig Dug's cell, or a flame over it, kills him."""
        if self.phase is not None:
            return
        for e in self.enemies:
            if e.inflate == 0 and e.pos == self.player:
                self._die()
                return
            if e.state == FIRE and self.player in self.flame_cells(e):
                self._die()
                return

    def _remove_enemy(self, e) -> None:
        self.enemies.remove(e)
        if self.pump_target == e.id:
            self.pump_target = None
        self.splats.append([e.x, e.y, 0.4])
        self._check_clear()

    def _check_clear(self) -> None:
        if self.phase is None and not self.enemies:
            self.phase = Phase("level_clear", LEVEL_CLEAR_TIME)

    def _die(self) -> None:
        if self.phase is not None:
            return
        self.lives -= 1
        self.phase = Phase("dying", DEATH_TIME)

    def _respawn(self) -> None:
        """Dig Dug back at the start; every monster back home, deflated and
        holding still for a moment."""
        self.player = START
        if self.grid[START[1]][START[0]] != OPEN:
            self.grid[START[1]][START[0]] = OPEN
        self.facing = DOWN
        self.move_cd = self.pump_cd = 0.0
        self.queued = None
        self.pump_target = None
        for e in self.enemies:
            e.x, e.y = e.hx, e.hy
            e.state, e.timer = WALK, 0.0
            e.inflate, e.deflate = 0, 0.0
            e.acc = -START_GRACE
            e.walk_time = 0.0
            e.ghost_steps = 0

    def resolve_phases(self) -> None:
        """Finish a pending transition immediately (pause/quit/save). The
        resulting state is exactly the one the animation would end in."""
        phase, self.phase = self.phase, None
        if phase is None:
            return
        if phase.kind == "dying":
            if self.lives <= 0:
                self.lives = 0
                self.game_over = True
            else:
                self._respawn()
        elif phase.kind == "level_clear":
            self.level += 1
            self._start_level()

    # -- rocks -------------------------------------------------------------

    def _rock_can_drop(self, rock) -> bool:
        x, y = rock.x, rock.y + 1
        return (in_bounds(x, y) and self.grid[y][x] == OPEN
                and self.rock_at((x, y)) is None)

    def _update_rocks(self, dt: float) -> bool:
        changed = False
        for rock in list(self.rocks):
            if rock.state == REST:
                below = (rock.x, rock.y + 1)
                # A rock waits while Dig Dug stands right under it.
                if self._rock_can_drop(rock) and self.player != below:
                    rock.state, rock.timer = WOBBLE, ROCK_WOBBLE
                    changed = True
            elif rock.state == WOBBLE:
                changed = True
                rock.timer -= dt
                if rock.timer <= 0:
                    rock.state, rock.timer = FALL, ROCK_FALL_STEP
            else:
                changed = True
                rock.timer -= dt
                while rock.timer <= 0 and rock in self.rocks:
                    if self._rock_can_drop(rock):
                        rock.y += 1
                        rock.timer += ROCK_FALL_STEP
                        self._rock_crush(rock)
                    else:
                        self._rock_land(rock)
            if self.phase is not None and self.phase.kind == "dying":
                break
        return changed

    def _rock_crush(self, rock) -> None:
        for e in [e for e in self.enemies if e.pos == rock.pos]:
            rock.crushed += 1
            self.add_score(crush_points(rock.crushed) - crush_points(rock.crushed - 1))
            self._remove_enemy(e)
        if self.player == rock.pos:
            self._die()

    def _rock_land(self, rock) -> None:
        self.rocks.remove(rock)
        self.rocks_dropped += 1
        self.splats.append([rock.x, rock.y, 0.4])
        if rock.crushed:
            self.message = [f"CRUSH x{rock.crushed} +{crush_points(rock.crushed)}", 1.5]
        if self.rocks_dropped >= ROCKS_FOR_VEG and not self.veg_done:
            self.veg_done = True
            self.veg = [START[0], START[1], VEG_TIME]
            self._check_veg()

    # -- monsters ----------------------------------------------------------

    def _walk_options(self, e) -> list:
        out = []
        for d in DIRS:
            dx, dy = DELTAS[d]
            nx, ny = e.x + dx, e.y + dy
            if ny >= 1 and self.is_open(nx, ny):
                out.append(d)
        return out

    def _choose(self, e, dirs, target, greedy: bool) -> str:
        if greedy:
            dists = {d: manhattan((e.x + DELTAS[d][0], e.y + DELTAS[d][1]), target)
                     for d in dirs}
            best = min(dists.values())
            dirs = [d for d in dirs if dists[d] == best]
        return self.rng.choice(dirs)

    def _move_enemy(self, e, d: str) -> None:
        dx, dy = DELTAS[d]
        e.x += dx
        e.y += dy
        e.dir = d
        if d in (LEFT, RIGHT):
            e.hdir = d

    def _ghost_step(self, e, target, sky=False) -> None:
        """Eyes drift through dirt and tunnel alike (never rocks) toward
        `target`, taking a closest step; ties and dead ends go random."""
        dirs = []
        for d in DIRS:
            dx, dy = DELTAS[d]
            nx, ny = e.x + dx, e.y + dy
            if in_bounds(nx, ny) and (ny >= 1 or sky) and self.rock_at((nx, ny)) is None:
                dirs.append(d)
        if not dirs:
            return
        here = manhattan(e.pos, target)
        closer = [d for d in dirs
                  if manhattan((e.x + DELTAS[d][0], e.y + DELTAS[d][1]), target) < here]
        self._move_enemy(e, self._choose(e, closer or dirs, target, bool(closer)))
        e.ghost_steps += 1

    def _flee_path(self, e):
        """First direction of a shortest route through tunnels and the sky
        to EXIT, or None."""
        start = e.pos
        prev = {start: None}
        todo = deque([start])
        while todo:
            cur = todo.popleft()
            if cur == EXIT:
                break
            for d in DIRS:
                dx, dy = DELTAS[d]
                n = (cur[0] + dx, cur[1] + dy)
                if n not in prev and self.is_open(*n):
                    prev[n] = (cur, d)
                    todo.append(n)
        if EXIT not in prev or start == EXIT:
            return None
        cur = EXIT
        while prev[cur][0] != start:
            cur = prev[cur][0]
        return prev[cur][1]

    def _enemy_step(self, e) -> None:
        if e.flee:
            d = self._flee_path(e)
            if d is not None:
                e.state = WALK
                self._move_enemy(e, d)
            else:
                e.state = GHOST
                self._ghost_step(e, EXIT, sky=True)
                if self.grid[e.y][e.x] == OPEN and self._flee_path(e) is not None:
                    e.state = WALK
            if e.pos == EXIT:
                self.enemies.remove(e)
                if self.pump_target == e.id:
                    self.pump_target = None
                self.escaped = True
                self.message = ["ESCAPED!", 1.5]
                self._check_clear()
            return
        if e.state == GHOST:
            self._ghost_step(e, self.player)
            if self.grid[e.y][e.x] == OPEN and e.ghost_steps >= 2:
                e.state, e.walk_time, e.ghost_steps = WALK, 0.0, 0
            return
        # Walking the tunnels.
        px, py = self.player
        if (e.kind == FYGAR and e.fire_cd <= 0 and py == e.y
                and 0 < abs(px - e.x) <= FIRE_RANGE + 1
                and self.rng.random() < FIRE_CHANCE):
            e.hdir = RIGHT if px > e.x else LEFT
            e.state, e.timer = WINDUP, FIRE_WINDUP
            return
        if e.walk_time >= ghost_after(self.level) and self.rng.random() < GHOST_CHANCE:
            e.state, e.ghost_steps = GHOST, 0
            self._ghost_step(e, self.player)
            return
        dirs = self._walk_options(e)
        if not dirs:
            return
        if len(dirs) > 1 and REVERSE[e.dir] in dirs:
            dirs.remove(REVERSE[e.dir])
        greedy = self.rng.random() < WALK_GREEDY
        self._move_enemy(e, self._choose(e, dirs, self.player, greedy))

    def _step_interval(self, e) -> float:
        base = enemy_interval(self.level)
        if e.flee:
            return base * 0.8
        if e.state == GHOST:
            return base * 1.5
        return base

    def _update_enemies(self, dt: float) -> bool:
        changed = False
        for e in list(self.enemies):
            if e not in self.enemies:
                continue
            if e.fire_cd > 0:
                e.fire_cd = max(0.0, e.fire_cd - dt)
            if e.inflate > 0:
                e.acc = 0.0
                e.deflate -= dt
                if e.deflate <= 0:
                    e.inflate -= 1
                    e.deflate = DEFLATE_TIME if e.inflate else 0.0
                    changed = True
                    if e.inflate == 0:
                        if self.pump_target == e.id:
                            self.pump_target = None
                        self._check_contacts()   # free again, maybe on Dig Dug
                        if self.phase is not None:
                            return True
                continue
            if e.state in (WINDUP, FIRE):
                changed = True
                e.timer -= dt
                if e.timer <= 0:
                    if e.state == WINDUP:
                        e.state, e.timer = FIRE, FIRE_TIME
                    else:
                        e.state, e.timer = WALK, 0.0
                        e.fire_cd = FIRE_COOLDOWN
                self._check_contacts()
                if self.phase is not None:
                    return True
                continue
            if e.state == WALK:
                e.walk_time += dt
            e.acc += dt
            interval = self._step_interval(e)
            while e.acc >= interval:
                e.acc -= interval
                changed = True
                self._enemy_step(e)
                self._check_contacts()
                if self.phase is not None:
                    return True
                if e not in self.enemies or e.state != GHOST and e.state != WALK:
                    break
                interval = self._step_interval(e)
        return changed

    # -- time --------------------------------------------------------------

    def update(self, dt: float) -> bool:
        """Advance by `dt` seconds. True if anything changed."""
        if self.paused or self.game_over:
            return False
        changed = self._tick_effects(dt)
        if self.phase is not None:
            self.phase.elapsed += dt
            if self.phase.elapsed >= self.phase.duration:
                self.resolve_phases()
            return True
        self.level_time += dt
        self.pump_cd = max(0.0, self.pump_cd - dt)
        if self.move_cd > 0:
            self.move_cd = max(0.0, self.move_cd - dt)
            if self.move_cd <= 0 and self.queued is not None:
                self._step(self.queued)
                changed = True
                if self.phase is not None:
                    return True
        if self.veg is not None:
            self.veg[2] -= dt
            if self.veg[2] <= 0:
                self.veg = None
                changed = True
        if self._update_rocks(dt):
            changed = True
        if self.phase is not None:
            return True
        if len(self.enemies) == 1 and not self.enemies[0].flee:
            self.enemies[0].flee = True
            self.message = ["LAST ONE RUNS!", 1.5]
            changed = True
        if self._update_enemies(dt):
            changed = True
        return changed

    def _tick_effects(self, dt: float) -> bool:
        changed = False
        for s in self.splats:
            s[2] -= dt
        before = len(self.splats)
        self.splats = [s for s in self.splats if s[2] > 0]
        changed = changed or len(self.splats) != before or bool(self.splats)
        for attr in ("harpoon_flash", "message"):
            eff = getattr(self, attr)
            if eff is not None:
                eff[1] -= dt
                changed = True
                if eff[1] <= 0:
                    setattr(self, attr, None)
        return changed

    # -- save / restore ----------------------------------------------------

    def to_save(self) -> dict:
        """Serializable snapshot. The caller resolves phases first (the UI
        save path does), so a save never holds a transition mid-flight."""
        return {
            "version": SAVE_VERSION,
            "score": self.score,
            "level": self.level,
            "lives": self.lives,
            "next_extra": self.next_extra,
            "grid": ["".join(row) for row in self.grid],
            "rocks": [[r.x, r.y, r.state, r.timer, r.crushed] for r in self.rocks],
            "enemies": [e.to_list() for e in self.enemies],
            "player": list(self.player),
            "facing": self.facing,
            "move_cd": self.move_cd,
            "pump_cd": self.pump_cd,
            "queued": self.queued,
            "pump_target": self.pump_target,
            "rocks_dropped": self.rocks_dropped,
            "veg": self.veg,
            "veg_done": self.veg_done,
            "level_time": self.level_time,
            "game_over": self.game_over,
            "rng": _rng_to_json(self.rng),
        }

    @classmethod
    def from_save(cls, data, rng=None):
        """Rebuild a saved game, or None for anything corrupt, incompatible,
        or not exactly a save this version wrote."""
        try:
            if not isinstance(data, dict) or data.get("version") != SAVE_VERSION:
                return None

            def integer(v, lo=0):
                if isinstance(v, bool) or not isinstance(v, int) or v < lo:
                    raise ValueError(v)
                return v

            def num(v, lo=0.0):
                if isinstance(v, bool) or not isinstance(v, (int, float)) or v < lo:
                    raise ValueError(v)
                return float(v)

            def cell(v):
                if not (isinstance(v, list) and len(v) == 2):
                    raise ValueError(v)
                x, y = integer(v[0]), integer(v[1])
                if not in_bounds(x, y):
                    raise ValueError(v)
                return (x, y)

            def choice(v, options):
                if v not in options:
                    raise ValueError(v)
                return v

            def flag(v):
                if not isinstance(v, bool):
                    raise ValueError(v)
                return v

            state = _rng_from_json(data["rng"])
            if state is None:
                return None
            game = cls(rng)
            game.score = integer(data["score"])
            game.level = integer(data["level"], 1)
            game.lives = integer(data["lives"], 1)
            game.next_extra = integer(data["next_extra"], 1)
            game.game_over = flag(data["game_over"])

            rows = data["grid"]
            if not isinstance(rows, list) or len(rows) != FIELD_H:
                return None
            grid = []
            for row in rows:
                if not isinstance(row, str) or len(row) != FIELD_W:
                    return None
                if any(c not in (DIRT, OPEN) for c in row):
                    return None
                grid.append(list(row))
            if any(c != OPEN for c in grid[0]):
                return None  # the sky is never dirt
            game.grid = grid

            rocks = []
            for r in data["rocks"]:
                if not isinstance(r, list) or len(r) != 5:
                    return None
                x, y = cell(r[:2])
                if y == 0 or grid[y][x] != OPEN or any(o.pos == (x, y) for o in rocks):
                    return None
                rocks.append(Rock(x, y, choice(r[2], ROCK_STATES), num(r[3], -1.0),
                                  integer(r[4])))
            game.rocks = rocks

            enemies = []
            for v in data["enemies"]:
                if not isinstance(v, list) or len(v) != 17:
                    return None
                e = Enemy(integer(v[0]), choice(v[1], KINDS), 0, 0)
                e.x, e.y = cell(v[2:4])
                e.hx, e.hy = cell(v[4:6])
                e.dir = choice(v[6], DIRS)
                e.hdir = choice(v[7], (LEFT, RIGHT))
                e.state = choice(v[8], STATES)
                e.timer = num(v[9], -1.0)
                e.inflate = integer(v[10])
                if e.inflate > INFLATE_STAGES:
                    return None
                e.deflate = num(v[11], -1.0)
                e.acc = num(v[12], -1e9)
                e.walk_time = num(v[13])
                e.ghost_steps = integer(v[14])
                e.fire_cd = num(v[15])
                e.flee = flag(v[16])
                if e.kind == POOKA and e.state in (WINDUP, FIRE):
                    return None
                if e.state != GHOST and not e.flee and grid[e.y][e.x] != OPEN:
                    return None
                if game.rock_at(e.pos) is not None or grid[e.hy][e.hx] != OPEN:
                    return None
                if any(o.id == e.id for o in enemies):
                    return None
                enemies.append(e)
            game.enemies = enemies
            if not enemies and not game.game_over:
                return None  # a cleared round is never saved mid-transition

            game.player = cell(data["player"])
            if grid[game.player[1]][game.player[0]] != OPEN or game.rock_at(game.player):
                return None
            game.facing = choice(data["facing"], DIRS)
            game.move_cd = num(data["move_cd"])
            game.pump_cd = num(data["pump_cd"])
            q = data["queued"]
            game.queued = None if q is None else choice(q, DIRS)
            t = data["pump_target"]
            if t is not None:
                target = game.enemy_by_id(integer(t))
                if target is None or target.inflate == 0:
                    return None
            game.pump_target = t
            game.rocks_dropped = integer(data["rocks_dropped"])
            veg = data["veg"]
            if veg is not None:
                if not isinstance(veg, list) or len(veg) != 3:
                    return None
                vx, vy = cell(veg[:2])
                veg = [vx, vy, num(veg[2])]
            game.veg = veg
            game.veg_done = flag(data["veg_done"])
            game.level_time = num(data["level_time"])

            # Restoring the RNG makes a resumed game play out exactly as the
            # saved one would have.
            game.rng.setstate(state)
            game.phase = None
            game.splats, game.harpoon_flash, game.message = [], None, None
            game.escaped = False
            return game
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            return None


TITLE = "Dig Dug"

# Every key the game reacts to (upper-case letters work too).
CONTROLS = [
    ("arrows", "walk / dig"),
    ("wasd hjkl", "walk too"),
    ("space", "pump / inflate"),
    ("enter", "pump too"),
    ("?", "help"),
    ("p / r", "pause / restart"),
    ("q", "menu"),
]

HELP = [
    ("Goal",
     "You are Dig Dug (D>), deep under a field of four coloured strata of "
     "dirt. Get rid of every Pooka and Fygar in the round - pump them up "
     "until they pop, or drop rocks on them - to move on to the next round."),
    ("Controls",
     "- arrows (or w a s d / h j k l): walk one cell. Walking into dirt digs "
     "a tunnel (a little slower than walking an open one). A direction key "
     "always turns Dig Dug, even into a rock he cannot pass; hold a key to "
     "keep going.\n"
     "- space (or enter): pump. Fires the harpoon the way Dig Dug faces, "
     "or pumps the monster already on it.\n"
     "- p pauses, r starts a new game, q goes back to the arcade menu.\n"
     "- ? opens this help (the game pauses while it is open)."),
    ("The pump",
     "The harpoon reaches 3 cells through open tunnel; dirt and rocks stop "
     "it. A hit monster inflates (stage 1) and freezes. Keep pressing space: "
     "stages 2 and 3, and the next pump pops it. Leave it alone and it "
     "shrinks back one stage every 0.7 s until it is free again. Moving or "
     "turning lets go of the harpoon. An inflated monster cannot hurt you, "
     "and eyes drifting through the dirt cannot be harpooned."),
    ("Monsters",
     "Pookas (()) and Fygars (<F F>) walk the tunnels, usually toward you. "
     "Touching one costs a life. After a while in the tunnels a monster may "
     "turn into a pair of eyes (oo) and drift straight through the dirt at "
     "you, becoming solid again once it reaches a tunnel. Each round brings "
     "more and faster monsters, and more Fygars."),
    ("Fygar's fire",
     "A Fygar that sees you in its row, within 4 cells, may breathe fire. "
     "First it flashes (<! !>) for 0.7 s - that is your warning - then the "
     "flame (~~) covers 3 cells in front of it for 0.6 s, straight through "
     "dirt (rocks stop it). Get out of the row, or pump it during the "
     "warning to cut the fire off."),
    ("Rocks",
     "Rocks ([]) sit in solid dirt. Dig out the dirt under one and, once "
     "you step out from beneath it, it wobbles (][) for a moment and falls "
     "until it lands, then breaks. A falling rock crushes every monster in "
     "its path - and you, if you are under it."),
    ("Bonus vegetable",
     "After two rocks have dropped in a round, a vegetable ($$) appears in "
     "the middle of the field, where you started, for 10 s. Walk onto it: "
     "400 on round 1, more on later rounds (up to 8000)."),
    ("The last monster",
     "When only one monster is left it gives up and runs for the surface, "
     "heading for the top-left corner of the sky. If it gets there it "
     "escapes: the round still ends, but you do not score it."),
    ("Lives and rounds",
     "You have 3 lives, plus one at 10000 points and one every 40000 after "
     "that. After losing one, Dig Dug starts again at the middle and every "
     "monster goes back to its den. At 0 lives the game is over. Every "
     "round has its own generated tunnels and rock placements."),
    ("Scoring",
     "- dig one cell of dirt: 10\n"
     "- pop a monster, by the stratum it is in: 200 top, 300, 400, 500 "
     "bottom (the sky counts as top)\n"
     "- a Fygar popped with a sideways (left/right) pump scores double\n"
     "- one rock crushing 1 monster 1000, 2: 2500, 3: 4000, 4: 6000, "
     "5: 8000, 6: 10000, 7: 12000, 8: 15000\n"
     "- vegetable: 400 on round 1 rising to 8000"),
    ("Legend",
     "- D> <D D^ Dv  Dig Dug, facing right / left / up / down\n"
     "- xx  Dig Dug losing a life\n"
     "- .. :: %% ##  dirt, strata 1 to 4 from the top\n"
     "- ()  Pooka\n"
     "- <F F>  Fygar, facing left / right\n"
     "- <! !>  Fygar about to breathe fire\n"
     "- ~~  Fygar's flame\n"
     "- oo  a monster's eyes, drifting through dirt\n"
     "- o0 00 @@  a pumped monster, stages 1 / 2 / 3\n"
     "- -- ||  the harpoon\n"
     "- [] ][  rock, wobbling rock\n"
     "- $$  bonus vegetable\n"
     "- **  a monster popped or crushed, a rock breaking"),
    ("Tips",
     "- Pump from the side: a Fygar is worth twice as much that way, and "
     "the deeper a monster is, the more it scores.\n"
     "- Lure monsters under a rock, then dig the last cell under it and "
     "step aside: several at once score far more than one.\n"
     "- Keep tapping space: a monster you stop pumping recovers.\n"
     "- Watch for the flashing Fygar and leave its row."),
]

# Layout: the framed field (15 cells x 2 cols + border = 32 x 19) with, when
# there is room, a side column of stacked boxes: stats, controls, agents.
# Row 0 is the header/banner; everything else is centred as one block below.
FIELD_WIN_W = FIELD_W * 2 + 2
FIELD_WIN_H = FIELD_H + 2
MIN_COLS = FIELD_WIN_W
MIN_ROWS = FIELD_WIN_H + 1
STATS_H = 8
AGENTS_MIN_H = 3
AGENTS_PREF_H = 8          # the side column grows to this much roster


def hint_text(width: int) -> str:
    """Compact key hint for tiers without a controls box; always leads
    with '? help' so it survives truncation."""
    first = [c for c in CONTROLS if c[0] == "?"]
    rest = [c for c in CONTROLS if c[0] != "?"]
    return (" " + controls_hint(first + rest))[: max(0, width - 1)]


def layout_plan(cols: int, rows: int) -> dict:
    """Pure layout decision: tier plus each region's (y, x, height, width).
    Tiers: 'full' (field + stats, controls and agents boxes), 'stats' (no
    agents box), 'controls' (controls box only; score in the header),
    'field' (no side column: score in the header, key hint on the last
    row), 'too_small'. The field and side column form one block centred
    below the header; the side column may run below the field."""
    plan = {"tier": "too_small", "too_small": True, "field": None,
            "stats": None, "controls": None, "agents": None, "hint": None}
    if cols < MIN_COLS or rows < MIN_ROWS:
        return plan
    plan["too_small"] = False
    avail = rows - 1
    side_w = side_width(cols, FIELD_WIN_W)
    ctrl_h = controls_height(CONTROLS, side_w) if side_w else 0
    if side_w and avail >= ctrl_h:
        stats_h = STATS_H if avail >= ctrl_h + STATS_H else 0
        agents = bool(stats_h) and avail >= ctrl_h + stats_h + AGENTS_MIN_H
        side_h = ctrl_h + stats_h + (AGENTS_PREF_H if agents else 0)
        comp_h = min(avail, max(FIELD_WIN_H, side_h))
        comp_w = FIELD_WIN_W + 1 + side_w
        y0, x0 = center_origin(cols, rows, comp_w, comp_h)
        plan["field"] = (y0, x0, FIELD_WIN_H, FIELD_WIN_W)
        sx, y = x0 + FIELD_WIN_W + 1, y0
        if stats_h:
            plan["stats"] = (y, sx, stats_h, side_w)
            y += stats_h
        plan["controls"] = (y, sx, ctrl_h, side_w)
        y += ctrl_h
        if agents:
            plan["agents"] = (y, sx, y0 + comp_h - y, side_w)
        plan["tier"] = "full" if agents else ("stats" if stats_h else "controls")
        return plan
    plan["tier"] = "field"
    hint = avail >= FIELD_WIN_H + 1
    y0, x0 = center_origin(cols, rows - (1 if hint else 0), FIELD_WIN_W, FIELD_WIN_H)
    plan["field"] = (y0, x0, FIELD_WIN_H, FIELD_WIN_W)
    if hint:
        plan["hint"] = (rows - 1, 0, 1, cols)
    return plan


# --------------------------------------------------------------------------
# curses UI
# --------------------------------------------------------------------------

# Colour pairs 40-59 are shared by the later games (one runs at a time);
# they are registered in run() on every start (see common.py).
PAIR_DIRT = (40, 41, 42, 43)     # strata 1-4, top to bottom
PAIR_ROCK, PAIR_PLAYER, PAIR_POOKA, PAIR_FYGAR = 44, 45, 46, 47
PAIR_FIRE, PAIR_HARPOON, PAIR_SPLAT, PAIR_VEG = 48, 49, 50, 51
PAIR_EYES, PAIR_DEAD, PAIR_WARN = 52, 53, 54

DIRT_GLYPHS = ("..", "::", "%%", "##")   # every field cell is exactly two columns
ROCK_GLYPH = "[]"
WOBBLE_GLYPH = "]["
POOKA_GLYPH = "()"
FYGAR_GLYPH = {LEFT: "<F", RIGHT: "F>"}
WINDUP_GLYPH = {LEFT: "<!", RIGHT: "!>"}
FLAME_GLYPH = "~~"
EYES_GLYPH = "oo"
INFLATE_GLYPHS = ("o0", "00", "@@")
HARPOON_GLYPH = {UP: "||", DOWN: "||", LEFT: "--", RIGHT: "--"}
VEG_GLYPH = "$$"
SPLAT_GLYPH = "**"
DEAD_GLYPH = "xx"
PLAYER_GLYPH = {UP: "D^", DOWN: "Dv", LEFT: "<D", RIGHT: "D>"}

DIRT_COLORS = (curses.COLOR_YELLOW, curses.COLOR_RED, curses.COLOR_MAGENTA,
               curses.COLOR_BLUE)


def init_colors() -> None:
    bg = default_bg()
    for pair, color in zip(PAIR_DIRT, DIRT_COLORS):
        curses.init_pair(pair, curses.COLOR_BLACK, color)
    curses.init_pair(PAIR_ROCK, curses.COLOR_WHITE, bg)
    curses.init_pair(PAIR_PLAYER, curses.COLOR_WHITE, bg)
    curses.init_pair(PAIR_POOKA, curses.COLOR_RED, bg)
    curses.init_pair(PAIR_FYGAR, curses.COLOR_GREEN, bg)
    curses.init_pair(PAIR_FIRE, curses.COLOR_YELLOW, curses.COLOR_RED)
    curses.init_pair(PAIR_HARPOON, curses.COLOR_CYAN, bg)
    curses.init_pair(PAIR_SPLAT, curses.COLOR_YELLOW, bg)
    curses.init_pair(PAIR_VEG, curses.COLOR_GREEN, bg)
    curses.init_pair(PAIR_EYES, curses.COLOR_WHITE, curses.COLOR_BLACK)
    curses.init_pair(PAIR_DEAD, curses.COLOR_RED, bg)
    curses.init_pair(PAIR_WARN, curses.COLOR_YELLOW, bg)


class Layout:
    """Screen regions for the current size; rebuilt on resize."""

    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.rows, self.cols = stdscr.getmaxyx()
        self.plan = layout_plan(self.cols, self.rows)
        self.too_small = self.plan["too_small"]
        self.tier = self.plan["tier"]
        self.field = self.stats = self.controls = self.agents = None
        if self.too_small:
            return
        for name in ("field", "stats", "controls", "agents"):
            if self.plan[name] is not None:
                y, x, h, w = self.plan[name]
                setattr(self, name, stdscr.derwin(h, w, y, x))

    def boxes(self):
        for win in (self.field, self.stats, self.controls, self.agents):
            if win is not None:
                win.erase()
                frame(win)


def _put(win, x, y, text, attr=0):
    """One field cell (two columns) at field coordinates."""
    safe_addstr(win, 1 + y, 1 + 2 * x, text, attr)


def _centered(win, row, text, attr):
    safe_addstr(win, row, max(1, (FIELD_WIN_W - len(text)) // 2), text[:FIELD_WIN_W - 2], attr)


def blink(now: float) -> bool:
    return int(now * 8) % 2 == 1


def display_key(game: Game, now: float):
    """Everything on screen that changes with time alone; the loop redraws
    whenever this changes."""
    animating = any(r.state == WOBBLE for r in game.rocks) or any(
        e.state == WINDUP for e in game.enemies)
    return (int(game.level_time), animating and blink(now))


def enemy_glyph(game: Game, e, now: float):
    """(glyph, attr) for a monster."""
    if e.inflate > 0:
        pair = PAIR_POOKA if e.kind == POOKA else PAIR_FYGAR
        return INFLATE_GLYPHS[e.inflate - 1], curses.color_pair(pair) | curses.A_BOLD
    if e.state == GHOST:
        return EYES_GLYPH, curses.color_pair(PAIR_EYES) | curses.A_BOLD
    if e.kind == POOKA:
        return POOKA_GLYPH, curses.color_pair(PAIR_POOKA) | curses.A_BOLD
    if e.state == WINDUP:
        attr = curses.color_pair(PAIR_WARN) | curses.A_BOLD
        if blink(now):
            attr |= curses.A_REVERSE
        return WINDUP_GLYPH[e.hdir], attr
    return FYGAR_GLYPH[e.hdir], curses.color_pair(PAIR_FYGAR) | curses.A_BOLD


def draw_field(win, game: Game) -> None:
    now = time.monotonic()
    phase = game.phase
    flash = phase is not None and phase.kind == "level_clear" and int(phase.elapsed * 6) % 2
    for y in range(1, FIELD_H):
        attr = curses.color_pair(PAIR_DIRT[layer_of(y) - 1])
        if flash:
            attr |= curses.A_BOLD
        glyph = DIRT_GLYPHS[layer_of(y) - 1]
        for x in range(FIELD_W):
            if game.grid[y][x] == DIRT:
                _put(win, x, y, glyph, attr)

    for rock in game.rocks:
        glyph = WOBBLE_GLYPH if rock.state == WOBBLE and blink(now) else ROCK_GLYPH
        _put(win, rock.x, rock.y, glyph, curses.color_pair(PAIR_ROCK) | curses.A_BOLD)
    if game.veg is not None:
        _put(win, game.veg[0], game.veg[1], VEG_GLYPH,
             curses.color_pair(PAIR_VEG) | curses.A_BOLD)

    harpoon = None
    if game.pump_target is not None:
        target = game.enemy_by_id(game.pump_target)
        if target is not None:
            harpoon = game._cells_to(target.pos)[:-1]
    elif game.harpoon_flash is not None:
        harpoon = game.harpoon_flash[0]
    for hx, hy in harpoon or ():
        _put(win, hx, hy, HARPOON_GLYPH[game.facing],
             curses.color_pair(PAIR_HARPOON) | curses.A_BOLD)

    for e in game.enemies:
        if e.state == FIRE:
            for fx, fy in game.flame_cells(e):
                _put(win, fx, fy, FLAME_GLYPH, curses.color_pair(PAIR_FIRE) | curses.A_BOLD)
    for sx, sy, _ in game.splats:
        _put(win, sx, sy, SPLAT_GLYPH, curses.color_pair(PAIR_SPLAT) | curses.A_BOLD)
    for e in game.enemies:
        glyph, attr = enemy_glyph(game, e, now)
        _put(win, e.x, e.y, glyph, attr)

    dying = phase is not None and phase.kind == "dying"
    if not game.game_over:
        px, py = game.player
        if dying:
            if int(phase.elapsed * 8) % 2 == 0:
                _put(win, px, py, DEAD_GLYPH, curses.color_pair(PAIR_DEAD) | curses.A_BOLD)
        else:
            _put(win, px, py, PLAYER_GLYPH[game.facing],
                 curses.color_pair(PAIR_PLAYER) | curses.A_BOLD)

    mid = 1 + FIELD_H // 2
    if game.game_over:
        _centered(win, mid - 1, " GAME OVER ", curses.A_STANDOUT | curses.A_BOLD)
        _centered(win, mid + 1, " r restart | q menu ", curses.A_STANDOUT)
    elif game.paused:
        _centered(win, mid - 1, " PAUSED ", curses.A_STANDOUT | curses.A_BOLD)
    elif phase is not None and phase.kind == "level_clear":
        _centered(win, mid - 1, f" ROUND {game.level + 1} ",
                  curses.color_pair(PAIR_SPLAT) | curses.A_BOLD | curses.A_REVERSE)
        if game.escaped:
            _centered(win, mid + 1, " it got away ", curses.color_pair(PAIR_SPLAT))
    elif game.message is not None:
        _centered(win, 1, f" {game.message[0]} ",
                  curses.color_pair(PAIR_SPLAT) | curses.A_BOLD)


def draw_stats(win, game: Game, best: int) -> None:
    rows, cols = win.getmaxyx()
    inner = cols - 2
    safe_addstr(win, 0, 2, f" {TITLE} "[: max(0, cols - 3)], curses.A_BOLD)
    if game.game_over:
        state = "GAME OVER"
    elif game.paused:
        state = "paused"
    elif game.phase is not None:
        state = "ouch!" if game.phase.kind == "dying" else "round clear!"
    elif len(game.enemies) == 1:
        state = "the last one runs!"
    else:
        state = f"time {int(game.level_time)}s"
    pookas = sum(1 for e in game.enemies if e.kind == POOKA)
    fygars = len(game.enemies) - pookas
    lines = [
        (f"score {game.score}", curses.A_BOLD),
        (f"best  {max(best, game.score)}", 0),
        (f"round {game.level}   rocks {game.rocks_dropped}", 0),
        (f"lives {game.lives}", curses.color_pair(PAIR_PLAYER)),
        (f"pooka {pookas}  fygar {fygars}", curses.color_pair(PAIR_POOKA)),
        (state, curses.A_DIM),
    ]
    for i, (line, attr) in enumerate(lines):
        if 1 + i < rows - 1:
            safe_addstr(win, 1 + i, 1, line[:inner], attr)


def draw(game: Game, lay: Layout, session, resume: bool, best: int,
         help=None) -> None:
    scr = lay.stdscr
    scr.erase()
    if lay.too_small:
        need = f"need {MIN_COLS}x{MIN_ROWS}, have {lay.cols}x{lay.rows}"
        lines = [f"pane too small: {need}", "resize the pane (q menu)"]
        if len(lines[0]) >= lay.cols:  # narrow: one short phrase per row
            lines = ["pane too small", need.split(", ")[0], need.split(", ")[1],
                     "resize the pane", "(q menu)"]
        top = max(0, (lay.rows - len(lines)) // 2)
        for i, line in enumerate(lines[: lay.rows]):
            line = line[: max(0, lay.cols - 1)]
            safe_addstr(scr, top + i, max(0, (lay.cols - len(line)) // 2), line,
                        curses.A_BOLD if i == 0 else 0)
        if help is not None:
            help.draw(scr)
        return

    if not draw_banner(scr, lay.cols, session.attention, resume):
        if game.game_over:
            head = " digdug - GAME OVER "
        else:
            head = f" digdug - {'paused' if game.paused else 'playing'} "
        if lay.plan["hint"] is None and lay.controls is None:
            head += "| ? help "  # no hint row and no controls box
        if lay.stats is None:  # no stats box in this tier: score goes here
            head += (f"| score {game.score} | lives {game.lives} | rnd {game.level}"
                     f" | left {len(game.enemies)} ")
        safe_addstr(scr, 0, 0, head[: lay.cols - 1],
                    curses.A_STANDOUT | (curses.A_BOLD if game.game_over else 0))

    lay.boxes()
    draw_field(lay.field, game)
    if lay.stats is not None:
        draw_stats(lay.stats, game, best)
    if lay.controls is not None:
        draw_controls(lay.controls, CONTROLS)
    if lay.agents is not None:
        draw_agents(lay.agents, session.roster, session.status_pairs)
    if lay.plan["hint"] is not None:
        y, x, _, w = lay.plan["hint"]
        safe_addstr(scr, y, x, hint_text(w), curses.A_DIM)
    if help is not None:
        help.draw(scr)


KEY_DIRS = {
    curses.KEY_UP: UP, curses.KEY_DOWN: DOWN,
    curses.KEY_LEFT: LEFT, curses.KEY_RIGHT: RIGHT,
}
for _keys, _d in (("kKwW", UP), ("jJsS", DOWN), ("hHaA", LEFT), ("lLdD", RIGHT)):
    for _k in _keys:
        KEY_DIRS[ord(_k)] = _d


PUMP_KEYS = (ord(" "), 10, 13, curses.KEY_ENTER)


class LoopState:
    """The run loop's pause bookkeeping: which pause came from what, so each
    source lifts only its own."""

    def __init__(self, resume=False):
        self.resume = resume
        self.paused_by_resume = resume
        self.paused_by_attention = False
        self.paused_by_help = False


def dispatch_key(ch, game, help, st, session):
    """One key from the run loop (KEY_RESIZE is handled before this).
    Returns 'quit', 'restart' or None; the caller redraws after any key."""
    if help.is_open:
        help.handle_key(ch)
        if not help.is_open and st.paused_by_help:
            st.paused_by_help = False
            if session.attention is not None or st.resume:
                st.paused_by_attention = True  # the banner's key resumes
            else:
                game.paused = False
        return None
    if ch == HELP_KEY:
        help.open()
        if not game.paused and not game.game_over:
            game.paused = True
            st.paused_by_help = True
        return None
    if session.attention is not None or st.resume:
        # Any key clears the banner and lifts only the pauses the banner
        # itself caused; the key is consumed.
        session.dismiss()
        st.resume = False
        if st.paused_by_attention or st.paused_by_resume:
            game.paused = False
        st.paused_by_attention = st.paused_by_resume = False
        return None
    if ch in (ord("q"), ord("Q")):
        return "quit"
    if ch in (ord("r"), ord("R")):
        return "restart"
    if ch in (ord("p"), ord("P")):
        if not game.game_over:
            game.paused = not game.paused
            st.paused_by_attention = st.paused_by_resume = False
    elif ch in KEY_DIRS:
        game.move(KEY_DIRS[ch])
    elif ch in PUMP_KEYS:
        game.pump()
    return None


def run(session) -> None:
    """Play Dig Dug until `q` (back to the arcade menu) or SIGTERM. The game
    is saved on every exit path and resumed, paused, on the next run."""
    scr = session.stdscr
    init_colors()
    game_path = session.path("digdug-game.json")
    hiscore_path = session.path("digdug-hiscore.json")
    hiscore = load_hiscore(hiscore_path)

    # A saved, unfinished game resumes exactly where it left off -- paused,
    # behind a banner. Corrupt/incompatible saves are ignored silently.
    game = Game()
    st = LoopState()
    if game_path:
        restored = Game.from_save(load_json_file(game_path))
        if restored is not None and not restored.game_over:
            game = restored
            game.paused = True
            st = LoopState(resume=True)

    help = HelpOverlay(TITLE, HELP)
    lay = Layout(scr)
    game_over_handled = game.game_over
    dirty = True
    last = time.monotonic()
    last_key = None

    try:
        while not session.quitting.is_set():
            now = time.monotonic()
            dt = min(now - last, 0.25)  # a stalled terminal must not warp time
            last = now

            changed, alerts = session.pump()
            if changed:
                dirty = True
            if alerts and session.should_auto_pause and not game.game_over:
                if not game.paused:
                    game.paused = True
                    st.paused_by_attention = True
                dirty = True

            quit_now = False
            while True:
                ch = scr.getch()
                if ch == -1:
                    break
                dirty = True
                if ch == curses.KEY_RESIZE:
                    curses.update_lines_cols()
                    scr.erase()
                    lay = Layout(scr)
                    continue
                result = dispatch_key(ch, game, help, st, session)
                if result == "quit":
                    quit_now = True
                    break
                if result == "restart":
                    # Restart abandons the saved game but banks the score.
                    if game.score > hiscore:
                        hiscore = game.score
                        save_hiscore(hiscore_path, hiscore)
                    if game_path:
                        remove_path(game_path)
                    game.reset()
                    game_over_handled = False
                    st.paused_by_attention = st.paused_by_resume = False
            if quit_now:
                break

            if game.update(dt):
                dirty = True
            # Time-driven visuals (the seconds counter, wobbling rocks and
            # flashing Fygars) redraw whenever what they show changes.
            key = display_key(game, now)
            if key != last_key:
                last_key = key
                dirty = True

            if game.game_over and not game_over_handled:
                game_over_handled = True
                if game_path:
                    remove_path(game_path)
                if game.score > hiscore:
                    hiscore = game.score
                save_hiscore(hiscore_path, hiscore)
                dirty = True

            if dirty:
                draw(game, lay, session, st.resume, hiscore, help)
                scr.noutrefresh()
                curses.doupdate()
                dirty = False

            time.sleep(0.01)
    finally:
        # The one save point: q, SIGTERM, or an exception lands here. A
        # pending death/round transition is resolved first so the save is a
        # consistent, resumable state.
        game.resolve_phases()
        if game_path:
            if game.game_over:
                remove_path(game_path)
            else:
                atomic_save_json(game_path, game.to_save())
        if hiscore_path and game.score > hiscore:
            save_hiscore(hiscore_path, game.score)


if __name__ == "__main__":
    import sys

    import arcade

    sys.exit(arcade.main(["--game", "digdug"] + sys.argv[1:]))
