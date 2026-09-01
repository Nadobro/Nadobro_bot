"""Shared presentation primitives for every Telegram surface.

Phase 1 of the interface overhaul. This module is the single place that decides
what a rule looks like, which glyph means which concept, and what the back
button says. Number formatting is NOT here — see the note at the foot of this
file; ``utils/visual.py`` already owns it.

Why it exists: the audit found the same decision made differently in different
files — five horizontal-rule widths, seven back/home labels, 96 emoji doing
icon duty. None of that was a bug in any one file; it was the absence of a
place to make the decision once.

Layering: this is a LEAF. It imports nothing from ``handlers`` (``formatters``
imports *it*, not the other way round), so there is no cycle and no risk of
dragging service code into a render path.

Escaping contract: every helper here returns PLAIN text, unescaped. Callers wrap
values in ``escape_md`` exactly as they already do::

    f"💰 *{_loc_md('Balance')}:* {escape_md(ui.usd(balance))}"

The one exception is ``rule()``, which returns pre-escaped MarkdownV2 because a
box-drawing rule has no variable content and every caller wants it escaped.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# MarkdownV2 escaping
#
# Canonical home. ``formatters`` re-exports these two names, so the 15 modules
# that do ``from ...formatters import escape_md`` keep working unchanged.
# ---------------------------------------------------------------------------

_MD2_SPECIAL = r"_*[]()~`>#+-=|{}.!"
_MD2_RE = re.compile("([" + re.escape(_MD2_SPECIAL) + "])")


def escape_md(text) -> str:
    """Escape text for MarkdownV2 body content."""
    if text is None:
        return ""
    text = str(text).replace("\\", "\\\\")
    return _MD2_RE.sub(r"\\\1", text)


def escape_md_code(text) -> str:
    """Escape for the INSIDE of a MarkdownV2 ``code``/``pre`` entity.

    Telegram only treats a backtick and a backslash as special inside a code
    entity; every other reserved character is literal there. Passing such text
    through :func:`escape_md` therefore injects backslashes that Telegram
    renders VERBATIM — and for a tap-to-copy block that means the user copies a
    corrupted value. A 1CT key is pure hex today, so ``escape_md`` happened to
    be a no-op on it; this makes the guarantee explicit rather than incidental.
    """
    if text is None:
        return ""
    return str(text).replace("\\", "\\\\").replace("`", "\\`")


# ---------------------------------------------------------------------------
# Rules
#
# The audit found five widths (30/28/24/22/20), so adjacent cards in the same
# conversation drew rules that did not line up — the home card alone used three.
#
# But collapsing all five to one would flatten a real distinction: the narrow
# rules are in-list dividers between repeated items, not section rules under a
# heading. Two named roles keep that difference legible and make the choice a
# semantic one at the call site instead of a number.
# ---------------------------------------------------------------------------

RULE_WIDTH = 28  # section rule, under a card heading
SUBRULE_WIDTH = 20  # divider between repeated items inside one section
_RULE_GLYPH = "━"


def rule(width: int | None = None) -> str:
    """A section rule, pre-escaped for MarkdownV2."""
    w = RULE_WIDTH if width is None else max(12, min(int(width), 40))
    return escape_md(_RULE_GLYPH * w)


def subrule() -> str:
    """A lighter divider between repeated items within a section."""
    return escape_md(_RULE_GLYPH * SUBRULE_WIDTH)


# ---------------------------------------------------------------------------
# Icons
#
# Emoji are this product's icon family, so they get the same discipline a font
# would: one glyph per concept, chosen once. The audit found 96 distinct glyphs
# across 1,066 uses, with a 43-glyph tail used three times or fewer — arrows in
# four incompatible weights, and three visual families mixed at random.
#
# This map is the vocabulary. Reach for an existing entry before adding a glyph;
# a new entry should name a concept the product genuinely did not have yet.
# ---------------------------------------------------------------------------

ICONS: dict[str, str] = {
    # status / feedback
    "warn": "⚠️",
    "ok": "✅",
    "error": "❌",
    "info": "ℹ️",
    "pending": "🔄",
    "stop": "🛑",
    "locked": "🔒",
    # market direction
    "long": "🟢",
    "short": "🔴",
    "up": "📈",
    "down": "📉",
    # modules (must match the home card)
    "trade": "🤖",
    "ask": "💬",
    "portfolio": "📁",
    "strategy": "🧠",
    "wallet": "💼",
    "points": "🏆",
    "vault": "💰",
    "alerts": "🔔",
    "referrals": "🎁",
    "settings": "⚙️",
    "links": "🔗",
    "mode": "🌐",
    "home": "🏠",
    # data
    "stats": "📊",
    "positions": "📋",
    "history": "🧾",
    "price": "🪙",
    "fees": "💸",
    "target": "🎯",
    "shield": "🛡",
    "clock": "⏱",
}


def icon(name: str) -> str:
    """Look up an icon, returning empty string for an unknown concept.

    Deliberately non-raising: a missing glyph should degrade a card, never take
    a trading screen down.
    """
    return ICONS.get(name, "")


# ---------------------------------------------------------------------------
# Navigation labels
#
# Hierarchical navigation and pagination are different actions and must not
# share a verb. The audit found trade history using "⬅ Back" for *previous
# page*, sitting next to "Next ➡", while "◀ Back" everywhere else in the bot
# means "go up one level" — so a user on page 2 taps what reads as "leave this
# screen" and instead moves one page.
#
# NOTE: these constants are for INLINE keyboards, which route by callback_data.
# Reply-keyboard labels route by their *translated text* through
# REPLY_BUTTON_MAP and i18n's reverse map, so renaming one is an i18n change,
# not a label change. Those are deliberately left alone here.
# ---------------------------------------------------------------------------

NAV_BACK = "◀ Back"
NAV_HOME = "🏠 Home"


def nav_back_to(destination: str) -> str:
    """A back button that names where it goes, e.g. ``◀ Back to Portfolio``."""
    return f"{NAV_BACK} to {destination}"


# Pagination gets its own glyph weight (‹ ›) so it never reads as hierarchical
# navigation (◀). Two vocabularies, because the lists differ:
#
#   chronological — history, performance, orders. All are sorted newest-first,
#   so "previous page" literally means more recent. "Newer/Older" says what the
#   tap does; "Prev/Next" makes the reader work out the sort order first.
#
#   ordinal — leaderboards, config pages. No time axis, so Prev/Next is correct
#   and Newer/Older would be nonsense.
#
# Picking the wrong pair is worse than the inconsistency it replaces, so check
# the sort before reaching for one.
NAV_NEWER = "‹ Newer"
NAV_OLDER = "Older ›"
NAV_PREV = "‹ Prev"
NAV_NEXT = "Next ›"


def pager(label: str) -> tuple[str, str]:
    """Prev/next labels for a screen with MORE THAN ONE paginated list.

    ``positions_view`` stacks a positions pager and an orders pager on one
    screen. Unlabelled arrows there are ambiguous — the user cannot tell which
    list a bare "‹ Newer" moves — so each pager keeps its list's name.
    """
    return f"‹ {label}", f"{label} ›"


# ---------------------------------------------------------------------------
# Numbers — deliberately NOT defined here.
#
# ``utils/visual.py`` already owns them: ``money`` (always separated),
# ``signed_money`` and ``pct`` (always signed), ``signed``. Nine modules use
# them, including the whole Portfolio v2 stack, and ``utils/`` is a leaf, which
# is the correct layer for value formatting.
#
# So F-05 was misdiagnosed in the plan. The problem is not that the product
# lacks a number formatter — it is that the legacy ``formatters.py`` card layer
# never adopted the one that exists, and hand-rolls f-strings instead. The fix
# is adoption, not a third source of truth. Import from utils.visual::
#
#     from src.nadobro.utils.visual import money, signed_money, pct
#
# Anything added here would be a duplicate that immediately starts drifting —
# which is the exact failure this module exists to end.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Callback toasts
#
# The tap-acknowledgement layer (Phase 2). Telegram gives a bot exactly one
# feedback channel for an inline-button tap: the answer to the callback query.
# The bot already spends it on a bare ``answer()`` before the per-user lock, so
# the spinner clears instantly — but a bare answer is silent, so a tap that
# takes two seconds looks identical to one that did nothing.
#
# ``toast_for`` turns that same single answer into a one-line present-tense
# acknowledgement — "Placing your order…", "Loading portfolio…" — derived from
# ``callback_data`` ALONE, because the pre-ack fires before the handler has run
# and before the lock is held. No lookup, no IO: a pure string map.
#
# Accuracy is the whole point, so the bias is deliberately conservative — an
# UNMAPPED callback keeps today's silent bare ack (no regression), while a WRONG
# verb would actively lie about what a tap does. So a verb is added only for a
# callback whose behaviour was read from the handler, and the screen-vs-execute
# distinction is honoured: ``pos:close_all`` merely opens a confirm dialog and
# is intentionally NOT mapped, while ``pos:confirm_close_all`` (which executes)
# is. Sub-flow micro-steps (a size tap, a leverage tap) stay silent too —
# toasting each one would be noise, not feedback.
#
# Localization is intentionally deferred to Phase 5 (the i18n/copy pass): these
# verbs are English here, and the pre-ack stays free of the cache/DB lookup a
# localized toast would require. The verb is transient (~1s) and is replaced by
# the fully-localized card, so the cost of English-for-now is small and bounded.
# ---------------------------------------------------------------------------


def toast_for(data: str) -> str | None:
    """Present-tense acknowledgement for an inline-button tap, or None.

    ``None`` means "no toast" — the caller falls back to today's bare ack. Pure
    and total: any unmapped or malformed ``data`` simply returns ``None``.
    """
    if not data:
        return None

    def pre(*prefixes: str) -> bool:
        return any(data.startswith(p) for p in prefixes)

    # --- actions that execute something (verb = what is happening now) ---
    if pre("card:trade:") and data.endswith(":confirm"):
        return "Placing your order…"
    if pre("exec_trade:"):
        return "Placing your order…"
    if pre("pos:close:"):
        return "Closing position…"
    if data == "pos:confirm_close_all":
        return "Closing everything…"
    if pre("strategy:startok:", "strategy:start:"):
        return "Starting strategy…"
    if data == "strategy:stop":
        return "Stopping strategy…"
    if pre("mode:"):
        return "Switching mode…"
    if data in ("refer:claim", "refer:autogen"):
        return "Claiming your code…"
    if pre("alert:del:"):
        return "Deleting alert…"

    # --- refreshes ---
    if data in ("nav:refresh", "status:refresh", "strategy:status"):
        return "Refreshing…"
    if data == "pos:view":
        return "Loading positions…"

    # --- screen loads that do real fetching (verb = loading X) ---
    if data == "portfolio:view":
        return "Loading portfolio…"
    if data == "portfolio:performance":
        return "Loading performance…"
    if pre("portfolio:history"):
        return "Loading history…"
    if data == "wallet:view":
        return "Loading wallet…"
    if data == "points:view":
        return "Loading points…"
    if data == "refer:view":
        return "Loading referrals…"
    if data == "alert:menu":
        return "Loading alerts…"
    if data == "settings:view":
        return "Loading settings…"
    if data == "vault:home":
        return "Loading vault…"
    if data == "resources:home":
        return "Opening links…"
    if data == "desk:view":
        return "Opening Nadobro…"
    if data == "card:trade:start":
        return "Opening trade console…"
    if data == "nav:strategy_hub":
        return "Loading strategies…"

    return None
