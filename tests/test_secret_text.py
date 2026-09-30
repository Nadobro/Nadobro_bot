"""utils/secret_text.py — secret-shape detection for the Arcus paste-key flow (03 §5, §19.1)."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.nadobro.utils.secret_text import (
    SecretShape,
    classify_secret_text,
    collapse_secret_candidate,
    extract_hex_key,
    is_wallet_private_key,
)

RFC_SEED = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"  # RFC 8032 test 1
WALLET_SEED = "ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"  # public Hardhat #0
WALLET_ADDR = "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"
ADDR40 = "ab" * 20
MODULE = Path(__file__).resolve().parents[1] / "src" / "nadobro" / "utils" / "secret_text.py"


def _groups(text: str, n: int) -> list[str]:
    return [text[i : i + n] for i in range(0, len(text), n)]


@pytest.mark.parametrize(
    "text",
    [
        RFC_SEED,
        RFC_SEED.upper(),
        "0x" + RFC_SEED,
        "0X" + RFC_SEED,
        "  " + RFC_SEED + "\n",
        "\n\t0x" + RFC_SEED + "  ",
        "`" + RFC_SEED + "`",
        "```" + RFC_SEED + "```",
        '"' + RFC_SEED + '"',
        "'" + RFC_SEED + "'",
        "<" + RFC_SEED + ">",
        "“" + RFC_SEED + "”",
        " ".join(_groups(RFC_SEED, 8)),  # 8 groups of 8
        RFC_SEED[:32] + "\n" + RFC_SEED[32:],  # wrapped across 2 lines
    ],
)
def test_whole_message_key_shapes_are_hex_key(text):
    assert classify_secret_text(text) is SecretShape.HEX_KEY
    assert extract_hex_key(text) == RFC_SEED


def test_lengths_around_the_key():
    assert classify_secret_text("a" * 63) is None
    assert classify_secret_text("0x" + "a" * 63) is None
    assert classify_secret_text("a" * 65) is SecretShape.HEX_OTHER
    assert classify_secret_text("ab" * 64) is SecretShape.HEX_OTHER  # 128-hex expanded key
    assert classify_secret_text("0x" + "ab" * 64) is SecretShape.HEX_OTHER
    assert extract_hex_key("a" * 65) is None


def test_embedded_hex_run():
    text = f"my key is {RFC_SEED} thanks"
    assert classify_secret_text(text) is SecretShape.HEX_EMBEDDED
    assert classify_secret_text(f"key=0x{RFC_SEED}, ok?") is SecretShape.HEX_EMBEDDED
    assert extract_hex_key(text) is None


def test_addresses_are_never_secret_shaped():
    assert classify_secret_text(ADDR40) is None
    assert classify_secret_text("0x" + ADDR40) is None
    assert classify_secret_text(f"0x{ADDR40} 0x{'cd' * 20}") is None
    assert classify_secret_text(f"send to 0x{ADDR40} please") is None


def test_pem_anywhere():
    assert classify_secret_text("-----BEGIN PRIVATE KEY-----\nMC4CAQAwBQYDK2VwBCIEI\n-----END PRIVATE KEY-----") is SecretShape.PEM
    assert classify_secret_text("here: -----BEGIN OPENSSH PRIVATE KEY----- abc") is SecretShape.PEM


@pytest.mark.parametrize("value", [None, "", 123, 1.5, b"ab" * 32, ["x"], "hello world", "long btc 10x"])
def test_total_and_non_secret_inputs(value):
    assert classify_secret_text(value) is None
    assert extract_hex_key(value) is None


def test_huge_input_is_bounded_and_never_raises():
    big = "x" * 100_000
    assert classify_secret_text(big) is None
    # A key past the scan bound is not seen; one inside it is (as embedded text).
    assert classify_secret_text(big + RFC_SEED) is None
    assert classify_secret_text(RFC_SEED + " " + big) is SecretShape.HEX_EMBEDDED
    # A whole-message verdict needs the whole message: > 8192 chars is never HEX_KEY.
    assert classify_secret_text(RFC_SEED + " " * 9000 + "tail") is not SecretShape.HEX_KEY
    assert collapse_secret_candidate(123) == ""  # type: ignore[arg-type]


def test_is_wallet_private_key_uses_the_injected_derivation():
    from src.nadobro.core.crypto import derive_address_from_private_key as derive

    assert is_wallet_private_key(WALLET_SEED, WALLET_ADDR, derive_address=derive) is True
    assert is_wallet_private_key(WALLET_SEED, "0x" + ADDR40, derive_address=derive) is False
    assert is_wallet_private_key(RFC_SEED, WALLET_ADDR, derive_address=derive) is False
    # An invalid secp256k1 scalar (eth-account raises ValueError) is not a wallet key.
    assert is_wallet_private_key("ff" * 32, WALLET_ADDR, derive_address=derive) is False


def test_is_wallet_private_key_fails_closed_on_other_errors():
    def broken(_key: str) -> str:
        raise RuntimeError("backend unavailable")

    with pytest.raises(RuntimeError):
        is_wallet_private_key(RFC_SEED, WALLET_ADDR, derive_address=broken)
    with pytest.raises(ValueError):
        is_wallet_private_key(RFC_SEED.upper(), WALLET_ADDR, derive_address=broken)
    with pytest.raises(ValueError):
        is_wallet_private_key(RFC_SEED, WALLET_ADDR.upper(), derive_address=broken)


def test_validation_messages_never_echo_the_input():
    for bad in (RFC_SEED.upper(), "zz" * 32):
        with pytest.raises(ValueError) as exc:
            is_wallet_private_key(bad, WALLET_ADDR, derive_address=lambda k: k)
        assert bad not in str(exc.value)


def test_module_is_a_stdlib_only_leaf():
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    nadobro = {m for m in imported if m.startswith("src.nadobro")}
    assert all(m.startswith("src.nadobro.utils") for m in nadobro), nadobro
    assert imported <= {"__future__", "re", "enum", "typing"}, imported
