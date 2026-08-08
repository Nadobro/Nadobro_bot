"""Strategy-domain callback handlers (extracted from callbacks.py, 2026-06).

Owns the strategy menu, per-strategy config sections, previews, and the
start/stop flow dispatched from ``strategy:*`` callback data. Shared UI
utilities (``_edit_loc``, ``_get_user_settings``) and cross-domain
navigation (``_handle_nav``) remain in ``callbacks.py``; this module may
import from callbacks at module level because callbacks only imports this
module lazily (inside its ``_handle_strategy`` shim), so there is no cycle.
"""
from __future__ import annotations

import logging

from src.nadobro.config import get_dn_pair, get_dn_products, get_perp_products, get_product_id, get_product_max_leverage, get_spot_product_id, list_volume_spot_product_names, normalize_volume_spot_symbol
from src.nadobro.handlers.commands import build_status_dashboard_parts
from src.nadobro.handlers.formatters import escape_md, fmt_price
from src.nadobro.handlers.keyboards import back_kb, dn_funding_rates_kb, strategy_action_kb, strategy_product_picker_kb
from src.nadobro.core.async_utils import run_blocking
from src.nadobro.strategy.bot_runtime import stop_user_bot, get_user_bot_status
from src.nadobro.users.onboarding_service import is_new_onboarding_complete
from src.nadobro.core.perf import timed_metric
from src.nadobro.users.settings_service import (
    get_strategy_settings,
    get_user_settings,
    update_user_settings,
)
from src.nadobro.strategy.strategy_pending_input import persist_strategy_pending_input
from src.nadobro.users.user_service import get_user_readonly_client, get_user_wallet_info, get_user, ensure_active_wallet_ready
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
import time

from src.nadobro.handlers.callbacks import (  # noqa: E402
    _edit_loc,
    _get_user_settings,
    _handle_nav,
)

logger = logging.getLogger(__name__)


# Turbo Volume preset: one tap writes the COHERENT setting trio (leverage,
# inventory allowance, session SL) plus fast cadence and tight/touch quoting.
# These knobs interact — leverage scales uPnL as % of margin linearly, so the
# session rail must widen with it, and the inventory/net-exposure caps must fit
# at least one full-size fill or the book goes one-sided after every fill.
# Values are written per strategy; the user can still tune any field after.
_TURBO_LEVERAGE_DEFAULT = 10.0
_TURBO_SESSION_SL_PCT = 10.0  # % of margin; at 10x = a 1% adverse price move


def _turbo_preset_settings(strategy_id: str, product_max_leverage: float) -> dict:
    """Settings the Turbo Volume preset writes for ``strategy_id``.

    Pure (unit-tested): leverage = min(10x, product max). Only ``mid`` gets
    touch quoting, the auto inventory cap (0 = deployed), a 100% net-exposure
    allowance (one full-size fill), and tp_pct=0 (no session profit-stop —
    mid's exits are purely rail-driven, so this only removes an auto-stop).
    grid/rgrid/dgrid keep their TP and level mechanics untouched — their
    position exits flow through barriers this preset must not disturb.
    """
    lev = max(1.0, min(_TURBO_LEVERAGE_DEFAULT, float(product_max_leverage or 1.0)))
    base = {
        "mm_leverage_override": int(lev),
        "interval_seconds": 5,
        "mm_preset": "turbo",
    }
    sid = str(strategy_id or "").lower()
    if sid == "mid":
        base.update({
            "spread_bp": 2.0,
            "mm_quote_mode": "touch",
            "inventory_soft_limit_usd": 0.0,   # 0 = auto (deployed size)
            "max_net_exposure_pct": 100.0,     # allow one full-size fill
            "sl_pct": _TURBO_SESSION_SL_PCT,
            "tp_pct": 0.0,
        })
    elif sid == "grid":
        base.update({"spread_bp": 3.0, "sl_pct": _TURBO_SESSION_SL_PCT})
    elif sid == "rgrid":
        base.update({
            "spread_bp": 3.0, "rgrid_spread_bp": 3.0,
            "rgrid_stop_loss_pct": _TURBO_SESSION_SL_PCT,
        })
    elif sid == "dgrid":
        base.update({
            "spread_bp": 3.0, "dgrid_spread_bp": 3.0,
            "rgrid_stop_loss_pct": _TURBO_SESSION_SL_PCT,
        })
    return base


_TINY_PRESET_KEYS = frozenset({
    "mm_leverage_override",
    "min_order_notional_usd",
    "mm_collateral_safety_factor",
})


def _replace_mm_preset(
    cfg: dict, strategy_id: str, preset_name: str, values: dict | None = None
) -> None:
    """Replace one MM preset atomically, removing the prior preset's keys.

    Presets share a settings dict and are user-switchable. Leaving the previous
    preset's owned keys behind creates hybrid modes (for example Tiny leverage
    with Turbo touch quotes and TP disabled), so transitions must be explicit.
    Unrelated/manual settings remain untouched.
    """
    current = str(cfg.get("mm_preset") or "").lower()
    if current == "turbo":
        for key in _turbo_preset_settings(strategy_id, 1.0):
            if key != "mm_preset":
                cfg.pop(key, None)
    elif current == "tiny":
        for key in _TINY_PRESET_KEYS:
            cfg.pop(key, None)
    cfg.pop("mm_preset", None)

    if preset_name == "standard":
        # Preserve Standard's historical behavior: it clears Tiny overrides
        # even for old rows that predate the mm_preset marker.
        for key in _TINY_PRESET_KEYS:
            cfg.pop(key, None)
    else:
        cfg.update(values or {})
    cfg["mm_preset"] = preset_name


def _strategy_available_products(strategy_id: str, network: str, vol_market: str | None = None) -> tuple[str, ...]:
    sid = str(strategy_id or "").lower().strip()
    if sid == "dn":
        return tuple(get_dn_products(network=network) or ("BTC", "ETH"))
    if sid == "vol" and str(vol_market or "perp").strip().lower() == "spot":
        names = list_volume_spot_product_names(network=network)
        return tuple(names) if names else tuple()
    return tuple(get_perp_products(network=network) or ("BTC", "ETH", "SOL"))


# F6 (Phase 5 audit): tiny per-process TTL cache for ``client.get_balance()``.
# A strategy preview render calls _build_strategy_preview_text and (for MM
# strategies) _append_mm_pretrade_breakdown back-to-back via run_blocking; both
# need the wallet balance and previously hit the SDK twice. Cache TTL is small
# (3s) so balance changes still propagate quickly. Race-safe under asyncio:
# Python dict ops are atomic at the GIL level, and a duplicate concurrent fetch
# is idempotent.
_BALANCE_CACHE_TTL_SECONDS = 3.0
# Moved from callbacks.py with _cached_user_balance: the extraction's AST
# mover collected Assign nodes but not AnnAssign, so this annotated dict
# was left behind while its consumer moved (prod NameError 2026-06-11).
_balance_cache: dict[int, tuple[float, dict]] = {}


def _cached_user_balance(telegram_id: int) -> dict:
    now = time.time()
    cached = _balance_cache.get(int(telegram_id))
    if cached and (now - cached[0]) < _BALANCE_CACHE_TTL_SECONDS:
        return cached[1]
    client = get_user_readonly_client(telegram_id)
    bal: dict = {}
    if client:
        try:
            bal = client.get_balance() or {}
        except Exception:
            bal = {}
    _balance_cache[int(telegram_id)] = (now, bal)
    return bal


def _wallet_collateral_usd_from_balance(bal: dict) -> float:
    if not isinstance(bal, dict) or not bal.get("exists"):
        return 0.0
    bals = bal.get("balances") or {}
    try:
        v = float(bals.get(0, 0) or 0.0)
    except (TypeError, ValueError):
        v = 0.0
    if v == 0.0:
        try:
            v = float(bals.get("0", 0) or 0.0)
        except (TypeError, ValueError):
            v = 0.0
    return v


def _vol_market_pref(context) -> str:
    # Volume strategy is spot-only as of 2026-05. The user_data slot and
    # ``strategy:volmarket`` callback are retained as no-ops only so cached
    # menus / deep links don't crash; we always return "spot".
    return "spot"


def _vol_fee_estimate(telegram_id: int, product: str, network: str):
    """Build the Volume Bot fee estimate the agreement card renders.

    Sync (catalog + settings reads) — call via run_blocking. Rates resolve
    venue-first with the production-measured fallback; see
    src/nadobro/quant/vol_fee_estimator.py for the provenance.
    """
    from src.nadobro.quant.vol_fee_estimator import estimate_vol_fees
    from src.nadobro.strategy.engine_runtime import resolve_vol_fee_rates

    _, conf = get_strategy_settings(telegram_id, "vol")
    # SAME resolver the engine uses (normalized symbol, same fallbacks), so the
    # rate on this card is provably the rate the run trades at.
    taker, builder = resolve_vol_fee_rates(product, network)
    margin = conf.get("session_margin_usd", conf.get("notional_usd", 100.0))
    # AUDIT-VOL-2026-07-31 F7: run_engine_cycle raises the per-cycle size to
    # min_closeable_entry_notional(venue_min) when the configured size would
    # leave an unclosable position — on a $100-min pair a $100 cycle actually
    # trades $115. Quote the size the engine will really use; a consent card
    # that understates the money at risk is not consent.
    try:
        from src.nadobro.quant.mm_quote_math import min_closeable_entry_notional
        from src.nadobro.venue.product_catalog import get_spot_min_notional_usd

        # SPOT accessor: get_product_min_quote_notional_usd resolves a PERP row
        # and returns None for every spot Volume pair (KBTC, WETH, …), so this
        # floor was dead exactly where it applies (self-review 2026-07-31).
        venue_min = get_spot_min_notional_usd(product, network=network) or 0.0
        floor = float(min_closeable_entry_notional(float(venue_min)) or 0.0)
        if floor > float(margin or 0):
            margin = floor
    except Exception:  # noqa: BLE001  # policy: degrade-ok(configured size is still quoted)
        pass
    return estimate_vol_fees(
        margin_usd=margin,
        target_volume_usd=conf.get("target_volume_usd", 0.0),
        taker_fee_rate=taker,
        builder_fee_rate=builder,
    )


def _vol_fee_quote_key(est, sl_pct: float) -> tuple:
    """Identity of the charges a fee card quoted.

    Compared at ack time so consent can only start a run on the numbers the
    user actually saw. Every field here is one the user is agreeing to.
    """
    return (
        str(est.margin_usd),
        str(est.target_volume_usd),
        str(est.total_rate),
        f"{float(sl_pct):.4f}",
    )


def _vol_fee_agreement_text(est, product: str, network: str, sl_pct: float = 0.0) -> str:
    """The charges the user is agreeing to. Every number here is quoted from
    the same estimate object that gets persisted on consent."""
    basis = (
        "venue rate" if est.rate_source == "venue"
        else "measured avg (venue does not publish this pair's rate)"
    )
    # AUDIT-VOL-2026-07-31 F6: the session stop is judged NET OF FEES, so if
    # the fees for the whole run exceed the stop budget the rail must trip
    # before the target can complete. Say so here rather than let the user
    # consent to a run that provably cannot finish.
    warning = ""
    fee_pct = float(est.fee_pct_of_margin)
    if sl_pct > 0:
        # Both legs are taker with no profit gate, so each round trip costs
        # fees (plus spread) by construction and the loss accumulates toward
        # the stop. Volume reachable before the stop trips ~= budget / rate.
        budget = float(est.margin_usd) * sl_pct / 100.0
        rate = float(est.total_rate)
        reachable = (budget / rate) if rate > 0 else 0.0
        stop_line = (
            f"\n\n🛑 *Session stop: {escape_md(f'{sl_pct:.0f}%')} of margin "
            f"\\({escape_md(f'${budget:,.2f}')}\\)*\n"
            f"Both legs take liquidity, so every round trip costs fees \\+ spread — "
            f"there is no profit target\\. The bot stops and closes everything once "
            f"cumulative loss net of fees hits "
            f"*{escape_md(f'-${budget:,.2f}')}*\\."
        )
        if reachable > 0 and reachable < float(est.target_volume_usd):
            stop_line += (
                f"\n⚠️ On fees alone that is about "
                f"*{escape_md(f'${reachable:,.0f}')}* of volume — **less than your "
                f"{escape_md(f'${float(est.target_volume_usd):,.0f}')} target**, and "
                f"spread will bring it lower\\. Raise the stop or lower the target "
                f"to finish the run\\."
            )
        warning = stop_line
    return (
        "💳 *Volume Bot — confirm charges*\n\n"
        f"Market: *{escape_md(str(product).upper())} SPOT* \\({escape_md(network.upper())}\\)\n"
        f"Margin per cycle: *{escape_md(f'${float(est.margin_usd):,.0f}')}*\n"
        f"Volume target: *{escape_md(f'${float(est.target_volume_usd):,.0f}')}*\n"
        f"Estimated round trips: *{escape_md(str(est.estimated_cycles))}*\n\n"
        "*Fees*\n"
        f"Taker fee: *{escape_md(f'{float(est.taker_fee_rate) * 10000:.2f} bp')}* "
        f"\\({escape_md(basis)}\\)\n"
        f"Builder fee: *{escape_md(f'{float(est.builder_fee_rate) * 10000:.2f} bp')}*\n"
        f"Total: *{escape_md(f'{float(est.total_rate_bp):.2f} bp')}* of traded volume\n\n"
        f"➡️ *Estimated total fee: {escape_md(f'${float(est.estimated_fee_usd):,.2f}')}* "
        f"\\({escape_md(f'{float(est.fee_pct_of_margin):.1f}%')} of your margin\\)\n\n"
        "⚠️ *Spot slippage:* both legs take liquidity at market\\. They fill at "
        "the best available price, which can be worse than the quoted touch — "
        "so your *actual total cost can exceed this estimate*\\. The estimate "
        "prices fees only, not the price you fill at\\."
        f"{warning}\n\n"
        "Starting the bot means you *agree to these charges*\\."
    )


def _vol_record_fee_ack(telegram_id: int, est) -> None:
    """Persist what the user agreed to (rate, estimate, size) for audit.

    Per start, not once-ever: the estimate depends on the margin, target and
    rates in force at that moment, so each run re-quotes and re-consents.
    """
    import time as _time

    def _mutate(s):
        cfg = s.setdefault("strategies", {}).setdefault("vol", {})
        cfg["vol_fee_ack_ts"] = _time.time()
        cfg["vol_fee_ack_margin_usd"] = float(est.margin_usd)
        cfg["vol_fee_ack_volume_usd"] = float(est.target_volume_usd)
        cfg["vol_fee_ack_rate_bp"] = float(est.total_rate_bp)
        cfg["vol_fee_ack_estimate_usd"] = float(est.estimated_fee_usd)

    update_user_settings(telegram_id, _mutate)


def _mid_bias_value(telegram_id: int) -> float:
    """Current Mid Mode directional bias as a float in [-1, 1] (sync DB read —
    call via run_blocking). Tolerates the legacy text values via the shared
    resolver so "long_bias"/"neutral" render the same ✅ as their numbers."""
    from src.nadobro.quant.mm_quote_math import _resolve_directional_bias_value

    _, conf = get_strategy_settings(telegram_id, "mid")
    try:
        return float(_resolve_directional_bias_value(conf.get("directional_bias")))
    except Exception:  # noqa: BLE001 - unreadable bias renders as neutral
        return 0.0


def _build_dn_funding_ranking(telegram_id: int) -> tuple[str, list[tuple[str, float | None, bool]]]:
    """Rank DN underlyings by their perp's current funding rate.

    Funding is a signed daily rate (positive ⇒ longs pay shorts ⇒ favorable for
    the DN short leg), settled hourly. Most-positive first = best short carry.
    Pulls every DN perp's rate in one batched indexer call
    (``client.get_perp_funding_rates``). Returns ``(markdown_text, rows)`` where
    each row is ``(product, daily_rate_or_None, entry_allowed)`` in display order.
    """
    user = get_user(telegram_id)
    network = user.network_mode.value if user else "mainnet"
    client = get_user_readonly_client(telegram_id)

    products = list(_strategy_available_products("dn", network))
    pid_by_product: dict[str, int] = {}
    entry_allowed_by_product: dict[str, bool] = {}
    for product in products:
        pair = get_dn_pair(product, network=network, client=client) or {}
        entry_allowed_by_product[product] = bool(pair.get("entry_allowed", True))
        pid = pair.get("perp_product_id")
        if pid is not None:
            try:
                pid_by_product[product] = int(pid)
            except (TypeError, ValueError):
                pass

    rates_by_pid: dict = {}
    if client and pid_by_product:
        try:
            rates_by_pid = client.get_perp_funding_rates(list(pid_by_product.values())) or {}
        except Exception:  # degrade-ok: funding screen still renders without rates
            rates_by_pid = {}

    rows: list[tuple[str, float | None, bool]] = []
    for product in products:
        rate: float | None = None
        pid = pid_by_product.get(product)
        if pid is not None:
            entry = rates_by_pid.get(pid) or {}
            raw = entry.get("funding_rate")
            if raw is not None:
                try:
                    rate = float(raw)
                except (TypeError, ValueError):
                    rate = None
        rows.append((product, rate, entry_allowed_by_product.get(product, True)))

    # Most-positive funding first; unknown rates sink to the bottom.
    rows.sort(key=lambda t: (t[1] is None, -(t[1] if t[1] is not None else 0.0)))

    lines = [
        "💹 *Delta Neutral — Funding Rates*",
        "Highest funding first \\(best carry for the DN *short* leg\\)\\.",
        "",
    ]
    have_rates = any(r is not None for _p, r, _e in rows)
    if not have_rates:
        lines.append("_Funding rates are unavailable right now — try Refresh in a moment\\._")
    else:
        for idx, (product, rate, entry_allowed) in enumerate(rows[:8], start=1):
            prod = escape_md(str(product).upper())
            if rate is None:
                detail = "n/a"
            else:
                bias = "short earns" if rate > 0 else ("short pays" if rate < 0 else "flat")
                detail = escape_md(f"{rate * 100:+.4f}%/day ({bias})")
            lock = "" if entry_allowed else "🔒 "
            lines.append(f"{idx}\\. {lock}*{prod}* — {detail}")
    lines.append("")
    lines.append("_Daily rate, settled hourly \\(about rate/24 per hour\\)\\. Tap a market to select it,_")
    lines.append("_then set your margin and start\\. Short hold/size can be fee\\-dominated\\._")
    return "\n".join(lines), rows


