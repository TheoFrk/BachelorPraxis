# Securities Eye

Eine lauffähige, selbst betreibbare Multi-Asset-Konsole auf Basis von **BachelorPraxis**. Die ursprünglichen S1–S4-Experimente und Ergebnisdateien bleiben erhalten. Die neue Anwendung läuft daneben mit eigener Datenbank.

- Aktien, ETFs, ETCs, Anleihen, Futures, Spot- und Rohstoffreferenzen als getrennte Instrumenttypen.
- Marktübersicht mit Suche, Länder-/Sektor-/Anlageklassenfiltern, Historie und persistenter Watchlist.
- Preis-, Lexikon-, FinBERT- und LLM-Scores mit Herkunft und Beobachtungsdatum. Keine ungeprüfte Mittelung verschiedener Modellskalen.
- Kakao mit Wetterbeobachtungen für Ashanti und Soubré; interaktive Karte und nachvollziehbare Expositionsverbindungen.
- Gold und Kakao mit automatisch abrufbaren World-Bank-Monatsreferenzen; Gold zusätzlich mit ETC-/Spot-Katalog.
- Wetter- und Konfliktnachrichten über GDELT, sichtbarer Feed-Zustand und Erhaltung bereits geladener Daten bei Ausfällen.
- Anmeldung, atomare CSV-Imports, Healthchecks, Backup-CLI, Docker und CI.

Die Oberfläche übernimmt das Prinzip einer gemeinsamen Lageansicht mit Ebenen und Zielinspektion aus [God’s Eye View](https://github.com/bilawalsidhu/gods-eye-view). Der Implementierungscode wurde neu geschrieben; es wurden keine Medien oder Datensätze des Vorbilds übernommen.

## Lokal starten

Voraussetzungen: Python 3.12+, Node.js 24+, Git LFS nur für den historischen Forschungsimport.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock -e '.[test]'
npm ci --prefix frontend
npm run build --prefix frontend
python scripts/create_runtime_env.py
python scripts/run_server.py
```

Öffne **http://localhost:8000**. Der Zugangsschlüssel steht in deiner lokal erzeugten, nicht versionierten `.env` (`SE_ACCESS_TOKEN`). Die Datei wird mit Modus 0600 angelegt. Dieser lokale Start bindet nur an Loopback; für HTTPS siehe [Betrieb](docs/operations.md).

Ein frischer Start zeigt den Instrumentkatalog ohne erfundene Kurse. Unter **Treiber & Ereignisse** lassen sich Rohstoffreferenzen, Wetter und Meldungen abrufen. Unter **Daten & Betrieb** lassen sich eigene, lizenzierte Kurse und Modellscores hochladen.

## Historische Bachelor-Daten übernehmen

```bash
git lfs install --local
git lfs pull --include='outputs/results/s1_revised_results.sqlite'
securities-eye import-thesis outputs/results/s1_revised_results.sqlite
```

Der Adapter liest ausschließlich S1-Scores, Momentum, Volatilität und Stammdaten. Er erfindet keine absoluten Kurse und übernimmt keine `ret_fwd_*`-Spalten. Die originale Quelldatenbank wird read-only geöffnet. Der aktuelle Forschungsbestand enthält 568 Instrumente und 347.629 Beobachtungen bis 2025-12-31.

S2–S4 können über das dokumentierte [Signal-CSV-Format](docs/operations.md) übernommen werden. Die Anwendung führt die vorhandenen kostenträchtigen LLM-Backtests nicht automatisch aus.

## Docker

```bash
python scripts/create_runtime_env.py
docker compose up --build -d
```

Der Dienst läuft als UID 10001 mit einem persistenten Volume, schreibgeschütztem Dateisystem und ohne Linux-Capabilities. Der veröffentlichte Port ist standardmäßig nur lokal erreichbar. [Betriebsanleitung](docs/operations.md) beschreibt HTTPS, Import, geplante Abrufe und Backups.

## Daten und Aussagegrenzen

**Kein Echtzeit-Handelssystem.** World-Bank-Daten sind Monatsmittel; Wetter ist eine verzögerte ERA5-Reanalyse; GDELT liefert unbestätigte Medien-Suchtreffer. Die Instrumente `GOLD-WB` und `COCOA-WB` sind Analyse-Referenzen, keine handelbaren Wertpapiere. `CC=F` bezeichnet eine kontinuierliche Futures-Reihe, keinen Einzelkontrakt. Kurse von Aktien/ETFs/ETCs/Futures benötigen einen passenden Datenimport.

Die Konsole ist für einen einzelnen Betreiber/ein gemeinsames Team mit gemeinsamem Schlüssel ausgelegt. Sie bietet keine individuellen Benutzerkonten, Handelsausführung, validierte Renditeprognose, SPI/SPEI-Dürreklassifikation oder ETF-Holdings-Durchschau. Ein erfolgreicher technischer Test ist kein Nachweis profitabler Signale.

Die [Paper-Recherche](docs/research.md) begründet die Treiber und benennt die nächsten Schritte für statistische Validierung. [Datenquellen und Lizenzen](docs/data-sources.md), [Architektur](docs/architecture.md) und [Testnachweise](docs/verification.md) gehören zum Produkt.

## Prüfen

```bash
pytest -q
npm test --prefix frontend
npm run build --prefix frontend
docker build -t securities-eye:local .
```

Tests verwenden kleine, klar gekennzeichnete Fixtures; externe APIs sind nicht Voraussetzung für CI.
