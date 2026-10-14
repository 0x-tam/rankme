CREATE TABLE rankme_cloud.booking_references (
  code text PRIMARY KEY CHECK (code ~ '^LM-[A-F0-9]{12}$'),
  owner_id uuid NOT NULL REFERENCES rankme_cloud.owner(id) ON DELETE CASCADE,
  department text NOT NULL CHECK (department IN ('adultsCosmetics','pediatric','orthodontics')),
  source text NOT NULL CHECK (source IN ('google_search','google_maps','unknown')),
  test_mode boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL DEFAULT (now() + interval '90 days')
);
CREATE INDEX booking_references_owner_created_idx ON rankme_cloud.booking_references(owner_id, created_at DESC);

CREATE TABLE rankme_cloud.bookings (
  id uuid PRIMARY KEY,
  owner_id uuid NOT NULL REFERENCES rankme_cloud.owner(id) ON DELETE CASCADE,
  reference_code text UNIQUE REFERENCES rankme_cloud.booking_references(code),
  request_key text NOT NULL,
  department text NOT NULL CHECK (department IN ('adultsCosmetics','pediatric','orthodontics')),
  source text NOT NULL CHECK (source IN ('google_search','google_maps','unknown')),
  clinic_key text NOT NULL DEFAULT 'lumident' CHECK (clinic_key='lumident'),
  status text NOT NULL CHECK (status IN ('confirmed','cancelled')),
  test_mode boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX bookings_owner_created_idx ON rankme_cloud.bookings(owner_id, created_at DESC);
CREATE UNIQUE INDEX bookings_owner_request_idx ON rankme_cloud.bookings(owner_id, request_key);

CREATE TABLE rankme_cloud.booking_events (
  id bigserial PRIMARY KEY,
  booking_id uuid NOT NULL REFERENCES rankme_cloud.bookings(id),
  owner_id uuid NOT NULL REFERENCES rankme_cloud.owner(id),
  action text NOT NULL CHECK (action IN ('confirmed','cancelled','corrected','reconfirmed')),
  department text NOT NULL CHECK (department IN ('adultsCosmetics','pediatric','orthodontics')),
  status text NOT NULL CHECK (status IN ('confirmed','cancelled')),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX booking_events_booking_idx ON rankme_cloud.booking_events(booking_id, created_at);
