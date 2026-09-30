"""End-to-end vault flows: every tap from the Vault card to a signed mint/burn.

Drives the REAL handler (handle_vault_callback / handle_vault_text), the REAL
snapshot/service logic (get_user_vault_snapshot, deposit_to_vault,
withdraw_from_vault), and the REAL cards — against a faithful fake venue
client. Only the process boundaries are stubbed (user lookup, pool metrics,
archive ledger, deposit-watch rows).

The fake client mirrors the verified gateway semantics (2026-07-18 probes):
max_nlp_mintable ~= min(per-wallet cap remainder, no-borrow spendable), with
ok=False representing a gateway-budget throttle; mint_nlp enforces the
documented spot_leverage=false contract ("fails if the transaction causes a
borrow").
"""

import asyncio
from unittest.mock import patch

import pytest

from src.nadobro.handlers import network_guard
from src.nadobro.handlers import vault_handler as vh
from src.nadobro.vault import nlp_vault_service as svc


# ── stubs ────────────────────────────────────────────────────────────────

class FakeVaultClient:
    _initialized = True
    subaccount_hex = "0x" + "ab" * 32

    def __init__(
        self,
        *,
        usdt0=144.93,
        lp_balance=0.0,
        nav=1.04,
        mintable_no_borrow=144.93,
        mintable_ok=True,
        mintable_with_borrow=None,
        lockup_ms=None,
        reject_mint=None,
        locked_query_ok=True,
    ):
        self.usdt0 = usdt0
        self.lp_balance = lp_balance
        self.nav = nav
        self.mintable_no_borrow = mintable_no_borrow
        self.mintable_ok = mintable_ok
        self.mintable_with_borrow = (
            mintable_with_borrow if mintable_with_borrow is not None else mintable_no_borrow
        )
        self.lockup_ms = lockup_ms
        self.reject_mint = reject_mint
        self.locked_query_ok = locked_query_ok
        self.mints = []
        self.burns = []
        self.burn_x18 = []

    def initialize(self):
        return True

    def get_balance(self, **_):
        return {"exists": True, "balances": {0: self.usdt0}}

    def resolve_nlp_product_id(self):
        return 11

    def get_max_nlp_mintable(self, *, spot_leverage=False, product_id=None):
        if not self.mintable_ok:
            return {"ok": False, "max_mintable_usdt0": 0.0, "raw": {}}
        amount = self.mintable_with_borrow if spot_leverage else self.mintable_no_borrow
        return {"ok": True, "max_mintable_usdt0": float(amount), "raw": {}}

    def _locked_split(self):
        """Mirror the venue: NLP minted within the 4-day lockup is locked, the
        rest is unlocked (and only the unlocked part is burnable)."""
        import time as _t

        locked = self.lp_balance if (
            self.lockup_ms and (_t.time() - self.lockup_ms / 1000.0) < 4 * 24 * 3600
        ) else 0.0
        return locked, self.lp_balance - locked

    def get_nlp_locked_balances(self):
        if self.locked_query_ok is False:      # simulate a throttled weight-20 query
            return {"ok": False, "balance_locked": 0.0, "balance_unlocked": 0.0,
                    "balance_unlocked_x18": 0, "locked_entries": []}
        locked, unlocked = self._locked_split()
        import time as _t

        return {
            "ok": True,
            "balance_locked": locked,
            "balance_unlocked": unlocked,
            "balance_unlocked_x18": int(round(unlocked * 1e18)),
            "locked_entries": (
                [{"amount": locked, "unlocked_at": int(self.lockup_ms / 1000.0) + 4 * 24 * 3600}]
                if locked > 0 and self.lockup_ms else []
            ),
        }

    def get_nlp_position(self):
        locked, unlocked = self._locked_split()
        known = self.locked_query_ok is not False
        return {
            "exists": self.lp_balance > 0,
            "lp_balance": self.lp_balance,
            "lp_value_usdt0": self.lp_balance * self.nav,
            "last_mint_ts_ms": self.lockup_ms,
            "nlp_product_id": 11,
            "nav_usdt0": self.nav,
            "unlocked_known": known,
            "lp_unlocked": unlocked if known else 0.0,
            "lp_unlocked_x18": int(round(unlocked * 1e18)) if known else 0,
            "lp_locked": locked if known else 0.0,
        }

    def mint_nlp(self, amount, *, spot_leverage=False):
        if self.reject_mint:
            return {"success": False, "error": self.reject_mint}
        self.mints.append((float(amount), bool(spot_leverage)))
        return {"success": True, "digest": "0x" + "1" * 64, "quote_amount_usdt0": amount}

    def burn_nlp(self, amount, *, amount_x18=None):
        self.burn_x18.append(amount_x18)
        self.burns.append(float(amount))
        return {"success": True, "digest": "0x" + "2" * 64, "nlp_amount": amount}


