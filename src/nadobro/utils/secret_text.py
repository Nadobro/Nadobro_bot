"""Secret-shaped text detection for the Arcus paste-key flow (Arcus P3b).

Pure and stdlib-only (``utils`` is a leaf package), so the venue gate can run
:func:`classify_secret_text` on every message without importing the Arcus
library. Nothing here logs, and nothing here raises on any input.

Shapes (build_decisions D-11 names hex and PEM shapes; 03 §5):

- ``HEX_KEY`` — the WHOLE message (all whitespace removed, wrapping quotes /
  backticks / angle brackets stripped) is ``(0x)?[0-9a-fA-F]{64}``: a candidate
  Arcus "API Signing Key". The docs build the key as
  ``Ed25519PrivateKey.from_private_bytes(bytes.fromhex("<API Signing Key>"))``
  (``guides__rest-trading.md``), i.e. 32 bytes of hex.
- ``HEX_OTHER`` — the whole message is ``(0x)?`` hex LONGER than 64 chars
  (e.g. a 128-hex expanded key).
- ``HEX_EMBEDDED`` — a run of at least 64 hex chars (optionally ``0x``-prefixed)
  inside other text.
- ``PEM`` — a ``-----BEGIN <LABEL>-----`` header anywhere (the manual Arcus key
  path uses PEM files: ``openssl pkey -in private.pem -pubout``,
  ``api-reference__authentication.md``).

A 40-hex address (with or without ``0x``) is never secret-shaped. A 64-hex
transaction hash is (an accepted cost: the interceptor is Arcus-scoped only).

Invisible Unicode format characters (category ``Cf``: zero-width space, the
LRM / RLM / Arabic-letter bidi marks, word joiner, BOM, …) are dropped before
any hex test: a paste from an RTL keyboard or a rich-text app can carry them
around or inside a key, and they must neither hide a key from the interceptor
nor turn a valid key into "not a key".

:func:`is_wallet_private_key` tells a pasted 32-byte value apart from the
secp256k1 WALLET key of the linked address. The address derivation is injected
(``core.crypto.derive_address_from_private_key`` in the bot), which keeps this
module free of third-party imports.
"""

from __future__ import annotations

import re
import unicodedata
from enum import Enum
from typing import Callable, Final

__all__ = [
    "SecretShape",
    "collapse_secret_candidate",
    "classify_secret_text",
    "extract_hex_key",
    "is_wallet_private_key",
]


class SecretShape(Enum):
    HEX_KEY = "hex_key"
    HEX_OTHER = "hex_other"
    HEX_EMBEDDED = "hex_embedded"
    PEM = "pem"


# At most this many characters are scanned (bounds regex work on a huge paste).
_MAX_SCAN: Final = 8192
_PEM_RE: Final = re.compile(r"-----BEGIN [A-Z0-9 ]{1,40}-----")
_HEX_RUN_RE: Final = re.compile(r"(?<![0-9A-Fa-f])(?:0[xX])?[0-9A-Fa-f]{64,}(?![0-9A-Fa-f])")
_HEX_BODY_RE: Final = re.compile(r"[0-9A-Fa-f]+")
_WRAP: Final = "`'\"<>“”‘’"
_KEY_HEX_LEN: Final = 64
_ADDRESS_RE: Final = re.compile(r"^0x[0-9a-f]{40}$")


def _drop_format_chars(text: str) -> str:
    """``text`` without Unicode format characters (category ``Cf``). ASCII has
    none, so the common case costs one ``isascii`` check."""
    if text.isascii():
        return text
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def collapse_secret_candidate(text: str) -> str:
    """``text`` with ALL whitespace and every invisible format character
    (category ``Cf``) removed (keys wrapped across lines, split into groups by a
    UI, or carrying bidi marks / zero-width spaces), and surrounding quote /
    backtick / angle-bracket characters stripped. Non-str input gives ``""``."""
    if not isinstance(text, str):
        return ""
    return _drop_format_chars("".join(text.split())).strip(_WRAP)


def _hex_body(collapsed: str) -> str:
    """The collapsed text without one leading ``0x``/``0X``."""
    return collapsed[2:] if collapsed[:2] in ("0x", "0X") else collapsed


def classify_secret_text(text: object) -> SecretShape | None:
    """The secret shape of a message text/caption, or None. Total and pure."""
    if not isinstance(text, str) or not text:
        return None
    scan = text[:_MAX_SCAN]
    if _PEM_RE.search(scan):
        return SecretShape.PEM
    # A whole-message verdict needs the whole message: a text longer than the
    # scan bound is never exactly a key (it can still be HEX_EMBEDDED below).
    if len(text) <= _MAX_SCAN:
        body = _hex_body(collapse_secret_candidate(text))
        if body and _HEX_BODY_RE.fullmatch(body):
            if len(body) == _KEY_HEX_LEN:
                return SecretShape.HEX_KEY
            if len(body) > _KEY_HEX_LEN:
                return SecretShape.HEX_OTHER
    # A zero-width character inside a key must not split its hex run.
    if _HEX_RUN_RE.search(_drop_format_chars(scan)):
        return SecretShape.HEX_EMBEDDED
    return None


def extract_hex_key(text: object) -> str | None:
    """The 64 lowercase hex chars (no ``0x``) iff the text is ``HEX_KEY``."""
    if classify_secret_text(text) is not SecretShape.HEX_KEY:
        return None
    assert isinstance(text, str)  # classify_secret_text returned a shape
    return _hex_body(collapse_secret_candidate(text)).lower()


def is_wallet_private_key(
    seed_hex: str,
    address: str,
    *,
    derive_address: Callable[[str], str],
) -> bool:
    """True when the 32-byte value ``seed_hex`` is the secp256k1 WALLET private
    key of ``address`` (build_decisions #3: "refuse a wallet private key
    (eth_account address == given address)").

    ``derive_address("0x" + seed)`` must return the EVM address of that key.
    A ``ValueError`` from it means "not a valid secp256k1 scalar" (eth-account:
    "Secret scalar must be greater than 0 …"), so the value cannot be a wallet
    key: False. Any OTHER exception propagates — the caller must fail closed
    (store nothing) rather than guess. The seed never appears in a message.
    """
    if not isinstance(seed_hex, str) or not re.fullmatch(r"[0-9a-f]{64}", seed_hex):
        raise ValueError("seed must be 64 lowercase hex")
    if not isinstance(address, str) or not _ADDRESS_RE.match(address):
        raise ValueError("address must be 0x + 40 lowercase hex")
    try:
        derived = derive_address("0x" + seed_hex)
    except ValueError:
        return False
    return isinstance(derived, str) and derived.strip().lower() == address
