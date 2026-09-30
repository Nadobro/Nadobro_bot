"""LR-LINEAR: log redaction must run in linear time AND stay byte-identical.

``redact_sensitive_text`` runs on EVERY log record (main.py installs
``SensitiveDataRedactFilter`` + ``RedactingFormatter`` on the stream handler),
and logging happens on the asyncio event loop, so a regex stage that is
super-linear in the line length lets one long log line stall the whole bot.
Measured 2026-09-30 on the pre-LR-LINEAR code: 16,000 plain letters took 4.6 s
in the URL-credential pattern, ``a-a-a-...`` took 3.9 s in the Supabase-host
pattern, and ``http://http://...`` grew quadratically in the Pinecone pattern.

Two halves:

* Timing. Every compiled pattern in the module (discovered, so a pattern added
  later is covered automatically) and the whole chain, across adversarial input
  families at 10k / 100k / 1M chars, in thread CPU time. Budgets sit ~5-20x
  above the linear cost measured on a laptop and orders of magnitude below the
  quadratic cost (hours at 1M), so they separate the two without flaking on a
  slow CI runner. A stage that blows its 10k budget fails there, before the
  larger sizes run.
* Differential. The pre-LR-LINEAR patterns and chain are kept below VERBATIM
  as the oracle. The rewrite must reproduce the oracle's output exactly for
  every input: hand-written word-boundary edge cases, exhaustive token
  sequences per rewritten stage, and a seeded grammar fuzz mixing realistic log
  fragments with adversarial noise, all at sizes where the oracle is fast. A
  rewrite that redacts less (or differently) fails here.
"""

import itertools
import random
import re
import time
import unittest

from src.nadobro.core import log_redaction as lr
from src.nadobro.core.log_redaction import redact_sensitive_text


# --------------------------------------------------------------------------
# Oracle: the pre-LR-LINEAR patterns and chain, copied VERBATIM. Do not "fix"
# anything here; this is the contract the linear rewrite must reproduce.
# --------------------------------------------------------------------------
_O_BOT_TOKEN_RE = re.compile(r"/bot\d+:[A-Za-z0-9_-]+(?=/|\s|$)")
_O_HEX_LONG_RE = re.compile(r"\b0x[a-fA-F0-9]{40,}\b")
_O_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}")
_O_ACCOUNT_ID_RE = re.compile(r'(?i)("?account_id"?\s*[:=]\s*)\d{8,}')
_O_SUBACCOUNT_FIELD_RE = re.compile(
    r"(?i)\b(subaccount(?:_hex)?=)(0x[a-fA-F0-9]+|[a-fA-F0-9]{16,})"
)
_O_TG_ID_FIELD_RE = re.compile(
    r"(?i)\b(user(?:_id)?|chat(?:_id)?|telegram(?:_id)?)([\s=:]+)(\d{5,})"
)
_O_ELLIPSIS_HEX_ADDR_RE = re.compile(r"\b0x[a-fA-F0-9]{4,}\.{3}[a-fA-F0-9]{4,}\b")
_O_BARE_LONG_ID_RE = re.compile(r"(?<![\w.-])\d{10,}(?![\w.-])")
_O_LONG_HEX_RE = re.compile(r"\b[a-fA-F0-9]{14,256}\b")
_O_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_O_IPV6_RE = re.compile(
    r"\b(?:"
    r"(?:[a-fA-F0-9]{1,4}:){3,7}[a-fA-F0-9]{1,4}"
    r"|(?:[a-fA-F0-9]{1,4}:){1,7}:[a-fA-F0-9]{0,4}(?::[a-fA-F0-9]{1,4})*"
    r"|::(?:[a-fA-F0-9]{1,4})(?::[a-fA-F0-9]{1,4}){0,7}"
    r")\b"
)
_O_SUPABASE_HOST_RE = re.compile(r"\b[a-z0-9-]+\.pooler\.supabase\.com\b", re.IGNORECASE)
_O_FLY_INTERNAL_RE = re.compile(r"\bfdaa:[a-fA-F0-9:]+\b")
_O_PINECONE_URL_RE = re.compile(r'https?://[^\s"\'<>]+pinecone\.io[^\s"\'<>]*', re.IGNORECASE)
_O_URL_CREDENTIALS_RE = re.compile(r"([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^@\s/]+)@", re.IGNORECASE)
_O_PRIVATE_KEY_FIELD_RE = re.compile(
    r"(?i)\b(private[_-]?key|secret|api[_-]?key|authorization|bearer|token)\b"
    r"(\s*[:=]\s*)"
    r"([A-Za-z0-9_\-./:+]{12,})"
)
_O_TG_BOT_TOKEN_BARE_RE = re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b")


