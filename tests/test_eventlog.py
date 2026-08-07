"""The §17 event sink seam: never raises, silent when unbound, JSON when bound.

The load-bearing check is `test_a_raising_sink_never_breaks_the_caller`: a
logging failure that propagated would 500 a webhook and make Telegram redeliver
a message that already succeeded, which is the whole reason `log_event` wraps
every write.
"""

import json
import logging

import pytest

from kanakko import configure_logging, eventlog


@pytest.fixture(autouse=True)
def _unbind():
    # Every test starts and ends with no sink — the module-level global is
    # process-wide, so leaking a bound sink would pollute other test modules.
    # The `kanakko.events` file logger keeps its first handler forever, so clear
    # it too or a later `file_sink(other_dir)` would silently keep the old path.
    def reset():
        eventlog.unbind_sink()
        events = logging.getLogger("kanakko.events")
        for h in list(events.handlers):
            events.removeHandler(h)
            h.close()

    reset()
    yield
    reset()


def test_unbound_is_a_silent_noop():
    # No sink bound: nothing to write to, and no error either.
    eventlog.log_event("transaction.confirmed", status="ok", txn_id=1)


def test_bound_sink_receives_event_status_and_fields():
    records = []
    eventlog.bind_sink(records.append)
    eventlog.log_event("transaction.undone", status="noop", txn_id=5, amount="500")
    assert records == [
        {"event": "transaction.undone", "status": "noop", "txn_id": 5, "amount": "500"}
    ]


def test_a_raising_sink_never_breaks_the_caller():
    def boom(record):
        raise RuntimeError("sink is down")

    eventlog.bind_sink(boom)
    reached = []
    eventlog.log_event("transaction.confirmed", status="ok", amount="500")
    reached.append("caller kept going")  # only runs if log_event didn't raise
    assert reached == ["caller kept going"]


def test_ms_since_measures_forward():
    import time

    start = time.perf_counter()
    assert eventlog.ms_since(start) >= 0


def test_file_sink_writes_json_lines(tmp_path):
    sink = eventlog.file_sink(str(tmp_path))
    sink({"event": "transaction.confirmed", "status": "ok", "amount": "500"})
    line = (tmp_path / "events.jsonl").read_text().strip()
    assert json.loads(line) == {
        "event": "transaction.confirmed",
        "status": "ok",
        "amount": "500",
    }


def test_scrub_redacts_secrets_but_keeps_the_rest():
    # A realistic payload: a bot token, an sk-or- key, a long base64 blob, and
    # the raw initData string — each absent from the output, everything else
    # intact. A scrubber that redacted everything would fail the survival asserts.
    bot_token = "8123456789:AAExampleTokenThatLooksRealEnough_0123456789"
    or_key = "sk-or-v1-0123456789abcdef0123456789abcdef0123456789abcdef"
    blob = "A" * 600
    record = {
        "event": "parse.completed",
        "status": "ok",
        "user_id": 42,
        "amount": "500.00",
        "note": "lunch at the airport",
        "headers": {"authorization": bot_token},
        "keys": [or_key, "keep-me"],
        "raw": blob,
        "init_data": "user=%7B...%7D&hash=deadbeef",
    }
    out = eventlog.scrub(record)
    dumped = json.dumps(out)

    assert bot_token not in dumped
    assert or_key not in dumped
    assert blob not in dumped
    assert out["init_data"] == eventlog.REDACTED
    assert out["headers"]["authorization"] == eventlog.REDACTED

    # Surrounding fields survive, at every depth.
    assert out["amount"] == "500.00"
    assert out["note"] == "lunch at the airport"
    assert out["user_id"] == 42
    assert "keep-me" in out["keys"]


def test_log_event_scrubs_before_the_sink():
    records = []
    eventlog.bind_sink(records.append)
    eventlog.log_event(
        "parse.completed", status="ok", note="hi", prompt="the full LLM prompt"
    )
    assert records[0]["prompt"] == eventlog.REDACTED
    assert records[0]["note"] == "hi"


def test_configure_logging_binds_only_when_log_dir_is_set(tmp_path, monkeypatch):
    # LOG_DIR unset (or empty — compose's `:-` trap) leaves the sink unbound so
    # `uv run pytest` writes no JSONL into the repo.
    monkeypatch.delenv("LOG_DIR", raising=False)
    configure_logging()
    assert eventlog._sink is None

    monkeypatch.setenv("LOG_DIR", "")  # present but empty
    configure_logging()
    assert eventlog._sink is None

    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    configure_logging()
    assert eventlog._sink is not None
