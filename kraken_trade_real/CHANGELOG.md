## 0.1.86

- Reserve projected exposure, open-position slots, cash and free margin across accepted, partially filled, and exchange-ambiguous Core orders in a trading cycle.
- Check Core cash reserve against post-order cash for both long and short increases; only a strictly valid reduce-only action is exempt.
- Restore Tactical TRAILING positions after restart, reserve capacity for unresolved entries, reject malformed/non-finite exit prices, and reject future-dated websocket snapshots as fresh data.
- Separate Tactical LIVE risk-limit PnL and entry-episode counts from SHADOW rows and partial-close rows. Include a conservative, explicitly unverified Spot-Margin rollover estimate in Tactical net outcomes; never treat modeled net PnL as ledger-verified training data.
- Reject non-finite/crossed Core quotes and invalid order-book levels. Normalize Core volatility over a fixed 15-return horizon and retain existing positions when history is insufficient rather than interpreting missing data as a negative signal.
- Align Tactical take-profit optimizer bounds with adaptive configuration (1–8%). Correct the global Core/Tactical net-limit diagnostic so it reports the same global cap used by RiskEngine.
- Add cash-flow-aware daily PnL/drawdown normalization from the Kraken Spot ledger. It is enabled only when history is complete and all external cash flows can be valued in EUR; otherwise the raw basis is retained and the incomplete basis is logged.
- Correct Futures long/short side normalization and add cycle-managed reduce-only stop/profit protection for identified derivative positions using validated executable close-side quotes, without mixing Futures observations into the Spot-specific exit-learning dataset.
- Bump add-on/package/build/User-Agent version metadata to 0.1.86. Add regression coverage for cycle-wide reservations, exact cash reserve thresholds, order-book integrity, restart recovery, cash-flow normalization, parameter bounds, and Futures protection.

# Changelog

## 0.1.85
- Tighten Tactical reward/risk admission: forecast net edge must cover the configured stop distance plus the modeled round-trip cost reserve, targeting approximately 1:1 net reward/risk under the conservative assumptions. Default gross expected-move floor rises to 600 bps; adaptive/normal cost floors are doubled to reflect that costs are incurred on both the winning and stopped-out scenario.
- Align cost-aware trailing protection to the same economics: at the protected floor, estimated net gain covers the stop-loss-plus-cost budget. Default target/arming threshold is 6.5% for the baseline assumptions and increases automatically with wider spread, financing and other cost reserves.
- Reject crossed bid/ask books and invalid/non-finite quote/spread data before entry; calculate quote spread from bid/ask and use the higher of exchange-reported and calculated spread. Malformed history points/volume ratios cannot poison the signal calculation.
- Keep exit management fail-safe when quote fields contain non-numeric or non-finite values; log and skip that observation instead of crashing the cycle.
- Extend regression tests across quote boundaries, cost understatement, NaN/malformed history, doubled cost floors, risk/reward calculations and live/replay target parity.
- Align app, package, add-on, Docker and Kraken User-Agent release metadata at 0.1.85.

## 0.1.84
- Correct Tactical SHORT profit/loss, peak-gain and realized gross-PnL calculations to use returns against entry notional; evaluate exits at executable bid (LONG) / ask (SHORT) prices.
- Keep Tactical maximum holding time effective even after trailing activation; align counterfactual exit replay with live trailing activation and maximum-hold rules.
- Require the forecast net edge to cover at least the configured stop-loss distance after estimated round-trip costs; set the default gross-move floor to 350 bps.
- Align the default trailing-entry target to 4.1%, so the nominal take-profit parameter matches the configured worst-case cost floor, stop-risk buffer and 0.7% trailing distance. Enforce cost/risk floors in config validation, adaptive policy refresh, optimizer candidates and promotion.
- Reuse Core's calibrated confidence at the final risk gate; malformed/non-finite confidence calibration safely falls back to neutral.
- Add regression cases for exact short stop-loss boundaries, executable bid/ask losses, linear short PnL, post-trailing time stop, live/replay alignment and calibrated confidence.
- Align add-on, package, application, Docker and Kraken User-Agent version metadata at 0.1.84.

