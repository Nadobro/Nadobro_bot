"""config.py Arcus URL helpers (02 §3.2 / §12.11).

Read at call time through utils/env.py (inline ``# comments`` honoured); https
only, except a loopback fake; importing config reads no ARCUS_* variable.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

from src.nadobro import config
from src.nadobro.config import arcus_rest_url, arcus_ws_url

REPO = pathlib.Path(__file__).resolve().parents[1]
_URL_ENVS = (
    "ARCUS_TESTNET_REST_URL",
    "ARCUS_MAINNET_REST_URL",
    "ARCUS_TESTNET_WS_URL",
    "ARCUS_MAINNET_WS_URL",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _URL_ENVS:
        monkeypatch.delenv(name, raising=False)
    yield


def test_default_urls():
    assert arcus_rest_url("testnet") == "https://api.testnet.arcus.xyz"
    assert arcus_rest_url("mainnet") == "https://api.arcus.xyz"
    assert arcus_ws_url("testnet") == "wss://api.testnet.arcus.xyz/v1/ws"
    assert arcus_ws_url("mainnet") == "wss://api.arcus.xyz/v1/ws"
    assert config.ARCUS_TESTNET_REST_DEFAULT == "https://api.testnet.arcus.xyz"
    assert config.ARCUS_MAINNET_REST_DEFAULT == "https://api.arcus.xyz"


def test_env_override_with_inline_comment(monkeypatch):
    monkeypatch.setenv("ARCUS_TESTNET_REST_URL", "https://arcus-proxy.example/  # staging relay")
    monkeypatch.setenv("ARCUS_MAINNET_WS_URL", "wss://ws.example/v1/ws/ # note")
    assert arcus_rest_url("testnet") == "https://arcus-proxy.example"
    assert arcus_rest_url("mainnet") == "https://api.arcus.xyz"  # the other network is untouched
    assert arcus_ws_url("mainnet") == "wss://ws.example/v1/ws"
    monkeypatch.setenv("ARCUS_TESTNET_REST_URL", "   ")  # blank -> default
    assert arcus_rest_url("testnet") == "https://api.testnet.arcus.xyz"


@pytest.mark.parametrize(
    "url",
    [
        "http://evil.example",
        "http://127.0.0.1.evil.example",
        "http://localhost.evil.example:80",
        "ftp://api.arcus.xyz",
        "https://user:pw@api.arcus.xyz",
        "https://api.arcus.xyz/?token=x",
        "https://",
        "api.arcus.xyz",
        "wss://api.arcus.xyz",
        "https://api.arcus.xyz:99999",
    ],
)
def test_rest_url_must_be_https(monkeypatch, url):
    monkeypatch.setenv("ARCUS_MAINNET_REST_URL", url)
    with pytest.raises(ValueError) as exc:
        arcus_rest_url("mainnet")
    assert "user" not in str(exc.value) and "token" not in str(exc.value)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080", "http://localhost:9000", "https://arcus-relay.example"])
def test_rest_url_local_fake_allowed(monkeypatch, url):
    monkeypatch.setenv("ARCUS_TESTNET_REST_URL", url)
    assert arcus_rest_url("testnet") == url


@pytest.mark.parametrize(
    "env,net,resolve",
    [
        # the finding's typos: one env line points a scope at the other network
        ({"ARCUS_TESTNET_REST_URL": "https://api.arcus.xyz"}, "testnet", "rest"),
        ({"ARCUS_MAINNET_WS_URL": "wss://api.testnet.arcus.xyz/v1/ws  # oops"}, "mainnet", "ws"),
        ({"ARCUS_TESTNET_WS_URL": "wss://api.arcus.xyz/v1/ws"}, "testnet", "ws"),
        ({"ARCUS_MAINNET_REST_URL": "https://api.testnet.arcus.xyz"}, "mainnet", "rest"),
        # host spelling cannot dodge it: case, trailing dot, port, path
        ({"ARCUS_TESTNET_REST_URL": "https://API.Arcus.XYZ."}, "testnet", "rest"),
        ({"ARCUS_TESTNET_REST_URL": "https://api.arcus.xyz:8443/v1"}, "testnet", "rest"),
        # the other network's CONFIGURED host counts too (a relay set for both)
        ({"ARCUS_MAINNET_REST_URL": "https://relay.example", "ARCUS_TESTNET_REST_URL": "https://relay.example"},
         "testnet", "rest"),
        ({"ARCUS_MAINNET_WS_URL": "wss://relay.example/v1/ws", "ARCUS_TESTNET_REST_URL": "https://relay.example"},
         "testnet", "rest"),
        # ... and it fails both scopes (fail-closed on an ambiguous config)
        ({"ARCUS_MAINNET_REST_URL": "https://relay.example", "ARCUS_TESTNET_REST_URL": "https://relay.example"},
         "mainnet", "rest"),
        # the same loopback fake cannot be both networks
        ({"ARCUS_TESTNET_REST_URL": "http://127.0.0.1:8080", "ARCUS_MAINNET_REST_URL": "http://localhost:8080"},
         "testnet", "rest"),
        ({"ARCUS_TESTNET_REST_URL": "http://localhost", "ARCUS_MAINNET_WS_URL": "ws://127.0.0.1:80/v1/ws"},
         "testnet", "rest"),
    ],
)
def test_url_pointing_at_the_other_network_is_refused(monkeypatch, env, net, resolve):
    """R2-5: an override whose host is the OTHER network's (default or
    configured) REST/WS host is refused for every caller (client, hub, WS)."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    fn = arcus_rest_url if resolve == "rest" else arcus_ws_url
    with pytest.raises(ValueError) as exc:
        fn(net)
    assert "arcus.xyz" not in str(exc.value) and "relay" not in str(exc.value)  # the URL is never echoed


