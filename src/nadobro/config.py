import logging
import os
from typing import Optional
from urllib.parse import urlsplit

from src.nadobro.utils.env import clean_env_value, env_bool, env_str
from src.nadobro.utils.venue_scope import (
    ARCUS_NETWORK_MAINNET,
    ARCUS_NETWORK_TESTNET,
    STR_STRIP_LOWER_ELSE_MAINNET,
    coerce_nado_network,
    guard_nado_scope,
    parse_arcus_net,
)

DATABASE_URL = os.environ.get("DATABASE_URL")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
BOT_USERNAME = (os.environ.get("BOT_USERNAME") or "Nadbro_bot").lstrip("@")
XAI_API_KEY = os.environ.get("XAI_API_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
NANOGPT_API_KEY = os.environ.get("NANOGPT_API_KEY") or os.environ.get("NANO_GPT_API_KEY")
NANOGPT_BASE_URL = os.environ.get("NANOGPT_BASE_URL", "https://nano-gpt.com/api/v1")
NANOGPT_MODEL = os.environ.get("NANOGPT_MODEL", "chatgpt-4o-latest")
DMIND_API_KEY = os.environ.get("DMIND_API_KEY")
DMIND_BASE_URL = os.environ.get("DMIND_BASE_URL", "https://api.dmind.ai")
DMIND_MODEL = os.environ.get("DMIND_MODEL", "dmind-finance")
# n8n: also accepts n8n_Server_URL, n8n_authorization, n8n_MCP_Access_Token
# (resolved in connectors/provider_config.py). Only feeds provider-status display
# now — the prompt-to-workflow builder that deployed to n8n was removed.
N8N_BASE_URL = os.environ.get("N8N_BASE_URL", "").rstrip("/")
N8N_API_KEY = os.environ.get("N8N_API_KEY", "")
N8N_WEBHOOK_SECRET = os.environ.get("N8N_WEBHOOK_SECRET", "")
INK_RPC_URL = os.environ.get("INK_RPC_URL", "")
INK_WS_URL = os.environ.get("INK_WS_URL", "")
ARKHAM_API_KEY = os.environ.get("ARKHAM_API_KEY", "")
COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY", "")
COINGLASS_API_KEY = os.environ.get("COINGLASS_API_KEY", "")
DEFILLAMA_API_KEY = os.environ.get("DEFILLAMA_API_KEY", "")
GLASSNODE_API_KEY = os.environ.get("GLASSNODE_API_KEY", "")
NFTGO_API_KEY = os.environ.get("NFTGO_API_KEY", "")
ROOTDATA_API_KEY = os.environ.get("ROOTDATA_API_KEY", "")
GLOBAL_NEWS_API_KEY = os.environ.get("GLOBAL_NEWS_API_KEY", "")
POLYMARKET_API_KEY = os.environ.get("POLYMARKET_API_KEY", "")
GOPLUS_API_KEY = os.environ.get("GOPLUS_API_KEY", "")
FMP_API_KEY = os.environ.get("FMP_API_KEY", "")
ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY")
PINECONE_API_KEY = os.environ.get("PINECONE_API_KEY")
PINECONE_INDEX_NAME = os.environ.get("PINECONE_INDEX_NAME", "nadobro")
X_API_BEARER_TOKEN = os.environ.get("X_API_BEARER_TOKEN")
CRYPTOPANIC_API_KEY = os.environ.get("CRYPTOPANIC_API_KEY", "")
def _parse_admin_user_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for token in clean_env_value(raw).split(","):
        token = token.strip()
        if not token:
            continue
        try:
            ids.append(int(token))
        except ValueError:
            logging.getLogger(__name__).warning(
                "ADMIN_USER_IDS entry %r is not an integer; skipping", token
            )
    return ids


ADMIN_USER_IDS = _parse_admin_user_ids(os.environ.get("ADMIN_USER_IDS", ""))

NADO_TESTNET_REST = "https://gateway.test.nado.xyz/v1"
NADO_MAINNET_REST = "https://gateway.prod.nado.xyz/v1"

NADO_TESTNET_ARCHIVE = "https://archive.test.nado.xyz/v1"
NADO_MAINNET_ARCHIVE = "https://archive.prod.nado.xyz/v1"

# Archive (Rewards) — a SEPARATE service/path from the archive indexer above.
# The Ink airdrop allocation query (variant ``ink_airdrop``) is served HERE, not
# on the indexer ``/v1`` (whose request enum has no such variant and rejects it
# with HTTP 422 "unknown variant `ink_airdrop`"). Per the live Nado endpoints list
# and https://docs.nado.xyz/developer-resources/api/rewards/ink-airdrop
NADO_TESTNET_ARCHIVE_REWARDS = "https://archive.test.nado.xyz/rewards/v1"
NADO_MAINNET_ARCHIVE_REWARDS = "https://archive.prod.nado.xyz/rewards/v1"

# --- Arcus (second venue; perps only) --------------------------------------
# REST base has NO "/v1" (client paths carry it). Sources: Arcus docs
# place-order servers "https://api.arcus.xyz Mainnet … https://api.testnet.arcus.xyz
# Testnet"; websocket "Connect to wss://api.testnet.arcus.xyz/v1/ws (mainnet:
# wss://api.arcus.xyz/v1/ws)". Env overrides are read at CALL time only
# (nothing here reads an ARCUS_* variable at import).
ARCUS_TESTNET_REST_DEFAULT = "https://api.testnet.arcus.xyz"
ARCUS_MAINNET_REST_DEFAULT = "https://api.arcus.xyz"
ARCUS_TESTNET_WS_DEFAULT = "wss://api.testnet.arcus.xyz/v1/ws"
ARCUS_MAINNET_WS_DEFAULT = "wss://api.arcus.xyz/v1/ws"

_ARCUS_REST_ENV = {
    ARCUS_NETWORK_TESTNET: ("ARCUS_TESTNET_REST_URL", ARCUS_TESTNET_REST_DEFAULT),
    ARCUS_NETWORK_MAINNET: ("ARCUS_MAINNET_REST_URL", ARCUS_MAINNET_REST_DEFAULT),
}
_ARCUS_WS_ENV = {
    ARCUS_NETWORK_TESTNET: ("ARCUS_TESTNET_WS_URL", ARCUS_TESTNET_WS_DEFAULT),
    ARCUS_NETWORK_MAINNET: ("ARCUS_MAINNET_WS_URL", ARCUS_MAINNET_WS_DEFAULT),
}
# Plain-text schemes are allowed ONLY for a local fake on the loopback host.
_ARCUS_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})


