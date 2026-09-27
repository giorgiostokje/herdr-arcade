#!/usr/bin/env python3
"""Boulder Dash for the Herdr arcade (see arcade.py, which runs it via `run()`).

Structure: everything above the "curses UI" divider is pure game logic --
the cave grid, Rockford, falling and rolling objects, fireflies,
butterflies, amoeba, magic wall, scoring, caves, save/restore and the
layout plan -- with no terminal dependency, so `tests/test_boulderdash.py`
imports this module without a terminal.

Rules in brief: dig through dirt and collect the cave's quota of diamonds
to open the exit, then reach it before the cave timer runs out. Boulders
and diamonds fall when nothing is under them and roll off round things; a
falling one kills Rockford, a resting one does not. Fireflies and
butterflies follow walls and explode on contact (butterflies into
diamonds). The world moves in discrete ticks; each move keypress is queued
and applied on the next tick, one cell per press.
"""

from __future__ import annotations

import curses
import math
import random
import sys
import time

from common import (  # noqa: F401  (agent names re-exported for the tests)
    HELP_KEY,
    AgentSnapshot,
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

GRID_W = 30   # cells; each cell is two terminal columns
GRID_H = 22   # the board frame is the cave's steel border

# Cave tile characters. P, F and B also live in the grid while the game
# runs (Rockford and the enemies block things like any other object).
EMPTY = " "
DIRT = "."
WALL = "#"        # brick: round, destroyed by explosions
STEEL = "@"       # titanium: indestructible (also everything off the grid)
BOULDER = "O"
DIAMOND = "*"
ROCKFORD = "P"
FIREFLY = "F"
BUTTERFLY = "B"
AMOEBA = "A"
MAGIC = "M"       # magic wall: looks like brick until a boulder lands on it
EXIT = "X"        # looks like steel until the quota is met
BOOM = "e"        # an explosion in progress (live grid only)
CAVE_TILES = frozenset((EMPTY, DIRT, WALL, STEEL, BOULDER, DIAMOND, ROCKFORD,
                        FIREFLY, BUTTERFLY, AMOEBA, MAGIC, EXIT))
GRID_TILES = CAVE_TILES | {BOOM}
ROUND = (BOULDER, DIAMOND, WALL)       # things objects roll off
ENEMIES = (FIREFLY, BUTTERFLY)
FALLERS = (BOULDER, DIAMOND)

UP, RIGHT, DOWN, LEFT = 0, 1, 2, 3
DELTAS = ((0, -1), (1, 0), (0, 1), (-1, 0))
FIREFLY_START = LEFT
BUTTERFLY_START = DOWN

# Timing.
TICK = 0.13              # seconds per world tick
MAX_QUEUE = 3            # queued moves; more (key auto-repeat) is dropped
BOOM_TICKS = 4           # an explosion lasts this many ticks
DEATH_TIME = 1.6         # seconds of explosion before the cave restarts
CLEAR_TIME = 1.5
MAGIC_TIME = 15.0        # seconds a magic wall stays active
AMOEBA_P = 0.03          # per amoeba cell per tick: chance to try growing
AMOEBA_MAX = 60          # an amoeba this big turns into boulders

# Scoring and lives.
TIME_POINTS = 3          # per second left on the clock at the exit
START_LIVES = 3
MAX_LIVES = 9
EXTRA_LIFE = 1000        # an extra life every this many points

SAVE_VERSION = 1

# Original caves, 30 x 22. Dict keys: name, quota (diamonds to open the
# exit), time (seconds), value / extra (points per diamond before / after
# the quota is met), map. Every cave's quota and exit are reachable (the
# tests check it statically and by playing each cave through with the real
# physics).
CAVES = [
    {"name": "First Dig", "quota": 12, "time": 100, "value": 10, "extra": 15,
     "map": [
         "P.....O.....*.....O...........",
         ".O..*.....O......O....*...O...",
         "....O..*.....O.........O......",
         "..*.....O.......*..O.........*",
         ".....O.....O.........*...O....",
         "..O....*......##########..O...",
         ".......O..*......O...........*",
         ".*..O.........O.......O..*....",
         "......O...*......*........O...",
         "..O.........O.........O.......",
         ".....*..O......*...O.......*..",
         "##########.......##########...",
         ".O....*......O.......O....*...",
         "...*.....O.....*..........O...",
         "..O.....*...O.......O..*......",
         "......O.........O.............",
         ".*.......O...*.......O....*...",
         "....O.........O.....*.........",
         "..*...O...*......O.......O....",
         ".O.......O.....O.....*.......X",
         "....*.......O.........O...*...",
         ".........*.......O............",
     ]},
    {"name": "Rockfall", "quota": 15, "time": 100, "value": 10, "extra": 20,
     "map": [
         "..O..O..O..O..O..O..O..O..O...",
         "..O..O..O..O..O..O..O..O..O...",
         "..*.....*.....*.....*.....*...",
         "..............................",
         "P....#####......#####.........",
         ".....O.O.O......O.O.O.....O...",
         ".....*.*.*......*.*.*.....*...",
         "..............................",
         "..O......O......O......O......",
         "..O..*...O..*...O..*...O..*...",
         "..............................",
         "#######..########..#######....",
         "..............................",
         "..OOO......OOO......OOO.......",
         "..***......***......***.......",
         "..............................",
         ".....O..O.....O..O.....O..O...",
         ".....*..*.....*..*.....*..*...",
         "..............................",
         "..O....O....O....O....O.......",
         "..*....*....*....*....*......X",
         "..............................",
     ]},
    {"name": "Firefly Ring", "quota": 16, "time": 95, "value": 15,
     "extra": 25,
     "map": [
         "P.......*......O.......*......",
         "..O.......O.........O.....*...",
         ".....#########......*.........",
         "..*..#F      #..O.......O.....",
         ".....# ##### #.....*.......*..",
         "..O..# #...# #....O...........",
         ".....# #.*.# #..*.....O...*...",
         "..*..# ##### #.......*........",
         ".....#       #...O.......O....",
         "..O..#########.....*..........",
         "......*....O...........*....O.",
         "..*.......O.....#########.....",
         ".....O..........#       #..*..",
         ".*......*...O...# ##### #.....",
         "....O...........# #.*.# #..O..",
         "..*......O..*...# ##### #.....",
         ".....*..........#      F#..*..",
         "..O.......O.....#########.....",
         ".....*.....O..........*.......",
         "..*......*.....O..*.....O....X",
         "....O.......*........O........",
         "..........O.......*...........",
     ]},
    {"name": "Butterfly House", "quota": 18, "time": 95, "value": 15,
     "extra": 25,
     "map": [
         "P......*.....O.......*......O.",
         "..O.....O.*.......O..O...*....",
         "...######.#####.######.#####..",
         "...#B          .           #..",
         "...#  .....    .    .....  #..",
         "...#  .*.*.    .    .*.*.  #..",
         "...#  .....    .    .....  #..",
         "...#          B.           #..",
         "...#########################..",
         "..*.....O......*.....O....*...",
         ".......*....O......*......O...",
         "..O.........*.........O.......",
         "......*..O.......O.......*....",
         "..*........................O..",
         "....O.....*....O....*.........",
         "..........O.........O....*....",
         ".*...O.......*..........O.....",
         "......*....O......*.......O...",
         "..O........*..O.........*.....",
         "......O..*........O..........X",
         "..*..........*.........O..*...",
         "...........O.......*..........",
     ]},
    {"name": "Magic Mill", "quota": 20, "time": 90, "value": 20,
     "extra": 30,
     "map": [
         "P.....*......O.......*........",
         "..O......*........O......*..O.",
         "....OOOOOO.....*.....OOOOOO...",
         "....OOOOOO...........OOOOOO...",
         "..*.......O....O..*...........",
         "......*.........*.......*.....",
         "..MMMMMMMMMM........MMMMMMMMM.",
         "..                           .",
         "..        ...*...O........*...",
         "..........*..........O........",
         "..O....*.......O.......*......",
         "######.....#########.....#####",
         "...*....O...........O.....*...",
         ".O.......*....O..*.......O....",
         "....*.O.................*.....",
         "......O..*..F       O.....*...",
         "..*.........#########.......*.",
         ".....O..*........O.....O......",
         "..O.......*..O.......*......O.",
         "........O......*....O........X",
         ".*...*.......O.........*......",
         "......O...*.......O......*....",
     ]},
    {"name": "Amoeba Creep", "quota": 22, "time": 90, "value": 20,
     "extra": 30,
     "map": [
         "P......*.......O.......*......",
         "..O.......*.........O....*....",
         "......O.......*...........O...",
         "..*......O.......O....*.......",
         "....#######......#######......",
         "..O....*.....*......*.....O...",
         "......*....O.....*.......*....",
         ".*.....O.........O.....*......",
         "...O.......*..........O....*..",
         "......*........*...........*..",
         "############.......###########",
         "..*.....O.....*......O....*...",
         ".....O.......*...O............",
         "..........O..........*....O...",
         "..*...*.........*.........*...",
         "....O......O........O.........",
         ".*........*.....*......*...*..",
         "......O.........O.............",
         "..*......*.......O.....*...O..",
         ".....O.......*................",
         "..O......*.....O.....*.......X",
         "A.........*.........O.........",
     ]},
    {"name": "Crystal Vault", "quota": 26, "time": 85, "value": 25,
     "extra": 40,
     "map": [
         "P....O.....*.....O.....*......",
         "..*......O.....*......O....*..",
         "#######.......######......####",
         "F      .*...*.#B   #.*..*.   F",
         "######.......O######.O.....###",
         "...*..O..*.........*.....*....",
         ".O.......*.....O.......O......",
         "....*.......*......*......*...",
         "..O....O.......O.......O......",
         "#####.....########.....#######",
         "..*....*.....*.....*.....*....",
         ".....O.....O.....O.....O......",
         "..*.....*.....*.....*.....*...",
         "........O..........O..........",
         "###.....####.....####.....####",
         "  #..*..#  #..*..#  #..*..#   ",
         "F #.....#  #.....#  #.....# F ",
         "  #..O..#  #..O..#  #..O..#   ",
         "..*.......*........*.......*..",
         ".....O.......O........O.......",
         "..*.....*.......*.......*....X",
         "......O.....O........O........",
     ]},
    {"name": "Last Stand", "quota": 28, "time": 80, "value": 30,
     "extra": 50,
     "map": [
         "P....*O.O.O.O......*.....O....",
         "..O.....*.......O.......*.....",
         "....MMMMMMMM.......O..OOOO....",
         "..*.        ....*.............",
         "....        ..........MMMM..*.",
         ".O..........O...*.....    ....",
         "..*....*..........O...    ..*.",
         "######......#######......#####",
         "...*....O.......*......*......",
         "..O..*...............O.....*..",
         "......#######B     ####.......",
         "..*...#          F    #...*...",
         "......#################.......",
         ".*......O.....*........O...*..",
         "....*.......O......*..........",
         "..O.....*........*.....O...*..",
         "......O......*.........*......",
         "..*........O......O.......*...",
         ".....*..O.........*...O.......",
         "..O.........*..........*.....X",
         "......*.........O.....*.......",
         "A..........*........O.........",
     ]},
]


def parse_cave(rows):
    """(grid rows as lists, Rockford's start, exit position) from a cave's
    strings. Raises ValueError for a malformed cave."""
    if len(rows) != GRID_H:
        raise ValueError("cave height")
    grid = []
    start = exit_pos = None
    for y, row in enumerate(rows):
        if len(row) != GRID_W:
            raise ValueError("cave width")
        for x, ch in enumerate(row):
            if ch not in CAVE_TILES:
                raise ValueError("tile %r" % ch)
            if ch == ROCKFORD:
                if start is not None:
                    raise ValueError("two Rockfords")
                start = (x, y)
            elif ch == EXIT:
                if exit_pos is not None:
                    raise ValueError("two exits")
                exit_pos = (x, y)
        grid.append(list(row))
    if start is None:
        raise ValueError("no Rockford")
    if exit_pos is None:
        raise ValueError("no exit")
    return grid, start, exit_pos


def time_factor(level: int, count: int = len(CAVES)) -> float:
    """Each pass through the whole cave set leaves 15% less time."""
    loops = (level - 1) // count
    return max(0.55, 1.0 - 0.15 * loops)


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


class Phase:
    """A timed interlude as an explicit logic phase: 'dying' (the world
    keeps ticking so the explosion plays out) or 'cave_clear' (frozen). Its
    consequences apply when it completes -- via update(), or immediately via
    finish_phase()."""

    __slots__ = ("kind", "elapsed", "duration")

    def __init__(self, kind, duration):
        self.kind = kind
        self.elapsed = 0.0
        self.duration = duration


class Game:
    """Full game state and every state transition. No I/O."""

    def __init__(self, rng=None, level: int = 1, caves=None):
        self.rng = rng if rng is not None else random.Random()
        self.caves = caves if caves is not None else CAVES
        self.score = 0
        self.lives = START_LIVES
        self.next_life = EXTRA_LIFE
        self.level = level
        self.paused = False
        self.game_over = False
        self.phase = None
        self.last_bonus = 0
        self.load_cave()

    def reset(self) -> None:
        self.__init__(self.rng, 1, self.caves)

    # -- cave ---------------------------------------------------------------

    @property
    def cave_index(self) -> int:
        return (self.level - 1) % len(self.caves)

    @property
    def cave(self) -> dict:
        return self.caves[self.cave_index]

    @property
    def quota(self) -> int:
        return self.cave["quota"]

    def load_cave(self) -> None:
        """(Re)start the current cave from its definition."""
        grid, (rx, ry), exit_pos = parse_cave(self.cave["map"])
        self.grid = grid
        self.rx, self.ry = rx, ry
        self.exit_pos = exit_pos
        self.alive = True
        self.facing = RIGHT
        self.falling = set()
        self.enemies = {}                  # (x, y) -> direction
        for y, row in enumerate(grid):
            for x, t in enumerate(row):
                if t == FIREFLY:
                    self.enemies[(x, y)] = FIREFLY_START
                elif t == BUTTERFLY:
                    self.enemies[(x, y)] = BUTTERFLY_START
        self.booms = {}                    # (x, y) -> [ticks left, result]
        self.magic = "dormant"             # dormant / active / expired
        self.magic_t = 0.0
        self.collected = 0
        self.exit_open = False
        self.time_left = float(self.cave["time"]) * time_factor(
            self.level, len(self.caves))
        self.acc = 0.0
        self.ticks = 0
        self.queue = []                    # [(direction, snap), ...]
        self.snap_armed = False
        self.phase = None

    # -- queries --------------------------------------------------------------

    @staticmethod
    def in_bounds(x, y) -> bool:
        return 0 <= x < GRID_W and 0 <= y < GRID_H

    def cell(self, x, y) -> str:
        """Tile at (x, y); everything off the grid is steel."""
        if not self.in_bounds(x, y):
            return STEEL
        return self.grid[y][x]

    def diamond_value(self) -> int:
        """Points the next diamond is worth."""
        if self.collected >= self.quota:
            return self.cave["extra"]
        return self.cave["value"]

    def seconds_left(self) -> int:
        return int(math.ceil(max(0.0, self.time_left) - 1e-9))

    def can_act(self) -> bool:
        return (not self.game_over and not self.paused and self.phase is None
                and self.alive)

    # -- input ------------------------------------------------------------------

    def queue_move(self, d: int, snap: bool = False) -> bool:
        """Queue one step (or snap) in direction d for the next free tick.
        Each keypress is one entry, applied once; beyond MAX_QUEUE pending
        entries (a held key's auto-repeat outrunning the ticks) presses are
        dropped so Rockford stops soon after the key is released."""
        if not self.can_act() or len(self.queue) >= MAX_QUEUE:
            return False
        self.queue.append((d, bool(snap)))
        return True

    def toggle_snap(self) -> bool:
        """Arm (or disarm) snap: the next direction key snaps instead of
        moving."""
        if not self.can_act():
            return False
        self.snap_armed = not self.snap_armed
        return True

    def direction_key(self, d: int) -> bool:
        snap = self.snap_armed
        if self.queue_move(d, snap):
            self.snap_armed = False
            return True
        return False

    # -- rockford ---------------------------------------------------------------

    def _add_score(self, points: int) -> None:
        self.score += points
        while self.score >= self.next_life:
            self.next_life += EXTRA_LIFE
            self.lives = min(MAX_LIVES, self.lives + 1)

    def _collect(self, x, y) -> None:
        self._add_score(self.diamond_value())
        self.collected += 1
        self.grid[y][x] = EMPTY
        self.falling.discard((x, y))
        if self.collected >= self.quota:
            self.exit_open = True

    def _move_rockford(self, tx, ty) -> None:
        self.grid[self.ry][self.rx] = EMPTY
        self.grid[ty][tx] = ROCKFORD
        self.rx, self.ry = tx, ty

    def _rockford_act(self) -> None:
        if not self.queue:
            return
        d, snap = self.queue.pop(0)
        dx, dy = DELTAS[d]
        if dx:
            self.facing = d
        tx, ty = self.rx + dx, self.ry + dy
        t = self.cell(tx, ty)
        if snap:
            if t == DIRT:
                self.grid[ty][tx] = EMPTY
            elif t == DIAMOND:
                self._collect(tx, ty)
            return
        if t in (EMPTY, DIRT):
            self._move_rockford(tx, ty)
        elif t == DIAMOND:
            self._collect(tx, ty)
            self._move_rockford(tx, ty)
        elif t == EXIT and self.exit_open:
            self._move_rockford(tx, ty)
            self._complete_cave()
        elif (t == BOULDER and dy == 0 and (tx, ty) not in self.falling
              and self.cell(tx + dx, ty) == EMPTY):
            self.grid[ty][tx + dx] = BOULDER
            self._move_rockford(tx, ty)

    def _complete_cave(self) -> None:
        self.last_bonus = self.seconds_left() * TIME_POINTS
        self._add_score(self.last_bonus)
        self.queue = []
        self.snap_armed = False
        self.phase = Phase("cave_clear", CLEAR_TIME)

    def _die(self) -> None:
        self.alive = False
        self.queue = []
        self.snap_armed = False
        if self.phase is None:
            self.phase = Phase("dying", DEATH_TIME)

    # -- world ------------------------------------------------------------------

    def explode(self, cx, cy, result=EMPTY) -> None:
        """3x3 explosion around (cx, cy). Steel and the exit survive; the
        rest (Rockford included) is consumed and becomes `result` (empty for
        a firefly, diamonds for a butterfly) once the blast dies down."""
        for y in range(cy - 1, cy + 2):
            for x in range(cx - 1, cx + 2):
                if not self.in_bounds(x, y):
                    continue
                t = self.grid[y][x]
                if t in (STEEL, EXIT):
                    continue
                if t == ROCKFORD:
                    self._die()
                self.enemies.pop((x, y), None)
                self.falling.discard((x, y))
                self.grid[y][x] = BOOM
                self.booms[(x, y)] = [BOOM_TICKS, result]

    def _place(self, x, y, t, moved) -> None:
        """Put a falling object at (x, y) (it has moved this tick)."""
        self.grid[y][x] = t
        self.falling.add((x, y))
        moved.add((x, y))

    def _fall(self, x, y, t, moved) -> None:
        was_falling = (x, y) in self.falling
        below = self.cell(x, y + 1)
        if below == EMPTY:
            self.grid[y][x] = EMPTY
            self.falling.discard((x, y))
            self._place(x, y + 1, t, moved)
            return
        if was_falling:
            if below == ROCKFORD:
                self.falling.discard((x, y))
                self.explode(x, y + 1)
                return
            if below in ENEMIES:
                self.falling.discard((x, y))
                self.explode(x, y + 1, DIAMOND if below == BUTTERFLY else EMPTY)
                return
            if below == MAGIC and self.magic != "expired":
                if self.magic == "dormant":
                    self.magic = "active"
                    self.magic_t = MAGIC_TIME
                self.grid[y][x] = EMPTY
                self.falling.discard((x, y))
                if self.cell(x, y + 2) == EMPTY:
                    other = DIAMOND if t == BOULDER else BOULDER
                    self._place(x, y + 2, other, moved)
                return
        if below in ROUND and (x, y + 1) not in self.falling:
            for dx in (-1, 1):
                if (self.cell(x + dx, y) == EMPTY
                        and self.cell(x + dx, y + 1) == EMPTY):
                    self.grid[y][x] = EMPTY
                    self.falling.discard((x, y))
                    self._place(x + dx, y, t, moved)
                    return
        if (x, y + 1) not in self.falling:
            self.falling.discard((x, y))  # landed

    def _enemy(self, x, y, t, moved) -> None:
        for dx, dy in DELTAS:
            if self.cell(x + dx, y + dy) in (ROCKFORD, AMOEBA):
                self.explode(x, y, DIAMOND if t == BUTTERFLY else EMPTY)
                return
        d = self.enemies[(x, y)]
        turn = 3 if t == FIREFLY else 1          # firefly prefers left
        for nd in ((d + turn) % 4, d):
            dx, dy = DELTAS[nd]
            if self.cell(x + dx, y + dy) == EMPTY:
                del self.enemies[(x, y)]
                self.grid[y][x] = EMPTY
                self.grid[y + dy][x + dx] = t
                self.enemies[(x + dx, y + dy)] = nd
                moved.add((x + dx, y + dy))
                return
        self.enemies[(x, y)] = (d - turn) % 4    # blocked: turn the other way

    def _amoeba(self) -> None:
        cells = [(x, y) for y in range(GRID_H) for x in range(GRID_W)
                 if self.grid[y][x] == AMOEBA]
        if not cells:
            return
        if len(cells) >= AMOEBA_MAX:
            for x, y in cells:
                self.grid[y][x] = BOULDER
            return
        if not any(self.cell(x + dx, y + dy) in (EMPTY, DIRT)
                   for x, y in cells for dx, dy in DELTAS):
            for x, y in cells:
                self.grid[y][x] = DIAMOND     # suffocated amoeba
            return
        for x, y in cells:
            if self.rng.random() < AMOEBA_P:
                dx, dy = DELTAS[self.rng.randrange(4)]
                if self.cell(x + dx, y + dy) in (EMPTY, DIRT):
                    self.grid[y + dy][x + dx] = AMOEBA

    def step(self) -> None:
        """One world tick: explosions settle, Rockford takes one queued
        action, then a top-to-bottom, left-to-right scan moves every object
        and enemy once, then the amoeba grows and the clocks run."""
        self.ticks += 1
        for pos in sorted(self.booms):
            self.booms[pos][0] -= 1
            if self.booms[pos][0] <= 0:
                x, y = pos
                self.grid[y][x] = self.booms.pop(pos)[1]
        if self.can_act():
            self._rockford_act()
            if self.phase is not None:
                return          # reached the exit: the world freezes
        moved = set()
        for y in range(GRID_H):
            row = self.grid[y]
            for x in range(GRID_W):
                if (x, y) in moved:
                    continue
                t = row[x]
                if t in FALLERS:
                    self._fall(x, y, t, moved)
                elif t in ENEMIES:
                    self._enemy(x, y, t, moved)
        self._amoeba()
        if self.magic == "active":
            self.magic_t -= TICK
            if self.magic_t <= 0:
                self.magic_t = 0.0
                self.magic = "expired"
        if self.alive and self.phase is None:
            self.time_left -= TICK
            if self.time_left <= 0:
                self.time_left = 0.0
                self.explode(self.rx, self.ry)     # out of time

    # -- time -------------------------------------------------------------------

    def finish_phase(self) -> None:
        """Resolve a pending phase immediately (quit, save)."""
        phase = self.phase
        if phase is None:
            return
        self.phase = None
        if phase.kind == "dying":
            self.lives -= 1
            if self.lives <= 0:
                self.lives = 0
                self.game_over = True
            else:
                self.load_cave()
        elif phase.kind == "cave_clear":
            self.level += 1
            self.load_cave()

    def update(self, dt: float) -> bool:
        """Advance the game by `dt` seconds. True if anything changed."""
        if self.paused or self.game_over:
            return False
        if self.phase is not None:
            self.phase.elapsed += dt
            if self.phase.kind == "dying":
                self.acc += dt
                while self.acc >= TICK:
                    self.acc -= TICK
                    self.step()
            if self.phase.elapsed >= self.phase.duration:
                self.finish_phase()
            return True
        self.acc += dt
        changed = False
        while self.acc >= TICK and self.phase is None:
            self.acc -= TICK
            self.step()
            changed = True
        return changed

    # -- save / restore -----------------------------------------------------------

    def to_save(self) -> dict:
        """Serializable snapshot. The caller resolves any phase first
        (finish_phase), so a save never holds a death/clear animation."""
        return {
            "version": SAVE_VERSION,
            "level": self.level,
            "score": self.score,
            "lives": self.lives,
            "next_life": self.next_life,
            "grid": ["".join(row) for row in self.grid],
            "rockford": [self.rx, self.ry],
            "facing": self.facing,
            "falling": sorted([x, y] for x, y in self.falling),
            "enemies": sorted([x, y, d] for (x, y), d in self.enemies.items()),
            "booms": sorted([x, y, b[0], b[1]] for (x, y), b in self.booms.items()),
            "magic": self.magic,
            "magic_t": self.magic_t,
            "collected": self.collected,
            "time_left": self.time_left,
            "acc": self.acc,
            "ticks": self.ticks,
            "queue": [[d, s] for d, s in self.queue],
            "snap_armed": self.snap_armed,
            "rng": _rng_to_json(self.rng),
        }

    @classmethod
    def from_save(cls, data, rng=None):
        """Rebuild a saved game, or None for anything corrupt/incompatible."""
        try:
            if not isinstance(data, dict) or data.get("version") != SAVE_VERSION:
                return None
            level = data["level"]
            if not isinstance(level, int) or isinstance(level, bool) or level < 1:
                return None
            game = cls(rng, level)
            grid = data["grid"]
            if not isinstance(grid, list) or len(grid) != GRID_H:
                return None
            for row in grid:
                if not isinstance(row, str) or len(row) != GRID_W:
                    return None
                if any(ch not in GRID_TILES for ch in row):
                    return None
            game.grid = [list(row) for row in grid]

            def pos(v, tiles):
                x, y = v
                if not (isinstance(x, int) and isinstance(y, int)
                        and game.in_bounds(x, y) and game.grid[y][x] in tiles):
                    raise ValueError("position")
                return x, y

            rx, ry = pos(data["rockford"], (ROCKFORD,))
            if sum(r.count(ROCKFORD) for r in grid) != 1:
                return None
            if sum(r.count(EXIT) for r in grid) != 1:
                return None
            game.rx, game.ry = rx, ry
            game.exit_pos = next((x, y) for y, r in enumerate(grid)
                                 for x, ch in enumerate(r) if ch == EXIT)
            facing = data["facing"]
            if facing not in (LEFT, RIGHT):
                return None
            game.facing = facing
            game.falling = set(pos(v, FALLERS) for v in data["falling"])
            enemies = {}
            for x, y, d in data["enemies"]:
                if d not in (0, 1, 2, 3):
                    return None
                enemies[pos((x, y), ENEMIES)] = d
            if len(enemies) != sum(r.count(FIREFLY) + r.count(BUTTERFLY)
                                   for r in grid):
                return None
            game.enemies = enemies
            booms = {}
            for x, y, t, result in data["booms"]:
                if not (isinstance(t, int) and 0 < t <= BOOM_TICKS
                        and result in (EMPTY, DIAMOND)):
                    return None
                booms[pos((x, y), (BOOM,))] = [t, result]
            if len(booms) != sum(r.count(BOOM) for r in grid):
                return None
            game.booms = booms
            if data["magic"] not in ("dormant", "active", "expired"):
                return None
            game.magic = data["magic"]
            game.magic_t = float(data["magic_t"])
            collected = data["collected"]
            if not isinstance(collected, int) or collected < 0:
                return None
            game.collected = collected
            game.exit_open = collected >= game.quota
            game.time_left = float(data["time_left"])
            game.acc = float(data["acc"])
            game.ticks = int(data["ticks"])
            queue = []
            for d, s in data["queue"]:
                if d not in (0, 1, 2, 3):
                    return None
                queue.append((d, bool(s)))
            if len(queue) > MAX_QUEUE:
                return None
            game.queue = queue
            game.snap_armed = bool(data["snap_armed"])
            score, lives, nxt = data["score"], data["lives"], data["next_life"]
            if not (isinstance(score, int) and isinstance(lives, int)
                    and isinstance(nxt, int) and lives >= 1 and score >= 0):
                return None
            game.score, game.lives, game.next_life = score, lives, nxt
            state = _rng_from_json(data.get("rng"))
            if state is None:
                return None
            game.rng.setstate(state)
            return game
        except (KeyError, TypeError, ValueError, IndexError):
            return None

    def clone(self):
        """An independent copy (for lookahead in solvers and tests)."""
        g = Game.__new__(Game)
        g.__dict__.update(self.__dict__)
        g.rng = random.Random()
        g.rng.setstate(self.rng.getstate())
        g.grid = [row[:] for row in self.grid]
        g.falling = set(self.falling)
        g.enemies = dict(self.enemies)
        g.booms = {k: v[:] for k, v in self.booms.items()}
        g.queue = list(self.queue)
        if self.phase is not None:
            g.phase = Phase(self.phase.kind, self.phase.duration)
            g.phase.elapsed = self.phase.elapsed
        return g


# --------------------------------------------------------------------------
# Controls, help and layout (pure: no terminal needed)
# --------------------------------------------------------------------------

TITLE = "Boulder Dash"

# Every key the game reacts to (see handle_key and dispatch_key).
CONTROLS = [
    ("arrows", "move / dig"),
    ("wasd hjkl", "as arrows"),
    ("space+dir", "snap: dig or grab next to you without moving"),
    ("?", "help"),
    ("p / r", "pause / restart"),
    ("q", "menu"),
]

HELP = [
    ("Goal",
     "Guide Rockford through each cave. Collect the number of diamonds the "
     "cave asks for (the stats show gems collected/needed) to open the "
     "exit, then walk into the exit before the cave timer runs out."),
    ("Controls",
     "- arrows (or w a s d, h j k l): move one cell per keypress, digging "
     "through dirt and picking up diamonds on the way. Hold a key to keep "
     "going.\n"
     "- space then a direction: snap. Rockford digs the dirt or grabs the "
     "diamond in that direction without stepping into the cell (the "
     "classic fire+direction). The stats show SNAP while it is armed; "
     "press space again to cancel.\n"
     "- ? this help (the game pauses), p pause, r restart from cave 1, "
     "q back to the menu (the game is saved and resumes paused)."),
    ("Ticks and the move queue",
     "The cave moves in steps of 0.13 seconds. Every keypress is queued and "
     "applied on the next step: one press is exactly one cell, never lost "
     "and never doubled. Up to 3 presses wait in the queue; a held key's "
     "extra auto-repeats beyond that are dropped, so Rockford stops soon "
     "after you let go."),
    ("Falling and rolling",
     "- Boulders and diamonds fall when there is nothing under them.\n"
     "- A resting one rolls off anything round (a boulder, a diamond or a "
     "brick wall) when the cell beside it and the cell below that are "
     "free, left first.\n"
     "- A FALLING boulder or diamond that lands on Rockford kills him. A "
     "resting one does not: you can stand right under a boulder. Step away "
     "downward and it follows you, so leave sideways.\n"
     "- Diamonds can be grabbed even while they fall."),
    ("Pushing",
     "Walk sideways into a resting boulder with an empty cell behind it and "
     "you push it one cell. Only one boulder at a time, never up or down."),
    ("Fireflies and butterflies",
     "- Fireflies ({}) and butterflies (}{) patrol empty space, hugging "
     "walls: fireflies turn left whenever they can, butterflies right.\n"
     "- Touching one (being next to it, not diagonally) makes it explode "
     "3x3, and Rockford with it. Tunnels you dig are empty space they can "
     "follow.\n"
     "- Drop a boulder or diamond on one to blow it up from a distance. A "
     "firefly leaves an empty crater; a butterfly leaves nine diamonds.\n"
     "- Explosions destroy dirt, brick, boulders and diamonds, but not "
     "steel or the exit."),
    ("Amoeba and magic wall",
     "- Amoeba (%%%%) slowly grows into dirt and empty space. Enclosed so it "
     "cannot grow, it turns into diamonds; if it reaches %d cells it turns "
     "into boulders. Fireflies and butterflies explode when they touch it.\n"
     "- A magic wall looks like brick. When a falling boulder lands on it, "
     "the wall starts to shimmer (~~) for %d seconds: every boulder that "
     "falls through comes out below as a diamond, and a diamond as a "
     "boulder (if the cell under the wall is free). Afterwards it is plain "
     "wall for good." % (AMOEBA_MAX, MAGIC_TIME)),
    ("Caves and time",
     "There are 8 caves. Each has a timer; at 0 Rockford explodes. Losing "
     "a life restarts the cave. You start with 3 lives. After the last "
     "cave they repeat with less time."),
    ("Scoring",
     "- a diamond: the cave's value (10 in cave 1, more in later caves), "
     "and more once the quota is met (15 in cave 1). The stats show what "
     "the next one is worth.\n"
     "- reaching the exit: %d points per second left on the clock\n"
     "- an extra life every %d points (up to %d)\n"
     "Your best score is kept between games."
     % (TIME_POINTS, EXTRA_LIFE, MAX_LIVES)),
    ("Legend",
     "- @> <@  Rockford\n"
     "- light shaded block: dirt\n"
     "- red shaded block: brick wall (a magic wall looks the same)\n"
     "- white solid block: steel (and the closed exit)\n"
     "- ()  boulder;  <>  diamond\n"
     "- {}  firefly;  }{  butterfly\n"
     "- %%  amoeba;  ~~  active magic wall\n"
     "- []  the open exit (blinking)\n"
     "- **  explosion"),
    ("Tips",
     "- Look up before you dig: taking the dirt under a boulder lets it "
     "fall, and the cell you just left is where it lands.\n"
     "- Snap lets you take a diamond from under a boulder, or dig a "
     "firefly's route open, while standing safely to the side.\n"
     "- Bank diamonds past the quota only if the clock allows: every "
     "second left is worth %d points at the exit.\n"
     "- p pauses at any time; the clock stops while paused or in help."
     % TIME_POINTS),
]

# The play window is 30 cells x 2 columns + 2 border = 62 wide and
# 22 + 2 = 24 rows tall. At the 93x28 popup the composition is board +
# 1 gap + a 30-wide side column (stats, keys, agents): exactly 93 wide.
BOARD_W = GRID_W * 2 + 2           # 62
BOARD_H = GRID_H + 2               # 24
STATS_H = 6                        # frame + 4 lines at the full side width
BOX_MIN_H = 3                      # frame + 1 line
AGENTS_MIN_H = 4                   # frame + an agent + "+N more"
AGENTS_MIN_W = 26                  # narrower, the roster is not worth it
MIN_COLS = BOARD_W                 # 62
MIN_ROWS = BOARD_H + 1             # 25: header + board


def layout_plan(cols: int, rows: int) -> dict:
    """Pure layout decision: tier plus each region's (y, x, height, width).
    Tiers: 'full' (board + side column: stats, controls, agents), 'side'
    (side column without agents: the roster drops first, when the column
    is narrower than AGENTS_MIN_W), 'board' (no side column: stats go in
    the header and a key hint row sits at the bottom -- or in the header
    when there is no spare row), 'too_small'."""
    plan = {"tier": "too_small", "too_small": True, "board": None,
            "stats": None, "controls": None, "agents": None, "hint": None,
            "hint_in_header": False}
    if cols < MIN_COLS or rows < MIN_ROWS:
        return plan
    plan["too_small"] = False
    side_w = side_width(cols, BOARD_W)
    if not side_w:
        plan["tier"] = "board"
        if rows >= MIN_ROWS + 1:
            y, x = center_origin(cols, rows - 1, BOARD_W, BOARD_H)
            plan["hint"] = (rows - 1, 0, 1, cols)
        else:
            y, x = center_origin(cols, rows, BOARD_W, BOARD_H)
            plan["hint_in_header"] = True
        plan["board"] = (y, x, BOARD_H, BOARD_W)
        return plan
    comp_w = BOARD_W + 1 + side_w
    y, x = center_origin(cols, rows, comp_w, BOARD_H)
    plan["board"] = (y, x, BOARD_H, BOARD_W)
    sx = x + BOARD_W + 1
    # Height priority: controls > stats > agents.
    ctrl_h = min(controls_height(CONTROLS, side_w), BOARD_H)
    left = BOARD_H - ctrl_h
    stats_h = min(STATS_H, left) if left >= BOX_MIN_H else 0
    left -= stats_h
    agents_h = left if (left >= AGENTS_MIN_H and side_w >= AGENTS_MIN_W) else 0
    if not agents_h and stats_h:
        stats_h += left  # no agents box: stats take the leftover rows
    elif not agents_h and not stats_h:
        ctrl_h += left
    plan["tier"] = "full" if agents_h else "side"
    cy = y
    if stats_h:
        plan["stats"] = (cy, sx, stats_h, side_w)
        cy += stats_h
    plan["controls"] = (cy, sx, ctrl_h, side_w)
    cy += ctrl_h
    if agents_h:
        plan["agents"] = (cy, sx, agents_h, side_w)
    return plan


def key_hint() -> str:
    """Compact key hint for tiers without a controls box; '? help' first so
    truncation never drops it."""
    short = [("?", "help"), ("arrows/wasd/hjkl", "move"),
             ("space+dir", "snap"), ("p", "pause"), ("r", "restart"),
             ("q", "menu")]
    return controls_hint(short)


def gems_text(game) -> str:
    if game.exit_open:
        return "gems %d EXIT OPEN" % game.collected
    return "gems %d/%d" % (game.collected, game.quota)


def stats_items(game, best: int) -> list:
    items = ["score %d" % game.score, "best %d" % max(best, game.score),
             "cave %d" % game.level, "lives %d" % game.lives,
             gems_text(game), "next +%d" % game.diamond_value(),
             "time %d" % game.seconds_left()]
    if game.snap_armed:
        items.append("SNAP")
    return items


def pack_items(items, width: int) -> list:
    """Greedily pack short items into lines of at most `width` columns."""
    lines = []
    for item in items:
        if lines and len(lines[-1]) + 2 + len(item) <= width:
            lines[-1] += "  " + item
        else:
            lines.append(item[:width])
    return lines


# --------------------------------------------------------------------------
# curses UI
# --------------------------------------------------------------------------

# Colour pairs 40-59 are shared by the later games (see common.py).
PAIR_DIRT, PAIR_WALL, PAIR_STEEL, PAIR_BOULDER = 40, 41, 42, 43
PAIR_DIAMOND, PAIR_ROCKFORD, PAIR_FIREFLY, PAIR_BUTTERFLY = 44, 45, 46, 47
PAIR_AMOEBA, PAIR_MAGIC, PAIR_EXIT, PAIR_BOOM = 48, 49, 50, 51

GLYPHS = {
    DIRT: ("░░", PAIR_DIRT),
    WALL: ("▒▒", PAIR_WALL),
    MAGIC: ("▒▒", PAIR_WALL),
    STEEL: ("██", PAIR_STEEL),
    EXIT: ("██", PAIR_STEEL),
    BOULDER: ("()", PAIR_BOULDER),
    DIAMOND: ("<>", PAIR_DIAMOND),
    FIREFLY: ("{}", PAIR_FIREFLY),
    BUTTERFLY: ("}{", PAIR_BUTTERFLY),
    AMOEBA: ("%%", PAIR_AMOEBA),
    BOOM: ("**", PAIR_BOOM),
}
ROCKFORD_GLYPH = {LEFT: "<@", RIGHT: "@>"}
EXIT_OPEN_GLYPH = "[]"
MAGIC_ACTIVE_GLYPH = "~~"
BOLD_TILES = (BOULDER, DIAMOND, FIREFLY, BUTTERFLY, BOOM)


def init_colors() -> None:
    bg = default_bg()
    curses.init_pair(PAIR_DIRT, curses.COLOR_YELLOW, bg)
    curses.init_pair(PAIR_WALL, curses.COLOR_RED, bg)
    curses.init_pair(PAIR_STEEL, curses.COLOR_WHITE, bg)
    curses.init_pair(PAIR_BOULDER, curses.COLOR_WHITE, bg)
    curses.init_pair(PAIR_DIAMOND, curses.COLOR_CYAN, bg)
    curses.init_pair(PAIR_ROCKFORD, curses.COLOR_GREEN, bg)
    curses.init_pair(PAIR_FIREFLY, curses.COLOR_RED, bg)
    curses.init_pair(PAIR_BUTTERFLY, curses.COLOR_MAGENTA, bg)
    curses.init_pair(PAIR_AMOEBA, curses.COLOR_GREEN, bg)
    curses.init_pair(PAIR_MAGIC, curses.COLOR_MAGENTA, bg)
    curses.init_pair(PAIR_EXIT, curses.COLOR_CYAN, bg)
    curses.init_pair(PAIR_BOOM, curses.COLOR_YELLOW, bg)


class Layout:
    """Screen regions, chosen by size tier; rebuilt on resize."""

    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.rows, self.cols = stdscr.getmaxyx()
        self.plan = layout_plan(self.cols, self.rows)
        self.too_small = self.plan["too_small"]
        self.tier = self.plan["tier"]
        self.board = self.stats = self.controls = self.agents = None
        if self.too_small:
            return
        for name in ("board", "stats", "controls", "agents"):
            region = self.plan[name]
            if region is not None:
                y, x, h, w = region
                setattr(self, name, stdscr.derwin(h, w, y, x))

    def boxes(self):
        for win in (self.board, self.stats, self.agents):
            if win is not None:
                win.erase()
                frame(win)
        if self.controls is not None:
            self.controls.erase()  # draw_controls frames it


def _center(win, y, text, attr=0):
    w = win.getmaxyx()[1]
    safe_addstr(win, y, max(0, (w - len(text)) // 2), text[: max(0, w)], attr)


def draw_board(bw, game) -> None:
    blink = game.ticks % 4 < 2
    for y in range(GRID_H):
        row = game.grid[y]
        for x in range(GRID_W):
            t = row[x]
            cx = 1 + x * 2
            if t == EMPTY:
                continue
            if t == ROCKFORD:
                safe_addstr(bw, 1 + y, cx, ROCKFORD_GLYPH[game.facing],
                            curses.color_pair(PAIR_ROCKFORD) | curses.A_BOLD)
                continue
            if t == EXIT and game.exit_open:
                if blink:
                    safe_addstr(bw, 1 + y, cx, EXIT_OPEN_GLYPH,
                                curses.color_pair(PAIR_EXIT) | curses.A_BOLD)
                continue
            if t == MAGIC and game.magic == "active":
                glyph = MAGIC_ACTIVE_GLYPH if blink else "▒▒"
                safe_addstr(bw, 1 + y, cx, glyph,
                            curses.color_pair(PAIR_MAGIC) | curses.A_BOLD)
                continue
            glyph = GLYPHS.get(t)
            if glyph is not None:
                attr = curses.color_pair(glyph[1])
                if t in BOLD_TILES:
                    attr |= curses.A_BOLD
                text = glyph[0]
                if t == BOOM and not blink:
                    text = "++"
                safe_addstr(bw, 1 + y, cx, text, attr)


def draw(game, lay, session, resume=False, best=0, help_overlay=None):
    scr = lay.stdscr
    scr.erase()
    if lay.too_small:
        msg = "pane too small: need %dx%d, have %dx%d" % (
            MIN_COLS, MIN_ROWS, lay.cols, lay.rows)
        y = lay.rows // 2
        safe_addstr(scr, y, max(0, (lay.cols - len(msg)) // 2),
                    msg[: max(0, lay.cols - 1)], curses.A_BOLD)
        msg2 = "resize the pane (? help, q menu)"
        safe_addstr(scr, y + 1, max(0, (lay.cols - len(msg2)) // 2),
                    msg2[: max(0, lay.cols - 1)])
        if help_overlay is not None:
            help_overlay.draw(scr)
        return

    items = stats_items(game, best)
    stats_lines = []
    sh = 0
    if lay.stats is not None:
        sh, sw = lay.stats.getmaxyx()
        stats_lines = pack_items(items, sw - 2)
    stats_fit = lay.stats is not None and len(stats_lines) <= sh - 2

    # -- header / banner ------------------------------------------------------
    if not draw_banner(scr, lay.cols, session.attention, resume):
        if game.game_over:
            state = "GAME OVER"
        elif game.paused:
            state = "paused"
        else:
            state = "cave %d: %s" % (game.level, game.cave["name"])
        head = " boulder dash - %s " % state
        if lay.plan["hint_in_header"]:
            head = " boulder dash | ? help "
        if not stats_fit:
            if game.snap_armed:
                head += "| SNAP "
            head += "| score %d | lives %d | %s | time %d " % (
                game.score, game.lives, gems_text(game), game.seconds_left())
        if lay.plan["hint_in_header"]:
            head += "| " + key_hint().replace("? help | ", "") + " "
        elif len(head) > lay.cols - 1 and state.startswith("cave"):
            head = head.replace(": %s " % game.cave["name"], " ", 1)
        safe_addstr(scr, 0, 0, head[: lay.cols - 1], curses.A_STANDOUT)

    lay.boxes()
    bw = lay.board
    draw_board(bw, game)

    # -- overlays -------------------------------------------------------------------
    mid = BOARD_H // 2 - 1
    help_open = help_overlay is not None and help_overlay.is_open
    if game.game_over:
        _center(bw, mid, " GAME OVER ", curses.A_STANDOUT | curses.A_BOLD)
        _center(bw, mid + 1, " r restart | ? help | q menu ", curses.A_STANDOUT)
    elif game.phase is not None and game.phase.kind == "cave_clear":
        _center(bw, mid, " CAVE CLEAR ", curses.A_STANDOUT | curses.A_BOLD)
        _center(bw, mid + 1, " time bonus +%d " % game.last_bonus,
                curses.A_STANDOUT)
    elif game.phase is not None and game.phase.kind == "dying":
        why = " OUT OF TIME " if game.time_left <= 0 else " OUCH! "
        _center(bw, mid, why, curses.A_STANDOUT | curses.A_BOLD)
    elif game.paused and not help_open:
        _center(bw, mid, " PAUSED ", curses.A_STANDOUT | curses.A_BOLD)
        _center(bw, mid + 1, " p resume | ? help | r restart | q menu ",
                curses.A_STANDOUT)

    # -- side column ------------------------------------------------------------------
    if lay.stats is not None:
        st = lay.stats
        safe_addstr(st, 0, 2, " %s " % TITLE.lower(), curses.A_BOLD)
        for i, line in enumerate(stats_lines[: sh - 2]):
            attr = 0
            if "SNAP" in line or "EXIT" in line:
                attr = curses.A_BOLD
            safe_addstr(st, 1 + i, 1, line, attr)
    if lay.controls is not None:
        draw_controls(lay.controls, CONTROLS)
    if lay.agents is not None:
        draw_agents(lay.agents, session.roster, session.status_pairs)

    hint = lay.plan["hint"]
    if hint is not None:
        hy, _, _, hw = hint
        text = key_hint()[: hw - 1]
        safe_addstr(scr, hy, max(0, (hw - 1 - len(text)) // 2), text,
                    curses.A_DIM)

    if help_overlay is not None:
        help_overlay.draw(scr)


KEY_DIRS = {
    curses.KEY_UP: UP, curses.KEY_DOWN: DOWN,
    curses.KEY_LEFT: LEFT, curses.KEY_RIGHT: RIGHT,
}
for _keys, _d in (("kKwW", UP), ("jJsS", DOWN), ("hHaA", LEFT), ("lLdD", RIGHT)):
    for _k in _keys:
        KEY_DIRS[ord(_k)] = _d
SNAP_KEYS = (ord(" "),)


def handle_key(game, ch) -> bool:
    """Apply a game key (pause, move, snap). Returns True if it changed
    anything. q, r, ? and the banners belong to dispatch_key."""
    if game.game_over:
        return False
    if ch in (ord("p"), ord("P")):
        game.paused = not game.paused
        return True
    if game.paused:
        return False
    if ch in KEY_DIRS:
        return game.direction_key(KEY_DIRS[ch])
    if ch in SNAP_KEYS:
        return game.toggle_snap()
    return False


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
        st.paused_by_attention = st.paused_by_resume = False
    handle_key(game, ch)
    return None


def run(session) -> None:
    """Play Boulder Dash until `q` (back to the menu) or SIGTERM. The game is
    saved on every exit path and resumed, paused, on the next run."""
    scr = session.stdscr
    init_colors()
    game_path = session.path("boulderdash-game.json")
    hiscore_path = session.path("boulderdash-hiscore.json")
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
        # pending death/clear transition is resolved first so the save is a
        # consistent, resumable state.
        game.finish_phase()
        if game_path:
            if game.game_over:
                remove_path(game_path)
            else:
                atomic_save_json(game_path, game.to_save())
        if hiscore_path and game.score > hiscore:
            save_hiscore(hiscore_path, game.score)


if __name__ == "__main__":
    import arcade

    sys.exit(arcade.main(["--game", "boulderdash"] + sys.argv[1:]))
