"""Arcus API credentials: CRUD over ``arcus_credentials`` (Arcus P3b).

The table is P1's migration 0022 (one row per (user, Arcus network); a partial
unique index keeps one ACTIVE owner per (network, address, account_index)).
The pasted "API Signing Key" (a 32-byte Ed25519 seed, docs
``guides__rest-trading.md``) is sealed ONCE at intake with the server Fernet
key (``core.crypto.encrypt_with_server_key``) and stored as
``base64(Fernet token)`` — the same double-base64 format as
``users.user_service.save_linked_signer``. It becomes plaintext again only
inside :func:`load_auth`, which hands it straight to an ``Ed25519Signer``.

Rules:
- No row object, log line, audit record or exception message ever carries the
  seed or the ciphertext (:data:`_ROW_COLS` never selects it).
- Every function here is SYNC and may hit Postgres: a coroutine calls it only
  through ``core.async_utils.run_blocking_db``.
- DB errors PROPAGATE (DENIED != EMPTY): callers say "couldn't read", never
  "not linked".
- Status transitions: ``active`` (usable) -> ``expired`` (validUntil passed;
  ciphertext kept for cancel-only use) / ``invalid`` (no longer listed ACTIVE;
  ciphertext kept so a re-listed key can be revived) / ``unlinked`` (ciphertext
  wiped to ``''``). An ``unlinked`` row is terminal until a new link upserts it.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final, Literal, NoReturn, SupportsIndex, cast

from cryptography.fernet import InvalidToken

from src.nadobro import db as _db
from src.nadobro.core.crypto import decrypt_with_server_key, encrypt_with_server_key
from src.nadobro.models.database import get_bot_state, set_bot_state
from src.nadobro.utils.venue_scope import ARCUS_KEY_NOTICE_PREFIX, arcus_scope_for, parse_arcus_net
from src.nadobro.venue.arcus.signing import (
    ArcusAuth,
    Ed25519Signer,
    derive_public_key_hex,
    make_auth,
    normalize_seed_hex,
)
from src.nadobro.venue.arcus.types import ArcusAccountRef, ArcusNet

__all__ = [
    "ArcusCredStatus",
    "ArcusCredentialRow",
    "SealedSigningKey",
    "ArcusCredentialError",
    "ArcusAddressTaken",
    "addr_short",
    "format_utc_ms",
    "seal_signing_seed",
    "get_credential",
    "get_active_credential",
    "get_credentials_for_user",
    "list_active_credentials",
    "list_lifecycle_credentials",
    "owner_of",
    "upsert_active_credential",
    "mark_status",
    "touch_verified",
    "load_auth",
    "get_key_notice_state",
    "save_key_notice_state",
    "key_notice_key",
]

ArcusCredStatus = Literal["active", "invalid", "expired", "unlinked"]
_STATUSES: Final = ("active", "invalid", "expired", "unlinked")
_ADDRESS_RE: Final = re.compile(r"^0x[0-9a-f]{40}$")
_PUB_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_NAME_MAX: Final = 256  # the venue caps names at 64; this is only a tolerant storage bound
_UNIQUE_VIOLATION: Final = "23505"
_NOT_SERIALIZABLE: Final = "SealedSigningKey is not serializable"

_ROW_COLS: Final = (
    "user_id, network, address, account_index, all_subaccounts, api_public_key, "
    "api_wallet_name, valid_until_ms, status, attested_at, linked_at, last_verified_at"
)


class ArcusCredentialError(Exception):
    """A stored key could not be decrypted, or does not match its public key.
    The message is fixed text: never key material."""


class ArcusAddressTaken(Exception):
    """Another user holds an ACTIVE credential for (network, address, 0)."""


# --- row type -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ArcusCredentialRow:
    """One ``arcus_credentials`` row WITHOUT any secret material."""

    user_id: int
    network: ArcusNet
    address: str
    account_index: int
    all_subaccounts: bool
    api_public_key: str
    api_wallet_name: str | None
    valid_until_ms: int | None
    status: ArcusCredStatus
    attested_at: datetime | None
    linked_at: datetime
    last_verified_at: datetime | None

    def ref(self) -> ArcusAccountRef:
        return ArcusAccountRef(self.network, self.address, self.account_index)

    def expires_at_ms(self) -> int | None:
        """The key's expiry (epoch ms); None when ``valid_until_ms`` is 0 (no
        expiry) or NULL."""
        return self.valid_until_ms if self.valid_until_ms else None


def _row(d: dict[str, Any]) -> ArcusCredentialRow:
    network = cast(ArcusNet, parse_arcus_net(d.get("network")))  # exact token, else ValueError
    status = d.get("status")
    if status not in _STATUSES:
        raise ValueError("unexpected arcus credential status")
    valid_until = d.get("valid_until_ms")
    return ArcusCredentialRow(
        user_id=int(d["user_id"]),
        network=network,
        address=str(d["address"]),
        account_index=int(d["account_index"]),
        all_subaccounts=bool(d["all_subaccounts"]),
        api_public_key=str(d["api_public_key"]),
        api_wallet_name=d.get("api_wallet_name") or None,
        valid_until_ms=None if valid_until is None else int(valid_until),
        status=cast(ArcusCredStatus, status),
        attested_at=d.get("attested_at"),
        linked_at=d["linked_at"],
        last_verified_at=d.get("last_verified_at"),
    )


# --- sealed seed ----------------------------------------------------------------------------


class SealedSigningKey:
    """A pasted signing key, sealed at intake: the derived public key plus the
    Fernet token of the 32 seed bytes. Never holds plaintext. Immutable; its
    ``repr``/``str`` are redacted and it refuses pickling, copying and
    ``dataclasses.asdict`` (it is not a dataclass), like P2's Ed25519Signer."""

    __slots__ = ("_api_public_key", "_token")
    _api_public_key: str
    _token: bytes

    def __init__(self, api_public_key: str, token: bytes) -> None:
        if not isinstance(api_public_key, str) or not _PUB_RE.match(api_public_key):
            raise ValueError("invalid api public key")
        if not isinstance(token, (bytes, bytearray)) or not token:
            raise ValueError("invalid sealed token")
        object.__setattr__(self, "_api_public_key", api_public_key)
        object.__setattr__(self, "_token", bytes(token))

    @property
    def api_public_key(self) -> str:
        return self._api_public_key

    @property
    def token(self) -> bytes:
        return self._token

    def __setattr__(self, name: str, value: object) -> NoReturn:
        raise AttributeError("SealedSigningKey is immutable")

    def __delattr__(self, name: str) -> NoReturn:
        raise AttributeError("SealedSigningKey is immutable")

    def __repr__(self) -> str:
        return "SealedSigningKey(<sealed>)"

    __str__ = __repr__

    def __reduce__(self) -> NoReturn:
        raise TypeError(_NOT_SERIALIZABLE)

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        raise TypeError(_NOT_SERIALIZABLE)

    def __getstate__(self) -> NoReturn:
        raise TypeError(_NOT_SERIALIZABLE)

    def __copy__(self) -> NoReturn:
        raise TypeError(_NOT_SERIALIZABLE)

    def __deepcopy__(self, memo: object) -> NoReturn:
        raise TypeError(_NOT_SERIALIZABLE)


