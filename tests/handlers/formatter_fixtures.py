"""Deterministic fixtures for the card-render snapshot suite.

Phase 0 of the interface overhaul: freeze the current rendered surface so the
mechanical refactors in later phases produce a reviewable diff instead of an
act of faith. See ``test_formatter_snapshots.py``.

Every fixture here is a plain literal — no clock, no DB, no venue. The two
time-dependent helpers in ``formatters`` (``_fmt_uptime`` / ``_fmt_age_seconds``)
are frozen by the test module, not here.

Cases deliberately cover BOTH the populated and the zero/empty branch of every
data-driven card, because the empty branches are what Phase 4 rewrites.
"""

from __future__ import annotations

from decimal import Decimal

# --------------------------------------------------------------------------
# building blocks
# --------------------------------------------------------------------------

POSITION_LONG = {
    "product_name": "BTC-PERP",
    "side": "LONG",
    "amount": 0.05,
    "price": 92000.0,
    "liquidation_price": 84150.25,
}

POSITION_SHORT = {
    "product_name": "ETH-PERP",
    "side": "SHORT",
    "amount": 1.5,
    "price": 3120.40,
    "liquidation_price": 3480.0,
}

PRICES = {
    "BTC": {"mid": 93250.75},
    "ETH": {"mid": 3080.10},
}

STATS = {
    "total_trades": 128,
    "total_volume": 1_284_500.0,
    "total_pnl": 842.19,
    "total_fees": 61.44,
    "total_funding": -12.08,
    "win_rate": 57.8,
    "wins": 74,
    "losses": 54,
    "closed": 128,
    "filled": 126,
    "failed": 2,
    "count": 128,
    "volume_windows": {"24h": 41_200.0, "7d": 288_400.0, "30d": 1_101_900.0},
    "by_product": {"BTC": {"count": 80, "pnl": 611.02}, "ETH": {"count": 48, "pnl": 231.17}},
}

STATS_EMPTY = {
    "total_trades": 0,
    "total_volume": 0.0,
    "total_pnl": 0.0,
    "total_fees": 0.0,
    "total_funding": 0.0,
    "win_rate": 0.0,
    "wins": 0,
    "losses": 0,
    "closed": 0,
    "filled": 0,
    "failed": 0,
    "count": 0,
    "volume_windows": {"24h": 0.0, "7d": 0.0, "30d": 0.0},
    "by_product": {},
}

OPEN_ORDERS = [
    {
        "product_name": "BTC-PERP",
        "product": "BTC",
        "side": "LONG",
        "amount": 0.02,
        "price": 90500.0,
        "limit_price": 90500.0,
        "requested_size": 0.02,
        "filled_size": 0.0,
        "status": "open",
        "type": "limit",
        "created_at": "2026-08-30T11:04:00+00:00",
    }
]

TRADES = [
    {
        "product": "BTC",
        "side": "LONG",
        "price": 91000.0,
        "close_price": 92400.0,
        "pnl": 70.0,
        "status": "closed",
        "created_at": "2026-08-30T09:15:00+00:00",
    },
    {
        "product": "ETH",
        "side": "SHORT",
        "price": 3200.0,
        "close_price": 3260.0,
        "pnl": -90.0,
        "status": "closed",
        "created_at": "2026-08-30T10:02:00+00:00",
    },
]

ALERTS = [
    {"id": 11, "product": "BTC", "kind": "price", "condition": "above", "target": 95000.0, "network": "mainnet"},
    {"id": 12, "product": "ETH", "kind": "price", "condition": "below", "target": 2900.0, "network": "mainnet"},
]

WALLET_LINKED = {
    "is_linked": True,
    "network": "mainnet",
    "active_address": "0x1111111111111111111111111111111111111111",
    "linked_signer_address": "0x2222222222222222222222222222222222222222",
    "current_signer": "0x2222222222222222222222222222222222222222",
    "expected_signer": "0x2222222222222222222222222222222222222222",
    "signer_verification": {"verified": True},
    "verified": True,
}

WALLET_UNLINKED = {
    "is_linked": False,
    "network": "testnet",
    "active_address": None,
    "linked_signer_address": None,
}

