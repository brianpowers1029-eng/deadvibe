#!/usr/bin/env python3
"""
Dead Vibe Matcher — Backend API
=================================
A minimal FastAPI backend that takes a vibe description and returns
Grateful Dead show recommendations powered by Claude.

Setup:
    pip install fastapi uvicorn anthropic python-dotenv
    Create a .env file with: ANTHROPIC_API_KEY=sk-ant-...
    python app.py
    API available at http://localhost:8000
"""

import os
import re
import json
import time
import sqlite3
import logging
import threading
import random
from contextlib import asynccontextmanager
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import anthropic
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── Show catalog (full Grateful Dead date list) ───────────────────────────────
# Built by scripts/build_shows_db.py from Deadgraph + archive.org. Falls back to
# the smaller curated shows_catalog.py if the DB isn't present yet.
SHOWS_DB_PATH = os.environ.get("SHOWS_DB_PATH", "shows.db")


def _load_show_catalog() -> tuple[list[dict], dict[str, dict], set[str]]:
    if os.path.exists(SHOWS_DB_PATH):
        with sqlite3.connect(SHOWS_DB_PATH) as conn:
            rows = conn.execute(
                """
                SELECT date, venue, city, era, depth, tags, archive_org_id
                FROM shows ORDER BY date
                """
            ).fetchall()
        catalog = [
            {
                "date": date,
                "venue": venue,
                "city": city or "",
                "era": era,
                "depth": depth or "deep",
                "tags": tags or "",
                "archive_org_id": archive_org_id,
            }
            for date, venue, city, era, depth, tags, archive_org_id in rows
        ]
        log.info(f"Loaded {len(catalog)} shows from {SHOWS_DB_PATH}")
    else:
        from shows_catalog import SHOW_CATALOG as _FALLBACK
        catalog = [dict(s) for s in _FALLBACK]
        log.warning(
            f"{SHOWS_DB_PATH} not found — using curated shows_catalog.py "
            f"({len(catalog)} shows). Run: python3 scripts/build_shows_db.py"
        )
    by_date = {s["date"]: s for s in catalog}
    return catalog, by_date, set(by_date)


SHOW_CATALOG, SHOWS_BY_DATE, CATALOG_DATES = _load_show_catalog()

# ── Anthropic Client ──────────────────────────────────────────────────────────
client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# ── Setlist.fm ────────────────────────────────────────────────────────────────
SETLIST_FM_KEY = os.environ.get("SETLIST_FM_API_KEY", "")

# Guard against oversized prompts blowing up token usage / cost.
MAX_PROMPT_CHARS = 2000

# Date-verification statuses returned by lookup_setlist().
DATE_VERIFIED = "verified"          # setlist.fm has a Grateful Dead show on this date
DATE_NOT_FOUND = "not_found"        # setlist.fm confirms no show on this date (likely hallucinated)
DATE_UNVERIFIABLE = "unverifiable"  # could not check (no key / rate limited / network error)

# Setlists are stable historical data — cache aggressively to skip repeat
# network round-trips on popular dates (and across overlapping recommendations).
SETLIST_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days


def _setlist_cache_get(date_str: str) -> tuple[str, dict | None] | None:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT status, payload, created_at FROM setlist_cache WHERE show_date = ?",
                (date_str,),
            ).fetchone()
    except sqlite3.OperationalError:
        # Table not created yet (very early import / first request race).
        return None
    if not row:
        return None
    status, payload_json, created_at = row
    created_ts = datetime.fromisoformat(created_at).timestamp()
    if time.time() - created_ts > SETLIST_CACHE_TTL_SECONDS:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM setlist_cache WHERE show_date = ?", (date_str,))
        return None
    setlist = json.loads(payload_json) if payload_json else None
    return status, setlist


