"""Per-site goldens for the Arcus-P1 network-coercion swap (SPEC-A Tier 1).

Every site that used to coerce "whatever network string arrived" into a Nado
network with a local ternary now goes through ``utils.venue_scope``. For each
swapped site this file drives the REAL code path (SQL captured by a spy, URLs /
pids / modes returned or recorded) over the P1 golden inputs and asserts:

* the observable output equals the hard-coded legacy golden (the values probed
  on the pre-swap code, SPEC-A §2), and equals a verbatim copy of the former
  expression (the "oracle") for the extra strip/case/enum inputs;
* ``arcus_mainnet`` / ``arcus_testnet`` (and case/whitespace variants) raise
  VenueScopeError instead of silently landing on a Nado network.

Two sites cannot be driven cheaply end to end (``bot_runtime.start_user_bot``
and ``nado_sync.sync_user``'s poll gateway); they are pinned by AST to the
exact helper call + policy, and that policy is golden-tested in
tests/utils/test_venue_scope.py.
"""
from __future__ import annotations

import ast
import enum
import pathlib
import re
from types import SimpleNamespace

import pytest

from src.nadobro.utils.venue_scope import VenueScopeError

REPO = pathlib.Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "nadobro"


class _Mode(enum.Enum):  # a plain Enum, like models.database.NetworkMode
    TESTNET = "testnet"
    MAINNET = "mainnet"


GOLDEN = [None, "", "testnet", "mainnet", "MAINNET", "Testnet", "arcus_mainnet", "arcus_testnet", "garbage"]
EXTRA = [" testnet", "testnet ", " Mainnet", "ARCUS_MAINNET", " arcus_mainnet", _Mode.TESTNET]
RAISE = "RAISE"


def _is_arcus(v) -> bool:
    return isinstance(v, str) and v.strip().lower().startswith("arcus")


def _outcome(fn, v):
    try:
        return fn(v)
    except VenueScopeError:
        return RAISE


def _check(fn, golden, oracle):
    """``golden``: expected outputs for GOLDEN (RAISE for arcus). ``oracle``:
    the verbatim legacy expression, checked on every non-arcus EXTRA input."""
    assert [_outcome(fn, v) for v in GOLDEN] == golden
    for v in EXTRA:
        got = _outcome(fn, v)
        if _is_arcus(v):
            assert got == RAISE, v
        else:
            assert got == oracle(v), v


# Legacy oracles (verbatim former expressions).
def _lt(v):  # str(v).lower() == "testnet" → testnet else mainnet
    return "testnet" if str(v).lower() == "testnet" else "mainnet"


def _xm(v):  # v == "mainnet" → mainnet else testnet
    return "mainnet" if v == "mainnet" else "testnet"


T, M, X = "testnet", "mainnet", RAISE
LT_GOLDEN = [M, M, T, M, M, T, X, X, M]
XM_GOLDEN = [T, T, T, M, T, T, X, X, T]
LM_GOLDEN = [T, T, T, M, M, T, X, X, T]


# ---------------------------------------------------------------------------
# SQL spy for the trades_<network> / funding_payments_<network> readers
# ---------------------------------------------------------------------------
_TABLE_RE = re.compile(r"\b((?:trades|funding_payments)_[a-z]+)\b")


class _SqlSpy:
    def __init__(self, one=None):
        self.sql: list[str] = []
        self._one = one

    def _rec(self, sql):
        self.sql.append(str(sql))

    def query_one(self, sql, params=None, *a, **k):
        self._rec(sql)
        return self._one(str(sql)) if self._one else None

    def query_all(self, sql, params=None, *a, **k):
        self._rec(sql)
        return []

    def execute(self, sql, params=None, *a, **k):
        self._rec(sql)
        return None

    def execute_returning(self, sql, params=None, *a, **k):
        self._rec(sql)
        return None

    def tables(self) -> tuple[str, ...]:
        return tuple(sorted(set(_TABLE_RE.findall("\n".join(self.sql)))))


def _patch_db(monkeypatch, module, spy):
    for name in ("query_one", "query_all", "execute", "execute_returning"):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, getattr(spy, name))