STATUS_RUNNING = {
    "running": True,
    "strategy": "grid",
    "product": "BTC",
    "network": "mainnet",
    "is_paused": False,
    "funded": True,
    "has_key": True,
    "onboarding_complete": True,
    "runs": 412,
    "orders_placed": 806,
    "orders_filled": 611,
    "orders_cancelled": 195,
    "maker_fill_ratio": 0.76,
    "cancellation_ratio": 0.24,
    "active_positions": 2,
    "trade_count": 88,
    "total_pnl": 118.40,
    "session_realized_pnl_usd": 96.10,
    "session_fees_usd": 14.22,
    "session_funding_usd": -2.06,
    "session_volume_usd": 92_400.0,
    "notional_usd": 100.0,
    "cycle_notional_usd": 100.0,
    "spread_bp": 8,
    "interval_seconds": 60,
    "next_cycle_in": 24,
    "last_cycle_ms": 512,
    "last_cycle_result": "placed",
    "last_action": "placed 4 orders",
    "started_at": "2026-08-31T18:00:00+00:00",
    "worker_last_heartbeat": 1_756_000_000.0,
}

STATUS_STOPPED = {
    "running": False,
    "strategy": "grid",
    "network": "mainnet",
    "funded": False,
    "has_key": False,
    "onboarding_complete": False,
    "missing_step": "wallet",
}

ONBOARDING_DONE = {"onboarding_complete": True, "funded": True, "has_key": True}
ONBOARDING_TODO = {"onboarding_complete": False, "funded": False, "has_key": False, "missing_step": "wallet"}

OPS = {
    "runtime": {"NADO_RUNTIME_MODE": "webhook", "NADO_STRATEGY_WORKERS": "4"},
    "runtime_env": {"NADO_RUNTIME_MODE": "webhook"},
    "queue": {"strategy_qsize": 3, "strategy_qmax": 256, "strategy_enqueued": 9012},
    "stats": {"ok_cycles": 8800, "failed_cycles": 12, "cycle_timeouts": 1, "zero_order_cycles": 40},
    "perf": {"strategy_workers_running": 4, "strategy_workers_target": 4},
    "account_snapshot": {"positions_count": 2, "open_orders_count": 6},
}

POINTS_OK = {
    "ok": True,
    "points": 18420.5,
    "volume_usd": 1_284_500.0,
    "total_costs": 612.40,
    "cost_per_point": 0.0332,
    "ppm": 14.34,
    "window_label": "30d",
}

POINTS_EMPTY = {"ok": True, "no_activity": True, "window_label": "30d"}

REFERRAL = {
    # ``share_code`` is a nested dict, not a string — the formatter reads
    # public_code/link/redemption_count off it.
    "share_code": {
        "public_code": "JAY7",
        "link": "https://t.me/nadobro_bot?start=JAY7",
        "redemption_count": 6,
    },
    "network": "mainnet",
    "total_referrals": 6,
    "total_referred_trades": 214,
    "total_referred_volume": 412_900.0,
    "min_code_len": 3,
    "max_code_len": 12,
    "referred_users": [
        {"username": "alpha_ape", "referred_user_id": 900001, "referred_trade_count": 120, "referred_volume_usd": 260_400.0},
        {"username": "", "referred_user_id": 900002, "referred_trade_count": 94, "referred_volume_usd": 152_500.0},
    ],
}

REFERRAL_EMPTY = {
    # No code claimed yet -> the "Claim your code" branch.
    "share_code": {},
    "network": "mainnet",
    "total_referrals": 0,
    "total_referred_trades": 0,
    "total_referred_volume": 0.0,
    "min_code_len": 3,
    "max_code_len": 12,
    "referred_users": [],
}

SETTINGS = {"default_leverage": 5, "risk_profile": "balanced", "slippage": 0.5}

TRADE_OK = {
    "success": True,
    "status": "filled",
    "product": "BTC",
    "side": "LONG",
    "size": 0.05,
    "price": 92000.0,
    "fee": 4.14,
    "type": "market",
    "network": "mainnet",
    "tp_requested": True,
    "tp_set": True,
    "tp_price": 96000.0,
    "sl_requested": True,
    "sl_armed": True,
    "sl_price": 88000.0,
}

TRADE_FAIL = {
    "success": False,
    "status": "rejected",
    "product": "BTC",
    "side": "LONG",
    "size": 0.05,
    "network": "mainnet",
    "error": "insufficient margin (need $412.00, have $180.20)",
}

BRACKET_OK = {
    "success": True,
    "product": "BTC",
    "network": "mainnet",
    "tp_requested": True,
    "tp_set": True,
    "tp_price": 96000.0,
    "sl_requested": True,
    "sl_armed": True,
    "sl_price": 88000.0,
}

