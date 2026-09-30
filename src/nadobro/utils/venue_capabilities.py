"""Venue capability table + callback/command classification (Arcus Phase 1).

Nado and Arcus run IN PARALLEL: ``users.active_venue`` only picks which venue's
screens a user SEES. The venue gate (``handlers/venue_gate.py``) uses this table
to decide, per update, what a user on the Arcus view may reach:

* ``NEVER_GATE`` — reduces/cancels Nado exposure or state (stop, close, cancel,
  remove), plus the read-only desk list (``desk:view`` / ``/desk``), the only
  entry to ``desk:stop`` (/stop_all does not stop desk plans). Always passes,
  whatever the venue: the owner rule is that every Nado stop path stays
  reachable from the Arcus view.
* ``NEUTRAL`` — venue-agnostic (``/venue``, help, language, onboarding language
  and terms). Always passes.
* ``DISPATCH`` — a view. Nado users get today's Nado screen; Arcus users get the
  Arcus render target instead (``ax:home`` / ``ax:settings`` / the "Not on Arcus
  yet" card).
* ``NADO_ONLY`` — opens Nado exposure or is a Nado-only feature. Denied on the
  Arcus view.
* ``ARCUS_ONLY`` — ``ax:*``. Denied on the Nado view.
* ``UNKNOWN`` — nothing the Nado router handles today. Denied on the Arcus view
  (fail-closed); passes on the Nado view exactly as today.

NEVER_GATE is exact strings and fully anchored patterns ONLY — never a keyword
or substring rule (``strategy:set:rgrid:rgrid_stop_pct:0.5`` is a config tap,
not a stop, and must stay NADO_ONLY).

Leaf module: stdlib + ``utils`` only (``utils`` has NO allowed outgoing package
edges). Pure data + pure, total functions; nothing here does IO.
``tests/handlers/test_venue_gate_coverage.py`` pins this table against every
``callback_data`` the bot emits, the ``handle_callback`` router, ``_handle_nav``,
the registered commands and the reply keyboard, so a new route cannot ship
unclassified.
"""
from __future__ import annotations

import re

from src.nadobro.utils.venue_scope import VENUE_ARCUS, VENUE_NADO

# --- classes ----------------------------------------------------------------
NEVER_GATE = "never_gate"
NEUTRAL = "neutral"
DISPATCH = "dispatch"
NADO_ONLY = "nado_only"
ARCUS_ONLY = "arcus_only"
UNKNOWN = "unknown"

CLASSES = (NEVER_GATE, NEUTRAL, DISPATCH, NADO_ONLY, ARCUS_ONLY, UNKNOWN)

# --- Arcus render targets for DISPATCH ---------------------------------------
# ax:home / ax:settings are also callback_data (ARCUS_ONLY); ax:unavailable is a
# render-only key (the "Not on Arcus yet" card) and has no callback route.
AX_HOME = "ax:home"
AX_HELP = "ax:help"
AX_SETTINGS = "ax:settings"
AX_UNAVAILABLE = "ax:unavailable"

# Per-venue capabilities. Phase 1: Arcus has none (no client, no trading).
VENUE_CAPABILITIES: dict[str, dict[str, frozenset[str]]] = {
    VENUE_NADO: {
        "strategies": frozenset({"grid", "rgrid", "dgrid", "mid", "dn", "vol", "bro"}),
        "features": frozenset({
            "trade", "portfolio", "wallet", "points", "referrals", "alerts", "copy",
            "desk", "vault", "howl", "brief", "news", "airdrop", "managed_agent",
            "mm_dashboard",
        }),
    },
    VENUE_ARCUS: {"strategies": frozenset(), "features": frozenset()},
}

# handlers/callbacks.py::handle_callback maps these BEFORE any routing (and so do
# messages._dispatch_reply_button and home_card.resolve_home_view).
CALLBACK_ALIASES: dict[str, str] = {
    "market:view": "points:view",
    "nav:market_radar": "points:view",
    "market:radar": "points:view",
    "home:market_radar": "points:view",
}

