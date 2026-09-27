#!/usr/bin/env python3
"""Breakout for the Herdr arcade (see arcade.py, which runs it via `run()`).

Structure: everything above the "curses UI" divider is pure game logic —
field, bricks, paddle, ball physics, lives, levels, save/restore and the
layout plan — with no terminal dependency, so `tests/test_breakout.py`
imports this module without a terminal. Agent awareness, state files and the
shared curses helpers live in common.py.

Coordinates are field cells: x grows right (0..FIELD_W), y grows down
(0..FIELD_H). The ball has a float position; the cell it occupies is
(floor(x), floor(y)). It moves in axis-separated substeps of at most half a
cell per axis, so it can never skip over a one-row brick or the paddle.
"""

from __future__ import annotations

import curses
import math
import random
import sys
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

FIELD_W = 44
FIELD_H = 21

BRICK_W = 4
BRICK_COLS = FIELD_W // BRICK_W  # 11
BRICK_ROWS = 6
BRICK_TOP = 3                    # rows 0-2 stay open behind the wall
# Points per brick row, top to bottom (1/1/4/4/7/7 bottom-to-top, as the
# original arcade cabinet scored its yellow/green/orange/red rows).
ROW_POINTS = (7, 7, 4, 4, 1, 1)
ORANGE_ROWS = (2, 3)             # first hit here speeds the ball up
RED_ROWS = (0, 1)                # first hit here speeds the ball up again

PADDLE_Y = FIELD_H - 2           # row 19; row 20 is the gutter below it
PADDLE_W = 7
PADDLE_SMALL_W = 4               # after the ball reaches the ceiling
PADDLE_STEP = 3                  # columns per keypress

START_LIVES = 3
BASE_SPEED = 12.0                # cells/s at level 1, before boosts
LEVEL_SPEEDUP = 1.1              # per level
MAX_BASE_SPEED = 30.0
BOOST = 1.15                     # per speed-up event
BOOST_HITS = (4, 12)             # hit counts that speed the ball up
MAX_ANGLE = math.radians(60)     # paddle bounce angle at the paddle's edge
LAUNCH_ANGLES = (-45.0, -30.0, 30.0, 45.0)  # degrees from vertical
MAX_SUBSTEP = 0.5                # cells per axis per physics substep

DEATH_TIME = 1.0
LEVEL_CLEAR_TIME = 1.5

SAVE_VERSION = 1

EPS = 1e-6


def base_speed(level: int) -> float:
    """Ball speed (cells/s) at the start of `level`: faster each level."""
    return min(MAX_BASE_SPEED, BASE_SPEED * LEVEL_SPEEDUP ** (level - 1))


def new_wall() -> list:
    """A full brick wall: BRICK_ROWS lists of BRICK_COLS booleans."""
    return [[True] * BRICK_COLS for _ in range(BRICK_ROWS)]


