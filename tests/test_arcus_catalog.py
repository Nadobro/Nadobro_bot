"""venue/arcus/catalog.py — fail-closed market metadata (02 §7.2 / §12.7).

No default/fallback market ever: a bad row is DROPPED (counted), a denied /
empty / all-bad load keeps the OLD snapshot and lets it go stale.
"""
from __future__ import annotations

import asyncio
import copy
import logging
from decimal import Decimal

import pytest

from arcus_helpers import FakeMono, load_fixture
from src.nadobro.venue.arcus import catalog as CAT
from src.nadobro.venue.arcus import errors as E
from src.nadobro.venue.arcus.catalog import ArcusCatalog, ArcusMarket, TickTier, parse_market
from src.nadobro.venue.arcus.errors import (
    ArcusSchemaError,
    InexactUnitError,
    Ok,
    Throttled,
    Unavailable,
    schema_error_counts,
)
from src.nadobro.venue.arcus.types import Lane, Side

D = Decimal


@pytest.fixture(autouse=True)
def _fresh_schema_counts():
    E._reset_schema_errors_for_tests()
    yield
    E._reset_schema_errors_for_tests()


def _rows() -> list[dict]:
    return load_fixture("markets_testnet_subset.json")["markets"]


def _row(ticker: str) -> dict:
    return copy.deepcopy(next(r for r in _rows() if r["marketDisplayName"] == ticker))


def _loaded(rows=None, **kwargs) -> tuple[ArcusCatalog, FakeMono]:
    mono = FakeMono(0.0)
    cat = ArcusCatalog("testnet", clock=mono, **kwargs)
    assert cat.load_from_payload(_rows() if rows is None else rows)
    return cat, mono


def _schema_total() -> int:
    return sum(schema_error_counts().values())


# --- parse_market ------------------------------------------------------------------------


def test_parse_btc_eth_sol():
    btc = parse_market(_row("BTC-USD"))
    assert btc.market_id == 1 and btc.ticker == "BTC-USD"
    assert (btc.status, btc.type, btc.category) == ("ONLINE", "PERPETUAL", "CRYPTO")
    assert (btc.base_asset, btc.quote_asset) == ("BTC", "USD")
    assert btc.tick_size == D("0.1") and btc.step_size == D("0.00000001")
    assert len(btc.tick_tiers) == 6
    assert btc.tick_tiers[0] == TickTier(D("500000"), D("0.1"))
    assert btc.tick_tiers[-1] == TickTier(None, D("5"))
    assert btc.min_order_notional == D("5") and btc.min_order_size == D("0.0001")
    assert btc.max_order_size == D("10000")
    assert btc.imf == D("0.025") and btc.mmf == D("0.016667") and btc.off_hours_imf == D("0.025")
    assert btc.max_leverage() == 40
    assert btc.mark_price == D("84321.1") and btc.oracle_price == D("84532.4")
    assert btc.volume_24h_notional == D("5155.4")
    assert btc.open_interest_cap_notional is None  # live sends null
    assert btc.is_outside_rth is False
    assert parse_market(_row("ETH-USD")).max_leverage() == 25
    assert parse_market(_row("SOL-USD")).max_leverage() == 20
    amd = parse_market(_row("AMD-USD"))
    assert amd.category == "EQUITIES" and amd.is_outside_rth is True and amd.off_hours_imf == D("0.15")
    f_usd = parse_market(_row("F-USD"))
    # numeric-zero rule: live F-USD sends "oraclePrice": "0.0000", "markPrice": "0"
    assert f_usd.oracle_price is None and f_usd.mark_price is None
    assert f_usd.status == "OFFLINE" and f_usd.is_outside_rth is None


def test_parse_mainnet_offline_crypto_capture():
    (row,) = load_fixture("markets_mainnet_offline_crypto.json")["markets"]
    kbonk = parse_market(row)
    assert kbonk.market_id == 67 and kbonk.status == "OFFLINE" and kbonk.category == "CRYPTO"
    assert kbonk.tick_size == D("0.00001") and kbonk.tick_for_price(D("0.003")) == D("0.0001")


def test_ticker_upper_cased_and_validated():
    row = _row("BTC-USD")
    row["marketDisplayName"] = "btc-usd"
    assert parse_market(row).ticker == "BTC-USD"
    row["marketDisplayName"] = "BTC/USD"
    with pytest.raises(ArcusSchemaError):
        parse_market(row)


def test_max_leverage_floors():
    row = _row("BTC-USD")
    row["initialMarginFraction"] = "0.03"
    row["maintenanceMarginFraction"] = "0.02"
    assert parse_market(row).max_leverage() == 33


# --- ticks / quantization ------------------------------------------------------------------