def _arcus_url_ok(url: str, *, secure_scheme: str, local_scheme: str) -> bool:
    """A parsed (never prefix-matched) check: ``http://127.0.0.1.evil.example``
    must not pass as local. No credentials, query or fragment in a base URL."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        _ = parts.port  # raises ValueError on a malformed / out-of-range port
    except ValueError:
        return False
    if not host or parts.username is not None or parts.password is not None:
        return False
    if parts.query or parts.fragment:
        return False
    scheme = parts.scheme.lower()
    if scheme == secure_scheme:
        return True
    return scheme == local_scheme and host.lower() in _ARCUS_LOCAL_HOSTS


def _arcus_host_key(url: str) -> str | None:
    """The host an Arcus URL talks to, for the cross-network check: lowercase,
    no trailing dot; a loopback fake keys on its port too (two local fakes may
    stand in for the two networks). None when the URL cannot be parsed."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower().rstrip(".")
    if host in _ARCUS_LOCAL_HOSTS:
        if port is None:
            port = 443 if parts.scheme.lower() in ("https", "wss") else 80
        return f"loopback:{port}"
    return host


def arcus_url_conflicts(net: str, url: str) -> bool:
    """True when ``url``'s host is one the OTHER Arcus network uses: its
    documented REST/WS default host or its configured ``ARCUS_*_URL`` override.
    One env typo (``ARCUS_TESTNET_REST_URL=https://api.arcus.xyz``) must never
    point the testnet scope at mainnet, or the reverse (R2-5; build decision 7
    "mainnet needs ARCUS_MAINNET_ENABLED")."""
    this = parse_arcus_net(net)
    key = _arcus_host_key(url)
    if key is None:
        return False
    for table in (_ARCUS_REST_ENV, _ARCUS_WS_ENV):
        for other, (env_name, default) in table.items():
            if other == this:
                continue
            for candidate in (default, env_str(env_name, default).rstrip("/")):
                if _arcus_host_key(candidate) == key:
                    return True
    return False


