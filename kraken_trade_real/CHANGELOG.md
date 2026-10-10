# Changelog

## 0.1.42

- Aggregate all Kraken OpenPositions lots per pair for quantity, PnL, cost basis, exposure and leverage instead of silently dropping later lots.
- Use Kraken's native Spot Margin `settle-position` order for confirmed full exits with the aggregate Kraken-reported open quantity; strictly validate this reduce-only path and avoid irrelevant minimum-cost/FX-funding checks.
- Score forecasts using their explicit LONG/SHORT direction and net directional outcome after expected costs; preserve regime and raw confidence, and cap the heuristic score away from false certainty.
- Exclude legacy forecasts with unknown direction from calibration and require at least 100 outcomes plus chronological hold-out validation before promoting a confidence scale.
- Include time-dependent Spot Margin opening and completed four-hour rollover costs in Tactical's trailing profit floor.
- Synchronize Home Assistant app, Python package, Docker build and API User-Agent to 0.1.42.


## 0.1.39

- Raise the Spot Margin partial take-profit threshold from 3.5% to 10% of Kraken-reported opening cost; the configured reduction remains 50% of the then-open position.
- Activate the persistent trailing-profit lock at the same 10% peak threshold so the old 5% full-exit rule cannot close the entire position before the intended partial take-profit; retain the 35% peak giveback and set the minimum locked-profit floor to 5%.
- Migrate the untouched v0.1.38 default option triplet (3.5% partial / 5% trailing / 2% floor) to the new 10% / 10% / 5% policy at runtime, and emit an audit event; preserve customized triplets.
- Add regression tests for the 10% trigger, trailing interaction, legacy migration and custom-option preservation.
- Bump Home Assistant app, Python package, Docker build and Kraken User-Agent to 0.1.39.

## 0.1.38

- Add exchange-reported Kraken Spot Margin PnL, quote-currency-converted PnL in EUR, cost-basis profit percentage and open quantity to portfolio reconciliation, with explicit audit events and a successful-read flag.
- Add persistent partial take-profit and profit-lock state: reduce 50% of a margin position after 3.5% profit on exchange cost basis, then trail the peak after 5% activation with a 35% giveback and 2% profit floor. These are configurable Home Assistant options.
- Prevent the core sizing strategy from immediately rebuilding an asset's exposure after a partial profit has been taken; preserve ordinary signal-driven reductions and full exits.
- Cancel an aged reduce-only resting order after execution_order_timeout_seconds, only for exchange-identified orders, and require an exchange-confirmed terminal state. Block re-submission in the same cycle to ensure a fresh portfolio snapshot is used; record partial fills instead of retaining them as live blockers.
- Add regression tests for profit high-water marks, partial-profit state/re-entry prevention and stale reduce-only order cancellation.
- Bump Home Assistant app, Python package, Docker build and Kraken User-Agent to 0.1.38.

## 0.1.37

- Deduplicate Tactical entry candidates by canonical base asset across EUR/USD markets, rank quote pairs by spread, volatility and EUR-normalized 24-hour turnover, and log the selected market plus discarded alternatives for each duplicated base.
- Preserve every open Tactical position's instrument in the live WebSocket subscription even when it falls outside the current market-ranking window.
- Keep Tactical's instrument map in actual rank order; report the complete stream symbol list and distinguish entry candidates, held-base exclusions, exact duplicate rows and quote-pair deduplication.
- Block duplicate Tactical exposure when the same base asset is already held under another quote symbol, including a final entry-time guard.
- Centralize exact Kraken legacy asset aliases across portfolio reconciliation, the regular scanner and Tactical; prevent generic X/Z prefix stripping from corrupting genuine tickers such as ZBCN/ZETA; add regression tests for alias normalization, quote-pair deduplication, rank order, and open-position monitoring.
- Correct Tactical Home Assistant status precedence so a disabled module is reported as DISABLED even when shadow mode is configured.
- Bump Home Assistant app, Python package, Docker build and Kraken User-Agent to 0.1.37.

## 0.1.36

- Keep the Tactical Kraken WebSocket connection alive when the candidate set rotates; unsubscribe removed symbols and subscribe added symbols incrementally instead of reconnecting every cycle.
- Add regression tests ensuring only delta symbols are sent and an unchanged candidate set produces no wire messages.
- Bump Home Assistant app, Python package, Docker build and Kraken User-Agent to 0.1.36.


## 0.1.35

- Fix cycle failures caused by float-valued configuration leaking through `LeverageEngine.choose` into the Decimal-only margin cost model.
- Normalize numeric inputs at the CostModel boundary and convert leverage/risk inputs to Decimal before min/max selection so order and cost calculations remain type-consistent.
- Add regression tests for float leverage, float cost configuration and Decimal return types.
- Bump Home Assistant app, Python package, Docker build and Kraken User-Agent to 0.1.35.


