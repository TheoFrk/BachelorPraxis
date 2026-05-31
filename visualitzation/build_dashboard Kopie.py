#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
RESULT_DB = ROOT / "outputs" / "results" / "s3_finbert_results.sqlite"
S2_RESULT_DB = ROOT / "outputs" / "results" / "s2_lexicon_results.sqlite"
S4_RESULT_DB = ROOT / "outputs" / "results" / "s4_llm_results.sqlite"
OUT_HTML = ROOT / "visualitzation" / "index.html"


PANEL_ORDER = [
    ("research_signal_date", "all_days"),
    ("research_signal_date", "coverage_80"),
    ("conservative_price_lag1", "all_days"),
    ("conservative_price_lag1", "coverage_80"),
]

SERIES_META = {
    "S1_PRICE_ONLY": {
        "label": "S1 price-only",
        "color": "#202124",
        "dash": "7 5",
        "description": "Momentum + Low Volatility baseline from the S2-vs-S1 matched-date comparison.",
    },
    "S2_LEXICON_ONLY": {
        "label": "S2 lexicon-only",
        "color": "#1f77b4",
        "dash": "",
        "description": "Pure Loughran-McDonald lexicon sentiment score from S2-vs-S1 matched dates.",
    },
    "HYBRID_S1_LEX_50_50": {
        "label": "S1 + S2 lex 50/50",
        "color": "#2ca02c",
        "dash": "",
        "description": "Late fusion: 50 percent S1 price score, 50 percent lexicon score from S2-vs-S1 matched dates.",
    },
    "S3_FINBERT_ONLY": {
        "label": "S3 FinBERT-only",
        "color": "#b54708",
        "dash": "",
        "description": "Pure FinBERT text signal on English FinBERT-ready news.",
    },
    "HYBRID_S1_FINBERT_50_50": {
        "label": "S1 + S3 FinBERT 50/50",
        "color": "#087443",
        "dash": "",
        "description": "Late fusion: 50 percent S1 price score, 50 percent FinBERT score.",
    },
    "BENCHMARK": {
        "label": "STOXX Europe 600",
        "color": "#d62728",
        "dash": "2 4",
        "description": "Benchmark compounded on the S2-vs-S1 matched dates, matching the S2 benchmark figure.",
    },
}

S4_SERIES_META = {
    "S4_LLM_ONLY": {
        "label": "{source} LLM-only {language}",
        "color": "#7a3e9d",
        "dash": "",
        "description": "Pure S4 LLM text signal, language mode {language}, source {source}.",
    },
    "HYBRID_S1_LLM_50_50": {
        "label": "{source} S1 + LLM 50/50 {language}",
        "color": "#0f766e",
        "dash": "6 3",
        "description": "Late fusion: 50 percent S1 price score, 50 percent S4 LLM score, language mode {language}, source {source}.",
    },
    "HYBRID_S1_LLM_70_30": {
        "label": "{source} S1 + LLM 70/30 {language}",
        "color": "#155e75",
        "dash": "10 4",
        "description": "Shrinkage robustness: 70 percent S1 price score, 30 percent S4 LLM score, language mode {language}, source {source}.",
    },
}

S4_MAIN_MODEL_FAMILIES = ["S4_LLM_ONLY", "HYBRID_S1_LLM_50_50", "HYBRID_S1_LLM_70_30"]

S4_SOURCES = [
    {
        "key": "current",
        "label": "S4 Current",
        "db": S4_RESULT_DB,
        "note": "Aktuelle S4-Ergebnisdatenbank. Waehrend des Full-Runs kann diese noch ein Smoke-/Zwischenstand sein.",
    },
]


def load_s4_source(source: dict) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    db_path = Path(source["db"])
    if not db_path.exists():
        return pd.DataFrame(), pd.DataFrame(), "not_found"
    try:
        with sqlite3.connect(db_path) as conn:
            daily = pd.read_sql_query(
                f"""
                SELECT *
                FROM s4_daily_returns
                WHERE news_mode = 'NEUTRAL_FILL'
                  AND portfolio_variant = 'standard'
                  AND model_family IN ({",".join(["?"] * len(S4_MAIN_MODEL_FAMILIES))})
                """,
                conn,
                params=S4_MAIN_MODEL_FAMILIES,
            )
            metadata = pd.read_sql_query("SELECT * FROM s4_run_metadata", conn)
        if daily.empty:
            return daily, metadata, "empty"
        daily["s4_source"] = source["key"]
        daily["s4_source_label"] = source["label"]
        return daily, metadata, "loaded"
    except (sqlite3.DatabaseError, pd.errors.DatabaseError) as exc:
        return pd.DataFrame(), pd.DataFrame(), f"unavailable: {exc}"