async def _handle_strategy(query, data, context, telegram_id):
    supported = ("grid", "rgrid", "dgrid", "mid", "dn", "vol", "bro")
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else ""
    strategy_id = parts[2] if len(parts) > 2 else ""

    if action == "volmarket" and len(parts) >= 4:
        # Volume is now spot-only — coerce any legacy ``perp`` selection to spot
        # and refresh the preview so cached menus reconverge.
        sid = parts[2]
        if sid != "vol":
            return
        context.user_data["vol_market:vol"] = "spot"
        user = get_user(telegram_id)
        network = user.network_mode.value if user else "mainnet"
        names = list_volume_spot_product_names(network=network)
        if names:
            context.user_data["strategy_pair:vol"] = names[0]
        await _handle_strategy(query, f"strategy:preview:{sid}", context, telegram_id)
        return

    if action == "preview":
        if strategy_id not in supported:
            return
        if strategy_id == "bro":
            # Legacy Alpha Agent dashboard remains reachable only when the operator
            # has explicitly re-enabled the legacy autoloop via env var.
            from src.nadobro.core.feature_flags import legacy_bro_autoloop_enabled
            if not legacy_bro_autoloop_enabled():
                # Alpha Agent has been retired; route back to the strategy hub.
                await _handle_nav(query, "nav:strategy_hub", telegram_id, context)
                return
            from src.nadobro.handlers.keyboards import bro_action_kb
            with timed_metric("cb.strategy.preview.bro"):
                preview_text = await run_blocking(_build_bro_preview_text, telegram_id)
            bot_status = get_user_bot_status(telegram_id) or {}
            is_running = bool(bot_status.get("running") and bot_status.get("strategy") == "bro")
            await _edit_loc(query,
                preview_text,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=bro_action_kb(is_running=is_running),
            )
            return
        user = get_user(telegram_id)
        network = user.network_mode.value if user else "mainnet"
        vm = _vol_market_pref(context) if strategy_id == "vol" else None
        available_pairs = _strategy_available_products(strategy_id, network, vm)
        if strategy_id == "vol" and vm == "spot" and not available_pairs:
            # Distinguish "catalog returned but nothing listed for {network}" vs
            # "catalog endpoint is failing/Cloudflare-blocked right now" so the
            # user gets accurate guidance for the active mode.
            try:
                from src.nadobro.venue.product_catalog import is_spot_catalog_dynamic

                catalog_live = is_spot_catalog_dynamic(network)
            except Exception:
                catalog_live = False
            network_label = "Testnet" if str(network).lower() == "testnet" else "Mainnet"
            if catalog_live:
                body = (
                    f"⚠️ *Volume Spot*\n\nNo spot books are listed for {network_label} right now\\. "
                    "Pick *Perp* or come back when Nado lists a spot pair on this network\\."
                )
            else:
                body = (
                    f"⚠️ *Volume Spot*\n\nSpot catalog temporarily unavailable on {network_label} "
                    "\\(upstream is being challenged by Cloudflare\\)\\. Please retry in ~30s\\."
                )
            await _edit_loc(
                query,
                body,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=strategy_action_kb("vol", "BTC", ["BTC"], vol_market=vm),
            )
            return
        selected_product = str(context.user_data.get(f"strategy_pair:{strategy_id}", available_pairs[0]) or available_pairs[0]).upper()
        if selected_product not in available_pairs:
            selected_product = available_pairs[0]
            context.user_data[f"strategy_pair:{strategy_id}"] = selected_product
        vkb_prev = _vol_market_pref(context) if strategy_id == "vol" else None
        with timed_metric("cb.strategy.preview"):
            preview_text = await run_blocking(
                _build_strategy_preview_text,
                telegram_id,
                strategy_id,
                selected_product,
                vkb_prev,
            )
        # Phase 3: append Tread-style breakdown for MM strategies. Built off the
        # mm_dashboard module so the pre-trade card and /mm_status share math.
        if strategy_id in ("grid", "rgrid", "dgrid", "mid"):
            preview_text = await run_blocking(
                _append_mm_pretrade_breakdown,
                telegram_id,
                strategy_id,
                selected_product,
                preview_text,
            )
        bot_status = get_user_bot_status(telegram_id) or {}
        is_running = bool(
            bot_status.get("running")
            and str(bot_status.get("strategy") or "").lower() == strategy_id
        )
        vkb = _vol_market_pref(context) if strategy_id == "vol" else "perp"
        mid_bias = (
            await run_blocking(_mid_bias_value, telegram_id)
            if strategy_id == "mid" else None
        )
        await _edit_loc(query,
            preview_text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=strategy_action_kb(
                strategy_id,
                selected_product,
                list(available_pairs),
                is_running=is_running,
                vol_market=vkb,
                mid_bias=mid_bias,
            ),
        )
    elif action == "custom" and len(parts) >= 4:
        strategy_id = parts[2]
        if strategy_id not in supported:
            return
        try:
            page = int(parts[3])
        except (TypeError, ValueError):
            page = 0
        user = get_user(telegram_id)
        network = user.network_mode.value if user else "mainnet"
        vm = _vol_market_pref(context) if strategy_id == "vol" else None
        available_pairs = _strategy_available_products(strategy_id, network, vm)
        if not available_pairs:
            await _edit_loc(query, "⚠️ No assets available for this mode\\.", parse_mode=ParseMode.MARKDOWN_V2)
            return
        selected_product = str(context.user_data.get(f"strategy_pair:{strategy_id}", available_pairs[0]) or available_pairs[0]).upper()
        if selected_product not in available_pairs:
            selected_product = available_pairs[0]
        await _edit_loc(
            query,
            f"🎯 *Select Asset for {escape_md(strategy_id.upper())}*",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=strategy_product_picker_kb(
                strategy_id=strategy_id,
                selected_product=selected_product,
                available_products=list(available_pairs),
                page=page,
            ),
        )
    elif action == "pair" and len(parts) >= 4:
        strategy_id = parts[2]
        selected_product = parts[3].upper()
        if strategy_id not in supported:
            return
        user = get_user(telegram_id)
        network = user.network_mode.value if user else "mainnet"
        vm = _vol_market_pref(context) if strategy_id == "vol" else None
        allowed_pairs = _strategy_available_products(strategy_id, network, vm)
        if selected_product not in allowed_pairs:
            return
        context.user_data[f"strategy_pair:{strategy_id}"] = selected_product
        vkb_pair = _vol_market_pref(context) if strategy_id == "vol" else None
        with timed_metric("cb.strategy.preview"):
            preview_text = await run_blocking(
                _build_strategy_preview_text,
                telegram_id,
                strategy_id,
                selected_product,
                vkb_pair,
            )
        if strategy_id in ("grid", "rgrid", "dgrid", "mid"):
            preview_text = await run_blocking(
                _append_mm_pretrade_breakdown,
                telegram_id,
                strategy_id,
                selected_product,
                preview_text,
            )
        bot_status = get_user_bot_status(telegram_id) or {}
        is_running = bool(
            bot_status.get("running")
            and str(bot_status.get("strategy") or "").lower() == strategy_id
        )
        vkb = _vol_market_pref(context) if strategy_id == "vol" else "perp"
        mid_bias = (
            await run_blocking(_mid_bias_value, telegram_id)
            if strategy_id == "mid" else None
        )
        await _edit_loc(query,
            preview_text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=strategy_action_kb(
                strategy_id,
                selected_product,
                list(allowed_pairs),
                is_running=is_running,
                vol_market=vkb,
                mid_bias=mid_bias,
            ),
        )
    elif action == "bias" and len(parts) >= 4:
        # BIAS-ON-CARD (2026-07-31): one-tap Long/Neutral/Short from the Mid
        # start card. Persists directional_bias and re-renders the SAME card
        # (the Advanced → Core path with the fine-grained ±/custom values is
        # unchanged). Mid-only: other strategies keep their set_text path.
        if strategy_id != "mid":
            return
        try:
            bias_value = float(parts[3])
        except (TypeError, ValueError):
            return
        bias_value = max(-1.0, min(1.0, bias_value))

        def _mutate_bias(s):
            strategies = s.setdefault("strategies", {})
            cfg = strategies.setdefault("mid", {})
            cfg["directional_bias"] = bias_value

        await run_blocking(update_user_settings, telegram_id, _mutate_bias)
        label = (
            "Long" if bias_value > 0 else "Short" if bias_value < 0 else "Neutral"
        )
        try:
            await query.answer(f"Bias: {label} ({bias_value:+.1f})")
        except Exception:  # noqa: BLE001 - toast is best-effort
            pass
        await _handle_strategy(query, f"strategy:preview:{strategy_id}", context, telegram_id)
    elif action == "funding":
        # DN-only: rank the corresponding-spot perps by funding so the user picks
        # the best short carry before sizing margin. Tapping a row routes through
        # the normal pair selection and lands back on the DN dashboard.
        if strategy_id != "dn":
            return
        with timed_metric("cb.strategy.funding.dn"):
            text, ranked = await run_blocking(_build_dn_funding_ranking, telegram_id)
        await _edit_loc(
            query,
            text,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=dn_funding_rates_kb(ranked),
        )
    elif action == "config":
        if strategy_id not in supported:
            return
        network, settings = get_user_settings(telegram_id)
        conf = _conf_for_card(settings, strategy_id, context, network)
        context.user_data.pop(f"strategy_config_section:{strategy_id}", None)
        await _edit_loc(query, 
            _strategy_config_menu_text(strategy_id, conf, network),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_strategy_config_menu_kb(strategy_id),
        )
    elif action == "config_section" and len(parts) >= 4:
        section = parts[3]
        if strategy_id not in supported:
            return
        valid_sections = {name for name, _label in _strategy_config_sections(strategy_id)}
        if section not in valid_sections:
            return
        context.user_data[f"strategy_config_section:{strategy_id}"] = section
        network, settings = get_user_settings(telegram_id)
        conf = _conf_for_card(settings, strategy_id, context, network)
        await _edit_loc(
            query,
            _strategy_config_section_text(strategy_id, conf, network, section),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_strategy_config_section_kb(strategy_id, section),
        )
    elif action == "preset" and len(parts) >= 4:
        # MM Tiny Budget / Standard preset.
        # Tiny Budget Preset auto-derives the leverage required to clear the venue
        # min_size floor for the selected pair and pins a tight safety factor so a
        # small wallet (e.g. $50) can quote on pairs that would otherwise reject.
        # Standard Preset clears those overrides and returns to per-asset MAX leverage
        # with the legacy safety cushion.
        strategy_id = parts[2]
        preset_name = parts[3]
        if strategy_id not in {"grid", "rgrid", "dgrid", "mid"} or preset_name not in {"tiny", "standard", "turbo"}:
            return
        network, settings = get_user_settings(telegram_id)
        cfg_now = settings.get("strategies", {}).get(strategy_id, {}) or {}
        # Selected product for the strategy was set during preview; default to BTC.
        selected_product = str(
            context.user_data.get(f"strategy_pair:{strategy_id}", "BTC") or "BTC"
        ).upper()
        if preset_name == "turbo":
            # Turbo Volume: coherent high-throughput quoting. Leverage capped
            # at the pair max; margin is whatever the user configured.
            try:
                lev_cap_t = float(get_product_max_leverage(selected_product, network=network))
            except Exception:  # noqa: BLE001 - unknown pair -> conservative 1x cap
                lev_cap_t = 1.0
            turbo_cfg = _turbo_preset_settings(strategy_id, lev_cap_t)

            def _mutate_turbo(s):
                strategies = s.setdefault("strategies", {})
                cfg = strategies.setdefault(strategy_id, {})
                _replace_mm_preset(cfg, strategy_id, "turbo", turbo_cfg)

            network, settings = update_user_settings(telegram_id, _mutate_turbo)
            conf = _conf_for_card(settings, strategy_id, context, network)
            section = "setup"
            context.user_data[f"strategy_config_section:{strategy_id}"] = section
            margin_t = float(conf.get("notional_usd", 100.0) or 0.0)
            lev_t = int(turbo_cfg["mm_leverage_override"])
            deployed_t = margin_t * lev_t
            mode_note = (
                "touch\\-joined quotes \\(fills at organic flow\\)" if strategy_id == "mid"
                else "tight 3bp quoting"
            )
            turbo_note = (
                f"*🚀 Turbo Volume preset applied for {escape_md(selected_product)}*\n"
                f"⚡ {lev_t}x leverage: \\${escape_md(f'{margin_t:,.0f}')} margin quotes "
                f"*\\${escape_md(f'{deployed_t:,.0f}')}* per side, {mode_note}, 5s cycles\\.\n"
                f"🛡 Session stop\\-loss set to *{escape_md(f'{_TURBO_SESSION_SL_PCT:.0f}%')} of margin* "
                f"\\(at {lev_t}x that is a \\~{escape_md(f'{_TURBO_SESSION_SL_PCT / max(lev_t, 1):.1f}%')} "
                f"adverse price move\\)\\.\n"
                f"⚠️ Volume mode trades per\\-fill edge for turnover — expect fees "
                f"\\(\\~\\$180\\+ per \\$1M net of maker rebates\\) and watch Cost/\\$1M in Performance\\."
            )
            body_t = _strategy_config_section_text(strategy_id, conf, network, section)
            await _edit_loc(
                query,
                f"{body_t}\n\n{turbo_note}",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=_strategy_config_section_kb(strategy_id, section),
            )
            return
        if preset_name == "standard":
            def _mutate_std(s):
                strategies = s.setdefault("strategies", {})
                cfg = strategies.setdefault(strategy_id, {})
                _replace_mm_preset(cfg, strategy_id, "standard")

            network, settings = update_user_settings(telegram_id, _mutate_std)
            conf = _conf_for_card(settings, strategy_id, context, network)
            section = "setup"
            context.user_data[f"strategy_config_section:{strategy_id}"] = section
            # Confirmation note so Standard is no longer a silent no-op: it clears
            # the Tiny override and returns to the leverage the run will resolve
            # (the pair max for grid/rgrid/dgrid, else the user's default) — read
            # through _mm_effective_leverage so the number shown is the engine's.
            margin_std = float(conf.get("notional_usd", 100.0) or 0.0)
            deployed_std = margin_std * _mm_effective_leverage(conf)
            std_note = (
                "*Standard preset applied*\n"
                f"✅ Cleared the Tiny override — back to default leverage "
                f"\\({escape_md(f'{_mm_effective_leverage(conf):.0f}x')}\\)\\. "
                f"Position \\= *{escape_md(f'${deployed_std:,.0f}')}* "
                f"\\(margin × leverage\\)\\. Pick a Lev button to override\\."
            )
            body_std = _strategy_config_section_text(strategy_id, conf, network, section)
            await _edit_loc(
                query,
                f"{body_std}\n\n{std_note}",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=_strategy_config_section_kb(strategy_id, section),
            )
            return

        # Tiny Budget — resolve venue minimum and required leverage.
        try:
            from src.nadobro.venue.product_catalog import (
                get_product_min_quote_notional_usd,
            )
            venue_min = get_product_min_quote_notional_usd(selected_product, network=network)
            lev_cap = float(get_product_max_leverage(selected_product, network=network))
        except Exception:
            venue_min = None
            lev_cap = 1.0
        try:
            collateral = float(cfg_now.get("notional_usd") or 0.0)
        except (TypeError, ValueError):
            collateral = 0.0
        if not venue_min or venue_min <= 0:
            await _edit_loc(
                query,
                "⚠️ *Tiny Budget Preset*\n\nVenue minimum could not be resolved for "
                f"`{escape_md(selected_product)}` from Nado catalog\\. Try again in a moment\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=_strategy_config_section_kb(strategy_id, "setup"),
            )
            return
        if collateral <= 0:
            await _edit_loc(
                query,
                "⚠️ *Tiny Budget Preset*\n\nSet your margin first \\(Custom Margin\\), "
                "then press Tiny Budget Preset to fit leverage to the venue floor\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=_strategy_config_section_kb(strategy_id, "setup"),
            )
            return
        import math as _math
        required_lev = max(1.0, _math.ceil(venue_min / collateral))
        # 1.1× cushion to absorb the 1.10 safety factor without overshooting cap.
        target_lev = min(_math.ceil(required_lev * 1.1), int(lev_cap))
        target_lev = max(int(required_lev), int(target_lev))
        target_lev = min(int(target_lev), int(lev_cap))

        def _mutate_tiny(s):
            strategies = s.setdefault("strategies", {})
            cfg = strategies.setdefault(strategy_id, {})
            _replace_mm_preset(
                cfg,
                strategy_id,
                "tiny",
                {
                    "mm_leverage_override": int(target_lev),
                    "min_order_notional_usd": float(venue_min),
                    "mm_collateral_safety_factor": 1.10,
                },
            )

        network, settings = update_user_settings(telegram_id, _mutate_tiny)
        conf = _conf_for_card(settings, strategy_id, context, network)
        section = "setup"
        context.user_data[f"strategy_config_section:{strategy_id}"] = section
        notional_after = collateral * float(target_lev)
        if notional_after >= venue_min:
            preflight_note = (
                f"✅ ${collateral:.0f} × {int(target_lev)}× \\= ${notional_after:.0f} notional "
                f"≥ pair minimum ${venue_min:.0f} \\(USDT0\\)\\. Cleared to quote\\."
            )
        else:
            preflight_note = (
                f"⚠️ ${collateral:.0f} × {int(target_lev)}× \\= ${notional_after:.0f} notional "
                f"< pair minimum ${venue_min:.0f}\\. Add collateral or pick a smaller-min pair\\."
            )
        body = _strategy_config_section_text(strategy_id, conf, network, section)
        await _edit_loc(
            query,
            f"{body}\n\n*Tiny Budget Preset applied for {escape_md(selected_product)}*\n{preflight_note}",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_strategy_config_section_kb(strategy_id, section),
        )
        return
    elif action == "set" and len(parts) >= 5:
        strategy_id = parts[2]
        field = parts[3]
        raw_value = parts[4]
        if strategy_id not in supported:
            return
        if strategy_id == "vol" and field not in {"tp_pct", "sl_pct", "session_margin_usd", "target_volume_usd"}:
            return
        allowed_numeric_fields = {
            "notional_usd", "spread_bp", "interval_seconds", "tp_pct", "sl_pct",
            "levels", "min_range_pct", "max_range_pct", "threshold_bp", "close_offset_bp",
            "cycle_notional_usd", "session_notional_cap_usd", "inventory_soft_limit_usd",
            "quote_ttl_seconds", "min_spread_bp", "max_spread_bp", "vol_sensitivity",
            "grid_reset_threshold_pct", "grid_reset_timeout_seconds",
            "rgrid_spread_bp", "rgrid_stop_loss_pct", "rgrid_take_profit_pct",
            "rgrid_reset_threshold_pct", "rgrid_reset_timeout_seconds", "rgrid_discretion",
            "dgrid_trend_on_variance_ratio", "dgrid_range_on_variance_ratio",
            "dgrid_spread_bp", "dgrid_min_spread_bp", "dgrid_max_spread_bp",
            "dgrid_short_window_points", "dgrid_long_window_points",
            "auto_close_on_maintenance", "is_long_bias",
            # GRID quoting mode (1 = fill-anchored maker with no-cross + soft reset,
            # 0 = classic static ladder). Ignored for rgrid: Reverse Grid has one
            # engine, and letting this route it elsewhere is what made R-Grid
            # sessions announce D-Grid phase flips.
            "fill_anchored",
            # Mid Mode accepts directional_bias as a continuous float in [-1, +1].
            "directional_bias",
            # Volume Bot (spot, 2026-05) accepts session margin + target volume.
            "session_margin_usd", "target_volume_usd",
            # Delta Neutral (engine v2) — the controller reads these directly:
            # per-leg size, hold duration, cycle count + gap, hedge drift gate.
            "fixed_margin_usd", "dn_hold_seconds", "dn_cycles",
            "dn_cycle_gap_seconds", "dn_max_drift_pct", "dn_hedge_ratio",
            # MM/grid leverage: turns margin into deployed notional (margin x lev).
            "mm_leverage_override",
            # MM run duration in minutes (participation/TWAP). Hard cap for
            # Mid/D-Grid; soft target for Grid/R-Grid.
            "mm_duration_minutes",
            # TWAP fast-move pause threshold (bp/cycle); 0 = off.
            "twap_pause_move_bp",
        }
        if field not in allowed_numeric_fields:
            return
        # Mid Mode: directional_bias is only valid as a number for the mid strategy
        # (other strategies still use the set_text path with neutral/long/short).
        if field == "directional_bias" and strategy_id != "mid":
            return
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            return
        # Mid Mode spread is capped at 0: a post-only LIMIT_MAKER can't cross the
        # book, so a negative spread is impossible to honor (it was previously
        # accepted but silently floored). 0 = quote as tight as the fee floor
        # allows (the speed end). Other strategies stay positive-only.
        if field == "spread_bp" and strategy_id == "mid":
            spread_lo, spread_hi = 0.0, 100.0
        else:
            spread_lo, spread_hi = 0.1, 200.0
        limits = {
            "notional_usd": (1, 1000000),
            "spread_bp": (spread_lo, spread_hi),
            "directional_bias": (-1.0, 1.0),
            # Floor 5 so Turbo's 5s cadence can be re-entered manually
            # (effective_interval_seconds hard-floors everything at 3s anyway).
            "interval_seconds": (5, 3600),
            "tp_pct": (0.05, 100),
            "sl_pct": (0.05, 100),
            # Volume Bot per-cycle size band (docs/volume_bot_taker_v4.md).
            "session_margin_usd": (100, 500),
            "target_volume_usd": (100, 100000000),
            "levels": (1, 20),
            "min_range_pct": (0.1, 20),
            "max_range_pct": (0.1, 40),
            "threshold_bp": (0, 500),
            "close_offset_bp": (1, 1000),
            "cycle_notional_usd": (1, 1000000),
            "session_notional_cap_usd": (0, 10000000),
            "inventory_soft_limit_usd": (1, 1000000),
            "quote_ttl_seconds": (5, 86400),
            "min_spread_bp": (0.1, 200),
            "max_spread_bp": (0.1, 500),
            "vol_sensitivity": (0.0, 1.0),
            # Soft-reset drift band. Grid re-anchors on SMALL drifts (it is a
            # spread-capture book that must stay near the last fill), so 1% is
            # the ceiling; RGrid follows trends and needs a much wider band.
            "grid_reset_threshold_pct": (0.05, 1.0),
            "grid_reset_timeout_seconds": (15, 86400),
            "rgrid_spread_bp": (0.1, 200),
            "rgrid_stop_loss_pct": (0.05, 100),
            "rgrid_take_profit_pct": (0.05, 200),
            "rgrid_reset_threshold_pct": (0.1, 10.0),
            "rgrid_reset_timeout_seconds": (15, 86400),
            "rgrid_discretion": (0.01, 0.5),
            "dgrid_trend_on_variance_ratio": (1.0, 5.0),
            "dgrid_range_on_variance_ratio": (0.1, 2.0),
            "dgrid_spread_bp": (0.1, 200.0),
            "dgrid_min_spread_bp": (0.0, 50.0),
            "dgrid_max_spread_bp": (1.0, 200.0),
            "dgrid_short_window_points": (2, 50),
            "dgrid_long_window_points": (4, 200),
            "auto_close_on_maintenance": (0, 1),
            "is_long_bias": (0, 1),
            "fill_anchored": (0, 1),
            # Delta Neutral (engine v2).
            "fixed_margin_usd": (1, 1000000),
            "dn_hold_seconds": (60, 86400),        # 1 minute .. 24 hours
            "dn_cycles": (1, 100),
            "dn_cycle_gap_seconds": (0, 86400),
            "dn_max_drift_pct": (0.5, 50),
            "dn_hedge_ratio": (0.1, 5.0),
            "mm_leverage_override": (1, 50),
            "mm_duration_minutes": (1, 14400),   # 1 min .. 240h (POV upper bound)
            "twap_pause_move_bp": (0, 5000),     # 0 = off .. 50% per-cycle move
        }
        # A whitelisted field with no limits entry is a wiring bug — reject
        # loudly in logs instead of crashing the callback with a KeyError.
        _bounds = limits.get(field)
        if _bounds is None:
            logger.error("strategy set: field %r whitelisted but has no limits entry", field)
            return
        lo, hi = _bounds
        if value < lo or value > hi:
            return
        int_fields = {
            "interval_seconds", "levels", "max_open_orders",
            "auto_close_on_maintenance", "is_long_bias", "rgrid_reset_timeout_seconds",
            "dn_hold_seconds", "dn_cycles", "dn_cycle_gap_seconds", "mm_leverage_override",
            "fill_anchored", "mm_duration_minutes", "twap_pause_move_bp",
        }

        def _mutate(s):
            strategies = s.setdefault("strategies", {})
            cfg = strategies.setdefault(strategy_id, {})
            if field in int_fields:
                cfg[field] = int(value)
            else:
                cfg[field] = value
            if field == "notional_usd":
                from src.nadobro.users.settings_service import sync_cycle_notional_with_margin

                sync_cycle_notional_with_margin(strategies, strategy_id)

        network, settings = update_user_settings(telegram_id, _mutate)
        conf = _conf_for_card(settings, strategy_id, context, network)
        section = context.user_data.get(f"strategy_config_section:{strategy_id}") or _strategy_section_for_field(strategy_id, field)
        context.user_data[f"strategy_config_section:{strategy_id}"] = section
        await _edit_loc(query, 
            _strategy_config_section_text(strategy_id, conf, network, section),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_strategy_config_section_kb(strategy_id, section),
        )
    elif action == "set_text" and len(parts) >= 5:
        strategy_id = parts[2]
        field = parts[3]
        raw_value = parts[4]
        if strategy_id not in supported:
            return
        allowed_text = {
            "reference_mode": {"mid", "ema_fast", "ema_slow"},
            "directional_bias": {"neutral", "long_bias", "short_bias"},
            "vol_direction": {"long", "short"},
            "funding_entry_mode": {"wait", "enter_anyway"},
            # Phase 2: Tread Fi POV / participation preset.
            "participation_preset": {"aggressive", "normal", "passive", "off"},
            # Volume Bot execution algo (maker TWAP default / chase / taker).
            "vol_execution_algo": {"twap", "chase", "taker"},
            # Ladder size curve (Mid + fill-anchored Grid): how the side's
            # deployment is distributed across levels. flat = equal size;
            # linear/geometric put progressively more size on the deeper
            # levels, i.e. scale harder into an adverse move.
            "size_curve": {"flat", "linear", "geometric"},
        }
        allowed_vals = allowed_text.get(field, set())
        if raw_value not in allowed_vals:
            return
        # participation_preset is only meaningful for the MM family.
        if field == "participation_preset" and strategy_id not in ("grid", "rgrid", "dgrid", "mid"):
            return
        # size_curve only drives the laddered quoters (Mid + fill-anchored Grid).
        if field == "size_curve" and strategy_id not in ("grid", "mid"):
            return

        def _mutate(s):
            strategies = s.setdefault("strategies", {})
            cfg = strategies.setdefault(strategy_id, {})
            if field == "participation_preset" and raw_value == "off":
                cfg.pop("participation_preset", None)
            else:
                cfg[field] = raw_value

        network, settings = update_user_settings(telegram_id, _mutate)
        conf = _conf_for_card(settings, strategy_id, context, network)
        section = context.user_data.get(f"strategy_config_section:{strategy_id}") or _strategy_section_for_field(strategy_id, field)
        context.user_data[f"strategy_config_section:{strategy_id}"] = section
        await _edit_loc(query, 
            _strategy_config_section_text(strategy_id, conf, network, section),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_strategy_config_section_kb(strategy_id, section),
        )
    elif action == "input" and len(parts) >= 4:
        strategy_id = parts[2]
        field = parts[3]
        if strategy_id not in supported:
            return
        allowed_inputs = (
            "notional_usd", "spread_bp", "interval_seconds", "tp_pct", "sl_pct",
            "levels", "min_range_pct", "max_range_pct", "threshold_bp", "close_offset_bp",
            "cycle_notional_usd", "session_notional_cap_usd", "inventory_soft_limit_usd",
            "quote_ttl_seconds", "min_spread_bp", "max_spread_bp", "vol_sensitivity",
            "rgrid_spread_bp", "rgrid_stop_loss_pct", "rgrid_take_profit_pct",
            "rgrid_reset_threshold_pct", "rgrid_reset_timeout_seconds", "rgrid_discretion",
            "dgrid_trend_on_variance_ratio", "dgrid_range_on_variance_ratio",
            "dgrid_spread_bp", "dgrid_min_spread_bp", "dgrid_max_spread_bp",
            "dgrid_short_window_points", "dgrid_long_window_points",
            "grid_reset_threshold_pct", "grid_reset_timeout_seconds",
            "directional_bias", "mm_leverage_override", "mm_duration_minutes",
            "twap_pause_move_bp",
            # Volume Bot (spot) custom inputs.
            "session_margin_usd", "target_volume_usd",
            # Delta Neutral (engine v2) custom inputs.
            "fixed_margin_usd", "dn_hold_seconds", "dn_cycles",
        )
        if strategy_id == "vol" and field not in {"tp_pct", "sl_pct", "session_margin_usd", "target_volume_usd"}:
            return
        if field not in allowed_inputs:
            return
        section = context.user_data.get(f"strategy_config_section:{strategy_id}") or _strategy_section_for_field(strategy_id, field)
        context.user_data[f"strategy_config_section:{strategy_id}"] = section
        context.user_data["pending_strategy_input"] = {
            "strategy": strategy_id,
            "field": field,
            "section": section,
        }
        await run_blocking(
            persist_strategy_pending_input,
            int(telegram_id),
            {"strategy": strategy_id, "field": field, "section": section},
        )
        help_text = {
            "notional_usd": "Enter margin in USD \\(example: `150`\\)",
            "spread_bp": "Enter spread in bps \\(example: `6`\\)",
            "interval_seconds": "Enter loop interval seconds \\(example: `45`\\)",
            "tp_pct": "Enter take profit % \\(example: `1\\.2`\\)",
            "sl_pct": "Enter stop loss % \\(example: `0\\.7`\\)",
            "levels": "Enter grid levels \\(example: `4`\\)",
            "min_range_pct": "Enter min range % \\(example: `1\\.0`\\)",
            "max_range_pct": "Enter max range % \\(example: `2\\.0`\\)",
            "threshold_bp": "Enter threshold in bps \\(example: `0` to disable, or `12`\\)",
            "close_offset_bp": "Enter close offset in bps \\(example: `25`\\)",
            "cycle_notional_usd": "Enter per\\-cycle budget in USD \\(usually same as margin\\)",
            "session_notional_cap_usd": "Enter optional session cap in USD \\(example: `5000`, or `0` to disable\\)",
            "inventory_soft_limit_usd": "Enter inventory soft limit in USD \\(example: `45`\\)",
            "quote_ttl_seconds": "Enter quote TTL seconds \\(example: `90`\\)",
            "min_spread_bp": "Enter minimum spread in bps \\(example: `2`\\)",
            "max_spread_bp": "Enter maximum spread in bps \\(example: `20`\\)",
            "vol_sensitivity": "Enter volatility sensitivity \\(example: `0\\.02`\\)",
            "grid_reset_threshold_pct": "Enter GRID reset threshold % \\(example: `0\\.8`\\)",
            "grid_reset_timeout_seconds": "Enter GRID reset timeout seconds \\(example: `120`\\)",
            "rgrid_spread_bp": "Enter RGRID spread in bps \\(example: `10`\\)",
            "rgrid_stop_loss_pct": "Enter RGRID PnL stop loss % of margin \\(example: `0\\.8`\\)",
            "rgrid_take_profit_pct": "Enter RGRID PnL take profit % of margin \\(example: `1\\.2`\\)",
            "rgrid_reset_threshold_pct": "Enter RGRID reset threshold % \\(example: `1\\.0`\\)",
            "rgrid_reset_timeout_seconds": "Enter RGRID reset timeout seconds \\(example: `120`\\)",
            "rgrid_discretion": "Enter RGRID discretion \\(example: `0\\.06`\\)",
            "dgrid_trend_on_variance_ratio": "Enter DGRID trend switch variance ratio \\(example: `1\\.25`\\)",
            "dgrid_range_on_variance_ratio": "Enter DGRID range switch variance ratio \\(example: `1\\.15`\\)",
            "dgrid_spread_bp": "Enter DGRID spread in bps \\(example: `8`\\)",
            "dgrid_min_spread_bp": "Enter DGRID minimum spread in bps \\(example: `2`\\)",
            "dgrid_max_spread_bp": "Enter DGRID maximum spread in bps \\(example: `50`\\)",
            "dgrid_short_window_points": "Enter DGRID short volatility window points \\(example: `4`\\)",
            "dgrid_long_window_points": "Enter DGRID long volatility window points \\(example: `12`\\)",
            "session_margin_usd": "Enter session margin in USD \\(per\\-cycle notional, example: `500`\\)",
            "target_volume_usd": "Enter target cumulative volume in USD \\(example: `25000`\\)",
            "mm_leverage_override": "Enter leverage \\(1 – 50; position size \\= margin × leverage, example: `5`\\)",
            "mm_duration_minutes": "Enter run duration in minutes \\(hard cap for Mid/D\\-Grid; a target for Grid/R\\-Grid, example: `60`\\)",
            "twap_pause_move_bp": "Enter fast\\-move pause threshold in bp per cycle \\(0 \\= off; pauses re\\-quoting when price jumps more, example: `200` \\= 2%\\)",
            "fixed_margin_usd": "Enter per\\-leg size in USD \\(example: `100`\\)",
            "dn_hold_seconds": "Enter *minimum* hold in seconds \\(60 – 86400; example: `3600` for 1h\\)\\. After this, the hedge stays open while funding is favorable and closes on a funding flip\\.",
            "dn_cycles": "Enter how many open→hold→close cycles to run \\(example: `3`\\)",
        }
        await _edit_loc(query, 
            f"✏️ *Custom {escape_md(field)}*\n\n"
            f"{help_text.get(field, 'Enter value')}\n\n"
            "Your next message will be used as this value\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=back_kb(f"strategy:config_section:{strategy_id}:{section}"),
        )
    elif action == "activate":
        context.user_data["active_setup"] = strategy_id
        await _edit_loc(query, 
            f"✅ Active setup is now *{escape_md(strategy_id.upper())}*\\.\n\n"
            "Next: open Buy/Long or Sell/Short and execute with preview\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    elif action in ("start", "startok") and len(parts) >= 4:
        strategy_id = parts[2]
        product = str(parts[3] or "").upper()
        # VOL-FEE-AGREEMENT (2026-07-31): "startok" is the same start path,
        # re-entered after the user accepted the fee estimate. Sharing this
        # branch means the agreement gate sits BEHIND every existing preflight
        # (onboarding, wallet, collateral) instead of duplicating it.
        fee_agreed = action == "startok"
        start_direction = "long"
        if strategy_id == "vol" and len(parts) >= 5:
            start_direction = "short" if str(parts[4]).lower() == "short" else "long"
        if strategy_id not in supported:
            return
        user = get_user(telegram_id)
        network = user.network_mode.value if user else "mainnet"
        vm = _vol_market_pref(context) if strategy_id == "vol" else None
        available_pairs = _strategy_available_products(strategy_id, network, vm)
        allowed_pairs = set(available_pairs)
        if product not in allowed_pairs:
            vkb_err = _vol_market_pref(context) if strategy_id == "vol" else "perp"
            await _edit_loc(
                query,
                f"⚠️ {escape_md(product)} is not currently available on {escape_md(network)}\\.\nPlease pick another asset\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=strategy_action_kb(
                    strategy_id,
                    available_pairs[0],
                    list(available_pairs),
                    vol_market=vkb_err,
                ),
            )
            return
        if not is_new_onboarding_complete(telegram_id):
            await _edit_loc(query, 
                "⚠️ Complete setup first (language + accept terms).",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("▶ Complete setup", callback_data="onboarding:resume")],
                    [InlineKeyboardButton("Exit", callback_data="nav:main")],
                ]),
            )
            return
        wallet_ready, wallet_msg = ensure_active_wallet_ready(telegram_id)
        if not wallet_ready:
            await _edit_loc(query, 
                f"⚠️ {escape_md(wallet_msg)}",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=back_kb(),
            )
            return
        settings = _get_user_settings(telegram_id, context)
        from src.nadobro.handlers.messages import execute_action_directly
        strategy_leverage = 1 if strategy_id == "vol" else settings.get("default_leverage", 3)
        if strategy_id == "dn":
            strategy_leverage = max(1, min(float(strategy_leverage), 5))
        if strategy_id in ("grid", "rgrid", "dgrid"):
            try:
                from src.nadobro.config import get_product_max_leverage as _gpml
                strategy_leverage = float(_gpml(product, network=network))
            except Exception:
                strategy_leverage = float(settings.get("default_leverage", 3))
        strategy_conf = (settings.get("strategies", {}) or {}).get(strategy_id, {}) or {}
        wallet_usdt_pf = None
        ro = get_user_readonly_client(telegram_id)
        if ro:
            try:
                b = ro.get_balance() or {}
                if b.get("exists"):
                    wallet_usdt_pf = float((b.get("balances") or {}).get(0, 0) or 0.0)
                    if wallet_usdt_pf == 0:
                        wallet_usdt_pf = float((b.get("balances") or {}).get("0", 0) or 0.0)
            except Exception:
                pass
        mm_budget_ok, mm_collateral_budget, mm_required_min_collateral, mm_min_order_notional, mm_max_quotes_est, mm_margin_per_quote_est = _mm_cycle_budget_preflight(
            strategy_id, strategy_conf, float(strategy_leverage or 1.0), wallet_usdt=wallet_usdt_pf
        )
        if not mm_budget_ok:
            vkb_block = _vol_market_pref(context) if strategy_id == "vol" else "perp"
            await _edit_loc(
                query,
                "⚠️ *Cannot start strategy*\n\n"
                "Collateral is below the estimated minimum for one venue\\-sized resting quote\\.\n"
                f"Each quote still carries roughly *{escape_md(f'${mm_min_order_notional:,.2f}')}* notional \\(exchange floor\\)\\.\n"
                f"Configured collateral cap: *{escape_md(f'${mm_collateral_budget:,.2f}')}*\n"
                f"Estimated margin per quote \\(~{escape_md(f'{strategy_leverage:.0f}x')}\\): "
                f"*{escape_md(f'${mm_margin_per_quote_est:,.2f}')}* \\(incl\\. safety buffer\\)\n"
                f"Need at least: *{escape_md(f'${mm_required_min_collateral:,.2f}')}*\n\n"
                "Deposit USDT, raise configured margin, or pick a product with a lower minimum order size\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=strategy_action_kb(
                    strategy_id,
                    product,
                    list(available_pairs),
                    vol_market=vkb_block,
                ),
            )
            return
        vol_m = _vol_market_pref(context) if strategy_id == "vol" else "perp"
        if strategy_id == "vol" and vol_m == "spot":
            start_direction = "long"
        # Charges gate: the Volume Bot trades taker on both legs, so fees are
        # a real, quantifiable cost the user must see and accept BEFORE any
        # order is placed. Everything above (onboarding, wallet, collateral)
        # has already passed, so this card is the last thing before the start.
        if strategy_id == "vol" and not fee_agreed:
            vol_est = await run_blocking(
                _vol_fee_estimate, telegram_id, product, network
            )
            _vol_sl = float((strategy_conf or {}).get("sl_pct") or 0.0)
            # CONSENT INTEGRITY: remember exactly what this card quoted. The
            # callback carries only the product, so without this a settings
            # change between rendering and tapping would start the run on
            # numbers the user never saw — and the ack record would rewrite
            # itself to match, destroying the audit trail too.
            context.user_data["vol_fee_quote"] = _vol_fee_quote_key(vol_est, _vol_sl)
            await _edit_loc(
                query,
                _vol_fee_agreement_text(vol_est, product, network, sl_pct=_vol_sl),
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "✅ Agree & Start",
                        callback_data=f"strategy:startok:vol:{product}",
                    )],
                    [InlineKeyboardButton(
                        "◀ Back", callback_data="strategy:preview:vol",
                    )],
                ]),
            )
            return
        if strategy_id == "vol" and fee_agreed:
            # Re-quote and compare against what the card actually showed. Any
            # drift (margin, target, rate, stop) means this consent was given
            # for different charges — re-render and make the user agree again
            # rather than trade on numbers they never saw.
            _live_est = await run_blocking(
                _vol_fee_estimate, telegram_id, product, network
            )
            _live_sl = float((strategy_conf or {}).get("sl_pct") or 0.0)
            _live_key = _vol_fee_quote_key(_live_est, _live_sl)
            if context.user_data.get("vol_fee_quote") != _live_key:
                context.user_data["vol_fee_quote"] = _live_key
                await _edit_loc(
                    query,
                    "⚠️ *Your settings changed since this quote\\.*\n\n"
                    + _vol_fee_agreement_text(
                        _live_est, product, network, sl_pct=_live_sl
                    ),
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton(
                            "✅ Agree & Start",
                            callback_data=f"strategy:startok:vol:{product}",
                        )],
                        [InlineKeyboardButton(
                            "◀ Back", callback_data="strategy:preview:vol",
                        )],
                    ]),
                )
                return
            # AUDIT-VOL-2026-07-31 F8: the start result arrives as a NEW
            # message, so the fee card and its "Agree & Start" button stay
            # live. Re-tapping later re-reads settings and could start with a
            # margin/target the user never saw quoted. Strip the keyboard so
            # the consent is single-use.
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:  # noqa: BLE001  # policy: degrade-ok(cosmetic; consent already captured)
                pass
            # Record what was agreed to, against the SAME inputs the run uses.
            try:
                _ack_est = await run_blocking(
                    _vol_fee_estimate, telegram_id, product, network
                )
                await run_blocking(_vol_record_fee_ack, telegram_id, _ack_est)
            except Exception:  # noqa: BLE001  # policy: degrade-ok(audit stamp only; never block a consented start)
                logger.warning("vol fee-ack persist failed", exc_info=True)
        start_payload = {
            "type": "start_strategy",
            "strategy": strategy_id,
            "product": product,
            "leverage": strategy_leverage,
            "slippage_pct": settings.get("slippage", 1),
            "direction": start_direction,
        }
        if strategy_id == "vol":
            start_payload["vol_market"] = vol_m
        await execute_action_directly(query, context, telegram_id, start_payload)
    elif action == "status":
        # Same merged dashboard as /status (Refresh/Stop) whether opened from
        # strategy cards (GRID/RGRID/DGRID/MID/DN/VOL/BRO) or commands.
        body, merged_kb = await build_status_dashboard_parts(telegram_id)
        await _edit_loc(
            query,
            body,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=merged_kb,
        )
    elif action == "stop":
        ok, msg = await run_blocking(stop_user_bot, telegram_id, True)
        body, merged_kb = await build_status_dashboard_parts(telegram_id)
        prefix = "🛑" if ok else "⚠️"
        await _edit_loc(
            query,
            "{body}\n\n{prefix} {msg}",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=merged_kb,
            body=body,
            prefix=prefix,
            msg=escape_md(msg),
        )