## 0.1.34

- Fix Spot Margin leverage selection to use Kraken's direction-specific leverage_buy/leverage_sell values; any new or increased Spot Margin short must select a sell-supported leverage even if the portfolio already has a short.
- Keep cash Spot reductions in the base-wallet balance and force leverage 1 when selling an existing long token balance, rather than accidentally treating that sale as a new margin short.
- Resolve full Spot cash exits from the available base quantity when EUR-notional sizing is rounded below an exchange minimum; classify tiny partial rebalance deltas separately from genuinely unorderable dust holdings.
- Add regression tests for direction-specific margin leverage and W/EUR minimum-size/quantity handling.
- Bump Home Assistant app, package, Docker build and Kraken User-Agent version to 0.1.34.


## 0.1.33

- Match order-type payloads to Kraken's documented contracts: market orders omit limit-price fields; Spot limit orders require a valid price; Futures lmt/post/ioc/fok orders require limitPrice, and unrecognized/missing Futures sendStatus is kept in reconciliation rather than marked accepted.
- Treat Futures sendStatus rejection/cancellation as terminal responses instead of converting result=success into an acknowledged order.
- Correct EUR/USD quote funding: EUR/USD is USD per EUR, so acquiring USD sells EUR at the bid, while acquiring EUR buys EUR at the ask. Use the correct source-currency amount and refresh balances after a position liquidation before conversion.
- Calculate position-funded FX bridge quantities in the position quote currency, reserve sale/conversion cost headroom, and enforce exchange minimum quantity/notional checks before funding orders.
- Normalize Spot sizes to AssetPairs lot_decimals and limit prices to instrument ticks; preserve legitimate zero precision values during discovery.
- Add regression tests for market-order price omission, required limit parameters, Futures rejected sendStatus, FX direction/bridge funding and precision handling.
- Bump Home Assistant app, package, Docker build and Kraken User-Agent version to 0.1.33.

## 0.1.32

- Prioritize an explicit held-position exit over position-size targeting; stale positions target zero rather than retaining unintended residual exposure, while negative-edge telemetry remains compatible.
- Require held positions to clear the configured expected-return/cost ratio rather than retaining risk with a barely positive edge.
- Track exchange-reported leverage for Spot Margin and Futures positions; use position-specific leverage for carry assumptions and Spot Margin short closes when available.
- Raise the legacy generic margin-fee fallback from 2 to 4 basis points for opening and rollover assumptions, while preserving deliberately supplied non-legacy rates. Kraken's displayed rate is asset-specific and may be dynamic; these estimates are logged as assumptions, not exchange-confirmed charges.
- Estimate Spot Margin rollover carry per direction using the same pre-trade leverage selector as execution, identify the assumption source in logs, and do not apply Spot Margin rollover assumptions to derivatives.
- Keep below-minimum residuals open and auditable, explicitly logging that no close order was sent and the residual was not marked closed.
- Enable Tactical by default on a fresh install in live-intent mode while global live trading remains disabled and the global kill switch remains enabled. Existing Home Assistant options remain preserved and must be reviewed after upgrade.
- Bump Home Assistant app, package, Docker label, and Kraken User-Agent to 0.1.32.

## 0.1.31

- Close a held position when its current-direction signal no longer clears the configured edge/confidence policy or expected return no longer covers estimated costs; do not keep risk open merely because its net edge remains slightly positive.
- Keep the existing staged reversal behavior and validated reduce-only risk path.
- Add regression coverage for a short whose positive net edge falls below the economic entry floor.
- Bump Home Assistant app, package, and Kraken User-Agent versions to 0.1.31.


## 0.1.30

- Resolve Kraken Spot Margin position aliases (for example `MINAUSD`) to canonical discovered symbols so held positions are included in every market-selection and reevaluation cycle.
- Include conservative expected Spot Margin opening and four-hour rollover costs in Core strategy edge calculations; expose the assumed leverage, holding horizon, and cost in structured logs.
- Expose the expected holding horizon, margin opening-fee estimate, and four-hour rollover estimate as Home Assistant options; the configured rates are estimates unless Kraken supplies an instrument-specific rate.
- Set Spot order margin funding from the actual leverage level, not merely the instrument's margin capability; cash Spot reductions are no longer sent as invalid reduce-only margin orders.
- Preserve the Spot Margin path when closing existing shorts, allow validated reduce-only exits through entry-only loss/drawdown/exposure/margin gates, and reject any reduce-only target that flips or increases exposure.
- Add regression tests for symbol alias resolution, time-based financing estimates, and reduce-only order arguments.