class Phase:
    """A transient animation as an explicit logic phase ('death' or
    'level'). The ball is frozen while it runs; its resolution (re-serve /
    next level) happens when it completes — via update(), or immediately via
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

    def __init__(self, rng: random.Random | None = None):
        self.rng = rng if rng is not None else random.Random()
        self.score = 0
        self.lives = START_LIVES
        self.level = 1
        self.paused = False
        self.game_over = False
        self.phase: Phase | None = None
        self.bricks = new_wall()
        self._new_level_state()
        self.serve()

    def reset(self) -> None:
        self.__init__(self.rng)

    def _new_level_state(self) -> None:
        self.hits = 0
        self.boosts = 0
        self.hit_orange = False
        self.hit_red = False

    # -- queries -----------------------------------------------------------

    @property
    def speed(self) -> float:
        return base_speed(self.level) * BOOST ** self.boosts

    def bricks_left(self) -> int:
        return sum(sum(1 for b in row if b) for row in self.bricks)

    def brick_at(self, cx: int, cy: int):
        """(row, col) of the live brick covering field cell (cx, cy), or
        None."""
        r = cy - BRICK_TOP
        if 0 <= r < BRICK_ROWS and 0 <= cx < FIELD_W:
            c = cx // BRICK_W
            if c < BRICK_COLS and self.bricks[r][c]:
                return (r, c)
        return None

    # -- paddle / serve ----------------------------------------------------

    def _clamp_paddle(self) -> None:
        self.paddle_x = max(0, min(FIELD_W - self.paddle_w, self.paddle_x))

    def _ride(self) -> None:
        """Stick the ball to the paddle's centre, one row above it."""
        self.ball_x = self.paddle_x + self.paddle_w / 2.0
        self.ball_y = PADDLE_Y - 0.5

    def serve(self) -> None:
        """New ball stuck to a full-size, centred paddle."""
        self.paddle_w = PADDLE_W
        self.paddle_x = (FIELD_W - PADDLE_W) // 2
        self.shrunk = False
        self.stuck = True
        self.vx = 0.0
        self.vy = 0.0
        self._ride()

    def launch(self) -> bool:
        if self.game_over or self.paused or self.phase is not None or not self.stuck:
            return False
        angle = math.radians(self.rng.choice(LAUNCH_ANGLES))
        self.stuck = False
        self.vx = self.speed * math.sin(angle)
        self.vy = -self.speed * math.cos(angle)
        return True

    def move_paddle(self, direction: int) -> bool:
        """Step the paddle PADDLE_STEP columns left (-1) or right (+1)."""
        if self.game_over or self.paused or self.phase is not None:
            return False
        old = self.paddle_x
        self.paddle_x += direction * PADDLE_STEP
        self._clamp_paddle()
        if self.stuck:
            self._ride()
        return self.paddle_x != old

    # -- physics -----------------------------------------------------------

    def _set_speed(self) -> None:
        """Rescale the velocity to the current speed, keeping direction."""
        mag = math.hypot(self.vx, self.vy)
        if mag > 0:
            k = self.speed / mag
            self.vx *= k
            self.vy *= k

    def _hit_brick(self, r: int, c: int) -> None:
        self.bricks[r][c] = False
        self.score += ROW_POINTS[r]
        self.hits += 1
        boost = self.hits in BOOST_HITS
        if r in ORANGE_ROWS and not self.hit_orange:
            self.hit_orange = True
            boost = True
        if r in RED_ROWS and not self.hit_red:
            self.hit_red = True
            boost = True
        if boost:
            self.boosts += 1
            self._set_speed()
        if self.bricks_left() == 0:
            self.phase = Phase("level", LEVEL_CLEAR_TIME)

    def _hit_ceiling(self) -> None:
        if not self.shrunk:
            self.shrunk = True
            centre = self.paddle_x + self.paddle_w / 2.0
            self.paddle_w = PADDLE_SMALL_W
            self.paddle_x = int(round(centre - PADDLE_SMALL_W / 2.0))
            self._clamp_paddle()

    def _bounce_off_paddle(self) -> None:
        """Reflect upward at an angle set by the hit offset from the
        paddle's centre: centre goes straight up, edges go out at MAX_ANGLE."""
        centre = self.paddle_x + self.paddle_w / 2.0
        offset = (self.ball_x - centre) / (self.paddle_w / 2.0)
        offset = max(-1.0, min(1.0, offset))
        angle = offset * MAX_ANGLE
        self.vx = self.speed * math.sin(angle)
        self.vy = -self.speed * math.cos(angle)
        self.ball_y = PADDLE_Y - EPS  # just above the paddle row

    def _on_paddle_span(self, cx: int) -> bool:
        return self.paddle_x <= cx < self.paddle_x + self.paddle_w

    def _lose_ball(self) -> None:
        self.lives -= 1
        self.stuck = False
        if self.lives <= 0:
            self.lives = 0
            self.game_over = True
            self.phase = None
        else:
            self.phase = Phase("death", DEATH_TIME)

    def _substep(self, dx: float, dy: float) -> None:
        """Move at most half a cell per axis: x first, then y. A blocked axis
        reverts and flips that velocity component, so a side hit flips x and
        a top/bottom hit flips y."""
        # -- x ---------------------------------------------------------------
        nx = self.ball_x + dx
        if nx < 0 or nx >= FIELD_W:
            self.vx = -self.vx
        else:
            hit = self.brick_at(math.floor(nx), math.floor(self.ball_y))
            if hit is not None:
                self.vx = -self.vx
                self._hit_brick(*hit)
                if self.phase is not None:
                    return
            else:
                self.ball_x = nx
        # -- y ---------------------------------------------------------------
        ny = self.ball_y + dy
        cy, ncy = math.floor(self.ball_y), math.floor(ny)
        cx = math.floor(self.ball_x)
        if ny < 0:
            self.vy = -self.vy
            self._hit_ceiling()
        elif self.vy > 0 and self._on_paddle_span(cx) and (
            (cy < PADDLE_Y <= ncy) or cy == PADDLE_Y
        ):
            # Crossing into the paddle row from above — or already in it
            # when the paddle stepped underneath the ball.
            self._bounce_off_paddle()
        elif ny >= FIELD_H:
            self._lose_ball()
        else:
            hit = self.brick_at(cx, ncy)
            if hit is not None:
                self.vy = -self.vy
                self._hit_brick(*hit)
            else:
                self.ball_y = ny

    def _advance(self, dt: float) -> None:
        remaining = dt
        while remaining > 1e-9 and self.phase is None and not self.game_over:
            vmax = max(abs(self.vx), abs(self.vy))
            if vmax <= 0:
                return
            h = min(remaining, MAX_SUBSTEP / vmax)
            remaining -= h
            self._substep(self.vx * h, self.vy * h)

    # -- phases ------------------------------------------------------------

    def finish_phase(self) -> None:
        """Resolve a pending death / level-clear immediately (pause, quit,
        save). The state afterwards is exactly the post-phase state."""
        phase = self.phase
        if phase is None:
            return
        self.phase = None
        if phase.kind == "level":
            self.level += 1
            self.bricks = new_wall()
            self._new_level_state()
        self.serve()

    def update(self, dt: float) -> bool:
        """Advance the game by `dt` seconds. True if anything changed."""
        if self.paused or self.game_over:
            return False
        if self.phase is not None:
            self.phase.elapsed += dt
            if self.phase.elapsed >= self.phase.duration:
                self.finish_phase()
            return True
        if self.stuck:
            return False
        self._advance(dt)
        return True

    # -- save / restore ----------------------------------------------------

    def to_save(self) -> dict:
        """Serializable snapshot. Always taken with no phase pending: the
        caller resolves it with finish_phase() first (the UI save path
        does)."""
        return {
            "version": SAVE_VERSION,
            "score": self.score,
            "lives": self.lives,
            "level": self.level,
            "bricks": [row[:] for row in self.bricks],
            "paddle_x": self.paddle_x,
            "paddle_w": self.paddle_w,
            "shrunk": self.shrunk,
            "ball": [self.ball_x, self.ball_y, self.vx, self.vy],
            "stuck": self.stuck,
            "hits": self.hits,
            "boosts": self.boosts,
            "hit_orange": self.hit_orange,
            "hit_red": self.hit_red,
            "rng": _rng_to_json(self.rng),
        }

    @classmethod
    def from_save(cls, data, rng=None):
        """Rebuild a saved game, or return None for anything corrupt,
        incompatible, or not exactly a save this version wrote."""
        try:
            if not isinstance(data, dict) or data.get("version") != SAVE_VERSION:
                return None
            state = _rng_from_json(data.get("rng"))
            if state is None:
                return None
            bricks = data["bricks"]
            if not isinstance(bricks, list) or len(bricks) != BRICK_ROWS:
                return None
            for row in bricks:
                if not isinstance(row, list) or len(row) != BRICK_COLS:
                    return None
                if any(not isinstance(b, bool) for b in row):
                    return None
            ints = {}
            for key in ("score", "lives", "level", "paddle_x", "paddle_w",
                        "hits", "boosts"):
                v = data[key]
                if not isinstance(v, int) or isinstance(v, bool):
                    return None
                ints[key] = v
            if (ints["score"] < 0 or ints["lives"] < 1 or ints["level"] < 1
                    or ints["hits"] < 0 or not 0 <= ints["boosts"] <= 8):
                return None
            if ints["paddle_w"] not in (PADDLE_W, PADDLE_SMALL_W):
                return None
            if not 0 <= ints["paddle_x"] <= FIELD_W - ints["paddle_w"]:
                return None
            ball = data["ball"]
            if (not isinstance(ball, list) or len(ball) != 4
                    or any(isinstance(v, bool) or not isinstance(v, (int, float))
                           for v in ball)):
                return None
            bx, by, vx, vy = (float(v) for v in ball)
            if not all(math.isfinite(v) for v in (bx, by, vx, vy)):
                return None
            if not (0 <= bx < FIELD_W and 0 <= by < FIELD_H):
                return None
            flags = {}
            for key in ("shrunk", "stuck", "hit_orange", "hit_red"):
                if not isinstance(data[key], bool):
                    return None
                flags[key] = data[key]

            game = cls(rng)
            game.rng.setstate(state)
            game.bricks = [row[:] for row in bricks]
            game.score = ints["score"]
            game.lives = ints["lives"]
            game.level = ints["level"]
            game.paddle_x = ints["paddle_x"]
            game.paddle_w = ints["paddle_w"]
            game.hits = ints["hits"]
            game.boosts = ints["boosts"]
            game.shrunk = flags["shrunk"]
            game.stuck = flags["stuck"]
            game.hit_orange = flags["hit_orange"]
            game.hit_red = flags["hit_red"]
            game.ball_x, game.ball_y, game.vx, game.vy = bx, by, vx, vy
            if game.stuck:
                game._ride()
            elif vx == 0 and vy == 0:
                return None  # a free ball that cannot move is corrupt
            if game.bricks_left() == 0:
                return None  # a cleared wall is always resolved before saving
            return game
        except (KeyError, TypeError, ValueError, IndexError):
            return None


