#!/usr/bin/env python3
"""Donkey Kong for the Herdr arcade (see arcade.py, which runs it via `run()`).

Structure: everything above the "curses UI" divider is pure game logic --
tile grid, Jumpman, jumps and falls, barrels, fireballs, hammers, rivets,
the bonus timer, scoring, stages, save/restore and the layout plan -- with no
terminal dependency, so `tests/test_donkeykong.py` imports this module
without a terminal.

Rules in brief: climb from the bottom of the stage to Pauline while Kong
throws barrels down the staggered girders (25m), or walk over every rivet
holding the floors up until Kong falls (100m). Jumping over a barrel or a
fireball scores; a hammer smashes them for a while. A bonus timer counts
down: what is left is scored when the stage is cleared, and running out
costs a life. The two stages alternate and every loop runs faster.

Geometry: the field is GRID_W x GRID_H cells, each drawn two terminal
columns wide. Jumpman, barrels and fireballs each fill one cell and stand in
the cell *above* a girder. A girder that slopes is a row of segments offset
by one row (a staircase); walking auto-steps up and down it.
"""

from __future__ import annotations

import curses
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

GRID_W = 28
GRID_H = 24

# Tiles on the live grid.
EMPTY = " "
GIRDER = "="
LADDER = "H"       # climbable ladder in the air between two girders
LTOP = "#"         # girder cell a ladder passes through (its top end)
BROKEN = "h"       # top stub of a broken ladder: scenery, not climbable
RIVET = "R"        # a girder cell held by a rivet (100m)
DRUM = "O"         # oil drum (25m)
GRID_TILES = frozenset((EMPTY, GIRDER, LADDER, LTOP, BROKEN, RIVET, DRUM))

# Markers that only appear in stage definitions (the grid holds EMPTY).
START = "M"        # Jumpman's start
KONG = "K"         # top-left cell of Kong (3 cells wide, 2 tall)
PAULINE = "P"
HAMMER = "T"
ITEM = "$"         # one of Pauline's belongings
STAGE_TILES = GRID_TILES | {START, KONG, PAULINE, HAMMER, ITEM}

GIRDERISH = frozenset((GIRDER, LTOP, RIVET))   # something to stand on
SOLID = GIRDERISH | {DRUM}                     # blocks walking and jumping
LADDERISH = frozenset((LADDER, LTOP))
KONG_W, KONG_H = 3, 2

# Scoring.
JUMP_SCORE = 100        # jumping over a barrel or a fireball
SMASH_POINTS = (300, 500, 800)
RIVET_SCORE = 100
ITEM_POINTS = (300, 500, 800)   # by loop (the last value repeats)
EXTRA_LIFE_AT = 7000

START_LIVES = 3
MAX_LIVES = 9

# Jumpman timing (seconds).
MOVE_STEP = 0.08        # shortest time between two walk/climb steps
JUMP_STEP = 0.11        # per arc step
# The jump arc: (moves horizontally?, dy) per step. The first step happens
# on the keypress, the rest one JUMP_STEP apart: up, across, across, down.
JUMP_ARC = ((1, -1), (1, 0), (1, 0), (0, 1))
RUN_WINDOW = 0.4        # space jumps the way you moved within this long
FALL_STEP = 0.06        # per cell
SAFE_FALL = 2           # falls of more rows than this are fatal

# Hazards.
BARREL_STEP = 0.15      # rolling, per cell (scaled by the loop's speed)
BARREL_DROP_STEP = 0.07  # falling or going down a ladder, per cell
FIRE_STEP = 0.30
FIRST_THROW = 1.5
THROW_MIN, THROW_MAX = 2.4, 4.0
KONG_POSE_TIME = 0.5
MAX_BARRELS = 6
MAX_FIRE_BARRELS = 4    # fireballs the oil drum can have out at once
FIRE_SPAWN = 6.0        # 100m: a new fireball this often...
FIRST_FIRE = 1.0
MAX_FIRE_RIVETS = 2     # ...up to this many (+1 per loop, at most 5)
FIRE_SPAWN_DIST = 10    # never closer to Jumpman than this (x + y cells)
LADDER_CHANCE = 0.25    # a barrel takes a ladder down...
LADDER_CHASE = 0.6      # ...more often when Jumpman is below and close

HAMMER_TIME = 9.0
HAMMER_WARN = 2.0       # the hammer blinks for its last seconds

# Bonus timer.
BONUS_START = 5000
BONUS_MAX = 8000
BONUS_DROP = 100
BONUS_TICK = 1.5        # seconds per BONUS_DROP

# Loops.
SPEEDUP = 0.12          # hazards run this much faster each loop...
SPEED_MIN = 0.5         # ...down to half their step time

DEATH_TIME = 1.5
CLEAR_TIME = 2.5
POPUP_TIME = 0.8

SAVE_VERSION = 1

# Original stage layouts, 28 x 24. 25m: four staggered girders over a flat
# floor; each slopes by one row and leaves a gap at its low end, so barrels
# zig-zag down and roll into the oil drum. 100m: five floors, eight rivets.
STAGES = [
    ("barrels", "25m", [
        "             P              ",
        "          ======#=          ",
        "K               H           ",
        "                H           ",
        "===========#==  H           ",
        "      h    H  ==========#=  ",
        "      h    H            H   ",
        "           H         T  H   ",
        "      H    H            H   ",
        "      H    H  =====#========",
        "  ==#=========     H        ",
        "    H    h         H        ",
        "    H              H        ",
        "    H    H         H        ",
        "==========#===     H        ",
        "          H   ========#===  ",
        "          H      h    H     ",
        "          H           H     ",
        "  T       H      H    H     ",
        "          H   ======#=======",
        "  ===#========      H   h   ",
        "     H              H       ",
        "   M H              H   H O ",
        "============================",
    ]),
    ("rivets", "100m", [
        "             P              ",
        "          ========          ",
        "            K               ",
        "        $                   ",
        "   ==#=R============R=#==   ",
        "     H   h            H     ",
        "     H                H     ",
        "     H       T        H     ",
        "     H   H         $  H     ",
        "  =#===R============R===#=  ",
        "   H            h       H   ",
        "   H                    H   ",
        "   H            H       H   ",
        "   H            H       H   ",
        " ===#==R============R==#=== ",
        "    H      h           H    ",
        "    H                  H    ",
        "    H      H     T     H    ",
        "    H      H$          H    ",
        "==#====R======#=====R====#==",
        "  H           H          H  ",
        "  H           H          H  ",
        "  H M         H          H  ",
        "============================",
    ]),
]


class Phase:
    """A timed interlude as an explicit logic phase: 'dying' or 'clear'.
    Its consequences apply when it completes -- via update(), or at once via
    finish_phase()."""

    __slots__ = ("kind", "elapsed", "duration")

    def __init__(self, kind, duration):
        self.kind = kind
        self.elapsed = 0.0
        self.duration = duration


