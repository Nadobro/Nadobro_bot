"""Per-user analytics: Nado vs Nadobro volume, perp vs spot, windowed.

Pins the 2026-07-26 portfolio bug: the card showed "Volume $0.00" next to
"Realized +$16.73" for the same 24h window, because Volume/Fees/Funding were
aggregated from a transient client.get_matches(limit=200) list while Realized
PnL was overwritten from the full trades_<network> history. One ledger now
feeds both, so volume and PnL always describe the SAME rows.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.nadobro.quant.user_analytics import (
    aggregate_user_analytics,
    is_perp_fill,
)

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def _fill(*, hours_ago, product, quote, fee="0", via_nadobro=False, side="long", base="1"):
    return {
        "product_id": 2,
        "product_name": product,
        "side": side,
        "base_filled_x18": str(int(Decimal(base) * 10**18)),
        "quote_filled_x18": str(int(Decimal(quote) * 10**18)),
        "fee_x18": str(int(Decimal(fee) * 10**18)),
        "via_nadobro": via_nadobro,
        "filled_at": NOW - timedelta(hours=hours_ago),
        "submission_idx": f"{hours_ago}{product}{quote}",
    }


# ── classification ──────────────────────────────────────────────

def test_perp_vs_spot_classification():
    assert is_perp_fill({"product_name": "BTC-PERP"}) is True
    assert is_perp_fill({"product_name": "ETH:PERP-USDC"}) is True
    assert is_perp_fill({"product_name": "KBTC"}) is False       # spot book
    assert is_perp_fill({"product_name": "WETH"}) is False
    assert is_perp_fill({"product_name": "USDT0"}) is False
    # An explicitly stored flag always wins (historical rows cannot drift).
    assert is_perp_fill({"product_name": "KBTC", "is_perp": True}) is True


# ── windows ─────────────────────────────────────────────────────

def test_volume_windows_are_cumulative_and_scoped():
    fills = [
        _fill(hours_ago=1, product="BTC-PERP", quote="1000"),
        _fill(hours_ago=48, product="BTC-PERP", quote="500"),     # outside 24h
        _fill(hours_ago=24 * 10, product="BTC-PERP", quote="200"),  # outside 7d
        _fill(hours_ago=24 * 60, product="BTC-PERP", quote="50"),   # outside 30d
    ]
    out = aggregate_user_analytics(fills, [], now=NOW)
    v = out["nado_volume"]
    assert v["24h"]["total_usd"] == Decimal("1000")
    assert v["7d"]["total_usd"] == Decimal("1500")
    assert v["30d"]["total_usd"] == Decimal("1700")
    assert v["all"]["total_usd"] == Decimal("1750")   # All is NOT capped


def test_perp_and_spot_split_per_window():
    fills = [
        _fill(hours_ago=1, product="BTC-PERP", quote="1000"),
        _fill(hours_ago=2, product="KBTC", quote="250"),
    ]
    out = aggregate_user_analytics(fills, [], now=NOW)
    day = out["nado_volume"]["24h"]
    assert day["perp_usd"] == Decimal("1000")
    assert day["spot_usd"] == Decimal("250")
    assert day["total_usd"] == Decimal("1250")


def test_nadobro_volume_is_a_subset_of_nado_volume():
    """Nado volume = everything on the account (incl. Nado-UI trades);
    Nadobro volume = only fills the bot routed."""
    fills = [
        _fill(hours_ago=1, product="BTC-PERP", quote="1000", via_nadobro=True),
        _fill(hours_ago=2, product="BTC-PERP", quote="400"),   # traded on Nado UI
    ]
    out = aggregate_user_analytics(fills, [], now=NOW)
    assert out["nado_volume"]["24h"]["total_usd"] == Decimal("1400")
    assert out["nadobro_volume"]["24h"]["total_usd"] == Decimal("1000")
    assert out["nadobro_volume"]["24h"]["fills"] == 1
    assert out["nado_volume"]["24h"]["fills"] == 2


def test_realized_pnl_comes_from_the_venue_windows_or_is_unknown():
    """Realized PnL is the VENUE'S figure (position windows). Without it the
    analytics must say UNKNOWN (None) — never a fill-replay guess: on
    2026-09-16 the replay showed -$161.36 for a day the venue booked +$10.50."""
    fills = [
        _fill(hours_ago=1, product="BTC-PERP", quote="-60000", side="long"),
        _fill(hours_ago=0.5, product="BTC-PERP", quote="60100", side="short"),
    ]
    out = aggregate_user_analytics(fills, [], now=NOW)
    assert out["realized_source"] == "pending"
    assert all(out["realized_pnl"][w] is None for w in ("24h", "7d", "30d", "all"))
    assert out["nado_volume"]["24h"]["total_usd"] == Decimal("120100")

    venue = {
        "pnl_windows": {"24h": Decimal("10.4981"), "7d": Decimal("10.4981"), "30d": Decimal("-91.37"), "all": Decimal("18.54")},
        "wins_windows": {"24h": 4, "7d": 4, "30d": 120, "all": 300},
        "losses_windows": {"24h": 0, "7d": 0, "30d": 125, "all": 192},
    }
    out = aggregate_user_analytics(fills, [], now=NOW, venue_pnl=venue)
    assert out["realized_source"] == "venue"
    assert out["realized_pnl"]["24h"] == Decimal("10.4981")
    assert out["realized_pnl"]["30d"] == Decimal("-91.37")
    assert out["wins"]["24h"] == 4 and out["losses"]["30d"] == 125


def test_nadobro_volume_uses_the_venue_builder_id_when_known():
    """'Routed through builder 2900' is decided by the order appendix the venue
    returns with each match; rows synced before that column existed fall back
    to the bot's own attribution (via_nadobro / session / bot source)."""
    from src.nadobro.quant.user_analytics import is_nadobro_fill

    ours = 2900
    assert is_nadobro_fill({"builder_id": 2900, "via_nadobro": False, "source": "manual"}, ours) is True
    assert is_nadobro_fill({"builder_id": 0, "via_nadobro": True, "source": "manual"}, ours) is False   # a Nado-UI fill
    assert is_nadobro_fill({"builder_id": 1234, "strategy_session_id": 9}, ours) is False              # another builder
    # Legacy rows (no builder id yet): bot attribution decides.
    assert is_nadobro_fill({"builder_id": None, "via_nadobro": True}, ours) is True
    assert is_nadobro_fill({"builder_id": None, "strategy_session_id": 314, "source": "strategy"}, ours) is True
    assert is_nadobro_fill({"builder_id": None, "source": "copy"}, ours) is True
    assert is_nadobro_fill({"builder_id": None, "source": "manual", "via_nadobro": None}, ours) is False
    # Testnet (builder routing bypassed, builder_id config None): bot attribution decides.
    assert is_nadobro_fill({"builder_id": 0, "via_nadobro": True}, None) is True

    bot = _fill(hours_ago=1, product="BTC-PERP", quote="-60000")
    bot["builder_id"] = 2900
    ui = _fill(hours_ago=2, product="BTC-PERP", quote="-60000")
    ui["builder_id"] = 0
    ui["via_nadobro"] = False
    legacy = _fill(hours_ago=3, product="BTC-PERP", quote="-60000")
    legacy["builder_id"] = None
    legacy["strategy_session_id"] = 7
    out = aggregate_user_analytics([bot, ui, legacy], [], now=NOW, builder_id=2900)
    assert out["nado_volume"]["24h"]["fills"] == 3
    assert out["nadobro_volume"]["24h"]["fills"] == 2


