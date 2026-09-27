# herdr-arcade

A small terminal arcade of seventeen golden-era games in a
[Herdr](https://herdr.dev) popup pane, aware of the coding agents in your
session: when an agent needs attention, the game pauses itself and says so.
Python 3.9+, stdlib only (`curses`).

## Install

```sh
herdr plugin link /path/to/herdr-arcade   # this checkout
```

That registers the plugin (`stokje-dev.arcade`, shown as "Herdr Arcade"):
a pane entrypoint for the menu plus one per game, and an action for each.
`herdr plugin unlink stokje-dev.arcade` removes it again.

## Open and play

| Action | Does |
| --- | --- |
| `toggle` | open the popup on the game menu, or close it if open |
| `open` / `close` | open on the menu / close, no-op if already so |
| `<game id>` | open straight into that game; close it if it is already showing; switch to it from the menu or another game |

Game ids, in the menu's release-year order: `breakout` (1976), `snake`
(1976), `invaders` (1978), `pacman` (1980), `centipede`, `frogger`,
`donkeykong`, `qix` (1981), `pengo`, `tron`, `digdug`, `qbert`, `sokoban`
(1982), `loderunner`, `bomberman` (1983), `boulderdash`, `tetris` (1984).

The **menu** lists the games oldest first, with their year, best score and
whether a saved game is waiting: `↑`/`↓` (or `j`/`k`) and `enter` to play,
the hotkey shown beside each game (`1`-`9`, `0`, then letters) to pick it
directly, `?` to read the selected
game's help, `q` to quit. The list scrolls on short panes. In a game, `q`
saves and goes back to the menu.

### Binding games to keys

Every game has its own action, so bind as many as you like in
`~/.config/herdr/config.toml` (then `herdr server reload-config`). Give each
binding a `description`, or herdr's key list shows it as "custom command":

```toml
# menu
[[keys.command]]
key = "prefix+shift+b"
type = "shell"
command = "herdr plugin action invoke toggle --plugin stokje-dev.arcade"
description = "Herdr Arcade: game menu"

# straight into a game (press again to close it)
[[keys.command]]
key = "prefix+shift+p"
type = "shell"
command = "herdr plugin action invoke pacman --plugin stokje-dev.arcade"
description = "Herdr Arcade: pac-man"
```

`type = "plugin_action"` with `command = "stokje-dev.arcade.pacman"` should
work too (other plugins bind that way), but the shell form is the one tested
with this plugin. Use the full path to `herdr` if it is not on the PATH herdr
runs shell bindings with.

Adding a game means a module with `run(session)`, an entry in `GAMES`
(arcade.py), and a `[[panes]]` + `[[actions]]` pair in the manifest.

The popup is 96×30 (93×28 usable — popups carry chrome and cannot be
resized). Every screen centres its playfield with a side column of stats,
the game's controls and the agent roster, and adapts to smaller panes:
agents drop first, then the side column gives way to a one-line key hint,
and only below the playfield itself does a resize message appear.

### Controls

Every game: `?` help (goal, rules, scoring, tips and a glyph legend; the
game pauses while it is open, up/down scroll it and any other key, `q`
included, closes it), `p` pause, `r` restart, `q` save and back to
the menu. Each game's controls stay on screen while you play. Movement is
arrows, `hjkl` or `wasd` unless noted. Terminals report no key releases,
so movement is one step per keypress (hold a key to auto-repeat).

| Game | Controls and notes |
| --- | --- |
| Breakout | `←`/`→` (`h`/`l`, `a`/`d`) move the paddle, `space` launches. Paddle offset sets the angle; the ball speeds up with hits and back rows; the paddle shrinks after hitting the ceiling. |
| Snake | steer; two quick turns both apply. Food grows you by 3; 8 foods clear a level; four wall layouts cycle; bonus food times out. |
| Space Invaders | `←`/`→` move, `space` fires. Five-row formation, destructible shields, mystery UFO, extra life at 1500. |
| Pac-Man | steer; a turn pressed early is taken at the next opening. 19×21 maze, four ghosts with the classic chase/scatter targeting, power pellets, extra life at 10,000. |
| Centipede | move within the bottom zone, `space` fires. Hit segments become mushrooms and split the centipede; spider and flea as in the original. |
| Frogger | hop per keypress. Five road and five river lanes, diving turtles, a timer per frog, five home bays and a bonus fly. |
| Donkey Kong | walk and climb; `space` jumps the way you last moved (or straight up), `z`/`,` and `x`/`.` jump left/right. Fixed jump arc, falls of more than two rows kill, broken ladders stop short. 25m barrels (hammers, oil-drum fireballs) and 100m rivets alternate, faster each loop; the bonus timer is your clear bonus. |
| Qix | move along safe lines; `space` toggles fast draw, `f` slow draw (double points). Close a shape to claim the side without the Qix; claim 75% to clear. Stopping mid-line lights a fuse. |
| Pengo | move, `space` (or `enter`) pushes the ice block in front, or shakes the wall to stun Sno-Bees touching it. Line up the three diamonds for a bonus. |
| Tron | steer the light cycle; 1 AI rival at level 1, up to 3 later. The AI looks ahead with a flood fill. |
| Dig Dug | dig as you move; `space` (or `enter`) fires the pump, pump again to inflate and pop. Popping deeper scores more, Fygars double from the side; undermined rocks fall and crush (multi-kill bonus); a vegetable follows two dropped rocks; the last monster flees top-left. |
| Q*bert | arrows turned 45°: `↑` up-right, `→` down-right, `↓` down-left, `←` up-left (`wasd`/`hjkl` follow the arrows); `y`/`u`/`b`/`n` are the diagonals as they sit on the keyboard. One hop per keypress, two queued in the air. Discs carry you to the top; lure Coily off one for 500. |
| Sokoban | walk and push; `u`/`z` undo, `y` redo, `r` restarts the level (replayable with `y`), `[`/`]` or `b`/`n` step through unlocked levels, `x` then `y` starts a new game. 32 levels; each solved level scores 1000 minus your best moves (at least 100). |
| Lode Runner | move, climb ladders, hang on bars; `z`/`,` dig left, `x`/`.` dig right. Collect all gold, then climb the escape ladder. Five original levels. |
| Bomberman | move, `space` drops a bomb, `x` sets off the oldest one once you have the remote. Flames chain bombs and burn the first soft block; power-ups and the exit hide under blocks; the exit opens once every monster is gone. Bombing the exit or a power-up spawns more monsters. |
| Boulder Dash | move and dig; `space` then a direction snaps (dig or grab next to you without moving). Falling boulders and diamonds kill, and they roll off round things. Collect the quota, then reach the exit before time runs out; 8 caves with fireflies, butterflies, amoeba and magic walls. |
| Tetris | `←`/`→` move, `↑` rotate, `↓` soft drop, `space` hard drop, `c` hold (vi keys work; wasd does not). 7-bag, ghost piece, 3-piece preview, line-clear animation. |

## Persistence

Closing the popup (any close/toggle, or `q` back to the menu) does not throw
a game away: its full state — including the RNG state, so Tetris's piece
sequence and the ghosts' frightened moves continue identically — is saved
atomically to `<game>-game.json` in the plugin state dir. Starting that game
again resumes it paused behind a "resumed — press any key" banner; a corrupt
or incompatible save is ignored silently. `r` (restart) and game over clear
the save. Best scores live in `<game>-hiscore.json` and show in each game's
stats box and on the menu.

## Agent awareness


While running, a background thread polls `herdr agent list` (JSON) every ~2s
and renders a side-panel roster: each agent's name/kind and status
(`idle`, `working`, `blocked`, `done`, `unknown`), colour coded.

One poller runs for the whole popup, shared by the menu and the games. When
an agent **transitions into `blocked` or `done`** — i.e. it needs you — the
arcade:

1. rings the terminal bell once,
2. shows a banner naming the agent and its new state and pauses the
   running game (the menu just shows the banner),
3. resumes on any key.

It does not re-trigger for the same agent until its status changes again (a
staying-`blocked` agent is quiet; leaving and re-entering `blocked` re-triggers).
A first sighting that is already `blocked`/`done` counts as a transition — the
agent needs attention whether or not you were watching before.

Run with `--no-auto-pause` (add it to the pane commands in
`herdr-plugin.toml` if you want it permanent) to keep banners and bells
without pausing gameplay.

Poll failures are ignored silently and the last known roster stays on screen,
so the arcade also runs fine outside Herdr (`python3 arcade.py [--game ID]`
in any terminal).

## Files

- `herdr-plugin.toml` — manifest: pane entrypoints (menu + one per game) and
  the `toggle`/`open`/`close`/per-game actions
- `arcade.py` — entrypoint: the menu, `--game`, the game registry
- `common.py` — shared: agent polling/tracking, state files, the popup
  sentinel, curses helpers, and the `Session` handed to every game
- `tetris.py`, `pacman.py`, `snake.py`, … — one module per game; pure logic at the top, terminal-free
  and unit-tested, the curses UI and `run(session)` below
- `herdr/pane.sh` — action helper. Popup panes are a blind spot of the herdr
  0.9.1 CLI (`plugin pane open` returns no pane id for them, `pane list`
  never shows them), so the popup is managed through its process: arcade.py
  registers its pid and current screen in `popup.json` in the plugin state
  dir, and closing is a SIGTERM that the arcade traps to save and exit. A
  per-game action reads the screen to decide between close and switch.
- `tests/` — unit tests (`python3 -m unittest discover -s tests` from this
  directory)
