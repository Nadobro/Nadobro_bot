"""The regular portfolio poll reads a SCOPED open-orders book (2026-09-16):
the venue charges 2 x products per multi-product read, so a whole-catalog
read on every sender every 30s could never fit the per-IP budget. The scope
is every product that can hold one of our rows, and the stale sweep is
restricted to exactly the products that were read.
"""
from __future__ import annotations

from unittest.mock import patch

from src.nadobro.venue import nado_sync


def test_scope_is_the_union_of_db_rows_and_the_last_snapshot():
    prior = {"open_orders": [{"product_id": 9}, {"product_id": "x"}], "positions": [{"product_id": 4}]}
    with patch("src.nadobro.models.database.get_open_order_product_ids", return_value=[2, 9]), \
         patch("src.nadobro.models.database.get_open_position_product_ids", return_value=[3]):
        assert nado_sync._open_orders_scope_for_user(1, "mainnet", prior) == [2, 3, 4, 9]


def test_scope_survives_db_helper_failures():
    with patch("src.nadobro.models.database.get_open_order_product_ids", side_effect=RuntimeError("db")), \
         patch("src.nadobro.models.database.get_open_position_product_ids", return_value=[3]):
        assert nado_sync._open_orders_scope_for_user(1, "mainnet", {"open_orders": [{"product_id": 1}]}) == [1, 3]


def test_scoped_sweep_only_touches_products_that_were_read():
    executed: list = []
    orders = [{"digest": "0xA", "product_id": 7, "product_name": "ETH-PERP", "side": "LONG", "amount": 1, "price": 2}]
    with patch.object(nado_sync, "execute", side_effect=lambda sql, params: executed.append((" ".join(sql.split()), params))):
        nado_sync._write_open_orders(1, "mainnet", orders, product_scope=[2, 3])
    sweep_sql, sweep_params = executed[0]
    assert "product_id = ANY(%s)" in sweep_sql
    assert sweep_params[-1] == [2, 3, 7]            # scope + the child's own product that returned an order
    assert sweep_params[:3] == (1, "mainnet", "0xA")


def test_scoped_read_of_nothing_never_sweeps_the_whole_account():
    executed: list = []
    with patch.object(nado_sync, "execute", side_effect=lambda sql, params: executed.append(sql)):
        nado_sync._write_open_orders(1, "mainnet", [], product_scope=[])
    assert executed == []


def test_empty_scoped_read_with_a_scope_sweeps_only_the_scope():
    executed: list = []
    with patch.object(nado_sync, "execute", side_effect=lambda sql, params: executed.append((" ".join(sql.split()), params))):
        nado_sync._write_open_orders(1, "mainnet", [], product_scope=[5])
    assert len(executed) == 1
    assert "product_id = ANY(%s)" in executed[0][0]
    assert executed[0][1] == (1, "mainnet", [5])


def test_unscoped_read_keeps_the_whole_account_sweep():
    executed: list = []
    with patch.object(nado_sync, "execute", side_effect=lambda sql, params: executed.append((" ".join(sql.split()), params))):
        nado_sync._write_open_orders(1, "mainnet", [], product_scope=None)
    assert len(executed) == 1
    assert "ANY(" not in executed[0][0]
    assert executed[0][1] == (1, "mainnet")
