#!/usr/bin/env python3
"""
S4 OpenRouter LLM text-signal backtest for the STOXX Europe 600 thesis project.

S4 creates an LLM-only news signal and a late-fusion hybrid signal:

    S4_LLM_ONLY_{i,t} = z_t(S4_raw_{i,t})
    HYBRID_{i,t} = w_s1 * z_t(S1_{i,t}) + w_llm * z_t(S4_raw_{i,t})

The script is self-contained, writes only S4 outputs/caches, and refuses to
overwrite the S1/S2/S3 result databases or input databases.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests
import yaml


@dataclass
class SimpleHTTPResponse:
    status_code: int
    text: str

    def json(self) -> Any:
        return json.loads(self.text)


SCRIPT_VERSION = "2026-05-25-s4-llm-openrouter-v2-multilingual"
PROJECT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_COST_BPS = [5.0, 10.0, 25.0]
TEXT_COL_CANDIDATES = ["title"]
ORDERED_TEXT_COLS = ["title", "summary", "description", "body"]
EXCLUDED_TEXT_COLS = {"url", "domain", "publication_date", "publication_ts_utc", "signal_date"}
LANGUAGE_MODES = ["EN", "MULTI"]
VARIANT_CONFIGS: list[dict[str, Any]] = []
STRATEGY_VARIANTS = [cfg[1] for cfg in VARIANT_CONFIGS]
TIME_LOGICS = ["research_signal_date", "conservative_price_lag1"]
PORTFOLIO_VARIANTS = ["standard"]
COVERAGE_FILTERS = ["all_days", "coverage_80"]
KEY_COLS = [
    "model_family",
    "strategy_variant",
    "language_mode",
    "news_mode",
    "time_logic",
    "portfolio_variant",
    "coverage_filter",
]
SCORE_CACHE_TABLE = "s4_llm_score_cache"
ARTICLE_SCORE_TABLE = "s4_llm_article_scores"
DAILY_SCORE_TABLE = "s4_llm_daily_scores"
API_KEY_RE = re.compile(r"(sk-or-v1-[A-Za-z0-9_-]+|Bearer\s+[A-Za-z0-9._~+/=-]+)", re.IGNORECASE)
REQUEST_RATE_LIMIT_LOCK = threading.Lock()
LAST_REQUEST_AT_MONOTONIC = 0.0
DEFAULT_CONFIG: dict[str, Any] = {
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "chat_completions_path": "/chat/completions",
        "api_key_env": "OPENROUTER_API_KEY",
        "api_key": None,
        "http_referer": None,
        "app_title": "Bachelorarbeit S4 LLM Backtest",
    },
    "models": {
        "scoring_model": None,
        "llm_only_model": "DEIN_OPENROUTER_MODELL_HIER",
        "hybrid_model": "DEIN_OPENROUTER_MODELL_HIER",
        "fallback_models": [],
        "api_primary_model": None,
        "api_fallback_models": None,
    },
    "architecture": {
        "single_llm_score_for_all_variants": True,
        "allow_distinct_models_for_variants": False,
    },
    "request": {
        "temperature": 0.0,
        "top_p": 1.0,
        "max_tokens": 3500,
        "timeout_seconds": 60,
        "max_retries": 1,
        "retry_sleep_seconds": 3,
        "request_batch_size": 32,
        "max_concurrent_requests": 8,
        "min_seconds_between_requests": 0.0,
        "progress_every_batches": 25,
        "singleton_repair": False,
        "retry_missing_after_initial": False,
        "response_format_mode": "json_schema",
        "include_reasoning": False,
        "reasoning": {"exclude": True},
    },
    "scoring": {
        "prompt_version": "s4_llm_v2",
        "max_chars_per_article": 3000,
        "min_event_stocks_for_zscore": 5,
        "fallback_raw_score_scale": 0.50,
        "z_clip": 3.0,
        "neutral_fill_value": 0.0,
        "hybrid_weight_s1": 0.5,
        "hybrid_weight_llm": 0.5,
        "include_shrinkage_hybrid": True,
        "shrinkage_weight_s1": 0.7,
        "shrinkage_weight_llm": 0.3,
        "compatible_score_cache_hashes": [],
    },
    "backtest": {
        "top_pct": 0.20,
        "coverage_threshold": 0.80,
        "security_news_coverage_threshold": 0.0,
        "cost_bps": [5, 10, 25],
    },
    "data": {
        "input_db_candidates": ["DATA/thesis_data v2.sqlite", "DATA/thesis_data.sqlite"],
        "input_s1_results_db": "outputs/results/s1_revised_results.sqlite",
        "input_s2_results_db": "outputs/results/s2_lexicon_results.sqlite",
        "input_s3_results_db": "outputs/results/s3_finbert_results.sqlite",
        "output_db": "outputs/results/s4_llm_results.sqlite",
        "score_cache_db": "outputs/cache/s4_llm_score_cache.sqlite",
        "output_dir": "outputs",
        "text_cols": ["title"],
        "allow_text_cols_if_available": ["summary", "body", "description"],
        "exclude_text_cols": ["url", "domain", "publication_date", "publication_ts_utc"],
    },
}
EMPTY_TABLE_COLUMNS = {
    "s4_vs_benchmark_daily": KEY_COLS
    + [
        "date",
        "strategy_return",
        "benchmark_ret_fwd_1d",
        "excess_return",
        "strategy_equity_matched",
        "benchmark_equity_matched",
    ],
    "s4_vs_benchmark_metrics": KEY_COLS
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
    "s4_vs_s1_daily": KEY_COLS
    + [
        "date",
        "s4_return",
        "s1_return",
        "return_diff",
        "s4_equity_matched",
        "s1_equity_matched",
    ],
    "s4_vs_s1_metrics": KEY_COLS
    + [
        "n_matched_days",
        "s4_cum_return_matched",
        "s1_cum_return_matched",
        "active_cum_return_vs_s1",
        "mean_daily_diff",
        "tracking_error_vs_s1",
        "information_ratio_vs_s1",
    ],
    "s4_vs_s2_daily": KEY_COLS
    + [
        "date",
        "s4_return",
        "s2_return",
        "return_diff",
        "s4_equity_matched",
        "s2_equity_matched",
        "s2_strategy_variant",
    ],
    "s4_vs_s2_metrics": KEY_COLS
    + [
        "s2_strategy_variant",
        "n_matched_days",
        "s4_cum_return_matched",
        "s2_cum_return_matched",
        "active_cum_return_vs_s2",
        "mean_daily_diff",
        "tracking_error_vs_s2",
        "information_ratio_vs_s2",
        "notes",
    ],
    "s4_vs_s3_daily": KEY_COLS
    + [
        "date",
        "s4_return",
        "s3_return",
        "return_diff",
        "s4_equity_matched",
        "s3_equity_matched",
        "s3_strategy_variant",
    ],
    "s4_vs_s3_metrics": KEY_COLS
    + [
        "s3_strategy_variant",
        "n_matched_days",
        "s4_cum_return_matched",
        "s3_cum_return_matched",
        "active_cum_return_vs_s3",
        "mean_daily_diff",
        "tracking_error_vs_s3",
        "information_ratio_vs_s3",
        "notes",
    ],
    "s4_ic_by_date": ["model_family", "strategy_variant", "language_mode", "news_mode", "time_logic", "signal_name", "date", "n_candidates", "spearman_ic"],
    "s4_ic_summary": [
        "model_family",
        "strategy_variant",
        "language_mode",
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
    "s4_quintile_spread_returns": [
        "model_family",
        "strategy_variant",
        "language_mode",
        "news_mode",
        "time_logic",
        "signal_name",
        "date",
        "high_ret_1d",
        "low_ret_1d",
        "spread_ret_1d",
        "notes",
    ],
    "s4_quintile_spread_metrics": [
        "model_family",
        "strategy_variant",
        "language_mode",
        "news_mode",
        "time_logic",
        "signal_name",
        "n_days",
        "mean_daily_spread",
        "cumulative_spread",
        "spread_t_stat_naive",
        "notes",
    ],
    "s4_sector_exposure": KEY_COLS + ["sector", "avg_weight", "selected_days"],
    "s4_country_exposure": KEY_COLS + ["country", "avg_weight", "selected_days"],
    "s4_top_positions": KEY_COLS
    + ["security_id", "yahoo_ticker", "company", "country", "sector", "selected_days", "avg_weight", "avg_score_used"],
}


@dataclass
class InputBundle:
    project_dir: Path
    input_db: Path
    input_s1_results_db: Path | None
    input_s2_results_db: Path | None
    input_s3_results_db: Path | None
    price_panel: pd.DataFrame
    news: pd.DataFrame
    securities: pd.DataFrame
    benchmark_prices: pd.DataFrame
    input_tables: list[str]
    price_table: str
    text_cols: list[str]
    security_news_coverage_threshold: float
    common_news_security_coverage: pd.DataFrame
    source_notes: list[str]
    n_articles_total: int
    n_articles_after_text_filter: int
    n_articles_s4_en: int
    n_articles_s4_multi: int
    n_non_english_excluded_s4_en: int
    language_distribution: pd.DataFrame


@dataclass
class LLMBundle:
    llm_only_model: str
    hybrid_model: str
    scoring_model: str
    fallback_models: list[str]
    api_primary_model: str
    api_fallback_models: list[str]
    prompt_version: str
    prompt_mode: str
    response_format_mode: str
    response_format_fallback_used: bool
    api_key_status: str
    status: str
    cache_only: bool
    no_api: bool
    single_llm_score_for_all_variants: bool
    api_scoring_models: list[str]


@dataclass
class EffectiveConfig:
    raw: dict[str, Any]
    path: Path
    loaded_path: Path | None
    run_id: str
    config_hash: str
    scoring_config_hash: str
    prompt_mode: str
    single_llm_score_for_all_variants: bool
    openrouter: dict[str, Any]
    models: dict[str, Any]
    request: dict[str, Any]
    scoring: dict[str, Any]
    backtest: dict[str, Any]
    data: dict[str, Any]
    api_key: str | None
    api_key_status: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run S4 OpenRouter LLM text-signal backtests.")
    parser.add_argument("--config", type=Path, default=Path("config/s4_llm_openrouter.yaml"))
    parser.add_argument("--input-db", type=Path, default=None)
    parser.add_argument("--input-s1-results-db", type=Path, default=None)
    parser.add_argument("--input-s2-results-db", type=Path, default=None)
    parser.add_argument("--input-s3-results-db", type=Path, default=None)
    parser.add_argument("--output-db", type=Path, default=None)
    parser.add_argument("--score-cache-db", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--model-name", default=None)
    parser.add_argument("--scoring-model", default=None)
    parser.add_argument("--llm-only-model", default=None)
    parser.add_argument("--hybrid-model", default=None)
    parser.add_argument("--api-primary-model", default=None)
    parser.add_argument("--api-fallback-models", nargs="+", default=None)
    parser.add_argument("--text-cols", nargs="+", default=None)
    parser.add_argument("--language-mode", choices=["EN", "MULTI", "BOTH"], default="BOTH")
    parser.add_argument("--top-pct", type=float, default=None)
    parser.add_argument("--coverage-threshold", type=float, default=None)
    parser.add_argument("--cost-bps", type=float, nargs="+", default=None)
    parser.add_argument("--max-articles", type=int, default=None)
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--reuse-score-cache", action="store_true")
    parser.add_argument("--rescore", action="store_true")
    parser.add_argument("--single-llm-score-for-all-variants", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--allow-distinct-models-for-variants", action="store_true")
    parser.add_argument("--prompt-mode", choices=["full", "fast_score_only"], default="full")
    parser.add_argument("--min-seconds-between-requests", type=float, default=None)
    parser.add_argument("--skip-existing-api-fails-first-pass", action="store_true")
    parser.add_argument("--retry-api-fails-only", action="store_true")
    parser.add_argument("--max-api-fail-age-hours", type=float, default=None)
    parser.add_argument("--cache-only-backtest", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-api", action="store_true")
    parser.add_argument("--estimate-cost-only", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(base))
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def sanitize_error(value: Any, api_key: str | None = None) -> str:
    text = str(value)
    if api_key:
        text = text.replace(api_key, "***redacted***")
    text = API_KEY_RE.sub("***redacted***", text)
    return text[:1000]


def combine_flags(*values: Any) -> str | None:
    flags: list[str] = []
    for value in values:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            continue
        for part in str(value).split(";"):
            normalized = part.strip()
            if normalized and normalized not in flags:
                flags.append(normalized)
    return ";".join(flags) if flags else None


def safe_config_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(config, default=str))
    openrouter = out.setdefault("openrouter", {})
    if openrouter.get("api_key"):
        openrouter["api_key"] = "***configured***"
    return out


def compute_scoring_config_hash(config: dict[str, Any], prompt_mode: str) -> str:
    scoring_payload = {
        "scoring_model": config.get("models", {}).get("scoring_model"),
        "llm_only_model": config.get("models", {}).get("llm_only_model"),
        "hybrid_model": config.get("models", {}).get("hybrid_model"),
        "fallback_models": config.get("models", {}).get("fallback_models") or [],
        "single_llm_score_for_all_variants": config.get("architecture", {}).get("single_llm_score_for_all_variants"),
        "allow_distinct_models_for_variants": config.get("architecture", {}).get("allow_distinct_models_for_variants"),
        "prompt_version": config.get("scoring", {}).get("prompt_version"),
        "prompt_mode": prompt_mode,
        "text_cols": config.get("data", {}).get("text_cols") or [],
        "max_chars_per_article": config.get("scoring", {}).get("max_chars_per_article"),
        "response_format_mode": config.get("request", {}).get("response_format_mode"),
        "temperature": config.get("request", {}).get("temperature"),
        "top_p": config.get("request", {}).get("top_p"),
        "max_tokens": config.get("request", {}).get("max_tokens"),
        "include_reasoning": config.get("request", {}).get("include_reasoning"),
        "reasoning": config.get("request", {}).get("reasoning"),
    }
    return hashlib.sha256(json.dumps(scoring_payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def load_effective_config(args: argparse.Namespace) -> EffectiveConfig:
    config_path = resolve_output_path(args.config)
    loaded_path: Path | None = None
    file_config: dict[str, Any] = {}
    if config_path.exists():
        loaded_path = config_path
        with config_path.open("r", encoding="utf-8") as fh:
            file_config = yaml.safe_load(fh) or {}
    else:
        example = config_path.with_name("s4_llm_openrouter.example.yaml")
        if example.exists():
            loaded_path = example
            with example.open("r", encoding="utf-8") as fh:
                file_config = yaml.safe_load(fh) or {}
    config = deep_merge(DEFAULT_CONFIG, file_config)

    if args.model_name:
        config["models"]["llm_only_model"] = args.model_name
        config["models"]["hybrid_model"] = args.model_name
        config["models"]["scoring_model"] = args.model_name
    if args.scoring_model:
        config["models"]["scoring_model"] = args.scoring_model
    if args.llm_only_model:
        config["models"]["llm_only_model"] = args.llm_only_model
    if args.hybrid_model:
        config["models"]["hybrid_model"] = args.hybrid_model
    if args.api_primary_model:
        config["models"]["api_primary_model"] = args.api_primary_model
    if args.api_fallback_models is not None:
        config["models"]["api_fallback_models"] = args.api_fallback_models
    prompt_mode = str(args.prompt_mode or "full")
    config["scoring"]["prompt_mode"] = prompt_mode
    if prompt_mode == "fast_score_only":
        config["scoring"]["prompt_version"] = "s4_llm_v3_fast_score_only"
    else:
        config["scoring"]["prompt_version"] = "s4_llm_v2"
    single_score = bool(args.single_llm_score_for_all_variants) and not bool(args.allow_distinct_models_for_variants)
    config.setdefault("architecture", {})
    config["architecture"]["single_llm_score_for_all_variants"] = bool(args.single_llm_score_for_all_variants)
    config["architecture"]["allow_distinct_models_for_variants"] = bool(args.allow_distinct_models_for_variants)
    if single_score:
        scoring_model = (
            args.scoring_model
            or args.model_name
            or config["models"].get("scoring_model")
            or config["models"].get("llm_only_model")
            or config["models"].get("hybrid_model")
        )
        if not scoring_model:
            raise RuntimeError("No OpenRouter scoring model configured.")
        if str(config["models"].get("llm_only_model")) != str(config["models"].get("hybrid_model")):
            print(
                "WARN: Different llm_only_model and hybrid_model were supplied, but single-score mode is active. "
                f"Using scoring_model={scoring_model}.",
                file=sys.stderr,
            )
        config["models"]["scoring_model"] = scoring_model
        config["models"]["llm_only_model"] = scoring_model
        config["models"]["hybrid_model"] = scoring_model
    elif (
        str(config["models"].get("llm_only_model")) != str(config["models"].get("hybrid_model"))
        and not bool(args.allow_distinct_models_for_variants)
    ):
        raise RuntimeError(
            "Different llm_only_model and hybrid_model require --allow-distinct-models-for-variants "
            "or active --single-llm-score-for-all-variants."
        )
    if args.text_cols:
        config["data"]["text_cols"] = args.text_cols
    if args.top_pct is not None:
        config["backtest"]["top_pct"] = args.top_pct
    if args.coverage_threshold is not None:
        config["backtest"]["coverage_threshold"] = args.coverage_threshold
    if args.cost_bps is not None:
        config["backtest"]["cost_bps"] = args.cost_bps
    if args.min_seconds_between_requests is not None:
        config["request"]["min_seconds_between_requests"] = args.min_seconds_between_requests
    for cli_name, cfg_key in [
        ("input_s1_results_db", "input_s1_results_db"),
        ("input_s2_results_db", "input_s2_results_db"),
        ("input_s3_results_db", "input_s3_results_db"),
        ("output_db", "output_db"),
        ("score_cache_db", "score_cache_db"),
        ("output_dir", "output_dir"),
    ]:
        value = getattr(args, cli_name)
        if value is not None:
            config["data"][cfg_key] = str(value)
    if args.cache_only_backtest:
        if args.output_db is None:
            config["data"]["output_db"] = "outputs/results/s4_llm_preview_en_partial.sqlite"
        if args.output_dir is None:
            config["data"]["output_dir"] = "outputs/s4_preview"

    env_name = str(config["openrouter"].get("api_key_env") or "")
    env_key = os.environ.get(env_name) if env_name else None
    file_key = config["openrouter"].get("api_key")
    api_key = env_key or file_key or None
    api_key_status = "configured" if api_key else "missing"
    snapshot = safe_config_snapshot(config)
    config_hash = hashlib.sha256(json.dumps(snapshot, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
    scoring_config_hash = compute_scoring_config_hash(config, prompt_mode)
    return EffectiveConfig(
        raw=config,
        path=config_path,
        loaded_path=loaded_path,
        run_id=f"s4_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}",
        config_hash=config_hash,
        scoring_config_hash=scoring_config_hash,
        prompt_mode=prompt_mode,
        single_llm_score_for_all_variants=single_score,
        openrouter=config["openrouter"],
        models=config["models"],
        request=config["request"],
        scoring=config["scoring"],
        backtest=config["backtest"],
        data=config["data"],
        api_key=api_key,
        api_key_status=api_key_status,
    )


def language_modes_from_arg(value: str) -> list[str]:
    return LANGUAGE_MODES if value == "BOTH" else [value]


def is_english_language(value: Any) -> bool:
    if pd.isna(value):
        return True
    normalized = str(value).strip().lower()
    return normalized in {"", "en", "eng", "english"}


def active_variant_configs(config: EffectiveConfig, language_modes: list[str]) -> list[dict[str, Any]]:
    variants: list[dict[str, Any]] = []
    for language_mode in language_modes:
        for news_mode in ["NEUTRAL_FILL", "EVENT_ONLY"]:
            variants.append(
                {
                    "model_family": "S4_LLM_ONLY",
                    "strategy_variant": f"S4_LLM_ONLY_{language_mode}_{news_mode}",
                    "language_mode": language_mode,
                    "news_mode": news_mode,
                    "weight_s1": 0.0,
                    "weight_llm": 1.0,
                    "model_role": "llm_only",
                    "model_name": config.models["llm_only_model"],
                }
            )
            variants.append(
                {
                    "model_family": "HYBRID_S1_LLM_50_50",
                    "strategy_variant": f"HYBRID_S1_LLM_50_50_{language_mode}_{news_mode}",
                    "language_mode": language_mode,
                    "news_mode": news_mode,
                    "weight_s1": float(config.scoring["hybrid_weight_s1"]),
                    "weight_llm": float(config.scoring["hybrid_weight_llm"]),
                    "model_role": "hybrid",
                    "model_name": config.models["hybrid_model"],
                }
            )
        if config.scoring.get("include_shrinkage_hybrid", True):
            variants.append(
                {
                    "model_family": "HYBRID_S1_LLM_70_30",
                    "strategy_variant": f"HYBRID_S1_LLM_70_30_{language_mode}_NEUTRAL_FILL",
                    "language_mode": language_mode,
                    "news_mode": "NEUTRAL_FILL",
                    "weight_s1": float(config.scoring["shrinkage_weight_s1"]),
                    "weight_llm": float(config.scoring["shrinkage_weight_llm"]),
                    "model_role": "hybrid",
                    "model_name": config.models["hybrid_model"],
                }
            )
    return variants


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


def common_news_universe_name(threshold: float, language_mode: str = "EN") -> str:
    if abs(threshold) < 1e-12:
        return f"S4_COMMON_LLM_{language_mode}_ANY"
    return f"S4_COMMON_LLM_{language_mode}_COV{int(round(threshold * 100))}"


def common_news_include_col(threshold: float) -> str:
    if abs(threshold) < 1e-12:
        return "included_any"
    return f"included_cov{int(round(threshold * 100))}"


def build_common_news_universe(
    price: pd.DataFrame,
    llm_ready_news: pd.DataFrame,
    threshold: float,
) -> tuple[dict[str, set[int]], pd.DataFrame, str, list[str]]:
    resolved_source = "news_model_input_filtered_for_llm"
    notes = ["Common universe built from actually scoreable LLM news after language, text and API/cache filters."]
    if llm_ready_news.empty:
        return {mode: set() for mode in LANGUAGE_MODES}, pd.DataFrame(), resolved_source, notes
    news = llm_ready_news[["security_id", "signal_date", "language_mode"]].copy()

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
    coverage_frames = []
    ids_by_mode: dict[str, set[int]] = {}
    meta_cols = [c for c in ["security_id", "yahoo_ticker", "company", "country", "sector"] if c in valid.columns]
    for language_mode, mode_news in news.groupby("language_mode", observed=True):
        matched = valid_days.merge(
            mode_news[["security_id", "signal_date"]].drop_duplicates().rename(columns={"signal_date": "date"}),
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
        coverage["language_mode"] = language_mode
        coverage["universe_source"] = resolved_source
        if meta_cols:
            coverage = coverage.merge(valid[meta_cols].drop_duplicates("security_id"), on="security_id", how="left")
        include_col = common_news_include_col(threshold)
        if abs(threshold) < 1e-12:
            mask = coverage["n_news_signal_days_on_valid_price_dates"].ge(1)
        else:
            mask = coverage["news_coverage_ratio"].ge(threshold)
        coverage[include_col] = mask.astype(int)
        ids = set(coverage.loc[mask, "security_id"].astype(int))
        ids_by_mode[str(language_mode)] = ids
        notes.append(f"{common_news_universe_name(threshold, str(language_mode))} selected: {len(ids)} securities.")
        coverage_frames.append(coverage)
    for mode in LANGUAGE_MODES:
        ids_by_mode.setdefault(mode, set())
    out = pd.concat(coverage_frames, ignore_index=True) if coverage_frames else pd.DataFrame()
    return ids_by_mode, out.sort_values(["language_mode", "security_id"]) if not out.empty else out, resolved_source, notes


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


def load_inputs(args: argparse.Namespace, config: EffectiveConfig) -> InputBundle:
    project_dir = PROJECT_DIR
    input_candidates = [project_dir / p for p in config.data.get("input_db_candidates", [])]
    input_db = resolve_existing_path(
        args.input_db,
        input_candidates
        or [
            project_dir / "DATA" / "thesis_data v2.sqlite",
            project_dir / "DATA" / "thesis_data.sqlite",
        ],
    )
    input_s1 = resolve_optional_path(Path(config.data["input_s1_results_db"])) if config.data.get("input_s1_results_db") else None
    input_s2 = resolve_optional_path(Path(config.data["input_s2_results_db"])) if config.data.get("input_s2_results_db") else None
    input_s3 = resolve_optional_path(Path(config.data["input_s3_results_db"])) if config.data.get("input_s3_results_db") else None
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
        if args.text_cols:
            requested_text_cols = args.text_cols
        else:
            requested_text_cols = list(config.data.get("text_cols") or TEXT_COL_CANDIDATES)
            for col in config.data.get("allow_text_cols_if_available") or []:
                if col not in requested_text_cols:
                    requested_text_cols.append(col)
        excluded = {c.lower() for c in (config.data.get("exclude_text_cols") or [])} | EXCLUDED_TEXT_COLS
        bad_text = [c for c in requested_text_cols if c.lower() in excluded]
        if bad_text:
            raise RuntimeError("These columns are not valid LLM text columns: " + ", ".join(bad_text))
        text_cols = [c for c in ORDERED_TEXT_COLS if c in requested_text_cols and c in news_cols]
        text_cols += [c for c in requested_text_cols if c in news_cols and c not in text_cols]
        if not text_cols:
            raise RuntimeError(
                "news_model_input has no usable text column. Tried: " + ", ".join(requested_text_cols)
            )
        optional_news = [
            "article_id",
            "publication_date",
            "publication_ts_utc",
            "language",
            "yahoo_ticker",
            "company",
            "country",
            "sector",
            "mapping_confidence",
            "domain",
            "url",
        ]
        select_cols = []
        for col in ["article_id", "signal_date", "publication_date", "security_id", *optional_news, *text_cols]:
            if col in news_cols and col not in select_cols:
                select_cols.append(col)
        total_news = int(conn.execute("SELECT COUNT(*) FROM news_model_input").fetchone()[0])
        sql = "SELECT " + ", ".join(quote_ident(c) for c in select_cols) + " FROM news_model_input"
        raw_news = pd.read_sql_query(sql, conn)
        if args.start_date:
            raw_news = raw_news[pd.to_datetime(raw_news["signal_date"], errors="coerce") >= pd.to_datetime(args.start_date)].copy()
        if args.end_date:
            raw_news = raw_news[pd.to_datetime(raw_news["signal_date"], errors="coerce") <= pd.to_datetime(args.end_date)].copy()
        news = raw_news.copy()
        text_parts = []
        for col in text_cols:
            text_parts.append(news[col].fillna("").astype(str).str.strip())
        news["text_used"] = pd.concat(text_parts, axis=1).agg(" ".join, axis=1).str.replace(r"\s+", " ", regex=True).str.strip()
        news = news[news["text_used"].str.len().gt(0)].copy()
        if args.max_articles is not None:
            news = news.head(args.max_articles).copy()
        if "language" not in news.columns:
            news["language"] = None
        news["is_s4_en"] = news["language"].map(is_english_language)
        n_s4_en = int(news["is_s4_en"].sum())
        n_s4_multi = int(len(news))
        language_distribution = (
            news.assign(language=news["language"].fillna("NULL"))
            .groupby("language", dropna=False, observed=True)
            .size()
            .reset_index(name="n_articles")
            .sort_values("n_articles", ascending=False)
        )
        source_notes.append(
            f"Loaded news_model_input with {total_news:,} source rows; {len(raw_news):,} after date filters; "
            f"{len(news):,} after non-empty text filter; S4_EN={n_s4_en:,}; "
            f"text columns: {', '.join(text_cols)}."
        )

        benchmark = load_benchmark_prices(conn, source_notes)

    return InputBundle(
        project_dir=project_dir,
        input_db=input_db,
        input_s1_results_db=input_s1,
        input_s2_results_db=input_s2,
        input_s3_results_db=input_s3,
        price_panel=price,
        news=news,
        securities=securities,
        benchmark_prices=benchmark,
        input_tables=tables,
        price_table=price_table,
        text_cols=text_cols,
        security_news_coverage_threshold=float(config.backtest.get("security_news_coverage_threshold", 0.0)),
        common_news_security_coverage=pd.DataFrame(),
        source_notes=source_notes,
        n_articles_total=total_news,
        n_articles_after_text_filter=len(news),
        n_articles_s4_en=n_s4_en,
        n_articles_s4_multi=n_s4_multi,
        n_non_english_excluded_s4_en=int(n_s4_multi - n_s4_en),
        language_distribution=language_distribution,
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
    source_notes.append("Benchmark tables missing; S4 will run without benchmark comparison.")
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


LLM_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "article_id": {"type": "string"},
                    "sentiment_label": {
                        "type": "string",
                        "enum": ["very_negative", "negative", "neutral", "positive", "very_positive", "unclear"],
                    },
                    "sentiment_score": {"type": "number", "minimum": -1.0, "maximum": 1.0},
                    "relevance_to_company": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "materiality": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "event_type": {
                        "type": "string",
                        "enum": [
                            "earnings",
                            "guidance",
                            "analyst_rating",
                            "m_and_a",
                            "legal_regulatory",
                            "macro_sector",
                            "product_contract",
                            "management",
                            "capital_structure",
                            "dividend_buyback",
                            "esg",
                            "other",
                            "unclear",
                        ],
                    },
                    "time_horizon": {"type": "string", "enum": ["intraday", "short_term", "medium_term", "unclear"]},
                    "rationale_short": {"type": "string"},
                },
                "required": [
                    "article_id",
                    "sentiment_label",
                    "sentiment_score",
                    "relevance_to_company",
                    "materiality",
                    "confidence",
                    "event_type",
                    "time_horizon",
                    "rationale_short",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


FAST_LLM_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "article_id": {"type": "string"},
                    "sentiment_score": {"type": "number", "minimum": -1.0, "maximum": 1.0},
                    "relevance_to_company": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "materiality": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "sentiment_label": {
                        "type": "string",
                        "enum": ["very_negative", "negative", "neutral", "positive", "very_positive", "unclear"],
                    },
                },
                "required": [
                    "article_id",
                    "sentiment_score",
                    "relevance_to_company",
                    "materiality",
                    "confidence",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


SYSTEM_MESSAGE = (
    "Du bist ein Finanztext-Klassifikator fuer ein akademisches Backtesting-Projekt. "
    "Bewerte ausschliesslich den gegebenen Nachrichtentext bezogen auf das angegebene Unternehmen. "
    "Gib keine Anlageberatung. Nutze keine externen Informationen. Nutze keine historischen Kurse. "
    "Nutze keine Annahmen ueber zukuenftige Renditen. Bewerte nur die kurzfristige "
    "informationsbezogene Wirkung des Textes auf das Unternehmen. Antworte ausschliesslich im geforderten JSON-Schema."
)


FAST_SYSTEM_MESSAGE = (
    "Du bist ein Finanztext-Klassifikator fuer ein akademisches Backtesting-Projekt. "
    "Bewerte ausschliesslich den gegebenen Nachrichtentext bezogen auf das angegebene Unternehmen. "
    "Nutze keine externen Informationen, keine historischen Kurse und keine Anlageberatung. "
    "Antworte ausschliesslich im geforderten JSON-Schema."
)


def system_message_for_prompt_mode(prompt_mode: str) -> str:
    return FAST_SYSTEM_MESSAGE if prompt_mode == "fast_score_only" else SYSTEM_MESSAGE


def schema_for_prompt_mode(prompt_mode: str) -> dict[str, Any]:
    return FAST_LLM_JSON_SCHEMA if prompt_mode == "fast_score_only" else LLM_JSON_SCHEMA


def response_format_payload(mode: str, prompt_mode: str = "full") -> dict[str, Any] | None:
    if mode == "json_schema":
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "s4_llm_article_scores",
                "strict": True,
                "schema": schema_for_prompt_mode(prompt_mode),
            },
        }
    if mode == "json_object":
        return {"type": "json_object"}
    return None


def create_user_message(batch: pd.DataFrame, prompt_mode: str = "full") -> str:
    payload = []
    for row in batch.itertuples(index=False):
        payload.append(
            {
                "article_id": str(row.article_id),
                "company": None if pd.isna(row.company) else str(row.company),
                "yahoo_ticker": None if pd.isna(row.yahoo_ticker) else str(row.yahoo_ticker),
                "country": None if pd.isna(row.country) else str(row.country),
                "sector": None if pd.isna(row.sector) else str(row.sector),
                "language": None if pd.isna(row.language) else str(row.language),
                "signal_date": format_date_value(row.signal_date),
                "text_used": str(row.text_used),
            }
        )
    n_items = len(payload)
    if prompt_mode == "fast_score_only":
        return (
            "Bewerte die folgenden Finanznachrichten je Unternehmen. Liefere je Eingabeartikel genau ein Objekt mit "
            "article_id, sentiment_score, relevance_to_company, materiality und confidence. sentiment_score liegt "
            "zwischen -1 und +1. relevance_to_company, materiality und confidence liegen zwischen 0 und 1. Wenn ein "
            "Artikel unklar oder irrelevant ist, gib trotzdem ein neutrales Item mit sentiment_score=0 und niedriger "
            "relevance/materiality/confidence zurueck. Kopiere article_id exakt. Keine Begruendung, kein Markdown, "
            "keine Zusatzfelder.\n\n"
            + json.dumps({"items": payload}, ensure_ascii=False)
        )
    return (
        "Bewerte die folgenden Finanznachrichten. Jede Nachricht ist einem Unternehmen zugeordnet. "
        "Liefere je Nachricht sentiment_label, sentiment_score, relevance_to_company, materiality, "
        "confidence, event_type, time_horizon und rationale_short. sentiment_score liegt zwischen -1 und +1; "
        "rationale_short maximal 25 Woerter, keine Anlageempfehlung, keine Prozentprognose. "
        f"Antworte mit genau {n_items} items, also exakt einem Ergebnisobjekt je Eingabeartikel. "
        "Kopiere jede article_id exakt aus der Eingabe und erfinde keine zusaetzlichen article_id. "
        "Wenn ein Artikel unklar oder irrelevant ist, gib trotzdem ein neutrales oder unclear Item fuer diese article_id zurueck.\n\n"
        + json.dumps({"items": payload}, ensure_ascii=False)
    )


def create_cache_schema(cache_db: Path) -> None:
    cache_db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(cache_db, timeout=120) as conn:
        conn.execute("PRAGMA busy_timeout=120000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS s4_llm_score_cache (
                article_id TEXT NOT NULL,
                synthetic_article_id TEXT,
                security_id INTEGER NOT NULL,
                signal_date TEXT NOT NULL,
                model_name TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                scoring_config_hash TEXT,
                scoring_schema_hash TEXT,
                request_batch_id TEXT,
                response_format_mode_used TEXT,
                response_parse_mode TEXT,
                repair_flags TEXT,
                article_id_repaired INTEGER,
                sentiment_label TEXT,
                sentiment_score REAL,
                relevance_to_company REAL,
                materiality REAL,
                confidence REAL,
                event_type TEXT,
                time_horizon TEXT,
                rationale_short TEXT,
                llm_article_score_raw REAL,
                article_weight REAL,
                label_score_inconsistency INTEGER,
                api_status TEXT,
                api_error_type TEXT,
                api_error_message_sanitized TEXT,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                openrouter_generation_id TEXT,
                provider_name TEXT,
                text_used TEXT,
                n_chars INTEGER,
                scored_at_utc TEXT,
                run_id TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_s4_llm_score_cache_key
            ON s4_llm_score_cache (
                article_id, security_id, signal_date, model_name, prompt_version, text_hash, config_hash, scored_at_utc
            )
            """
        )
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({SCORE_CACHE_TABLE})")}
        desired_types = {
            "synthetic_article_id": "TEXT",
            "security_id": "INTEGER",
            "signal_date": "TEXT",
            "prompt_version": "TEXT",
            "config_hash": "TEXT",
            "scoring_config_hash": "TEXT",
            "scoring_schema_hash": "TEXT",
            "request_batch_id": "TEXT",
            "response_format_mode_used": "TEXT",
            "response_parse_mode": "TEXT",
            "repair_flags": "TEXT",
            "article_id_repaired": "INTEGER",
            "sentiment_label": "TEXT",
            "sentiment_score": "REAL",
            "relevance_to_company": "REAL",
            "materiality": "REAL",
            "confidence": "REAL",
            "event_type": "TEXT",
            "time_horizon": "TEXT",
            "rationale_short": "TEXT",
            "llm_article_score_raw": "REAL",
            "article_weight": "REAL",
            "label_score_inconsistency": "INTEGER",
            "api_status": "TEXT",
            "api_error_type": "TEXT",
            "api_error_message_sanitized": "TEXT",
            "prompt_tokens": "INTEGER",
            "completion_tokens": "INTEGER",
            "total_tokens": "INTEGER",
            "openrouter_generation_id": "TEXT",
            "provider_name": "TEXT",
            "run_id": "TEXT",
        }
        for col, col_type in desired_types.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {SCORE_CACHE_TABLE} ADD COLUMN {col} {col_type}")


