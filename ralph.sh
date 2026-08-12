#!/usr/bin/env bash
#
# ralph.sh — drive Kanakko forward one task per iteration.
#
# Each iteration runs two passes:
#   1. implementer (prompts/dev.md) — fixes open QA findings, else does the
#      next task in TASKS.md
#   2. QA          (prompts/qa.md)  — reviews that commit into REVIEWS.md
#
# QA runs after EVERY implementer pass, not after a batch, so a defect survives
# at most one iteration before it becomes the next iteration's first job. The
# implementer is handed the branch and recent commits and stays on that branch —
# it never creates one. Stops when the implementer emits COMPLETE.
#
#   ./ralph.sh [N]        paired loop, N iterations   (default 10)
#   ./ralph.sh dev [N]    implementer passes only
#   ./ralph.sh qa  [N]    review passes only
#
# docs/DECISIONS.md is the spec. TASKS.md is the queue. REVIEWS.md is the
# review log. Each pass is a fresh context — the repo is the memory.
#
# Exit codes (supervise.sh depends on these):
#   0  COMPLETE — nothing left but `[human]` tasks. Stop.
#   1  Stalled — a pass committed nothing. Usually an exhausted session limit;
#      occasionally a genuine blocker. Worth retrying after a wait.
#   2  Iteration cap reached, work remains. Restart immediately.
#
# There is no BLOCKED code: prompts/dev.md skips `[human]` tasks and reports
# "only those remain" as COMPLETE, so the case exists but arrives as 0.
#
# Overridable: RALPH_DEV_MODEL RALPH_QA_MODEL RALPH_ALLOWED_TOOLS

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

MODE="loop"
case "${1:-}" in
  dev|qa|loop) MODE="$1"; shift ;;
  -h|--help)   sed -n '3,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
esac
MAX="${1:-10}"

DEV_MODEL="${RALPH_DEV_MODEL:-claude-opus-4-8}"
QA_MODEL="${RALPH_QA_MODEL:-claude-opus-4-8}"

# Least privilege. The loop runs unattended, so anything not listed here stalls
# rather than prompting. Widen it deliberately when a task genuinely needs
# something, rather than reaching for --dangerously-skip-permissions.
# `gh` is absent on purpose: its credentials live in the keyring, not the
# environment, so the scrub below cannot revoke them.
ALLOWED_TOOLS="${RALPH_ALLOWED_TOOLS:-Edit Write Read Grep Glob Bash(git *) Bash(uv *) Bash(python *) Bash(pytest *) Bash(mkdir *) Bash(ls *) Bash(cat *) Bash(rg *)}"

log() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# ---------------------------------------------------------------- preflight --

command -v claude >/dev/null || { echo "claude CLI not found on PATH" >&2; exit 1; }
[[ "$MAX" =~ ^[0-9]+$ ]]     || { echo "iterations must be a number" >&2; exit 1; }
for f in TASKS.md REVIEWS.md prompts/dev.md prompts/qa.md; do
  [[ -f "$f" ]] || { echo "$f not found — run from the repo root" >&2; exit 1; }
done

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$BRANCH" == "main" || "$BRANCH" == "master" || "$BRANCH" == "develop" ]]; then
  cat >&2 <<'EOF'
Refusing to run on main or develop.

develop is the deploy branch — a push to it builds an image and redeploys.

Ralph converges over many iterations rather than being correct at each one, so
it belongs on a branch you can throw away:

    git switch -c ralph/phase-0

EOF
  exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Working tree is dirty. Commit or stash first — one pass must be one clean commit." >&2
  exit 1
fi

# ------------------------------------------------------- the security boundary

# Defined ONCE, used by every pass. An unattended agent must not hold
# deployment or admin credentials: this repo deploys to a company Dokploy
# instance and $GITEA_ADMIN_TOKEN is admin-scoped.
#
# Add any new secret to THIS list. There is deliberately no second copy.
run() {  # $1 = prompt text (not a path), $2 = model
  env -u DOKPLOY_OPS_PANEL_TOKEN \
      -u DOKPLOY_DOC_PANEL_TOKEN \
      -u DOKPLOY_SARON_TOKEN \
      -u DOKPLOY_ELNOTE_STAGING_TOKEN \
      -u GITEA_ADMIN_TOKEN \
      -u GITEA_LFS_S3_ACCESS_KEY \
      -u GITEA_LFS_S3_SECRET_KEY \
      -u GH_TOKEN \
      -u GITHUB_TOKEN \
      claude -p "$1" \
             --model "$2" \
             --permission-mode acceptEdits \
             --allowedTools "$ALLOWED_TOOLS"
}

# ------------------------------------------------------------------- passes --

# Hands the implementer its branch and recent history so it doesn't re-derive
# them, and so it knows not to branch.
dev_pass() {  # echoes the agent's output; caller checks for the sentinel
  local prompt
  prompt="Current branch (do NOT create another): $(git rev-parse --abbrev-ref HEAD)
Recent commits:
$(git log --oneline -n 15 2>/dev/null || echo none)

$(cat prompts/dev.md)"
  run "$prompt" "$DEV_MODEL" 2>&1 | tee /dev/stderr
}

qa_pass() {
  run "$(cat prompts/qa.md)" "$QA_MODEL" 2>&1 || log "[qa] pass exited non-zero — continuing"
}

# -------------------------------------------------------------------- loops --

for (( i = 1; i <= MAX; i++ )); do
  if [[ "$MODE" != "qa" ]]; then
    log "══ iteration $i/$MAX — implementer   model=$DEV_MODEL"
    before="$(git rev-parse HEAD)"
    output="$(dev_pass)" || log "[dev] pass exited non-zero — continuing"
    # A pass that both failed and committed nothing produced nothing, and the
    # next one will fail the same way — an exhausted account session limit is
    # the usual cause, and "continuing" burns the whole cap in seconds. Stop and
    # let a person decide, rather than spending 8 iterations on nothing.
    if [[ "$(git rev-parse HEAD)" == "$before" ]] \
       && ! grep -qF '<promise>COMPLETE</promise>' <<<"$output"; then
      log "implementer produced no commit — stopping at iteration $i. Last output:"
      tail -n 3 <<<"$output" >&2
      exit 1
    fi
    if grep -qF '<promise>COMPLETE</promise>' <<<"$output"; then
      log "COMPLETE emitted — every task done and no open findings. Stopping at iteration $i."
      exit 0
    fi
  fi

  if [[ "$MODE" != "dev" ]]; then
    log "══ iteration $i/$MAX — QA            model=$QA_MODEL"
    qa_pass
  fi
done

log "reached the iteration cap ($MAX) without COMPLETE — work remains"
log "review with:  git log --oneline  &&  cat REVIEWS.md"

# Exit 2, not 0: "there is more to do" must be distinguishable from "everything
# is done", or a supervisor stops the phase the first time it hits the cap.
exit 2
