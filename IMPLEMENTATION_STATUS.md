# Kraken AI Trader v1 – Implementation Status

## Scope

This is a clean Home Assistant custom integration designed from the authoritative
`AUTONOMOUS_TRADER_SPEC(1).md` and `MASTER_PROMPT_AUTONOMOUS_KRAKEN_AI_TRADER(1).md`.
The legacy KTKI code was inspected read-only and was not imported into the new
runtime.

## Architecture delivered

- One `CentralTradingAuthority` is the only component allowed to create live order intents and submit them.
- Kraken is treated as account/order/position truth; SQLite is historical materialization.
- Dynamic Spot, Spot Margin and Kraken Futures instrument discovery uses Kraken metadata rather than a manual universe.
- Fast scan → eligibility/liquidity/spread/data-quality filter → bounded deep scan.
- Long and Short decisions are separate; leverage is selected dynamically inside immutable safety limits.
- Cost-aware execution chooses passive/aggressive order style instead of a fixed market-order path.
- Unknown/ambiguous order submissions enter reconciliation before any replacement is possible.
- Portfolio and order reconciliation are explicit and recovery is fail-safe.
- News is an event pipeline; Gemini produces validated structured enrichment and cannot submit orders or change limits.
- Learning covers trades, non-trades and outcomes; calibration and model registry/promotion/rollback are persisted.
- HA sensors/services use one shared runtime coordinator; no web dashboard/GUI exists.
- WebSocket supervision handles reconnects and sequence gaps and delegates recovery to the central authority.

## Verification

Last local verification:

- `pytest -q`: 36 passed.
- Coverage: 85.76% with branch coverage; the configured 85% gate is reached.
- `python -m compileall -q custom_components tests`: passed.
- JSON validation of repository JSON files: passed.
- `python -m pip wheel . --no-deps --no-build-isolation`: package build passed.

The local environment did not contain the `ruff` executable, so Ruff was not run locally. The CI workflow runs Ruff in GitHub Actions.
The full Home Assistant runtime package was not installed in the local environment, so HA lifecycle execution was not run locally; the integration modules were syntax-checked and the deterministic core was tested independently.

## Target repository write status

Target: `GerMorg/Kraken-Trade-Real`, base `main`, source commit:
`76bdf6bbf45b0edf5a2b1c99b0bb46c88c288bdb`.

The connected GitHub installation currently exposes `GerMorg/KTKI` for write operations but not `GerMorg/Kraken-Trade-Real`. Attempts to create a branch/file in the target repository were rejected by GitHub with HTTP 403 (`Resource not accessible by integration`). Therefore no target-repository commit has been claimed here.

To complete the GitHub integration step, the repository must be added to the connected GitHub App installation's repository selection (or the installation must be configured to allow the repository). After that, the prepared tree can be committed and then verified by GitHub Actions before merging to `main`.

## Operational safety

`live_enabled` defaults to `false`. A small account (including the 50 EUR acceptance case) can legitimately resolve to `NO TRADE` whenever Kraken minimums, available funds/margin, costs or risk constraints make execution uneconomic.
