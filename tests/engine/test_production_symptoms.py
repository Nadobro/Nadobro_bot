"""END-TO-END: does this branch actually fix the four reported production symptoms?

Every other test in the branch pins a MECHANISM. Nothing pinned the OUTCOME the
user reported, which is the gap that mattered most. One test per symptom, each
driving the real controller/adapter code with the real production numbers.

Sources: Fly logs + read-only DB reads, 2026-07-28.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

from tests.engine._mock_nado import MockNadoAdapter

from src.nadobro.engine.controllers.dynamic_grid import DynamicGridController
from src.nadobro.engine.inventory import InventoryRepository
from src.nadobro.engine.types import TradeType
from src.nadobro.engine.orchestrator import ExecutorOrchestrator
from src.nadobro.engine.routines import variance_regime as vr


# ── SYMPTOM 1: rgrid quotes went stale ──────────────────────────
# Session 165: 2080 cycles, 26 orders, 2063 cycles placed NOTHING, and the same
# maker price (63352.4017854148) re-issued from 09:58 to 13:27. Cause: a 0.8%
# (80bp) re-center threshold on a 20bp ladder — BTC's whole 49bp session range
# could never reach it.

_S165 = {
    "trading_pair": "BTC-PERP",
    "start_price": "63200", "end_price": "63400", "limit_price": "0",
    "total_amount_quote": "1000", "min_spread_between_orders": "0.001",
    "max_open_orders": 3, "step_pct": "0.001", "levels_count": 3,
    "dgrid_reset_threshold_bp": 80.0,          # the user's real 0.8%
    "regime_gate_enabled": 0.0,
}


def test_symptom1_the_ladder_follows_price_within_the_sessions_own_range():
    """The ladder must re-quote on a move SMALLER than session 165's whole range."""
    async def body():
        adapter = MockNadoAdapter(mid=Decimal("63373.5"), venue_held={"BTC-PERP": Decimal(0)})
        orch = ExecutorOrchestrator()
        c = DynamicGridController(
            user_id=1, orchestrator=orch, adapter=adapter,
            inventory=InventoryRepository(),
            configs=dict(_S165, candle_provider=lambda p: [
                {"close": 63300 + (i % 2) * 20} for i in range(200)]),
        )
        await orch.spawn_controller(c)
        await orch.tick_controller(c.id)
        ex = orch.list(c.id, active_only=True)[0]
        before = [lv.open_price for lv in ex.levels]

        # BTC's ENTIRE session range was 49bp. Move only 30bp.
        adapter.set_mid(Decimal("63373.5") * (Decimal(1) + Decimal("0.0030")))
        await orch.tick_controller(c.id)

        after = [lv.open_price for lv in orch.list(c.id, active_only=True)[0].levels]
        assert after != before, (
            "a 30bp move — well inside session 165's 49bp range — still left every "
            "quote at its old price: the stale-ladder bug is NOT fixed"
        )
    asyncio.run(body())


# ── SYMPTOM 2: GRID<->RGRID flip-flop ───────────────────────────
# Six flips in forty minutes, every notification reading "downtrend detected"
# while half of them armed the LONG grid.

_RG = dict(short_window=3, long_window=8, trend_on=1.10, range_on=1.05,
           trend_drift_pct=0.15)


def _choppy(drop_per_candle, n=24, period=6, amp=120.0, start=63500.0):
    out = []
    for i in range(n):
        p = (i % period) / period
        out.append({"close": start - drop_per_candle * i
                    + amp * (1.0 - 2.0 * abs(2.0 * p - 1.0))})
    return out


def test_symptom2_a_downtrend_never_hands_the_short_back_to_the_long_grid():
    """Replays the reported alternation: rates wobbling either side of the 0.15%
    trend threshold while price falls throughout."""
    phase, flips, directions = vr.GRID, 0, []
    for rate in (16.0, 14.0, 18.0, 14.0, 17.0, 13.5, 16.5, 14.0):
        info = asyncio.run(vr.run("BTC-PERP", _choppy(rate), current_phase=phase, **_RG))
        directions.append(info["direction"])
        if info["phase"] != phase:
            flips += 1
        phase = str(info["phase"])
    assert all(d == vr.DOWN for d in directions), directions
    assert phase == vr.RGRID, "ended a sustained downtrend on the LONG grid"
    assert flips == 1, f"{flips} flips across one downtrend (production saw 6)"


