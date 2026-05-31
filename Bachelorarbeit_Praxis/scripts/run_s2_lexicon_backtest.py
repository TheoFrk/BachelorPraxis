#!/usr/bin/env python3
"""
S2 lexicon hybrid backtest for the STOXX Europe 600 thesis project.

S2 combines the existing price-only S1 score with Loughran-McDonald
dictionary sentiment by late fusion:

    S2_{i,t} = 0.5 * z_t(S1_{i,t}) + 0.5 * z_t(LEX_{i,t})

The script is self-contained, uses only local files, and writes a new
versioned result database. Existing S1 results and input databases are never
modified.
"""

from __future__ import annotations

import argparse
import html
import hashlib
import json
import math
import re
import shutil
import sqlite3
import sys
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


SCRIPT_VERSION = "2026-05-18-s2-lexicon-v1"
PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_COST_BPS = [5.0, 10.0, 25.0]
TEXT_COL_CANDIDATES = ["title", "headline", "summary", "description", "body", "text", "content"]
VARIANT_CONFIGS = [
    ("S2_LEXICON_ONLY", "S2_LEX_ONLY_NEUTRAL_FILL", "NEUTRAL_FILL", 0.0, 1.0),
    ("S2_LEXICON_ONLY", "S2_LEX_ONLY_EVENT_ONLY", "EVENT_ONLY", 0.0, 1.0),
    ("HYBRID_S1_LEX_50_50", "HYBRID_S1_LEX_50_50_NEUTRAL_FILL", "NEUTRAL_FILL", 0.5, 0.5),
    ("HYBRID_S1_LEX_50_50", "HYBRID_S1_LEX_50_50_EVENT_ONLY", "EVENT_ONLY", 0.5, 0.5),
]
STRATEGY_VARIANTS = [cfg[1] for cfg in VARIANT_CONFIGS]
TIME_LOGICS = ["research_signal_date", "conservative_price_lag1"]
PORTFOLIO_VARIANTS = ["standard"]
COVERAGE_FILTERS = ["all_days", "coverage_80"]
KEY_COLS = ["model_family", "strategy_variant", "news_mode", "time_logic", "portfolio_variant", "coverage_filter"]
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
TOKEN_RE = re.compile(r"[a-z]+")
EMPTY_TABLE_COLUMNS = {
    "s2_vs_benchmark_daily": KEY_COLS
    + [
        "date",
        "strategy_return",
        "benchmark_ret_fwd_1d",
        "excess_return",
        "strategy_equity_matched",
        "benchmark_equity_matched",
    ],
    "s2_vs_benchmark_metrics": KEY_COLS
    + [
        "n_matched_days",
        "benchmark_symbol",
        "strategy_cum_return_matched",
        "benchmark_cum_return_matched",
        "active_cum_return",
        "mean_daily_excess_return",
        "tracking_error",
        "information_ratio",
    ],
    "s2_vs_s1_daily": KEY_COLS
    + [
        "date",
        "s2_return",
        "s1_return",
        "return_diff",
        "s2_equity_matched",
        "s1_equity_matched",
    ],
    "s2_vs_s1_metrics": KEY_COLS
    + [
        "n_matched_days",
        "s2_cum_return_matched",
        "s1_cum_return_matched",
        "active_cum_return_vs_s1",
        "mean_daily_diff",
        "tracking_error_vs_s1",
        "information_ratio_vs_s1",
    ],
    "s2_ic_by_date": ["model_family", "strategy_variant", "news_mode", "time_logic", "signal_name", "date", "n_candidates", "spearman_ic"],
    "s2_ic_summary": [
        "model_family",
        "strategy_variant",
        "news_mode",
        "time_logic",
        "signal_name",
        "mean_ic",
        "median_ic",
        "positive_ic_share",
        "ic_t_stat_naive",
        "ic_t_stat_newey_west",
        "newey_west_lag",
        "n_ic_days",
    ],
    "s2_quintile_spread_returns": [
        "model_family",
        "strategy_variant",
        "news_mode",
        "time_logic",
        "signal_name",
        "date",
        "high_ret_1d",
        "low_ret_1d",
        "spread_ret_1d",
        "notes",
    ],
    "s2_quintile_spread_metrics": [
        "model_family",
        "strategy_variant",
        "news_mode",
        "time_logic",
        "signal_name",
        "n_days",
        "mean_daily_spread",
        "cumulative_spread",
        "spread_t_stat_naive",
        "notes",
    ],
    "s2_sector_exposure": KEY_COLS + ["sector", "avg_weight", "selected_days"],
    "s2_country_exposure": KEY_COLS + ["country", "avg_weight", "selected_days"],
    "s2_top_positions": KEY_COLS
    + ["security_id", "yahoo_ticker", "company", "country", "sector", "selected_days", "avg_weight", "avg_score_used"],
}


@dataclass
class InputBundle:
    project_dir: Path
    input_db: Path
    input_s1_results_db: Path | None
    price_panel: pd.DataFrame
    news: pd.DataFrame
    securities: pd.DataFrame
    benchmark_prices: pd.DataFrame
    input_tables: list[str]
    price_table: str
    text_cols: list[str]
    common_universe_source: str
    security_news_coverage_threshold: float
    common_news_security_coverage: pd.DataFrame
    source_notes: list[str]


@dataclass
class LexiconBundle:
    terms: pd.DataFrame
    source_path: Path | None
    is_toy: bool
    conflict_count: int
    positive_count: int
    negative_count: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run S2 lexicon hybrid backtests.")
    parser.add_argument("--input-db", type=Path, default=None)
    parser.add_argument("--input-s1-results-db", type=Path, default=Path("outputs/results/s1_revised_results.sqlite"))
    parser.add_argument("--output-db", type=Path, default=Path("outputs/results/s2_lexicon_results.sqlite"))
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"))
    parser.add_argument("--lexicon-csv", type=Path, default=Path("DATA/lexicons/Loughran-McDonald_MasterDictionary_1993-2025.csv"))
    parser.add_argument("--top-pct", type=float, default=0.20)
    parser.add_argument("--cost-bps", type=float, nargs="+", default=DEFAULT_COST_BPS)
    parser.add_argument("--language", default="English")
    parser.add_argument("--text-cols", nargs="+", default=None)
    parser.add_argument("--min-token-count", type=int, default=3)
    parser.add_argument("--allow-toy-lexicon", action="store_true")
    parser.add_argument("--coverage-threshold", type=float, default=0.80)
    parser.add_argument(
        "--common-universe-source",
        choices=["news_model_input", "daily_text_scores", "auto"],
        default="news_model_input",
    )
    parser.add_argument("--security-news-coverage-threshold", type=float, default=0.0)
    return parser.parse_args()


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def sqlite_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")
    }


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({quote_ident(table)})")]


def read_sql_if_exists(conn: sqlite3.Connection, table: str) -> pd.DataFrame:
    if table not in sqlite_tables(conn):
        return pd.DataFrame()
    return pd.read_sql_query(f"SELECT * FROM {quote_ident(table)}", conn)


def resolve_existing_path(path: Path | None, candidates: Iterable[Path]) -> Path:
    if path is not None:
        resolved = path.expanduser()
        if not resolved.is_absolute():
            cwd_candidate = Path.cwd() / resolved
            project_candidate = PROJECT_DIR / resolved
            if cwd_candidate.exists():
                resolved = cwd_candidate
            else:
                resolved = project_candidate
        if resolved.exists():
            return resolved.resolve()
        raise FileNotFoundError(f"Input not found: {resolved}")
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    raise FileNotFoundError("No suitable input database found. Tried: " + ", ".join(str(p) for p in candidates))


def resolve_optional_path(path: Path | None) -> Path | None:
    if path is None:
        return None
    resolved = path.expanduser()
    if not resolved.is_absolute():
        cwd_candidate = Path.cwd() / resolved
        project_candidate = PROJECT_DIR / resolved
        resolved = cwd_candidate if cwd_candidate.exists() else project_candidate
    return resolved.resolve() if resolved.exists() else None


def resolve_output_path(path: Path) -> Path:
    out = path.expanduser()
    if not out.is_absolute():
        out = PROJECT_DIR / out
    return out.resolve()


def zscore_by_date(df: pd.DataFrame, col: str, date_col: str = "date") -> pd.Series:
    values = pd.to_numeric(df[col], errors="coerce")
    grouped = values.groupby(df[date_col], observed=True)
    mean = grouped.transform("mean")
    std = grouped.transform(lambda x: x.std(ddof=0))
    z = (values - mean) / std
    return z.where(std.gt(0) & std.notna(), 0.0).fillna(0.0)


def common_news_universe_name(threshold: float) -> str:
    if abs(threshold) < 1e-12:
        return "S1_COMMON_NEWS_ANY"
    return f"S1_COMMON_NEWS_COV{int(round(threshold * 100))}"


def common_news_include_col(threshold: float) -> str:
    if abs(threshold) < 1e-12:
        return "included_any"
    return f"included_cov{int(round(threshold * 100))}"


def resolve_common_universe_source(conn: sqlite3.Connection, source: str) -> tuple[str, list[str]]:
    tables = sqlite_tables(conn)
    notes: list[str] = []
    if source == "auto":
        if "news_model_input" in tables and conn.execute("SELECT COUNT(*) FROM news_model_input").fetchone()[0] > 0:
            notes.append("common_universe_source=auto resolved to news_model_input.")
            return "news_model_input", notes
        if "daily_text_scores" in tables and conn.execute("SELECT COUNT(*) FROM daily_text_scores").fetchone()[0] > 0:
            notes.append("common_universe_source=auto resolved to daily_text_scores because news_model_input is missing or empty.")
            return "daily_text_scores", notes
        raise RuntimeError("No non-empty news_model_input or daily_text_scores table available for common universe.")
    if source not in tables:
        raise RuntimeError(f"Requested common universe source table not found: {source}")
    if conn.execute(f"SELECT COUNT(*) FROM {quote_ident(source)}").fetchone()[0] == 0:
        raise RuntimeError(f"Requested common universe source table is empty: {source}")
    notes.append(f"common_universe_source explicitly set to {source}.")
    return source, notes


def build_common_news_universe(
    input_db: Path,
    price: pd.DataFrame,
    source: str,
    threshold: float,
) -> tuple[set[int], pd.DataFrame, str, list[str]]:
    with sqlite3.connect(input_db) as conn:
        resolved_source, notes = resolve_common_universe_source(conn, source)
        cols = table_columns(conn, resolved_source)
        if "security_id" not in cols:
            raise RuntimeError(f"{resolved_source} is missing security_id.")
        if resolved_source == "news_model_input":
            if "signal_date" not in cols:
                raise RuntimeError("news_model_input is missing signal_date.")
            date_col = "signal_date"
        elif "signal_date" in cols:
            date_col = "signal_date"
        elif "date" in cols:
            date_col = "date"
        else:
            raise RuntimeError(f"{resolved_source} is missing signal_date/date.")
        news = pd.read_sql_query(
            f"SELECT security_id, {quote_ident(date_col)} AS signal_date FROM {quote_ident(resolved_source)}",
            conn,
        )

    valid = price.dropna(subset=["security_id", "date", "z_s1_price_score", "ret_fwd_1d"]).copy()
    valid["security_id"] = pd.to_numeric(valid["security_id"], errors="coerce").astype("Int64")
    valid["date"] = pd.to_datetime(valid["date"], errors="coerce")
    valid = valid.dropna(subset=["security_id", "date"]).copy()
    valid["security_id"] = valid["security_id"].astype(int)
    valid_days = valid[["security_id", "date"]].drop_duplicates()
    valid_summary = (
        valid_days.groupby("security_id", observed=True)
        .agg(
            n_valid_price_days=("date", "nunique"),
            first_valid_price_date=("date", "min"),
            last_valid_price_date=("date", "max"),
        )
        .reset_index()
    )
    news["security_id"] = pd.to_numeric(news["security_id"], errors="coerce").astype("Int64")
    news["signal_date"] = pd.to_datetime(news["signal_date"], errors="coerce")
    news = news.dropna(subset=["security_id", "signal_date"]).copy()
    news["security_id"] = news["security_id"].astype(int)
    news_days = news[["security_id", "signal_date"]].drop_duplicates()
    matched = valid_days.merge(news_days.rename(columns={"signal_date": "date"}), on=["security_id", "date"], how="inner")
    news_summary = (
        matched.groupby("security_id", observed=True)
        .agg(
            n_news_signal_days_on_valid_price_dates=("date", "nunique"),
            first_news_signal_date=("date", "min"),
            last_news_signal_date=("date", "max"),
        )
        .reset_index()
    )
    coverage = valid_summary.merge(news_summary, on="security_id", how="left")
    coverage["n_news_signal_days_on_valid_price_dates"] = coverage[
        "n_news_signal_days_on_valid_price_dates"
    ].fillna(0).astype(int)
    coverage["news_coverage_ratio"] = np.where(
        coverage["n_valid_price_days"] > 0,
        coverage["n_news_signal_days_on_valid_price_dates"] / coverage["n_valid_price_days"],
        0.0,
    )
    coverage["universe_source"] = resolved_source
    meta_cols = [c for c in ["security_id", "yahoo_ticker", "company", "country", "sector"] if c in valid.columns]
    if meta_cols:
        coverage = coverage.merge(valid[meta_cols].drop_duplicates("security_id"), on="security_id", how="left")
    include_col = common_news_include_col(threshold)
    if abs(threshold) < 1e-12:
        mask = coverage["n_news_signal_days_on_valid_price_dates"].ge(1)
    else:
        mask = coverage["news_coverage_ratio"].ge(threshold)
    coverage[include_col] = mask.astype(int)
    ids = set(coverage.loc[mask, "security_id"].astype(int))
    notes.append(f"{common_news_universe_name(threshold)} selected for S2/HYBRID: {len(ids)} securities.")
    return ids, coverage.sort_values("security_id"), resolved_source, notes


