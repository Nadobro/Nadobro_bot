#!/usr/bin/env python3
"""Check the egress IP geolocation for a Nado-restricted territory.

Nado geo-blocks order placement/cancel from IPs that geolocate to a restricted
territory (US, CA, OFAC-sanctioned). Fly's egress IP geolocation is INDEPENDENT
of the region, so a 'fra' machine can still egress via a US-registered IP. Run
this from INSIDE the deployed machine to check what Nado actually sees:

    fly ssh console -a nadobro-bot -C "python scripts/check_egress_geo.py"

Or locally to sanity-check the logic (it will report YOUR machine's egress).

Exit code: 0 = clean/unverified, 2 = egress geolocates to a restricted territory
(so a release pipeline can fail the deploy). Restricted set is tunable via
NADO_RESTRICTED_COUNTRIES. See docs/OPS_CLOUDFLARE.md.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.nadobro.core.egress_geo import (  # noqa: E402
    probe_egress,
    restricted_countries,
)


def main() -> int:
    restricted = restricted_countries()
    print(f"Restricted territories: {', '.join(sorted(restricted))}")
    report = probe_egress()
    any_bad = False
    for p in report.probes:
        if p.country:
            org = f" {p.org}" if p.org else ""
            region = f" {p.region}" if p.region else ""
            verdict = "RESTRICTED ❌" if p.restricted else "OK ✅"
            print(f"  {p.family}: {p.ip} -> {p.country}{region}{org}  {verdict}")
            any_bad = any_bad or p.restricted
        else:
            print(f"  {p.family}: unavailable ({p.error})")

    if any_bad:
        print(
            "\nEGRESS GEO-BLOCK RISK: at least one egress family geolocates to a "
            "Nado-restricted territory. Nado will reject writes with ip_query_only.\n"
            "Remediate (see docs/OPS_CLOUDFLARE.md):\n"
            "  fly ips release <egress-ip> -a nadobro-bot\n"
            "  fly ips allocate-egress -a nadobro-bot -r fra\n"
            "  fly ips list -a nadobro-bot   # re-verify with this script after"
        )
        return 2
    if not report.verified:
        print("\nCould not verify egress country (geo service unreachable). Retry.")
        return 0
    print("\nEgress geolocation is clean (no restricted territory).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
