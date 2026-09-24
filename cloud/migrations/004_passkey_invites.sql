CREATE TABLE IF NOT EXISTS rankme_cloud.passkey_invites (
  owner_id uuid PRIMARY KEY REFERENCES rankme_cloud.owner(id) ON DELETE CASCADE,
  secret_hash text NOT NULL UNIQUE,
  expires_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE rankme_cloud.challenges
  DROP CONSTRAINT IF EXISTS challenges_kind_check;
ALTER TABLE rankme_cloud.challenges
  ADD CONSTRAINT challenges_kind_check CHECK (kind IN ('enroll','login','stepup','add','invite'));
ALTER TABLE rankme_cloud.challenges ADD COLUMN IF NOT EXISTS invite_hash text;
CREATE INDEX IF NOT EXISTS challenges_invite_hash_idx ON rankme_cloud.challenges(invite_hash)
  WHERE invite_hash IS NOT NULL;