def _oracle_redact(value):
    if not isinstance(value, str):
        return value

    text = _O_URL_CREDENTIALS_RE.sub(r"\1<REDACTED>:<REDACTED>@", value)
    text = _O_PINECONE_URL_RE.sub("<REDACTED_PINECONE_URL>", text)
    text = _O_BOT_TOKEN_RE.sub("/bot<REDACTED>", text)
    text = _O_TG_BOT_TOKEN_BARE_RE.sub("<REDACTED_BOT_TOKEN>", text)
    text = _O_BEARER_RE.sub("Bearer <REDACTED>", text)
    text = _O_SUBACCOUNT_FIELD_RE.sub(lambda m: f"{m.group(1)}<REDACTED>", text)
    text = _O_ELLIPSIS_HEX_ADDR_RE.sub("0x<REDACTED>...<REDACTED>", text)
    text = _O_HEX_LONG_RE.sub("0x<REDACTED>", text)
    text = _O_ACCOUNT_ID_RE.sub(lambda m: f"{m.group(1)}<REDACTED>", text)
    text = _O_TG_ID_FIELD_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}<REDACTED_ID>", text
    )
    text = _O_PRIVATE_KEY_FIELD_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}<REDACTED>", text)
    text = _O_SUPABASE_HOST_RE.sub("<REDACTED_DB_HOST>", text)
    text = _O_FLY_INTERNAL_RE.sub("<REDACTED_IPV6>", text)
    text = _O_IPV4_RE.sub("<REDACTED_IP>", text)
    text = _O_IPV6_RE.sub("<REDACTED_IPV6>", text)
    text = _O_LONG_HEX_RE.sub("<REDACTED_HEX>", text)
    text = _O_BARE_LONG_ID_RE.sub("<REDACTED_ID>", text)
    return text


# The three stages LR-LINEAR rewrote, each paired with its oracle stage, so a
# difference is caught at the stage that made it instead of possibly being
# masked by a later stage of the chain.
_REWRITTEN_STAGES = {
    "_redact_url_credentials": lambda t: _O_URL_CREDENTIALS_RE.sub(r"\1<REDACTED>:<REDACTED>@", t),
    "_redact_pinecone_urls": lambda t: _O_PINECONE_URL_RE.sub("<REDACTED_PINECONE_URL>", t),
    "_redact_supabase_hosts": lambda t: _O_SUPABASE_HOST_RE.sub("<REDACTED_DB_HOST>", t),
}