def seal_signing_seed(seed_hex: str) -> SealedSigningKey:
    """Derive the Ed25519 public key and Fernet-encrypt the seed bytes: the ONLY
    place a plaintext seed becomes ciphertext. Pure CPU + Fernet (call it off
    the loop). ``ValueError("invalid signing key")`` never echoes the input."""
    seed = normalize_seed_hex(seed_hex)
    if seed is None:
        raise ValueError("invalid signing key")
    pub = derive_public_key_hex(seed)
    token = encrypt_with_server_key(bytes.fromhex(seed))
    del seed
    return SealedSigningKey(pub, token)


# --- pure helpers (no IO, never log) ---------------------------------------------------------


def addr_short(address: object) -> str:
    """``"0x1234…abcd"`` (first 6 + ellipsis + last 4); ``"—"`` for a non-str or
    too-short value."""
    if not isinstance(address, str) or len(address) < 12:
        return "—"
    return f"{address[:6]}…{address[-4:]}"


def format_utc_ms(epoch_ms: int) -> str:
    """``"YYYY-MM-DD HH:MM UTC"`` for epoch milliseconds > 0, else ``ValueError``."""
    if isinstance(epoch_ms, bool) or not isinstance(epoch_ms, int) or epoch_ms <= 0:
        raise ValueError("epoch ms must be a positive int")
    try:
        when = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise ValueError("epoch ms out of range") from None
    return when.strftime("%Y-%m-%d %H:%M UTC")


# --- validation (fixed messages, never the input) --------------------------------------------


def _uid(user_id: object) -> int:
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("invalid user id")
    return user_id


