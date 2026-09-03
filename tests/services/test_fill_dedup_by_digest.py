"""A manual/strategy close record and the venue match for the SAME fill must not
both persist (prod 2026-09-03, session 312: rows 6576 manual + 6577 venue, same
digest, +0.0184 phantom close volume). The venue match reconciles the manual
placeholder (attaching submission_idx) instead of inserting a duplicate."""
from __future__ import annotations

import os
import pathlib

import pytest

if not (os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DATABASE_URL")):
    pytest.skip("no DATABASE_URL", allow_module_level=True)
try:
    import psycopg2  # noqa: F401
    _u = os.environ.get("SUPABASE_DATABASE_URL") or os.environ["DATABASE_URL"]
    psycopg2.connect(_u).close()
except Exception:
    pytest.skip("no reachable Postgres", allow_module_level=True)

from src.nadobro.db import execute, query_all, query_one  # noqa: E402
from src.nadobro.utils.x18 import to_x18  # noqa: E402
from src.nadobro.venue import nado_sync  # noqa: E402

MIG = pathlib.Path("src/nadobro/migrations")


@pytest.fixture(autouse=True)
def _schema():
    for m in ("0007_engine_v2_tables.sql",):
        try:
            execute((MIG / m).read_text())
        except Exception:
            pass
    execute("DELETE FROM trades_mainnet WHERE user_id = 9319")
    yield
    execute("DELETE FROM trades_mainnet WHERE user_id = 9319")


def _venue_match(digest, base, quote, fee, idx):
    return {
        "submission_idx": str(idx), "product_id": 2, "product_name": "BTC-PERP",
        "digest": digest, "base_filled": str(to_x18(base)), "quote_filled": str(to_x18(quote)),
        "fee": str(to_x18(fee)), "isolated": False,
    }


def test_venue_match_reconciles_a_manual_placeholder_instead_of_duplicating():
    digest = "0xc15a6c2f970704deadbeef"
    # The manual close placeholder: our realized PnL, no submission_idx.
    execute(
        "INSERT INTO trades_mainnet (user_id, product_id, product_name, order_type, side, "
        "size, fill_size, price, fill_price, realized_pnl, status, order_digest, source, filled_at, created_at) "
        "VALUES (9319, 2, 'BTC-PERP', 'market', 'long', '0.0184', '0.0184', '78420', '78420', -3.708, "
        "'filled', %s, 'manual', now(), now())",
        (digest,),
    )
    # The venue match for the SAME fill arrives.
    nado_sync._write_matches(9319, "mainnet", [_venue_match(digest, 0.0184, -1442.9, 0.72, 78096804)])

    rows = query_all("SELECT id, submission_idx, realized_pnl, fill_size FROM trades_mainnet WHERE user_id=9319 AND order_digest=%s", (digest,))
    assert len(rows) == 1, f"the venue match duplicated the close instead of reconciling: {rows}"
    assert str(rows[0]["submission_idx"]) == "78096804", "submission_idx not attached to the placeholder"
    assert float(rows[0]["realized_pnl"]) == -3.708, "reconcile must keep our realized-PnL attribution"


def test_a_second_venue_match_for_the_same_digest_still_inserts():
    digest = "0xmultifill000001"
    execute(
        "INSERT INTO trades_mainnet (user_id, product_id, product_name, order_type, side, size, "
        "fill_size, price, fill_price, status, order_digest, source, filled_at, created_at) "
        "VALUES (9319, 2, 'BTC-PERP', 'market', 'long', '0.01', '0.01', '78000', '78000', 'filled', %s, 'manual', now(), now())",
        (digest,),
    )
    nado_sync._write_matches(9319, "mainnet", [_venue_match(digest, 0.01, -780, 0.4, 111)])   # reconciles placeholder
    nado_sync._write_matches(9319, "mainnet", [_venue_match(digest, 0.01, -781, 0.4, 222)])   # distinct fill -> inserts
    idxs = {str(r["submission_idx"]) for r in query_all("SELECT submission_idx FROM trades_mainnet WHERE user_id=9319 AND order_digest=%s", (digest,))}
    assert idxs == {"111", "222"}, idxs


def test_a_venue_match_with_no_placeholder_inserts_normally():
    digest = "0xnoplaceholder01"
    nado_sync._write_matches(9319, "mainnet", [_venue_match(digest, 0.02, -1560, 0.8, 333)])
    rows = query_all("SELECT submission_idx FROM trades_mainnet WHERE user_id=9319 AND order_digest=%s", (digest,))
    assert len(rows) == 1 and str(rows[0]["submission_idx"]) == "333"
