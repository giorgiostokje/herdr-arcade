#!/usr/bin/env python3
"""Pengo for the Herdr arcade (Sega 1982; see arcade.py, which runs it via
`run()`).

Pengo the penguin lives in a maze of ice blocks. Push a block and it slides
until it hits something, crushing every Sno-Bee in its way; push a block
that cannot move and it shatters. Shake the outer wall to stun the Sno-Bees
touching it, then walk over them. Line up the three diamond blocks for a big
bonus. Clear the level by destroying every Sno-Bee, including the eggs
hidden in the ice.

Structure: everything above the "curses UI" divider is pure game logic —
maze generation, movement, pushing, Sno-Bee AI, scoring, lives, levels,
save/restore and the layout plan — with no terminal dependency, so
`tests/test_pengo.py` imports this module without a terminal.
"""

from __future__ import annotations

import curses
import random
import time

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

FIELD_W = 13  # cells; each cell is two terminal columns
FIELD_H = 15

EMPTY, ICE, DIAMOND = ".", "#", "D"
BLOCKS = (ICE, DIAMOND)

UP, DOWN, LEFT, RIGHT = "up", "down", "left", "right"
DELTAS = {UP: (0, -1), DOWN: (0, 1), LEFT: (-1, 0), RIGHT: (1, 0)}
DIRS = (UP, DOWN, LEFT, RIGHT)

START = (6, 6)          # Pengo's start cell (a maze "room" cell: even x, y)
LIVES = 3
N_DIAMONDS = 3
DIAMOND_MIN_APART = 4   # Manhattan distance between any two diamonds

CRUSH_POINTS = (0, 400, 1600, 3200, 6400)  # by Sno-Bees crushed in one slide
BREAK_POINTS = 30
EGG_POINTS = 500
STUN_KILL_POINTS = 100
DIAMOND_BONUS_WALL = 5000     # line touches the outer wall
DIAMOND_BONUS_FREE = 10000    # line clear of the outer wall
# Level time bonus: (finished within seconds, points)
TIME_BONUS = ((20, 5000), (30, 2000), (40, 1000), (50, 500), (60, 10))

STUN_TIME = 3.0
RESPAWN_GRACE = 1.5     # Sno-Bees stay stunned this long after a respawn
HATCH_DELAY = 1.0       # seconds before an egg replaces a destroyed Sno-Bee
DEATH_TIME = 1.0
LEVEL_CLEAR_TIME = 2.0
GREEDY = 0.75           # chance a Sno-Bee takes the step closest to Pengo
BREAK_FROM_LEVEL = 3    # Sno-Bees occasionally break ice from this level
BREAK_CHANCE = 0.08

SAVE_VERSION = 1


def bee_interval(level: int) -> float:
    """Seconds per Sno-Bee step at `level`: faster each level, 0.2s floor."""
    return max(0.2, 0.6 * 0.88 ** (level - 1))


def active_target(level: int) -> int:
    """How many Sno-Bees are out of their eggs at once."""
    return 3 if level < 3 else 4


def egg_count(level: int) -> int:
    """Eggs hidden in the ice at the start of `level`."""
    return min(2 + level, 8)


def time_bonus(seconds: float) -> int:
    for limit, points in TIME_BONUS:
        if seconds < limit:
            return points
    return 0


def crush_points(n: int) -> int:
    return CRUSH_POINTS[min(n, len(CRUSH_POINTS) - 1)]


def in_bounds(x: int, y: int) -> bool:
    return 0 <= x < FIELD_W and 0 <= y < FIELD_H


def generate_maze(rng: random.Random) -> list:
    """A perfect maze of ice: every cell starts as a block, then a
    randomized depth-first search carves the "room" cells (even x and y) and
    the passages between neighbouring rooms. Returns grid[y][x]."""
    grid = [[ICE] * FIELD_W for _ in range(FIELD_H)]
    sx, sy = START
    grid[sy][sx] = EMPTY
    stack = [(sx, sy)]
    while stack:
        x, y = stack[-1]
        options = []
        for d in DIRS:
            dx, dy = DELTAS[d]
            nx, ny = x + 2 * dx, y + 2 * dy
            if in_bounds(nx, ny) and grid[ny][nx] == ICE:
                options.append((nx, ny, x + dx, y + dy))
        if not options:
            stack.pop()
            continue
        nx, ny, wx, wy = rng.choice(options)
        grid[wy][wx] = EMPTY
        grid[ny][nx] = EMPTY
        stack.append((nx, ny))
    return grid


