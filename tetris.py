#!/usr/bin/env python3
"""Tetris for the Herdr arcade (see arcade.py, which runs it via `run()`).

Structure: everything above the "curses UI" divider is pure game logic —
board, pieces, bag, scoring — with no terminal dependency, so
`tests/test_game.py` imports this module without a terminal. Agent
awareness, state files and the shared curses helpers live in common.py.
"""

from __future__ import annotations

import curses
import random
import sys
import time

from common import (  # noqa: F401  (agent names re-exported for the tests)
    ATTENTION_STATES,
    AgentSnapshot,
    HELP_KEY,
    AgentTracker,
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
    normalize_agent_list,
    remove_path,
    safe_addstr,
    save_hiscore,
    side_width,
)

# --------------------------------------------------------------------------
# Pure game logic
# --------------------------------------------------------------------------

BOARD_WIDTH = 10
BOARD_HEIGHT = 20

# SRS cell offsets per kind per rotation state (0, R, 2, L). x grows right,
# y grows down; offsets are relative to a 4x4 box whose top-left sits at the
# piece's (x, y). States advance clockwise.
SHAPES: dict[str, tuple[tuple[tuple[int, int], ...], ...]] = {
    "I": (
        ((0, 1), (1, 1), (2, 1), (3, 1)),
        ((2, 0), (2, 1), (2, 2), (2, 3)),
        ((0, 2), (1, 2), (2, 2), (3, 2)),
        ((1, 0), (1, 1), (1, 2), (1, 3)),
    ),
    "O": (
        ((1, 0), (2, 0), (1, 1), (2, 1)),
    ) * 4,
    "T": (
        ((1, 0), (0, 1), (1, 1), (2, 1)),
        ((1, 0), (1, 1), (2, 1), (1, 2)),
        ((0, 1), (1, 1), (2, 1), (1, 2)),
        ((1, 0), (0, 1), (1, 1), (1, 2)),
    ),
    "S": (
        ((1, 0), (2, 0), (0, 1), (1, 1)),
        ((1, 0), (1, 1), (2, 1), (2, 2)),
        ((1, 1), (2, 1), (0, 2), (1, 2)),
        ((0, 0), (0, 1), (1, 1), (1, 2)),
    ),
    "Z": (
        ((0, 0), (1, 0), (1, 1), (2, 1)),
        ((2, 0), (1, 1), (2, 1), (1, 2)),
        ((0, 1), (1, 1), (1, 2), (2, 2)),
        ((1, 0), (0, 1), (1, 1), (0, 2)),
    ),
    "J": (
        ((0, 0), (0, 1), (1, 1), (2, 1)),
        ((1, 0), (2, 0), (1, 1), (1, 2)),
        ((0, 1), (1, 1), (2, 1), (2, 2)),
        ((1, 0), (1, 1), (0, 2), (1, 2)),
    ),
    "L": (
        ((2, 0), (0, 1), (1, 1), (2, 1)),
        ((1, 0), (1, 1), (1, 2), (2, 2)),
        ((0, 1), (1, 1), (2, 1), (0, 2)),
        ((0, 0), (1, 0), (1, 1), (1, 2)),
    ),
}
PIECE_KINDS = ("I", "O", "T", "S", "Z", "J", "L")

LINE_SCORES = {1: 100, 2: 300, 3: 500, 4: 800}
LINES_PER_LEVEL = 10

# Basic wall kicks tried, in order, when a rotation collides: nudge left/right,
# then a one-row floor kick.
WALL_KICKS = ((0, 0), (-1, 0), (1, 0), (-2, 0), (2, 0), (0, -1))

SPAWN_X = 3
SPAWN_Y = 0

# Save format version: a mismatch (either direction) means "ignore silently".
SAVE_VERSION = 1

# Line-clear animation durations (seconds): a Tetris gets a longer, visually
# distinct sweep; 1-3 lines flash briefly.
CLEAR_DURATION = 0.25
TETRIS_CLEAR_DURATION = 0.35


def drop_interval(level: int) -> float:
    """Seconds per gravity row at `level`: faster each level, 0.05s floor."""
    return max(0.05, 0.8 ** (level - 1))


class Bag:
    """7-bag randomizer: every kind appears exactly once per 7 draws."""

    def __init__(self, rng=None, queue=None):
        self._rng = rng if rng is not None else random.Random()
        self._bag: list[str] = list(queue) if queue is not None else []

    def _ensure(self, n: int) -> None:
        while len(self._bag) < n:
            refill = list(PIECE_KINDS)
            self._rng.shuffle(refill)
            self._bag.extend(refill)

    def next(self) -> str:
        self._ensure(1)
        return self._bag.pop(0)

    def peek(self, n: int) -> list[str]:
        self._ensure(n)
        return self._bag[:n]

    @property
    def queue(self) -> list[str]:
        """Remaining draw order (front first); for save/restore."""
        return list(self._bag)


