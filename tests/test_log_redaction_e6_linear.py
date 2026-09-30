"""LR-LINEAR for the E6 redaction rules (Arcus P2).

Two E6 patterns were quadratic on adversarial input, and redaction runs on every
log record on the event loop:

* ``_E6_LABELLED_SECRET_RE`` — the separator's adjacent ``\\s*`` quantifiers split
  one whitespace run in every way before failing ("api_secret" + 16k spaces +
  ":" + 16k spaces + "!" took ~13.8 s).
* ``_E6_PEM_RE`` — every ``-----BEGIN X-----`` header rescanned to the end of the
  text when no ``-----END`` followed (64k chars of headers took ~1.7 s).

The rewrite must be byte-identical: the pre-rewrite patterns are kept verbatim
below as the oracle and compared on hand-written edge cases and a seeded fuzz.
Timing uses thread CPU time (robust to machine load) with generous budgets.
"""

from __future__ import annotations

import random
import re
import time
import unittest

from src.nadobro.core import log_redaction as lr

_PLACEHOLDER = "-----BEGIN <REDACTED_PEM>"

# --- Oracle: the E6 patterns exactly as they were before LR-LINEAR ------------
_OLD_PEM_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]{3,64}-----.*?-----END [A-Z0-9 ]{3,64}-----", re.DOTALL
)
_OLD_LABELLED_SECRET_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"((?:[A-Za-z0-9]+[_-]){0,6}(?:api[_-]?secret|secret|signing[_-]?(?:key|seed)|private[_-]?key|seed[_-]?hex|x-signature))"
    r"(?![A-Za-z0-9])"
    r"(\s*[\"']?\s*[:=]\s*[\"']?)"
    r"([A-Za-z0-9_\-./+=]{8,})"
)


def _old_pem(text: str) -> str:
    return _OLD_PEM_RE.sub(_PLACEHOLDER, text)


def _new_pem(text: str) -> str:
    return lr._E6_PEM_RE.sub(lr._e6_pem_repl, text)


def _labelled_repl(m: "re.Match[str]") -> str:
    return f"{m.group(1)}{m.group(2)}<REDACTED>"


def _old_labelled(text: str) -> str:
    return _OLD_LABELLED_SECRET_RE.sub(_labelled_repl, text)


def _new_labelled(text: str) -> str:
    return lr._E6_LABELLED_SECRET_RE.sub(_labelled_repl, text)


_PEM_EDGE_CASES = [
    "",
    "-----BEGIN KEY-----",
    "-----BEGIN KEY-----abc-----END KEY-----",
    "x -----BEGIN RSA PRIVATE KEY-----\nAAAA\n-----END RSA PRIVATE KEY----- y",
    "-----BEGIN A B-----no end here",
    "-----BEGIN AB-----short label (2 chars) -----END AB-----",
    "-----BEGIN KEY----- a -----BEGIN KEY----- b -----END KEY----- c",
    "-----BEGIN KEY----- a -----END KEY----- -----BEGIN KEY----- b",
    "-----BEGIN KEY----- a -----END KEY----- -----BEGIN KEY----- b -----END KEY-----",
    "-----BEGIN key-----lowercase label-----END key-----",
    "-----BEGIN KEY----------END KEY-----",
    "------BEGIN KEY------ extra dashes -----END KEY-----",
    "-----BEGIN " + "A" * 64 + "-----x-----END " + "B" * 64 + "-----",
    "-----BEGIN " + "A" * 65 + "-----x-----END KEY-----",
    "-----BEGIN KEY-----x-----END " + "B" * 65 + "-----",
    "-----BEGIN KEY-----\n\n\n-----END KEY-----\n-----BEGIN KEY-----",
]

_LABELLED_EDGE_CASES = [
    "api_secret=abcdefgh12345678",
    "api_secret = 'abcdefgh12345678'",
    'api_secret : "abcdefgh12345678"',
    "API-SECRET:abcdefgh",
    "wallet_private_key=0xdeadbeefdeadbeef",
    "arcus_probe_signing_key=QUJDREVGR0g=",
    "x-signature: ZmFrZXNpZ25hdHVyZQ==",
    "seedhex=0123456789abcdef",
    "secret=short",
    "secret  \"  =  \"  longenoughvalue",
    "mysecret=abcdefgh12345678",
    "a_b_c_d_e_f_g_secret=abcdefgh12345678",
    "a_b_c_d_e_f_secret=abcdefgh12345678",
    "secret\t=\tabcdefgh12345678",
    "secret=\n abcdefgh12345678",
    "secret '=' abcdefgh12345678",
    "secret = ' ' abcdefgh12345678",
    "signing_seed='abc.def/ghi+jk='",
    "private-key:abcdefgh!!",
    "secret:" + " " * 50 + "!",
    "secret" + " " * 50 + "x",
]


