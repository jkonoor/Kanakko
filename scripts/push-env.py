#!/usr/bin/env python3
"""Push local .env secrets to Dokploy and register the Telegram webhook.

Values go straight from your machine to Dokploy and Telegram. Nothing is
printed — this script reports key *names* and lengths, never contents.

    export DOKPLOY_DOC_PANEL_TOKEN=...      # already in your shell profile
    python3 scripts/push-env.py             # --dry-run to see what it would do

DATABASE_URL is read back from the managed Postgres rather than taken from
.env: the local file describes the compose stack, and the deployed apps use a
different database. Resource IDs come from docs/DEPLOYMENT.md.
"""

import json
import os
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path

API = "https://doc-panel.innogenio.com/api"
WEB = "xvcTr0x3RmHLzUHAuH1Mn"
CRON = "ZW25YvpB8Edv-nCfh6Jwz"
POSTGRES = "y8iECURcqfeVsYkaNkcCt"
WEBHOOK_URL = "https://kanakko-web-kmeizk-a87806-49-12-44-133.sslip.io/webhook"

# DECISIONS §15: Telegram accepts A-Za-z0-9_- only, 1-256 chars. A secret with
# a `.` or `~` is silently refused by setWebhook, leaving the bot with no
# webhook at all rather than an obviously broken one.
SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")

DRY = "--dry-run" in sys.argv


def read_env(path=".env"):
    """.env as a dict. Parsed line by line, not regexed — CLAUDE.md."""
    out = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        out[key.strip()] = value.strip()
    return out


def api(endpoint, body=None):
    url = f"{API}/{endpoint}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method="POST" if data else "GET",
        headers={"x-api-key": TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read() or "null")


TOKEN = os.environ.get("DOKPLOY_DOC_PANEL_TOKEN")
if not TOKEN:
    sys.exit("DOKPLOY_DOC_PANEL_TOKEN is not set")

env = read_env()

missing = [k for k in ("TELEGRAM_BOT_TOKEN", "OPENROUTER_API_KEY",
                       "TELEGRAM_WEBHOOK_SECRET") if not env.get(k)]
if missing:
    sys.exit(f"fill these in .env first: {', '.join(missing)}")

secret = env["TELEGRAM_WEBHOOK_SECRET"]
if not SECRET_RE.match(secret):
    sys.exit("TELEGRAM_WEBHOOK_SECRET must be 1-256 chars of A-Za-z0-9_- "
             "(DECISIONS §15) — setWebhook rejects anything else")

pg = api(f"postgres.one?postgresId={POSTGRES}")
dsn = (f"postgresql://{pg['databaseUser']}:{pg['databasePassword']}"
       f"@{pg['appName']}:5432/{pg['databaseName']}")

web_env = {
    "DATABASE_URL": dsn,
    "TELEGRAM_BOT_TOKEN": env["TELEGRAM_BOT_TOKEN"],
    "TELEGRAM_WEBHOOK_SECRET": secret,
    "OPENROUTER_API_KEY": env["OPENROUTER_API_KEY"],
    "OPENROUTER_MODEL": env.get("OPENROUTER_MODEL", ""),
}
# cron sends summaries, so it needs the bot token; it never parses, so it does
# not get the OpenRouter key. TZ is load-bearing (DECISIONS §10).
cron_env = {
    "DATABASE_URL": dsn,
    "TELEGRAM_BOT_TOKEN": env["TELEGRAM_BOT_TOKEN"],
    "TZ": "Asia/Kolkata",
}


def render(d):
    return "\n".join(f"{k}={v}" for k, v in d.items())


for name, app_id, values in (("kanakko-web", WEB, web_env),
                             ("kanakko-cron", CRON, cron_env)):
    keys = ", ".join(values)
    if DRY:
        print(f"would set on {name}: {keys}")
        continue
    api("application.update", {"applicationId": app_id, "env": render(values)})
    print(f"set on {name}: {keys}")

# Register the webhook so Telegram sends the secret header with every update.
if DRY:
    print(f"would setWebhook -> {WEBHOOK_URL} (with secret_token)")
else:
    body = json.dumps({
        "url": WEBHOOK_URL,
        "secret_token": secret,
        "allowed_updates": ["message", "callback_query"],
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{env['TELEGRAM_BOT_TOKEN']}/setWebhook",
        data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            result = json.loads(r.read())
        print(f"setWebhook: ok={result.get('ok')} {result.get('description', '')}")
    except urllib.error.HTTPError as e:
        sys.exit(f"setWebhook failed: {e.code} {e.read().decode()[:200]}")

    for name, app_id in (("kanakko-web", WEB), ("kanakko-cron", CRON)):
        api("application.deploy", {"applicationId": app_id})
        print(f"redeployed {name}")

    print("\nDone. Verify with:\n"
          f"  curl -s https://api.telegram.org/bot$(grep '^TELEGRAM_BOT_TOKEN=' .env "
          "| cut -d= -f2-)/getWebhookInfo | python3 -m json.tool")
