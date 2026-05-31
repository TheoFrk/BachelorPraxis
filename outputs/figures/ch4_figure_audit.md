# Chapter 4 Figure Audit

- Script version: `2026-05-30-ch4-figures-v1`
- Data root: `/Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Bachelorarbeit_Praxis`
- LaTeX root: `/Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Bachelorarbeit-3`
- Image dir: `/Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Bachelorarbeit-3/images`
- Figure output dir: `/Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Bachelorarbeit_Praxis/outputs/figures`
- Audit file: `/Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Bachelorarbeit_Praxis/outputs/figures/ch4_figure_audit.md`

## Input Files
- FOUND: s1 `/Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Bachelorarbeit_Praxis/outputs/results/s1_revised_results.sqlite`
- FOUND: s2 `/Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Bachelorarbeit_Praxis/outputs/results/s2_lexicon_results.sqlite`
- FOUND: s3 `/Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Bachelorarbeit_Praxis/outputs/results/s3_finbert_results.sqlite`
- FOUND: s4 `/Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Bachelorarbeit_Praxis/outputs/results/s4_llm_results.sqlite`
- FOUND: index fallback `/Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Bachelorarbeit_Praxis/visualitzation/index.html`
- FOUND: chapter check `/Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Bachelorarbeit-3/sections/4_empirical_results.tex`
- Chapter references ch4_coverage_overview: `False`
- Chapter references ch4_main_equity_curves: `False`
- WARN: Chapter file does not currently reference both new figure names. No .tex file was modified.

## Coverage Values
| Stufe | Zielwerte | Zusatz | News | Zusatz News | Anteil Positionen mit News | Quelle |
|---|---:|---:|---:|---:|---:|---|
| S1 | 568 | 292 | n/v | n/v | n/v | s1_universe_summary / s1_coverage_by_date; methodisch nicht anwendbar; nicht verfügbar |
| S2 | 292 | n/v | 267.577 | n/v | 47,7 % | s2_run_metadata / s2_model_comparison_summary; s2_lexicon_article_scores / s2_run_metadata; s2_model_comparison_summary |
| S3 | 291 | n/v | 267.577 | 517.072 | 56,4 % | s3_run_metadata / s3_model_comparison_summary; s3_run_metadata; s3_model_comparison_summary |
| S4 EN | 287 | n/v | 267.577 | n/v | 30,1 % | s4_model_comparison_summary; s4_run_metadata; s4_model_comparison_summary |
| S4 MULTI | 287 | n/v | 517.072 | n/v | 34,3 % | s4_model_comparison_summary; s4_run_metadata; s4_model_comparison_summary |

## Planned Equity Series
| Serie | Quelle | Equity-Spalte | Return-Spalte | Rohdaten n | Rohdaten Start | Rohdaten Ende |
|---|---|---|---|---:|---|---|
| S1 Price-only | s1_revised_results.sqlite:s1_daily_returns | equity | strategy_ret_fwd_1d | 366 | 2024-07-26 | 2025-12-30 |
| S2 Lexikon-only | s2_lexicon_results.sqlite:s2_daily_returns | equity | strategy_ret_fwd_1d | 619 | 2023-08-01 | 2025-12-30 |
| S1 + S2 Lexikon 50/50 | s2_lexicon_results.sqlite:s2_daily_returns | equity | strategy_ret_fwd_1d | 366 | 2024-07-26 | 2025-12-30 |
| S3 FinBERT-only | s3_finbert_results.sqlite:s3_daily_returns | equity | strategy_ret_fwd_1d | 619 | 2023-08-01 | 2025-12-30 |
| S1 + S3 FinBERT 50/50 | s3_finbert_results.sqlite:s3_daily_returns | equity | strategy_ret_fwd_1d | 619 | 2023-08-01 | 2025-12-30 |
| S4 LLM-only EN | s4_llm_results.sqlite:s4_daily_returns | equity | strategy_ret_fwd_1d | 619 | 2023-08-01 | 2025-12-30 |
| S1 + S4 LLM 50/50 EN | s4_llm_results.sqlite:s4_daily_returns | equity | strategy_ret_fwd_1d | 619 | 2023-08-01 | 2025-12-30 |
| S1 + S4 LLM 70/30 EN | s4_llm_results.sqlite:s4_daily_returns | equity | strategy_ret_fwd_1d | 619 | 2023-08-01 | 2025-12-30 |
| STOXX Europe 600 Benchmark | s4_llm_results.sqlite:s4_vs_benchmark_daily | benchmark_equity_matched | benchmark_ret_fwd_1d | 604 | 2023-08-02 | 2025-12-29 |

