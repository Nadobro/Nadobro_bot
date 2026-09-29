"""Backfill strategy_sessions metrics for engine strategies (grid/dgrid/dn/...).

Why: before the Engine-v2 → reporting bridge landed, engine fills were never
attributed to a ``strategy_session`` (the engine wrote no ``order_intents`` and
the per-cycle increment only counted cycles). So historical engine sessions
show ``total_volume_usd = realized_pnl = total_fees_paid = 0`` even after
profitable runs (e.g. the $26 / $12k dgrid run in the report).

The data isn't lost: the venue match-sync wrote those fills into
``trades_<network>`` as ``*_x18`` rows (authoritative base/quote/fee/realized
PnL) — just with ``strategy_session_id = NULL``. This script re-attributes them
to each session by ``user_id + product_id`` within the session's
``[started_at, stopped_at]`` window, sums funding from
``funding_payments_<network>`` over the same window, and writes the totals back
onto ``strategy_sessions``. Where no venue rows exist it falls back to the
engine's own ``engine_executors`` accounting for the controller in-window.

Safe by default: prints a dry-run table and changes nothing unless ``--apply``
is passed. Optionally tags the matched venue rows with the session id
(``--tag-rows``) so future rollups stay correct without re-running this.

Usage:
    # dry run (default) — show what WOULD change
    DATABASE_URL=... python scripts/backfill_engine_session_metrics.py

    # apply, and tag the venue rows with the resolved session id
    DATABASE_URL=... python scripts/backfill_engine_session_metrics.py --apply --tag-rows

    # limit to one session / network
    DATABASE_URL=... python scripts/backfill_engine_session_metrics.py --session-id 17
"""
from __future__ import annotations

import argparse

from src.nadobro.db import execute, query_all, query_one
from src.nadobro.strategy.engine_runtime import (
    ENGINE_MAPPED_STRATEGIES,
    deterministic_controller_id,
)
from src.nadobro.utils.venue_scope import (
    LOWER_ELSE_MAINNET,
    coerce_nado_network,
    is_non_nado_scope,
)


# Legacy ``"testnet" if str(network).lower() == "testnet" else "mainnet"``,
# routed through utils.venue_scope: an Arcus scope raises instead of tagging
# Nado's trades_mainnet rows with an Arcus session id.
def _trades_table(network: str) -> str:
    return "trades_" + coerce_nado_network(
        network, LOWER_ELSE_MAINNET, site="scripts.backfill_engine_session_metrics._trades_table"
    )


def _funding_table(network: str) -> str:
    return "funding_payments_" + coerce_nado_network(
        network, LOWER_ELSE_MAINNET, site="scripts.backfill_engine_session_metrics._funding_table"
    )


def _candidate_sessions(session_id: int | None, network: str | None) -> list[dict]:
    where = ["strategy = ANY(%s)"]
    params: list = [list(ENGINE_MAPPED_STRATEGIES)]
    if session_id is not None:
        where.append("id = %s")
        params.append(int(session_id))
    if network:
        where.append("network = %s")
        params.append(network)
    rows = query_all(
        f"SELECT id, user_id, strategy, network, product_id, product_name, "
        f"started_at, stopped_at, total_volume_usd, realized_pnl, total_fees_paid, "
        f"total_funding_paid FROM strategy_sessions "
        f"WHERE {' AND '.join(where)} ORDER BY id",
        tuple(params),
    )
    # Arcus sessions carry scope tokens (``arcus_mainnet``) in ``network`` and
    # share strategy names with Nado: they are never attributed against Nado's
    # trades_<network> / funding_payments_<network> tables.
    return [row for row in rows if not is_non_nado_scope(row.get("network"))]


def _venue_totals(sess: dict) -> dict | None:
    """Authoritative totals from untagged venue match rows in the session window."""
    table = _trades_table(sess["network"])
    if sess.get("product_id") is None or sess.get("started_at") is None:
        return None
    # Match by tagged session id first; otherwise attribute by user+product+window.
    row = query_one(
        f"""
        SELECT
          COUNT(*) AS fills,
          COALESCE(SUM(quote_filled_x18) / 1e18, 0) AS volume,
          COALESCE(SUM(fee_x18) / 1e18, 0) AS fees,
          COALESCE(SUM(realized_pnl_x18) / 1e18, 0) AS realized_pnl
        FROM {table}
        WHERE order_type = 'match'
          AND (
            strategy_session_id = %s
            OR (
              strategy_session_id IS NULL
              AND user_id = %s AND product_id = %s
              AND COALESCE(filled_at, created_at) >= %s
              AND (%s IS NULL OR COALESCE(filled_at, created_at) <= %s)
            )
          )
        """,
        (
            int(sess["id"]), int(sess["user_id"]), int(sess["product_id"]),
            sess["started_at"], sess["stopped_at"], sess["stopped_at"],
        ),
    )
    if not row or int(row.get("fills") or 0) == 0:
        return None
    return {
        "source": "venue",
        "total_orders_filled": int(row["fills"]),
        "total_volume_usd": float(row["volume"]),
        "total_fees_paid": float(row["fees"]),
        "realized_pnl": float(row["realized_pnl"]),
    }


