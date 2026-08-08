import logging
import os

__version__ = "0.1.0"


def configure_logging() -> None:
    """Attach a stderr handler to the `kanakko` package logger at INFO, and bind
    the §17 event sink when `LOG_DIR` is set.

    Under uvicorn the root logger has no app handler, so a `kanakko` logger's
    INFO record propagates to root and is dropped by Python's last-resort
    handler (WARNING and above only) — verified live: a real Telegram delivery
    produced uvicorn access lines and nothing from the app. Configuring the
    package logger directly, rather than root via `basicConfig` (which uvicorn's
    own config can render a no-op), guarantees `log.info` — and the scheduled
    jobs whose only evidence of running is a log line — actually emit.
    Idempotent: safe to call from every entry point (web app, each cron job).

    The event sink (§17) binds here rather than in a second startup hook — this
    is already called from all four entry points. `LOG_DIR` unset (or present
    but empty — compose's `:-` makes it so, the §2 trap) leaves it unbound, so
    `uv run pytest` writes no JSONL into the repo. Read with truthiness, not a
    `get(key, default)` that a present-but-empty value would slip past.
    """
    log_dir = os.environ.get("LOG_DIR")
    if log_dir:
        from kanakko import eventlog

        eventlog.bind_sink(eventlog.file_sink(log_dir))

    logger = logging.getLogger("kanakko")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(handler)