## Panel-Matched Equity Audit
| Panel | Serie | n_days | Start | Ende | End-Equity | Kumulierte Rendite | Quelle |
|---|---|---:|---|---|---:|---:|---|
| A | S1 Price-only | 356 | 2024-07-26 | 2025-12-29 | 1,607 | 60,7 % | s1_revised_results.sqlite:s1_daily_returns |
| A | STOXX Europe 600 Benchmark | 356 | 2024-07-26 | 2025-12-29 | 1,158 | 15,8 % | s4_llm_results.sqlite:s4_vs_benchmark_daily |
| B | S2 Lexikon-only | 604 | 2023-08-02 | 2025-12-29 | 1,657 | 65,7 % | s2_lexicon_results.sqlite:s2_daily_returns |
| B | S3 FinBERT-only | 604 | 2023-08-02 | 2025-12-29 | 1,433 | 43,3 % | s3_finbert_results.sqlite:s3_daily_returns |
| B | S4 LLM-only EN | 604 | 2023-08-02 | 2025-12-29 | 1,565 | 56,5 % | s4_llm_results.sqlite:s4_daily_returns |
| B | STOXX Europe 600 Benchmark | 604 | 2023-08-02 | 2025-12-29 | 1,294 | 29,4 % | s4_llm_results.sqlite:s4_vs_benchmark_daily |
| C | S1 Price-only | 366 | 2024-07-26 | 2025-12-30 | 1,605 | 60,5 % | s1_revised_results.sqlite:s1_daily_returns |
| C | S1 + S2 Lexikon 50/50 | 366 | 2024-07-26 | 2025-12-30 | 1,503 | 50,3 % | s2_lexicon_results.sqlite:s2_daily_returns |
| C | S1 + S3 FinBERT 50/50 | 366 | 2024-07-26 | 2025-12-30 | 1,450 | 45,0 % | s3_finbert_results.sqlite:s3_daily_returns |
| C | S1 + S4 LLM 50/50 EN | 366 | 2024-07-26 | 2025-12-30 | 1,557 | 55,7 % | s4_llm_results.sqlite:s4_daily_returns |
| D | S1 Price-only | 356 | 2024-07-26 | 2025-12-29 | 1,607 | 60,7 % | s1_revised_results.sqlite:s1_daily_returns |
| D | S1 + S4 LLM 50/50 EN | 356 | 2024-07-26 | 2025-12-29 | 1,558 | 55,8 % | s4_llm_results.sqlite:s4_daily_returns |
| D | S1 + S4 LLM 70/30 EN | 356 | 2024-07-26 | 2025-12-29 | 1,557 | 55,7 % | s4_llm_results.sqlite:s4_daily_returns |
| D | STOXX Europe 600 Benchmark | 356 | 2024-07-26 | 2025-12-29 | 1,158 | 15,8 % | s4_llm_results.sqlite:s4_vs_benchmark_daily |

## Validation Checks
- Input databases complete: `True`
- No NaN values in final plotted equity values: `True`
- Equity starts at 1.0 after normalization: `True`
- Coverage values source: DB metadata/output tables unless warnings below state fallback.
- Images successfully written: `True`
- Panel A line count: `2`
- Panel B line count: `4`
- Panel C line count: `4`
- Panel D line count: `4`

## Generated Files
- OK: `/Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Bachelorarbeit-3/images/ch4_coverage_overview.png` (257589 bytes)
- OK: `/Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Bachelorarbeit-3/images/ch4_coverage_overview.pdf` (33392 bytes)
- OK: `/Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Bachelorarbeit-3/images/ch4_main_equity_curves.png` (412877 bytes)
- OK: `/Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Bachelorarbeit-3/images/ch4_main_equity_curves.pdf` (78708 bytes)
- OK: `/Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Bachelorarbeit-3/images/ch4_main_equity_curves_compact.png` (259182 bytes)
- OK: `/Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Bachelorarbeit-3/images/ch4_main_equity_curves_hybrids_only.png` (291319 bytes)
- OK: `/Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Bachelorarbeit_Praxis/outputs/figures/ch4_coverage_data_used.csv` (1011 bytes)
- OK: `/Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Bachelorarbeit_Praxis/outputs/figures/ch4_equity_series_audit.csv` (6047 bytes)
- OK: `/Users/theo_frk/Library/Mobile Documents/com~apple~CloudDocs/Study/Literatur und Informationen/Betriebliches Ressourcenmanagment/Bachelor/Bachelorarbeit_Praxis/outputs/figures/ch4_equity_curve_points_used.csv` (346791 bytes)

## Warnings
- None
