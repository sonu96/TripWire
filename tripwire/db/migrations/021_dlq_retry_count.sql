-- Add persistent DLQ retry count to webhook_deliveries.
-- Replaces the in-memory retry counter in DLQHandler so counts survive restarts.
ALTER TABLE webhook_deliveries ADD COLUMN IF NOT EXISTS dlq_retry_count INTEGER NOT NULL DEFAULT 0;
