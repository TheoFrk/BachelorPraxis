# Betrieb

## Konfiguration

| Variable | Zweck |
| --- | --- |
| `SE_ACCESS_TOKEN` | Zufälliger Zugangsschlüssel, mindestens 32 Zeichen. Keine Standard-Anmeldedaten. |
| `SE_ORIGIN` | Exakter Browser-Ursprung, z. B. `https://securities.example.org`; Prüfung für sämtliche schreibenden HTTP-Anfragen. |
| `SE_ALLOWED_HOSTS` | Kommaseparierte erlaubte Hostnamen, keine URLs. |
| `SE_SECURE_COOKIE` | `1` für HTTPS (Default im Code), `0` ausschließlich für lokalen HTTP-Test. |
| `SE_DB` | Pfad der eigenen SQLite-Datenbank, Default `var/securities-eye.sqlite`. |
| `SE_STATIC_DIR` | Gebaute Frontend-Dateien, Default `frontend/dist`. |

`python scripts/create_runtime_env.py` erzeugt eine neue `.env` mit zufälligem Schlüssel, überschreibt keine bestehende Konfiguration und zeigt keinen Schlüssel im Terminal. Die lokale `.env` setzt explizit Loopback-HTTP. Lies deinen Schlüssel mit deinem Editor und gib ihn im Login ein. Keine Schlüssel in Git, CI-Ausgaben oder Screenshots kopieren.

`python scripts/run_server.py` lädt ausschließlich `SE_`-Variablen aus der lokalen Datei und startet einen Worker auf 127.0.0.1:8000. Bei externem Hosting Runtime-Variablen über den Secret-Store der Plattform injizieren.

Für öffentlichen Betrieb: HTTPS-Reverse-Proxy vor die Anwendung setzen, `SE_SECURE_COOKIE=1`, Origin/Host auf die tatsächliche Domain setzen, nur den Proxy erreichbar machen und Login-/Request-Limits am Gateway ergänzen. Keine Wildcard-Hosts konfigurieren. Der gemeinsame Schlüssel ersetzt keine individuellen Benutzerkonten. Das Löschen des Browsercookies meldet diesen Browser ab; eine kopierte Session bleibt bis Ablauf gültig. Schlüsselrotation plus Neustart invalidiert alle Sessions.

## Daten importieren

Kurse, minimale UTF-8-Datei:

```csv
symbol,date,close,name,currency,asset_class,underlying,unit,frequency
```

Erforderlich: `symbol,date,close`. Datum ISO `YYYY-MM-DD`, Kurs endlich und >0, Dezimalpunkt, Komma als Trennzeichen. Optionale Anlageklasse: `equity`, `etf`, `etc`, `commodity_spot`, `commodity_reference`, `future`, `bond`, `index`. Optional `frequency=daily` (Default) oder `monthly`. Tages- und Monatsfrequenzen desselben Symbols werden nicht vermischt. Leere optionale Stammdatenfelder erhalten bestehende Namen/Länder/Sektoren/Währungen. Vollständige Metadaten beim Export sind dennoch empfehlenswert. Monatsdaten benötigen Monatsend-Daten; bei Lücken wird kein vermeintlicher 12-Monats-Score erzeugt. World-Bank-Referenzen verwenden separate Symbole, um Futures und Spot nicht zu überschreiben.

```bash
securities-eye import-csv /path/to/licensed-prices.csv --source 'Anbieter / Datenexport'
```

Modellscores:

```csv
symbol,date,layer,score
```

`layer` = `price`, `lexicon`, `finbert`, `llm`. Instrumente müssen existieren. Scores sind endliche modellspezifische Zahlen, keine universelle Einheit. Die Ansicht zeigt einen Score nur für dasselbe Datum wie die jüngste Instrumentbeobachtung; alte oder fehlende Scores werden nicht als Nullsignal ausgegeben.

```bash
securities-eye import-csv /path/to/model-scores.csv --kind signals --source 'Modell, Version und Datensatz'
```

HTTP-Upload im Bereich **Daten & Betrieb**: maximal 10 MiB / 100.000 Zeilen, vollständige Prüfung und atomare Transaktion. Doppelte Schlüssel aktualisieren bestehende Beobachtungen; jeder erfolgreiche Import wird protokolliert. CLI ist für größere vertrauenswürdige lokale Dateien vorgesehen.

## Feeds und geplante Aktualisierung

```bash
securities-eye sync-context commodities
securities-eye sync-context weather
securities-eye sync-context news
```

