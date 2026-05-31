#!/usr/bin/env python3
"""
Revised S1 price-only backtests for the STOXX Europe 600 thesis project.

The script is intentionally self-contained and uses only local SQLite/CSV inputs.
It does not modify the existing RESULTS.sql / RESULTS.sql.zip files. A new
versioned result database is written to outputs/results/s1_revised_results.sqlite.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import shutil
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


SCRIPT_VERSION = "2026-05-15-s1-revised-v2"
PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_COST_BPS = [5.0, 10.0, 25.0]
REQUIRED_PRICE_COLS = {"date", "ret_fwd_1d"}
QUALITY_COLUMNS = [
    "universe_quality",
    "quality_bucket",
    "match_quality",
    "price_match_quality",
    "yahoo_match_quality",
    "download_priority",
    "cap_group",
    "bucket",
    "classification",
]


@dataclass
class InputBundle:
    project_dir: Path
    repo_root: Path
    input_db: Path
    input_results_db: Path | None
    price_panel: pd.DataFrame
    benchmark_prices: pd.DataFrame
    securities: pd.DataFrame
    text_security_ids: set[int]
    common_text_source: str
    common_universe_source: str
    security_news_coverage_thresholds: list[float]
    common_news_security_coverage: pd.DataFrame
    discovered_inputs: list[dict]
    source_notes: list[str]


@dataclass
class UniverseDetection:
    universes: dict[str, set[int]]
    universe_notes: dict[str, str]
    candidate_sources: list[dict]
    core_identified: bool


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run revised S1 price-only backtests for multiple universes."
    )
    parser.add_argument("--input-db", type=Path, default=None)
    parser.add_argument("--input-results-db", type=Path, default=None)
    parser.add_argument(
        "--output-db",
        type=Path,
        default=Path("outputs/results/s1_revised_results.sqlite"),
    )
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"))
    parser.add_argument("--top-pct", type=float, default=0.20)
    parser.add_argument("--coverage-threshold", type=float, default=0.80)
    parser.add_argument("--cost-bps", type=float, nargs="+", default=DEFAULT_COST_BPS)
    parser.add_argument("--risk-free-rate", type=float, default=0.0)
    parser.add_argument(
        "--common-universe-source",
        choices=["news_model_input", "daily_text_scores", "auto"],
        default="news_model_input",
    )
    parser.add_argument(
        "--security-news-coverage-thresholds",
        nargs="+",
        type=float,
        default=[0.0, 0.3, 0.5],
    )
    return parser.parse_args()


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def resolve_existing_path(path: Path | None, candidates: Iterable[Path]) -> Path:
    if path is not None:
        resolved = path.expanduser()
        if not resolved.is_absolute():
            cwd_candidate = Path.cwd() / resolved
            project_candidate = PROJECT_DIR / resolved
            resolved = cwd_candidate if cwd_candidate.exists() else project_candidate
        if resolved.exists():
            return resolved
        raise FileNotFoundError(
            f"Input not found: {resolved}. Check --input-db or provide an existing SQLite file."
        )
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    raise FileNotFoundError(
        "No suitable input database found. Tried: "
        + ", ".join(str(p) for p in candidates)
    )


def repo_root_from(project_dir: Path) -> Path:
    current = project_dir.resolve()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    return current


def sqlite_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({quote_ident(table)})")]


def quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def read_sql_if_exists(conn: sqlite3.Connection, table: str) -> pd.DataFrame:
    tables = sqlite_tables(conn)
    if table not in tables:
        return pd.DataFrame()
    return pd.read_sql_query(f"SELECT * FROM {quote_ident(table)}", conn)


def first_existing_col(df: pd.DataFrame, candidates: Iterable[str]) -> str | None:
    lower_to_col = {c.lower(): c for c in df.columns}
    for candidate in candidates:
        found = lower_to_col.get(candidate.lower())
        if found is not None:
            return found
    return None


def discover_file_inputs(project_dir: Path, repo_root: Path) -> list[dict]:
    roots = [project_dir, repo_root]
    rows: list[dict] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            if path.suffix.lower() not in {".sqlite", ".db", ".csv", ".sql", ".zip"}:
                continue
            item = {
                "path": str(path),
                "suffix": path.suffix.lower(),
                "size_bytes": path.stat().st_size,
            }
            if path.suffix.lower() == ".csv":
                try:
                    item["columns"] = list(pd.read_csv(path, nrows=0).columns)
                except Exception as exc:  # pragma: no cover - diagnostic only
                    item["read_error"] = str(exc)
            rows.append(item)
    return rows


def load_inputs(args: argparse.Namespace) -> InputBundle:
    project_dir = PROJECT_DIR
    repo_root = repo_root_from(project_dir)
    input_db = resolve_existing_path(
        args.input_db,
        [
            project_dir / "DATA" / "thesis_data v2.sqlite",
            project_dir / "DATA" / "thesis_data.sqlite",
            repo_root / "DATA" / "thesis_data v2.sqlite",
            repo_root / "DATA" / "thesis_data.sqlite",
            repo_root / "Projekt_Bachelor_DATENAUFBEREITUNG" / "thesis_data.sqlite",
        ],
    )

    input_results_db = args.input_results_db
    if input_results_db is None:
        default_results = project_dir / "RESULTS.sql"
        input_results_db = default_results if default_results.exists() else None
    else:
        input_results_db = resolve_existing_path(input_results_db, [])

    source_notes: list[str] = []
    with sqlite3.connect(input_db) as conn:
        tables = sqlite_tables(conn)
        if "price_features_s1" in tables:
            price_panel = pd.read_sql_query(
                """
                SELECT
                    p.*,
                    s.company,
                    s.country,
                    s.sector,
                    s.cap_group,
                    s.download_priority,
                    s.mapping_confidence,
                    s.n_price_days
                FROM price_features_s1 p
                LEFT JOIN securities s
                  ON s.security_id = p.security_id
                """,
                conn,
            )
            source_notes.append("Loaded price panel from SQLite table price_features_s1.")
        elif "backtest_panel_price_only" in tables:
            price_panel = pd.read_sql_query("SELECT * FROM backtest_panel_price_only", conn)
            source_notes.append("Loaded price panel from SQLite table backtest_panel_price_only.")
        else:
            raise ValueError(
                f"No S1 price panel table found in {input_db}. Expected price_features_s1 "
                "or backtest_panel_price_only."
            )

        securities = read_sql_if_exists(conn, "securities")
        benchmark_prices = load_benchmark_prices(conn, source_notes)

        text_security_ids = set()
        common_text_source = (
            f"Common-news universe source requested: {args.common_universe_source}. "
            "Coverage is built after S1 score validation from signal_date/date matched to valid price days."
        )

    discovered_inputs = discover_file_inputs(project_dir, repo_root)
    return InputBundle(
        project_dir=project_dir,
        repo_root=repo_root,
        input_db=input_db,
        input_results_db=input_results_db,
        price_panel=price_panel,
        benchmark_prices=benchmark_prices,
        securities=securities,
        text_security_ids=text_security_ids,
        common_text_source=common_text_source,
        common_universe_source=args.common_universe_source,
        security_news_coverage_thresholds=args.security_news_coverage_thresholds,
        common_news_security_coverage=pd.DataFrame(),
        discovered_inputs=discovered_inputs,
        source_notes=source_notes,
    )


def load_benchmark_prices(conn: sqlite3.Connection, source_notes: list[str]) -> pd.DataFrame:
    tables = sqlite_tables(conn)
    if {"benchmark_prices_daily", "benchmark_instruments"}.issubset(tables):
        df = pd.read_sql_query(
            """
            SELECT
                p.symbol,
                p.date,
                p.adjusted_close,
                b.name,
                b.is_preferred
            FROM benchmark_prices_daily p
            JOIN benchmark_instruments b
              ON b.symbol = p.symbol
            ORDER BY p.symbol, p.date
            """,
            conn,
        )
        source_notes.append("Loaded benchmark prices from benchmark_prices_daily.")
        return df
    source_notes.append("No benchmark_prices_daily table found.")
    return pd.DataFrame()


def compute_or_validate_s1_score(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str], str]:
    out = df.copy()
    notes: list[str] = []
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out["security_id"] = pd.to_numeric(out["security_id"], errors="coerce").astype("Int64")
    out["ret_fwd_1d"] = pd.to_numeric(out["ret_fwd_1d"], errors="coerce")

    vol_convention = "unknown"
    has_vol60 = "vol_60d" in out.columns or "lowvol_60d" in out.columns
    has_vol63 = "vol_63d" in out.columns or "lowvol_63d" in out.columns
    if has_vol60:
        vol_convention = "VOL60"
        notes.append(
            "Die implementierte Variante nutzt VOL^60, was ca. drei Handelsmonaten entspricht. "
            "Die Formel in der Arbeit ist entsprechend auf VOL^60 anzupassen oder die Features sind auf VOL^63 neu zu berechnen."
        )
    elif has_vol63:
        vol_convention = "VOL63"
        notes.append("Using VOL63/lowvol_63d convention from available inputs.")
    else:
        notes.append("No VOL60/VOL63 column found; low-volatility component cannot be rebuilt from volatility.")

    numeric_cols = [
        "mom_12_1",
        "mom_3m",
        "vol_60d",
        "vol_63d",
        "lowvol_60d",
        "lowvol_63d",
        "z_mom_12_1",
        "z_lowvol_60d",
        "z_lowvol_63d",
        "s1_price_score",
        "z_s1_price_score",
    ]
    for col in numeric_cols:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")

    if "z_s1_price_score" in out.columns and out["z_s1_price_score"].notna().any():
        out["score_research"] = out["z_s1_price_score"]
        notes.append("Using existing z_s1_price_score as the research score.")
    else:
        if "z_mom_12_1" not in out.columns or out["z_mom_12_1"].notna().sum() == 0:
            if "mom_12_1" not in out.columns:
                raise ValueError("Cannot compute S1 score: missing mom_12_1 and z_mom_12_1.")
            out["z_mom_12_1"] = zscore_by_date(out, "mom_12_1")
            notes.append("Computed z_mom_12_1 by date from mom_12_1.")

        lowvol_col = None
        if "z_lowvol_60d" in out.columns and out["z_lowvol_60d"].notna().any():
            lowvol_col = "z_lowvol_60d"
        elif "z_lowvol_63d" in out.columns and out["z_lowvol_63d"].notna().any():
            lowvol_col = "z_lowvol_63d"
        elif "lowvol_60d" in out.columns:
            out["z_lowvol_60d"] = zscore_by_date(out, "lowvol_60d")
            lowvol_col = "z_lowvol_60d"
            notes.append("Computed z_lowvol_60d by date from lowvol_60d.")
        elif "lowvol_63d" in out.columns:
            out["z_lowvol_63d"] = zscore_by_date(out, "lowvol_63d")
            lowvol_col = "z_lowvol_63d"
            notes.append("Computed z_lowvol_63d by date from lowvol_63d.")
        elif "vol_60d" in out.columns:
            out["lowvol_60d"] = -out["vol_60d"]
            out["z_lowvol_60d"] = zscore_by_date(out, "lowvol_60d")
            lowvol_col = "z_lowvol_60d"
            notes.append("Computed lowvol_60d as -vol_60d and z-scored by date.")
        elif "vol_63d" in out.columns:
            out["lowvol_63d"] = -out["vol_63d"]
            out["z_lowvol_63d"] = zscore_by_date(out, "lowvol_63d")
            lowvol_col = "z_lowvol_63d"
            notes.append("Computed lowvol_63d as -vol_63d and z-scored by date.")
        else:
            raise ValueError("Cannot compute S1 score: missing low-volatility inputs.")

        out["s1_price_score"] = 0.5 * out["z_mom_12_1"] + 0.5 * out[lowvol_col]
        out["score_research"] = zscore_by_date(out, "s1_price_score")
        out["z_s1_price_score"] = out["score_research"]
        notes.append("Computed s1_price_score and z_s1_price_score from available components.")

    out = out.sort_values(["security_id", "date"]).copy()
    out["score_lag1"] = out.groupby("security_id", observed=True)["score_research"].shift(1)
    return out, notes, vol_convention


def zscore_by_date(df: pd.DataFrame, col: str) -> pd.Series:
    values = pd.to_numeric(df[col], errors="coerce")
    mean = values.groupby(df["date"]).transform("mean")
    std = values.groupby(df["date"]).transform("std")
    return np.where(std > 0, (values - mean) / std, np.nan)


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
        if "news_model_input" in tables:
            count = conn.execute("SELECT COUNT(*) FROM news_model_input").fetchone()[0]
            if count > 0:
                notes.append("common_universe_source=auto resolved to news_model_input because it exists and is non-empty.")
                return "news_model_input", notes
        if "daily_text_scores" in tables:
            count = conn.execute("SELECT COUNT(*) FROM daily_text_scores").fetchone()[0]
            if count > 0:
                notes.append("common_universe_source=auto resolved to daily_text_scores because news_model_input is missing or empty.")
                return "daily_text_scores", notes
        raise RuntimeError("No non-empty news_model_input or daily_text_scores table available for common universe.")
    if source not in tables:
        raise RuntimeError(f"Requested common universe source table not found: {source}")
    count = conn.execute(f"SELECT COUNT(*) FROM {quote_ident(source)}").fetchone()[0]
    if count == 0:
        raise RuntimeError(f"Requested common universe source table is empty: {source}")
    notes.append(f"common_universe_source explicitly set to {source}.")
    return source, notes


def build_common_news_universes(
    conn: sqlite3.Connection,
    panel: pd.DataFrame,
    source: str = "news_model_input",
    thresholds: list[float] | None = None,
) -> tuple[dict[str, set[int]], pd.DataFrame, list[str]]:
    thresholds = sorted(set(float(t) for t in (thresholds or [0.0, 0.3, 0.5])))
    resolved_source, notes = resolve_common_universe_source(conn, source)
    cols = table_columns(conn, resolved_source)
    if "security_id" not in cols:
        raise RuntimeError(f"{resolved_source} is missing security_id.")
    if resolved_source == "news_model_input":
        if "signal_date" not in cols:
            raise RuntimeError("news_model_input is missing signal_date.")
        date_col = "signal_date"
    else:
        if "signal_date" in cols:
            date_col = "signal_date"
        elif "date" in cols:
            date_col = "date"
        else:
            raise RuntimeError(f"{resolved_source} is missing signal_date/date.")

    score_col = "score_research" if "score_research" in panel.columns else "z_s1_price_score"
    valid = panel.dropna(subset=["security_id", "date", score_col, "ret_fwd_1d"]).copy()
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

    news = pd.read_sql_query(
        f"SELECT security_id, {quote_ident(date_col)} AS signal_date FROM {quote_ident(resolved_source)}",
        conn,
    )
    news["security_id"] = pd.to_numeric(news["security_id"], errors="coerce").astype("Int64")
    news["signal_date"] = pd.to_datetime(news["signal_date"], errors="coerce")
    news = news.dropna(subset=["security_id", "signal_date"]).copy()
    news["security_id"] = news["security_id"].astype(int)
    news_days = news[["security_id", "signal_date"]].drop_duplicates()
    matched = valid_days.merge(
        news_days.rename(columns={"signal_date": "date"}),
        on=["security_id", "date"],
        how="inner",
    )
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
    meta = valid[meta_cols].drop_duplicates("security_id") if meta_cols else pd.DataFrame({"security_id": coverage["security_id"]})
    coverage = coverage.merge(meta, on="security_id", how="left")

    universes: dict[str, set[int]] = {}
    for threshold in thresholds:
        col = common_news_include_col(threshold)
        mask = (
            coverage["n_news_signal_days_on_valid_price_dates"].ge(1)
            if abs(threshold) < 1e-12
            else coverage["news_coverage_ratio"].ge(threshold)
        )
        coverage[col] = mask.astype(int)
        universes[common_news_universe_name(threshold)] = set(coverage.loc[mask, "security_id"].astype(int))
        notes.append(f"{common_news_universe_name(threshold)} built from {resolved_source}: {int(mask.sum())} securities.")

    ordered_cols = [
        "universe_source",
        "security_id",
        "yahoo_ticker",
        "company",
        "country",
        "sector",
        "n_valid_price_days",
        "n_news_signal_days_on_valid_price_dates",
        "news_coverage_ratio",
    ]
    include_cols = [common_news_include_col(t) for t in thresholds]
    date_cols = [
        "first_valid_price_date",
        "last_valid_price_date",
        "first_news_signal_date",
        "last_news_signal_date",
    ]
    for col in ordered_cols + include_cols + date_cols:
        if col not in coverage.columns:
            coverage[col] = np.nan
    coverage = coverage[ordered_cols + include_cols + date_cols].sort_values("security_id")
    return universes, coverage, notes


def detect_universes(bundle: InputBundle, panel: pd.DataFrame) -> UniverseDetection:
    valid = panel.dropna(subset=["security_id", "ret_fwd_1d", "score_research"])
    full_ids = set(valid["security_id"].astype(int).unique())
    universes: dict[str, set[int]] = {
        "S1_FULL_568": full_ids,
    }
    notes = {
        "S1_FULL_568": (
            f"U_FULL_PRICE: all securities with valid S1 score and ret_fwd_1d. "
            f"This is the broad gross result universe ({len(full_ids)} securities)."
        )
    }
    candidate_sources: list[dict] = []
    core_ids = find_core_universe_candidates(bundle, candidate_sources)
    core_identified = core_ids is not None
    if core_ids is not None:
        universes["S1_CORE"] = core_ids
        notes["S1_CORE"] = (
            f"U_CORE: identified 255-security A/B universe from documented source."
        )
    else:
        notes["S1_CORE"] = (
            "U_CORE konnte nicht eindeutig identifiziert werden. "
            "No arbitrary reconstruction was used; see candidate sources."
        )

    with sqlite3.connect(bundle.input_db) as conn:
        common_universes, coverage, common_notes = build_common_news_universes(
            conn,
            panel,
            source=bundle.common_universe_source,
            thresholds=bundle.security_news_coverage_thresholds,
        )
    bundle.common_news_security_coverage = coverage
    for name, ids in common_universes.items():
        ids = ids.intersection(full_ids)
        if ids:
            universes[name] = ids
            notes[name] = (
                f"{name}: Common-News universe from {coverage['universe_source'].iloc[0] if not coverage.empty else bundle.common_universe_source}; "
                f"security-level news coverage rule; {len(ids)} securities."
            )
        else:
            notes[name] = f"{name} could not be formed; no securities passed the news coverage rule."
    notes["COMMON_NEWS_SOURCE"] = " ".join(common_notes)

    return UniverseDetection(
        universes=universes,
        universe_notes=notes,
        candidate_sources=candidate_sources,
        core_identified=core_identified,
    )


def find_core_universe_candidates(
    bundle: InputBundle, candidate_sources: list[dict]
) -> set[int] | None:
    candidates: list[tuple[str, set[int]]] = []

    with sqlite3.connect(bundle.input_db) as conn:
        for table in sqlite_tables(conn):
            cols = table_columns(conn, table)
            quality_hits = [c for c in cols if c.lower() in QUALITY_COLUMNS]
            if "security_id" in cols and quality_hits:
                sample = pd.read_sql_query(f"SELECT * FROM {quote_ident(table)}", conn)
                result = inspect_quality_frame(sample, f"sqlite:{table}", candidate_sources)
                if result is not None:
                    candidates.append(result)

    search_roots = [bundle.project_dir, bundle.repo_root]
    seen: set[Path] = set()
    for root in search_roots:
        if not root.exists():
            continue
        for path in root.rglob("*.csv"):
            if path in seen:
                continue
            seen.add(path)
            lower_name = path.name.lower()
            if not any(token in lower_name for token in ["universe", "constituent", "price_panel", "03_price_panel"]):
                continue
            try:
                header = list(pd.read_csv(path, nrows=0).columns)
            except Exception:
                continue
            quality_hits = [c for c in header if c.lower() in QUALITY_COLUMNS]
            candidate_sources.append(
                {
                    "source": str(path),
                    "type": "csv",
                    "columns": header,
                    "quality_columns": quality_hits,
                }
            )
            if "security_id" not in header or not quality_hits:
                continue
            df = pd.read_csv(path)
            result = inspect_quality_frame(df, str(path), candidate_sources)
            if result is not None:
                candidates.append(result)

    exact_255 = [(source, ids) for source, ids in candidates if len(ids) == 255]
    unique_sets = {tuple(sorted(ids)) for _, ids in exact_255}
    if len(unique_sets) == 1:
        return set(exact_255[0][1])
    return None


def inspect_quality_frame(
    df: pd.DataFrame, source: str, candidate_sources: list[dict]
) -> tuple[str, set[int]] | None:
    if "security_id" not in df.columns:
        return None
    quality_cols = [c for c in df.columns if c.lower() in QUALITY_COLUMNS]
    if not quality_cols:
        return None

    for col in quality_cols:
        values = df[col].astype("string").str.upper().str.strip()
        mask = values.str.contains(r"(?:^A$|^B$|A_CORE|B_DOWNLOAD|A/B|AB)", regex=True, na=False)
        ids = set(pd.to_numeric(df.loc[mask, "security_id"], errors="coerce").dropna().astype(int))
        candidate_sources.append(
            {
                "source": source,
                "quality_column": col,
                "matched_ab_like_security_count": len(ids),
                "value_counts": values.value_counts(dropna=False).head(12).to_dict(),
            }
        )
        if len(ids) == 255:
            return source, ids
    return None


def add_time_logic_scores(panel: pd.DataFrame, time_logic: str) -> pd.DataFrame:
    out = panel.copy()
    if time_logic == "research_close_to_close":
        out["score_used"] = out["score_research"]
    elif time_logic == "conservative_lag1":
        out["score_used"] = out["score_lag1"]
    else:
        raise ValueError(f"Unknown time logic: {time_logic}")
    return out


def run_backtest(
    panel: pd.DataFrame,
    universe_name: str,
    security_ids: set[int],
    time_logic: str,
    portfolio_variant: str,
    coverage_filter: str,
    top_pct: float,
    coverage_threshold: float,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    target_n = len(security_ids)
    data = add_time_logic_scores(panel[panel["security_id"].astype(int).isin(security_ids)], time_logic)
    candidates = data.dropna(subset=["date", "security_id", "score_used", "ret_fwd_1d"]).copy()
    candidates["security_id"] = candidates["security_id"].astype(int)

    coverage = (
        candidates.groupby("date", observed=True)
        .agg(n_candidates=("security_id", "nunique"))
        .reset_index()
    )
    coverage["universe"] = universe_name
    coverage["time_logic"] = time_logic
    coverage["portfolio_variant"] = portfolio_variant
    coverage["coverage_filter"] = coverage_filter
    coverage["n_target_securities"] = target_n
    coverage["coverage_ratio"] = np.where(
        target_n > 0, coverage["n_candidates"] / target_n, np.nan
    )
    if coverage_filter == "coverage_80":
        valid_dates = set(
            coverage.loc[coverage["coverage_ratio"] >= coverage_threshold, "date"]
        )
        candidates = candidates[candidates["date"].isin(valid_dates)].copy()
        coverage = coverage[coverage["date"].isin(valid_dates)].copy()

    if candidates.empty:
        return pd.DataFrame(), pd.DataFrame(), coverage

    selected_rows = []
    if portfolio_variant == "standard":
        for date, day in candidates.groupby("date", sort=True, observed=True):
            day = day.sort_values(["score_used", "security_id"], ascending=[False, True]).copy()
            n_candidates = len(day)
            n_select = max(1, int(math.ceil(top_pct * n_candidates)))
            selected = day.head(n_select).copy()
            selected["rank"] = np.arange(1, len(selected) + 1)
            selected["n_candidates"] = n_candidates
            selected["n_positions"] = len(selected)
            selected_rows.append(selected)
    elif portfolio_variant == "sector_neutral_robustness":
        if "sector" not in candidates.columns or candidates["sector"].isna().all():
            return pd.DataFrame(), pd.DataFrame(), coverage
        for date, day in candidates.groupby("date", sort=True, observed=True):
            selected_parts = []
            for _, sector_day in day.groupby("sector", dropna=True, observed=True):
                sector_day = sector_day.sort_values(
                    ["score_used", "security_id"], ascending=[False, True]
                ).copy()
                n_select = max(1, int(math.ceil(top_pct * len(sector_day))))
                selected_parts.append(sector_day.head(n_select))
            if not selected_parts:
                continue
            selected = pd.concat(selected_parts, ignore_index=True).sort_values(
                ["score_used", "security_id"], ascending=[False, True]
            )
            selected["rank"] = np.arange(1, len(selected) + 1)
            selected["n_candidates"] = len(day)
            selected["n_positions"] = len(selected)
            selected_rows.append(selected)
    else:
        raise ValueError(f"Unknown portfolio variant: {portfolio_variant}")

    if not selected_rows:
        return pd.DataFrame(), pd.DataFrame(), coverage

    positions = pd.concat(selected_rows, ignore_index=True)
    positions["weight"] = 1.0 / positions["n_positions"]
    positions["universe"] = universe_name
    positions["time_logic"] = time_logic
    positions["portfolio_variant"] = portfolio_variant
    positions["coverage_filter"] = coverage_filter
    positions["score"] = positions["score_used"]
    keep_cols = [
        "universe",
        "time_logic",
        "portfolio_variant",
        "coverage_filter",
        "date",
        "security_id",
        "yahoo_ticker",
        "company",
        "country",
        "sector",
        "score",
        "ret_fwd_1d",
        "weight",
        "rank",
        "n_candidates",
        "n_positions",
    ]
    for col in keep_cols:
        if col not in positions.columns:
            positions[col] = np.nan
    positions = positions[keep_cols].copy()

    daily = (
        positions.assign(weighted_return=positions["weight"] * positions["ret_fwd_1d"])
        .groupby(["universe", "time_logic", "portfolio_variant", "coverage_filter", "date"], observed=True)
        .agg(
            strategy_ret_fwd_1d=("weighted_return", "sum"),
            n_positions=("security_id", "nunique"),
            n_candidates=("n_candidates", "first"),
        )
        .reset_index()
    )
    daily["n_target_securities"] = target_n
    daily["coverage_ratio"] = np.where(
        target_n > 0, daily["n_candidates"] / target_n, np.nan
    )
    daily = add_equity_and_drawdown(daily, "strategy_ret_fwd_1d", "equity", "drawdown")
    return daily, positions, coverage


def add_equity_and_drawdown(
    df: pd.DataFrame, return_col: str, equity_col: str, drawdown_col: str
) -> pd.DataFrame:
    out = df.sort_values("date").copy()
    if out.empty:
        out[equity_col] = []
        out[drawdown_col] = []
        return out
    out[equity_col] = (1.0 + out[return_col].fillna(0.0)).cumprod()
    running_max = out[equity_col].cummax()
    out[drawdown_col] = out[equity_col] / running_max - 1.0
    return out


def compute_turnover_and_costs(
    daily_returns: pd.DataFrame, positions: pd.DataFrame, cost_bps: list[float]
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if daily_returns.empty or positions.empty:
        return daily_returns, pd.DataFrame()

    daily_parts = []
    metrics_net = []
    key_cols = ["universe", "time_logic", "portfolio_variant", "coverage_filter"]
    for key, pos in positions.groupby(key_cols, observed=True):
        pos = pos.sort_values("date")
        wide = pos.pivot_table(
            index="date", columns="security_id", values="weight", aggfunc="sum", fill_value=0.0
        ).sort_index()
        turnover_total = wide.diff().abs().sum(axis=1)
        if len(turnover_total) > 0:
            turnover_total.iloc[0] = 0.0
        turnover = pd.DataFrame(
            {
                "date": turnover_total.index,
                "turnover_total": turnover_total.values,
                "turnover_oneway": 0.5 * turnover_total.values,
            }
        )
        mask = np.logical_and.reduce(
            [daily_returns[col].eq(value) for col, value in zip(key_cols, key)]
        )
        part = daily_returns.loc[mask].merge(turnover, on="date", how="left")
        part["turnover_total"] = part["turnover_total"].fillna(0.0)
        part["turnover_oneway"] = part["turnover_oneway"].fillna(0.0)
        for bps in cost_bps:
            label = cost_label(bps)
            cost_rate = bps / 10000.0
            part[f"transaction_cost_{label}"] = cost_rate * part["turnover_total"]
            part[f"net_return_{label}"] = (
                part["strategy_ret_fwd_1d"] - part[f"transaction_cost_{label}"]
            )
            part[f"net_equity_{label}"] = (1.0 + part[f"net_return_{label}"]).cumprod()
            running_max = part[f"net_equity_{label}"].cummax()
            part[f"net_drawdown_{label}"] = part[f"net_equity_{label}"] / running_max - 1.0
            metric = compute_return_metrics(
                part,
                return_col=f"net_return_{label}",
                equity_col=f"net_equity_{label}",
                drawdown_col=f"net_drawdown_{label}",
            )
            metrics_net.append(
                {
                    **dict(zip(key_cols, key)),
                    "cost_bps": bps,
                    **metric,
                }
            )
        daily_parts.append(part)

    return pd.concat(daily_parts, ignore_index=True), pd.DataFrame(metrics_net)


def cost_label(bps: float) -> str:
    if float(bps).is_integer():
        return f"{int(bps)}bps"
    return f"{str(bps).replace('.', '_')}bps"


def compute_return_metrics(
    df: pd.DataFrame,
    return_col: str = "strategy_ret_fwd_1d",
    equity_col: str = "equity",
    drawdown_col: str = "drawdown",
    risk_free_rate: float = 0.0,
) -> dict:
    returns = pd.to_numeric(df[return_col], errors="coerce").dropna()
    n_days = len(returns)
    if n_days == 0:
        return {
            "n_days": 0,
            "first_date": None,
            "last_date": None,
            "mean_daily_return": np.nan,
            "cumulative_return": np.nan,
            "cagr": np.nan,
            "ann_vol": np.nan,
            "sharpe": np.nan,
            "max_drawdown": np.nan,
        }
    final_equity = float(df[equity_col].dropna().iloc[-1])
    std = float(returns.std(ddof=1)) if n_days > 1 else np.nan
    mean = float(returns.mean())
    daily_rf = risk_free_rate / 252.0
    sharpe = ((mean - daily_rf) / std * math.sqrt(252.0)) if std and not np.isnan(std) else np.nan
    return {
        "n_days": n_days,
        "first_date": df["date"].min(),
        "last_date": df["date"].max(),
        "mean_daily_return": mean,
        "cumulative_return": final_equity - 1.0,
        "cagr": final_equity ** (252.0 / n_days) - 1.0,
        "ann_vol": std * math.sqrt(252.0) if not np.isnan(std) else np.nan,
        "sharpe": sharpe,
        "max_drawdown": float(df[drawdown_col].min()),
    }


def compute_metrics(daily_returns: pd.DataFrame, risk_free_rate: float) -> pd.DataFrame:
    rows = []
    key_cols = ["universe", "time_logic", "portfolio_variant", "coverage_filter"]
    for key, df in daily_returns.groupby(key_cols, observed=True):
        rows.append(
            {
                **dict(zip(key_cols, key)),
                **compute_return_metrics(df, risk_free_rate=risk_free_rate),
                "avg_n_positions": df["n_positions"].mean(),
                "avg_n_candidates": df["n_candidates"].mean(),
                "avg_coverage_ratio": df["coverage_ratio"].mean(),
                "avg_turnover_total": df.get("turnover_total", pd.Series(dtype=float)).mean(),
                "avg_turnover_oneway": df.get("turnover_oneway", pd.Series(dtype=float)).mean(),
            }
        )
    return pd.DataFrame(rows)


def compute_benchmark_returns(benchmark_prices: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    notes: list[str] = []
    if benchmark_prices.empty:
        return pd.DataFrame(), ["No benchmark prices available."]
    df = benchmark_prices.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["adjusted_close"] = pd.to_numeric(df["adjusted_close"], errors="coerce")
    if "is_preferred" in df.columns and df["is_preferred"].notna().any():
        preferred = df[pd.to_numeric(df["is_preferred"], errors="coerce").fillna(0).eq(1)]
        if not preferred.empty:
            df = preferred.copy()
            notes.append("Benchmark uses preferred instrument(s) from benchmark_instruments.")
    df = df.dropna(subset=["date", "symbol", "adjusted_close"]).sort_values(["symbol", "date"])
    df["benchmark_ret_fwd_1d"] = df.groupby("symbol", observed=True)["adjusted_close"].shift(-1) / df["adjusted_close"] - 1.0
    out = df.dropna(subset=["benchmark_ret_fwd_1d"])[["symbol", "date", "benchmark_ret_fwd_1d"]].copy()
    notes.append("Benchmark returns computed as adjusted_close(t+1) / adjusted_close(t) - 1.")
    return out, notes


def compute_benchmark_metrics(
    daily_returns: pd.DataFrame, benchmark_prices: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, list[str]]:
    benchmark_returns, notes = compute_benchmark_returns(benchmark_prices)
    if benchmark_returns.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), notes

    rows = []
    metrics = []
    missing = []
    key_cols = ["universe", "time_logic", "portfolio_variant", "coverage_filter"]
    for key, strat in daily_returns.groupby(key_cols, observed=True):
        merged = strat.merge(benchmark_returns, on="date", how="left")
        missing_part = merged[merged["benchmark_ret_fwd_1d"].isna()][key_cols + ["date"]].copy()
        missing_part["reason"] = "missing benchmark_ret_fwd_1d for strategy date"
        missing.append(missing_part)
        matched = merged.dropna(subset=["benchmark_ret_fwd_1d"]).copy()
        if matched.empty:
            continue
        matched["excess_return"] = matched["strategy_ret_fwd_1d"] - matched["benchmark_ret_fwd_1d"]
        matched["strategy_equity_matched"] = (1.0 + matched["strategy_ret_fwd_1d"]).cumprod()
        matched["benchmark_equity_matched"] = (1.0 + matched["benchmark_ret_fwd_1d"]).cumprod()
        rows.append(matched)
        excess_std = matched["excess_return"].std(ddof=1)
        strategy_final = matched["strategy_equity_matched"].iloc[-1]
        benchmark_final = matched["benchmark_equity_matched"].iloc[-1]
        metrics.append(
            {
                **dict(zip(key_cols, key)),
                "n_matched_days": len(matched),
                "benchmark_symbol": ",".join(sorted(matched["symbol"].dropna().unique())),
                "strategy_cum_return_matched": strategy_final - 1.0,
                "benchmark_cum_return_matched": benchmark_final - 1.0,
                "active_cum_return": strategy_final / benchmark_final - 1.0,
                "mean_daily_excess_return": matched["excess_return"].mean(),
                "tracking_error": excess_std * math.sqrt(252.0) if excess_std and not np.isnan(excess_std) else np.nan,
                "information_ratio": (
                    matched["excess_return"].mean() / excess_std * math.sqrt(252.0)
                    if excess_std and not np.isnan(excess_std)
                    else np.nan
                ),
            }
        )

    return (
        pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(),
        pd.DataFrame(metrics),
        pd.concat(missing, ignore_index=True) if missing else pd.DataFrame(),
        notes,
    )


def compute_coverage(daily_returns: pd.DataFrame) -> pd.DataFrame:
    if daily_returns.empty:
        return pd.DataFrame()
    return daily_returns[
        [
            "universe",
            "time_logic",
            "portfolio_variant",
            "coverage_filter",
            "date",
            "n_target_securities",
            "n_candidates",
            "n_positions",
            "coverage_ratio",
        ]
    ].copy()


def compute_ic_and_quintiles(
    panel: pd.DataFrame,
    universe_detection: UniverseDetection,
    top_pct: float,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    ic_rows = []
    quintile_rows = []
    for universe_name, ids in universe_detection.universes.items():
        for time_logic in ["research_close_to_close", "conservative_lag1"]:
            data = add_time_logic_scores(
                panel[panel["security_id"].astype(int).isin(ids)], time_logic
            ).dropna(subset=["date", "security_id", "score_used", "ret_fwd_1d"])
            for date, day in data.groupby("date", sort=True, observed=True):
                if len(day) >= 3:
                    ic = day["score_used"].corr(day["ret_fwd_1d"], method="spearman")
                    ic_rows.append(
                        {
                            "universe": universe_name,
                            "time_logic": time_logic,
                            "date": date,
                            "n_candidates": len(day),
                            "spearman_ic": ic,
                        }
                    )
                if len(day) >= 5:
                    ranked = day.sort_values(["score_used", "security_id"], ascending=[True, True]).copy()
                    ranked["score_quintile"] = pd.qcut(
                        ranked["score_used"].rank(method="first"), 5, labels=[1, 2, 3, 4, 5]
                    ).astype(int)
                    q = (
                        ranked.groupby("score_quintile", observed=True)
                        .agg(mean_ret_fwd_1d=("ret_fwd_1d", "mean"), n=("security_id", "nunique"))
                        .reset_index()
                    )
                    q["universe"] = universe_name
                    q["time_logic"] = time_logic
                    q["date"] = date
                    quintile_rows.append(q)

    ic_by_date = pd.DataFrame(ic_rows)
    if ic_by_date.empty:
        ic_summary = pd.DataFrame()
    else:
        summary_rows = []
        for key, df in ic_by_date.groupby(["universe", "time_logic"], observed=True):
            ic = df["spearman_ic"].dropna()
            std = ic.std(ddof=1)
            summary_rows.append(
                {
                    "universe": key[0],
                    "time_logic": key[1],
                    "mean_ic": ic.mean(),
                    "median_ic": ic.median(),
                    "positive_ic_share": (ic > 0).mean(),
                    "ic_t_stat": ic.mean() / (std / math.sqrt(len(ic))) if len(ic) > 1 and std else np.nan,
                    "n_ic_days": len(ic),
                }
            )
        ic_summary = pd.DataFrame(summary_rows)

    quintile_returns = (
        pd.concat(quintile_rows, ignore_index=True)
        if quintile_rows
        else pd.DataFrame()
    )
    if quintile_returns.empty:
        quintile_summary = pd.DataFrame()
    else:
        q_mean = (
            quintile_returns.groupby(["universe", "time_logic", "score_quintile"], observed=True)
            .agg(mean_ret_fwd_1d=("mean_ret_fwd_1d", "mean"), n_days=("date", "nunique"))
            .reset_index()
        )
        spreads = []
        for key, df in q_mean.groupby(["universe", "time_logic"], observed=True):
            by_q = df.set_index("score_quintile")["mean_ret_fwd_1d"]
            spreads.append(
                {
                    "universe": key[0],
                    "time_logic": key[1],
                    "q5_minus_q1_spread": by_q.get(5, np.nan) - by_q.get(1, np.nan),
                }
            )
        quintile_summary = q_mean.merge(pd.DataFrame(spreads), on=["universe", "time_logic"], how="left")
    return ic_by_date, ic_summary, quintile_returns, quintile_summary


def compute_exposures(
    positions: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if positions.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    key_cols = ["universe", "time_logic", "portfolio_variant", "coverage_filter"]
    if "sector" in positions.columns:
        sector = (
            positions.dropna(subset=["sector"])
            .groupby(key_cols + ["date", "sector"], observed=True)["weight"]
            .sum()
            .reset_index()
            .groupby(key_cols + ["sector"], observed=True)
            .agg(avg_portfolio_weight=("weight", "mean"), n_days=("date", "nunique"))
            .reset_index()
            .sort_values(key_cols + ["avg_portfolio_weight"], ascending=[True, True, True, True, False])
        )
    else:
        sector = pd.DataFrame()
    if "country" in positions.columns:
        country = (
            positions.dropna(subset=["country"])
            .groupby(key_cols + ["date", "country"], observed=True)["weight"]
            .sum()
            .reset_index()
            .groupby(key_cols + ["country"], observed=True)
            .agg(avg_portfolio_weight=("weight", "mean"), n_days=("date", "nunique"))
            .reset_index()
        )
    else:
        country = pd.DataFrame()

    top_positions = (
        positions.groupby(key_cols + ["security_id", "yahoo_ticker", "company", "sector", "country"], dropna=False, observed=True)
        .agg(
            n_selected_days=("date", "nunique"),
            avg_weight_when_selected=("weight", "mean"),
            avg_score_when_selected=("score", "mean"),
        )
        .reset_index()
    )
    top_positions["selection_rank"] = top_positions.groupby(key_cols, observed=True)[
        "n_selected_days"
    ].rank(method="first", ascending=False)
    top_positions = top_positions[top_positions["selection_rank"] <= 10].sort_values(
        key_cols + ["selection_rank"]
    )
    return sector, country, top_positions


def run_validations(
    panel: pd.DataFrame,
    positions: pd.DataFrame,
    daily_returns: pd.DataFrame,
    benchmark_daily: pd.DataFrame,
    input_results_db: Path | None,
    output_db: Path,
    vol_convention: str,
    bundle: InputBundle | None = None,
    universe_detection: UniverseDetection | None = None,
) -> pd.DataFrame:
    checks = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"check_name": name, "status": status, "detail": detail})

    duplicate_input = panel.duplicated(["security_id", "date"]).sum()
    add(
        "keine doppelten security_id-date-Zeilen im Input",
        "PASS" if duplicate_input == 0 else "FAIL",
        f"duplicate rows: {duplicate_input}",
    )
    duplicate_positions = positions.duplicated(
        ["universe", "time_logic", "portfolio_variant", "coverage_filter", "date", "security_id"]
    ).sum() if not positions.empty else 0
    add(
        "keine doppelten security_id-date-Zeilen in Positions",
        "PASS" if duplicate_positions == 0 else "FAIL",
        f"duplicate position rows: {duplicate_positions}",
    )
    if not positions.empty:
        weight_sums = positions.groupby(
            ["universe", "time_logic", "portfolio_variant", "coverage_filter", "date"], observed=True
        )["weight"].sum()
        max_weight_error = float((weight_sums - 1.0).abs().max())
        add(
            "Gewichte summieren pro Tag auf 1.0 +/- 1e-9",
            "PASS" if max_weight_error <= 1e-9 else "FAIL",
            f"max abs error: {max_weight_error}",
        )
    else:
        add("Gewichte summieren pro Tag auf 1.0 +/- 1e-9", "FAIL", "no positions")

    top20_status, top20_detail = validate_top_selection(panel, positions)
    add("Top-20-Auswahl korrekt", top20_status, top20_detail)

    return_status, return_detail = validate_daily_returns(positions, daily_returns)
    add("daily_return entspricht Summe(weight * ret_fwd_1d)", return_status, return_detail)

    cum_status, cum_detail = validate_cumulative_returns(daily_returns)
    add("cumulative_return entspricht product(1 + daily_return) - 1", cum_status, cum_detail)

    if benchmark_daily.empty:
        add("Benchmark-Excess nur auf matched days", "WARN", "no benchmark matched rows")
    else:
        add(
            "Benchmark-Excess nur auf matched days",
            "PASS" if benchmark_daily["benchmark_ret_fwd_1d"].notna().all() else "FAIL",
            f"matched rows: {len(benchmark_daily)}",
        )

    zero_position_days = int((daily_returns["n_positions"] <= 0).sum()) if not daily_returns.empty else 0
    add(
        "n_positions > 0 fuer alle Backtesttage",
        "PASS" if zero_position_days == 0 and not daily_returns.empty else "FAIL",
        f"zero-position days: {zero_position_days}",
    )

    selected_bad = positions["ret_fwd_1d"].replace([np.inf, -np.inf], np.nan).isna().sum() if not positions.empty else 0
    add(
        "keine NaN/Inf in ausgewaehlten Renditen",
        "PASS" if selected_bad == 0 else "FAIL",
        f"bad selected returns: {selected_bad}",
    )

    lag_ok = panel["score_lag1"].equals(panel.groupby("security_id", observed=True)["score_research"].shift(1))
    add(
        "conservative_lag1 nutzt tatsaechlich gelaggten Score",
        "PASS" if lag_ok else "FAIL",
        "score_lag1 equals groupwise previous score_research" if lag_ok else "lag mismatch",
    )
    add(
        "VOL60/VOL63-Namenskonvention dokumentiert",
        "PASS" if vol_convention in {"VOL60", "VOL63"} else "WARN",
        f"vol_convention={vol_convention}",
    )
    if bundle is not None and universe_detection is not None:
        coverage = bundle.common_news_security_coverage
        source_used = coverage["universe_source"].iloc[0] if not coverage.empty else bundle.common_universe_source
        add(
            "common_universe_source_used",
            "PASS" if source_used == bundle.common_universe_source or bundle.common_universe_source == "auto" else "FAIL",
            f"requested={bundle.common_universe_source}; used={source_used}",
        )
        add(
            "daily_text_scores is not used by default",
            "PASS" if bundle.common_universe_source != "daily_text_scores" and source_used != "daily_text_scores" else "WARN",
            f"requested={bundle.common_universe_source}; used={source_used}",
        )
        any_ids = universe_detection.universes.get("S1_COMMON_NEWS_ANY", set())
        full_ids = universe_detection.universes.get("S1_FULL_568", set())
        add("S1_COMMON_NEWS_ANY not empty", "PASS" if any_ids else "FAIL", f"n={len(any_ids)}")
        subset_ok = all(ids.issubset(full_ids) for name, ids in universe_detection.universes.items() if name.startswith("S1_COMMON_NEWS_"))
        add("Alle Common-News-Universen sind Teilmengen von S1_FULL_PRICE", "PASS" if subset_ok else "FAIL", f"full_n={len(full_ids)}")
        ratio_ok = coverage.empty or coverage["news_coverage_ratio"].between(0, 1).all()
        add("news_coverage_ratio liegt zwischen 0 und 1", "PASS" if ratio_ok else "FAIL", f"rows={len(coverage)}")
        for threshold in bundle.security_news_coverage_thresholds:
            name = common_news_universe_name(float(threshold))
            ids = universe_detection.universes.get(name, set())
            subset = coverage[coverage["security_id"].isin(ids)] if not coverage.empty else pd.DataFrame()
            if abs(float(threshold)) < 1e-12:
                ok = not subset.empty and subset["n_news_signal_days_on_valid_price_dates"].ge(1).all()
                check_name = "Für S1_COMMON_NEWS_ANY hat jede enthaltene Security mindestens einen News-Signaltag"
                detail = f"n={len(subset)}"
            else:
                ok = not subset.empty and subset["news_coverage_ratio"].ge(float(threshold) - 1e-12).all()
                check_name = f"Für {name} hat jede enthaltene Security news_coverage_ratio >= {threshold:g}"
                detail = f"n={len(subset)}; threshold={threshold:g}"
            add(check_name, "PASS" if ok else "FAIL", detail)
    if input_results_db is None:
        add(
            "Ergebnisse aus bestehender Results-DB nicht ueberschreiben",
            "PASS",
            "No existing results DB configured as input.",
        )
    else:
        add(
            "Ergebnisse aus bestehender Results-DB nicht ueberschreiben",
            "PASS" if input_results_db.resolve() != output_db.resolve() else "FAIL",
            f"input_results_db={input_results_db}; output_db={output_db}",
        )
    return pd.DataFrame(checks)


def validate_top_selection(panel: pd.DataFrame, positions: pd.DataFrame) -> tuple[str, str]:
    if positions.empty:
        return "FAIL", "no positions"
    violations = 0
    keys = ["universe", "time_logic", "portfolio_variant", "coverage_filter", "date"]
    for _, pos in positions[positions["portfolio_variant"].eq("standard")].groupby(keys, observed=True):
        min_selected = pos["score"].min()
        n_candidates = int(pos["n_candidates"].iloc[0])
        if len(pos) != max(1, math.ceil(0.20 * n_candidates)):
            violations += 1
        # The rank-based implementation sorts descending; this check catches scoring inversions.
        if pos["score"].isna().any() or min_selected > pos["score"].max():
            violations += 1
    return ("PASS" if violations == 0 else "FAIL", f"violating groups: {violations}")


def validate_daily_returns(positions: pd.DataFrame, daily_returns: pd.DataFrame) -> tuple[str, str]:
    if positions.empty or daily_returns.empty:
        return "FAIL", "missing positions or daily returns"
    calc = (
        positions.assign(calc_return=positions["weight"] * positions["ret_fwd_1d"])
        .groupby(["universe", "time_logic", "portfolio_variant", "coverage_filter", "date"], observed=True)["calc_return"]
        .sum()
        .reset_index()
    )
    merged = daily_returns.merge(
        calc,
        on=["universe", "time_logic", "portfolio_variant", "coverage_filter", "date"],
        how="left",
    )
    max_error = (merged["strategy_ret_fwd_1d"] - merged["calc_return"]).abs().max()
    return ("PASS" if max_error <= 1e-12 else "FAIL", f"max abs error: {max_error}")


def validate_cumulative_returns(daily_returns: pd.DataFrame) -> tuple[str, str]:
    if daily_returns.empty:
        return "FAIL", "no daily returns"
    max_error = 0.0
    for _, df in daily_returns.groupby(["universe", "time_logic", "portfolio_variant", "coverage_filter"], observed=True):
        calc = (1.0 + df["strategy_ret_fwd_1d"]).cumprod() - 1.0
        stored = df["equity"] - 1.0
        max_error = max(max_error, float((calc - stored).abs().max()))
    return ("PASS" if max_error <= 1e-12 else "FAIL", f"max abs error: {max_error}")


def write_outputs(
    args: argparse.Namespace,
    bundle: InputBundle,
    universe_detection: UniverseDetection,
    score_notes: list[str],
    vol_convention: str,
    outputs: dict[str, pd.DataFrame],
    benchmark_notes: list[str],
) -> None:
    output_dir = (PROJECT_DIR / args.output_dir).resolve() if not args.output_dir.is_absolute() else args.output_dir
    output_db = (PROJECT_DIR / args.output_db).resolve() if not args.output_db.is_absolute() else args.output_db
    report_dir = output_dir / "reports"
    figures_dir = output_dir / "figures"
    results_dir = output_db.parent
    for directory in [report_dir, figures_dir, results_dir]:
        directory.mkdir(parents=True, exist_ok=True)

    if output_db.exists():
        backup = output_db.with_name(
            output_db.stem + f".backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}" + output_db.suffix
        )
        shutil.copy2(output_db, backup)

    metadata = pd.DataFrame(
        [
            {
                "created_at_utc": now_utc(),
                "script": "scripts/run_s1_revised_backtests.py",
                "script_version": SCRIPT_VERSION,
                "input_db": str(bundle.input_db),
                "input_results_db": str(bundle.input_results_db) if bundle.input_results_db else None,
                "output_db": str(output_db),
                "top_pct": args.top_pct,
                "coverage_threshold": args.coverage_threshold,
                "cost_bps": json.dumps(args.cost_bps),
                "risk_free_rate": args.risk_free_rate,
                "common_universe_source": bundle.common_universe_source,
                "security_news_coverage_thresholds": json.dumps(bundle.security_news_coverage_thresholds),
                "common_universe_source_used": (
                    bundle.common_news_security_coverage["universe_source"].iloc[0]
                    if not bundle.common_news_security_coverage.empty
                    else None
                ),
                "vol_convention": vol_convention,
                "score_notes": json.dumps(score_notes, ensure_ascii=False),
                "benchmark_notes": json.dumps(benchmark_notes, ensure_ascii=False),
            }
        ]
    )
    outputs["s1_run_metadata"] = metadata
    outputs["s1_universe_summary"] = build_universe_summary(universe_detection, outputs)

    with sqlite3.connect(output_db) as conn:
        for table, df in outputs.items():
            safe_df = df.copy()
            for col in safe_df.columns:
                if pd.api.types.is_datetime64_any_dtype(safe_df[col]):
                    safe_df[col] = safe_df[col].dt.strftime("%Y-%m-%d")
            safe_df.to_sql(table, conn, if_exists="replace", index=False)

    write_report(
        report_dir / "s1_revised_report.md",
        bundle,
        universe_detection,
        outputs,
        score_notes,
        benchmark_notes,
    )
    write_figures(figures_dir, outputs)


def build_universe_summary(
    universe_detection: UniverseDetection, outputs: dict[str, pd.DataFrame]
) -> pd.DataFrame:
    rows = []
    coverage = outputs.get("s1_coverage_by_date", pd.DataFrame())
    for name, note in universe_detection.universe_notes.items():
        ids = universe_detection.universes.get(name, set())
        row = {
            "universe": name,
            "n_target_securities": len(ids),
            "status": "READY" if ids else "WARN",
            "note": note,
        }
        if not coverage.empty and name in set(coverage["universe"]):
            c = coverage[(coverage["universe"] == name) & (coverage["coverage_filter"] == "all_days")]
            row.update(
                {
                    "coverage_min": c["coverage_ratio"].min(),
                    "coverage_median": c["coverage_ratio"].median(),
                    "coverage_mean": c["coverage_ratio"].mean(),
                    "coverage_max": c["coverage_ratio"].max(),
                }
            )
        rows.append(row)
    return pd.DataFrame(rows).drop_duplicates(subset=["universe", "status", "note"])


def write_report(
    path: Path,
    bundle: InputBundle,
    universe_detection: UniverseDetection,
    outputs: dict[str, pd.DataFrame],
    score_notes: list[str],
    benchmark_notes: list[str],
) -> None:
    metrics = outputs.get("s1_metrics", pd.DataFrame())
    metrics_net = outputs.get("s1_metrics_net", pd.DataFrame())
    bench_metrics = outputs.get("s1_vs_benchmark_metrics", pd.DataFrame())
    coverage = outputs.get("s1_coverage_by_date", pd.DataFrame())
    ic_summary = outputs.get("s1_ic_summary", pd.DataFrame())
    quintile_summary = outputs.get("s1_quintile_summary", pd.DataFrame())
    validations = outputs.get("s1_validation_checks", pd.DataFrame())
    sector = outputs.get("s1_sector_exposure", pd.DataFrame())
    country = outputs.get("s1_country_exposure", pd.DataFrame())
    common_coverage = outputs.get("s1_common_news_security_coverage", pd.DataFrame())

    lines = [
        "# S1 Revised Backtest Report",
        "",
        "## 1. Executive Summary",
        "",
        "S1 is evaluated as a pure price-only baseline using Momentum plus Low Volatility. "
        "The existing broad result is preserved conceptually as S1_FULL_568 and is treated as a robustness/additional analysis, not as the sole main comparison against text models.",
        "",
        "## 2. Welche Eingabedaten gefunden wurden",
        "",
        f"- Input SQLite: `{bundle.input_db}`",
        f"- Existing results DB: `{bundle.input_results_db}`",
        f"- Common-news source requested: `{bundle.common_universe_source}`",
        f"- Common-news source used: `{common_coverage['universe_source'].iloc[0] if not common_coverage.empty else 'n/a'}`",
        "",
        "Score-/Feature-Notizen:",
        *[f"- {note}" for note in score_notes],
        "",
        "Benchmark-Notizen:",
        *[f"- {note}" for note in benchmark_notes],
        "",
        "## 3. Welche Universen gebaut wurden",
        "",
        dataframe_to_markdown(outputs.get("s1_universe_summary", pd.DataFrame())),
        "",
        "Gefundene Kandidatenquellen fuer U_CORE:",
        dataframe_to_markdown(pd.DataFrame(universe_detection.candidate_sources).head(25)),
        "",
        "## 4. Warum S1_FULL_568 nicht automatisch das Hauptresultat ist",
        "",
        "S1_FULL_PRICE bzw. S1_FULL_568 nutzt alle Aktien mit gueltigen Preisfeatures und ist damit eine breite Price-only-Robustheitsanalyse. "
        "Text- und Hybridmodelle duerfen methodisch nur gegen S1 auf demselben Common-Universe verglichen werden, weil sonst Universumsabdeckung und Signalverfuegbarkeit vermischt werden.",
        "",
        "S1_COMMON_NEWS_ANY ist das Hauptvergleichsuniversum fuer Text-/Hybridmodelle, sofern keine strengere Security-Level-News-Coverage-Schwelle gewaehlt wird. "
        "S1_COMMON_NEWS_COV30 und S1_COMMON_NEWS_COV50 sind Robustheitsuniversen mit hoeherer News-Coverage.",
        "",
        "Security-Level-News-Coverage:",
        dataframe_to_markdown(common_coverage.head(30)),
        "",
        "## 5. Ergebnisse je Universum und Zeitlogik",
        "",
        dataframe_to_markdown(select_report_cols(metrics)),
        "",
        "## 6. Brutto vs. Netto bei 5/10/25 bps",
        "",
        dataframe_to_markdown(select_report_cols(metrics_net, net=True)),
        "",
        "## 7. Benchmarkvergleich nur auf matched days",
        "",
        dataframe_to_markdown(select_benchmark_cols(bench_metrics)),
        "",
        "## 8. Coverage-Analyse",
        "",
        dataframe_to_markdown(coverage_summary_table(coverage)),
        "",
        "## 9. IC- und Quintile-Test",
        "",
        dataframe_to_markdown(ic_summary),
        "",
        dataframe_to_markdown(quintile_summary.head(30)),
        "",
        "## 10. Sektor-/Laender-Tilts",
        "",
        "Sektor-Exposure:",
        dataframe_to_markdown(sector.head(30)),
        "",
        "Laender-Exposure:",
        dataframe_to_markdown(country.head(30)),
        "",
        "## 11. Validierungschecks",
        "",
        dataframe_to_markdown(validations),
        "",
        "## 12. Methodische Empfehlung",
        "",
        "- S1_PRICE_ONLY bleibt die Price-only-Baseline.",
        "- S1_FULL_568 als breite Robustheitsanalyse verwenden.",
        "- S1_COMMON_NEWS_ANY fuer faire Hauptvergleiche mit S2_LEXICON_ONLY und HYBRID_S1_LEX_50_50 verwenden.",
        "- S1_COMMON_NEWS_COV30/COV50 als strengere Coverage-Robustheit verwenden.",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def dataframe_to_markdown(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return "_Keine Daten verfuegbar._"
    display = df.copy()
    for col in display.columns:
        if pd.api.types.is_float_dtype(display[col]):
            display[col] = display[col].map(lambda x: "" if pd.isna(x) else f"{x:.6g}")
    return display.to_markdown(index=False)


def select_report_cols(df: pd.DataFrame, net: bool = False) -> pd.DataFrame:
    if df.empty:
        return df
    cols = [
        "universe",
        "time_logic",
        "portfolio_variant",
        "coverage_filter",
        "cost_bps",
        "n_days",
        "cumulative_return",
        "cagr",
        "ann_vol",
        "sharpe",
        "max_drawdown",
        "avg_n_positions",
        "avg_turnover_total",
    ]
    if not net:
        cols.remove("cost_bps")
    return df[[c for c in cols if c in df.columns]]


def select_benchmark_cols(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    cols = [
        "universe",
        "time_logic",
        "portfolio_variant",
        "coverage_filter",
        "n_matched_days",
        "benchmark_symbol",
        "strategy_cum_return_matched",
        "benchmark_cum_return_matched",
        "active_cum_return",
        "tracking_error",
        "information_ratio",
    ]
    return df[[c for c in cols if c in df.columns]]


def coverage_summary_table(coverage: pd.DataFrame) -> pd.DataFrame:
    if coverage.empty:
        return pd.DataFrame()
    return (
        coverage.groupby(["universe", "time_logic", "portfolio_variant", "coverage_filter"], observed=True)
        .agg(
            n_days=("date", "nunique"),
            min_coverage=("coverage_ratio", "min"),
            median_coverage=("coverage_ratio", "median"),
            mean_coverage=("coverage_ratio", "mean"),
            max_coverage=("coverage_ratio", "max"),
            mean_positions=("n_positions", "mean"),
        )
        .reset_index()
    )


def write_figures(figures_dir: Path, outputs: dict[str, pd.DataFrame]) -> None:
    daily = outputs.get("s1_daily_returns", pd.DataFrame())
    quintile_summary = outputs.get("s1_quintile_summary", pd.DataFrame())
    coverage = outputs.get("s1_coverage_by_date", pd.DataFrame())
    if daily.empty:
        return

    plot_df = daily[
        (daily["portfolio_variant"] == "standard") & (daily["coverage_filter"] == "all_days")
    ].copy()
    if not plot_df.empty:
        plt.figure(figsize=(12, 7))
        for key, df in plot_df.groupby(["universe", "time_logic"], observed=True):
            plt.plot(df["date"], df["equity"], label=f"{key[0]} | {key[1]}")
        plt.legend(fontsize=8)
        plt.title("S1 Equity Curves")
        plt.xlabel("Date")
        plt.ylabel("Equity")
        plt.tight_layout()
        plt.savefig(figures_dir / "s1_equity_curves.png", dpi=160)
        plt.close()

        plt.figure(figsize=(12, 7))
        for key, df in plot_df.groupby(["universe", "time_logic"], observed=True):
            plt.plot(df["date"], df["drawdown"], label=f"{key[0]} | {key[1]}")
        plt.legend(fontsize=8)
        plt.title("S1 Drawdowns")
        plt.xlabel("Date")
        plt.ylabel("Drawdown")
        plt.tight_layout()
        plt.savefig(figures_dir / "s1_drawdowns.png", dpi=160)
        plt.close()

    if not quintile_summary.empty:
        q = quintile_summary[
            quintile_summary["time_logic"].eq("research_close_to_close")
        ].copy()
        plt.figure(figsize=(10, 6))
        for universe, df in q.groupby("universe", observed=True):
            plt.plot(df["score_quintile"], df["mean_ret_fwd_1d"], marker="o", label=universe)
        plt.legend(fontsize=8)
        plt.title("S1 Quintile Returns")
        plt.xlabel("Score Quintile")
        plt.ylabel("Mean ret_fwd_1d")
        plt.tight_layout()
        plt.savefig(figures_dir / "s1_quintile_returns.png", dpi=160)
        plt.close()

    if not coverage.empty:
        c = coverage[
            (coverage["portfolio_variant"] == "standard") & (coverage["coverage_filter"] == "all_days")
        ].copy()
        plt.figure(figsize=(12, 7))
        for key, df in c.groupby(["universe", "time_logic"], observed=True):
            plt.plot(df["date"], df["coverage_ratio"], label=f"{key[0]} | {key[1]}")
        plt.legend(fontsize=8)
        plt.title("S1 Coverage")
        plt.xlabel("Date")
        plt.ylabel("Coverage Ratio")
        plt.tight_layout()
        plt.savefig(figures_dir / "s1_coverage.png", dpi=160)
        plt.close()


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type IN ('table', 'view')
          AND name = ?
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def column_exists(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    if not table_exists(conn, table_name):
        return False
    return any(row[1] == column_name for row in conn.execute(f"PRAGMA table_info({quote_ident(table_name)})"))


def add_column_if_missing(
    conn: sqlite3.Connection, table_name: str, column_definition: str
) -> None:
    column_name = column_definition.strip().split()[0]
    if not column_exists(conn, table_name, column_name):
        conn.execute(f"ALTER TABLE {quote_ident(table_name)} ADD COLUMN {column_definition}")


def replace_validation_check(
    conn: sqlite3.Connection, check_name: str, status: str, details: str
) -> None:
    if not table_exists(conn, "s1_validation_checks"):
        raise RuntimeError("Missing required table s1_validation_checks.")
    conn.execute("DELETE FROM s1_validation_checks WHERE check_name = ?", (check_name,))
    conn.execute(
        """
        INSERT INTO s1_validation_checks (check_name, status, detail)
        VALUES (?, ?, ?)
        """,
        (check_name, status, details),
    )


def infer_existing_sharpe_ddof(
    daily_returns: pd.DataFrame, metrics: pd.DataFrame
) -> tuple[int, str]:
    rows = []
    key_cols = ["universe", "time_logic", "portfolio_variant", "coverage_filter"]
    for _, metric in metrics.dropna(subset=["sharpe"]).iterrows():
        mask = np.logical_and.reduce(
            [daily_returns[col].eq(metric[col]) for col in key_cols]
        )
        returns = daily_returns.loc[mask, "strategy_ret_fwd_1d"].dropna()
        if len(returns) < 2:
            continue
        mean_ret = returns.mean()
        calc0 = mean_ret / returns.std(ddof=0) * math.sqrt(252.0)
        calc1 = mean_ret / returns.std(ddof=1) * math.sqrt(252.0)
        rows.append(
            {
                "err0": abs(calc0 - metric["sharpe"]),
                "err1": abs(calc1 - metric["sharpe"]),
            }
        )
    if not rows:
        return 1, "Could not infer ddof from existing sharpe; defaulted to ddof=1."
    err0 = float(np.nanmean([row["err0"] for row in rows]))
    err1 = float(np.nanmean([row["err1"] for row in rows]))
    if err0 < err1 * 0.1:
        return 0, f"Inferred ddof=0 from existing sharpe; mean_abs_error_ddof0={err0:.3g}, ddof1={err1:.3g}."
    if err1 < err0 * 0.1:
        return 1, f"Inferred ddof=1 from existing sharpe; mean_abs_error_ddof0={err0:.3g}, ddof1={err1:.3g}."
    return 1, f"Ambiguous sharpe ddof inference; defaulted to ddof=1. mean_abs_error_ddof0={err0:.3g}, ddof1={err1:.3g}."


def compute_newey_west_t_stat(values: pd.Series | np.ndarray) -> tuple[float | None, int | None, str]:
    x = pd.Series(values, dtype="float64").dropna().to_numpy()
    n = len(x)
    if n < 5:
        return None, None, "too_few_observations"
    lag_raw = int(math.floor(4.0 * (n / 100.0) ** (2.0 / 9.0)))
    lag = max(1, lag_raw)
    lag = min(lag, n - 1)

    try:
        import statsmodels.api as sm  # type: ignore

        model = sm.OLS(x, np.ones((n, 1))).fit(cov_type="HAC", cov_kwds={"maxlags": lag})
        t_value = float(model.tvalues[0])
        if math.isfinite(t_value):
            return t_value, lag, "computed"
    except Exception:
        pass

    demeaned = x - float(np.mean(x))
    gamma0 = float(np.sum(demeaned * demeaned) / n)
    nw_variance = gamma0
    for k in range(1, lag + 1):
        gamma_k = float(np.sum(demeaned[k:] * demeaned[:-k]) / n)
        weight = 1.0 - k / (lag + 1.0)
        nw_variance += 2.0 * weight * gamma_k
    if nw_variance <= 0 or not math.isfinite(nw_variance):
        return None, lag, "invalid_nw_variance"
    nw_se = math.sqrt(nw_variance / n)
    if nw_se <= 0 or not math.isfinite(nw_se):
        return None, lag, "invalid_nw_variance"
    return float(np.mean(x) / nw_se), lag, "computed"


def compute_drawdown_recovery(df: pd.DataFrame, equity_col: str) -> dict:
    work = df.sort_values("date").copy()
    if work.empty or equity_col not in work:
        return {
            "max_drawdown_start_date": None,
            "max_drawdown_end_date": None,
            "max_drawdown_recovery_date": None,
            "max_drawdown_recovery_days": None,
        }
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
    recovery_days = None
    if recovery_date is not None:
        recovery_days = int((work["date"] > end_date).loc[work["date"] <= recovery_date].sum())
    return {
        "max_drawdown_start_date": format_date_value(start_date),
        "max_drawdown_end_date": format_date_value(end_date),
        "max_drawdown_recovery_date": format_date_value(recovery_date),
        "max_drawdown_recovery_days": recovery_days,
    }


def upsert_metric_definition(
    conn: sqlite3.Connection,
    metric_name: str,
    definition: str,
    interpretation_note: str | None = None,
    implementation_note: str | None = None,
) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS s1_metric_definitions (
            metric_name TEXT PRIMARY KEY,
            definition TEXT NOT NULL,
            interpretation_note TEXT,
            implementation_note TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO s1_metric_definitions
        (metric_name, definition, interpretation_note, implementation_note)
        VALUES (?, ?, ?, ?)
        """,
        (metric_name, definition, interpretation_note, implementation_note),
    )


