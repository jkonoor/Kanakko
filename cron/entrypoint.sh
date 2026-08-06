#!/bin/sh
# Entry point for the cron sidecar (docker-compose.yml `cron` service; on
# Dokploy, kanakko-cron's command). DECISIONS §8.
#
# cron runs every job with a stripped environment — none of the container's
# variables (DATABASE_URL, TELEGRAM_BOT_TOKEN) reach a job. So dump the current
# environment to a file that each crontab line sources, then hand off to cron in
# the foreground as PID 1 (its stdout becomes the container log; the jobs
# redirect there so their one log line is visible).
set -e

# CRON_ENV_FILE is overridable only so the test can point it at a temp file;
# production always uses the default.
: "${CRON_ENV_FILE:=/app/cron.env}"

# Single-quote each value so $, ;, spaces etc. stay literal when sourced; a
# literal ' inside a value becomes '\'' . umask keeps the file (it holds
# secrets) readable only by root.
umask 077
printenv | while IFS='=' read -r name value; do
    esc=$(printf '%s' "$value" | sed "s/'/'\\\\''/g")
    printf "export %s='%s'\n" "$name" "$esc"
done > "$CRON_ENV_FILE"

exec cron -f