class Piece:
    __slots__ = ("kind", "x", "y", "rot")

    def __init__(self, kind: str, x: int = SPAWN_X, y: int = SPAWN_Y, rot: int = 0):
        self.kind = kind
        self.x = x
        self.y = y
        self.rot = rot % 4

    def cells(self) -> list[tuple[int, int]]:
        return [(self.x + dx, self.y + dy) for dx, dy in SHAPES[self.kind][self.rot]]

    def moved(self, dx: int, dy: int) -> "Piece":
        return Piece(self.kind, self.x + dx, self.y + dy, self.rot)

    def rotated(self, steps: int = 1) -> "Piece":
        return Piece(self.kind, self.x, self.y, self.rot + steps)


class Board:
    """10x20 grid of filled-cell kinds (or None)."""

    def __init__(self, width: int = BOARD_WIDTH, height: int = BOARD_HEIGHT):
        self.width = width
        self.height = height
        self.grid: list[list[str | None]] = [[None] * width for _ in range(height)]

    def collides(self, cells) -> bool:
        for x, y in cells:
            if x < 0 or x >= self.width or y >= self.height:
                return True
            if y >= 0 and self.grid[y][x] is not None:
                return True
        return False

    def lock(self, piece: Piece) -> bool:
        """Write the piece into the grid. Returns True on lock-out (any cell
        settled above row 0), which callers treat as game over."""
        locked_out = False
        for x, y in piece.cells():
            if y < 0:
                locked_out = True
            else:
                self.grid[y][x] = piece.kind
        return locked_out

    def full_rows(self) -> list[int]:
        return [y for y in range(self.height) if all(self.grid[y])]

    def clear_rows(self, rows) -> None:
        for y in sorted(rows):
            del self.grid[y]
            self.grid.insert(0, [None] * self.width)


