"""Arcus request signing — sync, pure, CPU-light (one Ed25519 sign ≈ 0.1 ms).

Two schemes (docs ``api-reference__authentication.md``):

- **Scheme 1 — typed payload** (``placeOrder``, ``cancelOrder``, and each
  ``batchCancelOrders`` element): "the signed message **is the request payload
  itself** — a compact, key-sorted JSON object built from engine-native integer
  values". Place: ``{"ad","ai",["c"],"ct","g","m","op":1,"p","q","r","s","t","v":1}``;
  cancel: ``{"ad","ai",["c"],"ct",["id"],"m","op":2,"v":1}``. "``c`` — client id;
  **omitted entirely when empty**"; "The address (``ad``) is the **only**
  case-folded field"; "``r`` — reduce-only, ``0`` or ``1`` (integer, not a
  boolean)"; "``g`` — ``goodTilTime`` in **nanoseconds** (the request's
  microsecond ``goodTilTime`` × 1000)".
- **Scheme 2 — legacy message** (``setLeverage`` only here): "sign the
  timestamp, then the action, then the canonical JSON body — concatenated with
  no delimiters".

Units: "``p`` — price in **integer ticks** = ``price ÷ market tickSize``", "``q``
— quantity in **integer quantums** = ``size ÷ market stepSize``", "the
conversion must be exact", and "The divisor is **always the market's top-level
``tickSize``**". Conversions here are exact rational arithmetic and raise
``InexactUnitError`` on any remainder — a quantization bug must be loud.

Secrets: the seed lives only inside :class:`Ed25519Signer` (a ``cryptography``
key object). It is never stored as a string, never in ``repr``/``str``, and
the signer cannot be pickled or copied. Nothing here logs.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Final, Mapping, NoReturn, SupportsIndex

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.nadobro.venue.arcus.errors import InexactUnitError
from src.nadobro.venue.arcus.types import (
    CLIENT_ID_RE,
    INT64_MAX,
    ORDER_ID_RE,
    ArcusAccountRef,
    Side,
    Tif,
    normalize_address,
)

# The ONLY Scheme-2 action Nadobro ever signs (cancelAllOrders can never be signed).
SCHEME2_ACTIONS: Final = frozenset({"setLeverage"})
_SEED_RE: Final = re.compile(r"^[0-9a-f]{64}$")
# place-order quantity/price "pattern" — never an exponent form.
_WIRE_DEC_RE: Final = re.compile(r"^(0|0\.[0-9]*[1-9][0-9]*|[1-9][0-9]*\.?[0-9]*)$")
# A unit/value with an absurd exponent is refused before any big-int arithmetic.
_UNIT_EXP_LIMIT: Final = 60
_NOT_SERIALIZABLE: Final = "Ed25519Signer is not serializable"


# --- keys ------------------------------------------------------------------------


def normalize_seed_hex(text: object) -> str | None:
    """The 64-hex Ed25519 seed from a pasted "API Signing Key", or None.

    ``str`` only; ALL whitespace removed (pasted keys may wrap); one leading
    ``0x``/``0X`` stripped; lower-cased; exactly 32 bytes of hex. Never raises,
    never logs.
    """
    if not isinstance(text, str):
        return None
    compact = "".join(text.split())
    if compact[:2] in ("0x", "0X"):
        compact = compact[2:]
    compact = compact.lower()
    return compact if _SEED_RE.match(compact) else None


class Ed25519Signer:
    """Holds the Ed25519 private key; the only object that can sign."""

    __slots__ = ("_key", "_pub_hex")

    def __init__(self, key: Ed25519PrivateKey) -> None:
        if not isinstance(key, Ed25519PrivateKey):
            raise TypeError("Ed25519Signer needs an Ed25519PrivateKey")
        self._key = key
        self._pub_hex = key.public_key().public_bytes_raw().hex()

    @classmethod
    def from_seed_hex(cls, seed_hex: str) -> Ed25519Signer:
        """Build from the 64-hex seed. ``ValueError("invalid signing key")`` —
        the message NEVER contains the input. The seed string is not kept."""
        seed = normalize_seed_hex(seed_hex)
        if seed is None:
            raise ValueError("invalid signing key")
        return cls(Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed)))

    @property
    def public_key_hex(self) -> str:
        """64 lowercase hex = the Arcus "API Key" (``X-API-Key``)."""
        return self._pub_hex

    def sign_hex(self, message: bytes) -> str:
        """Ed25519 signature over ``message``: 128 lowercase hex."""
        if not isinstance(message, (bytes, bytearray)):
            raise TypeError("message must be bytes")
        return self._key.sign(bytes(message)).hex()

    def __repr__(self) -> str:
        return "Ed25519Signer(<redacted>)"

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


def derive_public_key_hex(seed_hex: str) -> str:
    """The Ed25519 public key (64 lowercase hex) for a seed."""
    return Ed25519Signer.from_seed_hex(seed_hex).public_key_hex


@dataclass(frozen=True, repr=False, eq=False)
class ArcusAuth:
    """Account + API key for signed writes. Build ONLY through :func:`make_auth`."""

    ref: ArcusAccountRef
    api_key_hex: str
    _signer: Ed25519Signer

    def __post_init__(self) -> None:
        if not isinstance(self.ref, ArcusAccountRef):
            raise ValueError("auth needs an ArcusAccountRef")
        if not isinstance(self._signer, Ed25519Signer):
            raise ValueError("auth needs an Ed25519Signer")
        if self.api_key_hex != self._signer.public_key_hex:
            raise ValueError("auth key mismatch")

    def sign_hex(self, message: bytes) -> str:
        """The only way callers sign."""
        return self._signer.sign_hex(message)

    def __repr__(self) -> str:
        return "ArcusAuth(<redacted>)"

    __str__ = __repr__


def make_auth(ref: ArcusAccountRef, signer: Ed25519Signer) -> ArcusAuth:
    """The ONLY constructor callers use (credentials loader, probe script)."""
    return ArcusAuth(ref, signer.public_key_hex, signer)


# --- canonical JSON / wire decimals -------------------------------------------------


def _check_json_value(value: object) -> None:
    # str / int / bool / list / dict only: a float, Decimal, bytes or None in a
    # signed or sent body is a builder bug (floats would change the bytes).
    if isinstance(value, (str, bool, int)):
        return
    if isinstance(value, list):
        for item in value:
            _check_json_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical_json keys must be str")
            _check_json_value(item)
        return
    raise TypeError(f"canonical_json cannot encode {type(value).__name__}")


def canonical_json(obj: Mapping[str, object]) -> bytes:
    """Sorted keys, no whitespace, ASCII: ``json.dumps(obj, sort_keys=True,
    separators=(",", ":"))`` (docs: "`canonical_json` serializes the body with
    sorted keys and no whitespace"). Only str/int/bool/list/dict values."""
    if not isinstance(obj, Mapping):
        raise TypeError("canonical_json needs a mapping")
    as_dict = dict(obj)
    _check_json_value(as_dict)
    return json.dumps(
        as_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def wire_decimal(value: Decimal) -> str:
    """A finite Decimal > 0 as the body's plain decimal string (never an
    exponent form): 84517.30 -> "84517.3", 5E+4 -> "50000", 1E-8 -> "0.00000001"."""
    if not isinstance(value, Decimal):
        raise TypeError("wire_decimal needs a Decimal")
    if not value.is_finite() or value <= 0:
        raise ValueError("wire decimal must be finite and > 0")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if not _WIRE_DEC_RE.match(text):
        raise ValueError("wire decimal not representable")
    return text


# --- exact unit conversion -----------------------------------------------------------


def _exact_units(value: Decimal, unit: Decimal, kind: str) -> int:
    for item in (value, unit):
        if not isinstance(item, Decimal):
            raise TypeError(f"{kind} and its unit must be Decimal")
    if not value.is_finite() or not unit.is_finite():
        raise ValueError(f"{kind} and its unit must be finite")
    if value <= 0 or unit <= 0:
        raise ValueError(f"{kind} and its unit must be > 0")
    if abs(value.adjusted()) > _UNIT_EXP_LIMIT or abs(unit.adjusted()) > _UNIT_EXP_LIMIT:
        raise InexactUnitError(f"{kind} out of range")
    # Exact rational arithmetic: Fraction(Decimal) is exact for finite values.
    ratio = Fraction(value) / Fraction(unit)
    if ratio.denominator != 1:
        raise InexactUnitError(f"{kind} not a multiple of unit")
    n = ratio.numerator
    if not 1 <= n <= INT64_MAX:
        raise InexactUnitError(f"{kind} out of range")
    return n


def to_ticks(price: Decimal, tick_size: Decimal) -> int:
    """Signed ``p``: ``price ÷ tickSize`` (top-level tickSize), exact, in [1, 2^63-1]."""
    return _exact_units(price, tick_size, "price")


def to_quantums(size: Decimal, step_size: Decimal) -> int:
    """Signed ``q``: ``size ÷ stepSize``, exact, in [1, 2^63-1]."""
    return _exact_units(size, step_size, "size")


# --- payload builders ------------------------------------------------------------------


def _req_int(value: object, name: str, *, lo: int = 0, hi: int = INT64_MAX) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an int")
    if not lo <= value <= hi:
        raise ValueError(f"{name} out of range")
    return value


def _opt_id(value: object, name: str, pattern: re.Pattern[str]) -> str | None:
    """None / "" -> absent; otherwise a str matching ``pattern`` (verbatim case)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a str")
    if value == "":
        return None
    if pattern.match(value) is None:
        raise ValueError(f"invalid {name}")
    return value


def place_payload(
    *,
    address: str,
    account_index: int,
    client_id: str | None,
    ct_ns: int,
    good_til_us: int,
    market_id: int,
    price_ticks: int,
    qty_quantums: int,
    reduce_only: bool,
    side: Side,
    tif: Tif,
) -> bytes:
    """Scheme-1 ``placeOrder`` payload bytes (op 1, v 1)."""
    if not isinstance(reduce_only, bool):
        raise TypeError("reduce_only must be a bool")
    if not isinstance(side, Side):
        raise TypeError("side must be a Side")
    if not isinstance(tif, Tif):
        raise TypeError("tif must be a Tif")
    good_til_us = _req_int(good_til_us, "good_til_us", hi=INT64_MAX // 1000)
    obj: dict[str, object] = {
        "ad": normalize_address(address),
        "ai": _req_int(account_index, "account_index", hi=9),
        "ct": _req_int(ct_ns, "ct_ns"),
        "g": good_til_us * 1000,
        "m": _req_int(market_id, "market_id", hi=65535),
        "op": 1,
        "p": _req_int(price_ticks, "price_ticks", lo=1),
        "q": _req_int(qty_quantums, "qty_quantums", lo=1),
        "r": 1 if reduce_only else 0,
        "s": side.signed,
        "t": tif.value,
        "v": 1,
    }
    cid = _opt_id(client_id, "client_id", CLIENT_ID_RE)
    if cid is not None:
        obj["c"] = cid
    return canonical_json(obj)


def cancel_payload(
    *,
    address: str,
    account_index: int,
    ct_ns: int,
    market_id: int,
    order_id: str | None = None,
    client_id: str | None = None,
) -> bytes:
    """Scheme-1 ``cancelOrder`` payload bytes (op 2, v 1); exactly one of
    ``order_id`` / ``client_id`` ("provide exactly one of `id` … or `c`")."""
    oid = _opt_id(order_id, "order_id", ORDER_ID_RE)
    cid = _opt_id(client_id, "client_id", CLIENT_ID_RE)
    if (oid is None) == (cid is None):
        raise ValueError("exactly one of order_id / client_id")
    obj: dict[str, object] = {
        "ad": normalize_address(address),
        "ai": _req_int(account_index, "account_index", hi=9),
        "ct": _req_int(ct_ns, "ct_ns"),
        "m": _req_int(market_id, "market_id", hi=65535),
        "op": 2,
        "v": 1,
    }
    if oid is not None:
        obj["id"] = oid
    else:
        obj["c"] = cid
    return canonical_json(obj)


def legacy_message(ts_ns: int, action: str, body: Mapping[str, object]) -> bytes:
    """Scheme 2: ``str(ts_ns) + action + canonical_json(body)`` (no delimiters).
    ``action`` must be in :data:`SCHEME2_ACTIONS` (defence in depth)."""
    if not isinstance(action, str) or action not in SCHEME2_ACTIONS:
        raise ValueError("action not allowed for Scheme-2 signing")
    ts = _req_int(ts_ns, "ts_ns")
    return str(ts).encode("ascii") + action.encode("ascii") + canonical_json(body)


__all__ = [
    "SCHEME2_ACTIONS",
    "normalize_seed_hex",
    "Ed25519Signer",
    "derive_public_key_hex",
    "ArcusAuth",
    "make_auth",
    "canonical_json",
    "wire_decimal",
    "to_ticks",
    "to_quantums",
    "place_payload",
    "cancel_payload",
    "legacy_message",
]
