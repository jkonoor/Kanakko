# Deployment — Dokploy

Verified against the live Dokploy API (v0.29.7). Host-specific values —
hostnames, IPs and resource IDs — live in `deploy.local.env`, which is
gitignored; the placeholders below stand in for them.

## Instance

| | |
|---|---|
| Dashboard | `https://<dokploy-host>/` |
| Host | `<dokploy-host>` |
| API base | `https://<dokploy-host>/api` |
| Auth | header `x-api-key: $DOKPLOY_DOC_PANEL_TOKEN` |
| Firewall | Hetzner Cloud Firewall at the edge; 80/443 public, SSH restricted |
| Under the hood | Docker Swarm + Traefik |

> **The instance is shared and not owned by this project.** Confirm before
> changing projects, domains, environment variables or deployments, and use
> only the token issued for it.

The token lives in the operator's shell profile. **Never commit it.** Check
presence without printing the value:

```bash
env | awk -F= '/^DOKPLOY_/ {print $1"=<set>"}' | sort
```

The instance hosts unrelated projects; this one lives in its own Dokploy
project and touches nothing else.

## Provisioned resources

Created 2026-08-06 via the REST API. IDs are needed for every subsequent call.

| Resource | ID | Internal host / notes |
|---|---|---|
| Project `Kanakko` | `<project-id>` | |
| Environment `production` | `<environment-id>` | `isDefault` — services attach here, not to the project |
| `kanakko-db` (Postgres 16-alpine) | `<postgres-id>` | host **`<db-internal-host>`**, db/user `kanakko` |
| `kanakko-web` (Application) | `<web-app-id>` | appName `<web-appname>` |
| `kanakko-cron` (Application) | `<cron-app-id>` | appName `<cron-appname>` |

**API notes for v0.29.7**, learned the hard way here:

- `project.create` returns `{"project": {...}, "environment": {...}}` — the IDs
  are nested, not top level.
- Services hang off `environmentId`, not `projectId`. `project.one` returns them
  under `environments[].applications` / `.postgres` / `.compose`.
- `application.saveEnvironment` rejects `{applicationId, env}` with a Zod
  validation error. **Use `application.update` with an `env` field instead** —
  it accepts arbitrary application fields, including `command`.
- `application.saveDockerProvider` takes
  `{applicationId, dockerImage, registryUrl, username, password}`.
- The deploy webhook token is the application's `refreshToken`.

## Topology

**Not a compose stack.** Three Dokploy resources in one `Kanakko` project:

| Resource | Kind | Notes |
|---|---|---|
| `kanakko-db` | Dokploy **Postgres** service | Managed by Dokploy, so backups are native (see below) |
| `kanakko-web` | **Application**, Docker provider | `ghcr.io/jkonoor/kanakko:latest`, runs uvicorn. Holds the domain |
| `kanakko-cron` | **Application**, Docker provider | Same image, command `sh /app/cron/entrypoint.sh`, `TZ=Asia/Kolkata` |

`docker-compose.yml` in the repo root stays as the **local development** stack
(`docker compose up --build`). It is no longer what gets deployed — keep the two
in step by hand when services or environment keys change.

Why separate services rather than the compose file: this instance has **no
GitHub git provider** (`github.githubProviders` returns `[]`; the only provider
is Gitea), so Dokploy cannot clone this repo to run `build: .`. Images are built
in GitHub Actions instead and pulled from GHCR. That also makes the database a
Dokploy-managed resource, which removes the compose-backup gotcha entirely.

### Registry credentials are per service

Each Application's **Provider → Docker** tab takes the image, registry URL,
username, and password directly. A private GHCR package therefore needs no
entry in Dokploy's global registry list:

| Field | Value |
|---|---|
| Docker Image | `ghcr.io/jkonoor/kanakko:latest` |
| Registry URL | `ghcr.io` |
| Username | `jkonoor` |
| Password | A GitHub PAT with **`read:packages`** — pull only, no write |

The instance's global registry entries belong to other work and are not used
here — the per-service credentials above are self-contained.

## CI/CD

`.github/workflows/deploy.yml`, modelled on the `saron-erp` builder pattern in
the DevOps repo:

```
push to main ─▶ test (uv run pytest)
                 └─▶ build ─▶ ghcr.io/jkonoor/kanakko:latest (+ :buildcache)
                       └─▶ POST $DOKPLOY_DEPLOY_WEBHOOK
                             └─▶ Dokploy pulls the image and redeploys
```

- Pushing to GHCR uses the workflow's own `GITHUB_TOKEN` with
  `packages: write` — **no PAT needed for the push.** The PAT above is only for
  Dokploy to *pull*.
- Only `:latest` (re-pushed in place) and `:buildcache` are pushed, so nothing
  accumulates and there is no cleanup job to fail — the "simpler alternative"
  in the DevOps repo's `gitea/workflows.md`. Adding per-build tags means adding
  a prune job with them.
- Repo secret **`DOKPLOY_DEPLOY_WEBHOOK`** — copy the URL from the
  `kanakko-web` service's UI. Until it is set the workflow still builds and
  pushes, and warns instead of deploying.
