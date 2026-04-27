#!/usr/bin/env python3
"""
GDELT Article Fulltext Scraper
================================

Reads the article CSVs produced by gdelt_server_downloader.py,
fetches the full text from each article URL, and produces a clean
CSV ready for FinBERT / LLM sentiment scoring.

Features:
  - Concurrent downloads (ThreadPool) with per-domain rate limiting
  - Resume capability via state file (survives crashes)
  - Domain blocklist for paywalled / bot-blocked sites
  - Language filtering (keep English by default, configurable)
  - Text quality filtering (min word count, boilerplate detection)
  - Progress logging with ETA
  - Graceful shutdown (Ctrl+C saves state)

Output CSV columns:
  date, datetime_utc, ticker, company, sector, country, cap_group,
  title, url, domain, language, source_country, fulltext, word_count,
  scrape_status

Usage:
  # Full run from merged CSV
  nohup python3 fulltext_scraper.py --input ./data/ALL_ARTICLES.csv --output ./data/fulltext &

  # From individual article files
  python3 fulltext_scraper.py --input ./data/articles/ --output ./data/fulltext

  # Test with 50 articles
  python3 fulltext_scraper.py --input ./data/ALL_ARTICLES.csv --output ./test --limit 50

  # Resume after interruption
  python3 fulltext_scraper.py --input ./data/ALL_ARTICLES.csv --output ./data/fulltext --resume

  # Only English articles, min 100 words
  python3 fulltext_scraper.py --input ./data/ALL_ARTICLES.csv --output ./data/fulltext \
      --languages English --min-words 100

  # Check progress
  python3 fulltext_scraper.py --input ./data/ALL_ARTICLES.csv --output ./data/fulltext --status
"""

import os
import csv
import json
import time
import signal
import argparse
import logging
import logging.handlers
import hashlib
from datetime import datetime
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import Optional

try:
    import trafilatura
    from trafilatura.settings import use_config
    TRAFILATURA_AVAILABLE = True
except ImportError:
    TRAFILATURA_AVAILABLE = False

try:
    from urllib.request import urlopen, Request
    from urllib.error import HTTPError, URLError
except ImportError:
    pass


# ============================================================================
# CONFIG
# ============================================================================
VERSION = "1.0.0"
STATE_FILE = "scraper_state.json"
LOG_FILE = "fulltext_scraper.log"
DEFAULT_TIMEOUT = 20
DEFAULT_WORKERS = 4
DEFAULT_DOMAIN_DELAY = 3.0  # seconds between requests to same domain
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# Domains known to block scrapers or require subscription
DOMAIN_BLOCKLIST = {
    "bloomberg.com", "ft.com", "wsj.com", "economist.com",
    "nytimes.com", "washingtonpost.com", "telegraph.co.uk",
    "handelsblatt.com", "manager-magazin.de", "spiegel.de",
    "faz.net", "zeit.de", "sueddeutsche.de",
    "lesechos.fr", "lefigaro.fr",
    "corriere.it", "repubblica.it",
    "elpais.com", "expansion.com",
    "seekingalpha.com",  # requires login
    "barrons.com",  # paywall
}

# Domains that are not news (social media, aggregators with no original content)
DOMAIN_SKIPLIST = {
    "twitter.com", "x.com", "facebook.com", "instagram.com",
    "youtube.com", "reddit.com", "tiktok.com",
    "linkedin.com",
    "biztoc.com",  # pure aggregator, no text
    "news.google.com",  # redirector
}

# ============================================================================
# SHUTDOWN HANDLING
# ============================================================================
_shutdown = False

def _signal_handler(signum, frame):
    global _shutdown
    _shutdown = True
    logging.warning("Shutdown signal received. Finishing current batch...")

signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# ============================================================================
# DOMAIN RATE LIMITER
# ============================================================================
class DomainRateLimiter:
    """Thread-safe per-domain rate limiter."""

    def __init__(self, delay: float = 3.0):
        self.delay = delay
        self._last_request = defaultdict(float)
        self._lock = Lock()

    def wait(self, domain: str):
        with self._lock:
            now = time.time()
            elapsed = now - self._last_request[domain]
            if elapsed < self.delay:
                time.sleep(self.delay - elapsed)
            self._last_request[domain] = time.time()


