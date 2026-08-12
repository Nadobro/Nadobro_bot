from _stubs import install_test_stubs

install_test_stubs()

def test_source_registry_records_freshness():
    from src.nadobro.connectors.source_registry import SourceRegistry

    registry = SourceRegistry()
    rec = registry.record("coinmarketcap", ttl_seconds=120, detail="quotes")

    assert rec.provider == "coinmarketcap"
    assert not rec.is_stale
    assert "quotes" in registry.freshness_footer()


def test_provider_catalog_contains_minara_equivalent_tools(monkeypatch):
    monkeypatch.setenv("DMIND_API_KEY", "dev")

    from src.nadobro.connectors.provider_catalog import provider_catalog

    names = {p["provider"] for p in provider_catalog()}
    assert {"nanogpt", "dmind", "n8n", "arkham", "coinglass", "defillama", "glassnode", "rootdata", "goplus", "fmp"} <= names
    assert next(p for p in provider_catalog() if p["provider"] == "dmind")["configured"] is True


def test_dmind_degraded_mode_without_key(monkeypatch):
    monkeypatch.delenv("DMIND_API_KEY", raising=False)
    monkeypatch.delenv("NANOGPT_API_KEY", raising=False)
    monkeypatch.delenv("NANO_GPT_API_KEY", raising=False)

    from src.nadobro.llm.dmind_service import analyze_financial_context

    result = analyze_financial_context("score BTC", context="BTC context")
    assert result["ok"] is False
    assert result["degraded"] is True


def test_nanogpt_json_extract_strips_fences():
    from src.nadobro.llm.nanogpt_client import extract_json_object

    raw = '```json\n{"a": 1}\n```'
    assert extract_json_object(raw) == {"a": 1}


def test_strategy_fsm_infers_failed_recoverable_state():
    from src.nadobro.strategy.strategy_fsm import PHASE_FAILED, infer_phase

    phase = infer_phase({"running": True, "last_error": "archive fill sync failed"})

    assert phase.phase == PHASE_FAILED
    assert phase.recoverable is True
    assert "recover" in phase.allowed_actions


def test_strategy_fsm_maps_volume_pending_fill_to_waiting_fill():
    from src.nadobro.strategy.strategy_fsm import PHASE_WAITING_FILL, infer_phase

    phase = infer_phase({"running": True, "vol_phase": "pending_fill"})

    assert phase.phase == PHASE_WAITING_FILL
    assert "recover" in phase.allowed_actions


def test_order_intent_suppresses_recent_duplicate(monkeypatch):
    store = {}

    def fake_get(key):
        return store.get(key)

    def fake_set(key, value):
        store[key] = value

    monkeypatch.setattr("src.nadobro.trading.order_intents.get_bot_state", fake_get)
    monkeypatch.setattr("src.nadobro.trading.order_intents.set_bot_state", fake_set)

    from src.nadobro.trading.order_intents import create_order_intent, should_skip_duplicate

    create_order_intent("abc", {"trade_id": 7, "status": "pending"})
    skip, existing = should_skip_duplicate("abc")

    assert skip is True
    assert existing["trade_id"] == 7
