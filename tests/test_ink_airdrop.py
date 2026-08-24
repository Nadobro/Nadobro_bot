"""Ink airdrop allocation lookup (/airdrop).

Archive doc: POST [ARCHIVE REWARDS] {"ink_airdrop": {"address": "0x<20-byte>"}}
returns {"amount": "<x18 string>"} with documented IP weight = 2.
The query lives on the Archive (Rewards) endpoint (.../rewards/v1), NOT the
archive indexer (.../v1) — the indexer rejects it with HTTP 422 "unknown variant
`ink_airdrop`". Verified live against archive.prod.nado.xyz/rewards/v1 on
2026-08-24 (the zero address answers {"amount":"0"} HTTP 200).
"""
import time
from decimal import Decimal
from unittest.mock import patch

from _stubs import install_test_stubs

install_test_stubs()

ADDR = "0x1234567890123456789012345678901234567890"


def _clear_airdrop_cache():
    import src.nadobro.venue.nado_archive as archive

    archive._AIRDROP_CACHE.clear()


# --- address normalization ---------------------------------------------------

def test_normalize_evm_address_accepts_canonical_forms():
    from src.nadobro.venue.nado_archive import normalize_evm_address

    checksummed = "0xAbCdEF1234567890123456789012345678901234"
    assert normalize_evm_address(ADDR) == ADDR
    assert normalize_evm_address(checksummed) == checksummed.lower()
    assert normalize_evm_address("0X" + ADDR[2:]) == ADDR
    assert normalize_evm_address(ADDR[2:]) == ADDR  # bare 40-hex
    assert normalize_evm_address(f"  {ADDR}  ") == ADDR  # whitespace


def test_normalize_evm_address_rejects_garbage():
    from src.nadobro.venue.nado_archive import normalize_evm_address

    assert normalize_evm_address(None) == ""
    assert normalize_evm_address("") == ""
    assert normalize_evm_address("0x123") == ""  # too short
    assert normalize_evm_address(ADDR + "ab") == ""  # too long (txhash-ish)
    assert normalize_evm_address("0x" + "g" * 40) == ""  # non-hex
    assert normalize_evm_address("0x" + ADDR) == ""  # double prefix


# --- query parsing -----------------------------------------------------------

def test_query_ink_airdrop_parses_x18_string():
    import src.nadobro.venue.nado_archive as archive

    _clear_airdrop_cache()
    with patch.object(archive, "_post", return_value={"amount": "1000000000000000000"}):
        assert archive.query_ink_airdrop("mainnet", ADDR) == Decimal("1")

    _clear_airdrop_cache()
    with patch.object(archive, "_post", return_value={"amount": "1234500000000000000000"}):
        assert archive.query_ink_airdrop("mainnet", ADDR) == Decimal("1234.5")


def test_query_ink_airdrop_zero_is_zero_not_none():
    import src.nadobro.venue.nado_archive as archive

    _clear_airdrop_cache()
    with patch.object(archive, "_post", return_value={"amount": "0"}):
        amount = archive.query_ink_airdrop("mainnet", ADDR)
    assert amount == Decimal("0")
    assert amount is not None


def test_query_ink_airdrop_none_on_archive_failure_and_not_cached():
    import src.nadobro.venue.nado_archive as archive

    _clear_airdrop_cache()
    with patch.object(archive, "_post", return_value=None):
        assert archive.query_ink_airdrop("mainnet", ADDR) is None
    assert archive._AIRDROP_CACHE == {}


def test_query_ink_airdrop_none_on_malformed_response():
    import src.nadobro.venue.nado_archive as archive

    for bad in ({"amount": "abc"}, {"amount": None}, {"amount": 1.5}, {}, [], "x"):
        _clear_airdrop_cache()
        with patch.object(archive, "_post", return_value=bad):
            assert archive.query_ink_airdrop("mainnet", ADDR) is None, bad
    _clear_airdrop_cache()
    with patch.object(archive, "_post", return_value={"amount": "-5"}):
        assert archive.query_ink_airdrop("mainnet", ADDR) is None


def test_query_ink_airdrop_invalid_address_never_hits_archive():
    import src.nadobro.venue.nado_archive as archive

    _clear_airdrop_cache()
    with patch.object(archive, "_post") as mock_post:
        assert archive.query_ink_airdrop("mainnet", "not-an-address") is None
        assert archive.query_ink_airdrop("mainnet", "") is None
    mock_post.assert_not_called()


