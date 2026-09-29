-- Venue selection + Arcus API credentials (Arcus multi-venue, Phase 1 plumbing).
--
-- `users.active_venue` is which venue's screens and actions the user SEES. Venues
-- run IN PARALLEL: flipping it never stops, flattens, cancels or resumes anything
-- on either venue. Every existing row reads as Nado through the column default —
-- there is no backfill. `users.network_mode` stays Nado-only; Arcus keeps its own
-- testnet/mainnet in `users.arcus_network_mode`.
--
-- `arcus_credentials` holds at most one pasted Arcus API Signing Key per
-- (user, network), encrypted with the server key (core/crypto.py
-- encrypt_with_server_key). `address` is stored lowercase and `api_public_key` is
-- the 64-hex Ed25519 public key. The partial unique index stops two Telegram users
-- from driving the same Arcus subaccount while both links are active. Phase 1
-- creates the table only; nothing reads or writes it yet.
--
-- Idempotent: db.py's init_db carries the same DDL in its own block (never inside
-- the Nado per-network loops) for deployments that have not run this file.

ALTER TABLE users
  ADD COLUMN IF NOT EXISTS active_venue TEXT NOT NULL DEFAULT 'nado'
    CONSTRAINT users_active_venue_check CHECK (active_venue IN ('nado', 'arcus'));

ALTER TABLE users
  ADD COLUMN IF NOT EXISTS arcus_network_mode TEXT NOT NULL DEFAULT 'testnet'
    CONSTRAINT users_arcus_network_mode_check CHECK (arcus_network_mode IN ('testnet', 'mainnet'));

CREATE TABLE IF NOT EXISTS arcus_credentials (
  id                    BIGSERIAL PRIMARY KEY,
  user_id               BIGINT NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
  network               TEXT NOT NULL CHECK (network IN ('testnet', 'mainnet')),
  address               TEXT NOT NULL CHECK (address ~ '^0x[0-9a-f]{40}$'),
  account_index         INT NOT NULL DEFAULT 0 CHECK (account_index BETWEEN 0 AND 9),
  all_subaccounts       BOOLEAN NOT NULL DEFAULT false,
  api_public_key        TEXT NOT NULL CHECK (api_public_key ~ '^[0-9a-f]{64}$'),
  encrypted_signing_key TEXT NOT NULL,
  api_wallet_name       TEXT,
  valid_until_ms        BIGINT CHECK (valid_until_ms >= 0),
  status                TEXT NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active', 'invalid', 'expired', 'unlinked')),
  attested_at           TIMESTAMPTZ,
  linked_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_verified_at      TIMESTAMPTZ,
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, network)
);

CREATE UNIQUE INDEX IF NOT EXISTS arcus_credentials_active_subaccount_uq
  ON arcus_credentials (network, address, account_index)
  WHERE status = 'active';
