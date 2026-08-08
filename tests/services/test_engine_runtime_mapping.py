"""Unit tests for the engine_runtime settings->config mapping, risk-limit
mapping, product-metadata builder, and the feature gate. Pure logic — no venue."""
from __future__ import annotations

from decimal import Decimal

from src.nadobro.engine.types import TripleBarrierConfig
from src.nadobro.strategy import engine_runtime as er


def test_map_grid_config_centers_band_and_sets_barriers():
    # Classic static-ladder mapping (grid now defaults to fill-anchored; the
    # ladder is the opt-out escape via fill_anchored=0).
    cfg = er.map_strategy_config(
        "grid",
        {"notional_usd": 75.0, "cycle_notional_usd": 75.0, "spread_bp": 4.0,
         "levels": 2, "tp_pct": 0.6, "sl_pct": 0.5, "fill_anchored": 0},
        Decimal(100), product="BTC-USDC", leverage=3,
    )
    assert cfg["trading_pair"] == "BTC-USDC"
    assert cfg["min_spread_between_orders"] == Decimal("0.0004")
    assert cfg["max_open_orders"] == 2
    # SIZING (2026-06-21): deployed notional = margin x effective leverage, so a
    # $75 margin at 3x quotes $225 across the ladder (was margin-only $75).
    assert cfg["total_amount_quote"] == Decimal("225")
    assert cfg["leverage"] == 3
    # POST-ONLY-CROSS fix: the near-mid boundary is offset onto the maker side by
    # max(step/2, 1.5bp). step=0.0004 -> maker_offset=0.0002; span=1*0.0004.
    # BUY band steps DOWN from (mid - maker_offset): start=100*(1-0.0002-0.0004),
    # end=100*(1-0.0002).
    assert cfg["start_price"] == Decimal("99.94")
    assert cfg["end_price"] == Decimal("99.98")
    # Knobs for DynamicGridController side-correct band rebuilds.
    assert cfg["step_pct"] == Decimal("0.0004")
    assert cfg["levels_count"] == 2
    # GRID-DUAL-UNIT fix (f391f3c): limit_price is NO LONGER auto-derived from
    # sl_pct. A mid-anchored hard stop fired on a brief wick to mid*(1-sl) even
    # when little had filled — a premature stop-out. SL now lives in the
    # fill-aware avg-entry barrier (triple_barrier_config.stop_loss) + the
    # fee-aware session rail. limit_price stays available as an explicit
    # catastrophic stop but defaults to 0 (disabled).
    assert cfg["limit_price"] == Decimal("0")
    # UNITS INVARIANT: the user's %-of-margin SL/TP are the session rail's, not a
    # per-level avg-entry price barrier. They reach the controller as percents.
    assert "triple_barrier_config" not in cfg
    assert float(cfg["tp_pct"]) == 0.6 and float(cfg["sl_pct"]) == 0.5


def test_map_dn_config_defaults():
    cfg = er.map_strategy_config(
        "dn",
        {"notional_usd": 500.0, "fixed_margin_usd": 500.0},
        Decimal(715), product="QQQ",
    )
    assert cfg["trading_pair_long"] == "QQQ-USDT0"   # spot leg (USDT0-quoted)
    assert cfg["trading_pair_short"] == "QQQ-PERP"    # perp leg
    assert cfg["leg_amount_quote"] == Decimal("500")
    assert cfg["hedge_ratio"] == Decimal("1")
    assert cfg["hold_seconds"] == 3600                # default 1h
    assert cfg["cycles"] == 1
    assert cfg["leverage"] == 1                       # strictly 1x short
    # Per-leg TP/SL are OFF by default — a one-sided TP would break the hedge.
    tb = cfg["barriers"]
    assert isinstance(tb, TripleBarrierConfig)
    assert tb.take_profit is None and tb.stop_loss is None


def test_map_dn_clamps_hold_and_sets_cycles():
    cfg = er.map_strategy_config(
        "dn",
        {"fixed_margin_usd": 250.0, "dn_hold_seconds": 999999.0,
         "dn_cycles": 5.0, "dn_cycle_gap_seconds": 45.0, "dn_hedge_ratio": 1.0},
        Decimal(100), product="AAPL",
    )
    assert cfg["hold_seconds"] == 86400               # clamped to 24h
    assert cfg["cycles"] == 5
    assert cfg["cycle_gap_seconds"] == 45


def test_map_rgrid_is_exposure_anchored_with_no_static_band():
    """Reverse Grid is its own market-making engine, not a one-sided ladder.

    There is no static band to place: both legs are derived each tick from the
    average of the buy/sell exposure prices. ``fill_anchored`` is not a choice
    here — setting it to 0 must NOT hand R-Grid to another engine (that wiring
    routed R-Grid to the D-Grid phase switcher, which is what produced
    "switched RGRID → GRID" on R-Grid sessions).
    """
    from src.nadobro.strategy.strategy_registry import effective_sl_tp_pct

    for settings in (
        {"notional_usd": 100.0, "spread_bp": 10.0, "levels": 4, "sl_pct": 0.8},
        {"notional_usd": 100.0, "spread_bp": 10.0, "levels": 4, "sl_pct": 0.8,
         "fill_anchored": 0},
    ):
        cfg = er.map_strategy_config("rgrid", settings, Decimal(100), product="BTC-USDC")
        assert cfg["controller_override"] == "fill_anchored"
        assert cfg["anchor_mode"] == "rgrid"
        assert cfg["passive_only"] is True, "every MM strategy rests post-only limits"
        # No static ladder geometry, and no phase-switcher knobs.
        for dead in ("start_price", "end_price", "dgrid_trend_on_vr", "dgrid_flip_confirm_ticks"):
            assert dead not in cfg, dead
        # SL/TP are the %-of-margin SESSION rails (live PnL incl. uPnL, net of
        # fees), exactly as the spec describes them — never ALSO a price-move
        # barrier on the same user number (CLAUDE.md units invariant).
        assert "triple_barrier_config" not in cfg
        assert effective_sl_tp_pct(
            "rgrid", {"rgrid_stop_loss_pct": 2.0, "rgrid_take_profit_pct": 5.0}
        ) == (2.0, 5.0)


def test_map_mid_config():
    cfg = er.map_strategy_config(
        "mid", {"notional_usd": 100.0, "spread_bp": 5.0, "levels": 2,
                "inventory_soft_limit_usd": 60.0},
        Decimal(100), product="BTC-USDC",
    )
    assert cfg["spread_bid_pct"] == Decimal("0.0005")
    assert cfg["spread_ask_pct"] == Decimal("0.0005")
    # order_amount_quote is the notional deployed per SIDE ($100 margin × 1x).
    # The ladder subdivides it across ``levels`` — the total is the same either
    # way, so this number does not move when levels change.
    assert cfg["order_amount_quote"] == Decimal("100")
    assert cfg["ladder_levels"] == 2
    assert cfg["max_base_quote"] == Decimal("60")


