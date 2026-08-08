"""Turbo Volume preset — the coherent setting trio, per strategy.

The preset exists because leverage, the inventory allowance, and the session
SL interact: leverage scales uPnL as % of margin linearly (the rail's unit),
and the caps must fit at least one full-size fill or the book goes one-sided
after every fill. These tests pin the written values and the coherence rules.
"""

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.handlers.strategy_handler import (  # noqa: E402
    _TURBO_LEVERAGE_DEFAULT,
    _TURBO_SESSION_SL_PCT,
    _replace_mm_preset,
    _turbo_preset_settings,
)


def test_mid_turbo_writes_the_coherent_trio():
    cfg = _turbo_preset_settings("mid", product_max_leverage=50.0)
    assert cfg["mm_leverage_override"] == 10          # min(10, 50)
    assert cfg["mm_quote_mode"] == "touch"
    assert cfg["inventory_soft_limit_usd"] == 0.0     # auto = deployed
    assert cfg["max_net_exposure_pct"] == 100.0       # one full-size fill fits
    assert cfg["sl_pct"] == _TURBO_SESSION_SL_PCT
    assert cfg["tp_pct"] == 0.0                       # no session profit-stop
    assert cfg["interval_seconds"] == 5
    assert cfg["spread_bp"] == 2.0
    assert cfg["mm_preset"] == "turbo"


def test_leverage_is_capped_by_the_product_max():
    assert _turbo_preset_settings("mid", 5.0)["mm_leverage_override"] == 5
    assert _turbo_preset_settings("mid", 1.0)["mm_leverage_override"] == 1
    assert _turbo_preset_settings("grid", 40.0)["mm_leverage_override"] == int(
        _TURBO_LEVERAGE_DEFAULT
    )


def test_rgrid_dgrid_write_their_own_sl_keys_and_keep_tp():
    """rgrid/dgrid rails read rgrid_stop_loss_pct; their TP and level
    mechanics must NOT be disturbed (position exits flow through barriers)."""
    for sid, spread_key in (("rgrid", "rgrid_spread_bp"), ("dgrid", "dgrid_spread_bp")):
        cfg = _turbo_preset_settings(sid, 50.0)
        assert cfg["rgrid_stop_loss_pct"] == _TURBO_SESSION_SL_PCT
        assert cfg[spread_key] == 3.0
        assert "rgrid_take_profit_pct" not in cfg
        assert "tp_pct" not in cfg
        assert "levels" not in cfg


def test_grid_keeps_tp_and_touch_mode_stays_mid_only():
    cfg = _turbo_preset_settings("grid", 50.0)
    assert cfg["sl_pct"] == _TURBO_SESSION_SL_PCT
    assert "tp_pct" not in cfg
    assert "mm_quote_mode" not in cfg
    assert "inventory_soft_limit_usd" not in cfg


def test_session_sl_survives_typical_noise_at_turbo_leverage():
    """Coherence invariant: at the turbo leverage, the session SL (% of
    margin) must tolerate at least a 0.5% adverse price move on full
    one-sided inventory — otherwise routine noise auto-stops the bot and
    volume collapses (a stopped bot prints zero fills)."""
    tolerated_price_move_pct = _TURBO_SESSION_SL_PCT / _TURBO_LEVERAGE_DEFAULT
    assert tolerated_price_move_pct >= 0.5


def test_tiny_after_turbo_removes_turbo_only_settings():
    cfg = {"notional_usd": 250.0}
    _replace_mm_preset(cfg, "mid", "turbo", _turbo_preset_settings("mid", 50.0))
    _replace_mm_preset(
        cfg,
        "mid",
        "tiny",
        {
            "mm_leverage_override": 3,
            "min_order_notional_usd": 20.0,
            "mm_collateral_safety_factor": 1.10,
        },
    )

    assert cfg["mm_preset"] == "tiny"
    assert cfg["mm_leverage_override"] == 3
    assert cfg["min_order_notional_usd"] == 20.0
    for key in (
        "mm_quote_mode",
        "inventory_soft_limit_usd",
        "max_net_exposure_pct",
        "tp_pct",
        "sl_pct",
        "spread_bp",
        "interval_seconds",
    ):
        assert key not in cfg
    assert cfg["notional_usd"] == 250.0


def test_turbo_after_tiny_removes_tiny_only_settings():
    cfg = {
        "mm_preset": "tiny",
        "mm_leverage_override": 3,
        "min_order_notional_usd": 20.0,
        "mm_collateral_safety_factor": 1.10,
        "notional_usd": 250.0,
    }
    _replace_mm_preset(cfg, "mid", "turbo", _turbo_preset_settings("mid", 50.0))

    assert cfg["mm_preset"] == "turbo"
    assert cfg["mm_leverage_override"] == 10
    assert "min_order_notional_usd" not in cfg
    assert "mm_collateral_safety_factor" not in cfg
    assert cfg["mm_quote_mode"] == "touch"
    assert cfg["notional_usd"] == 250.0


