# S1 Revised Backtest Report

## 1. Executive Summary

S1 is evaluated as a pure price-only baseline using Momentum plus Low Volatility. The existing broad result is preserved conceptually as S1_FULL_568 and is treated as a robustness/additional analysis, not as the sole main comparison against text models.

## 2. Welche Eingabedaten gefunden wurden

- Input SQLite: `/Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Bachelorarbeit_Praxis/DATA/thesis_data v2.sqlite`
- Existing results DB: `/Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Bachelorarbeit_Praxis/RESULTS.sql`
- Common text source: daily_text_scores (167 securities); used as textscore universe.

Score-/Feature-Notizen:
- Die implementierte Variante nutzt VOL^60, was ca. drei Handelsmonaten entspricht. Die Formel in der Arbeit ist entsprechend auf VOL^60 anzupassen oder die Features sind auf VOL^63 neu zu berechnen.
- Using existing z_s1_price_score as the research score.

Benchmark-Notizen:
- Benchmark uses preferred instrument(s) from benchmark_instruments.
- Benchmark returns computed as adjusted_close(t+1) / adjusted_close(t) - 1.

## 3. Welche Universen gebaut wurden

| universe       |   n_target_securities | status   | note                                                                                                                       | coverage_min   | coverage_median   | coverage_mean   | coverage_max   |
|:---------------|----------------------:|:---------|:---------------------------------------------------------------------------------------------------------------------------|:---------------|:------------------|:----------------|:---------------|
| S1_FULL_568    |                   568 | READY    | U_FULL_PRICE: all securities with valid S1 score and ret_fwd_1d. This is the broad gross result universe (568 securities). | 0.248239       | 1                 | 0.980911        | 1              |
| S1_CORE        |                     0 | WARN     | U_CORE konnte nicht eindeutig identifiziert werden. No arbitrary reconstruction was used; see candidate sources.           |                |                   |                 |                |
| S1_COMMON_TEXT |                   167 | READY    | U_COMMON_TEXT: intersection of price universe and daily_text_scores (167 securities); used as textscore universe.          | 0.317365       | 1                 | 0.982618        | 1              |