def test_tick_for_price_exclusive_bounds():
    btc = parse_market(_row("BTC-USD"))
    assert btc.tick_for_price(D("499999.9")) == D("0.1")
    assert btc.tick_for_price(D("500000")) == D("0.2")  # upToPrice is EXCLUSIVE
    assert btc.tick_for_price(D("999999.8")) == D("0.2")
    assert btc.tick_for_price(D("10000000")) == D("5")  # last (unbounded) band


def test_quantize_maker_and_crossing():
    btc = parse_market(_row("BTC-USD"))
    assert btc.quantize_price(D("84517.34"), Side.BUY) == D("84517.3")
    assert btc.quantize_price(D("84517.34"), Side.SELL) == D("84517.4")
    assert btc.quantize_price(D("84517.34"), Side.BUY, crossing=True) == D("84517.4")
    assert btc.quantize_price(D("84517.34"), Side.SELL, crossing=True) == D("84517.3")
    assert btc.quantize_price(D("84517.3"), Side.BUY) == D("84517.3")  # already on-grid
    assert btc.quantize_price(D("499999.95"), Side.SELL) == D("500000.0")
    # a band edge that needs a second pass (tickSize 0.1, second band tick 0.2)
    row = _row("BTC-USD")
    row["tickTiers"] = [{"upToPrice": "1.05", "tick": "0.1"}, {"tick": "0.2"}]
    syn = parse_market(row)
    assert syn.quantize_price(D("1.02"), Side.SELL) == D("1.2")
    assert syn.quantize_price(D("1.1"), Side.BUY) == D("1.0")  # floors back into the finer band
    for bad in (D("0"), D("-1"), D("NaN"), D("Infinity"), 1.5, "84517"):
        with pytest.raises(ValueError):
            btc.quantize_price(bad, Side.BUY)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        btc.quantize_price(D("0.05"), Side.BUY)  # floors to 0
    with pytest.raises(ValueError):
        btc.quantize_price(D("1"), "BUY")  # type: ignore[arg-type]


def test_quantize_every_result_is_signable():
    """Every quantized price is an exact multiple of the TOP-LEVEL tickSize (the
    signing divisor) and of its own band's tick."""
    btc = parse_market(_row("BTC-USD"))
    for raw in ("84517.37", "499999.99", "500000.07", "999999.95", "1000000.3", "12345678.9"):
        for side in (Side.BUY, Side.SELL):
            for crossing in (False, True):
                q = btc.quantize_price(D(raw), side, crossing=crossing)
                assert (q / btc.tick_size) == (q / btc.tick_size).to_integral_value()
                band = btc.tick_for_price(q)
                assert (q / band) == (q / band).to_integral_value()


def test_quantize_tier_exhaustion_raises():
    # A pathological tier table where the rounding keeps crossing edges.
    market = ArcusMarket(
        market_id=99, ticker="X-USD", status="ONLINE", type="PERPETUAL", category="CRYPTO",
        base_asset="X", quote_asset="USD", tick_size=D("0.1"), step_size=D("1"),
        # SELL 1.02 -> 1.2 (band tick 0.8) -> 1.6 (3.2) -> 3.2 (6.4) -> 6.4 (12.8): never settles in 4 passes
        tick_tiers=(
            TickTier(D("1.01"), D("0.1")),
            TickTier(D("1.19"), D("0.2")),
            TickTier(D("1.59"), D("0.8")),
            TickTier(D("3.19"), D("3.2")),
            TickTier(D("6.39"), D("6.4")),
            TickTier(None, D("12.8")),
        ),
        min_order_notional=D("5"), min_order_size=D("1"), max_order_size=D("10"),
        imf=D("0.1"), mmf=D("0.05"), off_hours_imf=D("0.1"), is_outside_rth=False,
        oracle_price=None, mark_price=None, volume_24h_notional=None, open_interest_cap_notional=None,
    )
    with pytest.raises(InexactUnitError):
        market.quantize_price(D("1.02"), Side.SELL)


def test_quantize_size_down():
    btc = parse_market(_row("BTC-USD"))
    assert btc.quantize_size_down(D("0.000123456789")) == D("0.00012345")
    assert btc.quantize_size_down(D("0.000000009")) == 0
    assert btc.quantize_size_down(D("0.0001")) == D("0.0001")
    for bad in (D("-0.1"), D("NaN"), 0.1):
        with pytest.raises(ValueError):
            btc.quantize_size_down(bad)  # type: ignore[arg-type]


def test_effective_min_notional():
    btc = parse_market(_row("BTC-USD"))
    assert btc.effective_min_notional(D("84321.1")) == D("8.43211")
    sol = parse_market(_row("SOL-USD"))
    assert sol.effective_min_notional(D("122")) == D("5")  # 0.01 × 122 = 1.22 < 5
    with pytest.raises(ValueError):
        btc.effective_min_notional(D("0"))


