#!/usr/bin/env python3
"""Herdr arcade: a splash menu of terminal games in a Herdr pane, aware of
the session's coding agents.

`arcade.py` owns the process: curses setup, the one agent poller, SIGTERM,
and the popup sentinel the pane actions use (all via common.Session). It
shows the menu, or starts straight into a game with `--game ID` (what the
per-game plugin actions and key bindings open). Each game module exposes
`run(session)`; `q` in a game comes back here, `q` in the menu quits.
"""

from __future__ import annotations

import argparse
import curses
import importlib
import os
import sys
import time

from common import (
    DEFAULT_POLL_INTERVAL,
    HELP_KEY,
    HelpOverlay,
    Session,
    center_origin,
    default_bg,
    draw_agents,
    draw_banner,
    frame,
    init_status_colors,
    load_hiscore,
    plugin_state_dir,
    safe_addstr,
    side_width,
)

# id, title, module, one-line blurb, arcade release year; listed oldest
# first. The id names the plugin action, the pane entrypoint, `--game` and
# the `<id>-game.json` / `<id>-hiscore.json` files. Years are the original
# arcade (or first) release: Snake after Gremlin's Blockade, Lode Runner and
# Tetris after their first (home computer) releases.
GAMES = (
    ("breakout", "Breakout", "breakout", "smash the wall, keep the ball up", 1976),
    ("snake", "Snake", "snake", "eat, grow, don't bite yourself", 1976),
    ("invaders", "Space Invaders", "invaders", "hold the line against the descending swarm", 1978),
    ("pacman", "Pac-Man", "pacman", "eat the dots, dodge the ghosts", 1980),
    ("centipede", "Centipede", "centipede", "blast the centipede through the mushrooms", 1981),
    ("frogger", "Frogger", "frogger", "cross the road and the river, get home", 1981),
    ("qix", "Qix", "qix", "claim 75% of the field, dodge the Qix", 1981),
    ("pengo", "Pengo", "pengo", "slide ice blocks, crush the Sno-Bees", 1982),
    ("tron", "Tron", "tron", "light cycles: box them in, don't crash", 1982),
    ("loderunner", "Lode Runner", "loderunner", "grab the gold, dig traps for the guards", 1983),
    ("tetris", "Tetris", "tetris", "stack falling blocks, clear lines", 1984),
)
GAME_IDS = tuple(g[0] for g in GAMES)

TITLE = (
    "  __ _ _ __ ___ __ _  __| | ___ ",
    " / _` | '__/ __/ _` |/ _` |/ _ \\",
    "| (_| | | | (_| (_| | (_| |  __/",
    " \\__,_|_|  \\___\\__,_|\\__,_|\\___|",
)
TITLE_W = max(len(line) for line in TITLE)

LIST_W = 46
HELP = "^ v select | enter play | 1-9 0 a.. pick | ? help | q quit"


