import sys
import types
import unittest

from _stubs import install_test_stubs

install_test_stubs()

if "openai" not in sys.modules:
    openai_mod = types.ModuleType("openai")

    class _OpenAI:
        def __init__(self, *args, **kwargs):
            self.base_url = kwargs.get("base_url") or "https://nano-gpt.com/api/v1"

    openai_mod.OpenAI = _OpenAI
    sys.modules["openai"] = openai_mod

if "cryptography.fernet" not in sys.modules:
    crypto_mod = types.ModuleType("cryptography")
    fernet_mod = types.ModuleType("cryptography.fernet")

    class _Fernet:
        def __init__(self, *args, **kwargs):
            pass

        @staticmethod
        def generate_key():
            return b"0" * 44

        def encrypt(self, value):
            return value

        def decrypt(self, value):
            return value

    class _InvalidToken(Exception):
        pass

    class _MultiFernet:
        def __init__(self, *args, **kwargs):
            pass

    fernet_mod.Fernet = _Fernet
    fernet_mod.MultiFernet = _MultiFernet
    fernet_mod.InvalidToken = _InvalidToken
    crypto_mod.fernet = fernet_mod
    sys.modules["cryptography"] = crypto_mod
    sys.modules["cryptography.fernet"] = fernet_mod


class TradingBroUpgradeTests(unittest.TestCase):
    def test_classifier_treats_mm_build_question_as_learning(self):
        from src.nadobro.llm.conversation_intent import classify_conversation_intent

        intent = classify_conversation_intent(
            "How can I build a working MM bot which runs on telegram and has GRID, RGRID, Dynamic GRID and Volume bot?"
        )

        self.assertEqual(intent.name, "learn")
        self.assertGreaterEqual(intent.confidence, 0.8)

    def test_managed_agent_does_not_launch_for_educational_strategy_question(self):
        from src.nadobro.llm.managed_agent_service import _is_strategy_start_request

        self.assertFalse(
            _is_strategy_start_request(
                "How can I build a working MM bot which runs on telegram and has GRID and Dynamic GRID?"
            )
        )
        self.assertTrue(_is_strategy_start_request("start dgrid BTC 3x"))

    def test_trading_bro_question_preserves_user_message_for_router(self):
        from src.nadobro.llm.knowledge_service import _question_for_routing
        from src.nadobro.llm.trading_bro_service import build_trading_bro_question

        framed = build_trading_bro_question("How do I build an MM bot?", mode="strategy_design")

        self.assertIn("Mode: strategy_design", framed)
        self.assertEqual(_question_for_routing(framed), "How do I build an MM bot?")

    def test_source_policy_omits_generic_sources_and_keeps_live_provenance(self):
        from src.nadobro.llm.knowledge_service import (
            _append_freshness_if_live,
            _append_x_links_if_needed,
            _format_provenance_line,
            _should_include_provenance,
        )

        self.assertFalse(
            _should_include_provenance(
                "How do I build an MM bot?",
                "",
                ["https://docs.nado.xyz"],
            )
        )
        self.assertTrue(
            _should_include_provenance(
                "latest news on BTC",
                "[X/TWITTER RESULTS]\nSome result",
                ["https://x.com"],
            )
        )
        self.assertEqual(_format_provenance_line(["https://x.com"]), "Based on: X")
        self.assertEqual(
            _append_freshness_if_live("Clean answer", "latest Nado news", "[X/TWITTER RESULTS]"),
            "Clean answer",
        )
        self.assertIn(
            "https://x.com/nadoHQ/status/2050260192751751237",
            _append_x_links_if_needed(
                "Nado points dropped.",
                "Link: https://x.com/nadoHQ/status/2050260192751751237",
            ),
        )

    def test_bro_answer_card_and_markdown_links_render_safely(self):
        from src.nadobro.handlers.formatters import fmt_bro_answer_card, format_ai_response

        rendered = format_ai_response("Read [Nado docs](https://docs.nado.xyz) and use **risk controls**.")
        self.assertIn("[Nado docs](https://docs.nado.xyz)", rendered)
        self.assertIn("*risk controls*", rendered)

        card = fmt_bro_answer_card("## Build Plan\n1. Add router\n- Test it", mode="strategy_design")
        self.assertIn("*Strategy Builder*", card)
        self.assertIn("1\\. Add router", card)
        self.assertIn("\\- Test it", card)

    def test_referral_dashboard_renders_configured_bot_link_and_code(self):
        from src.nadobro.handlers.formatters import fmt_referral_dashboard

        card = fmt_referral_dashboard(
            {
                "network": "testnet",
                "total_referrals": 1,
                "total_referred_volume": 2_500,
                "total_referred_trades": 3,
                "share_code": {
                    "public_code": "ABCDEFGH",
                    "link": "https://t.me/Nadbro_bot?start=ref_ABCDEFGH",
                    "redemption_count": 4,
                },
                "has_code": True,
                "referred_users": [],
                "min_code_len": 3,
                "max_code_len": 20,
            }
        )

        self.assertIn("TESTNET", card)
        self.assertIn("*Your Referral Code*", card)
        self.assertIn("`ABCDEFGH`", card)
        self.assertIn("https://t\\.me/Nadbro\\_bot?start\\=ref\\_ABCDEFGH", card)
        self.assertIn("*Redemptions:* 4", card)

    def test_referral_dashboard_renders_claim_prompt_when_no_code(self):
        from src.nadobro.handlers.formatters import fmt_referral_dashboard

        card = fmt_referral_dashboard(
            {
                "network": "mainnet",
                "total_referrals": 0,
                "total_referred_volume": 0.0,
                "total_referred_trades": 0,
                "share_code": None,
                "has_code": False,
                "referred_users": [],
                "min_code_len": 3,
                "max_code_len": 20,
            }
        )

        self.assertIn("*Claim your code*", card)
        self.assertNotIn("`", card.split("*Direct Referrals*")[0])

    def test_trading_bro_prompt_uses_cool_buddy_tone(self):
        from src.nadobro.llm.trading_bro_service import build_trading_bro_question

        framed = build_trading_bro_question("Explain D-GRID", mode="educational_guide")

        self.assertIn("cool buddy", framed)
        self.assertIn("relaxed, helpful, confident", framed)

    def test_streaming_draft_updates_at_word_like_boundaries(self):
        from src.nadobro.handlers.messages import _should_update_streaming_draft

        self.assertTrue(_should_update_streaming_draft("This is a longer streamed chunk ", 0, 1.0, 2.0))
        self.assertFalse(_should_update_streaming_draft("This is a longer streamed chu", 0, 1.0, 2.0))

    def test_eth_chart_predict_is_market_call_not_quote(self):
        from src.nadobro.llm.conversation_intent import classify_conversation_intent, wants_market_call
        from src.nadobro.llm.knowledge_service import _question_for_routing, _should_direct_price_brief
        from src.nadobro.llm.trading_bro_service import answer_mode_for_text, build_trading_bro_question

        q = "Read the ETH chart on Nado, predict the market direction for 4hrly, up or down?"
        self.assertEqual(classify_conversation_intent(q).name, "chart_ta")
        self.assertTrue(wants_market_call(q))
        self.assertEqual(answer_mode_for_text(q), "market_call")
        framed = build_trading_bro_question(q)
        self.assertEqual(_question_for_routing(framed), q)
        self.assertFalse(_should_direct_price_brief(framed))
        self.assertFalse(_should_direct_price_brief(q))
        self.assertNotIn("on Nado", framed.split("User message:")[0])

    def test_eth_price_is_quote_lookup(self):
        from src.nadobro.llm.conversation_intent import classify_conversation_intent, wants_market_call
        from src.nadobro.llm.knowledge_service import _should_direct_price_brief
        from src.nadobro.llm.trading_bro_service import answer_mode_for_text

        q = "What's the current ETH price"
        self.assertEqual(classify_conversation_intent(q).name, "quote")
        self.assertFalse(wants_market_call(q))
        self.assertEqual(answer_mode_for_text(q), "market_analysis")
        self.assertTrue(_should_direct_price_brief(q))

    def test_cbrs_earnings_is_event_predict(self):
        from src.nadobro.llm.conversation_intent import classify_conversation_intent, wants_market_call
        from src.nadobro.llm.knowledge_service import _should_direct_price_brief
        from src.nadobro.llm.trading_bro_service import answer_mode_for_text

        q = "CBRS earnings report after market close predict if miss or beat. predict up or down"
        self.assertEqual(classify_conversation_intent(q).name, "event_predict")
        self.assertTrue(wants_market_call(q))
        self.assertEqual(answer_mode_for_text(q), "market_call")
        self.assertFalse(_should_direct_price_brief(q))


if __name__ == "__main__":
    unittest.main()