def arcus_rest_url(net: str) -> str:
    """Arcus REST base URL for ``net`` (``'testnet'``/``'mainnet'`` only).

    ``ARCUS_TESTNET_REST_URL`` / ``ARCUS_MAINNET_REST_URL`` override the
    defaults (inline ``# comments`` allowed). The result has no trailing ``/``
    and must be ``https://`` (``http://`` only for 127.0.0.1 / localhost
    fakes), and its host must not be one the OTHER network uses
    (:func:`arcus_url_conflicts`), else ``ValueError``. The URL is never logged.
    """
    env_name, default = _ARCUS_REST_ENV[parse_arcus_net(net)]
    url = env_str(env_name, default).rstrip("/")
    if not _arcus_url_ok(url, secure_scheme="https", local_scheme="http"):
        raise ValueError("ARCUS REST URL must be https")
    if arcus_url_conflicts(net, url):
        raise ValueError("ARCUS REST URL points at the other Arcus network's host")
    return url


def arcus_ws_url(net: str) -> str:
    """Arcus WebSocket URL for ``net``: ``ARCUS_TESTNET_WS_URL`` /
    ``ARCUS_MAINNET_WS_URL`` or the default; must be ``wss://`` (``ws://`` only
    for 127.0.0.1 / localhost fakes) on a host the OTHER network does not use,
    else ``ValueError``. Used from P4a."""
    env_name, default = _ARCUS_WS_ENV[parse_arcus_net(net)]
    url = env_str(env_name, default).rstrip("/")
    if not _arcus_url_ok(url, secure_scheme="wss", local_scheme="ws"):
        raise ValueError("ARCUS WS URL must be wss")
    if arcus_url_conflicts(net, url):
        raise ValueError("ARCUS WS URL points at the other Arcus network's host")
    return url

PRODUCTS = {
    "USDT0": {"id": 0, "type": "spot"},
    "BTC": {"id": 2, "type": "perp", "symbol": "BTC-PERP"},
    "ETH": {"id": 4, "type": "perp", "symbol": "ETH-PERP"},
    "SOL": {"id": 8, "type": "perp", "symbol": "SOL-PERP"},
    "XRP": {"id": 10, "type": "perp", "symbol": "XRP-PERP"},
    "BNB": {"id": 14, "type": "perp", "symbol": "BNB-PERP"},
    "LINK": {"id": 16, "type": "perp", "symbol": "LINK-PERP"},
    "DOGE": {"id": 22, "type": "perp", "symbol": "DOGE-PERP"},
}

# Spot product ids currently supported for DN hedge legs.
SPOT_PRODUCT_IDS = {
    "USDT0": 0,
    "BTC": 1,
    "ETH": 3,
}

PRODUCT_ALIASES = {}
for name, info in PRODUCTS.items():
    PRODUCT_ALIASES[name.lower()] = info["id"]
    if "symbol" in info:
        PRODUCT_ALIASES[info["symbol"].lower()] = info["id"]
        PRODUCT_ALIASES[info["symbol"].replace("-PERP", "").lower() + "-perp"] = info["id"]

def _default_catalog_network() -> str:
    return os.environ.get("NADO_PRODUCT_CATALOG_DEFAULT_NETWORK", "mainnet").strip().lower() or "mainnet"


def get_product_id(name: str, network: str = None, client=None) -> Optional[int]:
    network_name = guard_nado_scope(
        str(network or _default_catalog_network()), site="config.get_product_id"
    )
    try:
        from src.nadobro.venue.product_catalog import get_product_id as _catalog_product_id

        pid = _catalog_product_id(name, network=network_name, client=client)
        if pid is not None:
            return pid
    except Exception:
        pass
    return PRODUCT_ALIASES.get((name or "").lower().strip())