## 0.1.83
- Add explicit Tactical live-entry fill events with BUY/SELL side, direction, quantity, fill price, notional, estimated move/cost/net edge and order ID.
- Enrich Tactical exit order/fill events with actual order side, entry/exit prices, quantity, exit reason, estimated fees and net result.
- Mark `CYCLE_COMPLETED.orders` as Core-strategy-only so Tactical orders running on their independent thread are not mistaken for missing orders.
- Prevent the trailing regime from arming below the estimated round-trip cost floor plus the trailing distance; avoids disabling the time stop at a nominal target that is not yet cost-covering.
- Bump Home Assistant app, package, container and Kraken API User-Agent versions to 0.1.83.


## 0.1.82
- Report when a ledger-verified fill fee is denominated in the base asset rather than the instrument quote asset, so estimate-only realized outcomes have an explicit cause.
- Add regression coverage for non-quote-denominated fees in attribution diagnostics.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent versions to 0.1.82.


## 0.1.81
- Add a drawdown-basis audit event with raw equity peak, peak timestamp, snapshot count and explicit disclosure that external cash flows are not normalized; preserve the immutable drawdown gate.
- Emit attribution anomaly reason counts and per-leg reasons why realized net outcomes remain estimate-only.
- Add regression tests for attribution diagnostics.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent versions to 0.1.81.


## 0.1.80

- Route live Tactical Spot-long quote funding through the same guarded FX funding manager used by Core when the exact quote currency is not already funded.
- Run funding only after Tactical risk approval; reserve fees/slippage/safety buffer in quote currency, reconcile after a confirmed conversion, re-check risk, and fail closed on unavailable funds or reconciliation errors.
- Use exchange-spendable quote balance (including order holds) instead of gross wallet balance for Tactical cash preflight.
- Add regression tests for successful FX funding, post-conversion reconciliation and fail-closed funding errors.
- Bump Home Assistant app, package, container and Kraken User-Agent versions to 0.1.80.

## 0.1.79

- Make Austrian Altvermögen/Neuvermögen attribution inventory-based instead of inferring the tax regime from disposal date.
- Mark disposals with legacy or mixed Alt-/Neuvermögen inventory as incomplete for manual review, without fabricating a 27.5% gain, consuming ambiguous lots or asserting crypto-to-crypto tax neutrality.
- Restrict automatic tax-neutral crypto-to-crypto cost-basis rollover to tracked Neuvermögen from 1 March 2022 onward; pre-effective-date and Altvermögen swaps remain explicitly review-required.
- Add regression tests for old holdings sold after 2021, mixed-regime inventory, and historical/legacy crypto-to-crypto swaps.
- Bump Home Assistant app, package, container and Kraken User-Agent versions to 0.1.79.


## 0.1.78

- Fix a Tactical risk-gate inconsistency: the strategy-specific position limit can no longer raise the immutable portfolio-wide net-exposure cap.
- Keep the Tactical position limit as a per-position gate while applying the same configured global net-exposure limit to Core and Tactical entries.
- Add regression coverage proving an 80% Tactical position allowance cannot admit a 75% net exposure when the global net cap is 50%.
- Treat the first two v0.1.77 post-install cycles as an initial observation window; zero new fills in that short interval is not, by itself, evidence of a fill-sync defect.
- Bump Home Assistant app, Python package, container build and Kraken User-Agent to 0.1.78.

## 0.1.77

- Extend `CYCLE_RISK_DECISION` with the actual equity, cash, daily PnL and daily-loss limit/headroom, drawdown, projected gross/net exposure, position percentage, cash reserve and position-count values.
- Include the exact failed risk-check names so a `daily_loss`, `gross_limit`, `net_limit` or other block can be verified numerically instead of inferred from a reason label.
- Keep the risk engine decision logic unchanged; this release improves observability only.
- Add regression tests for numeric diagnostics and bump release version to 0.1.77.

## 0.1.76

- Add Tactical cooldown to the internally managed, versioned policy; remove it from visible Home Assistant options and ignore stale values in existing options files.
- Increase cooldown gradually after a negative realized live-trade window and reduce it cautiously only after a positive, improving live window.
- Enforce a 30–600 second cooldown range and retain immutable maximum trades-per-hour/day and daily-loss limits.
- Add regression coverage for stale-option isolation, policy promotion and cooldown bounds.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.76.

## 0.1.75

