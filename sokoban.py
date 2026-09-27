#!/usr/bin/env python3
"""Sokoban for the Herdr arcade (Thinking Rabbit 1982; see arcade.py, which
runs it via `run()`).

You are the warehouse keeper. Walk the floor one cell per keypress and push
the crates - one at a time, never pulling - until every crate stands on a
goal. Every move can be undone and redone; a level can be restarted at any
time. Solving a level unlocks the next one, and each level keeps your best
move and push counts.

Structure: everything above the "curses UI" divider is pure game logic -
the level set, movement, pushing, undo/redo, progress and bests, scoring,
save/restore and the layout plan - with no terminal dependency, so
`tests/test_sokoban.py` imports this module without a terminal. Sokoban
has no randomness at all, so unlike the other games there is no RNG state
to save: the move history alone reproduces the position exactly.
"""

from __future__ import annotations

import curses
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

FIELD_W = 16  # cells; each cell is two terminal columns
FIELD_H = 12

UP, DOWN, LEFT, RIGHT = "up", "down", "left", "right"
DELTAS = {UP: (0, -1), DOWN: (0, 1), LEFT: (-1, 0), RIGHT: (1, 0)}
DIRS = (UP, DOWN, LEFT, RIGHT)
# The move history uses the classic Sokoban notation: one letter per move,
# lower case for a walk, upper case for a push.
LETTER = {UP: "u", DOWN: "d", LEFT: "l", RIGHT: "r"}
FROM_LETTER = {v: k for k, v in LETTER.items()}

SOLVED_TIME = 1.5       # seconds the "solved" banner shows before moving on
SCORE_BASE = 1000       # per solved level, minus your best move count...
SCORE_FLOOR = 100       # ...but never less than this

SAVE_VERSION = 1

