# Development Status

## Release candidate — v0.1.86 (audit implementation)

- **Candidate branch:** release/v0.1.86-core-tactical-audit
- **Base:** main v0.1.85, commit c1d629f6d14273d71fcb7f968dda3576b10bf826
- **Status:** implementation and regression tests in progress; do not install as a release until all required CI jobs are green and the release PR is merged.
- **Risk and execution:** provisional portfolio, cash and free-margin reservations across same-cycle orders; reductions do not release capacity before exchange reconciliation; post-order cash-reserve logic covers long and short increases.
- **Tactical lifecycle:** recover OPEN and TRAILING positions, fail closed on invalid exit quotes and stale/future timestamps, reserve slots for unresolved entries, and filter LIVE-only risk accounting.
- **Market data and Core strategy:** reject non-finite and crossed quotes/depth; stabilize volatility horizon; block new entries with insufficient history and avoid forced exits caused solely by absent history.
- **Learning provenance:** estimated Tactical fees/funding are explicitly marked unverified. The live learner accepts only rows marked ledger-verified; existing estimated-only rows remain excluded. Automated verified-outcome attribution for every Tactical episode remains a follow-up before enabling live parameter learning from those episodes.
- **Risk accounting:** flow-normalized daily PnL/high-water drawdown is used only when the deposit/withdrawal ledger can be completely read and every external flow is valued in EUR. If ledger permissions/history are incomplete or a non-EUR transfer is present, the app logs the limitation and keeps the raw-equity basis.
- **Futures:** reconciler distinguishes long and short sides; reduce-only stop/partial/trailing actions use price-return PnL only when the open position maps to an instrument and a valid close-side quote is available. This is application-cycle protection, not a guaranteed exchange-native stop order.
- **Version alignment:** add-on manifest, app package, Docker build version and User-Agent are set to 0.1.86.
- **Validation:** pending CI for this candidate; green CI is a software-quality gate, not proof of trading profitability. Installed Home Assistant version cannot be verified from repository state alone.

---

# Kraken Trade Real — Development Status

Last reconciled against GitHub: 2026-10-12  
Repository: `GerMorg/Kraken-Trade-Real`  
Main version: **0.1.83**  
Latest runtime release commit (v0.1.80): `cbf653a4e9c3659f8df9f075025ba1758b8f5c20`

This file is the durable continuation record for work split across chats/timeouts. Always verify the current `main` branch and open PRs before resuming; do not assume this file is newer than GitHub.

## Historical notes — v0.1.83

- Tactical live entries now emit a dedicated fill-confirmed event showing BUY/SELL, position direction, filled quantity/price, notional, signal edge/cost and client order ID. Exit events now include order side, entry/exit prices, quantity, exit reason and estimated net result.
- `CYCLE_COMPLETED.orders` is explicitly marked `CORE_STRATEGY_ONLY`; Tactical runs independently and must be followed through its own events.
- The trailing regime only arms once the estimated cost floor plus trailing distance is reached, avoiding a lower nominal target disabling the time stop before the trade is cost-covering.
- PR #96 merged as `35a2edf7ae9198049a164a31f84e3f74e0ccb87c`; all five PR checks passed. Post-merge main CI is being verified. No risk limits were relaxed.

## Previous release — v0.1.82

- Adds explicit `ENTRY_FEE_NOT_QUOTE_DENOMINATED` / `EXIT_FEE_NOT_QUOTE_DENOMINATED` counts when an exchange-ledger fee is verified but paid in a non-quote currency. This closes a diagnostic blind spot for estimate-only realized outcomes.
- PR #94 merged as `cc28cf5435626af10fa4f0e242defa5b059c31bb`; all five PR checks passed. The post-merge main CI is being verified. No risk gate or order behavior changes.

## Previous release — v0.1.81

- Adds `PORTFOLIO_DRAWDOWN_BASIS` diagnostics for raw high-water-mark equity, peak timestamp, snapshot count, drawdown, and explicit `cash_flow_adjusted: false`. It does not change the 15% drawdown gate; cash-flow normalization is not yet implemented.
- Realized attribution rebuild now reports reason counts for fill anomalies and estimate-only net outcomes, making missing fee evidence, quote mismatches and margin/rollover verification gaps diagnosable from logs.
- PR #92 merged as `ed0bcf2dce2cc191087a682f25cd2c81bdf6bcd8`; all five PR checks and all five post-merge main checks passed. No live-trading risk limits were loosened.

