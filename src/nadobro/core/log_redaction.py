"""Patched: services/log_redaction.py

Fix applied (search for AUDIT-FIX):
  AUDIT-FIX-LR-1: the original _BARE_LONG_ID_RE matched ANY bare run of 8+
                  digits and replaced it with <REDACTED_ID>. That hides
                  unix-epoch timestamps, prices in cents, lot sizes,
                  product IDs and any other long numeric value — including
                  things you actually need to debug. The dedicated
                  account_id and subaccount field patterns above already
                  cover the real risk (numeric account identifiers in
                  structured key=value pairs). The bare-number pattern
                  produces "silent damage" with no security gain — any
                  attacker who can read logs already has the structured
                  patterns redacted. We constrain the rule to long numbers
                  that look like Telegram chat IDs (>= 10 digits) which is
                  closer to the originally intended use.
  AUDIT-FIX-LR-2: the previous _IPV6_RE matched ``HH:MM:SS`` clock-style
                  timestamps because ``\\d{1,2}:\\d{1,2}:\\d{1,2}`` is a
                  legal subset of ``(?:[a-fA-F0-9]{1,4}:){2,}[a-fA-F0-9:]+``.
                  Since RedactingFormatter is applied AFTER ``%(asctime)s``
                  formatting, every log line in production had its time
                  rewritten to ``<REDACTED_IPV6>``. The new pattern only
                  matches things that are unambiguously IPv6: either 4+
                  colon-separated hex groups, or a compressed form that
                  contains ``::``. Pure HH:MM:SS clocks are 3 groups with
                  no ``::`` and no longer match.
  AUDIT-FIX-LR-3: explicit Telegram identifier field redaction. The bare
                  long-ID rule needs 10+ digits, but legacy Telegram user
                  IDs (and many synthetic test IDs) are 8-9 digits and
                  were sliding through unredacted in ``user=380277661``
                  style operational logs. The structured key=value rule
                  catches them regardless of digit count.
  LR-LINEAR:      this chain runs on EVERY log record, on the event loop, so
                  every stage must be linear in the line length or one long
                  line stalls the bot. The URL-credential, Pinecone-URL and
                  Supabase-host patterns were quadratic (16k plain letters
                  took 4.6 s) and are rewritten to produce BYTE-IDENTICAL
                  output; tests/test_log_redaction_linear.py keeps the old
                  patterns as an oracle, differential-fuzzes against them,
                  and times every pattern in this module (10k/100k chars)
                  and the whole chain (up to 1M chars).
"""
import logging
import re
from typing import Any