def _mm_cycle_budget_preflight(
    strategy_id: str,
    strategy_conf: dict,
    leverage: float,
    *,
    wallet_usdt: float | None = None,
) -> tuple[bool, float, float, float, int, float]:
    """MM grid family: collateral vs estimated margin per venue-sized quote.

    Returns
        ok, collateral_budget, required_min_collateral_1q, min_order_notional,
        max_resting_quotes_est, margin_per_quote_est
    """
    if strategy_id not in ("grid", "rgrid", "dgrid", "mid"):
        return True, 0.0, 0.0, 0.0, 0, 0.0

    from src.nadobro.quant.mm_quote_math import DEFAULT_MIN_ORDER_NOTIONAL_USD, estimate_mm_quote_capacity

    margin_usd = max(0.0, float(strategy_conf.get("notional_usd", 100.0) or 0.0))
    cycle_cfg = max(0.0, float(strategy_conf.get("cycle_notional_usd", margin_usd) or 0.0))
    collateral_cfg = max(cycle_cfg, margin_usd)
    if wallet_usdt is not None and float(wallet_usdt) > 0:
        collateral_budget = min(collateral_cfg, float(wallet_usdt))
    else:
        collateral_budget = collateral_cfg

    min_order_notional = max(
        1.0,
        float(strategy_conf.get("min_order_notional_usd") or DEFAULT_MIN_ORDER_NOTIONAL_USD),
    )
    try:
        max_open_orders = int(strategy_conf.get("max_open_orders", 6) or 6)
    except (TypeError, ValueError):
        max_open_orders = 6
    max_open_orders = max(1, max_open_orders)

    lev = max(1.0, float(leverage or 1.0))
    cap = estimate_mm_quote_capacity(
        collateral_budget,
        min_order_notional,
        lev,
        max_open_orders=max_open_orders,
    )
    max_q = int(cap["max_resting_quotes"])
    margin_per = float(cap["margin_per_quote_est_usd"])
    required_min = float(cap["min_collateral_1_quote_usd"])
    ok = max_q >= 1
    return ok, collateral_budget, required_min, min_order_notional, max_q, margin_per


def _fmt_strategy_config_text(strategy: str, conf: dict, network: str) -> str:
    if strategy == "vol":
        tp_pct = float(conf.get("tp_pct", 1.0))
        sl_pct = float(conf.get("sl_pct", 1.0))
        direction = "SHORT" if str(conf.get("vol_direction", "long")).lower() == "short" else "LONG"
        return (
            "⚙️ *VOL*\n\n"
            f"Mode: *{escape_md(network.upper())}*\n"
            f"Fixed margin: *{escape_md('$100.00')}* · Fixed leverage: *{escape_md('1x')}*\n"
            f"Direction: *{escape_md(direction)}*\n"
            "Entry: *Limit @ mid* · Exit: *Market close after 60s from fill*\n"
            f"Session TP/SL: *{escape_md(f'{tp_pct:.2f}%/{sl_pct:.2f}%')}* of fixed margin\n\n"
            "Use controls below to change direction or TP/SL only\\."
        )

    notional = float(conf.get("notional_usd", 100.0))
    spread_bp = float(conf.get("spread_bp", 5.0))
    if strategy == "rgrid":
        spread_bp = float(conf.get("rgrid_spread_bp", conf.get("grid_spread_bp", spread_bp)))
    interval_seconds = int(conf.get("interval_seconds", 60))
    tp_pct = float(conf.get("tp_pct", 1.0))
    sl_pct = float(conf.get("sl_pct", 0.5))
    base = (
        f"⚙️ *{escape_md(strategy.upper())}*\n\n"
        f"Mode: *{escape_md(network.upper())}*\n"
        f"Margin: *{escape_md(f'${notional:,.2f}')}* · Spread: *{escape_md(f'{spread_bp:.1f} bp')}*\n"
        f"Interval: *{escape_md(f'{interval_seconds}s')}*"
    )
    if strategy != "rgrid":
        base += f" · TP/SL: *{escape_md(f'{tp_pct:.2f}%/{sl_pct:.2f}%')}*"
    base += "\n\n"
    extra = ""
    if strategy == "rgrid":
        grid_sl = float(conf.get("rgrid_stop_loss_pct", conf.get("grid_stop_loss_pct", sl_pct)))
        grid_tp = float(conf.get("rgrid_take_profit_pct", conf.get("grid_take_profit_pct", tp_pct)))
        grid_discretion = float(conf.get("rgrid_discretion", conf.get("grid_discretion", 0.06)))
        reset_threshold = float(conf.get("rgrid_reset_threshold_pct", conf.get("grid_reset_threshold_pct", 0.2)))
        reset_timeout = int(conf.get("rgrid_reset_timeout_seconds", conf.get("grid_reset_timeout_seconds", 120)))
        spread_hint = "Reverse breakout width"
        extra = (
            f"Reverse Levels: *{escape_md(str(int(conf.get('levels', 4))))}* \\| "
            f"Spread mode: *{escape_md(spread_hint)}*\n"
            f"PnL SL/TP: *{escape_md(f'{grid_sl:.2f}% / {grid_tp:.2f}%')}* \\| "
            f"Reset: *{escape_md(f'{reset_threshold:.2f}% / {reset_timeout}s')}*\n"
            f"Discretion: *{escape_md(f'{grid_discretion:.2f}')}*\n\n"
        )
    elif strategy == "dgrid":
        trend_on = float(conf.get("dgrid_trend_on_variance_ratio", 1.25))
        range_on = float(conf.get("dgrid_range_on_variance_ratio", 1.15))
        dgrid_spread = float(conf.get("dgrid_spread_bp", conf.get("spread_bp", 8.0)))
        extra = (
            f"Auto phase: *GRID ⇄ RGRID* \\| Variance: *{escape_md(f'{range_on:.2f} / {trend_on:.2f}')}*\n"
            f"Spread: *{escape_md(f'{dgrid_spread:.1f} bp')}*\n\n"
        )
    elif strategy == "grid":
        threshold = f"{float(conf.get('threshold_bp', 12.0)):.1f} bp"
        close_offset = f"{float(conf.get('close_offset_bp', 24.0)):.1f} bp"
        ref_mode = str(conf.get("reference_mode", "ema_fast")).upper()
        bias = str(conf.get("directional_bias", "neutral")).upper()
        cycle_notional = float(conf.get("cycle_notional_usd", notional))
        session_cap = float(conf.get("session_notional_cap_usd", 0) or 0)
        inv_soft = float(conf.get("inventory_soft_limit_usd", notional * 0.6))
        quote_ttl = int(conf.get("quote_ttl_seconds", 90))
        min_spread = float(conf.get("min_spread_bp", 2.0))
        max_spread = float(conf.get("max_spread_bp", 20.0))
        vol_sensitivity = float(conf.get("vol_sensitivity", 0.02))
        cap_str = f"${session_cap:,.0f}" if session_cap > 0 else "OFF"
        spread_band = f"{min_spread:.1f} - {max_spread:.1f} bp"
        extra = (
            f"Move to quote: *{escape_md(threshold)}* · Close offset: *{escape_md(close_offset)}*\n"
            f"Ref: *{escape_md(ref_mode)}* · Bias: *{escape_md(bias)}*\n"
            f"Per\\-cycle budget: *{escape_md(f'${cycle_notional:,.0f}')}* · Cap: *{escape_md(cap_str)}*\n"
            f"Inventory limit: *{escape_md(f'${inv_soft:,.0f}')}* · Quote TTL: *{escape_md(f'{quote_ttl}s')}*\n"
            f"Spread band: *{escape_md(spread_band)}* · Vol: *{escape_md(f'{vol_sensitivity:.3f}')}*\n\n"
        )
    elif strategy == "dn":
        auto_close = "ON" if float(conf.get("auto_close_on_maintenance", 1) or 0) >= 0.5 else "OFF"
        leg_size = float(conf.get("fixed_margin_usd", conf.get("notional_usd", 100.0)) or 100.0)
        hold_s = int(conf.get("dn_hold_seconds", 3600) or 3600)
        cycles = int(conf.get("dn_cycles", 1) or 1)
        drift_pct = float(conf.get("dn_max_drift_pct", 5.0) or 5.0)
        extra = (
            "Hedge model: *Spot long \\+ 1x perp short* \\(BTC, ETH, QQQ, SPY…\\)\n"
            f"Size \\(per leg\\): {_leg_size_display(leg_size, str(conf.get('product') or ''), network)} · "
            f"Hold: *{escape_md(_fmt_hold_duration(hold_s))}* · Cycles: *{escape_md(str(cycles))}*\n"
            f"Hedge drift gate: *{escape_md(f'{drift_pct:.1f}%')}*\n"
            f"Auto\\-close on maintenance: *{escape_md(auto_close)}*\n\n"
        )
    return base + extra + "Use presets or set custom values below\\."


