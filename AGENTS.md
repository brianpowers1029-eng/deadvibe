# Dead Vibe Matcher

A minimal app that turns a "vibe" description into Grateful Dead live-show recommendations.

- `app.py` — FastAPI backend (Claude-powered `/recommend`, `/feedback`, `/health`). Runs on port 8000.
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
- `ANTHROPIC_API_KEY` is **required at import time** — `app.py` reads it via `os.environ["ANTHROPIC_API_KEY"]` at line 37, so the process crashes with `KeyError` before serving anything if it is unset. It must be a valid key for `/recommend` (core functionality) to work; the code targets the `claude-opus-4-6` model.
- `SETLIST_FM_API_KEY` (optional) enriches recommendations with real setlists; without it, setlist data is simply omitted (frontend handles gracefully).
- `ALLOWED_ORIGINS` (optional) restricts CORS; defaults to `*`.

### Gotchas
- The frontend hardcodes `const API_BASE = "http://localhost:8000"` in `index.html`. It only works against a backend reachable at that origin.
- `/recommend` is rate-limited (10/min per IP) and caches identical requests for 6 hours, so repeated identical prompts return cached results rather than re-calling Claude.
- Feedback votes persist to a local `feedback.db` SQLite file (auto-created on startup).
- There is no lint config or automated test suite in this repo.