class Clearing:
    """The line-clear animation as an explicit logic phase: the full rows sit
    in the grid (uncollapsed) while `elapsed` advances toward `duration`; the
    collapse (row removal, scoring, level, next spawn) happens when the phase
    completes — via update(), or immediately via finish_clearing()."""

    __slots__ = ("rows", "count", "elapsed", "duration")

    def __init__(self, rows, duration):
        self.rows = sorted(rows)
        self.count = len(rows)
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

    def __init__(self, rng: random.Random | None = None):
        self.rng = rng if rng is not None else random.Random()
        self.bag = Bag(self.rng)
        self.board = Board()
        self.score = 0
        self.lines = 0
        self.level = 1
        self.hold: str | None = None
        self.hold_used = False
        self.paused = False
        self.game_over = False
        self.piece: Piece | None = None
        self.clearing: Clearing | None = None
        self._drop_timer = 0.0
        self._input_buffer: list = []
        self._spawn()

    def reset(self) -> None:
        self.__init__(self.rng)

    # -- save / restore ----------------------------------------------------

    def to_save(self) -> dict:
        """Serializable snapshot. Always taken post-clear: the caller is
        responsible for finish_clearing() first (the UI save path does)."""
        piece = self.piece
        return {
            "version": SAVE_VERSION,
            "grid": [row[:] for row in self.board.grid],
            "piece": None if piece is None else {
                "kind": piece.kind, "x": piece.x, "y": piece.y, "rot": piece.rot,
            },
            "hold": self.hold,
            "hold_used": self.hold_used,
            "bag": self.bag.queue,
            "rng": _rng_to_json(self.rng),
            "score": self.score,
            "lines": self.lines,
            "level": self.level,
        }





    @classmethod
    def from_save(cls, data, rng=None):
        """Rebuild a saved game, or return None for anything corrupt,
        incompatible, or not exactly a save this version wrote."""
        try:
            if not isinstance(data, dict) or data.get("version") != SAVE_VERSION:
                return None
            game = cls(rng)
            grid = data["grid"]
            if not isinstance(grid, list) or len(grid) != game.board.height:
                return None
            for row in grid:
                if not isinstance(row, list) or len(row) != game.board.width:
                    return None
                for cell in row:
                    if cell is not None and not (
                        isinstance(cell, str) and cell in SHAPES
                    ):
                        return None
            bag = data["bag"]
            if not isinstance(bag, list) or any(k not in SHAPES for k in bag):
                return None
            pd = data["piece"]
            if pd is not None:
                if not isinstance(pd, dict) or pd["kind"] not in SHAPES:
                    return None
                piece = Piece(pd["kind"], pd["x"], pd["y"], pd["rot"])
            else:
                piece = None
            game.board.grid = [[c for c in row] for row in grid]
            game.bag = Bag(game.rng, bag)
            game.piece = piece
            game.hold = data["hold"] if data["hold"] in SHAPES else None
            game.hold_used = bool(data["hold_used"])
            game.score = int(data["score"])
            game.lines = int(data["lines"])
            game.level = int(data["level"])
            # Restore the RNG state so the bag refills identically too — the
            # saved queue alone would diverge at the next 7-bag refill. A
            # present-but-malformed rng field means a corrupt save.
            if "rng" in data:
                state = _rng_from_json(data.get("rng"))
                if state is None:
                    return None
                game.rng.setstate(state)
            if game.piece is not None and game.board.collides(game.piece.cells()):
                return None  # a save whose piece intersects the stack is corrupt
            return game
        except (KeyError, TypeError, ValueError, IndexError):
            return None

    # -- spawn / movement --------------------------------------------------

    def _spawn(self, kind: str | None = None) -> None:
        self.piece = Piece(kind if kind is not None else self.bag.next())
        self._drop_timer = 0.0
        if self.board.collides(self.piece.cells()):
            self.game_over = True

    def _buffer(self, action) -> None:
        """Hold one input during the clear animation so it is neither lost
        nor double-applied; replayed on the freshly spawned piece."""
        if len(self._input_buffer) < 8:
            self._input_buffer.append(action)

    def _flush_input(self) -> None:
        buffered, self._input_buffer = self._input_buffer, []
        for action in buffered:
            action()

    def move(self, dx: int) -> bool:
        if self.clearing is not None:
            self._buffer(lambda: self.move(dx))
            return False
        if self.game_over or self.piece is None:
            return False
        cand = self.piece.moved(dx, 0)
        if not self.board.collides(cand.cells()):
            self.piece = cand
            return True
        return False

    def rotate(self, steps: int = 1) -> bool:
        if self.clearing is not None:
            self._buffer(lambda: self.rotate(steps))
            return False
        if self.game_over or self.piece is None:
            return False
        target = self.piece.rotated(steps)
        for dx, dy in WALL_KICKS:
            cand = target.moved(dx, dy)
            if not self.board.collides(cand.cells()):
                self.piece = cand
                return True
        return False


    def drop_distance(self) -> int:
        """Rows the current piece can still fall."""
        if self.piece is None:
            return 0
        d = 0
        while not self.board.collides(self.piece.moved(0, d + 1).cells()):
            d += 1
        return d

    def ghost(self) -> Piece | None:
        if self.piece is None:
            return None
        return self.piece.moved(0, self.drop_distance())

    def soft_drop(self) -> bool:
        """One row down for +1 point; locks when it cannot fall. Returns True
        whenever the game state changed."""
        if self.game_over or self.piece is None:
            return False
        if self._step_down():
            self.score += 1
        return True

    def _step_down(self) -> bool:
        assert self.piece is not None
        cand = self.piece.moved(0, 1)
        if not self.board.collides(cand.cells()):
            self.piece = cand
            return True
        self._lock_piece()
        return False

    def hard_drop(self) -> int:
        """Drop to rest for +2 points per row, then lock. Returns rows fallen."""
        if self.game_over or self.piece is None:
            return 0
        d = self.drop_distance()
        self.piece = self.piece.moved(0, d)
        self.score += 2 * d
        self._lock_piece()
        return d

    def hold_swap(self) -> bool:
        if self.clearing is not None:
            self._buffer(self.hold_swap)
            return False
        if self.game_over or self.piece is None or self.hold_used:
            return False
        current = self.piece.kind
        if self.hold is None:
            self.hold = current
            self._spawn()
        else:
            held = self.hold
            self.hold = current
            self._spawn(held)
        self.hold_used = True
        return True

    def _lock_piece(self) -> None:
        assert self.piece is not None
        locked_out = self.board.lock(self.piece)
        rows = self.board.full_rows()
        self.hold_used = False
        if locked_out:
            self.game_over = True
            self.board.clear_rows(rows)
            self.clearing = None
            return
        if rows:
            # Enter the clear-animation phase: rows stay in the grid until
            # the phase completes; no piece is active meanwhile.
            duration = TETRIS_CLEAR_DURATION if len(rows) >= 4 else CLEAR_DURATION
            self.clearing = Clearing(rows, duration)
            self.piece = None
            self._drop_timer = 0.0
        else:
            self._spawn()

    def _apply_clear(self, rows) -> None:
        n = len(rows)
        self.board.clear_rows(rows)
        self.lines += n
        self.score += LINE_SCORES.get(n, 0) * self.level
        self.level = 1 + self.lines // LINES_PER_LEVEL

    def finish_clearing(self) -> None:
        """Resolve a pending clear immediately (pause, quit, save). The state
        afterwards is exactly the post-clear state."""
        if self.clearing is None:
            return
        rows = self.clearing.rows
        self.clearing = None
        self._apply_clear(rows)
        self._spawn()
        self._flush_input()

    # -- time --------------------------------------------------------------

    def update(self, dt: float) -> bool:
        """Advance the game by `dt` seconds: the clear animation while it is
        pending, gravity otherwise. True if anything changed."""
        if self.paused or self.game_over:
            return False
        if self.clearing is not None:
            self.clearing.elapsed += dt
            if self.clearing.elapsed >= self.clearing.duration:
                self.finish_clearing()
            return True
        if self.piece is None:
            return False
        self._drop_timer += dt
        interval = drop_interval(self.level)
        changed = False
        while self._drop_timer >= interval:
            self._drop_timer -= interval
            # Firing gravity always changes state: the piece either falls a
            # row or locks and the next one spawns.
            changed = True
            if not self._step_down() or self.game_over:
                break
        return changed


