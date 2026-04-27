#!/usr/bin/env python3
"""
Config Generator: Convert a STOXX 600 constituent CSV into companies_config.json
================================================================================

Accepts a CSV with columns: Name, Ticker, Sector, Country, (optional: Cap, From, To)
and generates the JSON config file for gdelt_server_downloader.py.

Input CSV format (minimum columns):
  Name,Ticker
  ASML Holding,ASML.AS
  SAP SE,SAP.DE
  ...

Extended format (recommended):
  Name,Ticker,Sector,Country,Cap,From,To
  ASML Holding,ASML.AS,Information Technology,NL,large,2024-08-01,current
  Worldline,WLN.PA,Information Technology,FR,mid,2024-08-01,2024-12-20
  ...

Usage:
  python3 generate_config.py --input stoxx600_constituents.csv --output companies_config.json
  python3 generate_config.py --input stoxx600.csv --output config.json --index "STOXX Europe 600"

You can also manually edit the output JSON to adjust queries, alt_queries, dates etc.

Tips for getting the constituent list:
  - Qontigo/STOXX website (free registration)
  - Bloomberg Terminal: SXXP Index -> MEMB
  - Refinitiv/LSEG: search STOXX 600 constituents
  - Wikipedia: "STOXX Europe 600" (check last revision date)
  - Export from Commerzbank Bloomberg access
"""

import csv
import json
import argparse
import re
from datetime import datetime


# Common company name → GDELT search query mappings
# Some companies are better found with shorter/different names
QUERY_OVERRIDES = {
    "ASML Holding NV": "ASML",
    "ASML Holding": "ASML",
    "SAP SE": "SAP SE",
    "LVMH Moet Hennessy Louis Vuitton": "LVMH",
    "LVMH Moet Hennessy Louis Vuitton SE": "LVMH",
    "Hermes International SCA": "Hermes International",
    "L'Oreal SA": "L'Oreal",
    "Deutsche Telekom AG": "Deutsche Telekom",
    "Bayerische Motoren Werke AG": "BMW",
    "Bayerische Motoren Werke": "BMW",
    "Mercedes-Benz Group AG": "Mercedes-Benz",
    "Volkswagen AG": "Volkswagen",
    "Anheuser-Busch InBev SA/NV": "AB InBev",
    "Muenchener Rueckversicherungs-Gesellschaft": "Munich Re",
    "Compagnie de Saint-Gobain SA": "Saint-Gobain",
    "Deutsche Boerse AG": "Deutsche Boerse",
    "Koninklijke Ahold Delhaize NV": "Ahold Delhaize",
    "Koninklijke Philips NV": "Philips",
    "Koninklijke DSM NV": "DSM",
    "Koninklijke KPN NV": "KPN",
    "Reckitt Benckiser Group PLC": "Reckitt",
    "British American Tobacco PLC": "British American Tobacco",
    "Unilever PLC": "Unilever",
    "AstraZeneca PLC": "AstraZeneca",
    "GlaxoSmithKline PLC": "GlaxoSmithKline",
    "GSK PLC": "GSK",
    "Rio Tinto PLC": "Rio Tinto",
    "BHP Group PLC": "BHP Group",
    "Royal Dutch Shell": "Shell",
    "Shell PLC": "Shell",
    "BP PLC": "BP",
    "HSBC Holdings PLC": "HSBC",
    "Barclays PLC": "Barclays",
    "Lloyds Banking Group PLC": "Lloyds Banking Group",
    "Standard Chartered PLC": "Standard Chartered",
    "Zurich Insurance Group AG": "Zurich Insurance",
    "Swiss Re AG": "Swiss Re",
    "UBS Group AG": "UBS",
    "Credit Suisse Group AG": "Credit Suisse",
    "Novartis AG": "Novartis",
    "Roche Holding AG": "Roche",
    "Nestle SA": "Nestle",
    "ABB Ltd": "ABB",
}

# Generate alternative queries for common cases
ALT_QUERY_RULES = {
    "ASML": ["ASML Holding"],
    "LVMH": ["Louis Vuitton", "Moet Hennessy"],
    "SAP SE": ["SAP"],
    "BMW": ["Bayerische Motoren Werke"],
    "Mercedes-Benz": ["Mercedes Benz", "Daimler"],
    "Volkswagen": ["VW"],
    "Deutsche Telekom": ["T-Mobile"],
    "AB InBev": ["Anheuser-Busch"],
    "Munich Re": ["Muenchener Rueck"],
    "Shell": ["Royal Dutch Shell"],
    "DHL Group": ["Deutsche Post"],
    "Stellantis": ["Fiat Chrysler", "PSA Group"],
}