# --------------------------------------------------------------------------
# Controls and help text (shown in the side column and the `?` overlay)
# --------------------------------------------------------------------------

TITLE = "Breakout"

CONTROLS = [
    ("<- -> a d", "move paddle"),
    ("h l", "move paddle"),
    ("space", "launch ball"),
    ("up w k", "launch ball"),
    ("?", "help"),
    ("p", "pause / resume"),
    ("r", "restart"),
    ("q", "menu (saves game)"),
]

HELP = [
    ("Goal",
     "Knock out every brick in the wall by bouncing the ball off your "
     "paddle. Clear the wall and a new, faster one appears. Keep the ball "
     "in play: every ball that falls past the paddle costs a life."),
    ("Controls",
     "- Left/right arrows, a/d or h/l move the paddle 3 columns per press.\n"
     "- Each new ball sits on the paddle until you launch it with space "
     "(or up, w, k). You can slide it into position first; it flies off "
     "up and to a random side.\n"
     "- p pauses and resumes, r abandons this game and starts a new one, "
     "q goes back to the arcade menu. Quitting saves the game; it comes "
     "back paused next time.\n"
     "- ? opens this help and pauses the game; any other key closes it.\n"
     "- When one of your agents needs attention the game pauses and a "
     "banner appears; any key dismisses it and resumes."),
    ("How it works",
     "- The wall is 6 rows of 11 bricks. The ball bounces off the side "
     "walls, the ceiling and the bricks; each brick breaks in one hit.\n"
     "- Where the ball lands on the paddle sets its angle: the centre "
     "sends it straight up, the edges send it out at up to 60 degrees.\n"
     "- The ball speeds up on the 4th and 12th brick of each wall, and the "
     "first time it breaks a yellow/green brick and a red/magenta brick.\n"
     "- The first time the ball touches the ceiling your paddle shrinks "
     "from 7 to 4 columns, until the next ball is served.\n"
     "- A ball that drops below the paddle is lost. You have 3 balls; "
     "losing the last one ends the game.\n"
     "- Each new level starts a little faster than the last."),
    ("Scoring",
     "- Blue and cyan bricks (bottom two rows): 1 point.\n"
     "- Green and yellow bricks (middle rows): 4 points.\n"
     "- Magenta and red bricks (top two rows): 7 points.\n"
     "A full wall is worth 264 points. Your best score is kept."),
    ("Tips",
     "- Aim with the paddle edges to steer the ball into gaps.\n"
     "- Break a hole through one side: a ball trapped above the wall "
     "clears the high-value rows by itself.\n"
     "- Expect a speed jump when you first reach the back rows, and a "
     "small paddle once the ball hits the ceiling."),
]


