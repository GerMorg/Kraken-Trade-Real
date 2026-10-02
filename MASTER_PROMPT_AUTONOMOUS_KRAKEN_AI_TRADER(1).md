# MASTER PROMPT — AUTONOMOUS KRAKEN AI TRADER

Du bist leitender Softwarearchitekt, Quant-Entwickler, Trading-System-Engineer und QA-Verantwortlicher. Baue eine vollständig neue Home-Assistant-App für autonomen Kraken-Realhandel.

## Ziel
Baue KEINE Weiterentwicklung von KTKI. Analysiere KTKI ausschließlich als Read-only-Referenz für Erkenntnisse, Fehler und Fallstricke. Schreibe den neuen Trader unabhängig von allen Legacy-Modulen, Versionen, GUIs und alten Runtime-Fassaden.

Ziel ist ein autonomes System:
DISCOVER → OBSERVE → ANALYZE → PREDICT → DECIDE → EXECUTE → RECONCILE → MEASURE → LEARN → CALIBRATE → VALIDATE → ADAPT → TRADE

Keine Profitgarantie. Optimiere robuste, kostenbereinigte, risikoadjustierte Erwartung unter festen Sicherheitsgrenzen.

## Handelsumfang ab Version 1
Von Anfang an:
- Kraken Spot
- Spot Margin
- Long
- Short
- Leverage
- Kraken Derivatives/Perpetuals, soweit für Konto, Instrument und aktuelle Regeln verfügbar

Kein manuell vorgegebenes Handelsuniversum. Die App entdeckt den gesamten erreichbaren Kraken-Markt dynamisch und bestimmt selbst, was aktuell handelbar und wirtschaftlich sinnvoll ist.

Ein Startkapital von ca. 50 EUR ist ein realer Testfall. Mindestgrößen, Gebühren, Margin, Präzision und wirtschaftliche Ausführbarkeit müssen automatisch berücksichtigt werden.

## Marktweite Selektion
Zuerst schneller Scan:
Instrumentstatus → Produkt-/Kontoberechtigung → Richtung/Margin/Leverage → Liquidität → Spread → Volumen → Datenqualität → Historie → Kosten → Risiko → Positionierbarkeit.

Danach Deep Analysis:
- Multi-Timeframe Returns
- Trend/Momentum
- ATR/Volatilität/Downside Volatility
- Volumen
- Spread/Depth/Orderbook/Imbalance
- Market Impact
- Relative Strength/Weakness
- Korrelationen
- Cross-Asset-/BTC-Regime
- Funding/Basis/Open Interest/Liquidationsdaten soweit verfügbar
- News
- Gemini
- Modellprognosen
- Unsicherheit
- historische Modellqualität
- Portfoliozusammenhang
- erwartete Ausführungskosten

Long und Short separat bewerten.

## News + Gemini
News sind Kernbestandteil:
FETCH → DEDUP → ENTITY → EVENT → DIRECTION → IMPACT → NOVELTY → CREDIBILITY → HORIZON → MARKET CONFIRMATION → OUTCOME → LEARNING.

Gemini ab Tag 1 für News/Eventanalyse, strukturierte Einschätzungen, Regimeinterpretation, Anomalien, Research, Fehleranalyse und Lern-/Kalibrierungsunterstützung.

Gemini darf niemals direkt Orders senden, Risk Limits ändern oder Circuit Breaker umgehen. Ausgaben strukturiert und validiert speichern. Deterministischer Trading-/Risk-/Execution-Layer bleibt letzte Instanz. Gemini- und News-Mehrwert muss später messbar sein.

## Vollautomatisches Lernen
Lerne aus:
- Trades und Nicht-Trades
- Signalen und Exits
- Positionsgröße und Leverage
- Slippage, Gebühren, Funding
- Rejections/API/WS/Datenfehlern
- News- und Gemini-Prognosen
- Regime-Klassifikationen
- erwarteter vs. realisierter Edge
- erwarteter vs. realisierter Slippage

Automatisch kalibrieren:
- Signalgewichte
- Confidence
- Expected Edge
- Regimewahrscheinlichkeiten
- News-/Gemini-Gewichte
- Slippage-/Fill-Modelle
- Entry-/Exit-Schwellen
- Positionsgrößen
- Leverage innerhalb der konfigurierten Grenze

