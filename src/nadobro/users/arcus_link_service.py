"""Arcus account linking: address precheck, paste-key verification, 401
diagnosis, unlink, key lifecycle and egress posture (Arcus P3b).

Async orchestration with no Telegram objects. Every venue read is a PUBLIC,
unauthenticated GET through P2's ``ArcusClient`` (docs: ``/v1/apiKeys`` "No
authentication required — API keys are Ed25519 public keys and are not
secret"; ``/v1/account`` "No authentication header is required"). Nothing here
signs, places or cancels anything.

Owner decisions encoded (build_decisions #2, #3, #5):
- Address FIRST: ``GET /v1/compliance?address=`` (BLOCKED -> refuse; the
  ``geo`` block describes the BOT's egress, not the user), ``GET /v1/account``
  (403 "address not on access whitelist" -> "This address isn't eligible for
  Arcus yet." and nothing stored; the body-matched 404 "this account has no
  activity yet" -> eligible, deposit first), ``GET /v1/apiKeys`` (existing key
  names, so the bot-generated ``nadobro-xxxx`` name can never revoke a key in
  use: docs changelog "re-creating with the same ``apiWalletName`` revokes the
  old key").
- Key: 64-hex seed; a WALLET private key is refused (its eth address == the
  linked address); the Ed25519 pubkey must be listed ACTIVE on
  ``GET /v1/apiKeys?address=`` with ``validUntil`` 0 or > now + 24 h, a scope
  covering subaccount 0 and no ``withdraw``; then Fernet at rest.
- DENIED != EMPTY: 429 / 5xx / schema drift / unknown 404 -> BUSY ("Arcus is
  busy, try again in a moment."), never "not found" / "not eligible";
  ``KEY_NOT_FOUND`` only when the LAST apiKeys read was a 200 (docs: "a 200 is
  safe to treat as the complete set").
- A 401 is never proof that a key is dead (docs place-order: 401 also for a
  timestamp outside ±30 s): :func:`diagnose_key` decides from a 200 listing only.
- No T-24 h stand-down here: P5's ``ArcusScheduler`` owns it (03 §1, V-2).

Coroutine rules: every ``_creds.*`` / ``audit_log.*`` call from an ``async
def`` goes through ``run_blocking_db``; key derivation and sealing run through
``run_blocking``. The pasted text, the seed, the ciphertext and the pubkey are
never logged, echoed, audited or kept in ``user_data``; exceptions on a
key-handling path are logged by type only.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import itertools
import logging
import re
import secrets
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Final, Iterable, Iterator, Literal, cast

from src.nadobro.core import crypto as _crypto
from src.nadobro.core.async_utils import run_blocking, run_blocking_db
from src.nadobro.core.feature_flags import (
    arcus_enabled_for,
    arcus_key_reminder_days,
    arcus_mainnet_enabled,
)
from src.nadobro.users import arcus_credentials as _creds
from src.nadobro.users import audit_log
from src.nadobro.users.arcus_credentials import (
    ArcusCredentialRow,
    SealedSigningKey,
    addr_short,
    format_utc_ms,
)
from src.nadobro.utils.secret_text import (
    SecretShape,
    classify_secret_text,
    collapse_secret_candidate,
    is_wallet_private_key,
)
from src.nadobro.utils.venue_scope import ARCUS_MAINNET_SCOPE, arcus_scope_for, parse_arcus_net
from src.nadobro.venue.arcus import hub
from src.nadobro.venue.arcus.errors import Forbidden, NoActivity, Ok, Throttled
from src.nadobro.venue.arcus.parse import ApiKeyEntry, ComplianceView
from src.nadobro.venue.arcus.signing import normalize_seed_hex
from src.nadobro.venue.arcus.types import ArcusAccountRef, ArcusNet, Lane

logger = logging.getLogger(__name__)

# --- constants -----------------------------------------------------------------------------------

ARCUS_ATTESTATION_VERSION: Final = "2026-09-29-v1"
# apiKeys readiness offsets (docs guides__rest-trading: "A new key takes a moment to
# start authenticating — wait for it", polled for 60 s); <= 7 reads x w20.
_POLL_SCHEDULE_S: Final[tuple[float, ...]] = (0.0, 3.0, 8.0, 15.0, 25.0, 40.0, 60.0)
_RECHECK_S: Final = 600.0  # a precheck younger than this may stand in for a busy re-read at store time
_READ_MAX_WAIT_S: Final = 3.0  # IpBudget wait per read (every read takes lane + max_wait_s)
_MAX_CONCURRENT: Final = 8  # verifications + prechecks in flight process-wide; beyond -> BUSY at once
_DIAGNOSE_MIN_INTERVAL_S: Final = 60.0
# = P2 clock.SKEW_WARN_MS. The client never refuses a write on skew (it signs with the
# measured offset); a large measured skew is only diagnostic here.
_SKEW_SUSPECT_MS: Final = 10_000.0
_MIN_KEY_VALIDITY_MS: Final = 86_400_000  # build_decisions #3: validUntil > now + 24 h
_DAY_MS: Final = 86_400_000
_HOUR_MS: Final = 3_600_000
_STASH_MAX: Final = 1000
_ADDRESS_RE: Final = re.compile(r"^0x[0-9a-f]{40}$")
_KEY_NAME_PREFIX: Final = "nadobro-"
_AUTOMATION_MODULE: Final = "src.nadobro.strategy.arcus_runtime"
_ACTIVE_LIKE: Final = ("active", "expired")

# --- user-facing text keys (English i18n sources; callers escape values + localize) ------------
# Defined HERE (not in handlers/) because runtime/scheduler.py sends the reminders and
# strategy/ renders diagnosis_text, and neither may import handlers/ (03 D-21).

TEXT_BUSY: Final = "Arcus is busy, try again in a moment."
TEXT_PR_NOT_ELIGIBLE: Final = "This address isn't eligible for Arcus yet."
TEXT_R_WALLET_KEY: Final = "That is a WALLET private key. Treat it as exposed and move your funds."

TEXT_D_OK: Final = "✅ Key active on Arcus · valid until {until}."
TEXT_D_OK_NO_EXPIRY: Final = "✅ Key active on Arcus · no expiry."
TEXT_D_SKEW: Final = (
    "Your key is active. Nadobro's clock was off by {seconds}s; it has re-synced with Arcus "
    "and retries. The team has been alerted."
)
TEXT_D_EXPIRED: Final = "Your Arcus key expired on {until}. Paste a new key to trade again."
TEXT_D_REVOKED: Final = "This key is no longer active on Arcus (revoked). Link a new key."
TEXT_D_NAME_REUSE: Final = (
    "This key was replaced: a newer key was created with the same name, <code>{key_name}</code>. "
    "Link a new key with a new name."
)
TEXT_D_WRONG_SCOPE: Final = "This key no longer covers subaccount 0. Link a new key."
TEXT_D_UNKNOWN_401: Final = (
    "Arcus refused a signed request, but your key looks fine. Nadobro holds new Arcus orders "
    "and retries. If this keeps happening, contact support."
)
TEXT_D_NO_CREDENTIAL: Final = "No Arcus key is linked on {network}. Link one in 👛 Arcus wallet."

TEXT_KR_DAYS: Final = (
    "⏳ Your Arcus {network} key <code>{key_name}</code> expires in {days} days ({until}). "
    "Create a new key in the Arcus app with a new name and paste it in 👛 Arcus wallet to renew. "
    "Arcus strategies stop {stop_hours} hours before a key expires and never restart on their own."
)
TEXT_KR_HOURS: Final = (
    "⏳ Your Arcus {network} key <code>{key_name}</code> expires in {hours} hours ({until}). "
    "Arcus strategies stop {stop_hours} hours before a key expires and never restart on their own. "
    "Renew the key in 👛 Arcus wallet."
)
TEXT_KR_EXPIRED: Final = (
    "⚠️ Your Arcus {network} key <code>{key_name}</code> has expired. "
    "Paste a new key in 👛 Arcus wallet to trade on Arcus again."
)

LABEL_RENEW_KEY: Final = "🔄 Renew key"
LABEL_VENUE_KEY: Final = "🔁 Venue"  # == handlers.venue_handler.LABEL_VENUE (P1; pinned by a test)
CB_LINK_START: Final = "ax:link:start"
CB_VENUE_VIEW: Final = "venue:view"

# Every text / label this module owns (all need the 5 translations in i18n.py).
I18N_TEXT_KEYS: Final[tuple[str, ...]] = (
    TEXT_BUSY,
    TEXT_PR_NOT_ELIGIBLE,
    TEXT_R_WALLET_KEY,
    TEXT_D_OK,
    TEXT_D_OK_NO_EXPIRY,
    TEXT_D_SKEW,
    TEXT_D_EXPIRED,
    TEXT_D_REVOKED,
    TEXT_D_NAME_REUSE,
    TEXT_D_WRONG_SCOPE,
    TEXT_D_UNKNOWN_401,
    TEXT_D_NO_CREDENTIAL,
    TEXT_KR_DAYS,
    TEXT_KR_HOURS,
    TEXT_KR_EXPIRED,
)
I18N_LABEL_KEYS: Final[tuple[str, ...]] = (LABEL_RENEW_KEY,)


# --- types ------------------------------------------------------------------------------------


class AddressCheck(Enum):
    ELIGIBLE = "eligible"
    ELIGIBLE_NO_ACTIVITY = "eligible_no_activity"
    NOT_WHITELISTED = "not_whitelisted"
    BLOCKED = "blocked"
    GEO_RESTRICTED = "geo_restricted"
    ALREADY_LINKED_ELSEWHERE = "already_linked_elsewhere"
    AUTOMATION_RUNNING = "automation_running"
    BUSY = "busy"
    DB_UNAVAILABLE = "db_unavailable"
    INVALID = "invalid"


_ELIGIBLE: Final = frozenset({AddressCheck.ELIGIBLE, AddressCheck.ELIGIBLE_NO_ACTIVITY})


@dataclass(frozen=True)
class LinkPending:
    """One user's in-progress link flow. Stored ONLY in
    ``context.user_data["arcus_link_pending"]`` (in memory, TTL). Holds no
    secret and no public key."""

    network: ArcusNet
    step: Literal["attest", "address", "address_check", "key", "verifying"]
    expires_mono: float
    generation: int
    attested_at: datetime | None = None
    renewal: bool = False  # an active/expired credential existed when the flow started
    previous_address: str | None = None  # that credential's address (for [Same address])
    address: str | None = None
    key_name: str | None = None
    address_check: AddressCheck | None = None
    address_checked_mono: float | None = None
    has_activity: bool | None = None


@dataclass(frozen=True)
class AddressPrecheck:
    check: AddressCheck
    existing_names: frozenset[str]  # lowercased apiWalletName values (only when ELIGIBLE*)
    has_activity: bool | None
    checked_mono: float
    geo_country: str | None


class LinkResult(Enum):
    LINKED = "linked"
    LINKED_NO_ACTIVITY = "linked_no_activity"
    WALLET_KEY_REFUSED = "wallet_key_refused"
    INVALID_KEY = "invalid_key"
    KEY_NOT_FOUND = "key_not_found"
    KEY_INACTIVE = "key_inactive"
    KEY_EXPIRES_TOO_SOON = "key_expires_too_soon"
    KEY_WRONG_SUBACCOUNT = "key_wrong_subaccount"
    KEY_HAS_WITHDRAW = "key_has_withdraw"
    NOT_WHITELISTED = "not_whitelisted"
    BLOCKED = "blocked"
    GEO_RESTRICTED = "geo_restricted"
    ALREADY_LINKED_ELSEWHERE = "already_linked_elsewhere"
    BUSY = "busy"
    NOT_ALLOWED = "not_allowed"
    NO_PENDING = "no_pending"
    PENDING_EXPIRED = "pending_expired"
    SUPERSEDED = "superseded"
    AUTOMATION_RUNNING = "automation_running"
    STORE_FAILED = "store_failed"


@dataclass(frozen=True)
class LinkOutcome:
    result: LinkResult
    network: ArcusNet
    address: str | None = None
    row: ArcusCredentialRow | None = None  # set on LINKED*
    previous: ArcusCredentialRow | None = None  # the row before the upsert (renewal info)
    renewed: bool = False  # previous was active/expired with the same address
    has_activity: bool | None = None  # None = unknown (never rendered as "no activity")
    valid_until_ms: int | None = None
    wrong_account_index: int | None = None  # for KEY_WRONG_SUBACCOUNT


@dataclass(frozen=True)
class KeyIntake:
    # "superseded": the flow moved to a newer generation while the key was being
    # sealed — nothing stashed (the caller's generation guard ends the flow silently).
    status: Literal["stashed", "invalid", "pem", "wallet_key", "no_address", "error", "superseded"]


@dataclass(frozen=True)
class KeyEvaluation:
    listing_ok: bool  # the LAST apiKeys read was a 200
    entry: ApiKeyEntry | None  # our key, if listed
    name_collision: bool  # our key absent AND an ACTIVE entry carries stored_name
    reads: int


class KeyVerdict(Enum):
    KEY_OK = "key_ok"
    CLOCK_SKEW = "clock_skew"
    EXPIRED = "expired"
    REVOKED = "revoked"
    REVOKED_NAME_REUSE = "revoked_name_reuse"
    WRONG_SCOPE = "wrong_scope"
    NO_CREDENTIAL = "no_credential"
    UNKNOWN = "unknown"


_DEAD_VERDICTS: Final = frozenset(
    {KeyVerdict.EXPIRED, KeyVerdict.REVOKED, KeyVerdict.REVOKED_NAME_REUSE, KeyVerdict.WRONG_SCOPE}
)


@dataclass(frozen=True)
class KeyDiagnosis:
    verdict: KeyVerdict
    network: ArcusNet
    skew_ms: float | None
    valid_until_ms: int | None
    listing_ok: bool
    key_name: str | None
    from_cache: bool = False

    @property
    def key_dead(self) -> bool:
        """True ONLY from a successful (200) apiKeys listing — never from a 401,
        a 429 or a 5xx alone."""
        return self.listing_ok and self.verdict in _DEAD_VERDICTS


@dataclass(frozen=True)
class EgressPosture:
    network: ArcusNet
    country: str
    perps_restricted: bool
    bypassed: bool
    checked_at: datetime

    @property
    def blocked(self) -> bool:
        return self.perps_restricted and not self.bypassed


class _StashedKey:
    """A sealed pasted key awaiting verification (ciphertext only)."""

    __slots__ = ("sealed", "generation", "seq", "expires_mono")

    def __init__(self, sealed: SealedSigningKey, generation: int, seq: int, expires_mono: float) -> None:
        self.sealed = sealed
        self.generation = generation
        self.seq = seq
        self.expires_mono = expires_mono

    def __repr__(self) -> str:
        return "_StashedKey(<sealed>)"


# --- module state (process-local; fly.toml runs one machine) --------------------------------------

_STASH: dict[tuple[int, str], _StashedKey] = {}
_SEQ: Iterator[int] = itertools.count(1)
_GEN: dict[tuple[int, str], int] = {}
_STORE_LOCKS: dict[tuple[int, str], tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}
_IN_FLIGHT = 0
_DIAG_CACHE: dict[tuple[int, str], tuple[float, KeyDiagnosis]] = {}
_POSTURE: dict[str, EgressPosture] = {}
_LISTENERS: list[Callable[[int, str, str], object]] = []
_SHIELDED: set[asyncio.Task[Any]] = set()


def _services(network: str) -> hub.ArcusServices:
    """Test seam: the per-network Arcus singletons (needs the running loop)."""
    return hub.services(network)


async def _sleep(seconds: float) -> None:
    """Test seam."""
    await asyncio.sleep(seconds)


def _now_ms() -> int:
    """Test seam: wall clock, epoch ms."""
    return time.time_ns() // 1_000_000


def _mono() -> float:
    """Test seam: monotonic clock, seconds."""
    return time.monotonic()


def _reset_for_tests() -> None:
    global _SEQ, _IN_FLIGHT
    _STASH.clear()
    _SEQ = itertools.count(1)
    _GEN.clear()
    _STORE_LOCKS.clear()
    _IN_FLIGHT = 0
    _DIAG_CACHE.clear()
    _POSTURE.clear()
    _LISTENERS.clear()
    _SHIELDED.clear()


def _net(network: object) -> ArcusNet:
    return cast(ArcusNet, parse_arcus_net(network))


def _key(user_id: int, network: object) -> tuple[int, str]:
    return (int(user_id), _net(network))


def _link_allowed(user_id: int, network: str) -> bool:
    """Cohort flag, plus ``ARCUS_MAINNET_ENABLED`` for mainnet (scope compare)."""
    if not arcus_enabled_for(user_id):
        return False
    return arcus_scope_for(network) != ARCUS_MAINNET_SCOPE or arcus_mainnet_enabled()


def _try_acquire() -> bool:
    """Non-blocking slot for a precheck/verification. Loop-agnostic counter (only
    the event-loop thread touches it; no await between check and increment)."""
    global _IN_FLIGHT
    if _IN_FLIGHT >= _MAX_CONCURRENT:
        return False
    _IN_FLIGHT += 1
    return True


def _release() -> None:
    global _IN_FLIGHT
    _IN_FLIGHT = max(0, _IN_FLIGHT - 1)


def _store_lock(k: tuple[int, str]) -> asyncio.Lock:
    """Per-(user, network) lock, created lazily on the RUNNING loop (a lock bound
    to a closed test loop is replaced)."""
    loop = asyncio.get_running_loop()
    held = _STORE_LOCKS.get(k)
    if held is None or held[0] is not loop:
        held = (loop, asyncio.Lock())
        _STORE_LOCKS[k] = held
    return held[1]


async def _audit(user_id: int, action: str, details: str) -> None:
    """Off-loop audit write; never raises (the audited action already happened)."""
    try:
        await run_blocking_db(audit_log.record_audit_event, user_id, action, details)
    except Exception as exc:  # policy: degrade-ok(audit is best-effort; record_audit_event itself never raises)
        logger.warning("arcus audit %s failed uid=%s (%s)", action, user_id, type(exc).__name__)


# --- generations and the stash (03 §7.3) ---------------------------------------------------------


def begin_generation(user_id: int, network: str) -> int:
    """Start a new link generation for (user, network); any older in-flight work
    becomes stale. Returns the new generation."""
    k = _key(user_id, network)
    _GEN[k] = _GEN.get(k, 0) + 1
    return _GEN[k]


def current_generation(user_id: int, network: str) -> int:
    return _GEN.get(_key(user_id, network), 0)


def _purge_stash() -> None:
    now = _mono()
    for k in [k for k, v in _STASH.items() if now >= v.expires_mono]:
        _STASH.pop(k, None)


def _stash_get(k: tuple[int, str]) -> _StashedKey | None:
    _purge_stash()
    return _STASH.get(k)


def _stash_put(k: tuple[int, str], entry: _StashedKey) -> None:
    _purge_stash()
    while len(_STASH) >= _STASH_MAX and k not in _STASH:
        oldest = min(_STASH, key=lambda key: _STASH[key].expires_mono)
        _STASH.pop(oldest, None)
    _STASH[k] = entry


def _drop_if_current(k: tuple[int, str], stash: _StashedKey) -> None:
    """Drop ``stash`` only if it is still the stashed key (never a newer paste)."""
    if _STASH.get(k) is stash:
        _STASH.pop(k, None)


def has_stash(user_id: int, network: str, generation: int) -> bool:
    """A sealed pasted key is waiting for this (user, network, generation)."""
    stash = _stash_get(_key(user_id, network))
    return stash is not None and stash.generation == generation


def drop_stash(user_id: int, network: str) -> None:
    _STASH.pop(_key(user_id, network), None)


def cancel_link(user_id: int, network: str) -> None:
    """End the flow: bump the generation (in-flight work ends SUPERSEDED) and
    drop the stash."""
    begin_generation(user_id, network)
    drop_stash(user_id, network)


# --- key names (03 §7.4) -------------------------------------------------------------------------


def new_key_name(existing_names: Iterable[str]) -> str:
    """``"nadobro-" + 4 lowercase hex`` not among ``existing_names``
    (case-insensitive); 6 hex after 50 collisions. The caller passes the apiKeys
    names plus the stored credential's name, so creating the key under this
    name can never revoke a key in use."""
    taken = {n.lower() for n in existing_names if isinstance(n, str)}
    for attempt in itertools.count():
        width = 2 if attempt < 50 else 3
        name = _KEY_NAME_PREFIX + secrets.token_hex(width)
        if name.lower() not in taken:
            return name
    raise AssertionError("unreachable")  # pragma: no cover


# --- address precheck (03 §7.5) -------------------------------------------------------------------


def _precheck(
    check: AddressCheck,
    *,
    names: frozenset[str] = frozenset(),
    has_activity: bool | None = None,
    country: str | None = None,
) -> AddressPrecheck:
    return AddressPrecheck(
        check=check,
        existing_names=names if check in _ELIGIBLE else frozenset(),
        has_activity=has_activity,
        checked_mono=_mono(),
        geo_country=country,
    )


async def precheck_address(network: str, address: str, *, user_id: int | None) -> AddressPrecheck:
    """Whitelist + compliance + existing key names for ``address`` BEFORE the user
    creates a key. ``user_id=None`` (the owner dry-run script) skips the DB
    checks. Never "not eligible" / "no activity" on a denied read."""
    net = _net(network)
    addr = address.strip().lower() if isinstance(address, str) else ""
    result = await _precheck_inner(net, addr, user_id)
    logger.info(
        "arcus precheck uid=%s net=%s addr=%s -> %s", user_id, net, addr_short(addr), result.check.value
    )
    return result


async def _precheck_inner(net: ArcusNet, addr: str, user_id: int | None) -> AddressPrecheck:
    if not _ADDRESS_RE.match(addr):
        return _precheck(AddressCheck.INVALID)
    stored_name: str | None = None
    if user_id is not None:
        try:
            row = await run_blocking_db(_creds.get_credential, user_id, net)
            owner = await run_blocking_db(_creds.owner_of, net, addr, 0)
        except Exception as exc:
            logger.warning("arcus precheck DB read failed uid=%s net=%s (%s)", user_id, net, type(exc).__name__)
            return _precheck(AddressCheck.DB_UNAVAILABLE)
        if owner is not None and owner != user_id:
            return _precheck(AddressCheck.ALREADY_LINKED_ELSEWHERE)
        if row is not None:
            stored_name = row.api_wallet_name
            if row.status in _ACTIVE_LIKE and row.address != addr and await automation_active(user_id):
                return _precheck(AddressCheck.AUTOMATION_RUNNING)
    if not _try_acquire():
        return _precheck(AddressCheck.BUSY)
    try:
        return await _precheck_reads(net, addr, stored_name)
    except Exception as exc:
        logger.warning("arcus precheck reads failed net=%s (%s)", net, type(exc).__name__)
        return _precheck(AddressCheck.BUSY)
    finally:
        _release()


async def _precheck_reads(net: ArcusNet, addr: str, stored_name: str | None) -> AddressPrecheck:
    client = _services(net).client
    lane, wait = Lane.L2_INTERACTIVE, _READ_MAX_WAIT_S
    c = await client.get_compliance(addr, lane=lane, max_wait_s=wait)  # w1
    if not isinstance(c, Ok):
        return _precheck(AddressCheck.BUSY)
    view = c.value
    note_egress_posture(net, view)
    country = view.country or None
    if view.address_status == "BLOCKED":
        return _precheck(AddressCheck.BLOCKED, country=country)
    if view.address_status is None:
        # We sent ?address=, so the section must be there: its absence is drift,
        # never COMPLIANT (fail closed).
        return _precheck(AddressCheck.BUSY, country=country)
    if view.restrictions_perps and not view.bypassed:
        return _precheck(AddressCheck.GEO_RESTRICTED, country=country)
    a = await client.get_account(ArcusAccountRef(net, addr, 0), lane=lane, max_wait_s=wait)  # w2
    if isinstance(a, Ok):
        has_activity = True
    elif isinstance(a, NoActivity):
        has_activity = False
    elif isinstance(a, Forbidden) and a.kind == "whitelist":
        return _precheck(AddressCheck.NOT_WHITELISTED, country=country)
    elif isinstance(a, Forbidden) and a.kind == "geo":
        return _precheck(AddressCheck.GEO_RESTRICTED, country=country)
    else:  # Throttled / Unavailable / unknown-path NotFound / LocalDenied / … : unknown
        return _precheck(AddressCheck.BUSY, country=country)
    k = await client.get_api_keys(addr, account_index=None, lane=lane, max_wait_s=wait)  # w20, all subaccounts
    if not isinstance(k, Ok):
        # Name uniqueness cannot be proven: fail closed.
        return _precheck(AddressCheck.BUSY, country=country)
    names = {e.api_wallet_name.lower() for e in k.value if e.api_wallet_name}
    if stored_name:
        names.add(stored_name.lower())
    check = AddressCheck.ELIGIBLE if has_activity else AddressCheck.ELIGIBLE_NO_ACTIVITY
    return _precheck(check, names=frozenset(names), has_activity=has_activity, country=country)


# --- key intake: the only place the pasted plaintext is handled (03 §7.6) ------------------------


def _intake_sync(seed: str, address: str) -> tuple[str, SealedSigningKey | None]:
    """Thread-pool payload: the wallet-key refusal, then derive + seal."""
    if is_wallet_private_key(seed, address, derive_address=_crypto.derive_address_from_private_key):
        return ("wallet_key", None)
    return ("ok", _creds.seal_signing_seed(seed))


async def intake_key(*, user_id: int, pending: LinkPending, pasted_text: str) -> KeyIntake:
    """Classify, refuse a wallet key, seal and stash a pasted signing key. The
    plaintext exists only here and in the sealing thread."""
    address = pending.address.strip().lower() if isinstance(pending.address, str) else ""
    if not _ADDRESS_RE.match(address):
        return KeyIntake("no_address")
    shape = classify_secret_text(pasted_text)
    if shape is SecretShape.PEM:
        return KeyIntake("pem")
    if shape is not SecretShape.HEX_KEY:
        return KeyIntake("invalid")
    seed = normalize_seed_hex(collapse_secret_candidate(pasted_text))
    pasted_text = ""
    if seed is None:
        return KeyIntake("invalid")
    try:
        kind, sealed = await run_blocking(_intake_sync, seed, address)
    except Exception as exc:
        logger.warning("arcus key intake failed uid=%s (%s)", user_id, type(exc).__name__)
        return KeyIntake("error")
    finally:
        seed = ""
    if kind == "wallet_key":
        await _audit(user_id, "arcus_wallet_key_pasted", pending.network)
        logger.warning("arcus wallet private key refused uid=%s net=%s", user_id, pending.network)
        return KeyIntake("wallet_key")
    if sealed is None:  # pragma: no cover - _intake_sync returns a key with "ok"
        return KeyIntake("error")
    # Stash only AFTER the await, in the coroutine: a cancelled task never writes.
    if current_generation(user_id, pending.network) != pending.generation:
        return KeyIntake("superseded")
    k = _key(user_id, pending.network)
    _stash_put(k, _StashedKey(sealed, pending.generation, next(_SEQ), pending.expires_mono))
    return KeyIntake("stashed")


# --- key evaluation (pubkey only; no secret, no DB) (03 §7.7) ------------------------------------


async def evaluate_key(
    network: str,
    address: str,
    api_public_key: str,
    *,
    poll: bool,
    stored_name: str | None = None,
) -> KeyEvaluation:
    """Look ``api_public_key`` up on ``GET /v1/apiKeys?address=`` (all subaccounts).
    With ``poll`` the read repeats on :data:`_POLL_SCHEDULE_S` until the key is
    listed. ``listing_ok`` reflects the LAST read only."""
    net = _net(network)
    pub = api_public_key.lower()
    try:
        client = _services(net).client
    except Exception as exc:
        logger.warning("arcus apiKeys client unavailable net=%s (%s)", net, type(exc).__name__)
        return KeyEvaluation(listing_ok=False, entry=None, name_collision=False, reads=0)
    offsets = _POLL_SCHEDULE_S if poll else (0.0,)
    listing_ok = False
    last: list[ApiKeyEntry] = []
    reads = 0
    previous = 0.0
    throttle_wait = 0.0
    for offset in offsets:
        wait = max(offset - previous, throttle_wait)
        previous = offset
        throttle_wait = 0.0
        if wait > 0:
            await _sleep(wait)
        reads += 1
        try:
            r = await client.get_api_keys(
                address, account_index=None, lane=Lane.L2_INTERACTIVE, max_wait_s=_READ_MAX_WAIT_S
            )
        except Exception as exc:
            logger.warning("arcus apiKeys read failed net=%s (%s)", net, type(exc).__name__)
            listing_ok, last = False, []
            continue
        if isinstance(r, Ok):
            listing_ok, last = True, list(r.value)
            entry = next((e for e in last if e.api_key.lower() == pub), None)
            if entry is not None:
                return KeyEvaluation(listing_ok=True, entry=entry, name_collision=False, reads=reads)
        else:
            listing_ok, last = False, []
            if isinstance(r, Throttled):
                throttle_wait = max(0.0, r.retry_after_ms / 1000.0)
    wanted = (stored_name or "").lower()
    name_collision = bool(
        listing_ok
        and wanted
        and any(e.status == "ACTIVE" and (e.api_wallet_name or "").lower() == wanted for e in last)
    )
    return KeyEvaluation(listing_ok=listing_ok, entry=None, name_collision=name_collision, reads=reads)


def key_problem(entry: ApiKeyEntry, *, now_ms: int) -> tuple[LinkResult, int | None] | None:
    """Why a listed key cannot be linked, or None. Order matters (03 §7.7)."""
    if entry.status != "ACTIVE":  # closed enum, exact match
        return (LinkResult.KEY_INACTIVE, None)
    if any(p.lower() == "withdraw" for p in entry.permissions):  # never stored
        return (LinkResult.KEY_HAS_WITHDRAW, None)
    if not (entry.all_subaccounts is True or entry.account_index == 0):
        return (LinkResult.KEY_WRONG_SUBACCOUNT, entry.account_index)
    if entry.valid_until_ms != 0 and entry.valid_until_ms <= now_ms + _MIN_KEY_VALIDITY_MS:
        return (LinkResult.KEY_EXPIRES_TOO_SOON, None)
    return None


# --- automation probe (03 §7.8) -------------------------------------------------------------------


async def automation_active(user_id: int) -> bool:
    """Fail-closed probe of P5's ``strategy.arcus_runtime.has_arcus_automation``.
    Before P5 ships that module no Arcus automation can exist (False). Any error
    (import, DB) reads as RUNNING, so unlink / address change / mode switch are
    refused rather than stranding orders."""
    try:
        mod = sys.modules.get(_AUTOMATION_MODULE)
        if mod is None:
            if importlib.util.find_spec(_AUTOMATION_MODULE) is None:
                return False
            mod = importlib.import_module(_AUTOMATION_MODULE)
        probe = getattr(mod, "has_arcus_automation")
        return bool(await run_blocking_db(probe, user_id))
    except Exception as exc:
        logger.warning("arcus automation probe failed uid=%s (%s); treated as running", user_id, type(exc).__name__)
        return True


# --- verification + store (03 §7.9) ---------------------------------------------------------------


async def verify_and_store(
    *, user_id: int, pending: LinkPending, pasted_secret: str | None = None
) -> LinkOutcome:
    """Verify the stashed (or just pasted) key on Arcus and store it.

    ``pasted_secret=None`` re-verifies the key already stashed for this
    generation ([Check again]). Nothing is stored without an attestation and a
    passed address precheck."""
    net = _net(pending.network)
    addr = pending.address.strip().lower() if isinstance(pending.address, str) else ""

    def out(result: LinkResult, **kw: Any) -> LinkOutcome:
        return LinkOutcome(result=result, network=net, address=addr or None, **kw)

    if (
        pending.step not in ("key", "verifying")
        or not _ADDRESS_RE.match(addr)
        or pending.attested_at is None
        or pending.address_check not in _ELIGIBLE
    ):
        return out(LinkResult.NO_PENDING)
    if not _link_allowed(user_id, net):
        return out(LinkResult.NOT_ALLOWED)
    if pasted_secret is not None:
        intake = await intake_key(user_id=user_id, pending=pending, pasted_text=pasted_secret)
        pasted_secret = None
        if intake.status == "wallet_key":
            return out(LinkResult.WALLET_KEY_REFUSED)
        if intake.status == "error":
            return out(LinkResult.STORE_FAILED)
        if intake.status == "superseded":
            return out(LinkResult.SUPERSEDED)
        if intake.status != "stashed":
            return out(LinkResult.INVALID_KEY)
    k = _key(user_id, net)
    stash = _stash_get(k)
    if stash is None or stash.generation != pending.generation:
        return out(LinkResult.PENDING_EXPIRED)
    if not _try_acquire():
        return out(LinkResult.BUSY)  # stash kept
    try:
        return await _verify_stash(user_id, pending, net, addr, k, stash, out)
    finally:
        _release()


async def _verify_stash(
    user_id: int,
    pending: LinkPending,
    net: ArcusNet,
    addr: str,
    k: tuple[int, str],
    stash: _StashedKey,
    out: Callable[..., LinkOutcome],
) -> LinkOutcome:
    ev = await evaluate_key(net, addr, stash.sealed.api_public_key, poll=True)
    if ev.entry is None:
        # Never KEY_NOT_FOUND unless the last read was a 200. Stash kept either way:
        # the user may not have tapped Authorize yet.
        return out(LinkResult.KEY_NOT_FOUND if ev.listing_ok else LinkResult.BUSY)
    entry = ev.entry
    problem = key_problem(entry, now_ms=_now_ms())
    if problem is not None:
        _drop_if_current(k, stash)  # this key is final
        result, index = problem
        return out(result, valid_until_ms=entry.valid_until_ms, wrong_account_index=index)
    terminal, busy, has_activity = await _fresh_address_gate(net, addr)
    if terminal is not None:
        _drop_if_current(k, stash)  # nothing is stored
        return out(terminal)
    if busy:
        checked = pending.address_checked_mono
        if checked is None or _mono() - checked >= _RECHECK_S:
            return out(LinkResult.BUSY)  # stash kept
        # A fresh precheck verdict stands; activity unknown unless the account read answered.
    try:
        prev = await run_blocking_db(_creds.get_credential, user_id, net)
    except Exception as exc:
        logger.warning("arcus credential read failed uid=%s net=%s (%s)", user_id, net, type(exc).__name__)
        return out(LinkResult.STORE_FAILED)  # stash kept
    if prev is not None and prev.status in _ACTIVE_LIKE and prev.address != addr and await automation_active(user_id):
        _drop_if_current(k, stash)
        return out(LinkResult.AUTOMATION_RUNNING)
    inner = asyncio.get_running_loop().create_task(
        _store_and_announce(user_id, pending, net, addr, k, stash, entry, prev, has_activity, out),
        name=f"arcus-store:{user_id}",
    )
    _SHIELDED.add(inner)
    inner.add_done_callback(_SHIELDED.discard)
    # Shielded: a newer paste cancels this task; once the upsert thread has started
    # the audit record, stash drop and listeners must still run for the stored row.
    return await asyncio.shield(inner)


async def _fresh_address_gate(net: ArcusNet, addr: str) -> tuple[LinkResult | None, bool, bool | None]:
    """Compliance + account re-read at store time: (terminal result, busy, has_activity)."""
    busy = False
    has_activity: bool | None = None
    lane, wait = Lane.L2_INTERACTIVE, _READ_MAX_WAIT_S
    try:
        client = _services(net).client
        c = await client.get_compliance(addr, lane=lane, max_wait_s=wait)
        if isinstance(c, Ok):
            view = c.value
            note_egress_posture(net, view)
            if view.address_status == "BLOCKED":
                return (LinkResult.BLOCKED, False, None)
            if view.address_status is None:
                busy = True
            elif view.restrictions_perps and not view.bypassed:
                return (LinkResult.GEO_RESTRICTED, False, None)
        else:
            busy = True
        a = await client.get_account(ArcusAccountRef(net, addr, 0), lane=lane, max_wait_s=wait)
    except Exception as exc:
        logger.warning("arcus address re-check failed net=%s (%s)", net, type(exc).__name__)
        return (None, True, None)
    if isinstance(a, Ok):
        has_activity = True
    elif isinstance(a, NoActivity):
        has_activity = False
    elif isinstance(a, Forbidden) and a.kind == "whitelist":
        return (LinkResult.NOT_WHITELISTED, False, None)
    elif isinstance(a, Forbidden) and a.kind == "geo":
        return (LinkResult.GEO_RESTRICTED, False, None)
    else:
        busy = True
    return (None, busy, has_activity)


async def _store_and_announce(
    user_id: int,
    pending: LinkPending,
    net: ArcusNet,
    addr: str,
    k: tuple[int, str],
    stash: _StashedKey,
    entry: ApiKeyEntry,
    prev: ArcusCredentialRow | None,
    has_activity: bool | None,
    out: Callable[..., LinkOutcome],
) -> LinkOutcome:
    """Steps 10-14 of 03 §7.9 as one unit. Never raises (it may run detached
    under ``asyncio.shield``, where an escaped exception would be logged with
    its message)."""
    row: ArcusCredentialRow | None = None
    try:
        async with _store_lock(k):
            if current_generation(user_id, net) != pending.generation or _STASH.get(k) is not stash:
                return out(LinkResult.SUPERSEDED)
            if not _link_allowed(user_id, net):  # flags can flip mid-poll
                return out(LinkResult.NOT_ALLOWED)
            attested_at = pending.attested_at
            if attested_at is None:  # checked by verify_and_store; defensive
                return out(LinkResult.NO_PENDING)
            try:
                row = await run_blocking_db(
                    _creds.upsert_active_credential,
                    user_id=user_id,
                    network=net,
                    address=addr,
                    all_subaccounts=bool(entry.all_subaccounts),
                    sealed=stash.sealed,
                    api_wallet_name=entry.api_wallet_name,
                    valid_until_ms=int(entry.valid_until_ms),
                    attested_at=attested_at,
                )
            except _creds.ArcusAddressTaken:
                _drop_if_current(k, stash)
                return out(LinkResult.ALREADY_LINKED_ELSEWHERE)
            except Exception as exc:
                logger.warning("arcus credential store failed uid=%s net=%s (%s)", user_id, net, type(exc).__name__)
                return out(LinkResult.STORE_FAILED)  # stash kept
            _drop_if_current(k, stash)
            _DIAG_CACHE.pop(k, None)  # a cached diagnosis described the previous key
        renewed = prev is not None and prev.status in _ACTIVE_LIKE and prev.address == addr
        await _audit(
            user_id,
            "arcus_linked",
            f"{net} {addr_short(addr)} idx0 renewed={int(renewed)} attest={ARCUS_ATTESTATION_VERSION}",
        )
        await _notify_listeners(user_id, net, "renewed" if renewed else "linked")
        logger.info("arcus linked uid=%s net=%s addr=%s renewed=%s", user_id, net, addr_short(addr), renewed)
        return out(
            LinkResult.LINKED_NO_ACTIVITY if has_activity is False else LinkResult.LINKED,
            row=row,
            previous=prev,
            renewed=renewed,
            has_activity=has_activity,
            valid_until_ms=row.valid_until_ms,
        )
    except Exception as exc:  # safety net: never escape (see docstring)
        logger.warning("arcus store section failed uid=%s net=%s (%s)", user_id, net, type(exc).__name__)
        if row is not None:
            return out(LinkResult.LINKED, row=row, previous=prev, valid_until_ms=row.valid_until_ms)
        return out(LinkResult.STORE_FAILED)


# --- 401 diagnosis (03 §7.10) ----------------------------------------------------------------------


async def diagnose_key(user_id: int, network: str) -> KeyDiagnosis:
    """Why Arcus refused a signed request (after an ``Unauthorized``). A 401 alone
    is never "key dead": only a 200 apiKeys listing can yield ``key_dead``.
    Callers HOLD whatever the verdict. One diagnosis per minute per (user, net)."""
    net = _net(network)
    k = _key(user_id, net)
    cached = _DIAG_CACHE.get(k)
    if cached is not None and _mono() - cached[0] < _DIAGNOSE_MIN_INTERVAL_S:
        return replace(cached[1], from_cache=True)
    try:
        row = await run_blocking_db(_creds.get_credential, user_id, net)
    except Exception as exc:
        logger.warning("arcus diagnose DB read failed uid=%s net=%s (%s)", user_id, net, type(exc).__name__)
        return KeyDiagnosis(KeyVerdict.UNKNOWN, net, None, None, False, None)
    if row is None or row.status == "unlinked":
        return KeyDiagnosis(KeyVerdict.NO_CREDENTIAL, net, None, None, False, None)
    skew: float | None = None
    try:
        svc = _services(net)
        skew = await svc.clock.sync(svc.client, lane=Lane.L2_INTERACTIVE, max_wait_s=_READ_MAX_WAIT_S)
    except Exception as exc:
        logger.warning("arcus diagnose clock sync failed net=%s (%s)", net, type(exc).__name__)
        skew = None
    ev = await evaluate_key(net, row.address, row.api_public_key, poll=False, stored_name=row.api_wallet_name)
    now_ms = _now_ms()
    stored_until = row.valid_until_ms or 0
    entry = ev.entry
    valid_until: int | None = row.valid_until_ms
    if not ev.listing_ok:
        verdict = KeyVerdict.UNKNOWN
    elif entry is None:
        # The docs only say PENDING_DELETE entries are filtered out; whether an EXPIRED
        # key stays listed is unverified. A key that simply expired is not "revoked".
        if stored_until and stored_until <= now_ms:
            verdict = KeyVerdict.EXPIRED
        else:
            verdict = KeyVerdict.REVOKED_NAME_REUSE if ev.name_collision else KeyVerdict.REVOKED
    else:
        valid_until = entry.valid_until_ms
        if entry.status != "ACTIVE":
            verdict = KeyVerdict.REVOKED
        elif entry.valid_until_ms != 0 and entry.valid_until_ms <= now_ms:
            verdict = KeyVerdict.EXPIRED
        elif not (entry.all_subaccounts is True or entry.account_index == 0):
            verdict = KeyVerdict.WRONG_SCOPE
        elif skew is not None and skew > _SKEW_SUSPECT_MS:
            logger.error("ARCUS_CLOCK_SKEW net=%s skew_ms=%.0f", net, skew)
            verdict = KeyVerdict.CLOCK_SKEW
        else:
            verdict = KeyVerdict.KEY_OK
    await _apply_diagnosis(user_id, net, row, verdict, entry)
    diagnosis = KeyDiagnosis(
        verdict=verdict,
        network=net,
        skew_ms=skew,
        valid_until_ms=valid_until,
        listing_ok=ev.listing_ok,
        key_name=row.api_wallet_name,
    )
    _DIAG_CACHE[k] = (_mono(), diagnosis)
    return diagnosis


async def _apply_diagnosis(
    user_id: int,
    net: ArcusNet,
    row: ArcusCredentialRow,
    verdict: KeyVerdict,
    entry: ApiKeyEntry | None,
) -> None:
    """Pubkey-guarded, off-loop status writes. DB errors are logged (type) and
    never change the verdict."""
    pub = row.api_public_key
    try:
        if verdict is KeyVerdict.EXPIRED:
            changed = await run_blocking_db(
                _creds.mark_status, user_id, net, "expired", wipe_secret=False, api_public_key=pub
            )
            if changed and row.status != "expired":
                await _notify_listeners(user_id, net, "expired")
        elif verdict in (KeyVerdict.REVOKED, KeyVerdict.REVOKED_NAME_REUSE, KeyVerdict.WRONG_SCOPE):
            changed = await run_blocking_db(
                _creds.mark_status, user_id, net, "invalid", wipe_secret=False, api_public_key=pub
            )
            if changed and row.status != "invalid":
                await _notify_listeners(user_id, net, "invalid")
                await _audit(user_id, "arcus_key_invalidated", f"{net} {verdict.value}")
        elif verdict in (KeyVerdict.KEY_OK, KeyVerdict.CLOCK_SKEW) and entry is not None:
            await run_blocking_db(
                _creds.touch_verified,
                user_id,
                net,
                api_public_key=pub,
                valid_until_ms=int(entry.valid_until_ms),
                status="active",
            )
    except Exception as exc:
        logger.warning("arcus diagnose status write failed uid=%s net=%s (%s)", user_id, net, type(exc).__name__)


def diagnosis_text(d: KeyDiagnosis, *, after_401: bool) -> tuple[str, dict[str, str]]:
    """(English i18n source key, RAW format values) for a diagnosis. Callers
    escape the values and localize the key."""
    until = _until_text(d.valid_until_ms)
    if d.verdict is KeyVerdict.KEY_OK:
        if after_401:
            return (TEXT_D_UNKNOWN_401, {})
        if not d.valid_until_ms:
            return (TEXT_D_OK_NO_EXPIRY, {})
        return (TEXT_D_OK, {"until": until})
    if d.verdict is KeyVerdict.CLOCK_SKEW:
        seconds = "?" if d.skew_ms is None else f"{d.skew_ms / 1000.0:.0f}"
        return (TEXT_D_SKEW, {"seconds": seconds})
    if d.verdict is KeyVerdict.EXPIRED:
        return (TEXT_D_EXPIRED, {"until": until})
    if d.verdict is KeyVerdict.REVOKED:
        return (TEXT_D_REVOKED, {})
    if d.verdict is KeyVerdict.REVOKED_NAME_REUSE:
        return (TEXT_D_NAME_REUSE, {"key_name": d.key_name or "—"})
    if d.verdict is KeyVerdict.WRONG_SCOPE:
        return (TEXT_D_WRONG_SCOPE, {})
    if d.verdict is KeyVerdict.NO_CREDENTIAL:
        return (TEXT_D_NO_CREDENTIAL, {"network": d.network.upper()})
    return (TEXT_BUSY, {})  # UNKNOWN: the key could not be checked right now


def _until_text(valid_until_ms: int | None) -> str:
    if not valid_until_ms or valid_until_ms <= 0:
        return "—"
    try:
        return format_utc_ms(int(valid_until_ms))
    except ValueError:
        return "—"


# --- unlink (03 §7.11) -------------------------------------------------------------------------------


async def unlink(user_id: int, network: str) -> Literal["unlinked", "refused_running", "none"]:
    """Forget the stored key for (user, network): the ciphertext is wiped and the
    address released. Refused while Arcus automation (or its cleanup) runs, so
    nothing is stranded. Flags are not checked (reducing exposure must always
    work). A DB error on the first read PROPAGATES."""
    net = _net(network)
    row = await run_blocking_db(_creds.get_credential, user_id, net)
    if row is None or row.status == "unlinked":
        cancel_link(user_id, net)
        return "none"
    if await automation_active(user_id):
        return "refused_running"
    changed = await run_blocking_db(_creds.mark_status, user_id, net, "unlinked", wipe_secret=True)
    cancel_link(user_id, net)
    _DIAG_CACHE.pop(_key(user_id, net), None)
    if not changed:
        return "none"
    await _audit(user_id, "arcus_unlinked", f"{net} {addr_short(row.address)}")
    await _notify_listeners(user_id, net, "unlinked")
    logger.info("arcus unlinked uid=%s net=%s addr=%s", user_id, net, addr_short(row.address))
    return "unlinked"


# --- credential listeners (composition-root seam; 03 §7.12) -----------------------------------------


def register_credential_listener(cb: Callable[[int, str, str], object]) -> None:
    """``cb(user_id, network, event)`` with event in {"linked", "renewed",
    "unlinked", "expired", "invalid"}; sync or async. Registering the same
    callable twice is a no-op."""
    if not callable(cb):
        raise ValueError("listener must be callable")
    if cb not in _LISTENERS:
        _LISTENERS.append(cb)


async def _notify_listeners(user_id: int, network: str, event: str) -> None:
    for cb in list(_LISTENERS):
        try:
            result = cb(user_id, network, event)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # policy: degrade-ok(listener)
            logger.warning("arcus credential listener failed event=%s (%s)", event, type(exc).__name__)


# --- key lifecycle (reminders + active->expired; 03 §7.13) -------------------------------------------
# No stand-down here: P5's ArcusScheduler._key_expiry_scan owns the T-24 h
# cancel-only stand-down and reads the credential row (status) as the truth.


@dataclass(frozen=True)
class KeyNoticeState:
    pub: str
    sent: frozenset[int]
    expired_notified: bool

    @classmethod
    def load(cls, raw: object, pub: str) -> KeyNoticeState:
        """The stored state for THIS key; a missing, malformed or other-key
        (renewed) state is a fresh one."""
        fresh = cls(pub=pub, sent=frozenset(), expired_notified=False)
        if not isinstance(raw, dict) or raw.get("pub") != pub:
            return fresh
        sent = raw.get("sent")
        expired = raw.get("expired_notified")
        if not isinstance(sent, list) or not isinstance(expired, bool):
            return fresh
        if any(isinstance(d, bool) or not isinstance(d, int) for d in sent):
            return fresh
        return cls(pub=pub, sent=frozenset(sent), expired_notified=expired)

    def dump(self) -> dict[str, Any]:
        return {"pub": self.pub, "sent": sorted(self.sent), "expired_notified": self.expired_notified}


@dataclass(frozen=True)
class KeyNoticeDecision:
    new_state: KeyNoticeState
    reminder_days: int | None  # threshold to announce now (the most urgent newly crossed)
    expired_now: bool  # first observation of an expired key (announce once)


def decide_key_notices(
    *, valid_until_ms: int, now_ms: int, state: KeyNoticeState, reminder_days: tuple[int, ...]
) -> KeyNoticeDecision:
    """Pure: which reminder (if any) is due now. A bot that was down from T-20 d
    to T-1.5 d sends ONE message (the most urgent), not three."""
    if not valid_until_ms:
        return KeyNoticeDecision(new_state=state, reminder_days=None, expired_now=False)
    remaining = valid_until_ms - now_ms
    if remaining <= 0:
        if state.expired_notified:
            return KeyNoticeDecision(new_state=state, reminder_days=None, expired_now=False)
        new_state = replace(state, sent=state.sent | frozenset(reminder_days), expired_notified=True)
        return KeyNoticeDecision(new_state=new_state, reminder_days=None, expired_now=True)
    due = [d for d in reminder_days if remaining <= d * _DAY_MS and d not in state.sent]
    if not due:
        return KeyNoticeDecision(new_state=state, reminder_days=None, expired_now=False)
    new_state = replace(state, sent=state.sent | frozenset(due))
    return KeyNoticeDecision(new_state=new_state, reminder_days=min(due), expired_now=False)


@dataclass(frozen=True)
class KeyNotice:
    user_id: int
    network: ArcusNet
    api_public_key: str
    key_name: str | None
    valid_until_ms: int
    reminder_days: int | None
    expired_now: bool


async def collect_key_notices(*, now_ms: int | None = None) -> list[KeyNotice]:
    """The reminders / expiry notices due now. Each notice's state is SAVED before
    it is returned (reminders are at-most-once). An ``active`` key found expired
    is moved to ``expired`` (pubkey-guarded). The list read PROPAGATES a DB error
    (the tick sends nothing: never "no keys"); a per-row DB error skips that row
    this tick."""
    now = _now_ms() if now_ms is None else now_ms
    rows = await run_blocking_db(_creds.list_lifecycle_credentials)
    days = arcus_key_reminder_days()
    notices: list[KeyNotice] = []
    for row in rows:
        if not row.valid_until_ms:
            continue
        try:
            raw = await run_blocking_db(_creds.get_key_notice_state, row.user_id, row.network)
            state = KeyNoticeState.load(raw, row.api_public_key)
            decision = decide_key_notices(
                valid_until_ms=row.valid_until_ms, now_ms=now, state=state, reminder_days=days
            )
            if decision.expired_now and row.status == "active":
                changed = await run_blocking_db(
                    _creds.mark_status,
                    row.user_id,
                    row.network,
                    "expired",
                    wipe_secret=False,
                    api_public_key=row.api_public_key,
                )
                if changed:
                    await _notify_listeners(row.user_id, row.network, "expired")
            if decision.new_state != state:
                await run_blocking_db(_creds.save_key_notice_state, row.user_id, row.network, decision.new_state.dump())
        except Exception as exc:
            logger.warning(
                "arcus key lifecycle row skipped uid=%s net=%s (%s)", row.user_id, row.network, type(exc).__name__
            )
            continue
        if decision.reminder_days is not None or decision.expired_now:  # state saved first: at most once
            notices.append(
                KeyNotice(
                    user_id=row.user_id,
                    network=row.network,
                    api_public_key=row.api_public_key,
                    key_name=row.api_wallet_name,
                    valid_until_ms=row.valid_until_ms,
                    reminder_days=decision.reminder_days,
                    expired_now=decision.expired_now,
                )
            )
    return notices


def build_key_notice(
    n: KeyNotice, *, now_ms: int, stop_hours: float
) -> tuple[str, dict[str, str], tuple[tuple[str, str], ...]]:
    """Pure: (English text key, RAW format values, ((label, callback_data), …)).
    The caller escapes the values, localizes the key and labels, builds the markup."""
    fmt: dict[str, str] = {
        "network": parse_arcus_net(n.network).upper(),
        "key_name": n.key_name or "—",
        "until": _until_text(n.valid_until_ms),
        "stop_hours": f"{stop_hours:g}",
    }
    remaining = n.valid_until_ms - now_ms
    if n.expired_now:
        key = TEXT_KR_EXPIRED
    elif (n.reminder_days is not None and n.reminder_days <= 1) or remaining < 2 * _DAY_MS:
        key = TEXT_KR_HOURS
        fmt["hours"] = str(max(1, remaining // _HOUR_MS))
    else:
        key = TEXT_KR_DAYS
        fmt["days"] = str(max(1, remaining // _DAY_MS))
    buttons = ((LABEL_RENEW_KEY, CB_LINK_START), (LABEL_VENUE_KEY, CB_VENUE_VIEW))
    return key, fmt, buttons


# --- egress posture (03 §7.14; docs get-compliance-status: "geo.restrictions is derived
# from the request origin" — the BOT's egress, a platform-level block) ------------------------------


def note_egress_posture(network: str, view: ComplianceView) -> EgressPosture:
    """Record the egress posture; one ERROR on a transition to blocked, one INFO
    on recovery."""
    net = _net(network)
    posture = EgressPosture(
        network=net,
        country=view.country or "",
        perps_restricted=bool(view.restrictions_perps),
        bypassed=bool(view.bypassed),
        checked_at=datetime.now(timezone.utc),
    )
    previous = _POSTURE.get(net)
    _POSTURE[net] = posture
    was_blocked = previous is not None and previous.blocked
    if posture.blocked and not was_blocked:
        logger.error(
            "ARCUS_GEO_RESTRICTED net=%s country=%s — Arcus perps writes will be refused from this egress",
            net,
            posture.country or "?",
        )
    elif was_blocked and not posture.blocked:
        logger.info("arcus egress geo ok net=%s", net)
    return posture


def egress_posture(network: str) -> EgressPosture | None:
    return _POSTURE.get(_net(network))


async def refresh_egress_posture(network: str) -> EgressPosture | None:
    """Keyless ``GET /v1/compliance`` (w1, background lane). A denied read keeps
    the last posture and returns None."""
    net = _net(network)
    try:
        client = _services(net).client
        c = await client.get_compliance(None, lane=Lane.L3_BACKGROUND, max_wait_s=_READ_MAX_WAIT_S)
    except Exception as exc:
        logger.warning("arcus egress compliance probe failed net=%s (%s)", net, type(exc).__name__)
        return None
    if not isinstance(c, Ok):
        return None
    return note_egress_posture(net, c.value)


__all__ = [
    "ARCUS_ATTESTATION_VERSION",
    "TEXT_BUSY",
    "TEXT_PR_NOT_ELIGIBLE",
    "TEXT_R_WALLET_KEY",
    "TEXT_D_OK",
    "TEXT_D_OK_NO_EXPIRY",
    "TEXT_D_SKEW",
    "TEXT_D_EXPIRED",
    "TEXT_D_REVOKED",
    "TEXT_D_NAME_REUSE",
    "TEXT_D_WRONG_SCOPE",
    "TEXT_D_UNKNOWN_401",
    "TEXT_D_NO_CREDENTIAL",
    "TEXT_KR_DAYS",
    "TEXT_KR_HOURS",
    "TEXT_KR_EXPIRED",
    "LABEL_RENEW_KEY",
    "LABEL_VENUE_KEY",
    "I18N_TEXT_KEYS",
    "I18N_LABEL_KEYS",
    "AddressCheck",
    "LinkPending",
    "AddressPrecheck",
    "LinkResult",
    "LinkOutcome",
    "KeyIntake",
    "KeyEvaluation",
    "KeyVerdict",
    "KeyDiagnosis",
    "EgressPosture",
    "KeyNoticeState",
    "KeyNoticeDecision",
    "KeyNotice",
    "begin_generation",
    "current_generation",
    "cancel_link",
    "has_stash",
    "drop_stash",
    "new_key_name",
    "precheck_address",
    "intake_key",
    "evaluate_key",
    "key_problem",
    "automation_active",
    "verify_and_store",
    "diagnose_key",
    "diagnosis_text",
    "unlink",
    "register_credential_listener",
    "decide_key_notices",
    "collect_key_notices",
    "build_key_notice",
    "note_egress_posture",
    "egress_posture",
    "refresh_egress_posture",
]
