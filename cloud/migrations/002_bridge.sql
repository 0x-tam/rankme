CREATE TABLE rankme_cloud.snapshot (
  singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
  state jsonb NOT NULL DEFAULT '{}'::jsonb,
  last_seen timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE rankme_cloud.commands (
  id uuid PRIMARY KEY,
  owner_id uuid NOT NULL REFERENCES rankme_cloud.owner(id) ON DELETE CASCADE,
  request_key text,
  method text NOT NULL,
  path text NOT NULL,
  body jsonb NOT NULL,
  status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','leased','succeeded','failed','needs_review')),
  result jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  leased_at timestamptz,
  CONSTRAINT commands_owner_request_unique UNIQUE (owner_id, request_key)
);
CREATE INDEX commands_owner_status_idx ON rankme_cloud.commands(owner_id, status, created_at);
