#!/usr/bin/env python3
"""Bomberman for the Herdr arcade (Hudson Soft 1983; see arcade.py, which
runs it via `run()`).

A walled grid with indestructible pillars on every other cell and soft
blocks scattered between them. Drop bombs: after a short fuse each one
bursts into a cross of flames that stops at pillars, burns the first soft
block it reaches, sets off other bombs and kills whatever stands in it -
monsters and you alike. Under the soft blocks hide a power-up and the exit;
the exit opens once every monster on the stage is dead. Bombing an exposed
exit or power-up lets loose a pack of extra monsters, as in the original,
and when the stage timer runs out fast ghosts flood in.

Structure: everything above the "curses UI" divider is pure game logic -
stage generation, movement, bombs and flames, monster AI, scoring, lives,
stages, save/restore and the layout plan - with no terminal dependency, so
`tests/test_bomberman.py` imports this module without a terminal.
"""

from __future__ import annotations

import curses
import math
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

# The frame is the outer wall; inside it pillars stand on every cell with an
# odd x and an odd y, so both dimensions are odd and the outer ring is open.
FIELD_W = 29  # cells; each cell is two terminal columns
FIELD_H = 15

EMPTY, PILLAR, SOFT, BURN = ".", "#", "s", "%"
GRID_CHARS = (EMPTY, PILLAR, SOFT, BURN)

UP, DOWN, LEFT, RIGHT = "up", "down", "left", "right"
DELTAS = {UP: (0, -1), DOWN: (0, 1), LEFT: (-1, 0), RIGHT: (1, 0)}
DIRS = (UP, DOWN, LEFT, RIGHT)
OPPOSITE = {UP: DOWN, DOWN: UP, LEFT: RIGHT, RIGHT: LEFT}

START = (0, 0)
# The start corner stays clear of soft blocks so the first bomb has an
# escape route: the corner plus two cells along each wall.
SAFE_CELLS = frozenset({(0, 0), (1, 0), (2, 0), (0, 1), (0, 2)})
ENEMY_MIN_DIST = 8          # monsters start at least this far from START

LIVES = 3
FUSE = 2.5                  # seconds from drop to blast
FLAME_TIME = 0.5            # flames (and burning blocks) last this long
MAX_BOMBS = 8
MAX_FLAME = 8
PLAYER_INTERVALS = (0.16, 0.12, 0.09)   # seconds per step by speed level
MAX_SPEED = len(PLAYER_INTERVALS) - 1
STAGE_TIME = 200.0
TIMEOUT_WAVE = 4            # reapers that appear when the timer hits 0
FLOOD_EVERY = 10.0          # then one more every this many seconds ...
FLOOD_MAX = 10              # ... while fewer than this many are out
ITEM_SPAWN = 6              # monsters let loose by bombing the exit/power-up
SPAWN_DELAY = FLAME_TIME + 0.05
STAGE_START_TIME = 1.5
DEATH_TIME = 1.5
STAGE_CLEAR_TIME = 2.0
MAX_STEP = 0.02             # update() integrates in slices no longer than this

POWERUP_POINTS = 1000
MAX_MULT = 8                # blast multiplier cap: x1, x2, x4, x8

SAVE_VERSION = 1

# Monster types, weakest first. interval: seconds per step; chase: Manhattan
# radius within which it steers toward you (0: never); smart: chance a
# chasing step is the greedy one; turn: chance a wandering step changes
# direction at random; wall: passes through soft blocks.
ENEMY_ORDER = ("drifter", "darter", "hunter", "phantom", "sprinter", "reaper")
ENEMY_TYPES = {
    "drifter": {"interval": 0.75, "points": 100, "chase": 0, "smart": 0.0,
                "turn": 0.10, "wall": False},
    "darter": {"interval": 0.45, "points": 200, "chase": 0, "smart": 0.0,
               "turn": 0.30, "wall": False},
    "hunter": {"interval": 0.60, "points": 400, "chase": 6, "smart": 0.8,
               "turn": 0.20, "wall": False},
    "phantom": {"interval": 0.70, "points": 800, "chase": 8, "smart": 0.7,
                "turn": 0.20, "wall": True},
    "sprinter": {"interval": 0.35, "points": 1000, "chase": 6, "smart": 0.8,
                 "turn": 0.25, "wall": False},
    "reaper": {"interval": 0.30, "points": 2000, "chase": 99, "smart": 0.9,
               "turn": 0.10, "wall": True},
}

# Power-ups. One hides under a soft block on every stage, following this
# cycle; remote, bomb-pass and wall-pass are lost with a life.
POWERUPS = ("bomb", "flame", "speed", "remote", "bombpass", "wallpass")
SPECIAL_POWERUPS = ("remote", "bombpass", "wallpass")
POWERUP_CYCLE = ("flame", "bomb", "speed", "bomb", "flame", "remote",
                 "bomb", "flame", "bombpass", "speed", "wallpass", "remote")

# Monster line-ups for the first stages (own design); later stages follow
# stage_roster()'s formula.
STAGE_ROSTERS = {
    1: ("drifter",) * 5,
    2: ("drifter",) * 3 + ("darter",) * 2,
    3: ("drifter",) * 2 + ("darter",) * 2 + ("hunter",) * 2,
    4: ("darter",) * 3 + ("hunter",) * 3,
    5: ("darter",) * 2 + ("hunter",) * 2 + ("phantom",) * 2,
    6: ("hunter",) * 3 + ("phantom",) * 3,
    7: ("hunter",) * 2 + ("phantom",) * 2 + ("sprinter",) * 2,
}


