"""Turning the mark-out ledger into a widening decision.

Three properties, in order of how much they cost if wrong:

* **widen only** — a mark-out series is evidence of harm, never evidence that
  quoting tighter is safe;
* **never block a tick** — the controller asks every 3-8s; the answer comes
  from a TTL cache and the query runs on the DB pool;
* **no evidence, no change** — below the sample floor the factor is 1.0.
"""
import asyncio

import pytest

from src.nadobro.quant import markout as mk
from src.nadobro.trading import markout_defense as md


@pytest.fixture(autouse=True)
def _clean():
    md.reset_state()
    yield
    md.reset_state()


def _sample(net_bp, horizon=60.0):
    return mk.MarkoutSample(
        horizon_nominal_s=horizon, horizon_actual_s=horizon, ref_price=100.0,
        markout_bp=net_bp + 5.0, net_markout_bp=net_bp, ref_source=mk.REF_CANDLE_1M,
    )


def _factor(**kw):
    return asyncio.run(md.widen_factor(1, "mainnet", "BTC-PERP", **kw))


# --- the decision -----------------------------------------------------------

def test_no_history_means_no_change(monkeypatch):
    monkeypatch.setattr(md, "_load_samples", lambda *a, **k: [])
    assert _factor(half_spread_bp=10.0) == 1.0


def test_too_few_samples_means_no_change(monkeypatch):
    monkeypatch.setattr(md, "_load_samples", lambda *a, **k: [_sample(-20.0)] * 5)
    assert _factor(half_spread_bp=10.0) == 1.0


def test_persistent_negative_markout_widens(monkeypatch):
    monkeypatch.setattr(md, "_load_samples",
                        lambda *a, **k: [_sample(-5.0)] * (md.MIN_SAMPLES + 10))
    factor = _factor(half_spread_bp=10.0)
    assert factor > 1.0
    # Covers the measured shortfall and no more: (10 + 5) / 10.
    assert factor == pytest.approx(1.5)


def test_profitable_fills_never_tighten(monkeypatch):
    # THE property. The fills that would have hurt at a tighter quote are, by
    # construction, absent from a sample that looks healthy.
    monkeypatch.setattr(md, "_load_samples",
                        lambda *a, **k: [_sample(40.0)] * (md.MIN_SAMPLES + 10))
    assert _factor(half_spread_bp=10.0) == 1.0


def test_the_widening_is_capped(monkeypatch):
    monkeypatch.setattr(md, "_load_samples",
                        lambda *a, **k: [_sample(-10_000.0)] * (md.MIN_SAMPLES + 10))
    assert _factor(half_spread_bp=10.0) == pytest.approx(md.MAX_FACTOR)


# --- caching ----------------------------------------------------------------

def test_the_answer_is_cached_so_a_tick_never_waits_on_the_database(monkeypatch):
    calls = []

    def _fake(*a, **k):
        calls.append(1)
        return [_sample(-5.0)] * (md.MIN_SAMPLES + 10)

    monkeypatch.setattr(md, "_load_samples", _fake)
    first = _factor(half_spread_bp=10.0)
    for _ in range(20):
        assert _factor(half_spread_bp=10.0) == first
    assert len(calls) == 1


def test_an_expired_entry_refreshes(monkeypatch):
    monkeypatch.setattr(md, "_load_samples",
                        lambda *a, **k: [_sample(-5.0)] * (md.MIN_SAMPLES + 10))
    _factor(half_spread_bp=10.0)
    monkeypatch.setattr(md, "_TTL_S", -1.0)
    monkeypatch.setattr(md, "_load_samples", lambda *a, **k: [])
    assert _factor(half_spread_bp=10.0) == 1.0


def test_a_failing_query_keeps_quoting_at_the_last_known_factor(monkeypatch):
    monkeypatch.setattr(md, "_load_samples",
                        lambda *a, **k: [_sample(-5.0)] * (md.MIN_SAMPLES + 10))
    good = _factor(half_spread_bp=10.0)
    assert good > 1.0
    monkeypatch.setattr(md, "_TTL_S", -1.0)

    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(md, "_compute", _boom)
    assert _factor(half_spread_bp=10.0) == pytest.approx(good)


def test_a_failing_query_with_no_history_is_neutral(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(md, "_compute", _boom)
    assert _factor(half_spread_bp=10.0) == 1.0


def test_the_cached_factor_is_readable_without_touching_the_database(monkeypatch):
    assert md.cached_factor(1, "mainnet", "BTC-PERP") is None
    monkeypatch.setattr(md, "_load_samples",
                        lambda *a, **k: [_sample(-5.0)] * (md.MIN_SAMPLES + 10))
    _factor(half_spread_bp=10.0)
    assert md.cached_factor(1, "mainnet", "BTC-PERP") == pytest.approx(1.5)


def test_products_are_cached_independently(monkeypatch):
    monkeypatch.setattr(md, "_load_samples",
                        lambda uid, net, prod, *a, **k:
                        [_sample(-5.0)] * (md.MIN_SAMPLES + 10) if prod == "BTC-PERP" else [])
    assert asyncio.run(
        md.widen_factor(1, "mainnet", "BTC-PERP", half_spread_bp=10.0)) > 1.0
    assert asyncio.run(
        md.widen_factor(1, "mainnet", "ETH-PERP", half_spread_bp=10.0)) == 1.0