def key_hint(width: int) -> str:
    """One-line key hint for tiers without a controls box; always keeps
    '? help' even when the full list does not fit."""
    full = controls_hint(CONTROLS)
    if len(full) <= width:
        return full
    rest = [c for c in CONTROLS if c[0] != "?"]
    return ("? help | " + controls_hint(rest))[: max(0, width)]


# --------------------------------------------------------------------------
# Layout plan (pure)
# --------------------------------------------------------------------------

FIELD_WIN_W = FIELD_W + 2        # 46
FIELD_WIN_H = FIELD_H + 2        # 23
MIN_COLS, MIN_ROWS = FIELD_WIN_W, FIELD_WIN_H
FULL_ROWS = FIELD_WIN_H + 1      # header + field
STATS_H = 7                      # frame + score/best/level/lives/state
BOX_MIN_H = 3                    # a framed box with one line inside


def layout_plan(cols: int, rows: int) -> dict:
    """Pure layout decision: tier plus each region's (y, x, height, width).
    Tiers: 'full' (field + stats/controls/agents side column), 'side' (side
    column without agents), 'field' (score and a key hint instead of the
    side column), 'too_small'. The field plus side column is centred below
    the header row; `hint` is the key-hint row (row 0 means it shares the
    header) in the 'field' tier."""
    plan = {"tier": "too_small", "too_small": True, "header": False,
            "field": None, "stats": None, "controls": None, "agents": None,
            "hint": None}
    if cols < MIN_COLS or rows < MIN_ROWS:
        return plan
    plan["too_small"] = False
    plan["header"] = rows >= FULL_ROWS
    top = 1 if plan["header"] else 0
    side = side_width(cols, FIELD_WIN_W)
    if not side:
        plan["tier"] = "field"
        if rows >= top + FIELD_WIN_H + 1:
            # A spare row below the field: centre above it, hint on it.
            y, x = center_origin(cols, rows - 1, FIELD_WIN_W, FIELD_WIN_H, top)
            plan["hint"] = (rows - 1, 0, 1, cols)
        else:
            y, x = center_origin(cols, rows, FIELD_WIN_W, FIELD_WIN_H, top)
            # Header row when there is one, else over the field's bottom edge.
            plan["hint"] = (0 if plan["header"] else rows - 1, 0, 1, cols)
        plan["field"] = (y, x, FIELD_WIN_H, FIELD_WIN_W)
        return plan
    y, x = center_origin(cols, rows, FIELD_WIN_W + 1 + side, FIELD_WIN_H, top)
    plan["field"] = (y, x, FIELD_WIN_H, FIELD_WIN_W)
    sx = x + FIELD_WIN_W + 1
    # Height priority: controls, then stats, then agents take what is left.
    left = FIELD_WIN_H
    ctrl_h = min(controls_height(CONTROLS, side), left)
    left -= ctrl_h
    stats_h = min(STATS_H, left) if left >= BOX_MIN_H else 0
    left -= stats_h
    agents_h = left if left >= BOX_MIN_H else 0
    if stats_h:
        plan["stats"] = (y, sx, stats_h, side)
    plan["controls"] = (y + stats_h, sx, ctrl_h, side)
    if agents_h:
        plan["agents"] = (y + stats_h + ctrl_h, sx, agents_h, side)
    plan["tier"] = "full" if agents_h else "side"
    return plan