class Jumpman:
    __slots__ = ("x", "y", "facing", "jump", "jump_dx", "jump_t", "jumped",
                 "falling", "fall_n", "fall_t", "cool", "run_dir", "run_t")

    def __init__(self, x, y):
        self.x = x
        self.y = y
        self.facing = 1
        self.jump = -1          # next JUMP_ARC step to apply, -1 when grounded
        self.jump_dx = 0
        self.jump_t = 0.0
        self.jumped = []        # uids of hazards already scored this jump
        self.falling = False
        self.fall_n = 0         # rows fallen so far
        self.fall_t = 0.0
        self.cool = 0.0         # until the next walk/climb step is allowed
        self.run_dir = 0        # last left/right pressed...
        self.run_t = 0.0        # ...and how much longer it counts for a jump


class Barrel:
    __slots__ = ("uid", "x", "y", "dx", "mode", "t")

    def __init__(self, uid, x, y, dx, mode="roll"):
        self.uid = uid
        self.x = x
        self.y = y
        self.dx = dx
        self.mode = mode        # 'roll', 'fall' or 'ladder'
        self.t = 0.0


class Fireball:
    __slots__ = ("uid", "x", "y", "dx", "climb", "t", "cool")

    def __init__(self, uid, x, y, dx):
        self.uid = uid
        self.x = x
        self.y = y
        self.dx = dx
        self.climb = 0          # -1 up / +1 down a ladder, 0 on a girder
        self.t = 0.0
        self.cool = 0           # steps before it may take a ladder again


BARREL_MODES = ("roll", "fall", "ladder")


def parse_stage(rows):
    """(grid, start, kong, pauline, hammers, items) from a stage's strings.
    Raises ValueError for a malformed stage."""
    if len(rows) != GRID_H:
        raise ValueError("stage height")
    grid = []
    start = kong = pauline = None
    hammers, items = [], []
    for y, row in enumerate(rows):
        if len(row) != GRID_W:
            raise ValueError("stage width")
        line = []
        for x, ch in enumerate(row):
            if ch not in STAGE_TILES:
                raise ValueError("tile %r" % ch)
            if ch == START:
                if start is not None:
                    raise ValueError("two starts")
                start = (x, y)
            elif ch == KONG:
                kong = (x, y)
            elif ch == PAULINE:
                pauline = (x, y)
            elif ch == HAMMER:
                hammers.append((x, y))
            elif ch == ITEM:
                items.append((x, y))
            line.append(ch if ch in GRID_TILES else EMPTY)
        grid.append(line)
    if start is None or kong is None:
        raise ValueError("no start or no kong")
    return grid, start, kong, pauline, hammers, items


def roll_directions(grid) -> dict:
    """{(x, y): dx} for every girder cell: the way a barrel rolls on it.
    A girder is a group of girder cells touching (diagonals included, which
    joins the offset segments of a staggered girder); barrels roll toward its
    lower end, and right on a flat one."""
    dirs = {}
    seen = set()
    for y0 in range(GRID_H):
        for x0 in range(GRID_W):
            if grid[y0][x0] not in GIRDERISH or (x0, y0) in seen:
                continue
            comp, todo = [], [(x0, y0)]
            seen.add((x0, y0))
            while todo:
                x, y = todo.pop()
                comp.append((x, y))
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        nx, ny = x + dx, y + dy
                        if (0 <= nx < GRID_W and 0 <= ny < GRID_H
                                and (nx, ny) not in seen
                                and grid[ny][nx] in GIRDERISH):
                            seen.add((nx, ny))
                            todo.append((nx, ny))
            left = min(comp)
            right = max(comp)
            d = -1 if left[1] > right[1] else 1
            for cell in comp:
                dirs[cell] = d
    return dirs


def speed_factor(loop: int) -> float:
    """Hazard step-time multiplier: every loop runs faster."""
    return max(SPEED_MIN, 1.0 - SPEEDUP * (loop - 1))


def bonus_start(loop: int) -> int:
    return min(BONUS_MAX, BONUS_START + 1000 * (loop - 1))


def item_points(loop: int) -> int:
    return ITEM_POINTS[min(loop, len(ITEM_POINTS)) - 1]


