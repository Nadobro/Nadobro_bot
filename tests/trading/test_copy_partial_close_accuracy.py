"""Partial-close accuracy for copy positions (real Postgres; auto-skips without one).

A copy position the leader trimmed before fully closing MUST end up with the
WHOLE-trade accumulators, so the Type A "COPY TRADE" share card and History
(both reconstruct exit = entry + pnl/(size*dir)) show the whole trade, not just
the final slice. This pins the DB write semantics:

- ``reduce_copy_position`` accumulates the closed slice's pnl AND base
  (``closed_size``), and decrements the live ``size``.
- ``close_copy_position`` ADDS the final slice to both accumulators (never
  overwrites pnl) and is idempotent (``status='open'`` guard) so a re-close can
  never double count.
"""

import os

import pytest


def _db_reachable() -> bool:
    if not (os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DATABASE_URL")):
        return False
    try:
        import psycopg2

        url = os.environ.get("SUPABASE_DATABASE_URL") or os.environ["DATABASE_URL"]
        psycopg2.connect(url).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_reachable(), reason="no reachable Postgres (DATABASE_URL)")

_USER = 990_017_055
_WALLET = "0xtest_copy_partial_close_accuracy"


@pytest.fixture()
def mirror_id():
    from src.nadobro.db import execute
    from src.nadobro.models.database import create_copy_mirror_v2, upsert_copy_trader

    execute("DELETE FROM copy_positions WHERE user_id = %s", (_USER,))
    execute("DELETE FROM copy_mirrors WHERE user_id = %s", (_USER,))
    execute("DELETE FROM copy_traders WHERE wallet_address = %s", (_WALLET,))
    tid = upsert_copy_trader(_WALLET, label="partial-close", is_curated=True)
    mid = create_copy_mirror_v2(
        user_id=_USER, trader_id=tid, network="mainnet",
        margin_per_trade=100.0, max_leverage=5.0,
        cumulative_stop_loss_pct=10.0, cumulative_take_profit_pct=0.0,
        total_allocated_usd=500.0,
    )
    yield mid
    execute("DELETE FROM copy_positions WHERE user_id = %s", (_USER,))
    execute("DELETE FROM copy_mirrors WHERE user_id = %s", (_USER,))
    execute("DELETE FROM copy_traders WHERE id = %s", (tid,))


def _open_position(mirror_id: int) -> int:
    from src.nadobro.models.database import insert_copy_position

    return insert_copy_position({
        "mirror_id": mirror_id, "user_id": _USER, "product_id": 2,
        "product_name": "ETH-PERP", "side": "long", "entry_price": 100.0,
        "size": 1.0, "leverage": 3, "status": "open",
    })


def test_partial_then_full_close_accumulates_whole_trade(mirror_id):
    from src.nadobro.db import query_one
    from src.nadobro.models.database import (
        close_copy_position,
        get_closed_copy_position,
        reduce_copy_position,
    )

    pid = _open_position(mirror_id)

    # Leader trims 75%: close 0.75 @ 120 → slice pnl +15, remaining 0.25.
    reduce_copy_position(pid, new_size=0.25, new_leader_size=0.25, pnl_delta=15.0)
    mid = query_one("SELECT size, pnl, closed_size, status FROM copy_positions WHERE id = %s", (pid,))
    assert float(mid["size"]) == 0.25            # live size decremented
    assert float(mid["pnl"]) == 15.0             # slice pnl accumulated
    assert float(mid["closed_size"]) == 0.75     # base closed so far
    assert mid["status"] == "open"

    # Leader flattens the rest: close 0.25 @ 140 → slice pnl +10.
    close_copy_position(pid, pnl=10.0, reason="leader_closed")
    row = query_one("SELECT size, pnl, closed_size, status FROM copy_positions WHERE id = %s", (pid,))
    assert row["status"] == "closed"
    assert float(row["pnl"]) == 25.0             # 15 + 10, NOT overwritten to 10
    assert float(row["closed_size"]) == 1.0      # whole trade: 0.75 + 0.25

    # The card builder reconstructs the whole-trade exit from the accumulators.
    pos = get_closed_copy_position(pid)
    from decimal import Decimal
    entry, pnl, size = Decimal(str(pos["entry_price"])), Decimal(str(pos["pnl"])), Decimal(str(pos["closed_size"]))
    exit_px = entry + pnl / (size * Decimal(1))
    assert exit_px == Decimal("125")             # (120*0.75 + 140*0.25) / 1.0


def test_reclose_is_idempotent_and_never_double_counts(mirror_id):
    from src.nadobro.db import query_one
    from src.nadobro.models.database import close_copy_position

    pid = _open_position(mirror_id)          # single full close, size 1.0
    close_copy_position(pid, pnl=40.0, reason="leader_closed")
    once = query_one("SELECT pnl, closed_size FROM copy_positions WHERE id = %s", (pid,))
    assert float(once["pnl"]) == 40.0 and float(once["closed_size"]) == 1.0

    # A racing second close must match 0 rows (status already 'closed') — the
    # accumulators must NOT move.
    close_copy_position(pid, pnl=40.0, reason="leader_closed")
    twice = query_one("SELECT pnl, closed_size FROM copy_positions WHERE id = %s", (pid,))
    assert float(twice["pnl"]) == 40.0 and float(twice["closed_size"]) == 1.0


def test_single_full_close_closed_size_equals_size(mirror_id):
    from src.nadobro.db import query_one
    from src.nadobro.models.database import close_copy_position

    pid = _open_position(mirror_id)          # no partial close
    close_copy_position(pid, pnl=7.0, reason="leader_closed")
    row = query_one("SELECT size, pnl, closed_size FROM copy_positions WHERE id = %s", (pid,))
    # closed_size seeded from the remaining size → equals the full size; the card
    # renders exactly as before for the common single-close case.
    assert float(row["closed_size"]) == 1.0 and float(row["size"]) == 1.0
    assert float(row["pnl"]) == 7.0
