"""/mm_status surfacing the Mid Mode v3 state.

``ladder_metrics()`` had no reader at all, so a user could not tell which
playbook was running, whether the fee floor had been raised, or — the one that
matters operationally — whether the signal feed was alive.

The property guarded hardest is that ABSENCE stays visible as absence: a
session that has not ticked yet must show nothing, not "VOLUME / alpha 0.00",
which would read as a measured fact.
"""
from src.nadobro.strategy import mm_dashboard


def _snapshot(engine_metrics=None, **state_kw):
    state = {"running": True, "leverage": 10.0}
    if engine_metrics is not None:
        state["mm_engine_metrics"] = engine_metrics
    state.update(state_kw)
    return mm_dashboard.build_status_snapshot(
        state=state, strategy_id="mid", network="mainnet",
        product="BTC-PERP", open_orders_count=4,
    )


def _lines(engine_metrics=None, **kw):
    return mm_dashboard.render_status_lines(_snapshot(engine_metrics, **kw))


# --- absence ----------------------------------------------------------------

def test_a_session_that_has_not_ticked_shows_no_v3_block():
    snap = _snapshot()
    assert snap["has_engine_metrics"] is False
    assert snap["mm_profile"] == ""
    assert not any("Profile:" in line for line in mm_dashboard.render_status_lines(snap))


def test_metrics_without_a_resolved_profile_still_show_nothing():
    # The controller reports every tick; the profile is empty until the book
    # can be read. Printing "Profile:" with a blank value is worse than silence.
    lines = _lines({"profile": "", "alpha": 0.0})
    assert not any("Profile:" in line for line in lines)


def test_other_strategies_are_unaffected():
    snap = mm_dashboard.build_status_snapshot(
        state={"running": True}, strategy_id="grid", network="mainnet",
        product="BTC-PERP", open_orders_count=2,
    )
    assert snap["has_engine_metrics"] is False
    assert not any("Profile:" in line for line in mm_dashboard.render_status_lines(snap))


# --- the block --------------------------------------------------------------

def test_the_resolved_profile_and_fee_floor_are_shown():
    lines = _lines({"profile": "spread", "half_spread_floor_bp": 3.5, "alpha": 0.0})
    line = next(line for line in lines if line.startswith("Profile:"))
    assert "SPREAD" in line and "3.5 bp" in line


def test_alpha_is_shown_with_its_confidence_and_anchor_shift():
    lines = _lines({"profile": "volume", "alpha": 0.21,
                    "alpha_confidence": 0.62, "alpha_offset_bp": 4.2})
    line = next(line for line in lines if line.startswith("Alpha:"))
    assert "+0.21" in line and "62%" in line and "+4.2 bp" in line


def test_a_degraded_feed_is_stated_plainly_instead_of_an_alpha_of_zero():
    # THE operational line. "Alpha: +0.00" would read as a measured neutral
    # view; the user needs to know the forecast is simply absent.
    lines = _lines({"profile": "volume", "signal_degraded": True, "alpha": 0.0})
    assert any("DEGRADED" in line for line in lines)
    assert not any(line.startswith("Alpha:") for line in lines)


def test_the_adjustments_line_only_appears_when_something_is_adjusted():
    quiet = _lines({"profile": "volume", "alpha": 0.1})
    assert not any(line.startswith("Adjustments:") for line in quiet)

    busy = _lines({"profile": "volume", "alpha": 0.1,
                   "reservation_offset_bp": -2.5, "markout_widen": 1.4,
                   "self_trade_blocks": 3})
    line = next(line for line in busy if line.startswith("Adjustments:"))
    assert "inventory -2.5 bp" in line
    assert "mark-out widen 1.40x" in line
    assert "self-trade blocked 3" in line


def test_a_neutral_markout_factor_is_not_reported_as_an_adjustment():
    lines = _lines({"profile": "volume", "alpha": 0.1, "markout_widen": 1.0})
    assert not any(line.startswith("Adjustments:") for line in lines)


def test_the_snapshot_carries_the_raw_values_for_other_consumers():
    snap = _snapshot({
        "profile": "spread", "half_spread_floor_bp": 3.5, "alpha": -0.3,
        "alpha_confidence": 0.5, "alpha_offset_bp": -5.0,
        "reservation_offset_bp": 1.25, "markout_widen": 1.75,
        "self_trade_blocks": 2, "ladder_live_bids": 3, "ladder_live_asks": 2,
        "signal_degraded": False,
    })
    assert snap["mm_profile"] == "spread"
    assert snap["alpha"] == -0.3
    assert snap["markout_widen"] == 1.75
    assert snap["ladder_live_bids"] == 3 and snap["ladder_live_asks"] == 2
    assert snap["self_trade_blocks"] == 2


def test_a_malformed_metrics_blob_does_not_break_the_card():
    snap = _snapshot({"profile": "volume", "alpha": "junk", "markout_widen": None})
    assert snap["alpha"] == 0.0
    assert snap["markout_widen"] == 1.0
    assert mm_dashboard.render_status_lines(snap)          # still renders
