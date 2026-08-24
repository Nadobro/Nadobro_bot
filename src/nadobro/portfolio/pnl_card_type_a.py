"""Type A PnL share card — normal trades (Desk/agent + Copy Trading).

``generate_type_a_card(data) -> PNG bytes``. Composites the trade's own stats
onto the master Type A artwork (miner background for a loss, trophy for a
gain), matching ``assets/cards/Master {positive,negative} pnl.png`` exactly.

Type B (strategy sessions) is a separate renderer — this module only handles
the per-trade History cards.
"""
from __future__ import annotations

import io
import logging
from decimal import Decimal
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

_ASSETS: Path = Path(__file__).resolve().parents[3] / "assets"
_CARDS: Path = _ASSETS / "cards"
_LOGOS: Path = _ASSETS / "logos"
_ICONS: Path = _ASSETS / "market_icons"

_BG = {
    True: _CARDS / "Background positive pnl.png",
    False: _CARDS / "Background negative pnl.png",
}
_LOGO_CANDIDATES = (
    _LOGOS / "Nadobro Logo trans v2.png",   # RGBA monogram (preferred)
    _CARDS / "Nadobro Logo trans v2.png",
    _LOGOS / "nadobro logo v2.png",
)
# The official "NADOBRO" wordmark, white on transparent for dark cards (the real
# brand logotype — custom letterforms + green accents — not typeset text). Same
# asset the Type B card uses, so both share cards render identical branding.
_WORDMARK_CANDIDATES = (
    _LOGOS / "nadobro_wordmark_white.png",
    _CARDS / "nadobro_wordmark_white.png",
)

# Palette measured from the masters.
_WHITE = (255, 255, 255)
_MUTED = (150, 166, 186)
_GREEN = (54, 232, 150)         # positive accent
_GREEN_TEXT = (6, 22, 18)       # dark text on the green badge
_RED = (240, 68, 68)            # negative accent
_RED_TEXT = (255, 255, 255)
_BOX_OUTLINE = (54, 74, 104)    # subtle panel outline
_ICON_CYAN = (77, 208, 255)     # brand cyan — the referral gift icon (both variants)
_RING_DIM = (58, 92, 128)       # faint ring around the gift badge

# ── fonts ───────────────────────────────────────────────────────
# The master uses Poppins (geometric sans). Bundled in assets/fonts/; system
# geometric/sans bolds are the fallback so the card still renders on a host
# without the bundled files.
_FONTS = _ASSETS / "fonts"
_BOLD_CHAIN = (
    str(_FONTS / "Poppins-Bold.ttf"),
    str(_FONTS / "Poppins-SemiBold.ttf"),
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
)
_SEMIBOLD_CHAIN = (
    str(_FONTS / "Poppins-SemiBold.ttf"),
    str(_FONTS / "Poppins-Medium.ttf"),
    str(_FONTS / "Poppins-Bold.ttf"),
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)
_REG_CHAIN = (
    str(_FONTS / "Poppins-Regular.ttf"),
    str(_FONTS / "Poppins-Medium.ttf"),
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
)