- Add an early exchange-minimum gate for new/increased positions so quantities below Kraken's minimum quantity or Spot minimum quote cost are skipped before an order intent is created.
- Emit `CYCLE_ORDER_BELOW_MINIMUM` with the exact quantity/cost shortfall and keep risk budgets unchanged; never round an order upward to force acceptance.
- Keep existing reduce-only dust/exit handling and the Futures contract-quantity semantics separate.
- Add regression tests for quantity, Spot minimum cost, Futures and invalid quantity cases.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.75.

## 0.1.74

- Fix repeated Spot fill-history sync failures: Kraken TradesHistory uses `ofs` pagination and does not support the previously sent `with_cursor`, `cursor` or `limit` parameters.
- Persist and resume an offset-based sync session, advance by the actual number of returned trades, and complete safely on the final page.
- Add regressions asserting only documented TradesHistory pagination arguments are sent and that multi-page fill attribution resumes across cycles.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.74.

## 0.1.73

- Move Normal market minimum-liquidity and maximum-spread thresholds into the versioned adaptive policy, removing both from the Home Assistant options/schema.
- Adjust these market filters only after sufficient fully verified Normal realized net outcomes: tighten following persistently poor outcomes and restore cautiously toward baseline after strong improving outcomes.
- Enforce 50–1,000 EUR minimum-liquidity and 20–80 bps maximum-spread bounds in runtime defaults, optimizer and central registry promotion.
- Apply newly promoted thresholds at each market scan without restart and include the active values in cycle diagnostics.
- Add regression coverage for stale HA-option isolation, central bound rejection, verified-outcome tightening/restoration and live scanner adoption.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.73.

## 0.1.72

- Treat Kraken Spot Ticker 24h volume as base-asset quantity and calculate quote-currency turnover explicitly.
- Convert Spot quote turnover into EUR using fresh, tradable Spot FX/fiat-stablecoin pairs; exclude Spot candidates if EUR conversion cannot be established.
- Rank Spot markets by EUR-notional turnover before history selection and use the same normalized value in market features.
- Keep open portfolio positions in the history-analysis set when entry liquidity/spread filters reject new entries, so existing holdings can still be evaluated for exits and rebalancing.
- Keep futures/derivative volume out of the Spot EUR-turnover comparison when a comparable contract notional is unavailable.
- Add diagnostics and regression tests for USD-to-EUR conversion, low-notional high-unit-volume assets, and missing FX conversion.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.72.

## 0.1.71

- Keep learned Tactical expected-move thresholds strictly above configured round-trip fee, spread, slippage and safety-buffer assumptions.
- Enforce the same invariant in the runtime adaptive overlay, the online Tactical optimizer and central model promotion so legacy/corrupt policies cannot undercut cost floors.
- Add regression tests for configured fee changes, unsafe model candidates and positive-performance policy relaxation at the cost boundary.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.71.

## 0.1.70

- Allow Normal entry-policy promotion to use only explicitly verified quote-ledger net outcomes from non-margin Spot trades and eligible short-hold Spot-Margin episodes; estimates, unlinked fees and rollover-exposed episodes remain excluded.
- Align centralized model-promotion validation with the same strict, versioned outcome contract.
- Fix a positive-outcome comparison that referenced a non-existent statistic and add regression coverage.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.70.

## 0.1.69

- Add Tactical context conflict tolerance to the same versioned policy registry as the other learned entry filters.
- Tighten the allowed context conflict by 10% after sufficiently poor LIVE outcomes; relax it by only 2% after positive, improving results, always within 10–200 bps.
- Use the context threshold from the same entry-time policy snapshot that is persisted with the trade, preventing mismatch between execution and learning attribution.
- Remove the learnable threshold from HA options and ignore legacy saved values.
- Add tests for policy overlay, promotion bounds, stale option isolation and entry-time capture.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.69.

## 0.1.68

- Make Core/Normal adaptive-edge evaluation and Tactical adaptive-entry evaluation application-owned, always-enabled modes instead of manually changeable HA options.
- Ignore legacy options that attempted to disable either adaptive path, ensuring the runtime policy remains consistent with its learner.
- Remove both switches from the Home Assistant options/schema to reduce manual tuning and accidental divergence.
- Add regression coverage proving stale options cannot disable adaptive strategy evaluation.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.68.

## 0.1.67

- Allow strictly valid reduce-only exits to proceed when portfolio equity is zero or negative, so an extreme loss or degraded valuation cannot trap an existing position behind the equity-positive gate.
- Keep new entries and invalid reduce-only requests blocked; the exception requires an existing nonzero position, a smaller target absolute exposure and no sign flip.
- Add regression tests for long/short risk reductions under depleted equity and for invalid flips/new risk.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.67.

