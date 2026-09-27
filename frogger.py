#!/usr/bin/env python3
"""Frogger for the Herdr arcade (see arcade.py, which runs it via `run()`).

Structure: everything above the "curses UI" divider is pure game logic —
lanes, frog, hops, riding, collisions, scoring, lives, levels, save/restore
and the layout plan — with no terminal dependency, so
`tests/test_frogger.py` imports this module without a terminal.

Geometry: the field is COLS cells wide and ROWS lanes tall; one cell is one
frog wide and is drawn two terminal columns wide. Row 0 is the top bank with
the five home bays, rows 1-5 the river, row 6 the median, rows 7-11 the road
and row 12 the start bank. x positions are floats in cell units; a frog at
`fx` occupies [fx, fx + 1). Lane objects move continuously and wrap around a
period wider than the field, so they always enter and leave off-screen.
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

COLS = 16                 # field width in cells (frog widths)
ROWS = 13                 # lanes, top (home) to bottom (start bank)
HOME_ROW = 0
RIVER_ROWS = (1, 2, 3, 4, 5)
MEDIAN_ROW = 6
ROAD_ROWS = (7, 8, 9, 10, 11)
START_ROW = 12
START_X = 7

MARGIN = 6                # off-screen run-out on each side (>= longest object)
PERIOD = COLS + 2 * MARGIN

BAY_X = (1, 4, 7, 10, 13)  # left cell of each home bay
BAY_W = 2                  # each bay is two cells wide

# Lane definitions: row -> (kind, base speed in cells/s, objects as
# (period position, length), indices of turtle groups that dive).
LANES = {
    1: ("log", 1.3, ((0, 4), (10, 4), (19, 4)), ()),
    2: ("turtle", -1.6, ((0, 2), (6, 2), (12, 2), (18, 2), (24, 2)), (2,)),
    3: ("log", 1.8, ((0, 6), (14, 6)), ()),
    4: ("log", 1.0, ((0, 4), (9, 4), (18, 4)), ()),
    5: ("turtle", -1.3, ((0, 3), (7, 3), (14, 3), (21, 3)), (1,)),
    7: ("truck", -0.9, ((0, 3), (13, 3)), ()),
    8: ("racer", 3.6, ((5, 1),), ()),
    9: ("van", -1.1, ((0, 2), (9, 2), (18, 2)), ()),
    10: ("car", 1.8, ((2, 1), (11, 1), (20, 1)), ()),
    11: ("car2", -1.4, ((0, 1), (7, 1), (14, 1), (21, 1)), ()),
}

# Diving turtles: one cycle, split into states. Only "under" is unsafe.
DIVE_CYCLE = 6.0
DIVE_UP, DIVE_SINK, DIVE_UNDER = 3.5, 4.2, 5.3   # state boundaries in seconds
DIVE_GROUP_PHASE = 1.3                            # per-group stagger

HIT_EPS = 0.2              # overlap forgiveness for vehicle hits (cells)
TIME_LIMIT = 30.0          # seconds per frog
LIVES = 3
DEATH_DURATION = 1.0
CLEAR_DURATION = 1.5
MAX_DT = 0.5               # clamp for stalls (the sweep covers big steps)

ROW_POINTS = 10
HOME_POINTS = 50
TIME_BONUS = 10            # per whole second left
LEVEL_POINTS = 1000
FLY_POINTS = 200
FLY_SHOW = 5.0
FLY_WAIT = (4.0, 9.0)

SAVE_VERSION = 1


def row_kind(row: int) -> str:
    if row == HOME_ROW:
        return "home"
    if row in RIVER_ROWS:
        return "river"
    if row in ROAD_ROWS:
        return "road"
    return "safe"  # median and start bank


def speed_mult(level: int) -> float:
    """Lane speed factor: +25% per level, capped at 3x so no object can wrap
    around the lane period within one MAX_DT step (keeps the sweep exact)."""
    return min(3.0, 1.0 + 0.25 * (level - 1))


def bay_at(center: float):
    """Index of the home bay containing x `center`, or None."""
    for i, bx in enumerate(BAY_X):
        if bx <= center < bx + BAY_W:
            return i
    return None


class Phase:
    """A transient animation as an explicit logic phase: 'dying' (the frog
    lies dead where it fell) or 'cleared' (all five bays full). Resolved
    when elapsed reaches duration, or at once via finish_phase()."""

    __slots__ = ("kind", "elapsed", "duration", "cause")

    def __init__(self, kind, duration, cause=None):
        self.kind = kind
        self.elapsed = 0.0
        self.duration = duration
        self.cause = cause


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
        self.lives = LIVES
        self.level = 1
        self.paused = False
        self.game_over = False
        self.phase: Phase | None = None
        self.homes = [False] * len(BAY_X)
        self.clock = 0.0
        self.offsets: dict = {}
        self.fly = None           # bay index or None
        self.fly_timer = 0.0      # time left showing (fly) or waiting (no fly)
        self._start_level()

    def reset(self) -> None:
        self.__init__(self.rng)

    def _start_level(self) -> None:
        self.homes = [False] * len(BAY_X)
        self.offsets = {row: self.rng.uniform(0, PERIOD) for row in LANES}
        self.fly = None
        self.fly_timer = self.rng.uniform(*FLY_WAIT)
        self._new_frog()

    def _new_frog(self) -> None:
        self.fx = float(START_X)
        self.row = START_ROW
        self.best_row = START_ROW
        self.time_left = TIME_LIMIT

    # -- lanes -------------------------------------------------------------

    def lane_speed(self, row: int) -> float:
        return LANES[row][1] * speed_mult(self.level)

    def objects(self, row: int):
        """(x, length, index) of every object in a lane, at field coords."""
        if row not in LANES:
            return []
        off = self.offsets[row]
        return [(((pos + off) % PERIOD) - MARGIN, length, i)
                for i, (pos, length) in enumerate(LANES[row][2])]

    def set_object_x(self, row: int, index: int, x: float) -> None:
        """Place object `index` of lane `row` at field x (moves the lane)."""
        pos = LANES[row][2][index][0]
        self.offsets[row] = (x + MARGIN - pos) % PERIOD

    def turtle_state(self, row: int, index: int) -> str:
        """'up', 'sinking', 'under' or 'rising' for a turtle group."""
        if LANES[row][0] != "turtle" or index not in LANES[row][3]:
            return "up"
        t = (self.clock + index * DIVE_GROUP_PHASE) % DIVE_CYCLE
        if t < DIVE_UP:
            return "up"
        if t < DIVE_SINK:
            return "sinking"
        if t < DIVE_UNDER:
            return "under"
        return "rising"

    def support(self):
        """The river object under the frog's centre if it is safe to stand
        on, else None."""
        center = self.fx + 0.5
        for x, length, i in self.objects(self.row):
            if x <= center < x + length and self.turtle_state(self.row, i) != "under":
                return (x, length, i)
        return None

    def _overlaps(self, lo: float, hi: float) -> bool:
        return lo < self.fx + 1 - HIT_EPS and hi > self.fx + HIT_EPS

    def vehicle_hit(self) -> bool:
        """A vehicle overlaps the frog right now."""
        return any(self._overlaps(x, x + length)
                   for x, length, _ in self.objects(self.row))

    # -- actions -----------------------------------------------------------

    @property
    def alive(self) -> bool:
        return not self.game_over and self.phase is None

    def hop(self, dx: int, drow: int) -> bool:
        """One hop: dx cells sideways or drow rows (negative = forward).
        Returns True if the frog moved."""
        if self.paused or not self.alive:
            return False
        if drow:
            row = self.row + drow
            if row < HOME_ROW or row > START_ROW:
                return False
            self.row = row
            if row_kind(row) in ("road", "safe"):
                self.fx = float(min(COLS - 1, max(0, round(self.fx))))
            if row < self.best_row and row != HOME_ROW:
                self.score += ROW_POINTS * (self.best_row - row)
                self.best_row = row
        else:
            new = min(float(COLS - 1), max(0.0, self.fx + dx))
            if (new - self.fx) * dx <= 0:
                return False
            self.fx = new
        self._check_landing()
        return True

    def _check_landing(self) -> None:
        kind = row_kind(self.row)
        if kind == "home":
            bay = bay_at(self.fx + 0.5)
            if bay is None:
                self._die("bank")
            elif self.homes[bay]:
                self._die("occupied")
            else:
                self._fill_home(bay)
        elif kind == "river":
            if self.support() is None:
                self._die("water")
        elif kind == "road":
            if self.vehicle_hit():
                self._die("vehicle")

    def _fill_home(self, bay: int) -> None:
        self.homes[bay] = True
        self.fx = float(BAY_X[bay]) + 0.5
        self.score += HOME_POINTS + int(max(0.0, self.time_left)) * TIME_BONUS
        if self.fly == bay:
            self.score += FLY_POINTS
            self.fly = None
            self.fly_timer = self.rng.uniform(*FLY_WAIT)
        if all(self.homes):
            self.score += LEVEL_POINTS
            self.phase = Phase("cleared", CLEAR_DURATION)
        else:
            self._new_frog()

    def _die(self, cause: str) -> None:
        self.phase = Phase("dying", DEATH_DURATION, cause)

    def finish_phase(self) -> None:
        """Resolve a pending phase immediately (pause, quit, save). The state
        afterwards is exactly what the animation would have led to."""
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
                self._new_frog()
        else:
            self.level += 1
            self._start_level()

    # -- time --------------------------------------------------------------

    def _advance_lanes(self, dt: float) -> bool:
        """Move every lane by dt. Returns True if a vehicle swept across the
        (living) frog during the step — the swept interval from start to end
        position is tested, so a fast vehicle cannot tunnel through."""
        hit = False
        check = self.alive and row_kind(self.row) == "road"
        for row in LANES:
            v = self.lane_speed(row)
            if check and row == self.row:
                for x0, length, _ in self.objects(row):
                    x1 = x0 + v * dt
                    if self._overlaps(min(x0, x1), max(x0, x1) + length):
                        hit = True
            self.offsets[row] = (self.offsets[row] + v * dt) % PERIOD
        self.clock = (self.clock + dt) % (DIVE_CYCLE * 1000)
        return hit

    def update(self, dt: float) -> bool:
        """Advance the game by dt seconds. True if anything changed."""
        if self.paused or self.game_over:
            return False
        dt = min(max(dt, 0.0), MAX_DT)
        if self.phase is not None:
            self._advance_lanes(dt)
            self.phase.elapsed += dt
            if self.phase.elapsed >= self.phase.duration:
                self.finish_phase()
            return True
        carried = row_kind(self.row) == "river" and self.support() is not None
        if self._advance_lanes(dt):
            self._die("vehicle")
            return True
        if row_kind(self.row) == "river":
            if carried:
                self.fx += self.lane_speed(self.row) * dt
            center = self.fx + 0.5
            if center < 0 or center > COLS:
                self._die("edge")
                return True
            if self.support() is None:
                self._die("water")
                return True
        self._update_fly(dt)
        self.time_left -= dt
        if self.time_left <= 0:
            self.time_left = 0.0
            self._die("timeout")
        return True

    def _update_fly(self, dt: float) -> None:
        self.fly_timer -= dt
        if self.fly_timer > 0:
            return
        if self.fly is not None:
            self.fly = None
            self.fly_timer = self.rng.uniform(*FLY_WAIT)
            return
        empty = [i for i, full in enumerate(self.homes) if not full]
        if empty:
            self.fly = self.rng.choice(empty)
            self.fly_timer = FLY_SHOW
        else:
            self.fly_timer = self.rng.uniform(*FLY_WAIT)

    # -- save / restore ----------------------------------------------------

    def to_save(self) -> dict:
        """Serializable snapshot. Taken with no phase pending: the caller
        runs finish_phase() first (the UI save path does)."""
        return {
            "version": SAVE_VERSION,
            "score": self.score,
            "lives": self.lives,
            "level": self.level,
            "homes": list(self.homes),
            "clock": self.clock,
            "offsets": {str(r): o for r, o in self.offsets.items()},
            "fly": self.fly,
            "fly_timer": self.fly_timer,
            "fx": self.fx,
            "row": self.row,
            "best_row": self.best_row,
            "time_left": self.time_left,
            "rng": _rng_to_json(self.rng),
        }

    @classmethod
    def from_save(cls, data, rng=None):
        """Rebuild a saved game, or None for anything corrupt/incompatible."""
        try:
            if not isinstance(data, dict) or data.get("version") != SAVE_VERSION:
                return None
            game = cls(rng)

            def num(v, lo, hi):
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    raise ValueError
                v = float(v)
                if not (lo <= v <= hi) or math.isnan(v):
                    raise ValueError
                return v

            def whole(v, lo, hi):
                if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                    raise ValueError
                return v

            homes = data["homes"]
            if (not isinstance(homes, list) or len(homes) != len(BAY_X)
                    or not all(isinstance(h, bool) for h in homes) or all(homes)):
                return None
            offsets = data["offsets"]
            if not isinstance(offsets, dict) or set(offsets) != {str(r) for r in LANES}:
                return None
            fly = data["fly"]
            if fly is not None:
                whole(fly, 0, len(BAY_X) - 1)
                if homes[fly]:
                    return None
            state = _rng_from_json(data.get("rng"))
            if state is None:
                return None
            game.score = whole(data["score"], 0, 10 ** 12)
            game.lives = whole(data["lives"], 1, LIVES)
            game.level = whole(data["level"], 1, 10 ** 6)
            game.homes = list(homes)
            game.clock = num(data["clock"], 0, 1e9)
            game.offsets = {r: num(offsets[str(r)], 0, PERIOD) for r in LANES}
            game.fly = fly
            game.fly_timer = num(data["fly_timer"], -1, 100)
            game.row = whole(data["row"], 1, START_ROW)
            game.fx = num(data["fx"], -1, COLS)
            game.best_row = whole(data["best_row"], 1, game.row)
            game.time_left = num(data["time_left"], 0, TIME_LIMIT)
            game.rng.setstate(state)
            return game
        except (KeyError, TypeError, ValueError, IndexError, OverflowError):
            return None


TITLE = "Frogger"

# Every key the game reacts to (upper-case letters work too).
CONTROLS = [
    ("arrows", "hop"),
    ("wasd hjkl", "hop too"),
    ("?", "help"),
    ("p / r", "pause / restart"),
    ("q", "menu"),
]

HELP = [
    ("Goal",
     "Guide frogs from the bottom bank, across a busy road and a river, "
     "into the five home bays along the top. Fill all five bays to clear "
     "the level; each level runs faster."),
    ("Controls",
     "- arrows (or w a s d / h j k l): hop one lane up or down, or one "
     "cell left or right. You can hop back down too.\n"
     "- p pauses, r starts a new game, q goes back to the arcade menu.\n"
     "- ? opens this help (the game pauses while it is open)."),
    ("The road",
     "The lower half is a road. Cars, vans, trucks and a fast racer drive "
     "along each lane and wrap around. Touching any vehicle kills the frog. "
     "The dotted strip in the middle and the bottom bank are safe."),
    ("The river",
     "The upper half is water: falling in kills. Hop onto logs and turtles "
     "and they carry you along. Being carried off the left or right edge "
     "of the field also kills."),
    ("Diving turtles",
     "Some turtle groups dive on a 6 second cycle: () is up and safe, oo "
     "means they are sinking or rising (still safe), ~~ means they are "
     "under water. A frog standing on them when they go under drowns, so "
     "hop off as soon as they turn to oo."),
    ("Home bays",
     "The top bank (##) has five gaps, the bays. Hop straight into an "
     "empty bay to save the frog; a new frog then starts at the bottom. "
     "Landing on the ## bank between bays, or in a bay that already holds "
     "a frog, kills. Now and then a fly (**) sits in an empty bay for a "
     "few seconds: catch it for a bonus."),
    ("Timer and lives",
     "Each frog has 30 seconds (the bar in the stats box, red near the "
     "end); if it runs out the frog dies. A new frog gets a fresh timer. "
     "You have 3 lives; the game is over when the last frog dies."),
    ("What kills you",
     "- being hit by any vehicle (hopping into one or it driving into you)\n"
     "- landing in the water instead of on a log or turtle\n"
     "- standing on diving turtles when they go under (~~)\n"
     "- being carried off the left or right edge on a log or turtle\n"
     "- landing on the ## bank between bays, or in a bay already holding a frog\n"
     "- the 30 second timer running out"),
    ("Scoring",
     "- 10 per lane of new forward progress (per frog)\n"
     "- 50 for reaching a bay, plus 10 per whole second left\n"
     "- 200 extra for catching the fly\n"
     "- 1000 for filling all five bays (level clear)"),
    ("Legend",
     "- @@ your frog (XX / ** when it dies)\n"
     "- (====) log     () turtles     oo turtles diving     ~~ water\n"
     "- o> <o cars and the racer     [=> <===] vans and trucks\n"
     "- ## top bank     .. safe strip and start bank\n"
     "- @@ in a bay: a saved frog     ** in a bay: fly bonus"),
    ("Tips",
     "- Wait on the safe strip for a gap rather than rushing the road.\n"
     "- Long logs are the safest rides; watch turtles for oo.\n"
     "- Every second left is worth 10 points, but a dead frog costs more.\n"
     "- Grab the fly only if its bay is reachable in time."),
]

# Layout: the field window hugs the grid (COLS cells * 2 columns + frame).
# Next to it, when there is room, a side column of stacked boxes: stats,
# controls, agents. Row 0 is the header/banner; everything else is centred
# as one block below it.
FIELD_W = COLS * 2 + 2     # 34
FIELD_H = ROWS + 2         # 15
STATS_H = 9
AGENTS_MIN_H = 3
AGENTS_PREF_H = 8          # the side column grows to this much roster
MIN_COLS = FIELD_W
MIN_ROWS = 1 + FIELD_H     # header + field


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
    side_w = side_width(cols, FIELD_W)
    ctrl_h = controls_height(CONTROLS, side_w) if side_w else 0
    if side_w and avail >= ctrl_h:
        stats_h = STATS_H if avail >= ctrl_h + STATS_H else 0
        agents = bool(stats_h) and avail >= ctrl_h + stats_h + AGENTS_MIN_H
        side_h = ctrl_h + stats_h + (AGENTS_PREF_H if agents else 0)
        comp_h = min(avail, max(FIELD_H, side_h))
        comp_w = FIELD_W + 1 + side_w
        y0, x0 = center_origin(cols, rows, comp_w, comp_h)
        plan["field"] = (y0, x0, FIELD_H, FIELD_W)
        sx, y = x0 + FIELD_W + 1, y0
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
    hint = avail >= FIELD_H + 1
    y0, x0 = center_origin(cols, rows - (1 if hint else 0), FIELD_W, FIELD_H)
    plan["field"] = (y0, x0, FIELD_H, FIELD_W)
    if hint:
        plan["hint"] = (rows - 1, 0, 1, cols)
    return plan


# --------------------------------------------------------------------------
# curses UI
# --------------------------------------------------------------------------

# Colour pairs 50-59 (inside the shared 40-59 game range, see common.py).
PAIR_FROG, PAIR_WATER, PAIR_LOG, PAIR_TURTLE = 50, 51, 52, 53
PAIR_CAR_A, PAIR_CAR_B, PAIR_TRUCK, PAIR_GRASS = 54, 55, 56, 57
PAIR_STRIP, PAIR_ALERT = 58, 59

LANE_PAIRS = {"log": PAIR_LOG, "turtle": PAIR_TURTLE, "truck": PAIR_TRUCK,
              "racer": PAIR_ALERT, "van": PAIR_CAR_B, "car": PAIR_CAR_A,
              "car2": PAIR_CAR_B}

FROG = "@@"
DEATH_GLYPHS = ("XX", "**")


def init_colors() -> None:
    bg = default_bg()
    for pair, fg in ((PAIR_FROG, curses.COLOR_GREEN), (PAIR_WATER, curses.COLOR_BLUE),
                     (PAIR_LOG, curses.COLOR_YELLOW), (PAIR_TURTLE, curses.COLOR_RED),
                     (PAIR_CAR_A, curses.COLOR_MAGENTA), (PAIR_CAR_B, curses.COLOR_CYAN),
                     (PAIR_TRUCK, curses.COLOR_WHITE), (PAIR_GRASS, curses.COLOR_GREEN),
                     (PAIR_STRIP, curses.COLOR_MAGENTA), (PAIR_ALERT, curses.COLOR_RED)):
        curses.init_pair(pair, fg, bg)


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


def object_glyph(kind: str, length: int, speed: float, state: str = "up") -> str:
    """Text for one lane object, two characters per cell."""
    n = 2 * length
    if kind == "log":
        return "(" + "=" * (n - 2) + ")"
    if kind == "turtle":
        cell = {"up": "()", "sinking": "oo", "rising": "oo", "under": "~~"}[state]
        return cell * length
    if length == 1:
        return "o>" if speed > 0 else "<o"
    if speed > 0:
        return "[" + "=" * (n - 2) + ">"
    return "<" + "=" * (n - 2) + "]"


def _put_span(win, y, col, text, attr):
    """Draw `text` starting at field character column `col`, clipped."""
    start = max(0, -col)
    end = min(len(text), COLS * 2 - col)
    if start < end:
        safe_addstr(win, y, 1 + col + start, text[start:end], attr)


def time_bar(game, width: int) -> str:
    filled = int(math.ceil(max(0.0, game.time_left) / TIME_LIMIT * width))
    return "#" * filled + "." * (width - filled)


def draw_field(win, game):
    cp = curses.color_pair
    for r in range(ROWS):
        y = 1 + r
        kind = row_kind(r)
        if kind == "home":
            safe_addstr(win, y, 1, "##" * COLS, cp(PAIR_GRASS))
            for i, bx in enumerate(BAY_X):
                if game.homes[i]:
                    text, attr = " " + FROG + " ", cp(PAIR_FROG) | curses.A_BOLD
                elif game.fly == i:
                    text, attr = " ** ", cp(PAIR_LOG) | curses.A_BOLD
                else:
                    text, attr = "    ", 0
                safe_addstr(win, y, 1 + 2 * bx, text, attr)
        elif kind == "river":
            safe_addstr(win, y, 1, "~~" * COLS, cp(PAIR_WATER) | curses.A_DIM)
        elif kind == "safe":
            safe_addstr(win, y, 1, ".." * COLS, cp(PAIR_STRIP))
        if r in LANES:
            lkind, _, _, _ = LANES[r]
            v = game.lane_speed(r)
            for x, length, i in game.objects(r):
                state = game.turtle_state(r, i)
                text = object_glyph(lkind, length, v, state)
                attr = cp(PAIR_WATER) if state == "under" else cp(LANE_PAIRS[lkind])
                if lkind == "log":
                    attr |= curses.A_BOLD
                _put_span(win, y, int(round(x * 2)), text, attr)
    # The frog (not while it sits in a bay at the end of a level).
    if game.game_over or (game.phase is not None and game.phase.kind == "cleared"):
        return
    col = min(COLS * 2 - 2, max(0, int(round(game.fx * 2))))
    if game.phase is not None:
        glyph = DEATH_GLYPHS[int(game.phase.elapsed * 8) % 2]
        attr = cp(PAIR_ALERT) | curses.A_BOLD
    else:
        glyph, attr = FROG, cp(PAIR_FROG) | curses.A_BOLD
    safe_addstr(win, 1 + game.row, 1 + col, glyph, attr)


def _overlay(win, lines):
    rows, cols = win.getmaxyx()
    y = rows // 2 - len(lines) // 2
    for i, (text, attr) in enumerate(lines):
        safe_addstr(win, y + i, max(1, (cols - len(text)) // 2), text[: cols - 2], attr)


def draw(game, lay, session, resume=False, best=0, help=None):
    scr = lay.stdscr
    scr.erase()
    if lay.too_small:
        msg = f"pane too small: need {MIN_COLS}x{MIN_ROWS}, have {lay.cols}x{lay.rows}"
        safe_addstr(scr, lay.rows // 2, max(0, (lay.cols - len(msg)) // 2),
                    msg[: max(0, lay.cols - 1)], curses.A_BOLD)
        msg2 = "resize the pane (q menu)"
        safe_addstr(scr, lay.rows // 2 + 1, max(0, (lay.cols - len(msg2)) // 2),
                    msg2[: max(0, lay.cols - 1)])
        if help is not None:
            help.draw(scr)
        return

    if not draw_banner(scr, lay.cols, session.attention, resume):
        state = "GAME OVER" if game.game_over else ("paused" if game.paused else "playing")
        head = f" frogger - {state} "
        if lay.plan["hint"] is None and lay.controls is None:
            head += "| ? help "  # no hint row and no controls box
        if lay.stats is None:
            head += (f"| score {game.score} | lives {game.lives} | lvl {game.level}"
                     f" | [{time_bar(game, 10)}] ")
        safe_addstr(scr, 0, 0, head[: lay.cols - 1], curses.A_STANDOUT)

    lay.boxes()
    fw = lay.field
    draw_field(fw, game)
    if game.game_over:
        _overlay(fw, [(" GAME OVER ", curses.A_STANDOUT | curses.A_BOLD),
                      (" r restart | q menu ", curses.A_STANDOUT)])
    elif game.paused and not resume:
        _overlay(fw, [(" PAUSED ", curses.A_STANDOUT | curses.A_BOLD),
                      (" p resume | q menu ", curses.A_STANDOUT)])
    elif game.phase is not None and game.phase.kind == "cleared":
        _overlay(fw, [(f" LEVEL {game.level} CLEAR ", curses.A_STANDOUT | curses.A_BOLD)])

    if lay.stats is not None:
        sw = lay.stats
        w = sw.getmaxyx()[1] - 2
        safe_addstr(sw, 0, 2, f" {TITLE} "[: max(0, w - 1)], curses.A_BOLD)
        lines = [
            f"score {game.score}",
            f"best  {max(best, game.score)}",
            f"level {game.level}",
            f"lives {'@@ ' * game.lives}".rstrip(),
            f"homes {sum(game.homes)}/{len(BAY_X)}",
            f"time  {int(math.ceil(game.time_left))}s",
        ]
        for i, line in enumerate(lines):
            safe_addstr(sw, 1 + i, 1, line[:w])
        bar = "[" + time_bar(game, max(1, w - 2)) + "]"
        attr = curses.color_pair(PAIR_ALERT if game.time_left < 8 else PAIR_FROG)
        safe_addstr(sw, 7, 1, bar[:w], attr)
    if lay.controls is not None:
        draw_controls(lay.controls, CONTROLS)
    if lay.agents is not None:
        draw_agents(lay.agents, session.roster, session.status_pairs)
    if lay.plan["hint"] is not None:
        y, x, _, w = lay.plan["hint"]
        safe_addstr(scr, y, x, hint_text(w), curses.A_DIM)
    if help is not None:
        help.draw(scr)


KEYS = {
    curses.KEY_LEFT: (-1, 0), ord("h"): (-1, 0), ord("a"): (-1, 0),
    curses.KEY_RIGHT: (1, 0), ord("l"): (1, 0), ord("d"): (1, 0),
    curses.KEY_UP: (0, -1), ord("k"): (0, -1), ord("w"): (0, -1),
    curses.KEY_DOWN: (0, 1), ord("j"): (0, 1), ord("s"): (0, 1),
}


def handle_key(game, ch) -> bool:
    """Apply a key to the game. Returns True if a redraw is needed."""
    if game.game_over:
        return False
    if ch in (ord("p"), ord("P")):
        game.paused = not game.paused
        return True
    if game.paused:
        return False
    if 0 <= ch < 256 and chr(ch).isupper():
        ch = ord(chr(ch).lower())
    move = KEYS.get(ch)
    if move is None:
        return False
    return game.hop(*move)


class LoopState:
    """The run loop's pause bookkeeping: which pause came from what, so each
    source lifts only its own."""

    def __init__(self, resume_banner=False):
        self.resume_banner = resume_banner
        self.paused_by_resume = resume_banner
        self.paused_by_attention = False
        self.paused_by_help = False


def dispatch_key(ch, game, help, st, session):
    """One key from the run loop (KEY_RESIZE is handled before this).
    Returns 'quit', 'restart', or whether a redraw is needed."""
    if help.is_open:
        help.handle_key(ch)
        if not help.is_open and st.paused_by_help:
            st.paused_by_help = False
            if session.attention is not None or st.resume_banner:
                st.paused_by_attention = True  # the banner's key resumes
            else:
                game.paused = False
        return True
    if ch == HELP_KEY:
        help.open()
        if not game.paused and not game.game_over:
            game.paused = True
            st.paused_by_help = True
        return True
    if ch in (ord("q"), ord("Q")):
        return "quit"
    if session.attention is not None or st.resume_banner:
        session.dismiss()
        st.resume_banner = False
        if st.paused_by_attention or st.paused_by_resume:
            game.paused = False
        st.paused_by_attention = st.paused_by_resume = False
        return True
    if ch in (ord("r"), ord("R")):
        return "restart"
    return handle_key(game, ch)


def run(session) -> None:
    """Play Frogger until `q` (back to the arcade menu) or SIGTERM. The game
    is saved on every exit path and resumed, paused, on the next run."""
    stdscr = session.stdscr
    init_colors()
    game_path = session.path("frogger-game.json")
    hiscore_path = session.path("frogger-hiscore.json")
    hiscore = load_hiscore(hiscore_path)

    game = Game()
    st = LoopState()
    if game_path:
        restored = Game.from_save(load_json_file(game_path))
        if restored is not None and not restored.game_over:
            game = restored
            game.paused = True
            st = LoopState(resume_banner=True)

    help = HelpOverlay(TITLE, HELP)
    lay = Layout(stdscr)
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
                if not game.paused:
                    st.paused_by_attention = True
                game.paused = True
            dirty = dirty or changed

            ch = stdscr.getch()
            if ch != -1:
                if ch == curses.KEY_RESIZE:
                    curses.update_lines_cols()
                    stdscr.erase()
                    lay = Layout(stdscr)
                    dirty = True
                else:
                    result = dispatch_key(ch, game, help, st, session)
                    if result == "quit":
                        break
                    if result == "restart":
                        if game.score > hiscore:
                            hiscore = game.score
                            save_hiscore(hiscore_path, hiscore)
                        if game_path:
                            remove_path(game_path)
                        game.reset()
                        st.paused_by_attention = st.paused_by_resume = False
                        game_over_handled = False
                        dirty = True
                    else:
                        dirty = result or dirty

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
                draw(game, lay, session, st.resume_banner, hiscore, help)
                stdscr.noutrefresh()
                curses.doupdate()
                dirty = False

            time.sleep(0.01)
    finally:
        # The one save point: resolve a pending death/level-clear first so
        # the saved state is the one the animation would have led to.
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

    sys.exit(arcade.main(["--game", "frogger"] + sys.argv[1:]))