- **commodities:** aktuelles World-Bank-Monthly-Workbook, Gold/Kakao, abgeschlossene Monate. Monatsende ist das Beobachtungsdatum, nicht die erste Veröffentlichung. Aktualisiert bis zu 1.300 Tabellenzeilen.
- **weather:** ERA5-Reanalyse für zwei feste Kakao-Punkte, vollständiges 30-Tage-Fenster bis heute−7. Eine Region mit fehlenden Daten wird nicht als trockener Zustand gespeichert.
- **news:** GDELT-Suchtreffer, thematisch und auf sieben Tage begrenzt. Providerfehler bleiben sichtbar; keine erfundenen Ersatzmeldungen.

Alle Feeds haben 15 Minuten Cooldown, feste Anbieterziele, Timeouts und Antwortlimits. Beispiel für einen lokalen Cronjob mit absolutem Projekt-/Venv-Pfad:

```cron
17 6 * * * cd /srv/securities-eye && .venv/bin/securities-eye sync-context weather >> var/weather-job.log 2>&1
37 6 * * * cd /srv/securities-eye && .venv/bin/securities-eye sync-context news >> var/news-job.log 2>&1
57 6 4 * * cd /srv/securities-eye && .venv/bin/securities-eye sync-context commodities >> var/commodities-job.log 2>&1
```

CLI-Fehler liefern Exitcode 1; beim API-Abruf wird der persistierte Fehlschlag auch als fachlicher Status im JSON zurückgegeben. Jobs/Logs überwachen und rotieren. Die Browseransicht prüft die eigene API alle 60 Sekunden, wenn sie sichtbar ist; sie löst keine regelmäßigen externen Abrufe aus.

## Docker und Forschungsimport

```bash
docker compose up --build -d
docker compose exec app securities-eye sync-context commodities
docker compose exec app securities-eye sync-context weather
```

Forschungsdatei read-only in einen separaten Container mit demselben Datenvolume einhängen. Das Volume heißt üblicherweise `<compose-project>_securities-data`; tatsächlichen Namen über `docker volume ls` prüfen. Nicht die originale Forschungsdatenbank als schreibbares Runtime-Volume benutzen. Große Git-LFS-Dateien sind nicht Bestandteil des Produktimages.

Container läuft als UID/GID 10001. Bei Bind-Mounts statt Named-Volumes Schreibrechte für diese UID am Runtime-Verzeichnis bereitstellen. Image enthält keine Marktdaten, keine Git-Historie und keine Schlüssel.

## Backup und Wiederherstellung

```bash
securities-eye backup /safe/location/securities-eye-2026-10-08.sqlite
```

Die CLI nutzt SQLite-Onlinebackup, nicht eine unsichere Kopie nur der Hauptdatei im WAL-Betrieb. Ziel muss neu sein. Auch täglich versionierte Backups außerhalb des Hosts aufbewahren; Probe-Wiederherstellungen durchführen.

Wiederherstellung: Dienst und Jobs stoppen, aktuelles Runtime-Verzeichnis sichern, Backup als `SE_DB` einsetzen, alte passende `-wal`/`-shm`-Begleitdateien bei gestopptem Dienst entfernen und UID-Rechte prüfen. Dann Dienst starten und `/health/ready`, Instrumentanzahl, Watchlist und Importprotokoll kontrollieren. Änderungen am Quellcode nicht rückgängig machen, um ein Datenbackup wiederherzustellen.

## Healthchecks und Beobachtbarkeit

- `GET /health/live`: Prozess erreichbar.
- `GET /health/ready`: SQLite-Schema lesbar.
- `GET /api/status`: authentisiert, letzte Imports und konfigurierbare Ebenen.
- `GET /api/context`: authentisiert, Datenstand je Quelle und Feedfehler.

Request-ID, Methode, HTTP-Status und Laufzeit werden geloggt, keine Inhalte, Tokens oder Fremdantworten. Readiness bestätigt nicht die Aktualität externer Daten. Source-Datenstände und letzte erfolgreiche Abrufe gesondert überwachen.

## Geprüfte und noch ausstehende Betriebsaspekte

Die lokale Anwendung und der nicht privilegierte Dockercontainer werden in [verification.md](verification.md) dokumentiert. Öffentliche Bereitstellung, Domain/TLS, kommerzielle Datenverträge, Mehrbenutzerbetrieb und Lasttests wurden nicht durchgeführt. Ein externer GDELT-Ausfall ist keine lokale Deploymentfreigabe oder Aussage über geopolitische Sicherheit.
