# Changelog

## 0.1.15

- Reconcile stale `SUBMITTING` and `UNKNOWN_RECONCILING` submissions against Kraken before new orders are evaluated; ambiguous states are never cleared by age alone.
- Treat a successful exact exchange lookup with no matching order as an exchange-confirmed no-order result, while lookup failures remain conservatively blocked.
- Broaden historical market hydration with a rotating exploration pool outside the core volume-ranked candidates.
- Add product-family-aware detailed selection so available XStocks, stocks, derivatives and crypto markets are represented during each cycle without exceeding the detailed-market cap (held positions remain preserved).
- Add explicit universe breakdown and exclusion diagnostics from discovery through ticker, prefilter, history and detailed selection stages.
- Add expected-return, expected-cost, net-edge and confidence diagnostics for every strategy rejection so `MIN_EDGE` blocks are explainable.
- Add configurable reconciliation and market-exploration limits and update the application/package/User-Agent version to 0.1.15.

## 0.1.14

- Count daily order limits and cooldowns from real submission attempts only, not from locally created or rejected order intents.
- Record submitted_at only when execution enters the exchange-submission state and treat ambiguous submission state as an open order.
- Add regression coverage for false daily-limit and cooldown blocks.
- Deduplicate Spot EUR/USD quote pairs for the same base asset during detailed market selection while always preserving held positions.
- Add a total deadline to Home Assistant sensor publication.
- Add a runtime watchdog with stage-specific deadlines and heartbeat diagnostics; a wedged process fails closed so Home Assistant can restart it.
- Recover stale RUNNING cycles after a process restart and expose explicit watchdog/step diagnostics.
- Bump application/package/User-Agent version to 0.1.14.

## 0.1.13

- Add a hard POSIX watchdog around Gemini client initialization and model requests so a stuck SDK/transport cannot block the trading cycle indefinitely.
- Emit explicit Gemini client-init start, ready, timeout and failure diagnostics.
- Treat Gemini client-init failures as zero-impact degraded operation so market analysis, risk, execution and learning continue normally.
- Add regression tests for blocking Gemini client initialization and model calls.

## 0.1.12

- Fix OrderIntent persistence SQL placeholder mismatch that could abort a cycle immediately before real order submission.
- Add a regression test covering complete OrderIntent persistence including margin, leverage, reduce-only and post-only fields.
- Update the Kraken User-Agent and Home Assistant/package version to 0.1.12.

## 0.1.11

- Add automatic Gemini model fallback for quota exhaustion, rate limits, timeouts and model availability failures.
- Try the configured primary Gemini model followed by stable Flash/Flash-Lite fallbacks without changing trading decisions or authority.
- Add detailed Gemini model-attempt, quota, fallback and exhaustion diagnostics.
- When every Gemini model fails, return zero AI market impact and continue the normal trading, risk and learning cycle instead of stopping.

## 0.1.10

- Bound Gemini model requests with a configurable 30-second client/request timeout.
- Disable Gemini retry loops for cycle-critical calls so a single API/network problem cannot stall a complete cycle.
- Add explicit Gemini request start, completion, timeout/error and duration diagnostics.


## 0.1.9

- Make News refresh cache-aware so the 10-minute refresh interval is actually respected.
- Fetch news sources concurrently with a bounded per-cycle timeout and explicit per-source diagnostics.
- Reconcile normal Spot balances into EUR-valued portfolio positions instead of only relying on margin OpenPositions.
- Count EUR, USD and other supported fiat balances toward the EUR cash reserve through live FX conversion.
- Carry currently held positions into detailed market analysis every cycle, even when they fall outside the top-20 scan ranking.
- Add position-aware target sizing, safe flatten-before-reverse behavior, and reduce-only rebalancing.
- Convert EUR trade notionals into the instrument quote currency before calculating order quantity, including USD pairs.
- Publish prediction lifecycle, learning progress, portfolio-symbol and FX diagnostics to AppLogs and Home Assistant sensors.
- Add explicit risk-decision and prediction-created AppLogs for every generated decision.

## 0.1.8

- Prevent long history enrichment from making every snapshot fail the 30-second freshness gate.
- Reuse persisted hourly history between cycles and refresh it only after the configured cache lifetime.
- Limit history enrichment to the automatically prefiltered top markets before detailed ranking.
- Refresh the bulk ticker immediately before decisions so execution uses current bid/ask/last prices.
- Keep full market discovery and automatic prefiltering while reducing unnecessary per-market API work.
- Add explicit history-cache, quote-refresh and history-quality diagnostics to cycle AppLogs.
- Remove the duplicate cycle market-scan log entry.

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