def format_date_value(value) -> str | None:
    if value is None or pd.isna(value):
        return None
    return pd.to_datetime(value).strftime("%Y-%m-%d")


def prepare_for_sql(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[col]):
            out[col] = out[col].dt.strftime("%Y-%m-%d")
        elif col.endswith("_date") or col == "date" or col in {"first_date", "last_date"}:
            try:
                parsed = pd.to_datetime(out[col], errors="coerce")
                mask = parsed.notna()
                out.loc[mask, col] = parsed.loc[mask].dt.strftime("%Y-%m-%d")
            except Exception:
                pass
    return out.replace({pd.NaT: None, np.nan: None})


def load_inputs(args: argparse.Namespace) -> InputBundle:
    project_dir = PROJECT_DIR
    input_db = resolve_existing_path(
        args.input_db,
        [
            project_dir / "DATA" / "thesis_data v2.sqlite",
            project_dir / "DATA" / "thesis_data.sqlite",
            project_dir / "Projekt_Bachelor_DATENAUFBEREITUNG" / "thesis_data.sqlite",
        ],
    )
    input_s1 = resolve_optional_path(args.input_s1_results_db)
    source_notes: list[str] = []
    with sqlite3.connect(input_db) as conn:
        tables = sorted(sqlite_tables(conn))
        if "price_features_s1" in tables:
            price_table = "price_features_s1"
        elif "backtest_panel_price_only" in tables:
            price_table = "backtest_panel_price_only"
        else:
            raise RuntimeError("No S1 price table found. Expected price_features_s1 or backtest_panel_price_only.")

        price_cols = table_columns(conn, price_table)
        price = pd.read_sql_query(f"SELECT * FROM {quote_ident(price_table)}", conn)
        securities = read_sql_if_exists(conn, "securities")
        if not securities.empty:
            keep_sec = [c for c in ["security_id", "yahoo_ticker", "company", "country", "sector"] if c in securities.columns]
            sec_small = securities[keep_sec].drop_duplicates("security_id").copy()
            for col in ["yahoo_ticker", "company", "country", "sector"]:
                if col not in price.columns and col in sec_small.columns:
                    price = price.merge(sec_small[["security_id", col]], on="security_id", how="left")
            if "yahoo_ticker" in price.columns and "yahoo_ticker" in sec_small.columns:
                price = price.merge(
                    sec_small.rename(columns={"yahoo_ticker": "_sec_yahoo_ticker"}),
                    on="security_id",
                    how="left",
                    suffixes=("", "_sec"),
                )
                for col in ["company", "country", "sector"]:
                    sec_col = f"{col}_sec"
                    if sec_col in price.columns:
                        price[col] = price[col].combine_first(price[sec_col]) if col in price.columns else price[sec_col]
                        price = price.drop(columns=[sec_col])
                if "_sec_yahoo_ticker" in price.columns:
                    price["yahoo_ticker"] = price["yahoo_ticker"].combine_first(price["_sec_yahoo_ticker"])
                    price = price.drop(columns=["_sec_yahoo_ticker"])
        source_notes.append(f"Loaded price panel from {price_table} with {len(price):,} rows and columns {price_cols}.")

        if "news_model_input" not in tables:
            raise RuntimeError("Required table news_model_input not found.")
        news_cols = table_columns(conn, "news_model_input")
        required_news = {"signal_date", "security_id"}
        missing_news = required_news.difference(news_cols)
        if missing_news:
            raise RuntimeError(f"news_model_input is missing required columns: {sorted(missing_news)}")
        requested_text_cols = args.text_cols or TEXT_COL_CANDIDATES
        text_cols = [c for c in requested_text_cols if c in news_cols]
        if not text_cols:
            raise RuntimeError(
                "news_model_input has no usable text column. Tried: " + ", ".join(requested_text_cols)
            )
        optional_news = [
            "article_id",
            "publication_date",
            "language",
            "yahoo_ticker",
            "company",
            "country",
            "sector",
            "mapping_confidence",
        ]
        select_cols = []
        for col in ["article_id", "signal_date", "publication_date", "security_id", *optional_news, *text_cols]:
            if col in news_cols and col not in select_cols:
                select_cols.append(col)
        where = ""
        params: tuple[str, ...] = ()
        if "language" in news_cols and args.language:
            where = " WHERE language = ? OR language IS NULL"
            params = (args.language,)
        sql = "SELECT " + ", ".join(quote_ident(c) for c in select_cols) + " FROM news_model_input" + where
        news = pd.read_sql_query(sql, conn, params=params)
        source_notes.append(f"Loaded news_model_input with {len(news):,} rows; text columns: {', '.join(text_cols)}.")

        benchmark = load_benchmark_prices(conn, source_notes)

    return InputBundle(
        project_dir=project_dir,
        input_db=input_db,
        input_s1_results_db=input_s1,
        price_panel=price,
        news=news,
        securities=securities,
        benchmark_prices=benchmark,
        input_tables=tables,
        price_table=price_table,
        text_cols=text_cols,
        common_universe_source=args.common_universe_source,
        security_news_coverage_threshold=args.security_news_coverage_threshold,
        common_news_security_coverage=pd.DataFrame(),
        source_notes=source_notes,
    )


def load_benchmark_prices(conn: sqlite3.Connection, source_notes: list[str]) -> pd.DataFrame:
    tables = sqlite_tables(conn)
    if {"benchmark_prices_daily", "benchmark_instruments"}.issubset(tables):
        df = pd.read_sql_query(
            """
            SELECT p.symbol, p.date, p.adjusted_close, b.name, b.is_preferred
            FROM benchmark_prices_daily p
            JOIN benchmark_instruments b ON b.symbol = p.symbol
            ORDER BY p.symbol, p.date
            """,
            conn,
        )
        source_notes.append("Loaded benchmark_prices_daily joined to benchmark_instruments.")
        return df
    source_notes.append("Benchmark tables missing; S2 will run without benchmark comparison.")
    return pd.DataFrame()


