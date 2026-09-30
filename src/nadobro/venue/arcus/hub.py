"""Per-network Arcus singletons (P2 part: clock, IP budget, REST client, catalog;
plus the per-subaccount pool governors).

P2 scope: the WebSocket pool, stream router, ledger/link writers and sync
arrive in P4a (``ws.py`` / ``order_store.py`` / ``ledger.py`` / ``sync.py`` do
not exist yet), so their slots are typed ``object | None`` and nothing is
imported for them. Nothing in the bot calls this module in P2 (unwired).

Rules:
- Objects are bound to the runtime event loop (A-10): :func:`services` must
  run on a running loop; sync callers bridge with
  ``asyncio.run_coroutine_threadsafe``. A call from a DIFFERENT live loop is a
  bug (RuntimeError); after the recorded loop has closed the objects are
  rebuilt (tests running several ``asyncio.run``).
- Flags: ``ARCUS_ENABLED`` is read in exactly one place in the package —
  :func:`enabled_networks` (which networks to start eagerly at boot). The
  master switch gates mainnet too (02 D20). :func:`services` itself never
  checks a flag: brakes must work with every flag off.
- Nothing here resumes or starts any trading: :func:`start` only syncs the
  clock and loads the catalog, and never raises.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Iterable, Sequence, cast

from src.nadobro.core.feature_flags import (
    arcus_catalog_max_age_s,
    arcus_enabled,
    arcus_ip_l0_reserve,
    arcus_mainnet_enabled,
    arcus_market_allowlist,
)
from src.nadobro.utils.venue_scope import (
    ARCUS_MAINNET_SCOPE,
    ARCUS_NETWORK_MAINNET,
    ARCUS_NETWORK_TESTNET,
    ARCUS_TESTNET_SCOPE,
    arcus_scope_for,
    parse_arcus_net,
)
from src.nadobro.venue.arcus.budget import IpBudget, PoolGovernor
from src.nadobro.venue.arcus.catalog import ArcusCatalog
from src.nadobro.venue.arcus.client import ArcusClient
from src.nadobro.venue.arcus.clock import ArcusClock
from src.nadobro.venue.arcus.types import ArcusAccountRef, ArcusNet, Lane

logger = logging.getLogger(__name__)


@dataclass
class ArcusServices:
    """Contract §4.13 fields; P4a narrows the ``object | None`` slots."""

    network: ArcusNet
    client: ArcusClient
    clock: ArcusClock
    ip_budget: IpBudget
    catalog: ArcusCatalog
    ws: object | None = None
    router: object | None = None
    ledger_writer: object | None = None
    link_writer: object | None = None
    sync: object | None = None


_SERVICES: dict[str, ArcusServices] = {}
_LOOPS: dict[str, asyncio.AbstractEventLoop] = {}
_GOVERNORS: dict[tuple[str, str, int], PoolGovernor] = {}


def _lane_floors() -> dict[Lane, int]:
    """``ARCUS_IP_L0_RESERVE`` = r: {L0: 0, L1: r, L2: r+100, L3: r+400}."""
    reserve = arcus_ip_l0_reserve()
    return {
        Lane.L0_BRAKE: 0,
        Lane.L1_ENGINE: reserve,
        Lane.L2_INTERACTIVE: reserve + 100,
        Lane.L3_BACKGROUND: reserve + 400,
    }


def _as_arcus_net(net: str) -> ArcusNet:
    """The token as the ``ArcusNet`` literal type. Sound: :func:`parse_arcus_net`
    returns exactly ``'testnet'`` or ``'mainnet'`` (else raises)."""
    return cast(ArcusNet, parse_arcus_net(net))


def services(network: str) -> ArcusServices:
    """The per-network singletons, built on first use ON the running loop.
    Creation is synchronous (no ``await``), so it is atomic on the loop."""
    net = parse_arcus_net(network)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        raise RuntimeError("arcus hub must be used on the runtime event loop") from None
    existing = _SERVICES.get(net)
    if existing is not None:
        recorded = _LOOPS.get(net)
        if recorded is loop:
            return existing
        if recorded is not None and not recorded.is_closed():
            raise RuntimeError("arcus hub is bound to another running event loop")
        # The recorded loop is closed: its connections are dead; rebuild.
        _SERVICES.pop(net, None)
        _LOOPS.pop(net, None)
    clock = ArcusClock(net)
    budget = IpBudget(net, lane_floors=_lane_floors())
    client = ArcusClient(net, clock=clock, ip_budget=budget)
    catalog = ArcusCatalog(net, allowlist=arcus_market_allowlist, max_age_s=arcus_catalog_max_age_s)
    built = ArcusServices(
        network=_as_arcus_net(net), client=client, clock=clock, ip_budget=budget, catalog=catalog
    )
    _SERVICES[net] = built
    _LOOPS[net] = loop
    return built


def pool_governor(ref: ArcusAccountRef) -> PoolGovernor:
    """Get-or-create the pool governor of one subaccount (pure state; not
    loop-bound)."""
    if not isinstance(ref, ArcusAccountRef):
        raise ValueError("an ArcusAccountRef is required")
    key = (ref.network, ref.address, ref.account_index)
    governor = _GOVERNORS.get(key)
    if governor is None:
        governor = PoolGovernor(ref)
        _GOVERNORS[key] = governor
    return governor


def enabled_networks(*, state_networks: Iterable[str] = ()) -> tuple[ArcusNet, ...]:
    """Networks ``main.py`` starts EAGERLY (P4a wires it): testnet when
    ``ARCUS_ENABLED`` or testnet has Arcus state; mainnet when
    (``ARCUS_ENABLED`` AND ``ARCUS_MAINNET_ENABLED``) or mainnet has state.
    ``state_networks`` are NETWORK tokens (a scope token raises ValueError).
    Testnet first."""
    if isinstance(state_networks, (str, bytes)):
        raise ValueError("state_networks must be an iterable of network tokens")
    scopes = {arcus_scope_for(n) for n in state_networks}
    master = arcus_enabled()
    out: list[ArcusNet] = []
    if master or ARCUS_TESTNET_SCOPE in scopes:
        out.append(_as_arcus_net(ARCUS_NETWORK_TESTNET))
    if (master and arcus_mainnet_enabled()) or ARCUS_MAINNET_SCOPE in scopes:
        out.append(_as_arcus_net(ARCUS_NETWORK_MAINNET))
    return tuple(out)


async def start(networks: Sequence[str]) -> None:
    """Warm each network: clock sync + catalog load. Failures are logged at
    WARNING and never raise (brakes must not depend on start succeeding).
    Unwired in P2."""
    for network in networks:
        try:
            svc = services(network)
            skew = await svc.clock.sync(svc.client, lane=Lane.L1_ENGINE)
            if skew is None:
                logger.warning("arcus %s start: clock sync failed (will retry on demand)", svc.network)
            loaded = await svc.catalog.refresh(svc.client, lane=Lane.L1_ENGINE)
            if not loaded:
                logger.warning(
                    "arcus %s start: catalog not loaded (%s)", svc.network, svc.catalog.last_error or "unknown"
                )
        except Exception as exc:
            logger.warning("arcus start failed for one network: %s", type(exc).__name__)


async def shutdown() -> None:
    """Close every client built on the CURRENT loop; clear the registries."""
    try:
        loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    for net, svc in list(_SERVICES.items()):
        if _LOOPS.get(net) is not loop:
            continue  # built on another (closed) loop: nothing awaitable to close
        try:
            await svc.client.aclose()
        except Exception as exc:
            logger.warning("arcus %s shutdown: client close failed: %s", net, type(exc).__name__)
    _SERVICES.clear()
    _LOOPS.clear()
    _GOVERNORS.clear()


def _reset_for_tests() -> None:
    """Drop every registry without awaiting (tests only)."""
    _SERVICES.clear()
    _LOOPS.clear()
    _GOVERNORS.clear()


__all__ = [
    "ArcusServices",
    "services",
    "pool_governor",
    "enabled_networks",
    "start",
    "shutdown",
]