_DB_SITES = {
    # A1..A9, A12 (models/database.py) — each must read exactly trades_<legacy>.
    "settle_closed_manual_positions": lambda d, v: d.settle_closed_manual_positions(1, v),
    "rollup_session_from_trades": lambda d, v: d.rollup_session_from_trades(1, v),
    "get_session_live_metrics": lambda d, v: d.get_session_live_metrics(1, v, user_id=1),
    "get_session_turnover": lambda d, v: d.get_session_turnover(1, v, 2, "2026-01-01T00:00:00+00:00"),
    "get_account_realized_pnl_windows": lambda d, v: d.get_account_realized_pnl_windows(1, v),
    "get_analytics_fills": lambda d, v: d.get_analytics_fills(1, v),
    "backfill_via_nadobro": lambda d, v: d.backfill_via_nadobro(v),
    "get_paired_trades": lambda d, v: d.get_paired_trades(1, v),
    "get_session_recent_fills": lambda d, v: d.get_session_recent_fills(1, v, user_id=1),
    "get_session_net_base_by_product": lambda d, v: d.get_session_net_base_by_product(1, v),
}


@pytest.mark.parametrize("fn_name", sorted(_DB_SITES))
def test_models_trades_readers(monkeypatch, fn_name):
    from src.nadobro.models import database

    def run(v):
        spy = _SqlSpy()
        _patch_db(monkeypatch, database, spy)
        _DB_SITES[fn_name](database, v)
        return spy.tables()

    golden = [RAISE if g == RAISE else (f"trades_{g}",) for g in LT_GOLDEN]
    _check(run, golden, lambda v: (f"trades_{_lt(v)}",))


def test_models_rollup_engine_session_pnl_funding(monkeypatch):
    # A10 + A11: trades_ and funding_payments_ follow the same legacy test.
    from src.nadobro.models import database

    sess = {"user_id": 1, "product_id": 2, "started_at": "2026-01-01", "stopped_at": None}

    def run(v):
        spy = _SqlSpy(one=lambda sql: dict(sess) if "FROM strategy_sessions WHERE id" in sql else None)
        _patch_db(monkeypatch, database, spy)
        database.rollup_engine_session_pnl_funding(1, v)
        return spy.tables()

    golden = [RAISE if g == RAISE else (f"funding_payments_{g}", f"trades_{g}") for g in LT_GOLDEN]
    _check(run, golden, lambda v: (f"funding_payments_{_lt(v)}", f"trades_{_lt(v)}"))


def test_models_userrow_network_mode():
    # F1: empty → mainnet; anything not exactly "mainnet" → testnet ('MAINNET' → testnet!).
    from src.nadobro.models.database import UserRow

    def run(v):
        return UserRow({"telegram_id": 1, "network_mode": v}).network_mode.value

    _check(
        run,
        [M, M, T, M, T, T, X, X, T],
        lambda v: "mainnet" if (v or "mainnet") == "mainnet" else "testnet",
    )


def test_trade_service_compute_round_trips(monkeypatch):
    # A13 — imports db.query_all function-locally.
    from src.nadobro import db
    from src.nadobro.trading import trade_service

    def run(v):
        spy = _SqlSpy()
        monkeypatch.setattr(db, "query_all", spy.query_all)
        trade_service.compute_round_trips(1, v)
        return spy.tables()

    golden = [RAISE if g == RAISE else (f"trades_{g}",) for g in LT_GOLDEN]
    _check(run, golden, lambda v: (f"trades_{_lt(v)}",))