# Level notation (XSB): '#' wall, ' ' floor, '.' goal, '$' crate, '*' crate
# on a goal, '@' keeper, '+' keeper on a goal. Blanks outside the walls are
# simply not drawn. All levels are original designs for this arcade: the
# first few teach the rules, the rest were composed with a reverse-play
# search (pulling crates away from their goals) and checked by the solver
# in the tests.
LEVELS = (
    # 1: pushes in a straight line
    ('#######',
     '#@ $ .#',
     '#######'),
    # 2: round a corner
    (' #####',
     '##   #',
     '#@$  #',
     '#  # #',
     '####.#',
     '   ###'),
    # 3: two crates
    (' ######',
     '##  . #',
     '#  $  #',
     '# $# .#',
     '#@    #',
     '#######'),
    # 4: two crates
    ('#### ####',
     '#  ###. #',
     '#   ##  #',
     '# $$ #  #',
     '##      #',
     ' ##     #',
     '  #. # @#',
     '  #######'),
    # 5
    ('   #####',
     ' ###   ##',
     '## .    #',
     '#  #   .#',
     '#    #  #',
     '##$# $  #',
     ' #@##   #',
     ' ########'),
    # 6
    ('   ######',
     '   #.  @#',
     '#####   #',
     '#  $    #',
     '#   $.###',
     '#######'),
    # 7
    (' #####',
     '##   ####',
     '#  .   .#',
     '#   #####',
     '# # @ $ #',
     '#  $ ## #',
     '####    #',
     '   ######'),
    # 8
    ('#######',
     '#   @ ##',
     '#  .   #',
     '# .#  $##',
     '#  # $  #',
     '#####   #',
     '    #####'),
    # 9: three crates
    ('   #####',
     '   #.@ #',
     '####.$$##',
     '#   #.  #',
     '#    $  #',
     '#  ###  #',
     '#### ####'),
    # 10
    ('  #####',
     '###  .#',
     '#  .  #####',
     '#  # .  $@#',
     '# $ $ #####',
     '#  ####',
     '####'),
    # 11
    ('##########',
     '#   .  . #',
     '# + $$ $ #',
     '#  # #####',
     '####   #',
     '   #   #',
     '   #   #',
     '   #####'),
    # 12
    ('  #######',
     '  #     #',
     '  #$#   ##',
     '###@###  #',
     '#  $#. . #',
     '#.$    ###',
     '#      #',
     '########'),
    # 13
    ('#########',
     '#.  ##  #',
     '#  .##  #',
     '##      #',
     '#@$ $$  #',
     '###  # .#',
     '  #  #  #',
     '  #######'),
    # 14
    ('   #####',
     '####   ##',
     '#  $  $@#',
     '# . #$ ##',
     '#   .  #',
     '##  .###',
     ' #####'),
    # 15
    ('#######',
     '#...  #',
     '##  @ ###',
     ' #  ##  #',
     '##$$    ###',
     '#   $#    #',
     '#    #    #',
     '###########'),
    # 16: four crates
    ('#####',
     '#   ######',
     '#   #  $.#',
     '#      $ #',
     '## $$ ##+#',
     ' ###. ##.#',
     '   #######'),
    # 17
    ('     ####',
     '     #  ####',
     ' ##### $$@ #',
     '##  $.   $ #',
     '#       ####',
     '#.  #.  .#',
     '##########'),
    # 18
    ('########',
     '#  .#  #',
     '#    $ ####',
     '### $ . $@#',
     '# $ #  ####',
     '#   ###   #',
     '##    .  .#',
     ' ##########'),
    # 19
    ('#######',
     '#     #',
     '#  $$$#',
     '#@##  #',
     '# ##  ##',
     '# ###  ###',
     '#.$    . #',
     '#####.  .#',
     '    ######'),
    # 20
    ('########',
     '#   .. ###',
     '# $# #  .#',
     '#  # # $$##',
     '#.   ###  #',
     '#       $@#',
     '####  #####',
     '   ####'),
    # 21
    ('  #####',
     '###@$.####',
     '#  $  #  #',
     '#   ###  #',
     '#  $  .$ #',
     '#  #  .. #',
     '##########'),
    # 22
    ('     ######',
     '   ### .  #',
     '####     .#',
     '#@$  .   ##',
     '##$ ###  .#',
     '# $$  ##  #',
     '#         #',
     '###########'),
    # 23: five crates
    ('##########',
     '#     .  #',
     '# .#$ ## #',
     '# $# .   #',
     '#$$  $  ##',
     '#.@####  #',
     '#### #  .#',
     '     #   #',
     '     #####'),
    # 24
    ('   #####',
     '   #.  #',
     '   ##. #####',
     '####  $@#. #',
     '#  $$ #$   #',
     '# ..     $ #',
     '######  #  #',
     '     #######'),
    # 25
    ('  ######',
     '  #.  .#',
     '###$$$ ######',
     '#   #   #  .#',
     '# $ #$ @ . ##',
     '##       . #',
     ' ###########'),
    # 26
    ('########',
     '#  ##  #####',
     '# $##  . ..#',
     '#    $$#$  #',
     '# .$   #@###',
     '#####  .##',
     '    #####'),
    # 27
    ('########',
     '#   ...#',
     '# @$$###',
     '#### ####',
     '#  . $  #',
     '#$$     #',
     '#  ######',
     '#.##',
     '###'),
    # 28: six crates
    ('###########',
     '#.     .  #',
     '## $ ..   #',
     ' #######  #',
     '## $ $   ##',
     '#@$ .. $$#',
     '######   #',
     '     #####'),
    # 29
    ('    ####',
     '    # .#',
     '    # $##',
     '#####$ .###',
     '#@$    #  #',
     '#$.$.$  . #',
     '#.###  ####',
     '### ####'),
    # 30
    ('   #######',
     '   # ... #',
     '   #  $  #',
     '###### $###',
     '#  ###$ $@#',
     '#   .  $$ #',
     '#   . ##.##',
     '##########'),
    # 31
    ('    ########',
     '#####    #.#',
     '# $@$  .$# #',
     '# #$## # # #',
     '# #.#  # $$#',
     '#. ##      #',
     '#  .   # ###',
     '#   ####.#',
     '#   #  ###',
     '#####'),
    # 32
    ('     ######',
     '    ##  @ #',
     '#####.$#$ #',
     '#  ## .# ##',
     '#  # $$# ##',
     '## # . $ .#',
     ' #    #  ##',
     ' ###.## $ #',
     '   #.##   #',
     '   ########'),
)