# --------------------------------------------------------------------------
# Adversarial input families (each generator returns exactly n chars).
# --------------------------------------------------------------------------
def _rep(unit: str, n: int) -> str:
    return (unit * (n // len(unit) + 1))[:n]


def _random_text(alphabet: str, n: int, seed: int = 20260930) -> str:
    rng = random.Random(seed)
    return "".join(rng.choice(alphabet) for _ in range(n))


# U+017F (long s), U+212A (Kelvin sign), U+0131 (dotless i) and U+0130 are
# case-insensitive matches for s / k / i under re.IGNORECASE.
_FOLD_LETTERS = "\u017f\u212a\u0131\u0130"

_FAMILIES = {
    # Runs the old URL-credential pattern rescanned from every letter.
    "letters": lambda n: _rep("a", n),
    "mixed_case": lambda n: _rep("aBcD", n),
    "unicode_letters": lambda n: _rep("\u00e9a" + _FOLD_LETTERS, n),
    "hex": lambda n: _rep("0123456789abcdef", n),
    "0x_hex": lambda n: "0x" + _rep("abcdef0123456789", n - 2),
    "digits": lambda n: _rep("7", n),
    "bearer_letters": lambda n: "Bearer " + _rep("a", n - 7),
    "bearer_repeats": lambda n: _rep("Bearer ", n),
    "url_prefix_letters": lambda n: "http://" + _rep("a", n - 7),
    "url_prefix_user_colon": lambda n: "postgres://u:" + _rep("p", n - 13),
    "scheme_run_no_user": lambda n: _rep("a", n - 4) + "://@",
    "scheme_run_then_user": lambda n: _rep("a", n // 2) + "://" + _rep("b", n - n // 2 - 3),
    "http_repeats": lambda n: _rep("http://", n),
    "https_repeats": lambda n: _rep("HTTPS://", n),
    "scheme_sep_repeats": lambda n: _rep("://", n),
    "at_repeats": lambda n: _rep("@", n),
    "letter_at_repeats": lambda n: _rep("a@", n),
    # Boundary-dense runs the old Supabase-host pattern rescanned per boundary.
    "a_dash": lambda n: _rep("a-", n),
    "a_dash_near_supabase": lambda n: _rep("a-", n - 19) + ".pooler.supabase.co",
    # Carries the literal (defeats the chain's pre-check) but never matches.
    "a_dash_then_supabase_literal": lambda n: _rep("a-", n - 21) + "_.pooler.supabase.com",
    "supabase_repeats": lambda n: _rep("x.pooler.supabase.com", n),
    "supabase_dash_repeats": lambda n: _rep("-x.pooler.supabase.com", n),
    "a_colon": lambda n: _rep("a:", n),
    "a_dot": lambda n: _rep("a.", n),
    "digit_dot": lambda n: _rep("1.", n),
    # Shapes aimed at the other stages' unbounded quantifiers.
    "fdaa_groups": lambda n: "fdaa:" + _rep("a:", n - 5),
    "ipv6_compressed_tail": lambda n: "1::" + _rep(":1", n - 3),
    "ipv6_repeats": lambda n: _rep("::1 ", n),
    "bot_token_unterminated": lambda n: "/bot1:" + _rep("a", n - 7) + "\u00e9",
    "bare_bot_token_unterminated": lambda n: "12345678:" + _rep("a", n - 10) + "\u00e9",
    "key_field_repeats": lambda n: _rep("token=", n),
    "id_field_separators": lambda n: "user=" + _rep(":", n - 5),
    "ellipsis_addr": lambda n: "0x" + _rep("a", n // 2) + "..." + _rep("b", n - n // 2 - 6) + "g",
    "whitespace": lambda n: _rep(" ", n),
    "random_specials": lambda n: _random_text(":/@.-_ aA1\u00e9" + _FOLD_LETTERS, n),
}

# The chain at 1M chars costs ~0.2-0.65 s per family on a laptop, so the
# per-family 1M check runs on a subset: families that were quadratic before
# LR-LINEAR, including ones that carry "://" + "@" / ".pooler.supabase.com" so
# the chain's exact pre-checks cannot skip the rewritten patterns. The 1M
# "gauntlet" below still runs every family through the chain, and the
# per-pattern test times every pattern directly (no pre-check) on every family.
_CHAIN_1M_FAMILIES = (
    "unicode_letters",
    "scheme_run_no_user",  # 1M letters + "://@": the 4.6 s-at-16k case
    "http_repeats",
    "a_dash_then_supabase_literal",
    "random_specials",
)

# Per-pattern budgets (CPU seconds). Laptop cost of the slowest pattern over
# every family: ~1.4 ms at 10k, ~13 ms at 100k, ~0.14 s at 1M. The
# pre-LR-LINEAR URL-credential pattern took ~1-1.8 s at 10k.
_STAGE_BUDGET_10K = 0.03
_STAGE_BUDGET_100K = 0.3
# Whole chain. Laptop, worst family: ~7 ms at 10k, ~67 ms at 100k, ~0.65 s at 1M.
_CHAIN_BUDGET_10K = 0.15
_CHAIN_BUDGET_100K = 0.8
_CHAIN_BUDGET_1M = 3.0
# 10x more input: linear grows ~10x, quadratic ~100x. Below the floor the larger
# timing is too small for its ratio to mean anything (and is within budget).
_MAX_GROWTH = 30.0
_RATIO_FLOOR = 0.005


def _elapsed(fn, arg) -> float:
    # CPU time of this thread, not wall time: the question is the regex
    # engine's work, and a loaded CI runner (or laptop) inflates wall time by
    # multiples -- measured at load average 36, wall timings of UNCHANGED
    # linear patterns showed fake 100x "growth"; thread CPU time stayed ~10x.
    t0 = time.thread_time()
    fn(arg)
    return time.thread_time() - t0


def _timed(fn, arg, budget: float) -> float:
    """One timing; if over budget (but not hopelessly), time twice more and keep
    the best, so a GC pause or a noisy CI neighbour cannot fail a linear stage."""
    t = _elapsed(fn, arg)
    if budget < t < 10 * budget:
        t = min(t, _elapsed(fn, arg), _elapsed(fn, arg))
    return t


def _module_patterns():
    return sorted(
        (name, obj) for name, obj in vars(lr).items() if isinstance(obj, re.Pattern)
    )


class LogRedactionLinearTimeTests(unittest.TestCase):
    def _check_growth(self, fn, small: str, large: str, t_small: float, t_large: float, what: str):
        if t_large <= _RATIO_FLOOR:
            return
        ratio = t_large / max(t_small, 1e-6)
        if ratio > _MAX_GROWTH:  # re-time both (best of 3) before calling it super-linear
            t_small = min(_elapsed(fn, small) for _ in range(3))
            t_large = min(_elapsed(fn, large) for _ in range(3))
            ratio = t_large / max(t_small, 1e-6)
        self.assertLessEqual(
            ratio,
            _MAX_GROWTH,
            f"{what}: {t_small * 1e3:.2f} ms -> {t_large * 1e3:.2f} ms for 10x input",
        )

    def test_every_pattern_is_linear_on_every_family(self):
        patterns = _module_patterns()
        self.assertGreaterEqual(len(patterns), 17)
        for family, gen in _FAMILIES.items():
            s10, s100 = gen(10_000), gen(100_000)
            self.assertEqual((len(s10), len(s100)), (10_000, 100_000), family)
            for name, pattern in patterns:
                with self.subTest(family=family, pattern=name):
                    fn = lambda s, p=pattern: p.sub("", s)  # noqa: E731
                    t10 = _timed(fn, s10, _STAGE_BUDGET_10K)
                    self.assertLessEqual(t10, _STAGE_BUDGET_10K, f"{name} on {family} @10k")
                    t100 = _timed(fn, s100, _STAGE_BUDGET_100K)
                    self.assertLessEqual(t100, _STAGE_BUDGET_100K, f"{name} on {family} @100k")
                    self._check_growth(fn, s10, s100, t10, t100, f"{name} on {family}")

    def test_whole_chain_is_linear_up_to_1m_chars(self):
        for family in _CHAIN_1M_FAMILIES:
            gen = _FAMILIES[family]
            with self.subTest(family=family):
                s10, s100 = gen(10_000), gen(100_000)
                t10 = _timed(redact_sensitive_text, s10, _CHAIN_BUDGET_10K)
                self.assertLessEqual(t10, _CHAIN_BUDGET_10K, f"chain on {family} @10k")
                t100 = _timed(redact_sensitive_text, s100, _CHAIN_BUDGET_100K)
                self.assertLessEqual(t100, _CHAIN_BUDGET_100K, f"chain on {family} @100k")
                self._check_growth(redact_sensitive_text, s10, s100, t10, t100, f"chain on {family}")
                s1m = gen(1_000_000)
                t1m = _timed(redact_sensitive_text, s1m, _CHAIN_BUDGET_1M)
                self.assertLessEqual(t1m, _CHAIN_BUDGET_1M, f"chain on {family} @1M")
                self._check_growth(redact_sensitive_text, s100, s1m, t100, t1m, f"chain on {family} (100k->1M)")

    def test_whole_chain_gauntlet_of_every_family(self):
        def gauntlet(total: int) -> str:
            chunk = total // len(_FAMILIES)
            return "".join(gen(chunk) for gen in _FAMILIES.values())

        small = gauntlet(100_000)
        t_small = _timed(redact_sensitive_text, small, _CHAIN_BUDGET_100K)
        self.assertLessEqual(t_small, _CHAIN_BUDGET_100K, "chain on the 100k gauntlet")
        large = gauntlet(1_000_000)
        t_large = _timed(redact_sensitive_text, large, _CHAIN_BUDGET_1M)
        self.assertLessEqual(t_large, _CHAIN_BUDGET_1M, "chain on the 1M gauntlet")
        self._check_growth(redact_sensitive_text, small, large, t_small, t_large, "chain on the gauntlet")


# --------------------------------------------------------------------------
# Differential: new == oracle.
# --------------------------------------------------------------------------
_EDGE_CASES = (
    # Supabase host: word-boundary semantics around the host run.
    "host=aws-1-eu-north-1.pooler.supabase.com:6543",
    " -abc.pooler.supabase.com",
    "--abc.pooler.supabase.com",
    "-abc.pooler.supabase.com",
    "_abc.pooler.supabase.com",
    "x_abc.pooler.supabase.com",
    "_-abc.pooler.supabase.com",
    "\u00e9abc.pooler.supabase.com",
    "\u00e9-abc.pooler.supabase.com",
    "Xa.pooler.supabase.com",
    "A-B.POOLER.SUPABASE.COM",
    "abc.pooler.supabase.comx",
    "abc.pooler.supabase.com_",
    "abc.pooler.supabase.com-",
    "abc.pooler.supabase.com.",
    ".pooler.supabase.com",
    "-.pooler.supabase.com",
    "a.pooler.\u017fupabase.com",
    "\u212aa.pooler.supabase.com",
    # Back-to-back hosts: re.sub resumes right after "...supabase.com".
    "a.pooler.supabase.com-b.pooler.supabase.com",
    "a.pooler.supabase.com--b.pooler.supabase.com",
    "a.pooler.supabase.com-_b.pooler.supabase.com",
    "a.pooler.supabase.comb.pooler.supabase.com",
    "a.pooler.supabase.com.pooler.supabase.com",
    "a.pooler.supabase.com-.pooler.supabase.com",
    "a-b-c-d.pooler.supabase.co-e.pooler.supabase.com",
    # URL credentials: scheme chars "+.-", runs starting with non-letters.
    "postgresql://postgres.abcd:s3cr3t@aws-1-eu-north-1.pooler.supabase.com:6543/postgres",
    "a.b://user:pw@host",
    "a+b-c.d://user:pw@host",
    "1a://user:pw@host",
    "+.-9a://user:pw@host",
    "123://user:pw@host",
    "x_a://user:pw@host",
    "\u00e9a://user:pw@host",
    "\u017f://user:pw@host",
    "\u212a1+://user:pw@host",
    "HTTPS://USER:PW@HOST",
    "https://user@host",
    "https://user:@host",
    "https://:pw@host",
    "https://user:pw:x@host",
    "https://us er:pw@host",
    "https://user:p/w@host",
    "a://b:c@d://e:f@g",
    "a://b:c@@",
    "a://b:c://d:e@f",
    "a:://b:c@",
    "a:/b:c@",
    "redis://:pw@10.0.0.1:6379/0 and amqp+ssl://guest:guest@rabbit:5671",
    # Pinecone: first scheme in a run decides for the whole run.
    "https://nadobro-test.svc.region.pinecone.io/v1/whatever",
    "http://http://x.pinecone.io",
    "https://pinecone.io",
    "https://xpinecone.io",
    "https://x.pinecone.iox/y z",
    "http://a https://b.pinecone.io/c",
    "https://a\"https://b.pinecone.io",
    "https://a<https://b.pinecone.io>",
    "HTTP\u017f://A.PINECONE.IO/Q",
    "https://a.p\u0131necone.io",
    "http://https://pinecone.io",
    "https://https://pinecone.io",
    "httpshttp://x.pinecone.io",
    # Chain interplay and existing contract examples.
    "POST https://api.telegram.org/bot123456:ABC_def-GHI/getMe",
    "Bearer abcdefghijklmnop https://u:p@x.pinecone.io/v",
    "user=380277661 chat_id: 1234567 telegram_id=99999 user count=3",
    "listen_address=[fdaa:4b:a29c:a7b:4d6:fafa:718b:2]:22 ip=51.21.189.77 fe80::1",
    "2026-05-24 15:52:43,015 [INFO] src.nadobro.db: Resolved hostname",
    "",
)


class LogRedactionDifferentialTests(unittest.TestCase):
    def _assert_same(self, text: str):
        self.assertEqual(redact_sensitive_text(text), _oracle_redact(text), repr(text))
        for name, oracle in _REWRITTEN_STAGES.items():
            self.assertEqual(getattr(lr, name)(text), oracle(text), f"{name}: {text!r}")

    def test_non_string_values_pass_through(self):
        for value in (None, 7, 7.123456789012345, b"bytes", ("t",)):
            self.assertIs(redact_sensitive_text(value), value)

    def test_hand_written_edge_cases(self):
        for text in _EDGE_CASES:
            self._assert_same(text)
            self._assert_same(text.upper())
            self._assert_same(" " + text + " ")

    def test_exhaustive_token_sequences_per_rewritten_stage(self):
        token_sets = {
            "_redact_url_credentials": (
                ("a", "Z", "1", "+", ".", "-", "://", ":", "/", "@", " ", "\u017f", "\u00e9", "_"),
                4,
            ),
            "_redact_supabase_hosts": (
                ("a", "M", "1", "-", "_", "\u00e9", ".", " ", "com",
                 ".pooler.supabase.com", ".POOLER.\u017fUPABASE.COM", ".pooler.supabase.co"),
                4,
            ),
            "_redact_pinecone_urls": (
                ("http://", "https://", "HTTP\u017f://", "http", "s", "a", " ", '"', "<",
                 "pinecone.io", "p\u0131necone.io", "/", "."),
                4,
            ),
        }
        for name, (tokens, max_len) in token_sets.items():
            new, oracle = getattr(lr, name), _REWRITTEN_STAGES[name]
            for length in range(max_len + 1):
                for combo in itertools.product(tokens, repeat=length):
                    text = "".join(combo)
                    self.assertEqual(new(text), oracle(text), f"{name}: {text!r}")
            rng = random.Random(f"{name}-long")
            for _ in range(3000):
                text = "".join(rng.choice(tokens) for _ in range(rng.randint(5, 14)))
                self.assertEqual(new(text), oracle(text), f"{name}: {text!r}")

    def test_grammar_fuzz_matches_oracle(self):
        rng = random.Random(20260930)
        for _ in range(8000):
            text = _fuzz_line(rng)
            self._assert_same(text)

    def test_url_scheme_class_splits_into_letters_and_lead_in(self):
        # The URL-credential rewrite captures a scheme run's non-letter lead-in
        # as [0-9+.-] and then requires [a-z]; that is exact only if, under
        # re.IGNORECASE, the scheme class is precisely the disjoint union of the
        # two (it includes the long s / Kelvin sign / dotless i folds).
        every_char = "".join(chr(c) for c in range(0x110000) if not 0xD800 <= c <= 0xDFFF)
        scheme = set(re.findall(r"[a-z0-9+.-]", every_char, re.IGNORECASE))
        letters = set(re.findall(r"[a-z]", every_char, re.IGNORECASE))
        lead_in = set(re.findall(r"[0-9+.-]", every_char, re.IGNORECASE))
        self.assertEqual(scheme, letters | lead_in)
        self.assertEqual(letters & lead_in, set())
        self.assertTrue({"\u017f", "\u212a"} <= letters)


_REALISTIC = (
    "postgresql://postgres.abcdefgh:supersecretpw@aws-1-eu-north-1.pooler.supabase.com:6543/postgres",
    "postgres://user:pa:ss@db.internal:5432/nadobro",
    "redis://:hunter2@10.0.0.7:6379/0",
    "https://user:pw@example.com/path?q=1",
    "amqp+ssl://guest:guest@rabbit.local:5671",
    "a.b-c+d://x:y@z",
    "HTTP://USER:PW@HOST",
    "https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/getUpdates",
    "http://localhost:8080/health",
    "wss://gateway.nado.xyz/v1/ws",
    "aws-1-eu-north-1.pooler.supabase.com",
    "AWS-0-US-EAST-1.POOLER.SUPABASE.COM",
    "-lead.pooler.supabase.com",
    "_under.pooler.supabase.com",
    "x.pooler.supabase.comx",
    "x.pooler.supabase.co",
    ".pooler.supabase.com",
    "db.pooler.\u017fupabase.com",
    "/bot123456:ABC_def-GHI/getMe",
    "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw",
    "Bearer abcdefghijklmnopqrstuvwxyz",
    "authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig",
    "0xac63eaedbbbb85afb7a42b1312b4982c23f14288",
    "0xabc1...9def",
    "f5f2a2a50e6e9226fcede55cf72a0f5fd9ff898bba6d80a25b86e84805a76219",
    "7849300be75398",
    "51.21.189.77",
    "10.0.0.1:5432",
    "fdaa:4b:a29c:a7b:4d6:fafa:718b:2",
    "[fdaa:0:1::3]:22",
    "fe80::1",
    "2001:db8::8a2e:370:7334",
    "https://nadobro-test.svc.region.pinecone.io/v1/whatever",
    "http://x.pinecone.io",
    "HTTPS://A.PINECONE.IO/Q",
    "user=380277661",
    "chat_id: 1234567",
    "telegram_id=99999",
    "user count=3",
    '{"account_id": 1982353571057176576}',
    '"subaccount_hex": "0xabc"',
    "subaccount=0xac63eaedbbbb85afb7a42b1312b4982c23f1428864656661756c740000000000",
    "api_key=sk-abcdefghijklmnop",
    "token: abcdefghijklmnopq",
    "secret=hunter2hunter2",
    "2026-05-24 15:52:43,015",
    "15:52:43",
    "1717171717171",
)

_NOISE_ALPHABETS = (
    "abcxyz",
    "aBcDeF",
    "0123456789abcdef",
    "0123456789",
    "-a",
    ":a1",
    ".a1",
    "+.-a1",
    ":/@",
    "_-a",
    " \t\n\u00a0",
    "\"'<>",
    "\u00e9\u00df\u03a9\u00b5\u01c5" + _FOLD_LETTERS,
    ":/@.-_ aA1\u00e9" + _FOLD_LETTERS,
)

_NOISE_TOKENS = (
    "://", "http://", "https://", "HTTPS://", "pinecone.io", ".pooler.supabase.com",
    ".pooler.supabase.co", "supabase.com", "Bearer ", "0x", "::", "@", "fdaa:", "/bot",
    "user=", "token=", "account_id:", "...",
)

_SEPARATORS = ("", "", "", " ", " ", "=", ":", "/", "@", ",", "-", "_", ".", '"', "\n")


def _mutate(rng: random.Random, text: str) -> str:
    roll = rng.random()
    if roll < 0.15:
        text = text.upper()
    elif roll < 0.25:
        text = text.swapcase()
    elif roll < 0.35:  # IGNORECASE-equivalent folds
        text = text.replace("s", "\u017f").replace("k", "\u212a").replace("i", "\u0131")
    if rng.random() < 0.3 and len(text) > 1:  # truncate or cut: near-misses
        a = rng.randrange(len(text))
        b = rng.randrange(a, len(text) + 1)
        text = text[:a] + text[b:] if rng.random() < 0.5 else text[a:b]
    return text


def _fuzz_line(rng: random.Random) -> str:
    pieces = []
    for _ in range(rng.randint(1, 10)):
        roll = rng.random()
        if roll < 0.45:
            piece = _mutate(rng, rng.choice(_REALISTIC))
        elif roll < 0.7:
            piece = rng.choice(_NOISE_TOKENS)
        else:
            alphabet = rng.choice(_NOISE_ALPHABETS)
            piece = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 16)))
        pieces.append(piece)
        pieces.append(rng.choice(_SEPARATORS))
    return "".join(pieces)


if __name__ == "__main__":
    unittest.main()
