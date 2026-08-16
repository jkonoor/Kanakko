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
/* Row rebuild (task 1704): a collapsed row was previously three always-visible
   action glyphs, each its own 44px grid lane, so every expense row was at
   least 132px tall for content needing 44-64px. `.txn-head` is now one button
   showing data only; every action lives inside `.txn-panel`, hidden until the
   row is tapped, so the collapsed height no longer depends on which actions a
   row happens to offer. */
.txn { padding: 4px 0; border-bottom: 1px solid rgba(128,128,128,.12); }
.txn-head {
  display: block; width: 100%; min-height: 44px; padding: 10px 0;
  border: 0; background: none; text-align: left; cursor: pointer;
  font: inherit; color: inherit;
}
/* align-items:center, not the default stretch: a stretched amount span would
   sit on a different baseline from the date beside it. `.amt` gets a fixed
   slot so every amount shares one right-hand lane however long the category
   name is. */
.txn-main { display: flex; justify-content: space-between; align-items: center; gap: 8px; min-width: 0; font-size: 15px; }
.amt { flex-shrink: 0; text-align: right; }
.txn-note { font-size: 13px; margin-top: 2px; }
/* `[hidden]` needs restating after `display: flex` below — an attribute
   selector and a class selector have equal specificity, so without this rule
   the later `.txn-panel`/`.txn-refund` declaration would win and the panel
   would never actually hide. */
.txn-panel, .txn-refund { display: flex; flex-direction: column; gap: 10px; padding: 6px 0 10px; }
.txn-panel[hidden], .txn-refund[hidden] { display: none; }
/* Label left (~90px), field right — the layout that kills the worst state in
   the old design, where two identical amount boxes (edit vs. refund) sat one
   above the other with nothing telling them apart. */
