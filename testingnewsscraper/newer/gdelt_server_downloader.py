#!/usr/bin/env python3
"""
GDELT News Downloader – Production Server Script
==================================================

Robust, resumable GDELT DOC API downloader for index-based sentiment research.
Designed to run unattended on a server for days/weeks.

Features:
  - External JSON config for company universe (easy to edit)
  - Temporal index composition: respects from_date/to_date per company
  - State file for crash recovery: resumes exactly where it stopped
  - Exponential backoff with jitter for rate limits
  - Rotating log files
  - Progress dashboard in logs
  - Alternative query support (multiple search terms per company)
  - Automatic merging of all results into a single CSV

Usage:
  # Full run (background, server-safe)
  nohup python3 gdelt_server_downloader.py --config companies_config.json --output ./data &

  # Quick test (3 companies, 1 month)
  python3 gdelt_server_downloader.py --config companies_config.json --output ./test \\
      --test-limit 3 --start-override 2024-08-01 --end-override 2024-08-31

  # Resume after crash/restart
  python3 gdelt_server_downloader.py --config companies_config.json --output ./data --resume

  # Only download tone timelines (fast)
  python3 gdelt_server_downloader.py --config companies_config.json --output ./data --tone-only

  # Check progress without downloading
  python3 gdelt_server_downloader.py --config companies_config.json --output ./data --status

Author: Built for Theodor Frank's Bachelor Thesis
"""

import os
import sys
import csv
import json
import time
import random
import signal
import socket
import argparse
import logging
import logging.handlers
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError
from typing import Optional

# ============================================================================
# CONFIGURATION
# ============================================================================
VERSION = "2.1.0"
STATE_FILE = "download_state.json"
LOG_FILE = "gdelt_downloader.log"
GDELT_API_BASE = "https://api.gdeltproject.org/api/v2/doc/doc"
MAX_ARTICLES_PER_QUERY = 250  # GDELT API limit
USER_AGENT = "Mozilla/5.0 (Academic Research - Bachelor Thesis - Sentiment Analysis)"

# Cache: queries that returned Invalid JSON → skip immediately on next call
# Persists across months for the same company, avoids burning API calls
_failed_queries = set()  # e.g. {"AP Moller-Maersk|timelinetone", "SAP SE|artlist"}


# ============================================================================
# GRACEFUL SHUTDOWN
# ============================================================================
_shutdown_requested = False

def signal_handler(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    logging.warning(f"Shutdown signal received ({signum}). Finishing current task and saving state...")

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


# ============================================================================
# STATE MANAGEMENT
# ============================================================================
class DownloadState:
    """Tracks download progress for crash recovery."""

    def __init__(self, state_dir: str):
        self.state_path = os.path.join(state_dir, STATE_FILE)
        self.state = {
            "version": VERSION,
            "started_at": None,
            "last_updated": None,
            "completed_tasks": [],  # list of "ticker|YYYY-MM|mode" strings
            "failed_tasks": [],
            "stats": {
                "total_articles": 0,
                "total_tone_points": 0,
                "total_api_calls": 0,
                "total_errors": 0,
                "total_rate_limits": 0,
            }
        }
        self._load()

    def _load(self):
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, "r") as f:
                    saved = json.load(f)
                self.state.update(saved)
                logging.info(f"Loaded state: {len(self.state['completed_tasks'])} completed tasks")
            except (json.JSONDecodeError, KeyError) as e:
                logging.warning(f"Could not load state file: {e}. Starting fresh.")

    def save(self):
        self.state["last_updated"] = datetime.utcnow().isoformat()
        try:
            with open(self.state_path, "w") as f:
                json.dump(self.state, f, indent=2)
        except IOError as e:
            logging.error(f"Could not save state: {e}")

    def is_completed(self, task_key: str) -> bool:
        return task_key in self.state["completed_tasks"]

    def mark_completed(self, task_key: str):
        if task_key not in self.state["completed_tasks"]:
            self.state["completed_tasks"].append(task_key)

    def mark_failed(self, task_key: str, error: str):
        entry = {"task": task_key, "error": error, "time": datetime.utcnow().isoformat()}
        self.state["failed_tasks"].append(entry)

    def add_stats(self, **kwargs):
        for k, v in kwargs.items():
            if k in self.state["stats"]:
                self.state["stats"][k] += v

    def get_progress_summary(self, total_tasks: int) -> str:
        done = len(self.state["completed_tasks"])
        failed = len(self.state["failed_tasks"])
        pct = (done / total_tasks * 100) if total_tasks > 0 else 0
        stats = self.state["stats"]
        return (
            f"Progress: {done}/{total_tasks} ({pct:.1f}%) | "
            f"Articles: {stats['total_articles']:,} | "
            f"Tone pts: {stats['total_tone_points']:,} | "
            f"API calls: {stats['total_api_calls']:,} | "
            f"Errors: {stats['total_errors']} | "
            f"Rate limits: {stats['total_rate_limits']}"
        )


