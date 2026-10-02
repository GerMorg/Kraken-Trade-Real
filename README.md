# Kraken Trade Real

Neue, autonome Home Assistant App für Kraken Trading.

Die Anwendung wurde unabhängig von KTKI aufgebaut. KTKI dient ausschließlich als dokumentierte Read-only-Referenz für historische Erkenntnisse; es gibt keine Runtime-Importe oder Legacy-Kompatibilitätsschichten.

## Leitprinzipien

- Eine zentrale Trading Runtime.
- Eine zentrale Trading Authority als einzige Order-Sendeinstanz.
- Kraken ist die Quelle der Wahrheit für Account-, Order- und Positionszustände.
- Home Assistant ist die Konfigurationsautorität.
- SQLite dient als lokale Historie und rekonstruierbare Materialisierung.
- Dynamische Entdeckung des gesamten verfügbaren Kraken-Universums.
- Long, Short, Spot, Margin, Leverage und verfügbare Derivatives werden über die tatsächlichen Kraken-Instrumentmetadaten bestimmt.
- News und Gemini können Entscheidungen informieren, aber niemals Orders senden.
- Lernen darf Sicherheitsobergrenzen nicht verändern.
- Bei Unsicherheit gilt fail-closed: kein neues Risiko.
- Status und Prozessnachweis erfolgen über HA-Sensoren und strukturierte Logs, nicht über eine eigene Web-GUI.

## Home Assistant App

Die installierbare App liegt unter kraken_trade_real/.

Die App-Konfiguration wird ausschließlich über Home Assistant Supervisor verwaltet. Persistente Laufzeitdaten liegen unter /data.

## Entwicklung

Ausführbare Qualitätsgates:

cd kraken_trade_real && python -m pytest
ruff check app tests
mypy app --ignore-missing-imports

Zusätzlich prüft CI die HA-App-Struktur, Security und den Container-Build.

## Sicherheit

Real Trading ist standardmäßig deaktiviert. Eine Aktivierung setzt weiterhin die vollständige Startup-, Permission-, Daten-, Portfolio-, Risiko- und Order-Preflight-Kette voraus. Secrets werden niemals in Logs, Sensorzuständen, Exceptions oder Test-Fixtures ausgegeben.
