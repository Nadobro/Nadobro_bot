#!/usr/bin/env python3
"""OWNER-RUN ONLY. Arcus TESTNET probe for the G1 measurements (02 §11.2, 06 §15.1).

An agent never runs this script. It signs real (testnet) orders with the
owner's probe key, so only the owner runs it, on the owner's own machine.

    cd <repo>
    export ARCUS_PROBE_NETWORK=testnet ARCUS_PROBE_ADDRESS=0x<your address>
    read -rs ARCUS_PROBE_SIGNING_KEY && export ARCUS_PROBE_SIGNING_KEY   # nothing echoes, no history
    .venv/bin/python scripts/arcus_testnet_probe.py <subcommand> [--market BTC-USD] [--n N] [--max N]
        [--out tests/fixtures/arcus/g1] [--far-bp 500] [--i-understand-this-trades] [--allow-fly]

The full owner checklist (key creation, every command, what each report
decides) is ``docs/arcus_g1_probe.md``.

Hard guards (any failure -> exit 2, nothing placed):
- TESTNET ONLY: ``ARCUS_PROBE_NETWORK`` must parse to the testnet token, and the
  REST/WS URLs must be the documented testnet hosts (an env override pointing
  at the mainnet host, or anywhere else, is refused).
- Refuses to run on a Fly machine (``FLY_APP_NAME`` / ``FLY_MACHINE_ID``) unless
  ``--allow-fly``: it would drain the production egress IP bucket.
- The signing key comes ONLY from ``ARCUS_PROBE_SIGNING_KEY`` (never argv, never
  a file); it is popped from this process's environment at once, never
  printed, logged or written. A WALLET private key is refused. The key must be
  listed ACTIVE for the address, valid > 24 h, cover subaccount 0, and carry no
  ``withdraw`` permission.
- ``pool-watch`` and ``default-leverage`` are KEYLESS: they never read the key
  (and pop it if present), so the 72 h ``pool-watch`` never holds it.

Order rules: clientIds ``nb0_<run36>-<seq>`` (user tag 0 = probe), goodTilTime
now + ARCUS_GTT_DAYS, ALO + LIMIT, prices ORACLE-anchored on the side that
cannot cross the book (``probe_price``; never book-mid-anchored), at most 2
placements/s, ``--max`` per subcommand and 300 per process. Trading
subcommands need ``--i-understand-this-trades``, a flat baseline position and
a book whose touches sit within 500 bp of mark AND oracle; each opens and
closes its OWN tiny position (protective close price asserted within 10 % of
mark). Cleanup always runs (also on Ctrl-C): batch-cancel every probe
clientId by id (never cancel-all, never modify), re-read open orders by the
run prefix (up to 3 rounds), flatten only a probe-created position delta when
trading was consented. Anything left -> exit 3 and a loud line.

Report: ``<out>/<subcommand>_<YYYYmmddTHHMMSSZ>.json`` (also printed) with no
address (replaced by 0x…dead), no key, no pubkey, no signature, no country.
Exit codes: 0 ok, 2 refused (preflight), 3 cleanup incomplete, 1 unexpected
error or interrupted (the traceback is redacted before printing). Press Ctrl-C
ONCE (or ``kill <pid>``: SIGTERM is handled the same way) and let the cleanup
finish; a second Ctrl-C aborts it. ``pool-watch`` is meant to be stopped that
way and exits 0 with its summary.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import signal
import socket
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, NamedTuple, Sequence
from urllib.parse import urlsplit

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import httpx  # noqa: E402

from src.nadobro.config import (  # noqa: E402
    ARCUS_MAINNET_REST_DEFAULT,
    ARCUS_MAINNET_WS_DEFAULT,
    ARCUS_TESTNET_REST_DEFAULT,
    ARCUS_TESTNET_WS_DEFAULT,
    arcus_rest_url,
    arcus_ws_url,
)
from src.nadobro.core.feature_flags import (  # noqa: E402
    arcus_catalog_max_age_s,
    arcus_clock_max_age_s,
    arcus_force_ipv4,
    arcus_gtt_days,
    arcus_market_allowlist,
)
from src.nadobro.core.log_redaction import redact_sensitive_text  # noqa: E402
from src.nadobro.utils.env import env_str  # noqa: E402
from src.nadobro.utils.venue_scope import (  # noqa: E402
    ARCUS_NETWORK_MAINNET,
    ARCUS_TESTNET_SCOPE,
    arcus_scope_for,
    parse_arcus_net,
)
from src.nadobro.venue.arcus.budget import IpBudget  # noqa: E402
from src.nadobro.venue.arcus.catalog import ArcusCatalog, ArcusMarket  # noqa: E402
from src.nadobro.venue.arcus.client import (  # noqa: E402
    _BASE_HEADERS,
    _TIMEOUT,
    ArcusClient,
    BatchCancelResult,
    BboView,
    build_transport,
)
from src.nadobro.venue.arcus.clock import GTT_MIN_AHEAD_US, ArcusClock  # noqa: E402
from src.nadobro.venue.arcus.errors import (  # noqa: E402
    Accepted,
    Ambiguous,
    Forbidden,
    LocalDenied,
    NoActivity,
    NotFound,
    Ok,
    Rejected,
    Throttled,
    Transmission,
    Unauthorized,
    Unavailable,
    WriteResult,
    classify_http,
    schema_error_counts,
)
from src.nadobro.venue.arcus.signing import (  # noqa: E402
    ArcusAuth,
    Ed25519Signer,
    canonical_json,
    cancel_payload,
    make_auth,
    normalize_seed_hex,
    place_payload,
    to_quantums,
    to_ticks,
    wire_decimal,
)
from src.nadobro.venue.arcus.types import (  # noqa: E402
    ARCUS_MARKET_PRICE_BAND,
    ARCUS_MAX_BATCH,
    TICKER_RE,
    ArcusAccountRef,
    CancelSpec,
    Lane,
    OrderRow,
    OrderSpec,
    Side,
    Tif,
    WireOrderType,
    client_id_for,
    normalize_address,
    session_client_prefix,
)

# --- constants ---------------------------------------------------------------------------
DEAD_ADDRESS = "0x000000000000000000000000000000000000dead"
PROBE_USER_TAG = 0  # 02 D17: user tag 0 is reserved for this script (Telegram ids are >= 1)
HARD_CAP_PLACEMENTS = 300  # per process (02 §11.2)
PLACEMENT_SPACING_S = 0.5  # <= 2 placements / s
DEFAULT_MAX = 60
DEFAULT_FAR_BP = 500  # guides: "A resting GTT buy ~5% below the BTC-USD oracle price"
FAR_BP_MIN, FAR_BP_MAX = 50, 2000
BAND_UPPER_BP = 2000  # oracle-band search ceiling
BAND_FLOOR_BP = 50
BAND_MAX_STEPS = 12
CAP_RUNG_START_BP = 200  # open-order-cap rungs stay within [200 bp, far_bp] of the oracle
TAKER_TOUCH_MAX_BP = Decimal(500)  # taker preflight: both touches within 500 bp of mark AND oracle
OPEN_CROSS_BP = Decimal(20)  # taker open: IOC LIMIT at the touch +/- 20 bp
CLOSE_TOUCH_BP = Decimal(100)  # close: protective price = opposite touch -/+ 100 bp
MARK_FALLBACK_BP = Decimal(900)  # close fallback / ioc-reduce-only: mark -/+ 9 % (inside the 10 % band)
KEY_MIN_VALIDITY_MS = 24 * 3600 * 1000
TERMINAL_WAIT_S = 10.0
TERMINAL_POLL_S = 0.25
CLEANUP_ROUNDS = 3
CLEANUP_SETTLE_S = 2.0
CLEANUP_GRACE_S = 5.0  # an order ACKed < 5 s ago may not be visible yet (absent != gone)
READ_WAIT_S = 30.0
ORACLE_MAX_AGE_S = 5.0  # a catalog snapshot this fresh is reused (markets costs weight ~23)
BP = Decimal(10_000)

KEYLESS = frozenset({"pool-watch", "default-leverage"})
# Always trade: need --i-understand-this-trades + flat baseline + taker preflight.
# alo-cross is here too although it is an ALO: if the ask moves between the BBO
# read and the placement the order RESTS at the touch and can be filled, so it
# gets the same consent and flatten guarantees (safer reading of 06 §15.1).
TAKER_ONLY = frozenset(
    {"tradeid-parity", "fee-sign", "entry-units", "ioc-reduce-only", "alo-reduce-only", "alo-cross"}
)
# Trade only with the flag (their non-taker half runs without it).
TAKER_OPTIONAL = frozenset({"min-size", "ws-fresh"})
SUBCOMMANDS = (
    "sign-check",
    "ack-latency",
    "ack-404-window",
    "cancel-race",
    "oracle-band",
    "open-order-cap",
    "min-size",
    "pool-watch",
    "ws-fresh",
    "tradeid-parity",
    "fee-sign",
    "entry-units",
    "ct-order",
    "charged-400",
    "default-leverage",
    "ioc-reduce-only",
    "alo-reduce-only",
    "alo-cross",
    "all",
)
# `all` = preflight once, then (02 §11.2; ct-order BEFORE cancel-race, C18):
ALL_SEQUENCE: tuple[tuple[str, Mapping[str, Any]], ...] = (
    ("sign-check", {}),
    ("ct-order", {}),
    ("ack-404-window", {"n": 5}),
    ("cancel-race", {"n": 5}),
    ("oracle-band", {}),
    ("min-size", {"taker": False}),
    ("charged-400", {}),
    ("default-leverage", {}),
    ("ack-latency", {"n": 20}),
)
DEFAULT_N = {"ack-latency": 50, "ack-404-window": 10, "cancel-race": 10}
TERMINAL_STATES = frozenset({"FILLED", "CANCELED", "REJECTED"})
TERMINAL_STATUSES = frozenset({"FILLED", "CANCELED", "MARGIN_CANCELED", "REJECTED", "LIQUIDATED", "ADL"})
WALLET_KEY_REFUSAL = "That is a WALLET private key. Treat it as exposed and move your funds."
_HEX_RUN_RE = re.compile(r"[0-9a-fA-F]{64,}")
_GEO_KEY_NAMES = frozenset({"country", "region"})


class Refused(Exception):
    """Preflight refusal: exit 2, nothing placed. Messages never carry a secret."""


# --- services (tests inject fakes through ``services_factory``) ---------------------------------


async def _websockets_connect(url: str, *, family: int) -> Any:
    import websockets  # probe-only dependency, imported lazily

    return await websockets.connect(
        url,
        family=family,
        open_timeout=10,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=5,
        max_size=2**23,
    )


@dataclass
class ProbeServices:
    """Everything the probe talks to. ``client.clock`` is the one ArcusClock."""

    client: ArcusClient
    catalog: ArcusCatalog
    raw_http: httpx.AsyncClient | None = None
    ws_connect: Callable[..., Awaitable[Any]] | None = None
    make_signer: Callable[[str], Ed25519Signer] = Ed25519Signer.from_seed_hex
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    wall_time: Callable[[], float] = time.time
    placement_spacing_s: float = PLACEMENT_SPACING_S
    ws_wait_s: float = 10.0
    owns_raw_http: bool = False

    async def aclose(self) -> None:
        await self.client.aclose()
        if self.owns_raw_http and self.raw_http is not None:
            await self.raw_http.aclose()


def default_services(net: str) -> ProbeServices:
    """Real clock / IP budget / REST client / catalog for ``net`` (no hub)."""
    clock = ArcusClock(net)
    client = ArcusClient(net, clock=clock, ip_budget=IpBudget(net))
    catalog = ArcusCatalog(net, allowlist=arcus_market_allowlist, max_age_s=arcus_catalog_max_age_s)
    raw = httpx.AsyncClient(
        transport=build_transport(force_ipv4=arcus_force_ipv4()),
        timeout=_TIMEOUT,
        headers=dict(_BASE_HEADERS),
        trust_env=False,
        follow_redirects=False,
    )
    return ProbeServices(
        client=client, catalog=catalog, raw_http=raw, ws_connect=_websockets_connect, owns_raw_http=True
    )


# --- argument parsing ---------------------------------------------------------------------------


def _int_in(lo: int, hi: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected an integer in [{lo}, {hi}]") from None
        if not lo <= value <= hi:
            raise argparse.ArgumentTypeError(f"expected an integer in [{lo}, {hi}]")
        return value

    return parse


def _hours(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("expected hours in (0, 336]") from None
    if not math.isfinite(value) or not 0 < value <= 336:
        raise argparse.ArgumentTypeError("expected hours in (0, 336]")
    return value


def _ticker(text: str) -> str:
    value = text.strip().upper()
    if not TICKER_RE.match(value):
        raise argparse.ArgumentTypeError("invalid market ticker")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arcus_testnet_probe.py",
        description="OWNER-RUN ONLY Arcus TESTNET probe (G1). Key from ARCUS_PROBE_SIGNING_KEY only.",
    )
    parser.add_argument("subcommand", choices=SUBCOMMANDS)
    parser.add_argument("--market", type=_ticker, default="BTC-USD")
    parser.add_argument("--n", type=_int_in(1, 200), default=None, help="iterations (ack-latency 50, ack-404-window 10, cancel-race 10)")
    parser.add_argument("--max", type=_int_in(1, HARD_CAP_PLACEMENTS), default=DEFAULT_MAX, help="placements per subcommand")
    parser.add_argument("--out", default=str(_ROOT / "tests" / "fixtures" / "arcus" / "g1"))
    parser.add_argument("--far-bp", type=_int_in(FAR_BP_MIN, FAR_BP_MAX), default=DEFAULT_FAR_BP)
    parser.add_argument("--interval", type=_int_in(10, 86_400), default=600, help="pool-watch seconds between samples")
    parser.add_argument("--hours", type=_hours, default=72.0, help="pool-watch duration")
    parser.add_argument("--i-understand-this-trades", dest="trades", action="store_true")
    parser.add_argument("--allow-fly", action="store_true")
    return parser


# --- guards -----------------------------------------------------------------------------------


def fly_guard(allow_fly: bool) -> None:
    if (env_str("FLY_APP_NAME") or env_str("FLY_MACHINE_ID")) and not allow_fly:
        raise Refused("refusing to run on a Fly machine (production egress IP); pass --allow-fly to override")


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def testnet_guard(net: str) -> None:
    """TESTNET ONLY: the network token AND the REST/WS hosts."""
    if arcus_scope_for(net) != ARCUS_TESTNET_SCOPE:
        raise Refused("testnet only: ARCUS_PROBE_NETWORK must be the Arcus testnet")
    try:
        rest_host = _host(arcus_rest_url(net))
        ws_host = _host(arcus_ws_url(net))
    except ValueError:
        raise Refused("invalid Arcus testnet URL override") from None
    mainnet_hosts = {_host(ARCUS_MAINNET_REST_DEFAULT), _host(ARCUS_MAINNET_WS_DEFAULT)}
    for resolve in (arcus_rest_url, arcus_ws_url):
        try:
            mainnet_hosts.add(_host(resolve(ARCUS_NETWORK_MAINNET)))
        except ValueError:
            pass
    mainnet_hosts.discard("")
    if rest_host in mainnet_hosts or ws_host in mainnet_hosts:
        raise Refused("testnet only: the testnet URL points at the MAINNET host")
    if rest_host != _host(ARCUS_TESTNET_REST_DEFAULT) or ws_host != _host(ARCUS_TESTNET_WS_DEFAULT):
        raise Refused("testnet only: the testnet URL is not the documented Arcus testnet host")


def key_fingerprint(pub_hex: str) -> str:
    return hashlib.sha256(pub_hex.encode("ascii")).hexdigest()[:8]


def is_wallet_key(seed_hex: str, address: str) -> bool:
    """True when the 32-byte value is the secp256k1 WALLET key of ``address``.
    Fail-closed: if the check cannot run, the key is treated as a wallet key."""
    try:
        from eth_account import Account
    except Exception:  # pragma: no cover - eth-account is a declared dependency
        return True
    try:
        derived = Account.from_key("0x" + seed_hex).address
    except Exception:
        return False  # not a valid secp256k1 scalar: cannot be a wallet key
    return str(derived).lower() == address


# --- pure pricing / sizing helpers (unit-tested) ---------------------------------------------------


class ProbeQuote(NamedTuple):
    side: Side
    price: Decimal


def _side_price(oracle: Decimal, bbo: BboView | None, side: Side, far_bp: Decimal | int, market: ArcusMarket) -> Decimal | None:
    distance = Decimal(far_bp) / BP
    if side is Side.BUY:
        price = market.quantize_price(oracle * (1 - distance), Side.BUY)
        ask = None if bbo is None else bbo.ask
        return price if ask is None or price < ask else None
    price = market.quantize_price(oracle * (1 + distance), Side.SELL)
    bid = None if bbo is None else bbo.bid
    return price if bid is None or price > bid else None


def probe_price(
    oracle: Decimal, bbo: BboView | None, side: Side, far_bp: Decimal | int, market: ArcusMarket
) -> ProbeQuote | None:
    """Oracle-anchored maker price on a side that cannot cross (02 §11.2 "Price anchoring").

    BUY ``quantize(oracle × (1 − far))`` must be below the best ask; SELL
    ``quantize(oracle × (1 + far))`` above the best bid. The requested side if it
    qualifies, else the other side, else None (nothing is placed). ``bbo`` None =
    an empty book (a DENIED BBO read must never be passed as None).
    """
    for candidate in (side, Side.SELL if side is Side.BUY else Side.BUY):
        price = _side_price(oracle, bbo, candidate, far_bp, market)
        if price is not None:
            return ProbeQuote(candidate, price)
    return None


def strict_side_price(
    oracle: Decimal, bbo: BboView | None, side: Side, far_bp: Decimal | int, market: ArcusMarket
) -> Decimal | None:
    """Like :func:`probe_price` but never switches side (reduce-only legs)."""
    return _side_price(oracle, bbo, side, far_bp, market)


def _ceil_steps(amount: Fraction, step: Decimal) -> Decimal:
    return Decimal(math.ceil(amount / Fraction(step))) * step


def probe_size(market: ArcusMarket, price: Decimal) -> Decimal | None:
    """Smallest step multiple with notional >= max(minOrderNotional,
    minOrderSize × price) × 1.10 at ``price``; None above maxOrderSize."""
    p = Fraction(price)
    target = max(Fraction(market.min_order_notional), Fraction(market.min_order_size) * p) * Fraction(11, 10)
    size = _ceil_steps(target / p, market.step_size)
    if size <= 0 or size < market.min_order_size or size > market.max_order_size:
        return None
    return size


def taker_size(market: ArcusMarket, price: Decimal, touch: Decimal | None = None) -> Decimal | None:
    """Tiny taker size at limit ``price``: >= minOrderSize and notional >=
    1.05 × minOrderNotional, and notional never above max(minOrderNotional ×
    1.1, minOrderSize × touch × 1.1) (02 §11.2; ``touch`` defaults to ``price``)."""
    p = Fraction(price)
    t = p if touch is None else Fraction(touch)
    need = max(Fraction(market.min_order_size), Fraction(market.min_order_notional) * Fraction(105, 100) / p)
    size = _ceil_steps(need, market.step_size)
    bound = max(Fraction(market.min_order_notional) * Fraction(11, 10), Fraction(market.min_order_size) * t * Fraction(11, 10))
    if size <= 0 or Fraction(size) * p > bound or size > market.max_order_size:
        return None
    return size


def below_min_size_size(market: ArcusMarket) -> Decimal | None:
    """``minOrderSize − stepSize`` (> 0), else None."""
    size = market.min_order_size - market.step_size
    return size if size > 0 else None


def below_notional_size(market: ArcusMarket, price: Decimal) -> Decimal | None:
    """Largest step multiple >= minOrderSize whose notional at ``price`` is
    strictly below minOrderNotional; None when no such size exists."""
    ratio = Fraction(market.min_order_notional) / (Fraction(price) * Fraction(market.step_size))
    n = math.ceil(ratio) - 1
    size = Decimal(n) * market.step_size
    if n < 1 or size < market.min_order_size or Fraction(size) * Fraction(price) >= Fraction(market.min_order_notional):
        return None
    return size


def _bp_from(price: Decimal, ref: Decimal) -> Decimal:
    return abs(price - ref) / ref * BP


def taker_preflight_reason(bbo: BboView | None, market: ArcusMarket) -> str | None:
    """None when a taker round trip is safe to attempt: both touches present and
    within 500 bp of the market's mark AND oracle (02 §11.2 "Taker preflight")."""
    if bbo is None or bbo.bid is None or bbo.ask is None:
        return "the book is missing a side"
    if market.mark_price is None or market.oracle_price is None:
        return "no mark or oracle price"
    for touch in (bbo.bid, bbo.ask):
        for ref in (market.mark_price, market.oracle_price):
            if _bp_from(touch, ref) > TAKER_TOUCH_MAX_BP:
                return "a touch is more than 500 bp from mark or oracle"
    return None