def _setlist_cache_set(date_str: str, status: str, setlist: dict | None) -> None:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                """
                INSERT INTO setlist_cache (show_date, status, payload)
                VALUES (?, ?, ?)
                ON CONFLICT(show_date) DO UPDATE SET
                    status = excluded.status,
                    payload = excluded.payload,
                    created_at = datetime('now')
                """,
                (date_str, status, json.dumps(setlist) if setlist else None),
            )
    except sqlite3.OperationalError as e:
        log.warning(f"Setlist cache write skipped: {e}")


def lookup_setlist(date_str: str) -> tuple[str, dict | None]:
    """Look up a Grateful Dead setlist for a YYYY-MM-DD date on setlist.fm.

    setlist.fm is our source of truth for whether the band actually played on a
    given date: a hit (HTTP 200 with results) confirms the show, while HTTP 404
    means no such show exists — the model hallucinated the date.

    Returns (status, setlist) where status is one of DATE_VERIFIED,
    DATE_NOT_FOUND, or DATE_UNVERIFIABLE. `setlist` is populated only when the
    date is verified. When we cannot check (missing key, rate limit, network
    error) we fail OPEN with DATE_UNVERIFIABLE rather than falsely calling a
    real show fake.
    """
    if not date_str:
        return DATE_UNVERIFIABLE, None
    if not SETLIST_FM_KEY:
        return DATE_UNVERIFIABLE, None

    cached = _setlist_cache_get(date_str)
    if cached is not None:
        return cached

    # Convert YYYY-MM-DD → DD-MM-YYYY (setlist.fm format)
    parts = date_str.split("-")
    if len(parts) != 3:
        return DATE_UNVERIFIABLE, None
    sfm_date = f"{parts[2]}-{parts[1]}-{parts[0]}"

    # setlist.fm rate-limits (~2 req/sec) and we fan out verifications
    # concurrently, so retry briefly on 429 rather than giving up — otherwise a
    # real show could be mislabeled "unverifiable" purely because of throttling.
    resp = None
    for attempt in range(3):
        try:
            resp = requests.get(
                "https://api.setlist.fm/rest/1.0/search/setlists",
                params={"artistName": "Grateful Dead", "date": sfm_date},
                headers={"x-api-key": SETLIST_FM_KEY, "Accept": "application/json"},
                timeout=3,
            )
        except Exception as e:
            log.warning(f"Setlist.fm lookup failed for {date_str}: {e}")
            return DATE_UNVERIFIABLE, None

        if resp.status_code != 429:
            break
        retry_after = resp.headers.get("Retry-After")
        delay = float(retry_after) if (retry_after or "").replace(".", "", 1).isdigit() else 0.5
        time.sleep(min(delay, 1.0))

    # 404 is setlist.fm's "no setlists match" response — a definitive no-show.
    if resp.status_code == 404:
        _setlist_cache_set(date_str, DATE_NOT_FOUND, None)
        return DATE_NOT_FOUND, None
    if resp.status_code != 200:
        # Still rate limited or a server error — don't trust or discard.
        log.warning(f"Setlist.fm returned {resp.status_code} for {date_str}")
        return DATE_UNVERIFIABLE, None

    try:
        data = resp.json()
    except Exception:
        return DATE_UNVERIFIABLE, None

    setlists = data.get("setlist", [])
    if not setlists:
        _setlist_cache_set(date_str, DATE_NOT_FOUND, None)
        return DATE_NOT_FOUND, None

    # Take the first result
    sl = setlists[0]
    sets = sl.get("sets", {}).get("set", [])

    # Flatten into labeled sets
    result = []
    for s in sets:
        name = s.get("name") or s.get("encore") and "Encore" or "Set"
        if s.get("encore"):
            name = "Encore"
        songs = [song.get("name", "") for song in s.get("song", []) if song.get("name")]
        if songs:
            result.append({"name": name, "songs": songs})

    venue_data = sl.get("venue", {})
    venue_name = venue_data.get("name", "")
    city = venue_data.get("city", {}).get("name", "")

    setlist = {
        "sets": result,
        "venue": f"{venue_name}, {city}".strip(", "),
        "setlist_url": sl.get("url", ""),
    }
    _setlist_cache_set(date_str, DATE_VERIFIED, setlist)
    return DATE_VERIFIED, setlist