def reachable(grid, start) -> set:
    """Empty cells reachable from `start` through empty cells."""
    seen = {start}
    todo = [start]
    while todo:
        x, y = todo.pop()
        for dx, dy in DELTAS.values():
            n = (x + dx, y + dy)
            if n not in seen and in_bounds(*n) and grid[n[1]][n[0]] == EMPTY:
                seen.add(n)
                todo.append(n)
    return seen


def diamonds_aligned(cells) -> str | None:
    """'row' or 'col' when the three diamond cells form one contiguous line,
    else None."""
    cells = sorted(cells)
    if len(cells) != N_DIAMONDS:
        return None
    xs = sorted(c[0] for c in cells)
    ys = sorted(c[1] for c in cells)
    if len(set(ys)) == 1 and all(b - a == 1 for a, b in zip(xs, xs[1:])):
        return "row"
    if len(set(xs)) == 1 and all(b - a == 1 for a, b in zip(ys, ys[1:])):
        return "col"
    return None


def touches_wall(x: int, y: int) -> bool:
    return x == 0 or y == 0 or x == FIELD_W - 1 or y == FIELD_H - 1


class Phase:
    """A transient animation as an explicit logic state (like tetris'
    Clearing): 'dying' or 'level_clear'. The state change happens when it
    completes — via update() or resolve_phases()."""

    __slots__ = ("kind", "duration", "elapsed")

    def __init__(self, kind: str, duration: float):
        self.kind = kind
        self.duration = duration
        self.elapsed = 0.0