# TP rejected but SL armed. NOTE: this is success=True — the card only reaches
# its per-leg branches on the success path; success=False short-circuits to the
# generic failure card (see BRACKET_FAILED).
BRACKET_PARTIAL = {
    "success": True,
    "product": "BTC",
    "network": "mainnet",
    "tp_requested": True,
    "tp_set": False,
    "tp_error": "trigger too close to mark",
    "sl_requested": True,
    "sl_armed": True,
    "sl_price": 88000.0,
}

BRACKET_FAILED = {
    "success": False,
    "product": "BTC",
    "network": "mainnet",
    "error": "no open position to attach TP/SL to",
}

LIMIT_CLOSE_OK = {
    "success": True,
    "product": "BTC",
    "side": "SHORT",
    "size": 0.05,
    "limit_price": 93400.0,
    "network": "mainnet",
}

LIMIT_CLOSE_FAIL = {
    "success": False,
    "product": "BTC",
    "network": "mainnet",
    "error": "no open position",
}

BALANCE = {"exists": True, "balances": {"0": 2481.09}}
BALANCE_MISSING = {"exists": False, "balances": {}}

STRATEGY_CONF = {"notional_usd": 100.0, "spread_bp": 8, "interval_seconds": 60, "tp_pct": 1.5, "sl_pct": 0.75}


# --------------------------------------------------------------------------
# case registry — (case_id, formatter_name, args, kwargs)
#
# case_id is the stable snapshot key. Never rename one without regenerating;
# renaming loses the history of that screen.
# --------------------------------------------------------------------------

