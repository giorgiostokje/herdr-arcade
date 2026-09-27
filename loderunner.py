#!/usr/bin/env python3
"""Lode Runner for the Herdr arcade (see arcade.py, which runs it via `run()`).

Structure: everything above the "curses UI" divider is pure game logic --
tile grid, runner, guards, holes, scoring, levels, save/restore and the
layout plan -- with no terminal dependency, so `tests/test_loderunner.py`
imports this module without a terminal.

Rules in brief: collect every gold piece while avoiding the guards; dig
holes (z/x) in brick diagonally below you to trap guards (they climb out
after a while, a guard carrying gold drops it), holes refill after a few
seconds and crush whatever is inside. With all gold collected the hidden
escape ladder appears; climbing to the top row clears the level.
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

GRID_W = 26
GRID_H = 16

# Level tile characters. P and E only appear in level definitions (player
# and guard starts); the live grid holds them as EMPTY.
EMPTY = " "
BRICK = "#"      # diggable
SOLID = "@"      # never diggable
LADDER = "H"
BAR = "-"        # hand-over-hand bar
GOLD = "$"
HIDDEN = "S"     # escape ladder, appears when all gold is collected
FALSE = "X"      # false brick / trapdoor: looks like brick, you fall through
PLAYER = "P"
GUARD = "E"
GRID_TILES = frozenset((EMPTY, BRICK, SOLID, LADDER, BAR, GOLD, HIDDEN, FALSE))
LEVEL_TILES = GRID_TILES | {PLAYER, GUARD}

# Scoring.
GOLD_SCORE = 250
TRAP_SCORE = 75
KILL_SCORE = 75
LEVEL_SCORE = 1500

START_LIVES = 3
MAX_LIVES = 9

# Timing (seconds).
HOLE_TIME = 5.0          # dug hole refills after this
HOLE_WARN = 1.0          # last second of a hole is drawn as refilling
TRAP_TIME = 2.5          # a guard stays trapped this long, then climbs out
GOLD_HOLD_MIN = 4.0      # a guard lets picked-up gold go after a random
GOLD_HOLD_MAX = 10.0     # while in this range (seconds)
PLAYER_FALL_STEP = 0.07  # per cell
GUARD_FALL_STEP = 0.10
GUARD_STEP = 0.30        # guards move one cell per this (player: per key)
GUARD_STEP_MIN = 0.20
DEATH_TIME = 1.0
CLEAR_TIME = 1.5

SAVE_VERSION = 1

# Original levels, 26 x 16. Every level is solvable without digging (the
# tests check reachability of all gold and of the top row once the escape
# ladder shows); digging is for trapping guards and shortcuts.
LEVELS = [
    ("First Steps", [
        "                     S    ",
        "                     S    ",
        "    $       $        S    ",
        "  ###H#########H#####H    ",
        "     H         H     H    ",
        "     H   ---------   H $  ",
        "     H           H  ######",
        "     H    $      H        ",
        "##########H####  H   E    ",
        "          H      ####H####",
        "  $       H          H    ",
        "#####H#######        H    ",
        "     H          $    H    ",
        "     H  P    ########H####",
        "     H               H    ",
        "@@@@@@@@@@@@@@@@@@@@@@@@@@",
    ]),
    ("Bars and Drops", [
        "  S                       ",
        "  S                       ",
        "  S  $    ------------  $ ",
        "  H######H            ####",
        "  H      H  $    E    H   ",
        "  H      H#########H##H   ",
        "  H--------        H  H   ",
        "  H           $    H  H   ",
        "######H###  #######H  H $ ",
        "      H            H  ####",
        "  $   H   -------  H      ",
        "######H            H  E   ",
        "      H       #####H######",
        "   P  H  $         H      ",
        "###################H   $  ",
        "@@@@@@@@@@@@@@@@@@@@@@@@@@",
    ]),
    ("Trapdoors", [
        "            S             ",
        "            S             ",
        "   $        S    E    $   ",
        "@@@@@H@@@@@@H@@@@@@H@@@@@@",
        "     H      H      H      ",
        "     H  $   H   $  H  E   ",
        "##XX#####H#####H#####XX###",
        "         H     H          ",
        " $     E H     H       $  ",
        "####H####@@@@@@@@####H####",
        "    H                H    ",
        "    H---          ---H    ",
        "    H      $    $    H    ",
        "##H###X#####H#####X####H##",
        "  H         H  P       H $",
        "@@@@@@@@@@@@@@@@@@@@@@@@@@",
    ]),
    ("Twin Towers", [
        "                        S ",
        "                        S ",
        " $ H    $E   H    $   H S ",
        "###H#######  H  #######H#H",
        "   H      #  H  #      H H",
        "   H  E   #  H  #   E  H H",
        "   H####  #$ H $#  ####H H",
        "   H      ####H####    H H",
        "  EH  $       H        H H",
        "#######H---------------H#H",
        "       H           $   H H",
        "   $   H  #####H#####  H H",
        "####H##H       H       H H",
        "    H#####  P  H   $   H H",
        "    H      ######H######H ",
        "@@@@@@@@@@@@@@@@@@@@@@@@@@",
    ]),
    ("The Vault", [
        "S                         ",
        "S                         ",
        "S  $   ----------   $   E ",
        "H#####H          H########",
        "H     H   $  $   H        ",
        "H  E  H##H####H##H  $   E ",
        "H     H  H    H  H########",
        "H#XX###  H $$ H  ###XX##H#",
        "H        H@@@@H         H ",
        "HE $     H    H    $  E H ",
        "###H######    ######H###H ",
        "   H    ---------   H   H ",
        "   H              $ H   H ",
        "   H########H#######H   H ",
        " $ H    P   H      $    H ",
        "@@@@@@@@@@@@@@@@@@@@@@@@@@",
    ]),
]


class Phase:
    """A timed interlude as an explicit logic phase (like Tetris'
    Clearing): 'dying' or 'level_clear'. Its consequences apply when it
    completes -- via update(), or immediately via finish_phase()."""

    __slots__ = ("kind", "elapsed", "duration")

    def __init__(self, kind, duration):
        self.kind = kind
        self.elapsed = 0.0
        self.duration = duration


class Runner:
    """The player."""

    __slots__ = ("x", "y", "falling", "fall_t")

    def __init__(self, x, y):
        self.x = x
        self.y = y
        self.falling = False
        self.fall_t = 0.0


class Guard:
    __slots__ = ("x", "y", "gold", "drop_t", "trap", "falling", "fall_t",
                 "step_t")

    def __init__(self, x, y):
        self.x = x
        self.y = y
        self.gold = False       # carrying a gold piece
        self.drop_t = 0.0       # seconds until it lets the gold go
        self.trap = None        # seconds left trapped in a hole, or None
        self.falling = False
        self.fall_t = 0.0
        self.step_t = 0.0


def parse_level(rows):
    """(grid rows as lists, player start, guard starts) from a level's
    strings. Raises ValueError for a malformed level."""
    if len(rows) != GRID_H:
        raise ValueError("level height")
    grid = []
    player = None
    guards = []
    for y, row in enumerate(rows):
        if len(row) != GRID_W:
            raise ValueError("level width")
        line = []
        for x, ch in enumerate(row):
            if ch not in LEVEL_TILES:
                raise ValueError("tile %r" % ch)
            if ch == PLAYER:
                if player is not None:
                    raise ValueError("two players")
                player = (x, y)
                ch = EMPTY
            elif ch == GUARD:
                guards.append((x, y))
                ch = EMPTY
            line.append(ch)
        grid.append(line)
    if player is None:
        raise ValueError("no player")
    return grid, player, guards


def guard_step_interval(level: int, count: int = len(LEVELS)) -> float:
    """Guards speed up a little each time the level set of `count` levels
    wraps around."""
    loops = (level - 1) // count
    return max(GUARD_STEP_MIN, GUARD_STEP - 0.03 * loops)


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

    def __init__(self, rng=None, level: int = 1, levels=None):
        self.rng = rng if rng is not None else random.Random()
        self.levels = levels if levels is not None else LEVELS
        self.score = 0
        self.lives = START_LIVES
        self.level = level
        self.paused = False
        self.game_over = False
        self.phase = None
        self.load_level()

    def reset(self) -> None:
        self.__init__(self.rng, 1, self.levels)

    # -- level --------------------------------------------------------------

    @property
    def level_index(self) -> int:
        return (self.level - 1) % len(self.levels)

    @property
    def level_name(self) -> str:
        return self.levels[self.level_index][0]

    def load_level(self) -> None:
        """(Re)start the current level from its definition."""
        grid, (px, py), guards = parse_level(self.levels[self.level_index][1])
        self.grid = grid
        self.holes = {}                   # (x, y) -> seconds until refill
        self.revealed = False
        self.player = Runner(px, py)
        self.guards = [Guard(x, y) for x, y in guards]
        self.phase = None
        self._check_reveal()

    # -- tile queries ---------------------------------------------------------

    def in_bounds(self, x, y) -> bool:
        return 0 <= x < GRID_W and 0 <= y < GRID_H

    def kind(self, x, y) -> str:
        """Effective tile: out of bounds is SOLID, holes are EMPTY, the
        escape ladder is EMPTY until revealed."""
        if not self.in_bounds(x, y):
            return SOLID
        if (x, y) in self.holes:
            return EMPTY
        t = self.grid[y][x]
        if t == HIDDEN:
            return LADDER if self.revealed else EMPTY
        return t

    def passable(self, x, y) -> bool:
        return self.in_bounds(x, y) and self.kind(x, y) not in (BRICK, SOLID)

    def guard_at(self, x, y, exclude=None):
        for g in self.guards:
            if g is not exclude and g.x == x and g.y == y:
                return g
        return None

    def supported(self, x, y, exclude=None) -> bool:
        """Would something at (x, y) stay put (not fall)? Ladders and bars
        hold you; so does standing on brick, solid, a ladder top, the
        bottom edge or a guard's head (incl. a guard trapped in a hole)."""
        k = self.kind(x, y)
        if k in (LADDER, BAR):
            return True
        if y + 1 >= GRID_H:
            return True
        if self.kind(x, y + 1) in (BRICK, SOLID, LADDER):
            return True
        return self.guard_at(x, y + 1, exclude) is not None

    def gold_left(self) -> int:
        on_grid = sum(row.count(GOLD) for row in self.grid)
        return on_grid + sum(1 for g in self.guards if g.gold)

    def _check_reveal(self) -> None:
        if not self.revealed and self.gold_left() == 0:
            self.revealed = True

    # -- player ---------------------------------------------------------------

    def _can_act(self) -> bool:
        return (not self.game_over and not self.paused and self.phase is None
                and not self.player.falling)

    def move(self, dx: int) -> bool:
        """One cell left/right per keypress."""
        if not self._can_act():
            return False
        p = self.player
        if not self.passable(p.x + dx, p.y):
            return False
        p.x += dx
        self._after_player_move()
        return True

    def climb(self, dy: int) -> bool:
        """Up (-1) a ladder; down (+1) a ladder, onto a ladder below, or
        off a bar (drop)."""
        if not self._can_act():
            return False
        p = self.player
        here = self.kind(p.x, p.y)
        if dy < 0:
            if here != LADDER or not self.passable(p.x, p.y - 1):
                return False
        else:
            if not self.passable(p.x, p.y + 1):
                return False
            if self.guard_at(p.x, p.y + 1) is not None:
                return False
            if here not in (LADDER, BAR) and self.kind(p.x, p.y + 1) != LADDER:
                return False
        p.y += dy
        self._after_player_move()
        return True

    def dig(self, dx: int) -> bool:
        """Dig the brick diagonally below-left (-1) / below-right (+1). Only
        plain brick, only with an empty cell above it (no ladder, bar, gold,
        brick or guard), and only while standing."""
        if not self._can_act():
            return False
        p = self.player
        if not self.supported(p.x, p.y):
            return False
        tx, ty = p.x + dx, p.y + 1
        if not self.in_bounds(tx, ty) or (tx, ty) in self.holes:
            return False
        if self.grid[ty][tx] != BRICK:
            return False
        if self.kind(tx, p.y) != EMPTY or self.guard_at(tx, p.y) is not None:
            return False
        self.holes[(tx, ty)] = HOLE_TIME
        return True

    def _after_player_move(self) -> None:
        p = self.player
        if self.grid[p.y][p.x] == GOLD:
            self.grid[p.y][p.x] = EMPTY
            self.score += GOLD_SCORE
            self._check_reveal()
        if self._check_collision():
            return
        if self.revealed and p.y == 0:
            self._complete_level()
            return
        if not self.supported(p.x, p.y):
            if not p.falling:
                p.falling = True
                p.fall_t = 0.0
        else:
            p.falling = False

    def _complete_level(self) -> None:
        self.score += LEVEL_SCORE
        self.player.falling = False
        self.phase = Phase("level_clear", CLEAR_TIME)

    def _check_collision(self) -> bool:
        p = self.player
        if self.phase is None and self.guard_at(p.x, p.y) is not None:
            self._die()
            return True
        return False

    def _die(self) -> None:
        self.player.falling = False
        self.phase = Phase("dying", DEATH_TIME)

    # -- guards ---------------------------------------------------------------

    def _guard_enter(self, g, x, y) -> None:
        g.x, g.y = x, y
        if g.trap is None and self.grid[y][x] == GOLD and not g.gold:
            self.grid[y][x] = EMPTY
            g.gold = True
            g.drop_t = self.rng.uniform(GOLD_HOLD_MIN, GOLD_HOLD_MAX)
        if (x, y) in self.holes and g.trap is None:
            self._trap(g)

    def _trap(self, g) -> None:
        g.trap = TRAP_TIME
        g.falling = False
        g.fall_t = 0.0
        g.step_t = 0.0
        self.score += TRAP_SCORE
        if g.gold:
            # The gold pops out on top of the hole, where it can be grabbed.
            ax, ay = g.x, g.y - 1
            if self.in_bounds(ax, ay) and self.grid[ay][ax] == EMPTY \
                    and (ax, ay) not in self.holes:
                self.grid[ay][ax] = GOLD
                g.gold = False

    def _guard_open(self, g, x, y) -> bool:
        """Can guard g step into (x, y)? (Other guards block.)"""
        return self.passable(x, y) and self.guard_at(x, y, g) is None

    def _guard_can_down(self, g) -> bool:
        if not self._guard_open(g, g.x, g.y + 1):
            return False
        here = self.kind(g.x, g.y)
        return here in (LADDER, BAR) or self.kind(g.x, g.y + 1) == LADDER

    def _guard_can_up(self, g) -> bool:
        return self.kind(g.x, g.y) == LADDER and self._guard_open(g, g.x, g.y - 1)

    def _scan(self, g, want_down: bool):
        """Nearest column along the guard's row (walking, no drops past the
        first edge) from which it can head down (ladder below, a drop edge,
        a bar end) or up (a ladder). Returns the direction to walk, or 0."""
        best = None
        for d in (-1, 1):
            x = g.x
            dist = 0
            while True:
                x += d
                dist += 1
                if not self.passable(x, g.y):
                    break
                unsupported = not self.supported(x, g.y, g)
                if want_down:
                    ok = unsupported or self.kind(x, g.y + 1) == LADDER or (
                        self.kind(x, g.y) in (LADDER, BAR)
                        and self.passable(x, g.y + 1))
                else:
                    ok = (self.kind(x, g.y) == LADDER
                          and self.passable(x, g.y - 1))
                if ok:
                    toward = d == _sign(self.player.x - g.x)
                    key = (dist, 0 if toward else 1)
                    if best is None or key < best[0]:
                        best = (key, d)
                    break
                if unsupported:
                    break
        return 0 if best is None else best[1]

    def _row_clear(self, g, tx) -> bool:
        """Can the guard walk straight along its row to column tx?"""
        d = _sign(tx - g.x)
        x = g.x
        while x != tx:
            x += d
            if not self.passable(x, g.y):
                return False
            if x != tx and not self.supported(x, g.y, g):
                return False
        return True

    def guard_choice(self, g):
        """The classic simple chase: on the player's row walk at him; below
        him climb/scan for a ladder up; above him go down or scan for a
        ladder, bar or edge down; otherwise just close in horizontally.
        Returns (dx, dy) or None."""
        p = self.player
        if g.y == p.y and g.x != p.x and self._row_clear(g, p.x):
            return (_sign(p.x - g.x), 0)
        if p.y > g.y:
            if self._guard_can_down(g):
                return (0, 1)
            d = self._scan(g, want_down=True)
            if d:
                return (d, 0)
        elif p.y < g.y:
            if self._guard_can_up(g):
                return (0, -1)
            d = self._scan(g, want_down=False)
            if d:
                return (d, 0)
        d = _sign(p.x - g.x)
        if d:
            return (d, 0)
        return None

    def _maybe_drop_gold(self, g) -> None:
        """Carried gold is let go once its timer runs out, on the first
        plain floor cell (so a guard never keeps the level's gold forever)."""
        if (g.gold and g.drop_t <= 0 and self.grid[g.y][g.x] == EMPTY
                and self.kind(g.x, g.y) == EMPTY
                and self.supported(g.x, g.y, g)):
            self.grid[g.y][g.x] = GOLD
            g.gold = False

    def _guard_step(self, g) -> None:
        self._maybe_drop_gold(g)
        choice = self.guard_choice(g)
        if choice is None:
            return
        dx, dy = choice
        nx, ny = g.x + dx, g.y + dy
        if self._guard_open(g, nx, ny):
            self._guard_enter(g, nx, ny)

    def _climb_out(self, g) -> bool:
        """Leave the hole diagonally upward, toward the player first."""
        d = _sign(self.player.x - g.x) or 1
        for dx in (d, -d):
            nx, ny = g.x + dx, g.y - 1
            if self._guard_open(g, nx, ny) and (nx, ny) not in self.holes:
                g.trap = None
                self._guard_enter(g, nx, ny)
                return True
        return False

    def _respawn(self, g) -> None:
        """A guard crushed by a refilling hole reappears at the top."""
        for y in range(GRID_H):
            spots = [x for x in range(GRID_W)
                     if self.kind(x, y) == EMPTY and self.guard_at(x, y, g) is None
                     and (x, y) != (self.player.x, self.player.y)]
            if spots:
                g.x, g.y = self.rng.choice(spots), y
                break
        g.trap = None
        g.falling = False
        g.fall_t = 0.0
        g.step_t = 0.0

    def _update_guard(self, g, dt, step) -> None:
        if g.trap is not None:
            g.trap -= dt
            if g.trap <= 0:
                g.trap = 0.0
                g.step_t += dt
                if g.step_t >= step:
                    g.step_t = 0.0
                    self._climb_out(g)
            return
        if not self.supported(g.x, g.y, g):
            if not g.falling:
                g.falling = True
                g.fall_t = 0.0
            g.fall_t += dt
            while g.fall_t >= GUARD_FALL_STEP and g.falling:
                g.fall_t -= GUARD_FALL_STEP
                if not self._guard_open(g, g.x, g.y + 1):
                    g.falling = False
                    break
                self._guard_enter(g, g.x, g.y + 1)
                if g.trap is not None or self.supported(g.x, g.y, g):
                    g.falling = False
            g.step_t = 0.0
            return
        g.falling = False
        if g.gold:
            g.drop_t -= dt
        g.step_t += dt
        if g.step_t >= step:
            g.step_t -= step
            self._guard_step(g)

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
                self.load_level()
        elif phase.kind == "level_clear":
            self.level += 1
            self.lives = min(MAX_LIVES, self.lives + 1)
            self.load_level()

    def update(self, dt: float) -> bool:
        """Advance the game by `dt` seconds. True if anything changed."""
        if self.paused or self.game_over:
            return False
        if self.phase is not None:
            self.phase.elapsed += dt
            if self.phase.elapsed >= self.phase.duration:
                self.finish_phase()
            return True
        p = self.player
        # Holes: tick and refill, crushing whoever is inside.
        for pos in list(self.holes):
            self.holes[pos] -= dt
            if self.holes[pos] <= 0:
                del self.holes[pos]
                for g in self.guards:
                    if (g.x, g.y) == pos:
                        self.score += KILL_SCORE
                        self._respawn(g)
                if (p.x, p.y) == pos:
                    self._die()
                    return True
        # Player gravity: a timed per-cell fall, no control meanwhile.
        if not p.falling and not self.supported(p.x, p.y):
            p.falling = True
            p.fall_t = 0.0
        if p.falling:
            p.fall_t += dt
            while p.falling and p.fall_t >= PLAYER_FALL_STEP:
                p.fall_t -= PLAYER_FALL_STEP
                if self.supported(p.x, p.y) or not self.passable(p.x, p.y + 1):
                    p.falling = False
                    break
                p.y += 1
                self._after_player_move()
                if self.phase is not None:
                    return True
        # Guards.
        step = guard_step_interval(self.level, len(self.levels))
        for g in self.guards:
            self._update_guard(g, dt, step)
        self._check_reveal()
        self._check_collision()
        return True

    # -- save / restore -----------------------------------------------------------

    def to_save(self) -> dict:
        """Serializable snapshot. The caller resolves any phase first
        (finish_phase), so a save never holds a death/clear animation."""
        p = self.player
        return {
            "version": SAVE_VERSION,
            "level": self.level,
            "score": self.score,
            "lives": self.lives,
            "grid": ["".join(row) for row in self.grid],
            "holes": [[x, y, t] for (x, y), t in sorted(self.holes.items())],
            "revealed": self.revealed,
            "player": [p.x, p.y, p.falling, p.fall_t],
            "guards": [[g.x, g.y, g.gold, g.drop_t, g.trap, g.falling,
                        g.fall_t, g.step_t] for g in self.guards],
            "rng": _rng_to_json(self.rng),
        }

    @classmethod
    def from_save(cls, data, rng=None):
        """Rebuild a saved game, or None for anything corrupt/incompatible."""
        try:
            if not isinstance(data, dict) or data.get("version") != SAVE_VERSION:
                return None
            level = data["level"]
            if not isinstance(level, int) or level < 1:
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
            holes = {}
            for x, y, t in data["holes"]:
                if not (isinstance(x, int) and isinstance(y, int)
                        and game.in_bounds(x, y) and game.grid[y][x] == BRICK):
                    return None
                holes[(x, y)] = float(t)
            game.holes = holes
            game.revealed = bool(data["revealed"])
            px, py, pfall, pft = data["player"]
            if not (isinstance(px, int) and isinstance(py, int)
                    and game.passable(px, py)):
                return None
            game.player = Runner(px, py)
            game.player.falling = bool(pfall)
            game.player.fall_t = float(pft)
            guards = []
            for (gx, gy, gold, drop_t, trap, falling, fall_t,
                 step_t) in data["guards"]:
                if not (isinstance(gx, int) and isinstance(gy, int)
                        and game.passable(gx, gy)):
                    return None
                g = Guard(gx, gy)
                g.gold = bool(gold)
                g.drop_t = float(drop_t)
                g.trap = None if trap is None else float(trap)
                g.falling = bool(falling)
                g.fall_t = float(fall_t)
                g.step_t = float(step_t)
                guards.append(g)
            game.guards = guards
            score, lives = data["score"], data["lives"]
            if not (isinstance(score, int) and isinstance(lives, int)
                    and lives >= 1):
                return None
            game.score, game.lives = score, lives
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

