Du bist der leitende Softwarearchitekt, Quant-Entwickler, Trading-System-Engineer und QA-Verantwortliche für den vollständigen Neubau einer autonomen Home-Assistant-App für automatisierten Kraken-Realhandel.

## 1. Ziel

Baue keine Weiterentwicklung des bestehenden KTKI-Programms.

Baue eine vollständig neue, saubere, autonome Trading-Anwendung von Grund auf.

Die bestehende KTKI-Anwendung darf ausschließlich als technische und fachliche Referenz analysiert werden, um bekannte Fehler, Fehlpfade und bereits vorhandene Erkenntnisse zu verstehen.

Der neue Trader muss unabhängig von sämtlichen alten Modulen, Versionen, Legacy-Fassaden und bisherigen Architekturentscheidungen funktionieren.

Das oberste Ziel lautet:

**Ein autonomes Trading-System, das selbstständig den verfügbaren Kraken-Markt entdeckt, geeignete Märkte identifiziert, Long-, Short-, Margin-, Hebel- und Spot-Positionen verwaltet, News und Gemini-Analysen einbezieht, aus vergangenen Trades und Fehlern lernt, seine Modelle automatisch neu kalibriert und ohne manuellen Wartungsaufwand dauerhaft betrieben werden kann.**

Dabei darf das System niemals Profitabilität versprechen oder voraussetzen.

Es soll stattdessen systematisch versuchen, einen positiven risikoadjustierten Erwartungswert zu finden und diesen durch Lernen, Kostenkontrolle, Ausführung und Risikomanagement zu verbessern.

---

# 2. Grundprinzipien

Die Anwendung muss:

- vollständig autonom arbeiten
- selbstständig den handelbaren Markt entdecken
- selbstständig Instrumente aktualisieren
- selbstständig Chancen erkennen
- selbstständig Long oder Short wählen
- selbstständig zwischen Spot, Spot Margin und Derivaten wählen
- selbstständig Leverage bestimmen
- selbstständig Positionsgrößen berechnen
- selbstständig Orders eröffnen, ändern und schließen
- selbstständig Stop-/Exit-Logik anwenden
- selbstständig Portfolio und offene Positionen rekonstruieren
- selbstständig Fehler erkennen
- selbstständig aus Fehlern lernen
- selbstständig Modelle und Parameter neu kalibrieren
- selbstständig Datenqualität überwachen
- selbstständig bei Problemen in einen sicheren Zustand wechseln
- nach temporären Fehlern selbstständig recovery durchführen
- selbstständig neue Kraken-Märkte erkennen
- selbstständig nicht mehr handelbare Märkte aussortieren
- selbstständig News sammeln und bewerten
- Gemini automatisch in den Analyse- und Lernprozess integrieren
- selbstständig Performance analysieren
- selbstständig feststellen, welche Strategiekomponenten funktionieren und welche nicht
- alte Erkenntnisse nicht verlieren
- bei schlechter Modellqualität automatisch auf robustere frühere Modelle zurückfallen

Der Benutzer muss nicht täglich Parameter ändern.

---

# 3. Kein GUI

Es gibt keine klassische Web-GUI.

Keine:

- Flask-Oberfläche
- Jinja-Seiten
- HTML-Dashboards
- CSS
- Ingress-Webseiten
- Konfigurationsseiten
- unnötige Statusseiten
- Paper-Trading-Seiten
- Lern-Webseiten
- Tax-Webseiten
- alte GUI-Kompatibilität

Home Assistant ist die Bedien- und Statusoberfläche.

Konfiguration erfolgt über Home Assistant.

Status erfolgt über Home-Assistant-Sensoren.

Detaildiagnostik erfolgt über strukturierte App-Logs.

---

# 4. Home Assistant als einzige Konfigurationsautorität

Die operative Konfiguration darf ausschließlich aus der Home-Assistant-Konfiguration kommen.

Keine zweite widersprüchliche Datenbank-Konfiguration.

Keine versteckten Defaultwerte, die wichtige Benutzerparameter überschreiben.

Die Datenbank darf historische Zustände, Lernparameter, Modellversionen und Laufzeitinformationen speichern, aber nicht als zweite konkurrierende Konfigurationsquelle dienen.

Konfigurierbar müssen unter anderem sein:

- Kraken API Key
- Kraken API Secret
- Gemini API Key
- Trading aktiv/inaktiv
- Real Trading Kill Switch
- erlaubte Kraken-Produkte, sofern optional vom Benutzer beschränkbar
- maximale Gesamt-Exposure
- maximales Risiko pro Position
- maximale Gesamt-Margin
- maximale Leverage
- maximale Anzahl gleichzeitiger Positionen
- minimale Liquidität
- minimale erwartete Netto-Rendite
- maximale Gebühren
- maximale Slippage
- Mindest-Confidence
- News-Gewichtung
- Gemini-Gewichtung
- Recalibration-Intervall
- Lernfenster
- Backfill-Zeitraum
- Circuit-Breaker
- Verlustgrenzen
- Drawdown-Grenzen
- Orderlimits
- Daten-Frische
- Zyklusintervall
- Logging-Level

Die Anwendung darf sich niemals selbstständig sicherheitsrelevante Obergrenzen erhöhen.

Sie darf jedoch innerhalb der konfigurierten Grenzen automatisch optimieren.

---

# 5. Kraken-Produkte

Der Trader muss von Anfang an eine Multi-Venue-/Multi-Product-Architektur besitzen.

Mindestens:

1. Kraken Spot
2. Kraken Spot Margin
3. Kraken Derivatives / Perpetuals bzw. weitere für das Konto verfügbare Kraken-Derivate

Spot Margin muss Long und Short unterstützen.

Derivatetrading muss Long und Short unterstützen.

Leverage muss Bestandteil des zentralen Risiko- und Positionsmodells sein.

Die konkrete Verfügbarkeit muss niemals statisch angenommen werden.

Sie muss anhand der aktuellen Kraken-Daten und der Berechtigungen des konkreten Kontos ermittelt werden.

---

# 6. Dynamisches Marktuniversum

Der Benutzer gibt kein fixes Handelsuniversum vor.

Die App muss beim Start und regelmäßig danach den gesamten für sie erreichbaren Kraken-Markt scannen.

Der Prozess:

DISCOVER ALL KRAKEN INSTRUMENTS

→ REMOVE UNSUPPORTED PRODUCTS

→ REMOVE INELIGIBLE MARKETS

→ REMOVE ACCOUNT-INELIGIBLE MARKETS

→ REMOVE NON-TRADEABLE STATUS

→ REMOVE INSUFFICIENT LIQUIDITY

→ REMOVE INVALID MARKET DATA

→ REMOVE INSUFFICIENT HISTORY

→ REMOVE EXCESSIVE SPREAD

→ REMOVE EXCESSIVE EXECUTION COST

→ REMOVE INSUFFICIENT POSITION SIZE FEASIBILITY

→ REMOVE EXCESSIVE RISK

→ RANK REMAINING INSTRUMENTS

→ DETAIL ANALYSIS

Das dynamische Universum darf sich jederzeit ändern.

Neue Kraken-Instrumente müssen automatisch erkannt werden.

Entfernte oder pausierte Instrumente müssen automatisch verschwinden.

---

# 7. Instrument Registry

Für jedes Instrument wird eine aktuelle Registry aufgebaut.

Speichern:

- venue
- product type
- symbol
- Kraken identifier
- altname
- base asset
- quote asset
- contract type
- margin availability
- long availability
- short availability
- maximum leverage
- available leverage tiers
- minimum order size
- minimum cost
- lot precision
- price precision
- tick size
- position limits
- margin class
- collateral requirements
- funding parameters
- fee model
- instrument status
- liquidity
- tradable status
- last metadata update

