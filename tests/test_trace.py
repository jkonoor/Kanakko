"""Trace mode (§17): the outcome is in the filename, and rotation actually deletes.

Two load-bearing checks. `test_failed_parse_names_the_failure` proves a directory
listing is the summary — a failed parse leaves a file whose *name* says it failed,
so an operator greps `ls`, not a stream. `test_rotation_deletes_the_oldest` proves
the disk stays bounded: a rotation that never fires is the bug that fills the
shared volume.
"""

import glob

import pytest

from kanakko import handlers
from kanakko.handlers import TextMessage
from kanakko.parse import Transaction
from kanakko.trace import open_trace


@pytest.fixture(autouse=True)
def _trace_env(tmp_path, monkeypatch):
    # LOG_DIR points at a throwaway dir and TRACE_MODE defaults on, so tracing is
    # active for these tests but writes nothing into the repo. TRACE_KEEP is left
    # to each test that cares.
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.delenv("TRACE_MODE", raising=False)
    monkeypatch.delenv("TRACE_KEEP", raising=False)
    return tmp_path


def test_disabled_without_log_dir(monkeypatch):
    monkeypatch.delenv("LOG_DIR", raising=False)
    tr = open_trace(1)
    assert tr.folder is None
    tr.write("input", {"text": "x"})  # a no-op, must not raise


def test_off_switch(monkeypatch):
    monkeypatch.setenv("TRACE_MODE", "off")
    assert open_trace(1).folder is None


def test_outcome_is_in_the_filename(tmp_path):
    tr = open_trace(42)
    tr.write("input", {"text": "spent 500"})
    tr.write("parse", {"error": "boom"}, outcome="invalid")
    names = sorted(p.name for p in (tmp_path / "trace" / "42").iterdir())
    assert names == ["01__input.json", "02__parse__invalid.json"]


def test_write_never_raises_and_scrubs(tmp_path):
    # A key-shaped value survives being written — trace writes the prompt on
    # purpose (§17), but the scrub backstop still redacts an accidental secret.
    tr = open_trace(7)
    tr.write("request", {"leak": "sk-or-v1-" + "a" * 40, "note": "lunch"})
    text = (tmp_path / "trace" / "7" / "01__request.json").read_text()
    assert "sk-or-v1-" not in text
    assert "lunch" in text


def test_rotation_deletes_the_oldest(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACE_KEEP", "3")
    for update_id in range(1, 7):  # 1..6, monotonic like real update ids
        open_trace(update_id).write("input", {"text": str(update_id)})
    kept = sorted(p.name for p in (tmp_path / "trace").iterdir())
    assert kept == ["4", "5", "6"]  # only the last TRACE_KEEP survive


def test_failed_parse_names_the_failure(conn, monkeypatch, tmp_path):
    """A failed parse through `handle_text` leaves a file whose name says so."""
    from kanakko.migrate import migrate

    migrate(conn)

    def raise_validation(text):
        Transaction.model_validate(
            {"type": "expense", "amount": "", "category": None,
             "date": "2026-08-06", "note": text}
        )

    monkeypatch.setattr(handlers, "parse_message", raise_validation)
    monkeypatch.setattr(
        handlers, "send_message", lambda *a, **k: {"result": {"message_id": 1}}
    )

    handlers.handle_text(
        conn, TextMessage(chat_id=12345, message_id=1, text="how's it going",
                          update_id=555)
    )
    conn.rollback()

    hits = glob.glob(str(tmp_path / "trace" / "555" / "*invalid*"))
    assert hits, "the failed-parse artefact should name the failure in its filename"
