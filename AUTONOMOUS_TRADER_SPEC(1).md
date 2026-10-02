# AUTONOMOUS_TRADER_SPEC

Diese Spezifikation ergänzt den Master-Prompt und ist verbindliche technische Referenz.

## 1. Architektur
- komplett neue Anwendung; KTKI nur Read-only-Referenz
- keine Legacy-Imports, keine alte GUI
- eine zentrale Trading Authority
- Kraken ist Wahrheit über Konto/Orders/Positionen
- DB ist Historie/Materialisierung
- HA ist operative Konfigurationsautorität
- Lernen darf Sicherheitsgrenzen nicht verändern
- Gemini darf keine Orders direkt auslösen
- Research und Live Trading logisch trennen
- jeder Trade und Nicht-Trade muss rekonstruierbar sein

## 2. Produkte
Ab Version 1: Spot, Spot Margin, Long, Short, Leverage und verfügbare Kraken-Derivate/Perpetuals. Verfügbarkeit immer dynamisch aus Konto-, Produkt- und Instrumentdaten ermitteln.

## 3. Dynamisches Universum
Kein Pflicht-Whitelist-Universum. Alle erreichbaren Instrumente scannen und filtern nach Status, Konto-Berechtigung, Richtung, Margin/Leverage, Mindestgröße, Liquidität, Datenqualität, Historie, Spread, Kosten, Risiko und Positionierbarkeit. Neue Märkte automatisch aufnehmen, unhandelbare entfernen.

## 4. Instrument Registry
Logisch erfassen: venue, product_type, symbol, instrument_id, altname, base/quote, contract_type, status, margin, long/short, leverage levels, max leverage, order_min, cost_min, lot/price precision, tick size, position limits, margin class, collateral, funding, fee model, update time. Keine Pair-IDs aus Strings konstruieren.

## 5. Analyse
Fast Scan: Status → Eligibility → Liquidity → Spread → Volume → Volatility → Data → History → Execution → Preliminary Edge.
Deep Scan: Multi-Timeframe, Trend, Momentum, Mean Reversion, Breakout, Volatility, Liquidity, Orderbook, Impact, Cross-Asset, News, Gemini, Ensemble, Portfolio Context, Expected Net Edge.

Features mindestens: Returns, Momentum, Trend, EMA/SMA slopes, ATR, realized/downside volatility, volume anomalies, spread, depth, imbalance, liquidity, market impact, relative strength/weakness, correlations, BTC regime, breadth, funding, basis, open interest, liquidation data, news intensity/direction/novelty, Gemini confidence, forecast and uncertainty. Keine Zukunftsdaten.

## 6. Regime
TREND_UP, TREND_DOWN, RANGE, HIGH_VOLATILITY, LOW_VOLATILITY, LIQUIDITY_STRESS, PANIC, RECOVERY, BREAKOUT, MEAN_REVERSION, UNKNOWN. Ebenen: global, cluster, asset, instrument.

## 7. Long/Short
Separate Scores. Zustände: NO_POSITION, OPEN_LONG, INCREASE_LONG, REDUCE_LONG, CLOSE_LONG, OPEN_SHORT, INCREASE_SHORT, REDUCE_SHORT, CLOSE_SHORT, REVERSE_LONG_TO_SHORT, REVERSE_SHORT_TO_LONG. Short besitzt eigene Kosten-, Risiko-, Exit- und Performancebehandlung.

## 8. Leverage/Margin
Leverage dynamisch anhand Signal, Edge, Volatilität, Liquidität, Slippage, Modellqualität, Regime, Drawdown, Exposure, Korrelation, Margin Utilization, Liquidation Distance, News und Gemini Confidence. 1x bleibt möglich. Überwachen: equity, initial/maintenance/used/free margin, collateral, notional, liquidation distance, PnL, funding/financing, open-order margin impact. Kritische Margin → keine neuen Entries → Risiko reduzieren → reconciliieren → erst danach normalisieren.

## 9. Position Sizing
Risk-based, abhängig von Edge, Unsicherheit, Downside Risk, Volatilität, Liquidität, Kosten, Modellqualität, Regime, Korrelation, Exposure, Drawdown, Daily Loss, Leverage und Margin. CVaR/Expected Shortfall und Volatility Targeting können verwendet werden.