def test_standard_clears_active_preset_but_keeps_unrelated_settings():
    cfg = {"notional_usd": 250.0}
    _replace_mm_preset(cfg, "mid", "turbo", _turbo_preset_settings("mid", 50.0))
    _replace_mm_preset(cfg, "mid", "standard")

    assert cfg == {"notional_usd": 250.0, "mm_preset": "standard"}


# ==========================================================================
# R-Grid taker-fee vs stop-loss headroom (2026-08-06)
# ==========================================================================
# Reverse Grid crosses the spread on both legs by design. At the leverage users
# actually run (the reported session was 49x on $100), one entry+exit costs more
# than the entire session stop budget — and the rail judges PnL NET of fees, so
# the session stops out on fees alone whichever way price goes.
def test_rgrid_step_is_capped_against_the_stop_budget():
    """The step per break is margin x leverage / levels, and both legs are taker —
    so leverage buys size but not stop budget. The engine caps the step so the
    stop covers several round trips instead of stopping out on the first."""
    from src.nadobro.handlers.strategy_handler import rgrid_stop_headroom

    # The reported session: $100 at 49x over 4 levels wants a $1,225 step, whose
    # round trip ($1.05) exceeds the whole $0.80 stop.
    room = rgrid_stop_headroom(
        {"notional_usd": 100.0, "levels": 4, "mm_leverage_override": 49}, 0.8
    )
    assert room["uncapped_step_usd"] == 1225.0
    assert room["capped"] is True
    assert room["step_usd"] < room["uncapped_step_usd"]
    # The bound is on the PYRAMID (levels x step) — $4,900 of exposure, not one
    # $1,225 break — so this configuration cannot be traded at any placeable size.
    # The cap lands on the venue floor and FLAGS it rather than certifying headroom
    # that does not exist (the old per-step bound claimed ~3 round trips fit).
    assert room["floored"] is True
    assert room["step_usd"] == 100.0
    assert room["round_trips"] < 3.0

    # The user's example: 50x on $100 over 10 steps with a 1% stop.
    thin = rgrid_stop_headroom(
        {"notional_usd": 100.0, "levels": 10, "mm_leverage_override": 50}, 1.0
    )
    assert thin["capped"] is True
    assert thin["uncapped_step_usd"] == 500.0 and thin["step_usd"] < 500.0
    # Also unreachable: a 10-level pyramid at the $100 floor is $1,000 of exposure,
    # whose round trip is $0.86 — 86% of the $1.00 stop. Flagged, not pretended.
    assert thin["floored"] is True and thin["round_trips"] < 3.0


def test_a_step_that_already_fits_is_left_alone():
    """A step fits when the PYRAMID's round trip fits inside the share of the stop.
    5x on $100 over 4 levels is a $125 step = $500 of pyramid, whose $0.43 round
    trip needs a stop of at least ~$1.30 to stay under the 33% share."""
    from src.nadobro.handlers.strategy_handler import rgrid_stop_headroom

    room = rgrid_stop_headroom(
        {"notional_usd": 100.0, "levels": 4, "mm_leverage_override": 5}, 5.0
    )
    assert room["capped"] is False
    assert room["step_usd"] == room["uncapped_step_usd"] == 125.0


def test_a_disarmed_stop_does_not_invent_a_budget_to_cap_against():
    from src.nadobro.handlers.strategy_handler import rgrid_stop_headroom

    room = rgrid_stop_headroom(
        {"notional_usd": 100.0, "levels": 4, "mm_leverage_override": 49}, 0.0
    )
    assert room["budget_usd"] == 0.0
    assert room["capped"] is False and room["step_usd"] == 1225.0


def test_a_stop_too_tight_for_the_venue_minimum_is_flagged_not_silently_unplaceable():
    """Below the venue's minimum order notional the cap cannot finish the job.
    Shipping an unplaceable size would be worse than saying so."""
    from src.nadobro.handlers.strategy_handler import rgrid_stop_headroom

    room = rgrid_stop_headroom(
        {"notional_usd": 100.0, "levels": 4, "mm_leverage_override": 49}, 0.05
    )
    assert room["floored"] is True
    assert room["step_usd"] >= 100.0, "never size below what the venue accepts"


def test_the_engine_and_the_card_agree_on_the_step():
    """The card must never quote a size the strategy does not place — both go
    through quant.rgrid_sizing.

    CARD-LEV-DIVERGE (self-review 2026-08-07): this test used to pass ONLY because
    every case pinned ``mm_leverage_override``, the one input where the card's old
    hand-written 3x default happened to agree with the engine. Every leverage path
    is exercised now — bare (engine falls back to 1x), the session ``leverage`` the
    start path writes from the PAIR MAX, and the explicit override. With the old
    card helper the 50x row rendered a $75 uncapped step against an engine that
    deployed $306.98 capped, so the stop-headroom warning was blind.
    """
    from decimal import Decimal

    from src.nadobro.handlers.strategy_handler import rgrid_stop_headroom
    from src.nadobro.strategy.engine_runtime import map_strategy_config

    base = {"notional_usd": 100.0, "levels": 4, "rgrid_stop_loss_pct": 0.8}
    for label, extra in (
        ("bare (no leverage anywhere)", {}),
        ("session leverage 5x", {"leverage": 5}),
        ("session leverage 50x — the pair max on BTC", {"leverage": 50}),
        ("explicit override 49x", {"mm_leverage_override": 49}),
        ("override wins over session", {"leverage": 3, "mm_leverage_override": 10}),
    ):
        conf = {**base, **extra}
        cfg = map_strategy_config(
            "rgrid", conf, Decimal(64500), product="BTC-PERP",
            # What run_engine_cycle passes: settings["leverage"] or 1.
            leverage=int(conf.get("leverage", 1) or 1),
        )
        room = rgrid_stop_headroom(conf, 0.8)
        assert float(cfg["order_amount_quote"]) == room["step_usd"], label
        assert bool(cfg["step_capped_by_stop"]) is room["capped"], label
        assert float(cfg["step_uncapped_quote"]) == room["uncapped_step_usd"], label


