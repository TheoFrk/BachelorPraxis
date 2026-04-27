# GDELT News Scraper (Server Run Guide)

This project runs in two stages:
1. Download GDELT tone + article metadata (`gdelt_server_downloader.py`)
2. Scrape full article text from the downloaded URLs (`fulltext_scraper.py`)

## Prerequisites

- Python 3.9+
- `companies_config.json` (already included, or generate your own)
- Internet access from the server

Recommended dependency for better text extraction:

```bash
python3 -m pip install --upgrade pip trafilatura
```

If `trafilatura` is not installed, fulltext scraping still runs with a fallback extractor.

## Start On A Server

Create output directories first (needed for `nohup` log redirection):

```bash
mkdir -p ./data ./data/fulltext
```

### 1. Run the GDELT downloader in background

```bash
nohup python3 gdelt_server_downloader.py \
  --config companies_config.json \
  --output ./data \
  > ./data/nohup_gdelt.out 2>&1 &
```

Useful commands while running:

```bash
python3 gdelt_server_downloader.py --config companies_config.json --output ./data --status
tail -f ./data/gdelt_downloader.log
```

Resume after interruption:

```bash
python3 gdelt_server_downloader.py --config companies_config.json --output ./data --resume
```

### 2. Run fulltext scraping after downloader output exists

```bash
nohup python3 fulltext_scraper.py \
  --input ./data/ALL_ARTICLES.csv \
  --output ./data/fulltext \
  > ./data/fulltext/nohup_fulltext.out 2>&1 &
```

Useful commands while running:

```bash
python3 fulltext_scraper.py --input ./data/ALL_ARTICLES.csv --output ./data/fulltext --status
tail -f ./data/fulltext/fulltext_scraper.log
```

Resume after interruption:

```bash
python3 fulltext_scraper.py --input ./data/ALL_ARTICLES.csv --output ./data/fulltext --resume
```

## What Happens When It Is Finished

### After `gdelt_server_downloader.py` finishes

- Logs `DOWNLOAD COMPLETE`
- Merges monthly/company files into:
  - `./data/ALL_ARTICLES.csv`
  - `./data/ALL_TONE.csv`
- Keeps per-task files in:
  - `./data/articles/`
  - `./data/tone/`
- Stores progress/state in:
  - `./data/download_state.json`
  - `./data/gdelt_downloader.log`

### After `fulltext_scraper.py` finishes

- Logs `SCRAPING COMPLETE`
- Writes final dataset to:
  - `./data/fulltext/fulltext_articles.csv`
- Saves resume state + stats in:
  - `./data/fulltext/scraper_state.json`
  - `./data/fulltext/fulltext_scraper.log`
- Prints a final summary in logs (success count and top tickers by article count)

## Optional: Build Config From Constituents CSV

```bash
python3 generate_config.py --input your_constituents.csv --output companies_config.json
```
