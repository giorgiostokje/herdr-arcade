"""Shared plumbing for the arcade games: agent awareness, plugin state files,
and small curses helpers.

The agent section above the curses divider is pure logic (unit-tested without
a terminal). `Session` is what arcade.py hands every game's `run()`: the
screen, the plugin state dir, the live agent roster and the pending
attention banner, plus the SIGTERM flag every game loop must honour.
"""

from __future__ import annotations

import curses
import json
import os
import queue
import shutil
import signal
import subprocess
import threading
import time

# --------------------------------------------------------------------------
# Agent awareness (polling is I/O; tracking is pure)
# --------------------------------------------------------------------------

AGENT_STATUSES = ("idle", "working", "blocked", "done", "unknown")
ATTENTION_STATES = ("blocked", "done")
DEFAULT_POLL_INTERVAL = 2.0


class AgentSnapshot:
    """One normalized entry of `herdr agent list`."""

    __slots__ = ("key", "label", "kind", "status")

    def __init__(self, key: str, label: str, kind: str, status: str):
        self.key = key
        self.label = label
        self.kind = kind
        self.status = status


def normalize_agent_list(raw) -> list[AgentSnapshot]:
    """Parse the JSON document returned by `herdr agent list` into roster
    entries. Tolerates a missing/odd envelope by returning []."""
    if not isinstance(raw, dict):
        return []
    agents = ((raw.get("result") or {}).get("agents")) or []
    out: list[AgentSnapshot] = []
    for i, entry in enumerate(agents):
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("agent") or "?")
        pane = str(entry.get("pane_id") or "")
        name = entry.get("name")
        status = str(entry.get("agent_status") or "unknown")
        if status not in AGENT_STATUSES:
            status = "unknown"
        key = pane or (str(name) if name else f"{kind}#{i}")
        if name:
            label = f"{name} ({kind})"
        elif pane:
            label = f"{kind} {pane}"
        else:
            label = kind
        out.append(AgentSnapshot(key, label, kind, status))
    return out


class AgentTracker:
    """Emits an attention event whenever an agent's status *becomes* blocked
    or done.

    - The same agent staying in an attention state never re-triggers.
    - It re-triggers only after its status changes away (or the agent
      vanishes and later returns).
    - A first sighting already blocked/done counts as a transition: the agent
      needs attention whether or not anyone was watching before.
    """

    def __init__(self):
        self._last: dict[str, str] = {}

    def update(self, agents: list[AgentSnapshot]) -> list[tuple[AgentSnapshot, str | None]]:
        """Feed one roster snapshot; returns (agent, previous_status) pairs for
        every fresh transition into blocked/done."""
        events: list[tuple[AgentSnapshot, str | None]] = []
        for agent in agents:
            prev = self._last.get(agent.key)
            if agent.status in ATTENTION_STATES and prev != agent.status:
                events.append((agent, prev))
            self._last[agent.key] = agent.status
        live = {a.key for a in agents}
        for key in [k for k in self._last if k not in live]:
            del self._last[key]
        return events


def fetch_agent_list(binary: str, timeout: float = 1.5):
    """Run `herdr agent list`; parsed JSON dict, or None on any failure (the
    games must keep running outside Herdr or while the CLI misbehaves)."""
    try:
        proc = subprocess.run(
            [binary, "agent", "list"], capture_output=True, timeout=timeout
        )
        if proc.returncode != 0:
            return None
        return json.loads(proc.stdout.decode("utf-8", "replace"))
    except Exception:
        return None


def poll_agents(
    out: "queue.Queue[tuple[list[AgentSnapshot] | None, list]]",
    interval: float = DEFAULT_POLL_INTERVAL,
    stop: threading.Event | None = None,
) -> None:
    """Thread body: poll `herdr agent list` every `interval` seconds and push
    (roster, attention_events) tuples. A failed poll pushes (None, []) so the
    UI keeps the last known roster. Runs forever; daemon thread."""
    tracker = AgentTracker()
    while stop is None or not stop.is_set():
        binary = os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")
        if binary:
            raw = fetch_agent_list(binary)
            if raw is None:
                out.put((None, []))
            else:
                roster = normalize_agent_list(raw)
                out.put((roster, tracker.update(roster)))
        else:
            out.put(([], []))
        if stop is not None:
            stop.wait(interval)
        else:
            time.sleep(interval)


# --------------------------------------------------------------------------
# Plugin state files
# --------------------------------------------------------------------------

PLUGIN_ID = "stokje-dev.arcade"


def plugin_state_dir():
    """The plugin state dir herdr injects, herdr's XDG layout as fallback,
    or None outside herdr. pane.sh resolves the same paths."""
    state_dir = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if state_dir:
        return state_dir
    if os.environ.get("HERDR_ENV") != "1":
        return None  # plain terminal run: nothing to persist or manage
    plugin_id = os.environ.get("HERDR_PLUGIN_ID", PLUGIN_ID)
    return os.path.expanduser(f"~/.local/state/herdr/plugins/{plugin_id}")


