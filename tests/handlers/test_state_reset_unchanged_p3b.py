"""Nado's state reset is untouched by Arcus P3b (03 D-14, §19.13).

``arcus_link_pending`` is deliberately NOT a transient key: a Nado Home tap or a
venue switch must not end the Arcus link flow (the interceptor scope "or an
Arcus link pending", D-11, has to survive a switch to Nado before the paste).
"""
from __future__ import annotations

from types import SimpleNamespace

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.handlers import state_reset  # noqa: E402

UID = 990_034_201


def test_the_arcus_link_flow_is_not_a_transient_key():
    assert "arcus_link_pending" not in state_reset._TRANSIENT_USER_DATA_KEYS
    assert not any(k.startswith("arcus") for k in state_reset._TRANSIENT_USER_DATA_KEYS)


def test_clear_pending_user_state_keeps_the_arcus_flow_and_calls_exactly_the_four_clearers(monkeypatch):
    called = []
    for name in ("clear_strategy_pending_input", "clear_text_trade_pending",
                 "clear_text_close_all_pending", "clear_wallet_pending_flow"):
        monkeypatch.setattr(state_reset, name, (lambda n: (lambda uid: called.append((n, uid))))(name))
    flow = object()
    context = SimpleNamespace(user_data={"arcus_link_pending": flow, "pending_trade": {"x": 1}})
    state_reset.clear_pending_user_state(context, UID)
    assert context.user_data == {"arcus_link_pending": flow}
    assert sorted(called) == sorted([
        ("clear_strategy_pending_input", UID), ("clear_text_trade_pending", UID),
        ("clear_text_close_all_pending", UID), ("clear_wallet_pending_flow", UID),
    ])