def get_product_name(product_id: int, network: str = None, client=None) -> str:
    network_name = guard_nado_scope(
        str(network or _default_catalog_network()), site="config.get_product_name"
    )
    try:
        from src.nadobro.venue.product_catalog import get_product_name as _catalog_product_name

        resolved = _catalog_product_name(product_id, network=network_name, client=client)
        if resolved and not resolved.startswith("ID:"):
            return resolved
    except Exception:
        pass
    for name, info in PRODUCTS.items():
        if info["id"] == product_id:
            return info.get("symbol", name)
    return f"ID:{product_id}"


def get_spot_product_id(name: str, network: str = None, client=None) -> Optional[int]:
    network_name = guard_nado_scope(
        str(network or _default_catalog_network()), site="config.get_spot_product_id"
    )
    try:
        from src.nadobro.venue.product_catalog import get_spot_product_id as _catalog_spot_product_id

        pid = _catalog_spot_product_id(name, network=network_name)
        if pid is not None:
            return pid
    except Exception:
        pass
    return SPOT_PRODUCT_IDS.get((name or "").upper().strip())


def get_spot_metadata(name: str, network: str = None) -> dict:
    network_name = guard_nado_scope(
        str(network or _default_catalog_network()), site="config.get_spot_metadata"
    )
    try:
        from src.nadobro.venue.product_catalog import get_spot_metadata as _catalog_spot_metadata

        metadata = _catalog_spot_metadata(name, network=network_name)
        if metadata:
            return metadata
    except Exception:
        pass
    spot_pid = SPOT_PRODUCT_IDS.get((name or "").upper().strip())
    if spot_pid is None:
        return {}
    return {"id": int(spot_pid), "symbol": str(name or "").upper().strip()}

RATE_LIMIT_SECONDS = 5
MAX_LEVERAGE = 50
MIN_TRADE_SIZE_USD = 1.0

# Fallback leverage caps when the live catalog is unavailable (Nado mainnet overrides via API).
PRODUCT_MAX_LEVERAGE = {
    "BTC": 50,
    "ETH": 50,
    "SOL": 40,
    "XRP": 20,
    "BNB": 20,
    "LINK": 20,
    "DOGE": 20,
}


def get_product_max_leverage(product: str, network: str = None, client=None) -> int:
    network_name = guard_nado_scope(
        str(network or _default_catalog_network()), site="config.get_product_max_leverage"
    )
    try:
        from src.nadobro.venue.product_catalog import get_product_max_leverage as _catalog_max_leverage

        return int(_catalog_max_leverage(product, network=network_name, client=client))
    except Exception:
        product_key = (product or "").upper().strip()
        if product_key not in PRODUCT_MAX_LEVERAGE:
            return 1  # Safe default for unknown products
        return int(PRODUCT_MAX_LEVERAGE[product_key])


def get_product_initial_margin_fraction(product: str, network: str = None, client=None) -> float:
    """Initial-margin fraction (``1/max_leverage``). Mirrors
    ``get_product_max_leverage``: live catalog first, static table on failure."""
    network_name = guard_nado_scope(
        str(network or _default_catalog_network()), site="config.get_product_initial_margin_fraction"
    )
    try:
        from src.nadobro.venue.product_catalog import (
            get_product_initial_margin_fraction as _catalog_imf,
        )

        return float(_catalog_imf(product, network=network_name, client=client))
    except Exception:
        return 1.0 / max(1, get_product_max_leverage(product, network=network_name, client=client))