def test_engine_persistence_recorder_dedupe_probe(monkeypatch):
    # A14 — network comes from the controller id; the probe table follows LT.
    from src.nadobro import db
    from src.nadobro.engine.types import TradeType
    from src.nadobro.trading import engine_persistence as ep

    monkeypatch.setattr(ep, "_resolve_engine_fill_product", lambda *a, **k: (2, "BTC-PERP"))

    def run(v):
        spy = _SqlSpy(one=lambda sql: {"hit": 1})  # already synced → return before insert
        monkeypatch.setattr(db, "query_one", spy.query_one)
        rec = ep.DbTradeRecorder()
        rec._resolve_session_id = lambda *a, **k: 7
        rec._record(
            f"grid:1:{'' if v is None else v}", "BTC-PERP", TradeType.BUY, "0.01", "60000", "0.1",
            "0xabc", None, realized_pnl=None, is_taker=False,
        )
        return spy.tables()

    # None cannot survive the controller-id string; it arrives as '' (→ mainnet).
    golden = [RAISE if g == RAISE else (f"trades_{g}",) for g in LT_GOLDEN]
    _check(run, golden, lambda v: (f"trades_{_lt(v)}",))


def test_pnl_card_builder_net_funding(monkeypatch):
    # A15
    from src.nadobro.portfolio import pnl_card_builder as pcb

    def run(v):
        spy = _SqlSpy(one=lambda sql: {"paid_x18": 0})
        monkeypatch.setattr(pcb, "query_one", spy.query_one)
        pcb._net_funding_usd({"product_id": 2, "started_at": "2026-01-01", "user_id": 1}, v)
        return spy.tables()

    golden = [RAISE if g == RAISE else (f"funding_payments_{g}",) for g in LT_GOLDEN]
    _check(run, golden, lambda v: (f"funding_payments_{_lt(v)}",))


def test_bot_runtime_resolve_session_network():
    # A16 — first truthy field wins; all falsy → "mainnet".
    from src.nadobro.strategy.bot_runtime import _resolve_session_network

    _check(lambda v: _resolve_session_network({"network": v}), LT_GOLDEN, lambda v: _lt(v) if v else "mainnet")
    # later probe keys keep their legacy precedence
    assert _resolve_session_network({"network": "", "network_mode": "Testnet"}) == "testnet"
    with pytest.raises(VenueScopeError):
        _resolve_session_network({"network": None, "selected_network": "arcus_mainnet"})


def test_nado_sync_normalize_network_and_table_suffix():
    # A18 (+ every caller: cache keys, _write_matches / _write_funding tables).
    from src.nadobro.venue import nado_sync

    _check(nado_sync._normalize_network, LT_GOLDEN, _lt)
    _check(nado_sync._network_table_suffix, LT_GOLDEN, _lt)


def test_nado_sync_gateway_circuit_probe(monkeypatch):
    # A19 — gateway picked via the normalizer; the probe swallows errors (→ False).
    from src.nadobro.config import NADO_MAINNET_REST, NADO_TESTNET_REST
    from src.nadobro.core import http_session
    from src.nadobro.venue import nado_sync

    seen: list[str] = []
    monkeypatch.setattr(http_session, "is_circuit_open", lambda gw: seen.append(gw) or False)
    url = {"testnet": NADO_TESTNET_REST, "mainnet": NADO_MAINNET_REST}

    def run(v):
        seen.clear()
        assert nado_sync._gateway_circuit_open(v) is False
        if not seen:
            raise VenueScopeError("swallowed")  # arcus: never reached a gateway
        return seen[-1]

    _check(run, [RAISE if g == RAISE else url[g] for g in LT_GOLDEN], lambda v: url[_lt(v)])


def test_nado_sync_gateway_for_is_canonical_only():
    from src.nadobro.config import NADO_MAINNET_REST, NADO_TESTNET_REST
    from src.nadobro.venue import nado_sync

    assert nado_sync._gateway_for("testnet") == NADO_TESTNET_REST
    assert nado_sync._gateway_for("mainnet") == NADO_MAINNET_REST
    with pytest.raises(KeyError):
        nado_sync._gateway_for("arcus_mainnet")


def test_referral_normalize_network():
    # B1 — strip + lower; valid referral network kept, else mainnet.
    from src.nadobro.users.referral_service import normalize_network

    def legacy(v):
        value = str(v or "mainnet").strip().lower()
        return value if value in ("mainnet", "testnet") else "mainnet"

    _check(normalize_network, LT_GOLDEN, legacy)