# ------------------------------------------------------------------ NEVER_GATE
NEVER_GATE_EXACT: frozenset[str] = frozenset({
    "strategy:stop",                 # strategy_handler: stop the running strategy
    "status:stop",                   # callbacks._handle_status_callback: stop from the status card
    "pos:close_all",                 # callbacks._handle_positions: close-all confirm screen
    "pos:confirm_close_all",         # callbacks._handle_positions: close every position
    "portfolio:close_all_confirm",   # portfolio_handler: close-all confirm screen
    "portfolio:close_all_yes",       # portfolio_handler: close every position
    "portfolio:cancel_all_confirm",  # portfolio_handler: cancel-all confirm screen
    "portfolio:cancel_all_yes",      # portfolio_handler: cancel every open order
    "wallet:revoke_steps",           # wallet_handler: 1CT revoke steps
    "wallet:revoke_confirm",         # wallet_handler: remove the stored 1CT signer
    "wallet:remove_active",          # wallet_handler: same effect as revoke_confirm (no emitter today)
    "cancel_trade",                  # callbacks: drop the pending trade
    "points:cancel",                 # callbacks._handle_points: cancel the LOWIQPTS request (self-answers)
    "vault:watch:off",               # vault_handler: stop the deposit watch
    "howl:dismiss",                  # callbacks._handle_howl: drop pending HOWL suggestions
    "trade:close",                   # callbacks._handle_trade: close picker (no emitter today)
    "trade:close_all",               # callbacks._handle_trade: close-all confirm (no emitter today)
    "desk:view",                     # desk_handler: the read-only desk list — its ONLY actions are
                                     # desk:stop:<id> and this refresh (desk:confirm stays NADO_ONLY)
})
NEVER_GATE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(re.compile(p) for p in (
    r"^copy:stop:\d+$",                               # copy_handler: stop a mirror
    r"^copy:pause:\d+$",                              # copy_handler: pause a mirror
    r"^desk:stop:[^:\s]+$",                           # desk_handler: stop a desk plan
    r"^desk:discard:[^:\s]+$",                        # desk_handler: discard a drafted plan
    r"^pos:close:[^:\s]+$",                           # callbacks._handle_positions: close one position
    r"^portfolio:cancel_order:(?:d:[0-9a-f]+|\d+)$",  # portfolio_handler / orders_view.cancel_callback_for
    r"^alert:del:\d+$",                               # alerts_handler: delete an alert
    r"^howl:reject:\d+$",                             # callbacks._handle_howl: reject one suggestion
))

# --------------------------------------------------------------------- NEUTRAL
NEUTRAL_EXACT: frozenset[str] = frozenset({
    "settings:language_menu",        # settings_handler: language picker
    "onb:accept_tos",                # callbacks._handle_onb_new: accept the terms
})
NEUTRAL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(re.compile(p) for p in (
    r"^venue:",                      # handlers/venue_handler re-checks flag + allowlist itself
    r"^resources:",                  # resources_handler: every resources:* renders the static links card
    r"^onb:lang:[a-z]{2}$",          # callbacks._handle_onb_new: onboarding language
    r"^settings:language:[a-z]{2}$", # settings_handler: set the language
))

# ------------------------------------------------------------ DISPATCH_BY_VENUE
DISPATCH_EXACT: dict[str, str] = {
    "nav:main": AX_HOME,             # callbacks._handle_nav: home
    "nav:refresh": AX_HOME,          # callbacks._handle_nav: home refresh
    "onboarding:resume": AX_HOME,    # callbacks._handle_onboarding: onboarded -> home
    "status:refresh": AX_HOME,       # callbacks._handle_status_callback: status card
    "strategy:status": AX_HOME,      # strategy_handler: status card
    "home:mode": AX_UNAVAILABLE,     # callbacks: Nado execution-mode card
    "settings:view": AX_SETTINGS,    # settings_handler: settings card
    "wallet:view": AX_UNAVAILABLE,   # wallet_handler (would mint a Nado 1CT key when unlinked)
    "pos:view": AX_UNAVAILABLE,      # callbacks._handle_positions: positions
    "portfolio:view": AX_UNAVAILABLE,
    "portfolio:refresh": AX_UNAVAILABLE,
    "portfolio:positions": AX_UNAVAILABLE,
    "portfolio:orders": AX_UNAVAILABLE,
    "portfolio:history": AX_UNAVAILABLE,
    "portfolio:performance": AX_UNAVAILABLE,
    "portfolio:analytics": AX_UNAVAILABLE,
    "portfolio:hours": AX_UNAVAILABLE,
    "mm:fills": AX_UNAVAILABLE,      # callbacks._handle_mm_dashboard
}
DISPATCH_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple((re.compile(p), t) for p, t in (
    (r"^portfolio:(?:view|refresh):[^:\s]+$", AX_UNAVAILABLE),          # portfolio_deck window tabs
    (r"^portfolio:positions:(?:\d+|pos:\d+|ord:\d+)$", AX_UNAVAILABLE),  # positions_view pagers
    (r"^portfolio:orders:\d+$", AX_UNAVAILABLE),                         # orders_view pager
    (r"^portfolio:history:\d+$", AX_UNAVAILABLE),                        # history_view pager
    (r"^portfolio:(?:performance|analytics):\d+$", AX_UNAVAILABLE),      # performance_view pager
    (r"^portfolio:session_trades:\d+:\d+$", AX_UNAVAILABLE),             # performance_view drill-down
    (r"^mm:status(?::refresh)?$", AX_UNAVAILABLE),                       # callbacks._handle_mm_dashboard
))

# ------------------------------------------------------------------ ARCUS_ONLY
ARCUS_ONLY_PREFIXES: tuple[str, ...] = ("ax:",)

