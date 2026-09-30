import unittest

from _stubs import install_test_stubs

install_test_stubs()

from src.nadobro.handlers import formatters
from src.nadobro.handlers import keyboards


class TelegramUiFormatterTests(unittest.TestCase):
    def test_home_command_center_card_uses_sectioned_layout(self):
        text = formatters.fmt_home_command_center_card("mainnet", "$119.39")
        self.assertIn("🎯 *Nadobro Command Center*", text)
        self.assertIn("*Quick snapshot*", text)
        self.assertIn("*Your toolkit*", text)
        self.assertIn("└ 🌐 *Execution Mode*", text)
        self.assertIn("place trades in plain English", text)

    def test_dashboard_home_returns_non_empty_message(self):
        text = formatters.fmt_dashboard_home()
        self.assertIsInstance(text, str)
        self.assertGreater(len(text.strip()), 0)
        self.assertIn("*Status*", text)

    def test_strategy_hub_intro_uses_tree_sections(self):
        text = formatters.fmt_strategy_hub_intro()
        self.assertIn("🧠 *Nadobro Strategy Lab*", text)
        self.assertIn("*How it works*", text)
        self.assertIn("├ Pick a strategy and I'll open its cockpit", text)
        self.assertIn("pre\\-trade readout", text)

    def test_wallet_connect_card_renders_the_key_verbatim_and_copyable(self):
        """Renamed from ..._escapes_private_key, which pinned the OPPOSITE of
        the correct behaviour: it asserted the key came out as `0xabc\\_def`,
        i.e. with a literal backslash injected by escape_md. Inside a
        MarkdownV2 code entity only ` and \\ are special, so that backslash was
        rendered verbatim and would have been COPIED as part of the key.

        The key now sits in a ``pre`` block (Telegram draws a copy button) and
        must appear byte-for-byte, unescaped."""
        text = formatters.fmt_wallet_connect_card("0xabc_def")
        self.assertIn("*Setup steps*", text)
        self.assertIn("```\n0xabc_def\n```", text)
        self.assertNotIn("0xabc\\_def", text)
        self.assertIn("tap to copy", text)
        self.assertIn("1\\-Click Trading", text)
        # '=' is MarkdownV2-reserved. Unescaped, Telegram rejected the whole
        # card and the plain-text fallback stripped the backticks — which is
        # what removed the key's copy affordance in the first place.
        self.assertIn("app\\.nado\\.xyz\\?join\\=FzpOSwX", text)

    def test_alert_menu_intro_uses_new_toolkit_copy(self):
        text = formatters.fmt_alert_menu_intro()
        self.assertIn("*What you can set*", text)
        self.assertIn("├ Price alerts", text)
        self.assertIn("└ PnL triggers", text)

    def test_home_keyboard_emoji_semantics_match(self):
        self.assertEqual(keyboards.HOME_MODULE_EMOJIS["trade"], "🤖")
        self.assertTrue(keyboards.HOME_BTN_TRADE.startswith("🤖"))
        self.assertTrue(keyboards.HOME_BTN_ALERTS.startswith("🔔"))
        self.assertTrue(keyboards.HOME_BTN_MODE.startswith("🌐"))

    # --- navigation / workflow uplift ---------------------------------------

    @staticmethod
    def _buttons(markup):
        return [b for row in markup.inline_keyboard for b in row]

    def test_module_labels_are_standardized(self):
        # No legacy names; one name per module.
        self.assertEqual(keyboards.HOME_BTN_ALERTS, "🔔 Alerts")
        self.assertEqual(keyboards.HOME_BTN_SETTINGS, "⚙️ Settings")
        self.assertEqual(keyboards.HOME_BTN_REFER, "🎁 Referrals")
        # Legacy labels still route (backward compatible).
        self.assertEqual(keyboards.REPLY_BUTTON_MAP.get("Control Panel"), "settings:view")
        self.assertEqual(keyboards.REPLY_BUTTON_MAP.get("Alert Engine"), "alert:menu")

    def test_home_card_surfaces_ask_nadobro_and_clean_names(self):
        cbs = [b.callback_data for b in self._buttons(keyboards.home_card_kb())]
        texts = [b.text for b in self._buttons(keyboards.home_card_kb())]
        self.assertIn("desk:view", cbs)  # Ask Nadobro surfaced
        self.assertIn("💬 Ask Nadobro", texts)
        self.assertTrue(any(t == "🔔 Alerts" for t in texts))
        self.assertTrue(any(t == "⚙️ Settings" for t in texts))
        self.assertFalse(any("Alert Engine" in t or "Control Panel" in t for t in texts))

    def test_getting_started_rail_links_wallet(self):
        cbs = [b.callback_data for b in self._buttons(keyboards.getting_started_kb())]
        self.assertIn("wallet:view", cbs)
        self.assertIn("card:trade:start", cbs)
        self.assertIn("nav:main", cbs)
        self.assertIn("🚀 *Getting Started*", formatters.fmt_getting_started())

    def test_empty_positions_offers_next_step_and_back(self):
        cbs = [b.callback_data for b in self._buttons(keyboards.positions_kb([], network="mainnet"))]
        self.assertIn("card:trade:start", cbs)
        self.assertIn("nav:strategy_hub", cbs)
        self.assertIn("portfolio:view", cbs)  # Back to Portfolio
        # With a position, show Close All instead of the empty CTA.
        cbs2 = [b.callback_data for b in self._buttons(
            keyboards.positions_kb([{"product_name": "BTC-PERP"}], network="mainnet"))]
        # The opener carries the network the positions were read from.
        self.assertIn("pos:close_all:mainnet", cbs2)
        self.assertNotIn("nav:strategy_hub", cbs2)

    def test_trade_confirm_puts_cancel_left_of_confirm(self):
        first_row = keyboards.trade_confirm_kb(network="mainnet").inline_keyboard[0]
        self.assertEqual(len(first_row), 2)
        self.assertIn("Cancel", first_row[0].text)
        self.assertIn("Confirm", first_row[1].text)


if __name__ == "__main__":
    unittest.main()
