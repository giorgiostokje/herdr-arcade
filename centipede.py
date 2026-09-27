#!/usr/bin/env python3
"""Centipede for the Herdr arcade (see arcade.py, which runs it via `run()`).

Structure: everything above the "curses UI" divider is pure game logic --
mushroom field, centipedes, spider, flea, shot, scoring, lives, waves,
save/restore and the layout plan -- with no terminal dependency, so
`tests/test_centipede.py` imports this module without a terminal.
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

FIELD_W = 20                 # cells (each drawn two columns wide)
FIELD_H = 21                 # rows
ZONE_H = 5                   # the player's zone: the bottom five rows
ZONE_TOP = FIELD_H - ZONE_H  # first row of the player's zone
PLAYER_START = (FIELD_W // 2, FIELD_H - 1)

SEGMENTS = 12                # centipede length per wave
MUSHROOM_COUNT = 34          # random field at game start
MUSHROOM_HP = 4
START_LIVES = 3

HEAD_POINTS = 100
BODY_POINTS = 10
MUSHROOM_POINTS = 1          # destroying one
RESTORE_POINTS = 5           # per damaged mushroom restored after a death
FLEA_POINTS = 200
SPIDER_BANDS = ((2, 900), (4, 600))  # (max Chebyshev distance, points)
SPIDER_FAR_POINTS = 300

SHOT_INTERVAL = 1.0 / 60     # seconds per cell the shot climbs
SPIDER_INTERVAL = 0.14
SPIDER_DELAY = (3.0, 7.0)    # seconds between spiders (uniform range)
SPIDER_MOVE_X = 0.5          # chance a spider step also moves sideways
FLEA_INTERVAL = 0.06
FLEA_DELAY = (1.5, 4.0)
FLEA_THRESHOLD = 5           # fewer zone mushrooms than this summons fleas
FLEA_DROP_CHANCE = 0.3       # per cell the flea falls through

DEATH_TIME = 1.0
WAVE_CLEAR_TIME = 1.0
MAX_STEP = 1.0 / 60          # update() integrates in slices no longer than this

SAVE_VERSION = 1


def centipede_interval(wave: int) -> float:
    """Seconds per centipede step: faster each wave, 0.04s floor."""
    return max(0.04, 0.11 * 0.88 ** (wave - 1))


def in_field(x: int, y: int) -> bool:
    return 0 <= x < FIELD_W and 0 <= y < FIELD_H


def spider_points(spider, player) -> int:
    """300/600/900 by how close the spider was to the player (Chebyshev)."""
    d = max(abs(spider[0] - player[0]), abs(spider[1] - player[1]))
    for limit, points in SPIDER_BANDS:
        if d <= limit:
            return points
    return SPIDER_FAR_POINTS


class Phase:
    """A timed interlude as an explicit logic phase (like Tetris' Clearing):
    'dying' or 'wave_clear'. Its consequences apply when it completes --
    via update(), or immediately via finish_phase()."""

    __slots__ = ("kind", "elapsed", "duration")

    def __init__(self, kind, duration):
        self.kind = kind
        self.elapsed = 0.0
        self.duration = duration


def spawn_centipede(n: int) -> list:
    """A centipede of `n` segments on the top row, head first, heading
    right. Each segment is [x, y, dx, dy]: position plus the direction it
    travels next (copied along the body, so a split's rear knows its way)."""
    n = max(1, min(n, FIELD_W))
    head_x = n - 1
    return [[head_x - i, 0, 1, 1] for i in range(n)]


def step_centipede(segs: list, mushrooms: dict) -> list:
    """One step: the head moves one cell sideways; blocked by a wall or a
    mushroom it moves one row vertically instead (ignoring mushrooms) and
    reverses. At the bottom it turns back up and then roams inside the
    player's zone. The body follows the leader."""
    x, y, dx, dy = segs[0]
    nx = x + dx
    if 0 <= nx < FIELD_W and (nx, y) not in mushrooms:
        head = [nx, y, dx, dy]
    else:
        ny = y + dy
        if ny >= FIELD_H:
            dy = -1
            ny = y - 1
        elif dy < 0 and ny < ZONE_TOP:
            dy = 1
            ny = y + 1
        head = [x, ny, -dx, dy]
    return [head] + [s[:] for s in segs[:-1]]


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

    def __init__(self, rng: random.Random | None = None, seed_field: bool = True):
        self.rng = rng if rng is not None else random.Random()
        self.score = 0
        self.lives = START_LIVES
        self.wave = 1
        self.paused = False
        self.game_over = False
        self.phase: Phase | None = None
        self.mushrooms: dict = {}
        if seed_field:
            self._seed_mushrooms()
        self.centipedes = [spawn_centipede(SEGMENTS)]
        self.cent_timer = 0.0
        self._reset_round()

    def reset(self) -> None:
        self.__init__(self.rng)

    def _seed_mushrooms(self) -> None:
        cells = [(x, y) for y in range(1, FIELD_H - 1) for x in range(FIELD_W)
                 if (x, y) != PLAYER_START]
        for pos in self.rng.sample(cells, MUSHROOM_COUNT):
            self.mushrooms[pos] = MUSHROOM_HP

    def _reset_round(self) -> None:
        """Player back to start; shot, spider and flea cleared."""
        self.px, self.py = PLAYER_START
        self.mushrooms.pop(PLAYER_START, None)
        self.bullet = None           # [x, y] or None
        self.shot_timer = 0.0
        self.spider = None           # [x, y, dx, dy] or None
        self.spider_timer = 0.0
        self.spider_wait = self.rng.uniform(*SPIDER_DELAY)
        self.flea = None             # [x, y] or None
        self.flea_timer = 0.0
        self.flea_wait = self.rng.uniform(*FLEA_DELAY)

    # -- queries -------------------------------------------------------------

    def segment_cells(self) -> set:
        return {(s[0], s[1]) for c in self.centipedes for s in c}

    def zone_mushrooms(self) -> int:
        return sum(1 for (_, y) in self.mushrooms if y >= ZONE_TOP)

    def _active(self) -> bool:
        return not (self.paused or self.game_over or self.phase is not None)

    # -- player --------------------------------------------------------------

    def move(self, dx: int, dy: int) -> bool:
        """One cell per keypress, inside the zone, blocked by mushrooms."""
        if not self._active():
            return False
        nx, ny = self.px + dx, self.py + dy
        if not (0 <= nx < FIELD_W and ZONE_TOP <= ny < FIELD_H):
            return False
        if (nx, ny) in self.mushrooms:
            return False
        self.px, self.py = nx, ny
        self._check_player()
        return True

    def fire(self) -> bool:
        """Launch the one shot; it immediately enters the cell above."""
        if not self._active() or self.bullet is not None:
            return False
        self.bullet = [self.px, self.py]
        self.shot_timer = 0.0
        self._advance_bullet()
        return True

    def _advance_bullet(self) -> None:
        """Move the shot one cell up and resolve anything it enters."""
        bx, by = self.bullet[0], self.bullet[1] - 1
        if by < 0:
            self.bullet = None
            return
        self.bullet = [bx, by]
        if self._hit_at(bx, by):
            self.bullet = None

    def _check_bullet(self) -> None:
        """After an enemy step: something may have moved onto the shot."""
        if self.bullet is not None and self._hit_at(*self.bullet):
            self.bullet = None

    # -- hits ----------------------------------------------------------------

    def _hit_at(self, x: int, y: int) -> bool:
        """Resolve a shot at (x, y): flea, spider, segment, mushroom (in that
        order). True if something was hit (the shot is spent)."""
        if self.flea is not None and (self.flea[0], self.flea[1]) == (x, y):
            self.flea = None
            self.flea_timer = 0.0
            self.flea_wait = self.rng.uniform(*FLEA_DELAY)
            self.score += FLEA_POINTS
            return True
        if self.spider is not None and (self.spider[0], self.spider[1]) == (x, y):
            self.score += spider_points((x, y), (self.px, self.py))
            self.spider = None
            self.spider_timer = 0.0
            self.spider_wait = self.rng.uniform(*SPIDER_DELAY)
            return True
        for ci, segs in enumerate(self.centipedes):
            for k, s in enumerate(segs):
                if (s[0], s[1]) == (x, y):
                    self._split(ci, k)
                    return True
        if (x, y) in self.mushrooms:
            self.mushrooms[(x, y)] -= 1
            if self.mushrooms[(x, y)] <= 0:
                del self.mushrooms[(x, y)]
                self.score += MUSHROOM_POINTS
            return True
        return False

    def _split(self, ci: int, k: int) -> None:
        """Segment k of centipede ci is shot: it becomes a mushroom; the part
        ahead keeps its head, the part behind becomes an independent
        centipede whose first segment is its new head."""
        segs = self.centipedes[ci]
        x, y = segs[k][0], segs[k][1]
        self.score += HEAD_POINTS if k == 0 else BODY_POINTS
        self.mushrooms[(x, y)] = MUSHROOM_HP
        front, rear = segs[:k], segs[k + 1:]
        parts = [p for p in (front, rear) if p]
        self.centipedes[ci:ci + 1] = parts

    # -- enemies -------------------------------------------------------------

    def _step_centipedes(self) -> None:
        self.centipedes = [step_centipede(c, self.mushrooms) for c in self.centipedes]

    def _spawn_spider(self) -> None:
        # Enter from either edge on any zone row -- but never on or next to
        # the player's cell, which would be an unavoidable death.
        spots = [(x, y) for x in (0, FIELD_W - 1)
                 for y in range(ZONE_TOP, FIELD_H)
                 if max(abs(x - self.px), abs(y - self.py)) > 1]
        x, y = self.rng.choice(spots)
        dx = 1 if x == 0 else -1
        self.spider = [x, y, dx, self.rng.choice((-1, 1))]
        self.mushrooms.pop((x, y), None)

    def _step_spider(self) -> None:
        x, y, dx, dy = self.spider
        if not ZONE_TOP <= y + dy < FIELD_H:
            dy = -dy
        y += dy
        if self.rng.random() < SPIDER_MOVE_X:
            x += dx
        if not 0 <= x < FIELD_W:
            self.spider = None  # wandered off the far side
            self.spider_timer = 0.0
            self.spider_wait = self.rng.uniform(*SPIDER_DELAY)
            return
        self.spider = [x, y, dx, dy]
        self.mushrooms.pop((x, y), None)  # spiders eat mushrooms

    def _spawn_flea(self, x: int | None = None) -> None:
        if x is None:
            x = self.rng.randrange(FIELD_W)
        self.flea = [x, 0]

    def _step_flea(self) -> None:
        x, y = self.flea
        # Fleas leave mushrooms behind in the cells they fall through.
        if (1 <= y < FIELD_H - 1 and (x, y) not in self.mushrooms
                and (x, y) != (self.px, self.py)
                and self.rng.random() < FLEA_DROP_CHANCE):
            self.mushrooms[(x, y)] = MUSHROOM_HP
        y += 1
        if y >= FIELD_H:
            self.flea = None
            self.flea_timer = 0.0
            self.flea_wait = self.rng.uniform(*FLEA_DELAY)
            return
        self.flea = [x, y]

    # -- death / waves ---------------------------------------------------------

    def _check_player(self) -> None:
        p = (self.px, self.py)
        hit = p in self.segment_cells()
        if self.spider is not None and (self.spider[0], self.spider[1]) == p:
            hit = True
        if self.flea is not None and (self.flea[0], self.flea[1]) == p:
            hit = True
        if hit and self.phase is None:
            self.phase = Phase("dying", DEATH_TIME)
            self.bullet = None

    def finish_phase(self) -> None:
        """Resolve the pending phase immediately (save, tests). The state
        afterwards is exactly as if it had run to completion."""
        phase = self.phase
        if phase is None:
            return
        self.phase = None
        if phase.kind == "dying":
            self.lives -= 1
            if self.lives <= 0:
                self.lives = 0
                self.game_over = True
                return
            # Damaged mushrooms grow back, as in the original.
            for pos, hp in list(self.mushrooms.items()):
                if hp < MUSHROOM_HP:
                    self.mushrooms[pos] = MUSHROOM_HP
                    self.score += RESTORE_POINTS
            remaining = sum(len(c) for c in self.centipedes)
            self.centipedes = [spawn_centipede(remaining or SEGMENTS)]
            self.cent_timer = 0.0
            self._reset_round()
        elif phase.kind == "wave_clear":
            self.wave += 1
            self.centipedes = [spawn_centipede(SEGMENTS)]
            self.cent_timer = 0.0

    def resolve_phases(self) -> None:
        while self.phase is not None:
            self.finish_phase()

    # -- time ------------------------------------------------------------------

    def _tick(self, step: float) -> bool:
        changed = False
        if self.bullet is not None:
            self.shot_timer += step
            while self.bullet is not None and self.shot_timer >= SHOT_INTERVAL:
                self.shot_timer -= SHOT_INTERVAL
                self._advance_bullet()
                changed = True

        self.cent_timer += step
        interval = centipede_interval(self.wave)
        while self.cent_timer >= interval and self.centipedes:
            self.cent_timer -= interval
            self._step_centipedes()
            self._check_bullet()
            changed = True

        if self.spider is None:
            self.spider_timer += step
            if self.spider_timer >= self.spider_wait:
                self.spider_timer = 0.0
                self._spawn_spider()
                changed = True
        else:
            self.spider_timer += step
            while self.spider is not None and self.spider_timer >= SPIDER_INTERVAL:
                self.spider_timer -= SPIDER_INTERVAL
                self._step_spider()
                self._check_bullet()
                changed = True

        if self.flea is None:
            if self.zone_mushrooms() < FLEA_THRESHOLD:
                self.flea_timer += step
                if self.flea_timer >= self.flea_wait:
                    self.flea_timer = 0.0
                    self._spawn_flea()
                    changed = True
        else:
            self.flea_timer += step
            while self.flea is not None and self.flea_timer >= FLEA_INTERVAL:
                self.flea_timer -= FLEA_INTERVAL
                self._step_flea()
                self._check_bullet()
                changed = True

        self._check_player()
        if self.phase is None and not self.centipedes:
            self.phase = Phase("wave_clear", WAVE_CLEAR_TIME)
            changed = True
        return changed or self.phase is not None

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

    # -- save / restore ----------------------------------------------------------

    def to_save(self) -> dict:
        """Serializable snapshot. Pending phases are resolved first, so a save
        taken mid-death or mid-wave-transition resumes consistently."""
        self.resolve_phases()
        return {
            "version": SAVE_VERSION,
            "score": self.score,
            "lives": self.lives,
            "wave": self.wave,
            "game_over": self.game_over,
            "mushrooms": sorted([x, y, hp] for (x, y), hp in self.mushrooms.items()),
            "centipedes": [[s[:] for s in c] for c in self.centipedes],
            "cent_timer": self.cent_timer,
            "player": [self.px, self.py],
            "bullet": None if self.bullet is None else self.bullet[:],
            "shot_timer": self.shot_timer,
            "spider": None if self.spider is None else self.spider[:],
            "spider_timer": self.spider_timer,
            "spider_wait": self.spider_wait,
            "flea": None if self.flea is None else self.flea[:],
            "flea_timer": self.flea_timer,
            "flea_wait": self.flea_wait,
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

            def integer(v, lo=0, hi=None):
                if isinstance(v, bool) or not isinstance(v, int) or v < lo:
                    raise ValueError(v)
                if hi is not None and v > hi:
                    raise ValueError(v)
                return v

            def unit(v):
                if integer(v, -1, 1) == 0:
                    raise ValueError(v)
                return v

            def cell(v, n):
                if not isinstance(v, list) or len(v) != n:
                    raise ValueError(v)
                x, y = integer(v[0]), integer(v[1])
                if not in_field(x, y):
                    raise ValueError(v)
                return [x, y] + [unit(d) for d in v[2:]]

            game = cls(rng, seed_field=False)
            game.score = integer(data["score"])
            game.lives = integer(data["lives"])
            game.wave = integer(data["wave"], 1)
            game.game_over = bool(data["game_over"])
            if not game.game_over and game.lives < 1:
                return None
            mushrooms = {}
            for item in data["mushrooms"]:
                if not isinstance(item, list) or len(item) != 3:
                    return None
                x, y = cell(item[:2], 2)
                mushrooms[(x, y)] = integer(item[2], 1, MUSHROOM_HP)
            game.mushrooms = mushrooms
            cents = data["centipedes"]
            if not isinstance(cents, list):
                return None
            game.centipedes = []
            for c in cents:
                if not isinstance(c, list) or not c:
                    return None
                game.centipedes.append([cell(s, 4) for s in c])
            if not game.centipedes and not game.game_over:
                return None
            game.cent_timer = num(data["cent_timer"])
            px, py = cell(data["player"], 2)
            if py < ZONE_TOP or (px, py) in mushrooms:
                return None
            game.px, game.py = px, py
            game.bullet = None if data["bullet"] is None else cell(data["bullet"], 2)
            game.shot_timer = num(data["shot_timer"])
            game.spider = None if data["spider"] is None else cell(data["spider"], 4)
            if game.spider is not None and game.spider[1] < ZONE_TOP:
                return None
            game.spider_timer = num(data["spider_timer"])
            game.spider_wait = num(data["spider_wait"])
            game.flea = None if data["flea"] is None else cell(data["flea"], 2)
            game.flea_timer = num(data["flea_timer"])
            game.flea_wait = num(data["flea_wait"])
            # The RNG drives spiders and fleas: restoring it makes a resumed
            # game continue exactly as the saved one would have.
            state = _rng_from_json(data["rng"])
            if state is None:
                return None
            game.rng.setstate(state)
            return game
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            return None


# --------------------------------------------------------------------------
# Controls and help text (shown in the side column and the `?` overlay)
# --------------------------------------------------------------------------

TITLE = "Centipede"

CONTROLS = [
    ("arrows", "move one cell"),
    ("wasd hjkl", "move one cell"),
    ("space", "fire"),
    ("?", "help"),
    ("p", "pause / resume"),
    ("r", "restart"),
    ("q", "menu (saves game)"),
]

HELP = [
    ("Goal",
     "Shoot the centipede apart before it reaches you. Destroy every "
     "segment to clear the wave; the next centipede comes faster. Dodge the "
     "spider and the flea, and score as high as you can with 3 lives."),
    ("Controls",
     "- Arrows, w a s d or h j k l move your shooter (/\\) one cell per "
     "press - up and down as well as sideways, but only inside the player "
     "zone. Mushrooms block your way.\n"
     "- space fires straight up. Only one shot can be on screen: you can "
     "fire again once it hits something or leaves the top.\n"
     "- p pauses and resumes, r abandons this game and starts a new one, "
     "q goes back to the arcade menu. Quitting saves the game; it comes "
     "back paused next time.\n"
     "- ? opens this help and pauses the game; any other key closes it.\n"
     "- When one of your agents needs attention the game pauses and a "
     "banner appears; any key dismisses it and resumes."),
    ("How it works",
     "- A 12-segment centipede (red @@ head, yellow oo body) enters on the "
     "top row heading right.\n"
     "- It walks sideways. When it runs into a wall or a mushroom it drops "
     "one row and turns around, so a crowded field brings it down faster.\n"
     "- Touching any segment costs a life."),
    ("The player zone",
     "The bottom 5 rows, marked with blue dots. You can never leave it. "
     "The spider lives here, and a centipede that reaches the bottom turns "
     "around and keeps roaming inside the zone instead of leaving."),
    ("Splitting",
     "- Every segment you hit turns into a fresh mushroom where it was.\n"
     "- Hit the head: the next segment becomes the new head.\n"
     "- Hit the tail: the centipede just gets shorter.\n"
     "- Hit the middle: it splits in two. The front part keeps its head, "
     "and the part behind becomes a separate centipede with its own new "
     "head, carrying on in the same direction. The rear part usually runs "
     "straight into the mushroom the hit left behind, so it drops a row "
     "and turns around.\n"
     "Split a long centipede and you have several heads coming at you."),
    ("Mushrooms",
     "Scattered over the field at the start. Each takes 4 shots to destroy "
     "(## #= == -. as it wears down) and blocks both you and the "
     "centipede's sideways walk. They stay from wave to wave."),
    ("Spider (XX)",
     "Appears every few seconds from the left or right edge on a row of "
     "the player zone. It zigzags up and down inside the zone while "
     "drifting across it, eats any mushroom it passes over, and leaves off "
     "the far side. Touching it costs a life. The closer it is to you when "
     "you shoot it, the more it is worth."),
    ("Flea (VV)",
     "Only comes when fewer than 5 mushrooms are left in the player zone. "
     "It drops straight down from the top, fast, and plants new mushrooms "
     "in some of the cells it falls through. Touching it costs a life."),
    ("Lives and waves",
     "- You start with 3 lives. After a death, damaged mushrooms grow back "
     "to full, you restart at the bottom centre and the surviving segments "
     "regroup into one centipede at the top. Losing the last life ends the game.\n"
     "- Destroy every segment to clear the wave: a new 12-segment "
     "centipede enters, a little faster each wave."),
    ("Scoring",
     "- Centipede head: 100. Body segment: 10.\n"
     "- Spider: 900 within 2 cells of you, 600 within 4, 300 farther.\n"
     "- Flea: 200.\n"
     "- Mushroom destroyed: 1. Each damaged mushroom restored after a "
     "death: 5.\n"
     "Your best score is kept."),
    ("Tips",
     "- Shoot heads when you can: 100 points, and the centipede does not "
     "split into more heads.\n"
     "- Keep a few mushrooms in your zone if you do not want fleas.\n"
     "- Stay low and shoot the spider up close for 900.\n"
     "- Clear mushrooms near your zone so centipedes cannot drop onto you "
     "quickly."),
]


def key_hint(width: int) -> str:
    """One-line key hint for tiers without a controls box; always keeps
    '? help' even when the full list does not fit."""
    full = controls_hint(CONTROLS)
    if len(full) <= width:
        return full
    rest = [c for c in CONTROLS if c[0] != "?"]
    return ("? help | " + controls_hint(rest))[: max(0, width)]


# Adaptive layout tiers. Row 0 is the header/banner; the field window (20
# cells x 2 cols + border = 42 x 23) and the side column (stats, controls,
# agents; common.side_width wide) are centred together below it. At the
# 93x28 popup the composition is 42 + 1 + 30 = 73 x 23.
FIELD_WIN_W = FIELD_W * 2 + 2
FIELD_WIN_H = FIELD_H + 2
MIN_COLS = FIELD_WIN_W
MIN_ROWS = FIELD_WIN_H + 1
STATS_H = 7                  # frame + score/best/wave/lives/state
BOX_MIN_H = 3                # a framed box with one line inside


def layout_plan(cols: int, rows: int) -> dict:
    """Pure layout decision: tier plus each region's (y, x, height, width).
    Tiers: 'full' (field + stats/controls/agents side column), 'side' (side
    column without agents), 'field' (score and a key hint instead of the
    side column), 'too_small'. `hint` is the key-hint row in the 'field'
    tier (row 0 means it shares the header)."""
    plan = {"tier": "too_small", "too_small": True, "field": None,
            "stats": None, "controls": None, "agents": None, "hint": None}
    if cols < MIN_COLS or rows < MIN_ROWS:
        return plan
    plan["too_small"] = False
    side = side_width(cols, FIELD_WIN_W)
    if not side:
        plan["tier"] = "field"
        if rows >= MIN_ROWS + 1:
            y, x = center_origin(cols, rows - 1, FIELD_WIN_W, FIELD_WIN_H)
            plan["hint"] = (rows - 1, 0, 1, cols)
        else:
            y, x = center_origin(cols, rows, FIELD_WIN_W, FIELD_WIN_H)
            plan["hint"] = (0, 0, 1, cols)
        plan["field"] = (y, x, FIELD_WIN_H, FIELD_WIN_W)
        return plan
    y, x = center_origin(cols, rows, FIELD_WIN_W + 1 + side, FIELD_WIN_H)
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

# Colour pairs 40-59 are shared by the later games (only one runs at a time;
# each registers its own inside run()); Centipede uses 40-47.
PAIR_MUSHROOM, PAIR_BODY, PAIR_HEAD, PAIR_PLAYER = 40, 41, 42, 43
PAIR_SHOT, PAIR_SPIDER, PAIR_FLEA, PAIR_ZONE = 44, 45, 46, 47

MUSHROOM_GLYPHS = {4: "##", 3: "#=", 2: "==", 1: "-."}
HEAD_GLYPH = "@@"
BODY_GLYPH = "oo"
PLAYER_GLYPH = "/\\"
DEATH_GLYPHS = ("**", "\\/", "..", "  ")
SHOT_GLYPH = "| "
SPIDER_GLYPH = "XX"
FLEA_GLYPH = "VV"
ZONE_GLYPH = ". "

def init_colors() -> None:
    bg = default_bg()
    curses.init_pair(PAIR_MUSHROOM, curses.COLOR_GREEN, bg)
    curses.init_pair(PAIR_BODY, curses.COLOR_YELLOW, bg)
    curses.init_pair(PAIR_HEAD, curses.COLOR_RED, bg)
    curses.init_pair(PAIR_PLAYER, curses.COLOR_CYAN, bg)
    curses.init_pair(PAIR_SHOT, curses.COLOR_WHITE, bg)
    curses.init_pair(PAIR_SPIDER, curses.COLOR_MAGENTA, bg)
    curses.init_pair(PAIR_FLEA, curses.COLOR_YELLOW, bg)
    curses.init_pair(PAIR_ZONE, curses.COLOR_BLUE, bg)


class Layout:
    """Screen regions for the current size; rebuilt on resize."""

    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.rows, self.cols = stdscr.getmaxyx()
        self.plan = layout_plan(self.cols, self.rows)
        self.too_small = self.plan["too_small"]
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


def _put(win, x, y, text, attr=0):
    """One field cell (two columns) at field coordinates."""
    if in_field(x, y):
        safe_addstr(win, 1 + y, 1 + 2 * x, text, attr)


def _centered(win, row, text, attr):
    safe_addstr(win, row, max(1, (FIELD_WIN_W - len(text)) // 2), text, attr)


def draw_field(win, game: Game) -> None:
    zone_attr = curses.color_pair(PAIR_ZONE) | curses.A_DIM
    for y in range(ZONE_TOP, FIELD_H):
        for x in range(FIELD_W):
            _put(win, x, y, ZONE_GLYPH, zone_attr)
    m_attr = curses.color_pair(PAIR_MUSHROOM)
    for (x, y), hp in game.mushrooms.items():
        _put(win, x, y, MUSHROOM_GLYPHS[hp], m_attr | (curses.A_BOLD if hp == 4 else 0))
    for segs in game.centipedes:
        for k in range(len(segs) - 1, -1, -1):
            x, y = segs[k][0], segs[k][1]
            if k == 0:
                _put(win, x, y, HEAD_GLYPH, curses.color_pair(PAIR_HEAD) | curses.A_BOLD)
            else:
                _put(win, x, y, BODY_GLYPH, curses.color_pair(PAIR_BODY))
    if game.spider is not None:
        _put(win, game.spider[0], game.spider[1], SPIDER_GLYPH,
             curses.color_pair(PAIR_SPIDER) | curses.A_BOLD)
    if game.flea is not None:
        _put(win, game.flea[0], game.flea[1], FLEA_GLYPH,
             curses.color_pair(PAIR_FLEA) | curses.A_BOLD)
    if game.bullet is not None:
        _put(win, game.bullet[0], game.bullet[1], SHOT_GLYPH,
             curses.color_pair(PAIR_SHOT) | curses.A_BOLD)
    phase = game.phase
    p_attr = curses.color_pair(PAIR_PLAYER) | curses.A_BOLD
    if phase is not None and phase.kind == "dying":
        i = int(phase.elapsed / phase.duration * len(DEATH_GLYPHS))
        _put(win, game.px, game.py, DEATH_GLYPHS[min(i, len(DEATH_GLYPHS) - 1)], p_attr)
    elif not game.game_over:
        _put(win, game.px, game.py, PLAYER_GLYPH, p_attr)

    mid = 1 + ZONE_TOP // 2
    if game.game_over:
        _centered(win, mid - 1, " GAME OVER ", curses.A_STANDOUT | curses.A_BOLD)
        _centered(win, mid + 1, " r restart | q menu ", curses.A_STANDOUT)
    elif game.paused:
        _centered(win, mid, " PAUSED ", curses.A_STANDOUT | curses.A_BOLD)
    elif phase is not None and phase.kind == "wave_clear":
        _centered(win, mid, f" WAVE {game.wave + 1} ", curses.A_STANDOUT | curses.A_BOLD)


def draw_stats(win, game: Game, best: int) -> None:
    rows, cols = win.getmaxyx()
    inner = cols - 2
    safe_addstr(win, 0, 2, f" {TITLE.lower()} "[: max(0, cols - 3)], curses.A_BOLD)
    if game.game_over:
        state = "GAME OVER"
    elif game.paused:
        state = "paused"
    elif game.phase is not None and game.phase.kind == "dying":
        state = "ouch!"
    else:
        state = "playing"
    lines = [
        f"score {game.score}",
        f"best  {max(best, game.score)}",
        f"wave  {game.wave}",
        f"lives {game.lives}",
        state,
    ]
    for i, line in enumerate(lines[: max(0, rows - 2)]):
        attr = curses.A_BOLD if i == 0 else 0
        if i == 3:
            attr = curses.color_pair(PAIR_PLAYER)
        elif i == 4:
            attr = curses.A_DIM
        safe_addstr(win, 1 + i, 1, line[:inner], attr)


def draw(game: Game, lay: Layout, session, resume: bool, best: int,
         help_overlay=None) -> None:
    scr = lay.stdscr
    scr.erase()
    if lay.too_small:
        need = f"need {MIN_COLS}x{MIN_ROWS}, have {lay.cols}x{lay.rows}"
        lines = [f"pane too small: {need}", "resize the pane (? help, q menu)"]
        if len(lines[0]) >= lay.cols:  # narrow: one short phrase per row
            lines = ["pane too small", need.split(", ")[0], need.split(", ")[1],
                     "resize the pane", "(? help, q menu)"]
        top = max(0, (lay.rows - len(lines)) // 2)
        for i, line in enumerate(lines):
            line = line[: max(0, lay.cols - 1)]
            safe_addstr(scr, top + i, max(0, (lay.cols - len(line)) // 2), line,
                        curses.A_BOLD if i == 0 else 0)
        if help_overlay is not None:
            help_overlay.draw(scr)
        return

    hint_in_header = lay.hint is not None and lay.hint[0] == 0
    stand = curses.A_STANDOUT | (curses.A_BOLD if game.game_over else 0)
    if not draw_banner(scr, lay.cols, session.attention, resume):
        if hint_in_header:
            # Tight 'field' tier: compact stats, then the key hint.
            head = f" {game.score} pts | {game.lives} lives | wave {game.wave} "
            safe_addstr(scr, 0, 0, head[: lay.cols - 1], stand)
            room = lay.cols - 1 - len(head) - 1
            if room > 0:
                safe_addstr(scr, 0, len(head) + 1, key_hint(room), curses.A_DIM)
        else:
            if game.game_over:
                head = f" {TITLE.lower()} - GAME OVER "
            else:
                head = f" {TITLE.lower()} - {'paused' if game.paused else 'playing'} "
            if lay.stats is None:  # no stats box in this tier: score goes here
                head += f"score {game.score} | lives {game.lives} | wave {game.wave} "
            safe_addstr(scr, 0, 0, head[: lay.cols - 1], stand)

    lay.boxes()
    draw_field(lay.field, game)
    if lay.hint is not None and not hint_in_header:
        y, x, _, w = lay.hint
        text = key_hint(w - 1)
        safe_addstr(scr, y, x + max(0, (w - 1 - len(text)) // 2), text, curses.A_DIM)
    if lay.stats is not None:
        draw_stats(lay.stats, game, best)
    if lay.controls is not None:
        draw_controls(lay.controls, CONTROLS)
    if lay.agents is not None:
        draw_agents(lay.agents, session.roster, session.status_pairs)
    if help_overlay is not None:
        help_overlay.draw(scr)


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


KEY_MOVES = {
    curses.KEY_UP: (0, -1), curses.KEY_DOWN: (0, 1),
    curses.KEY_LEFT: (-1, 0), curses.KEY_RIGHT: (1, 0),
}
for _keys, _d in (("kKwW", (0, -1)), ("jJsS", (0, 1)),
                  ("hHaA", (-1, 0)), ("lLdD", (1, 0))):
    for _k in _keys:
        KEY_MOVES[ord(_k)] = _d


def run(session) -> None:
    scr = session.stdscr
    init_colors()
    game_path = session.path("centipede-game.json")
    hiscore_path = session.path("centipede-hiscore.json")
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
    help_overlay = HelpOverlay(TITLE, HELP)
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
                    # The banner now owns the pause: closing help keeps it.
                    paused_by_help = False
                    paused_by_attention = True
                dirty = True

            quit_now = False
            while True:
                ch = scr.getch()
                if ch == -1:
                    break
                dirty = True
                consumed, paused_by_help = help_key(
                    help_overlay, game, ch, paused_by_help)
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
                elif ch == ord(" "):
                    game.fire()
                elif ch in KEY_MOVES:
                    game.move(*KEY_MOVES[ch])
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
                draw(game, lay, session, resume, hiscore, help_overlay)
                scr.noutrefresh()
                curses.doupdate()
                dirty = False

            time.sleep(0.01)
    finally:
        # The one save point: q, SIGTERM, or an exception lands here. A
        # pending death/wave transition is resolved first so the save is a
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

    sys.exit(arcade.main(["--game", "centipede"] + sys.argv[1:]))
