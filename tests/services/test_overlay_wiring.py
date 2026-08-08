"""Overlay runtime wiring — _maybe_apply_overlay mutates the mapped configs
for MM strategies and no-ops elsewhere. Pure of any real DB/venue (mocked)."""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from src.nadobro.strategy import engine_runtime as er


class _FakeClient:
    """get_candlesticks returns an uptrend for every timeframe."""
    def get_candlesticks(self, product_id, timeframe, limit, max_time=None):
        return [
            {"close": 100 + i * 0.4, "high": 100 + i * 0.4 + 1, "low": 100 + i * 0.4 - 1, "volume": 10}
            for i in range(80)
        ]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    from src.nadobro.strategy import market_features as mf
    mf.reset_cache()
    er._OVERLAY_FUNDING_CACHE.clear()
    monkeypatch.setenv("NADO_SIGNAL_OVERLAY", "1")
    # Persistence is best-effort; stub it so no DB is needed.
    import src.nadobro.models.database as db
    monkeypatch.setattr(db, "insert_overlay_signal", lambda row: 1, raising=False)
    yield
    mf.reset_cache()
    er._OVERLAY_FUNDING_CACHE.clear()


def test_overlay_steers_mid_bias_and_size():
    cfg = {
        "order_amount_quote": Decimal("500"),
        "spread_bid_pct": Decimal("0.0005"),
        "spread_ask_pct": Decimal("0.0005"),
        "directional_bias": 0.0,
    }
    state = {"strategy": "mid", "strategy_session_id": 1, "sl_pct": 0.5, "tp_pct": 1.0}
    asyncio.run(er._maybe_apply_overlay(
        7, "mainnet", "mid", "BTC", 2, cfg, state, client=_FakeClient(), mid=131.6,
    ))
    # Uptrend -> long bias applied, size scaled up, spread widened.
    assert cfg["directional_bias"] > 0.2
    assert Decimal(str(cfg["order_amount_quote"])) > Decimal("500")


def test_overlay_never_overwrites_user_set_bias():
    """USER-BIAS-WINS (2026-07-30): a user-set directional lean is a binding
    contract. The uptrend fixture makes the overlay want a long bias, but the
    user's explicit short lean must survive untouched (the overlay may steer
    the bias only while the user is neutral — see the test above)."""
    cfg = {
        "order_amount_quote": Decimal("500"),
        "spread_bid_pct": Decimal("0.0005"),
        "spread_ask_pct": Decimal("0.0005"),
        "directional_bias": -0.5,   # mapped from the user's setting
    }
    state = {"strategy": "mid", "strategy_session_id": 1, "sl_pct": 0.5,
             "tp_pct": 1.0, "directional_bias": -0.5}
    asyncio.run(er._maybe_apply_overlay(
        7, "mainnet", "mid", "BTC", 2, cfg, state, client=_FakeClient(), mid=131.6,
    ))
    assert cfg["directional_bias"] == -0.5


def test_overlay_size_up_never_exceeds_risk_cap():
    """OVERLAY-SIZE-VS-RISK-CAP (2026-07-30): Mid's base order sits exactly at
    max_single_order_quote (one full deployed quote per side). The uptrend
    fixture makes the overlay scale size UP; unclamped, the risk engine would
    refuse every spawn and the session goes LIVE with 0 orders."""
    from src.nadobro.engine.types import RiskLimits

    cfg = {
        "order_amount_quote": Decimal("500"),
        "spread_bid_pct": Decimal("0.0005"),
        "spread_ask_pct": Decimal("0.0005"),
        "directional_bias": 0.0,
    }
    state = {"strategy": "mid", "strategy_session_id": 1, "sl_pct": 0.5, "tp_pct": 1.0}
    limits = RiskLimits(max_single_order_quote=Decimal("500"))
    asyncio.run(er._maybe_apply_overlay(
        7, "mainnet", "mid", "BTC", 2, cfg, state,
        client=_FakeClient(), mid=131.6, limits=limits,
    ))
    # The overlay wanted a size-up (see test above), but the order must stay
    # within the risk cap so the spawn is never refused.
    assert Decimal(str(cfg["order_amount_quote"])) == Decimal("500")


