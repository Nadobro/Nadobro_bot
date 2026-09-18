"""Percent-of-Volume (POV) participation engine.

Maps a target notional and a participation preset (Aggressive / Normal /
Passive) to a duration / cycle cadence / per-cycle notional, using the pair's
rolling 24h volume from the Nado archive ``/market_snapshots`` endpoint.

Multipliers are per-minute participation rates against the pair's 24h volume.
Higher multipliers complete a target faster; lower multipliers stretch it across
more cycles.

One formula, one answer
=======================
``cycle_notional_usd`` is what the BOT actually places each cycle, and both the
pre-trade card and the live sizing path go through it. They used to disagree:
the card rendered ``compute_pov_duration``'s ``notional / cycles`` over a
``60 / multiplier`` second cycle — a cadence the bot never ran — while the live
path used the cadence-correct ``rate x volume_per_minute x cycle_minutes``. On a
$10M-volume pair the card promised "$4,900 every 1200s" against a real $347
every 60s.
"""

from __future__ import annotations

from typing import Mapping


# Per-minute participation rates documented by Tread Fi:
# Aggressive 10%, Normal 5%, Passive 1%.
PARTICIPATION_MULTIPLIERS: Mapping[str, float] = {
    "aggressive": 0.10,
    "normal": 0.05,
    "passive": 0.01,
}

DEFAULT_PRESET = "normal"
PRESET_NAMES: tuple[str, ...] = ("aggressive", "normal", "passive")

# Throughput floor for a participation cycle. Sizing purely off the participation
# rate produces cycles far too small to build meaningful volume on a venue as
# thin as Nado (5%/min of a $1M-a-day pair is ~$35 a minute). The floor is
# ALWAYS capped by the deployed budget, so it can never place money the user has
# not allocated — but on a thin pair it does mean participating well above the
# preset's nominal rate. That is the deliberate trade for a volume product.
MIN_CYCLE_NOTIONAL_USD = 1000.0

# Reference cadence for the throughput target: the floor above is meant to
# deliver >= MIN_CYCLE_NOTIONAL_USD per <= this many seconds. Every shipped MM
# cadence already clears it (grid 45s, mid 60s), so the target holds out of the
# box. It is NOT applied as a clamp: a POV rate is defined per unit time, so a
# user who deliberately picks a longer interval must get a proportionally
# LARGER cycle, not the same cycle stretched thinner. Clamping the sizing
# cadence did the opposite — it shrank the chunk on long intervals and made the
# pre-trade card print a cadence the bot never ran.
THROUGHPUT_REFERENCE_SECONDS = 200

# Sentinel cap for cadence math when volume is unknown / zero.
_MIN_VOLUME_PER_MINUTE_FLOOR = 1e-9


def normalize_preset(preset: str | None) -> str:
    text = (preset or "").strip().lower()
    if text in PARTICIPATION_MULTIPLIERS:
        return text
    return DEFAULT_PRESET


def participation_rate(preset: str | None) -> float:
    """Per-minute participation fraction for the given preset."""
    return float(PARTICIPATION_MULTIPLIERS.get(normalize_preset(preset), PARTICIPATION_MULTIPLIERS[DEFAULT_PRESET]))


def effective_cycle_interval_seconds(raw_interval_seconds: object) -> int:
    """The bot's REAL cadence, normalised. Unset/invalid falls back to 60s.

    Deliberately not clamped — see ``THROUGHPUT_REFERENCE_SECONDS``. The card
    and the live sizing path both call this, so whatever it returns is what the
    user is shown AND what the bot places against.
    """
    try:
        raw = int(float(raw_interval_seconds or 0))
    except (TypeError, ValueError):
        raw = 0
    if raw <= 0:
        raw = 60
    return max(1, raw)


def cycle_notional_usd(
    preset: str,
    pair_24h_volume_usd: float,
    interval_seconds: object,
    deployed_usd: float,
    *,
    venue_min_notional_usd: float = 0.0,
) -> float:
    """Per-cycle notional the bot actually places.

    ``rate x volume_per_minute x cycle_minutes`` — the participation rate applied
    over the bot's REAL cadence — then floored at ``MIN_CYCLE_NOTIONAL_USD`` (and
    the venue minimum) and finally capped at the deployed budget. The cap is the
    invariant that matters: one cycle can never exceed the notional the user
    actually allocated, whatever the floor says.

    This is the single source of truth for both the pre-trade card and the live
    sizing path; they disagreed for as long as there were two formulas.
    """
    deployed = max(0.0, float(deployed_usd or 0.0))
    if deployed <= 0:
        return 0.0
    rate = participation_rate(preset)
    vol_per_minute = max(0.0, float(pair_24h_volume_usd or 0.0)) / 1440.0
    cycle_minutes = effective_cycle_interval_seconds(interval_seconds) / 60.0
    chunk = rate * vol_per_minute * cycle_minutes
    chunk = max(chunk, MIN_CYCLE_NOTIONAL_USD, max(0.0, float(venue_min_notional_usd or 0.0)))
    return min(chunk, deployed)


def compute_pov_duration(
    notional_usd: float,
    preset: str,
    pair_24h_volume_usd: float,
    interval_seconds: object = None,
) -> dict:
    """Compute duration / cadence / per-cycle notional for a POV preset.

    ``notional_usd`` — total USD notional the user wants to push through the
    book over the lifetime of the strategy.
    ``preset`` — "aggressive" / "normal" / "passive".
    ``pair_24h_volume_usd`` — rolling 24h USD volume on the pair, sourced from
    Nado archive ``/market_snapshots``.

    Returns a dict with ``duration_minutes``, ``interval_seconds``,
    ``cycle_notional_usd``, ``multiplier``, and the resolved preset.
    """
    multiplier = participation_rate(preset)
    notional = max(0.0, float(notional_usd or 0.0))
    pair_volume = max(0.0, float(pair_24h_volume_usd or 0.0))

    # 1440 minutes in 24h. We never divide by zero — clamp the per-minute volume
    # floor so missing/empty archive responses fall back to a long but finite
    # duration instead of inf or NaN.
    vol_per_minute = max(pair_volume / 1440.0, _MIN_VOLUME_PER_MINUTE_FLOOR)
    duration_minutes = notional / max(multiplier * vol_per_minute, _MIN_VOLUME_PER_MINUTE_FLOOR)
    # Cadence: the bot's REAL interval. The legacy fallback was
    # ``60 / multiplier`` — 1200s for the Normal preset — which had nothing to
    # do with how often the bot ticks and is what put "cycle $100 every 1200s"
    # on the card for a strategy running every 60s. Callers pass the real
    # interval; an absent one falls back to the same 60s default the rest of
    # the sizing path uses.
    cycle_seconds = effective_cycle_interval_seconds(interval_seconds)
    per_cycle = cycle_notional_usd(
        preset, pair_volume, cycle_seconds, notional
    )
    cycles = (notional / per_cycle) if per_cycle > 0 else 1.0

    return {
        "preset": normalize_preset(preset),
        "multiplier": multiplier,
        "duration_minutes": duration_minutes,
        "interval_seconds": cycle_seconds,
        "cycle_notional_usd": per_cycle,
        "cycles": cycles,
        "pair_24h_volume_usd": pair_volume,
        "vol_per_minute_usd": vol_per_minute,
    }