## Previous release — v0.1.80

- Scope: wire Tactical live Spot-long entries to the same audited FX quote-funding manager used by Core, so EUR cash can fund a risk-approved USD-quoted entry where the guarded conversion rules allow it.
- Tactical funding now occurs only after the initial risk decision allows the entry. If a conversion occurs, portfolio balances are reconciled and risk is checked again before order submission. Spendable quote balance and shared fee/slippage/safety reserves are used; funding or reconciliation uncertainty fails closed.
- The v0.1.79 log supplied on 2026-10-12 spans 2026-10-12 07:26:39–14:43:54 UTC (the 10,000-line excerpt covers about 7h17m). It contains 88 Core cycles and no Core orders. Of 840 Core risk decisions, 753 were rejected by the 15% drawdown gate; this release deliberately does not bypass that protection.
- The same log showed three Tactical signals blocked because spendable USD was only about 1.02 USD against a roughly 58 USD order requirement. The FX-funding path addresses this independent Tactical funding gap, but it will not create trades while the global drawdown gate rejects them.
- Portfolio snapshot in the excerpt showed about €206.5 equity, about €204.1 cash, and only one tracked open Spot position AAVE/EUR of about €2.4, with a dust amount below Kraken's minimum orderable quantity. This log alone does not prove that any other exchange-side margin/futures positions are absent; check separate margin reconciliation evidence if such positions are expected.
- Fill sync completed each cycle but inserted/matched zero fills in the sampled windows; the realized attribution rebuild repeatedly reported 13 anomalies, 16 estimated-only outcomes, and 0 verified net outcomes across 120 processed fills. These remain explicit follow-up diagnostics; do not promote estimated PnL to verified learning.
- PR #90 merged as runtime commit `cbf653a4e9c3659f8df9f075025ba1758b8f5c20` after all five CI jobs passed on the exact PR head (295 tests passed). Confirm main-branch post-merge CI for this commit separately.
- Keep release metadata aligned across config.yaml, pyproject.toml, Dockerfile and Kraken User-Agent.

## Recently merged releases

All listed PRs were merged only after all five CI jobs passed.

- **0.1.74 — PR #79:** Fixed Spot fill-history sync. Kraken `TradesHistory` is paginated with `ofs`; the previous `with_cursor`, `cursor`, and `limit` arguments caused `EGeneral:Invalid arguments`. Sync progress is now durably resumed by offset.
- **0.1.75 — PR #80:** Added early exchange-minimum checks for new/increased orders. Emits `CYCLE_ORDER_BELOW_MINIMUM`; does not round orders up or bypass risk/cash limits. Reduce-only dust handling and Futures quantity semantics remain separate.
- **0.1.77 — PR #82:** Added numerical context and `failed_checks` to `CYCLE_RISK_DECISION` so daily-loss, gross/net exposure, cash reserve, drawdown and position-limit blocks can be diagnosed from logs. Risk decisions themselves were not loosened.
- **0.1.76 — PR #81:** Tactical cooldown is now a bounded, versioned learned parameter (30–600 seconds), hidden from HA options and protected against legacy `options.json` overrides. It increases after poor verified live outcomes and decreases cautiously only after improving outcomes. Hard trade-rate and risk caps remain immutable.
- **0.1.78 — PR #85:** Fixed Tactical risk-gate inconsistency: a Tactical per-position allowance can no longer widen the portfolio-wide `risk_max_net_pct` hard cap. Regression coverage now requires a `net_limit` rejection for 75% projected net exposure when the global cap is 50%, even if the Tactical per-position cap is 80%.
- **0.1.79 — PR #88:** Prevented Austrian tax-ledger attribution from equating disposal date with acquisition regime; legacy/mixed inventory and pre-effective-date crypto swaps are marked incomplete for review rather than receiving fabricated gains or automatic neutral treatment.
- **0.1.80 — PR #90:** Wired risk-approved Tactical live Spot longs to guarded FX quote funding, followed by portfolio reconciliation and a second risk check after conversion.
- Obsolete PRs #30, #35 and #49 were closed as superseded by later releases and the newer adaptive-policy architecture.

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
