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
