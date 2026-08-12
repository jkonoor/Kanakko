"""Guards over the cron sidecar: the crontab and its entrypoint.

Both failure modes here are silent. A crontab in the wrong /etc/cron.d format
(no user field) makes cron log an error to a place no one reads and skip every
job. And cron runs each job with a *stripped* environment — if the entrypoint
doesn't hand DATABASE_URL/TELEGRAM_BOT_TOKEN through, the jobs crash the moment
they connect, but only at 08:00/12:00/21:00 IST, in the sidecar's log. So this
file pins the schedule→job mapping (§12/§18), the /etc/cron.d format, and — by actually
running the entrypoint — that a value survives the dump-and-source round trip.
"""

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.parent
CRONTAB = (ROOT / "cron" / "kanakko.crontab").read_text()
ENTRYPOINT = ROOT / "cron" / "entrypoint.sh"
COMPOSE = (ROOT / "docker-compose.yml").read_text()

# DECISIONS §12/§18: (minute, hour, day-of-month, month, day-of-week) → job module.
EXPECTED = {
    "recurring": ("0", "8", "*", "*", "*"),
    "noon": ("0", "12", "*", "*", "*"),
    "evening": ("0", "21", "*", "*", "*"),
    "monthly": ("0", "9", "1", "*", "*"),
}


def _jobs():
    """Each crontab job line as (schedule_tuple, user, command)."""
    for line in CRONTAB.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split(None, 6)
        assert len(fields) == 7, f"not 6-field /etc/cron.d format: {line!r}"
        yield tuple(fields[:5]), fields[5], fields[6]


JOBS = list(_jobs())


def _module(command: str) -> str:
    """The kanakko.jobs.<name> a command invokes → <name>."""
    m = re.search(r"kanakko\.jobs\.(\w+)", command)
    assert m, f"no kanakko.jobs.* module in {command!r}"
    return m.group(1)


def test_schedules_match_the_spec():
    """§12: noon 12:00 daily, evening 21:00 daily, monthly 09:00 on the 1st.

    A fat-fingered 20:00 or a 09:00 daily monthly would ship a job that fires at
    the wrong time and log nothing wrong.
    """
    got = {_module(cmd): sched for sched, _user, cmd in JOBS}
    assert got == EXPECTED


def test_each_line_carries_the_root_user_field():
    """/etc/cron.d entries need a user column; a 5-field (crontab -e) line makes
    cron reject the whole file. The user must be root — the jobs write to PID 1's
    stdout and the venv lives under /app, both root-owned."""
    assert JOBS, "crontab has no job lines"
    for _sched, user, _cmd in JOBS:
        assert user == "root", user


def test_every_job_module_exists():
    """A renamed or misspelt job module is a crash at fire time, not at build."""
    for _sched, _user, cmd in JOBS:
        name = _module(cmd)
        assert (ROOT / "kanakko" / "jobs" / f"{name}.py").is_file(), name


def test_every_line_sources_the_env_file():
    """Without sourcing /app/cron.env the job runs with cron's stripped env and
    dies on a missing DATABASE_URL."""
    for _sched, _user, cmd in JOBS:
        assert ". /app/cron.env" in cmd, cmd


def test_compose_cron_runs_the_entrypoint():
    """The env file only exists if the entrypoint wrote it. A bare `cron -f`
    command (the obvious edit) skips that and every job loses its env."""
    cron_block = re.search(r"^  cron:\n(.*?)(?=^  \S|\Z)", COMPOSE, re.M | re.S).group(1)
    assert re.search(r"^\s+command:.*entrypoint\.sh", cron_block, re.M), cron_block


def test_entrypoint_round_trips_a_hostile_value(tmp_path):
    """The whole point of the entrypoint: a value in the container env comes back
    intact after dump-and-source. Uses a value with a single quote, a space, a
    dollar and a semicolon — the escaping this guards is the reason the jobs get
    a working DATABASE_URL.
    """
    env_file = tmp_path / "cron.env"
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "cron").write_text("#!/bin/sh\nexit 0\n")  # so `exec cron -f` returns
    (stub / "cron").chmod(0o755)

    hostile = "post'gres://u:p @db;5432/$k"
    env = {
        "PATH": f"{stub}:{os.environ['PATH']}",
        "CRON_ENV_FILE": str(env_file),
        "DATABASE_URL": hostile,
    }
    subprocess.run(["sh", str(ENTRYPOINT)], env=env, check=True)

    # Source the produced file in a fresh shell that does NOT already hold the
    # var, and echo it back.
    got = subprocess.run(
        ["sh", "-c", f". {env_file}; printf '%s' \"$DATABASE_URL\""],
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert got == hostile