def _sign(n):
    return (n > 0) - (n < 0)


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

    def __init__(self, rng=None, stage_no: int = 1, stages=None):
        self.rng = rng if rng is not None else random.Random()
        self.stages = stages if stages is not None else STAGES
        self.score = 0
        self.lives = START_LIVES
        self.stage_no = stage_no        # stages played, 1-based
        self.extra_given = False
        self.paused = False
        self.game_over = False
        self.phase = None
        self.next_uid = 1
        self.load_stage()

    def reset(self) -> None:
        self.__init__(self.rng, 1, self.stages)

    # -- stage --------------------------------------------------------------

    @property
    def stage_index(self) -> int:
        return (self.stage_no - 1) % len(self.stages)

    @property
    def loop(self) -> int:
        return (self.stage_no - 1) // len(self.stages) + 1

    @property
    def kind(self) -> str:
        return self.stages[self.stage_index][0]

    @property
    def height_name(self) -> str:
        return self.stages[self.stage_index][1]

    @property
    def speed(self) -> float:
        return speed_factor(self.loop)

    def load_stage(self) -> None:
        """(Re)start the current stage from its definition."""
        grid, start, kong, pauline, hammers, items = parse_stage(
            self.stages[self.stage_index][2])
        self.grid = grid
        self.rolls = roll_directions(grid)
        self.kong = kong
        self.pauline = pauline
        self.drum = None
        for y in range(GRID_H):
            for x in range(GRID_W):
                if grid[y][x] == DRUM:
                    self.drum = (x, y)
        self.player = Jumpman(*start)
        self.hammers = list(hammers)
        self.items = list(items)
        self.hammer_t = 0.0
        self.barrels = []
        self.fireballs = []
        self.popups = []                # [x, y, text, seconds left]
        self.bonus = bonus_start(self.loop)
        self.bonus_t = 0.0
        self.throw_t = FIRST_THROW
        self.kong_t = 0.0               # throw pose time left
        self.spawn_t = FIRST_FIRE
        self.drum_lit = False
        self.phase = None

    # -- tile queries ---------------------------------------------------------

    def in_bounds(self, x, y) -> bool:
        return 0 <= x < GRID_W and 0 <= y < GRID_H

    def tile(self, x, y) -> str:
        """The tile at (x, y); out of bounds counts as girder (a wall)."""
        if not self.in_bounds(x, y):
            return GIRDER
        return self.grid[y][x]

    def open_cell(self, x, y) -> bool:
        return self.in_bounds(x, y) and self.tile(x, y) not in SOLID

    def on_girder(self, x, y) -> bool:
        """Standing on something: a girder (or ladder top, or rivet) below."""
        return self.tile(x, y + 1) in GIRDERISH and self.tile(x, y) != LTOP

    def kong_cells(self):
        kx, ky = self.kong
        return {(kx + i, ky + j) for i in range(KONG_W) for j in range(KONG_H)}

    def rivets_left(self) -> int:
        return sum(row.count(RIVET) for row in self.grid)

    def hazards(self):
        return self.barrels + self.fireballs

    def _walk_target(self, x, y, dx):
        """Where a step sideways from (x, y) leads: (nx, ny, falls), or None
        when blocked. Steps one row up or down the staggered girders."""
        nx = x + dx
        if not self.in_bounds(nx, y):
            return None
        here = self.tile(nx, y)
        if here in SOLID:
            if here in GIRDERISH and self.open_cell(nx, y - 1) \
                    and self.open_cell(x, y - 1):
                return (nx, y - 1, False)        # step up
            return None
        if self.tile(nx, y + 1) in GIRDERISH:
            return (nx, y, False)
        if self.open_cell(nx, y + 1) and self.tile(nx, y + 2) in GIRDERISH:
            return (nx, y + 1, False)            # step down
        return (nx, y, True)                     # off the edge

    # -- scoring --------------------------------------------------------------

    def _add(self, points: int, x=None, y=None) -> None:
        self.score += points
        if not self.extra_given and self.score >= EXTRA_LIFE_AT:
            self.extra_given = True
            self.lives = min(MAX_LIVES, self.lives + 1)
        if x is not None:
            self.popups.append([x, y, str(points), POPUP_TIME])

    # -- Jumpman ----------------------------------------------------------------

    def _can_act(self) -> bool:
        p = self.player
        return (not self.game_over and not self.paused and self.phase is None
                and p.jump < 0 and not p.falling)

    def standing(self) -> bool:
        p = self.player
        return self.on_girder(p.x, p.y)

    def move(self, dx: int) -> bool:
        """One cell left/right per keypress (while standing on a girder)."""
        p = self.player
        if not self._can_act():
            return False
        p.facing = dx
        p.run_dir, p.run_t = dx, RUN_WINDOW
        if p.cool > 0 or not self.standing():
            return False
        target = self._walk_target(p.x, p.y, dx)
        if target is None:
            return False
        ox, oy = p.x, p.y
        p.x, p.y, falls = target
        p.cool = MOVE_STEP
        self._left_cell(ox, oy)
        if falls:
            p.falling, p.fall_n, p.fall_t = True, 0, 0.0
        self._after_move()
        return True

    def climb(self, dy: int) -> bool:
        """Up (-1) / down (+1) one ladder cell. Not with a hammer."""
        p = self.player
        if not self._can_act() or self.hammer_t > 0 or p.cool > 0:
            return False
        here = self.tile(p.x, p.y)
        if dy < 0:
            if here not in LADDERISH or not self.in_bounds(p.x, p.y - 1):
                return False
            above = self.tile(p.x, p.y - 1)
            if not (above in LADDERISH
                    or (here == LTOP and above in (EMPTY, BROKEN))):
                return False
        else:
            if self.tile(p.x, p.y + 1) not in LADDERISH:
                return False
        p.y += dy
        p.cool = MOVE_STEP
        self._after_move()
        return True

    def jump(self, dx=None) -> bool:
        """Start the fixed jump arc: dx -1/1, 0 straight up, or None for the
        direction of a left/right press in the last RUN_WINDOW seconds."""
        p = self.player
        if not self._can_act() or self.hammer_t > 0 or not self.standing():
            return False
        if dx is None:
            dx = p.run_dir if p.run_t > 0 else 0
        if dx:
            p.facing = dx
        p.jump_dx = dx
        p.jump = 0
        p.jump_t = 0.0
        p.jumped = []
        self._jump_step()
        return True

    def _jump_step(self) -> None:
        p = self.player
        across, dy = JUMP_ARC[p.jump]
        dx = p.jump_dx if across else 0
        p.jump += 1
        ox, oy = p.x, p.y
        for cx, cy in ((p.x + dx, p.y + dy), (p.x, p.y + dy), (p.x + dx, p.y)):
            if (cx, cy) != (ox, oy) and self.open_cell(cx, cy):
                p.x, p.y = cx, cy
                break
        if p.jump >= len(JUMP_ARC):
            p.jump = -1
            if not self.on_girder(p.x, p.y):
                p.falling, p.fall_n, p.fall_t = True, 0, 0.0
        if (p.x, p.y) != (ox, oy):
            self._left_cell(ox, oy)
        self._after_move()

    def _left_cell(self, ox, oy) -> None:
        """Walking (or jumping) off the cell above a rivet pulls it out."""
        p = self.player
        if p.x != ox and self.tile(ox, oy + 1) == RIVET:
            self.grid[oy + 1][ox] = EMPTY
            self._add(RIVET_SCORE, ox, oy)
            if self.kind == "rivets" and self.rivets_left() == 0:
                self._clear()

    def _after_move(self) -> None:
        if self.phase is not None:
            return
        p = self.player
        pos = (p.x, p.y)
        if pos in self.hammers and self.hammer_t <= 0:
            self.hammers.remove(pos)
            self.hammer_t = HAMMER_TIME
        if pos in self.items:
            self.items.remove(pos)
            self._add(item_points(self.loop), p.x, p.y)
        if self.kind == "barrels" and p.y == 0:
            self._clear()
            return
        self._check_hits()

    def _clear(self) -> None:
        p = self.player
        p.jump, p.falling = -1, False
        self._add(self.bonus, p.x, max(0, p.y - 1))
        self.phase = Phase("clear", CLEAR_TIME)

    def _die(self) -> None:
        p = self.player
        p.jump, p.falling = -1, False
        self.phase = Phase("dying", DEATH_TIME)

    def _check_hits(self) -> bool:
        """Smash, score jumps and resolve collisions. True if Jumpman died."""
        if self.phase is not None:
            return False
        p = self.player
        if self.hammer_t > 0:
            reach = ((p.x, p.y), (p.x + p.facing, p.y))
            for h in self.hazards():
                if (h.x, h.y) in reach:
                    self._smash(h)
        for h in self.hazards():
            if (h.x, h.y) == (p.x, p.y):
                self._die()
                return True
        if (p.x, p.y) in self.kong_cells():
            self._die()
            return True
        if p.jump >= 0:
            for h in self.hazards():
                if h.x == p.x and 1 <= h.y - p.y <= 2 \
                        and h.uid not in p.jumped:
                    p.jumped.append(h.uid)
                    self._add(JUMP_SCORE, h.x, h.y - 1)
        return False

    def _smash(self, h) -> None:
        if h in self.barrels:
            self.barrels.remove(h)
        else:
            self.fireballs.remove(h)
        self._add(self.rng.choice(SMASH_POINTS), h.x, h.y)

    # -- hazards ----------------------------------------------------------------

    def _uid(self) -> int:
        uid = self.next_uid
        self.next_uid += 1
        return uid

    def _hazard_at(self, x, y):
        for h in self.hazards():
            if (h.x, h.y) == (x, y):
                return h
        return None

    def _throw(self) -> None:
        kx, ky = self.kong
        x, y = kx + KONG_W, ky + KONG_H - 1
        self.kong_t = KONG_POSE_TIME
        if len(self.barrels) < MAX_BARRELS and self._hazard_at(x, y) is None:
            dx = self.rolls.get((x, y + 1), 1)
            self.barrels.append(Barrel(self._uid(), x, y, dx))

    def _spawn_fire(self, x, y, dx) -> None:
        self.fireballs.append(Fireball(self._uid(), x, y, dx))

    def _burn(self, b) -> None:
        """A barrel rolled into the oil drum: it flares up and may let a
        fireball out."""
        self.barrels.remove(b)
        self.drum_lit = True
        if len(self.fireballs) >= MAX_FIRE_BARRELS:
            return
        dx_, dy_ = self.drum
        for side in (-1, 1):
            if self.open_cell(dx_ + side, dy_) and self.on_girder(dx_ + side, dy_):
                self._spawn_fire(dx_ + side, dy_, side)
                return

    def _land(self, b) -> None:
        b.mode = "roll"
        b.dx = self.rolls.get((b.x, b.y + 1), b.dx)

    def _barrel_step(self, b) -> None:
        if b.mode == "roll":
            nx = b.x + b.dx
            if self.drum is not None and (nx, b.y) == self.drum:
                self._burn(b)
                return
            target = self._walk_target(b.x, b.y, b.dx)
            if target is None:
                b.dx = -b.dx
                return
            b.x, b.y, falls = target
            if falls:
                b.mode = "fall"
            elif self.tile(b.x, b.y + 1) == LTOP:
                p = self.player
                chance = LADDER_CHANCE
                if p.y > b.y and abs(p.x - b.x) <= 4:
                    chance = LADDER_CHASE
                if self.rng.random() < chance:
                    b.mode = "ladder"
            return
        below = self.tile(b.x, b.y + 1)
        if self.drum is not None and (b.x, b.y + 1) == self.drum:
            self._burn(b)
            return
        if b.mode == "ladder":
            if below in LADDERISH:
                b.y += 1
                if self.tile(b.x, b.y + 1) not in LADDERISH:
                    if self.tile(b.x, b.y + 1) in GIRDERISH:
                        self._land(b)
                    else:
                        b.mode = "fall"
                return
            b.mode = "fall"
        # falling
        if below in GIRDERISH:
            self._land(b)
            return
        if b.y + 1 >= GRID_H:
            self.barrels.remove(b)
            return
        b.y += 1
        if self.tile(b.x, b.y + 1) in GIRDERISH:
            self._land(b)

    def _fire_blocked(self, x, y) -> bool:
        return (x, y) in self.kong_cells() or (
            self.drum is not None and (x, y) == self.drum)

    def _full_ladder_up(self, x, y) -> bool:
        """Does a whole ladder rise from (x, y) to a girder a fireball may
        use? Not a broken stub, and never up to Pauline above Kong."""
        if self.tile(x, y) != LADDER:
            return False
        while self.tile(x, y - 1) == LADDER:
            y -= 1
        return self.tile(x, y - 1) == LTOP and y - 1 >= self.kong[1]

    def _fire_step(self, f) -> None:
        p = self.player
        if f.climb:
            here = self.tile(f.x, f.y)
            ny = f.y + f.climb
            if f.climb < 0:
                above = self.tile(f.x, ny)
                if here == LTOP and above in (EMPTY, BROKEN) and ny >= 0:
                    f.y, f.climb, f.cool = ny, 0, 3
                elif here in LADDERISH and above in LADDERISH:
                    f.y = ny
                else:
                    f.climb = 0
            else:
                if self.tile(f.x, ny) in LADDERISH:
                    f.y = ny
                    if self.on_girder(f.x, f.y):
                        f.climb, f.cool = 0, 3
                else:
                    f.climb = 0
            return
        if f.cool > 0:
            f.cool -= 1
        else:
            up_ok = self._full_ladder_up(f.x, f.y)
            down_ok = self.tile(f.x, f.y + 1) == LTOP
            roll = self.rng.random()
            if up_ok and (roll < 0.15 or (p.y < f.y and roll < 0.6)):
                f.climb = -1
                f.y -= 1
                return
            if down_ok and (roll < 0.15 or (p.y > f.y and roll < 0.6)):
                f.climb = 1
                f.y += 1
                return
        roll = self.rng.random()
        if abs(p.y - f.y) <= 1 and p.x != f.x and roll < 0.5:
            f.dx = _sign(p.x - f.x)
        elif roll > 0.92:
            f.dx = -f.dx
        for dx in (f.dx, -f.dx):
            target = self._walk_target(f.x, f.y, dx)
            if target is not None and not target[2] \
                    and not self._fire_blocked(target[0], target[1]):
                f.x, f.y, _ = target
                f.dx = dx
                return

    def _spawn_rivet_fire(self) -> None:
        cap = min(5, MAX_FIRE_RIVETS + self.loop - 1)
        if len(self.fireballs) >= cap:
            return
        p = self.player
        kong = self.kong_cells()
        # Below Kong's row: never on Pauline's girder, out of reach.
        spots = [(x, y) for y in range(self.kong[1], GRID_H - 1)
                 for x in range(GRID_W)
                 if self.tile(x, y) == EMPTY and self.on_girder(x, y)
                 and abs(x - p.x) + abs(y - p.y) >= FIRE_SPAWN_DIST
                 and (x, y) not in kong and self._hazard_at(x, y) is None]
        if spots:
            x, y = self.rng.choice(spots)
            self._spawn_fire(x, y, self.rng.choice((-1, 1)))

    # -- time -------------------------------------------------------------------

    def finish_phase(self) -> None:
        """Resolve a pending phase immediately (pause, quit, save)."""
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
                self.load_stage()
        elif phase.kind == "clear":
            self.stage_no += 1
            self.load_stage()

    def update(self, dt: float) -> bool:
        """Advance the game by `dt` seconds. True if anything changed."""
        if self.paused or self.game_over:
            return False
        for pop in self.popups:
            pop[3] -= dt
        self.popups = [pop for pop in self.popups if pop[3] > 0]
        if self.phase is not None:
            self.phase.elapsed += dt
            if self.phase.elapsed >= self.phase.duration:
                self.finish_phase()
            return True
        p = self.player
        p.cool = max(0.0, p.cool - dt)
        p.run_t = max(0.0, p.run_t - dt)
        self.kong_t = max(0.0, self.kong_t - dt)
        if self.hammer_t > 0:
            self.hammer_t = max(0.0, self.hammer_t - dt)

        # Bonus timer: running out costs a life.
        self.bonus_t += dt
        while self.bonus_t >= BONUS_TICK:
            self.bonus_t -= BONUS_TICK
            self.bonus = max(0, self.bonus - BONUS_DROP)
            if self.bonus == 0:
                self._die()
                return True

        # Jumpman: the jump arc, then gravity.
        if p.jump >= 0:
            p.jump_t += dt
            while p.jump >= 0 and p.jump_t >= JUMP_STEP:
                p.jump_t -= JUMP_STEP
                self._jump_step()
                if self.phase is not None:
                    return True
        elif not p.falling and not self.on_girder(p.x, p.y) \
                and self.tile(p.x, p.y) not in LADDERISH:
            p.falling, p.fall_n, p.fall_t = True, 0, 0.0
        if p.falling:
            p.fall_t += dt
            while p.falling and p.fall_t >= FALL_STEP:
                p.fall_t -= FALL_STEP
                if self.on_girder(p.x, p.y) or \
                        self.tile(p.x, p.y + 1) in GIRDERISH:
                    p.falling = False
                    if p.fall_n > SAFE_FALL:
                        self._die()
                        return True
                    break
                if p.y + 1 >= GRID_H or not self.open_cell(p.x, p.y + 1):
                    p.falling = False
                    self._die()
                    return True
                p.y += 1
                p.fall_n += 1
                self._after_move()
                if self.phase is not None:
                    return True

        speed = self.speed
        # Kong throws barrels (25m); fireballs appear (100m).
        if self.kind == "barrels":
            self.throw_t -= dt
            if self.throw_t <= 0:
                self._throw()
                self.throw_t = self.rng.uniform(THROW_MIN, THROW_MAX) * speed
        else:
            self.spawn_t -= dt
            if self.spawn_t <= 0:
                self._spawn_rivet_fire()
                self.spawn_t = FIRE_SPAWN * speed

        roll_step = BARREL_STEP * speed
        drop_step = BARREL_DROP_STEP * speed
        for b in list(self.barrels):
            b.t += dt
            while b in self.barrels:
                step = roll_step if b.mode == "roll" else drop_step
                if b.t < step:
                    break
                b.t -= step
                self._barrel_step(b)
                self._check_hits()
                if self.phase is not None:
                    return True
        fire_step = FIRE_STEP * speed
        for f in list(self.fireballs):
            f.t += dt
            while f in self.fireballs and f.t >= fire_step:
                f.t -= fire_step
                self._fire_step(f)
                self._check_hits()
                if self.phase is not None:
                    return True
        self._check_hits()
        return True

    # -- save / restore -----------------------------------------------------------

    def to_save(self) -> dict:
        """Serializable snapshot. The caller resolves any phase first
        (finish_phase), so a save never holds a death/clear animation."""
        p = self.player
        return {
            "version": SAVE_VERSION,
            "stage_no": self.stage_no,
            "score": self.score,
            "lives": self.lives,
            "extra_given": self.extra_given,
            "grid": ["".join(row) for row in self.grid],
            "hammers": [list(h) for h in self.hammers],
            "items": [list(i) for i in self.items],
            "hammer_t": self.hammer_t,
            "bonus": self.bonus,
            "bonus_t": self.bonus_t,
            "throw_t": self.throw_t,
            "kong_t": self.kong_t,
            "spawn_t": self.spawn_t,
            "drum_lit": self.drum_lit,
            "next_uid": self.next_uid,
            "player": [p.x, p.y, p.facing, p.jump, p.jump_dx, p.jump_t,
                       list(p.jumped), p.falling, p.fall_n, p.fall_t,
                       p.cool, p.run_dir, p.run_t],
            "barrels": [[b.uid, b.x, b.y, b.dx, b.mode, b.t]
                        for b in self.barrels],
            "fireballs": [[f.uid, f.x, f.y, f.dx, f.climb, f.t, f.cool]
                          for f in self.fireballs],
            "popups": [list(pop) for pop in self.popups],
            "rng": _rng_to_json(self.rng),
        }

    @classmethod
    def from_save(cls, data, rng=None):
        """Rebuild a saved game, or None for anything corrupt/incompatible."""
        try:
            if not isinstance(data, dict) or data.get("version") != SAVE_VERSION:
                return None
            stage_no = data["stage_no"]
            if not isinstance(stage_no, int) or stage_no < 1:
                return None
            game = cls(rng, stage_no)
            base = game.grid
            grid = data["grid"]
            if not isinstance(grid, list) or len(grid) != GRID_H:
                return None
            for y, row in enumerate(grid):
                if not isinstance(row, str) or len(row) != GRID_W:
                    return None
                for x, ch in enumerate(row):
                    # Only rivets can have changed since the stage began.
                    if ch != base[y][x] and not (
                            base[y][x] == RIVET and ch == EMPTY):
                        return None
            game.grid = [list(row) for row in grid]

            def cell(v, open_=True):
                x, y = v
                if not (isinstance(x, int) and isinstance(y, int)
                        and game.in_bounds(x, y)):
                    raise ValueError("cell")
                if open_ and game.tile(x, y) in (GIRDER, RIVET, DRUM):
                    raise ValueError("cell")  # ladder tops are fine
                return x, y

            def num(v):
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    raise ValueError("number")
                return float(v)

            def direction(v, zero=False):
                if v not in ((-1, 0, 1) if zero else (-1, 1)) \
                        or isinstance(v, bool):
                    raise ValueError("direction")
                return v

            game.hammers = [cell(h) for h in data["hammers"]]
            game.items = [cell(i) for i in data["items"]]
            game.hammer_t = num(data["hammer_t"])
            bonus = data["bonus"]
            if not isinstance(bonus, int) or bonus < 0:
                return None
            game.bonus = bonus
            game.bonus_t = num(data["bonus_t"])
            game.throw_t = num(data["throw_t"])
            game.kong_t = num(data["kong_t"])
            game.spawn_t = num(data["spawn_t"])
            game.drum_lit = bool(data["drum_lit"])
            next_uid = data["next_uid"]
            if not isinstance(next_uid, int) or next_uid < 1:
                return None
            game.next_uid = next_uid

            (px, py, facing, jump, jump_dx, jump_t, jumped, falling, fall_n,
             fall_t, cool, run_dir, run_t) = data["player"]
            p = Jumpman(*cell((px, py)))
            p.facing = direction(facing)
            if not isinstance(jump, int) or not -1 <= jump < len(JUMP_ARC) \
                    or jump == 0:
                return None
            p.jump = jump
            p.jump_dx = direction(jump_dx, zero=True)
            p.jump_t = num(jump_t)
            if not isinstance(jumped, list) or \
                    not all(isinstance(u, int) for u in jumped):
                return None
            p.jumped = list(jumped)
            p.falling = bool(falling)
            if not isinstance(fall_n, int) or fall_n < 0:
                return None
            p.fall_n = fall_n
            p.fall_t = num(fall_t)
            p.cool = num(cool)
            p.run_dir = direction(run_dir, zero=True)
            p.run_t = num(run_t)
            game.player = p

            barrels = []
            for uid, x, y, dx, mode, t in data["barrels"]:
                if not isinstance(uid, int) or mode not in BARREL_MODES:
                    return None
                bx, by = cell((x, y))
                b = Barrel(uid, bx, by, direction(dx), mode)
                b.t = num(t)
                barrels.append(b)
            game.barrels = barrels
            fireballs = []
            for uid, x, y, dx, climb, t, fcool in data["fireballs"]:
                if not isinstance(uid, int) or not isinstance(fcool, int):
                    return None
                fx, fy = cell((x, y))
                f = Fireball(uid, fx, fy, direction(dx))
                f.climb = direction(climb, zero=True)
                f.t = num(t)
                f.cool = fcool
                fireballs.append(f)
            game.fireballs = fireballs
            popups = []
            for x, y, text, t in data["popups"]:
                cell((x, y), open_=False)
                if not isinstance(text, str):
                    return None
                popups.append([x, y, text, num(t)])
            game.popups = popups

            score, lives = data["score"], data["lives"]
            if not (isinstance(score, int) and isinstance(lives, int)
                    and lives >= 1):
                return None
            game.score, game.lives = score, lives
            game.extra_given = bool(data["extra_given"])
            state = _rng_from_json(data.get("rng"))
            if state is None:
                return None
            game.rng.setstate(state)
            return game
        except (KeyError, TypeError, ValueError, IndexError):
            return None