def menu_layout(cols: int, rows: int, n_games: int = len(GAMES)) -> dict:
    """Pure layout for the menu: each region's (y, x, h, w), or None when it
    does not fit. The block (title over list + agents) is centred between
    the header row and the help row; the title and agents box drop away on
    small panes, and the game list scrolls when not every game fits."""
    full_h = n_games + 4  # frame, one line per game, a spacer, the blurb
    list_h = min(full_h, rows - 3)  # header row above, help row below
    list_w = min(LIST_W, cols - 2)
    plan = {"too_small": cols < 30 or list_h < 6,
            "title": None, "list": None, "agents": None, "help": None}
    if plan["too_small"]:
        return plan
    agents_w = side_width(cols - 2, list_w)
    content_w = list_w + (1 + agents_w if agents_w else 0)
    show_title = rows >= len(TITLE) + 1 + full_h + 3 and cols >= TITLE_W + 2
    block_h = list_h + (len(TITLE) + 1 if show_title else 0)
    y, x0 = center_origin(cols, rows - 1, content_w, block_h)
    if show_title:
        plan["title"] = (y, (cols - TITLE_W) // 2, len(TITLE), TITLE_W)
        y += len(TITLE) + 1
    plan["list"] = (y, x0, list_h, list_w)
    if agents_w:
        plan["agents"] = (y, x0 + list_w + 1, list_h, agents_w)
    plan["help"] = (rows - 1, 0, 1, cols)
    return plan


def scroll_offset(selected: int, n_games: int, visible: int) -> int:
    """First game row shown so `selected` stays in view, centred when the
    list scrolls."""
    if n_games <= visible:
        return 0
    return max(0, min(selected - visible // 2, n_games - visible))


# Direct-pick keys, in menu order: digits, then letters the menu does not
# already use (j k w s move, l plays, q quits).
MENU_KEYS = "wsjklq"
HOTKEYS = "1234567890" + "".join(
    c for c in "abcdefghijklmnopqrstuvwxyz" if c not in MENU_KEYS)


def hotkey_label(i: int) -> str:
    """The key that picks game i directly, or a blank past the last one."""
    return HOTKEYS[i] if i < len(HOTKEYS) else " "


def hotkey_index(ch: int, n_games: int = len(GAMES)):
    """Game index a pressed key picks, or None."""
    if 0 <= ch < 256:
        i = HOTKEYS.find(chr(ch))
        if 0 <= i < n_games:
            return i
    return None


def saved_game_exists(session, game_id) -> bool:
    path = session.path(f"{game_id}-game.json")
    return bool(path) and os.path.exists(path)


def draw_menu(session, selected: int, lay: dict, cols: int, rows: int) -> None:
    scr = session.stdscr
    scr.erase()
    if lay["too_small"]:
        msg = f"pane too small ({cols}x{rows})"
        safe_addstr(scr, rows // 2, max(0, (cols - len(msg)) // 2), msg, curses.A_BOLD)
        return

    if not draw_banner(scr, cols, session.attention):
        safe_addstr(scr, 0, 0, " herdr arcade "[: cols - 1], curses.A_STANDOUT)

    if lay["title"]:
        y, x, _, _ = lay["title"]
        for i, line in enumerate(TITLE):
            safe_addstr(scr, y + i, x, line, curses.color_pair(MENU_PAIR) | curses.A_BOLD)

    y, x, h, w = lay["list"]
    win = scr.derwin(h, w, y, x)
    frame(win)
    safe_addstr(win, 0, 2, " games ", curses.A_BOLD)
    # Column heads sit over the row format below: title from x 5, year at
    # x 21-24, best right-aligned ending at x 33.
    safe_addstr(win, 0, 20, " year ", curses.A_DIM)
    safe_addstr(win, 0, 29, " best ", curses.A_DIM)
    visible = h - 4
    top = scroll_offset(selected, len(GAMES), visible)
    for row, i in enumerate(range(top, min(top + visible, len(GAMES)))):
        gid, title, year = GAMES[i][0], GAMES[i][1], GAMES[i][4]
        attr = curses.A_REVERSE | curses.A_BOLD if i == selected else 0
        best = load_hiscore(session.path(f"{gid}-hiscore.json"))
        tag = " saved" if saved_game_exists(session, gid) else ""
        line = f" {hotkey_label(i)}  {title:<15} {year}  {best:>7}{tag}"
        safe_addstr(win, 1 + row, 1, line.ljust(w - 2)[: w - 2], attr)
    if top > 0:
        safe_addstr(win, 1, w - 2, "^", curses.A_DIM)
    if top + visible < len(GAMES):
        safe_addstr(win, visible, w - 2, "v", curses.A_DIM)
    safe_addstr(win, h - 2, 2, GAMES[selected][3][: w - 4], curses.A_DIM)

    if lay["agents"]:
        y, x, h, w = lay["agents"]
        aw = scr.derwin(h, w, y, x)
        frame(aw)
        draw_agents(aw, session.roster, session.status_pairs)

    if lay["help"]:
        y, _, _, _ = lay["help"]
        safe_addstr(scr, y, max(0, (cols - len(HELP)) // 2), HELP[: cols - 1], curses.A_DIM)


MENU_PAIR = 30  # see the colour-pair numbering note in common.py


def game_help(index: int) -> HelpOverlay:
    """The `?` page for a menu entry: the game module's own HELP sections
    (the same page `?` shows in-game), headed by its year and blurb."""
    gid, title, module, blurb, year = GAMES[index]
    mod = importlib.import_module(module)
    sections = [(f"{title} ({year})", blurb[0].upper() + blurb[1:] + ".")]
    sections += list(getattr(mod, "HELP", []))
    return HelpOverlay(title, sections)


def run_game(session, game_id: str) -> None:
    module = GAMES[GAME_IDS.index(game_id)][2]
    session.set_screen(game_id)
    session.stdscr.erase()
    importlib.import_module(module).run(session)
    session.set_screen("menu")


def menu(session, selected: int = 0) -> None:
    """The splash screen; returns when the user quits or SIGTERM arrives."""
    scr = session.stdscr
    curses.init_pair(MENU_PAIR, curses.COLOR_YELLOW, default_bg())
    rows, cols = scr.getmaxyx()
    lay = menu_layout(cols, rows)
    help_view = None
    dirty = True
    while not session.quitting.is_set():
        changed, _ = session.pump()
        dirty = dirty or changed
        ch = scr.getch()
        play = None
        if ch == -1:
            pass
        elif ch == curses.KEY_RESIZE:
            curses.update_lines_cols()
            rows, cols = scr.getmaxyx()
            lay = menu_layout(cols, rows)
            dirty = True
        elif help_view is not None and help_view.is_open:
            help_view.handle_key(ch)
            dirty = True
        elif session.attention is not None:
            session.dismiss()  # any key clears the alert banner
            dirty = True
        elif ch == HELP_KEY:
            help_view = game_help(selected)
            help_view.open()
            dirty = True
        elif ch in (ord("q"), ord("Q")):
            return
        elif ch in (curses.KEY_UP, ord("k"), ord("w")):
            selected = (selected - 1) % len(GAMES)
            dirty = True
        elif ch in (curses.KEY_DOWN, ord("j"), ord("s")):
            selected = (selected + 1) % len(GAMES)
            dirty = True
        elif ch in (curses.KEY_ENTER, 10, 13, ord(" "), curses.KEY_RIGHT, ord("l")):
            play = selected
        elif hotkey_index(ch) is not None:
            play = selected = hotkey_index(ch)
        if play is not None:
            run_game(session, GAME_IDS[play])
            rows, cols = scr.getmaxyx()
            lay = menu_layout(cols, rows)
            dirty = True
            continue
        if dirty:
            draw_menu(session, selected, lay, cols, rows)
            if help_view is not None:
                help_view.draw(scr)
            scr.noutrefresh()
            curses.doupdate()
            dirty = False
        time.sleep(0.01)


def app(stdscr, args) -> None:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.nodelay(True)
    stdscr.keypad(True)
    curses.start_color()
    session = Session(stdscr, args.no_auto_pause, args.poll_interval,
                      plugin_state_dir())
    session.status_pairs = init_status_colors()
    session.start()
    try:
        selected = 0
        if args.game:
            selected = GAME_IDS.index(args.game)
            run_game(session, args.game)
        if not session.quitting.is_set():
            menu(session, selected)
    finally:
        session.stop()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Terminal arcade for a Herdr pane, with agent awareness."
    )
    parser.add_argument("--game", choices=GAME_IDS,
                        help="start straight into this game instead of the menu")
    parser.add_argument(
        "--no-auto-pause",
        action="store_true",
        help="show the agent banner and bell without pausing the game",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        help=f"seconds between herdr agent list polls (default: {DEFAULT_POLL_INTERVAL})",
    )
    args = parser.parse_args(argv)
    curses.wrapper(app, args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
