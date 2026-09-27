#!/usr/bin/env python3
"""Q*bert for the Herdr arcade (see arcade.py, which runs it via `run()`).

Structure: everything above the "curses UI" divider is pure game logic --
the pyramid, hops, discs, enemies, colour rules, scoring, lives, rounds and
levels, save/restore and the layout plan -- with no terminal dependency, so
`tests/test_qbert.py` imports this module without a terminal.

Geometry: the pyramid has ROWS rows; row r holds r + 1 cubes, addressed
(r, c) with 0 <= c <= r, (0, 0) at the top. A hop changes the row by one:
up-right (r-1, c), up-left (r-1, c-1), down-right (r+1, c+1) and down-left
(r+1, c). Any other (r, c) is off the pyramid: a fall, unless a disc floats
there (discs sit just beside the left and right edges, at (q, -1) and
(q, q + 1)). Each cube is drawn 8 columns wide and 3 rows tall (top tile plus
two rows of side faces), rows offset by half a cube, so the text reads as
an isometric pyramid; characters stand on the row just above their tile.
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

ROWS = 7                  # pyramid rows: 28 cubes
N_CUBES = ROWS * (ROWS + 1) // 2

UR, UL, DR, DL = "ur", "ul", "dr", "dl"
DIRS = (UR, UL, DR, DL)
DELTAS = {UR: (-1, 0), UL: (-1, -1), DR: (1, 1), DL: (1, 0)}
COILY_ORDER = (UL, UR, DL, DR)   # Coily's tie-break when two hops are equal

QB_HOP = 0.24             # seconds per Q*bert hop (input queues meanwhile)
QUEUE_MAX = 2             # hops remembered while one is in the air
ENEMY_HOP = 0.28          # enemy hop animation
DROP_TIME = 0.45          # a ball or Slick/Sam dropping onto row 1
ENTER_TIME = 0.35         # Ugg/Wrongway climbing on at a bottom corner
HATCH_TIME = 1.0          # the purple ball waits this long before hatching
MIN_WAIT = 0.08
DISC_TIME = 1.8           # the disc ride to the top
DEATH_TIME = 1.8
CLEAR_TIME = 2.4          # round-clear flash
FREEZE_TIME = 4.0         # green ball
FIRST_SPAWN = 2.2         # quiet start of every round / life
SPAWN_WAIT = (2.4, 4.4)
MAX_DT = 0.25             # clamp for stalls
MAX_STEP = 1.0 / 60       # update() integrates in slices no longer than this
NOTE_TIME = 2.0

LIVES = 4
MAX_LIVES = 9
ROUNDS_PER_LEVEL = 4

CHANGE_POINTS = 25        # every colour change toward the target
GREEN_POINTS = 100
CATCH_POINTS = 300        # Slick or Sam
LURE_POINTS = 500         # Coily follows you off the edge
ROUND_BONUS = 1000        # +250 per round played, up to ROUND_BONUS_MAX
ROUND_BONUS_STEP = 250
ROUND_BONUS_MAX = 5000
DISC_BONUS = 50           # per unused disc, times the level
EXTRA_LIFE_FIRST = 8000
EXTRA_LIFE_EVERY = 14000

# Seconds between hops, before the per-round speed-up.
INTERVALS = {"red": 0.80, "purple": 0.85, "coily": 0.80, "green": 0.85,
             "slick": 0.90, "sam": 0.90, "ugg": 0.95, "wrongway": 0.95}
KINDS = tuple(INTERVALS)
DEADLY = ("red", "purple", "coily", "ugg", "wrongway")
DOWNHILL = ("red", "purple", "green", "slick", "sam")
# Ugg climbs from the bottom-right corner leftwards and up-left; Wrongway
# from the bottom-left corner rightwards and up-right. Both run along the
# sides of the cubes and leave when they step off.
SIDE_MOVES = {"ugg": ((0, -1), (-1, -1)), "wrongway": ((0, 1), (-1, 0))}

PALETTE = ("blue", "yellow", "cyan", "green", "magenta", "red", "white")
# (start, intermediate, target) tile colours, one scheme per round.
SCHEMES = (
    ("blue", "cyan", "yellow"),
    ("green", "yellow", "magenta"),
    ("red", "white", "cyan"),
    ("magenta", "blue", "white"),
    ("cyan", "red", "green"),
    ("white", "magenta", "yellow"),
)
# Colour rules by level (the last applies to every later level).
RULES = ("once", "twice", "toggle", "twice_back")
RULE_TEXT = {
    "once": "one hop sets a cube",
    "twice": "two hops per cube",
    "toggle": "hop again: flips back",
    "twice_back": "two hops; 3rd flips back",
}
DISC_ROWS = (1, 2, 3, 4)  # rows a disc can float beside

SAVE_VERSION = 1


def valid(r: int, c: int) -> bool:
    """(r, c) is a cube of the pyramid."""
    return 0 <= r < ROWS and 0 <= c <= r


def neighbour(r: int, c: int, d: str):
    dr, dc = DELTAS[d]
    return r + dr, c + dc


def hops_between(a, b) -> int:
    """Fewest hops from cube a to cube b on an unbounded lattice."""
    ax, bx = 2 * a[1] - a[0], 2 * b[1] - b[0]
    return max(abs(bx - ax), abs(b[0] - a[0]))


def rule_for(level: int) -> str:
    return RULES[min(level, len(RULES)) - 1]


def stages_for(level: int) -> int:
    """Colour states per cube: start .. target."""
    return 3 if rule_for(level) in ("twice", "twice_back") else 2


def discs_for(level: int) -> int:
    return 2 if level <= 2 else (3 if level <= 4 else 4)


def speed_factor(round_index: int) -> float:
    return min(1.8, 1.0 + 0.06 * round_index)


class Phase:
    """A transient animation as an explicit logic phase: 'disc' (riding a
    disc to the top), 'dying' (hit, or fallen off) or 'clear' (round-clear
    flash). Resolved when elapsed reaches duration, or at once via
    finish_phase()."""

    __slots__ = ("kind", "elapsed", "duration", "cause")

    def __init__(self, kind, duration, cause=None):
        self.kind = kind
        self.elapsed = 0.0
        self.duration = duration
        self.cause = cause


class Enemy:
    """One enemy. (r, c) is where it is or is hopping to; (fr, fc) where
    the current hop started (None while dropping or climbing on); t is the
    hop time left out of dur, and wait the time until its next hop."""

    __slots__ = ("kind", "r", "c", "fr", "fc", "t", "dur", "wait")

    def __init__(self, kind, r, c, fr=None, fc=None, t=0.0, dur=0.0, wait=0.0):
        self.kind = kind
        self.r, self.c = r, c
        self.fr, self.fc = fr, fc
        self.t, self.dur, self.wait = t, dur, wait

    def cell(self):
        """The cube it occupies for collisions: the start of a hop until
        halfway through, then the destination (None while dropping in)."""
        if self.t > 0 and self.t > self.dur / 2:
            return None if self.fr is None else (self.fr, self.fc)
        return (self.r, self.c)

    def to_list(self):
        return [self.kind, self.r, self.c, self.fr, self.fc, self.t, self.dur, self.wait]


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
        self.round = 1
        self.paused = False
        self.game_over = False
        self.phase: Phase | None = None
        self.clock = 0.0
        self.next_extra = EXTRA_LIFE_FIRST
        self.note = ""            # transient message (not saved)
        self.note_t = 0.0
        self.spawning = True      # tests switch enemies off
        self._start_round()

    def reset(self) -> None:
        self.__init__(self.rng)

    # -- rounds ------------------------------------------------------------

    @property
    def round_index(self) -> int:
        return (self.level - 1) * ROUNDS_PER_LEVEL + self.round - 1

    @property
    def rule(self) -> str:
        return rule_for(self.level)

    @property
    def stages(self) -> int:
        return stages_for(self.level)

    @property
    def scheme(self):
        return SCHEMES[self.round_index % len(SCHEMES)]

    def cube_colour(self, state: int) -> str:
        """Palette name of a cube state under this round's scheme."""
        start, mid, target = self.scheme
        if state >= self.stages - 1:
            return target
        return start if state == 0 else mid

    def _start_round(self) -> None:
        self.cubes = [[0] * (r + 1) for r in range(ROWS)]
        self.discs = self._make_discs()
        self.enemies: list = []
        self.spawn_t = FIRST_SPAWN
        self.spawned = 0
        self.freeze_t = 0.0
        self.lure = None
        self._place_qbert(0, 0)
        self._say(f"level {self.level} - round {self.round}")

    def _make_discs(self) -> list:
        spots = [[q, -1] for q in DISC_ROWS] + [[q, q + 1] for q in DISC_ROWS]
        left, right = spots[:len(DISC_ROWS)], spots[len(DISC_ROWS):]
        chosen = [self.rng.choice(left), self.rng.choice(right)]
        rest = [s for s in spots if s not in chosen]
        self.rng.shuffle(rest)
        chosen += rest[:max(0, discs_for(self.level) - 2)]
        return sorted(chosen)

    def _place_qbert(self, r: int, c: int) -> None:
        self.qr, self.qc = r, c
        self.q_from = None
        self.q_t = 0.0
        self.facing = DR
        self.queue: list = []

    def _say(self, text: str) -> None:
        self.note, self.note_t = text, NOTE_TIME

    def _add_score(self, points: int) -> None:
        self.score += points
        while self.score >= self.next_extra:
            self.lives = min(MAX_LIVES, self.lives + 1)
            self.next_extra += EXTRA_LIFE_EVERY

    def remaining(self) -> int:
        top = self.stages - 1
        return sum(1 for row in self.cubes for s in row if s != top)

    def round_bonus(self) -> int:
        return min(ROUND_BONUS_MAX, ROUND_BONUS + ROUND_BONUS_STEP * self.round_index)

    # -- Q*bert ------------------------------------------------------------

    @property
    def alive(self) -> bool:
        return not self.game_over and self.phase is None

    def qbert_cell(self):
        """The cube Q*bert occupies for collisions, or None off the pyramid."""
        if self.q_t > 0 and self.q_t > QB_HOP / 2 and self.q_from is not None:
            cell = tuple(self.q_from)
        else:
            cell = (self.qr, self.qc)
        return cell if valid(*cell) else None

    def press(self, d: str) -> bool:
        """A hop key. Starts the hop, or queues it while one is in the air.
        Returns True if the key was taken."""
        if self.paused or not self.alive:
            return False
        if self.q_t > 0:
            if len(self.queue) < QUEUE_MAX:
                self.queue.append(d)
                return True
            return False
        self._start_hop(d)
        return True

    def _start_hop(self, d: str) -> None:
        self.q_from = [self.qr, self.qc]
        self.qr, self.qc = neighbour(self.qr, self.qc, d)
        self.q_t = QB_HOP
        self.facing = d

    def _land_qbert(self) -> None:
        self.q_from = None
        self.q_t = 0.0
        spot = [self.qr, self.qc]
        if valid(self.qr, self.qc):
            self._step_cube(self.qr, self.qc)
        elif spot in self.discs:
            self._board_disc(spot)
        else:
            self.queue = []
            self.phase = Phase("dying", DEATH_TIME, "fall")

    def _step_cube(self, r: int, c: int) -> None:
        top = self.stages - 1
        s = self.cubes[r][c]
        if s < top:
            self.cubes[r][c] = s + 1
            self._add_score(CHANGE_POINTS)
            if self.remaining() == 0:
                self._clear_round()
        elif self.rule == "toggle":
            self.cubes[r][c] = 0
        elif self.rule == "twice_back":
            self.cubes[r][c] = top - 1

    def _clear_round(self) -> None:
        bonus = self.round_bonus() + DISC_BONUS * self.level * len(self.discs)
        self._add_score(bonus)
        self._say(f"round clear  +{bonus}")
        self.enemies = []
        self.queue = []
        self.lure = None
        self.freeze_t = 0.0
        self.phase = Phase("clear", CLEAR_TIME)

    def _board_disc(self, spot) -> None:
        self.discs.remove(spot)
        self.queue = []
        coily = [e for e in self.enemies if e.kind == "coily"]
        self.enemies = coily       # everything else leaves the pyramid
        self.lure = list(spot) if coily else None
        self.phase = Phase("disc", DISC_TIME, list(spot))

    def _die(self, cause: str) -> None:
        cell = self.qbert_cell()
        if cell is not None:
            self.qr, self.qc = cell
        self.q_from = None
        self.q_t = 0.0
        self.queue = []
        self.phase = Phase("dying", DEATH_TIME, cause)

    def finish_phase(self) -> None:
        """Resolve a pending phase immediately (pause, quit, save). The state
        afterwards is exactly what the animation would have led to."""
        phase = self.phase
        if phase is None:
            return
        self.phase = None
        if phase.kind == "disc":
            self._place_qbert(0, 0)
        elif phase.kind == "dying":
            self.lives -= 1
            self.enemies = []
            self.lure = None
            self.freeze_t = 0.0
            self.spawn_t = FIRST_SPAWN
            if self.lives <= 0:
                self.lives = 0
                self.game_over = True
            elif phase.cause == "fall":
                self._place_qbert(0, 0)
            else:
                self._place_qbert(self.qr, self.qc)
        else:  # clear
            self.round += 1
            if self.round > ROUNDS_PER_LEVEL:
                self.round = 1
                self.level += 1
            self._start_round()

    def resolve_phases(self) -> None:
        while self.phase is not None:
            self.finish_phase()

    # -- enemies -----------------------------------------------------------

    def interval(self, kind: str) -> float:
        return INTERVALS[kind] / speed_factor(self.round_index)

    def max_enemies(self) -> int:
        return min(6, 2 + self.level)

    def spawn(self, kind: str) -> Enemy:
        if kind == "ugg":
            e = Enemy(kind, ROWS - 1, ROWS - 1, t=ENTER_TIME, dur=ENTER_TIME)
        elif kind == "wrongway":
            e = Enemy(kind, ROWS - 1, 0, t=ENTER_TIME, dur=ENTER_TIME)
        else:
            e = Enemy(kind, 1, self.rng.randrange(2), t=DROP_TIME, dur=DROP_TIME)
        self.enemies.append(e)
        self.spawned += 1
        return e

    def _pick_kind(self):
        present = {e.kind for e in self.enemies}
        ri = self.round_index
        has_coily = bool(present & {"purple", "coily"})
        if self.spawned == 1 and not has_coily:
            return "purple"  # Coily turns up early every round
        weights = [("red", 4)]
        if not has_coily:
            weights.append(("purple", 3))
        if ri >= 1 and "green" not in present and self.freeze_t <= 0:
            weights.append(("green", 1))
        if ri >= 2:
            weights += [(k, 1) for k in ("slick", "sam") if k not in present]
        if ri >= 4:
            weights += [(k, 1) for k in ("ugg", "wrongway") if k not in present]
        total = sum(w for _, w in weights)
        pick = self.rng.uniform(0, total)
        for kind, w in weights:
            pick -= w
            if pick <= 0:
                return kind
        return weights[-1][0]

    def _spawn_tick(self, dt: float) -> None:
        if not self.spawning:
            return
        self.spawn_t -= dt
        if self.spawn_t > 0:
            return
        f = speed_factor(self.round_index)
        self.spawn_t = self.rng.uniform(*SPAWN_WAIT) / f
        if len(self.enemies) < self.max_enemies():
            self.spawn(self._pick_kind())

    def _update_enemies(self, dt: float) -> None:
        if self.freeze_t > 0:
            self.freeze_t = max(0.0, self.freeze_t - dt)
            return
        for e in list(self.enemies):
            if e.t > 0:
                e.t -= dt
                if e.t <= 0:
                    e.t = 0.0
                    self._enemy_land(e)
                continue
            e.wait -= dt
            if e.wait <= 0:
                self._enemy_act(e)

    def _enemy_land(self, e: Enemy) -> None:
        e.fr = e.fc = None
        if not valid(e.r, e.c):
            if e.kind == "coily" and self.lure == [e.r, e.c]:
                self._add_score(LURE_POINTS)
                self._say(f"Coily lured off!  +{LURE_POINTS}")
                self.lure = None
            self.enemies.remove(e)
            return
        if e.kind in ("slick", "sam"):
            self.cubes[e.r][e.c] = max(0, self.cubes[e.r][e.c] - 1)
        if e.kind == "purple" and e.r == ROWS - 1:
            e.wait = HATCH_TIME
        else:
            e.wait = max(MIN_WAIT, self.interval(e.kind) - ENEMY_HOP)

    def _enemy_act(self, e: Enemy) -> None:
        if e.kind == "purple" and e.r == ROWS - 1:
            e.kind = "coily"
            e.wait = self.interval("coily") / 2
            return
        if e.kind in DOWNHILL:
            dr, dc = DELTAS[self.rng.choice((DL, DR))]
        elif e.kind == "coily":
            d = self.coily_dir(e)
            if d is None:
                e.wait = self.interval("coily")
                return
            dr, dc = DELTAS[d]
        else:
            dr, dc = self.rng.choice(SIDE_MOVES[e.kind])
        e.fr, e.fc = e.r, e.c
        e.r, e.c = e.r + dr, e.c + dc
        e.t = e.dur = ENEMY_HOP

    def coily_dir(self, e: Enemy):
        """Coily's next hop: straight at Q*bert, or at the disc Q*bert rode
        away on (the one spot off the pyramid he will follow him to)."""
        target = tuple(self.lure) if self.lure else (self.qr, self.qc)
        best, best_key = None, None
        for d in COILY_ORDER:
            n = neighbour(e.r, e.c, d)
            if not (valid(*n) or (self.lure is not None and list(n) == self.lure)):
                continue
            nx, tx = 2 * n[1] - n[0], 2 * target[1] - target[0]
            key = (hops_between(n, target), abs(tx - nx) + abs(target[0] - n[0]))
            if best_key is None or key < best_key:
                best, best_key = d, key
        return best

    def _check_collisions(self) -> None:
        if not self.alive:
            return
        q = self.qbert_cell()
        if q is None:
            return
        for e in list(self.enemies):
            hit = e.cell() == q
            if (not hit and self.q_t > 0 and e.t > 0 and e.fr is not None
                    and self.q_from is not None
                    and self.q_from == [e.r, e.c] and [self.qr, self.qc] == [e.fr, e.fc]):
                hit = True  # hopped through each other
            if not hit:
                continue
            if e.kind == "green":
                self.enemies.remove(e)
                self.freeze_t = FREEZE_TIME
                self._add_score(GREEN_POINTS)
                self._say(f"freeze!  +{GREEN_POINTS}")
            elif e.kind in ("slick", "sam"):
                self.enemies.remove(e)
                self._add_score(CATCH_POINTS)
                self._say(f"caught {e.kind.capitalize()}!  +{CATCH_POINTS}")
            else:
                self._die(e.kind)
                return

    # -- time --------------------------------------------------------------

    def _tick(self, dt: float) -> None:
        self.clock += dt           # drives the disc spin, also during phases
        if self.phase is not None:
            self.phase.elapsed += dt
            if self.phase.kind == "disc":
                self._update_enemies(dt)
            if self.phase.elapsed >= self.phase.duration:
                self.finish_phase()
                self._check_collisions()
            return
        if self.q_t > 0:
            self.q_t -= dt
            if self.q_t <= 0:
                self._land_qbert()
                if self.phase is not None:
                    return
        if self.q_t <= 0 and self.queue:
            self._start_hop(self.queue.pop(0))
        if self.freeze_t <= 0:
            self._spawn_tick(dt)
        self._update_enemies(dt)
        self._check_collisions()

    def update(self, dt: float) -> bool:
        """Advance the game by dt seconds. True if anything changed."""
        if self.paused or self.game_over:
            return False
        dt = min(max(dt, 0.0), MAX_DT)
        self.note_t = max(0.0, self.note_t - dt)
        while dt > 1e-9 and not self.game_over:
            step = min(dt, MAX_STEP)
            self._tick(step)
            dt -= step
        return True

    # -- save / restore ----------------------------------------------------

    def to_save(self) -> dict:
        """Serializable snapshot. Taken with no phase pending: the caller
        runs resolve_phases() first (the UI save path does)."""
        return {
            "version": SAVE_VERSION,
            "score": self.score,
            "lives": self.lives,
            "level": self.level,
            "round": self.round,
            "clock": self.clock,
            "next_extra": self.next_extra,
            "cubes": [list(row) for row in self.cubes],
            "discs": [list(d) for d in self.discs],
            "q": [self.qr, self.qc],
            "q_from": list(self.q_from) if self.q_from is not None else None,
            "q_t": self.q_t,
            "facing": self.facing,
            "queue": list(self.queue),
            "enemies": [e.to_list() for e in self.enemies],
            "spawn_t": self.spawn_t,
            "spawned": self.spawned,
            "freeze_t": self.freeze_t,
            "lure": list(self.lure) if self.lure is not None else None,
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
                if math.isnan(v) or not (lo <= v <= hi):
                    raise ValueError
                return v

            def whole(v, lo, hi):
                if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                    raise ValueError
                return v

            def spot(v, allow_none=False):
                if v is None and allow_none:
                    return None
                if not isinstance(v, list) or len(v) != 2:
                    raise ValueError
                return [whole(v[0], -2, ROWS + 1), whole(v[1], -2, ROWS + 1)]

            level = whole(data["level"], 1, 10 ** 6)
            top = stages_for(level) - 1
            cubes = data["cubes"]
            if (not isinstance(cubes, list) or len(cubes) != ROWS
                    or any(not isinstance(row, list) or len(row) != r + 1
                           for r, row in enumerate(cubes))):
                return None
            cubes = [[whole(s, 0, top) for s in row] for row in cubes]
            if all(s == top for row in cubes for s in row):
                return None  # a cleared round is never saved
            discs = data["discs"]
            if not isinstance(discs, list) or len(discs) > 4:
                return None
            discs = [spot(d) for d in discs]
            legal = [[q, -1] for q in DISC_ROWS] + [[q, q + 1] for q in DISC_ROWS]
            if any(d not in legal for d in discs):
                return None
            q = spot(data["q"])
            q_from = spot(data["q_from"], allow_none=True)
            q_t = num(data["q_t"], 0, QB_HOP)
            if (q_t > 0) != (q_from is not None):
                return None
            if q_from is None:
                if not valid(*q):
                    return None
            elif not valid(*q_from) or tuple(q) not in [
                    neighbour(q_from[0], q_from[1], d) for d in DIRS]:
                return None  # mid-hop: from a cube to a neighbouring spot
            if data["facing"] not in DIRS:
                return None
            queue = data["queue"]
            if (not isinstance(queue, list) or len(queue) > QUEUE_MAX
                    or any(d not in DIRS for d in queue)):
                return None
            enemies = []
            for item in data["enemies"]:
                if not isinstance(item, list) or len(item) != 8 or item[0] not in KINDS:
                    return None
                kind, r, c, fr, fc, t, dur, wait = item
                pos = spot([r, c])
                prev = spot([fr, fc]) if fr is not None or fc is not None else None
                enemies.append(Enemy(kind, pos[0], pos[1],
                                     prev[0] if prev else None,
                                     prev[1] if prev else None,
                                     num(t, 0, 10), num(dur, 0, 10), num(wait, -1, 100)))
            if len(enemies) > 8:
                return None
            lure = spot(data["lure"], allow_none=True)
            state = _rng_from_json(data.get("rng"))
            if state is None:
                return None
            game.score = whole(data["score"], 0, 10 ** 12)
            game.lives = whole(data["lives"], 1, MAX_LIVES)
            game.level = level
            game.round = whole(data["round"], 1, ROUNDS_PER_LEVEL)
            game.clock = num(data["clock"], 0, 1e12)
            game.next_extra = whole(data["next_extra"], EXTRA_LIFE_FIRST, 10 ** 13)
            game.cubes = cubes
            game.discs = discs
            game.qr, game.qc = q
            game.q_from = q_from
            game.q_t = q_t
            game.facing = data["facing"]
            game.queue = list(queue)
            game.enemies = enemies
            game.spawn_t = num(data["spawn_t"], -1, 100)
            game.spawned = whole(data["spawned"], 0, 10 ** 9)
            game.freeze_t = num(data["freeze_t"], 0, FREEZE_TIME)
            game.lure = lure
            game.note, game.note_t = "", 0.0
            game.rng.setstate(state)
            return game
        except (KeyError, TypeError, ValueError, IndexError, OverflowError):
            return None


TITLE = "Q*bert"

# Every key the game reacts to (upper-case letters work too). The two
# diagram rows draw the four diagonal hops as an X: each key sits at the
# end of the stroke it hops along.
CONTROLS = [
    ("arrows", "turned 45 deg:"),
    ("left y \\", "/ u up"),
    ("down b /", "\\ n right"),
    ("wasd hjkl", "same as the arrows"),
    ("y u b n", "diagonals as drawn"),
    ("?", "help"),
    ("p", "pause"),
    ("r", "restart"),
    ("q", "menu"),
]

HELP = [
    ("Goal",
     "Hop Q*bert around a pyramid of 28 cubes. Every cube top starts in one "
     "colour; landing on it changes it toward the target colour (shown in "
     "the stats box). Turn every cube to the target to clear the round. "
     "Four rounds make a level; each round is faster."),
    ("Controls",
     "Q*bert only hops diagonally, one hop per keypress. The arrow keys are "
     "turned 45 degrees clockwise:\n"
     "- up arrow: hop up-right\n"
     "- right arrow: hop down-right\n"
     "- down arrow: hop down-left\n"
     "- left arrow: hop up-left\n"
     "- w a s d and k h j l work exactly like up left down right.\n"
     "- y u b n are the diagonals as they sit on the keyboard: y up-left, "
     "u up-right, b down-left, n down-right.\n"
     "- The keys box draws this as an X: each key sits at the end of the "
     "stroke it hops along (left y \\ is up-left, / u up is up-right, "
     "down b / is down-left, \\ n right is down-right).\n"
     "- A key pressed while Q*bert is in the air is remembered (up to two) "
     "and hopped next, so you can type a path ahead.\n"
     "- p pauses, r starts a new game, q goes back to the arcade menu.\n"
     "- ? opens this help (the game pauses while it is open)."),
    ("Levels and colours",
     "- Level 1: one hop turns a cube to the target.\n"
     "- Level 2: two hops per cube, through an intermediate colour.\n"
     "- Level 3: one hop sets a cube, but landing on a finished cube flips "
     "it back to the start colour.\n"
     "- Level 4 and on: two hops per cube, and landing on a finished cube "
     "drops it back to the intermediate colour.\n"
     "Each round uses new colours. Clearing a round flashes the pyramid."),
    ("Discs",
     "Spinning discs <==> float beside the pyramid. Hop off the edge onto "
     "one and it carries Q*bert back to the top cube; everything on the "
     "pyramid except Coily leaves. A disc works once. If Coily is chasing "
     "you, he follows you off the edge and falls: a 500 point lure."),
    ("Enemies",
     "- Red balls () drop onto the second row and bounce down to the "
     "bottom and off.\n"
     "- The purple ball bounces to the bottom row and hatches into Coily, "
     "a snake (SS) who hops straight at you, in any direction.\n"
     "- Green ball (): catch it to freeze every enemy for 4 seconds "
     "(frozen enemies still hurt).\n"
     "- Slick and Sam (88) bounce down and turn cubes back one colour. "
     "Catch them by touching them.\n"
     "- From level 2, Ugg (UG) and Wrongway (WW) climb along the sides of "
     "the cubes from the bottom corners. They kill if you land on the cube "
     "they cling to."),
    ("What kills you",
     "- hopping off the pyramid where there is no disc\n"
     "- touching a red or purple ball, Coily, Ugg or Wrongway\n"
     "You have 4 lives; losing one clears the pyramid of enemies. After a "
     "fall Q*bert restarts on the top cube."),
    ("Scoring",
     "- 25 for every colour change toward the target\n"
     "- 100 for catching the green ball, 300 for Slick or Sam\n"
     "- 500 for luring Coily off the edge with a disc\n"
     "- round clear: 1000, plus 250 for every round before it (up to "
     "5000), plus 50 x level for every unused disc\n"
     "- extra life at 8000, then every 14000"),
    ("Legend",
     "- Q> <Q  Q*bert (facing right / left)\n"
     "- solid bar on a cube: its top, in its current colour\n"
     "- () red, purple or green ball     SS Coily     88 Slick and Sam\n"
     "- UG Ugg and WW Wrongway, on the cube sides\n"
     "- <==> a spinning disc beside the pyramid\n"
     "- dim enemies are frozen"),
    ("Tips",
     "- Clear the bottom rows while the balls are still up top.\n"
     "- Coily is fast but predictable: lead him to an edge with a disc, "
     "then hop on it.\n"
     "- Keep a disc for Coily rather than using both early; unused discs "
     "pay a bonus at the end of the round.\n"
     "- In level 3, plan a route that visits each cube once."),
]

# Field geometry: the pyramid's interior is FIELD_IW x FIELD_IH characters.
CUBE_W = 8                # one cube, drawn 8 columns wide
HALF = CUBE_W // 2
PITCH = 3                 # rows per pyramid row: top tile + two face rows
FIELD_IW = CUBE_W * ROWS              # 56: the bottom row, edge to edge
FIELD_IH = PITCH * ROWS + 1           # 22: sprites stand one row above row 0
CX = FIELD_IW // 2


def tile_left(r: int, c: int) -> int:
    """Interior column of the left edge of cube (r, c)'s slot (also used
    for off-pyramid spots: discs, falls)."""
    return CX + (2 * c - r) * HALF - HALF


def tile_top(r: int) -> int:
    """Interior row of cube row r's top tile."""
    return 1 + PITCH * r


# Layout: the field window hugs the pyramid. Next to it, when there is room,
# a side column of stacked boxes: stats, controls, agents. Row 0 is the
# header/banner; everything else is centred as one block below it.
FIELD_W = FIELD_IW + 2     # 58
FIELD_H = FIELD_IH + 2     # 24
STATS_H = 11
AGENTS_MIN_H = 3
AGENTS_PREF_H = 8          # the side column grows to this much roster
MIN_COLS = FIELD_W
MIN_ROWS = 1 + FIELD_H     # header + field


def hint_text(width: int) -> str:
    """Compact key hint for tiers without a controls box; always leads
    with '? help' so it survives truncation."""
    parts = [("?", "help"), ("arrows", "turned 45: up=up-right"),
             ("y u b n", "diagonals"), ("p", "pause"), ("r", "restart"), ("q", "menu")]
    return (" " + controls_hint(parts))[: max(0, width - 1)]


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

# Colour pairs 40-46 (inside the shared 40-59 game range, see common.py):
# one per palette colour, used for tiles and characters alike.
PAIR_BASE = 40
CURSES_COLOURS = {
    "blue": curses.COLOR_BLUE, "yellow": curses.COLOR_YELLOW,
    "cyan": curses.COLOR_CYAN, "green": curses.COLOR_GREEN,
    "magenta": curses.COLOR_MAGENTA, "red": curses.COLOR_RED,
    "white": curses.COLOR_WHITE,
}

TILE = "█" * (CUBE_W - 2)      # solid block: the cube top
FACE = "░" * HALF              # light shade: a side face
QBERT_GLYPHS = {UR: "Q>", DR: "Q>", UL: "<Q", DL: "<Q"}
CURSE = "@!#?@!"
ENEMY_GLYPHS = {"red": "()", "purple": "()", "green": "()", "coily": "SS",
                "slick": "88", "sam": "88", "ugg": "UG", "wrongway": "WW"}
ENEMY_COLOURS = {"red": "red", "purple": "magenta", "green": "green",
                 "coily": "magenta", "slick": "green", "sam": "cyan",
                 "ugg": "magenta", "wrongway": "cyan"}
DISC_GLYPHS = ("<==>", ">==<")


def pair(name: str) -> int:
    return curses.color_pair(PAIR_BASE + PALETTE.index(name))


def init_colors() -> None:
    bg = default_bg()
    for i, name in enumerate(PALETTE):
        curses.init_pair(PAIR_BASE + i, CURSES_COLOURS[name], bg)


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


def put(win, iy: int, ix: int, text: str, attr=0) -> None:
    """Draw `text` at field interior (iy, ix), clipped to the interior."""
    if not 0 <= iy < FIELD_IH:
        return
    start = max(0, -ix)
    end = min(len(text), FIELD_IW - ix)
    if start < end:
        safe_addstr(win, 1 + iy, 1 + ix + start, text[start:end], attr)


def sprite_pos(r: int, c: int):
    """Interior (y, x) of a 2-column sprite standing on cube/spot (r, c)."""
    return tile_top(r) - 1, tile_left(r, c) + HALF - 1


def side_pos(kind: str, r: int, c: int):
    """Ugg and Wrongway cling to a cube's right / left face."""
    x = tile_left(r, c) + (HALF + 1 if kind == "ugg" else 1)
    return tile_top(r) + 1, x


def hop_pos(a, b, progress: float, pos=sprite_pos):
    """Sprite position between spots a and b, with a one-row jump arc."""
    ay, ax = pos(*a)
    by, bx = pos(*b)
    y = ay + (by - ay) * progress
    x = ax + (bx - ax) * progress
    arc = 1 if 0.2 < progress < 0.8 else 0
    return int(round(y)) - arc, int(round(x))


def disc_path(game):
    """(y, x) of the disc Q*bert is riding, from its slot up over the top."""
    r, c = game.phase.cause
    p = min(1.0, game.phase.elapsed / game.phase.duration * 1.15)
    y0, x0 = tile_top(r), tile_left(r, c) + 2
    y1, x1 = 0, CX - 2
    return int(round(y0 + (y1 - y0) * p)), int(round(x0 + (x1 - x0) * p))


def draw_pyramid(win, game) -> None:
    flashing = game.phase is not None and game.phase.kind == "clear"
    step = int(game.phase.elapsed * 10) if flashing else 0
    left, right = pair("white"), pair("blue")
    for r in range(ROWS):
        ty = tile_top(r)
        for c in range(r + 1):
            x = tile_left(r, c)
            if flashing:
                colour = PALETTE[(step + r + c) % len(PALETTE)]
            else:
                colour = game.cube_colour(game.cubes[r][c])
            put(win, ty, x + 1, TILE, pair(colour) | curses.A_BOLD)
            for dy in (1, 2):
                put(win, ty + dy, x, FACE, left)
                put(win, ty + dy, x + HALF, FACE, right)


def draw_discs(win, game) -> None:
    spin = int(game.clock * 6)
    for i, (r, c) in enumerate(game.discs):
        colour = PALETTE[(spin + i) % len(PALETTE)]
        put(win, tile_top(r), tile_left(r, c) + 2, DISC_GLYPHS[spin % 2],
            pair(colour) | curses.A_BOLD)


def draw_enemies(win, game) -> None:
    frozen = game.freeze_t > 0
    for e in game.enemies:
        glyph = ENEMY_GLYPHS[e.kind]
        attr = pair(ENEMY_COLOURS[e.kind])
        attr |= curses.A_DIM if frozen else curses.A_BOLD
        side = e.kind in SIDE_MOVES
        pos = (lambda r, c, k=e.kind: side_pos(k, r, c)) if side else sprite_pos
        if e.t > 0 and e.dur > 0:
            p = 1.0 - e.t / e.dur
            if e.fr is None:
                if side:
                    y, x = pos(e.r, e.c)
                else:  # dropping in from above the field
                    ty, x = pos(e.r, e.c)
                    y = int(round(-1 + (ty + 1) * p))
            else:
                y, x = hop_pos((e.fr, e.fc), (e.r, e.c), p, pos)
        else:
            y, x = pos(e.r, e.c)
            if e.kind == "purple" and e.r == ROWS - 1 and int(game.clock * 6) % 2:
                glyph = "<>"  # about to hatch
        put(win, y, x, glyph, attr)


def draw_qbert(win, game) -> None:
    attr = pair("yellow") | curses.A_BOLD
    glyph = QBERT_GLYPHS[game.facing]
    phase = game.phase
    if phase is not None and phase.kind == "clear":
        y, x = sprite_pos(game.qr, game.qc)
        put(win, y, x, glyph, attr)
        return
    if phase is not None and phase.kind == "disc":
        dy, dx = disc_path(game)
        put(win, dy, dx, DISC_GLYPHS[int(game.clock * 6) % 2],
            pair(PALETTE[int(phase.elapsed * 8) % len(PALETTE)]) | curses.A_BOLD)
        put(win, dy - 1, dx + 1, glyph, attr)
        return
    if phase is not None and phase.kind == "dying":
        y, x = sprite_pos(game.qr, game.qc)
        if phase.cause == "fall":
            y += int(phase.elapsed * 14)
            put(win, y, x, glyph, attr)
            return
        if int(phase.elapsed * 6) % 2 == 0:
            put(win, y, x, glyph, attr)
        bx = x + 3 if x + 3 + len(CURSE) <= FIELD_IW else x - 1 - len(CURSE)
        put(win, y, bx, CURSE, pair("white") | curses.A_BOLD | curses.A_REVERSE)
        return
    if game.q_t > 0 and game.q_from is not None:
        y, x = hop_pos(game.q_from, (game.qr, game.qc), 1.0 - game.q_t / QB_HOP)
    else:
        y, x = sprite_pos(game.qr, game.qc)
    put(win, y, x, glyph, attr)


NOTE_W = CX - 3            # the note never reaches the top cube's sprite


def draw_field(win, game) -> None:
    draw_pyramid(win, game)
    draw_discs(win, game)
    if not game.game_over:
        draw_enemies(win, game)
        draw_qbert(win, game)
    if game.note_t > 0 and game.note:
        # Top-left corner: clear of the pyramid and of Q*bert on the top cube.
        put(win, 0, 0, game.note[:NOTE_W], curses.A_BOLD)


def _overlay(win, lines):
    rows, cols = win.getmaxyx()
    y = rows // 2 - len(lines) // 2
    for i, (text, attr) in enumerate(lines):
        safe_addstr(win, y + i, max(1, (cols - len(text)) // 2), text[: cols - 2], attr)


def draw_stats(win, game, best: int) -> None:
    w = win.getmaxyx()[1] - 2
    safe_addstr(win, 0, 2, f" {TITLE} "[: max(0, w - 1)], curses.A_BOLD)
    lines = [
        f"score {game.score}",
        f"best  {max(best, game.score)}",
        f"level {game.level}  round {game.round}",
        ("lives " + " ".join(["Q>"] * min(game.lives, 6))).rstrip(),
    ]
    for i, line in enumerate(lines):
        safe_addstr(win, 1 + i, 1, line[:w])
    # The colour path: start (> intermediate) > target, as real tiles.
    start, mid, target = game.scheme
    path = [start, mid, target] if game.stages == 3 else [start, target]
    x = 1
    safe_addstr(win, 5, x, "tiles"[:w])
    x += 6
    for i, name in enumerate(path):
        if x + 2 > w + 1:
            break
        safe_addstr(win, 5, x, "██", pair(name) | curses.A_BOLD)
        x += 2
        if i < len(path) - 1 and x + 3 <= w + 1:
            safe_addstr(win, 5, x, " > ")
            x += 3
    info = [
        RULE_TEXT[game.rule],
        f"left  {game.remaining()} of {N_CUBES} cubes",
        f"discs {len(game.discs)}",
    ]
    if game.freeze_t > 0:
        info.append(f"FROZEN {int(math.ceil(game.freeze_t))}s")
    elif any(e.kind == "coily" for e in game.enemies):
        info.append("Coily is out!")
    for i, line in enumerate(info):
        safe_addstr(win, 6 + i, 1, line[:w],
                    curses.A_BOLD if i == 3 else 0)


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
        head = f" q*bert - {state} "
        if lay.plan["hint"] is None and lay.controls is None:
            head += "| ? help "  # no hint row and no controls box
        if lay.stats is None:
            head += (f"| score {game.score} | lives {game.lives}"
                     f" | lvl {game.level}-{game.round} | left {game.remaining()} ")
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
    elif game.phase is not None and game.phase.kind == "clear":
        _overlay(fw, [(f" ROUND {game.round} CLEAR ", curses.A_STANDOUT | curses.A_BOLD)])

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


# Arrows turned 45 degrees clockwise; wasd and hjkl follow the arrows; y u
# b n are the diagonals where they sit on the keyboard.
KEYS = {
    curses.KEY_UP: UR, ord("w"): UR, ord("k"): UR, ord("u"): UR,
    curses.KEY_RIGHT: DR, ord("d"): DR, ord("l"): DR, ord("n"): DR,
    curses.KEY_DOWN: DL, ord("s"): DL, ord("j"): DL, ord("b"): DL,
    curses.KEY_LEFT: UL, ord("a"): UL, ord("h"): UL, ord("y"): UL,
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
    d = KEYS.get(ch)
    if d is None:
        return False
    return game.press(d)


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
    """Play Q*bert until `q` (back to the arcade menu) or SIGTERM. The game
    is saved on every exit path and resumed, paused, on the next run."""
    stdscr = session.stdscr
    init_colors()
    game_path = session.path("qbert-game.json")
    hiscore_path = session.path("qbert-hiscore.json")
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

            # Drain every pending key, so a hop typed during the hop
            # animation is queued at once rather than a tick later.
            quit_now = False
            while True:
                ch = stdscr.getch()
                if ch == -1:
                    break
                if ch == curses.KEY_RESIZE:
                    curses.update_lines_cols()
                    stdscr.erase()
                    lay = Layout(stdscr)
                    dirty = True
                    continue
                result = dispatch_key(ch, game, help, st, session)
                if result == "quit":
                    quit_now = True
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
            if quit_now:
                break

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
        # The one save point: resolve a pending ride/death/round-clear first
        # so the saved state is the one the animation would have led to.
        game.resolve_phases()
        if game_path:
            if game.game_over:
                remove_path(game_path)
            else:
                atomic_save_json(game_path, game.to_save())
        if game.score > hiscore:
            save_hiscore(hiscore_path, game.score)


if __name__ == "__main__":
    import arcade

    sys.exit(arcade.main(["--game", "qbert"] + sys.argv[1:]))