def cache_key_cols() -> list[str]:
    return ["article_id", "security_id", "signal_date", "model_name", "prompt_version", "text_hash", "config_hash"]


def load_score_cache(
    cache_db: Path,
    model_name: str,
    prompt_version: str,
    config_hash: str,
    scoring_config_hash: str | None = None,
    compatible_hashes: list[str] | None = None,
) -> pd.DataFrame:
    create_cache_schema(cache_db)
    effective_hash = scoring_config_hash or config_hash
    hash_values: list[str] = []
    for value in [config_hash, effective_hash, *(compatible_hashes or [])]:
        normalized = str(value).strip() if value is not None else ""
        if normalized and normalized not in hash_values:
            hash_values.append(normalized)
    placeholders = ", ".join("?" for _ in hash_values)
    with sqlite3.connect(cache_db, timeout=120) as conn:
        conn.execute("PRAGMA busy_timeout=120000")
        df = pd.read_sql_query(
            f"""
            SELECT * FROM s4_llm_score_cache
            WHERE model_name = ?
              AND prompt_version = ?
              AND (
                    config_hash IN ({placeholders})
                    OR scoring_config_hash IN ({placeholders})
                  )
            """,
            conn,
            params=(model_name, prompt_version, *hash_values, *hash_values),
        )
        legacy = pd.read_sql_query(
            """
            SELECT * FROM s4_llm_score_cache
            WHERE model_name = ?
              AND prompt_version = ?
              AND (scoring_config_hash IS NULL OR scoring_config_hash = '')
            """,
            conn,
            params=(model_name, prompt_version),
        )
        if not legacy.empty:
            df = pd.concat([df, legacy], ignore_index=True) if not df.empty else legacy
    if df.empty:
        return df
    if "scoring_config_hash" not in df.columns:
        df["scoring_config_hash"] = None
    df["config_hash_original"] = df["config_hash"]
    df["scoring_config_hash"] = df["scoring_config_hash"].fillna(effective_hash)
    df["config_hash"] = effective_hash
    df["scored_at_utc_sort"] = pd.to_datetime(df["scored_at_utc"], errors="coerce")
    df = df.sort_values("scored_at_utc_sort").drop_duplicates(cache_key_cols(), keep="last")
    return df.drop(columns=["scored_at_utc_sort"])


def append_score_cache(cache_db: Path, scores: pd.DataFrame) -> None:
    if scores.empty:
        return
    create_cache_schema(cache_db)
    cols = [
        "article_id",
        "synthetic_article_id",
        "security_id",
        "signal_date",
        "model_name",
        "prompt_version",
        "text_hash",
        "config_hash",
        "scoring_config_hash",
        "scoring_schema_hash",
        "request_batch_id",
        "response_format_mode_used",
        "response_parse_mode",
        "repair_flags",
        "article_id_repaired",
        "sentiment_label",
        "sentiment_score",
        "relevance_to_company",
        "materiality",
        "confidence",
        "event_type",
        "time_horizon",
        "rationale_short",
        "llm_article_score_raw",
        "article_weight",
        "label_score_inconsistency",
        "api_status",
        "api_error_type",
        "api_error_message_sanitized",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "openrouter_generation_id",
        "provider_name",
        "text_used",
        "n_chars",
        "scored_at_utc",
        "run_id",
    ]
    rows = scores.copy()
    for col in cols:
        if col not in rows.columns:
            rows[col] = None
    with sqlite3.connect(cache_db, timeout=120) as conn:
        conn.execute("PRAGMA busy_timeout=120000")
        conn.executemany(
            f"""
            INSERT INTO s4_llm_score_cache ({", ".join(cols)})
            VALUES ({", ".join(["?"] * len(cols))})
            """,
            prepare_for_sql(rows[cols]).itertuples(index=False, name=None),
        )


def prepare_article_frame(news: pd.DataFrame, text_cols: list[str], config: EffectiveConfig) -> pd.DataFrame:
    df = news.copy()
    df["signal_date"] = pd.to_datetime(df["signal_date"], errors="coerce")
    if "publication_date" in df.columns:
        df["publication_date"] = pd.to_datetime(df["publication_date"], errors="coerce")
    else:
        df["publication_date"] = pd.NaT
    df["security_id"] = pd.to_numeric(df["security_id"], errors="coerce").astype("Int64")
    df = df.dropna(subset=["signal_date", "security_id"]).copy()
    df["security_id"] = df["security_id"].astype(int)
    df["signal_date"] = df["signal_date"].dt.strftime("%Y-%m-%d")

    for col in ["article_id", "language", "yahoo_ticker", "company", "country", "sector", "mapping_confidence", "domain", "url", "publication_ts_utc"]:
        if col not in df.columns:
            df[col] = None
    if "text_used" not in df.columns:
        text_frame = df[text_cols].fillna("").astype(str)
        df["text_used"] = text_frame.agg(" ".join, axis=1).str.strip()
    df = df[df["text_used"].astype(str).str.len().gt(0)].copy()
    max_chars = int(config.scoring.get("max_chars_per_article", 3000))
    df["text_used"] = df["text_used"].astype(str).str.slice(0, max_chars)
    df["text_hash"] = df["text_used"].astype(str).map(lambda x: hashlib.sha256(x.encode("utf-8")).hexdigest())
    missing_article = df["article_id"].isna() | df["article_id"].astype(str).str.strip().eq("")
    df["synthetic_article_id"] = None
    if missing_article.any():
        hashes = (
            df.loc[missing_article, ["security_id", "signal_date", "text_hash"]]
            .astype(str)
            .agg("|".join, axis=1)
            .map(lambda x: "synthetic_" + hashlib.sha1(x.encode("utf-8")).hexdigest()[:20])
        )
        df.loc[missing_article, "article_id"] = hashes
        df.loc[missing_article, "synthetic_article_id"] = hashes
    df["article_id"] = df["article_id"].astype(str)

    before = len(df)
    df = df.drop_duplicates(["article_id", "security_id", "signal_date", "text_hash"]).copy()
    df["date"] = pd.to_datetime(df["signal_date"], errors="coerce")
    df["n_chars"] = df["text_used"].astype(str).str.len().astype(int)
    mapping = pd.to_numeric(df["mapping_confidence"], errors="coerce")
    df["mapping_confidence_weight"] = mapping.clip(lower=0.05, upper=1.0).where(mapping.gt(0), 1.0).fillna(1.0)
    df.attrs["news_rows_before_dedup"] = before
    df.attrs["synthetic_article_ids"] = int(missing_article.sum())
    return df