def test_overlay_noop_for_non_mm_strategy():
    cfg = {"leg_amount_quote": Decimal("50")}
    before = dict(cfg)
    state = {"strategy": "dn"}
    asyncio.run(er._maybe_apply_overlay(
        7, "mainnet", "dn", "BTC", 2, cfg, state, client=_FakeClient(), mid=100.0,
    ))
    assert cfg == before   # DN is not an overlay strategy


def test_overlay_noop_when_flag_off(monkeypatch):
    monkeypatch.setenv("NADO_SIGNAL_OVERLAY", "0")
    cfg = {"order_amount_quote": Decimal("500"), "directional_bias": 0.0}
    before = dict(cfg)
    state = {"strategy": "mid"}
    asyncio.run(er._maybe_apply_overlay(
        7, "mainnet", "mid", "BTC", 2, cfg, state, client=_FakeClient(), mid=100.0,
    ))
    assert cfg == before


def test_overlay_noop_without_client_candles():
    cfg = {"order_amount_quote": Decimal("500"), "directional_bias": 0.0}
    before = dict(cfg)
    state = {"strategy": "grid"}
    # client without get_candlesticks -> overlay bails cleanly.
    asyncio.run(er._maybe_apply_overlay(
        7, "mainnet", "grid", "BTC", 2, cfg, state, client=object(), mid=100.0,
    ))
    assert cfg == before


def test_overlay_writes_barrier_state_for_rail():
    cfg = {"order_amount_quote": Decimal("500"), "directional_bias": 0.0}
    state = {"strategy": "mid", "strategy_session_id": 1, "sl_pct": 0.5, "tp_pct": 1.0}
    asyncio.run(er._maybe_apply_overlay(
        7, "mainnet", "mid", "BTC", 2, cfg, state, client=_FakeClient(), mid=131.6,
    ))
    # Regime-adjusted barriers surfaced to state for the session rail.
    assert "overlay_sl_pct" in state and state["overlay_sl_pct"] > 0
    assert "overlay_tp_pct" in state and state["overlay_tp_pct"] > 0


def test_overlay_rail_sl_never_widens_past_user_stop():
    """The uptrend fixture reads as a trend (signal SL = base x 1.3), but the
    rail barrier must stay clamped at the user's configured stop."""
    cfg = {"order_amount_quote": Decimal("500"), "directional_bias": 0.0}
    state = {"strategy": "mid", "strategy_session_id": 1, "sl_pct": 0.5, "tp_pct": 1.0}
    asyncio.run(er._maybe_apply_overlay(
        7, "mainnet", "mid", "BTC", 2, cfg, state, client=_FakeClient(), mid=131.6,
    ))
    assert state["overlay_sl_pct"] <= 0.5


def test_overlay_rail_stays_disarmed_when_user_has_no_sl_tp():
    """A user who runs without a session SL/TP must not get rail barriers armed
    by the overlay (the 10% drawdown cap is the backstop)."""
    cfg = {"order_amount_quote": Decimal("500"), "directional_bias": 0.0}
    state = {"strategy": "mid", "strategy_session_id": 1, "sl_pct": 0.0, "tp_pct": 0.0}
    asyncio.run(er._maybe_apply_overlay(
        7, "mainnet", "mid", "BTC", 2, cfg, state, client=_FakeClient(), mid=131.6,
    ))
    assert "overlay_sl_pct" not in state
    assert "overlay_tp_pct" not in state


def test_overlay_persist_throttled_while_signal_holds(monkeypatch):
    """A steady signal must not insert a row every tick — only on an applied
    change or the heartbeat."""
    inserts = {"n": 0}
    import src.nadobro.models.database as db
    monkeypatch.setattr(
        db, "insert_overlay_signal", lambda row: inserts.update(n=inserts["n"] + 1) or 1,
        raising=False,
    )
    cfg_factory = lambda: {"order_amount_quote": Decimal("500"), "directional_bias": 0.0}
    state = {"strategy": "mid", "strategy_session_id": 1, "sl_pct": 0.5, "tp_pct": 1.0}
    for _ in range(3):
        asyncio.run(er._maybe_apply_overlay(
            7, "mainnet", "mid", "BTC", 2, cfg_factory(), state,
            client=_FakeClient(), mid=131.6,
        ))
    assert inserts["n"] == 1
    # The stable factors are still applied to every cycle's fresh configs.
    cfg = cfg_factory()
    asyncio.run(er._maybe_apply_overlay(
        7, "mainnet", "mid", "BTC", 2, cfg, state, client=_FakeClient(), mid=131.6,
    ))
    assert cfg["directional_bias"] != 0.0


