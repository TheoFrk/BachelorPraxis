import argparse
import json
import os
from pathlib import Path

from . import store, context


def main():
    parser = argparse.ArgumentParser(description="Securities Eye data operations")
    parser.add_argument(
        "--db", default=os.environ.get("SE_DB", "var/securities-eye.sqlite")
    )
    sub = parser.add_subparsers(dest="command", required=True)
    thesis = sub.add_parser("import-thesis")
    thesis.add_argument("path", type=Path)
    csv = sub.add_parser("import-csv")
    csv.add_argument("path", type=Path)
    csv.add_argument("--source", required=True)
    csv.add_argument("--kind", choices=["prices", "signals"], default="prices")
    sync = sub.add_parser("sync-context")
    sync.add_argument("feed", choices=["weather", "news", "commodities"])
    backup = sub.add_parser("backup")
    backup.add_argument("path", type=Path)
    args = parser.parse_args()
    store.initialize(args.db)
    context.initialize_catalog(args.db)
    if args.command == "sync-context":
        result = context.sync(args.db, args.feed)
        print(json.dumps(result))
        if result["status"] == "error":
            raise SystemExit(1)
        return
    if args.command == "import-thesis":
        count = store.import_thesis(args.db, args.path)
    elif args.command == "import-csv":
        count = store.import_csv(
            args.db, args.path.read_text(encoding="utf-8-sig"), args.source, args.kind
        )
    else:
        import sqlite3

        if args.path.exists() or args.path.resolve() == Path(args.db).resolve():
            parser.error("Backup target must be a new file")
        args.path.parent.mkdir(parents=True, exist_ok=True)
        with store.connect(args.db) as source:
            target = sqlite3.connect(args.path)
            try:
                source.backup(target)
            finally:
                target.close()
        print(json.dumps({"backup": str(args.path)}))
        return
    print(json.dumps({"imported_rows": count}))


if __name__ == "__main__":
    main()
