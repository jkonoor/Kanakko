"""Per-update trace artefacts — the trace-mode half of §17.

One folder per Telegram update under `LOG_DIR/trace/<update_id>/`, one JSON file
per step with the **outcome in the filename** — `ls` a folder and the failure is
right there, `ha-backend`'s idea worth stealing. Numeric prefixes keep the files
in chronological order. **On by default** (`TRACE_MODE`, off with `off`), gated so
it can be turned off without a deploy, and rotated to the last `TRACE_KEEP` update
folders so the shared volume can't fill.

Like `log_event`, a trace write **never raises**: a trace failure must not fail a
webhook, or Telegram would redeliver a message that already succeeded. Every write
and the rotation are wrapped. `LOG_DIR` unset leaves it disabled, so `uv run
pytest` writes nothing into the repo without opting in — the same lever as the
event sink.

Trace mode deliberately writes the prompt to disk (§17): it is the whole point of
the artefact, and the wrong trade once strangers pay for this — recorded there to
be revisited before the first paying customer. The `scrub` backstop still runs, so
an accidental key-shaped value never lands even here.
"""

import json
import logging
import os
import shutil
from pathlib import Path

from kanakko.eventlog import scrub

log = logging.getLogger(__name__)

TRACE_KEEP_DEFAULT = 500


def _keep() -> int:
    """`TRACE_KEEP` folders to retain — at least 1 (0 would delete the live one).

    Read with `or`, not `get(key, default)`: compose's `:-` makes the variable
    present-but-empty, the §2 trap. A non-integer value falls back to the default.
    """
    try:
        return max(1, int(os.environ.get("TRACE_KEEP") or TRACE_KEEP_DEFAULT))
    except ValueError:
        return TRACE_KEEP_DEFAULT


class Trace:
    """A per-update artefact folder, or a no-op when tracing is off.

    `folder` is `None` for the disabled case, so every call site is unconditional:
    `open_trace(...)` then `.write(...)` with no `if enabled` scattered around.
    """

    def __init__(self, folder: Path | None):
        self.folder = folder
        self._n = 0

    def write(self, name: str, data, *, outcome: str | None = None) -> None:
        """Write one numbered step file, the outcome in its name. Never raises.

        Filename is `NN__<name>[__<outcome>].json` — the numeric prefix orders the
        steps, the outcome makes a directory listing the summary (§17). `data` is
        scrubbed and `default=str` serialises the `Decimal`/`date` a parse carries.
        """
        if self.folder is None:
            return
        self._n += 1
        parts = [f"{self._n:02d}", name] + ([outcome] if outcome else [])
        try:
            (self.folder / ("__".join(parts) + ".json")).write_text(
                json.dumps(scrub(data), default=str, indent=2), encoding="utf-8"
            )
        except Exception:  # a trace failure must never fail a handler (§17)
            try:
                log.warning("trace write failed for %s", name, exc_info=True)
            except Exception:
                pass


def open_trace(update_id: int | None) -> Trace:
    """Open the trace folder for `update_id`, or a disabled `Trace`.

    Disabled — a no-op `Trace` — when `LOG_DIR` is unset (nothing to write to, and
    what keeps tests quiet), when `TRACE_MODE` is `off`, or when there is no
    `update_id` to name the folder by. Otherwise the folder is created and the
    rotation runs, both wrapped so a filesystem failure downgrades to no tracing
    rather than a 500.
    """
    log_dir = os.environ.get("LOG_DIR")
    mode = os.environ.get("TRACE_MODE") or "on"
    if not log_dir or mode == "off" or update_id is None:
        return Trace(None)
    root = Path(log_dir) / "trace"
    folder = root / str(update_id)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        _rotate(root, _keep())
    except Exception:
        try:
            log.warning("trace open failed for update %s", update_id, exc_info=True)
        except Exception:
            pass
        return Trace(None)
    return Trace(folder)


def _rotate(root: Path, keep: int) -> None:
    """Keep the `keep` newest update folders, delete the rest.

    Newest = the largest `update_id`, which Telegram issues monotonically, so
    sorting the folder names numerically is chronological order — no clock read
    and no mtime needed. A rotation that never fires is the bug that fills a disk,
    so this runs on every `open_trace` after the current folder is created (which
    is why the current folder, the max id, is always among those kept).
    """
    folders = [p for p in root.iterdir() if p.is_dir()]

    def by_id(p: Path) -> int:
        try:
            return int(p.name)
        except ValueError:
            return -1

    folders.sort(key=by_id)
    for stale in folders[:-keep]:
        shutil.rmtree(stale, ignore_errors=True)
