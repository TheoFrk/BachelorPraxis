# GDELT Datenakquisition – Strategie & BigQuery-Optimierung

## Übersicht: Zwei Wege zu den Daten

| Methode | Kosten/Limit | Geschwindigkeit | Volltext? | Empfehlung |
|---------|-------------|-----------------|-----------|------------|
| **GDELT Raw Files** (Script) | Kostenlos, kein Limit | ~2-4 Tage Download | Nein (Metadaten + Tone) | ✅ Primär |
| **BigQuery** | 1 TB/Monat (free tier) | Minuten | Nein (Metadaten + Tone) | ✅ Ergänzend |
| **LexisNexis** | 50 Artikel/Download | Manuell, langsam | Ja (Volltext) | 🔄 Proof-of-Concept |
| **Bloomberg** (Commerzbank) | Noch in Klärung | Schnell | Ja | ⏳ Idealfall |

---

## Teil 1: GDELT Raw Files Script

### Verwendung

```bash
# Vollständiger Download (Aug 2024 - Dez 2025)
python gdelt_euro_stoxx50_downloader.py \
    --start 2024-08-01 \
    --end 2025-12-31 \
    --output ./gdelt_data \
    --sleep 0.1

# Falls unterbrochen – Resume ab bestimmtem Datum
python gdelt_euro_stoxx50_downloader.py \
    --start 2024-08-01 \
    --end 2025-12-31 \
    --output ./gdelt_data \
    --resume-from 2024-11-15

# Schnelltest: nur 1 Monat
python gdelt_euro_stoxx50_downloader.py \
    --start 2024-08-01 \
    --end 2024-08-31 \
    --output ./gdelt_test
```

### Was du bekommst

