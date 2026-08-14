"""Tests for the egress-IP geolocation guard.

Nado geo-blocks writes from restricted territories; Fly's egress geolocation is
independent of the region, so the bot must self-verify. These mock the HTTP
calls — no network.
"""
import logging

from _stubs import install_test_stubs

install_test_stubs()


def _reset_module(monkeypatch, env=None):
    from src.nadobro.core import egress_geo
    for k in ("NADO_RESTRICTED_COUNTRIES", "NADO_EGRESS_IPV4_ECHO_URL",
              "NADO_EGRESS_IPV6_ECHO_URL", "NADO_EGRESS_GEO_URL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    egress_geo._last_report = None
    return egress_geo


def test_default_restricted_set_matches_nado_terms(monkeypatch):
    egress_geo = _reset_module(monkeypatch)
    restricted = egress_geo.restricted_countries()
    # Whole-country entries from Nado's Terms of Use.
    assert restricted == {"US", "CA", "PA", "BY", "CU", "IR", "KP", "RU", "SY"}
    # Ukraine is region-restricted only — a country-code match would false-flag Kyiv.
    assert "UA" not in restricted


def test_restricted_set_is_env_overridable(monkeypatch):
    egress_geo = _reset_module(monkeypatch, {"NADO_RESTRICTED_COUNTRIES": "us, ru , kp"})
    assert egress_geo.restricted_countries() == {"US", "RU", "KP"}


def _stub_probes(monkeypatch, egress_geo, ipv4=None, ipv6=None):
    """ipv4/ipv6 = (ip, country) or None to simulate an unavailable family."""
    def fake_echo(url):
        if "api6" in url or "ipv6" in url:
            if ipv6 is None:
                raise OSError("no v6")
            return ipv6[0]
        if ipv4 is None:
            raise OSError("no v4")
        return ipv4[0]

    def fake_geo(ip, geo_url):
        for pair in (ipv4, ipv6):
            if pair and pair[0] == ip:
                return {"ip": ip, "country": pair[1], "org": "AS0 Test"}
        return {}

    monkeypatch.setattr(egress_geo, "_echo_ip", fake_echo)
    monkeypatch.setattr(egress_geo, "_geolocate", fake_geo)


def test_clean_egress_is_not_flagged(monkeypatch, caplog):
    egress_geo = _reset_module(monkeypatch)
    _stub_probes(monkeypatch, egress_geo, ipv4=("1.2.3.4", "NL"), ipv6=("2a09::1", "NL"))
    with caplog.at_level(logging.INFO, logger="src.nadobro.core.egress_geo"):
        report = egress_geo.evaluate_and_log(egress_geo.probe_egress())
    assert report.verified
    assert not report.any_restricted
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


def test_us_egress_is_flagged_as_error(monkeypatch, caplog):
    egress_geo = _reset_module(monkeypatch)
    # Fly's pool skews US: a fra machine can egress via a Colorado IP.
    _stub_probes(monkeypatch, egress_geo, ipv4=("104.28.0.1", "US"), ipv6=None)
    with caplog.at_level(logging.ERROR, logger="src.nadobro.core.egress_geo"):
        report = egress_geo.evaluate_and_log(egress_geo.probe_egress())
    assert report.any_restricted
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert "GEO-BLOCK RISK" in errors[0].getMessage()


def test_ipv6_restricted_even_when_ipv4_clean(monkeypatch):
    """Nado may see either family on a dual-stack connect — either restricted fails."""
    egress_geo = _reset_module(monkeypatch)
    _stub_probes(monkeypatch, egress_geo, ipv4=("1.2.3.4", "NL"), ipv6=("2a09::1", "CA"))
    report = egress_geo.evaluate_and_log(egress_geo.probe_egress())
    assert report.any_restricted


def test_unverified_when_geo_service_unreachable(monkeypatch, caplog):
    egress_geo = _reset_module(monkeypatch)
    _stub_probes(monkeypatch, egress_geo, ipv4=None, ipv6=None)
    with caplog.at_level(logging.WARNING, logger="src.nadobro.core.egress_geo"):
        report = egress_geo.evaluate_and_log(egress_geo.probe_egress())
    assert not report.verified
    assert not report.any_restricted  # unknown != restricted; do not false-alarm
    assert any("UNVERIFIED" in r.getMessage() for r in caplog.records)


def test_status_line_reflects_last_report(monkeypatch):
    egress_geo = _reset_module(monkeypatch)
    assert "not checked" in egress_geo.status_line()
    _stub_probes(monkeypatch, egress_geo, ipv4=("1.2.3.4", "NL"), ipv6=None)
    egress_geo.evaluate_and_log(egress_geo.probe_egress())
    line = egress_geo.status_line()
    assert "ok" in line and "NL" in line


def test_probe_never_raises_on_network_error(monkeypatch):
    egress_geo = _reset_module(monkeypatch)

    def boom(url):
        raise ConnectionError("down")

    monkeypatch.setattr(egress_geo, "_echo_ip", boom)
    report = egress_geo.probe_egress()  # must not raise
    assert all(p.country is None for p in report.probes)