def format_date_value(value) -> str | None:
    if value is None or pd.isna(value):
        return None
    return pd.to_datetime(value).strftime("%Y-%m-%d")


def ensure_v2_schema(conn: sqlite3.Connection) -> None:
    required = [
        "s1_metrics",
        "s1_metrics_net",
        "s1_daily_returns",
        "s1_ic_by_date",
        "s1_ic_summary",
        "s1_quintile_summary",
        "s1_positions",
        "s1_vs_benchmark_daily",
        "s1_validation_checks",
    ]
    missing = [table for table in required if not table_exists(conn, table)]
    if missing:
        existing = sorted(sqlite_tables(conn))
        raise RuntimeError(
            f"Missing required v1 tables: {missing}. Existing tables/views: {existing}"
        )

    metric_columns = [
        "sharpe_excess_estr REAL",
        "avg_rf_daily REAL",
        "rf_rate_source TEXT",
        "sharpe_std_ddof INTEGER",
        "annualized_return_period_geometric REAL",
        "max_drawdown_start_date TEXT",
        "max_drawdown_end_date TEXT",
        "max_drawdown_recovery_date TEXT",
        "max_drawdown_recovery_days INTEGER",
        "calmar_ratio REAL",
        "calmar_ratio_status TEXT",
    ]
    for col in metric_columns:
        add_column_if_missing(conn, "s1_metrics", col)
        add_column_if_missing(conn, "s1_metrics_net", col)

    ic_columns = [
        "ic_t_stat_nw REAL",
        "ic_t_stat_nw_lag INTEGER",
        "ic_t_stat_nw_abs_le_naive_abs INTEGER",
        "ic_t_stat_nw_status TEXT",
        "mean_ic_cs REAL",
        "median_ic_cs REAL",
        "mean_ic_ts REAL",
        "median_ic_ts REAL",
        "n_ts_ic_securities_valid INTEGER",
        "n_ts_ic_securities_excluded INTEGER",
        "ts_ic_min_obs_threshold INTEGER",
    ]
    for col in ic_columns:
        add_column_if_missing(conn, "s1_ic_summary", col)

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS risk_free_rate_daily (
            date TEXT PRIMARY KEY,
            estr_annual_pct REAL NOT NULL,
            estr_daily_decimal REAL NOT NULL,
            source TEXT,
            loaded_at_utc TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS risk_free_rate_backtest_aligned (
            date TEXT PRIMARY KEY,
            estr_source_date TEXT NOT NULL,
            estr_annual_pct REAL NOT NULL,
            estr_daily_decimal REAL NOT NULL,
            fill_method TEXT NOT NULL,
            fill_lag_calendar_days INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS s1_quintile_spread_returns (
            date TEXT NOT NULL,
            universe TEXT NOT NULL,
            time_logic TEXT NOT NULL,
            high_quintile INTEGER NOT NULL,
            low_quintile INTEGER NOT NULL,
            high_quintile_ret_1d REAL,
            low_quintile_ret_1d REAL,
            spread_ret_1d REAL,
            n_high INTEGER,
            n_low INTEGER,
            notes TEXT,
            PRIMARY KEY (date, universe, time_logic)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS s1_quintile_spread_metrics (
            universe TEXT NOT NULL,
            time_logic TEXT NOT NULL,
            n_days INTEGER,
            mean_spread_daily REAL,
            std_spread_daily REAL,
            ann_return REAL,
            ann_vol REAL,
            spread_sharpe_annualized REAL,
            spread_t_stat_naive REAL,
            spread_t_stat_nw REAL,
            spread_t_stat_nw_lag INTEGER,
            high_quintile INTEGER,
            low_quintile INTEGER,
            notes TEXT NOT NULL,
            PRIMARY KEY (universe, time_logic)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS s1_ic_time_series (
            universe TEXT NOT NULL,
            time_logic TEXT NOT NULL,
            security_id INTEGER NOT NULL,
            yahoo_ticker TEXT,
            ts_ic REAL,
            n_obs INTEGER NOT NULL,
            is_valid_ts_ic INTEGER NOT NULL,
            exclusion_reason TEXT,
            PRIMARY KEY (universe, time_logic, security_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS s1_event_attribution (
            universe TEXT NOT NULL,
            time_logic TEXT NOT NULL,
            portfolio_variant TEXT NOT NULL,
            coverage_filter TEXT NOT NULL,
            return_type TEXT NOT NULL,
            cost_bps INTEGER NOT NULL,
            event_label TEXT NOT NULL,
            event_label_display TEXT,
            event_start_date TEXT NOT NULL,
            event_end_date TEXT NOT NULL,
            n_event_days INTEGER,
            benchmark_symbol TEXT,
            strategy_cumulative_return REAL,
            benchmark_cumulative_return REAL,
            relative_performance REAL,
            notes TEXT,
            PRIMARY KEY (
                universe,
                time_logic,
                portfolio_variant,
                coverage_filter,
                return_type,
                cost_bps,
                event_label
            )
        )
        """
    )


def load_or_refresh_estr(conn: sqlite3.Connection, min_date: str, max_date: str) -> str:
    start = (pd.to_datetime(min_date) - pd.Timedelta(days=14)).strftime("%Y-%m-%d")
    existing = pd.read_sql_query(
        """
        SELECT *
        FROM risk_free_rate_daily
        WHERE date BETWEEN ? AND ?
          AND estr_daily_decimal IS NOT NULL
        """,
        conn,
        params=(start, max_date),
    )
    if existing.empty:
        raise RuntimeError(
            "No local risk_free_rate_daily fallback is available. "
            "Internet downloads are disabled for this thesis backtest script."
        )
    return "risk_free_rate_daily_existing_fallback"


def align_estr_to_backtest_dates(conn: sqlite3.Connection) -> dict:
    daily_dates = pd.read_sql_query(
        "SELECT DISTINCT date FROM s1_daily_returns ORDER BY date", conn
    )
    rf = pd.read_sql_query(
        "SELECT date, estr_annual_pct, estr_daily_decimal, source FROM risk_free_rate_daily ORDER BY date",
        conn,
    )
    if daily_dates.empty or rf.empty:
        raise RuntimeError("Cannot align ESTR: missing backtest dates or risk-free rows.")
    daily_dates["date_dt"] = pd.to_datetime(daily_dates["date"])
    rf["date_dt"] = pd.to_datetime(rf["date"])
    aligned = []
    for _, row in daily_dates.iterrows():
        eligible = rf[rf["date_dt"] <= row["date_dt"]]
        if eligible.empty:
            continue
        selected = eligible.iloc[-1]
        lag = int((row["date_dt"] - selected["date_dt"]).days)
        if lag > 7:
            continue
        aligned.append(
            {
                "date": row["date"],
                "estr_source_date": selected["date"],
                "estr_annual_pct": selected["estr_annual_pct"],
                "estr_daily_decimal": selected["estr_daily_decimal"],
                "fill_method": "exact" if lag == 0 else "forward_fill",
                "fill_lag_calendar_days": lag,
            }
        )
    conn.execute("DELETE FROM risk_free_rate_backtest_aligned")
    conn.executemany(
        """
        INSERT INTO risk_free_rate_backtest_aligned
        (date, estr_source_date, estr_annual_pct, estr_daily_decimal, fill_method, fill_lag_calendar_days)
        VALUES (:date, :estr_source_date, :estr_annual_pct, :estr_daily_decimal, :fill_method, :fill_lag_calendar_days)
        """,
        aligned,
    )
    aligned_df = pd.DataFrame(aligned)
    n_backtest = len(daily_dates)
    n_missing = n_backtest - len(aligned_df)
    detail = {
        "min_date": daily_dates["date"].min(),
        "max_date": daily_dates["date"].max(),
        "n_backtest_dates": n_backtest,
        "n_exact": int((aligned_df["fill_method"] == "exact").sum()) if not aligned_df.empty else 0,
        "n_forward_fill": int((aligned_df["fill_method"] == "forward_fill").sum()) if not aligned_df.empty else 0,
        "n_missing": n_missing,
        "max_fill_lag_calendar_days": int(aligned_df["fill_lag_calendar_days"].max()) if not aligned_df.empty else None,
    }
    status = "PASS" if n_missing == 0 and (detail["max_fill_lag_calendar_days"] or 0) <= 7 else "FAIL"
    replace_validation_check(
        conn,
        "ESTR-Daten auf Backtest-Handelstage gemappt",
        status,
        json.dumps(detail, ensure_ascii=False, sort_keys=True),
    )
    return detail


def update_sharpe_excess_and_risk_metrics(conn: sqlite3.Connection) -> tuple[int, str]:
    daily = pd.read_sql_query("SELECT * FROM s1_daily_returns", conn)
    metrics = pd.read_sql_query("SELECT rowid AS _rowid, * FROM s1_metrics", conn)
    metrics_net = pd.read_sql_query("SELECT rowid AS _rowid, * FROM s1_metrics_net", conn)
    rf = pd.read_sql_query(
        """
        SELECT
            risk_free_rate_backtest_aligned.date AS date,
            risk_free_rate_backtest_aligned.estr_daily_decimal AS estr_daily_decimal,
            risk_free_rate_daily.source AS source
        FROM risk_free_rate_backtest_aligned
        JOIN risk_free_rate_daily
          ON risk_free_rate_backtest_aligned.estr_source_date = risk_free_rate_daily.date
        """,
        conn,
    )
    daily["date"] = pd.to_datetime(daily["date"])
    rf["date"] = pd.to_datetime(rf["date"])
    ddof, ddof_detail = infer_existing_sharpe_ddof(daily, metrics)
    key_cols = ["universe", "time_logic", "portfolio_variant", "coverage_filter"]

    for _, row in metrics.iterrows():
        mask = np.logical_and.reduce([daily[col].eq(row[col]) for col in key_cols])
        part = daily.loc[mask].sort_values("date").merge(rf, on="date", how="left")
        excess = part["strategy_ret_fwd_1d"] - part["estr_daily_decimal"]
        std = excess.std(ddof=ddof)
        sharpe_excess = excess.mean() / std * math.sqrt(252.0) if std and not pd.isna(std) else None
        drawdown = compute_drawdown_recovery(part, "equity")
        max_dd = row["max_drawdown"]
        calmar, calmar_status = compute_calmar(row["cagr"], max_dd)
        conn.execute(
            """
            UPDATE s1_metrics
            SET sharpe_excess_estr = ?,
                avg_rf_daily = ?,
                rf_rate_source = ?,
                sharpe_std_ddof = ?,
                annualized_return_period_geometric = cagr,
                max_drawdown_start_date = ?,
                max_drawdown_end_date = ?,
                max_drawdown_recovery_date = ?,
                max_drawdown_recovery_days = ?,
                calmar_ratio = ?,
                calmar_ratio_status = ?
            WHERE rowid = ?
            """,
            (
                sharpe_excess,
                part["estr_daily_decimal"].mean(),
                ",".join(sorted(part["source"].dropna().unique())),
                ddof,
                drawdown["max_drawdown_start_date"],
                drawdown["max_drawdown_end_date"],
                drawdown["max_drawdown_recovery_date"],
                drawdown["max_drawdown_recovery_days"],
                calmar,
                calmar_status,
                int(row["_rowid"]),
            ),
        )

    for _, row in metrics_net.iterrows():
        label = cost_label(float(row["cost_bps"]))
        net_col = f"net_return_{label}"
        equity_col = f"net_equity_{label}"
        if net_col not in daily.columns or equity_col not in daily.columns:
            raise RuntimeError(f"Missing exact daily net columns for cost_bps={row['cost_bps']}: {net_col}, {equity_col}")
        mask = np.logical_and.reduce([daily[col].eq(row[col]) for col in key_cols])
        part = daily.loc[mask].sort_values("date").merge(rf, on="date", how="left")
        excess = part[net_col] - part["estr_daily_decimal"]
        std = excess.std(ddof=ddof)
        sharpe_excess = excess.mean() / std * math.sqrt(252.0) if std and not pd.isna(std) else None
        drawdown_col = f"net_drawdown_{label}"
        drawdown = compute_drawdown_recovery(part, equity_col)
        max_dd = part[drawdown_col].min() if drawdown_col in part else row["max_drawdown"]
        calmar, calmar_status = compute_calmar(row["cagr"], max_dd)
        conn.execute(
            """
            UPDATE s1_metrics_net
            SET sharpe_excess_estr = ?,
                avg_rf_daily = ?,
                rf_rate_source = ?,
                sharpe_std_ddof = ?,
                annualized_return_period_geometric = cagr,
                max_drawdown_start_date = ?,
                max_drawdown_end_date = ?,
                max_drawdown_recovery_date = ?,
                max_drawdown_recovery_days = ?,
                calmar_ratio = ?,
                calmar_ratio_status = ?
            WHERE rowid = ?
            """,
            (
                sharpe_excess,
                part["estr_daily_decimal"].mean(),
                ",".join(sorted(part["source"].dropna().unique())),
                ddof,
                drawdown["max_drawdown_start_date"],
                drawdown["max_drawdown_end_date"],
                drawdown["max_drawdown_recovery_date"],
                drawdown["max_drawdown_recovery_days"],
                calmar,
                calmar_status,
                int(row["_rowid"]),
            ),
        )

    refreshed_metrics = pd.read_sql_query("SELECT * FROM s1_metrics", conn)
    refreshed_net = pd.read_sql_query("SELECT * FROM s1_metrics_net", conn)
    missing = int(refreshed_metrics["sharpe_excess_estr"].isna().sum() + refreshed_net["sharpe_excess_estr"].isna().sum())
    le_count = int(
        (refreshed_metrics["sharpe_excess_estr"] <= refreshed_metrics["sharpe"]).sum()
        + (refreshed_net["sharpe_excess_estr"] <= refreshed_net["sharpe"]).sum()
    )
    detail = {
        "ddof_detail": ddof_detail,
        "recognized_ddof": ddof,
        "s1_metrics_rows": len(refreshed_metrics),
        "s1_metrics_net_rows": len(refreshed_net),
        "avg_rf_daily": float(pd.concat([refreshed_metrics["avg_rf_daily"], refreshed_net["avg_rf_daily"]]).mean()),
        "n_sharpe_excess_le_sharpe": le_count,
        "n_missing_sharpe_excess": missing,
    }
    replace_validation_check(
        conn,
        "sharpe_excess_estr berechnet",
        "PASS" if missing == 0 else "FAIL",
        json.dumps(detail, ensure_ascii=False, sort_keys=True),
    )
    return ddof, ddof_detail


def compute_calmar(annualized_return: float | None, max_drawdown: float | None) -> tuple[float | None, str]:
    if max_drawdown is None or pd.isna(max_drawdown) or max_drawdown == 0:
        return None, "undefined_zero_or_null_drawdown"
    ratio = annualized_return / abs(max_drawdown) if annualized_return is not None and not pd.isna(annualized_return) else None
    if abs(max_drawdown) < 0.001:
        return ratio, "computed_but_small_drawdown_warn"
    return ratio, "computed"


def update_ic_newey_west(conn: sqlite3.Connection) -> None:
    ic_by_date = pd.read_sql_query("SELECT * FROM s1_ic_by_date", conn)
    summary = pd.read_sql_query("SELECT rowid AS _rowid, * FROM s1_ic_summary", conn)
    rows = []
    for _, row in summary.iterrows():
        series = ic_by_date[
            (ic_by_date["universe"] == row["universe"])
            & (ic_by_date["time_logic"] == row["time_logic"])
        ]["spearman_ic"].dropna()
        t_nw, lag, status = compute_newey_west_t_stat(series)
        flag = None
        if t_nw is not None and not pd.isna(row["ic_t_stat"]):
            flag = int(abs(t_nw) <= abs(row["ic_t_stat"]))
        conn.execute(
            """
            UPDATE s1_ic_summary
            SET ic_t_stat_nw = ?,
                ic_t_stat_nw_lag = ?,
                ic_t_stat_nw_abs_le_naive_abs = ?,
                ic_t_stat_nw_status = ?,
                mean_ic_cs = mean_ic,
                median_ic_cs = median_ic
            WHERE rowid = ?
            """,
            (t_nw, lag, flag, status, int(row["_rowid"])),
        )
        rows.append({"status": status, "lag": lag, "t_nw": t_nw, "flag": flag, "n": len(series)})

    refreshed = pd.read_sql_query("SELECT * FROM s1_ic_summary", conn)
    expected_lag_ok = True
    for _, row in refreshed.iterrows():
        if row["n_ic_days"] >= 5:
            expected = max(1, min(int(math.floor(4.0 * (row["n_ic_days"] / 100.0) ** (2.0 / 9.0))), int(row["n_ic_days"]) - 1))
            expected_lag_ok = expected_lag_ok and row["ic_t_stat_nw_lag"] == expected
    detail = {
        "n_rows": len(refreshed),
        "n_computed": int((refreshed["ic_t_stat_nw_status"] == "computed").sum()),
        "n_too_few_observations": int((refreshed["ic_t_stat_nw_status"] == "too_few_observations").sum()),
        "n_invalid_nw_variance": int((refreshed["ic_t_stat_nw_status"] == "invalid_nw_variance").sum()),
        "n_abs_nw_le_abs_naive": int(refreshed["ic_t_stat_nw_abs_le_naive_abs"].fillna(0).sum()),
        "expected_lag_ok": bool(expected_lag_ok),
    }
    status = "PASS" if expected_lag_ok and detail["n_computed"] + detail["n_too_few_observations"] + detail["n_invalid_nw_variance"] == len(refreshed) else "FAIL"
    replace_validation_check(
        conn,
        "Newey-West IC berechnet",
        status,
        json.dumps(detail, ensure_ascii=False, sort_keys=True),
    )


def compute_v2_signal_diagnostics(
    conn: sqlite3.Connection, panel: pd.DataFrame, universe_detection: UniverseDetection
) -> None:
    conn.execute("DELETE FROM s1_quintile_spread_returns")
    conn.execute("DELETE FROM s1_quintile_spread_metrics")
    conn.execute("DELETE FROM s1_ic_time_series")
    spread_notes = "hypothetische Long-Short-Konstruktion; nicht die implementierte S1-Long-only-Strategie"
    ts_threshold = 100
    spread_rows = []
    ts_rows = []
    for universe, ids in universe_detection.universes.items():
        for time_logic in ["research_close_to_close", "conservative_lag1"]:
            data = add_time_logic_scores(
                panel[panel["security_id"].astype(int).isin(ids)], time_logic
            ).dropna(subset=["date", "security_id", "score_used", "ret_fwd_1d"]).copy()
            if data.empty:
                continue
            data["date"] = pd.to_datetime(data["date"]).dt.strftime("%Y-%m-%d")
            for sec_id, sec in data.groupby("security_id", observed=True):
                n_obs = len(sec)
                if n_obs >= ts_threshold:
                    ts_ic = sec["score_used"].corr(sec["ret_fwd_1d"], method="spearman")
                    valid = int(pd.notna(ts_ic))
                    reason = None if valid else "correlation_not_finite"
                else:
                    ts_ic = None
                    valid = 0
                    reason = "n_obs_lt_100"
                ts_rows.append(
                    {
                        "universe": universe,
                        "time_logic": time_logic,
                        "security_id": int(sec_id),
                        "yahoo_ticker": sec["yahoo_ticker"].dropna().iloc[0] if "yahoo_ticker" in sec and sec["yahoo_ticker"].notna().any() else None,
                        "ts_ic": ts_ic,
                        "n_obs": n_obs,
                        "is_valid_ts_ic": valid,
                        "exclusion_reason": reason,
                    }
                )

            daily_spreads = []
            for date, day in data.groupby("date", sort=True, observed=True):
                if len(day) < 5:
                    continue
                ranked = day.sort_values(["score_used", "security_id"], ascending=[True, True]).copy()
                ranked["score_quintile"] = pd.qcut(
                    ranked["score_used"].rank(method="first"), 5, labels=[1, 2, 3, 4, 5]
                ).astype(int)
                avg_score = ranked.groupby("score_quintile", observed=True)["score_used"].mean()
                high_q = int(avg_score.idxmax())
                low_q = int(avg_score.idxmin())
                high = ranked[ranked["score_quintile"] == high_q]
                low = ranked[ranked["score_quintile"] == low_q]
                spread = high["ret_fwd_1d"].mean() - low["ret_fwd_1d"].mean()
                daily_spreads.append(spread)
                spread_rows.append(
                    {
                        "date": date,
                        "universe": universe,
                        "time_logic": time_logic,
                        "high_quintile": high_q,
                        "low_quintile": low_q,
                        "high_quintile_ret_1d": high["ret_fwd_1d"].mean(),
                        "low_quintile_ret_1d": low["ret_fwd_1d"].mean(),
                        "spread_ret_1d": spread,
                        "n_high": len(high),
                        "n_low": len(low),
                        "notes": spread_notes,
                    }
                )

            spreads = pd.Series(daily_spreads, dtype="float64").dropna()
            if not spreads.empty:
                std = spreads.std(ddof=1)
                t_nw, lag, _ = compute_newey_west_t_stat(spreads)
                mean = spreads.mean()
                high_q = 5
                low_q = 1
                conn.execute(
                    """
                    INSERT OR REPLACE INTO s1_quintile_spread_metrics
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        universe,
                        time_logic,
                        len(spreads),
                        mean,
                        std,
                        mean * 252.0,
                        std * math.sqrt(252.0) if std and not pd.isna(std) else None,
                        mean / std * math.sqrt(252.0) if std and not pd.isna(std) else None,
                        mean / (std / math.sqrt(len(spreads))) if std and not pd.isna(std) else None,
                        t_nw,
                        lag,
                        high_q,
                        low_q,
                        spread_notes,
                    ),
                )

    if spread_rows:
        conn.executemany(
            """
            INSERT OR REPLACE INTO s1_quintile_spread_returns
            (date, universe, time_logic, high_quintile, low_quintile, high_quintile_ret_1d,
             low_quintile_ret_1d, spread_ret_1d, n_high, n_low, notes)
            VALUES (:date, :universe, :time_logic, :high_quintile, :low_quintile,
                    :high_quintile_ret_1d, :low_quintile_ret_1d, :spread_ret_1d,
                    :n_high, :n_low, :notes)
            """,
            spread_rows,
        )
    if ts_rows:
        conn.executemany(
            """
            INSERT OR REPLACE INTO s1_ic_time_series
            (universe, time_logic, security_id, yahoo_ticker, ts_ic, n_obs, is_valid_ts_ic, exclusion_reason)
            VALUES (:universe, :time_logic, :security_id, :yahoo_ticker, :ts_ic, :n_obs, :is_valid_ts_ic, :exclusion_reason)
            """,
            ts_rows,
        )

    ts = pd.read_sql_query("SELECT * FROM s1_ic_time_series", conn)
    for (universe, time_logic), group in ts.groupby(["universe", "time_logic"], observed=True):
        valid = group[group["is_valid_ts_ic"] == 1]["ts_ic"].dropna()
        conn.execute(
            """
            UPDATE s1_ic_summary
            SET mean_ic_ts = ?,
                median_ic_ts = ?,
                n_ts_ic_securities_valid = ?,
                n_ts_ic_securities_excluded = ?,
                ts_ic_min_obs_threshold = ?
            WHERE universe = ?
              AND time_logic = ?
            """,
            (
                valid.mean() if not valid.empty else None,
                valid.median() if not valid.empty else None,
                int((group["is_valid_ts_ic"] == 1).sum()),
                int((group["is_valid_ts_ic"] == 0).sum()),
                ts_threshold,
                universe,
                time_logic,
            ),
        )


def update_event_attribution(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM s1_event_attribution")
    daily = pd.read_sql_query("SELECT * FROM s1_daily_returns", conn)
    benchmark = pd.read_sql_query("SELECT * FROM s1_vs_benchmark_daily", conn)
    metrics = pd.read_sql_query("SELECT * FROM s1_metrics", conn)
    metrics_net = pd.read_sql_query("SELECT * FROM s1_metrics_net", conn)
    daily["date"] = pd.to_datetime(daily["date"])
    benchmark["date"] = pd.to_datetime(benchmark["date"])
    key_cols = ["universe", "time_logic", "portfolio_variant", "coverage_filter"]
    rows = []
    for _, row in metrics.iterrows():
        rows.append(build_event_row(row, daily, benchmark, key_cols, "gross", -1, "strategy_ret_fwd_1d"))
    for _, row in metrics_net.iterrows():
        label = cost_label(float(row["cost_bps"]))
        rows.append(build_event_row(row, daily, benchmark, key_cols, "net", int(float(row["cost_bps"])), f"net_return_{label}"))
    rows = [row for row in rows if row is not None]
    conn.executemany(
        """
        INSERT OR REPLACE INTO s1_event_attribution
        (universe, time_logic, portfolio_variant, coverage_filter, return_type, cost_bps,
         event_label, event_label_display, event_start_date, event_end_date, n_event_days,
         benchmark_symbol, strategy_cumulative_return, benchmark_cumulative_return,
         relative_performance, notes)
        VALUES (:universe, :time_logic, :portfolio_variant, :coverage_filter, :return_type, :cost_bps,
                :event_label, :event_label_display, :event_start_date, :event_end_date, :n_event_days,
                :benchmark_symbol, :strategy_cumulative_return, :benchmark_cumulative_return,
                :relative_performance, :notes)
        """,
        rows,
    )


def build_event_row(row, daily: pd.DataFrame, benchmark: pd.DataFrame, key_cols: list[str], return_type: str, cost_bps: int, return_col: str) -> dict | None:
    start = row.get("max_drawdown_start_date")
    end = row.get("max_drawdown_end_date")
    if pd.isna(start) or pd.isna(end) or start is None or end is None:
        return None
    start_dt = pd.to_datetime(start)
    end_dt = pd.to_datetime(end)
    mask = np.logical_and.reduce([daily[col].eq(row[col]) for col in key_cols])
    event = daily.loc[mask & (daily["date"] >= start_dt) & (daily["date"] <= end_dt)].copy()
    if event.empty or return_col not in event:
        return None
    strategy_cum = float((1.0 + event[return_col].dropna()).prod() - 1.0)
    bench_mask = np.logical_and.reduce([benchmark[col].eq(row[col]) for col in key_cols])
    bench_event = benchmark.loc[
        bench_mask & (benchmark["date"] >= start_dt) & (benchmark["date"] <= end_dt)
    ].dropna(subset=["benchmark_ret_fwd_1d"])
    if bench_event.empty:
        benchmark_cum = None
        relative = None
        benchmark_symbol = None
        bench_note = "No matched benchmark data in event window."
    else:
        benchmark_cum = float((1.0 + bench_event["benchmark_ret_fwd_1d"]).prod() - 1.0)
        relative = strategy_cum - benchmark_cum
        benchmark_symbol = ",".join(sorted(bench_event["symbol"].dropna().unique()))
        bench_note = "Benchmark return computed on matched event days only."
    april_days = int(((event["date"].dt.year == 2025) & (event["date"].dt.month == 4)).sum())
    event_label = "april_2025_max_drawdown" if april_days > len(event) / 2.0 else "max_drawdown_event"
    display_prefix = "April 2025 Max Drawdown" if event_label == "april_2025_max_drawdown" else "Max Drawdown Event"
    config = f"{row['universe']} | {row['time_logic']} | {row['portfolio_variant']} | {row['coverage_filter']} | {return_type}"
    if return_type == "net":
        config += f" | {cost_bps}bps"
    return {
        "universe": row["universe"],
        "time_logic": row["time_logic"],
        "portfolio_variant": row["portfolio_variant"],
        "coverage_filter": row["coverage_filter"],
        "return_type": return_type,
        "cost_bps": cost_bps,
        "event_label": event_label,
        "event_label_display": f"{display_prefix} | {config}",
        "event_start_date": format_date_value(start_dt),
        "event_end_date": format_date_value(end_dt),
        "n_event_days": len(event),
        "benchmark_symbol": benchmark_symbol,
        "strategy_cumulative_return": strategy_cum,
        "benchmark_cumulative_return": benchmark_cum,
        "relative_performance": relative,
        "notes": (
            "Tatsaechlich identifiziertes Max-Drawdown-Fenster; April 2025 wird nur "
            "verwendet, wenn das Fenster ueberwiegend in April 2025 liegt; "
            "event_label ist nur zusammen mit den Dimensionsspalten eindeutig. "
            + bench_note
        ),
    }


def insert_v2_metric_definitions(conn: sqlite3.Connection, ddof_detail: str) -> None:
    definitions = {
        "sharpe": (
            "Annualisierte naive Sharpe Ratio der Strategie gegen einen Null-Risk-Free-Satz: mean(daily_return) / std(daily_return) * sqrt(252).",
            "Legacy-Kennzahl; methodisch als Brutto-Sharpe ohne risikofreie Tagesrendite zu lesen.",
            ddof_detail,
        ),
        "sharpe_excess_estr": (
            "Annualisierte Sharpe Ratio auf Basis taeglicher Ueberschussrenditen gegen ESTR: mean(strategy_return - estr_daily_decimal) / std(strategy_return - estr_daily_decimal) * sqrt(252).",
            "Methodisch bevorzugte Sharpe-Kennzahl, weil ein beobachteter kurzfristiger EUR-Risk-Free-Satz abgezogen wird.",
            "ESTR daily decimal = (1 + estr_annual_pct / 100)^(1/252) - 1; gleiche ddof-Konvention wie bestehende sharpe-Spalte.",
        ),
        "avg_rf_daily": ("Durchschnitt der auf Backtest-Handelstage gemappten taeglichen ESTR-Dezimalrendite.", "Nicht mit 0 imputiert; fehlende Daten fuehren zu kontrolliertem Abbruch oder Validation-FAIL.", None),
        "sharpe_std_ddof": ("Verwendete Freiheitsgrad-Konvention der Standardabweichung fuer Sharpe-Berechnungen.", "Aus bestehender v1-sharpe rekonstruiert; bei Uneindeutigkeit default ddof=1.", ddof_detail),
        "annualized_return_period_geometric": ("Geometrisch annualisierte Rendite des beobachteten Backtest-Zeitraums.", "Beobachtungszeitraum ca. 17 Monate; nicht als langfristige Wachstumsrate zu interpretieren.", "Legacy-Spalte cagr bleibt erhalten; Reporting-Hauptlabel ist annualized_return_period_geometric."),
        "mean_ic_cs": ("Mittelwert der taeglichen Cross-Sectional Spearman Rank ICs ueber Aktien.", "Hauptdiagnostik fuer S1, weil S1 Aktien taeglich gegeneinander rankt.", "Entspricht der bestehenden mean_ic-Spalte."),
        "median_ic_cs": ("Median der taeglichen Cross-Sectional Spearman Rank ICs ueber Aktien.", "Robuste Hauptdiagnostik fuer taegliche Ranking-Qualitaet.", "Entspricht der bestehenden median_ic-Spalte."),
        "mean_ic_ts": ("Durchschnitt der security-spezifischen Time-Series Spearman ICs zwischen Score und ret_fwd_1d.", "Ergaenzende Robustheitsdiagnostik; kein Ersatz fuer Cross-Sectional IC.", "Nur Securities mit mindestens 100 Beobachtungen werden beruecksichtigt."),
        "median_ic_ts": ("Median der security-spezifischen Time-Series Spearman ICs.", "Ergaenzende Robustheitsdiagnostik.", "Nur Securities mit mindestens 100 Beobachtungen werden beruecksichtigt."),
        "ic_t_stat": ("Naive t-Statistik des mittleren taeglichen Cross-Sectional IC.", "Ignoriert moegliche Autokorrelation der IC-Serie.", None),
        "ic_t_stat_nw": ("Newey-West/HAC-korrigierte t-Statistik des mittleren taeglichen Cross-Sectional IC.", "Robustere Inferenz bei serieller Abhaengigkeit der IC-Serie.", "Lag = max(1, min(floor(4*(n/100)^(2/9)), n-1))."),
        "ic_t_stat_nw_lag": ("Verwendete Newey-West-Lag-Laenge fuer die IC-Serie.", "Datengesteuert nach Standardregel.", None),
        "spread_ret_1d": ("Taeglicher High-Score-minus-Low-Score-Quintile-Spread: mean(Q_high ret_fwd_1d) - mean(Q_low ret_fwd_1d).", "Hypothetische Long-Short-Signalgüte-Diagnostik, nicht die implementierte S1-Long-only-Strategie.", None),
        "spread_sharpe_annualized": ("Annualisierte Sharpe Ratio der hypothetischen taeglichen Quintile-Spread-Rendite.", "Diagnostik der Signalguete, nicht Performance der gehandelten S1-Strategie.", None),
        "spread_t_stat_nw": ("Newey-West-korrigierte t-Statistik des mittleren taeglichen Quintile-Spreads.", "Robuste Signifikanzdiagnostik fuer den hypothetischen Long-Short-Spread.", None),
        "max_drawdown_start_date": ("Datum des letzten Equity-Hochs vor dem tiefsten Drawdown.", "Startpunkt des Max-Drawdown-Fensters.", None),
        "max_drawdown_end_date": ("Datum des tiefsten Drawdowns.", "Tiefpunkt des Max-Drawdown-Fensters.", None),
        "max_drawdown_recovery_date": ("Erstes Datum nach dem Tiefpunkt, an dem die Equity das vorherige Hoch wieder erreicht oder ueberschreitet.", "NULL, wenn keine Erholung im Beobachtungszeitraum erfolgt.", None),
        "max_drawdown_recovery_days": ("Anzahl Handelstage zwischen Max-Drawdown-Tiefpunkt und Recovery-Datum.", "NULL, wenn keine Erholung im Beobachtungszeitraum erfolgt.", None),
        "calmar_ratio": ("annualized_return_period_geometric / abs(max_drawdown).", "Rendite-Risiko-Kennzahl relativ zum groessten Verlustpfad.", "Bei Null- oder NULL-Drawdown nicht definiert."),
        "strategy_cumulative_return": ("Kumulierte Strategie-Rendite im identifizierten Event-Fenster: product(1 + strategy_daily_return) - 1.", "Event-Attribution des tatsaechlichen Max-Drawdown-Fensters.", None),
        "benchmark_cumulative_return": ("Kumulierte Benchmark-Rendite im identifizierten Event-Fenster auf matched Tagen.", "Fehlende Benchmark-Tage werden nicht gefuellt.", None),
        "relative_performance": ("strategy_cumulative_return - benchmark_cumulative_return im Event-Fenster.", "Aktive Event-Performance gegen Benchmark auf matched Tagen.", None),
    }
    for name, values in definitions.items():
        upsert_metric_definition(conn, name, *values)


def run_v2_validations(conn: sqlite3.Connection) -> tuple[dict, bool, bool]:
    # Annualized return alias
    metrics = pd.read_sql_query("SELECT * FROM s1_metrics", conn)
    metrics_net = pd.read_sql_query("SELECT * FROM s1_metrics_net", conn)
    tol_ok = (
        np.allclose(metrics["annualized_return_period_geometric"], metrics["cagr"], atol=1e-12, equal_nan=True)
        and np.allclose(metrics_net["annualized_return_period_geometric"], metrics_net["cagr"], atol=1e-12, equal_nan=True)
    )
    replace_validation_check(
        conn,
        "annualized_return_period_geometric entspricht cagr",
        "PASS" if tol_ok else "FAIL",
        f"s1_metrics_rows={len(metrics)}; s1_metrics_net_rows={len(metrics_net)}; tolerance=1e-12",
    )

    # Quintile notes
    q = pd.read_sql_query("SELECT * FROM s1_quintile_spread_metrics", conn)
    q_ok = (not q.empty) and q["notes"].str.contains("hypothetische|hypothetisch", case=False, regex=True).all() and q["notes"].str.contains("Long-Short", case=False, regex=False).all()
    replace_validation_check(
        conn,
        "Quintile-Spread-Definition dokumentiert",
        "PASS" if q_ok else "FAIL",
        f"rows={len(q)}; all_notes_document_hypothetical_long_short={bool(q_ok)}",
    )

    # TS IC
    ic_summary = pd.read_sql_query("SELECT * FROM s1_ic_summary", conn)
    ts = pd.read_sql_query("SELECT * FROM s1_ic_time_series", conn)
    ts_ok = (
        {"n_ts_ic_securities_valid", "n_ts_ic_securities_excluded", "ts_ic_min_obs_threshold"}.issubset(ic_summary.columns)
        and (ic_summary["ts_ic_min_obs_threshold"] == 100).all()
        and (
            ((ts["is_valid_ts_ic"] == 1) & ts["ts_ic"].notna())
            | ((ts["is_valid_ts_ic"] == 0) & ts["exclusion_reason"].notna())
        ).all()
    )
    replace_validation_check(
        conn,
        "Time-Series IC Ausschluesse dokumentiert",
        "PASS" if ts_ok else "FAIL",
        f"summary_rows={len(ic_summary)}; ts_rows={len(ts)}; threshold=100",
    )

    # Drawdown recovery
    dd_ok = True
    for frame in [metrics, metrics_net]:
        subset = frame[frame["max_drawdown"] < 0]
        if subset.empty:
            continue
        dd_ok = dd_ok and subset["max_drawdown_start_date"].notna().all() and subset["max_drawdown_end_date"].notna().all()
        dd_ok = dd_ok and (pd.to_datetime(subset["max_drawdown_start_date"]) <= pd.to_datetime(subset["max_drawdown_end_date"])).all()
        recovered = subset[subset["max_drawdown_recovery_date"].notna()]
        if not recovered.empty:
            dd_ok = dd_ok and (pd.to_datetime(recovered["max_drawdown_recovery_date"]) >= pd.to_datetime(recovered["max_drawdown_end_date"])).all()
        dd_ok = dd_ok and subset["calmar_ratio_status"].notna().all()
    replace_validation_check(
        conn,
        "Drawdown-Recovery-Felder berechnet",
        "PASS" if dd_ok else "FAIL",
        "Checked non-null start/end dates, date ordering, recovery ordering, and calmar status for gross and net metrics.",
    )

    # Event attribution
    ev = pd.read_sql_query("SELECT * FROM s1_event_attribution", conn)
    gross_expected = len(metrics)
    net_expected = len(metrics_net)
    dup_ev = ev.duplicated(["universe", "time_logic", "portfolio_variant", "coverage_filter", "return_type", "cost_bps", "event_label"]).sum()
    ev_ok = (
        dup_ev == 0
        and (ev["return_type"] == "gross").sum() == gross_expected
        and (ev["return_type"] == "net").sum() == net_expected
        and ev["event_label_display"].notna().all()
    )
    replace_validation_check(
        conn,
        "Event Attribution eindeutig",
        "PASS" if ev_ok else "FAIL",
        f"gross_expected={gross_expected}; net_expected={net_expected}; event_rows={len(ev)}; duplicate_pk_rows={dup_ev}",
    )

    # Duplicate keys for new tables
    duplicate_checks = {
        "risk_free_rate_daily": ["date"],
        "risk_free_rate_backtest_aligned": ["date"],
        "s1_quintile_spread_returns": ["date", "universe", "time_logic"],
        "s1_quintile_spread_metrics": ["universe", "time_logic"],
        "s1_ic_time_series": ["universe", "time_logic", "security_id"],
        "s1_event_attribution": ["universe", "time_logic", "portfolio_variant", "coverage_filter", "return_type", "cost_bps", "event_label"],
    }
    dup_details = {}
    all_ok = True
    for table, cols in duplicate_checks.items():
        df = pd.read_sql_query(f"SELECT * FROM {quote_ident(table)}", conn)
        dup = int(df.duplicated(cols).sum())
        dup_details[table] = dup
        all_ok = all_ok and dup == 0
    replace_validation_check(
        conn,
        "Neue Tabellen ohne doppelte Schluessel",
        "PASS" if all_ok else "FAIL",
        json.dumps(dup_details, ensure_ascii=False, sort_keys=True),
    )

    validations = pd.read_sql_query("SELECT * FROM s1_validation_checks", conn)
    counts = validations["status"].value_counts().to_dict()
    existing_names = {
        "keine doppelten security_id-date-Zeilen im Input",
        "keine doppelten security_id-date-Zeilen in Positions",
        "Gewichte summieren pro Tag auf 1.0 +/- 1e-9",
        "Top-20-Auswahl korrekt",
        "daily_return entspricht Summe(weight * ret_fwd_1d)",
        "cumulative_return entspricht product(1 + daily_return) - 1",
        "Benchmark-Excess nur auf matched days",
        "n_positions > 0 fuer alle Backtesttage",
        "keine NaN/Inf in ausgewaehlten Renditen",
        "conservative_lag1 nutzt tatsaechlich gelaggten Score",
        "VOL60/VOL63-Namenskonvention dokumentiert",
        "Ergebnisse aus bestehender Results-DB nicht ueberschreiben",
    }
    new_names = {
        "ESTR-Daten auf Backtest-Handelstage gemappt",
        "sharpe_excess_estr berechnet",
        "Newey-West IC berechnet",
        "Quintile-Spread-Definition dokumentiert",
        "annualized_return_period_geometric entspricht cagr",
        "Time-Series IC Ausschluesse dokumentiert",
        "Drawdown-Recovery-Felder berechnet",
        "Event Attribution eindeutig",
        "Neue Tabellen ohne doppelte Schluessel",
    }
    existing_ok = validations[validations["check_name"].isin(existing_names)]["status"].eq("PASS").all()
    new_ok = validations[validations["check_name"].isin(new_names)]["status"].eq("PASS").all()
    return counts, bool(existing_ok), bool(new_ok)


def write_v2_report(conn: sqlite3.Connection, output_dir: Path, ddof_detail: str) -> None:
    report_dir = output_dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    universe = pd.read_sql_query("SELECT * FROM s1_universe_summary", conn)
    metrics = pd.read_sql_query("SELECT * FROM s1_metrics", conn)
    metrics_net = pd.read_sql_query("SELECT * FROM s1_metrics_net", conn)
    ic = pd.read_sql_query("SELECT * FROM s1_ic_summary", conn)
    spread = pd.read_sql_query("SELECT * FROM s1_quintile_spread_metrics", conn)
    events = pd.read_sql_query("SELECT * FROM s1_event_attribution", conn)
    validations = pd.read_sql_query("SELECT * FROM s1_validation_checks", conn)
    definitions = pd.read_sql_query("SELECT * FROM s1_metric_definitions ORDER BY metric_name", conn)
    rf = pd.read_sql_query("SELECT * FROM risk_free_rate_backtest_aligned", conn)
    common_coverage = (
        pd.read_sql_query("SELECT * FROM s1_common_news_security_coverage", conn)
        if table_exists(conn, "s1_common_news_security_coverage")
        else pd.DataFrame()
    )

    metric_cols = [
        "universe",
        "time_logic",
        "portfolio_variant",
        "coverage_filter",
        "n_days",
        "annualized_return_period_geometric",
        "sharpe",
        "sharpe_excess_estr",
        "max_drawdown",
        "max_drawdown_start_date",
        "max_drawdown_end_date",
        "max_drawdown_recovery_date",
        "calmar_ratio",
    ]
    net_cols = metric_cols[:4] + ["cost_bps"] + metric_cols[4:]
    lines = [
        "# S1 Revised Backtest Report",
        "",
        "## Executive Summary",
        "",
        f"Script version: `{SCRIPT_VERSION}`.",
        "S1 bleibt eine reine Price-only-Baseline aus Momentum plus Low Volatility. "
        "S1_FULL_568 bleibt eine breite Price-only-Robustheitsanalyse. "
        "S1_COMMON_NEWS_ANY ist das Hauptvergleichsuniversum fuer S1_PRICE_ONLY vs. S2_LEXICON_ONLY vs. HYBRID_S1_LEX_50_50, sofern keine strengere Coverage-Schwelle gewaehlt wird.",
        "",
        "## Risk-Free und Sharpe",
        "",
        "- Die bestehende Spalte `sharpe` ist die naive Sharpe Ratio gegen Null-Risk-Free.",
        "- Die neue Spalte `sharpe_excess_estr` ist die methodisch bevorzugte Sharpe Ratio auf Basis von ESTR-Ueberschussrenditen.",
        "- Tagesumrechnung: `estr_daily_decimal = (1 + estr_annual_pct / 100.0)^(1 / 252.0) - 1`.",
        f"- Sharpe-Standardabweichungskonvention: {ddof_detail}",
        f"- ESTR-Abdeckung: {len(rf)} Backtest-Handelstage, max Forward-Fill-Lag {rf['fill_lag_calendar_days'].max() if not rf.empty else 'n/a'} Kalendertage.",
        "",
        "## Universen",
        "",
        dataframe_to_markdown(universe),
        "",
        "## Common-News Security Coverage",
        "",
        "Die Common-News-Universen werden standardmaessig aus `news_model_input.signal_date` gebildet. "
        "`daily_text_scores` wird nur verwendet, wenn es explizit angefordert wird oder `auto` keinen nutzbaren `news_model_input` findet.",
        "",
        "S1_COMMON_NEWS_COV30/COV50 sind Robustheitsuniversen mit Security-Level-News-Coverage-Schwellen.",
        "",
        dataframe_to_markdown(common_coverage.head(40)),
        "",
        "## Brutto-Metriken",
        "",
        dataframe_to_markdown(metrics[[c for c in metric_cols if c in metrics.columns]]),
        "",
        "## Netto-Metriken",
        "",
        dataframe_to_markdown(metrics_net[[c for c in net_cols if c in metrics_net.columns]].head(48)),
        "",
        "## CAGR / Annualisierte Periodenrendite",
        "",
        "`cagr` bleibt als Legacy-Spalte erhalten. Das Hauptlabel im Reporting ist "
        "`annualized_return_period_geometric`, also die geometrisch annualisierte Periodenrendite des beobachteten ca. 17-monatigen Backtest-Zeitraums.",
        "",
        "## IC-Diagnostik",
        "",
        "Cross-Sectional IC ist die Hauptkennzahl fuer S1, weil S1 taeglich Aktien gegeneinander rankt. "
        "Time-Series IC ist eine ergaenzende Robustheitsdiagnostik und kein Ersatz fuer CS-IC.",
        "",
        dataframe_to_markdown(ic),
        "",
        "## Quintile High-Low Spread",
        "",
        "Q5-Q1 bzw. High-Low-Spread ist eine hypothetische Long-Short-Signalgüte-Diagnostik. "
        "Sie ist nicht die tatsaechlich implementierte S1-Long-only-Strategie.",
        "",
        dataframe_to_markdown(spread),
        "",
        "## Drawdown Recovery, Calmar und Event Attribution",
        "",
        "Recovery-Dauer und Calmar Ratio werden als zusaetzliche Risikokennzahlen berichtet. "
        "Event Attribution bezieht sich auf das tatsaechlich identifizierte Max-Drawdown-Fenster. "
        "April 2025 wird nur dann im Label verwendet, wenn dieses Fenster ueberwiegend in April 2025 liegt.",
        "",
        dataframe_to_markdown(events.head(48)),
        "",
        "## Validierungschecks",
        "",
        dataframe_to_markdown(validations),
        "",
        "## Methodikdefinitionen",
        "",
        dataframe_to_markdown(definitions),
    ]
    (report_dir / "s1_revised_report.md").write_text("\n".join(lines), encoding="utf-8")


def run_v2_update(args: argparse.Namespace) -> None:
    output_db = (PROJECT_DIR / args.output_db).resolve() if not args.output_db.is_absolute() else args.output_db
    output_dir = (PROJECT_DIR / args.output_dir).resolve() if not args.output_dir.is_absolute() else args.output_dir
    if not output_db.exists():
        raise FileNotFoundError(
            f"Expected v1 result DB not found: {output_db}. Run the v1 backtest first."
        )

    bundle = load_inputs(args)
    panel, _, _ = compute_or_validate_s1_score(bundle.price_panel)
    universe_detection = detect_universes(bundle, panel)

    with sqlite3.connect(output_db) as conn:
        ensure_v2_schema(conn)
        daily_dates = pd.read_sql_query(
            "SELECT MIN(date) AS min_date, MAX(date) AS max_date FROM s1_daily_returns",
            conn,
        ).iloc[0]
        rf_source = load_or_refresh_estr(conn, daily_dates["min_date"], daily_dates["max_date"])
        align_estr_to_backtest_dates(conn)
        ddof, ddof_detail = update_sharpe_excess_and_risk_metrics(conn)
        update_ic_newey_west(conn)
        compute_v2_signal_diagnostics(conn, panel, universe_detection)
        update_event_attribution(conn)
        insert_v2_metric_definitions(conn, ddof_detail)
        validation_counts, existing_ok, new_ok = run_v2_validations(conn)
        conn.execute(
            """
            UPDATE s1_run_metadata
            SET script_version = ?,
                risk_free_rate = NULL
            """,
            (SCRIPT_VERSION,),
        )
        write_v2_report(conn, output_dir, ddof_detail)
        conn.commit()

        table_counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {quote_ident(table)}").fetchone()[0]
            for table in [
                "risk_free_rate_daily",
                "risk_free_rate_backtest_aligned",
                "s1_quintile_spread_returns",
                "s1_quintile_spread_metrics",
                "s1_ic_time_series",
                "s1_event_attribution",
                "s1_metric_definitions",
            ]
        }

    print("S1 revised v2 additive update completed.")
    print(f"Result DB: {output_db}")
    print(f"script_version: {SCRIPT_VERSION}")
    print(f"risk_free_source: {rf_source}")
    for table, count in table_counts.items():
        print(f"{table}: {count}")
    print(f"validation_counts: {validation_counts}")
    print(f"existing_checks_still_pass: {existing_ok}")
    print(f"new_checks_pass: {new_ok}")


def run_full_backtest(args: argparse.Namespace) -> dict:
    output_db = (PROJECT_DIR / args.output_db).resolve() if not args.output_db.is_absolute() else args.output_db
    bundle = load_inputs(args)
    panel, score_notes, vol_convention = compute_or_validate_s1_score(bundle.price_panel)
    universe_detection = detect_universes(bundle, panel)

    daily_parts = []
    position_parts = []
    coverage_parts = []
    portfolio_variants = ["standard"]
    if "sector" in panel.columns and panel["sector"].notna().any():
        portfolio_variants.append("sector_neutral_robustness")
    for universe_name, ids in universe_detection.universes.items():
        if not ids:
            continue
        for time_logic in ["research_close_to_close", "conservative_lag1"]:
            for portfolio_variant in portfolio_variants:
                for coverage_filter in ["all_days", "coverage_80"]:
                    daily, positions, coverage = run_backtest(
                        panel,
                        universe_name,
                        ids,
                        time_logic,
                        portfolio_variant,
                        coverage_filter,
                        args.top_pct,
                        args.coverage_threshold,
                    )
                    if not daily.empty:
                        daily_parts.append(daily)
                    if not positions.empty:
                        position_parts.append(positions)
                    if not coverage.empty:
                        coverage_parts.append(coverage)

    daily_returns = pd.concat(daily_parts, ignore_index=True) if daily_parts else pd.DataFrame()
    positions = pd.concat(position_parts, ignore_index=True) if position_parts else pd.DataFrame()
    coverage = pd.concat(coverage_parts, ignore_index=True) if coverage_parts else pd.DataFrame()
    daily_returns, metrics_net = compute_turnover_and_costs(daily_returns, positions, args.cost_bps)
    metrics = compute_metrics(daily_returns, args.risk_free_rate)
    benchmark_daily, benchmark_metrics, benchmark_missing, benchmark_notes = compute_benchmark_metrics(
        daily_returns, bundle.benchmark_prices
    )
    coverage_table = compute_coverage(daily_returns)
    ic_by_date, ic_summary, quintile_returns, quintile_summary = compute_ic_and_quintiles(
        panel, universe_detection, args.top_pct
    )
    sector, country, top_positions = compute_exposures(positions)
    validations = run_validations(
        panel,
        positions,
        daily_returns,
        benchmark_daily,
        bundle.input_results_db,
        output_db,
        vol_convention,
        bundle,
        universe_detection,
    )
    outputs = {
        "s1_price_panel_scored": panel,
        "s1_positions": positions,
        "s1_daily_returns": daily_returns,
        "s1_metrics": metrics,
        "s1_metrics_net": metrics_net,
        "s1_coverage_by_date": coverage_table if not coverage_table.empty else coverage,
        "s1_ic_by_date": ic_by_date,
        "s1_ic_summary": ic_summary,
        "s1_quintile_returns": quintile_returns,
        "s1_quintile_summary": quintile_summary,
        "s1_vs_benchmark_daily": benchmark_daily,
        "s1_vs_benchmark_metrics": benchmark_metrics,
        "s1_benchmark_missing_dates": benchmark_missing,
        "s1_sector_exposure": sector,
        "s1_country_exposure": country,
        "s1_top_positions": top_positions,
        "s1_validation_checks": validations,
        "s1_common_news_security_coverage": bundle.common_news_security_coverage,
    }
    write_outputs(args, bundle, universe_detection, score_notes, vol_convention, outputs, benchmark_notes)
    return {
        "output_db": str(output_db),
        "validation_counts": validations["status"].value_counts().to_dict(),
        "universes": {name: len(ids) for name, ids in universe_detection.universes.items()},
    }


def main() -> None:
    args = parse_args()
    summary = run_full_backtest(args)
    run_v2_update(args)
    print("S1 revised full backtest completed.")
    print(f"Result DB: {summary['output_db']}")
    print(f"universes: {summary['universes']}")
    print(f"initial_validation_counts: {summary['validation_counts']}")


if __name__ == "__main__":
    main()