def _font(size: int, bold: bool = True, semibold: bool = False) -> ImageFont.FreeTypeFont:
    chain = _SEMIBOLD_CHAIN if semibold else (_BOLD_CHAIN if bold else _REG_CHAIN)
    for p in chain:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _text_w(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    l, _t, r, _b = draw.textbbox((0, 0), text, font=font)
    return r - l


# ── helpers ─────────────────────────────────────────────────────

def _load_logo(px: int) -> Optional[Image.Image]:
    for cand in _LOGO_CANDIDATES:
        if not cand.exists():
            continue
        try:
            img = Image.open(cand).convert("RGBA")
            bbox = img.getbbox()          # crop the transparent padding
            if bbox:
                img = img.crop(bbox)
            w, h = img.size
            scale = px / max(w, h)
            return img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        except Exception:
            logger.debug("type-a logo load failed for %s", cand, exc_info=True)
    return None


def _load_wordmark(px_height: int) -> Optional[Image.Image]:
    """The official NADOBRO wordmark scaled to ``px_height`` tall."""
    for cand in _WORDMARK_CANDIDATES:
        if not cand.exists():
            continue
        try:
            img = Image.open(cand).convert("RGBA")
            bbox = img.getbbox()
            if bbox:
                img = img.crop(bbox)
            w, h = img.size
            scale = px_height / h
            return img.resize((max(1, int(w * scale)), px_height), Image.LANCZOS)
        except Exception:
            logger.debug("type-a wordmark load failed for %s", cand, exc_info=True)
    return None


def _load_icon(symbol: str, px: int) -> Optional[Image.Image]:
    if not symbol:
        return None
    cand = _ICONS / f"{symbol.upper()}.png"
    if not cand.exists():
        return None
    try:
        img = Image.open(cand).convert("RGBA")
        return img.resize((px, px), Image.LANCZOS)
    except Exception:
        return None


def _fmt_money(v: float) -> str:
    return f"{v:,.2f}"


def _fmt_signed_dollar(v: float) -> str:
    return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"


def _fmt_size(v: float, symbol: str) -> str:
    d = Decimal(str(v)).normalize()
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return f"{s} {symbol}".strip()


def _fmt_leverage(lev) -> str:
    try:
        f = float(lev)
    except (TypeError, ValueError):
        return ""
    if f <= 0:
        return ""
    return f"{int(f)}X" if f == int(f) else f"{f:g}X"


def _gift_icon(
    canvas: Image.Image, draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int,
    color=_ICON_CYAN, ring_color=_RING_DIM,
) -> None:
    """Clean line-art gift box inside a circular badge, matching the master:
    a ribboned present (box + lid + vertical ribbon + a two-loop bow) in brand
    cyan, ringed by a faint circle. Centered on ``(cx, cy)`` with ring radius
    ``r``."""
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=ring_color, width=2)

    g = r * 0.52                       # gift half-width
    lw = max(2, int(round(r * 0.085)))
    box_l, box_r = cx - g, cx + g
    box_t = cy - g * 0.22
    box_b = cy + g * 1.05
    lid_l, lid_r = cx - g * 1.18, cx + g * 1.18
    lid_t = box_t - g * 0.34

    # box body + lid + vertical ribbon
    draw.rectangle((box_l, box_t, box_r, box_b), outline=color, width=lw)
    draw.rectangle((lid_l, lid_t, lid_r, box_t), outline=color, width=lw)
    draw.line((cx, lid_t, cx, box_b), fill=color, width=lw)

    # bow: two loops sitting ON TOP of the lid, meeting at a centre knot.
    loop_w = max(5, int(g * 0.80))
    loop_h = max(6, int(g * 0.95))
    draw.ellipse((cx - loop_w, lid_t - loop_h, cx, lid_t), outline=color, width=lw)
    draw.ellipse((cx, lid_t - loop_h, cx + loop_w, lid_t), outline=color, width=lw)
    # knot where the two loops meet the lid
    k = max(2, int(lw * 1.2))
    draw.ellipse((cx - k, lid_t - k, cx + k, lid_t + k), fill=color)


# ── main ────────────────────────────────────────────────────────