def get_product_maintenance_margin_fraction(product: str, network: str = None, client=None) -> float:
    """Maintenance-margin fraction — the liquidation buffer the strategy leverage
    guard checks. Live catalog first (venue maintenance weight when available,
    else a conservative fallback derived from imf); static fallback on failure."""
    network_name = guard_nado_scope(
        str(network or _default_catalog_network()), site="config.get_product_maintenance_margin_fraction"
    )
    try:
        from src.nadobro.venue.product_catalog import (
            get_product_maintenance_margin_fraction as _catalog_mmf,
        )

        return float(_catalog_mmf(product, network=network_name, client=client))
    except Exception:
        imf = get_product_initial_margin_fraction(product, network=network_name, client=client)
        from src.nadobro.quant.liquidation import fallback_mmf
        return fallback_mmf(imf)


def get_perp_products(network: str = None, client=None) -> list[str]:
    network_name = guard_nado_scope(
        str(network or _default_catalog_network()), site="config.get_perp_products"
    )
    try:
        from src.nadobro.venue.product_catalog import list_perp_names

        products = list_perp_names(network=network_name, client=client)
        if products:
            return products
    except Exception:
        pass
    return [name for name, info in PRODUCTS.items() if info.get("type") == "perp"]


def get_dn_pair(product: str, network: str = None, client=None) -> dict:
    network_name = guard_nado_scope(
        str(network or _default_catalog_network()), site="config.get_dn_pair"
    )
    try:
        from src.nadobro.venue.product_catalog import get_dn_pair as _catalog_dn_pair

        pair = _catalog_dn_pair(product, network=network_name, client=client)
        if pair:
            return pair
    except Exception:
        pass
    spot_pid = get_spot_product_id(product, network=network_name, client=client)
    perp_pid = get_product_id(product, network=network_name, client=client)
    if spot_pid is None or perp_pid is None:
        return {}
    product_key = (product or "").upper().replace("-PERP", "").strip()
    return {
        "product": product_key,
        "underlying_key": product_key,
        "perp_product_id": int(perp_pid),
        "perp_symbol": get_product_name(perp_pid, network=network_name, client=client),
        "spot_product_id": int(spot_pid),
        "spot_symbol": product_key,
        "spot_trading_status": "live",
        "perp_trading_status": "live",
        "spot_market_hours": None,
        "perp_market_hours": None,
        "entry_allowed": True,
        "entry_block_reason": "",
    }


# Volume strategy spot mode SAFETY FLOOR: used only if the live catalog is
# unreachable. The live list comes from ``product_catalog.list_volume_spot_bases``,
# so adding a new spot market on Nado (e.g. QQQX, SPYX) shows up automatically
# without a code change. Quote-like assets (USDC, USDT0) are NEVER tradeable as
# a base and are excluded everywhere.
VOLUME_SPOT_SYMBOLS: tuple[str, ...] = ("KBTC", "WETH", "BTC", "ETH", "QQQX", "SPYX")


def normalize_volume_spot_symbol(name: str) -> str:
    """Canonicalize a user/miniapp/state symbol to the catalog-facing key.

    Maps common alias forms (``btc`` -> ``KBTC``, ``eth`` -> ``WETH``) and
    strips quoting suffixes (``KBTC-USDC0`` -> ``KBTC``) so callers can pass
    whatever shape they have without crashing the controller.
    """
    raw = (name or "").strip()
    if not raw:
        return ""
    # Strip dashed/slashed quote suffix: "KBTC-USDC0" -> "KBTC".
    head = raw.replace("/", "-").split("-", 1)[0]
    low = head.lower().replace(" ", "")
    if low in ("btc", "kbtc"):
        return "KBTC"
    if low in ("eth", "weth"):
        return "WETH"
    return head.upper()


def list_volume_spot_product_names(network: str = None, client=None) -> list[str]:
    """Spot base symbols available for the Volume strategy on ``network``.

    Live source: ``product_catalog.list_volume_spot_bases`` (Nado v2 spot
    catalog). Falls back to ``VOLUME_SPOT_SYMBOLS`` filtered by which symbols
    resolve to a spot product id, so the menu still renders something if the
    archive endpoint is temporarily 403'd.
    """
    network_name = guard_nado_scope(
        str(network or _default_catalog_network()), site="config.list_volume_spot_product_names"
    )
    try:
        from src.nadobro.venue.product_catalog import list_volume_spot_bases

        names = list_volume_spot_bases(network=network_name)
        if names:
            return names
    except Exception:
        pass
    out: list[str] = []
    for sym in VOLUME_SPOT_SYMBOLS:
        if get_spot_product_id(sym, network=network_name, client=client) is not None:
            out.append(sym)
    return out