.field { display: flex; align-items: center; gap: 10px; }
.field-label { flex: 0 0 90px; font-size: 13px; color: var(--tg-theme-hint-color, #707579); }
.field input, .field select, .txn-refund input {
  flex: 1; min-width: 0; font: inherit; color: inherit;
  background: var(--tg-theme-secondary-bg-color, rgba(128,128,128,.1));
  border: 0; border-radius: 8px; padding: 8px 10px; min-height: 44px;
}
/* `<input type="date">` formats to the device locale and can't be styled, so
   this spells the date out the way the collapsed summary already does. */
.date-human { flex-shrink: 0; font-size: 13px; color: var(--tg-theme-hint-color, #707579); }
.txn-rule { border: 0; border-top: 1px solid rgba(128,128,128,.15); margin: 2px 0; }
/* Delete and Refund: labelled, not glyphs, and reachable only from inside the
   panel — deleting a row is now two deliberate taps (expand, then Delete)
   rather than one tap on a ✕ that read as "close" on an expandable card. */
.txn-actions { display: flex; gap: 8px; }
.del, .refund-toggle, .refund-submit {
  flex: 1; border: 0; border-radius: 8px; padding: 10px; min-height: 44px;
  font: inherit; font-weight: 600; cursor: pointer;
}
.del, .refund-toggle { background: rgba(128,128,128,.14); color: inherit; }
/* The refund panel's explicit submit (task 1082) — unlike the fields above,
   which auto-save on `change`, a refund adds a new row rather than
   overwriting one, so it gets a real button rather than firing on blur.
   Telegram's own button colour, the same var `.fill` already follows. */
.refund-submit {
  background: var(--tg-theme-button-color, #3390ec); color: var(--tg-theme-button-text-color, #fff);
}
/* The recurring-rules list (task 1161 split 2/2a) — its own row grid, its two
   actions the same 44px tap target `.del` already uses. */
.rule { display: grid; grid-template-columns: 1fr auto auto; column-gap: 8px; align-items: center; padding: 4px 0; }
.rule.paused { opacity: .5; }
.rule-main { font-size: 15px; min-width: 0; }
.rule-toggle, .rule-del {
  border: 0; background: none; cursor: pointer;
  color: var(--tg-theme-hint-color, #707579);
  width: 44px; height: 44px; flex-shrink: 0; font-size: 16px;
}
/* A failed mutation (task 1558) surfaces here instead of nowhere; a delete
   (task 1771) reuses the same fixed slot to offer Undo instead of asking to
   confirm before the fact. Fixed to the viewport, not the flow, so it doesn't
   shift anything else when it appears. Red only for the failure case — an
   undo offer is not an error, so it gets `.undo`'s neutral dark instead. */
#toast {
  position: fixed; left: 16px; right: 16px; bottom: 16px; padding: 12px 16px;
  border-radius: 8px; background: #d64545; color: #fff; text-align: center;
  font-size: 14px;
}
#toast.undo { background: #323232; }
#toast button {
  background: none; border: 0; color: inherit; font: inherit; font-weight: 600;
  text-decoration: underline; padding: 0; cursor: pointer;
}
#toast[hidden] { display: none; }
</style>
</head>
<body>
<div id="app">Loading…</div>
<div id="toast" hidden></div>
<script>
const tg = window.Telegram.WebApp;
tg.ready();
const app = document.getElementById('app');
const toast = document.getElementById('toast');
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
let toastTimer;
function showToast() {
  clearTimeout(toastTimer);
  toast.className = '';
  toast.textContent = "Couldn't save — try again.";
  toast.hidden = false;
  toastTimer = setTimeout(() => { toast.hidden = true; }, 3000);
}
// The undo offer (task 1771) replaces a confirm-before-delete dialog: it
// doesn't tax the case where the user meant it, and still leaves the mistake
// recoverable. `id`/`amount` come off the `.del` button that was just tapped,
// not the (now-gone) row, since the reload the delete triggers replaces the
// list before this ever renders.
function showUndoToast(id, amount) {
  clearTimeout(toastTimer);
  toast.className = 'undo';
  toast.innerHTML = 'Deleted ' + amount + ' · <button type="button" class="toast-undo" data-id="' + id + '">Undo</button>';
  toast.hidden = false;
  toastTimer = setTimeout(() => { toast.hidden = true; }, 5000);
}
// The one fetch every mutating action (edit, delete, restore, refund,
// category, recurring pause/delete — seven call sites) goes through. Always
// reloads, success or not: that discards whatever the user just typed and
// re-renders the server's actual state, so a rejected write visibly snaps back
// instead of silently staying wrong. The toast fires only when the write
// itself failed, so "reverted because it failed" is distinguishable from an
// ordinary refresh. `onSuccess`, when given, runs only on a 2xx response —
// today only the delete call site uses it, to raise the undo offer.
function mutate(url, body, onSuccess) {
  fetch(url, {
    method: 'POST',
    headers: {Authorization: 'tma ' + tg.initData, 'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  })
    .then(r => { if (!r.ok) { showToast(); return; } if (onSuccess) onSuccess(); })
    .catch(showToast)
    .finally(load);
}
app.addEventListener('click', e => {
  // Switching period is a local view change — every panel is already in the
  // fragment, so no request and no reload.
  const seg = e.target.closest('.seg');
  if (seg) { period = seg.dataset.period; applyPeriod(); return; }
  // Tapping the collapsed row is a local view change too — it just reveals
  // the hidden panel `_txn_panel` already rendered for this row, no request
  // either (task 1704 — this replaced a separate ✎ glyph button).
  const head = e.target.closest('.txn-head');
  if (head) {
    const panel = head.closest('.txn').querySelector('.txn-panel');
    panel.hidden = !panel.hidden;
    head.setAttribute('aria-expanded', String(!panel.hidden));
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
    mutate('/app/refund', {id: Number(refundSubmit.dataset.id), amount});
    return;
  }
  // The recurring-rule pause/resume toggle (task 1161 split 2/2a) — sends the
  // state to move *to*, the opposite of `data-active` (the rule's current
  // state, per `render.recurring_list`'s docstring), not a flip computed
  // server-side.
  const ruleToggle = e.target.closest('.rule-toggle');
  if (ruleToggle) {
    mutate('/app/recurring/active', {id: Number(ruleToggle.dataset.id), active: ruleToggle.dataset.active !== 'true'});
    return;
  }
  const ruleDel = e.target.closest('.rule-del');
  if (ruleDel) {
    mutate('/app/recurring/delete', {id: Number(ruleDel.dataset.id)});
    return;
  }
  const btn = e.target.closest('.del');
  if (!btn) return;
  const id = Number(btn.dataset.id);
  mutate('/app/delete', {id}, () => showUndoToast(id, btn.dataset.amount));
});
// The toast's own Undo button (task 1771) — separate listener because the
// toast sits outside `#app` and is never replaced by `load()`'s innerHTML
// swap, unlike everything the listener above handles.
toast.addEventListener('click', e => {
  const undo = e.target.closest('.toast-undo');
  if (!undo) return;
  toast.hidden = true;
  clearTimeout(toastTimer);
  mutate('/app/restore', {id: Number(undo.dataset.id)});
});
app.addEventListener('change', e => {
  const sel = e.target.closest('.cat-select');
  if (sel && sel.value) {
    mutate('/app/category', {id: Number(sel.dataset.id), category: sel.value});
    return;
  }
  // The four row-editor fields (`_txn_panel`) share one dispatch: each carries
  // `data-edit-field` naming the column `/app/edit` changes, mirroring
  // `edit_transaction_field` taking one column name rather than four near-
  // duplicate routes. Empty is a no-op except for `note`, where it means "clear
  // it" — every other field's native input (date/number/select) already refuses
  // to go empty on its own.
  const field = e.target.closest('[data-edit-field]');
  if (!field) return;
  if (field.value === '' && field.dataset.editField !== 'note') return;
  mutate('/app/edit', {id: Number(field.dataset.id), field: field.dataset.editField, value: field.value});
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
