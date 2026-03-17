# Goldsky Pipeline Setup

Goldsky Turbo streams raw ERC-3009 `AuthorizationUsed` events from Base, Ethereum, and Arbitrum and delivers them via webhook directly to TripWire's ingest endpoint. TripWire then verifies, enriches, and dispatches webhooks to your application.

## Prerequisites

- A [Goldsky](https://goldsky.com) account (free tier available)
- A Supabase project with the TripWire schema applied (see [Deployment Guide](./deployment.md))
- Goldsky CLI installed

## 1. Install the Goldsky CLI

```bash
curl https://goldsky.com | sh
```

Verify the installation:

```bash
goldsky --version
```

Authenticate:

```bash
goldsky login
```

## 2. Configure Your Webhook Secret

Goldsky needs a webhook URL and signing secret to deliver events to TripWire. Add the signing secret as a Goldsky secret:

```bash
goldsky secret create TRIPWIRE_WEBHOOK_SECRET --value "your_hmac_signing_secret"
```

Your TripWire ingest endpoint URL will be `https://your-tripwire-host/api/v1/ingest`. The signing secret must match the `GOLDSKY_WEBHOOK_SECRET` configured in TripWire's environment.

## 3. Understand the Pipeline Config

TripWire generates pipeline YAML via `tripwire.ingestion.pipeline`. Each pipeline has three sections:

### Sources

```yaml
sources:
  base_logs:
    type: dataset
    dataset_name: base.raw_logs
    version: "1.0.0"
```

This reads from Goldsky's indexed dataset of raw logs for the target chain. Available datasets: `ethereum.raw_logs`, `base.raw_logs`, `arbitrum.raw_logs`.

### Transforms

```yaml
transforms:
  erc3009_decoded:
    primary_key: id
    sql: >
      SELECT
        id,
        block_number,
        block_hash,
        transaction_hash,
        log_index,
        block_timestamp,
        _gs_log_decode(
          'event AuthorizationUsed(address indexed authorizer, bytes32 indexed nonce)',
          topics, data
        ) AS decoded
      FROM base_logs
      WHERE address = '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913'
      AND topic0 = '0x98de503528ee59b575ef0c0a2576a82497bfc029a5685b209e9ec333479b10a5'
```

- **address**: The USDC contract on the target chain (lowercased).
- **topic0**: The `keccak256` hash of `AuthorizationUsed(address,bytes32)`.
- **`_gs_log_decode`**: Goldsky's built-in ABI decoder that extracts `authorizer` and `nonce` from indexed topics.

### Sinks

```yaml
sinks:
  tripwire_webhook:
    type: webhook
    url: https://your-tripwire-host/api/v1/ingest
    secret_name: TRIPWIRE_WEBHOOK_SECRET
    from: erc3009_decoded
```

Decoded events are delivered via webhook POST to TripWire's ingest endpoint. Goldsky handles reorg detection automatically.

## 4. Generate Pipeline Config

You can generate the YAML programmatically:

```python
from tripwire.ingestion.pipeline import build_pipeline_yaml
from tripwire.types.models import ChainId

# Print the pipeline config for Base
print(build_pipeline_yaml(ChainId.BASE))
```

Or export it to a file:

```bash
python -c "
from tripwire.ingestion.pipeline import build_pipeline_yaml
from tripwire.types.models import ChainId
print(build_pipeline_yaml(ChainId.BASE))
" > pipeline-base.yaml
```

## 5. Deploy Pipelines

Deploy one pipeline per chain:

```bash
# Base (chain ID 8453)
goldsky turbo apply pipeline-base.yaml

# Ethereum (chain ID 1)
goldsky turbo apply pipeline-ethereum.yaml

# Arbitrum (chain ID 42161)
goldsky turbo apply pipeline-arbitrum.yaml
```

Or deploy programmatically:

```python
from tripwire.ingestion.pipeline import deploy_pipeline
from tripwire.types.models import ChainId

deploy_pipeline(ChainId.BASE)
deploy_pipeline(ChainId.ETHEREUM)
deploy_pipeline(ChainId.ARBITRUM)
```

Pipeline names follow the pattern `tripwire-<chain>-erc3009` (e.g., `tripwire-base-erc3009`).

## 6. Monitor Pipeline Status

Check status via CLI:

```bash
goldsky pipeline status tripwire-base-erc3009
goldsky pipeline status tripwire-ethereum-erc3009
goldsky pipeline status tripwire-arbitrum-erc3009
```

Or via the Goldsky dashboard at [app.goldsky.com](https://app.goldsky.com).

Expected statuses:
- **ACTIVE**: Pipeline is running and streaming events.
- **PAUSED**: Pipeline is stopped but can be resumed.
- **ERROR**: Pipeline encountered an issue (check logs).

## 7. Pipeline Lifecycle

Stop a pipeline:

```bash
goldsky pipeline stop tripwire-base-erc3009
```

Restart a stopped pipeline:

```bash
goldsky pipeline start tripwire-base-erc3009
```

Delete a pipeline:

```bash
goldsky pipeline delete tripwire-base-erc3009
```

## Troubleshooting

### Pipeline stuck in STARTING state

This usually means the webhook secret is invalid or the TripWire ingest endpoint is unreachable. Verify:

1. Your TripWire server is running and the `/api/v1/ingest` endpoint is accessible from the internet.
2. The `TRIPWIRE_WEBHOOK_SECRET` in Goldsky matches the `GOLDSKY_WEBHOOK_SECRET` in TripWire's environment.
3. Your TripWire host has a valid TLS certificate (Goldsky requires HTTPS for webhook delivery).

### No events appearing in TripWire

- Confirm the USDC contract address is correct and lowercased.
- Check that `topic0` matches `AuthorizationUsed`. The hash is: `0x98de503528ee59b575ef0c0a2576a82497bfc029a5685b209e9ec333479b10a5`.
- Verify the pipeline is in ACTIVE status.
- Check TripWire server logs for incoming webhook requests from Goldsky.
- Note: ERC-3009 `AuthorizationUsed` events only fire for `transferWithAuthorization` calls. If there are no x402 payments happening on that chain, there will be no events.

### Reorg handling

Goldsky automatically detects chain reorgs and delivers corrected events via webhook. TripWire's finality tracking layer provides an additional safety check before dispatching webhooks to your application.
