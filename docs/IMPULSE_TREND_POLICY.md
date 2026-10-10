# Impulse entries and trend-following exits (v0.1.43)

## Which trading model owns what?

The Tactical Trader owns short-horizon impulse entries and rapid trade management. The core trader remains a slower, cost-aware portfolio allocation/rebalancing strategy. Tactical's signal is a technical impulse score assembled from short-horizon momentum, breakout, realized volatility, volume expansion and order-book imbalance. Its confidence is a rule-derived score, not a calibrated probability that a forecast will be correct; Gemini/news context can filter the setup but does not turn it into a proven price predictor.

## Entry strength and leverage

For Spot Margin, Tactical selects leverage from Kraken's side-specific market metadata only: `leverage_buy` for a long and `leverage_sell` for a short. Leverage is bounded by the configured global cap, instrument cap and an immutable 5x ceiling. Weak long impulses remain unleveraged, medium/high-confidence longs may use supported 2x, and a strong impulse (confidence >= 0.90, score >= 200, net edge >= max(75 bps, 30% of estimated costs)) may use the highest supported level up to the global default cap of 3x. Spot Margin shorts continue to require at least 2x and explicitly supported sell-side leverage. Futures/other product types keep their prior 1x Tactical sizing path pending a separately tested contract-specific implementation.

`tactical_portfolio_pct` and `tactical_max_capital_eur` define the capital/margin budget. The order notional is capital budget multiplied by selected leverage, capped by `tactical_position_limit_pct`. The new default Tactical position cap is 80% of equity, but global risk gates still apply: gross exposure remains capped by the configured 80%, used margin by 35%, leverage by 3x by default, free margin and minimum margin level must pass, and daily-loss/drawdown checks remain active. Tactical's net exposure ceiling may reach its own configured position cap, while core keeps its independent 50% default net cap. Existing quote-cash checks still apply to unleveraged Spot longs; leveraged Spot Margin longs must pass the margin engine instead.

## Losses and exits

Tactical's adverse-price stop remains 1% by default. Once price gain reaches the configured 2.2% threshold it **arms trend-following** instead of forcing a close. The trailing profit floor is cost-aware: it estimates round-trip fees, max spread, slippage and safety buffer, then adds a 25 bps target cushion. A peak-to-price pullback stop is armed only after the peak covers that floor plus the configured trailing distance; if price fades through the cost floor sooner, it exits at that floor rather than accepting a planned negative net return. Once a position is in the trailing state, the 30-minute time limit no longer closes it by itself; the cost-aware trailing exit, a qualifying opposite signal or the 1% adverse-price stop can still close it. The target does not guarantee the exact fill price; fast moves, spread, latency or slippage can increase realized loss or giveback.

For core Spot Margin positions, a new 2% loss-versus-cost-basis stop takes priority over profit management and creates a full reduce-only exit. Existing core profit protection remains: realize a configurable 50% partial reduction at 10% position profit, retain a persistent profit high-water mark, and flatten on the configured 35% peak-profit giveback after 10% activation subject to the 5% profit floor. The normal core policy otherwise remains conservative and signal/cost-aware; core leverage does not scale with Tactical impulse score.

## Safety and validation

The strategy cannot promise a correct forecast or maximize the exact peak. Stops are decision rules, not guaranteed stop-market execution prices. High leverage amplifies adverse moves and can lead to rapid loss or liquidation, so live orders remain subject to Kraken capability, available collateral, risk limits, order preflight, the global live-enabled flag and kill switch. Fresh-install configuration still defaults to `live_enabled: false` and `kill_switch: true`.

Regression tests cover confidence-tiered leverage, side-specific support, exposure/margin sizing, Tactical trend continuation after the target threshold, core full exit on adverse PnL, legacy option migration and global risk gates. CI success validates code and packaging; it is not evidence of trading profitability.


## v0.1.42 operational correctness

- Multiple `OpenPositions` rows for one pair are aggregated for quantity, cost basis, PnL, and signed exposure. Wallet inventory remains distinguished from margin lots to avoid unintended double counting.
- A full Spot Margin flatten uses Kraken `ordertype=settle-position` with the documented `volume=0` sentinel to settle all matching margin positions in one exchange order, but only after a successful fresh OpenPositions read confirms an exchange margin position and its leverage. Aggregated open quantity remains available for audit, profit/loss and partial-reduction logic. Kraken's documented settlement side is sell for a short and buy for a long; this differs from the ordinary opposite-side trade used to reduce exposure. Partial reductions continue to use quantity orders.
- Forecast success is directional and measured after expected costs; legacy pending rows with unknown direction become `UNSCORABLE`. Rule-derived confidence is stored on the same scale used by the decision engine, capped to [0.01, 0.99] to avoid false certainty, and is not asserted as a calibrated probability.
- Confidence-scale promotion requires 100 observations, selects on an earlier chronological segment, validates on the newest 30%, and requires hold-out Brier improvement of at least 0.005.
- Tactical's trailing break-even floor includes estimated Spot Margin opening fees and completed four-hour rollover fees in proportion to the borrowed share. Rates remain estimates if exchange/instrument-specific values are unavailable.
- App-managed stops cannot guarantee a maximum fill loss across app/network outages. Live trading still requires explicit enablement and the kill switch to be disabled; CI is not evidence of trading profitability.


## v0.1.43 learning and execution architecture

- Core decision-policy tuning is isolated in a separate `strategy_policy` registry family. Legacy confidence-scale candidates from earlier releases remain stored for audit but are not applied to live confidence or target sizing.
- The optimizer uses independently grouped symbol/time buckets and a chronological 60/20/20 train/validation/test split. It tunes signal feature weights, news/Gemini weights, volatility-cost multiplier, spread/liquidity quality scales, confidence transformation scales, and edge/confidence/cost-ratio thresholds within bounded ranges. It cannot mutate leverage ceilings, account loss limits, kill switches, execution authority, or order mechanics.
- Candidate promotion requires at least 300 independent symbol/time groups, enough selected outcomes in both validation and test sets, positive net expectancy in both, at least 1 bps validation mean-net improvement, and at least 2 bps test mean-net improvement. These thresholds are safeguards, not a guarantee of future profitability.
- Both accepted and rejected directional signals are recorded once per symbol/direction/15-minute bucket. Labels use the observed directional price change minus the expected transaction costs. Missing price history remains pending only for a bounded period and then becomes unscorable.
- Tactical order timeouts no longer silently discard partial fills. The exact exchange order must be terminally reconciled after a cancellation request; cumulative filled quantity is applied to the local position only after confirmation. Unknown cancellation status remains a blocker.
- Stop-loss and trailing behaviour are still monitored by the running app, not guaranteed server-side conditional orders. Their parameter family should only be optimized after reliable high-frequency path and actual-fill records can replay the full exit path; this release deliberately does not tune those risk controls from incomplete trade summaries.


## Additional portfolio safety in v0.1.43

- Kraken's ordinary spot balances and margin-backed assets are distinct inventory sources. The reconciler stores wallet exposure and signed margin exposure separately, then combines both legs for portfolio gross/net values rather than silently skipping a margin position when the same symbol is present in the wallet.
- Strategy decisions manage the margin leg when an OpenPositions row exists, while risk checks replace that leg in aggregate gross/net exposure math and apply the per-symbol position limit to wallet plus margin exposure.
- New risk on Spot Margin-eligible instruments is blocked while OpenPositions reconciliation is unverified; Tactical may still attempt a locally known reduce-only exit. This prevents a temporary private API/read failure from being interpreted as a flat margin book.