def _net(network: object) -> str:
    try:
        return parse_arcus_net(network)
    except ValueError:
        raise ValueError("invalid arcus network") from None


def _address(address: object) -> str:
    if not isinstance(address, str) or not _ADDRESS_RE.match(address):
        raise ValueError("invalid arcus address")
    return address


def _pub(api_public_key: object) -> str:
    if not isinstance(api_public_key, str) or not _PUB_RE.match(api_public_key):
        raise ValueError("invalid api public key")
    return api_public_key


def _valid_until(valid_until_ms: object) -> int:
    if isinstance(valid_until_ms, bool) or not isinstance(valid_until_ms, int) or valid_until_ms < 0:
        raise ValueError("invalid valid_until_ms")
    return valid_until_ms


def _name(api_wallet_name: object) -> str | None:
    if api_wallet_name is None or api_wallet_name == "":
        return None
    if not isinstance(api_wallet_name, str) or len(api_wallet_name) > _NAME_MAX:
        raise ValueError("invalid api wallet name")
    return api_wallet_name


def _is_unique_violation(exc: BaseException) -> bool:
    # Checked on pgcode (no psycopg2.errors import: test stubs may lack it).
    return getattr(exc, "pgcode", None) == _UNIQUE_VIOLATION


# --- reads ------------------------------------------------------------------------------------


def get_credential(user_id: int, network: ArcusNet) -> ArcusCredentialRow | None:
    """The user's row for ``network`` in ANY status; None when there is none.
    Raises on a DB error."""
    uid, net = _uid(user_id), _net(network)
    row = _db.query_one(
        f"SELECT {_ROW_COLS} FROM arcus_credentials WHERE user_id = %s AND network = %s",
        (uid, net),
    )
    return _row(row) if row else None


def get_active_credential(user_id: int, network: ArcusNet) -> ArcusCredentialRow | None:
    uid, net = _uid(user_id), _net(network)
    row = _db.query_one(
        f"SELECT {_ROW_COLS} FROM arcus_credentials "
        "WHERE user_id = %s AND network = %s AND status = 'active'",
        (uid, net),
    )
    return _row(row) if row else None


def get_credentials_for_user(user_id: int) -> dict[str, ArcusCredentialRow]:
    """``{network: row}`` for every row of the user (any status)."""
    uid = _uid(user_id)
    rows = _db.query_all(
        f"SELECT {_ROW_COLS} FROM arcus_credentials WHERE user_id = %s ORDER BY network",
        (uid,),
    )
    out: dict[str, ArcusCredentialRow] = {}
    for d in rows:
        row = _row(d)
        out[row.network] = row
    return out


def list_active_credentials(network: ArcusNet | None = None) -> list[ArcusCredentialRow]:
    if network is None:
        rows = _db.query_all(
            f"SELECT {_ROW_COLS} FROM arcus_credentials WHERE status = 'active' "
            "ORDER BY user_id, network"
        )
    else:
        rows = _db.query_all(
            f"SELECT {_ROW_COLS} FROM arcus_credentials WHERE status = 'active' AND network = %s "
            "ORDER BY user_id, network",
            (_net(network),),
        )
    return [_row(d) for d in rows]


def list_lifecycle_credentials() -> list[ArcusCredentialRow]:
    """Rows the key-lifecycle job watches: ``active`` and ``expired``."""
    rows = _db.query_all(
        f"SELECT {_ROW_COLS} FROM arcus_credentials WHERE status IN ('active', 'expired') "
        "ORDER BY user_id, network"
    )
    return [_row(d) for d in rows]


def owner_of(network: ArcusNet, address: str, account_index: int) -> int | None:
    """The user_id holding an ACTIVE row for (network, address, account_index),
    else None (mirrors the partial unique index)."""
    net, addr = _net(network), _address(address)
    if isinstance(account_index, bool) or not isinstance(account_index, int) or not 0 <= account_index <= 9:
        raise ValueError("invalid account index")
    row = _db.query_one(
        "SELECT user_id FROM arcus_credentials "
        "WHERE network = %s AND address = %s AND account_index = %s AND status = 'active' LIMIT 1",
        (net, addr, account_index),
    )
    return int(row["user_id"]) if row else None


# --- writes -----------------------------------------------------------------------------------


