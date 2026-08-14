import json
import logging
import os
import re
import shutil
import subprocess
import time
from typing import Any

from src.nadobro.trading.execution_queue import get_queue_diagnostics
from src.nadobro.core.perf import snapshot as perf_snapshot
from src.nadobro.utils.env import env_bool, env_float
from src.nadobro.config import (
    get_dn_products,
    get_perp_products,
    get_product_id,
    list_volume_spot_product_names,
)
from src.nadobro.users.user_service import (
    get_runtime_wallet_readiness,
    get_user,
    get_user_readonly_client,
)

logger = logging.getLogger(__name__)

_CLI_TIMEOUT_SECONDS = env_float("NADO_TOOLING_CLI_TIMEOUT_SECONDS", 6.0)


def tooling_enabled() -> bool:
    return env_bool("NADO_TOOLING_ENABLE", True)


def twap_preview_enabled() -> bool:
    return env_bool("NADO_TOOLING_ENABLE_TWAP_PREVIEW", True)


def trigger_preview_enabled() -> bool:
    return env_bool("NADO_TOOLING_ENABLE_TRIGGER_PREVIEW", True)


def _network_to_data_env(network: str) -> str:
    return "nadoMainnet" if str(network or "").lower() == "mainnet" else "nadoTestnet"


def _catalog_symbols_for_network(network: str, client) -> set[str]:
    out: set[str] = set()
    for batch in (
        get_perp_products(network=network, client=client) or [],
        get_dn_products(network=network, client=client) or [],
        list_volume_spot_product_names(network=network, client=client) or [],
    ):
        for name in batch:
            n = str(name).upper().strip()
            if n:
                out.add(n)
    return out


def _cli_env_for_user(telegram_id: int) -> dict[str, str] | None:
    user = get_user(telegram_id)
    if not user:
        return None
    env = os.environ.copy()
    env["DATA_ENV"] = _network_to_data_env(user.network_mode.value)
    env["SUBACCOUNT_NAME"] = "default"
    if user.main_address:
        env["SUBACCOUNT_OWNER"] = str(user.main_address)
    return env


def _nado_cli_available() -> bool:
    return bool(shutil.which("nado") or shutil.which("npx") or shutil.which("bunx"))


