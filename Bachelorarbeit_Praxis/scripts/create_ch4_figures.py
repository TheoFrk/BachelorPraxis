#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Create Chapter 4 publication figures from existing backtest outputs.

The script is intentionally read-only with respect to all SQLite inputs. It
only writes figures and audit/data extracts.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.ticker import FuncFormatter


SCRIPT_VERSION = "2026-05-30-ch4-figures-v1"
DEFAULT_DATA_ROOT = Path(__file__).resolve().parents[1]

DB_FILES = {
    "s1": Path("outputs/results/s1_revised_results.sqlite"),
    "s2": Path("outputs/results/s2_lexicon_results.sqlite"),
    "s3": Path("outputs/results/s3_finbert_results.sqlite"),
    "s4": Path("outputs/results/s4_llm_results.sqlite"),
}

COLORS = {
    "s1": "#2b2b2b",
    "benchmark": "#c43b3b",
    "s2": "#1f77b4",
    "s3": "#b36b00",
    "s4": "#7b3294",
    "hybrid_lex": "#178f65",
    "hybrid_finbert": "#009688",
    "hybrid_llm": "#0b7f7a",
    "hybrid_llm_alt": "#116c8d",
    "missing": "#d0d5dd",
}


@dataclass
class CoverageRow:
    key: str
    label: str
    color: str
    target_securities: float | None = None
    target_secondary: float | None = None
    target_note: str = ""
    target_source: str = ""
    news_articles: float | None = None
    news_secondary: float | None = None
    news_note: str = ""
    news_source: str = ""
    share_positions_with_news: float | None = None
    share_source: str = ""
    warnings: list[str] = field(default_factory=list)


@dataclass
class EquitySeries:
    key: str
    label: str
    direct_label: str
    color: str
    linestyle: str | tuple[int, tuple[int, ...]]
    linewidth: float
    data: pd.DataFrame
    db_key: str
    source_table: str
    equity_col: str | None
    return_col: str | None
    filters: dict[str, Any]
    source_note: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class PanelResult:
    panel_key: str
    title: str
    series: dict[str, pd.DataFrame]
    warnings: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create Chapter 4 coverage and equity figures.")
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--latex-root", type=Path, default=None)
    parser.add_argument("--image-dir", type=Path, default=None)
    parser.add_argument("--figure-output-dir", type=Path, default=None)
    return parser.parse_args()


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def open_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({quote_ident(table)})")]


def read_metadata(conn: sqlite3.Connection, table: str) -> dict[str, Any]:
    if not table_exists(conn, table):
        return {}
    cols = table_columns(conn, table)
    if {"key", "value"}.issubset(cols):
        out: dict[str, Any] = {}
        for key, value in conn.execute(f"SELECT key, value FROM {quote_ident(table)}"):
            if key in out and value not in (None, ""):
                out[key] = f"{out[key]}; {value}"
            else:
                out[key] = value
        return out
    row = conn.execute(f"SELECT * FROM {quote_ident(table)} LIMIT 1").fetchone()
    if row is None:
        return {}
    return dict(zip(cols, row))


def as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    text = str(value).strip()
    if not text or text.lower() in {"none", "null", "nan"}:
        return None
    text = text.replace("%", "").replace(" ", "")
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        parsed = float(text)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def first_float(*values: Any) -> float | None:
    for value in values:
        parsed = as_float(value)
        if parsed is not None:
            return parsed
    return None


def de_int(value: float | int | None) -> str:
    if value is None or not math.isfinite(float(value)):
        return "n/v"
    return f"{int(round(float(value))):,}".replace(",", ".")


def de_decimal(value: float | None, digits: int = 1) -> str:
    if value is None or not math.isfinite(float(value)):
        return "n/v"
    return f"{float(value):.{digits}f}".replace(".", ",")


def de_percent(value: float | None, digits: int = 1) -> str:
    if value is None or not math.isfinite(float(value)):
        return "n/v"
    return f"{value * 100:.{digits}f} %".replace(".", ",")