# --------------------------------------------------------------------------
# curses UI
# --------------------------------------------------------------------------

# Colour pairs 40-59 are shared by the later games (one runs at a time;
# registered in run() on every start): 40-45 brick rows,
# 46 paddle, 47 ball.
ROW_COLORS = (
    curses.COLOR_RED,
    curses.COLOR_MAGENTA,
    curses.COLOR_YELLOW,
    curses.COLOR_GREEN,
    curses.COLOR_CYAN,
    curses.COLOR_BLUE,
)
PAIR_ROW_BASE = 40
PAIR_PADDLE = 46
PAIR_BALL = 47

BRICK_GLYPH = "[==]"
PADDLE_CHAR = "█"
BALL_GLYPH = "o"
DEATH_GLYPHS = ("*", "+", ".", " ")

def init_colors() -> None:
    bg = default_bg()
    for i, color in enumerate(ROW_COLORS):
        curses.init_pair(PAIR_ROW_BASE + i, color, bg)
    curses.init_pair(PAIR_PADDLE, curses.COLOR_CYAN, bg)
    curses.init_pair(PAIR_BALL, curses.COLOR_WHITE, bg)


class Layout:
    """Screen regions for the current size; rebuilt on resize."""

    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.rows, self.cols = stdscr.getmaxyx()
        self.plan = layout_plan(self.cols, self.rows)
        self.too_small = self.plan["too_small"]
        self.header = self.plan["header"]
        self.tier = self.plan["tier"]
        self.hint = self.plan["hint"]
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