TITLE = "Lode Runner"

# Every key the game reacts to (see handle_key and run()).
CONTROLS = [
    ("arrows", "run/climb"),
    ("wasd hjkl", "as arrows"),
    ("z or ,", "dig down-L"),
    ("x or .", "dig down-R"),
    ("?", "help"),
    ("p / r", "pause / restart"),
    ("q", "menu"),
]

HELP = [
    ("Goal",
     "Collect every gold piece ($$) on the level while staying away from "
     "the guards (<>). When the last piece is taken a hidden escape ladder "
     "appears; climb it to the very top row of the level to clear it."),
    ("Controls",
     "- left / right (or a d, h l): run one cell per keypress; hold the "
     "key to keep running.\n"
     "- up (w, k): climb a ladder.\n"
     "- down (s, j): climb down a ladder, step down onto a ladder below "
     "you, or let go of a bar and drop.\n"
     "- z or , : dig the brick down-left of you.\n"
     "- x or . : dig the brick down-right of you.\n"
     "- ? this help (the game pauses), p pause, r restart from level 1, "
     "q back to the menu (the game is saved and resumes paused)."),
    ("Ladders, bars and falling",
     "- Ladders (H) can be climbed up and down, and you can stand on top "
     "of one.\n"
     "- Bars (--) are hand-over-hand ropes: run along them without "
     "falling, press down to drop off.\n"
     "- Anywhere else you need something under your feet: brick, stone, "
     "a ladder top or a guard's head. Step off an edge and you fall, cell "
     "by cell, until you land or a bar catches you. Falls never hurt, but "
     "you cannot steer or dig while falling."),
    ("Digging",
     "- z / x dig the cell one step to the side and one row DOWN (a "
     "diagonal), never the one under your own feet.\n"
     "- Only red brick can be dug. Grey stone never can, nor can false "
     "brick.\n"
     "- You must be standing (not falling), and the cell beside you, "
     "above the brick, must be empty: no ladder, bar, gold, brick or "
     "guard there.\n"
     "- A hole stays open for 5 seconds, then refills; during its last "
     "second it shows as .. as a warning. Whatever is inside when it "
     "refills is crushed - including you, and you cannot climb out of a "
     "hole on your own, so do not fall into one."),
    ("Trapping guards",
     "- A guard that walks or falls into a hole is stuck there (+75). "
     "After about 2.5 seconds it climbs out diagonally upward, toward you "
     "if it can.\n"
     "- A trapped guard's head is solid ground: walk right across the "
     "hole on top of it. You can stand on any guard's head, in fact.\n"
     "- If the hole refills while a guard is still inside, the guard is "
     "crushed (+75) and reappears somewhere on the top of the level."),
    ("Guards and gold",
     "- Guards hunt you: they run along your row toward you, climb "
     "ladders and drop off edges and bars to reach you. They move a bit "
     "slower than you can tap. Touching a guard costs a life.\n"
     "- A guard that walks over gold picks it up and is drawn as <$. "
     "Gold a guard carries still counts as not collected, so the exit "
     "stays hidden.\n"
     "- A carrying guard lets the gold go after 4 to 10 seconds, on the "
     "next plain floor cell. Trap it in a hole and the gold pops out on "
     "top of the hole at once, ready to grab."),
    ("Escape ladder",
     "Once no gold is left - on the ground or with a guard - the hidden "
     "escape ladder appears (a bright cyan H, usually rising to the top "
     "edge) and the stats show gold EXIT!. Reach the top row of the "
     "level to clear it (+1500) and gain an extra life."),
    ("False bricks",
     "Some bricks are fake: they look exactly like normal red brick, but "
     "they are trapdoors - you (and guards) fall straight through them "
     "and they cannot be dug. Levels 3 (Trapdoors) and 5 (The Vault) "
     "have them; if you drop through a floor, that was one."),
    ("Lives and levels",
     "You start with 3 lives and earn one more per cleared level (up to "
     "9). Losing a life restarts the current level from scratch, gold and "
     "all. At 0 lives the game is over. There are 5 levels; after the "
     "last one they repeat with faster guards."),
    ("Scoring",
     "- gold piece: 250\n"
     "- guard trapped in a hole: 75\n"
     "- guard crushed by a refilling hole: 75\n"
     "- level cleared: 1500\n"
     "Your best score is kept between games."),
    ("Legend",
     "- []  you (** when you die)\n"
     "- <>  guard;  <$  guard carrying gold\n"
     "- solid red block: brick, diggable (false brick looks the same)\n"
     "- grey shaded block: stone, never diggable\n"
     "- H   ladder;  bright cyan H: the escape ladder\n"
     "- --  bar (hang and move along it)\n"
     "- $$  gold\n"
     "- ..  a dug hole about to refill"),
    ("Tips",
     "- Plan a route before grabbing gold: every level can be solved "
     "without digging; digging is for guards and shortcuts.\n"
     "- Dig a hole in a guard's path, wait for it to drop in, then cross "
     "on its head.\n"
     "- Guards follow you down ladders: lure them into a pit, or dig two "
     "holes side by side for a wider trap.\n"
     "- p pauses at any time; the timers stop while paused or in help."),
]