class Bee:
    __slots__ = ("x", "y", "stun")

    def __init__(self, x: int, y: int, stun: float = 0.0):
        self.x, self.y, self.stun = x, y, stun

    @property
    def pos(self):
        return (self.x, self.y)


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
        self.paused = False
        self.game_over = False
        self.phase: Phase | None = None
        # Cosmetic, never saved: recent effects for the renderer.
        self.splats: list = []      # [x, y, seconds left]
        self.shake = None           # [direction, seconds left]
        self.message = None         # [text, seconds left]
        self._start_level()

    def reset(self) -> None:
        self.__init__(self.rng)

    # -- level setup -------------------------------------------------------

    def _start_level(self) -> None:
        self.grid = generate_maze(self.rng)
        self.pengo = START
        self.facing = DOWN
        self.level_time = 0.0
        self.bee_timer = 0.0
        self.hatch_timer = 0.0
        self.diamond_done = False
        self.last_time_bonus = 0
        self.splats = []
        self.shake = None
        self.message = None
        self._place_diamonds()
        self._place_bees_and_eggs()

    def _ice_cells(self) -> list:
        return [(x, y) for y in range(FIELD_H) for x in range(FIELD_W)
                if self.grid[y][x] == ICE]

    def _place_diamonds(self) -> None:
        sx, sy = START
        for _ in range(200):
            cand = [c for c in self._ice_cells()
                    if abs(c[0] - sx) + abs(c[1] - sy) > 2]
            picks = self.rng.sample(cand, N_DIAMONDS)
            apart = all(abs(a[0] - b[0]) + abs(a[1] - b[1]) >= DIAMOND_MIN_APART
                        for i, a in enumerate(picks) for b in picks[i + 1:])
            if apart:
                break
        for x, y in picks:
            self.grid[y][x] = DIAMOND

    def _place_bees_and_eggs(self) -> None:
        sx, sy = START
        far = [(x, y) for y in range(0, FIELD_H, 2) for x in range(0, FIELD_W, 2)
               if abs(x - sx) + abs(y - sy) >= 6]
        self.bees = [Bee(x, y) for x, y in self.rng.sample(far, active_target(self.level))]
        ice = self._ice_cells()
        self.eggs = set(self.rng.sample(ice, min(egg_count(self.level), len(ice))))

    # -- queries -----------------------------------------------------------

    def cell(self, x: int, y: int) -> str:
        return self.grid[y][x]

    def bee_at(self, pos):
        for bee in self.bees:
            if bee.pos == pos:
                return bee
        return None

    def diamonds(self) -> list:
        return [(x, y) for y in range(FIELD_H) for x in range(FIELD_W)
                if self.grid[y][x] == DIAMOND]

    def remaining(self) -> int:
        """Sno-Bees left this level, eggs included."""
        return len(self.bees) + len(self.eggs)

    def _can_act(self) -> bool:
        return not (self.paused or self.game_over or self.phase is not None)

    # -- Pengo -------------------------------------------------------------

    def move(self, d: str) -> bool:
        """Face `d` and step one cell if it is open. Walking over a stunned
        Sno-Bee kills it; walking into an active one is death. True if Pengo
        moved."""
        if d not in DELTAS or not self._can_act():
            return False
        self.facing = d
        dx, dy = DELTAS[d]
        nx, ny = self.pengo[0] + dx, self.pengo[1] + dy
        if not in_bounds(nx, ny) or self.grid[ny][nx] != EMPTY:
            return False
        self.pengo = (nx, ny)
        bee = self.bee_at(self.pengo)
        if bee is not None:
            if bee.stun > 0:
                self._kill_bee(bee, STUN_KILL_POINTS)
            else:
                self._die()
        return True

    def push(self) -> str:
        """Push whatever Pengo faces. Returns what happened: 'shake' (outer
        wall), 'slide', 'break', 'none' (open floor or an unbreakable
        diamond that cannot move)."""
        if not self._can_act():
            return "none"
        dx, dy = DELTAS[self.facing]
        bx, by = self.pengo[0] + dx, self.pengo[1] + dy
        if not in_bounds(bx, by):
            self._shake_wall(self.facing)
            return "shake"
        kind = self.grid[by][bx]
        if kind == EMPTY:
            return "none"
        nx, ny = bx + dx, by + dy
        if not in_bounds(nx, ny) or self.grid[ny][nx] != EMPTY:
            # Blocked directly behind: ice shatters, diamonds hold.
            if kind == DIAMOND:
                return "none"
            self.grid[by][bx] = EMPTY
            if (bx, by) in self.eggs:
                self.eggs.discard((bx, by))
                self.score += EGG_POINTS
                self.splats.append([bx, by, 0.4])
                self._check_clear()
            else:
                self.score += BREAK_POINTS
            return "break"
        # Slide until the next cell is a wall or a block; Sno-Bees in the
        # path (stunned or not) are crushed.
        crushed = []
        x, y = bx, by
        while in_bounds(x + dx, y + dy) and self.grid[y + dy][x + dx] == EMPTY:
            x, y = x + dx, y + dy
            bee = self.bee_at((x, y))
            if bee is not None:
                crushed.append(bee)
        self.grid[by][bx] = EMPTY
        self.grid[y][x] = kind
        if (bx, by) in self.eggs:
            self.eggs.discard((bx, by))
            self.eggs.add((x, y))
        if crushed:
            for bee in crushed:
                self.bees.remove(bee)
                self.splats.append([bee.x, bee.y, 0.4])
            self.score += crush_points(len(crushed))
        if kind == DIAMOND:
            self._check_diamonds()
        self._check_clear()
        return "slide"

    def _shake_wall(self, d: str) -> None:
        test = {
            LEFT: lambda b: b.x == 0,
            RIGHT: lambda b: b.x == FIELD_W - 1,
            UP: lambda b: b.y == 0,
            DOWN: lambda b: b.y == FIELD_H - 1,
        }[d]
        for bee in self.bees:
            if test(bee):
                bee.stun = max(bee.stun, STUN_TIME)
        self.shake = [d, 0.3]

    def _check_diamonds(self) -> None:
        if self.diamond_done:
            return
        cells = self.diamonds()
        if diamonds_aligned(cells) is None:
            return
        self.diamond_done = True
        walled = any(touches_wall(x, y) for x, y in cells)
        bonus = DIAMOND_BONUS_WALL if walled else DIAMOND_BONUS_FREE
        self.score += bonus
        for bee in self.bees:
            bee.stun = max(bee.stun, STUN_TIME)
        self.message = [f"DIAMONDS +{bonus}", 2.0]

    def _kill_bee(self, bee, points: int) -> None:
        self.bees.remove(bee)
        self.score += points
        self.splats.append([bee.x, bee.y, 0.4])
        self._check_clear()

    def _check_clear(self) -> None:
        if self.phase is None and not self.bees and not self.eggs:
            self.last_time_bonus = time_bonus(self.level_time)
            self.score += self.last_time_bonus
            self.phase = Phase("level_clear", LEVEL_CLEAR_TIME)

    def _die(self) -> None:
        if self.phase is not None:
            return
        self.lives -= 1
        self.phase = Phase("dying", DEATH_TIME)

    def _respawn(self) -> None:
        """Pengo back at (or nearest open cell to) the start; the Sno-Bees
        are stunned briefly so the respawn is not an instant death."""
        occupied = {b.pos for b in self.bees}
        sx, sy = START
        best = None
        for y in range(FIELD_H):
            for x in range(FIELD_W):
                if self.grid[y][x] == EMPTY and (x, y) not in occupied:
                    dist = abs(x - sx) + abs(y - sy)
                    if best is None or dist < best[0]:
                        best = (dist, (x, y))
        if best is None:  # the ice filled the maze: free the start cell
            self.grid[sy][sx] = EMPTY
            self.eggs.discard(START)
            best = (0, START)
        self.pengo = best[1]
        self.facing = DOWN
        self.bee_timer = 0.0
        for bee in self.bees:
            bee.stun = max(bee.stun, RESPAWN_GRACE)

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

    # -- Sno-Bees ----------------------------------------------------------

    def _bee_step(self, bee) -> None:
        """One step: usually the open neighbour closest to Pengo, sometimes
        a random one; in later levels a Sno-Bee now and then breaks the ice
        between it and Pengo instead."""
        px, py = self.pengo
        occupied = {b.pos for b in self.bees if b is not bee}
        options = []
        toward_ice = []
        for d in DIRS:
            dx, dy = DELTAS[d]
            nx, ny = bee.x + dx, bee.y + dy
            if not in_bounds(nx, ny):
                continue
            dist = abs(nx - px) + abs(ny - py)
            kind = self.grid[ny][nx]
            if kind == EMPTY and (nx, ny) not in occupied:
                options.append((dist, nx, ny))
            elif (kind == ICE and (nx, ny) not in self.eggs
                  and dist < abs(bee.x - px) + abs(bee.y - py)):
                toward_ice.append((nx, ny))
        if (toward_ice and self.level >= BREAK_FROM_LEVEL
                and self.rng.random() < BREAK_CHANCE):
            x, y = self.rng.choice(toward_ice)
            self.grid[y][x] = EMPTY
            return
        if not options:
            return
        if self.rng.random() < GREEDY:
            best = min(o[0] for o in options)
            options = [o for o in options if o[0] == best]
        _, bee.x, bee.y = self.rng.choice(options)

    def step_bees(self) -> None:
        """Every active Sno-Bee takes one step; touching Pengo kills him."""
        for bee in list(self.bees):
            if bee.stun > 0:
                continue
            self._bee_step(bee)
            if bee.pos == self.pengo:
                self._die()
                return

    def _hatch(self) -> None:
        """An egg hatches: its ice block breaks and a Sno-Bee steps out."""
        eggs = sorted(self.eggs)
        occupied = {b.pos for b in self.bees}
        eggs = [e for e in eggs if e != self.pengo and e not in occupied]
        if not eggs:
            return
        x, y = self.rng.choice(eggs)
        self.eggs.discard((x, y))
        self.grid[y][x] = EMPTY
        self.bees.append(Bee(x, y))

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
        for bee in self.bees:
            if bee.stun > 0:
                bee.stun = max(0.0, bee.stun - dt)
                changed = True
        if len(self.bees) < active_target(self.level) and self.eggs:
            self.hatch_timer += dt
            if self.hatch_timer >= HATCH_DELAY:
                self.hatch_timer = 0.0
                self._hatch()
                changed = True
        else:
            self.hatch_timer = 0.0
        self.bee_timer += dt
        interval = bee_interval(self.level)
        while self.bee_timer >= interval:
            self.bee_timer -= interval
            changed = True
            self.step_bees()
            if self.phase is not None:
                self.bee_timer = 0.0
                break
        return changed

    def _tick_effects(self, dt: float) -> bool:
        changed = False
        for s in self.splats:
            s[2] -= dt
        before = len(self.splats)
        self.splats = [s for s in self.splats if s[2] > 0]
        changed = changed or len(self.splats) != before or bool(self.splats)
        for attr in ("shake", "message"):
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
            "grid": ["".join(row) for row in self.grid],
            "eggs": sorted([list(e) for e in self.eggs]),
            "bees": [[b.x, b.y, b.stun] for b in self.bees],
            "pengo": list(self.pengo),
            "facing": self.facing,
            "level_time": self.level_time,
            "bee_timer": self.bee_timer,
            "hatch_timer": self.hatch_timer,
            "diamond_done": self.diamond_done,
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

            def num(v):
                if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                    raise ValueError(v)
                return float(v)

            def cell(v):
                if not (isinstance(v, list) and len(v) == 2):
                    raise ValueError(v)
                x, y = integer(v[0]), integer(v[1])
                if not in_bounds(x, y):
                    raise ValueError(v)
                return (x, y)

            state = _rng_from_json(data["rng"])
            if state is None:
                return None
            game = cls(rng)
            game.score = integer(data["score"])
            game.level = integer(data["level"], 1)
            game.lives = integer(data["lives"], 1)
            game.game_over = bool(data["game_over"])

            rows = data["grid"]
            if not isinstance(rows, list) or len(rows) != FIELD_H:
                return None
            grid = []
            for row in rows:
                if not isinstance(row, str) or len(row) != FIELD_W:
                    return None
                if any(c not in (EMPTY, ICE, DIAMOND) for c in row):
                    return None
                grid.append(list(row))
            game.grid = grid
            if len(game.diamonds()) != N_DIAMONDS:
                return None

            eggs = {cell(e) for e in data["eggs"]}
            if any(grid[y][x] != ICE for x, y in eggs):
                return None
            game.eggs = eggs

            game.pengo = cell(data["pengo"])
            if grid[game.pengo[1]][game.pengo[0]] != EMPTY:
                return None
            if data["facing"] not in DELTAS:
                return None
            game.facing = data["facing"]

            bees = []
            for b in data["bees"]:
                if not isinstance(b, list) or len(b) != 3:
                    return None
                x, y = cell(b[:2])
                if grid[y][x] != EMPTY or (x, y) in {bb.pos for bb in bees}:
                    return None
                bees.append(Bee(x, y, num(b[2])))
            game.bees = bees
            if not game.bees and not game.eggs and not game.game_over:
                return None  # a cleared level is never saved mid-transition

            game.level_time = num(data["level_time"])
            game.bee_timer = num(data["bee_timer"])
            game.hatch_timer = num(data["hatch_timer"])
            if not isinstance(data["diamond_done"], bool):
                return None
            game.diamond_done = data["diamond_done"]

            # Restoring the RNG makes a resumed game play out exactly as the
            # saved one would have.
            game.rng.setstate(state)
            game.phase = None
            game.splats, game.shake, game.message = [], None, None
            game.last_time_bonus = 0
            return game
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            return None