def load_data() -> dict:
    if not RESULT_DB.exists():
        raise FileNotFoundError(f"Missing result database: {RESULT_DB}")
    with sqlite3.connect(RESULT_DB) as conn:
        s3_daily = pd.read_sql_query(
            """
            SELECT *
            FROM s3_daily_returns
            WHERE news_mode = 'NEUTRAL_FILL'
              AND portfolio_variant = 'standard'
            """,
            conn,
        )
        metrics = pd.read_sql_query("SELECT * FROM s3_model_comparison_summary", conn)
        metadata = pd.read_sql_query("SELECT * FROM s3_run_metadata", conn)

    s2_vs_s1 = pd.DataFrame()
    s2_benchmark = pd.DataFrame()
    s2_status = "not_found"
    if S2_RESULT_DB.exists():
        try:
            with sqlite3.connect(S2_RESULT_DB) as conn:
                s2_vs_s1 = pd.read_sql_query(
                    """
                    SELECT *
                    FROM s2_vs_s1_daily
                    WHERE news_mode = 'NEUTRAL_FILL'
                      AND portfolio_variant = 'standard'
                    """,
                    conn,
                )
                s2_benchmark = pd.read_sql_query(
                    """
                    SELECT DISTINCT date, symbol, benchmark_ret_fwd_1d
                    FROM s2_vs_benchmark_daily
                    WHERE benchmark_ret_fwd_1d IS NOT NULL
                    """,
                    conn,
                )
            s2_status = "loaded" if not s2_vs_s1.empty else "empty"
        except (sqlite3.DatabaseError, pd.errors.DatabaseError) as exc:
            s2_status = f"unavailable: {exc}"

    s4_frames = []
    s4_metadata_by_source: dict[str, pd.DataFrame] = {}
    s4_status_by_source: dict[str, str] = {}
    for source in S4_SOURCES:
        daily_part, metadata_part, status = load_s4_source(source)
        s4_status_by_source[source["key"]] = status
        if not metadata_part.empty:
            s4_metadata_by_source[source["key"]] = metadata_part
        if status == "loaded":
            s4_frames.append(daily_part)
    s4_daily = pd.concat(s4_frames, ignore_index=True) if s4_frames else pd.DataFrame()

    s3_daily["date"] = pd.to_datetime(s3_daily["date"], errors="coerce")
    if not s2_vs_s1.empty:
        s2_vs_s1["date"] = pd.to_datetime(s2_vs_s1["date"], errors="coerce")
    if not s2_benchmark.empty:
        s2_benchmark["date"] = pd.to_datetime(s2_benchmark["date"], errors="coerce")
    if not s4_daily.empty:
        s4_daily["date"] = pd.to_datetime(s4_daily["date"], errors="coerce")
    series_meta = dict(SERIES_META)
    s4_groups: list[tuple[str, str, str]] = []
    if not s4_daily.empty and "language_mode" in s4_daily.columns:
        s4_group_frame = (
            s4_daily[["s4_source", "s4_source_label", "language_mode"]]
            .dropna()
            .drop_duplicates()
            .sort_values(["s4_source", "language_mode"])
        )
        s4_groups = [
            (str(row["s4_source"]), str(row["s4_source_label"]), str(row["language_mode"]))
            for _, row in s4_group_frame.iterrows()
        ]
        for source_key, source_label, language in s4_groups:
            for model_family in S4_MAIN_MODEL_FAMILIES:
                meta = S4_SERIES_META[model_family]
                series_meta[s4_series_id(model_family, language, source_key)] = {
                    "label": meta["label"].format(language=language, source=source_label),
                    "color": meta["color"],
                    "dash": meta["dash"],
                    "description": meta["description"].format(language=language, source=source_label),
                }

    panels: dict[str, dict] = {}
    for time_logic, coverage_filter in PANEL_ORDER:
        key = f"{time_logic}|{coverage_filter}"
        panel = s3_daily[(s3_daily["time_logic"] == time_logic) & (s3_daily["coverage_filter"] == coverage_filter)].copy()
        series: dict[str, list[dict]] = {}
        if not panel.empty:
            for model_family in ["S3_FINBERT_ONLY", "HYBRID_S1_FINBERT_50_50"]:
                model = panel[panel["model_family"] == model_family].sort_values("date")
                series[model_family] = points_from_equity(model, "equity")

            s2_matched_panel = (
                s2_vs_s1[(s2_vs_s1["time_logic"] == time_logic) & (s2_vs_s1["coverage_filter"] == coverage_filter)].copy()
                if not s2_vs_s1.empty
                else pd.DataFrame()
            )
            matched_dates: set[pd.Timestamp] = set()
            if not s2_matched_panel.empty:
                s1_line = s2_matched_panel.sort_values("date").drop_duplicates("date")
                series["S1_PRICE_ONLY"] = points_from_equity(s1_line, "s1_equity_matched")
                matched_dates = set(s1_line["date"].dropna())

                lex_line = s2_matched_panel[s2_matched_panel["model_family"].astype(str).eq("S2_LEXICON_ONLY")].sort_values("date")
                hybrid_lex_line = s2_matched_panel[s2_matched_panel["model_family"].astype(str).eq("HYBRID_S1_LEX_50_50")].sort_values("date")
                series["S2_LEXICON_ONLY"] = points_from_equity(lex_line.drop_duplicates("date"), "s2_equity_matched")
                series["HYBRID_S1_LEX_50_50"] = points_from_equity(hybrid_lex_line.drop_duplicates("date"), "s2_equity_matched")

            bench_line = (
                s2_benchmark[s2_benchmark["date"].isin(matched_dates)].drop_duplicates("date").sort_values("date").copy()
                if matched_dates
                else pd.DataFrame()
            )
            if not bench_line.empty:
                bench_line["benchmark_equity"] = (1.0 + bench_line["benchmark_ret_fwd_1d"]).cumprod()
                series["BENCHMARK"] = points_from_equity(bench_line, "benchmark_equity")

            if not s4_daily.empty:
                s4_panel = s4_daily[(s4_daily["time_logic"] == time_logic) & (s4_daily["coverage_filter"] == coverage_filter)].copy()
                for source_key, _source_label, language in s4_groups:
                    language_panel = s4_panel[
                        (s4_panel["language_mode"].astype(str) == language)
                        & (s4_panel["s4_source"].astype(str) == source_key)
                    ]
                    for model_family in S4_MAIN_MODEL_FAMILIES:
                        model = language_panel[language_panel["model_family"] == model_family].sort_values("date")
                        if not model.empty:
                            series[s4_series_id(model_family, language, source_key)] = points_from_equity(model, "equity")
        panels[key] = {
            "time_logic": time_logic,
            "coverage_filter": coverage_filter,
            "title": f"{time_logic.replace('_', ' ')} / {coverage_filter}",
            "series": series,
        }

    metric_rows = []
    if not metrics.empty:
        keep = metrics[metrics["strategy_variant"].astype(str).str.contains("NEUTRAL_FILL", na=False)].copy()
        if "portfolio_variant" in keep.columns:
            keep = keep[keep["portfolio_variant"] == "standard"].copy()
        if "n_days" in keep.columns:
            keep = keep[keep["n_days"].fillna(0).astype(float) > 0].copy()
        for _, row in keep.iterrows():
            metric_rows.append(
                {
                    "model_family": row.get("model_family"),
                    "strategy_variant": row.get("strategy_variant"),
                    "time_logic": row.get("time_logic"),
                    "coverage_filter": row.get("coverage_filter"),
                    "n_days": safe_number(row.get("n_days")),
                    "n_target_securities": safe_number(row.get("n_target_securities")),
                    "avg_n_positions": safe_number(row.get("avg_n_positions")),
                    "avg_coverage_ratio": safe_number(row.get("avg_coverage_ratio")),
                    "avg_share_positions_with_news": safe_number(row.get("avg_share_positions_with_news")),
                    "cumulative_return": safe_number(row.get("cumulative_return")),
                    "annualized_return": safe_number(row.get("annualized_return_period_geometric")),
                    "sharpe": safe_number(row.get("sharpe")),
                    "max_drawdown": safe_number(row.get("max_drawdown")),
                    "active_cum_return_vs_s1": safe_number(row.get("active_cum_return_vs_s1")),
                }
            )

    meta = {str(row["key"]): row["value"] for _, row in metadata.iterrows()} if not metadata.empty else {}
    meta["s2_status"] = s2_status
    if s2_status == "loaded":
        meta["s2_output_db"] = str(S2_RESULT_DB)
    for source in S4_SOURCES:
        source_key = source["key"]
        status = s4_status_by_source.get(source_key, "not_found")
        if status != "not_found":
            meta[f"s4_{source_key}_status"] = status
        metadata_part = s4_metadata_by_source.get(source_key, pd.DataFrame())
        if not metadata_part.empty:
            meta[f"s4_{source_key}_output_db"] = str(source["db"])
            s4_run_at = metadata_part[metadata_part["key"].astype(str) == "run_at_utc"]
            if not s4_run_at.empty:
                meta[f"s4_{source_key}_run_at_utc"] = str(s4_run_at.iloc[-1]["value"])
    generated_sources = [str(RESULT_DB.relative_to(ROOT))]
    if s2_status == "loaded":
        generated_sources.append(str(S2_RESULT_DB.relative_to(ROOT)))
    for source in S4_SOURCES:
        if s4_status_by_source.get(source["key"]) == "loaded":
            generated_sources.append(str(Path(source["db"]).relative_to(ROOT)))
    return {
        "generated_from": str(RESULT_DB.relative_to(ROOT)),
        "generated_sources": generated_sources,
        "metadata": meta,
        "series_meta": series_meta,
        "panels": panels,
        "metrics": metric_rows,
        "universe_notes": [
            {"name": "S3_COMMON_FINBERT_EN_ANY", "text": "Hauptuniversum bei security_news_coverage_threshold 0.0: gueltige Preisfeatures, Folgerenditen und mindestens eine English/NULL FinBERT-faehige News nach Textfilter."},
            {"name": "S3_COMMON_FINBERT_EN_COV80", "text": "Tagesfilter coverage_80: nur Handelstage mit coverage_ratio mindestens 0.80; das Portfolio-Datum bleibt signal_date."},
            {"name": "S3_FINBERT_ONLY_NEUTRAL_FILL", "text": "Reine FinBERT-Textsignal-Variante. Fehlende Tagesnews werden neutral gefuellt; das ist die Hauptstrategie."},
            {"name": "HYBRID_S1_FINBERT_50_50_NEUTRAL_FILL", "text": "Late Fusion mit 50 Prozent S1-Price-Score und 50 Prozent FinBERT-Textscore; Gewichtung ex ante gesetzt."},
            {"name": "S4_LLM_ONLY_NEUTRAL_FILL", "text": "Reine S4-LLM-Textsignal-Variante; wird automatisch angezeigt, sobald die S4-Ergebnisdaten vorhanden sind."},
            {"name": "HYBRID_S1_LLM_50_50_NEUTRAL_FILL", "text": "Late Fusion mit 50 Prozent S1-Price-Score und 50 Prozent S4-LLM-Score; je vorhandener Sprache als eigene Linie."},
            {"name": "HYBRID_S1_LLM_70_30_NEUTRAL_FILL", "text": "Shrinkage-Robustheit mit 70 Prozent S1 und 30 Prozent LLM; sichtbar, sobald die jeweilige S4-Quelle diese Variante enthaelt."},
        ],
    }