Niemals eigene Paar-IDs aus Symbolstrings zusammensetzen.

Immer die aktuelle Kraken-Instrumentdefinition verwenden.

---

# 8. Zwei Analysephasen

Die Marktanalyse erfolgt in zwei Ebenen.

## Phase A – Market Discovery

Sehr schneller Scan des gesamten verfügbaren Marktes.

Für jedes Instrument werden nur günstige, schnelle Merkmale berechnet:

- Liquidität
- Spread
- Volumen
- Volumenänderung
- kurzfristige Rendite
- mittelfristige Rendite
- Volatilität
- Trend
- Momentum
- relative Stärke
- Orderbook-Imbalance
- Marktbreite
- Cross-Asset-Regime
- BTC-Regime
- News-Aktivität
- News-Richtung
- Liquiditätsstress
- geschätzte Ausführungskosten

Nur ein kleiner Teil wird zur Detailanalyse weitergereicht.

## Phase B – Deep Analysis

Für die priorisierten Instrumente:

- Multi-Timeframe-Analyse
- technische Features
- Marktstruktur
- Trendqualität
- Mean-Reversion-Wahrscheinlichkeit
- Momentumqualität
- Breakout-Wahrscheinlichkeit
- Volatilitätsregime
- Liquiditätsregime
- Orderbook
- erwartete Slippage
- Kosten
- News
- Gemini-Interpretation
- Cross-Asset-Abhängigkeiten
- Modellprognosen
- Unsicherheit
- historische Modellqualität
- aktuelle Positionssituation
- Portfoliozusammenhang

---

# 9. Long und Short als gleichwertige Möglichkeiten

Jedes geeignete Instrument muss separat auf beide Richtungen untersucht werden.

LONG SCORE

SHORT SCORE

nicht nur:

BUY / HOLD / SELL

Die Entscheidung soll beispielsweise zwischen folgenden Zuständen erfolgen:

- NO_POSITION
- OPEN_LONG
- OPEN_SHORT
- INCREASE_LONG
- REDUCE_LONG
- CLOSE_LONG
- INCREASE_SHORT
- REDUCE_SHORT
- CLOSE_SHORT
- REVERSE_LONG_TO_SHORT
- REVERSE_SHORT_TO_LONG

Eine Short-Position darf nicht einfach als „SELL“ eines Long-Portfolios behandelt werden.

Short benötigt eigene:

- Kosten
- Borrow/Margin-Mechanik
- Liquidationsrisiken
- Funding-/Finanzierungskosten
- Entry-Logik
- Exit-Logik
- Stop-Logik
- Performancebewertung

---

# 10. Leverage Engine

Leverage darf niemals ein statischer Parameter sein.

Die Anwendung muss die optimale Leverage innerhalb der konfigurierten Sicherheitsgrenzen dynamisch bestimmen.

Eingaben:

- Signalqualität
- erwartete Rendite
- erwartete Netto-Rendite
- Volatilität
- Liquidität
- Spread
- Slippage
- Drawdown
- aktuelle Portfolio-Risikobelastung
- Korrelationen
- Margin Utilization
- Liquidationsabstand
- Modellqualität
- Regime
- News Risk
- Gemini Confidence
- historische Performance des jeweiligen Modells

Bei schlechter Qualität sinkt der Leverage automatisch.

Bei hoher Unsicherheit darf kein erhöhter Leverage eingesetzt werden.

Leverage 1x ist jederzeit eine gültige Wahl.

Bei extremen Risiken muss die Entscheidung automatisch auf:

NO TRADE

zurückfallen.

---

# 11. Margin Risk Engine

Margin muss als eigenständiger Risikobereich behandelt werden.

Überwachen:

- Equity
- Used Margin
- Free Margin
- Maintenance Margin
- Initial Margin
- Margin Ratio
- Liquidation Distance
- Position Notional
- Collateral
- unrealized PnL
- realized PnL
- funding/financing costs
- open-order margin impact

Die App darf niemals eine neue Position eröffnen, wenn dadurch ein definierter Sicherheitsabstand zur Liquidation verletzt wird.

Bei kritischer Margin:

1. neue Entries stoppen
2. Risiko reduzieren
3. gefährdete Positionen priorisieren
4. nötigenfalls Positionen teilweise schließen
5. Portfolio rekonstruieren
6. System erst nach Wiederherstellung stabiler Marginbedingungen wieder freigeben

---

# 12. Positionsgröße

Die Positionsgröße wird nicht direkt aus einem statischen Prozentsatz berechnet.

Sie basiert auf:

- erwarteter Netto-Rendite
- erwarteter Verlustverteilung
- Volatilität
- Downside Volatility
- Sortino
- Maximum Drawdown
- Expected Shortfall / CVaR
- Liquidität
- Spread
- Slippage
- Modellqualität
- Regime
- News Risk
- Korrelation
- aktueller Portfolio-Exposure
- Leverage
- Margin
- verbleibendem Risikobudget

Kleine Konten müssen besonders strikt behandelt werden.

Bei 50 EUR kann die technisch mögliche Position aufgrund von Kraken-Mindestgrößen, Gebühren und Marginbedingungen unhandelbar sein.

Das muss die Engine automatisch erkennen und sauber als

BLOCKED_INSTRUMENT
oder
BLOCKED_POSITION_SIZE

melden.

Nicht als ERROR.

---

# 13. Kostenmodell

Jede Entscheidung muss netto nach Kosten berechnet werden.

Einbeziehen:

- Trading Fees
- Maker/Taker
- Spread
- Slippage
- Market Impact
- Funding
- Margin-Finanzierung
- relevante Währungsumrechnungskosten
- Sicherheitsmarge

Berechnung:

EXPECTED GROSS EDGE

minus FEES

minus SPREAD

minus SLIPPAGE

minus MARKET IMPACT

minus FUNDING

minus FINANCING

minus SAFETY BUFFER

=

EXPECTED NET EDGE

Nur wenn der erwartete Nettoedge ausreichend positiv ist, darf ein neuer Trade entstehen.

---

# 14. News Engine

News müssen ein nativer Bestandteil des Systems sein.

Die News Engine soll automatisch:

- Nachrichtenquellen erfassen
- neue Nachrichten erkennen
- Duplikate entfernen
- Nachrichten clustern
- Assets erkennen
- Unternehmen/Projekte/Personen erkennen
- Ereignisse klassifizieren
- Richtung bestimmen
- erwarteten Impact bestimmen
- zeitliche Relevanz bestimmen
- Neuigkeit bestimmen
- Glaubwürdigkeit bewerten
- Unsicherheit bestimmen
- Marktreaktion messen

News sollen nicht einfach einen simplen BUY/SELL-Score erzeugen.

Stattdessen:

NEWS EVENT

→ ENTITY

→ EVENT TYPE

→ EXPECTED IMPACT

→ DIRECTION

→ CONFIDENCE

→ EXPECTED DURATION

→ MARKET CONFIRMATION

→ ACTUAL OUTCOME

→ LEARNING RECORD

---

# 15. Gemini AI

Gemini wird von Anfang an integriert.

Gemini darf eingesetzt werden für:

- News-Analyse
- Ereignisklassifikation
- Zusammenfassung
- Entity Recognition
- Impact-Schätzung
- Sentiment
- Regime Interpretation
- Hypothesenbildung
- Anomalieinterpretation
- Analyse von Fehlentscheidungen
- Performanceanalyse
- Mustererkennung
- Vorschläge für neue Features
- Vorschläge für Modelländerungen
- automatische Research-Aufgaben
- automatische Lernberichte