Keine Änderung unveränderlicher Sicherheitsgrenzen. Keine Selbstmodifikation des Codes. Keine ungeprüfte Modell-Promotion.

## Research/Modelle
Research und Live Trading trennen. Kandidaten benötigen:
- chronologische Datenaufteilung
- Walk-Forward
- Out-of-Sample
- realistische Fees/Spread/Slippage/Funding
- Regime-Tests
- Robustheit/Sensitivity
- Downside-Risk
- Parameterstabilität
- Overfitting-/Multiple-Testing-Kontrolle
- Deflated-Sharpe oder gleichwertige Robustheitsprüfung

Active Model, Candidate Models und Baseline getrennt. Bei Degradation automatisch auf das letzte stabile Modell zurückfallen.

## Risk/Margin/Leverage
Risk ist letzte Instanz. Überwachen:
Equity, Cash, Used/Free Margin, Gross/Net Exposure, Notional, Initial/Maintenance Margin, Liquidation Distance, Volatilität, Downside Risk, CVaR/Expected Shortfall, Drawdown, Daily Loss, Korrelation/Cluster Exposure, Funding/Finanzierung, Kosten, Liquidität.

Leverage dynamisch wählen; 1x ist immer möglich. Starkes Signal kann wegen Kosten, Liquidität, Margin, Korrelation, Unsicherheit oder Drawdown trotzdem NO TRADE ergeben.

Unveränderliche Grenzen: Kill Switch, Max-Leverage, Max-Margin, Max-Position/Exposure, Tagesverlust, Drawdown, Datenqualität, unbekannter Orderstatus, Portfolio-Inkonsistenz.

## Execution
Eine zentrale Order Authority. Jede Order:
cycle_id, decision_id, intent_id, client_order_id, Kraken_order_id, strategy_version, model_version, config_hash.

Order-State:
INTENT_CREATED → PRECHECK_PASSED → SUBMITTING → ACKNOWLEDGED → LIVE → PARTIALLY_FILLED → FILLED/CANCELED/EXPIRED/REJECTED/UNKNOWN_RECONCILING.

Bei unklarer Netzwerkantwort niemals blind erneut senden. Über client_order_id, offene/geschlossene Orders, Query Order und private Execution Events reconciliieren.

Kraken-Instrumentmetadaten für IDs, Präzision, Mindestgrößen und Leverage verwenden; keine selbst konstruierten Pair-IDs.

Execution berücksichtigt Spread, Liquidität, Fill-Wahrscheinlichkeit, Slippage, Queue, Edge Decay und Dringlichkeit. Keine pauschale Market-Order-Strategie.

## Zentrale Runtime
Nur eine Trading Authority darf Cycles starten, finale Entscheidungen treffen, Risk freigeben, Order Intents erzeugen und Orders senden. News, Gemini, WebSockets, Learning und Sensoren dürfen niemals selbst Orders auslösen.

Startup:
BOOT → CONFIG_VALIDATING → KRAKEN_AUTH_CHECK → PRODUCT_DISCOVERY → INSTRUMENT_SYNC → PUBLIC_DATA → PRIVATE_DATA → ACCOUNT_SYNC → PORTFOLIO_SYNC → HISTORY/MODELS → HEALTH_CHECK → READY

Trading:
CYCLE_START → MARKET_DISCOVERY → FILTER → SNAPSHOT → FEATURES → REGIME → NEWS → GEMINI → SIGNALS → COST → EDGE → TARGET → LEVERAGE/MARGIN → RISK → DECISION → PRETRADE → ORDER → RECONCILIATION → PORTFOLIO → OUTCOME → LEARNING → CALIBRATION → COMPLETE

## Kein GUI
Keine Flask/Jinja/HTML/CSS-GUI, kein Ingress-Dashboard, keine Legacy-Webseiten. Home Assistant ist Konfiguration und Statusoberfläche. Konfiguration ausschließlich aus HA; Status über Sensoren; Detaildiagnostik über strukturierte Logs. Sensoren dürfen den Trading-Core nicht blockieren.

## Fehler/Self-Healing
`BLOCKED`, `SKIPPED`, `NO_ACTION`, `REJECTED` und `FAILED` semantisch trennen. `FAILED` nur bei unerwartetem technischem Fehler.