## 0.1.66

- Derive Normal cash-Spot mark-to-market PnL from app-owned FIFO inventory only when the complete wallet quantity exactly matches unleveraged, single-quote-pair inventory.
- Extend persistent Normal stop-loss, partial-profit and trailing-profit management to those proven cash-Spot holdings; manual, mixed, margin-backed, cross-pair or quantity-mismatched balances remain excluded.
- Include fully attributed cash-Spot position episodes in Normal exit-path learning, only when the opening order/decision can be reconciled to the current-cycle entry.
- Keep exit path capture alive only with trustworthy source reads and avoid closing/censoring episodes on failed wallet or margin-position reads.
- Add regression coverage for exact-inventory matching, fail-closed handling, cash-Spot stops and exit-episode eligibility.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.66.

## 0.1.65

- Train Tactical entry filters only from completed LIVE position episodes; historical SHADOW outcomes no longer contaminate live policy adaptation.
- Aggregate partial-close rows into a single position outcome and exclude positions that still remain open when counting the 60-sample minimum or the 10-new-episode reevaluation gate.
- Label the learner's data basis explicitly as configured-cost estimates, not ledger-verified exchange net PnL; immutable risk caps remain outside learning.
- Add regression coverage for Shadow exclusion, multiple partial closes, and distinct-position sample counts.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.65.

## 0.1.64

- Fall back to validated per-parameter defaults when learned model records contain malformed, non-numeric or non-finite values.
- Prevent corrupted active policy data from leaking NaN/Infinity or strings into live trading arithmetic; keep cross-parameter safety relationships enforced.
- Add regression coverage for malformed Core and Tactical registry values.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.64.

## 0.1.63

- Add structured, credential-free context to deterministic exchange order rejections: instrument/base/quote, side/direction, order type, quantity, price/notional estimate, leverage/margin/reduce-only, exchange minimums, and linked decision rationale where available.
- Persist each deterministic rejection as an `ORDER_SUBMISSION_REJECTED` database event in addition to emitting a structured audit-log entry, so failures can be counted and investigated without joining by an otherwise opaque intent ID.
- Classify common failures such as insufficient funds, invalid arguments, below-minimum orders and rate limits, and include diagnostic next-step hints without retrying or relaxing any risk checks automatically.
- Keep diagnostics best-effort: event-write/query failures must never alter exchange submission handling or block exits.
- Add regression tests for rejection classification, event persistence, order context and absence of credential fields.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.63.

## 0.1.62

- Centralize validated numeric bounds for learned Core/Normal and Tactical strategy parameters so runtime clamping and promotion validation share the same source.
- Validate complete candidate policy combinations before activation, including Core adaptive-threshold nesting, Core profit-exit ordering, Tactical volatility-band ordering, and Tactical stop/target ordering.
- Reject non-finite/out-of-range values, fractional Tactical maximum-hold values, and attempted changes to non-learnable Tactical position ceilings or adaptive-entry mode.
- Emit an auditable policy-promotion rejection reason without changing the active model; immutable account/exposure/daily-loss/leverage controls remain outside the optimizer.
- Add regression tests for bounds completeness, invalid cross-parameter combinations, and immutable Tactical safety/operation settings.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.62.

## 0.1.61

- Capture the same actionable Tactical signal direction and net edge with each observed price-path point, enabling faithful replay of reversal exits.
- Add idempotent migration for existing price-path databases; legacy samples retain empty signal metadata and cannot fabricate reversal labels.
- Include the bounded Tactical reversal threshold in path replay, chronological candidate selection, and the existing holdout promotion gates.
- Remove the learned reversal threshold from Home Assistant options/schema and ignore old options.json overrides.
- Validate the threshold again during model promotion and preserve its fixed bounds of 30–500 bps.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.61.

## 0.1.60

- Require Tactical signal-reversal exits to confirm that the actionable signal direction is opposite to the held position; a strong same-direction setup can no longer trigger a false reversal close.
- Add regression tests for same-direction signals, genuine opposite-direction signals and the configured minimum reversal edge.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.60.

## 0.1.59