Gefundene Kandidatenquellen fuer U_CORE:
| source                                                                                                                                                                                                                                                      | quality_column    | matched_ab_like_security_count   | value_counts             | type   | columns                                                                                                                                                                                                                                                                    | quality_columns                    |
|:------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|:------------------|:---------------------------------|:-------------------------|:-------|:---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|:-----------------------------------|
| sqlite:v_price_panel_with_universe                                                                                                                                                                                                                          | cap_group         | 0                                | {'ALL': 347629}          | nan    | nan                                                                                                                                                                                                                                                                        | nan                                |
| sqlite:securities                                                                                                                                                                                                                                           | cap_group         | 0                                | {'ALL': 568}             | nan    | nan                                                                                                                                                                                                                                                                        | nan                                |
| sqlite:securities                                                                                                                                                                                                                                           | download_priority | 568                              | {'A_CORE_DOWNLOAD': 568} | nan    | nan                                                                                                                                                                                                                                                                        | nan                                |
| sqlite:v_strategy_s1_common_news_top20_long                                                                                                                                                                                                                 | cap_group         | 0                                | {'ALL': 21197}           | nan    | nan                                                                                                                                                                                                                                                                        | nan                                |
| sqlite:v_s1_backtest_common_news_universe                                                                                                                                                                                                                   | cap_group         | 0                                | {'ALL': 104929}          | nan    | nan                                                                                                                                                                                                                                                                        | nan                                |
| sqlite:v_daily_text_scores_with_security                                                                                                                                                                                                                    | cap_group         | 0                                | {'ALL': 37053}           | nan    | nan                                                                                                                                                                                                                                                                        | nan                                |
| /Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Projekt_Bachelor_DATENAUFBEREITUNG/thesis_workflow_output_v2/03_price_panel_with_returns_and_s1.csv               | nan               |                                  | nan                      | csv    | ['date', 'yahoo_ticker', 'open', 'high', 'low', 'close', 'adjusted_close', 'volume', 'dividends', 'stock_splits', 'ret_1d', 'ret_fwd_1d', 'ret_fwd_5d', 'mom_12_1', 'mom_3m', 'vol_60d', 'lowvol_60d', 'z_mom_12_1', 'z_lowvol_60d', 's1_price_score', 'z_s1_price_score'] | []                                 |
| /Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Projekt_Bachelor_DATENAUFBEREITUNG/thesis_workflow_output_v2/01_universe_initial.csv                              | nan               |                                  | nan                      | csv    | ['security_id', 'news_ticker', 'yahoo_ticker', 'company', 'country', 'sector', 'cap_group', 'news_count', 'download_priority', 'mapping_confidence', 'price_start', 'price_end', 'n_price_days']                                                                           | ['cap_group', 'download_priority'] |
| /Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Projekt_Bachelor_DATENAUFBEREITUNG/thesis_workflow_output_v2/01_universe_initial.csv                              | cap_group         | 0                                | {'ALL': 568}             | nan    | nan                                                                                                                                                                                                                                                                        | nan                                |
| /Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Projekt_Bachelor_DATENAUFBEREITUNG/thesis_workflow_output_v2/01_universe_initial.csv                              | download_priority | 568                              | {'A_CORE_DOWNLOAD': 568} | nan    | nan                                                                                                                                                                                                                                                                        | nan                                |
| /Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Projekt_Bachelor_DATENAUFBEREITUNG/db_diagnostics/samples/sample_s1_common_news_universe_daily_returns_python.csv | nan               |                                  | nan                      | csv    | ['date', 'portfolio_ret_fwd_1d', 'n_positions', 'avg_z_s1_price_score']                                                                                                                                                                                                    | []                                 |
| /Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Projekt_Bachelor_DATENAUFBEREITUNG/db_diagnostics/samples/sample_common_news_universe.csv                         | nan               |                                  | nan                      | csv    | ['security_id', 'yahoo_ticker', 'company', 'country', 'sector']                                                                                                                                                                                                            | []                                 |
| /Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Projekt_Bachelor_DATENAUFBEREITUNG/DATA/universe_candidates.csv                                                   | nan               |                                  | nan                      | csv    | ['blackrock_ticker', 'company_name', 'isin', 'exchange', 'country', 'currency', 'weight_pct', 'sector', 'security_id', 'yahoo_ticker', 'stooq_symbol', 'mapping_source']                                                                                                   | []                                 |
| /Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Projekt_Bachelor_DATENAUFBEREITUNG/thesis_workflow_output/03_price_panel_with_returns_and_s1.csv                  | nan               |                                  | nan                      | csv    | ['date', 'yahoo_ticker', 'open', 'high', 'low', 'close', 'adjusted_close', 'volume', 'dividends', 'stock_splits', 'ret_1d', 'ret_fwd_1d', 'ret_fwd_5d', 'mom_12_1', 'mom_3m', 'vol_60d', 'lowvol_60d', 'z_mom_12_1', 'z_lowvol_60d', 's1_price_score', 'z_s1_price_score'] | []                                 |
| /Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Projekt_Bachelor_DATENAUFBEREITUNG/thesis_workflow_output/01_universe_initial.csv                                 | nan               |                                  | nan                      | csv    | ['security_id', 'news_ticker', 'yahoo_ticker', 'company', 'country', 'sector', 'cap_group', 'news_count', 'download_priority', 'mapping_confidence', 'price_start', 'price_end', 'n_price_days']                                                                           | ['cap_group', 'download_priority'] |
| /Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Projekt_Bachelor_DATENAUFBEREITUNG/thesis_workflow_output/01_universe_initial.csv                                 | cap_group         | 0                                | {'ALL': 568}             | nan    | nan                                                                                                                                                                                                                                                                        | nan                                |
| /Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Projekt_Bachelor_DATENAUFBEREITUNG/thesis_workflow_output/01_universe_initial.csv                                 | download_priority | 568                              | {'A_CORE_DOWNLOAD': 568} | nan    | nan                                                                                                                                                                                                                                                                        | nan                                |

## 4. Warum S1_FULL_568 nicht automatisch das Hauptresultat ist

S1_FULL_568 nutzt alle Aktien mit gueltigen Preisfeatures und ist damit ein breites Bruttoergebnis. Text- und Hybridmodelle duerfen methodisch nur gegen S1 auf demselben Common-Universe verglichen werden, weil sonst Universumsabdeckung und Signalverfuegbarkeit vermischt werden.

## 5. Ergebnisse je Universum und Zeitlogik

