# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Herdr Arcade (`stokje-dev.arcade`): seventeen terminal arcade games in a
[Herdr](https://herdr.dev) popup pane that pause themselves when a session
agent needs attention. Python, **stdlib only** (`curses`), no build step.
README.md is the user-facing spec (controls, persistence, agent awareness)
and must stay in sync with behaviour changes.

## Commands

```sh
python3 -m unittest discover -s tests            # all tests (run from repo root)
python3 -m unittest tests.test_snake             # one module
python3 -m unittest tests.test_snake.MovementTest # one test class
python3 -m py_compile *.py                       # syntax check
python3 arcade.py [--game ID] [--no-auto-pause]  # play in any terminal, no herdr needed
herdr plugin link "$PWD"                         # register with herdr (unlink: herdr plugin unlink stokje-dev.arcade)
```

Keep code **Python 3.9 compatible**: the herdr pane resolves `python3` to the
system 3.9 even though a newer one may be on your PATH. Modules use
`from __future__ import annotations`; avoid `match`, `X | Y` at runtime, etc.

## Architecture

- `arcade.py` owns the process: curses setup, the menu, `--game ID`, and the
  `GAMES` registry (id, title, module, blurb, year — kept in release-year
  order). Games are imported lazily and entered via `module.run(session)`;
  `q` in a game returns to the menu.
- `common.py` holds everything shared: the `Session` handed to every game
  (one agent poller thread for the whole process, SIGTERM → `quitting`,
  `pump()` once per tick returning `(changed, new_alerts)`,
  `should_auto_pause`, `dismiss()`), agent polling/`AgentTracker` (alerts on
  transitions into `blocked`/`done`), state files (`atomic_save_json`,
  hiscores, the popup sentinel), and screen furniture (`center_origin`,
  `side_width`, controls box, `HelpOverlay`, `draw_banner`, `draw_agents`,
  `safe_addstr`).
- Each game module follows the same shape: **pure, terminal-free logic at the
  top** (a `Game` class with serialise/deserialise guarded by `SAVE_VERSION`,
  deterministic via a seeded `random.Random` whose state is saved too), then
  `layout_plan(cols, rows)` / `Layout`, `CONTROLS`, help text, drawing, and
  `run(session)` at the bottom. Timed effects (animations, clear phases) are
  explicit phases in the logic layer, not sleeps in the renderer.
- Layout targets the fixed 96×30 popup (93×28 inner, `POPUP_COLS/ROWS`):
  centred playfield plus a side column (stats, controls, agent roster), then
  degrades — agents drop first, then the side column becomes a one-line key
  hint, and only below the playfield size does a resize message appear.
  Every screen shows its controls and supports `?` help, `p`, `r`, `q`.
- `herdr/pane.sh` implements the manifest actions. Herdr 0.9.1 gives no pane
  id for popups, so the popup is managed through its process: `arcade.py`
  writes its pid and current screen to `popup.json` in the plugin state dir,
  and closing is a SIGTERM that the game traps to save and exit. A per-game
  action reads the screen to decide close vs. switch.
- Persistence: `<game>-game.json` (full state, saved on `q`/SIGTERM, resumed
  paused; corrupt or version-mismatched saves are ignored silently) and
  `<game>-hiscore.json`, both via `Session.path()` (None outside herdr).

## Tests

`tests/test_<game>.py` per module (Tetris's is `test_game.py`), plus
`test_common.py` and `test_arcade.py`. Logic is tested directly; rendering is
tested with a `FakeWin` stand-in for curses windows (asserting layout/border
columns at popup size and smaller tiers). Save round-trips must reproduce
identical continuation, RNG included.

## Adding a game

A module with `run(session)`, an entry in `GAMES` (arcade.py), a `[[panes]]`
+ `[[actions]]` pair in `herdr-plugin.toml` (bump its `version`), a tests
module, and README updates (game list, controls table, the game count).
