#!/usr/bin/env python3
"""Pac-Man for the Herdr arcade pane.

arcade.py calls `run(session)` with a ready curses screen and a
`common.Session` (agent roster, attention banner, SIGTERM flag, state dir).

Structure: everything above the "curses UI" divider is pure game logic --
maze, movement, ghosts, pellets, scoring, lives, levels, save/restore and the
layout plan -- with no terminal dependency, so `tests/test_pacman.py` runs
without a terminal.
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

# 19 x 21 cells, drawn 2 columns wide: a 40x23 framed window.
#   #  wall            .  dot            o  power pellet
#   (space) corridor   x  void (outside the maze, blocked, drawn blank)
#   =  ghost-house door (ghosts only: eyes going home, ghosts leaving)
#   H  ghost-house interior                P  Pac-Man start (a corridor)
# Row 9 is open at both edges: the side tunnel, which wraps.
MAZE = (
    "###################",
    "#........#........#",
    "#o##.###.#.###.##o#",
    "#.................#",
    "#.##.#.#####.#.##.#",
    "#....#...#...#....#",
    "####.### # ###.####",
    "xxx#.#       #.#xxx",
    "####.# ##=## #.####",
    "    .  #HHH#  .    ",
    "####.# ##### #.####",
    "xxx#.#       #.#xxx",
    "####.# ##### #.####",
    "#........#........#",
    "#.##.###.#.###.##.#",
    "#o.#.....P.....#.o#",
    "##.#.#.#####.#.#.##",
    "#....#...#...#....#",
    "#.######.#.######.#",
    "#.................#",
    "###################",
)
MAZE_ROWS = len(MAZE)
MAZE_COLS = len(MAZE[0])

BLOCKED = "#x"          # nobody enters
GHOST_ONLY = "=H"       # door + house interior: eyes and leaving ghosts only

UP, LEFT, DOWN, RIGHT = "up", "left", "down", "right"
DIR_ORDER = (UP, LEFT, DOWN, RIGHT)  # classic ghost tie-break order
DELTAS = {UP: (0, -1), LEFT: (-1, 0), DOWN: (0, 1), RIGHT: (1, 0)}
OPPOSITE = {UP: DOWN, DOWN: UP, LEFT: RIGHT, RIGHT: LEFT}


def _find(ch):
    for y, row in enumerate(MAZE):
        x = row.find(ch)
        if x >= 0:
            return (x, y)
    raise ValueError(ch)


def _cell(x, y) -> str:
    if 0 <= y < MAZE_ROWS and 0 <= x < MAZE_COLS:
        return MAZE[y][x]
    return "#"


WRAP_ROWS = frozenset(
    y for y, row in enumerate(MAZE) if row[0] not in BLOCKED and row[-1] not in BLOCKED
)


def _tunnel_cells():
    """Cells of a wrap row that are walled above and below, walked inward
    from each edge: where ghosts slow down."""
    out = set()
    for y in WRAP_ROWS:
        for xs in (range(MAZE_COLS), range(MAZE_COLS - 1, -1, -1)):
            for x in xs:
                if (_cell(x, y) in BLOCKED or _cell(x, y - 1) not in BLOCKED
                        or _cell(x, y + 1) not in BLOCKED):
                    break
                out.add((x, y))
    return frozenset(out)


TUNNEL_CELLS = _tunnel_cells()
PAC_START = _find("P")
DOOR = _find("=")
HOUSE_EXIT = (DOOR[0], DOOR[1] - 1)    # the corridor tile above the door
HOUSE_CENTER = (DOOR[0], DOOR[1] + 1)  # where eyes revive
DOT_CELLS = frozenset(
    (x, y) for y, row in enumerate(MAZE) for x, c in enumerate(row) if c == "."
)
PELLET_CELLS = frozenset(
    (x, y) for y, row in enumerate(MAZE) for x, c in enumerate(row) if c == "o"
)

GHOST_NAMES = ("blinky", "pinky", "inky", "clyde")
HOUSE, LEAVING, ACTIVE, EYES = "house", "leaving", "active", "eyes"
GHOST_MODES = (HOUSE, LEAVING, ACTIVE, EYES)
GHOST_STARTS = {
    "blinky": (HOUSE_EXIT, ACTIVE),
    "pinky": (HOUSE_CENTER, HOUSE),
    "inky": ((HOUSE_CENTER[0] - 1, HOUSE_CENTER[1]), HOUSE),
    "clyde": ((HOUSE_CENTER[0] + 1, HOUSE_CENTER[1]), HOUSE),
}
# Scatter targets sit outside the maze, one per corner.
SCATTER_TARGETS = {
    "blinky": (MAZE_COLS - 3, -3),
    "pinky": (2, -3),
    "inky": (MAZE_COLS - 1, MAZE_ROWS + 1),
    "clyde": (0, MAZE_ROWS + 1),
}
# Ghosts in the house leave in this order, each once its dot count (eaten
# this round) or its timer (seconds into the round) is reached.
RELEASE_ORDER = ("pinky", "inky", "clyde")
RELEASE_TIMES = {"pinky": 0.0, "inky": 4.0, "clyde": 8.0}
# Even when the dot counts allow it (later levels), exits stay staggered.
RELEASE_MIN_TIMES = {"pinky": 0.0, "inky": 1.0, "clyde": 2.0}


def release_dots(name: str, level: int) -> int:
    if level == 1:
        return {"pinky": 0, "inky": 30, "clyde": 60}[name]
    if level == 2:
        return {"pinky": 0, "inky": 0, "clyde": 50}[name]
    return 0


# Scatter/chase alternation in seconds; even indices scatter, odd chase,
# and past the end it is chase forever.
MODE_SCHEDULE = (7.0, 20.0, 7.0, 20.0, 5.0, 20.0, 5.0)

DOT_POINTS = 10
PELLET_POINTS = 50
GHOST_POINTS = (200, 400, 800, 1600)
EXTRA_LIFE_AT = 10000
START_LIVES = 3

READY_TIME = 1.5
DEATH_TIME = 1.2
LEVEL_CLEAR_TIME = 1.5
POPUP_TIME = 1.0
FLASH_TIME = 2.0      # frightened ghosts flash for the last seconds
MAX_STEP = 1.0 / 60   # update() integrates in slices no longer than this

SAVE_VERSION = 1


def pac_speed(level: int) -> float:
    """Cells per second."""
    return min(10.0, 7.5 + 0.5 * (level - 1))


def ghost_speed(level: int) -> float:
    return min(9.5, 7.0 + 0.5 * (level - 1))


FRIGHT_FACTOR = 0.6
TUNNEL_FACTOR = 0.5
EYES_SPEED = 15.0
HOUSE_SPEED = 4.0


def fright_duration(level: int) -> float:
    return max(1.0, 7.0 - level)


def neighbour(x, y, d):
    """The cell one step from (x, y) in direction d, wrapping on tunnel rows;
    None when that leaves the maze."""
    dx, dy = DELTAS[d]
    nx, ny = x + dx, y + dy
    if ny < 0 or ny >= MAZE_ROWS:
        return None
    if nx < 0 or nx >= MAZE_COLS:
        if ny not in WRAP_ROWS:
            return None
        nx %= MAZE_COLS
    return (nx, ny)


def pac_can_enter(pos) -> bool:
    return pos is not None and _cell(*pos) not in BLOCKED + GHOST_ONLY


def _dist2(a, b) -> int:
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2


class Ghost:
    __slots__ = ("name", "x", "y", "dir", "mode", "frightened", "timer")

    def __init__(self, name, x, y, dir=LEFT, mode=ACTIVE, frightened=False, timer=0.0):
        self.name = name
        self.x = x
        self.y = y
        self.dir = dir
        self.mode = mode
        self.frightened = frightened
        self.timer = timer

    @property
    def pos(self):
        return (self.x, self.y)

    def can_enter(self, pos) -> bool:
        if pos is None:
            return False
        c = _cell(*pos)
        if c in BLOCKED:
            return False
        if c in GHOST_ONLY:
            return self.mode in (EYES, LEAVING)
        return True


class Phase:
    """A timed interlude as an explicit logic phase (like Tetris' Clearing):
    'ready' (round start), 'dying' (death animation) or 'level_clear'. Its
    consequences apply when it completes -- via update(), or immediately via
    finish_phase()."""

    __slots__ = ("kind", "elapsed", "duration")

    def __init__(self, kind, duration):
        self.kind = kind
        self.elapsed = 0.0
        self.duration = duration


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

    def __init__(self, rng: random.Random | None = None, ready: bool = True):
        self.rng = rng if rng is not None else random.Random()
        self.score = 0
        self.lives = START_LIVES
        self.level = 1
        self.paused = False
        self.game_over = False
        self.extra_awarded = False
        self.popup = None  # [x, y, points, seconds left]; cosmetic, not saved
        self._load_dots()
        self._reset_round(ready)

    def reset(self) -> None:
        self.__init__(self.rng)

    def _load_dots(self) -> None:
        self.dots = set(DOT_CELLS)
        self.pellets = set(PELLET_CELLS)

    def _reset_round(self, ready: bool = True) -> None:
        """Everyone back to their start; dots, score and lives are kept."""
        self.pac_x, self.pac_y = PAC_START
        self.pac_dir = LEFT
        self.pac_want = None
        self.pac_timer = 0.0
        self.pac_moving = False
        self.chomp = 0
        self.ghosts = []
        for name in GHOST_NAMES:
            (x, y), mode = GHOST_STARTS[name]
            self.ghosts.append(Ghost(name, x, y, LEFT, mode))
        self.mode_index = 0
        self.mode_timer = 0.0
        self.fright_timer = 0.0
        self.eat_chain = 0
        self.round_time = 0.0
        self.round_dots = 0
        self.popup = None
        self.phase = Phase("ready", READY_TIME) if ready else None

    # -- queries -------------------------------------------------------------

    @property
    def pac_pos(self):
        return (self.pac_x, self.pac_y)

    def ghost(self, name) -> Ghost:
        for g in self.ghosts:
            if g.name == name:
                return g
        raise KeyError(name)

    @property
    def scatter(self) -> bool:
        return self.mode_index < len(MODE_SCHEDULE) and self.mode_index % 2 == 0

    def ghost_target(self, g: Ghost):
        """The tile a ghost steers toward (classic personalities)."""
        if g.mode == EYES:
            return HOUSE_CENTER
        if self.scatter:
            return SCATTER_TARGETS[g.name]
        px, py = self.pac_pos
        dx, dy = DELTAS[self.pac_dir]
        if g.name == "blinky":
            return (px, py)
        if g.name == "pinky":
            return (px + 4 * dx, py + 4 * dy)
        if g.name == "inky":
            pivot = (px + 2 * dx, py + 2 * dy)
            b = self.ghost("blinky")
            return (2 * pivot[0] - b.x, 2 * pivot[1] - b.y)
        # clyde: chase when far, retreat to his corner within 8 tiles
        if _dist2(g.pos, (px, py)) > 64:
            return (px, py)
        return SCATTER_TARGETS["clyde"]

    def ghost_interval(self, g: Ghost) -> float:
        if g.mode == EYES:
            speed = EYES_SPEED
        elif g.mode in (HOUSE, LEAVING):
            speed = HOUSE_SPEED
        else:
            speed = ghost_speed(self.level)
            if g.frightened:
                speed *= FRIGHT_FACTOR
            if g.pos in TUNNEL_CELLS:
                speed *= TUNNEL_FACTOR
        return 1.0 / speed

    # -- input ---------------------------------------------------------------

    def set_direction(self, d: str) -> None:
        """Buffer a turn: it applies as soon as it is legal (at the next
        cell where that exit is open), and stays buffered until then."""
        if d in DELTAS:
            self.pac_want = d

    # -- scoring -------------------------------------------------------------

    def _add_score(self, points: int) -> None:
        self.score += points
        if not self.extra_awarded and self.score >= EXTRA_LIFE_AT:
            self.extra_awarded = True
            self.lives += 1

    def _eat_at(self, pos) -> None:
        if pos in self.dots:
            self.dots.discard(pos)
            self.round_dots += 1
            self._add_score(DOT_POINTS)
        elif pos in self.pellets:
            self.pellets.discard(pos)
            self.round_dots += 1
            self._add_score(PELLET_POINTS)
            self._start_fright()
        else:
            return
        if not self.dots and not self.pellets:
            self.phase = Phase("level_clear", LEVEL_CLEAR_TIME)

    def _start_fright(self) -> None:
        self.fright_timer = fright_duration(self.level)
        self.eat_chain = 0
        for g in self.ghosts:
            if g.mode == ACTIVE:
                g.dir = OPPOSITE[g.dir]
            if g.mode != EYES:
                g.frightened = True

    def _end_fright(self) -> None:
        self.fright_timer = 0.0
        for g in self.ghosts:
            g.frightened = False

    # -- movement ------------------------------------------------------------

    def _pac_step(self) -> bool:
        """One cell of Pac-Man movement. True if Pac-Man moved."""
        pos = self.pac_pos
        if self.pac_want is not None and pac_can_enter(neighbour(*pos, self.pac_want)):
            self.pac_dir = self.pac_want
        nxt = neighbour(*pos, self.pac_dir)
        if not pac_can_enter(nxt):
            self.pac_moving = False
            return False
        self.pac_x, self.pac_y = nxt
        self.pac_moving = True
        self.chomp ^= 1
        self._eat_at(nxt)
        return True

    def _choose_dir(self, g: Ghost) -> str | None:
        options = [
            d for d in DIR_ORDER
            if d != OPPOSITE[g.dir] and g.can_enter(neighbour(g.x, g.y, d))
        ]
        if not options:  # dead end: the only way out is back
            back = OPPOSITE[g.dir]
            return back if g.can_enter(neighbour(g.x, g.y, back)) else None
        if g.frightened and g.mode == ACTIVE:
            return self.rng.choice(options)
        target = self.ghost_target(g)
        best = options[0]
        best_d = _dist2(neighbour(g.x, g.y, best), target)
        for d in options[1:]:
            dist = _dist2(neighbour(g.x, g.y, d), target)
            if dist < best_d:  # strict: earlier in DIR_ORDER wins ties
                best, best_d = d, dist
        return best

    def _ghost_step(self, g: Ghost) -> bool:
        """One cell of ghost movement. True if the ghost moved."""
        if g.mode == HOUSE:
            return False
        if g.mode == LEAVING:
            ex, ey = HOUSE_EXIT
            if g.x != ex:
                g.dir = RIGHT if ex > g.x else LEFT
                g.x += 1 if ex > g.x else -1
            elif g.y > ey:
                g.dir = UP
                g.y -= 1
            if g.pos == HOUSE_EXIT:
                g.mode = ACTIVE
                g.dir = LEFT
            return True
        if g.mode == EYES and g.pos == HOUSE_CENTER:
            g.mode = LEAVING
            g.frightened = False
            return False
        d = self._choose_dir(g)
        if d is None:
            return False
        g.dir = d
        g.x, g.y = neighbour(g.x, g.y, d)
        if g.mode == EYES and g.pos == HOUSE_CENTER:
            g.mode = LEAVING
            g.frightened = False
        return True

    def _release_ghosts(self) -> None:
        for name in RELEASE_ORDER:
            g = self.ghost(name)
            if g.mode != HOUSE:
                continue
            if self.round_time >= RELEASE_MIN_TIMES[name] and (
                    self.round_dots >= release_dots(name, self.level)
                    or self.round_time >= RELEASE_TIMES[name]):
                g.mode = LEAVING
                g.timer = 0.0
            return  # only the first ghost still waiting is considered

    def _touch(self, g: Ghost) -> None:
        """Pac-Man and a ghost share a cell (or just passed through each
        other)."""
        if g.mode in (EYES, HOUSE):
            return
        if g.frightened:
            points = GHOST_POINTS[min(self.eat_chain, len(GHOST_POINTS) - 1)]
            self.eat_chain += 1
            self._add_score(points)
            g.mode = EYES
            g.frightened = False
            self.popup = [g.x, g.y, points, POPUP_TIME]
        else:
            self.phase = Phase("dying", DEATH_TIME)

    def _advance_mode(self, step: float) -> None:
        if self.fright_timer > 0:
            self.fright_timer -= step
            if self.fright_timer <= 0:
                self._end_fright()
            return  # the scatter/chase clock stands still while frightened
        if self.mode_index >= len(MODE_SCHEDULE):
            return
        self.mode_timer += step
        if self.mode_timer >= MODE_SCHEDULE[self.mode_index]:
            self.mode_timer = 0.0
            self.mode_index += 1
            for g in self.ghosts:  # every mode change reverses ghosts
                if g.mode == ACTIVE:
                    g.dir = OPPOSITE[g.dir]

    def _tick(self, step: float) -> bool:
        changed = False
        self.round_time += step
        if self.popup is not None:
            self.popup[3] -= step
            if self.popup[3] <= 0:
                self.popup = None
            changed = True
        self._advance_mode(step)
        self._release_ghosts()

        # Pac-Man first; a collision is checked after every single move.
        pac_from = self.pac_pos
        pac_moved = False
        interval = 1.0 / pac_speed(self.level)
        self.pac_timer += step
        if self.pac_timer >= interval:
            self.pac_timer -= interval
            pac_moved = self._pac_step()
            if not pac_moved:
                # Blocked: keep the timer primed so a newly legal turn
                # moves on the very next tick.
                self.pac_timer = interval
            changed = True
            if self.phase is not None:
                return True  # level cleared
            if pac_moved:
                for g in self.ghosts:
                    if g.pos == self.pac_pos:
                        self._touch(g)
                        if self.phase is not None:
                            return True

        for g in self.ghosts:
            g.timer += step
            gi = self.ghost_interval(g)
            if g.timer < gi:
                continue
            g.timer -= gi
            ghost_from = g.pos
            if not self._ghost_step(g):
                continue
            changed = True
            swapped = (pac_moved and ghost_from == self.pac_pos
                       and g.pos == pac_from)
            if g.pos == self.pac_pos or swapped:
                self._touch(g)
                if self.phase is not None:
                    return True
        if 0 < self.fright_timer <= FLASH_TIME:
            changed = True  # the flashing needs redraws
        return changed

    # -- phases / time -------------------------------------------------------

    def finish_phase(self) -> None:
        """Resolve the pending phase immediately (save, tests). The state
        afterwards is exactly as if it had run to completion."""
        phase = self.phase
        if phase is None:
            return
        self.phase = None
        if phase.kind == "dying":
            self.lives -= 1
            self._end_fright()
            if self.lives <= 0:
                self.lives = 0
                self.game_over = True
            else:
                self._reset_round(ready=True)
        elif phase.kind == "level_clear":
            self.level += 1
            self._load_dots()
            self._reset_round(ready=True)

    def resolve_phases(self) -> None:
        """Finish every pending phase (dying -> ready -> none) so the state
        is a consistent, resumable one."""
        while self.phase is not None:
            self.finish_phase()

    def update(self, dt: float) -> bool:
        """Advance by `dt` seconds. True if anything visible changed."""
        if self.paused or self.game_over:
            return False
        if self.phase is not None:
            self.phase.elapsed += dt
            if self.phase.elapsed >= self.phase.duration:
                self.finish_phase()
            return True
        changed = False
        remaining = dt
        while remaining > 1e-12 and self.phase is None and not self.game_over:
            step = min(remaining, MAX_STEP)
            remaining -= step
            if self._tick(step):
                changed = True
        return changed

    # -- save / restore ------------------------------------------------------

    def to_save(self) -> dict:
        """Serializable snapshot. Pending phases are resolved first, so a
        save taken mid-death or mid-level-transition resumes consistently."""
        self.resolve_phases()
        return {
            "version": SAVE_VERSION,
            "level": self.level,
            "score": self.score,
            "lives": self.lives,
            "extra_awarded": self.extra_awarded,
            "game_over": self.game_over,
            "dots": sorted([x, y] for x, y in self.dots),
            "pellets": sorted([x, y] for x, y in self.pellets),
            "pac": {
                "x": self.pac_x, "y": self.pac_y, "dir": self.pac_dir,
                "want": self.pac_want, "timer": self.pac_timer,
            },
            "ghosts": [
                {
                    "name": g.name, "x": g.x, "y": g.y, "dir": g.dir,
                    "mode": g.mode, "frightened": g.frightened, "timer": g.timer,
                }
                for g in self.ghosts
            ],
            "mode_index": self.mode_index,
            "mode_timer": self.mode_timer,
            "fright_timer": self.fright_timer,
            "eat_chain": self.eat_chain,
            "round_time": self.round_time,
            "round_dots": self.round_dots,
            "rng": _rng_to_json(self.rng),
        }

    @classmethod
    def from_save(cls, data, rng=None):
        """Rebuild a saved game, or return None for anything corrupt,
        incompatible, or not exactly a save this version wrote."""
        try:
            if not isinstance(data, dict) or data.get("version") != SAVE_VERSION:
                return None

            def num(v, lo=0.0):
                if isinstance(v, bool) or not isinstance(v, (int, float)) or v < lo:
                    raise ValueError(v)
                return float(v)

            def integer(v, lo=0):
                if isinstance(v, bool) or not isinstance(v, int) or v < lo:
                    raise ValueError(v)
                return v

            def cells(v, allowed):
                out = set()
                for item in v:
                    if not isinstance(item, list) or len(item) != 2:
                        raise ValueError(item)
                    pos = (integer(item[0]), integer(item[1]))
                    if pos not in allowed:
                        raise ValueError(pos)
                    out.add(pos)
                return out

            game = cls(rng, ready=False)
            game.level = integer(data["level"], 1)
            game.score = integer(data["score"])
            game.lives = integer(data["lives"])
            game.extra_awarded = bool(data["extra_awarded"])
            game.game_over = bool(data["game_over"])
            if not game.game_over and game.lives < 1:
                return None
            game.dots = cells(data["dots"], DOT_CELLS)
            game.pellets = cells(data["pellets"], PELLET_CELLS)
            if not game.dots and not game.pellets and not game.game_over:
                return None

            pac = data["pac"]
            pos = (integer(pac["x"]), integer(pac["y"]))
            if not pac_can_enter(pos) or pac["dir"] not in DELTAS:
                return None
            if pac["want"] is not None and pac["want"] not in DELTAS:
                return None
            game.pac_x, game.pac_y = pos
            game.pac_dir = pac["dir"]
            game.pac_want = pac["want"]
            game.pac_timer = num(pac["timer"])

            ghosts = data["ghosts"]
            if not isinstance(ghosts, list) or len(ghosts) != len(GHOST_NAMES):
                return None
            game.ghosts = []
            for name, gd in zip(GHOST_NAMES, ghosts):
                if gd["name"] != name or gd["mode"] not in GHOST_MODES:
                    return None
                if gd["dir"] not in DELTAS:
                    return None
                g = Ghost(name, integer(gd["x"]), integer(gd["y"]), gd["dir"],
                          gd["mode"], bool(gd["frightened"]), num(gd["timer"]))
                if (g.mode == HOUSE and _cell(g.x, g.y) != "H") or (
                        g.mode != HOUSE and not g.can_enter(g.pos)):
                    return None
                game.ghosts.append(g)

            game.mode_index = integer(data["mode_index"])
            if game.mode_index > len(MODE_SCHEDULE):
                return None
            game.mode_timer = num(data["mode_timer"])
            game.fright_timer = num(data["fright_timer"])
            game.eat_chain = integer(data["eat_chain"])
            game.round_time = num(data["round_time"])
            game.round_dots = integer(data["round_dots"])
            # The RNG drives frightened moves: restoring it makes a resumed
            # game continue exactly as the saved one would have.
            state = _rng_from_json(data["rng"])
            if state is None:
                return None
            game.rng.setstate(state)
            return game
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            return None


TITLE = "Pac-Man"

# Every key the game reacts to (the side column's controls box, the key hint
# in narrow tiers). Key strings stay <= 9 characters so they are not clipped
# in the narrowest side column (common.controls_lines caps the key column).
CONTROLS = [
    ("arrows", "steer"),
    ("wasd/hjkl", "steer"),
    ("?", "help"),
    ("p / r", "pause / restart"),
    ("q", "menu"),
]

HELP = [
    ("Goal",
     "Eat every dot and power pellet in the maze to clear the level, "
     "without getting caught by the four ghosts. Each cleared level "
     "refills the maze and starts a faster one."),
    ("Controls",
     "- arrows, w a s d or h j k l: steer Pac-Man.\n"
     "- Turns are buffered: press a direction before a junction and Pac-Man "
     "takes it at the first opening. He keeps running until a wall stops "
     "him.\n"
     "- p pauses and resumes.\n"
     "- r restarts (the current game is abandoned, its score still counts "
     "for the best score).\n"
     "- q returns to the arcade menu; the game is saved and resumes, "
     "paused, next time.\n"
     "- ? opens this help and pauses the game; up/down scroll, any other "
     "key closes it."),
    ("How it works",
     "The maze: walls are blue blocks, '.' is a dot, '()' a power pellet, "
     "'--' the ghost-house door (ghosts only). The middle row is a tunnel "
     "that wraps to the other side; ghosts slow down inside it.\n"
     "The ghosts ('MM'):\n"
     "- Blinky (red) heads straight for you.\n"
     "- Pinky (magenta) aims 4 tiles ahead of you.\n"
     "- Inky (cyan) aims at a spot mirrored through Blinky, so he flanks.\n"
     "- Clyde (yellow) chases when far away but backs off to his corner "
     "within 8 tiles.\n"
     "Pinky, Inky and Clyde start in the ghost house and leave one by one. "
     "Ghosts alternate between scatter (each heads to its own corner) and "
     "chase, turning around at every switch; after a few rounds they chase "
     "for good.\n"
     "A power pellet turns the ghosts blue ('ww') for a while: 6 seconds "
     "on level 1, a second less each level, never under 1. They slow down, "
     "wander at random and flash white just before they recover. Eat one "
     "and its eyes ('\"\"') race home to revive it.\n"
     "Touching a normal ghost costs a life. You start with 3; losing the "
     "last one ends the game. Every level makes Pac-Man and the ghosts "
     "faster and the power pellets shorter."),
    ("Scoring",
     "- dot: 10\n"
     "- power pellet: 50\n"
     "- ghosts eaten on one power pellet: 200, 400, 800, 1600\n"
     "- one extra life at 10000 points.\n"
     "Your best score is kept between games."),
    ("Tips",
     "- Save the power pellets until ghosts are close, then turn and eat "
     "them in a chain for the big points.\n"
     "- Use the side tunnel to escape: ghosts crawl through it.\n"
     "- Ghosts turn around when a pellet is eaten and at every scatter/"
     "chase switch; a sudden U-turn is not always aimed at you.\n"
     "- Buffer your next turn early so you never stall at a corner."),
]


def key_hint(width: int) -> str:
    """One-line key hint for tiers without a controls box, cut to `width`.
    When the full hint does not fit, '? help' moves to the front so it
    always survives the cut."""
    text = controls_hint(CONTROLS)
    if len(text) > width:
        text = controls_hint([c for c in CONTROLS if c[0] == "?"]
                             + [c for c in CONTROLS if c[0] != "?"])
    return text[: max(0, width)]


# Adaptive layout. The popup's inner area is 93x28 (77x24 for the older
# 80x26 popup). Row 0 is the header/banner; the maze window (19 cells x 2
# cols + border = 40 x 23) and the side column (common.side_width: 30 when it
# fits) are centred as one block below it. The side column stacks the stats
# box, the controls box and the agents box. Without room for a side column
# the maze is centred alone and a one-line key hint goes on the last row --
# or into the header when there is no spare row.
MAZE_WIN_W = MAZE_COLS * 2 + 2
MAZE_WIN_H = MAZE_ROWS + 2
MIN_COLS = MAZE_WIN_W
MIN_ROWS = MAZE_WIN_H + 1
STATS_H = 7          # five lines + frame
AGENTS_MIN_H = 3     # frame + one line


def side_boxes(y: int, x: int, h: int, w: int) -> dict:
    """Stack stats, controls and agents in a y/x/h/w side column. Priority
    when short: controls, then stats, then agents (dropped first)."""
    ctrl_h = min(controls_height(CONTROLS, w), h)
    stats_h = min(STATS_H, h - ctrl_h)
    agents_h = h - ctrl_h - stats_h
    if agents_h < AGENTS_MIN_H:
        stats_h += agents_h  # no agents box: the stats box takes the rest
        agents_h = 0
    boxes = {"stats": None, "controls": None, "agents": None}
    if stats_h >= 3:
        boxes["stats"] = (y, x, stats_h, w)
    boxes["controls"] = (y + stats_h, x, ctrl_h, w)
    if agents_h:
        boxes["agents"] = (y + stats_h + ctrl_h, x, agents_h, w)
    return boxes


def layout_plan(cols: int, rows: int) -> dict:
    """Pure layout decision: tier plus each region's (y, x, height, width).
    Tiers: 'full' (maze + side column: stats, controls, agents), 'maze'
    (maze alone; score in the header and a key hint row), 'too_small'.
    `hint` is the key-hint row (None when the controls box is shown);
    `hint_in_header` says it shares row 0 with the header."""
    plan = {"tier": "too_small", "too_small": True, "maze": None,
            "stats": None, "controls": None, "agents": None,
            "hint": None, "hint_in_header": False}
    if cols < MIN_COLS or rows < MIN_ROWS:
        return plan
    plan["too_small"] = False
    side_w = side_width(cols, MAZE_WIN_W)
    if side_w:
        plan["tier"] = "full"
        y, x = center_origin(cols, rows, MAZE_WIN_W + 1 + side_w, MAZE_WIN_H)
        plan["maze"] = (y, x, MAZE_WIN_H, MAZE_WIN_W)
        plan.update(side_boxes(y, x + MAZE_WIN_W + 1, MAZE_WIN_H, side_w))
        return plan
    plan["tier"] = "maze"
    if rows >= MIN_ROWS + 1:  # a spare row: the hint gets the last one
        plan["hint"] = (rows - 1, 0, 1, cols)
        y, x = center_origin(cols, rows - 1, MAZE_WIN_W, MAZE_WIN_H)
    else:
        plan["hint"] = (0, 0, 1, cols)
        plan["hint_in_header"] = True
        y, x = center_origin(cols, rows, MAZE_WIN_W, MAZE_WIN_H)
    plan["maze"] = (y, x, MAZE_WIN_H, MAZE_WIN_W)
    return plan


# --------------------------------------------------------------------------
# curses UI
# --------------------------------------------------------------------------

# Colour pairs 20-29 belong to Pac-Man (see common.py).
PAIR_WALL, PAIR_PAC, PAIR_DOT, PAIR_DOOR = 20, 21, 22, 23
PAIR_FRIGHT, PAIR_FLASH = 24, 25
GHOST_PAIRS = {"blinky": 26, "pinky": 27, "inky": 28, "clyde": 29}

WALL = "██"  # every maze cell is exactly two columns
DOT = ". "
PELLET = "()"
DOOR_GLYPH = "--"
GHOST_GLYPH = "MM"
FRIGHT_GLYPH = "ww"
EYES_GLYPH = '""'
PAC_GLYPHS = {RIGHT: "(<", LEFT: ">)", UP: "\\/", DOWN: "/\\"}
DEATH_GLYPHS = ("()", "\\/", "--", "..", "  ")


def init_colors() -> None:
    bg = default_bg()
    curses.init_pair(PAIR_WALL, curses.COLOR_BLUE, bg)
    curses.init_pair(PAIR_PAC, curses.COLOR_YELLOW, bg)
    curses.init_pair(PAIR_DOT, curses.COLOR_WHITE, bg)
    curses.init_pair(PAIR_DOOR, curses.COLOR_MAGENTA, bg)
    curses.init_pair(PAIR_FRIGHT, curses.COLOR_BLUE, bg)
    curses.init_pair(PAIR_FLASH, curses.COLOR_WHITE, bg)
    curses.init_pair(GHOST_PAIRS["blinky"], curses.COLOR_RED, bg)
    curses.init_pair(GHOST_PAIRS["pinky"], curses.COLOR_MAGENTA, bg)
    curses.init_pair(GHOST_PAIRS["inky"], curses.COLOR_CYAN, bg)
    curses.init_pair(GHOST_PAIRS["clyde"], curses.COLOR_YELLOW, bg)


class Layout:
    """Screen regions for the current size; rebuilt on resize."""

    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.rows, self.cols = stdscr.getmaxyx()
        self.plan = layout_plan(self.cols, self.rows)
        self.too_small = self.plan["too_small"]
        self.maze = self.stats = self.controls = self.agents = None
        self.hint = self.plan["hint"]
        self.hint_in_header = self.plan["hint_in_header"]
        if self.too_small:
            return
        for name in ("maze", "stats", "controls", "agents"):
            if self.plan[name] is not None:
                y, x, h, w = self.plan[name]
                setattr(self, name, stdscr.derwin(h, w, y, x))

    def boxes(self):
        for win in (self.maze, self.stats, self.controls, self.agents):
            if win is not None:
                win.erase()
                frame(win)


def _put(win, x, y, text, attr=0):
    """One maze cell (two columns) at maze coordinates."""
    safe_addstr(win, 1 + y, 1 + 2 * x, text, attr)


def _centered(win, row, text, attr):
    safe_addstr(win, row, max(1, (MAZE_WIN_W - len(text)) // 2), text, attr)


def draw_maze(win, game: Game) -> None:
    phase = game.phase
    wall_attr = curses.color_pair(PAIR_WALL)
    if phase is not None and phase.kind == "level_clear" and int(phase.elapsed * 6) % 2:
        wall_attr = curses.color_pair(PAIR_FLASH) | curses.A_BOLD
    pellet_on = int(game.round_time * 4) % 2 == 0 or game.paused
    for y, row in enumerate(MAZE):
        for x, c in enumerate(row):
            if c == "#":
                _put(win, x, y, WALL, wall_attr)
            elif c == "=":
                _put(win, x, y, DOOR_GLYPH, curses.color_pair(PAIR_DOOR))
            elif (x, y) in game.pellets:
                if pellet_on:
                    _put(win, x, y, PELLET, curses.color_pair(PAIR_DOT) | curses.A_BOLD)
            elif (x, y) in game.dots:
                _put(win, x, y, DOT, curses.color_pair(PAIR_DOT))

    dying = phase is not None and phase.kind == "dying"
    hide_ghosts = (dying and phase.elapsed > DEATH_TIME * 0.3) or (
        phase is not None and phase.kind == "level_clear")
    if not hide_ghosts and not game.game_over:
        flash = 0 < game.fright_timer <= FLASH_TIME and int(game.fright_timer * 5) % 2 == 0
        for g in game.ghosts:
            if g.mode == EYES:
                _put(win, g.x, g.y, EYES_GLYPH, curses.color_pair(PAIR_FLASH) | curses.A_BOLD)
            elif g.frightened:
                pair = PAIR_FLASH if flash else PAIR_FRIGHT
                _put(win, g.x, g.y, FRIGHT_GLYPH, curses.color_pair(pair) | curses.A_BOLD)
            else:
                _put(win, g.x, g.y, GHOST_GLYPH,
                     curses.color_pair(GHOST_PAIRS[g.name]) | curses.A_BOLD)

    pac_attr = curses.color_pair(PAIR_PAC) | curses.A_BOLD
    if dying:
        i = int(phase.elapsed / phase.duration * len(DEATH_GLYPHS))
        glyph = DEATH_GLYPHS[min(i, len(DEATH_GLYPHS) - 1)]
    elif game.pac_moving and game.chomp:
        glyph = "()"
    else:
        glyph = PAC_GLYPHS[game.pac_dir]
    if not game.game_over:
        _put(win, game.pac_x, game.pac_y, glyph, pac_attr)

    if game.popup is not None:
        px, py, points, _ = game.popup
        text = str(points).ljust(4)
        px = min(max(px, 0), MAZE_COLS - 2)  # four columns = two cells
        _put(win, px, py, text, curses.color_pair(PAIR_FLASH) | curses.A_BOLD)

    # Overlays sit on the empty corridor below the ghost house.
    mid = 1 + HOUSE_CENTER[1] + 2
    if game.game_over:
        _centered(win, mid - 2, " GAME OVER ", curses.A_STANDOUT | curses.A_BOLD)
        _centered(win, mid, " r restart | q menu ", curses.A_STANDOUT)
    elif game.paused:
        _centered(win, mid, "PAUSED", curses.A_STANDOUT | curses.A_BOLD)
    elif phase is not None and phase.kind == "ready":
        _centered(win, mid, "READY!", curses.color_pair(PAIR_PAC) | curses.A_BOLD)
    elif phase is not None and phase.kind == "level_clear":
        _centered(win, mid, "CLEAR!", curses.color_pair(PAIR_PAC) | curses.A_BOLD)


def draw_stats(win, game: Game, best: int) -> None:
    rows, cols = win.getmaxyx()
    inner = cols - 2
    safe_addstr(win, 0, 2, " pac-man "[: max(0, cols - 3)], curses.A_BOLD)
    if game.game_over:
        state = "GAME OVER"
    elif game.paused:
        state = "paused"
    elif game.fright_timer > 0:
        state = f"power {game.fright_timer:.0f}s"
    else:
        state = "scatter" if game.scatter else "chase"
    lines = [
        f"score {game.score}",
        f"best  {max(best, game.score)}",
        f"level {game.level}",
        f"lives {game.lives}",
        state,
    ]
    for i, line in enumerate(lines[: max(0, rows - 2)]):
        attr = curses.A_BOLD if i == 0 else 0
        if i == 3:
            attr = curses.color_pair(PAIR_PAC)
        safe_addstr(win, 1 + i, 1, line[:inner], attr)


def draw(game: Game, lay: Layout, session, resume: bool, best: int,
         help: HelpOverlay | None = None) -> None:
    _draw_screen(game, lay, session, resume, best)
    if help is not None and help.is_open:
        help.draw(lay.stdscr)


def _draw_screen(game: Game, lay: Layout, session, resume: bool, best: int) -> None:
    scr = lay.stdscr
    scr.erase()
    if lay.too_small:
        draw_banner(scr, lay.cols, session.attention, resume)
        need = f"need {MIN_COLS}x{MIN_ROWS}, have {lay.cols}x{lay.rows}"
        lines = [f"pane too small: {need}", "resize the pane (q menu)"]
        if len(lines[0]) >= lay.cols:  # narrow: one short phrase per row
            lines = ["pane too small", need.split(", ")[0], need.split(", ")[1],
                     "resize the pane", "(q menu)"]
        top = max(0, (lay.rows - len(lines)) // 2)
        for i, line in enumerate(lines):
            line = line[: max(0, lay.cols - 1)]
            safe_addstr(scr, top + i, max(0, (lay.cols - len(line)) // 2), line,
                        curses.A_BOLD if i == 0 else 0)
        return

    if not draw_banner(scr, lay.cols, session.attention, resume):
        score = f"score {game.score} | lives {game.lives} | lvl {game.level} "
        if lay.hint_in_header:  # no spare row: the header carries the keys
            head = f" {score}| "
            head += key_hint(max(0, lay.cols - 1 - len(head)))
        else:
            if game.game_over:
                head = " pac-man - GAME OVER "
            else:
                head = f" pac-man - {'paused' if game.paused else 'playing'} "
            if lay.stats is None:  # no stats box in this tier: score goes here
                head += score
        safe_addstr(scr, 0, 0, head[: lay.cols - 1],
                    curses.A_STANDOUT | (curses.A_BOLD if game.game_over else 0))

    lay.boxes()
    draw_maze(lay.maze, game)
    if lay.stats is not None:
        draw_stats(lay.stats, game, best)
    if lay.controls is not None:
        draw_controls(lay.controls, CONTROLS)
    if lay.agents is not None:
        draw_agents(lay.agents, session.roster, session.status_pairs)
    if lay.hint is not None and not lay.hint_in_header:
        y, x, _, w = lay.hint
        text = key_hint(w - 1)
        safe_addstr(scr, y, x + max(0, (w - len(text)) // 2), text, curses.A_DIM)


KEY_DIRS = {
    curses.KEY_UP: UP, curses.KEY_DOWN: DOWN,
    curses.KEY_LEFT: LEFT, curses.KEY_RIGHT: RIGHT,
}
for _keys, _d in (("kKwW", UP), ("jJsS", DOWN), ("hHaA", LEFT), ("lLdD", RIGHT)):
    for _k in _keys:
        KEY_DIRS[ord(_k)] = _d


def route_help_key(help: HelpOverlay, game: Game, ch: int,
                   paused_by_help: bool):
    """The `?` help contract, ahead of every other key. Returns (consumed,
    paused_by_help). While the overlay is open it takes every key except
    KEY_RESIZE; closing it lifts the pause it caused. `?` opens it and
    pauses a running game."""
    if help.is_open:
        if ch == curses.KEY_RESIZE:
            return False, paused_by_help
        help.handle_key(ch)
        if not help.is_open and paused_by_help:
            game.paused = False
            paused_by_help = False
        return True, paused_by_help
    if ch == HELP_KEY:
        help.open()
        if not game.paused and not game.game_over:
            game.paused = True
            paused_by_help = True
        return True, paused_by_help
    return False, paused_by_help


def run(session) -> None:
    scr = session.stdscr
    init_colors()
    game_path = session.path("pacman-game.json")
    hiscore_path = session.path("pacman-hiscore.json")
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
    paused_by_help = False
    paused_by_attention = False
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
                elif paused_by_help:
                    # The alert now owns the help's pause: closing the help
                    # keeps the game paused until the banner is dismissed.
                    paused_by_attention = True
                    paused_by_help = False
                dirty = True

            quit_now = False
            while True:
                ch = scr.getch()
                if ch == -1:
                    break
                dirty = True
                consumed, paused_by_help = route_help_key(
                    help, game, ch, paused_by_help)
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
                elif ch in (ord("p"), ord("P")):
                    if not game.game_over:
                        game.paused = not game.paused
                        paused_by_attention = paused_by_resume = False
                elif ch in KEY_DIRS:
                    if not game.paused and not game.game_over:
                        game.set_direction(KEY_DIRS[ch])
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

    sys.exit(arcade.main(["--game", "pacman"] + sys.argv[1:]))