| universe       | time_logic              | portfolio_variant         | coverage_filter   |   n_days |   cumulative_return |     cagr |   ann_vol |   sharpe |   max_drawdown |   avg_n_positions |   avg_turnover_total |
|:---------------|:------------------------|:--------------------------|:------------------|---------:|--------------------:|---------:|----------:|---------:|---------------:|------------------:|---------------------:|
| S1_COMMON_TEXT | conservative_lag1       | sector_neutral_robustness | all_days          |      365 |            0.536959 | 0.345469 |  0.12234  |  2.48834 |      -0.116632 |           36.4301 |             0.184871 |
| S1_COMMON_TEXT | conservative_lag1       | sector_neutral_robustness | coverage_80       |      352 |            0.521358 | 0.350396 |  0.123516 |  2.49545 |      -0.116632 |           36.8892 |             0.140587 |
| S1_COMMON_TEXT | conservative_lag1       | standard                  | all_days          |      365 |            0.698814 | 0.441767 |  0.129421 |  2.89387 |      -0.110383 |           33.389  |             0.166758 |
| S1_COMMON_TEXT | conservative_lag1       | standard                  | coverage_80       |      352 |            0.685271 | 0.453031 |  0.130825 |  2.92379 |      -0.110383 |           33.8778 |             0.125652 |
| S1_COMMON_TEXT | research_close_to_close | sector_neutral_robustness | all_days          |      366 |            0.541957 | 0.347387 |  0.122971 |  2.48783 |      -0.119218 |           36.4317 |             0.183927 |
| S1_COMMON_TEXT | research_close_to_close | sector_neutral_robustness | coverage_80       |      353 |            0.520177 | 0.3485   |  0.124187 |  2.47133 |      -0.119218 |           36.8895 |             0.137743 |
| S1_COMMON_TEXT | research_close_to_close | standard                  | all_days          |      366 |            0.641435 | 0.406652 |  0.13084  |  2.67526 |      -0.113543 |           33.3907 |             0.165664 |
| S1_COMMON_TEXT | research_close_to_close | standard                  | coverage_80       |      353 |            0.609396 | 0.404535 |  0.132494 |  2.63212 |      -0.113543 |           33.8782 |             0.122458 |
| S1_FULL_568    | conservative_lag1       | sector_neutral_robustness | all_days          |      365 |            0.368619 | 0.241912 |  0.115699 |  1.93141 |      -0.113234 |          114.86   |             0.189362 |
| S1_FULL_568    | conservative_lag1       | sector_neutral_robustness | coverage_80       |      352 |            0.358344 | 0.245153 |  0.116346 |  1.94373 |      -0.113234 |          116.52   |             0.146226 |
| S1_FULL_568    | conservative_lag1       | standard                  | all_days          |      365 |            0.447889 | 0.29114  |  0.120945 |  2.1745  |      -0.108317 |          111.83   |             0.158019 |
| S1_FULL_568    | conservative_lag1       | standard                  | coverage_80       |      352 |            0.444522 | 0.301212 |  0.121516 |  2.22887 |      -0.108317 |          113.511  |             0.11722  |
| S1_FULL_568    | research_close_to_close | sector_neutral_robustness | all_days          |      366 |            0.363799 | 0.238166 |  0.115942 |  1.90154 |      -0.120384 |          114.866  |             0.188385 |
| S1_FULL_568    | research_close_to_close | sector_neutral_robustness | coverage_80       |      353 |            0.328657 | 0.224904 |  0.117212 |  1.79025 |      -0.120384 |          116.521  |             0.143973 |
| S1_FULL_568    | research_close_to_close | standard                  | all_days          |      366 |            0.46767  | 0.30235  |  0.121522 |  2.23599 |      -0.109295 |          111.836  |             0.158224 |
| S1_FULL_568    | research_close_to_close | standard                  | coverage_80       |      353 |            0.428176 | 0.289721 |  0.12277  |  2.13505 |      -0.109295 |          113.513  |             0.115695 |

## 6. Brutto vs. Netto bei 5/10/25 bps

