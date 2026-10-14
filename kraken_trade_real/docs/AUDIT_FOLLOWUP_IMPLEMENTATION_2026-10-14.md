# Audit-Follow-up Implementierung – 14.10.2026

## Zweck und Release-Grenze

Dieses Dokument verfolgt die Implementierungen aus `AUDIT_2026-10-14.md`. Es trennt Code-/Testkorrekturen von Dingen, die empirische Daten oder einen kontrollierten Lauf in Home Assistant benötigen. Ein grüner CI-Lauf wird nie als Rendite- oder Live-Sicherheitsnachweis interpretiert.

## In diesem Follow-up umgesetzt

| Auditbereich | Implementierung | Regression / Nachweis |
|---|---|---|
| Order-Authority | Finaler Preflight auf endliche/valide Mengen, Hebel, Preise, Instrument-Minimums, Tickgröße und Richtung/Side-Konsistenz | Ruff, Mypy, Compile und gesamte Pytest-Suite prüfen diese Pfade |
| Datenfrische | Bei aktiviertem Live-Submit müssen Symbol, Zeitstempel, Bid/Ask/Preis zusammenpassen; fehlende, veraltete oder unmögliche Quotes blockieren den Submit | Tests für stale und fehlende Timestamps; bestehender Exchange-Reject-Test verwendet bewusst frischen Quote |
| Idempotenz | Bereits gespeicherte Client-Order-ID wird nie nochmals an die Börse gesendet; Wiederverwendung mit anderer Intent-ID wird abgelehnt | Regressionen für denselben Intent und absichtliche Client-ID-Kollision |
| Portfolio-/Risikogrenzen | Taktische Rationale/Parameter können die unveränderliche 15-%-Einzelpositionsgrenze nicht überschreiben; Diagnostik nutzt denselben Deckel | Tests mit 25-/75-/80-%-Profilwerten; Testfälle für Volatilität, Margin und Net-Exposure wurden unter der 15-%-Einzelgrenze neu isoliert |
| FIFO-Lernen | Ein Close-Fill, der die lokal rekonstruierte FIFO-Menge übersteigt, quarantänisiert alle Opening-Decisions des Fills auch dann, wenn ein Teil-Leg scheinbar matched | Regression auf übergroßen Reduce-only-Fill: kein Ergebnis wird als verifizierter Outcome verfügbar gemacht |
| Kalibrierung | Log Loss wird zusätzlich zu Brier/ECE berechnet, in `calibrations` gespeichert, per idempotenter Schema-Migration auf Version 17 ergänzt und als zusätzliche Promotion-Schranke verwendet | Tests für Wertebereich, persistierten Log Loss und idempotente Migration |
| Walk-forward | Expanding-window-Aufteilung mit disjunkten chronologischen Testfenstern, Zeitstempelvalidierung, Datenhashes und expliziter Aussage, ob ein eingefrorener Scorer oder ein Train/Test-Scorer verwendet wurde | Regressionen für nicht-chronologische Daten und disjunkte Testfenster |
| Offline-Ausführung | Kostenmodell mit Bid/Ask, Taker-Gebühr, Slippage/Impact, Tiefe, teilweisen Fills, Mindestmengen/-werten und explizitem Finanzierungsaufschlag; keine Auto-Promotion | Synthetische Regressionen auf Kosten, Tiefe, Stale-Quotes, Mindestorders und Look-ahead |
| Historische Daten | `run_from_database` liest gespeicherte `market_snapshots`, übergibt dem Signal-Callback ausschließlich bis zum jeweiligen Entscheidungszeitpunkt bekannte Snapshots und simuliert die Ausführung erst am Folgequote | Fake-DB-Regression prüft die zeitliche Begrenzung des Callbacks und die Verwendung des Folgequotes |

## Noch nicht vollständig erledigt bzw. nicht belegt

### P0/P1 – Ausführung und Bestandskonsistenz
- Der zuvor gemergte Cash-Spot-Paarbestand-Hotfix begrenzt Exit-Mengen auf bestätigte paarbezogene App-Inventarmengen und verfügbaren Wallet-Bestand.
- Die neue Submission-ID-Sperre macht denselben Client-Order-ID-Submit innerhalb eines Datenbankzustands idempotent; sie ist **kein vollständiger persistenter Asset-Reservierungsdienst** über beliebige verschiedene Order-IDs, mehrere Prozesse und mehrere Quote-Paare. Diese Race-/Restart-Matrix braucht noch explizite Tests und ggf. eine DB-gestützte Reservierungs-/Lease-Tabelle.
- Historische `REDUCE_ONLY_EXCEEDS_LOCAL_INVENTORY`- und Cross-Quote-Anomalien sind nicht rückwirkend repariert. Ohne zugehörige Exchange-Fills und Lots kann aus dem Repo-Log nicht seriös entschieden werden, welcher Restbestand wem gehört.
- Es wurde keine echte Exchange-Stop-Order eröffnet oder geprüft. Schutz bei HA-Ausfall, Netzwerkverlust, Börsen-Disconnect, Exchange-Stop-Reject, Teilfüllung und Liquidation bleibt für Spot Margin/Futures unbewiesen.

