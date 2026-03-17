-- Migration 016: Drop plaintext webhook_secret from endpoints
-- Fixes issue #8: DB breach exposes all signing secrets
-- Deploy order: code first (stop writing column), then this migration
-- Convoy is the sole HMAC signer and stores secrets internally.

ALTER TABLE endpoints DROP COLUMN IF EXISTS webhook_secret;