| universe       | time_logic              | portfolio_variant         | coverage_filter   |   cost_bps |   n_days |   cumulative_return |      cagr |   ann_vol |   sharpe |   max_drawdown |
|:---------------|:------------------------|:--------------------------|:------------------|-----------:|---------:|--------------------:|----------:|----------:|---------:|---------------:|
| S1_COMMON_TEXT | conservative_lag1       | sector_neutral_robustness | all_days          |          5 |      365 |            0.485989 | 0.314503  |  0.122471 | 2.29548  |      -0.117309 |
| S1_COMMON_TEXT | conservative_lag1       | sector_neutral_robustness | all_days          |         10 |      365 |            0.436699 | 0.284243  |  0.122625 | 2.10263  |      -0.117985 |
| S1_COMMON_TEXT | conservative_lag1       | sector_neutral_robustness | all_days          |         25 |      365 |            0.298367 | 0.197542  |  0.123232 | 1.52521  |      -0.122719 |
| S1_COMMON_TEXT | conservative_lag1       | sector_neutral_robustness | coverage_80       |          5 |      352 |            0.4842   | 0.326701  |  0.123583 | 2.35075  |      -0.117309 |
| S1_COMMON_TEXT | conservative_lag1       | sector_neutral_robustness | coverage_80       |         10 |      352 |            0.447946 | 0.303419  |  0.123656 | 2.20611  |      -0.117985 |
| S1_COMMON_TEXT | conservative_lag1       | sector_neutral_robustness | coverage_80       |         25 |      352 |            0.34439  | 0.235983  |  0.123909 | 1.77274  |      -0.122719 |
| S1_COMMON_TEXT | conservative_lag1       | standard                  | all_days          |          5 |      365 |            0.647922 | 0.411808  |  0.129546 | 2.72888  |      -0.110782 |
| S1_COMMON_TEXT | conservative_lag1       | standard                  | all_days          |         10 |      365 |            0.598546 | 0.382465  |  0.129691 | 2.56384  |      -0.111181 |
| S1_COMMON_TEXT | conservative_lag1       | standard                  | all_days          |         25 |      365 |            0.459064 | 0.298012  |  0.130235 | 2.06911  |      -0.112378 |
| S1_COMMON_TEXT | conservative_lag1       | standard                  | coverage_80       |          5 |      352 |            0.648456 | 0.430236  |  0.130841 | 2.80242  |      -0.110782 |
| S1_COMMON_TEXT | conservative_lag1       | standard                  | coverage_80       |         10 |      352 |            0.612443 | 0.407797  |  0.130862 | 2.681    |      -0.111181 |
| S1_COMMON_TEXT | conservative_lag1       | standard                  | coverage_80       |         25 |      352 |            0.509036 | 0.342557  |  0.130948 | 2.31652  |      -0.112378 |
| S1_COMMON_TEXT | research_close_to_close | sector_neutral_robustness | all_days          |          5 |      366 |            0.490966 | 0.316548  |  0.12301  | 2.29863  |      -0.120129 |
| S1_COMMON_TEXT | research_close_to_close | sector_neutral_robustness | all_days          |         10 |      366 |            0.44165  | 0.286409  |  0.123073 | 2.10915  |      -0.121039 |
| S1_COMMON_TEXT | research_close_to_close | sector_neutral_robustness | all_days          |         25 |      366 |            0.303219 | 0.200031  |  0.123405 | 1.5401   |      -0.123781 |
| S1_COMMON_TEXT | research_close_to_close | sector_neutral_robustness | coverage_80       |          5 |      353 |            0.483694 | 0.325316  |  0.124228 | 2.3308   |      -0.120129 |
| S1_COMMON_TEXT | research_close_to_close | sector_neutral_robustness | coverage_80       |         10 |      353 |            0.448083 | 0.302529  |  0.124275 | 2.19026  |      -0.121039 |
| S1_COMMON_TEXT | research_close_to_close | sector_neutral_robustness | coverage_80       |         25 |      353 |            0.346276 | 0.236478  |  0.124449 | 1.76883  |      -0.123781 |
| S1_COMMON_TEXT | research_close_to_close | standard                  | all_days          |          5 |      366 |            0.59247  | 0.377625  |  0.130876 | 2.51504  |      -0.11402  |
| S1_COMMON_TEXT | research_close_to_close | standard                  | all_days          |         10 |      366 |            0.544956 | 0.349191  |  0.130931 | 2.35455  |      -0.114498 |
| S1_COMMON_TEXT | research_close_to_close | standard                  | all_days          |         25 |      366 |            0.4107   | 0.267329  |  0.131213 | 1.87224  |      -0.11593  |
| S1_COMMON_TEXT | research_close_to_close | standard                  | coverage_80       |          5 |      353 |            0.575024 | 0.383055  |  0.132505 | 2.51547  |      -0.11402  |
| S1_COMMON_TEXT | research_close_to_close | standard                  | coverage_80       |         10 |      353 |            0.541383 | 0.361902  |  0.132518 | 2.39877  |      -0.114498 |
| S1_COMMON_TEXT | research_close_to_close | standard                  | coverage_80       |         25 |      353 |            0.444693 | 0.300351  |  0.132583 | 2.04846  |      -0.11593  |
| S1_FULL_568    | conservative_lag1       | sector_neutral_robustness | all_days          |          5 |      365 |            0.322158 | 0.21265   |  0.11573  | 1.72472  |      -0.114743 |
| S1_FULL_568    | conservative_lag1       | sector_neutral_robustness | all_days          |         10 |      365 |            0.277265 | 0.18407   |  0.115787 | 1.51781  |      -0.11625  |
| S1_FULL_568    | conservative_lag1       | sector_neutral_robustness | all_days          |         25 |      365 |            0.151473 | 0.102276  |  0.11611  | 0.89711  |      -0.120756 |
| S1_FULL_568    | conservative_lag1       | sector_neutral_robustness | coverage_80       |          5 |      352 |            0.323855 | 0.222437  |  0.116365 | 1.78507  |      -0.114743 |
| S1_FULL_568    | conservative_lag1       | sector_neutral_robustness | coverage_80       |         10 |      352 |            0.290238 | 0.200133  |  0.116389 | 1.62642  |      -0.11625  |
| S1_FULL_568    | conservative_lag1       | sector_neutral_robustness | coverage_80       |         25 |      352 |            0.194405 | 0.135622  |  0.116482 | 1.15058  |      -0.120756 |
| S1_FULL_568    | conservative_lag1       | standard                  | all_days          |          5 |      365 |            0.406752 | 0.265701  |  0.121013 | 2.00875  |      -0.108651 |
| S1_FULL_568    | conservative_lag1       | standard                  | all_days          |         10 |      365 |            0.366776 | 0.240757  |  0.121102 | 1.84286  |      -0.108985 |
| S1_FULL_568    | conservative_lag1       | standard                  | all_days          |         25 |      365 |            0.253488 | 0.168808  |  0.121502 | 1.34519  |      -0.109985 |
| S1_FULL_568    | conservative_lag1       | standard                  | coverage_80       |          5 |      352 |            0.415044 | 0.282146  |  0.121563 | 2.10651  |      -0.108651 |
| S1_FULL_568    | conservative_lag1       | standard                  | coverage_80       |         10 |      352 |            0.386164 | 0.263358  |  0.121613 | 1.98418  |      -0.108985 |
| S1_FULL_568    | conservative_lag1       | standard                  | coverage_80       |         25 |      352 |            0.302999 | 0.208619  |  0.121784 | 1.61757  |      -0.109985 |
| S1_FULL_568    | research_close_to_close | sector_neutral_robustness | all_days          |          5 |      366 |            0.317614 | 0.209141  |  0.115961 | 1.69653  |      -0.121916 |
| S1_FULL_568    | research_close_to_close | sector_neutral_robustness | all_days          |         10 |      366 |            0.272984 | 0.180791  |  0.116006 | 1.49126  |      -0.123445 |
| S1_FULL_568    | research_close_to_close | sector_neutral_robustness | all_days          |         25 |      366 |            0.14791  | 0.0996336 |  0.116289 | 0.875279 |      -0.128019 |
| S1_FULL_568    | research_close_to_close | sector_neutral_robustness | coverage_80       |          5 |      353 |            0.29533  | 0.202891  |  0.117279 | 1.63456  |      -0.121916 |
| S1_FULL_568    | research_close_to_close | sector_neutral_robustness | coverage_80       |         10 |      353 |            0.262835 | 0.181271  |  0.117349 | 1.47899  |      -0.123445 |
| S1_FULL_568    | research_close_to_close | sector_neutral_robustness | coverage_80       |         25 |      353 |            0.170143 | 0.118702  |  0.117583 | 1.01321  |      -0.128019 |
| S1_FULL_568    | research_close_to_close | standard                  | all_days          |          5 |      366 |            0.425826 | 0.27667   |  0.121512 | 2.07211  |      -0.10967  |
| S1_FULL_568    | research_close_to_close | standard                  | all_days          |         10 |      366 |            0.385167 | 0.251491  |  0.121524 | 1.90784  |      -0.110044 |
| S1_FULL_568    | research_close_to_close | standard                  | all_days          |         25 |      366 |            0.269965 | 0.178863  |  0.121698 | 1.41367  |      -0.111165 |
| S1_FULL_568    | research_close_to_close | standard                  | coverage_80       |          5 |      353 |            0.399326 | 0.271068  |  0.122806 | 2.01571  |      -0.10967  |
| S1_FULL_568    | research_close_to_close | standard                  | coverage_80       |         10 |      353 |            0.371056 | 0.252683  |  0.122846 | 1.89639  |      -0.110044 |
| S1_FULL_568    | research_close_to_close | standard                  | coverage_80       |         25 |      353 |            0.289616 | 0.1991    |  0.122984 | 1.53867  |      -0.111165 |