def test_symptom2_the_reported_notification_can_no_longer_contradict_itself():
    """Every logged flip said 'downtrend detected'; half armed GRID. With the
    release in place, a DOWN read while short can never resolve to GRID."""
    for rate in (13.0, 14.0, 14.5, 15.0, 16.0, 18.0):
        info = asyncio.run(vr.run("BTC-PERP", _choppy(rate),
                                  current_phase=vr.RGRID, **_RG))
        if info["direction"] == vr.DOWN:
            assert info["phase"] == vr.RGRID, (
                f"direction=down but phase={info['phase']} — that is exactly the "
                f"'downtrend detected ... GRID now quoting' contradiction"
            )


# ── SYMPTOM 4: the phantom fill price that tripped the TP rail ──
# Session 164: fill_size 0.00265 (requested) against base_x18 0.00395 (venue) gave
# fill_price 250.18/0.00265 = 94,408 while BTC was 63.6k — a 1.49x inflation that
# booked a fake profit and stopped the run at a real $0.68.

def test_symptom4_the_human_fill_columns_follow_the_venue_quote():
    """SYMPTOM 4 — the $0.68 take-profit stop (phantom 94,408 fill price).

    AUDIT round 4: this was the branch's only end-to-end proof of the fix, and it
    was VACUOUS. It asserted on the params tuple, which is byte-identical before and
    after the fix — only the SQL's CASE *conditions* changed. Reverting the four
    CASE expressions left it green.

    The real proof now lives in tests/services/test_nado_sync_enrich_db.py, which
    runs the statement against a REAL Postgres row that already holds the phantom
    price, so the ELSE branch is actually observed. Verified: with the buggy SQL
    restored, 4 of its 5 tests FAIL (the mock-based ones passed).

    What is left here is the part a mock CAN prove, stated honestly: the params the
    enrich sends are derived from the VENUE quote and not from the recorder's
    requested size. That is necessary but NOT sufficient — do not treat this as the
    behavioural guard.
    """
    from unittest.mock import patch
    from src.nadobro.venue import nado_sync
    from src.nadobro.utils.x18 import to_x18

    calls = []

    def _q(sql, *params):
        if "NOT ILIKE" in sql:
            return {"id": 99, "fill_size": "0.00265"}   # the corrupt recorder row
        return None

    with patch.object(nado_sync, "query_one", side_effect=_q), \
         patch.object(nado_sync, "execute", side_effect=lambda *a, **k: calls.append(a)):
        nado_sync._write_matches(42, "mainnet", [{
            "submission_idx": "7", "digest": "0xdead", "product_id": 2,
            "base_filled": str(to_x18("0.00395")),
            "quote_filled": str(to_x18("-250.18")),
            "fee": str(to_x18("0.05")),
        }])

    params = calls[0][1]
    price = float(params[9])
    assert abs(price - 250.18 / 0.00395) < 0.01, price
    assert abs(price - 94408) > 1000, (
        f"the params carry the phantom {price:,.0f} — priced off the REQUESTED size"
    )

    # And the behavioural guard is reachable, so a future edit cannot quietly
    # delete the only real proof.
    import pathlib as _p
    db_test = _p.Path("tests/services/test_nado_sync_enrich_db.py")
    assert db_test.exists(), (
        "the DB-backed enrich test is gone — without it nothing observes the "
        "CASE/ELSE branch and this symptom has no real coverage"
    )
    body = db_test.read_text()
    assert "test_the_phantom_price_is_overwritten_on_a_real_row" in body


_DN = {
    # Session 167's shape: BTC spot long + BTC perp short, $99 a leg, 1h hold.
    "trading_pair_long": "BTC-USDT0", "trading_pair_short": "BTC-PERP",
    "trading_pair": "BTC-USDT0", "hedge_ratio": Decimal(1),
    "leg_amount_quote": Decimal(99), "hold_seconds": 3600, "cycles": 1,
    "leverage": 1,
}


def _dn(adapter, orch, inventory=None):
    from src.nadobro.engine.controllers.delta_neutral import DeltaNeutralController
    return DeltaNeutralController(
        user_id=1, orchestrator=orch, adapter=adapter,
        inventory=inventory if inventory is not None else InventoryRepository(),
        configs=dict(_DN), controller_id="dn:1:mainnet")