- Restrict Normal entry-policy training and promotion to fully closed non-margin Spot outcomes with verified quote-currency or explicitly zero fees.
- Train from the realized, fee-verified net return stored by the FIFO fill ledger rather than gross return minus an estimated cost field.
- Exclude estimated-only fills, unverified fee currencies, incomplete FIFO episodes, remaining inventory and Tactical trades from this learning controller.
- Require promotion metrics to attest to verified-net labels and the non-margin Spot data scope; stale estimated-return candidates are not promotable under the new contract.
- Add regression tests proving verified net outcomes can train the policy and estimate-only outcomes cannot.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.59.

## 0.1.58

- Read Kraken Spot BalanceEx balances and calculate spendable funds after open-order holds, separate from total portfolio valuation.
- Fail closed for new Spot cash spending when the extended balance cannot be read; total holdings remain available for valuation and diagnosis.
- Gate cash Spot buys and sells in the shared trading authority using a common fee/slippage buffer and fresh available balances.
- Serialize funding orders with ordinary core and Tactical submissions, and use available balances when sourcing FX or liquidating a funding source.
- Reserve accepted cash-buy quote amounts through the Normal decision pass so several candidates cannot each plan to spend the same wallet snapshot after one FX conversion.
- Add regression coverage for BalanceEx parsing, fail-closed behavior, insufficient free funds, and the shared quote-budget formula.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.58.

## 0.1.57

- Normalize Python boolean parameters to lowercase REST form values before signing and sending authenticated Spot requests.
- Prevent invalid-argument responses from Kraken TradesHistory when requesting cursor pagination, trade data and linked ledger IDs.
- Add a wire-level regression test verifying exact serialized values and signature consistency.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.57.

## 0.1.56

- Request linked ledger IDs in Kraken Spot trade history and resolve fee currency through QueryLedgers batches of at most 20 IDs; missing permission or ambiguous data leaves the fee explicitly unverified and never interrupts fill persistence.
- Persist base-asset context and idempotently enrich previously unverified fills without changing their quantity, price or order attribution.
- Separate ledger-backed verified net PnL from historical estimate-only PnL. Verified outcomes require quote-denominated or explicitly zero entry/exit fees and exclude margin/leverage episodes whose financing costs are not yet fully reconciled.
- Preserve estimate-only fields for incomplete or non-quote fee data; verified columns remain null instead of receiving fabricated conversions.
- Add schema migration v12 and regression tests for ledger fee matching, permission-denial fallback, duplicate enrichment, verified net calculations, and rejection of base-asset fees as quote net.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.56.

## 0.1.55

- Capture the exact versioned Tactical entry policy and signal features alongside the opening decision before any order is submitted.
- Evaluate learned entry filters against the same snapshot captured for that signal, preventing a concurrent policy refresh from recording a different set of thresholds.
- Persist entry snapshots in SQLite with an idempotent schema migration; routine position updates preserve them instead of replacing them with empty defaults.
- Restore entry-time policy/context when loading positions after a restart and when reconciling a pending entry order from its stored decision rationale.
- Trade learning records now use true entry-time values and signal features. Legacy positions without a snapshot are explicitly marked as missing rather than filled with unrelated close-time parameters.
- Preserve existing learning-field aliases while also storing exact internal parameter names for later attribution and audit.
- Add regression tests for durable snapshots, restart recovery, immutable updates, entry-time/close-time separation, legacy-data labeling, and migration idempotence.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.55.

## 0.1.54

- Extend the versioned Tactical policy with minimum/maximum volatility, order-book imbalance, adaptive confidence, and minimum net-edge thresholds.
- Apply bounded entry-threshold tightening after poor realized trade windows and cautious loosening only after positive, improving windows; enforce the volatility-band relationship both before runtime use and during model promotion.
- Remove those learned strategy thresholds from the Home Assistant options/schema and ignore stale values in existing `options.json` files.
- Add regression coverage for internal policy overlays, legacy-option isolation, parameter adaptation, promotion bounds, and corrupt volatility-band fallback.
- Keep position/capital limits, daily-loss limits, leverage, and order-rate caps outside automatic optimization.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.54.

## 0.1.53

