# Anime Universe City — Live Deploy Server & Portable Project

Standalone FastAPI + WebGL server configuration for running **Anime Universe City** (`3D Map Mode` + `GTA / RP Mode` + `WoW-style Sharded Multiplayer` + `Private Instanced Residences`) cleanly on `http://localhost:8010` or on Render (`render.yaml`).

## Required Environment Variables (`.env`)
- `GOOGLE_CLIENT_ID`: Google OAuth 2.0 Web Client ID (enables real Google Identity Services Sign-In via `https://accounts.google.com/gsi/client` + `https://oauth2.googleapis.com/tokeninfo` and automatically locks out local dev sign-in with HTTP `403`).
- `TRIPO_API_KEY`: Tripo 3D API key powering user image-to-3D `.glb` avatar generation.
- `AU_CITY_DB_PATH`: Persistent path to SQLite DB (default: `data/au_city.db`).
- `PORT`: Server port (default: `8010`).

## Google Cloud Console Checklist for Live Domains
Add each live origin under **APIs & Services -> Credentials -> OAuth 2.0 Client ID -> Authorized JavaScript origins**:
- `http://localhost:8010`
- `http://127.0.0.1:8010`
- `http://localhost:8000`
- `https://anime-universe-city.onrender.com` (or your custom domain)

## Quick Start
```powershell
pip install -r projects/anime-universe-city/requirements.txt
$env:PYTHONPATH = "."
python -m uvicorn projects.anime-universe-city.run_au_city_server:app --host 0.0.0.0 --port 8010
```