def _centered(win, row, text, attr=0):
    _, cols = win.getmaxyx()
    safe_addstr(win, row, max(0, (cols - len(text)) // 2), text, attr)


def draw_field(win, game: Game, show_hint: bool) -> None:
    for r in range(BRICK_ROWS):
        attr = curses.color_pair(PAIR_ROW_BASE + r) | curses.A_REVERSE | curses.A_BOLD
        for c in range(BRICK_COLS):
            if game.bricks[r][c]:
                safe_addstr(win, 1 + BRICK_TOP + r, 1 + c * BRICK_W, BRICK_GLYPH, attr)

    safe_addstr(win, 1 + PADDLE_Y, 1 + game.paddle_x, PADDLE_CHAR * game.paddle_w,
                curses.color_pair(PAIR_PADDLE) | curses.A_BOLD)

    phase = game.phase
    bx = min(FIELD_W - 1, max(0, math.floor(game.ball_x)))
    by = min(FIELD_H - 1, max(0, math.floor(game.ball_y)))
    ball_attr = curses.color_pair(PAIR_BALL) | curses.A_BOLD
    if phase is not None and phase.kind == "death":
        i = min(len(DEATH_GLYPHS) - 1,
                int(phase.elapsed / phase.duration * len(DEATH_GLYPHS)))
        safe_addstr(win, 1 + by, 1 + bx, DEATH_GLYPHS[i], ball_attr)
        _centered(win, 1 + BRICK_TOP + BRICK_ROWS + 4, " ball lost ", curses.A_STANDOUT)
    elif phase is not None and phase.kind == "level":
        if int(phase.elapsed * 6) % 2 == 0:
            _centered(win, 1 + BRICK_TOP + BRICK_ROWS + 4,
                      f" level {game.level} clear! ",
                      curses.A_STANDOUT | curses.A_BOLD)
    elif not game.game_over:
        safe_addstr(win, 1 + by, 1 + bx, BALL_GLYPH, ball_attr)

    if game.stuck and phase is None and not game.game_over and show_hint:
        _centered(win, 1 + PADDLE_Y + 1, " space: launch ", curses.A_DIM)

    if game.game_over:
        mid = 1 + BRICK_TOP + BRICK_ROWS + 4
        _centered(win, mid, " GAME OVER ", curses.A_STANDOUT | curses.A_BOLD)
        _centered(win, mid + 1, " r restart | q menu ", curses.A_STANDOUT)
    elif game.paused:
        mid = 1 + BRICK_TOP + BRICK_ROWS + 4
        _centered(win, mid, " PAUSED ", curses.A_STANDOUT | curses.A_BOLD)
        _centered(win, mid + 1, " p resume | q menu ", curses.A_STANDOUT)


def _state_word(game) -> str:
    if game.game_over:
        return "GAME OVER"
    return "paused" if game.paused else "playing"


def draw(status_pairs, game, lay, roster, attention, resume=False, best=0,
         help_overlay=None):
    scr = lay.stdscr
    scr.erase()

    if lay.too_small:
        msg = f"pane too small: need {MIN_COLS}x{MIN_ROWS}, have {lay.cols}x{lay.rows}"
        msg2 = "resize the pane (? help, q menu)"
        for i, text in enumerate((msg, msg2)):
            text = text[: max(0, lay.cols - 1)]
            safe_addstr(scr, lay.rows // 2 + i, max(0, (lay.cols - len(text)) // 2),
                        text, curses.A_BOLD if i == 0 else 0)
        if help_overlay is not None:
            help_overlay.draw(scr)
        return

    hint_in_header = lay.hint is not None and lay.hint[0] == 0
    stand = curses.A_STANDOUT | (curses.A_BOLD if game.game_over else 0)
    if not draw_banner(scr, lay.cols, attention, resume) and lay.header:
        if hint_in_header:
            # Tight 'field' tier: compact stats, then the key hint.
            head = f" {game.score} pts | {game.lives} lives | lvl {game.level} "
            safe_addstr(scr, 0, 0, head[: lay.cols - 1], stand)
            room = lay.cols - 1 - len(head) - 1
            if room > 0:
                safe_addstr(scr, 0, len(head) + 1, key_hint(room), curses.A_DIM)
        else:
            head = f" {TITLE.lower()} - {_state_word(game).lower()} "
            if game.game_over:
                head = f" {TITLE.lower()} - GAME OVER "
            if lay.stats is None:
                head += f"score {game.score} | lives {game.lives} | lvl {game.level} "
            safe_addstr(scr, 0, 0, head[: lay.cols - 1], stand)

    lay.boxes()
    draw_field(lay.field, game, show_hint=True)

    if lay.hint is not None and not hint_in_header:
        y, x, _, w = lay.hint
        text = key_hint(w - 1)
        safe_addstr(scr, y, x + max(0, (w - 1 - len(text)) // 2), text, curses.A_DIM)

    if lay.stats is not None:
        sw = lay.stats
        rows, w = sw.getmaxyx()
        inner = w - 2
        safe_addstr(sw, 0, 2, f" {TITLE.lower()} "[: max(0, w - 3)], curses.A_BOLD)
        lines = [
            (f"score {game.score}", curses.A_BOLD),
            (f"best  {max(best, game.score)}", 0),
            (f"level {game.level}", 0),
            (f"lives {game.lives}", 0),
            (_state_word(game), curses.A_DIM),
        ]
        for i, (line, attr) in enumerate(lines[: rows - 2]):
            safe_addstr(sw, 1 + i, 1, line[:inner], attr)

    if lay.controls is not None:
        draw_controls(lay.controls, CONTROLS)

    if lay.agents is not None:
        draw_agents(lay.agents, roster, status_pairs)

    if help_overlay is not None:
        help_overlay.draw(scr)


def handle_key(game, ch) -> bool:
    """Apply a key to the game. Returns True if a redraw is needed."""
    if game.game_over:
        return False
    if ch in (ord("p"), ord("P")):
        game.paused = not game.paused
        return True
    if game.paused:
        return False
    if ch in (curses.KEY_LEFT, ord("h"), ord("H"), ord("a"), ord("A")):
        return game.move_paddle(-1)
    if ch in (curses.KEY_RIGHT, ord("l"), ord("L"), ord("d"), ord("D")):
        return game.move_paddle(1)
    if ch in (ord(" "), curses.KEY_UP, ord("k"), ord("w")):
        return game.launch()
    return False


def help_key(help_overlay, game, ch, paused_by_help):
    """Route a key through the `?` help overlay first. Returns (consumed,
    paused_by_help). While the overlay is open it takes every key except
    KEY_RESIZE; closing it lifts only the pause the help itself caused. `?`
    opens it (from play, pause or game over) and pauses a running game."""
    if help_overlay.is_open:
        if not help_overlay.handle_key(ch):
            return False, paused_by_help  # resize: the caller relays out
        if not help_overlay.is_open and paused_by_help:
            game.paused = False
            paused_by_help = False
        return True, paused_by_help
    if ch == HELP_KEY:
        help_overlay.open()
        if not game.paused and not game.game_over:
            game.paused = True
            paused_by_help = True
        return True, paused_by_help
    return False, paused_by_help


MAX_DT = 0.1  # a stall (suspend, slow terminal) never teleports the ball


def run(session) -> None:
    """Play Breakout until `q` (back to the arcade menu) or SIGTERM. The game
    is saved on every exit path and resumed, paused, on the next run."""
    stdscr = session.stdscr
    init_colors()
    game_path = session.path("breakout-game.json")
    hiscore_path = session.path("breakout-hiscore.json")
    hiscore = load_hiscore(hiscore_path)

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
    help_overlay = HelpOverlay(TITLE, HELP)
    paused_by_help = False
    paused_by_attention = False
    game_over_handled = game.game_over
    dirty = True
    last = time.monotonic()

    try:
        while not session.quitting.is_set():
            now = time.monotonic()
            dt = min(MAX_DT, now - last)
            last = now

            changed, alerts = session.pump()
            if alerts and session.should_auto_pause and not game.game_over:
                if not game.paused:
                    game.paused = True
                    paused_by_attention = True
                elif paused_by_help:
                    # The banner now owns the pause: closing help keeps it.
                    paused_by_help = False
                    paused_by_attention = True
            dirty = dirty or changed

            ch = stdscr.getch()
            if ch != -1:
                consumed, paused_by_help = help_key(
                    help_overlay, game, ch, paused_by_help)
                if consumed:
                    dirty = True
                elif ch == curses.KEY_RESIZE:
                    curses.update_lines_cols()
                    stdscr.erase()
                    lay = Layout(stdscr)
                    dirty = True
                elif ch in (ord("q"), ord("Q")):
                    break
                elif session.attention is not None or resume_banner:
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
                draw(session.status_pairs, game, lay, session.roster,
                     session.attention, resume_banner, hiscore, help_overlay)
                stdscr.noutrefresh()
                curses.doupdate()
                dirty = False

            time.sleep(0.01)
    finally:
        # The one save point: q, SIGTERM, or any other exit path lands here.
        # Resolve a pending death / level clear first so what is saved is a
        # consistent post-phase state.
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

    sys.exit(arcade.main(["--game", "breakout"] + sys.argv[1:]))
