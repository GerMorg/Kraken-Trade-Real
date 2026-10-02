# Kraken Trade Real Home Assistant App

Diese App ist eine eigenständige autonome Trading Runtime für Kraken.

## Konfiguration

Alle Betriebsparameter werden über die Home Assistant App-Konfiguration gesetzt. Die Runtime liest /data/options.json und verwendet keine parallele lokale Konfigurationsdatei.

## Beobachtung

Es gibt keine eigene Weboberfläche. Der Betriebszustand wird über Home-Assistant-Sensoren und strukturierte App-Logs bereitgestellt.

## Persistenz

- /data/state/trader.db
- /data/logs/trader-events.jsonl

Real Trading ist standardmäßig aus. Auch nach Aktivierung müssen API-Berechtigungen, Markt-/Portfolio-Reconciliation, Datenfrische, Risiko, Margin, Leverage, Positionsgröße, Kosten und die zentrale Trading Authority alle Gates bestehen.

Secrets werden redigiert und nie als Sensorzustand oder Loginhalt ausgegeben.
