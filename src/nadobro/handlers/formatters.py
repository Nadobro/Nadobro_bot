import re
import time
import logging
from decimal import Decimal, InvalidOperation, ROUND_DOWN, localcontext
from typing import Optional
from src.nadobro.config import get_product_name, PRODUCTS
from src.nadobro.i18n import get_active_language, localize_text
from src.nadobro.handlers import ui as _ui

logger = logging.getLogger(__name__)


def _loc(text):
    return localize_text(text, get_active_language())


def _loc_md(text):
    return escape_md(_loc(text))


# ``escape_md`` / ``escape_md_code`` now live in ``handlers/ui.py``, the leaf
# that owns presentation primitives. Re-exported here so the 15 modules doing
# ``from ...formatters import escape_md`` keep working unchanged.
escape_md = _ui.escape_md
escape_md_code = _ui.escape_md_code


def format_ai_response(text: str) -> str:
    """Convert LLM markdown output to Telegram MarkdownV2.

    Preserves bold, bullet points, numbered lists, and emojis while
    escaping everything else for safe Telegram rendering.  Falls back
    to full ``escape_md`` on any error.
    """
    if not text:
        return ""
    try:
        return _md_to_tg_md2(text)
    except Exception:
        logger.warning("format_ai_response fallback to escape_md", exc_info=True)
        return escape_md(text)


# ── Telegram MarkdownV2 converter ────────────────────────────────────

# Characters that Telegram requires escaped outside of formatting spans.
_TG_SPECIAL = set(r'_[]()~`>#+-=|{}.!')