def s4_series_id(model_family: str, language: str, source_key: str) -> str:
    clean_language = str(language).upper().replace(" ", "_")
    clean_source = str(source_key).upper().replace(" ", "_")
    return f"{model_family}_{clean_language}_{clean_source}"


def points_from_equity(df: pd.DataFrame, col: str) -> list[dict]:
    out = []
    for _, row in df.dropna(subset=["date", col]).iterrows():
        equity = float(row[col])
        out.append(
            {
                "date": row["date"].strftime("%Y-%m-%d"),
                "equity": equity,
                "ret": equity - 1.0,
            }
        )
    return out


def safe_number(value):
    if value is None or pd.isna(value):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def build_html(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    sources = ", ".join(data.get("generated_sources") or [data["generated_from"]])
    return f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>S1 / S2 / S3 / S4 Interactive Backtest Dashboard</title>
  <style>
    :root {{
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #1f2933;
      --muted: #667085;
      --line: #d8dde6;
      --accent: #2457a6;
      --shadow: 0 8px 24px rgba(17, 24, 39, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }}
    header {{
      padding: 22px 28px 12px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }}
    h1 {{ margin: 0 0 8px; font-size: 24px; font-weight: 700; letter-spacing: 0; }}
    .sub {{ margin: 0; color: var(--muted); font-size: 14px; overflow-wrap: anywhere; }}
    main {{ padding: 18px 28px 32px; max-width: 1480px; margin: 0 auto; }}
    .toolbar {{
      display: grid;
      grid-template-columns: minmax(220px, 1fr) minmax(220px, 1fr) minmax(220px, 1fr) auto;
      gap: 12px;
      align-items: end;
      margin-bottom: 14px;
    }}
    .control, .card, .chart-wrap, .table-wrap, .notes {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }}
    .control {{ padding: 12px; }}
    label.caption {{ display: block; color: var(--muted); font-size: 12px; margin-bottom: 7px; }}
    select, button {{
      width: 100%;
      border: 1px solid #cbd3df;
      background: #fff;
      border-radius: 6px;
      padding: 9px 10px;
      color: var(--text);
      font-size: 14px;
    }}
    button {{ cursor: pointer; font-weight: 600; color: var(--accent); }}
    .segmented {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 4px;
      padding: 3px;
      border: 1px solid #cbd3df;
      border-radius: 6px;
      background: #eef2f7;
    }}
    .segmented button {{
      border: 0;
      background: transparent;
      color: var(--muted);
      padding: 7px 9px;
      box-shadow: none;
    }}
    .segmented button.active {{
      background: #fff;
      color: var(--accent);
      box-shadow: 0 1px 4px rgba(17,24,39,.10);
    }}
    .legend {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin: 12px 0 14px;
    }}
    .toggle {{
      display: inline-flex;
      align-items: center;
      gap: 7px;
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 8px 12px;
      box-shadow: 0 2px 8px rgba(17,24,39,.05);
      cursor: pointer;
      font-size: 14px;
    }}
    .swatch {{ width: 22px; height: 3px; border-radius: 2px; display: inline-block; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(140px, 1fr));
      gap: 12px;
      margin-bottom: 14px;
    }}
    .card {{ padding: 13px 14px; min-height: 82px; }}
    .card .k {{ color: var(--muted); font-size: 12px; }}
    .card .v {{ font-size: 23px; font-weight: 700; margin-top: 5px; }}
    .card .d {{ color: var(--muted); font-size: 12px; margin-top: 4px; }}
    .chart-wrap {{ padding: 14px; position: relative; }}
    #chart {{ width: 100%; height: 560px; display: block; }}
    .tooltip {{
      position: absolute;
      pointer-events: none;
      background: rgba(255,255,255,.96);
      border: 1px solid #cbd3df;
      border-radius: 8px;
      padding: 10px 11px;
      box-shadow: var(--shadow);
      min-width: 230px;
      font-size: 13px;
      display: none;
      z-index: 5;
    }}
    .tooltip .date {{ font-weight: 700; margin-bottom: 6px; }}
    .tooltip-row {{ display: flex; justify-content: space-between; gap: 14px; margin: 3px 0; }}
    .table-wrap, .notes {{ padding: 14px; margin-top: 14px; }}
    .table-wrap {{ overflow-x: auto; }}
    table {{ width: 100%; min-width: 1180px; border-collapse: collapse; font-size: 13px; }}
    th, td {{ text-align: right; border-bottom: 1px solid #edf0f5; padding: 8px 7px; }}
    th:first-child, td:first-child {{ text-align: left; }}
    th {{ color: var(--muted); font-weight: 600; }}
    .notes-grid {{ display: grid; grid-template-columns: repeat(2, minmax(240px, 1fr)); gap: 10px; }}
    .note-item {{ border: 1px solid #edf0f5; border-radius: 7px; padding: 10px; background: #fbfcfe; }}
    .note-item strong {{ display: block; margin-bottom: 4px; }}
    @media (max-width: 900px) {{
      main, header {{ padding-left: 14px; padding-right: 14px; }}
      .toolbar {{ grid-template-columns: 1fr; }}
      .grid {{ grid-template-columns: repeat(2, 1fr); }}
      #chart {{ height: 430px; }}
      .notes-grid {{ grid-template-columns: 1fr; }}
    }}
    @media (max-width: 520px) {{
      .legend {{ display: grid; grid-template-columns: 1fr; }}
      .toggle {{ width: 100%; }}
      .grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>S1 / S2 / S3 / S4 Interactive Backtest Dashboard</h1>
    <p class="sub">Equity-Kurven fuer S3 FinBERT, S4 LLM, Hybride, S1/S2-Vergleiche und Benchmark. Quelle(n): <code>{sources}</code></p>
  </header>
  <main>
    <section class="toolbar">
      <div class="control">
        <label class="caption" for="timeLogic">Zeitlogik</label>
        <select id="timeLogic">
          <option value="research_signal_date">research_signal_date</option>
          <option value="conservative_price_lag1">conservative_price_lag1</option>
        </select>
      </div>
      <div class="control">
        <label class="caption" for="coverageFilter">Coverage Filter</label>
        <select id="coverageFilter">
          <option value="all_days">all_days</option>
          <option value="coverage_80">coverage_80</option>
        </select>
      </div>
      <div class="control">
        <label class="caption">Vergleichsdaten</label>
        <div class="segmented" role="group" aria-label="Vergleichsdaten">
          <button class="active" data-comparison-mode="full" type="button">Alle</button>
          <button data-comparison-mode="common" type="button">Gemeinsam</button>
        </div>
      </div>
      <div class="control">
        <label class="caption">Ansicht</label>
        <button id="resetBtn" type="button">Alle Linien anzeigen</button>
      </div>
    </section>

    <section class="legend" id="legend"></section>

    <section class="grid" id="cards"></section>

    <section class="chart-wrap">
      <svg id="chart" role="img" aria-label="Interactive equity chart"></svg>
      <div class="tooltip" id="tooltip"></div>
    </section>

    <section class="table-wrap">
      <h2 style="margin:0 0 10px;font-size:17px;">Kennzahlen fuer aktuelle Auswahl</h2>
      <table id="metricsTable"></table>
    </section>

    <section class="notes">
      <h2 style="margin:0 0 10px;font-size:17px;">Universen</h2>
      <div class="notes-grid" id="universeNotes"></div>
    </section>
  </main>

  <script id="payload" type="application/json">{payload}</script>
  <script>
    const DATA = JSON.parse(document.getElementById("payload").textContent);
    const state = {{
      timeLogic: "research_signal_date",
      coverageFilter: "all_days",
      comparisonMode: "full",
      visible: Object.fromEntries(Object.keys(DATA.series_meta).map(id => [id, true]))
    }};
    const svg = document.getElementById("chart");
    const tooltip = document.getElementById("tooltip");
    const legend = document.getElementById("legend");
    const cards = document.getElementById("cards");
    const metricsTable = document.getElementById("metricsTable");
    const comparisonButtons = document.querySelectorAll("[data-comparison-mode]");

    function panelKey() {{
      return `${{state.timeLogic}}|${{state.coverageFilter}}`;
    }}

    function fmtPct(x) {{
      if (x === null || x === undefined || !Number.isFinite(x)) return "n/a";
      return `${{(x * 100).toFixed(1)}}%`;
    }}

    function fmtNum(x, digits = 3) {{
      if (x === null || x === undefined || !Number.isFinite(x)) return "n/a";
      return Number(x).toFixed(digits);
    }}

    function mean(values) {{
      if (!values.length) return null;
      return values.reduce((sum, value) => sum + value, 0) / values.length;
    }}

    function stdDev(values) {{
      if (values.length < 2) return null;
      const avg = mean(values);
      const variance = values.reduce((sum, value) => sum + Math.pow(value - avg, 2), 0) / (values.length - 1);
      return Math.sqrt(variance);
    }}

    function returnsFromEquity(points) {{
      const returns = [];
      let previousEquity = 1;
      points.forEach(point => {{
        if (Number.isFinite(point.equity) && Number.isFinite(previousEquity) && previousEquity > 0) {{
          returns.push(point.equity / previousEquity - 1);
        }}
        previousEquity = point.equity;
      }});
      return returns;
    }}

    function momentum(points, tradingDays) {{
      if (!points || points.length <= tradingDays) return null;
      const last = points[points.length - 1].equity;
      const base = points[points.length - 1 - tradingDays].equity;
      if (!Number.isFinite(last) || !Number.isFinite(base) || base <= 0) return null;
      return last / base - 1;
    }}

    function seriesForCurrentPanel() {{
      const panel = DATA.panels[panelKey()];
      return panel ? panel.series : {{}};
    }}

    function dateSet(points) {{
      return new Set(points.map(point => point.date));
    }}

    function intersectDates(entries) {{
      if (!entries.length) return new Set();
      let common = dateSet(entries[0][1]);
      entries.slice(1).forEach(([, points]) => {{
        const dates = dateSet(points);
        common = new Set([...common].filter(date => dates.has(date)));
      }});
      return common;
    }}

    function rebasePoints(points) {{
      if (!points.length) return points;
      const base = points[0].equity;
      if (!Number.isFinite(base) || base <= 0) return points;
      return points.map(point => ({{ ...point, equity: point.equity / base, ret: point.equity / base - 1 }}));
    }}

    function entriesForDisplay(series) {{
      const entries = Object.entries(series).filter(([id, pts]) => state.visible[id] && pts.length);
      if (state.comparisonMode !== "common" || entries.length < 2) return entries;
      const common = intersectDates(entries);
      return entries.map(([id, points]) => {{
        const filtered = points
          .filter(point => common.has(point.date))
          .sort((a, b) => new Date(a.date) - new Date(b.date));
        return [id, rebasePoints(filtered)];
      }}).filter(([, points]) => points.length);
    }}

    function syncComparisonButtons() {{
      comparisonButtons.forEach(button => {{
        button.classList.toggle("active", button.dataset.comparisonMode === state.comparisonMode);
      }});
    }}

    function makeLegend() {{
      legend.innerHTML = "";
      Object.entries(DATA.series_meta).forEach(([id, meta]) => {{
        const item = document.createElement("label");
        item.className = "toggle";
        item.title = meta.description;
        item.innerHTML = `
          <input type="checkbox" data-id="${{id}}" ${{state.visible[id] ? "checked" : ""}}>
          <span class="swatch" style="background:${{meta.color}}"></span>
          <span>${{meta.label}}</span>
        `;
        legend.appendChild(item);
      }});
      legend.querySelectorAll("input").forEach(input => {{
        input.addEventListener("change", event => {{
          state.visible[event.target.dataset.id] = event.target.checked;
          render();
        }});
      }});
    }}

    function metricForSeries(id, points) {{
      if (!points || !points.length) return null;
      let maxEq = 1;
      let maxDrawdown = 0;
      points.forEach(p => {{
        maxEq = Math.max(maxEq, p.equity);
        maxDrawdown = Math.min(maxDrawdown, p.equity / maxEq - 1);
      }});
      const dailyReturns = returnsFromEquity(points);
      const avgDailyReturn = mean(dailyReturns);
      const dailyVolatility = stdDev(dailyReturns);
      const annualizedReturn = points.length > 0 ? Math.pow(points[points.length - 1].equity, 252 / points.length) - 1 : null;
      const annualizedVolatility = dailyVolatility === null ? null : dailyVolatility * Math.sqrt(252);
      const sharpe = dailyVolatility && dailyVolatility > 0 ? avgDailyReturn / dailyVolatility * Math.sqrt(252) : null;
      const calmar = maxDrawdown < 0 ? annualizedReturn / Math.abs(maxDrawdown) : null;
      const hitRate = dailyReturns.length ? dailyReturns.filter(value => value > 0).length / dailyReturns.length : null;
      return {{
        id,
        label: DATA.series_meta[id].label,
        n: points.length,
        last: points[points.length - 1].equity,
        cum: points[points.length - 1].equity - 1,
        annualizedReturn,
        annualizedVolatility,
        sharpe,
        calmar,
        hitRate,
        momentum3m: momentum(points, 63),
        momentum12m: momentum(points, 252),
        maxDrawdown
      }};
    }}

    function renderCards(metrics) {{
      const visible = metrics.filter(Boolean);
      const best = visible.slice().sort((a, b) => b.last - a.last)[0];
      const bestAnnualized = visible.filter(m => Number.isFinite(m.annualizedReturn)).sort((a, b) => b.annualizedReturn - a.annualizedReturn)[0];
      const lowestVolatility = visible.filter(m => Number.isFinite(m.annualizedVolatility)).sort((a, b) => a.annualizedVolatility - b.annualizedVolatility)[0];
      const bestSharpe = visible.filter(m => Number.isFinite(m.sharpe)).sort((a, b) => b.sharpe - a.sharpe)[0];
      const bestMomentum = visible.filter(m => Number.isFinite(m.momentum3m)).sort((a, b) => b.momentum3m - a.momentum3m)[0];
      const worstDD = visible.slice().sort((a, b) => a.maxDrawdown - b.maxDrawdown)[0];
      const benchmark = visible.find(m => m.id === "BENCHMARK");
      const bestVsBenchmark = benchmark ? visible
        .filter(m => m.id !== "BENCHMARK")
        .map(m => ({{ ...m, diffToBenchmark: m.last - benchmark.last }}))
        .sort((a, b) => b.diffToBenchmark - a.diffToBenchmark)[0] : null;
      cards.innerHTML = [
        ["Beste End-Equity", best ? best.label : "n/a", best ? fmtNum(best.last) : "n/a"],
        ["Beste Rendite p.a.", bestAnnualized ? bestAnnualized.label : "n/a", bestAnnualized ? fmtPct(bestAnnualized.annualizedReturn) : "n/a"],
        ["Niedrigste Volatilitaet", lowestVolatility ? lowestVolatility.label : "n/a", lowestVolatility ? fmtPct(lowestVolatility.annualizedVolatility) : "n/a"],
        ["Bestes Sharpe", bestSharpe ? bestSharpe.label : "n/a", bestSharpe ? fmtNum(bestSharpe.sharpe, 2) : "n/a"],
        ["Bestes Momentum 3M", bestMomentum ? bestMomentum.label : "n/a", bestMomentum ? fmtPct(bestMomentum.momentum3m) : "n/a"],
        ["Groesster Drawdown", worstDD ? worstDD.label : "n/a", worstDD ? fmtPct(worstDD.maxDrawdown) : "n/a"],
        ["Beste Kurve vs Benchmark", bestVsBenchmark ? bestVsBenchmark.label : "n/a", bestVsBenchmark ? fmtNum(bestVsBenchmark.diffToBenchmark) : "n/a"],
        ["Benchmark End-Equity", benchmark ? benchmark.label : "n/a", benchmark ? fmtNum(benchmark.last) : "n/a"],
      ].map(([k, d, v]) => `<div class="card"><div class="k">${{k}}</div><div class="v">${{v}}</div><div class="d">${{d}}</div></div>`).join("");
    }}

    function renderMetricsTable(metrics) {{
      const rows = metrics.filter(Boolean).map(m => `
        <tr>
          <td>${{m.label}}</td>
          <td>${{m.n}}</td>
          <td>${{fmtNum(m.last)}}</td>
          <td>${{fmtPct(m.cum)}}</td>
          <td>${{fmtPct(m.annualizedReturn)}}</td>
          <td>${{fmtPct(m.annualizedVolatility)}}</td>
          <td>${{fmtNum(m.sharpe, 2)}}</td>
          <td>${{fmtNum(m.calmar, 2)}}</td>
          <td>${{fmtPct(m.hitRate)}}</td>
          <td>${{fmtPct(m.momentum3m)}}</td>
          <td>${{fmtPct(m.momentum12m)}}</td>
          <td>${{fmtPct(m.maxDrawdown)}}</td>
        </tr>
      `).join("");
      metricsTable.innerHTML = `
        <thead><tr><th>Kurve</th><th>Tage</th><th>End-Equity</th><th>Kumuliert</th><th>Rendite p.a.</th><th>Volatilitaet p.a.</th><th>Sharpe</th><th>Calmar</th><th>Trefferquote</th><th>Momentum 3M</th><th>Momentum 12M</th><th>Max Drawdown</th></tr></thead>
        <tbody>${{rows}}</tbody>
      `;
    }}

    function renderNotes() {{
      document.getElementById("universeNotes").innerHTML = DATA.universe_notes.map(note => `
        <div class="note-item"><strong>${{note.name}}</strong><span>${{note.text}}</span></div>
      `).join("");
    }}

    function render() {{
      const panel = DATA.panels[panelKey()];
      const series = seriesForCurrentPanel();
      const active = entriesForDisplay(series);
      const metrics = active.map(([id, pts]) => metricForSeries(id, pts));
      renderCards(metrics);
      renderMetricsTable(metrics);

      const width = svg.clientWidth || 1200;
      const height = svg.clientHeight || 560;
      const margin = {{ left: 62, right: 24, top: 34, bottom: 54 }};
      svg.setAttribute("viewBox", `0 0 ${{width}} ${{height}}`);
      svg.innerHTML = "";

      const allPoints = active.flatMap(([, pts]) => pts);
      if (!allPoints.length) {{
        svg.innerHTML = `<text x="${{width/2}}" y="${{height/2}}" text-anchor="middle" fill="#667085">Keine sichtbaren Linien.</text>`;
        return;
      }}
      const dates = allPoints.map(p => new Date(p.date).getTime());
      const values = allPoints.map(p => p.equity);
      const minX = Math.min(...dates);
      const maxX = Math.max(...dates);
      let minY = Math.min(...values);
      let maxY = Math.max(...values);
      const padY = Math.max(0.02, (maxY - minY) * 0.08);
      minY -= padY;
      maxY += padY;
      const x = t => margin.left + ((t - minX) / Math.max(1, maxX - minX)) * (width - margin.left - margin.right);
      const y = v => height - margin.bottom - ((v - minY) / Math.max(0.0001, maxY - minY)) * (height - margin.top - margin.bottom);

      const ns = "http://www.w3.org/2000/svg";
      function add(name, attrs, parent = svg) {{
        const el = document.createElementNS(ns, name);
        Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, v));
        parent.appendChild(el);
        return el;
      }}

      for (let i = 0; i <= 5; i++) {{
        const yy = margin.top + i * (height - margin.top - margin.bottom) / 5;
        const val = maxY - i * (maxY - minY) / 5;
        add("line", {{ x1: margin.left, x2: width - margin.right, y1: yy, y2: yy, stroke: "#e6eaf0", "stroke-width": 1 }});
        add("text", {{ x: margin.left - 10, y: yy + 4, "text-anchor": "end", fill: "#667085", "font-size": 12 }}).textContent = val.toFixed(2);
      }}
      const tickDates = [];
      const start = new Date(minX);
      start.setDate(1);
      for (let d = new Date(start); d.getTime() <= maxX; d.setMonth(d.getMonth() + 2)) tickDates.push(new Date(d));
      tickDates.forEach(d => {{
        const xx = x(d.getTime());
        add("line", {{ x1: xx, x2: xx, y1: margin.top, y2: height - margin.bottom, stroke: "#eef1f5", "stroke-width": 1 }});
        const text = add("text", {{ x: xx, y: height - margin.bottom + 28, "text-anchor": "middle", fill: "#667085", "font-size": 12 }});
        text.textContent = d.toISOString().slice(0, 7);
      }});
      add("line", {{ x1: margin.left, x2: width - margin.right, y1: height - margin.bottom, y2: height - margin.bottom, stroke: "#98a2b3" }});
      add("line", {{ x1: margin.left, x2: margin.left, y1: margin.top, y2: height - margin.bottom, stroke: "#98a2b3" }});

      active.forEach(([id, pts]) => {{
        const meta = DATA.series_meta[id];
        const d = pts.map((p, i) => `${{i === 0 ? "M" : "L"}} ${{x(new Date(p.date).getTime()).toFixed(2)}} ${{y(p.equity).toFixed(2)}}`).join(" ");
        add("path", {{
          d,
          fill: "none",
          stroke: meta.color,
          "stroke-width": id === "BENCHMARK" ? 2.5 : 2.4,
          "stroke-dasharray": meta.dash || "",
          "stroke-linejoin": "round",
          "stroke-linecap": "round"
        }});
      }});

      add("text", {{ x: margin.left, y: 20, fill: "#1f2933", "font-size": 15, "font-weight": 700 }}).textContent = panel.title;
      const hoverLine = add("line", {{ x1: 0, x2: 0, y1: margin.top, y2: height - margin.bottom, stroke: "#475467", "stroke-width": 1, opacity: 0, "stroke-dasharray": "3 3" }});
      const overlay = add("rect", {{ x: margin.left, y: margin.top, width: width - margin.left - margin.right, height: height - margin.top - margin.bottom, fill: "transparent" }});
      overlay.addEventListener("mousemove", event => {{
        const rect = svg.getBoundingClientRect();
        const mx = event.clientX - rect.left;
        const t = minX + ((mx - margin.left) / Math.max(1, width - margin.left - margin.right)) * (maxX - minX);
        const nearest = nearestDate(active, t);
        if (!nearest) return;
        hoverLine.setAttribute("x1", x(new Date(nearest.date).getTime()));
        hoverLine.setAttribute("x2", x(new Date(nearest.date).getTime()));
        hoverLine.setAttribute("opacity", "1");
        tooltip.style.display = "block";
        tooltip.style.left = `${{Math.min(rect.width - 260, Math.max(12, mx + 18))}}px`;
        tooltip.style.top = `${{Math.max(12, event.clientY - rect.top - 34)}}px`;
        tooltip.innerHTML = `<div class="date">${{nearest.date}}</div>` + nearest.rows.map(row => `
          <div class="tooltip-row"><span style="color:${{row.color}}">${{row.label}}</span><strong>${{fmtNum(row.equity)}}</strong></div>
        `).join("");
      }});
      overlay.addEventListener("mouseleave", () => {{
        hoverLine.setAttribute("opacity", "0");
        tooltip.style.display = "none";
      }});
    }}

    function nearestDate(active, targetTime) {{
      const candidates = new Map();
      active.forEach(([id, pts]) => {{
        let best = null;
        let bestDist = Infinity;
        pts.forEach(p => {{
          const dist = Math.abs(new Date(p.date).getTime() - targetTime);
          if (dist < bestDist) {{ best = p; bestDist = dist; }}
        }});
        if (!best) return;
        const key = best.date;
        if (!candidates.has(key)) candidates.set(key, []);
        candidates.get(key).push({{
          id,
          label: DATA.series_meta[id].label,
          color: DATA.series_meta[id].color,
          equity: best.equity
        }});
      }});
      let chosen = null;
      let chosenDist = Infinity;
      candidates.forEach((rows, date) => {{
        const dist = Math.abs(new Date(date).getTime() - targetTime);
        if (dist < chosenDist) {{ chosen = {{ date, rows }}; chosenDist = dist; }}
      }});
      return chosen;
    }}

    document.getElementById("timeLogic").addEventListener("change", event => {{
      state.timeLogic = event.target.value;
      render();
    }});
    document.getElementById("coverageFilter").addEventListener("change", event => {{
      state.coverageFilter = event.target.value;
      render();
    }});
    comparisonButtons.forEach(button => {{
      button.addEventListener("click", event => {{
        state.comparisonMode = event.currentTarget.dataset.comparisonMode;
        syncComparisonButtons();
        render();
      }});
    }});
    document.getElementById("resetBtn").addEventListener("click", () => {{
      Object.keys(state.visible).forEach(k => state.visible[k] = true);
      makeLegend();
      render();
    }});
    window.addEventListener("resize", render);
    syncComparisonButtons();
    makeLegend();
    renderNotes();
    render();
  </script>
</body>
</html>
"""


def main() -> None:
    data = load_data()
    OUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    OUT_HTML.write_text(build_html(data), encoding="utf-8")
    print(f"Wrote {OUT_HTML}")


if __name__ == "__main__":
    main()
