#!/usr/bin/env python3
"""
Build scenario 1 price-only result tables.

Input:
    DATA/thesis_data v2.sqlite

Output:
    RESULTS.sql

The source database already contains the prepared S1 price features
(`price_features_s1`). This script calculates the scenario result tables from
those features: top quintile selection, daily portfolio returns, benchmark
comparison, and summary statistics.
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
SOURCE_DB = PROJECT_DIR / "DATA" / "thesis_data v2.sqlite"
RESULTS_DB = PROJECT_DIR / "RESULTS.sql"
SCRIPT_NAME = "szenario 1/build_scenario_1_results.py"


S1_PRICE_FEATURE_COLUMNS = [
    "feature_id",
    "security_id",
    "yahoo_ticker",
    "company",
    "country",
    "sector",
    "cap_group",
    "date",
    "ret_1d",
    "ret_fwd_1d",
    "ret_fwd_5d",
    "mom_12_1",
    "mom_3m",
    "vol_60d",
    "lowvol_60d",
    "z_mom_12_1",
    "z_lowvol_60d",
    "s1_price_score",
    "z_s1_price_score",
]

BACKTEST_PANEL_COLUMNS = [
    "panel_id",
    "date",
    "security_id",
    "yahoo_ticker",
    "company",
    "country",
    "sector",
    "ret_fwd_1d",
    "ret_fwd_5d",
    "z_s1_price_score",
    "s1_price_score",
]

TOP20_COLUMNS = [
    "date",
    "security_id",
    "yahoo_ticker",
    "ret_fwd_1d",
    "ret_fwd_5d",
    "z_s1_price_score",
    "s1_price_score",
    "score_quintile",
]

DAILY_RETURN_COLUMNS = [
    "date",
    "portfolio_ret_fwd_1d",
    "n_positions",
]

VS_BENCHMARK_COLUMNS = [
    "date",
    "strategy_ret_fwd_1d",
    "benchmark_ret_1d",
    "excess_ret_1d",
    "n_positions",
    "benchmark_symbol",
]


def quote_ident(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def placeholders(n: int) -> str:
    return ", ".join("?" for _ in range(n))


def avg(values: list[float]) -> float | None:
    clean = [value for value in values if value is not None]
    if not clean:
        return None
    return sum(clean) / len(clean)


def population_stddev(values: list[float]) -> float | None:
    clean = [value for value in values if value is not None]
    if not clean:
        return None
    mean = sum(clean) / len(clean)
    variance = sum((value - mean) ** 2 for value in clean) / len(clean)
    return math.sqrt(variance)


def sqlite_ntile(row_number: int, row_count: int, buckets: int) -> int:
    """Match SQLite NTILE bucket sizing for a 1-based row number."""
    base_size, larger_bucket_count = divmod(row_count, buckets)
    larger_bucket_size = base_size + 1
    larger_bucket_limit = larger_bucket_size * larger_bucket_count

    if row_number <= larger_bucket_limit:
        return math.ceil(row_number / larger_bucket_size)

    if base_size == 0:
        return larger_bucket_count

    return larger_bucket_count + math.ceil(
        (row_number - larger_bucket_limit) / base_size
    )


def connect_source() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True)


def connect_results() -> sqlite3.Connection:
    return sqlite3.connect(RESULTS_DB)


def reset_result_tables(conn: sqlite3.Connection) -> None:
    for table_name in [
        "s1_price_run_metadata",
        "s1_price_features",
        "s1_price_backtest_panel",
        "s1_price_top20_long",
        "s1_price_daily_returns",
        "s1_price_vs_benchmark_daily_returns",
        "s1_price_summary",
    ]:
        conn.execute(f"DROP TABLE IF EXISTS {quote_ident(table_name)}")


def create_result_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE s1_price_run_metadata (
            created_at_utc TEXT NOT NULL,
            script_name TEXT NOT NULL,
            source_database TEXT NOT NULL,
            notes TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE s1_price_features (
            feature_id INTEGER,
            security_id INTEGER,
            yahoo_ticker TEXT,
            company TEXT,
            country TEXT,
            sector TEXT,
            cap_group TEXT,
            date TEXT,
            ret_1d REAL,
            ret_fwd_1d REAL,
            ret_fwd_5d REAL,
            mom_12_1 REAL,
            mom_3m REAL,
            vol_60d REAL,
            lowvol_60d REAL,
            z_mom_12_1 REAL,
            z_lowvol_60d REAL,
            s1_price_score REAL,
            z_s1_price_score REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE s1_price_backtest_panel (
            panel_id INTEGER,
            date TEXT,
            security_id INTEGER,
            yahoo_ticker TEXT,
            company TEXT,
            country TEXT,
            sector TEXT,
            ret_fwd_1d REAL,
            ret_fwd_5d REAL,
            z_s1_price_score REAL,
            s1_price_score REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE s1_price_top20_long (
            date TEXT,
            security_id INTEGER,
            yahoo_ticker TEXT,
            ret_fwd_1d REAL,
            ret_fwd_5d REAL,
            z_s1_price_score REAL,
            s1_price_score REAL,
            score_quintile INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE s1_price_daily_returns (
            date TEXT,
            portfolio_ret_fwd_1d REAL,
            n_positions INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE s1_price_vs_benchmark_daily_returns (
            date TEXT,
            strategy_ret_fwd_1d REAL,
            benchmark_ret_1d REAL,
            excess_ret_1d REAL,
            n_positions INTEGER,
            benchmark_symbol TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE s1_price_summary (
            n_days INTEGER,
            first_date TEXT,
            last_date TEXT,
            avg_strategy_ret_1d REAL,
            avg_benchmark_ret_1d REAL,
            avg_excess_ret_1d REAL,
            avg_n_positions REAL,
            min_strategy_ret_1d REAL,
            max_strategy_ret_1d REAL,
            strategy_ret_stddev_1d REAL,
            excess_ret_stddev_1d REAL,
            strategy_sharpe_annualized REAL,
            excess_sharpe_annualized REAL
        )
        """
    )


