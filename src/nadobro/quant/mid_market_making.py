"""Pure Mid Mode market-making execution policy.

The controller is shared by legacy maker strategies, so Mid-specific presets
live here and must be explicitly enabled by the Mid runtime mapping.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MidExecutionProfile:
    name: str
    interval_seconds: int
    spread_multiplier: float
    size_multiplier: float
    quote_ttl_seconds: int


_PROFILES: dict[str, MidExecutionProfile] = {
    "aggressive": MidExecutionProfile("aggressive", 4, 0.75, 1.25, 6),
    "normal": MidExecutionProfile("normal", 8, 1.0, 1.0, 12),
    "passive": MidExecutionProfile("passive", 16, 1.35, 0.70, 24),
}


def normalize_mid_execution_mode(value: object) -> str:
    """Return a supported Mid execution mode, defaulting safely to Normal."""
    name = str(value or "normal").strip().lower()
    return name if name in _PROFILES else "normal"


def resolve_mid_execution_profile(value: object) -> MidExecutionProfile:
    """Resolve the immutable quote-cadence, width and size policy for Mid."""
    return _PROFILES[normalize_mid_execution_mode(value)]