def resolve_latex_root(data_root: Path, explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit.expanduser().resolve()

    candidates: list[Path] = [
        data_root,
        data_root.parent / "Bachelorarbeit-6",
        data_root.parent / "Bachelorarbeit-3",
    ]
    candidates.extend(sorted(data_root.parent.glob("Bachelorarbeit-*")))
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if (resolved / "sections" / "4_empirical_results.tex").exists():
            return resolved
    return None


def find_index_html(data_root: Path, latex_root: Path | None) -> Path | None:
    candidates = [
        data_root / "index.html",
        data_root / "visualitzation" / "index.html",
    ]
    if latex_root is not None:
        candidates.append(latex_root / "index.html")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    found = sorted(data_root.rglob("index.html"))
    return found[0] if found else None


def load_dashboard_payload(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    match = re.search(
        r'<script[^>]+id=["\']payload["\'][^>]*>(.*?)</script>',
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if not match:
        return None
    try:
        return json.loads(html.unescape(match.group(1)))
    except json.JSONDecodeError:
        return None


def scalar_query(
    conn: sqlite3.Connection,
    query: str,
    params: tuple[Any, ...] = (),
) -> Any:
    row = conn.execute(query, params).fetchone()
    if row is None:
        return None
    return row[0]


def summary_value(
    conn: sqlite3.Connection,
    table: str,
    value_col: str,
    filters: dict[str, Any],
) -> float | None:
    if not table_exists(conn, table):
        return None
    cols = table_columns(conn, table)
    if value_col not in cols:
        return None
    clauses = [f"{quote_ident(key)} = ?" for key in filters if key in cols]
    params = tuple(filters[key] for key in filters if key in cols)
    where = " AND ".join(clauses)
    query = f"SELECT {quote_ident(value_col)} FROM {quote_ident(table)}"
    if where:
        query += f" WHERE {where}"
    query += " LIMIT 1"
    return as_float(scalar_query(conn, query, params))


def average_coverage_value(
    conn: sqlite3.Connection,
    table: str,
    value_col: str,
    filters: dict[str, Any],
) -> float | None:
    if not table_exists(conn, table):
        return None
    cols = table_columns(conn, table)
    if value_col not in cols:
        return None
    clauses = [f"{quote_ident(key)} = ?" for key in filters if key in cols]
    params = tuple(filters[key] for key in filters if key in cols)
    where = " AND ".join(clauses)
    query = f"SELECT AVG({quote_ident(value_col)}) FROM {quote_ident(table)}"
    if where:
        query += f" WHERE {where}"
    return as_float(scalar_query(conn, query, params))


def collect_coverage(
    data_root: Path,
    db_paths: dict[str, Path],
    payload: dict[str, Any] | None,
) -> tuple[list[CoverageRow], list[str]]:
    warnings: list[str] = []
    rows = [
        CoverageRow("s1", "S1", COLORS["s1"]),
        CoverageRow("s2", "S2", COLORS["s2"]),
        CoverageRow("s3", "S3", COLORS["s3"]),
        CoverageRow("s4_en", "S4 EN", COLORS["s4"]),
        CoverageRow("s4_multi", "S4 MULTI", "#9b59b6"),
    ]
    by_key = {row.key: row for row in rows}

    s1 = db_paths["s1"]
    if s1.exists():
        with open_ro(s1) as conn:
            full = summary_value(
                conn,
                "s1_universe_summary",
                "n_target_securities",
                {"universe": "S1_FULL_568"},
            )
            common = summary_value(
                conn,
                "s1_universe_summary",
                "n_target_securities",
                {"universe": "S1_COMMON_NEWS_ANY"},
            )
            if full is None:
                full = average_coverage_value(
                    conn,
                    "s1_coverage_by_date",
                    "n_target_securities",
                    {
                        "universe": "S1_FULL_568",
                        "time_logic": "research_close_to_close",
                        "portfolio_variant": "standard",
                        "coverage_filter": "all_days",
                    },
                )
            if common is None:
                common = average_coverage_value(
                    conn,
                    "s1_coverage_by_date",
                    "n_target_securities",
                    {
                        "universe": "S1_COMMON_NEWS_ANY",
                        "time_logic": "research_close_to_close",
                        "portfolio_variant": "standard",
                        "coverage_filter": "all_days",
                    },
                )
            by_key["s1"].target_securities = full
            by_key["s1"].target_secondary = common
            by_key["s1"].target_note = "Preisuniversum; Marker zeigt Common-News-Universum"
            by_key["s1"].target_source = "s1_universe_summary / s1_coverage_by_date"
            by_key["s1"].news_note = "kein Textsignal in S1"
            by_key["s1"].news_source = "methodisch nicht anwendbar"
            by_key["s1"].share_source = "nicht verfügbar"
    else:
        warnings.append(f"WARN missing S1 database: {s1}")

    s2 = db_paths["s2"]
    if s2.exists():
        with open_ro(s2) as conn:
            meta = read_metadata(conn, "s2_run_metadata")
            article_stats = parse_json_object(meta.get("article_stats"))
            article_count = first_float(
                article_stats.get("news_rows_after_dedup") if article_stats else None,
                meta.get("n_articles_scored"),
            )
            if article_count is None and table_exists(conn, "s2_lexicon_article_scores"):
                article_count = as_float(scalar_query(conn, "SELECT COUNT(*) FROM s2_lexicon_article_scores"))
            filters = {
                "strategy_variant": "S2_LEX_ONLY_NEUTRAL_FILL",
                "news_mode": "NEUTRAL_FILL",
                "time_logic": "research_signal_date",
                "portfolio_variant": "standard",
                "coverage_filter": "all_days",
            }
            by_key["s2"].target_securities = first_float(
                meta.get("n_target_securities"),
                summary_value(conn, "s2_model_comparison_summary", "n_target_securities", filters),
                average_coverage_value(conn, "s2_coverage_by_date", "n_target_securities", filters),
            )
            by_key["s2"].news_articles = article_count
            by_key["s2"].share_positions_with_news = first_float(
                summary_value(conn, "s2_model_comparison_summary", "avg_share_positions_with_news", filters),
                average_coverage_value(conn, "s2_coverage_by_date", "share_positions_with_news", filters),
            )
            by_key["s2"].target_source = "s2_run_metadata / s2_model_comparison_summary"
            by_key["s2"].news_source = "s2_lexicon_article_scores / s2_run_metadata"
            by_key["s2"].share_source = "s2_model_comparison_summary"
    else:
        warnings.append(f"WARN missing S2 database: {s2}")

    s3 = db_paths["s3"]
    if s3.exists():
        with open_ro(s3) as conn:
            meta = read_metadata(conn, "s3_run_metadata")
            filters = {
                "strategy_variant": "S3_FINBERT_ONLY_NEUTRAL_FILL",
                "news_mode": "NEUTRAL_FILL",
                "time_logic": "research_signal_date",
                "portfolio_variant": "standard",
                "coverage_filter": "all_days",
            }
            by_key["s3"].target_securities = first_float(
                meta.get("n_target_securities"),
                summary_value(conn, "s3_model_comparison_summary", "n_target_securities", filters),
                average_coverage_value(conn, "s3_coverage_by_date", "n_target_securities", filters),
            )
            by_key["s3"].news_articles = first_float(meta.get("n_articles_after_language_filter"))
            by_key["s3"].news_secondary = first_float(meta.get("n_articles_total"))
            by_key["s3"].news_note = "Balken: nach English-Filter; Marker: Gesamt-News"
            by_key["s3"].share_positions_with_news = first_float(
                summary_value(conn, "s3_model_comparison_summary", "avg_share_positions_with_news", filters),
                average_coverage_value(conn, "s3_coverage_by_date", "share_positions_with_news", filters),
            )
            by_key["s3"].target_source = "s3_run_metadata / s3_model_comparison_summary"
            by_key["s3"].news_source = "s3_run_metadata"
            by_key["s3"].share_source = "s3_model_comparison_summary"
    else:
        warnings.append(f"WARN missing S3 database: {s3}")

    s4 = db_paths["s4"]
    if s4.exists():
        with open_ro(s4) as conn:
            meta = read_metadata(conn, "s4_run_metadata")
            for key, language, variant in [
                ("s4_en", "EN", "S4_LLM_ONLY_EN_NEUTRAL_FILL"),
                ("s4_multi", "MULTI", "S4_LLM_ONLY_MULTI_NEUTRAL_FILL"),
            ]:
                filters = {
                    "strategy_variant": variant,
                    "language_mode": language,
                    "news_mode": "NEUTRAL_FILL",
                    "time_logic": "research_signal_date",
                    "portfolio_variant": "standard",
                    "coverage_filter": "all_days",
                }
                by_key[key].target_securities = first_float(
                    summary_value(conn, "s4_model_comparison_summary", "n_target_securities", filters),
                    average_coverage_value(conn, "s4_coverage_by_date", "n_target_securities", filters),
                )
                by_key[key].share_positions_with_news = first_float(
                    summary_value(conn, "s4_model_comparison_summary", "avg_share_positions_with_news", filters),
                    average_coverage_value(conn, "s4_coverage_by_date", "share_positions_with_news", filters),
                )
                by_key[key].target_source = "s4_model_comparison_summary"
                by_key[key].share_source = "s4_model_comparison_summary"
            by_key["s4_en"].news_articles = first_float(meta.get("n_articles_s4_en"))
            by_key["s4_multi"].news_articles = first_float(meta.get("n_articles_s4_multi"))
            by_key["s4_en"].news_source = "s4_run_metadata"
            by_key["s4_multi"].news_source = "s4_run_metadata"
            api_usage = first_float(meta.get("n_articles_scored"))
            api_fail = first_float(meta.get("n_api_fail"))
            if api_usage is not None:
                by_key["s4_en"].news_note = f"LLM-Scoring OK/Cache: {de_int(api_usage)}"
                by_key["s4_multi"].news_note = f"API-Fails im Cache: {de_int(api_fail)}" if api_fail is not None else ""
    else:
        warnings.append(f"WARN missing S4 database: {s4}")

    apply_coverage_fallbacks(rows, payload, warnings)
    return rows, warnings


def parse_json_object(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def apply_coverage_fallbacks(
    rows: list[CoverageRow],
    payload: dict[str, Any] | None,
    warnings: list[str],
) -> None:
    if payload is None:
        return
    meta = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), list) else []
    by_key = {row.key: row for row in rows}

    if by_key["s3"].news_articles is None:
        by_key["s3"].news_articles = as_float(meta.get("n_articles_after_language_filter"))
        by_key["s3"].news_source = "index.html payload fallback"
        warnings.append("WARN S3 article count used index.html fallback.")
    if by_key["s3"].news_secondary is None:
        by_key["s3"].news_secondary = as_float(meta.get("n_articles_total"))

    for strategy, key in [
        ("S3_FINBERT_ONLY_NEUTRAL_FILL", "s3"),
        ("S2_LEX_ONLY_NEUTRAL_FILL", "s2"),
        ("S4_LLM_ONLY_EN_NEUTRAL_FILL", "s4_en"),
        ("S4_LLM_ONLY_MULTI_NEUTRAL_FILL", "s4_multi"),
    ]:
        row = by_key[key]
        if row.target_securities is not None and row.share_positions_with_news is not None:
            continue
        metric = next((m for m in metrics if m.get("strategy_variant") == strategy and m.get("coverage_filter") == "all_days"), None)
        if not metric:
            continue
        if row.target_securities is None:
            row.target_securities = as_float(metric.get("n_target_securities"))
            row.target_source = "index.html payload fallback"
            warnings.append(f"WARN {row.label} target securities used index.html fallback.")
        if row.share_positions_with_news is None:
            row.share_positions_with_news = as_float(metric.get("avg_share_positions_with_news"))
            row.share_source = "index.html payload fallback"
            warnings.append(f"WARN {row.label} share_positions_with_news used index.html fallback.")


def load_equity_series_from_db(
    key: str,
    label: str,
    direct_label: str,
    color: str,
    linestyle: str | tuple[int, tuple[int, ...]],
    linewidth: float,
    db_key: str,
    db_path: Path,
    table: str,
    filters: dict[str, Any],
    equity_candidates: list[str],
    return_candidates: list[str],
    warnings: list[str],
) -> EquitySeries | None:
    if not db_path.exists():
        warnings.append(f"WARN missing DB for {label}: {db_path}")
        return None
    with open_ro(db_path) as conn:
        if not table_exists(conn, table):
            warnings.append(f"WARN missing table for {label}: {db_path.name}:{table}")
            return None
        cols = table_columns(conn, table)
        if "date" not in cols:
            warnings.append(f"WARN missing date column for {label}: {table}")
            return None
        equity_col = next((col for col in equity_candidates if col in cols), None)
        return_col = next((col for col in return_candidates if col in cols), None)
        if equity_col is None and return_col is None:
            warnings.append(f"WARN no equity/return column for {label}: {table}")
            return None
        missing_filters = [col for col in filters if col not in cols]
        if missing_filters:
            warnings.append(f"WARN ignored unavailable filters for {label}: {missing_filters}")
        clauses = [f"{quote_ident(col)} = ?" for col in filters if col in cols]
        params = tuple(filters[col] for col in filters if col in cols)
        selected_cols = ["date"]
        if equity_col is not None:
            selected_cols.append(equity_col)
        if return_col is not None and return_col not in selected_cols:
            selected_cols.append(return_col)
        query = "SELECT " + ", ".join(quote_ident(col) for col in selected_cols)
        query += f" FROM {quote_ident(table)}"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY date"
        frame = pd.read_sql_query(query, conn, params=params)

    series_warnings: list[str] = []
    data, used_equity_col, used_return_col = prepare_equity_data(
        frame,
        label,
        equity_col,
        return_col,
        series_warnings,
    )
    if data.empty:
        warnings.extend(series_warnings)
        warnings.append(f"WARN no usable observations for {label}")
        return None
    return EquitySeries(
        key=key,
        label=label,
        direct_label=direct_label,
        color=color,
        linestyle=linestyle,
        linewidth=linewidth,
        data=data,
        db_key=db_key,
        source_table=table,
        equity_col=used_equity_col,
        return_col=used_return_col,
        filters=filters,
        source_note=f"{db_path.name}:{table}",
        warnings=series_warnings,
    )


def prepare_equity_data(
    frame: pd.DataFrame,
    label: str,
    equity_col: str | None,
    return_col: str | None,
    warnings: list[str],
) -> tuple[pd.DataFrame, str | None, str | None]:
    if frame.empty:
        return pd.DataFrame(columns=["date", "equity"]), equity_col, return_col
    frame = frame.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date"]).sort_values("date")
    if frame["date"].duplicated().any():
        n_dup = int(frame["date"].duplicated().sum())
        warnings.append(f"WARN {label}: {n_dup} duplicate dates; keeping last value per date.")
        frame = frame.drop_duplicates(subset=["date"], keep="last")

    used_equity_col: str | None = None
    used_return_col: str | None = None
    if equity_col is not None:
        equity = pd.to_numeric(frame[equity_col], errors="coerce")
        if equity.notna().sum() > 0:
            used_equity_col = equity_col
        else:
            equity = pd.Series(index=frame.index, dtype=float)
    else:
        equity = pd.Series(index=frame.index, dtype=float)

    if used_equity_col is None:
        if return_col is None:
            return pd.DataFrame(columns=["date", "equity"]), None, None
        returns = pd.to_numeric(frame[return_col], errors="coerce")
        valid = returns.notna()
        if valid.sum() == 0:
            return pd.DataFrame(columns=["date", "equity"]), None, return_col
        equity = (1.0 + returns.where(valid)).cumprod()
        used_return_col = return_col
    else:
        used_return_col = return_col if return_col is not None else None

    data = pd.DataFrame({"date": frame["date"], "equity": equity})
    if return_col is not None and return_col in frame:
        data["return"] = pd.to_numeric(frame[return_col], errors="coerce")
    data = data.dropna(subset=["equity"])
    data = data[np.isfinite(data["equity"])]
    if data["equity"].isna().any():
        warnings.append(f"WARN {label}: NaN equity values dropped.")
    return data, used_equity_col, used_return_col


def load_equity_series_from_payload(
    payload: dict[str, Any] | None,
    wanted_key: str,
    label: str,
    direct_label: str,
    color: str,
    linestyle: str | tuple[int, tuple[int, ...]],
    linewidth: float,
) -> EquitySeries | None:
    if payload is None:
        return None
    panels = payload.get("panels")
    if not isinstance(panels, dict):
        return None
    panel = panels.get("research_signal_date|all_days")
    if not isinstance(panel, dict):
        return None
    series = panel.get("series")
    if not isinstance(series, dict):
        return None
    points = series.get(wanted_key)
    if not points:
        return None
    frame = pd.DataFrame(points)
    if "date" not in frame or "equity" not in frame:
        return None
    warnings: list[str] = [f"WARN {label}: loaded from index.html payload fallback."]
    data, _, _ = prepare_equity_data(frame[["date", "equity"]].copy(), label, "equity", None, warnings)
    if data.empty:
        return None
    return EquitySeries(
        key=wanted_key.lower(),
        label=label,
        direct_label=direct_label,
        color=color,
        linestyle=linestyle,
        linewidth=linewidth,
        data=data,
        db_key="index",
        source_table="index.html payload",
        equity_col="equity",
        return_col=None,
        filters={"panel": "research_signal_date|all_days", "series": wanted_key},
        source_note="index.html fallback",
        warnings=warnings,
    )


def build_equity_series(
    db_paths: dict[str, Path],
    payload: dict[str, Any] | None,
) -> tuple[dict[str, EquitySeries], list[str]]:
    warnings: list[str] = []
    specs = [
        {
            "key": "s1",
            "payload_key": "S1_PRICE_ONLY",
            "label": "S1 Price-only",
            "direct": "S1",
            "color": COLORS["s1"],
            "linestyle": "-",
            "linewidth": 2.2,
            "db_key": "s1",
            "table": "s1_daily_returns",
            "filters": {
                "universe": "S1_COMMON_NEWS_ANY",
                "time_logic": "research_close_to_close",
                "portfolio_variant": "standard",
                "coverage_filter": "all_days",
            },
            "equity_candidates": ["equity", "strategy_equity_matched"],
            "return_candidates": ["strategy_ret_fwd_1d", "strategy_return"],
        },
        {
            "key": "s2",
            "payload_key": "S2_LEXICON_ONLY",
            "label": "S2 Lexikon-only",
            "direct": "S2",
            "color": COLORS["s2"],
            "linestyle": "-",
            "linewidth": 1.8,
            "db_key": "s2",
            "table": "s2_daily_returns",
            "filters": {
                "strategy_variant": "S2_LEX_ONLY_NEUTRAL_FILL",
                "news_mode": "NEUTRAL_FILL",
                "time_logic": "research_signal_date",
                "portfolio_variant": "standard",
                "coverage_filter": "all_days",
            },
            "equity_candidates": ["equity", "strategy_equity_matched"],
            "return_candidates": ["strategy_ret_fwd_1d", "strategy_return"],
        },
        {
            "key": "s1_s2_50",
            "payload_key": "HYBRID_S1_LEX_50_50",
            "label": "S1 + S2 Lexikon 50/50",
            "direct": "S1+S2 50/50",
            "color": COLORS["hybrid_lex"],
            "linestyle": "-",
            "linewidth": 1.9,
            "db_key": "s2",
            "table": "s2_daily_returns",
            "filters": {
                "strategy_variant": "HYBRID_S1_LEX_50_50_NEUTRAL_FILL",
                "news_mode": "NEUTRAL_FILL",
                "time_logic": "research_signal_date",
                "portfolio_variant": "standard",
                "coverage_filter": "all_days",
            },
            "equity_candidates": ["equity", "strategy_equity_matched"],
            "return_candidates": ["strategy_ret_fwd_1d", "strategy_return"],
        },
        {
            "key": "s3",
            "payload_key": "S3_FINBERT_ONLY",
            "label": "S3 FinBERT-only",
            "direct": "S3",
            "color": COLORS["s3"],
            "linestyle": (0, (5, 2, 1, 2)),
            "linewidth": 1.8,
            "db_key": "s3",
            "table": "s3_daily_returns",
            "filters": {
                "strategy_variant": "S3_FINBERT_ONLY_NEUTRAL_FILL",
                "news_mode": "NEUTRAL_FILL",
                "time_logic": "research_signal_date",
                "portfolio_variant": "standard",
                "coverage_filter": "all_days",
            },
            "equity_candidates": ["equity", "strategy_equity_matched"],
            "return_candidates": ["strategy_ret_fwd_1d", "strategy_return"],
        },
        {
            "key": "s1_s3_50",
            "payload_key": "HYBRID_S1_FINBERT_50_50",
            "label": "S1 + S3 FinBERT 50/50",
            "direct": "S1+S3 50/50",
            "color": COLORS["hybrid_finbert"],
            "linestyle": (0, (5, 2)),
            "linewidth": 1.9,
            "db_key": "s3",
            "table": "s3_daily_returns",
            "filters": {
                "strategy_variant": "HYBRID_S1_FINBERT_50_50_NEUTRAL_FILL",
                "news_mode": "NEUTRAL_FILL",
                "time_logic": "research_signal_date",
                "portfolio_variant": "standard",
                "coverage_filter": "all_days",
            },
            "equity_candidates": ["equity", "strategy_equity_matched"],
            "return_candidates": ["strategy_ret_fwd_1d", "strategy_return"],
        },
        {
            "key": "s4",
            "payload_key": "S4_LLM_ONLY_EN_CURRENT",
            "label": "S4 LLM-only EN",
            "direct": "S4",
            "color": COLORS["s4"],
            "linestyle": (0, (1.5, 1.5)),
            "linewidth": 1.9,
            "db_key": "s4",
            "table": "s4_daily_returns",
            "filters": {
                "strategy_variant": "S4_LLM_ONLY_EN_NEUTRAL_FILL",
                "language_mode": "EN",
                "news_mode": "NEUTRAL_FILL",
                "time_logic": "research_signal_date",
                "portfolio_variant": "standard",
                "coverage_filter": "all_days",
            },
            "equity_candidates": ["equity", "strategy_equity_matched"],
            "return_candidates": ["strategy_ret_fwd_1d", "strategy_return"],
        },
        {
            "key": "s1_s4_50",
            "payload_key": "HYBRID_S1_LLM_50_50_EN_CURRENT",
            "label": "S1 + S4 LLM 50/50 EN",
            "direct": "S1+S4 50/50",
            "color": COLORS["hybrid_llm"],
            "linestyle": (0, (6, 2)),
            "linewidth": 2.0,
            "db_key": "s4",
            "table": "s4_daily_returns",
            "filters": {
                "strategy_variant": "HYBRID_S1_LLM_50_50_EN_NEUTRAL_FILL",
                "language_mode": "EN",
                "news_mode": "NEUTRAL_FILL",
                "time_logic": "research_signal_date",
                "portfolio_variant": "standard",
                "coverage_filter": "all_days",
            },
            "equity_candidates": ["equity", "strategy_equity_matched"],
            "return_candidates": ["strategy_ret_fwd_1d", "strategy_return"],
        },
        {
            "key": "s1_s4_70",
            "payload_key": "HYBRID_S1_LLM_70_30_EN_CURRENT",
            "label": "S1 + S4 LLM 70/30 EN",
            "direct": "S1+S4 70/30",
            "color": COLORS["hybrid_llm_alt"],
            "linestyle": (0, (2, 2)),
            "linewidth": 1.9,
            "db_key": "s4",
            "table": "s4_daily_returns",
            "filters": {
                "strategy_variant": "HYBRID_S1_LLM_70_30_EN_NEUTRAL_FILL",
                "language_mode": "EN",
                "news_mode": "NEUTRAL_FILL",
                "time_logic": "research_signal_date",
                "portfolio_variant": "standard",
                "coverage_filter": "all_days",
            },
            "equity_candidates": ["equity", "strategy_equity_matched"],
            "return_candidates": ["strategy_ret_fwd_1d", "strategy_return"],
        },
    ]

    series: dict[str, EquitySeries] = {}
    for spec in specs:
        loaded = load_equity_series_from_db(
            key=spec["key"],
            label=spec["label"],
            direct_label=spec["direct"],
            color=spec["color"],
            linestyle=spec["linestyle"],
            linewidth=spec["linewidth"],
            db_key=spec["db_key"],
            db_path=db_paths[spec["db_key"]],
            table=spec["table"],
            filters=spec["filters"],
            equity_candidates=spec["equity_candidates"],
            return_candidates=spec["return_candidates"],
            warnings=warnings,
        )
        if loaded is None:
            loaded = load_equity_series_from_payload(
                payload,
                spec["payload_key"],
                spec["label"],
                spec["direct"],
                spec["color"],
                spec["linestyle"],
                spec["linewidth"],
            )
            if loaded is not None:
                warnings.extend(loaded.warnings)
        if loaded is not None:
            loaded.key = spec["key"]
            series[spec["key"]] = loaded

    benchmark = load_benchmark_series(db_paths, payload, warnings)
    if benchmark is not None:
        series["benchmark"] = benchmark
    return series, warnings


def load_benchmark_series(
    db_paths: dict[str, Path],
    payload: dict[str, Any] | None,
    warnings: list[str],
) -> EquitySeries | None:
    candidates = [
        (
            "s4",
            "s4_vs_benchmark_daily",
            {
                "strategy_variant": "S4_LLM_ONLY_EN_NEUTRAL_FILL",
                "language_mode": "EN",
                "news_mode": "NEUTRAL_FILL",
                "time_logic": "research_signal_date",
                "portfolio_variant": "standard",
                "coverage_filter": "all_days",
            },
        ),
        (
            "s3",
            "s3_vs_benchmark_daily",
            {
                "strategy_variant": "S3_FINBERT_ONLY_NEUTRAL_FILL",
                "news_mode": "NEUTRAL_FILL",
                "time_logic": "research_signal_date",
                "portfolio_variant": "standard",
                "coverage_filter": "all_days",
            },
        ),
        (
            "s2",
            "s2_vs_benchmark_daily",
            {
                "strategy_variant": "S2_LEX_ONLY_NEUTRAL_FILL",
                "news_mode": "NEUTRAL_FILL",
                "time_logic": "research_signal_date",
                "portfolio_variant": "standard",
                "coverage_filter": "all_days",
            },
        ),
        (
            "s1",
            "s1_vs_benchmark_daily",
            {
                "universe": "S1_COMMON_NEWS_ANY",
                "time_logic": "research_close_to_close",
                "portfolio_variant": "standard",
                "coverage_filter": "all_days",
            },
        ),
    ]
    for db_key, table, filters in candidates:
        loaded = load_equity_series_from_db(
            key="benchmark",
            label="STOXX Europe 600 Benchmark",
            direct_label="Benchmark",
            color=COLORS["benchmark"],
            linestyle=(0, (3, 2)),
            linewidth=1.4,
            db_key=db_key,
            db_path=db_paths[db_key],
            table=table,
            filters=filters,
            equity_candidates=["benchmark_equity_matched"],
            return_candidates=["benchmark_ret_fwd_1d"],
            warnings=[],
        )
        if loaded is not None:
            return loaded

    fallback = load_equity_series_from_payload(
        payload,
        "BENCHMARK",
        "STOXX Europe 600 Benchmark",
        "Benchmark",
        COLORS["benchmark"],
        (0, (3, 2)),
        1.4,
    )
    if fallback is not None:
        warnings.extend(fallback.warnings)
        fallback.key = "benchmark"
        return fallback
    warnings.append("WARN benchmark series missing.")
    return None


def build_panel_results(
    all_series: dict[str, EquitySeries],
) -> tuple[list[PanelResult], list[dict[str, Any]], pd.DataFrame, list[str]]:
    panel_specs = [
        ("A", "Referenz", ["s1", "benchmark"]),
        ("B", "Text-only-Strategien", ["s2", "s3", "s4", "benchmark"]),
        ("C", "50/50-Hybride", ["s1", "s1_s2_50", "s1_s3_50", "s1_s4_50"]),
        ("D", "S4-Gewichtungsrobustheit", ["s1", "s1_s4_50", "s1_s4_70", "benchmark"]),
    ]
    panel_results: list[PanelResult] = []
    audit_rows: list[dict[str, Any]] = []
    point_frames: list[pd.DataFrame] = []
    warnings: list[str] = []

    for panel_key, title, keys in panel_specs:
        panel_warnings: list[str] = []
        available = [all_series[key] for key in keys if key in all_series]
        missing = [key for key in keys if key not in all_series]
        for key in missing:
            panel_warnings.append(f"WARN Panel {panel_key}: missing series {key}.")
        if len(available) > 4:
            panel_warnings.append(f"WARN Panel {panel_key}: more than 4 lines would be plotted.")
            available = available[:4]
        if not available:
            panel_warnings.append(f"WARN Panel {panel_key}: no available series.")
            panel_results.append(PanelResult(panel_key, title, {}, panel_warnings))
            warnings.extend(panel_warnings)
            continue

        date_sets = [set(series.data["date"]) for series in available]
        common_dates = sorted(set.intersection(*date_sets)) if date_sets else []
        if not common_dates:
            panel_warnings.append(f"WARN Panel {panel_key}: no matched dates across available series.")
            panel_results.append(PanelResult(panel_key, title, {}, panel_warnings))
            warnings.extend(panel_warnings)
            continue
        common_index = pd.DatetimeIndex(common_dates)
        plotted: dict[str, pd.DataFrame] = {}
        for series in available:
            frame = series.data[series.data["date"].isin(common_index)].copy().sort_values("date")
            frame = frame.dropna(subset=["equity"])
            frame = frame[np.isfinite(frame["equity"])]
            if frame.empty:
                panel_warnings.append(f"WARN Panel {panel_key}: {series.label} has no finite equity after matching.")
                continue
            base = frame["equity"].iloc[0]
            if not math.isfinite(float(base)) or abs(float(base)) < 1e-12:
                panel_warnings.append(f"WARN Panel {panel_key}: {series.label} has invalid base equity.")
                continue
            frame["equity_norm"] = frame["equity"] / base
            frame["panel"] = panel_key
            frame["series_key"] = series.key
            frame["series_label"] = series.label
            if frame["equity_norm"].isna().any():
                panel_warnings.append(f"WARN Panel {panel_key}: NaN after normalization for {series.label}.")
                frame = frame.dropna(subset=["equity_norm"])
            if not frame.empty and abs(float(frame["equity_norm"].iloc[0]) - 1.0) > 1e-10:
                panel_warnings.append(f"WARN Panel {panel_key}: {series.label} does not start at 1.0.")
            plotted[series.key] = frame
            point_frames.append(
                frame[["panel", "series_key", "series_label", "date", "equity_norm"]].copy()
            )
            audit_rows.append(
                {
                    "panel": panel_key,
                    "panel_title": title,
                    "series_key": series.key,
                    "series_label": series.label,
                    "source": series.source_note,
                    "source_table": series.source_table,
                    "equity_column": series.equity_col,
                    "return_column": series.return_col,
                    "filters": json.dumps(series.filters, ensure_ascii=False, sort_keys=True),
                    "n_days": int(len(frame)),
                    "first_date": frame["date"].min().date().isoformat(),
                    "last_date": frame["date"].max().date().isoformat(),
                    "start_equity": float(frame["equity_norm"].iloc[0]),
                    "end_equity": float(frame["equity_norm"].iloc[-1]),
                    "cumulative_return": float(frame["equity_norm"].iloc[-1] - 1.0),
                    "nan_equity_values": int(frame["equity_norm"].isna().sum()),
                }
            )
        panel_results.append(PanelResult(panel_key, title, plotted, panel_warnings))
        warnings.extend(panel_warnings)

    points = pd.concat(point_frames, ignore_index=True) if point_frames else pd.DataFrame()
    return panel_results, audit_rows, points, warnings


def write_coverage_csv(rows: list[CoverageRow], path: Path) -> None:
    frame = pd.DataFrame(
        [
            {
                "stage": row.key,
                "label": row.label,
                "target_securities": row.target_securities,
                "target_secondary": row.target_secondary,
                "target_note": row.target_note,
                "target_source": row.target_source,
                "news_articles": row.news_articles,
                "news_secondary": row.news_secondary,
                "news_note": row.news_note,
                "news_source": row.news_source,
                "share_positions_with_news": row.share_positions_with_news,
                "share_source": row.share_source,
                "warnings": " | ".join(row.warnings),
            }
            for row in rows
        ]
    )
    frame.to_csv(path, index=False)


def style_axis(ax: plt.Axes) -> None:
    ax.set_facecolor("white")
    ax.grid(axis="y", color="#d8dde6", linewidth=0.7, alpha=0.75)
    ax.set_axisbelow(True)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#98a2b3")
    ax.spines["bottom"].set_color("#98a2b3")


def plot_coverage(rows: list[CoverageRow], image_dir: Path) -> list[Path]:
    plt.rcParams.update(
        {
            "font.size": 9,
            "axes.titlesize": 10,
            "axes.labelsize": 9,
            "xtick.labelsize": 8.5,
            "ytick.labelsize": 8.5,
            "legend.fontsize": 8,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
        }
    )
    labels = [row.label for row in rows]
    x = np.arange(len(rows))
    colors = [row.color for row in rows]

    fig, axes = plt.subplots(3, 1, figsize=(7.2, 8.1), sharex=True)
    fig.suptitle("Coverage-Überblick der Strategiestufen S1 bis S4", fontsize=13, fontweight="bold", y=0.985)

    ax = axes[0]
    values = [row.target_securities if row.target_securities is not None else 0 for row in rows]
    bars = ax.bar(x, values, color=colors, width=0.58)
    style_axis(ax)
    ax.set_title("A) Zielwerte und Wertpapierabdeckung", loc="left", fontweight="bold")
    ax.set_ylabel("Anzahl Wertpapiere")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda val, _pos: de_int(val)))
    y_max = max(values + [row.target_secondary or 0 for row in rows]) * 1.20
    ax.set_ylim(0, y_max if y_max > 0 else 1)
    for i, (bar, row) in enumerate(zip(bars, rows)):
        if row.target_securities is not None:
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + y_max * 0.025,
                de_int(row.target_securities),
                ha="center",
                va="bottom",
                fontsize=8.5,
            )
        else:
            ax.text(i, y_max * 0.08, "n/v", ha="center", color="#667085", fontsize=8)
        if row.target_secondary is not None and row.target_securities is not None:
            marker_color = "#f2f4f7" if row.key == "s1" else "#111827"
            text_color = "#f2f4f7" if row.key == "s1" else "#344054"
            ax.scatter(i, row.target_secondary, marker="_", s=350, color=marker_color, linewidth=1.5, zorder=4)
            if row.key == "s1":
                ax.text(
                    i,
                    row.target_secondary - y_max * 0.045,
                    f"Common-News\n{de_int(row.target_secondary)}",
                    ha="center",
                    va="top",
                    fontsize=7.0,
                    color=text_color,
                    linespacing=0.95,
                )
            else:
                ax.text(
                    i + 0.08,
                    row.target_secondary + y_max * 0.025,
                    f"Common-News: {de_int(row.target_secondary)}",
                    ha="left",
                    va="bottom",
                    fontsize=7.6,
                    color=text_color,
                )

    ax = axes[1]
    values = [row.news_articles if row.news_articles is not None else 0 for row in rows]
    bars = ax.bar(x, values, color=colors, width=0.58)
    style_axis(ax)
    ax.set_title("B) Nachrichtenabdeckung", loc="left", fontweight="bold")
    ax.set_ylabel("Anzahl News")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda val, _pos: de_int(val)))
    secondary = [row.news_secondary or 0 for row in rows]
    y_max = max(values + secondary) * 1.18 if max(values + secondary) > 0 else 1
    ax.set_ylim(0, y_max)
    for i, (bar, row) in enumerate(zip(bars, rows)):
        if row.news_articles is not None:
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + y_max * 0.025,
                de_int(row.news_articles),
                ha="center",
                va="bottom",
                fontsize=8.5,
            )
        else:
            ax.text(i, y_max * 0.08, "kein Textsignal", ha="center", color="#667085", fontsize=8)
        if row.news_secondary is not None:
            ax.scatter(i, row.news_secondary, marker="_", s=350, color="#111827", linewidth=1.5, zorder=4)
            ax.text(
                i + 0.08,
                row.news_secondary + y_max * 0.02,
                f"Gesamt: {de_int(row.news_secondary)}",
                ha="left",
                va="bottom",
                fontsize=7.6,
                color="#344054",
            )

    ax = axes[2]
    values = [
        (row.share_positions_with_news * 100.0) if row.share_positions_with_news is not None else 0
        for row in rows
    ]
    bars = ax.bar(x, values, color=colors, width=0.58)
    style_axis(ax)
    ax.set_title("C) Anteil ausgewählter Positionen mit News", loc="left", fontweight="bold")
    ax.set_ylabel("Durchschnittlicher Anteil")
    ax.yaxis.set_major_formatter(FuncFormatter(lambda val, _pos: f"{val:.0f} %".replace(".", ",")))
    ax.set_ylim(0, 100)
    for i, (bar, row) in enumerate(zip(bars, rows)):
        if row.share_positions_with_news is not None:
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + 2.5,
                de_percent(row.share_positions_with_news),
                ha="center",
                va="bottom",
                fontsize=8.5,
            )
        else:
            ax.text(i, 5, "n/v", ha="center", color="#667085", fontsize=8)

    axes[-1].set_xticks(x)
    axes[-1].set_xticklabels(labels)
    fig.text(
        0.01,
        0.01,
        "Hinweis: S3-Balken zeigt English-Filter; Marker zeigt alle News. S1 hat kein eigenständiges Textsignal.",
        fontsize=7.5,
        color="#475467",
    )
    fig.tight_layout(rect=(0, 0.035, 1, 0.965), h_pad=1.35)

    png = image_dir / "ch4_coverage_overview.png"
    pdf = image_dir / "ch4_coverage_overview.pdf"
    fig.savefig(png, dpi=300, bbox_inches="tight")
    fig.savefig(pdf, bbox_inches="tight")
    plt.close(fig)
    return [png, pdf]


