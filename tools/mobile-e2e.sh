#!/usr/bin/env bash
# zcharlie mobile E2E harness.
#
# Reads DOM state through the debug WebView's CDP endpoint (tools/mobile-e2e/cdp.mjs)
# and acts through real system input injection (adb input tap/text/keyevent), so the
# app is driven the way a user drives it while assertions stay exact.
#
# Selectors are CSS selectors inside the app's WebView. Screenshots land in $M2E_SHOTS.
# Commands:
#   ensure-cdp                       (re)resolve app pid and adb-forward the CDP port
#   shot <name>                      screenshot -> $M2E_SHOTS/<name>.png
#   tap <selector>                   real tap at the element's center
#   type_ascii <selector> <text>     tap + adb input text (ASCII; space -> %s) + dismiss keyboard
#   insert_text <selector> <text>    tap + CDP insertText (CJK-safe) + dismiss keyboard
#   text <selector>                  print textContent
#   exists <selector>                print yes/no
#   eval '<js>'                      evaluate in page, print JSON value
#   waittext <selector> <needle> [s] poll until textContent contains needle (default 20s)
#   waitgone <selector> <needle> [s] poll until needle disappears
#   waitsel <selector> [s]           poll until element exists
#   key <code> | back | home         system keys (4 = BACK)
#   swipe <x1 y1 x2 y2 ms>           real swipe (scroll gestures)
#   app-stop | app-start | app-restart | app-clear
set -euo pipefail

ADB="${ADB:-adb}"
APP_ID="${M2E_APP_ID:-com.zcharlie.mobile.debug}"
M2E_SHOTS="${M2E_SHOTS:-/tmp/mobile-e2e-shots}"
CDP_PORT="${M2E_CDP_PORT:-9222}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CDP_JS="$HERE/mobile-e2e/cdp.mjs"
mkdir -p "$M2E_SHOTS"

log() { echo "[m2e] $*"; }
die() { echo "[m2e] FAIL: $*" >&2; exit 1; }

app_pid() { "$ADB" shell pidof "$APP_ID" | tr -d '\r' | awk '{print $1}' || true; }

ensure_cdp() {
  local pid
  pid="$(app_pid)"
  [ -n "$pid" ] || return 1
  "$ADB" forward "tcp:$CDP_PORT" "localabstract:webview_devtools_remote_$pid" >/dev/null 2>&1
}

webview_origin() {
  "$ADB" shell uiautomator dump /sdcard/window_dump.xml >/dev/null 2>&1 || true
  "$ADB" shell cat /sdcard/window_dump.xml | python3 -c '
import re, sys
match = re.search(r"class=\"android.webkit.WebView\"[^>]*bounds=\"\[(\d+),(\d+)\]", sys.stdin.read())
print(f"{match.group(1)} {match.group(2)}" if match else "0 0")'
}

cdp() { node "$CDP_JS" "$@"; }

shot() {
  local name="$1"
  "$ADB" exec-out screencap -p > "$M2E_SHOTS/$name.png"
  log "shot $name"
}

_tap_rect() {  # _tap_rect <cdp-rect-command> <selector>
  ensure_cdp
  local rect origin x y ox oy
  rect="$(cdp "$1" "$2")" || die "no rect for selector: $2"
  origin="$(webview_origin)"
  x="$(python3 -c "import json,sys;r=json.loads(sys.argv[1]);print(int((r['x']+r['w']/2)*r['dpr']))" "$rect")"
  y="$(python3 -c "import json,sys;r=json.loads(sys.argv[1]);print(int((r['y']+r['h']/2)*r['dpr']))" "$rect")"
  ox="${origin% *}"; oy="${origin#* }"
  "$ADB" shell input tap "$((x + ox))" "$((y + oy))"
}
tap() { _tap_rect rect "$1"; }
taplast() { _tap_rect lastrect "$1"; }

_type_common() {  # tap, clear existing content, feed text, dismiss keyboard
  tap "$1"
  sleep 1.2   # let the keyboard settle
  ensure_cdp
  node "$CDP_JS" clear "$1" >/dev/null || true   # replace, not append
  sleep 0.3
  if [ "$2" = ascii ]; then
    local escaped="${3// /%s}"
    "$ADB" shell input text "$escaped"
  else
    node "$CDP_JS" insert "$1" "$3"
  fi
  "$ADB" shell input keyevent 4   # dismiss keyboard; layout restores
  sleep 0.6
}
type_ascii() { _type_common "$1" ascii "$2"; }
insert_text() { _type_common "$1" cjk "$2"; }