TITLE = "Pengo"

# Every key the game reacts to (upper-case letters work too).
CONTROLS = [
    ("arrows", "walk / turn"),
    ("wasd hjkl", "walk too"),
    ("space", "push, break, shake"),
    ("enter", "push too"),
    ("?", "help"),
    ("p / r", "pause / restart"),
    ("q", "menu"),
]

HELP = [
    ("Goal",
     "You are Pengo (@v), a penguin in a maze of ice blocks. Destroy every "
     "Sno-Bee on the level - including the ones still hidden as eggs inside "
     "the ice - to move on to the next level and a new maze."),
    ("Controls",
     "- arrows (or w a s d / h j k l): walk one cell. A direction key always "
     "turns Pengo to face that way, even when ice blocks the step; the "
     "arrow in his glyph (@v @^ <@ @>) shows where he faces.\n"
     "- space (or enter): push. It acts on the cell Pengo is facing - see "
     "the next section.\n"
     "- p pauses, r starts a new game, q goes back to the arcade menu.\n"
     "- ? opens this help (the game pauses while it is open)."),
    ("Push, break and shake",
     "Space does one of three things, depending on what Pengo faces:\n"
     "- PUSH (slide): facing a block with an open cell behind it. The block "
     "slides until it hits another block or the outer wall, crushing every "
     "Sno-Bee in its path, stunned or not.\n"
     "- BREAK: facing an ice block that cannot move because another block "
     "or the wall is right behind it. The ice shatters. Diamond blocks never "
     "break - they just stay put.\n"
     "- SHAKE: standing at the edge of the field facing the outer wall. The "
     "wall flashes and every Sno-Bee touching that wall is stunned for 3 "
     "seconds.\n"
     "Facing open floor, space does nothing."),
    ("Sno-Bees",
     "Sno-Bees (}{) chase you one step at a time, usually taking the step "
     "that brings them closest. Touching an active Sno-Bee - you walk into "
     "it or it walks into you - costs a life. Three are out at once (four "
     "from level 3), they speed up every level, and from level 3 they "
     "sometimes break the ice between you and them."),
    ("Stunned Sno-Bees",
     "A stunned Sno-Bee shows as zz. It cannot move and cannot hurt you: "
     "walk over it to destroy it, or crush it with a sliding block. Sno-Bees "
     "get stunned by a wall shake (3 s, only those touching that wall), by "
     "lining up the diamonds (3 s, all of them), and for 1.5 s after you "
     "lose a life. When the zz turns back into }{ it is dangerous again."),
    ("Eggs hidden in the ice",
     "Each level hides eggs inside some ice blocks (3 on level 1, one more "
     "per level, up to 8). An egg block looks exactly like plain ice ([]). "
     "Whenever fewer Sno-Bees than the limit are out, an egg hatches about "
     "once a second: its block breaks open and a new Sno-Bee steps out.\n"
     "Break an egg's block (push it when it cannot slide) to destroy the "
     "egg before it hatches. A sliding block carries its egg along. The "
     "stats box shows 'bees N+M eggs': the level is clear when both are 0."),
    ("Diamond line-up",
     "Three diamond blocks (<>) sit in the maze. They slide like ice but "
     "never break. Push all three into one straight line - side by side in "
     "a row or a column, no gaps - for a bonus: 10000 if none of them "
     "touches the outer wall, 5000 if the line touches it. Every Sno-Bee is "
     "stunned for 3 s and the diamonds blink. Once per level."),
    ("Lives and levels",
     "You have 3 lives. After losing one, Pengo restarts near the middle "
     "and the Sno-Bees are briefly stunned; at 0 lives the game is over. "
     "Clearing a level gives a time bonus and builds a fresh maze with more "
     "eggs and faster Sno-Bees."),
    ("Scoring",
     "- crush with one slide: 1 bee 400, 2 bees 1600, 3 bees 3200, 4 6400\n"
     "- walk over a stunned Sno-Bee: 100\n"
     "- break an ice block: 30; break a block holding an egg: 500\n"
     "- diamond line-up: 10000 clear of the wall, 5000 touching it\n"
     "- level cleared within 20 s: 5000, 30 s: 2000, 40 s: 1000, "
     "50 s: 500, 60 s: 10"),
    ("Legend",
     "- @v @^ <@ @>  Pengo, facing down / up / left / right\n"
     "- xx  Pengo losing a life\n"
     "- []  ice block (may hide an egg)\n"
     "- <>  diamond block (slides, never breaks)\n"
     "- }{  Sno-Bee: deadly to touch\n"
     "- zz  stunned Sno-Bee: walk over it\n"
     "- **  a Sno-Bee or egg just destroyed\n"
     "- ==== or ! flashing on the border: the wall you shook"),
    ("Tips",
     "- Wait for Sno-Bees to line up in a corridor, then slide a block down "
     "it: several at once score far more.\n"
     "- A Sno-Bee hugging the outer wall is easy prey: face that wall, "
     "shake, then walk over the zz.\n"
     "- Breaking spare ice finds eggs early; every egg broken is one "
     "Sno-Bee you never have to fight.\n"
     "- Be quick: a level cleared in under 20 seconds earns 5000."),
]