Gemini-Ausgaben müssen strukturiert und validiert werden.

Beispiel:

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

Freitext darf niemals direkt als Tradingentscheidung verwendet werden.

---

# 16. Gemini darf kein unkontrollierter Order-Agent sein

Gemini darf niemals selbstständig:

- API Calls zu Kraken durchführen
- Ordergrößen festlegen und direkt senden
- Risk Limits erhöhen
- Leverage Limits überschreiben
- Circuit Breaker deaktivieren
- API-Sicherheitsprüfungen umgehen

Gemini liefert Informationen und strukturierte Hypothesen.

Die zentrale Trading Engine entscheidet anschließend anhand aller Informationen.

---

# 17. Event-driven Learning

Jeder relevante Vorgang muss lernfähig sein.

Die App speichert:

- Marktbedingungen
- Features
- Signal
- Modellprognose
- News
- Gemini-Bewertung
- Entscheidung
- erwartete Rendite
- tatsächliche Rendite
- Gebühren
- Slippage
- Ausführungsqualität
- Haltedauer
- Exit-Grund
- ursprüngliches Risiko
- tatsächliches Risiko
- Fehler
- Blockierungen
- Kraken-Rejections
- API-Probleme
- Datenprobleme

Jede Entscheidung bekommt anschließend ein Outcome.

---

# 18. Sofortiges Lernen aus Fehlern

Alle Fehler müssen klassifiziert werden.

Beispielsweise:

DATA_ERROR
MARKET_DATA_STALE
API_ERROR
AUTH_ERROR
PERMISSION_ERROR
ORDER_REJECTED
INVALID_PRICE
INVALID_VOLUME
INSUFFICIENT_FUNDS
INSUFFICIENT_MARGIN
LIQUIDATION_RISK
STALE_DECISION
DUPLICATE_ORDER_RISK
NETWORK_AMBIGUITY
MODEL_ERROR
BAD_SIGNAL
BAD_EXIT
EXCESSIVE_SLIPPAGE
EXCESSIVE_SPREAD
NEWS_MISINTERPRETATION
GEMINI_MISINTERPRETATION
REGIME_MISCLASSIFICATION

Aus jedem Fehler wird ein Lernereignis erzeugt.

Beispiel:

FEHLER

→ Ursache

→ Situation

→ vorhergesagte Wahrscheinlichkeit

→ tatsächlicher Ausgang

→ mögliche Ursache

→ Modellbewertung

→ Kalibrierungsdaten

→ zukünftige Gewichtungsänderung

---

# 19. Kontinuierliche Kalibrierung

Die Anwendung soll sich selbst kontinuierlich neu kalibrieren.

Aber:

**Parameteroptimierung darf nicht mit unkontrollierter Selbstprogrammierung verwechselt werden.**

Die App darf innerhalb definierter Grenzen automatisch:

- Signalgewichte anpassen
- Confidence-Kalibrierung anpassen
- Regimewahrscheinlichkeiten anpassen
- News-Gewichte anpassen
- Leverage innerhalb des Limits anpassen
- Positionsgrößenmodelle kalibrieren
- Entry Thresholds anpassen
- Exit Thresholds anpassen
- Kostenpuffer kalibrieren
- Slippage-Schätzungen kalibrieren
- Modellensemble-Gewichte anpassen

---

# 20. Keine unkontrollierte Auto-Promotion

Ein neues Modell darf nicht deshalb live gehen, weil ein Backtest besser aussieht.

Jede neue Modellversion benötigt automatisch:

- zeitlich saubere Datenaufteilung
- Walk-Forward-Validation
- Out-of-Sample-Test
- Cost-adjusted Evaluation
- Slippage-Simulation
- Regime-Segmentierung
- Downside Risk Evaluation
- Drawdown Evaluation
- Robustheitstest
- Parameter Stability Test
- Sensitivity Test
- Bootstrap oder vergleichbare Unsicherheitsanalyse
- Multiple-Testing-Kontrolle
- Overfitting-Prüfung

Erst danach darf das Modell als Kandidat bewertet werden.

---

# 21. Shadow Evaluation ohne Shadow Trading

Es gibt kein klassisches Shadow Trading GUI.

Modelle können intern parallel bewertet werden.

Beispiel:

ACTIVE_MODEL
CANDIDATE_MODEL_A
CANDIDATE_MODEL_B
BASELINE_MODEL

Alle erzeugen hypothetische Entscheidungen.

Nur ACTIVE_MODEL darf neue Live-Orders beeinflussen.

Kandidaten sammeln Performance.

Bei ausreichender Evidenz kann das System einen Kandidaten automatisch als neuen Kandidaten für die Aktivierung auswählen.

Die Aktivierung muss aber zwingend durch einen deterministischen Promotion-Gate abgesichert werden.

---

# 22. Automatische Rollbacks

Jede Modellversion, Strategieversion und Kalibrierung bekommt:

- version_id
- parent_version
- config_hash
- model_hash
- training_window
- validation_window
- promotion_time
- performance_snapshot

Bei statistisch signifikanter Verschlechterung:

NEW MODEL

→ MONITOR

→ DEGRADATION DETECTED

→ FALLBACK TO PREVIOUS STABLE MODEL

Das muss automatisch funktionieren.

---

# 23. Lernen aus dem kompletten historischen Bestand

Beim ersten Start darf die App nicht bei null beginnen.

Sie soll, soweit technisch und API-seitig verfügbar:

- historische Kraken-Marktdaten
- historische Trades
- historische Preisbewegungen
- historische Orderbuchdaten
- historische Portfolioentwicklung
- historische eigene Orderdaten
- historische Fehlermeldungen
- historische News-Daten

aufbauen.

Besonders wichtig:

Der bisherige KTKI-Bestand darf fachlich untersucht werden.

Die neue App darf daraus Erkenntnisse übernehmen.

Sie darf jedoch keinen alten KTKI-Code importieren.

---

# 24. Backfill

Bei Erstinstallation:

START

→ DISCOVER MARKETS

→ DISCOVER AVAILABLE DATA

→ BACKFILL HISTORY

→ CALCULATE FEATURES

→ DETECT REGIMES

→ PROCESS NEWS

→ BUILD TRAINING DATA

→ INITIALIZE MODELS

→ CALIBRATE

→ VALIDATE

→ CREATE INITIAL MODEL

→ CONNECT LIVE

→ RECONCILE ACCOUNT

→ READY

Wenn historisches Material nicht verfügbar ist, muss die Anwendung trotzdem starten und dies explizit protokollieren.

---

# 25. Kein Leerlauf-Lernen

Lernen darf auch im laufenden Betrieb stattfinden.

Jeder geschlossene Trade liefert neue Daten.

Jeder nicht ausgeführte Trade kann später ausgewertet werden.

Jede Marktprognose bekommt später ein Ergebnis.

Jede News-Prognose bekommt später ein Ergebnis.

Jede Gemini-Prognose bekommt später ein Ergebnis.

Damit entsteht ein kontinuierlich wachsender Datensatz:

PREDICTION → OUTCOME → ERROR → CALIBRATION

---

# 26. Lernen aus NICHT gehandelten Chancen

Nicht nur ausgeführte Trades analysieren.

Die App muss auch untersuchen:

- welche Signale keinen Trade ausgelöst haben
- warum sie blockiert wurden
- was anschließend mit dem Markt passiert ist
- ob ein Trade tatsächlich sinnvoll gewesen wäre
- ob die Blockierung richtig oder falsch war

Dadurch können auch Entry Thresholds und Risk Gates kalibriert werden.

---

# 27. Regime Detection

Das System muss Märkte dynamisch klassifizieren:

