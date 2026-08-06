"""Guards over docker-compose.yml and the Dockerfile they share.

The stack has one failure mode that reports nothing: a cron sidecar running in
UTC sends the 21:00 IST summary at 02:30 IST (DECISIONS §10). Both the zone
name and the zone *files* are needed for that, and neither absence errors. The
other assertions pin decisions these two files are the only record of.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
COMPOSE = (ROOT / "docker-compose.yml").read_text()
DOCKERFILE = (ROOT / "Dockerfile").read_text()
ENV_EXAMPLE = (ROOT / ".env.example").read_text()
SERVICES = yaml.safe_load(COMPOSE)["services"]

def _env_pairs(text):
    """Every `key=value` in a .env, as (key, value) — a list, not a dict.

    Parsed, not pattern-matched: an anchored `^(\\w+)=(.*)$` regex passed on
    four ordinary spellings that put a real token in the committed file —
    `export KEY=…`, a leading indent, spaces around `=`, and a duplicate key
    whose valued line is shadowed by an empty one (`dict()` kept the empty).
    A list keeps the shadowed pair so a value hiding above a blank still fails.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        yield key.strip(), value.strip()


ENV_PAIRS = list(_env_pairs(ENV_EXAMPLE))
ENV_KEYS = {k for k, _ in ENV_PAIRS}

# Vars compose interpolates from the environment: `${VAR...}` or bare `$VAR`,
# but not `$$VAR` (compose's escape for a literal `$` handed to the shell).
COMPOSE_VARS = set(re.findall(r"(?<!\$)\$\{?(\w+)", COMPOSE))

# Keys compose builds or consumes without interpolating them: DATABASE_URL is
# assembled from the POSTGRES_* keys and can also be set directly to run outside
# compose (.env.example documents that). Everything else must reach a service.
COMPOSE_DERIVED = {"DATABASE_URL"}


def service(name: str) -> str:
    """One service's block, from its key to the next key at that indent."""
    block = re.search(rf"^  {name}:\n(.*?)(?=^  \S|^\S|\Z)", COMPOSE, re.M | re.S)
    assert block, f"docker-compose.yml defines no {name} service"
    return block.group(1)


def test_stack_has_web_db_and_cron():
    for name in ("web", "db", "cron"):
        assert service(name)


def test_cron_runs_in_asia_kolkata():
    assert re.search(r"^\s+TZ: Asia/Kolkata$", service("cron"), re.M)


def test_image_ships_zone_files():
    """`TZ: Asia/Kolkata` is a silent no-op if /usr/share/zoneinfo lacks it.

    glibc falls back to UTC wall-clock with exit 0 and no warning, so the TZ
    assertion above stays green while the sidecar runs 5.5 hours off.
    """
    assert re.search(r"apt-get install\b[^\n]*\btzdata\b", DOCKERFILE)


def test_no_service_publishes_beyond_loopback():
    """This file is pasted into Dokploy on a shared host (DECISIONS §14).

    A 0.0.0.0 binding answers /telegram/webhook over plaintext :8000 from
    anywhere, bypassing the HTTPS domain, and nothing about it looks wrong.
    Every service, not just web: publishing 5432 to debug against the deployed
    database is a one-line edit that puts Postgres on the host's public
    interface with the credentials from .env.

    Read through the YAML parser, not a regex over the text. Two rounds of QA
    went on spellings a regex missed while the file really did publish the
    port — quotes, a trailing `# debug`, four-space list items, long syntax,
    a service the guard's hardcoded list had never heard of. The parser sees
    what Docker sees, so all of those collapse into one comparison.
    """
    found = 0
    for name, svc in SERVICES.items():
        for published in svc.get("ports") or []:
            found += 1
            # str(): long syntax parses to a dict, a bare port to an int.
            assert str(published).startswith("127.0.0.1:"), f"{name}: {published}"
    assert found, "no published port found — web's ports: block is gone"


def test_env_example_lists_every_key_compose_interpolates():
    """.env.example is the only list of what the operator has to set.

    A key that exists in the compose file but not here is invisible until
    `compose.saveEnvironment` is filled in from this file and the deploy dies
    on `:?see .env.example` — a message pointing at a file that never had it.
    """
    for name in COMPOSE_VARS:
        assert name in ENV_KEYS, f"{name} is interpolated by compose but absent"


def test_env_example_lists_no_key_no_service_consumes():
    """The reverse: a key the operator is told to set that no service reads.

    The gap is silent — `up` succeeds because the keys that are wired are the
    only ones checked, the operator sees all six set correctly on Dokploy, and
    the KeyError surfaces phases later inside the container (parse.py reads
    OPENROUTER_API_KEY, the webhook reads TELEGRAM_BOT_TOKEN). The evidence
    points at the app; the cause is a compose file that never asked for it.
    """
    for name in ENV_KEYS:
        assert name in COMPOSE_VARS or name in COMPOSE_DERIVED, (
            f"{name} is in .env.example but no service consumes it"
        )


def test_env_example_holds_no_values():
    """This file is committed (.gitignore negates it out of *.env).

    Filling a value in during local debugging commits the secret, and nothing
    about the diff looks different from the placeholder it replaced.
    """
    assert ENV_PAIRS, ".env.example has no keys — the guard would pass vacuously"
    for name, value in ENV_PAIRS:
        assert not value, f"{name} carries a value"


def test_web_and_cron_share_one_image():
    """DECISIONS §8: same image, different command.

    A separate build for the sidecar is how it ends up running last week's
    job code against this week's schema.
    """
    for name in ("web", "cron"):
        assert re.search(r"^\s+build: \.$", service(name), re.M), name


def test_web_migrates_before_it_serves():
    """Otherwise /healthz answers 200 on an unmigrated database and the first
    webhook dies on `relation "transactions" does not exist`.

    Asserted against the Dockerfile rather than compose: the deployed Dokploy
    Application carries no command override, so the ordering has to be a
    property of the image or it only holds locally.
    """
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    cmd = [ln for ln in dockerfile.splitlines() if ln.startswith("CMD ")][-1]
    assert re.search(r"kanakko\.migrate.*&&.*uvicorn", cmd)


def test_image_is_runnable_without_an_external_command():
    """The image must start on its own.

    Dokploy's per-service `command` is an argv array split on whitespace, so a
    shell-form command set there arrives mangled and the container crash-loops
    with "Unterminated quoted string". Relying on an override to make the image
    runnable is what made that possible; an exec-form CMD removes the dependency.
    """
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    cmd_lines = [ln for ln in dockerfile.splitlines() if ln.startswith("CMD ")]
    assert cmd_lines, "Dockerfile has no CMD — the image cannot start unaided"
    assert cmd_lines[-1].startswith('CMD ['), (
        "CMD must be exec form (a JSON array); shell form is re-split by the "
        f"runtime: {cmd_lines[-1]!r}"
    )