def test_the_card_reads_the_leverage_the_run_will_deploy_at():
    """The stored per-strategy config has no ``leverage`` — the session value is
    resolved at START from the pair max — so _conf_for_card must inject it or every
    size on the card is computed at the wrong leverage (CARD-LEV-DIVERGE)."""
    from types import SimpleNamespace

    from src.nadobro.handlers.strategy_handler import (
        _conf_for_card, _mm_effective_leverage,
    )
    from src.nadobro.config import get_product_max_leverage

    ctx = SimpleNamespace(user_data={"strategy_pair:rgrid": "BTC"})
    conf = _conf_for_card(
        {"strategies": {"rgrid": {"notional_usd": 100.0, "levels": 4}}},
        "rgrid", ctx, "mainnet",
    )
    pair_max = float(get_product_max_leverage("BTC", network="mainnet"))
    assert conf["leverage"] == pair_max, "the card must use the pair max, not 3x"
    assert _mm_effective_leverage(conf) == pair_max

    # An explicit override still wins, in the card exactly as in the engine.
    conf_ov = _conf_for_card(
        {"strategies": {"rgrid": {"notional_usd": 100.0, "mm_leverage_override": 7}}},
        "rgrid", ctx, "mainnet",
    )
    assert _mm_effective_leverage(conf_ov) == 7.0

    # Vol is always 1x; dn is clamped to 5x. Neither may inherit a perp pair max.
    assert _conf_for_card({"strategies": {"vol": {}}}, "vol", ctx, "mainnet")[
        "leverage"] == 1.0
    assert _conf_for_card(
        {"strategies": {"dn": {}}, "default_leverage": 20}, "dn", ctx, "mainnet",
    )["leverage"] == 5.0


def test_rgrid_risk_card_explains_the_cap_and_its_failure_modes():
    from src.nadobro.handlers.strategy_handler import _strategy_config_section_text

    def _card(**conf):
        base = {"notional_usd": 100.0, "levels": 4, "mm_leverage_override": 49,
                "rgrid_take_profit_pct": 1.2}
        base.update(conf)
        return _strategy_config_section_text("rgrid", base, "mainnet", "risk")

    # Capped: informational, tells the user what it cost them. Needs a stop big
    # enough that the PYRAMID's bound still lands above the venue minimum — at
    # 4 levels that is a budget over ~$1.05, i.e. an SL above ~1.05% of $100.
    capped = _card(rgrid_stop_loss_pct=2.0)
    assert "Step capped to" in capped and "round trips" in capped
    # Floored but workable: thin, not broken.
    thin = _card(rgrid_stop_loss_pct=0.8)
    assert "Thin" in thin and "venue minimum" in thin
    # Floored and unworkable: fees exceed the whole stop even at the minimum.
    broken = _card(rgrid_stop_loss_pct=0.05)
    assert "too tight to trade" in broken and "fees" in broken
    # Disarmed: say the cap is not protecting anything.
    off = _card(rgrid_stop_loss_pct=0.0, sl_pct=0.0)
    assert "disarmed" in off
    # Comfortable: no note, but the numbers are still shown.
    calm = _card(mm_leverage_override=5, rgrid_stop_loss_pct=5.0)
    assert "Step capped" not in calm and "Thin" not in calm
    assert "Stop budget" in calm and "per break" in calm


def test_rgrid_stop_headroom_counts_round_trips_against_the_margin_budget():
    from src.nadobro.handlers.strategy_handler import rgrid_stop_headroom

    room = rgrid_stop_headroom(
        {"notional_usd": 100.0, "levels": 10, "mm_leverage_override": 50}, 1.0
    )
    assert room["budget_usd"] == 1.0                 # 1% of $100 margin
    # Round trips are counted against the PYRAMID (levels x step), which is the
    # exposure an exit actually closes. The uncapped $500 step is $5,000 of
    # pyramid — $4.30 a round trip against a $1.00 stop — so even the floored $100
    # step leaves barely one, and the card says so instead of claiming three.
    assert round(room["uncapped_step_usd"] * 10 * 0.00086, 2) == 4.30
    assert room["floored"] is True and room["round_trips"] < 3.0