def test_query_ink_airdrop_posts_to_the_rewards_endpoint_not_the_indexer():
    """REGRESSION GUARD (2026-08-24). The ``ink_airdrop`` query MUST go to the
    Archive (Rewards) endpoint (``.../rewards/v1``), NOT the archive indexer
    (``.../v1``). Posting it to the indexer returns HTTP 422 "unknown variant
    `ink_airdrop`" (the indexer's request enum has no airdrop variant), which is
    what made ``/airdrop`` always answer "Couldn't reach the Nado archive".
    Verified live: the rewards endpoint returns ``{"amount": ...}`` HTTP 200."""
    import src.nadobro.venue.nado_archive as archive
    from src.nadobro.config import (
        NADO_MAINNET_ARCHIVE,
        NADO_MAINNET_ARCHIVE_REWARDS,
        NADO_TESTNET_ARCHIVE_REWARDS,
    )

    _clear_airdrop_cache()
    checksummed = "0xAbCdEF1234567890123456789012345678901234"
    with patch.object(archive, "_post", return_value={"amount": "0"}) as mock_post:
        archive.query_ink_airdrop("mainnet", checksummed)
    mock_post.assert_called_once_with(
        NADO_MAINNET_ARCHIVE_REWARDS,
        {"ink_airdrop": {"address": checksummed.lower()}},
    )
    # It must NOT be the indexer endpoint (the bug).
    assert mock_post.call_args.args[0] != NADO_MAINNET_ARCHIVE
    assert mock_post.call_args.args[0].endswith("/rewards/v1")

    _clear_airdrop_cache()
    with patch.object(archive, "_post", return_value={"amount": "0"}) as mock_post:
        archive.query_ink_airdrop("testnet", checksummed)
    mock_post.assert_called_once_with(
        NADO_TESTNET_ARCHIVE_REWARDS,
        {"ink_airdrop": {"address": checksummed.lower()}},
    )


def test_query_ink_airdrop_caches_per_network_and_address():
    import src.nadobro.venue.nado_archive as archive

    _clear_airdrop_cache()
    with patch.object(archive, "_post", return_value={"amount": "1000000000000000000"}) as mock_post:
        first = archive.query_ink_airdrop("mainnet", ADDR)
        second = archive.query_ink_airdrop("mainnet", ADDR)  # served from cache
        archive.query_ink_airdrop("testnet", ADDR)  # distinct network -> new call
        archive.query_ink_airdrop("mainnet", ADDR, refresh=True)  # forced -> new call
    assert first == second == Decimal("1")
    assert mock_post.call_count == 3


def test_query_ink_airdrop_cache_expires():
    import src.nadobro.venue.nado_archive as archive

    _clear_airdrop_cache()
    with patch.object(archive, "_post", return_value={"amount": "1000000000000000000"}) as mock_post:
        archive.query_ink_airdrop("mainnet", ADDR)
        key = ("mainnet", ADDR)
        ts, amount = archive._AIRDROP_CACHE[key]
        archive._AIRDROP_CACHE[key] = (
            time.time() - archive._AIRDROP_CACHE_TTL_SECONDS - 1,
            amount,
        )
        archive.query_ink_airdrop("mainnet", ADDR)
    assert mock_post.call_count == 2


# --- rate-limit weight -------------------------------------------------------

def test_ink_airdrop_weight_matches_docs():
    from src.nadobro.venue.nado_weights import query_weight
    from src.nadobro.venue.nado_archive import _derive_archive_weight

    # Documented weight = 2 (not the conservative default of 5).
    assert query_weight("ink_airdrop") == 2
    assert _derive_archive_weight({"ink_airdrop": {"address": ADDR}}) == 2.0


# --- display formatting ------------------------------------------------------

def test_fmt_ink_amount_display():
    from src.nadobro.handlers.formatters import _fmt_ink_amount

    assert _fmt_ink_amount(Decimal("0")) == "0"
    assert _fmt_ink_amount(Decimal("1")) == "1"
    assert _fmt_ink_amount(Decimal("1234.5")) == "1,234.5"
    assert _fmt_ink_amount(Decimal("1000000")) == "1,000,000"
    # Rounded DOWN at 4 decimals — never overstate an allocation.
    assert _fmt_ink_amount(Decimal("0.12349")) == "0.1234"
    # Dust below the 4-decimal cut renders at full precision, not "0".
    assert _fmt_ink_amount(Decimal("1E-18")) == "0.000000000000000001"


def test_fmt_ink_airdrop_card_states():
    from src.nadobro.handlers.formatters import fmt_ink_airdrop_card

    card = fmt_ink_airdrop_card(ADDR, Decimal("1234.5"))
    assert "Ink Airdrop" in card
    assert "0x1234" in card and "7890" in card  # shortened address
    assert ADDR not in card  # never the full address
    assert "1,234\\.5 INK" in card

    zero_card = fmt_ink_airdrop_card(ADDR, Decimal("0"))
    # Zero must read as "not recorded yet", not as a final verdict: the venue
    # table only holds distributions loaded so far (points->INK lands at TGE).
    assert "No Ink airdrop allocation recorded" in zero_card
    assert "yet" in zero_card
    assert "INK*" not in zero_card