def test_distinct_hosts_per_network_are_allowed(monkeypatch):
    monkeypatch.setenv("ARCUS_TESTNET_REST_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv("ARCUS_MAINNET_REST_URL", "http://127.0.0.1:8081")  # two local fakes
    monkeypatch.setenv("ARCUS_TESTNET_WS_URL", "wss://testnet-relay.example/v1/ws")
    assert arcus_rest_url("testnet") == "http://127.0.0.1:8080"
    assert arcus_rest_url("mainnet") == "http://127.0.0.1:8081"
    assert arcus_ws_url("testnet") == "wss://testnet-relay.example/v1/ws"
    assert arcus_ws_url("mainnet") == "wss://api.arcus.xyz/v1/ws"
    assert config.arcus_url_conflicts("testnet", "https://api.arcus.xyz")
    assert config.arcus_url_conflicts("mainnet", "wss://testnet-relay.example/x")
    assert not config.arcus_url_conflicts("testnet", "https://api.testnet.arcus.xyz")
    assert not config.arcus_url_conflicts("testnet", "not a url")  # parse failures are the https check's job
    with pytest.raises(ValueError):
        config.arcus_url_conflicts("arcus_testnet", "https://api.arcus.xyz")


@pytest.mark.parametrize("url", ["https://api.arcus.xyz/v1/ws", "ws://evil.example/v1/ws", "ws://127.0.0.1.x/v1/ws"])
def test_ws_url_must_be_wss(monkeypatch, url):
    monkeypatch.setenv("ARCUS_TESTNET_WS_URL", url)
    with pytest.raises(ValueError):
        arcus_ws_url("testnet")


def test_ws_url_local_fake_allowed(monkeypatch):
    monkeypatch.setenv("ARCUS_TESTNET_WS_URL", "ws://127.0.0.1:8765/v1/ws")
    assert arcus_ws_url("testnet") == "ws://127.0.0.1:8765/v1/ws"


@pytest.mark.parametrize("net", ["arcus_testnet", "Testnet", " mainnet", "", None, "nado"])
def test_urls_reject_non_network_tokens(net):
    with pytest.raises(ValueError):
        arcus_rest_url(net)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        arcus_ws_url(net)  # type: ignore[arg-type]


_RECORDER = r"""
import json, os
class Rec(dict):
    seen = []
    def get(self, k, d=None):
        Rec.seen.append(k)
        return super().get(k, d)
    def __getitem__(self, k):
        Rec.seen.append(k)
        return super().__getitem__(k)
    def __contains__(self, k):
        Rec.seen.append(k)
        return super().__contains__(k)
os.environ = Rec(os.environ)
import src.nadobro.config as config
at_import = sorted({k for k in Rec.seen if isinstance(k, str) and k.startswith("ARCUS")})
config.arcus_rest_url("testnet")
after_call = sorted({k for k in Rec.seen if isinstance(k, str) and k.startswith("ARCUS")})
print(json.dumps({"at_import": at_import, "after_call": after_call}))
"""


def test_config_import_side_effect_free():
    proc = subprocess.run(
        [sys.executable, "-c", _RECORDER], cwd=REPO, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    seen = json.loads(proc.stdout.strip().splitlines()[-1])
    assert seen["at_import"] == []
    # positive control: the recorder does see the call-time reads (this network's
    # URL, plus the other network's REST/WS overrides for the cross-network check)
    assert seen["after_call"] == ["ARCUS_MAINNET_REST_URL", "ARCUS_MAINNET_WS_URL", "ARCUS_TESTNET_REST_URL"]