# ============================================================================
# COMPANY CONFIG LOADER
# ============================================================================
def load_companies(config_path: str) -> tuple:
    """Load and validate company configuration."""
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    companies = []
    for c in config.get("companies", []):
        # Skip comment-only entries
        if "query" not in c:
            continue

        company = {
            "query": c["query"],
            "alt_queries": c.get("alt_queries", []),
            "ticker": c["ticker"],
            "name": c["name"],
            "type": c.get("type", "company"),  # "company" or "macro"
            "sector": c.get("sector", "Unknown"),
            "country": c.get("country", "XX"),
            "cap_group": c.get("cap_group", "unknown"),
            "from_date": datetime.strptime(c["from_date"], "%Y-%m-%d"),
            "to_date": (datetime.strptime(c["to_date"], "%Y-%m-%d")
                        if c.get("to_date", "current") != "current"
                        else datetime(2099, 12, 31)),
        }
        companies.append(company)

    data_start = config.get("data_start", "2024-08-01")
    data_end = config.get("data_end", "2025-12-31")
    index_name = config.get("index", "Custom")

    logging.info(f"Loaded {len(companies)} companies from {config_path}")
    logging.info(f"Index: {index_name} | Period: {data_start} to {data_end}")

    return companies, data_start, data_end, index_name


def get_active_months(company: dict, global_start: datetime, global_end: datetime) -> list:
    """
    Get list of (month_start, month_end) tuples where this company was in the index.
    Respects both the global date range and the company's from_date/to_date.
    """
    effective_start = max(company["from_date"], global_start)
    effective_end = min(company["to_date"], global_end)

    if effective_start > effective_end:
        return []

    months = []
    current = effective_start.replace(day=1)

    while current <= effective_end:
        month_start = max(current, effective_start)
        if current.month == 12:
            next_month = current.replace(year=current.year + 1, month=1)
        else:
            next_month = current.replace(month=current.month + 1)
        month_end = min(next_month - timedelta(days=1), effective_end)

        months.append((month_start, month_end))
        current = next_month

    return months


