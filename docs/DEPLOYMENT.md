# Deployment — Innogenio `doc-panel` Dokploy

Facts gathered 2026-08-05 from the Innogenio DevOps repo
(`~/Innogenio/unassigned/DevOps/DevOps/docs/dokploy/`) and verified against the
live API. That repo is the upstream source of truth for the instance; this file
records only what Kanakko needs and what was learned first-hand.

## Instance

| | |
|---|---|
| Dashboard | `https://doc-panel.innogenio.com/` |
| Host | `doc-panel-dokploy` — 49.12.44.133 |
| API base | `https://doc-panel.innogenio.com/api` |
| Auth | header `x-api-key: $DOKPLOY_DOC_PANEL_TOKEN` |
| Firewall | Hetzner Cloud Firewall at the edge; 80/443 public, SSH restricted |
| Under the hood | Docker Swarm + Traefik |

> **This instance is owned by the wider DevOps team**, not by this project.
> Confirm before changing projects, domains, environment variables, remote
> servers, or deployments. Use the `doc-panel` token only — never an
> `ops-panel` or `saron` token against this host.

The token lives in the operator's shell profile. **Never commit it.** Check
presence without printing the value:

```bash
env | awk -F= '/^DOKPLOY_/ {print $1"=<set>"}' | sort
```

Existing projects on this instance (do not disturb): `Innogenio`,
`fixed-asset`, `shared-mariadb`, `vpn`, `CineApp`, `N8N`, `ace`, `insurance`.
Kanakko gets its own project.

## Deploy sequence

Dokploy is driven entirely over REST. The working order:

1. `project.create` — a `Kanakko` project
2. `compose.create` — **`sourceType: "raw"`** with the compose file inline,
   `composeType: "docker-compose"`
3. `compose.saveEnvironment` — bot token, OpenRouter key, DB credentials
4. `domain.create` — hostname + Let's Encrypt
5. `compose.deploy`

### Gotchas, all learned the hard way upstream

- **`sourceType` defaults to a git provider.** Omitting `sourceType: "raw"`
  fails at deploy time with `Github Provider not found`, not at create time.
- **`compose.deployTemplate` is broken on this Dokploy image** — it 500s with
  `Template files not found`. Deploy manually via the sequence above.
- **`appName` gets a random suffix.** Container names are
  `<appName>-<suffix>-<service>-N`, not `<appName>-<service>-N`. Don't
  hardcode container names anywhere.
- **Every redeploy returns 502/503 for ~30–60 seconds** while containers
  recreate. This is normal. Wait for a 200 before concluding a deploy failed.

## Scheduling — Dokploy will not do this for you

**Dokploy has no general-purpose task scheduler.** Its only cron-like feature is
database backups (`backup.create`), executed by `node-schedule` *inside*
Dokploy. There is no "run this command on a schedule" primitive.

Kanakko's three reminder jobs therefore run in a **cron sidecar container** in
the compose stack: same image as `web`, command runs `cron`, crontab holds the
three entries. It deploys and versions with the app and needs no host access —
which matters, because adding root crontab entries to a team-owned box for a
personal project is not appropriate.

Set the container `TZ=Asia/Kolkata` so the crontab's wall-clock times mean what
they say (see DECISIONS §10).

## Backups

Dokploy's backup system does exactly what Kanakko needs, so don't hand-roll a
`pg_dump` cron.

**An S3 destination already exists on this instance:** `Dokploy Buckets`,
bucket `dokploy-backup`, provider `AWS`. Either reuse it or create a Kanakko
destination — confirm with the DevOps team which is appropriate.

> **Documentation discrepancy found 2026-08-05, worth fixing upstream:**
> `docs/dokploy/backups.md` line 7 states *"`doc-panel.innogenio.com` has no
> backups configured"*, while `docs/dokploy/README.md` says scheduled S3
> backups *are* configured on this instance. The API confirms at least one
> destination exists. The two docs disagree; treat neither as authoritative
> until reconciled.

### The gotcha that silently breaks a compose-embedded DB backup

Kanakko's Postgres runs *inside* the compose stack, so the backup is
`backupType: "compose"` — and Dokploy then has no credential record to read
from. It expects credentials in a `metadata` field:

```json
{"metadata": {"postgres": {"databaseUser": "kanakko"}}}
```

**Without `metadata`, `generateBackupCommand` returns `null` and the backup run
shells out to the literal string `null`** — `/bin/bash: line 15: null: command
not found`. The API returns a bare 400 and the real reason appears *only in the
deployment log*. This has bitten this team before.

Also: **`backup.update` requires the full field set on every call.** A partial
payload is rejected by Zod with "expected string, received undefined" for each
omitted field. Always re-send `schedule`, `enabled`, `prefix`, `destinationId`,
`database`, `keepLatestCount`, `serviceName`, and `databaseType`.

### Prove the restore

A backup that has never been restored is not a backup. Trigger one manually
(`POST /api/backup.manualBackupCompose`, body `{"backupId": "..."}`), restore it
into a scratch database, and verify the row count. Phase 5 isn't done until
this has happened once.

## Secrets

Bot token, OpenRouter API key, and database credentials go in Dokploy's
environment for the compose stack (`compose.saveEnvironment`) — never in the
repository, never in the compose file committed here. The repo carries a
`.env.example` with keys and empty values only.