# --------------------------------------------------------------------------
# Controls, help and layout (pure: no terminal needed)
# --------------------------------------------------------------------------

TITLE = "Donkey Kong"

# Every key the game reacts to (see handle_key and run()).
CONTROLS = [
    ("arrows", "walk / climb"),
    ("wasd hjkl", "as arrows"),
    ("space", "jump (last dir)"),
    ("z or ,", "jump left"),
    ("x or .", "jump right"),
    ("?", "help"),
    ("p / r", "pause / restart"),
    ("q", "menu"),
]

HELP = [
    ("Goal",
     "Kong has carried Pauline to the top of a building site. Climb up to "
     "her past everything he throws at you. The 25m stage (barrels) is "
     "cleared by reaching Pauline's girder at the very top; the 100m stage "
     "(rivets) by pulling out all eight rivets, which brings Kong down."),
    ("Controls",
     "- left / right (a d, h l): walk one cell per keypress; hold the key "
     "to keep walking. Sloped girders are steps: you walk up and down them "
     "by yourself.\n"
     "- up / down (w s, k j): climb a ladder. You can only leave a ladder "
     "at its top or bottom.\n"
     "- space: jump. If you pressed left or right just before, you jump "
     "that way; otherwise straight up.\n"
     "- z or , : jump left.   x or . : jump right.\n"
     "- ? this help (the game pauses), p pause, r restart, q back to the "
     "menu (the game is saved and resumes paused)."),
    ("Jumping and falling",
     "- A jump is a fixed arc: up one row, three cells across (or straight "
     "up and down), and down again. There is no steering in the air, and "
     "keys pressed mid-jump are ignored.\n"
     "- Jump when a barrel or fireball is right next to you to clear it "
     "(+100).\n"
     "- A fall of more than %d rows is fatal: walking or jumping off the "
     "end of a girder kills you, one step down a sloped girder does not."
     % SAFE_FALL),
    ("Ladders",
     "Some ladders are broken: a stub at the bottom and a stub hanging from "
     "the girder above, with a gap between. They cannot be climbed all the "
     "way up (or down)."),
    ("Barrels (25m)",
     "Kong throws barrels that roll down the sloped girders, drop off the "
     "low end onto the girder below and sometimes roll down a ladder "
     "instead - more often when you are right below. At the bottom they "
     "roll into the oil drum, which flares up and lets out a fireball."),
    ("Rivets (100m)",
     "Walk over a rivet (the [] set in the floors) to pull it out: it goes "
     "when you step off it, leaving a gap in the floor. Do not fall into a "
     "gap; jump across it. Fireballs keep appearing and roam the floors and "
     "ladders, but never cross a gap."),
    ("Hammers",
     "Jump up into a hammer (T) to grab it. For %d seconds you smash every "
     "barrel and fireball you walk into or that comes at you from the front "
     "or lands on you, but you can neither climb nor jump. It blinks just "
     "before it runs out." % HAMMER_TIME),
    ("Bonus timer",
     "Each stage starts with a bonus of %d (1000 more each loop, up to %d) "
     "that drops by %d every %.1f seconds. What is left when you clear the "
     "stage is added to your score; if it reaches 0 you lose a life."
     % (BONUS_START, BONUS_MAX, BONUS_DROP, BONUS_TICK)),
    ("Lives and loops",
     "You start with %d lives and get one extra at %d points. Touching a "
     "barrel, a fireball or Kong costs a life and restarts the stage. The "
     "two stages alternate; every loop makes barrels, fireballs and Kong's "
     "throws faster." % (START_LIVES, EXTRA_LIFE_AT)),
    ("Scoring",
     "- jumping over a barrel or fireball: %d\n"
     "- smashing one with the hammer: 300, 500 or 800\n"
     "- pulling a rivet: %d\n"
     "- Pauline's things ($$): 300, 500 then 800 from the third loop\n"
     "- clearing a stage: the remaining bonus\n"
     "Your best score is kept between games." % (JUMP_SCORE, RIVET_SCORE)),
    ("Legend",
     "- @> <@  Jumpman (facing right / left), -T T- his hammer\n"
     "- {(oo)}  Kong;  P  Pauline\n"
     "- ==  girder;  H  ladder;  H=  ladder top in a girder\n"
     "- ()  barrel;  **  fireball;  UU  oil drum (^^ burning)\n"
     "- T   hammer;  $$  Pauline's things;  []  rivet"),
    ("Tips",
     "- Stand still and jump straight up as a barrel reaches you.\n"
     "- Barrels like to drop down ladders right on top of you: do not "
     "linger at the foot of one.\n"
     "- A hammer clears a path, but plan where you are when it runs out.\n"
     "- On 100m, pull the rivets on a floor from the outside in, so you are "
     "never stranded between two gaps without a ladder.\n"
     "- p pauses at any time; the timers stop while paused or in help."),
]

