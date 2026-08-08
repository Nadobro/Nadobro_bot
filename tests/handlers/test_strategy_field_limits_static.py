"""Static guard: every whitelisted strategy field has a limits entry.

AUDIT-MM-2026-07-14 #1: ``mm_cross_after_seconds`` was added to
``allowed_numeric_fields`` without a ``limits`` entry — every Cross button
then crashed on ``limits[field]``. The handler now rejects such fields
defensively, but a silent reject is still a dead button, so this test pins
the invariant at the source level: the whitelist must be a subset of the
limits keys (and of the custom-input help so ✍️ paths stay reachable).
"""
import ast
import pathlib

SRC = pathlib.Path("src/nadobro/handlers/strategy_handler.py").read_text()
TREE = ast.parse(SRC)


def _string_elts(node) -> set[str]:
    return {e.value for e in getattr(node, "elts", []) if isinstance(e, ast.Constant)}


def _collect():
    allowed: set[str] = set()
    limit_keys: set[str] = set()
    input_allowed: set[str] = set()
    help_keys: set[str] = set()
    for node in ast.walk(TREE):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            name = getattr(target, "id", "")
            if name == "allowed_numeric_fields" and isinstance(node.value, ast.Set):
                allowed = _string_elts(node.value)
            elif name == "limits" and isinstance(node.value, ast.Dict):
                limit_keys = {
                    k.value for k in node.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                }
            elif name == "allowed_inputs" and isinstance(node.value, ast.Tuple):
                input_allowed = _string_elts(node.value)
            elif name == "help_text" and isinstance(node.value, ast.Dict):
                help_keys = {
                    k.value for k in node.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                }
    return allowed, limit_keys, input_allowed, help_keys


def test_every_whitelisted_numeric_field_has_a_limits_entry():
    allowed, limit_keys, _, _ = _collect()
    assert allowed, "failed to locate allowed_numeric_fields in source"
    assert limit_keys, "failed to locate limits dict in source"
    missing = allowed - limit_keys
    assert not missing, (
        f"whitelisted fields with no limits entry (buttons would be dead): {sorted(missing)}"
    )


def test_every_custom_input_field_is_numeric_whitelisted():
    allowed, _, input_allowed, _ = _collect()
    assert input_allowed, "failed to locate allowed_inputs in source"
    # A ✍️ input for a field the set-path rejects can never be saved.
    orphans = input_allowed - allowed
    assert not orphans, f"custom-input fields missing from allowed_numeric_fields: {sorted(orphans)}"



# ── VOL-DIRECTION-TAB (reported 2026-07-28, resolved round 3) ───
# The Direction tab originally rendered the TP/SL keyboard (it ignored `section`).
# Wiring it up exposed the deeper problem: vol is spot-only and bot_runtime
# FORCE-SETS vol_direction="long" for a spot market, so a SHORT the user picked
# could never be honoured — the card would have claimed SHORT while every run
# bought spot. The tab is removed until a vol market exists that can run short.

def _vol_kb(section):
    from src.nadobro.handlers.strategy_handler import _strategy_config_section_kb
    return _strategy_config_section_kb("vol", section)


def _callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_vol_offers_no_direction_tab():
    from src.nadobro.handlers.strategy_handler import _strategy_config_sections
    keys = [k for k, _label in _strategy_config_sections("vol")]
    assert "direction" not in keys, (
        "the Direction tab is back, but bot_runtime still forces spot vol to long "
        "— the card would claim a side the engine ignores"
    )
    assert "risk" in keys


def test_vol_exposes_no_direction_control_anywhere():
    from src.nadobro.handlers.strategy_handler import _strategy_config_sections
    for section, _label in _strategy_config_sections("vol"):
        for cb in _callbacks(_vol_kb(section)):
            assert "vol_direction" not in cb, cb


def test_vol_default_section_is_reachable_and_has_the_real_controls():
    from src.nadobro.handlers.strategy_handler import (
        _strategy_config_default_section, _strategy_config_sections)
    default = _strategy_config_default_section("vol")
    assert default in [k for k, _l in _strategy_config_sections("vol")], (
        "vol opens on a tab that is not in its own section list"
    )
    cbs = " ".join(_callbacks(_vol_kb(default)))
    for field in ("tp_pct", "sl_pct", "session_margin_usd", "target_volume_usd"):
        assert field in cbs, f"{field} unreachable from the default vol tab"