# Changelog

## 0.1.29

- make Core entries cost-aware with configurable Kraken fee assumptions and multi-horizon momentum (5m/15m/60m/240m)
- add a controlled adaptive Core edge tier for high-confidence setups while preserving hard economic and risk gates
- correct Core direction eligibility so unavailable exchange-side shorts are filtered before risk
- allow supported Spot Margin shorts when actual margin/market hard guards pass instead of rejecting normal volatility by an overly strict leverage heuristic
- keep Spot Margin closing transactions as explicit reduce-only orders; reserve `settle-position` for true margin settlement flows
- warm-start Tactical WebSocket price history from existing REST candles so newly rotated candidates do not spend the first minutes in data warm-up
- add Tactical adaptive entry economics and explicit per-candidate evaluation diagnostics

## 0.1.28

- Correct Tactical short-position accounting so exposure, margin checks, minimum-cost checks and realized P&L always use absolute notional with direction stored separately.
- Repair legacy 0.1.27 Tactical short positions persisted with a negative notional.
- Add a conservative Spot Ticker fallback for Tactical position management when the WebSocket price becomes stale.
- Reconcile filled Tactical orders after restart and restore missing Tactical positions from the exact Kraken order ID and persisted decision context.
- Keep Tactical long/short execution isolated behind the existing Trading Authority; short entry remains Spot Margin-only and uses exchange-reported sell-side leverage availability.
- Bump application, package, Home Assistant and Kraken User-Agent versions to 0.1.28.

## 0.1.25

- Fix Spot order reconciliation to use the persisted Kraken transaction/order ID with QueryOrders instead of the unsupported cl_ord_id query argument.
- Preserve ambiguous state when a historical order has no Kraken order ID; only an exact open-order match can resolve it safely.
- Pass persisted Kraken order IDs through stale-order and preflight reconciliation.
- Add regression coverage for the Kraken Spot reconciliation query contract.

## 0.1.24

- Replace the previous non-EUR quote-currency blocker with automatic quote funding for Spot trades.
- When a Spot LONG needs USD and the available USD balance is insufficient, the runtime now executes a separate EUR/USD market conversion first and only continues after Kraken confirms the conversion as filled/partially filled.
- FX conversion orders are persisted, audited, participate in the daily order limit, and reserve an order slot so the dependent trade is not allowed to consume the final slot.
- The EUR/USD conversion includes a configurable 40 bps cost reserve by default; when conversion is required this cost is added to the trade's expected cost before the strategy/risk edge gate.
- After the FX fill the portfolio is reconciled again and the dependent USD trade is checked against the actual refreshed USD balance.
- FX failures remain safe blockers; the main trade is never submitted against unconfirmed funds.
- Add regression coverage for automatic EUR/USD funding and FX cost accounting.

## 0.1.23

- Fix Spot-Margin USD short execution: a new short position on a margin-capable Spot pair must use an actual Kraken margin leverage level; the runtime no longer silently falls back to an unleveraged Spot sell that can fail with insufficient funds.
- Reconcile Kraken Spot margin health from TradeBalance and route the correct Spot/Futures margin account into leverage and risk evaluation.
- Track per-asset Spot cash so non-margin USD quote purchases are blocked locally when the required USD quote balance is unavailable, instead of reaching Kraken and failing there.
- Reconcile all still-open order states, including ACKNOWLEDGED/LIVE/PARTIALLY_FILLED, so stale acknowledged orders cannot permanently block rebalancing.
- Interpret Kraken QueryOrders status "closed" using executed volume so completed orders become FILLED and partially executed closed orders become PARTIALLY_FILLED.
- Prevent automatic learning promotion when the candidate's absolute calibration quality remains poor despite relative improvement.
- Add regression tests for USD margin payloads, order reconciliation and calibration quality gates.
- Bump application/package/Home Assistant add-on and Kraken User-Agent version to 0.1.23.

## 0.1.22

- Fix a position-management deadlock where held positions were evaluated but discarded by the new-entry MIN_EDGE gate before a reduce-only decision could be created.
- Add an explicit negative-edge exit path: when an existing long/short position has non-positive net edge, the strategy creates a reduce-only flattening decision even if entry confidence/edge thresholds are not met.
- Allow reduce-only risk reduction to bypass entry-only edge and confidence gates while retaining all hard portfolio, drawdown, daily-loss, leverage, direction, minimum-cost and other safety limits.
- Prevent deterministically rejected orders from consuming the daily submission limit or one-minute direction cooldown after the exchange has confirmed rejection.
- Add regression tests proving negative-edge held positions are flattened and rejected submissions do not exhaust the daily order budget.
- Bump application/package/Home Assistant add-on and Kraken User-Agent version to 0.1.22.