Blocker mindestens:
BLOCKED_API_PERMISSIONS, BLOCKED_INSTRUMENT, BLOCKED_MARKET_DATA, BLOCKED_PRIVATE_DATA, BLOCKED_EXPECTED_EDGE, BLOCKED_COST, BLOCKED_LIQUIDITY, BLOCKED_MARGIN, BLOCKED_LEVERAGE, BLOCKED_RISK, BLOCKED_POSITION_SIZE, BLOCKED_PORTFOLIO, BLOCKED_RECONCILIATION, CIRCUIT_BREAKER, SAFE_STOP.

WS reconnect, Sequence-Gap-Reconciliation, REST Recovery, stale-data handling, kontrollierte API-Retries, Portfolio-Reconciliation und Unknown-Order-Recovery implementieren. Bei Unsicherheit kein neues Risiko.

## Observability
Jeder Trade und Nicht-Trade muss rekonstruierbar sein. Strukturierte Logs mit:
app_instance_id, cycle_id, decision_id, intent_id, client_order_id, Kraken_order_id, symbol, stage, event_code, actual, required, threshold, timestamp.

Keine Secrets loggen.

Dokumentiere:
- warum gehandelt/nicht gehandelt wurde
- aktives Modell
- relevante News/Gemini-Information
- Risiko und Leverage
- Execution/Fills
- Lernresultat

## Datenbank
Mindestens logisch:
schema_meta, cycles, market_snapshots, instrument_metadata, portfolio_snapshots, portfolio_positions, decisions, decision_checks, orders, order_events, fills, positions, news_events, news_analysis, gemini_analysis, predictions, prediction_outcomes, learning_events, calibration_history, strategy_versions, model_versions, model_evaluations, risk_events, health_snapshots, app_events, error_events.

Finanzwerte mit Decimal. Kraken ist Konto-Wahrheit; DB ist Historie/Materialisierung.

## Home-Assistant-Sensoren
Binary:
trading_enabled, system_ready, market_data_healthy, private_data_healthy, portfolio_consistent, model_ready, news_healthy, gemini_healthy, circuit_breaker, margin_safe.

Numerisch:
equity, cash, available_margin, used_margin, gross_exposure, net_exposure, realized_pnl, unrealized_pnl, daily_pnl, drawdown, open_positions, open_orders, orders_today, current_leverage, average_slippage, average_latency, expected_edge.

Text:
system_state, last_action, last_symbol, last_direction, last_blocker, last_trade, active_strategy, active_model, active_regime, last_news_event, gemini_status, learning_status.

## Entwicklung
Vor dem Programmieren:
1. KTKI vollständig analysieren.
2. Fehler, Fehlpfade, gute Erkenntnisse und Risiken dokumentieren.
3. neue Architektur entwerfen.
4. Legacy isolieren.
5. neue App implementieren.
6. vollständige Tests.
7. End-to-End-Prüfung.
8. CI vollständig grün.
9. Fehler beheben und erneut testen.
10. erst dann Main-Integration.

## Definition of Done
Nachweislich:
- selbstständige Markt-/Produktentdeckung
- dynamisches Universum ohne manuelle Symbolpflege
- Long + Short
- Spot + Margin + Leverage + verfügbare Derivate
- News + Gemini
- Lernen aus Trades und Nicht-Trades
- automatische Kalibrierung
- getestete Modellpromotion/Rollback
- Kosten-/Slippage-aware Trading
- Margin-/Liquidationsschutz
- Order-Reconciliation
- Neustart/API/WS-Recovery
- Portfolio-Konsistenz
- Automatisches Portfolio Rebalancing
- keine GUI
- HA-Konfiguration/Sensoren
- vollständige Logs
- keine Legacy-Abhängigkeiten
- keine versteckten Trading-Autoritäten
- grüne CI
- grüne End-to-End-Tests

Lies zuerst `AUTONOMOUS_TRADER_SPEC.md` vollständig. Sie ist verbindlicher Bestandteil dieses Auftrags und enthält die Detailanforderungen, State Machines, Datenmodelle, Events, Lernlogik und Testmatrix.

Bei Konflikten entscheide zugunsten von Sicherheit, deterministischer Nachvollziehbarkeit und der zentralen Trading Authority.

Arbeite bis zum getesteten Ergebnis, nicht nur bis zu Vorschlägen.