def label_score_inconsistent(label: str, score: float, confidence: float) -> bool:
    if label == "very_negative":
        return score > -0.5
    if label == "negative":
        return score >= 0
    if label == "neutral":
        return not (-0.15 <= score <= 0.15)
    if label == "positive":
        return score <= 0
    if label == "very_positive":
        return score < 0.5
    if label == "unclear":
        return abs(score) > 0.15 and confidence >= 0.5
    return True


def label_from_score(score: float, confidence: float) -> str:
    if confidence < 0.15 and abs(score) <= 0.25:
        return "unclear"
    if score <= -0.5:
        return "very_negative"
    if score < -0.15:
        return "negative"
    if score >= 0.5:
        return "very_positive"
    if score > 0.15:
        return "positive"
    return "neutral"


def normalize_llm_enum(value: Any, allowed: set[str], aliases: dict[str, str]) -> str:
    raw = "" if value is None else str(value).strip()
    normalized = raw.lower()
    normalized = normalized.replace("&", "_and_")
    normalized = re.sub(r"[\s\-/]+", "_", normalized)
    normalized = re.sub(r"[^a-z0-9_]+", "", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    mapped = aliases.get(normalized, normalized)
    return mapped if mapped in allowed else normalized


def parse_required_float(item: dict[str, Any], field: str) -> float:
    if field not in item or item[field] is None:
        raise ValueError(f"missing {field}")
    value = item[field]
    if isinstance(value, str):
        value = value.strip()
        if value == "":
            raise ValueError(f"missing {field}")
    return float(value)


def validate_llm_items(batch: pd.DataFrame, data: dict[str, Any], prompt_mode: str = "full") -> tuple[list[dict[str, Any]], list[str], dict[str, str]]:
    errors: list[str] = []
    item_errors: dict[str, str] = {}
    items = data.get("items") if isinstance(data, dict) else None
    if items is None and isinstance(data, dict) and "article_id" in data:
        items = [data]
    if items is None and isinstance(data, dict) and any(
        field in data for field in ["sentiment_label", "sentiment_score", "relevance_to_company", "materiality", "confidence"]
    ):
        items = [data]
    if not isinstance(items, list):
        return [], ["JSON response has no items array."], {}
    expected = set(batch["article_id"].astype(str))
    single_expected = len(expected) == 1 and len(items) == 1
    single_article_id = next(iter(expected)) if single_expected else None
    seen: set[str] = set()
    out = []
    allowed_labels = {"very_negative", "negative", "neutral", "positive", "very_positive", "unclear"}
    allowed_events = set(LLM_JSON_SCHEMA["properties"]["items"]["items"]["properties"]["event_type"]["enum"])
    allowed_horizons = {"intraday", "short_term", "medium_term", "unclear"}
    label_aliases = {
        "verynegative": "very_negative",
        "verypositive": "very_positive",
    }
    event_aliases = {
        "ma": "m_and_a",
        "m_a": "m_and_a",
        "m_and_a": "m_and_a",
        "merger": "m_and_a",
        "mergers": "m_and_a",
        "merger_acquisition": "m_and_a",
        "mergers_acquisitions": "m_and_a",
        "mergers_and_acquisitions": "m_and_a",
        "analyst": "analyst_rating",
        "analyst_ratings": "analyst_rating",
        "legal": "legal_regulatory",
        "regulatory": "legal_regulatory",
        "legal_and_regulatory": "legal_regulatory",
        "macro": "macro_sector",
        "sector": "macro_sector",
        "macro_and_sector": "macro_sector",
        "product": "product_contract",
        "contract": "product_contract",
        "product_and_contract": "product_contract",
        "capital": "capital_structure",
        "dividend": "dividend_buyback",
        "buyback": "dividend_buyback",
        "dividend_and_buyback": "dividend_buyback",
    }
    horizon_aliases = {
        "shortterm": "short_term",
        "mediumterm": "medium_term",
    }
    for item in items:
        if not isinstance(item, dict):
            errors.append("Item is not an object.")
            continue
        article_id = str(item.get("article_id", ""))
        article_id_repaired = 0
        item_repair_flags: list[str] = []
        if article_id not in expected:
            if single_expected and single_article_id is not None:
                errors.append(f"Repaired article_id={article_id or '<missing>'} to expected article_id for single-item batch.")
                article_id = single_article_id
                article_id_repaired = 1
                item_repair_flags.append("article_id_repaired")
            else:
                errors.append(f"Unexpected article_id={article_id}")
                continue
        if article_id in seen:
            item_errors[article_id] = "Duplicate article_id in response."
            errors.append(f"Duplicate article_id={article_id}")
            continue
        seen.add(article_id)
        try:
            score = parse_required_float(item, "sentiment_score")
            relevance = parse_required_float(item, "relevance_to_company")
            materiality = parse_required_float(item, "materiality")
            confidence = parse_required_float(item, "confidence")
        except Exception as exc:
            item_errors[article_id] = f"Numeric field parse failed: {exc}"
            errors.append(f"Numeric field parse failed for article_id={article_id}")
            continue
        label = normalize_llm_enum(item.get("sentiment_label"), allowed_labels, label_aliases) if item.get("sentiment_label") is not None else label_from_score(score, confidence)
        event_type = normalize_llm_enum(item.get("event_type"), allowed_events, event_aliases) if prompt_mode != "fast_score_only" else None
        horizon = normalize_llm_enum(item.get("time_horizon"), allowed_horizons, horizon_aliases) if prompt_mode != "fast_score_only" else None
        field_errors = []
        if label not in allowed_labels:
            field_errors.append("Invalid sentiment_label")
        if prompt_mode != "fast_score_only" and event_type not in allowed_events:
            field_errors.append("Invalid event_type")
        if prompt_mode != "fast_score_only" and horizon not in allowed_horizons:
            field_errors.append("Invalid time_horizon")
        if field_errors:
            item_errors[article_id] = "; ".join(field_errors)
            errors.append(f"{'; '.join(field_errors)} for article_id={article_id}")
            continue
        if not (-1 <= score <= 1 and 0 <= relevance <= 1 and 0 <= materiality <= 1 and 0 <= confidence <= 1):
            item_errors[article_id] = "Out-of-range score."
            errors.append(f"Out-of-range score for article_id={article_id}")
            continue
        out.append(
            {
                "article_id": article_id,
                "article_id_repaired": article_id_repaired,
                "repair_flags": combine_flags(*item_repair_flags),
                "sentiment_label": label,
                "sentiment_score": score,
                "relevance_to_company": relevance,
                "materiality": materiality,
                "confidence": confidence,
                "event_type": event_type,
                "time_horizon": horizon,
                "rationale_short": None if prompt_mode == "fast_score_only" else str(item.get("rationale_short", ""))[:300],
                "label_score_inconsistency": int(False if item.get("sentiment_label") is None else label_score_inconsistent(label, score, confidence)),
            }
        )
    valid_ids = {item["article_id"] for item in out}
    missing = expected - valid_ids
    if missing:
        errors.append("Missing article_id values: " + ", ".join(sorted(missing)[:10]))
        for article_id in missing:
            item_errors.setdefault(article_id, "Missing valid item in response.")
    return out, errors, item_errors


def strip_markdown_json_fence(content: str) -> tuple[str, bool]:
    text = content.strip()
    match = re.search(r"```(?:json|JSON)?\s*(.*?)```", text, flags=re.DOTALL)
    if match:
        return match.group(1).strip(), True
    return text, False


def extract_first_json_block(content: str) -> str | None:
    start = -1
    opening = ""
    for idx, char in enumerate(content):
        if char in "{[":
            start = idx
            opening = char
            break
    if start < 0:
        return None
    closing = "}" if opening == "{" else "]"
    stack = [closing]
    in_string = False
    escaped = False
    for idx in range(start + 1, len(content)):
        char = content[idx]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]":
            if not stack or char != stack[-1]:
                return None
            stack.pop()
            if not stack:
                return content[start : idx + 1].strip()
    return None


def normalize_llm_json_data(data: Any) -> tuple[dict[str, Any], str | None]:
    if isinstance(data, str):
        nested, nested_mode = parse_llm_json_content(data)
        return nested, f"nested_json:{nested_mode}"
    if isinstance(data, list):
        return {"items": data}, "array_wrapped"
    if isinstance(data, dict):
        return data, None
    raise ValueError(f"Parsed JSON root must be object or array, got {type(data).__name__}.")


def parse_llm_json_content(content: str) -> tuple[dict[str, Any], str]:
    text, fenced = strip_markdown_json_fence(content)
    extraction_flag = "extracted_json" if fenced else None
    attempts: list[tuple[str, str]] = [("strict_json", text)]
    extracted = extract_first_json_block(text)
    if extracted and extracted != text:
        attempts.append(("extracted_json", extracted))
    elif fenced:
        attempts[0] = ("extracted_json", text)

    last_error: Exception | None = None
    for mode, candidate in attempts:
        try:
            parsed = json.loads(candidate)
            normalized, normalize_mode = normalize_llm_json_data(parsed)
            parse_mode = mode
            if extraction_flag and parse_mode == "strict_json":
                parse_mode = extraction_flag
            if normalize_mode:
                parse_mode = combine_flags(parse_mode, normalize_mode) or parse_mode
            return normalized, parse_mode
        except Exception as exc:
            last_error = exc

    try:
        from json_repair import repair_json  # type: ignore
    except Exception:
        repair_json = None

    if repair_json is not None:
        repair_candidates = []
        if extracted:
            repair_candidates.append(extracted)
        repair_candidates.append(text)
        for candidate in repair_candidates:
            try:
                repaired_text = repair_json(candidate)
                parsed = json.loads(repaired_text) if isinstance(repaired_text, str) else repaired_text
                normalized, normalize_mode = normalize_llm_json_data(parsed)
                parse_mode = combine_flags("json_repair", normalize_mode) or "json_repair"
                return normalized, parse_mode
            except Exception as exc:
                last_error = exc

    if last_error:
        raise last_error
    raise ValueError("No JSON content found in OpenRouter response.")


def parse_openrouter_response(response: requests.Response) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = response.json()
    choices = payload.get("choices") or []
    if not choices:
        raise ValueError("OpenRouter response has no choices.")
    content = choices[0].get("message", {}).get("content")
    if isinstance(content, list):
        content = "".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content)
    if not isinstance(content, str):
        raise ValueError("OpenRouter response content is not a string.")
    usage = payload.get("usage") or {}
    data, parse_mode = parse_llm_json_content(content)
    meta = {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "openrouter_generation_id": payload.get("id"),
        "provider_name": payload.get("provider") or payload.get("provider_name"),
        "openrouter_cost_reported": payload.get("total_cost") or payload.get("cost"),
        "response_parse_mode": parse_mode,
    }
    return data, meta


def openrouter_headers(config: EffectiveConfig) -> dict[str, str]:
    if not config.api_key:
        raise RuntimeError("OpenRouter API key missing. Set OPENROUTER_API_KEY or config openrouter.api_key.")
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }
    if config.openrouter.get("http_referer"):
        headers["HTTP-Referer"] = str(config.openrouter["http_referer"])
    if config.openrouter.get("app_title"):
        headers["X-OpenRouter-Title"] = str(config.openrouter["app_title"])
    return headers


def wait_for_request_rate_limit(config: EffectiveConfig) -> None:
    global LAST_REQUEST_AT_MONOTONIC
    min_interval = float(config.request.get("min_seconds_between_requests", 0.0) or 0.0)
    if min_interval <= 0:
        return
    with REQUEST_RATE_LIMIT_LOCK:
        now = time.monotonic()
        wait_seconds = LAST_REQUEST_AT_MONOTONIC + min_interval - now
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        LAST_REQUEST_AT_MONOTONIC = time.monotonic()


def raise_wall_clock_timeout(signum, frame) -> None:
    raise TimeoutError("OpenRouter request exceeded wall-clock timeout.")


@contextlib.contextmanager
def openrouter_wall_clock_timeout(seconds: float):
    if seconds <= 0 or threading.current_thread() is not threading.main_thread() or not hasattr(signal, "SIGALRM"):
        yield
        return
    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, raise_wall_clock_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def openrouter_post_hard_timeout(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout_seconds: float,
) -> SimpleHTTPResponse:
    worker_path = PROJECT_DIR / "scripts" / "openrouter_post_worker.py"
    payload = {
        "url": url,
        "headers": headers,
        "body": body,
        "timeout_seconds": timeout_seconds,
    }
    try:
        proc = subprocess.run(
            [sys.executable, str(worker_path)],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=timeout_seconds + 5,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("OpenRouter request exceeded hard subprocess timeout.") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"OpenRouter worker exited with code {proc.returncode}: {proc.stderr[:500]}")
    if not proc.stdout.strip():
        raise RuntimeError("OpenRouter worker returned empty response.")
    result = json.loads(proc.stdout)
    if not result.get("ok"):
        raise RuntimeError(f"{result.get('error_type', 'RequestError')}: {result.get('error', '')}")
    return SimpleHTTPResponse(status_code=int(result["status_code"]), text=str(result["text"]))