## 10. Kosten/Execution
Kostenmodell: Fees + Spread + Slippage + Market Impact + Funding + Financing + ggf. FX + Safety Buffer. Nur positiver ausreichender Net Edge. Execution wählt abhängig von Spread, Depth, Volatility, Urgency, Edge Decay, Fill Probability und Liquidity zwischen passive limit, post-only, marketable limit und market. Keine pauschale Market-Order-Strategie.

## 11. Order State
INTENT_CREATED → PRECHECK_PASSED → SUBMITTING → ACKNOWLEDGED → LIVE → PARTIALLY_FILLED → FILLED; Endzustände CANCELED/EXPIRED/REJECTED/UNKNOWN_RECONCILING. Bei ambiger Antwort niemals blind retry. Reconcile über client_order_id, open/closed orders, Query Order und private executions.

## 12. IDs
app_instance_id, cycle_id, decision_id, intent_id, client_order_id, Kraken_order_id, trade_id, strategy_version, model_version, config_hash müssen durch Logs/DB/Sensoren konsistent sein.

## 13. News
SOURCE → FETCH → DEDUP → NORMALIZE → ENTITY → EVENT → DIRECTION → IMPACT → NOVELTY → CREDIBILITY → HORIZON → MARKET CONFIRMATION → OUTCOME → LEARNING. Nicht nur Sentiment.

## 14. Gemini
Für Event-/News-Interpretation, strukturierte Extraktion, Regime, Anomalien, Research, Post-Trade, Fehleranalyse, Hypothesen und Kalibrierung. Beispiel:
{
  "asset": "...",
  "event": "...",
  "direction": "bullish|bearish|neutral",
  "impact": 0.0,
  "confidence": 0.0,
  "time_horizon": "...",
  "novelty": 0.0,
  "market_confirmation": 0.0,
  "risk_flags": []
}
Gemini nie direkt als Kraken Order Authority.

## 15. Learning
Lerninputs: Trades, Nicht-Trades, Blocker, Rejections, Execution, Fills, Exits, News, Gemini, Regimes, Predictions, Forecasts, API-/Datenfehler. Jede Prediction bekommt später ein Outcome. Auch blockierte Trades counterfactual bewerten.

## 16. Calibration
Automatisch kalibrieren: probability, expected return/edge, confidence, regime probability, news/Gemini confidence, slippage, fill probability, volatility. Predicted vs realized überwachen.

## 17. Ensemble
Mögliche Modelle: trend, momentum, mean reversion, breakout, volatility, cross-sectional, news, regime, ML, Gemini information. Gewichte dynamisch innerhalb kontrollierter Grenzen.

## 18. Research/Validation
Hypothesen wie neue Features, Entry/Exit, News-Horizonte, Short-Filter, Leverage oder Liquidity Weighting getrennt evaluieren. Pflicht: chronologische Daten, Walk-Forward, OOS, realistische Kosten, Regime-Tests, Sensitivity, Downside Risk, Parameterstabilität, Overfitting-/Multiple-Testing-Kontrolle, Deflated Sharpe oder Äquivalent.

## 19. Model Registry/Rollback
Version, Parent, Hashes, Train/Validation/Test, Metrics, Regime Metrics, Cost-adjusted Metrics, Promotion und Rollback speichern. Active, Candidates und Baseline getrennt. Bei Degradation automatisch auf letzten stabilen Stand zurück.

## 20. Attribution/Counterfactuals
Trade zerlegen in Signal, Sizing, Leverage, Entry, Exit, Fees, Spread, Slippage, Funding, Regime, News, Gemini, Portfolio. Counterfactuals: no trade, andere Größe, andere Leverage, Long/Short, anderer Exit.

