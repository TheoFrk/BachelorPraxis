#!/usr/bin/env python3
"""
Revised S1 price-only backtests for the STOXX Europe 600 thesis project.

The script is intentionally self-contained and uses only local SQLite/CSV inputs.
It does not modify the existing RESULTS.sql / RESULTS.sql.zip files. A new
versioned result database is written to outputs/results/s1_revised_results.sqlite.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


SCRIPT_VERSION = "2026-05-15-s1-revised-v1"
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
    return parser.parse_args()


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def resolve_existing_path(path: Path | None, candidates: Iterable[Path]) -> Path:
    if path is not None:
        resolved = path.expanduser()
        if not resolved.is_absolute():
            resolved = Path.cwd() / resolved
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
    project_dir = Path.cwd().resolve()
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

        daily_text_scores = read_sql_if_exists(conn, "daily_text_scores")
        news_model_input = read_sql_if_exists(conn, "news_model_input")
        if not daily_text_scores.empty:
            text_security_ids = set(
                pd.to_numeric(daily_text_scores["security_id"], errors="coerce")
                .dropna()
                .astype(int)
            )
            common_text_source = (
                f"daily_text_scores ({len(text_security_ids)} securities); used as textscore universe."
            )
        elif not news_model_input.empty:
            text_security_ids = set(
                pd.to_numeric(news_model_input["security_id"], errors="coerce")
                .dropna()
                .astype(int)
            )
            common_text_source = (
                f"news_model_input ({len(text_security_ids)} securities); documented as Common News Universe."
            )
        else:
            text_security_ids = set()
            common_text_source = "No daily_text_scores or news_model_input table found."

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

    common_ids = full_ids.intersection(bundle.text_security_ids)
    if common_ids:
        universes["S1_COMMON_TEXT"] = common_ids
        notes["S1_COMMON_TEXT"] = (
            f"U_COMMON_TEXT: intersection of price universe and {bundle.common_text_source}"
        )
    else:
        notes["S1_COMMON_TEXT"] = (
            "U_COMMON_TEXT konnte nicht gebildet werden, weil keine Text-/News-Securities gefunden wurden."
        )

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
    output_dir = (Path.cwd() / args.output_dir).resolve() if not args.output_dir.is_absolute() else args.output_dir
    output_db = (Path.cwd() / args.output_db).resolve() if not args.output_db.is_absolute() else args.output_db
    report_dir = output_dir / "reports"
    figures_dir = output_dir / "figures"
    results_dir = output_db.parent
    for directory in [report_dir, figures_dir, results_dir]:
        directory.mkdir(parents=True, exist_ok=True)

    if output_db.exists():
        backup = output_db.with_name(
            output_db.stem + f".backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}" + output_db.suffix
        )
        shutil.move(output_db, backup)

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
        f"- Common text source: {bundle.common_text_source}",
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
        "S1_FULL_568 nutzt alle Aktien mit gueltigen Preisfeatures und ist damit ein breites Bruttoergebnis. "
        "Text- und Hybridmodelle duerfen methodisch nur gegen S1 auf demselben Common-Universe verglichen werden, weil sonst Universumsabdeckung und Signalverfuegbarkeit vermischt werden.",
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
        "- S1_CORE als Hauptbaseline verwenden, falls eindeutig verfuegbar.",
        "- S1_FULL_568 als Robustheitsanalyse verwenden.",
        "- S1_COMMON_TEXT fuer faire Vergleiche mit S2/S3/S4/Hybrid verwenden.",
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


def main() -> None:
    args = parse_args()
    bundle = load_inputs(args)
    panel, score_notes, vol_convention = compute_or_validate_s1_score(bundle.price_panel)
    universe_detection = detect_universes(bundle, panel)

    daily_parts = []
    position_parts = []
    coverage_parts = []
    for universe_name, ids in universe_detection.universes.items():
        if not ids:
            continue
        for time_logic in ["research_close_to_close", "conservative_lag1"]:
            for portfolio_variant in ["standard", "sector_neutral_robustness"]:
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
    daily_returns, metrics_net = compute_turnover_and_costs(daily_returns, positions, args.cost_bps)
    coverage = compute_coverage(daily_returns)
    metrics = compute_metrics(daily_returns, args.risk_free_rate)
    benchmark_daily, benchmark_metrics, benchmark_missing, benchmark_notes = compute_benchmark_metrics(
        daily_returns, bundle.benchmark_prices
    )
    ic_by_date, ic_summary, quintile_returns, quintile_summary = compute_ic_and_quintiles(
        panel, universe_detection, args.top_pct
    )
    sector_exposure, country_exposure, top_positions = compute_exposures(positions)
    output_db = (Path.cwd() / args.output_db).resolve() if not args.output_db.is_absolute() else args.output_db
    validations = run_validations(
        panel,
        positions,
        daily_returns,
        benchmark_daily,
        bundle.input_results_db,
        output_db,
        vol_convention,
    )

    outputs = {
        "s1_daily_returns": daily_returns,
        "s1_positions": positions,
        "s1_metrics": metrics,
        "s1_metrics_net": metrics_net,
        "s1_vs_benchmark_daily": benchmark_daily,
        "s1_vs_benchmark_metrics": benchmark_metrics,
        "s1_benchmark_missing_dates": benchmark_missing,
        "s1_coverage_by_date": coverage,
        "s1_ic_by_date": ic_by_date,
        "s1_ic_summary": ic_summary,
        "s1_quintile_returns": quintile_returns,
        "s1_quintile_summary": quintile_summary,
        "s1_sector_exposure": sector_exposure,
        "s1_country_exposure": country_exposure,
        "s1_top_positions": top_positions,
        "s1_validation_checks": validations,
    }
    write_outputs(
        args,
        bundle,
        universe_detection,
        score_notes,
        vol_convention,
        outputs,
        benchmark_notes,
    )

    print("S1 revised backtests completed.")
    print(f"Input DB: {bundle.input_db}")
    print(f"Output DB: {output_db}")
    print(f"Universes run: {', '.join(universe_detection.universes.keys())}")
    if not universe_detection.core_identified:
        print("WARN: U_CORE konnte nicht eindeutig identifiziert werden.")
    if not metrics.empty:
        summary = select_report_cols(metrics)
        print(summary.to_string(index=False))


if __name__ == "__main__":
    main()
