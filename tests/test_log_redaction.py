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

    # --- E6 (Arcus P2): PEM blocks and prefixed/labelled secrets -------------

    def test_e6_pem_block(self):
        text = (
            "key=-----BEGIN PRIVATE KEY-----\nMC4CAQAwBQYDK2VwBCIEIAABAgMEBQYHCAkKCwwNDg8Q\n"
            "-----END PRIVATE KEY----- tail"
        )
        redacted = redact_sensitive_text(text)
        self.assertIn("<REDACTED_PEM>", redacted)
        self.assertIn("tail", redacted)
        self.assertNotIn("MC4CAQAwBQYDK2VwBCIE", redacted)

    def test_e6_truncated_pem(self):
        redacted = redact_sensitive_text("-----BEGIN PRIVATE KEY-----\nMC4CAQAw")
        self.assertIn("<REDACTED_PEM>", redacted)
        self.assertNotIn("MC4CAQAw", redacted)

    def test_e6_labelled_secrets(self):
        cases = (
            ("api_secret=QUJDREVGR0hJSktMTU5PUA==", "api_secret", "QUJDREVGR0hJSktMTU5PUA"),
            ('"signing_key": "0f1e2d3c4b5a69788796a5b4c3d2e1f0"', "signing_key", "0f1e2d3c4b5a69788796a5b4c3d2e1f0"),
            ("wallet_private_key: abcdefgh12345678", "wallet_private_key", "abcdefgh12345678"),
            ("ARCUS_PROBE_SIGNING_KEY=zz11yy22xx33ww44", "ARCUS_PROBE_SIGNING_KEY", "zz11yy22xx33ww44"),
            ("arcus_signing_seed=AbCdEfGh12", "arcus_signing_seed", "AbCdEfGh12"),
            ("X-Signature: sig_abcdefgh12", "X-Signature", "sig_abcdefgh12"),
        )
        for text, label, value in cases:
            redacted = redact_sensitive_text(text)
            self.assertNotIn(value, redacted, text)
            self.assertIn(label, redacted, text)
            # "<REDACTED>" from the labelled rule, or "<REDACTED_HEX>" when the
            # (unchanged, earlier) long-hex rule already masked the value.
            self.assertIn("<REDACTED", redacted, text)

    def test_e6_does_not_eat_neighbours(self):
        for text in (
            "secretary=bob12345678",
            "signature_ok=True",
            "user count=3 active=2",
            "key_fingerprint=ab12cd34",
            "signing_key_fingerprint=ab12cd34",
        ):
            self.assertEqual(redact_sensitive_text(text), text)

    def test_e6_seed_and_pubkey_hex(self):
        seed = bytes(range(32)).hex()
        pub = "03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8"
        redacted = redact_sensitive_text(f"seed {seed} and api_key={pub}")
        self.assertNotIn(seed, redacted)
        self.assertNotIn(pub, redacted)

    def test_e6_value_glued_to_a_pem_header_stays_redacted(self):
        """R2-4: the pre-E6 chain redacted ``token=hunter2abcd-----BEGIN …``
        because the value rule consumed ``-----BEGIN`` (12+ chars). E6 must not
        shorten that value: both the value and the PEM body stay hidden."""
        body = "MC4CAQAwBQYDK2VwBCIEIAABAgMEBQYHCAkKCwwNDg8Q"
        for text, value in (
            (f"token=hunter2abcd-----BEGIN PRIVATE KEY-----\n{body}\n-----END PRIVATE KEY-----", "hunter2abcd"),
            ("api_key=abcDEF12345-----BEGIN CERTIFICATE-----", "abcDEF12345"),
            (f"Bearer abc-----BEGIN PRIVATE KEY-----{body}", "abc-"),
            (f"GET /bot123:abcdef-----BEGIN PRIVATE KEY-----\n{body}\n-----END PRIVATE KEY----- ok", "abcdef"),
        ):
            redacted = redact_sensitive_text(text)
            self.assertNotIn(value, redacted, text)
            self.assertNotIn(body, redacted, text)
            self.assertNotIn("MC4CAQAw", redacted, text)

    def test_e6_is_strictly_additive_over_the_pre_e6_chain(self):
        """R2-4 differential: no token survives the full chain that the pre-E6
        chain (``_base_chain``, byte-identical) redacted. Corpus: seeded random
        concatenations of secrets, labels, PEM markers and glue characters."""
        import random
        import re

        from src.nadobro.core import log_redaction as lr

        pieces = [
            "private_key", "secret", "api_secret", "signing_key", "x-signature", "token", "password",
            "=", ": ", '"', "'", " ", "\n", "-----BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY-----",
            "-----BEGIN ", "-----BEGIN CERTIFICATE-----", "-----END CERTIFICATE-----", "-----", "BEGIN",
            "postgres://user:pass@db.supabase.co/x", "https://u:p@host.com/", "0x" + "ab" * 20, "deadbeef" * 8,
            "1234567890", "1234567890:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "10.1.2.3", "fdaa::1", "fdaa:1234:5678",
            "abcDEF123xyz", "QUJDREVGR0hJSktMTU5PUA==", "MC4CAQAwBQYDK2VwBCIEI", "_", "-", "/", ".", ":",
            "/bot123:ABC", "sk-abcdef1234567890", "user=", "12345678", "id=987654321012", "Bearer abcdefghijklmnop",
            "Bearer ", "api_key=", "hunter2abcd", "subaccount=", "abcdef0123456789abcd", "account_id=",
            "123456789012", "x.pooler.supabase.com", "https://a.pinecone.io/x",
        ]
        token_re = re.compile(r"[A-Za-z0-9]{4,}")
        placeholder_re = re.compile(r"<REDACTED[A-Z_]*>")

        def tokens(text: str) -> set[str]:
            return set(token_re.findall(placeholder_re.sub(" ", text)))

        rng = random.Random(20260930)
        for _ in range(20_000):
            text = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 12)))
            revealed = tokens(redact_sensitive_text(text)) - tokens(lr._base_chain(text))
            self.assertEqual(revealed, set(), text)


if __name__ == "__main__":
    unittest.main()
