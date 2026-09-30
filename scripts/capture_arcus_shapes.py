#!/usr/bin/env python3
"""Capture Arcus response shapes with KEYLESS public GETs (02 §11.1).

Anyone may run it: it never reads a signing key, never signs, never writes to
the venue — every call is an unauthenticated ``GET`` spending IP weight on the
L3 background lane (sequential; total weight ≈ 200 of the 1,500 bucket).

    .venv/bin/python scripts/capture_arcus_shapes.py [--network testnet|mainnet] [--market BTC-USD]
                                                     [--address 0x…] [--out DIR] [--allow-fly]

Public endpoints: ``/``, ``/health``, ``/v1/time``, ``/v1/markets``, ``/v1/mids``,
``/v1/prices``, ``/v1/bbo/{m}``, ``/v1/l2OrderBook/{m}?nLevels=20``,
``/v1/candles`` (1m, countback 5), ``/v1/compliance``, ``/v1/feetiers``. With
``--address`` also the account-scoped reads (``account``, ``positions``,
``openOrders``, ``fills``, ``funding``, ``rateLimit``, ``leverages`` with
``accountIndex=0``; ``apiKeys``) and ``/v1/compliance?address=``.

Output (``--out``, default ``./arcus_capture_<net>_<utc>/``): one JSON file per
endpoint plus ``report.json`` — per endpoint the HTTP status, whether the typed
parser accepted the body, the top-level keys the docs do not list
(``unknown_fields``) and the required keys that are missing
(``missing_required``); ``candles_order``; ``schema_error_counts``.

Redaction of every saved file: the requested address (any case) becomes
0x000000000000000000000000000000000000dead; compliance ``geo.country`` /
``geo.region`` become ``"XX"`` (they describe the CALLER's egress IP). The
script never commits anything: copy chosen files into
``tests/fixtures/arcus/captured/`` by hand.

Refuses to run on a Fly machine (``FLY_APP_NAME`` / ``FLY_MACHINE_ID`` set)
unless ``--allow-fly``: it would spend the production egress IP's bucket.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.nadobro.core.log_redaction import redact_sensitive_text  # noqa: E402
from src.nadobro.utils.env import env_str  # noqa: E402
from src.nadobro.utils.venue_scope import ARCUS_NETWORK_TESTNET, parse_arcus_net  # noqa: E402
from src.nadobro.venue.arcus.budget import IpBudget  # noqa: E402
from src.nadobro.venue.arcus.catalog import parse_market  # noqa: E402
from src.nadobro.venue.arcus.client import ArcusClient  # noqa: E402
from src.nadobro.venue.arcus.clock import ArcusClock  # noqa: E402
from src.nadobro.venue.arcus.errors import (  # noqa: E402
    ArcusSchemaError,
    Forbidden,
    LocalDenied,
    NoActivity,
    NotFound,
    Ok,
    Throttled,
    Unauthorized,
    Unavailable,
    schema_error_counts,
)
from src.nadobro.venue.arcus.parse import (  # noqa: E402
    FIELD_SETS,
    REQUIRED_FIELDS,
    parse_account,
    parse_api_keys,
    parse_bbo,
    parse_candles,
    parse_compliance,
    parse_fills_payload,
    parse_funding_payload,
    parse_l2,
    parse_leverages,
    parse_markets_payload,
    parse_mids,
    parse_open_orders_payload,
    parse_positions_payload,
    parse_prices,
    parse_rate_limit,
    parse_time,
)
from src.nadobro.venue.arcus.types import TICKER_RE, ArcusAccountRef, Lane, normalize_address  # noqa: E402

DEAD_ADDRESS = "0x000000000000000000000000000000000000dead"
LANE = Lane.L3_BACKGROUND
MAX_WAIT_S = 30.0


# --- argument parsing ---------------------------------------------------------------------------


def _network(text: str) -> str:
    try:
        return parse_arcus_net(text)
    except ValueError:
        raise argparse.ArgumentTypeError("network must be testnet or mainnet") from None


def _ticker(text: str) -> str:
    value = text.strip().upper()
    if not TICKER_RE.match(value):
        raise argparse.ArgumentTypeError("invalid market ticker")
    return value


def _address(text: str) -> str:
    try:
        return normalize_address(text)
    except ValueError:
        raise argparse.ArgumentTypeError("address must be 0x + 40 hex") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="capture_arcus_shapes.py", description="Keyless public-GET Arcus shape capture.")
    parser.add_argument("--network", type=_network, default=ARCUS_NETWORK_TESTNET)
    parser.add_argument("--market", type=_ticker, default="BTC-USD")
    parser.add_argument("--address", type=_address, default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--allow-fly", action="store_true")
    return parser


# --- endpoint table -------------------------------------------------------------------------------


def _rows(container: str) -> Callable[[object], list[Any]]:
    def get(body: object) -> list[Any]:
        rows = body.get(container) if isinstance(body, Mapping) else None
        if isinstance(rows, list):
            return rows
        if isinstance(rows, Mapping):
            return list(rows.values())
        return []

    return get


def _itself(body: object) -> list[Any]:
    return [body] if isinstance(body, Mapping) else []


def _parse_all_markets(body: object) -> object:
    rows = parse_markets_payload(body)
    return [parse_market(row) for row in rows]


@dataclass(frozen=True)
class Endpoint:
    name: str
    path_key: str
    path: str
    params: Mapping[str, str] | None
    parse: Callable[[object], object] | None
    shape: str | None = None
    rows: Callable[[object], list[Any]] | None = None


def endpoints(ticker: str, address: str | None, net: str, now_us: int) -> list[Endpoint]:
    m = quote(ticker, safe="")
    out = [
        Endpoint("root", "root", "/", None, None),
        Endpoint("health", "health", "/health", None, None),
        Endpoint("time", "time", "/v1/time", None, parse_time),
        Endpoint("markets", "markets", "/v1/markets", None, _parse_all_markets, "market", _rows("markets")),
        Endpoint("mids", "mids", "/v1/mids", None, parse_mids),
        Endpoint("prices", "prices", "/v1/prices", None, parse_prices),
        Endpoint(f"bbo_{ticker}", "bbo", f"/v1/bbo/{m}", None, parse_bbo, "bbo", _itself),
        Endpoint(f"l2_{ticker}", "l2OrderBook", f"/v1/l2OrderBook/{m}", {"nLevels": "20"}, parse_l2),
        Endpoint(
            f"candles_{ticker}_1m",
            "candles",
            "/v1/candles",
            {"market": ticker, "timeframe": "1m", "to": str(now_us), "countback": "5"},
            lambda b: parse_candles(b, final_only=False),
            "candle",
            _rows("candles"),
        ),
        Endpoint("compliance", "compliance", "/v1/compliance", None, parse_compliance),
        # Documented spelling (get-fee-tier-table "GET /v1/feetiers"); weight key feeTiers.
        Endpoint("feetiers", "feeTiers", "/v1/feetiers", None, None),
    ]
    if address is None:
        return out
    ref = ArcusAccountRef(net, address, 0)
    acct = {"address": address, "accountIndex": "0"}
    out += [
        Endpoint("compliance_address", "compliance", "/v1/compliance", {"address": address}, parse_compliance),
        Endpoint("account", "account", "/v1/account", acct, lambda b: parse_account(b, ref=ref, now_mono=0.0), "account", _itself),
        Endpoint("positions", "positions", "/v1/positions", acct, lambda b: parse_positions_payload(b, ref=ref), "position", _rows("positions")),
        Endpoint("openOrders", "openOrders", "/v1/openOrders", {**acct, "status": "OPEN", "limit": "5"}, parse_open_orders_payload, "order", _rows("orders")),
        Endpoint("fills", "fills", "/v1/fills", {**acct, "limit": "5"}, lambda b: parse_fills_payload(b, ref=ref), "fill", _rows("fills")),
        Endpoint("funding", "funding", "/v1/funding", {**acct, "limit": "5"}, parse_funding_payload, "fundingPayment", _rows("fundingPayments")),
        Endpoint("rateLimit", "rateLimit", "/v1/rateLimit", acct, lambda b: parse_rate_limit(b, ref=ref, now_mono=0.0), "rateLimit", _itself),
        Endpoint("leverages", "leverages", "/v1/leverages", acct, lambda b: parse_leverages(b, ref=ref)),
        Endpoint("apiKeys", "apiKeys", "/v1/apiKeys", {"address": address}, parse_api_keys, "apiKey", _rows("apiKeys")),
    ]
    return out


# --- pure helpers (unit-tested) ------------------------------------------------------------------------


def drift(objects: Iterable[Any], shape: str | None) -> tuple[list[str], list[str]]:
    """(unknown_fields, missing_required) over the top-level keys of every object."""
    if shape is None:
        return [], []
    known = FIELD_SETS.get(shape, frozenset())
    required = REQUIRED_FIELDS.get(shape, frozenset())
    unknown: set[str] = set()
    missing: set[str] = set()
    for obj in objects:
        if not isinstance(obj, Mapping):
            continue
        keys = {str(k) for k in obj}
        unknown |= keys - known
        missing |= required - keys
    return sorted(unknown), sorted(missing)


def candles_order(body: object) -> str:
    rows = body.get("candles") if isinstance(body, Mapping) else None
    if not isinstance(rows, list):
        return "n/a"
    times = [r.get("openTime") for r in rows if isinstance(r, Mapping)]
    if len(times) < 2 or not all(isinstance(t, int) and not isinstance(t, bool) for t in times):
        return "n/a"
    if all(a > b for a, b in zip(times, times[1:])):
        return "newest_first"
    if all(a < b for a, b in zip(times, times[1:])):
        return "oldest_first"
    return "n/a"


def redact_capture(value: Any, address: str | None) -> Any:
    """The requested address (any case, with or without 0x) -> the dead address;
    ``geo.country`` / ``geo.region`` -> ``"XX"``; applied to keys and values."""
    body = address[2:].lower() if address else None
    dead_body = DEAD_ADDRESS[2:]

    def text(s: str) -> str:
        return re.sub(re.escape(body), dead_body, s, flags=re.IGNORECASE) if body else s

    def walk(v: Any, key: str | None = None, in_geo: bool = False) -> Any:
        if in_geo and key in ("country", "region") and v is not None:
            return "XX"
        if isinstance(v, str):
            return text(v)
        if isinstance(v, Mapping):
            return {text(str(k)): walk(item, str(k), in_geo=(key == "geo")) for k, item in v.items()}
        if isinstance(v, list):
            return [walk(item, None, in_geo) for item in v]
        return v

    return walk(value)


def _jsonable(value: Any) -> Any:
    """Decoded bodies carry ``Decimal`` for JSON numbers (the client never uses
    float): write them back as numbers when a float round-trips exactly."""
    if isinstance(value, Decimal):
        if value.is_finite():
            as_float = float(value)
            if Decimal(repr(as_float)) == value:
                return as_float
        return str(value)
    if isinstance(value, Mapping):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    return value


def _http_of(out: object) -> int | str:
    if isinstance(out, Ok):
        return out.http_status
    if isinstance(out, Unavailable):
        return out.http_status
    if isinstance(out, Throttled):
        return 429
    if isinstance(out, Unauthorized):
        return 401
    if isinstance(out, Forbidden):
        return 403
    if isinstance(out, (NoActivity, NotFound)):
        return 404
    if isinstance(out, LocalDenied):
        return "local"
    return "?"


def _outcome_of(out: object) -> str:
    name = type(out).__name__
    if isinstance(out, Forbidden):
        return f"{name}:{out.kind}"
    if isinstance(out, Throttled):
        return f"{name}:{out.layer}"
    if isinstance(out, Unavailable):
        return f"{name}:{out.message[:40]}"
    return name


# --- capture --------------------------------------------------------------------------------------


def default_client(net: str) -> ArcusClient:
    """No hub: a plain clock + IP budget + REST client for ``net``."""
    return ArcusClient(net, clock=ArcusClock(net), ip_budget=IpBudget(net))


async def capture(client: ArcusClient, *, net: str, ticker: str, address: str | None, now_us: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run every GET; returns (report, {file_name: body_to_save}) — unredacted."""
    report: dict[str, Any] = {"network": net, "endpoints": {}, "candles_order": "n/a"}
    files: dict[str, Any] = {}
    for ep in endpoints(ticker, address, net, now_us):
        out = await client.get_raw(ep.path_key, ep.path, ep.params, lane=LANE, max_wait_s=MAX_WAIT_S)
        entry: dict[str, Any] = {"http": _http_of(out), "outcome": _outcome_of(out), "parse_ok": None, "unknown_fields": [], "missing_required": []}
        if isinstance(out, Ok):
            body = out.value
            files[f"{ep.name}.json"] = body
            if ep.parse is not None:
                try:
                    ep.parse(body)
                    entry["parse_ok"] = True
                except (ArcusSchemaError, ValueError, TypeError):
                    entry["parse_ok"] = False
            objects = ep.rows(body) if ep.rows is not None else []
            entry["unknown_fields"], entry["missing_required"] = drift(objects, ep.shape)
            if ep.path_key == "candles":
                report["candles_order"] = candles_order(body)
        else:
            files[f"{ep.name}.json"] = {"outcome": entry["outcome"], "http": entry["http"]}
        report["endpoints"][ep.name] = entry
    report["schema_error_counts"] = schema_error_counts()
    return report, files