- The deploy step prints the HTTP status and **fails on `000`**. A silent pass
  there is how the `saron-erp` CD hid a runner-DNS failure for a while.

### Gotchas

- **`appName` gets a random suffix.** Container names are
  `<appName>-<suffix>-…`. Don't hardcode container names anywhere.
- **Every redeploy returns 502/503 for ~30–60 seconds** while containers
  recreate. This is normal. Wait for a 200 before concluding a deploy failed.
- **`compose.deployTemplate` is broken on this Dokploy image** (500s with
  `Template files not found`) — irrelevant now, but don't reach for it later.
- **`sourceType` defaults to a git provider** on `compose.create`. Only matters
  if something is ever deployed here as a raw compose file.

## Scheduling — Dokploy will not do this for you

**Dokploy has no general-purpose task scheduler.** Its only cron-like feature is
database backups (`backup.create`), executed by `node-schedule` *inside*
Dokploy. There is no "run this command on a schedule" primitive.

Kanakko's three reminder jobs therefore run in a **cron sidecar container** in
the compose stack: same image as `web`, command `sh /app/cron/entrypoint.sh`
(dumps the container env to a file the crontab sources, then execs `cron -f` —
cron does not pass the container's env to jobs), and `cron/kanakko.crontab`
(installed to `/etc/cron.d/kanakko`) holds the three entries. It deploys and
versions with the app and needs no host access — which matters, because adding
root crontab entries to a team-owned box for a personal project is not
appropriate.

Set the container `TZ=Asia/Kolkata` so the crontab's wall-clock times mean what
they say (see DECISIONS §10).

## Backups

Dokploy's backup system does exactly what Kanakko needs, so don't hand-roll a
`pg_dump` cron.

**An S3 destination already exists on this instance:** `Dokploy Buckets`,
bucket `dokploy-backup`, provider `AWS`. Either reuse it or create a Kanakko
destination — confirm with the DevOps team which is appropriate.


### The compose-backup gotcha no longer applies — keep it in mind anyway

Because `kanakko-db` is a **Dokploy-managed Postgres resource** rather than a
container inside a compose stack, its backup is `backupType: "database"` and
Dokploy already holds the credentials. The `metadata` trap below is therefore
**avoided by the topology**, which is a real argument for it:

> For `backupType: "compose"`, Dokploy has no credential record and expects
> `{"metadata": {"postgres": {"databaseUser": "…"}}}`. Without it,
> `generateBackupCommand` returns `null`, the run shells out to the literal
> string `null` (`/bin/bash: line 15: null: command not found`), the API
> returns a bare 400, and the real reason appears *only in the deployment log*.
> This has bitten this team before.

If the stack is ever collapsed back into a single compose deployment, that trap
returns.

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

## BotFather — settings that live outside the repo

These are configured against @BotFather (or the equivalent Bot API method) and
exist nowhere in git. **Anything the bot's own text refers to is coupled to a
value no test can see**, so the current values are written here; change one and
this file changes with it.

| Setting | Current value | Why it is written down |
|---|---|---|
| Menu button label | **`Dashboard`** | `HELP_TEXT` and `WELCOME` say "Tap Dashboard at the bottom-left of the chat". Naming the button exactly as it reads on screen saves the user decoding "the menu button" — and if the label is ever changed, that sentence goes stale silently, with nothing going red. |
| Menu button URL | the Mini App `/app` URL | Registered in Phase 4; verified by the Mini App opening and its `initData` HMAC validating against real Telegram payloads. |
| Command list (`/setcommands`) | see below | The "/" menu is where a Telegram user looks first. It must match `HELP_TEXT`'s list or the two disagree about what the bot can do. |
| Description | shown *before* a user presses Start | The first sentence any new tester reads, on the empty chat screen. |
| About | profile text | — |

The command list must stay in step with `kanakko/handlers.py::HELP_TEXT`:

```
start - Start using Kanakko
help - What I can do
undo - Remove your last entry
refund - Money back on something you bought
account - Set an account's balance, or check a savings pot
recurring - Set up a monthly auto-debit
household - Who's in your household
invite - Add someone to your household (owner only)
remove - Remove a member, or leave
transfer - Hand over household ownership
delete_account - Erase your account and all your data
```

Ordered by how often it is used, not alphabetically — the "/" menu is a list a
user scans, and `/undo` and `/refund` are reached far more often than
`/transfer`. `refund` earns its slot only because it is now `/refund` as well as
the bare word: BotFather registers commands, not vocabulary.

`/remove` and `/transfer` are listed deliberately. Leaving a destructive command
out of the menu does not make it safer — it makes it unfindable — and `/remove`
already asks Keep-or-Delete before it acts.

**`/invite_signup` is deliberately absent** from that list and from `HELP_TEXT`.
It is operator-only (gated on `ADMIN_TELEGRAM_IDS`, see DECISIONS §16), and a
command in the "/" menu that refuses almost everyone who taps it is worse than an
unlisted one. It works when typed regardless — Telegram does not require a command
to be registered, only to be spelled with `a-z 0-9 _`, which is why the name
carries an underscore and not the hyphen it reads with.