def test_symptom3_a_stranded_spot_leg_is_SURFACED_not_silently_naked():
    """SYMPTOM 3 — partially fixed, and the limit is stated on purpose.

    Session 167 left net spot +0.00155 (~$99) unhedged while the perp was flat.
    The engine hold recorded buy 0.00155 / sell 0.00155 — it NETS FLAT — so the
    controller cannot prove that balance is its own.

    Audit round 4 established that auto-selling it is NOT acceptable: the rule that
    did so (max of venue/book, capped at GROSS buys) also market-sold balances the
    user held for their own reasons, and oscillated sell/buy forever because the
    sweep's own fills moved the book.

    So the guarantee today is: exposure the hedge CAN attribute is swept; exposure
    it CANNOT is reported to the user and left alone, and the account is refused a
    new hedge while it is non-flat. The user is never silently naked, and we never
    dispose of coins we cannot prove we bought.

    TO FULLY CLOSE THIS: attribute residue from the PERSISTED per-session fills
    (trades_<network>.strategy_session_id — which for session 167 correctly shows
    spot buys 0.0031 vs sells 0.00155, i.e. net +0.00155) instead of the in-memory
    hold that EngineRuntime.start() wipes on every build. Then the leg becomes
    attributable and is swept automatically.
    """
    async def body():
        adapter = MockNadoAdapter(mid=Decimal("63890"), auto_fill_market=True,
                                  venue_held={"BTC-USDT0": Decimal("0.00155"),
                                              "BTC-PERP": Decimal(0)})
        orch = ExecutorOrchestrator()
        inv = InventoryRepository()
        for side in (TradeType.BUY, TradeType.SELL):     # the real hold: nets flat
            inv.apply_fill(1, "BTC-USDT0", "dn:1:mainnet", side,
                           Decimal("0.00155"), Decimal("63890"), Decimal(0))
        c = _dn(adapter, orch, inventory=inv)
        assert c._net_base("BTC-USDT0") == 0, "precondition: the book nets flat"

        flat = await c._residuals_flat()

        # 1) the user is TOLD, with the size
        kinds = [e["kind"] for e in c.consume_dn_events()]
        assert "residual_exposure" in kinds, (
            "the venue holds ~$99 of spot the hedge cannot account for and the user "
            "was never told — that is the silent naked leg from session 167"
        )
        # 2) it is NOT sold, because it cannot be proven ours
        assert not [o for o in adapter.placed if o.side is TradeType.SELL], (
            "sold a balance the controller cannot attribute to itself"
        )
        # 3) and it does not spin CLOSING forever
        assert flat is True

        # 4) a fresh hedge must NOT be stacked on top of it
        await c._open_cycle()
        assert "duplicate_open_blocked" in [e["kind"] for e in c.consume_dn_events()]

    asyncio.run(body())


def test_symptom3_the_hedge_is_never_opened_twice():
    """The race that created the orphan. A duplicate start must neither stack a
    second pair NOR tear down the healthy one."""
    from src.nadobro.engine.controllers.delta_neutral import DNPhase
    from src.nadobro.engine.types import TradeType

    async def body():
        adapter = MockNadoAdapter(mid=Decimal("63890"), auto_fill_market=True)
        orch = ExecutorOrchestrator()
        c = _dn(adapter, orch)
        await orch.spawn_controller(c)
        spot_buys = len([o for o in adapter.placed
                         if o.trading_pair == "BTC-USDT0" and o.side is TradeType.BUY])
        n_before = len(adapter.placed)

        await c.on_start()                     # the racing duplicate

        assert len([o for o in adapter.placed
                    if o.trading_pair == "BTC-USDT0" and o.side is TradeType.BUY]) \
            == spot_buys, "a SECOND spot long was opened — that made the orphan"
        assert len(adapter.placed) == n_before, "the duplicate touched the venue"
        assert c.phase is DNPhase.HOLDING, "the duplicate tore down a healthy hedge"

    asyncio.run(body())


def test_symptom3_a_spot_exit_is_never_sized_above_the_balance():
    """The other half: even when the sweep fires, the order must be fillable.
    A close grown above the held balance is rejected and the leg stays naked."""
    from unittest.mock import patch
    from src.nadobro.engine.adapter.nado import NadoAdapter, ProductMeta
    from src.nadobro.engine.types import OrderType, TradeType, TradeType

    sent = []

    class _C:
        def get_balance(self, *a, **k):
            return {"exists": True, "balances": {1: 0.00155}}

        def place_market_order(self, product_id, size, is_buy=True, **kw):
            sent.append(float(size))
            return {"digest": "m", "status": "filled", "price": 63890,
                    "filled_base": str(size), "filled_quote": str(size * 63890)}

    meta = {"S": ProductMeta(1, Decimal("1"), Decimal("0.00005"), Decimal(100),
                             is_perp=False, isolated_only=False)}

    async def body():
        await NadoAdapter(_C(), meta).place_order(
            "S", TradeType.SELL, OrderType.MARKET, Decimal("0.0031"),
            reduce_only=True)
        assert sent and sent[0] <= 0.00155 + 1e-12, (
            f"asked the venue to sell {sent[0]} of a 0.00155 balance — rejected, "
            f"and the leg stays naked"
        )

    asyncio.run(body())
