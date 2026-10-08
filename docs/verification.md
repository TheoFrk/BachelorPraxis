# Verifikation · 8. Oktober 2026

## Technische Checks

- **16 Backendtests bestanden** in einer frischen Python-3.12-Venv mit `requirements.lock`.
- **1 Frontendtest bestanden**: fehlende Scores bleiben fehlend; unterschiedliche Modellskalen werden nicht still gemittelt.
- **Vite-Produktionsbuild bestanden**, React mit automatischer JSX-Transformation, ca. 115 KiB gzip JavaScript inklusive Karte.
- **Dockerbuild bestanden**; Produktcontainer als UID 10001, read-only Root-Filesystem, ohne Capabilities; Compose-Konfiguration validiert.
- Im Container: Readiness, Anmeldung, Schutz nicht authentisierter API-Abfragen, begrenzte Login-Bodies, statische Dateien und Wetterhistorien-API geprüft.
- **Onlinebackup geprüft:** SQLite `integrity_check=ok`; 1.602 Rohstoffpreiszeilen enthalten. Watchlist und Daten überleben einen Containerneustart.
- **Reale Anbieterintegration:** 1.602 World-Bank-Monatsbeobachtungen für Gold/Kakao bis 2026-09-30 und 120 ERA5-Tagesbeobachtungen für zwei Regionen erfolgreich geladen.
- **Forschungsimport:** 347.629 Zeilen / 568 Instrumente erfolgreich übernommen. Keine absoluten Kurse erfunden, keine Forward-Returns übernommen; Fremddatenbank als Runtime-Ziel wird abgewiesen.
- GDELT lieferte im Test einen HTTP-Fehler. Der Fehlerzustand ist sichtbar, keine Ersatzmeldungen werden erzeugt. Adapterlogik, URL-Prüfung und Deduplizierung wurden mit Fixtures getestet; erfolgreicher Liveabruf ist hier nicht nachgewiesen.
- `git diff --check` bestanden. Originale Forschungsskripte, Datenbanken und bestehende Ergebnisdateien bleiben unverändert.

Die verwendete Starlette-Testclient-Version meldet eine Deprecation-Warnung zum bestehenden httpx-Client. Alle Tests bestehen; die Warnung ist keine behauptete Laufzeitstörung. Ein Upgrade des Testtransports kann bei einer späteren Abhängigkeitsaktualisierung erfolgen.

## Browser und Bedienabläufe

Browser-/IAB-Plugin war nicht verfügbar; deshalb Playwright Chromium. Der Browser nutzte den konfigurierten Session-Proxy und die bereitgestellte CA-Vertrauensstellung, keine deaktivierte TLS-Prüfung. Externe OSM-Kacheln wurden erfolgreich geladen.

Geprüft: Login, Suche, Wertpapierauswahl und Chart, Watchliständerung und Persistenz nach Reload, Anlageklassen/Instrumentmetadaten, Wetterkarte, Regionalwechsel, Gold-/Kakao-Tabs, Verbindung zu Rohstoffreferenzen, erfolgreicher ETF-CSV-Import mit klarer QA-Fixture, mobile Darstellung und Logout. Keine JavaScript-Fehler im abschließenden Ablauf. Die temporären QA-Instrumente/Imports wurden aus dem lokalen Forschungsbestand entfernt.

Desktop geprüft bei **1440×900**, **1536×1024** und der tatsächlichen nativen Konzeptgröße **1586×992**; Mobile bei **390×844**. Mobile Tabellen scrollen innerhalb ihrer Region, der Dokumentbereich hat keinen horizontalen Overflow. Inspector und dichte Tabellen haben eigene Scrollbereiche; auf Mobilgeräten fließt die Detailansicht unter die Liste.

Screenshots der tatsächlich gerenderten Anwendung:

- [Marktübersicht](screenshots/market.png)
- [Wetter- und Ereigniskonsole](screenshots/drivers.png)
- [Mobilansicht](screenshots/mobile.png)

## Visueller Vergleich

Die beiden generierten Referenzkonzepte lagen während der Prüfung unter:

