-- Drop Supabase RPC wrappers for advisory locks.
--
-- These functions were called via PostgREST but advisory locks acquired
-- through PostgREST are unreliable because PgBouncer / connection pooling
-- can return the connection to the pool immediately, releasing the lock.
--
-- Replaced by direct asyncpg connections in tripwire/db/postgres.py which
-- hold a dedicated connection for the entire lock duration.

DROP FUNCTION IF EXISTS try_acquire_leader_lock(bigint);
DROP FUNCTION IF EXISTS release_leader_lock(bigint);