def prepare_price_panel(price: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    out = price.copy()
    notes: list[str] = []
    required = {"date", "security_id", "ret_fwd_1d"}
    missing = required.difference(out.columns)
    if missing:
        raise RuntimeError(f"Price table is missing required columns: {sorted(missing)}")
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out["security_id"] = pd.to_numeric(out["security_id"], errors="coerce").astype("Int64")
    out["ret_fwd_1d"] = pd.to_numeric(out["ret_fwd_1d"], errors="coerce")
    for col in ["z_s1_price_score", "s1_price_score", "z_mom_12_1", "z_lowvol_60d", "z_lowvol_63d"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")

    if "z_s1_price_score" in out.columns and out["z_s1_price_score"].notna().any():
        notes.append("Using existing z_s1_price_score from price table.")
    elif "s1_price_score" in out.columns and out["s1_price_score"].notna().any():
        out["z_s1_price_score"] = zscore_by_date(out, "s1_price_score")
        notes.append("Computed z_s1_price_score by date from s1_price_score.")
    else:
        if "z_mom_12_1" not in out.columns:
            raise RuntimeError("Cannot compute S1: missing z_s1_price_score, s1_price_score, and z_mom_12_1.")
        lowvol_col = None
        if "z_lowvol_60d" in out.columns and out["z_lowvol_60d"].notna().any():
            lowvol_col = "z_lowvol_60d"
        elif "z_lowvol_63d" in out.columns and out["z_lowvol_63d"].notna().any():
            lowvol_col = "z_lowvol_63d"
        if lowvol_col is None:
            raise RuntimeError("Cannot compute S1: missing z_lowvol_60d/z_lowvol_63d.")
        out["s1_price_score"] = 0.5 * out["z_mom_12_1"] + 0.5 * out[lowvol_col]
        out["z_s1_price_score"] = zscore_by_date(out, "s1_price_score")
        notes.append(f"Computed s1_price_score from z_mom_12_1 and {lowvol_col}.")

    out = out.dropna(subset=["date", "security_id"]).copy()
    out["security_id"] = out["security_id"].astype(int)
    out = out.sort_values(["security_id", "date"])
    out["z_s1_price_score_lag1"] = out.groupby("security_id", observed=True)["z_s1_price_score"].shift(1)
    for col in ["yahoo_ticker", "company", "country", "sector"]:
        if col not in out.columns:
            out[col] = None
    return out, notes


def find_lexicon_path(project_dir: Path, explicit: Path | None) -> Path | None:
    if explicit is not None:
        path = explicit.expanduser()
        if not path.is_absolute():
            path = project_dir / path
        return path.resolve() if path.exists() else None
    patterns = [
        project_dir / "data" / "lexicons",
        project_dir / "DATA" / "lexicons",
        project_dir / "lexicons",
    ]
    for root in patterns:
        if root.exists():
            matches = sorted(root.glob("Loughran-McDonald*.csv"))
            if matches:
                return matches[0].resolve()
    matches = sorted(project_dir.glob("**/Loughran-McDonald*.csv"))
    return matches[0].resolve() if matches else None


def load_lm_lexicon(project_dir: Path, lexicon_csv: Path | None, allow_toy: bool) -> LexiconBundle:
    path = find_lexicon_path(project_dir, lexicon_csv)
    if path is None:
        if not allow_toy:
            raise RuntimeError("Please provide --lexicon-csv path to Loughran-McDonald MasterDictionary.")
        toy = pd.DataFrame(
            [
                {"term": "gain", "polarity": 1, "category": "positive"},
                {"term": "growth", "polarity": 1, "category": "positive"},
                {"term": "profit", "polarity": 1, "category": "positive"},
                {"term": "strong", "polarity": 1, "category": "positive"},
                {"term": "loss", "polarity": -1, "category": "negative"},
                {"term": "decline", "polarity": -1, "category": "negative"},
                {"term": "weak", "polarity": -1, "category": "negative"},
                {"term": "risk", "polarity": -1, "category": "negative"},
            ]
        )
        return LexiconBundle(toy, None, True, 0, 4, 4)

    raw = pd.read_csv(path)
    lower = {c.lower().strip(): c for c in raw.columns}
    word_col = lower.get("word")
    pos_col = lower.get("positive")
    neg_col = lower.get("negative")
    if word_col is None or pos_col is None or neg_col is None:
        raise RuntimeError(f"Lexicon CSV must contain Word, Positive, Negative columns: {path}")

    terms = raw[[word_col, pos_col, neg_col]].copy()
    terms["term"] = (
        terms[word_col].astype(str).str.lower().str.strip().str.replace(r"[^a-z]", "", regex=True)
    )
    terms = terms[terms["term"].str.fullmatch(r"[a-z]+", na=False)].copy()
    pos = pd.to_numeric(terms[pos_col], errors="coerce").fillna(0).ne(0)
    neg = pd.to_numeric(terms[neg_col], errors="coerce").fillna(0).ne(0)
    conflict = pos & neg
    conflict_count = int(conflict.sum())
    terms = terms[~conflict & (pos | neg)].copy()
    terms["polarity"] = np.where(pd.to_numeric(terms[pos_col], errors="coerce").fillna(0).ne(0), 1, -1)
    terms["category"] = np.where(terms["polarity"].eq(1), "positive", "negative")
    terms = terms[["term", "polarity", "category"]].drop_duplicates("term").sort_values("term")
    return LexiconBundle(
        terms=terms,
        source_path=path,
        is_toy=False,
        conflict_count=conflict_count,
        positive_count=int((terms["polarity"] == 1).sum()),
        negative_count=int((terms["polarity"] == -1).sum()),
    )


def tokenize(text: str) -> list[str]:
    if not isinstance(text, str):
        text = "" if pd.isna(text) else str(text)
    text = html.unescape(text).lower()
    text = URL_RE.sub(" ", text)
    return TOKEN_RE.findall(text)


def score_articles(news: pd.DataFrame, text_cols: list[str], lexicon: LexiconBundle, min_token_count: int) -> tuple[pd.DataFrame, dict]:
    df = news.copy()
    df["signal_date"] = pd.to_datetime(df["signal_date"], errors="coerce")
    if "publication_date" in df.columns:
        df["publication_date"] = pd.to_datetime(df["publication_date"], errors="coerce")
    else:
        df["publication_date"] = pd.NaT
    df["security_id"] = pd.to_numeric(df["security_id"], errors="coerce").astype("Int64")
    df = df.dropna(subset=["signal_date", "security_id"]).copy()
    df["security_id"] = df["security_id"].astype(int)

    for col in ["article_id", "language", "yahoo_ticker", "company", "country", "sector", "mapping_confidence"]:
        if col not in df.columns:
            df[col] = None
    text_frame = df[text_cols].fillna("").astype(str)
    df["text_used"] = text_frame.agg(" ".join, axis=1).str.strip()
    missing_article = df["article_id"].isna() | df["article_id"].astype(str).str.strip().eq("")
    if missing_article.any():
        hashes = (
            df.loc[missing_article, ["security_id", "signal_date", "text_used"]]
            .astype(str)
            .agg("|".join, axis=1)
            .map(lambda x: "synthetic_" + hashlib.sha1(x.encode("utf-8")).hexdigest()[:20])
        )
        df.loc[missing_article, "article_id"] = hashes
    df["article_id"] = df["article_id"].astype(str)

    before = len(df)
    df = df.drop_duplicates(["article_id", "security_id", "signal_date"]).copy()
    after = len(df)

    polarity = dict(zip(lexicon.terms["term"], lexicon.terms["polarity"]))
    records = []
    for row in df.itertuples(index=False):
        text = getattr(row, "text_used")
        tokens = tokenize(text)
        n_tokens = len(tokens)
        n_pos = 0
        n_neg = 0
        if n_tokens >= min_token_count:
            for token in tokens:
                pol = polarity.get(token)
                if pol == 1:
                    n_pos += 1
                elif pol == -1:
                    n_neg += 1
            usable = 1
            lex_score = (n_pos - n_neg) / n_tokens if n_tokens > 0 else 0.0
        else:
            usable = 0
            lex_score = 0.0
        weight_raw = pd.to_numeric(getattr(row, "mapping_confidence"), errors="coerce")
        weight = float(weight_raw) if pd.notna(weight_raw) and float(weight_raw) > 0 else 1.0
        records.append(
            {
                "article_id": getattr(row, "article_id"),
                "security_id": getattr(row, "security_id"),
                "date": getattr(row, "signal_date"),
                "publication_date": getattr(row, "publication_date"),
                "language": getattr(row, "language"),
                "yahoo_ticker": getattr(row, "yahoo_ticker"),
                "company": getattr(row, "company"),
                "country": getattr(row, "country"),
                "sector": getattr(row, "sector"),
                "text_used": text,
                "n_tokens": n_tokens,
                "n_pos": n_pos,
                "n_neg": n_neg,
                "n_lex_hits": n_pos + n_neg,
                "lex_score": lex_score if n_tokens >= min_token_count else 0.0,
                "mapping_confidence": weight_raw if pd.notna(weight_raw) else None,
                "weight": weight,
                "usable_text": usable,
            }
        )
    scored = pd.DataFrame(records)
    stats = {
        "news_rows_before_dedup": before,
        "news_rows_after_dedup": after,
        "news_duplicate_rows_removed": before - after,
        "synthetic_article_ids": int(missing_article.sum()),
        "mapping_weight_min": float(scored["weight"].min()) if not scored.empty else None,
        "mapping_weight_max": float(scored["weight"].max()) if not scored.empty else None,
    }
    return scored, stats


def aggregate_daily_lex(article_scores: pd.DataFrame) -> pd.DataFrame:
    if article_scores.empty:
        return pd.DataFrame()
    rows = []
    for key, df in article_scores.groupby(["date", "security_id"], observed=True):
        weights = pd.to_numeric(df["weight"], errors="coerce").fillna(1.0)
        denom = float(weights.sum())
        score = float(np.average(df["lex_score"], weights=weights)) if denom > 0 else float(df["lex_score"].mean())
        rows.append(
            {
                "date": key[0],
                "security_id": int(key[1]),
                "lex_score_raw": score,
                "n_articles": int(len(df)),
                "n_usable_articles": int(pd.to_numeric(df["usable_text"], errors="coerce").fillna(0).sum()),
                "n_tokens_sum": int(pd.to_numeric(df["n_tokens"], errors="coerce").fillna(0).sum()),
                "n_pos_sum": int(pd.to_numeric(df["n_pos"], errors="coerce").fillna(0).sum()),
                "n_neg_sum": int(pd.to_numeric(df["n_neg"], errors="coerce").fillna(0).sum()),
                "n_lex_hits_sum": int(pd.to_numeric(df["n_lex_hits"], errors="coerce").fillna(0).sum()),
                "avg_mapping_confidence": pd.to_numeric(df["mapping_confidence"], errors="coerce").mean(),
                "min_publication_date": df["publication_date"].min(),
                "max_publication_date": df["publication_date"].max(),
            }
        )
    return pd.DataFrame(rows).sort_values(["date", "security_id"])


def build_backtest_panel(price: pd.DataFrame, daily_lex: pd.DataFrame, common_ids: set[int]) -> pd.DataFrame:
    if not common_ids:
        raise RuntimeError("Common-News universe is empty.")

    base_cols = [
        "date",
        "security_id",
        "yahoo_ticker",
        "company",
        "country",
        "sector",
        "ret_fwd_1d",
        "z_s1_price_score",
        "z_s1_price_score_lag1",
    ]
    base = price[price["security_id"].isin(common_ids)][base_cols].copy()
    lex_cols = ["date", "security_id", "lex_score_raw", "n_articles"]
    neutral = base.merge(daily_lex[lex_cols], on=["date", "security_id"], how="left")
    neutral["lex_score_raw"] = pd.to_numeric(neutral["lex_score_raw"], errors="coerce").fillna(0.0)
    neutral["n_lex_articles"] = pd.to_numeric(neutral["n_articles"], errors="coerce").fillna(0).astype(int)
    neutral["has_lex_news"] = neutral["n_lex_articles"].gt(0).astype(int)
    neutral = neutral.drop(columns=["n_articles"])
    neutral["news_mode"] = "NEUTRAL_FILL"

    event = base.merge(daily_lex[lex_cols], on=["date", "security_id"], how="inner")
    event["lex_score_raw"] = pd.to_numeric(event["lex_score_raw"], errors="coerce").fillna(0.0)
    event["n_lex_articles"] = pd.to_numeric(event["n_articles"], errors="coerce").fillna(0).astype(int)
    event["has_lex_news"] = event["n_lex_articles"].gt(0).astype(int)
    event = event.drop(columns=["n_articles"])
    event["news_mode"] = "EVENT_ONLY"

    variant_frames = []
    for frame in [neutral, event]:
        if frame.empty:
            continue
        frame = frame.copy()
        frame["z_lex_score"] = frame.groupby("date", observed=True)["lex_score_raw"].transform(zscore_single_group)
        for time_logic in TIME_LOGICS:
            part = frame.copy()
            part["time_logic"] = time_logic
            if time_logic == "research_signal_date":
                part["s1_component"] = part["z_s1_price_score"]
            else:
                part["s1_component"] = part["z_s1_price_score_lag1"]
            part["lex_component"] = part["z_lex_score"].fillna(0.0)
            for model_family, strategy_variant, news_mode, weight_s1, weight_lex in VARIANT_CONFIGS:
                if news_mode != part["news_mode"].iloc[0]:
                    continue
                model = part.copy()
                model["model_family"] = model_family
                model["strategy_variant"] = strategy_variant
                model["weight_s1"] = weight_s1
                model["weight_lex"] = weight_lex
                if weight_s1 == 0.0:
                    model["score_used"] = model["lex_component"]
                elif weight_lex == 0.0:
                    model["score_used"] = model["s1_component"]
                else:
                    model["score_used"] = weight_s1 * model["s1_component"] + weight_lex * model["lex_component"]
                model["s2_score"] = model["score_used"]
                variant_frames.append(model)
    panel = pd.concat(variant_frames, ignore_index=True) if variant_frames else pd.DataFrame()
    keep = [
        "model_family",
        "strategy_variant",
        "news_mode",
        "time_logic",
        "date",
        "security_id",
        "yahoo_ticker",
        "company",
        "country",
        "sector",
        "ret_fwd_1d",
        "z_s1_price_score",
        "z_s1_price_score_lag1",
        "s1_component",
        "lex_score_raw",
        "z_lex_score",
        "lex_component",
        "n_lex_articles",
        "has_lex_news",
        "score_used",
        "s2_score",
        "weight_s1",
        "weight_lex",
    ]
    return panel[keep].sort_values(["model_family", "strategy_variant", "time_logic", "date", "security_id"])


def zscore_single_group(values: pd.Series) -> pd.Series:
    x = pd.to_numeric(values, errors="coerce")
    std = x.std(ddof=0)
    if pd.isna(std) or std == 0 or x.notna().sum() < 2:
        return pd.Series(0.0, index=values.index)
    return ((x - x.mean()) / std).fillna(0.0)


def add_equity_and_drawdown(df: pd.DataFrame, return_col: str, equity_col: str, drawdown_col: str) -> pd.DataFrame:
    out = df.sort_values("date").copy()
    if out.empty:
        out[equity_col] = pd.Series(dtype=float)
        out[drawdown_col] = pd.Series(dtype=float)
        return out
    out[equity_col] = (1.0 + pd.to_numeric(out[return_col], errors="coerce").fillna(0.0)).cumprod()
    running_max = out[equity_col].cummax()
    out[drawdown_col] = out[equity_col] / running_max - 1.0
    return out


def run_backtests(panel: pd.DataFrame, top_pct: float, coverage_threshold: float) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    positions_all = []
    daily_all = []
    coverage_all = []
    target_n = int(panel["security_id"].nunique()) if not panel.empty else 0
    for model_family, strategy_variant, news_mode, _, _ in VARIANT_CONFIGS:
        for time_logic in TIME_LOGICS:
            base = panel[
                panel["strategy_variant"].eq(strategy_variant) & panel["time_logic"].eq(time_logic)
            ].copy()
            candidates_all = base.dropna(subset=["date", "security_id", "score_used", "ret_fwd_1d"]).copy()
            for portfolio_variant in PORTFOLIO_VARIANTS:
                for coverage_filter in COVERAGE_FILTERS:
                    coverage = (
                        candidates_all.groupby("date", observed=True)
                        .agg(
                            n_candidates=("security_id", "nunique"),
                            share_candidates_with_news=("has_lex_news", "mean"),
                        )
                        .reset_index()
                    )
                    coverage["model_family"] = model_family
                    coverage["strategy_variant"] = strategy_variant
                    coverage["news_mode"] = news_mode
                    coverage["time_logic"] = time_logic
                    coverage["portfolio_variant"] = portfolio_variant
                    coverage["coverage_filter"] = coverage_filter
                    coverage["n_target_securities"] = target_n
                    coverage["coverage_ratio"] = np.where(target_n > 0, coverage["n_candidates"] / target_n, np.nan)
                    candidates = candidates_all.copy()
                    if coverage_filter == "coverage_80":
                        valid_dates = set(coverage.loc[coverage["coverage_ratio"] >= coverage_threshold, "date"])
                        candidates = candidates[candidates["date"].isin(valid_dates)].copy()
                        coverage = coverage[coverage["date"].isin(valid_dates)].copy()
                    selected_rows = []
                    for date, day in candidates.groupby("date", sort=True, observed=True):
                        day = day.sort_values(["score_used", "security_id"], ascending=[False, True]).copy()
                        n_candidates = len(day)
                        n_select = max(1, int(math.ceil(top_pct * n_candidates)))
                        selected = day.head(n_select).copy()
                        selected["rank"] = np.arange(1, len(selected) + 1)
                        selected["n_candidates"] = n_candidates
                        selected["n_positions"] = len(selected)
                        selected_rows.append(selected)
                    if not selected_rows:
                        coverage["n_positions"] = 0
                        coverage["share_positions_with_news"] = np.nan
                        coverage_all.append(coverage)
                        continue
                    positions = pd.concat(selected_rows, ignore_index=True)
                    positions["weight"] = 1.0 / positions["n_positions"]
                    positions["portfolio_variant"] = portfolio_variant
                    positions["coverage_filter"] = coverage_filter
                    pos_cols = [
                        "strategy_variant",
                        "model_family",
                        "news_mode",
                        "time_logic",
                        "portfolio_variant",
                        "coverage_filter",
                        "date",
                        "security_id",
                        "yahoo_ticker",
                        "company",
                        "country",
                        "sector",
                        "score_used",
                        "s2_score",
                        "s1_component",
                        "lex_component",
                        "lex_score_raw",
                        "n_lex_articles",
                        "has_lex_news",
                        "weight_s1",
                        "weight_lex",
                        "ret_fwd_1d",
                        "weight",
                        "rank",
                        "n_candidates",
                        "n_positions",
                    ]
                    positions = positions[pos_cols].copy()
                    daily = (
                        positions.assign(weighted_return=positions["weight"] * positions["ret_fwd_1d"])
                        .groupby(KEY_COLS + ["date"], observed=True)
                        .agg(
                            strategy_ret_fwd_1d=("weighted_return", "sum"),
                            n_positions=("security_id", "nunique"),
                            n_candidates=("n_candidates", "first"),
                            share_positions_with_news=("has_lex_news", "mean"),
                            avg_n_lex_articles_selected=("n_lex_articles", "mean"),
                            avg_score_used=("score_used", "mean"),
                            avg_s1_component=("s1_component", "mean"),
                            avg_lex_component=("lex_component", "mean"),
                        )
                        .reset_index()
                    )
                    daily["n_target_securities"] = target_n
                    daily["coverage_ratio"] = np.where(target_n > 0, daily["n_candidates"] / target_n, np.nan)
                    daily = add_equity_and_drawdown(daily, "strategy_ret_fwd_1d", "equity", "drawdown")
                    coverage = coverage.merge(
                        daily[KEY_COLS + ["date", "n_positions", "share_positions_with_news"]],
                        on=KEY_COLS + ["date"],
                        how="left",
                    )
                    positions_all.append(positions)
                    daily_all.append(daily)
                    coverage_all.append(coverage)
    positions = pd.concat(positions_all, ignore_index=True) if positions_all else pd.DataFrame()
    daily = pd.concat(daily_all, ignore_index=True) if daily_all else pd.DataFrame()
    coverage = pd.concat(coverage_all, ignore_index=True) if coverage_all else pd.DataFrame()
    return positions, daily, coverage


def compute_turnover_and_costs(daily_returns: pd.DataFrame, positions: pd.DataFrame, cost_bps: list[float]) -> tuple[pd.DataFrame, pd.DataFrame]:
    if daily_returns.empty or positions.empty:
        return daily_returns, pd.DataFrame()
    daily_parts = []
    net_metrics = []
    for key, pos in positions.groupby(KEY_COLS, observed=True):
        pos = pos.sort_values("date")
        wide = pos.pivot_table(index="date", columns="security_id", values="weight", aggfunc="sum", fill_value=0.0).sort_index()
        turnover_total = wide.diff().abs().sum(axis=1)
        if len(turnover_total):
            turnover_total.iloc[0] = 0.0
        turnover = pd.DataFrame(
            {"date": turnover_total.index, "turnover_total": turnover_total.values, "turnover_oneway": 0.5 * turnover_total.values}
        )
        mask = np.logical_and.reduce([daily_returns[col].eq(value) for col, value in zip(KEY_COLS, key)])
        part = daily_returns.loc[mask].merge(turnover, on="date", how="left")
        part["turnover_total"] = part["turnover_total"].fillna(0.0)
        part["turnover_oneway"] = part["turnover_oneway"].fillna(0.0)
        for bps in cost_bps:
            label = cost_label(bps)
            part[f"transaction_cost_{label}"] = part["turnover_total"] * bps / 10000.0
            part[f"net_return_{label}"] = part["strategy_ret_fwd_1d"] - part[f"transaction_cost_{label}"]
            part[f"net_equity_{label}"] = (1.0 + part[f"net_return_{label}"]).cumprod()
            running_max = part[f"net_equity_{label}"].cummax()
            part[f"net_drawdown_{label}"] = part[f"net_equity_{label}"] / running_max - 1.0
            net_metrics.append(
                {
                    **dict(zip(KEY_COLS, key)),
                    "cost_bps": bps,
                    **compute_return_metrics(part, f"net_return_{label}", f"net_equity_{label}", f"net_drawdown_{label}"),
                }
            )
        daily_parts.append(part)
    return pd.concat(daily_parts, ignore_index=True), pd.DataFrame(net_metrics)


def cost_label(bps: float) -> str:
    return f"{int(bps)}bps" if float(bps).is_integer() else f"{str(bps).replace('.', '_')}bps"


def compute_drawdown_recovery(df: pd.DataFrame, equity_col: str) -> dict:
    work = df.sort_values("date").copy()
    if work.empty or equity_col not in work:
        return {"max_drawdown_start_date": None, "max_drawdown_end_date": None, "max_drawdown_recovery_date": None, "max_drawdown_recovery_days": None}
    work["running_max"] = work[equity_col].cummax()
    work["drawdown_calc"] = work[equity_col] / work["running_max"] - 1.0
    end_idx = work["drawdown_calc"].idxmin()
    end_pos = int(work.index.get_loc(end_idx))
    end_date = work.loc[end_idx, "date"]
    peak_equity = work.loc[end_idx, "running_max"]
    prior = work.iloc[: end_pos + 1]
    peak_rows = prior[np.isclose(prior[equity_col], peak_equity)]
    start_date = peak_rows.iloc[-1]["date"] if not peak_rows.empty else None
    after = work.iloc[end_pos + 1 :]
    recovered = after[after[equity_col] >= peak_equity]
    recovery_date = recovered.iloc[0]["date"] if not recovered.empty else None
    recovery_days = int((work["date"] > end_date).loc[work["date"] <= recovery_date].sum()) if recovery_date is not None else None
    return {
        "max_drawdown_start_date": format_date_value(start_date),
        "max_drawdown_end_date": format_date_value(end_date),
        "max_drawdown_recovery_date": format_date_value(recovery_date),
        "max_drawdown_recovery_days": recovery_days,
    }


def compute_return_metrics(df: pd.DataFrame, return_col: str, equity_col: str, drawdown_col: str) -> dict:
    returns = pd.to_numeric(df[return_col], errors="coerce").dropna()
    n_days = len(returns)
    if n_days == 0:
        return empty_metric_dict()
    final_equity = float(df[equity_col].dropna().iloc[-1])
    std = float(returns.std(ddof=1)) if n_days > 1 else np.nan
    mean = float(returns.mean())
    ann_return = final_equity ** (252.0 / n_days) - 1.0
    max_dd = float(df[drawdown_col].min())
    calmar = ann_return / abs(max_dd) if max_dd < 0 else np.nan
    return {
        "n_days": n_days,
        "first_date": format_date_value(df["date"].min()),
        "last_date": format_date_value(df["date"].max()),
        "mean_daily_return": mean,
        "cumulative_return": final_equity - 1.0,
        "annualized_return_period_geometric": ann_return,
        "cagr": ann_return,
        "ann_vol": std * math.sqrt(252.0) if not np.isnan(std) else np.nan,
        "sharpe": mean / std * math.sqrt(252.0) if std and not np.isnan(std) else np.nan,
        "max_drawdown": max_dd,
        **compute_drawdown_recovery(df, equity_col),
        "calmar_ratio": calmar,
    }


def empty_metric_dict() -> dict:
    keys = [
        "n_days",
        "first_date",
        "last_date",
        "mean_daily_return",
        "cumulative_return",
        "annualized_return_period_geometric",
        "cagr",
        "ann_vol",
        "sharpe",
        "max_drawdown",
        "max_drawdown_start_date",
        "max_drawdown_end_date",
        "max_drawdown_recovery_date",
        "max_drawdown_recovery_days",
        "calmar_ratio",
    ]
    out = {k: None for k in keys}
    out["n_days"] = 0
    return out


def compute_metrics(daily: pd.DataFrame, rf: pd.DataFrame) -> pd.DataFrame:
    rows = []
    if daily.empty:
        return pd.DataFrame()
    for key, df in daily.groupby(KEY_COLS, observed=True):
        metrics = compute_return_metrics(df, "strategy_ret_fwd_1d", "equity", "drawdown")
        sharpe_excess, avg_rf, rf_source = compute_sharpe_excess(df, rf, "strategy_ret_fwd_1d")
        rows.append(
            {
                **dict(zip(KEY_COLS, key)),
                **metrics,
                "n_target_securities": df["n_target_securities"].max(),
                "avg_n_positions": df["n_positions"].mean(),
                "avg_n_candidates": df["n_candidates"].mean(),
                "avg_coverage_ratio": df["coverage_ratio"].mean(),
                "avg_turnover_total": df.get("turnover_total", pd.Series(dtype=float)).mean(),
                "avg_turnover_oneway": df.get("turnover_oneway", pd.Series(dtype=float)).mean(),
                "avg_share_positions_with_news": df["share_positions_with_news"].mean(),
                "avg_n_lex_articles_selected": df["avg_n_lex_articles_selected"].mean(),
                "sharpe_excess_estr": sharpe_excess,
                "avg_rf_daily": avg_rf,
                "rf_rate_source": rf_source,
                "sharpe_std_ddof": 1,
            }
        )
    return complete_metrics_grid(pd.DataFrame(rows), include_cost=False)


def compute_metrics_net(metrics_net: pd.DataFrame, daily: pd.DataFrame, rf: pd.DataFrame, cost_bps: list[float]) -> pd.DataFrame:
    rows = []
    if daily.empty:
        return pd.DataFrame()
    for key, df in daily.groupby(KEY_COLS, observed=True):
        for bps in cost_bps:
            label = cost_label(bps)
            ret_col = f"net_return_{label}"
            eq_col = f"net_equity_{label}"
            dd_col = f"net_drawdown_{label}"
            if ret_col not in df.columns:
                continue
            sharpe_excess, avg_rf, rf_source = compute_sharpe_excess(df, rf, ret_col)
            rows.append(
                {
                    **dict(zip(KEY_COLS, key)),
                    "cost_bps": bps,
                    **compute_return_metrics(df, ret_col, eq_col, dd_col),
                    "sharpe_excess_estr": sharpe_excess,
                    "avg_rf_daily": avg_rf,
                    "rf_rate_source": rf_source,
                    "sharpe_std_ddof": 1,
                }
            )
    return complete_metrics_grid(pd.DataFrame(rows), include_cost=True, cost_bps=cost_bps)


def complete_metrics_grid(df: pd.DataFrame, include_cost: bool, cost_bps: list[float] | None = None) -> pd.DataFrame:
    rows = []
    costs = cost_bps or [None]
    existing = set()
    if not df.empty:
        key_cols = KEY_COLS + (["cost_bps"] if include_cost else [])
        existing = set(tuple(row) for row in df[key_cols].itertuples(index=False, name=None))
    for model_family, strategy_variant, news_mode, _, _ in VARIANT_CONFIGS:
        for time_logic in TIME_LOGICS:
            for portfolio_variant in PORTFOLIO_VARIANTS:
                for coverage_filter in COVERAGE_FILTERS:
                    for cost in costs:
                        key = (model_family, strategy_variant, news_mode, time_logic, portfolio_variant, coverage_filter)
                        full_key = key + ((cost,) if include_cost else tuple())
                        if full_key in existing:
                            continue
                        row = {
                            "model_family": model_family,
                            "strategy_variant": strategy_variant,
                            "news_mode": news_mode,
                            "time_logic": time_logic,
                            "portfolio_variant": portfolio_variant,
                            "coverage_filter": coverage_filter,
                            **empty_metric_dict(),
                        }
                        if include_cost:
                            row["cost_bps"] = cost
                        row.update(
                            {
                                "avg_n_positions": None,
                                "avg_n_candidates": None,
                                "avg_coverage_ratio": None,
                                "avg_turnover_total": None,
                                "avg_turnover_oneway": None,
                                "avg_share_positions_with_news": None,
                                "avg_n_lex_articles_selected": None,
                                "sharpe_excess_estr": None,
                                "avg_rf_daily": None,
                                "rf_rate_source": None,
                                "sharpe_std_ddof": 1,
                            }
                        )
                        if include_cost:
                            for extra in [
                                "avg_n_positions",
                                "avg_n_candidates",
                                "avg_coverage_ratio",
                                "avg_turnover_total",
                                "avg_turnover_oneway",
                                "avg_share_positions_with_news",
                                "avg_n_lex_articles_selected",
                            ]:
                                row.pop(extra, None)
                        rows.append(row)
    if rows:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            df = pd.concat([df, pd.DataFrame(rows)], ignore_index=True) if not df.empty else pd.DataFrame(rows)
    sort_cols = KEY_COLS + (["cost_bps"] if include_cost else [])
    return df.sort_values(sort_cols).reset_index(drop=True) if not df.empty else df


def load_risk_free(input_db: Path, s1_db: Path | None) -> pd.DataFrame:
    for db in [s1_db, input_db]:
        if db is None or not db.exists():
            continue
        with sqlite3.connect(db) as conn:
            if "risk_free_rate_backtest_aligned" not in sqlite_tables(conn):
                continue
            rf = pd.read_sql_query("SELECT * FROM risk_free_rate_backtest_aligned", conn)
            if {"date", "estr_daily_decimal"}.issubset(rf.columns):
                rf["date"] = pd.to_datetime(rf["date"], errors="coerce")
                rf["estr_daily_decimal"] = pd.to_numeric(rf["estr_daily_decimal"], errors="coerce")
                rf["rf_source"] = str(db)
                return rf[["date", "estr_daily_decimal", "rf_source"]].dropna(subset=["date", "estr_daily_decimal"])
    return pd.DataFrame()


def compute_sharpe_excess(df: pd.DataFrame, rf: pd.DataFrame, return_col: str) -> tuple[float | None, float | None, str | None]:
    if rf.empty:
        return None, None, None
    merged = df[["date", return_col]].merge(rf, on="date", how="inner")
    if len(merged) < 2:
        return None, None, None
    excess = pd.to_numeric(merged[return_col], errors="coerce") - merged["estr_daily_decimal"]
    std = excess.std(ddof=1)
    val = excess.mean() / std * math.sqrt(252.0) if std and not np.isnan(std) else None
    return (float(val) if val is not None else None, float(merged["estr_daily_decimal"].mean()), merged["rf_source"].iloc[0])


def compute_benchmark_returns(benchmark_prices: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    notes = []
    if benchmark_prices.empty:
        return pd.DataFrame(), ["Benchmark tables missing."]
    df = benchmark_prices.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["adjusted_close"] = pd.to_numeric(df["adjusted_close"], errors="coerce")
    if "is_preferred" in df.columns:
        preferred = df[pd.to_numeric(df["is_preferred"], errors="coerce").fillna(0).eq(1)]
        if not preferred.empty:
            df = preferred.copy()
            notes.append("Preferred benchmark instrument selected via is_preferred.")
    df = df.dropna(subset=["symbol", "date", "adjusted_close"]).sort_values(["symbol", "date"])
    df["benchmark_ret_fwd_1d"] = df.groupby("symbol", observed=True)["adjusted_close"].shift(-1) / df["adjusted_close"] - 1.0
    notes.append("Benchmark returns use adjusted_close(t+1) / adjusted_close(t) - 1 on matched days only.")
    return df.dropna(subset=["benchmark_ret_fwd_1d"])[["symbol", "date", "benchmark_ret_fwd_1d"]], notes


def compute_benchmark_comparison(daily: pd.DataFrame, benchmark_prices: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    bench, notes = compute_benchmark_returns(benchmark_prices)
    if daily.empty or bench.empty:
        return pd.DataFrame(), pd.DataFrame(), notes
    rows = []
    metrics = []
    for key, strat in daily.groupby(KEY_COLS, observed=True):
        merged = strat.merge(bench, on="date", how="inner")
        if merged.empty:
            continue
        merged["strategy_return"] = merged["strategy_ret_fwd_1d"]
        merged["excess_return"] = merged["strategy_return"] - merged["benchmark_ret_fwd_1d"]
        merged["strategy_equity_matched"] = (1.0 + merged["strategy_return"]).cumprod()
        merged["benchmark_equity_matched"] = (1.0 + merged["benchmark_ret_fwd_1d"]).cumprod()
        rows.append(merged)
        excess_std = merged["excess_return"].std(ddof=1)
        metrics.append(
            {
                **dict(zip(KEY_COLS, key)),
                "n_matched_days": len(merged),
                "benchmark_symbol": ",".join(sorted(merged["symbol"].dropna().unique())),
                "strategy_cum_return_matched": merged["strategy_equity_matched"].iloc[-1] - 1.0,
                "benchmark_cum_return_matched": merged["benchmark_equity_matched"].iloc[-1] - 1.0,
                "active_cum_return": merged["strategy_equity_matched"].iloc[-1] / merged["benchmark_equity_matched"].iloc[-1] - 1.0,
                "mean_daily_excess_return": merged["excess_return"].mean(),
                "tracking_error": excess_std * math.sqrt(252.0) if excess_std and not np.isnan(excess_std) else np.nan,
                "information_ratio": merged["excess_return"].mean() / excess_std * math.sqrt(252.0) if excess_std and not np.isnan(excess_std) else np.nan,
            }
        )
    return (pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(), pd.DataFrame(metrics), notes)


def compute_newey_west_t_stat(values: pd.Series | np.ndarray) -> tuple[float | None, int | None]:
    x = pd.Series(values, dtype="float64").dropna().to_numpy()
    n = len(x)
    if n < 5:
        return None, None
    lag = min(max(1, int(math.floor(4.0 * (n / 100.0) ** (2.0 / 9.0)))), n - 1)
    try:
        import statsmodels.api as sm  # type: ignore

        model = sm.OLS(x, np.ones((n, 1))).fit(cov_type="HAC", cov_kwds={"maxlags": lag})
        return float(model.tvalues[0]), lag
    except Exception:
        pass
    demeaned = x - float(np.mean(x))
    gamma0 = float(np.sum(demeaned * demeaned) / n)
    nw_variance = gamma0
    for k in range(1, lag + 1):
        gamma_k = float(np.sum(demeaned[k:] * demeaned[:-k]) / n)
        nw_variance += 2.0 * (1.0 - k / (lag + 1.0)) * gamma_k
    if nw_variance <= 0 or not math.isfinite(nw_variance):
        return None, lag
    se = math.sqrt(nw_variance / n)
    return (float(np.mean(x) / se), lag) if se > 0 else (None, lag)


def compute_signal_diagnostics(panel: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    ic_rows = []
    q_rows = []
    for key, data in panel.dropna(subset=["score_used", "ret_fwd_1d"]).groupby(["model_family", "strategy_variant", "news_mode", "time_logic"], observed=True):
        for date, day in data.groupby("date", sort=True, observed=True):
            if len(day) >= 3:
                for signal_name, col in [
                    ("score_used", "score_used"),
                    ("lex_component", "lex_component"),
                    ("s1_component", "s1_component"),
                ]:
                    valid = day[[col, "ret_fwd_1d"]].dropna()
                    if len(valid) >= 3 and valid[col].nunique() > 1 and valid["ret_fwd_1d"].nunique() > 1:
                        ic_rows.append(
                            {
                                "model_family": key[0],
                                "strategy_variant": key[1],
                                "news_mode": key[2],
                                "time_logic": key[3],
                                "signal_name": signal_name,
                                "date": date,
                                "n_candidates": len(day),
                                "spearman_ic": valid[col].corr(valid["ret_fwd_1d"], method="spearman"),
                            }
                        )
            if len(day) >= 5:
                for signal_name, col in [("score_used", "score_used"), ("lex_component", "lex_component")]:
                    if day[col].nunique() < 2:
                        continue
                    ranked = day.sort_values([col, "security_id"], ascending=[True, True]).copy()
                    ranked["score_quintile"] = pd.qcut(ranked[col].rank(method="first"), 5, labels=[1, 2, 3, 4, 5]).astype(int)
                    agg = (
                        ranked.groupby("score_quintile", observed=True)
                        .agg(mean_ret_fwd_1d=("ret_fwd_1d", "mean"), n=("security_id", "nunique"))
                        .reset_index()
                    )
                    low = agg.loc[agg["score_quintile"].eq(1), "mean_ret_fwd_1d"]
                    high = agg.loc[agg["score_quintile"].eq(5), "mean_ret_fwd_1d"]
                    if not low.empty and not high.empty:
                        q_rows.append(
                            {
                                "model_family": key[0],
                                "strategy_variant": key[1],
                                "news_mode": key[2],
                                "time_logic": key[3],
                                "signal_name": signal_name,
                                "date": date,
                                "high_ret_1d": float(high.iloc[0]),
                                "low_ret_1d": float(low.iloc[0]),
                                "spread_ret_1d": float(high.iloc[0] - low.iloc[0]),
                                "notes": "Hypothetical long-short signal-quality diagnostic, not the traded long-only strategy.",
                            }
                        )
    ic_by_date = pd.DataFrame(ic_rows)
    summary = []
    if not ic_by_date.empty:
        for key, df in ic_by_date.groupby(["model_family", "strategy_variant", "news_mode", "time_logic", "signal_name"], observed=True):
            ic = df["spearman_ic"].dropna()
            nw_t, lag = compute_newey_west_t_stat(ic)
            std = ic.std(ddof=1)
            summary.append(
                {
                    "model_family": key[0],
                    "strategy_variant": key[1],
                    "news_mode": key[2],
                    "time_logic": key[3],
                    "signal_name": key[4],
                    "mean_ic": ic.mean(),
                    "median_ic": ic.median(),
                    "positive_ic_share": ic.gt(0).mean(),
                    "ic_t_stat_naive": ic.mean() / std * math.sqrt(len(ic)) if std and not np.isnan(std) else np.nan,
                    "ic_t_stat_newey_west": nw_t,
                    "newey_west_lag": lag,
                    "n_ic_days": len(ic),
                }
            )
    quintile = pd.DataFrame(q_rows)
    q_metrics = []
    if not quintile.empty:
        for key, df in quintile.groupby(["model_family", "strategy_variant", "news_mode", "time_logic", "signal_name"], observed=True):
            ret = df["spread_ret_1d"].dropna()
            q_metrics.append(
                {
                    "model_family": key[0],
                    "strategy_variant": key[1],
                    "news_mode": key[2],
                    "time_logic": key[3],
                    "signal_name": key[4],
                    "n_days": len(ret),
                    "mean_daily_spread": ret.mean(),
                    "cumulative_spread": (1.0 + ret).prod() - 1.0,
                    "spread_t_stat_naive": ret.mean() / ret.std(ddof=1) * math.sqrt(len(ret)) if len(ret) > 1 and ret.std(ddof=1) else np.nan,
                    "notes": "Hypothetical long-short signal-quality diagnostic, not the traded long-only strategy.",
                }
            )
    return ic_by_date, pd.DataFrame(summary), quintile, pd.DataFrame(q_metrics)


def compute_exposures(positions: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if positions.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    pos = positions.copy()
    sector = (
        pos.assign(sector=pos["sector"].fillna("Unknown"))
        .groupby(KEY_COLS + ["sector"], observed=True)
        .agg(avg_weight=("weight", "mean"), selected_days=("date", "nunique"))
        .reset_index()
    )
    country = (
        pos.assign(country=pos["country"].fillna("Unknown"))
        .groupby(KEY_COLS + ["country"], observed=True)
        .agg(avg_weight=("weight", "mean"), selected_days=("date", "nunique"))
        .reset_index()
    )
    top = (
        pos.groupby(KEY_COLS + ["security_id", "yahoo_ticker", "company", "country", "sector"], dropna=False, observed=True)
        .agg(selected_days=("date", "nunique"), avg_weight=("weight", "mean"), avg_score_used=("score_used", "mean"))
        .reset_index()
        .sort_values(KEY_COLS + ["selected_days"], ascending=[True, True, True, True, True, True, False])
    )
    top = top.groupby(KEY_COLS, observed=True).head(20).reset_index(drop=True)
    return sector, country, top


def compare_with_s1(daily: pd.DataFrame, s1_db: Path | None, threshold: float) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    if s1_db is None or not s1_db.exists():
        return pd.DataFrame(), pd.DataFrame(), "S1 DB missing."
    with sqlite3.connect(s1_db) as conn:
        tables = sqlite_tables(conn)
        if "s1_daily_returns" not in tables:
            return pd.DataFrame(), pd.DataFrame(), "s1_daily_returns missing in S1 DB."
        s1 = pd.read_sql_query("SELECT * FROM s1_daily_returns", conn)
    if s1.empty:
        return pd.DataFrame(), pd.DataFrame(), "s1_daily_returns empty."
    expected_universe = common_news_universe_name(threshold)
    if "universe" not in s1.columns:
        return pd.DataFrame(), pd.DataFrame(), "S1 results have no universe column; comparison skipped."
    common = s1[s1["universe"].eq(expected_universe)].copy()
    if common.empty:
        return pd.DataFrame(), pd.DataFrame(), f"Expected S1 universe {expected_universe} not found; comparison skipped to avoid universe mismatch."
    s1 = common
    s1["date"] = pd.to_datetime(s1["date"], errors="coerce")
    daily = daily.copy()
    daily["date"] = pd.to_datetime(daily["date"], errors="coerce")
    rows = []
    metrics = []
    for key, s2_part in daily.groupby(KEY_COLS, observed=True):
        model_family, s2_variant, news_mode, time_logic, portfolio_variant, coverage_filter = key
        s1_time = "research_close_to_close" if time_logic == "research_signal_date" else "conservative_lag1"
        s1_part = s1.copy()
        for col, val in [("time_logic", s1_time), ("portfolio_variant", portfolio_variant), ("coverage_filter", coverage_filter)]:
            if col in s1_part.columns:
                s1_part = s1_part[s1_part[col].eq(val)]
        if s1_part.empty:
            continue
        merged = s2_part.merge(
            s1_part[["date", "strategy_ret_fwd_1d"]].rename(columns={"strategy_ret_fwd_1d": "s1_return"}),
            on="date",
            how="inner",
        )
        if merged.empty:
            continue
        merged["s2_return"] = merged["strategy_ret_fwd_1d"]
        merged["return_diff"] = merged["s2_return"] - merged["s1_return"]
        merged["s2_equity_matched"] = (1.0 + merged["s2_return"]).cumprod()
        merged["s1_equity_matched"] = (1.0 + merged["s1_return"]).cumprod()
        rows.append(merged)
        diff_std = merged["return_diff"].std(ddof=1)
        metrics.append(
            {
                **dict(zip(KEY_COLS, key)),
                "s1_universe": expected_universe,
                "n_matched_days": len(merged),
                "s2_cum_return_matched": merged["s2_equity_matched"].iloc[-1] - 1.0,
                "s1_cum_return_matched": merged["s1_equity_matched"].iloc[-1] - 1.0,
                "active_cum_return_vs_s1": merged["s2_equity_matched"].iloc[-1] / merged["s1_equity_matched"].iloc[-1] - 1.0,
                "mean_daily_diff": merged["return_diff"].mean(),
                "tracking_error_vs_s1": diff_std * math.sqrt(252.0) if diff_std and not np.isnan(diff_std) else np.nan,
                "information_ratio_vs_s1": merged["return_diff"].mean() / diff_std * math.sqrt(252.0) if diff_std and not np.isnan(diff_std) else np.nan,
            }
        )
    return (pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(), pd.DataFrame(metrics), f"S1 comparison uses {expected_universe} on matched dates only.")


def build_metric_definitions() -> pd.DataFrame:
    rows = [
        ("score_used", "Ranking score used by the selected model family.", "Lexicon-only uses lex_component; Hybrid uses 0.5*s1_component + 0.5*lex_component."),
        ("S2_LEXICON_ONLY", "score_used = lex_component.", "Pure Loughran-McDonald lexicon model."),
        ("HYBRID_S1_LEX_50_50", "score_used = 0.5*s1_component + 0.5*lex_component.", "Late fusion; 50/50 weights are ex ante and not optimized."),
        ("lex_score", "(n_pos - n_neg) / n_tokens.", "Article-level Loughran-McDonald dictionary score."),
        ("lex_score_raw", "Weighted average of article lex_score by security_id and signal_date.", "mapping_confidence used as weight when numeric and positive."),
        ("z_lex_score", "Cross-sectional z-score of lex_score_raw by date and strategy variant.", "Set to 0 when cross-sectional std is 0 or too few values."),
        ("strategy_ret_fwd_1d", "Equal-weighted average of selected ret_fwd_1d.", "Long-only Top 20 percent portfolio."),
        ("turnover_total", "sum(abs(w_t - w_{t-1})) across securities.", "First day turnover is 0."),
        ("transaction_cost", "turnover_total * cost_bps / 10000.", "Applied to daily gross returns."),
        ("sharpe", "mean(daily_return) / std(daily_return, ddof=1) * sqrt(252).", "NaN if std is zero."),
        ("quintile_spread", "Mean return of highest S2 quintile minus lowest S2 quintile.", "Diagnostic only; not traded."),
    ]
    return pd.DataFrame(rows, columns=["metric", "definition", "notes"])


def build_model_comparison_summary(metrics: pd.DataFrame, metrics_net: pd.DataFrame, s1_metrics: pd.DataFrame) -> pd.DataFrame:
    if metrics.empty:
        return pd.DataFrame()
    cols = [
        "model_family",
        "strategy_variant",
        "time_logic",
        "coverage_filter",
        "n_target_securities",
        "n_days",
        "avg_n_positions",
        "avg_coverage_ratio",
        "avg_share_positions_with_news",
        "cumulative_return",
        "annualized_return_period_geometric",
        "sharpe",
        "sharpe_excess_estr",
        "max_drawdown",
        "avg_turnover_total",
    ]
    out = metrics.copy()
    if not metrics_net.empty:
        net10 = metrics_net[pd.to_numeric(metrics_net["cost_bps"], errors="coerce").eq(10.0)].copy()
        net_cols = KEY_COLS + ["cumulative_return", "annualized_return_period_geometric", "sharpe", "max_drawdown"]
        net10 = net10[[c for c in net_cols if c in net10.columns]].rename(
            columns={
                "cumulative_return": "net10_cumulative_return",
                "annualized_return_period_geometric": "net10_annualized_return_period_geometric",
                "sharpe": "net10_sharpe",
                "max_drawdown": "net10_max_drawdown",
            }
        )
        out = out.merge(net10, on=KEY_COLS, how="left")
    if not s1_metrics.empty:
        out = out.merge(
            s1_metrics[KEY_COLS + ["active_cum_return_vs_s1", "information_ratio_vs_s1"]],
            on=KEY_COLS,
            how="left",
        )
    report_cols = cols + [
        "net10_cumulative_return",
        "net10_annualized_return_period_geometric",
        "active_cum_return_vs_s1",
        "information_ratio_vs_s1",
    ]
    return out[[c for c in report_cols if c in out.columns]].sort_values(
        [c for c in ["model_family", "strategy_variant", "time_logic", "coverage_filter"] if c in out.columns]
    )


def validation_row(rows: list[dict], name: str, status: str, detail: str) -> None:
    rows.append({"check_name": name, "status": status, "detail": detail})


def run_validations(
    bundle: InputBundle,
    lexicon: LexiconBundle,
    article_scores: pd.DataFrame,
    daily_lex: pd.DataFrame,
    panel: pd.DataFrame,
    positions: pd.DataFrame,
    daily: pd.DataFrame,
    benchmark_daily: pd.DataFrame,
    s1_metrics: pd.DataFrame,
    report_path: Path,
    figures_dir: Path,
    output_db: Path,
    args: argparse.Namespace,
) -> pd.DataFrame:
    rows: list[dict] = []
    validation_row(rows, "input_db exists", "PASS" if bundle.input_db.exists() else "FAIL", str(bundle.input_db))
    validation_row(rows, "news_model_input exists", "PASS", f"rows={len(bundle.news):,}")
    validation_row(rows, "signal_date exists and used as date", "PASS", "Article and daily lex dates are derived from signal_date.")
    validation_row(rows, "publication_date not used as portfolio date", "PASS", "publication_date is stored only for reporting/validation.")
    validation_row(
        rows,
        "lexicon loaded and not toy lexicon unless explicitly allowed",
        "WARN" if lexicon.is_toy else "PASS",
        "Toy lexicon used - results not thesis-grade." if lexicon.is_toy else str(lexicon.source_path),
    )
    art_dups = int(article_scores.duplicated(["article_id", "security_id", "date"]).sum()) if not article_scores.empty else 0
    validation_row(rows, "article scores have no duplicate article_id-security_id-date", "PASS" if art_dups == 0 else "FAIL", f"duplicates={art_dups}")
    daily_dups = int(daily_lex.duplicated(["security_id", "date"]).sum()) if not daily_lex.empty else 0
    validation_row(rows, "daily lex scores have no duplicate security_id-date", "PASS" if daily_dups == 0 else "FAIL", f"duplicates={daily_dups}")
    source_used = bundle.common_news_security_coverage["universe_source"].iloc[0] if not bundle.common_news_security_coverage.empty else bundle.common_universe_source
    validation_row(rows, "common_universe_source_used", "PASS" if source_used == bundle.common_universe_source or bundle.common_universe_source == "auto" else "FAIL", f"requested={bundle.common_universe_source}; used={source_used}")
    validation_row(rows, "Common-News-Universum nicht leer", "PASS" if not panel.empty else "FAIL", f"panel_rows={len(panel):,}")
    nf = panel[panel["news_mode"].eq("NEUTRAL_FILL")]
    missing_ok = bool(((nf.loc[nf["has_lex_news"].eq(0), "lex_score_raw"] == 0).all())) if not nf.empty else False
    validation_row(rows, "Neutral-Fill: missing news rows have lex_score_raw = 0 and has_lex_news = 0", "PASS" if missing_ok else "FAIL", f"neutral_rows={len(nf):,}")
    ev = panel[panel["news_mode"].eq("EVENT_ONLY")]
    validation_row(rows, "Event-Only: alle Zeilen haben has_lex_news = 1", "PASS" if not ev.empty and ev["has_lex_news"].eq(1).all() else "WARN", f"event_rows={len(ev):,}")
    validation_row(rows, "z_lex_score has no NaN after fill", "PASS" if panel["z_lex_score"].notna().all() else "FAIL", f"nan={panel['z_lex_score'].isna().sum()}")
    lex_only = panel[panel["model_family"].eq("S2_LEXICON_ONLY")]
    lex_weight_ok = lex_only.empty or (lex_only["weight_s1"].eq(0.0).all() and lex_only["weight_lex"].eq(1.0).all())
    lex_score_ok = lex_only.empty or np.allclose(lex_only["score_used"], lex_only["lex_component"], atol=1e-12, equal_nan=True)
    validation_row(rows, "S2_LEX_ONLY_* hat weight_s1 = 0.0, weight_lex = 1.0", "PASS" if lex_weight_ok else "FAIL", f"rows={len(lex_only):,}")
    validation_row(rows, "S2_LEX_ONLY_* score_used == lex_component", "PASS" if lex_score_ok else "FAIL", "tolerance=1e-12")
    hybrid = panel[panel["model_family"].eq("HYBRID_S1_LEX_50_50")]
    hybrid_weight_ok = hybrid.empty or (hybrid["weight_s1"].eq(0.5).all() and hybrid["weight_lex"].eq(0.5).all())
    hybrid_score_ok = hybrid.empty or np.allclose(hybrid["score_used"], 0.5 * hybrid["s1_component"] + 0.5 * hybrid["lex_component"], atol=1e-12, equal_nan=True)
    validation_row(rows, "HYBRID_* hat weight_s1 = 0.5, weight_lex = 0.5", "PASS" if hybrid_weight_ok else "FAIL", f"rows={len(hybrid):,}")
    validation_row(rows, "HYBRID_* score_used == 0.5*s1_component + 0.5*lex_component", "PASS" if hybrid_score_ok else "FAIL", "tolerance=1e-12")
    selected_candidates = positions.dropna(subset=["score_used"]) if not positions.empty else positions
    validation_row(rows, "score_used has no NaN in selected candidates", "PASS" if len(selected_candidates) == len(positions) else "FAIL", f"positions={len(positions):,}")
    if positions.empty:
        validation_row(rows, "weights sum to 1 per day within tolerance 1e-9", "FAIL", "No positions.")
    else:
        sums = positions.groupby(KEY_COLS + ["date"], observed=True)["weight"].sum()
        bad = int((sums.sub(1.0).abs() > 1e-9).sum())
        validation_row(rows, "weights sum to 1 per day within tolerance 1e-9", "PASS" if bad == 0 else "FAIL", f"bad_days={bad}")
    if positions.empty:
        validation_row(rows, "Top-20 selection count equals ceil(top_pct*n_candidates)", "FAIL", "No positions.")
    else:
        check = positions.groupby(KEY_COLS + ["date"], observed=True).agg(n_positions=("security_id", "nunique"), n_candidates=("n_candidates", "first"))
        expected = np.ceil(args.top_pct * check["n_candidates"]).clip(lower=1).astype(int)
        bad = int((check["n_positions"] != expected).sum())
        validation_row(rows, "Top-20 selection count equals ceil(top_pct*n_candidates)", "PASS" if bad == 0 else "FAIL", f"bad_days={bad}")
    if positions.empty or daily.empty:
        validation_row(rows, "daily_return equals sum(weight*ret_fwd_1d)", "FAIL", "Missing positions or daily returns.")
    else:
        calc = positions.assign(calc=positions["weight"] * positions["ret_fwd_1d"]).groupby(KEY_COLS + ["date"], observed=True)["calc"].sum().reset_index()
        merged = daily.merge(calc, on=KEY_COLS + ["date"], how="left")
        bad = int((merged["strategy_ret_fwd_1d"].sub(merged["calc"]).abs() > 1e-12).sum())
        validation_row(rows, "daily_return equals sum(weight*ret_fwd_1d)", "PASS" if bad == 0 else "FAIL", f"bad_days={bad}")
    if daily.empty:
        validation_row(rows, "cumulative equity equals cumprod(1+return)", "FAIL", "No daily returns.")
    else:
        bad = 0
        for _, df in daily.groupby(KEY_COLS, observed=True):
            calc = (1.0 + df.sort_values("date")["strategy_ret_fwd_1d"]).cumprod()
            bad += int((calc.reset_index(drop=True).sub(df.sort_values("date")["equity"].reset_index(drop=True)).abs() > 1e-12).sum())
        validation_row(rows, "cumulative equity equals cumprod(1+return)", "PASS" if bad == 0 else "FAIL", f"bad_rows={bad}")
    validation_row(rows, "turnover non-negative", "PASS" if daily.empty or daily["turnover_total"].dropna().ge(0).all() else "FAIL", "turnover_total checked.")
    cov80 = daily[daily["coverage_filter"].eq("coverage_80")]
    cov_ok = cov80.empty or cov80["coverage_ratio"].ge(args.coverage_threshold - 1e-12).all()
    validation_row(rows, "coverage_80 only includes days above threshold", "PASS" if cov_ok else "FAIL", f"threshold={args.coverage_threshold}")
    validation_row(rows, "benchmark metrics only use matched days", "WARN" if benchmark_daily.empty else "PASS", f"matched_rows={len(benchmark_daily):,}")
    validation_row(rows, "share_positions_with_news is computed", "PASS" if "share_positions_with_news" in daily.columns and daily["share_positions_with_news"].notna().any() else "FAIL", "daily column checked.")
    distinct = {str(bundle.input_db.resolve())}
    if bundle.input_s1_results_db is not None:
        distinct.add(str(bundle.input_s1_results_db.resolve()))
    validation_row(rows, "output_db does not overwrite input_db or input_s1_results_db", "PASS" if str(output_db.resolve()) not in distinct else "FAIL", str(output_db))
    validation_row(rows, "report file written", "PASS" if report_path.exists() else "FAIL", str(report_path))
    figures = ["s2_equity_curves.png", "s2_drawdowns.png", "s2_coverage.png", "s2_share_positions_with_news.png", "s2_quintile_spread.png"]
    missing_figs = [f for f in figures if not (figures_dir / f).exists()]
    validation_row(rows, "figures written", "PASS" if not missing_figs else "WARN", f"missing={missing_figs}")
    pk_bad = []
    for name, df, subset in [
        ("s2_lexicon_article_scores", article_scores, ["article_id", "security_id", "date"]),
        ("s2_lexicon_daily_scores", daily_lex, ["security_id", "date"]),
        ("s2_positions", positions, KEY_COLS + ["date", "security_id"]),
        ("s2_daily_returns", daily, KEY_COLS + ["date"]),
    ]:
        if not df.empty and int(df.duplicated(subset).sum()) > 0:
            pk_bad.append(name)
    validation_row(rows, "no duplicate primary keys in output tables", "PASS" if not pk_bad else "FAIL", ", ".join(pk_bad))
    if bundle.benchmark_prices.empty:
        validation_row(rows, "optional benchmark available", "WARN", "Benchmark tables missing; benchmark outputs are empty.")
    if bundle.input_s1_results_db is None or s1_metrics.empty:
        validation_row(rows, "optional S1 comparison available", "WARN", "S1 comparison unavailable or ambiguous.")
    else:
        validation_row(rows, "S2/HYBRID universe matches expected S1 common news universe if S1 DB is available", "PASS", common_news_universe_name(args.security_news_coverage_threshold))
    report_text = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
    deprecated_s2_hybrid_phrase = "S2 kombiniert S1 Price-only und lexikonbasiertes"
    validation_row(
        rows,
        "Report enthält nicht mehr die falsche Aussage, dass S2 selbst der 50/50-Hybrid ist",
        "PASS" if deprecated_s2_hybrid_phrase not in report_text else "FAIL",
        "Deprecated S2-as-hybrid wording absent from report.",
    )
    if lexicon.is_toy:
        validation_row(rows, "Toy lexicon warning", "WARN", "Toy lexicon used - results not thesis-grade.")
    return pd.DataFrame(rows)


def dataframe_to_markdown(df: pd.DataFrame, max_rows: int = 20) -> str:
    if df.empty:
        return "_Keine Daten._"
    return df.head(max_rows).to_markdown(index=False)


def write_report(path: Path, outputs: dict[str, pd.DataFrame], bundle: InputBundle, lexicon: LexiconBundle, article_stats: dict, benchmark_notes: list[str], s1_note: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    metrics = outputs.get("s2_metrics", pd.DataFrame())
    metrics_net = outputs.get("s2_metrics_net", pd.DataFrame())
    validations = outputs.get("s2_validation_checks", pd.DataFrame())
    coverage = outputs.get("s2_coverage_by_date", pd.DataFrame())
    common_cov = outputs.get("s2_common_news_security_coverage", pd.DataFrame())
    comparison = outputs.get("s2_model_comparison_summary", pd.DataFrame())
    lines = [
        "# S2 Lexicon Backtest Report",
        "",
        "## 1. Executive Summary",
        "S2_LEXICON_ONLY ist das reine lexikonbasierte Einzelmodell auf Basis des Loughran-McDonald-Woerterbuchs. HYBRID_S1_LEX_50_50 ist die Late-Fusion-Kombination aus S1_PRICE_ONLY und Lexikon-Sentiment mit ex ante gesetzter 50/50-Gewichtung.",
        "NEUTRAL_FILL ist die Hauptvariante fuer investierbare taegliche Portfolios. EVENT_ONLY ist Diagnostik zur Signalgüte an Tagen mit News.",
        "",
        "## 2. Inputdaten und erkannte Tabellen",
        f"- Input DB: `{bundle.input_db}`",
        f"- Price table: `{bundle.price_table}`",
        f"- News rows: {len(bundle.news):,}",
        f"- Text columns: {', '.join(bundle.text_cols)}",
        f"- Universe source: `{bundle.common_universe_source}`",
        f"- Security news coverage threshold: {bundle.security_news_coverage_threshold:g}",
        f"- Target securities: {common_cov[common_news_include_col(bundle.security_news_coverage_threshold)].sum() if not common_cov.empty and common_news_include_col(bundle.security_news_coverage_threshold) in common_cov.columns else 'n/a'}",
        f"- Detected input tables/views: {len(bundle.input_tables)} total. `news_model_input` is the Common-News source for this run.",
        "",
        "## 2a. Universen kurz erklaert",
        "- `S1_FULL_568`: breite Price-only-Robustheitsanalyse mit allen Aktien, fuer die S1-Preisfeatures und Folgerenditen verfuegbar sind.",
        "- `S1_COMMON_NEWS_ANY`: Hauptvergleichsuniversum fuer S1_PRICE_ONLY, S2_LEXICON_ONLY und HYBRID_S1_LEX_50_50; eine Aktie muss mindestens einen News-Signaltag auf einem gueltigen Preistag haben.",
        "- `S1_COMMON_NEWS_COV30`: strengeres Robustheitsuniversum; eine Aktie muss an mindestens 30 Prozent ihrer gueltigen Preistage News-Signale haben.",
        "- `S1_COMMON_NEWS_COV50`: strengeres Robustheitsuniversum; eine Aktie muss an mindestens 50 Prozent ihrer gueltigen Preistage News-Signale haben.",
        "- Fuer diesen Report wird das zu `security_news_coverage_threshold` passende Common-News-Universum verwendet. Bei `0.0` ist das `S1_COMMON_NEWS_ANY`.",
        "",
        "## 3. Lexikonquelle und Lexikonstatistik",
        f"- Source: `{lexicon.source_path}`" if lexicon.source_path else "- Source: Toy fallback",
        f"- Positive terms: {lexicon.positive_count:,}",
        f"- Negative terms: {lexicon.negative_count:,}",
        f"- Conflicts excluded: {lexicon.conflict_count:,}",
        "- WARN: Toy lexicon used - results not thesis-grade." if lexicon.is_toy else "",
        "",
        "## 4. Artikel-Scoring-Statistik",
        "```json",
        json.dumps(article_stats, indent=2, default=str),
        "```",
        "",
        "## 5. Daily Lexicon Coverage",
        dataframe_to_markdown(coverage.groupby(["model_family", "strategy_variant", "news_mode", "time_logic", "coverage_filter"], observed=True).agg(avg_coverage=("coverage_ratio", "mean"), avg_share_positions_with_news=("share_positions_with_news", "mean")).reset_index() if not coverage.empty else pd.DataFrame()),
        "",
        "## 6. S2-Strategievarianten",
        "- S2_LEX_ONLY_NEUTRAL_FILL: reines Lexikonmodell, alle Common-News-Aktien/Tage, fehlende News neutral mit lex_score_raw = 0.",
        "- S2_LEX_ONLY_EVENT_ONLY: reines Lexikonmodell, nur Aktien/Tage mit News, Diagnostik.",
        "- HYBRID_S1_LEX_50_50_NEUTRAL_FILL: 50/50 Late Fusion aus S1 und Lexikon, Haupt-Hybridvariante.",
        "- HYBRID_S1_LEX_50_50_EVENT_ONLY: Hybrid nur an News-Tagen, Diagnostik.",
        "",
        "## 6a. Hauptvergleich",
        dataframe_to_markdown(comparison, max_rows=80),
        "",
        "## 7. Brutto-Metriken",
        dataframe_to_markdown(metrics),
        "",
        "## 8. Netto-Metriken 5/10/25 bps",
        dataframe_to_markdown(metrics_net),
        "",
        "## 9. Benchmarkvergleich",
        "\n".join(f"- {note}" for note in benchmark_notes),
        "- Zusatzgrafik: `outputs/figures/s2_vs_s1_benchmark_equity.png` zeigt S1, S2_LEXICON_ONLY, HYBRID_S1_LEX_50_50 und den STOXX Europe 600 Benchmark auf denselben matched dates.",
        dataframe_to_markdown(outputs.get("s2_vs_benchmark_metrics", pd.DataFrame())),
        "",
        "## 10. Vergleich gegen S1, falls möglich",
        s1_note,
        dataframe_to_markdown(outputs.get("s2_vs_s1_metrics", pd.DataFrame())),
        "",
        "## 11. IC-Diagnostik",
        dataframe_to_markdown(outputs.get("s2_ic_summary", pd.DataFrame())),
        "",
        "## 12. Quintile-Spread-Diagnostik",
        dataframe_to_markdown(outputs.get("s2_quintile_spread_metrics", pd.DataFrame())),
        "",
        "## 13. Coverage / share_positions_with_news",
        dataframe_to_markdown(coverage),
        "",
        "Security-Level Common-News Coverage:",
        dataframe_to_markdown(common_cov.head(40)),
        "",
        "## 14. Sektor- und Länderexposures",
        "### Sektor",
        dataframe_to_markdown(outputs.get("s2_sector_exposure", pd.DataFrame())),
        "### Land",
        dataframe_to_markdown(outputs.get("s2_country_exposure", pd.DataFrame())),
        "",
        "## 15. Validierungschecks",
        dataframe_to_markdown(validations, max_rows=100),
        "",
        "## 16. Methodische Hinweise",
        "- S2 ist lexikonbasiert, nicht FinBERT/LLM.",
        "- S1_PRICE_ONLY bleibt die faire Preisbaseline auf demselben Common-News-Universum.",
        "- S2_LEXICON_ONLY hat keinen S1-Anteil im Score.",
        "- HYBRID_S1_LEX_50_50 nutzt 50 Prozent S1-Komponente und 50 Prozent Lexikon-Komponente.",
        "- News werden nur über signal_date verwendet.",
        "- publication_date is never used as portfolio date.",
        "- Neutral-Fill ist die Hauptstrategie.",
        "- Event-only ist nur Diagnostik.",
        "- S1_COMMON_NEWS_ANY ist das faire Hauptvergleichsuniversum, sofern keine strengere Coverage-Schwelle genutzt wird.",
        "- 50/50-Gewichtung im Hybrid ist ex ante gesetzt und nicht optimiert.",
    ]
    path.write_text("\n".join(line for line in lines if line is not None), encoding="utf-8")


def write_figures(figures_dir: Path, outputs: dict[str, pd.DataFrame]) -> None:
    figures_dir.mkdir(parents=True, exist_ok=True)
    daily = outputs.get("s2_daily_returns", pd.DataFrame()).copy()
    if not daily.empty:
        daily["date"] = pd.to_datetime(daily["date"], errors="coerce")
        main = daily[daily["news_mode"].eq("NEUTRAL_FILL")].copy()
        plt.figure(figsize=(10, 5))
        for key, df in main.groupby(["model_family", "time_logic", "coverage_filter"], observed=True):
            plt.plot(df["date"], df["equity"], label=f"{key[0]} | {key[1]} | {key[2]}")
        plt.title("S2 Equity Curves")
        plt.legend()
        plt.tight_layout()
        plt.savefig(figures_dir / "s2_equity_curves.png", dpi=160)
        plt.close()

        plt.figure(figsize=(10, 5))
        for key, df in main.groupby(["model_family", "time_logic", "coverage_filter"], observed=True):
            plt.plot(df["date"], df["drawdown"], label=f"{key[0]} | {key[1]} | {key[2]}")
        plt.title("S2 Drawdowns")
        plt.legend()
        plt.tight_layout()
        plt.savefig(figures_dir / "s2_drawdowns.png", dpi=160)
        plt.close()

        plt.figure(figsize=(10, 5))
        for key, df in main.groupby(["model_family", "time_logic", "coverage_filter"], observed=True):
            plt.plot(df["date"], df["coverage_ratio"], label=f"{key[0]} | {key[1]} | {key[2]}")
        plt.title("S2 Coverage")
        plt.legend()
        plt.tight_layout()
        plt.savefig(figures_dir / "s2_coverage.png", dpi=160)
        plt.close()

        plt.figure(figsize=(10, 5))
        for key, df in main.groupby(["model_family", "time_logic", "coverage_filter"], observed=True):
            plt.plot(df["date"], df["share_positions_with_news"], label=f"{key[0]} | {key[1]} | {key[2]}")
        plt.title("S2 Share Positions With News")
        plt.legend()
        plt.tight_layout()
        plt.savefig(figures_dir / "s2_share_positions_with_news.png", dpi=160)
        plt.close()

    quintile = outputs.get("s2_quintile_spread_returns", pd.DataFrame()).copy()
    if not quintile.empty:
        quintile["date"] = pd.to_datetime(quintile["date"], errors="coerce")
        plt.figure(figsize=(10, 5))
        for key, df in quintile.groupby(["model_family", "strategy_variant", "news_mode", "time_logic", "signal_name"], observed=True):
            if key[2] != "NEUTRAL_FILL" or key[4] != "score_used":
                continue
            equity = (1.0 + df.sort_values("date")["spread_ret_1d"]).cumprod()
            plt.plot(df.sort_values("date")["date"], equity, label=f"{key[0]} | {key[3]}")
        plt.title("S2 Quintile Spread Diagnostic")
        plt.legend()
        plt.tight_layout()
        plt.savefig(figures_dir / "s2_quintile_spread.png", dpi=160)
        plt.close()

    s1 = outputs.get("s2_vs_s1_daily", pd.DataFrame()).copy()
    if not s1.empty:
        s1["date"] = pd.to_datetime(s1["date"], errors="coerce")
        plot = s1[(s1["news_mode"] == "NEUTRAL_FILL") & (s1["portfolio_variant"] == "standard")].copy()
        combos = [
            ("research_signal_date", "all_days"),
            ("research_signal_date", "coverage_80"),
            ("conservative_price_lag1", "all_days"),
            ("conservative_price_lag1", "coverage_80"),
        ]
        fig, axes = plt.subplots(2, 2, figsize=(14, 8), sharex=True, sharey=True)
        colors = {
            "S1_PRICE_ONLY": "#222222",
            "S2_LEXICON_ONLY": "#1f77b4",
            "HYBRID_S1_LEX_50_50": "#2ca02c",
        }
        for ax, (time_logic, coverage_filter) in zip(axes.ravel(), combos):
            sub = plot[(plot["time_logic"] == time_logic) & (plot["coverage_filter"] == coverage_filter)].copy()
            if sub.empty:
                ax.set_title(f"{time_logic} | {coverage_filter}\n(no matched S1 data)")
                ax.grid(True, alpha=0.25)
                continue
            s1_line = sub.sort_values("date").drop_duplicates("date")
            ax.plot(
                s1_line["date"],
                s1_line["s1_equity_matched"],
                color=colors["S1_PRICE_ONLY"],
                linestyle="--",
                linewidth=2.0,
                label="S1 price-only",
            )
            for model_family, label in [
                ("S2_LEXICON_ONLY", "S2 lexicon-only"),
                ("HYBRID_S1_LEX_50_50", "Hybrid 50/50"),
            ]:
                model = sub[sub["model_family"] == model_family].sort_values("date")
                if model.empty:
                    continue
                ax.plot(
                    model["date"],
                    model["s2_equity_matched"],
                    color=colors[model_family],
                    linewidth=2.0,
                    label=label,
                )
            ax.set_title(f"{time_logic.replace('_', ' ')}\n{coverage_filter}")
            ax.grid(True, alpha=0.25)
            ax.set_ylabel("Matched equity")
        handles, labels = axes[0, 0].get_legend_handles_labels()
        fig.legend(handles, labels, loc="lower center", ncol=3, frameon=False)
        fig.suptitle("S2 Lexicon / Hybrid vs S1 Price-only, matched dates", fontsize=14)
        fig.autofmt_xdate()
        plt.tight_layout()
        plt.subplots_adjust(bottom=0.12, top=0.88)
        plt.savefig(figures_dir / "s2_vs_s1_equity.png", dpi=160)
        plt.close()

    benchmark = outputs.get("s2_vs_benchmark_daily", pd.DataFrame()).copy()
    if not s1.empty and not benchmark.empty:
        benchmark["date"] = pd.to_datetime(benchmark["date"], errors="coerce")
        benchmark_returns = (
            benchmark[["date", "symbol", "benchmark_ret_fwd_1d"]]
            .dropna(subset=["date", "benchmark_ret_fwd_1d"])
            .drop_duplicates(["date", "symbol"])
            .sort_values("date")
        )
        symbols = sorted(benchmark_returns["symbol"].dropna().astype(str).unique())
        benchmark_label = "STOXX Europe 600" if not symbols else f"STOXX Europe 600 ({', '.join(symbols)})"

        fig, axes = plt.subplots(2, 2, figsize=(14, 8), sharex=True, sharey=True)
        colors = {
            "S1_PRICE_ONLY": "#222222",
            "S2_LEXICON_ONLY": "#1f77b4",
            "HYBRID_S1_LEX_50_50": "#2ca02c",
            "BENCHMARK": "#d62728",
        }
        styles = {
            "S1_PRICE_ONLY": "--",
            "S2_LEXICON_ONLY": "-",
            "HYBRID_S1_LEX_50_50": "-",
            "BENCHMARK": ":",
        }
        for ax, (time_logic, coverage_filter) in zip(axes.ravel(), combos):
            sub = plot[(plot["time_logic"] == time_logic) & (plot["coverage_filter"] == coverage_filter)].copy()
            if sub.empty:
                ax.set_title(f"{time_logic} | {coverage_filter}\n(no matched S1 data)")
                ax.grid(True, alpha=0.25)
                continue
            s1_line = sub.sort_values("date").drop_duplicates("date")
            matched_dates = set(s1_line["date"].dropna())
            bench_line = benchmark_returns[benchmark_returns["date"].isin(matched_dates)].copy()
            bench_line = bench_line.drop_duplicates("date").sort_values("date")
            if not bench_line.empty:
                bench_line["benchmark_equity_panel"] = (1.0 + bench_line["benchmark_ret_fwd_1d"]).cumprod()
                ax.plot(
                    bench_line["date"],
                    bench_line["benchmark_equity_panel"],
                    color=colors["BENCHMARK"],
                    linestyle=styles["BENCHMARK"],
                    linewidth=2.4,
                    label=benchmark_label,
                )
            ax.plot(
                s1_line["date"],
                s1_line["s1_equity_matched"],
                color=colors["S1_PRICE_ONLY"],
                linestyle=styles["S1_PRICE_ONLY"],
                linewidth=2.0,
                label="S1 price-only",
            )
            for model_family, label in [
                ("S2_LEXICON_ONLY", "S2 lexicon-only"),
                ("HYBRID_S1_LEX_50_50", "Hybrid 50/50"),
            ]:
                model = sub[sub["model_family"] == model_family].sort_values("date")
                if model.empty:
                    continue
                ax.plot(
                    model["date"],
                    model["s2_equity_matched"],
                    color=colors[model_family],
                    linestyle=styles[model_family],
                    linewidth=2.0,
                    label=label,
                )
            ax.set_title(f"{time_logic.replace('_', ' ')}\n{coverage_filter}")
            ax.grid(True, alpha=0.25)
            ax.set_ylabel("Matched equity")
        handles, labels = axes[0, 0].get_legend_handles_labels()
        fig.legend(handles, labels, loc="lower center", ncol=4, frameon=False)
        fig.suptitle("S2 Lexicon / Hybrid vs S1 Price-only and STOXX Europe 600 Benchmark", fontsize=14)
        fig.autofmt_xdate()
        plt.tight_layout()
        plt.subplots_adjust(bottom=0.13, top=0.88)
        plt.savefig(figures_dir / "s2_vs_s1_benchmark_equity.png", dpi=160)
        plt.close()


def backup_output_if_exists(output_db: Path) -> Path | None:
    if not output_db.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = output_db.with_name(f"{output_db.stem}.backup_{stamp}{output_db.suffix}")
    shutil.copy2(output_db, backup)
    return backup


def write_outputs(output_db: Path, outputs: dict[str, pd.DataFrame]) -> None:
    output_db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(output_db) as conn:
        for table, df in outputs.items():
            prepare_for_sql(df).to_sql(table, conn, if_exists="replace", index=False)


def ensure_output_schemas(outputs: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    fixed: dict[str, pd.DataFrame] = {}
    for table, df in outputs.items():
        if df.empty and len(df.columns) == 0 and table in EMPTY_TABLE_COLUMNS:
            fixed[table] = pd.DataFrame(columns=EMPTY_TABLE_COLUMNS[table])
        else:
            fixed[table] = df
    return fixed


def build_metadata(args: argparse.Namespace, bundle: InputBundle, lexicon: LexiconBundle, article_stats: dict, backup_path: Path | None) -> pd.DataFrame:
    rows = [
        ("script_version", SCRIPT_VERSION),
        ("run_at_utc", now_utc()),
        ("input_db", str(bundle.input_db)),
        ("input_s1_results_db", str(bundle.input_s1_results_db) if bundle.input_s1_results_db else None),
        ("output_db", str(resolve_output_path(args.output_db))),
        ("backup_created", str(backup_path) if backup_path else None),
        ("price_table", bundle.price_table),
        ("text_cols", json.dumps(bundle.text_cols)),
        ("top_pct", args.top_pct),
        ("cost_bps", json.dumps(args.cost_bps)),
        ("coverage_threshold", args.coverage_threshold),
        ("universe_source", bundle.common_universe_source),
        ("universe_source_used", bundle.common_news_security_coverage["universe_source"].iloc[0] if not bundle.common_news_security_coverage.empty else None),
        ("security_news_coverage_threshold", bundle.security_news_coverage_threshold),
        ("n_target_securities", int(bundle.common_news_security_coverage[common_news_include_col(bundle.security_news_coverage_threshold)].sum()) if not bundle.common_news_security_coverage.empty and common_news_include_col(bundle.security_news_coverage_threshold) in bundle.common_news_security_coverage.columns else None),
        ("lexicon_source", str(lexicon.source_path) if lexicon.source_path else "toy"),
        ("lexicon_is_toy", int(lexicon.is_toy)),
        ("lexicon_conflicts_excluded", lexicon.conflict_count),
        ("method_news_date", "News signal is keyed by signal_date. publication_date is never used as portfolio date."),
        ("article_stats", json.dumps(article_stats, default=str)),
    ]
    if lexicon.is_toy:
        rows.append(("warning", "Toy lexicon used - results not thesis-grade."))
    return pd.DataFrame(rows, columns=["key", "value"])


def main() -> None:
    args = parse_args()
    output_dir = resolve_output_path(args.output_dir)
    output_db = resolve_output_path(args.output_db)
    if args.input_db is not None and resolve_existing_path(args.input_db, []) == output_db:
        raise RuntimeError("Refusing to overwrite input DB with output DB.")
    if args.input_s1_results_db is not None:
        s1_candidate = resolve_optional_path(args.input_s1_results_db)
        if s1_candidate is not None and s1_candidate == output_db:
            raise RuntimeError("Refusing to overwrite S1 results DB with output DB.")

    bundle = load_inputs(args)
    price, price_notes = prepare_price_panel(bundle.price_panel)
    common_ids, common_coverage, source_used, common_notes = build_common_news_universe(
        bundle.input_db,
        price,
        args.common_universe_source,
        args.security_news_coverage_threshold,
    )
    bundle.common_news_security_coverage = common_coverage
    bundle.source_notes.extend(common_notes)
    lexicon = load_lm_lexicon(bundle.project_dir, args.lexicon_csv, args.allow_toy_lexicon)
    article_scores, article_stats = score_articles(bundle.news, bundle.text_cols, lexicon, args.min_token_count)
    daily_lex = aggregate_daily_lex(article_scores)
    panel = build_backtest_panel(price, daily_lex, common_ids)
    for frame in [panel]:
        frame["universe_source"] = source_used
        frame["security_news_coverage_threshold"] = args.security_news_coverage_threshold
    positions, daily, coverage = run_backtests(panel, args.top_pct, args.coverage_threshold)
    for frame in [positions, daily, coverage]:
        if not frame.empty:
            frame["universe_source"] = source_used
            frame["security_news_coverage_threshold"] = args.security_news_coverage_threshold
    daily, _ = compute_turnover_and_costs(daily, positions, args.cost_bps)
    if not daily.empty:
        daily["universe_source"] = source_used
        daily["security_news_coverage_threshold"] = args.security_news_coverage_threshold
    rf = load_risk_free(bundle.input_db, bundle.input_s1_results_db)
    metrics = compute_metrics(daily, rf)
    metrics_net = compute_metrics_net(pd.DataFrame(), daily, rf, args.cost_bps)
    bench_daily, bench_metrics, bench_notes = compute_benchmark_comparison(daily, bundle.benchmark_prices)
    ic_by_date, ic_summary, q_returns, q_metrics = compute_signal_diagnostics(panel)
    sector, country, top_positions = compute_exposures(positions)
    s2_vs_s1_daily, s2_vs_s1_metrics, s1_note = compare_with_s1(daily, bundle.input_s1_results_db, args.security_news_coverage_threshold)
    comparison_summary = build_model_comparison_summary(metrics, metrics_net, s2_vs_s1_metrics)
    for frame in [
        metrics,
        metrics_net,
        bench_daily,
        bench_metrics,
        ic_by_date,
        ic_summary,
        q_returns,
        q_metrics,
        sector,
        country,
        top_positions,
        s2_vs_s1_daily,
        s2_vs_s1_metrics,
        comparison_summary,
    ]:
        if not frame.empty:
            frame["universe_source"] = source_used
            frame["security_news_coverage_threshold"] = args.security_news_coverage_threshold

    outputs: dict[str, pd.DataFrame] = {
        "s2_run_metadata": pd.DataFrame(),
        "s2_lexicon_terms": lexicon.terms,
        "s2_lexicon_article_scores": article_scores,
        "s2_lexicon_daily_scores": daily_lex,
        "s2_backtest_panel": panel,
        "s2_positions": positions,
        "s2_daily_returns": daily,
        "s2_metrics": metrics,
        "s2_metrics_net": metrics_net,
        "s2_coverage_by_date": coverage,
        "s2_ic_by_date": ic_by_date,
        "s2_ic_summary": ic_summary,
        "s2_quintile_spread_returns": q_returns,
        "s2_quintile_spread_metrics": q_metrics,
        "s2_vs_benchmark_daily": bench_daily,
        "s2_vs_benchmark_metrics": bench_metrics,
        "s2_vs_s1_daily": s2_vs_s1_daily,
        "s2_vs_s1_metrics": s2_vs_s1_metrics,
        "s2_sector_exposure": sector,
        "s2_country_exposure": country,
        "s2_top_positions": top_positions,
        "s2_metric_definitions": build_metric_definitions(),
        "s2_model_comparison_summary": comparison_summary,
        "s2_common_news_security_coverage": common_coverage,
        "s2_component_ic_summary": ic_summary,
    }

    figures_dir = output_dir / "figures"
    report_path = output_dir / "reports" / "s2_lexicon_report.md"
    write_figures(figures_dir, outputs)
    write_report(report_path, outputs, bundle, lexicon, {**article_stats, "price_notes": price_notes}, bench_notes, s1_note)

    backup_path = backup_output_if_exists(output_db) if output_db.exists() else None
    outputs["s2_run_metadata"] = build_metadata(args, bundle, lexicon, {**article_stats, "price_notes": price_notes}, backup_path)
    validations = run_validations(
        bundle,
        lexicon,
        article_scores,
        daily_lex,
        panel,
        positions,
        daily,
        bench_daily,
        s2_vs_s1_metrics,
        report_path,
        figures_dir,
        output_db,
        args,
    )
    outputs["s2_validation_checks"] = validations
    write_report(report_path, outputs, bundle, lexicon, {**article_stats, "price_notes": price_notes}, bench_notes, s1_note)
    outputs = ensure_output_schemas(outputs)
    write_outputs(output_db, outputs)

    main_tables = [
        "s2_run_metadata",
        "s2_lexicon_terms",
        "s2_lexicon_article_scores",
        "s2_lexicon_daily_scores",
        "s2_backtest_panel",
        "s2_positions",
        "s2_daily_returns",
        "s2_metrics",
        "s2_validation_checks",
    ]
    row_counts = {table: len(outputs.get(table, pd.DataFrame())) for table in main_tables}
    validation_counts = validations["status"].value_counts().to_dict()
    print("S2 lexicon backtest completed.")
    print(f"output_db: {output_db}")
    print(f"row_counts: {row_counts}")
    print(f"validation_counts: {validation_counts}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