TREND_UP
TREND_DOWN
RANGE
HIGH_VOLATILITY
LOW_VOLATILITY
LIQUIDITY_STRESS
PANIC
RECOVERY
BREAKOUT
MEAN_REVERSION
UNKNOWN

Regimes sollen nicht nur global für den Markt gelten.

Es soll mindestens geben:

GLOBAL REGIME

ASSET REGIME

SECTOR/CLUSTER REGIME

INSTRUMENT REGIME

---

# 28. Multi-Factor Signal Engine

Features müssen mindestens berücksichtigen:

- Multi-Timeframe Returns
- Momentum
- Trend
- EMA/SMA slopes
- ATR
- realized volatility
- downside volatility
- volume
- volume anomaly
- spread
- orderbook imbalance
- depth
- liquidity
- market impact
- relative strength
- cross-sectional strength
- correlation
- BTC regime
- market breadth
- funding
- basis
- open interest, soweit verfügbar
- liquidation data, soweit verfügbar
- news
- news reaction
- Gemini interpretation
- model forecasts
- uncertainty

Nicht jeder Faktor muss immer aktiv sein.

Die Lernengine soll empirisch bestimmen, welche Faktoren in welchen Regimes nützlich sind.

---

# 29. Long/Short Asymmetry

Das System muss davon ausgehen, dass Long und Short nicht spiegelbildlich funktionieren.

Deshalb getrennte Modelle bzw. Parameterbereiche:

LONG MODEL

SHORT MODEL

LONG EXECUTION MODEL

SHORT EXECUTION MODEL

LONG COST MODEL

SHORT COST MODEL

LONG RISK MODEL

SHORT RISK MODEL

---

# 30. Portfolio Optimization

Die Zielposition ergibt sich aus:

SIGNAL

× EXPECTED NET EDGE

× MODEL QUALITY

× CALIBRATION

× REGIME QUALITY

× LIQUIDITY

× RISK BUDGET

× PORTFOLIO CONTEXT

× DOWNSIDE RISK

nicht nur aus Signalstärke.

Berücksichtige:

- Cash Reserve
- Gesamt-Exposure
- Long Exposure
- Short Exposure
- Gross Exposure
- Net Exposure
- Margin Utilization
- Per-Asset Concentration
- Correlation Clusters
- Sector/Theme Exposure
- Drawdown
- Daily Loss
- Tail Risk

---

# 31. Risiko ist wichtiger als Signal

Ein starkes Signal darf nicht automatisch zu einem Trade führen.

Beispiel:

SIGNAL = sehr stark

aber

LIQUIDITY = schlecht

oder

SLIPPAGE = zu hoch

oder

MARGIN RISK = hoch

oder

NEWS EVENT = extrem unsicher

oder

PORTFOLIO CORRELATION = zu hoch

→ NO TRADE

---

# 32. Execution Intelligence

Orders werden nicht einfach blind als Market Order gesendet.

Die Execution Engine entscheidet abhängig von:

- Spread
- Book depth
- Volatility
- urgency
- expected edge decay
- fill probability
- queue quality
- slippage
- order size
- liquidity
- direction

möglicherweise zwischen:

- passive limit
- post-only limit
- marketable limit
- market order
- amend
- cancel/reprice

Market Orders sind nicht automatisch verboten, aber müssen wirtschaftlich begründet sein.

---

# 33. Keine Order-Duplikate

Jede Order erhält:

- cycle_id
- decision_id
- intent_id
- client_order_id
- Kraken_order_id
- strategy_version
- model_version
- config_hash

Bei unklarer Netzwerkantwort darf niemals blind erneut gesendet werden.

Stattdessen:

RECONCILE BY CLIENT ORDER ID

→ OPEN ORDERS

→ CLOSED ORDERS

→ QUERY ORDER

→ PRIVATE EXECUTION EVENTS

→ DETERMINE ACTUAL STATE

Erst danach darf über einen neuen Submit entschieden werden.

---

# 34. Zentrale State Machine

Startup:

BOOT
→ CONFIG_VALIDATING
→ KRAKEN_AUTH_CHECK
→ PRODUCT_DISCOVERY
→ INSTRUMENT_SYNC
→ PUBLIC_DATA_CONNECT
→ PRIVATE_DATA_CONNECT
→ ACCOUNT_SYNC
→ PORTFOLIO_SYNC
→ HISTORICAL_BACKFILL
→ MODEL_INITIALIZATION
→ SYSTEM_HEALTH_CHECK
→ READY

Trading cycle:

CYCLE_START
→ MARKET_DISCOVERY
→ MARKET_FILTER
→ MARKET_SNAPSHOT
→ PORTFOLIO_SNAPSHOT
→ FEATURE_CALCULATION
→ REGIME_DETECTION
→ NEWS_ANALYSIS
→ GEMINI_ANALYSIS
→ SIGNAL_EVALUATION
→ COST_ESTIMATION
→ EXPECTED_EDGE
→ POSITION_SIZING
→ LEVERAGE_SELECTION
→ PORTFOLIO_RISK
→ MARGIN_RISK
→ PRETRADE_CHECK
→ DECISION
→ ORDER_INTENT
→ ORDER_SUBMISSION
→ RECONCILIATION
→ POSITION_UPDATE
→ OUTCOME_TRACKING
→ LEARNING_EVENT
→ CALIBRATION
→ CYCLE_COMPLETE

---

# 35. Blocker-System

Jeder abgebrochene oder nicht ausgeführte Prozess muss einen eindeutigen Grund besitzen.

Beispiel:

BLOCKED_CONFIG
BLOCKED_API_PERMISSIONS
BLOCKED_KRAKEN_STATUS
BLOCKED_INSTRUMENT
BLOCKED_MARKET_DATA
BLOCKED_PRIVATE_DATA
BLOCKED_HISTORY
BLOCKED_NEWS
BLOCKED_GEMINI
BLOCKED_STRATEGY
BLOCKED_EXPECTED_EDGE
BLOCKED_COST
BLOCKED_LIQUIDITY
BLOCKED_MARGIN
BLOCKED_LEVERAGE
BLOCKED_RISK
BLOCKED_POSITION_SIZE
BLOCKED_PORTFOLIO
BLOCKED_ORDER_LIMIT
BLOCKED_RECONCILIATION
CIRCUIT_BREAKER
SAFE_STOP

`FAILED` darf nur bei einem tatsächlichen unerwarteten technischen Fehler verwendet werden.

---

# 36. Self-Healing

Die Anwendung muss sich soweit technisch möglich selbst reparieren.

Beispiele:

WebSocket Disconnect
→ reconnect

Sequence Gap
→ REST reconciliation

Stale Market Data
→ reconnect/resubscribe

Kraken API timeout
→ controlled retry

Gemini timeout
→ continue trading without Gemini only if strategy policy permits

News provider unavailable
→ continue with reduced information mode

Database failure
→ stop opening new risk if state cannot be safely persisted

Unknown order
→ reconcile before proceeding

Portfolio mismatch
→ stop new exposure

---

# 37. Private Account Truth

Die Wahrheit über das reale Konto kommt von Kraken.

Insbesondere:

- balances
- open positions
- open orders
- executions
- fills
- fees
- realized PnL
- margin
- collateral

Die lokale Datenbank ist eine Historie und Materialisierung.

Sie darf niemals den tatsächlichen Kraken-Zustand überstimmen.

---

# 38. WebSocket

Nutze soweit passend:

- Public market streams
- Private account streams
- execution streams
- balance streams

Sequence gaps müssen erkannt werden.

Bei einem Gap:

1. Trading-Risiko stoppen
2. REST-Reconciliation
3. Stream-State neu aufbauen
4. Konsistenz bestätigen
5. Trading wieder aktivieren

