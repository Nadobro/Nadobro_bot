"""Shared fakes for the Arcus P3b link-service tests (03 §19 intro).

``tests/`` is on ``sys.path`` (conftest), so test files ``import arcus_link_helpers``.

- :class:`FakeClient` scripts P2 read outcomes per endpoint (a list is served
  in order; the LAST entry repeats) and records every call.
- :class:`FakeDB` replaces the ``users.arcus_credentials`` functions the link
  service calls; every fake asserts it runs OFF the event-loop thread (the
  loop runs on the main thread under ``asyncio.run``).
- :func:`install` wires both into ``users.arcus_link_service``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from src.nadobro.users import arcus_credentials as creds
from src.nadobro.users import arcus_link_service as ls
from src.nadobro.venue.arcus.errors import (
    Forbidden,
    LocalDenied,
    NoActivity,
    NotFound,
    Ok,
    Throttled,
    Unavailable,
)
from src.nadobro.venue.arcus.parse import ApiKeyEntry, ComplianceView
from src.nadobro.venue.arcus.types import Lane

RFC_SEED = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
RFC_PUB = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
SEED_B = bytes(range(32)).hex()
PUB_B = "03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8"
WALLET_SEED = "ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"  # public Hardhat #0
WALLET_ADDR = "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"
WALLET_ED25519_PUB = "468995cdd3c27569e004c89098c44bf584b1a4c6cd89a203fc47825f48684964"
ADDR = "0x" + "ab" * 20
ADDR2 = "0x" + "cd" * 20
UID = 990_034_001
NOW_MS = 1_790_000_000_000
DAY_MS = 86_400_000
NOW_DT = datetime(2026, 9, 21, 14, 13, tzinfo=timezone.utc)


def compliance(status: str | None = "COMPLIANT", *, perps: bool = False, bypassed: bool = False, country: str = "XX") -> ComplianceView:
    return ComplianceView(
        country=country,
        restrictions_perps=perps,
        bypassed=bypassed,
        address_status=status,  # type: ignore[arg-type]
        reason=None,
    )


def entry(
    pub: str = RFC_PUB,
    *,
    address: str = ADDR,
    status: str = "ACTIVE",
    all_sub: bool = False,
    index: int | None = 0,
    name: str | None = "nadobro-ab12",
    until: int = 0,
    permissions: tuple[str, ...] = (),
) -> ApiKeyEntry:
    return ApiKeyEntry(
        api_key=pub,
        address=address,
        all_subaccounts=all_sub,
        account_index=None if all_sub else index,
        api_wallet_name=name,
        status=status,
        permissions=permissions,
        valid_until_ms=until,
        created_us=1_789_000_000_000_000,
    )


def ok(value: Any) -> Ok[Any]:
    return Ok(value=value, http_status=200, weight_charged=1)


THROTTLED = Throttled(layer="read_ip", retry_after_ms=1000, client_ids=())
UNAVAILABLE = Unavailable(http_status=500, message="server_error")
SCHEMA = Unavailable(http_status=200, message="schema")
DENIED = LocalDenied(reason="ip_budget")
WHITELIST = Forbidden(kind="whitelist", message="address not on access whitelist")
GEO = Forbidden(kind="geo", message="geo")
NOT_FOUND = NotFound(message="not found")
NO_ACTIVITY = NoActivity()


class FakeClient:
    def __init__(self, *, lane: Lane | None = Lane.L2_INTERACTIVE) -> None:
        self.compliance: list[Any] = [ok(compliance())]
        self.account: list[Any] = [ok(object())]
        self.api_keys: list[Any] = [ok([])]
        self.time: list[Any] = [ok(1_790_000_000_000_000_000)]
        self.calls: list[tuple[str, Any]] = []
        self.lane = lane
        self.hook: Any = None  # optional callable(name) run before each read

    @staticmethod
    def _pop(queue: list[Any]) -> Any:
        assert queue, "no scripted result left"
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def _check(self, name: str, lane: Lane, max_wait_s: float | None) -> None:
        if self.lane is not None:
            assert lane is self.lane, (name, lane)
        assert max_wait_s is not None and max_wait_s > 0
        if self.hook is not None:
            self.hook(name)

    async def get_compliance(self, address: str | None, *, lane: Lane, max_wait_s: float | None = None) -> Any:
        self.calls.append(("compliance", address))
        self._check("compliance", lane, max_wait_s)
        return self._pop(self.compliance)

    async def get_account(self, ref: Any, *, lane: Lane, max_wait_s: float | None = None) -> Any:
        self.calls.append(("account", ref))
        self._check("account", lane, max_wait_s)
        assert ref.account_index == 0
        return self._pop(self.account)

    async def get_api_keys(self, address: str, *, account_index: int | None, lane: Lane, max_wait_s: float | None = None) -> Any:
        self.calls.append(("apiKeys", address))
        assert account_index is None  # all subaccounts, deliberately (F5)
        self._check("apiKeys", lane, max_wait_s)
        return self._pop(self.api_keys)

    async def get_time(self, *, lane: Lane, max_wait_s: float | None = None) -> Any:
        self.calls.append(("time", None))
        return self._pop(self.time)

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)


class FakeClock:
    def __init__(self, skew: float | None = 5.0) -> None:
        self.skew = skew
        self.calls: list[dict[str, Any]] = []

    async def sync(self, client: Any, *, lane: Lane = Lane.L1_ENGINE, max_wait_s: float = 1.0) -> float | None:
        self.calls.append({"lane": lane, "max_wait_s": max_wait_s})
        return self.skew


class FakeMono:
    def __init__(self, start: float = 10_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class FakeSleep:
    def __init__(self, mono: FakeMono) -> None:
        self.mono = mono
        self.calls: list[float] = []
        self.hook: Any = None

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.mono.now += seconds
        if self.hook is not None:
            self.hook(len(self.calls))


def _off_loop() -> None:
    assert threading.current_thread() is not threading.main_thread(), "sync DB call on the event loop"


def row(
    *,
    uid: int = UID,
    network: str = "testnet",
    address: str = ADDR,
    pub: str = RFC_PUB,
    name: str | None = "nadobro-ab12",
    until: int | None = 0,
    status: str = "active",
    all_sub: bool = False,
) -> creds.ArcusCredentialRow:
    return creds.ArcusCredentialRow(
        user_id=uid,
        network=network,  # type: ignore[arg-type]
        address=address,
        account_index=0,
        all_subaccounts=all_sub,
        api_public_key=pub,
        api_wallet_name=name,
        valid_until_ms=until,
        status=status,  # type: ignore[arg-type]
        attested_at=NOW_DT,
        linked_at=NOW_DT,
        last_verified_at=NOW_DT,
    )


@dataclass
class FakeDB:
    credential: Any = None  # row returned by get_credential (or an Exception to raise)
    owner: Any = None  # user_id returned by owner_of (or an Exception)
    upsert_error: BaseException | None = None
    mark_result: bool = True
    touch_result: bool = True
    lifecycle: Any = field(default_factory=list)  # rows (or an Exception)
    notice_states: dict[tuple[int, str], Any] = field(default_factory=dict)
    notice_errors: set[tuple[int, str]] = field(default_factory=set)
    upserts: list[dict[str, Any]] = field(default_factory=list)
    marks: list[tuple[Any, ...]] = field(default_factory=list)
    touches: list[tuple[Any, ...]] = field(default_factory=list)
    saves: list[tuple[Any, ...]] = field(default_factory=list)
    audits: list[tuple[Any, ...]] = field(default_factory=list)
    all_args: list[Any] = field(default_factory=list)
    upsert_gate: Any = None  # optional threading.Event the upsert waits on

    def get_credential(self, user_id: int, network: str) -> Any:
        _off_loop()
        self.all_args.append(("get_credential", user_id, network))
        if isinstance(self.credential, BaseException):
            raise self.credential
        return self.credential

    def owner_of(self, network: str, address: str, index: int) -> Any:
        _off_loop()
        self.all_args.append(("owner_of", network, address, index))
        if isinstance(self.owner, BaseException):
            raise self.owner
        return self.owner

    def upsert_active_credential(self, **kwargs: Any) -> creds.ArcusCredentialRow:
        _off_loop()
        self.all_args.append(("upsert", kwargs))
        if self.upsert_gate is not None:
            assert self.upsert_gate.wait(10), "upsert gate never released"
        self.upserts.append(kwargs)
        if self.upsert_error is not None:
            raise self.upsert_error
        sealed = kwargs["sealed"]
        return row(
            uid=kwargs["user_id"],
            network=kwargs["network"],
            address=kwargs["address"],
            pub=sealed.api_public_key,
            name=kwargs["api_wallet_name"],
            until=kwargs["valid_until_ms"],
            all_sub=kwargs["all_subaccounts"],
        )

    def mark_status(self, *args: Any, **kwargs: Any) -> bool:
        _off_loop()
        self.all_args.append(("mark", args, kwargs))
        self.marks.append(args + (kwargs,))
        return self.mark_result

    def touch_verified(self, *args: Any, **kwargs: Any) -> bool:
        _off_loop()
        self.all_args.append(("touch", args, kwargs))
        self.touches.append(args + (kwargs,))
        return self.touch_result

    def list_lifecycle_credentials(self) -> list[Any]:
        _off_loop()
        if isinstance(self.lifecycle, BaseException):
            raise self.lifecycle
        return list(self.lifecycle)

    def get_key_notice_state(self, user_id: int, network: str) -> Any:
        _off_loop()
        if (user_id, network) in self.notice_errors:
            raise RuntimeError("db down for this row")
        return self.notice_states.get((user_id, network))

    def save_key_notice_state(self, user_id: int, network: str, state: dict[str, Any]) -> None:
        _off_loop()
        self.all_args.append(("save", user_id, network, state))
        self.saves.append((user_id, network, state))
        self.notice_states[(user_id, network)] = state

    def record_audit_event(self, user_id: Any, action: str, details: Any = None) -> None:
        _off_loop()
        self.all_args.append(("audit", user_id, action, details))
        self.audits.append((user_id, action, details))


def install(monkeypatch: Any, *, client: FakeClient | None = None, clock: FakeClock | None = None,
            db: FakeDB | None = None, mono: FakeMono | None = None) -> SimpleNamespace:
    """Wire the fakes into users.arcus_link_service (and reset its state)."""
    ls._reset_for_tests()
    client = client or FakeClient()
    clock = clock or FakeClock()
    db = db or FakeDB()
    mono = mono or FakeMono()
    sleep = FakeSleep(mono)
    svc = SimpleNamespace(client=client, clock=clock)
    monkeypatch.setattr(ls, "_services", lambda net: svc)
    monkeypatch.setattr(ls, "_sleep", sleep)
    monkeypatch.setattr(ls, "_now_ms", lambda: NOW_MS)
    monkeypatch.setattr(ls, "_mono", mono)
    for name in (
        "get_credential",
        "owner_of",
        "upsert_active_credential",
        "mark_status",
        "touch_verified",
        "list_lifecycle_credentials",
        "get_key_notice_state",
        "save_key_notice_state",
    ):
        monkeypatch.setattr(ls._creds, name, getattr(db, name))
    monkeypatch.setattr(ls.audit_log, "record_audit_event", db.record_audit_event)
    return SimpleNamespace(client=client, clock=clock, db=db, mono=mono, sleep=sleep, svc=svc)


def contains_secret(obj: Any, secrets: tuple[str, ...]) -> bool:
    """Recursive scan of call arguments / containers for any secret spelling."""
    spellings = set()
    for s in secrets:
        spellings |= {s, s.upper(), "0x" + s, "0X" + s}
    seen: set[int] = set()

    def walk(o: Any) -> bool:
        if id(o) in seen:
            return False
        seen.add(id(o))
        if isinstance(o, (bytes, bytearray)):
            try:
                o = bytes(o).decode("latin-1")
            except Exception:
                return False
        if isinstance(o, str):
            return any(sp in o for sp in spellings)
        if isinstance(o, dict):
            return any(walk(k) or walk(v) for k, v in o.items())
        if isinstance(o, (list, tuple, set, frozenset)):
            return any(walk(v) for v in o)
        if hasattr(o, "__dict__"):
            return walk(vars(o))
        if hasattr(o, "__slots__"):
            return any(walk(getattr(o, n, None)) for n in o.__slots__)
        return False

    return walk(obj)