def _enrich_recommendation(rec: dict) -> None:
    """Verify a recommendation's date and attach its real setlist.

    Mutates only the passed-in dict, so it is safe to run concurrently across
    recommendations. Archive.org IDs are intentionally NOT validated here —
    Claude hallucinates most of them, and each metadata probe added seconds of
    latency for little gain. The frontend builds a date-based search URL when
    no ID is present.
    """
    status, setlist = lookup_setlist(rec.get("date"))
    rec["setlist"] = setlist  # populated only when the date is verified
    rec["date_verified"] = (status == DATE_VERIFIED)
    rec["_date_status"] = status  # internal — stripped before returning
    # Prefer a search link over a guessed identifier.
    rec["archive_org_id"] = None

# ── App ───────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Pre-warm today's "Dead History" in the background so the first visitor
    # doesn't eat the Claude latency. Runs after the module is fully loaded.
    threading.Thread(target=_prewarm_today_history, daemon=True).start()
    yield

app = FastAPI(title="Dead Vibe Matcher", version="1.0.0", lifespan=_lifespan)

# Lock CORS to your site in production by setting ALLOWED_ORIGINS to a
# comma-separated list of origins (e.g. "https://deadvibe.app"). Defaults to "*".
_origins_env = os.environ.get("ALLOWED_ORIGINS", "*").strip()
allowed_origins = ["*"] if _origins_env == "*" else [o.strip() for o in _origins_env.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Compress responses — recommendation payloads carry full setlists and
# compress well, which matters most on mobile connections.
app.add_middleware(GZipMiddleware, minimum_size=500)

# ── Rate Limiting ─────────────────────────────────────────────────────────────
# Each /recommend call spends real Claude credits, so cap per-IP usage.
limiter = Limiter(key_func=get_remote_address, default_limits=["60/hour"])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── Response Cache ────────────────────────────────────────────────────────────
# Identical requests reuse a cached result instead of re-calling Claude. Stored
# in SQLite so it survives restarts — the dev server runs with reload=True and
# production redeploys otherwise wiped a purely in-memory cache.
CACHE_TTL_SECONDS = 6 * 60 * 60   # 6 hours
CACHE_MAX_ENTRIES = 500

def _cache_get(key: str) -> dict | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT payload, created_at FROM response_cache WHERE cache_key = ?",
            (key,),
        ).fetchone()
    if not row:
        return None
    payload_json, created_at = row
    created_ts = datetime.fromisoformat(created_at).timestamp()
    if time.time() - created_ts > CACHE_TTL_SECONDS:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM response_cache WHERE cache_key = ?", (key,))
        return None
    return json.loads(payload_json)