def add_direct_labels(ax: plt.Axes, plotted: list[tuple[EquitySeries, pd.DataFrame]]) -> None:
    if not plotted:
        return
    y_low, y_high = ax.get_ylim()
    span = max(0.001, y_high - y_low)
    min_gap = span * 0.055
    end_points = []
    for series, frame in plotted:
        last = frame.iloc[-1]
        end_points.append([series, last["date"], float(last["equity_norm"]), float(last["equity_norm"])])
    end_points.sort(key=lambda item: item[2])
    for idx in range(1, len(end_points)):
        if end_points[idx][3] - end_points[idx - 1][3] < min_gap:
            end_points[idx][3] = end_points[idx - 1][3] + min_gap
    overflow = end_points[-1][3] - (y_high - min_gap * 0.4)
    if overflow > 0:
        for item in end_points:
            item[3] -= overflow
    for series, x_date, y_value, y_label in end_points:
        arrowprops = None
        if abs(y_label - y_value) > min_gap * 0.3:
            arrowprops = {"arrowstyle": "-", "color": series.color, "lw": 0.6, "alpha": 0.7}
        ax.annotate(
            series.direct_label,
            xy=(x_date, y_value),
            xytext=(x_date + pd.Timedelta(days=45), y_label),
            textcoords="data",
            va="center",
            ha="left",
            fontsize=7.4,
            color=series.color,
            arrowprops=arrowprops,
            annotation_clip=False,
        )


