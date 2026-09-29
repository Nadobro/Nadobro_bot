#!/usr/bin/env python3
"""
Hard reset Nadobro database: drops all known app tables and recreates the
production schema from src.nadobro.db.init_db().

Run from project root with .env set (or SUPABASE_DATABASE_URL):
  python scripts/reset_db.py
  # or with venv: .venv/bin/python scripts/reset_db.py

Type 'hard reset' when prompted to confirm. All application data in these
tables will be deleted and table definitions will be recreated.
"""
import sys
from pathlib import Path

# Ensure project root is on path when run as scripts/reset_db.py
_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from dotenv import load_dotenv

load_dotenv()

from src.nadobro.db import execute
from src.nadobro.models.database import init_db

DROP_SQL = """
DROP TABLE IF EXISTS
  miniapp_rate_limit,
  referral_volume_events,
  referrals,
  fill_sync_queue,
  strategy_performance_snapshots,
  strategies,
  points_snapshots,
  open_orders,
  positions,
  strategy_sessions,
  invite_codes,
  admin_logs,
  bot_state,
  copy_trades,
  copy_positions,
  copy_snapshots,
  copy_mirrors,
  copy_traders,
  alerts_mainnet,
  alerts_testnet,
  alerts,
  trades_mainnet,
  trades_testnet,
  trades,
  arcus_credentials,
  users
CASCADE;
"""


def main():
    confirm = input(
        "Hard reset database? This will DROP and RECREATE all Nadobro app tables. "
        "Type 'hard reset' to confirm: "
    )
    if confirm.strip().lower() != "hard reset":
        print("Aborted.")
        sys.exit(0)

    try:
        execute(DROP_SQL)
        init_db()
        print("Database hard reset complete. App tables dropped and production schema recreated.")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