def test_overlay_funding_read_is_cached_across_ticks():
    calls = {"funding": 0}

    class _ClientWithFunding(_FakeClient):
        def get_perp_funding_rates(self, product_ids):
            calls["funding"] += 1
            return {int(product_ids[0]): {"funding_rate": 0.0012}}

    state = {"strategy": "mid", "strategy_session_id": 1, "sl_pct": 0.5, "tp_pct": 1.0}
    for _ in range(3):
        asyncio.run(er._maybe_apply_overlay(
            7, "mainnet", "mid", "BTC", 2,
            {"order_amount_quote": Decimal("500"), "directional_bias": 0.0}, state,
            client=_ClientWithFunding(), mid=131.6,
        ))
    assert calls["funding"] == 1


def test_overlay_fetches_funding_without_breaking():
    calls = {"funding": 0}

    class _ClientWithFunding(_FakeClient):
        def get_perp_funding_rates(self, product_ids):
            calls["funding"] += 1
            return {int(product_ids[0]): {"funding_rate": 0.0012}}

    cfg = {"order_amount_quote": Decimal("500"), "directional_bias": 0.0,
           "spread_bid_pct": Decimal("0.0005"), "spread_ask_pct": Decimal("0.0005")}
    state = {"strategy": "mid", "strategy_session_id": 1, "sl_pct": 0.5, "tp_pct": 1.0}
    asyncio.run(er._maybe_apply_overlay(
        7, "mainnet", "mid", "BTC", 2, cfg, state, client=_ClientWithFunding(), mid=131.6,
    ))
    # Funding was fetched and the overlay still applied (bias set).
    assert calls["funding"] == 1
    assert cfg["directional_bias"] != 0.0


def test_overlay_survives_funding_fetch_failure():
    class _ClientFundingBoom(_FakeClient):
        def get_perp_funding_rates(self, product_ids):
            raise RuntimeError("indexer down")

    cfg = {"order_amount_quote": Decimal("500"), "directional_bias": 0.0}
    state = {"strategy": "mid", "strategy_session_id": 1, "sl_pct": 0.5, "tp_pct": 1.0}
    # Must not raise; overlay still applies from candle features.
    asyncio.run(er._maybe_apply_overlay(
        7, "mainnet", "mid", "BTC", 2, cfg, state, client=_ClientFundingBoom(), mid=131.6,
    ))
    assert cfg["directional_bias"] != 0.0


def test_funding_flag_fires_with_held_long_position(monkeypatch):
    """End-to-end: a held long + positive funding -> the persisted signal
    carries a funding carry-cost risk (position side read from inventory)."""
    class _Hold:
        net_amount_base = 0.05          # long
    class _Inv:
        def get(self, *a, **k):
            return _Hold()
    class _Ctrl:
        inventory = _Inv()
        trading_pair = "BTC-PERP"
        id = "mid:7:mainnet"

    class _ClientFunding(_FakeClient):
        def get_perp_funding_rates(self, product_ids):
            return {int(product_ids[0]): {"funding_rate": 0.0015}}   # longs pay

    monkeypatch.setitem(er.RUNTIME._controllers, (7, "mainnet", "mid"), _Ctrl())
    captured = {}
    import src.nadobro.models.database as db
    monkeypatch.setattr(db, "insert_overlay_signal", lambda row: captured.update(row) or 1, raising=False)

    cfg = {"order_amount_quote": Decimal("500"), "directional_bias": 0.0}
    state = {"strategy": "mid", "strategy_session_id": 1, "sl_pct": 0.5, "tp_pct": 1.0}
    try:
        asyncio.run(er._maybe_apply_overlay(
            7, "mainnet", "mid", "BTC", 2, cfg, state, client=_ClientFunding(), mid=131.6,
        ))
    finally:
        er.RUNTIME._controllers.pop((7, "mainnet", "mid"), None)

    risks = captured.get("risks_json") or []
    assert any("Funding" in r and "paying" in r for r in risks)


