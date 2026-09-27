#!/usr/bin/env python3
"""Snake for the Herdr arcade (Blockade / Nibbler lineage; see arcade.py,
which runs it via `run()`).

Structure: everything above the "curses UI" divider is pure game logic —
field, layouts, snake, turn queue, food, lives, levels, save/restore and the
layout plan — with no terminal dependency, so `tests/test_snake.py` imports
this module without a terminal. Agent awareness, state files and the shared
curses helpers live in common.py.
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

FIELD_W = 26  # cells; each cell is two terminal columns
FIELD_H = 21

UP, DOWN, LEFT, RIGHT = "up", "down", "left", "right"
DELTAS = {UP: (0, -1), DOWN: (0, 1), LEFT: (-1, 0), RIGHT: (1, 0)}
OPPOSITE = {UP: DOWN, DOWN: UP, LEFT: RIGHT, RIGHT: LEFT}

# Nibbler-style wall layouts, one per level, cycling after the last. '#' is
# a wall, '.' open floor. Every layout keeps the start row clear so the
# snake always spawns with a long runway ahead.
LAYOUTS = (
    ("open", (
        "..........................",
        "..........................",
        "..........................",
        "..........................",
        "..........................",
        "..........................",
        "..........................",
        "..........................",
        "..........................",
        "..........................",
        "..........................",
        "..........................",
        "..........................",
        "..........................",
        "..........................",
        "..........................",
        "..........................",
        "..........................",
        "..........................",
        "..........................",
        "..........................",
    )),
    ("bars", (
        "..........................",
        "..........................",
        "..........................",
        "..........................",
        "..........................",
        "....##################....",
        "..........................",
        "..........................",
        "..........................",
        "..........................",
        "..........................",
        "..........................",
        "..........................",
        "..........................",
        "..........................",
        "....##################....",
        "..........................",
        "..........................",
        "..........................",
        "..........................",
        "..........................",
    )),
    ("pillars", (
        "..........................",
        "..........................",
        "........#........#........",
        "........#........#........",
        "........#........#........",
        "........#........#........",
        "........#........#........",
        "........#........#........",
        "..........................",
        "..........................",
        "..........................",
        "..........................",
        "..........................",
        "........#........#........",
        "........#........#........",
        "........#........#........",
        "........#........#........",
        "........#........#........",
        "........#........#........",
        "..........................",
        "..........................",
    )),
    ("rooms", (
        "............##............",
        "............##............",
        "............##............",
        "............##............",
        "#######.....##.....#######",
        "............##............",
        "............##............",
        "..........................",
        "..........................",
        "..........................",
        "..........................",
        "..........................",
        "..........................",
        "..........................",
        "............##............",
        "............##............",
        "#######.....##.....#######",
        "............##............",
        "............##............",
        "............##............",
        "............##............",
    )),
)

START_HEAD = (7, 10)
START_DIR = RIGHT
START_LEN = 4
GROW = 3               # segments gained per food
FOODS_PER_LEVEL = 8    # foods to clear a level
FOOD_POINTS = 10       # x level
LIVES = 3
MAX_QUEUE = 3          # pending turns buffered between ticks

BONUS_EVERY = 5        # a bonus appears after every 5th food eaten
BONUS_TIME = 6.0       # seconds before an uneaten bonus vanishes
BONUS_POINTS = 50      # x level

DEATH_TIME = 1.0
LEVEL_CLEAR_TIME = 1.2

SAVE_VERSION = 1


def step_interval(level: int) -> float:
    """Seconds per snake step at `level`: faster each level, 0.05s floor."""
    return max(0.05, 0.15 * 0.9 ** (level - 1))


def layout_for(level: int):
    """(name, rows) of the wall layout used at `level` (cycles)."""
    return LAYOUTS[(level - 1) % len(LAYOUTS)]


def walls_for(level: int) -> frozenset:
    _, rows = layout_for(level)
    return frozenset(
        (x, y) for y, row in enumerate(rows) for x, c in enumerate(row) if c == "#"
    )


def start_snake() -> list:
    hx, hy = START_HEAD
    dx, dy = DELTAS[START_DIR]
    return [(hx - dx * i, hy - dy * i) for i in range(START_LEN)]


class Phase:
    """A transient animation as an explicit logic state (like tetris'
    Clearing): 'dying' (the dead snake flashes) or 'level_clear'. The state
    change happens when it completes — via update() or resolve_phases()."""

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
        self.eaten_total = 0
        self.paused = False
        self.game_over = False
        self.phase: Phase | None = None
        self.food = None
        self.bonus = None  # [x, y, seconds left] or None
        self._start_level()

    def reset(self) -> None:
        self.__init__(self.rng)

    # -- level / respawn ---------------------------------------------------

    def _start_level(self) -> None:
        self.walls = walls_for(self.level)
        self.foods = 0  # eaten this level
        self.food = None
        self.bonus = None
        self._respawn()

    def _respawn(self) -> None:
        """Snake back at the start, original length, no pending turns."""
        self.snake = start_snake()
        self.dir = START_DIR
        self.queue: list = []
        self.pending_growth = 0
        self.step_timer = 0.0
        if self.food is None or self.food in self.snake:
            self.spawn_food()
        if self.bonus is not None and (self.bonus[0], self.bonus[1]) in self.snake:
            self.bonus = None

    def free_cells(self) -> list:
        taken = set(self.snake) | self.walls
        if self.food is not None:
            taken.add(self.food)
        if self.bonus is not None:
            taken.add((self.bonus[0], self.bonus[1]))
        return [(x, y) for y in range(FIELD_H) for x in range(FIELD_W)
                if (x, y) not in taken]

    def spawn_food(self) -> None:
        self.food = None
        free = self.free_cells()
        self.food = self.rng.choice(free) if free else None

    def _spawn_bonus(self) -> None:
        free = self.free_cells()
        if free:
            x, y = self.rng.choice(free)
            self.bonus = [x, y, BONUS_TIME]

    # -- input -------------------------------------------------------------

    def set_direction(self, d: str) -> bool:
        """Queue a turn. It is compared against the last queued direction (or
        the current one), so a fast double-tap applies on successive ticks;
        repeats and reversals into the neck are ignored."""
        if d not in DELTAS or self.game_over or self.phase is not None:
            return False
        last = self.queue[-1] if self.queue else self.dir
        if d == last or d == OPPOSITE[last] or len(self.queue) >= MAX_QUEUE:
            return False
        self.queue.append(d)
        return True

    # -- stepping ----------------------------------------------------------

    def step(self) -> None:
        """Advance the snake one cell."""
        if self.queue:
            self.dir = self.queue.pop(0)
        dx, dy = DELTAS[self.dir]
        hx, hy = self.snake[0]
        head = (hx + dx, hy + dy)
        # The tail vacates its cell this very tick unless the snake is
        # growing, so moving into it is legal only then.
        body = self.snake if self.pending_growth > 0 else self.snake[:-1]
        if (
            not (0 <= head[0] < FIELD_W and 0 <= head[1] < FIELD_H)
            or head in self.walls
            or head in body
        ):
            self._die()
            return
        self.snake.insert(0, head)
        if self.pending_growth > 0:
            self.pending_growth -= 1
        else:
            self.snake.pop()

        if self.bonus is not None and head == (self.bonus[0], self.bonus[1]):
            self.score += BONUS_POINTS * self.level
            self.bonus = None
        if head == self.food:
            self._eat()

    def _eat(self) -> None:
        self.score += FOOD_POINTS * self.level
        self.pending_growth += GROW
        self.foods += 1
        self.eaten_total += 1
        self.food = None
        if self.foods >= FOODS_PER_LEVEL:
            self.phase = Phase("level_clear", LEVEL_CLEAR_TIME)
            return
        self.spawn_food()
        if self.eaten_total % BONUS_EVERY == 0 and self.bonus is None:
            self._spawn_bonus()

    def _die(self) -> None:
        self.lives -= 1
        self.queue = []
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
            else:
                self.bonus = None
                self._respawn()
        elif phase.kind == "level_clear":
            self.level += 1
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
        changed = False
        if self.bonus is not None:
            self.bonus[2] -= dt
            if self.bonus[2] <= 0:
                self.bonus = None
                changed = True
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
        """Serializable snapshot. The caller resolves phases first (the UI
        save path does), so a save never holds a transition mid-flight."""
        return {
            "version": SAVE_VERSION,
            "score": self.score,
            "level": self.level,
            "lives": self.lives,
            "foods": self.foods,
            "eaten_total": self.eaten_total,
            "snake": [list(c) for c in self.snake],
            "dir": self.dir,
            "queue": list(self.queue),
            "pending_growth": self.pending_growth,
            "food": None if self.food is None else list(self.food),
            "bonus": None if self.bonus is None else list(self.bonus),
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
                x, y = integer(v[0]), integer(v[1])
                if x >= FIELD_W or y >= FIELD_H:
                    raise ValueError(v)
                return (x, y)

            game = cls(rng)
            game.level = integer(data["level"], 1)
            game.walls = walls_for(game.level)
            game.score = integer(data["score"])
            game.lives = integer(data["lives"], 1)
            game.foods = integer(data["foods"])
            if game.foods >= FOODS_PER_LEVEL:
                return None
            game.eaten_total = integer(data["eaten_total"])
            game.game_over = bool(data["game_over"])

            snake = [cell(c) for c in data["snake"]]
            if not snake or len(set(snake)) != len(snake):
                return None
            if any(c in game.walls for c in snake):
                return None
            for (ax, ay), (bx, by) in zip(snake, snake[1:]):
                if abs(ax - bx) + abs(ay - by) != 1:
                    return None
            game.snake = snake

            if data["dir"] not in DELTAS:
                return None
            game.dir = data["dir"]
            queue = data["queue"]
            if not isinstance(queue, list) or len(queue) > MAX_QUEUE:
                return None
            if any(d not in DELTAS for d in queue):
                return None
            game.queue = list(queue)
            game.pending_growth = integer(data["pending_growth"])
            game.step_timer = num(data["step_timer"])

            food = data["food"]
            game.food = None if food is None else cell(food)
            if game.food is not None and (game.food in game.walls or game.food in snake):
                return None
            bonus = data["bonus"]
            if bonus is None:
                game.bonus = None
            else:
                if not isinstance(bonus, list) or len(bonus) != 3:
                    return None
                bx, by = cell(bonus[:2])
                if (bx, by) in game.walls or (bx, by) in snake:
                    return None
                game.bonus = [bx, by, num(bonus[2])]

            # Restoring the RNG makes a resumed game place food exactly as
            # the saved one would have.
            state = _rng_from_json(data["rng"])
            if state is None:
                return None
            game.rng.setstate(state)
            game.phase = None
            return game
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            return None


TITLE = "Snake"

# Every key the game reacts to (upper case works too). The controls box in
# the side column lists these; narrower tiers show a one-line hint instead.
CONTROLS = [
    ("arrows", "steer"),
    ("h j k l", "steer (vi)"),
    ("w a s d", "steer"),
    ("?", "help"),
    ("p / r", "pause / restart"),
    ("q", "menu"),
]

HELP = [
    ("Goal",
     f"Guide the snake to the food () to make it grow. Eat {FOODS_PER_LEVEL} "
     "pieces of food to clear a level; every level brings a new wall layout "
     "and a faster snake. Score as much as you can before your lives run "
     "out."),
    ("Controls",
     "- arrow keys, h j k l or w a s d: turn the snake. It never stops "
     "moving; a key only changes its direction.\n"
     "- Turning straight back into your own neck is ignored.\n"
     f"- Up to {MAX_QUEUE} turns are remembered between steps, so a quick "
     "double tap (say up then left) makes a tight U-turn.\n"
     "- p pause, r restart (abandons the game, keeps your best), "
     "q back to the menu (the game is saved and comes back paused), "
     "? this help."),
    ("How it works",
     f"The field is {FIELD_W} by {FIELD_H} cells inside a solid border. "
     f"Each food makes the snake {GROW} segments longer. You lose a life "
     "when the head hits the border, a wall block (blue shaded) or the "
     "snake's own body - the cell the tail is just leaving is safe, though.\n"
     f"You start with {LIVES} lives. After a crash the snake flashes, then "
     "starts again at its original length from the start position; the "
     "food eaten on this level still counts. With no lives left the game "
     "is over.\n"
     f"Clearing a level ({FOODS_PER_LEVEL} foods) moves you to the next "
     "wall layout (open, bars, pillars, rooms, then round again) with the "
     "snake about 10% faster and back to its starting length.\n"
     f"After every {BONUS_EVERY}th food a bonus $$ appears for "
     f"{BONUS_TIME:g} seconds; it blinks when it is about to vanish."),
    ("Scoring",
     f"- food: {FOOD_POINTS} x level.\n"
     f"- bonus $$: {BONUS_POINTS} x level.\n"
     "Your best score is kept between games."),
    ("Tips",
     "- Plan your route around the walls before you turn; the snake gets "
     "long quickly.\n"
     "- Hug the border or run in wide loops to keep open space in front of "
     "you.\n"
     "- The bonus is worth five foods: go for it only if the path is "
     "safe.\n"
     "- Pause (p) before looking away - the snake keeps moving."),
]

# Adaptive layout. The popup's inner area is 93x28 (common.POPUP_COLS/ROWS);
# row 0 is the header/banner and the composition below it (the field window,
# 26 cells x 2 cols + border = 54 x 23, plus a side column of up to 30
# columns holding the stats, controls and agents boxes) is centred at every
# size. Too narrow for a side column, the field stands alone, the header
# carries the stats and a key hint sits on the last row (or in the header
# when there is no spare row).
FIELD_WIN_W = FIELD_W * 2 + 2
FIELD_WIN_H = FIELD_H + 2
MIN_COLS = FIELD_WIN_W
MIN_ROWS = FIELD_WIN_H + 1
STATS_H = 8  # six stat lines + frame


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
    """Pure layout decision: tier plus each region's (y, x, height, width).
    Tiers: 'full' (field + side column of stats, controls and agents),
    'field' (field alone; stats in the header, a key hint row), 'too_small'.
    """
    plan = {"tier": "too_small", "too_small": True,
            "field": None, "stats": None, "controls": None, "agents": None,
            "hint": None, "hint_in_header": False}
    if cols < MIN_COLS or rows < MIN_ROWS:
        return plan
    plan["too_small"] = False
    side_w = side_width(cols, FIELD_WIN_W)
    if not side_w:
        plan["tier"] = "field"
        usable_rows = rows
        if rows >= MIN_ROWS + 1:
            plan["hint"] = (rows - 1, 0, 1, cols)
            usable_rows = rows - 1
        else:
            plan["hint_in_header"] = True
        y, x = center_origin(cols, usable_rows, FIELD_WIN_W, FIELD_WIN_H)
        plan["field"] = (y, x, FIELD_WIN_H, FIELD_WIN_W)
        return plan
    plan["tier"] = "full"
    y, x = center_origin(cols, rows, FIELD_WIN_W + 1 + side_w, FIELD_WIN_H)
    plan["field"] = (y, x, FIELD_WIN_H, FIELD_WIN_W)
    plan["stats"], plan["controls"], plan["agents"] = side_stack(
        y, x + FIELD_WIN_W + 1, side_w, FIELD_WIN_H, STATS_H,
        controls_height(CONTROLS, side_w))
    return plan


# --------------------------------------------------------------------------
# curses UI
# --------------------------------------------------------------------------

# Colour pairs 40-59 are shared by the later games (one runs at a time);
# they are registered in run() on every start (see common.py).
PAIR_BODY, PAIR_HEAD, PAIR_WALL, PAIR_FOOD = 40, 41, 42, 43
PAIR_BONUS, PAIR_DEAD, PAIR_FLASH = 44, 45, 46

CELL = "██"  # every field cell is exactly two columns
WALL = "░░"
FOOD = "()"
BONUS = "$$"

def init_colors() -> None:
    bg = default_bg()
    curses.init_pair(PAIR_BODY, curses.COLOR_GREEN, bg)
    curses.init_pair(PAIR_HEAD, curses.COLOR_YELLOW, bg)
    curses.init_pair(PAIR_WALL, curses.COLOR_BLUE, bg)
    curses.init_pair(PAIR_FOOD, curses.COLOR_RED, bg)
    curses.init_pair(PAIR_BONUS, curses.COLOR_MAGENTA, bg)
    curses.init_pair(PAIR_DEAD, curses.COLOR_RED, bg)
    curses.init_pair(PAIR_FLASH, curses.COLOR_WHITE, bg)


class Layout:
    """Screen regions for the current size; rebuilt on resize."""

    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.rows, self.cols = stdscr.getmaxyx()
        self.plan = layout_plan(self.cols, self.rows)
        self.too_small = self.plan["too_small"]
        self.hint = self.plan["hint"]
        self.hint_in_header = self.plan["hint_in_header"]
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
    safe_addstr(win, row, max(1, (FIELD_WIN_W - len(text)) // 2), text, attr)


def draw_field(win, game: Game) -> None:
    phase = game.phase
    wall_attr = curses.color_pair(PAIR_WALL)
    if phase is not None and phase.kind == "level_clear" and int(phase.elapsed * 6) % 2:
        wall_attr = curses.color_pair(PAIR_FLASH) | curses.A_BOLD
    for x, y in game.walls:
        _put(win, x, y, WALL, wall_attr)

    if game.food is not None:
        _put(win, game.food[0], game.food[1], FOOD,
             curses.color_pair(PAIR_FOOD) | curses.A_BOLD)
    if game.bonus is not None:
        bx, by, left = game.bonus
        if left > 2.0 or int(left * 5) % 2 == 0 or game.paused:
            _put(win, bx, by, BONUS, curses.color_pair(PAIR_BONUS) | curses.A_BOLD)

    dying = phase is not None and phase.kind == "dying"
    show = not dying or int(phase.elapsed * 8) % 2 == 0
    if show and not game.game_over:
        body_attr = curses.color_pair(PAIR_DEAD if dying else PAIR_BODY)
        head_attr = curses.color_pair(PAIR_DEAD if dying else PAIR_HEAD) | curses.A_BOLD
        for x, y in game.snake[1:]:
            _put(win, x, y, CELL, body_attr)
        hx, hy = game.snake[0]
        _put(win, hx, hy, CELL, head_attr)

    mid = 1 + FIELD_H // 2
    if game.game_over:
        _centered(win, mid - 1, " GAME OVER ", curses.A_STANDOUT | curses.A_BOLD)
        _centered(win, mid + 1, " r restart | q menu ", curses.A_STANDOUT)
    elif game.paused:
        _centered(win, mid - 2, " PAUSED ", curses.A_STANDOUT | curses.A_BOLD)
    elif phase is not None and phase.kind == "level_clear":
        _centered(win, mid - 2, f" LEVEL {game.level + 1} ",
                  curses.color_pair(PAIR_HEAD) | curses.A_BOLD)


def draw_stats(win, game: Game, best: int) -> None:
    rows, cols = win.getmaxyx()
    inner = cols - 2
    safe_addstr(win, 0, 2, f" {TITLE.lower()} "[: max(0, cols - 3)], curses.A_BOLD)
    if game.game_over:
        state = "GAME OVER"
    elif game.paused:
        state = "paused"
    elif game.phase is not None:
        state = "ouch!" if game.phase.kind == "dying" else "level clear!"
    else:
        state = layout_for(game.level)[0]
    lines = [
        (f"score {game.score}", curses.A_BOLD),
        (f"best  {max(best, game.score)}", 0),
        (f"level {game.level}", 0),
        (f"lives {game.lives}", curses.color_pair(PAIR_HEAD)),
        (f"food  {game.foods}/{FOODS_PER_LEVEL}", curses.color_pair(PAIR_FOOD)),
        (state, curses.A_DIM),
    ]
    for i, (line, attr) in enumerate(lines[: max(0, rows - 2)]):
        safe_addstr(win, 1 + i, 1, line[:inner], attr)


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


def draw(game: Game, lay: Layout, session, resume: bool, best: int,
         help_view=None) -> None:
    scr = lay.stdscr
    scr.erase()
    if lay.too_small:
        need = f"need {MIN_COLS}x{MIN_ROWS}, have {lay.cols}x{lay.rows}"
        lines = [f"pane too small: {need}", "resize the pane (? help, q menu)"]
        if len(lines[0]) >= lay.cols:  # narrow: one short phrase per row
            lines = ["pane too small", need.split(", ")[0], need.split(", ")[1],
                     "resize the pane", "(? help, q menu)"]
        top = max(0, (lay.rows - len(lines)) // 2)
        for i, line in enumerate(lines[: lay.rows]):
            line = line[: max(0, lay.cols - 1)]
            safe_addstr(scr, top + i, max(0, (lay.cols - len(line)) // 2), line,
                        curses.A_BOLD if i == 0 else 0)
        if help_view is not None:
            help_view.draw(scr)
        return

    if not draw_banner(scr, lay.cols, session.attention, resume):
        if game.game_over:
            head = " snake - GAME OVER "
        else:
            head = f" snake - {'paused' if game.paused else 'playing'} "
        if lay.stats is None:  # no stats box in this tier: score goes here
            head += (f"score {game.score} | lives {game.lives} | lvl {game.level}"
                     f" | food {game.foods}/{FOODS_PER_LEVEL} ")
            if lay.hint_in_header and len(head) + 7 > lay.cols - 1:
                # The key hint needs room too; the field itself shows PAUSED
                # and GAME OVER, so the state can go.
                head = (f" score {game.score} | lives {game.lives} | lvl"
                        f" {game.level} | food {game.foods}/{FOODS_PER_LEVEL} ")
        draw_header(scr, lay.cols, head,
                    curses.A_STANDOUT | (curses.A_BOLD if game.game_over else 0),
                    lay.hint_in_header)
    elif lay.hint_in_header:
        # The banner owns row 0; keep '? help' visible at its right end.
        safe_addstr(scr, 0, max(0, lay.cols - 1 - len(" ? help")), " ? help",
                    curses.A_DIM)

    lay.boxes()
    draw_field(lay.field, game)
    if lay.stats is not None:
        draw_stats(lay.stats, game, best)
    if lay.controls is not None:
        draw_controls(lay.controls, CONTROLS)
    if lay.agents is not None:
        draw_agents(lay.agents, session.roster, session.status_pairs)
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


KEY_DIRS = {
    curses.KEY_UP: UP, curses.KEY_DOWN: DOWN,
    curses.KEY_LEFT: LEFT, curses.KEY_RIGHT: RIGHT,
}
for _keys, _d in (("kKwW", UP), ("jJsS", DOWN), ("hHaA", LEFT), ("lLdD", RIGHT)):
    for _k in _keys:
        KEY_DIRS[ord(_k)] = _d


def run(session) -> None:
    """Play Snake until `q` (back to the arcade menu) or SIGTERM. The game is
    saved on every exit path and resumed, paused, on the next run."""
    scr = session.stdscr
    init_colors()
    game_path = session.path("snake-game.json")
    hiscore_path = session.path("snake-hiscore.json")
    hiscore = load_hiscore(hiscore_path)
    help_view = HelpOverlay(TITLE, HELP)

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
                elif paused_by_help:
                    # The banner now owns this pause: closing help must not
                    # lift it before the alert is acknowledged.
                    paused_by_attention = True
                    paused_by_help = False
                dirty = True

            quit_now = False
            while True:
                ch = scr.getch()
                if ch == -1:
                    break
                dirty = True
                consumed, paused_by_help = help_key(help_view, game, ch,
                                                    paused_by_help)
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
                    if not game.paused:
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
                draw(game, lay, session, resume, hiscore, help_view)
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

    sys.exit(arcade.main(["--game", "snake"] + sys.argv[1:]))
