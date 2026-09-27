#!/usr/bin/env python3
"""Qix (Taito 1981) for the Herdr arcade (see arcade.py, which runs it via
`run()`).

Structure: everything above the "curses UI" divider is pure game logic —
the cell grid, safe lines, Stix trails, shape claiming, the Qix, Sparx, the
fuse, lives, levels, save/restore and the layout plan — with no terminal
dependency, so `tests/test_qix.py` imports this module without a terminal.

Terminals give no key-up events, so the draw button is a toggle: `space`
arms fast draw, `f` slow draw (double points, but the Stix grows at a
limited rate). Moving off a safe line while armed leaves a trail.
"""

from __future__ import annotations

import curses
import math
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

# One terminal cell per field cell. The outer ring is the playfield border
# (the first safe line) and doubles as the field's visible frame.
FIELD_W = 58
FIELD_H = 23

EMPTY, EDGE, FILL_FAST, FILL_SLOW = 0, 1, 2, 3
CELL_CHARS = ".+fs"  # save encoding, indexed by cell state

UP, DOWN, LEFT, RIGHT = "up", "down", "left", "right"
DELTAS = {UP: (0, -1), DOWN: (0, 1), LEFT: (-1, 0), RIGHT: (1, 0)}
VEC_DIR = {v: k for k, v in DELTAS.items()}

FAST, SLOW = "fast", "slow"

LIVES = 3
CLAIM_TARGET = 75          # percent to clear a level
POINTS_PER_CELL = 10       # x2 for slow draw
BONUS_PER_PERCENT = 1000   # per whole percent above the target
SLOW_STEP = 0.12           # seconds between trail steps in slow draw

FUSE_DELAY = 1.5           # idle seconds on a trail before the fuse lights
FUSE_SPEED = 5.0           # trail cells burned per second