---

# 39. News Market Reaction Learning

Besonders wichtig:

Nicht nur News analysieren.

Nach einer News muss die Anwendung später prüfen:

Was wurde erwartet?

Was ist tatsächlich passiert?

Wie stark war die Preisreaktion?

Wie lange hielt sie an?

War die Richtung korrekt?

War das Signal zu spät?

War das Signal zu früh?

Hat der Markt die News bereits eingepreist?

War Gemini korrekt?

War die Quelle zuverlässig?

Diese Erkenntnisse gehen wieder in die News- und Gemini-Kalibrierung.

---

# 40. Gemini Performance Tracking

Für jede relevante Gemini-Einschätzung:

GEMINI PREDICTION

→ MARKET OUTCOME

→ ERROR

→ CONFIDENCE CALIBRATION

Die App muss erkennen, ob Gemini bei bestimmten:

- Assetklassen
- Regimes
- News-Arten
- Zeithorizonten

besser oder schlechter funktioniert.

Gemini-Gewichtung kann entsprechend automatisch angepasst werden.

---

# 41. Ensemble statt Einzelmodell

Nicht ausschließlich ein Modell.

Mögliche Komponenten:

- trend model
- momentum model
- mean-reversion model
- breakout model
- volatility model
- cross-sectional model
- news model
- regime model
- ML model
- Gemini information model

Die Ensemble-Gewichte werden empirisch kalibriert.

Ein schwaches Modell soll automatisch weniger Einfluss erhalten.

---

# 42. Adaptive Strategy Selection

Die App soll nicht immer dieselbe Strategie verwenden.

Beispiel:

TRENDING MARKET
→ Trend/Momentum stärker

RANGING MARKET
→ Mean-Reversion stärker

HIGH VOLATILITY
→ kleinere Positionen / höhere Edge-Anforderung

LIQUIDITY STRESS
→ weniger oder keine Trades

NEWS SHOCK
→ News/Event Model priorisieren

UNCERTAIN REGIME
→ Risiko reduzieren oder NO TRADE

---

# 43. Performance Attribution

Nach jedem Trade:

SIGNAL CONTRIBUTION

SIZING CONTRIBUTION

LEVERAGE CONTRIBUTION

ENTRY EXECUTION

EXIT EXECUTION

FEES

SLIPPAGE

FUNDING

REGIME

NEWS

GEMINI

MODEL

PORTFOLIO EFFECT

ACTUAL PNL

analysieren.

Dadurch soll das System lernen, welcher Bestandteil tatsächlich Performance erzeugt oder vernichtet.

---

# 44. Automatische Fehlerdatenbank

Alle Fehler werden dauerhaft strukturiert gespeichert.

Ein Fehler erhält:

- error_id
- error_type
- stage
- instrument
- venue
- timestamp
- market_state
- portfolio_state
- relevant decision
- Kraken response
- Gemini state
- recovery action
- outcome

Die Anwendung soll wiederkehrende Fehler erkennen.

---

# 45. Automatische Anomalieerkennung

Überwachen:

- ungewöhnliche Slippage
- ungewöhnliche Spread-Ausweitung
- ungewöhnliche Fills
- ungewöhnliche API-Latenz
- ungewöhnliche Reject-Raten
- ungewöhnliche News-Reaktionen
- ungewöhnliche Modellabweichungen
- ungewöhnliche Drawdowns
- ungewöhnliche Korrelationen

Bei Anomalien:

WARN

→ REDUCE RISK

→ POSSIBLE SAFE MODE

→ LEARN

---

# 46. Kapitalwachstum

Das System darf nicht einfach davon ausgehen:

mehr Kapital = größere Position.

Es muss unterscheiden zwischen:

- capital growth
- risk budget
- available collateral
- margin capacity
- execution capacity
- liquidity capacity

Bei wachsendem Konto soll das Risiko kontrolliert mitwachsen.

---

# 47. Kleine Startsumme

Das System muss explizit für sehr kleine Konten funktionieren.

Startkapital beispielsweise:

~50 EUR

Das bedeutet:

- Trades können an Mindestgrößen scheitern
- manche Märkte sind wirtschaftlich ungeeignet
- Hebel darf nicht dazu missbraucht werden, unrealistisch große Positionen aufzubauen
- Gebühren haben relativ hohen Einfluss
- minimale Positionsgrößen müssen berücksichtigt werden

Die App soll aus diesen Situationen lernen.

---

# 48. No Forced Trading

Die App muss jederzeit akzeptieren:

NO TRADE

Ein leerer Tag ist kein Fehler.

Wenige Trades sind kein Fehler.

Kein Trade bei schlechten Bedingungen ist ein valides Ergebnis.

---

# 49. Keine künstliche Aktivität

Keine Trades erzeugen nur um:

- Statistik aufzubauen
- Gemini zu testen
- Lernen zu beschleunigen
- Portfolio zu bewegen
- Aktivität zu zeigen

Lernen erfolgt auch mit nicht ausgeführten Entscheidungen.

---

# 50. Automatische Research Engine

Die App darf intern laufend neue Hypothesen untersuchen.

Beispiele:

- neuer technischer Faktor
- neuer Volatilitätsfilter
- anderer Exit
- anderer News-Horizont
- andere Short-Filter
- anderes Leverage-Modell
- andere Liquiditätsgewichtung

Jede Hypothese muss aber automatisch evaluiert werden.

Keine experimentelle Hypothese darf direkt ungeprüft live handeln.

---

# 51. Research Isolation

Research-Code und Live-Trading-Code müssen logisch getrennt sein.

Research darf:

- Modelle trainieren
- Daten analysieren
- Parameter testen
- Backtests ausführen

Live Trading darf ausschließlich:

- geprüfte Modelle
- geprüfte Parameter
- geprüfte Strategien

verwenden.

---

# 52. Keine automatische Quellcode-Mutation

Die App darf sich nicht selbstständig ihren Python-Code verändern.

Autonomes Lernen erfolgt auf Daten-, Modell-, Parameter-, Gewichtungs- und Kalibrierungsebene.

Nicht über:

- selbstgeschriebene Python-Dateien
- selbständige Änderung der Sicherheitslogik
- Änderung von Risk Limits
- Änderung von API-Rechten

---

# 53. Sicherheitsgates bleiben unveränderlich

Folgende Elemente darf das Lernsystem nicht umgehen:

- Kill Switch
- maximale Leverage
- maximaler Drawdown
- maximaler Tagesverlust
- maximaler Portfolio-Risk
- maximale Position
- maximale Margin
- Mindestdatenqualität
- unbekannter Orderstatus
- Portfolio-Inkonsistenz

Diese Grenzen kommen ausschließlich aus der HA-Konfiguration bzw. unveränderlichen System-Sicherheitsregeln.

---

# 54. Automatische Modellkalibrierung

Kalibriere unter anderem:

- probability calibration
- expected return bias
- confidence bias
- regime probability
- news confidence
- Gemini confidence
- slippage estimator
- fill probability
- volatility forecasts

---

# 55. Calibration Monitoring

Überwache:

PREDICTED 10%

gegen

REALIZED FREQUENCY

sowie:

PREDICTED EDGE

gegen

REALIZED EDGE

und:

PREDICTED SLIPPAGE

gegen

REALIZED SLIPPAGE

Bei systematischem Bias automatisch recalibrieren.

---

# 56. Stop-Loss- und Exit-Engine

Jede Position benötigt eine aktive Exit-Logik.

Nicht zwingend nur einen festen Stop.

Mögliche Signale:

- volatility stop
- structural stop
- trailing stop
- time stop
- signal reversal
- risk reduction
- margin risk
- news reversal
- expected edge decay
- regime change
- portfolio rebalance