def _cache_set(key: str, value: dict) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO response_cache (cache_key, payload)
            VALUES (?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                payload = excluded.payload,
                created_at = datetime('now')
            """,
            (key, json.dumps(value)),
        )
        # Prune expired entries and bound total size.
        conn.execute(
            "DELETE FROM response_cache WHERE created_at < datetime('now', ?)",
            (f"-{CACHE_TTL_SECONDS} seconds",),
        )
        conn.execute(
            """
            DELETE FROM response_cache WHERE cache_key NOT IN (
                SELECT cache_key FROM response_cache ORDER BY created_at DESC LIMIT ?
            )
            """,
            (CACHE_MAX_ENTRIES,),
        )

# ── Feedback Store ────────────────────────────────────────────────────────────
# Persist 👍/👎 votes so we can see which recommendations actually land.
# NOTE: on an ephemeral host (e.g. a single Railway dyno) this file resets on
# redeploy — swap DB_PATH for a mounted volume or external DB to keep history.
DB_PATH = os.environ.get("FEEDBACK_DB_PATH", "feedback.db")

def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                show_date  TEXT,
                venue      TEXT,
                is_match   INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS history_cache (
                cache_key  TEXT PRIMARY KEY,
                payload    TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                email      TEXT PRIMARY KEY,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS response_cache (
                cache_key  TEXT PRIMARY KEY,
                payload    TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS setlist_cache (
                show_date  TEXT PRIMARY KEY,
                status     TEXT NOT NULL,
                payload    TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

init_db()

# ── Today in Dead History ─────────────────────────────────────────────────────
# Results for a given month/day are stable year over year, so we persist them in
# SQLite (survives restarts) and pre-warm on startup. The dedicated endpoint uses
# a shorter prompt and faster model than /recommend.
MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
HISTORY_MODEL = os.environ.get("HISTORY_MODEL", "claude-haiku-4-5")
HISTORY_MAX_TOKENS = 1600
# MM-DD entries rarely need refreshing; 30 days is plenty for a daily feature.
HISTORY_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
_history_locks: dict[str, threading.Lock] = {}
_history_locks_guard = threading.Lock()


def _history_cache_key(month: int, day: int) -> str:
    return f"{month:02d}-{day:02d}"


def _history_cache_get(key: str) -> dict | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT payload, created_at FROM history_cache WHERE cache_key = ?",
            (key,),
        ).fetchone()
    if not row:
        return None
    payload_json, created_at = row
    created_ts = datetime.fromisoformat(created_at).timestamp()
    if time.time() - created_ts > HISTORY_CACHE_TTL_SECONDS:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM history_cache WHERE cache_key = ?", (key,))
        return None
    return json.loads(payload_json)


def _history_cache_set(key: str, value: dict) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO history_cache (cache_key, payload)
            VALUES (?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                payload = excluded.payload,
                created_at = datetime('now')
            """,
            (key, json.dumps(value)),
        )


def _history_lock(key: str) -> threading.Lock:
    with _history_locks_guard:
        if key not in _history_locks:
            _history_locks[key] = threading.Lock()
        return _history_locks[key]


def _strip_json_fences(raw: str) -> str:
    if raw.startswith("```"):
        parts_split = raw.split("```")
        if len(parts_split) >= 2:
            raw = parts_split[1]
            if raw.startswith("json"):
                raw = raw[4:].strip()
    return raw


def _call_claude(system: str, user_message: str, *, model: str, max_tokens: int) -> dict:
    """Call Claude and parse a JSON object from the response."""
    data = None
    raw = ""
    for attempt in range(2):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_message}],
            )
        except anthropic.AuthenticationError:
            raise HTTPException(401, "Invalid Anthropic API key — check your .env file")
        except anthropic.RateLimitError:
            raise HTTPException(429, "Rate limited — please wait a moment and try again")
        except Exception as e:
            log.error(f"Claude API error: {e}")
            raise HTTPException(500, f"AI error: {str(e)}")

        raw = _strip_json_fences(response.content[0].text.strip())
        try:
            data = json.loads(raw)
            break
        except json.JSONDecodeError as e:
            log.warning(f"Bad JSON from Claude (attempt {attempt + 1}/2): {e}\nRaw: {raw[:500]}")

    if data is None:
        raise HTTPException(500, "AI returned an unexpected format — please try again")
    return data


def _enrich_all(recommendations: list[dict]) -> None:
    if recommendations:
        with ThreadPoolExecutor(max_workers=min(8, len(recommendations))) as pool:
            list(pool.map(_enrich_recommendation, recommendations))