# ============================================================================
# STATE MANAGEMENT
# ============================================================================
class ScraperState:
    """Track which URLs have been scraped for resume capability."""

    def __init__(self, state_dir: str):
        self.state_path = os.path.join(state_dir, STATE_FILE)
        self.scraped_urls = set()
        self.stats = {
            "total_attempted": 0,
            "success": 0,
            "failed": 0,
            "blocked": 0,
            "skipped_domain": 0,
            "too_short": 0,
            "timeout": 0,
        }
        self._lock = Lock()
        self._load()

    def _load(self):
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, "r") as f:
                    data = json.load(f)
                self.scraped_urls = set(data.get("scraped_urls", []))
                self.stats.update(data.get("stats", {}))
                logging.info(f"Loaded scraper state: {len(self.scraped_urls)} URLs already processed")
            except Exception as e:
                logging.warning(f"Could not load state: {e}")

    def save(self):
        with self._lock:
            try:
                with open(self.state_path, "w") as f:
                    json.dump({
                        "scraped_urls": list(self.scraped_urls),
                        "stats": self.stats,
                        "last_saved": datetime.utcnow().isoformat(),
                    }, f)
            except IOError as e:
                logging.error(f"Could not save state: {e}")

    def is_scraped(self, url: str) -> bool:
        # Use URL hash for memory efficiency with large datasets
        return self._url_hash(url) in self.scraped_urls

    def mark_scraped(self, url: str):
        with self._lock:
            self.scraped_urls.add(self._url_hash(url))

    def add_stat(self, key: str, n: int = 1):
        with self._lock:
            self.stats[key] = self.stats.get(key, 0) + n

    @staticmethod
    def _url_hash(url: str) -> str:
        return hashlib.md5(url.encode()).hexdigest()[:16]

    def get_summary(self) -> str:
        s = self.stats
        total = s["total_attempted"]
        if total == 0:
            return "No articles processed yet"
        rate = s["success"] / total * 100 if total > 0 else 0
        return (
            f"Processed: {total:,} | "
            f"Success: {s['success']:,} ({rate:.1f}%) | "
            f"Failed: {s['failed']:,} | "
            f"Blocked: {s['blocked']:,} | "
            f"Too short: {s['too_short']:,} | "
            f"Timeout: {s['timeout']:,} | "
            f"Domain skip: {s['skipped_domain']:,}"
        )


# ============================================================================
# FULLTEXT EXTRACTION
# ============================================================================
def extract_fulltext(url: str, timeout: int = DEFAULT_TIMEOUT) -> tuple:
    """
    Fetch and extract article fulltext using trafilatura.
    Returns (text, status) where status is 'ok', 'empty', 'error', 'timeout', 'blocked'.
    """
    if not TRAFILATURA_AVAILABLE:
        # Fallback: simple urllib fetch
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT})
            resp = urlopen(req, timeout=timeout)
            html = resp.read().decode("utf-8", errors="replace")
            # Very basic text extraction
            import re
            text = re.sub(r'<[^>]+>', ' ', html)
            text = re.sub(r'\s+', ' ', text).strip()
            return text[:10000], "ok_fallback"
        except Exception as e:
            return "", f"error: {e}"

    try:
        # Configure trafilatura for speed + quality
        config = use_config()
        config.set("DEFAULT", "EXTRACTION_TIMEOUT", str(timeout))

        downloaded = trafilatura.fetch_url(url)

        if downloaded is None:
            return "", "fetch_failed"

        text = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=False,
            no_fallback=False,
            favor_precision=False,  # favor recall for more text
            config=config,
        )

        if text and len(text.strip()) > 0:
            return text.strip(), "ok"
        else:
            return "", "empty"

    except TimeoutError:
        return "", "timeout"
    except Exception as e:
        error_str = str(e)[:100]
        return "", f"error: {error_str}"