def within_mark_band(price: Decimal, mark: Decimal | None) -> bool:
    """place-order: a MARKET protective price "Must be within 10% of the current mark price"."""
    if mark is None or mark <= 0:
        return False
    return abs(price - mark) <= mark * ARCUS_MARKET_PRICE_BAND


def close_protective_price(market: ArcusMarket, bbo: BboView | None, close_side: Side) -> Decimal | None:
    """Protective price of a reduce-only MARKET+IOC close: the opposite touch ∓
    100 bp; if that touch is missing or outside the band, mark ∓ 9 %. Always
    asserted within 10 % of MARK (never the mid); None = cannot close safely."""
    mark = market.mark_price
    if mark is None:
        return None
    candidates: list[Decimal] = []
    if close_side is Side.SELL:
        if bbo is not None and bbo.bid is not None:
            candidates.append(bbo.bid * (1 - CLOSE_TOUCH_BP / BP))
        candidates.append(mark * (1 - MARK_FALLBACK_BP / BP))
    else:
        if bbo is not None and bbo.ask is not None:
            candidates.append(bbo.ask * (1 + CLOSE_TOUCH_BP / BP))
        candidates.append(mark * (1 + MARK_FALLBACK_BP / BP))
    for raw in candidates:
        if raw <= 0:
            continue
        price = market.quantize_price(raw, close_side, crossing=True)
        if within_mark_band(price, mark):
            return price
    return None


