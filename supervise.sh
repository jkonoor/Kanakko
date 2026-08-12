#!/usr/bin/env bash
#
# supervise.sh — keep ralph.sh running unattended, across session limits.
#
# Ralph stops for several different reasons and they need different responses.
#
# A usage limit is detected from ralph's own output and waited out on its own
# counter — it is not a stall, and treating it as one killed an otherwise
# healthy run an hour before the window would have reset. That failure cost this
# project 13 hours: the loop died at iteration 8 of 16 on the session limit and
# nothing noticed, because the watcher was matching its own command line.
#
# Anything else that stalls is judged on *progress*: if consecutive restarts
# produce no new commits, something is actually wrong and a person should look.
# If commits are landing, a stop is just the account catching its breath.
#
#   ./supervise.sh              run until COMPLETE or the wall clock
#   ./supervise.sh 20           ... with 20 iterations per ralph batch
#
# Intended to run detached. See "tmux" at the bottom of this file.
#
# Tunable: RALPH_BATCH RALPH_MAX_HOURS RALPH_MAX_DEAD RALPH_THROTTLE_WAIT
#          RALPH_MAX_THROTTLE RALPH_DEV_MODEL RALPH_QA_MODEL

# Deliberately no `set -e`: this script's whole job is reacting to non-zero
# exit codes. `set -u` and a failing pipe still matter.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

BATCH="${1:-${RALPH_BATCH:-8}}"           # iterations per ralph invocation
MAX_HOURS="${RALPH_MAX_HOURS:-12}"        # wall-clock cap on the whole run
MAX_DEAD="${RALPH_MAX_DEAD:-3}"           # consecutive no-progress stops before giving up
THROTTLE_WAIT="${RALPH_THROTTLE_WAIT:-1800}"   # 30m — a usage window is usually <1h away
MAX_THROTTLE="${RALPH_MAX_THROTTLE:-8}"        # up to ~4h of waiting before giving up
BACKOFF=(300 900 1800 3600)               # 5m, 15m, 30m, then 60m for every retry after

LOG_DIR="logs"
mkdir -p "$LOG_DIR"
RUN_LOG="$LOG_DIR/supervise-$(date +%Y%m%d-%H%M%S).log"
STATUS="$LOG_DIR/STATUS"

# Plain redirect, not `tee`: piping into tee wrote to the terminal but silently
# never reached the file when run detached under tmux, which left the whole run
# unlogged. Two explicit writes are duller and actually work.
log() {
  local line
  line="$(date '+%F %T')  $*"
  printf '%s\n' "$line"
  printf '%s\n' "$line" >> "$RUN_LOG"
}
status() { printf '%s\n' "$*" > "$STATUS"; }

# Best-effort desktop notification; never let its absence break the run.
notify() {
  command -v notify-send >/dev/null 2>&1 && notify-send "Ralph — Kanakko" "$1" 2>/dev/null
  log "$1"
}

commits() { git rev-list --count HEAD 2>/dev/null || echo 0; }

# ------------------------------------------------------------------ preflight

[[ -x ./ralph.sh ]] || { echo "ralph.sh not found or not executable" >&2; exit 1; }
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "not a git repo" >&2; exit 1; }

# ralph.sh refuses main and develop, but it does so *after* this script has
# already committed to a 12-hour run. Fail here instead, while a person is still
# watching: develop is the deploy branch and a push to it ships an image.
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
case "$BRANCH" in
  main|master|develop)
    echo "Refusing to supervise on $BRANCH — develop deploys. Use: git switch -c ralph/phase-10 develop" >&2
    exit 1 ;;
esac

# Keep the pane (and its scrollback) after this script exits. Without it tmux
# tears the session down the moment the run ends, so the reason it stopped —
# COMPLETE, a stall, or the wall clock — disappears with it.
[[ -n "${TMUX:-}" ]] && tmux set-option -p remain-on-exit on 2>/dev/null

START_EPOCH=$(date +%s)
DEADLINE=$(( START_EPOCH + MAX_HOURS * 3600 ))
START_COMMITS=$(commits)
dead_streak=0
throttle_streak=0
attempt=0

log "supervisor starting — batch=$BATCH max_hours=$MAX_HOURS branch=$BRANCH"
log "log: $RUN_LOG"
status "RUNNING since $(date '+%F %T')"

# --------------------------------------------------------------------- loop --