# ============================================================================
# GDELT API CLIENT
# ============================================================================
def gdelt_request(query: str, mode: str, start_dt: str, end_dt: str,
                  max_records: int = 250, sleep_base: float = 15.0,
                  max_retries: int = 6, state: Optional[DownloadState] = None) -> dict:
    """
    GDELT DOC API request with exponential backoff + jitter.
    """
    encoded = urllib.parse.quote(query)
    url = (
        f"{GDELT_API_BASE}?"
        f"query={encoded}"
        f"&mode={mode}"
        f"&startdatetime={start_dt}"
        f"&enddatetime={end_dt}"
        f"&format=json"
    )
    if mode == "artlist":
        url += f"&maxrecords={max_records}"

    for attempt in range(max_retries):
        if _shutdown_requested:
            logging.info("Shutdown requested, aborting API call")
            return {}

        try:
            req = Request(url, headers={"User-Agent": USER_AGENT})
            resp = urlopen(req, timeout=45)
            raw = resp.read()

            if state:
                state.add_stats(total_api_calls=1)

            if not raw.strip():
                return {}

            return json.loads(raw)

        except HTTPError as e:
            if e.code == 429:
                wait = sleep_base * (2 ** attempt) + random.uniform(0, 5)
                logging.warning(f"Rate limited (429) for '{query}'. "
                                f"Backoff: {wait:.0f}s (attempt {attempt + 1}/{max_retries})")
                if state:
                    state.add_stats(total_rate_limits=1)
                time.sleep(wait)
            elif e.code in (500, 502, 503):
                wait = sleep_base * (attempt + 1) + random.uniform(0, 3)
                logging.warning(f"Server error ({e.code}) for '{query}'. Waiting {wait:.0f}s...")
                time.sleep(wait)
            else:
                logging.error(f"HTTP {e.code} for '{query}': {e}")
                if state:
                    state.add_stats(total_errors=1)
                return {}

        except (URLError, TimeoutError, ConnectionError,
                socket.timeout) as e:
            wait = sleep_base * (attempt + 1) + random.uniform(0, 3)
            logging.warning(f"Network error for '{query}': {e}. Waiting {wait:.0f}s...")
            time.sleep(wait)

        except json.JSONDecodeError:
            cache_key = f"{query}|{mode}"
            _failed_queries.add(cache_key)
            logging.warning(f"Invalid JSON for '{query}' ({mode}) – cached, will skip this query variant in future months")
            if state:
                state.add_stats(total_errors=1)
            return {}

    logging.error(f"Failed after {max_retries} retries: '{query}' ({mode})")
    if state:
        state.add_stats(total_errors=1)
    return {}


# ============================================================================
# DOWNLOAD FUNCTIONS
# ============================================================================
def download_articles_for_month(company: dict, month_start: datetime,
                                month_end: datetime, output_dir: str,
                                sleep_time: float, state: DownloadState) -> int:
    """Download articles for one company for one month. Returns article count."""

    task_key = f"{company['ticker']}|{month_start.strftime('%Y-%m')}|articles"

    if state.is_completed(task_key):
        logging.debug(f"  Skipping (already done): {task_key}")
        return 0

    start_str = month_start.strftime("%Y%m%d000000")
    end_str = month_end.strftime("%Y%m%d235959")

    # Try primary query + alt queries, merge results
    all_articles = {}  # url -> article dict (dedup by URL)

    queries = [company["query"]] + company.get("alt_queries", [])
    for q in queries:
        # Skip queries that previously returned Invalid JSON
        cache_key = f"{q}|artlist"
        if cache_key in _failed_queries:
            logging.debug(f"  Skipping cached-failed query: '{q}' (artlist)")
            continue

        data = gdelt_request(q, "artlist", start_str, end_str,
                             sleep_base=sleep_time, state=state)

        for art in data.get("articles", []):
            url = art.get("url", "")
            if url and url not in all_articles:
                all_articles[url] = art

        # Sleep between alt_query variants too
        if len(queries) > 1:
            time.sleep(sleep_time)

    if not all_articles:
        state.mark_completed(task_key)
        state.save()
        return 0

    # Save to CSV
    articles_dir = os.path.join(output_dir, "articles")
    os.makedirs(articles_dir, exist_ok=True)

    safe_name = company["name"].replace(" ", "_").replace("'", "").replace("/", "_")
    month_str = month_start.strftime("%Y-%m")
    filename = os.path.join(articles_dir, f"{safe_name}_{month_str}.csv")

    fieldnames = [
        "date", "datetime_utc", "ticker", "company", "type", "sector", "country",
        "cap_group", "tone", "title", "url", "domain", "language", "source_country"
    ]

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for art in all_articles.values():
            seen = art.get("seendate", "")
            try:
                dt = datetime.strptime(seen, "%Y%m%dT%H%M%SZ")
                date_str = dt.strftime("%Y-%m-%d")
                datetime_str = dt.strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                date_str = seen[:8] if len(seen) >= 8 else ""
                datetime_str = seen

            writer.writerow({
                "date": date_str,
                "datetime_utc": datetime_str,
                "ticker": company["ticker"],
                "company": company["name"],
                "type": company.get("type", "company"),
                "sector": company["sector"],
                "country": company["country"],
                "cap_group": company["cap_group"],
                "tone": art.get("tone", ""),
                "title": art.get("title", ""),
                "url": art.get("url", ""),
                "domain": art.get("domain", ""),
                "language": art.get("language", ""),
                "source_country": art.get("sourcecountry", ""),
            })

    count = len(all_articles)
    state.mark_completed(task_key)
    state.add_stats(total_articles=count)
    state.save()

    return count