# The play window is 26 cells x 2 columns + 2 border = 54 wide and
# 16 + 2 = 18 rows tall. At the 93x28 popup the composition is board +
# 1 gap + a 30-wide side column (stats, keys, agents), centred.
BOARD_W = GRID_W * 2 + 2           # 54
BOARD_H = GRID_H + 2               # 18
STATS_H = 5                        # frame + 3 lines at the full side width
BOX_MIN_H = 3                      # frame + 1 line
AGENTS_MIN_H = 4                   # frame + an agent + "+N more"
MIN_COLS = BOARD_W                 # 54
MIN_ROWS = BOARD_H + 1             # 19: header + board


def layout_plan(cols: int, rows: int) -> dict:
    """Pure layout decision: tier plus each region's (y, x, height, width).
    Tiers: 'full' (board + side column: stats, controls, agents), 'side'
    (side column without agents), 'board' (no side column: stats go in the
    header and a key hint row sits at the bottom -- or in the header when
    there is no spare row), 'too_small'."""
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
    agents_h = left if left >= AGENTS_MIN_H else 0
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
    gold = "gold EXIT!" if game.revealed else "gold %d" % game.gold_left()
    return ["score %d" % game.score, "best %d" % max(best, game.score),
            "level %d" % game.level, "lives %d" % game.lives, gold,
            "guards %d" % len(game.guards)]


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
PAIR_BRICK, PAIR_SOLID, PAIR_LADDER, PAIR_BAR = 40, 41, 42, 43
PAIR_GOLD, PAIR_PLAYER, PAIR_GUARD, PAIR_ESCAPE = 44, 45, 46, 47

