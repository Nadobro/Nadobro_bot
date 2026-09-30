#!/usr/bin/env python3
"""OWNER-RUN ONLY. Arcus TESTNET link dry-run (Arcus P3b, 03 §22).

Runs the bot's own link checks for one address — and, optionally, one API
Signing Key — WITHOUT a database, WITHOUT Telegram and WITHOUT storing
anything. Every Arcus call is a public, unauthenticated GET (compliance,
account, apiKeys); nothing is signed, placed or cancelled. An agent never runs
this script with a key.

    cd <repo>
    export ARCUS_PROBE_NETWORK=testnet ARCUS_PROBE_ADDRESS=0x<your address>
    # optional — without it the run is keyless (address precheck only):
    read -rs ARCUS_PROBE_SIGNING_KEY && export ARCUS_PROBE_SIGNING_KEY   # nothing echoes, no history
    .venv/bin/python scripts/arcus_link_dryrun.py [--allow-fly]

Guards (any failure -> exit 2, nothing sent):
- TESTNET ONLY: ``ARCUS_PROBE_NETWORK`` must parse to the testnet token and the
  REST URL must be the documented testnet host.
- Refuses to run on a Fly machine (``FLY_APP_NAME`` / ``FLY_MACHINE_ID``)
  unless ``--allow-fly``.
- The key comes ONLY from ``ARCUS_PROBE_SIGNING_KEY``; it is popped from the
  environment at once and never printed, logged or written. Only a fingerprint
  of its PUBLIC key (sha256[:8]) is shown. A WALLET private key is refused
  before any network call.

Output: the precheck verdict, whether the key is listed, its status, scope,
valid-until date, days left, name, withdraw permission and the verdict.
Exit codes: 0 = the address (keyless) / the key would link; 1 = it would not
(the reason is printed); 2 = refused by a guard; 3 = unexpected error (the
traceback is redacted before printing).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
import time
import traceback
from pathlib import Path
from urllib.parse import urlsplit

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.nadobro.config import ARCUS_TESTNET_REST_DEFAULT, arcus_rest_url  # noqa: E402
from src.nadobro.core.crypto import derive_address_from_private_key  # noqa: E402
from src.nadobro.core.log_redaction import redact_sensitive_text  # noqa: E402
from src.nadobro.users import arcus_link_service as link_service  # noqa: E402
from src.nadobro.users.arcus_credentials import format_utc_ms  # noqa: E402
from src.nadobro.utils.env import env_str  # noqa: E402
from src.nadobro.utils.secret_text import is_wallet_private_key  # noqa: E402
from src.nadobro.utils.venue_scope import ARCUS_TESTNET_SCOPE, arcus_scope_for, parse_arcus_net  # noqa: E402
from src.nadobro.venue.arcus import hub  # noqa: E402
from src.nadobro.venue.arcus.signing import derive_public_key_hex, normalize_seed_hex  # noqa: E402
from src.nadobro.venue.arcus.types import normalize_address  # noqa: E402

KEY_ENV = "ARCUS_PROBE_SIGNING_KEY"
_DAY_MS = 86_400_000
_ELIGIBLE = (link_service.AddressCheck.ELIGIBLE, link_service.AddressCheck.ELIGIBLE_NO_ACTIVITY)


class Refused(Exception):
    """A guard refused the run: exit 2, nothing sent. Messages never carry a secret."""


def fly_guard(allow_fly: bool) -> None:
    if (env_str("FLY_APP_NAME") or env_str("FLY_MACHINE_ID")) and not allow_fly:
        raise Refused("refusing to run on a Fly machine (production egress IP); pass --allow-fly to override")


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def testnet_guard(net: str) -> None:
    """TESTNET ONLY: the network token (a SCOPE compare) AND the REST host."""
    if arcus_scope_for(net) != ARCUS_TESTNET_SCOPE:
        raise Refused("testnet only: ARCUS_PROBE_NETWORK must be the Arcus testnet")
    try:
        rest_host = _host(arcus_rest_url(net))
    except ValueError as exc:  # config's fixed messages never echo the URL
        raise Refused(f"invalid Arcus testnet URL override: {exc}") from None
    if rest_host != _host(ARCUS_TESTNET_REST_DEFAULT):
        raise Refused("testnet only: the testnet REST URL is not the documented Arcus testnet host")


def key_fingerprint(pub_hex: str) -> str:
    return hashlib.sha256(pub_hex.encode("ascii")).hexdigest()[:8]


def read_inputs(allow_fly: bool) -> tuple[str, str, str | None]:
    """(network, address, api public key or None). The seed never leaves this
    function: it is popped from the environment, checked, turned into its
    public key and dropped."""
    raw_key = env_str(KEY_ENV)
    os.environ.pop(KEY_ENV, None)
    seed: str | None = None
    try:
        fly_guard(allow_fly)
        try:
            net = parse_arcus_net(env_str("ARCUS_PROBE_NETWORK"))
        except ValueError:
            raise Refused("testnet only: set ARCUS_PROBE_NETWORK to the Arcus testnet") from None
        testnet_guard(net)
        try:
            address = normalize_address(env_str("ARCUS_PROBE_ADDRESS"))
        except ValueError:
            raise Refused("ARCUS_PROBE_ADDRESS must be a 0x-prefixed 40-hex address") from None
        if not raw_key:
            return net, address, None
        seed = normalize_seed_hex(raw_key)
        if seed is None:
            raise Refused(f"{KEY_ENV} is not a 64-hex API Signing Key (value not shown)")
        # A ValueError here means "not a valid secp256k1 scalar" (not a wallet key);
        # any other failure propagates (exit 3): never guess on a wallet key.
        if is_wallet_private_key(seed, address, derive_address=derive_address_from_private_key):
            raise Refused(link_service.TEXT_R_WALLET_KEY)
        return net, address, derive_public_key_hex(seed)
    finally:
        raw_key = ""
        seed = None


def _yes(flag: bool) -> str:
    return "yes" if flag else "no"


async def run(net: str, address: str, pub: str | None) -> int:
    try:
        pre = await link_service.precheck_address(net, address, user_id=None)
        eligible = pre.check in _ELIGIBLE
        print(f"network: {net.upper()}")
        print(f"precheck: {pre.check.value}")
        if pre.has_activity is not None:
            print(f"account activity: {_yes(pre.has_activity)}" + ("" if pre.has_activity else " (deposit in the Arcus app)"))
        if eligible:
            print(f"existing key names: {len(pre.existing_names)} (a new name: {link_service.new_key_name(pre.existing_names)})")
        if pub is None:
            print("key: not provided (keyless run)")
            print("verdict: " + ("the address can link" if eligible else f"the address cannot link now ({pre.check.value})"))
            return 0 if eligible else 1
        print(f"key fingerprint: {key_fingerprint(pub)}")
        ev = await link_service.evaluate_key(net, address, pub, poll=False)
        if ev.entry is None:
            if ev.listing_ok:
                print("key found: no (not listed for this address — tapped Authorize and signed?)")
                print("verdict: key_not_found")
            else:
                print("key found: unknown (apiKeys unreadable: Arcus busy, try again)")
                print("verdict: busy")
            return 1
        entry = ev.entry
        now_ms = time.time_ns() // 1_000_000
        print("key found: yes")
        print(f"status: {entry.status}")
        print("scope: " + ("all" if entry.all_subaccounts else f"idx {entry.account_index}"))
        if entry.valid_until_ms:
            print(f"valid until: {format_utc_ms(entry.valid_until_ms)}")
            print(f"days left: {max(0, (entry.valid_until_ms - now_ms) // _DAY_MS)}")
        else:
            print("valid until: no expiry")
        print(f"name: {entry.api_wallet_name or '-'}")
        print(f"withdraw: {_yes(any(p.lower() == 'withdraw' for p in entry.permissions))}")
        problem = link_service.key_problem(entry, now_ms=now_ms)
        if problem is not None:
            print(f"verdict: {problem[0].value}")
            return 1
        if not eligible:
            print(f"verdict: the key is fine but the address cannot link now ({pre.check.value})")
            return 1
        print("verdict: the key would link")
        return 0
    finally:
        await hub.shutdown()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OWNER-RUN Arcus testnet link dry-run (no DB, nothing stored)")
    parser.add_argument("--allow-fly", action="store_true")
    args = parser.parse_args(argv)
    try:
        net, address, pub = read_inputs(args.allow_fly)
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr, flush=True)
        return 2
    except Exception:
        print(redact_sensitive_text(traceback.format_exc()), file=sys.stderr, flush=True)
        return 3
    try:
        return asyncio.run(run(net, address, pub))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr, flush=True)
        return 3
    except Exception:
        print(redact_sensitive_text(traceback.format_exc()), file=sys.stderr, flush=True)
        return 3


if __name__ == "__main__":
    import logging

    from src.nadobro.core.log_redaction import RedactingFormatter

    _handler = logging.StreamHandler()
    _handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=logging.WARNING, handlers=[_handler])
    raise SystemExit(main())