# --- fail-closed parsing ------------------------------------------------------------------


def test_oi_cap_spellings():
    row = _row("BTC-USD")
    row.pop("openInterestCapNotional")
    row["openInterestCap"] = "50000000"
    assert parse_market(row).open_interest_cap_notional == D("50000000")
    row = _row("BTC-USD")
    row["openInterestCapNotional"] = "1"
    assert parse_market(row).open_interest_cap_notional == D("1")
    row["openInterestCap"] = "1.0"
    assert parse_market(row).open_interest_cap_notional == D("1")  # both, equal
    row["openInterestCap"] = "2"
    before = _schema_total()
    with pytest.raises(ArcusSchemaError):
        parse_market(row)
    assert _schema_total() == before + 1
    row = _row("BTC-USD")
    row.pop("openInterestCapNotional")
    assert parse_market(row).open_interest_cap_notional is None


def _mutations() -> dict[str, callable]:
    def missing(key):
        return lambda r: r.pop(key)

    def setv(key, value):
        return lambda r: r.__setitem__(key, value)

    return {
        "missing_tickSize": missing("tickSize"),
        "missing_stepSize": missing("stepSize"),
        "tiers_unsorted": setv("tickTiers", [{"upToPrice": "1000000", "tick": "0.1"}, {"upToPrice": "500000", "tick": "0.2"}, {"tick": "5"}]),
        "last_tier_bounded": setv("tickTiers", [{"upToPrice": "500000", "tick": "0.1"}, {"upToPrice": "1000000", "tick": "0.2"}]),
        "first_tier_not_ticksize": setv("tickTiers", [{"upToPrice": "500000", "tick": "0.2"}, {"tick": "5"}]),
        "tier_not_multiple": setv("tickTiers", [{"upToPrice": "500000", "tick": "0.1"}, {"tick": "0.15"}]),
        "tiers_empty": setv("tickTiers", []),
        "tier_tick_zero": setv("tickTiers", [{"upToPrice": "500000", "tick": "0.1"}, {"tick": "0"}]),
        "imf_zero": setv("initialMarginFraction", "0"),
        "imf_above_one": setv("initialMarginFraction", "1.5"),
        "mmf_above_imf": setv("maintenanceMarginFraction", "0.03"),
        "missing_minOrderSize": missing("minOrderSize"),
        "missing_offhours_imf": missing("offHoursInitialMarginFraction"),
        "max_size_zero": setv("maxOrderSize", "0"),
        "bad_status_type": setv("status", 1),
        "rth_not_bool": setv("isOutsideRth", "no"),
        "negative_mark": setv("markPrice", "-1"),
        "float_tick": setv("tickSize", 0.1),
        "missing_market_id": missing("marketId"),
    }


@pytest.mark.parametrize("name", sorted(_mutations()))
def test_fail_closed_drops(name):
    row = _row("BTC-USD")
    _mutations()[name](row)
    rows = [row, _row("ETH-USD"), _row("SOL-USD")]
    before = _schema_total()
    cat, _ = _loaded(rows)
    assert cat.get(1) is None and cat.by_ticker("BTC-USD") is None  # never a default meta
    assert cat.get(2) is not None and cat.get(3) is not None  # other markets still load
    assert _schema_total() > before
    assert cat.last_error == "dropped:1"


def test_duplicate_id_or_ticker_drops_both():
    dup = _row("ETH-USD")
    dup["marketId"] = 1  # same id as BTC-USD
    cat, _ = _loaded([_row("BTC-USD"), dup, _row("SOL-USD")])
    assert cat.get(1) is None and cat.by_ticker("ETH-USD") is None and cat.by_ticker("BTC-USD") is None
    assert cat.get(3) is not None
    assert schema_error_counts().get("markets.duplicate", 0) >= 1
    twin = _row("BTC-USD")
    twin["marketId"] = 42  # same ticker, other id
    cat, _ = _loaded([_row("BTC-USD"), twin, _row("SOL-USD")])
    assert cat.by_ticker("BTC-USD") is None and cat.get(42) is None and cat.get(1) is None


def test_load_keeps_old_on_empty_or_all_bad():
    cat, mono = _loaded()
    assert cat.last_error is None
    assert not cat.load_from_payload([])
    assert cat.last_error == "empty" and cat.get(1) is not None
    bad = _row("BTC-USD")
    bad.pop("tickSize")
    assert not cat.load_from_payload([bad])
    assert cat.get(1) is not None and cat.get(1).tick_size == D("0.1")
    assert cat.last_error == "no_valid_market"
    with pytest.raises(TypeError):
        cat.load_from_payload("markets")  # type: ignore[arg-type]


