CREATE SCHEMA IF NOT EXISTS rankme_cloud;

CREATE TABLE IF NOT EXISTS rankme_cloud.schema_migrations (
  version integer PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rankme_cloud.owner (
  id uuid PRIMARY KEY,
  singleton boolean NOT NULL DEFAULT true UNIQUE CHECK (singleton),
  user_handle bytea NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rankme_cloud.bootstrap (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  secret_hash text NOT NULL,
  expires_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS rankme_cloud.credentials (
  id text PRIMARY KEY,
  owner_id uuid NOT NULL REFERENCES rankme_cloud.owner(id) ON DELETE CASCADE,
  name text NOT NULL DEFAULT 'Passkey',
  public_key bytea NOT NULL,
  counter bigint NOT NULL DEFAULT 0,
  transports text[] NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  last_used_at timestamptz
);
CREATE INDEX IF NOT EXISTS credentials_owner_idx ON rankme_cloud.credentials(owner_id);

CREATE TABLE IF NOT EXISTS rankme_cloud.sessions (
  id uuid PRIMARY KEY,
  owner_id uuid NOT NULL REFERENCES rankme_cloud.owner(id) ON DELETE CASCADE,
  credential_id text NOT NULL REFERENCES rankme_cloud.credentials(id) ON DELETE CASCADE,
  token_hash text NOT NULL UNIQUE,
  csrf_hash text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  last_active_at timestamptz NOT NULL DEFAULT now(),
  absolute_expires_at timestamptz NOT NULL,
  idle_expires_at timestamptz NOT NULL,
  step_up_at timestamptz
);
CREATE INDEX IF NOT EXISTS sessions_owner_idx ON rankme_cloud.sessions(owner_id);

CREATE TABLE IF NOT EXISTS rankme_cloud.challenges (
  id uuid PRIMARY KEY,
  kind text NOT NULL CHECK (kind IN ('enroll','login','stepup','add')),
  challenge text NOT NULL,
  browser_hash text NOT NULL,
  session_id uuid REFERENCES rankme_cloud.sessions(id) ON DELETE CASCADE,
  bootstrap_hash text,
  name text,
  expires_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS challenges_browser_kind_idx ON rankme_cloud.challenges(browser_hash, kind, created_at DESC);

CREATE TABLE IF NOT EXISTS rankme_cloud.rate_limits (
  scope text NOT NULL,
  bucket text NOT NULL,
  window_start timestamptz NOT NULL,
  attempts integer NOT NULL,
  PRIMARY KEY (scope, bucket)
);