def _fuzz_pem(rng: random.Random) -> str:
    parts = []
    for _ in range(rng.randint(1, 8)):
        choice = rng.random()
        label = "".join(rng.choice("ABC 12") for _ in range(rng.randint(2, 6)))
        if choice < 0.3:
            parts.append(f"-----BEGIN {label}-----")
        elif choice < 0.55:
            parts.append(f"-----END {label}-----")
        elif choice < 0.7:
            parts.append(rng.choice(["-----", "-----BEGIN ", "-----END ", "-", "\n"]))
        else:
            parts.append("".join(rng.choice("abcXYZ \n-=") for _ in range(rng.randint(0, 8))))
    return "".join(parts)


def _fuzz_labelled(rng: random.Random) -> str:
    labels = ["secret", "api_secret", "api-secret", "apisecret", "signing_key", "signing-seed",
              "private_key", "privatekey", "seed_hex", "x-signature", "token", "SECRET", "Secret"]
    parts = []
    for _ in range(rng.randint(1, 6)):
        choice = rng.random()
        if choice < 0.35:
            prefix = "".join(rng.choice(["a_", "b-", "wallet_", "x", "_", "1-"]) for _ in range(rng.randint(0, 7)))
            parts.append(prefix + rng.choice(labels))
        elif choice < 0.65:
            parts.append("".join(rng.choice([" ", "\t", "'", '"', ":", "=", "\n"]) for _ in range(rng.randint(0, 6))))
        else:
            parts.append("".join(rng.choice("abcXYZ0189_-./+=!@ ") for _ in range(rng.randint(0, 14))))
    return "".join(parts)


def _cpu(fn, text: str) -> float:
    start = time.thread_time()
    fn(text)
    return time.thread_time() - start


class E6DifferentialTests(unittest.TestCase):
    def test_pem_edge_cases_match_oracle(self) -> None:
        for text in _PEM_EDGE_CASES:
            with self.subTest(text=text[:60]):
                self.assertEqual(_new_pem(text), _old_pem(text))

    def test_labelled_edge_cases_match_oracle(self) -> None:
        for text in _LABELLED_EDGE_CASES:
            with self.subTest(text=text[:60]):
                self.assertEqual(_new_labelled(text), _old_labelled(text))

    def test_pem_fuzz_matches_oracle(self) -> None:
        rng = random.Random(20260930)
        for _ in range(20000):
            text = _fuzz_pem(rng)
            self.assertEqual(_new_pem(text), _old_pem(text), msg=repr(text))

    def test_labelled_fuzz_matches_oracle(self) -> None:
        rng = random.Random(930)
        for _ in range(20000):
            text = _fuzz_labelled(rng)
            self.assertEqual(_new_labelled(text), _old_labelled(text), msg=repr(text))

    def test_full_chain_still_redacts_pem_and_labelled_secrets(self) -> None:
        out = lr.redact_sensitive_text(
            "boot -----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY----- api_secret=abcdefgh12345678"
        )
        self.assertNotIn("MIIE", out)
        self.assertNotIn("abcdefgh12345678", out)
        self.assertIn(_PLACEHOLDER, out)
        self.assertIn("api_secret=<REDACTED>", out)


class E6LinearTimeTests(unittest.TestCase):
    """Generous CPU budgets: the old patterns took seconds on these at 16k-64k."""

    FAMILIES = {
        "labelled_ws_fail": lambda n: "api_secret" + " " * (n // 2) + ":" + " " * (n // 2) + "!",
        "labelled_ws_no_sep": lambda n: "secret" + " " * n + "x",
        "labelled_quote_ws": lambda n: "secret" + " " * (n // 2) + "'" + " " * (n // 2) + "!",
        "labelled_repeats": lambda n: "a_secret= " * (n // 10),
        "pem_headers_no_end": lambda n: "-----BEGIN KEY----- x " * (n // 22),
        "pem_header_then_text": lambda n: "-----BEGIN KEY-----" + "a" * n,
        "pem_end_only": lambda n: "-----END KEY----- " * (n // 18),
    }

    def test_each_family_is_linear(self) -> None:
        for name, family in self.FAMILIES.items():
            for fn in (_new_pem, _new_labelled, lr.redact_sensitive_text):
                with self.subTest(family=name, fn=getattr(fn, "__name__", "?")):
                    small = _cpu(fn, family(10_000))
                    large = _cpu(fn, family(100_000))
                    self.assertLess(large, 1.0, f"{name}: {large:.3f}s CPU at 100k")
                    # Linear growth is ~10x; allow noise, forbid quadratic (~100x).
                    if small > 0.002:
                        self.assertLess(large / small, 30.0, f"{name}: growth {large / small:.1f}x")


if __name__ == "__main__":
    unittest.main()