- `/workspace/generated_images/exec-a52b6b42-1bc8-44fb-99c1-687ad38478c3.png`
- `/workspace/generated_images/exec-23813e11-4564-466b-af4c-b68b27c95bad.png`

Beide Konzepte und die abschließenden Browserbilder wurden mit `view_image` direkt geprüft. Die temporären Konzeptdateien sind keine Produktassets; die Anwendung rendert sämtliche Bedienelemente, Texte, Daten und Charts selbst. Die Referenzen enthalten illustrative Werte, keine übernommenen Marktdaten.

| Vergleichspunkt | Umsetzung und Prüfung |
| --- | --- |
| Layout | Linke Navigation, ruhiger Header, drei Kennzahlen, zentrale Tabelle und rechter Inspector erhalten. Neuer Treiberbereich folgt dem zweispaltigen Karten-/Indikatorlayout. |
| Palette | Dunkles #080f19, Flächen #101c2b, Linien #25364a, Cyan #58d5de und zurückhaltendes Amber für Datenhinweise. Unpassende helle native Checkboxdarstellung durch `color-scheme: dark` korrigiert. |
| Typografie | Systemschrift mit definierten Größen für Navigation, Tabellen, Formulare und Metadaten; kein Browserstandard auf ungestylten Controls. Lesbarkeit im nativen Desktopformat und Mobile kontrolliert. |
| Tabelle / Detail | Selektierte Zeile, Detailchart, Sternaktion und Quellenstatus entsprechen dem Referenzprinzip. Eigene Scrollbereiche ergänzen die tatsächlich längeren Daten-/Quellenangaben. |
| Karte | Tatsächliche OSM-Geografie, reale Koordinaten, zwei auswählbare Messpunkte und sichtbare Attribution. Anfangs fehlende Kacheln wurden durch korrekte Browser-Proxy-/CA-Konfiguration geladen; ein Leaflet-Fehler beim Unmount während Zoom wurde durch deaktivierte Kartenanimation und Stop bereinigt. |
| Responsive Verhalten | Navigation und Ebenen umbrechen, Spalten werden untereinander angeordnet, Daten bleiben zugänglich. Keine Dokumentüberbreite bei 390 px. |
| Interaktionen | Alle sichtbaren Aktionen sind mit API/React-Zustand verbunden: Suche, Filter, Detail, Watchlist, Import und Feedabruf. Keine dekorativen Kauf-/Verkaufsschaltflächen. |

**Copy-Diff und bewusste Abweichungen:** Fiktive Preise, ISINs, Renditen, Kauf-/Verkaufsempfehlungen und Anbieterbehauptungen der generierten Marktvorlage wurden durch tatsächliche Daten oder explizite Leerzustände ersetzt. Nicht implementierte Tabs wurden nicht als inerte Controls übernommen. Der neue Multi-Asset-Auftrag ergänzt „Treiber & Ereignisse“, Anlageklassen, Einheiten, explizite Expositionen, Wetterhistorie und einen dritten Abrufknopf für World-Bank-Daten. Der Datenhinweis lautet daher „Beobachteter Datenbestand“ statt einer ausschließlich historischen Aktienansicht. Live-Abrufdaten und Quellenstatus ersetzen illustrative Zahlen; dies ist eine dokumentierte fachliche Anpassung der Designreferenz. Der abschließende visuelle Abgleich bestätigte die gemeinsame Designstruktur mit diesen Anpassungen, keine behauptete pixelidentische Kopie.

## Aussagegrenzen

Lokale/Container-Verifikation ersetzt keine Prüfung eines öffentlichen HTTPS-Deployments, einer Multiuser-Umgebung oder hoher Last. Datenverfügbarkeit, Lizenzen, veröffentlichte Marktpreise, Reanalysequalität und Medienrelevanz bleiben providerabhängig. Es wurde keine profitable Strategie, kalibrierte Dürreklassifikation, kausale Wetter-Preiswirkung oder Echtzeitabdeckung bestätigt. Statistische Anforderungen stehen in [research.md](research.md).
