"""Fail-closed Nado network switch (testnet <-> mainnet).

The runtime is single-active-network: Stop, /status, the strategy cards and
/desk read only the user's ACTIVE network, and a strategy loop whose network is
no longer the active one stands itself down without cancelling its orders
(``bot_runtime._run_cycle``). Anything left live on the network being left is
therefore stranded where the user can't see or stop it.

So the switch STOPS what is live on the old network, and flips
``users.network_mode`` only once every item is CONFIRMED stopped. In order:

1. read everything first — the run's state, the recorded pending cleanups and
   the desk plans; any unreadable one refuses the switch with NO side effects;
2. the running strategy -> Stop-button semantics (scheduler unregister, engine
   stop, flatten + cancel, venue trigger sweep);
3. every earlier stop whose venue half never confirmed
   (``strategy/pending_cleanup.py``) -> its cancel-only sweep is retried;
4. armed / running desk plans -> cancelled, last, because a cancel cannot be
   undone; a RUNNING plan's product is also swept cancel-only, so the flip never
   waits on the desk runner's own (unconfirmed) order pull.

Anything unconfirmed — a rate-limited cleanup, a failed read — refuses the
switch: the user stays on the old network, where Stop / /stop_all / /desk still
reach it (DENIED != EMPTY), and retrying the switch retries the cleanup. A
retry is cancel-only; a position an unconfirmed flatten may have left is
reported, not closed.

Left running and only reported (owner decision 2026-09-28 — they are network-
scoped end to end, so they keep working correctly after the switch):

* copy mirrors (listed from either network in Copy Trading);
* stop-loss rules (protective closes keyed by their own network);
* the managed agent (a per-network chat mode; nothing runs in the background).

A switch never resumes or re-arms anything: switching back does not restart
what the switch stopped. User-facing text is rendered by
``handlers/formatters.fmt_network_switch_result``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from src.nadobro.llm import managed_agent_state
from src.nadobro.strategy import bot_runtime, pending_cleanup
from src.nadobro.trading import copy_service, desk_store, stop_loss_service
from src.nadobro.trading.desk_plans import ACTIVE_STATUSES, ST_RUNNING
from src.nadobro.users import user_service

logger = logging.getLogger(__name__)

NETWORKS = ("testnet", "mainnet")

# Item kinds.
STRATEGY = "strategy"
LEFTOVER_ORDERS = "leftover_orders"
DESK_PLAN = "desk_plan"
COPY_MIRROR = "copy_mirror"
STOP_LOSS_RULE = "stop_loss_rule"
MANAGED_AGENT = "managed_agent"

# Item outcomes.
STOPPED = "stopped"  # was live on the old network; confirmed stopped / cleaned
KEPT = "kept"        # deliberately left running (network-scoped, safe to keep)
FAILED = "failed"    # could not be confirmed — blocks the flip
SKIPPED = "skipped"  # not touched because the switch was already refused

# Item warnings.
POSITION_MAY_BE_OPEN = "position_may_be_open"

# Result errors.
ERR_INVALID_NETWORK = "invalid_network"
ERR_USER_NOT_FOUND = "user_not_found"
ERR_NOT_CONFIRMED = "not_confirmed"
ERR_FLIP_FAILED = "flip_failed"


@dataclass(frozen=True)
class SwitchItem:
    kind: str
    outcome: str
    label: str = ""    # strategy id / desk plan id / copied trader
    product: str = ""
    error: str = ""
    warning: str = ""


@dataclass(frozen=True)
class NetworkSwitchResult:
    switched: bool
    from_network: str
    to_network: str
    items: tuple[SwitchItem, ...] = ()
    error: str = ""
    wallet_address: str = ""

    def by_outcome(self, outcome: str) -> tuple[SwitchItem, ...]:
        return tuple(i for i in self.items if i.outcome == outcome)


def switch_network(telegram_id: int, network: str) -> NetworkSwitchResult:
    """Switch the user's active network, fail-closed (see module docstring).
    Blocking (DB + venue) — call via ``run_blocking`` from handlers."""
    target = str(network or "").strip().lower()
    if target not in NETWORKS:
        return NetworkSwitchResult(False, "", target, error=ERR_INVALID_NETWORK)
    user = user_service.get_user(telegram_id)
    if not user:
        return NetworkSwitchResult(False, "", target, error=ERR_USER_NOT_FOUND)
    source = user.network_mode.value
    address = str(user.main_address or "")
    if source == target:
        return NetworkSwitchResult(True, source, target, wallet_address=address)

    def _refused(items: list[SwitchItem]) -> NetworkSwitchResult:
        failed = [i for i in items if i.outcome == FAILED]
        logger.warning(
            "network switch refused user=%s %s->%s: %d item(s) not confirmed stopped: %s",
            telegram_id, source, target, len(failed),
            "; ".join(f"{i.kind}:{i.label or i.product}:{i.error}" for i in failed)[:500],
        )
        return NetworkSwitchResult(False, source, target, tuple(items), error=ERR_NOT_CONFIRMED,
                                   wallet_address=address)

    # 1) Read everything first: an unreadable source is refused before anything
    #    irreversible (a flatten, a desk cancel) has happened.
    try:
        state = bot_runtime.get_user_bot_state(telegram_id, source)
    except Exception as exc:  # noqa: BLE001 - an unreadable run is never "nothing running"
        return _refused([SwitchItem(STRATEGY, FAILED, error=f"could not read the strategy state: {exc}"[:240])])
    try:
        pending = pending_cleanup.list_entries(telegram_id, source)
    except Exception as exc:  # noqa: BLE001 - an unreadable record list is never "nothing pending"
        return _refused([SwitchItem(LEFTOVER_ORDERS, FAILED, error=f"could not read pending cleanups: {exc}"[:240])])
    try:
        plans = list(desk_store.list_active_plans(telegram_id, source) or [])
    except Exception as exc:  # noqa: BLE001 - an unreadable plan table is never "no plans"
        return _refused([SwitchItem(DESK_PLAN, FAILED, error=f"could not read desk plans: {exc}"[:240])])

    items: list[SwitchItem] = []
    # 2) The running strategy (Stop semantics). Its own record is deleted when
    #    the stop confirms, kept pending when it does not.
    if state.get("running"):
        items.append(_stop_strategy(telegram_id, source, state))
    # 3) Earlier stops that never confirmed their venue half — skipped when the
    #    stop above just failed (a retry now would only repeat it).
    if pending and not any(i.outcome == FAILED for i in items):
        items.extend(_retry_pending(telegram_id, source))
    if any(i.outcome == FAILED for i in items):
        items.extend(_desk_items(plans, SKIPPED))
        return _refused(items)
    # 4) Desk plans, last: a cancel cannot be undone.
    items.extend(_cancel_desk_plans(telegram_id, source, plans))
    if any(i.outcome == FAILED for i in items):
        return _refused(items)
    items.extend(_kept_automation(telegram_id, source))
    try:
        user_service.set_network_mode(telegram_id, target)
    except Exception as exc:  # noqa: BLE001 - reported; the user stays on the old network
        logger.warning("network switch flip failed user=%s %s->%s: %s", telegram_id, source, target, exc)
        return NetworkSwitchResult(
            False, source, target, tuple(items), error=ERR_FLIP_FAILED, wallet_address=address,
        )
    logger.info(
        "network switch user=%s %s->%s stopped=%d kept=%d",
        telegram_id, source, target, sum(1 for i in items if i.outcome == STOPPED),
        sum(1 for i in items if i.outcome == KEPT),
    )
    return NetworkSwitchResult(True, source, target, tuple(items), wallet_address=address)


def _stop_strategy(telegram_id: int, network: str, state: dict) -> SwitchItem:
    try:
        res = bot_runtime.stop_strategy_for_network_switch(telegram_id, network, state)
    except Exception as exc:  # noqa: BLE001 - an unstoppable run blocks the flip
        logger.warning("network switch: strategy stop failed user=%s %s: %s", telegram_id, network, exc)
        return SwitchItem(STRATEGY, FAILED, label=str(state.get("strategy") or ""),
                          product=str(state.get("product") or "").upper(), error=str(exc)[:240])
    return SwitchItem(
        STRATEGY,
        STOPPED if res.get("ok") else FAILED,
        label=str(res.get("strategy") or ""),
        product=str(res.get("product") or ""),
        error=str(res.get("error") or "")[:240],
    )


def _retry_pending(telegram_id: int, network: str) -> list[SwitchItem]:
    try:
        results = bot_runtime.retry_pending_cleanups(telegram_id, network)
    except Exception as exc:  # noqa: BLE001 - an unreadable record list is never "nothing pending"
        return [SwitchItem(LEFTOVER_ORDERS, FAILED, error=f"could not retry pending cleanups: {exc}"[:240])]
    return [
        SwitchItem(
            LEFTOVER_ORDERS,
            STOPPED if r.get("ok") else FAILED,
            label=str(r.get("strategy") or ""),
            product=str(r.get("product") or ""),
            error=str(r.get("error") or "")[:240],
            warning=POSITION_MAY_BE_OPEN if (r.get("ok") and r.get("flatten_unconfirmed")) else "",
        )
        for r in results
    ]


def _plan_bits(rec: dict) -> tuple[str, str, str, str]:
    plan = rec.get("plan")
    plan_id = str(getattr(plan, "plan_id", None) or rec.get("plan_id") or "")
    product = str(getattr(plan, "product", None) or "").upper()
    market = str(getattr(plan, "market", None) or "perp").lower()
    return plan_id, product, market, str(rec.get("status") or "")


def _desk_items(plans: list[dict], outcome: str) -> list[SwitchItem]:
    return [SwitchItem(DESK_PLAN, outcome, label=pid, product=product)
            for pid, product, _m, _s in (_plan_bits(r) for r in plans)]


def _begin_desk_cleanup(telegram_id: int, network: str, plan_id: str, product: str, market: str):
    pids = pending_cleanup.product_ids_for("desk", product, network, market=market) or []
    pending = pending_cleanup.begin(
        telegram_id, network, {"strategy": "desk", "product": product}, "network_switch",
        market=market, run_id=plan_id, product_ids=pids,
    )
    return pending, pids


def _cancel_desk_plans(telegram_id: int, network: str, plans: list[dict]) -> list[SwitchItem]:
    items: list[SwitchItem] = []
    for rec in plans:
        plan_id, product, market, status = _plan_bits(rec)
        # A RUNNING plan may have orders resting; the desk runner pulls them on a
        # later tick and only logs a failure, so sweep its product cancel-only
        # here and keep the record pending until that is confirmed. Its fills
        # (a position) are kept, as for a /desk Cancel. An armed plan has no
        # venue orders — its trigger is watched by the runner.
        running = status == ST_RUNNING
        pending, pids = _begin_desk_cleanup(telegram_id, network, plan_id, product, market) if running else (None, [])
        try:
            if not desk_store.cancel_plan(plan_id, telegram_id, network):
                # Lost the guarded UPDATE: the runner finished the plan first.
                # Only a plan that is terminal now counts as stopped.
                current = desk_store.get_plan(plan_id, network)
                if current is not None and str(current.get("status") or "") in ACTIVE_STATUSES:
                    pending_cleanup.end(pending, ok=False, error="plan could not be cancelled")
                    items.append(SwitchItem(DESK_PLAN, FAILED, label=plan_id, product=product,
                                            error="plan could not be cancelled"))
                    continue
            elif not running:
                # Its entry trigger may have fired between the read and the cancel.
                current = desk_store.get_plan(plan_id, network)
                if current is not None and current.get("started_at"):
                    running = True
                    pending, pids = _begin_desk_cleanup(telegram_id, network, plan_id, product, market)
            if running:
                sweep = bot_runtime.cancel_orders_on_products(telegram_id, network, pids)
                pending_cleanup.end(pending, ok=bool(sweep.get("success")), error=sweep.get("error"))
                if not sweep.get("success"):
                    items.append(SwitchItem(DESK_PLAN, FAILED, label=plan_id, product=product,
                                            error=str(sweep.get("error") or "orders not confirmed cancelled")[:240]))
                    continue
            items.append(SwitchItem(DESK_PLAN, STOPPED, label=plan_id, product=product,
                                    warning=POSITION_MAY_BE_OPEN if running else ""))
        except Exception as exc:  # noqa: BLE001 - per plan; the switch is refused
            pending_cleanup.end(pending, ok=False, error=str(exc))
            items.append(SwitchItem(DESK_PLAN, FAILED, label=plan_id, product=product, error=str(exc)[:240]))
    return items


def _kept_automation(telegram_id: int, network: str) -> list[SwitchItem]:
    """Report (never stop) the network-scoped automation that keeps working on the
    old network. Informational: a failed read here does not gate the flip."""
    items: list[SwitchItem] = []
    try:
        for m in copy_service.get_user_copies(telegram_id, network) or []:
            items.append(SwitchItem(COPY_MIRROR, KEPT, label=str(m.get("trader_label") or m.get("mirror_id") or "")))
    except Exception:  # policy: degrade-ok(informational listing; mirrors keep running either way)
        logger.debug("network switch: copy mirror listing failed user=%s", telegram_id, exc_info=True)
    try:
        for rule in stop_loss_service.list_active_stop_loss_rules(telegram_id, network) or []:
            items.append(SwitchItem(STOP_LOSS_RULE, KEPT, product=str(rule.get("product") or "").upper()))
    except Exception:  # policy: degrade-ok(informational listing; rules keep protecting either way)
        logger.debug("network switch: stop-loss rule listing failed user=%s", telegram_id, exc_info=True)
    try:
        # Reads the ACTIVE network's settings — still the old one before the flip.
        if (
            managed_agent_state.is_managed_agent_globally_enabled()
            and managed_agent_state.get_managed_agent_state(telegram_id).get("enabled")
        ):
            items.append(SwitchItem(MANAGED_AGENT, KEPT))
    except Exception:  # policy: degrade-ok(informational; a chat mode, nothing runs in the background)
        logger.debug("network switch: managed agent read failed user=%s", telegram_id, exc_info=True)
    return items
