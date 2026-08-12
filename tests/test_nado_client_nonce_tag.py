"""Pin the 20-bit width of the order-nonce digest tag.

``place_order`` correlates otherwise-identical orders (the same grid level placed
repeatedly) by embedding a ``client_id`` in the low bits of the order nonce,
because the venue documents ``client_id`` as NOT part of the order digest. The
SDK packs the nonce as::

    nonce = (recv_time_ms << 20) + random_int      # nado_protocol/utils/nonce.py

so the mask in ``nado_client.place_order`` must be exactly the 20 bits that shift
leaves. Widening it carries the tag into the timestamp bits and moves the order's
expiry; narrowing it silently collides tags. Digest tagging is what lets closes
reconcile instead of leaking into History as phantom trades
(``trading/order_intents.py``), so a shift/mask drift here corrupts PnL
attribution rather than failing loudly.

These tests assert against the real SDK, so a future SDK that changes the shift
width breaks them instead of quietly mis-tagging orders in production.

NOTE on nonce randomness: the SDK's default ``random_int`` is
``random.randint(0, 999)`` (Mersenne Twister). That is intentional and safe —
authorization is the EIP-712 signature and replay is prevented by the venue
rejecting an already-seen digest, so nonce unpredictability is not a security
property. A fully deterministic tag can at worst produce a duplicate digest on
your OWN account, which the venue no-ops.
"""
from nado_protocol.utils.nonce import gen_order_nonce

# Must stay identical to the literal in venue/nado_client.py::place_order.
TAG_MASK = 0xFFFFF
NONCE_SHIFT_BITS = 20


def test_the_mask_is_exactly_the_bits_the_sdk_shift_leaves():
    assert TAG_MASK == (1 << NONCE_SHIFT_BITS) - 1
    assert TAG_MASK.bit_length() == NONCE_SHIFT_BITS


def test_the_sdk_still_shifts_by_the_width_we_mask_for():
    """Recover the shift empirically: bumping recv_time_ms by 1 must move the
    nonce by exactly 2**20. If the SDK changes its packing, this fails."""
    assert gen_order_nonce(recv_time_ms=2, random_int=0) - gen_order_nonce(
        recv_time_ms=1, random_int=0
    ) == 1 << NONCE_SHIFT_BITS


def test_a_masked_tag_round_trips_and_leaves_the_timestamp_intact():
    recv = 1_777_000_000_000
    for client_id in (0, 1, 42, 999, TAG_MASK - 1, TAG_MASK):
        tag = client_id & TAG_MASK
        nonce = gen_order_nonce(recv_time_ms=recv, random_int=tag)
        assert nonce & TAG_MASK == tag, "tag must survive round-trip"
        assert nonce >> NONCE_SHIFT_BITS == recv, "tag must not disturb the timestamp"


def test_masking_is_what_stops_a_large_client_id_shifting_the_expiry():
    """A ``client_id`` above the 20-bit space is the failure this mask prevents:
    unmasked it carries into ``recv_time_ms`` and changes the order's expiry."""
    recv = 1_777_000_000_000
    oversized = TAG_MASK + 7

    masked = gen_order_nonce(recv_time_ms=recv, random_int=oversized & TAG_MASK)
    assert masked >> NONCE_SHIFT_BITS == recv

    unmasked = gen_order_nonce(recv_time_ms=recv, random_int=oversized)
    assert unmasked >> NONCE_SHIFT_BITS == recv + 1, (
        "an unmasked oversized client_id bleeds into the timestamp — this is the "
        "corruption the mask exists to prevent"
    )


def test_place_order_uses_the_pinned_mask_literal():
    """Guard against the source drifting away from this pin."""
    import inspect

    from src.nadobro.venue import nado_client

    src = inspect.getsource(nado_client.NadoClient.place_order)
    assert "& 0xFFFFF" in src, "place_order must mask client_id to 20 bits"
    assert "gen_order_nonce(random_int=tag)" in src