def test_fees_are_not_double_counted_across_overlapping_columns():
    """The recorder stores fill_fee == fees == fee + builder; summing them all
    (the Night HOWL bug) inflated fees ~2.2x."""
    row = {
        "product_name": "BTC-PERP", "side": "long",
        "base_filled_x18": str(10**18), "quote_filled_x18": str(1000 * 10**18),
        "fill_fee": "2.0", "fees": "2.0", "builder_fee": "0.5",
        "filled_at": NOW - timedelta(hours=1), "submission_idx": "1",
    }
    out = aggregate_user_analytics([row], [], now=NOW)
    assert out["fees"]["24h"] == Decimal("2.0")


def test_funding_is_reported_paid_positive_as_a_cost():
    # amount_x18 positive = paid by the user (indexer convention).
    payments = [{"amount_x18": str(3 * 10**18), "paid_at": NOW - timedelta(hours=2)}]
    out = aggregate_user_analytics([], payments, now=NOW)
    assert out["funding"]["24h"] == Decimal("3")


def test_empty_ledger_is_all_zero_not_an_error():
    zero = {w: Decimal("0") for w in ("24h", "7d", "30d", "all")}
    out = aggregate_user_analytics([], [], now=NOW, venue_pnl={"pnl_windows": zero, "wins_windows": {}, "losses_windows": {}})
    for w in ("24h", "7d", "30d", "all"):
        assert out["nado_volume"][w]["total_usd"] == Decimal("0")
        assert out["nadobro_volume"][w]["total_usd"] == Decimal("0")
        assert out["realized_pnl"][w] == Decimal("0")


