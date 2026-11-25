"""Standalone FastAPI production launcher for Anime Universe City."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
ROOT = PROJECT_DIR.parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
if (ROOT / "antonella").is_dir() and str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))


def _load_env_defaults(env_path: Path, allowed_keys: set[str] | None = None) -> None:
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        clean = raw_line.strip()
        if not clean or clean.startswith("#") or "=" not in clean:
            continue
        key, val = clean.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if allowed_keys is not None and key not in allowed_keys:
            continue
        if key and val and not os.environ.get(key):
            os.environ[key] = val


# Load standalone AU City .env BEFORE importing antonella.api.routes_au_city / AuCityService
_load_env_defaults(
    PROJECT_DIR / ".env",
    allowed_keys={"PORT", "AU_CITY_DB_PATH", "DATABASE_URL", "AU_CITY_GOOGLE_CLIENT_ID", "TRIPO_API_KEY"},
)
_load_env_defaults(ROOT / ".env", allowed_keys={"TRIPO_API_KEY"})

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from antonella.api.routes_au_city import phase_manager, router as au_city_router

app = FastAPI(title="Anime Universe City Server", version="1.1.0")

FORBIDDEN_PREFIXES = ("/.env", "/.git", "/data", "/server", "/shared", "/node_modules")
DOT_SEGMENT_RE = re.compile(r"/\.[^/]+")


@app.middleware("http")
async def security_and_dotfile_gate(request: Request, call_next):
    low_path = str(request.url.path or "/").lower()
    if (
        ".." in low_path
        or any(low_path == p or low_path.startswith(f"{p}/") or low_path.startswith(f"{p}.") for p in FORBIDDEN_PREFIXES)
        or DOT_SEGMENT_RE.search(low_path)
    ):
        return PlainTextResponse("Not found", status_code=404)

    response: Response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://accounts.google.com; "
        "style-src 'self' 'unsafe-inline' https://accounts.google.com https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "img-src 'self' https: data: blob:; "
        "connect-src 'self' https://accounts.google.com https://oauth2.googleapis.com ws: wss: blob: data: https:; "
        "frame-src https://accounts.google.com https://www.youtube.com; "
        "frame-ancestors 'none';"
    )
    return response


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(au_city_router)


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "service": "anime-universe-city",
        "db_backend": "postgres" if os.getenv("DATABASE_URL", "").strip() else "sqlite",
        "google_client_id_configured": bool(os.getenv("AU_CITY_GOOGLE_CLIENT_ID", "").strip()),
        "tripo_configured": bool(os.getenv("TRIPO_API_KEY", "").strip()),
        "active_online": len(phase_manager.clients),
    }


@app.get("/robots.txt")
def robots(request: Request):
    origin = str(request.base_url).rstrip("/")
    return PlainTextResponse(f"User-agent: *\nAllow: /\nDisallow: /api/\nSitemap: {origin}/sitemap.xml\n")


@app.get("/sitemap.xml")
def sitemap(request: Request):
    origin = str(request.base_url).rstrip("/")
    xml = f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>{origin}/</loc></url></urlset>'
    return Response(content=xml, media_type="application/xml")


STANDALONE_DIST = PROJECT_DIR / "static" / "dist"
FRONTEND_DIST = STANDALONE_DIST if (STANDALONE_DIST / "index.html").is_file() else (ROOT / "frontend" / "dist")
FRONTEND_PUBLIC = ROOT / "frontend" / "public"
if (FRONTEND_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="assets")
MODELS_DIR = (FRONTEND_DIST / "models" / "au_city") if (FRONTEND_DIST / "models" / "au_city").is_dir() else (FRONTEND_PUBLIC / "models" / "au_city")
if MODELS_DIR.is_dir():
    app.mount(
        "/models/au_city",
        StaticFiles(directory=str(MODELS_DIR)),
        name="au-city-models",
    )
IMAGES_DIR = (FRONTEND_DIST / "images" / "au_city") if (FRONTEND_DIST / "images" / "au_city").is_dir() else (FRONTEND_PUBLIC / "images" / "au_city")
if IMAGES_DIR.is_dir():
    app.mount(
        "/images/au_city",
        StaticFiles(directory=str(IMAGES_DIR)),
        name="au-city-images",
    )


@app.get("/{full_path:path}")
def serve_au_city(full_path: str = ""):
    clean_path = str(full_path or "").lstrip("/").lower()
    if clean_path.startswith(("api/", "assets/", "models/", "images/")):
        return PlainTextResponse("Not found", status_code=404)
    idx = FRONTEND_DIST / "index.html"
    if idx.is_file():
        html = idx.read_text(encoding="utf-8")
        html = re.sub(
            r"<title>.*?</title>",
            "<title>Anime Universe City — 3D Map & RP World</title>",
            html,
            count=1,
            flags=re.IGNORECASE | re.DOTALL,
        )
        inject = "<script>window.__AU_CITY_STANDALONE__ = true;</script>"
        if "</head>" in html:
            html = html.replace("</head>", f"  {inject}\n  </head>", 1)
        else:
            html = f"{inject}\n{html}"
        return HTMLResponse(content=html)
    return {"ok": True, "service": "Anime Universe City Backend"}