## 7. Benchmarkvergleich nur auf matched days

| universe       | time_logic              | portfolio_variant         | coverage_filter   |   n_matched_days | benchmark_symbol   |   strategy_cum_return_matched |   benchmark_cum_return_matched |   active_cum_return |   tracking_error |   information_ratio |
|:---------------|:------------------------|:--------------------------|:------------------|-----------------:|:-------------------|------------------------------:|-------------------------------:|--------------------:|-----------------:|--------------------:|
| S1_COMMON_TEXT | conservative_lag1       | sector_neutral_robustness | all_days          |              355 | ^STOXX             |                      0.532939 |                       0.158249 |            0.323497 |        0.0691713 |             2.85223 |
| S1_COMMON_TEXT | conservative_lag1       | sector_neutral_robustness | coverage_80       |              348 | ^STOXX             |                      0.543986 |                       0.196874 |            0.290016 |        0.0650807 |             2.81613 |
| S1_COMMON_TEXT | conservative_lag1       | standard                  | all_days          |              355 | ^STOXX             |                      0.702953 |                       0.158249 |            0.470283 |        0.0831149 |             3.28279 |
| S1_COMMON_TEXT | conservative_lag1       | standard                  | coverage_80       |              348 | ^STOXX             |                      0.712873 |                       0.196874 |            0.431123 |        0.079496  |             3.26218 |
| S1_COMMON_TEXT | research_close_to_close | sector_neutral_robustness | all_days          |              356 | ^STOXX             |                      0.537487 |                       0.1559   |            0.330121 |        0.0691151 |             2.89919 |
| S1_COMMON_TEXT | research_close_to_close | sector_neutral_robustness | coverage_80       |              349 | ^STOXX             |                      0.541357 |                       0.149916 |            0.340408 |        0.0690767 |             3.03937 |
| S1_COMMON_TEXT | research_close_to_close | standard                  | all_days          |              356 | ^STOXX             |                      0.644986 |                       0.1559   |            0.423122 |        0.0816086 |             3.05336 |
| S1_COMMON_TEXT | research_close_to_close | standard                  | coverage_80       |              349 | ^STOXX             |                      0.636543 |                       0.149916 |            0.423184 |        0.0818309 |             3.10638 |
| S1_FULL_568    | conservative_lag1       | sector_neutral_robustness | all_days          |              355 | ^STOXX             |                      0.363338 |                       0.158249 |            0.177069 |        0.0635016 |             1.78191 |
| S1_FULL_568    | conservative_lag1       | sector_neutral_robustness | coverage_80       |              348 | ^STOXX             |                      0.383818 |                       0.196874 |            0.156194 |        0.0594066 |             1.73391 |
| S1_FULL_568    | conservative_lag1       | standard                  | all_days          |              355 | ^STOXX             |                      0.440942 |                       0.158249 |            0.24407  |        0.0724749 |             2.11232 |
| S1_FULL_568    | conservative_lag1       | standard                  | coverage_80       |              348 | ^STOXX             |                      0.468803 |                       0.196874 |            0.2272   |        0.0690431 |             2.126   |
| S1_FULL_568    | research_close_to_close | sector_neutral_robustness | all_days          |              356 | ^STOXX             |                      0.356792 |                       0.1559   |            0.173797 |        0.0637069 |             1.74097 |
| S1_FULL_568    | research_close_to_close | sector_neutral_robustness | coverage_80       |              349 | ^STOXX             |                      0.35246  |                       0.149916 |            0.176138 |        0.0636293 |             1.80099 |
| S1_FULL_568    | research_close_to_close | standard                  | all_days          |              356 | ^STOXX             |                      0.461024 |                       0.1559   |            0.263972 |        0.0727545 |             2.25375 |
| S1_FULL_568    | research_close_to_close | standard                  | coverage_80       |              349 | ^STOXX             |                      0.453366 |                       0.149916 |            0.263889 |        0.0727593 |             2.29797 |