def _escape_tg(s: str) -> str:
    """Escape for MarkdownV2 but leave already-escaped chars alone."""
    out: list[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == '\\' and i + 1 < len(s):
            # already escaped – pass through
            out.append(ch)
            out.append(s[i + 1])
            i += 2
            continue
        if ch in _TG_SPECIAL:
            out.append('\\')
        out.append(ch)
        i += 1
    return "".join(out)


def _escape_tg_url(url: str) -> str:
    """Escape URL chars required inside Telegram MarkdownV2 link targets."""
    return (url or "").replace("\\", "\\\\").replace(")", "\\)")


def _escape_and_convert_inline(raw: str) -> str:
    """Process inline markdown on a RAW (unescaped) string.

    Finds links, **bold**, and `code` spans first, escapes everything else.
    This avoids the problem of escaping destroying markdown markers.
    """
    result: list[str] = []
    i = 0
    while i < len(raw):
        # Markdown link: [label](https://...)
        if raw[i] == '[':
            close_label = raw.find(']', i + 1)
            if close_label != -1 and close_label + 1 < len(raw) and raw[close_label + 1] == '(':
                close_url = raw.find(')', close_label + 2)
                if close_url != -1:
                    label = _escape_tg(raw[i + 1:close_label])
                    url = _escape_tg_url(raw[close_label + 2:close_url])
                    if label and url:
                        result.append(f'[{label}]({url})')
                        i = close_url + 1
                        continue
        # Bold: **...**
        if raw[i:i+2] == '**':
            end = raw.find('**', i + 2)
            if end != -1:
                inner = _escape_tg(raw[i+2:end])
                result.append(f'*{inner}*')
                i = end + 2
                continue
        # Inline code: `...`
        if raw[i] == '`':
            end = raw.find('`', i + 1)
            if end != -1:
                inner = raw[i+1:end]  # code content not escaped in TG
                result.append(f'`{inner}`')
                i = end + 1
                continue
        # Regular character — escape if special
        ch = raw[i]
        if ch == '\\' and i + 1 < len(raw):
            result.append(ch)
            result.append(raw[i + 1])
            i += 2
            continue
        if ch in _TG_SPECIAL:
            result.append('\\')
        result.append(ch)
        i += 1
    return "".join(result)


def _md_to_tg_md2(text: str) -> str:
    """Convert standard markdown produced by the LLM into Telegram MarkdownV2."""
    lines = text.split('\n')
    out: list[str] = []

    for raw_line in lines:
        line = raw_line.rstrip()

        # Empty line → preserve spacing
        if not line:
            out.append("")
            continue

        # Bullet point: - text  or • text
        bullet_match = re.match(r'^(\s*)([-•])\s+(.+)$', line)
        if bullet_match:
            indent = bullet_match.group(1)
            body = _escape_and_convert_inline(bullet_match.group(3))
            out.append(f'{indent}\\- {body}')
            continue

        # Numbered list: 1. text
        num_match = re.match(r'^(\s*)(\d+)\.\s+(.+)$', line)
        if num_match:
            indent = num_match.group(1)
            num = _escape_tg(num_match.group(2))
            body = _escape_and_convert_inline(num_match.group(3))
            out.append(f'{indent}{num}\\. {body}')
            continue

        # Section header: ### text or ## text — render as bold line
        header_match = re.match(r'^#{1,4}\s+(.+)$', line)
        if header_match:
            body = _escape_tg(header_match.group(1))
            out.append(f'*{body}*')
            continue

        # Regular line — process inline formatting on raw text
        out.append(_escape_and_convert_inline(line))

    return '\n'.join(out)


def fmt_bro_answer_card(
    answer: str,
    mode: str | None = None,
    *,
    sources: list[str] | None = None,
) -> str:
    """Render Trading Bro answers as a compact Telegram card."""
    mode_titles = {
        "strategy_design": "Strategy Builder",
        "educational_guide": "Trading Guide",
        "debugging": "Debug Desk",
        "market_analysis": "Market Read",
        "market_call": "Market Call",
        "product_support": "Nadobro Help",
        "casual": "Trading Bro",
        "morning_brief": "Morning Brief",
    }
    title = mode_titles.get(str(mode or ""), "Trading Bro")
    title_emoji = "📊" if mode == "morning_brief" else "🧠"
    body = format_ai_response((answer or "").strip())
    lines = [
        f"{title_emoji} *{escape_md(title)}*",
        md2_rule(),
        "",
    ]
    if body:
        lines.append(body)
    else:
        lines.append(escape_md("I couldn't generate an answer. Try again."))

    if sources:
        unique = []
        seen: set[str] = set()
        for src in sources:
            if not src:
                continue
            key = src.strip()
            if key.lower() in seen:
                continue
            seen.add(key.lower())
            unique.append(key)
            if len(unique) >= 6:
                break
        if unique:
            lines.append("")
            lines.append(f"_{escape_md('Sources: ' + ', '.join(unique))}_")
    return "\n".join(lines)


def _calc_position_pnl(position: dict, current_price: float) -> Optional[float]:
    """Unrealized PnL aligned with Nado: prefer exchange-reported uPnL, else v_quote settlement."""
    raw = position.get("unrealized_pnl")
    if raw is not None:
        try:
            return float(raw)
        except Exception:
            pass

    v_quote = position.get("v_quote_balance")
    signed_amount = position.get("signed_amount")
    if v_quote is not None and signed_amount is not None and current_price:
        try:
            return float(v_quote) + float(signed_amount) * float(current_price)
        except Exception:
            pass

    entry = float(position.get("price", 0) or 0)
    amount = abs(float(position.get("amount", 0) or 0))
    if not current_price or not entry or not amount:
        return None
    side = str(position.get("side", "LONG")).upper()
    if side == "LONG":
        return (float(current_price) - entry) * amount
    return (entry - float(current_price)) * amount


def _has_exchange_unrealized_pnl(position: dict) -> bool:
    """True when the position dict carries a parseable uPnL from the exchange."""
    raw = position.get("unrealized_pnl")
    if raw is None:
        return False
    try:
        float(raw)
        return True
    except Exception:
        return False


def fmt_price(price, product="BTC"):
    if price is None or price == 0:
        return "N/A"
    product_upper = str(product).upper().replace("-PERP", "")
    if product_upper in ("BTC", "ETH", "BNB"):
        return f"{price:,.2f}"
    if price >= 1:
        return f"{price:,.2f}"
    return f"{price:,.4f}"



def fmt_positions(positions, prices=None, mode_label: str | None = None):
    if not positions:
        header = [_ui_header("Open Positions", icon="📋"), md2_rule()]
        if mode_label:
            header.append(_ui_section("Snapshot", [f"└ 🌐 *{_loc('Mode:')}* {escape_md(mode_label)}"]))
        return "\n".join(header) + "\n\n" + _loc("No open positions\\.")

    lines = [
        _ui_header("Open Positions", icon="📋"),
        md2_rule(),
        "",
    ]
    if mode_label:
        lines.append(_ui_section("Snapshot", [f"├ 🌐 *{_loc('Mode:')}* {escape_md(mode_label)}", f"└ 📌 *{_loc('Total')}:* {escape_md(str(len(positions)))}"]))
    else:
        lines.append(_ui_section("Snapshot", [f"└ 📌 *{_loc('Total')}:* {escape_md(str(len(positions)))}"]))
    lines.append("")
    any_estimated_pnl = False

    for i, p in enumerate(positions, 1):
        side = p.get("side", "LONG")
        side_emoji = "🟢" if side == "LONG" else "🔴"
        amount = abs(p.get("amount", 0))
        pname = p.get("product_name", "???")
        entry = float(p.get("price", 0) or 0)
        base = pname.replace("-PERP", "")

        lines.append(f"{side_emoji} *{escape_md(side)}* {escape_md(pname)}")

        current = 0.0
        if prices and base in prices:
            current = float(prices[base].get("mid", 0) or 0)

        mark_str = f"${fmt_price(current, base)}" if current else "—"

        pnl = None
        if current:
            pnl = _calc_position_pnl(p, current)
        if pnl is not None:
            if not _has_exchange_unrealized_pnl(p):
                any_estimated_pnl = True
            pnl_str = f"+${pnl:,.2f}" if pnl >= 0 else f"-${abs(pnl):,.2f}"
            pnl_emoji = "🟢" if pnl >= 0 else "🔴"
        else:
            pnl_emoji = ""
            pnl_str = "—"

        liq_raw = p.get("liquidation_price")
        if liq_raw is not None:
            try:
                liq_v = float(liq_raw)
                if liq_v > 0:
                    liq_str = f"${fmt_price(liq_v, base)}"
                else:
                    liq_str = "—"
            except Exception:
                liq_str = "—"
        else:
            liq_str = "—"

        lines.append(
            f"{_loc('Size')}: *{escape_md(f'{amount:.4f}')} {escape_md(base)}* \\| "
            f"{_loc('uPnL')}: {pnl_emoji} *{escape_md(pnl_str)}*"
        )
        lines.append(
            f"{_loc('Entry')}: {escape_md(f'${entry:,.2f}')} \\| "
            f"{_loc('Mark')}: {escape_md(mark_str)} \\| "
            f"{_loc('Liq')}: {escape_md(liq_str)}"
        )
        if i < len(positions):
            lines.extend(["", _ui.subrule(), ""])

    if any_estimated_pnl:
        lines.append("")
        lines.append(
            _loc(
                "ℹ️ PnL is estimated from mark vs\\. entry when the exchange does not report uPnL for this position\\."
            )
        )
    return "\n".join(lines)


def fmt_balance(balance_data, wallet_addr=None):
    lines = [
        _ui_header("Wallet Vault Balance", icon="💰"),
        md2_rule(),
        "",
    ]

    if not balance_data or not balance_data.get("exists"):
        lines.append(_loc("⚠️ No subaccount found\\."))
        lines.append("")
        if wallet_addr:
            lines.append(_loc("Deposit ≥ \\$5 USDT0 to:"))
            lines.append(f"`{escape_md(wallet_addr)}`")
            lines.append("")
            lines.append(f"🚰 {_loc('Faucet')}: {escape_md('https://testnet.nado.xyz/portfolio/faucet')}")
        return "\n".join(lines)

    balances = balance_data.get("balances", {}) or {}
    usdt = balances.get(0, balances.get("0", 0))
    lines.append(f"💵 *USDT0:* {escape_md(f'${usdt:,.2f}')}")

    for pid_raw, bal in balances.items():
        try:
            pid = int(pid_raw)
        except (TypeError, ValueError):
            continue
        if pid != 0 and bal != 0:
            pname = get_product_name(pid)
            lines.append(f"  └ {escape_md(pname)}: {escape_md(f'{bal:.6f}')}")

    if wallet_addr:
        lines.append("")
        lines.append(f"📋 {_loc('Address')}: `{escape_md(wallet_addr)}`")

    return "\n".join(lines)


def fmt_trade_preview(action, product, size, price, leverage=1, est_margin=None):
    action_upper = action.upper()
    emoji = "🟢" if "LONG" in action_upper else "🔴"

    if est_margin is None and price:
        est_margin = (size * price) / leverage if leverage > 1 else size * price

    lines = [
        f"{emoji} *{_loc_md('Trade Preview')}*",
        escape_md("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"),
        "",
        f"📌 *{_loc_md('Action')}:* {escape_md(action_upper)}",
        f"🪙 *{_loc_md('Product')}:* {escape_md(product)}\\-PERP",
        f"📏 *{_loc_md('Size')}:* {escape_md(str(size))}",
        f"💲 *{_loc_md('Price')}:* {escape_md(f'~${fmt_price(price, product)}')}",
        f"⚡ *{_loc_md('Leverage')}:* {escape_md(f'{leverage}x')}",
    ]

    if est_margin is not None:
        lines.append(f"💰 *{_loc_md('Est. Margin')}:* {escape_md(f'${est_margin:,.2f}')}")

    lines.append("")
    lines.append(escape_md(_loc("Look good? Hit confirm.")))

    return "\n".join(lines)


def build_trade_preview_text(
    action: str,
    product: str,
    size: float,
    price: float,
    leverage: int = 1,
    est_margin=None,
    tp=None,
    sl=None,
) -> str:
    preview = fmt_trade_preview(action, product, size, price, leverage, est_margin)
    if tp:
        preview += f"\n\n📈 *{_loc_md('Take Profit')}:* {escape_md(str(tp))}"
    if sl:
        preview += f"\n📉 *{_loc_md('Stop Loss')}:* {escape_md(str(sl))}"
    return preview


def humanize_exchange_error(error) -> str:
    """Turn raw JSON / engine errors into user-friendly text (Telegram-safe)."""
    if error is None:
        return ""
    s = str(error).strip()
    low = s.lower()
    if "2070" in s or "maximum open interest" in low:
        return (
            "This market has hit its maximum open interest on the exchange (error 2070). "
            "You cannot open new positions until capacity frees up; you can still close or reduce positions."
        )
    try:
        import json

        j = json.loads(s)
        if isinstance(j, dict):
            code = j.get("error_code")
            msg = str(j.get("error") or "")
            if code == 2070 or "maximum open interest" in msg.lower():
                return humanize_exchange_error(msg or s)
    except Exception:
        pass
    return s


def fmt_trade_result(result):
    if result.get("success"):
        r_price = result.get("price", 0)
        r_product = result.get("product", "BTC")
        price_str = "$" + fmt_price(r_price, r_product)
        order_type_u = str(result.get("type", "MARKET") or "MARKET").upper()
        status_u = str(result.get("status", "") or "").lower()
        is_limit_pending = order_type_u == "LIMIT" and status_u == "pending"
        header = _loc("✅ *Limit order's in\\.*") if is_limit_pending else _loc("✅ *Filled\\.*")
        price_label = _loc_md("Limit price") if is_limit_pending else _loc_md("Fill price")
        lines = [
            header,
            escape_md("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"),
            "",
            f"📌 *{_loc_md('Side')}:* {escape_md(result.get('side', '?'))}",
            f"🪙 *{_loc_md('Product')}:* {escape_md(result.get('product', '?'))}",
            f"📏 *{_loc_md('Size')}:* {escape_md(str(result.get('size', '?')))}",
            f"💲 *{price_label}:* {escape_md(price_str)}",
            "",
            f"🌐 *{_loc_md('Network:')}* {escape_md(result.get('network', '?'))}",
        ]
        if result.get("fee") is not None:
            try:
                fee_v = float(result.get("fee") or 0)
                lines.insert(-2, f"🧾 *{_loc_md('Fee')}:* {escape_md(f'${fee_v:,.4f}')}")
            except Exception:
                pass
        if result.get("tp_requested"):
            if result.get("tp_set"):
                lines.append(f"📈 *{_loc_md('Take Profit')}:* {escape_md(str(result.get('tp_price')))}")
            else:
                lines.append(f"⚠️ *{_loc_md('Take Profit')}:* {escape_md(str(result.get('tp_error', _loc('Failed to place TP order.'))))}")
        if result.get("sl_requested"):
            if result.get("sl_armed"):
                lines.append(f"🛡 *{_loc_md('Stop Loss')}:* {escape_md(str(result.get('sl_price')))}")
            else:
                lines.append(f"⚠️ *{_loc_md('Stop Loss')}:* {escape_md(str(result.get('sl_error', _loc('Failed to arm SL rule.'))))}")
        order_type = result.get("type", "MARKET")
        if order_type != "MARKET":
            lines.insert(3, f"📋 *{_loc_md('Type')}:* {escape_md(order_type)}")
        return "\n".join(lines)
    else:
        error = humanize_exchange_error(result.get("error", _loc("Unknown error")))
        return f"❌ *{_loc('Trade Failed')}*\n\n{escape_md(error)}"


def fmt_bracket_result(result: dict) -> str:
    """Format TP/SL placement on an existing position (natural-language commands)."""
    if result.get("success"):
        lines = [
            _loc("✅ *TP/SL updated*"),
            escape_md("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"),
            "",
            f"🪙 *{_loc_md('Product')}:* {escape_md(result.get('product', '?'))}",
            f"🌐 *{_loc_md('Network:')}* {escape_md(result.get('network', '?'))}",
        ]
        if result.get("tp_requested"):
            if result.get("tp_set"):
                lines.append(f"📈 *{_loc_md('Take Profit')}:* {escape_md(str(result.get('tp_price')))}")
            else:
                lines.append(
                    f"⚠️ *{_loc_md('Take Profit')}:* {escape_md(str(result.get('tp_error', _loc('Failed to place TP order.'))))}"
                )
        if result.get("sl_requested"):
            if result.get("sl_armed"):
                lines.append(f"🛡 *{_loc_md('Stop Loss')}:* {escape_md(str(result.get('sl_price')))}")
            else:
                lines.append(
                    f"⚠️ *{_loc_md('Stop Loss')}:* {escape_md(str(result.get('sl_error', _loc('Failed to arm SL rule.'))))}"
                )
        return "\n".join(lines)
    err = humanize_exchange_error(result.get("error", _loc("Unknown error")))
    return f"❌ *{_loc('Trade Failed')}:* TP/SL\n\n{escape_md(err)}"


def fmt_limit_close_result(result: dict) -> str:
    """Reduce-only limit order used to close or scale out of a position."""
    if result.get("success"):
        price_str = "$" + fmt_price(float(result.get("limit_price", 0)), result.get("product", "BTC"))
        lines = [
            _loc("✅ *Limit close placed*"),
            escape_md("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"),
            "",
            f"🪙 *{_loc_md('Product')}:* {escape_md(result.get('product', '?'))}",
            f"📏 *{_loc_md('Size')}:* {escape_md(str(result.get('size', '?')))}",
            f"💲 *{_loc_md('Limit')}:* {escape_md(price_str)}",
            f"📌 *{_loc_md('Side')}:* {escape_md(result.get('side', '?'))}",
            "",
            f"🌐 *{_loc_md('Network:')}* {escape_md(result.get('network', '?'))}",
        ]
        return "\n".join(lines)
    err = humanize_exchange_error(result.get("error", _loc("Unknown error")))
    return f"❌ *{_loc('Trade Failed')}:* limit close\n\n{escape_md(err)}"


def fmt_wallet_info(wallet_info):
    if not wallet_info:
        return _loc("💼 *Wallet Vault*") + "\n" + md2_rule() + "\n\n" + _loc("Wallet not found\\. Use /start first\\.")

    net = wallet_info.get("network", "testnet")
    net_emoji = "🧪" if net == "testnet" else "🌐"
    signer_linked = bool(wallet_info.get("is_linked"))

    lines = [
        _loc("💼 *Wallet Vault*"),
        md2_rule(),
        "",
        f"📊 *{_loc('Network:')}* {net_emoji} {escape_md(net.upper())}",
    ]

    addr = wallet_info.get("active_address")
    if addr:
        lines.extend(["", f"*{_loc('Main Wallet')}*"])
        lines.append(f"`{escape_md(addr)}`")
    else:
        lines.extend(["", f"*{_loc('Main Wallet')}*"])
        lines.append(f"📋 *{_loc('Main Wallet:')}* {_loc('Not set')}")

    if signer_linked:
        lines.extend(["", f"*{_loc('Linked Signer')}*"])
        lines.append(f"`{escape_md(wallet_info['linked_signer_address'])}`")

    verification = wallet_info.get("signer_verification")
    if verification:
        lines.extend(["", f"*{_loc('Verification')}*"])
        if verification.get("error"):
            lines.append(f"⚠️ *{_loc('Signer Check')}:* {escape_md(_loc('Could not verify') + ': ' + str(verification['error'])[:60])}")
        elif verification.get("verified"):
            lines.append(f"✅ *{_loc('Signer Check')}:* {_loc('1CT key is linked on Nado')}")
        elif verification.get("current_signer"):
            current = verification["current_signer"]
            expected = verification.get("expected_signer", "")
            lines.append(f"❌ *{_loc('Signer Check')}:* {_loc('MISMATCH')}")
            lines.append(f"  {_loc('Exchange has')}: `{escape_md(current[:10])}\\.\\.\\.$`")
            lines.append(f"  {_loc('Bot expects')}: `{escape_md(expected[:10])}\\.\\.\\.$`")
            lines.append(escape_md(_loc("→ Disable 1-Click Trading on Nado, then re-link using Advanced 1CT with the bot's key.")))
        else:
            lines.append(f"❌ *{_loc('Signer Check')}:* {_loc('No signer linked on exchange')}")
            lines.append(escape_md(_loc("→ Go to Nado Settings → 1-Click Trading → Advanced 1CT → paste bot's key → enable and save.")))
    elif signer_linked:
        lines.extend(["", f"*{_loc('Verification')}*"])
        lines.append(f"🔗 *{_loc('1CT Signer')}:* {escape_md(_loc('LINKED (not verified)'))}")
    else:
        lines.extend(["", f"*{_loc('Verification')}*"])
        lines.append(f"🔗 *{_loc('1CT Signer')}:* {escape_md(_loc('NOT LINKED'))}")

    lines.extend(["", "_Use Wallet actions below to view balance, link, or revoke your trading signer\\._"])

    return "\n".join(lines)


def fmt_alerts(alerts):
    if not alerts:
        return (
            _ui_header("Alerts", icon="🔔")
            + "\n"
            + md2_rule()
            + "\n\n"
            + _loc("No alerts set yet\\.")
            + "\n\n"
            + _loc("Tap *Set Alert* to add a price, funding, or PnL trigger\\.")
        )

    lines = [
        _ui_header("Alerts", icon="🔔"),
        md2_rule(),
        "",
        _ui_section("Snapshot", [f"└ 📌 *{_loc('Active alerts')}:* {escape_md(str(len(alerts)))}"]),
        "",
    ]

    for idx, a in enumerate(alerts, start=1):
        condition = str(a.get("condition") or "")
        mode_str = escape_md(str(a.get("network", "mainnet")).upper())
        if a.get("kind") == "vault_deposit":
            # Bug #2: the NLP vault deposit watch is a separate, read-only kind
            # of alert (no numeric target / id). Render it with its own line.
            lines.append(f"🔔 *{escape_md(a['product'])}* \\| {_loc('Deposit Capacity Open')}")
            lines.append(
                f"{_loc('Notifies when vault deposit space opens')} \\| "
                f"{_loc('Mode')}: *{mode_str}*"
            )
        else:
            target = float(a.get("target") or 0)
            if condition.startswith("funding"):
                target_str = f"{target:,.4f}%"
            else:
                target_str = f"${target:,.2f}"
            lines.append(f"🔔 *{escape_md(a['product'])}* \\| {escape_md(condition.replace('_', ' ').title())}")
            lines.append(
                f"{_loc('Target')}: *{escape_md(target_str)}* \\| "
                f"{_loc('ID')}: *\\#{escape_md(str(a['id']))}* \\| "
                f"{_loc('Mode')}: *{mode_str}*"
            )
        if idx < len(alerts):
            lines.extend(["", _ui.subrule(), ""])

    return "\n".join(lines)


def fmt_alert_menu_intro() -> str:
    return (
        "🔔 *Alerts*\n"
        f"{md2_rule()}\n\n"
        "*What you can set*\n"
        "├ Price alerts\n"
        "├ Funding thresholds\n"
        "└ PnL triggers\n\n"
        "_Pick an action below\\._"
    )


def fmt_alert_product_prompt() -> str:
    return "\n\n".join(
        [
            _ui_header("Create Alert", icon="🔔"),
            md2_rule(),
            _ui_footer_hint("Choose the asset you want to monitor\\."),
        ]
    )


def fmt_alert_condition_prompt(product: str) -> str:
    return "\n\n".join(
        [
            f"🔔 *{escape_md(product)}\\-PERP Alert*",
            md2_rule(),
            _ui_footer_hint("Choose the trigger type below\\."),
        ]
    )


def fmt_alert_target_prompt(product: str, label: str, example: str) -> str:
    return "\n\n".join(
        [
            f"🔔 *{escape_md(product)}\\-PERP*",
            md2_rule(),
            _ui_section("Trigger", [f"└ {escape_md(label)}"]),
            "Enter the target value in chat\\.\n" + f"{example}",
        ]
    )


def fmt_mode_view(current_network: str) -> str:
    network_label = "🧪 TESTNET" if current_network == "testnet" else "🌐 MAINNET"
    return (
        "🌐 *Execution Mode*\n"
        f"{md2_rule()}\n\n"
        "*Quick snapshot*\n"
        f"└ *Current mode:* {escape_md(network_label)}\n\n"
        "_Mainnet trades real funds\\. Testnet is your sandbox\\. Flip anytime\\._"
    )


def fmt_close_all_confirm() -> str:
    return (
        "⚠️ *Close everything?*\n"
        f"{md2_rule()}\n\n"
        "This closes every open position in your current mode\\.\n\n"
        "_Only tap continue if you want all the way out\\._"
    )



def fmt_points_dashboard(payload: dict) -> str:
    if not payload or not payload.get("ok"):
        err = (payload or {}).get("error") or "Points data unavailable."
        return f"🏆 *Nado Points*\n\n{escape_md(err)}"

    no_activity = bool(payload.get("no_activity"))
    points = float(payload.get("points") or 0.0)
    volume_usd = float(payload.get("volume_usd") or 0.0)
    cpp = float(payload.get("cost_per_point") or 0.0)
    total_costs = float(payload.get("total_costs") or 0.0)
    ppm = float(payload.get("ppm") or 0.0)
    window = payload.get("window_label") or "Last 7 Days"

    lines = [
        "🏆 *Your Nado Points Dashboard*",
        md2_rule(),
        _ui_section("Snapshot", [f"└ 📅 *Window:* {escape_md(window)}"]),
        "",
    ]
    if no_activity:
        lines.extend(
            [
                "No points activity found for this period\\.",
                "",
                "Tip: increase real trading activity and check again after the next weekly epoch\\.",
            ]
        )
        return "\n".join(lines)

    lines.extend(
        [
            f"⭐ *Points:* {escape_md(f'{points:,.2f}')}",
            f"💰 *Volume:* {escape_md(f'${volume_usd:,.2f}')}",
            f"🧾 *Cost / Point:* {escape_md(f'${cpp:,.4f}')}",
            f"💸 *{_loc_md('Est. Costs')}:* {escape_md(f'${total_costs:,.2f}')}",
            f"📊 *Points / $1M:* {escape_md(f'{ppm:,.2f}')}",
        ]
    )
    return "\n".join(lines)


def _compute_exchange_stats(positions, prices):
    unrealized_pnl = 0.0
    position_value = 0.0
    for p in (positions or []):
        amount = abs(float(p.get("amount", 0) or 0))
        pname = p.get("product_name", "???")
        base = pname.replace("-PERP", "")
        current = 0.0
        if prices and base in prices:
            try:
                current = float((prices.get(base) or {}).get("mid", 0) or 0)
            except Exception:
                current = 0.0
        if not current:
            try:
                current = float(p.get("mark_price") or p.get("price") or 0)
            except Exception:
                current = 0.0
        if current:
            pnl = _calc_position_pnl(p, current)
            if pnl is not None:
                unrealized_pnl += pnl
            position_value += amount * current
    return unrealized_pnl, position_value


def fmt_portfolio(stats, positions, prices=None, open_orders=None, mode_label: str | None = None):
    total_trades = int(stats.get("total_trades", 0) or 0)
    open_orders = open_orders or []

    unrealized_pnl, position_value = _compute_exchange_stats(positions, prices)

    upnl_emoji = "🟢" if unrealized_pnl >= 0 else "🔴"
    upnl_str = f"+${unrealized_pnl:,.2f}" if unrealized_pnl >= 0 else f"-${abs(unrealized_pnl):,.2f}"

    lines = [
        _ui_header("Portfolio Deck", icon="📁"),
        md2_rule(),
        "",
    ]
    if mode_label:
        lines.extend([
            f"🌐 *{_loc('Mode:')}* {escape_md(mode_label)}",
            "",
        ])
    lines.extend([
        f"*{_loc('Snapshot')}*",
        f"📌 *{_loc('Open Positions:')}* {escape_md(str(len(positions or [])))} \\| "
        f"📬 *{_loc('Open Orders:')}* {escape_md(str(len(open_orders)))}",
        f"💎 *{_loc('Position Value:')}* {escape_md(f'${position_value:,.2f}')} \\| "
        f"{upnl_emoji} *{_loc('Unrealized PnL:')}* {escape_md(upnl_str)}",
    ])

    if total_trades > 0:
        total_volume = float(stats.get("total_volume", 0) or 0)
        volume_windows = stats.get("volume_windows") or {}
        volume_24h = float(volume_windows.get("24h", total_volume) or 0.0)
        volume_7d = float(volume_windows.get("7d", total_volume) or 0.0)
        volume_30d = float(volume_windows.get("30d", total_volume) or 0.0)
        total_pnl = float(stats.get("total_pnl", 0) or 0)
        total_fees = float(stats.get("total_fees", 0) or 0.0)
        total_funding = float(stats.get("total_funding", 0) or 0.0)
        win_rate = float(stats.get("win_rate", 0) or 0)
        rpnl_emoji = "🟢" if total_pnl >= 0 else "🔴"
        rpnl_str = f"+${total_pnl:,.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):,.2f}"
        lines.extend([
            "",
            md2_rule(),
            "",
            f"*{_loc('Bot Trading Stats')}*",
            f"💰 *{_loc('Volume:')}* 24h {escape_md(f'${volume_24h:,.2f}')} \\| 7d {escape_md(f'${volume_7d:,.2f}')}",
            f"📆 30d {escape_md(f'${volume_30d:,.2f}')} \\| All {escape_md(f'${total_volume:,.2f}')}",
            f"💸 *{_loc('Fees:')}* {escape_md(f'${total_fees:,.2f}')} \\| "
            f"🌀 *{_loc('Funding:')}* {escape_md(f'${total_funding:,.2f}')}",
            f"{rpnl_emoji} *{_loc('Realized PnL:')}* {escape_md(rpnl_str)}",
            f"🏆 *{_loc('Win Rate:')}* {escape_md(f'{win_rate:.1f}%')} \\| "
            f"{_loc('Trades:')} {escape_md(str(total_trades))}",
        ])

    lines.extend(["", f"*{_loc('Open Orders')}*"])
    if not open_orders:
        lines.append(_loc("No open orders right now\\."))
    else:
        for order in open_orders[:5]:
            o_type = str(order.get("type") or "LIMIT").upper()
            side = str(order.get("side") or "?").upper()
            product = str(order.get("product") or "?")
            size = float(order.get("size") or 0)
            limit_price = float(order.get("limit_price") or 0)
            created = str(order.get("created_at") or "")[:16]
            status = str(order.get("status") or "pending")
            filled_size = float(order.get("filled_size") or 0)
            requested_size = float(order.get("requested_size") or size)
            limit_price_str = f"${fmt_price(limit_price, product.replace('-PERP', ''))}"
            fill_progress = ""
            if requested_size > 0 and filled_size > 0:
                fill_progress = f" \\| {_loc('Filled')}: {escape_md(f'{filled_size:.4f}/{requested_size:.4f}')}"
            lines.append(
                f"• *{escape_md(product)}* \\| {escape_md(side)} {escape_md(o_type)}"
            )
            lines.append(
                f"  {_loc('Size')}: {escape_md(f'{size:.4f}')} \\| {_loc('Limit')}: {escape_md(limit_price_str)}"
            )
            lines.append(
                f"  {_loc('Status')}: *{escape_md(status)}*{fill_progress} \\| "
                f"{_loc('Created')}: {escape_md(created if created else '—')}"
            )
        if len(open_orders) > 5:
            lines.append(f"• \\.\\.\\. {_loc('and')} {escape_md(str(len(open_orders) - 5))} {_loc('more')}")

    if not positions:
        lines.append("")
        lines.append(_loc("No open positions right now\\."))
        return "\n".join(lines)

    lines.extend(["", f"*{_loc('Top Open Positions')}*"])
    for p in (positions or [])[:5]:
        side = p.get("side", "LONG")
        amount = abs(float(p.get("amount", 0) or 0))
        pname = p.get("product_name", "???")
        base = pname.replace("-PERP", "")
        current = 0.0
        if prices and base in prices:
            try:
                current = float((prices.get(base) or {}).get("mid", 0) or 0)
            except Exception:
                current = 0.0
        if not current:
            try:
                current = float(p.get("mark_price") or p.get("price") or 0)
            except Exception:
                current = 0.0

        pnl_text = _loc("N/A")
        if current:
            pnl = _calc_position_pnl(p, current)
            if pnl is not None:
                pnl_text = f"+${pnl:,.2f}" if pnl >= 0 else f"-${abs(pnl):,.2f}"

        lines.append(
            f"• *{escape_md(pname)}* \\| {escape_md(side)} {escape_md(f'{amount:.4f}')} \\| "
            f"{_loc('PnL')}: {escape_md(pnl_text)}"
        )

    if len(positions) > 5:
        lines.append(f"• \\.\\.\\. {_loc('and')} {escape_md(str(len(positions) - 5))} {_loc('more')}")
    return "\n".join(lines)


def fmt_trade_history(trades, page=0, page_size=10, mode_label: str | None = None):
    start = page * page_size
    page_trades = trades[start:start + page_size]

    lines = [
        _loc("📜 *Trade History*"),
        md2_rule(),
    ]
    if mode_label:
        lines.extend(["", f"🌐 *{_loc('Mode:')}* {escape_md(mode_label)}"])

    if not page_trades:
        lines.append("")
        lines.append(_loc("No trade history yet\\."))
        return "\n".join(lines)

    total = len(trades)
    current_page = page + 1
    total_pages = max(1, (total + page_size - 1) // page_size)
    lines.extend([
        "",
        f"*{_loc('Overview')}*",
        f"{_loc('Showing')}: *{escape_md(str(start + 1))}*\\-*{escape_md(str(min(start + page_size, total)))}* {_loc('of')} *{escape_md(str(total))}*",
        f"{_loc('Page')}: *{escape_md(str(current_page))}* {_loc('of')} *{escape_md(str(total_pages))}*",
        "",
    ])

    for idx, t in enumerate(page_trades, start=start + 1):
        product = t.get("product", "???")
        side = (t.get("side") or "???").upper()
        side_emoji = "🟢" if side == "LONG" else "🔴"
        status = (t.get("status") or "???").upper()
        price = t.get("price")
        close_price = t.get("close_price")
        pnl = t.get("pnl")
        created = t.get("created_at", "")[:16]

        price_str = fmt_price(float(price), product.replace("-PERP", "")) if price else "N/A"
        close_str = fmt_price(float(close_price), product.replace("-PERP", "")) if close_price else "—"

        pnl_str = "—"
        if pnl is not None:
            pnl_val = float(pnl)
            pnl_str = f"+${pnl_val:,.2f}" if pnl_val >= 0 else f"-${abs(pnl_val):,.2f}"

        lines.append(f"{side_emoji} *{escape_md(product)}* \\| *{escape_md(side)}* \\| {escape_md(status)}")
        lines.append(
            f"{_loc('Trade')}: *\\#{escape_md(str(idx))}* \\| "
            f"{_loc('PnL')}: *{escape_md(pnl_str)}* \\| "
            f"{escape_md(created if created else '—')}"
        )
        lines.append(f"{_loc('Entry:')} {escape_md(price_str)} → {_loc('Exit:')} {escape_md(close_str)}")
        if idx < min(start + page_size, total):
            lines.extend(["", _ui.subrule(), ""])

    return "\n".join(lines)


def fmt_analytics(stats, mode_label: str | None = None):
    lines = [
        _loc("📊 *Trading Analytics*"),
        md2_rule(),
        "",
    ]
    if mode_label:
        lines.extend([f"🌐 *{_loc('Mode:')}* {escape_md(mode_label)}", ""])

    total_trades = int(stats.get("total_trades", 0) or 0)
    if total_trades == 0:
        lines.append(_loc("No trades recorded yet\\."))
        return "\n".join(lines)

    filled = int(stats.get("filled", 0) or 0)
    closed = int(stats.get("closed", 0) or 0)
    failed = int(stats.get("failed", 0) or 0)
    wins = int(stats.get("wins", 0) or 0)
    losses = int(stats.get("losses", 0) or 0)
    win_rate = float(stats.get("win_rate", 0) or 0)
    total_pnl = float(stats.get("total_pnl", 0) or 0)
    total_volume = float(stats.get("total_volume", 0) or 0)
    volume_windows = stats.get("volume_windows") or {}
    volume_24h = float(volume_windows.get("24h", total_volume) or 0.0)
    volume_7d = float(volume_windows.get("7d", total_volume) or 0.0)
    volume_30d = float(volume_windows.get("30d", total_volume) or 0.0)
    total_fees = float(stats.get("total_fees", 0) or 0.0)
    total_funding = float(stats.get("total_funding", 0) or 0.0)

    pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"
    pnl_str = f"+${total_pnl:,.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):,.2f}"

    avg_trade = total_volume / max(filled + closed, 1)

    lines.extend([
        f"*{_loc('Overview')}*",
        f"{pnl_emoji} *{_loc('Total PnL:')}* {escape_md(pnl_str)} \\| "
        f"🏆 *{_loc('Win Rate:')}* {escape_md(f'{win_rate:.1f}%')}",
        f"📋 *{_loc('Total Trades:')}* {escape_md(str(total_trades))} \\| "
        f"✅ *{_loc('Wins:')}* {escape_md(str(wins))} \\| ❌ *{_loc('Losses:')}* {escape_md(str(losses))}",
        "",
        md2_rule(),
        "",
        f"*{_loc('Volume')}*",
        f"💰 *24h:* {escape_md(f'${volume_24h:,.2f}')} \\| *7d:* {escape_md(f'${volume_7d:,.2f}')}",
        f"📆 *30d:* {escape_md(f'${volume_30d:,.2f}')} \\| *{_loc('All')}:* {escape_md(f'${total_volume:,.2f}')}",
        f"💰 *{_loc('Total Volume:')}* {escape_md(f'${total_volume:,.2f}')} \\| "
        f"📏 *{_loc('Avg Trade Size:')}* {escape_md(f'${avg_trade:,.2f}')}",
        f"💸 *{_loc('Fees:')}* {escape_md(f'${total_fees:,.2f}')} \\| "
        f"🌀 *{_loc('Funding:')}* {escape_md(f'${total_funding:,.2f}')}",
        "",
        md2_rule(),
        "",
        f"*{_loc('Outcomes')}*",
        f"✅ *{_loc('Filled:')}* {escape_md(str(filled))} \\| "
        f"🔒 *{_loc('Closed:')}* {escape_md(str(closed))} \\| "
        f"❌ *{_loc('Failed:')}* {escape_md(str(failed))}",
    ])

    # Per-product breakdown if available
    by_product = stats.get("by_product")
    if by_product:
        lines.extend(["", f"*{_loc('PnL by Product')}*"])
        for product, data in by_product.items():
            p_pnl = float(data.get("pnl", 0))
            p_emoji = "🟢" if p_pnl >= 0 else "🔴"
            p_str = f"+${p_pnl:,.2f}" if p_pnl >= 0 else f"-${abs(p_pnl):,.2f}"
            p_count = int(data.get("count", 0))
            lines.append(f"{p_emoji} {escape_md(product)}: {escape_md(p_str)} \\({escape_md(str(p_count))} {_loc('trades')}\\)")

    return "\n".join(lines)


def md2_rule(width: int | None = None) -> str:
    """Horizontal rule for Telegram MarkdownV2 (escaped, safe).

    Delegates to ``ui.rule``. Kept as a name because ~50 call sites use it;
    calling it with no width now yields the single house width, not the old 30.
    """
    return _ui.rule(width)

def _ui_header(title: str, subtitle: str | None = None, icon: str | None = None) -> str:
    lines = []
    if icon:
        lines.append(f"{icon} *{_loc(title)}*")
    else:
        lines.append(f"*{_loc(title)}*")
    if subtitle:
        lines.append(_loc(subtitle))
    return "\n".join(lines)


def _ui_section(title: str, lines: list[str] | None = None, icon: str | None = None) -> str:
    head = f"*{_loc(title)}*"
    if icon:
        head = f"{icon} {head}"
    body = [head]
    if lines:
        body.extend(lines)
    return "\n".join(body)


def _ui_tree_line(prefix: str, label: str, value: str) -> str:
    return f"{prefix} *{_loc(label)}:* {escape_md(value)}"


def _ui_footer_hint(text: str) -> str:
    return f"_{_loc(text)}_"


def fmt_home_header() -> str:
    """First lines of home / start dashboard (MarkdownV2)."""
    return "\n".join(
        [
            _loc("🎯 *Nadobro Command Center*"),
            _loc(
                "_Your trading bro's online\\. Let's get it\\._"
            ),
            "📌",
        ]
    )


def _fmt_home_toolkit_tree() -> str:
    """Module map: matches reply keyboard and home card inline actions (MarkdownV2)."""
    return "\n".join(
        [
            f"├ 💼 *Wallet Vault* · {escape_md('balances and signer')}",
            f"├ 🤖 *Trade Console* · {escape_md('place perp orders')}",
            f"├ 🧠 *Strategy Lab* · {escape_md('automation')}",
            f"├ 📁 *Portfolio Deck* · {escape_md('positions and PnL')}",
            f"├ 💰 *Nado Vault* · {escape_md('LP deposit and withdraw')}",
            f"├ 🏆 *Nado Points* · {escape_md('rewards and market radar')}",
            f"├ 🎁 *Referrals* · {escape_md('your code')}",
            f"├ 🔔 *Alerts* · {escape_md('price and funding triggers')}",
            f"├ ⚙️ *Settings* · {escape_md('leverage and slippage')}",
            f"└ 🌐 *Execution Mode* · {escape_md('mainnet or testnet')}",
        ]
    )


def _fmt_network_balance_snapshot(network: str, balance_str: str) -> str:
    net_emoji = "🧪" if network == "testnet" else "🌐"
    net_name = "TESTNET" if network == "testnet" else "MAINNET"
    return "\n".join(
        [
            f"*{_loc('Quick snapshot')}*",
            f"├ {net_emoji} *Mode:* {escape_md(net_name)}",
            f"├ 💵 *USDT:* {escape_md(balance_str)}",
        ]
    )


def fmt_home_command_center_card(network: str, balance_str: str) -> str:
    """Home “Command Center” card for logged-in users (MarkdownV2)."""
    return "\n\n".join(
        [
            fmt_home_header(),
            md2_rule(),
            _fmt_network_balance_snapshot(network, balance_str),
            md2_rule(),
            f"*{_loc('Your toolkit')}*",
            _fmt_home_toolkit_tree(),
            md2_rule(),
            "_Tap a button, or just type\\. I'll answer questions and place trades in plain English\\._",
        ]
    )


def fmt_dashboard_home() -> str:
    """Home dashboard banner after /start (MarkdownV2)."""
    status = _ui_section("Status", [f"├ 🟢 *Nadobro* · {escape_md('online')} ✅"])
    nxt = _ui_section(
        "Next step",
        [escape_md("Use the reply keyboard below to open modules.")],
    )
    return "\n\n".join(
        [
            fmt_home_header(),
            md2_rule(),
            status,
            md2_rule(),
            nxt,
        ]
    )


def fmt_getting_started() -> str:
    """First-run guided rail shown right after a user accepts the terms."""
    return "\n".join(
        [
            "🚀 *Getting Started*",
            md2_rule(),
            "",
            "Three quick steps and you're trading:",
            "",
            "*1\\. Link your wallet*",
            escape_md("Open Wallet Vault and connect with the secure 1CT signer. Your main keys never move."),
            "",
            "*2\\. Add funds*",
            escape_md("Deposit USDT0 on Nado so you've got margin to work with."),
            "",
            "*3\\. Make your first trade*",
            "Type something like `Long BTC 0.01 at 5x`, or tap Trade Console\\.",
            "",
            "_Tap below to get going\\._",
        ]
    )


def fmt_referral_dashboard(payload: dict) -> str:
    code = payload.get("share_code") or {}
    public_code = str(code.get("public_code") or "")
    link = str(code.get("link") or "")
    redemption_count = int(code.get("redemption_count") or 0)
    total_referrals = int(payload.get("total_referrals") or 0)
    total_referred_volume = float(payload.get("total_referred_volume") or 0.0)
    total_referred_trades = int(payload.get("total_referred_trades") or 0)
    min_len = int(payload.get("min_code_len") or 3)
    max_len = int(payload.get("max_code_len") or 20)
    network = str(payload.get("network") or "mainnet").upper()

    lines = [
        _ui_header("Referral Deck", icon="🎁"),
        md2_rule(),
        "",
        "*Snapshot*",
        f"🌐 *Mode:* {escape_md(network)}",
        f"👥 *Direct Referrals:* {escape_md(str(total_referrals))}",
        f"💎 *Referred Volume:* {escape_md(f'${total_referred_volume:,.2f}')}",
        f"📈 *Referred Trades:* {escape_md(str(total_referred_trades))}",
    ]

    if public_code:
        lines.extend([
            "",
            "*Your Referral Code*",
            f"🔑 *Code:* `{escape_md(public_code)}`",
            f"🔗 *Link:* {escape_md(link)}",
            f"🎟 *Redemptions:* {escape_md(str(redemption_count))}",
            escape_md("Codes are permanent and unlimited - share freely."),
        ])
    else:
        lines.extend([
            "",
            "*Claim your code*",
            escape_md(
                f"You don't have a referral code yet. Pick a custom one "
                f"({min_len}-{max_len} chars, A-Z and 0-9) or auto-generate "
                "an 8-character code. Either way, the code is yours forever "
                "once claimed."
            ),
        ])

    referred_users = payload.get("referred_users") or []
    lines.extend(["", "*Direct Referrals*"])
    if not referred_users:
        lines.append(
            escape_md(
                "No referrals yet. Drop your code in the group chats and "
                "watch the volume roll in here."
            )
        )
    else:
        for row in referred_users[:5]:
            username = str(row.get("username") or "").strip()
            label = f"@{username}" if username else f"ID {row.get('referred_user_id')}"
            vol = float(row.get("referred_volume_usd") or 0.0)
            trades = int(row.get("referred_trade_count") or 0)
            lines.append(
                f"• *{escape_md(label)}* \\| {escape_md(f'${vol:,.2f}')} \\| "
                f"{escape_md(str(trades))} trades"
            )
        if len(referred_users) > 5:
            lines.append(f"• \\.\\.\\. and {escape_md(str(len(referred_users) - 5))} more")

    lines.extend([
        "",
        md2_rule(),
        "_Anyone can claim a code; each code is unique and tied to one Nadobro account\\._",
    ])
    return "\n".join(lines)
    return "\n\n".join(
        [
            fmt_home_header(),
            md2_rule(),
            status,
            "",
            nxt,
            md2_rule(),
            f"*{_loc('Your toolkit')}*",
            _fmt_home_toolkit_tree(),
            md2_rule(),
            "_Tap a button, or just type\\. I'll answer questions and place trades in plain English\\._",
        ]
    )


def fmt_strategy_hub_intro() -> str:
    return "\n\n".join(
        [
            _ui_header("Nadobro Strategy Lab", icon="🧠"),
            md2_rule(),
            _ui_section(
                "How it works",
                [
                    "├ Pick a strategy and I'll open its cockpit",
                    "├ Tune the parameters \\(safe defaults are already in\\)",
                    "└ Launch with a pre\\-trade readout so nothing's a surprise",
                ],
            ),
            _ui_footer_hint("Pick one below\\."),
        ]
    )


def fmt_stop_all_result(ok: bool, backend_msg: str, hint: str) -> str:
    """Stop\\_all command reply (MarkdownV2)."""
    icon = "🛑" if ok else "⚠️"
    title = "Automation stopped" if ok else "Stop automation"
    return (
        f"{icon} *{escape_md(title)}*\n"
        f"{md2_rule()}\n\n"
        f"{escape_md(backend_msg)}\n\n"
        f"_{escape_md(hint)}_"
    )


def fmt_revoke_card() -> str:
    """1CT revoke steps (MarkdownV2)."""
    return (
        "🔄 *Revoke 1CT Key \\(Nado\\)*\n"
        f"{md2_rule()}\n\n"
        "1\\. Open Nado → Settings\n"
        "2\\. 1\\-Click Trading → Advanced 1CT\n"
        "3\\. Disable the toggle and save\n\n"
        "_Your main wallet never moves\\. Re\\-link anytime from Wallet Vault\\._"
    )


def fmt_managed_agent_enabled() -> str:
    return (
        "🧠 *Managed AI* is *ON*\n"
        f"{md2_rule()}\n\n"
        "Just talk to me normally\\.\n"
        "I send *analysis* to the brain and run any *strategy* through the usual safety checks first\\.\n\n"
        "_Check anytime with_ `/agent\\_status`\\."
    )


def fmt_managed_agent_disabled() -> str:
    return (
        "🧠 *Managed AI* is *OFF*\n"
        f"{md2_rule()}\n\n"
        "Back to standard Nadobro chat\\.\n\n"
        "_Turn it back on anytime:_ `/agent\\_on`"
    )


def fmt_managed_agent_globally_disabled() -> str:
    return (
        "🧠 *Managed AI mode*\n"
        f"{md2_rule()}\n\n"
        "⚠️ *Turned off by ops* right now\\.\n"
        "Try again a bit later\\."
    )


def fmt_managed_agent_status(effective_on: bool, global_enabled: bool, updated_at: str) -> str:
    eff = "ON ✅" if effective_on else "OFF"
    glob = "ENABLED ✅" if global_enabled else "DISABLED ⛔️"
    return (
        "🧠 *Managed AI Status*\n"
        f"{md2_rule()}\n\n"
        f"*Effective mode:* {escape_md(eff)}\n"
        f"*Global ops switch:* {escape_md(glob)}\n"
        f"*Last update:* {escape_md(updated_at)}"
    )


def fmt_settings(user_data):
    leverage = user_data.get("default_leverage", 1)
    slippage = user_data.get("slippage", 1)
    risk_profile = str(user_data.get("risk_profile", "balanced")).upper()

    lines = [
        _ui_header("Settings", icon="⚙️"),
        md2_rule(),
        "",
        _ui_section(
            "Right now",
            [
                _ui_tree_line("├", "Risk Profile", risk_profile),
                _ui_tree_line("├", "Default Leverage", f"{leverage}x"),
                _ui_tree_line("└", "Slippage", f"{slippage}%"),
            ],
        ),
        "",
        _ui_footer_hint("Buttons below change language, leverage, slippage, and risk preset\\."),
    ]

    return "\n".join(lines)


def fmt_wallet_connect_card(pk_hex: str) -> str:
    # A fenced ``pre`` block, not an inline code span: Telegram renders it with
    # an explicit COPY button, where inline code relies on the user knowing to
    # tap it. Same tap-to-copy semantics, a visible affordance. Escaped with
    # escape_md_code — inside a code entity only ` and \\ are special, and using
    # the general escaper would put literal backslashes into the copied key.
    key_line = f"```\n{escape_md_code(pk_hex)}\n```"
    return "\n\n".join(
        [
            _ui_header("Wallet Connect", icon="👛"),
            md2_rule(),
            _ui_section(
                "Setup steps",
                [
                    "├ 1\\. Open https://app\\.nado\\.xyz\\?join\\=FzpOSwX, connect your wallet, and deposit at least $5 USDT0\\.",
                    "├ 2\\. Go to *Settings → 1\\-Click Trading → Advanced 1CT*\\.",
                    "├ 3\\. Paste the trading key below into *1CT Private Key* \\(tap to copy\\):",
                    "├ 4\\. Enable the toggle, save, and confirm the wallet transaction\\.",
                    "└ 5\\. Reply here with your main wallet address \\(`0x...`\\)\\.",
                ],
            ),
            key_line,
            _ui_footer_hint("This signer is for trading only and cannot withdraw funds\\."),
        ]
    )


def fmt_wallet_balance_card(amount: float) -> str:
    return "\n\n".join(
        [
            _ui_header("Wallet Balance", icon="💰"),
            md2_rule(),
            _ui_section("Quick snapshot", [f"└ *Available USDT0:* {escape_md(f'${amount:,.2f}')}"]),
        ]
    )


def fmt_wallet_balance_error() -> str:
    return "\n\n".join(
        [
            _ui_header("Wallet Balance", icon="💰"),
            md2_rule(),
            "⚠️ Could not fetch balance right now\\. Try again shortly\\.",
        ]
    )


def fmt_wallet_revoke_steps_card() -> str:
    return "\n\n".join(
        [
            _ui_header("Revoke 1CT Key", icon="🔄"),
            md2_rule(),
            _ui_section(
                "Steps",
                [
                    "├ 1\\. Open Nado → Settings",
                    "├ 2\\. 1\\-Click Trading → Advanced 1CT",
                    "└ 3\\. Disable the toggle and save",
                ],
            ),
            _ui_footer_hint("You can also reset Nadobro's stored signer below if you want to link a new key\\."),
        ]
    )


def _fmt_ink_amount(amount: Decimal) -> str:
    """Display an INK amount: thousands separators, up to 4 decimals
    (rounded DOWN — never overstate an allocation), trailing zeros trimmed.
    Dust below the 4-decimal cut renders at full precision instead of a
    misleading "0"."""
    if amount == 0:
        return "0"
    try:
        with localcontext() as ctx:
            ctx.prec = 50
            q = amount.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
    except InvalidOperation:
        q = amount
    shown = q if q != 0 else amount
    text = format(shown, ",f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def fmt_ink_airdrop_card(address: str, amount: Decimal) -> str:
    """MarkdownV2 card for /airdrop. ``amount`` is whole INK; zero means the
    archive answered and the address has no allocation (the archive-down case
    never reaches here)."""
    short = f"{address[:6]}…{address[-4:]}" if len(address) >= 12 else address
    lines = [
        "🪂 *Ink Airdrop*",
        escape_md(f"Address: {short}"),
    ]
    if amount > 0:
        lines.append("Allocation: *" + escape_md(f"{_fmt_ink_amount(amount)} INK") + "*")
    else:
        # "yet", deliberately: the venue table only holds allocations recorded
        # so far. Nado points convert to INK at TGE, so an active trader can
        # legitimately read 0 here until Nado loads that distribution.
        lines.append(escape_md("No Ink airdrop allocation recorded for this address yet."))
        lines.append(escape_md(
            "Nado points convert to INK at TGE — distributions that haven't "
            "been loaded by Nado won't show here."
        ))
    return "\n".join(lines)


def fmt_help():
    _HELP_TEXT = (
        "📖 *Nadobro Guide*\n"
        "_Your trading bro, right here in chat\\._\n"
        + escape_md("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━") + "\n"
        "\n"
        "*Commands*\n"
        "/start \\- Open your home dashboard\n"
        "/help \\- This guide\n"
        "/status \\- Bot health, setup, and live strategies\n"
        "/ops \\- Order flow and runtime diagnostics\n"
        "/desk \\- Talk a trade out loud, preview it, then confirm\n"
        "/brief \\- Your full morning market brief\n"
        "/news \\- Latest market news \\(add a category to filter\\)\n"
        "/airdrop \\- Ink airdrop allocation for your wallet \\(or any 0x address\\)\n"
        "/howl \\- Your saved Night HOWL reports \\(`list` or a date to browse\\)\n"
        "/mm\\_status \\- Live market\\-making dashboard \\(GRID / RGRID / DGRID / Mid\\)\n"
        "/mm\\_fills \\- Recent fills on your active MM strategy\n"
        "/stop\\_all \\- Stop every running strategy and flatten bot exposure\n"
        "/revoke \\- How to revoke your 1CT signer\n"
        "/agent\\_on \\- Turn on managed AI chat\n"
        "/agent\\_off \\- Turn off managed AI chat\n"
        "/agent\\_status \\- Check managed AI chat status\n"
        "\n"
        "*What's inside*\n"
        "\n"
        "💼 *Wallet Vault*\n"
        "Link your wallet with the secure 1CT flow, check balances, manage your signer\\.\n"
        "\n"
        "🤖 *Trade Console*\n"
        "Market and limit orders\\. Tap through the guided flow or just type the trade\\.\n"
        "\n"
        "🧠 *Strategy Lab*\n"
        "Market making \\(GRID, Reverse GRID, Dynamic GRID, Mid Mode\\), plus Volume Bot, Copy Trading, and Delta Neutral — all live\\.\n"
        "Every strategy opens with controls, safety defaults, and a pre\\-trade readout\\.\n"
        "\n"
        "📁 *Portfolio Deck*\n"
        "Open positions, realized and unrealized PnL, trade history, and analytics in one place\\.\n"
        "\n"
        "💰 *Nado Vault*\n"
        "One\\-tap deposit and withdraw on Nado's market\\-making LP vault\\. We ping you when a slot opens\\.\n"
        "\n"
        "🔔 *Alerts*\n"
        "Set price and funding triggers, get pinged instantly\\.\n"
        "\n"
        "🏆 *Nado Points \\+ Market Radar*\n"
        "Track your Season 1 points, volume, and cost\\-per\\-point in real time, plus a live market read\\.\n"
        "\n"
        "🎁 *Referrals*\n"
        "Claim your code \\(live now\\) and bring traders in\\.\n"
        "\n"
        "🌐 *Execution Mode*\n"
        "Flip between Testnet and Mainnet anytime\\. Practice risk\\-free, then go live\\.\n"
        "\n"
        "🔒 *Security*\n"
        "• Your 1CT signer can trade but can never withdraw\n"
        "• Never share a private key or seed phrase\n"
        "• Use a dedicated wallet for automation\n"
        "\n"
        "*Just type it*\n"
        "  • `Long BTC 0.01 at 5x`\n"
        "  • `Short ETH 0.05 limit 2400`\n"
        "  • `Show my portfolio`\n"
        "  • `Close all positions`\n"
        "  • `What's unified margin?`\n"
        "\n"
        "Stuck? Tell me what you tapped and paste the error\\. I'll sort it\\."
    )
    return _loc(_HELP_TEXT)


def _fmt_action_label(action: str) -> str:
    labels = {
        "hold": _loc("Holding"),
        "open_long": _loc("Opened Long"),
        "open_short": _loc("Opened Short"),
        "close": _loc("Closed Position"),
        "emergency_flatten": _loc("Emergency Flatten"),
        "blocked": _loc("Blocked"),
        "cycle": _loc("Cycle Complete"),
    }
    return labels.get(action, action.replace("_", " ").title() if action else "—")


def _fmt_uptime(started_at: str) -> str:
    if not started_at:
        return "—"
    try:
        from datetime import datetime, timezone
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - start
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes, _ = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"
    except Exception:
        return "—"


def _fmt_age_seconds(ts: float) -> str:
    if not ts:
        return "—"
    try:
        age = max(0, int(time.time() - float(ts)))
        if age < 60:
            return f"{age}s"
        mins, secs = divmod(age, 60)
        if mins < 60:
            return f"{mins}m {secs}s"
        hours, mins = divmod(mins, 60)
        return f"{hours}h {mins}m"
    except Exception:
        return "—"


def _fmt_progress_bar(done: float, total: float, width: int = 12) -> str:
    total_v = max(0.0, float(total or 0.0))
    done_v = max(0.0, float(done or 0.0))
    if total_v <= 0:
        return "[............]"
    pct = min(1.0, done_v / total_v)
    filled = max(0, min(width, int(round(pct * width))))
    return "[" + ("#" * filled) + ("." * (width - filled)) + "]"


def _fmt_compact_usd(value: float | int | None) -> str:
    amount = float(value or 0.0)
    abs_amount = abs(amount)
    if abs_amount >= 1_000_000:
        return f"${amount / 1_000_000:.2f}M"
    if abs_amount >= 1_000:
        return f"${amount / 1_000:.2f}K"
    return f"${amount:,.2f}"


def _fmt_signed_usd(value: float | int | None) -> str:
    amount = float(value or 0.0)
    if amount > 0:
        return f"+{_fmt_compact_usd(amount)}"
    if amount < 0:
        return f"-{_fmt_compact_usd(abs(amount))}"
    return "$0.00"


def fmt_status_overview(status: dict, onboarding: dict):
    running = status.get("running")
    complete = onboarding.get("onboarding_complete")
    mode = onboarding.get("network", "testnet").upper()
    key_ready = onboarding.get("has_key")
    funded = onboarding.get("funded")

    lines = [_loc("📡 *Status*"), md2_rule(), f"{_loc('Network:')} *{escape_md(mode)}*"]

    if not complete:
        step = onboarding.get("missing_step") or "unknown"
        lines.extend(["", f"*{_loc('Setup')}*"])
        lines.append(f"{_loc('Setup:')} *{_loc('IN PROGRESS')}* · {escape_md(step.replace('_', ' ').title())}")
        if not key_ready:
            lines.append(f"{_loc('Key:')} *{_loc('NOT SET')}* · {_loc('link via Wallet Vault to continue')}")
        elif not funded:
            lines.append(f"{_loc('Funding:')} *{_loc('NEEDED')}* · {_loc('deposit to your wallet')}")
        return "\n".join(lines)

    readiness_lines = []
    if not key_ready or not funded:
        readiness_lines.extend(["", f"*{_loc('Trading Readiness')}*"])
        if not key_ready:
            readiness_lines.append(f"{_loc('Wallet')}: *{_loc('NOT LINKED')}* · {_loc('open Wallet to connect your 1CT signer')}")
        if key_ready and not funded:
            readiness_lines.append(f"{_loc('Funding')}: *{_loc('NEEDED')}* · {_loc('deposit to your wallet')}")

    if not running:
        lines.extend(["", f"*{_loc('Runtime')}*"])
        lines.extend(readiness_lines)
        lines.append(f"{_loc('Strategy:')} *{_loc('OFF')}*")
        last_action = status.get("last_action")
        if last_action:
            lines.append(f"{_loc('Last:')} {escape_md(_fmt_action_label(last_action))}")
        if status.get("last_error"):
            lines.append(f"{_loc('Note:')} {escape_md(str(status.get('last_error'))[:120])}")
        lines.extend(_fmt_copy_mirror_lines(status, _loc))
        return "\n".join(lines)

    strategy = (status.get("strategy") or "").upper()
    product = str(status.get("product") or "BTC").upper()
    product_base = product.replace("-PERP", "")
    if strategy == "VOL" and str(status.get("vol_market") or "").lower() == "spot":
        product_label = f"{product_base} SPOT"
    else:
        product_label = "MULTI" if product == "MULTI" else f"{product_base}-PERP"
    global_pause_active = bool(status.get("global_pause_active"))
    locally_paused = bool(status.get("is_paused"))
    runs = int(status.get("runs") or 0)
    interval = int(status.get("interval_seconds") or 0)
    uptime = _fmt_uptime(status.get("started_at"))
    next_in = int(status.get("next_cycle_in") or 0)
    spread_bp = float(status.get("spread_bp") or 0.0)
    order_obs = status.get("order_observability") or {}
    obs_placed = int(order_obs.get("orders_placed") or 0)
    obs_filled = int(order_obs.get("orders_filled") or 0)
    obs_cancelled = int(order_obs.get("orders_cancelled") or 0)
    fill_pct = (obs_filled / obs_placed * 100.0) if obs_placed > 0 else 0.0

    session_volume = float(status.get("session_volume_usd") or 0.0)
    if strategy == "VOL" and session_volume <= 0:
        session_volume = float(status.get("volume_done_usd") or 0.0)
    if strategy in ("GRID", "RGRID", "DGRID") and session_volume <= 0:
        session_volume = float(status.get("session_notional_done_usd") or 0.0)
    session_fees = float(status.get("session_fees_usd") or 0.0)
    session_funding = float(status.get("session_funding_usd") or 0.0)

    session_pnl = float(status.get("session_analytics_pnl_usd") or 0.0)
    if strategy == "VOL":
        session_pnl = float(status.get("session_realized_pnl_usd") or session_pnl)
    elif strategy in ("GRID", "RGRID", "DGRID"):
        session_pnl = float(status.get("rgrid_last_cycle_pnl_usd") or session_pnl)
    elif strategy == "BRO":
        session_pnl = float((status.get("bro_state") or {}).get("total_pnl") or session_pnl)

    # Cost/$1M — net-of-fees result per $1M of volume: (realized PnL − fees) /
    # volume × 1,000,000. Signed by outcome (profit after fees → +, loss → −).
    cost_per_million = (
        (session_pnl - session_fees) / session_volume * 1_000_000.0
        if session_volume > 0 else 0.0
    )
    state_label = "PAUSED" if (global_pause_active or locally_paused) else "LIVE"
    last_cycle_result = str(status.get("last_cycle_result") or "").strip().lower()
    runtime_summary = f"{_loc('Uptime')} *{escape_md(uptime)}* \\| {_loc('Cycles')} *{escape_md(str(runs))}*"
    if next_in > 0:
        runtime_summary += f" \\| {_loc('Next')} *{escape_md(str(next_in))}s*"
    elif interval > 0:
        runtime_summary += f" \\| {_loc('Every')} *{escape_md(str(interval))}s*"

    lines.extend([
        "",
        f"*{escape_md(strategy)} · {escape_md(product_label)}*",
        f"{_loc('Status')}: *{escape_md(state_label)}* \\| {runtime_summary}",
    ])
    # GATE-VISIBILITY (2026-07-31): while the regime gate pauses quoting, the
    # card used to read "LIVE … Last cycle: OK" with zero orders — dark quoting
    # was invisible. Name the state, the reason, and how long it has held.
    gate_verdict = str(status.get("mm_gate_verdict") or "").strip().upper()
    if gate_verdict == "PAUSE":
        # Via the strategy layer: handlers must not import engine directly
        # (tests/lint/test_architecture_layers.py pins the edge set).
        from src.nadobro.strategy.engine_runtime import (
            GATE_REASON_HUMAN, REVGRID_GATE_REASONS,
        )
        _gate_reason_key = str(status.get("mm_gate_reason") or "").strip()
        gate_why = GATE_REASON_HUMAN.get(_gate_reason_key, "unfavourable regime")
        gate_age = _fmt_age_seconds(float(status.get("mm_gate_since_ts") or 0.0))
        gate_line = (
            f"⏸ {_loc('Quoting')}: *{_loc_md('PAUSED')}* \\({_loc_md(gate_why)}\\)"
        )
        if gate_age != "—":
            gate_line += f" \\| {_loc('for')} *{escape_md(gate_age)}*"
        lines.append(gate_line)
        # Resume condition is the MIRROR of the strategy: a ranging grid resumes
        # when the market ranges again; a reverse grid pyramids WITH a trend, so it
        # resumes when a trend forms. Word it per reason so the card never tells a
        # reverse-grid user to wait for the opposite of what actually arms it.
        if _gate_reason_key in REVGRID_GATE_REASONS:
            _resume_line = _loc("New quotes resume automatically when a trend forms.")
        else:
            _resume_line = _loc("New quotes resume automatically when the market ranges again.")
        lines.append(f"{escape_md(_resume_line)}")
    elif gate_verdict == "QUOTE" and str(status.get("mm_gate_reason") or "") == "":
        # Quiet confirmation only — the pause state is the one that matters.
        lines.append(f"{_loc('Quoting')}: {_loc_md('active')} ✅")
    if strategy == "MID":
        mode = str(status.get("mid_execution_mode") or "").upper()
        inv_state = str(status.get("inventory_state") or "")
        budget = status.get("expected_budget_usd")
        used = status.get("expected_budget_used_usd")
        stop_reason = str(status.get("budget_stop_reason") or "")
        if mode:
            lines.append(f"Execution: *{escape_md(mode)}* · Inventory: *{escape_md(inv_state or 'neutral')}*")
        if budget is not None and float(budget or 0) > 0:
            budget_line = (
                f"Expected budget: *{escape_md(_fmt_compact_usd(float(used or 0)))} / "
                f"{escape_md(_fmt_compact_usd(float(budget)))}*"
            )
            if stop_reason:
                budget_line += f" · *{escape_md(stop_reason.replace('_', ' '))}*"
            lines.append(budget_line)
    lines.extend(readiness_lines)

    if strategy == "DN":
        # DN sizes from fixed_margin_usd; show per-leg Size (not notional_usd).
        dn_size = status.get("dn_size_usd")
        if dn_size is not None:
            lines.append(
                f"{_loc('Size')} \\(per leg\\): *{escape_md(_fmt_compact_usd(float(dn_size)))}* \\| "
                f"{_loc('Short')}: *1x*"
            )
    else:
        margin = status.get("notional_usd")
        cyc = status.get("cycle_notional_usd")
        if margin is not None:
            margin_text = f"{_loc('Margin')}: *{escape_md(_fmt_compact_usd(float(margin)))}*"
            if cyc is not None:
                margin_text += f" \\| {_loc('Per cycle')}: *{escape_md(_fmt_compact_usd(float(cyc)))}*"
            lines.append(margin_text)

    lines.extend(["", f"*{_loc('Trading Summary')}*"])
    lines.append(
        f"{_loc('Volume')}: *{escape_md(_fmt_compact_usd(session_volume))}* \\| "
        f"{_loc('Fees')}: *{escape_md(_fmt_compact_usd(session_fees))}*"
    )
    lines.append(
        f"{_loc('PnL')}: *{escape_md(_fmt_signed_usd(session_pnl))}* \\| "
        f"{_loc('Filled')}: *{escape_md(f'{fill_pct:.0f}%')}*"
    )
    lines.append(
        f"{_loc('Cost / $1M')}: *{escape_md(_fmt_signed_usd(cost_per_million))}* \\| "
        f"{_loc('Spread')}: *{escape_md(f'{spread_bp:.0f}bp')}*"
    )
    if session_funding:
        lines.append(f"{_loc('Funding')}: *{escape_md(_fmt_signed_usd(session_funding))}*")

    if strategy == "VOL":
        volume_done = float(status.get("volume_done_usd") or 0.0)
        volume_remaining = float(status.get("volume_remaining_usd") or 0.0)
        target_volume = float(status.get("target_volume_usd") or 0.0)
        progress_pct = (volume_done / target_volume * 100.0) if target_volume > 0 else 0.0
        # Internal phase ids are not user copy — name what the bot is doing.
        _VOL_PHASE_LABELS = {
            "idle": "idle",
            "pending_fill": "buying",
            "pending_entry_fill": "buying",
            "filled_wait_close": "preparing exit",
            "hold_for_profit": "closing a held position",
            "pending_close_fill": "selling",
            "cycle_gap": "between cycles",
            "market_closed": "waiting for the market to open",
            "done": "finished",
        }
        _vol_phase_raw = str(status.get("vol_phase") or "").strip() or "idle"
        vol_phase = _VOL_PHASE_LABELS.get(_vol_phase_raw, _vol_phase_raw)
        lines.extend(["", "*Volume Progress*"])
        lines.append(
            f"{_loc('Done')}: *{escape_md(_fmt_compact_usd(volume_done))}* \\| "
            f"{_loc('Remaining')}: *{escape_md(_fmt_compact_usd(volume_remaining))}*"
        )
        if target_volume > 0:
            lines.append(
                f"{_loc('Bar')}: `{escape_md(_fmt_progress_bar(volume_done, target_volume))}` \\| "
                f"{_loc('Progress')}: *{escape_md(f'{progress_pct:.1f}%')}*"
            )
            lines.append(f"{_loc('Target')}: *{escape_md(_fmt_compact_usd(target_volume))}*")
        lines.append(f"{_loc('Phase')}: *{escape_md(vol_phase)}*")
        closed_cycles = int(status.get("vol_closed_cycles") or 0)
        if closed_cycles > 0:
            vol_win_rate_pct = float(status.get("vol_win_rate") or 0.0) * 100.0
            vol_avg_pnl = float(status.get("vol_avg_cycle_pnl_usd") or 0.0)
            lines.append(
                f"{_loc('Cycles')}: *{escape_md(str(closed_cycles))}* \\| "
                f"{_loc('Win rate')}: *{escape_md(f'{vol_win_rate_pct:.0f}%')}* \\| "
                f"{_loc('Avg PnL')}: *{escape_md(_fmt_signed_usd(vol_avg_pnl))}*"
            )

    pause_reason = str(status.get("pause_reason") or "").strip()
    if global_pause_active:
        lines.append("")
        lines.append(f"⚠️ *{_loc('Paused')}*: {escape_md(_loc('Global trading pause is active. Strategy execution is suspended.'))}")
    elif locally_paused and pause_reason:
        lines.append("")
        lines.append(f"⚠️ *{_loc('Paused')}*: {escape_md(pause_reason)}")

    if last_cycle_result:
        cycle_badge = "✅" if last_cycle_result == "ok" else "❌"
        lines.append(f"{cycle_badge} {_loc('Last cycle')}: *{escape_md(last_cycle_result.upper())}*")

    if status.get("last_error"):
        lines.append(f"{_loc('Note')}: {escape_md(str(status.get('last_error'))[:160])}")
    if status.get("last_error_category"):
        lines.append(f"{_loc('Error class')}: *{escape_md(str(status.get('last_error_category')).upper())}*")
    if status.get("last_order_error"):
        lines.append(f"{_loc('Last order error')}: {escape_md(str(status.get('last_order_error'))[:160])}")

    heartbeat = _fmt_age_seconds(float(status.get("worker_last_heartbeat") or 0.0))
    cycle_ms = float(status.get("last_cycle_ms") or 0.0)
    if obs_placed or runs or cycle_ms > 0:
        lines.extend(["", f"*{_loc('Runtime')}*"])
        lines.append(
            f"{_loc('Orders')}: {_loc('placed')} *{escape_md(str(obs_placed))}* \\| "
            f"{_loc('filled')} *{escape_md(str(obs_filled))}* \\| "
            f"{_loc('cancelled')} *{escape_md(str(obs_cancelled))}*"
        )
        lines.append(
            f"{_loc('Loop')}: *{escape_md(str(runs))}* \\| "
            f"{_loc('Cycle')}: *{escape_md(f'{cycle_ms:.0f}ms')}* \\| "
            f"{_loc('Heartbeat')}: *{escape_md(heartbeat)}*"
        )

    if strategy == "DN":
        dn_fr = float(status.get("dn_last_funding_rate") or 0.0)
        hold_s = int(status.get("dn_hold_seconds") or 3600)
        hold_lbl = (
            f"{hold_s // 3600}h" if hold_s % 3600 == 0
            else (f"{hold_s // 60}m" if hold_s % 60 == 0 else f"{hold_s}s")
        )
        total_cycles = int(status.get("dn_cycles") or 1)
        done_raw = status.get("dn_cycles_completed")
        cycles_lbl = (
            f"{int(done_raw)}/{total_cycles}" if done_raw is not None else str(total_cycles)
        )
        funding_earned = status.get("dn_funding_earned_usd")
        lines.extend(["", "*Delta Neutral*"])
        lines.append(
            f"{_loc('Hold')}: *{escape_md(hold_lbl)}* \\| "
            f"{_loc('Cycles')}: *{escape_md(cycles_lbl)}*"
        )
        if funding_earned is not None:
            lines.append(
                f"{_loc('Funding earned')}: *{escape_md(_fmt_signed_usd(float(funding_earned)))}* \\| "
                f"{_loc('Rate')}: *{escape_md(f'{dn_fr:.6f}')}*"
            )
        else:
            lines.append(f"{_loc('Funding rate')}: *{escape_md(f'{dn_fr:.6f}')}*")

    bro_state = status.get("bro_state") or {}
    if strategy == "BRO" and bro_state:
        trade_count = int(bro_state.get("trade_count", 0) or 0)
        active = len(bro_state.get("active_positions", []))
        lines.extend(["", "*Alpha Agent*"])
        lines.append(f"{_loc('Trades')}: *{escape_md(str(trade_count))}* \\| {_loc('Open Positions')}: *{escape_md(str(active))}*")

    if strategy in ("GRID", "RGRID", "DGRID"):
        anchor = float(status.get("rgrid_anchor_price") or 0.0)
        buy_exp = float(status.get("rgrid_buy_exposure_price") or 0.0)
        sell_exp = float(status.get("rgrid_sell_exposure_price") or 0.0)
        drift_pct = float(status.get("rgrid_drift_from_anchor_pct") or 0.0)
        reset_active = bool(status.get("rgrid_reset_active"))
        reset_side = str(status.get("rgrid_reset_side") or "none").upper()
        sl_pct = float(status.get("rgrid_stop_loss_pct") or 0.0)
        tp_pct = float(status.get("rgrid_take_profit_pct") or 0.0)
        discretion = float(status.get("rgrid_discretion") or 0.0)
        inventory_source = str(status.get("inventory_source") or "").strip().lower()
        if strategy == "DGRID":
            phase = str(status.get("dgrid_phase") or "grid").upper()
            vr = float(status.get("dgrid_variance_ratio") or 0.0)
            move_bp = float(status.get("dgrid_realized_move_bp") or 0.0)
            reset_bp = float(status.get("dgrid_reset_threshold_bp") or 0.0)
            lines.extend(["", "*Dynamic GRID*"])
            lines.append(
                f"{_loc('Phase')}: *{escape_md(phase)}* \\| "
                f"{_loc('Variance')}: *{escape_md(f'{vr:.2f}')}* \\| "
                f"{_loc('Move')}: *{escape_md(f'{move_bp:.1f}bp')}*"
            )
            lines.append(f"{_loc('Auto reset')}: *{escape_md(f'{reset_bp:.1f}bp')}*")
        else:
            lines.extend(["", "*Reverse GRID*" if strategy == "RGRID" else "*GRID*"])
        lines.append(
            f"{_loc('Anchor')}: *{escape_md(f'{anchor:,.2f}') if anchor > 0 else 'n/a'}* \\| "
            f"{_loc('Drift')}: *{escape_md(f'{drift_pct:.3f}%')}*"
        )
        lines.append(
            f"{_loc('Buy VWAP')}: *{escape_md(f'{buy_exp:,.2f}') if buy_exp > 0 else 'n/a'}* \\| "
            f"{_loc('Sell VWAP')}: *{escape_md(f'{sell_exp:,.2f}') if sell_exp > 0 else 'n/a'}*"
        )
        lines.append(
            f"{_loc('Soft Reset')}: *{escape_md('ON' if reset_active else 'OFF')}* \\| "
            f"{_loc('Side')}: *{escape_md(reset_side)}* \\| "
            f"SL/TP: *{escape_md(f'{sl_pct:.2f}%/{tp_pct:.2f}%')}*"
        )
        # Green/red soft-reset trigger levels: 🟢 = over-short, price must RISE to
        # this to rebalance; 🔴 = over-long, price must FALL to this. The arrow
        # marks the side currently in play based on net exposure.
        up_price = float(status.get("grid_reset_up_price") or 0.0)
        down_price = float(status.get("grid_reset_down_price") or 0.0)
        if up_price > 0 or down_price > 0:
            net_base = float(status.get("grid_net_base") or 0.0)
            watch = " ◀ short" if net_base < 0 else (" ◀ long" if net_base > 0 else "")
            engaged = " \\(engaged\\)" if status.get("grid_soft_reset_engaged") else ""
            lines.append(
                f"{_loc('Reset levels')}: 🟢 *{escape_md(f'{up_price:,.2f}')}* / "
                f"🔴 *{escape_md(f'{down_price:,.2f}')}*{escape_md(watch)}{engaged}"
            )
        maker_fill_ratio = status.get("maker_fill_ratio")
        cancellation_ratio = status.get("cancellation_ratio")
        avg_quote_distance_bp = status.get("avg_quote_distance_bp")
        if maker_fill_ratio is not None or cancellation_ratio is not None or avg_quote_distance_bp is not None:
            lines.append(
                f"{_loc('Quote health')}: "
                f"{_loc('fill')} *{escape_md(f'{float(maker_fill_ratio or 0) * 100:.0f}%')}* \\| "
                f"{_loc('cancel')} *{escape_md(f'{float(cancellation_ratio or 0) * 100:.0f}%')}* \\| "
                f"{_loc('avg distance')} *{escape_md(f'{float(avg_quote_distance_bp or 0):.1f}bp')}*"
            )
        if strategy == "RGRID":
            lines.append(f"{_loc('Discretion')}: *{escape_md(f'{discretion:.2f}')}*")
        if inventory_source and inventory_source != "exchange":
            lines.append(f"{_loc('Inventory source')}: *{escape_md(inventory_source.upper())}*")

    lines.extend(_fmt_copy_mirror_lines(status, _loc))
    return "\n".join(lines)


def _fmt_copy_mirror_lines(status: dict, loc) -> list[str]:
    """Copy Trading block for /status — one line per active mirror with the
    rail-consistent net PnL (realized derived gross + last unrealized - fees)."""
    mirrors = status.get("copy_mirrors") or []
    if not mirrors:
        return []
    lines = ["", f"*{loc('Copy Trading')}*"]
    for m in mirrors:
        net = float(m.get("net_pnl") or 0.0)
        allocated = float(m.get("total_allocated_usd") or 0.0)
        pct = (net / allocated * 100.0) if allocated > 0 else 0.0
        state = loc("PAUSED") if m.get("paused") else loc("LIVE")
        net_str = f"+${net:,.2f}" if net >= 0 else f"-${abs(net):,.2f}"
        lines.append(
            f"🔁 *{escape_md(str(m.get('trader_label') or '?'))}* \\[{state}\\] · "
            f"{loc('Positions:')} {int(m.get('open_positions') or 0)} · "
            f"{loc('Net PnL:')} *{escape_md(net_str)}* \\({escape_md(f'{pct:+.1f}%')}\\) "
            f"{loc('of')} ${escape_md(f'{allocated:,.0f}')}"
        )
    return lines


def fmt_ops_overview(status: dict, ops: dict) -> str:
    runtime_diag = status.get("runtime_diagnostics") or {}
    queue_diag = runtime_diag.get("queue") or {}
    order_obs = status.get("order_observability") or {}

    strategy = str(status.get("strategy") or "none").upper()
    running = bool(status.get("running"))
    global_pause_active = bool(status.get("global_pause_active"))

    lines = [
        "🧪 *Ops Snapshot*",
        md2_rule(),
        f"{_loc('Strategy')}: *{escape_md(strategy)}* \\| {('PAUSED' if global_pause_active else ('ON' if running else 'OFF'))}",
    ]

    if global_pause_active:
        lines.append(f"⚠️ {_loc('Global trading pause is active. Strategy execution is suspended.')}")

    lines.extend(["", f"*{_loc('Queue & Workers')}*"])
    lines.append(
        f"{_loc('Queue')}: *{escape_md(str(int(queue_diag.get('strategy_qsize') or 0)))}*"
        f"/{escape_md(str(int(queue_diag.get('strategy_qmax') or 0)))} \\| "
        f"{_loc('coalesced')}: *{escape_md(str(int(runtime_diag.get('pending_coalesced_ticks') or 0)))}*"
    )

    if order_obs:
        lines.append(
            f"{_loc('Order Flow')}: {_loc('placed')} *{escape_md(str(int(order_obs.get('orders_placed') or 0)))}* · "
            f"{_loc('filled')} *{escape_md(str(int(order_obs.get('orders_filled') or 0)))}* · "
            f"{_loc('cancelled')} *{escape_md(str(int(order_obs.get('orders_cancelled') or 0)))}*"
        )
        lines.append(
            f"{_loc('Cycles')}: *{escape_md(str(int(order_obs.get('ok_cycles') or 0)))}* ok / "
            f"*{escape_md(str(int(order_obs.get('failed_cycles') or 0)))}* failed / "
            f"*{escape_md(str(int(order_obs.get('zero_order_cycles') or 0)))}* orderless"
        )
        last_reason = str(order_obs.get("last_reason") or "").strip()
        if last_reason:
            lines.append(f"{_loc('Last reason')}: {escape_md(last_reason[:160])}")

    queue_stats = queue_diag.get("stats") or {}
    rt_stats = (ops.get("runtime") or {}).get("stats") or {}
    cycle_timeouts = int(rt_stats.get("cycle_timeouts") or 0)
    if cycle_timeouts > 0:
        lines.append(
            f"{_loc('Cycle timeouts')}: *{escape_md(str(cycle_timeouts))}*"
        )
    strategy_workers_running = int(queue_diag.get("strategy_workers_running") or 0)
    strategy_workers_target = int(queue_diag.get("strategy_workers_target") or 0)
    alert_workers_running = int(queue_diag.get("alert_workers_running") or 0)
    alert_workers_target = int(queue_diag.get("alert_workers_target") or 0)
    lines.append(
        f"{_loc('Workers')}: strategy *{escape_md(str(strategy_workers_running))}/{escape_md(str(strategy_workers_target))}* \\| "
        f"alert *{escape_md(str(alert_workers_running))}/{escape_md(str(alert_workers_target))}*"
    )
    lines.append(
        f"{_loc('Queue traffic')}: enq *{escape_md(str(int(queue_stats.get('strategy_enqueued') or 0)))}* \\| "
        f"dedup *{escape_md(str(int(queue_stats.get('strategy_deduped') or 0)))}* \\| "
        f"drop *{escape_md(str(int(queue_stats.get('strategy_dropped') or 0)))}*"
    )

    if order_obs:
        lines.extend(["", f"*{_loc('Order Flow')}*"])
    if order_obs:
        cycles_ok = int(order_obs.get("ok_cycles") or 0)
        cycles_failed = int(order_obs.get("failed_cycles") or 0)
        cycles_zero = int(order_obs.get("zero_order_cycles") or 0)
        lines.append(
            f"{_loc('Orders')}: {_loc('placed')} *{escape_md(str(int(order_obs.get('orders_placed') or 0)))}* \\| "
            f"{_loc('filled')} *{escape_md(str(int(order_obs.get('orders_filled') or 0)))}* \\| "
            f"{_loc('cancelled')} *{escape_md(str(int(order_obs.get('orders_cancelled') or 0)))}*"
        )
        lines.append(
            f"{_loc('Cycles')}: *{escape_md(str(cycles_ok))}* ok \\| "
            f"*{escape_md(str(cycles_failed))}* failed \\| "
            f"*{escape_md(str(cycles_zero))}* orderless"
        )
        last_reason = str(order_obs.get("last_reason") or "").strip()
        if last_reason:
            lines.append(f"{_loc('Last reason')}: {escape_md(last_reason[:120])}")

    perf = ops.get("perf") or {}
    renv = ops.get("runtime_env") or {}
    snap = ops.get("account_snapshot") or {}
    if perf or renv or snap or cycle_timeouts > 0:
        lines.extend(["", "*Diagnostics*"])
    if perf:
        lines.append(
            f"{_loc('Performance')}: active *{escape_md(str(int(perf.get('active_timers') or 0)))}* \\| "
            f"totals *{escape_md(str(int(perf.get('total_records') or 0)))}*"
        )

    if renv:
        lines.append(
            f"{_loc('Runtime settings')}: mode *{escape_md(str(renv.get('NADO_RUNTIME_MODE')))}* \\| "
            f"workers *{escape_md(str(renv.get('NADO_STRATEGY_WORKERS')))}* \\| "
            f"cycle cap *{escape_md(str(renv.get('NADO_STRATEGY_CYCLE_TIMEOUT_SECONDS')))}s*"
        )

    egress_geo = str(ops.get("egress_geo") or "").strip()
    if egress_geo:
        lines.append(escape_md(egress_geo))

    if snap:
        if snap.get("success"):
            lines.append(
                f"{_loc('Account snapshot')}: pos *{escape_md(str(int(snap.get('positions_count') or 0)))}* \\| "
                f"orders *{escape_md(str(int(snap.get('open_orders_count') or 0)))}* \\| "
                f"src *{escape_md(str(snap.get('source') or ''))}*"
            )
        else:
            err = str(snap.get("error") or "").strip()[:120]
            lines.append(
                f"{_loc('Account snapshot')}: *{_loc('unavailable')}*"
                + (f": {escape_md(err)}" if err else "")
            )

    return "\n".join(lines)


def fmt_strategy_update(strategy: str, network: str, conf: dict) -> str:
    notional = float(conf.get("notional_usd", 100.0))
    spread_bp = float(conf.get("spread_bp", 5.0))
    interval_seconds = int(conf.get("interval_seconds", 60))
    tp_pct = float(conf.get("tp_pct", 1.0))
    sl_pct = float(conf.get("sl_pct", 0.5))
    return (
        f"✅ *{escape_md(strategy.upper())} {_loc('updated')}* \\({escape_md(network.upper())}\\)\n\n"
        f"{_loc('Margin')}: {escape_md(f'${notional:,.2f}')}\n"
        f"{_loc('Spread')}: {escape_md(f'{spread_bp:.1f} bp')}\n"
        f"{_loc('Interval')}: {escape_md(f'{interval_seconds}s')}\n"
        f"{_loc('TP')}: {escape_md(f'{tp_pct:.2f}%')}\n"
        f"{_loc('SL')}: {escape_md(f'{sl_pct:.2f}%')}"
    )