class Level:
    """One parsed level: walls, goals, crate and keeper start, and the
    interior (floor reachable from the keeper)."""

    __slots__ = ("number", "rows", "width", "height", "walls", "goals",
                 "crates", "start", "floor")

    def __init__(self, number: int, rows):
        self.number = number
        self.rows = tuple(rows)
        self.height = len(self.rows)
        self.width = max(len(r) for r in self.rows)
        walls, goals, crates, start = set(), set(), set(), None
        for y, row in enumerate(self.rows):
            for x, c in enumerate(row):
                if c == "#":
                    walls.add((x, y))
                if c in ".*+":
                    goals.add((x, y))
                if c in "$*":
                    crates.add((x, y))
                if c in "@+":
                    start = (x, y)
        self.walls = frozenset(walls)
        self.goals = frozenset(goals)
        self.crates = frozenset(crates)
        self.start = start
        self.floor = frozenset(flood(self.width, self.height, self.walls, start))

    def is_floor(self, cell) -> bool:
        return cell in self.floor


def flood(width: int, height: int, walls, start) -> set:
    """Cells reachable from `start` through non-wall cells. For a
    well-formed (enclosed) level this is the interior; a flood that reaches
    the grid edge means the walls leak."""
    if start is None:
        return set()
    seen = {start}
    todo = [start]
    while todo:
        x, y = todo.pop()
        for dx, dy in DELTAS.values():
            n = (x + dx, y + dy)
            if (n not in seen and n not in walls
                    and 0 <= n[0] < width and 0 <= n[1] < height):
                seen.add(n)
                todo.append(n)
    return seen


LEVEL_SET = tuple(Level(i + 1, rows) for i, rows in enumerate(LEVELS))
N_LEVELS = len(LEVEL_SET)


def level_points(best_moves: int) -> int:
    """Score for one solved level: fewer moves, more points."""
    return max(SCORE_FLOOR, SCORE_BASE - best_moves)


class Phase:
    """A transient state as an explicit logic phase (like tetris' Clearing):
    'solved' shows the banner, then the next level starts. The change
    happens when it completes - via update() or resolve_phases()."""

    __slots__ = ("kind", "duration", "elapsed")

    def __init__(self, kind: str, duration: float):
        self.kind = kind
        self.duration = duration
        self.elapsed = 0.0