Exitentscheidungen müssen ebenfalls gelernt und ausgewertet werden.

---

# 57. Short-spezifische Exits

Short-Positionen müssen separat behandelt werden.

Besonders berücksichtigen:

- schnelle Gegenbewegungen
- Short squeeze
- Liquidität
- Margin pressure
- Finanzierung
- Gap risk
- News risk

---

# 58. Reversal

Das System darf automatisch:

LONG → SHORT

oder

SHORT → LONG

durchführen.

Aber nicht blind.

Es muss vorher:

- alte Position schließen/reduzieren
- Status bestätigen
- neues Risiko berechnen
- Margin prüfen
- neues Signal validieren
- neues Order Intent erstellen

---

# 59. Circuit Breaker

Automatische Aktivierung bei:

- API authentication failure
- permission mismatch
- Kraken service problems
- stale market data
- stale private data
- sequence gap
- portfolio mismatch
- unknown order
- extreme spread
- extreme slippage
- excessive rejects
- excessive API errors
- excessive losses
- daily loss limit
- drawdown limit
- margin danger
- abnormal volatility
- abnormal execution

Nach Circuit Breaker:

NO NEW RISK

bis Recovery erfolgreich abgeschlossen wurde.

---

# 60. Home Assistant Sensoren

Keine GUI.

Status über Sensoren.

Mindestens:

binary sensors:

- trading_enabled
- system_ready
- market_data_healthy
- private_data_healthy
- portfolio_consistent
- model_ready
- news_healthy
- gemini_healthy
- circuit_breaker
- margin_safe

numerische Sensoren:

- portfolio_equity
- available_cash
- available_margin
- used_margin
- gross_exposure
- net_exposure
- realized_pnl
- unrealized_pnl
- daily_pnl
- drawdown
- open_positions
- open_orders
- orders_today
- current_leverage
- average_slippage
- average_latency
- expected_edge

Text-Sensoren:

- system_state
- last_action
- last_symbol
- last_direction
- last_blocker
- last_trade
- active_strategy
- active_model
- active_regime
- last_news_event
- gemini_status
- learning_status

---

# 61. Sensoren dürfen Trading niemals blockieren

Falls Home Assistant nicht erreichbar ist:

→ Trading Core läuft weiter, sofern die Sicherheitsarchitektur dies erlaubt.

Sensor Publish Failure darf nicht zum Trading-Code-Crash führen.

---

# 62. Logging

Logs müssen sehr detailliert sein.

Jeder Prozessschritt muss nachvollziehbar sein.

Mindestens:

BOOT_START
CONFIG_LOADED
CONFIG_INVALID
KRAKEN_AUTH_CHECK
KRAKEN_PERMISSION_CHECK
PRODUCT_DISCOVERY
INSTRUMENT_SYNC
MARKET_UNIVERSE_UPDATED
PUBLIC_WS_CONNECTED
PRIVATE_WS_CONNECTED
DATA_STALE
NEWS_FETCH
NEWS_ANALYSIS
GEMINI_REQUEST
GEMINI_RESPONSE
FEATURES_COMPUTED
REGIME_DETECTED
SIGNAL_CREATED
COST_ESTIMATE
EXPECTED_EDGE
POSITION_SIZE
LEVERAGE_SELECTED
MARGIN_CHECK
RISK_CHECK
DECISION_CREATED
ORDER_INTENT
PRETRADE_CHECK
ORDER_SUBMITTING
ORDER_ACKNOWLEDGED
ORDER_RECONCILING
ORDER_FILLED
ORDER_PARTIAL_FILL
ORDER_CANCELED
ORDER_REJECTED
PORTFOLIO_UPDATED
LEARNING_EVENT
MODEL_EVALUATED
CALIBRATION_UPDATED
MODEL_PROMOTED
MODEL_ROLLBACK
CIRCUIT_BREAKER
RECOVERY_STARTED
RECOVERY_COMPLETED

---

# 63. Blocker Logging

Wenn kein Trade erfolgt, muss exakt erklärt werden warum.

Beispiel:

symbol=XYZ/USD
direction=LONG
expected_edge_bps=18
required_edge_bps=35
spread_bps=9
slippage_bps=7
news_confidence=0.41
model_confidence=0.55

result=BLOCKED_EXPECTED_EDGE

Das System darf niemals einfach:

NO TRADE

loggen.

---

# 64. Jeder Zyklus nachvollziehbar

Ein einzelner Cycle muss vom Anfang bis zum Ende rekonstruierbar sein.

Beispiel:

cycle_id

→ instruments scanned

→ candidates

→ filtered instruments

→ deep analyses

→ news

→ Gemini

→ signals

→ expected edges

→ positions

→ risk checks

→ order intent

→ order

→ fills

→ final position

→ learning event

Damit ist jeder Trade und auch jeder nicht ausgeführte Trade auditierbar.

---

# 65. Datenbank

Mindestens:

schema_meta
cycles
market_snapshots
instrument_metadata
portfolio_snapshots
portfolio_positions
decisions
decision_checks
orders
order_events
fills
positions
news_events
news_analysis
gemini_analysis
predictions
prediction_outcomes
learning_events
calibration_history
strategy_versions
model_versions
model_evaluations
risk_events
health_snapshots
app_events
error_events

---

# 66. Event-Sourcing-Light

Wichtige Ereignisse append-only speichern.

Aktuellen Zustand materialisieren.

Dadurch können jederzeit folgende Fragen beantwortet werden:

Warum wurde gehandelt?

Warum wurde nicht gehandelt?

Warum wurde Short gewählt?

Warum wurde Leverage verwendet?

Warum wurde Position geschlossen?

Warum wurde Order abgelehnt?

Was hat Gemini gesagt?

Welche News waren vorhanden?

Welche Modellversion war aktiv?

Was wurde vorhergesagt?

Was ist tatsächlich passiert?

---

# 67. Keine Legacy-Abhängigkeiten

Nicht übernehmen:

- alte KTKI Engines
- v66/v67/v68/v94/v95/v96/v97/v98/v99/v100/v101/v102/v103 Facades
- alte GUI
- alte Paper Engine
- alte Automation
- alte Real Allocators
- alte Tax-Komponenten
- alte Portfolio-Logik
- alte Datenbankmodelle

Nur fachliche Erkenntnisse übernehmen.

Code neu schreiben.

---

# 68. Saubere Schichten

Empfohlene Struktur:

app/
  config/
  runtime/
  kraken/
  market/
  portfolio/
  trading/
  strategy/
  signals/
  risk/
  margin/
  leverage/
  execution/
  news/
  gemini/
  learning/
  calibration/
  research/
  persistence/
  sensors/
  monitoring/
  recovery/
  tests/

Eine zentrale Runtime koordiniert alles.

Keine parallelen Trading-Orchestratoren.

---

# 69. Single Trading Authority

Nur ein zentraler Controller darf:

- Trading Cycle starten
- Trade Decision finalisieren
- Risk freigeben
- Order Intent erstellen
- Order senden

WebSocket-Tasks, News-Tasks, Gemini-Tasks oder Lern-Tasks dürfen niemals eigenständig Orders senden.

---

# 70. Asynchronität

News und Gemini dürfen langsam sein.

Das Trading-System darf deswegen nicht blockieren.

Es muss klar unterscheiden:

DATA REQUIRED FOR TRADE

und

OPTIONAL INFORMATION

Fehlt Gemini, darf je nach Konfiguration:

- Trading pausieren
oder
- ohne Gemini mit reduziertem Modell weiterlaufen

Dies muss deterministisch geregelt sein.

---

# 71. Kosten von Gemini