def download_tone_for_month(company: dict, month_start: datetime,
                            month_end: datetime, output_dir: str,
                            sleep_time: float, state: DownloadState) -> int:
    """Download tone timeline for one company for one month."""

    task_key = f"{company['ticker']}|{month_start.strftime('%Y-%m')}|tone"

    if state.is_completed(task_key):
        return 0

    start_str = month_start.strftime("%Y%m%d000000")
    end_str = month_end.strftime("%Y%m%d235959")

    timeline = []
    queries = [company["query"]] + company.get("alt_queries", [])

    for q in queries:
        # Skip queries known to fail (cached from previous months)
        cache_key = f"{q}|timelinetone"
        if cache_key in _failed_queries:
            continue

        data = gdelt_request(q, "timelinetone", start_str, end_str,
                             sleep_base=sleep_time, state=state)
        tl = data.get("timeline", [])
        if tl:
            timeline = tl
            break

        if len(queries) > 1:
            time.sleep(sleep_time)

    if not timeline:
        state.mark_completed(task_key)
        state.save()
        return 0

    tone_dir = os.path.join(output_dir, "tone")
    os.makedirs(tone_dir, exist_ok=True)

    safe_name = company["name"].replace(" ", "_").replace("'", "").replace("/", "_")
    month_str = month_start.strftime("%Y-%m")
    filename = os.path.join(tone_dir, f"{safe_name}_{month_str}_tone.csv")

    fieldnames = ["date", "ticker", "company", "type", "sector", "country", "cap_group", "tone"]
    count = 0

    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for series in timeline:
            for dp in series.get("data", []):
                try:
                    dt = datetime.strptime(dp["date"], "%Y%m%dT%H%M%SZ")
                    date_str = dt.strftime("%Y-%m-%d")
                except (ValueError, KeyError):
                    date_str = str(dp.get("date", ""))

                writer.writerow({
                    "date": date_str,
                    "ticker": company["ticker"],
                    "company": company["name"],
                    "type": company.get("type", "company"),
                    "sector": company["sector"],
                    "country": company["country"],
                    "cap_group": company["cap_group"],
                    "tone": dp.get("value", ""),
                })
                count += 1

    state.mark_completed(task_key)
    state.add_stats(total_tone_points=count)
    state.save()

    return count

    return count


# ============================================================================
# MERGE RESULTS
# ============================================================================
def merge_csv_files(directory: str, output_file: str):
    """Merge all CSV files in a directory into one file."""
    csv_files = sorted(Path(directory).glob("*.csv"))
    if not csv_files:
        return 0

    total_rows = 0
    header_written = False

    with open(output_file, "w", newline="", encoding="utf-8") as fout:
        for csv_file in csv_files:
            with open(csv_file, "r", encoding="utf-8") as fin:
                reader = csv.reader(fin)
                header = next(reader, None)

                if not header_written and header:
                    writer = csv.writer(fout)
                    writer.writerow(header)
                    header_written = True

                for row in reader:
                    writer.writerow(row)
                    total_rows += 1

    return total_rows