- Add a Normal/Core Spot Margin exit-policy replay optimizer over persistently recorded, eligible, app-managed single-order episodes.
- Reconstruct historical paths only from observations captured before each real close; require the active baseline policy to match the observed exit reason, timing, and partial-profit state before using an episode for training.
- Select bounded exit-policy neighbours on a chronological training split and promote only when coverage, net returns after the recorded decision-cost estimate, independent holdout improvement, and drawdown gates all pass.
- Move Normal/Core stop-loss and profit-taking/trailing parameters into the versioned internal policy and remove them from the Home Assistant options schema; stale legacy options are ignored.
- Preserve all hard portfolio, leverage, daily-loss and drawdown limits outside automatic policy learning.
- Add regression tests for candidate promotion, minimum sample requirements, path quality filters, and internal configuration ownership.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.53.

## 0.1.52

- Record restart-safe Normal/Core Spot Margin position episodes and per-cycle PnL paths, including the exact exit-policy snapshot, basis, quantity, pending partial-exit state and data-quality context.
- Attribute only fresh, single-order positions captured from their opening cycle to app-managed Core decisions; inherited/manual positions, mixed directions and multiple open margin lots are explicitly ineligible for policy learning.
- Persist one path point per runtime cycle and upsert the final portfolio reconciliation, preventing double-counting from the early and final snapshot in the same cycle.
- Reset the Core profit high-water/partial-exit state when the exchange opening-order identity changes for a symbol; preserve state across failed OpenPositions reads.
- Keep path-ledger operations non-blocking for live stop-loss and profit-protection logic. Exit parameters remain visible until replay and validation gates are implemented.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.52.

## 0.1.51

- Add versioned, bounded adaptive management of Normal strategy entry thresholds using fully closed app-managed Spot/Margin position outcomes.
- Use directional gross return in basis points minus the entry decision's stored expected-cost estimate; do not read or learn from unverified TradesHistory fee amounts/net PnL.
- Tighten entry filters after weak recent outcomes; only restore prior, extra selectivity toward documented defaults after sustained improving results. Never relax below defaults or alter hard risk controls.
- Require 60 completed outcomes, compare chronological 30-outcome windows, wait for at least 10 new outcomes between evaluations, and enforce policy evidence and parameter bounds again in the model registry.
- Create dedicated baseline model families for Normal and Tactical policies so version history, promotion and rollback stay within the correct strategy family.
- Remove learned Normal entry thresholds from HA options/schema and ignore legacy values in existing options.json files; publish optimizer status in learning telemetry.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.51.

## 0.1.50

- Rebuild a deterministic FIFO position-attribution ledger from app-managed Spot/Margin fills, including partial reductions and multiple closes linked back to their opening decision.
- Record each matched close leg with entry/exit prices, quote-currency gross PnL, proportional TradeHistory fee estimates and estimated—not verified—net PnL. Open inventory lots persist separately from realized legs.
- Handle long and short position direction explicitly using local order direction and `reduce_only`; quarantine unexplained reversals, side mismatches, missing raw exchange context and closes that exceed tracked inventory.
- Rebuild derived outcome tables atomically and deterministically after new fill data, preserving auditability across restarts and repeated sync pages.
- Keep every outcome marked `QUOTE_ESTIMATE_ONLY` / `net_verified=0`. This release does not use estimated Spot fee amounts to auto-promote Normal strategy parameters; actual Ledger fee reconciliation and Futures attribution remain required.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.50.

## 0.1.49

- Wire Kraken Spot TradesHistory into the live cycle so actual local Spot/Margin order fills are persisted; the pre-existing fill table is no longer an unused sink.
- Match exchange fills only to known local Spot orders and preserve decision ID, order/client IDs, quote asset and the raw exchange payload for auditable attribution.
- Add schema migration for fill attribution fields and indexes without deleting prior fill history; idempotent order/trade keys prevent duplicates after retries.
- Use fixed time-window pagination with Kraken's `with_cursor`/`cursor.next` API (not deprecated `ofs`), persisting opaque cursors between cycles; bound pages per cycle and make failures non-blocking. The sync never changes live order decisions.
- Kraken's Spot TradesHistory payload does not include an explicit fee-currency field; keep that currency marked UNVERIFIED and do not use those fee values to promote strategy policy until fee denomination and realized-PnL attribution are independently validated.
- Futures fill and position-event attribution remains a separate package; this release does not yet claim full Normal strategy realized-PnL learning.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.49.

## 0.1.48