class Game:
    """Full game state and every state transition. No I/O."""

    def __init__(self, levels=None):
        self.levels = tuple(levels) if levels is not None else LEVEL_SET
        self.paused = False
        self.reset()

    def reset(self) -> None:
        """A brand-new game: only level 1 unlocked, no bests."""
        self.unlocked = 0            # highest level index you may play
        self.bests: dict = {}        # level index -> [best moves, best pushes]
        self.paused = False
        self.start_level(0)

    def start_level(self, index: int) -> None:
        lvl = self.levels[index]
        self.index = index
        self.player = lvl.start
        self.crates = set(lvl.crates)
        self.history: list = []      # move letters, oldest first
        self.redo_stack: list = []   # undone letters, next redo last
        self.level_time = 0.0
        self.phase: Phase | None = None
        self.last_record = None      # what the last solve improved

    # -- queries -----------------------------------------------------------

    @property
    def level(self) -> Level:
        return self.levels[self.index]

    @property
    def moves(self) -> int:
        return len(self.history)

    @property
    def pushes(self) -> int:
        return sum(1 for c in self.history if c.isupper())

    @property
    def on_goal(self) -> int:
        return len(self.crates & self.level.goals)

    def is_solved(self) -> bool:
        return self.crates == set(self.level.goals)

    @property
    def solved_count(self) -> int:
        return len(self.bests)

    def all_solved(self) -> bool:
        return len(self.bests) == len(self.levels)

    @property
    def score(self) -> int:
        """Sum over solved levels of level_points(best moves)."""
        return sum(level_points(m) for m, _ in self.bests.values())

    def _can_act(self) -> bool:
        return not self.paused and self.phase is None

    # -- movement ----------------------------------------------------------

    def _step(self, d: str):
        """Try one step; returns the history letter, or None if blocked."""
        dx, dy = DELTAS[d]
        px, py = self.player
        target = (px + dx, py + dy)
        if not self.level.is_floor(target):
            return None
        if target in self.crates:
            beyond = (target[0] + dx, target[1] + dy)
            if not self.level.is_floor(beyond) or beyond in self.crates:
                return None
            self.crates.discard(target)
            self.crates.add(beyond)
            self.player = target
            return LETTER[d].upper()
        self.player = target
        return LETTER[d]

    def move(self, d: str) -> bool:
        """Walk (or push) one cell. A new move forgets the redo stack."""
        if not self._can_act() or self.is_solved():
            return False
        letter = self._step(d)
        if letter is None:
            return False
        self.history.append(letter)
        self.redo_stack = []
        self._check_solved()
        return True

    def undo(self) -> bool:
        if not self._can_act() or not self.history:
            return False
        letter = self.history.pop()
        dx, dy = DELTAS[FROM_LETTER[letter.lower()]]
        px, py = self.player
        if letter.isupper():
            self.crates.discard((px + dx, py + dy))
            self.crates.add((px, py))
        self.player = (px - dx, py - dy)
        self.redo_stack.append(letter)
        return True

    def redo(self) -> bool:
        if not self._can_act() or not self.redo_stack or self.is_solved():
            return False
        letter = self.redo_stack[-1]
        if self._step(FROM_LETTER[letter.lower()]) != letter:
            self.redo_stack = []  # cannot happen for a consistent stack
            return False
        self.redo_stack.pop()
        self.history.append(letter)
        self._check_solved()
        return True

    def restart_level(self) -> None:
        """Back to the level's start. The moves made go onto the redo stack,
        so redo can replay them step by step."""
        self.phase = None
        stack = self.redo_stack + list(reversed(self.history))
        self.start_level(self.index)
        self.redo_stack = stack

    def goto_level(self, index: int) -> bool:
        """Start another unlocked level (its own moves are not kept)."""
        if self.paused or not 0 <= index <= self.unlocked or index == self.index:
            return False
        self.start_level(index)
        return True

    def step_level(self, delta: int) -> bool:
        return self.goto_level(self.index + delta)

    def _check_solved(self) -> None:
        if not self.is_solved():
            return
        moves, pushes = self.moves, self.pushes
        old = self.bests.get(self.index)
        if old is None:
            self.bests[self.index] = [moves, pushes]
            self.last_record = "first"
        else:
            better = moves < old[0] or pushes < old[1]
            self.bests[self.index] = [min(old[0], moves), min(old[1], pushes)]
            self.last_record = "best" if better else None
        self.unlocked = max(self.unlocked, min(self.index + 1, len(self.levels) - 1))
        self.phase = Phase("solved", SOLVED_TIME)

    def next_index(self):
        """Where a solve leads: the next level, else the first unsolved one,
        else None (everything solved: stay put)."""
        n = len(self.levels)
        for i in list(range(self.index + 1, n)) + list(range(0, self.index)):
            if i == self.index + 1 or i not in self.bests:
                return i
        return None

    def resolve_phases(self) -> None:
        """Finish a pending transition immediately (quit/save/level keys).
        The resulting state is exactly the one the banner would end in."""
        phase, self.phase = self.phase, None
        if phase is None:
            return
        if phase.kind == "solved":
            nxt = self.next_index()
            if nxt is not None:
                self.start_level(nxt)

    # -- time --------------------------------------------------------------

    def update(self, dt: float) -> bool:
        """Advance by `dt` seconds. True if a phase changed the screen."""
        if self.paused:
            return False
        if self.phase is not None:
            self.phase.elapsed += dt
            if self.phase.elapsed >= self.phase.duration:
                self.resolve_phases()
                return True
            return False
        if not self.is_solved():
            self.level_time += dt
        return False

    # -- save / restore ----------------------------------------------------

    def to_save(self) -> dict:
        """Serializable snapshot. The caller resolves phases first (the UI
        save path does), so a save never holds a transition mid-flight."""
        return {
            "version": SAVE_VERSION,
            "levels": len(self.levels),
            "level": self.index,
            "unlocked": self.unlocked,
            "bests": {str(i): list(b) for i, b in sorted(self.bests.items())},
            "history": "".join(self.history),
            "redo": "".join(self.redo_stack),
            "level_time": self.level_time,
        }

    @classmethod
    def from_save(cls, data, levels=None):
        """Rebuild a saved game, or None for anything corrupt, incompatible,
        or not exactly a save this version wrote. The position is rebuilt by
        replaying the history through the real move rules."""
        try:
            if not isinstance(data, dict) or data.get("version") != SAVE_VERSION:
                return None

            def integer(v, lo=0):
                if isinstance(v, bool) or not isinstance(v, int) or v < lo:
                    raise ValueError(v)
                return v

            game = cls(levels)
            n = len(game.levels)
            if data["levels"] != n:
                return None
            unlocked = integer(data["unlocked"])
            index = integer(data["level"])
            if unlocked >= n or index > unlocked:
                return None
            bests = {}
            raw = data["bests"]
            if not isinstance(raw, dict):
                return None
            for key, value in raw.items():
                i = int(key)
                if not 0 <= i < n or str(i) != key:
                    return None
                if not isinstance(value, list) or len(value) != 2:
                    return None
                moves, pushes = integer(value[0], 1), integer(value[1], 1)
                if pushes > moves:
                    return None
                bests[i] = [moves, pushes]
            history, redo = data["history"], data["redo"]
            if not isinstance(history, str) or not isinstance(redo, str):
                return None
            if any(c.lower() not in FROM_LETTER for c in history + redo):
                return None
            time_ = data["level_time"]
            if isinstance(time_, bool) or not isinstance(time_, (int, float)) or time_ < 0:
                return None

            game.unlocked = unlocked
            game.bests = bests
            game.start_level(index)
            for letter in history:
                if game._step(FROM_LETTER[letter.lower()]) != letter:
                    return None  # the history does not replay: corrupt
                game.history.append(letter)
            game.redo_stack = list(redo)
            game.level_time = float(time_)
            return game
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            return None