# --------------------------------------------------------------------------
# curses UI
# --------------------------------------------------------------------------

PIECE_COLORS = {
    "I": curses.COLOR_CYAN,
    "O": curses.COLOR_YELLOW,
    "T": curses.COLOR_MAGENTA,
    "S": curses.COLOR_GREEN,
    "Z": curses.COLOR_RED,
    "J": curses.COLOR_BLUE,
    "L": curses.COLOR_WHITE,
}
CELL = "██"  # one board cell, two columns wide so cells look square
GHOST = "░░"

TITLE = "Tetris"

# Every key the game reacts to (upper case works too). The controls box in
# the side column lists these; narrower tiers show a one-line hint instead.
CONTROLS = [
    ("< > h l", "move left / right"),
    ("v j", "soft drop"),
    ("^ k", "rotate"),
    ("space", "hard drop"),
    ("c", "hold piece"),
    ("?", "help"),
    ("p / r", "pause / restart"),
    ("q", "menu"),
]

HELP = [
    ("Goal",
     "Steer the falling pieces so they complete solid horizontal rows. A "
     "full row disappears and everything above it drops down. Keep the "
     "stack from reaching the top for as long as you can and chase a high "
     "score."),
    ("Controls",
     "- left / right arrow (or h / l): move the piece one column.\n"
     "- down arrow (or j): soft drop, one row down (+1 point). If the piece "
     "cannot fall any further it locks in place at once.\n"
     "- up arrow (or k): rotate clockwise. If the turned piece would hit a "
     "wall or the stack it is nudged sideways (or up one row) to fit.\n"
     "- space: hard drop, straight down to the ghost and lock (+2 points "
     "per row).\n"
     "- c: hold. Puts the current piece in the hold box and brings out the "
     "held one (or the next piece). Once per piece: it recharges when a "
     "piece locks.\n"
     "- p pause, r restart (abandons the game, keeps your best), "
     "q back to the menu (the game is saved and comes back paused), "
     "? this help."),
    ("How it works",
     "The well is 10 columns by 20 rows. Pieces come from a 7-bag: all "
     "seven shapes (I O T S Z J L) appear once, in random order, before "
     "any repeats. The next box shows what is coming; the dim ghost on the "
     "board shows where the piece will land.\n"
     "Pieces fall on their own and there is no lock delay: a piece locks "
     "the moment it tries to fall and cannot. Every 10 cleared lines the "
     "level goes up and the pieces fall faster.\n"
     "The game ends when a new piece has no room to appear, or a piece "
     "locks above the top of the well."),
    ("Scoring",
     "- 1 line 100, 2 lines 300, 3 lines 500, 4 lines (a Tetris) 800, "
     "each times the current level.\n"
     "- soft drop +1 per row, hard drop +2 per row.\n"
     "Your best score is kept between games."),
    ("Tips",
     "- Keep the stack flat and leave one column open for the long I "
     "piece: a Tetris scores far more than four single lines.\n"
     "- Use hold to save an I piece for that open column, or to park a "
     "piece that does not fit right now.\n"
     "- Watch the next box and plan two pieces ahead.\n"
     "- Hard drop saves time at higher levels and scores double."),
]