def _strategy_config_default_section(strategy: str) -> str:
    return "risk" if strategy == "vol" else "setup"


def _strategy_config_sections(strategy: str) -> list[tuple[str, str]]:
    if strategy == "vol":
        # NO Direction tab. Vol is spot-only, and bot_runtime FORCE-SETS
        # vol_direction="long" for a spot market (bot_runtime.py:1089), so a
        # SHORT the user picked could never be honoured — the card would have
        # claimed SHORT while every run bought spot. Audit round 3: an
        # unhonourable control is worse than no control. Re-add this tab only
        # together with a vol market that can actually run short.
        return [("risk", "🛡 TP / SL")]
    if strategy == "grid":
        return [("setup", "⚙️ Core"), ("execution", "📐 Spread"), ("risk", "🛡 Risk")]
    if strategy == "rgrid":
        # The "reset" section id is kept (callbacks and pending-input routing use
        # it). The threshold ARMS the trailing soft reset: once price has drifted
        # favourably by it, the exit leg follows the trend instead of banking.
        return [("setup", "⚙️ Core"), ("risk", "🛡 Risk"), ("reset", "🎯 Soft Reset")]
    if strategy == "dgrid":
        return [("setup", "⚙️ Core"), ("regime", "⚡ Regime"), ("risk", "🛡 Risk")]
    if strategy == "mid":
        # Mid Mode is intentionally lean (Tread parity): Core (margin/spread/levels)
        # + Risk (TP/SL). No regime, no momentum, no soft-reset, no anchor.
        return [("setup", "⚙️ Core"), ("risk", "🛡 Risk")]
    if strategy == "dn":
        return [("setup", "⚙️ Core"), ("safety", "🛡 Safety")]
    return [("setup", "⚙️ Core")]


def _strategy_section_for_field(strategy: str, field: str) -> str:
    if strategy == "vol":
        return "risk"
    if strategy == "grid":
        if field in {"min_spread_bp", "max_spread_bp", "grid_reset_threshold_pct"}:
            return "execution"
        if field in {"cycle_notional_usd", "inventory_soft_limit_usd", "session_notional_cap_usd"}:
            return "risk"
        return "setup"
    if strategy == "rgrid":
        if field in {"rgrid_reset_threshold_pct", "rgrid_reset_timeout_seconds", "rgrid_discretion"}:
            return "reset"
        if field in {"rgrid_stop_loss_pct", "rgrid_take_profit_pct"}:
            return "risk"
        return "setup"
    if strategy == "dgrid":
        if field.startswith("dgrid_"):
            return "regime"
        if field in {"rgrid_stop_loss_pct", "rgrid_take_profit_pct", "tp_pct", "sl_pct"}:
            return "risk"
        return "setup"
    if strategy == "mid":
        if field in {"tp_pct", "sl_pct", "rgrid_stop_loss_pct", "rgrid_take_profit_pct"}:
            return "risk"
        return "setup"
    if strategy == "dn":
        return "safety" if field in {"auto_close_on_maintenance", "dn_max_drift_pct"} else "setup"
    return "setup"


def _fmt_hold_duration(seconds: int) -> str:
    """Human-friendly hold duration: 3600 -> '1h', 21600 -> '6h', 5400 -> '90m'."""
    s = max(0, int(seconds or 0))
    if s and s % 3600 == 0:
        return f"{s // 3600}h"
    if s and s % 60 == 0:
        return f"{s // 60}m"
    return f"{s}s"


def _strategy_config_menu_text(strategy: str, conf: dict, network: str) -> str:
    titles = {
        "grid": "GRID",
        "rgrid": "Reverse GRID",
        "dgrid": "Dynamic GRID",
        "mid": "Mid Mode",
        "dn": "Delta Neutral",
        "vol": "Volume Bot",
    }
    return (
        f"⚙️ *{escape_md(titles.get(strategy, strategy.upper()))} Advanced*\n\n"
        f"Mode: *{escape_md(network.upper())}*\n\n"
        "Choose one section below to edit\\. This keeps the setup clean and focused\\."
    )


def _strategy_config_menu_kb(strategy: str):
    rows = []
    section_buttons = [
        InlineKeyboardButton(label, callback_data=f"strategy:config_section:{strategy}:{section}")
        for section, label in _strategy_config_sections(strategy)
    ]
    for i in range(0, len(section_buttons), 2):
        rows.append(section_buttons[i:i + 2])
    rows.append([InlineKeyboardButton("◀ Back", callback_data=f"strategy:preview:{strategy}")])
    return InlineKeyboardMarkup(rows)


def _mm_effective_leverage(conf: dict) -> float:
    """The leverage the ENGINE will deploy at — resolved by the engine's own
    helper rather than mirrored, so the card cannot drift from it.

    This used to be a hand-written "override wins, else 3x" copy, and it was
    wrong in both directions (CARD-LEV-DIVERGE, self-review 2026-08-07). The
    engine resolves ``mm_leverage_override`` -> session ``leverage`` -> 1x, and
    for grid/rgrid/dgrid the start path sets that session ``leverage`` to the
    PAIR MAX. On BTC (50x) the card therefore priced every size at 3x: it showed
    a $75 step where the engine deployed $306.98, and — because the step cap is
    computed from the same number — it reported "not capped" on a run the engine
    was capping. The whole stop-headroom warning was blind to the real size.

    ``_conf_for_card`` injects ``leverage`` with the value the start path will
    resolve, so a card rendered BEFORE the run agrees with the run.
    """
    from src.nadobro.strategy.engine_runtime import _effective_leverage

    return _effective_leverage(conf, 1.0)


def _start_leverage_for_card(
    settings: dict, strategy_id: str, product: str | None, network: str | None,
) -> float:
    """The session leverage the START path will hand the engine.

    Mirrors the resolution in the ``preview`` branch (the value that becomes
    ``state["leverage"]``, which is what ``_effective_leverage`` reads): vol is
    always 1x, dn is clamped to 5x, grid/rgrid/dgrid deploy at the PAIR MAX, and
    everything else takes the user's ``default_leverage``. Kept next to
    ``_mm_effective_leverage`` so the two stay in step.

    Cache-first: ``get_product_max_leverage`` reads the product catalog, the same
    synchronous call the start branch already makes on a tap, and any failure
    degrades to ``default_leverage`` rather than blocking the card.
    """
    if strategy_id == "vol":
        return 1.0
    try:
        default_lev = float(settings.get("default_leverage", 3) or 3)
    except (TypeError, ValueError):
        default_lev = 3.0
    if strategy_id == "dn":
        return max(1.0, min(default_lev, 5.0))
    if strategy_id in ("grid", "rgrid", "dgrid") and product:
        try:
            from src.nadobro.config import get_product_max_leverage as _gpml

            return max(1.0, float(_gpml(str(product), network=network)))
        except Exception:  # noqa: BLE001  # policy: degrade-ok(card falls back to the user default)
            return max(1.0, default_lev)
    return max(1.0, default_lev)


def rgrid_step_plan(conf: dict, sl_pct_val: float):
    """The per-break size R-Grid will actually trade, and why.

    Uses the SAME pure math the engine mapping uses
    (:mod:`src.nadobro.quant.rgrid_sizing`), so the card can never quote a size
    the strategy does not place. Lazy import: handlers/ has no module-level edge
    to quant/ (tests/lint/test_architecture_layers.py).
    """
    from src.nadobro.quant.mm_quote_math import DEFAULT_MIN_ORDER_NOTIONAL_USD
    from src.nadobro.quant.rgrid_sizing import resolve_step_quote
    from src.nadobro.strategy.strategy_registry import session_margin_usd

    # TWO different bases, on purpose — mixing them is what the 2026-08-06 audit
    # caught. Deployment is cycle-first (what the engine actually trades); the stop
    # budget is notional-first (what the session rail measures its % against).
    deploy_margin = _effective_margin_usd(conf)
    rail_margin = session_margin_usd(conf) or deploy_margin
    levels = max(1, int(float(conf.get("levels", 4) or 4)))
    # The same band engine_runtime derives (spread_bp -> fraction, 10bp default),
    # so the card's cap matches the engine's on the price bound too.
    _band_bp = float(conf.get("rgrid_spread_bp", conf.get("spread_bp", 10.0)) or 10.0)
    return rail_margin, resolve_step_quote(
        deployed_quote=deploy_margin * _mm_effective_leverage(conf),
        levels=levels,
        stop_budget_usd=rail_margin * max(0.0, sl_pct_val) / 100.0,
        min_step_usd=DEFAULT_MIN_ORDER_NOTIONAL_USD,
        band_frac=(_band_bp / 10000.0) if _band_bp > 0 else 0.001,
    )


def rgrid_stop_headroom(conf: dict, sl_pct_val: float) -> dict:
    """How much room the session stop leaves R-Grid to trade, AFTER the step cap.

    SL/TP are % of MARGIN and the rail judges live PnL NET of fees, so the real
    question is not "is 1% a sensible stop" but "how many round trips fit inside 1%
    of my margin before the stop fires on costs alone". The engine caps the step so
    at least ~3 do; this reports the resulting numbers and flags the one case the
    cap cannot fix.

    Costed at the TAKER round trip on purpose. R-Grid rests post-only quotes, so its
    real cost is lower — pricing the bound conservatively means the displayed
    headroom is a floor, never an overstatement.
    """
    margin, plan = rgrid_step_plan(conf, sl_pct_val)
    budget = float(plan.stop_budget_usd)
    fee = float(plan.round_trip_cost)
    return {
        "margin": margin,
        "budget_usd": budget,
        "step_usd": float(plan.step),
        "uncapped_step_usd": float(plan.uncapped),
        "round_trip_usd": fee,
        "ratio": (fee / budget) if budget > 0 else 0.0,
        "round_trips": (budget / fee) if fee > 0 else float("inf"),
        "capped": bool(plan.capped),
        "floored": bool(plan.floored),
    }


def _rgrid_step_cap_note(conf: dict, sl_pct_val: float) -> str:
    """Explain a step that is smaller than margin x leverage / levels, or warn when
    even the venue-minimum step cannot fit inside the stop."""
    room = rgrid_stop_headroom(conf, sl_pct_val)
    if room["budget_usd"] <= 0:
        return (
            "\n\n⚠️ PnL stop disarmed — the step is NOT capped against a stop "
            "budget; only the net\\-exposure cap and take profit remain\\."
        )
    step = escape_md(f"${room['step_usd']:,.0f}")
    fee = escape_md(f"${room['round_trip_usd']:,.2f}")
    budget = escape_md(f"${room['budget_usd']:,.2f}")
    if room["floored"]:
        # The budget wanted a step SMALLER than the venue accepts, so the cap could
        # not finish the job. How bad that is depends on what is actually left.
        if room["ratio"] >= 1.0:
            return (
                f"\n\n🚨 *Stop too tight to trade\\.* The smallest step the venue "
                f"accepts \\({step}\\) costs {fee} per round trip — more than "
                f"your whole {budget} stop budget, so the session stops out on fees "
                "alone whichever way price goes\\. Raise the PnL stop or add margin\\."
            )
        trips = escape_md(f"{room['round_trips']:.1f}")
        return (
            f"\n\n⚠️ *Thin\\.* The step is already at the venue minimum \\({step}\\) "
            f"so it cannot be capped further: {fee} per round trip leaves only about "
            f"{trips} inside your {budget} stop budget\\. Raise the PnL stop or add "
            "margin for more room\\."
        )
    if room["capped"]:
        uncapped = escape_md(f"${room['uncapped_step_usd']:,.0f}")
        trips = escape_md(f"{room['round_trips']:.0f}")
        return (
            f"\n\n🛡 Step capped to *{step}* per break \\(from {uncapped}\\) so your "
            f"stop budget covers about {trips} round trips\\. Raise the PnL "
            "stop or lower leverage to trade larger steps\\."
        )
    return ""


def _mm_sizing_line(conf: dict) -> str:
    """'Leverage Nx -> Position $X (margin x lev)' + preset label for the Core
    card, so picking Tiny/Standard or a leverage button VISIBLY changes the card
    and the resulting position size (the #4 'nothing changes' fix)."""
    margin = _effective_margin_usd(conf)
    override = float(conf.get("mm_leverage_override", 0) or 0)
    eff_lev = _mm_effective_leverage(conf)
    deployed = margin * eff_lev
    preset = str(conf.get("mm_preset") or "").lower()
    preset_label = {"tiny": "Tiny Budget", "standard": "Standard"}.get(preset, "—")
    src = "preset/custom" if override >= 1 else "default"
    return (
        f"Leverage: *{escape_md(f'{eff_lev:.0f}x')}* \\({escape_md(src)}\\) \\| "
        f"Position: *{escape_md(f'${deployed:,.0f}')}* \\(margin×lev\\)\n"
        f"Preset: *{escape_md(preset_label)}*"
    )