# The play window is 28 cells x 2 columns + 2 border = 58 wide and
# 24 + 2 = 26 rows tall. At the 93x28 popup the composition is board +
# 1 gap + a 30-wide side column (stats, keys, agents), centred.
BOARD_W = GRID_W * 2 + 2           # 58
BOARD_H = GRID_H + 2               # 26
STATS_H = 6                        # frame + 4 lines at the full side width
BOX_MIN_H = 3                      # frame + 1 line
AGENTS_MIN_H = 4                   # frame + an agent + "+N more"
AGENTS_MIN_W = 26                  # narrower side columns drop the agents
MIN_COLS = BOARD_W                 # 58
MIN_ROWS = BOARD_H + 1             # 27: header + board


def layout_plan(cols: int, rows: int) -> dict:
    """Pure layout decision: tier plus each region's (y, x, height, width).
    Tiers: 'full' (board + side column: stats, controls, agents), 'side'
    (side column without agents: it is too narrow for them), 'board' (no
    side column: stats go in the header and a key hint row sits at the
    bottom -- or in the header when there is no spare row), 'too_small'."""
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
            # Centre the board between the header and the hint row.
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
    agents_h = left if left >= AGENTS_MIN_H and side_w >= AGENTS_MIN_W else 0
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
    first = [c for c in CONTROLS if c[0] == "?"]
    return controls_hint(first + [c for c in CONTROLS if c[0] != "?"])