def test_user_service_update_trade_stats(monkeypatch):
    # B2 — volume column + the referral network actually recorded downstream.
    from src.nadobro.users import referral_service, user_service

    def legacy_referral(v):
        value = str(v or "mainnet").strip().lower()
        return value if value in ("mainnet", "testnet") else "mainnet"

    def run(v):
        spy = _SqlSpy()
        forwarded: list = []
        monkeypatch.setattr(user_service, "execute", spy.execute)
        monkeypatch.setattr(user_service, "invalidate_user_cache", lambda *a, **k: None)
        monkeypatch.setattr(
            referral_service, "record_referred_volume",
            lambda uid, vol, *, network, **k: forwarded.append(network),
        )
        user_service.update_trade_stats(1, 10.0, network=v)
        column = re.search(r"\b(testnet|mainnet)_volume_usd = ", spy.sql[0]).group(1)
        # Downstream, record_referred_volume normalizes what it is handed.
        return column, legacy_referral(forwarded[0])

    def legacy(v):
        folded = str(v or "mainnet").strip().lower()
        col = "testnet" if folded == "testnet" else "mainnet"
        return col, legacy_referral(folded)

    _check(run, [RAISE if g == RAISE else (g, g) for g in LT_GOLDEN], legacy)


def test_config_builder_routing(monkeypatch):
    # B3 — a non-str is mainnet; strip+lower "testnet" bypasses the builder.
    from src.nadobro import config

    monkeypatch.setenv("NADO_BUILDER_ID", "7")
    monkeypatch.delenv("NADO_BUILDER_FEE_RATE", raising=False)
    mainnet = config.get_nado_builder_routing_config("mainnet")
    assert mainnet != (0, 0)

    def legacy(v):
        return (0, 0) if isinstance(v, str) and v.strip().lower() == "testnet" else mainnet

    _check(
        config.get_nado_builder_routing_config,
        [mainnet, mainnet, (0, 0), mainnet, mainnet, (0, 0), X, X, mainnet],
        legacy,
    )


def _bare_client(network):
    """A NadoClient with ``network`` set directly — bypasses the constructor
    guards so the per-method site itself is what is under test."""
    from src.nadobro.venue.nado_client import NadoClient

    c = NadoClient.__new__(NadoClient)
    c.network = network
    c.private_key = "0x" + "11" * 32
    c.client = None
    c.address = None
    c.main_address = None
    c.subaccount_hex = None
    c.acting_user_id = None
    c._initialized = False
    return c


def test_nado_client_initialize_sdk_mode(monkeypatch):
    # C1 — exact "testnet" → TESTNET SDK, EVERYTHING else → MAINNET SDK.
    import nado_protocol.client as sdk

    modes: list = []

    def fake_create(mode, key):
        modes.append(mode)
        raise RuntimeError("stop after mode selection")

    monkeypatch.setattr(sdk, "create_nado_client", fake_create)

    def run(v):
        modes.clear()
        assert _bare_client(v).initialize() is False  # initialize swallows errors
        if not modes:
            raise VenueScopeError("never selected an SDK mode")
        return "testnet" if modes[-1] == sdk.NadoClientMode.TESTNET else "mainnet"

    _check(run, [M, M, T, M, M, M, X, X, M], lambda v: "testnet" if v == "testnet" else "mainnet")


def test_nado_client_rest_and_archive_urls():
    # D1 / D2 — exact "mainnet" → prod, EVERYTHING else → test (split-brain vs C1 kept).
    from src.nadobro.config import (
        NADO_MAINNET_ARCHIVE,
        NADO_MAINNET_REST,
        NADO_TESTNET_ARCHIVE,
        NADO_TESTNET_REST,
    )

    rest = {"testnet": NADO_TESTNET_REST, "mainnet": NADO_MAINNET_REST}
    arch = {"testnet": NADO_TESTNET_ARCHIVE, "mainnet": NADO_MAINNET_ARCHIVE}
    _check(lambda v: _bare_client(v)._rest_url(), [RAISE if g == RAISE else rest[g] for g in XM_GOLDEN],
           lambda v: rest[_xm(v)])
    _check(lambda v: _bare_client(v)._archive_url(), [RAISE if g == RAISE else arch[g] for g in XM_GOLDEN],
           lambda v: arch[_xm(v)])