class _FakeMarkets:
    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[tuple[Lane, float | None]] = []

    async def get_markets(self, *, lane, max_wait_s=None):
        self.calls.append((lane, max_wait_s))
        return self.result


def test_refresh_denied_keeps_old_and_ages():
    async def body():
        mono = FakeMono(0.0)
        cat = ArcusCatalog("testnet", clock=mono, max_age_s=lambda: 300.0)
        assert cat.age_s() is None and not cat.is_fresh()
        ok = _FakeMarkets(Ok(value=_rows(), http_status=200, weight_charged=20))
        assert await cat.refresh(ok, lane=Lane.L0_BRAKE, max_wait_s=2.0)
        assert ok.calls == [(Lane.L0_BRAKE, 2.0)]
        assert cat.is_fresh()
        denied = _FakeMarkets(Throttled(layer="read_ip", retry_after_ms=1000, client_ids=()))
        mono.advance(200)
        assert not await cat.refresh(denied)
        assert denied.calls == [(Lane.L1_ENGINE, None)]
        assert cat.last_error == "Throttled"
        assert cat.get(1) is not None  # old data kept
        assert cat.age_s() == 200 and cat.is_fresh()
        mono.advance(101)
        assert not cat.is_fresh()  # stale -> callers refuse
        assert not await cat.refresh(_FakeMarkets(Unavailable(http_status=200, message="schema")))
        assert cat.last_error == "Unavailable" and cat.get(1) is not None

    asyncio.run(body())


def test_allowlisted_filters(caplog):
    cat, _ = _loaded()
    assert [m.ticker for m in cat.allowlisted()] == ["BTC-USD", "ETH-USD", "SOL-USD"]
    assert [m.market_id for m in cat.allowlisted()] == [1, 2, 3]
    assert all(m.ticker not in ("AMD-USD", "F-USD") for m in cat.allowlisted())
    cat, _ = _loaded(allowlist=lambda: {"btc-usd"})
    assert [m.ticker for m in cat.allowlisted()] == ["BTC-USD"]
    cat, _ = _loaded(allowlist=lambda: {"DOGE-USD", "AMD-USD"})
    assert cat.allowlisted() == ()  # the allowlist can only SHRINK the v1 set
    # id drift -> excluded + one ERROR
    drift = _row("BTC-USD")
    drift["marketId"] = 7
    cat, _ = _loaded([drift, _row("ETH-USD")])
    with caplog.at_level(logging.ERROR, logger=CAT.__name__):
        assert [m.ticker for m in cat.allowlisted()] == ["ETH-USD"]
        cat.allowlisted()
    assert sum("market id drift" in r.getMessage() for r in caplog.records) == 1
    assert cat.get(7) is not None  # still parsed (brakes can read it)
    for key, value in (("status", "OFFLINE"), ("category", "EQUITIES"), ("type", "SPOT")):
        row = _row("BTC-USD")
        row[key] = value
        cat, _ = _loaded([row, _row("SOL-USD")])
        assert [m.ticker for m in cat.allowlisted()] == ["SOL-USD"], key
        assert cat.get(1) is not None  # brakes still get tick/step


def test_by_ticker_case_insensitive():
    cat, _ = _loaded()
    assert cat.by_ticker("btc-usd") is cat.get(1)
    assert cat.by_ticker(" BTC-USD ") is cat.get(1)
    assert cat.by_ticker("NOPE") is None and cat.by_ticker(None) is None  # type: ignore[arg-type]
    assert cat.get(True) is None and cat.get("1") is None  # type: ignore[arg-type]


def test_catalog_validates_network():
    with pytest.raises(ValueError):
        ArcusCatalog("arcus_testnet")


@pytest.mark.parametrize("net", ["testnet", "mainnet"])
def test_live_capture_loads_without_drops(net):
    """Public ``GET /v1/markets`` captured 2026-09-30 (unauthenticated): every live
    market parses (no drop, no schema count) and the allowlist is exactly the v1
    set at its pinned ids — the fail-closed parser does not reject the real shape."""
    from src.nadobro.venue.arcus.parse import parse_markets_payload

    rows = parse_markets_payload(load_fixture(f"captured/{net}_markets_20260930.json"))
    assert len(rows) > 50
    cat = ArcusCatalog(net)
    assert cat.load_from_payload(rows)
    assert cat.last_error is None and schema_error_counts() == {}
    assert [(m.market_id, m.ticker) for m in cat.allowlisted()] == [(1, "BTC-USD"), (2, "ETH-USD"), (3, "SOL-USD")]
    for m in cat.allowlisted():
        assert m.mark_price is not None and m.oracle_price is not None
        assert m.tick_tiers[0].tick == m.tick_size and m.tick_tiers[-1].up_to_price is None
