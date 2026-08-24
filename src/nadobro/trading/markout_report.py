"""Mark-out READOUT — turn the ``fill_markouts`` ledger into the one number the
Mid-mode viability decision needs: are our fills net positive after fees, and is
any negativity real adverse selection or just a Nado-vs-HL level offset?

The ledger is written by ``trading/markout_scorer`` (a read-only scheduler job)
and grades every strategy fill at 60s/300s against the Hyperliquid mid. Nothing
here trades, places orders, or mutates state — it only SELECTs and aggregates.

Decision metrics, per horizon (and split by side, because the venue basis
survives averaging across buys and sells while our own half-spread cancels):

* ``net_markout_bp``    — market moved our way minus the round-trip fee. THE
                          headline: positive => the quote was worth posting.
* ``basis_adj_net``     — ``net_markout_bp + side * basis_bp``. Removes a
                          persistent Nado-vs-HL level offset so a venue basis
                          cannot masquerade as (or hide) toxicity. This is the
                          keepable edge against the true forward price.
* ``basis_bp`` (mean)   — the level offset itself, so its size is visible.
* ``adverse_share``     — fraction of fills with negative net mark-out.

Pure aggregation (:func:`summarize_rows`, :func:`verdict`, :func:`format_report`)
is split from the thin DB fetch so the decision logic is unit-tested without a
database. Mirrors the layering of ``markout_scorer`` (grade_fill is pure).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from src.nadobro.quant import markout as mk

# Horizons the durable scorer grades (candle-based). Sub-minute grades live only
# in the in-process HL ring and are not persisted, so the readout speaks to the
# horizons that actually survive in the ledger.
REPORT_HORIZONS_S = (60.0, 300.0)

# Below this, a per-cell number is noise, not evidence — mirrors the scorer's own
# min-sample posture and markout.widen_recommendation's default gate.
MIN_SAMPLES = 30


def _side_sign(side: Optional[str]) -> int:
    """+1 buy / -1 sell, matching markout_scorer._side_sign."""
    return mk.SELL if str(side or "").upper().startswith("S") else mk.BUY


def _median(values: Sequence[float]) -> Optional[float]:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _mean(values: Sequence[float]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _cell(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate one group of graded rows. ``basis_adj`` is computed only over
    rows that carry a basis (t0 had a reference); a row without one still counts
    toward the raw net/gross figures."""
    net = [r["net_markout_bp"] for r in rows if r.get("net_markout_bp") is not None]
    gross = [r["markout_bp"] for r in rows if r.get("markout_bp") is not None]
    basis = [r["basis_bp"] for r in rows if r.get("basis_bp") is not None]
    basis_adj = [
        r["net_markout_bp"] + _side_sign(r.get("side")) * r["basis_bp"]
        for r in rows
        if r.get("net_markout_bp") is not None and r.get("basis_bp") is not None
    ]
    return {
        "n": len(rows),
        "net_median_bp": _median(net),
        "net_mean_bp": _mean(net),
        "gross_median_bp": _median(gross),
        "basis_adj_net_median_bp": _median(basis_adj),
        "basis_mean_bp": _mean(basis),
        "adverse_share": (sum(1 for v in net if v < 0) / len(net)) if net else None,
    }


def summarize_rows(rows: Sequence[Dict[str, Any]]) -> Dict[float, Dict[str, Any]]:
    """Pure: group graded ``fill_markouts`` rows by horizon (with buy/sell
    sub-cells). Rows need keys: horizon_nominal_s, side, markout_bp,
    net_markout_bp, basis_bp."""
    by_h: Dict[float, List[Dict[str, Any]]] = {}
    for r in rows:
        try:
            h = float(r["horizon_nominal_s"])
        except (TypeError, ValueError, KeyError):
            continue
        by_h.setdefault(h, []).append(dict(r))
    out: Dict[float, Dict[str, Any]] = {}
    for h, hrows in sorted(by_h.items()):
        cell = _cell(hrows)
        cell["buy"] = _cell([r for r in hrows if _side_sign(r.get("side")) == mk.BUY])
        cell["sell"] = _cell([r for r in hrows if _side_sign(r.get("side")) == mk.SELL])
        out[h] = cell
    return out