## 8. Coverage-Analyse

| universe       | time_logic              | portfolio_variant         | coverage_filter   |   n_days |   min_coverage |   median_coverage |   mean_coverage |   max_coverage |   mean_positions |
|:---------------|:------------------------|:--------------------------|:------------------|---------:|---------------:|------------------:|----------------:|---------------:|-----------------:|
| S1_COMMON_TEXT | conservative_lag1       | sector_neutral_robustness | all_days          |      365 |       0.317365 |                 1 |        0.982594 |              1 |          36.4301 |
| S1_COMMON_TEXT | conservative_lag1       | sector_neutral_robustness | coverage_80       |      352 |       0.850299 |                 1 |        0.996836 |              1 |          36.8892 |
| S1_COMMON_TEXT | conservative_lag1       | standard                  | all_days          |      365 |       0.317365 |                 1 |        0.982594 |              1 |          33.389  |
| S1_COMMON_TEXT | conservative_lag1       | standard                  | coverage_80       |      352 |       0.850299 |                 1 |        0.996836 |              1 |          33.8778 |
| S1_COMMON_TEXT | research_close_to_close | sector_neutral_robustness | all_days          |      366 |       0.317365 |                 1 |        0.982641 |              1 |          36.4317 |
| S1_COMMON_TEXT | research_close_to_close | sector_neutral_robustness | coverage_80       |      353 |       0.850299 |                 1 |        0.996845 |              1 |          36.8895 |
| S1_COMMON_TEXT | research_close_to_close | standard                  | all_days          |      366 |       0.317365 |                 1 |        0.982641 |              1 |          33.3907 |
| S1_COMMON_TEXT | research_close_to_close | standard                  | coverage_80       |      353 |       0.850299 |                 1 |        0.996845 |              1 |          33.8782 |
| S1_FULL_568    | conservative_lag1       | sector_neutral_robustness | all_days          |      365 |       0.248239 |                 1 |        0.980885 |              1 |         114.86   |
| S1_FULL_568    | conservative_lag1       | sector_neutral_robustness | coverage_80       |      352 |       0.802817 |                 1 |        0.995669 |              1 |         116.52   |
| S1_FULL_568    | conservative_lag1       | standard                  | all_days          |      365 |       0.248239 |                 1 |        0.980885 |              1 |         111.83   |
| S1_FULL_568    | conservative_lag1       | standard                  | coverage_80       |      352 |       0.802817 |                 1 |        0.995669 |              1 |         113.511  |
| S1_FULL_568    | research_close_to_close | sector_neutral_robustness | all_days          |      366 |       0.248239 |                 1 |        0.980937 |              1 |         114.866  |
| S1_FULL_568    | research_close_to_close | sector_neutral_robustness | coverage_80       |      353 |       0.804577 |                 1 |        0.995681 |              1 |         116.521  |
| S1_FULL_568    | research_close_to_close | standard                  | all_days          |      366 |       0.248239 |                 1 |        0.980937 |              1 |         111.836  |
| S1_FULL_568    | research_close_to_close | standard                  | coverage_80       |      353 |       0.804577 |                 1 |        0.995681 |              1 |         113.513  |

## 9. IC- und Quintile-Test

| universe       | time_logic              |   mean_ic |   median_ic |   positive_ic_share |   ic_t_stat |   n_ic_days |
|:---------------|:------------------------|----------:|------------:|--------------------:|------------:|------------:|
| S1_COMMON_TEXT | conservative_lag1       | 0.0321761 |   0.0490557 |            0.564384 |     2.94746 |         365 |
| S1_COMMON_TEXT | research_close_to_close | 0.033311  |   0.0419151 |            0.568306 |     3.05185 |         366 |
| S1_FULL_568    | conservative_lag1       | 0.0285049 |   0.0387967 |            0.591781 |     2.72795 |         365 |
| S1_FULL_568    | research_close_to_close | 0.0293683 |   0.0371969 |            0.590164 |     2.80489 |         366 |