def plot_equity(
    panel_results: list[PanelResult],
    all_series: dict[str, EquitySeries],
    image_dir: Path,
) -> list[Path]:
    plt.rcParams.update(
        {
            "font.size": 9,
            "axes.titlesize": 10,
            "axes.labelsize": 9,
            "xtick.labelsize": 8,
            "ytick.labelsize": 8,
            "legend.fontsize": 7.3,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
        }
    )
    fig, axes = plt.subplots(2, 2, figsize=(7.35, 6.6), sharex=True, sharey=True)
    axes_flat = axes.flatten()

    all_values: list[float] = []
    all_dates: list[pd.Timestamp] = []
    line_handles: dict[str, Any] = {}
    plotted_by_axis: list[list[tuple[EquitySeries, pd.DataFrame]]] = []

    for ax, panel in zip(axes_flat, panel_results):
        style_axis(ax)
        ax.set_title(f"Panel {panel.panel_key}: {panel.title}", loc="left", fontweight="bold")
        plotted: list[tuple[EquitySeries, pd.DataFrame]] = []
        for key, frame in panel.series.items():
            series = all_series[key]
            line, = ax.plot(
                frame["date"],
                frame["equity_norm"],
                label=series.label,
                color=series.color,
                linestyle=series.linestyle,
                linewidth=series.linewidth,
                alpha=0.95 if key not in {"benchmark"} else 0.82,
            )
            line_handles.setdefault(series.label, line)
            plotted.append((series, frame))
            all_values.extend(frame["equity_norm"].astype(float).tolist())
            all_dates.extend(pd.to_datetime(frame["date"]).tolist())
        plotted_by_axis.append(plotted)

    if all_values:
        y_min = min(all_values)
        y_max = max(all_values)
        pad = max(0.05, (y_max - y_min) * 0.10)
        for ax in axes_flat:
            ax.set_ylim(y_min - pad, y_max + pad)
            ax.yaxis.set_major_formatter(FuncFormatter(lambda val, _pos: f"{val:.1f}".replace(".", ",")))
    if all_dates:
        min_date = min(all_dates)
        max_date = max(all_dates) + pd.Timedelta(days=95)
        for ax in axes_flat:
            ax.set_xlim(min_date, max_date)
            ax.xaxis.set_major_locator(mdates.MonthLocator(interval=6))
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))

    for ax, plotted in zip(axes_flat, plotted_by_axis):
        add_direct_labels(ax, plotted)

    axes_flat[0].set_ylabel("Indexierter Portfoliowert\n(Start = 1,0)")
    axes_flat[2].set_ylabel("Indexierter Portfoliowert\n(Start = 1,0)")
    for ax in axes_flat[2:]:
        ax.set_xlabel("Datum")

    fig.suptitle(
        "Equity-Kurven der Hauptstrategien im Neutral-Fill-Setup",
        fontsize=13,
        fontweight="bold",
        y=0.985,
    )
    fig.text(
        0.5,
        0.925,
        "research_signal_date, all_days, standard; Equity auf Startwert 1,0 normalisiert",
        ha="center",
        fontsize=8,
        color="#475467",
    )
    fig.legend(
        list(line_handles.values()),
        list(line_handles.keys()),
        loc="lower center",
        bbox_to_anchor=(0.5, 0.012),
        ncol=3,
        frameon=False,
        handlelength=2.8,
        columnspacing=1.1,
    )
    fig.tight_layout(rect=(0, 0.08, 1, 0.905), h_pad=1.15, w_pad=0.8)

    png = image_dir / "ch4_main_equity_curves.png"
    pdf = image_dir / "ch4_main_equity_curves.pdf"
    fig.savefig(png, dpi=300, bbox_inches="tight")
    fig.savefig(pdf, bbox_inches="tight")
    plt.close(fig)
    return [png, pdf]