## 0.1.21

- Fix slow Home Assistant startup caused by bulk instrument persistence running executemany() in SQLite autocommit mode, which committed each Kraken instrument separately.
- Persist the complete discovered instrument universe in one explicit SQLite transaction.
- Distinguish Kraken discovery completion from instrument database persistence in startup diagnostics and watchdog messages.
- Bump application/package/Home Assistant add-on and Kraken User-Agent version to 0.1.21.

## 0.1.20

- Make tokenized xStock discovery explicitly opt-in so EEA-incompatible API market-order-book paths cannot delay or block core Spot startup.
- Add a POSIX hard deadline around startup instrument discovery and persistence; a blocked synchronous Kraken call can no longer wait for the external watchdog before the application records a startup failure.
- Track and publish the exact startup instrument operation (`SPOT_ASSETPAIRS`, tokenized AssetPairs, or Futures instruments) involved in a failure.
- Bump application/package/Home Assistant add-on and Kraken User-Agent version to 0.1.20.

## 0.1.19

- Keep core startup dependent only on the documented default Spot AssetPairs request.
- Make tokenized xStock discovery and ticker retrieval best-effort with a bounded per-call timeout.
- Preserve the normal crypto universe when xStock API access is unavailable or times out.
- Publish explicit diagnostics for optional xStock market-data degradation instead of restarting the application.

## 0.1.18

- Align Spot margin orders with Kraken's pair-specific leverage rules by omitting `leverage=1` and validating supported buy/sell leverage values.
- Add explicit `asset_class=tokenized_asset` handling for xStock AssetPairs, Ticker, OHLC, Depth, and AddOrder requests.
- Normalize Futures order types to Kraken's `lmt`, `mkt`, and `post` values and remove the unsupported `postOnly` field.
- Replace the obsolete Futures candle request with Kraken's Charts API and normalize Futures instrument metadata and contract-unit sizing.
- Propagate post-only execution policy into submitted order intents.
- Add regression tests for all corrected Kraken argument paths.

## 0.1.17

- Generate Kraken Spot-compatible client order identifiers as UUIDs instead of the legacy `client_<32hex>` format, which exceeds Kraken's 18-character free-text limit.
- Reject invalid client order identifiers before any exchange submission so malformed identifiers cannot create new reconciliation gates.
- Automatically clear legacy `UNKNOWN_RECONCILING` orders carrying invalid client identifiers as deterministic rejected orders instead of repeatedly calling `QueryOrders` with invalid arguments.
- Add regression coverage for client-order-id validation, generation and legacy duplicate-gate cleanup.
- Bump application/package/User-Agent version to 0.1.17.


## 0.1.16

- Distinguish deterministic Kraken API/order rejections from genuinely ambiguous transport failures; deterministic rejections are stored as REJECTED and cannot poison the duplicate-open-order gate.
- Treat timeouts, transport failures, HTTP 5xx responses and invalid HTTP payloads as ambiguous so uncertain submissions remain conservatively reconciled.
- Reconcile ambiguous/submitting orders for the specific symbol again during preflight before applying DUPLICATE_OPEN_ORDER, so stale gates cannot survive merely because the cycle-wide reconciliation backlog was limited.
- Expand stale-order reconciliation coverage from 3 to 20 records per cycle and expose detailed exchange error diagnostics.
- Add regression coverage for deterministic exchange rejection and transport ambiguity classification.
- Bump application/package/User-Agent version to 0.1.16.

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

## 0.1.26

- reduce-only exits no longer require positive entry alpha
- repeated dust-position exit attempts are suppressed and diagnosed explicitly
- Gemini model cooldown avoids retry storms after quota/model failures
- log-driven execution and rebalancing diagnostics improved


## 0.1.27

- added isolated Tactical Volatility/Momentum/Breakout strategy alongside the existing Core strategy
- added fast Kraken WebSocket v2 ticker, trade and level-2 book data for tactical decisions
- added separate tactical capital, position, trade-frequency, daily-loss and execution-cost limits
- added Shadow mode by default so the new strategy can collect forward results without submitting live orders
- added immediate long and short tactical capability; Spot Margin uses exchange-reported short availability and the configured minimum supported leverage
- added take-profit, stop-loss, trailing-stop, time-stop and signal-reversal exits
- added persistent tactical positions and trade results plus dedicated Home Assistant sensors and audit events
- synchronized core and tactical order submission to prevent concurrent authority races