Gemini-Nutzung muss effizient sein.

Nicht jeden Markt ständig an Gemini schicken.

Nur für:

- priorisierte Instrumente
- neue relevante News
- ungewöhnliche Ereignisse
- Unsicherheitsfälle
- Research
- Lernanalyse

Caching und Deduplizierung verwenden.

---

# 72. News darf keine Verzögerungsfalle sein

Ein schneller Markt darf nicht dadurch verpasst werden, dass eine langsame AI-Antwort abgewartet wird.

Daher:

FAST PATH

→ Markt/Execution

SLOW PATH

→ News/Gemini/Research

Die Ergebnisse können anschließend den nächsten Zyklus beeinflussen.

---

# 73. Lernschleife

Die Hauptschleife lautet:

DISCOVER

→ OBSERVE

→ ANALYZE

→ PREDICT

→ DECIDE

→ EXECUTE

→ OBSERVE OUTCOME

→ MEASURE ERROR

→ LEARN

→ CALIBRATE

→ VALIDATE

→ ADAPT

→ TRADE

und niemals:

TRADE → blindes Parameterändern.

---

# 74. Erfolgsmetriken

Nicht nur PnL betrachten.

Mindestens:

- net PnL
- gross PnL
- Sharpe
- Sortino
- max drawdown
- Expected Shortfall
- downside deviation
- win rate
- payoff ratio
- expectancy
- turnover
- fees
- slippage
- fill rate
- reject rate
- adverse selection
- prediction calibration
- predicted vs realized edge
- performance by regime
- performance by instrument
- performance by direction
- performance by leverage
- performance by execution method
- performance by news class
- performance with/without Gemini influence

---

# 75. Ziel der Optimierung

Nicht:

MAXIMIZE NUMBER OF TRADES

Nicht:

MAXIMIZE LEVERAGE

Nicht:

MAXIMIZE RAW RETURN

Sondern:

MAXIMIZE ROBUST RISK-ADJUSTED NET EXPECTANCY

unter den verfügbaren Markt-, Kosten-, Margin- und Sicherheitsbedingungen.

---

# 76. Automatische Priorisierung

Die Anwendung soll selbst erkennen:

Welche Märkte sind aktuell interessant?

Welche Märkte sind liquid genug?

Wo ist die erwartete Netto-Chance am größten?

Wo ist Long attraktiver?

Wo ist Short attraktiver?

Welches Instrument hat das beste Verhältnis aus:

EDGE / COST / RISK / LIQUIDITY ?

---

# 77. Marktweite relative Selektion

Nicht jeden Markt isoliert betrachten.

Die App soll auch fragen:

Welches Asset ist innerhalb seines Clusters am stärksten?

Welches am schwächsten?

Wo ist Relative Strength?

Wo ist Relative Weakness?

Wo ist Marktbreite?

Welche Assets bewegen sich synchron?

Wo entsteht Divergenz?

Welche Märkte haben ungewöhnliche Kapitalflüsse?

---

# 78. Execution-aware Selection

Ein theoretisch gutes Signal soll aussortiert werden, wenn es nicht vernünftig ausführbar ist.

Also:

SIGNAL QUALITY

allein nicht ausreichend.

Entscheidend:

EXPECTED NET EXECUTABLE EDGE.

---

# 79. Post-Trade Learning

Nach jedem Trade:

Was wurde erwartet?

Was passierte?

War Entry gut?

War Exit gut?

War Leverage gut?

War Positionsgröße gut?

War Markt liquide genug?

War Slippage erwartbar?

War News korrekt interpretiert?

War Gemini hilfreich?

War Regime korrekt?

War das Risiko angemessen?

Diese Antworten müssen automatisch in die nächste Modellgeneration einfließen.

---

# 80. Pre-Trade Counterfactual

Bei wichtigen Entscheidungen soll die App zusätzlich intern evaluieren:

Was wäre passiert mit:

- keinem Trade
- kleinerer Position
- größerer Position
- 1x
- niedrigerem Leverage
- höherem Leverage
- Long statt Short
- Short statt Long
- früherem Exit
- späterem Exit

Diese Counterfactuals dienen der Lernengine.

---

# 81. Keine Daten-Leakage

Beim Training:

- Zeitreihen sauber trennen
- keine zukünftigen Daten verwenden
- keine zukünftigen News verwenden
- keine nachträglich bekannten Ergebnisse in Features einfließen lassen
- keine Full-Sample-Normalisierung
- keine Zukunftsinformationen in Regime Labels

---

# 82. Overfitting-Kontrolle

Jede Optimierung muss gegen Overfitting geschützt werden.

Insbesondere:

- Walk Forward
- Nested Validation
- Parameter Stability
- Regime Stability
- Bootstrap
- Permutation Tests
- Deflated Sharpe
- Multiple Testing
- Out-of-Sample Correlation
- Sensitivity Analysis

---

# 83. Live Monitoring

Die App muss permanent überwachen:

- API latency
- WebSocket latency
- market-data freshness
- private-data freshness
- order latency
- fill latency
- fill probability
- slippage
- spreads
- margin
- equity
- exposure
- PnL
- drawdown
- model quality
- prediction calibration
- news quality
- Gemini quality

---

# 84. Automatisches Safe Mode

Bei erheblicher Unsicherheit:

SAFE MODE

Dann beispielsweise:

- keine neuen Positionen
- bestehende Positionen weiterhin überwachen
- Risiko reduzieren
- Lernprozess weiterführen
- Markt- und API-Daten weiter sammeln
- Recovery versuchen

---

# 85. Recovery nach Neustart

Nach jedem Neustart:

- Konfiguration laden
- API überprüfen
- Instrumente synchronisieren
- offene Orders laden
- Positionen laden
- Balances laden
- Executions laden
- historische Lücken schließen
- private WebSocket verbinden
- lokalen Zustand gegen Kraken prüfen
- Lernsystem synchronisieren
- erst danach READY

Niemals blind von einem lokalen alten Zustand ausgehen.

---

# 86. API-Sicherheit

Keine Secrets loggen.

Nie:

- API secret
- Gemini key
- auth header
- Supervisor token
- private credentials

in Logs schreiben.

---

# 87. API Permission Audit

Beim Start automatisch prüfen:

- API key gültig?
- benötigte Permissions vorhanden?
- Trade permissions vorhanden?
- Query permissions vorhanden?
- WebSocket token möglich?
- Restrictions?
- IP restrictions?
- Ablaufdatum?

Fehlt eine notwendige Berechtigung:

BLOCKED_API_PERMISSIONS

und kein Trading.

---

# 88. Kraken Status

Bei Kraken-Problemen:

Kein neues Risiko eröffnen.

Bestehende Positionen nur entsprechend dem sicheren Recovery-/Risk-Plan verwalten.

---

# 89. Testing

Vor Freigabe:

- Unit Tests
- Integration Tests
- Kraken API contract tests
- WebSocket tests
- order-state tests
- reconciliation tests
- margin tests
- leverage tests
- short tests
- long tests
- partial-fill tests
- duplicate-order tests
- ambiguous-network tests
- portfolio mismatch tests
- stale-data tests
- news tests
- Gemini failure tests
- learning tests
- calibration tests
- model promotion tests
- rollback tests
- recovery tests
- HA sensor tests

---

# 90. End-to-End Tests

Mindestens folgende Abläufe vollständig testen:

STARTUP

→ AUTH

→ MARKET DISCOVERY

→ ANALYSIS

→ NEWS

→ GEMINI

→ SIGNAL

→ RISK

→ MARGIN

→ LEVERAGE

→ ORDER

→ FILL

→ PORTFOLIO

→ OUTCOME