def matched_supplement_panel(
    keys: list[str],
    all_series: dict[str, EquitySeries],
) -> list[tuple[EquitySeries, pd.DataFrame]]:
    available = [all_series[key] for key in keys if key in all_series]
    if not available:
        return []
    date_sets = [set(series.data["date"]) for series in available]
    common_dates = sorted(set.intersection(*date_sets)) if date_sets else []
    if not common_dates:
        return []
    common_index = pd.DatetimeIndex(common_dates)
    plotted: list[tuple[EquitySeries, pd.DataFrame]] = []
    for series in available:
        frame = series.data[series.data["date"].isin(common_index)].copy().sort_values("date")
        frame = frame.dropna(subset=["equity"])
        frame = frame[np.isfinite(frame["equity"])]
        if frame.empty:
            continue
        base = float(frame["equity"].iloc[0])
        if not math.isfinite(base) or abs(base) < 1e-12:
            continue
        frame["equity_norm"] = frame["equity"] / base
        plotted.append((series, frame))
    return plotted


def plot_supplementary_equity(
    all_series: dict[str, EquitySeries],
    image_dir: Path,
) -> list[Path]:
    plt.rcParams.update(
        {
            "font.size": 9,
            "axes.titlesize": 10,
            "axes.labelsize": 9,
            "xtick.labelsize": 8,
            "ytick.labelsize": 8,
            "legend.fontsize": 7.5,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
        }
    )
    figure_specs = [
        {
            "filename": "ch4_main_equity_curves_compact.png",
            "title": "Equity-Kurven kompakt: Hauptsignale und S4-Shrinkage",
            "subtitle": "Je Panel matched dates; Equity auf Startwert 1,0 normalisiert",
            "panels": [
                ("A", "Text-only und Benchmark", ["s2", "s3", "s4", "benchmark"]),
                ("B", "S4 70/30 gegen Referenzen", ["s1", "s1_s4_70", "benchmark"]),
            ],
        },
        {
            "filename": "ch4_main_equity_curves_hybrids_only.png",
            "title": "Equity-Kurven der Hybridstrategien",
            "subtitle": "research_signal_date, all_days, standard; Equity auf Startwert 1,0 normalisiert",
            "panels": [
                ("A", "50/50-Hybride", ["s1", "s1_s2_50", "s1_s3_50", "s1_s4_50"]),
                ("B", "S4-Gewichtung und Benchmark", ["s1", "s1_s4_50", "s1_s4_70", "benchmark"]),
            ],
        },
    ]

    outputs: list[Path] = []
    for spec in figure_specs:
        fig, axes = plt.subplots(1, 2, figsize=(7.35, 4.2), sharey=True)
        all_values: list[float] = []
        all_dates: list[pd.Timestamp] = []
        line_handles: dict[str, Any] = {}
        plotted_by_axis: list[list[tuple[EquitySeries, pd.DataFrame]]] = []

        for ax, (panel_key, panel_title, keys) in zip(axes, spec["panels"]):
            style_axis(ax)
            ax.set_title(f"Panel {panel_key}: {panel_title}", loc="left", fontweight="bold")
            plotted = matched_supplement_panel(keys, all_series)
            plotted_by_axis.append(plotted)
            for series, frame in plotted:
                line, = ax.plot(
                    frame["date"],
                    frame["equity_norm"],
                    label=series.label,
                    color=series.color,
                    linestyle=series.linestyle,
                    linewidth=series.linewidth,
                    alpha=0.95 if series.key != "benchmark" else 0.82,
                )
                line_handles.setdefault(series.label, line)
                all_values.extend(frame["equity_norm"].astype(float).tolist())
                all_dates.extend(pd.to_datetime(frame["date"]).tolist())

        if all_values:
            y_min = min(all_values)
            y_max = max(all_values)
            pad = max(0.05, (y_max - y_min) * 0.10)
            for ax in axes:
                ax.set_ylim(y_min - pad, y_max + pad)
                ax.yaxis.set_major_formatter(FuncFormatter(lambda val, _pos: f"{val:.1f}".replace(".", ",")))
        if all_dates:
            min_date = min(all_dates)
            max_date = max(all_dates) + pd.Timedelta(days=105)
            for ax in axes:
                ax.set_xlim(min_date, max_date)
                ax.xaxis.set_major_locator(mdates.MonthLocator(interval=6))
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
                ax.set_xlabel("Datum")

        axes[0].set_ylabel("Indexierter Portfoliowert\n(Start = 1,0)")
        for ax, plotted in zip(axes, plotted_by_axis):
            add_direct_labels(ax, plotted)

        fig.suptitle(str(spec["title"]), fontsize=12.5, fontweight="bold", y=0.985)
        fig.text(0.5, 0.92, str(spec["subtitle"]), ha="center", fontsize=8, color="#475467")
        fig.legend(
            list(line_handles.values()),
            list(line_handles.keys()),
            loc="lower center",
            bbox_to_anchor=(0.5, 0.012),
            ncol=3,
            frameon=False,
            handlelength=2.8,
            columnspacing=1.1,
        )
        fig.tight_layout(rect=(0, 0.12, 1, 0.88), w_pad=0.9)
        out = image_dir / str(spec["filename"])
        fig.savefig(out, dpi=300, bbox_inches="tight")
        plt.close(fig)
        outputs.append(out)
    return outputs