# ── SPOT-EXIT-GUARANTEE piece 2a: entries born closeable ────────
# A venue min NOTIONAL applies to the EXIT too. kBTC's minimum is $100 and a ~$99
# Delta Neutral leg therefore cannot be closed by a resting limit — fees plus any
# adverse tick put the exit under the floor, the venue rejects it, and the leg
# strands naked (prod 2026-07-28).

def test_min_closeable_entry_leaves_room_above_the_venue_floor():
    from src.nadobro.quant.mm_quote_math import min_closeable_entry_notional
    assert min_closeable_entry_notional(100.0) > 100.0
    # A $99 leg against a $100 floor is exactly the reported case.
    assert 99.0 < min_closeable_entry_notional(100.0)


def test_min_closeable_entry_is_inert_without_a_venue_minimum():
    from src.nadobro.quant.mm_quote_math import min_closeable_entry_notional
    for bad in (0, 0.0, None, -5, "x"):
        assert min_closeable_entry_notional(bad) == 0.0


def test_min_closeable_entry_scales_with_the_floor():
    from src.nadobro.quant.mm_quote_math import min_closeable_entry_notional
    assert min_closeable_entry_notional(200.0) == 2 * min_closeable_entry_notional(100.0)


def test_the_buffer_survives_a_taker_round_trip_and_drift():
    """The buffer must cover both fees and the price drift between entry/exit."""
    from src.nadobro.quant.mm_quote_math import min_closeable_entry_notional
    floor = 100.0
    entry = min_closeable_entry_notional(floor)
    taker_round_trip = entry * 0.00033 * 2          # Nado 0.033% taker, both ways
    adverse_drift = entry * 0.10                    # a 10% move against us
    assert entry - taker_round_trip - adverse_drift > floor


# ── audit round 3: the DN leg meta must be REAL, and the floor must see it ──

def test_dn_leg_meta_uses_the_real_catalog_values_not_placeholders():
    """Literal tick/lot/min_notional made the spot-exit clamp floor a 0.00155
    kBTC balance to 0.001 — stranding ~35% of the leg — and made the sub-minimum
    MARKET fallback unreachable ($99 > a fake $1 minimum)."""
    from decimal import Decimal
    from unittest.mock import patch
    from src.nadobro.engine.adapter.nado import ProductMeta
    from src.nadobro.strategy import engine_runtime as er

    # The catalog-built map, keyed by SYMBOL, carrying kBTC's real venue values.
    meta = {
        "KBTC": ProductMeta(1, Decimal("1"), Decimal("0.00005"), Decimal(100),
                            is_perp=False, isolated_only=False),
        "BTC": ProductMeta(2, Decimal("1"), Decimal("0.00001"), Decimal(10),
                           is_perp=True, isolated_only=False),
    }
    configs = {"trading_pair_long": "BTC-USDT0", "trading_pair_short": "BTC-PERP"}

    with patch.object(er, "get_dn_pair", create=True), \
         patch("src.nadobro.venue.product_catalog.get_dn_pair",
               return_value={"spot_product_id": 1, "perp_product_id": 2}), \
         patch("src.nadobro.venue.product_catalog.is_product_isolated_only",
               return_value=False):
        er._materialize_dn_leg_meta(meta, configs, None, "mainnet", "BTC")

    spot = meta["BTC-USDT0"]
    assert spot.product_id == 1
    assert spot.lot_size == Decimal("0.00005"), (
        f"lot is {spot.lot_size}, not the venue's 0.00005 — the exit clamp would "
        f"floor 0.00155 to {int(Decimal('0.00155') / spot.lot_size) * spot.lot_size} "
        f"and strand the rest"
    )
    assert spot.min_notional == Decimal(100), (
        "min_notional is a placeholder, so the sub-minimum MARKET fallback that "
        "rescues a $99 exit can never fire"
    )
    assert spot.is_perp is False
    perp = meta["BTC-PERP"]
    assert perp.product_id == 2 and perp.is_perp is True


