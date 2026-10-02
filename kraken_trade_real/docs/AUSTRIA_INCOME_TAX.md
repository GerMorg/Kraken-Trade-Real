# Einkommensteuerinformation Österreich

Die App enthält eine eigene österreichische Steuer-Buchhaltungs- und Aufbereitungsschicht. Sie soll die jährliche Einkommensteuererklärung möglichst einfach vorbereiten. Sie ist keine Steuerberatung.

## Aktuelle Grundlage

Das BMF ordnet Einkünfte aus Kryptowährungen grundsätzlich den Einkünften aus Kapitalvermögen zu. Für Kryptowährungen gilt grundsätzlich der besondere Steuersatz von 27,5 Prozent.

Für Neuvermögen gelten die ausdrücklichen Krypto-Regeln grundsätzlich ab 1. März 2021. Bis 28. Februar 2021 angeschaffte Kryptowerte sind grundsätzlich Altvermögen.

Die App trennt Altvermögen und Neuvermögen sowie Krypto und Derivate.

## Gleitender Durchschnitt

Für Neuvermögen verwendet die Steuerbuchhaltung den gleitenden Durchschnittspreis in Euro für gleichartige Krypto-Bestände. Für Altvermögen verlangt der Report bei einer Veräußerung eine dokumentierte konkrete Kostenbasis, statt stillschweigend eine falsche Durchschnittsmethode anzuwenden.

## Krypto-zu-Krypto-Tausch

Ein Tausch einer Kryptowährung gegen eine andere Kryptowährung ist unter den gesetzlichen Voraussetzungen grundsätzlich steuerneutral. Die Anschaffungskosten werden auf den erhaltenen Kryptowert übertragen. Die App kennzeichnet diese Transaktionen separat.

## Kraken und Providerklassifikation

Kraken weist aktuell für EWR-Kunden irische Kraken-Unternehmen als Vertragspartner für die dort angebotenen Dienste aus. Daher ist die App-Voreinstellung FOREIGN. Diese Klassifikation muss anhand der für das konkrete Konto geltenden Vertrags- und Steuerunterlagen geprüft werden.

## E1 und E1kv

Bei Kapitalerträgen ohne österreichischen KESt-Abzug kann eine Einkommensteuerveranlagung erforderlich sein. Das BMF stellt hierfür die Beilage E1kv bereit.

Für die offizielle E1kv-Fassung 2025 verwendet die App folgende dokumentierte Krypto-Kennzahlen:

- laufende Krypto-Einkünfte: KZ 171 inländisch / KZ 172 ausländisch
- Überschüsse aus realisierten Krypto-Wertsteigerungen: KZ 173 inländisch / KZ 174 ausländisch
- Verluste: KZ 175 inländisch / KZ 176 ausländisch
- einbehaltene KESt auf inländische Kapitaleinkünfte: KZ 899

Für 2026 und folgende Jahre werden keine zukünftigen Formularnummern vorweggenommen. Der Bericht verlangt dann die aktuelle BMF-Fassung.

## Derivate, Margin und Short

Derivate und Futures werden getrennt vom §27b-Kryptoergebnis ausgewiesen. Margin-Sonderfälle werden als manuell zu prüfen markiert. Damit wird nicht automatisch aus jedem Trading-PnL ein vermeintlich steuerlich identisches Kryptoergebnis.

## Jahresreport

Pro Steuerjahr schreibt die App unter /data/reports/tax/AT/<Jahr>:

- income-tax-report.json
- tax-events.csv
- income-tax-information.md

Der Report enthält Jahreswerte, Datenvollständigkeit, E1/E1kv-Vorbereitung und offene Prüfungen.

## Datenqualität

Nicht-EUR-Transaktionen ohne belastbare EUR-Bewertung, fehlende Anschaffungskosten, unbekannte Paarauflösung und unklare Margin-/Derivateklassifikation werden nicht mit erfundenen Zahlen ergänzt. Der Jahresstatus bleibt dann INCOMPLETE_DATA.

## Offizielle Quellen

BMF – Steuerliche Behandlung von Kryptowährungen:
https://www.bmf.gv.at/themen/steuern/sparen-veranlagen/steuerliche-behandlung-von-kryptowaehrungen.html

BMF – Steuerreporting:
https://www.bmf.gv.at/themen/steuern/sparen-veranlagen/steuerreporting.html

BMF – Einkommensteuererklärung:
https://www.bmf.gv.at/themen/steuern/fuer-unternehmen/einkommensteuer/einkommensteuererklaerungspflicht.html

BMF – E1kv 2025:
https://service.bmf.gv.at/service/anwend/formulare/show_det.asp?MIdVal=46800&STyp=&Typ=SD&s=e1kv

BMF Findok – EStR 2000:
https://findok.bmf.gv.at/findok/volltext?segmentId=625e1d5c-cb04-40f3-ae3d-5aa3d3f3bcab6

Kraken – EWR-Vertragspartner und aktuelle Lizenzen:
https://support.kraken.com/at/articles/where-is-kraken-licensed-or-regulated