## 21. Fehler/Blocker
Fehlercodes: DATA_ERROR, MARKET_DATA_STALE, PRIVATE_DATA_STALE, API_ERROR, AUTH_ERROR, PERMISSION_ERROR, ORDER_REJECTED, INVALID_PRICE, INVALID_VOLUME, INSUFFICIENT_FUNDS, INSUFFICIENT_MARGIN, LIQUIDATION_RISK, STALE_DECISION, DUPLICATE_ORDER_RISK, NETWORK_AMBIGUITY, MODEL_ERROR, BAD_SIGNAL, BAD_EXIT, EXCESSIVE_SLIPPAGE, EXCESSIVE_SPREAD, NEWS_MISINTERPRETATION, GEMINI_MISINTERPRETATION, REGIME_MISCLASSIFICATION.

Blocker: BLOCKED_CONFIG, BLOCKED_API_PERMISSIONS, BLOCKED_KRAKEN_STATUS, BLOCKED_INSTRUMENT, BLOCKED_MARKET_DATA, BLOCKED_PRIVATE_DATA, BLOCKED_HISTORY, BLOCKED_NEWS, BLOCKED_GEMINI, BLOCKED_STRATEGY, BLOCKED_EXPECTED_EDGE, BLOCKED_COST, BLOCKED_LIQUIDITY, BLOCKED_MARGIN, BLOCKED_LEVERAGE, BLOCKED_RISK, BLOCKED_POSITION_SIZE, BLOCKED_PORTFOLIO, BLOCKED_ORDER_LIMIT, BLOCKED_RECONCILIATION, CIRCUIT_BREAKER, SAFE_STOP. FAILED nur bei unerwartetem technischem Fehler.

## 22. State Machines
Startup:
BOOT → CONFIG_VALIDATING → KRAKEN_AUTH_CHECK → PRODUCT_DISCOVERY → INSTRUMENT_SYNC → PUBLIC_DATA_CONNECT → PRIVATE_DATA_CONNECT → ACCOUNT_SYNC → PORTFOLIO_SYNC → HISTORY_BACKFILL → MODEL_INITIALIZATION → HEALTH_CHECK → READY

Trading:
CYCLE_START → MARKET_DISCOVERY → MARKET_FILTER → MARKET_SNAPSHOT → PORTFOLIO_SNAPSHOT → FEATURE_CALCULATION → REGIME_DETECTION → NEWS_ANALYSIS → GEMINI_ANALYSIS → SIGNAL_EVALUATION → COST_ESTIMATION → EXPECTED_EDGE → PORTFOLIO_TARGET → LEVERAGE_SELECTION → MARGIN_CHECK → RISK_CHECK → DECISION_CREATED → PRETRADE_CHECK → ORDER_SUBMITTING → RECONCILIATION → PORTFOLIO_UPDATED → OUTCOME_TRACKING → LEARNING_EVENT → CALIBRATION → CYCLE_COMPLETE

## 23. Recovery
WS disconnect → reconnect/resubscribe/reconcile.
Sequence gap → no new risk → REST reconcile → rebuild state → health check.
API timeout → controlled retry; ambiguous order → reconcile.
Gemini unavailable → configured degraded mode, never core crash.
News unavailable → reduced-information mode if allowed.
DB unavailable → no new risk if safe persistence impossible.
Portfolio mismatch → no new exposure.
Unknown order → reconcile before replacement.

## 24. Circuit Breaker
Triggers: auth/permission failure, Kraken incident, stale market/private data, sequence gap, portfolio mismatch, unknown order, extreme spread/slippage, reject/API errors, daily loss, drawdown, margin danger, abnormal volatility/execution. State = NO NEW RISK until recovery.

## 25. HA Configuration
Operational authority in HA. Suggested groups:
kraken: api_key, api_secret, enabled, live_enabled
gemini: api_key, enabled, model
market: discovery_interval, minimum_liquidity, max_spread, data_freshness
strategy: minimum_expected_edge, minimum_confidence, recalibration_interval
risk: max_position_risk, max_gross_exposure, max_net_exposure, max_margin, max_leverage, max_positions, daily_loss_limit, max_drawdown, cash_reserve
execution: max_slippage, order_timeout, max_reprices, max_orders_per_day
learning: enabled, lookback, validation_interval, auto_calibration, auto_promotion
sensor: enabled
Use current HA App/Supervisor conventions at implementation time.