CASES: list[tuple[str, str, tuple, dict]] = [
    # --- static cards (no inputs) ---
    ("alert_menu_intro", "fmt_alert_menu_intro", (), {}),
    ("alert_product_prompt", "fmt_alert_product_prompt", (), {}),
    ("close_all_confirm", "fmt_close_all_confirm", (), {}),
    ("dashboard_home", "fmt_dashboard_home", (), {}),
    ("getting_started", "fmt_getting_started", (), {}),
    ("help", "fmt_help", (), {}),
    ("home_header", "fmt_home_header", (), {}),
    ("managed_agent_disabled", "fmt_managed_agent_disabled", (), {}),
    ("managed_agent_enabled", "fmt_managed_agent_enabled", (), {}),
    ("managed_agent_globally_disabled", "fmt_managed_agent_globally_disabled", (), {}),
    ("revoke_card", "fmt_revoke_card", (), {}),
    ("strategy_hub_intro", "fmt_strategy_hub_intro", (), {}),
    ("wallet_balance_error", "fmt_wallet_balance_error", (), {}),
    ("wallet_revoke_steps_card", "fmt_wallet_revoke_steps_card", (), {}),

    # --- home / mode ---
    ("home_command_center__mainnet", "fmt_home_command_center_card", ("mainnet", "$2,481.09"), {}),
    ("home_command_center__testnet", "fmt_home_command_center_card", ("testnet", "$0.00"), {}),
    ("home_command_center__updating", "fmt_home_command_center_card", ("mainnet", "updating…"), {}),
    ("home_command_center__na", "fmt_home_command_center_card", ("mainnet", "N/A"), {}),
    ("mode_view__mainnet", "fmt_mode_view", ("mainnet",), {}),
    ("mode_view__testnet", "fmt_mode_view", ("testnet",), {}),

    # --- positions / portfolio ---
    ("positions__empty", "fmt_positions", ([],), {}),
    ("positions__empty_with_mode", "fmt_positions", ([],), {"mode_label": "🌐 MAINNET"}),
    ("positions__two", "fmt_positions", ([POSITION_LONG, POSITION_SHORT],), {"prices": PRICES, "mode_label": "🌐 MAINNET"}),
    ("positions__no_prices", "fmt_positions", ([POSITION_LONG],), {}),
    ("portfolio__populated", "fmt_portfolio", (STATS, [POSITION_LONG, POSITION_SHORT]), {"prices": PRICES, "open_orders": OPEN_ORDERS, "mode_label": "🌐 MAINNET"}),
    ("portfolio__empty", "fmt_portfolio", (STATS_EMPTY, []), {"mode_label": "🌐 MAINNET"}),
    ("analytics__populated", "fmt_analytics", (STATS,), {"mode_label": "🌐 MAINNET"}),
    ("analytics__empty", "fmt_analytics", (STATS_EMPTY,), {}),

    # --- history ---
    ("trade_history__populated", "fmt_trade_history", (TRADES,), {"mode_label": "🌐 MAINNET"}),
    ("trade_history__empty", "fmt_trade_history", ([],), {}),

    # --- alerts ---
    ("alerts__populated", "fmt_alerts", (ALERTS,), {}),
    ("alerts__empty", "fmt_alerts", ([],), {}),
    ("alert_condition_prompt", "fmt_alert_condition_prompt", ("BTC",), {}),
    ("alert_target_prompt", "fmt_alert_target_prompt", ("BTC", "above", "95000"), {}),

    # --- wallet ---
    ("wallet_info__linked", "fmt_wallet_info", (WALLET_LINKED,), {}),
    ("wallet_info__unlinked", "fmt_wallet_info", (WALLET_UNLINKED,), {}),
    ("balance__present", "fmt_balance", (BALANCE,), {"wallet_addr": "0x1111111111111111111111111111111111111111"}),
    ("balance__missing", "fmt_balance", (BALANCE_MISSING,), {}),
    ("wallet_balance_card", "fmt_wallet_balance_card", (2481.09,), {}),
    ("wallet_connect_card", "fmt_wallet_connect_card", ("0x" + "ab" * 32,), {}),
    ("ink_airdrop_card", "fmt_ink_airdrop_card", ("0x1111111111111111111111111111111111111111", Decimal("1234.5678")), {}),

    # --- trading results ---
    ("trade_preview", "fmt_trade_preview", ("LONG", "BTC", 0.05, 92000.0), {"leverage": 10, "est_margin": 460.0}),
    ("trade_result__filled", "fmt_trade_result", (TRADE_OK,), {}),
    ("trade_result__rejected", "fmt_trade_result", (TRADE_FAIL,), {}),
    ("bracket_result__both_set", "fmt_bracket_result", (BRACKET_OK,), {}),
    ("bracket_result__partial", "fmt_bracket_result", (BRACKET_PARTIAL,), {}),
    ("bracket_result__failed", "fmt_bracket_result", (BRACKET_FAILED,), {}),
    ("limit_close__ok", "fmt_limit_close_result", (LIMIT_CLOSE_OK,), {}),
    ("limit_close__fail", "fmt_limit_close_result", (LIMIT_CLOSE_FAIL,), {}),
    ("stop_all__ok", "fmt_stop_all_result", (True, "3 strategies stopped", "Check Positions if exposure remains."), {}),
    ("stop_all__fail", "fmt_stop_all_result", (False, "gateway timeout", "Retry in a moment."), {}),

    # --- strategy / status / ops ---
    ("status_overview__running", "fmt_status_overview", (STATUS_RUNNING, ONBOARDING_DONE), {}),
    ("status_overview__stopped", "fmt_status_overview", (STATUS_STOPPED, ONBOARDING_TODO), {}),
    ("strategy_update", "fmt_strategy_update", ("grid", "mainnet", STRATEGY_CONF), {}),
    ("ops_overview", "fmt_ops_overview", (STATUS_RUNNING, OPS), {}),

    # --- points / referrals ---
    ("points__populated", "fmt_points_dashboard", (POINTS_OK,), {}),
    ("points__no_activity", "fmt_points_dashboard", (POINTS_EMPTY,), {}),
    ("referral__populated", "fmt_referral_dashboard", (REFERRAL,), {}),
    ("referral__empty", "fmt_referral_dashboard", (REFERRAL_EMPTY,), {}),

    # --- settings / misc ---
    ("settings", "fmt_settings", (SETTINGS,), {}),
    ("managed_agent_status", "fmt_managed_agent_status", (True, True, "2026-08-31T18:00:00+00:00"), {}),
    ("bro_answer_card", "fmt_bro_answer_card", ("BTC funding is mildly positive on Nado right now.",), {"mode": "fast", "sources": ["nado-indexer"]}),

    # --- price helper (not a card, but a shared primitive Phase 1 replaces) ---
    ("price__btc", "fmt_price", (93250.756,), {"product": "BTC"}),
    ("price__sub_dollar", "fmt_price", (0.4218,), {"product": "DOGE"}),
    ("price__zero", "fmt_price", (0,), {"product": "BTC"}),
]