def get_dn_products(network: str = None, client=None) -> list[str]:
    network_name = guard_nado_scope(
        str(network or _default_catalog_network()), site="config.get_dn_products"
    )
    try:
        from src.nadobro.venue.product_catalog import list_dn_product_names as _catalog_dn_products

        products = _catalog_dn_products(network=network_name, client=client)
        if products:
            return products
    except Exception:
        pass
    return [name for name in ("BTC", "ETH") if get_spot_product_id(name, network=network_name, client=client) is not None]


def is_product_isolated_only(product: str, network: str = None, client=None) -> bool:
    network_name = guard_nado_scope(
        str(network or _default_catalog_network()), site="config.is_product_isolated_only"
    )
    try:
        from src.nadobro.venue.product_catalog import is_product_isolated_only as _catalog_isolated_only

        return bool(_catalog_isolated_only(product, network=network_name, client=client))
    except Exception:
        return False

EST_FEE_RATE = 0.0003
EST_FILL_EFFICIENCY = 0.45
DUAL_MODE_CARD_FLOW = env_bool("DUAL_MODE_CARD_FLOW", True)

NADO_BUILDER_ID_ENV = "NADO_BUILDER_ID"
NADO_BUILDER_FEE_RATE_ENV = "NADO_BUILDER_FEE_RATE"
NADO_BUILDER_FEE_RATE_1_BPS = 10  # 0.1 bps units


def get_nado_builder_routing_config(network: str | None = None) -> tuple[int, int]:
    """Return validated (builder_id, builder_fee_rate) for order routing.

    Safety-first behavior:
    - Builder routing is mandatory for **mainnet** order placement only.
    - Testnet orders bypass builder routing entirely and return ``(0, 0)`` —
      the venue's builder registry is mainnet-only, and a stale/invalid
      builder id is what was causing testnet places to fail with
      ``error_code=2118 "Invalid builder"``.
    - Fee rate is locked to 1 bps (10 units) to avoid accidental fee changes.
    """
    # Legacy ``isinstance(network, str) and network.strip().lower() == "testnet"``
    # (a non-str is mainnet) kept exactly; an Arcus scope raises VenueScopeError.
    if coerce_nado_network(
        network, STR_STRIP_LOWER_ELSE_MAINNET, site="config.get_nado_builder_routing_config"
    ) == "testnet":
        return 0, 0

    builder_id_raw = (os.environ.get(NADO_BUILDER_ID_ENV) or "").strip()
    if not builder_id_raw:
        raise ValueError(f"{NADO_BUILDER_ID_ENV} is required for order placement.")

    try:
        builder_id = int(builder_id_raw)
    except ValueError as exc:
        raise ValueError(f"{NADO_BUILDER_ID_ENV} must be an integer in [1, 65535].") from exc

    if builder_id < 1 or builder_id > 65535:
        raise ValueError(f"{NADO_BUILDER_ID_ENV} must be in [1, 65535].")

    fee_rate_raw = (os.environ.get(NADO_BUILDER_FEE_RATE_ENV) or str(NADO_BUILDER_FEE_RATE_1_BPS)).strip()
    try:
        fee_rate = int(fee_rate_raw)
    except ValueError as exc:
        raise ValueError(f"{NADO_BUILDER_FEE_RATE_ENV} must be an integer in [0, 1023].") from exc

    if fee_rate < 0 or fee_rate > 1023:
        raise ValueError(f"{NADO_BUILDER_FEE_RATE_ENV} must be in [0, 1023].")

    if fee_rate != NADO_BUILDER_FEE_RATE_1_BPS:
        raise ValueError(
            f"{NADO_BUILDER_FEE_RATE_ENV} must be {NADO_BUILDER_FEE_RATE_1_BPS} (1 bps) for safe routing."
        )

    return builder_id, fee_rate

