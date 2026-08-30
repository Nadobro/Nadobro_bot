"""Focused contract tests for the intentionally small Mid Mode setup UI."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.nadobro.handlers import strategy_handler as sh


def _callbacks(markup):
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]


def test_mid_setup_exposes_only_user_facing_controls():
    callbacks = _callbacks(sh._strategy_config_section_kb("mid", "setup", 50))

    expected = {
        "strategy:set:mid:notional_usd:50",
        "strategy:set:mid:notional_usd:100",
        "strategy:set:mid:notional_usd:250",
        "strategy:input:mid:notional_usd",
        "strategy:set:mid:mm_leverage_override:1",
        "strategy:set:mid:mm_leverage_override:2",
        "strategy:set:mid:mm_leverage_override:3",
        "strategy:set:mid:mm_leverage_override:5",
        "strategy:set:mid:mm_leverage_override:10",
        "strategy:set:mid:mm_leverage_override:20",
        "strategy:set:mid:mm_leverage_override:40",
        "strategy:set:mid:mm_leverage_override:50",
        "strategy:input:mid:mm_leverage_override",
        "strategy:set:mid:spread_bp:2",
        "strategy:set:mid:spread_bp:5",
        "strategy:set:mid:spread_bp:25",
        "strategy:input:mid:spread_bp",
        "strategy:set_text:mid:mid_execution_mode:aggressive",
        "strategy:set_text:mid:mid_execution_mode:normal",
        "strategy:set_text:mid:mid_execution_mode:passive",
        # RUN DURATION + CADENCE (user request 2026-08-30): a Mid session can now be
        # given a hard run duration (mm_duration_minutes, honored as a stop by the
        # runtime) and a custom fast-cadence interval (interval_seconds), matching
        # the controls grid/dgrid/rgrid already expose.
        "strategy:set:mid:mm_duration_minutes:30",
        "strategy:set:mid:mm_duration_minutes:120",
        "strategy:input:mid:mm_duration_minutes",
        "strategy:set:mid:interval_seconds:5",
        "strategy:set:mid:interval_seconds:8",
        "strategy:input:mid:interval_seconds",
        "strategy:set:mid:directional_bias:-0.5",
        "strategy:set:mid:directional_bias:0",
        "strategy:set:mid:directional_bias:0.5",
        # The ladder knobs stay on the card: the mid mapping consumes levels
        # (-> ladder_levels) and size_curve (-> ladder_curve), so removing the
        # buttons would orphan a live feature. Guarded end-to-end by
        # tests/handlers/test_ladder_ui_wiring.py.
        "strategy:set:mid:levels:1",
        "strategy:set:mid:levels:2",
        "strategy:set:mid:levels:4",
        "strategy:input:mid:levels",
        "strategy:set_text:mid:size_curve:flat",
        "strategy:set_text:mid:size_curve:linear",
        "strategy:set_text:mid:size_curve:geometric",
    }
    assert set(callbacks[:-1]) == expected
    assert callbacks[-1] == "strategy:config:mid"

    # Still intentionally off the mid card (the mapping ignores them or they are
    # engine-internal): min/max spread bounds, POV participation, TWAP pause, and
    # the old margin presets. (mm_duration_minutes is now exposed above — a user
    # needs a run-duration cap so the session doesn't stop at an arbitrary time.)
    obsolete_fields = (
        "min_spread_bp", "max_spread_bp",
        "participation_preset",
        "twap_pause_move_bp",
    )
    assert not any(
        any(f":{field}:" in callback for field in obsolete_fields)
        for callback in callbacks
    )
    assert not any(callback.startswith("strategy:preset:mid:") for callback in callbacks)


def test_mid_risk_exposes_all_four_safety_rails_without_tuning_knobs():
    callbacks = _callbacks(sh._strategy_config_section_kb("mid", "risk"))
    joined = " ".join(callbacks)

    for field in (
        "tp_pct", "sl_pct", "inventory_soft_limit_usd",
        "inventory_hard_limit_usd", "expected_budget_usd",
    ):
        assert f"mid:{field}" in joined
    assert "strategy:set:mid:tp_pct:0" in callbacks
    assert "strategy:set:mid:sl_pct:0" in callbacks

    for field in (
        "levels", "size_curve", "spread_bp", "interval_seconds",
        "participation_preset", "mm_leverage_override", "mm_duration_minutes",
        "twap_pause_move_bp",
    ):
        assert f"mid:{field}" not in joined


def test_mid_displayed_fields_route_to_their_visible_sections():
    for field in (
        "notional_usd", "mm_leverage_override", "spread_bp",
        "mid_execution_mode", "directional_bias",
    ):
        assert sh._strategy_section_for_field("mid", field) == "setup"
    for field in (
        "tp_pct", "sl_pct", "inventory_soft_limit_usd",
        "inventory_hard_limit_usd", "expected_budget_usd",
    ):
        assert sh._strategy_section_for_field("mid", field) == "risk"


def test_mid_custom_hard_inventory_and_budget_inputs_are_saved():
    """Every Mid Risk custom button must survive the pending chat-input path."""
    from src.nadobro.handlers import messages

    for field, typed_value, expected_value in (
        ("inventory_hard_limit_usd", "125", 125.0),
        ("expected_budget_usd", "0", 0.0),
    ):
        saved = {}

        def _update_settings(_telegram_id, mutate):
            settings = {"strategies": {"mid": {}}}
            mutate(settings)
            saved.update(settings["strategies"]["mid"])
            return "mainnet", settings

        context = SimpleNamespace(
            user_data={
                "pending_strategy_input": {
                    "strategy": "mid",
                    "field": field,
                    "section": "risk",
                }
            }
        )
        with patch.object(messages, "update_user_settings", _update_settings), \
             patch.object(messages, "run_blocking", AsyncMock(return_value=None)), \
             patch.object(messages, "_reply_loc", AsyncMock()), \
             patch.object(messages, "fmt_strategy_update", return_value="updated"):
            handled = asyncio.run(
                messages._handle_pending_strategy_input(
                    SimpleNamespace(message=SimpleNamespace()), context, 7, typed_value
                )
            )

        assert handled is True
        assert saved[field] == expected_value
        assert "pending_strategy_input" not in context.user_data


def test_mid_leverage_buttons_and_typed_input_share_the_selected_asset_cap():
    capped_callbacks = _callbacks(sh._strategy_config_section_kb("mid", "setup", 10))
    leverage_callbacks = [
        callback for callback in capped_callbacks if "mm_leverage_override" in callback
    ]
    assert "strategy:set:mid:mm_leverage_override:10" in leverage_callbacks
    assert not any(callback.endswith(":20") or callback.endswith(":40") for callback in leverage_callbacks)

    from src.nadobro.handlers import messages

    saved = {}

    def _update_settings(_telegram_id, mutate):
        settings = {"strategies": {"mid": {}}}
        mutate(settings)
        saved.update(settings["strategies"]["mid"])
        return "mainnet", settings

    context = SimpleNamespace(
        user_data={
            "strategy_pair:mid": "QQQ",
            "pending_strategy_input": {
                "strategy": "mid",
                "field": "mm_leverage_override",
                "section": "setup",
            },
        }
    )
    reply = AsyncMock()
    with patch.object(messages, "get_product_max_leverage", return_value=10), \
         patch.object(messages, "get_user", return_value=None), \
         patch.object(messages, "update_user_settings", _update_settings), \
         patch.object(messages, "_reply_loc", reply):
        handled = asyncio.run(
            messages._handle_pending_strategy_input(
                SimpleNamespace(message=SimpleNamespace()), context, 7, "11"
            )
        )

    assert handled is True
    assert saved == {}
    assert "Max leverage for QQQ is 10x" in reply.await_args.args[1]
    assert "pending_strategy_input" in context.user_data

    with patch.object(messages, "get_product_max_leverage", return_value=10), \
         patch.object(messages, "get_user", return_value=None), \
         patch.object(messages, "update_user_settings", _update_settings), \
         patch.object(messages, "run_blocking", AsyncMock(return_value=None)), \
         patch.object(messages, "_reply_loc", AsyncMock()), \
         patch.object(messages, "fmt_strategy_update", return_value="updated"):
        handled = asyncio.run(
            messages._handle_pending_strategy_input(
                SimpleNamespace(message=SimpleNamespace()), context, 7, "10"
            )
        )

    assert handled is True
    assert saved["mm_leverage_override"] == 10
    assert "pending_strategy_input" not in context.user_data


def test_mid_setup_copy_shows_margin_leverage_and_position_notional():
    text = sh._strategy_config_section_text(
        "mid",
        {"notional_usd": 100.0, "mm_leverage_override": 5},
        "mainnet",
        "setup",
    )

    assert "Margin: *$100*" in text
    assert "Leverage: *5x*" in text
    assert "Position: *$500*" in text


def test_mid_start_card_uses_setup_label_without_changing_other_cards():
    from src.nadobro.handlers.keyboards import strategy_action_kb

    mid_labels = [
        button.text
        for row in strategy_action_kb("mid", "BTC", ["BTC"]).inline_keyboard
        for button in row
    ]
    grid_labels = [
        button.text
        for row in strategy_action_kb("grid", "BTC", ["BTC"]).inline_keyboard
        for button in row
    ]
    assert "⚙️ Setup" in mid_labels
    assert "⚙️ Advanced" not in mid_labels
    assert "⚙️ Advanced" in grid_labels