#!/usr/bin/env bash
#
# Check a running imessage-mcp server and optionally report to a dead-man's-switch
# service such as healthchecks.io.
#
# Run it from cron or a LaunchAgent on the machine hosting the server. Reporting
# outward matters: a monitor running on the same machine cannot tell you that the
# machine is gone, whereas a service expecting a regular ping can.
#
# Configuration comes from ~/.imessage-mcp/check.env, if it exists:
#
#   HEALTH_URL=http://127.0.0.1:18791/health
#   PING_URL=https://hc-ping.com/your-uuid-here
#   EXPECTED_PYTHON=/Users/USERNAME/.local/share/uv/python/cpython-3.12.13-.../bin/python3.12
#   VENV_PYTHON=/Users/USERNAME/Code/imessage-mcp/.venv/bin/python
#   MAX_QUIET_SECONDS=0  # 0 disables the staleness check
#
# Everything is optional except HEALTH_URL. Without PING_URL it just prints its
# findings and exits non-zero on a problem, which is useful for running by hand.

set -uo pipefail

CONFIG="${IMESSAGE_MCP_CHECK_ENV:-$HOME/.imessage-mcp/check.env}"
# shellcheck source=/dev/null
[[ -f "$CONFIG" ]] && source "$CONFIG"

HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/health}"
PING_URL="${PING_URL:-}"
EXPECTED_PYTHON="${EXPECTED_PYTHON:-}"
VENV_PYTHON="${VENV_PYTHON:-}"
# Off by default, and think before turning it on. The age of the newest message
# cannot distinguish "Messages has stopped syncing" from "nobody has texted me"
# -- a quiet weekend looks identical to a broken sync, and this is a personal
# account, not a busy one. Set a threshold in seconds only once the logged
# quiet values show what an ordinary lull looks like, and pick a number no
# genuine silence would reach. The age is reported on every run either way.
MAX_QUIET_SECONDS="${MAX_QUIET_SECONDS:-0}"

problems=()
report=""

ping_hc() {
  [[ -z "$PING_URL" ]] && return 0
  curl -fsS -m 10 --retry 3 --data-raw "$2" "${PING_URL}${1}" >/dev/null 2>&1 || true
}

ping_hc "/start" ""

# --- is the server answering, and does it think it is healthy? ---------------
# A timeout here is meaningful: the documented failure mode on macOS is a
# privacy prompt that hangs the process rather than crashing it.
body=$(curl -fsS -m 15 "$HEALTH_URL" 2>/dev/null)
if [[ -z "$body" ]]; then
  problems+=("no response from $HEALTH_URL (down, or hung on a permission prompt)")
else
  # Parsed with plutil, which ships with macOS. Deliberately not /usr/bin/python3:
  # that is a Command Line Tools shim, and an OS update can leave it prompting to
  # install developer tools -- which would break this check exactly when an OS
  # update is the thing most likely to have broken something.
  jget() { printf '%s' "$body" | plutil -extract "$1" raw -o - - 2>/dev/null; }
  status=$(jget status);  database=$(jget database)
  newest=$(jget newest_message); python=$(jget python_version)
  messages=$(jget messages_running)

  if [[ -z "$status" ]]; then
    problems+=("could not parse the health response from $HEALTH_URL")
  fi

  # How long since the newest message arrived. date -j -f is the BSD form; GNU
  # date is not present on macOS.
  quiet="?"
  if [[ -n "$newest" ]]; then
    # Trim fractional seconds and the offset: the health field is ISO 8601 with
    # both, and the BSD parser accepts neither.
    trimmed="${newest%%.*}"; trimmed="${trimmed%%+*}"
    epoch=$(date -j -u -f "%Y-%m-%dT%H:%M:%S" "$trimmed" "+%s" 2>/dev/null || echo "")
    [[ -n "$epoch" ]] && quiet=$(( $(date -u "+%s") - epoch ))
  fi
  report+="status=${status:-?} database=${database:-?} messages=${messages:-?}"
  report+=" quiet=${quiet}s python=${python:-?}"

  [[ -n "$status" && "$status" != "ok" ]] && problems+=("health status is '$status'")
  [[ -n "$database" && "$database" != "ok" ]] && problems+=("database is $database")
  # Reads still work without Messages; sends would not. Worth waking someone
  # for, because nothing else about the server looks wrong.
  [[ "$messages" == "false" || "$messages" == "False" ]] &&
    problems+=("Messages.app is not running; sends will fail")

  if (( MAX_QUIET_SECONDS > 0 )) && [[ "$quiet" != "?" ]]; then
    if (( quiet > MAX_QUIET_SECONDS )); then
      problems+=("no new message for ${quiet}s (limit ${MAX_QUIET_SECONDS}s); sync may be dead")
    fi
  fi
fi

# --- has the interpreter moved out from under its privacy approval? ----------
# macOS grants Full Disk Access against a path, and tool-managed interpreters
# live at version-stamped paths. An upgrade silently revokes the grant, and the
# service then hangs on its next restart with no error anywhere. Catching the
# move is the only warning available before that happens.
if [[ -n "$EXPECTED_PYTHON" && -n "$VENV_PYTHON" ]]; then
  # readlink -f resolves the whole chain; stat -f %Y follows only one link, which
  # would stop at uv's stable alias and miss the versioned path that matters.
  actual=$(readlink -f "$VENV_PYTHON" 2>/dev/null)
  if [[ -z "$actual" ]]; then
    problems+=("cannot resolve $VENV_PYTHON")
  elif [[ "$actual" != "$EXPECTED_PYTHON" ]]; then
    problems+=("interpreter moved to $actual (approval was granted to $EXPECTED_PYTHON); re-grant Full Disk Access and update EXPECTED_PYTHON")
  fi
fi

# --- report ------------------------------------------------------------------
now=$(date '+%Y-%m-%d %H:%M:%S')

if (( ${#problems[@]} )); then
  msg="imessage-mcp check FAILED"$'\n'"$report"$'\n'
  for p in "${problems[@]}"; do msg+="- $p"$'\n'; done
  printf '[%s] %s' "$now" "$msg"
  ping_hc "/fail" "$msg"
  exit 1
fi

# Logged on every run, not just failures, so the log doubles as a record of how
# fresh the database has been staying.
printf '[%s] imessage-mcp OK %s\n' "$now" "$report"
ping_hc "" "OK $report"
