"""Leaf helpers for reading environment variables consistently.

This module has no intra-package imports (stdlib only) so it can be used from
any layer — config, services, handlers, engine, main — without risking an
import cycle.

The project historically inlined the truthy check
``os.environ.get(NAME, ...).strip().lower() in ("1", "true", "yes", "on")``
in ~20 places (config.py, main.py, and a dozen service modules), each with its
own default handling. ``env_bool`` is the single source of truth for that
parse. An unset **or blank** variable resolves to ``default``.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})

# An inline comment in an env value: whitespace followed by ``#`` and the rest
# of the line. Deployment dashboards / .env templates routinely carry a trailing
# "# note" that plain ``.strip()`` leaves attached — which historically broke
# provider model ids and would make ``float("30  # seconds")`` crash the boot.
# A single-token config value never contains " #", so cutting there is safe.
_INLINE_COMMENT_RE = re.compile(r"\s+#.*$", re.DOTALL)


def clean_env_value(raw: object) -> str:
    """Strip an inline ``# comment`` and surrounding whitespace from an env
    value. Idempotent and safe for single-token values (keys, URLs, model ids,
    numbers), which never legitimately contain a space-delimited ``#``."""
    text = "" if raw is None else str(raw)
    return _INLINE_COMMENT_RE.sub("", text).strip()


def env_str(name: str, default: str = "") -> str:
    """Cleaned string read: unset or blank (after comment-stripping) resolves
    to ``default``."""
    value = clean_env_value(os.environ.get(name))
    return value if value else default


def env_int(name: str, default: int) -> int:
    """Integer env read that tolerates inline comments and garbage.

    Unset/blank resolves to ``default``. An unparseable value logs a warning
    and resolves to ``default`` instead of raising — these reads mostly happen
    at import time, where a ValueError would take the whole bot down.
    """
    value = clean_env_value(os.environ.get(name))
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning("env %s=%r is not an int; using default %r", name, value, default)
        return default


def env_float(name: str, default: float) -> float:
    """Float env read with the same comment/garbage tolerance as env_int."""
    value = clean_env_value(os.environ.get(name))
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        logger.warning("env %s=%r is not a float; using default %r", name, value, default)
        return default


def env_bool(name: str, default: bool = False) -> bool:
    """Return the boolean value of env var ``name``.

    Unset or blank (whitespace-only, or comment-only) resolves to ``default``.
    Otherwise the value is truthy when it is one of ``1/true/yes/on``
    (case-insensitive). Inline ``# comments`` are stripped first, so
    ``FLAG=true  # enable`` reads as True.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    raw = clean_env_value(raw).lower()
    if raw == "":
        return default
    return raw in _TRUTHY


def env_tristate(name: str) -> bool | None:
    """Three-valued read: ``True`` for truthy, ``False`` for explicit falsy
    (``0/false/no/off``), ``None`` when unset/blank/unrecognized.

    Use when "not set" must be distinguished from "set to false" (e.g. a
    per-strategy override that should fall through to a global default).
    Inline ``# comments`` are stripped before matching.
    """
    raw = os.environ.get(name)
    if raw is None:
        return None
    raw = clean_env_value(raw).lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    return None


_WARNED_INT_SET_TOKENS: set[tuple[str, str]] = set()


def env_int_set(name: str) -> frozenset[int]:
    """Comma-separated integer set (e.g. Telegram user ids).

    Unset or blank resolves to an EMPTY set. Inline ``# comments`` are stripped
    first and blank tokens are ignored. A non-integer token is skipped with one
    WARNING per (name, token) — never raises, so a garbage entry can only shrink
    the set, never widen it.
    """
    ids: set[int] = set()
    for token in clean_env_value(os.environ.get(name)).split(","):
        token = token.strip()
        if not token:
            continue
        try:
            ids.add(int(token))
        except ValueError:
            if (name, token) not in _WARNED_INT_SET_TOKENS:
                _WARNED_INT_SET_TOKENS.add((name, token))
                logger.warning("env %s entry %r is not an integer; skipping", name, token[:32])
    return frozenset(ids)
