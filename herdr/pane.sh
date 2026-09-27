#!/usr/bin/env bash
# Arcade pane actions.
#
#   pane.sh toggle     open the arcade popup on the menu, or close it if open
#   pane.sh open       open the arcade popup on the menu, no-op if open
#   pane.sh close      close the arcade popup, no-op if closed
#   pane.sh game ID    open straight into game ID; close it if ID is already
#                      showing; switch to it (close + reopen) from any other
#                      screen. These are the actions to bind to keys.
#
# The game runs as a popup pane, and popup panes are a blind spot of the herdr
# 0.9.1 CLI: `plugin pane open` returns no pane id for them (only {"type":
# "ok"}), and `herdr pane list` / `pane process-info` never show them. The pane
# is therefore managed through the game process itself: arcade.py writes its
# pid and current screen to the plugin state dir (common.write_sentinel) and removes it on exit, and
# herdr tears the popup down when its process exits. Closing = SIGTERM to that
# pid; the game traps it and shuts down cleanly. Actions refuse loudly
# (exit 1, one stderr line) and report successes on stdout.
set -uo pipefail

# herdr runs plugin commands with a minimal PATH; ensure jq resolves on common installs.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

mode="${1:-toggle}"
H="${HERDR_BIN_PATH:-herdr}"

refuse() { printf 'arcade: %s\n' "$1" >&2; exit 1; }

root="${HERDR_PLUGIN_ROOT:-}"
[ -n "$root" ] || refuse "HERDR_PLUGIN_ROOT not set"
[ -f "$root/arcade.py" ] || refuse "arcade.py missing under plugin root"

ws="${HERDR_WORKSPACE_ID:-}"
[ -n "$ws" ] || refuse "no workspace context (invoke from inside herdr)"

# Same resolution both sides use (common.py mirrors it): the state dir herdr
# injects, else herdr's XDG layout for this plugin id.
plugin_id="${HERDR_PLUGIN_ID:-stokje-dev.arcade}"
state="${HERDR_PLUGIN_STATE_DIR:-$HOME/.local/state/herdr/plugins/$plugin_id}"
sentinel="$state/popup.json"

# The live game pid, or nothing. Validated through the process command line:
# pids are reused, so a stale sentinel after a crash must read as closed, and
# a recycled pid must never be signalled.
game_pid() {
  local pid cmd
  pid=$(jq -r '.pid // 0' "$sentinel" 2>/dev/null) || pid=0
  case "$pid" in '' | *[!0-9]*) return 1 ;; esac
  [ "$pid" -gt 0 ] 2>/dev/null || return 1
  cmd=$(ps -p "$pid" -o command= 2>/dev/null) || return 1
  case "$cmd" in
  *arcade.py*) ;;
  *) return 1 ;;
  esac
  printf '%s' "$pid"
}

# The screen the live popup shows: "menu" or a game id.
current_screen() {
  jq -r '.game // "menu"' "$sentinel" 2>/dev/null
}

close_game() {
  pid=$(game_pid) || { printf 'close: nothing open\n'; exit 0; }
  kill "$pid" 2>/dev/null
  # The game traps SIGTERM and exits within one input-poll tick (~10ms); wait
  # it out so a following `open` in the same action sequence cannot race.
  i=0
  while [ "$i" -lt 50 ]; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.1
    i=$((i + 1))
  done
  kill -0 "$pid" 2>/dev/null && refuse "arcade process $pid did not exit"
  printf 'closed arcade popup (pid %s) in %s\n' "$pid" "$ws"
}

# Open the popup on entrypoint $1 (arcade = menu, or a game id). A manual
# open takes focus so the user can play at once. 96x30 popup = 93x28 inner
# area (common.POPUP_COLS/ROWS), which every screen lays out for. stderr joins stdout so a server error body can be read:
# ui_busy means some other popup holds the single popup slot.
open_pane() {
  local entry="${1:-arcade}" open_json i
  open_json=$("$H" plugin pane open --plugin "$plugin_id" --entrypoint "$entry" \
    --placement popup --width 96 --height 30 --focus 2>&1)
  if [ "$?" -ne 0 ]; then
    case "$open_json" in
    *ui_busy*) refuse "another popup pane is already open" ;;
    *) refuse "herdr plugin pane open failed" ;;
    esac
  fi
  printf '%s' "$open_json" | jq -e \
    '(.result.type == "ok") or (.result.plugin_pane.pane.pane_id != null)' \
    >/dev/null 2>&1 || refuse "herdr plugin pane open failed"
  # Wait for the game to register itself so an immediate toggle-off sees it.
  i=0
  while [ "$i" -lt 30 ]; do
    game_pid >/dev/null && break
    sleep 0.1
    i=$((i + 1))
  done
  printf 'opened arcade popup (%s) in %s\n' "$entry" "$ws"
}


case "$mode" in
close)
  close_game
  ;;
toggle)
  if game_pid >/dev/null; then
    close_game
  else
    open_pane
  fi
  ;;
open)
  if game_pid >/dev/null; then
    printf 'open: already open in %s\n' "$ws"
  else
    open_pane
  fi
  ;;
game)
  id="${2:-}"
  [ -f "$root/$id.py" ] && [ "$id" != arcade ] && [ "$id" != common ] ||
    refuse "unknown game '$id'"
  if game_pid >/dev/null; then
    was=$(current_screen)
    close_game
    [ "$was" = "$id" ] && exit 0
  fi
  open_pane "$id"
  ;;
*)
  refuse "unknown mode '$mode' (toggle | open | close | game ID)"
  ;;
esac
