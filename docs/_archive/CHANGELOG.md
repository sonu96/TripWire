# Changelog

All notable changes to TripWire will be documented in this file.

## [Unreleased]

### Added
- **Trigger Registry**: Dynamic trigger system — create triggers for any EVM event via MCP, no deploy needed
- **Generic Event Decoder**: ABI-driven decoder handles any EVM event (not just ERC-3009)
- **Filter Engine**: Rule-based event filtering with 10 operators (eq, neq, gt, gte, lt, lte, in, not_in, between, contains, regex)
- **MCP Server**: 8 MCP tools mounted at /mcp — register_middleware, create_trigger, list_triggers, delete_trigger, list_templates, activate_template, get_trigger_status, search_events
- **register_middleware**: One-call MCP tool to set up TripWire as onchain middleware for any API
- **Trigger Bazaar**: 5 pre-built templates (whale transfer, DEX swap, NFT mint, ERC-3009 payment, ownership transfer)
- **x402 Bazaar Manifest**: /.well-known/x402-manifest.json for agent service discovery
- **Goldsky Edge Integration**: Replaced WebSocket subscriber + manual RPC with Edge managed endpoints (-726 lines)
