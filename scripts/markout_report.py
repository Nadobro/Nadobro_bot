#!/usr/bin/env python3
"""Read the fill-mark-out ledger and print the Mid-mode viability verdict.

The ``markout_scorer`` scheduler job grades every strategy fill at 60s/300s
against the Hyperliquid mid and writes ``fill_markouts``. This reads that ledger
back and answers the one question the Phase-0 study could not (it needs REAL
Nado fills): are Mid's fills net positive after fees, and is any negativity real
adverse selection or just a Nado-vs-HL level offset?

Read-only. Needs a DATABASE_URL pointing at the environment whose fills you want
to grade — run it on the box, e.g.:

  fly ssh console -C ".venv/bin/python scripts/markout_report.py --network mainnet --days 30"

or locally with DATABASE_URL exported. Compare Mid against the grid family with
``--all``.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--network", choices=["mainnet", "testnet"], default="mainnet")
    ap.add_argument("--strategy", default="mid", help="strategy id to grade (default: mid)")
    ap.add_argument("--all", action="store_true", help="report across all strategies (ignores --strategy)")
    ap.add_argument("--days", type=float, default=30.0, help="lookback window in days")
    ap.add_argument("--product", default=None, help="optional product filter, e.g. BTC-PERP")
    args = ap.parse_args()

    if not os.environ.get("DATABASE_URL"):
        print("DATABASE_URL is not set — run this on the box (fly ssh console) or export it.",
              file=sys.stderr)
        return 2

    from src.nadobro.trading.markout_report import markout_report

    print(markout_report(
        args.network,
        strategy=(None if args.all else args.strategy),
        lookback_days=args.days,
        product=args.product,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