def test_the_closeable_entry_floor_runs_after_the_dn_legs_are_registered():
    """The floor reads meta[trading_pair_long]. It used to run BEFORE
    _materialize_dn_leg_meta created that key, so it was dead for DN."""
    import pathlib
    src = pathlib.Path("src/nadobro/strategy/engine_runtime.py").read_text()
    i_mat = src.index("_materialize_dn_leg_meta(meta, configs, client, network, product)")
    i_floor = src.index("min_closeable_entry_notional(_mn)")
    assert i_mat < i_floor, (
        "the closeable-entry floor runs before the DN leg meta exists — "
        "meta.get(trading_pair_long) is None and the floor is a no-op for DN"
    )


# ── audit round 4: risk caps must follow the closeable-entry floor ──
# map_risk_limits derives the DN cap from the PRE-floor size
# (per_order_cap = fixed_margin_usd * hedge * 2). The floor then raises the leg,
# so a small preset is floored ABOVE its own cap and every leg is rejected — the
# session goes LIVE and places nothing.

def test_the_dn_risk_cap_is_too_small_for_a_floored_leg_unless_recomputed():
    """The arithmetic of the failure, stated plainly."""
    from src.nadobro.quant.mm_quote_math import min_closeable_entry_notional
    from src.nadobro.strategy.engine_runtime import map_risk_limits

    pre_floor_cap = float(map_risk_limits(
        {"fixed_margin_usd": 50.0}, "dn", leverage=1).max_single_order_quote)
    floored_leg = min_closeable_entry_notional(100.0)          # kBTC $100 min

    assert floored_leg > pre_floor_cap, (
        f"precondition: a $50 leg floored to ${floored_leg:.2f} must exceed its "
        f"pre-floor cap of ${pre_floor_cap:.2f} for this bug to exist"
    )
    post_floor_cap = float(map_risk_limits(
        {"fixed_margin_usd": floored_leg}, "dn", leverage=1).max_single_order_quote)
    assert post_floor_cap >= floored_leg, (
        "recomputing the caps from the floored size still cannot admit the leg"
    )


def test_engine_runtime_recomputes_limits_when_the_floor_fires():
    """Static guard on the ordering + the recompute, since exercising
    run_engine_cycle end to end needs a live client."""
    import pathlib
    src = pathlib.Path("src/nadobro/strategy/engine_runtime.py").read_text()
    i_floor = src.index("min_closeable_entry_notional(_mn)")
    i_recompute = src.index("limits = map_risk_limits(", i_floor)
    i_consume = src.index("limits=limits,", i_floor)
    assert i_floor < i_recompute < i_consume, (
        "the floor must recompute `limits` BEFORE build_orchestrator consumes them"
    )
    # and the recompute must be keyed on the size field the strategy actually uses
    window = src[i_floor:i_consume]
    assert "fixed_margin_usd" in window and "session_margin_usd" in window, (
        "the recompute must cover both dn (fixed_margin_usd) and vol "
        "(session_margin_usd) — the two strategies the floor applies to"
    )


def test_vol_risk_cap_also_admits_a_floored_notional():
    from src.nadobro.quant.mm_quote_math import min_closeable_entry_notional
    from src.nadobro.strategy.engine_runtime import map_risk_limits

    floored = min_closeable_entry_notional(100.0)
    cap = float(map_risk_limits(
        {"session_margin_usd": floored}, "vol", leverage=1).max_single_order_quote)
    assert cap >= floored