def _effective_margin_usd(conf: dict) -> float:
    """The margin the ENGINE will actually use.

    map_strategy_config resolves ``cycle_notional_usd`` FIRST and only falls
    back to ``notional_usd``, so a user who taps a "Cycle $250" button gets $250
    deployed while every card kept printing the old Margin value. Same
    resolution order here keeps the card and the engine telling one story.
    """
    for key in ("cycle_notional_usd", "notional_usd"):
        try:
            value = float(conf.get(key) or 0.0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 100.0


def _ladder_line(conf: dict) -> str:
    """'Levels: N x $S each | Curve: FLAT' for the laddered quoters.

    Shows the PER-LEVEL size because that is the number the venue minimum acts
    on: the planner drops levels whose size falls under the product floor, so a
    user picking 8 levels on a small margin needs to see each rung shrink. The
    total never changes — levels redistribute the same deployment.

    AUDIT-F11: this used to print ``deployed / levels`` — the FLAT size — for
    every curve. On geometric/4 at $100 it claimed "4 × $25 each" when the plan
    is $6.67 / $13.33 / $26.67 / $53.33, and it could not show the level count
    being cut by the venue minimum at all. Ask the planner instead of dividing.
    """
    from decimal import Decimal as _D

    from src.nadobro.quant.ladder import plan_ladder

    levels = max(1, int(conf.get("levels", 2) or 2))
    deployed = _effective_margin_usd(conf) * _mm_effective_leverage(conf)
    curve = str(conf.get("size_curve") or "flat").lower()
    min_notional = 0.0
    product = str(conf.get("product") or "")
    if product:
        try:
            from src.nadobro.venue.product_catalog import get_product_min_quote_notional_usd

            min_notional = float(get_product_min_quote_notional_usd(product) or 0.0)
        except Exception:  # noqa: BLE001  # policy: degrade-ok(no catalog -> no clamp preview)
            min_notional = 0.0
    plan = plan_ladder(
        _D(str(deployed)), levels=levels, step_bp=1, curve=curve,
        min_notional=_D(str(min_notional)),
    )
    if not plan:
        return f"Levels: *{escape_md(str(levels))}* \\| Curve: *{escape_md(curve.upper())}*"
    sizes = [float(lv.size_quote) for lv in plan]
    if len({f"{s:.2f}" for s in sizes}) == 1:
        size_str = f"*{escape_md(f'${sizes[0]:,.0f}')}* each"
    else:
        size_str = f"*{escape_md(f'${sizes[0]:,.0f}')}*→*{escape_md(f'${sizes[-1]:,.0f}')}* \\(near→deep\\)"
    clamped = (
        f"\n⚠️ Capped at *{escape_md(str(len(plan)))}* of {escape_md(str(levels))} levels "
        f"by the {escape_md(f'${min_notional:,.0f}')} venue minimum\\."
        if len(plan) < levels else ""
    )
    return (
        f"Levels: *{escape_md(str(len(plan)))}* × {size_str} "
        f"\\| Curve: *{escape_md(curve.upper())}*{clamped}"
    )


def _conf_for_card(
    settings: dict, strategy_id: str, context, network: str | None = None,
) -> dict:
    """The strategy's stored config PLUS the product the user has selected.

    ``settings["strategies"][sid]`` has no "product" key — the chosen asset lives
    in ``context.user_data["strategy_pair:<sid>"]``, set during preview. The cards
    were passing ``conf.get('product')`` to ``_leg_size_display``, which always hit
    its `if not spot_base` early return, so the effective-size disclosure (the
    venue exit floor raising a $100 leg to $115) was DEAD in every config card.
    Audit round 3. This is a display-only overlay; it never mutates the stored dict.
    """
    conf = dict(settings.get("strategies", {}).get(strategy_id, {}) or {})
    try:
        sel = context.user_data.get(f"strategy_pair:{strategy_id}")
    except Exception:  # noqa: BLE001  # policy: degrade-ok(no context -> card falls back to the plain size)
        sel = None
    if sel:
        conf.setdefault("product", str(sel).upper())
    # ...and the leverage the run will actually deploy at. The stored per-strategy
    # config has no ``leverage`` key — the session value is resolved at START from
    # the pair max — so every size the card renders (step, deployed notional, the
    # stop-budget cap and its four warning tiers) was computed at a hardcoded 3x
    # while the engine ran at up to 50x. setdefault, so a stored value or an
    # explicit mm_leverage_override still wins, exactly as in the engine.
    conf.setdefault(
        "leverage",
        _start_leverage_for_card(settings, strategy_id, conf.get("product"), network),
    )
    return conf


def _leg_size_display(user_leg_usd: float, spot_base: str | None, network: str) -> str:
    """Render the leg size, showing the EFFECTIVE size when the venue's exit
    floor raises it.

    SPOT-EXIT-GUARANTEE: a venue min NOTIONAL applies to the EXIT as well as the
    entry, so a leg sized at the floor cannot be closed by a resting limit — fees
    plus any adverse tick put the exit under it and the leg strands naked (kBTC's
    $100 minimum against a ~$99 leg, prod 2026-07-28). engine_runtime raises such
    a leg to min_notional x 1.15, so the card must not keep claiming the smaller
    number the user typed. Cache-only lookup: a miss falls back to the plain
    value rather than blocking the tap on a venue read.
    """
    plain = f"${user_leg_usd:,.0f}"
    if not spot_base or user_leg_usd <= 0:
        return escape_md(plain)
    try:
        from src.nadobro.venue.product_catalog import spot_min_notional_cached
        from src.nadobro.quant.mm_quote_math import min_closeable_entry_notional

        floor = spot_min_notional_cached(spot_base, network)
        effective = min_closeable_entry_notional(floor or 0.0)
    except Exception:  # noqa: BLE001 - display only, never break the card
        return escape_md(plain)
    if effective <= user_leg_usd:
        return escape_md(plain)
    return (
        f"{escape_md(plain)} → *{escape_md(f'${effective:,.0f}')}*"
        f" {escape_md(f'(venue exit floor ${floor:,.0f})')}"
    )


def _strategy_config_section_text(strategy: str, conf: dict, network: str, section: str) -> str:
    if strategy == "vol":
        direction = "SHORT" if str(conf.get("vol_direction", "long")).lower() == "short" else "LONG"
        tp_pct = float(conf.get("tp_pct", 1.0))
        sl_pct = float(conf.get("sl_pct", 1.0))
        _vol_margin = float(conf.get("session_margin_usd", conf.get("notional_usd", 100.0)) or 100.0)
        _algo = str(conf.get("vol_execution_algo") or "twap").lower()
        _algo_label = {
            "twap": "Maker TWAP (rests, ~4x cheaper)",
            "chase": "Chase (maker, fills faster)",
            "taker": "Taker (crosses, most expensive)",
        }.get(_algo, _algo)
        return (
            "⚙️ *Vol Bot · TP / SL*\n\n"
            f"Execution: *{escape_md(_algo_label)}*\n"
            f"Current TP/SL: *{escape_md(f'{tp_pct:.2f}% / {sl_pct:.2f}%')}*\n"
            f"Margin per cycle: {_leg_size_display(_vol_margin, str(conf.get('product') or ''), network)}\n\n"
            "Choose quick presets or set custom values\\."
        )

    notional = _effective_margin_usd(conf)
    spread_bp = float(conf.get("spread_bp", 5.0))
    interval_seconds = int(conf.get("interval_seconds", 60))
    tp_pct = float(conf.get("tp_pct", 1.0))
    sl_pct = float(conf.get("sl_pct", 0.5))

    if strategy == "grid":
        # Fill-anchored Grid reads NONE of threshold_bp / close_offset_bp /
        # reference_mode / quote_ttl_seconds / inventory_soft_limit_usd /
        # cycle_notional_usd — yet all of them were rendered here, and the
        # Execution tab's text described knobs its own keyboard had already
        # replaced with the spread band. Only live values appear now.
        from src.nadobro.quant.mm_quote_math import _resolve_directional_bias_value

        try:
            g_bias = float(_resolve_directional_bias_value(conf.get("directional_bias")))
        except (TypeError, ValueError):
            g_bias = 0.0
        g_bias_str = (
            f"LONG {g_bias:+.2f}" if g_bias > 0
            else (f"SHORT {g_bias:+.2f}" if g_bias < 0 else "NEUTRAL")
        )
        if section == "execution":
            min_spread = float(conf.get("min_spread_bp", 2.0))
            max_spread = float(conf.get("max_spread_bp", 20.0))
            reset_pct = float(conf.get("grid_reset_threshold_pct", 0.2) or 0.2)
            return (
                "⚙️ *GRID · Spread*\n\n"
                f"Spread band: *{escape_md(f'{min_spread:.1f} – {max_spread:.1f} bp')}*\n"
                f"Soft reset: *{escape_md(f'{reset_pct:.2f}%')}* drift from the anchor\n\n"
                "The band floors and caps the auto\\-spread\\. Soft reset re\\-anchors both "
                "legs to mid when price runs away from the last fill\\."
            )
        if section == "risk":
            return (
                "⚙️ *GRID · Risk*\n\n"
                f"PnL TP/SL: *{escape_md(f'{tp_pct:.2f}% / {sl_pct:.2f}%')}* of margin\n\n"
                "Judged NET of fees on live PnL including unrealised\\.\n\n"
                "The net\\-exposure cap governs FILLED inventory: once a side is "
                "over the cap only the reducing side keeps quoting\\. Resting "
                "orders are not capped below one full deployment — a bid and an "
                "ask net to zero, and refusing the first quote would leave a flat "
                "book unable to start\\. Your margin is the hard ceiling\\."
            )
        pov_label = str(conf.get("participation_preset") or "OFF").upper()
        # Grid runs one of TWO controllers and the card must not describe the
        # wrong one. fill_anchored=0 (the default) is the classic static band;
        # =1 is the fill-anchored ladder the Levels/Curve controls drive.
        _fa = bool(float(conf.get("fill_anchored", 0) or 0))
        _mode_line = (
            f"Mode: *Fill\\-anchored ladder*\n{_ladder_line(conf)}\n" if _fa
            else f"Mode: *Classic band* \\({escape_md(str(int(conf.get('levels', 2) or 2)))} levels\\)\n"
        )
        _mode_help = (
            "Quotes step away from the LAST FILL, never buying above the last sell "
            "or selling below the last buy\\. Levels split the same margin into rungs "
            "so the book scales in and out of a move — they never add exposure\\."
            if _fa else
            "A static ladder centred on mid, rebuilt each cycle\\. Levels set the "
            "band width\\. Tap *Fill\\-anchored* to anchor on your last fill instead "
            "and enable per\\-rung sizing\\."
        )
        return (
            "⚙️ *GRID · Core*\n\n"
            f"Margin: *{escape_md(f'${notional:,.0f}')}* \\| Interval: *{escape_md(f'{interval_seconds}s')}*\n"
            f"Spread: *{escape_md(f'{spread_bp:.1f} bp')}* \\| Bias: *{escape_md(g_bias_str)}*\n"
            f"{_mode_line}"
            f"POV: *{escape_md(pov_label)}* \\(per\\-cycle pacing from Nado 24h volume\\)\n"
            f"{_mm_sizing_line(conf)}\n\n"
            f"{_mode_help}"
        )

    if strategy == "rgrid":
        if section == "risk":
            _sl_val = float(conf.get("rgrid_stop_loss_pct", sl_pct) or 0.0)
            pnl_sl = f"{_sl_val:.2f}%"
            pnl_tp = f"{float(conf.get('rgrid_take_profit_pct', tp_pct)):.2f}%"
            _room = rgrid_stop_headroom(conf, _sl_val)
            _budget_txt = escape_md(f"${_room['budget_usd']:,.2f}")
            _margin_txt = escape_md(f"${_room['margin']:,.0f}")
            _rt_txt = escape_md(f"${_room['round_trip_usd']:,.2f}")
            _step_txt = escape_md(f"${_room['step_usd']:,.0f}")
            _budget_line = (
                f"Stop budget: *{_budget_txt}* "
                f"\\({escape_md(pnl_sl)} of {_margin_txt} margin\\)\n"
                f"Step: *{_step_txt}* per break \\| Round trip \\(worst case\\): *{_rt_txt}*\n"
                if _room["budget_usd"] > 0 else ""
            )
            return (
                "⚙️ *Reverse GRID · Risk*\n\n"
                f"PnL stop: *{escape_md(pnl_sl)}* \\| "
                f"PnL take profit: *{escape_md(pnl_tp)}*\n"
                f"{_budget_line}\n"
                "Both are % of MARGIN, measured on live PnL \\(realized \\+ open\\) "
                "NET of fees — not raw market drift\\. R\\-Grid rests post\\-only "
                "limit orders, so it earns the spread rather than paying it; only the "
                "trailing stop crosses\\. The size per break is capped so even a "
                "worst\\-case crossing round trip fits inside that budget\\."
                f"{_rgrid_step_cap_note(conf, _sl_val)}"
            )
        if section == "reset":
            _tp_pct_val = float(conf.get("rgrid_reset_threshold_pct", 0.2) or 0.2)
            reset_threshold = f"{_tp_pct_val:.2f}%"
            discretion = f"{float(conf.get('rgrid_discretion', 0.06)):.2f}"
            # The controller REFUSES a target inside 2x the entry band (it would
            # close the position before the break that opened it is established).
            # 0.1% is a valid setting yet sits inside the band at the default
            # 10bp spread — so without this the knob would silently do nothing.
            _band_bp = max(
                float(conf.get("rgrid_spread_bp", conf.get("spread_bp", 10.0)) or 10.0),
                float(conf.get("min_spread_bp", 1.5) or 1.5),
            )
            _inactive = _tp_pct_val * 100.0 < 2.0 * _band_bp
            _tp_note = (
                f"\n⚠️ Inactive: needs \\> *{escape_md(f'{2.0 * _band_bp / 100.0:.2f}%')}* "
                f"at a {escape_md(f'{_band_bp:.1f} bp')} band \\(2× the entry band\\)\\."
                if _inactive else ""
            )
            # This threshold ARMS the soft reset — it is not a re-anchor (grid's
            # meaning) and no longer an immediate take-profit. Re-anchoring the
            # ENTRY on drift would suppress the very break the strategy exists to
            # trade; banking at the threshold would stop it following the trend.
            # rgrid_reset_timeout_seconds is NOT displayed — no controller reads it.
            return (
                "⚙️ *Reverse GRID · Soft Reset*\n\n"
                f"Arms after: *{escape_md(reset_threshold)}* favourable move{_tp_note}\n"
                f"Discretion: *{escape_md(discretion)}* \\(exposure VWAP window\\)\n\n"
                "R\\-Grid adds INTO a trend\\. Once the move has gone this far your "
                "way *and* you are in profit, the exit starts FOLLOWING the trend — "
                "one spread behind the best price, instead of sitting at your entry, "
                "so the run keeps going and the profit is locked\\. A pullback that "
                "stops short of it is booked by the resting limit; once price comes "
                "back THROUGH it the stop crosses and closes the whole position\\. "
                "Range 0\\.1–10%\\. Set it clear of the entry band or the exit would "
                "arm before the move that opened the position is established\\."
            )
        levels = str(int(conf.get("levels", 4)))
        rgrid_spread = f"{float(conf.get('rgrid_spread_bp', spread_bp)):.1f} bp"
        pov_label = str(conf.get("participation_preset") or "OFF").upper()
        return (
            "⚙️ *Reverse GRID · Core*\n\n"
            f"Margin: *{escape_md(f'${notional:,.0f}')}* \\| Interval: *{escape_md(f'{interval_seconds}s')}*\n"
            f"Levels: *{escape_md(levels)}* \\| Spread: *{escape_md(rgrid_spread)}*\n"
            f"POV: *{escape_md(pov_label)}*\n"
            f"{_mm_sizing_line(conf)}\n\n"
            "Both legs rest as *post\\-only limit orders* one spread either side of "
            "the *average of your buy and sell exposure prices* — the buy ABOVE it, "
            "the sell BELOW\\. Each becomes fillable once price has travelled past "
            "it, so you buy into strength and sell into weakness without ever paying "
            "the spread\\. Profits in trends, waits in chop — the mirror of GRID\\. "
            "The trailing stop is the one order that crosses\\. "
            "*Position size \\= margin × leverage*\\."
        )

    if strategy == "dgrid":
        if section == "regime":
            trend_on = float(conf.get("dgrid_trend_on_variance_ratio", 1.25))
            range_on = float(conf.get("dgrid_range_on_variance_ratio", 1.15))
            min_spread = float(conf.get("dgrid_min_spread_bp", 2.0))
            max_spread = float(conf.get("dgrid_max_spread_bp", 50.0))
            return (
                "⚡ *Dynamic GRID · Regime*\n\n"
                f"Switch to RGRID: *{escape_md(f'{trend_on:.2f}')}* variance ratio\n"
                f"Switch to GRID: *{escape_md(f'{range_on:.2f}')}* variance ratio\n"
                f"Spread band: *{escape_md(f'{min_spread:.1f} - {max_spread:.1f} bp')}*\n\n"
                "DGRID uses hysteresis so it does not flip\\-flop in mixed regimes\\."
            )
        if section == "risk":
            pnl_sl = f"{float(conf.get('rgrid_stop_loss_pct', sl_pct)):.2f}%"
            pnl_tp = f"{float(conf.get('rgrid_take_profit_pct', tp_pct)):.2f}%"
            return (
                "⚡ *Dynamic GRID · Risk*\n\n"
                f"PnL stop: *{escape_md(pnl_sl)}* \\| PnL take profit: *{escape_md(pnl_tp)}*\n\n"
                "GRID phase watches realized PnL; RGRID phase watches open exposure risk\\."
            )
        levels = str(int(conf.get("levels", 4)))
        pov_label = str(conf.get("participation_preset") or "OFF").upper()
        return (
            "⚡ *Dynamic GRID · Core*\n\n"
            f"Margin: *{escape_md(f'${notional:,.0f}')}* \\| Interval: *{escape_md(f'{interval_seconds}s')}*\n"
            f"Levels: *{escape_md(levels)}* \\| Starting spread: *{escape_md(f'{spread_bp:.1f} bp')}*\n"
            f"POV: *{escape_md(pov_label)}*\n"
            f"{_mm_sizing_line(conf)}\n\n"
            "DGRID auto\\-switches GRID↔RGRID, ladders into the move, trails take\\-profit, "
            "and flips long↔short on a confirmed reversal\\. *Position size \\= margin × leverage*\\."
        )

    if strategy == "mid":
        # Mid Mode: pure mid ± spread. No anchor / no soft-reset.
        # AUDIT-MID-2026-07-30 #5 pulled "Levels" and "Reference" off this card
        # because the mid mapping consumed neither. LADDER (2026-08-02) made
        # Levels real — it now splits the side's deployment across levels
        # stepping away from mid — so it is back, with the size curve beside it.
        # "Reference" stays off: mid mode is always mid-referenced.
        try:
            bias_val = float(conf.get("directional_bias", 0.0) or 0.0)
        except (TypeError, ValueError):
            bias_val = 0.0
        if bias_val > 0:
            bias_str = f"LONG {bias_val:+.2f}"
        elif bias_val < 0:
            bias_str = f"SHORT {bias_val:+.2f}"
        else:
            bias_str = "NEUTRAL"
        if section == "risk":
            return (
                "⚙️ *MID MODE · Risk*\n\n"
                f"PnL TP/SL: *{escape_md(f'{tp_pct:.2f}% / {sl_pct:.2f}%')}* of margin\n\n"
                "Stops are applied to *margin* \\(notional / leverage\\), not to notional\\."
            )
        pov_label = str(conf.get("participation_preset") or "OFF").upper()
        return (
            "⚙️ *MID MODE · Core*\n\n"
            f"Margin: *{escape_md(f'${notional:,.0f}')}* \\| Interval: *{escape_md(f'{interval_seconds}s')}*\n"
            f"Spread: *{escape_md(f'{spread_bp:+.1f} bp')}* \\| Bias: *{escape_md(bias_str)}*\n"
            f"{_ladder_line(conf)}\n"
            f"POV: *{escape_md(pov_label)}* \\(per\\-cycle pacing from Nado 24h volume\\)\n"
            f"{_mm_sizing_line(conf)}\n\n"
            "Pure mid ± spread\\. No anchor, no soft\\-reset\\. Levels split the same "
            "margin into rungs stepping away from mid — more rungs scale in and out "
            "of a move; they never add exposure\\. "
            "Bias range −1\\.0 → \\+1\\.0 \\(short → long\\); \\|1\\.0\\| adds 20% margin\\."
        )

    if strategy == "dn":
        leg_size = float(conf.get("fixed_margin_usd", conf.get("notional_usd", 100.0)) or 100.0)
        hold_s = int(conf.get("dn_hold_seconds", 3600) or 3600)
        cycles = int(conf.get("dn_cycles", 1) or 1)
        drift_pct = float(conf.get("dn_max_drift_pct", 5.0) or 5.0)
        if section == "safety":
            auto_close = "ON" if float(conf.get("auto_close_on_maintenance", 1) or 0) >= 0.5 else "OFF"
            return (
                "⚙️ *Delta Neutral · Safety*\n\n"
                f"Hedge drift gate: *{escape_md(f'{drift_pct:.1f}%')}*\n"
                f"Auto\\-close on maintenance: *{escape_md(auto_close)}*\n\n"
                "If the two legs drift apart by more than the gate, both are closed immediately\\."
            )
        return (
            "⚙️ *Delta Neutral · Core*\n\n"
            f"Size \\(per leg\\): {_leg_size_display(leg_size, str(conf.get('product') or ''), network)}"
            f" \\| Short: *1x*\n"
            f"Hold: *{escape_md(_fmt_hold_duration(hold_s))}* \\| Cycles: *{escape_md(str(cycles))}*\n\n"
            "Buys spot \\+ 1x\\-shorts the perp, holds, then exits both legs together, repeated per cycle\\."
        )

    return _fmt_strategy_config_text(strategy, conf, network)


def _strategy_config_section_kb(strategy: str, section: str):
    if strategy == "vol":
        # VOL-DIRECTION-TAB: this branch used to IGNORE `section` and return the
        # margin/TP/SL/target keyboard for BOTH tabs. Tapping "🎯 Direction"
        # showed the Direction *heading* over the TP/SL *controls*, and
        # vol_direction had no button anywhere in the codebase — the value was
        # validated ({"long","short"}) and consumed by bot_runtime, but nothing
        # could ever set it from the UI. Reported 2026-07-28.
        # Volume is spot-only as of 2026-05. The user-tunable params are:
        # session margin (per-cycle notional), stop loss %, target volume.
        rows = [
            [
                # Band $100-$500 (docs/volume_bot_taker_v4.md). The old $1000
                # button wrote a value the mapping now clamps back to 500 —
                # a control that silently disagreed with the engine.
                InlineKeyboardButton("Margin $100", callback_data="strategy:set:vol:session_margin_usd:100"),
                InlineKeyboardButton("Margin $250", callback_data="strategy:set:vol:session_margin_usd:250"),
                InlineKeyboardButton("Margin $500", callback_data="strategy:set:vol:session_margin_usd:500"),
            ],
            [
                InlineKeyboardButton("✍️ Custom Margin", callback_data="strategy:input:vol:session_margin_usd"),
            ],
            [
                InlineKeyboardButton("TP 0.5%", callback_data="strategy:set:vol:tp_pct:0.5"),
                InlineKeyboardButton("TP 1.0%", callback_data="strategy:set:vol:tp_pct:1.0"),
                InlineKeyboardButton("TP 2.0%", callback_data="strategy:set:vol:tp_pct:2.0"),
            ],
            [
                InlineKeyboardButton("✍️ Custom TP", callback_data="strategy:input:vol:tp_pct"),
            ],
            [
                # EXECUTION ALGO. Maker TWAP is the default: measured on KBTC
                # spot a taker round trip costs 14.3bp vs 3.6bp resting, i.e.
                # ~4x the volume for the same stop budget.
                InlineKeyboardButton("⏱ Maker TWAP", callback_data="strategy:set_text:vol:vol_execution_algo:twap"),
                InlineKeyboardButton("🏃 Chase", callback_data="strategy:set_text:vol:vol_execution_algo:chase"),
                InlineKeyboardButton("⚡ Taker", callback_data="strategy:set_text:vol:vol_execution_algo:taker"),
            ],
            [
                # Every taker round trip costs spread + both legs' fees, so
                # the session stop is the binding limit on how much volume a
                # run completes. 5% of margin (-$5 on $100) is the default;
                # sub-1% stops trip almost immediately and are not offered.
                InlineKeyboardButton("SL 2%", callback_data="strategy:set:vol:sl_pct:2"),
                InlineKeyboardButton("SL 5%", callback_data="strategy:set:vol:sl_pct:5"),
                InlineKeyboardButton("SL 10%", callback_data="strategy:set:vol:sl_pct:10"),
            ],
            [
                InlineKeyboardButton("✍️ Custom SL", callback_data="strategy:input:vol:sl_pct"),
            ],
            [
                InlineKeyboardButton("Target $10k", callback_data="strategy:set:vol:target_volume_usd:10000"),
                InlineKeyboardButton("Target $25k", callback_data="strategy:set:vol:target_volume_usd:25000"),
                InlineKeyboardButton("Target $100k", callback_data="strategy:set:vol:target_volume_usd:100000"),
            ],
            [
                InlineKeyboardButton("✍️ Custom Target", callback_data="strategy:input:vol:target_volume_usd"),
            ],
        ]
        rows.append([InlineKeyboardButton("◀ Back", callback_data="strategy:config:vol")])
        return InlineKeyboardMarkup(rows)

    rows: list[list[InlineKeyboardButton]] = []
    if strategy == "grid":
        if section == "setup":
            rows = [
                [
                    InlineKeyboardButton("🎯 Tiny Budget Preset", callback_data="strategy:preset:grid:tiny"),
                    InlineKeyboardButton("Standard", callback_data="strategy:preset:grid:standard"),
                ],
                [
                    InlineKeyboardButton("🚀 Turbo Volume", callback_data="strategy:preset:grid:turbo"),
                ],
                [
                    InlineKeyboardButton("Margin $50", callback_data="strategy:set:grid:notional_usd:50"),
                    InlineKeyboardButton("Margin $100", callback_data="strategy:set:grid:notional_usd:100"),
                    InlineKeyboardButton("Margin $250", callback_data="strategy:set:grid:notional_usd:250"),
                ],
                [
                    InlineKeyboardButton("Lev 1x", callback_data="strategy:set:grid:mm_leverage_override:1"),
                    InlineKeyboardButton("3x", callback_data="strategy:set:grid:mm_leverage_override:3"),
                    InlineKeyboardButton("5x", callback_data="strategy:set:grid:mm_leverage_override:5"),
                    InlineKeyboardButton("10x", callback_data="strategy:set:grid:mm_leverage_override:10"),
                ],
                [
                    InlineKeyboardButton("Spread 2bp", callback_data="strategy:set:grid:spread_bp:2"),
                    InlineKeyboardButton("Spread 5bp", callback_data="strategy:set:grid:spread_bp:5"),
                    InlineKeyboardButton("Spread 10bp", callback_data="strategy:set:grid:spread_bp:10"),
                ],
                # AUDIT-F10: the Levels/Curve controls below drive the
                # FILL-ANCHORED quoter, which grid could not reach — fill_anchored
                # defaults to 0 and only rgrid had a toggle, so the buttons and the
                # card copy described a controller that never ran. Grid gets the
                # same opt-in rgrid has.
                [
                    InlineKeyboardButton("🪜 Fill-anchored (ladder)", callback_data="strategy:set:grid:fill_anchored:1"),
                    InlineKeyboardButton("📊 Classic band (default)", callback_data="strategy:set:grid:fill_anchored:0"),
                ],
                # LADDER: how many rungs the side's margin is split across, and
                # how size is distributed over them. The same total either way.
                [
                    InlineKeyboardButton("Levels 1", callback_data="strategy:set:grid:levels:1"),
                    InlineKeyboardButton("2", callback_data="strategy:set:grid:levels:2"),
                    InlineKeyboardButton("4", callback_data="strategy:set:grid:levels:4"),
                    InlineKeyboardButton("✍️", callback_data="strategy:input:grid:levels"),
                ],
                [
                    InlineKeyboardButton("Curve Flat", callback_data="strategy:set_text:grid:size_curve:flat"),
                    InlineKeyboardButton("Linear", callback_data="strategy:set_text:grid:size_curve:linear"),
                    InlineKeyboardButton("Geometric", callback_data="strategy:set_text:grid:size_curve:geometric"),
                ],
                [
                    InlineKeyboardButton("30s", callback_data="strategy:set:grid:interval_seconds:30"),
                    InlineKeyboardButton("60s", callback_data="strategy:set:grid:interval_seconds:60"),
                    InlineKeyboardButton("120s", callback_data="strategy:set:grid:interval_seconds:120"),
                ],
                [
                    InlineKeyboardButton("⚡ Aggressive", callback_data="strategy:set_text:grid:participation_preset:aggressive"),
                    InlineKeyboardButton("Normal", callback_data="strategy:set_text:grid:participation_preset:normal"),
                    InlineKeyboardButton("Passive", callback_data="strategy:set_text:grid:participation_preset:passive"),
                ],
                [
                    InlineKeyboardButton("Duration 30m", callback_data="strategy:set:grid:mm_duration_minutes:30"),
                    InlineKeyboardButton("2h", callback_data="strategy:set:grid:mm_duration_minutes:120"),
                    InlineKeyboardButton("✍️ Custom Duration", callback_data="strategy:input:grid:mm_duration_minutes"),
                ],
                [
                    InlineKeyboardButton("Pause: Off", callback_data="strategy:set:grid:twap_pause_move_bp:0"),
                    InlineKeyboardButton("1%", callback_data="strategy:set:grid:twap_pause_move_bp:100"),
                    InlineKeyboardButton("2%", callback_data="strategy:set:grid:twap_pause_move_bp:200"),
                    InlineKeyboardButton("✍️", callback_data="strategy:input:grid:twap_pause_move_bp"),
                ],
                [
                    InlineKeyboardButton("Custom Margin", callback_data="strategy:input:grid:notional_usd"),
                    InlineKeyboardButton("Custom Lev", callback_data="strategy:input:grid:mm_leverage_override"),
                    InlineKeyboardButton("Custom Interval", callback_data="strategy:input:grid:interval_seconds"),
                ],
            ]
        elif section == "execution":
            # Spread bounds: the floor/cap the ATR auto-spread clamps to (and the
            # manual-spread floor). Replaces the previous dead controls
            # (threshold/close-offset/reference-mode/bias/POV — none were wired).
            rows = [
                [
                    InlineKeyboardButton("Min Spread 2bp", callback_data="strategy:set:grid:min_spread_bp:2"),
                    InlineKeyboardButton("5bp", callback_data="strategy:set:grid:min_spread_bp:5"),
                    InlineKeyboardButton("10bp", callback_data="strategy:set:grid:min_spread_bp:10"),
                ],
                [
                    InlineKeyboardButton("Max Spread 30bp", callback_data="strategy:set:grid:max_spread_bp:30"),
                    InlineKeyboardButton("50bp", callback_data="strategy:set:grid:max_spread_bp:50"),
                    InlineKeyboardButton("100bp", callback_data="strategy:set:grid:max_spread_bp:100"),
                ],
                # Soft-reset threshold: fully validated and prompt-ready in the
                # backend since it shipped, but no button existed anywhere, so
                # the value could never be changed from the UI.
                [
                    InlineKeyboardButton("Reset 0.05%", callback_data="strategy:set:grid:grid_reset_threshold_pct:0.05"),
                    InlineKeyboardButton("0.25%", callback_data="strategy:set:grid:grid_reset_threshold_pct:0.25"),
                    InlineKeyboardButton("1.0%", callback_data="strategy:set:grid:grid_reset_threshold_pct:1.0"),
                    InlineKeyboardButton("✍️", callback_data="strategy:input:grid:grid_reset_threshold_pct"),
                ],
                [
                    InlineKeyboardButton("Custom Min Spread", callback_data="strategy:input:grid:min_spread_bp"),
                    InlineKeyboardButton("Custom Max Spread", callback_data="strategy:input:grid:max_spread_bp"),
                ],
            ]
        else:
            rows = [
                [
                    InlineKeyboardButton("TP 0.5%", callback_data="strategy:set:grid:tp_pct:0.5"),
                    InlineKeyboardButton("TP 1.0%", callback_data="strategy:set:grid:tp_pct:1.0"),
                    InlineKeyboardButton("TP 2.0%", callback_data="strategy:set:grid:tp_pct:2.0"),
                ],
                [
                    InlineKeyboardButton("SL 0.25%", callback_data="strategy:set:grid:sl_pct:0.25"),
                    InlineKeyboardButton("SL 0.5%", callback_data="strategy:set:grid:sl_pct:0.5"),
                    InlineKeyboardButton("SL 1.0%", callback_data="strategy:set:grid:sl_pct:1.0"),
                ],
                [
                    InlineKeyboardButton("Cycle $50", callback_data="strategy:set:grid:cycle_notional_usd:50"),
                    InlineKeyboardButton("Cycle $100", callback_data="strategy:set:grid:cycle_notional_usd:100"),
                    InlineKeyboardButton("Cycle $250", callback_data="strategy:set:grid:cycle_notional_usd:250"),
                ],
                # inventory_soft_limit_usd is read ONLY by the mid mapping, so
                # the old "Inv $30 / $60" buttons did nothing on grid. The reset
                # presets that sat here duplicated the Spread tab and one of
                # them (1.5%) is now above the 1% ceiling, i.e. a tap the
                # validator would silently swallow.
                [
                    InlineKeyboardButton("Custom TP", callback_data="strategy:input:grid:tp_pct"),
                    InlineKeyboardButton("Custom SL", callback_data="strategy:input:grid:sl_pct"),
                    InlineKeyboardButton("Session Cap", callback_data="strategy:input:grid:session_notional_cap_usd"),
                ],
            ]
    elif strategy == "dgrid":
        if section == "setup":
            rows = [
                [
                    InlineKeyboardButton("🎯 Tiny Budget Preset", callback_data="strategy:preset:dgrid:tiny"),
                    InlineKeyboardButton("Standard", callback_data="strategy:preset:dgrid:standard"),
                ],
                [
                    InlineKeyboardButton("🚀 Turbo Volume", callback_data="strategy:preset:dgrid:turbo"),
                ],
                [
                    InlineKeyboardButton("Margin $50", callback_data="strategy:set:dgrid:notional_usd:50"),
                    InlineKeyboardButton("Margin $100", callback_data="strategy:set:dgrid:notional_usd:100"),
                    InlineKeyboardButton("Margin $250", callback_data="strategy:set:dgrid:notional_usd:250"),
                ],
                [
                    InlineKeyboardButton("Lev 1x", callback_data="strategy:set:dgrid:mm_leverage_override:1"),
                    InlineKeyboardButton("3x", callback_data="strategy:set:dgrid:mm_leverage_override:3"),
                    InlineKeyboardButton("5x", callback_data="strategy:set:dgrid:mm_leverage_override:5"),
                    InlineKeyboardButton("10x", callback_data="strategy:set:dgrid:mm_leverage_override:10"),
                ],
                [
                    InlineKeyboardButton("Levels 3", callback_data="strategy:set:dgrid:levels:3"),
                    InlineKeyboardButton("Levels 4", callback_data="strategy:set:dgrid:levels:4"),
                    InlineKeyboardButton("Levels 6", callback_data="strategy:set:dgrid:levels:6"),
                ],
                [
                    InlineKeyboardButton("30s", callback_data="strategy:set:dgrid:interval_seconds:30"),
                    InlineKeyboardButton("60s", callback_data="strategy:set:dgrid:interval_seconds:60"),
                ],
                [
                    InlineKeyboardButton("⚡ Aggressive", callback_data="strategy:set_text:dgrid:participation_preset:aggressive"),
                    InlineKeyboardButton("Normal", callback_data="strategy:set_text:dgrid:participation_preset:normal"),
                    InlineKeyboardButton("Passive", callback_data="strategy:set_text:dgrid:participation_preset:passive"),
                ],
                [
                    InlineKeyboardButton("Duration 30m", callback_data="strategy:set:dgrid:mm_duration_minutes:30"),
                    InlineKeyboardButton("2h", callback_data="strategy:set:dgrid:mm_duration_minutes:120"),
                    InlineKeyboardButton("✍️ Custom Duration", callback_data="strategy:input:dgrid:mm_duration_minutes"),
                ],
                [
                    InlineKeyboardButton("Pause: Off", callback_data="strategy:set:dgrid:twap_pause_move_bp:0"),
                    InlineKeyboardButton("1%", callback_data="strategy:set:dgrid:twap_pause_move_bp:100"),
                    InlineKeyboardButton("2%", callback_data="strategy:set:dgrid:twap_pause_move_bp:200"),
                    InlineKeyboardButton("✍️", callback_data="strategy:input:dgrid:twap_pause_move_bp"),
                ],
                [
                    InlineKeyboardButton("Custom Margin", callback_data="strategy:input:dgrid:notional_usd"),
                    InlineKeyboardButton("Custom Interval", callback_data="strategy:input:dgrid:interval_seconds"),
                ],
            ]
        elif section == "regime":
            rows = [
                [
                    InlineKeyboardButton("Trend 1.25", callback_data="strategy:set:dgrid:dgrid_trend_on_variance_ratio:1.25"),
                    InlineKeyboardButton("Trend 1.50", callback_data="strategy:set:dgrid:dgrid_trend_on_variance_ratio:1.50"),
                ],
                [
                    InlineKeyboardButton("Range 1.15", callback_data="strategy:set:dgrid:dgrid_range_on_variance_ratio:1.15"),
                    InlineKeyboardButton("Range 1.05", callback_data="strategy:set:dgrid:dgrid_range_on_variance_ratio:1.05"),
                ],
                [
                    InlineKeyboardButton("Spread 2bp", callback_data="strategy:set:dgrid:dgrid_spread_bp:2"),
                    InlineKeyboardButton("Spread 8bp", callback_data="strategy:set:dgrid:dgrid_spread_bp:8"),
                    InlineKeyboardButton("Spread 15bp", callback_data="strategy:set:dgrid:dgrid_spread_bp:15"),
                ],
                [
                    InlineKeyboardButton("Min Spread 2bp", callback_data="strategy:set:dgrid:dgrid_min_spread_bp:2"),
                    InlineKeyboardButton("Max 30bp", callback_data="strategy:set:dgrid:dgrid_max_spread_bp:30"),
                    InlineKeyboardButton("Max 50bp", callback_data="strategy:set:dgrid:dgrid_max_spread_bp:50"),
                ],
                [
                    InlineKeyboardButton("Custom Trend", callback_data="strategy:input:dgrid:dgrid_trend_on_variance_ratio"),
                    InlineKeyboardButton("Custom Range", callback_data="strategy:input:dgrid:dgrid_range_on_variance_ratio"),
                ],
                [
                    InlineKeyboardButton("✍️ Custom Spread", callback_data="strategy:input:dgrid:dgrid_spread_bp"),
                ],
                # The min/max spread band had FIXED presets only (Min 2bp / Max
                # 30bp / 50bp), so there was no way to set a custom DGRID minimum
                # spread even though the prompt and the (0-50 / 1-200) validation
                # already existed — the prompt was unreachable dead code.
                [
                    InlineKeyboardButton("✍️ Custom Min Spread", callback_data="strategy:input:dgrid:dgrid_min_spread_bp"),
                    InlineKeyboardButton("✍️ Custom Max Spread", callback_data="strategy:input:dgrid:dgrid_max_spread_bp"),
                ],
            ]
        else:
            rows = [
                [
                    InlineKeyboardButton("PnL SL 0.5%", callback_data="strategy:set:dgrid:rgrid_stop_loss_pct:0.5"),
                    InlineKeyboardButton("1.0%", callback_data="strategy:set:dgrid:rgrid_stop_loss_pct:1.0"),
                ],
                [
                    InlineKeyboardButton("PnL TP 1.5%", callback_data="strategy:set:dgrid:rgrid_take_profit_pct:1.5"),
                    InlineKeyboardButton("2.0%", callback_data="strategy:set:dgrid:rgrid_take_profit_pct:2.0"),
                ],
                [
                    InlineKeyboardButton("Custom PnL SL", callback_data="strategy:input:dgrid:rgrid_stop_loss_pct"),
                    InlineKeyboardButton("Custom PnL TP", callback_data="strategy:input:dgrid:rgrid_take_profit_pct"),
                ],
            ]
    elif strategy == "rgrid":
        if section == "setup":
            rows = [
                [
                    InlineKeyboardButton("🎯 Tiny Budget Preset", callback_data="strategy:preset:rgrid:tiny"),
                    InlineKeyboardButton("Standard", callback_data="strategy:preset:rgrid:standard"),
                ],
                [
                    InlineKeyboardButton("🚀 Turbo Volume", callback_data="strategy:preset:rgrid:turbo"),
                ],
                [
                    InlineKeyboardButton("Margin $50", callback_data="strategy:set:rgrid:notional_usd:50"),
                    InlineKeyboardButton("Margin $100", callback_data="strategy:set:rgrid:notional_usd:100"),
                    InlineKeyboardButton("Margin $250", callback_data="strategy:set:rgrid:notional_usd:250"),
                ],
                [
                    InlineKeyboardButton("Lev 1x", callback_data="strategy:set:rgrid:mm_leverage_override:1"),
                    InlineKeyboardButton("3x", callback_data="strategy:set:rgrid:mm_leverage_override:3"),
                    InlineKeyboardButton("5x", callback_data="strategy:set:rgrid:mm_leverage_override:5"),
                    InlineKeyboardButton("10x", callback_data="strategy:set:rgrid:mm_leverage_override:10"),
                ],
                [
                    InlineKeyboardButton("Levels 3", callback_data="strategy:set:rgrid:levels:3"),
                    InlineKeyboardButton("Levels 5", callback_data="strategy:set:rgrid:levels:5"),
                    InlineKeyboardButton("Levels 7", callback_data="strategy:set:rgrid:levels:7"),
                ],
                [
                    InlineKeyboardButton("Spread 5bp", callback_data="strategy:set:rgrid:rgrid_spread_bp:5"),
                    InlineKeyboardButton("10bp", callback_data="strategy:set:rgrid:rgrid_spread_bp:10"),
                    InlineKeyboardButton("20bp", callback_data="strategy:set:rgrid:rgrid_spread_bp:20"),
                ],
                [
                    InlineKeyboardButton("60s", callback_data="strategy:set:rgrid:interval_seconds:60"),
                    InlineKeyboardButton("120s", callback_data="strategy:set:rgrid:interval_seconds:120"),
                ],
                # No mode toggle: Reverse Grid has exactly one engine (both legs
                # quote off the average of the buy/sell exposure prices and take
                # the break). The old "Directional ladder" option routed R-Grid to
                # the D-Grid phase switcher, which is why R-Grid sessions reported
                # "switched RGRID → GRID" — pick D-Grid if you want a switcher.
                [
                    InlineKeyboardButton("Discretion 0.06", callback_data="strategy:set:rgrid:rgrid_discretion:0.06"),
                    InlineKeyboardButton("0.12", callback_data="strategy:set:rgrid:rgrid_discretion:0.12"),
                    InlineKeyboardButton("0.25", callback_data="strategy:set:rgrid:rgrid_discretion:0.25"),
                ],
                [
                    InlineKeyboardButton("Custom Levels", callback_data="strategy:input:rgrid:levels"),
                    InlineKeyboardButton("Custom Spread", callback_data="strategy:input:rgrid:rgrid_spread_bp"),
                ],
                [
                    InlineKeyboardButton("⚡ Aggressive", callback_data="strategy:set_text:rgrid:participation_preset:aggressive"),
                    InlineKeyboardButton("Normal", callback_data="strategy:set_text:rgrid:participation_preset:normal"),
                    InlineKeyboardButton("Passive", callback_data="strategy:set_text:rgrid:participation_preset:passive"),
                ],
                [
                    InlineKeyboardButton("Duration 30m (target)", callback_data="strategy:set:rgrid:mm_duration_minutes:30"),
                    InlineKeyboardButton("✍️ Custom Duration", callback_data="strategy:input:rgrid:mm_duration_minutes"),
                ],
                [
                    InlineKeyboardButton("Custom Margin", callback_data="strategy:input:rgrid:notional_usd"),
                    InlineKeyboardButton("Custom Interval", callback_data="strategy:input:rgrid:interval_seconds"),
                ],
            ]
        elif section == "risk":
            rows = [
                [
                    InlineKeyboardButton("PnL SL 0.5%", callback_data="strategy:set:rgrid:rgrid_stop_loss_pct:0.5"),
                    InlineKeyboardButton("1.0%", callback_data="strategy:set:rgrid:rgrid_stop_loss_pct:1.0"),
                ],
                [
                    InlineKeyboardButton("PnL TP 1.5%", callback_data="strategy:set:rgrid:rgrid_take_profit_pct:1.5"),
                    InlineKeyboardButton("2.0%", callback_data="strategy:set:rgrid:rgrid_take_profit_pct:2.0"),
                ],
                [
                    InlineKeyboardButton("Custom PnL SL", callback_data="strategy:input:rgrid:rgrid_stop_loss_pct"),
                    InlineKeyboardButton("Custom PnL TP", callback_data="strategy:input:rgrid:rgrid_take_profit_pct"),
                ],
            ]
        else:
            rows = [
                [
                    # Range spans the full 0.1–10% band. RGrid follows trends, so
                    # its anchor legitimately sits far from mid — a grid-sized
                    # band would re-anchor constantly and never let a trend run.
                    # RGRID-STALE-LADDER note: on the CLASSIC ladder opt-out
                    # (fill_anchored=0) GridTradingController still caps the
                    # value to the ladder band and logs it, so a wide setting
                    # cannot strand that path. The fill-anchored default (both
                    # maker and taker-momentum) honours it verbatim.
                    InlineKeyboardButton("Reset 0.1%", callback_data="strategy:set:rgrid:rgrid_reset_threshold_pct:0.1"),
                    InlineKeyboardButton("1%", callback_data="strategy:set:rgrid:rgrid_reset_threshold_pct:1"),
                    InlineKeyboardButton("5%", callback_data="strategy:set:rgrid:rgrid_reset_threshold_pct:5"),
                    InlineKeyboardButton("10%", callback_data="strategy:set:rgrid:rgrid_reset_threshold_pct:10"),
                ],
                [
                    InlineKeyboardButton("Disc 0.06", callback_data="strategy:set:rgrid:rgrid_discretion:0.06"),
                    InlineKeyboardButton("Disc 0.10", callback_data="strategy:set:rgrid:rgrid_discretion:0.10"),
                ],
                [
                    InlineKeyboardButton("Custom Reset", callback_data="strategy:input:rgrid:rgrid_reset_threshold_pct"),
                    InlineKeyboardButton("Custom Disc", callback_data="strategy:input:rgrid:rgrid_discretion"),
                ],
            ]
    elif strategy == "mid":
        if section == "risk":
            rows = [
                [
                    InlineKeyboardButton("TP 0.5%", callback_data="strategy:set:mid:tp_pct:0.5"),
                    InlineKeyboardButton("TP 1.0%", callback_data="strategy:set:mid:tp_pct:1.0"),
                    InlineKeyboardButton("TP 2.0%", callback_data="strategy:set:mid:tp_pct:2.0"),
                ],
                [
                    InlineKeyboardButton("SL 0.25%", callback_data="strategy:set:mid:sl_pct:0.25"),
                    InlineKeyboardButton("SL 0.5%", callback_data="strategy:set:mid:sl_pct:0.5"),
                    InlineKeyboardButton("SL 1.0%", callback_data="strategy:set:mid:sl_pct:1.0"),
                ],
                [
                    InlineKeyboardButton("Custom TP", callback_data="strategy:input:mid:tp_pct"),
                    InlineKeyboardButton("Custom SL", callback_data="strategy:input:mid:sl_pct"),
                ],
            ]
        else:
            # Setup: Tiny Budget Preset, margin, spread (signed), levels, reference, bias.
            rows = [
                [
                    InlineKeyboardButton("🎯 Tiny Budget Preset", callback_data="strategy:preset:mid:tiny"),
                    InlineKeyboardButton("Standard", callback_data="strategy:preset:mid:standard"),
                ],
                [
                    InlineKeyboardButton("🚀 Turbo Volume", callback_data="strategy:preset:mid:turbo"),
                ],
                [
                    InlineKeyboardButton("Margin $50", callback_data="strategy:set:mid:notional_usd:50"),
                    InlineKeyboardButton("Margin $100", callback_data="strategy:set:mid:notional_usd:100"),
                    InlineKeyboardButton("Margin $250", callback_data="strategy:set:mid:notional_usd:250"),
                ],
                [
                    InlineKeyboardButton("Lev 1x", callback_data="strategy:set:mid:mm_leverage_override:1"),
                    InlineKeyboardButton("3x", callback_data="strategy:set:mid:mm_leverage_override:3"),
                    InlineKeyboardButton("5x", callback_data="strategy:set:mid:mm_leverage_override:5"),
                    InlineKeyboardButton("10x", callback_data="strategy:set:mid:mm_leverage_override:10"),
                ],
                [
                    InlineKeyboardButton("Tight 2bp", callback_data="strategy:set:mid:spread_bp:2"),
                    InlineKeyboardButton("Spread 5bp", callback_data="strategy:set:mid:spread_bp:5"),
                    InlineKeyboardButton("Spread 25bp", callback_data="strategy:set:mid:spread_bp:25"),
                ],
                # LADDER: restores the "Levels" control AUDIT-MID-2026-07-30 #5
                # pulled off this card as dead. It is live now — levels split
                # the side's margin into rungs stepping away from mid, same
                # total. Curve controls how size is distributed across them.
                [
                    InlineKeyboardButton("Levels 1", callback_data="strategy:set:mid:levels:1"),
                    InlineKeyboardButton("2", callback_data="strategy:set:mid:levels:2"),
                    InlineKeyboardButton("4", callback_data="strategy:set:mid:levels:4"),
                    InlineKeyboardButton("✍️", callback_data="strategy:input:mid:levels"),
                ],
                [
                    InlineKeyboardButton("Curve Flat", callback_data="strategy:set_text:mid:size_curve:flat"),
                    InlineKeyboardButton("Linear", callback_data="strategy:set_text:mid:size_curve:linear"),
                    InlineKeyboardButton("Geometric", callback_data="strategy:set_text:mid:size_curve:geometric"),
                ],
                [
                    InlineKeyboardButton("30s", callback_data="strategy:set:mid:interval_seconds:30"),
                    InlineKeyboardButton("60s", callback_data="strategy:set:mid:interval_seconds:60"),
                    InlineKeyboardButton("120s", callback_data="strategy:set:mid:interval_seconds:120"),
                ],
                [
                    InlineKeyboardButton("Short −0.5", callback_data="strategy:set:mid:directional_bias:-0.5"),
                    InlineKeyboardButton("Neutral", callback_data="strategy:set:mid:directional_bias:0"),
                    InlineKeyboardButton("Long +0.5", callback_data="strategy:set:mid:directional_bias:0.5"),
                ],
                [
                    InlineKeyboardButton("Min Spread 2bp", callback_data="strategy:set:mid:min_spread_bp:2"),
                    InlineKeyboardButton("Max 30bp", callback_data="strategy:set:mid:max_spread_bp:30"),
                    InlineKeyboardButton("Max 50bp", callback_data="strategy:set:mid:max_spread_bp:50"),
                ],
                [
                    InlineKeyboardButton("⚡ Aggressive", callback_data="strategy:set_text:mid:participation_preset:aggressive"),
                    InlineKeyboardButton("Normal", callback_data="strategy:set_text:mid:participation_preset:normal"),
                    InlineKeyboardButton("Passive", callback_data="strategy:set_text:mid:participation_preset:passive"),
                ],
                [
                    InlineKeyboardButton("Duration 30m", callback_data="strategy:set:mid:mm_duration_minutes:30"),
                    InlineKeyboardButton("2h", callback_data="strategy:set:mid:mm_duration_minutes:120"),
                    InlineKeyboardButton("✍️ Custom Duration", callback_data="strategy:input:mid:mm_duration_minutes"),
                ],
                [
                    InlineKeyboardButton("Pause: Off", callback_data="strategy:set:mid:twap_pause_move_bp:0"),
                    InlineKeyboardButton("1%", callback_data="strategy:set:mid:twap_pause_move_bp:100"),
                    InlineKeyboardButton("2%", callback_data="strategy:set:mid:twap_pause_move_bp:200"),
                    InlineKeyboardButton("✍️", callback_data="strategy:input:mid:twap_pause_move_bp"),
                ],
                [
                    InlineKeyboardButton("Custom Margin", callback_data="strategy:input:mid:notional_usd"),
                    InlineKeyboardButton("Custom Spread", callback_data="strategy:input:mid:spread_bp"),
                ],
                [
                    InlineKeyboardButton("Custom Bias", callback_data="strategy:input:mid:directional_bias"),
                ],
            ]
    elif strategy == "dn":
        if section == "setup":
            # Engine-v2 Delta Neutral knobs: per-leg size, hold duration, and how
            # many open→hold→close cycles to run. The short is strictly 1x, so
            # there is no leverage control here.
            rows = [
                [
                    InlineKeyboardButton("Size $50", callback_data="strategy:set:dn:fixed_margin_usd:50"),
                    InlineKeyboardButton("Size $100", callback_data="strategy:set:dn:fixed_margin_usd:100"),
                    InlineKeyboardButton("Size $250", callback_data="strategy:set:dn:fixed_margin_usd:250"),
                ],
                [
                    InlineKeyboardButton("Min hold 1h", callback_data="strategy:set:dn:dn_hold_seconds:3600"),
                    InlineKeyboardButton("Min hold 6h", callback_data="strategy:set:dn:dn_hold_seconds:21600"),
                    InlineKeyboardButton("Min hold 24h", callback_data="strategy:set:dn:dn_hold_seconds:86400"),
                ],
                [
                    InlineKeyboardButton("1 Cycle", callback_data="strategy:set:dn:dn_cycles:1"),
                    InlineKeyboardButton("5 Cycles", callback_data="strategy:set:dn:dn_cycles:5"),
                    InlineKeyboardButton("10 Cycles", callback_data="strategy:set:dn:dn_cycles:10"),
                ],
                [
                    InlineKeyboardButton("✍️ Custom Size", callback_data="strategy:input:dn:fixed_margin_usd"),
                    InlineKeyboardButton("✍️ Custom Hold (s)", callback_data="strategy:input:dn:dn_hold_seconds"),
                ],
            ]
        else:
            rows = [
                [
                    InlineKeyboardButton("Drift 3%", callback_data="strategy:set:dn:dn_max_drift_pct:3"),
                    InlineKeyboardButton("Drift 5%", callback_data="strategy:set:dn:dn_max_drift_pct:5"),
                    InlineKeyboardButton("Drift 10%", callback_data="strategy:set:dn:dn_max_drift_pct:10"),
                ],
                [
                    InlineKeyboardButton("Auto-Close ON", callback_data="strategy:set:dn:auto_close_on_maintenance:1"),
                    InlineKeyboardButton("Auto-Close OFF", callback_data="strategy:set:dn:auto_close_on_maintenance:0"),
                ],
            ]

    rows.append([InlineKeyboardButton("◀ Back", callback_data=f"strategy:config:{strategy}")])
    return InlineKeyboardMarkup(rows)


def _build_bro_preview_text(telegram_id: int) -> str:
    network, settings = get_user_settings(telegram_id)
    conf = settings.get("strategies", {}).get("bro", {})
    budget = float(conf.get("budget_usd", 500))
    risk_level = conf.get("risk_level", "balanced")
    max_positions = int(conf.get("max_positions", 3))
    leverage_cap = int(conf.get("leverage_cap", 5))
    tp_pct = float(conf.get("tp_pct", 2.0))
    sl_pct = float(conf.get("sl_pct", 1.5))
    min_confidence = float(conf.get("min_confidence", 0.65))
    products = conf.get("products", get_perp_products()[:6] or ["BTC", "ETH", "SOL"])
    max_loss = float(conf.get("max_loss_pct", 15))
    cycle_seconds = int(conf.get("cycle_seconds", 300))

    available_margin = 0.0
    client = get_user_readonly_client(telegram_id)
    if client:
        try:
            bal = client.get_balance()
            if bal and bal.get("exists"):
                available_margin = float((bal.get("balances", {}) or {}).get(0, 0) or 0)
                if available_margin == 0:
                    available_margin = float((bal.get("balances", {}) or {}).get("0", 0) or 0)
        except Exception:
            pass

    from src.nadobro.strategy.bot_runtime import get_user_bot_status
    bot_status = get_user_bot_status(telegram_id)
    is_running = bool(bot_status.get("running") and bot_status.get("strategy") == "bro")
    if is_running:
        status_emoji = "⏸️" if bool(bot_status.get("is_paused")) else "🟢"
        status_label = "PAUSED" if bool(bot_status.get("is_paused")) else "LIVE"
    else:
        status_emoji = "🟠"
        status_label = "READY"

    wallet_ready, _wallet_msg = ensure_active_wallet_ready(telegram_id)
    wallet_info = get_user_wallet_info(telegram_id, verify_signer=False) or {}
    wallet_addr = str(wallet_info.get("active_address") or "")
    wallet_short = "N/A"
    if wallet_addr:
        wallet_short = f"{wallet_addr[:6]}...{wallet_addr[-4:]}" if len(wallet_addr) >= 10 else wallet_addr
    account_status = "✅ Connected" if wallet_ready else "⚠️ Setup Needed"

    risk_emoji = {"conservative": "🛡️", "balanced": "⚖️", "aggressive": "🔥"}.get(risk_level, "⚖️")
    products_str = ", ".join(products)
    session_volume = float(bot_status.get("session_volume_usd") or 0.0)
    session_pnl = float((bot_status.get("bro_state") or {}).get("total_pnl") or 0.0)
    trade_count = int((bot_status.get("bro_state") or {}).get("trade_count") or 0)
    active_positions = len((bot_status.get("bro_state") or {}).get("active_positions") or [])
    warning = ""
    if not wallet_ready:
        warning = "⚠️ Open Wallet to link your 1CT signer and fund this mode\\."
    elif available_margin < budget * 0.2:
        warning = f"⚠️ Keep at least {escape_md(f'${budget * 0.2:,.2f}')} available for BRO allocations\\."

    return (
        "🧠 *Alpha Agent Dashboard*\n"
        f"Status: {status_emoji} *{escape_md(status_label)}*\n\n"
        "🔑 *Account*\n"
        f"• Status: *{escape_md(account_status)}*\n"
        f"• Wallet: `{escape_md(wallet_short)}`\n"
        f"• Balance: *{escape_md(f'${available_margin:,.2f}')}*\n\n"
        "⚙️ *Configuration*\n"
        f"• Budget: *{escape_md(f'${budget:,.0f}')}*\n"
        f"• Risk: {escape_md(risk_emoji)} *{escape_md(risk_level.upper())}*\n"
        f"• Assets: *{escape_md(products_str)}*\n"
        f"• Max Positions: *{escape_md(str(max_positions))}*\n"
        f"• Max Leverage: *{escape_md(f'{leverage_cap}x')}*\n"
        f"• TP/SL: *{escape_md(f'{tp_pct:.1f}% / {sl_pct:.1f}%')}*\n"
        f"• Min Confidence: *{escape_md(f'{min_confidence:.0%}')}*\n"
        f"• Cycle: *{escape_md(f'{cycle_seconds}s')}*\n"
        f"• Max Loss: *{escape_md(f'{max_loss:.0f}%')}*\n\n"
        "📊 *Statistics*\n"
        f"• Total Volume: *{escape_md(f'${session_volume:,.2f}')}*\n"
        f"• Trades: *{escape_md(str(trade_count))}*\n"
        f"• Open Positions: *{escape_md(str(active_positions))}*\n"
        f"• PnL: *{escape_md(f'{session_pnl:+,.2f} USD')}*\n\n"
        "ℹ️ *How it works*\n"
        "Scans supported markets, scores setups with AI and sentiment, then opens only high-confidence trades under risk guardrails\\."
        + (f"\n\n{warning}" if warning else "")
    )


def _append_mm_pretrade_breakdown(
    telegram_id: int,
    strategy_id: str,
    product: str,
    base_preview: str,
) -> str:
    """Phase 3 pre-trade card: appends a sizing/fee breakdown for MM strategies.

    Numbers come from ``mm_dashboard.build_pretrade_breakdown`` so the preview
    and the live ``/mm_status`` command stay in sync.
    """
    try:
        from src.nadobro.strategy import mm_dashboard
        from src.nadobro.venue.nado_archive import get_pair_24h_volume_usd

        network, settings = get_user_settings(telegram_id)
        conf = settings.get("strategies", {}).get(strategy_id, {})
        try:
            from src.nadobro.config import get_product_max_leverage as _gpml
            leverage = float(_gpml(product, network=network))
        except Exception:
            leverage = float(settings.get("default_leverage", 3))
        wallet_collateral = None
        try:
            # F6: read from the per-process TTL cache so we don't double-fetch
            # the balance after _build_strategy_preview_text just fetched it.
            v = _wallet_collateral_usd_from_balance(_cached_user_balance(telegram_id))
            wallet_collateral = v if v > 0 else None
        except Exception:
            wallet_collateral = None
        pair_volume = None
        if conf.get("participation_preset"):
            try:
                pid = get_product_id(product, network=network)
                if pid is not None:
                    pair_volume = get_pair_24h_volume_usd(network=network, product_id=int(pid))
            except Exception:
                pair_volume = None
        breakdown = mm_dashboard.build_pretrade_breakdown(
            strategy_id=strategy_id,
            conf=conf,
            network=network,
            product=product,
            leverage=leverage,
            wallet_collateral_usd=wallet_collateral,
            pair_24h_volume_usd=pair_volume,
        )
        lines = mm_dashboard.render_pretrade_card_lines(breakdown)
    except Exception:
        logger.warning("Failed to render pre-trade breakdown for user=%s strategy=%s", telegram_id, strategy_id, exc_info=True)
        return base_preview

    body = "\n".join(f"• {escape_md(line)}" for line in lines)
    return f"{base_preview}\n\n📐 *Pre\\-Trade Breakdown*\n{body}"


def _build_strategy_preview_text(
    telegram_id: int,
    strategy_id: str,
    product: str,
    vol_market: str | None = None,
) -> str:
    names = {
        "grid": "GRID",
        "rgrid": "Reverse GRID",
        "dgrid": "Dynamic GRID",
        "mid": "Mid Mode",
        "dn": "Delta Neutral",
        "vol": "Volume Bot",
    }
    network, settings = get_user_settings(telegram_id)
    conf = settings.get("strategies", {}).get(strategy_id, {})
    margin_usd = float(conf.get("notional_usd", 100.0))
    cycle_notional_cfg = float(conf.get("cycle_notional_usd", margin_usd))
    spread_bp = float(conf.get("spread_bp", 5.0))
    if strategy_id == "rgrid":
        spread_bp = float(conf.get("rgrid_spread_bp", conf.get("grid_spread_bp", spread_bp)))
    interval_seconds = int(conf.get("interval_seconds", 60))
    tp_pct = float(conf.get("tp_pct", 1.0))
    sl_pct = float(conf.get("sl_pct", 0.5))
    available_margin = 0.0
    mid = 0.0
    funding_rate = 0.0
    # F6: route through the per-process TTL balance cache so the immediate
    # follow-up call from _append_mm_pretrade_breakdown reuses this fetch.
    bal = _cached_user_balance(telegram_id)
    available_margin = _wallet_collateral_usd_from_balance(bal)
    client = get_user_readonly_client(telegram_id)

    # CEO directive (2026-05): MM family and Volume Perp run at per-asset MAX
    # leverage at runtime (mm_bot.py / volume_bot.py overwrite state["leverage"]
    # at cycle start). Reflect that in the preview so the dashboard shows what
    # the bot will actually use, and so the budget preflight uses the real value.
    if strategy_id in ("grid", "rgrid", "dgrid", "mid"):
        try:
            from src.nadobro.config import get_product_max_leverage as _gpml
            leverage = float(_gpml(product, network=network))
        except Exception:
            leverage = float(settings.get("default_leverage", 3))
    elif strategy_id == "vol":
        if (vol_market or "perp").lower() == "spot":
            leverage = 1.0
        else:
            try:
                from src.nadobro.config import get_product_max_leverage as _gpml
                leverage = float(_gpml(product, network=network))
            except Exception:
                leverage = 1.0
    else:
        leverage = float(settings.get("default_leverage", 3))
    if strategy_id == "dn":
        leverage = max(1.0, min(leverage, 5.0))
    wallet_for_preflight = available_margin if available_margin > 0 else None
    mm_budget_ok, mm_collateral_budget, mm_required_min_collateral, mm_min_order_notional, mm_max_quotes_est, mm_margin_per_quote_est = _mm_cycle_budget_preflight(
        strategy_id, conf, leverage, wallet_usdt=wallet_for_preflight
    )

    def _fmt_usd(value: float) -> str:
        return f"${value:,.2f}"

    dn_pair = get_dn_pair(product, network=network, client=client) if strategy_id == "dn" else {}
    if client:
        try:
            user = get_user(telegram_id)
            network = user.network_mode.value if user else "mainnet"
            vm_prev = str(vol_market or "perp").strip().lower() if strategy_id == "vol" else "perp"
            if strategy_id == "vol" and vm_prev == "spot":
                sym = normalize_volume_spot_symbol(product)
                pid = get_spot_product_id(sym, network=network, client=client)
            else:
                pid = get_product_id(product, network=network, client=client)
            if pid is not None:
                mp = client.get_market_price(pid)
                mid = float(mp.get("mid", 0) or 0.0)
                if strategy_id == "vol" and vm_prev == "spot":
                    funding_rate = 0.0
                else:
                    fr = client.get_funding_rate(pid) or {}
                    funding_rate = float(fr.get("funding_rate", 0) or 0.0)
        except Exception:
            pass

    bot_status = get_user_bot_status(telegram_id) or {}
    active_same_strategy = (
        str(bot_status.get("strategy") or "").lower() == strategy_id
        and str(bot_status.get("product") or "").upper() == str(product or "").upper()
    )
    if strategy_id == "vol":
        vm_prev = str(vol_market or "perp").strip().lower()
        st_vm = str(bot_status.get("vol_market") or "perp").strip().lower()
        active_same_strategy = active_same_strategy and st_vm == vm_prev
    if active_same_strategy and bool(bot_status.get("running")):
        status_emoji = "⏸️" if bool(bot_status.get("is_paused")) else "🟢"
        status_label = "PAUSED" if bool(bot_status.get("is_paused")) else "LIVE"
    elif active_same_strategy:
        status_emoji = "⚪"
        status_label = "STOPPED"
    else:
        status_emoji = "🟠"
        status_label = "READY"

    wallet_ready, _wallet_msg = ensure_active_wallet_ready(telegram_id)
    wallet_info = get_user_wallet_info(telegram_id, verify_signer=False) or {}
    wallet_addr = str(wallet_info.get("active_address") or "")
    wallet_short = "N/A"
    if wallet_addr:
        wallet_short = f"{wallet_addr[:6]}...{wallet_addr[-4:]}" if len(wallet_addr) >= 10 else wallet_addr
    account_status = "✅ Connected" if wallet_ready else "⚠️ Setup Needed"

    cycle_notional = (
        max(cycle_notional_cfg, margin_usd * max(1.0, leverage))
        if strategy_id in ("grid", "rgrid", "mid")
        else cycle_notional_cfg
    )
    required_margin = margin_usd if strategy_id in ("grid", "rgrid", "mid") else (cycle_notional / leverage if leverage > 0 else cycle_notional)
    inventory_soft_limit = float(conf.get("inventory_soft_limit_usd", margin_usd * 0.6))
    recommended_buffer = max(5.0, required_margin * 0.20)
    recommended_available = required_margin + (inventory_soft_limit / max(leverage, 1.0)) + recommended_buffer
    mid_str = f"${fmt_price(mid, product)}" if mid > 0 else "N/A"

    # Stats belong to the ONE strategy currently held in bot_status. Only show
    # them on that strategy's dashboard — otherwise the last run (e.g. D-Grid)
    # leaks its volume/PnL onto every other mode's dashboard. Each mode is
    # unique and shows only its own session.
    _stats_owner = str(bot_status.get("strategy") or "").lower() == strategy_id
    trades_count = int(bot_status.get("session_trade_count") or 0) if _stats_owner else 0
    session_volume = float(bot_status.get("session_volume_usd") or 0.0) if _stats_owner else 0.0
    session_fees = float(bot_status.get("session_fees_usd") or 0.0) if _stats_owner else 0.0
    session_pnl = float(bot_status.get("session_analytics_pnl_usd") or 0.0) if _stats_owner else 0.0

    # Route the MM dashboards through the SAME venue-authoritative, per-run
    # source as /status (get_live_session_snapshot) for THIS strategy's latest
    # run. PnL is per-SESSION = realized (this run, gross) + current open uPnL,
    # updated in real time — not a per-position figure. Falls back to the
    # bot_status values above when no session exists or the read fails.
    _snapshot_applied = False
    if strategy_id in ("grid", "rgrid", "dgrid", "mid"):
        try:
            from src.nadobro.models.database import get_strategy_sessions_by_user
            from src.nadobro.trading.live_session import get_live_session_snapshot

            _runs = get_strategy_sessions_by_user(
                telegram_id, strategy=strategy_id, network=network, limit=1
            )
            if _runs:
                _snap = get_live_session_snapshot(
                    telegram_id, network, _runs[0], state=conf, client=client
                )
                session_volume = float(_snap.get("volume") or 0.0)
                session_fees = float(_snap.get("fees") or 0.0)
                trades_count = int(_snap.get("fills") or 0)
                session_pnl = float(_snap.get("session_pnl") or 0.0)
                _snapshot_applied = True
        except Exception:  # pragma: no cover - dashboard must always render
            logger.debug("live-session snapshot for dashboard failed", exc_info=True)

    if strategy_id == "vol":
        # Volume is spot-only as of 2026-05. "Session margin" doubles as the
        # per-cycle notional; the bot rotates that amount through one
        # post-only buy → wait fill → post-only sell loop per cycle.
        session_margin = float(conf.get("session_margin_usd") or conf.get("fixed_margin_usd") or 100.0)
        target_volume = float(conf.get("target_volume_usd") or 10000.0)
        tp_pct_vol = float(conf.get("tp_pct") or 1.0)
        sl_pct_vol = float(conf.get("sl_pct") or 0.5)
        from src.nadobro.config import EST_FEE_RATE as _SPOT_FEE_RATE_EST
        maker_fee_rate = conf.get("vol_maker_fee_rate")
        if maker_fee_rate is None:
            try:
                from src.nadobro.venue.product_catalog import get_spot_maker_fee_rate

                maker_fee_rate = get_spot_maker_fee_rate(product, network=network)
            except Exception:
                maker_fee_rate = None
        if maker_fee_rate is None and conf.get("vol_maker_fee_bp") is not None:
            maker_fee_rate = float(conf.get("vol_maker_fee_bp") or 0.0) / 10_000.0
        if maker_fee_rate is None:
            maker_fee_rate = _SPOT_FEE_RATE_EST
        maker_fee_bp = float(maker_fee_rate) * 10_000.0
        volume_done = float(bot_status.get("volume_done_usd") or 0.0)
        volume_remaining = float(
            bot_status.get("volume_remaining_usd") or max(0.0, target_volume - volume_done)
        )
        if _stats_owner:
            session_fees = float(bot_status.get("session_fees_usd") or session_fees)
            session_pnl = float(bot_status.get("session_realized_pnl_usd") or session_pnl)
        # Each cycle pushes 2× session_margin of volume (buy leg + sell leg).
        est_cycles = max(1, int((target_volume + (2 * session_margin) - 1) // (2 * session_margin))) if session_margin > 0 else 0
        est_fees_usd = target_volume * float(maker_fee_rate)
        est_pnl_at_sl_usd = -session_margin * (sl_pct_vol / 100.0) if sl_pct_vol > 0 else 0.0
        market_label = f"{str(product).upper()} SPOT"
        phase = str(bot_status.get("vol_phase") or "idle").upper()
        warning = ""
        if not wallet_ready:
            warning = "⚠️ Open Wallet to link your 1CT signer and fund this mode\\."
        elif available_margin < session_margin:
            warning = (
                f"⚠️ Add margin before starting "
                f"\\(need {escape_md(_fmt_usd(session_margin))}\\)\\."
            )
        return (
            "🔁 *Volume Bot Dashboard*\n"
            f"Status: {status_emoji} *{status_label}*\n\n"
            "🔑 *Account*\n"
            f"• Status: *{escape_md(account_status)}*\n"
            f"• Wallet: `{escape_md(wallet_short)}`\n"
            f"• Balance: *{escape_md(_fmt_usd(available_margin))}*\n\n"
            "⚙️ *Configuration*\n"
            f"• Market: *{escape_md(market_label)}*\n"
            f"• Session margin: *{escape_md(_fmt_usd(session_margin))}*\n"
            f"• Take profit: *{escape_md(f'{tp_pct_vol:.2f}%')}*\n"
            f"• Stop loss: *{escape_md(f'{sl_pct_vol:.2f}%')}*\n"
            f"• Target volume: *{escape_md(_fmt_usd(target_volume))}*\n\n"
            "🧮 *Pre\\-flight analytics*\n"
            f"• Est\\. cycles to target: *{escape_md(str(est_cycles))}*\n"
            f"• Est\\. fees \\(maker {escape_md(f'{maker_fee_bp:.1f}bp')}\\): *{escape_md(_fmt_usd(est_fees_usd))}*\n"
            f"• Est\\. PnL if SL hits: *{escape_md(f'{est_pnl_at_sl_usd:+,.2f} USD')}*\n"
            f"• Slippage: *post\\-only \\(maker fills only\\)*\n\n"
            "📊 *Live statistics*\n"
            f"• Volume done: *{escape_md(_fmt_usd(volume_done))}* / *{escape_md(_fmt_usd(target_volume))}*\n"
            f"• Volume remaining: *{escape_md(_fmt_usd(volume_remaining))}*\n"
            f"• Fees paid: *{escape_md(_fmt_usd(session_fees))}*\n"
            f"• Realized PnL: *{escape_md(f'{session_pnl:+,.2f} USD')}*\n"
            f"• Phase: *{escape_md(phase)}*\n\n"
            "ℹ️ *How it works*\n"
            "Post\\-only spot buy at the bid → wait for fill → post\\-only sell at the ask\\. "
            "Loop repeats until target volume is reached, or the session TP/SL halts the bot at "
            f"*{escape_md(f'{tp_pct_vol:.2f}% / {sl_pct_vol:.2f}%')}* of margin\\."
            + (f"\n\n{warning}" if warning else "")
        )

    if strategy_id == "dgrid":
        levels = int(conf.get("levels", 4) or 4)
        trend_on = float(conf.get("dgrid_trend_on_variance_ratio", 1.25))
        range_on = float(conf.get("dgrid_range_on_variance_ratio", 1.15))
        min_spread = float(conf.get("dgrid_min_spread_bp", 2.0))
        max_spread = float(conf.get("dgrid_max_spread_bp", 50.0))
        phase = str(bot_status.get("dgrid_phase") or "grid").upper()
        variance = float(bot_status.get("dgrid_variance_ratio") or 0.0)
        realized_move = float(bot_status.get("dgrid_realized_move_bp") or 0.0)
        reset_bp = float(bot_status.get("dgrid_reset_threshold_bp") or 0.0)
        if _stats_owner and not _snapshot_applied:
            session_volume = float(bot_status.get("session_notional_done_usd") or session_volume)
            session_pnl = float(bot_status.get("rgrid_last_cycle_pnl_usd") or session_pnl)
        warning = ""
        if not wallet_ready:
            warning = "⚠️ Open Wallet to link your 1CT signer and fund this mode\\."
        elif available_margin < required_margin:
            warning = (
                f"⚠️ Recommended available margin {escape_md(_fmt_usd(recommended_available))} "
                f"\\(trade {escape_md(_fmt_usd(required_margin))} + buffer {escape_md(_fmt_usd(recommended_available - required_margin))}\\)\\."
            )
        if not mm_budget_ok:
            warning = (
                f"⚠️ Collateral looks tight for MM quoting \\(each resting order ~*{escape_md(_fmt_usd(mm_min_order_notional))}* notional\\)\\.\n"
                f"Budget after wallet cap: *{escape_md(_fmt_usd(mm_collateral_budget))}* · Need ~*{escape_md(_fmt_usd(mm_required_min_collateral))}* for one quote "
                f"\\(~*{escape_md(_fmt_usd(mm_margin_per_quote_est))}* est\\. margin at *{escape_md(f'{leverage:.0f}x')}*\\)\\."
            )
        return (
            "⚡ *Dynamic GRID Dashboard*\n"
            f"Status: {status_emoji} *{status_label}*\n\n"
            "🔑 *Account*\n"
            f"• Status: *{escape_md(account_status)}*\n"
            f"• Wallet: `{escape_md(wallet_short)}`\n"
            f"• Balance: *{escape_md(_fmt_usd(available_margin))}*\n\n"
            "⚙️ *Configuration*\n"
            f"• Market: *{escape_md(product)}\\-PERP*\n"
            f"• Margin \\(collateral cap\\): *{escape_md(_fmt_usd(margin_usd))}*\n"
            f"• Est\\. max resting quotes: *{escape_md(str(mm_max_quotes_est))}* \\| "
            f"*~{escape_md(_fmt_usd(mm_margin_per_quote_est))}* margin/quote \\(est\\.\\)\n"
            f"• Levels: *{escape_md(str(levels))}*\n"
            f"• Phase: *{escape_md(phase)}* \\| Variance: *{escape_md(f'{variance:.2f}')}*\n"
            f"• Realized move: *{escape_md(f'{realized_move:.1f}bp')}* \\| Reset: *{escape_md(f'{reset_bp:.1f}bp')}*\n"
            f"• Hysteresis: *{escape_md(f'{range_on:.2f} / {trend_on:.2f}')}* \\| Spread: *{escape_md(f'{min_spread:.0f}-{max_spread:.0f}bp')}*\n\n"
            "📊 *Statistics*\n"
            f"• Total Volume: *{escape_md(_fmt_usd(session_volume))}*\n"
            f"• Total Trades: *{escape_md(str(trades_count))}*\n"
            f"• Fees Paid: *{escape_md(_fmt_usd(session_fees))}*\n"
            f"• PnL: *{escape_md(f'{session_pnl:+,.2f} USD')}*\n\n"
            "ℹ️ *How it works*\n"
            "Automatically switches between GRID in range regimes and RGRID in trend regimes, while resizing spread from recent realized movement\\."
            + (f"\n\n{warning}" if warning else "")
        )

    if strategy_id == "grid":
        levels = int(conf.get("levels", 2) or 2)
        min_spread = float(conf.get("min_spread_bp", 2.0))
        max_spread = float(conf.get("max_spread_bp", 20.0))
        reset_threshold = float(conf.get("grid_reset_threshold_pct", 0.8))
        reset_timeout = int(conf.get("grid_reset_timeout_seconds", 120))
        if _stats_owner and not _snapshot_applied:
            session_volume = float(bot_status.get("session_notional_done_usd") or session_volume)
        warning = ""
        if not wallet_ready:
            warning = "⚠️ Open Wallet to link your 1CT signer and fund this mode\\."
        elif available_margin < required_margin:
            warning = (
                f"⚠️ Recommended available margin {escape_md(_fmt_usd(recommended_available))} "
                f"\\(trade {escape_md(_fmt_usd(required_margin))} + buffer {escape_md(_fmt_usd(recommended_available - required_margin))}\\)\\."
            )
        if not mm_budget_ok:
            warning = (
                f"⚠️ Collateral looks tight for MM quoting \\(each resting order ~*{escape_md(_fmt_usd(mm_min_order_notional))}* notional\\)\\.\n"
                f"Budget after wallet cap: *{escape_md(_fmt_usd(mm_collateral_budget))}* · Need ~*{escape_md(_fmt_usd(mm_required_min_collateral))}* for one quote "
                f"\\(~*{escape_md(_fmt_usd(mm_margin_per_quote_est))}* est\\. margin at *{escape_md(f'{leverage:.0f}x')}*\\)\\."
            )
        return (
            "📊 *GRID Dashboard*\n"
            f"Status: {status_emoji} *{status_label}*\n\n"
            "🔑 *Account*\n"
            f"• Status: *{escape_md(account_status)}*\n"
            f"• Wallet: `{escape_md(wallet_short)}`\n"
            f"• Balance: *{escape_md(_fmt_usd(available_margin))}*\n\n"
            "⚙️ *Configuration*\n"
            f"• Market: *{escape_md(product)}\\-PERP*\n"
            f"• Margin \\(collateral cap\\): *{escape_md(_fmt_usd(margin_usd))}*\n"
            f"• Est\\. max resting quotes: *{escape_md(str(mm_max_quotes_est))}* \\| "
            f"*~{escape_md(_fmt_usd(mm_margin_per_quote_est))}* margin/quote \\(est\\.\\)\n"
            f"• Levels: *{escape_md(str(levels))}*\n"
            f"• Spread: *{escape_md(f'{min_spread:.0f}bp - {max_spread:.0f}bp')}*\n"
            f"• Timing: *{escape_md(f'{interval_seconds}s')}*\n"
            f"• Leverage: *{escape_md(f'MAX ({leverage:.0f}x per-asset)')}*\n"
            f"• Reset: *{escape_md(f'{reset_threshold:.2f}% / {reset_timeout}s')}*\n"
            f"• TP/SL: *{escape_md(f'{tp_pct:.1f}% / {sl_pct:.1f}%')}*\n\n"
            "📊 *Statistics*\n"
            f"• Total Volume: *{escape_md(_fmt_usd(session_volume))}*\n"
            f"• Total Trades: *{escape_md(str(trades_count))}*\n"
            f"• Fees Paid: *{escape_md(_fmt_usd(session_fees))}*\n"
            f"• PnL: *{escape_md(f'{session_pnl:+,.2f} USD')}*\n"
            f"• Est\\. quote depth cap: *{escape_md(str(mm_max_quotes_est))}* resting \\(wallet \\& collateral limited\\)\n\n"
            "ℹ️ *How it works*\n"
            "Places maker-only bids and asks around the market to harvest spread\\."
            + (f"\n\n{warning}" if warning else "")
        )

    if strategy_id == "rgrid":
        levels = int(conf.get("levels", 4) or 4)
        grid_tp = float(conf.get("rgrid_take_profit_pct", conf.get("grid_take_profit_pct", tp_pct)))
        max_loss_pct = float(conf.get("rgrid_stop_loss_pct", conf.get("grid_stop_loss_pct", sl_pct)))
        discretion = float(conf.get("rgrid_discretion", conf.get("grid_discretion", 0.06)))
        reset_threshold = float(conf.get("rgrid_reset_threshold_pct", conf.get("grid_reset_threshold_pct", 0.2)))
        reset_timeout = int(conf.get("rgrid_reset_timeout_seconds", conf.get("grid_reset_timeout_seconds", 120)))
        if _stats_owner and not _snapshot_applied:
            session_volume = float(bot_status.get("session_notional_done_usd") or session_volume)
            session_pnl = float(bot_status.get("rgrid_last_cycle_pnl_usd") or session_pnl)
        warning = ""
        if not wallet_ready:
            warning = "⚠️ Open Wallet to link your 1CT signer and fund this mode\\."
        elif available_margin < required_margin:
            warning = (
                f"⚠️ Recommended available margin {escape_md(_fmt_usd(recommended_available))} "
                f"\\(trade {escape_md(_fmt_usd(required_margin))} + buffer {escape_md(_fmt_usd(recommended_available - required_margin))}\\)\\."
            )
        if not mm_budget_ok:
            warning = (
                f"⚠️ Collateral looks tight for MM quoting \\(each resting order ~*{escape_md(_fmt_usd(mm_min_order_notional))}* notional\\)\\.\n"
                f"Budget after wallet cap: *{escape_md(_fmt_usd(mm_collateral_budget))}* · Need ~*{escape_md(_fmt_usd(mm_required_min_collateral))}* for one quote "
                f"\\(~*{escape_md(_fmt_usd(mm_margin_per_quote_est))}* est\\. margin at *{escape_md(f'{leverage:.0f}x')}*\\)\\."
            )
        return (
            "🧮 *Reverse GRID Dashboard*\n"
            f"Status: {status_emoji} *{status_label}*\n\n"
            "🔑 *Account*\n"
            f"• Status: *{escape_md(account_status)}*\n"
            f"• Wallet: `{escape_md(wallet_short)}`\n"
            f"• Balance: *{escape_md(_fmt_usd(available_margin))}*\n\n"
            "⚙️ *Configuration*\n"
            f"• Market: *{escape_md(product)}\\-PERP*\n"
            f"• Margin \\(collateral cap\\): *{escape_md(_fmt_usd(margin_usd))}*\n"
            f"• Est\\. max resting quotes: *{escape_md(str(mm_max_quotes_est))}* \\| "
            f"*~{escape_md(_fmt_usd(mm_margin_per_quote_est))}* margin/quote \\(est\\.\\)\n"
            f"• Levels: *{escape_md(str(levels))}*\n"
            f"• Spread: *{escape_md(f'{spread_bp:.0f}bp')}*\n"
            f"• Timing: *{escape_md(f'{interval_seconds}s')}*\n"
            f"• Leverage: *{escape_md(f'MAX ({leverage:.0f}x per-asset)')}*\n"
            f"• Reset: *{escape_md(f'{reset_threshold:.2f}% / {reset_timeout}s')}*\n"
            f"• Discretion: *{escape_md(f'{discretion:.2f}')}*\n"
            f"• PnL SL/TP: *{escape_md(f'{max_loss_pct:.2f}% / {grid_tp:.2f}%')}*\n\n"
            "📊 *Statistics*\n"
            f"• Total Volume: *{escape_md(_fmt_usd(session_volume))}*\n"
            f"• Total Trades: *{escape_md(str(trades_count))}*\n"
            f"• Fees Paid: *{escape_md(_fmt_usd(session_fees))}*\n"
            f"• PnL: *{escape_md(f'{session_pnl:+,.2f} USD')}*\n"
            f"• Est\\. quote depth cap: *{escape_md(str(mm_max_quotes_est))}* resting \\(wallet \\& collateral limited\\)\n\n"
            "ℹ️ *How it works*\n"
            "Anchors to exposure and places buy above / sell below to capture continuation\\."
            + (f"\n\n{warning}" if warning else "")
        )

    if strategy_id == "mid":
        # Tolerates legacy TEXT bias values ("long_bias"/"neutral"/...) — a bare
        # float() would raise and break the dashboard render.
        from src.nadobro.quant.mm_quote_math import _resolve_directional_bias_value
        directional_bias = _resolve_directional_bias_value(conf.get("directional_bias"))
        if directional_bias > 0:
            bias_label = f"LONG {directional_bias:+.2f}"
        elif directional_bias < 0:
            bias_label = f"SHORT {directional_bias:+.2f}"
        else:
            bias_label = "NEUTRAL"
        warning = ""
        if not wallet_ready:
            warning = "⚠️ Open Wallet to link your 1CT signer and fund this mode\\."
        elif available_margin < required_margin:
            warning = (
                f"⚠️ Recommended available margin {escape_md(_fmt_usd(recommended_available))} "
                f"\\(trade {escape_md(_fmt_usd(required_margin))} + buffer {escape_md(_fmt_usd(recommended_available - required_margin))}\\)\\."
            )
        if not mm_budget_ok:
            warning = (
                f"⚠️ Collateral looks tight for MM quoting \\(each resting order ~*{escape_md(_fmt_usd(mm_min_order_notional))}* notional\\)\\.\n"
                f"Budget after wallet cap: *{escape_md(_fmt_usd(mm_collateral_budget))}* · Need ~*{escape_md(_fmt_usd(mm_required_min_collateral))}* for one quote "
                f"\\(~*{escape_md(_fmt_usd(mm_margin_per_quote_est))}* est\\. margin at *{escape_md(f'{leverage:.0f}x')}*\\)\\."
            )
        return (
            "📈 *Mid Mode Dashboard*\n"
            f"Status: {status_emoji} *{status_label}*\n\n"
            "🔑 *Account*\n"
            f"• Status: *{escape_md(account_status)}*\n"
            f"• Wallet: `{escape_md(wallet_short)}`\n"
            f"• Balance: *{escape_md(_fmt_usd(available_margin))}*\n\n"
            "⚙️ *Configuration*\n"
            f"• Market: *{escape_md(product)}\\-PERP*\n"
            f"• Margin \\(collateral cap\\): *{escape_md(_fmt_usd(margin_usd))}*\n"
            f"• Est\\. max resting quotes: *{escape_md(str(mm_max_quotes_est))}* \\| "
            f"*~{escape_md(_fmt_usd(mm_margin_per_quote_est))}* margin/quote \\(est\\.\\)\n"
            f"• Spread: *{escape_md(f'{spread_bp:.0f}bp')}*\n"
            f"• Directional bias: *{escape_md(bias_label)}*\n"
            f"• Timing: *{escape_md(f'{interval_seconds}s')}*\n"
            f"• Leverage: *{escape_md(f'MAX ({leverage:.0f}x per-asset)')}*\n"
            f"• TP/SL: *{escape_md(f'{tp_pct:.2f}% / {sl_pct:.2f}%')}*\n\n"
            "📊 *Statistics*\n"
            f"• Total Volume: *{escape_md(_fmt_usd(session_volume))}*\n"
            f"• Total Trades: *{escape_md(str(trades_count))}*\n"
            f"• Fees Paid: *{escape_md(_fmt_usd(session_fees))}*\n"
            f"• PnL: *{escape_md(f'{session_pnl:+,.2f} USD')}*\n\n"
            "ℹ️ *How it works*\n"
            "Quotes a two\\-sided market around mid \\(maker\\-only\\) to capture the "
            "spread; optional directional bias skews the quotes\\."
            + (f"\n\n{warning}" if warning else "")
        )

    funding_bias = "FAVORABLE" if funding_rate > 0.000001 else "UNFAVORABLE"
    auto_close = "ON" if float(conf.get("auto_close_on_maintenance", 1) or 0) >= 0.5 else "OFF"
    # Engine-v2 DN settings (what the controller actually runs).
    dn_leg_size = float(conf.get("fixed_margin_usd", margin_usd) or margin_usd)
    dn_hold_label = _fmt_hold_duration(int(conf.get("dn_hold_seconds", 3600) or 3600))
    dn_cycles = int(conf.get("dn_cycles", 1) or 1)
    dn_cycles_label = str(dn_cycles) + (
        "" if dn_cycles == 1 else f" · {int(conf.get('dn_cycle_gap_seconds', 30) or 0)}s gap"
    )
    dn_spot_symbol = str(dn_pair.get("spot_symbol") or product).upper()
    dn_perp_symbol = str(dn_pair.get("perp_symbol") or f"{product}-PERP").upper()
    dn_market_label = f"{dn_spot_symbol} spot / {dn_perp_symbol}"
    # Economics over the configured hold × cycles. The short earns funding when
    # the daily rate is positive; it accrues on the perp notional (≈ per-leg size
    # at 1x) pro-rated over the hold, summed across cycles. Sign is preserved, so
    # an unfavorable rate shows as a negative contribution, not a phantom gain.
    # (The old preview multiplied |rate| × margin × 3 — wrong magnitude AND it
    # hid the sign; funding is a daily rate, not a 3-interval one.)
    from src.nadobro.config import EST_FEE_RATE
    dn_hold_seconds = int(conf.get("dn_hold_seconds", 3600) or 3600)
    dn_est_funding = funding_rate * (dn_hold_seconds / 86400.0) * dn_leg_size * dn_cycles
    # Round-trip taker fees: open + close on BOTH legs each cycle (4 fills × per-leg notional).
    dn_est_fees = 4.0 * dn_leg_size * EST_FEE_RATE * dn_cycles
    dn_est_net = dn_est_funding - dn_est_fees
    dn_net_dot = "🟢" if dn_est_net >= 0 else "🟠"
    warning = ""
    if not wallet_ready:
        warning = "⚠️ Open Wallet to link your 1CT signer and fund this mode\\."
    elif available_margin < required_margin:
        warning = f"⚠️ Add margin before starting \\(need {escape_md(_fmt_usd(required_margin))}\\)\\."
    elif strategy_id == "dn" and dn_pair and not bool(dn_pair.get("entry_allowed", True)):
        warning = f"⚠️ {escape_md(str(dn_pair.get('entry_block_reason') or 'This DN pair is not currently tradable.'))}"
    elif strategy_id == "dn" and not dn_pair:
        warning = "⚠️ This DN pair is not currently available\\."
    return (
        "⚖️ *Delta Neutral Dashboard*\n"
        f"Strategy Status: {status_emoji} *{status_label}*\n\n"
        "📊 *Your Stats*\n"
        f"• Volume Traded: *{escape_md(_fmt_usd(session_volume))}*\n"
        f"• Positions Created: *{escape_md(str(trades_count))}*\n"
        f"• Fees Paid: *{escape_md(_fmt_usd(session_fees))}*\n"
        f"• Funding: *{escape_md(f'{funding_rate:.6f}')}*\n\n"
        "🔑 *Exchange Account*\n"
        f"• Status: *{escape_md(account_status)}*\n"
        f"• Wallet: `{escape_md(wallet_short)}`\n"
        f"• Balance: *{escape_md(_fmt_usd(available_margin))}*\n\n"
        "⚙️ *Current Settings*\n"
        f"• Market: *{escape_md(dn_market_label)}*\n"
        # Last screen before capital is committed — it must show the size that
        # will ACTUALLY be deployed, not the one the user typed. See
        # _leg_size_display / SPOT-EXIT-GUARANTEE.
        f"• Size \\(per leg\\): {_leg_size_display(dn_leg_size, dn_spot_symbol, network)}\n"
        f"• Short Leverage: *1x* \\(fixed\\)\n"
        f"• Min hold: *{escape_md(dn_hold_label)}*\n"
        f"• Cycles: *{escape_md(dn_cycles_label)}*\n"
        f"• Auto\\-close on maintenance: *{escape_md(auto_close)}*\n\n"
        "ℹ️ *How it works*\n"
        "Buys spot \\+ 1x\\-shorts the same perp, holds for *at least* the min hold, then keeps the "
        "hedge open while funding stays favorable and closes BOTH legs the moment funding flips "
        "\\(or you close it\\)\\. Farms spot \\+ perp volume and funding while staying delta neutral\\.\n"
        f"Funding now: *{escape_md(funding_bias)}* \\({escape_md(f'{funding_rate * 100:+.4f}')}%/day\\)\n"
        f"Est\\. over {escape_md(dn_hold_label)} × {escape_md(str(dn_cycles))}: "
        f"funding *{escape_md(_fmt_usd(dn_est_funding))}* · fees *{escape_md(_fmt_usd(dn_est_fees))}* · "
        f"net {dn_net_dot} *{escape_md(_fmt_usd(dn_est_net))}*"
        + (f"\n\n{warning}" if warning else "")
    )