def _stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def write_outputs(out_dir: Path, report: Mapping[str, Any], files: Mapping[str, Any], address: str | None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        clean = redact_capture(_jsonable(body), address)
        (out_dir / name).write_text(json.dumps(clean, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    clean_report = redact_capture(_jsonable(dict(report)), address)
    (out_dir / "report.json").write_text(json.dumps(clean_report, indent=1, sort_keys=True) + "\n", encoding="utf-8")


async def _amain(args: argparse.Namespace, services_factory: Callable[[str], ArcusClient] | None = None) -> int:
    if (env_str("FLY_APP_NAME") or env_str("FLY_MACHINE_ID")) and not args.allow_fly:
        print("REFUSED: refusing to run on a Fly machine (production egress IP); pass --allow-fly", file=sys.stderr)
        return 2
    net = args.network
    started = time.time()
    client = (services_factory or default_client)(net)
    try:
        report, files = await capture(client, net=net, ticker=args.market, address=args.address, now_us=time.time_ns() // 1000)
    finally:
        await client.aclose()
    report["captured_utc"] = datetime.fromtimestamp(started, timezone.utc).isoformat()
    report["market"] = args.market
    report["with_address"] = args.address is not None
    out_dir = Path(args.out) if args.out else Path.cwd() / f"arcus_capture_{net}_{_stamp(started)}"
    write_outputs(out_dir, report, files, args.address)
    for name, entry in report["endpoints"].items():
        line = f"{name:24s} http={entry['http']!s:5s} {entry['outcome']:28s} parse_ok={entry['parse_ok']} unknown={entry['unknown_fields']} missing={entry['missing_required']}"
        print(redact_capture(line, args.address))  # the outcome may carry server text
    print(f"candles_order={report['candles_order']} schema_error_counts={report['schema_error_counts']}")
    print(f"written: {out_dir}")
    return 0


def main(argv: list[str] | None = None, *, services_factory: Callable[[str], ArcusClient] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_amain(args, services_factory))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 1
    except Exception:
        import traceback

        print(redact_sensitive_text(traceback.format_exc()), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