def upsert_active_credential(
    *,
    user_id: int,
    network: ArcusNet,
    address: str,
    all_subaccounts: bool,
    sealed: SealedSigningKey,
    api_wallet_name: str | None,
    valid_until_ms: int,
    attested_at: datetime,
) -> ArcusCredentialRow:
    """Store (or replace) the user's ACTIVE credential for ``network`` at
    subaccount 0, in ONE transaction.

    This single statement IS the no-gap renewal: the row flips from the old key
    to the new key atomically. The old key is not revoked (the bot cannot
    revoke: revoking needs the owning wallet's EIP-712 signature).
    ``ArcusAddressTaken`` when another user holds an ACTIVE row for the same
    (network, address, 0) — by the pre-check or by the partial unique index.
    """
    uid, net, addr = _uid(user_id), _net(network), _address(address)
    if not isinstance(sealed, SealedSigningKey):
        raise ValueError("sealed key required")
    pub = _pub(sealed.api_public_key)
    until = _valid_until(valid_until_ms)
    name = _name(api_wallet_name)
    if not isinstance(all_subaccounts, bool):
        raise ValueError("all_subaccounts must be a bool")
    if not isinstance(attested_at, datetime):
        raise ValueError("attested_at must be a datetime")
    ciphertext = base64.b64encode(sealed.token).decode("ascii")

    def work(cur: Any) -> ArcusCredentialRow:
        cur.execute(
            "SELECT user_id FROM arcus_credentials WHERE network = %s AND address = %s "
            "AND account_index = 0 AND status = 'active' AND user_id <> %s LIMIT 1",
            (net, addr, uid),
        )
        if cur.fetchone():
            raise ArcusAddressTaken()
        cur.execute(
            "INSERT INTO arcus_credentials (user_id, network, address, account_index, all_subaccounts, "
            "api_public_key, encrypted_signing_key, api_wallet_name, valid_until_ms, status, attested_at, "
            "linked_at, last_verified_at, updated_at) "
            "VALUES (%s, %s, %s, 0, %s, %s, %s, %s, %s, 'active', %s, now(), now(), now()) "
            "ON CONFLICT (user_id, network) DO UPDATE SET "
            "address = EXCLUDED.address, account_index = 0, all_subaccounts = EXCLUDED.all_subaccounts, "
            "api_public_key = EXCLUDED.api_public_key, encrypted_signing_key = EXCLUDED.encrypted_signing_key, "
            "api_wallet_name = EXCLUDED.api_wallet_name, valid_until_ms = EXCLUDED.valid_until_ms, "
            "status = 'active', attested_at = EXCLUDED.attested_at, linked_at = now(), "
            "last_verified_at = now(), updated_at = now() "
            f"RETURNING {_ROW_COLS}",
            (uid, net, addr, all_subaccounts, pub, ciphertext, name, until, attested_at),
        )
        stored = cur.fetchone()
        if not stored:  # pragma: no cover - INSERT … RETURNING always returns the row
            raise RuntimeError("arcus credential upsert returned no row")
        return _row(dict(stored))

    try:
        return cast(ArcusCredentialRow, _db.run_transaction(work))
    except ArcusAddressTaken:
        raise
    except Exception as exc:
        if _is_unique_violation(exc):  # the partial index won a race with the pre-check
            raise ArcusAddressTaken() from None
        raise


def mark_status(
    user_id: int,
    network: ArcusNet,
    status: ArcusCredStatus,
    *,
    wipe_secret: bool,
    api_public_key: str | None = None,
) -> bool:
    """Move the row to ``invalid`` / ``expired`` / ``unlinked``. True iff a row changed.

    - ``unlinked`` ALWAYS wipes the ciphertext (``wipe_secret=True`` is required
      with it and refused with any other status).
    - ``active`` is refused: activation happens only through
      :func:`upsert_active_credential` / :func:`touch_verified` (which honour the
      partial unique index).
    - With ``api_public_key`` the update applies only while the row still holds
      that key: a diagnosis or expiry sweep that read the OLD key can never
      invalidate a key renewed a moment later.
    - An ``unlinked`` row is never moved to ``invalid``/``expired``.
    """
    uid, net = _uid(user_id), _net(network)
    if status not in ("invalid", "expired", "unlinked"):
        raise ValueError("mark_status: status must be invalid, expired or unlinked")
    if not isinstance(wipe_secret, bool) or wipe_secret != (status == "unlinked"):
        raise ValueError("mark_status: wipe_secret must be set exactly for unlinked")
    sql = (
        "UPDATE arcus_credentials SET status = %s, "
        "encrypted_signing_key = CASE WHEN %s THEN '' ELSE encrypted_signing_key END, "
        "updated_at = now() WHERE user_id = %s AND network = %s"
    )
    params: list[object] = [status, wipe_secret, uid, net]
    if status != "unlinked":
        sql += " AND status <> 'unlinked'"
    if api_public_key is not None:
        sql += " AND api_public_key = %s"
        params.append(_pub(api_public_key))
    sql += " RETURNING user_id"
    return _db.execute_returning(sql, tuple(params)) is not None