# ==========================================================================
# Overlay read must not churn live orders (audit 2026-08-06, HIGH)
# ==========================================================================
# The overlay's regime read is handed to the controllers so D-Grid can weigh it
# on a flip and R-Grid can surface it. It is written into the mapped config —
# and the mapped config is hashed into the LIVE-CONFIG SIGNATURE, whose only job
# is to decide when to re-quote a live controller. `signal_bias`/
# `signal_confidence` are continuous floats recomputed from fresh candles every
# cycle, so including them re-placed the entire resting ladder every tick:
# permanent loss of queue position, and both of D-Grid's re-center throttles
# (reset_threshold_bp, _DGRID_RECENTER_MIN_INTERVAL_S) bypassed.
def test_signal_read_never_flips_the_live_config_signature():
    from src.nadobro.strategy.engine_runtime import _live_config_signature

    base = {"trading_pair": "BTC-PERP", "spread_bid_pct": "0.001", "step_pct": "0.0008"}
    quiet = dict(base, signal_regime="trend_up", signal_bias=0.6104, signal_confidence=0.5512)
    wobble = dict(base, signal_regime="trend_up", signal_bias=0.6109, signal_confidence=0.5518)
    assert _live_config_signature(quiet) == _live_config_signature(wobble), (
        "a 4th-decimal signal wobble re-quotes every live ladder"
    )
    # Even a full regime flip must not, on its own, re-place the book — the
    # controllers read it directly and act on it in their own tick.
    flipped = dict(base, signal_regime="trend_down", signal_bias=-0.7, signal_confidence=0.9)
    assert _live_config_signature(quiet) == _live_config_signature(flipped)
    # A REAL parameter change still must.
    assert _live_config_signature(quiet) != _live_config_signature(
        dict(quiet, spread_bid_pct="0.002")
    )


def test_a_real_config_change_still_re_quotes():
    """Guard the other direction: the exclusion must not blanket-disable the
    live-reconfig path."""
    from src.nadobro.strategy.engine_runtime import _live_config_signature

    base = {"trading_pair": "BTC-PERP", "order_amount_quote": "100"}
    for key, changed in (
        ("order_amount_quote", "250"),
        ("trading_pair", "ETH-PERP"),
        ("reset_threshold_pct", "0.01"),
    ):
        assert _live_config_signature(base) != _live_config_signature(dict(base, **{key: changed}))


def test_every_overlay_steered_controller_can_receive_the_signal_push():
    """The push before RUNTIME.tick is guarded by `hasattr(ctrl, "signal_regime")`.
    If a controller renames or drops those attributes the push silently no-ops and
    D-Grid's flip weighting dies with no error — pin the contract instead."""
    from decimal import Decimal

    from tests.engine._mock_nado import MockNadoAdapter

    from src.nadobro.engine.controllers.dynamic_grid import DynamicGridController
    from src.nadobro.engine.controllers.rgrid import RGridController
    from src.nadobro.engine.inventory import InventoryRepository
    from src.nadobro.engine.orchestrator import ExecutorOrchestrator

    for cls in (DynamicGridController, RGridController):
        c = cls(
            user_id=1, orchestrator=ExecutorOrchestrator(),
            adapter=MockNadoAdapter(mid=Decimal(100)), inventory=InventoryRepository(),
            configs={"trading_pair": "BTC-PERP"}, controller_id="X",
        )
        assert hasattr(c, "signal_regime"), cls.__name__
        assert hasattr(c, "signal_confidence"), cls.__name__
        # The push writes plain attributes; nothing may reject the assignment.
        c.signal_regime = "trend_down"
        c.signal_confidence = 0.9
        assert c.signal_regime == "trend_down" and c.signal_confidence == 0.9


def test_the_runtime_pushes_the_signal_before_ticking_not_via_live_config():
    """Structural guard. Routing the read through the live-config path is what
    re-quoted every ladder each cycle; it must stay a direct attribute write that
    happens BEFORE the tick (so the controller acts on THIS cycle's signal)."""
    import inspect

    from src.nadobro.strategy import engine_runtime as er

    source = inspect.getsource(er._run_engine_cycle_locked)
    push = source.index("_sig_ctrl.signal_regime =")
    tick = source.index("await RUNTIME.tick(")
    assert push < tick, "the signal must reach the controller before it ticks"
    # And the live-reconfig helpers must not ASSIGN it (that is the churn path).
    # Substring-match would trip on the explanatory comment, so match the write.
    assert "signal_regime =" not in inspect.getsource(er._apply_rgrid_controller_config)
    assert "signal_regime =" not in inspect.getsource(er._apply_fill_anchored_controller_config)