def test_mid_regime_gate_defaults_off_and_is_rearmable():
    """MID-GATE-DEFAULT (2026-07-31): the regime gate is OFF by default for Mid
    — same decision GRID-IN-TRENDS made for grid/rgrid. Session 178 post-mortem:
    the gate read PAUSE 46-61% of minutes on Nado's thin books and a paused
    FLAT book quotes nothing, so Mid sessions sat dark ($0 volume). An explicit
    regime_gate_enabled=1 must still re-arm it (user override)."""
    base = {"notional_usd": 100.0, "spread_bp": 5.0}
    cfg = er.map_strategy_config("mid", dict(base), Decimal(100), product="BTC-PERP")
    assert not bool(cfg["regime_gate_enabled"])

    armed = er.map_strategy_config(
        "mid", {**base, "regime_gate_enabled": 1.0}, Decimal(100), product="BTC-PERP",
    )
    assert bool(armed["regime_gate_enabled"])

    disarmed = er.map_strategy_config(
        "mid", {**base, "regime_gate_enabled": 0.0}, Decimal(100), product="BTC-PERP",
    )
    assert not bool(disarmed["regime_gate_enabled"])


def test_negative_min_spread_bp_is_the_no_floor_sentinel():
    """Mid's SHIPPED registry default is min_spread_bp = -10 ("user-tunable in
    [-10, +100]" — strategy_registry) and means NO spread floor: the
    Tread-parity volume product may concede spread all the way to 0. The clamp
    must map any negative to floor 0, never reinterpret it as garbage and
    substitute the 1.5bp fee floor (audit 2026-07-31 caught exactly that
    regression: it would have silently widened every default mid config)."""
    neg = er.map_strategy_config(
        "mid", {"notional_usd": 100.0, "spread_bp": 5.0, "min_spread_bp": -10.0},
        Decimal(100), product="BTC-PERP",
    )
    assert neg["spread_floor_half_pct"] == Decimal(0)

    zero = er.map_strategy_config(
        "mid", {"notional_usd": 100.0, "spread_bp": 5.0, "min_spread_bp": 0.0},
        Decimal(100), product="BTC-PERP",
    )
    assert zero["spread_floor_half_pct"] == Decimal(0)


def test_participation_chunk_overrides_per_order_size():
    """mm_cycle_notional_usd (set by bot_runtime at start when a participation
    preset is active) replaces the deployed-based per-order size across the MM
    family; 0/unset keeps the deployed sizing."""
    # chunk 30 is distinct from every default (mid 100, grid/rgrid 100/4=25, dgrid 100).
    mid = er.map_strategy_config(
        "mid", {"notional_usd": 100.0, "spread_bp": 5.0, "mm_cycle_notional_usd": 30.0},
        Decimal(100), product="BTC-USDC",
    )
    assert mid["order_amount_quote"] == Decimal("30")
    grid = er.map_strategy_config(
        "grid", {"notional_usd": 100.0, "levels": 4, "fill_anchored": 1, "mm_cycle_notional_usd": 30.0},
        Decimal(100), product="BTC-USDC",
    )
    assert grid["order_amount_quote"] == Decimal("30")
    # RGRID-STEP-CLAMP (2026-08-03): rgrid fires ONE taker per break, so
    # order_amount_quote is a per-trade RISK parameter, not a throughput target.
    # A participation chunk may only make that step SMALLER. Since the POV floor
    # is capped at the deployed budget, an unclamped chunk turned a $25 step into
    # the whole $100 budget — one break at 3.3x the net-exposure cap.
    rg = er.map_strategy_config(
        "rgrid", {"notional_usd": 100.0, "levels": 4, "fill_anchored": 1, "mm_cycle_notional_usd": 30.0},
        Decimal(100), product="BTC-USDC",
    )
    assert rg["order_amount_quote"] == Decimal("25"), "POV must not enlarge the rgrid step"
    rg_small = er.map_strategy_config(
        "rgrid", {"notional_usd": 100.0, "levels": 4, "fill_anchored": 1, "mm_cycle_notional_usd": 10.0},
        Decimal(100), product="BTC-USDC",
    )
    assert rg_small["order_amount_quote"] == Decimal("10"), "POV must still be able to shrink it"
    dg = er.map_strategy_config(
        "dgrid", {"notional_usd": 100.0, "mm_cycle_notional_usd": 30.0},
        Decimal(100), product="BTC-USDC",
    )
    assert dg["total_amount_quote"] == Decimal("30")
    # Opt-out: no chunk → deployed-based sizing unchanged.
    mid0 = er.map_strategy_config(
        "mid", {"notional_usd": 100.0, "spread_bp": 5.0}, Decimal(100), product="BTC-USDC",
    )
    assert mid0["order_amount_quote"] == Decimal("100")


def test_fill_anchored_reset_threshold_reads_ui_keys():
    """The UI writes the reset threshold under grid_reset_threshold_pct /
    rgrid_reset_threshold_pct, but the default (fill-anchored) grid/rgrid
    controller reads reset_threshold_pct. The mapping must translate the UI keys
    or the "Reset Threshold" button is a silent no-op (the controller would
    always use its 0.25% / 0.125% default). Regression guard for that bug."""
    # Grid: button writes grid_reset_threshold_pct=0.8 (%). Expect 0.8/100.
    grid = er.map_strategy_config(
        "grid", {"notional_usd": 100.0, "levels": 4, "fill_anchored": 1,
                 "grid_reset_threshold_pct": 0.8},
        Decimal(100), product="BTC-USDC",
    )
    assert grid["reset_threshold_pct"] == Decimal("0.8") / Decimal(100)
    # R-Grid: button writes rgrid_reset_threshold_pct=1.5 (%). Expect 1.5/100.
    rgrid = er.map_strategy_config(
        "rgrid", {"notional_usd": 100.0, "levels": 4, "fill_anchored": 1,
                  "rgrid_reset_threshold_pct": 1.5},
        Decimal(100), product="BTC-USDC",
    )
    assert rgrid["reset_threshold_pct"] == Decimal("1.5") / Decimal(100)
    # Unset → per-mode default. grid keeps 0.25%. rgrid's threshold ARMS its
    # trailing soft reset and the controller refuses one that sits inside the entry
    # band, so its default is 0.2% but never below 2 x spread — the old flat 0.125%
    # was under 2 x the shipped 10bp spread and silently disabled the soft reset
    # for anyone who never touched the button.
    grid_def = er.map_strategy_config(
        "grid", {"notional_usd": 100.0, "levels": 4, "fill_anchored": 1},
        Decimal(100), product="BTC-USDC",
    )
    assert grid_def["reset_threshold_pct"] == Decimal("0.25") / Decimal(100)
    rgrid_def = er.map_strategy_config(
        "rgrid", {"notional_usd": 100.0, "levels": 4, "rgrid_spread_bp": 10.0},
        Decimal(100), product="BTC-USDC",
    )
    assert rgrid_def["reset_threshold_pct"] == Decimal("0.2") / Decimal(100)
    assert rgrid_def["reset_threshold_pct"] >= rgrid_def["spread_ask_pct"] * 2
    # A wide spread lifts the default with it, so the trail stays armable.
    rgrid_wide = er.map_strategy_config(
        "rgrid", {"notional_usd": 100.0, "levels": 4, "rgrid_spread_bp": 30.0},
        Decimal(100), product="BTC-USDC",
    )
    assert rgrid_wide["reset_threshold_pct"] == Decimal("0.6") / Decimal(100)
    assert rgrid_wide["reset_threshold_pct"] >= rgrid_wide["spread_ask_pct"] * 2


