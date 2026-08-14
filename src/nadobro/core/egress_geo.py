"""Egress-IP geolocation guard.

Nado geo-blocks *writes* (place/cancel order — reads are unaffected) from any IP
that geolocates to a restricted territory (US, CA, and OFAC-sanctioned
jurisdictions), rejecting them with ``{"reason":"ip_query_only","blocked":true}``.
See docs/OPS_CLOUDFLARE.md and https://docs.nado.xyz/legal/restricted-territories.

The trap: **Fly region != egress-IP geolocation.** A machine in ``fra`` can still
be assigned a US-registered egress IP from Fly's pool (which skews US). Fly's
static egress is an IPv4+IPv6 pair, and Nado may see *either* on a dual-stack
connect, so BOTH must be verified. Machine recreation can also change the
allocation, so this must be re-checked after every redeploy — which is why it
runs on boot and on a periodic timer rather than as a one-off manual step.

This guard is **advisory**: it loudly logs the egress country and warns when it
is restricted, and exposes the last result for /ops. It does NOT hard-block
trading — a flaky geo lookup must never halt the desk. The authoritative,
reactive defense stays the ``ip_query_only`` write circuit in
``venue/gateway_budget.py``, which opens only on a real Nado rejection.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

from src.nadobro.utils.env import env_int, env_str

logger = logging.getLogger(__name__)

# Nado's Terms of Use (nado.xyz/terms-of-use, retrieved 2026-08) restrict:
# United States, Canada, Republic of Panama, Belarus, Cuba, Iran, North Korea,
# Russia, the Crimea/Donetsk/Luhansk regions of Ukraine, and any jurisdiction
# under comprehensive US/UK sanctions (which covers Syria). These are the
# whole-country entries as ISO 3166-1 alpha-2 codes.
# NOTE: Ukraine is deliberately EXCLUDED — only three regions are restricted, not
# the country, so a country-code match on "UA" would false-flag a legitimate Kyiv
# egress. ipinfo's `region` field is captured on each probe if a region-level
# check is ever needed. Override with NADO_RESTRICTED_COUNTRIES to re-align if
# Nado revises the list.
_DEFAULT_RESTRICTED = "US,CA,PA,BY,CU,IR,KP,RU,SY"

# Public "what is my IP" endpoints. api.ipify.org answers over IPv4 only and
# api6.ipify.org over IPv6 only, which is what lets us probe each family's egress
# independently. Geolocation is a second call to ipinfo.io/<ip>/json.
_DEFAULT_IPV4_ECHO = "https://api.ipify.org"
_DEFAULT_IPV6_ECHO = "https://api6.ipify.org"
_DEFAULT_GEO_URL = "https://ipinfo.io/{ip}/json"
_HTTP_TIMEOUT = 5.0


def restricted_countries() -> set[str]:
    raw = env_str("NADO_RESTRICTED_COUNTRIES", _DEFAULT_RESTRICTED)
    return {c.strip().upper() for c in raw.split(",") if c.strip()}


@dataclass
class EgressProbe:
    family: str  # "ipv4" | "ipv6"
    ip: Optional[str] = None
    country: Optional[str] = None
    org: Optional[str] = None
    region: Optional[str] = None
    error: Optional[str] = None

    @property
    def restricted(self) -> bool:
        return bool(self.country) and self.country.upper() in restricted_countries()


@dataclass
class EgressReport:
    probes: list[EgressProbe] = field(default_factory=list)
    checked_at: float = 0.0

    @property
    def any_restricted(self) -> bool:
        return any(p.restricted for p in self.probes)

    @property
    def verified(self) -> bool:
        """True once at least one family reported a country (clean or not)."""
        return any(p.country for p in self.probes)


_last_report: Optional[EgressReport] = None
_lock = threading.Lock()


def _echo_ip(url: str) -> str:
    resp = requests.get(url, timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.text.strip()


def _geolocate(ip: str, geo_url: str) -> dict:
    resp = requests.get(geo_url.format(ip=ip), timeout=_HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}


def _probe(family: str, echo_url: str, geo_url: str) -> EgressProbe:
    probe = EgressProbe(family=family)
    try:
        probe.ip = _echo_ip(echo_url)
    except Exception as exc:  # noqa: BLE001 — no v6 egress / offline is expected
        probe.error = f"echo failed: {type(exc).__name__}"
        return probe
    try:
        geo = _geolocate(probe.ip, geo_url)
        probe.country = str(geo.get("country") or "").upper() or None
        probe.org = geo.get("org")
        probe.region = geo.get("region")
    except Exception as exc:  # noqa: BLE001
        probe.error = f"geolocate failed: {type(exc).__name__}"
    return probe


def probe_egress() -> EgressReport:
    """Blocking: probe the IPv4 and IPv6 egress country. Never raises."""
    ipv4_echo = env_str("NADO_EGRESS_IPV4_ECHO_URL", _DEFAULT_IPV4_ECHO)
    ipv6_echo = env_str("NADO_EGRESS_IPV6_ECHO_URL", _DEFAULT_IPV6_ECHO)
    geo_url = env_str("NADO_EGRESS_GEO_URL", _DEFAULT_GEO_URL)
    report = EgressReport(
        probes=[
            _probe("ipv4", ipv4_echo, geo_url),
            _probe("ipv6", ipv6_echo, geo_url),
        ],
    )
    return report


def _summarize(report: EgressReport) -> str:
    parts = []
    for p in report.probes:
        if p.country:
            flag = "RESTRICTED" if p.restricted else "ok"
            org = f" {p.org}" if p.org else ""
            parts.append(f"{p.family}={p.ip}[{p.country}{org}] {flag}")
        elif p.error:
            parts.append(f"{p.family}=unavailable({p.error})")
    return "; ".join(parts) if parts else "no egress info"


def evaluate_and_log(report: EgressReport) -> EgressReport:
    """Log the egress geolocation at a level matching the risk. Never raises."""
    global _last_report
    report.checked_at = time.time()
    with _lock:
        _last_report = report

    summary = _summarize(report)
    if report.any_restricted:
        bad = ", ".join(
            f"{p.family} {p.ip} -> {p.country}" for p in report.probes if p.restricted
        )
        logger.error(
            "EGRESS GEO-BLOCK RISK: egress geolocates to a Nado-restricted "
            "territory (%s). Nado will reject order placement/cancel with "
            "ip_query_only. Allocate an allowed-country egress IP — see "
            "docs/OPS_CLOUDFLARE.md. Full: %s",
            bad, summary,
        )
    elif report.verified:
        logger.info("Egress geolocation OK (no restricted territory): %s", summary)
    else:
        logger.warning(
            "Egress geolocation UNVERIFIED (could not reach geo service): %s. "
            "Retrying on the next scheduled check.", summary,
        )
    return report


def last_report() -> Optional[EgressReport]:
    with _lock:
        return _last_report


def status_line() -> str:
    """One-line status for /ops. Cheap: reads the cached last report only."""
    with _lock:
        report = _last_report
    if report is None:
        return "egress geo: not checked yet"
    age = int(time.time() - report.checked_at) if report.checked_at else -1
    verdict = "RESTRICTED" if report.any_restricted else ("ok" if report.verified else "unverified")
    return f"egress geo: {verdict} ({_summarize(report)}; {age}s ago)"


def egress_geo_check_hours() -> int:
    return max(1, env_int("NADO_EGRESS_GEO_CHECK_HOURS", 6))