## 26. Sensors
Binary: trading_enabled, system_ready, market_data_healthy, private_data_healthy, portfolio_consistent, model_ready, news_healthy, gemini_healthy, circuit_breaker, margin_safe.
Numeric: portfolio_equity, available_cash, available_margin, used_margin, gross_exposure, net_exposure, realized_pnl, unrealized_pnl, daily_pnl, drawdown, open_positions, open_orders, orders_today, current_leverage, average_slippage, average_latency, expected_edge.
Text: system_state, last_action, last_symbol, last_direction, last_blocker, last_trade, active_strategy, active_model, active_regime, last_news_event, gemini_status, learning_status.
Attributes bounded. Sensor publishing must not block trading.

## 27. Logging
stdout/stderr for HA logs, optional /data/logs/trader-events.jsonl. Never log API secrets, Gemini key, Supervisor token, auth headers or credentials. Event record: timestamp, level, event_code, stage, cycle_id, decision_id, intent_id, client_order_id, Kraken_order_id, symbol, actual, required, threshold, message, safe exception metadata.

Event codes mindestens:
BOOT_START, CONFIG_LOADED, CONFIG_INVALID, KRAKEN_AUTH_CHECK_STARTED, KRAKEN_AUTH_CHECK_OK, KRAKEN_AUTH_CHECK_FAILED, KRAKEN_PERMISSION_MISSING, KRAKEN_STATUS, PRODUCT_DISCOVERY_STARTED, PRODUCT_DISCOVERY_COMPLETED, INSTRUMENT_SYNC_STARTED, INSTRUMENT_SYNC_COMPLETED, INSTRUMENT_INVALID, MARKET_UNIVERSE_UPDATED, PUBLIC_WS_CONNECTED, PUBLIC_WS_DISCONNECTED, PUBLIC_DATA_STALE, PRIVATE_WS_CONNECTED, PRIVATE_WS_DISCONNECTED, PRIVATE_SEQUENCE_GAP, ACCOUNT_SYNC_STARTED, ACCOUNT_SYNC_COMPLETED, PORTFOLIO_RECONCILIATION_STARTED, PORTFOLIO_RECONCILIATION_OK, PORTFOLIO_RECONCILIATION_MISMATCH, CYCLE_START, CYCLE_END, UNIVERSE_FILTER, FEATURES_COMPUTED, REGIME_DETECTED, SIGNAL_CREATED, NEWS_FETCH, NEWS_ANALYSIS, GEMINI_REQUEST, GEMINI_RESPONSE, COST_ESTIMATE, EXPECTED_EDGE_BLOCKED, RISK_CHECK, MARGIN_CHECK, LEVERAGE_SELECTED, DECISION_CREATED, PRETRADE_CHECK, ORDER_SUBMITTING, ORDER_ACKNOWLEDGED, ORDER_RECONCILING, ORDER_STATUS_CHANGED, ORDER_PARTIAL_FILL, ORDER_FILL, ORDER_CANCELED, ORDER_EXPIRED, ORDER_REJECTED, ORDER_SUBMISSION_AMBIGUOUS, PORTFOLIO_UPDATED, LEARNING_EVENT, MODEL_EVALUATED, CALIBRATION_UPDATED, MODEL_PROMOTED, MODEL_ROLLBACK, CIRCUIT_BREAKER, SAFE_STOP, RECOVERY_STARTED, RECOVERY_COMPLETED, SENSOR_PUBLISH.

## 28. Datenbank
Logisch mindestens:
schema_meta, cycles, market_snapshots, instrument_metadata, portfolio_snapshots, portfolio_positions, decisions, decision_checks, orders, order_events, fills, positions, news_events, news_analysis, gemini_analysis, predictions, prediction_outcomes, learning_events, calibration_history, strategy_versions, model_versions, model_evaluations, risk_events, health_snapshots, app_events, error_events.
Finanzwerte mit Decimal.

## 29. No Order Spam
Vor Order: bestehende Position, offene Orders, pending intent, client_order_id, previous decision hash, target change, price movement, edge change, cooldown, replacements prüfen.

## 30. Performance
Track: gross/net PnL, fees, funding, Sharpe, Sortino, max drawdown, Expected Shortfall/CVaR, downside deviation, hit rate, payoff ratio, expectancy, turnover, fill/reject rate, slippage, latency, adverse selection, predicted-vs-realized edge, calibration, Performance nach Regime, Richtung, Leverage, Instrument, Execution, News, Gemini.

