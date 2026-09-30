"""Volume Bot charges agreement — the card, the band, and the audit stamp.

The user consents to real money off this card, so what it claims and what the
engine then runs must be the same numbers.
"""
from decimal import Decimal

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.handlers.strategy_handler import (  # noqa: E402
    _vol_fee_agreement_text,
)
from src.nadobro.quant.vol_fee_estimator import estimate_vol_fees  # noqa: E402


def _est(**kw):
    base = dict(
        margin_usd=100,
        target_volume_usd=10_000,
        taker_fee_rate=Decimal("0.0004"),
        builder_fee_rate=Decimal("0.0001"),
    )
    base.update(kw)
    return estimate_vol_fees(**base)


def _plain(text: str) -> str:
    """Drop MarkdownV2 escapes so assertions read as the user sees the card."""
    return text.replace("\\", "")


def test_card_states_every_charge_component():
    text = _plain(_vol_fee_agreement_text(_est(), "KBTC", "mainnet"))
    assert "KBTC SPOT" in text
    assert "4.00 bp" in text        # taker
    assert "1.00 bp" in text        # builder
    assert "5.00 bp" in text        # total
    assert "$5.00" in text          # estimated fee on $10k of volume
    assert "Estimated round trips" in text
    assert "50" in text             # 10_000 / (2 x 100)


def test_card_carries_the_required_slippage_disclaimer():
    text = _plain(_vol_fee_agreement_text(_est(), "KBTC", "mainnet"))
    assert "slippage" in text.lower()
    # Execution is a bounded marketable limit, so the copy says "take
    # liquidity at market" rather than claiming a naked market order.
    assert "at market" in text.lower()
    assert "exceed this estimate" in text.lower()


def test_card_says_start_means_agreeing_to_charges():
    text = _plain(_vol_fee_agreement_text(_est(), "KBTC", "mainnet"))
    assert "agree to these charges" in text.lower()


def test_card_is_honest_about_the_rate_basis():
    """A measured fallback must not be dressed up as a venue-published rate."""
    venue = _plain(_vol_fee_agreement_text(_est(), "KBTC", "mainnet"))
    assert "venue rate" in venue

    measured = _plain(_vol_fee_agreement_text(_est(taker_fee_rate=None), "KBTC", "mainnet"))
    assert "measured avg" in measured


def test_out_of_band_margin_is_shown_clamped_not_as_requested():
    """The card must quote the margin the engine will actually use — the
    mapping clamps to the band, so an unclamped card would misstate the run."""
    text = _plain(_vol_fee_agreement_text(_est(margin_usd=5000), "KBTC", "mainnet"))
    assert "$500" in text
    assert "$5,000" not in text


def test_margin_band_matches_between_button_input_and_chat_input():
    """The two validators disagreed before v4 (handler floor 100 vs chat floor
    10). A user could type a size the buttons forbid."""
    import re

    from src.nadobro.handlers import messages as msg_mod
    import src.nadobro.handlers.strategy_handler as sh_mod

    handler_src = open(sh_mod.__file__).read()
    messages_src = open(msg_mod.__file__).read()
    pattern = r'"session_margin_usd":\s*\((\d+),\s*(\d+)\)'
    assert re.search(pattern, handler_src).groups() == ("100", "500")
    assert re.search(pattern, messages_src).groups() == ("100", "500")


# --------------------------------------------------------------------------
# Consent coverage: "starting the bot means agreeing to the charges" only
# holds if EVERY start path shows the charges. There are exactly two ways to
# start a strategy — the callback (gated by the card) and the managed AI
# agent, which calls start_user_bot directly and cannot render the card.
# --------------------------------------------------------------------------
def test_managed_agent_cannot_start_vol_without_the_fee_card():
    import src.nadobro.llm.managed_agent_service as mas

    src = open(mas.__file__).read()
    guard = src.split("ok, msg = start_user_bot(")[0]
    assert 'if strategy == "vol":' in guard, (
        "the managed agent must refuse vol before reaching start_user_bot — "
        "otherwise 'start volume bot' runs a taker session with no charges shown"
    )
    assert "strategy_start_needs_fee_ack" in guard


def test_callback_start_is_gated_behind_startok():
    import src.nadobro.handlers.strategy_handler as sh

    src = open(sh.__file__).read()
    assert 'action in ("start", "startok")' in src
    assert 'if strategy_id == "vol" and not fee_agreed:' in src
    assert "strategy:startok:vol:" in src


def test_card_and_engine_quote_the_identical_rate():
    """The card's rate and the engine's rate must come from ONE resolver.

    They used to be two independent call sites with different symbol
    normalization (the card passed the raw UI product, the mapping passed the
    catalog-normalized pair), so a card could quote 5.3bp while the run traded
    at 4.5bp — the user agreeing to a price that was not the price.
    """
    from decimal import Decimal

    from src.nadobro.strategy import engine_runtime as er

    for product in ("KBTC", "KBTC-USDC0", "WETH"):
        taker, builder = er.resolve_vol_fee_rates(product, "mainnet")
        cfg = er.map_strategy_config(
            "vol", {"session_margin_usd": 100.0, "target_volume_usd": 10_000.0},
            Decimal(64000), product=product, network="mainnet",
        )
        engine_total = (
            Decimal(str(cfg["spot_taker_fee_rate"])) + Decimal(str(cfg["vol_builder_fee_rate"]))
        )
        card = estimate_vol_fees(
            margin_usd=100, target_volume_usd=10_000,
            taker_fee_rate=taker, builder_fee_rate=builder,
        )
        assert card.total_rate == engine_total, (
            f"{product}: card quotes {card.total_rate} but engine trades {engine_total}"
        )
        assert card.taker_fee_rate > 0, "a 0 rate would make a round trip look free"