# Adaptive layout. The popup's inner area is 93x28 (common.POPUP_COLS/ROWS);
# the composition is centred in it at every size:
#   hold/next column (14) + 1 + board (22) + 1 + side column (up to 30)
# The side column stacks the stats box, the controls box and the agents box,
# all as tall as the board. Narrower panes drop the side column (the header
# then carries the score and a key hint is shown on the last row or in the
# header), then the hold/next column; below the board itself the too-small
# message appears. The board window is exactly 10 cells x 2 columns + 2
# border = 22 columns wide, so the frame hugs the field in every tier.
BOARD_W = 22                       # 10 cells * 2 columns + 2 border columns
BOARD_H = 22                       # 20 rows + 2 border rows
MIN_COLS, MIN_ROWS = 24, BOARD_H + 1   # board plus the header row
LEFT_W = 14
HOLD_H = 5
MID_COLS = LEFT_W + 1 + BOARD_W    # 37: hold/next column fits
STATS_H = 6                        # four stat lines + frame
MAX_PREVIEWS = 5


def key_hint(width: int) -> str:
    """The full key hint if it fits `width`, else as many entries as fit,
    always starting with '? help'."""
    full = controls_hint(CONTROLS)
    if len(full) <= width:
        return full
    ordered = [c for c in CONTROLS if c[0] == "?"] + [c for c in CONTROLS if c[0] != "?"]
    picked = []
    for entry in ordered:
        if picked and len(controls_hint(picked + [entry])) > width:
            continue
        picked.append(entry)
    return controls_hint(picked)[:max(0, width)]


def side_stack(y: int, x: int, w: int, h: int, stats_h: int, controls_h: int):
    """Stack stats / controls / agents boxes in an h-row side column.
    Priority when short: controls > stats > agents. Returns three regions
    (or None each)."""
    ctrl = min(controls_h, h)
    rest = h - ctrl
    stats = min(stats_h, rest) if rest >= 3 else 0
    rest -= stats
    agents = rest if rest >= 4 else 0  # frame + one agent + '+N more'
    if not agents:  # nothing worth an agents box: give the rows away
        if stats:
            stats += rest
        else:
            ctrl += rest
    regions = [None, None, None]
    top = y
    for i, height in enumerate((stats, ctrl, agents)):
        if height:
            regions[i] = (top, x, height, w)
            top += height
    return regions


def layout_plan(cols: int, rows: int) -> dict:
    """Pure layout decision for a (cols, rows) screen: tier flags plus each
    region's (y, x, height, width). Unit-tested; Layout() only realizes it.
    Tiers: 'full' (hold/next + board + side column), 'left' (hold/next +
    board), 'board', 'too_small'."""
    plan = {
        "tier": "too_small",
        "too_small": rows < MIN_ROWS or cols < MIN_COLS,
        "header": True,
        "left": False,
        "agents": False,
        "board": None,
        "hold": None,
        "next": None,
        "stats": None,
        "controls": None,
        "agents_win": None,
        "hint": None,
        "hint_in_header": False,
    }
    if plan["too_small"]:
        return plan
    left = cols >= MID_COLS
    side_w = side_width(cols, MID_COLS) if left else 0
    comp_w = (MID_COLS if left else BOARD_W) + (1 + side_w if side_w else 0)
    plan["left"] = left
    plan["tier"] = "full" if side_w else ("left" if left else "board")

    usable_rows = rows
    if not side_w:  # no controls box: a key hint must stay visible
        if rows >= 1 + BOARD_H + 1:
            plan["hint"] = (rows - 1, 0, 1, cols)
            usable_rows = rows - 1
        else:
            plan["hint_in_header"] = True
    y0, x = center_origin(cols, usable_rows, comp_w, BOARD_H)
    if left:
        plan["hold"] = (y0, x, HOLD_H, LEFT_W)
        plan["next"] = (y0 + HOLD_H, x, BOARD_H - HOLD_H, LEFT_W)
        x += LEFT_W + 1
    plan["board"] = (y0, x, BOARD_H, BOARD_W)
    x += BOARD_W + 1
    if side_w:
        stats, ctrl, agents = side_stack(
            y0, x, side_w, BOARD_H, STATS_H, controls_height(CONTROLS, side_w))
        plan["stats"], plan["controls"], plan["agents_win"] = stats, ctrl, agents
        plan["agents"] = agents is not None
    return plan


