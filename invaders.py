#!/usr/bin/env python3
"""Space Invaders for the Herdr arcade (see arcade.py, which runs it via
`run()`).

Structure: everything above the "curses UI" divider is pure game logic —
formation, cannon, shots, bombs, shields, UFO, scoring, waves, save/restore
and the layout plan — with no terminal dependency, so
`tests/test_invaders.py` imports this module without a terminal. Agent
awareness, state files and the shared curses helpers live in common.py.

Coordinates are terminal cells of the field interior: x grows right
(0..FIELD_W-1), y grows down (0..FIELD_H-1). Invaders, the cannon and the
UFO are multi-column sprites anchored at their leftmost cell.
"""

from __future__ import annotations

import curses
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

FIELD_W = 40
FIELD_H = 20

UFO_Y = 0
CANNON_Y = FIELD_H - 1
CANNON_W = 3
CANNON_STEP = 2          # columns per keypress; key auto-repeat drives motion

FORM_ROWS = 5
FORM_COLS = 8
SPRITE_W = 3
COL_PITCH = 4            # sprite + one blank column
ROW_PITCH = 2            # sprite row + one blank row
FORMATION_W = (FORM_COLS - 1) * COL_PITCH + SPRITE_W  # 31
FORMATION_TOP = 2        # wave 1 top row; later waves start lower
MAX_START_DROP = 3

# Invader type per formation row: row 0 squids, rows 1-2 crabs, 3-4 octopi.
ROW_KINDS = ("squid", "crab", "crab", "octopus", "octopus")
KIND_POINTS = {"squid": 30, "crab": 20, "octopus": 10}

SHIELD_Y = 15            # top row of the bunkers
SHIELD_SHAPE = (
    " #### ",
    "######",
    "##  ##",
)
SHIELD_W = len(SHIELD_SHAPE[0])
SHIELD_COUNT = 4

SHOT_SPEED = 30.0        # rows/second
BOMB_BASE_SPEED = 8.0
BOMB_MAX_SPEED = 14.0

MARCH_BASE = 0.6         # seconds per step with a full wave-1 formation
MARCH_MIN = 0.04
MARCH_WAVE_FACTOR = 0.85

UFO_W = 5
UFO_SPEED = 8.0          # columns/second
UFO_SCORES = (50, 100, 150, 300)
UFO_MIN_DELAY, UFO_MAX_DELAY = 15.0, 28.0

START_LIVES = 3
MAX_LIVES = 9
EXTRA_LIFE_SCORE = 1500

DEATH_DURATION = 1.0
WAVE_DURATION = 1.5
EXPLOSION_DURATION = 0.2
UFO_POPUP_DURATION = 1.0

# Save format version: a mismatch (either direction) means "ignore silently".
SAVE_VERSION = 1

PLAY, DYING, WAVE = "play", "dying", "wave"


def march_interval(alive: int, wave: int, total: int = FORM_ROWS * FORM_COLS) -> float:
    """Seconds per formation step: faster as invaders die and per wave."""
    base = MARCH_BASE * (MARCH_WAVE_FACTOR ** (max(1, wave) - 1))
    frac = max(0, alive) / float(total)
    return max(MARCH_MIN, MARCH_MIN + (base - MARCH_MIN) * frac)


def bomb_speed(wave: int) -> float:
    return min(BOMB_MAX_SPEED, BOMB_BASE_SPEED + 0.5 * (wave - 1))