def stage_roster(stage: int) -> tuple:
    """Monster types starting `stage`, weakest first."""
    if stage in STAGE_ROSTERS:
        return STAGE_ROSTERS[stage]
    n = min(7 + (stage - 8) // 3, 10)
    mix = ("sprinter", "phantom", "hunter", "darter")
    kinds = [mix[i % len(mix)] for i in range(n)]
    return tuple(sorted(kinds, key=ENEMY_ORDER.index))


def spawn_kind(stage: int) -> str:
    """What a bombed exit or power-up lets loose: one type above the
    stage's toughest, up to sprinters."""
    top = max(ENEMY_ORDER.index(k) for k in stage_roster(stage))
    return ENEMY_ORDER[min(top + 1, ENEMY_ORDER.index("sprinter"))]


def stage_powerup(stage: int) -> str:
    return POWERUP_CYCLE[(stage - 1) % len(POWERUP_CYCLE)]


def soft_density(stage: int) -> float:
    return min(0.30 + 0.01 * (stage - 1), 0.40)


def multiplier(kills: int) -> int:
    """Score multiplier for the `kills`-th monster killed by one blast."""
    return min(2 ** (kills - 1), MAX_MULT)


def in_bounds(x: int, y: int) -> bool:
    return 0 <= x < FIELD_W and 0 <= y < FIELD_H


def is_pillar_cell(x: int, y: int) -> bool:
    return x % 2 == 1 and y % 2 == 1


def manhattan(a, b) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def base_grid() -> list:
    """Empty field with the pillar lattice. grid[y][x]."""
    return [[PILLAR if is_pillar_cell(x, y) else EMPTY for x in range(FIELD_W)]
            for y in range(FIELD_H)]


def generate_stage(rng: random.Random, stage: int):
    """(grid, enemies, powerups, exit) for a fresh stage: monsters on open
    cells far from the start, soft blocks scattered over the rest (never in
    the start corner), and the exit and the stage's power-up each under a
    soft block of their own."""
    grid = base_grid()
    free = [(x, y) for y in range(FIELD_H) for x in range(FIELD_W)
            if grid[y][x] == EMPTY and (x, y) not in SAFE_CELLS]
    far = [c for c in free if manhattan(c, START) >= ENEMY_MIN_DIST]
    roster = stage_roster(stage)
    cells = rng.sample(far, len(roster))
    enemies = [Enemy(x, y, kind, rng.choice(DIRS)) for (x, y), kind in zip(cells, roster)]
    taken = set(cells)
    density = soft_density(stage)
    for x, y in free:
        if (x, y) not in taken and rng.random() < density:
            grid[y][x] = SOFT
    soft = [(x, y) for y in range(FIELD_H) for x in range(FIELD_W) if grid[y][x] == SOFT]
    while len(soft) < 2:  # vanishingly unlikely; keeps the pick below valid
        c = rng.choice([c for c in free if c not in taken and grid[c[1]][c[0]] == EMPTY])
        grid[c[1]][c[0]] = SOFT
        soft = sorted(soft + [c], key=lambda p: (p[1], p[0]))
    exit_cell, item_cell = rng.sample(soft, 2)
    return grid, enemies, {item_cell: stage_powerup(stage)}, exit_cell


class Phase:
    """A transient animation as an explicit logic state: 'stage_start',
    'dying' or 'stage_clear'. The state change happens when it completes -
    via update() or resolve_phases()."""

    __slots__ = ("kind", "duration", "elapsed")

    def __init__(self, kind: str, duration: float):
        self.kind = kind
        self.duration = duration
        self.elapsed = 0.0


class Enemy:
    __slots__ = ("x", "y", "kind", "dir", "timer")

    def __init__(self, x: int, y: int, kind: str, d: str = LEFT, timer: float = 0.0):
        self.x, self.y, self.kind, self.dir, self.timer = x, y, kind, d, timer

    @property
    def pos(self):
        return (self.x, self.y)

    @property
    def spec(self) -> dict:
        return ENEMY_TYPES[self.kind]


class Bomb:
    __slots__ = ("x", "y", "fuse", "remote")

    def __init__(self, x: int, y: int, fuse: float = FUSE, remote: bool = False):
        self.x, self.y, self.fuse, self.remote = x, y, fuse, remote

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
        self.stage = 1
        self.lives = LIVES
        self.paused = False
        self.game_over = False
        self.phase: Phase | None = None
        # Power-ups carried from stage to stage.
        self.max_bombs = 1
        self.flame = 1
        self.speed = 0
        self.remote = False
        self.bombpass = False
        self.wallpass = False
        # Cosmetic, never saved.
        self.message = None         # [text, seconds left]
        self._start_stage()

    def reset(self) -> None:
        self.__init__(self.rng)

    # -- stage setup -------------------------------------------------------

    def _start_stage(self) -> None:
        grid, enemies, powerups, exit_cell = generate_stage(self.rng, self.stage)
        self.grid = grid
        self.enemies = enemies
        self.powerups = powerups        # {(x, y): kind}, hidden until exposed
        self.exit = exit_cell
        self.player = START
        self.move_cd = 0.0
        self.queued = None
        self.bombs: list = []
        self.flames: dict = {}          # {(x, y): [ttl, shape, group]}
        self.groups: dict = {}          # {group id: monsters killed}
        self.next_group = 1
        self.pending: list = []         # [x, y, count, kind, delay]
        self.time_left = STAGE_TIME
        self.timeout = False
        self.flood_timer = FLOOD_EVERY
        self.message = None
        self.phase = Phase("stage_start", STAGE_START_TIME)

    # -- queries -----------------------------------------------------------

    def cell(self, x: int, y: int) -> str:
        return self.grid[y][x]

    def bomb_at(self, pos):
        for bomb in self.bombs:
            if bomb.pos == pos:
                return bomb
        return None

    def enemy_at(self, pos):
        for e in self.enemies:
            if e.pos == pos:
                return e
        return None

    def exposed(self, pos) -> bool:
        return self.grid[pos[1]][pos[0]] == EMPTY

    def exit_open(self) -> bool:
        return not self.enemies and not self.pending

    def remaining(self) -> int:
        """Monsters left this stage, including ones about to appear."""
        return len(self.enemies) + sum(p[2] for p in self.pending)

    def step_interval(self) -> float:
        return PLAYER_INTERVALS[min(self.speed, MAX_SPEED)]

    def _can_act(self) -> bool:
        return not (self.paused or self.game_over or self.phase is not None)

    # -- the player ----------------------------------------------------------

    def player_can_enter(self, pos) -> bool:
        x, y = pos
        if not in_bounds(x, y):
            return False
        c = self.grid[y][x]
        if c == PILLAR:
            return False
        if c in (SOFT, BURN) and not self.wallpass:
            return False
        if self.bomb_at(pos) is not None and not self.bombpass:
            return False
        return True

    def move(self, d: str) -> bool:
        """One step in direction `d` (a keypress). Steps are rate-limited by
        the speed level: a key pressed while the last step is still in
        progress is buffered and taken as soon as it may be. True if the
        player moved now."""
        if d not in DELTAS or not self._can_act():
            return False
        if self.move_cd > 0:
            self.queued = d
            return False
        return self._step_player(d)

    def _step_player(self, d: str) -> bool:
        dx, dy = DELTAS[d]
        target = (self.player[0] + dx, self.player[1] + dy)
        if not self.player_can_enter(target):
            return False
        self.player = target
        self.move_cd = self.step_interval()
        if target in self.powerups and self.exposed(target):
            self._collect(self.powerups.pop(target))
        self._check_player()
        return True

    def _collect(self, kind: str) -> None:
        self.score += POWERUP_POINTS
        if kind == "bomb":
            self.max_bombs = min(MAX_BOMBS, self.max_bombs + 1)
        elif kind == "flame":
            self.flame = min(MAX_FLAME, self.flame + 1)
        elif kind == "speed":
            self.speed = min(MAX_SPEED, self.speed + 1)
        else:
            setattr(self, kind, True)
        self.message = [POWERUP_NAMES[kind] + "!", 1.5]

    def drop_bomb(self) -> bool:
        """Drop a bomb on the player's cell, if one is free and the cell has
        none. With the remote detonator it waits for detonate()."""
        if not self._can_act():
            return False
        if len(self.bombs) >= self.max_bombs or self.bomb_at(self.player) is not None:
            return False
        x, y = self.player
        self.bombs.append(Bomb(x, y, FUSE, self.remote))
        return True

    def detonate(self) -> bool:
        """Remote detonator: set off the oldest remote-controlled bomb."""
        if not self._can_act():
            return False
        for bomb in self.bombs:
            if bomb.remote:
                self._explode(bomb)
                return True
        return False

    def _check_player(self) -> None:
        """Flames or a monster on the player's cell kill; the open exit
        clears the stage."""
        if self.phase is not None or self.game_over:
            return
        if self.player in self.flames or self.enemy_at(self.player) is not None:
            self._die()
        elif (self.player == self.exit and self.exposed(self.exit)
              and self.exit_open()):
            self.phase = Phase("stage_clear", STAGE_CLEAR_TIME)

    def _die(self) -> None:
        if self.phase is not None:
            return
        self.lives -= 1
        self.queued = None
        self.phase = Phase("dying", DEATH_TIME)

    def _finish_phase(self, intro: bool) -> None:
        """Complete the current phase. A new or restarted stage opens with
        the 'STAGE n' intro when `intro` is set (live play), straight into
        play otherwise (resolving for a save)."""
        phase, self.phase = self.phase, None
        if phase is None:
            return
        if phase.kind == "dying":
            if self.lives <= 0:
                self.lives = 0
                self.game_over = True
                return
            # As in the original: the stage starts over and the special
            # power-ups are lost; bombs, flame and speed stay.
            self.remote = self.bombpass = self.wallpass = False
            self._start_stage()
        elif phase.kind == "stage_clear":
            self.stage += 1
            self._start_stage()
        else:
            return
        if not intro:
            self.phase = None

    def resolve_phases(self) -> None:
        """Finish any pending transition immediately (pause/quit/save). The
        resulting state is exactly the one the animation would end in."""
        while self.phase is not None:
            self._finish_phase(intro=False)

    # -- bombs and flames ----------------------------------------------------

    def _explode(self, first) -> None:
        """Set off `first` and every bomb its flames reach, as one blast
        (one score multiplier). Each arm of the cross runs up to the flame
        range, stops at pillars, burns the first soft block it meets and
        sets off the bombs it reaches."""
        group = self.next_group
        self.next_group += 1
        self.groups[group] = 0
        todo = [first]
        self.bombs.remove(first)
        hit_items = []
        burned = set()
        while todo:
            bomb = todo.pop(0)
            self._flame_at(bomb.pos, "c", group)
            bx, by = bomb.pos
            if self.grid[by][bx] in (SOFT, BURN):  # dropped inside by wall-pass
                self.grid[by][bx] = BURN
                burned.add(bomb.pos)
            elif bomb.pos == self.exit or bomb.pos in self.powerups:
                hit_items.append(bomb.pos)
            for d in DIRS:
                dx, dy = DELTAS[d]
                shape = "h" if dx else "v"
                x, y = bomb.x, bomb.y
                for _ in range(self.flame):
                    x, y = x + dx, y + dy
                    if not in_bounds(x, y):
                        break
                    c = self.grid[y][x]
                    if c == PILLAR:
                        break
                    if c in (SOFT, BURN):
                        self.grid[y][x] = BURN
                        burned.add((x, y))
                        self._flame_at((x, y), shape, group)
                        break
                    other = self.bomb_at((x, y))
                    if other is not None:
                        self.bombs.remove(other)
                        todo.append(other)
                        break  # its own blast carries on from there
                    self._flame_at((x, y), shape, group)
                    if (x, y) not in burned and ((x, y) == self.exit
                                                 or (x, y) in self.powerups):
                        hit_items.append((x, y))
        for pos in sorted(set(hit_items)):
            if pos in self.powerups:
                del self.powerups[pos]  # a bombed power-up is gone for good
            self.pending.append([pos[0], pos[1], ITEM_SPAWN,
                                 spawn_kind(self.stage), SPAWN_DELAY])
            self.message = ["uh-oh!", 1.5]
        for e in list(self.enemies):
            if e.pos in self.flames:
                self._kill_enemy(e, self.flames[e.pos][2])
        self._check_player()

    def _flame_at(self, pos, shape: str, group: int) -> None:
        old = self.flames.get(pos)
        if old is not None and old[1] != shape:
            shape = "c"
        self.flames[pos] = [FLAME_TIME, shape, group]

    def _kill_enemy(self, e, group: int) -> None:
        self.enemies.remove(e)
        kills = self.groups.get(group, 0) + 1
        self.groups[group] = kills
        mult = multiplier(kills)
        points = e.spec["points"] * mult
        self.score += points
        self.message = [f"+{points}" + (f" x{mult}" if mult > 1 else ""), 1.0]

    # -- monsters --------------------------------------------------------------

    def enemy_can_enter(self, e, pos) -> bool:
        x, y = pos
        if not in_bounds(x, y):
            return False
        c = self.grid[y][x]
        if c == PILLAR or (c in (SOFT, BURN) and not e.spec["wall"]):
            return False
        return self.bomb_at(pos) is None

    def _enemy_step(self, e) -> None:
        """One step: toward the player when chasing, else keep going and
        turn now and then (always when blocked). Walking into flames kills
        it; walking into the player kills the player."""
        spec = e.spec
        options = []
        for d in DIRS:
            dx, dy = DELTAS[d]
            n = (e.x + dx, e.y + dy)
            if self.enemy_can_enter(e, n):
                options.append((d, n))
        if not options:
            return
        chasing = (spec["chase"] and manhattan(e.pos, self.player) <= spec["chase"]
                   and self.rng.random() < spec["smart"])
        if chasing:
            best = min(manhattan(n, self.player) for _, n in options)
            d, n = self.rng.choice([o for o in options if manhattan(o[1], self.player) == best])
        else:
            ahead = [o for o in options if o[0] == e.dir]
            if ahead and self.rng.random() >= spec["turn"]:
                d, n = ahead[0]
            else:
                fresh = [o for o in options if o[0] != OPPOSITE[e.dir]] or options
                d, n = self.rng.choice(fresh)
        e.dir = d
        e.x, e.y = n
        if n in self.flames:
            self._kill_enemy(e, self.flames[n][2])
        elif n == self.player:
            self._die()

    def _spawn(self, x: int, y: int, kind: str) -> None:
        self.enemies.append(Enemy(x, y, kind, self.rng.choice(DIRS)))

    def _spawn_far(self, kind: str) -> None:
        """A monster on an open cell well away from the player."""
        occupied = {e.pos for e in self.enemies} | {b.pos for b in self.bombs}
        opens = [(x, y) for y in range(FIELD_H) for x in range(FIELD_W)
                 if self.grid[y][x] == EMPTY and (x, y) not in occupied
                 and (x, y) not in self.flames and (x, y) != self.player]
        far = [c for c in opens if manhattan(c, self.player) >= ENEMY_MIN_DIST]
        pool = far or [c for c in opens if manhattan(c, self.player) >= 3]
        if pool:
            x, y = self.rng.choice(pool)
            self._spawn(x, y, kind)

    def _timeout(self) -> None:
        self.timeout = True
        self.flood_timer = FLOOD_EVERY
        for _ in range(TIMEOUT_WAVE):
            self._spawn_far("reaper")
        self.message = ["TIME UP!", 2.0]

    # -- time ------------------------------------------------------------------

    def update(self, dt: float) -> bool:
        """Advance by `dt` seconds. True if anything visible changed."""
        if self.paused or self.game_over:
            return False
        changed = self._tick_effects(dt)
        if self.phase is not None:
            self.phase.elapsed += dt
            if self.phase.elapsed >= self.phase.duration:
                self._finish_phase(intro=True)
            return True
        left = dt
        while left > 1e-9 and self.phase is None:
            step = min(MAX_STEP, left)
            left -= step
            changed = self._tick(step) or changed
        return changed

    def _tick(self, step: float) -> bool:
        changed = False
        # Stage timer, then the flood of reapers.
        if not self.timeout:
            self.time_left = max(0.0, self.time_left - step)
            if self.time_left <= 0:
                self._timeout()
                changed = True
        else:
            self.flood_timer -= step
            if self.flood_timer <= 0:
                self.flood_timer = FLOOD_EVERY
                if sum(e.kind == "reaper" for e in self.enemies) < FLOOD_MAX:
                    self._spawn_far("reaper")
                    changed = True
        # The player's step in progress, then a buffered one.
        if self.move_cd > 0:
            self.move_cd = max(0.0, self.move_cd - step)
            if self.move_cd == 0 and self.queued is not None:
                d, self.queued = self.queued, None
                changed = self._step_player(d) or changed
        # Fuses.
        for bomb in self.bombs:
            if not bomb.remote:
                bomb.fuse -= step
        for bomb in list(self.bombs):
            if bomb in self.bombs and not bomb.remote and bomb.fuse <= 0:
                self._explode(bomb)
                changed = True
        # Flames die down; burnt blocks clear and reveal what they hid.
        for pos in sorted(self.flames):
            f = self.flames[pos]
            f[0] -= step
            if f[0] <= 0:
                del self.flames[pos]
                if self.grid[pos[1]][pos[0]] == BURN:
                    self.grid[pos[1]][pos[0]] = EMPTY
                changed = True
        live = {f[2] for f in self.flames.values()}
        for g in [g for g in self.groups if g not in live]:
            del self.groups[g]
        # Monsters let loose from a bombed exit or power-up.
        for p in list(self.pending):
            p[4] -= step
            if p[4] <= 0:
                self.pending.remove(p)
                for _ in range(p[2]):
                    self._spawn(p[0], p[1], p[3])
                changed = True
        # Monsters.
        for e in list(self.enemies):
            if e not in self.enemies:
                continue
            e.timer += step
            interval = e.spec["interval"]
            if e.timer >= interval:
                e.timer -= interval
                self._enemy_step(e)
                changed = True
                if self.phase is not None:
                    break
        self._check_player()
        if self.phase is not None:
            changed = True
        return changed

    def _tick_effects(self, dt: float) -> bool:
        if self.message is None:
            return False
        self.message[1] -= dt
        if self.message[1] <= 0:
            self.message = None
        return True

    # -- save / restore --------------------------------------------------------

    def to_save(self) -> dict:
        """Serializable snapshot. The caller resolves phases first (the UI
        save path does), so a save never holds a transition mid-flight."""
        return {
            "version": SAVE_VERSION,
            "score": self.score,
            "stage": self.stage,
            "lives": self.lives,
            "game_over": self.game_over,
            "max_bombs": self.max_bombs,
            "flame": self.flame,
            "speed": self.speed,
            "remote": self.remote,
            "bombpass": self.bombpass,
            "wallpass": self.wallpass,
            "grid": ["".join(row) for row in self.grid],
            "exit": list(self.exit),
            "powerups": [[x, y, k] for (x, y), k in sorted(self.powerups.items())],
            "player": list(self.player),
            "move_cd": self.move_cd,
            "queued": self.queued,
            "bombs": [[b.x, b.y, b.fuse, b.remote] for b in self.bombs],
            "flames": [[x, y, f[0], f[1], f[2]] for (x, y), f in sorted(self.flames.items())],
            "groups": [[g, n] for g, n in sorted(self.groups.items())],
            "next_group": self.next_group,
            "pending": [list(p) for p in self.pending],
            "enemies": [[e.x, e.y, e.kind, e.dir, e.timer] for e in self.enemies],
            "time_left": self.time_left,
            "timeout": self.timeout,
            "flood_timer": self.flood_timer,
            "rng": _rng_to_json(self.rng),
        }

    @classmethod
    def from_save(cls, data, rng=None):
        """Rebuild a saved game, or None for anything corrupt, incompatible,
        or not exactly a save this version wrote."""
        try:
            if not isinstance(data, dict) or data.get("version") != SAVE_VERSION:
                return None

            def integer(v, lo=0, hi=None):
                if isinstance(v, bool) or not isinstance(v, int) or v < lo:
                    raise ValueError(v)
                if hi is not None and v > hi:
                    raise ValueError(v)
                return v

            def num(v, lo=0.0):
                if isinstance(v, bool) or not isinstance(v, (int, float)) or v < lo:
                    raise ValueError(v)
                if math.isnan(v) or math.isinf(v):
                    raise ValueError(v)
                return float(v)

            def flag(v):
                if not isinstance(v, bool):
                    raise ValueError(v)
                return v

            def cell(v):
                if not (isinstance(v, list) and len(v) >= 2):
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
            game.stage = integer(data["stage"], 1)
            game.lives = integer(data["lives"], 1)
            game.game_over = flag(data["game_over"])
            game.max_bombs = integer(data["max_bombs"], 1, MAX_BOMBS)
            game.flame = integer(data["flame"], 1, MAX_FLAME)
            game.speed = integer(data["speed"], 0, MAX_SPEED)
            game.remote = flag(data["remote"])
            game.bombpass = flag(data["bombpass"])
            game.wallpass = flag(data["wallpass"])

            rows = data["grid"]
            if not isinstance(rows, list) or len(rows) != FIELD_H:
                return None
            grid = []
            for y, row in enumerate(rows):
                if not isinstance(row, str) or len(row) != FIELD_W:
                    return None
                for x, c in enumerate(row):
                    if c not in GRID_CHARS or (c == PILLAR) != is_pillar_cell(x, y):
                        return None
                grid.append(list(row))
            game.grid = grid

            def open_cell(v):
                x, y = cell(v)
                if grid[y][x] == PILLAR:
                    raise ValueError(v)
                return (x, y)

            game.exit = open_cell(data["exit"])
            powerups = {}
            for p in data["powerups"]:
                if not isinstance(p, list) or len(p) != 3 or p[2] not in POWERUPS:
                    return None
                powerups[open_cell(p)] = p[2]
            game.powerups = powerups

            game.player = open_cell(data["player"])
            px, py = game.player
            if grid[py][px] != EMPTY and not game.wallpass:
                return None
            game.move_cd = num(data["move_cd"])
            if data["queued"] is not None and data["queued"] not in DELTAS:
                return None
            game.queued = data["queued"]

            bombs = []
            for b in data["bombs"]:
                if not isinstance(b, list) or len(b) != 4:
                    return None
                pos = open_cell(b)
                if pos in {bb.pos for bb in bombs}:
                    return None
                bombs.append(Bomb(pos[0], pos[1], num(b[2], -1.0), flag(b[3])))
            if len(bombs) > game.max_bombs:
                return None
            game.bombs = bombs

            flames = {}
            for f in data["flames"]:
                if not isinstance(f, list) or len(f) != 5 or f[3] not in ("c", "h", "v"):
                    return None
                flames[open_cell(f)] = [num(f[2], -1.0), f[3], integer(f[4], 1)]
            game.flames = flames
            groups = {}
            for g in data["groups"]:
                if not isinstance(g, list) or len(g) != 2:
                    return None
                groups[integer(g[0], 1)] = integer(g[1])
            game.groups = groups
            game.next_group = integer(data["next_group"], 1)

            pending = []
            for p in data["pending"]:
                if not isinstance(p, list) or len(p) != 5 or p[3] not in ENEMY_TYPES:
                    return None
                x, y = open_cell(p)
                pending.append([x, y, integer(p[2], 1), p[3], num(p[4], -1.0)])
            game.pending = pending

            enemies = []
            for e in data["enemies"]:
                if not isinstance(e, list) or len(e) != 5:
                    return None
                x, y = open_cell(e)
                if e[2] not in ENEMY_TYPES or e[3] not in DELTAS:
                    return None
                enemies.append(Enemy(x, y, e[2], e[3], num(e[4])))
            game.enemies = enemies

            game.time_left = num(data["time_left"])
            if game.time_left > STAGE_TIME:
                return None
            game.timeout = flag(data["timeout"])
            game.flood_timer = num(data["flood_timer"], -1.0)

            # Restoring the RNG makes a resumed game play out exactly as the
            # saved one would have.
            game.rng.setstate(state)
            game.phase = None
            game.message = None
            return game
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            return None


POWERUP_NAMES = {
    "bomb": "extra bomb", "flame": "flame up", "speed": "speed up",
    "remote": "remote detonator", "bombpass": "bomb-pass", "wallpass": "wall-pass",
}

TITLE = "Bomberman"

# Every key the game reacts to (upper-case letters work too).
CONTROLS = [
    ("arrows", "move"),
    ("wasd hjkl", "move too"),
    ("space", "drop a bomb"),
    ("x", "detonate (remote)"),
    ("?", "help"),
    ("p / r", "pause / restart"),
    ("q", "menu"),
]

HELP = [
    ("Goal",
     "Blast your way through a walled grid. Kill every monster on the "
     "stage with your bombs, find the exit hidden under one of the soft "
     "blocks, and step onto it once it is open to reach the next stage."),
    ("Controls",
     "- arrows (or w a s d / h j k l): move one cell per keypress; hold a "
     "key to keep walking. Your speed sets how fast steps can follow each "
     "other: a key pressed mid-step is remembered and taken as soon as the "
     "step ends.\n"
     "- space: drop a bomb on your cell.\n"
     "- x: with the remote detonator, set off your oldest bomb.\n"
     "- p pauses, r starts a new game, q saves and goes back to the arcade "
     "menu (the game resumes, paused, next time).\n"
     "- ? opens this help (the game pauses while it is open)."),
    ("Bombs and flames",
     "A bomb shows its fuse counting down (O3 O2 O1) and goes off after "
     "2.5 seconds, throwing flames in a cross: up to your flame range in "
     "each direction (1 cell at first). Flames stop at the solid pillars, "
     "burn the first soft block they reach (and stop there), set off any "
     "other bomb they touch - chain reactions - and kill every monster and "
     "you if you stand in them. Flames linger for half a second.\n"
     "You can hold one bomb at a time at first. You cannot walk through a "
     "bomb: once you step off one you just dropped, it blocks you like a "
     "wall - and it blocks monsters too."),
    ("Power-ups",
     "One power-up hides under a soft block on every stage. Walk over it "
     "to take it (1000 points):\n"
     "- +B extra bomb: one more bomb at a time\n"
     "- +F flame: one more cell of flame range\n"
     "- +S speed: walk faster (twice at most)\n"
     "- +R remote detonator: bombs wait for x instead of a fuse\n"
     "- +P bomb-pass: walk through bombs\n"
     "- +W wall-pass: walk through soft blocks\n"
     "Losing a life loses remote, bomb-pass and wall-pass; bombs, flame "
     "and speed stay with you. Beware: a flame that hits an uncovered "
     "power-up destroys it and lets loose a pack of 6 monsters."),
    ("The exit",
     "The exit ([]) is hidden under a soft block. It stays shut while any "
     "monster is alive and opens (flashing) once the last one is dead: step "
     "on it to clear the stage. Never bomb an uncovered exit - every flame "
     "that touches it lets loose 6 monsters tougher than the stage's own."),
    ("Monsters",
     "Touching a monster costs a life. They come in six kinds, tougher "
     "ones on later stages:\n"
     "- qp drifter: slow, wanders at random (100)\n"
     "- >< darter: faster wanderer (200)\n"
     "- }{ hunter: follows you when you are within 6 cells (400)\n"
     "- && phantom: floats through soft blocks and follows you (800)\n"
     "- MM sprinter: fast and follows you (1000)\n"
     "- WW reaper: fastest, floats through soft blocks and always hunts "
     "you. Reapers only come when the stage timer runs out (2000)."),
    ("Stage timer",
     "Each stage has 200 seconds. When the timer hits 0, four reapers "
     "appear far from you, and one more every 10 seconds after that."),
    ("Lives and stages",
     "You have 3 lives. Losing one restarts the stage with a fresh layout. "
     "At 0 lives the game is over. Every stage brings more and smarter "
     "monsters and a few more soft blocks."),
    ("Scoring",
     "- monsters: 100 to 2000 by kind (see Monsters)\n"
     "- several monsters killed by one blast (chain reactions included): "
     "the 2nd scores double, the 3rd x4, the 4th and later x8\n"
     "- power-up picked up: 1000"),
    ("Legend",
     "- @@  you; xx  you losing a life\n"
     "- solid blocks  pillars: nothing gets through them\n"
     "- shaded blocks  soft blocks: flames burn them\n"
     "- %%  a soft block burning\n"
     "- O3 O2 O1  bomb with its fuse; Or  remote-controlled bomb\n"
     "- ** == ||  flames (centre, across, up and down)\n"
     "- []  the exit (flashing when open)\n"
     "- +B +F +S +R +P +W  power-ups\n"
     "- qp >< }{ && MM WW  monsters"),
    ("Tips",
     "- Drop a bomb, then step around a pillar corner: flames only travel "
     "in straight lines.\n"
     "- Let monsters walk into lingering flames, or wait for a line of them "
     "and blast them together for the multiplier.\n"
     "- Take care when chaining bombs: a chain goes off at once, sooner "
     "than you might expect.\n"
     "- Watch the timer: waiting too long brings the reapers."),
]

# Layout: the framed field (29 cells x 2 cols + border = 60 x 17) with, when
# there is room, a side column of stacked boxes: stats, controls, agents.
# Row 0 is the header/banner; everything else is centred as one block below.
FIELD_WIN_W = FIELD_W * 2 + 2
FIELD_WIN_H = FIELD_H + 2
MIN_COLS = FIELD_WIN_W
MIN_ROWS = FIELD_WIN_H + 1
STATS_H = 9
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
PAIR_PILLAR, PAIR_SOFT, PAIR_FLAME, PAIR_FLAME_HOT = 40, 41, 42, 43
PAIR_BOMB, PAIR_PLAYER, PAIR_EXIT, PAIR_ITEM = 44, 45, 46, 47
ENEMY_PAIRS = {"drifter": 48, "darter": 49, "hunter": 50, "phantom": 51,
               "sprinter": 52, "reaper": 53}

PILLAR_GLYPH = "██"   # full blocks; every field cell is two columns
SOFT_GLYPH = "░░"     # light shade
BURN_GLYPH = "%%"
FLAME_GLYPHS = {"c": "**", "h": "==", "v": "||"}
PLAYER_GLYPH = "@@"
DEAD_GLYPH = "xx"
EXIT_GLYPH = "[]"
REMOTE_BOMB_GLYPH = "Or"
POWERUP_GLYPHS = {"bomb": "+B", "flame": "+F", "speed": "+S", "remote": "+R",
                  "bombpass": "+P", "wallpass": "+W"}
ENEMY_GLYPHS = {"drifter": "qp", "darter": "><", "hunter": "}{", "phantom": "&&",
                "sprinter": "MM", "reaper": "WW"}


def bomb_glyph(bomb) -> str:
    if bomb.remote:
        return REMOTE_BOMB_GLYPH
    return "O%d" % max(1, min(9, math.ceil(bomb.fuse)))


def init_colors() -> None:
    bg = default_bg()
    curses.init_pair(PAIR_PILLAR, curses.COLOR_BLUE, bg)
    curses.init_pair(PAIR_SOFT, curses.COLOR_YELLOW, bg)
    curses.init_pair(PAIR_FLAME, curses.COLOR_RED, bg)
    curses.init_pair(PAIR_FLAME_HOT, curses.COLOR_YELLOW, bg)
    curses.init_pair(PAIR_BOMB, curses.COLOR_WHITE, bg)
    curses.init_pair(PAIR_PLAYER, curses.COLOR_WHITE, bg)
    curses.init_pair(PAIR_EXIT, curses.COLOR_GREEN, bg)
    curses.init_pair(PAIR_ITEM, curses.COLOR_CYAN, bg)
    for kind, color in (("drifter", curses.COLOR_YELLOW), ("darter", curses.COLOR_CYAN),
                        ("hunter", curses.COLOR_RED), ("phantom", curses.COLOR_MAGENTA),
                        ("sprinter", curses.COLOR_GREEN), ("reaper", curses.COLOR_RED)):
        curses.init_pair(ENEMY_PAIRS[kind], color, bg)


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
    """Centred text on a field row, padded and placed on the two-column cell
    grid so it never splits a block glyph in half."""
    text = text[:FIELD_WIN_W - 2]
    if len(text) % 2:
        text += " "
    x = 1 + 2 * max(0, (FIELD_WIN_W - 2 - len(text)) // 4)
    safe_addstr(win, row, x, text, attr)


def blink(now: float) -> bool:
    return int(now * 4) % 2 == 1


def display_key(game: Game, now: float):
    """Everything on screen that changes with time alone (the timer's
    seconds, the fuse digits, blinking); the loop redraws whenever this
    changes."""
    fuses = tuple(bomb_glyph(b) for b in game.bombs)
    blinking = bool(game.bombs) or game.exit_open()
    return (math.ceil(game.time_left), fuses, blinking and blink(now))


def draw_field(win, game: Game, now: float) -> None:
    phase = game.phase
    flash = blink(now)
    pillar = curses.color_pair(PAIR_PILLAR)
    soft = curses.color_pair(PAIR_SOFT)
    for y in range(FIELD_H):
        for x in range(FIELD_W):
            c = game.grid[y][x]
            if c == PILLAR:
                _put(win, x, y, PILLAR_GLYPH, pillar)
            elif c == SOFT:
                _put(win, x, y, SOFT_GLYPH, soft)
            elif c == BURN:
                _put(win, x, y, BURN_GLYPH, curses.color_pair(PAIR_FLAME) | curses.A_BOLD)
            elif (x, y) == game.exit:
                attr = curses.color_pair(PAIR_EXIT) | curses.A_BOLD
                if game.exit_open() and flash:
                    attr |= curses.A_REVERSE
                _put(win, x, y, EXIT_GLYPH, attr)
            elif (x, y) in game.powerups:
                _put(win, x, y, POWERUP_GLYPHS[game.powerups[(x, y)]],
                     curses.color_pair(PAIR_ITEM) | curses.A_BOLD)

    for bomb in game.bombs:
        attr = curses.color_pair(PAIR_BOMB) | curses.A_BOLD
        if not bomb.remote and bomb.fuse < 1.0 and flash:
            attr = curses.color_pair(PAIR_FLAME) | curses.A_BOLD
        _put(win, bomb.x, bomb.y, bomb_glyph(bomb), attr)
    for (x, y), f in game.flames.items():
        if game.grid[y][x] == BURN:
            continue
        hot = PAIR_FLAME_HOT if f[1] == "c" else PAIR_FLAME
        _put(win, x, y, FLAME_GLYPHS[f[1]], curses.color_pair(hot) | curses.A_BOLD)
    # Monsters after the blocks, so a phantom inside a soft block shows.
    for e in game.enemies:
        attr = curses.color_pair(ENEMY_PAIRS[e.kind]) | curses.A_BOLD
        if e.kind == "reaper":
            attr |= curses.A_REVERSE
        _put(win, e.x, e.y, ENEMY_GLYPHS[e.kind], attr)

    dying = phase is not None and phase.kind == "dying"
    if not game.game_over:
        px, py = game.player
        if dying:
            if int(phase.elapsed * 8) % 2 == 0:
                _put(win, px, py, DEAD_GLYPH, curses.color_pair(PAIR_FLAME) | curses.A_BOLD)
        else:
            _put(win, px, py, PLAYER_GLYPH, curses.color_pair(PAIR_PLAYER) | curses.A_BOLD)

    mid = 1 + FIELD_H // 2
    gold = curses.color_pair(PAIR_FLAME_HOT) | curses.A_BOLD
    if game.game_over:
        _centered(win, mid - 1, " GAME OVER ", curses.A_STANDOUT | curses.A_BOLD)
        _centered(win, mid + 1, " r restart | q menu ", curses.A_STANDOUT)
    elif game.paused:
        _centered(win, mid - 1, " PAUSED ", curses.A_STANDOUT | curses.A_BOLD)
    elif phase is not None and phase.kind == "stage_start":
        _centered(win, mid, f" STAGE {game.stage} ", curses.A_STANDOUT | curses.A_BOLD)
    elif phase is not None and phase.kind == "stage_clear":
        _centered(win, mid, f" STAGE {game.stage} CLEAR ", gold | curses.A_REVERSE)
    elif game.message is not None:
        # On the bottom border, so it never hides the play.
        _centered(win, FIELD_WIN_H - 1, f" {game.message[0]} ", gold | curses.A_REVERSE)


def timer_text(game: Game) -> str:
    if game.timeout:
        return "time  HURRY!"
    secs = math.ceil(game.time_left)
    return "time  %d:%02d" % (secs // 60, secs % 60)


def draw_stats(win, game: Game, best: int) -> None:
    rows, cols = win.getmaxyx()
    inner = cols - 2
    safe_addstr(win, 0, 2, f" {TITLE} "[: max(0, cols - 3)], curses.A_BOLD)
    if game.game_over:
        state = "GAME OVER"
    elif game.paused:
        state = "paused"
    elif game.phase is not None:
        state = {"dying": "ouch!", "stage_clear": "stage clear!",
                 "stage_start": "get ready"}[game.phase.kind]
    else:
        state = timer_text(game)
    extras = [name for name, on in (("remote", game.remote), ("bomb-pass", game.bombpass),
                                    ("wall-pass", game.wallpass)) if on]
    if game.exit_open():
        left = "monsters 0 - exit open!"
    else:
        left = f"monsters {game.remaining()}"
    lines = [
        (f"score {game.score}", curses.A_BOLD),
        (f"best  {max(best, game.score)}", 0),
        (f"stage {game.stage}   lives {game.lives}", curses.color_pair(PAIR_PLAYER)),
        (state, curses.color_pair(PAIR_FLAME) | curses.A_BOLD if game.timeout else 0),
        (left, curses.color_pair(ENEMY_PAIRS["hunter"])),
        (f"bombs {game.max_bombs}  fire {game.flame}  speed {game.speed}",
         curses.color_pair(PAIR_ITEM)),
        (" ".join(extras) if extras else "", curses.color_pair(PAIR_ITEM)),
    ]
    for i, (line, attr) in enumerate(lines):
        if 1 + i < rows - 1:
            safe_addstr(win, 1 + i, 1, line[:inner], attr)


def draw(game: Game, lay: Layout, session, resume: bool, best: int,
         help=None, now=None) -> None:
    if now is None:
        now = time.monotonic()
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
            head = " bomberman - GAME OVER "
        else:
            head = f" bomberman - {'paused' if game.paused else 'playing'} "
        if lay.plan["hint"] is None and lay.controls is None:
            head += "| ? help "  # no hint row and no controls box
        if lay.stats is None:  # no stats box in this tier: score goes here
            secs = "hurry" if game.timeout else f"{math.ceil(game.time_left)}s"
            head += (f"| score {game.score} | lives {game.lives} | stage {game.stage}"
                     f" | {secs} | left {game.remaining()} ")
        safe_addstr(scr, 0, 0, head[: lay.cols - 1],
                    curses.A_STANDOUT | (curses.A_BOLD if game.game_over else 0))

    lay.boxes()
    draw_field(lay.field, game, now)
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

BOMB_KEYS = (ord(" "),)
DETONATE_KEYS = (ord("x"), ord("X"))


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
    elif ch in BOMB_KEYS:
        game.drop_bomb()
    elif ch in DETONATE_KEYS:
        game.detonate()
    return None


def run(session) -> None:
    """Play Bomberman until `q` (back to the arcade menu) or SIGTERM. The
    game is saved on every exit path and resumed, paused, on the next run."""
    scr = session.stdscr
    init_colors()
    game_path = session.path("bomberman-game.json")
    hiscore_path = session.path("bomberman-hiscore.json")
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
            # Time-driven visuals (timer seconds, fuses, blinking) redraw
            # whenever what they show changes.
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
                draw(game, lay, session, st.resume, hiscore, help, now)
                scr.noutrefresh()
                curses.doupdate()
                dirty = False

            time.sleep(0.01)
    finally:
        # The one save point: q, SIGTERM, or an exception lands here. A
        # pending death/stage transition is resolved first so the save is a
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

    sys.exit(arcade.main(["--game", "bomberman"] + sys.argv[1:]))
