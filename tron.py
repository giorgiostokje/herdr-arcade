#!/usr/bin/env python3
"""Tron light cycles for the Herdr arcade (see arcade.py, which runs it via
`run()`), after the light-cycle stage of Bally Midway's 1982 arcade game.

Structure: everything above the "curses UI" divider is pure game logic —
arena, cycles, simultaneous collision resolution, the AI, scoring, lives,
levels, save/restore and the layout plan — with no terminal dependency, so
`tests/test_tron.py` imports this module without a terminal.

Design choices:
- Cells are one terminal column wide: the arena is 56x21 cells inside a
  58x23 framed window (58 + 1 + 30-column side column = 89 of the popup's
  93 columns). That is twice
  the room of two-column cells, which four cycles need; horizontal motion
  looks about twice as fast as vertical on screen, which is the trade-off.
- As in the arcade, a crashed cycle's trail vanishes ("derezzes") at once.
  The player's own crash freezes the board for a ~1s flashing death phase,
  after which the round restarts on a cleared arena.
- Scoring: +level for every step the player survives, plus
  ROUND_BONUS x level for winning a round.
"""

from __future__ import annotations

import curses
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

FIELD_W = 56  # cells, one terminal column each
FIELD_H = 21

UP, DOWN, LEFT, RIGHT = "up", "down", "left", "right"
DIRS = (UP, RIGHT, DOWN, LEFT)  # fixed order: the AI never iterates a set
DELTAS = {UP: (0, -1), DOWN: (0, 1), LEFT: (-1, 0), RIGHT: (1, 0)}
OPPOSITE = {UP: DOWN, DOWN: UP, LEFT: RIGHT, RIGHT: LEFT}

# Start cell and heading per cycle index: 0 is the player, 1-3 the AI.
STARTS = (
    ((8, 10), RIGHT),
    ((47, 10), LEFT),
    ((28, 2), DOWN),
    ((28, 18), UP),
)

LIVES = 3
MAX_QUEUE = 2          # pending player turns buffered between ticks
MAX_AI = 3
ROUND_BONUS = 250      # x level, for winning a round
DEATH_TIME = 1.0
ROUND_WON_TIME = 1.2
READY_TIME = 0.8       # a short beat before each round starts moving
BFS_LIMIT = 400        # cells the AI's look-ahead flood fill explores
AGGRO_AHEAD = 3        # the AI aims this many cells ahead of the player
JITTER = 1.5           # random slack added to the aggression distance

SAVE_VERSION = 1


def step_interval(level: int) -> float:
    """Seconds per cycle step at `level`: faster each level, 0.04s floor."""
    return max(0.04, 0.09 * (0.9 ** (level - 1)))


def ai_count(level: int) -> int:
    """AI cycles in a round at `level`: 1, 2, then 3."""
    return max(1, min(MAX_AI, level))


def in_bounds(cell) -> bool:
    return 0 <= cell[0] < FIELD_W and 0 <= cell[1] < FIELD_H


def step_cell(cell, d):
    dx, dy = DELTAS[d]
    return (cell[0] + dx, cell[1] + dy)


class Cycle:
    """One light cycle: head position, heading, trail (oldest first, the
    head is the last entry) and whether it is still riding."""

    __slots__ = ("pos", "dir", "alive", "trail")

    def __init__(self, pos, d, alive=True, trail=None):
        self.pos = pos
        self.dir = d
        self.alive = alive
        self.trail = list(trail) if trail is not None else [pos]


class Phase:
    """A transient animation as an explicit logic state (like tetris'
    Clearing): 'ready' (round about to start), 'dying' (the player crashed;
    the board freezes and the player's trail flashes) or 'round_won'. The
    state change happens when it completes — via update() or
    resolve_phases()."""

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
        and all(isinstance(n, int) and not isinstance(n, bool) for n in data[1])
    ):
        return (data[0], tuple(data[1]), data[2])
    return None