def test_every_vol_field_routes_to_an_existing_section():
    from src.nadobro.handlers.strategy_handler import (
        _strategy_section_for_field, _strategy_config_sections)
    keys = [k for k, _l in _strategy_config_sections("vol")]
    for field in ("tp_pct", "sl_pct", "session_margin_usd", "target_volume_usd",
                  "vol_direction"):
        assert _strategy_section_for_field("vol", field) in keys, field


# ── SPOT-EXIT-GUARANTEE on the UI ───────────────────────────────
# engine_runtime raises a DN/Vol spot leg to min_notional x 1.15 so its EXIT can
# still clear the venue floor (kBTC $100 vs a ~$99 leg, prod 2026-07-28). The
# cards must not keep claiming the smaller number the user typed — that is real
# capital they did not ask for.

_KBTC_ROW = {"id": 1, "base": "KBTC", "symbol": "KBTC", "underlying_key": "BTC",
             "min_size_x18": "100000000000000000000"}     # $100, the live value


def _warm_spot_cache(monkeypatch):
    from src.nadobro.venue import product_catalog as pc
    monkeypatch.setitem(pc._spot_catalog_cache, "mainnet",
                        {"ts": 9e18, "data": {"spots": {"KBTC": _KBTC_ROW}}})


def test_leg_size_shows_the_effective_size_when_the_floor_raises_it(monkeypatch):
    _warm_spot_cache(monkeypatch)
    from src.nadobro.handlers.strategy_handler import _leg_size_display
    out = _leg_size_display(100.0, "BTC", "mainnet")
    assert "$100" in out and "$115" in out, out
    assert "floor" in out


def test_leg_size_is_plain_when_the_user_is_already_above_the_floor(monkeypatch):
    _warm_spot_cache(monkeypatch)
    from src.nadobro.handlers.strategy_handler import _leg_size_display
    assert _leg_size_display(500.0, "BTC", "mainnet") == "$500"


def test_leg_size_never_blocks_the_tap_on_a_cache_miss(monkeypatch):
    """Click-path rule: serve cached or nothing. A cold catalog must NOT fetch."""
    from src.nadobro.venue import product_catalog as pc
    from src.nadobro.handlers.strategy_handler import _leg_size_display
    monkeypatch.setattr(pc, "_spot_catalog_cache", {})

    def _boom(*a, **k):
        raise AssertionError("the card fetched the catalog — this hangs the tap")

    monkeypatch.setattr(pc, "_build_dynamic_spot_catalog", _boom)
    monkeypatch.setattr(pc, "get_spot_catalog", _boom)
    assert _leg_size_display(100.0, "BTC", "mainnet") == "$100"


def test_leg_size_is_plain_without_a_product(monkeypatch):
    _warm_spot_cache(monkeypatch)
    from src.nadobro.handlers.strategy_handler import _leg_size_display
    assert _leg_size_display(100.0, "", "mainnet") == "$100"
    assert _leg_size_display(100.0, None, "mainnet") == "$100"


def test_leg_size_survives_a_broken_catalog_row(monkeypatch):
    from src.nadobro.venue import product_catalog as pc
    from src.nadobro.handlers.strategy_handler import _leg_size_display
    monkeypatch.setitem(pc._spot_catalog_cache, "mainnet",
                        {"ts": 9e18, "data": {"spots": {"KBTC": {
                            "base": "KBTC", "min_size_x18": "not-a-number"}}}})
    assert _leg_size_display(100.0, "BTC", "mainnet") == "$100"


def test_the_dn_core_card_shows_the_effective_leg(monkeypatch):
    _warm_spot_cache(monkeypatch)
    from src.nadobro.handlers.strategy_handler import _strategy_config_section_text
    line = [l for l in _strategy_config_section_text(
        "dn", {"product": "BTC", "fixed_margin_usd": 100.0}, "mainnet", "setup"
    ).split("\n") if "per leg" in l][0]
    assert "$115" in line, line


def test_the_vol_card_shows_the_effective_margin(monkeypatch):
    _warm_spot_cache(monkeypatch)
    from src.nadobro.handlers.strategy_handler import _strategy_config_section_text
    line = [l for l in _strategy_config_section_text(
        "vol", {"product": "BTC", "session_margin_usd": 100.0}, "mainnet", "risk"
    ).split("\n") if "Margin per cycle" in l][0]
    assert "$115" in line, line


