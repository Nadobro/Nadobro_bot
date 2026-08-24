"""Wake a Mid session when Hyperliquid's book moves, instead of waiting a tick.

``core/cadence`` floors strategies at 3s and caps Mid at 8s, and no controller
owns a task, so sub-second quoting is architecturally impossible. The one event
channel that already exists is ``bot_runtime.nudge_strategy_cycle`` — built for
venue WS fills, debounced, serialized per user, and explicitly documented as
safe to call from a websocket callback because it does no IO. This module feeds
it a second kind of event: a material move on HL's pushed book. Reaction goes
from up-to-8s to ~2s with machinery that already ships, and the execute budget
does not notice (a 2-level requote is 8 of ~540 weight/min).

THREE THINGS KEEP THIS FROM BECOMING A STAMPEDE
-----------------------------------------------
1. **One listener, ever.** ``ensure_registered`` attaches a MODULE-LEVEL
   function, so ``register_book_listener``'s equality dedupe actually fires. A
   per-call closure would attach a new listener on every cycle — the exact leak
   that ``hl_ws`` shipped with once.
2. **Interest expires.** A session announces itself every cycle and the entry
   ages out after ``WATCH_TTL_S``. A stopped strategy therefore stops being
   nudged on its own, with no unregister call to forget; and
   ``_nudge_async`` re-checks ``running`` anyway and negative-caches the miss.
3. **Only material moves.** HL pushes on every book event — thousands a minute
   on a major. Nudging on each one would be pure waste, so a nudge needs the
   touch to have moved ``MIN_MOVE_BP`` since the last one, and each session has
   its own interval floor on top of the debounce inside the nudge itself.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Optional, Tuple

from src.nadobro.utils.env import env_bool, env_float

logger = logging.getLogger(__name__)


def enabled() -> bool:
    return env_bool("NADO_HL_FAST_REQUOTE", True)


# How long a session stays "interested" without renewing. Comfortably longer
# than Mid's 8s cap so a slow cycle never drops the watch, short enough that a
# stopped session stops being nudged promptly.
WATCH_TTL_S = env_float("NADO_HL_WATCH_TTL_S", 90.0)

# Move required before the feed is worth waking anyone for.
MIN_MOVE_BP = env_float("NADO_HL_REQUOTE_MOVE_BP", 3.0)

# Per-session floor. nudge_strategy_cycle debounces too; this stops us from
# calling it at book-event rate in the first place.
MIN_INTERVAL_S = env_float("NADO_HL_REQUOTE_INTERVAL_S", 2.0)

_watchers: Dict[str, Dict[Tuple[int, str], float]] = {}
_last_mid: Dict[str, float] = {}
_last_nudge: Dict[Tuple[int, str], float] = {}
_registered = False


def reset_state() -> None:
    """Tests only."""
    global _registered
    _watchers.clear()
    _last_mid.clear()
    _last_nudge.clear()
    _registered = False


def note_active(telegram_id: int, network: str, product: str) -> None:
    """Announce that this session is quoting ``product`` right now.

    Idempotent and cheap — called once per cycle from the Mid path. Renewing
    the timestamp IS the subscription; there is nothing to unsubscribe.
    """
    if not enabled():
        return
    from src.nadobro.trading.hl_signals import coin_for

    coin = coin_for(product)
    if not coin:
        return
    _watchers.setdefault(coin, {})[(int(telegram_id), str(network))] = time.time()


def _live_watchers(coin: str) -> list:
    """Non-expired watchers for a coin, pruning as it goes."""
    entries = _watchers.get(coin)
    if not entries:
        return []
    cutoff = time.time() - WATCH_TTL_S
    for key in [k for k, ts in entries.items() if ts < cutoff]:
        entries.pop(key, None)
        _last_nudge.pop(key, None)
    if not entries:
        _watchers.pop(coin, None)
        _last_mid.pop(coin, None)
        return []
    return list(entries)


def _material_move(coin: str) -> bool:
    """Has the HL touch moved enough to be worth a requote?"""
    from src.nadobro.market_data import hl_ws

    mid = hl_ws.mid(coin)
    if mid is None or mid <= 0:
        return False
    prev = _last_mid.get(coin)
    if prev is None or prev <= 0:
        _last_mid[coin] = mid          # seed: the first frame is not a move
        return False
    move_bp = abs(mid - prev) / prev * 10_000.0
    if move_bp < MIN_MOVE_BP:
        return False
    _last_mid[coin] = mid
    return True


def on_hl_book(coin: str) -> None:
    """HL book listener. Runs on the websocket's loop, so it does NO IO."""
    if not enabled():
        return
    try:
        watchers = _live_watchers(str(coin or "").upper().strip())
        if not watchers:
            return
        if not _material_move(str(coin or "").upper().strip()):
            return
        from src.nadobro.strategy.bot_runtime import nudge_strategy_cycle

        now = time.time()
        for key in watchers:
            if now - _last_nudge.get(key, 0.0) < MIN_INTERVAL_S:
                continue
            _last_nudge[key] = now
            nudge_strategy_cycle(key[0], key[1])
    except Exception:  # noqa: BLE001 - a listener bug must not kill the feed
        logger.debug("hl fast requote failed coin=%s", coin, exc_info=True)


def ensure_registered() -> None:
    """Attach the listener to the HL feed. Idempotent — see note 1 above."""
    global _registered
    if _registered or not enabled():
        return
    from src.nadobro.market_data import hl_ws

    hl_ws.register_book_listener(on_hl_book)
    _registered = True


def watch_state() -> dict:
    """Who is being watched, for /mm_status and tests."""
    return {
        "enabled": enabled(),
        "registered": _registered,
        "coins": {c: len(v) for c, v in _watchers.items()},
    }


def cached_mid(coin: str) -> Optional[float]:
    return _last_mid.get(str(coin or "").upper().strip())
