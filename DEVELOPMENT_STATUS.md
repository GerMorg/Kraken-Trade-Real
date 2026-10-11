# Kraken Trade Real — Development Status

Last reconciled against GitHub: 2026-10-11  
Repository: `GerMorg/Kraken-Trade-Real`  
Main version: **0.1.77**  
Main commit: `aabe554cf0b3e2b9a1e7eaa5492df39366b1a839`

This file is the durable continuation record for work split across chats/timeouts. Always verify the current `main` branch and open PRs before resuming; do not assume this file is newer than GitHub.

## Recently merged releases

All listed PRs were merged only after all five CI jobs passed.

- **0.1.74 — PR #79:** Fixed Spot fill-history sync. Kraken `TradesHistory` is paginated with `ofs`; the previous `with_cursor`, `cursor`, and `limit` arguments caused `EGeneral:Invalid arguments`. Sync progress is now durably resumed by offset.
- **0.1.75 — PR #80:** Added early exchange-minimum checks for new/increased orders. Emits `CYCLE_ORDER_BELOW_MINIMUM`; does not round orders up or bypass risk/cash limits. Reduce-only dust handling and Futures quantity semantics remain separate.
- **0.1.76 — PR #81:** Tactical cooldown is now a bounded, versioned learned parameter (30–600 seconds), hidden from HA options and protected against legacy `options.json` overrides. It increases after poor verified live outcomes and decreases cautiously only after improving outcomes. Hard trade-rate and risk caps remain immutable.
- **0.1.77 — PR #82:** Added numerical context and `failed_checks` to `CYCLE_RISK_DECISION` so daily-loss, gross/net exposure, cash reserve, drawdown and position-limit blocks can be diagnosed from logs. Risk decisions themselves were not loosened.
- Obsolete PR #49 was closed as superseded by the newer adaptive-policy architecture.

## Findings from the supplied v0.1.56 log

The attachment contained 10,000 JSON log lines covering 87 completed cycles.

- `CORE_SPOT_FILL_HISTORY_SYNC_FAILED`: **86** occurrences, with `KRAKEN_PRIVATE:EGeneral:Invalid arguments`. Addressed in v0.1.74; must be confirmed with a fresh log from a newer installed version.
- `ORDER_BLOCKED`: 18 occurrences — 16 `MIN_ORDER_QTY`, 2 `DUPLICATE_OPEN_ORDER`. The early minimum-order gate was added in v0.1.75. Fresh logs should confirm that these no longer become futile order attempts.
- `ORDER_REJECTED_EXCHANGE`: 32 occurrences of `EOrder:Insufficient funds`. Current main has Spot available-funds preflight and per-cycle quote reservations from later releases, but the v0.1.56 log predates those changes. Do not claim the live issue is resolved until newer logs confirm it.
- Risk decisions included many `daily_loss` and `gross_limit` blocks. These are hard safety gates and must **not** be relaxed automatically. v0.1.77 now logs the actual PnL/limit/headroom and projected exposure needed to determine whether a block is correct.
- Gemini model fallback occurred 40 times and all 87 cycles completed. This log does not support a claim that Gemini fallback stalled the cycle.
- Learning reported no eligible realized Core outcomes and no validated Tactical exit paths. The fill-history defect was a major blocker to verified outcome learning. Confirm data collection and optimizer eligibility using a post-v0.1.74 log.

## Current adaptive-policy scope

- Normal/Core: internally versioned entry thresholds, market-liquidity/spread filters, and path-based exit-policy parameters; regime-specific confidence calibration.
- Tactical: internally versioned entry filters, allocation within its existing bound, path-replayed stop/target/trailing/hold/reversal exits, and cooldown.
- Learned policy values are bounded and stale HA option values are ignored.
- Immutable safety limits remain outside learning: daily loss, drawdown, leverage, gross/net exposure, cash reserve, maximum positions and trade-rate ceilings. Credentials, live/kill-switch controls and feature toggles also remain explicit configuration.
- **Not complete:** transaction-cost assumptions remain visible and static, including fee estimates, execution overhead/slippage assumptions, FX funding costs and margin opening/rollover costs. They must only be hidden after a reliable estimator is implemented from verified fills/ledger evidence, separated by strategy/venue and protected by conservative bounds and minimum sample gates.

## Next work packages

1. **Validate v0.1.77 in a fresh live log.** Confirm offset-based fill sync completes, actual verified outcomes accumulate, order minimums are skipped before submission, Spot funds are budgeted correctly, and risk blocks include numeric evidence. Do not tune hard risk gates based on the old v0.1.56 log.
2. **Build verified execution-cost calibration.** Derive only from ledger-verified fills with known quote-currency fees and valid order attribution. Keep Core/Tactical and Spot/Margin/Futures scopes separate where the evidence supports it; use minimum samples, robust/conservative statistics, bounded changes and audit history. Hide corresponding HA options only after the runtime actually consumes the learned values.
3. **Audit order lifecycle and portfolio behavior** from fresh logs: duplicate-order prevention, fill/partial-fill reconciliation, rebalancing deltas, held-position exit behavior, and Tactical partial-profit/trailing behavior.
4. **Re-check Austrian tax reporting and Gemini fallback** against current code and fresh logs. Do not assume old-version defects remain or that they are fixed without evidence.
5. Continue one small branch/PR at a time; bump every app version consistently; merge only after all five CI jobs are green.

Profitability is not guaranteed; learning should only promote bounded policies when there is sufficient verified evidence.
