import logging

__version__ = "0.1.0"


def configure_logging() -> None:
    """Attach a stderr handler to the `kanakko` package logger at INFO.

    Under uvicorn the root logger has no app handler, so a `kanakko` logger's
    INFO record propagates to root and is dropped by Python's last-resort
    handler (WARNING and above only) — verified live: a real Telegram delivery
    produced uvicorn access lines and nothing from the app. Configuring the
    package logger directly, rather than root via `basicConfig` (which uvicorn's
    own config can render a no-op), guarantees `log.info` — and the scheduled
    jobs whose only evidence of running is a log line — actually emit.
    Idempotent: safe to call from every entry point (web app, each cron job).
    """
    logger = logging.getLogger("kanakko")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(handler)