def stats_items(game, best: int) -> list:
    items = ["score %d" % game.score, "best %d" % max(best, game.score),
             "lives %d" % game.lives, "bonus %d" % game.bonus,
             "%s %s" % (game.height_name, game.kind), "loop %d" % game.loop]
    if game.kind == "rivets":
        items.append("rivets %d" % game.rivets_left())
    if game.hammer_t > 0:
        items.append("hammer %ds" % (int(game.hammer_t) + 1))
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


def help_key(game, help_overlay, ch, paused_by_help: bool):
    """The `?` contract, before any other key handling. Returns (consumed,
    paused_by_help). While help is open it takes every key (except resize);
    closing it resumes the game if the help had paused it."""
    if help_overlay.is_open:
        if ch == curses.KEY_RESIZE:
            return False, paused_by_help
        help_overlay.handle_key(ch)
        if not help_overlay.is_open and paused_by_help:
            game.paused = False
            paused_by_help = False
        return True, paused_by_help
    if ch == HELP_KEY:
        help_overlay.open()
        if not game.paused:
            game.paused = True
            paused_by_help = True
        return True, paused_by_help
    return False, paused_by_help


# --------------------------------------------------------------------------
# curses UI
# --------------------------------------------------------------------------

# Colour pairs 40-59 are shared by the later games (see common.py).
PAIR_GIRDER, PAIR_LADDER, PAIR_PLAYER, PAIR_BARREL = 40, 41, 42, 43
PAIR_FIRE, PAIR_KONG, PAIR_PAULINE, PAIR_HAMMER = 44, 45, 46, 47
PAIR_RIVET, PAIR_DRUM = 48, 49

