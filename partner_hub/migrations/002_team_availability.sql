BEGIN;

ALTER TABLE partner_members
  DROP CONSTRAINT IF EXISTS partner_members_role_check;

ALTER TABLE partner_members
  ADD CONSTRAINT partner_members_role_check
  CHECK (
    role IN ('AGENCY_OWNER','AGENCY_ADMIN','ADVISOR','SECRETARY')
  );

ALTER TABLE partner_members
  ADD COLUMN IF NOT EXISTS can_manage_team BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS can_view_stats BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE partner_members
SET can_manage_team = TRUE,
    can_view_stats = TRUE
WHERE role IN ('AGENCY_OWNER','AGENCY_ADMIN');

ALTER TABLE partner_invites
  ADD COLUMN IF NOT EXISTS is_revoked BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE partner_submissions
  ADD COLUMN IF NOT EXISTS last_availability_confirmed_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS availability_check_due_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS availability_check_sent_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS availability_reminder_sent_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS availability_missed_cycles INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS availability_paused_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS status_before_availability_pause VARCHAR(30),
  ADD COLUMN IF NOT EXISTS availability_last_error TEXT;

UPDATE partner_submissions
SET last_availability_confirmed_at = COALESCE(
      last_availability_confirmed_at,
      NOW()
    ),
    availability_check_due_at = COALESCE(
      availability_check_due_at,
      NOW() + INTERVAL '3 days'
    )
WHERE status = 'ACTIVE';

CREATE INDEX IF NOT EXISTS idx_partner_availability_due
ON partner_submissions(status, availability_check_due_at);

COMMIT;