_BOT_TOKEN_RE = re.compile(r"/bot\d+:[A-Za-z0-9_-]+(?=/|\s|$)")
_HEX_LONG_RE = re.compile(r"\b0x[a-fA-F0-9]{40,}\b")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}")
_ACCOUNT_ID_RE = re.compile(r'(?i)("?account_id"?\s*[:=]\s*)\d{8,}')
_SUBACCOUNT_FIELD_RE = re.compile(
    r"(?i)\b(subaccount(?:_hex)?=)(0x[a-fA-F0-9]+|[a-fA-F0-9]{16,})"
)
# AUDIT-FIX-LR-3: redact Telegram-style identifiers in structured operational
# logs (``user=380277661``, ``chat_id 1234567``, ``telegram_id: 999999999``,
# ``for user 380277661 on mainnet`` etc.). 5+ digits is loose enough to catch
# legacy 8-9 digit Telegram IDs while still avoiding common counters like
# ``user count=3``.
_TG_ID_FIELD_RE = re.compile(
    r"(?i)\b(user(?:_id)?|chat(?:_id)?|telegram(?:_id)?)([\s=:]+)(\d{5,})"
)
_ELLIPSIS_HEX_ADDR_RE = re.compile(r"\b0x[a-fA-F0-9]{4,}\.{3}[a-fA-F0-9]{4,}\b")
# AUDIT-FIX-LR-1: bump 8 -> 10 so we stop nuking 8-digit prices, 8-digit
# product ids, and 8-digit unix timestamps. Telegram user IDs are 10+ digits.
_BARE_LONG_ID_RE = re.compile(r"(?<![\w.-])\d{10,}(?![\w.-])")
_LONG_HEX_RE = re.compile(r"\b[a-fA-F0-9]{14,256}\b")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# AUDIT-FIX-LR-2: real IPv6 only. Two alternatives:
#   1) 4+ colon-separated hex groups: catches full ``fdaa:4b:a29c:...``
#      style addresses but rejects ``HH:MM:SS`` (3 groups, 2 colons).
#   2) compressed form containing ``::``: catches ``fe80::1`` and
#      ``2001:db8::8a2e:370:7334`` which the old pattern also missed.
_IPV6_RE = re.compile(
    r"\b(?:"
    r"(?:[a-fA-F0-9]{1,4}:){3,7}[a-fA-F0-9]{1,4}"
    r"|(?:[a-fA-F0-9]{1,4}:){1,7}:[a-fA-F0-9]{0,4}(?::[a-fA-F0-9]{1,4})*"
    r"|::(?:[a-fA-F0-9]{1,4})(?::[a-fA-F0-9]{1,4}){0,7}"
    r")\b"
)
# LR-LINEAR: the old ``\b[a-z0-9-]+\.pooler\.supabase\.com\b`` was tried at
# EVERY word boundary and each try rescanned to the end of the host-char run,
# and ``a-a-a-...`` has a boundary at every char: O(n^2). '.' is not a host
# char, so the literal can only follow the END of a maximal run, and every
# start inside one run shares that continuation: the run's FIRST boundary
# decides for the whole run. So start only where a run starts -- or right
# after a previous match, the one place re.sub resumes inside a run
# (``...supabase.com`` then ``-``) -- and let group 1 consume, and the
# replacement write back, the run's chars before its first boundary.
_SUPABASE_HOST_RE = re.compile(
    r"(?:(?<![a-z0-9-])|(?<=\.pooler\.supabase\.com))"
    r"((?:(?!\b)[a-z0-9-])*+)"
    r"\b[a-z0-9-]++\.pooler\.supabase\.com\b",
    re.IGNORECASE,
)
# Exact pre-check (every match contains this literal, same flags), so a line
# without it skips the lookbehind-per-position scan above.
_SUPABASE_LITERAL_RE = re.compile(r"\.pooler\.supabase\.com", re.IGNORECASE)
_FLY_INTERNAL_RE = re.compile(r"\bfdaa:[a-fA-F0-9:]+\b")
# LR-LINEAR: the old ``https?://[^\s"'<>]+pinecone\.io[^\s"'<>]*`` scanned to
# the end of the separator-free run from EVERY ``http(s)://``, so
# ``http://http://...`` was O(n^2). A match always extends to that run's end,
# and a later scheme in the same run sees a subset of the ``pinecone.io``
# occurrences the first one saw: if the first fails, every later one fails.
# The second alternative consumes such a run (group 1 unset) and
# ``_pinecone_repl`` writes it back unchanged.
_PINECONE_URL_RE = re.compile(
    r'https?://(?:([^\s"\'<>]+pinecone\.io[^\s"\'<>]*)|[^\s"\'<>]*+)',
    re.IGNORECASE,
)
# LR-LINEAR: the old ``([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^@\s/]+)@`` was
# tried at EVERY letter and each try rescanned the rest of the scheme-char run
# for ``://``: O(run^2). ':' is not a scheme char, so ``://`` can only follow
# the END of a maximal run, and every start inside one run shares that
# continuation: the run's first letter decides for the whole run. So start
# only where a run starts (re.sub never resumes inside one: a match ends in
# '@') and let group 1 consume, and the replacement write back, the run's
# non-letter lead-in. The possessive quantifiers only drop backtracking that
# can never succeed (a shorter run is always followed by a char of the same
# class, never by the literal that must come next).
_URL_CREDENTIALS_RE = re.compile(
    r"(?<![a-z0-9+.-])([0-9+.-]*+)([a-z][a-z0-9+.-]*+://)([^/\s:@]++):([^@\s/]++)@",
    re.IGNORECASE,
)
_PRIVATE_KEY_FIELD_RE = re.compile(
    r"(?i)\b(private[_-]?key|secret|api[_-]?key|authorization|bearer|token)\b"
    r"(\s*[:=]\s*)"
    r"([A-Za-z0-9_\-./:+]{12,})"
)
# AUDIT-FIX-LR-1: catch Telegram bot tokens even when not in /bot<token>/
# URL form. Token shape is <digits>:<35+ chars from URL-safe alphabet>.
_TG_BOT_TOKEN_BARE_RE = re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b")