# ------------------------------------------------------------------- NADO_ONLY
# Every prefix handle_callback routes. Anything under one of them that no earlier
# class claims is NADO_ONLY.
NADO_ONLY_EXACT: frozenset[str] = frozenset()
NADO_ONLY_PREFIXES: tuple[str, ...] = (
    "vault:", "card:trade:", "onboarding:", "trade:", "product:", "size:", "leverage:",
    "exec_trade:", "pos:", "portfolio:", "status:", "wallet:", "points:", "refer:", "alert:",
    "settings:", "strategy:", "copy:", "bro:", "howl:", "desk:", "mode:", "mm:",
    "onb:",          # onb:* other than lang/accept_tos (ignored by _handle_onb_new)
    "trade_flow:",   # reply-keyboard targets (keyboards.REPLY_BUTTON_MAP); never callback_data
)

# ----------------------------------------------------- nav: second-level router
# callbacks._handle_nav strips ``nav:`` ONCE. Its exact targets, then ONLY these
# forwarded prefixes; every other inner value renders "link not available", so
# e.g. ``nav:status:stop`` is NOT a stop path (it is UNKNOWN).
NAV_EXACT: dict[str, tuple[str, str | None]] = {
    "main": (DISPATCH, AX_HOME),
    "refresh": (DISPATCH, AX_HOME),
    "help": (NEUTRAL, None),
    "quick_start": (DISPATCH, AX_HOME),       # -> onboarding:resume (onboarded -> home)
    "mode": (DISPATCH, AX_UNAVAILABLE),
    "trade": (NADO_ONLY, None),
    "ask_nado": (NADO_ONLY, None),            # arms pending_question -> LLM chat
    "strategy_hub": (DISPATCH, AX_UNAVAILABLE),
}
NAV_FORWARDED_PREFIXES: tuple[str, ...] = (
    "strategy:", "copy:", "bro:", "settings:", "alert:", "wallet:", "portfolio:", "refer:",
)

# ----------------------------------------------------------- commands (main.py)
COMMANDS: dict[str, tuple[str, str | None]] = {
    "start": (DISPATCH, AX_HOME),
    "help": (NEUTRAL, None),
    "status": (DISPATCH, AX_HOME),
    "ops": (NEUTRAL, None),
    "stop_all": (NEVER_GATE, None),
    "revoke": (NEVER_GATE, None),            # 1CT revoke steps (a remove path)
    "agent_on": (NADO_ONLY, None),
    "agent_off": (NEVER_GATE, None),
    "agent_status": (NADO_ONLY, None),
    "brief": (NADO_ONLY, None),
    "howl": (NADO_ONLY, None),
    "news": (NADO_ONLY, None),
    "airdrop": (NADO_ONLY, None),
    "mm_status": (DISPATCH, AX_UNAVAILABLE),
    "mm_fills": (DISPATCH, AX_UNAVAILABLE),
    "desk": (NEVER_GATE, None),              # the read-only desk list (entry to desk:stop), as desk:view
    "venue": (NEUTRAL, None),
}


def _classify_flat(data: str) -> tuple[str, str | None]:
    if data in NEVER_GATE_EXACT or any(p.match(data) for p in NEVER_GATE_PATTERNS):
        return NEVER_GATE, None
    if data in NEUTRAL_EXACT or any(p.match(data) for p in NEUTRAL_PATTERNS):
        return NEUTRAL, None
    if data in DISPATCH_EXACT:
        return DISPATCH, DISPATCH_EXACT[data]
    for pat, target in DISPATCH_PATTERNS:
        if pat.match(data):
            return DISPATCH, target
    if data.startswith(ARCUS_ONLY_PREFIXES):
        return ARCUS_ONLY, None
    if data in NADO_ONLY_EXACT or data.startswith(NADO_ONLY_PREFIXES):
        return NADO_ONLY, None
    return UNKNOWN, None


def classify_callback(raw: object) -> tuple[str, str | None]:
    """``(class, arcus_render_target)`` for a ``callback_data`` string.

    Total and pure: None, ``""`` and garbage never raise (they are UNKNOWN).
    The render target is set only for DISPATCH.
    """
    data = raw if isinstance(raw, str) else ("" if raw is None else str(raw))
    data = CALLBACK_ALIASES.get(data, data)
    if data.startswith("nav:"):
        inner = data[len("nav:"):]
        if inner in NAV_EXACT:
            return NAV_EXACT[inner]
        if inner.startswith(NAV_FORWARDED_PREFIXES):
            return _classify_flat(inner)
        return UNKNOWN, None
    return _classify_flat(data)


def classify_command(name: object) -> tuple[str, str | None]:
    """``(class, arcus_render_target)`` for a bot command name (no slash).

    Case-insensitive like PTB's CommandHandler; unregistered names are UNKNOWN.
    """
    key = (name if isinstance(name, str) else ("" if name is None else str(name))).lower()
    return COMMANDS.get(key, (UNKNOWN, None))