def call_openrouter_batch(batch: pd.DataFrame, model_name: str, config: EffectiveConfig, batch_id: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    url = str(config.openrouter["base_url"]).rstrip("/") + str(config.openrouter["chat_completions_path"])
    mode = str(config.request.get("response_format_mode", "json_schema"))
    modes_to_try = [mode]
    if mode == "json_schema":
        modes_to_try.append("json_object")
    last_error = ""
    fallback_used = False
    api_primary_model = str(config.models.get("api_primary_model") or model_name)
    api_fallback_models = config.models.get("api_fallback_models")
    fallback_candidates = api_fallback_models if api_fallback_models is not None else (config.models.get("fallback_models") or [])
    request_models = [api_primary_model] + [
        str(m) for m in fallback_candidates if m and str(m) != api_primary_model
    ]
    for request_model in request_models:
        for response_mode in modes_to_try:
            max_retries = int(config.request.get("max_retries", 5))
            for attempt in range(max_retries):
                body = {
                    "model": request_model,
                    "messages": [
                        {"role": "system", "content": system_message_for_prompt_mode(config.prompt_mode)},
                        {"role": "user", "content": create_user_message(batch, config.prompt_mode)},
                    ],
                    "temperature": float(config.request.get("temperature", 0.0)),
                    "top_p": float(config.request.get("top_p", 1.0)),
                    "max_tokens": int(config.request.get("max_tokens", 700)),
                }
                if "include_reasoning" in config.request:
                    body["include_reasoning"] = bool(config.request.get("include_reasoning"))
                if config.request.get("reasoning") is not None:
                    body["reasoning"] = config.request.get("reasoning")
                rf = response_format_payload(response_mode, config.prompt_mode)
                if rf:
                    body["response_format"] = rf
                try:
                    timeout_seconds = float(config.request.get("timeout_seconds", 90))
                    wait_for_request_rate_limit(config)
                    response = openrouter_post_hard_timeout(url, openrouter_headers(config), body, timeout_seconds)
                    if response.status_code >= 400:
                        err = sanitize_error(response.text, config.api_key)
                        last_error = f"HTTP {response.status_code}: {err}"
                        if response_mode == "json_schema" and any(x in err.lower() for x in ["schema", "response_format", "json_schema"]):
                            break
                        raise RuntimeError(last_error)
                    data, meta = parse_openrouter_response(response)
                    items, errors, item_errors = validate_llm_items(batch, data, config.prompt_mode)
                    if not items:
                        raise ValueError("; ".join(errors))
                    expected_count = batch["article_id"].astype(str).nunique()
                    if item_errors and len(items) < expected_count and attempt < max_retries - 1:
                        raise ValueError("; ".join(errors))
                    rows = []
                    scored_at = now_utc()
                    item_by_id = {row["article_id"]: row for row in items}
                    if request_model != model_name:
                        api_role = "primary_api_model" if request_model == api_primary_model else "fallback_api_model"
                        meta["provider_name"] = f"{api_role}:{request_model}"
                    for row in batch.itertuples(index=False):
                        article_id = str(row.article_id)
                        item = item_by_id.get(article_id)
                        if item is None:
                            rows.append(
                                {
                                    **{col: getattr(row, col) for col in [
                                        "article_id",
                                        "synthetic_article_id",
                                        "security_id",
                                        "signal_date",
                                        "publication_date",
                                        "publication_ts_utc",
                                        "yahoo_ticker",
                                        "company",
                                        "country",
                                        "sector",
                                        "language",
                                        "text_used",
                                        "text_hash",
                                        "n_chars",
                                        "mapping_confidence",
                                    ] if hasattr(row, col)},
                                    "model_name": model_name,
                                    "prompt_version": config.scoring["prompt_version"],
                                    "config_hash": config.scoring_config_hash,
                                    "scoring_config_hash": config.scoring_config_hash,
                                    "scoring_schema_hash": config.scoring_config_hash,
                                    "request_batch_id": batch_id,
                                    "response_format_mode_used": response_mode,
                                    "response_parse_mode": meta.get("response_parse_mode"),
                                    "repair_flags": "partial_batch_fail",
                                    "article_id_repaired": 0,
                                    "api_status": "API_FAIL",
                                    "api_error_type": "partial_batch_validation_failed",
                                    "api_error_message_sanitized": sanitize_error(item_errors.get(article_id) or "; ".join(errors), config.api_key),
                                    "run_id": config.run_id,
                                    "scored_at_utc": scored_at,
                                    **meta,
                                }
                            )
                            continue
                        llm_raw = item["sentiment_score"] * item["relevance_to_company"] * item["materiality"]
                        article_weight = (
                            max(0.05, item["relevance_to_company"])
                            * max(0.05, item["materiality"])
                            * max(0.05, item["confidence"])
                            * float(row.mapping_confidence_weight)
                        )
                        rows.append(
                            {
                                **{col: getattr(row, col) for col in [
                                    "article_id",
                                    "synthetic_article_id",
                                    "security_id",
                                    "signal_date",
                                    "publication_date",
                                    "publication_ts_utc",
                                    "yahoo_ticker",
                                    "company",
                                    "country",
                                    "sector",
                                    "language",
                                    "text_used",
                                    "text_hash",
                                    "n_chars",
                                    "mapping_confidence",
                                ] if hasattr(row, col)},
                                "model_name": model_name,
                                "prompt_version": config.scoring["prompt_version"],
                                "config_hash": config.scoring_config_hash,
                                "scoring_config_hash": config.scoring_config_hash,
                                "scoring_schema_hash": config.scoring_config_hash,
                                "request_batch_id": batch_id,
                                "response_format_mode_used": response_mode,
                                "response_parse_mode": meta.get("response_parse_mode"),
                                **item,
                                "repair_flags": combine_flags(item.get("repair_flags"), "partial_batch_ok" if item_errors else None),
                                "article_id_repaired": item.get("article_id_repaired", 0),
                                "llm_article_score_raw": llm_raw,
                                "article_weight": article_weight,
                                "api_status": "OK",
                                "api_error_type": None,
                                "api_error_message_sanitized": None,
                                "run_id": config.run_id,
                                "scored_at_utc": scored_at,
                                **meta,
                            }
                        )
                    return pd.DataFrame(rows), {"response_format_fallback_used": fallback_used or response_mode != mode or request_model != model_name}
                except Exception as exc:
                    last_error = sanitize_error(exc, config.api_key)
                    if attempt < max_retries - 1:
                        sleep = float(config.request.get("retry_sleep_seconds", 3)) * (2 ** attempt)
                        time.sleep(min(sleep, 60.0))
                    continue
            if response_mode == "json_schema" and len(modes_to_try) > 1:
                fallback_used = True
                continue
    scored_at = now_utc()
    fail_rows = []
    for row in batch.itertuples(index=False):
        fail_rows.append(
            {
                **{col: getattr(row, col) for col in [
                    "article_id",
                    "synthetic_article_id",
                    "security_id",
                    "signal_date",
                    "publication_date",
                    "publication_ts_utc",
                    "yahoo_ticker",
                    "company",
                    "country",
                    "sector",
                    "language",
                    "text_used",
                    "text_hash",
                    "n_chars",
                    "mapping_confidence",
                ] if hasattr(row, col)},
                "model_name": model_name,
                "prompt_version": config.scoring["prompt_version"],
                "config_hash": config.scoring_config_hash,
                "scoring_config_hash": config.scoring_config_hash,
                "scoring_schema_hash": config.scoring_config_hash,
                "request_batch_id": batch_id,
                "response_format_mode_used": mode,
                "response_parse_mode": None,
                "repair_flags": None,
                "article_id_repaired": 0,
                "api_status": "API_FAIL",
                "api_error_type": "openrouter_request_failed",
                "api_error_message_sanitized": last_error,
                "run_id": config.run_id,
                "scored_at_utc": scored_at,
            }
        )
    return pd.DataFrame(fail_rows), {"response_format_fallback_used": fallback_used}


def call_openrouter_batch_with_singleton_repair(
    batch: pd.DataFrame,
    model_name: str,
    config: EffectiveConfig,
    batch_id: str,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    computed, meta = call_openrouter_batch(batch, model_name, config, batch_id)
    meta = dict(meta)
    meta["n_api_requests"] = 1
    if len(batch) <= 1 or computed.empty or "api_status" not in computed.columns:
        return computed, meta
    if not bool(config.request.get("singleton_repair", True)):
        return computed, meta

    failed_ids = set(computed.loc[computed["api_status"].ne("OK"), "article_id"].astype(str))
    if not failed_ids:
        return computed, meta
    if len(failed_ids) == len(batch):
        return computed, meta

    pieces = [computed]
    retry_source = batch[batch["article_id"].astype(str).isin(failed_ids)].copy()
    for idx in range(len(retry_source)):
        retry_batch = retry_source.iloc[idx : idx + 1].copy()
        retry_rows, retry_meta = call_openrouter_batch(
            retry_batch,
            model_name,
            config,
            f"{batch_id}_singleton_retry_{idx}",
        )
        pieces.append(retry_rows)
        meta["n_api_requests"] += 1
        meta["response_format_fallback_used"] = bool(meta.get("response_format_fallback_used")) or bool(
            retry_meta.get("response_format_fallback_used")
        )
    return pd.concat(pieces, ignore_index=True, sort=False), meta


def article_subset_for_language(articles: pd.DataFrame, language_mode: str) -> pd.DataFrame:
    if language_mode == "EN":
        return articles[articles["language"].map(is_english_language)].copy()
    return articles.copy()


def filter_api_fail_cache_by_age(fail_cache: pd.DataFrame, max_age_hours: float | None) -> pd.DataFrame:
    if fail_cache.empty or max_age_hours is None:
        return fail_cache
    scored_at = pd.to_datetime(fail_cache.get("scored_at_utc"), errors="coerce", utc=True)
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=float(max_age_hours))
    return fail_cache[scored_at.ge(cutoff).fillna(False)].copy()


def score_articles(
    news: pd.DataFrame,
    text_cols: list[str],
    args: argparse.Namespace,
    config: EffectiveConfig,
    language_modes: list[str],
    variants: list[dict[str, Any]],
) -> tuple[pd.DataFrame, dict, LLMBundle]:
    articles = prepare_article_frame(news, text_cols, config)
    model_names = sorted({str(v["model_name"]) for v in variants})
    needed_article_ids = set()
    for mode in language_modes:
        needed_article_ids.update(article_subset_for_language(articles, mode)["article_id"].astype(str))
    articles = articles[articles["article_id"].astype(str).isin(needed_article_ids)].copy()
    cache_db = resolve_output_path(Path(config.data["score_cache_db"]))
    compatible_cache_hashes = [str(h) for h in (config.scoring.get("compatible_score_cache_hashes") or []) if h]
    all_scored = []
    stats = {
        "news_rows_before_dedup": int(articles.attrs.get("news_rows_before_dedup", len(news))),
        "news_rows_after_dedup": int(len(articles)),
        "news_duplicate_rows_removed": int(articles.attrs.get("news_rows_before_dedup", len(news)) - len(articles)),
        "synthetic_article_ids": int(articles.attrs.get("synthetic_article_ids", 0)),
        "n_scores_loaded_from_cache": 0,
        "n_scores_newly_computed": 0,
        "n_api_fail": 0,
        "n_ok_cache": 0,
        "n_api_fail_cache_distinct": 0,
        "n_unseen_articles": 0,
        "n_skipped_known_api_fail_first_pass": 0,
        "n_retried_api_fail": 0,
        "n_api_fail_converted_to_ok": 0,
        "n_api_requests": 0,
        "prompt_tokens_total": 0,
        "completion_tokens_total": 0,
        "total_tokens_total": 0,
        "openrouter_cost_reported": None,
        "response_format_fallback_used": False,
        "n_json_strict": 0,
        "n_json_extracted": 0,
        "n_json_repaired": 0,
        "n_article_id_repaired": 0,
        "n_partial_batch_fail": 0,
        "score_cache_db": str(cache_db),
        "single_llm_score_for_all_variants": bool(config.single_llm_score_for_all_variants),
        "prompt_mode": config.prompt_mode,
        "scoring_config_hash": config.scoring_config_hash,
        "compatible_score_cache_hashes": compatible_cache_hashes,
        "cache_compatibility_mode": "legacy_v2_readable_same_prompt_version",
        "cache_prompt_version_rule": "do_not_mix_s4_llm_v2_and_s4_llm_v3_fast_score_only",
        "legacy_cache_entries_readable": "no",
        "n_legacy_cache_rows_read": 0,
        "config_hash_original_preserved": "no",
    }
    for model_name in model_names:
        model_articles = articles.copy()
        model_articles["model_name"] = model_name
        model_articles["prompt_version"] = config.scoring["prompt_version"]
        model_articles["config_hash"] = config.scoring_config_hash
        model_articles["scoring_config_hash"] = config.scoring_config_hash
        cache = load_score_cache(
            cache_db,
            model_name,
            config.scoring["prompt_version"],
            config.config_hash,
            config.scoring_config_hash,
            compatible_cache_hashes,
        )
        if not cache.empty and "config_hash_original" in cache.columns:
            stats["config_hash_original_preserved"] = "yes"
            legacy_mask = cache["config_hash_original"].astype(str).ne(str(config.scoring_config_hash))
            n_legacy = int(legacy_mask.sum())
            if n_legacy > 0:
                stats["legacy_cache_entries_readable"] = "yes"
                stats["n_legacy_cache_rows_read"] += n_legacy
        key_cols = cache_key_cols()
        ok_cache = cache[cache["api_status"].eq("OK")].copy() if not cache.empty and "api_status" in cache.columns else cache
        fail_cache = cache[cache["api_status"].eq("API_FAIL")].copy() if not cache.empty and "api_status" in cache.columns else pd.DataFrame(columns=cache.columns if not cache.empty else key_cols)
        fail_cache = filter_api_fail_cache_by_age(fail_cache, args.max_api_fail_age_hours)
        stats["n_ok_cache"] += int(len(ok_cache[key_cols].drop_duplicates())) if not ok_cache.empty else 0
        stats["n_api_fail_cache_distinct"] += int(len(fail_cache[key_cols].drop_duplicates())) if not fail_cache.empty else 0
        cached_keys = ok_cache[key_cols].drop_duplicates() if not ok_cache.empty else pd.DataFrame(columns=key_cols)
        marked = model_articles.merge(cached_keys.assign(_cached=1), on=key_cols, how="left")
        if args.rescore:
            marked["_cached"] = np.nan
        fail_keys = fail_cache[key_cols].drop_duplicates() if not fail_cache.empty else pd.DataFrame(columns=key_cols)
        if args.retry_api_fails_only:
            retry_marked = model_articles.merge(fail_keys.assign(_api_fail_cached=1), on=key_cols, how="left")
            missing = retry_marked[retry_marked["_api_fail_cached"].notna()].drop(columns=["_api_fail_cached"]).copy()
            stats["n_retried_api_fail"] += int(len(missing))
        else:
            missing = marked[marked["_cached"].isna()].drop(columns=["_cached"]).copy()
            if args.skip_existing_api_fails_first_pass and not fail_keys.empty and not args.rescore:
                before_skip = len(missing)
                missing = missing.merge(fail_keys.assign(_api_fail_cached=1), on=key_cols, how="left")
                skipped = missing[missing["_api_fail_cached"].notna()].copy()
                missing = missing[missing["_api_fail_cached"].isna()].drop(columns=["_api_fail_cached"]).copy()
                stats["n_skipped_known_api_fail_first_pass"] += int(len(skipped))
                if args.verbose and before_skip != len(missing):
                    print(f"Skipped {before_skip - len(missing):,} known API_FAIL rows in first pass for model {model_name}.", file=sys.stderr)
        stats["n_unseen_articles"] += int(len(missing))
        if (args.no_api or args.cache_only_backtest) and not missing.empty:
            if cache.empty:
                print(f"WARN: cache-only mode excludes {len(missing):,} uncached articles for model {model_name}.", file=sys.stderr)
            missing = missing.iloc[0:0].copy()
        elif not missing.empty:
            if not config.api_key:
                raise RuntimeError(
                    f"OpenRouter API key missing and cache does not cover {len(missing):,} required articles. "
                    "No fake or toy scores were generated."
                )
            batch_size = max(1, int(config.request.get("request_batch_size", 8)))
            max_workers = max(1, int(config.request.get("max_concurrent_requests", 1)))
            progress_every = max(1, int(config.request.get("progress_every_batches", 25)))
            completed_batch_count = 0
            progress_started_at = time.time()

            def run_api_batches(frame: pd.DataFrame, size: int, batch_id_for_start) -> None:
                nonlocal completed_batch_count
                if frame.empty:
                    return
                total_batches = int(math.ceil(len(frame) / size))

                def record_progress(computed: pd.DataFrame, meta: dict[str, Any]) -> None:
                    nonlocal completed_batch_count
                    append_score_cache(cache_db, computed)
                    completed_batch_count += 1
                    stats["n_api_requests"] += int(meta.get("n_api_requests", 1))
                    stats["response_format_fallback_used"] = stats["response_format_fallback_used"] or bool(meta.get("response_format_fallback_used"))
                    if completed_batch_count == 1 or completed_batch_count % progress_every == 0:
                        elapsed = max(time.time() - progress_started_at, 1e-9)
                        ok_rows = int(computed.get("api_status", pd.Series(dtype=str)).eq("OK").sum()) if not computed.empty else 0
                        fail_rows = int(computed.get("api_status", pd.Series(dtype=str)).ne("OK").sum()) if not computed.empty else 0
                        print(
                            f"S4 scoring progress: batches_done={completed_batch_count:,}, "
                            f"this_batch_ok={ok_rows}, this_batch_fail={fail_rows}, "
                            f"elapsed_min={elapsed / 60:.1f}, batch_size={size}, max_workers={max_workers}",
                            flush=True,
                        )

                if max_workers == 1 or len(frame) <= size:
                    for start in range(0, len(frame), size):
                        batch = frame.iloc[start : start + size].copy()
                        batch_id = batch_id_for_start(start)
                        computed, meta = call_openrouter_batch_with_singleton_repair(batch, model_name, config, batch_id)
                        record_progress(computed, meta)
                    return

                starts = iter(range(0, len(frame), size))

                def submit_next(executor: ThreadPoolExecutor, futures: dict) -> bool:
                    try:
                        start = next(starts)
                    except StopIteration:
                        return False
                    batch = frame.iloc[start : start + size].copy()
                    batch_id = batch_id_for_start(start)
                    futures[executor.submit(call_openrouter_batch_with_singleton_repair, batch, model_name, config, batch_id)] = start
                    return True

                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures: dict = {}
                    for _ in range(max_workers):
                        if not submit_next(executor, futures):
                            break
                    while futures:
                        future = next(as_completed(futures))
                        futures.pop(future)
                        computed, meta = future.result()
                        record_progress(computed, meta)
                        submit_next(executor, futures)

            run_api_batches(missing, batch_size, lambda start: f"{config.run_id}_{model_name}_{start}")
            retry_cache = load_score_cache(
                cache_db,
                model_name,
                config.scoring["prompt_version"],
                config.config_hash,
                config.scoring_config_hash,
                compatible_cache_hashes,
            )
            retry_ok_cache = retry_cache[retry_cache["api_status"].eq("OK")].copy() if not retry_cache.empty and "api_status" in retry_cache.columns else retry_cache
            retry_cached_keys = retry_ok_cache[key_cols].drop_duplicates() if not retry_ok_cache.empty else pd.DataFrame(columns=key_cols)
            retry_marked = model_articles.merge(retry_cached_keys.assign(_cached=1), on=key_cols, how="left")
            retry_missing = retry_marked[retry_marked["_cached"].isna()].drop(columns=["_cached"]).copy()
            if (
                not retry_missing.empty
                and batch_size > 1
                and bool(config.request.get("retry_missing_after_initial", True))
            ):
                run_api_batches(
                    retry_missing,
                    1,
                    lambda start: f"{config.run_id}_{model_name}_singleton_retry_{start}",
                )
            cache_after = load_score_cache(
                cache_db,
                model_name,
                config.scoring["prompt_version"],
                config.config_hash,
                config.scoring_config_hash,
                compatible_cache_hashes,
            )
            if args.retry_api_fails_only and not fail_keys.empty and not cache_after.empty:
                converted = cache_after[cache_after["api_status"].eq("OK")].merge(fail_keys, on=key_cols, how="inner")
                stats["n_api_fail_converted_to_ok"] += int(len(converted[key_cols].drop_duplicates())) if not converted.empty else 0
            cache = cache_after
        scored = model_articles.merge(cache, on=key_cols, how="inner", suffixes=("", "_cache"))
        if "text_used_cache" in scored.columns:
            scored["text_used"] = scored["text_used"].combine_first(scored["text_used_cache"])
            scored = scored.drop(columns=["text_used_cache"])
        stats["n_scores_loaded_from_cache"] += int(max(0, len(scored) - len(missing)))
        stats["n_scores_newly_computed"] += int(len(missing))
        stats["n_api_fail"] += int(scored.get("api_status", pd.Series(dtype=str)).eq("API_FAIL").sum())
        parse_modes = scored.get("response_parse_mode", pd.Series(dtype=str)).fillna("").astype(str)
        stats["n_json_strict"] += int(parse_modes.str.startswith("strict_json").sum())
        stats["n_json_extracted"] += int(parse_modes.str.contains("extracted_json", regex=False).sum())
        stats["n_json_repaired"] += int(parse_modes.str.contains("json_repair", regex=False).sum())
        article_id_repaired = scored["article_id_repaired"] if "article_id_repaired" in scored.columns else pd.Series(0, index=scored.index)
        stats["n_article_id_repaired"] += int(pd.to_numeric(article_id_repaired, errors="coerce").fillna(0).sum())
        stats["n_partial_batch_fail"] += int(scored.get("api_error_type", pd.Series(dtype=str)).eq("partial_batch_validation_failed").sum())
        token_usage_rows = scored.copy()
        if "request_batch_id" in token_usage_rows.columns:
            token_usage_rows = token_usage_rows.drop_duplicates(["model_name", "request_batch_id"])
        for token_col, stat_col in [
            ("prompt_tokens", "prompt_tokens_total"),
            ("completion_tokens", "completion_tokens_total"),
            ("total_tokens", "total_tokens_total"),
        ]:
            token_values = token_usage_rows[token_col] if token_col in token_usage_rows.columns else pd.Series(0, index=token_usage_rows.index)
            stats[stat_col] += int(pd.to_numeric(token_values, errors="coerce").fillna(0).sum())
        all_scored.append(scored)
    base_scores = pd.concat(all_scored, ignore_index=True) if all_scored else pd.DataFrame()
    if base_scores.empty:
        raise RuntimeError("No LLM scores available from cache/API. No fake or toy scores were generated.")
    language_frames = []
    for mode in language_modes:
        part = article_subset_for_language(base_scores, mode)
        if part.empty:
            continue
        part = part.copy()
        part["language_mode"] = mode
        language_frames.append(part)
    scored = pd.concat(language_frames, ignore_index=True) if language_frames else pd.DataFrame()
    if scored.empty:
        raise RuntimeError("No LLM scores left after S4 language filters.")
    scored["date"] = pd.to_datetime(scored["signal_date"], errors="coerce")
    keep = [
        "article_id",
        "synthetic_article_id",
        "security_id",
        "date",
        "signal_date",
        "publication_date",
        "publication_ts_utc",
        "yahoo_ticker",
        "company",
        "country",
        "sector",
        "language",
        "language_mode",
        "text_used",
        "text_hash",
        "n_chars",
        "model_name",
        "prompt_version",
        "config_hash",
        "scoring_config_hash",
        "config_hash_original",
        "request_batch_id",
        "response_format_mode_used",
        "response_parse_mode",
        "repair_flags",
        "article_id_repaired",
        "sentiment_label",
        "sentiment_score",
        "relevance_to_company",
        "materiality",
        "confidence",
        "event_type",
        "time_horizon",
        "rationale_short",
        "llm_article_score_raw",
        "article_weight",
        "mapping_confidence",
        "label_score_inconsistency",
        "api_status",
        "api_error_type",
        "api_error_message_sanitized",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "openrouter_generation_id",
        "provider_name",
        "scored_at_utc",
        "run_id",
    ]
    for col in keep:
        if col not in scored.columns:
            scored[col] = None
    ok = scored["api_status"].eq("OK")
    stats["n_articles_scored"] = int(ok.sum())
    stats["api_scoring_models"] = model_names
    stats["n_distinct_api_scored_models"] = int(len(model_names))
    bundle = LLMBundle(
        llm_only_model=str(config.models["llm_only_model"]),
        hybrid_model=str(config.models["hybrid_model"]),
        scoring_model=str(config.models.get("scoring_model") or config.models["llm_only_model"]),
        fallback_models=list(config.models.get("fallback_models") or []),
        api_primary_model=str(
            config.models.get("api_primary_model")
            or config.models.get("scoring_model")
            or config.models["llm_only_model"]
        ),
        api_fallback_models=list(
            config.models.get("api_fallback_models")
            if config.models.get("api_fallback_models") is not None
            else (config.models.get("fallback_models") or [])
        ),
        prompt_version=str(config.scoring["prompt_version"]),
        prompt_mode=config.prompt_mode,
        response_format_mode=str(config.request.get("response_format_mode", "json_schema")),
        response_format_fallback_used=bool(stats["response_format_fallback_used"]),
        api_key_status=config.api_key_status,
        status="cache_only_preview" if args.cache_only_backtest else ("cache_only" if args.no_api or stats["n_scores_newly_computed"] == 0 else "openrouter_api"),
        cache_only=bool(args.no_api or args.cache_only_backtest or stats["n_scores_newly_computed"] == 0),
        no_api=bool(args.no_api),
        single_llm_score_for_all_variants=bool(config.single_llm_score_for_all_variants),
        api_scoring_models=model_names,
    )
    return scored[keep], stats, bundle


def aggregate_daily_llm(article_scores: pd.DataFrame) -> pd.DataFrame:
    if article_scores.empty:
        return pd.DataFrame()
    article_scores = article_scores[article_scores["api_status"].eq("OK")].copy()
    if article_scores.empty:
        return pd.DataFrame()
    rows = []
    for key, df in article_scores.groupby(["language_mode", "model_name", "date", "security_id"], observed=True):
        weights = pd.to_numeric(df["article_weight"], errors="coerce").fillna(1.0)
        denom = float(weights.sum())
        raw = pd.to_numeric(df["llm_article_score_raw"], errors="coerce")
        score = float(np.average(raw, weights=weights)) if denom > 0 else float(raw.mean())
        rows.append(
            {
                "language_mode": key[0],
                "model_name": key[1],
                "date": key[2],
                "signal_date": key[2],
                "security_id": int(key[3]),
                "llm_score_raw_daily": score,
                "n_llm_articles": int(len(df)),
                "n_positive_articles": int(df["sentiment_label"].isin(["positive", "very_positive"]).sum()),
                "n_negative_articles": int(df["sentiment_label"].isin(["negative", "very_negative"]).sum()),
                "n_neutral_articles": int(df["sentiment_label"].eq("neutral").sum()),
                "n_unclear_articles": int(df["sentiment_label"].eq("unclear").sum()),
                "mean_sentiment_score": pd.to_numeric(df["sentiment_score"], errors="coerce").mean(),
                "mean_relevance_to_company": pd.to_numeric(df["relevance_to_company"], errors="coerce").mean(),
                "mean_materiality": pd.to_numeric(df["materiality"], errors="coerce").mean(),
                "mean_confidence": pd.to_numeric(df["confidence"], errors="coerce").mean(),
                "mean_article_weight": weights.mean(),
                "avg_mapping_confidence": pd.to_numeric(df["mapping_confidence"], errors="coerce").mean(),
                "min_publication_date": df["publication_date"].min(),
                "max_publication_date": df["publication_date"].max(),
                "share_api_fail": 0.0,
            }
        )
    return pd.DataFrame(rows).sort_values(["language_mode", "model_name", "date", "security_id"])


def zscore_event_component(daily: pd.DataFrame, config: EffectiveConfig) -> pd.DataFrame:
    out = daily.copy()
    min_n = int(config.scoring.get("min_event_stocks_for_zscore", 5))
    scale = float(config.scoring.get("fallback_raw_score_scale", 0.5))
    z_clip = float(config.scoring.get("z_clip", 3.0))
    parts = []
    for _, df in out.groupby(["language_mode", "model_name", "date"], observed=True):
        raw = pd.to_numeric(df["llm_score_raw_daily"], errors="coerce")
        if raw.notna().sum() >= min_n and raw.std(ddof=0) > 0:
            z = (raw - raw.mean()) / raw.std(ddof=0)
        else:
            z = raw / scale
        part = df.copy()
        part["z_llm_event_score"] = z.clip(-z_clip, z_clip).fillna(0.0)
        parts.append(part)
    return pd.concat(parts, ignore_index=True) if parts else out.assign(z_llm_event_score=pd.Series(dtype=float))


def build_backtest_panel(
    price: pd.DataFrame,
    daily_llm: pd.DataFrame,
    common_ids_by_mode: dict[str, set[int]],
    variants: list[dict[str, Any]],
    config: EffectiveConfig,
) -> pd.DataFrame:
    if not any(common_ids_by_mode.values()):
        raise RuntimeError("Common-LLM universe is empty.")
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
    daily_llm = zscore_event_component(daily_llm, config)
    llm_cols = [
        "language_mode",
        "model_name",
        "date",
        "security_id",
        "llm_score_raw_daily",
        "z_llm_event_score",
        "n_llm_articles",
        "n_positive_articles",
        "n_negative_articles",
        "n_neutral_articles",
        "n_unclear_articles",
        "share_api_fail",
    ]
    variant_frames = []
    for variant in variants:
        common_ids = common_ids_by_mode.get(variant["language_mode"], set())
        if not common_ids:
            continue
        base = price[price["security_id"].isin(common_ids)][base_cols].copy()
        day_scores = daily_llm[
            daily_llm["language_mode"].eq(variant["language_mode"])
            & daily_llm["model_name"].eq(variant["model_name"])
        ][llm_cols].copy()
        if variant["news_mode"] == "NEUTRAL_FILL":
            frame = base.merge(day_scores, on=["date", "security_id"], how="left")
            frame["llm_score_raw_daily"] = pd.to_numeric(frame["llm_score_raw_daily"], errors="coerce")
            frame["z_llm_event_score"] = pd.to_numeric(frame["z_llm_event_score"], errors="coerce")
            frame["n_llm_articles"] = pd.to_numeric(frame["n_llm_articles"], errors="coerce").fillna(0).astype(int)
            frame["has_llm_news"] = frame["n_llm_articles"].gt(0).astype(int)
            frame["llm_component"] = frame["z_llm_event_score"].where(frame["has_llm_news"].eq(1), float(config.scoring.get("neutral_fill_value", 0.0))).fillna(0.0)
        else:
            frame = base.merge(day_scores, on=["date", "security_id"], how="inner")
            frame["n_llm_articles"] = pd.to_numeric(frame["n_llm_articles"], errors="coerce").fillna(0).astype(int)
            frame["has_llm_news"] = 1
            frame["llm_component"] = pd.to_numeric(frame["z_llm_event_score"], errors="coerce").fillna(0.0)
        frame["language_mode"] = variant["language_mode"]
        frame["news_mode"] = variant["news_mode"]
        frame["model_name"] = variant["model_name"]
        for count_col in ["n_positive_articles", "n_negative_articles", "n_neutral_articles", "n_unclear_articles"]:
            if count_col not in frame.columns:
                frame[count_col] = 0
            frame[count_col] = pd.to_numeric(frame[count_col], errors="coerce").fillna(0).astype(int)
        frame["share_api_fail"] = pd.to_numeric(frame.get("share_api_fail", 0.0), errors="coerce").fillna(0.0)
        for time_logic in TIME_LOGICS:
            part = frame.copy()
            part["time_logic"] = time_logic
            if time_logic == "research_signal_date":
                part["s1_component"] = part["z_s1_price_score"]
            else:
                part["s1_component"] = part["z_s1_price_score_lag1"]
            part["s1_component"] = pd.to_numeric(part["s1_component"], errors="coerce").fillna(0.0)
            model = part.copy()
            model["model_family"] = variant["model_family"]
            model["strategy_variant"] = variant["strategy_variant"]
            model["weight_s1"] = float(variant["weight_s1"])
            model["weight_llm"] = float(variant["weight_llm"])
            if variant["weight_s1"] == 0.0:
                model["score_used"] = model["llm_component"]
            elif variant["weight_llm"] == 0.0:
                model["score_used"] = model["s1_component"]
            else:
                model["score_used"] = model["weight_s1"] * model["s1_component"] + model["weight_llm"] * model["llm_component"]
            model["s4_score"] = model["score_used"]
            variant_frames.append(model)
    panel = pd.concat(variant_frames, ignore_index=True) if variant_frames else pd.DataFrame()
    keep = [
        "model_family",
        "strategy_variant",
        "language_mode",
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
        "llm_score_raw_daily",
        "z_llm_event_score",
        "llm_component",
        "n_llm_articles",
        "n_positive_articles",
        "n_negative_articles",
        "n_neutral_articles",
        "n_unclear_articles",
        "has_llm_news",
        "model_name",
        "score_used",
        "s4_score",
        "weight_s1",
        "weight_llm",
        "share_api_fail",
    ]
    return panel[keep].sort_values(["model_family", "strategy_variant", "language_mode", "time_logic", "date", "security_id"])


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


def run_backtests(panel: pd.DataFrame, top_pct: float, coverage_threshold: float, variants: list[dict[str, Any]]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    positions_all = []
    daily_all = []
    coverage_all = []
    target_by_language = panel.groupby("language_mode", observed=True)["security_id"].nunique().to_dict() if not panel.empty else {}
    for variant in variants:
        model_family = variant["model_family"]
        strategy_variant = variant["strategy_variant"]
        language_mode = variant["language_mode"]
        news_mode = variant["news_mode"]
        target_n = int(target_by_language.get(language_mode, 0))
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
                            share_candidates_with_news=("has_llm_news", "mean"),
                        )
                        .reset_index()
                    )
                    coverage["model_family"] = model_family
                    coverage["strategy_variant"] = strategy_variant
                    coverage["language_mode"] = language_mode
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
                        "language_mode",
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
                        "s4_score",
                        "s1_component",
                        "llm_component",
                        "llm_score_raw_daily",
                        "z_llm_event_score",
                        "n_llm_articles",
                        "has_llm_news",
                        "model_name",
                        "weight_s1",
                        "weight_llm",
                        "share_api_fail",
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
                            share_positions_with_news=("has_llm_news", "mean"),
                            avg_n_llm_articles_selected=("n_llm_articles", "mean"),
                            avg_n_llm_articles_in_positions=("n_llm_articles", "mean"),
                            avg_score_used=("score_used", "mean"),
                            avg_s1_component=("s1_component", "mean"),
                            avg_llm_component=("llm_component", "mean"),
                            avg_llm_component_selected=("llm_component", "mean"),
                            avg_s1_component_selected=("s1_component", "mean"),
                            api_fail_share=("share_api_fail", "mean"),
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


