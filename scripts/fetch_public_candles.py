#!/usr/bin/env python3
"""Fetch REAL public OHLC candles (Binance USDT-M futures) into an OHLC CSV.

BTC/ETH perp prices are arbitraged across venues, so Binance BTCUSDT/ETHUSDT perp
candles are a faithful proxy for the Nado BTC-PERP/ETH-PERP price PATH the grid
strategies traded — and they need no venue key and touch no user data (public
market data only). Output is the OHLC CSV ``engine.backtester.candles_from_ohlc``
ingests directly.

  .venv/bin/python scripts/fetch_public_candles.py \
      --symbol BTCUSDT --interval 1m \
      --start 2026-08-01 --end 2026-08-24 --out btc_perp_aug2026_1m.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone

_FAPI = "https://fapi.binance.com/fapi/v1/klines"
_SPOT = "https://api.binance.com/api/v3/klines"
_MAX = 1500  # Binance klines max per call (futures)


def _epoch_ms(date_str: str) -> int:
    return int(datetime.strptime(date_str, "%Y-%m-%d")
               .replace(tzinfo=timezone.utc).timestamp() * 1000)


def _interval_ms(interval: str) -> int:
    unit = interval[-1]
    n = int(interval[:-1])
    return n * {"m": 60_000, "h": 3_600_000, "d": 86_400_000}[unit]


def _get(url: str, params: dict) -> list:
    q = "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(f"{url}?{q}", headers={"User-Agent": "nadobro-backtest"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", required=True, help="e.g. BTCUSDT, ETHUSDT")
    ap.add_argument("--interval", default="1m")
    ap.add_argument("--start", required=True, help="UTC date YYYY-MM-DD (inclusive)")
    ap.add_argument("--end", required=True, help="UTC date YYYY-MM-DD (exclusive)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    start_ms, end_ms = _epoch_ms(args.start), _epoch_ms(args.end)
    step = _interval_ms(args.interval)
    base = _FAPI
    rows: dict[int, dict] = {}
    cursor = start_ms
    calls = 0
    while cursor < end_ms:
        params = {"symbol": args.symbol, "interval": args.interval,
                  "startTime": cursor, "endTime": end_ms, "limit": _MAX}
        try:
            data = _get(base, params)
        except Exception as e:  # noqa: BLE001
            if base is _FAPI:
                print(f"futures fetch failed ({e}); falling back to spot", file=sys.stderr)
                base = _SPOT
                continue
            sys.exit(f"kline fetch failed: {e}")
        calls += 1
        if not data:
            break
        for k in data:
            t_ms = int(k[0])
            if t_ms >= end_ms:
                continue
            rows[t_ms] = {"time": t_ms // 1000, "open": k[1], "high": k[2],
                          "low": k[3], "close": k[4], "volume": k[5]}
        last = int(data[-1][0])
        nxt = last + step
        if nxt <= cursor:
            break
        cursor = nxt
        if calls % 10 == 0:
            print(f"  ...{calls} calls, {len(rows)} candles", file=sys.stderr)
        time.sleep(0.2)

    if not rows:
        sys.exit("no candles returned")
    ordered = [rows[t] for t in sorted(rows)]
    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["time", "open", "high", "low", "close", "volume"])
        w.writeheader()
        w.writerows(ordered)
    span_h = (ordered[-1]["time"] - ordered[0]["time"]) / 3600.0
    print(f"wrote {len(ordered)} candles to {args.out} "
          f"({span_h:.1f}h, {calls} calls, "
          f"px {float(ordered[0]['close']):.2f}->{float(ordered[-1]['close']):.2f})",
          file=sys.stderr)


if __name__ == "__main__":
    main()
