# Szenario 1

Dieses Verzeichnis enthaelt das Python-Skript fuer die nachvollziehbare Berechnung der Scenario-1-Preisstrategie aus der Quelldatenbank:

`DATA/thesis_data v2.sqlite`

Das Ergebnis wird in der zentralen Ergebnisdatenbank im Hauptordner gespeichert:

`RESULTS.sql`

## Ausfuehren

Vom Hauptordner des Projekts:

```sh
python3 'szenario 1/build_scenario_1_results.py'
```

Das Skript nutzt nur die Python-Standardbibliothek (`sqlite3`) und berechnet aus den vorbereiteten S1-Preisfeatures:

- Top-Quintil-Auswahl je Handelstag
- taegliche Portfolio-Renditen
- Benchmark-Vergleich
- aggregierte Summary-Kennzahlen

Eine kompakte SQL-Variante liegt ebenfalls im Ordner:

```sh
sqlite3 RESULTS.sql < 'szenario 1/build_scenario_1_results.sql'
```

## Ergebnis-Tabellen

- `s1_price_features`
- `s1_price_backtest_panel`
- `s1_price_top20_long`
- `s1_price_daily_returns`
- `s1_price_vs_benchmark_daily_returns`
- `s1_price_summary`
- `s1_price_run_metadata`