GLYPHS = {
    GIRDER: ("==", PAIR_GIRDER),
    LADDER: ("H ", PAIR_LADDER),
    BROKEN: ("H ", PAIR_LADDER),
    RIVET: ("[]", PAIR_RIVET),
    DRUM: ("UU", PAIR_DRUM),
}
PLAYER_GLYPHS = {1: "@>", -1: "<@"}
HAMMER_GLYPHS = {1: "-T", -1: "T-"}
DEAD_GLYPH = "XX"
BARREL_GLYPH = "()"
FIRE_GLYPH = "**"
FLAME_GLYPH = "^^"
HAMMER_ITEM_GLYPH = "T "
ITEM_GLYPH = "$$"
PAULINE_GLYPH = "P "
KONG_SPRITES = {
    "idle": ("{(oo)}", "/|__|\\"),
    "throw": ("\\(OO)/", " |__| "),
    "fall": ("\\|__|/", "{(xx)}"),
}


def init_colors() -> None:
    bg = default_bg()
    curses.init_pair(PAIR_GIRDER, curses.COLOR_RED, bg)
    curses.init_pair(PAIR_LADDER, curses.COLOR_CYAN, bg)
    curses.init_pair(PAIR_PLAYER, curses.COLOR_GREEN, bg)
    curses.init_pair(PAIR_BARREL, curses.COLOR_YELLOW, bg)
    curses.init_pair(PAIR_FIRE, curses.COLOR_RED, bg)
    curses.init_pair(PAIR_KONG, curses.COLOR_YELLOW, bg)
    curses.init_pair(PAIR_PAULINE, curses.COLOR_MAGENTA, bg)
    curses.init_pair(PAIR_HAMMER, curses.COLOR_WHITE, bg)
    curses.init_pair(PAIR_RIVET, curses.COLOR_YELLOW, bg)
    curses.init_pair(PAIR_DRUM, curses.COLOR_CYAN, bg)


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


def _cell(win, x, y, text, attr=0):
    """Draw a 2-column cell glyph at grid (x, y), if on the field."""
    if 0 <= x < GRID_W and 0 <= y < GRID_H:
        safe_addstr(win, 1 + y, 1 + x * 2, text, attr)