def write_audit(
    path: Path,
    data_root: Path,
    latex_root: Path | None,
    image_dir: Path,
    figure_dir: Path,
    db_paths: dict[str, Path],
    index_path: Path | None,
    chapter_path: Path | None,
    coverage_rows: list[CoverageRow],
    series: dict[str, EquitySeries],
    panel_results: list[PanelResult],
    series_audit_rows: list[dict[str, Any]],
    generated_files: list[Path],
    warnings: list[str],
) -> None:
    lines: list[str] = []
    lines.append("# Chapter 4 Figure Audit")
    lines.append("")
    lines.append(f"- Script version: `{SCRIPT_VERSION}`")
    lines.append(f"- Data root: `{data_root}`")
    lines.append(f"- LaTeX root: `{latex_root if latex_root is not None else 'not found'}`")
    lines.append(f"- Image dir: `{image_dir}`")
    lines.append(f"- Figure output dir: `{figure_dir}`")
    lines.append(f"- Audit file: `{path}`")
    lines.append("")
    lines.append("## Input Files")
    for key, path_item in db_paths.items():
        status = "FOUND" if path_item.exists() else "MISSING"
        lines.append(f"- {status}: {key} `{path_item}`")
    if index_path is not None:
        lines.append(f"- FOUND: index fallback `{index_path}`")
    else:
        lines.append("- MISSING: index fallback")
    if chapter_path is not None and chapter_path.exists():
        chapter_text = chapter_path.read_text(encoding="utf-8", errors="replace")
        has_coverage_ref = "ch4_coverage_overview" in chapter_text
        has_equity_ref = "ch4_main_equity_curves" in chapter_text
        lines.append(f"- FOUND: chapter check `{chapter_path}`")
        lines.append(f"- Chapter references ch4_coverage_overview: `{has_coverage_ref}`")
        lines.append(f"- Chapter references ch4_main_equity_curves: `{has_equity_ref}`")
        if not (has_coverage_ref and has_equity_ref):
            lines.append("- WARN: Chapter file does not currently reference both new figure names. No .tex file was modified.")
    else:
        lines.append("- MISSING: sections/4_empirical_results.tex check")

    lines.append("")
    lines.append("## Coverage Values")
    lines.append("| Stufe | Zielwerte | Zusatz | News | Zusatz News | Anteil Positionen mit News | Quelle |")
    lines.append("|---|---:|---:|---:|---:|---:|---|")
    for row in coverage_rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    row.label,
                    de_int(row.target_securities),
                    de_int(row.target_secondary),
                    de_int(row.news_articles),
                    de_int(row.news_secondary),
                    de_percent(row.share_positions_with_news),
                    f"{row.target_source}; {row.news_source}; {row.share_source}".strip("; "),
                ]
            )
            + " |"
        )

    lines.append("")
    lines.append("## Planned Equity Series")
    lines.append("| Serie | Quelle | Equity-Spalte | Return-Spalte | Rohdaten n | Rohdaten Start | Rohdaten Ende |")
    lines.append("|---|---|---|---|---:|---|---|")
    for series_item in series.values():
        data = series_item.data
        lines.append(
            "| "
            + " | ".join(
                [
                    series_item.label,
                    series_item.source_note,
                    series_item.equity_col or "",
                    series_item.return_col or "",
                    str(len(data)),
                    data["date"].min().date().isoformat(),
                    data["date"].max().date().isoformat(),
                ]
            )
            + " |"
        )

    lines.append("")
    lines.append("## Panel-Matched Equity Audit")
    lines.append("| Panel | Serie | n_days | Start | Ende | End-Equity | Kumulierte Rendite | Quelle |")
    lines.append("|---|---|---:|---|---|---:|---:|---|")
    for row in series_audit_rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row["panel"]),
                    str(row["series_label"]),
                    str(row["n_days"]),
                    str(row["first_date"]),
                    str(row["last_date"]),
                    de_decimal(row["end_equity"], 3),
                    de_percent(row["cumulative_return"], 1),
                    str(row["source"]),
                ]
            )
            + " |"
        )

    lines.append("")
    lines.append("## Validation Checks")
    all_inputs_found = all(path_item.exists() for path_item in db_paths.values())
    all_images_ok = all(path_item.exists() and path_item.stat().st_size > 0 for path_item in generated_files)
    no_nan = all(int(row.get("nan_equity_values", 0)) == 0 for row in series_audit_rows)
    starts_at_one = all(abs(float(row.get("start_equity", 0)) - 1.0) < 1e-10 for row in series_audit_rows)
    lines.append(f"- Input databases complete: `{all_inputs_found}`")
    lines.append(f"- No NaN values in final plotted equity values: `{no_nan}`")
    lines.append(f"- Equity starts at 1.0 after normalization: `{starts_at_one}`")
    lines.append("- Coverage values source: DB metadata/output tables unless warnings below state fallback.")
    lines.append(f"- Images successfully written: `{all_images_ok}`")
    for panel in panel_results:
        n_lines = len(panel.series)
        lines.append(f"- Panel {panel.panel_key} line count: `{n_lines}`")
        if n_lines > 4:
            lines.append(f"- WARN Panel {panel.panel_key}: more than 4 lines.")

    lines.append("")
    lines.append("## Generated Files")
    for generated in generated_files:
        status = "OK" if generated.exists() and generated.stat().st_size > 0 else "MISSING"
        size = generated.stat().st_size if generated.exists() else 0
        lines.append(f"- {status}: `{generated}` ({size} bytes)")

    lines.append("")
    lines.append("## Warnings")
    unique_warnings = []
    for warning in warnings:
        if warning and warning not in unique_warnings:
            unique_warnings.append(warning)
    if unique_warnings:
        for warning in unique_warnings:
            lines.append(f"- {warning}")
    else:
        lines.append("- None")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    data_root = args.data_root.expanduser().resolve()
    latex_root = resolve_latex_root(data_root, args.latex_root)
    image_dir = args.image_dir.expanduser().resolve() if args.image_dir else None
    if image_dir is None:
        image_dir = (latex_root / "images") if latex_root is not None else (data_root / "images")
    figure_dir = args.figure_output_dir.expanduser().resolve() if args.figure_output_dir else data_root / "outputs" / "figures"
    image_dir.mkdir(parents=True, exist_ok=True)
    figure_dir.mkdir(parents=True, exist_ok=True)

    db_paths = {key: data_root / rel for key, rel in DB_FILES.items()}
    index_path = find_index_html(data_root, latex_root)
    chapter_path = (latex_root / "sections" / "4_empirical_results.tex") if latex_root is not None else None

    payload = load_dashboard_payload(index_path)

    coverage_rows, coverage_warnings = collect_coverage(data_root, db_paths, payload)
    series, series_warnings = build_equity_series(db_paths, payload)
    panel_results, series_audit_rows, equity_points, panel_warnings = build_panel_results(series)

    coverage_csv = figure_dir / "ch4_coverage_data_used.csv"
    series_csv = figure_dir / "ch4_equity_series_audit.csv"
    points_csv = figure_dir / "ch4_equity_curve_points_used.csv"
    write_coverage_csv(coverage_rows, coverage_csv)
    pd.DataFrame(series_audit_rows).to_csv(series_csv, index=False)
    equity_points.to_csv(points_csv, index=False)

    generated_files = []
    generated_files.extend(plot_coverage(coverage_rows, image_dir))
    generated_files.extend(plot_equity(panel_results, series, image_dir))
    generated_files.extend(plot_supplementary_equity(series, image_dir))
    generated_files.extend([coverage_csv, series_csv, points_csv])

    audit_path = figure_dir / "ch4_figure_audit.md"
    warnings = coverage_warnings + series_warnings + panel_warnings
    for series_item in series.values():
        warnings.extend(series_item.warnings)
    for panel in panel_results:
        warnings.extend(panel.warnings)
    write_audit(
        audit_path,
        data_root,
        latex_root,
        image_dir,
        figure_dir,
        db_paths,
        index_path,
        chapter_path,
        coverage_rows,
        series,
        panel_results,
        series_audit_rows,
        generated_files,
        warnings,
    )
    generated_files.append(audit_path)

    print("Erzeugte Dateien:")
    for path_item in generated_files:
        print(f"- {path_item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
