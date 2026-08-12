"""One module per bot command handler, split out of `kanakko/handlers.py`.

`handlers.py` had grown to 1541 lines holding `dispatch`, every command
predicate/handler, and the confirm-card core in one file — far past
CLAUDE.md's 300-line guideline. `dispatch`, `TextMessage`/`ButtonPress`,
`_command_arg`, and the confirm-card core (`handle_confirm`/`handle_cancel`/
`handle_category`/`handle_account_choice`/`handle_change_amount_request`/
`handle_amount_reply`) are the shared spine every command module needs, so
they stay in `handlers.py`; each standalone command (`/transfer`, `/account`,
`/remove`, `/invite`+`/invite_signup`, `/recurring`, `refund`) moves here one
at a time, mirroring how `kanakko/webapp/refund.py` and
`kanakko/webapp/recurring.py` split off `webapp/routes.py`.
"""
