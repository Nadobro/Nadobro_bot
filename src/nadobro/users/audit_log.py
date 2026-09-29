"""Append-only security audit trail for sensitive user actions.

Records to the ``audit_logs`` table (see ``db.init_db``). This is intentionally
fail-soft: an audit write must NEVER break the action it's recording, so every
helper swallows DB errors (and the failure is logged). Details strings pass
through the same log-redaction used elsewhere so we never persist a raw key,
token, or address into the audit row.

Sensitive events currently recorded:
  - wallet_linked / wallet_unlinked        (key lifecycle)
  - order_placed                           (every signed order; agent + manual)
  - howl_suggestion_rejected               (blocked unsafe auto-tuning)
  - agent_enabled / agent_disabled         (autonomous execution toggles)
  - venue_switched                         (/venue view selection; details "nado->arcus")
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_MAX_DETAILS = 1000


def record_audit_event(user_id: int | None, action: str, details: str | None = None) -> None:
    """Insert one append-only audit row. Never raises."""
    try:
        from src.nadobro.db import execute
        from src.nadobro.core.log_redaction import redact_sensitive_text

        safe_action = str(action or "unknown")[:128]
        safe_details = None
        if details is not None:
            safe_details = str(redact_sensitive_text(str(details)))[:_MAX_DETAILS]
        execute(
            "INSERT INTO audit_logs (user_id, action, details) VALUES (%s, %s, %s)",
            (int(user_id) if user_id is not None else None, safe_action, safe_details),
        )
    except Exception as exc:  # never let auditing break the audited action
        logger.warning("audit_log write failed action=%s: %s", action, exc)


def get_recent_audit_events(user_id: int | None = None, limit: int = 50) -> list[dict]:
    """Read recent audit rows (admin/diagnostics helper)."""
    try:
        from src.nadobro.db import query_all

        capped = max(1, min(int(limit), 500))
        if user_id is None:
            return query_all(
                "SELECT user_id, action, details, created_at FROM audit_logs "
                "ORDER BY created_at DESC LIMIT %s",
                (capped,),
            )
        return query_all(
            "SELECT user_id, action, details, created_at FROM audit_logs "
            "WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
            (int(user_id), capped),
        )
    except Exception as exc:
        logger.warning("audit_log read failed: %s", exc)
        return []