def test_dgrid_continuous_quoting_wiring():
    """D-Grid must (a) recycle completed levels so it keeps working the band and
    (b) leave the re-center threshold at 0 when the user hasn't pinned one, so
    the controller picks its band-width auto-follow default (50bp was too coarse
    and the grid 'placed a few orders and stopped'). An explicit value passes
    through."""
    dg = er.map_strategy_config(
        "dgrid", {"notional_usd": 100.0, "levels": 3, "dgrid_spread_bp": 8.0},
        Decimal(58000), product="BTC-USDC",
    )
    assert dg["recycle_levels"] is True
    assert dg["dgrid_reset_threshold_bp"] == 0.0  # -> controller auto-follow default
    # Explicit user reset (grid_reset_threshold_pct=0.8% -> 80bp) is honored.
    dg2 = er.map_strategy_config(
        "dgrid", {"notional_usd": 100.0, "levels": 3, "grid_reset_threshold_pct": 0.8},
        Decimal(58000), product="BTC-USDC",
    )
    assert dg2["dgrid_reset_threshold_bp"] == 80.0


def test_grid_defaults_to_a_recycling_ladder_rgrid_never_does():
    """grid DEFAULTS to the multi-level recycling ladder and opts into
    fill-anchored quoting; rgrid IS the exposure-anchored engine, always."""
    g_def = er.map_strategy_config(
        "grid", {"notional_usd": 100.0, "levels": 3}, Decimal(58000), product="BTC-USDC",
    )
    assert g_def.get("recycle_levels") is True and "controller_override" not in g_def
    # Classic long ladder, NOT the dynamic engine: no dgrid regime knobs.
    assert "dgrid_trend_on_vr" not in g_def

    g_fa = er.map_strategy_config(
        "grid", {"notional_usd": 100.0, "levels": 3, "fill_anchored": 1},
        Decimal(58000), product="BTC-USDC",
    )
    assert g_fa["controller_override"] == "fill_anchored" and g_fa["momentum"] is False

    r_def = er.map_strategy_config(
        "rgrid", {"notional_usd": 100.0, "levels": 3}, Decimal(58000), product="BTC-USDC",
    )
    assert r_def["controller_override"] == "fill_anchored" and r_def["passive_only"] is True
    assert "recycle_levels" not in r_def, "no resting ladder to recycle"


def test_only_dgrid_gets_the_phase_switcher_knobs():
    """D-Grid is the phase switcher. R-Grid must carry NONE of its knobs — it has
    no phases to switch between, and sharing the engine is what made R-Grid
    sessions announce "switched RGRID → GRID … now quoting the LONG ladder"."""
    rg = er.map_strategy_config("rgrid", {"notional_usd": 100.0}, Decimal(58000), product="BTC-USDC")
    dg = er.map_strategy_config("dgrid", {"notional_usd": 100.0}, Decimal(58000), product="BTC-USDC")
    for knob in ("dgrid_trend_on_vr", "dgrid_trend_drift_pct", "dgrid_flip_confirm_ticks",
                 "dgrid_short_window", "dgrid_reversal_flip_pct", "dgrid_range_on_vr"):
        assert knob in dg, knob
        assert knob not in rg, knob
    # A dgrid_* setting left over on an R-Grid config cannot resurrect the switcher.
    rg_ov = er.map_strategy_config(
        "rgrid", {"notional_usd": 100.0, "dgrid_trend_on_variance_ratio": 1.40},
        Decimal(58000), product="BTC-USDC",
    )
    assert "dgrid_trend_on_vr" not in rg_ov


def test_map_vol_config_is_spot():
    cfg = er.map_strategy_config(
        "vol", {"notional_usd": 40.0, "interval_seconds": 60}, Decimal(100), product="KBTC-USDC",
    )
    assert cfg["market"] == "spot" and cfg["leverage"] == 1
    # $40 is below the product's $100-$500 band and is clamped UP to the floor
    # (docs/volume_bot_taker_v4.md) — the fee estimate the user agreed to was
    # quoted against a banded margin, so the run must use one.
    assert cfg["total_amount_quote"] == Decimal("100")
    assert cfg["total_duration"] == 240.0 and cfg["order_interval"] == 60.0


def test_map_vol_config_enforces_the_margin_band():
    """Margin band $100-$500. The UI enforces it on entry; the mapping
    re-clamps so a stale or hand-edited config cannot start an out-of-band run."""
    lo = er.map_strategy_config(
        "vol", {"session_margin_usd": 25.0}, Decimal(100), product="KBTC-USDC",
    )
    hi = er.map_strategy_config(
        "vol", {"session_margin_usd": 5000.0}, Decimal(100), product="KBTC-USDC",
    )
    ok = er.map_strategy_config(
        "vol", {"session_margin_usd": 250.0}, Decimal(100), product="KBTC-USDC",
    )
    assert lo["total_amount_quote"] == Decimal("100")
    assert hi["total_amount_quote"] == Decimal("500")
    assert ok["total_amount_quote"] == Decimal("250")


def test_map_vol_config_defaults_to_maker_twap():
    """v4.2: maker TWAP is the shipped default. Measured on KBTC spot, resting
    costs 3.6bp per round trip vs 14.3bp crossing — ~4x the volume per unit of
    the session loss budget. The legacy vol_taker_mode kill-switch must NOT be
    defaulted, or it would pin every run to taker and this default could never
    take effect."""
    cfg = er.map_strategy_config(
        "vol", {"session_margin_usd": 100.0}, Decimal(100), product="KBTC-USDC",
    )
    assert cfg["vol_execution_algo"] == "twap"
    assert "vol_taker_mode" not in cfg, "the kill-switch must not be defaulted on"
    assert cfg["vol_twap_horizon_seconds"] == 120.0
    assert cfg["vol_max_taker_frac"] == 0.25
    # The breakeven gate must never see a 0 fee rate.
    assert Decimal(str(cfg["spot_taker_fee_rate"])) > 0

    for algo in ("chase", "taker"):
        c = er.map_strategy_config(
            "vol", {"session_margin_usd": 100.0, "vol_execution_algo": algo},
            Decimal(100), product="KBTC-USDC",
        )
        assert c["vol_execution_algo"] == algo

    # Explicit legacy kill-switch still forwards.
    legacy = er.map_strategy_config(
        "vol", {"session_margin_usd": 100.0, "vol_taker_mode": 1},
        Decimal(100), product="KBTC-USDC",
    )
    assert legacy["vol_taker_mode"] == 1.0