def test_nado_client_nlp_default_product_id(monkeypatch):
    # D3 — the per-network default the resolver falls back to.
    monkeypatch.delenv("NADO_NLP_PRODUCT_ID", raising=False)

    def run(v):
        c = _bare_client(v)
        c._nlp_product_id = None
        c._query_rest = lambda *a, **k: {}
        return c.resolve_nlp_product_id()

    _check(run, [RAISE if g == RAISE else {"testnet": 1, "mainnet": 11}[g] for g in XM_GOLDEN],
           lambda v: 11 if v == "mainnet" else 1)


def test_nado_archive_urls():
    # D4 / D5
    from src.nadobro.config import (
        NADO_MAINNET_ARCHIVE,
        NADO_MAINNET_ARCHIVE_REWARDS,
        NADO_TESTNET_ARCHIVE,
        NADO_TESTNET_ARCHIVE_REWARDS,
    )
    from src.nadobro.venue import nado_archive

    arch = {"testnet": NADO_TESTNET_ARCHIVE, "mainnet": NADO_MAINNET_ARCHIVE}
    rew = {"testnet": NADO_TESTNET_ARCHIVE_REWARDS, "mainnet": NADO_MAINNET_ARCHIVE_REWARDS}
    _check(nado_archive.archive_url_for_network, [RAISE if g == RAISE else arch[g] for g in XM_GOLDEN],
           lambda v: arch[_xm(v)])
    _check(nado_archive.archive_rewards_url_for_network,
           [RAISE if g == RAISE else rew[g] for g in XM_GOLDEN], lambda v: rew[_xm(v)])


def test_nado_ws_urls():
    # D6 (+ alias) / D7 — str(v) == "mainnet" → prod, else test.
    from src.nadobro.venue import nado_ws, nado_ws_actions

    def sub(net):
        return f"wss://gateway.{'prod' if net == 'mainnet' else 'test'}.nado.xyz/v1/subscribe"

    def act(net):
        return f"wss://gateway.{'prod' if net == 'mainnet' else 'test'}.nado.xyz/ws/v2"

    def str_xm(v):
        return "mainnet" if str(v) == "mainnet" else "testnet"

    golden_sub = [RAISE if g == RAISE else sub(g) for g in XM_GOLDEN]
    _check(nado_ws.subscribe_url_for_network, golden_sub, lambda v: sub(str_xm(v)))
    _check(nado_ws.ws_url_for_network, golden_sub, lambda v: sub(str_xm(v)))
    _check(nado_ws_actions.actions_url_for_network, [RAISE if g == RAISE else act(g) for g in XM_GOLDEN],
           lambda v: act(str_xm(v)))


def test_product_catalog_urls():
    # E1 / E2 — case-insensitive "mainnet" → prod, else test.
    from src.nadobro.config import (
        NADO_MAINNET_ARCHIVE,
        NADO_MAINNET_REST,
        NADO_TESTNET_ARCHIVE,
        NADO_TESTNET_REST,
    )
    from src.nadobro.venue import product_catalog as pc

    def lm(v):
        return "mainnet" if str(v).lower() == "mainnet" else "testnet"

    rest = {"testnet": NADO_TESTNET_REST, "mainnet": NADO_MAINNET_REST}
    v2 = {k: str(u).rstrip("/").replace("/v1", "/v2") for k, u in
          {"testnet": NADO_TESTNET_ARCHIVE, "mainnet": NADO_MAINNET_ARCHIVE}.items()}
    _check(pc._rest_url, [RAISE if g == RAISE else rest[g] for g in LM_GOLDEN], lambda v: rest[lm(v)])
    _check(pc._archive_v2_url, [RAISE if g == RAISE else v2[g] for g in LM_GOLDEN], lambda v: v2[lm(v)])


