from __future__ import annotations

import hashlib
import csv
import hmac
import logging
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.concurrency import run_in_threadpool

from . import store, context

log = logging.getLogger("securities_eye")


class Login(BaseModel):
    token: str = Field(max_length=256)


class Watch(BaseModel):
    watched: bool


def create_app(db_path=None, access_token=None, static_dir=None):
    db = str(db_path or os.environ.get("SE_DB", "var/securities-eye.sqlite"))
    token = access_token or os.environ.get("SE_ACCESS_TOKEN", "")
    if len(token) < 32:
        raise RuntimeError("SE_ACCESS_TOKEN must contain at least 32 characters")
    cookie_key = hashlib.sha256(token.encode()).digest()
    secure = os.environ.get("SE_SECURE_COOKIE", "1") != "0"
    origin = os.environ.get("SE_ORIGIN", "http://localhost:8000")
    attempts = {}

    @asynccontextmanager
    async def lifespan(app):
        store.initialize(db)
        context.initialize_catalog(db)
        yield

    app = FastAPI(
        title="Securities Eye",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=os.environ.get(
            "SE_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver"
        ).split(","),
    )

    @app.middleware("http")
    async def boundary(request, call_next):
        request_id = secrets.token_hex(8)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("origin") != origin:
                return JSONResponse(
                    {"detail": "Unzulässiger Ursprung"}, status_code=403
                )
        if request.url.path == "/api/session" and request.method == "POST":
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > 4096:
                    return JSONResponse(
                        {"detail": "Anmeldedaten zu groß"}, status_code=413
                    )
                body.extend(chunk)
            request._body = bytes(body)
        start = time.monotonic()
        response = await call_next(request)
        response.headers.update(
            {
                "X-Request-ID": request_id,
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https://*.tile.openstreetmap.org; connect-src 'self'; frame-ancestors 'none'",
            }
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        log.info(
            "request id=%s method=%s status=%s duration_ms=%.1f",
            request_id,
            request.method,
            response.status_code,
            (time.monotonic() - start) * 1000,
        )
        return response

    def auth(request: Request):
        cookie = request.cookies.get("se_session", "")
        try:
            expires, signature = cookie.split(".")
            expected = hmac.new(
                cookie_key, expires.encode(), hashlib.sha256
            ).hexdigest()
            if (
                not hmac.compare_digest(signature, expected)
                or int(expires) < time.time()
            ):
                raise ValueError()
        except (ValueError, TypeError):
            raise HTTPException(401, "Anmeldung erforderlich")

    @app.post("/api/session")
    def login(body: Login, request: Request, response: Response):
        ip = request.client.host if request.client else "unknown"
        minute = int(time.time() // 60)
        # Bound memory; no forwarded headers trusted for this limiter.
        for key in list(attempts):
            if attempts[key][0] != minute:
                del attempts[key]
        window, count = attempts.get(ip, (minute, 0))
        if count >= 10 or len(attempts) >= 10000:
            raise HTTPException(429, "Zu viele Anmeldeversuche")
        attempts[ip] = (window, count + 1)
        if not hmac.compare_digest(body.token.encode(), token.encode()):
            raise HTTPException(401, "Zugangsschlüssel ungültig")
        expires = str(int(time.time()) + 8 * 3600)
        signature = hmac.new(cookie_key, expires.encode(), hashlib.sha256).hexdigest()
        response.set_cookie(
            "se_session",
            f"{expires}.{signature}",
            httponly=True,
            secure=secure,
            samesite="strict",
            max_age=8 * 3600,
        )
        return {"authenticated": True}

    @app.delete("/api/session")
    def logout(response: Response):
        response.delete_cookie(
            "se_session", secure=secure, httponly=True, samesite="strict"
        )
        return {"authenticated": False}

    @app.exception_handler(sqlite3.OperationalError)
    async def database_unavailable(request, exc):
        return JSONResponse(
            {"detail": "Datenbank vorübergehend nicht verfügbar"}, status_code=503
        )

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        try:
            with store.connect(db) as c:
                c.execute("SELECT version FROM schema_version").fetchone()
        except sqlite3.Error:
            raise HTTPException(503, "Datenbank nicht bereit")
        return {"status": "ready"}

    @app.get("/api/market", dependencies=[Depends(auth)])
    def market():
        return {
            "securities": store.snapshot(db),
            "generated_at": store.now(),
            "mode": "observations",
        }

    @app.get("/api/securities/{symbol}", dependencies=[Depends(auth)])
    def security(symbol: str):
        try:
            symbol = store.validate_symbol(symbol)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        rows = store.detail(db, symbol)
        if not rows:
            with store.connect(db) as c:
                if not c.execute(
                    "SELECT 1 FROM securities WHERE symbol=?", (symbol,)
                ).fetchone():
                    raise HTTPException(404, "Wertpapier nicht gefunden")
        return {"symbol": symbol, "history": rows}

    @app.put("/api/watchlist/{symbol}", dependencies=[Depends(auth)])
    def watch(symbol: str, body: Watch):
        with store.connect(db) as c:
            if not c.execute(
                "SELECT 1 FROM securities WHERE symbol=?", (symbol,)
            ).fetchone():
                raise HTTPException(404, "Wertpapier nicht gefunden")
            if body.watched:
                c.execute("INSERT OR IGNORE INTO watchlist VALUES(?)", (symbol,))
            else:
                c.execute("DELETE FROM watchlist WHERE symbol=?", (symbol,))
        return {"symbol": symbol, "watched": body.watched}

    @app.get("/api/status", dependencies=[Depends(auth)])
    def status():
        with store.connect(db) as c:
            imports = [
                dict(r)
                for r in c.execute("SELECT * FROM imports ORDER BY id DESC LIMIT 30")
            ]
        return {
            "imports": imports,
            "schema_version": 1,
            "live_feed": False,
            "layers": sorted(store.LAYERS),
        }

    @app.get("/api/context", dependencies=[Depends(auth)])
    def context_data():
        return context.snapshot(db)

    @app.get("/api/context/history/{region}", dependencies=[Depends(auth)])
    def context_history(region: str):
        if region not in context.REGIONS:
            raise HTTPException(404, "Region nicht gefunden")
        with store.connect(db) as c:
            rows = [
                dict(r)
                for r in c.execute(
                    "SELECT date,value,unit,source FROM context_series WHERE region=? AND metric='precipitation' ORDER BY date DESC LIMIT 60",
                    (region,),
                )
            ]
        return {"region": region, "history": rows[::-1]}

    @app.post("/api/context/sync/{feed}", dependencies=[Depends(auth)])
    def sync_context(feed: str):
        try:
            return context.sync(db, feed)
        except ValueError as exc:
            raise HTTPException(429, str(exc))

    @app.post("/api/import", dependencies=[Depends(auth)])
    async def upload(request: Request):
        # Bound the streamed request before multipart parsing / decoding.
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > 10 * 1024 * 1024:
                raise HTTPException(413, "Maximal 10 MiB pro Datei")
            data.extend(chunk)
        kind = request.query_params.get("kind", "prices")
        if kind not in {"prices", "signals"}:
            raise HTTPException(422, "Unbekannter Importtyp")
        try:
            count = await run_in_threadpool(
                store.import_csv,
                db,
                data.decode("utf-8-sig"),
                request.query_params.get("source", ""),
                kind,
            )
        except (ValueError, UnicodeError, csv.Error) as exc:
            raise HTTPException(422, str(exc))
        return {"imported_rows": count}

    static = Path(static_dir or os.environ.get("SE_STATIC_DIR", "frontend/dist"))
    if static.exists():
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

        @app.get("/")
        def index():
            return FileResponse(static / "index.html")

    return app
