# Documentation

The runtime is intentionally GUI-free. Home Assistant is the configuration and operations surface; the app publishes status and diagnostics via sensors and structured logs.

Persistent data includes cycle state, instruments, market/features, news/Gemini analysis, decisions/orders/fills, portfolio reconciliation, learning, calibration, model registry and recovery history.


## Tax report access

Austrian tax reports are written to the app's user-accessible addon configuration folder:

`/config/reports/tax/AT/<year>/`

The files are:
- `income-tax-report.json`
- `tax-events.csv`
- `income-tax-information.md`

Because `addon_config` is mapped to Home Assistant's per-app configuration directory, the same files are visible under Home Assistant's `/addon_configs/<repo-hash>_kraken_trade_real/reports/tax/AT/<year>/` path. The exact repository hash is assigned by Home Assistant.


## Market data flow

The runtime evaluates the complete discovered Kraken instrument set through the bulk ticker prefilter. Historical candles are then fetched only for the configured top candidate subset and persisted in the market-history cache. Cached history is reused until `market_history_cache_seconds` expires.

Immediately before decisions, the runtime refreshes the bulk ticker so price, bid and ask are current. Order books are fetched only for the final selected instruments.

Default values in 0.1.8 are 200 history candidates and a 900-second history cache lifetime.
