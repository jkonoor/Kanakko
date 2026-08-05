"""Guards over docker-compose.yml.

The stack has one failure mode that reports nothing: a cron sidecar running in
UTC sends the 21:00 IST summary at 02:30 IST (DECISIONS §10). The other two
assertions pin decisions the compose file is the only record of.
"""

import re
from pathlib import Path

COMPOSE = (Path(__file__).parent.parent / "docker-compose.yml").read_text()


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


def test_web_and_cron_share_one_image():
    """DECISIONS §8: same image, different command.

    A separate build for the sidecar is how it ends up running last week's
    job code against this week's schema.
    """
    for name in ("web", "cron"):
        assert re.search(r"^\s+build: \.$", service(name), re.M), name


def test_web_migrates_before_it_serves():
    """Otherwise /healthz answers 200 on an unmigrated database and the first
    webhook dies on `relation "transactions" does not exist`."""
    command = re.search(r"^\s+command: (.*)$", service("web"), re.M).group(1)
    assert re.search(r"kanakko\.migrate.*&&.*uvicorn", command)