while :; do
  now=$(date +%s)
  if (( now >= DEADLINE )); then
    notify "Wall-clock cap of ${MAX_HOURS}h reached. $(( $(commits) - START_COMMITS )) commits this run."
    status "STOPPED: wall-clock cap"
    exit 0
  fi

  # Ralph refuses a dirty tree, and a killed pass can leave one. Stash rather
  # than clean: a half-finished pass is worthless, but destroying work
  # unattended is not a decision a script should make. Recover with `git stash list`.
  if [[ -n "$(git status --porcelain)" ]]; then
    log "dirty tree — stashing partial work from an interrupted pass"
    git stash push -u -m "supervise: interrupted pass $(date '+%F %T')" >>"$RUN_LOG" 2>&1
  fi

  before=$(commits)
  attempt=$((attempt + 1))
  log "── ralph batch #$attempt (${BATCH} iterations, from commit count $before)"

  # tee so the pane shows progress live AND the log keeps the full record.
  # PIPESTATUS, not $?, or we would read tee's exit code instead of ralph's and
  # every outcome would look like success.
  ./ralph.sh "$BATCH" 2>&1 | tee -a "$RUN_LOG"
  code=${PIPESTATUS[0]}

  after=$(commits)
  gained=$(( after - before ))
  log "── ralph exited $code, +$gained commits"

  case "$code" in
    0)
      # For Kanakko this also covers "only [human] tasks remain" — prompts/dev.md
      # skips those and reports COMPLETE rather than a distinct blocked code.
      notify "COMPLETE — nothing left but [human] tasks. +$(( after - START_COMMITS )) commits."
      status "STOPPED: COMPLETE"
      exit 0
      ;;
    2)
      # Cap reached with work remaining: the healthy path. Straight back in.
      dead_streak=0
      log "cap reached, work remains — continuing immediately"
      continue
      ;;
    *)
      # A usage limit is not a stall, and treating it as one is how an
      # otherwise-healthy run dies: the dead-streak ladder gives up after ~20
      # minutes, while a limit can be an hour or more away from resetting.
      # Detected explicitly rather than inferred, and given its own counter so a
      # genuinely broken loop still stops.
      if tail -n 40 "$RUN_LOG" 2>/dev/null | grep -qiE "session limit|usage limit|rate limit|resets [0-9]"; then
        throttle_streak=$((throttle_streak + 1))
        if (( throttle_streak >= MAX_THROTTLE )); then
          notify "Stopped: still rate-limited after $MAX_THROTTLE waits. Needs a person."
          status "STOPPED: rate-limited for too long"
          exit 1
        fi
        wait_s=$THROTTLE_WAIT
        remaining=$(( DEADLINE - $(date +%s) ))     # never sleep past the cap
        (( wait_s > remaining )) && wait_s=$remaining
        (( wait_s <= 0 )) && continue
        log "usage limit hit ($throttle_streak/$MAX_THROTTLE) — waiting ${wait_s}s for the window to reset"
        status "WAITING on usage limit (${throttle_streak}/${MAX_THROTTLE})"
        sleep "$wait_s"
        status "RUNNING"
        continue
      fi

      # Stall. Progress is the discriminator: commits landing means the account
      # is throttled and will recover; no commits repeatedly means it is broken.
      if (( gained > 0 )); then
        dead_streak=0
        log "stalled but made progress — treating as a limit, backing off"
      else
        dead_streak=$((dead_streak + 1))
        log "stalled with no progress ($dead_streak/$MAX_DEAD)"
      fi

      if (( dead_streak >= MAX_DEAD )); then
        notify "Stopped: $MAX_DEAD consecutive batches produced no commits. Needs a person."
        status "STOPPED: no progress after $MAX_DEAD attempts"
        exit 1
      fi

      idx=$(( dead_streak > 0 ? dead_streak - 1 : 0 ))
      (( idx >= ${#BACKOFF[@]} )) && idx=$(( ${#BACKOFF[@]} - 1 ))
      wait_s=${BACKOFF[$idx]}

      # Never sleep past the deadline.
      remaining=$(( DEADLINE - $(date +%s) ))
      (( wait_s > remaining )) && wait_s=$remaining
      (( wait_s <= 0 )) && continue

      log "sleeping ${wait_s}s before retry"
      status "SLEEPING ${wait_s}s (attempt $attempt, dead streak $dead_streak)"
      sleep "$wait_s"
      status "RUNNING"
      ;;
  esac
done

# ----------------------------------------------------------------------------
# Running it detached, so it survives closing the terminal:
#
#   git switch -c ralph/phase-10 develop
#   tmux new -d -s kanakko './supervise.sh'   # start
#   tmux attach -t kanakko                    # watch
#   (Ctrl-b then d to detach again)
#   tmux kill-session -t kanakko              # stop
#
# From anywhere, without attaching:
#   cat logs/STATUS                         # one line: what it is doing now
#   tail -f logs/supervise-*.log            # full history
#   git log --oneline                       # what actually landed
#   cat REVIEWS.md                          # what QA thought of it
# ----------------------------------------------------------------------------