# Layout: the framed field (13 cells x 2 cols + border = 28 x 17) with, when
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
PAIR_ICE, PAIR_DIAMOND, PAIR_PENGO, PAIR_BEE = 40, 41, 42, 43
PAIR_STUN, PAIR_DEAD, PAIR_SPLAT, PAIR_WALL = 44, 45, 46, 47

ICE_GLYPH = "[]"   # every field cell is exactly two columns
DIAMOND_GLYPH = "<>"
BEE_GLYPH = "}{"
STUN_GLYPH = "zz"
SPLAT_GLYPH = "**"
DEAD_GLYPH = "xx"
PENGO_GLYPH = {UP: "@^", DOWN: "@v", LEFT: "<@", RIGHT: "@>"}

def init_colors() -> None:
    bg = default_bg()
    curses.init_pair(PAIR_ICE, curses.COLOR_CYAN, bg)
    curses.init_pair(PAIR_DIAMOND, curses.COLOR_YELLOW, bg)
    curses.init_pair(PAIR_PENGO, curses.COLOR_WHITE, bg)
    curses.init_pair(PAIR_BEE, curses.COLOR_RED, bg)
    curses.init_pair(PAIR_STUN, curses.COLOR_MAGENTA, bg)
    curses.init_pair(PAIR_DEAD, curses.COLOR_RED, bg)
    curses.init_pair(PAIR_SPLAT, curses.COLOR_YELLOW, bg)
    curses.init_pair(PAIR_WALL, curses.COLOR_BLUE, bg)


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


