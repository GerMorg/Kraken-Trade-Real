# Impulse entries and trend-following exits (v0.1.40)

## Which trading model owns what?

The Tactical Trader owns short-horizon impulse entries and rapid trade management. The core trader remains a slower, cost-aware portfolio allocation/rebalancing strategy. Tactical's signal is a technical impulse score assembled from short-horizon momentum, breakout, realized volatility, volume expansion and order-book imbalance. Its confidence is a rule-derived score, not a calibrated probability that a forecast will be correct; Gemini/news context can filter the setup but does not turn it into a proven price predictor.

## Entry strength and leverage

For Spot Margin, Tactical selects leverage from Kraken's side-specific market metadata only: `leverage_buy` for a long and `leverage_sell` for a short. Leverage is bounded by the configured global cap, instrument cap and an immutable 5x ceiling. Weak long impulses remain unleveraged, medium/high-confidence longs may use supported 2x, and a strong impulse (confidence >= 0.90, score >= 200, net edge >= max(75 bps, 30% of estimated costs)) may use the highest supported level up to the global default cap of 3x. Spot Margin shorts continue to require at least 2x and explicitly supported sell-side leverage. Futures/other product types keep their prior 1x Tactical sizing path pending a separately tested contract-specific implementation.

`tactical_portfolio_pct` and `tactical_max_capital_eur` define the capital/margin budget. The order notional is capital budget multiplied by selected leverage, capped by `tactical_position_limit_pct`. The new default Tactical position cap is 80% of equity, but global risk gates still apply: gross exposure remains capped by the configured 80%, used margin by 35%, leverage by 3x by default, free margin and minimum margin level must pass, and daily-loss/drawdown checks remain active. Tactical's net exposure ceiling may reach its own configured position cap, while core keeps its independent 50% default net cap. Existing quote-cash checks still apply to unleveraged Spot longs; leveraged Spot Margin longs must pass the margin engine instead.

## Losses and exits

Tactical's adverse-price stop remains 1% by default. Once price gain reaches the configured 2.2% threshold it now **arms trend-following** instead of immediately closing the entire position. The position stays open until a trailing-stop pullback, a qualifying opposite signal, the time stop or the adverse-price stop fires. The trailing exit is based on the observed peak/trough and does not guarantee the exact fill price; a fast move, spread, latency or slippage can increase realized loss or giveback.

For core Spot Margin positions, a new 2% loss-versus-cost-basis stop takes priority over profit management and creates a full reduce-only exit. Existing core profit protection remains: realize a configurable 50% partial reduction at 10% position profit, retain a persistent profit high-water mark, and flatten on the configured 35% peak-profit giveback after 10% activation subject to the 5% profit floor. The normal core policy otherwise remains conservative and signal/cost-aware; core leverage does not scale with Tactical impulse score.

## Safety and validation

The strategy cannot promise a correct forecast or maximize the exact peak. Stops are decision rules, not guaranteed stop-market execution prices. High leverage amplifies adverse moves and can lead to rapid loss or liquidation, so live orders remain subject to Kraken capability, available collateral, risk limits, order preflight, the global live-enabled flag and kill switch. Fresh-install configuration still defaults to `live_enabled: false` and `kill_switch: true`.

Regression tests cover confidence-tiered leverage, side-specific support, exposure/margin sizing, Tactical trend continuation after the target threshold, core full exit on adverse PnL, legacy option migration and global risk gates. CI success validates code and packaging; it is not evidence of trading profitability.