TITLE = "Sokoban"

# Every key the game reacts to (upper-case letters work too).
CONTROLS = [
    ("arrows", "walk / push"),
    ("wasd hjkl", "walk too"),
    ("u z", "undo"),
    ("y", "redo"),
    ("r", "restart level"),
    ("[ ] b n", "prev / next level"),
    ("x", "new game (asks)"),
    ("?", "help"),
    ("p", "pause"),
    ("q", "menu"),
]

HELP = [
    ("Goal",
     "You are the warehouse keeper (@@). Push every crate, shown as [], onto "
     "a goal, shown as (), to solve the level. When the last crate lands, the next level "
     "starts. There are %d levels: the first few teach the rules, the "
     "later ones need real planning." % N_LEVELS),
    ("Controls",
     "- arrows (or w a s d / h j k l): walk one cell. Walking into a crate "
     "pushes it one cell, if the cell behind it is free.\n"
     "- u or z: undo the last move. Undo is unlimited, all the way back to "
     "the start of the level.\n"
     "- y: redo a move you undid. Any new move forgets what you could redo.\n"
     "- r: restart the level. Your moves go onto the redo stack, so y can "
     "replay them step by step.\n"
     "- [ and ] (or b and n): step back and forward through the levels you "
     "have unlocked. The level you leave starts afresh next time.\n"
     "- x: new game. It asks first (y to confirm); it wipes every unlocked "
     "level and every best, then starts again at level 1. The all-time best "
     "score stays.\n"
     "- p pauses, q goes back to the arcade menu (your level, moves and "
     "undo history are kept for next time).\n"
     "- ? opens this help."),
    ("Rules",
     "- You can push a crate, never pull it.\n"
     "- Only one crate at a time: a crate with a wall or another crate "
     "behind it will not move.\n"
     "- A crate pushed into a corner (or against a wall with no goal along "
     "it) can never come back out. Undo is your friend.\n"
     "- There is no timer and nothing chases you: take your time. An agent "
     "alert only pauses the game and shows the banner."),
    ("Progress and records",
     "Solving a level unlocks the next one. Each level remembers your best "
     "move count and your best push count separately (the stats box shows "
     "them as 'best moves/pushes'); a solve that improves either says NEW "
     "BEST. The live counters show the moves and pushes of the current try."),
    ("Scoring",
     "Your score is the sum over every solved level of 1000 minus your best "
     "move count for it, but at least 100 per level. Solving more levels "
     "raises it, and so does re-solving a level in fewer moves. The best "
     "score ever reached is kept across new games and shows on the menu."),
    ("Legend",
     "- @@  you, the keeper (magenta when standing on a goal)\n"
     "- []  crate (yellow)\n"
     "- ()  goal, an empty storage spot (magenta)\n"
     "- {}  crate on a goal (green)\n"
     "- solid blocks are walls"),
    ("Tips",
     "- Before pushing, ask where the crate has to end up, and from which "
     "side you will need to push it there.\n"
     "- Fill goals in the far corners and dead ends first: a crate on a "
     "goal near the entrance can block the way to the others.\n"
     "- Crates against a wall can only slide along it: make sure a goal "
     "lies on that line.\n"
     "- Stuck? Undo a few moves rather than restarting."),
]