def test_map_vol_config_normalizes_dashed_and_bare_product():
    """``state.product`` may be stored as a bare base (current UI flow) or
    as a dashed pair (legacy + tests). Both must produce a canonical
    ``trading_pair`` the VolumeBotController accepts. Regression guard for
    the production crash where ``USDC`` / ``KBTC`` collided with the
    controller's hardcoded ``{"KBTC-USDC", "WETH-USDC"}`` set.
    """
    bare = er.map_strategy_config(
        "vol", {"notional_usd": 50.0, "interval_seconds": 30}, Decimal(100), product="KBTC",
    )
    assert bare["trading_pair"] == "KBTC"

    dashed = er.map_strategy_config(
        "vol", {"notional_usd": 50.0, "interval_seconds": 30}, Decimal(100), product="KBTC-USDC0",
    )
    # Suffix stripped via normalize_volume_spot_symbol.
    assert dashed["trading_pair"] == "KBTC"

    testnet_listing = er.map_strategy_config(
        "vol", {"notional_usd": 50.0, "interval_seconds": 30}, Decimal(100), product="QQQX",
    )
    assert testnet_listing["trading_pair"] == "QQQX"


def test_map_risk_limits():
    lim = er.map_risk_limits({"notional_usd": 100.0, "levels": 3, "session_notional_cap_usd": 0.0})
    assert lim.max_open_executors == 5            # levels + 2
    assert lim.max_single_order_quote == Decimal("100")
    assert lim.max_position_size_quote == Decimal("300")  # notional * levels fallback


def test_deployed_notional_is_margin_times_leverage():
    """SIZING (2026-06-21): grid/MM deploy margin x effective leverage. With no
    leverage it stays margin-sized (back-compat); with leverage it scales."""
    # Classic ladder (fill_anchored=0) exposes deployed as total_amount_quote.
    base = er.map_strategy_config(
        "grid", {"notional_usd": 100.0, "levels": 2, "fill_anchored": 0}, Decimal(100), product="BTC-PERP",
    )
    assert base["total_amount_quote"] == Decimal("100")   # eff_lev defaults to 1
    lev5 = er.map_strategy_config(
        "grid", {"notional_usd": 100.0, "levels": 2, "fill_anchored": 0}, Decimal(100), product="BTC-PERP", leverage=5,
    )
    assert lev5["total_amount_quote"] == Decimal("500")   # 100 x 5
    assert lev5["leverage"] == 5
    # An explicit mm_leverage_override (Tiny preset / Lev button) wins over the
    # session leverage.
    over = er.map_strategy_config(
        "grid", {"notional_usd": 100.0, "mm_leverage_override": 3, "levels": 2, "fill_anchored": 0},
        Decimal(100), product="BTC-PERP", leverage=10,
    )
    assert over["total_amount_quote"] == Decimal("300")   # override 3x beats 10x


def test_map_risk_limits_scale_with_leverage():
    """The per-order and position caps must follow the DEPLOYED notional or the
    Risk Engine rejects every leveraged order (the documented 'LIVE but 0 orders'
    failure)."""
    lim = er.map_risk_limits(
        {"notional_usd": 100.0, "levels": 3, "session_notional_cap_usd": 0.0}, "grid", leverage=5,
    )
    assert lim.max_single_order_quote == Decimal("500")          # deployed = 100 x 5
    assert lim.max_position_size_quote == Decimal("1500")        # deployed * levels


def test_map_risk_limits_dn_follows_leg_size_not_notional():
    """Regression: DN sizes legs from fixed_margin_usd, so its risk caps must
    follow that — not notional_usd. The old behavior capped single orders at
    notional_usd ($50) and rejected every $250 leg (LIVE but 0 orders)."""
    lim = er.map_risk_limits(
        {"fixed_margin_usd": 250.0, "notional_usd": 50.0, "dn_hedge_ratio": 1.0}, "dn"
    )
    # cap = leg(250) * hedge(1) * 2.0 headroom = 500
    assert lim.max_single_order_quote == Decimal("500")
    assert lim.max_position_size_quote == Decimal("500")
    assert lim.max_open_executors == 6
    # A 2x hedge ratio scales the cap with the larger short leg.
    lim2 = er.map_risk_limits({"fixed_margin_usd": 100.0, "dn_hedge_ratio": 2.0}, "dn")
    assert lim2.max_single_order_quote == Decimal("400")  # 100 * 2 * 2


