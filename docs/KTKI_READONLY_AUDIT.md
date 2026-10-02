# KTKI Read-only Audit

## Zweck

KTKI (GerMorg/KTKI) wurde vor dem Neubau ausschließlich als fachliche Referenz untersucht. Die neue Anwendung übernimmt keine KTKI-Datei, keine KTKI-Runtime und keine KTKI-Kompatibilitätsschicht.

## Festgestellte Probleme und Lernpunkte

### Architektur

KTKI enthält einen über viele Entwicklungsstände gewachsenen Mix aus v67 bis v103, versionierten Decision-/Execution-/Paper-/Real-Modulen, Kompatibilitätsfassaden und historisch migrierten Komponenten. Das erzeugt mehrere potenzielle Wahrheiten über Runtime, Status und Ausführung.

Neubauentscheidung: eine einzige kanonische Runtime mit klaren Domänengrenzen.

### GUI

Die letzte KTKI-Strecke bündelte fachliche Trading-Prozesse mit Flask-Routen, Templates und Legacy-Weiterleitungen. Die Projektgeschichte dokumentiert wiederkehrende 404-, Navigations- und Statusabweichungen.

Neubauentscheidung: keine Web-GUI. Home Assistant Konfiguration, HA-Sensoren und strukturierte Prozesslogs sind die primären Oberflächen.

### Market Data

Historisch wurden öffentliche Marktdaten zunächst zu eng gefiltert und EUR/USD-Paarauflösung war über mehrere Module verteilt. xStocks und andere Märkte verursachten zusätzlich Pair-/Asset-Class-Routing-Probleme.

Neubauentscheidung: Kraken-Instrumentmetadaten sind alleinige Quelle für handelbare Instrument-IDs, Asset-Klasse, Quote, Präzision, Mindestgröße, Kostenminimum und verfügbare Leverage-/Margin-Attribute.

### Orders und Realhandel

KTKI entwickelte über mehrere Generationen getrennte Realhandels-Gates. Die fachliche Erkenntnis ist richtig, die Umsetzung darf jedoch nicht mehrere Orderautoritäten erzeugen.

Neubauentscheidung: nur TradingAuthority darf den Schreibpfad des Kraken-Gateways aufrufen. Gemini, News, Learning, Risk und Sensors besitzen keinerlei Order-Schnittstelle.

### Portfolio

Historische Fehler rund um getrennte Paper-/Real-Wahrheiten und Reconciliation zeigen, dass lokale Zustände nie als alleinige Wahrheit verwendet werden dürfen.

Neubauentscheidung: Kraken-Account-/Order-/Positionsdaten werden nach Start aktiv geladen und gegen lokale offene Zustände abgeglichen. Ambiguous Orders werden vor jedem Retry rekonstruiert.

### News

KTKI hatte mehrere Quellen, Fallbacks und eine spätere externe Nachrichten-AI. Die zentrale Erkenntnis ist die getrennte Persistenz von Rohdaten, Klassifikation, Einfluss und Ergebnis.

Neubauentscheidung: Source → Fetch → Normalize → Deduplicate → Entity → Event → Direction → Impact → Novelty → Credibility → Horizon → Market Confirmation → Outcome → Learning.

### Gemini

Die bisherige Integration nutzte einen älteren generateContent-Pfad. Für den Neubau wird die aktuelle Google GenAI Python SDK / Interactions API mit strukturiertem JSON verwendet.

Neubauentscheidung: Gemini ist ein Analyse-/Research-/Post-Trade-Dienst ohne Order Authority und ohne direkte Mutation aktiver Sicherheitsparameter.

### Lernen

Die historische Entwicklung führte zu versionierten Parametern, Walk-forward-Vergleichen und expliziten Freigabegates. Der Neubau führt dies zu einem einheitlichen Model Registry / Calibration / Research Layer zusammen.

### Technische Qualitätsprobleme

Die KTKI-Historie dokumentiert unter anderem migrationsbedingte Startfehler, UTF-8-/Mojibake-Probleme, inkonsistente Insert-Spalten, Public-Market Routing, obsolete Version-Wrapper und GUI-Regressionen.

Neubauentscheidung: explizites Schema, idempotente Migrationen, UTF-8-only, strukturelle Architekturtests und Ende-zu-Ende-State-Machine-Tests.

## Gute fachliche Erkenntnisse aus KTKI

- Kosten müssen vollständig in die Edge-Bewertung.
- Mindestordergrößen und Mindestkosten kommen aus Kraken-Metadaten.
- Aktuelle unvollständige OHLC-Kerzen dürfen nicht als historische Information dienen.
- Prognosen müssen Version und Feature-Snapshot speichern.
- Chronologische Validierung ist zwingend.
- Unbekannte Orderantworten dürfen nicht blind wiederholt werden.
- News und externe AI benötigen Ergebnisbewertung.
- Sensorfehler sollen die Trading-Engine nicht selbst zu einer versteckten zweiten State Machine machen.

## Nicht übernommen

Keine KTKI-Python-Datei, kein Flask-Template, kein CSS, kein Legacy-Import, keine historische Runtime-Fassade, keine KTKI-Datenbank und keine KTKI-Orderroutine werden von der neuen Anwendung importiert.
