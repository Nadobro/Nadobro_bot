import io
import logging
import unittest

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.core.log_redaction import (
    ApschedulerOverlapFilter,
    RedactingFormatter,
    SensitiveDataRedactFilter,
    redact_sensitive_text,
)


class LogRedactionTests(unittest.TestCase):
    def test_redacts_bot_tokens_addresses_subaccounts_and_account_ids(self):
        text = (
            "POST https://api.telegram.org/bot123456:ABC_def-GHI/getMe "
            "addr=0xac63eaedbbbb85afb7a42b1312b4982c23f14288 "
            "subaccount=0xac63eaedbbbb85afb7a42b1312b4982c23f1428864656661756c740000000000 "
            '{"account_id":1982353571057176576} '
            "host=aws-1-eu-north-1.pooler.supabase.com ip=51.21.189.77 "
            "machine=7849300be75398 listen_address=[fdaa:4b:a29c:a7b:4d6:fafa:718b:2]:22 "
            "digest=sha256:f5f2a2a50e6e9226fcede55cf72a0f5fd9ff898bba6d80a25b86e84805a76219"
        )

        redacted = redact_sensitive_text(text)

        self.assertIn("/bot<REDACTED>/getMe", redacted)
        self.assertNotIn("123456:ABC_def-GHI", redacted)
        self.assertNotIn("0xac63eaedbbbb85afb7a42b1312b4982c23f14288", redacted)
        self.assertNotIn("64656661756c740000000000", redacted)
        self.assertNotIn("1982353571057176576", redacted)
        self.assertNotIn("aws-1-eu-north-1.pooler.supabase.com", redacted)
        self.assertNotIn("51.21.189.77", redacted)
        self.assertNotIn("7849300be75398", redacted)
        self.assertNotIn("fdaa:4b:a29c:a7b:4d6:fafa:718b:2", redacted)
        self.assertNotIn("f5f2a2a50e6e9226fcede55cf72a0f5fd9ff898bba6d80a25b86e84805a76219", redacted)

    def test_redacts_secp256k1_signatures_and_pinecone_hosts(self):
        sig = "0x" + "ab" * 65
        url = "https://nadobro-test.svc.region.pinecone.io/v1/whatever"
        text = f'place_order failed {{"signature":"{sig}","status":"failure"}} host={url} short=0xabc1...9def'
        redacted = redact_sensitive_text(text)
        self.assertIn("<REDACTED_PINECONE_URL>", redacted)
        self.assertNotIn(url, redacted)
        self.assertNotIn(sig, redacted)
        self.assertIn("0x<REDACTED>...<REDACTED>", redacted)

    def test_redacts_plaintext_logs_via_filter(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(SensitiveDataRedactFilter())
        handler.setFormatter(RedactingFormatter("%(levelname)s:%(message)s"))
        logger = logging.getLogger("test.redaction")
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)

        try:
            raise RuntimeError(
                "bad subaccount 0xac63eaedbbbb85afb7a42b1312b4982c23f1428864656661756c740000000000"
            )
        except RuntimeError:
            logger.exception("request failed for %s", "0xac63eaedbbbb85afb7a42b1312b4982c23f14288")

        output = stream.getvalue()
        self.assertIn("0x<REDACTED>", output)
        self.assertNotIn("0xac63eaedbbbb85afb7a42b1312b4982c23f14288", output)
        self.assertNotIn("64656661756c740000000000", output)

    def test_redaction_preserves_numeric_log_args(self):
        self.assertEqual(redact_sensitive_text(7.123456789012345), 7.123456789012345)

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(SensitiveDataRedactFilter())
        handler.setFormatter(RedactingFormatter("%(message)s"))
        logger = logging.getLogger("test.redaction.numeric")
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)

        logger.info("Support answer generated in %.1fs", 7.123456789012345)

        self.assertIn("7.1s", stream.getvalue())

    def test_preserves_hh_mm_ss_timestamps(self):
        # AUDIT-FIX-LR-2 regression: HH:MM:SS clock-style timestamps must NOT
        # be mistaken for IPv6 by the formatter. Production logs depend on
        # this since RedactingFormatter runs against the formatted asctime.
        for text in (
            "2026-05-24 15:52:43,015 [INFO] src.nadobro.db: Resolved hostname",
            "2026/05/24 15:52:50 [error] 677#677 nginx connect() failed",
            "started_at=15:52:43 elapsed=12.3s",
        ):
            self.assertIn("15:52", redact_sensitive_text(text), text)
            self.assertNotIn("<REDACTED_IPV6>", redact_sensitive_text(text), text)

    def test_telegram_identifier_fields_are_redacted(self):
        # AUDIT-FIX-LR-3 regression: 9-digit Telegram IDs were leaking via
        # ``user=...`` / ``chat_id=...`` operational log lines because the
        # bare-long-id rule needs 10+ digits.
        cases = (
            "Strategy cycle start user=380277661 network=mainnet strategy=dgrid",
            "Starting strategy loop for user 380277661 on mainnet",
            "sent to chat_id=380277661 reply",
            "telegram_id: 380277661 banned",
            "context user_id: 12345678",
        )
        for text in cases:
            redacted = redact_sensitive_text(text)
            self.assertNotIn("380277661", redacted, text)
            self.assertNotIn("12345678", redacted, text)
            self.assertIn("<REDACTED_ID>", redacted, text)

    def test_telegram_identifier_redaction_does_not_eat_counters(self):
        # ``user count=3`` and similar non-identifier phrases must not be
        # rewritten — that would defeat the whole point of debug logs.
        self.assertEqual(
            redact_sensitive_text("user count=3 active=2"),
            "user count=3 active=2",
        )

    def test_compressed_ipv6_is_redacted(self):
        # AUDIT-FIX-LR-2: the old regex missed compressed IPv6 forms like
        # ``fe80::1`` while the new one catches them.
        for text in ("connected fe80::1", "rpc 2001:db8::8a2e:370:7334 ok"):
            self.assertIn("<REDACTED_IPV6>", redact_sensitive_text(text), text)

    def test_apscheduler_overlap_filter_drops_skip_spam(self):
        filt = ApschedulerOverlapFilter()
        skip = logging.LogRecord(
            name="apscheduler.scheduler",
            level=logging.WARNING,
            pathname="",
            lineno=1,
            msg='Execution of job "poll_lowiqpts_relay (trigger: interval[0:00:02], next run at: 2026-08-13 17:13:52 UTC)" skipped: maximum number of running instances reached (1)',
            args=(),
            exc_info=None,
        )
        keep = logging.LogRecord(
            name="apscheduler.scheduler",
            level=logging.WARNING,
            pathname="",
            lineno=1,
            msg='Run time of job "tick_desk_runner" was missed by 8 seconds',
            args=(),
            exc_info=None,
        )
        self.assertFalse(filt.filter(skip))
        self.assertTrue(filt.filter(keep))


if __name__ == "__main__":
    unittest.main()