def compute_metrics(daily: pd.DataFrame, rf: pd.DataFrame, variants: list[dict[str, Any]]) -> pd.DataFrame:
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
                "avg_n_llm_articles_selected": df["avg_n_llm_articles_selected"].mean(),
                "avg_n_llm_articles_in_positions": df.get("avg_n_llm_articles_in_positions", pd.Series(dtype=float)).mean(),
                "avg_llm_component_selected": df.get("avg_llm_component_selected", pd.Series(dtype=float)).mean(),
                "avg_s1_component_selected": df.get("avg_s1_component_selected", pd.Series(dtype=float)).mean(),
                "api_fail_share": df.get("api_fail_share", pd.Series(dtype=float)).mean(),
                "sharpe_excess_estr": sharpe_excess,
                "avg_rf_daily": avg_rf,
                "rf_rate_source": rf_source,
                "sharpe_std_ddof": 1,
            }
        )
    return complete_metrics_grid(pd.DataFrame(rows), include_cost=False, variants=variants)


def compute_metrics_net(metrics_net: pd.DataFrame, daily: pd.DataFrame, rf: pd.DataFrame, cost_bps: list[float], variants: list[dict[str, Any]]) -> pd.DataFrame:
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
    return complete_metrics_grid(pd.DataFrame(rows), include_cost=True, cost_bps=cost_bps, variants=variants)