def _executor_fallback(sess: dict) -> dict | None:
    """Fallback: the engine's own per-controller accounting in the window."""
    cid = deterministic_controller_id(sess["strategy"], int(sess["user_id"]), sess["network"])
    row = query_one(
        """
        SELECT
          COUNT(*) FILTER (WHERE volume_quote > 0) AS fills,
          COALESCE(SUM(volume_quote), 0) AS volume,
          COALESCE(SUM(fees_paid_quote), 0) AS fees,
          COALESCE(SUM(net_pnl_quote), 0) AS realized_pnl
        FROM engine_executors
        WHERE controller_id = %s
          AND created_at >= %s
          AND (%s IS NULL OR created_at <= %s)
        """,
        (cid, sess["started_at"], sess["stopped_at"], sess["stopped_at"]),
    )
    if not row or int(row.get("fills") or 0) == 0:
        return None
    return {
        "source": "executors",
        "total_orders_filled": int(row["fills"]),
        "total_volume_usd": float(row["volume"]),
        "total_fees_paid": float(row["fees"]),
        "realized_pnl": float(row["realized_pnl"]),
    }


def _funding(sess: dict) -> float:
    if sess.get("product_id") is None or sess.get("started_at") is None:
        return 0.0
    row = query_one(
        f"""
        SELECT COALESCE(SUM(amount_x18) / 1e18, 0) AS funding
        FROM {_funding_table(sess['network'])}
        WHERE user_id = %s AND product_id = %s
          AND paid_at >= %s
          AND (%s IS NULL OR paid_at <= %s)
        """,
        (int(sess["user_id"]), int(sess["product_id"]),
         sess["started_at"], sess["stopped_at"], sess["stopped_at"]),
    )
    return float(row["funding"]) if row else 0.0


def _tag_rows(sess: dict) -> int:
    """Tag the untagged venue rows in-window with the session id (idempotent)."""
    table = _trades_table(sess["network"])
    execute(
        f"""
        UPDATE {table} SET strategy_session_id = %s
        WHERE order_type = 'match' AND strategy_session_id IS NULL
          AND user_id = %s AND product_id = %s
          AND COALESCE(filled_at, created_at) >= %s
          AND (%s IS NULL OR COALESCE(filled_at, created_at) <= %s)
        """,
        (int(sess["id"]), int(sess["user_id"]), int(sess["product_id"]),
         sess["started_at"], sess["stopped_at"], sess["stopped_at"]),
    )
    return 1


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write the updates (default: dry run)")
    ap.add_argument("--tag-rows", action="store_true",
                    help="also tag matched venue rows with the session id")
    ap.add_argument("--session-id", type=int, default=None)
    ap.add_argument("--network", default=None, choices=[None, "mainnet", "testnet"])
    args = ap.parse_args()

    sessions = _candidate_sessions(args.session_id, args.network)
    print(f"Scanning {len(sessions)} engine session(s)...\n")
    header = f"{'id':>5} {'strat':<6} {'net':<8} {'src':<9} {'fills':>5} {'volume':>12} {'pnl':>10} {'fees':>9} {'funding':>9}"
    print(header)
    print("-" * len(header))

    # Pass 1: compute + (optionally) write metrics for every session. Tagging
    # is deferred to pass 2 so a session's metrics are never affected by another
    # session's tagging within this run (deterministic, order-independent).
    changed = 0
    to_tag: list[dict] = []
    for sess in sessions:
        totals = _venue_totals(sess) or _executor_fallback(sess)
        funding = _funding(sess)
        if not totals and abs(funding) < 1e-9:
            continue
        totals = totals or {"source": "funding-only", "total_orders_filled": 0,
                            "total_volume_usd": 0.0, "total_fees_paid": 0.0, "realized_pnl": 0.0}
        totals["total_funding_paid"] = funding

        # Only act when this actually changes the stored (zeroed) numbers.
        cur_vol = float(sess.get("total_volume_usd") or 0)
        cur_pnl = float(sess.get("realized_pnl") or 0)
        if abs(cur_vol - totals["total_volume_usd"]) < 1e-6 and abs(cur_pnl - totals["realized_pnl"]) < 1e-6 \
                and abs(float(sess.get("total_funding_paid") or 0) - funding) < 1e-6:
            continue

        print(f"{sess['id']:>5} {sess['strategy']:<6} {sess['network']:<8} {totals['source']:<9} "
              f"{totals['total_orders_filled']:>5} {totals['total_volume_usd']:>12.2f} "
              f"{totals['realized_pnl']:>10.4f} {totals['total_fees_paid']:>9.4f} {funding:>9.4f}")
        changed += 1

        if args.apply:
            update = {k: v for k, v in totals.items() if k != "source"}
            from src.nadobro.models.database import update_strategy_session

            update_strategy_session(int(sess["id"]), update)
            if args.tag_rows and totals["source"] == "venue":
                to_tag.append(sess)

    # Pass 2: tag rows (each untagged row to the first session that claims it).
    for sess in to_tag:
        _tag_rows(sess)

    print("-" * len(header))
    action = "UPDATED" if args.apply else "would update (dry run)"
    print(f"\n{changed} session(s) {action}.")
    if not args.apply and changed:
        print("Re-run with --apply (and optionally --tag-rows) to persist.")


if __name__ == "__main__":
    main()
