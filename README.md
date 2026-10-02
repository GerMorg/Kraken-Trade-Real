# Kraken AI Trader

A clean Home Assistant custom integration implementing the autonomous trader defined by `AUTONOMOUS_TRADER_SPEC(1).md` and `MASTER_PROMPT_AUTONOMOUS_KRAKEN_AI_TRADER(1).md`.

## Architecture

The runtime has one order authority: `CentralTradingAuthority`. Kraken is account/order truth; SQLite is historical materialization. News, Gemini, market data, learning and sensors never submit orders.

The implementation supports dynamic Kraken Spot/Spot Margin discovery and separate Kraken Futures discovery/trading when futures credentials are configured. Market snapshots are filtered by liquidity/spread/data quality before deeper feature/regime/signal analysis. Long and short are scored independently. Risk limits are immutable during learning and are applied again immediately before an order intent can be submitted.

Gemini uses structured JSON output through the current Gemini `generateContent` interface. The configured model defaults to `gemini-3.8-flash`; the model name remains configurable. News is treated as an event stream rather than raw sentiment and is attached to later learning/outcome tracking.

## Safety

`live_enabled` defaults to false. A 50 EUR account is a first-class case: minimum order cost, available margin, reserve and risk-based sizing can legitimately produce NO TRADE rather than an invalid or oversized order.

The integration has no web dashboard and no hidden trading loop. Home Assistant is the configuration and status surface; structured events are persisted to SQLite.

## Tests

`pytest` covers cryptographic signing, analytics/regimes, risk gates, calibration, walk-forward validation, no-order-spam behavior and an end-to-end simulated trading cycle. CI additionally compiles all modules, lints with Ruff and validates JSON.

## External API notes

Kraken Spot private requests are signed using the documented API-Key/API-Sign SHA256 + HMAC-SHA512 scheme. Kraken Futures v3 uses the documented `APIKey`/`Authent` flow. Client order identifiers are UUIDs so order intent identity survives ambiguous network responses and reconciliation.