def diamond_flash(game: Game, now: float) -> bool:
    """Lined-up diamonds blink at 2Hz for the rest of the level."""
    return game.diamond_done and int(now * 4) % 2 == 1


def display_key(game: Game, now: float):
    """Everything on screen that changes with time alone; the loop redraws
    whenever this changes."""
    return (int(game.level_time), diamond_flash(game, now))


def draw_field(win, game: Game) -> None:
    phase = game.phase
    if game.shake is not None:
        # The shaken wall flashes.
        d = game.shake[0]
        attr = curses.color_pair(PAIR_WALL) | curses.A_BOLD | curses.A_REVERSE
        if d in (UP, DOWN):
            row = 0 if d == UP else FIELD_WIN_H - 1
            safe_addstr(win, row, 1, "=" * (FIELD_WIN_W - 2), attr)
        else:
            col = 0 if d == LEFT else FIELD_WIN_W - 1
            for y in range(1, FIELD_WIN_H - 1):
                safe_addstr(win, y, col, "!", attr)

    flash = phase is not None and phase.kind == "level_clear" and int(phase.elapsed * 6) % 2
    ice_attr = curses.color_pair(PAIR_ICE) | (curses.A_BOLD if flash else 0)
    dia_attr = curses.color_pair(PAIR_DIAMOND) | curses.A_BOLD
    if diamond_flash(game, time.monotonic()):
        dia_attr |= curses.A_REVERSE
    for y in range(FIELD_H):
        for x in range(FIELD_W):
            c = game.grid[y][x]
            if c == ICE:
                _put(win, x, y, ICE_GLYPH, ice_attr)
            elif c == DIAMOND:
                _put(win, x, y, DIAMOND_GLYPH, dia_attr)

    for sx, sy, _ in game.splats:
        _put(win, sx, sy, SPLAT_GLYPH, curses.color_pair(PAIR_SPLAT) | curses.A_BOLD)
    for bee in game.bees:
        if bee.stun > 0:
            _put(win, bee.x, bee.y, STUN_GLYPH, curses.color_pair(PAIR_STUN) | curses.A_BOLD)
        else:
            _put(win, bee.x, bee.y, BEE_GLYPH, curses.color_pair(PAIR_BEE) | curses.A_BOLD)

    dying = phase is not None and phase.kind == "dying"
    if not game.game_over:
        px, py = game.pengo
        if dying:
            if int(phase.elapsed * 8) % 2 == 0:
                _put(win, px, py, DEAD_GLYPH, curses.color_pair(PAIR_DEAD) | curses.A_BOLD)
        else:
            _put(win, px, py, PENGO_GLYPH[game.facing],
                 curses.color_pair(PAIR_PENGO) | curses.A_BOLD)

    mid = 1 + FIELD_H // 2
    if game.game_over:
        _centered(win, mid - 1, " GAME OVER ", curses.A_STANDOUT | curses.A_BOLD)
        _centered(win, mid + 1, " r restart | q menu ", curses.A_STANDOUT)
    elif game.paused:
        _centered(win, mid - 1, " PAUSED ", curses.A_STANDOUT | curses.A_BOLD)
    elif phase is not None and phase.kind == "level_clear":
        _centered(win, mid - 1, f" LEVEL {game.level + 1} ",
                  curses.color_pair(PAIR_DIAMOND) | curses.A_BOLD)
        if game.last_time_bonus:
            _centered(win, mid + 1, f" time +{game.last_time_bonus} ",
                      curses.color_pair(PAIR_DIAMOND))
    elif game.message is not None:
        _centered(win, mid - 1, f" {game.message[0]} ",
                  curses.color_pair(PAIR_DIAMOND) | curses.A_BOLD)


