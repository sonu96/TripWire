-- Migration 008: Rename Svix columns to Convoy equivalents
-- Part of the Svix → Convoy self-hosted migration

-- Rename endpoint columns
ALTER TABLE endpoints RENAME COLUMN svix_app_id TO convoy_project_id;
ALTER TABLE endpoints RENAME COLUMN svix_endpoint_id TO convoy_endpoint_id;

-- Add webhook_secret column for per-endpoint HMAC signing
ALTER TABLE endpoints ADD COLUMN IF NOT EXISTS webhook_secret TEXT;

-- Rename webhook delivery tracking column
ALTER TABLE webhook_deliveries RENAME COLUMN svix_message_id TO provider_message_id;