def touch_verified(
    user_id: int,
    network: ArcusNet,
    *,
    api_public_key: str,
    valid_until_ms: int,
    status: Literal["active", "expired"],
) -> bool:
    """Record a successful key check: ``last_verified_at``, the venue's current
    ``validUntil`` (the user may have rewritten it) and the status. Applies only
    while the row still holds ``api_public_key`` and is not ``unlinked``. A
    revival to ``active`` that collides with another user's ACTIVE row (partial
    unique index) returns False and changes nothing."""
    uid, net, pub = _uid(user_id), _net(network), _pub(api_public_key)
    until = _valid_until(valid_until_ms)
    if status not in ("active", "expired"):
        raise ValueError("touch_verified: status must be active or expired")
    try:
        row = _db.execute_returning(
            "UPDATE arcus_credentials SET last_verified_at = now(), valid_until_ms = %s, status = %s, "
            "updated_at = now() WHERE user_id = %s AND network = %s AND api_public_key = %s "
            "AND status <> 'unlinked' RETURNING user_id",
            (until, status, uid, net, pub),
        )
    except Exception as exc:
        if _is_unique_violation(exc):
            return False
        raise
    return row is not None


def load_auth(user_id: int, network: ArcusNet) -> ArcusAuth | None:
    """The signing auth for the user's subaccount-0 credential, or None.

    None for no row, an ``unlinked`` / ``invalid`` row, or an empty ciphertext.
    ``active`` rows load; ``expired`` rows that still hold ciphertext load too
    (cancel-only sweeps). ``ArcusCredentialError`` when the ciphertext cannot be
    decrypted or does not match the stored public key. Built through P2's
    ``signing.make_auth`` (the only allowed constructor)."""
    uid, net = _uid(user_id), _net(network)
    raw = _db.query_one(
        f"SELECT {_ROW_COLS}, encrypted_signing_key FROM arcus_credentials "
        "WHERE user_id = %s AND network = %s",
        (uid, net),
    )
    if not raw:
        return None
    ciphertext = raw.pop("encrypted_signing_key", None)
    row = _row(raw)
    if row.status not in ("active", "expired"):
        return None
    if not isinstance(ciphertext, str) or not ciphertext:
        return None
    try:
        seed_bytes = decrypt_with_server_key(base64.b64decode(ciphertext.encode("ascii"), validate=True))
        if len(seed_bytes) != 32:
            raise ValueError("bad seed length")
        signer = Ed25519Signer.from_seed_hex(seed_bytes.hex())
    except (InvalidToken, binascii.Error, ValueError, UnicodeEncodeError):
        raise ArcusCredentialError("stored Arcus key could not be decrypted") from None
    finally:
        seed_bytes = b""
    if signer.public_key_hex != row.api_public_key:
        raise ArcusCredentialError("stored Arcus key does not match its public key")
    return make_auth(row.ref(), signer)


# --- key-notice state (bot_state; the PUBLIC key only) ------------------------------------------


def key_notice_key(user_id: int, network: ArcusNet) -> str:
    """``arcus_key_notice:<uid>:arcus_<net>`` (shared keys carry the scope token)."""
    return f"{ARCUS_KEY_NOTICE_PREFIX}{_uid(user_id)}:{arcus_scope_for(_net(network))}"


def get_key_notice_state(user_id: int, network: ArcusNet) -> dict[str, Any] | None:
    """The stored reminder state, or None (absent / not a JSON object). Raises
    on a DB error."""
    raw = get_bot_state(key_notice_key(user_id, network))
    return raw if isinstance(raw, dict) else None


def save_key_notice_state(user_id: int, network: ArcusNet, state: dict[str, Any]) -> None:
    """Persist the reminder state ``{"pub", "sent", "expired_notified"}``. The
    public key is public (docs: "API keys are Ed25519 public keys and are not
    secret"); no secret is ever stored here."""
    if not isinstance(state, dict):
        raise ValueError("key notice state must be a dict")
    set_bot_state(key_notice_key(user_id, network), state)
