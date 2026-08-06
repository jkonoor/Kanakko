import logging

from kanakko import configure_logging


def test_configure_logging_lets_a_kanakko_info_record_reach_a_handler(capsys):
    """The same INFO record is dropped before config and emitted after.

    Without configuration a `kanakko` logger inherits root's WARNING level and
    has no handler, so INFO is dropped by Python's last-resort handler — the
    exact reason the jobs (whose only evidence is a log line) would silently
    emit nothing under uvicorn. `configure_logging` must flip that.
    """
    pkg = logging.getLogger("kanakko")
    saved_handlers, saved_level = pkg.handlers[:], pkg.level
    pkg.handlers.clear()
    pkg.setLevel(logging.NOTSET)  # inherit root (WARNING) — the unconfigured state
    try:
        logging.getLogger("kanakko.jobs").info("before config")
        assert "before config" not in capsys.readouterr().err

        configure_logging()
        logging.getLogger("kanakko.jobs").info("after config")
        assert "after config" in capsys.readouterr().err
    finally:
        pkg.handlers[:] = saved_handlers
        pkg.setLevel(saved_level)
