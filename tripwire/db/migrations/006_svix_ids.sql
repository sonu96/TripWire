-- TripWire: Add Svix provider IDs to endpoints table
-- Stores the Svix application ID and endpoint ID returned during registration,
-- so dispatch uses the correct Svix app_id instead of the TripWire endpoint ID.

ALTER TABLE endpoints ADD COLUMN IF NOT EXISTS svix_app_id TEXT;
ALTER TABLE endpoints ADD COLUMN IF NOT EXISTS svix_endpoint_id TEXT;