REGIONS = ("board", "hold", "next", "stats", "controls", "agents_win")


class Layout:
    """Screen regions, chosen by size tier; rebuilt on resize."""

    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.rows, self.cols = stdscr.getmaxyx()
        self.plan = layout_plan(self.cols, self.rows)
        for name, flag in self.plan.items():
            setattr(self, name, flag)
        if self.too_small:
            return
        for name in REGIONS:
            if self.plan[name] is not None:
                y, x, h, w = self.plan[name]
                setattr(self, name, stdscr.derwin(h, w, y, x))
        self.board_y, self.board_x = self.plan["board"][:2]

    def boxes(self):
        for name in REGIONS:
            win = getattr(self, name)
            if win is not None:
                win.erase()
                frame(win)


PIECE_PAIR_BASE = 10  # pairs 10-16; see the numbering note in common.py


def init_colors() -> dict:
    """Register the piece colours; returns {kind: pair number}."""
    bg = default_bg()
    pairs = {}
    for i, (kind, color) in enumerate(PIECE_COLORS.items()):
        curses.init_pair(PIECE_PAIR_BASE + i, color, bg)
        pairs[kind] = PIECE_PAIR_BASE + i
    return pairs


def draw_preview(win, kind, row, inner_w, piece_pairs):
    """Draw a piece's spawn orientation centered on `row` of a box interior."""
    if kind is None:
        return
    cells = SHAPES[kind][0]
    min_x = min(x for x, _ in cells)
    min_y = min(y for _, y in cells)
    max_x = max(x for x, _ in cells)
    w = (max_x - min_x + 1) * 2
    x0 = (inner_w - w) // 2
    for x, dy in cells:
        safe_addstr(win, row + (dy - min_y), x0 + (x - min_x) * 2, CELL,
                    curses.color_pair(piece_pairs[kind]))


def draw_header(scr, cols, head, attr, hint_in_header):
    """Row-0 header; in tiers with no controls box and no spare row the key
    hint shares it (always keeping '? help' on screen)."""
    room = cols - 1
    if not hint_in_header:
        safe_addstr(scr, 0, 0, head[:room], attr)
        return
    hint_room = room - len(head) - 1
    hint = key_hint(hint_room) if hint_room >= len("? help") else "? help"
    head = head[: max(0, room - len(hint) - 1)]
    safe_addstr(scr, 0, 0, head, attr)
    safe_addstr(scr, 0, len(head) + 1, hint[: max(0, room - len(head) - 1)],
                curses.A_DIM)


