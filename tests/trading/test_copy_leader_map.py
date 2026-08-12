"""The venue-client → copy_service position seam.

The venue client publishes a position's average entry under "price"
("entry_price" is a newer alias emitted by both extractors). copy_service
gates every mirrored open on entry > 0, so a key mismatch here silently
disables ALL copy opens — exactly the regression these tests pin.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.nadobro.trading.copy_service as copy_service
import src.nadobro.venue.nado_client as nado_client_mod


class _StubReadonlyClient:
    """Returns venue-shaped position rows (see NadoClient position dicts:
    product_id/product_name/amount/signed_amount/price/side [+unrealized])."""

    def __init__(self, positions):
        self._positions = positions

    def get_all_positions(self):
        return list(self._positions)

    def get_open_orders(self, pid):
        return []


@pytest.fixture()
def _no_snapshot(monkeypatch):
    monkeypatch.setattr(copy_service, "save_copy_snapshot", lambda *a, **k: None)


def _patch_factory(monkeypatch, client):
    monkeypatch.setattr(
        nado_client_mod, "get_or_create_readonly_client", lambda *a, **k: client
    )


def test_leader_entry_price_read_from_price_key(monkeypatch, _no_snapshot):
    """Older/cached clients emit only "price" — the map must still carry the
    real entry so opens are not skipped."""
    client = _StubReadonlyClient([{
        "product_id": 2,
        "product_name": "BTC-PERP",
        "amount": 1.5,
        "signed_amount": 1.5,
        "price": 65000.0,
        "side": "LONG",
        "unrealized_pnl": 120.0,
    }])
    _patch_factory(monkeypatch, client)

    pos_map = copy_service._load_leader_position_map(1, "0xleader", "mainnet")

    assert pos_map[2]["entry_price"] == pytest.approx(65000.0)
    assert pos_map[2]["side"] == "LONG"
    assert pos_map[2]["size"] == pytest.approx(1.5)


def test_leader_entry_price_prefers_explicit_alias(monkeypatch, _no_snapshot):
    client = _StubReadonlyClient([{
        "product_id": 4,
        "product_name": "ETH-PERP",
        "amount": 3.0,
        "signed_amount": -3.0,
        "price": 3400.0,
        "entry_price": 3400.0,
        "side": "SHORT",
    }])
    _patch_factory(monkeypatch, client)

    pos_map = copy_service._load_leader_position_map(1, "0xleader", "mainnet")

    assert pos_map[4]["entry_price"] == pytest.approx(3400.0)
    assert pos_map[4]["side"] == "SHORT"


def test_zero_entry_still_zero_when_venue_omits_both_keys(monkeypatch, _no_snapshot):
    """No price info at all -> entry stays 0 and the open gate (entry <= 0)
    correctly refuses to size a position off garbage."""
    client = _StubReadonlyClient([{
        "product_id": 6,
        "product_name": "SOL-PERP",
        "amount": 10.0,
        "signed_amount": 10.0,
        "side": "LONG",
    }])
    _patch_factory(monkeypatch, client)

    pos_map = copy_service._load_leader_position_map(1, "0xleader", "mainnet")

    assert pos_map[6]["entry_price"] == 0.0


def test_sizing_works_end_to_end_with_price_keyed_positions(monkeypatch, _no_snapshot):
    """The conviction-weighted sizing must produce a positive copy size from a
    venue-shaped map — the exact chain that was dead while entry_price was 0."""
    client = _StubReadonlyClient([{
        "product_id": 2,
        "product_name": "BTC-PERP",
        "amount": 2.0,
        "signed_amount": 2.0,
        "price": 50000.0,
        "side": "LONG",
    }])
    _patch_factory(monkeypatch, client)

    pos_map = copy_service._load_leader_position_map(1, "0xleader", "mainnet")
    leader = pos_map[2]
    copy_size, lev = copy_service._compute_copy_sizing(
        leader_size=leader["size"],
        leader_entry=leader["entry_price"],
        leader_leverage=leader["leverage"],
        leader_max_notional=copy_service._leader_max_notional(pos_map),
        margin_per_trade=100.0,
        max_leverage=5.0,
        product_max_leverage=20.0,
    )

    # Largest (only) position -> full margin at capped leverage: 100*5/50000.
    assert copy_size == pytest.approx(0.01)
    assert lev == pytest.approx(5.0)


# ==========================================================================
# COPY-LEADER-READ-FLAP — an unbelievable leader read must not close anything
# ==========================================================================
def test_an_empty_leader_read_is_untrusted_when_the_snapshot_had_positions():
    """``get_all_positions()`` returns [] WITHOUT raising on a total read failure
    and on isolated-subaccount discovery failure. Absence from the map is read
    downstream as "leader closed", so a believable-looking [] flattens the whole
    copy book at market."""
    with patch.object(copy_service, "_archive_reads_unreliable", return_value=False), \
         patch.object(copy_service, "_leader_snapshot_had_positions", return_value=True):
        assert copy_service._leader_read_is_trustworthy(1, "mainnet", {}) is False


def test_an_empty_leader_read_is_trusted_when_the_leader_was_already_flat():
    """A genuinely flat leader must still be believed, or closes never mirror."""
    with patch.object(copy_service, "_archive_reads_unreliable", return_value=False), \
         patch.object(copy_service, "_leader_snapshot_had_positions", return_value=False):
        assert copy_service._leader_read_is_trustworthy(1, "mainnet", {}) is True


def test_a_nonempty_leader_read_is_always_trusted():
    with patch.object(copy_service, "_archive_reads_unreliable", return_value=True):
        assert copy_service._leader_read_is_trustworthy(1, "mainnet", {2: {}}) is True


def test_an_unhealthy_archive_makes_an_empty_read_untrusted():
    with patch.object(copy_service, "_archive_reads_unreliable", return_value=True), \
         patch.object(copy_service, "_leader_snapshot_had_positions", return_value=False):
        assert copy_service._leader_read_is_trustworthy(1, "mainnet", {}) is False


def test_an_unreadable_snapshot_fails_closed():
    """If the comparison basis cannot be read we must assume the leader HAD
    positions — an unreadable basis must not license a mass close."""
    with patch.object(copy_service, "get_latest_copy_snapshot", side_effect=RuntimeError("db down")):
        assert copy_service._leader_snapshot_had_positions(1, "mainnet") is True


def test_an_empty_read_does_not_overwrite_a_nonempty_snapshot():
    """The snapshot is the only basis for telling 'leader flat' from 'cannot see
    the leader', so a suspect empty read must not destroy it."""
    client = MagicMock()
    client.get_all_positions.return_value = []
    with patch.object(copy_service, "get_or_create_readonly_client", return_value=client, create=True), \
         patch("src.nadobro.venue.nado_client.get_or_create_readonly_client", return_value=client), \
         patch.object(copy_service, "_leader_snapshot_had_positions", return_value=True), \
         patch.object(copy_service, "save_copy_snapshot") as save:
        out = copy_service._load_leader_position_map(1, "0xleader", "mainnet")

    assert out == {}
    save.assert_not_called()


def test_the_close_pass_is_skipped_when_the_leader_read_is_untrusted():
    """End-to-end: an untrusted read must place NO closing order."""
    import asyncio

    mirror = {
        "id": 7, "user_id": 4242, "network": "mainnet", "trader_id": 9,
        "margin_per_trade": 100.0, "max_leverage": 5.0, "total_allocated_usd": 500.0,
    }
    held = [{"id": 1, "product_id": 2, "side": "LONG", "size": 1.0,
             "entry_price": 100.0, "leverage": 2.0, "product_name": "BTC-PERP"}]

    with patch.object(copy_service, "get_copy_mirror", return_value=mirror), \
         patch.object(copy_service, "get_open_copy_positions", return_value=held), \
         patch.object(copy_service, "execute_market_order") as market, \
         patch.object(copy_service, "_execute_maker_open") as maker, \
         patch.object(copy_service, "_notify_user", new_callable=AsyncMock):
        asyncio.run(copy_service._sync_mirror_positions(
            mirror, {}, leader_trusted=False,
        ))

    market.assert_not_called()
    maker.assert_not_called()