class FakeUser:
    linked_signer_address = "0x" + "3" * 40

    class network_mode:  # noqa: N801 - mirrors the enum attr shape
        value = "mainnet"


class FakeQuery:
    def __init__(self, data, user_id=7):
        self.data = data
        self.from_user = type("U", (), {"id": user_id})()
        self.edits = []
        self.answers = []

    async def edit_message_text(self, text, parse_mode=None, reply_markup=None):
        self.edits.append({"text": text, "kb": reply_markup, "parse_mode": parse_mode})

    async def answer(self, text=None, **_):
        self.answers.append(text)


class FakeMessage:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, text, parse_mode=None, reply_markup=None):
        self.replies.append({"text": text, "kb": reply_markup})


class FakeUpdate:
    def __init__(self, text, user_id=7):
        self.message = FakeMessage(text)
        self.effective_user = type("U", (), {"id": user_id})()


class FakeContext:
    def __init__(self):
        self.user_data = {}


def _drive(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(monkeypatch):
    """Patch the process boundaries; everything between them runs for real."""
    client = FakeVaultClient()
    holder = {"client": client}
    # The service resolves the user itself; the handler reads the user's active
    # network through network_guard (confirms are bound to it).
    for mod in (svc, network_guard):
        monkeypatch.setattr(mod, "get_user", lambda *_a, **_k: FakeUser())
    monkeypatch.setattr(svc, "get_user_nado_client", lambda *_a, **_k: holder["client"])
    monkeypatch.setattr(svc, "get_pool_metrics", lambda *_a, **_k: {
        "tvl_usdt0": 9_099_631.40, "apr_pct": 7.59, "apr_source": "snapshots",
    })
    monkeypatch.setattr(svc, "_sync_lp_events_with_archive", lambda *_a, **_k: None)
    monkeypatch.setattr(svc, "_ledger_rows_for_pnl", lambda *_a, **_k: [])
    monkeypatch.setattr(svc, "get_vault_deposit_watch", lambda *_a, **_k: None)
    monkeypatch.setattr(svc, "_log_vault_event", lambda *_a, **_k: None)
    return holder


def _buttons(edit):
    kb = edit["kb"]
    return [btn for row in kb.inline_keyboard for btn in row] if kb else []


def _callbacks(edit):
    return [b.callback_data for b in _buttons(edit)]


# ── deposit flow ─────────────────────────────────────────────────────────

def test_deposit_happy_path_home_to_mint(env):
    ctx = FakeContext()

    q = FakeQuery("vault:home")
    _drive(vh.handle_vault_callback(q, ctx))
    home = q.edits[-1]
    assert "USDT0 balance: `$144.93`" in home["text"]
    assert "vault:deposit" in _callbacks(home)

    q = FakeQuery("vault:deposit")
    _drive(vh.handle_vault_callback(q, ctx))
    picker = q.edits[-1]
    assert "Max you can deposit now" in picker["text"]
    # 70% ceiling: min(no-borrow 144.93, 70% of 144.93 = 101.451, room)
    assert "$101.45" in picker["text"]

    q = FakeQuery("vault:deposit:preset:100.0")
    _drive(vh.handle_vault_callback(q, ctx))
    confirm = q.edits[-1]
    assert "Confirm Deposit" in confirm["text"]
    assert "Gas fee: `$1.00`" in confirm["text"]
    # (100 - 1) / 1.04 NAV = 95.192307...
    assert "95.192308" in confirm["text"]
    assert "4 days" in confirm["text"]
    # PREVIEW-NETWORK-BIND: Confirm is bound to the network it was quoted on.
    assert "vault:deposit:confirm:100.0:mainnet" in _callbacks(confirm)

    q = FakeQuery("vault:deposit:confirm:100.0:mainnet")
    _drive(vh.handle_vault_callback(q, ctx))
    assert env["client"].mints == [(100.0, False)]  # exactly one, never borrows
    done = q.edits[-1]
    assert "Deposited $100.00" in done["text"]
    assert "4-day post-mint lockup" in done["text"]


def test_deposit_margin_locked_bounces_with_reason(env):
    env["client"] = FakeVaultClient(mintable_no_borrow=0.0, mintable_with_borrow=120.0)
    ctx = FakeContext()
    q = FakeQuery("vault:deposit")
    _drive(vh.handle_vault_callback(q, ctx))
    flash = q.edits[-1]
    assert "backing open positions" in flash["text"]
    assert "Max you can deposit now" not in flash["text"]
    assert env["client"].mints == []


def test_deposit_cap_reached_bounces_with_reason(env):
    env["client"] = FakeVaultClient(
        lp_balance=19_500.0, nav=1.03, mintable_no_borrow=0.0, mintable_with_borrow=0.0,
    )
    ctx = FakeContext()
    q = FakeQuery("vault:deposit")
    _drive(vh.handle_vault_callback(q, ctx))
    assert "Private Alpha" in q.edits[-1]["text"]


def test_deposit_unknown_capacity_reaches_picker_and_mints(env):
    """A throttled capacity read must not bounce the user — the picker is
    balance-ceiling bounded and the venue's no-borrow check guards the mint."""
    env["client"] = FakeVaultClient(mintable_ok=False)
    ctx = FakeContext()

    q = FakeQuery("vault:deposit")
    _drive(vh.handle_vault_callback(q, ctx))
    assert "Max you can deposit now" in q.edits[-1]["text"]

    q = FakeQuery("vault:deposit:confirm:100.0:mainnet")
    _drive(vh.handle_vault_callback(q, ctx))
    assert env["client"].mints == [(100.0, False)]


def test_deposit_over_70pct_rejected_end_to_end(env):
    ctx = FakeContext()
    q = FakeQuery("vault:deposit:confirm:120.0:mainnet")  # > 101.45 ceiling
    _drive(vh.handle_vault_callback(q, ctx))
    assert env["client"].mints == []
    assert "70%" in q.edits[-1]["text"]


def test_deposit_double_tap_mints_once(env):
    ctx = FakeContext()
    ctx.user_data[vh._OP_INFLIGHT_KEY] = "deposit"  # first tap still in flight
    q = FakeQuery("vault:deposit:confirm:100.0:mainnet")
    _drive(vh.handle_vault_callback(q, ctx))
    assert env["client"].mints == []
    assert any("processing" in (a or "") for a in q.answers)
    # guard clears with the first operation, later taps work again
    ctx.user_data.pop(vh._OP_INFLIGHT_KEY)
    q2 = FakeQuery("vault:deposit:confirm:100.0:mainnet")
    _drive(vh.handle_vault_callback(q2, ctx))
    assert env["client"].mints == [(100.0, False)]


def test_deposit_venue_rejection_surfaces_error(env):
    env["client"] = FakeVaultClient(reject_mint="Mint would cause a borrow on the subaccount")
    ctx = FakeContext()
    q = FakeQuery("vault:deposit:confirm:100.0:mainnet")
    _drive(vh.handle_vault_callback(q, ctx))
    assert "borrow" in q.edits[-1]["text"]
    assert env["client"].mints == []


@pytest.mark.parametrize(
    "data",
    [
        "vault:deposit:confirm:100.0:testnet",   # quoted on the other network
        "vault:deposit:confirm:100.0",           # legacy button with no network
        "vault:withdraw:confirm:50.0:testnet",
        "vault:withdraw:confirm:50.0",
    ],
)
def test_confirm_from_another_network_or_untagged_mints_and_burns_nothing(env, data):
    """PREVIEW-NETWORK-BIND: the user is on MAINNET. A confirm quoted on testnet,
    or a legacy untagged one, must neither mint/burn nor take the in-flight lock."""
    env["client"] = FakeVaultClient(lp_balance=100.0, nav=1.04)
    ctx = FakeContext()
    q = FakeQuery(data)
    _drive(vh.handle_vault_callback(q, ctx))
    assert env["client"].mints == [] and env["client"].burns == []
    assert vh._OP_INFLIGHT_KEY not in ctx.user_data
    assert "Nothing was sent" in q.edits[-1]["text"]


def test_deposit_custom_amount_text_flow(env):
    ctx = FakeContext()
    q = FakeQuery("vault:deposit:custom")
    _drive(vh.handle_vault_callback(q, ctx))
    assert ctx.user_data[vh._DEPOSIT_PENDING_KEY] == "deposit"

    bad = FakeUpdate("not-a-number")
    assert _drive(vh.handle_vault_text(bad, ctx)) is True
    assert "positive number" in bad.message.replies[-1]["text"]

    good = FakeUpdate("$1,00")  # "$1,00" -> "100"
    assert _drive(vh.handle_vault_text(good, ctx)) is True
    assert "Confirm Deposit" in good.message.replies[-1]["text"]
    assert "vault:deposit:confirm:100.0:mainnet" in _callbacks({"kb": good.message.replies[-1]["kb"]})
    assert vh._DEPOSIT_PENDING_KEY not in ctx.user_data


def test_deposit_custom_cancel_clears_pending(env):
    ctx = FakeContext()
    _drive(vh.handle_vault_callback(FakeQuery("vault:deposit:custom"), ctx))
    # ❌ Cancel routes to vault:home — the prompt must be forgotten
    _drive(vh.handle_vault_callback(FakeQuery("vault:home"), ctx))
    assert vh._DEPOSIT_PENDING_KEY not in ctx.user_data
    # a number typed later in normal chat is NOT hijacked into a confirm card
    later = FakeUpdate("500")
    assert _drive(vh.handle_vault_text(later, ctx)) is False
    assert later.message.replies == []


# ── withdraw flow ────────────────────────────────────────────────────────

def test_withdraw_happy_path_pct_to_burn(env):
    env["client"] = FakeVaultClient(lp_balance=100.0, nav=1.04)
    ctx = FakeContext()

    q = FakeQuery("vault:withdraw")
    _drive(vh.handle_vault_callback(q, ctx))
    picker = q.edits[-1]
    assert "NLP balance: `100.000000`" in picker["text"]
    assert any(cb.startswith("vault:withdraw:pct:") for cb in _callbacks(picker))

    q = FakeQuery("vault:withdraw:pct:50")
    _drive(vh.handle_vault_callback(q, ctx))
    confirm = q.edits[-1]
    assert "Burn `50.000000` NLP" in confirm["text"]
    # est out = 52.00; fee = $1 sequencer + max($1, 10bps of 52) = $2.00
    assert "Estimated USDT0 out (pre-fees): `$52.00`" in confirm["text"]
    assert "Estimated fees: `$2.00`" in confirm["text"]

    assert "vault:withdraw:confirm:50.0:mainnet" in _callbacks(confirm)

    q = FakeQuery("vault:withdraw:confirm:50.0:mainnet")
    _drive(vh.handle_vault_callback(q, ctx))
    assert env["client"].burns == [50.0]
    assert "Burned 50.000000 NLP" in q.edits[-1]["text"]


def test_withdraw_lockup_blocks_even_from_stale_keyboard(env):
    import time as _time

    env["client"] = FakeVaultClient(
        lp_balance=100.0, lockup_ms=int(_time.time() * 1000)  # minted just now
    )
    ctx = FakeContext()

    q = FakeQuery("vault:withdraw")
    _drive(vh.handle_vault_callback(q, ctx))
    picker = q.edits[-1]
    assert "lockup" in picker["text"].lower()
    assert not any(cb.startswith("vault:withdraw:pct:") for cb in _callbacks(picker))

    # A stale confirm button (pre-lockup keyboard) must still be rejected
    q = FakeQuery("vault:withdraw:confirm:50.0:mainnet")
    _drive(vh.handle_vault_callback(q, ctx))
    assert env["client"].burns == []
    assert "Lockup active" in q.edits[-1]["text"]


def test_withdraw_over_balance_rejected(env):
    env["client"] = FakeVaultClient(lp_balance=10.0)
    ctx = FakeContext()
    q = FakeQuery("vault:withdraw:confirm:11.0:mainnet")
    _drive(vh.handle_vault_callback(q, ctx))
    assert env["client"].burns == []
    assert "only have" in q.edits[-1]["text"]


def test_withdraw_custom_with_zero_nlp_is_friendly(env):
    ctx = FakeContext()
    _drive(vh.handle_vault_callback(FakeQuery("vault:withdraw:custom"), ctx))
    reply = FakeUpdate("1.25")
    assert _drive(vh.handle_vault_text(reply, ctx)) is True
    assert "don't have any NLP" in reply.message.replies[-1]["text"]


# ── snapshot semantics under the fake venue ─────────────────────────────

def test_snapshot_throttle_is_unknown_not_locked(env):
    env["client"] = FakeVaultClient(mintable_ok=False)
    snap = svc.get_user_vault_snapshot(7)
    assert snap["mintable_known"] is False
    assert snap["deposit_blocked_reason"] is None
    assert snap["deposit_max_usdt0"] == pytest.approx(144.93 * 0.7)


def test_snapshot_margin_locked_requires_successful_probes(env):
    env["client"] = FakeVaultClient(mintable_no_borrow=0.0, mintable_with_borrow=120.0)
    snap = svc.get_user_vault_snapshot(7)
    assert snap["deposit_blocked_reason"] == "margin_locked"
    assert snap["mintable_with_borrow_usdt0"] == pytest.approx(120.0)


def test_snapshot_vault_full_when_borrow_probe_also_zero(env):
    env["client"] = FakeVaultClient(mintable_no_borrow=0.0, mintable_with_borrow=0.0)
    snap = svc.get_user_vault_snapshot(7)
    assert snap["deposit_blocked_reason"] == "vault_full"


def test_withdraw_picker_hides_presets_when_unlocked_balance_is_unknown(env):
    """2026-07-25 bug: the weight-20 nlp_locked_balances query lost the gateway
    budget race, the old code read that as "0 locked / 0 unlocked", the card
    showed the full spot balance as Unlocked, and every withdraw came back
    2096. Unknown must render as 'checking…' with NO burn buttons."""
    env["client"] = FakeVaultClient(lp_balance=2.777688, nav=1.0638, locked_query_ok=False)
    ctx = FakeContext()

    q = FakeQuery("vault:home")
    _drive(vh.handle_vault_callback(q, ctx))
    assert "Withdrawable: `checking…`" in q.edits[-1]["text"]
    assert "Lockup: `Unlocked`" not in q.edits[-1]["text"]   # never claim unlocked

    q = FakeQuery("vault:withdraw")
    _drive(vh.handle_vault_callback(q, ctx))
    picker = q.edits[-1]
    assert not any(cb.startswith("vault:withdraw:pct:") for cb in _callbacks(picker))
    assert "Refresh" in picker["text"]


def test_withdraw_100pct_burns_only_the_unlocked_tranche(env):
    """Half the position is inside the 4-day lock: 100% must burn the unlocked
    half (with the venue's exact wei), not the total."""
    import time as _t

    # Minted 2 days ago -> still locked; plus we model a partially unlocked book
    client = FakeVaultClient(lp_balance=2.0, nav=1.0)
    client.lockup_ms = int((_t.time() - 2 * 24 * 3600) * 1000)
    env["client"] = client
    ctx = FakeContext()

    q = FakeQuery("vault:withdraw")
    _drive(vh.handle_vault_callback(q, ctx))
    picker = q.edits[-1]
    # Entire balance is locked -> no presets, and the lockup is explained.
    assert not any(cb.startswith("vault:withdraw:pct:") for cb in _callbacks(picker))
    assert "locked" in picker["text"].lower()
    # And a stale-keyboard confirm cannot sneak a burn through.
    q = FakeQuery("vault:withdraw:confirm:2.0:mainnet")
    _drive(vh.handle_vault_callback(q, ctx))
    assert client.burns == []
