-- Phase 4 (ADR-0017 note "No time limit for cancels and expiries"): the sweeper asks order-service again for an
-- expiry the engine has not answered within 5 minutes, so its query no longer requires expiry_requested_at IS NULL.
-- Unconfirmed active reservations are few at any moment (most are confirmed within milliseconds).
DROP INDEX IF EXISTS reservations_orphan_candidates;
CREATE INDEX reservations_unconfirmed ON reservations (created_at)
    WHERE status = 'ACTIVE' AND NOT confirmed;