→ LEARNING

→ CALIBRATION

→ NEXT DECISION

Zusätzlich:

STARTUP AFTER CRASH

UNKNOWN ORDER

WEBSOCKET GAP

KRAKEN API FAILURE

GEMINI FAILURE

NEWS FAILURE

PARTIAL FILL

SHORT POSITION

LONG POSITION

LEVERAGED POSITION

POSITION CLOSE

REVERSAL

CIRCUIT BREAKER

RECOVERY

---

# 91. CI

CI muss vollständig grün sein.

Nicht nur Unit Tests.

Auch:

- lint
- type checking
- security scanning
- dependency checks
- integration tests
- state-machine tests
- persistence tests
- deterministic test suite

Kein Merge, solange relevante Tests fehlschlagen.

---

# 92. Dokumentation

Dokumentiere jeden Prozess.

Mindestens:

ARCHITECTURE.md
TRADING_FLOW.md
RISK.md
MARGIN.md
LEVERAGE.md
SHORT_TRADING.md
MARKET_DISCOVERY.md
NEWS.md
GEMINI.md
LEARNING.md
CALIBRATION.md
EXECUTION.md
RECOVERY.md
ERRORS.md
SENSORS.md
CONFIG.md
DATABASE.md
TESTING.md

---

# 93. Process Documentation

Für jeden Prozess erklären:

- Input
- Verarbeitung
- Output
- Fehler
- Blocker
- Recovery
- Learning Event

Beispiel:

NEWS → Gemini → Signal Influence → Trade → Outcome → Calibration

muss vollständig nachvollziehbar sein.

---

# 94. Keine versteckten Prozesse

Es darf keine Hintergrundlogik geben, die:

- Trades startet
- Parameter verändert
- Modelle aktiviert
- Risk Limits verändert
- Orders sendet

ohne dass dieser Vorgang in der zentralen Architektur nachvollziehbar ist.

---

# 95. Vollautonomie

Nach erfolgreicher Installation und Konfiguration soll die Anwendung selbstständig:

- Märkte entdecken
- Daten laden
- historische Daten aufbauen
- Modelle initialisieren
- News analysieren
- Gemini einsetzen
- Märkte priorisieren
- Signale berechnen
- Long/Short auswählen
- Leverage auswählen
- Positionen dimensionieren
- Orders ausführen
- Orders überwachen
- Positionen verwalten
- Gewinne/Verluste auswerten
- aus Ergebnissen lernen
- Modelle kalibrieren
- Modelle evaluieren
- schlechte Modelle zurückstufen
- stabile Modelle bevorzugen
- Fehler erkennen
- Recovery durchführen
- Sensoren aktualisieren
- Logs schreiben

ohne manuelle tägliche Pflege.

---

# 96. Aber keine uneingeschränkte Selbstgefährdung

Vollautonomie bedeutet nicht:

„Das System darf alles.“

Es bedeutet:

„Das System darf innerhalb klarer unveränderlicher Sicherheitsgrenzen selbst entscheiden.“

Der Risk Layer ist die letzte Instanz.

---

# 97. Implementierungsreihenfolge

Baue in dieser Reihenfolge:

1. neue Projektstruktur
2. HA App
3. Config
4. Kraken Authentication
5. Account Discovery
6. Instrument Discovery
7. Market Data
8. Private Data
9. Portfolio
10. Order Engine
11. Position Engine
12. Margin
13. Leverage
14. Short
15. Dynamic Market Scanner
16. Feature Engine
17. Regime Engine
18. News Engine
19. Gemini
20. Initial Learning Dataset
21. Learning Engine
22. Calibration
23. Strategy Ensemble
24. Risk Engine
25. Execution Optimization
26. Recovery
27. Sensors
28. Full Integration
29. CI
30. End-to-End validation

---

# 98. Wichtigste Architekturregel

Es darf niemals mehrere parallele Wahrheiten geben.

Eine zentrale Trading Runtime.

Eine zentrale Order Authority.

Eine zentrale Portfolio-Reconciliation.

Eine zentrale Risk Engine.

Eine zentrale Learning Engine.

Eine zentrale Model Registry.

Eine zentrale Configuration.

---

# 99. Abschlussprüfung

Bevor du den Neubau als fertig betrachtest, beantworte anhand des Codes:

Kann das System nach einem Neustart vollständig selbstständig wieder anlaufen?

Kann es den gesamten aktuell handelbaren Kraken-Markt selbst entdecken?

Kann es neue Märkte automatisch erkennen?

Kann es Long und Short?

Kann es Spot Margin?

Kann es Leverage?

Kann es Kraken Derivatives verwenden, sofern für Konto und Markt verfügbar?

Kann es ein 50-EUR-Konto korrekt behandeln?

Kann es erkennen, dass manche Orders wegen Mindestgröße oder Kosten nicht ausführbar sind?

Kann es News automatisch verarbeiten?

Kann es Gemini automatisch verwenden?

Kann es erkennen, ob Gemini tatsächlich Mehrwert liefert?

Kann es aus jedem abgeschlossenen Trade lernen?

Kann es aus nicht ausgeführten Trades lernen?

Kann es aus Fehlern lernen?

Kann es Modelle automatisch kalibrieren?

Kann es schlechte Modelle automatisch zurückstufen?

Kann es auf stabile Modelle zurückrollen?

Kann es bei API-/WS-/Portfolio-Problemen selbstständig recovern?

Kann es eine unbekannte Order sicher rekonstruieren?

Kann es ohne GUI betrieben werden?

Kann der gesamte Prozess ausschließlich über Logs und HA-Sensoren nachvollzogen werden?

Gibt es irgendwo noch eine versteckte Legacy-Abhängigkeit?

Gibt es irgendwo mehrere Trading-Autoritäten?

Gibt es irgendwo einen Pfad, über den Gemini oder ein anderer Hintergrunddienst direkt eine Order senden kann?

Gibt es irgendwo einen Pfad, über den automatisches Lernen Sicherheitsgrenzen verändern kann?

**Erst wenn alle Antworten technisch nachvollziehbar mit Tests belegt werden können, ist der Neubau fertig.**

# 100. Arbeitsauftrag

Beginne nicht mit dem Programmieren.

Analysiere zuerst das bestehende KTKI-System vollständig und erstelle daraus eine Liste:

- bestehende Fehler
- bekannte Fehlentscheidungen
- bekannte GUI-Probleme
- problematische Schnittstellen
- problematische Datenflüsse
- fehlerhafte Orderpfade
- Probleme der bisherigen Lernsysteme
- Probleme der bisherigen News-Integration
- Probleme der bisherigen Gemini-Integration
- Probleme von Paper/Real
- Probleme von Portfolio-Reconciliation
- Probleme von Kraken API/WS
- unnötige Komponenten
- gute fachliche Erkenntnisse
- erfolgreiche technische Ansätze

Danach entwirf die neue Architektur.

Danach implementiere die neue Anwendung vollständig unabhängig.

Keine Legacy-Datei übernehmen.

Keine Legacy-Abhängigkeit einbauen.

Danach vollständige Tests.

Danach vollständige End-to-End-Prüfung.

Danach CI.

Danach sämtliche gefundenen Fehler beheben.

Danach erneut alle Tests.

Erst wenn alles grün und technisch konsistent ist, den finalen Stand in den vorgesehenen Main-Branch integrieren.

Der neue Trader soll nicht wie KTKI aussehen.

Er soll nicht KTKI mit weniger GUI sein.

Er soll eine **neue autonome Trading-Plattform** sein.

Der entscheidende Leitsatz lautet:

**Nicht die alte Anwendung reparieren. Die Aufgabe vollständig neu und sauber lösen.**