def test_tooling_data_env():
    # E3
    from src.nadobro.venue.nado_tooling_service import _network_to_data_env

    env = {"testnet": "nadoTestnet", "mainnet": "nadoMainnet"}
    _check(_network_to_data_env, [RAISE if g == RAISE else env[g] for g in LM_GOLDEN],
           lambda v: "nadoMainnet" if str(v or "").lower() == "mainnet" else "nadoTestnet")


def test_howl_recent_bro_trades_table(monkeypatch):
    # C2 — exact "testnet"/"mainnet" keep their table, anything else trades_mainnet.
    from src.nadobro.llm import howl_service

    def run(v):
        spy = _SqlSpy()
        monkeypatch.setattr(howl_service, "query_all", spy.query_all)
        howl_service.get_recent_bro_trades(1, network=v)
        return spy.tables()

    _check(
        run,
        [RAISE if g == RAISE else (f"trades_{g}",) for g in [M, M, T, M, M, M, X, X, M]],
        lambda v: (f"trades_{v}" if v in ("testnet", "mainnet") else "trades_mainnet",),
    )


def test_vault_snapshot_nlp_default(monkeypatch):
    # D8 — the fallback NLP pid when the client cannot resolve one.
    from src.nadobro.vault import nlp_vault_service as nvs

    class _Stop(Exception):
        pass

    pids: list[int] = []

    class _Client:
        _initialized = True

        def get_balance(self):
            return {}

        def resolve_nlp_product_id(self):
            return 0

        def get_max_nlp_mintable(self, *, spot_leverage, product_id):
            pids.append(product_id)
            raise _Stop()

    monkeypatch.setattr(nvs, "get_user", lambda uid: SimpleNamespace())
    monkeypatch.setattr(nvs, "get_user_nado_client", lambda uid: _Client())

    def run(v):
        pids.clear()
        monkeypatch.setattr(nvs, "_user_network", lambda user: v)
        with pytest.raises(_Stop):
            try:
                nvs.get_user_vault_snapshot(1)
            except VenueScopeError:
                raise _Stop() from None
        if not pids:
            raise VenueScopeError("no pid chosen")
        return pids[-1]

    _check(run, [RAISE if g == RAISE else {"testnet": 1, "mainnet": 11}[g] for g in XM_GOLDEN],
           lambda v: 11 if v == "mainnet" else 1)


# ---------------------------------------------------------------------------
# AST pins for the two sites not driven end to end
# ---------------------------------------------------------------------------

def _calls_in(path: pathlib.Path, func_name: str) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return [n for n in ast.walk(node) if isinstance(n, ast.Call)]
    raise AssertionError(f"{func_name} not found in {path}")


def test_start_user_bot_persists_network_through_the_lt_helper():
    # A17: state["network"] = coerce_nado_network(network, LOWER_ELSE_MAINNET, ...)
    path = SRC / "strategy" / "bot_runtime.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef)
              and n.name == "start_user_bot")
    hits = []
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Subscript)
            and isinstance(node.targets[0].slice, ast.Constant)
            and node.targets[0].slice.value == "network"
            and isinstance(node.value, ast.Call)
            and getattr(node.value.func, "id", None) == "coerce_nado_network"
        ):
            args = node.value.args
            hits.append((args[0].id, args[1].id))
    assert hits == [("network", "LOWER_ELSE_MAINNET")]


def test_sync_user_poll_gateway_indexes_the_normalized_network():
    # D9: network is normalized at the top of sync_user; the poll gateway is
    # _gateway_for(network) — never a raw-value ternary.
    calls = _calls_in(SRC / "venue" / "nado_sync.py", "sync_user")
    names = [getattr(c.func, "id", None) for c in calls]
    assert "_normalize_network" in names
    gw = [c for c in calls if getattr(c.func, "id", None) == "_gateway_for"]
    assert len(gw) == 1 and isinstance(gw[0].args[0], ast.Name) and gw[0].args[0].id == "network"