def generate_type_a_card(data: dict) -> bytes:
    """Render a Type A PnL card to PNG bytes.

    Expected ``data`` keys: badge, product, base_symbol, side, leverage, pnl
    (signed float), entry_price, exit_price, size (base float), referral_code.
    """
    pnl = float(data.get("pnl") or 0.0)
    positive = pnl >= 0
    accent = _GREEN if positive else _RED

    bg_path = _BG[positive]
    canvas = Image.open(bg_path).convert("RGBA")
    W, H = canvas.size
    draw = ImageDraw.Draw(canvas)

    # Outer rounded border (subtle).
    draw.rounded_rectangle((14, 14, W - 14, H - 14), radius=42,
                           outline=(60, 84, 118), width=2)

    # ── header: monogram + official NADOBRO wordmark ────────────
    logo = _load_logo(118)
    lx, ly = 88, 62
    logo_cy = ly + 118 // 2          # monogram vertical centre
    if logo is not None:
        canvas.alpha_composite(logo, (lx, ly + max(0, (118 - logo.height) // 2)))
        word_x = lx + logo.width + 34
    else:
        word_x = lx
    # Prefer the official brand wordmark; scaled to match the monogram (same
    # 38/82 monogram→wordmark ratio the Type B card uses → 54px here).
    wordmark = _load_wordmark(54)
    if wordmark is not None:
        canvas.alpha_composite(wordmark, (word_x, logo_cy - wordmark.height // 2))
    else:
        # Fallback only if the brand asset is missing: typeset the name.
        word_font = _font(70, bold=True)
        wx = word_x
        wy = ly + 20
        for ch in "NADOBRO":
            draw.text((wx, wy), ch, font=word_font, fill=_WHITE)
            wx += _text_w(draw, ch, word_font) + 8

    # ── badge pill (COPY TRADE / DESK TRADE) ────────────────────
    badge = str(data.get("badge") or "TRADE").upper()
    badge_font = _font(34, bold=True)
    bpad_x, bh = 30, 54
    by = 196
    bw = _text_w(draw, badge, badge_font) + bpad_x * 2
    draw.rounded_rectangle((88, by, 88 + bw, by + bh), radius=bh // 2, fill=accent)
    _bt = _RED_TEXT if not positive else _GREEN_TEXT
    draw.text((88 + bpad_x, by + (bh - 34) // 2 - 2), badge, font=badge_font, fill=_bt)

    # ── product row: outlined box + token icon + symbol ─────────
    prow_y0, prow_y1 = 274, 366
    draw.rounded_rectangle((76, prow_y0, 690, prow_y1), radius=22,
                           outline=_BOX_OUTLINE, width=2)
    icon = _load_icon(str(data.get("base_symbol") or ""), 66)
    sym_x = 112
    if icon is not None:
        canvas.alpha_composite(icon, (110, prow_y0 + (prow_y1 - prow_y0 - 66) // 2))
        sym_x = 110 + 66 + 22
    product = str(data.get("product") or "").upper()
    prod_font = _font(52, bold=True)
    draw.text((sym_x, prow_y0 + (prow_y1 - prow_y0 - 52) // 2 - 4), product,
              font=prod_font, fill=_WHITE)

    # ── side + leverage pill ────────────────────────────────────
    side = str(data.get("side") or "").upper()
    lev = _fmt_leverage(data.get("leverage"))
    if side:
        side_font = _font(40, bold=True)
        lev_font = _font(32, bold=True)
        pill_x0, pill_x1 = 716, 940
        draw.rounded_rectangle((pill_x0, prow_y0 + 2, pill_x1, prow_y1 - 2),
                               radius=24, outline=accent, width=2)
        inner = side + ("  " + lev if lev else "")
        tw = _text_w(draw, side, side_font) + (_text_w(draw, "  " + lev, lev_font) if lev else 0)
        tx = pill_x0 + ((pill_x1 - pill_x0) - tw) // 2
        ty = prow_y0 + (prow_y1 - prow_y0 - 40) // 2 - 2
        draw.text((tx, ty), side, font=side_font, fill=accent)
        if lev:
            draw.text((tx + _text_w(draw, side, side_font) + _text_w(draw, "  ", lev_font),
                       ty + 6), lev, font=lev_font, fill=_WHITE)

    # ── Realized PnL ────────────────────────────────────────────
    draw.text((92, 396), "Realized PnL", font=_font(36, bold=False), fill=_MUTED)
    pnl_font = _font(118, bold=True)
    draw.text((88, 446), _fmt_signed_dollar(pnl), font=pnl_font, fill=accent)

    # ── divider ─────────────────────────────────────────────────
    dy = 606
    draw.line((95, dy, 858, dy), fill=accent, width=4)
    draw.ellipse((858, dy - 7, 872, dy + 7), fill=accent)

    # ── stats: Entry / Exit / Size ──────────────────────────────
    lbl_font = _font(30, bold=False)
    val_font = _font(48, bold=True)
    cols = [
        (95, "Entry Price", _fmt_money(float(data.get("entry_price") or 0.0))),
        (402, "Exit Price", _fmt_money(float(data.get("exit_price") or 0.0))),
        (712, "Size", _fmt_size(float(data.get("size") or 0.0),
                                str(data.get("base_symbol") or ""))),
    ]
    for cx, label, value in cols:
        draw.text((cx, 634), label, font=lbl_font, fill=_MUTED)
        draw.text((cx, 672), value, font=val_font, fill=_WHITE)

    # ── referral box ────────────────────────────────────────────
    ref = str(data.get("referral_code") or "").upper()
    if ref:
        rb_y0, rb_y1 = 762, 858
        draw.rounded_rectangle((76, rb_y0, 748, rb_y1), radius=30,
                               outline=_BOX_OUTLINE, width=2)
        _gift_icon(canvas, draw, 150, (rb_y0 + rb_y1) // 2, 40)
        rl_font = _font(34, bold=False)
        rc_font = _font(40, bold=True)
        draw.text((208, rb_y0 + (rb_y1 - rb_y0 - 34) // 2), "Referral Code",
                  font=rl_font, fill=_MUTED)
        div_x = 470
        draw.line((div_x, rb_y0 + 26, div_x, rb_y1 - 26), fill=_BOX_OUTLINE, width=2)
        draw.text((div_x + 40, rb_y0 + (rb_y1 - rb_y0 - 40) // 2), ref,
                  font=rc_font, fill=accent)

    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="PNG")
    return out.getvalue()


if __name__ == "__main__":  # pragma: no cover - manual visual check
    sample = {
        "badge": "COPY TRADE", "product": "ETH:PERP-USDC", "base_symbol": "ETH",
        "side": "LONG", "leverage": 10, "pnl": 428.32,
        "entry_price": 2412.35, "exit_price": 2456.78, "size": 1.25,
        "referral_code": "NADO8RO",
    }
    Path("/tmp/type_a_positive.png").write_bytes(generate_type_a_card(sample))
    Path("/tmp/type_a_negative.png").write_bytes(
        generate_type_a_card({**sample, "badge": "DESK TRADE", "pnl": -428.32})
    )
    print("wrote /tmp/type_a_positive.png and /tmp/type_a_negative.png")