def mark_offset_price(market: ArcusMarket, close_side: Side, bp: Decimal) -> Decimal | None:
    """mark ∓ ``bp`` (crossing-quantized), asserted within the 10 % band."""
    mark = market.mark_price
    if mark is None:
        return None
    raw = mark * (1 - bp / BP) if close_side is Side.SELL else mark * (1 + bp / BP)
    price = market.quantize_price(raw, close_side, crossing=True)
    return price if within_mark_band(price, mark) else None


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest-rank percentile (q in (0, 100]); None for no samples."""
    data = sorted(values)
    if not data:
        return None
    rank = max(1, math.ceil(q / 100.0 * len(data)))
    return data[min(rank, len(data)) - 1]


def summarize_ms(values: Sequence[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "p99": percentile(values, 99),
        "max": max(values) if values else None,
    }


def recommend_constants(ack_p99_ms: float | None, cancel_p99_ms: float | None) -> dict[str, float | None]:
    """06 §15.1: ACK grace = max(2, 3 × p99); cancel confirm = max(2, 3 × p99);
    per-call cancel wait = max(0.5, 2 × p99) (seconds)."""
    def scaled(p99_ms: float | None, factor: float, floor: float) -> float | None:
        if p99_ms is None:
            return None
        return round(max(floor, factor * max(0.0, p99_ms) / 1000.0), 3)

    return {
        "ARCUS_ACK_GRACE_S": scaled(ack_p99_ms, 3.0, 2.0),
        "ARCUS_CANCEL_CONFIRM_S": scaled(cancel_p99_ms, 3.0, 2.0),
        "ARCUS_CANCEL_WAIT_S": scaled(cancel_p99_ms, 2.0, 0.5),
    }


def is_terminal_row(row: OrderRow) -> bool:
    """Only an authoritative 200 terminal state counts (absent != gone)."""
    if row.state is not None:
        if row.state in TERMINAL_STATES:
            return True
        # "PARTIALLY_FILLED is terminal for IOC orders"
        return row.state == "PARTIALLY_FILLED" and row.tif in (Tif.IOC, Tif.FOK)
    return row.status in TERMINAL_STATUSES


def outcome_dict(out: object) -> dict[str, Any]:
    """A typed outcome as report data (server text is redacted later)."""
    name = type(out).__name__
    if isinstance(out, Accepted):
        return {
            "outcome": name,
            "http": out.http_status,
            "status": out.status,
            "rejection_reason": out.rejection_reason,
            "pool_remaining": None if out.pool is None else out.pool.remaining,
        }
    if isinstance(out, Rejected):
        return {"outcome": name, "http": out.http_status, "error_type": out.error_type, "error_source": out.error_source, "message": out.message[:200]}
    if isinstance(out, Throttled):
        return {"outcome": name, "http": 429, "layer": out.layer, "retry_after_ms": out.retry_after_ms}
    if isinstance(out, Unauthorized):
        return {"outcome": name, "http": 401, "message": out.message[:200]}
    if isinstance(out, Forbidden):
        return {"outcome": name, "http": 403, "kind": out.kind}
    if isinstance(out, (NotFound, NoActivity)):
        return {"outcome": name, "http": 404}
    if isinstance(out, Unavailable):
        return {"outcome": name, "http": out.http_status, "message": out.message[:120]}
    if isinstance(out, Ambiguous):
        return {"outcome": name, "detail": out.detail}
    if isinstance(out, Transmission):
        return {"outcome": name, "message": out.message[:200]}
    if isinstance(out, LocalDenied):
        return {"outcome": name, "reason": out.reason}
    if isinstance(out, Ok):
        return {"outcome": name, "http": out.http_status}
    return {"outcome": name}


def race_label(place_out: object, cancel_out: object, final_state: str | None) -> str:
    """cancel-race verdict (A6): was a cancel that beat its placement buffered?"""
    if isinstance(place_out, Rejected) or (isinstance(place_out, Accepted) and place_out.status == "REJECTED"):
        return "PLACE_REJECTED"
    if not isinstance(place_out, Accepted):
        return "PLACE_UNKNOWN"
    not_found = isinstance(cancel_out, Accepted) and cancel_out.rejection_reason == "ORDER_NOT_FOUND"
    if final_state == "CANCELED":
        return "CANCELED"
    if final_state == "OPEN":
        if not_found:
            return "OPEN_AFTER_NOT_FOUND"
        return "OPEN_AFTER_CANCEL_ACK" if isinstance(cancel_out, Accepted) else "OPEN_AFTER_CANCEL_FAILED"
    if final_state in ("FILLED", "PARTIALLY_FILLED", "REJECTED"):
        return final_state
    return "UNKNOWN"


async def bisect_band(
    try_at: Callable[[int], Awaitable[tuple[str, Decimal | None]]],
    *,
    lower_bp: int,
    upper_bp: int = BAND_UPPER_BP,
    max_steps: int = BAND_MAX_STEPS,
) -> dict[str, Any]:
    """Bracket the OracleDeviation distance with <= ``max_steps`` placements.

    ``try_at(d)`` places one ALO ``d`` bp from the oracle and returns
    (``"accepted"`` | ``"oracle_deviation"`` | ``"inconclusive:<why>"``, the
    actual distance in bp of the price sent). Any inconclusive step aborts the
    side (never read as a threshold)."""
    trace: list[dict[str, Any]] = []
    if lower_bp > upper_bp:
        return {"result": "n/a: book crosses band", "lower_bp": lower_bp, "steps": 0, "trace": trace}

    async def probe(d: int) -> str:
        verdict, actual = await try_at(d)
        trace.append({"bp": d, "actual_bp": None if actual is None else float(actual), "result": verdict})
        return verdict

    def done(result: str, **extra: Any) -> dict[str, Any]:
        accepted = [t["actual_bp"] for t in trace if t["result"] == "accepted" and t["actual_bp"] is not None]
        rejected = [t["actual_bp"] for t in trace if t["result"] == "oracle_deviation" and t["actual_bp"] is not None]
        return {
            "result": result,
            "lower_bp": lower_bp,
            "upper_bp": upper_bp,
            "accepted_max_bp": max(accepted) if accepted else None,
            "rejected_min_bp": min(rejected) if rejected else None,
            "steps": len(trace),
            "trace": trace,
            **extra,
        }

    first = await probe(lower_bp)
    if first.startswith("inconclusive"):
        return done("inconclusive")
    if first == "oracle_deviation":
        return done("below_lower")
    last = await probe(upper_bp)
    if last.startswith("inconclusive"):
        return done("inconclusive")
    if last == "accepted":
        return done("above_upper")
    lo, hi = lower_bp, upper_bp
    while len(trace) < max_steps and hi - lo > 1:
        mid = (lo + hi) // 2
        verdict = await probe(mid)
        if verdict == "accepted":
            lo = mid
        elif verdict == "oracle_deviation":
            hi = mid
        else:
            return done("inconclusive")
    return done("bracketed")


# --- report redaction ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value, key=str) if isinstance(value, (set, frozenset)) else value
        return [_jsonable(v) for v in items]
    if isinstance(value, Side):
        return value.value
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def redact_report(obj: Any, *, address: str | None, secrets: Iterable[str]) -> Any:
    """Report data with the address -> 0x…dead (any case, with or without 0x),
    secrets and every >= 64-hex run -> ``<redacted>``, country/region -> ``XX``."""
    body = address[2:].lower() if address else None
    dead_body = DEAD_ADDRESS[2:]
    lowered = [s.lower() for s in secrets if s]

    def text(value: str) -> str:
        out = value
        if body:
            out = re.sub(re.escape(body), dead_body, out, flags=re.IGNORECASE)
        for secret in lowered:
            out = re.sub(re.escape(secret), "<redacted>", out, flags=re.IGNORECASE)
        return _HEX_RUN_RE.sub("<redacted>", out)

    def walk(value: Any, key: str | None = None) -> Any:
        if key is not None and key.lower() in _GEO_KEY_NAMES and value is not None:
            return "XX"
        if isinstance(value, str):
            return text(value)
        if isinstance(value, Mapping):
            return {text(str(k)): walk(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value

    return walk(_jsonable(obj))


def assert_report_clean(text: str, *, address: str | None, secrets: Iterable[str]) -> None:
    """Last line of defence before anything is written or printed."""
    lowered = text.lower()
    if address and address.lower() != DEAD_ADDRESS and address[2:].lower() in lowered:
        raise RuntimeError("report redaction failed (address)")
    for secret in secrets:
        if secret and secret.lower() in lowered:
            raise RuntimeError("report redaction failed (secret)")
    if _HEX_RUN_RE.search(text):
        raise RuntimeError("report redaction failed (long hex)")


# --- placement accounting ---------------------------------------------------------------------------


class PlacementGate:
    """<= 2 placements / s, ``per_sub_max`` per subcommand, 300 per process."""

    def __init__(
        self,
        per_sub_max: int,
        *,
        monotonic: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
        spacing_s: float = PLACEMENT_SPACING_S,
        hard_cap: int = HARD_CAP_PLACEMENTS,
    ) -> None:
        self.per_sub_max = per_sub_max
        self.hard_cap = hard_cap
        self.total = 0
        self.count = 0
        self._mono = monotonic
        self._sleep = sleep
        self._spacing = spacing_s
        self._last: float | None = None

    def start(self) -> None:
        self.count = 0

    async def take(self, *, essential: bool = False) -> bool:
        """``essential`` (a reduce-only safety close) is never refused by the caps
        — a cap must never leave a probe position open — but is still spaced."""
        if not essential and (self.count >= self.per_sub_max or self.total >= self.hard_cap):
            return False
        if self._last is not None:
            wait = self._spacing - (self._mono() - self._last)
            if wait > 0:
                await self._sleep(wait)
        self._last = self._mono()
        self.count += 1
        self.total += 1
        return True


@dataclass
class ProbeOrder:
    client_id: str
    market_id: int
    ticker: str
    sub: str
    reduce_only: bool = False
    order_id: str | None = None
    terminal: str | None = None  # verified/definite terminal evidence only


@dataclass
class Placed:
    rec: ProbeOrder | None
    out: WriteResult | None
    t_send: float
    t_ack: float

    @property
    def accepted(self) -> bool:
        return isinstance(self.out, Accepted) and self.out.status != "REJECTED"

    def info(self) -> dict[str, Any]:
        if self.out is None:
            return {"outcome": "cap_reached"}
        data = outcome_dict(self.out)
        if self.rec is not None:
            data["client_id"] = self.rec.client_id
        return data


def _note_place(rec: ProbeOrder, out: WriteResult) -> None:
    if isinstance(out, Accepted):
        rec.order_id = out.order_id
        if out.status in ("REJECTED", "FILLED", "CANCELED"):
            rec.terminal = out.status
    elif isinstance(out, Rejected):
        rec.terminal = "rejected_sync"
    elif isinstance(out, LocalDenied):
        rec.terminal = "not_sent"
    elif isinstance(out, Unavailable) and out.message.startswith("not_sent:"):
        rec.terminal = "not_sent"
    elif isinstance(out, (Throttled, Unauthorized, Forbidden)):
        rec.terminal = "refused"
    # Ambiguous / Transmission / other Unavailable: unknown -> cleanup cancels it.


# --- raw WebSocket collector (probe-only; P4a's ws.py replaces nothing here) ------------------------


def _iter_dicts(value: Any, key: str, depth: int = 0) -> Iterable[Mapping[str, Any]]:
    """Every dict under ``value`` (dicts/lists, depth <= 4) that carries ``key``."""
    if depth > 4:
        return
    if isinstance(value, Mapping):
        if key in value:
            yield value
        for item in value.values():
            if isinstance(item, (Mapping, list)):
                yield from _iter_dicts(item, key, depth + 1)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_dicts(item, key, depth + 1)


def order_events(frame: Mapping[str, Any], client_id: str) -> list[Mapping[str, Any]]:
    if frame.get("channel") != "orders":
        return []
    return [d for d in _iter_dicts(frame.get("contents"), "clientId") if d.get("clientId") == client_id]


def order_event_state(event: Mapping[str, Any]) -> str:
    state = event.get("state")
    if isinstance(state, str) and state:
        return state.upper()
    status = event.get("status")
    return status.upper() if isinstance(status, str) else "?"


def fill_trade_ids(frame: Mapping[str, Any], order_ids: set[str], client_ids: set[str]) -> list[str]:
    if frame.get("channel") != "userFills":
        return []
    out = []
    for d in _iter_dicts(frame.get("contents"), "tradeId"):
        if d.get("orderId") in order_ids or d.get("clientId") in client_ids:
            trade_id = d.get("tradeId")
            if isinstance(trade_id, (str, int)):
                out.append(str(trade_id))
    return out


def is_snapshot_frame(frame: Mapping[str, Any]) -> bool:
    """The ``subscribed`` frame IS the initial snapshot; re-broadcasts carry
    ``isSnapshot: true`` (asyncapi AccountSnapshot "``isSnapshot: true`` injected
    at the top level" — accepted on the envelope or inside ``contents``)."""
    if frame.get("type") == "subscribed" or frame.get("isSnapshot") is True:
        return True
    contents = frame.get("contents")
    return isinstance(contents, Mapping) and contents.get("isSnapshot") is True


def frame_position_size(frame: Mapping[str, Any], market_id: int) -> Decimal | None:
    """Signed size of ``market_id`` in an ``account``/``positions`` frame. In a
    SNAPSHOT an absent market is flat (0); in a delta that does not mention the
    market the answer is unknown (None)."""
    contents = frame.get("contents")
    if not isinstance(contents, Mapping):
        return None
    positions = contents.get("positions")
    rows: list[Mapping[str, Any]] = []
    if isinstance(positions, Mapping):
        rows = [r for r in positions.values() if isinstance(r, Mapping)]
    elif isinstance(positions, list):
        rows = [r for r in positions if isinstance(r, Mapping)]
    else:
        return None
    for row in rows:
        if row.get("marketId") == market_id:
            try:
                size = Decimal(str(row.get("size")))
            except Exception:
                return None
            return size if size.is_finite() else None
    if is_snapshot_frame(frame):
        return Decimal(0)
    return None


class WsTap:
    """Raw collector: subscribe by address (accountIndex 0) and keep every frame
    with a local receive timestamp. Nothing here is logged or reported raw."""

    def __init__(
        self,
        connect: Callable[..., Awaitable[Any]],
        url: str,
        address: str,
        *,
        force_ipv4: bool,
        monotonic: Callable[[], float],
    ) -> None:
        self._connect = connect
        self._url = url
        self._address = address
        self._family = socket.AF_INET if force_ipv4 else 0
        self._mono = monotonic
        self._ws: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._changed = asyncio.Event()
        self.frames: list[tuple[float, Mapping[str, Any]]] = []
        self.closed_reason: str | None = None

    async def open(self) -> bool:
        try:
            self._ws = await asyncio.wait_for(self._connect(self._url, family=self._family), timeout=15.0)
        except Exception as exc:
            self.closed_reason = type(exc).__name__
            return False
        self._reader = asyncio.create_task(self._read_loop())
        return True

    async def _read_loop(self) -> None:
        try:
            while True:
                raw = await self._ws.recv()
                t = self._mono()
                try:
                    frame = json.loads(raw, parse_float=Decimal)
                except (TypeError, ValueError):
                    continue
                if isinstance(frame, Mapping):
                    self.frames.append((t, frame))
                    self._changed.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.closed_reason = type(exc).__name__
            self._changed.set()

    async def subscribe(self, channel: str, *, snapshot: bool, timeout: float = 10.0) -> bool:
        start = len(self.frames)
        msg = {"type": "subscribe", "channel": channel, "id": self._address, "accountIndex": 0, "snapshot": snapshot}
        try:
            await self._ws.send(json.dumps(msg))
        except Exception as exc:
            self.closed_reason = type(exc).__name__
            return False
        hit = await self.wait_for(
            lambda f: f.get("type") == "subscribed" and f.get("channel") == channel, timeout, start=start
        )
        return hit is not None

    async def wait_for(
        self, pred: Callable[[Mapping[str, Any]], bool], timeout: float, *, start: int = 0
    ) -> tuple[float, Mapping[str, Any]] | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        i = start
        while True:
            while i < len(self.frames):
                t, frame = self.frames[i]
                i += 1
                if pred(frame):
                    return t, frame
            if self.closed_reason is not None:
                return None
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            self._changed.clear()
            if i < len(self.frames):
                continue
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                pass

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except BaseException:
                pass
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass


# --- raw signed POST (ct-order only) -----------------------------------------------------------------


@dataclass(frozen=True)
class RawRequest:
    path: str
    params: Mapping[str, str]
    content: bytes
    headers: Mapping[str, str]
    client_id: str | None
    expect_pool: str


class RawSigner:
    """Builds and sends a signed placeOrder / cancelOrder with a CHOSEN ``ct``
    (the public client always draws a fresh one). Same headers / body / query as
    the client (02 §8.1); outcomes via ``errors.classify_http``. Never logs."""

    def __init__(self, http: httpx.AsyncClient, base_url: str, auth: ArcusAuth, client: ArcusClient, monotonic: Callable[[], float]) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self._auth = auth
        self._client = client
        self._mono = monotonic

    def _headers(self, ct: int, signature: str) -> dict[str, str]:
        return {
            **_BASE_HEADERS,
            "Content-Type": "application/json",
            "X-API-Key": self._auth.api_key_hex,
            "X-Timestamp": str(ct),
            "X-Signature": signature,
        }

    def build_place(self, spec: OrderSpec, ct: int) -> RawRequest:
        ref = self._auth.ref
        payload = place_payload(
            address=ref.address,
            account_index=ref.account_index,
            client_id=spec.client_id,
            ct_ns=ct,
            good_til_us=spec.good_til_us,
            market_id=spec.market_id,
            price_ticks=to_ticks(spec.price, spec.tick_size),
            qty_quantums=to_quantums(spec.quantity, spec.step_size),
            reduce_only=spec.reduce_only,
            side=spec.side,
            tif=spec.tif,
        )
        body: dict[str, object] = {
            "address": ref.address,
            "accountIndex": ref.account_index,
            "marketId": spec.market_id,
            "orderSide": spec.side.value,
            "orderType": spec.order_type.value,
            "quantity": wire_decimal(spec.quantity),
            "price": wire_decimal(spec.price),
            "timeInForce": spec.tif.name,
            "goodTilTime": str(spec.good_til_us),
            "reduceOnly": spec.reduce_only,
            "clientId": spec.client_id,
            "timestamp": ct,
        }
        return RawRequest(
            path="/v1/placeOrder",
            params={"address": ref.address},
            content=canonical_json(body),
            headers=self._headers(ct, self._auth.sign_hex(payload)),
            client_id=spec.client_id,
            expect_pool="order",
        )

    def build_cancel(self, spec: CancelSpec, ct: int) -> RawRequest:
        ref = self._auth.ref
        payload = cancel_payload(
            address=ref.address,
            account_index=ref.account_index,
            ct_ns=ct,
            market_id=spec.market_id,
            order_id=spec.order_id,
            client_id=spec.client_id,
        )
        body: dict[str, object] = {
            "address": ref.address,
            "accountIndex": ref.account_index,
            "marketId": spec.market_id,
            "timestamp": ct,
        }
        if spec.order_id is not None:
            body["kind"] = "orderId"
            body["orderId"] = spec.order_id
        else:
            body["kind"] = "clientId"
            body["clientId"] = spec.client_id
        return RawRequest(
            path="/v1/cancelOrder",
            params={"address": ref.address},
            content=canonical_json(body),
            headers=self._headers(ct, self._auth.sign_hex(payload)),
            client_id=spec.client_id,
            expect_pool="cancel",
        )

    async def send(self, req: RawRequest) -> WriteResult:
        budget = self._client.ip_budget
        if budget.write_blocked():
            return LocalDenied(reason="ip_blocked")
        try:
            resp = await self._http.post(
                self._base + req.path,
                params=dict(req.params),
                content=req.content,
                headers=dict(req.headers),
                timeout=_TIMEOUT,
                follow_redirects=False,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol) as exc:
            return Unavailable(http_status=0, message=f"not_sent:{type(exc).__name__}")
        except httpx.HTTPError as exc:
            return Ambiguous(client_id=req.client_id, detail=type(exc).__name__)
        try:
            raw = json.loads(resp.content, parse_float=Decimal) if resp.content else None
        except ValueError:
            raw = None
        body = raw if isinstance(raw, Mapping) else None
        out = classify_http(
            resp.status_code,
            body,
            resp.headers,
            is_write=True,
            client_id=req.client_id,
            now_mono=self._mono(),
            expect_pool="order" if req.expect_pool == "order" else "cancel",
        )
        if isinstance(out, Throttled) and out.layer in ("ip", "unknown", "read_ip"):
            budget.note_server_429(out.retry_after_ms)
        if isinstance(out, (Accepted, Rejected, Throttled, Unauthorized, Forbidden, NotFound, Unavailable, Transmission, Ambiguous, LocalDenied)):
            return out
        return Ambiguous(client_id=req.client_id, detail="unexpected_outcome")


# --- the probe context ----------------------------------------------------------------------------


@dataclass
class ProbeCtx:
    args: argparse.Namespace
    svc: ProbeServices
    net: str
    ref: ArcusAccountRef
    auth: ArcusAuth | None
    market: ArcusMarket
    run_id: int
    gate: PlacementGate
    trades: bool = False  # --i-understand-this-trades consented for THIS subcommand
    registry: dict[str, ProbeOrder] = field(default_factory=dict)
    seq: int = 0
    baseline_positions: dict[int, Decimal] = field(default_factory=dict)
    baseline_open: dict[str, int] = field(default_factory=dict)
    touched: set[int] = field(default_factory=set)
    flatten_allowed: bool = False
    last_place_mono: float | None = None

    @property
    def client(self) -> ArcusClient:
        return self.svc.client

    @property
    def clock(self) -> ArcusClock:
        return self.svc.client.clock

    @property
    def prefix(self) -> str:
        return session_client_prefix(PROBE_USER_TAG, self.run_id)

    def mono(self) -> float:
        return self.svc.monotonic()

    async def sleep(self, seconds: float) -> None:
        await self.svc.sleep(seconds)

    def next_client_id(self) -> str:
        self.seq += 1
        return client_id_for(PROBE_USER_TAG, self.run_id, self.seq)

    def _auth(self) -> ArcusAuth:
        if self.auth is None:
            raise RuntimeError("keyed step in a keyless run")
        return self.auth

    # -- reads (DENIED != EMPTY: None means unknown, never "empty") --
    async def refresh_catalog(self, max_age_s: float = ORACLE_MAX_AGE_S) -> bool:
        """Reuse a snapshot at most ``max_age_s`` old, else ``GET /v1/markets``."""
        age = self.svc.catalog.age_s()
        if age is not None and age <= max_age_s:
            return True
        return await self.svc.catalog.refresh(self.client, lane=Lane.L1_ENGINE, max_wait_s=READ_WAIT_S)

    async def refresh_market(self, ticker: str) -> ArcusMarket | None:
        """A fresh (<= 5 s) allowlisted market; None when the read was denied."""
        if not await self.refresh_catalog():
            return None
        market = self.svc.catalog.by_ticker(ticker)
        if market is None or market not in self.svc.catalog.allowlisted():
            return None
        return market

    async def bbo(self, ticker: str, lane: Lane = Lane.L1_ENGINE) -> tuple[bool, BboView | None]:
        """(read_ok, view); a DENIED read is (False, None) — never an empty book."""
        res = await self.client.get_bbo(ticker, lane=lane, max_wait_s=READ_WAIT_S)
        if not isinstance(res, Ok):
            return False, None
        view = res.value
        if view.bid is None and view.ask is None:
            return True, None
        return True, view

    async def position_size(self, market: ArcusMarket, lane: Lane = Lane.L1_ENGINE) -> Decimal | None:
        res = await self.client.get_positions(self.ref, market=market.ticker, lane=lane, max_wait_s=READ_WAIT_S)
        if not isinstance(res, Ok):
            return None
        row = res.value.get(market.market_id)
        return Decimal(0) if row is None else row.size

    async def rate_limit(self) -> dict[str, Any] | None:
        res = await self.client.get_rate_limit(self.ref, lane=Lane.L1_ENGINE, max_wait_s=READ_WAIT_S)
        if not isinstance(res, Ok):
            return None
        order, cancel = res.value
        return {
            "order": {"used": order.used, "cap": order.cap, "next_available_ms": order.next_available_ms},
            "cancel": {"used": cancel.used, "cap": cancel.cap, "next_available_ms": cancel.next_available_ms},
        }

    # -- writes --
    async def place(
        self,
        sub: str,
        market: ArcusMarket,
        side: Side,
        price: Decimal,
        size: Decimal,
        *,
        tif: Tif = Tif.ALO,
        order_type: WireOrderType = WireOrderType.LIMIT,
        reduce_only: bool = False,
        essential: bool = False,
    ) -> Placed:
        now = self.mono()
        if not await self.gate.take(essential=essential and reduce_only):
            return Placed(None, None, now, now)
        cid = self.next_client_id()
        spec = OrderSpec(
            market_id=market.market_id,
            side=side,
            order_type=order_type,
            tif=tif,
            quantity=size,
            price=price,
            reduce_only=reduce_only,
            client_id=cid,
            good_til_us=self.clock.gtt_us(arcus_gtt_days()),
            tick_size=market.tick_size,
            step_size=market.step_size,
        )
        rec = ProbeOrder(cid, market.market_id, market.ticker, sub, reduce_only=reduce_only)
        self.registry[cid] = rec  # write-ahead: cleanup knows it even if the POST dies
        self.touched.add(market.market_id)
        t_send = self.mono()
        self.last_place_mono = t_send
        out = await self.client.place_order(self._auth(), spec)
        t_ack = self.mono()
        _note_place(rec, out)
        return Placed(rec, out, t_send, t_ack)

    async def cancel_recs(self, recs: Sequence[ProbeOrder]) -> list[dict[str, Any]]:
        """Bot path: batchCancelOrders by clientId, <= 100 per call."""
        reports: list[dict[str, Any]] = []
        todo = [r for r in recs]
        for i in range(0, len(todo), ARCUS_MAX_BATCH):
            chunk = todo[i : i + ARCUS_MAX_BATCH]
            specs = [CancelSpec(r.market_id, client_id=r.client_id) for r in chunk]
            res: BatchCancelResult = await self.client.batch_cancel(self._auth(), specs)
            rows = []
            for rec, row in zip(chunk, res.rows):
                if isinstance(row, Accepted) and row.status == "CANCELED":
                    rec.terminal = "CANCELED"
                rows.append(None if row is None else outcome_dict(row))
            reports.append({"sent": len(chunk), "outcome": outcome_dict(res.outcome), "rows": rows})
        return reports

    async def get_order_row(self, rec: ProbeOrder) -> OrderRow | None:
        if rec.order_id is None:
            return None
        res = await self.client.get_order(self.ref, rec.order_id, lane=Lane.L1_ENGINE, max_wait_s=READ_WAIT_S)
        return res.value if isinstance(res, Ok) else None

    async def verify_terminal(self, rec: ProbeOrder, timeout: float = TERMINAL_WAIT_S) -> str:
        """Poll ``GET /v1/order`` every 250 ms; only a 200 terminal state counts
        (a 404 is absent, NOT gone). Otherwise ``"unverified"`` (left to cleanup)."""
        if rec.order_id is None:
            return "unverified: no orderId"
        deadline = self.mono() + timeout
        while True:
            row = await self.get_order_row(rec)
            if row is not None and is_terminal_row(row):
                rec.terminal = row.state or row.status
                return rec.terminal
            if self.mono() >= deadline:
                return "unverified"
            await self.sleep(TERMINAL_POLL_S)

    async def resolve_order_id(self, rec: ProbeOrder, timeout: float = 5.0) -> str | None:
        """orderId of a probe order by clientId via openOrders (when the ACK lacked it)."""
        if rec.order_id is not None:
            return rec.order_id
        deadline = self.mono() + timeout
        while True:
            res = await self.client.get_open_orders(
                self.ref, market=rec.ticker, status=("OPEN",), lane=Lane.L1_ENGINE, max_wait_s=READ_WAIT_S
            )
            if isinstance(res, Ok):
                for row in res.value:
                    if row.client_id == rec.client_id:
                        rec.order_id = row.order_id
                        return row.order_id
            if self.mono() >= deadline:
                return None
            await self.sleep(0.5)

    async def maker_quote(self, market: ArcusMarket, side: Side, far_bp: Decimal | int | None = None) -> tuple[ProbeQuote | None, str | None]:
        """Fresh oracle (catalog) + fresh BBO -> ``probe_price``; (None, reason) to skip."""
        fresh = await self.refresh_market(market.ticker)
        if fresh is None or fresh.oracle_price is None:
            return None, "skipped: no fresh oracle price"
        ok, view = await self.bbo(fresh.ticker)
        if not ok:
            return None, "skipped: BBO read denied"
        quote = probe_price(fresh.oracle_price, view, side, self.args.far_bp if far_bp is None else far_bp, fresh)
        if quote is None:
            return None, "skipped: book crosses the oracle band"
        return quote, None

    async def open_tap(self, channels: Sequence[tuple[str, bool]]) -> WsTap | None:
        if self.svc.ws_connect is None:
            return None
        tap = WsTap(self.svc.ws_connect, arcus_ws_url(self.net), self.ref.address, force_ipv4=arcus_force_ipv4(), monotonic=self.svc.monotonic)
        if not await tap.open():
            return None
        for channel, snapshot in channels:
            if not await tap.subscribe(channel, snapshot=snapshot, timeout=self.svc.ws_wait_s):
                await tap.close()
                return None
        return tap

    async def wait_order_event(self, tap: WsTap, cid: str, states: Iterable[str], *, start: int = 0, timeout: float | None = None) -> tuple[float, Mapping[str, Any]] | None:
        wanted = {s.upper() for s in states}

        def pred(frame: Mapping[str, Any]) -> bool:
            return any(order_event_state(e) in wanted for e in order_events(frame, cid))

        hit = await tap.wait_for(pred, self.svc.ws_wait_s if timeout is None else timeout, start=start)
        if hit is None:
            return None
        t, frame = hit
        events = [e for e in order_events(frame, cid) if order_event_state(e) in wanted]
        return t, events[-1]

    # -- taker round trip (only with --i-understand-this-trades) --
    async def taker_open(self, sub: str, market: ArcusMarket, side: Side) -> dict[str, Any]:
        """IOC LIMIT at the touch ± 20 bp, tiny size; re-checks the taker preflight."""
        if not self.trades:
            raise RuntimeError("taker step without --i-understand-this-trades")
        fresh = await self.refresh_market(market.ticker)
        if fresh is None:
            return {"skipped": "market refresh denied"}
        ok, view = await self.bbo(fresh.ticker)
        if not ok:
            return {"skipped": "BBO read denied"}
        reason = taker_preflight_reason(view, fresh)
        if reason is not None or view is None:
            return {"skipped": f"taker preflight: {reason}"}
        before = await self.position_size(fresh)
        if before is None:
            return {"skipped": "positions read denied"}
        touch = view.ask if side is Side.BUY else view.bid
        assert touch is not None
        raw = touch * (1 + OPEN_CROSS_BP / BP) if side is Side.BUY else touch * (1 - OPEN_CROSS_BP / BP)
        price = fresh.quantize_price(raw, side, crossing=True)
        size = taker_size(fresh, price, touch)
        if size is None:
            return {"skipped": "no tiny taker size fits the bounds"}
        placed = await self.place(sub, fresh, side, price, size, tif=Tif.IOC)
        after = await self._await_position_change(fresh, before)
        return {
            "placement": placed.info(),
            "client_id": None if placed.rec is None else placed.rec.client_id,
            "order_id": None if placed.rec is None else placed.rec.order_id,
            "price": price,
            "size": size,
            "position_before": before,
            "position_after": after,
        }

    async def _await_position_change(self, market: ArcusMarket, before: Decimal, timeout: float = 5.0) -> Decimal | None:
        deadline = self.mono() + timeout
        last: Decimal | None = None
        while True:
            last = await self.position_size(market)
            if last is not None and last != before:
                return last
            if self.mono() >= deadline:
                return last
            await self.sleep(0.25)

    async def close_delta(self, sub: str, market: ArcusMarket, target: Decimal, lane: Lane = Lane.L1_ENGINE) -> dict[str, Any]:
        """Reduce-only close of (current − target) — the probe-created delta only.
        MARKET+IOC with the protective price asserted inside the 10 % mark band;
        a 400 on MARKET falls back to LIMIT+IOC at the same price."""
        steps: list[dict[str, Any]] = []
        for _attempt in range(2):
            current = await self.position_size(market, lane)
            if current is None:
                return {"closed": False, "reason": "positions read denied", "steps": steps}
            delta = current - target
            if delta == 0:
                return {"closed": True, "steps": steps}
            fresh = await self.refresh_market(market.ticker) or self.svc.catalog.get(market.market_id)
            if fresh is None:
                return {"closed": False, "reason": "no market metadata", "steps": steps}
            ok, view = await self.bbo(fresh.ticker, lane)
            close_side = Side.SELL if delta > 0 else Side.BUY
            price = close_protective_price(fresh, view if ok else None, close_side)
            size = fresh.quantize_size_down(abs(delta))
            if price is None or size <= 0:
                return {"closed": False, "reason": "no protective price inside the 10% mark band", "steps": steps}
            placed = await self.place(
                sub, fresh, close_side, price, size,
                tif=Tif.IOC, order_type=WireOrderType.MARKET, reduce_only=True, essential=True,
            )
            steps.append({"order_type": "MARKET", **placed.info()})
            if isinstance(placed.out, Rejected):
                placed = await self.place(sub, fresh, close_side, price, size, tif=Tif.IOC, reduce_only=True, essential=True)
                steps.append({"order_type": "LIMIT", **placed.info()})
            after = await self._await_position_change(fresh, current)
            if after is not None and after == target:
                return {"closed": True, "steps": steps}
        return {"closed": False, "reason": "position still differs from the baseline", "steps": steps}


# --- subcommands ------------------------------------------------------------------------------------


def _sig_verdict(out: object) -> str:
    """Signature verdict of one signed write: only ``Accepted`` proves the venue
    verified the signature; ``Unauthorized`` fails it; a 400/403 (or any other
    outcome) is INCONCLUSIVE — the docs do not say whether the signature is
    checked before the API-layer checks, so it is never read as "signature OK"."""
    if isinstance(out, Accepted):
        return "accepted"
    if isinstance(out, Unauthorized):
        return "failed"
    return "inconclusive"


async def sub_sign_check(ctx: ProbeCtx) -> dict[str, Any]:
    """G1 gate before P6a: both signing schemes + batch cancel, venue-verified.

    place #1 -> cancel by orderId; place #2 -> cancel by clientId; place #3, #4 ->
    one batchCancelOrders [#3 by orderId, #4 by clientId]; setLeverage to the
    CURRENT value (no change, margin mode untouched); every placement verified
    terminal through ``GET /v1/order`` (a 404 is not terminal).
    """
    market = ctx.market
    auth = ctx._auth()
    res: dict[str, Any] = {"place": [], "cancel_by_id": None, "cancel_by_cid": None, "batch": None, "set_leverage": None, "terminal": {}}
    verdicts: dict[str, str] = {}
    placed: list[Placed] = []

    async def place_one(tag: str) -> Placed | None:
        quote, why = await ctx.maker_quote(market, Side.BUY)
        size = None if quote is None else probe_size(market, quote.price)
        if quote is None or size is None:
            res["place"].append({"skipped": why or "no probe size"})
            verdicts[tag] = "inconclusive"
            return None
        p = await ctx.place("sign-check", market, quote.side, quote.price, size)
        placed.append(p)
        res["place"].append(p.info())
        verdicts[tag] = _sig_verdict(p.out) if p.out is not None else "inconclusive"
        return p

    # 1 -> cancel by orderId
    p1 = await place_one("place1")
    oid1 = None if p1 is None or p1.rec is None or not isinstance(p1.out, Accepted) else await ctx.resolve_order_id(p1.rec)
    if oid1 is None:
        res["cancel_by_id"] = {"outcome": "n/a: placement #1 has no orderId"}
        verdicts["cancel_by_id"] = "inconclusive"
    else:
        out = await ctx.client.cancel_order(auth, CancelSpec(market.market_id, order_id=oid1))
        res["cancel_by_id"] = outcome_dict(out)
        verdicts["cancel_by_id"] = _sig_verdict(out)
    # 2 -> cancel by clientId
    p2 = await place_one("place2")
    if p2 is None or p2.rec is None or not isinstance(p2.out, Accepted):
        res["cancel_by_cid"] = {"outcome": "n/a: placement #2 not accepted"}
        verdicts["cancel_by_cid"] = "inconclusive"
    else:
        out = await ctx.client.cancel_order(auth, CancelSpec(market.market_id, client_id=p2.rec.client_id))
        res["cancel_by_cid"] = outcome_dict(out)
        verdicts["cancel_by_cid"] = _sig_verdict(out)
    # 3 + 4 -> one batch [#3 by orderId, #4 by clientId]
    p3 = await place_one("place3")
    p4 = await place_one("place4")
    oid3 = None if p3 is None or p3.rec is None or not isinstance(p3.out, Accepted) else await ctx.resolve_order_id(p3.rec)
    if oid3 is None or p4 is None or p4.rec is None or not isinstance(p4.out, Accepted):
        res["batch"] = {"outcome": "n/a: placements #3/#4 not both accepted with ids"}
        verdicts["batch"] = "inconclusive"
    else:
        batch = await ctx.client.batch_cancel(
            auth, [CancelSpec(market.market_id, order_id=oid3), CancelSpec(market.market_id, client_id=p4.rec.client_id)]
        )
        res["batch"] = {"outcome": outcome_dict(batch.outcome), "rows": [None if r is None else outcome_dict(r) for r in batch.rows]}
        if not isinstance(batch.outcome, Accepted):
            verdicts["batch"] = _sig_verdict(batch.outcome)
        elif all(isinstance(r, Accepted) for r in batch.rows):
            verdicts["batch"] = "accepted"
        else:  # a missing (pending) or ERROR row: not proof either way
            verdicts["batch"] = "inconclusive"
    # setLeverage (Scheme 2) to the value the account already has
    lev = await ctx.client.get_leverages(ctx.ref, lane=Lane.L1_ENGINE, max_wait_s=READ_WAIT_S)
    current = None
    if isinstance(lev, Ok):
        current = next((e.leverage for e in lev.value if e.market_id == market.market_id), None)
    if current is None:
        res["set_leverage"] = {"outcome": "n/a: current leverage unknown (never guessed; set it once in the Arcus app, then re-run)"}
        verdicts["set_leverage"] = "inconclusive"
    else:
        out = await ctx.client.set_leverage(auth, market.market_id, current)
        info = outcome_dict(out)
        info.setdefault("status", None)
        res["set_leverage"] = {"leverage": current, **info}
        # A 422 means the engine judged the request, so the Scheme-2 signature verified.
        lev_ok = isinstance(out, Accepted) or (isinstance(out, Rejected) and out.http_status == 422)
        verdicts["set_leverage"] = "accepted" if lev_ok else _sig_verdict(out)
    for p in placed:
        if p.rec is not None and isinstance(p.out, Accepted):
            res["terminal"][p.rec.client_id] = await ctx.verify_terminal(p.rec)
    res["verdicts"] = verdicts
    res["failed"] = sorted(k for k, v in verdicts.items() if v == "failed")
    res["inconclusive"] = sorted(k for k, v in verdicts.items() if v == "inconclusive")
    res["all_accepted"] = len(verdicts) == 8 and all(v == "accepted" for v in verdicts.values())
    return res


async def sub_ack_latency(ctx: ProbeCtx, n: int | None = None) -> dict[str, Any]:
    n = n or ctx.args.n or DEFAULT_N["ack-latency"]
    tap = await ctx.open_tap([("orders", False)])
    if tap is None:
        return {"skipped": "websocket unavailable"}
    ack_open: list[float] = []
    send_open: list[float] = []
    cancel_done: list[float] = []
    timeouts = {"open": 0, "canceled": 0}
    rejected: list[dict[str, Any]] = []
    try:
        market = ctx.market
        quote: ProbeQuote | None = None
        for i in range(n):
            if i % 10 == 0 or quote is None:
                quote, why = await ctx.maker_quote(market, Side.BUY)
                if quote is None:
                    return {"skipped": why, "done": i}
            size = probe_size(market, quote.price)
            if size is None:
                return {"skipped": "no probe size"}
            start = len(tap.frames)
            placed = await ctx.place("ack-latency", market, quote.side, quote.price, size)
            if placed.out is None:
                break
            if not placed.accepted or placed.rec is None:
                rejected.append(placed.info())
                continue
            hit = await ctx.wait_order_event(tap, placed.rec.client_id, ("OPEN", "REJECTED", "FILLED", "CANCELED"), start=start)
            if hit is None:
                timeouts["open"] += 1
            else:
                t_open, event = hit
                state = order_event_state(event)
                if state != "OPEN":
                    placed.rec.terminal = state
                    rejected.append({"client_id": placed.rec.client_id, "ws_state": state, "reason": event.get("rejectionReason")})
                    continue
                ack_open.append((t_open - placed.t_ack) * 1000.0)
                send_open.append((t_open - placed.t_send) * 1000.0)
            start2 = len(tap.frames)
            await ctx.cancel_recs([placed.rec])
            t_cancel_ack = ctx.mono()
            hit2 = await ctx.wait_order_event(tap, placed.rec.client_id, ("CANCELED", "FILLED", "REJECTED"), start=start2)
            if hit2 is None:
                timeouts["canceled"] += 1
            else:
                placed.rec.terminal = order_event_state(hit2[1])
                cancel_done.append((hit2[0] - t_cancel_ack) * 1000.0)
    finally:
        await tap.close()
    ack = summarize_ms(ack_open)
    cancel = summarize_ms(cancel_done)
    return {
        "n": n,
        "ack_to_open_ms": ack,
        "send_to_open_ms": summarize_ms(send_open),
        "cancel_to_canceled_ms": cancel,
        "timeouts": timeouts,
        "rejected": rejected,
        "recommend": recommend_constants(ack["p99"], cancel["p99"]),
        "incomplete": bool(timeouts["open"] or timeouts["canceled"]),
    }


async def sub_ack_404_window(ctx: ProbeCtx, n: int | None = None) -> dict[str, Any]:
    n = n or ctx.args.n or DEFAULT_N["ack-404-window"]
    market = ctx.market
    first_200: list[float] = []
    n404: list[int] = []
    visible: list[float] = []
    notes: list[dict[str, Any]] = []
    for _ in range(n):
        quote, why = await ctx.maker_quote(market, Side.BUY)
        if quote is None:
            notes.append({"skipped": why})
            break
        size = probe_size(market, quote.price)
        if size is None:
            break
        placed = await ctx.place("ack-404-window", market, quote.side, quote.price, size)
        if placed.out is None:
            break
        if not placed.accepted or placed.rec is None:
            notes.append(placed.info())
            continue
        rec = placed.rec
        if rec.order_id is None:
            notes.append({**placed.info(), "note": "ACK carried no orderId; not measurable"})
            await ctx.cancel_recs([rec])
            continue
        oid = rec.order_id
        count, other, t200 = 0, 0, None
        deadline = placed.t_ack + TERMINAL_WAIT_S
        while ctx.mono() < deadline:
            r = await ctx.client.get_order(ctx.ref, oid, lane=Lane.L1_ENGINE, max_wait_s=2.0)
            if isinstance(r, Ok):
                t200 = (ctx.mono() - placed.t_ack) * 1000.0
                break
            if isinstance(r, NotFound):
                count += 1
            else:
                other += 1
            await ctx.sleep(0.05)
        if t200 is not None:
            first_200.append(t200)
            n404.append(count)
        t_vis = None
        deadline = placed.t_ack + TERMINAL_WAIT_S
        while ctx.mono() < deadline:
            r2 = await ctx.client.get_open_orders(ctx.ref, market=market.ticker, status=("OPEN",), lane=Lane.L1_ENGINE, max_wait_s=2.0)
            if isinstance(r2, Ok) and any(row.client_id == rec.client_id for row in r2.value):
                t_vis = (ctx.mono() - placed.t_ack) * 1000.0
                break
            await ctx.sleep(0.25)
        if t_vis is not None:
            visible.append(t_vis)
        notes.append({"client_id": rec.client_id, "first_200_ms": t200, "n_404": count, "denied_polls": other, "open_orders_visible_ms": t_vis})
        await ctx.cancel_recs([rec])
    return {
        "n": n,
        "first_200_ms": summarize_ms(first_200),
        "n_404_before_visible": {"max": max(n404) if n404 else None, "values": n404},
        "open_orders_visible_ms": summarize_ms(visible),
        "orders": notes,
    }


async def sub_cancel_race(ctx: ProbeCtx, n: int | None = None) -> dict[str, Any]:
    n = n or ctx.args.n or DEFAULT_N["cancel-race"]
    market = ctx.market
    tap = await ctx.open_tap([("orders", False)])
    cases: dict[str, list[dict[str, Any]]] = {"0ms": [], "5ms": [], "20ms": []}
    try:
        for _ in range(n):
            for delay_ms in (0, 5, 20):
                quote, why = await ctx.maker_quote(market, Side.BUY)
                if quote is None:
                    return {"skipped": why, "cases": cases}
                size = probe_size(market, quote.price)
                if size is None:
                    return {"skipped": "no probe size", "cases": cases}
                if not await ctx.gate.take():
                    return {"stopped": "placement cap reached", "cases": cases}
                cid = ctx.next_client_id()
                spec = OrderSpec(
                    market_id=market.market_id, side=quote.side, order_type=WireOrderType.LIMIT, tif=Tif.ALO,
                    quantity=size, price=quote.price, reduce_only=False, client_id=cid,
                    good_til_us=ctx.clock.gtt_us(arcus_gtt_days()), tick_size=market.tick_size, step_size=market.step_size,
                )
                rec = ProbeOrder(cid, market.market_id, market.ticker, "cancel-race")
                ctx.registry[cid] = rec
                ctx.touched.add(market.market_id)
                ctx.last_place_mono = ctx.mono()
                # The placement draws its ct and reaches its POST before the cancel is built
                # (sleep(0) yields to it), so the cancel's ct is always the newer one.
                place_task = asyncio.create_task(ctx.client.place_order(ctx._auth(), spec))
                await asyncio.sleep(delay_ms / 1000.0)
                cancel_out = await ctx.client.cancel_order(ctx._auth(), CancelSpec(market.market_id, client_id=cid))
                place_out = await place_task
                _note_place(rec, place_out)
                await ctx.sleep(1.5)
                final = await _observe_state(ctx, rec, tap)
                label = race_label(place_out, cancel_out, final)
                cases[f"{delay_ms}ms"].append({"client_id": cid, "place": outcome_dict(place_out), "cancel": outcome_dict(cancel_out), "final_state": final, "final": label})
                if final in ("CANCELED", "FILLED", "REJECTED"):
                    rec.terminal = final
                elif final == "OPEN" or rec.terminal is None:
                    await ctx.cancel_recs([rec])
    finally:
        if tap is not None:
            await tap.close()
    summary = {k: _count_labels(v) for k, v in cases.items()}
    return {"n": n, "summary": summary, "cases": cases}


async def _observe_state(ctx: ProbeCtx, rec: ProbeOrder, tap: WsTap | None) -> str | None:
    """Settled state of one order: the first 200 from ``GET /v1/order`` (a 404
    is the ACK-then-404 window, absent != gone, so keep polling), else the last
    WS event for its clientId, else None."""
    if rec.order_id is not None:
        deadline = ctx.mono() + TERMINAL_WAIT_S
        while True:
            row = await ctx.get_order_row(rec)
            if row is not None:
                return row.state or row.status
            if ctx.mono() >= deadline:
                break
            await ctx.sleep(TERMINAL_POLL_S)
    if tap is not None:
        events = [order_event_state(e) for _, f in tap.frames for e in order_events(f, rec.client_id)]
        return events[-1] if events else None
    return None


def _count_labels(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in rows:
        out[row["final"]] = out.get(row["final"], 0) + 1
    return out


async def sub_ct_order(ctx: ProbeCtx) -> dict[str, Any]:
    """A16: is ``ct`` arrival order enforced, and is a replayed ``ct`` refused?"""
    if ctx.svc.raw_http is None:
        return {"skipped": "raw HTTP client unavailable"}
    market = ctx.market
    quote, why = await ctx.maker_quote(market, Side.BUY)
    if quote is None:
        return {"skipped": why}
    size = probe_size(market, quote.price)
    if size is None:
        return {"skipped": "no probe size"}
    clock = ctx.clock
    if not clock.synced_within(arcus_clock_max_age_s()):
        await clock.sync(ctx.client, lane=Lane.L1_ENGINE, max_wait_s=READ_WAIT_S)
        if not clock.synced_within(arcus_clock_max_age_s()):
            return {"skipped": "clock unsynced"}
    auth = ctx._auth()
    raw = RawSigner(ctx.svc.raw_http, arcus_rest_url(ctx.net), auth, ctx.client, ctx.svc.monotonic)
    recs: list[ProbeOrder] = []
    specs: list[OrderSpec] = []
    for _ in range(2):
        if not await ctx.gate.take():
            return {"stopped": "placement cap reached"}
        cid = ctx.next_client_id()
        spec = OrderSpec(
            market_id=market.market_id, side=quote.side, order_type=WireOrderType.LIMIT, tif=Tif.ALO,
            quantity=size, price=quote.price, reduce_only=False, client_id=cid,
            good_til_us=clock.gtt_us(arcus_gtt_days()), tick_size=market.tick_size, step_size=market.step_size,
        )
        if spec.good_til_us < clock.now_us() + GTT_MIN_AHEAD_US:
            return {"skipped": "gtt too near"}
        rec = ProbeOrder(cid, market.market_id, market.ticker, "ct-order")
        ctx.registry[cid] = rec
        recs.append(rec)
        specs.append(spec)
    ctx.touched.add(market.market_id)
    ct1 = clock.next_ct_ns(auth.api_key_hex)
    ct2 = clock.next_ct_ns(auth.api_key_hex)
    req_a = raw.build_place(specs[0], ct1)  # older ct
    req_b = raw.build_place(specs[1], ct2)  # newer ct
    ctx.last_place_mono = ctx.mono()
    out_b = await raw.send(req_b)
    _note_place(recs[1], out_b)
    await ctx.sleep(0.1)
    ctx.last_place_mono = ctx.mono()
    out_a = await raw.send(req_a)
    _note_place(recs[0], out_a)
    result: dict[str, Any] = {
        "newer_ct_first": outcome_dict(out_b),
        "older_ct_after_newer": outcome_dict(out_a),
        "client_ids": [r.client_id for r in recs],
    }
    ct3 = clock.next_ct_ns(auth.api_key_hex)
    cancel_req = raw.build_cancel(CancelSpec(market.market_id, client_id=recs[1].client_id), ct3)
    first = await raw.send(cancel_req)
    replay = await raw.send(cancel_req)  # the identical bytes, headers and ct
    result["cancel_newer"] = outcome_dict(first)
    result["replayed_ct"] = outcome_dict(replay)
    return result


async def sub_oracle_band(ctx: ProbeCtx) -> dict[str, Any]:
    market = ctx.market
    out: dict[str, Any] = {"threshold_bp": {}, "sides": {}}
    for side in (Side.BUY, Side.SELL):
        fresh = await ctx.refresh_market(market.ticker)
        ok, view = await ctx.bbo(market.ticker)
        if fresh is None or fresh.oracle_price is None or not ok:
            out["sides"][side.value] = {"result": "inconclusive: fresh oracle/BBO unavailable"}
            out["threshold_bp"][side.value.lower()] = None
            continue
        oracle = fresh.oracle_price
        if side is Side.BUY:
            touch = None if view is None else view.ask
            d_cross = Decimal(0) if touch is None else max(Decimal(0), (1 - touch / oracle) * BP)
        else:
            touch = None if view is None else view.bid
            d_cross = Decimal(0) if touch is None else max(Decimal(0), (touch / oracle - 1) * BP)
        lower = max(BAND_FLOOR_BP, math.ceil(d_cross + 10))

        async def try_at(d: int, side: Side = side) -> tuple[str, Decimal | None]:
            m = await ctx.refresh_market(market.ticker)
            ok2, v2 = await ctx.bbo(market.ticker)
            if m is None or m.oracle_price is None or not ok2:
                return "inconclusive:fresh reads denied", None
            price = strict_side_price(m.oracle_price, v2, side, d, m)
            if price is None:
                return "inconclusive:would cross the book", None
            size = probe_size(m, price)
            if size is None:
                return "inconclusive:no probe size", None
            actual = _bp_from(price, m.oracle_price)
            placed = await ctx.place("oracle-band", m, side, price, size)
            if placed.out is None:
                return "inconclusive:placement cap reached", actual
            if isinstance(placed.out, Rejected) and placed.out.error_type == "OracleDeviation":
                return "oracle_deviation", actual
            if placed.accepted and placed.rec is not None:
                await ctx.cancel_recs([placed.rec])
                return "accepted", actual
            return "inconclusive:" + str(placed.info().get("rejection_reason") or placed.info().get("error_type") or placed.info()["outcome"]), actual

        result = await bisect_band(try_at, lower_bp=lower)
        result["d_cross_bp"] = float(d_cross)
        out["sides"][side.value] = result
        out["threshold_bp"][side.value.lower()] = result.get("accepted_max_bp") if result["result"] in ("bracketed", "above_upper") else None
    return out


async def sub_open_order_cap(ctx: ProbeCtx) -> dict[str, Any]:
    market = ctx.market
    tap = await ctx.open_tap([("orders", False)])
    if tap is None:
        return {"skipped": "websocket unavailable (async OPEN_ORDER_CAP_EXCEEDED rejects would be missed)"}
    rungs: list[ProbeOrder] = []
    result: dict[str, Any] = {"cap": None, "probe_open": 0}
    try:
        base = await ctx.client.get_open_orders(ctx.ref, market=None, status=("OPEN", "UNTRIGGERED"), lane=Lane.L1_ENGINE, max_wait_s=READ_WAIT_S)
        if not isinstance(base, Ok):
            return {"skipped": "open-orders read denied (cannot count the baseline)"}
        baseline = len(base.value)
        result["baseline_open"] = baseline
        fresh = await ctx.refresh_market(market.ticker)
        ok, view = await ctx.bbo(market.ticker)
        if fresh is None or fresh.oracle_price is None or not ok:
            return {**result, "skipped": "fresh oracle/BBO unavailable"}
        oracle = fresh.oracle_price
        quote = probe_price(oracle, view, Side.BUY, CAP_RUNG_START_BP, fresh)
        if quote is None:
            return {**result, "skipped": "book crosses the oracle band"}
        side, price = quote
        result["side"] = side.value
        limit_bp = Decimal(ctx.args.far_bp)
        open_count = 0
        stop = "max"
        for _ in range(ctx.args.max):
            if _bp_from(price, oracle) > limit_bp:
                stop = "range exhausted (far_bp)"
                break
            size = probe_size(fresh, price)
            if size is None:
                stop = "no probe size"
                break
            start = len(tap.frames)
            placed = await ctx.place("open-order-cap", fresh, side, price, size)
            if placed.out is None:
                stop = "max"
                break
            if placed.rec is not None:
                rungs.append(placed.rec)
            out = placed.out
            if isinstance(out, Accepted) and out.rejection_reason == "OPEN_ORDER_CAP_EXCEEDED":
                result["cap"] = baseline + open_count
                stop = "cap"
                break
            if not placed.accepted or placed.rec is None:
                stop = "inconclusive: " + json.dumps(placed.info(), default=str)
                break
            hit = await ctx.wait_order_event(tap, placed.rec.client_id, ("OPEN", "REJECTED", "FILLED", "CANCELED"), start=start)
            if hit is None:
                stop = "inconclusive: no OPEN/REJECTED event"
                break
            state = order_event_state(hit[1])
            if state == "OPEN":
                open_count += 1
            elif state == "REJECTED" and hit[1].get("rejectionReason") == "OPEN_ORDER_CAP_EXCEEDED":
                placed.rec.terminal = "REJECTED"
                result["cap"] = baseline + open_count
                stop = "cap"
                break
            else:
                stop = f"inconclusive: {state} {hit[1].get('rejectionReason')}"
                break
            tick = fresh.tick_for_price(price)
            nxt = price - tick if side is Side.BUY else price + tick
            price = fresh.quantize_price(nxt, side)
        result["probe_open"] = open_count
        result["stop"] = stop
        if result["cap"] is None and stop == "max":
            result["cap"] = f">{baseline + open_count}"
    finally:
        if rungs:
            result["cancel"] = await ctx.cancel_recs(rungs)
        await tap.close()
    return result


async def _first_market(ctx: ProbeCtx, predicate: Callable[[ArcusMarket, Decimal], Decimal | None]) -> tuple[ArcusMarket, ProbeQuote, Decimal] | None:
    """First allowlisted market (by id) whose oracle-anchored probe price makes
    ``predicate`` return a size."""
    if not await ctx.refresh_catalog():
        return None
    for market in ctx.svc.catalog.allowlisted():
        if market.oracle_price is None:
            continue
        ok2, view = await ctx.bbo(market.ticker)
        if not ok2:
            continue
        quote = probe_price(market.oracle_price, view, Side.BUY, ctx.args.far_bp, market)
        if quote is None:
            continue
        size = predicate(market, quote.price)
        if size is not None:
            return market, quote, size
    return None


def _size_if_min_size_case(market: ArcusMarket, price: Decimal) -> Decimal | None:
    size = below_min_size_size(market)
    if size is None or size * price < market.min_order_notional:
        return None  # confounded: the notional rule would reject it too
    return size


def _size_if_notional_case(market: ArcusMarket, price: Decimal) -> Decimal | None:
    if market.min_order_size * price >= market.min_order_notional:
        return None
    return below_notional_size(market, price)


async def sub_min_size(ctx: ProbeCtx, taker: bool | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, pred in (("below_min_size", _size_if_min_size_case), ("below_min_notional", _size_if_notional_case)):
        pick = await _first_market(ctx, pred)
        if pick is None:
            out[key] = "n/a"
            continue
        market, quote, size = pick
        placed = await ctx.place("min-size", market, quote.side, quote.price, size)
        out[key] = {"market": market.ticker, "size": size, "price": quote.price, **placed.info()}
        if placed.accepted and placed.rec is not None:
            await ctx.cancel_recs([placed.rec])
    do_taker = ctx.trades if taker is None else (taker and ctx.trades)
    if not do_taker:
        out["reduce_only_dust"] = "not run (needs --i-understand-this-trades)"
        return out
    market = ctx.market
    base = ctx.baseline_positions.get(market.market_id, Decimal(0))
    opened = await ctx.taker_open("min-size", market, Side.BUY)
    dust: dict[str, Any] = {"open": opened}
    after = opened.get("position_after")
    if isinstance(after, Decimal) and after > base:
        fresh = await ctx.refresh_market(market.ticker) or market
        ok, view = await ctx.bbo(market.ticker)
        mark = fresh.mark_price
        price = close_protective_price(fresh, view if ok else None, Side.SELL)
        if mark is not None and price is not None:
            limit_by_notional = fresh.min_order_notional / mark
            cap = min(fresh.min_order_size - fresh.step_size, limit_by_notional, after - base)
            size = fresh.quantize_size_down(max(Decimal(0), cap))
            if size > 0 and size * mark < fresh.min_order_notional and size < fresh.min_order_size:
                placed = await ctx.place("min-size", fresh, Side.SELL, price, size, tif=Tif.IOC, order_type=WireOrderType.MARKET, reduce_only=True)
                dust["reduce_only_dust"] = {"size": size, **placed.info()}
                dust["position_after_dust"] = await ctx._await_position_change(fresh, after)
            else:
                dust["reduce_only_dust"] = "n/a: no size is both below minOrderSize and below minOrderNotional"
        else:
            dust["reduce_only_dust"] = "n/a: no protective price inside the 10% mark band"
    dust["close"] = await ctx.close_delta("min-size", market, base)
    out["reduce_only_dust"] = dust
    return out


async def sub_charged_400(ctx: ProbeCtx) -> dict[str, Any]:
    pick = await _first_market(ctx, _size_if_notional_case)
    if pick is None:
        return {"result": "n/a: no allowlisted market where minOrderSize × price < minOrderNotional"}
    market, quote, size = pick
    before = await ctx.rate_limit()
    placements = []
    accepted = 0
    for _ in range(3):
        placed = await ctx.place("charged-400", market, quote.side, quote.price, size)
        placements.append(placed.info())
        if placed.accepted:
            accepted += 1
    after = await ctx.rate_limit()
    delta = None
    if before is not None and after is not None and before["order"]["used"] is not None and after["order"]["used"] is not None:
        delta = after["order"]["used"] - before["order"]["used"]
    return {
        "market": market.ticker,
        "size": size,
        "price": quote.price,
        "placements": placements,
        "accepted": accepted,
        "rate_limit_before": before,
        "rate_limit_after": after,
        "order_used_delta": delta,
        "note": "delta counts accepted placements too; a reseed between reads makes it negative (A1)",
    }


async def sub_default_leverage(ctx: ProbeCtx) -> dict[str, Any]:
    res = await ctx.client.get_leverages(ctx.ref, lane=Lane.L1_ENGINE, max_wait_s=READ_WAIT_S)
    if not isinstance(res, Ok):
        return {"result": "denied", **outcome_dict(res)}
    by_id = {e.market_id: e for e in res.value}
    out: dict[str, Any] = {"entries": len(res.value)}
    for market in ctx.svc.catalog.allowlisted():
        entry = by_id.get(market.market_id)
        out[market.ticker] = (
            "absent (no entry: the venue default applies)"
            if entry is None
            else {"leverage": entry.leverage, "margin_mode": entry.margin_mode, "max_leverage": market.max_leverage()}
        )
    return out


async def sub_pool_watch(ctx: ProbeCtx) -> dict[str, Any]:
    """Keyless ``GET /v1/rateLimit`` every ``--interval`` s for ``--hours``; JSONL
    to stdout and ``<out>/pool-watch_<stamp>.jsonl``. Ctrl-C safe."""
    out_dir = Path(ctx.args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = _utc_stamp(ctx.svc.wall_time())
    path = out_dir / f"pool-watch_{stamp}.jsonl"
    deadline = ctx.mono() + ctx.args.hours * 3600.0
    prev: dict[str, Any] | None = None
    samples = denied = 0
    reseeds: list[dict[str, Any]] = []
    first = last = None
    interrupted = False
    try:
        while True:
            now_iso = _iso(ctx.svc.wall_time())
            reading = await ctx.rate_limit()
            if reading is None:
                denied += 1
                line: dict[str, Any] = {"t_utc": now_iso, "denied": True}
            else:
                samples += 1
                line = {"t_utc": now_iso, **reading}
                if prev is not None:
                    for pool in ("order", "cancel"):
                        was, now_used = prev[pool]["used"], reading[pool]["used"]
                        if was is not None and now_used is not None and now_used < was:
                            reseeds.append({"t_utc": now_iso, "pool": pool, "used_before": was, "used_after": now_used, "cap": reading[pool]["cap"]})
                prev = reading
                first = first or line
                last = line
            text = json.dumps(line, sort_keys=True)
            print(text, flush=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(text + "\n")
            if ctx.mono() + ctx.args.interval > deadline:
                break
            await ctx.sleep(ctx.args.interval)
    except asyncio.CancelledError:
        interrupted = True
    return {"samples": samples, "denied": denied, "reseed_events": reseeds, "first": first, "last": last, "jsonl": path.name, "interrupted": interrupted}


async def sub_ws_fresh(ctx: ProbeCtx) -> dict[str, Any]:
    tap = await ctx.open_tap([("account", True), ("positions", True)])
    if tap is None:
        return {"skipped": "websocket unavailable"}
    market = ctx.market
    out: dict[str, Any] = {}
    try:
        t_start = ctx.mono()
        await ctx.sleep(0)  # let the reader run
        wait_until = asyncio.get_running_loop().time() + max(20.0, ctx.svc.ws_wait_s * 2)
        def account_snaps() -> list[float]:
            return [t for t, f in tap.frames if f.get("channel") == "account" and is_snapshot_frame(f)]

        while asyncio.get_running_loop().time() < wait_until:
            if len(account_snaps()) >= 4:
                break
            await asyncio.sleep(0.5)
        snaps = account_snaps()
        gaps = [b - a for a, b in zip(snaps, snaps[1:])]
        out["account_resnapshot_s"] = {"p50": percentile(gaps, 50), "samples": len(gaps)}
        out["observed_s"] = ctx.mono() - t_start
        if not ctx.trades:
            out["positions_lead_ms"] = "not run (needs --i-understand-this-trades)"
            return out
        base = ctx.baseline_positions.get(market.market_id, Decimal(0))
        leads: dict[str, Any] = {}
        start = len(tap.frames)
        opened = await ctx.taker_open("ws-fresh", market, Side.BUY)
        out["open"] = opened
        leads["open"] = await _lead_ms(ctx, tap, market.market_id, start, lambda s: s != base)
        start = len(tap.frames)
        out["close"] = await ctx.close_delta("ws-fresh", market, base)
        leads["close"] = await _lead_ms(ctx, tap, market.market_id, start, lambda s: s == base)
        out["positions_lead_ms"] = leads
    finally:
        await tap.close()
    return out


async def _lead_ms(ctx: ProbeCtx, tap: WsTap, market_id: int, start: int, cond: Callable[[Decimal], bool]) -> dict[str, Any]:
    def seen(channel: str) -> Callable[[Mapping[str, Any]], bool]:
        def pred(frame: Mapping[str, Any]) -> bool:
            if frame.get("channel") != channel:
                return False
            size = frame_position_size(frame, market_id)
            return size is not None and cond(size)

        return pred

    t_pos = await tap.wait_for(seen("positions"), 15.0, start=start)
    t_acc = await tap.wait_for(seen("account"), 15.0, start=start)
    lead = None if t_pos is None or t_acc is None else (t_acc[0] - t_pos[0]) * 1000.0
    return {"positions_seen": t_pos is not None, "account_seen": t_acc is not None, "lead_ms": lead}


async def _our_fills(ctx: ProbeCtx, market: ArcusMarket, t0_us: int, order_ids: set[str]) -> tuple[list[Any], str]:
    res = await ctx.client.get_fills(ctx.ref, market=market.ticker, from_us=t0_us, to_us=None, limit=1000, lane=Lane.L1_ENGINE, max_wait_s=READ_WAIT_S)
    if not isinstance(res, Ok):
        return [], "denied"
    if order_ids:
        return [f for f in res.value if f.order_id in order_ids], "order_id"
    return list(res.value), "window"


def _probe_order_ids(ctx: ProbeCtx, sub: str) -> tuple[set[str], set[str]]:
    oids = {r.order_id for r in ctx.registry.values() if r.sub == sub and r.order_id}
    cids = {r.client_id for r in ctx.registry.values() if r.sub == sub}
    return {o for o in oids if o}, cids


async def sub_tradeid_parity(ctx: ProbeCtx) -> dict[str, Any]:
    market = ctx.market
    tap = await ctx.open_tap([("userFills", False)])
    if tap is None:
        return {"skipped": "websocket unavailable"}
    base = ctx.baseline_positions.get(market.market_id, Decimal(0))
    t0_us = ctx.clock.now_us() - 1_000_000
    out: dict[str, Any] = {}
    try:
        out["open"] = await ctx.taker_open("tradeid-parity", market, Side.BUY)
        await ctx.sleep(3.0)
        out["close"] = await ctx.close_delta("tradeid-parity", market, base)
        await ctx.sleep(3.0)
        await asyncio.sleep(0.2)
        oids, cids = _probe_order_ids(ctx, "tradeid-parity")
        ws_ids = sorted({tid for _, f in tap.frames for tid in fill_trade_ids(f, oids, cids)})
        fills, matched_by = await _our_fills(ctx, market, t0_us, oids)
        rest_ids = sorted({f.trade_id for f in fills})
        out.update({
            "ws_trade_ids": ws_ids,
            "rest_trade_ids": rest_ids,
            "rest_matched_by": matched_by,
            "equal": bool(ws_ids) and ws_ids == rest_ids,
            "ws_only": sorted(set(ws_ids) - set(rest_ids)),
            "rest_only": sorted(set(rest_ids) - set(ws_ids)),
        })
    finally:
        await tap.close()
    return out


def _sign(value: Decimal) -> str:
    return "+" if value > 0 else "-" if value < 0 else "0"


async def sub_fee_sign(ctx: ProbeCtx) -> dict[str, Any]:
    market = ctx.market
    base = ctx.baseline_positions.get(market.market_id, Decimal(0))
    t0_us = ctx.clock.now_us() - 1_000_000
    out: dict[str, Any] = {"open": await ctx.taker_open("fee-sign", market, Side.BUY)}
    out["close"] = await ctx.close_delta("fee-sign", market, base)
    await ctx.sleep(2.0)
    oids, _ = _probe_order_ids(ctx, "fee-sign")
    fills, matched_by = await _our_fills(ctx, market, t0_us, oids)
    signs: dict[str, Any] = {}
    for f in fills:
        signs.setdefault(f.role, []).append(_sign(f.fee))
    out["own_fills"] = {role: sorted(set(v)) for role, v in signs.items()}
    out["matched_by"] = matched_by
    hist = await ctx.client.get_fills(ctx.ref, market=None, from_us=None, to_us=None, limit=1000, lane=Lane.L1_ENGINE, max_wait_s=READ_WAIT_S)
    maker = None
    if isinstance(hist, Ok):
        maker_rows = [f for f in hist.value if f.role == "MAKER"]
        if maker_rows:
            maker = _sign(maker_rows[0].fee)
    result = {"TAKER": ",".join(out["own_fills"].get("TAKER", [])) or None, "MAKER": maker or "none observed"}
    out["fee_sign"] = result
    return out


async def sub_entry_units(ctx: ProbeCtx) -> dict[str, Any]:
    market = ctx.market
    base = ctx.baseline_positions.get(market.market_id, Decimal(0))
    t0_us = ctx.clock.now_us() - 1_000_000
    out: dict[str, Any] = {"open": await ctx.taker_open("entry-units", market, Side.BUY)}
    await ctx.sleep(2.0)
    res = await ctx.client.get_positions(ctx.ref, market=market.ticker, lane=Lane.L1_ENGINE, max_wait_s=READ_WAIT_S)
    oids, _ = _probe_order_ids(ctx, "entry-units")
    fills, matched_by = await _our_fills(ctx, market, t0_us, oids)
    entry = None
    if isinstance(res, Ok) and market.market_id in res.value:
        entry = res.value[market.market_id].average_entry_price
    size_sum = sum((f.size for f in fills), Decimal(0))
    vwap = None if size_sum == 0 else sum((f.size * f.price for f in fills), Decimal(0)) / size_sum
    ratio = None if entry is None or vwap is None or vwap == 0 else entry / vwap
    out.update({
        "average_entry_price": entry,
        "fill_vwap": vwap,
        "ratio": ratio,
        "scale_suspect": None if ratio is None else not (Decimal("0.5") < ratio < Decimal(2)),
        "matched_by": matched_by,
    })
    out["close"] = await ctx.close_delta("entry-units", market, base)
    return out


async def _order_frames(ctx: ProbeCtx, tap: WsTap, cid: str, start: int, wait_s: float = 5.0) -> list[dict[str, Any]]:
    await tap.wait_for(lambda f: any(order_event_state(e) in TERMINAL_STATES | {"PARTIALLY_FILLED"} for e in order_events(f, cid)), wait_s, start=start)
    frames = []
    for _, f in tap.frames[start:]:
        for e in order_events(f, cid):
            frames.append({k: e.get(k) for k in ("status", "state", "rejectionReason", "remainingSize", "originalSize", "positionEffect") if k in e})
    return frames


async def sub_ioc_reduce_only(ctx: ProbeCtx) -> dict[str, Any]:
    market = ctx.market
    base = ctx.baseline_positions.get(market.market_id, Decimal(0))
    tap = await ctx.open_tap([("orders", False)])
    if tap is None:
        return {"skipped": "websocket unavailable"}
    out: dict[str, Any] = {}
    try:
        # (a) MARKET+IOC+reduceOnly at mark − 9 % closing our own tiny long.
        opened = await ctx.taker_open("ioc-reduce-only", market, Side.BUY)
        out["open_a"] = opened
        pos = opened.get("position_after")
        if isinstance(pos, Decimal) and pos > base:
            fresh = await ctx.refresh_market(market.ticker) or market
            price = mark_offset_price(fresh, Side.SELL, MARK_FALLBACK_BP)
            if price is None:
                out["market_reduce_only"] = "n/a: no mark price"
            else:
                start = len(tap.frames)
                placed = await ctx.place("ioc-reduce-only", fresh, Side.SELL, price, pos - base, tif=Tif.IOC, order_type=WireOrderType.MARKET, reduce_only=True)
                frames = [] if placed.rec is None else await _order_frames(ctx, tap, placed.rec.client_id, start)
                out["market_reduce_only"] = {**placed.info(), "ws": frames, "position_after": await ctx._await_position_change(fresh, pos)}
        # (b) oversize reduce-only (2 × position).
        out["reset_b"] = await ctx.close_delta("ioc-reduce-only", market, base)
        opened_b = await ctx.taker_open("ioc-reduce-only", market, Side.BUY)
        out["open_b"] = opened_b
        pos_b = opened_b.get("position_after")
        if isinstance(pos_b, Decimal) and pos_b > base:
            fresh = await ctx.refresh_market(market.ticker) or market
            price = mark_offset_price(fresh, Side.SELL, MARK_FALLBACK_BP)
            if price is not None:
                start = len(tap.frames)
                placed = await ctx.place("ioc-reduce-only", fresh, Side.SELL, price, (pos_b - base) * 2, tif=Tif.IOC, order_type=WireOrderType.MARKET, reduce_only=True)
                frames = [] if placed.rec is None else await _order_frames(ctx, tap, placed.rec.client_id, start)
                out["oversize_reduce_only"] = {**placed.info(), "ws": frames, "position_after": await ctx._await_position_change(fresh, pos_b)}
        out["reset_c"] = await ctx.close_delta("ioc-reduce-only", market, base)
        # (c) zero-fill LIMIT+IOC far from the touch (non-crossing, opening, cannot fill).
        quote, why = await ctx.maker_quote(market, Side.BUY)
        if quote is None:
            out["zero_fill_ioc"] = why
        else:
            size = probe_size(market, quote.price)
            if size is not None:
                start = len(tap.frames)
                placed = await ctx.place("ioc-reduce-only", market, quote.side, quote.price, size, tif=Tif.IOC)
                frames = [] if placed.rec is None else await _order_frames(ctx, tap, placed.rec.client_id, start)
                out["zero_fill_ioc"] = {**placed.info(), "ws": frames}
    finally:
        out["close"] = await ctx.close_delta("ioc-reduce-only", market, base)
        await tap.close()
    return out


async def sub_alo_reduce_only(ctx: ProbeCtx) -> dict[str, Any]:
    market = ctx.market
    base = ctx.baseline_positions.get(market.market_id, Decimal(0))
    fresh = await ctx.refresh_market(market.ticker)
    ok, view = await ctx.bbo(market.ticker)
    if fresh is None or fresh.oracle_price is None or not ok:
        return {"skipped": "fresh oracle/BBO unavailable"}
    close_side = None
    for side in (Side.SELL, Side.BUY):
        if strict_side_price(fresh.oracle_price, view, side, ctx.args.far_bp, fresh) is not None:
            close_side = side
            break
    if close_side is None:
        return {"skipped": "no non-crossing reduce-only side inside the band"}
    open_side = Side.BUY if close_side is Side.SELL else Side.SELL
    tap = await ctx.open_tap([("orders", False)])
    if tap is None:
        return {"skipped": "websocket unavailable"}
    out: dict[str, Any] = {"close_side": close_side.value}
    try:
        opened = await ctx.taker_open("alo-reduce-only", market, open_side)
        out["open"] = opened
        pos = opened.get("position_after")
        if isinstance(pos, Decimal) and pos != base:
            m2 = await ctx.refresh_market(market.ticker) or fresh
            ok2, v2 = await ctx.bbo(market.ticker)
            price = None if m2.oracle_price is None or not ok2 else strict_side_price(m2.oracle_price, v2, close_side, ctx.args.far_bp, m2)
            if price is None:
                out["reduce_only_alo"] = "skipped: the closing side now crosses"
            else:
                start = len(tap.frames)
                placed = await ctx.place("alo-reduce-only", m2, close_side, price, abs(pos - base), reduce_only=True)
                ws_state = None
                if placed.rec is not None and placed.accepted:
                    hit = await ctx.wait_order_event(tap, placed.rec.client_id, ("OPEN", "REJECTED", "CANCELED", "FILLED"), start=start)
                    ws_state = None if hit is None else {"state": order_event_state(hit[1]), "rejectionReason": hit[1].get("rejectionReason")}
                row = None if placed.rec is None else await ctx.get_order_row(placed.rec)
                out["reduce_only_alo"] = {
                    **placed.info(),
                    "ws": ws_state,
                    "rest_state": None if row is None else (row.state or row.status),
                    "accepted_and_open": bool(ws_state and ws_state["state"] == "OPEN"),
                }
                if placed.rec is not None and placed.accepted:
                    out["cancel"] = await ctx.cancel_recs([placed.rec])
    finally:
        out["close"] = await ctx.close_delta("alo-reduce-only", market, base)
        await tap.close()
    return out


async def sub_alo_cross(ctx: ProbeCtx) -> dict[str, Any]:
    market = ctx.market
    tap = await ctx.open_tap([("orders", False)])
    if tap is None:
        return {"skipped": "websocket unavailable"}
    base = ctx.baseline_positions.get(market.market_id, Decimal(0))
    out: dict[str, Any] = {}
    try:
        before = await ctx.rate_limit()
        fresh = await ctx.refresh_market(market.ticker)
        ok, view = await ctx.bbo(market.ticker)
        if fresh is None or not ok or view is None or view.ask is None:
            return {"skipped": "no fresh best ask"}
        price = fresh.quantize_price(view.ask, Side.BUY, crossing=True)  # at (or one tick above) the ask
        size = probe_size(fresh, price)
        if size is None:
            return {"skipped": "no probe size"}
        start = len(tap.frames)
        placed = await ctx.place("alo-cross", fresh, Side.BUY, price, size)
        frames = [] if placed.rec is None else await _order_frames(ctx, tap, placed.rec.client_id, start)
        after = await ctx.rate_limit()
        charged = None
        if before and after and before["order"]["used"] is not None and after["order"]["used"] is not None:
            charged = after["order"]["used"] - before["order"]["used"]
        out.update({"price": price, "size": size, **placed.info(), "ws": frames, "order_units_charged": charged})
        if placed.rec is not None and placed.rec.terminal is None and any(f.get("state") == "OPEN" or f.get("status") == "OPEN" for f in frames):
            out["rested_then_cancel"] = await ctx.cancel_recs([placed.rec])
    finally:
        out["close"] = await ctx.close_delta("alo-cross", market, base)
        await tap.close()
    return out


SUB_FUNCS: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
    "sign-check": sub_sign_check,
    "ack-latency": sub_ack_latency,
    "ack-404-window": sub_ack_404_window,
    "cancel-race": sub_cancel_race,
    "oracle-band": sub_oracle_band,
    "open-order-cap": sub_open_order_cap,
    "min-size": sub_min_size,
    "pool-watch": sub_pool_watch,
    "ws-fresh": sub_ws_fresh,
    "tradeid-parity": sub_tradeid_parity,
    "fee-sign": sub_fee_sign,
    "entry-units": sub_entry_units,
    "ct-order": sub_ct_order,
    "charged-400": sub_charged_400,
    "default-leverage": sub_default_leverage,
    "ioc-reduce-only": sub_ioc_reduce_only,
    "alo-reduce-only": sub_alo_reduce_only,
    "alo-cross": sub_alo_cross,
}


def mark_confounded(results: dict[str, Any]) -> None:
    """C18: a cancel-race result is confounded when an older ``ct`` is not
    accepted after a newer one (arrival order would then decide the race)."""
    ct = results.get("ct-order")
    race = results.get("cancel-race")
    if not isinstance(race, dict):
        return
    older = ct.get("older_ct_after_newer") if isinstance(ct, dict) else None
    if not (isinstance(older, dict) and older.get("outcome") == "Accepted"):
        race["confounded"] = True


# --- cleanup ----------------------------------------------------------------------------------------


async def cleanup(ctx: ProbeCtx) -> dict[str, Any]:
    """Always runs. Cancel every probe clientId not known terminal (by id, batch
    <= 100; never cancel-all), re-read open orders by the run prefix, up to 3
    rounds; then compare positions with the baseline and flatten only a
    probe-created delta when trading was consented. Never raises."""
    report: dict[str, Any] = {"rounds": [], "remaining_probe_orders": [], "position_delta": {}, "complete": False}
    if ctx.auth is None or not ctx.registry:
        report["complete"] = True
        return report
    remaining: list[OrderRow] | None = None
    for _round in range(CLEANUP_ROUNDS):
        pending = [r for r in ctx.registry.values() if r.terminal is None]
        if remaining:
            seen = {r.client_id for r in pending}
            for row in remaining:
                cid = row.client_id or ""
                rec = ctx.registry.get(cid)
                if rec is None:  # carries this run's prefix, so it is ours
                    rec = ProbeOrder(cid, row.market_id, row.ticker, "cleanup", order_id=row.order_id)
                    ctx.registry[cid] = rec
                rec.terminal = None
                if cid not in seen:
                    seen.add(cid)
                    pending.append(rec)
        entry: dict[str, Any] = {"cancel": await ctx.cancel_recs(pending) if pending else []}
        since = None if ctx.last_place_mono is None else ctx.mono() - ctx.last_place_mono
        wait = max(CLEANUP_SETTLE_S, CLEANUP_GRACE_S - (since or 0.0))
        await ctx.sleep(wait)
        read = await ctx.client.get_open_orders(ctx.ref, market=None, status=("OPEN", "UNTRIGGERED"), lane=Lane.L0_BRAKE, max_wait_s=READ_WAIT_S)
        if not isinstance(read, Ok):
            entry["open_orders"] = "denied: " + type(read).__name__
            remaining = None
            report["rounds"].append(entry)
            continue
        remaining = [row for row in read.value if row.client_id and row.client_id.startswith(ctx.prefix)]
        entry["open_orders_with_run_prefix"] = len(remaining)
        report["rounds"].append(entry)
        if not remaining:
            break
    if remaining is None:
        report["remaining_probe_orders"] = "unknown (open-orders read denied)"
    else:
        report["remaining_probe_orders"] = [{"client_id": r.client_id, "market": r.ticker} for r in remaining]
    deltas_ok = True
    for market_id in sorted(ctx.touched):
        market = ctx.svc.catalog.get(market_id)
        if market is None:
            report["position_delta"][str(market_id)] = "unknown (no market metadata)"
            deltas_ok = False
            continue
        base = ctx.baseline_positions.get(market_id, Decimal(0))
        cur = await ctx.position_size(market, Lane.L0_BRAKE)
        if cur is None:
            report["position_delta"][market.ticker] = "unknown (positions read denied)"
            deltas_ok = False
            continue
        delta = cur - base
        if delta != 0 and ctx.flatten_allowed:
            report.setdefault("flatten", {})[market.ticker] = await ctx.close_delta("cleanup", market, base, Lane.L0_BRAKE)
            cur2 = await ctx.position_size(market, Lane.L0_BRAKE)
            delta = (cur2 - base) if cur2 is not None else delta
        report["position_delta"][market.ticker] = str(delta)
        if delta != 0:
            deltas_ok = False
    report["complete"] = isinstance(report["remaining_probe_orders"], list) and not report["remaining_probe_orders"] and deltas_ok
    return report


# --- orchestration ---------------------------------------------------------------------------------


def _cancel_on_sigterm() -> Callable[[], None]:
    """SIGTERM cancels the running probe task exactly like Ctrl-C (a background
    ``nohup … &`` job may have SIGINT ignored), so cleanup and the report run.
    Returns the undo (restores the previous handler, not just SIG_DFL)."""
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    if task is None:
        return lambda: None
    try:
        previous = signal.getsignal(signal.SIGTERM)
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
    except (NotImplementedError, RuntimeError, ValueError):  # non-main thread / platform
        return lambda: None

    def restore() -> None:
        try:
            loop.remove_signal_handler(signal.SIGTERM)
            signal.signal(signal.SIGTERM, previous if previous is not None else signal.SIG_DFL)
        except (NotImplementedError, RuntimeError, ValueError):
            pass

    return restore


def _utc_stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _git_info() -> dict[str, Any]:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=_ROOT, capture_output=True, text=True, timeout=5)
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=_ROOT, capture_output=True, text=True, timeout=5)
        return {"git_sha": sha.stdout.strip() or "unknown", "dirty": bool(dirty.stdout.strip())}
    except Exception:
        return {"git_sha": "unknown", "dirty": None}


async def _preflight(args: argparse.Namespace, raw_key: str, services_factory: Callable[[str], ProbeServices] | None) -> tuple[ProbeCtx, str | None, str | None]:
    """Steps 1–8 of 02 §11.2 (keyless: 1, 5, 7 + the address). Raises Refused."""
    sub = args.subcommand
    keyless = sub in KEYLESS
    fly_guard(args.allow_fly)
    try:
        net = parse_arcus_net(env_str("ARCUS_PROBE_NETWORK"))
    except ValueError:
        raise Refused("testnet only: set ARCUS_PROBE_NETWORK to the Arcus testnet") from None
    testnet_guard(net)
    try:
        address = normalize_address(env_str("ARCUS_PROBE_ADDRESS"))
    except ValueError:
        raise Refused("ARCUS_PROBE_ADDRESS must be a 0x-prefixed 40-hex address") from None
    if sub in TAKER_ONLY and not args.trades:
        raise Refused(f"{sub} trades a tiny testnet position: pass --i-understand-this-trades")
    seed: str | None = None
    if not keyless:
        seed = normalize_seed_hex(raw_key)
        if seed is None:
            raise Refused("ARCUS_PROBE_SIGNING_KEY is not a 64-hex API Signing Key (value not shown)")
        if is_wallet_key(seed, address):
            raise Refused(WALLET_KEY_REFUSAL)
    svc = (services_factory or default_services)(net)
    try:
        ref = ArcusAccountRef(net, address, 0)
        auth = None
        fingerprint = None
        if seed is not None:
            signer = svc.make_signer(seed)
            auth = make_auth(ref, signer)
            fingerprint = key_fingerprint(signer.public_key_hex)
            print(f"probe key fingerprint {fingerprint}", flush=True)
        client, catalog = svc.client, svc.catalog
        if await client.clock.sync(client, lane=Lane.L1_ENGINE, max_wait_s=READ_WAIT_S) is None:
            raise Refused("clock sync failed (GET /v1/time denied)")
        if not await catalog.refresh(client, lane=Lane.L1_ENGINE, max_wait_s=READ_WAIT_S):
            raise Refused("market catalog load failed (GET /v1/markets denied)")
        market = catalog.by_ticker(args.market)
        if market is None or market not in catalog.allowlisted():
            raise Refused(f"--market {args.market} is not an allowlisted ONLINE Arcus market")
        if auth is not None:
            if market.oracle_price is None:
                raise Refused(f"{market.ticker} has no oracle price")
            keys = await client.get_api_keys(address, account_index=None, lane=Lane.L1_ENGINE, max_wait_s=READ_WAIT_S)
            if not isinstance(keys, Ok):
                raise Refused("cannot verify the key (GET /v1/apiKeys denied)")
            entry = next((e for e in keys.value if e.api_key == auth.api_key_hex), None)
            now_ms = client.clock.now_us() // 1000
            if entry is None:
                raise Refused("this signing key is not listed for ARCUS_PROBE_ADDRESS")
            if entry.status != "ACTIVE":
                raise Refused("this signing key is not ACTIVE")
            if entry.valid_until_ms != 0 and entry.valid_until_ms <= now_ms + KEY_MIN_VALIDITY_MS:
                raise Refused("this signing key expires within 24 h")
            if not entry.covers_account(0):
                raise Refused("this signing key does not cover subaccount 0")
            if "withdraw" in entry.permissions:
                raise Refused("use a trade-only key for probing (this key can withdraw)")
        account = await client.get_account(ref, lane=Lane.L1_ENGINE, max_wait_s=READ_WAIT_S)
        if isinstance(account, NoActivity):
            raise Refused("fund the testnet account first (Testnet Deposit)")
        if isinstance(account, Forbidden) and account.kind == "whitelist":
            raise Refused("address not whitelisted on Arcus testnet")
        if not isinstance(account, Ok):
            raise Refused(f"account read denied ({type(account).__name__})")
        ctx = ProbeCtx(
            args=args,
            svc=svc,
            net=net,
            ref=ref,
            auth=auth,
            market=market,
            run_id=int(svc.wall_time()),
            gate=PlacementGate(args.max, monotonic=svc.monotonic, sleep=svc.sleep, spacing_s=svc.placement_spacing_s),
        )
        if auth is not None:
            opened = await client.get_open_orders(ref, market=None, status=("OPEN",), lane=Lane.L1_ENGINE, max_wait_s=READ_WAIT_S)
            positions = await client.get_positions(ref, market=None, lane=Lane.L1_ENGINE, max_wait_s=READ_WAIT_S)
            if not isinstance(opened, Ok) or not isinstance(positions, Ok):
                raise Refused("cannot read the baseline (open orders / positions denied)")
            ctx.baseline_open = {"count": len(opened.value), "bot_prefixed": sum(1 for r in opened.value if (r.client_id or "").startswith("nb"))}
            ctx.baseline_positions = {mid: row.size for mid, row in positions.value.items()}
            wants_trades = sub in TAKER_ONLY or (sub in TAKER_OPTIONAL and args.trades)
            if wants_trades:
                if ctx.baseline_positions.get(market.market_id, Decimal(0)) != 0:
                    raise Refused(f"trading subcommands need a FLAT {market.ticker} position")
                ok, view = await ctx.bbo(market.ticker)
                reason = "BBO read denied" if not ok else taker_preflight_reason(view, market)
                if reason is not None:
                    raise Refused(f"taker preflight: {reason}; nothing traded")
                ctx.trades = True
                ctx.flatten_allowed = True
        return ctx, fingerprint, seed
    except BaseException:
        await svc.aclose()
        raise


async def _amain(args: argparse.Namespace, services_factory: Callable[[str], ProbeServices] | None = None) -> int:
    sub = args.subcommand
    # The key never outlives this block in os.environ (keyless: never even read).
    raw_key = "" if sub in KEYLESS else env_str("ARCUS_PROBE_SIGNING_KEY")
    os.environ.pop("ARCUS_PROBE_SIGNING_KEY", None)
    started = datetime.now(timezone.utc).timestamp()
    try:
        ctx, fingerprint, seed = await _preflight(args, raw_key, services_factory)
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr, flush=True)
        return 2
    finally:
        raw_key = ""
    started = ctx.svc.wall_time()
    pub = None if ctx.auth is None else ctx.auth.api_key_hex
    restore_sigterm = _cancel_on_sigterm()  # `kill <pid>` behaves like one Ctrl-C: cleanup + report
    results: dict[str, Any] = {}
    error: dict[str, Any] | None = None
    interrupted = False
    cleanup_report: dict[str, Any] = {"complete": True}
    try:
        try:
            if sub == "all":
                for name, opts in ALL_SEQUENCE:
                    ctx.gate.start()
                    results[name] = await SUB_FUNCS[name](ctx, **opts)
                mark_confounded(results)
            else:
                ctx.gate.start()
                results[sub] = await SUB_FUNCS[sub](ctx)
        except asyncio.CancelledError:
            interrupted = True
        except Exception as exc:
            error = {"type": type(exc).__name__, "traceback": redact_sensitive_text(traceback.format_exc())}
        finally:
            try:
                cleanup_report = await cleanup(ctx)
            except Exception as exc:  # cleanup must never hide what is left
                cleanup_report = {"complete": False, "error": type(exc).__name__, "remaining_probe_orders": "unknown"}
        code = 3 if not cleanup_report.get("complete") else (1 if error or interrupted else 0)
        report = {
            "probe": sub,
            "network": ctx.net,
            "run_id": ctx.run_id,
            "market": ctx.market.ticker,
            "key_fingerprint": fingerprint,
            "started_utc": _iso(started),
            "finished_utc": _iso(ctx.svc.wall_time()),
            "library": _git_info(),
            "placements": ctx.gate.total,
            "baseline_open_orders": ctx.baseline_open,
            "steps": list(results),  # execution order (the JSON keys are sorted)
            "results": results if sub == "all" else results.get(sub),
            "cleanup": cleanup_report,
            "schema_error_counts": schema_error_counts(),
            "interrupted": interrupted,
            "error": None if error is None else {"type": error["type"]},
            "exit_code": code,
        }
        secrets = [s for s in (seed, pub) if s]
        clean = redact_report(report, address=ctx.ref.address, secrets=secrets)
        text = json.dumps(clean, indent=1, sort_keys=True, default=str)
        assert_report_clean(text, address=ctx.ref.address, secrets=secrets)
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{sub}_{_utc_stamp(started)}.json"
        path.write_text(text + "\n", encoding="utf-8")
        print(text, flush=True)
        print(f"report written: {path}", flush=True)
        if error is not None:
            print(error["traceback"], file=sys.stderr, flush=True)
        if code == 3:
            left = cleanup_report.get("remaining_probe_orders")
            print(
                "PROBE CLEANUP INCOMPLETE — cancel these in the Arcus app and close any probe position: "
                f"orders={left} position_delta={cleanup_report.get('position_delta')}",
                file=sys.stderr,
                flush=True,
            )
        return code
    finally:
        restore_sigterm()
        await ctx.svc.aclose()


def main(argv: list[str] | None = None, *, services_factory: Callable[[str], ProbeServices] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_amain(args, services_factory))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr, flush=True)
        return 1
    except Exception:
        print(redact_sensitive_text(traceback.format_exc()), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    import logging

    from src.nadobro.core.log_redaction import RedactingFormatter

    _handler = logging.StreamHandler()
    _handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=logging.WARNING, handlers=[_handler])
    raise SystemExit(main())