def _verify_and_filter(data: dict) -> dict:
    """Enrich recommendations, drop fabricated dates, and renumber ranks.

    Each recommendation makes independent setlist.fm / archive.org calls, so
    _enrich_all fans them out concurrently. Shows whose date setlist.fm
    confirms never happened are dropped — this is how we stop the model from
    inventing performances. Dates we simply couldn't check are kept but
    flagged (date_verified=false) so the frontend can label them.
    """
    recommendations = data.get("recommendations", [])
    if not recommendations:
        return data

    _enrich_all(recommendations)

    kept = [r for r in recommendations if r.pop("_date_status", None) != DATE_NOT_FOUND]
    dropped = len(recommendations) - len(kept)
    if dropped:
        log.info(f"Dropped {dropped} recommendation(s) with unverifiable/fabricated dates")
    # Renumber ranks so the displayed list has no gaps after filtering.
    for i, rec in enumerate(kept, start=1):
        rec["rank"] = i
    data["recommendations"] = kept
    return data


HISTORY_SYSTEM_PROMPT = """You are a Grateful Dead historian. Given a calendar month and day, return the best Grateful Dead live performances that occurred on that exact date in any year between 1965 and 1995.

Return ONLY valid JSON — no markdown fences, no commentary:
{
  "vibe_interpretation": "1-2 sentences on what makes this date notable in Dead history",
  "recommendations": [
    {
      "rank": 1,
      "date": "1977-05-08",
      "venue": "Barton Hall, Cornell University, Ithaca, NY",
      "era": "Hiatus & Return (1975–1977)",
      "vibe_match": 92,
      "pitch": "1-2 sentences. Lead with a specific song, jam, or setlist fact. No vague praise.",
      "key_moments": ["Two specific moments — name songs, name what happened"],
      "recording_type": "Soundboard",
      "recording_quality": "Brief note on tape quality",
      "caveats": null
    }
  ],
  "deeper_cut": "Brief suggestion for further listening on this date"
}

Rules:
1. Real shows only — real dates, venues, and setlists.
2. Return exactly 3 shows, ranked by overall quality (setlist, improvisation, recording availability).
3. If few shows exist on this date, say so honestly in vibe_interpretation.
4. Never use: transcendent, remarkable, devastating, stunning, beautiful.
5. Return ONLY valid JSON.
"""


def _generate_history(month: int, day: int) -> dict:
    month_name = MONTH_NAMES[month - 1]
    user_message = (
        f"Find the 3 best Grateful Dead shows performed on {month_name} {day} "
        f"(any year from 1965 to 1995). Rank by setlist strength, improvisation, "
        f"and recording availability."
    )
    log.info(f"Generating Dead history for {month_name} {day} via {HISTORY_MODEL}...")
    data = _call_claude(
        HISTORY_SYSTEM_PROMPT,
        user_message,
        model=HISTORY_MODEL,
        max_tokens=HISTORY_MAX_TOKENS,
    )
    return _verify_and_filter(data)


def _get_or_generate_history(month: int, day: int) -> dict:
    key = _history_cache_key(month, day)
    cached = _history_cache_get(key)
    if cached is not None:
        log.info(f"History cache hit: {key}")
        return cached

    lock = _history_lock(key)
    with lock:
        # Another thread may have populated the cache while we waited.
        cached = _history_cache_get(key)
        if cached is not None:
            return cached
        data = _generate_history(month, day)
        _history_cache_set(key, data)
        return data


def _prewarm_today_history() -> None:
    now = datetime.now()
    key = _history_cache_key(now.month, now.day)
    if _history_cache_get(key) is not None:
        log.info(f"History prewarm skipped — cache already warm for {key}")
        return
    try:
        log.info(f"Prewarming Dead history cache for {key}...")
        _get_or_generate_history(now.month, now.day)
        log.info(f"History prewarm complete for {key}")
    except Exception as e:
        log.warning(f"History prewarm failed: {e}")

