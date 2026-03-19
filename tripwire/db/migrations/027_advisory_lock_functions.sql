-- Advisory lock wrapper functions for leader election.
--
-- Used by the finality poller and pre-confirmed TTL sweeper to ensure
-- only one instance runs per poll cycle when multiple TripWire replicas
-- are deployed.  Each background task uses a distinct lock_id.
--
-- Lock IDs:
--   839201 = finality poller
--   839202 = pre-confirmed TTL sweeper

CREATE OR REPLACE FUNCTION try_acquire_leader_lock(lock_id bigint)
RETURNS boolean
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN pg_try_advisory_lock(lock_id);
END;
$$;

CREATE OR REPLACE FUNCTION release_leader_lock(lock_id bigint)
RETURNS boolean
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN pg_advisory_unlock(lock_id);
END;
$$;