def insert_metadata(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO s1_price_run_metadata
        VALUES (?, ?, ?, ?)
        """,
        (
            datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            SCRIPT_NAME,
            str(SOURCE_DB.relative_to(PROJECT_DIR)),
            "price-only scenario 1 calculated with Python sqlite3",
        ),
    )


def copy_price_features(source: sqlite3.Connection, results: sqlite3.Connection) -> int:
    rows = source.execute(
        """
        SELECT
            p.feature_id,
            p.security_id,
            p.yahoo_ticker,
            s.company,
            s.country,
            s.sector,
            s.cap_group,
            p.date,
            p.ret_1d,
            p.ret_fwd_1d,
            p.ret_fwd_5d,
            p.mom_12_1,
            p.mom_3m,
            p.vol_60d,
            p.lowvol_60d,
            p.z_mom_12_1,
            p.z_lowvol_60d,
            p.s1_price_score,
            p.z_s1_price_score
        FROM price_features_s1 p
        JOIN securities s
          ON s.security_id = p.security_id
        """
    ).fetchall()
    results.executemany(
        f"""
        INSERT INTO s1_price_features ({", ".join(S1_PRICE_FEATURE_COLUMNS)})
        VALUES ({placeholders(len(S1_PRICE_FEATURE_COLUMNS))})
        """,
        rows,
    )
    return len(rows)


def copy_backtest_panel(source: sqlite3.Connection, results: sqlite3.Connection) -> int:
    rows = source.execute(
        """
        SELECT
            panel_id,
            date,
            security_id,
            yahoo_ticker,
            company,
            country,
            sector,
            ret_fwd_1d,
            ret_fwd_5d,
            z_s1_price_score,
            s1_price_score
        FROM backtest_panel_price_only
        """
    ).fetchall()
    results.executemany(
        f"""
        INSERT INTO s1_price_backtest_panel ({", ".join(BACKTEST_PANEL_COLUMNS)})
        VALUES ({placeholders(len(BACKTEST_PANEL_COLUMNS))})
        """,
        rows,
    )
    return len(rows)


def calculate_top_quintile(
    source: sqlite3.Connection, results: sqlite3.Connection
) -> list[tuple]:
    rows_by_date: dict[str, list[tuple]] = defaultdict(list)
    candidate_rows = source.execute(
        """
        SELECT
            date,
            security_id,
            yahoo_ticker,
            ret_fwd_1d,
            ret_fwd_5d,
            z_s1_price_score,
            s1_price_score
        FROM backtest_panel_price_only
        WHERE z_s1_price_score IS NOT NULL
          AND ret_fwd_1d IS NOT NULL
        ORDER BY date, z_s1_price_score DESC
        """
    )

    for row in candidate_rows:
        rows_by_date[row[0]].append(row)

    top_rows = []
    for date in sorted(rows_by_date):
        date_rows = rows_by_date[date]
        row_count = len(date_rows)
        for index, row in enumerate(date_rows, start=1):
            quintile = sqlite_ntile(index, row_count, 5)
            if quintile == 1:
                top_rows.append((*row, quintile))

    results.executemany(
        f"""
        INSERT INTO s1_price_top20_long ({", ".join(TOP20_COLUMNS)})
        VALUES ({placeholders(len(TOP20_COLUMNS))})
        """,
        top_rows,
    )
    return top_rows


def calculate_daily_returns(
    results: sqlite3.Connection, top_rows: list[tuple]
) -> list[tuple]:
    returns_by_date: dict[str, list[float]] = defaultdict(list)
    for row in top_rows:
        date = row[0]
        ret_fwd_1d = row[3]
        returns_by_date[date].append(ret_fwd_1d)

    daily_returns = []
    for date in sorted(returns_by_date):
        date_returns = returns_by_date[date]
        daily_returns.append((date, avg(date_returns), len(date_returns)))

    results.executemany(
        f"""
        INSERT INTO s1_price_daily_returns ({", ".join(DAILY_RETURN_COLUMNS)})
        VALUES ({placeholders(len(DAILY_RETURN_COLUMNS))})
        """,
        daily_returns,
    )
    return daily_returns


def calculate_benchmark_returns(source: sqlite3.Connection) -> dict[str, tuple[str, float]]:
    benchmark_rows = source.execute(
        """
        SELECT p.symbol, p.date, p.adjusted_close
        FROM benchmark_prices_daily p
        JOIN benchmark_instruments b
          ON b.symbol = p.symbol
        WHERE b.is_preferred = 1
        ORDER BY p.symbol, p.date
        """
    ).fetchall()

    returns_by_date: dict[str, tuple[str, float]] = {}
    previous_close_by_symbol: dict[str, float] = {}
    for symbol, date, adjusted_close in benchmark_rows:
        previous_close = previous_close_by_symbol.get(symbol)
        if previous_close is not None:
            returns_by_date[date] = (symbol, adjusted_close / previous_close - 1.0)
        previous_close_by_symbol[symbol] = adjusted_close

    return returns_by_date


def calculate_vs_benchmark(
    source: sqlite3.Connection,
    results: sqlite3.Connection,
    daily_returns: list[tuple],
) -> list[tuple]:
    benchmark_returns = calculate_benchmark_returns(source)
    rows = []

    for date, strategy_return, n_positions in daily_returns:
        benchmark_symbol = None
        benchmark_return = None
        excess_return = None
        if date in benchmark_returns:
            benchmark_symbol, benchmark_return = benchmark_returns[date]
            excess_return = strategy_return - benchmark_return

        rows.append(
            (
                date,
                strategy_return,
                benchmark_return,
                excess_return,
                n_positions,
                benchmark_symbol,
            )
        )

    results.executemany(
        f"""
        INSERT INTO s1_price_vs_benchmark_daily_returns
        ({", ".join(VS_BENCHMARK_COLUMNS)})
        VALUES ({placeholders(len(VS_BENCHMARK_COLUMNS))})
        """,
        rows,
    )
    return rows


def calculate_summary(results: sqlite3.Connection, rows: list[tuple]) -> tuple:
    dates = [row[0] for row in rows]
    strategy_returns = [row[1] for row in rows]
    benchmark_returns = [row[2] for row in rows]
    excess_returns = [row[3] for row in rows]
    position_counts = [row[4] for row in rows]

    strategy_stddev = population_stddev(strategy_returns)
    excess_stddev = population_stddev(excess_returns)
    avg_strategy_return = avg(strategy_returns)
    avg_excess_return = avg(excess_returns)

    strategy_sharpe = None
    if strategy_stddev not in (None, 0) and avg_strategy_return is not None:
        strategy_sharpe = avg_strategy_return / strategy_stddev * math.sqrt(252.0)

    excess_sharpe = None
    if excess_stddev not in (None, 0) and avg_excess_return is not None:
        excess_sharpe = avg_excess_return / excess_stddev * math.sqrt(252.0)

    summary = (
        len(rows),
        min(dates) if dates else None,
        max(dates) if dates else None,
        avg_strategy_return,
        avg(benchmark_returns),
        avg_excess_return,
        avg(position_counts),
        min(strategy_returns) if strategy_returns else None,
        max(strategy_returns) if strategy_returns else None,
        strategy_stddev,
        excess_stddev,
        strategy_sharpe,
        excess_sharpe,
    )

    results.execute(
        """
        INSERT INTO s1_price_summary
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        summary,
    )
    return summary