def bomb_cap(wave: int) -> int:
    return min(5, 3 + (wave - 1) // 2)


def bomb_delay_base(wave: int) -> float:
    return max(0.3, 1.2 - 0.1 * (wave - 1))


def shield_cells() -> set:
    """Fresh bunkers: four evenly spaced copies of SHIELD_SHAPE."""
    cells = set()
    for i in range(SHIELD_COUNT):
        center = FIELD_W * (2 * i + 1) // (2 * SHIELD_COUNT)
        x0 = center - SHIELD_W // 2
        for dy, line in enumerate(SHIELD_SHAPE):
            for dx, ch in enumerate(line):
                if ch == "#":
                    cells.add((x0 + dx, SHIELD_Y + dy))
    return cells


class Formation:
    """The invader grid: an origin (x, y), a march direction and an alive
    mask. Invader (r, c) sits at (x + c*COL_PITCH, y + r*ROW_PITCH)."""

    def __init__(self, x: int, y: int, direction: int = 1):
        self.x = x
        self.y = y
        self.dir = direction
        self.frame = 0
        self.alive = [[True] * FORM_COLS for _ in range(FORM_ROWS)]

    def count(self) -> int:
        return sum(sum(1 for a in row if a) for row in self.alive)

    def pos(self, r: int, c: int):
        return self.x + c * COL_PITCH, self.y + r * ROW_PITCH

    def members(self):
        """(row, col, x, y) of every live invader."""
        for r in range(FORM_ROWS):
            for c in range(FORM_COLS):
                if self.alive[r][c]:
                    x, y = self.pos(r, c)
                    yield r, c, x, y

    def bounds(self):
        """(left_x, right_x, top_y, bottom_y) over live invaders, inclusive
        cell coordinates; None when the formation is empty."""
        cols = [c for c in range(FORM_COLS) if any(self.alive[r][c] for r in range(FORM_ROWS))]
        rows = [r for r in range(FORM_ROWS) if any(self.alive[r])]
        if not cols:
            return None
        left = self.x + min(cols) * COL_PITCH
        right = self.x + max(cols) * COL_PITCH + SPRITE_W - 1
        return left, right, self.y + min(rows) * ROW_PITCH, self.y + max(rows) * ROW_PITCH

    def at(self, x: int, y: int):
        """(row, col) of the live invader covering cell (x, y), or None."""
        dy = y - self.y
        dx = x - self.x
        if dy < 0 or dx < 0 or dy % ROW_PITCH or dx % COL_PITCH >= SPRITE_W:
            return None
        r, c = dy // ROW_PITCH, dx // COL_PITCH
        if r < FORM_ROWS and c < FORM_COLS and self.alive[r][c]:
            return r, c
        return None

    def bottom_of_column(self, c: int):
        for r in range(FORM_ROWS - 1, -1, -1):
            if self.alive[r][c]:
                return r
        return None


class Projectile:
    """A shot or bomb: integer cell plus a fractional progress accumulator,
    advanced one row at a time so nothing tunnels through a 1-row target."""

    __slots__ = ("x", "y", "acc")

    def __init__(self, x: int, y: int, acc: float = 0.0):
        self.x = x
        self.y = y
        self.acc = acc


class Ufo:
    __slots__ = ("x", "dir")

    def __init__(self, x: float, direction: int):
        self.x = x
        self.dir = direction

    def span(self):
        left = int(self.x)
        return left, left + UFO_W - 1


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


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class Game:
    """Full game state and every state transition. No I/O."""

    def __init__(self, rng: random.Random | None = None):
        self.rng = rng if rng is not None else random.Random()
        self.score = 0
        self.lives = START_LIVES
        self.wave = 1
        self.extra_life_given = False
        self.paused = False
        self.game_over = False
        self.invaded = False
        self.explosions: list = []   # cosmetic [x, y, time_left]; never saved
        self.ufo_popup = None        # cosmetic [x, points, time_left]
        self._start_wave()

    def reset(self) -> None:
        self.__init__(self.rng)

    def _start_wave(self) -> None:
        drop = min(self.wave - 1, MAX_START_DROP)
        self.formation = Formation((FIELD_W - FORMATION_W) // 2, FORMATION_TOP + drop)
        self.march_timer = 0.0
        self.shields = shield_cells()
        self.cannon_x = (FIELD_W - CANNON_W) // 2
        self.shot: Projectile | None = None
        self.bombs: list = []
        self.bomb_timer = self._next_bomb_delay()
        self.ufo: Ufo | None = None
        self.ufo_timer = self.rng.uniform(UFO_MIN_DELAY, UFO_MAX_DELAY)
        self.phase = PLAY
        self.phase_elapsed = 0.0

    def _next_bomb_delay(self) -> float:
        return bomb_delay_base(self.wave) * self.rng.uniform(0.5, 1.5)

    # -- scoring -----------------------------------------------------------

    def add_score(self, points: int) -> None:
        self.score += points
        if not self.extra_life_given and self.score >= EXTRA_LIFE_SCORE:
            self.extra_life_given = True
            self.lives = min(MAX_LIVES, self.lives + 1)

    # -- save / restore ----------------------------------------------------

    def to_save(self) -> dict:
        """Serializable snapshot. Always taken in the PLAY phase: the caller
        is responsible for finish_phase() first (the UI save path does)."""
        f = self.formation
        shot = self.shot
        ufo = self.ufo
        return {
            "version": SAVE_VERSION,
            "rng": _rng_to_json(self.rng),
            "score": self.score,
            "lives": self.lives,
            "wave": self.wave,
            "extra_life_given": self.extra_life_given,
            "formation": {
                "x": f.x, "y": f.y, "dir": f.dir, "frame": f.frame,
                "alive": [row[:] for row in f.alive],
            },
            "march_timer": self.march_timer,
            "cannon_x": self.cannon_x,
            "shot": None if shot is None else [shot.x, shot.y, shot.acc],
            "bombs": [[b.x, b.y, b.acc] for b in self.bombs],
            "bomb_timer": self.bomb_timer,
            "shields": sorted([x, y] for x, y in self.shields),
            "ufo": None if ufo is None else [ufo.x, ufo.dir],
            "ufo_timer": self.ufo_timer,
            "game_over": self.game_over,
        }

    @classmethod
    def from_save(cls, data, rng=None):
        """Rebuild a saved game, or return None for anything corrupt,
        incompatible, or not exactly a save this version wrote."""
        try:
            if not isinstance(data, dict) or data.get("version") != SAVE_VERSION:
                return None
            game = cls(rng)
            state = _rng_from_json(data["rng"])
            if state is None:
                return None
            for key in ("score", "lives", "wave", "cannon_x"):
                if not _is_int(data[key]):
                    return None
            for key in ("march_timer", "bomb_timer", "ufo_timer"):
                if not _is_num(data[key]):
                    return None
            if data["score"] < 0 or data["lives"] < 0 or data["wave"] < 1:
                return None
            if not 0 <= data["cannon_x"] <= FIELD_W - CANNON_W:
                return None

            fd = data["formation"]
            alive = fd["alive"]
            if (not isinstance(alive, list) or len(alive) != FORM_ROWS
                    or any(not isinstance(row, list) or len(row) != FORM_COLS
                           or any(not isinstance(a, bool) for a in row)
                           for row in alive)):
                return None
            if not all(_is_int(fd[k]) for k in ("x", "y", "dir", "frame")):
                return None
            if fd["dir"] not in (-1, 1):
                return None
            form = Formation(fd["x"], fd["y"], fd["dir"])
            form.frame = fd["frame"] & 1
            form.alive = [row[:] for row in alive]
            b = form.bounds()
            if b is None:
                return None  # an empty formation is never saved (wave resolved)
            if b[0] < 0 or b[1] > FIELD_W - 1 or b[2] < 0 or b[3] >= CANNON_Y:
                return None

            def proj(p):
                if (not isinstance(p, list) or len(p) != 3 or not _is_int(p[0])
                        or not _is_int(p[1]) or not _is_num(p[2])):
                    raise ValueError
                if not (0 <= p[0] < FIELD_W and 0 <= p[1] < FIELD_H):
                    raise ValueError
                return Projectile(p[0], p[1], float(p[2]))

            shot = None if data["shot"] is None else proj(data["shot"])
            bombs = data["bombs"]
            if not isinstance(bombs, list):
                return None
            bombs = [proj(p) for p in bombs]

            shields = set()
            for cell in data["shields"]:
                if (not isinstance(cell, list) or len(cell) != 2
                        or not _is_int(cell[0]) or not _is_int(cell[1])):
                    return None
                x, y = cell
                if not (0 <= x < FIELD_W and 0 <= y < FIELD_H):
                    return None
                shields.add((x, y))

            ud = data["ufo"]
            ufo = None
            if ud is not None:
                if (not isinstance(ud, list) or len(ud) != 2 or not _is_num(ud[0])
                        or ud[1] not in (-1, 1)):
                    return None
                ufo = Ufo(float(ud[0]), ud[1])

            game.score = data["score"]
            game.lives = data["lives"]
            game.wave = data["wave"]
            game.extra_life_given = bool(data["extra_life_given"])
            game.formation = form
            game.march_timer = float(data["march_timer"])
            game.cannon_x = data["cannon_x"]
            game.shot = shot
            game.bombs = bombs
            game.bomb_timer = float(data["bomb_timer"])
            game.shields = shields
            game.ufo = ufo
            game.ufo_timer = float(data["ufo_timer"])
            game.game_over = bool(data["game_over"]) or game.lives <= 0
            game.phase = PLAY
            game.phase_elapsed = 0.0
            game.rng.setstate(state)
            return game
        except (KeyError, TypeError, ValueError, IndexError, AttributeError):
            return None

    # -- player ------------------------------------------------------------

    @property
    def playing(self) -> bool:
        return not self.paused and not self.game_over and self.phase == PLAY

    def move(self, dx: int) -> bool:
        """Step the cannon; one keypress is one short step."""
        if not self.playing:
            return False
        step = CANNON_STEP if dx > 0 else -CANNON_STEP
        nx = max(0, min(FIELD_W - CANNON_W, self.cannon_x + step))
        if nx == self.cannon_x:
            return False
        self.cannon_x = nx
        # Sliding into a bomb already on the cannon row counts as a hit.
        for bomb in self.bombs:
            if bomb.y == CANNON_Y and self._cannon_covers(bomb.x):
                self._hit_cannon()
                break
        return True

    def fire(self) -> bool:
        """Launch a shot: only one player shot may be on screen at a time."""
        if not self.playing or self.shot is not None:
            return False
        self.shot = Projectile(self.cannon_x + 1, CANNON_Y)
        return True

    def _cannon_covers(self, x: int) -> bool:
        return self.cannon_x <= x < self.cannon_x + CANNON_W

    def _hit_cannon(self) -> None:
        self.phase = DYING
        self.phase_elapsed = 0.0
        self.shot = None
        self.bombs = []

    # -- invaders ----------------------------------------------------------

    def _kill_invader(self, r: int, c: int) -> None:
        f = self.formation
        f.alive[r][c] = False
        x, y = f.pos(r, c)
        self.explosions.append([x, y, EXPLOSION_DURATION])
        self.add_score(KIND_POINTS[ROW_KINDS[r]])
        if f.count() == 0:
            self.phase = WAVE
            self.phase_elapsed = 0.0
            self.shot = None
            self.bombs = []
            self.ufo = None

    def march_step(self) -> None:
        """One formation step: sideways, or drop a row and reverse at an
        edge. Then invaders chew through shields and may land."""
        f = self.formation
        b = f.bounds()
        if b is None:
            return
        left, right, _, _ = b
        if (f.dir > 0 and right + 1 > FIELD_W - 1) or (f.dir < 0 and left - 1 < 0):
            f.y += 1
            f.dir = -f.dir
        else:
            f.x += f.dir
        f.frame ^= 1
        for _, _, x, y in f.members():
            for dx in range(SPRITE_W):
                self.shields.discard((x + dx, y))
        if self.shot is not None:
            hit = f.at(self.shot.x, self.shot.y)
            if hit is not None:
                self.shot = None
                self._kill_invader(*hit)
        b = f.bounds()
        if b is not None and b[3] >= CANNON_Y:
            self.invaded = True
            self.game_over = True

    def _fire_bomb(self) -> None:
        if len(self.bombs) >= bomb_cap(self.wave):
            return
        f = self.formation
        cols = [c for c in range(FORM_COLS) if f.bottom_of_column(c) is not None]
        if not cols:
            return
        c = self.rng.choice(cols)
        r = f.bottom_of_column(c)
        x, y = f.pos(r, c)
        bomb = Projectile(x + 1, y + 1)
        # Resolve the spawn cell too (a shield, the cannon, the shot).
        if self._resolve_bomb(bomb):
            self.bombs.append(bomb)

    # -- projectiles ---------------------------------------------------------

    def _resolve_shot(self) -> None:
        """Collide the shot with whatever occupies its current cell."""
        s = self.shot
        if s is None:
            return
        if s.y < 0:
            self.shot = None
            return
        for bomb in self.bombs:
            if bomb.x == s.x and bomb.y == s.y:
                self.bombs.remove(bomb)
                self.shot = None
                return
        if (s.x, s.y) in self.shields:
            self.shields.discard((s.x, s.y))
            self.shot = None
            return
        hit = self.formation.at(s.x, s.y)
        if hit is not None:
            self.shot = None
            self._kill_invader(*hit)
            return
        if s.y == UFO_Y and self.ufo is not None:
            left, right = self.ufo.span()
            if left <= s.x <= right:
                points = self.rng.choice(UFO_SCORES)
                self.add_score(points)
                self.ufo_popup = [max(0, left), points, UFO_POPUP_DURATION]
                self.ufo = None
                self.shot = None

    def _resolve_bomb(self, bomb) -> bool:
        """Collide one bomb; returns False once it is gone."""
        if bomb.y >= FIELD_H:
            return False
        s = self.shot
        if s is not None and s.x == bomb.x and s.y == bomb.y:
            self.shot = None
            return False
        if (bomb.x, bomb.y) in self.shields:
            self.shields.discard((bomb.x, bomb.y))
            return False
        if bomb.y == CANNON_Y and self._cannon_covers(bomb.x):
            self._hit_cannon()
            return False
        return True

    def _advance_projectiles(self, dt: float) -> bool:
        shot_steps = 0
        if self.shot is not None:
            self.shot.acc += SHOT_SPEED * dt
            shot_steps = int(self.shot.acc)
            self.shot.acc -= shot_steps
        speed = bomb_speed(self.wave)
        bomb_steps = {}
        for bomb in self.bombs:
            bomb.acc += speed * dt
            n = int(bomb.acc)
            bomb.acc -= n
            bomb_steps[id(bomb)] = n
        most = max([shot_steps] + list(bomb_steps.values()))
        for i in range(most):
            # Shot first, then bombs, one row each, checking the cell after
            # every move: a shot and bomb can never pass through each other.
            if self.shot is not None and i < shot_steps:
                self.shot.y -= 1
                self._resolve_shot()
            survivors = []
            for bomb in self.bombs:
                if i < bomb_steps.get(id(bomb), 0):
                    bomb.y += 1
                    if not self._resolve_bomb(bomb):
                        continue
                survivors.append(bomb)
            if self.phase != PLAY:
                break  # a hit or wave clear resets the projectiles
            self.bombs = survivors
        return most > 0

    # -- phases ------------------------------------------------------------

    def finish_phase(self) -> None:
        """Resolve a pending death or wave transition immediately (pause,
        quit, save). The state afterwards is exactly the post-phase state."""
        if self.phase == DYING:
            self.lives -= 1
            self.phase = PLAY
            self.phase_elapsed = 0.0
            if self.lives <= 0:
                self.lives = 0
                self.game_over = True
            else:
                self.cannon_x = (FIELD_W - CANNON_W) // 2
                self.shot = None
                self.bombs = []
                self.bomb_timer = self._next_bomb_delay()
        elif self.phase == WAVE:
            self.wave += 1
            self._start_wave()
        self.explosions = []
        self.ufo_popup = None

    # -- time --------------------------------------------------------------

    def update(self, dt: float) -> bool:
        """Advance the game by `dt` seconds. True if anything changed."""
        if self.paused or self.game_over:
            return False
        changed = False
        if self.explosions:
            for e in self.explosions:
                e[2] -= dt
            self.explosions = [e for e in self.explosions if e[2] > 0]
            changed = True
        if self.ufo_popup is not None:
            self.ufo_popup[2] -= dt
            if self.ufo_popup[2] <= 0:
                self.ufo_popup = None
            changed = True

        if self.phase != PLAY:
            self.phase_elapsed += dt
            limit = DEATH_DURATION if self.phase == DYING else WAVE_DURATION
            if self.phase_elapsed >= limit:
                self.finish_phase()
            return True

        # March.
        self.march_timer += dt
        while self.phase == PLAY and not self.game_over:
            interval = march_interval(self.formation.count(), self.wave)
            if self.march_timer < interval:
                break
            self.march_timer -= interval
            self.march_step()
            changed = True
        if self.game_over or self.phase != PLAY:
            return True

        # UFO.
        if self.ufo is None:
            self.ufo_timer -= dt
            if self.ufo_timer <= 0:
                direction = self.rng.choice((-1, 1))
                x = -float(UFO_W) + 1 if direction > 0 else float(FIELD_W - 1)
                self.ufo = Ufo(x, direction)
                self.ufo_timer = self.rng.uniform(UFO_MIN_DELAY, UFO_MAX_DELAY)
                changed = True
        else:
            self.ufo.x += self.ufo.dir * UFO_SPEED * dt
            if self.ufo.x > FIELD_W or self.ufo.x < -UFO_W:
                self.ufo = None
            changed = True

        # Bombs.
        self.bomb_timer -= dt
        if self.bomb_timer <= 0:
            self._fire_bomb()
            self.bomb_timer = self._next_bomb_delay()
            changed = True

        if self._advance_projectiles(dt):
            changed = True
        return changed


# -- screen text -------------------------------------------------------------

TITLE = "Space Invaders"

# Every key the game reacts to (controls box / key hint). Key strings stay
# <= 9 characters so the narrowest side column does not clip them.
CONTROLS = [
    ("<- / ->", "move"),
    ("a/d h/l", "move"),
    ("space", "fire"),
    ("up w k", "fire"),
    ("?", "help"),
    ("p / r", "pause / restart"),
    ("q", "menu"),
]

HELP = [
    ("Goal",
     "Shoot down every invader in the marching formation before it reaches "
     "the bottom, while dodging the bombs it drops. Clearing a formation "
     "starts the next, faster wave."),
    ("Controls",
     "- left/right arrows, a/d or h/l: move the cannon two columns per "
     "press. Hold the key to keep moving (key repeat).\n"
     "- space, up arrow, w or k: fire. Only one of your shots can be on "
     "screen at a time, so a miss costs you time.\n"
     "- p pauses and resumes.\n"
     "- r restarts (the current game is abandoned, its score still counts "
     "for the best score).\n"
     "- q returns to the arcade menu; the game is saved and resumes, "
     "paused, next time.\n"
     "- ? opens this help and pauses the game; up/down scroll, any other "
     "key closes it."),
    ("How it works",
     "The formation is 5 rows of 8 invaders. It marches sideways; at an "
     "edge it drops one row and turns around. It speeds up as invaders die "
     "and with every wave.\n"
     "The lowest invader in each column drops bombs ('!' and ':'). Only a "
     "few bombs fall at once: 3 at first, up to 5 on later waves, and they "
     "fall faster each wave.\n"
     "A bomb hitting your cannon ('/A\\') costs a life. You start with 3 "
     "(at most 9); losing the last one ends the game. If the formation "
     "reaches your row, the game is over at once, whatever lives you have "
     "left.\n"
     "The four green bunkers soak up shots and bombs: each hit chips one "
     "block away, and invaders marching through them erase them. Your shot "
     "and a bomb that meet cancel each other out.\n"
     "Now and then a UFO ('<=O=>') flies across the top row.\n"
     "Each new wave starts one row lower (up to 3 rows) with fresh "
     "bunkers."),
    ("Scoring",
     "- squid (top row, '/o\\'): 30\n"
     "- crab (rows 2-3, '{#}'): 20\n"
     "- octopus (rows 4-5, '<=>'): 10\n"
     "- UFO: 50, 100, 150 or 300 at random\n"
     "- one extra life at 1500 points.\n"
     "Your best score is kept between games."),
    ("Tips",
     "- Fire from close under a target: the shot lands sooner and you can "
     "shoot again sooner.\n"
     "- Clear the outer columns first: a narrower formation marches "
     "further before each drop, which buys you time.\n"
     "- Duck under a bunker to wait out a bomb, but it wears away.\n"
     "- The last few invaders move very fast; lead them."),
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


# -- layout (pure) -----------------------------------------------------------

# The popup's inner area is 93x28 (77x24 for the older 80x26 popup). Row 0
# is the header/banner; below it the field window (40x20 interior + frame =
# 42x22) and the side column (common.side_width: 30 when it fits) are
# centred as one block. The side column stacks the stats box, the controls
# box and the agents box. Without room for a side column the field is
# centred alone and a one-line key hint goes on the last row -- or into the
# header, or (no header row at all) into the field's bottom border. Below
# the field itself the too-small message appears.
FIELD_WIN_W = FIELD_W + 2
FIELD_WIN_H = FIELD_H + 2
STATS_H = 7            # five lines + frame
AGENTS_MIN_H = 3       # frame + one line
MIN_COLS, MIN_ROWS = FIELD_WIN_W, FIELD_WIN_H


def side_boxes(y: int, x: int, h: int, w: int) -> dict:
    """Stack stats, controls and agents in a y/x/h/w side column. Priority
    when short: controls, then stats, then agents (dropped first)."""
    ctrl_h = min(controls_height(CONTROLS, w), h)
    stats_h = min(STATS_H, h - ctrl_h)
    agents_h = h - ctrl_h - stats_h
    if agents_h < AGENTS_MIN_H:
        stats_h += agents_h  # no agents box: the stats box takes the rest
        agents_h = 0
    boxes = {"stats_win": None, "controls_win": None, "agents_win": None}
    if stats_h >= 3:
        boxes["stats_win"] = (y, x, stats_h, w)
    boxes["controls_win"] = (y + stats_h, x, ctrl_h, w)
    if agents_h:
        boxes["agents_win"] = (y + stats_h + ctrl_h, x, agents_h, w)
    return boxes


def layout_plan(cols: int, rows: int) -> dict:
    """Pure layout decision for a (cols, rows) screen: tier flags plus each
    region's (y, x, height, width). Unit-tested; Layout() only realizes it.
    `hint` is the key-hint row when there is no controls box;
    `hint_in_header` says it shares row 0 with the header."""
    plan = {
        "too_small": rows < MIN_ROWS or cols < MIN_COLS,
        "header": rows >= MIN_ROWS + 1,
        "stats": False,
        "agents": False,
        "field": None,
        "stats_win": None,
        "controls_win": None,
        "agents_win": None,
        "hint": None,
        "hint_in_header": False,
    }
    if plan["too_small"]:
        return plan
    top = 1 if plan["header"] else 0
    side_w = side_width(cols, FIELD_WIN_W)
    if side_w:
        y, x = center_origin(cols, rows, FIELD_WIN_W + 1 + side_w, FIELD_WIN_H, top)
        plan["field"] = (y, x, FIELD_WIN_H, FIELD_WIN_W)
        plan.update(side_boxes(y, x + FIELD_WIN_W + 1, FIELD_WIN_H, side_w))
        plan["stats"] = plan["stats_win"] is not None
        plan["agents"] = plan["agents_win"] is not None
        return plan
    if rows >= FIELD_WIN_H + 2:  # header + field + a spare last row
        plan["hint"] = (rows - 1, 0, 1, cols)
        y, x = center_origin(cols, rows - 1, FIELD_WIN_W, FIELD_WIN_H, top)
    else:
        y, x = center_origin(cols, rows, FIELD_WIN_W, FIELD_WIN_H, top)
        if plan["header"]:
            plan["hint"] = (0, 0, 1, cols)
            plan["hint_in_header"] = True
        else:  # not a spare row anywhere: inside the field's bottom border
            plan["hint"] = (y + FIELD_WIN_H - 1, x + 2, 1, FIELD_WIN_W - 4)
    plan["field"] = (y, x, FIELD_WIN_H, FIELD_WIN_W)
    return plan


# --------------------------------------------------------------------------
# curses UI
# --------------------------------------------------------------------------

SPRITES = {
    "squid": ("/o\\", "\\o/"),
    "crab": ("{#}", "}#{"),
    "octopus": ("<=>", ">=<"),
}
CANNON = "/A\\"
CANNON_DEAD = ("*#*", "#*#")
EXPLOSION = "\\*/"
UFO_SPRITE = "<=O=>"
SHOT = "|"
BOMB = ("!", ":")
SHIELD = "█"

PAIR_BASE = 40  # pairs 40-47; see the numbering note in common.py
COLORS = {
    "squid": curses.COLOR_MAGENTA,
    "crab": curses.COLOR_CYAN,
    "octopus": curses.COLOR_YELLOW,
    "cannon": curses.COLOR_GREEN,
    "shield": curses.COLOR_GREEN,
    "ufo": curses.COLOR_RED,
    "bomb": curses.COLOR_RED,
    "shot": curses.COLOR_WHITE,
}


def init_colors() -> dict:
    """Register the invaders colours; returns {name: pair number}."""
    bg = default_bg()
    pairs = {}
    for i, (name, color) in enumerate(COLORS.items()):
        curses.init_pair(PAIR_BASE + i, color, bg)
        pairs[name] = PAIR_BASE + i
    return pairs


class Layout:
    """Screen regions, chosen by size tier; rebuilt on resize."""

    def __init__(self, stdscr):
        self.stdscr = stdscr
        self.rows, self.cols = stdscr.getmaxyx()
        self.plan = layout_plan(self.cols, self.rows)
        self.too_small = self.plan["too_small"]
        self.header = self.plan["header"]
        self.stats = self.plan["stats"]
        self.agents = self.plan["agents"]
        self.hint = self.plan["hint"]
        self.hint_in_header = self.plan["hint_in_header"]
        self.field = self.stats_win = self.controls_win = self.agents_win = None
        if self.too_small:
            return
        for name in ("field", "stats_win", "controls_win", "agents_win"):
            if self.plan[name] is not None:
                y, x, h, w = self.plan[name]
                setattr(self, name, stdscr.derwin(h, w, y, x))

    def boxes(self):
        for win in (self.field, self.stats_win, self.controls_win, self.agents_win):
            if win is not None:
                win.erase()
                frame(win)


def _center(win, y, text, attr=0):
    _, cols = win.getmaxyx()
    safe_addstr(win, y, max(0, (cols - len(text)) // 2), text, attr)


def draw(pairs, status_pairs, game, lay, roster, attention, resume=False, best=0,
         help=None):
    _draw_screen(pairs, status_pairs, game, lay, roster, attention, resume, best)
    if help is not None and help.is_open:
        help.draw(lay.stdscr)


def _draw_screen(pairs, status_pairs, game, lay, roster, attention, resume, best):
    scr = lay.stdscr
    scr.erase()

    if lay.too_small:
        msg = f"pane too small: need {MIN_COLS}x{MIN_ROWS}, have {lay.cols}x{lay.rows}"
        safe_addstr(scr, lay.rows // 2, max(0, (lay.cols - len(msg)) // 2),
                    msg[: max(0, lay.cols - 1)], curses.A_BOLD)
        msg2 = "resize the pane (q menu)"
        safe_addstr(scr, lay.rows // 2 + 1, max(0, (lay.cols - len(msg2)) // 2),
                    msg2[: max(0, lay.cols - 1)])
        return

    def cp(name):
        return curses.color_pair(pairs[name]) if name in pairs else 0

    # -- header / attention banner ------------------------------------------
    if not draw_banner(scr, lay.cols, attention, resume) and lay.header:
        if game.game_over:
            state = "GAME OVER"
        elif game.paused:
            state = "paused"
        else:
            state = f"wave {game.wave}"
        score = f"score {game.score} | lives {game.lives} | wave {game.wave} "
        if lay.hint_in_header:  # no spare row: the header carries the keys
            head = f" {score}| "
            head += key_hint(max(0, lay.cols - 1 - len(head)))
        else:
            head = f" space invaders - {state} "
            if not lay.stats:
                head += "| " + score
        safe_addstr(scr, 0, 0, head[: lay.cols - 1],
                    curses.A_STANDOUT | (curses.A_BOLD if game.game_over else 0))

    # -- field ---------------------------------------------------------------
    lay.boxes()
    fw = lay.field

    def put(x, y, text, attr=0):
        # Clip sprites to the field interior (the UFO enters partly off-screen).
        if y < 0 or y >= FIELD_H:
            return
        if x < 0:
            text = text[-x:]
            x = 0
        text = text[: max(0, FIELD_W - x)]
        if text:
            safe_addstr(fw, 1 + y, 1 + x, text, attr)

    for x, y in game.shields:
        put(x, y, SHIELD, cp("shield"))
    f = game.formation
    for r, _, x, y in f.members():
        kind = ROW_KINDS[r]
        put(x, y, SPRITES[kind][f.frame], cp(kind) | curses.A_BOLD)
    for x, y, _ in game.explosions:
        put(x, y, EXPLOSION, curses.A_BOLD)
    if game.ufo is not None:
        put(int(game.ufo.x), UFO_Y, UFO_SPRITE, cp("ufo") | curses.A_BOLD)
    if game.ufo_popup is not None:
        put(game.ufo_popup[0], UFO_Y, str(game.ufo_popup[1]), cp("ufo") | curses.A_BOLD)
    for i, b in enumerate(game.bombs):
        put(b.x, b.y, BOMB[(b.y + i) % 2], cp("bomb") | curses.A_BOLD)
    if game.shot is not None:
        put(game.shot.x, game.shot.y, SHOT, cp("shot") | curses.A_BOLD)
    if game.phase == DYING:
        flash = CANNON_DEAD[int(game.phase_elapsed * 8) % 2]
        put(game.cannon_x, CANNON_Y, flash, cp("bomb") | curses.A_BOLD)
    elif not game.game_over or not game.invaded:
        put(game.cannon_x, CANNON_Y, CANNON, cp("cannon") | curses.A_BOLD)

    # -- overlays -------------------------------------------------------------
    mid = 1 + FIELD_H // 2
    if game.game_over:
        _center(fw, mid - 1, " GAME OVER ", curses.A_STANDOUT | curses.A_BOLD)
        _center(fw, mid, " r restart | q menu ", curses.A_STANDOUT)
    elif game.phase == WAVE:
        _center(fw, mid - 1, f" WAVE {game.wave} CLEARED ", curses.A_STANDOUT | curses.A_BOLD)
        _center(fw, mid, f" get ready for wave {game.wave + 1} ", curses.A_STANDOUT)
    elif game.paused:
        _center(fw, mid - 1, " PAUSED ", curses.A_STANDOUT | curses.A_BOLD)
        _center(fw, mid, " p resume | q menu ", curses.A_STANDOUT)

    # -- stats ---------------------------------------------------------------
    if lay.stats_win is not None:
        sw = lay.stats_win
        sw_h, sw_w = sw.getmaxyx()
        safe_addstr(sw, 0, 2, " space invaders "[: max(0, sw_w - 3)], curses.A_BOLD)
        lines = (
            f"score {game.score}",
            f"best  {max(best, game.score)}",
            f"lives {game.lives}",
            f"wave  {game.wave}",
            f"left  {f.count()}",
        )
        for i, line in enumerate(lines[: max(0, sw_h - 2)]):
            safe_addstr(sw, 1 + i, 1, line[: sw_w - 2], curses.A_BOLD if i == 0 else 0)

    # -- controls / key hint ---------------------------------------------------
    if lay.controls_win is not None:
        draw_controls(lay.controls_win, CONTROLS)
    if lay.hint is not None and not lay.hint_in_header:
        y, x, _, w = lay.hint
        text = key_hint(w - 1)
        if y != lay.rows - 1:  # inside the field border: set off by spaces
            text = f" {key_hint(w - 2)[: w - 2]} "
        safe_addstr(scr, y, x + max(0, (w - len(text)) // 2), text, curses.A_DIM)

    # -- agents --------------------------------------------------------------
    if lay.agents_win is not None:
        draw_agents(lay.agents_win, roster, status_pairs)


LEFT_KEYS = (curses.KEY_LEFT, ord("h"), ord("H"), ord("a"), ord("A"))
RIGHT_KEYS = (curses.KEY_RIGHT, ord("l"), ord("L"), ord("d"), ord("D"))
FIRE_KEYS = (ord(" "), curses.KEY_UP, ord("k"), ord("K"), ord("w"), ord("W"))


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
    if ch in LEFT_KEYS:
        return game.move(-1)
    if ch in RIGHT_KEYS:
        return game.move(1)
    if ch in FIRE_KEYS:
        return game.fire()
    return False


def route_help_key(help, game, ch, paused_by_help: bool):
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
    """Play Space Invaders until `q` (back to the arcade menu) or SIGTERM.
    The game is saved on every exit path and resumed, paused, next run."""
    stdscr = session.stdscr
    pairs = init_colors()
    game_path = session.path("invaders-game.json")
    hiscore_path = session.path("invaders-hiscore.json")
    hiscore = load_hiscore(hiscore_path)

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
    help = HelpOverlay(TITLE, HELP)
    paused_by_help = False
    paused_by_attention = False
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
                # The alert also takes over a pause the help caused: closing
                # the help keeps the game paused until the banner goes.
                paused_by_help = False
                game.paused = True
                paused_by_attention = True
            dirty = dirty or changed

            # Input: drain every pending key this tick so auto-repeat keeps
            # the cannon responsive.
            quit_now = False
            while True:
                ch = stdscr.getch()
                if ch == -1:
                    break
                consumed, paused_by_help = route_help_key(
                    help, game, ch, paused_by_help)
                if consumed:
                    dirty = True
                    continue
                if ch in (ord("q"), ord("Q")):
                    quit_now = True
                    break
                if ch == curses.KEY_RESIZE:
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
            if quit_now:
                break

            # Clamp dt so a stall (suspend, slow terminal) cannot teleport
            # the formation several steps at once.
            if game.update(min(dt, 0.1)):
                dirty = True

            if game.game_over and not game_over_handled:
                game_over_handled = True
                if game_path:
                    remove_path(game_path)
                hiscore = max(hiscore, game.score)
                save_hiscore(hiscore_path, hiscore)
                dirty = True

            if dirty:
                draw(pairs, session.status_pairs, game, lay,
                     session.roster, session.attention, resume_banner, hiscore, help)
                stdscr.noutrefresh()
                curses.doupdate()
                dirty = False

            time.sleep(0.01)
    finally:
        # The one save point: q, SIGTERM, or any other exit path lands here.
        # Resolve a pending death/wave transition first so what is saved is
        # a consistent PLAY state, exactly as if it had finished on screen.
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

    sys.exit(arcade.main(["--game", "invaders"] + sys.argv[1:]))
