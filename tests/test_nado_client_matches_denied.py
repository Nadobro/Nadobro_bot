"""NadoClient.get_matches DENIED-vs-EMPTY contract (AUDIT-DENY-2026-09-02-F3).

``[]`` = a successful read with no matches. ``None`` = the read could not be
performed (no SDK client, archive budget denied, SDK raised) = fills UNKNOWN.
Returning ``[]`` for the unreadable cases booked gone orders as CANCELLED(0).
"""
from __future__ import annotations

import asyncio
from unittest import mock

from src.nadobro.venue.nado_client import NadoClient


def _client() -> NadoClient:
    return NadoClient(private_key="0xabc", network="mainnet")


def test_no_sdk_client_reports_unknown_not_empty():
    c = _client()                                  # uninitialized: no SDK client
    assert asyncio.run(c.get_matches(limit=5)) is None


def test_archive_budget_denied_reports_unknown_not_empty():
    c = _client()
    with mock.patch.object(c, "_ensure_sdk_client", return_value=True), \
         mock.patch.object(c, "_gateway_allowed", return_value=False):
        assert asyncio.run(c.get_matches(limit=5)) is None
