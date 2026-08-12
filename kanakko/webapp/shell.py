"""The Mini App bootstrap page — the only HTML/CSS/JS in the project."""

# The Mini App bootstrap: Telegram opens this plain URL with no initData in it, so
# the page reads `initData` client-side and hands it to `/app/data` in the `tma`
# Authorization header, which validates it and returns the server-rendered
# figures. Contains no data and no secret, so it needs no auth itself.
SHELL_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kanakko</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
/* Follow Telegram's theme: it injects --tg-theme-* CSS vars, so binding the
   background and text to them makes the page dark in a dark theme and light in
   a light one. Without this the body keeps the browser default (black on white)
   and shows as a glaring white panel inside Telegram's dark chrome. Fallbacks
   are the light-theme values for a plain-browser open. */
body {
  font-family: system-ui, sans-serif; margin: 0; padding: 16px 16px 32px;
  background: var(--tg-theme-bg-color, #fff);
  color: var(--tg-theme-text-color, #000);
  /* Amounts are columns of digits — tabular figures keep them aligned on the
     decimal instead of jittering by glyph width. */
  font-variant-numeric: tabular-nums;
}
/* The only colour this design adds. Everything else is Telegram's theme, per
   its guideline to follow the dynamic theme-based colours. Income gets the one
   accent because direction is the fastest thing an eye can pick up; expenses
   stay in normal ink, which also avoids the red/green pairing that red-green
   colour deficiency makes unreadable. Two steps because one green cannot clear
   4.5:1 on both a white and a near-black surface — the scheme is stamped on
   <html> from Telegram's own colorScheme, so this is exact, not a guess. */
:root { --income: #1a7f37; }
:root[data-scheme="dark"] { --income: #4ac26b; }
.in { color: var(--income); }
h2 {
  font-size: 13px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase;
  color: var(--tg-theme-hint-color, #707579); margin: 28px 0 10px;
}
.label, .txn-note, .pct { color: var(--tg-theme-hint-color, #707579); }
.stat, .cat-head { display: flex; justify-content: space-between; gap: 12px; }
/* Hero: one figure with dominant scale gives the screen an entry point. Tight
   leading because it is a single line, not a paragraph. */
.hero-label { font-size: 13px; color: var(--tg-theme-hint-color, #707579); margin: 20px 0 2px; }
.hero { font-size: 40px; font-weight: 600; line-height: 1.1; margin: 0; }
/* The period-over-period delta. Hint ink, not the income accent: direction is
   carried by the arrow glyph and the label, never colour alone (WCAG 1.4.1). */
.delta { font-size: 13px; color: var(--tg-theme-hint-color, #707579); margin: 6px 0 0; }
.substats { display: flex; flex-direction: column; gap: 6px; margin-top: 14px; }
.stat { font-size: 15px; }
/* Segmented control: 44px tall so the tap target clears WCAG 2.5.8's 24px
   minimum with Apple's 44pt recommendation to spare. */
.switch {
  display: flex; gap: 2px; padding: 3px; border-radius: 11px;
  background: rgba(128,128,128,.14);
}
.seg {
  flex: 1; min-height: 44px; border: 0; border-radius: 9px; cursor: pointer;
  background: none; font: inherit; font-size: 14px;
  color: var(--tg-theme-hint-color, #707579);
}
.seg[aria-selected="true"] {
  background: var(--tg-theme-bg-color, #fff);
  color: var(--tg-theme-text-color, #000); font-weight: 600;
}
.cat { margin: 12px 0; }
.cat-amt { display: flex; gap: 8px; flex-shrink: 0; }
.pct { font-size: 13px; min-width: 34px; text-align: right; }
.bar { background: rgba(128,128,128,.2); border-radius: 4px; height: 6px; overflow: hidden; margin-top: 6px; }
/* One hue, varying length. Length is what people judge accurately, so the bar
   already carries the magnitude; per-category hues would encode identity nobody
   needs and would owe a colour-blind check in two themes. */
.fill { background: var(--tg-theme-button-color, #3390ec); height: 100%; border-radius: 4px; }
/* Grid, not wrapping flex: the note needs its own row while the delete button
   stays on the first one. As sibling flex items with `.txn-note` at
   flex-basis:100%, `.del` was pushed onto a third line and the rows collided —
   visible only in a browser, which is why the test can guard just the structure
   that makes this work (see test_txn_row_places_note_and_delete). */
.txn { display: grid; grid-template-columns: 1fr auto; column-gap: 8px; align-items: center; padding: 4px 0; }
/* align-items:center, not the default stretch: the 44px select makes the row
   tall, and a stretched amount span sits on a different baseline from the date
   beside it. `.amt` gets a fixed slot so every amount shares one right-hand
   lane however long the category name is. */
.txn-main { display: flex; justify-content: space-between; align-items: center; gap: 8px; min-width: 0; font-size: 15px; }
.amt { flex-shrink: 0; text-align: right; }
.txn-note { grid-column: 1; font-size: 13px; padding-bottom: 6px; }
/* 44px square: `.del` is destructive and sits next to the category control, so
   it gets Apple's recommended target rather than WCAG 2.5.8's 24px floor. The
   fixed width also gives every row's ✕ one vertical lane, however long the
   amount beside it. */
.del {
  grid-row: 1; grid-column: 2; border: 0; background: none; cursor: pointer;
  color: var(--tg-theme-hint-color, #707579);
  width: 44px; height: 44px; flex-shrink: 0; font-size: 16px;
}
.cat-select {
  background: none; border: 0; color: inherit; font: inherit; cursor: pointer;
  min-height: 44px; max-width: 45vw;
}
/* Same lane as `.del` (44px square, same hint ink) but the note-row's column,
   which is otherwise empty when a row carries no note — no layout cost. */
.edit-toggle {
  grid-row: 2; grid-column: 2; border: 0; background: none; cursor: pointer;
  color: var(--tg-theme-hint-color, #707579);
  width: 44px; height: 44px; flex-shrink: 0; font-size: 16px;
}
/* Third action, `expense` rows only (task 1082) — its own row so it doesn't
   contend with `.del`/`.edit-toggle` for row 1/2, column 2; row 3 column 1 is
   simply left empty on these rows, same as it already is on every other row. */
.refund-toggle {
  grid-row: 3; grid-column: 2; border: 0; background: none; cursor: pointer;
  color: var(--tg-theme-hint-color, #707579);
  width: 44px; height: 44px; flex-shrink: 0; font-size: 16px;
}
/* The hidden per-row editor (task 974): amount, date, note, account. `[hidden]`
   needs restating after `display: flex` below — an attribute selector and a
   class selector have equal specificity, so without this rule the later
   `.txn-edit` declaration would win and the panel would never actually hide.
   `.txn-refund` (task 1082) shares the same shape. */
.txn-edit, .txn-refund { grid-column: 1 / -1; display: flex; flex-direction: column; gap: 6px; padding-top: 6px; }
.txn-edit[hidden], .txn-refund[hidden] { display: none; }
.txn-edit input, .txn-edit select, .txn-refund input {
  font: inherit; color: inherit; background: var(--tg-theme-secondary-bg-color, rgba(128,128,128,.1));
  border: 0; border-radius: 8px; padding: 8px 10px; min-height: 44px;
}
/* The refund panel's explicit submit (task 1082) — unlike the edit fields, which
   auto-save on `change`, a refund adds a new row rather than overwriting one, so
   it gets a real button rather than firing on blur. Telegram's own button colour,
   the same var `.fill` already follows. */
.refund-submit {
  border: 0; border-radius: 8px; padding: 10px; min-height: 44px; font: inherit; font-weight: 600;
  background: var(--tg-theme-button-color, #3390ec); color: var(--tg-theme-button-text-color, #fff);
  cursor: pointer;
}
</style>
</head>
<body>
<div id="app">Loading…</div>
<script>
const tg = window.Telegram.WebApp;
tg.ready();
const app = document.getElementById('app');
// Stamp Telegram's own light/dark scheme on <html> so the income accent can pick
// the step that clears contrast on this surface. `prefers-color-scheme` is not a
// substitute — it reports the OS theme, which need not match the theme the user
// set inside Telegram.
function stampScheme() {
  document.documentElement.dataset.scheme = tg.colorScheme || 'light';
}
stampScheme();
if (tg.onEvent) { tg.onEvent('themeChanged', stampScheme); }

// Which period tab is open. Kept outside load() so a refresh — reopening the
// Mini App, or coming back from a delete — restores the tab the user chose
// instead of snapping back to Month under their hands.
let period = 'month';
function applyPeriod() {
  app.querySelectorAll('.seg').forEach(
    b => b.setAttribute('aria-selected', String(b.dataset.period === period)));
  app.querySelectorAll('.panel').forEach(
    p => { p.hidden = p.dataset.period !== period; });
}
function load() {
  fetch('/app/data', {headers: {Authorization: 'tma ' + tg.initData}})
    .then(r => { if (!r.ok) throw new Error(r.status); return r.text(); })
    .then(html => { app.innerHTML = html; applyPeriod(); })
    .catch(() => { app.textContent = 'Could not load dashboard.'; });
}
app.addEventListener('click', e => {
  // Switching period is a local view change — every panel is already in the
  // fragment, so no request and no reload.
  const seg = e.target.closest('.seg');
  if (seg) { period = seg.dataset.period; applyPeriod(); return; }
  // The edit toggle is a local view change too — it just reveals the hidden
  // panel `_edit_panel` already rendered for this row, no request either.
  const editBtn = e.target.closest('.edit-toggle');
  if (editBtn) {
    const panel = editBtn.closest('.txn').querySelector('.txn-edit');
    panel.hidden = !panel.hidden;
    return;
  }
  // Same for the refund toggle (task 1082) — reveals `_refund_panel`.
  const refundBtn = e.target.closest('.refund-toggle');
  if (refundBtn) {
    const panel = refundBtn.closest('.txn').querySelector('.txn-refund');
    panel.hidden = !panel.hidden;
    return;
  }
  // The refund panel's own submit (task 1082) — unlike every other control here,
  // this one doesn't fire on `change`: a refund adds a row rather than
  // overwriting one, so it waits for an explicit tap.
  const refundSubmit = e.target.closest('.refund-submit');
  if (refundSubmit) {
    const amount = refundSubmit.closest('.txn-refund').querySelector('.refund-amount').value;
    if (!amount) return;
    fetch('/app/refund', {
      method: 'POST',
      headers: {Authorization: 'tma ' + tg.initData, 'Content-Type': 'application/json'},
      body: JSON.stringify({id: Number(refundSubmit.dataset.id), amount}),
    }).then(r => { if (r.ok) load(); });
    return;
  }
  const btn = e.target.closest('.del');
  if (!btn) return;
  fetch('/app/delete', {
    method: 'POST',
    headers: {Authorization: 'tma ' + tg.initData, 'Content-Type': 'application/json'},
    body: JSON.stringify({id: Number(btn.dataset.id)}),
  }).then(r => { if (r.ok) load(); });
});
app.addEventListener('change', e => {
  const sel = e.target.closest('.cat-select');
  if (sel && sel.value) {
    fetch('/app/category', {
      method: 'POST',
      headers: {Authorization: 'tma ' + tg.initData, 'Content-Type': 'application/json'},
      body: JSON.stringify({id: Number(sel.dataset.id), category: sel.value}),
    }).then(r => { if (r.ok) load(); });
    return;
  }
  // The four row-editor fields (`_edit_panel`) share one dispatch: each carries
  // `data-edit-field` naming the column `/app/edit` changes, mirroring
  // `edit_transaction_field` taking one column name rather than four near-
  // duplicate routes. Empty is a no-op except for `note`, where it means "clear
  // it" — every other field's native input (date/number/select) already refuses
  // to go empty on its own.
  const field = e.target.closest('[data-edit-field]');
  if (!field) return;
  if (field.value === '' && field.dataset.editField !== 'note') return;
  fetch('/app/edit', {
    method: 'POST',
    headers: {Authorization: 'tma ' + tg.initData, 'Content-Type': 'application/json'},
    body: JSON.stringify({id: Number(field.dataset.id), field: field.dataset.editField, value: field.value}),
  }).then(r => { if (r.ok) load(); });
});
load();
// Reload when the Mini App comes back to the foreground. Without this the page
// keeps whatever it rendered when first opened, so an expense logged in the chat
// while the dashboard is minimized is invisible until it is fully closed and
// reopened — the figures silently disagree with the ledger, which is the one
// thing a finance dashboard must not do.
//
// `activated` is Telegram's own event for exactly this: "Occurs when the Mini App
// becomes active (e.g., opened from minimized state or selected among tabs)"
// (Bot API 8.0+, verified 2026-08-07). `visibilitychange` is the plain-web
// fallback for clients older than 8.0, where onEvent('activated') never fires.
if (tg.onEvent) { tg.onEvent('activated', load); }
document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
</script>
</body>
</html>
"""
