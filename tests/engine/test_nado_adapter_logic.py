"""Unit tests for NadoAdapter order-state reconstruction logic.

These exercise the adapter's parsing/state machine against a lightweight
NadoClient stub that returns the *assumed* venue shapes (the field maps in
adapter/nado.py). They validate the logic; the exact field names are confirmed
on testnet via scripts/capture_nado_shapes.py. No live venue is used.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

from src.nadobro.engine.adapter.base import OrderState
from src.nadobro.engine.adapter.nado import AdapterError, NadoAdapter, ProductMeta
from src.nadobro.engine.types import OrderType, TradeType

PAIR = "KBTC-USDC"
META = {PAIR: ProductMeta(product_id=2, tick_size=Decimal("0.01"),
                          lot_size=Decimal("0.001"), min_notional=Decimal(1))}


class _FakeClient:
    def __init__(self):
        self.open_orders = []
        self.matches = []
        self.cancelled = []

    def place_limit_order(self, product_id, size, price, is_buy=True, post_only=False, reduce_only=False, **kwargs):
        return {"digest": "d1", "status": "open"}

    def place_market_order(self, product_id, size, is_buy=True, reduce_only=False, **kwargs):
        return {"digest": "m1", "status": "filled", "price": 100}

    def get_open_orders(self, product_id, refresh=False, sender=None):
        return list(self.open_orders)

    async def get_matches(self, *, product_ids=None, limit=200, idx=None, max_time=None):
        return list(self.matches)

    async def cancel_orders(self, *, product_id, digests):
        self.cancelled.extend(digests)
        return {"status": "ok"}

    def get_market_price(self, product_id):
        return {"bid": 99.0, "ask": 101.0}


def _adapter():
    return NadoAdapter(_FakeClient(), META)


class _CapturingClient(_FakeClient):
    """Captures the isolated-margin kwargs the adapter forwards."""

    def __init__(self):
        super().__init__()
        self.market_calls = []

    def place_market_order(self, product_id, size, is_buy=True, **kwargs):
        self.market_calls.append({"product_id": product_id, "size": size,
                                  "is_buy": is_buy, **kwargs})
        return {"digest": "m1", "status": "filled", "price": 100,
                "filled_base": str(size), "filled_quote": str(size * 100)}


def test_isolated_perp_order_posts_isolated_margin():
    """An order on an isolated-only perp must carry isolated_only=True and a
    safety-buffered isolated_margin (= notional * 1.20 at 1x). RWA perps on Nado
    testnet reject otherwise (error_code 2006). This is the fix that unblocks the
    Delta Neutral short leg."""
    perp_pair = "QQQ-PERP"
    meta = {perp_pair: ProductMeta(product_id=7, tick_size=Decimal("0.01"),
                                   lot_size=Decimal("0.001"), min_notional=Decimal(1),
                                   is_perp=True, isolated_only=True)}

    async def body():
        client = _CapturingClient()
        a = NadoAdapter(client, meta)
        # SELL 0.5 base; ref price falls back to mid = (99+101)/2 = 100.
        await a.place_order(perp_pair, TradeType.SELL, OrderType.MARKET, Decimal("0.5"))
        assert len(client.market_calls) == 1
        call = client.market_calls[0]
        assert call["isolated_only"] is True
        # 0.5 * 100 / 1 * 1.20 = 60.0
        assert abs(float(call["isolated_margin"]) - 60.0) < 1e-6

    asyncio.run(body())


def test_funding_since_sums_received_positive():
    """funding_since returns net funding RECEIVED (positive), filtered to the
    product and timestamp window. The indexer signs funding positive = paid, so
    a row of -2.0 paid (i.e. received) becomes +2.0 received."""
    perp_pair = "QQQ-PERP"
    meta = {perp_pair: ProductMeta(product_id=7, tick_size=Decimal("0.01"),
                                   lot_size=Decimal("0.001"), min_notional=Decimal(1),
                                   is_perp=True, isolated_only=True)}

    class _FundingClient(_FakeClient):
        async def get_interest_and_funding_payments(self, *, product_ids=None, limit=200, idx=None):
            return [
                # received 2.0 (paid = -2.0), in-window, this product
                {"type": "funding", "product_id": 7, "amount": "-2.0", "timestamp": 2000},
                # paid 0.5 (cost), in-window
                {"type": "funding", "product_id": 7, "amount": "0.5", "timestamp": 2500},
                # before since_ts → excluded
                {"type": "funding", "product_id": 7, "amount": "-9.0", "timestamp": 500},
                # different product → excluded
                {"type": "funding", "product_id": 99, "amount": "-9.0", "timestamp": 3000},
                # interest, not funding → excluded
                {"type": "interest", "product_id": 7, "amount": "-9.0", "timestamp": 3000},
            ]

    async def body():
        a = NadoAdapter(_FundingClient(), meta)
        net = await a.funding_since(perp_pair, since_ts=1000)
        # received 2.0 - paid 0.5 = net received 1.5
        assert net == Decimal("1.5")

    asyncio.run(body())


def test_funding_since_excludes_undated_rows():
    """DN-FUNDING-WINDOW fix: a funding row with no parseable timestamp must NOT
    be summed (it used to leak in regardless of the run window, overstating
    funding earned). Only the in-window, dated row counts here."""
    perp_pair = "QQQ-PERP"
    meta = {perp_pair: ProductMeta(product_id=7, tick_size=Decimal("0.01"),
                                   lot_size=Decimal("0.001"), min_notional=Decimal(1),
                                   is_perp=True, isolated_only=True)}

    class _FundingClient(_FakeClient):
        async def get_interest_and_funding_payments(self, *, product_ids=None, limit=200, idx=None):
            return [
                {"type": "funding", "product_id": 7, "amount": "-2.0", "timestamp": 2000},
                # no timestamp / paid_at → undated → must be excluded
                {"type": "funding", "product_id": 7, "amount": "-50.0"},
            ]

    async def body():
        a = NadoAdapter(_FundingClient(), meta)
        net = await a.funding_since(perp_pair, since_ts=1000)
        assert net == Decimal("2.0")  # the undated -50 is NOT leaked in

    asyncio.run(body())


def test_reduce_only_stripped_on_spot_orders():
    """reduce_only is a perp concept; Nado rejects a reduce-only SPOT order with
    error_code 5000 'Invalid value' (this broke the DN spot leg's close). The
    adapter must strip it for non-perp products and keep it for perps."""
    spot = {"S": ProductMeta(2, Decimal("0.01"), Decimal("0.001"), Decimal(1),
                             is_perp=False, isolated_only=False)}
    perp = {"P": ProductMeta(7, Decimal("0.01"), Decimal("0.001"), Decimal(1),
                             is_perp=True, isolated_only=True)}

    async def body():
        c1 = _CapturingClient()
        await NadoAdapter(c1, spot).place_order(
            "S", TradeType.SELL, OrderType.MARKET, Decimal("0.5"), reduce_only=True)
        assert c1.market_calls[0]["reduce_only"] is False  # stripped for spot

        c2 = _CapturingClient()
        await NadoAdapter(c2, perp).place_order(
            "P", TradeType.SELL, OrderType.MARKET, Decimal("0.5"), reduce_only=True)
        assert c2.market_calls[0]["reduce_only"] is True   # kept for perp

    asyncio.run(body())


def test_spot_order_is_not_isolated():
    """A spot (or cross) product must NOT post isolated margin — that path is
    unchanged."""
    async def body():
        client = _CapturingClient()
        a = NadoAdapter(client, META)  # META[PAIR] is not isolated
        await a.place_order(PAIR, TradeType.BUY, OrderType.MARKET, Decimal("0.5"))
        call = client.market_calls[0]
        assert call["isolated_only"] is False
        assert call["isolated_margin"] is None

    asyncio.run(body())


def test_place_registers_order_and_market_fills():
    async def body():
        a = _adapter()
        # BUG-NA-2: venue may report "filled" without inline fill data.
        # The adapter must follow up with a matches query, NOT synthesize.
        a._client.matches = [{"digest": "m1", "amount": 1, "price": 100, "fee": "0.05"}]
        o = await a.place_order(PAIR, TradeType.BUY, OrderType.MARKET, Decimal(1))
        assert o.id == "m1" and o.state is OrderState.FILLED
        assert o.filled_base == Decimal(1)
        assert o.filled_quote == Decimal(100)
        assert o.fee_quote == Decimal("0.05")
        assert o.id in a._orders

    asyncio.run(body())


def test_market_fill_converts_x18_match_amounts_to_human():
    """The Nado indexer returns match fills x18-scaled (base_filled / quote_filled).
    The adapter MUST convert to human units — reading them raw recorded a
    0.128-SPY fill as 128000000000000000, which made the DN short un-placeable
    (base-matched off an x18 fill) and the long un-closeable (sold 1e18× the
    held size → venue 'Invalid value'). Regression for that root cause.
    """
    async def body():
        a = _adapter()
        # 0.128 base, 98.13 quote, 0.05 fee — all x18-scaled as the indexer sends.
        a._client.matches = [{
            "digest": "m1",
            "base_filled": "128000000000000000",       # 0.128
            "quote_filled": "98130000000000000000",     # 98.13
            "fee": "50000000000000000",                 # 0.05
        }]
        o = await a.place_order(PAIR, TradeType.BUY, OrderType.MARKET, Decimal("0.128"))
        assert o.state is OrderState.FILLED
        assert o.filled_base == Decimal("0.128")        # NOT 1.28e17
        assert o.filled_quote == Decimal("98.13")
        assert o.fee_quote == Decimal("0.05")

    asyncio.run(body())


def test_place_market_with_no_inline_or_match_data_downgrades_to_partial():
    """BUG-NA-2 fix: when venue reports FILLED but no match data exists yet,
    the adapter must NOT mark FILLED with synthesized base/quote. It must
    downgrade to PARTIALLY_FILLED with zeros so the executor keeps polling.
    """
    async def body():
        a = _adapter()
        a._client.matches = []  # archive hasn't indexed the fill yet
        o = await a.place_order(PAIR, TradeType.BUY, OrderType.MARKET, Decimal(1))
        assert o.state is OrderState.PARTIALLY_FILLED
        assert o.filled_base == Decimal(0)
        assert o.filled_quote == Decimal(0)

    asyncio.run(body())


def test_order_status_open_when_resting():
    async def body():
        a = _adapter()
        o = await a.place_order(PAIR, TradeType.BUY, OrderType.LIMIT_MAKER, Decimal(1), Decimal(99))
        a._client.open_orders = [{"digest": "d1", "filled": 0}]
        st = await a.order_status(o.id)
        assert st.id == o.id
        assert st.state is OrderState.OPEN

    asyncio.run(body())


def test_order_status_filled_when_gone_and_matched():
    async def body():
        a = _adapter()
        o = await a.place_order(PAIR, TradeType.BUY, OrderType.LIMIT_MAKER, Decimal(1), Decimal(99))
        a._client.open_orders = []  # no longer resting
        a._client.matches = [{"digest": "d1", "amount": 1, "price": 99, "fee": "0.1"}]
        st = await a.order_status(o.id)
        assert st.id == o.id
        assert st.state is OrderState.FILLED
        assert st.filled_base == Decimal(1) and st.filled_quote == Decimal(99)
        assert st.fee_quote == Decimal("0.1")

    asyncio.run(body())


def test_order_status_cancelled_with_fills_when_gone_and_partially_matched():
    async def body():
        a = _adapter()
        o = await a.place_order(PAIR, TradeType.BUY, OrderType.LIMIT_MAKER, Decimal(1), Decimal(99))
        a._client.open_orders = []  # terminal: remainder is no longer resting
        a._client.matches = [{"digest": "d1", "amount": "0.4", "price": 99, "fee": "0.04"}]
        st = await a.order_status(o.id)
        assert st.id == o.id
        assert st.state is OrderState.CANCELLED
        assert st.filled_base == Decimal("0.4")
        assert st.filled_quote == Decimal("39.6")
        assert st.fee_quote == Decimal("0.04")

    asyncio.run(body())


def test_order_status_cancelled_when_gone_and_no_fills():
    async def body():
        a = _adapter()
        o = await a.place_order(PAIR, TradeType.BUY, OrderType.LIMIT_MAKER, Decimal(1), Decimal(99))
        a._client.open_orders = []
        a._client.matches = []
        st = await a.order_status(o.id)
        assert st.id == o.id
        assert st.state is OrderState.CANCELLED

    asyncio.run(body())


def test_cancel_uses_registry_product_and_is_idempotent():
    async def body():
        a = _adapter()
        o = await a.place_order(PAIR, TradeType.BUY, OrderType.LIMIT_MAKER, Decimal(1), Decimal(99))
        assert await a.cancel_order(o.id) is True
        assert "d1" in a._client.cancelled
        assert await a.cancel_order("unknown") is False  # idempotent

    asyncio.run(body())


def test_mid_price_from_market_price():
    async def body():
        a = _adapter()
        assert await a.mid_price(PAIR) == Decimal(100)

    asyncio.run(body())


# ── SPOT-CLOSE-BUMP (reported 2026-07-28) ───────────────────────
# "Whether DN or Vol bot, the bot is having issues selling Spot balances."
#
# NadoClient.place_order's min-notional retry did, for EVERY rejected order:
#     retry_size = max(size, target_size)   # side-blind, reduce_only-blind
# KBTC's min notional is $100 (min_size_x18 = 100e18) and its size increment is
# 0.00005. A ~$99 DN / Vol leg therefore lands just UNDER the floor, so a close
# sized to exactly the held base (0.00155) was grown to 0.0016 — MORE than the
# balance. It could not fill, and the spot leg was left naked. On a perp the same
# growth pushes a close past the position size and flips the side outright.
#
# reduce_only cannot carry "this is an exit" to the client, because the adapter
# must STRIP it for spot (the venue rejects reduce-only spot with error_code
# 5000 — see test_reduce_only_stripped_on_spot_orders). Hence `never_grow`.

def test_never_grow_is_set_for_a_spot_close_even_though_reduce_only_is_stripped():
    spot = {"S": ProductMeta(2, Decimal("0.01"), Decimal("0.001"), Decimal(1),
                             is_perp=False, isolated_only=False)}

    async def body():
        c = _CapturingClient()
        await NadoAdapter(c, spot).place_order(
            "S", TradeType.SELL, OrderType.MARKET, Decimal("0.00155"),
            reduce_only=True)
        call = c.market_calls[0]
        assert call["reduce_only"] is False, "still stripped for spot (venue 5000)"
        assert call.get("never_grow") is True, (
            "the reducing intent was lost — the client may grow this close above "
            "the held balance and strand the spot leg"
        )

    asyncio.run(body())


def test_never_grow_is_set_for_a_perp_close():
    perp = {"P": ProductMeta(7, Decimal("0.01"), Decimal("0.001"), Decimal(1),
                             is_perp=True, isolated_only=True)}

    async def body():
        c = _CapturingClient()
        await NadoAdapter(c, perp).place_order(
            "P", TradeType.SELL, OrderType.MARKET, Decimal("0.5"), reduce_only=True)
        assert c.market_calls[0].get("never_grow") is True, (
            "growing a perp close past the position size FLIPS the side"
        )

    asyncio.run(body())


def test_an_opening_order_may_still_be_bumped():
    """Opens must keep the min-notional bump — that is how a sub-minimum entry
    gets to the venue floor at all."""
    spot = {"S": ProductMeta(2, Decimal("0.01"), Decimal("0.001"), Decimal(1),
                             is_perp=False, isolated_only=False)}

    async def body():
        c = _CapturingClient()
        await NadoAdapter(c, spot).place_order(
            "S", TradeType.BUY, OrderType.MARKET, Decimal("0.00155"))
        assert c.market_calls[0].get("never_grow") is False

    asyncio.run(body())


def test_client_refuses_to_bump_a_never_grow_order():
    """The guard itself: a min-notional reject on a reducing order must NOT retry
    with a larger size — it must surface as a blocked close."""
    import inspect
    from src.nadobro.venue.nado_client import NadoClient

    src = inspect.getsource(NadoClient.place_order)
    # The bump branch must be gated on never_grow.
    assert "not never_grow and self._is_min_notional_error" in src, (
        "the min-notional bump is no longer gated on never_grow"
    )
    assert "min_notional_block" in src
    for m in ("place_order", "place_market_order", "place_limit_order"):
        assert "never_grow" in inspect.signature(getattr(NadoClient, m)).parameters, m


# ── SPOT-EXIT-GUARANTEE (pieces 1 + 2b) ─────────────────────────
# Engine inventory is not venue truth: in the 2026-07-28 DN incident the
# controller's hold said 0.00155 while the venue had filled 0.0031, so a size
# taken from the book can be too HIGH (rejected: insufficient balance) as easily
# as too low. And kBTC's $100 min notional sits ABOVE a $99 leg, so the exit
# cannot fill as a resting limit at any size — market orders are exempt (the DN
# $98.88 market sell did fill).

class _BalanceClient(_CapturingClient):
    """A venue with a known spot balance, and limit-call capture."""

    def __init__(self, balances):
        super().__init__()
        self._balances = balances
        self.limit_calls = []

    def get_balance(self, *a, **k):
        return {"exists": True, "balances": self._balances}

    def place_limit_order(self, product_id, size, price, is_buy=True, **kwargs):
        self.limit_calls.append({"product_id": product_id, "size": size,
                                 "price": price, "is_buy": is_buy, **kwargs})
        return {"digest": "l1", "status": "open"}


_SPOT = {"S": ProductMeta(1, Decimal("1"), Decimal("0.00005"), Decimal(100),
                          is_perp=False, isolated_only=False)}


def test_held_base_reads_the_spot_balance_by_product_id():
    async def body():
        c = _BalanceClient({0: 164.48, 1: 0.00155})
        assert await NadoAdapter(c, _SPOT).held_base("S") == Decimal("0.00155")
    asyncio.run(body())


def test_held_base_is_none_when_the_venue_cannot_be_read():
    """None, not 0 — a failed read must not be mistaken for 'flat'."""
    class _Broken(_CapturingClient):
        def get_balance(self, *a, **k):
            raise RuntimeError("gateway down")

    async def body():
        assert await NadoAdapter(_Broken(), _SPOT).held_base("S") is None
    asyncio.run(body())


def test_a_spot_close_is_clamped_down_to_the_venue_balance():
    """Session 167's shape: the book asks for 0.0031, the venue holds 0.00155."""
    async def body():
        c = _BalanceClient({1: 0.00155})
        await NadoAdapter(c, _SPOT).place_order(
            "S", TradeType.SELL, OrderType.MARKET, Decimal("0.0031"),
            reduce_only=True)
        sold = float(c.market_calls[0]["size"])
        assert sold <= 0.00155 + 1e-12, (
            f"asked the venue to sell {sold} of a 0.00155 balance — this is the "
            f"insufficient-balance rejection that strands the leg"
        )
        assert sold == 0.00155, "must sell the WHOLE balance, not a fraction"
    asyncio.run(body())


def test_the_clamp_floors_to_the_lot_size():
    async def body():
        c = _BalanceClient({1: 0.0015712345})
        await NadoAdapter(c, _SPOT).place_order(
            "S", TradeType.SELL, OrderType.MARKET, Decimal("1"), reduce_only=True)
        sold = float(c.market_calls[0]["size"])
        assert abs(sold - 0.00155) < 1e-12, f"lot-floored to 0.00005: got {sold}"
    asyncio.run(body())


def test_a_sub_minimum_spot_exit_crosses_instead_of_resting():
    """0.00155 x 63890 = $99.03 < the $100 minimum: a resting limit can NEVER
    fill, so the exit must cross rather than leave the leg naked."""
    async def body():
        c = _BalanceClient({1: 0.00155})
        await NadoAdapter(c, _SPOT).place_order(
            "S", TradeType.SELL, OrderType.LIMIT, Decimal("0.00155"),
            price=Decimal("63890"), reduce_only=True)
        assert c.market_calls and not c.limit_calls, (
            "the exit stayed a resting limit under the venue minimum — unfillable"
        )
    asyncio.run(body())


def test_an_exit_above_the_minimum_stays_a_limit():
    """Maker-first is the standing rule; only sub-minimum exits may cross."""
    async def body():
        c = _BalanceClient({1: 0.01})
        await NadoAdapter(c, _SPOT).place_order(
            "S", TradeType.SELL, OrderType.LIMIT, Decimal("0.01"),
            price=Decimal("63890"), reduce_only=True)
        assert c.limit_calls and not c.market_calls
    asyncio.run(body())


def test_a_zero_balance_close_raises_rather_than_sending_a_doomed_order():
    async def body():
        c = _BalanceClient({1: 0.0})
        try:
            await NadoAdapter(c, _SPOT).place_order(
                "S", TradeType.SELL, OrderType.MARKET, Decimal("0.00155"),
                reduce_only=True)
        except AdapterError:
            return
        raise AssertionError("a close against a zero balance must not be sent")
    asyncio.run(body())


def test_an_unreadable_balance_does_not_block_the_close():
    """Fail OPEN on the clamp: if we cannot read the balance we still try to
    exit (never_grow already stops the size being inflated)."""
    class _Broken(_BalanceClient):
        def get_balance(self, *a, **k):
            raise RuntimeError("down")

    async def body():
        c = _Broken({})
        await NadoAdapter(c, _SPOT).place_order(
            "S", TradeType.SELL, OrderType.MARKET, Decimal("0.00155"),
            reduce_only=True)
        assert c.market_calls, "an unreadable balance must not veto the exit"
    asyncio.run(body())


def test_a_spot_BUY_is_never_clamped():
    """The clamp is exit-only; an entry has no balance to be limited by."""
    async def body():
        c = _BalanceClient({1: 0.0})
        await NadoAdapter(c, _SPOT).place_order(
            "S", TradeType.BUY, OrderType.MARKET, Decimal("0.00155"))
        assert float(c.market_calls[0]["size"]) == 0.00155
    asyncio.run(body())


# ── held_base perp sign, against the REAL wrapper row shape ─────
# Audit round 4 flagged this path as completely uncovered, and a sign error here
# makes DN pick side=SELL to "close" a short — DOUBLING it. Grounded in the actual
# Nado SDK: PerpBalance.amount is SIGNED (negative = short, x18). nado_client's
# wrapper then normalizes that into a dict with "amount" = ABSOLUTE magnitude and
# "signed_amount" = the signed value (nado_client.py:1784-1797), so held_base must
# read the signed key — reading "amount" is what inverted the sweep side.

class _PositionsClient(_FakeClient):
    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    def get_all_positions(self):
        return self.rows


_PERP_META = {"P": ProductMeta(2, Decimal("1"), Decimal("0.00001"), Decimal(10),
                              is_perp=True, isolated_only=False)}


def _wrapper_row(signed):
    """A row shaped exactly as NadoClient.get_all_positions emits."""
    return {"product_id": 2, "product_name": "BTC-PERP",
            "amount": abs(signed), "signed_amount": signed,
            "price": 63890.0, "side": "LONG" if signed >= 0 else "SHORT"}


def test_held_base_reports_a_perp_short_as_NEGATIVE():
    async def body():
        got = await NadoAdapter(_PositionsClient([_wrapper_row(-0.00155)]),
                                _PERP_META).held_base("P")
        assert got < 0, (
            f"a SHORT read as {got}: the DN sweep derives its SIDE from this sign, "
            f"so a positive value makes it SELL MORE to 'close' a short"
        )
        assert abs(float(got) + 0.00155) < 1e-12
    asyncio.run(body())


def test_held_base_reports_a_perp_long_as_POSITIVE():
    async def body():
        got = await NadoAdapter(_PositionsClient([_wrapper_row(0.00155)]),
                                _PERP_META).held_base("P")
        assert got > 0 and abs(float(got) - 0.00155) < 1e-12
    asyncio.run(body())


def test_held_base_derives_the_sign_from_side_when_only_a_magnitude_is_present():
    """Older/partial rows carry an unsigned amount plus a side."""
    async def body():
        legacy = {"product_id": 2, "amount": 0.00155, "side": "SHORT", "price": 63890.0}
        got = await NadoAdapter(_PositionsClient([legacy]), _PERP_META).held_base("P")
        assert got is not None and got < 0
    asyncio.run(body())


def test_held_base_refuses_to_GUESS_an_unsigned_perp_row():
    """No signed value and no usable side => UNKNOWN (None), never a guess. A wrong
    guess here doubles a position; None makes the caller fall back safely."""
    async def body():
        blind = {"product_id": 2, "amount": 0.00155, "price": 63890.0}
        got = await NadoAdapter(_PositionsClient([blind]), _PERP_META).held_base("P")
        assert got is None
    asyncio.run(body())


def test_held_base_returns_zero_for_a_genuinely_flat_perp():
    """Flat must be 0, not None — otherwise the DN guard can never conclude flat."""
    async def body():
        got = await NadoAdapter(_PositionsClient([]), _PERP_META).held_base("P")
        assert got == Decimal(0)
    asyncio.run(body())


# ── audit round 4: the PERP exit had no escape, and an absent key read as flat ──

def test_a_sub_minimum_PERP_close_crosses_instead_of_being_refused():
    """The spot path got a MARKET fallback; the perp path did not — so a
    sub-minimum reduce-only perp close was refused client-side, retried 3x and
    terminated the executor FAILED with the position still OPEN."""
    async def body():
        c = _BalanceClient({})
        perp = {"P": ProductMeta(2, Decimal("1"), Decimal("0.00001"), Decimal(100),
                                 is_perp=True, isolated_only=False)}
        await NadoAdapter(c, perp).place_order(
            "P", TradeType.BUY, OrderType.LIMIT, Decimal("0.00155"),
            price=Decimal("63890"), reduce_only=True)          # $99 < $100 min
        assert c.market_calls and not c.limit_calls, (
            "the perp exit stayed a resting limit below the venue minimum — it can "
            "never fill, so the position is stranded"
        )
    asyncio.run(body())


def test_a_perp_close_above_the_minimum_stays_a_limit():
    async def body():
        c = _BalanceClient({})
        perp = {"P": ProductMeta(2, Decimal("1"), Decimal("0.00001"), Decimal(100),
                                 is_perp=True, isolated_only=False)}
        await NadoAdapter(c, perp).place_order(
            "P", TradeType.BUY, OrderType.LIMIT, Decimal("0.01"),
            price=Decimal("63890"), reduce_only=True)
        assert c.limit_calls and not c.market_calls
    asyncio.run(body())


def test_an_OPENING_perp_order_below_the_minimum_is_left_alone():
    """The escape is exit-only — an open must still go to the client, which bumps
    it to the floor."""
    async def body():
        c = _BalanceClient({})
        perp = {"P": ProductMeta(2, Decimal("1"), Decimal("0.00001"), Decimal(100),
                                 is_perp=True, isolated_only=False)}
        await NadoAdapter(c, perp).place_order(
            "P", TradeType.SELL, OrderType.LIMIT, Decimal("0.00155"),
            price=Decimal("63890"))
        assert c.limit_calls and not c.market_calls
    asyncio.run(body())


def test_a_product_absent_from_the_balance_snapshot_is_UNKNOWN_not_flat():
    """get_balance returns an entry for every product it saw — including explicit
    0.0 — so a MISSING product means the snapshot cannot answer. Returning 0 made
    the exit clamp raise 'balance is 0 — nothing to sell' and refuse a real close."""
    async def body():
        c = _BalanceClient({0: 164.48, 3: 0.0})        # product 1 absent
        got = await NadoAdapter(c, _SPOT).held_base("S")
        assert got is None, f"absent product read as {got} — that blocks the exit"
    asyncio.run(body())


def test_a_present_zero_balance_IS_flat():
    """Only an explicit zero for a PRESENT key justifies concluding flat."""
    async def body():
        c = _BalanceClient({1: 0.0})
        assert await NadoAdapter(c, _SPOT).held_base("S") == Decimal(0)
    asyncio.run(body())


# --- sized depth ------------------------------------------------------------
# ``order_book`` is the top-of-book hot path behind every ``mid_price`` call and
# deliberately carries no sizes. ``depth_book`` is the separate, lower-cadence
# read that structure/microstructure work needs.

class _DepthClient(_FakeClient):
    def __init__(self, book=None, raises=False):
        super().__init__()
        self._book = book
        self._raises = raises
        self.calls = []

    def get_market_liquidity(self, product_id, depth):
        self.calls.append((product_id, depth))
        if self._raises:
            raise RuntimeError("engine unavailable")
        return self._book


def test_depth_book_carries_real_sizes():
    async def body():
        client = _DepthClient({
            "bids": [[99.0, 2.5], [98.0, 4.0]],
            "asks": [[101.0, 1.5]],
            "timestamp": 123.0,
        })
        book = await NadoAdapter(client, META).depth_book(PAIR, depth=5)
        assert [(l.price, l.amount) for l in book.bids] == [
            (Decimal("99.0"), Decimal("2.5")), (Decimal("98.0"), Decimal("4.0")),
        ]
        assert [(l.price, l.amount) for l in book.asks] == [(Decimal("101.0"), Decimal("1.5"))]
        assert client.calls == [(2, 5)]
    asyncio.run(body())


def test_order_book_stays_top_of_book_only():
    """The hot path must not start paying for the full ladder."""
    async def body():
        client = _DepthClient({"bids": [[99.0, 2.5]], "asks": [[101.0, 1.5]]})
        book = await NadoAdapter(client, META).order_book(PAIR)
        assert len(book.bids) == 1 and book.bids[0].amount == Decimal(0)
        assert client.calls == [], "order_book must not query market liquidity"
    asyncio.run(body())


def test_depth_book_degrades_to_empty_rather_than_raising():
    """Depth is an enrichment — a venue blip must not break a controller tick."""
    async def body():
        book = await NadoAdapter(_DepthClient(raises=True), META).depth_book(PAIR)
        assert book.bids == [] and book.asks == []
    asyncio.run(body())


def test_depth_book_drops_unusable_levels():
    async def body():
        client = _DepthClient({
            "bids": [[99.0, 2.5], [0, 1.0], [98.0, 0], ["junk", 1.0], [97.0]],
            "asks": None,
        })
        book = await NadoAdapter(client, META).depth_book(PAIR)
        assert [l.price for l in book.bids] == [Decimal("99.0")]
        assert book.asks == []
    asyncio.run(body())