GLYPHS = {
    BRICK: ("██", PAIR_BRICK),
    FALSE: ("██", PAIR_BRICK),
    SOLID: ("░░", PAIR_SOLID),
    LADDER: ("H ", PAIR_LADDER),
    BAR: ("--", PAIR_BAR),
    GOLD: ("$$", PAIR_GOLD),
}
PLAYER_GLYPH = "[]"
GUARD_GLYPH = "<>"
GUARD_GOLD_GLYPH = "<$"
REFILL_GLYPH = ".."


def init_colors() -> None:
    bg = default_bg()
    curses.init_pair(PAIR_BRICK, curses.COLOR_RED, bg)
    curses.init_pair(PAIR_SOLID, curses.COLOR_WHITE, bg)
    curses.init_pair(PAIR_LADDER, curses.COLOR_WHITE, bg)
    curses.init_pair(PAIR_BAR, curses.COLOR_WHITE, bg)
    curses.init_pair(PAIR_GOLD, curses.COLOR_YELLOW, bg)
    curses.init_pair(PAIR_PLAYER, curses.COLOR_GREEN, bg)
    curses.init_pair(PAIR_GUARD, curses.COLOR_MAGENTA, bg)
    curses.init_pair(PAIR_ESCAPE, curses.COLOR_CYAN, bg)


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
            state = "level %d: %s" % (game.level, game.level_name)
        head = " lode runner - %s " % state
        if lay.plan["hint_in_header"]:
            head = " lode runner | ? help "
        if not stats_fit:
            head += "| score %d | lives %d | gold %s " % (
                game.score, game.lives,
                "EXIT!" if game.revealed else game.gold_left())
        if lay.plan["hint_in_header"]:
            head += "| " + key_hint().replace("? help | ", "") + " "
        elif len(head) > lay.cols - 1 and state.startswith("level"):
            head = head.replace(": %s " % game.level_name, " ", 1)
        safe_addstr(scr, 0, 0, head[: lay.cols - 1], curses.A_STANDOUT)

    lay.boxes()
    bw = lay.board
    dying = game.phase is not None and game.phase.kind == "dying"

    # -- tiles ------------------------------------------------------------------
    for y in range(GRID_H):
        for x in range(GRID_W):
            cx = 1 + x * 2
            if (x, y) in game.holes:
                if game.holes[(x, y)] <= HOLE_WARN:
                    safe_addstr(bw, 1 + y, cx, REFILL_GLYPH,
                                curses.color_pair(PAIR_BRICK))
                continue
            t = game.grid[y][x]
            if t == HIDDEN:
                if game.revealed:
                    safe_addstr(bw, 1 + y, cx, "H ",
                                curses.color_pair(PAIR_ESCAPE) | curses.A_BOLD)
                continue
            glyph = GLYPHS.get(t)
            if glyph is not None:
                attr = curses.color_pair(glyph[1])
                if t == GOLD:
                    attr |= curses.A_BOLD
                safe_addstr(bw, 1 + y, cx, glyph[0], attr)

    # -- actors -------------------------------------------------------------------
    for g in game.guards:
        text = GUARD_GOLD_GLYPH if g.gold else GUARD_GLYPH
        attr = curses.color_pair(PAIR_GUARD) | curses.A_BOLD
        if g.trap is not None:
            attr = curses.color_pair(PAIR_GUARD)
        safe_addstr(bw, 1 + g.y, 1 + g.x * 2, text, attr)
    p = game.player
    if dying:
        if int(game.phase.elapsed * 10) % 2 == 0:
            safe_addstr(bw, 1 + p.y, 1 + p.x * 2, "**",
                        curses.color_pair(PAIR_PLAYER) | curses.A_BOLD)
    else:
        safe_addstr(bw, 1 + p.y, 1 + p.x * 2, PLAYER_GLYPH,
                    curses.color_pair(PAIR_PLAYER) | curses.A_BOLD)

    # -- overlays -------------------------------------------------------------------
    mid = BOARD_H // 2 - 1
    help_open = help_overlay is not None and help_overlay.is_open
    if game.game_over:
        _center(bw, mid, " GAME OVER ", curses.A_STANDOUT | curses.A_BOLD)
        _center(bw, mid + 1, " r restart | ? help | q menu ", curses.A_STANDOUT)
    elif game.phase is not None and game.phase.kind == "level_clear":
        _center(bw, mid, " LEVEL CLEAR +%d " % LEVEL_SCORE,
                curses.A_STANDOUT | curses.A_BOLD)
    elif game.paused and not help_open:
        _center(bw, mid, " PAUSED ", curses.A_STANDOUT | curses.A_BOLD)
        _center(bw, mid + 1, " p resume | ? help | r restart | q menu ",
                curses.A_STANDOUT)

    # -- side column ------------------------------------------------------------------
    if lay.stats is not None:
        sw = lay.stats
        safe_addstr(sw, 0, 2, " %s " % TITLE.lower(), curses.A_BOLD)
        for i, line in enumerate(stats_lines[: sh - 2]):
            safe_addstr(sw, 1 + i, 1, line)
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
DIG_LEFT_KEYS = (ord("z"), ord("Z"), ord(","))
DIG_RIGHT_KEYS = (ord("x"), ord("X"), ord("."))


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
    if ch in DIG_LEFT_KEYS:
        return game.dig(-1)
    if ch in DIG_RIGHT_KEYS:
        return game.dig(1)
    return False


def run(session) -> None:
    """Play Lode Runner until `q` (back to the menu) or SIGTERM. The game is
    saved on every exit path and resumed, paused, on the next run."""
    scr = session.stdscr
    init_colors()
    game_path = session.path("loderunner-game.json")
    hiscore_path = session.path("loderunner-hiscore.json")
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

    sys.exit(arcade.main(["--game", "loderunner"] + sys.argv[1:]))