def flood_count(grid, start, limit: int = BFS_LIMIT) -> int:
    """Free cells reachable from `start` (itself included), counting at most
    `limit`. `start` must be free; the grid is not modified."""
    seen = {start}
    todo = deque([start])
    count = 0
    while todo and count < limit:
        x, y = todo.popleft()
        count += 1
        for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
            nx, ny = x + dx, y + dy
            if (0 <= nx < FIELD_W and 0 <= ny < FIELD_H and grid[ny][nx] < 0
                    and (nx, ny) not in seen):
                seen.add((nx, ny))
                todo.append((nx, ny))
    return count


class Game:
    """Full game state and every state transition. No I/O."""

    def __init__(self, rng: random.Random | None = None):
        self.rng = rng if rng is not None else random.Random()
        self.score = 0
        self.level = 1
        self.lives = LIVES
        self.round_steps = 0  # steps survived this round
        self.paused = False
        self.game_over = False
        self.phase: Phase | None = None
        self._start_round()

    def reset(self) -> None:
        self.__init__(self.rng)

    # -- rounds ------------------------------------------------------------

    def _start_round(self) -> None:
        """Clear the arena and put the player plus this level's AI cycles on
        their start cells, with a short ready beat."""
        self.cycles = [Cycle(pos, d) for pos, d in STARTS[: 1 + ai_count(self.level)]]
        self.queue: list = []
        self.step_timer = 0.0
        self.round_steps = 0
        self.rebuild_grid()
        self.phase = Phase("ready", READY_TIME)

    def rebuild_grid(self) -> None:
        """Occupancy grid (owner index, -1 free) from every living trail."""
        self.grid = [[-1] * FIELD_W for _ in range(FIELD_H)]
        for i, c in enumerate(self.cycles):
            if c.alive:
                for x, y in c.trail:
                    self.grid[y][x] = i

    @property
    def player(self) -> Cycle:
        return self.cycles[0]

    @property
    def ai(self) -> list:
        return self.cycles[1:]

    def free(self, cell) -> bool:
        return in_bounds(cell) and self.grid[cell[1]][cell[0]] < 0

    # -- input -------------------------------------------------------------

    def turn(self, d: str) -> bool:
        """Queue a player turn. It is compared against the last queued
        heading (or the current one), so a quick double turn applies on
        successive ticks; repeats and reversals are ignored."""
        if d not in DELTAS or self.game_over:
            return False
        if self.phase is not None and self.phase.kind != "ready":
            return False
        last = self.queue[-1] if self.queue else self.player.dir
        if d == last or d == OPPOSITE[last] or len(self.queue) >= MAX_QUEUE:
            return False
        self.queue.append(d)
        return True

    # -- AI ----------------------------------------------------------------

    def ai_choose(self, idx: int) -> str:
        """Heading for AI cycle `idx` this tick. Never a fatal cell while a
        safe one exists; among safe cells, prefer the largest reachable region
        (bounded flood fill), then one no other head can also reach this tick,
        then — aggression — the one closest to a point just ahead of the
        player, with a little seeded randomness in that distance."""
        me = self.cycles[idx]
        contested = set()
        for j, c in enumerate(self.cycles):
            if j != idx and c.alive:
                for d in DIRS:
                    if d != OPPOSITE[c.dir]:
                        contested.add(step_cell(c.pos, d))
        target = self.player.pos
        if self.player.alive:
            for _ in range(AGGRO_AHEAD):
                nxt = step_cell(target, self.player.dir)
                if not in_bounds(nxt):
                    break
                target = nxt
        best = None
        best_key = None
        for d in DIRS:
            if d == OPPOSITE[me.dir]:
                continue
            cell = step_cell(me.pos, d)
            jitter = self.rng.random() * JITTER  # drawn every time: determinism
            if not self.free(cell):
                key = (0, 0, 0, 0.0)
            else:
                dist = abs(cell[0] - target[0]) + abs(cell[1] - target[1])
                key = (1, flood_count(self.grid, cell),
                       0 if cell in contested else 1, -(dist + jitter))
            if best_key is None or key > best_key:
                best, best_key = d, key
        if best_key is not None and best_key[0] == 0:
            return me.dir  # boxed in: ride straight into the wall
        return best if best is not None else me.dir

    # -- stepping ----------------------------------------------------------

    def step(self) -> None:
        """Advance every living cycle one cell, resolving all collisions
        simultaneously against the pre-tick arena."""
        if self.queue:
            self.player.dir = self.queue.pop(0)
        for i in range(1, len(self.cycles)):
            if self.cycles[i].alive:
                self.cycles[i].dir = self.ai_choose(i)

        moving = [i for i, c in enumerate(self.cycles) if c.alive]
        target = {i: step_cell(self.cycles[i].pos, self.cycles[i].dir) for i in moving}
        dead = set()
        claims: dict = {}
        for i in moving:
            if not self.free(target[i]):
                dead.add(i)
            claims.setdefault(target[i], []).append(i)
        for ids in claims.values():
            if len(ids) > 1:
                dead.update(ids)  # same cell at once: all of them crash

        for i in moving:
            c = self.cycles[i]
            if i in dead:
                c.alive = False
            else:
                c.pos = target[i]
                c.trail.append(c.pos)
        # The dead derez: their trails vanish from the arena.
        self.rebuild_grid()

        if not self.player.alive:
            self.lives -= 1
            self.queue = []
            self.phase = Phase("dying", DEATH_TIME)
            return
        self.round_steps += 1
        self.score += self.level
        if not any(c.alive for c in self.ai):
            self.score += ROUND_BONUS * self.level
            self.phase = Phase("round_won", ROUND_WON_TIME)

    def resolve_phases(self) -> None:
        """Finish a pending transition immediately (pause/quit/save). The
        resulting state is exactly the one the animation would end in."""
        phase, self.phase = self.phase, None
        if phase is None or phase.kind == "ready":
            return
        if phase.kind == "dying":
            if self.lives <= 0:
                self.lives = 0
                self.game_over = True
            else:
                self._start_round()
        elif phase.kind == "round_won":
            self.level += 1
            self._start_round()
        # A new round opens with its own 'ready' beat (not saved: a resumed
        # game starts paused anyway).

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
        changed = False
        self.step_timer += dt
        interval = step_interval(self.level)
        while self.step_timer >= interval:
            self.step_timer -= interval
            changed = True
            self.step()
            if self.phase is not None or self.game_over:
                self.step_timer = 0.0
                break
        return changed

    # -- save / restore ----------------------------------------------------

    def to_save(self) -> dict:
        """Serializable snapshot. The caller resolves the death/round-won
        phases first (the UI save path does), so a save never holds a
        transition mid-flight."""
        return {
            "version": SAVE_VERSION,
            "score": self.score,
            "level": self.level,
            "lives": self.lives,
            "round_steps": self.round_steps,
            "cycles": [
                {"pos": list(c.pos), "dir": c.dir, "alive": c.alive,
                 "trail": [list(t) for t in c.trail]}
                for c in self.cycles
            ],
            "queue": list(self.queue),
            "step_timer": self.step_timer,
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
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    raise ValueError(v)
                return float(v)

            def cell(v):
                if not (isinstance(v, list) and len(v) == 2):
                    raise ValueError(v)
                c = (integer(v[0]), integer(v[1]))
                if not in_bounds(c):
                    raise ValueError(v)
                return c

            game = cls(rng)
            game.phase = None
            game.level = integer(data["level"], 1)
            game.score = integer(data["score"])
            game.lives = integer(data["lives"], 1)
            game.round_steps = integer(data["round_steps"])
            game.step_timer = num(data["step_timer"])
            game.game_over = data["game_over"]
            if not isinstance(game.game_over, bool):
                return None

            raw = data["cycles"]
            if not isinstance(raw, list) or len(raw) != 1 + ai_count(game.level):
                return None
            cycles = []
            taken = set()
            for entry in raw:
                if not isinstance(entry, dict):
                    return None
                d = entry["dir"]
                alive = entry["alive"]
                if d not in DELTAS or not isinstance(alive, bool):
                    return None
                pos = cell(entry["pos"])
                trail = [cell(t) for t in entry["trail"]]
                if not trail or trail[-1] != pos or len(set(trail)) != len(trail):
                    return None
                for (ax, ay), (bx, by) in zip(trail, trail[1:]):
                    if abs(ax - bx) + abs(ay - by) != 1:
                        return None
                if alive:
                    if taken & set(trail):
                        return None
                    taken.update(trail)
                cycles.append(Cycle(pos, d, alive, trail))
            if not cycles[0].alive:
                return None  # a live save always has a riding player
            game.cycles = cycles

            queue = data["queue"]
            if not isinstance(queue, list) or len(queue) > MAX_QUEUE:
                return None
            if any(d not in DELTAS for d in queue):
                return None
            game.queue = list(queue)
            game.rebuild_grid()

            # Restoring the RNG makes a resumed game's AI decide exactly as
            # the saved one would have.
            state = _rng_from_json(data["rng"])
            if state is None:
                return None
            game.rng.setstate(state)
            return game
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            return None


TITLE = "Tron"

# Every key the game reacts to (upper case works too).
CONTROLS = [
    ("arrows", "turn (or wasd / hjkl)"),
    ("?", "help"),
    ("p / r", "pause / restart"),
    ("q", "menu"),
]

HELP = [
    ("Goal",
     "Be the last light cycle riding. Every cycle - yours and the computer's "
     "- leaves a solid wall of light behind it. Make the enemy cycles crash "
     "into a wall or a trail before you do, then move on to a faster level "
     "with more enemies."),
    ("Controls",
     "- arrows (or w a s d, or h j k l): turn. Your cycle never stops; you "
     "only choose where it goes next.\n"
     "- You cannot reverse straight back into your own trail; that key is "
     "ignored.\n"
     "- Two quick turns are remembered and applied on the next two steps, "
     "so you can make a tight U-turn.\n"
     "- ? help, p pause, r restart (banks your score), q back to the menu. "
     "Your game is saved when you leave and resumes paused."),
    ("How it works",
     "- You ride the cyan cycle; its head is the arrow showing its heading. "
     "Enemy cycles are yellow, red and magenta.\n"
     "- Level 1 has one enemy, level 2 two, level 3 and up three. Each "
     "level is about 10% faster.\n"
     "- Riding into the arena wall or any trail (your own included) "
     "crashes the cycle. Two cycles moving into the same cell, or head-on "
     "into each other, both crash.\n"
     "- A crashed enemy derezzes: its trail vanishes at once, opening up "
     "space.\n"
     "- If you crash you lose a life and the round restarts on a clear "
     "arena. You have 3 lives; losing the last ends the game.\n"
     "- When every enemy has crashed you win the round and go up a level. "
     "Each round opens with a short READY beat."),
    ("Scoring",
     "- +level points for every step you survive (1 per step on level 1, "
     "2 on level 2, ...).\n"
     "- Winning a round: +250 x level bonus.\n"
     "- Your best score is kept between games."),
    ("Tips",
     "- Enemies aim for a spot just ahead of you and prefer open space: "
     "cut across their path to wall them in.\n"
     "- Don't seal yourself into a small pocket - keep an exit open.\n"
     "- Stay away from other heads: a same-cell collision takes you both "
     "out.\n"
     "- Pause with p (or open help) any time; the arena freezes."),
]


def hint_text(width: int) -> str:
    """Key hint for tiers without a controls box, at most `width` columns;
    when it must be cut, '? help' comes first so it always survives, then
    the menu/pause keys, and the (self-evident) movement keys last."""
    full = controls_hint(CONTROLS)
    if len(full) <= width:
        return full
    first = ([c for c in CONTROLS if c[0] == "?"]
             + [c for c in reversed(CONTROLS) if c[0] != "?"])
    return controls_hint(first)[: max(0, width)]


# Adaptive layout. The popup's inner area is 93x28. Row 0 is the
# header/banner; the field window (56 cells + border = 58 x 23) and the side
# column (common.side_width: 30 at the popup size, down to 20) are centred
# together below it. The side column stacks stats, controls and agents, all
# as tall as the field together. Without room for a side column the field
# is centred alone and a one-line key hint sits under it (or, when there is
# no spare row, in the header).
FIELD_WIN_W = FIELD_W + 2
FIELD_WIN_H = FIELD_H + 2
MIN_COLS = FIELD_WIN_W
MIN_ROWS = FIELD_WIN_H + 1
STATS_H = 9           # stats box rows when there is room
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
PAIR_CYCLE = (40, 41, 42, 43)  # player, then AI 1-3
PAIR_WALL, PAIR_FLASH = 44, 45

CYCLE_COLORS = (curses.COLOR_CYAN, curses.COLOR_YELLOW, curses.COLOR_RED,
                curses.COLOR_MAGENTA)
TRAIL = "█"
HEADS = {UP: "^", DOWN: "v", LEFT: "<", RIGHT: ">"}


def init_colors() -> None:
    bg = default_bg()
    for pair, color in zip(PAIR_CYCLE, CYCLE_COLORS):
        curses.init_pair(pair, color, bg)
    curses.init_pair(PAIR_WALL, curses.COLOR_BLUE, bg)
    curses.init_pair(PAIR_FLASH, curses.COLOR_WHITE, bg)


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
        for win in (self.field, self.stats, self.controls, self.agents):
            if win is not None:
                win.erase()
                frame(win)


def _put(win, x, y, text, attr=0):
    """One arena cell at arena coordinates."""
    safe_addstr(win, 1 + y, 1 + x, text, attr)


def _centered(win, row, text, attr):
    safe_addstr(win, row, max(1, (FIELD_WIN_W - len(text)) // 2), text, attr)


def draw_field(win, game: Game) -> None:
    phase = game.phase
    kind = phase.kind if phase is not None else None
    frame_attr = curses.color_pair(PAIR_WALL)
    if kind == "round_won" and int(phase.elapsed * 6) % 2:
        frame_attr = curses.color_pair(PAIR_FLASH) | curses.A_BOLD
    rows, cols = win.getmaxyx()
    bar = "+" + "-" * max(0, cols - 2) + "+"
    safe_addstr(win, 0, 0, bar, frame_attr)
    safe_addstr(win, rows - 1, 0, bar, frame_attr)
    for y in range(1, rows - 1):
        safe_addstr(win, y, 0, "|", frame_attr)
        safe_addstr(win, y, cols - 1, "|", frame_attr)

    if not game.game_over:
        flash = kind == "dying" and int(phase.elapsed * 8) % 2 == 1
        for i, c in enumerate(game.cycles):
            dying_player = i == 0 and not c.alive
            if not c.alive and not dying_player:
                continue  # derezzed: the trail is gone
            if dying_player and flash:
                attr = curses.color_pair(PAIR_FLASH) | curses.A_BOLD
            else:
                attr = curses.color_pair(PAIR_CYCLE[i])
            for x, y in c.trail[:-1]:
                _put(win, x, y, TRAIL, attr)
            hx, hy = c.pos
            head = "*" if dying_player else HEADS[c.dir]
            _put(win, hx, hy, head, attr | curses.A_BOLD | curses.A_REVERSE)

    mid = 1 + FIELD_H // 2
    if game.game_over:
        _centered(win, mid - 1, " GAME OVER ", curses.A_STANDOUT | curses.A_BOLD)
        _centered(win, mid + 1, " r restart | q menu | ? help ", curses.A_STANDOUT)
    elif game.paused:
        _centered(win, mid - 3, " PAUSED ", curses.A_STANDOUT | curses.A_BOLD)
        _centered(win, mid - 1, " p resume | ? help ", curses.A_STANDOUT)
    elif kind == "round_won":
        _centered(win, mid - 3, f" LEVEL {game.level + 1} ",
                  curses.color_pair(PAIR_CYCLE[0]) | curses.A_BOLD)
    elif kind == "dying":
        _centered(win, mid - 3, " DEREZZED ",
                  curses.color_pair(PAIR_CYCLE[2]) | curses.A_BOLD)
    elif kind == "ready":
        _centered(win, mid - 3, f" LEVEL {game.level} - READY ",
                  curses.color_pair(PAIR_CYCLE[0]) | curses.A_BOLD)


def _state_text(game: Game) -> str:
    if game.game_over:
        return "GAME OVER"
    if game.paused:
        return "paused"
    kind = game.phase.kind if game.phase is not None else None
    return {"dying": "derezzed!", "round_won": "round won!",
            "ready": "get ready..."}.get(kind, "riding")


def draw_stats(win, game: Game, best: int) -> None:
    rows, cols = win.getmaxyx()
    inner = cols - 2
    safe_addstr(win, 0, 2, f" {TITLE} "[: max(0, cols - 3)], curses.A_BOLD)
    alive = sum(1 for c in game.ai if c.alive)
    speed = 1.0 / step_interval(game.level)
    lines = [
        (f"score {game.score}", curses.A_BOLD),
        (f"best  {max(best, game.score)}", 0),
        (f"level {game.level}   lives {game.lives}", curses.color_pair(PAIR_CYCLE[0])),
        (f"enemy {alive}/{len(game.ai)} riding", curses.color_pair(PAIR_CYCLE[1])),
        (f"speed {speed:.0f} cells/s", curses.A_DIM),
        ("you: cyan " + HEADS[game.player.dir], curses.color_pair(PAIR_CYCLE[0])),
        (_state_text(game), curses.A_BOLD),
    ]
    for i, (line, attr) in enumerate(lines[: max(0, rows - 2)]):
        safe_addstr(win, 1 + i, 1, line[:inner], attr)


def header_text(game: Game, plan: dict, cols: int) -> str:
    """Row-0 text: title and state; in the field tier also the score, and
    the key hint when it has no row of its own ('? help' always kept)."""
    width = max(0, cols - 1)
    state = "GAME OVER" if game.game_over else _state_text(game)
    text = f" {TITLE.lower()} - {state} "
    if plan["stats"] is None:  # no stats box in this tier: score goes here
        text += f"| score {game.score} | lives {game.lives} | lvl {game.level} "
    if plan["hint_in_header"]:
        room = width - len(text) - 2
        if room >= len("? help"):
            text += "| " + hint_text(room)
        else:
            text = text[: max(0, width - 9)] + " | ? help"
    return text[:width]


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
            safe_addstr(scr, 0, 0, header_text(game, lay.plan, lay.cols),
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
    """Play Tron until `q` (back to the arcade menu) or SIGTERM. The game is
    saved on every exit path and resumed, paused, on the next run."""
    scr = session.stdscr
    init_colors()
    game_path = session.path("tron-game.json")
    hiscore_path = session.path("tron-hiscore.json")
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
                elif ch in KEY_DIRS:
                    if not game.paused:
                        game.turn(KEY_DIRS[ch])
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
        # pending death/round transition is resolved first so the save is a
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

    sys.exit(arcade.main(["--game", "tron"] + sys.argv[1:]))
