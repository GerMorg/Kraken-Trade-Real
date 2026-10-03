# Changelog

## 0.1.7

- Expose Austrian tax reports through Home Assistant's user-accessible `addon_config` directory.
- Fix the HA tax sensor to publish the actual indicative 27.5% report amount.
- Reduce market-cycle API work by filtering from the bulk ticker first, fetching historical candles only for fast candidates, and order books only for the selected instruments.
- Fetch candidate history/order books concurrently with bounded workers and publish explicit market-stage diagnostics.
- Add explicit no-action reasons (`MIN_EDGE`, `MIN_CONFIDENCE`, `MINIMUM_COST`) to cycle AppLogs without changing trading thresholds.
- Make the scan interval start-to-start rather than adding a full sleep after every cycle.

## 0.1.6

- Prevent a tax-report KeyError from breaking startup or later runtime processing.
- Isolate learning-feedback failures so the autonomous trading cycle continues and logs the exact failure.
- Add explicit cycle-stage AppLogs and top-level runtime protection against silent process termination.
- Make tax-report rendering and persisted tax rows tolerant of missing optional fields.

## 0.1.5

- Bump the Home Assistant app version so Supervisor detects and offers the current main build.
- Keep the Spot nonce and autonomous learning fixes from 0.1.4 as the current release baseline.

## 0.1.4

- Disable Kraken Futures by default and require separate Futures credentials when explicitly enabled.
- Keep Spot market discovery, tickers and portfolio reconciliation independent of Futures.
- Validate read-only Spot API access independently of live-trading permissions.
- Preserve Kraken private API error details and report Home Assistant sensor publication results.

## 0.1.3

- Start through `with-contenv` so Supervisor-provided environment variables, including `SUPERVISOR_TOKEN`, reach the Python runtime.
- Remove the unsupported Futures `status` startup request that produced HTTP 404.

## 0.1.2

- Publish startup and degraded runtime states to Home Assistant sensors.
- Keep trading cycles blocked while startup is degraded.
- Add actionable Kraken and network error details to AppLogs.
- Send explicit JSON Accept and User-Agent headers to Kraken.

## 0.1.1

- Fix AppArmor rules for S6-Overlay startup.

## 0.1.0

- kompletter unabhängiger Neubau
- Home Assistant App Struktur gemäß aktueller Supervisor-Dokumentation
- zentrale Trading Authority
- dynamische Spot-/Margin-/Derivatives-Instrumenterkennung
- Long/Short-/Leverage-/Margin-Risk Layer
- kostenbewusste Execution
- News- und Gemini-Integration
- Post-Trade Learning und Calibration
- Reconciliation und Recovery
- HA-Sensoren und strukturierte Logs
- keine Web-GUI und keine KTKI-Legacy-Abhängigkeit
