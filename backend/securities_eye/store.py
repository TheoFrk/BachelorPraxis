from __future__ import annotations

import csv
import calendar
import io
import math
import re
import sqlite3
import statistics
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

SYMBOL = re.compile(r"^[A-Za-z0-9^][A-Za-z0-9.^=_-]{0,39}$")
LAYERS = {"price", "lexicon", "finbert", "llm"}


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect(path):
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def initialize(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as c:
        tables = {
            r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if tables and "schema_version" not in tables:
            raise RuntimeError("Refusing to modify a non-application database")
        if (
            "schema_version" in tables
            and c.execute("SELECT max(version) FROM schema_version").fetchone()[0] != 1
        ):
            raise RuntimeError("Unsupported database schema version")
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript("""
        CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
        INSERT OR IGNORE INTO schema_version VALUES(1);
        CREATE TABLE IF NOT EXISTS securities(
          symbol TEXT PRIMARY KEY, name TEXT NOT NULL, country TEXT NOT NULL DEFAULT '',
          sector TEXT NOT NULL DEFAULT '', currency TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS observations(
          symbol TEXT NOT NULL REFERENCES securities(symbol), date TEXT NOT NULL,
          close REAL, momentum REAL, volatility REAL, source TEXT NOT NULL,
          imported_at TEXT NOT NULL, PRIMARY KEY(symbol,date));
        CREATE TABLE IF NOT EXISTS signals(
          symbol TEXT NOT NULL REFERENCES securities(symbol), date TEXT NOT NULL,
          layer TEXT NOT NULL, score REAL NOT NULL, source TEXT NOT NULL,
          PRIMARY KEY(symbol,date,layer));
        CREATE TABLE IF NOT EXISTS watchlist(symbol TEXT PRIMARY KEY REFERENCES securities(symbol));
        CREATE TABLE IF NOT EXISTS imports(
          id INTEGER PRIMARY KEY, source TEXT NOT NULL, imported_at TEXT NOT NULL,
          rows INTEGER NOT NULL, kind TEXT NOT NULL);
        """)
        obs_columns = {r[1] for r in c.execute("PRAGMA table_info(observations)")}
        if "frequency" not in obs_columns:
            c.execute(
                "ALTER TABLE observations ADD COLUMN frequency TEXT NOT NULL DEFAULT 'daily'"
            )
        columns = {r[1] for r in c.execute("PRAGMA table_info(securities)")}
        for column, default in [
            ("asset_class", "equity"),
            ("underlying", ""),
            ("unit", "share"),
            ("reference_url", ""),
        ]:
            if column not in columns:
                c.execute(
                    f"ALTER TABLE securities ADD COLUMN {column} TEXT NOT NULL DEFAULT '{default}'"
                )
        c.executescript("""
          CREATE TABLE IF NOT EXISTS exposures(symbol TEXT NOT NULL REFERENCES securities(symbol),driver TEXT NOT NULL,region TEXT NOT NULL,relation TEXT NOT NULL,source TEXT NOT NULL,PRIMARY KEY(symbol,driver,region));
          CREATE TABLE IF NOT EXISTS context_series(driver TEXT NOT NULL,region TEXT NOT NULL,date TEXT NOT NULL,metric TEXT NOT NULL,value REAL NOT NULL,unit TEXT NOT NULL,source TEXT NOT NULL,observed_at TEXT NOT NULL,PRIMARY KEY(driver,region,date,metric));
          CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,driver TEXT NOT NULL,region TEXT NOT NULL,date TEXT NOT NULL,title TEXT NOT NULL,url TEXT NOT NULL,source TEXT NOT NULL,observed_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS feed_runs(feed TEXT PRIMARY KEY,last_attempt TEXT NOT NULL,last_success TEXT,status TEXT NOT NULL,message TEXT NOT NULL);
        """)
        version = c.execute("SELECT max(version) FROM schema_version").fetchone()[0]
        if version != 1:
            raise RuntimeError("Unsupported database schema version")


def finite(value, optional=False):
    if value is None or value == "":
        if optional:
            return None
        raise ValueError("Zahl fehlt")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Nur endliche Zahlen sind erlaubt")
    return result


def validate_symbol(symbol):
    if not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol):
        raise ValueError("Ungültiges Wertpapier-Symbol")
    return symbol.upper()


def validate_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Datum muss YYYY-MM-DD sein")
    result = date.fromisoformat(value)
    if result > date.today():
        raise ValueError("Zukünftige Daten sind nicht erlaubt")
    return result.isoformat()


def text(value, limit=160):
    value = str(value or "").strip()
    if len(value) > limit:
        raise ValueError("Textfeld zu lang")
    return value


def import_csv(path, content, source, kind="prices"):
    source = text(source)
    if not source:
        raise ValueError("Quellenangabe fehlt")
    reader = csv.DictReader(io.StringIO(content))
    required = (
        {"symbol", "date", "close"}
        if kind == "prices"
        else {"symbol", "date", "layer", "score"}
    )
    if not required.issubset(reader.fieldnames or []):
        raise ValueError("Erforderliche Spalten: " + ", ".join(sorted(required)))
    records = []
    for row in reader:
        if len(records) >= 100000:
            raise ValueError("Maximal 100000 Zeilen pro Import")
        symbol, day = validate_symbol(row["symbol"]), validate_date(row["date"])
        if kind == "prices":
            close = finite(row["close"])
            if close <= 0:
                raise ValueError("Kurse müssen positiv sein")
            records.append(
                (
                    symbol,
                    day,
                    close,
                    text(row.get("name") or symbol),
                    text(row.get("country")),
                    text(row.get("sector")),
                    text(row.get("currency"), 12),
                    text(row.get("asset_class") or "equity"),
                    text(row.get("underlying")),
                    text(row.get("unit") or "share"),
                    text(row.get("frequency") or "daily"),
                )
            )
        else:
            layer = row["layer"]
            if layer not in LAYERS:
                raise ValueError("Unbekannte Signal-Ebene")
            records.append((symbol, day, layer, finite(row["score"])))
    if not records:
        raise ValueError("Leere Datei")
    stamp = now()
    with connect(path) as c:
        for record in records:
            symbol, day = record[:2]
            if kind == "prices":
                (
                    _,
                    _,
                    close,
                    name,
                    country,
                    sector,
                    currency,
                    asset_class,
                    underlying,
                    unit,
                    frequency,
                ) = record
                if (
                    frequency == "monthly"
                    and int(day[-2:])
                    != calendar.monthrange(int(day[:4]), int(day[5:7]))[1]
                ):
                    raise ValueError("Monatsdaten müssen am Monatsende datiert sein")
                if frequency not in {"daily", "monthly"}:
                    raise ValueError("Frequenz muss daily oder monthly sein")
                if asset_class not in {
                    "equity",
                    "etf",
                    "etc",
                    "commodity_spot",
                    "future",
                    "bond",
                    "index",
                    "commodity_reference",
                }:
                    raise ValueError("Unbekannte Anlageklasse")
                c.execute(
                    "INSERT INTO securities(symbol,name,country,sector,currency) VALUES(?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET name=CASE WHEN excluded.name=excluded.symbol THEN securities.name ELSE excluded.name END,country=CASE WHEN excluded.country='' THEN securities.country ELSE excluded.country END,sector=CASE WHEN excluded.sector='' THEN securities.sector ELSE excluded.sector END,currency=CASE WHEN excluded.currency='' THEN securities.currency ELSE excluded.currency END",
                    (symbol, name, country, sector, currency),
                )
                if "asset_class" in (reader.fieldnames or []):
                    c.execute(
                        "UPDATE securities SET asset_class=?,underlying=?,unit=? WHERE symbol=?",
                        (asset_class, underlying, unit, symbol),
                    )
                c.execute(
                    "INSERT INTO observations(symbol,date,close,momentum,volatility,source,imported_at,frequency) VALUES(?,?,?,NULL,NULL,?,?,?) ON CONFLICT(symbol,date) DO UPDATE SET close=excluded.close,momentum=NULL,volatility=NULL,source=excluded.source,imported_at=excluded.imported_at,frequency=excluded.frequency",
                    (symbol, day, close, source, stamp, frequency),
                )
            else:
                if not c.execute(
                    "SELECT 1 FROM securities WHERE symbol=?", (symbol,)
                ).fetchone():
                    raise ValueError(f"Zuerst Kursdaten importieren: {symbol}")
                c.execute(
                    "INSERT INTO signals VALUES(?,?,?,?,?) ON CONFLICT(symbol,date,layer) DO UPDATE SET score=excluded.score,source=excluded.source",
                    (*record, source),
                )
        if kind == "prices":
            for symbol in {r[0] for r in records}:
                history = c.execute(
                    "SELECT date,close,frequency FROM observations WHERE symbol=? AND close IS NOT NULL ORDER BY date",
                    (symbol,),
                ).fetchall()
                frequencies = {r["frequency"] for r in history}
                if len(frequencies) > 1:
                    raise ValueError("Frequenzen pro Instrument nicht mischen")
                monthly = frequencies == {"monthly"}
                lookback = 12 if monthly else 20
                c.execute(
                    "UPDATE observations SET momentum=NULL,volatility=NULL WHERE symbol=? AND close IS NOT NULL",
                    (symbol,),
                )
                c.execute(
                    "DELETE FROM signals WHERE symbol=? AND layer='price' AND source IN (?,?)",
                    (
                        symbol,
                        "Preis 12M: Momentum minus annualisierte Volatilität",
                        "Preis 20T: Momentum minus annualisierte Volatilität",
                    ),
                )
                for i in range(lookback, len(history)):
                    window = history[i - lookback : i + 1]
                    if monthly:
                        month_ids = [
                            int(r["date"][:4]) * 12 + int(r["date"][5:7])
                            for r in window
                        ]
                        if any(b - a != 1 for a, b in zip(month_ids, month_ids[1:])):
                            continue
                    momentum = window[-1]["close"] / window[0]["close"] - 1
                    returns = [
                        window[j]["close"] / window[j - 1]["close"] - 1
                        for j in range(1, lookback + 1)
                    ]
                    volatility = statistics.stdev(returns) * math.sqrt(
                        12 if monthly else 252
                    )
                    day = history[i]["date"]
                    c.execute(
                        "UPDATE observations SET momentum=?,volatility=? WHERE symbol=? AND date=?",
                        (momentum, volatility, symbol, day),
                    )
                    c.execute(
                        "INSERT INTO signals VALUES(?,?,?,?,?) ON CONFLICT(symbol,date,layer) DO UPDATE SET score=excluded.score,source=excluded.source",
                        (
                            symbol,
                            day,
                            "price",
                            momentum - volatility,
                            f"Preis {'12M' if monthly else '20T'}: Momentum minus annualisierte Volatilität",
                        ),
                    )
        c.execute(
            "INSERT INTO imports(source,imported_at,rows,kind) VALUES(?,?,?,?)",
            (source, stamp, len(records), kind),
        )
    return len(records)


def import_thesis(path, source_path):
    source_path = Path(source_path).resolve()
    if source_path == Path(path).resolve():
        raise ValueError("Quelle und Produktdatenbank müssen getrennt sein")
    if not source_path.is_file():
        raise ValueError("Quelldatenbank nicht gefunden")
    with source_path.open("rb") as file:
        header = file.read(16)
    if header != b"SQLite format 3\x00":
        raise ValueError("Keine SQLite-Datenbank. Git-LFS-Datei zuerst laden.")
    source = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    count, stamp = 0, now()
    try:
        with connect(path) as c:
            for r in source.execute(
                "SELECT yahoo_ticker,date,company,country,sector,mom_12_1,vol_60d,s1_price_score FROM s1_price_panel_scored WHERE yahoo_ticker IS NOT NULL"
            ):
                symbol, day = validate_symbol(r["yahoo_ticker"]), validate_date(
                    r["date"]
                )
                c.execute(
                    "INSERT OR IGNORE INTO securities(symbol,name,country,sector,currency) VALUES(?,?,?,?,?)",
                    (
                        symbol,
                        text(r["company"] or symbol),
                        text(r["country"]),
                        text(r["sector"]),
                        "",
                    ),
                )
                # This source contains features, not observed prices. Never fabricate a price.
                c.execute(
                    "INSERT OR IGNORE INTO observations(symbol,date,close,momentum,volatility,source,imported_at) VALUES(?,?,NULL,?,?,?,?)",
                    (
                        symbol,
                        day,
                        finite(r["mom_12_1"], True),
                        finite(r["vol_60d"], True),
                        "BachelorPraxis S1",
                        stamp,
                    ),
                )
                if r["s1_price_score"] is not None:
                    c.execute(
                        "INSERT INTO signals VALUES(?,?,?,?,?) ON CONFLICT(symbol,date,layer) DO UPDATE SET score=excluded.score,source=excluded.source",
                        (
                            symbol,
                            day,
                            "price",
                            finite(r["s1_price_score"]),
                            "BachelorPraxis S1",
                        ),
                    )
                count += 1
            c.execute(
                "INSERT INTO imports(source,imported_at,rows,kind) VALUES(?,?,?,?)",
                ("BachelorPraxis S1", stamp, count, "thesis"),
            )
    finally:
        source.close()
    return count


def snapshot(path):
    with connect(path) as c:
        rows = c.execute(
            """SELECT s.*,o.date,o.close,o.momentum,o.volatility,o.source,o.imported_at,o.frequency,
          EXISTS(SELECT 1 FROM watchlist w WHERE w.symbol=s.symbol) watched
          FROM securities s LEFT JOIN observations o ON o.symbol=s.symbol
          AND o.date=(SELECT max(date) FROM observations WHERE symbol=s.symbol)
          ORDER BY s.symbol"""
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metric_basis"] = (
                "Momentum 12–1M / Volatilität 60T"
                if item["source"] == "BachelorPraxis S1" and item["close"] is None
                else (
                    "Momentum 12M / Volatilität 12M (annualisiert)"
                    if item["frequency"] == "monthly"
                    else "Momentum 20T / Volatilität 20T (annualisiert)"
                )
            )
            item["age_days"] = (
                (date.today() - date.fromisoformat(item["date"])).days
                if item["date"]
                else None
            )
            item["signals"] = {
                r["layer"]: dict(r)
                for r in c.execute(
                    "SELECT layer,score,source,date FROM signals WHERE symbol=? AND date=?",
                    (item["symbol"], item["date"]),
                )
            }
            item["exposures"] = [
                dict(r)
                for r in c.execute(
                    "SELECT driver,region,relation,source FROM exposures WHERE symbol=?",
                    (item["symbol"],),
                )
            ]
            result.append(item)
        return result


def detail(path, symbol):
    with connect(path) as c:
        return [
            dict(r)
            for r in c.execute(
                "SELECT date,close,momentum,volatility,source,frequency FROM observations WHERE symbol=? ORDER BY date DESC LIMIT 260",
                (symbol,),
            )
        ][::-1]