def create_indexes(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE INDEX idx_s1_price_features_security_date
        ON s1_price_features(security_id, date)
        """
    )
    conn.execute(
        """
        CREATE INDEX idx_s1_price_features_date_score
        ON s1_price_features(date, z_s1_price_score)
        """
    )
    conn.execute(
        """
        CREATE INDEX idx_s1_price_backtest_panel_date_score
        ON s1_price_backtest_panel(date, z_s1_price_score)
        """
    )
    conn.execute(
        """
        CREATE INDEX idx_s1_price_top20_long_date
        ON s1_price_top20_long(date)
        """
    )
    conn.execute(
        """
        CREATE INDEX idx_s1_price_daily_returns_date
        ON s1_price_daily_returns(date)
        """
    )
    conn.execute(
        """
        CREATE INDEX idx_s1_price_vs_benchmark_daily_returns_date
        ON s1_price_vs_benchmark_daily_returns(date)
        """
    )


def main() -> None:
    if not SOURCE_DB.exists():
        raise FileNotFoundError(f"Source database not found: {SOURCE_DB}")

    with connect_source() as source, connect_results() as results:
        results.execute("PRAGMA foreign_keys = OFF")
        reset_result_tables(results)
        create_result_tables(results)
        insert_metadata(results)

        feature_count = copy_price_features(source, results)
        panel_count = copy_backtest_panel(source, results)
        top_rows = calculate_top_quintile(source, results)
        daily_returns = calculate_daily_returns(results, top_rows)
        benchmark_rows = calculate_vs_benchmark(source, results, daily_returns)
        summary = calculate_summary(results, benchmark_rows)
        create_indexes(results)
        results.commit()

    print(f"Wrote {RESULTS_DB}")
    print(f"s1_price_features: {feature_count}")
    print(f"s1_price_backtest_panel: {panel_count}")
    print(f"s1_price_top20_long: {len(top_rows)}")
    print(f"s1_price_daily_returns: {len(daily_returns)}")
    print(f"s1_price_vs_benchmark_daily_returns: {len(benchmark_rows)}")
    print(
        "summary: "
        f"n_days={summary[0]}, "
        f"first_date={summary[1]}, "
        f"last_date={summary[2]}, "
        f"avg_strategy_ret_1d={summary[3]:.6f}"
    )


if __name__ == "__main__":
    main()
