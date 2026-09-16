"""Documented Nado rate-limit weights.

Source: https://docs.nado.xyz/developer-resources/api/rate-limits

Nado rate-limits by *weight*, not request count, across three budgets:

  * Core queries   — per **IP**     : 2400/min, 400/10s  (= 40 weight/s, burst 400)
  * Archive/index  — per **IP**     : 2400/min, 400/10s  (separate host, same shape)
  * Executes       — per **wallet** : 600/min, 100/10s   (= 10 weight/s, burst 100)

A flat "1 token per request" budget under-counts heavy calls — e.g. a
``market_prices`` query costs ``len(product_ids)``, ``subaccount_info`` with
txns costs 10-15, and a place-order *without* spot leverage costs 20. These
helpers map a call to its documented weight so the gateway budget charges the
right number of tokens.

Fractional weights are rounded **up** (ceil): over-charging slightly is always
safe; under-charging risks a 429.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Optional

# --- Core query weights (per IP), keyed by the ``type`` string we POST/GET to
# the gateway ``/query`` endpoint. Variable-weight queries are handled in
# ``query_weight`` before this table is consulted.
_QUERY_WEIGHTS: dict[str, int] = {
    "status": 1,
    "contracts": 1,
    "order": 1,
    "market_liquidity": 1,
    "orderbook": 1,
    "nonces": 2,
    "symbols": 2,
    "health_groups": 2,
    "insurance": 2,
    "fee_rates": 2,
    "assets": 2,
    "all_products": 5,
    "edge_all_products": 5,
    "max_order_size": 5,
    "max_withdrawable": 5,
    "linked_signer": 5,
    "isolated_positions": 10,
    "nlp_pool_info": 20,
    "max_nlp_mintable": 20,
    "max_nlp_burnable": 20,
    "nlp_locked_balances": 20,
}

# --- Archive/indexer query weights (separate per-IP budget on the archive host).
_ARCHIVE_WEIGHTS: dict[str, int] = {
    "funding_rate": 2,
    "oracle_price": 2,
    "perp_prices": 2,
    "subaccounts": 2,
    "linked_signers": 2,
    "linked_signer_rate_limit": 2,
    "isolated_subaccounts": 2,
    "ink_airdrop": 2,
    "liquidation_feed": 2,
    "sequencer_backlog": 1,
    "quote_price": 2,
    "interest_and_funding": 5,
    "nlp_funding_payments": 5,
    "nlp_interest_payments": 5,
    "product_snapshots": 10,
    "direct_deposit_address": 10,
    "fast_withdrawal_signature": 10,
}

# Conservative fallbacks for call types not explicitly tabulated. Default to the
# heaviest plausible weight in each lane so an unmapped call never *under*-charges.
_DEFAULT_QUERY_WEIGHT = 5
_DEFAULT_EXECUTE_WEIGHT = 20


def _ceil_int(value: float) -> int:
    try:
        return max(1, int(math.ceil(float(value))))
    except (TypeError, ValueError):
        return 1


def _count(value: Any) -> int:
    """Length of a sequence param, or 1 for a scalar/empty value (never 0 so a
    single-item query is never charged 0)."""
    if value is None:
        return 1
    if isinstance(value, (str, bytes)):
        return 1
    try:
        return max(1, len(value))
    except TypeError:
        return 1


def query_weight(query_type: str, params: Optional[Mapping[str, Any]] = None) -> int:
    """Documented IP weight for a query/indexer call.

    ``params`` may carry ``product_ids``, ``subaccounts``, ``limit``,
    ``with_txns`` / ``pre_state`` (subaccount_info) so variable-weight queries
    are sized correctly.
    """
    qt = (query_type or "").strip().lower()
    p: Mapping[str, Any] = params or {}

    # --- Variable-weight core queries ---------------------------------------
    if qt == "market_price":
        return 1
    if qt == "market_prices":
        return _ceil_int(_count(p.get("product_ids")))
    if qt in ("subaccount_info", "subaccount"):
        if p.get("pre_state"):
            return 15
        if p.get("with_txns") or p.get("txns"):
            return 10
        return 2
    if qt in ("orders", "subaccount_orders"):
        # IP weight = 2 * product_ids.length
        pids = p.get("product_ids")
        if pids is None and p.get("product_id") is not None:
            pids = [p.get("product_id")]
        return _ceil_int(2 * _count(pids))

    # --- Variable-weight archive/indexer queries ----------------------------
    limit = p.get("limit")
    subs = _count(p.get("subaccounts")) if p.get("subaccounts") is not None else 1
    if qt in ("candlesticks", "edge_candlesticks"):
        return _ceil_int(1 + (float(limit or 0) / 20.0))
    if qt in ("matches", "events", "interest_and_funding_events"):
        return _ceil_int(2 + (float(limit or 0) * subs / 10.0))
    if qt in ("archive_orders",):
        return _ceil_int(2 + (float(limit or 0) * subs / 20.0))
    if qt in ("subaccount_snapshots",):
        return _ceil_int(2 + (float(limit or 0) * subs / 10.0))
    if qt in ("positions",):
        # Archive position windows: 2 + limit / 20 (default limit 100 -> 7).
        return _ceil_int(2 + (float(limit or 100) / 20.0))
    if qt in ("portfolio", "portfolio_history", "portfolio_calendar"):
        return 5
    if qt in ("signatures",):
        return _ceil_int(2 + (_count(p.get("digests")) / 10.0))
    if qt in ("tx_hashes",):
        return _ceil_int(_count(p.get("idxs")) * 2)

    if qt in _QUERY_WEIGHTS:
        return _QUERY_WEIGHTS[qt]
    if qt in _ARCHIVE_WEIGHTS:
        return _ARCHIVE_WEIGHTS[qt]
    return _DEFAULT_QUERY_WEIGHT


def execute_weight(execute_type: str, params: Optional[Mapping[str, Any]] = None) -> int:
    """Documented wallet weight for an execute (order/cancel/withdraw/etc.).

    ``params`` may carry ``spot_leverage`` (default True), ``count`` (number of
    orders), ``digests`` and ``product_ids``.
    """
    et = (execute_type or "").strip().lower()
    p: Mapping[str, Any] = params or {}
    # Orders placed *without* spot leverage cost 20x (extra health checks).
    spot_leverage = p.get("spot_leverage", True)
    per_order = 1 if spot_leverage else 20

    if et in ("place_order", "place"):
        return per_order
    if et in ("place_orders", "place_batch"):
        return _ceil_int(per_order * max(1, int(p.get("count", 1) or 1)))
    if et in ("cancel_orders", "cancel"):
        digests = p.get("digests")
        return _ceil_int(_count(digests)) if digests else 1
    if et in ("cancel_product_orders",):
        pids = p.get("product_ids")
        return _ceil_int(5 * _count(pids)) if pids else 50
    if et in ("cancel_and_place",):
        digests = p.get("digests")
        cancel = _ceil_int(_count(digests)) if digests else 1
        return cancel + per_order
    if et in ("withdraw_collateral", "withdraw"):
        return 10 if spot_leverage else 20
    if et in ("liquidate_subaccount", "liquidate"):
        return 20
    if et in ("mint_nlp", "burn_nlp"):
        return 10
    if et in ("link_signer",):
        return 30
    if et in ("transfer_quote",):
        return 10
    return _DEFAULT_EXECUTE_WEIGHT
