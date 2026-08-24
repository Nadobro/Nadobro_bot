#!/usr/bin/env python3
"""Fetch REAL Nado OHLC candles into a CSV for the cost-aware backtester.

WHY: the grid-family strategies (grid / rgrid / dgrid) must be re-validated on the
REAL market regime that lost money in August 2026 — synthetic tapes are what the
original regression was (wrongly) validated on. This pulls real 1m candles from the
Nado indexer for a product + date range and writes an OHLC CSV that
``engine.backtester.candles_from_ohlc`` ingests directly.

RUN IT WHERE VENUE CREDENTIALS EXIST (prod / your local env with the bot's .env, or
`fly ssh console` on the app). Candles are public market data, but the SDK client is
built from a key the same way the app's alert price-check client is:

    # August 2026 BTC perp, 1-minute:
    NADO_ALERT_CHECK_PRIVATE_KEY=0x... \
    .venv/bin/python scripts/fetch_backtest_candles.py \
        --product BTC-PERP --timeframe 1m \
        --start 2026-08-01 --end 2026-08-24 \
        --network mainnet --out btc_perp_aug2026_1m.csv

Then hand me the CSV (drop it in the repo root) and I validate Phases 1-3 against it
with scripts/backtest_real_tape.py.

Env used (same names as main.py's alert client):
  NADO_ALERT_CHECK_PRIVATE_KEY  signing key (preferred — candle queries need the SDK)
  NADO_ALERT_CHECK_ADDRESS      fallback read-only address (may return no candles if
                                the SDK requires a signer for the indexer query)
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from datetime import datetime, timezone

# Repo root on path when run as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.nadobro.venue.nado_client import (  # noqa: E402
    get_or_create_readonly_client,
    get_or_create_signing_client,
)
from src.nadobro.venue.product_catalog import get_product_id  # noqa: E402

_INTERVAL_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
_LIMIT = 200  # indexer max per call


def _epoch_s(date_str: str) -> int:
    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def _build_client(network: str):
    pk = (os.environ.get("NADO_ALERT_CHECK_PRIVATE_KEY") or "").strip()
    if pk:
        return get_or_create_signing_client(pk, network)
    addr = (os.environ.get("NADO_ALERT_CHECK_ADDRESS") or "").strip()
    if not addr:
        sys.exit("Set NADO_ALERT_CHECK_PRIVATE_KEY (preferred) or NADO_ALERT_CHECK_ADDRESS.")
    print("WARNING: no signing key set; a read-only client may return no candles.", file=sys.stderr)
    return get_or_create_readonly_client(addr, network)


def _time_unit_divisor(sample_time: float) -> float:
    """Return the divisor that turns a candle 'time' into epoch SECONDS."""
    return 1000.0 if float(sample_time) > 1e12 else 1.0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--product", default="BTC-PERP")
    ap.add_argument("--timeframe", default="1m", choices=sorted(_INTERVAL_SECONDS))
    ap.add_argument("--start", required=True, help="UTC date YYYY-MM-DD (inclusive)")
    ap.add_argument("--end", required=True, help="UTC date YYYY-MM-DD (exclusive upper bound)")
    ap.add_argument("--network", default="mainnet", choices=["mainnet", "testnet"])
    ap.add_argument("--out", required=True, help="output CSV path")
    args = ap.parse_args()

    start_s, end_s = _epoch_s(args.start), _epoch_s(args.end)
    if end_s <= start_s:
        sys.exit("--end must be after --start")
    step = _INTERVAL_SECONDS[args.timeframe]

    client = _build_client(args.network)
    pid = get_product_id(args.product, network=args.network, client=client)
    if pid is None:
        sys.exit(f"could not resolve product_id for {args.product} on {args.network}")
    print(f"product={args.product} id={pid} tf={args.timeframe} "
          f"range=[{args.start},{args.end}) network={args.network}", file=sys.stderr)

    # Probe the latest window to learn the indexer's time unit, then paginate
    # backwards with max_time until we pass `start`.
    probe = client.get_candlesticks(int(pid), timeframe=args.timeframe, limit=1)
    if not probe:
        sys.exit("no candles returned on probe — check credentials / product / network")
    div = _time_unit_divisor(probe[0]["time"])
    max_time_unit = 1000.0 if div == 1000.0 else 1.0  # assume max_time takes the same unit

    seen: dict[int, dict] = {}
    cursor = end_s  # walk backwards from the end
    calls = 0
    while cursor > start_s:
        max_time = int(cursor * max_time_unit)
        rows = client.get_candlesticks(int(pid), timeframe=args.timeframe,
                                       limit=_LIMIT, max_time=max_time)
        calls += 1
        if not rows:
            break
        oldest = None
        for r in rows:
            t_s = int(float(r["time"]) / div)
            oldest = t_s if oldest is None else min(oldest, t_s)
            if start_s <= t_s < end_s:
                seen[t_s] = {"time": t_s, "open": r["open"], "high": r["high"],
                             "low": r["low"], "close": r["close"], "volume": r.get("volume", 0)}
        if oldest is None or oldest >= cursor:
            cursor -= step * _LIMIT  # no progress (gap) — jump a page and retry
        else:
            cursor = oldest - step
        if calls % 20 == 0:
            print(f"  ...{calls} calls, {len(seen)} candles, cursor={cursor}", file=sys.stderr)
        time.sleep(0.15)  # be gentle on the gateway budget

    if not seen:
        sys.exit("no candles in the requested range")
    ordered = [seen[k] for k in sorted(seen)]
    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["time", "open", "high", "low", "close", "volume"])
        w.writeheader()
        w.writerows(ordered)
    span_h = (ordered[-1]["time"] - ordered[0]["time"]) / 3600.0
    print(f"wrote {len(ordered)} candles to {args.out} "
          f"({span_h:.1f}h, {calls} indexer calls)", file=sys.stderr)


if __name__ == "__main__":
    main()
