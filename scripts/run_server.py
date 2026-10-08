#!/usr/bin/env python3
"""Load the generated local .env without echoing its contents, then run the app."""

import os
from pathlib import Path
import uvicorn

path = Path(".env")
if path.exists():
    for line in path.read_text().splitlines():
        if line and not line.startswith("#"):
            name, value = line.split("=", 1)
            if name.startswith("SE_"):
                os.environ.setdefault(name, value)
uvicorn.run(
    "securities_eye.app:create_app",
    factory=True,
    host="127.0.0.1",
    port=8000,
    proxy_headers=False,
)