# ── System Prompt ─────────────────────────────────────────────────────────────
# Catalog-grounded: the model may ONLY pick dates from the candidate list we
# pass in. That eliminates invented "deep cut" dates (the cause of the ~50s
# Sonnet fallback) and keeps every /recommend on a single fast Haiku call.
SYSTEM_PROMPT = """You are the Dead Vibe Matcher. The user describes a vibe; you pick the best matching Grateful Dead shows FROM THE CANDIDATE LIST provided in the user message.

Return ONLY valid JSON — no markdown fences, no commentary:
{
  "vibe_interpretation": "1-2 sentences restating the request in Dead terms",
  "recommendations": [
    {
      "rank": 1,
      "date": "YYYY-MM-DD",
      "vibe_match": 92,
      "pitch": "1-2 sentences. Lead with a specific song, jam, or setlist fact. No vague praise.",
      "key_moments": ["Two specific song moments — name the song and what happened"],
      "recording_type": "Soundboard",
      "recording_quality": "Brief tape-quality note",
      "caveats": null
    }
  ],
  "deeper_cut": "One brief further-listening tip"
}

Rules:
1. EVERY recommendation date MUST appear in the candidate list. Never invent a date.
2. Copy dates exactly as written (YYYY-MM-DD). Do not include venue/era — those are filled in server-side.
3. Return exactly the number of shows requested, ranked by vibe match.
4. When the user wants deep/obscure cuts, prefer candidates tagged "deep" and avoid gateway classics (Cornell, Veneta).
5. Banned words: transcendent, remarkable, devastating, stunning, beautiful, crucial, noteworthy.
6. JSON only.
"""

RECOMMEND_MODEL = os.environ.get("RECOMMEND_MODEL", "claude-haiku-4-5")
# How many catalog candidates to show the model (enough variety, small prompt).
CANDIDATE_POOL_SIZE = 36


def _recommend_max_tokens(num_results: int) -> int:
    return min(1800, 500 + max(1, num_results) * 280)


def _wants_deep_catalog(prompt: str) -> bool:
    p = prompt.lower()
    needles = (
        "deep in the catalog", "skip anything overplayed", "underrated",
        "overlooked", "dozens of shows", "hundreds of shows",
        "seasoned deadhead", "serious collectors", "genuinely special",
        "dig into the catalog", "something obscure", "hidden gem",
    )
    return any(n in p for n in needles)


def _wants_gateway(prompt: str) -> bool:
    p = prompt.lower()
    return any(n in p for n in (
        "never heard", "brand new", "gateway", "barely heard", "casual listener",
    ))