| universe       | time_logic              |   score_quintile |   mean_ret_fwd_1d |   n_days |   q5_minus_q1_spread |
|:---------------|:------------------------|-----------------:|------------------:|---------:|---------------------:|
| S1_COMMON_TEXT | conservative_lag1       |                1 |       0.00105609  |      365 |          0.000428279 |
| S1_COMMON_TEXT | conservative_lag1       |                2 |       0.000672773 |      365 |          0.000428279 |
| S1_COMMON_TEXT | conservative_lag1       |                3 |       0.000992076 |      365 |          0.000428279 |
| S1_COMMON_TEXT | conservative_lag1       |                4 |       0.000576285 |      365 |          0.000428279 |
| S1_COMMON_TEXT | conservative_lag1       |                5 |       0.00148437  |      365 |          0.000428279 |
| S1_COMMON_TEXT | research_close_to_close |                1 |       0.00101334  |      366 |          0.000377457 |
| S1_COMMON_TEXT | research_close_to_close |                2 |       0.000742436 |      366 |          0.000377457 |
| S1_COMMON_TEXT | research_close_to_close |                3 |       0.000889184 |      366 |          0.000377457 |
| S1_COMMON_TEXT | research_close_to_close |                4 |       0.000754176 |      366 |          0.000377457 |
| S1_COMMON_TEXT | research_close_to_close |                5 |       0.0013908   |      366 |          0.000377457 |
| S1_FULL_568    | conservative_lag1       |                1 |       0.000665459 |      365 |          0.000377591 |
| S1_FULL_568    | conservative_lag1       |                2 |       0.000637993 |      365 |          0.000377591 |
| S1_FULL_568    | conservative_lag1       |                3 |       0.000555383 |      365 |          0.000377591 |
| S1_FULL_568    | conservative_lag1       |                4 |       0.000680458 |      365 |          0.000377591 |
| S1_FULL_568    | conservative_lag1       |                5 |       0.00104305  |      365 |          0.000377591 |
| S1_FULL_568    | research_close_to_close |                1 |       0.000699947 |      366 |          0.000377168 |
| S1_FULL_568    | research_close_to_close |                2 |       0.000580091 |      366 |          0.000377168 |
| S1_FULL_568    | research_close_to_close |                3 |       0.000510203 |      366 |          0.000377168 |
| S1_FULL_568    | research_close_to_close |                4 |       0.000697925 |      366 |          0.000377168 |
| S1_FULL_568    | research_close_to_close |                5 |       0.00107711  |      366 |          0.000377168 |

## 10. Sektor-/Laender-Tilts

Sektor-Exposure:
| universe       | time_logic        | portfolio_variant         | coverage_filter   | sector                 |   avg_portfolio_weight |   n_days |
|:---------------|:------------------|:--------------------------|:------------------|:-----------------------|-----------------------:|---------:|
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | all_days          | Financials             |              0.242503  |      365 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | all_days          | Industrials            |              0.215294  |      365 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | all_days          | Consumer Discretionary |              0.108477  |      365 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | all_days          | Health Care            |              0.0808056 |      365 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | all_days          | Materials              |              0.0807957 |      365 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | all_days          | Utilities              |              0.0548947 |      365 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | all_days          | Communication          |              0.0546343 |      365 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | all_days          | Energy                 |              0.0545969 |      364 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | all_days          | Consumer Staples       |              0.0544732 |      365 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | all_days          | Information Technology |              0.0536746 |      365 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | coverage_80       | Financials             |              0.24287   |      352 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | coverage_80       | Industrials            |              0.215828  |      352 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | coverage_80       | Consumer Discretionary |              0.108293  |      352 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | coverage_80       | Materials              |              0.0810902 |      352 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | coverage_80       | Health Care            |              0.0809334 |      352 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | coverage_80       | Communication          |              0.0542326 |      352 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | coverage_80       | Consumer Staples       |              0.0542326 |      352 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | coverage_80       | Energy                 |              0.0542326 |      352 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | coverage_80       | Utilities              |              0.0542326 |      352 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | coverage_80       | Information Technology |              0.0540551 |      352 |
| S1_COMMON_TEXT | conservative_lag1 | standard                  | all_days          | Financials             |              0.447326  |      365 |
| S1_COMMON_TEXT | conservative_lag1 | standard                  | all_days          | Industrials            |              0.166747  |      365 |
| S1_COMMON_TEXT | conservative_lag1 | standard                  | all_days          | Communication          |              0.0930452 |      356 |
| S1_COMMON_TEXT | conservative_lag1 | standard                  | all_days          | Consumer Discretionary |              0.0867513 |      341 |
| S1_COMMON_TEXT | conservative_lag1 | standard                  | all_days          | Consumer Staples       |              0.0673985 |      304 |
| S1_COMMON_TEXT | conservative_lag1 | standard                  | all_days          | Utilities              |              0.0659551 |      343 |
| S1_COMMON_TEXT | conservative_lag1 | standard                  | all_days          | Health Care            |              0.0554375 |      226 |
| S1_COMMON_TEXT | conservative_lag1 | standard                  | all_days          | Energy                 |              0.0376587 |      217 |
| S1_COMMON_TEXT | conservative_lag1 | standard                  | all_days          | Information Technology |              0.0374804 |      295 |
| S1_COMMON_TEXT | conservative_lag1 | standard                  | all_days          | Materials              |              0.0296192 |      111 |