START_MARKER = (FIELD_W // 2, FIELD_H - 1)
SPARX_START = (FIELD_W // 2, 0)
START_SPARX = 2
MAX_SPARX = 4
SPARX_SPAWN_EVERY = 25.0   # seconds of level time per extra Sparx
SUPER_TIME = 60.0          # level seconds before Sparx turn Super (faster)

QIX_ARMS = 2               # segments per side of the centre
QIX_MIN_LEN, QIX_MAX_LEN = 2, 6
QIX_BEND = 0.8             # radians between consecutive segments
QIX_HISTORY = 5

DEATH_TIME = 1.0
LEVEL_CLEAR_TIME = 1.5

SAVE_VERSION = 1


def qix_interval(level: int) -> float:
    """Seconds per Qix step: faster each level."""
    return max(0.03, 0.07 * 0.9 ** (level - 1))


def sparx_interval(level: int, super_: bool = False) -> float:
    """Seconds per Sparx step: faster each level, faster still when Super."""
    base = max(0.05, 0.16 * 0.92 ** (level - 1))
    return base * (0.6 if super_ else 1.0)


def new_grid() -> list:
    """Fresh field: border ring of safe line around unclaimed space."""
    grid = [[EMPTY] * FIELD_W for _ in range(FIELD_H)]
    for x in range(FIELD_W):
        grid[0][x] = grid[FIELD_H - 1][x] = EDGE
    for y in range(FIELD_H):
        grid[y][0] = grid[y][FIELD_W - 1] = EDGE
    return grid


INTERIOR_TOTAL = (FIELD_W - 2) * (FIELD_H - 2)


def in_bounds(x: int, y: int) -> bool:
    return 0 <= x < FIELD_W and 0 <= y < FIELD_H


def _round(v: float) -> int:
    return int(math.floor(v + 0.5))


def line_cells(x0: int, y0: int, x1: int, y1: int) -> list:
    """Bresenham cells from (x0, y0) to (x1, y1), both inclusive."""
    cells = []
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    x, y = x0, y0
    while True:
        cells.append((x, y))
        if x == x1 and y == y1:
            return cells
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x += sx
        if e2 <= dx:
            err += dx
            y += sy


def right_of(d: tuple) -> tuple:
    """Clockwise quarter turn on screen (y grows downward)."""
    return (-d[1], d[0])


def left_of(d: tuple) -> tuple:
    return (d[1], -d[0])


class Phase:
    """A transient animation as an explicit logic state (like tetris'
    Clearing): 'dying' or 'level_clear'. The state change happens when it
    completes — via update() or resolve_phases()."""

    __slots__ = ("kind", "duration", "elapsed")

    def __init__(self, kind: str, duration: float):
        self.kind = kind
        self.duration = duration
        self.elapsed = 0.0


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
        self.last_bonus = 0
        self._start_level()

    def reset(self) -> None:
        self.__init__(self.rng)

    # -- level / respawn ---------------------------------------------------

    def _start_level(self) -> None:
        self.grid = new_grid()
        self.marker = START_MARKER
        self.mode = None           # None (move), FAST or SLOW
        self._clear_trail()
        self.level_time = 0.0
        self.spawn_timer = 0.0
        self.sparx_timer = 0.0
        self.qix_timer = 0.0
        self.sparx = [
            {"x": SPARX_START[0], "y": SPARX_START[1], "dir": RIGHT, "cw": True},
            {"x": SPARX_START[0], "y": SPARX_START[1], "dir": LEFT, "cw": False},
        ][:START_SPARX]
        self._reset_qix()

    def _clear_trail(self) -> None:
        self.trail: list = []
        self.trail_set: set = set()
        self.trail_start = None
        self.fuse = None           # burned trail cells (float) or None
        self.idle = 0.0
        self.slow_cd = 0.0

    def _reset_qix(self) -> None:
        ang = self.rng.uniform(0, 2 * math.pi)
        self.qix = {
            "x": float(FIELD_W // 2), "y": float(FIELD_H // 2),
            "vx": math.cos(ang) * 0.7, "vy": math.sin(ang) * 0.35,
            "a": self.rng.uniform(0, math.pi), "va": 0.2, "len": 4,
        }
        self.qix_history: list = []

    # -- queries -----------------------------------------------------------

    def cell(self, x: int, y: int) -> int:
        return self.grid[y][x]

    def empty_count(self) -> int:
        return sum(row.count(EMPTY) for row in self.grid)

    def claimed_percent(self) -> float:
        """Share of the interior (border ring excluded) no longer EMPTY."""
        return 100.0 * (INTERIOR_TOTAL - self.empty_count()) / INTERIOR_TOTAL

    def is_super(self) -> bool:
        return self.level_time >= SUPER_TIME

    def _edge_bfs(self, start) -> dict:
        """BFS distances over EDGE cells from `start` (4-connected)."""
        dist = {start: 0}
        q = deque([start])
        while q:
            x, y = q.popleft()
            for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
                n = (x + dx, y + dy)
                if n in dist or not in_bounds(*n) or self.grid[n[1]][n[0]] != EDGE:
                    continue
                dist[n] = dist[(x, y)] + 1
                q.append(n)
        return dist

    def _nearest_edge(self, start) -> tuple:
        """Closest EDGE cell to `start` over any cells (BFS)."""
        seen = {start}
        q = deque([start])
        while q:
            x, y = q.popleft()
            if self.grid[y][x] == EDGE:
                return (x, y)
            for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
                n = (x + dx, y + dy)
                if n not in seen and in_bounds(*n):
                    seen.add(n)
                    q.append(n)
        return start

    def _far_edge(self) -> tuple:
        """The safe-line cell farthest (along the lines) from the marker."""
        origin = self.trail_start if self.trail else self.marker
        if self.grid[origin[1]][origin[0]] != EDGE:
            origin = self._nearest_edge(origin)
        dist = self._edge_bfs(origin)
        best, best_d = origin, -1
        for c, d in dist.items():  # insertion order: deterministic ties
            if d > best_d:
                best, best_d = c, d
        return best

    # -- input -------------------------------------------------------------

    def toggle_draw(self, mode: str) -> bool:
        """`space` (FAST) / `f` (SLOW) arm or disarm drawing. Mid-trail the
        mode is locked, except that slow may be upgraded to fast (the whole
        shape then scores as fast, as in the arcade)."""
        if self.game_over or self.paused or self.phase is not None:
            return False
        if self.trail:
            if mode == FAST and self.mode == SLOW:
                self.mode = FAST
                return True
            return False
        self.mode = None if self.mode == mode else mode
        return True

    def move(self, d: str) -> bool:
        """Move the marker one cell. Returns True if it moved."""
        if d not in DELTAS or self.game_over or self.paused or self.phase is not None:
            return False
        dx, dy = DELTAS[d]
        n = (self.marker[0] + dx, self.marker[1] + dy)
        if not in_bounds(*n):
            return False
        state = self.grid[n[1]][n[0]]
        if not self.trail:
            if state == EDGE:
                self.marker = n
            elif state == EMPTY and self.mode is not None:
                if self.mode == SLOW and self.slow_cd > 0:
                    return False
                self.trail_start = self.marker
                self.trail = [n]
                self.trail_set = {n}
                self.marker = n
                if self.mode == SLOW:
                    self.slow_cd = SLOW_STEP
            else:
                return False
        else:
            if n in self.trail_set or n == self.trail_start:
                return False  # the Stix may not cross itself
            if state == EMPTY:
                if self.mode == SLOW and self.slow_cd > 0:
                    return False
                self.trail.append(n)
                self.trail_set.add(n)
                self.marker = n
                if self.mode == SLOW:
                    self.slow_cd = SLOW_STEP
            elif state == EDGE:
                self.marker = n
                self._close_shape()
            else:
                return False
        self.idle = 0.0
        self.fuse = None
        if self.phase is None:
            self._check_sparx_hit()
        if self.phase is None and self.trail:
            self._check_qix_hit()
        return True

    # -- claiming ----------------------------------------------------------

    def _close_shape(self) -> None:
        """The trail reached a safe line: it becomes safe line, every empty
        region not holding the Qix is claimed, and safe lines left with no
        unclaimed neighbour become claimed interior."""
        fill = FILL_SLOW if self.mode == SLOW else FILL_FAST
        grid = self.grid
        for x, y in self.trail:
            grid[y][x] = EDGE
        claimed = len(self.trail)

        keep = set()
        qx, qy = self._qix_anchor()
        if grid[qy][qx] == EMPTY:
            keep.add((qx, qy))
            q = deque([(qx, qy)])
            while q:
                x, y = q.popleft()
                for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
                    n = (x + dx, y + dy)
                    if n not in keep and grid[n[1]][n[0]] == EMPTY:
                        keep.add(n)
                        q.append(n)
        for y in range(FIELD_H):
            row = grid[y]
            for x in range(FIELD_W):
                if row[x] == EMPTY and (x, y) not in keep:
                    row[x] = fill
                    claimed += 1

        # Safe lines stay safe only while touching (8-way) unclaimed space;
        # 4-way would retire the outer corner of every L and break loops.
        retire = []
        for y in range(FIELD_H):
            for x in range(FIELD_W):
                if grid[y][x] != EDGE:
                    continue
                touching = False
                for ny in (y - 1, y, y + 1):
                    for nx in (x - 1, x, x + 1):
                        if in_bounds(nx, ny) and grid[ny][nx] == EMPTY:
                            touching = True
                            break
                    if touching:
                        break
                if not touching:
                    retire.append((x, y))
        for x, y in retire:
            grid[y][x] = fill

        self.score += claimed * POINTS_PER_CELL * (2 if fill == FILL_SLOW else 1)
        if grid[self.marker[1]][self.marker[0]] != EDGE:
            # Closed into a dead end that retired: step onto the new line.
            self.marker = self._nearest_edge(self.marker)
        self._clear_trail()
        self.qix_history = []
        for s in self.sparx:
            if grid[s["y"]][s["x"]] != EDGE:
                s["x"], s["y"] = self._nearest_edge((s["x"], s["y"]))
        self._check_sparx_hit()
        if self.phase is None and self.claimed_percent() >= CLAIM_TARGET:
            over = int(self.claimed_percent()) - CLAIM_TARGET
            self.last_bonus = over * BONUS_PER_PERCENT
            self.score += self.last_bonus
            self.phase = Phase("level_clear", LEVEL_CLEAR_TIME)

    # -- enemies -----------------------------------------------------------

    def _qix_anchor(self) -> tuple:
        """The unclaimed cell the Qix occupies: its centre, or (defensively)
        the nearest EMPTY cell to it."""
        c = (min(max(_round(self.qix["x"]), 0), FIELD_W - 1),
             min(max(_round(self.qix["y"]), 0), FIELD_H - 1))
        seen = {c}
        q = deque([c])
        while q:
            x, y = q.popleft()
            if self.grid[y][x] == EMPTY:
                return (x, y)
            for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
                n = (x + dx, y + dy)
                if n not in seen and in_bounds(*n):
                    seen.add(n)
                    q.append(n)
        return c

    def qix_body(self):
        """(cells, hit): the Qix polyline rasterized outward from its centre,
        each arm stopping at the first claimed/safe cell; `hit` is True when
        it touches the Stix trail."""
        q = self.qix
        cx, cy = _round(q["x"]), _round(q["y"])
        cells = [(cx, cy)]
        hit = (cx, cy) in self.trail_set
        half = q["len"] / 2.0
        for side in (1, -1):
            px, py = q["x"], q["y"]
            prev = (cx, cy)
            blocked = False
            for k in range(QIX_ARMS):
                ang = q["a"] + k * QIX_BEND
                nx = px + side * half * math.cos(ang)
                ny = py + side * half * math.sin(ang) * 0.5
                end = (_round(nx), _round(ny))
                for c in line_cells(prev[0], prev[1], end[0], end[1])[1:]:
                    if not in_bounds(*c):
                        blocked = True
                        break
                    if c in self.trail_set:
                        hit = True
                        blocked = True
                        break
                    if self.grid[c[1]][c[0]] != EMPTY:
                        blocked = True
                        break
                    cells.append(c)
                if blocked:
                    break
                px, py, prev = nx, ny, end
        return cells, hit

    def _check_qix_hit(self) -> None:
        if self.trail and self.qix_body()[1]:
            self._die()

    def _qix_open(self, x: float, y: float) -> bool:
        cx, cy = _round(x), _round(y)
        return in_bounds(cx, cy) and (
            self.grid[cy][cx] == EMPTY or (cx, cy) in self.trail_set)

    def step_qix(self) -> None:
        q = self.qix
        rng = self.rng
        if rng.random() < 0.08:
            ang = rng.uniform(0, 2 * math.pi)
            speed = rng.uniform(0.5, 0.9)
            q["vx"], q["vy"] = math.cos(ang) * speed, math.sin(ang) * speed * 0.5
        if rng.random() < 0.06:
            q["va"] = rng.uniform(-0.35, 0.35)
        if rng.random() < 0.1:
            q["len"] = max(QIX_MIN_LEN, min(QIX_MAX_LEN, q["len"] + rng.choice((-1, 1))))
        q["a"] = (q["a"] + q["va"]) % (2 * math.pi)
        vx, vy = q["vx"], q["vy"]
        for cvx, cvy in ((vx, vy), (-vx, vy), (vx, -vy), (-vx, -vy)):
            if self._qix_open(q["x"] + cvx, q["y"] + cvy):
                q["vx"], q["vy"] = cvx, cvy
                q["x"] += cvx
                q["y"] += cvy
                break
        body, hit = self.qix_body()
        self.qix_history.append(body)
        del self.qix_history[:-QIX_HISTORY]
        if hit and self.trail:
            self._die()

    def step_sparx(self) -> None:
        """Every Sparx takes one step along the safe lines: clockwise ones
        by the right-hand rule, counter-clockwise ones by the left."""
        for s in self.sparx:
            d = DELTAS[s["dir"]]
            if s["cw"]:
                order = (right_of(d), d, left_of(d), (-d[0], -d[1]))
            else:
                order = (left_of(d), d, right_of(d), (-d[0], -d[1]))
            for od in order:
                n = (s["x"] + od[0], s["y"] + od[1])
                if in_bounds(*n) and self.grid[n[1]][n[0]] == EDGE:
                    s["x"], s["y"] = n
                    s["dir"] = VEC_DIR[od]
                    break
        self._check_sparx_hit()

    def _check_sparx_hit(self) -> None:
        if self.phase is not None:
            return
        for s in self.sparx:
            if (s["x"], s["y"]) == self.marker:
                self._die()
                return

    def _spawn_sparx(self) -> None:
        x, y = self._far_edge()
        cw = len(self.sparx) % 2 == 0
        self.sparx.append({"x": x, "y": y, "dir": RIGHT if cw else LEFT, "cw": cw})

    # -- death / phases ----------------------------------------------------

    def _die(self) -> None:
        if self.phase is not None:
            return
        self.lives -= 1
        self.phase = Phase("dying", DEATH_TIME)

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
                return
            if self.trail:
                self.marker = self.trail_start
            self._clear_trail()
            self.mode = None
            self.qix_history = []
            for s in self.sparx:
                s["x"], s["y"] = self._far_edge()
        elif phase.kind == "level_clear":
            self.level += 1
            self.last_bonus = 0
            self._start_level()

    # -- time --------------------------------------------------------------

    def update(self, dt: float) -> bool:
        """Advance by `dt` seconds. True if anything changed."""
        if self.paused or self.game_over:
            return False
        if self.phase is not None:
            self.phase.elapsed += dt
            if self.phase.elapsed >= self.phase.duration:
                self.resolve_phases()
            return True
        # Only report a change when something visible moved, so the UI
        # redraws at the enemies' pace rather than every loop tick.
        was_super = self.is_super()
        colour_tick = int(self.level_time * 6)
        self.level_time += dt
        changed = int(self.level_time * 6) != colour_tick or self.is_super() != was_super
        self.slow_cd = max(0.0, self.slow_cd - dt)

        if len(self.sparx) < MAX_SPARX:
            self.spawn_timer += dt
            if self.spawn_timer >= SPARX_SPAWN_EVERY:
                self.spawn_timer = 0.0
                self._spawn_sparx()
                changed = True

        if self.trail:
            if self.fuse is None:
                self.idle += dt
                if self.idle >= FUSE_DELAY:
                    self.fuse = 0.0
                    changed = True
            else:
                burned = int(self.fuse)
                self.fuse += FUSE_SPEED * dt
                changed = changed or int(self.fuse) != burned
                if self.fuse >= len(self.trail):
                    self._die()
                    return True

        self.qix_timer += dt
        interval = qix_interval(self.level)
        while self.qix_timer >= interval and self.phase is None:
            self.qix_timer -= interval
            self.step_qix()
            changed = True

        self.sparx_timer += dt
        interval = sparx_interval(self.level, self.is_super())
        while self.sparx_timer >= interval and self.phase is None:
            self.sparx_timer -= interval
            self.step_sparx()
            changed = True
        if self.phase is not None:
            self.qix_timer = self.sparx_timer = 0.0
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
            "game_over": self.game_over,
            "grid": ["".join(CELL_CHARS[c] for c in row) for row in self.grid],
            "marker": list(self.marker),
            "mode": self.mode,
            "trail": [list(c) for c in self.trail],
            "trail_start": None if self.trail_start is None else list(self.trail_start),
            "fuse": self.fuse,
            "idle": self.idle,
            "slow_cd": self.slow_cd,
            "level_time": self.level_time,
            "spawn_timer": self.spawn_timer,
            "sparx_timer": self.sparx_timer,
            "qix_timer": self.qix_timer,
            "sparx": [dict(s) for s in self.sparx],
            "qix": dict(self.qix),
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
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    raise ValueError(v)
                if not math.isfinite(v) or v < lo:
                    raise ValueError(v)
                return float(v)

            def cell(v):
                if not (isinstance(v, list) and len(v) == 2):
                    raise ValueError(v)
                x, y = integer(v[0]), integer(v[1])
                if not in_bounds(x, y):
                    raise ValueError(v)
                return (x, y)

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
                grid.append([CELL_CHARS.index(ch) for ch in row])
            ring = [(x, 0) for x in range(FIELD_W)] + [(x, FIELD_H - 1) for x in range(FIELD_W)]
            ring += [(0, y) for y in range(FIELD_H)] + [(FIELD_W - 1, y) for y in range(FIELD_H)]
            if any(grid[y][x] == EMPTY for x, y in ring):
                return None
            game.grid = grid

            game.mode = data["mode"]
            if game.mode not in (None, FAST, SLOW):
                return None
            trail = [cell(c) for c in data["trail"]]
            if len(set(trail)) != len(trail):
                return None
            if any(grid[y][x] != EMPTY for x, y in trail):
                return None
            marker = cell(data["marker"])
            if trail:
                start = cell(data["trail_start"])
                if grid[start[1]][start[0]] != EDGE or game.mode is None:
                    return None
                path = [start] + trail
                for (ax, ay), (bx, by) in zip(path, path[1:]):
                    if abs(ax - bx) + abs(ay - by) != 1:
                        return None
                if marker != trail[-1]:
                    return None
                game.trail_start = start
            else:
                if data["trail_start"] is not None:
                    return None
                if grid[marker[1]][marker[0]] != EDGE:
                    return None
                game.trail_start = None
            game.trail = trail
            game.trail_set = set(trail)
            game.marker = marker

            fuse = data["fuse"]
            game.fuse = None if fuse is None else num(fuse)
            if game.fuse is not None and (not trail or game.fuse >= len(trail)):
                return None
            game.idle = num(data["idle"])
            game.slow_cd = num(data["slow_cd"])
            game.level_time = num(data["level_time"])
            game.spawn_timer = num(data["spawn_timer"])
            game.sparx_timer = num(data["sparx_timer"])
            game.qix_timer = num(data["qix_timer"])

            sparx = []
            raw_sparx = data["sparx"]
            if not isinstance(raw_sparx, list) or len(raw_sparx) > MAX_SPARX:
                return None
            for s in raw_sparx:
                x, y = cell([s["x"], s["y"]])
                if grid[y][x] != EDGE or s["dir"] not in DELTAS:
                    return None
                if not isinstance(s["cw"], bool):
                    return None
                sparx.append({"x": x, "y": y, "dir": s["dir"], "cw": s["cw"]})
            game.sparx = sparx

            rq = data["qix"]
            qix = {}
            for k in ("x", "y", "vx", "vy", "a", "va"):
                qix[k] = num(rq[k], -1e9)
            qix["len"] = integer(rq["len"], QIX_MIN_LEN)
            if qix["len"] > QIX_MAX_LEN:
                return None
            if not game._qix_open(qix["x"], qix["y"]):
                return None
            game.qix = qix
            game.qix_history = []

            state = _rng_from_json(data["rng"])
            if state is None:
                return None
            game.rng.setstate(state)
            game.phase = None
            return game
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            return None


TITLE = "Qix"

# Every key the game reacts to (upper case works too).
CONTROLS = [
    ("arrows", "move (or wasd / hjkl)"),
    ("space", "fast draw on/off"),
    ("f", "slow draw x2 on/off"),
    ("?", "help"),
    ("p / r", "pause / restart"),
    ("q", "menu"),
]

HELP = [
    ("Goal",
     "Claim 75% of the field. You claim space by drawing lines across the "
     "empty (unclaimed) area and closing them into shapes, while the Qix "
     "roams the empty space and Sparx patrol the lines."),
    ("Safe lines vs unclaimed space",
     "- Safe lines are the white - | + lines: the field's border at first, "
     "then the edges of every shape you close. Standing on one, the Qix "
     "cannot hurt you (but Sparx can).\n"
     "- Unclaimed space is the blank area inside. The Qix lives there.\n"
     "- Claimed space is shaded: blue if drawn fast, red if drawn slow."),
    ("Controls",
     "- arrows (or w a s d, or h j k l): move your marker @ one cell.\n"
     "- In MOVING mode (the default) you can only walk along safe lines.\n"
     "- space: turn FAST DRAW on or off. f: turn SLOW DRAW on or off.\n"
     "- Drawing is a toggle, not a held button: once on, it STAYS on - "
     "also after you close a shape - until you press the same key again. "
     "The marker shows reversed while a draw mode is on, and the mode is "
     "shown in the header and the stats box.\n"
     "- With a draw mode on, step off a safe line into unclaimed space to "
     "start drawing a line (the Stix, shown as #).\n"
     "- While a line is being drawn the mode is locked, except that slow "
     "can be switched up to fast (the shape then counts as fast).\n"
     "- ? help, p pause, r restart (banks your score), q back to the "
     "menu. Your game is saved when you leave and resumes paused."),
    ("Drawing and claiming",
     "- Your line may not cross or back up over itself.\n"
     "- Reach any safe line to close the shape. Your line becomes a safe "
     "line, and every part of the unclaimed space that does NOT contain "
     "the Qix is claimed (filled). The side with the Qix stays open.\n"
     "- Old safe lines no longer next to any unclaimed space turn into "
     "claimed area.\n"
     "- Fast draw: the line follows you at full speed.\n"
     "- Slow draw: the line grows at most about 8 cells a second, but the "
     "shape scores double and is shaded red."),
    ("Dangers",
     "- The Qix: the spinning multicoloured lines in the unclaimed space. "
     "If it touches any part of your unfinished line - or you draw into it "
     "- you lose a life. It cannot touch you on a safe line.\n"
     "- Sparx (x): run along the safe lines, starting from the top in "
     "both directions. One touching your marker on a line kills you; they "
     "cannot follow you off the lines. Another Sparx arrives every 25s (up "
     "to 4), and after 60s on a level they turn Super (X): red and faster.\n"
     "- The fuse: stop moving while drawing for 1.5s and a fuse (*) lights "
     "at the start of your line and burns along it (the burned part shows "
     "as .). If it catches you, you lose a life. Moving again puts it out."),
    ("Lives and levels",
     "- You have 3 lives. Losing one puts you back where your line started "
     "(if you were drawing), erases the line, turns drawing off and moves the Sparx away.\n"
     "- Claim 75% or more to clear the level. The next level starts with "
     "an empty field and a faster Qix and Sparx.\n"
     "- The area counter in the stats box (or header) shows claimed / "
     "target."),
    ("Scoring",
     "- 10 points per cell claimed (the new line's cells included), x2 "
     "for slow draw.\n"
     "- Level bonus: 1000 per whole percent claimed above 75%.\n"
     "- Your best score is kept between games."),
    ("Legend",
     "- @ = you (shown reversed while a draw mode is on)\n"
     "- white - | + = safe lines\n"
     "- blank = unclaimed space\n"
     "- shaded blue / shaded red = claimed fast / claimed slow\n"
     "- # = your line being drawn (cyan fast, red slow)\n"
     "- * on your line = the burning fuse; . = burned line\n"
     "- x = Sparx; X = Super Sparx\n"
     "- coloured / \\ - | * in the open = the Qix"),
    ("Tips",
     "- Draw small, quick shapes near the border first; big shapes are "
     "risky while the Qix is nearby.\n"
     "- Watch where the Qix is before leaving a line; close the shape on "
     "the side away from it.\n"
     "- Keep moving while drawing, or the fuse will get you.\n"
     "- Slow draw pays double - use it when the Qix is far away.\n"
     "- Remember draw mode stays on: turn it off (same key) when you only "
     "want to walk the lines."),
]


def hint_text(width: int) -> str:
    """Key hint for tiers without a controls box, at most `width` columns;
    when it must be cut, '? help' comes first so it always survives, then
    the menu/pause keys and the draw toggles, movement last."""
    full = controls_hint(CONTROLS)
    if len(full) <= width:
        return full
    first = ([c for c in CONTROLS if c[0] == "?"]
             + [c for c in reversed(CONTROLS) if c[0] != "?"])
    return controls_hint(first)[: max(0, width)]


# Adaptive layout. The popup's inner area is 93x28. Row 0 is the
# header/banner; the field (58x23, its border ring is its frame) and the
# side column (common.side_width: 30 at the popup size, down to 20) are
# centred together below it. The side column stacks stats, controls and
# agents, all as tall as the field together. Without room for a side column
# the field is centred alone and a one-line key hint sits under it (or, when
# there is no spare row, in the header).
FIELD_WIN_W = FIELD_W
FIELD_WIN_H = FIELD_H
MIN_COLS = FIELD_WIN_W
MIN_ROWS = FIELD_WIN_H + 1
STATS_H = 10          # stats box rows when there is room
STATS_MIN_H = 4
AGENTS_MIN_H = 3


def layout_plan(cols: int, rows: int) -> dict:
    """Pure layout decision: tier plus each region's (y, x, height, width).
    Tiers: 'full' (field + stats + controls + agents), 'side' (no room for
    the agents box), 'field' (score and key hint outside the field),
    'too_small'. `hint` is the key-hint row in the field tier, or None with
    `hint_in_header` set when the hint shares the header row."""
    plan = {"tier": "too_small", "too_small": True, "field": None,
            "stats": None, "controls": None, "agents": None, "hint": None,
            "hint_in_header": False}
    if cols < MIN_COLS or rows < MIN_ROWS:
        return plan
    plan["too_small"] = False
    side_w = side_width(cols, FIELD_WIN_W)
    if not side_w:
        plan["tier"] = "field"
        if rows >= FIELD_WIN_H + 2:
            y, x = center_origin(cols, rows, FIELD_WIN_W, FIELD_WIN_H + 1)
            plan["hint"] = (y + FIELD_WIN_H, 0, 1, cols)
        else:
            y, x = center_origin(cols, rows, FIELD_WIN_W, FIELD_WIN_H)
            plan["hint_in_header"] = True
        plan["field"] = (y, x, FIELD_WIN_H, FIELD_WIN_W)
        return plan
    y, x = center_origin(cols, rows, FIELD_WIN_W + 1 + side_w, FIELD_WIN_H)
    plan["field"] = (y, x, FIELD_WIN_H, FIELD_WIN_W)
    sx = x + FIELD_WIN_W + 1
    # Priority when short: controls, then stats, then agents.
    ctrl_h = min(controls_height(CONTROLS, side_w), FIELD_WIN_H)
    rest = FIELD_WIN_H - ctrl_h
    stats_h = min(STATS_H, rest)
    if stats_h < STATS_MIN_H:
        stats_h = 0
    agents_h = rest - stats_h
    if agents_h < AGENTS_MIN_H:
        stats_h, agents_h = (rest if stats_h else 0), 0
        ctrl_h = FIELD_WIN_H - stats_h
    top = y
    if stats_h:
        plan["stats"] = (top, sx, stats_h, side_w)
        top += stats_h
    plan["controls"] = (top, sx, ctrl_h, side_w)
    top += ctrl_h
    if agents_h:
        plan["agents"] = (top, sx, agents_h, side_w)
    plan["tier"] = "full" if agents_h else "side"
    return plan


def help_key(help, game, ch, paused_by_help: bool, hold: bool = False):
    """The `?` help contract for one key press. Returns (consumed,
    paused_by_help, handover). While help is open it takes every key but
    KEY_RESIZE; closing it lifts the pause it caused, unless `hold` (a
    banner is waiting), in which case `handover` is True and the caller
    keeps the game paused under the banner's pause flag. `?` opens help and
    pauses a running game."""
    if help.is_open:
        if ch == curses.KEY_RESIZE:
            return False, paused_by_help, False
        help.handle_key(ch)
        if not help.is_open and paused_by_help:
            if hold:
                return True, False, True
            game.paused = False
            return True, False, False
        return True, paused_by_help, False
    if ch == HELP_KEY:
        help.open()
        if not game.paused and not game.game_over:
            game.paused = True
            paused_by_help = True
        return True, paused_by_help, False
    return False, paused_by_help, False


# --------------------------------------------------------------------------
# curses UI
# --------------------------------------------------------------------------

# Colour pairs 40-59 are shared by the later games (one runs at a time);
# they are registered in run() on every start (see common.py).
PAIR_EDGE, PAIR_FAST, PAIR_SLOW, PAIR_TRAIL = 40, 41, 42, 43
PAIR_MARKER, PAIR_SPARX, PAIR_FUSE = 44, 45, 46
PAIR_QIX0 = 47  # 47-50 cycle the Qix colours
QIX_PAIRS = 4


def init_colors() -> None:
    bg = default_bg()
    curses.init_pair(PAIR_EDGE, curses.COLOR_WHITE, bg)
    curses.init_pair(PAIR_FAST, curses.COLOR_BLUE, bg)
    curses.init_pair(PAIR_SLOW, curses.COLOR_RED, bg)
    curses.init_pair(PAIR_TRAIL, curses.COLOR_CYAN, bg)
    curses.init_pair(PAIR_MARKER, curses.COLOR_YELLOW, bg)
    curses.init_pair(PAIR_SPARX, curses.COLOR_YELLOW, bg)
    curses.init_pair(PAIR_FUSE, curses.COLOR_RED, bg)
    for i, c in enumerate((curses.COLOR_MAGENTA, curses.COLOR_RED,
                           curses.COLOR_GREEN, curses.COLOR_CYAN)):
        curses.init_pair(PAIR_QIX0 + i, c, bg)


class Layout:
    """Screen regions for the current size; rebuilt on resize."""

    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.rows, self.cols = stdscr.getmaxyx()
        self.plan = layout_plan(self.cols, self.rows)
        self.too_small = self.plan["too_small"]
        self.field = self.stats = self.controls = self.agents = None
        if self.too_small:
            return
        for name in ("field", "stats", "controls", "agents"):
            if self.plan[name] is not None:
                y, x, h, w = self.plan[name]
                setattr(self, name, stdscr.derwin(h, w, y, x))

    def boxes(self):
        if self.field is not None:
            self.field.erase()
        for win in (self.stats, self.controls, self.agents):
            if win is not None:
                win.erase()
                frame(win)


def _edge_glyph(grid, x, y) -> str:
    def edge(nx, ny):
        return in_bounds(nx, ny) and grid[ny][nx] == EDGE
    h = edge(x - 1, y) or edge(x + 1, y)
    v = edge(x, y - 1) or edge(x, y + 1)
    if h and not v:
        return "-"
    if v and not h:
        return "|"
    return "+"


def _slope_glyph(a, b) -> str:
    dx, dy = b[0] - a[0], b[1] - a[1]
    if dy == 0:
        return "-"
    if dx == 0:
        return "|"
    return "\\" if (dx > 0) == (dy > 0) else "/"


def _qix_glyphs(body) -> list:
    out = []
    for i, c in enumerate(body):
        nb = body[i + 1] if i + 1 < len(body) else body[i - 1] if i else c
        out.append((c, _slope_glyph(c, nb) if nb != c else "*"))
    return out


def _centered(win, row, text, attr):
    safe_addstr(win, row, max(1, (FIELD_WIN_W - len(text)) // 2), text, attr)


def draw_field(win, game: Game) -> None:
    grid = game.grid
    phase = game.phase
    flash = phase is not None and int(phase.elapsed * 8) % 2 == 1
    edge_attr = curses.color_pair(PAIR_EDGE) | curses.A_BOLD
    if flash and phase.kind == "level_clear":
        edge_attr = curses.color_pair(PAIR_MARKER) | curses.A_BOLD
    fills = {FILL_FAST: ("░", curses.color_pair(PAIR_FAST)),
             FILL_SLOW: ("░", curses.color_pair(PAIR_SLOW))}
    canvas = []
    for y in range(FIELD_H):
        row = []
        for x in range(FIELD_W):
            c = grid[y][x]
            if c == EMPTY:
                row.append((" ", 0))
            elif c == EDGE:
                row.append((_edge_glyph(grid, x, y), edge_attr))
            else:
                row.append(fills[c])
        canvas.append(row)

    def put(c, ch, attr):
        x, y = c
        if in_bounds(x, y):
            canvas[y][x] = (ch, attr)

    # Qix: fading history, then the live body in a cycling colour.
    tick = int(game.level_time * 6)
    for body in game.qix_history[:-1]:
        for c, ch in _qix_glyphs(body):
            if grid[c[1]][c[0]] == EMPTY and c not in game.trail_set:
                put(c, ch, curses.color_pair(PAIR_QIX0 + tick % QIX_PAIRS) | curses.A_DIM)
    body = game.qix_body()[0]
    for c, ch in _qix_glyphs(body):
        put(c, ch, curses.color_pair(PAIR_QIX0 + tick % QIX_PAIRS) | curses.A_BOLD)

    trail_attr = curses.color_pair(PAIR_SLOW if game.mode == SLOW else PAIR_TRAIL)
    dying = phase is not None and phase.kind == "dying"
    if not (dying and flash):
        burned = int(game.fuse) if game.fuse is not None else 0
        for i, c in enumerate(game.trail):
            if i < burned:
                put(c, ".", curses.A_DIM)
            else:
                put(c, "#", trail_attr | curses.A_BOLD)
        if game.fuse is not None and burned < len(game.trail):
            put(game.trail[burned], "*", curses.color_pair(PAIR_FUSE) | curses.A_BOLD)

    for s in game.sparx:
        super_ = game.is_super()
        put((s["x"], s["y"]), "X" if super_ else "x",
            curses.color_pair(PAIR_FUSE if super_ else PAIR_SPARX) | curses.A_BOLD)

    if not game.game_over and not (dying and flash):
        mattr = curses.color_pair(PAIR_FUSE if dying else PAIR_MARKER) | curses.A_BOLD
        if game.mode is not None:
            mattr |= curses.A_REVERSE
        put(game.marker, "@", mattr)

    for y, row in enumerate(canvas):
        x = 0
        while x < FIELD_W:
            ch, attr = row[x]
            end = x + 1
            while end < FIELD_W and row[end][1] == attr:
                end += 1
            safe_addstr(win, y, x, "".join(r[0] for r in row[x:end]), attr)
            x = end

    mid = FIELD_H // 2
    if game.game_over:
        _centered(win, mid - 1, " GAME OVER ", curses.A_STANDOUT | curses.A_BOLD)
        _centered(win, mid + 1, " r restart | q menu | ? help ", curses.A_STANDOUT)
    elif game.paused:
        _centered(win, mid - 1, " PAUSED ", curses.A_STANDOUT | curses.A_BOLD)
        _centered(win, mid + 1, " p resume | ? help ", curses.A_STANDOUT)
    elif phase is not None and phase.kind == "level_clear":
        _centered(win, mid - 1, f" LEVEL {game.level + 1} ",
                  curses.A_STANDOUT | curses.A_BOLD)
        if game.last_bonus:
            _centered(win, mid + 1, f" bonus {game.last_bonus} ", curses.A_STANDOUT)


MODE_LABELS = {None: "MOVING", FAST: "FAST DRAW", SLOW: "SLOW DRAW x2"}


def _mode_text(game: Game) -> str:
    return MODE_LABELS[game.mode]


def _mode_attr(game: Game) -> int:
    """Reverse-video colour for the mode tag: white moving, cyan fast (the
    trail colour), red slow."""
    pair = {None: PAIR_EDGE, FAST: PAIR_TRAIL, SLOW: PAIR_SLOW}[game.mode]
    return curses.color_pair(pair) | curses.A_REVERSE | curses.A_BOLD


def _mode_hint(game: Game) -> str:
    if game.mode is None:
        return "space/f: draw"
    if game.trail:
        return "reach a line!"
    return "step off to draw"


def _state_text(game: Game) -> str:
    if game.game_over:
        return "GAME OVER"
    if game.paused:
        return "paused"
    if game.phase is not None:
        return "ouch!" if game.phase.kind == "dying" else "level clear!"
    if game.fuse is not None:
        return "FUSE LIT - MOVE!"
    return ""


def _area_bar(game: Game, width: int) -> str:
    """[####....|...] claimed-area bar with the target marked by '|'."""
    inner = max(1, width - 2)
    filled = int(game.claimed_percent() * inner / 100)
    target = min(inner - 1, int(CLAIM_TARGET * inner / 100))
    cells = ["#" if i < filled else ("|" if i == target else ".")
             for i in range(inner)]
    return "[" + "".join(cells) + "]"


def draw_stats(win, game: Game, best: int) -> None:
    rows, cols = win.getmaxyx()
    inner = cols - 2
    safe_addstr(win, 0, 2, f" {TITLE} "[: max(0, cols - 3)], curses.A_BOLD)
    if game.is_super():
        sparx = (f"SUPER SPARX {len(game.sparx)}",
                 curses.color_pair(PAIR_FUSE) | curses.A_BOLD)
    else:
        sparx = (f"sparx {len(game.sparx)}", curses.color_pair(PAIR_SPARX))
    state = _state_text(game)
    alert = curses.A_BOLD
    if game.fuse is not None and not game.paused:
        alert |= curses.color_pair(PAIR_FUSE) | curses.A_REVERSE
    lines = [
        (f"score {game.score}", curses.A_BOLD),
        (f"best  {max(best, game.score)}", 0),
        (f"level {game.level}   lives {game.lives}", curses.color_pair(PAIR_MARKER)),
        (f"area  {int(game.claimed_percent())}% / {CLAIM_TARGET}%",
         curses.color_pair(PAIR_FAST)),
        (_area_bar(game, inner), curses.color_pair(PAIR_FAST)),
        sparx,
        (_mode_text(game).center(inner), _mode_attr(game)),
        # A state worth shouting (fuse, pause, ...) replaces the mode hint.
        (state, alert) if state else (_mode_hint(game), curses.A_DIM),
    ]
    for i, (line, attr) in enumerate(lines[: max(0, rows - 2)]):
        if line:
            safe_addstr(win, 1 + i, 1, line[:inner], attr)


def header_parts(game: Game, plan: dict, cols: int):
    """Row-0 text and where the mode tag sits in it: (text, tag_x, tag).
    The mode is shown in every tier; the field tier adds the score and,
    when the key hint has no row of its own, the hint ('? help' kept)."""
    width = max(0, cols - 1)
    prefix = f" {TITLE.lower()} "
    tag = f" {_mode_text(game)} "
    text = prefix + tag
    state = _state_text(game)
    if state:
        text += f" {state}"
    text += " "
    if plan["stats"] is None:  # no stats box in this tier: score goes here
        text += (f"| score {game.score} | lives {game.lives} | lvl {game.level}"
                 f" | {int(game.claimed_percent())}%/{CLAIM_TARGET}% ")
    if plan["hint_in_header"]:
        room = width - len(text) - 2
        if room >= len("? help"):
            text += "| " + hint_text(room)
        else:
            text = text[: max(0, width - 9)] + " | ? help"
    return text[:width], len(prefix), tag


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
    else:
        if not draw_banner(scr, lay.cols, session.attention, resume):
            text, tag_x, tag = header_parts(game, lay.plan, lay.cols)
            safe_addstr(scr, 0, 0, text,
                        curses.A_STANDOUT | (curses.A_BOLD if game.game_over else 0))
            if not game.game_over and tag_x + len(tag) <= len(text):
                safe_addstr(scr, 0, tag_x, tag, _mode_attr(game))
        lay.boxes()
        draw_field(lay.field, game)
        if lay.stats is not None:
            draw_stats(lay.stats, game, best)
        if lay.controls is not None:
            draw_controls(lay.controls, CONTROLS)
        if lay.agents is not None:
            draw_agents(lay.agents, session.roster, session.status_pairs)
        if lay.plan["hint"] is not None:
            y, _, _, w = lay.plan["hint"]
            text = hint_text(w - 1)
            safe_addstr(scr, y, max(0, (w - 1 - len(text)) // 2), text, curses.A_DIM)
    if help is not None and help.is_open:
        help.draw(scr)


KEY_DIRS = {
    curses.KEY_UP: UP, curses.KEY_DOWN: DOWN,
    curses.KEY_LEFT: LEFT, curses.KEY_RIGHT: RIGHT,
}
for _keys, _d in (("kKwW", UP), ("jJsS", DOWN), ("hHaA", LEFT), ("lLdD", RIGHT)):
    for _k in _keys:
        KEY_DIRS[ord(_k)] = _d


def run(session) -> None:
    """Play Qix until `q` (back to the arcade menu) or SIGTERM. The game is
    saved on every exit path and resumed, paused, on the next run."""
    scr = session.stdscr
    init_colors()
    game_path = session.path("qix-game.json")
    hiscore_path = session.path("qix-hiscore.json")
    hiscore = load_hiscore(hiscore_path)

    # A saved, unfinished game resumes exactly where it left off -- paused,
    # behind a banner. Corrupt/incompatible saves are ignored silently.
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
    help = HelpOverlay(TITLE, HELP)
    paused_by_attention = False
    paused_by_help = False
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
                    paused_by_attention = True
                dirty = True

            quit_now = False
            while True:
                ch = scr.getch()
                if ch == -1:
                    break
                dirty = True
                # Help first: while open it takes every key (it paused the
                # game); a banner still waiting keeps the pause on close.
                consumed, paused_by_help, handover = help_key(
                    help, game, ch, paused_by_help,
                    hold=session.attention is not None or resume)
                if handover:
                    if resume:
                        paused_by_resume = True
                    else:
                        paused_by_attention = True
                if consumed:
                    continue
                if ch == curses.KEY_RESIZE:
                    curses.update_lines_cols()
                    scr.erase()
                    lay = Layout(scr)
                elif session.attention is not None or resume:
                    # Any key clears the banner and lifts only the pauses
                    # the banner itself caused; the key is consumed.
                    session.dismiss()
                    resume = False
                    if paused_by_attention or paused_by_resume:
                        game.paused = False
                    paused_by_attention = paused_by_resume = False
                elif ch in (ord("q"), ord("Q")):
                    quit_now = True
                    break
                elif ch in (ord("r"), ord("R")):
                    # Restart abandons the saved game but banks the score.
                    if game.score > hiscore:
                        hiscore = game.score
                        save_hiscore(hiscore_path, hiscore)
                    if game_path:
                        remove_path(game_path)
                    game.reset()
                    game_over_handled = False
                    paused_by_attention = paused_by_resume = False
                    paused_by_help = False
                elif ch in (ord("p"), ord("P")):
                    if not game.game_over:
                        game.paused = not game.paused
                        paused_by_attention = paused_by_resume = False
                        paused_by_help = False
                elif ch == ord(" "):
                    game.toggle_draw(FAST)
                elif ch in (ord("f"), ord("F")):
                    game.toggle_draw(SLOW)
                elif ch in KEY_DIRS:
                    game.move(KEY_DIRS[ch])
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
                draw(game, lay, session, resume, hiscore, help)
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

    sys.exit(arcade.main(["--game", "qix"] + sys.argv[1:]))
