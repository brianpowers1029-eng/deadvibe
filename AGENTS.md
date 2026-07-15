# Dead Vibe Matcher

A minimal app that turns a "vibe" description into Grateful Dead live-show recommendations.

- `app.py` — FastAPI backend (Claude-powered `/recommend`, `/history/today`, plus `/feedback`, `/subscribe`, `/health`). Runs on port 8000.
- `shows.db` — SQLite catalog of every Grateful Dead show date (~2,000+ rows). Built by `scripts/build_shows_db.py`; `/recommend` reads this at startup.
- `shows_catalog.py` — smaller curated overlay (depth/tags for gateway/classic/deep). Used by the build script and as a fallback if `shows.db` is missing.
- `scripts/build_shows_db.py` — downloads Deadgraph + archive.org metadata and writes `shows.db`.
- `index.html` — static single-page frontend. Calls the backend at `API_BASE` (`http://localhost:8000`).
- `requirements.txt` — Python dependencies.
- `Procfile` — production start command (`uvicorn app:app`), used by hosts like Railway.

## Cursor Cloud specific instructions

### Services

| Service  | Command | Port | Notes |
|----------|---------|------|-------|
| Backend  | `python3 app.py` | 8000 | Runs uvicorn with `reload=True` (hot reload). |
| Frontend | `python3 -m http.server 8080` | 8080 | Static file server; open `http://localhost:8080/index.html`. |

### Required environment
- `ANTHROPIC_API_KEY` is **required at import time** — `app.py` reads it via `os.environ["ANTHROPIC_API_KEY"]` at import, so the process crashes with `KeyError` before serving anything if it is unset. It must be a valid key for `/recommend` (core functionality) to work.
- `/recommend` is catalog-grounded: Haiku (`RECOMMEND_MODEL`, default `claude-haiku-4-5`) picks only from `shows.db` (full 1965–1995 date list), so it cannot invent dates. Typical latency ~8–15s. `/history/today` uses `HISTORY_MODEL` (default `claude-haiku-4-5`).
- `SHOWS_DB_PATH` (optional) overrides the shows SQLite path (default `shows.db`). Rebuild with `python3 scripts/build_shows_db.py`.
- `SETLIST_FM_API_KEY` (optional) attaches real setlists (lookups cached in SQLite for 30 days). Catalog membership is the source of truth for date validity. When `shows.db` includes an `archive_org_id`, Listen links use it directly.
- Deep-catalog quiz answers bias the candidate pool toward `depth=deep` shows (tags come from the curated overlay in `shows_catalog.py`).
- `ALLOWED_ORIGINS` (optional) restricts CORS; defaults to `*`.

### Gotchas
- The frontend hardcodes `const API_BASE = "http://localhost:8000"` in `index.html`. It only works against a backend reachable at that origin.
- `/recommend` is rate-limited (10/min per IP) and caches identical requests for 6 hours in SQLite (`response_cache` table), so repeated identical prompts return cached results rather than re-calling Claude — and the cache survives restarts/hot reloads.
- The frontend supports shareable links: `index.html?vibe=<prompt>[&era=...][&n=3..5]` auto-runs that search on load. The last results are also persisted to `localStorage` for 24h and can be reopened from the home screen.
- Feedback votes (👍/👎 on each result card) and newsletter signups persist to a local `feedback.db` SQLite file (auto-created on startup).
- The tip-jar box in the frontend is hidden unless `TIP_JAR_URL` (a const near the top of the script in `index.html`) is set to a payment link.
- There is no lint config or automated test suite in this repo.