def test_build_product_meta_from_catalog(monkeypatch):
    """Real catalog rows carry x18-scaled increments + isolated_only; the builder
    must convert them (not fall back to permissive 0.01/0.001/1 defaults) and
    flag perps vs spot. Regression for the bug where get_all_products_info()
    returned {"perp","spot"} (not a "products" list) so the builder yielded {}.
    """
    from src.nadobro.venue import product_catalog as pc

    # x18-scaled: 0.5 tick, 0.001 lot, 5 min-notional.
    X18 = 10 ** 18
    perps = {
        "perps": {
            "QQQ": {
                "id": 7, "symbol": "QQQ-PERP", "isolated_only": True,
                "price_increment_x18": str(5 * X18 // 10),     # 0.5
                "size_increment_x18": str(X18 // 1000),        # 0.001
                "min_size_x18": str(5 * X18),                  # 5
            },
        }
    }
    spots = {
        "spots": {
            "QQQ-USDC0": {
                "id": 8, "symbol": "QQQ-USDC0",
                "price_increment_x18": str(X18 // 100),        # 0.01
                "size_increment_x18": str(X18 // 1000),        # 0.001
                "min_size_x18": str(X18),                      # 1
            },
        }
    }
    monkeypatch.setattr(pc, "get_catalog", lambda **kw: perps)
    monkeypatch.setattr(pc, "get_spot_catalog", lambda **kw: spots)

    class _Client:
        network = "testnet"

    meta = er.build_product_meta_from_catalog(_Client())

    # Perp resolves under base, canonical symbol, and BASE-PERP alias.
    for key in ("QQQ", "QQQ-PERP"):
        assert key in meta
    perp = meta["QQQ-PERP"]
    assert perp.product_id == 7
    assert perp.tick_size == Decimal("0.5")        # real increment, not 0.01 default
    assert perp.lot_size == Decimal("0.001")
    assert perp.min_notional == Decimal("5")
    assert perp.is_perp is True and perp.isolated_only is True

    spot = meta["QQQ-USDC0"]
    assert spot.product_id == 8 and spot.is_perp is False and spot.isolated_only is False


def test_dual_listed_base_keeps_spot_and_perp_addressable(monkeypatch):
    """Desk routing regression: a base with BOTH a perp and a spot listing
    (e.g. ETH) must keep the bare base on the perp (grid/MM expect that) AND
    expose a -SPOT alias on the spot product, or a spot 'buy 2 ETH' silently
    opens a perp."""
    from src.nadobro.venue import product_catalog as pc

    X18 = 10 ** 18
    perps = {"perps": {"ETH": {
        "id": 100, "symbol": "ETH-PERP",
        "price_increment_x18": str(X18 // 100), "size_increment_x18": str(X18 // 1000),
        "min_size_x18": str(X18),
    }}}
    spots = {"spots": {"ETH": {
        "id": 200, "symbol": "ETH",
        "price_increment_x18": str(X18 // 100), "size_increment_x18": str(X18 // 1000),
        "min_size_x18": str(X18),
    }}}
    monkeypatch.setattr(pc, "get_catalog", lambda **kw: perps)
    monkeypatch.setattr(pc, "get_spot_catalog", lambda **kw: spots)

    class _Client:
        network = "testnet"

    meta = er.build_product_meta_from_catalog(_Client())

    # Bare base + -PERP -> the perp (unchanged behaviour for grid/MM).
    assert meta["ETH"].product_id == 100 and meta["ETH"].is_perp is True
    assert meta["ETH-PERP"].product_id == 100 and meta["ETH-PERP"].is_perp is True
    # -SPOT alias -> the SPOT product (the Desk routing fix).
    assert meta["ETH-SPOT"].product_id == 200 and meta["ETH-SPOT"].is_perp is False


def test_build_product_meta_handles_bad_catalog(monkeypatch):
    from src.nadobro.venue import product_catalog as pc

    def _boom(**kw):
        raise RuntimeError("down")

    monkeypatch.setattr(pc, "get_catalog", _boom)
    monkeypatch.setattr(pc, "get_spot_catalog", _boom)

    class _Client:
        network = "testnet"

    assert er.build_product_meta_from_catalog(_Client()) == {}


def test_engine_v2_enabled_gate(monkeypatch):
    # BUG-SR-1 / BR-1 fix: default is now ON. The legacy dispatch path was
    # retired, so unset must mean enabled to avoid silently no-opping.
    monkeypatch.delenv("NADO_ENGINE_V2_RUNTIME", raising=False)
    assert er.engine_v2_enabled() is True
    monkeypatch.setenv("NADO_ENGINE_V2_RUNTIME", "true")
    assert er.engine_v2_enabled() is True
    monkeypatch.setenv("NADO_ENGINE_V2_RUNTIME", "0")
    assert er.engine_v2_enabled() is False
    monkeypatch.setenv("NADO_ENGINE_V2_RUNTIME", "false")
    assert er.engine_v2_enabled() is False


def test_spread_is_per_strategy_user_set():
    """The quoting step must come from the strategy's own user-set spread, not a
    single hardcoded spread_bp for everyone."""
    from decimal import Decimal
    from src.nadobro.strategy.engine_runtime import map_strategy_config

    mid = Decimal("100")

    # rgrid honors rgrid_spread_bp (20bp), not the generic spread_bp (5). The
    # spread IS the trigger offset from the exposure average on each leg.
    rg = map_strategy_config(
        "rgrid", {"notional_usd": 100.0, "spread_bp": 5.0, "rgrid_spread_bp": 20.0, "levels": 2},
        mid, product="BTC-PERP",
    )
    assert rg["spread_ask_pct"] == Decimal("20.0") / Decimal(10000)
    assert rg["spread_bid_pct"] == Decimal("20.0") / Decimal(10000)

    # dgrid honors dgrid_spread_bp (15bp), not the default 8.
    dg = map_strategy_config(
        "dgrid", {"notional_usd": 100.0, "dgrid_spread_bp": 15.0, "levels": 4},
        mid, product="BTC-PERP",
    )
    assert dg["step_pct"] == Decimal("15.0") / Decimal(10000)

    # grid (classic ladder, fill_anchored=0) still uses spread_bp as the step.
    g = map_strategy_config(
        "grid", {"notional_usd": 100.0, "spread_bp": 3.0, "levels": 2, "fill_anchored": 0},
        mid, product="BTC-PERP",
    )
    assert g["min_spread_between_orders"] == Decimal("3.0") / Decimal(10000)


def test_min_max_spread_bp_drive_auto_spread_bounds():
    """min_spread_bp / max_spread_bp (and dgrid_*) now set the per-side spread
    floor/cap (bps → fraction). Unset preserves the legacy 1.5bp / 50bp band."""
    from decimal import Decimal
    from src.nadobro.strategy.engine_runtime import map_strategy_config

    mid = Decimal("100")
    g = map_strategy_config(
        "grid", {"notional_usd": 100.0, "levels": 2, "min_spread_bp": 3.0, "max_spread_bp": 40.0},
        mid, product="BTC-PERP",
    )
    assert g["spread_floor_half_pct"] == Decimal("3.0") / Decimal(10000)
    assert g["spread_cap_half_pct"] == Decimal("40.0") / Decimal(10000)

    g0 = map_strategy_config("grid", {"notional_usd": 100.0, "levels": 2}, mid, product="BTC-PERP")
    assert g0["spread_floor_half_pct"] == Decimal("1.5") / Decimal(10000)
    assert g0["spread_cap_half_pct"] == Decimal("50.0") / Decimal(10000)

    m = map_strategy_config(
        "mid", {"notional_usd": 100.0, "levels": 2, "min_spread_bp": 4.0, "max_spread_bp": 20.0},
        mid, product="BTC-PERP",
    )
    assert m["spread_floor_half_pct"] == Decimal("4.0") / Decimal(10000)
    assert m["spread_cap_half_pct"] == Decimal("20.0") / Decimal(10000)

    dg = map_strategy_config(
        "dgrid", {"notional_usd": 100.0, "levels": 4, "dgrid_min_spread_bp": 5.0,
                  "dgrid_max_spread_bp": 30.0},
        mid, product="BTC-PERP",
    )
    assert dg["spread_floor_half_pct"] == Decimal("5.0") / Decimal(10000)
    assert dg["spread_cap_half_pct"] == Decimal("30.0") / Decimal(10000)

    # min > max can't invert the band — cap is held at the floor.
    inv = map_strategy_config(
        "grid", {"notional_usd": 100.0, "levels": 2, "min_spread_bp": 30.0, "max_spread_bp": 10.0},
        mid, product="BTC-PERP",
    )
    assert inv["spread_cap_half_pct"] == inv["spread_floor_half_pct"] == Decimal("30.0") / Decimal(10000)


def test_mid_directional_bias_scales_net_exposure_cap_and_clamps():
    """Mid directional bias is allowed up to +20% net-exposure headroom at |bias|=1
    (the documented '20% additional margin'), scaling linearly, and bias is
    clamped to [-1, 1]."""
    from decimal import Decimal
    from src.nadobro.strategy.engine_runtime import map_strategy_config

    mid = Decimal("100")
    neutral = map_strategy_config("mid", {"notional_usd": 100.0, "levels": 1}, mid, product="BTC-PERP")
    assert neutral["max_net_exposure_pct"] == 30.0          # default, no bias
    assert neutral["directional_bias"] == 0.0

    long_full = map_strategy_config(
        "mid", {"notional_usd": 100.0, "levels": 1, "directional_bias": 1.0}, mid, product="BTC-PERP",
    )
    assert abs(long_full["max_net_exposure_pct"] - 36.0) < 1e-9   # +20%
    assert long_full["directional_bias"] == 1.0

    half = map_strategy_config(
        "mid", {"notional_usd": 100.0, "levels": 1, "directional_bias": -0.5}, mid, product="BTC-PERP",
    )
    assert abs(half["max_net_exposure_pct"] - 33.0) < 1e-9        # +10% at |0.5|
    assert half["directional_bias"] == -0.5

    clamped = map_strategy_config(
        "mid", {"notional_usd": 100.0, "levels": 1, "directional_bias": 5.0}, mid, product="BTC-PERP",
    )
    assert clamped["directional_bias"] == 1.0

    # BIAS-TEXT-COERCION (2026-07-30): legacy TEXT bias values must resolve to
    # the same lean everywhere, not silently coerce to neutral via _f().
    for text, expected in (("long_bias", 1.0), ("short_bias", -1.0), ("neutral", 0.0)):
        mapped = map_strategy_config(
            "mid", {"notional_usd": 100.0, "levels": 1, "directional_bias": text},
            mid, product="BTC-PERP",
        )
        assert mapped["directional_bias"] == expected, text
    assert abs(clamped["max_net_exposure_pct"] - 36.0) < 1e-9


def test_sl_tp_is_per_strategy_user_set():
    """rgrid/dgrid SL/TP come from rgrid_stop_loss_pct / rgrid_take_profit_pct
    (the fields the UI writes), not the generic sl_pct/tp_pct default."""
    from decimal import Decimal
    from src.nadobro.strategy.engine_runtime import map_strategy_config
    from src.nadobro.strategy.strategy_registry import effective_sl_tp_pct

    # resolver
    assert effective_sl_tp_pct("dgrid", {"rgrid_stop_loss_pct": 10.0, "rgrid_take_profit_pct": 50.0,
                                         "sl_pct": 0.8, "tp_pct": 1.2}) == (10.0, 50.0)
    assert effective_sl_tp_pct("rgrid", {"sl_pct": 0.8, "tp_pct": 1.2}) == (0.8, 1.2)  # fallback
    assert effective_sl_tp_pct("grid", {"sl_pct": 3.0, "tp_pct": 9.0,
                                        "rgrid_stop_loss_pct": 99.0}) == (3.0, 9.0)  # grid ignores rgrid_*

    # The user's dgrid SL/TP (10% / 50%) reach the controller as PERCENTS of
    # margin — the unit the tiered scale-out and the session rail both use. They
    # are NOT also copied onto a per-level price barrier (units invariant).
    cfg = map_strategy_config(
        "dgrid",
        {"notional_usd": 100.0, "levels": 4, "rgrid_stop_loss_pct": 10.0,
         "rgrid_take_profit_pct": 50.0, "sl_pct": 0.8, "tp_pct": 1.2},
        Decimal("100"), product="BTC-PERP",
    )
    assert float(cfg["sl_pct"]) == 10.0
    assert float(cfg["tp_pct"]) == 50.0
    assert "triple_barrier_config" not in cfg


def test_should_build_controller_truth_table():
    """The build gate must let the cycle-running worker ADOPT a controller it
    lacks locally (even against a stale remote row), while the non-worker
    fallback defers to a live owner so it never double-builds."""
    from src.nadobro.strategy.engine_runtime import _should_build_controller as B

    # Local FAILED controller -> always rebuild.
    assert B(needs_recovery=True, has_local_active=True, worker_mode=True, is_running=True) is True
    # Live local controller -> just tick.
    assert B(needs_recovery=False, has_local_active=True, worker_mode=False, is_running=True) is False
    # Worker with NO local controller -> adopt, even if remote says running
    # (crashed/recycled worker, or stale row). THIS is the no-orders fix.
    assert B(needs_recovery=False, has_local_active=False, worker_mode=True, is_running=True) is True
    # Non-worker (main fallback) with no local and a live remote owner -> defer
    # (no double build).
    assert B(needs_recovery=False, has_local_active=False, worker_mode=False, is_running=True) is False
    # Non-worker, no local, nobody running -> build (single-process happy path).
    assert B(needs_recovery=False, has_local_active=False, worker_mode=False, is_running=False) is True


def test_live_config_signature_ignores_mid_anchor_but_detects_leverage():
    """A price-only remap must not churn the live controller, but a leverage /
    deployed-notional edit must be visible before the next tick."""
    cfg_100 = er.map_strategy_config(
        "grid",
        {"notional_usd": 100.0, "levels": 2, "mm_leverage_override": 5},
        Decimal("100"),
        product="BTC-PERP",
        leverage=5,
    )
    cfg_110 = er.map_strategy_config(
        "grid",
        {"notional_usd": 100.0, "levels": 2, "mm_leverage_override": 5},
        Decimal("110"),
        product="BTC-PERP",
        leverage=5,
    )
    limits_5x = er.map_risk_limits(
        {"notional_usd": 100.0, "levels": 2, "mm_leverage_override": 5},
        "grid",
        leverage=5,
    )
    assert er._live_config_signature(cfg_100, limits_5x) == er._live_config_signature(cfg_110, limits_5x)

    cfg_1x = er.map_strategy_config(
        "grid",
        {"notional_usd": 100.0, "levels": 2, "mm_leverage_override": 1},
        Decimal("110"),
        product="BTC-PERP",
        leverage=1,
    )
    limits_1x = er.map_risk_limits(
        {"notional_usd": 100.0, "levels": 2, "mm_leverage_override": 1},
        "grid",
        leverage=1,
    )
    assert er._live_config_signature(cfg_100, limits_5x) != er._live_config_signature(cfg_1x, limits_1x)


def test_apply_live_mid_config_updates_order_size_and_risk_limits():
    import asyncio
    from types import SimpleNamespace

    from src.nadobro.engine.controllers.market_making import MarketMakingController

    old_settings = {"notional_usd": 100.0, "levels": 2, "mm_leverage_override": 5}
    new_settings = {"notional_usd": 100.0, "levels": 2, "mm_leverage_override": 1}
    old_cfg = er.map_strategy_config("mid", old_settings, Decimal("100"), product="BTC-PERP", leverage=5)
    new_cfg = er.map_strategy_config("mid", new_settings, Decimal("100"), product="BTC-PERP", leverage=1)
    old_limits = er.map_risk_limits(old_settings, "mid", leverage=5)
    new_limits = er.map_risk_limits(new_settings, "mid", leverage=1)

    class _Orch:
        def __init__(self):
            # Production ExecutorOrchestrator exposes RiskEngine as ``risk``.
            self.risk = SimpleNamespace(limits=old_limits)
            self.stopped = []

        async def stop(self, ex_id):
            self.stopped.append(ex_id)

    orch = _Orch()
    controller = MarketMakingController(
        user_id=7,
        configs=old_cfg,
        orchestrator=orch,
        adapter=object(),
        inventory=None,
        limits=old_limits,
        controller_id="mid:7:mainnet",
    )
    controller._bid_id = "bid-order"
    controller._ask_id = "ask-order"
    controller._bid_price = Decimal("99")
    controller._ask_price = Decimal("101")

    asyncio.run(
        er._apply_live_controller_update(
            "mid", controller, orch, new_cfg, new_limits, Decimal("100")
        )
    )

    assert controller.order_amount_quote == Decimal("100")   # full deployed (one bid+ask)
    assert controller.configs["leverage"] == 1
    assert controller.limits.max_single_order_quote == Decimal("100.0")
    assert orch.risk.limits.max_single_order_quote == Decimal("100.0")
    assert orch.stopped == ["bid-order", "ask-order"]
    assert controller._bid_id is None and controller._ask_id is None


def test_apply_live_fill_anchored_grid_refreshes_mm_quotes_and_risk_limits():
    import asyncio
    from types import SimpleNamespace

    from src.nadobro.engine.controllers.fill_anchored import FillAnchoredQuotingController

    old_settings = {
        "notional_usd": 100.0,
        "levels": 2,
        "fill_anchored": 1,
        "spread_bp": 5.0,
        "mm_leverage_override": 5,
    }
    new_settings = {
        "notional_usd": 100.0,
        "levels": 2,
        "fill_anchored": 1,
        "spread_bp": 20.0,
        "mm_leverage_override": 1,
        "reset_threshold_pct": 0.5,
    }
    old_cfg = er.map_strategy_config("grid", old_settings, Decimal("100"), product="BTC-PERP", leverage=5)
    new_cfg = er.map_strategy_config("grid", new_settings, Decimal("100"), product="BTC-PERP", leverage=1)
    old_limits = er.map_risk_limits(old_settings, "grid", leverage=5)
    new_limits = er.map_risk_limits(new_settings, "grid", leverage=1)

    class _Orch:
        def __init__(self):
            self.risk = SimpleNamespace(limits=old_limits)
            self.stopped = []

        async def stop(self, ex_id):
            self.stopped.append(ex_id)

    orch = _Orch()
    controller = FillAnchoredQuotingController(
        user_id=7,
        configs=old_cfg,
        orchestrator=orch,
        adapter=object(),
        inventory=None,
        limits=old_limits,
        controller_id="grid:7:mainnet",
    )
    controller._bid_id = "bid-order"
    controller._ask_id = "ask-order"
    controller._bid_price = Decimal("99")
    controller._ask_price = Decimal("101")

    asyncio.run(
        er._apply_live_controller_update(
            "grid", controller, orch, new_cfg, new_limits, Decimal("100")
        )
    )

    # LADDER (2026-08-02): order_amount_quote is now the notional deployed per
    # SIDE, subdivided across ``levels`` levels — it used to be the size of the
    # single order fill-anchored rested. With deployed=$100 and levels=2 the
    # per-level size is still $50; what changed is that BOTH levels now rest,
    # instead of one order sitting alone while the rest of the budget idled.
    assert controller.order_amount_quote == Decimal("100")
    assert controller.ladder_levels == 2
    assert controller.order_amount_quote / controller.ladder_levels == Decimal("50")
    assert controller.spread_bid_pct == Decimal("0.002")
    assert controller.reset_threshold_pct == Decimal("0.005")
    assert orch.risk.limits.max_single_order_quote == Decimal("100.0")
    assert orch.stopped == ["bid-order", "ask-order"]
    assert controller._bid_id is None and controller._ask_id is None


def test_apply_live_grid_config_requotes_without_controller_stop():
    import asyncio
    from types import SimpleNamespace

    from src.nadobro.engine.controllers.grid_trading import build_grid_config
    from src.nadobro.engine.types import TradeType

    old_settings = {"notional_usd": 100.0, "levels": 2, "mm_leverage_override": 5, "fill_anchored": 0}
    new_settings = {"notional_usd": 100.0, "levels": 2, "mm_leverage_override": 1, "fill_anchored": 0}
    old_cfg = er.map_strategy_config("grid", old_settings, Decimal("100"), product="BTC-PERP", leverage=5)
    new_cfg = er.map_strategy_config("grid", new_settings, Decimal("100"), product="BTC-PERP", leverage=1)
    old_limits = er.map_risk_limits(old_settings, "grid", leverage=5)
    new_limits = er.map_risk_limits(new_settings, "grid", leverage=1)

    class _Executor:
        id = "grid-ex"
        open_side = TradeType.BUY

        def __init__(self):
            self.config = build_grid_config(old_cfg, TradeType.BUY)
            self.recentered = []

        async def recenter(self, start_price, end_price):
            self.recentered.append((start_price, end_price))

    class _Orch:
        def __init__(self, executor):
            self.risk = SimpleNamespace(limits=old_limits)
            self.executor = executor
            self.stopped = []

        def list(self, controller_id, active_only=True):
            return [self.executor]

        async def stop(self, ex_id):
            self.stopped.append(ex_id)

    executor = _Executor()
    orch = _Orch(executor)
    controller = SimpleNamespace(
        id="grid:7:mainnet",
        configs=old_cfg,
        limits=old_limits,
        trading_pair="BTC-PERP",
    )

    asyncio.run(
        er._apply_live_controller_update(
            "grid", controller, orch, new_cfg, new_limits, Decimal("100")
        )
    )

    assert executor.config.total_amount_quote == Decimal("100")
    assert executor.config.leverage == 1
    assert executor.recentered
    assert orch.stopped == []
    assert orch.risk.limits.max_single_order_quote == Decimal("100.0")


# --- Turbo Volume additions (2026-07) ----------------------------------------

def test_map_mid_quote_mode_defaults_to_mid_and_maps_touch():
    base = {"notional_usd": 100.0, "spread_bp": 5.0, "levels": 2,
            "inventory_soft_limit_usd": 60.0}
    cfg = er.map_strategy_config("mid", dict(base), Decimal(100), product="BTC-USDC")
    assert cfg["quote_mode"] == "mid"
    cfg_t = er.map_strategy_config(
        "mid", {**base, "mm_quote_mode": "touch"}, Decimal(100), product="BTC-USDC",
    )
    assert cfg_t["quote_mode"] == "touch"


def test_map_mid_inventory_cap_zero_means_auto_deployed():
    """inventory_soft_limit_usd <= 0 -> cap = deployed (margin x leverage), so
    the cap always fits one full-size fill; an explicit positive value (the
    registry default 60) is honored unchanged."""
    cfg = er.map_strategy_config(
        "mid", {"notional_usd": 100.0, "spread_bp": 5.0,
                "inventory_soft_limit_usd": 0.0, "mm_leverage_override": 10},
        Decimal(100), product="BTC-USDC",
    )
    assert cfg["order_amount_quote"] == Decimal("1000")   # 100 x 10
    assert cfg["max_base_quote"] == Decimal("1000")       # auto = deployed
    cfg_explicit = er.map_strategy_config(
        "mid", {"notional_usd": 100.0, "spread_bp": 5.0,
                "inventory_soft_limit_usd": 60.0},
        Decimal(100), product="BTC-USDC",
    )
    assert cfg_explicit["max_base_quote"] == Decimal("60")


def test_apply_mid_controller_config_syncs_quote_mode():
    """AUDIT-MM-2026-07-14 #5: Turbo tapped on a RUNNING mid must live-apply
    quote_mode, not just sizing/spread — the old sync delivered a
    half-applied preset until a full stop/start."""
    from src.nadobro.engine.controllers.market_making import MarketMakingController
    from src.nadobro.engine.orchestrator import ExecutorOrchestrator
    from src.nadobro.engine.inventory import InventoryRepository

    controller = MarketMakingController(
        user_id=1, orchestrator=ExecutorOrchestrator(), adapter=None,
        inventory=InventoryRepository(),
        configs={"trading_pair": "P", "spread_bid_pct": "0.001",
                 "spread_ask_pct": "0.001", "order_amount_quote": "10"},
    )
    assert controller.quote_mode == "mid"

    new_cfg = er.map_strategy_config(
        "mid", {"notional_usd": 100.0, "spread_bp": 2.0,
                "mm_quote_mode": "touch", "inventory_soft_limit_usd": 0.0},
        Decimal(100), product="P",
    )
    er._apply_mid_controller_config(controller, new_cfg)
    assert controller.quote_mode == "touch"
    # POLICY 2026-07-15: maker-only — no cross knobs exist on the controller.
    assert not hasattr(controller, "cross_close_after_seconds")


def test_vol_session_loss_limit_is_mapped_from_sl_pct():
    """User directive 2026-07-31: the session stop is 5% of margin — cumulative
    realized PnL net of fees reaching -$5 on a $100 margin stops the bot. It is
    mapped to an absolute USD limit the CONTROLLER enforces, because the
    live-session rail is structurally blind to spot inventory."""
    cfg = er.map_strategy_config(
        "vol", {"session_margin_usd": 100.0, "sl_pct": 5.0},
        Decimal(100), product="KBTC-USDC",
    )
    assert Decimal(str(cfg["vol_session_loss_limit_usd"])) == Decimal("5")
    assert cfg["vol_taker_slippage_bp"] == 15.0

    # Scales with margin, and honors a user override.
    big = er.map_strategy_config(
        "vol", {"session_margin_usd": 500.0, "sl_pct": 5.0},
        Decimal(100), product="KBTC-USDC",
    )
    assert Decimal(str(big["vol_session_loss_limit_usd"])) == Decimal("25")
    tight = er.map_strategy_config(
        "vol", {"session_margin_usd": 100.0, "sl_pct": 2.0},
        Decimal(100), product="KBTC-USDC",
    )
    assert Decimal(str(tight["vol_session_loss_limit_usd"])) == Decimal("2")


def test_vol_registry_default_sl_is_5_pct():
    from src.nadobro.strategy.strategy_registry import settings_strategy_defaults

    assert float(settings_strategy_defaults()["vol"]["sl_pct"]) == 5.0

# ==========================================================================
# Ladder mapping (Phase 2, 2026-08-02)
# ==========================================================================
def test_mid_resurrects_the_levels_input_without_changing_deployment():
    """``levels`` used to be read and thrown away for Mid ("Mid is a single bid
    + single ask"). It now shapes the book — but the per-side TOTAL is
    unchanged, which is what makes turning it on risk-neutral."""
    settings = {"notional_usd": 1000.0, "levels": 4}
    cfg = er.map_strategy_config("mid", settings, Decimal("1000"), product="BTC-PERP", leverage=1)
    assert cfg["ladder_levels"] == 4
    assert cfg["order_amount_quote"] == Decimal("1000")     # side total, unchanged
    one = er.map_strategy_config(
        "mid", {"notional_usd": 1000.0, "levels": 1}, Decimal("1000"),
        product="BTC-PERP", leverage=1,
    )
    assert one["order_amount_quote"] == cfg["order_amount_quote"]
    assert one["ladder_levels"] == 1


def test_rgrid_rests_one_quote_per_side_not_a_ladder():
    """R-Grid quotes exactly two prices (anchor +- spread), so handing it the
    multi-level ladder would multiply its deployed size by ``levels`` while it only
    ever rests one quote per side."""
    rg = er.map_strategy_config(
        "rgrid", {"notional_usd": 100.0, "levels": 4}, Decimal(58000), product="BTC-USDC",
    )
    assert rg["ladder_levels"] == 1
    assert "ladder_step_bp" not in rg and "ladder_curve" not in rg
    assert rg["passive_only"] is True


def test_min_quote_lifetime_tracks_the_actual_cadence():
    """Derived from the strategy's real cadence (~2 cycles), not a constant, and
    capped so a slow strategy doesn't inherit a multi-minute hold."""
    mid = er.map_strategy_config(
        "mid", {"notional_usd": 100.0, "interval_seconds": 8}, Decimal("100"),
        product="BTC-PERP", leverage=1,
    )
    assert mid["min_quote_lifetime_s"] == Decimal("16")
    slow = er.map_strategy_config(
        "grid", {"notional_usd": 100.0, "interval_seconds": 600, "fill_anchored": 1},
        Decimal("100"), product="BTC-PERP", leverage=1,
    )
    assert slow["min_quote_lifetime_s"] == Decimal("30"), "cap not applied"
    explicit = er.map_strategy_config(
        "mid", {"notional_usd": 100.0, "min_quote_lifetime_s": 5}, Decimal("100"),
        product="BTC-PERP", leverage=1,
    )
    assert explicit["min_quote_lifetime_s"] == Decimal("5"), "explicit setting ignored"