def verdict(summary: Dict[float, Dict[str, Any]], *, min_samples: int = MIN_SAMPLES) -> str:
    """A one-line, honest read of the summary. Conservative by construction:
    it only calls a strategy positive when the evidence clears the sample gate
    AND both the raw and basis-adjusted medians agree."""
    graded = {h: c for h, c in summary.items() if (c.get("n") or 0) >= min_samples}
    if not graded:
        total = sum((c.get("n") or 0) for c in summary.values())
        return (
            f"INSUFFICIENT DATA — {total} graded sample(s), need >={min_samples} per horizon. "
            "Has Mid actually quoted, and is MARKOUT_SCORER_INTERVAL_SECONDS>0? "
            "Run a small Mid session and let the 60s/300s horizons elapse."
        )
    raw = [c["net_median_bp"] for c in graded.values() if c.get("net_median_bp") is not None]
    adj = [c["basis_adj_net_median_bp"] for c in graded.values() if c.get("basis_adj_net_median_bp") is not None]
    if raw and adj and all(v > 0 for v in raw) and all(v > 0 for v in adj):
        return ("POSITIVE (provisional) — net mark-out is positive at every graded horizon, "
                "raw and basis-adjusted. Confirm sub-minute toxicity before scaling.")
    if raw and adj and all(v < 0 for v in raw) and all(v < 0 for v in adj):
        return ("BLEEDING — net mark-out is negative raw AND basis-adjusted at every graded "
                "horizon: real adverse selection, not a venue-basis artifact.")
    if raw and all(v < 0 for v in raw) and adj and any(v >= 0 for v in adj):
        return ("MIXED — raw net mark-out is negative but the basis-adjusted edge is not: the "
                "drag is largely the Nado-vs-HL level offset. Capturing that basis may be viable.")
    return ("MIXED — horizons disagree or hover near zero. More samples needed before a call; "
            "do not scale on this.")


def _fmt(v: Optional[float], width: int = 9, prec: int = 2) -> str:
    return f"{v:>+{width}.{prec}f}" if isinstance(v, (int, float)) else f"{'--':>{width}}"


def format_report(
    summary: Dict[float, Dict[str, Any]],
    *,
    strategy: str,
    network: str,
    lookback_days: float,
) -> str:
    """Human-readable table for the CLI / a status card."""
    total = sum((c.get("n") or 0) for c in summary.values())
    lines = [
        f"Mid-mode mark-out readout  —  strategy={strategy}  network={network}  "
        f"window={lookback_days:g}d  graded_samples={total}",
        f"  (ref = Hyperliquid mid; fee-netted round trip; +bp = market moved our way)",
        "",
        f"  {'horizon':>8} {'n':>6} {'net med':>9} {'net mean':>9} "
        f"{'basisAdj':>9} {'gross med':>9} {'basis':>8} {'adverse%':>8}",
    ]
    for h in sorted(summary):
        c = summary[h]
        adv = c.get("adverse_share")
        lines.append(
            f"  {h:>7.0f}s {c.get('n', 0):>6} "
            f"{_fmt(c.get('net_median_bp'))} {_fmt(c.get('net_mean_bp'))} "
            f"{_fmt(c.get('basis_adj_net_median_bp'))} {_fmt(c.get('gross_median_bp'))} "
            f"{_fmt(c.get('basis_mean_bp'), 8)} "
            + (f"{adv*100:>7.0f}%" if isinstance(adv, (int, float)) else f"{'--':>8}")
        )
        for sd in ("buy", "sell"):
            s = c.get(sd, {})
            if s.get("n"):
                lines.append(
                    f"      {sd:>5} {s.get('n', 0):>6} "
                    f"{_fmt(s.get('net_median_bp'))} {_fmt(s.get('net_mean_bp'))} "
                    f"{_fmt(s.get('basis_adj_net_median_bp'))} {_fmt(s.get('gross_median_bp'))} "
                    f"{_fmt(s.get('basis_mean_bp'), 8)}"
                )
    lines += ["", "  VERDICT: " + verdict(summary)]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Thin DB layer (the only impure part).
# --------------------------------------------------------------------------
_VALID_NETWORKS = frozenset({"testnet", "mainnet"})


def fetch_markout_rows(
    network: str,
    *,
    strategy: Optional[str] = "mid",
    lookback_days: float = 30.0,
    product: Optional[str] = None,
    limit: int = 500_000,
) -> List[Dict[str, Any]]:
    """Graded rows for the window. Read-only. ``strategy=None`` reports across
    all strategies (useful to compare Mid against the grid family)."""
    if network not in _VALID_NETWORKS:
        raise ValueError(f"Invalid network: {network}")
    from src.nadobro.db import query_all

    clauses = ["network = %s", "ts_fill >= now() - make_interval(secs => %s)"]
    params: List[Any] = [network, float(lookback_days) * 86400.0]
    if strategy:
        clauses.append("strategy = %s")
        params.append(strategy)
    if product:
        clauses.append("upper(product_name) = %s")
        params.append(product.upper())
    params.append(int(limit))
    rows = query_all(
        f"""
        SELECT strategy, product_name, side, horizon_nominal_s,
               markout_bp, net_markout_bp, basis_bp, is_taker
          FROM fill_markouts
         WHERE {' AND '.join(clauses)}
         ORDER BY ts_fill DESC
         LIMIT %s
        """,
        tuple(params),
    )
    return [dict(r) for r in (rows or [])]


def markout_report(
    network: str,
    *,
    strategy: Optional[str] = "mid",
    lookback_days: float = 30.0,
    product: Optional[str] = None,
) -> str:
    """Fetch + summarize + format. The one call the CLI / a command makes."""
    rows = fetch_markout_rows(
        network, strategy=strategy, lookback_days=lookback_days, product=product
    )
    summary = summarize_rows(rows)
    return format_report(
        summary, strategy=(strategy or "ALL"), network=network, lookback_days=lookback_days
    )
