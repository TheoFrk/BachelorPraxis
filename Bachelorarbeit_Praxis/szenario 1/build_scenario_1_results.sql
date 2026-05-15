PRAGMA foreign_keys = OFF;

ATTACH DATABASE 'DATA/thesis_data v2.sqlite' AS source;

DROP TABLE IF EXISTS s1_price_run_metadata;
DROP TABLE IF EXISTS s1_price_features;
DROP TABLE IF EXISTS s1_price_backtest_panel;
DROP TABLE IF EXISTS s1_price_top20_long;
DROP TABLE IF EXISTS s1_price_daily_returns;
DROP TABLE IF EXISTS s1_price_vs_benchmark_daily_returns;
DROP TABLE IF EXISTS s1_price_summary;

CREATE TABLE s1_price_run_metadata AS
SELECT
    datetime('now') AS created_at_utc,
    'szenario 1/build_scenario_1_results.sql' AS script_name,
    'DATA/thesis_data v2.sqlite' AS source_database,
    'price-only scenario 1 export' AS notes;

CREATE TABLE s1_price_features AS
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
FROM source.price_features_s1 p
JOIN source.securities s
  ON s.security_id = p.security_id;

CREATE TABLE s1_price_backtest_panel AS
SELECT *
FROM source.backtest_panel_price_only;

CREATE TABLE s1_price_top20_long AS
SELECT *
FROM source.v_strategy_s1_top20_long;

CREATE TABLE s1_price_daily_returns AS
SELECT *
FROM source.v_strategy_s1_daily_returns;

CREATE TABLE s1_price_vs_benchmark_daily_returns AS
SELECT *
FROM source.v_s1_vs_benchmark_daily_returns;

CREATE TABLE s1_price_summary AS
WITH returns AS (
    SELECT
        date,
        strategy_ret_fwd_1d,
        benchmark_ret_1d,
        excess_ret_1d,
        n_positions
    FROM s1_price_vs_benchmark_daily_returns
),
strategy_stats AS (
    SELECT
        COUNT(*) AS n_days,
        MIN(date) AS first_date,
        MAX(date) AS last_date,
        AVG(strategy_ret_fwd_1d) AS avg_strategy_ret_1d,
        AVG(benchmark_ret_1d) AS avg_benchmark_ret_1d,
        AVG(excess_ret_1d) AS avg_excess_ret_1d,
        AVG(n_positions) AS avg_n_positions,
        MIN(strategy_ret_fwd_1d) AS min_strategy_ret_1d,
        MAX(strategy_ret_fwd_1d) AS max_strategy_ret_1d
    FROM returns
),
strategy_variance AS (
    SELECT
        AVG((strategy_ret_fwd_1d - (SELECT avg_strategy_ret_1d FROM strategy_stats)) *
            (strategy_ret_fwd_1d - (SELECT avg_strategy_ret_1d FROM strategy_stats))) AS strategy_ret_variance,
        AVG((excess_ret_1d - (SELECT avg_excess_ret_1d FROM strategy_stats)) *
            (excess_ret_1d - (SELECT avg_excess_ret_1d FROM strategy_stats))) AS excess_ret_variance
    FROM returns
)
SELECT
    s.n_days,
    s.first_date,
    s.last_date,
    s.avg_strategy_ret_1d,
    s.avg_benchmark_ret_1d,
    s.avg_excess_ret_1d,
    s.avg_n_positions,
    s.min_strategy_ret_1d,
    s.max_strategy_ret_1d,
    sqrt(v.strategy_ret_variance) AS strategy_ret_stddev_1d,
    sqrt(v.excess_ret_variance) AS excess_ret_stddev_1d,
    CASE
        WHEN v.strategy_ret_variance IS NULL OR v.strategy_ret_variance = 0 THEN NULL
        ELSE s.avg_strategy_ret_1d / sqrt(v.strategy_ret_variance) * sqrt(252.0)
    END AS strategy_sharpe_annualized,
    CASE
        WHEN v.excess_ret_variance IS NULL OR v.excess_ret_variance = 0 THEN NULL
        ELSE s.avg_excess_ret_1d / sqrt(v.excess_ret_variance) * sqrt(252.0)
    END AS excess_sharpe_annualized
FROM strategy_stats s
CROSS JOIN strategy_variance v;

CREATE INDEX idx_s1_price_features_security_date
    ON s1_price_features(security_id, date);
CREATE INDEX idx_s1_price_features_date_score
    ON s1_price_features(date, z_s1_price_score);
CREATE INDEX idx_s1_price_backtest_panel_date_score
    ON s1_price_backtest_panel(date, z_s1_price_score);
CREATE INDEX idx_s1_price_top20_long_date
    ON s1_price_top20_long(date);
CREATE INDEX idx_s1_price_daily_returns_date
    ON s1_price_daily_returns(date);
CREATE INDEX idx_s1_price_vs_benchmark_daily_returns_date
    ON s1_price_vs_benchmark_daily_returns(date);

DETACH DATABASE source;