def _select_candidates(prompt: str, era: Optional[str], pool_size: int = CANDIDATE_POOL_SIZE) -> list[dict]:
    """Pick a diverse slice of the full show DB for the model to choose from."""
    shows = list(SHOW_CATALOG)
    if era:
        era_l = era.lower()
        filtered = [
            s for s in shows
            if era_l in (s.get("era") or "").lower() or era_l in (s.get("tags") or "")
        ]
        # Also match year ranges like "1977" against the date.
        year_hits = [
            s for s in shows
            if era_l[:4].isdigit() and s["date"].startswith(era_l[:4])
        ]
        shows = filtered or year_hits or shows

    deep = _wants_deep_catalog(prompt)
    gateway = _wants_gateway(prompt)

    if deep:
        preferred = [s for s in shows if s.get("depth") == "deep"]
        secondary = [s for s in shows if s.get("depth") == "classic"]
        # Almost no gateway shows for deep-catalog requests.
        shows = preferred + secondary
    elif gateway:
        preferred = [s for s in shows if s.get("depth") in ("gateway", "classic")]
        secondary = [s for s in shows if s.get("depth") == "deep"]
        shows = preferred + secondary[: max(0, pool_size // 3)]

    if len(shows) <= pool_size:
        random.shuffle(shows)
        return shows

    # Uniform sample across the (possibly depth-filtered) full era — with 2k+
    # shows we don't want to only ever see the first half of the list.
    return random.sample(shows, pool_size)


def _format_candidates(candidates: list[dict]) -> str:
    lines = []
    for s in candidates:
        lines.append(f"- {s['date']} | {s['venue']} | {s['era']} | depth={s['depth']}")
    return "\n".join(lines)


def _ground_recommendations(data: dict, want: int) -> dict:
    """Keep only catalog dates and fill venue/era from the catalog."""
    kept = []
    seen = set()
    for rec in data.get("recommendations", []):
        date = (rec.get("date") or "").strip()
        if date not in CATALOG_DATES or date in seen:
            continue
        seen.add(date)
        catalog = SHOWS_BY_DATE[date]
        rec["date"] = date
        rec["venue"] = catalog["venue"]
        rec["era"] = catalog["era"]
        # Prefer a real archive.org id from the DB when we have one.
        rec["archive_org_id"] = catalog.get("archive_org_id") or None
        # Catalog membership is proof the date is real; setlist attach is best-effort.
        rec["date_verified"] = True
        kept.append(rec)
        if len(kept) >= want:
            break

    # Fan out setlist lookups (SQLite-cached after the first hit).
    if kept:
        def _attach(rec: dict) -> None:
            _, setlist = lookup_setlist(rec["date"])
            rec["setlist"] = setlist

        with ThreadPoolExecutor(max_workers=min(8, len(kept))) as pool:
            list(pool.map(_attach, kept))

    for i, rec in enumerate(kept, start=1):
        rec["rank"] = i
    data["recommendations"] = kept
    return data

# ── Request Model ─────────────────────────────────────────────────────────────
class VibeRequest(BaseModel):
    prompt: str
    era: Optional[str] = None         # e.g. "1972-1974", "1977", "1980s"
    mood: Optional[str] = None        # e.g. "dark and psychedelic", "upbeat and fun"
    num_results: int = 3              # 3–5 recommendations

class FeedbackRequest(BaseModel):
    date: Optional[str] = None        # show date the user voted on
    venue: Optional[str] = None
    is_match: bool                    # True = 👍, False = 👎

class SubscribeRequest(BaseModel):
    email: str

# Deliberately loose — real validation happens when the first email bounces.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": "1.0.0",
        "shows_catalog_size": len(SHOW_CATALOG),
        "shows_db": SHOWS_DB_PATH if os.path.exists(SHOWS_DB_PATH) else None,
    }


@app.get("/history/today")
@limiter.limit("30/minute")
def history_today(
    request: Request,
    month: Optional[int] = None,
    day: Optional[int] = None,
):
    """Return the best Dead shows for today's calendar date (cached in SQLite)."""
    now = datetime.now()
    m = month if month is not None else now.month
    d = day if day is not None else now.day
    if not (1 <= m <= 12 and 1 <= d <= 31):
        raise HTTPException(400, "Invalid month or day")
    return _get_or_generate_history(m, d)


@app.post("/recommend")
@limiter.limit("10/minute")
def recommend(request: Request, payload: VibeRequest):
    if not payload.prompt.strip():
        raise HTTPException(400, "Prompt cannot be empty")
    if len(payload.prompt) > MAX_PROMPT_CHARS:
        raise HTTPException(413, f"Prompt too long (max {MAX_PROMPT_CHARS} characters)")

    want = max(3, min(5, payload.num_results))

    # Cache key — stable for identical vibe/era/count (candidates are resampled
    # each miss, so the model still sees variety across cache TTLs).
    cache_key = (
        f'Vibe request: "{payload.prompt}"\n'
        + (f"Preferred era: {payload.era}\n" if payload.era else "")
        + (f"Mood refinement: {payload.mood}\n" if payload.mood else "")
        + f"Number of recommendations requested: {want}"
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        log.info(f"Cache hit: {payload.prompt[:80]}...")
        return cached

    candidates = _select_candidates(payload.prompt, payload.era)
    if len(candidates) < want:
        raise HTTPException(500, "Show catalog too small for this filter — try another era")

    parts = [f'Vibe request: "{payload.prompt}"']
    if payload.era:
        parts.append(f"Preferred era: {payload.era}")
    if payload.mood:
        parts.append(f"Mood refinement: {payload.mood}")
    if _wants_deep_catalog(payload.prompt):
        parts.append(
            "The listener wants deep catalog / underrated shows. "
            "Prefer depth=deep candidates; avoid gateway classics."
        )
    parts.append(f"Number of recommendations requested: {want}")
    parts.append("Pick ONLY from this candidate list (date | venue | era | depth):")
    parts.append(_format_candidates(candidates))
    user_message = "\n".join(parts)

    log.info(
        f"Vibe request ({RECOMMEND_MODEL}, {len(candidates)} candidates"
        f"{', deep' if _wants_deep_catalog(payload.prompt) else ''}): "
        f"{payload.prompt[:80]}..."
    )
    t0 = time.time()
    data = _call_claude(
        SYSTEM_PROMPT,
        user_message,
        model=RECOMMEND_MODEL,
        max_tokens=_recommend_max_tokens(want),
    )
    t_claude = time.time() - t0

    t1 = time.time()
    # Constrain to catalog dates (drops any invented ones) and attach setlists
    # in parallel for the kept shows only.
    data = _ground_recommendations(data, want)

    # If the model somehow returned fewer than wanted, top up from candidates
    # that weren't already chosen — still no second LLM call.
    have = {r["date"] for r in data.get("recommendations", [])}
    if len(have) < want:
        fillers = []
        for s in candidates:
            if s["date"] in have:
                continue
            fillers.append(s)
            have.add(s["date"])
            if len(have) >= want:
                break

        def _fill(s: dict) -> dict:
            _, setlist = lookup_setlist(s["date"])
            return {
                "rank": 0,
                "date": s["date"],
                "venue": s["venue"],
                "era": s["era"],
                "vibe_match": 70,
                "pitch": (
                    f"A strong {s['era'].split('(')[0].strip()} show from the "
                    f"catalog that fits this lane."
                ),
                "key_moments": ["Check the setlist below for the peaks"],
                "recording_type": "Soundboard",
                "recording_quality": "See archive.org for available transfers",
                "caveats": None,
                "setlist": setlist,
                "date_verified": True,
                "archive_org_id": s.get("archive_org_id"),
            }

        with ThreadPoolExecutor(max_workers=min(8, len(fillers))) as pool:
            data["recommendations"].extend(pool.map(_fill, fillers))
        for i, rec in enumerate(data["recommendations"][:want], start=1):
            rec["rank"] = i
        data["recommendations"] = data["recommendations"][:want]

    t_enrich = time.time() - t1

    log.info(
        f"Recommend timing: model={RECOMMEND_MODEL} claude={t_claude:.1f}s "
        f"enrich={t_enrich:.1f}s total={t_claude + t_enrich:.1f}s "
        f"results={len(data.get('recommendations', []))}"
    )

    _cache_set(cache_key, data)
    return data

@app.post("/subscribe")
@limiter.limit("5/minute")
def subscribe(request: Request, payload: SubscribeRequest):
    """Add an email to the 'Today in Dead History' newsletter list."""
    email = payload.email.strip().lower()
    if len(email) > 254 or not EMAIL_RE.match(email):
        raise HTTPException(400, "That doesn't look like a valid email address")
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO subscribers (email) VALUES (?) ON CONFLICT(email) DO NOTHING",
                (email,),
            )
    except Exception as e:
        log.error(f"Failed to store subscriber: {e}")
        raise HTTPException(500, "Could not save your signup — please try again")
    return {"status": "ok"}


@app.post("/feedback")
@limiter.limit("30/minute")
def feedback(request: Request, payload: FeedbackRequest):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO feedback (show_date, venue, is_match) VALUES (?, ?, ?)",
                (payload.date, payload.venue, 1 if payload.is_match else 0),
            )
    except Exception as e:
        log.error(f"Failed to store feedback: {e}")
        raise HTTPException(500, "Could not record feedback")
    return {"status": "ok"}

# ── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