def _run_cli_json(args: list[str], env: dict[str, str] | None = None) -> dict[str, Any]:
    if not _nado_cli_available():
        return {"success": False, "source": "cli", "error": "nado-cli unavailable"}
    command: list[str]
    if shutil.which("nado"):
        command = ["nado"] + args + ["--format", "json"]
    elif shutil.which("npx"):
        command = ["npx", "@nadohq/nado-cli"] + args + ["--format", "json"]
    else:
        command = ["bunx", "@nadohq/nado-cli"] + args + ["--format", "json"]

    try:
        proc = subprocess.run(
            command,
            env=env,
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception as e:
        return {"success": False, "source": "cli", "error": f"cli invocation failed: {e}"}

    stdout = (proc.stdout or "").strip()
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        return {
            "success": False,
            "source": "cli",
            "error": stderr or f"cli exited with code {proc.returncode}",
        }
    if not stdout:
        return {"success": False, "source": "cli", "error": "cli returned empty output"}
    try:
        payload = json.loads(stdout)
    except Exception:
        return {"success": False, "source": "cli", "error": "cli returned non-json output"}
    return {"success": True, "source": "cli", "data": payload}


def get_account_snapshot(telegram_id: int, prefer_cli: bool = False) -> dict[str, Any]:
    if not tooling_enabled():
        return {"success": False, "error": "tooling disabled by environment"}

    env = _cli_env_for_user(telegram_id)
    if prefer_cli and env:
        via_cli = _run_cli_json(["account", "summary"], env=env)
        if via_cli.get("success"):
            return via_cli

    client = get_user_readonly_client(telegram_id)
    if not client:
        return {"success": False, "source": "sdk", "error": "readonly client unavailable"}
    try:
        balance = client.get_balance() or {}
        positions = client.get_all_positions() or []
        open_orders = client.get_all_open_orders() or []
        return {
            "success": True,
            "source": "sdk",
            "data": {
                "balance": balance,
                "positions": positions,
                "open_orders": open_orders,
            },
        }
    except Exception as e:
        return {"success": False, "source": "sdk", "error": str(e)}


def get_market_snapshot(telegram_id: int, product: str, prefer_cli: bool = False) -> dict[str, Any]:
    if not tooling_enabled():
        return {"success": False, "error": "tooling disabled by environment"}
    symbol = str(product or "").upper().replace("-PERP", "").strip()
    if not symbol:
        return {"success": False, "error": "product is required"}

    client = get_user_readonly_client(telegram_id)
    if not client:
        return {"success": False, "source": "sdk", "error": "readonly client unavailable"}

    allowed = _catalog_symbols_for_network(client.network, client)
    if symbol not in allowed:
        return {"success": False, "error": f"unknown product {symbol}"}

    env = _cli_env_for_user(telegram_id)
    if prefer_cli and env:
        via_cli = _run_cli_json(["market", "price", symbol], env=env)
        if via_cli.get("success"):
            return via_cli

    pid = get_product_id(symbol, network=client.network, client=client)
    if pid is None:
        return {"success": False, "source": "sdk", "error": f"unknown product {symbol}"}
    price = client.get_market_price(pid) or {}
    return {"success": True, "source": "sdk", "data": {"product": symbol, "product_id": pid, "price": price}}


def preview_twap_plan(
    telegram_id: int,
    product: str,
    side: str,
    quantity: float,
    duration_minutes: int,
    interval_seconds: int = 30,
) -> dict[str, Any]:
    if not twap_preview_enabled():
        return {"success": False, "error": "TWAP preview disabled"}
    qty = float(quantity or 0)
    mins = int(duration_minutes or 0)
    interval = max(5, int(interval_seconds or 30))
    if qty <= 0:
        return {"success": False, "error": "quantity must be positive"}
    if mins <= 0:
        return {"success": False, "error": "duration must be positive"}

    mk = get_market_snapshot(telegram_id, product)
    if not mk.get("success"):
        return mk
    price = float(((mk.get("data") or {}).get("price") or {}).get("mid") or 0.0)
    if price <= 0:
        return {"success": False, "error": "could not fetch market mid price"}
    slices = max(1, int((mins * 60) // interval))
    slice_qty = qty / slices
    est_notional = qty * price
    return {
        "success": True,
        "source": mk.get("source"),
        "data": {
            "product": str(product).upper().replace("-PERP", ""),
            "side": str(side or "buy").lower(),
            "quantity": qty,
            "duration_minutes": mins,
            "interval_seconds": interval,
            "estimated_slices": slices,
            "estimated_slice_qty": slice_qty,
            "estimated_notional_usd": est_notional,
            "reference_mid_price": price,
            "summary": (
                f"TWAP preview: {slices} slices every {interval}s, "
                f"~{slice_qty:.6f} per slice, notional ~${est_notional:,.2f}"
            ),
        },
    }


def preview_trigger_plan(
    telegram_id: int,
    product: str,
    side: str,
    trigger_price: float,
    quantity: float,
) -> dict[str, Any]:
    if not trigger_preview_enabled():
        return {"success": False, "error": "trigger preview disabled"}
    qty = float(quantity or 0)
    trg = float(trigger_price or 0)
    if qty <= 0:
        return {"success": False, "error": "quantity must be positive"}
    if trg <= 0:
        return {"success": False, "error": "trigger price must be positive"}
    mk = get_market_snapshot(telegram_id, product)
    if not mk.get("success"):
        return mk
    mid = float(((mk.get("data") or {}).get("price") or {}).get("mid") or 0.0)
    distance_pct = ((trg - mid) / mid * 100.0) if mid > 0 else 0.0
    est_notional = qty * (mid or trg)
    direction = str(side or "buy").lower()
    return {
        "success": True,
        "source": mk.get("source"),
        "data": {
            "product": str(product).upper().replace("-PERP", ""),
            "side": direction,
            "quantity": qty,
            "trigger_price": trg,
            "reference_mid_price": mid,
            "distance_pct_from_mid": distance_pct,
            "estimated_notional_usd": est_notional,
            "summary": (
                f"Trigger preview: {direction} {qty:.6f} when price hits ${trg:,.2f} "
                f"({distance_pct:+.2f}% vs mid), est notional ~${est_notional:,.2f}"
            ),
        },
    }


def parse_twap_intent(text: str) -> dict[str, Any] | None:
    # Example: "twap buy btc 0.5 over 10m"
    m = re.search(
        r"\btwap\s+(buy|sell|long|short)\s+([A-Za-z]{2,10})\s+([0-9]*\.?[0-9]+)\s+(?:over|for)\s+([0-9]+)\s*(m|min|mins|minute|minutes)\b",
        str(text or ""),
        flags=re.IGNORECASE,
    )
    if not m:
        return None
    return {
        "side": m.group(1).lower(),
        "product": m.group(2).upper(),
        "quantity": float(m.group(3)),
        "duration_minutes": int(m.group(4)),
    }


def parse_trigger_intent(text: str) -> dict[str, Any] | None:
    # Example: "trigger buy btc 0.1 at 65000"
    m = re.search(
        r"\btrigger\s+(buy|sell|long|short)\s+([A-Za-z]{2,10})\s+([0-9]*\.?[0-9]+)\s+(?:at|when)\s+([0-9]*\.?[0-9]+)\b",
        str(text or ""),
        flags=re.IGNORECASE,
    )
    if not m:
        return None
    return {
        "side": m.group(1).lower(),
        "product": m.group(2).upper(),
        "quantity": float(m.group(3)),
        "trigger_price": float(m.group(4)),
    }


def get_ops_diagnostics(telegram_id: int | None = None) -> dict[str, Any]:
    from src.nadobro.strategy.bot_runtime import get_runtime_diagnostics
    from src.nadobro.runtime.runtime_supervisor import runtime_mode

    from src.nadobro.core.egress_geo import status_line as egress_status_line

    payload: dict[str, Any] = {
        "ts": time.time(),
        "queue": get_queue_diagnostics(),
        "runtime": get_runtime_diagnostics(),
        "perf": perf_snapshot(),
        "egress_geo": egress_status_line(),
        "runtime_env": {
            "NADO_RUNTIME_MODE": runtime_mode(),
            "NADO_STRATEGY_WORKERS": (os.environ.get("NADO_STRATEGY_WORKERS") or "2").strip(),
            "NADO_STRATEGY_CYCLE_TIMEOUT_SECONDS": (os.environ.get("NADO_STRATEGY_CYCLE_TIMEOUT_SECONDS") or "180").strip(),
        },
    }
    if telegram_id is not None:
        payload["wallet_readiness"] = get_runtime_wallet_readiness(telegram_id, verify_signer=False)
        if tooling_enabled():
            snap = get_account_snapshot(telegram_id, prefer_cli=False)
            entry: dict[str, Any] = {
                "success": bool(snap.get("success")),
                "source": snap.get("source"),
                "error": (snap.get("error") or "")[:200],
            }
            if snap.get("success") and isinstance(snap.get("data"), dict):
                d = snap["data"]
                entry["positions_count"] = len(d.get("positions") or [])
                entry["open_orders_count"] = len(d.get("open_orders") or [])
            payload["account_snapshot"] = entry
    return payload
