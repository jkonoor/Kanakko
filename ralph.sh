#!/usr/bin/env bash
#
# Ralph loop for Kanakko.
#
#   ./ralph.sh            auto — dev until the queue drains, then QA, repeat
#   ./ralph.sh dev [N]    implement up to N tasks   (default $RALPH_DEV_MAX)
#   ./ralph.sh qa  [N]    run N review passes       (default $RALPH_QA_PASSES)
#
# docs/DECISIONS.md is the spec. TASKS.md is the queue. Each iteration is a
# fresh context — the repository is the memory. See prompts/dev.md, prompts/qa.md.
#
# Overridable: RALPH_DEV_MODEL RALPH_QA_MODEL RALPH_DEV_MAX RALPH_QA_PASSES
#              RALPH_CYCLES
#
# Setting RALPH_QA_MODEL to a different model than RALPH_DEV_MODEL is
# worthwhile: a review is more useful from a different vantage point than the
# one that wrote the code.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

MODE="${1:-auto}"
N="${2:-}"

DEV_MODEL="${RALPH_DEV_MODEL:-opus}"
QA_MODEL="${RALPH_QA_MODEL:-opus}"

# Least privilege. The loop runs unattended, so anything not listed here stalls
# rather than prompting — widen it deliberately when a task genuinely needs
# something, rather than reaching for --dangerously-skip-permissions.
# Note `gh` is absent on purpose: its credentials live in the keyring, not the
# environment, so the scrub below cannot revoke them. The loop has no business
# pushing anywhere.
ALLOWED_TOOLS="${RALPH_ALLOWED_TOOLS:-Edit Write Read Grep Glob Bash(git *) Bash(uv *) Bash(python *) Bash(pytest *) Bash(mkdir *) Bash(ls *) Bash(cat *) Bash(rg *)}"
DEV_MAX="${RALPH_DEV_MAX:-20}"
QA_PASSES="${RALPH_QA_PASSES:-1}"
CYCLES="${RALPH_CYCLES:-5}"
DRY_LIMIT=2          # consecutive clean QA passes that mean "converged"

log() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# ---------------------------------------------------------------- preflight --

[[ "$MODE" == "-h" || "$MODE" == "--help" ]] && { sed -n '3,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

command -v claude >/dev/null || { echo "claude CLI not found on PATH" >&2; exit 1; }
[[ -f TASKS.md ]]        || { echo "TASKS.md not found — run from the repo root" >&2; exit 1; }
[[ -f prompts/dev.md ]]  || { echo "prompts/dev.md not found" >&2; exit 1; }
[[ -f prompts/qa.md ]]   || { echo "prompts/qa.md not found" >&2; exit 1; }

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$BRANCH" == "main" || "$BRANCH" == "master" ]]; then
  cat >&2 <<'EOF'
Refusing to run on the deploy branch.

Ralph produces broken intermediate states by design — it converges over many
iterations rather than being correct at each one. That belongs on a branch you
can throw away:

    git switch -c ralph/phase-0

EOF
  exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Working tree is dirty. Commit or stash first — one iteration must be one clean commit." >&2
  exit 1
fi

# ------------------------------------------------------- the security boundary

# Defined ONCE, used by every mode. An unattended agent must not hold
# deployment or admin credentials: this repo deploys to a company Dokploy
# instance and $GITEA_ADMIN_TOKEN is admin-scoped.
#
# Add any new secret to THIS list. There is deliberately no second copy to
# forget about.
run() {  # $1 = prompt file, $2 = model
  env -u DOKPLOY_OPS_PANEL_TOKEN \
      -u DOKPLOY_DOC_PANEL_TOKEN \
      -u DOKPLOY_SARON_TOKEN \
      -u DOKPLOY_ELNOTE_STAGING_TOKEN \
      -u GITEA_ADMIN_TOKEN \
      -u GITEA_LFS_S3_ACCESS_KEY \
      -u GITEA_LFS_S3_SECRET_KEY \
      -u GH_TOKEN \
      -u GITHUB_TOKEN \
      claude -p "$(cat "$1")" \
             --model "$2" \
             --permission-mode acceptEdits \
             --allowedTools "$ALLOWED_TOOLS"
}

# ------------------------------------------------------------------ helpers --

# True when at least one unchecked task is not marked [human].
#
# Counts rather than relying on grep's exit code on purpose: /usr/bin/grep is
# ugrep on some machines (including this one), and its exit status for -v
# reflects whether the *pattern* matched rather than whether inverted output
# was produced — so `grep -qv` returns 1 even when non-matching lines exist.
# Comparing a count is correct under both GNU grep and ugrep.
open_tasks() {
  local n
  n="$( grep -E '^[[:space:]]*- \[ \]' TASKS.md | grep -vc '\[human\]' || true )"
  [[ "${n:-0}" -gt 0 ]]
}

# TASKS.md is the QA loop's only output, so its hash is the convergence signal.
fingerprint() { md5sum TASKS.md | cut -d' ' -f1; }

# -------------------------------------------------------------------- loops --

dev_loop() {
  local max="$1" i=0
  while (( i < max )); do
    if ! open_tasks; then log "[dev] queue empty"; return 0; fi
    i=$(( i + 1 ))
    log "[dev] iteration $i/$max   model=$DEV_MODEL"
    run prompts/dev.md "$DEV_MODEL" || log "[dev] iteration exited non-zero — continuing"
  done
  log "[dev] hit the iteration cap ($max) with tasks still open"
}

# Returns 0 if the pass added findings, 1 if it found nothing.
qa_pass() {
  local before after
  before="$(fingerprint)"
  log "[qa] review pass   model=$QA_MODEL"
  run prompts/qa.md "$QA_MODEL" || log "[qa] pass exited non-zero — continuing"
  after="$(fingerprint)"
  [[ "$before" != "$after" ]]
}

auto_loop() {
  local cycle=0 dry=0
  while (( cycle < CYCLES && dry < DRY_LIMIT )); do
    cycle=$(( cycle + 1 ))
    log "══════ cycle $cycle/$CYCLES ══════"
    dev_loop "$DEV_MAX"
    if qa_pass; then
      dry=0
      log "[auto] QA added findings — back to dev"
    else
      dry=$(( dry + 1 ))
      log "[auto] QA found nothing ($dry/$DRY_LIMIT clean passes)"
    fi
  done
  if (( dry >= DRY_LIMIT )); then
    log "[auto] converged — $DRY_LIMIT consecutive clean QA passes"
  else
    log "[auto] stopped at the cycle cap ($CYCLES); NOT converged — tasks remain"
  fi
}

# ----------------------------------------------------------------- dispatch --

case "$MODE" in
  dev)  dev_loop "${N:-$DEV_MAX}" ;;
  qa)   for (( p = 1; p <= ${N:-$QA_PASSES}; p++ )); do
          qa_pass || log "[qa] no findings"
        done ;;
  auto) auto_loop ;;
  *)    echo "usage: $0 [dev|qa|auto] [N]   (try --help)" >&2; exit 1 ;;
esac

log "done — review with:  git log --oneline  &&  git diff main..HEAD"
