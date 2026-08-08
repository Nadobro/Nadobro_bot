"""Knowledge-hub seed corpus — src/nadobro/data/knowledge_sources.json.

The manifest is what the Phase-6 ingestion pipeline reads, and the tier on each
entry is a POLICY field, not a label: retail material is allowed in only as a
hypothesis source. A silent retier would let unverified prose reach the
recommendation path, so the discipline is pinned here rather than left to
review.
"""
import json
from pathlib import Path

import pytest

MANIFEST = Path("src/nadobro/data/knowledge_sources.json")

REQUIRED_FIELDS = {
    "id", "title", "url", "type", "tier", "domain", "ingestible", "refresh", "why",
}
KNOWN_TYPES = {"paper", "book", "article", "docs", "code", "feed", "video"}
KNOWN_DOMAINS = {
    "market_making", "microstructure", "execution", "perps", "grid_risk",
    "regime", "risk_sizing", "method", "structure",
}
KNOWN_REFRESH = {"never", "once", "daily", "weekly", "monthly", "quarterly"}


@pytest.fixture(scope="module")
def manifest():
    return json.loads(MANIFEST.read_text())


@pytest.fixture(scope="module")
def sources(manifest):
    return manifest["sources"]


def test_manifest_is_loadable_with_stdlib_only(manifest):
    # No YAML/feedparser in requirements.txt — the manifest must stay JSON so
    # the pipeline can read it without adding a dependency.
    assert manifest["version"] == 1
    assert manifest["sources"]


def test_every_source_is_complete(sources):
    for src in sources:
        missing = REQUIRED_FIELDS - set(src)
        assert not missing, f"{src.get('id')} missing {missing}"


def test_ids_are_unique(sources):
    ids = [s["id"] for s in sources]
    assert len(ids) == len(set(ids))


def test_enumerated_fields_are_known(manifest, sources):
    tiers = set(manifest["tiers"])
    for src in sources:
        assert src["tier"] in tiers, src["id"]
        assert src["type"] in KNOWN_TYPES, src["id"]
        assert src["domain"] in KNOWN_DOMAINS, src["id"]
        assert src["refresh"] in KNOWN_REFRESH, src["id"]


def test_ingestible_sources_have_fetchable_urls(sources):
    for src in sources:
        if src["ingestible"]:
            assert src["url"].startswith("https://"), src["id"]


def test_every_source_justifies_itself_against_this_bot(sources):
    # A curated corpus is only worth anything if each entry says why it applies
    # to a perps market maker on a thin book. A one-line 'why' is a red flag
    # that something was added because it sounded impressive.
    for src in sources:
        assert len(src["why"]) > 80, f"{src['id']} lacks a real justification"


def test_retail_material_stays_quarantined(sources):
    retail = [s for s in sources if s["tier"] == "retail_unverified"]
    assert retail, "the SMC source should still be present as a hypothesis source"
    for src in retail:
        assert "HYPOTHESIS SOURCE ONLY" in src["why"], src["id"]


def test_smc_video_is_not_promoted_above_retail(sources):
    smc = next(s for s in sources if s["id"] == "smc-full-course")
    assert smc["tier"] == "retail_unverified"


def test_policy_forbids_prose_setting_parameters(manifest):
    # The load-bearing rule of the whole hub design.
    assert "prose_cannot_set_parameters" in manifest["policy"]


def test_the_bots_own_failure_mode_is_covered(sources):
    # Nadobro ships three grid variants to real users and grids fail in trends.
    # A corpus that omits that is not curated for this product.
    assert any(s["domain"] == "grid_risk" for s in sources)


def test_core_domains_are_all_represented(sources):
    covered = {s["domain"] for s in sources}
    assert KNOWN_DOMAINS <= covered, f"uncovered: {KNOWN_DOMAINS - covered}"


def test_daily_feeds_exist_for_continuous_learning(sources):
    daily = [s for s in sources if s["refresh"] == "daily"]
    assert daily, "a hub that never refreshes cannot learn daily"
    for src in daily:
        assert src["ingestible"], f"{src['id']} refreshes daily but can't be fetched"
