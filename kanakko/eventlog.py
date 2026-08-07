"""One logging call, a swappable sink — the operational half of §17.

`log_event(event, *, status, **fields)` emits one JSON object per money/parse
event through a **module-level sink bound once at startup**. The sink is the
seam: swapping the backing store (a file today, a remote aggregator tomorrow)
is a new `bind_sink` and zero call-site changes — the reason `ha-backend` made
its signature `async`, achieved here without forcing `await` through sync
handlers. Unbound is a silent no-op, so a test that imports a handler writes
nothing without opting in (`LOG_DIR` unset is what leaves it unbound).

`log_event` **never raises**. A logging failure must not fail a webhook —
Telegram would redeliver a message that already succeeded — so every sink call
is wrapped and a broken sink is swallowed.
"""

import json
import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# The only three statuses (§17). ok = it happened; noop = a redelivery or a tap
# with nothing to do; error = the handler raised (logged once, in the webhook).
OK = "ok"
ERROR = "error"
NOOP = "noop"
STATUSES = (OK, ERROR, NOOP)

Sink = Callable[[dict], None]

_sink: Sink | None = None


def bind_sink(sink: Sink) -> None:
    """Bind the module-level sink. Called once at startup; last bind wins."""
    global _sink
    _sink = sink


def unbind_sink() -> None:
    """Drop the sink — `log_event` is a silent no-op again (tests, teardown)."""
    global _sink
    _sink = None


def log_event(event: str, *, status: str, **fields) -> None:
    """Emit one operational event as JSON. Never raises (§17).

    `event` and `status` are the greppable identifier — the flat-file
    equivalent of `ls | grep __error` is `jq 'select(.status=="error")'`.
    `**fields` carry the correlation id, user, duration, amount, and so on.
    An unbound sink is a no-op; a sink that raises is swallowed so a logging
    failure can never fail the caller's work.
    """
    sink = _sink
    if sink is None:
        return
    record = {"event": event, "status": status, **fields}
    try:
        sink(record)
    except Exception:  # a broken sink must never break a handler (§17)
        try:
            log.warning("event sink failed for %s", event, exc_info=True)
        except Exception:
            pass


def ms_since(start: float) -> int:
    """Milliseconds since a `time.perf_counter()` mark — the `duration_ms` helper."""
    return round((time.perf_counter() - start) * 1000)


def file_sink(log_dir: str) -> Sink:
    """The default sink: JSON Lines into `LOG_DIR/events.jsonl`, rotated.

    A dedicated `kanakko.events` logger with a stdlib `RotatingFileHandler` —
    that is where §17's "no new dependency" rotation comes from. `propagate` is
    off so these JSON lines stay out of the operational stderr handler that
    `configure_logging` attaches to the `kanakko` package logger. `default=str`
    serialises the `Decimal` amounts later call sites pass.
    """
    events = logging.getLogger("kanakko.events")
    events.setLevel(logging.INFO)
    events.propagate = False
    if not events.handlers:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            Path(log_dir) / "events.jsonl",
            maxBytes=5_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        events.addHandler(handler)

    def sink(record: dict) -> None:
        events.info(json.dumps(record, default=str))

    return sink
