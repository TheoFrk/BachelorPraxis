#!/usr/bin/env python3
"""Small isolated OpenRouter POST worker for hard subprocess timeouts."""

from __future__ import annotations

import json
import sys

import requests


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        response = requests.post(
            payload["url"],
            headers=payload["headers"],
            data=json.dumps(payload["body"], ensure_ascii=False).encode("utf-8"),
            timeout=float(payload["timeout_seconds"]),
        )
        sys.stdout.write(
            json.dumps(
                {
                    "ok": True,
                    "status_code": response.status_code,
                    "text": response.text,
                }
            )
        )
        return 0
    except Exception as exc:
        sys.stdout.write(
            json.dumps(
                {
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