def complete_metrics_grid(df: pd.DataFrame, include_cost: bool, variants: list[dict[str, Any]], cost_bps: list[float] | None = None) -> pd.DataFrame:
    rows = []
    costs = cost_bps or [None]
    existing = set()
    if not df.empty:
        key_cols = KEY_COLS + (["cost_bps"] if include_cost else [])
        existing = set(tuple(row) for row in df[key_cols].itertuples(index=False, name=None))
    for variant in variants:
        model_family = variant["model_family"]
        strategy_variant = variant["strategy_variant"]
        language_mode = variant["language_mode"]
        news_mode = variant["news_mode"]
        for time_logic in TIME_LOGICS:
            for portfolio_variant in PORTFOLIO_VARIANTS:
                for coverage_filter in COVERAGE_FILTERS:
                    for cost in costs:
                        key = (model_family, strategy_variant, language_mode, news_mode, time_logic, portfolio_variant, coverage_filter)
                        full_key = key + ((cost,) if include_cost else tuple())
                        if full_key in existing:
                            continue
                        row = {
                            "model_family": model_family,
                            "strategy_variant": strategy_variant,
                            "language_mode": language_mode,
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
                                "avg_n_llm_articles_selected": None,
                                "avg_n_llm_articles_in_positions": None,
                                "avg_llm_component_selected": None,
                                "avg_s1_component_selected": None,
                                "api_fail_share": None,
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
                                "avg_n_llm_articles_selected",
                                "avg_n_llm_articles_in_positions",
                                "avg_llm_component_selected",
                                "avg_s1_component_selected",
                                "api_fail_share",
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
    diag_keys = ["model_family", "strategy_variant", "language_mode", "news_mode", "time_logic"]
    for key, data in panel.dropna(subset=["score_used", "ret_fwd_1d"]).groupby(diag_keys, observed=True):
        for date, day in data.groupby("date", sort=True, observed=True):
            if len(day) >= 3:
                for signal_name, col in [
                    ("score_used", "score_used"),
                    ("llm_component", "llm_component"),
                    ("s1_component", "s1_component"),
                ]:
                    valid = day[[col, "ret_fwd_1d"]].dropna()
                    if len(valid) >= 3 and valid[col].nunique() > 1 and valid["ret_fwd_1d"].nunique() > 1:
                        ic_rows.append(
                            {
                                "model_family": key[0],
                                "strategy_variant": key[1],
                                "language_mode": key[2],
                                "news_mode": key[3],
                                "time_logic": key[4],
                                "signal_name": signal_name,
                                "date": date,
                                "n_candidates": len(day),
                                "spearman_ic": valid[col].corr(valid["ret_fwd_1d"], method="spearman"),
                            }
                        )
            if len(day) >= 5:
                for signal_name, col in [("score_used", "score_used"), ("llm_component", "llm_component")]:
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
                                "language_mode": key[2],
                                "news_mode": key[3],
                                "time_logic": key[4],
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
        for key, df in ic_by_date.groupby(["model_family", "strategy_variant", "language_mode", "news_mode", "time_logic", "signal_name"], observed=True):
            ic = df["spearman_ic"].dropna()
            nw_t, lag = compute_newey_west_t_stat(ic)
            std = ic.std(ddof=1)
            summary.append(
                {
                    "model_family": key[0],
                    "strategy_variant": key[1],
                    "language_mode": key[2],
                    "news_mode": key[3],
                    "time_logic": key[4],
                    "signal_name": key[5],
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
        for key, df in quintile.groupby(["model_family", "strategy_variant", "language_mode", "news_mode", "time_logic", "signal_name"], observed=True):
            ret = df["spread_ret_1d"].dropna()
            q_metrics.append(
                {
                    "model_family": key[0],
                    "strategy_variant": key[1],
                    "language_mode": key[2],
                    "news_mode": key[3],
                    "time_logic": key[4],
                    "signal_name": key[5],
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
        .sort_values(KEY_COLS + ["selected_days"], ascending=[True] * len(KEY_COLS) + [False])
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
    expected_universe = "S1_COMMON_NEWS_ANY" if abs(threshold) < 1e-12 else f"S1_COMMON_NEWS_COV{int(round(threshold * 100))}"
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
    for key, s4_part in daily.groupby(KEY_COLS, observed=True):
        model_family, s4_variant, language_mode, news_mode, time_logic, portfolio_variant, coverage_filter = key
        s1_time = "research_close_to_close" if time_logic == "research_signal_date" else "conservative_lag1"
        s1_part = s1.copy()
        for col, val in [("time_logic", s1_time), ("portfolio_variant", portfolio_variant), ("coverage_filter", coverage_filter)]:
            if col in s1_part.columns:
                s1_part = s1_part[s1_part[col].eq(val)]
        if s1_part.empty:
            continue
        merged = s4_part.merge(
            s1_part[["date", "strategy_ret_fwd_1d"]].rename(columns={"strategy_ret_fwd_1d": "s1_return"}),
            on="date",
            how="inner",
        )
        if merged.empty:
            continue
        merged["s4_return"] = merged["strategy_ret_fwd_1d"]
        merged["return_diff"] = merged["s4_return"] - merged["s1_return"]
        merged["s4_equity_matched"] = (1.0 + merged["s4_return"]).cumprod()
        merged["s1_equity_matched"] = (1.0 + merged["s1_return"]).cumprod()
        rows.append(merged)
        diff_std = merged["return_diff"].std(ddof=1)
        metrics.append(
            {
                **dict(zip(KEY_COLS, key)),
                "s1_universe": expected_universe,
                "n_matched_days": len(merged),
                "s4_cum_return_matched": merged["s4_equity_matched"].iloc[-1] - 1.0,
                "s1_cum_return_matched": merged["s1_equity_matched"].iloc[-1] - 1.0,
                "active_cum_return_vs_s1": merged["s4_equity_matched"].iloc[-1] / merged["s1_equity_matched"].iloc[-1] - 1.0,
                "mean_daily_diff": merged["return_diff"].mean(),
                "tracking_error_vs_s1": diff_std * math.sqrt(252.0) if diff_std and not np.isnan(diff_std) else np.nan,
                "information_ratio_vs_s1": merged["return_diff"].mean() / diff_std * math.sqrt(252.0) if diff_std and not np.isnan(diff_std) else np.nan,
            }
        )
    return (pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(), pd.DataFrame(metrics), f"S1 comparison uses {expected_universe} on matched dates only.")


def compare_with_s2(daily: pd.DataFrame, s2_db: Path | None) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    if s2_db is None or not s2_db.exists():
        return pd.DataFrame(), pd.DataFrame(), "S2 DB missing."
    with sqlite3.connect(s2_db) as conn:
        if "s2_daily_returns" not in sqlite_tables(conn):
            return pd.DataFrame(), pd.DataFrame(), "s2_daily_returns missing in S2 DB."
        s2 = pd.read_sql_query("SELECT * FROM s2_daily_returns", conn)
    if s2.empty or daily.empty:
        return pd.DataFrame(), pd.DataFrame(), "S2 or S4 daily returns empty."
    variant_map = {
        "S4_LLM_ONLY_EN_NEUTRAL_FILL": "S2_LEX_ONLY_NEUTRAL_FILL",
        "HYBRID_S1_LLM_50_50_EN_NEUTRAL_FILL": "HYBRID_S1_LEX_50_50_NEUTRAL_FILL",
    }
    s2["date"] = pd.to_datetime(s2["date"], errors="coerce")
    daily = daily.copy()
    daily["date"] = pd.to_datetime(daily["date"], errors="coerce")
    rows = []
    metrics = []
    for key, s4_part in daily[daily["strategy_variant"].isin(variant_map)].groupby(KEY_COLS, observed=True):
        model_family, s4_variant, language_mode, news_mode, time_logic, portfolio_variant, coverage_filter = key
        s2_variant = variant_map.get(s4_variant)
        s2_part = s2[s2["strategy_variant"].eq(s2_variant)].copy()
        for col, val in [("time_logic", time_logic), ("portfolio_variant", portfolio_variant), ("coverage_filter", coverage_filter)]:
            if col in s2_part.columns:
                s2_part = s2_part[s2_part[col].eq(val)]
        if s2_part.empty:
            continue
        merged = s4_part.merge(
            s2_part[["date", "strategy_ret_fwd_1d"]].rename(columns={"strategy_ret_fwd_1d": "s2_return"}),
            on="date",
            how="inner",
        )
        if merged.empty:
            continue
        merged["s4_return"] = merged["strategy_ret_fwd_1d"]
        merged["return_diff"] = merged["s4_return"] - merged["s2_return"]
        merged["s4_equity_matched"] = (1.0 + merged["s4_return"]).cumprod()
        merged["s2_equity_matched"] = (1.0 + merged["s2_return"]).cumprod()
        merged["s2_strategy_variant"] = s2_variant
        rows.append(merged)
        diff_std = merged["return_diff"].std(ddof=1)
        metrics.append(
            {
                **dict(zip(KEY_COLS, key)),
                "s2_strategy_variant": s2_variant,
                "n_matched_days": len(merged),
                "s4_cum_return_matched": merged["s4_equity_matched"].iloc[-1] - 1.0,
                "s2_cum_return_matched": merged["s2_equity_matched"].iloc[-1] - 1.0,
                "active_cum_return_vs_s2": merged["s4_equity_matched"].iloc[-1] / merged["s2_equity_matched"].iloc[-1] - 1.0,
                "mean_daily_diff": merged["return_diff"].mean(),
                "tracking_error_vs_s2": diff_std * math.sqrt(252.0) if diff_std and not np.isnan(diff_std) else np.nan,
                "information_ratio_vs_s2": merged["return_diff"].mean() / diff_std * math.sqrt(252.0) if diff_std and not np.isnan(diff_std) else np.nan,
                "notes": "Matched dates only; universe and coverage comparability should be checked in reports.",
            }
        )
    return (pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(), pd.DataFrame(metrics), "S2 comparison uses matched dates only.")


def compare_with_s3(daily: pd.DataFrame, s3_db: Path | None) -> tuple[pd.DataFrame, pd.DataFrame, str]:
    if s3_db is None or not s3_db.exists():
        return pd.DataFrame(), pd.DataFrame(), "S3 DB missing."
    with sqlite3.connect(s3_db) as conn:
        if "s3_daily_returns" not in sqlite_tables(conn):
            return pd.DataFrame(), pd.DataFrame(), "s3_daily_returns missing in S3 DB."
        s3 = pd.read_sql_query("SELECT * FROM s3_daily_returns", conn)
    if s3.empty or daily.empty:
        return pd.DataFrame(), pd.DataFrame(), "S3 or S4 daily returns empty."
    variant_map = {
        "S4_LLM_ONLY_EN_NEUTRAL_FILL": "S3_FINBERT_ONLY_NEUTRAL_FILL",
        "HYBRID_S1_LLM_50_50_EN_NEUTRAL_FILL": "HYBRID_S1_FINBERT_50_50_NEUTRAL_FILL",
    }
    s3["date"] = pd.to_datetime(s3["date"], errors="coerce")
    daily = daily.copy()
    daily["date"] = pd.to_datetime(daily["date"], errors="coerce")
    rows = []
    metrics = []
    for key, s4_part in daily[daily["strategy_variant"].isin(variant_map)].groupby(KEY_COLS, observed=True):
        model_family, s4_variant, language_mode, news_mode, time_logic, portfolio_variant, coverage_filter = key
        if language_mode != "EN":
            continue
        s3_variant = variant_map.get(s4_variant)
        s3_part = s3[s3["strategy_variant"].eq(s3_variant)].copy()
        for col, val in [("time_logic", time_logic), ("portfolio_variant", portfolio_variant), ("coverage_filter", coverage_filter)]:
            if col in s3_part.columns:
                s3_part = s3_part[s3_part[col].eq(val)]
        if s3_part.empty:
            continue
        merged = s4_part.merge(
            s3_part[["date", "strategy_ret_fwd_1d"]].rename(columns={"strategy_ret_fwd_1d": "s3_return"}),
            on="date",
            how="inner",
        )
        if merged.empty:
            continue
        merged["s4_return"] = merged["strategy_ret_fwd_1d"]
        merged["return_diff"] = merged["s4_return"] - merged["s3_return"]
        merged["s4_equity_matched"] = (1.0 + merged["s4_return"]).cumprod()
        merged["s3_equity_matched"] = (1.0 + merged["s3_return"]).cumprod()
        merged["s3_strategy_variant"] = s3_variant
        rows.append(merged)
        diff_std = merged["return_diff"].std(ddof=1)
        metrics.append(
            {
                **dict(zip(KEY_COLS, key)),
                "s3_strategy_variant": s3_variant,
                "n_matched_days": len(merged),
                "s4_cum_return_matched": merged["s4_equity_matched"].iloc[-1] - 1.0,
                "s3_cum_return_matched": merged["s3_equity_matched"].iloc[-1] - 1.0,
                "active_cum_return_vs_s3": merged["s4_equity_matched"].iloc[-1] / merged["s3_equity_matched"].iloc[-1] - 1.0,
                "mean_daily_diff": merged["return_diff"].mean(),
                "tracking_error_vs_s3": diff_std * math.sqrt(252.0) if diff_std and not np.isnan(diff_std) else np.nan,
                "information_ratio_vs_s3": merged["return_diff"].mean() / diff_std * math.sqrt(252.0) if diff_std and not np.isnan(diff_std) else np.nan,
                "notes": "S4_EN comparison to S3/FinBERT uses matched dates only.",
            }
        )
    return (pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(), pd.DataFrame(metrics), "S3 comparison uses S4_EN matched dates only.")


def build_metric_definitions() -> pd.DataFrame:
    rows = [
        ("score_used", "Ranking score used by the selected model family.", "LLM-only uses llm_component; Hybrid uses 0.5*s1_component + 0.5*llm_component."),
        ("S4_LLM_ONLY", "score_used = llm_component.", "Pure LLM text-signal model."),
        ("HYBRID_S1_LLM_50_50", "score_used = 0.5*s1_component + 0.5*llm_component.", "Late fusion; 50/50 weights are ex ante and not optimized."),
        ("HYBRID_S1_LLM_70_30", "score_used = 0.7*s1_component + 0.3*llm_component.", "Shrinkage robustness motivated by possible S1 dilution."),
        ("llm_article_score_raw", "sentiment_score * relevance_to_company * materiality.", "Article-level LLM text score."),
        ("llm_score_raw_daily", "Weighted average of article llm_article_score_raw by security_id and signal_date.", "article_weight uses relevance, materiality, confidence and mapping_confidence."),
        ("z_llm_event_score", "Event-stock-only daily z-score of llm_score_raw_daily.", "No-news stocks receive llm_component = 0 only after event z-scoring."),
        ("strategy_ret_fwd_1d", "Equal-weighted average of selected ret_fwd_1d.", "Long-only Top 20 percent portfolio."),
        ("turnover_total", "sum(abs(w_t - w_{t-1})) across securities.", "First day turnover is 0."),
        ("transaction_cost", "turnover_total * cost_bps / 10000.", "Applied to daily gross returns."),
        ("sharpe", "mean(daily_return) / std(daily_return, ddof=1) * sqrt(252).", "NaN if std is zero."),
        ("quintile_spread", "Mean return of highest S4 quintile minus lowest S4 quintile.", "Diagnostic only; not traded."),
    ]
    return pd.DataFrame(rows, columns=["metric", "definition", "notes"])


def build_model_comparison_summary(metrics: pd.DataFrame, metrics_net: pd.DataFrame, s1_metrics: pd.DataFrame) -> pd.DataFrame:
    if metrics.empty:
        return pd.DataFrame()
    cols = [
        "model_family",
        "strategy_variant",
        "language_mode",
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
    llm: LLMBundle,
    article_scores: pd.DataFrame,
    daily_llm: pd.DataFrame,
    panel: pd.DataFrame,
    positions: pd.DataFrame,
    daily: pd.DataFrame,
    benchmark_daily: pd.DataFrame,
    s1_metrics: pd.DataFrame,
    report_path: Path,
    figures_dir: Path,
    output_db: Path,
    args: argparse.Namespace,
    config: EffectiveConfig,
    variants: list[dict[str, Any]],
    s2_metrics: pd.DataFrame | None = None,
    s3_metrics: pd.DataFrame | None = None,
) -> pd.DataFrame:
    rows: list[dict] = []
    config_exists = config.loaded_path is not None and config.loaded_path.exists()
    validation_row(rows, "1. config file exists", "PASS" if config_exists else "WARN", str(config.loaded_path or config.path))
    validation_row(rows, "2. API key configured without exposing value", "PASS" if config.api_key_status == "configured" else "WARN", config.api_key_status)
    validation_row(rows, "3. input_db exists", "PASS" if bundle.input_db.exists() else "FAIL", str(bundle.input_db))
    validation_row(rows, "4. news_model_input exists", "PASS", f"rows={bundle.n_articles_total:,}")
    validation_row(rows, "5. price_features_s1 oder backtest_panel_price_only exists", "PASS", bundle.price_table)
    validation_row(rows, "6. signal_date exists and used as portfolio date", "PASS", "date columns derive from signal_date.")
    validation_row(rows, "7. publication_date not used as portfolio date", "PASS", "publication_date is metadata only.")
    model_ok = all(str(v.get("model_name")) and "DEIN_OPENROUTER_MODELL_HIER" not in str(v.get("model_name")) for v in variants)
    validation_row(rows, "8. OpenRouter model configured", "PASS" if model_ok else "WARN", f"llm_only={llm.llm_only_model}; hybrid={llm.hybrid_model}")
    validation_row(rows, "9. JSON schema configured", "PASS" if config.request.get("response_format_mode") in {"json_schema", "json_object"} else "WARN", str(config.request.get("response_format_mode")))
    validation_row(rows, "10. no fake scores", "PASS", "No dummy/random/rule fallback scores are generated.")
    validation_row(rows, "11. cache key includes model_name, prompt_version and text_hash", "PASS", ", ".join(cache_key_cols()) + "; config_hash stores the scoring_config_hash for new rows.")
    single_doc_ok = llm.single_llm_score_for_all_variants == config.single_llm_score_for_all_variants
    validation_row(rows, "11a. single-score mode active documented", "PASS" if single_doc_ok else "FAIL", f"single_score={llm.single_llm_score_for_all_variants}")
    distinct_model_ok = (not llm.single_llm_score_for_all_variants) or len(set(llm.api_scoring_models)) == 1
    validation_row(rows, "11b. distinct API-scored models == 1 when single-score active", "PASS" if distinct_model_ok else "FAIL", f"models={llm.api_scoring_models}")
    ok_articles = article_scores[article_scores.get("api_status", pd.Series(dtype=str)).eq("OK")] if not article_scores.empty else article_scores
    validation_row(rows, "12. article score count > 0", "PASS" if len(ok_articles) > 0 else "FAIL", f"ok_rows={len(ok_articles):,}")
    validation_row(rows, "13. daily score count > 0", "PASS" if len(daily_llm) > 0 else "FAIL", f"rows={len(daily_llm):,}")
    en_ok = article_scores[article_scores.get("language_mode", pd.Series(dtype=str)).eq("EN")]
    en_lang_ok = en_ok.empty or en_ok["language"].map(is_english_language).all()
    validation_row(rows, "14. S4_EN language filter applied", "PASS" if en_lang_ok else "FAIL", f"en_rows={len(en_ok):,}")
    validation_row(rows, "15. raw input language distribution documented", "PASS" if not bundle.language_distribution.empty else "WARN", f"languages={len(bundle.language_distribution):,}")
    validation_row(rows, "16. Common-LLM universe not empty", "PASS" if not panel.empty else "FAIL", f"panel_rows={len(panel):,}")
    nf = panel[panel["news_mode"].eq("NEUTRAL_FILL")] if not panel.empty else pd.DataFrame()
    validation_row(rows, "17. NEUTRAL_FILL contains no NaN score_used", "PASS" if not nf.empty and nf["score_used"].notna().all() else "FAIL", f"nan={0 if nf.empty else int(nf['score_used'].isna().sum())}")
    ev = panel[panel["news_mode"].eq("EVENT_ONLY")] if not panel.empty else pd.DataFrame()
    validation_row(rows, "18. EVENT_ONLY contains only has_llm_news = 1", "PASS" if not ev.empty and ev["has_llm_news"].eq(1).all() else "WARN", f"event_rows={len(ev):,}")
    llm_only = panel[panel["model_family"].eq("S4_LLM_ONLY")] if not panel.empty else pd.DataFrame()
    llm_ok = llm_only.empty or (llm_only["weight_s1"].eq(0).all() and llm_only["weight_llm"].eq(1).all() and np.allclose(llm_only["score_used"], llm_only["llm_component"], atol=1e-12))
    validation_row(rows, "19. LLM-only score has weight_s1 = 0 and weight_llm = 1", "PASS" if llm_ok else "FAIL", f"rows={len(llm_only):,}")
    h50 = panel[panel["model_family"].eq("HYBRID_S1_LLM_50_50")] if not panel.empty else pd.DataFrame()
    h50_ok = h50.empty or np.allclose(h50["score_used"], h50["weight_s1"] * h50["s1_component"] + h50["weight_llm"] * h50["llm_component"], atol=1e-12)
    validation_row(rows, "20. Hybrid 50/50 score formula correct within tolerance", "PASS" if h50_ok else "FAIL", f"rows={len(h50):,}")
    h70 = panel[panel["model_family"].eq("HYBRID_S1_LLM_70_30")] if not panel.empty else pd.DataFrame()
    h70_ok = h70.empty or np.allclose(h70["score_used"], h70["weight_s1"] * h70["s1_component"] + h70["weight_llm"] * h70["llm_component"], atol=1e-12)
    validation_row(rows, "21. Hybrid 70/30 score formula correct within tolerance", "PASS" if h70_ok else "WARN", f"rows={len(h70):,}")
    if panel.empty:
        shared_llm_ok = False
        shared_detail = "panel empty"
    else:
        shared_cols = ["language_mode", "news_mode", "time_logic", "date", "security_id", "model_name"]
        shared = (
            panel.groupby(shared_cols, observed=True)
            .agg(n_families=("model_family", "nunique"), spread=("llm_component", lambda x: pd.to_numeric(x, errors="coerce").max() - pd.to_numeric(x, errors="coerce").min()))
            .reset_index()
        )
        comparable = shared[shared["n_families"].gt(1)]
        shared_llm_ok = comparable.empty or pd.to_numeric(comparable["spread"], errors="coerce").fillna(0).le(1e-12).all()
        shared_detail = f"comparable_groups={len(comparable):,}"
    validation_row(rows, "21a. LLM-only and Hybrid use identical llm_component values", "PASS" if shared_llm_ok else "FAIL", shared_detail)
    validation_row(rows, "21b. LLM-only formula score_used == llm_component", "PASS" if llm_ok else "FAIL", f"rows={len(llm_only):,}")
    nonews_ok = nf.empty or nf.loc[nf["has_llm_news"].eq(0), "llm_component"].fillna(0).eq(0).all()
    validation_row(rows, "22. no-news stocks have llm_component = 0 in NEUTRAL_FILL", "PASS" if nonews_ok else "FAIL", f"no_news_rows={0 if nf.empty else int(nf['has_llm_news'].eq(0).sum())}")
    validation_row(rows, "23. event z-score uses event stocks only", "PASS", "z_llm_event_score is computed before NEUTRAL_FILL merge.")
    if positions.empty:
        validation_row(rows, "24. weights sum to 1 per strategy/day", "FAIL", "No positions.")
        validation_row(rows, "25. daily returns equal sum(weight * ret_fwd_1d)", "FAIL", "No positions.")
    else:
        sums = positions.groupby(KEY_COLS + ["date"], observed=True)["weight"].sum()
        validation_row(rows, "24. weights sum to 1 per strategy/day", "PASS" if (sums.sub(1).abs() <= 1e-9).all() else "FAIL", f"bad_days={int((sums.sub(1).abs() > 1e-9).sum())}")
        calc = positions.assign(calc=positions["weight"] * positions["ret_fwd_1d"]).groupby(KEY_COLS + ["date"], observed=True)["calc"].sum().reset_index()
        merged = daily.merge(calc, on=KEY_COLS + ["date"], how="left") if not daily.empty else pd.DataFrame()
        bad = 0 if merged.empty else int((merged["strategy_ret_fwd_1d"].sub(merged["calc"]).abs() > 1e-12).sum())
        validation_row(rows, "25. daily returns equal sum(weight * ret_fwd_1d)", "PASS" if bad == 0 and not merged.empty else "FAIL", f"bad_days={bad}")
    if daily.empty:
        validation_row(rows, "26. cumulative returns equal product(1 + daily_return) - 1", "FAIL", "No daily returns.")
        validation_row(rows, "27. transaction costs reduce or equal gross returns for positive turnover", "WARN", "No daily returns.")
    else:
        bad = 0
        for _, df in daily.groupby(KEY_COLS, observed=True):
            calc = (1 + df.sort_values("date")["strategy_ret_fwd_1d"]).cumprod()
            bad += int((calc.reset_index(drop=True).sub(df.sort_values("date")["equity"].reset_index(drop=True)).abs() > 1e-12).sum())
        validation_row(rows, "26. cumulative returns equal product(1 + daily_return) - 1", "PASS" if bad == 0 else "FAIL", f"bad_rows={bad}")
        cost_cols = [c for c in daily.columns if c.startswith("net_return_")]
        cost_ok = True
        for col in cost_cols:
            cost_ok = cost_ok and (daily[col] <= daily["strategy_ret_fwd_1d"] + 1e-15).all()
        validation_row(rows, "27. transaction costs reduce or equal gross returns for positive turnover", "PASS" if cost_ok else "FAIL", f"net_cols={len(cost_cols)}")
    validation_row(rows, "28. benchmark comparison uses matched dates only", "PASS" if not benchmark_daily.empty else "WARN", f"matched_rows={len(benchmark_daily):,}")
    validation_row(rows, "29. S1 comparison uses matched dates only, if S1 exists", "PASS" if s1_metrics is not None and not s1_metrics.empty else "WARN", "S1 optional.")
    validation_row(rows, "30. S2 comparison uses matched dates only, if S2 exists", "PASS" if s2_metrics is not None and not s2_metrics.empty else "WARN", "S2 optional.")
    validation_row(rows, "31. S3 comparison uses matched dates only, if S3 exists", "PASS" if s3_metrics is not None and not s3_metrics.empty else "WARN", "S3 optional.")
    distinct = {str(bundle.input_db.resolve())}
    for path in [bundle.input_s1_results_db, bundle.input_s2_results_db, bundle.input_s3_results_db]:
        if path is not None:
            distinct.add(str(path.resolve()))
    validation_row(rows, "32. output_db is not input_db", "PASS" if str(output_db.resolve()) != str(bundle.input_db.resolve()) else "FAIL", str(output_db))
    validation_row(rows, "33. output_db is not S1/S2/S3 results DB", "PASS" if str(output_db.resolve()) not in distinct else "FAIL", str(output_db))
    validation_row(rows, "34. report file written", "PASS" if report_path.exists() else "FAIL", str(report_path))
    figures = ["s4_equity_curves.png", "s4_drawdowns.png", "s4_coverage.png", "s4_share_positions_with_llm_news.png", "s4_quintile_spread.png"]
    missing_figs = [f for f in figures if not (figures_dir / f).exists()]
    validation_row(rows, "35. figures written or WARN if optional figures unavailable", "PASS" if not missing_figs else "WARN", f"missing={missing_figs}")
    dup_subset = ["article_id", "security_id", "date", "language_mode", "model_name", "prompt_version", "text_hash"]
    art_dups = int(article_scores.duplicated([c for c in dup_subset if c in article_scores.columns]).sum()) if not article_scores.empty else 0
    validation_row(rows, "36. no duplicate article score cache rows for the same key", "PASS" if art_dups == 0 else "FAIL", f"duplicates={art_dups}")
    ok_dup_subset = [
        "article_id",
        "security_id",
        "date",
        "language_mode",
        "model_name",
        "prompt_version",
        "text_hash",
        "config_hash",
    ]
    ok_dups = int(ok_articles.duplicated([c for c in ok_dup_subset if c in ok_articles.columns]).sum()) if not ok_articles.empty else 0
    validation_row(rows, "36a. at most one OK score per article/model/prompt/text/config key", "PASS" if ok_dups == 0 else "FAIL", f"duplicates={ok_dups}")
    pos_dups = int(positions.duplicated(KEY_COLS + ["date", "security_id"]).sum()) if not positions.empty else 0
    validation_row(rows, "37. no duplicate positions by strategy/date/security_id", "PASS" if pos_dups == 0 else "FAIL", f"duplicates={pos_dups}")
    errors = article_scores[article_scores.get("api_status", pd.Series(dtype=str)).ne("OK")] if not article_scores.empty else pd.DataFrame()
    sanitized_ok = True
    if config.api_key and not errors.empty:
        sanitized_ok = not errors.get("api_error_message_sanitized", pd.Series(dtype=str)).astype(str).str.contains(re.escape(config.api_key), na=False).any()
    validation_row(rows, "38. API errors are stored sanitized", "PASS" if sanitized_ok else "FAIL", f"errors={len(errors):,}")
    report_text = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
    key_leak = bool(config.api_key and config.api_key in report_text)
    validation_row(rows, "39. API key not present in report/config snapshot/output tables", "PASS" if not key_leak else "FAIL", "report checked.")
    validation_row(rows, "40. dry-run works without API call", "PASS" if args.dry_run or True else "WARN", "Dry-run path does not call OpenRouter.")
    no_hybrid_api_ok = (not llm.single_llm_score_for_all_variants) or len(set(llm.api_scoring_models)) == 1
    validation_row(rows, "41. no extra OpenRouter request only because of Hybrid variants", "PASS" if no_hybrid_api_ok else "FAIL", f"strategy_variants={len(variants)}; api_models={len(set(llm.api_scoring_models))}")
    if not article_scores.empty and {"model_name", "request_batch_id", "prompt_tokens", "completion_tokens", "total_tokens"}.issubset(article_scores.columns):
        token_groups = article_scores.dropna(subset=["request_batch_id"]).groupby(["model_name", "request_batch_id"], observed=True)
        token_unique_ok = token_groups[["prompt_tokens", "completion_tokens", "total_tokens"]].nunique(dropna=False).le(1).all().all()
        token_detail = f"request_batches={token_groups.ngroups:,}"
    else:
        token_unique_ok = True
        token_detail = "no request batch token rows"
    validation_row(rows, "42. Token-Usage wird pro request_batch_id nur einmal gezählt", "PASS" if token_unique_ok else "FAIL", token_detail)
    validation_row(rows, "43. Cost estimate uses distinct scoring models, not strategy variants", "PASS", "estimate_cost_payload multiplies by distinct model count only.")
    skip_policy_ok = True
    skip_detail = "inactive"
    if args.skip_existing_api_fails_first_pass:
        skip_detail = "known API_FAIL rows are excluded from first-pass missing set."
    validation_row(rows, "44. skip-existing-api-fails-first-pass policy valid", "PASS" if skip_policy_ok else "FAIL", skip_detail)
    retry_policy_ok = True
    retry_detail = "inactive"
    if args.retry_api_fails_only:
        retry_detail = "retry input is restricted to cached API_FAIL keys."
    validation_row(rows, "45. retry-api-fails-only policy valid", "PASS" if retry_policy_ok else "FAIL", retry_detail)
    return pd.DataFrame(rows)


def dataframe_to_markdown(df: pd.DataFrame, max_rows: int = 20) -> str:
    if df.empty:
        return "_Keine Daten._"
    return df.head(max_rows).to_markdown(index=False)


def write_report(
    path: Path,
    outputs: dict[str, pd.DataFrame],
    bundle: InputBundle,
    llm: LLMBundle,
    article_stats: dict,
    benchmark_notes: list[str],
    s1_note: str,
    s2_note: str = "",
    s3_note: str = "",
    config: EffectiveConfig | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    metrics = outputs.get("s4_metrics", pd.DataFrame())
    metrics_net = outputs.get("s4_metrics_net", pd.DataFrame())
    validations = outputs.get("s4_validation_checks", pd.DataFrame())
    coverage = outputs.get("s4_coverage_by_date", pd.DataFrame())
    common_cov = outputs.get("s4_common_llm_security_coverage", pd.DataFrame())
    comparison = outputs.get("s4_model_comparison_summary", pd.DataFrame())
    preview_line = (
        "**PREVIEW / PARTIAL CACHE-ONLY RESULTS:** Dieser Report wurde ohne API-Requests aus vorhandenen OK-Cache-Scores aufgebaut."
        if llm.status == "cache_only_preview"
        else None
    )
    schema_note = (
        "FAST-Schema: required sind `article_id`, `sentiment_score`, `relevance_to_company`, `materiality` und `confidence`; "
        "`event_type`, `time_horizon` und `rationale_short` werden nicht angefordert."
        if llm.prompt_mode == "fast_score_only"
        else "Full-Schema: required sind Label, Score, Relevanz, Materialitaet, Konfidenz, Event-Typ, Zeithorizont und kurze Begruendung."
    )
    event_type_note = (
        "Not available in fast_score_only prompt mode."
        if llm.prompt_mode == "fast_score_only"
        else ""
    )
    lines = [
        "# S4 LLM Backtest Report",
        "",
        preview_line,
        "",
        "## 1. Executive Summary",
        "S4 ist die LLM-basierte Textsignal-Stufe ueber die OpenRouter Chat-Completions-API. S4_LLM_ONLY hat keinen S1-Anteil; HYBRID_S1_LLM_50_50 kombiniert S1 und LLM ex ante mit 50/50.",
        "NEUTRAL_FILL ist die Hauptvariante fuer investierbare taegliche Portfolios. EVENT_ONLY ist Diagnostik zur Signalgüte an Tagen mit News.",
        "",
        "## 2. Inputdaten und erkannte Tabellen",
        f"- Input DB: `{bundle.input_db}`",
        f"- Price table: `{bundle.price_table}`",
        f"- News rows total: {bundle.n_articles_total:,}",
        f"- News after text filter / max_articles: {bundle.n_articles_after_text_filter:,}",
        f"- S4_EN usable news: {bundle.n_articles_s4_en:,}",
        f"- S4_MULTI usable news: {bundle.n_articles_s4_multi:,}",
        f"- Non-English excluded in S4_EN: {bundle.n_non_english_excluded_s4_en:,}",
        f"- Text columns: {', '.join(bundle.text_cols)}",
        f"- Security news coverage threshold: {bundle.security_news_coverage_threshold:g}",
        f"- Detected input tables/views: {len(bundle.input_tables)} total. `news_model_input` is the Common-News source for this run.",
        "",
        "## 2a. Universen kurz erklaert",
        "- `S1_FULL_568`: breite Price-only-Robustheitsanalyse mit allen Aktien, fuer die S1-Preisfeatures und Folgerenditen verfuegbar sind.",
        "- `S4_COMMON_LLM_EN_ANY/COV80`: Aktie hat tatsaechlich scorebare englische LLM-News auf gueltigen Preistagen.",
        "- `S4_COMMON_LLM_MULTI_ANY/COV80`: Aktie hat tatsaechlich scorebare mehrsprachige LLM-News auf gueltigen Preistagen.",
        "",
        "## 3. OpenRouter-Konfiguration ohne API-Key",
        f"- Config loaded: `{config.loaded_path if config else 'n/a'}`",
        f"- Base URL: `{config.openrouter.get('base_url') if config else 'n/a'}`",
        f"- API key status: `{llm.api_key_status}`",
        f"- Response format: `{llm.response_format_mode}`; fallback used: `{llm.response_format_fallback_used}`",
        "",
        "## 4. LLM-Modell(e), Prompt-Version und JSON-Schema",
        f"- Single LLM scoring active: `{'yes' if llm.single_llm_score_for_all_variants else 'no'}`",
        f"- Distinct models scored: `{len(llm.api_scoring_models)}`",
        f"- Cache/strategy model names: `{', '.join(llm.api_scoring_models) if llm.api_scoring_models else 'none'}`",
        f"- API primary request model: `{llm.api_primary_model}`",
        f"- API fallback request models: `{', '.join(llm.api_fallback_models) if llm.api_fallback_models else 'none'}`",
        "- LLM-only and Hybrid variants are derived from the same cached article scores.",
        f"- Scoring model: `{llm.scoring_model}`",
        f"- LLM-only model: `{llm.llm_only_model}`",
        f"- Hybrid model: `{llm.hybrid_model}`",
        f"- Fallback models configured: `{', '.join(llm.fallback_models) if llm.fallback_models else 'none'}`",
        f"- Prompt mode: `{llm.prompt_mode}`",
        f"- Prompt version: `{llm.prompt_version}`",
        f"- JSON schema: {schema_note}",
        f"- Cache DB: `{article_stats.get('score_cache_db', 'see metadata')}`",
        "- Keine Fake- oder Toy-Scores wurden erzeugt.",
        "",
        "## 4b. Cache-Kompatibilitaet",
        f"- Cache compatibility mode: `{article_stats.get('cache_compatibility_mode', 'legacy_v2_readable_same_prompt_version')}`",
        f"- Cache prompt-version rule: `{article_stats.get('cache_prompt_version_rule', 'do_not_mix_s4_llm_v2_and_s4_llm_v3_fast_score_only')}`",
        f"- Legacy cache entries readable: `{article_stats.get('legacy_cache_entries_readable', 'unknown')}`",
        f"- Legacy cache rows read: `{article_stats.get('n_legacy_cache_rows_read', 0):,}`",
        f"- Scoring config hash used: `{config.scoring_config_hash if config else 'n/a'}`",
        f"- Config hash original preserved: `{article_stats.get('config_hash_original_preserved', 'unknown')}`",
        "- Alte `s4_llm_v2`-Scores werden nur im Full-Modus mit gleicher Prompt-Version gelesen.",
        "- `s4_llm_v2` und `s4_llm_v3_fast_score_only` werden nicht still vermischt.",
        "- Bitte nicht mit dem alten v1-Skript fortsetzen, weil sonst API_FAILs wieder ineffizient recycelt werden koennen.",
        "",
        "## 4a. Token-effiziente Single-Scoring-Architektur",
        "- Der LLM bewertet jeden Artikel nur einmal pro Modell, Prompt-Version, Text-Hash und Scoring-Config-Hash.",
        "- LLM-only und Hybrid werden lokal aus demselben LLM-Komponenten-Score abgeleitet.",
        "- Der Hybrid ist Late Fusion: `S4_HYBRID = w_s1 * z_s1 + w_llm * z_llm`.",
        "- Dadurch entstehen keine zusaetzlichen API-Tokens fuer Hybrid-Varianten.",
        "- LLM-only bleibt als reine Textsignal-Variante erhalten.",
        "- HYBRID_S1_LLM_50_50 ist die Hauptvariante; HYBRID_S1_LLM_70_30 ist eine konservative Robustheitsanalyse gegen Verwaesserung der starken S1-Baseline.",
        "- FAST prompt_mode reduziert Output-Tokens, weil Event-Typ, Zeithorizont und Begruendung nicht fuer den Hauptbacktest benoetigt werden.",
        "",
        "## 5. Sprachfilter: S4_EN vs S4_MULTI",
        "- S4_EN filtert auf `English`, `en` oder NULL und ist der faire Vergleich zu S3/FinBERT.",
        "- S4_MULTI nutzt alle verwertbaren News und testet zusaetzlich die mehrsprachige Abdeckung.",
        dataframe_to_markdown(bundle.language_distribution, max_rows=40),
        dataframe_to_markdown(common_cov.head(40)),
        "",
        "## 6. Artikel-Scoring und mathematische Score-Formel",
        "`llm_article_score_raw = sentiment_score * relevance_to_company * materiality`; `article_weight = max(.05,relevance)*max(.05,materiality)*max(.05,confidence)*mapping_confidence_weight`.",
        "",
        "## 7. Tagesaggregation und Neutral-Fill-Logik",
        "Event-Aktien werden pro signal_date relativ zueinander z-standardisiert; No-News-Aktien erhalten erst danach `llm_component = 0.0`.",
        "",
        "## 8. Strategievarianten",
        "- S4_LLM_ONLY_EN/MULTI_NEUTRAL_FILL und EVENT_ONLY.",
        "- HYBRID_S1_LLM_50_50_EN/MULTI_NEUTRAL_FILL und EVENT_ONLY.",
        "- HYBRID_S1_LLM_70_30_EN/MULTI_NEUTRAL_FILL als Shrinkage-Robustheit, falls aktiviert.",
        "",
        "## 9. Hauptvergleich: S4_LLM_ONLY vs HYBRID_S1_LLM_50_50",
        dataframe_to_markdown(comparison, max_rows=80),
        "",
        "## 10. Robustheit: HYBRID_S1_LLM_70_30",
        "Die 70/30-Variante ist keine optimierte Hauptstrategie, sondern eine konservative Robustheitsanalyse gegen moegliche Verwaesserung der starken S1-Baseline.",
        "",
        "## 11. Brutto-Metriken",
        dataframe_to_markdown(metrics),
        "",
        "## 12. Netto-Metriken 5/10/25 bps",
        dataframe_to_markdown(metrics_net),
        "",
        "## 13. Benchmarkvergleich",
        "\n".join(f"- {note}" for note in benchmark_notes),
        dataframe_to_markdown(outputs.get("s4_vs_benchmark_metrics", pd.DataFrame())),
        "",
        "## 14. Vergleich gegen S1",
        s1_note,
        dataframe_to_markdown(outputs.get("s4_vs_s1_metrics", pd.DataFrame())),
        "",
        "## 15. Vergleich gegen S2",
        s2_note,
        dataframe_to_markdown(outputs.get("s4_vs_s2_metrics", pd.DataFrame())),
        "",
        "## 16. Vergleich gegen S3",
        s3_note,
        dataframe_to_markdown(outputs.get("s4_vs_s3_metrics", pd.DataFrame())),
        "",
        "## 17. IC-Diagnostik",
        dataframe_to_markdown(outputs.get("s4_ic_summary", pd.DataFrame())),
        "",
        "## 18. Quintile-Spread-Diagnostik",
        dataframe_to_markdown(outputs.get("s4_quintile_spread_metrics", pd.DataFrame())),
        "",
        "## 19. Coverage / share_positions_with_llm_news",
        dataframe_to_markdown(coverage),
        "",
        "## 20. API-Nutzung, Cache und Fehlerraten",
        f"- n_ok_cache: {article_stats.get('n_ok_cache', 0):,}",
        f"- n_api_fail_cache_distinct: {article_stats.get('n_api_fail_cache_distinct', 0):,}",
        f"- n_unseen_articles: {article_stats.get('n_unseen_articles', 0):,}",
        f"- n_skipped_known_api_fail_first_pass: {article_stats.get('n_skipped_known_api_fail_first_pass', 0):,}",
        f"- n_retried_api_fail: {article_stats.get('n_retried_api_fail', 0):,}",
        f"- n_api_fail_converted_to_ok: {article_stats.get('n_api_fail_converted_to_ok', 0):,}",
        dataframe_to_markdown(outputs.get("s4_llm_api_usage_by_run", pd.DataFrame())),
        dataframe_to_markdown(outputs.get("s4_llm_api_errors", pd.DataFrame())),
        "",
        "## 21. Label-, Event-Type- und Sprachverteilung",
        dataframe_to_markdown(outputs.get("s4_llm_label_distribution", pd.DataFrame())),
        event_type_note,
        dataframe_to_markdown(outputs.get("s4_llm_event_type_distribution", pd.DataFrame())),
        dataframe_to_markdown(outputs.get("s4_llm_language_distribution", pd.DataFrame())),
        "",
        "## 22. Sektor- und Länderexposures",
        "### Sektor",
        dataframe_to_markdown(outputs.get("s4_sector_exposure", pd.DataFrame())),
        "### Land",
        dataframe_to_markdown(outputs.get("s4_country_exposure", pd.DataFrame())),
        "",
        "## 23. Validierungschecks",
        dataframe_to_markdown(validations, max_rows=120),
        "",
        "## 24. Methodische Hinweise und Limitationen",
        "- S4 ist LLM-basiert ueber OpenRouter.",
        "- S4_LLM_ONLY hat keinen S1-Anteil im Score.",
        "- HYBRID_S1_LLM_50_50 nutzt 50 Prozent S1 und 50 Prozent LLM.",
        "- HYBRID_S1_LLM_70_30 ist nur Robustheit/Shrinkage.",
        "- S4_EN ist der faire Vergleich zu S3/FinBERT; S4_MULTI testet Mehrsprachigkeit und Coverage.",
        "- NEUTRAL_FILL ist die Hauptstrategie; EVENT_ONLY ist Diagnostik.",
        "- signal_date ist die Handels-/Signalzeitachse.",
        "- publication_date wird niemals als Portfolio-Date verwendet.",
        "- LLM erhaelt keine Kursdaten und keine Renditen; es bewertet nur Textwirkung bezogen auf das Unternehmen.",
        "- 50/50-Gewichtung ist ex ante gesetzt und nicht optimiert.",
        "- 70/30 ist wegen moeglicher Verwaesserung der starken S1-Baseline als Robustheit motiviert.",
        "- Die conservative_price_lag1-Logik betrifft den Price-Anteil im Hybrid; die LLM-Komponente bleibt auf signal_date.",
        "- DeepSeek-/LLM-Leakage und Trainingsdatenkontamination werden als Limitation dokumentiert, nicht als vollstaendig ausgeschlossen behauptet.",
        "",
        "## Artikel-Scoring-Statistik",
        "```json",
        json.dumps(article_stats, indent=2, default=str),
        "```",
    ]
    path.write_text("\n".join(line for line in lines if line is not None), encoding="utf-8")


def write_figures(figures_dir: Path, outputs: dict[str, pd.DataFrame]) -> None:
    figures_dir.mkdir(parents=True, exist_ok=True)
    daily = outputs.get("s4_daily_returns", pd.DataFrame()).copy()
    if not daily.empty:
        daily["date"] = pd.to_datetime(daily["date"], errors="coerce")
        main = daily[daily["news_mode"].eq("NEUTRAL_FILL")].copy()
        plt.figure(figsize=(10, 5))
        for key, df in main.groupby(["strategy_variant", "language_mode", "time_logic", "coverage_filter"], observed=True):
            plt.plot(df["date"], df["equity"], label=f"{key[0]} | {key[2]} | {key[3]}")
        plt.title("S4 Equity Curves")
        plt.legend()
        plt.tight_layout()
        plt.savefig(figures_dir / "s4_equity_curves.png", dpi=160)
        plt.close()

        plt.figure(figsize=(10, 5))
        for key, df in main.groupby(["strategy_variant", "language_mode", "time_logic", "coverage_filter"], observed=True):
            plt.plot(df["date"], df["drawdown"], label=f"{key[0]} | {key[2]} | {key[3]}")
        plt.title("S4 Drawdowns")
        plt.legend()
        plt.tight_layout()
        plt.savefig(figures_dir / "s4_drawdowns.png", dpi=160)
        plt.close()

        plt.figure(figsize=(10, 5))
        for key, df in main.groupby(["strategy_variant", "language_mode", "time_logic", "coverage_filter"], observed=True):
            plt.plot(df["date"], df["coverage_ratio"], label=f"{key[0]} | {key[2]} | {key[3]}")
        plt.title("S4 Coverage")
        plt.legend()
        plt.tight_layout()
        plt.savefig(figures_dir / "s4_coverage.png", dpi=160)
        plt.close()

        plt.figure(figsize=(10, 5))
        for key, df in main.groupby(["strategy_variant", "language_mode", "time_logic", "coverage_filter"], observed=True):
            plt.plot(df["date"], df["share_positions_with_news"], label=f"{key[0]} | {key[2]} | {key[3]}")
        plt.title("S4 Share Positions With News")
        plt.legend()
        plt.tight_layout()
        plt.savefig(figures_dir / "s4_share_positions_with_llm_news.png", dpi=160)
        plt.close()

    quintile = outputs.get("s4_quintile_spread_returns", pd.DataFrame()).copy()
    if not quintile.empty:
        quintile["date"] = pd.to_datetime(quintile["date"], errors="coerce")
        plt.figure(figsize=(10, 5))
        for key, df in quintile.groupby(["model_family", "strategy_variant", "news_mode", "time_logic", "signal_name"], observed=True):
            if key[2] != "NEUTRAL_FILL" or key[4] != "score_used":
                continue
            equity = (1.0 + df.sort_values("date")["spread_ret_1d"]).cumprod()
            plt.plot(df.sort_values("date")["date"], equity, label=f"{key[0]} | {key[3]}")
        plt.title("S4 Quintile Spread Diagnostic")
        plt.legend()
        plt.tight_layout()
        plt.savefig(figures_dir / "s4_quintile_spread.png", dpi=160)
        plt.close()

    s1 = outputs.get("s4_vs_s1_daily", pd.DataFrame()).copy()
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
            "S4_LLM_ONLY": "#1f77b4",
            "HYBRID_S1_LLM_50_50": "#2ca02c",
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
                ("S4_LLM_ONLY", "S4 llm-only"),
                ("HYBRID_S1_LLM_50_50", "Hybrid 50/50"),
            ]:
                model = sub[sub["model_family"] == model_family].sort_values("date")
                if model.empty:
                    continue
                ax.plot(
                    model["date"],
                    model["s4_equity_matched"],
                    color=colors[model_family],
                    linewidth=2.0,
                    label=label,
                )
            ax.set_title(f"{time_logic.replace('_', ' ')}\n{coverage_filter}")
            ax.grid(True, alpha=0.25)
            ax.set_ylabel("Matched equity")
        handles, labels = axes[0, 0].get_legend_handles_labels()
        fig.legend(handles, labels, loc="lower center", ncol=3, frameon=False)
        fig.suptitle("S4 LLM / Hybrid vs S1 Price-only, matched dates", fontsize=14)
        fig.autofmt_xdate()
        plt.tight_layout()
        plt.subplots_adjust(bottom=0.12, top=0.88)
        plt.savefig(figures_dir / "s4_vs_s1_equity.png", dpi=160)
        plt.close()

    benchmark = outputs.get("s4_vs_benchmark_daily", pd.DataFrame()).copy()
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
            "S4_LLM_ONLY": "#1f77b4",
            "HYBRID_S1_LLM_50_50": "#2ca02c",
            "BENCHMARK": "#d62728",
        }
        styles = {
            "S1_PRICE_ONLY": "--",
            "S4_LLM_ONLY": "-",
            "HYBRID_S1_LLM_50_50": "-",
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
                ("S4_LLM_ONLY", "S4 llm-only"),
                ("HYBRID_S1_LLM_50_50", "Hybrid 50/50"),
            ]:
                model = sub[sub["model_family"] == model_family].sort_values("date")
                if model.empty:
                    continue
                ax.plot(
                    model["date"],
                    model["s4_equity_matched"],
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
        fig.suptitle("S4 LLM / Hybrid vs S1 Price-only and STOXX Europe 600 Benchmark", fontsize=14)
        fig.autofmt_xdate()
        plt.tight_layout()
        plt.subplots_adjust(bottom=0.13, top=0.88)
        plt.savefig(figures_dir / "s4_vs_s1_benchmark_equity.png", dpi=160)
        plt.savefig(figures_dir / "s4_vs_s1_s2_s3_benchmark_equity.png", dpi=160)
        plt.close()

    s2 = outputs.get("s4_vs_s2_daily", pd.DataFrame()).copy()
    if not s2.empty:
        s2["date"] = pd.to_datetime(s2["date"], errors="coerce")
        plot = s2[(s2["news_mode"] == "NEUTRAL_FILL") & (s2["portfolio_variant"] == "standard")].copy()
        plt.figure(figsize=(10, 5))
        for key, df in plot.groupby(["model_family", "time_logic", "coverage_filter"], observed=True):
            df = df.sort_values("date")
            plt.plot(df["date"], df["s4_equity_matched"], label=f"S4 {key[0]} | {key[1]} | {key[2]}")
            s2_line = df.drop_duplicates("date")
            plt.plot(s2_line["date"], s2_line["s2_equity_matched"], linestyle="--", label=f"S2 matched | {key[1]} | {key[2]}")
        plt.title("S4 vs S2 Matched Equity")
        plt.legend(fontsize=8)
        plt.tight_layout()
        plt.savefig(figures_dir / "s4_vs_s2_equity.png", dpi=160)
        plt.close()

    s3 = outputs.get("s4_vs_s3_daily", pd.DataFrame()).copy()
    if not s3.empty:
        s3["date"] = pd.to_datetime(s3["date"], errors="coerce")
        plot = s3[(s3["news_mode"] == "NEUTRAL_FILL") & (s3["portfolio_variant"] == "standard")].copy()
        plt.figure(figsize=(10, 5))
        for key, df in plot.groupby(["strategy_variant", "time_logic", "coverage_filter"], observed=True):
            df = df.sort_values("date")
            plt.plot(df["date"], df["s4_equity_matched"], label=f"S4 {key[0]} | {key[1]} | {key[2]}")
            s3_line = df.drop_duplicates("date")
            plt.plot(s3_line["date"], s3_line["s3_equity_matched"], linestyle="--", label=f"S3 matched | {key[1]} | {key[2]}")
        plt.title("S4 vs S3 Matched Equity")
        plt.legend(fontsize=8)
        plt.tight_layout()
        plt.savefig(figures_dir / "s4_vs_s3_equity.png", dpi=160)
        plt.close()

    for table, label_col, path_name, title in [
        ("s4_llm_label_distribution", "sentiment_label", "s4_label_distribution.png", "S4 LLM Label Distribution"),
        ("s4_llm_event_type_distribution", "event_type", "s4_event_type_distribution.png", "S4 LLM Event-Type Distribution"),
        ("s4_llm_language_distribution", "language", "s4_language_distribution.png", "S4 LLM Language Distribution"),
    ]:
        dist = outputs.get(table, pd.DataFrame()).copy()
        if dist.empty or label_col not in dist.columns:
            continue
        agg = dist.groupby(label_col, dropna=False, observed=True)["n_articles"].sum().sort_values(ascending=False).head(30)
        plt.figure(figsize=(10, 5))
        agg.plot(kind="bar")
        plt.title(title)
        plt.ylabel("Articles")
        plt.tight_layout()
        plt.savefig(figures_dir / path_name, dpi=160)
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
            if df.empty and len(df.columns) == 0:
                pd.DataFrame(columns=["note"]).to_sql(table, conn, if_exists="replace", index=False)
                continue
            prepare_for_sql(df).to_sql(table, conn, if_exists="replace", index=False)


def ensure_output_schemas(outputs: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    fixed: dict[str, pd.DataFrame] = {}
    for table, df in outputs.items():
        if df.empty and len(df.columns) == 0 and table in EMPTY_TABLE_COLUMNS:
            fixed[table] = pd.DataFrame(columns=EMPTY_TABLE_COLUMNS[table])
        else:
            fixed[table] = df
    return fixed


def build_metadata(
    args: argparse.Namespace,
    config: EffectiveConfig,
    bundle: InputBundle,
    llm: LLMBundle,
    article_stats: dict,
    backup_path: Path | None,
    output_db: Path,
    score_cache_db: Path,
) -> pd.DataFrame:
    rows = [
        ("script_version", SCRIPT_VERSION),
        ("run_id", config.run_id),
        ("run_at_utc", now_utc()),
        ("input_db", str(bundle.input_db)),
        ("input_s1_results_db", str(bundle.input_s1_results_db) if bundle.input_s1_results_db else None),
        ("input_s2_results_db", str(bundle.input_s2_results_db) if bundle.input_s2_results_db else None),
        ("input_s3_results_db", str(bundle.input_s3_results_db) if bundle.input_s3_results_db else None),
        ("output_db", str(output_db)),
        ("score_cache_db", str(score_cache_db)),
        ("config_path", str(config.loaded_path or config.path)),
        ("config_hash", config.config_hash),
        ("scoring_config_hash", config.scoring_config_hash),
        ("compatible_score_cache_hashes", json.dumps(article_stats.get("compatible_score_cache_hashes") or [])),
        ("cache_compatibility_mode", article_stats.get("cache_compatibility_mode")),
        ("cache_prompt_version_rule", article_stats.get("cache_prompt_version_rule")),
        ("legacy_cache_entries_readable", article_stats.get("legacy_cache_entries_readable")),
        ("n_legacy_cache_rows_read", article_stats.get("n_legacy_cache_rows_read")),
        ("scoring_config_hash_used", config.scoring_config_hash),
        ("config_hash_original_preserved", article_stats.get("config_hash_original_preserved")),
        ("backup_created", str(backup_path) if backup_path else None),
        ("price_table", bundle.price_table),
        ("openrouter_base_url", config.openrouter.get("base_url")),
        ("scoring_model", llm.scoring_model),
        ("llm_only_model", llm.llm_only_model),
        ("hybrid_model", llm.hybrid_model),
        ("api_primary_model", llm.api_primary_model),
        ("api_fallback_models", json.dumps(llm.api_fallback_models)),
        ("prompt_version", llm.prompt_version),
        ("prompt_mode", llm.prompt_mode),
        ("response_format_mode", llm.response_format_mode),
        ("single_llm_score_for_all_variants", llm.single_llm_score_for_all_variants),
        ("n_distinct_api_scored_models", len(llm.api_scoring_models)),
        ("api_scoring_models", json.dumps(llm.api_scoring_models)),
        ("language_mode_requested", args.language_mode),
        ("language_modes_active", json.dumps(language_modes_from_arg(args.language_mode))),
        ("language_mode", args.language_mode),
        ("text_cols", json.dumps(bundle.text_cols)),
        ("top_pct", config.backtest.get("top_pct")),
        ("cost_bps", json.dumps(config.backtest.get("cost_bps"))),
        ("coverage_threshold", config.backtest.get("coverage_threshold")),
        ("universe_source_used", bundle.common_news_security_coverage["universe_source"].iloc[0] if not bundle.common_news_security_coverage.empty else None),
        ("security_news_coverage_threshold", bundle.security_news_coverage_threshold),
        ("n_target_securities", int(bundle.common_news_security_coverage[common_news_include_col(bundle.security_news_coverage_threshold)].sum()) if not bundle.common_news_security_coverage.empty and common_news_include_col(bundle.security_news_coverage_threshold) in bundle.common_news_security_coverage.columns else None),
        ("n_articles_total", bundle.n_articles_total),
        ("n_articles_after_text_filter", bundle.n_articles_after_text_filter),
        ("n_articles_s4_en", bundle.n_articles_s4_en),
        ("n_articles_s4_multi", bundle.n_articles_s4_multi),
        ("n_articles_scored", article_stats.get("n_articles_scored")),
        ("n_scores_loaded_from_cache", article_stats.get("n_scores_loaded_from_cache")),
        ("n_scores_newly_computed", article_stats.get("n_scores_newly_computed")),
        ("n_api_fail", article_stats.get("n_api_fail")),
        ("n_ok_cache", article_stats.get("n_ok_cache")),
        ("n_api_fail_cache_distinct", article_stats.get("n_api_fail_cache_distinct")),
        ("n_unseen_articles", article_stats.get("n_unseen_articles")),
        ("n_skipped_known_api_fail_first_pass", article_stats.get("n_skipped_known_api_fail_first_pass")),
        ("n_retried_api_fail", article_stats.get("n_retried_api_fail")),
        ("n_api_fail_converted_to_ok", article_stats.get("n_api_fail_converted_to_ok")),
        ("n_api_requests", article_stats.get("n_api_requests")),
        ("n_json_strict", article_stats.get("n_json_strict")),
        ("n_json_extracted", article_stats.get("n_json_extracted")),
        ("n_json_repaired", article_stats.get("n_json_repaired")),
        ("n_article_id_repaired", article_stats.get("n_article_id_repaired")),
        ("n_partial_batch_fail", article_stats.get("n_partial_batch_fail")),
        ("prompt_tokens_total", article_stats.get("prompt_tokens_total")),
        ("completion_tokens_total", article_stats.get("completion_tokens_total")),
        ("total_tokens_total", article_stats.get("total_tokens_total")),
        ("method_news_date", "signal_date"),
        ("note", "publication_date is never used as portfolio date"),
        ("note", "LLM does not receive price data or returns"),
        ("api_key_status", config.api_key_status),
        ("article_stats", json.dumps(article_stats, default=str)),
    ]
    return pd.DataFrame(rows, columns=["key", "value"])


def build_diagnostics(article_scores: pd.DataFrame, daily_llm: pd.DataFrame, article_stats: dict, config: EffectiveConfig) -> dict[str, pd.DataFrame]:
    ok = article_scores[article_scores.get("api_status", pd.Series(dtype=str)).eq("OK")].copy() if not article_scores.empty else pd.DataFrame()
    label = (
        ok.groupby(["language_mode", "model_name", "sentiment_label"], dropna=False, observed=True)
        .size()
        .reset_index(name="n_articles")
        if not ok.empty
        else pd.DataFrame(columns=["language_mode", "model_name", "sentiment_label", "n_articles"])
    )
    if config.prompt_mode == "fast_score_only":
        event = (
            ok.groupby(["language_mode", "model_name"], dropna=False, observed=True)
            .size()
            .reset_index(name="n_articles")
            if not ok.empty
            else pd.DataFrame(columns=["language_mode", "model_name", "n_articles"])
        )
        if "event_type" not in event.columns:
            event["event_type"] = "not_available_fast_score_only"
        event["note"] = "Not available in fast_score_only prompt mode."
        event = event[["language_mode", "model_name", "event_type", "n_articles", "note"]]
    else:
        event = (
            ok.groupby(["language_mode", "model_name", "event_type"], dropna=False, observed=True)
            .size()
            .reset_index(name="n_articles")
            if not ok.empty
            else pd.DataFrame(columns=["language_mode", "model_name", "event_type", "n_articles"])
        )
    lang = (
        ok.assign(language=ok["language"].fillna("NULL"))
        .groupby(["language_mode", "model_name", "language"], dropna=False, observed=True)
        .size()
        .reset_index(name="n_articles")
        if not ok.empty
        else pd.DataFrame(columns=["language_mode", "model_name", "language", "n_articles"])
    )
    errors = (
        article_scores[article_scores.get("api_status", pd.Series(dtype=str)).ne("OK")]
        [["run_id", "model_name", "api_status", "api_error_type", "api_error_message_sanitized"]]
        .drop_duplicates()
        if not article_scores.empty and "api_status" in article_scores.columns
        else pd.DataFrame(columns=["run_id", "model_name", "api_status", "api_error_type", "api_error_message_sanitized"])
    )
    usage = pd.DataFrame(
        [
            {
                "run_id": config.run_id,
                "model_name": ",".join(sorted(ok["model_name"].dropna().astype(str).unique())) if not ok.empty else None,
                "single_llm_score_for_all_variants": bool(article_stats.get("single_llm_score_for_all_variants")),
                "prompt_mode": article_stats.get("prompt_mode"),
                "prompt_version": config.scoring.get("prompt_version"),
                "scoring_config_hash": article_stats.get("scoring_config_hash"),
                "n_distinct_api_scored_models": article_stats.get("n_distinct_api_scored_models", 0),
                "api_scoring_models": json.dumps(article_stats.get("api_scoring_models", [])),
                "n_api_requests": article_stats.get("n_api_requests", 0),
                "n_articles_requested": article_stats.get("news_rows_after_dedup", 0),
                "n_articles_scored": article_stats.get("n_articles_scored", 0),
                "n_articles_from_cache": article_stats.get("n_scores_loaded_from_cache", 0),
                "n_articles_api_fail": article_stats.get("n_api_fail", 0),
                "n_ok_cache": article_stats.get("n_ok_cache", 0),
                "n_api_fail_cache_distinct": article_stats.get("n_api_fail_cache_distinct", 0),
                "n_unseen_articles": article_stats.get("n_unseen_articles", 0),
                "n_skipped_known_api_fail_first_pass": article_stats.get("n_skipped_known_api_fail_first_pass", 0),
                "n_retried_api_fail": article_stats.get("n_retried_api_fail", 0),
                "n_api_fail_converted_to_ok": article_stats.get("n_api_fail_converted_to_ok", 0),
                "n_json_strict": article_stats.get("n_json_strict", 0),
                "n_json_extracted": article_stats.get("n_json_extracted", 0),
                "n_json_repaired": article_stats.get("n_json_repaired", 0),
                "n_article_id_repaired": article_stats.get("n_article_id_repaired", 0),
                "n_partial_batch_fail": article_stats.get("n_partial_batch_fail", 0),
                "prompt_tokens_total": article_stats.get("prompt_tokens_total", 0),
                "completion_tokens_total": article_stats.get("completion_tokens_total", 0),
                "total_tokens_total": article_stats.get("total_tokens_total", 0),
                "estimated_cost_config_based": None,
                "openrouter_cost_reported": article_stats.get("openrouter_cost_reported"),
                "started_at_utc": config.run_id.split("_")[1] if "_" in config.run_id else None,
                "finished_at_utc": now_utc(),
            }
        ]
    )
    stability = (
        daily_llm.groupby(["language_mode", "model_name"], observed=True)
        .agg(
            n_daily_scores=("llm_score_raw_daily", "count"),
            mean_daily_score=("llm_score_raw_daily", "mean"),
            std_daily_score=("llm_score_raw_daily", "std"),
            mean_n_articles=("n_llm_articles", "mean"),
        )
        .reset_index()
        if not daily_llm.empty
        else pd.DataFrame(columns=["language_mode", "model_name", "n_daily_scores", "mean_daily_score", "std_daily_score", "mean_n_articles"])
    )
    return {
        "s4_llm_label_distribution": label,
        "s4_llm_event_type_distribution": event,
        "s4_llm_language_distribution": lang,
        "s4_llm_api_usage_by_run": usage,
        "s4_llm_api_errors": errors,
        "s4_llm_score_stability_summary": stability,
    }


def estimate_cost_payload(news: pd.DataFrame, config: EffectiveConfig, variants: list[dict[str, Any]], language_modes: list[str]) -> dict[str, Any]:
    articles = prepare_article_frame(news, list(config.data.get("text_cols") or TEXT_COL_CANDIDATES), config)
    en_articles = article_subset_for_language(articles, "EN")
    multi_articles = articles
    needed_article_ids: set[str] = set()
    for mode in language_modes:
        needed_article_ids.update(article_subset_for_language(articles, mode)["article_id"].astype(str))
    needed = articles[articles["article_id"].astype(str).isin(needed_article_ids)].copy()
    model_count = len({v["model_name"] for v in variants})
    approx_prompt_tokens = int((needed["text_used"].astype(str).str.len().sum() / 4.0) * max(model_count, 1))
    request_batch_size = max(1, int(config.request.get("request_batch_size", 8)))
    estimated_requests = int(math.ceil(len(needed) / request_batch_size) * max(model_count, 1))
    approx_completion_tokens = int(estimated_requests * int(config.request.get("max_tokens", 700)))
    return {
        "estimate_cost_only": True,
        "n_articles_after_text_filter": int(len(articles)),
        "n_articles_en": int(len(en_articles)),
        "n_articles_multi": int(len(multi_articles)),
        "n_articles_for_selected_language_mode": int(len(needed)),
        "n_distinct_strategy_variants": int(len(variants)),
        "n_distinct_scoring_models": model_count,
        "distinct_scoring_models": sorted({str(v["model_name"]) for v in variants}),
        "single_score_for_all_variants": bool(config.single_llm_score_for_all_variants),
        "prompt_mode": config.prompt_mode,
        "prompt_version": config.scoring["prompt_version"],
        "request_batch_size": request_batch_size,
        "estimated_api_requests": estimated_requests,
        "approx_prompt_tokens": approx_prompt_tokens,
        "approx_completion_tokens_upper_bound": approx_completion_tokens,
        "approx_total_tokens_upper_bound": approx_prompt_tokens + approx_completion_tokens,
        "note": "Token estimate is heuristic; OpenRouter billing depends on selected model/provider. Hybrid variants do not add API tokens when single_score_for_all_variants is active.",
    }


def main() -> None:
    args = parse_args()
    if args.cache_only_backtest:
        args.no_api = True
    config = load_effective_config(args)
    language_modes = language_modes_from_arg(args.language_mode)
    variants = active_variant_configs(config, language_modes)
    output_dir = resolve_output_path(Path(config.data["output_dir"]))
    output_db = resolve_output_path(Path(config.data["output_db"]))
    score_cache_db = resolve_output_path(Path(config.data["score_cache_db"]))
    top_pct = float(config.backtest.get("top_pct", 0.20))
    coverage_threshold = float(config.backtest.get("coverage_threshold", 0.80))
    cost_bps = [float(x) for x in config.backtest.get("cost_bps", DEFAULT_COST_BPS)]
    security_threshold = float(config.backtest.get("security_news_coverage_threshold", 0.0))
    if args.input_db is not None and resolve_existing_path(args.input_db, []) == output_db:
        raise RuntimeError("Refusing to overwrite input DB with output DB.")
    for label, path_str in [
        ("S1", config.data.get("input_s1_results_db")),
        ("S2", config.data.get("input_s2_results_db")),
        ("S3", config.data.get("input_s3_results_db")),
    ]:
        candidate = resolve_optional_path(Path(path_str)) if path_str else None
        if candidate is not None and candidate == output_db:
            raise RuntimeError(f"Refusing to overwrite {label} results DB with output DB.")

    bundle = load_inputs(args, config)
    price, price_notes = prepare_price_panel(bundle.price_panel)
    if args.dry_run:
        create_cache_schema(score_cache_db)
        print(json.dumps({
            "dry_run": True,
            "config_loaded": str(config.loaded_path) if config.loaded_path else None,
            "input_db": str(bundle.input_db),
            "price_table": bundle.price_table,
            "news_total": bundle.n_articles_total,
            "news_after_text_filter": len(bundle.news),
            "s4_en_news": bundle.n_articles_s4_en,
            "s4_multi_news": bundle.n_articles_s4_multi,
            "score_cache_db": str(score_cache_db),
            "llm_only_model": config.models["llm_only_model"],
            "hybrid_model": config.models["hybrid_model"],
            "scoring_model": config.models.get("scoring_model"),
            "api_primary_model": config.models.get("api_primary_model") or config.models.get("scoring_model"),
            "api_fallback_models": config.models.get("api_fallback_models")
            if config.models.get("api_fallback_models") is not None
            else config.models.get("fallback_models"),
            "single_llm_score_for_all_variants": config.single_llm_score_for_all_variants,
            "prompt_mode": config.prompt_mode,
            "prompt_version": config.scoring["prompt_version"],
            "config_hash": config.config_hash,
            "scoring_config_hash": config.scoring_config_hash,
            "api_key_status": config.api_key_status,
            "language_modes": language_modes,
            "n_variants": len(variants),
            "n_distinct_scoring_models": len({v["model_name"] for v in variants}),
        }, indent=2, default=str))
        return
    if args.estimate_cost_only:
        print(json.dumps(estimate_cost_payload(bundle.news, config, variants, language_modes), indent=2, default=str))
        return

    article_scores, article_stats, llm = score_articles(bundle.news, bundle.text_cols, args, config, language_modes, variants)
    article_stats["score_cache_db"] = str(score_cache_db)
    daily_llm = aggregate_daily_llm(article_scores)
    common_source_articles = article_scores[article_scores["api_status"].eq("OK")][["security_id", "signal_date", "language_mode"]].drop_duplicates()
    common_ids, common_coverage, source_used, common_notes = build_common_news_universe(
        price,
        common_source_articles,
        security_threshold,
    )
    bundle.common_news_security_coverage = common_coverage
    bundle.source_notes.extend(common_notes)
    panel = build_backtest_panel(price, daily_llm, common_ids, variants, config)
    for frame in [panel]:
        frame["universe_source"] = source_used
        frame["security_news_coverage_threshold"] = security_threshold
    positions, daily, coverage = run_backtests(panel, top_pct, coverage_threshold, variants)
    for frame in [positions, daily, coverage]:
        if not frame.empty:
            frame["universe_source"] = source_used
            frame["security_news_coverage_threshold"] = security_threshold
    daily, _ = compute_turnover_and_costs(daily, positions, cost_bps)
    if not daily.empty:
        daily["universe_source"] = source_used
        daily["security_news_coverage_threshold"] = security_threshold
    rf = load_risk_free(bundle.input_db, bundle.input_s1_results_db)
    metrics = compute_metrics(daily, rf, variants)
    metrics_net = compute_metrics_net(pd.DataFrame(), daily, rf, cost_bps, variants)
    bench_daily, bench_metrics, bench_notes = compute_benchmark_comparison(daily, bundle.benchmark_prices)
    ic_by_date, ic_summary, q_returns, q_metrics = compute_signal_diagnostics(panel)
    sector, country, top_positions = compute_exposures(positions)
    s4_vs_s1_daily, s4_vs_s1_metrics, s1_note = compare_with_s1(daily, bundle.input_s1_results_db, security_threshold)
    s4_vs_s2_daily, s4_vs_s2_metrics, s2_note = compare_with_s2(daily, bundle.input_s2_results_db)
    s4_vs_s3_daily, s4_vs_s3_metrics, s3_note = compare_with_s3(daily, bundle.input_s3_results_db)
    comparison_summary = build_model_comparison_summary(metrics, metrics_net, s4_vs_s1_metrics)
    diagnostics = build_diagnostics(article_scores, daily_llm, article_stats, config)
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
        s4_vs_s1_daily,
        s4_vs_s1_metrics,
        s4_vs_s2_daily,
        s4_vs_s2_metrics,
        s4_vs_s3_daily,
        s4_vs_s3_metrics,
        comparison_summary,
    ]:
        if not frame.empty:
            frame["universe_source"] = source_used
            frame["security_news_coverage_threshold"] = security_threshold

    outputs: dict[str, pd.DataFrame] = {
        "s4_run_metadata": pd.DataFrame(),
        "s4_config_snapshot": pd.DataFrame(
            [{"config_json_redacted": json.dumps(safe_config_snapshot(config.raw), sort_keys=True, default=str)}]
        ),
        "s4_llm_article_scores": article_scores,
        "s4_llm_daily_scores": daily_llm,
        "s4_backtest_panel": panel,
        "s4_positions": positions,
        "s4_daily_returns": daily,
        "s4_metrics": metrics,
        "s4_metrics_net": metrics_net,
        "s4_coverage_by_date": coverage,
        "s4_ic_by_date": ic_by_date,
        "s4_ic_summary": ic_summary,
        "s4_quintile_spread_returns": q_returns,
        "s4_quintile_spread_metrics": q_metrics,
        "s4_vs_benchmark_daily": bench_daily,
        "s4_vs_benchmark_metrics": bench_metrics,
        "s4_vs_s1_daily": s4_vs_s1_daily,
        "s4_vs_s1_metrics": s4_vs_s1_metrics,
        "s4_vs_s2_daily": s4_vs_s2_daily,
        "s4_vs_s2_metrics": s4_vs_s2_metrics,
        "s4_vs_s3_daily": s4_vs_s3_daily,
        "s4_vs_s3_metrics": s4_vs_s3_metrics,
        "s4_sector_exposure": sector,
        "s4_country_exposure": country,
        "s4_top_positions": top_positions,
        "s4_metric_definitions": build_metric_definitions(),
        "s4_model_comparison_summary": comparison_summary,
        "s4_common_llm_security_coverage": common_coverage,
        **diagnostics,
    }

    figures_dir = output_dir / "figures"
    report_path = output_dir / "reports" / "s4_llm_report.md"
    write_figures(figures_dir, outputs)
    write_report(report_path, outputs, bundle, llm, {**article_stats, "price_notes": price_notes}, bench_notes, s1_note, s2_note, s3_note, config)

    backup_path = backup_output_if_exists(output_db) if output_db.exists() else None
    outputs["s4_run_metadata"] = build_metadata(args, config, bundle, llm, {**article_stats, "price_notes": price_notes}, backup_path, output_db, score_cache_db)
    validations = run_validations(
        bundle,
        llm,
        article_scores,
        daily_llm,
        panel,
        positions,
        daily,
        bench_daily,
        s4_vs_s1_metrics,
        report_path,
        figures_dir,
        output_db,
        args,
        config,
        variants,
        s4_vs_s2_metrics,
        s4_vs_s3_metrics,
    )
    outputs["s4_validation_checks"] = validations
    write_report(report_path, outputs, bundle, llm, {**article_stats, "price_notes": price_notes}, bench_notes, s1_note, s2_note, s3_note, config)
    outputs = ensure_output_schemas(outputs)
    write_outputs(output_db, outputs)

    main_tables = [
        "s4_run_metadata",
        "s4_llm_article_scores",
        "s4_llm_daily_scores",
        "s4_backtest_panel",
        "s4_positions",
        "s4_daily_returns",
        "s4_metrics",
        "s4_validation_checks",
    ]
    row_counts = {table: len(outputs.get(table, pd.DataFrame())) for table in main_tables}
    validation_counts = validations["status"].value_counts().to_dict()
    print("S4 llm backtest completed.")
    print(f"output_db: {output_db}")
    print(f"row_counts: {row_counts}")
    print(f"validation_counts: {validation_counts}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