def _redact_url_credentials(text: str) -> str:
    # Exact pre-check: every match contains "://" and "@" (caseless chars).
    if "://" not in text or "@" not in text:
        return text
    return _URL_CREDENTIALS_RE.sub(r"\1\2<REDACTED>:<REDACTED>@", text)


def _pinecone_repl(match: "re.Match[str]") -> str:
    if match.group(1) is None:  # a run with no pinecone host: keep it as-is
        return match.group(0)
    return "<REDACTED_PINECONE_URL>"


def _redact_pinecone_urls(text: str) -> str:
    return _PINECONE_URL_RE.sub(_pinecone_repl, text)


def _redact_supabase_hosts(text: str) -> str:
    if _SUPABASE_LITERAL_RE.search(text) is None:
        return text
    return _SUPABASE_HOST_RE.sub(r"\1<REDACTED_DB_HOST>", text)


def redact_sensitive_text(value: Any) -> Any:
    """Redact secrets and account identifiers from text while preserving non-string
    values so %-style logging keeps numeric formatting semantics."""
    if not isinstance(value, str):
        return value

    text = _redact_url_credentials(value)
    text = _redact_pinecone_urls(text)
    text = _BOT_TOKEN_RE.sub("/bot<REDACTED>", text)
    text = _TG_BOT_TOKEN_BARE_RE.sub("<REDACTED_BOT_TOKEN>", text)
    text = _BEARER_RE.sub("Bearer <REDACTED>", text)
    text = _SUBACCOUNT_FIELD_RE.sub(lambda m: f"{m.group(1)}<REDACTED>", text)
    text = _ELLIPSIS_HEX_ADDR_RE.sub("0x<REDACTED>...<REDACTED>", text)
    text = _HEX_LONG_RE.sub("0x<REDACTED>", text)
    text = _ACCOUNT_ID_RE.sub(lambda m: f"{m.group(1)}<REDACTED>", text)
    text = _TG_ID_FIELD_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}<REDACTED_ID>", text
    )
    text = _PRIVATE_KEY_FIELD_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}<REDACTED>", text)
    text = _redact_supabase_hosts(text)
    text = _FLY_INTERNAL_RE.sub("<REDACTED_IPV6>", text)
    text = _IPV4_RE.sub("<REDACTED_IP>", text)
    text = _IPV6_RE.sub("<REDACTED_IPV6>", text)
    text = _LONG_HEX_RE.sub("<REDACTED_HEX>", text)
    text = _BARE_LONG_ID_RE.sub("<REDACTED_ID>", text)
    return text


class SensitiveDataRedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_sensitive_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: redact_sensitive_text(v) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(redact_sensitive_text(arg) for arg in record.args)
            else:
                record.args = redact_sensitive_text(record.args)
        return True


class ApschedulerOverlapFilter(logging.Filter):
    """Drop APScheduler max_instances skip warnings.

    Short-tick jobs (LOWIQPTS 2s poll, desk 5s, alerts 5s) use ``max_instances=1``
    + coalesce, so overlap is expected. Logging each skip at WARNING drowned
    production on 2026-08-13 (poll held the slot while the AMS relay lagged).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return "maximum number of running instances reached" not in msg


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_sensitive_text(super().format(record))