def write_sentinel(state_dir, game="menu"):
    """Record this process's pid and the screen it shows, so the pane
    actions can find, stop or switch the popup. Popup panes are invisible to
    `herdr pane list`, which makes the process the only durable handle.
    Returns the path, or None."""
    if not state_dir:
        return None
    try:
        os.makedirs(state_dir, exist_ok=True)
        path = os.path.join(state_dir, "popup.json")
        atomic_save_json(path, {"pid": os.getpid(), "game": game})
        return path
    except OSError:
        return None


def remove_sentinel(path):
    """Remove our registration, but never a newer instance's: a second
    process may have overwritten the sentinel since we wrote it."""
    try:
        with open(path) as f:
            if json.load(f).get("pid") != os.getpid():
                return
    except (OSError, ValueError):
        pass  # unreadable/absent: try the unlink anyway
    try:
        os.unlink(path)
    except OSError:
        pass


def atomic_save_json(path, data):
    """Write JSON atomically: temp file in the same directory, then rename."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def load_json_file(path):
    """Parsed JSON from `path`, or None for any failure/corruption."""
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def remove_path(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def load_hiscore(path) -> int:
    data = load_json_file(path) if path else None
    if data and isinstance(data.get("hiscore"), int):
        return data["hiscore"]
    return 0


def save_hiscore(path, score: int) -> None:
    if path:
        atomic_save_json(path, {"hiscore": score})


# --------------------------------------------------------------------------
# curses helpers
# --------------------------------------------------------------------------

# Colour-pair numbering: 1-5 are agent statuses (common), 30 is the menu,
# and 10-29 plus 40-59 belong to whichever game is running. Only one screen
# draws at a time and each game registers its pairs when it starts, so every
# game shares that range. Staying under 64 keeps 8-colour terminals working.
STATUS_COLORS = {
    "idle": curses.COLOR_GREEN,
    "working": curses.COLOR_CYAN,
    "blocked": curses.COLOR_RED,
    "done": curses.COLOR_MAGENTA,
    "unknown": curses.COLOR_WHITE,
}
STATUS_PAIR_BASE = 1


def default_bg():
    """Terminal default background (-1), or black where unsupported."""
    try:
        curses.use_default_colors()
        return -1
    except curses.error:
        return curses.COLOR_BLACK


def init_status_colors() -> dict:
    """Register the agent-status pairs; returns {status: pair number}."""
    bg = default_bg()
    pairs = {}
    for i, (status, color) in enumerate(STATUS_COLORS.items()):
        curses.init_pair(STATUS_PAIR_BASE + i, color, bg)
        pairs[status] = STATUS_PAIR_BASE + i
    return pairs


def safe_addstr(win, y, x, text, attr=0):
    """addstr that ignores out-of-window writes instead of raising."""
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass


def frame(win):
    """ASCII frame around a window. Deliberately not curses box(): its
    ACS box-drawing glyphs are ambiguous-width in some renderers, which can
    shift a border off the column it logically occupies."""
    rows, cols = win.getmaxyx()
    bar = "+" + "-" * max(0, cols - 2) + "+"
    safe_addstr(win, 0, 0, bar)
    safe_addstr(win, rows - 1, 0, bar)
    for y in range(1, rows - 1):
        safe_addstr(win, y, 0, "|")
        safe_addstr(win, y, cols - 1, "|")


def draw_agents(win, roster, status_pairs):
    """Fill a framed window with the agent roster (title, one line each)."""
    rows, cols = win.getmaxyx()
    safe_addstr(win, 0, 2, " agents ", curses.A_BOLD)
    inner_w = cols - 2
    if roster is None:
        safe_addstr(win, 2, 1, "no data (herdr?)"[:inner_w], curses.A_DIM)
    elif not roster:
        safe_addstr(win, 2, 1, "none", curses.A_DIM)
    else:
        room = rows - 3  # frame rows plus one for "+N more"
        shown = roster[:room] if len(roster) > rows - 2 else roster
        for i, agent in enumerate(shown):
            attr = curses.color_pair(status_pairs[agent.status])
            if agent.status in ATTENTION_STATES:
                attr |= curses.A_BOLD
            line = f"* {agent.label} {agent.status}"
            safe_addstr(win, 1 + i, 1, line[:inner_w], attr)
        extra = len(roster) - len(shown)
        if extra > 0:
            safe_addstr(win, 1 + len(shown), 1, f"+{extra} more", curses.A_DIM)


def attention_text(attention) -> str:
    agent, prev = attention
    was = f" (was {prev})" if prev else ""
    return f" ! {agent.label} is {agent.status}{was} - press any key "


def draw_banner(scr, cols, attention, resume=False) -> bool:
    """Row-0 banner for a pending agent alert or a restored game. Returns
    True if a banner was drawn (the caller then skips its own header)."""
    if attention is not None:
        text = attention_text(attention)
    elif resume:
        text = " resumed - press any key "
    else:
        return False
    safe_addstr(scr, 0, 0, text[: cols - 1], curses.A_STANDOUT | curses.A_BOLD)
    return True


# --------------------------------------------------------------------------
# Session: what arcade.py hands each game
# --------------------------------------------------------------------------


class Session:
    """Process-wide state shared by the menu and every game.

    One agent poller runs for the whole process, so an agent that went
    blocked while you sat in the menu does not re-alert when a game starts.
    Game loops call `pump()` once per tick, check `quitting` (SIGTERM from
    the close action), and read `roster` / `attention` when drawing.
    """

    def __init__(self, stdscr, no_auto_pause=False,
                 poll_interval=DEFAULT_POLL_INTERVAL, state_dir=None):
        self.stdscr = stdscr
        self.no_auto_pause = no_auto_pause
        self.state_dir = state_dir
        self.roster: list[AgentSnapshot] | None = None
        self.attention = None  # (AgentSnapshot, prev_status) or None
        self.quitting = threading.Event()
        self.status_pairs: dict = {}
        self.sentinel = None
        self._events: "queue.Queue" = queue.Queue()
        self._stop = threading.Event()
        self._poll_interval = poll_interval

    def start(self):
        """Start the poller, trap SIGTERM, register the sentinel."""
        threading.Thread(
            target=poll_agents,
            args=(self._events, self._poll_interval, self._stop),
            daemon=True,
        ).start()
        try:
            signal.signal(signal.SIGTERM, lambda signum, frame: self.quitting.set())
        except ValueError:
            pass  # not the main thread (tests); no signal handling then
        self.sentinel = write_sentinel(self.state_dir)

    def stop(self):
        self._stop.set()
        if self.sentinel:
            remove_sentinel(self.sentinel)

    def set_screen(self, game: str):
        """Tell the pane actions which screen is showing ('menu' or a game
        id), so a per-game action knows whether to close or switch."""
        if self.sentinel:
            write_sentinel(self.state_dir, game)

    def path(self, name: str):
        """A file in the plugin state dir, or None outside herdr."""
        return os.path.join(self.state_dir, name) if self.state_dir else None

    def pump(self):
        """Drain the poller. Returns (changed, new_alerts): changed means the
        roster or banner moved and the screen needs a redraw; new_alerts is
        True when a fresh attention event arrived (the bell has been rung and
        `attention` set — auto-pause is the game's call, via
        `should_auto_pause`)."""
        changed = False
        alerts = False
        try:
            while True:
                roster, events = self._events.get_nowait()
                if roster is not None:
                    self.roster = roster
                    changed = True
                for agent, prev in events:
                    self.attention = (agent, prev)
                    alerts = True
                    curses.beep()  # bell once per transition
        except queue.Empty:
            pass
        return changed or alerts, alerts

    @property
    def should_auto_pause(self) -> bool:
        return not self.no_auto_pause

    def dismiss(self):
        """Clear the attention banner (any key does this)."""
        self.attention = None


# --------------------------------------------------------------------------
# Shared screen furniture: layout constants, controls box, help overlay
# --------------------------------------------------------------------------

# The popup is 96x30, which leaves a 93x28 inner area (3 cols and 2 rows of
# chrome). Every game lays out for that size first and degrades below it.
POPUP_COLS, POPUP_ROWS = 93, 28
SIDE_W = 30      # side column width when there is room for it
SIDE_MIN_W = 20  # narrowest side column worth drawing
HELP_KEY = ord("?")


def center_origin(cols: int, rows: int, width: int, height: int, top: int = 1):
    """(y, x) that centres a width x height block in the screen below the
    `top` reserved rows (the header/banner row)."""
    return (top + max(0, (rows - top - height) // 2),
            max(0, (cols - width) // 2))


def side_width(cols: int, field_w: int, gap: int = 1) -> int:
    """Width for the side column next to a field_w-wide field: SIDE_W when it
    fits, else whatever is left down to SIDE_MIN_W, else 0 (no column)."""
    room = cols - field_w - gap
    if room >= SIDE_W:
        return SIDE_W
    return room if room >= SIDE_MIN_W else 0


def controls_lines(controls, width: int) -> list:
    """Format [(keys, action), ...] as aligned 'keys  action' lines that fit
    `width` columns; overlong actions wrap under their action column."""
    if not controls:
        return []
    key_w = min(max(len(k) for k, _ in controls), max(4, width // 2))
    out = []
    for keys, action in controls:
        room = max(1, width - key_w - 1)
        words, line = action.split(), ""
        first = True
        for word in words:
            if line and len(line) + 1 + len(word) > room:
                out.append((keys if first else "").ljust(key_w)[:key_w] + " " + line)
                first, line = False, word
            else:
                line = f"{line} {word}" if line else word
        out.append((keys if first else "").ljust(key_w)[:key_w] + " " + line)
    return [line[:width] for line in out]


def controls_hint(controls) -> str:
    """One-line key hint for tiers too small for a controls box."""
    return " | ".join(f"{k} {a}" for k, a in controls)


def draw_controls(win, controls, title=" keys "):
    """Framed box listing every control (frame drawn here)."""
    rows, cols = win.getmaxyx()
    frame(win)
    safe_addstr(win, 0, 2, title, curses.A_BOLD)
    for i, line in enumerate(controls_lines(controls, cols - 2)[: rows - 2]):
        safe_addstr(win, 1 + i, 1, line)


def controls_height(controls, width: int) -> int:
    """Rows a framed controls box needs at `width` columns."""
    return len(controls_lines(controls, width - 2)) + 2


def wrap_text(text: str, width: int) -> list:
    """Word-wrap one paragraph (blank lines and leading '- ' bullets kept)."""
    out = []
    for para in text.split("\n"):
        if not para.strip():
            out.append("")
            continue
        indent = "  " if para.startswith("- ") else ""
        line = ""
        for word in para.split():
            cand = f"{line} {word}" if line else word
            if line and len(cand) > width:
                out.append(line)
                line = indent + word
            else:
                line = cand
        out.append(line)
    return out


def help_lines(sections, width: int) -> list:
    """Flatten [(heading, body), ...] into (text, is_heading) lines."""
    lines = []
    for heading, body in sections:
        if lines:
            lines.append(("", False))
        lines.append((heading, True))
        lines.extend((line, False) for line in wrap_text(body, width))
    return lines


class HelpOverlay:
    """The `?` screen: a framed, scrollable page of a game's HELP sections
    drawn over the game. While it is open it takes every key: up/down (j/k)
    and page keys scroll, anything else closes it. Games pause while it is
    open (see the contract in each game's run loop)."""

    MAX_W = 76

    def __init__(self, title: str, sections):
        self.title = title
        self.sections = sections
        self.is_open = False
        self.offset = 0
        self._view = 1  # body rows visible at the last draw
        self._total = 0

    def open(self):
        self.is_open = True
        self.offset = 0

    def close(self):
        self.is_open = False

    def handle_key(self, ch) -> bool:
        """Feed a key while open; returns True if it was consumed."""
        if not self.is_open:
            return False
        if ch == curses.KEY_RESIZE:
            return False  # let the game rebuild its layout
        top = max(0, self._total - self._view)
        if ch in (curses.KEY_DOWN, ord("j"), ord("s")):
            self.offset = min(top, self.offset + 1)
        elif ch in (curses.KEY_UP, ord("k"), ord("w")):
            self.offset = max(0, self.offset - 1)
        elif ch in (curses.KEY_NPAGE, ord(" ")):
            self.offset = min(top, self.offset + self._view)
        elif ch == curses.KEY_PPAGE:
            self.offset = max(0, self.offset - self._view)
        else:
            self.close()
        return True

    def box(self, cols: int, rows: int):
        """(y, x, h, w) of the overlay for a cols x rows screen."""
        w = min(self.MAX_W, cols - 2)
        lines = help_lines(self.sections, w - 4)
        h = min(len(lines) + 4, rows - 1)
        return center_origin(cols, rows, w, h) + (h, w)

    def draw(self, scr):
        if not self.is_open:
            return
        rows, cols = scr.getmaxyx()
        if cols < 24 or rows < 8:
            return
        # Blank everything below the header first: the help is its own page,
        # and no playfield or side box should show around its edges.
        scr.derwin(rows - 1, cols, 1, 0).erase()
        y, x, h, w = self.box(cols, rows)
        lines = help_lines(self.sections, w - 4)
        self._view = max(1, h - 4)
        self._total = len(lines)
        self.offset = min(self.offset, max(0, self._total - self._view))
        win = scr.derwin(h, w, y, x)
        win.erase()
        frame(win)
        safe_addstr(win, 0, 2, f" {self.title} - help "[: w - 4], curses.A_BOLD)
        for i, (text, heading) in enumerate(
                lines[self.offset: self.offset + self._view]):
            attr = curses.A_BOLD | curses.A_UNDERLINE if heading else 0
            safe_addstr(win, 2 + i, 2, text[: w - 4], attr)
        more = self._total > self._view
        foot = " up/down scroll | any key close " if more else " any key closes "
        safe_addstr(win, h - 1, max(1, (w - len(foot)) // 2), foot[: w - 2], curses.A_DIM)