# Layout: the framed field (16 cells x 2 cols + border = 34 x 14) with, when
# there is room, a side column of stacked boxes: stats, controls, agents.
# Row 0 is the header/banner; everything else is centred as one block below.
FIELD_WIN_W = FIELD_W * 2 + 2
FIELD_WIN_H = FIELD_H + 2
MIN_COLS = FIELD_WIN_W
MIN_ROWS = FIELD_WIN_H + 1
STATS_H = 10
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
    agents box), 'controls' (controls box only; counters in the header),
    'field' (no side column: counters in the header, key hint on the last
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


def level_origin(level: Level):
    """(x, y) field-cell offset that centres `level` in the field."""
    return (FIELD_W - level.width) // 2, (FIELD_H - level.height) // 2


def banner_rows(level: Level):
    """Field-window rows just above and just below `level`, for banners."""
    _, oy = level_origin(level)
    return max(1, oy), min(FIELD_WIN_H - 2, 1 + oy + level.height)


# --------------------------------------------------------------------------
# curses UI
# --------------------------------------------------------------------------

# Colour pairs 40-59 are shared by the later games (one runs at a time);
# they are registered in run() on every start (see common.py).
PAIR_WALL, PAIR_CRATE, PAIR_GOAL, PAIR_DONE, PAIR_KEEPER = 40, 41, 42, 43, 44

WALL_GLYPH = "██"   # every field cell is exactly two columns
CRATE_GLYPH = "[]"
GOAL_GLYPH = "()"
DONE_GLYPH = "{}"   # a crate on a goal
KEEPER_GLYPH = "@@"


def init_colors() -> None:
    bg = default_bg()
    curses.init_pair(PAIR_WALL, curses.COLOR_BLUE, bg)
    curses.init_pair(PAIR_CRATE, curses.COLOR_YELLOW, bg)
    curses.init_pair(PAIR_GOAL, curses.COLOR_MAGENTA, bg)
    curses.init_pair(PAIR_DONE, curses.COLOR_GREEN, bg)
    curses.init_pair(PAIR_KEEPER, curses.COLOR_WHITE, bg)


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


def display_key(game: Game, now: float):
    """Everything on screen that changes with time alone; the loop redraws
    whenever this changes."""
    return (int(game.level_time),)


def draw_field(win, game: Game, confirm: bool = False) -> None:
    lvl = game.level
    ox, oy = level_origin(lvl)
    safe_addstr(win, 0, 2, f" level {lvl.number} ", curses.A_BOLD)
    wall_attr = curses.color_pair(PAIR_WALL)
    goal_attr = curses.color_pair(PAIR_GOAL) | curses.A_BOLD
    for x, y in lvl.walls:
        _put(win, ox + x, oy + y, WALL_GLYPH, wall_attr)
    for x, y in lvl.goals:
        if (x, y) not in game.crates:
            _put(win, ox + x, oy + y, GOAL_GLYPH, goal_attr)
    for x, y in game.crates:
        if (x, y) in lvl.goals:
            _put(win, ox + x, oy + y, DONE_GLYPH,
                 curses.color_pair(PAIR_DONE) | curses.A_BOLD)
        else:
            _put(win, ox + x, oy + y, CRATE_GLYPH,
                 curses.color_pair(PAIR_CRATE) | curses.A_BOLD)
    px, py = game.player
    keeper = PAIR_GOAL if game.player in lvl.goals else PAIR_KEEPER
    _put(win, ox + px, oy + py, KEEPER_GLYPH, curses.color_pair(keeper) | curses.A_BOLD)

    # Banners go in the free rows just above and below the level, so they
    # never hide the warehouse (every level leaves at least one of each).
    above, below = banner_rows(lvl)
    if confirm:
        _centered(win, above, " NEW GAME? ", curses.A_STANDOUT | curses.A_BOLD)
        _centered(win, below, " y wipes progress, n keeps ", curses.A_STANDOUT)
    elif game.paused:
        _centered(win, above, " PAUSED ", curses.A_STANDOUT | curses.A_BOLD)
    elif game.phase is not None and game.phase.kind == "solved":
        _centered(win, above, f" LEVEL {lvl.number} SOLVED ",
                  curses.color_pair(PAIR_DONE) | curses.A_BOLD | curses.A_REVERSE)
        if game.last_record == "best":
            _centered(win, below, " NEW BEST ", curses.color_pair(PAIR_DONE) | curses.A_BOLD)
        else:
            _centered(win, below, f" {game.moves} moves, {game.pushes} pushes ",
                      curses.color_pair(PAIR_DONE) | curses.A_BOLD)
    elif game.is_solved():
        if game.all_solved():
            _centered(win, above, f" ALL {len(game.levels)} LEVELS SOLVED ",
                      curses.color_pair(PAIR_DONE) | curses.A_BOLD | curses.A_REVERSE)
        else:
            _centered(win, above, " solved: [ ] other levels ",
                      curses.color_pair(PAIR_DONE) | curses.A_BOLD)

def draw_stats(win, game: Game, best: int) -> None:
    rows, cols = win.getmaxyx()
    inner = cols - 2
    safe_addstr(win, 0, 2, f" {TITLE} "[: max(0, cols - 3)], curses.A_BOLD)
    n = len(game.levels)
    rec = game.bests.get(game.index)
    if game.paused:
        state = "paused"
    elif game.phase is not None or game.is_solved():
        state = "solved!"
    else:
        state = f"time {int(game.level_time)}s"
    lines = [
        (f"level  {game.index + 1}/{n}" + ("  solved" if rec else ""), curses.A_BOLD),
        (f"moves  {game.moves}", 0),
        (f"pushes {game.pushes}", 0),
        (f"best   {rec[0]}/{rec[1]}" if rec else "best   -", curses.color_pair(PAIR_DONE)),
        (f"crates {game.on_goal}/{len(game.level.goals)} home", curses.color_pair(PAIR_CRATE)),
        (f"score  {game.score}  (best {max(best, game.score)})", 0),
        (f"solved {game.solved_count}/{n}", 0),
        (state, curses.A_DIM),
    ]
    for i, (line, attr) in enumerate(lines):
        if 1 + i < rows - 1:
            safe_addstr(win, 1 + i, 1, line[:inner], attr)


def draw(game: Game, lay: Layout, session, resume: bool, best: int,
         help=None, confirm: bool = False) -> None:
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
        if lay.stats is None:  # no stats box in this tier: counters go here,
            # kept short so the move and push counts survive narrow panes
            head = f" sokoban{' (paused)' if game.paused else ''} "
            if lay.plan["hint"] is None and lay.controls is None:
                head += "| ? help "  # no hint row and no controls box
            head += (f"| lvl {game.index + 1}/{len(game.levels)} | moves {game.moves}"
                     f" | pushes {game.pushes} | score {game.score} ")
        else:
            head = f" sokoban - {'paused' if game.paused else 'playing'} "
        safe_addstr(scr, 0, 0, head[: lay.cols - 1], curses.A_STANDOUT)

    lay.boxes()
    draw_field(lay.field, game, confirm)
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

UNDO_KEYS = tuple(ord(c) for c in "uUzZ")
REDO_KEYS = tuple(ord(c) for c in "yY")
PREV_KEYS = tuple(ord(c) for c in "[bB")
NEXT_KEYS = tuple(ord(c) for c in "]nN")
NEW_GAME_KEYS = tuple(ord(c) for c in "xX")


ESC = 27
ESC_ARROWS = {ord("A"): curses.KEY_UP, ord("B"): curses.KEY_DOWN,
              ord("C"): curses.KEY_RIGHT, ord("D"): curses.KEY_LEFT}


def read_key(getch):
    """Next key from `getch`, with any escape sequence curses could not
    decode swallowed whole: such a sequence arrives as ESC followed by its
    bytes (ESC [ D for a left arrow on a terminal ignoring keypad mode, or
    a focus report), and its '[' must never reach the level keys. Plain
    arrow sequences are mapped to the arrow keys; a lone ESC comes back as
    ESC; -1 means no key (or only a swallowed sequence)."""
    ch = getch()
    if ch != ESC:
        return ch
    nxt = getch()
    if nxt == -1:
        return ESC
    if nxt not in (ord("["), ord("O")):
        return nxt  # ESC then an ordinary key: keep the key
    params = 0
    while True:
        ch = getch()
        if ch == -1:
            return -1
        if 0x40 <= ch <= 0x7E:  # CSI/SS3 final byte ends it
            # A plain arrow still moves (terminals that ignore keypad mode).
            return ESC_ARROWS.get(ch, -1) if not params else -1
        params += 1


class LoopState:
    """The run loop's pause bookkeeping: which pause came from what, so each
    source lifts only its own. `confirm` is the new-game prompt."""

    def __init__(self, resume=False):
        self.resume = resume
        self.paused_by_resume = resume
        self.paused_by_attention = False
        self.paused_by_help = False
        self.confirm = False


def dispatch_key(ch, game, help, st, session):
    """One key from the run loop (KEY_RESIZE is handled before this).
    Returns 'quit', 'restart', 'new' or None; the caller redraws after any
    key. 'restart' restarts the level; 'new' is a confirmed new game."""
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
        if not game.paused:
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
        st.confirm = False
        return "quit"
    if st.confirm:
        # The new-game prompt takes the next key: y confirms, anything
        # else (n, esc, a move) cancels without acting.
        st.confirm = False
        return "new" if ch in (ord("y"), ord("Y")) else None
    if ch in (ord("r"), ord("R")):
        return "restart"
    if ch in (ord("p"), ord("P")):
        game.paused = not game.paused
        st.paused_by_attention = st.paused_by_resume = False
    elif game.paused:
        return None
    elif ch in KEY_DIRS:
        game.move(KEY_DIRS[ch])
    elif ch in UNDO_KEYS:
        game.undo()
    elif ch in REDO_KEYS:
        game.redo()
    elif ch in NEXT_KEYS and game.phase is not None:
        game.resolve_phases()  # skip the "solved" banner: on to the next
    elif ch in PREV_KEYS or ch in NEXT_KEYS:
        game.phase = None
        game.step_level(1 if ch in NEXT_KEYS else -1)
    elif ch in NEW_GAME_KEYS:
        st.confirm = True
    return None


def run(session) -> None:
    """Play Sokoban until `q` (back to the arcade menu) or SIGTERM. The game
    is saved on every exit path and resumed, paused, on the next run."""
    scr = session.stdscr
    init_colors()
    game_path = session.path("sokoban-game.json")
    hiscore_path = session.path("sokoban-hiscore.json")
    hiscore = load_hiscore(hiscore_path)

    # A saved game resumes exactly where it left off -- paused, behind a
    # banner. Corrupt/incompatible saves are ignored silently.
    game = Game()
    st = LoopState()
    if game_path:
        restored = Game.from_save(load_json_file(game_path))
        if restored is not None:
            game = restored
            game.paused = True
            st = LoopState(resume=True)

    help = HelpOverlay(TITLE, HELP)
    lay = Layout(scr)
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
            if alerts and session.should_auto_pause:
                if not game.paused:
                    game.paused = True
                    st.paused_by_attention = True
                dirty = True

            quit_now = False
            while True:
                ch = read_key(scr.getch)
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
                    # Restart only resets the level: progress, bests and
                    # the save all stay.
                    game.restart_level()
                elif result == "new":
                    # A new game wipes progress and the save; the all-time
                    # best score stays.
                    if game_path:
                        remove_path(game_path)
                    game.reset()
                    st.paused_by_attention = st.paused_by_resume = False
            if quit_now:
                break

            if game.update(dt):
                dirty = True
            # The seconds counter redraws whenever it changes.
            key = display_key(game, now)
            if key != last_key:
                last_key = key
                dirty = True

            if game.score > hiscore:
                hiscore = game.score
                save_hiscore(hiscore_path, hiscore)
                dirty = True

            if dirty:
                draw(game, lay, session, st.resume, hiscore, help, st.confirm)
                scr.noutrefresh()
                curses.doupdate()
                dirty = False

            time.sleep(0.01)
    finally:
        # The one save point: q, SIGTERM, or an exception lands here. A
        # pending "solved" transition is resolved first so the save is a
        # consistent, resumable state.
        game.resolve_phases()
        if game_path:
            atomic_save_json(game_path, game.to_save())
        if hiscore_path and game.score > hiscore:
            save_hiscore(hiscore_path, game.score)


if __name__ == "__main__":
    import sys

    import arcade

    sys.exit(arcade.main(["--game", "sokoban"] + sys.argv[1:]))
