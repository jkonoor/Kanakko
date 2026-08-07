"""Mini App: validation, calendar ranges, rendering, and the bootstrap page.

The package is the interface — callers import from `kanakko.webapp`, not from the
modules inside it, so the file layout can change without touching every caller.

Split out of a single 545-line module that held HMAC verification and CSS side by
side: `auth` is security, `periods` is §10 calendar maths, `render` builds the
fragment, `shell` is the page.
"""

from kanakko.webapp.auth import (
    InitDataError,
    user_id_from_init_data,
    validate_init_data,
)
from kanakko.webapp.periods import (
    Period,
    current_month_ist,
    current_week_ist,
    previous_month_first,
)
from kanakko.webapp.render import (
    category_bars,
    dashboard_html,
    recent_list,
)
from kanakko.webapp.shell import SHELL_HTML

__all__ = [
    "InitDataError",
    "Period",
    "SHELL_HTML",
    "category_bars",
    "current_month_ist",
    "current_week_ist",
    "dashboard_html",
    "previous_month_first",
    "recent_list",
    "user_id_from_init_data",
    "validate_init_data",
]
