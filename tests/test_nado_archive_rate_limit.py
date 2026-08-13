import time
from unittest.mock import MagicMock, patch

from _stubs import install_test_stubs

install_test_stubs()


def _reset_archive_rate_state():
    import src.nadobro.venue.nado_archive as archive

    archive._rate_limited_until = 0.0
    archive._last_request_at = 0.0
    archive._last_429_log_at = 0.0


def test_post_sets_global_cooldown_on_429():
    import src.nadobro.venue.nado_archive as archive

    _reset_archive_rate_state()
    response = MagicMock()
    response.status_code = 429
    response.text = '{"error":"Too Many Requests","error_code":1000}'
    response.raise_for_status.side_effect = archive.requests.HTTPError(response=response)

    session = MagicMock()
    session.post.return_value = response

    with patch.object(archive, "_get_session", return_value=session):
        result = archive._post("https://archive.example", {"orders": {"digests": ["0x1"]}})

    assert result is None
    assert archive.is_archive_rate_limited()
    assert archive.archive_rate_limit_remaining() > 0.0


def test_post_skips_requests_while_rate_limited():
    import src.nadobro.venue.nado_archive as archive

    _reset_archive_rate_state()
    archive._rate_limited_until = time.time() + 30.0
    session = MagicMock()

    with patch.object(archive, "_get_session", return_value=session):
        result = archive._post("https://archive.example", {"orders": {"digests": ["0x1"]}})

    assert result is None
    session.post.assert_not_called()


def test_query_orders_by_digests_parses_batch_response():
    import src.nadobro.venue.nado_archive as archive

    payload = {
        "orders": [
            {"digest": "0xabc", "base_filled": 1e18, "quote_filled": 100e18},
            {"digest": "0xdef", "base_filled": 0, "quote_filled": 0},
        ]
    }

    with patch.object(archive, "_post", return_value=payload):
        out = archive.query_orders_by_digests("mainnet", ["0xabc", "0xdef", "0xabc"])

    assert set(out.keys()) == {"0xabc", "0xdef"}
    assert out["0xabc"]["is_filled"] is True
    assert out["0xdef"]["is_filled"] is False


def test_query_order_by_digest_stops_polling_when_rate_limited():
    import src.nadobro.venue.nado_archive as archive

    _reset_archive_rate_state()
    archive._rate_limited_until = time.time() + 30.0

    with patch.object(archive, "_post") as mock_post:
        result = archive.query_order_by_digest("mainnet", "0xabc", max_wait_seconds=5.0, poll_interval=0.1)

    assert result is None
    mock_post.assert_not_called()


def test_pair_volume_cache_only_does_not_post():
    import src.nadobro.venue.nado_archive as archive

    archive._VOLUME_CACHE.clear()
    with patch.object(archive, "_post") as mock_post:
        assert archive.get_pair_24h_volume_usd("mainnet", 2, cache_only=True) is None
        mock_post.assert_not_called()
        archive._VOLUME_CACHE[("mainnet", 2)] = (0.0, 99.5)
        assert archive.get_pair_24h_volume_usd("mainnet", 2, cache_only=True) == 99.5
        mock_post.assert_not_called()