def test_the_effective_size_string_is_markdownv2_safe(monkeypatch):
    """Unescaped ( ) or . in MarkdownV2 makes Telegram reject the whole message."""
    _warm_spot_cache(monkeypatch)
    from src.nadobro.handlers.strategy_handler import _leg_size_display
    out = _leg_size_display(100.0, "BTC", "mainnet")
    # every literal paren must be backslash-escaped
    for i, ch in enumerate(out):
        if ch in "()":
            assert i > 0 and out[i - 1] == "\\", f"unescaped {ch!r} in {out!r}"


def test_conf_for_card_supplies_the_selected_product(monkeypatch):
    """The disclosure was dead because settings["strategies"][sid] has no
    "product" key — the selected asset lives in context.user_data."""
    from types import SimpleNamespace
    from src.nadobro.handlers.strategy_handler import _conf_for_card
    ctx = SimpleNamespace(user_data={"strategy_pair:dn": "btc"})
    conf = _conf_for_card({"strategies": {"dn": {"fixed_margin_usd": 100.0}}}, "dn", ctx)
    assert conf["product"] == "BTC"
    assert conf["fixed_margin_usd"] == 100.0


def test_conf_for_card_never_mutates_stored_settings():
    from types import SimpleNamespace
    from src.nadobro.handlers.strategy_handler import _conf_for_card
    settings = {"strategies": {"dn": {"fixed_margin_usd": 100.0}}}
    ctx = SimpleNamespace(user_data={"strategy_pair:dn": "ETH"})
    _conf_for_card(settings, "dn", ctx)
    assert "product" not in settings["strategies"]["dn"], "leaked into stored settings"


def test_conf_for_card_tolerates_a_missing_selection_and_a_bad_context():
    from types import SimpleNamespace
    from src.nadobro.handlers.strategy_handler import _conf_for_card
    assert "product" not in _conf_for_card({"strategies": {"dn": {}}}, "dn",
                                           SimpleNamespace(user_data={}))
    # A bad context still degrades to "no selection" rather than raising. The only
    # key the overlay adds unconditionally is the effective leverage (CARD-LEV-
    # DIVERGE) — display-only, and never a product it cannot know.
    bad_ctx = _conf_for_card({"strategies": {"dn": {}}}, "dn", object())
    assert "product" not in bad_ctx
    assert set(bad_ctx) == {"leverage"}


def test_the_dn_core_card_disclosure_is_LIVE_end_to_end(monkeypatch):
    """The whole point: a $100 leg on kBTC must render as raised to $115."""
    from types import SimpleNamespace
    from src.nadobro.venue import product_catalog as pc
    from src.nadobro.handlers.strategy_handler import (
        _conf_for_card, _strategy_config_section_text)
    monkeypatch.setitem(pc._spot_catalog_cache, "mainnet",
                        {"ts": 9e18, "data": {"spots": {"KBTC": _KBTC_ROW}}})
    ctx = SimpleNamespace(user_data={"strategy_pair:dn": "BTC"})
    conf = _conf_for_card({"strategies": {"dn": {"fixed_margin_usd": 100.0}}}, "dn", ctx)
    line = [l for l in _strategy_config_section_text("dn", conf, "mainnet", "setup").split("\n")
            if "per leg" in l][0]
    assert "$115" in line, f"the disclosure is still dead: {line}"


def test_every_card_conf_site_uses_the_helper():
    """Guard against a new card being wired to the bare settings dict again.

    Exactly TWO sites may read it directly — _append_mm_pretrade_breakdown and
    _build_strategy_preview_text — because neither takes `context`; the preview
    already passes a real symbol (dn_spot_symbol) of its own. Every card rendered
    from _handle_strategy must go through _conf_for_card or its disclosure is dead.
    """
    import pathlib
    src = pathlib.Path("src/nadobro/handlers/strategy_handler.py").read_text()
    bare = src.count('conf = settings.get("strategies", {}).get(strategy_id, {})')
    assert bare <= 2, (
        f"{bare} sites read the stored config directly (expected <= 2). A card "
        f"wired that way does not carry the selected product, so its "
        f"effective-size disclosure silently renders the pre-floor number."
    )
    # Prefix match, no closing paren: the helper grew a `network` argument for the
    # effective-leverage overlay and a literal-with-paren guard broke on it. Match
    # the call, not its arity.
    assert src.count("_conf_for_card(settings, strategy_id, context") >= 7