## 31. No-Trade
NO TRADE ist gültig. Gründe: Edge, Kosten, Liquidität, Risiko, Margin, Daten, Modellunsicherheit, Konzentration, Instrument, Mindestgröße, News-Unsicherheit.

## 32. Startkapital
Explizit für ca. 50 EUR testen. Mindestgrößen, Kosten, Leverage, Margin, Precision und unzureichendes Kapital korrekt behandeln. Keine stillen Überschreitungen.

## 33. Research-Hypothesen
Aktuelle Forschung als Hypothese prüfen, nicht als Profitgarantie: cost-aware signal filtering, regime/low-vol filtering, downside-risk portfolio construction, walk-forward robustness, backtest-overfitting controls, liquidity spillovers, execution latency/adverse selection, limit-order queue uncertainty, Momentum-Robustheit.

## 34. Implementierungsreihenfolge
Projekt/HA App → Auth → Capability Discovery → Instrument Registry → Market Data → Private Data → Portfolio/Reconciliation → Orders/Positions → Margin → Leverage → Short → Dynamic Scanner → Features → Regime → News → Gemini → History/Backfill → Learning → Calibration → Model Registry → Ensemble → Risk → Execution → Recovery → Sensors → Observability → E2E → CI → Final Audit.

## 35. Testmatrix
Core: config, auth, permissions, discovery, metadata, precision, minimums.
Trading: long, short, spot, margin, leverage, partial/full fills, cancellation, expiry, rejection, reversal.
Safety: insufficient funds/margin, liquidation risk, leverage, spread, slippage, stale data, sequence gap, portfolio mismatch, unknown/duplicate order, ambiguous network, daily loss, drawdown, circuit breaker.
AI: Gemini success/timeout/malformed output, news duplicate/unavailable, prediction outcomes, calibration, model evaluation/rollback.
Recovery: restart, WS disconnect, API timeout, DB issue, Kraken unavailable, sensor failure.
E2E: startup → discovery → analysis → news → Gemini → signal → risk → margin → leverage → order → fill → portfolio → outcome → learning → calibration → next cycle.

## 36. Acceptance
Nicht fertig ohne Nachweis:
- Markt-/Produktentdeckung ohne manuelle Symbolpflege
- neue Märkte automatisch berücksichtigt
- Long/Short
- Spot/Margin/Leverage/verfügbare Derivate
- risk-based sizing
- Kosten-/Execution-Modell
- News und Gemini mit auditierbarem Einfluss
- Lernen aus Trades und Nicht-Trades
- automatische Kalibrierung
- Modellpromotion/Rollback
- unveränderliche Sicherheitsgrenzen
- Order-Reconciliation
- Portfolio-Reconciliation
- Restart/WS/API-Recovery
- HA-Sensoren
- rekonstruierbare Logs
- keine Secrets in Logs
- keine GUI
- keine Legacy-Abhängigkeit
- keine versteckten Trading-Autoritäten
- grüne CI und E2E-Tests

## 37. Finaler Architekturtest
Beweise im Code und in Tests:
Kann das System den verfügbaren Kraken-Markt ohne manuelle Liste entdecken?
Kann es aktuelle Konto-/Produktberechtigungen bestimmen?
Kann es Long/Short selbst wählen?
Kann es Leverage innerhalb fester Grenzen wählen?
Kann es ein 50-EUR-Konto korrekt behandeln?
Kann es News/Gemini integrieren und deren Mehrwert messen?
Kann es aus Trades, Nicht-Trades und Fehlern lernen?
Kann es kalibrieren?
Kann es Kandidaten validieren und bei Degradation zurückrollen?
Kann es Neustarts, WS-Gaps und API-Probleme überleben?
Kann es unbekannte Orders sicher reconciliieren?
Kann es ohne GUI laufen?
Kann jeder Trade/Nicht-Trade aus Events und Logs rekonstruiert werden?
Kann kein Hintergrunddienst unabhängig Orders senden?
Wenn eine Antwort nein ist, ist der Neubau nicht fertig.
