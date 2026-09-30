"""Venue + network-scope helpers: a stray Arcus value never becomes a Nado network.

Nadobro runs two venues side by side. Nado keeps its historical network tokens
(``testnet`` / ``mainnet``) everywhere; Arcus state is keyed by its own SCOPE
tokens (``arcus_testnet`` / ``arcus_mainnet`` — underscore, never ``:`` or
``-``, so controller ids keep their 3-part shape and ``f"trades_{network}"``
stays a valid identifier).

Before this module, more than thirty Nado sites coerced "whatever network string arrived"
into a Nado network with a local ternary, and the sites disagree on policy
(``str(v).lower() == "testnet"`` → else mainnet; ``v == "mainnet"`` → else
testnet; ``(v or "mainnet") == "mainnet"``; ...). Every one of them silently
folds ``arcus_mainnet`` into a real Nado network — Nado's TESTNET at some sites,
MAINNET at others.

Contract of :func:`coerce_nado_network` (one call per former ternary):

1. a non-Nado scope token (anything whose stripped, lowercased form starts with
   ``arcus``) ALWAYS raises :class:`VenueScopeError`;
2. every other input returns EXACTLY what that site returned before — each site
   passes its own legacy :class:`NetworkPolicy` (fallback network, case policy,
   strip, ``value or X`` pre-default, raw-vs-``str()`` compare);
3. inputs outside ``{None, '', 'testnet', 'mainnet'}`` keep that legacy result
   but log one WARNING per (site, value) so a production soak can prove no
   other values occur before any strict mode is considered. Nothing here
   raises for them.

Chokepoints that forward a network untouched (client constructors, factories,
catalog roots) use :func:`guard_nado_scope`: raise on a non-Nado scope,
otherwise return the value as-is.

Leaf module: stdlib imports only (``utils`` has no outgoing package edges), so
``models/`` can import it function-locally and everything else at module level.
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import TypeVar

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# --- Venues ---------------------------------------------------------------
VENUE_NADO = "nado"
VENUE_ARCUS = "arcus"
VENUES = (VENUE_NADO, VENUE_ARCUS)

# --- Nado networks (unchanged historical tokens) --------------------------
NADO_TESTNET = "testnet"
NADO_MAINNET = "mainnet"
NADO_NETWORKS = (NADO_TESTNET, NADO_MAINNET)

# --- Arcus scope tokens (keyed state only; NEVER a Nado network) ----------
ARCUS_SCOPE_PREFIX = "arcus"
ARCUS_TESTNET_SCOPE = "arcus_testnet"
ARCUS_MAINNET_SCOPE = "arcus_mainnet"
ARCUS_SCOPES = (ARCUS_TESTNET_SCOPE, ARCUS_MAINNET_SCOPE)

# Disjoint bot_state key prefixes for Arcus. Nado's LIKE enumerators are all
# anchored on prefixes starting with "s" ("strategy_bot:%", "stop_loss:%",
# "strategy_cleanup:%"), so these can never be enumerated by Nado code
# (pinned by tests/lint/test_venue_scope_hygiene.py).
ARCUS_STRATEGY_BOT_PREFIX = "arcus_strategy_bot:"
ARCUS_USER_SETTINGS_PREFIX = "arcus_user_settings:"
# Arcus API-key reminder state (P3b): ``f"{prefix}{uid}:{arcus_scope_for(net)}"``,
# e.g. ``arcus_key_notice:42:arcus_testnet`` (shared keys carry the scope token).
ARCUS_KEY_NOTICE_PREFIX = "arcus_key_notice:"

# --- Arcus network modes (``users.arcus_network_mode``) --------------------
# The same two words as Nado's networks but a DIFFERENT domain: always a plain
# str, never ``NetworkMode``, never passed where Nado code expects a network.
ARCUS_NETWORK_TESTNET = "testnet"
ARCUS_NETWORK_MAINNET = "mainnet"
ARCUS_NETWORK_MODES = (ARCUS_NETWORK_TESTNET, ARCUS_NETWORK_MAINNET)

_OTHER_NETWORK = {NADO_TESTNET: NADO_MAINNET, NADO_MAINNET: NADO_TESTNET}

# Bounded (site, value) dedupe for the non-canonical WARNING: a garbage stream
# must not grow memory without limit. Past the cap, warnings stop (the first
# 256 distinct pairs are plenty for a soak).
_WARN_CAP = 256
_warned: set[tuple[str, str]] = set()


class VenueScopeError(ValueError):
    """A non-Nado venue scope (e.g. ``arcus_mainnet``) reached a Nado site.

    Subclasses ``ValueError`` so the strict validators that already reject
    unknown networks (``_trades_table``, ``desk_store._table``, ...) and their
    callers keep behaving the same way.
    """


@dataclass(frozen=True)
class NetworkPolicy:
    """One site's legacy coercion rule, spelled out.

    ``fallback`` is where every non-matching input lands; the matched token is
    the OTHER Nado network. ``case_sensitive=False`` means the legacy code
    compared ``str(v).lower()``; ``strip`` adds ``.strip()``; ``empty`` mirrors
    a legacy ``(v or <empty>)`` pre-default (only matters when it differs from
    ``fallback``); ``raw_compare`` means the legacy code compared the value
    itself (``v == "testnet"``) or required ``isinstance(v, str)``, so only a
    ``str`` can ever match — ``str(v)`` is never taken.
    """

    fallback: str
    case_sensitive: bool
    strip: bool = False
    empty: str | None = None
    raw_compare: bool = False

    def __post_init__(self) -> None:
        if self.fallback not in NADO_NETWORKS:
            raise ValueError(f"NetworkPolicy.fallback must be a Nado network, got {self.fallback!r}")
        if self.empty is not None and self.empty not in NADO_NETWORKS:
            raise ValueError(f"NetworkPolicy.empty must be a Nado network, got {self.empty!r}")

    @property
    def match(self) -> str:
        return _OTHER_NETWORK[self.fallback]


# --- Legacy policies found in the codebase (see tests/utils/test_venue_scope.py
# for the golden table each one reproduces). --------------------------------

# ``"testnet" if str(v).lower() == "testnet" else "mainnet"`` — also covers
# ``str(v or "mainnet").lower()`` (the pre-default lands on the fallback anyway).
LOWER_ELSE_MAINNET = NetworkPolicy(fallback=NADO_MAINNET, case_sensitive=False)
# ``str(v or "mainnet").strip().lower() == "testnet"`` → testnet, else mainnet.
STRIP_LOWER_ELSE_MAINNET = NetworkPolicy(fallback=NADO_MAINNET, case_sensitive=False, strip=True)
# ``isinstance(v, str) and v.strip().lower() == "testnet"`` → testnet, else
# mainnet (config.get_nado_builder_routing_config: a non-str is mainnet).
STR_STRIP_LOWER_ELSE_MAINNET = NetworkPolicy(
    fallback=NADO_MAINNET, case_sensitive=False, strip=True, raw_compare=True
)
# ``v == "testnet"`` (raw, case-sensitive) → testnet, else mainnet.
EXACT_ELSE_MAINNET = NetworkPolicy(fallback=NADO_MAINNET, case_sensitive=True, raw_compare=True)
# ``v == "mainnet"`` (raw, case-sensitive) → mainnet, else testnet.
EXACT_ELSE_TESTNET = NetworkPolicy(fallback=NADO_TESTNET, case_sensitive=True, raw_compare=True)
# ``str(v) == "mainnet"`` → mainnet, else testnet.
STR_EXACT_ELSE_TESTNET = NetworkPolicy(fallback=NADO_TESTNET, case_sensitive=True)
# ``str(v).lower() == "mainnet"`` / ``str(v or "").lower() == "mainnet"`` →
# mainnet, else testnet.
LOWER_ELSE_TESTNET = NetworkPolicy(fallback=NADO_TESTNET, case_sensitive=False)
# UserRow: ``nm = v or "mainnet"; MAINNET if nm == "mainnet" else TESTNET`` —
# empty → mainnet, any other non-"mainnet" value (incl. "MAINNET") → testnet.
EMPTY_MAINNET_EXACT_ELSE_TESTNET = NetworkPolicy(
    fallback=NADO_TESTNET, case_sensitive=True, empty=NADO_MAINNET, raw_compare=True
)


def _scope_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, enum.Enum):
        value = value.value
    try:
        return str(value)
    except Exception:  # pragma: no cover - exotic __str__
        return None


def is_non_nado_scope(value: object) -> bool:
    """True for any value whose stripped, lowercased form starts with ``arcus``.

    Strip-before-check so ``' arcus_mainnet'`` cannot slip past the raise at a
    strip-folding site. Enum members are judged by their ``.value``.
    """
    if value is None:
        return False
    if type(value) is str and value in NADO_NETWORKS:  # hot-path fast exit
        return False
    text = _scope_text(value)
    if not text:
        return False
    return text.strip().lower().startswith(ARCUS_SCOPE_PREFIX)


def _shown(value: object) -> str:
    """Bounded repr for logs/errors; never raises (a helper must not add a
    failure mode the legacy expression did not have)."""
    try:
        return repr(value)[:40]
    except Exception:  # pragma: no cover - exotic __repr__
        return f"<{type(value).__name__}>"


def _raise_scope(value: object, site: str) -> None:
    raise VenueScopeError(
        f"non-Nado venue scope {_shown(value)} reached a Nado network site ({site})"
    )


def guard_nado_scope(value: _T, *, site: str) -> _T:
    """Chokepoint guard: raise :class:`VenueScopeError` if ``value`` is a
    non-Nado scope; otherwise return ``value`` completely untouched."""
    if is_non_nado_scope(value):
        _raise_scope(value, site)
    return value


def _warn_once(site: str, value: object, result: str) -> None:
    shown = _shown(value)
    key = (site, shown)
    if key in _warned or len(_warned) >= _WARN_CAP:
        return
    _warned.add(key)
    logger.warning(
        "venue_scope: non-canonical Nado network %s at %s -> %s (legacy result kept)",
        shown,
        site,
        result,
    )


def _legacy_result(value: object, policy: NetworkPolicy) -> str:
    if policy.empty is not None and not value:
        value = policy.empty
    text: str | None
    if policy.raw_compare:
        text = value if isinstance(value, str) else None
    else:
        text = str(value)
    if text is not None:
        if policy.strip:
            text = text.strip()
        if not policy.case_sensitive:
            text = text.lower()
    return policy.match if text == policy.match else policy.fallback


def coerce_nado_network(value: object, policy: NetworkPolicy, *, site: str) -> str:
    """Legacy-preserving Nado network coercion for one call site.

    Returns ``'testnet'`` or ``'mainnet'`` — exactly what the site's former
    ternary produced for ``value`` — except that a non-Nado scope token raises
    :class:`VenueScopeError`. Non-canonical inputs log one WARNING per
    (site, value).
    """
    if type(value) is str and value in NADO_NETWORKS:
        # Canonical fast path: every legacy policy maps a canonical token to itself.
        return value
    if is_non_nado_scope(value):
        _raise_scope(value, site)
    result = _legacy_result(value, policy)
    # None / "" are expected at many sites (defaults); anything else outside
    # {None, "", "testnet", "mainnet"} is logged once per (site, value).
    if not (value is None or (type(value) is str and value == "")):
        _warn_once(site, value, result)
    return result


def active_venue_from_db(raw: object) -> str:
    """``users.active_venue`` -> the venue a user SEES.

    ``'arcus'`` only on an EXACT match; ``None``, a missing column, ``'ARCUS'``,
    ``'arcus_mainnet'`` or anything else is ``'nado'``, so no stray value can
    route a user to Arcus. Pure: never logs, never raises (it runs inside
    ``UserRow.__init__`` on every update).
    """
    return VENUE_ARCUS if isinstance(raw, str) and raw == VENUE_ARCUS else VENUE_NADO


def arcus_network_from_db(raw: object) -> str:
    """``users.arcus_network_mode`` -> ``'mainnet'`` only on an EXACT match,
    otherwise ``'testnet'`` (the safe side). Pure: never logs, never raises."""
    if isinstance(raw, str) and raw == ARCUS_NETWORK_MAINNET:
        return ARCUS_NETWORK_MAINNET
    return ARCUS_NETWORK_TESTNET


_ARCUS_SCOPE_BY_NET = {
    ARCUS_NETWORK_TESTNET: ARCUS_TESTNET_SCOPE,
    ARCUS_NETWORK_MAINNET: ARCUS_MAINNET_SCOPE,
}
_ARCUS_NET_BY_SCOPE = {scope: net for net, scope in _ARCUS_SCOPE_BY_NET.items()}


def parse_arcus_net(value: object) -> str:
    """Exact Arcus network token: ``'testnet'`` or ``'mainnet'``.

    Only a plain ``str`` (``type(value) is str`` — no subclass, no bytes) that
    is EXACTLY one of :data:`ARCUS_NETWORK_MODES` is accepted: no strip, no case
    fold, no scope tokens. Anything else raises ``ValueError`` whose message
    never echoes the value. This is the one place Arcus code compares a network
    token (the hygiene lint flags every other compare). Pure: no logging.
    """
    if type(value) is str and value in ARCUS_NETWORK_MODES:
        return value
    raise ValueError("not an Arcus network token")


def arcus_scope_for(net: object) -> str:
    """``'testnet'`` -> ``'arcus_testnet'``, ``'mainnet'`` -> ``'arcus_mainnet'``.

    Validated through :func:`parse_arcus_net` and looked up by dict SUBSCRIPT
    (never a ``.get`` default), so anything else raises ``ValueError``.
    """
    return _ARCUS_SCOPE_BY_NET[parse_arcus_net(net)]


def arcus_net_from_scope(scope: object) -> str:
    """``'arcus_testnet'`` -> ``'testnet'``, ``'arcus_mainnet'`` -> ``'mainnet'``.

    Exact ``str`` tokens only (no strip, no case fold); anything else raises
    ``ValueError`` without echoing the value. Inverse of :func:`arcus_scope_for`.
    """
    if type(scope) is str and scope in _ARCUS_NET_BY_SCOPE:
        return _ARCUS_NET_BY_SCOPE[scope]
    raise ValueError("not an Arcus scope token")


def _reset_warnings_for_tests() -> None:
    _warned.clear()


__all__ = [
    "VENUE_NADO",
    "VENUE_ARCUS",
    "VENUES",
    "NADO_TESTNET",
    "NADO_MAINNET",
    "NADO_NETWORKS",
    "ARCUS_SCOPE_PREFIX",
    "ARCUS_TESTNET_SCOPE",
    "ARCUS_MAINNET_SCOPE",
    "ARCUS_SCOPES",
    "ARCUS_STRATEGY_BOT_PREFIX",
    "ARCUS_USER_SETTINGS_PREFIX",
    "ARCUS_KEY_NOTICE_PREFIX",
    "ARCUS_NETWORK_TESTNET",
    "ARCUS_NETWORK_MAINNET",
    "ARCUS_NETWORK_MODES",
    "VenueScopeError",
    "NetworkPolicy",
    "LOWER_ELSE_MAINNET",
    "STRIP_LOWER_ELSE_MAINNET",
    "STR_STRIP_LOWER_ELSE_MAINNET",
    "EXACT_ELSE_MAINNET",
    "EXACT_ELSE_TESTNET",
    "STR_EXACT_ELSE_TESTNET",
    "LOWER_ELSE_TESTNET",
    "EMPTY_MAINNET_EXACT_ELSE_TESTNET",
    "is_non_nado_scope",
    "guard_nado_scope",
    "coerce_nado_network",
    "active_venue_from_db",
    "arcus_network_from_db",
    "parse_arcus_net",
    "arcus_scope_for",
    "arcus_net_from_scope",
]
