"""The minimal look: one typeface, a quiet neutral palette, one accent color.

Typeface: the system UI font (SF Pro on macOS, Segoe UI on Windows). It's what the
operating system itself uses, so it looks native, needs no download, and works
offline. Times use tabular figures so columns of times line up.

Palette ("paper and ink"): warm off-white canvas, white surfaces, near-black text,
one muted blue accent, and softened status colors. Borders replace shadows.
"""

from __future__ import annotations

from nicegui import ui

INK = "#1D1D1F"  # body text
MUTED = "#6E6E73"  # secondary text
LINE = "#E5E5E7"  # borders and dividers
CANVAS = "#F6F6F4"  # page background
SURFACE = "#FFFFFF"  # cards, header
ACCENT = "#3A6EA5"  # buttons, links, active tab
POSITIVE = "#2F7D4F"
WARNING = "#B7791F"
NEGATIVE = "#B83A3A"

FONT_STACK = ('-apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", system-ui, Roboto, '
              '"Helvetica Neue", Arial, sans-serif')

CSS = f"""
:root {{ --ink: {INK}; --muted: {MUTED}; --line: {LINE}; --canvas: {CANVAS}; --surface: {SURFACE}; }}
body, .q-field__native, .q-btn, .q-item, .q-tab {{ font-family: {FONT_STACK}; }}
body {{ background: var(--canvas); color: var(--ink); -webkit-font-smoothing: antialiased; }}

/* Times and numbers: same font, but every digit the same width so columns line up. */
.font-mono {{ font-family: inherit; font-variant-numeric: tabular-nums; letter-spacing: 0; }}

/* Header: white bar with a hairline instead of a colored, shadowed block. */
.q-header {{ background: var(--surface) !important; color: var(--ink); box-shadow: none;
             border-bottom: 1px solid var(--line); }}

/* Cards: flat, hairline border, softer corners. */
.q-card {{ box-shadow: none; border: 1px solid var(--line); border-radius: 12px; }}

/* The white panel holding each tab's page: same corners and hairline as cards, roomier inside. */
.q-tab-panels {{ border-radius: 12px; border: 1px solid var(--line); overflow: hidden; }}
.q-tab-panel {{ padding: 28px 32px !important; }}

/* Collapsible sections: a thin outline like the task cards, and a quiet header row. */
.q-expansion-item {{ border: 1px solid var(--line); border-radius: 12px; }}
.q-expansion-item__container > .q-item {{ padding-left: 14px; padding-right: 14px; border-radius: 11px; }}
/* Padding goes on the inner box: the open/close animation shrinks .q-expansion-item__content's
   height to 0, and padding on that box would stay as a leftover slice, then vanish at once. */
.q-expansion-item__content {{ padding: 0 !important; }}
.q-expansion-item__content > .nicegui-expansion-content {{ padding: 4px 14px 14px; }}

/* Floating menus and lists (time picker, Re-plan ▾, selects): thin outline, soft shadow. */
.q-menu {{ border: 1px solid var(--line); border-radius: 8px; box-shadow: 0 6px 20px rgba(0, 0, 0, .06) !important; }}

/* Events: each day is one outlined list with hairlines between events. */
.events-day {{ border: 1px solid var(--line); border-radius: 12px; overflow: hidden; }}
.event-row {{ padding: 8px 14px; min-height: 48px; border-top: 1px solid var(--line); }}
.event-row:first-child {{ border-top: none; }}

/* "Mark done": a quick green glow, then the task fades away (0.4 s). */
@keyframes done-glow {{
  0%   {{ box-shadow: 0 0 0 0 rgba(47, 125, 79, 0); background: var(--surface); opacity: 1; }}
  35%  {{ box-shadow: 0 0 0 4px rgba(47, 125, 79, .35); background: #EAF5EE; opacity: 1; }}
  100% {{ box-shadow: 0 0 0 6px rgba(47, 125, 79, 0); background: #EAF5EE; opacity: 0; }}
}}
.done-flash {{ animation: done-glow .4s ease-out forwards; border-color: {POSITIVE} !important; }}

/* Task cards: details on the left, action buttons on the right. On a phone-width window the
   buttons move below the details, left-aligned. (A plain media query: NiceGUI's Tailwind
   doesn't apply responsive prefixes such as sm: reliably.) */
.task-row {{ display: flex; flex-direction: row; align-items: center; gap: 8px; }}
.task-row > :first-child {{ flex: 1 1 auto; }}
.task-actions {{ margin-left: auto; flex-shrink: 0; }}
@media (max-width: 560px) {{
  .task-row {{ flex-direction: column; align-items: stretch; gap: 4px; }}
  .task-actions {{ margin-left: -8px; }}
}}

/* Undo popup at the bottom center, like Google Calendar's "Event deleted · Undo". */
.undo-toast {{ position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%); z-index: 6000;
              background: #2B2B2D; color: #FFFFFF; border-radius: 8px; padding: 6px 8px 6px 16px;
              box-shadow: 0 6px 24px rgba(0, 0, 0, .18); font-size: 14px; animation: toast-in .18s ease-out; }}
.undo-toast .q-btn {{ color: #9CC3F0 !important; font-weight: 600; }}
@keyframes toast-in {{ from {{ opacity: 0; transform: translate(-50%, 8px); }} to {{ opacity: 1; transform: translate(-50%, 0); }} }}

/* Buttons and tabs: sentence case, no raised shadow. */
.q-btn, .q-tab__label {{ text-transform: none; letter-spacing: 0; font-weight: 500; }}
.q-btn--standard {{ box-shadow: none; }}
.q-btn--rectangle {{ border-radius: 8px; }}
/* Split buttons (Re-plan ▾): the group wraps both halves. Give it the same corners and no
   shadow, and square the halves inside it, so nothing peeks out behind them. */
.q-btn-group {{ box-shadow: none; border-radius: 8px; overflow: hidden; }}
.q-btn-group > .q-btn-item {{ border-radius: 0; }}
.q-btn-dropdown--split > .q-btn-dropdown__arrow-container {{ border-left: 1px solid rgba(255, 255, 255, .35); }}
.q-tab {{ color: var(--muted); }}
.q-tab--active {{ color: var(--ink); }}

/* Secondary text and dividers use the palette's grays. */
.text-gray-500, .text-gray-600 {{ color: var(--muted) !important; }}
.q-separator {{ background: var(--line); }}
"""


def apply() -> None:
    """Call once per page, before building it."""
    ui.colors(primary=ACCENT, secondary=MUTED, accent=ACCENT, dark=INK,
              positive=POSITIVE, negative=NEGATIVE, warning=WARNING, info=ACCENT)
    ui.add_css(CSS)


# Folds a card up from its real height, so the panel around it shrinks smoothly instead of
# snapping when the card is removed. The negative margin takes back the 16px gap it leaves.
FOLD_JS = """
(() => {{
  const el = getHtmlElement({id});
  if (!el) return;
  el.style.overflow = 'hidden';
  el.animate([
    {{ height: el.offsetHeight + 'px', opacity: getComputedStyle(el).opacity }},
    {{ height: '0px', paddingTop: '0px', paddingBottom: '0px', marginTop: '-16px', borderWidth: '0px', opacity: 0 }}
  ], {{ duration: {ms}, easing: 'cubic-bezier(.4, 0, .2, 1)', fill: 'forwards' }});
}})();
"""