- Record a cost snapshot and execution context with each closed Tactical LIVE trade so path-based comparisons account for fees, spread, slippage, safety buffer and estimated margin financing.
- Add a conservative path-replay exit optimizer for stop-loss, take-profit activation, trailing trigger/stop and maximum hold. It requires at least 60 supported closed episodes, baseline replay alignment, at least 85% candidate coverage, a chronological 70/30 split, positive holdout net expectancy and bounded drawdown degradation before promotion.
- Enable versioned AdaptiveConfig management of the five Tactical exit parameters and hide them from HA options/schema; stale values in old options.json files cannot override the learned policy.
- Add registry-side hard bounds and independent holdout checks. Daily loss, leverage, exposure caps, maximum position/capital controls and other hard safety boundaries remain outside the optimizer.
- Do not fabricate entry-time observations for recovered legacy positions; their replay path begins when actual post-upgrade prices are observed.
- Bump app/package/container version and Kraken API User-Agent to 0.1.48.

## 0.1.47

- Persist timestamped Tactical price-path observations per position (including direction, PnL, peak/trough and state) for future path-aware exit-policy evaluation.
- Seed each path with the known entry price, including after process restart or position recovery.
- Deduplicate repeated WebSocket ticks, bound each position's retained path, and make telemetry failures non-blocking for live exit management.
- Add an idempotent SQLite schema migration and path retrieval that can be truncated to an actual close timestamp.
- Keep exit-policy parameters visible until a validated, path-based optimizer is implemented in a subsequent release.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.47.

## 0.1.46

- Extend forecast calibration with regime-specific confidence scales trained on chronological training windows and accepted only when the combined holdout improves Brier score while respecting ECE and absolute-quality gates.
- Apply the active regime-specific scale consistently in core signal selection, position sizing, held-position quality checks and rejection diagnostics.
- Keep regime scales in versioned model parameters; no additional HA tuning options are introduced.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.46.

## 0.1.45

- Add a versioned adaptive configuration overlay so selected Tactical entry filters and allocation are loaded from the active policy model, not Home Assistant options.
- Ignore legacy HA option values for internally learned parameters and remove those fields from the visible add-on schema.
- Add a bounded Tactical policy controller based on realized closed-trade outcomes, with minimum sample windows, deterministic candidate versions, transactional promotion and independent hard bounds.
- Refresh promoted policy values between cycles and emit auditable policy evaluation/promotion events.
- Keep stop-loss, take-profit, trailing exits, leverage and global risk caps outside this controller until the trade ledger contains the path-dependent evidence needed to optimize them safely.
- Add regression tests for learned configuration isolation and adaptive policy promotion.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.45.

## 0.1.44

- Derive daily P/L from persisted UTC-day equity baselines rather than Kraken TradeBalance's non-daily P/L field; avoid double-counting open-position P/L.
- Include Kraken Spot Margin used margin in portfolio-wide margin utilization and budget checks.
- Treat Kraken terminal closed partial fills as terminal while preserving cumulative execution quantities for reconciliation.
- Track Tactical cumulative fills durably, apply incremental entry/exit fills once only, size positions to actual entry fills, and avoid competing orders while a Tactical order remains active.
- Add regression coverage for daily P/L, Spot Margin utilization and terminal partial/zero-fill order states.
- Bump Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.44.

## 0.1.43

- Bound forecast scoring to fresh market snapshots; expired forecasts without a valid sampling window become explicitly `UNSCORABLE` instead of remaining `OPEN` indefinitely.
- Require at least 300 settled forecasts and 90 chronological validation outcomes before automatic calibration; require meaningful Brier improvement and non-regressing ECE/absolute-quality gates.
- Derive candidate IDs from the parent model and evaluated data so repeated feedback passes cannot overwrite an already promoted model with a same-second candidate.
- Recover the model registry deterministically if persistent state has zero or multiple active decision models, and make promotion/rollback transitions transactional.
- Honor `learning_enabled`, `learning_auto_calibration`, `learning_lookback_days` and `learning_validation_interval_hours`; the bounded calibration dataset advances to the most recent observations while preserving chronological order.
- Bump the Home Assistant app, Python package, container build and Kraken API User-Agent to 0.1.43.


## 0.1.42

- Aggregate all Kraken OpenPositions lots per pair for quantity, PnL, cost basis, exposure and leverage instead of silently dropping later lots.
- Use Kraken's native Spot Margin `settle-position` order for confirmed full exits with Kraken's documented `volume=0` settle-all sentinel, which does not need an estimated quantity; strictly validate this reduce-only path and avoid irrelevant minimum-cost/FX-funding checks.
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
