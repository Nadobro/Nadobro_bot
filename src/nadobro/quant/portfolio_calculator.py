from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from src.nadobro.utils.env import env_float
from src.nadobro.utils.x18 import from_x18


ZERO = Decimal("0")
# USDT0 — the USD quote product. Every other spot product in
# ``get_balance().balances`` is a TOKEN amount and must not be summed as USD.
USD_QUOTE_PRODUCT_ID = 0


@dataclass(frozen=True)
class PortfolioPosition:
    product_id: int
    symbol: str
    isolated: bool
    is_long: bool
    amount: Decimal
    notional_value: Decimal
    avg_entry_price: Decimal | None
    est_liq_price: Decimal | None
    est_pnl: Decimal | None
    margin_used: Decimal | None
    leverage: Decimal | None
    initial_health: Decimal | None
    maintenance_health: Decimal | None
    upnl_pct: Decimal | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def decimal_value(value: Any, default: Decimal = ZERO) -> Decimal:
    if value is None or value == "":
        return default
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def optional_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return decimal_value(value)


def _pick(row: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return default


def _position_amount(row: dict[str, Any]) -> Decimal:
    raw_x18 = _pick(row, "amount_x18")
    if raw_x18 is not None:
        return from_x18(raw_x18)
    return decimal_value(_pick(row, "position_size", "amount", "signed_amount", default=0))


def signed_position_size(row: dict[str, Any]) -> Decimal:
    """Signed base size for a position row: +long / -short."""
    explicit = optional_decimal(_pick(row, "signed_amount", "size_signed"))
    if explicit is not None and explicit != ZERO:
        return explicit
    size = abs(decimal_value(_pick(row, "amount", "size", "position_size", default=0)))
    side = str(_pick(row, "side", default="")).strip().lower()
    if side in ("short", "sell"):
        return -size
    if side in ("long", "buy"):
        return size
    # No side field: position_size is already signed on the venue payloads.
    return decimal_value(_pick(row, "position_size", "amount", "size", default=0))


def derive_unrealized_pnl(
    row: dict[str, Any], *, mark_price: Any = None
) -> Decimal | None:
    """Unrealized PnL for a position row, DERIVED when the venue omits it.

    Nado's ``IsolatedPositionMetrics`` carries no ``est_pnl`` and no
    ``avg_entry_price`` (nado_protocol/utils/margin_manager.py:97-110) — only
    cross positions get them. Every isolated position therefore reported uPnL
    ``None`` -> 0.0, and Delta Neutral + copy trading both run isolated, so the
    session SL/TP rail (live_session -> _evaluate_session_pnl_rail) could not
    see an open position's loss at all.

    Uses the venue's own identity (``calculate_perp_balance_value``,
    margin_manager.py:352-374)::

        uPnL = signed_size * mark + v_quote_balance

    and falls back to ``(mark - entry) * signed_size``. Returns ``None`` when
    neither can be evaluated, so callers can tell "no data" from "zero PnL".
    """
    explicit = optional_decimal(_pick(row, "est_pnl", "unrealized_pnl", "unrealized_pnl_usd"))
    _unsettled = optional_decimal(_pick(row, "unsettled"))
    if explicit is not None:
        # Prefer the on-chain ``unsettled`` when the indexer est_pnl diverges from
        # it (corrupt basis; see reconcile_unrealized_pnl / session 312).
        return reconcile_unrealized_pnl(explicit, _unsettled, optional_decimal(_pick(row, "notional_value", "value")))
    if _unsettled is not None:
        return _unsettled

    signed = signed_position_size(row)
    if signed == ZERO:
        return ZERO

    mark = optional_decimal(mark_price)
    if mark is None:
        mark = optional_decimal(_pick(row, "mark_price", "mark"))
    if mark is None:
        # The venue prices isolated notional at the oracle:
        # notional_value = |size| * oracle_price (margin_manager.py:362-364).
        notional = optional_decimal(_pick(row, "notional_value", "value"))
        if notional is not None and signed != ZERO:
            mark = abs(notional) / abs(signed)
    if mark is None or mark <= ZERO:
        return None

    v_quote = optional_decimal(_pick(row, "v_quote_balance", "v_quote", "unsettled"))
    if v_quote is not None:
        return signed * mark + v_quote

    entry = optional_decimal(_pick(row, "avg_entry_price", "entry_price"))
    if entry is not None and entry > ZERO:
        return (mark - entry) * signed
    return None


_UPNL_RECONCILE_FRACTION = env_float("NADO_UPNL_RECONCILE_NOTIONAL_FRACTION", 0.10)
_UPNL_RECONCILE_FLOOR = env_float("NADO_UPNL_RECONCILE_ABS_FLOOR", 10.0)


def reconcile_unrealized_pnl(
    est_pnl: "Decimal | None", unsettled: "Decimal | None", notional: "Decimal | None"
) -> "Decimal | None":
    """The trustworthy unrealized PnL for a cross position (prod 2026-09-03,
    session 312). A cross position carries TWO uPnL figures: ``est_pnl``
    (indexer: amount*oracle - net_entry_unrealized) and ``unsettled`` (on-chain:
    amount*oracle + v_quote_balance). They differ by EXACTLY the accumulated
    fees + settled funding (est_pnl is price-only; unsettled carries funding via
    v_quote_balance), which over even a long high-turnover session stays a few
    percent of notional. When the indexer's net_entry_unrealized is corrupt they
    diverge FAR beyond that (312: est_pnl +$295.65 from a fabricated avg_entry
    105,635 vs mark 78,260 — 35% of notional — while unsettled was ~-$6). The band
    (default 10% of notional, well above any real fees+funding) separates the
    two: below it, keep the funding-separated ``est_pnl`` the rail's
    "price - fees - funding" basis wants (so the rail's own ``- funding_paid`` is
    not double-counted); above it, ``est_pnl`` is provably corrupt and the
    on-chain ``unsettled`` is the only trustworthy figure. Leverage-agnostic: it
    never overrides a legitimate PnL, only a corrupt one."""
    if unsettled is None:
        return est_pnl                       # isolated / no on-chain figure
    if est_pnl is None:
        return unsettled
    band = _UPNL_RECONCILE_FLOOR
    if notional is not None:
        band = max(band, abs(notional) * Decimal(str(_UPNL_RECONCILE_FRACTION)))
    if abs(est_pnl - unsettled) > band:
        return unsettled                     # indexer est_pnl is corrupt -> trust the chain
    return est_pnl


def normalize_position(row: dict[str, Any], *, isolated: bool) -> PortfolioPosition:
    amount = _position_amount(row)
    notional = decimal_value(_pick(row, "notional_value", "value", default=0))
    est_pnl = reconcile_unrealized_pnl(
        optional_decimal(_pick(row, "est_pnl", "unrealized_pnl")),
        optional_decimal(_pick(row, "unsettled")),
        optional_decimal(_pick(row, "notional_value", "value")),
    )
    margin_used = optional_decimal(_pick(row, "margin_used", "net_margin"))
    leverage = optional_decimal(_pick(row, "leverage"))
    avg_entry = optional_decimal(_pick(row, "avg_entry_price", "entry_price"))
    liq = optional_decimal(_pick(row, "est_liq_price", "liquidation_price"))
    upnl_pct = unrealized_pnl_pct(
        est_pnl=est_pnl,
        margin_used=margin_used,
        notional_value=notional,
        leverage=leverage,
        isolated=isolated,
    )
    return PortfolioPosition(
        product_id=int(_pick(row, "product_id", default=0) or 0),
        symbol=str(_pick(row, "symbol", "product_name", default="")),
        isolated=bool(isolated),
        is_long=amount >= ZERO,
        amount=amount,
        notional_value=notional,
        avg_entry_price=avg_entry,
        est_liq_price=liq,
        est_pnl=est_pnl,
        margin_used=margin_used,
        leverage=leverage,
        initial_health=optional_decimal(_pick(row, "initial_health")),
        maintenance_health=optional_decimal(_pick(row, "maintenance_health")),
        upnl_pct=upnl_pct,
    )


def positions_from_account_summary(summary: dict[str, Any]) -> list[PortfolioPosition]:
    positions: list[PortfolioPosition] = []
    for row in summary.get("cross_positions") or []:
        positions.append(normalize_position(dict(row), isolated=False))
    for row in summary.get("isolated_positions") or []:
        positions.append(normalize_position(dict(row), isolated=True))
    return positions


def fill_price(base_amount: str | int | Decimal, quote_amount: str | int | Decimal, fee_amount: str | int | Decimal = 0) -> Decimal:
    if isinstance(base_amount, bool) or isinstance(quote_amount, bool) or isinstance(fee_amount, bool):
        raise TypeError("fill_price amounts must be numeric, not bool")
    base = from_x18(base_amount) if isinstance(base_amount, (str, int)) else decimal_value(base_amount)
    quote = from_x18(quote_amount) if isinstance(quote_amount, (str, int)) else decimal_value(quote_amount)
    fee = from_x18(fee_amount) if isinstance(fee_amount, (str, int)) else decimal_value(fee_amount)
    if base == ZERO:
        return ZERO
    return abs(quote + fee) / abs(base)


def funding_payment_amount(payment: dict[str, Any]) -> Decimal:
    return _decimal_from_possible_x18(payment, "amount_x18", "amount")


def funding_payment_label(payment: dict[str, Any]) -> tuple[Decimal, str]:
    amount = funding_payment_amount(payment)
    return abs(amount), "paid" if amount > ZERO else "received"


def funding_rate_conversions(daily_rate: Decimal) -> dict[str, Decimal]:
    daily = decimal_value(daily_rate)
    return {
        "1h": daily / Decimal(24),
        "8h": daily / Decimal(3),
        "1y": daily * Decimal(365),
    }


def unrealized_pnl_pct(
    *,
    est_pnl: Decimal | None,
    margin_used: Decimal | None,
    notional_value: Decimal,
    leverage: Decimal | None,
    isolated: bool,
) -> Decimal | None:
    if est_pnl is None:
        return None
    if isolated:
        denominator = margin_used
    else:
        # Prefer notional/leverage (margin actually committed); the SDK
        # summary often omits ``leverage`` for cross rows, so fall back to
        # margin_used rather than returning None (which renders as a fake
        # 0.00% in the deck).
        if leverage and leverage != ZERO:
            denominator = notional_value / leverage
        elif margin_used and margin_used != ZERO:
            denominator = margin_used
        else:
            denominator = None
    if denominator is None or denominator == ZERO:
        return None
    return (est_pnl / denominator) * Decimal(100)


def account_leverage(notional_values: list[Decimal], unweighted_health: Decimal) -> Decimal:
    health = decimal_value(unweighted_health)
    if health == ZERO:
        return ZERO
    return sum(abs(decimal_value(v)) for v in notional_values) / health


def clamp_margin_usage(value: Decimal) -> Decimal:
    raw = decimal_value(value)
    if raw < ZERO:
        return ZERO
    if raw > Decimal(1):
        return Decimal(1)
    return raw


def _row_time(row: dict[str, Any]) -> datetime | None:
    raw = _pick(row, "filled_at", "paid_at", "timestamp")
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw / 1000 if raw > 10_000_000_000 else raw, tz=timezone.utc)
    text = str(raw)
    if text.isdigit():
        value = int(text)
        return datetime.fromtimestamp(value / 1000 if value > 10_000_000_000 else value, tz=timezone.utc)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def aggregate_trading_stats(
    fills: list[dict[str, Any]],
    funding_payments: list[dict[str, Any]] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    windows = {
        "24h": Decimal(24 * 60 * 60),
        "7d": Decimal(7 * 24 * 60 * 60),
        "30d": Decimal(30 * 24 * 60 * 60),
    }
    keys_with_all = ("24h", "7d", "30d", "all")
    volume_windows = {k: ZERO for k in keys_with_all}
    fees_windows = {k: ZERO for k in keys_with_all}
    pnl_windows = {k: ZERO for k in keys_with_all}
    funding_windows = {k: ZERO for k in keys_with_all}
    wins = 0
    losses = 0
    trade_keys: set[str] = set()
    anonymous_fills = 0

    for fill in fills or []:
        quote = _decimal_from_possible_x18(fill, "quote_filled_x18", "quote_filled")
        fee = abs(_decimal_from_possible_x18(fill, "fee_x18", "fee"))
        pnl = _decimal_from_possible_x18(fill, "realized_pnl_x18", "realized_pnl")
        key = _trade_key(fill)
        if key:
            trade_keys.add(key)
        else:
            anonymous_fills += 1
        volume = abs(quote)
        volume_windows["all"] += volume
        fees_windows["all"] += fee
        pnl_windows["all"] += pnl
        if pnl > ZERO:
            wins += 1
        elif pnl < ZERO:
            losses += 1

        ts = _row_time(fill)
        if ts is None:
            continue
        age = Decimal(max(0, (now - ts.astimezone(timezone.utc)).total_seconds()))
        for win_key, threshold in windows.items():
            if age <= threshold:
                volume_windows[win_key] += volume
                fees_windows[win_key] += fee
                pnl_windows[win_key] += pnl

    for payment in funding_payments or []:
        amount = funding_payment_amount(payment)
        funding_windows["all"] += amount
        ts = _row_time(payment)
        if ts is None:
            continue
        age = Decimal(max(0, (now - ts.astimezone(timezone.utc)).total_seconds()))
        for win_key, threshold in windows.items():
            if age <= threshold:
                funding_windows[win_key] += amount

    decisive = wins + losses
    total_trades = len(trade_keys) + anonymous_fills
    return {
        "volume_windows": volume_windows,
        "fees_windows": fees_windows,
        "pnl_windows": pnl_windows,
        "funding_windows": funding_windows,
        "total_volume": volume_windows["all"],
        "total_fees": fees_windows["all"],
        "total_funding": funding_windows["all"],
        "total_pnl": pnl_windows["all"],
        "wins": wins,
        "losses": losses,
        "total_trades": total_trades,
        "win_rate": (Decimal(wins) / Decimal(decisive) * Decimal(100)) if decisive else ZERO,
    }


_EPOCH0 = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _fill_signed_base_price_ts(
    row: dict[str, Any],
) -> tuple[int, Decimal, Decimal, datetime | None] | None:
    """Normalize a fill/trade row to ``(product_id, signed_base, price, ts)``.

    ``signed_base`` is +base for long/buy, -base for short/sell. ``price`` is the
    per-unit fill price (prefers venue ``quote_filled_x18 / base_filled_x18``,
    falls back to the recorder ``fill_price``/``price``). Returns ``None`` when the
    row has no usable base / price / side."""
    base = abs(_decimal_from_possible_x18(row, "base_filled_x18", "fill_size"))
    if base <= ZERO:
        base = abs(decimal_value(_pick(row, "fill_size", "size", default=0)))
    if base <= ZERO:
        return None
    quote = abs(_decimal_from_possible_x18(row, "quote_filled_x18", "quote_filled"))
    price = (quote / base) if quote > ZERO else decimal_value(_pick(row, "fill_price", "price", default=0))
    if price <= ZERO:
        return None
    side = str(_pick(row, "side", default="")).lower()
    if side in ("long", "buy"):
        signed = base
    elif side in ("short", "sell"):
        signed = -base
    else:
        return None
    pid = int(_pick(row, "product_id", default=0) or 0)
    return pid, signed, price, _row_time(row)


def realized_pnl_windows_from_rows(
    rows: list[dict[str, Any]] | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Position-aware realized PnL over a COMPLETE set of fills.

    This venue reports no per-fill realized PnL (the indexer match has only
    base/quote/fee), so realized PnL MUST be derived: replay fills per product in
    time order, keep a running signed position + average entry cost, and realize
    PnL (gross of fees) on the portion each fill CLOSES — ``(exit - entry) * closed``
    for longs, ``(entry - exit) * closed`` for shorts. A position flip realizes the
    closed leg then opens the remainder at the fill price. Realized PnL is attributed
    to the closing fill's timestamp and bucketed into 24h / 7d / 30d / all windows.

    Accuracy depends on ``rows`` being the FULL per-product history — a truncated
    feed misses earlier entry basis. Callers should pass complete
    ``trades_<network>`` fills (see ``get_account_realized_pnl_windows``).

    Positions are keyed by ``(product_id, isolated)`` — the venue tracks a
    separate position per margin bucket, so an isolated leg (copy / DN) and a
    cross leg (manual / strategy) on the SAME product must NOT be paired against
    each other. Win/loss is counted per ROUND-TRIP (one decision per open→flat
    cycle, bucketed by the closing fill's time), not per reducing fill — so a
    grid/MM position closed in many partial fills is one outcome, not many.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    thresholds = {"24h": Decimal(24 * 3600), "7d": Decimal(7 * 24 * 3600), "30d": Decimal(30 * 24 * 3600)}
    keys = ("24h", "7d", "30d", "all")
    pnl_windows = {k: ZERO for k in keys}
    # Per-window round-trip win/loss counts + the summed win/loss PnL (so callers
    # can derive avg win, avg loss, payoff ratio and expectancy).
    wins_windows = {k: 0 for k in keys}
    losses_windows = {k: 0 for k in keys}
    win_pnl_windows = {k: ZERO for k in keys}
    loss_pnl_windows = {k: ZERO for k in keys}

    def _windows_for(ts: datetime | None) -> list[str]:
        wk = ["all"]
        if ts is not None:
            age = Decimal(max(0, int((now - ts.astimezone(timezone.utc)).total_seconds())))
            wk.extend(k for k, thr in thresholds.items() if age <= thr)
        return wk

    # (product_id, isolated) -> signed fill legs, in chronological order.
    norm: list[tuple[tuple[int, bool], Decimal, Decimal, datetime | None]] = []
    for r in (rows or []):
        c = _fill_signed_base_price_ts(r)
        if c is None:
            continue
        pid, signed_qty, price, ts = c
        norm.append(((pid, bool(r.get("isolated"))), signed_qty, price, ts))
    norm.sort(key=lambda c: c[3] or _EPOCH0)

    state: dict[tuple[int, bool], tuple[Decimal, Decimal]] = {}  # key -> (signed_pos, avg_entry)
    cycle: dict[tuple[int, bool], Decimal] = {}                  # key -> realized in the open cycle

    def _finalize(key: tuple[int, bool], ts: datetime | None) -> None:
        """A round-trip just closed (flat, or the leg before a flip): count ONE
        win/loss on the cycle's net realized, bucketed by the close time."""
        cyc = cycle.get(key, ZERO)
        cycle[key] = ZERO
        if cyc == ZERO:
            return  # scratch (breakeven) round-trip: neither a win nor a loss
        for wk in _windows_for(ts):
            if cyc > ZERO:
                wins_windows[wk] += 1
                win_pnl_windows[wk] += cyc
            else:
                losses_windows[wk] += 1
                loss_pnl_windows[wk] += cyc

    for key, signed_qty, price, ts in norm:
        pos, entry = state.get(key, (ZERO, ZERO))
        realized = ZERO
        reduced = flipped = False
        if pos == ZERO or (pos > ZERO) == (signed_qty > ZERO):
            # Opening or adding in the same direction: update the average entry.
            new_abs = abs(pos) + abs(signed_qty)
            entry = (entry * abs(pos) + price * abs(signed_qty)) / new_abs
            pos = pos + signed_qty
        else:
            # Reducing / closing / flipping: realize PnL on the closed portion.
            reduced = True
            closing = min(abs(pos), abs(signed_qty))
            realized = (price - entry) * closing if pos > ZERO else (entry - price) * closing
            if abs(signed_qty) < abs(pos):
                pos = pos + signed_qty  # move toward flat; entry unchanged
            elif abs(signed_qty) == abs(pos):
                pos, entry = ZERO, ZERO
            else:
                remaining = abs(signed_qty) - abs(pos)  # flip: open the remainder
                pos = remaining if signed_qty > ZERO else -remaining
                entry = price
                flipped = True
        state[key] = (pos, entry)

        # Per-FILL realized bucketing keeps the PnL total exact at fill granularity.
        if realized != ZERO:
            cycle[key] = cycle.get(key, ZERO) + realized
            for wk in _windows_for(ts):
                pnl_windows[wk] += realized

        # Round-trip win/loss: decide once, when the cycle goes flat or flips.
        if reduced and (pos == ZERO or flipped):
            _finalize(key, ts)

    wins = wins_windows["all"]
    losses = losses_windows["all"]
    decisive = wins + losses
    return {
        "pnl_windows": pnl_windows,
        "total_pnl": pnl_windows["all"],
        "wins": wins,
        "losses": losses,
        "win_rate": (Decimal(wins) / Decimal(decisive) * Decimal(100)) if decisive else ZERO,
        "wins_windows": wins_windows,
        "losses_windows": losses_windows,
        "win_pnl_windows": win_pnl_windows,
        "loss_pnl_windows": loss_pnl_windows,
    }


def _fill_fee(row: dict[str, Any]) -> Decimal:
    """Total fee for a fill row (venue ``fee_x18`` preferred, else recorder
    ``fill_fee``/``fees`` + ``builder_fee``)."""
    if row.get("fee_x18") is not None:
        return abs(from_x18(row.get("fee_x18")))
    fee = abs(decimal_value(_pick(row, "fill_fee", "fees", default=0)))
    builder = abs(decimal_value(_pick(row, "builder_fee", default=0)))
    return fee + builder


@dataclass(frozen=True)
class PairedTrade:
    """One position round-trip (flat -> flat, or the leg before a flip), built by
    pairing fills. ``realized_pnl`` is GROSS of fees; ``fees`` is the total paid
    across every fill in the trade; ``net_pnl`` subtracts them."""
    product_id: int
    direction: str            # 'long' or 'short' (the entry direction)
    size: Decimal             # base closed (== entry base for a fully-closed trade)
    entry_price: Decimal      # size-weighted average entry
    exit_price: Decimal | None  # size-weighted average exit (None while open)
    realized_pnl: Decimal     # gross of fees
    fees: Decimal
    opened_at: datetime | None
    closed_at: datetime | None
    closed: bool

    @property
    def net_pnl(self) -> Decimal:
        return self.realized_pnl - self.fees

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["net_pnl"] = self.net_pnl
        return d


class _OpenLeg:
    """Mutable accumulator for the position currently open on one product."""

    __slots__ = ("pid", "direction", "pos", "entry_base", "entry_cost",
                 "exit_base", "exit_proceeds", "realized", "fees", "opened_at", "closed_at")

    def __init__(self, pid, signed_qty, price, fee, ts):
        self.pid = pid
        self.direction = "long" if signed_qty > ZERO else "short"
        self.pos = signed_qty
        self.entry_base = abs(signed_qty)
        self.entry_cost = abs(signed_qty) * price
        self.exit_base = ZERO
        self.exit_proceeds = ZERO
        self.realized = ZERO
        self.fees = fee
        self.opened_at = ts
        self.closed_at = ts

    def _finish(self) -> PairedTrade:
        entry_price = (self.entry_cost / self.entry_base) if self.entry_base > ZERO else ZERO
        exit_price = (self.exit_proceeds / self.exit_base) if self.exit_base > ZERO else None
        return PairedTrade(
            product_id=self.pid, direction=self.direction, size=self.exit_base,
            entry_price=entry_price, exit_price=exit_price, realized_pnl=self.realized,
            fees=self.fees, opened_at=self.opened_at, closed_at=self.closed_at, closed=True,
        )

    def _open_snapshot(self) -> PairedTrade:
        entry_price = (self.entry_cost / self.entry_base) if self.entry_base > ZERO else ZERO
        exit_price = (self.exit_proceeds / self.exit_base) if self.exit_base > ZERO else None
        return PairedTrade(
            product_id=self.pid, direction=self.direction, size=abs(self.pos),
            entry_price=entry_price, exit_price=exit_price, realized_pnl=self.realized,
            fees=self.fees, opened_at=self.opened_at, closed_at=self.closed_at, closed=False,
        )


def pair_fills_into_trades(rows: list[dict[str, Any]] | None) -> list[PairedTrade]:
    """Pair a COMPLETE fill ledger into per-position round-trips.

    Replays fills per product in time order on a running signed position with an
    average entry cost (the venue reports no realized PnL, so it is derived). Each
    time the position returns to flat (or flips) the round-trip is emitted as a
    closed ``PairedTrade`` carrying avg entry, avg exit, realized PnL (gross of
    fees), and the fees paid across the trade. A position still open at the end is
    emitted last with ``closed=False`` (its ``realized_pnl`` is whatever earlier
    partial closes booked; unrealized is left to the live position feed).

    Accuracy depends on ``rows`` being the full per-product history — pass
    venue-confirmed fills (see ``database.get_paired_trades``). Closed trades are
    returned in close-time order; a trailing open trade (if any) comes last."""
    norm = []
    for row in rows or []:
        c = _fill_signed_base_price_ts(row)
        if c is not None:
            pid, signed, price, ts = c
            norm.append((pid, signed, price, _fill_fee(row), ts))
    norm.sort(key=lambda c: c[4] or _EPOCH0)

    open_legs: dict[int, _OpenLeg] = {}
    closed: list[PairedTrade] = []
    for pid, signed_qty, price, fee, ts in norm:
        leg = open_legs.get(pid)
        if leg is None:
            open_legs[pid] = _OpenLeg(pid, signed_qty, price, fee, ts)
            continue
        leg.fees += fee
        leg.closed_at = ts
        if (leg.pos > ZERO) == (signed_qty > ZERO):
            # Same direction: add to the position, re-weighting the avg entry.
            leg.entry_base += abs(signed_qty)
            leg.entry_cost += abs(signed_qty) * price
            leg.pos += signed_qty
            continue
        # Opposite direction: realize PnL on the closed portion.
        closing = min(abs(leg.pos), abs(signed_qty))
        entry_avg = (leg.entry_cost / leg.entry_base) if leg.entry_base > ZERO else ZERO
        leg.realized += (price - entry_avg) * closing if leg.pos > ZERO else (entry_avg - price) * closing
        leg.exit_base += closing
        leg.exit_proceeds += closing * price
        if abs(signed_qty) < abs(leg.pos):
            leg.pos += signed_qty  # partial close; entry avg unchanged
        elif abs(signed_qty) == abs(leg.pos):
            closed.append(leg._finish())
            del open_legs[pid]
        else:
            # Flip: close the current trade fully, open a new one with the remainder.
            closed.append(leg._finish())
            remaining = abs(signed_qty) - abs(leg.pos)
            signed_remaining = remaining if signed_qty > ZERO else -remaining
            open_legs[pid] = _OpenLeg(pid, signed_remaining, price, ZERO, ts)

    result = list(closed)
    result.extend(leg._open_snapshot() for leg in open_legs.values())
    return result


def compute_total_equity(
    summary: dict[str, Any] | None,
    spot_balances: dict[Any, Any] | None = None,
) -> dict[str, Decimal]:
    """Account net worth (Total Balance) plus a NON-overlapping breakdown.

    Nado is a cross-margin venue: your spot balances ARE your margin collateral.
    The old formula ``total = spot + cross + isolated`` therefore counted the
    same dollars two-to-three times — and because none of the equity key names
    it looked for exist on Nado's summary, ``cross`` fell through to
    ``initial_health``, a margin-WEIGHTED health figure that is not equity at
    all. Measured against a live mainnet account (2026-07-26):

        USDC spot deposits  = 2562.70
        unweighted_health   = 2566.74   <- true net worth
        old total           = 6773.44   <- 2.6x overstated

    ``unweighted_health`` is the venue's own net account value (every balance at
    oracle price, no margin weights, position PnL included), so it is the
    authoritative Total Balance. The breakdown is then derived so the parts
    always SUM to the total instead of inflating it:

    - ``spot``      net spot collateral (deposits - borrows).
    - ``positions`` everything the total carries beyond spot: perp position
      equity (entry margin + unrealized) across cross AND isolated.
    - ``isolated``  informational subset of ``positions`` (isolated margin+PnL);
      it is NOT added again into the total.
    """
    summary = summary or {}
    spot_balances = spot_balances or {}

    # ``portfolio_value`` IS the venue's Total Equity:
    #   portfolio_value = unweighted_health + total_iso_net_margin
    # (nado_protocol/utils/margin_manager.py:329, printed as "Total Equity"
    # at :807). unweighted_health alone omits isolated margin, which is
    # segregated collateral the user still owns.
    total: Decimal | None = None
    for key in ("portfolio_value", "account_value", "net_value", "equity"):
        if key in summary and summary[key] not in (None, ""):
            try:
                total = decimal_value(summary[key])
                break
            except Exception:
                continue

    # Net spot collateral in USD. The venue already prices these
    # (amount * oracle_price, margin_manager.py:352-364). The raw
    # ``get_balance().balances`` map is TOKEN AMOUNTS keyed by product_id, so
    # summing it as dollars valued 0.5 spot BTC at $0.50 and counted borrows
    # (negative amounts) as assets via abs(). Only fall back to it when the
    # summary is unavailable, and then only for the USD quote product.
    spot: Decimal | None = None
    if summary.get("total_spot_deposits") not in (None, ""):
        try:
            spot = decimal_value(summary["total_spot_deposits"]) - abs(
                decimal_value(summary.get("total_spot_borrows") or 0)
            )
        except Exception:
            spot = None
    if spot is None:
        spot = ZERO
        for key, val in spot_balances.items():
            if val in (None, ""):
                continue
            try:
                if int(key) != USD_QUOTE_PRODUCT_ID:
                    continue  # non-USD token amount — cannot be summed as USD
                spot += decimal_value(val)   # signed: borrows reduce equity
            except Exception:
                continue

    # Isolated equity = segregated net margin (position PnL is already inside
    # net_margin; IsolatedPositionMetrics carries no separate PnL field).
    isolated = ZERO
    for row in summary.get("isolated_positions") or []:
        if not isinstance(row, dict):
            continue
        margin = optional_decimal(_pick(row, "net_margin", "margin_used")) or ZERO
        isolated += margin

    if total is None:
        # No equity field at all: fall back to the additive estimate, correct
        # when spot and isolated genuinely do not overlap.
        unweighted = optional_decimal(summary.get("unweighted_health"))
        total = (unweighted if unweighted is not None else spot) + isolated

    # Decompose the total without double counting:
    #   total = spot + cross + isolated   (cross = the remainder)
    cross = total - spot - isolated
    return {
        "total": total,
        "spot": spot,
        "cross": cross,
        "isolated": isolated,
    }


def _decimal_from_possible_x18(row: dict[str, Any], x18_key: str, human_key: str) -> Decimal:
    if row.get(x18_key) is not None:
        return from_x18(row.get(x18_key))
    value = row.get(human_key)
    if value is None or value == "":
        return ZERO
    text = str(value)
    if any(ch in text for ch in ".eE"):
        return decimal_value(value)
    try:
        integer = int(text)
    except (TypeError, ValueError):
        return decimal_value(value)
    if abs(integer) >= 1_000_000_000:
        return from_x18(integer)
    return Decimal(integer)


def _trade_key(fill: dict[str, Any]) -> str | None:
    for key in ("submission_idx", "order_digest", "digest", "tx_hash"):
        value = fill.get(key)
        if value is not None and value != "":
            return f"{key}:{value}"
    order = fill.get("order") or {}
    if isinstance(order, dict):
        for key in ("digest", "order_digest"):
            value = order.get(key)
            if value is not None and value != "":
                return f"order.{key}:{value}"
    return None