def get_domain(url: str) -> str:
    """Extract domain from URL."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.netloc.replace("www.", "")
    except Exception:
        return ""


# ============================================================================
# ARTICLE PROCESSING
# ============================================================================
def process_article(row: dict, rate_limiter: DomainRateLimiter,
                    state: ScraperState, min_words: int = 50,
                    custom_blocklist: set = None) -> Optional[dict]:
    """
    Process a single article: check filters, fetch text, return enriched row.
    Returns None if article should be skipped.
    """
    url = row.get("url", "").strip()
    if not url:
        return None

    if state.is_scraped(url):
        return None  # already done

    state.add_stat("total_attempted")
    domain = get_domain(url)

    # Domain filters
    blocklist = DOMAIN_BLOCKLIST | (custom_blocklist or set())
    if domain in blocklist:
        state.mark_scraped(url)
        state.add_stat("blocked")
        return {**row, "fulltext": "", "word_count": 0, "scrape_status": "blocked_paywall"}

    if domain in DOMAIN_SKIPLIST:
        state.mark_scraped(url)
        state.add_stat("skipped_domain")
        return {**row, "fulltext": "", "word_count": 0, "scrape_status": "skipped_not_news"}

    # Rate limit per domain
    rate_limiter.wait(domain)

    # Fetch fulltext
    text, status = extract_fulltext(url)
    state.mark_scraped(url)

    word_count = len(text.split()) if text else 0

    if status == "timeout":
        state.add_stat("timeout")
    elif "error" in status or status == "fetch_failed":
        state.add_stat("failed")
    elif word_count < min_words:
        state.add_stat("too_short")
        status = f"too_short ({word_count} words)"
    else:
        state.add_stat("success")

    # Clean text: remove excessive whitespace, limit length
    if text:
        text = " ".join(text.split())  # normalize whitespace
        text = text[:50000]  # cap at 50k chars (~8k words) to prevent CSV bloat

    return {
        **row,
        "fulltext": text,
        "word_count": word_count,
        "scrape_status": status,
    }


# ============================================================================
# INPUT LOADING
# ============================================================================
def load_articles(input_path: str, languages: Optional[set] = None,
                  limit: Optional[int] = None) -> list:
    """
    Load articles from a CSV file or a directory of CSV files.
    Deduplicates by URL.
    """
    articles = {}
    files = []

    path = Path(input_path)
    if path.is_dir():
        files = sorted(path.glob("*.csv"))
    elif path.is_file():
        files = [path]
    else:
        logging.error(f"Input not found: {input_path}")
        return []

    for csv_file in files:
        try:
            with open(csv_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    url = row.get("url", "").strip()
                    if not url:
                        continue

                    # Language filter
                    if languages:
                        lang = row.get("language", "").strip()
                        if lang and lang not in languages:
                            continue

                    # Deduplicate by URL
                    if url not in articles:
                        articles[url] = row

                    if limit and len(articles) >= limit:
                        break
        except Exception as e:
            logging.warning(f"Error reading {csv_file}: {e}")

        if limit and len(articles) >= limit:
            break

    logging.info(f"Loaded {len(articles)} unique articles from {len(files)} files")
    return list(articles.values())


# ============================================================================
# MAIN
# ============================================================================
def main():
    parser = argparse.ArgumentParser(
        description="Fetch fulltext for GDELT articles → CSV for FinBERT/LLM",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--input", type=str, required=True,
                        help="Input: ALL_ARTICLES.csv or articles/ directory")
    parser.add_argument("--output", type=str, default="./fulltext_data",
                        help="Output directory")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"Concurrent download threads (default: {DEFAULT_WORKERS})")
    parser.add_argument("--domain-delay", type=float, default=DEFAULT_DOMAIN_DELAY,
                        help=f"Min seconds between requests to same domain (default: {DEFAULT_DOMAIN_DELAY})")
    parser.add_argument("--min-words", type=int, default=50,
                        help="Minimum word count to keep article (default: 50)")
    parser.add_argument("--languages", type=str, default=None,
                        help="Comma-separated languages to keep (e.g., 'English,German')")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max articles to process (for testing)")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from saved state")
    parser.add_argument("--status", action="store_true",
                        help="Show progress and exit")
    parser.add_argument("--batch-size", type=int, default=500,
                        help="Save state every N articles (default: 500)")
    parser.add_argument("--extra-blocklist", type=str, default=None,
                        help="File with additional domains to block (one per line)")

    args = parser.parse_args()

    # Check dependencies
    if not TRAFILATURA_AVAILABLE:
        logging.warning("trafilatura not installed! Install: pip install trafilatura")
        logging.warning("Falling back to basic urllib extraction (lower quality)")

    os.makedirs(args.output, exist_ok=True)

    # Logging
    log_path = os.path.join(args.output, LOG_FILE)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.handlers.RotatingFileHandler(log_path, maxBytes=10_000_000, backupCount=3),
            logging.StreamHandler(),
        ]
    )

    logging.info(f"Fulltext Scraper v{VERSION}")

    # Load state
    state = ScraperState(args.output)

    if args.status:
        print(state.get_summary())
        return

    # Parse languages
    languages = None
    if args.languages:
        languages = set(l.strip() for l in args.languages.split(","))
        logging.info(f"Language filter: {languages}")

    # Load custom blocklist
    custom_blocklist = set()
    if args.extra_blocklist and os.path.exists(args.extra_blocklist):
        with open(args.extra_blocklist) as f:
            custom_blocklist = set(line.strip() for line in f if line.strip())
        logging.info(f"Custom blocklist: {len(custom_blocklist)} domains")

    # Load articles
    articles = load_articles(args.input, languages, args.limit)

    if not articles:
        logging.error("No articles to process!")
        return

    # Filter already-scraped if resuming
    if args.resume:
        before = len(articles)
        articles = [a for a in articles if not state.is_scraped(a.get("url", ""))]
        logging.info(f"Resume mode: {before - len(articles)} already done, {len(articles)} remaining")

    logging.info(f"Articles to process: {len(articles)}")
    est_hours = len(articles) * args.domain_delay / args.workers / 3600
    logging.info(f"Estimated time: ~{est_hours:.1f} hours ({args.workers} workers, {args.domain_delay}s delay)")

    # Setup
    rate_limiter = DomainRateLimiter(delay=args.domain_delay)
    output_file = os.path.join(args.output, "fulltext_articles.csv")

    fieldnames = [
        "date", "datetime_utc", "ticker", "company", "type", "sector", "country",
        "cap_group", "title", "url", "domain", "language", "source_country",
        "fulltext", "word_count", "scrape_status",
    ]

    # Open output file (append mode for resume)
    write_header = not os.path.exists(output_file) or not args.resume
    fout = open(output_file, "a" if args.resume else "w", newline="", encoding="utf-8")
    writer = csv.DictWriter(fout, fieldnames=fieldnames, extrasaction="ignore")
    if write_header:
        writer.writeheader()

    # Process in batches with ThreadPool
    processed = 0
    start_time = time.time()

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            # Submit in manageable batches
            batch_start = 0

            while batch_start < len(articles) and not _shutdown:
                batch_end = min(batch_start + args.batch_size, len(articles))
                batch = articles[batch_start:batch_end]

                futures = {
                    executor.submit(
                        process_article, row, rate_limiter, state,
                        args.min_words, custom_blocklist
                    ): row
                    for row in batch
                }

                for future in as_completed(futures):
                    if _shutdown:
                        break

                    result = future.result()
                    if result is not None:
                        # Ensure all fieldnames exist
                        for fn in fieldnames:
                            if fn not in result:
                                result[fn] = ""
                        writer.writerow(result)
                        processed += 1

                    # Progress logging
                    if processed % 50 == 0 and processed > 0:
                        elapsed = time.time() - start_time
                        rate = processed / elapsed if elapsed > 0 else 0
                        remaining = (len(articles) - processed) / rate if rate > 0 else 0
                        logging.info(
                            f"Progress: {processed}/{len(articles)} "
                            f"({processed/len(articles)*100:.1f}%) | "
                            f"Rate: {rate:.1f}/s | "
                            f"ETA: {remaining/3600:.1f}h | "
                            f"{state.get_summary()}"
                        )

                # Save state after each batch
                fout.flush()
                state.save()
                batch_start = batch_end

    except Exception as e:
        logging.error(f"Fatal error: {e}")
    finally:
        fout.close()
        state.save()

    # Final summary
    elapsed = time.time() - start_time
    logging.info("\n" + "=" * 60)
    if _shutdown:
        logging.info("SCRAPING PAUSED (shutdown requested)")
        logging.info("Run with --resume to continue")
    else:
        logging.info("SCRAPING COMPLETE")
    logging.info(f"Time elapsed: {elapsed/3600:.1f} hours")
    logging.info(state.get_summary())
    logging.info(f"Output: {output_file}")
    logging.info("=" * 60)

    # Generate a summary of what we got
    if os.path.exists(output_file):
        success_count = 0
        ticker_counts = defaultdict(int)
        with open(output_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("scrape_status") == "ok":
                    success_count += 1
                    ticker_counts[row.get("ticker", "unknown")] += 1

        logging.info(f"\nArticles with fulltext: {success_count:,}")
        logging.info("Top 20 companies by article count:")
        for ticker, count in sorted(ticker_counts.items(), key=lambda x: -x[1])[:20]:
            logging.info(f"  {ticker}: {count}")


if __name__ == "__main__":
    main()
