"""Type A PnL share card — normal trades (Desk/agent + Copy Trading).

``generate_type_a_card(data) -> PNG bytes``. Composites the trade's own stats
onto the Type A mascot artwork — the trophy robot for a gain, the sad robot
for a loss (``assets/cards/Background {positive,negative} pnl.png``). Those
backgrounds share the Type B mascot art by design (the per-trade card was
upgraded to match the strategy-session card's look); each card type still owns
its own named background file, so they can diverge again by dropping new art in.

Layout matches the design mockups: an outlined "⚡ DESK/COPY TRADE" badge (brand
blue in both variants), the token icon + symbol, a side-coloured LONG/SHORT
leverage pill (green long / red short — by SIDE, not PnL), the big signed PnL,
and Entry / Exit / Size split by thin vertical rules, plus the referral box.

Type B (strategy sessions) is a separate renderer — this module only handles
the per-trade History cards. Backgrounds are normalised to the 1672×941 design
canvas on load so a re-exported mascot PNG can't silently shift every coord.
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

# The design canvas the coordinates below are calibrated for. Backgrounds are
# normalised to this exact size on load (see ``generate_type_a_card``) so a
# differently-exported mascot PNG can never silently shift every text position —
# the documented trap that broke these cards once (see the card-assets memo).
_DESIGN_W, _DESIGN_H = 1672, 941

# Palette measured from the masters + the new mascot mockups.
_WHITE = (255, 255, 255)
_MUTED = (150, 166, 186)
_GREEN = (54, 232, 150)         # positive PnL accent + LONG pill
_GREEN_TEXT = (6, 22, 18)       # dark text on a green badge (legacy fallback)
_RED = (240, 68, 68)            # negative PnL accent + SHORT pill
_RED_TEXT = (255, 255, 255)
_BADGE_BLUE = (74, 162, 255)    # the ⚡ DESK/COPY TRADE pill — blue in BOTH variants
_BOX_OUTLINE = (54, 74, 104)    # subtle panel outline (referral box)
_DIVIDER = (74, 96, 128)        # thin vertical rules between stat columns
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
    # Lowercase "x" to match the mockups ("LONG 1x" / "LONG 10x").
    return f"{int(f)}x" if f == int(f) else f"{f:g}x"


def _side_color(side: str) -> tuple:
    """The LONG/SHORT pill colour — keyed on SIDE, never on PnL. A long that
    lost money still shows a green LONG pill; a profitable short shows red."""
    return _GREEN if str(side).upper() == "LONG" else _RED


def _lightning(draw: ImageDraw.ImageDraw, x: float, cy: float, h: float, color) -> float:
    """A filled lightning bolt, ``h`` tall, left edge at ``x`` and vertically
    centred on ``cy`` — the mark that prefixes the DESK/COPY TRADE badge.
    Returns the width consumed so the caller can advance the text cursor."""
    w = h * 0.62
    # Classic bolt: down-left to a mid notch, then down; up-right to a mid notch,
    # then up. Normalised to a (w, h) box with the top at ``cy - h/2``.
    top = cy - h / 2.0
    pts = [
        (0.58, 0.00), (0.05, 0.56), (0.40, 0.56),
        (0.30, 1.00), (0.95, 0.38), (0.56, 0.38), (0.74, 0.00),
    ]
    draw.polygon([(x + px * w, top + py * h) for px, py in pts], fill=color)
    return w


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
    # Normalise to the design canvas so every hard-coded coordinate lands where
    # it was calibrated, regardless of the exported mascot PNG's raw size.
    if canvas.size != (_DESIGN_W, _DESIGN_H):
        logger.warning("type-a background %s is %s, resizing to %sx%s",
                       bg_path.name, canvas.size, _DESIGN_W, _DESIGN_H)
        canvas = canvas.resize((_DESIGN_W, _DESIGN_H), Image.LANCZOS)
    W, H = canvas.size
    draw = ImageDraw.Draw(canvas)

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

    # ── badge pill: "⚡ DESK TRADE" — outlined, brand blue in BOTH variants ──
    badge = str(data.get("badge") or "TRADE").upper()
    badge_font = _font(32, bold=True)
    bx0, by, bh = 88, 196, 58
    bpad_x, bolt_h, inner_gap, letter_gap = 28, 30, 14, 3
    txt_w = sum(_text_w(draw, ch, badge_font) + letter_gap for ch in badge) - letter_gap
    bolt_w = bolt_h * 0.62
    bw = int(bpad_x + bolt_w + inner_gap + txt_w + bpad_x)
    draw.rounded_rectangle((bx0, by, bx0 + bw, by + bh), radius=bh // 2,
                           outline=_BADGE_BLUE, width=3)
    b_cy = by + bh / 2
    cx = bx0 + bpad_x
    _lightning(draw, cx, b_cy, bolt_h, _BADGE_BLUE)
    cx += bolt_w + inner_gap
    b_ty = by + (bh - 32) // 2 - 2
    for ch in badge:
        draw.text((cx, b_ty), ch, font=badge_font, fill=_BADGE_BLUE)
        cx += _text_w(draw, ch, badge_font) + letter_gap

    # ── product row: token icon + symbol (no box) ───────────────
    row_cy = 320
    icon = _load_icon(str(data.get("base_symbol") or ""), 68)
    sym_x = 88
    if icon is not None:
        canvas.alpha_composite(icon, (88, int(row_cy - 34)))
        sym_x = 88 + 68 + 22
    product = str(data.get("product") or "").upper()
    prod_font = _font(56, bold=True)
    pl, pt, pr, pb = draw.textbbox((0, 0), product, font=prod_font)
    draw.text((sym_x - pl, int(row_cy - (pt + pb) / 2)), product, font=prod_font, fill=_WHITE)
    prod_end = sym_x + (pr - pl)

    # ── side + leverage pill — coloured by SIDE (LONG green / SHORT red),
    #    NOT by PnL: a losing long still shows a green LONG pill. ─
    side = str(data.get("side") or "").upper()
    lev = _fmt_leverage(data.get("leverage"))
    if side:
        side_col = _side_color(side)
        side_font = _font(40, bold=True)
        lev_font = _font(30, bold=True)
        gap = "  "
        side_w = _text_w(draw, side, side_font)
        gap_w = _text_w(draw, gap, lev_font) if lev else 0
        lev_w = _text_w(draw, lev, lev_font) if lev else 0
        pad, ph = 30, 68
        pill_w = pad + side_w + gap_w + lev_w + pad
        pill_x0 = prod_end + 34
        py0 = int(row_cy - ph / 2)
        draw.rounded_rectangle((pill_x0, py0, pill_x0 + pill_w, py0 + ph),
                               radius=ph // 2, outline=side_col, width=3)
        tx = pill_x0 + pad
        sl, st, sr, sb = draw.textbbox((0, 0), side, font=side_font)
        draw.text((tx - sl, int(row_cy - (st + sb) / 2)), side, font=side_font, fill=side_col)
        if lev:
            lx = tx + side_w + gap_w
            ll, lt, lr, lb = draw.textbbox((0, 0), lev, font=lev_font)
            draw.text((lx - ll, int(row_cy - (lt + lb) / 2)), lev, font=lev_font, fill=_WHITE)

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
    # Thin vertical rules in the gaps between the three stat columns.
    for dx in (372, 682):
        draw.line((dx, 638, dx, 720), fill=_DIVIDER, width=2)

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
        # Referral code renders WHITE in both variants (matches the mockups +
        # the Type B card), never tinted to the PnL colour.
        draw.text((div_x + 40, rb_y0 + (rb_y1 - rb_y0 - 40) // 2), ref,
                  font=rc_font, fill=_WHITE)

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