Laender-Exposure:
| universe       | time_logic        | portfolio_variant         | coverage_filter   | country        |   avg_portfolio_weight |   n_days |
|:---------------|:------------------|:--------------------------|:------------------|:---------------|-----------------------:|---------:|
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | all_days          | Austria        |              0.0274159 |      237 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | all_days          | Belgium        |              0.0431961 |      356 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | all_days          | Denmark        |              0.0309385 |      260 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | all_days          | Finland        |              0.0517549 |      268 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | all_days          | France         |              0.0772627 |      363 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | all_days          | Germany        |              0.0645771 |      348 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | all_days          | Italy          |              0.109207  |      357 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | all_days          | Netherlands    |              0.0379924 |      315 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | all_days          | Norway         |              0.094773  |      351 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | all_days          | Poland         |              0.027027  |       10 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | all_days          | Portugal       |              0.0279822 |       98 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | all_days          | Spain          |              0.153662  |      364 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | all_days          | Sweden         |              0.0370638 |      259 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | all_days          | United Kingdom |              0.328485  |      359 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | coverage_80       | Austria        |              0.0272434 |      233 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | coverage_80       | Belgium        |              0.0425577 |      344 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | coverage_80       | Denmark        |              0.0305912 |      256 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | coverage_80       | Finland        |              0.0510346 |      265 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | coverage_80       | France         |              0.0753707 |      351 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | coverage_80       | Germany        |              0.0639957 |      340 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | coverage_80       | Italy          |              0.108737  |      350 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | coverage_80       | Netherlands    |              0.0373164 |      306 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | coverage_80       | Norway         |              0.0937595 |      347 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | coverage_80       | Poland         |              0.027027  |       10 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | coverage_80       | Portugal       |              0.0270967 |       94 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | coverage_80       | Spain          |              0.151067  |      352 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | coverage_80       | Sweden         |              0.0367469 |      256 |
| S1_COMMON_TEXT | conservative_lag1 | sector_neutral_robustness | coverage_80       | United Kingdom |              0.323954  |      352 |
| S1_COMMON_TEXT | conservative_lag1 | standard                  | all_days          | Austria        |              0.0298885 |       69 |
| S1_COMMON_TEXT | conservative_lag1 | standard                  | all_days          | Belgium        |              0.0535011 |      352 |

## 11. Validierungschecks

| check_name                                                 | status   | detail                                                                                                                                                                                                                                                                                                                                                                                                            |
|:-----------------------------------------------------------|:---------|:------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| keine doppelten security_id-date-Zeilen im Input           | PASS     | duplicate rows: 0                                                                                                                                                                                                                                                                                                                                                                                                 |
| keine doppelten security_id-date-Zeilen in Positions       | PASS     | duplicate position rows: 0                                                                                                                                                                                                                                                                                                                                                                                        |
| Gewichte summieren pro Tag auf 1.0 +/- 1e-9                | PASS     | max abs error: 1.1102230246251565e-16                                                                                                                                                                                                                                                                                                                                                                             |
| Top-20-Auswahl korrekt                                     | PASS     | violating groups: 0                                                                                                                                                                                                                                                                                                                                                                                               |
| daily_return entspricht Summe(weight * ret_fwd_1d)         | PASS     | max abs error: 0.0                                                                                                                                                                                                                                                                                                                                                                                                |
| cumulative_return entspricht product(1 + daily_return) - 1 | PASS     | max abs error: 0.0                                                                                                                                                                                                                                                                                                                                                                                                |
| Benchmark-Excess nur auf matched days                      | PASS     | matched rows: 5632                                                                                                                                                                                                                                                                                                                                                                                                |
| n_positions > 0 fuer alle Backtesttage                     | PASS     | zero-position days: 0                                                                                                                                                                                                                                                                                                                                                                                             |
| keine NaN/Inf in ausgewaehlten Renditen                    | PASS     | bad selected returns: 0                                                                                                                                                                                                                                                                                                                                                                                           |
| conservative_lag1 nutzt tatsaechlich gelaggten Score       | PASS     | score_lag1 equals groupwise previous score_research                                                                                                                                                                                                                                                                                                                                                               |
| VOL60/VOL63-Namenskonvention dokumentiert                  | PASS     | vol_convention=VOL60                                                                                                                                                                                                                                                                                                                                                                                              |
| Ergebnisse aus bestehender Results-DB nicht ueberschreiben | PASS     | input_results_db=/Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Bachelorarbeit_Praxis/RESULTS.sql; output_db=/Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Bachelorarbeit_Praxis/outputs/results/s1_revised_results.sqlite |

## 12. Methodische Empfehlung

- S1_CORE als Hauptbaseline verwenden, falls eindeutig verfuegbar.
- S1_FULL_568 als Robustheitsanalyse verwenden.
- S1_COMMON_TEXT fuer faire Vergleiche mit S2/S3/S4/Hybrid verwenden.