def clean_company_name(name: str) -> str:
    """Remove common suffixes for a cleaner GDELT query."""
    suffixes = [
        " SE", " SA", " AG", " NV", " PLC", " Ltd", " SpA", " SCA",
        " SA/NV", " Inc", " Corp", " Group", " Holding", " Holdings",
        " N.V.", " S.A.", " S.p.A.",
    ]
    result = name.strip()
    for suffix in suffixes:
        if result.endswith(suffix):
            result = result[:-len(suffix)].strip()
    return result


def generate_query(name: str) -> tuple:
    """Generate (primary_query, alt_queries) for a company name."""
    # Check overrides first
    if name in QUERY_OVERRIDES:
        query = QUERY_OVERRIDES[name]
    else:
        query = clean_company_name(name)

    # Check alt query rules
    alt = ALT_QUERY_RULES.get(query, [])

    return query, alt


def main():
    parser = argparse.ArgumentParser(
        description="Generate companies_config.json from a CSV constituent list"
    )
    parser.add_argument("--input", type=str, required=True,
                        help="Input CSV file with constituent data")
    parser.add_argument("--output", type=str, default="companies_config.json",
                        help="Output JSON config file")
    parser.add_argument("--index", type=str, default="STOXX Europe 600",
                        help="Index name")
    parser.add_argument("--start", type=str, default="2024-08-01",
                        help="Default data start date")
    parser.add_argument("--end", type=str, default="2025-12-31",
                        help="Default data end date")
    parser.add_argument("--delimiter", type=str, default=",",
                        help="CSV delimiter (default: comma)")

    args = parser.parse_args()

    # Read CSV
    companies = []
    with open(args.input, "r", encoding="utf-8-sig") as f:
        # Auto-detect delimiter if needed
        sample = f.read(2048)
        f.seek(0)

        if args.delimiter == "auto":
            sniffer = csv.Sniffer()
            dialect = sniffer.sniff(sample)
            reader = csv.DictReader(f, dialect=dialect)
        else:
            reader = csv.DictReader(f, delimiter=args.delimiter)

        # Normalize column names (case-insensitive)
        for row in reader:
            # Try to find columns by various names
            norm = {k.strip().lower(): v.strip() for k, v in row.items() if k}

            name = (norm.get("name") or norm.get("company") or
                    norm.get("company_name") or norm.get("constituent") or "")
            ticker = (norm.get("ticker") or norm.get("symbol") or
                      norm.get("bloomberg_ticker") or norm.get("code") or "")
            sector = (norm.get("sector") or norm.get("gics_sector") or
                      norm.get("industry") or "Unknown")
            country = (norm.get("country") or norm.get("country_code") or
                       norm.get("domicile") or "XX")
            cap = (norm.get("cap") or norm.get("cap_group") or
                   norm.get("size") or "unknown")
            from_date = norm.get("from") or norm.get("from_date") or args.start
            to_date = norm.get("to") or norm.get("to_date") or "current"

            if not name:
                continue

            query, alt_queries = generate_query(name)

            companies.append({
                "query": query,
                "alt_queries": alt_queries,
                "ticker": ticker,
                "name": name,
                "sector": sector,
                "country": country[:2].upper() if country else "XX",
                "cap_group": cap.lower() if cap else "unknown",
                "from_date": from_date,
                "to_date": to_date,
            })

    # Build config
    config = {
        "_meta": {
            "description": f"{args.index} component configuration for GDELT downloader",
            "version": "1.0",
            "generated_at": datetime.utcnow().isoformat(),
            "source_file": args.input,
            "instructions": [
                "Edit from_date/to_date to reflect index composition changes",
                "Add alt_queries for companies with multiple common names",
                "Set to_date to a specific date (YYYY-MM-DD) when a company leaves the index",
                "query = the primary GDELT search term, keep it recognizable but not too generic",
            ]
        },
        "index": args.index,
        "data_start": args.start,
        "data_end": args.end,
        "companies": companies,
    }

    # Write JSON
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

    print(f"Generated config with {len(companies)} companies → {args.output}")
    print(f"\nSector distribution:")
    sectors = {}
    for c in companies:
        sectors[c["sector"]] = sectors.get(c["sector"], 0) + 1
    for s, n in sorted(sectors.items(), key=lambda x: -x[1]):
        print(f"  {s}: {n}")

    print(f"\nCountry distribution:")
    countries = {}
    for c in companies:
        countries[c["country"]] = countries.get(c["country"], 0) + 1
    for s, n in sorted(countries.items(), key=lambda x: -x[1]):
        print(f"  {s}: {n}")

    print(f"\nNext steps:")
    print(f"  1. Review and edit {args.output}")
    print(f"  2. Adjust from_date/to_date for index changes")
    print(f"  3. Run: python3 gdelt_server_downloader.py --config {args.output} --output ./data")


if __name__ == "__main__":
    main()