def _sprite(win, x, y, lines, attr):
    """A multi-cell sprite, drawn as 2-column cells from grid (x, y)."""
    for j, line in enumerate(lines):
        for i in range(0, len(line), 2):
            _cell(win, x + i // 2, y + j, line[i:i + 2], attr)


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
            state = "%s %s - loop %d" % (game.height_name, game.kind,
                                         game.loop)
        head = " donkey kong - %s " % state
        if lay.plan["hint_in_header"]:
            head = " donkey kong | ? help "
        if not stats_fit:
            head += "| score %d | lives %d | bonus %d " % (
                game.score, game.lives, game.bonus)
        if lay.plan["hint_in_header"]:
            head += "| " + key_hint().replace("? help | ", "") + " "
        safe_addstr(scr, 0, 0, head[: lay.cols - 1], curses.A_STANDOUT)

    lay.boxes()
    bw = lay.board
    phase = game.phase
    dying = phase is not None and phase.kind == "dying"
    clearing = phase is not None and phase.kind == "clear"

    # -- tiles ------------------------------------------------------------------
    for y in range(GRID_H):
        for x in range(GRID_W):
            t = game.grid[y][x]
            if t == LTOP:
                safe_addstr(bw, 1 + y, 1 + x * 2, "H",
                            curses.color_pair(PAIR_LADDER) | curses.A_BOLD)
                safe_addstr(bw, 1 + y, 2 + x * 2, "=",
                            curses.color_pair(PAIR_GIRDER) | curses.A_BOLD)
                continue
            glyph = GLYPHS.get(t)
            if glyph is not None:
                attr = curses.color_pair(glyph[1])
                if t in (GIRDER, RIVET):
                    attr |= curses.A_BOLD
                _cell(bw, x, y, glyph[0], attr)
    if game.drum is not None and game.drum_lit:
        dx_, dy_ = game.drum
        _cell(bw, dx_, dy_ - 1, FLAME_GLYPH,
              curses.color_pair(PAIR_FIRE) | curses.A_BOLD)
    for hx, hy in game.hammers:
        _cell(bw, hx, hy, HAMMER_ITEM_GLYPH,
              curses.color_pair(PAIR_HAMMER) | curses.A_BOLD)
    for ix, iy in game.items:
        _cell(bw, ix, iy, ITEM_GLYPH,
              curses.color_pair(PAIR_PAULINE) | curses.A_BOLD)

    # -- Pauline and Kong ---------------------------------------------------------
    if game.pauline is not None:
        px_, py_ = game.pauline
        pair = curses.color_pair(PAIR_PAULINE) | curses.A_BOLD
        _cell(bw, px_, py_, PAULINE_GLYPH, pair)
        if clearing:
            _cell(bw, px_ + 1, py_, "<3", pair)
        elif not dying and int(time.monotonic() * 2) % 2 == 0:
            _sprite(bw, px_ + 1, py_, ["HELP! "], pair)
    kx, ky = game.kong
    pose = "throw" if game.kong_t > 0 else "idle"
    if clearing and game.kind == "rivets":
        pose = "fall"
        drop = int(phase.elapsed / phase.duration * (GRID_H - KONG_H - ky - 1))
        ky = min(GRID_H - KONG_H - 1, ky + drop)
    _sprite(bw, kx, ky, KONG_SPRITES[pose],
            curses.color_pair(PAIR_KONG) | curses.A_BOLD)

    # -- hazards and Jumpman ------------------------------------------------------
    for b in game.barrels:
        _cell(bw, b.x, b.y, BARREL_GLYPH, curses.color_pair(PAIR_BARREL))
    for f in game.fireballs:
        _cell(bw, f.x, f.y, FIRE_GLYPH,
              curses.color_pair(PAIR_FIRE) | curses.A_BOLD)
    p = game.player
    pl_attr = curses.color_pair(PAIR_PLAYER) | curses.A_BOLD
    if dying:
        if int(phase.elapsed * 10) % 2 == 0:
            _cell(bw, p.x, p.y, DEAD_GLYPH, pl_attr)
    else:
        _cell(bw, p.x, p.y, PLAYER_GLYPHS[p.facing], pl_attr)
        if game.hammer_t > 0 and (game.hammer_t > HAMMER_WARN
                                  or int(game.hammer_t * 6) % 2 == 0):
            hx = p.x + p.facing
            if game.open_cell(hx, p.y):
                _cell(bw, hx, p.y, HAMMER_GLYPHS[p.facing],
                      curses.color_pair(PAIR_HAMMER) | curses.A_BOLD)
    for x, y, text, _ in game.popups:
        cx = min(max(1, 1 + x * 2), BOARD_W - 1 - len(text))
        safe_addstr(bw, 1 + y, cx, text, curses.A_BOLD)

    # -- overlays -------------------------------------------------------------------
    mid = BOARD_H // 2 - 1
    help_open = help_overlay is not None and help_overlay.is_open
    if game.game_over:
        _center(bw, mid, " GAME OVER ", curses.A_STANDOUT | curses.A_BOLD)
        _center(bw, mid + 1, " r restart | ? help | q menu ", curses.A_STANDOUT)
    elif clearing:
        _center(bw, mid, " STAGE CLEAR ", curses.A_STANDOUT | curses.A_BOLD)
    elif game.paused and not help_open:
        _center(bw, mid, " PAUSED ", curses.A_STANDOUT | curses.A_BOLD)
        _center(bw, mid + 1, " p resume | ? help | r restart | q menu ",
                curses.A_STANDOUT)

    # -- side column ------------------------------------------------------------------
    if lay.stats is not None:
        sw_ = lay.stats
        safe_addstr(sw_, 0, 2, " %s " % TITLE.lower(), curses.A_BOLD)
        for i, line in enumerate(stats_lines[: sh - 2]):
            safe_addstr(sw_, 1 + i, 1, line)
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


LEFT_KEYS = (curses.KEY_LEFT, ord("h"), ord("a"), ord("A"), ord("H"))
RIGHT_KEYS = (curses.KEY_RIGHT, ord("l"), ord("d"), ord("D"), ord("L"))
UP_KEYS = (curses.KEY_UP, ord("k"), ord("w"), ord("W"), ord("K"))
DOWN_KEYS = (curses.KEY_DOWN, ord("j"), ord("s"), ord("S"), ord("J"))
JUMP_KEYS = (ord(" "),)
JUMP_LEFT_KEYS = (ord("z"), ord("Z"), ord(","))
JUMP_RIGHT_KEYS = (ord("x"), ord("X"), ord("."))


def handle_key(game, ch) -> bool:
    """Apply a key to the game. Returns True if a redraw is needed."""
    if game.game_over:
        return False
    if ch in (ord("p"), ord("P")):
        game.paused = not game.paused
        return True
    if game.paused:
        return False
    if ch in LEFT_KEYS:
        return game.move(-1)
    if ch in RIGHT_KEYS:
        return game.move(1)
    if ch in UP_KEYS:
        return game.climb(-1)
    if ch in DOWN_KEYS:
        return game.climb(1)
    if ch in JUMP_KEYS:
        return game.jump()
    if ch in JUMP_LEFT_KEYS:
        return game.jump(-1)
    if ch in JUMP_RIGHT_KEYS:
        return game.jump(1)
    return False


def run(session) -> None:
    """Play Donkey Kong until `q` (back to the menu) or SIGTERM. The game is
    saved on every exit path and resumed, paused, on the next run."""
    scr = session.stdscr
    init_colors()
    game_path = session.path("donkeykong-game.json")
    hiscore_path = session.path("donkeykong-hiscore.json")
    hiscore = load_hiscore(hiscore_path)

    game = Game()
    resume = False
    paused_by_resume = False
    if game_path:
        restored = Game.from_save(load_json_file(game_path))
        if restored is not None and not restored.game_over:
            game = restored
            game.paused = True
            resume = paused_by_resume = True

    lay = Layout(scr)
    help_overlay = HelpOverlay(TITLE, HELP)
    paused_by_attention = False
    paused_by_help = False
    game_over_handled = game.game_over
    dirty = True
    last = time.monotonic()
    blink = 0

    try:
        while not session.quitting.is_set():
            now = time.monotonic()
            dt = min(now - last, 0.25)  # a stalled terminal must not warp time
            last = now

            changed, alerts = session.pump()
            if alerts and session.should_auto_pause and not game.game_over:
                if not game.paused:
                    game.paused = True
                    paused_by_attention = True
            dirty = dirty or changed

            ch = scr.getch()
            if ch != -1:
                was_help = help_overlay.is_open
                consumed, paused_by_help = help_key(
                    game, help_overlay, ch, paused_by_help)
                if consumed:
                    if (was_help and not help_overlay.is_open
                            and session.attention is not None
                            and game.paused is False):
                        # An alert arrived while reading: keep the game
                        # paused until the banner is dismissed.
                        game.paused = True
                        paused_by_attention = True
                    dirty = True
                elif ch in (ord("q"), ord("Q")):
                    break
                elif ch == curses.KEY_RESIZE:
                    curses.update_lines_cols()
                    scr.erase()
                    lay = Layout(scr)
                    dirty = True
                elif session.attention is not None or resume:
                    session.dismiss()
                    resume = False
                    if paused_by_attention:
                        game.paused = False
                        paused_by_attention = False
                    if paused_by_resume:
                        game.paused = False
                        paused_by_resume = False
                    dirty = True
                elif ch in (ord("r"), ord("R")):
                    if game.score > hiscore:
                        hiscore = game.score
                        save_hiscore(hiscore_path, hiscore)
                    if game_path:
                        remove_path(game_path)
                    game.reset()
                    game_over_handled = False
                    paused_by_attention = paused_by_resume = False
                    dirty = True
                else:
                    if ch in (ord("p"), ord("P")):
                        paused_by_attention = paused_by_resume = False
                    dirty = handle_key(game, ch) or dirty

            if game.update(dt):
                dirty = True
            # Pauline's call for help blinks even while nothing moves.
            if int(now * 2) != blink:
                blink = int(now * 2)
                dirty = True

            if game.game_over and not game_over_handled:
                game_over_handled = True
                if game_path:
                    remove_path(game_path)
                hiscore = max(hiscore, game.score)
                save_hiscore(hiscore_path, hiscore)
                dirty = True

            if dirty:
                draw(game, lay, session, resume, hiscore, help_overlay)
                scr.noutrefresh()
                curses.doupdate()
                dirty = False

            time.sleep(0.01)
    finally:
        game.finish_phase()
        if game_path:
            if game.game_over:
                remove_path(game_path)
            else:
                atomic_save_json(game_path, game.to_save())
        if game.score > hiscore:
            save_hiscore(hiscore_path, game.score)


if __name__ == "__main__":
    import arcade

    sys.exit(arcade.main(["--game", "donkeykong"] + sys.argv[1:]))
