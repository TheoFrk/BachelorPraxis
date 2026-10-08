#!/usr/bin/env python3
"""Generate a local configuration without printing credentials or overwriting files."""

import os
import secrets
from pathlib import Path

path = Path(".env")
try:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    raise SystemExit(".env already exists; left unchanged.")
with os.fdopen(fd, "w") as file:
    file.write(
        f"SE_ACCESS_TOKEN={secrets.token_urlsafe(48)}\nSE_ORIGIN=http://localhost:8000\nSE_ALLOWED_HOSTS=localhost,127.0.0.1\nSE_SECURE_COOKIE=0\n"
    )
print("Created owner-only .env for loopback HTTP. The access token was not printed.")