def draw(piece_pairs, status_pairs, game, lay, roster, attention, resume=False,
         best=0, help_view=None):
    scr = lay.stdscr
    scr.erase()

    if lay.too_small:
        msg = f"pane too small: need {MIN_COLS}x{MIN_ROWS}, have {lay.cols}x{lay.rows}"
        safe_addstr(scr, lay.rows // 2, max(0, (lay.cols - len(msg)) // 2),
                    msg[: max(0, lay.cols - 1)], curses.A_BOLD)
        msg2 = "resize the pane (? help, q menu)"
        safe_addstr(scr, lay.rows // 2 + 1, max(0, (lay.cols - len(msg2)) // 2),
                    msg2[: max(0, lay.cols - 1)])
        if help_view is not None:
            help_view.draw(scr)
        return

    # -- header / attention banner ------------------------------------------
    if not draw_banner(scr, lay.cols, attention, resume):
        if game.game_over:
            head = " tetris - GAME OVER "
        else:
            state = "paused" if game.paused else "playing"
            head = f" tetris - {state} "
        if lay.stats is None:  # no stats box in this tier: score goes here
            head += f"score {game.score} | lvl {game.level} | lines {game.lines} "
            if lay.hint_in_header and len(head) + 7 > lay.cols - 1:
                # Narrow: the key hint needs room too; the board shows
                # PAUSED / GAME OVER itself, so the state can go.
                head = f" score {game.score} lvl {game.level} lines {game.lines} "
        draw_header(scr, lay.cols, head,
                    curses.A_STANDOUT | (curses.A_BOLD if game.game_over else 0),
                    lay.hint_in_header)
    elif lay.hint_in_header:
        # The banner owns row 0; keep '? help' visible at its right end.
        safe_addstr(scr, 0, max(0, lay.cols - 1 - len(" ? help")), " ? help",
                    curses.A_DIM)

    # -- board ---------------------------------------------------------------
    lay.boxes()
    bw = lay.board
    ghost = game.ghost()
    if ghost is not None and not game.game_over:
        for x, y in ghost.cells():
            if y >= 0:
                safe_addstr(bw, 1 + y, 1 + x * 2, GHOST,
                            curses.color_pair(piece_pairs[ghost.kind]) | curses.A_DIM)
    if game.piece is not None:
        for x, y in game.piece.cells():
            if y >= 0:
                safe_addstr(bw, 1 + y, 1 + x * 2, CELL,
                            curses.color_pair(piece_pairs[game.piece.kind]))
    for y in range(game.board.height):
        for x in range(game.board.width):
            k = game.board.grid[y][x]
            if k is not None:
                safe_addstr(bw, 1 + y, 1 + x * 2, CELL,
                            curses.color_pair(piece_pairs[k]))

    # -- clear animation -----------------------------------------------------
    # 1-3 rows flash on/off; a Tetris sweeps away left-to-right. The rows are
    # still in the grid (the logic phase has not collapsed them yet).
    if game.clearing is not None:
        cl = game.clearing
        if cl.count >= 4:
            hidden = int((cl.elapsed / cl.duration) * game.board.width)
            for y in cl.rows:
                for x in range(min(hidden, game.board.width)):
                    safe_addstr(bw, 1 + y, 1 + x * 2, "  ")
        elif int(cl.elapsed * 12) % 2 == 0:
            for y in cl.rows:
                for x in range(game.board.width):
                    safe_addstr(bw, 1 + y, 1 + x * 2, "  ")

    # -- game-over / pause overlay --------------------------------------------
    mid_y = 9
    if game.game_over:
        text = " GAME OVER "
        safe_addstr(bw, mid_y, (BOARD_W - len(text)) // 2, text,
                    curses.A_STANDOUT | curses.A_BOLD)
        text2 = " r restart | q menu "
        safe_addstr(bw, mid_y + 1, (BOARD_W - len(text2)) // 2, text2,
                    curses.A_STANDOUT)
    elif game.paused and not (help_view is not None and help_view.is_open):
        text = " PAUSED "
        safe_addstr(bw, mid_y, (BOARD_W - len(text)) // 2, text,
                    curses.A_STANDOUT | curses.A_BOLD)

    # -- hold / next -----------------------------------------------------------
    if lay.left:
        lw = lay.hold
        safe_addstr(lw, 0, 2, " hold ", curses.A_BOLD)
        draw_preview(lw, game.hold, 2, LEFT_W - 2, piece_pairs)

        nw = lay.next
        safe_addstr(nw, 0, 2, " next ", curses.A_BOLD)
        n_rows = nw.getmaxyx()[0]
        count = max(1, min(MAX_PREVIEWS, (n_rows - 2) // 3))
        for i, kind in enumerate(game.bag.peek(count)):
            draw_preview(nw, kind, 2 + i * 3, LEFT_W - 2, piece_pairs)

    # -- side column: stats / controls / agents --------------------------------
    if lay.stats is not None:
        sw = lay.stats
        s_rows, s_cols = sw.getmaxyx()
        safe_addstr(sw, 0, 2, f" {TITLE.lower()} "[: max(0, s_cols - 3)],
                    curses.A_BOLD)
        lines = [
            (f"score {game.score}", curses.A_BOLD),
            (f"best  {max(best, game.score)}", 0),
            (f"level {game.level}", 0),
            (f"lines {game.lines}", 0),
        ]
        for i, (line, attr) in enumerate(lines[: max(0, s_rows - 2)]):
            safe_addstr(sw, 1 + i, 1, line[: s_cols - 2], attr)
    if lay.controls is not None:
        draw_controls(lay.controls, CONTROLS)
    if lay.agents_win is not None:
        draw_agents(lay.agents_win, roster, status_pairs)

    if lay.hint is not None:
        y, _, _, w = lay.hint
        hint = key_hint(w - 1)
        safe_addstr(scr, y, max(0, (w - 1 - len(hint)) // 2), hint, curses.A_DIM)

    if help_view is not None:
        help_view.draw(scr)


def help_key(help_view, game, ch, paused_by_help):
    """The `?` help contract, ahead of every other key. Returns
    (consumed, paused_by_help). While the overlay is open it takes every key
    (KEY_RESIZE excepted, so the caller still rebuilds its layout); closing
    it lifts only a pause the help itself caused. `?` opens it and pauses a
    running game."""
    if help_view.is_open:
        if not help_view.handle_key(ch):
            return False, paused_by_help
        if not help_view.is_open and paused_by_help:
            game.paused = False
            paused_by_help = False
        return True, paused_by_help
    if ch == HELP_KEY:
        help_view.open()
        if not game.paused and not game.game_over:
            game.paused = True
            paused_by_help = True
        return True, paused_by_help
    return False, paused_by_help


def handle_key(game, ch) -> bool:
    """Apply a key to the game. Returns True if state changed/redraw needed."""
    if ch in (ord("q"), ord("Q")):
        return False  # handled by the loop
    if game.game_over:
        return False
    if ch in (ord("p"), ord("P")):
        game.paused = not game.paused
        return True
    if game.paused:
        return False
    if ch in (curses.KEY_LEFT, ord("h"), ord("H")):
        return game.move(-1)
    if ch in (curses.KEY_RIGHT, ord("l"), ord("L")):
        return game.move(1)
    if ch in (curses.KEY_DOWN, ord("j"), ord("J")):
        return game.soft_drop()
    if ch in (curses.KEY_UP, ord("k"), ord("K")):
        return game.rotate(1)
    if ch == ord(" "):
        game.hard_drop()
        return True
    if ch in (ord("c"), ord("C")):
        return game.hold_swap()
    return False


def run(session) -> None:
    """Play Tetris until `q` (back to the arcade menu) or SIGTERM. The game
    is saved on every exit path and resumed, paused, on the next run."""
    stdscr = session.stdscr
    piece_pairs = init_colors()
    game_path = session.path("tetris-game.json")
    hiscore_path = session.path("tetris-hiscore.json")
    hiscore = load_hiscore(hiscore_path)
    help_view = HelpOverlay(TITLE, HELP)

    # A saved, unfinished game resumes exactly where it left off — paused,
    # behind a banner. Corrupt/incompatible saves are ignored silently.
    game = Game()
    resume_banner = False
    paused_by_resume = False
    if game_path:
        restored = Game.from_save(load_json_file(game_path))
        if restored is not None and not restored.game_over:
            game = restored
            game.paused = True
            resume_banner = True
            paused_by_resume = True

    lay = Layout(stdscr)
    paused_by_attention = False
    paused_by_help = False
    game_over_handled = game.game_over
    dirty = True
    last = time.monotonic()

    try:
        while not session.quitting.is_set():
            now = time.monotonic()
            dt = now - last
            last = now

            changed, alerts = session.pump()
            if alerts and session.should_auto_pause and not game.game_over:
                game.paused = True
                paused_by_attention = True
                # The banner now owns this pause: closing help must not
                # lift it before the alert is acknowledged.
                paused_by_help = False
            dirty = dirty or changed

            # Input.
            ch = stdscr.getch()
            if ch != -1:
                consumed, paused_by_help = help_key(help_view, game, ch,
                                                    paused_by_help)
                if consumed:
                    dirty = True
                elif ch in (ord("q"), ord("Q")):
                    break
                elif ch == curses.KEY_RESIZE:
                    curses.update_lines_cols()
                    stdscr.erase()
                    lay = Layout(stdscr)
                    dirty = True
                elif session.attention is not None or resume_banner:
                    # Any key clears the banner and resumes the pause it came
                    # with (attention pause or restored-game pause).
                    session.dismiss()
                    resume_banner = False
                    if paused_by_attention:
                        game.paused = False
                        paused_by_attention = False
                    if paused_by_resume:
                        game.paused = False
                        paused_by_resume = False
                    dirty = True
                elif ch in (ord("r"), ord("R")):
                    # Restart abandons the saved game but banks the score.
                    if game.score > hiscore:
                        hiscore = game.score
                        save_hiscore(hiscore_path, hiscore)
                    if game_path:
                        remove_path(game_path)
                    game.reset()
                    game_over_handled = False
                    dirty = True
                else:
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
                draw(piece_pairs, session.status_pairs, game, lay,
                     session.roster, session.attention, resume_banner, hiscore,
                     help_view)
                stdscr.noutrefresh()
                curses.doupdate()
                dirty = False

            time.sleep(0.01)
    finally:
        # The one save point: q, SIGTERM, or any other exit path lands here.
        # Resolve a pending clear first so what is saved is the post-clear
        # state, exactly as if the animation had finished on screen.
        game.finish_clearing()
        if game_path:
            if game.game_over:
                remove_path(game_path)
            else:
                atomic_save_json(game_path, game.to_save())
        if game.score > hiscore:
            save_hiscore(hiscore_path, game.score)


if __name__ == "__main__":
    import arcade

    sys.exit(arcade.main(["--game", "tetris"] + sys.argv[1:]))