def draw_stats(win, game: Game, best: int) -> None:
    rows, cols = win.getmaxyx()
    inner = cols - 2
    safe_addstr(win, 0, 2, f" {TITLE} "[: max(0, cols - 3)], curses.A_BOLD)
    if game.game_over:
        state = "GAME OVER"
    elif game.paused:
        state = "paused"
    elif game.phase is not None:
        state = "ouch!" if game.phase.kind == "dying" else "level clear!"
    else:
        state = f"time {int(game.level_time)}s"
    lines = [
        (f"score {game.score}", curses.A_BOLD),
        (f"best  {max(best, game.score)}", 0),
        (f"level {game.level}", 0),
        (f"lives {game.lives}", curses.color_pair(PAIR_PENGO)),
        (f"bees  {len(game.bees)}+{len(game.eggs)} eggs", curses.color_pair(PAIR_BEE)),
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
            head = " pengo - GAME OVER "
        else:
            head = f" pengo - {'paused' if game.paused else 'playing'} "
        if lay.plan["hint"] is None and lay.controls is None:
            head += "| ? help "  # no hint row and no controls box
        if lay.stats is None:  # no stats box in this tier: score goes here
            head += (f"| score {game.score} | lives {game.lives} | lvl {game.level}"
                     f" | bees {game.remaining()} ")
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


PUSH_KEYS = (ord(" "), 10, 13, curses.KEY_ENTER)


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
    elif ch in PUSH_KEYS:
        game.push()
    return None


def run(session) -> None:
    """Play Pengo until `q` (back to the arcade menu) or SIGTERM. The game is
    saved on every exit path and resumed, paused, on the next run."""
    scr = session.stdscr
    init_colors()
    game_path = session.path("pengo-game.json")
    hiscore_path = session.path("pengo-hiscore.json")
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
            # Time-driven visuals (the seconds counter, the diamond flash)
            # redraw whenever what they show changes.
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
        # pending death/level transition is resolved first so the save is a
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

    sys.exit(arcade.main(["--game", "pengo"] + sys.argv[1:]))