text() { ensure_cdp; cdp text "$1"; }
lasttext() { ensure_cdp; cdp lasttext "$1"; }
jsclicklast() { ensure_cdp; cdp jsclick "$1"; }
exists() { ensure_cdp; cdp exists "$1"; }
evaljs() { ensure_cdp; cdp eval "$1"; }

_poll() {  # _poll <kind: text|gone|sel|lasttext|lastgone> <selector> <needle|-> <timeout>
  ensure_cdp
  local kind="$1" sel="$2" needle="$3" timeout="${4:-20}" got="" found=""
  local attempts=$((timeout * 2))
  for _ in $(seq 1 "$attempts"); do
    case "$kind" in
      sel)
        found="$(cdp exists "$sel" 2>/dev/null || echo no)"
        [ "$found" = yes ] && return 0
        ;;
      text|gone)
        got="$(cdp text "$sel" 2>/dev/null || true)"
        if [ "$kind" = text ]; then
          [[ "$got" == *"$needle"* ]] && { echo "$got"; return 0; }
        else
          [[ "$got" != *"$needle"* ]] && return 0
        fi
        ;;
      lasttext|lastgone)
        got="$(cdp lasttext "$sel" 2>/dev/null || true)"
        if [ "$kind" = lasttext ]; then
          [[ "$got" == *"$needle"* ]] && { echo "$got"; return 0; }
        else
          [[ "$got" != *"$needle"* ]] && return 0
        fi
        ;;
    esac
    sleep 0.5
  done
  shot "FAIL-poll-$kind"
  die "timeout (${timeout}s) kind=$kind sel=$sel needle=$needle got=${got:-<none>}"
}
waittext() { _poll text "$1" "$2" "${3:-20}"; }
waitgone() { _poll gone "$1" "$2" "${3:-20}"; }
waitsel() { _poll sel "$1" - "${2:-20}"; }
waitlast() { _poll lasttext "$1" "$2" "${3:-20}"; }
waitlastgone() { _poll lastgone "$1" "$2" "${3:-20}"; }

key() { "$ADB" shell input keyevent "$1"; }
back() { key 4; }
home() { key 3; }

app_stop() { "$ADB" shell am force-stop "$APP_ID"; log "app stopped"; }
app_start() {
  local attempt pid
  for attempt in 1 2; do
    "$ADB" shell am start -W -n "$APP_ID/com.zcharlie.mobile.MainActivity" >/dev/null
    for _ in $(seq 1 60); do   # wait for process + WebView CDP socket (up to 30s)
      pid="$(app_pid)"
      if [ -n "$pid" ] && ensure_cdp && cdp exists "body" >/dev/null 2>&1; then
        waitsel ".pair-screen, .app-shell" 15
        log "app started"
        return 0
      fi
      sleep 0.5
    done
    log "start attempt $attempt did not reach CDP, retrying"
  done
  die "app failed to start with a readable WebView"
}
app_restart() { app_stop; sleep 1; app_start; }
app_clear() {
  app_stop
  "$ADB" shell pm clear "$APP_ID" >/dev/null
  app_start
}

main() {
  local cmd="${1:-}"
  [ -n "$cmd" ] || die "no command; see header"
  shift
  case "$cmd" in
    ensure-cdp) ensure_cdp ;;
    shot) shot "$@" ;;
    tap) tap "$@" ;;
    taplast) taplast "$@" ;;
    type_ascii) type_ascii "$@" ;;
    insert_text) insert_text "$@" ;;
    text) text "$@" ;;
    lasttext) lasttext "$@" ;;
    jsclicklast) jsclicklast "$@" ;;
    exists) exists "$@" ;;
    eval) evaljs "$@" ;;
    waittext) waittext "$@" ;;
    waitgone) waitgone "$@" ;;
    waitsel) waitsel "$@" ;;
    waitlast) waitlast "$@" ;;
    waitlastgone) waitlastgone "$@" ;;
    key) key "$@" ;;
    back) back ;;
    home) home ;;
    swipe) "$ADB" shell input swipe "$@" ;;
    app-stop) app_stop ;;
    app-start) app_start ;;
    app-restart) app_restart ;;
    app-clear) app_clear ;;
    *) die "unknown command: $cmd" ;;
  esac
}

main "$@"