# Every Tier-1 site and the policy it must use (guards against a later edit
# quietly switching a site to a different legacy policy).
_SITE_POLICIES = {
    ("models/database.py", "_nado_ledger_network"): "LOWER_ELSE_MAINNET",
    ("models/database.py", "__init__"): "EMPTY_MAINNET_EXACT_ELSE_TESTNET",
    ("trading/trade_service.py", "compute_round_trips"): "LOWER_ELSE_MAINNET",
    ("trading/engine_persistence.py", "_record"): "LOWER_ELSE_MAINNET",
    ("portfolio/pnl_card_builder.py", "_net_funding_usd"): "LOWER_ELSE_MAINNET",
    ("strategy/bot_runtime.py", "_resolve_session_network"): "LOWER_ELSE_MAINNET",
    ("strategy/bot_runtime.py", "start_user_bot"): "LOWER_ELSE_MAINNET",
    ("venue/nado_sync.py", "_normalize_network"): "LOWER_ELSE_MAINNET",
    ("users/referral_service.py", "normalize_network"): "STRIP_LOWER_ELSE_MAINNET",
    ("users/user_service.py", "update_trade_stats"): "STRIP_LOWER_ELSE_MAINNET",
    ("config.py", "get_nado_builder_routing_config"): "STR_STRIP_LOWER_ELSE_MAINNET",
    ("venue/nado_client.py", "initialize"): "EXACT_ELSE_MAINNET",
    ("llm/howl_service.py", "get_recent_bro_trades"): "EXACT_ELSE_MAINNET",
    ("venue/nado_client.py", "_rest_url"): "EXACT_ELSE_TESTNET",
    ("venue/nado_client.py", "_archive_url"): "EXACT_ELSE_TESTNET",
    ("venue/nado_client.py", "resolve_nlp_product_id"): "EXACT_ELSE_TESTNET",
    ("venue/nado_archive.py", "archive_url_for_network"): "EXACT_ELSE_TESTNET",
    ("venue/nado_archive.py", "archive_rewards_url_for_network"): "EXACT_ELSE_TESTNET",
    ("vault/nlp_vault_service.py", "get_user_vault_snapshot"): "EXACT_ELSE_TESTNET",
    ("venue/nado_ws.py", "subscribe_url_for_network"): "STR_EXACT_ELSE_TESTNET",
    ("venue/nado_ws_actions.py", "actions_url_for_network"): "STR_EXACT_ELSE_TESTNET",
    ("venue/product_catalog.py", "_rest_url"): "LOWER_ELSE_TESTNET",
    ("venue/product_catalog.py", "_archive_v2_url"): "LOWER_ELSE_TESTNET",
    ("venue/nado_tooling_service.py", "_network_to_data_env"): "LOWER_ELSE_TESTNET",
}


class _CoerceCallCollector(ast.NodeVisitor):
    """Maps each ``coerce_nado_network(...)`` call to its innermost enclosing
    function and records the policy name it passes."""

    def __init__(self, rel: str, out: dict[tuple[str, str], list[str]]):
        self.rel, self.out, self.stack = rel, out, []

    def _visit_fn(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _visit_fn

    def visit_Call(self, node):
        if getattr(node.func, "id", None) == "coerce_nado_network":
            owner = self.stack[-1] if self.stack else "<module>"
            policy = node.args[1].id if len(node.args) > 1 and isinstance(node.args[1], ast.Name) else "?"
            self.out.setdefault((self.rel, owner), []).append(policy)
        self.generic_visit(node)


def _coerce_call_sites() -> dict[tuple[str, str], list[str]]:
    found: dict[tuple[str, str], list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts or path.name == "venue_scope.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        _CoerceCallCollector(str(path.relative_to(SRC)), found).visit(tree)
    return found


def test_every_coerce_call_site_uses_its_pinned_legacy_policy():
    found = _coerce_call_sites()
    for key, policy in _SITE_POLICIES.items():
        assert key in found, f"expected a coerce_nado_network call in {key}"
        assert set(found[key]) == {policy}, (key, found[key])
    unexpected = sorted(k for k in found if k not in _SITE_POLICIES)
    assert not unexpected, (
        "new coerce_nado_network call site(s): pin each one's legacy policy in "
        f"_SITE_POLICIES and add an end-to-end golden above: {unexpected}"
    )