- **~50.000 GKG-Dateien** (17 Monate × 96 Dateien/Tag)
- Pro Datei: ~10.000-50.000 Records, davon ~50-200 Euro Stoxx 50 Matches
- **Geschätztes Gesamtvolumen: 100.000-500.000 gefilterte Zeilen**
- Output: CSV mit Tone-Scores (GDELT's eigener Sentiment), Quellen, Timestamps

### Wichtig: GDELT Tone vs. dein FinBERT/LLM-Score

GDELT's `tone`-Spalte ist ein einfacher lexikon-basierter Score – **das ist NICHT dein Sentiment-Signal**. 
Für deine Thesis brauchst du:
1. GDELT als **Artikel-Index**: Welche Firmen wurden wann in welcher Quelle erwähnt?
2. Den **Volltext** dieser Artikel für dein eigenes FinBERT/LLM-Scoring
3. GDELT-Tone kannst du als **Baseline/Benchmark** gegen FinBERT vergleichen

Der Volltext ist über die `source_url` erreichbar – du kannst die URLs nachträglich scrapen 
(vgl. newspaper3k, trafilatura). Das ist legal, da du die Originalartikel abrufst.

---

## Teil 2: BigQuery-Optimierung (unter 1 TB bleiben)

### Warum du 1 TB verbraucht hast

BigQuery berechnet nach **gescannten Bytes**, nicht nach Ergebniszeilen. Die GDELT-Tabellen 
sind riesig (~500 GB für GKG V2). Ein `SELECT *` über den vollen Zeitraum scannt die 
gesamte Tabelle.

### Optimierungstechniken

#### 1. NUR die Spalten abfragen, die du brauchst (~80% Ersparnis)

```sql
-- ❌ SCHLECHT: Scannt alle Spalten (~8 GB/Tag)
SELECT * FROM `gdelt-bq.gdeltv2.gkg`
WHERE DATE(_PARTITIONTIME) = '2024-08-01'

-- ✅ GUT: Nur relevante Spalten (~1.5 GB/Tag)
SELECT 
    DATE,
    V2Organizations,
    V2Tone,
    V2Themes,
    DocumentIdentifier
FROM `gdelt-bq.gdeltv2.gkg_partitioned`
WHERE _PARTITIONTIME >= '2024-08-01'
  AND _PARTITIONTIME < '2024-08-02'
```

#### 2. Partitionierte Tabelle verwenden

```sql
-- Die partitionierte Tabelle ist VIEL günstiger:
-- `gdelt-bq.gdeltv2.gkg_partitioned` statt `gdelt-bq.gdeltv2.gkg`
-- BigQuery scannt nur die angeforderten Partitionen
```

#### 3. Erst filtern, dann joinen – Subquery-Strategie

```sql
-- Schritt 1: Nur Records mit relevanten Organisationen (Vorfilter)
CREATE TEMP TABLE filtered_gkg AS
SELECT 
    DATE,
    V2Organizations,
    V2Tone,
    DocumentIdentifier
FROM `gdelt-bq.gdeltv2.gkg_partitioned`
WHERE _PARTITIONTIME >= '2024-08-01'
  AND _PARTITIONTIME < '2024-09-01'
  AND (
    V2Organizations LIKE '%ASML%'
    OR V2Organizations LIKE '%SAP%'
    OR V2Organizations LIKE '%LVMH%'
    OR V2Organizations LIKE '%Siemens%'
    OR V2Organizations LIKE '%TotalEnergies%'
    OR V2Organizations LIKE '%Airbus%'
    OR V2Organizations LIKE '%Allianz%'
    OR V2Organizations LIKE '%BNP Paribas%'
    OR V2Organizations LIKE '%Sanofi%'
    OR V2Organizations LIKE '%Volkswagen%'
    -- ... alle 50 Firmen
  )
```

#### 4. Kosten vorher prüfen (Dry Run)

```sql
-- In BigQuery UI: Klick auf "More" → "Query Settings" → "Dry Run"
-- Zeigt geschätzte Bytes VOR der Ausführung
-- Faustregel: gkg_partitioned, 1 Monat, 5 Spalten ≈ 30-50 GB
-- 17 Monate = ~600-850 GB → passt in 1 TB!
```

#### 5. Optimaler Monats-Query (Copy-Paste-fertig)

```sql
-- Dieser Query sollte ~30-50 GB pro Monat kosten
-- 17 Monate × 40 GB = ~680 GB → unter 1 TB Limit

DECLARE companies ARRAY<STRING>;
SET companies = [
  'ASML', 'SAP', 'LVMH', 'Louis Vuitton', 'Hermes', 'L\'Oreal',
  'Siemens', 'Airbus', 'Schneider Electric', 'TotalEnergies',
  'Shell', 'Enel', 'Iberdrola', 'ENI',
  'Allianz', 'BNP Paribas', 'AXA', 'ING Group', 'Intesa Sanpaolo',
  'Munich Re', 'Deutsche Boerse', 'Santander', 'BBVA',
  'Sanofi', 'Bayer', 'Deutsche Telekom',
  'Linde', 'Air Liquide', 'BASF',
  'AB InBev', 'Danone', 'Adidas', 'Ferrari',
  'Volkswagen', 'Stellantis', 'BMW', 'Mercedes-Benz', 'Porsche',
  'Kering', 'EssilorLuxottica', 'Safran', 'Vinci', 'Prosus',
  'CRH', 'DHL Group', 'Deutsche Post', 'Infineon', 'Nokia',
  'Nordea', 'UniCredit', 'Pernod Ricard', 'Saint-Gobain'
];

SELECT 
    SUBSTR(DATE, 1, 8) as date,
    DATE as datetime_raw,
    V2Organizations,
    V2Tone,
    V2Themes,
    DocumentIdentifier as source_url
FROM `gdelt-bq.gdeltv2.gkg_partitioned`
WHERE _PARTITIONTIME >= '2024-08-01'
  AND _PARTITIONTIME < '2024-09-01'  -- ← Monat für Monat ändern!
  AND EXISTS (
    SELECT 1 FROM UNNEST(companies) AS c
    WHERE V2Organizations LIKE CONCAT('%', c, '%')
  )
```

#### 6. Monatsweise exportieren

Führe den Query 17× aus (einmal pro Monat), exportiere nach Google Cloud Storage 
oder direkt als CSV. Pro Monat ~30-50 GB Scan → 17 Monate ≈ 600-850 GB → unter 1 TB.

**WICHTIG:** Warte bis zum 1. März, wenn dein BigQuery Free Tier sich zurücksetzt, 
oder verwende bis dahin das Raw-Files-Script.

---

## Teil 3: Empfohlene Gesamtstrategie

### Jetzt sofort:
1. **DOC API Script starten** (gdelt_doc_api_downloader.py) → Kostenlos, kein BigQuery
   - Laufzeit bei 15s Sleep: ~50 Firmen × 17 Monate × 15s ≈ **3.5 Stunden** (Artikel)
   - Plus ~50 × 15s ≈ **12 Minuten** (Tone Timelines)
   - **Tipp:** Overnight laufen lassen mit `nohup python3 gdelt_doc_api_downloader.py &`
   - Erwartet: ~50.000-100.000 Artikel-Metadaten + ~25.000 tägliche Tone-Werte
2. **SLUB-Mail senden** → LexisNexis API-Zugang klären
3. **Commerzbank-Bloomberg** → Weiter nachfragen

### Wenn Raw-Files fertig (~3-4 Tage):
4. **Artikel-URLs extrahieren** → Volltext per Web-Scraping nachladen (trafilatura/newspaper3k)
5. **FinBERT-Pipeline aufbauen** → Eigenes Sentiment-Scoring auf Volltexten
6. **GDELT-Tone als Baseline** → Vergleich Lexikon-Score vs. FinBERT vs. LLM

### Ab März (BigQuery-Reset):
7. **BigQuery als Validierung** → Optimierte Queries, Ergebnisse gegen Raw-Files prüfen

### Wenn Bloomberg kommt:
8. **Ad-hoc-Meldungen als Premium-Signal** → Höchste Signalqualität, exakte Timestamps
9. **GDELT als Nachrichtenkanal** → Bloomberg als regulatorischer Kanal → Vergleich beider

---

## Hinweis: Volltext-Beschaffung für FinBERT

Das Script liefert dir Metadaten + GDELT-Tone, aber **keinen Artikeltext**. 
Für dein eigenes FinBERT/LLM-Scoring brauchst du den Volltext. Optionen:

```python
# Option A: trafilatura (empfohlen, robust, respektiert robots.txt)
import trafilatura
downloaded = trafilatura.fetch_url(article_url)
text = trafilatura.extract(downloaded)

# Option B: newspaper3k
from newspaper import Article
article = Article(article_url)
article.download()
article.parse()
text = article.text
```

Damit kannst du die source_urls aus dem GDELT-Output nachträglich anreichern.
Beachte: Nicht alle URLs sind noch erreichbar (404s bei älteren Artikeln).
Für den Zeitraum Aug 2024 - Dez 2025 sollte die Verfügbarkeit aber >80% sein.