def test_card_states_the_session_stop_and_when_it_will_trip():
    """v4.1: both legs are taker with no profit gate, so every round trip costs
    fees + spread and the loss walks toward the stop. The card must state the
    stop in dollars AND warn when the target is unreachable before it trips —
    consenting to a run that provably cannot finish is the failure mode."""
    # $100 margin, 5% stop = $5 budget; at 5bp that is ~$10k of volume.
    text = _plain(_vol_fee_agreement_text(_est(), "KBTC", "mainnet", sl_pct=5.0))
    assert "session stop" in text.lower()
    assert "$5.00" in text
    assert "closes everything" in text.lower()

    # A 1% stop ($1) reaches only ~$2k — far short of the $10k target.
    tight = _plain(_vol_fee_agreement_text(_est(), "KBTC", "mainnet", sl_pct=1.0))
    assert "less than your" in tight.lower()
    assert "$2,000" in tight

    # A generous stop clears the target: no shortfall warning.
    roomy = _plain(_vol_fee_agreement_text(_est(), "KBTC", "mainnet", sl_pct=25.0))
    assert "less than your" not in roomy.lower()

    # No configured stop -> no stop section at all.
    none = _plain(_vol_fee_agreement_text(_est(), "KBTC", "mainnet", sl_pct=0.0))
    assert "session stop" not in none.lower()


def test_consent_is_single_use():
    """F8: the fee card's button must be stripped once consumed, so a stale
    card cannot start a run with inputs the user never saw quoted."""
    import src.nadobro.handlers.strategy_handler as sh

    src = open(sh.__file__).read()
    ack = src.split('if strategy_id == "vol" and fee_agreed:')[1][:2400]
    assert "edit_message_reply_markup(reply_markup=None)" in ack


def test_card_min_notional_floor_works_for_SPOT_pairs():
    """The card's size floor used the PERP-only accessor, which returns None
    for every spot Volume pair — so it was dead exactly where it applies and
    the card quoted $100 while the engine traded $115."""
    from src.nadobro.venue.product_catalog import (
        get_product_min_quote_notional_usd,
        get_spot_min_notional_usd,
    )

    # The perp accessor is genuinely blind to spot (this is the bug's root).
    assert get_product_min_quote_notional_usd("KBTC", network="mainnet") is None
    # The spot accessor sees it.
    assert (get_spot_min_notional_usd("KBTC", network="mainnet") or 0) > 0

    # And the card now quotes the floored size the engine will really trade.
    import src.nadobro.handlers.strategy_handler as sh

    src = open(sh.__file__).read()
    assert "get_spot_min_notional_usd" in src
    assert "get_product_min_quote_notional_usd" not in src.split("_vol_fee_estimate")[1][:1200]


def test_consent_is_bound_to_the_quoted_numbers():
    """The startok callback carries only the product, so the estimate is
    recomputed at ack time. Without binding consent to what was SHOWN, a
    settings change between rendering and tapping starts the run on numbers
    the user never saw — and the ack record rewrites itself to match,
    destroying the audit trail too."""
    from src.nadobro.handlers.strategy_handler import _vol_fee_quote_key

    shown = _vol_fee_quote_key(_est(), sl_pct=5.0, network="mainnet")
    assert _vol_fee_quote_key(_est(), sl_pct=5.0, network="mainnet") == shown, "stable when nothing moved"

    # Each user-agreed dimension must invalidate the consent.
    assert _vol_fee_quote_key(_est(margin_usd=250), sl_pct=5.0, network="mainnet") != shown
    assert _vol_fee_quote_key(_est(target_volume_usd=50_000), sl_pct=5.0, network="mainnet") != shown
    assert _vol_fee_quote_key(_est(taker_fee_rate=Decimal("0.0009")), sl_pct=5.0, network="mainnet") != shown
    assert _vol_fee_quote_key(_est(), sl_pct=10.0, network="mainnet") != shown, "the stop is agreed to as well"
    # PREVIEW-NETWORK-BIND: consent to testnet charges is not consent to the
    # same numbers on mainnet.
    assert _vol_fee_quote_key(_est(), sl_pct=5.0, network="testnet") != shown, "the network is agreed to as well"


def test_ack_path_rechecks_the_quote_before_starting():
    import src.nadobro.handlers.strategy_handler as sh

    src = open(sh.__file__).read()
    ack = src.split('if strategy_id == "vol" and fee_agreed:')[1][:1600]
    assert '_vol_fee_quote_key' in ack
    assert 'context.user_data.get("vol_fee_quote") != _live_key' in ack
    assert "settings changed" in ack.lower()