### P1 – Kosten und Ergebnisattribution
- Der Logstand meldete null vollständig verifizierte Netto-Outcomes. Diese neuen Codeänderungen verwandeln historische estimated-only/ambiguous Ergebnisse nicht automatisch in geprüfte Nettoergebnisse.
- Tatsächliche Entry-/Exit-Gebührenwährungen, zeitpunktbezogene FX-Kurse, Margin-Eröffnungsgebühren und Rollover/Finanzierung müssen für konkrete Fills anhand von `TradesHistory`, verlinkten Ledgers, Positionen und Order-IDs reconciliert werden. Private Konto-/Ledgerdaten waren für diesen Patchlauf nicht verfügbar und wurden nicht angefordert.
- Nicht aufklärbare Fälle bleiben unscorable; sie dürfen nicht Auto-Promotion oder Hyperparameter-Optimierung verbessern.

### P1 – Backtest / Wissenschaft
- `run_from_database` ist jetzt an archivierte Quotes/Book-Depths anschließbar. Es wurde in diesem Patchlauf **keine historische DB aus der laufenden HA-Installation geladen**; darum liegt kein Ergebnis aus realen historischen Kraken-Daten vor.
- Ein direkter Same-Window-Vergleich für `CASH`, `BUY_AND_HOLD`, eine feste 12-Beobachtungen-Momentum-Baseline und eine feste 5/20-Trend-Baseline ist implementiert. Das ist keine Optimierung dieser Baselines und es wird kein Gewinner automatisch ausgewählt. Der Simulator ist aktuell cash-Spot/long-only; negative Short-Signale werden als nicht unterstützt zurückgewiesen. Die 80-bps-Taker-Defaultgebühr ist ein konservativer Startwert, keine historische Gebührentier-Rekonstruktion.
- Der Simulator ist derzeit ein pro-Symbol-Execution-Simulator. Er ist kein voll integrierter gemeinsamer Core+Tactical-Portfolio-Backtest und bildet Exchange-Queue-Priority, tatsächliche Maker-Fill-Wahrscheinlichkeit, Kraken-Produkt-/Margin-Liquidation, Futures Funding, historische Fee-Tier-Wechsel oder alle Reject-/Timeout-Rennen nicht automatisch ab.
- Signal-Provider und historische Minima-/Gebührentabellen müssen für jeden Lauf versionsgebunden eingespeist werden. Ein zu großer Annahmenbereich kann ein Ergebnis verzerren; der Run protokolliert Annahmen, nicht die Wahrheit über Erreichbarkeit.
- Es gibt noch kein OOS-Ergebnis für Core, Tactical, kombiniert, News-/Gemini-Ablation oder einfache Baselines. Kein Modell und keine Parameter wurden aufgrund des neuen Simulators automatisch promoviert.
- Die zusätzlichen Log-Loss-/Reliability-Metriken sind verfügbar; ein ausreichender, unabhängiger Samplebestand und kalibrierte Wahrscheinlichkeiten wurden damit noch nicht nachgewiesen.

### P2 – Betrieb und Daten
- Die vorhandene Portfolio-Reconciliation hat externe Cashflows und einen expliziten unvollständigen/unbewerteten Status. Dennoch müssen die Tages-/Drawdown-Werte nach Release im HA-Runtime-Log mit vollständigen Exchange-Ledgers, echten Fees und Ein-/Auszahlungen verglichen werden.
- Der ganze Markt-/News-/Gemini-Pfad hat noch keine reproduzierbare, unabhängige Ablationsauswertung, die einen positiven inkrementellen Nettoeffekt nach Kosten belegt.
- Die laufende HA-Installation wird nicht durch einen GitHub-Merge aktualisiert. Version, geladene Config und wirksame Sicherheitsgrenzen müssen dort nach einem kontrollierten Update verifiziert werden.

## Noch notwendige Freigabeschritte

1. CI auf dem endgültigen PR-Head muss vollständig grün sein; danach den PR-Head und den Main-Commit nach Merge erneut kontrollieren.
2. Historische DB im Read-only-Modus exportieren und Simulation auf einem festgelegten Zeitfenster gegen eine eingefrorene Strategy-/Feature-Version laufen lassen; zuerst Datenqualität, Abdeckung und Ausführbarkeit berichten, dann erst Netto-Metriken.
3. Sämtliche auflösbaren Fills einschließlich Gebühren-/FX-/Margin-Ledger abgleichen; Anomalien bleiben sonst gesperrt und unscorable.
4. Race-/Restart-Tests auf Base-Asset-/Quote-Paar-Ebene sowie getrennte Cash-Spot-, Spot-Margin- und Futures-Orderzustandsautomaten vervollständigen.
5. Paper-/Shadow-Forward-Zeitraum, Stichprobengröße und Stop-Kriterien vorab definieren. Erst danach über eine überwachte, kleinste Live-Stufe ohne Hebel entscheiden.
6. Hebel/Short gesondert freigeben, nachdem eine tatsächlich exchange-seitige Schutzstrategie und Disconnect-/Restart-/Liquidationsszenarien verifiziert sind.

## Explizite Entscheidung

Die Umsetzung stärkt mehrere zentrale Invarianten und macht die Forschung reproduzierbarer. Sie liefert **noch keine Freigabe für unbeaufsichtigten Live-Handel** und keinen Nachweis von Profitabilität. Ein späterer Merge bestätigt nur, dass die geprüften Tests auf dem Commit bestanden wurden.