# ============================================================================
# MAIN
# ============================================================================
def main():
    parser = argparse.ArgumentParser(
        description="GDELT News Downloader – Production Server Script",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Full background run
  nohup python3 %(prog)s --config companies_config.json --output ./data &

  # Resume after interruption
  python3 %(prog)s --config companies_config.json --output ./data --resume

  # Quick test with 3 companies
  python3 %(prog)s --config companies_config.json --output ./test --test-limit 3

  # Check progress
  python3 %(prog)s --config companies_config.json --output ./data --status
        """
    )
    parser.add_argument("--config", type=str, required=True,
                        help="Path to companies JSON config file")
    parser.add_argument("--output", type=str, default="./gdelt_data",
                        help="Output directory")
    parser.add_argument("--sleep", type=float, default=15.0,
                        help="Base sleep between API calls (seconds, min 10)")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from saved state (automatic if state file exists)")
    parser.add_argument("--test-limit", type=int, default=None,
                        help="Only process N companies (for testing)")
    parser.add_argument("--start-override", type=str, default=None,
                        help="Override config start date (YYYY-MM-DD)")
    parser.add_argument("--end-override", type=str, default=None,
                        help="Override config end date (YYYY-MM-DD)")
    parser.add_argument("--tone-only", action="store_true",
                        help="Only download tone timelines (skip articles)")
    parser.add_argument("--articles-only", action="store_true",
                        help="Only download articles (skip tone)")
    parser.add_argument("--status", action="store_true",
                        help="Show progress status and exit")
    parser.add_argument("--merge-only", action="store_true",
                        help="Only merge existing CSV files and exit")

    args = parser.parse_args()

    # Setup output directory
    os.makedirs(args.output, exist_ok=True)

    # Setup logging: console + rotating file
    log_path = os.path.join(args.output, LOG_FILE)
    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=10_000_000, backupCount=5, encoding="utf-8"
    )
    console_handler = logging.StreamHandler()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[file_handler, console_handler]
    )

    logging.info(f"GDELT Server Downloader v{VERSION}")

    # Load config
    companies, data_start, data_end, index_name = load_companies(args.config)

    global_start = datetime.strptime(args.start_override or data_start, "%Y-%m-%d")
    global_end = datetime.strptime(args.end_override or data_end, "%Y-%m-%d")

    if args.test_limit:
        companies = companies[:args.test_limit]
        logging.info(f"TEST MODE: Limited to {args.test_limit} companies")

    # Load state
    state = DownloadState(args.output)
    if not state.state["started_at"]:
        state.state["started_at"] = datetime.utcnow().isoformat()

    # Calculate total tasks
    total_article_tasks = 0
    total_tone_tasks = 0
    for c in companies:
        months = get_active_months(c, global_start, global_end)
        total_article_tasks += len(months)
        total_tone_tasks += len(months)  # tone = monthly, same granularity as articles

    total_tasks = 0
    if not args.tone_only:
        total_tasks += total_article_tasks
    if not args.articles_only:
        total_tasks += total_tone_tasks

    # Status only
    if args.status:
        print(state.get_progress_summary(total_tasks))
        print(f"\nFailed tasks: {len(state.state['failed_tasks'])}")
        for ft in state.state["failed_tasks"][-10:]:
            print(f"  - {ft['task']}: {ft['error']}")
        return

    # Merge only
    if args.merge_only:
        articles_dir = os.path.join(args.output, "articles")
        tone_dir = os.path.join(args.output, "tone")
        if os.path.exists(articles_dir):
            n = merge_csv_files(articles_dir, os.path.join(args.output, "ALL_ARTICLES.csv"))
            logging.info(f"Merged articles: {n:,} rows → ALL_ARTICLES.csv")
        if os.path.exists(tone_dir):
            n = merge_csv_files(tone_dir, os.path.join(args.output, "ALL_TONE.csv"))
            logging.info(f"Merged tone data: {n:,} rows → ALL_TONE.csv")
        return

    # Print plan
    logging.info("=" * 70)
    logging.info(f"Index: {index_name}")
    logging.info(f"Period: {global_start.date()} to {global_end.date()}")
    logging.info(f"Companies: {len(companies)}")
    logging.info(f"Article download tasks: {total_article_tasks}")
    logging.info(f"Tone download tasks: {total_tone_tasks}")
    logging.info(f"Already completed: {len(state.state['completed_tasks'])}")
    remaining = total_tasks - len(state.state["completed_tasks"])
    est_hours = remaining * args.sleep / 3600
    logging.info(f"Remaining tasks: ~{remaining} (est. {est_hours:.1f} hours)")
    logging.info(f"Sleep between calls: {args.sleep}s")
    logging.info(f"Log file: {log_path}")
    logging.info("=" * 70)

    # === PHASE 1: Tone timelines (month by month, like articles) ===
    if not args.articles_only:
        logging.info("\n" + "=" * 40)
        logging.info("PHASE 1: Daily tone timelines")
        logging.info("=" * 40)

        for i, company in enumerate(companies):
            if _shutdown_requested:
                break

            months = get_active_months(company, global_start, global_end)
            if not months:
                continue

            logging.info(f"[Tone {i+1}/{len(companies)}] {company['name']} ({company['ticker']}) | {len(months)} months")

            company_total = 0
            for m_start, m_end in months:
                if _shutdown_requested:
                    break
                count = download_tone_for_month(
                    company, m_start, m_end, args.output, args.sleep, state
                )
                company_total += count
                time.sleep(args.sleep)

            if company_total > 0:
                logging.info(f"  → {company_total} daily data points")

            time.sleep(args.sleep)

    # === PHASE 2: Articles month by month ===
    if not args.tone_only and not _shutdown_requested:
        logging.info("\n" + "=" * 40)
        logging.info("PHASE 2: Article metadata")
        logging.info("=" * 40)

        for i, company in enumerate(companies):
            if _shutdown_requested:
                break

            months = get_active_months(company, global_start, global_end)
            if not months:
                logging.info(f"[{i+1}/{len(companies)}] {company['name']} – not in index during this period, skipping")
                continue

            logging.info(f"\n[{i+1}/{len(companies)}] {company['name']} ({company['ticker']}) "
                         f"| {company['sector']} | {company['country']} "
                         f"| Active: {months[0][0].date()} to {months[-1][1].date()}")

            company_total = 0
            for m_start, m_end in months:
                if _shutdown_requested:
                    break

                month_str = m_start.strftime("%Y-%m")
                count = download_articles_for_month(
                    company, m_start, m_end, args.output, args.sleep, state
                )

                if count > 0:
                    logging.info(f"  [{month_str}] {count} articles")
                company_total += count

                time.sleep(args.sleep)

            logging.info(f"  TOTAL: {company_total} articles")

            # Progress report every 10 companies
            if (i + 1) % 10 == 0:
                logging.info(f"\n>>> {state.get_progress_summary(total_tasks)}\n")

    # === PHASE 3: Merge all CSVs ===
    if not _shutdown_requested:
        logging.info("\n" + "=" * 40)
        logging.info("PHASE 3: Merging results")
        logging.info("=" * 40)

        articles_dir = os.path.join(args.output, "articles")
        tone_dir = os.path.join(args.output, "tone")

        if os.path.exists(articles_dir):
            n = merge_csv_files(articles_dir, os.path.join(args.output, "ALL_ARTICLES.csv"))
            logging.info(f"Merged articles: {n:,} rows → ALL_ARTICLES.csv")

        if os.path.exists(tone_dir):
            n = merge_csv_files(tone_dir, os.path.join(args.output, "ALL_TONE.csv"))
            logging.info(f"Merged tone: {n:,} rows → ALL_TONE.csv")

    # Final report
    logging.info("\n" + "=" * 70)
    if _shutdown_requested:
        logging.info("DOWNLOAD PAUSED (shutdown requested)")
        logging.info("Run with --resume to continue")
    else:
        logging.info("DOWNLOAD COMPLETE")
    logging.info(state.get_progress_summary(total_tasks))
    logging.info(f"Output: {args.output}/")
    logging.info("=" * 70)

    state.save()


if __name__ == "__main__":
    main()
