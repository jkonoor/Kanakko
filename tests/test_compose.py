"""Guards over docker-compose.yml and the Dockerfile they share.

The stack has one failure mode that reports nothing: a cron sidecar running in
UTC sends the 21:00 IST summary at 02:30 IST (DECISIONS §10). Both the zone
name and the zone *files* are needed for that, and neither absence errors. The
other assertions pin decisions these two files are the only record of.
"""

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
COMPOSE = (ROOT / "docker-compose.yml").read_text()
DOCKERFILE = (ROOT / "Dockerfile").read_text()


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


def test_web_publishes_on_loopback_only():
    """This file is pasted into Dokploy on a shared host (DECISIONS §14).

    A 0.0.0.0 binding answers /telegram/webhook over plaintext :8000 from
    anywhere, bypassing the HTTPS domain, and nothing about it looks wrong.
    """
    for published in re.findall(r"^\s+- \"([^\"]+)\"$", service("web"), re.M):
        assert published.startswith("127.0.0.1:"), published


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
