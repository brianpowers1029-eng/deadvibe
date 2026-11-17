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
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from owner_picks import OWNER_PICKS
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

# Personal library dates sent by the browser (heard / favorited shows).
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_LIBRARY_DATES = 200
MAX_LIBRARY_PROMPT_DATES = 15

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
7. If the user names a date that is not in the candidate list, do not mention that date, do not apologize for it, and do not substitute a different night at the same venue. Restate the mood and recommend only from the list.
"""

RECOMMEND_MODEL = os.environ.get("RECOMMEND_MODEL", "claude-haiku-4-5")
# How many catalog candidates to show the model. Smaller than the old random
# 36 because these are already ranked for the vibe, which keeps the prompt
# short and the match on-mood.
CANDIDATE_POOL_SIZE = 16
# Don't let one calendar year crowd the shortlist.
MAX_PER_YEAR = 3

# Prompt phrases that mean "show the owner's list", not "run a vibe search".
# The button also sends mode=owner. These phrases cover that button's prompt
# and the older client that named three favorite dates inside a vibe request.
_OWNER_LIST_PHRASES = (
    "owner's full list",
    "owners full list",
    "personal favorites",
    "personal favorite shows",
    "owner's picks",
    "owner's favorite",
    "owner favorites",
    "all-time favorites",
    "personally curated",
    "essential shows",
    "all 25",
    "all 24",
    "the 25 favorite",
    "my 25 favorite",
)
OWNER_DATES = {pick["date"] for pick in OWNER_PICKS}

# (needles in the prompt, substring of the catalog era name)
_ERA_HINTS = (
    (("primal", "acid test", "1965", "1966", "1967"), "primal dead"),
    (("psychedelic", "dark star", "live/dead", "live dead", "1968", "1969"), "psychedelic peak"),
    (("americana", "acoustic", "workingman", "pigpen", "1970", "1971"), "americana pivot"),
    (("europe", "keith", "wall of sound", "jazzy", "playin", "1972", "1973", "1974"), "jazz fusion"),
    (("1977", "cornell", "hiatus", "terrapin", "1975", "1976"), "hiatus & return"),
    (("shakedown", "disco", "egypt", "red rocks", "1978"), "shakedown"),
    (("brent", "1980", "1981", "1982", "1983", "1984", "1985", "1986", "1987", "1988", "1989"), "brent"),
    (("hornsby", "vince", "welnick", "1990", "1991", "1992", "1993", "1994", "1995"), "final chapter"),
)

# Prompt word -> tag token stored on curated shows.
_TAG_HINTS = {
    "dark": "dark",
    "psychedelic": "psychedelic",
    "space": "dark_star",
    "acoustic": "acoustic",
    "mellow": "acoustic",
    "outdoor": "outdoor",
    "emotional": "emotional",
    "jazzy": "jazzy",
    "jazz": "jazzy",
    "brent": "brent",
    "keith": "keith",
    "pigpen": "pigpen",
    "europe": "europe72",
    "gateway": "gateway",
    "energetic": "energetic",
    "energy": "energetic",
    "cornell": "cornell",
    "veneta": "veneta",
}


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


def _clean_catalog_dates(dates: Optional[list[str]], limit: int = MAX_LIBRARY_DATES) -> list[str]:
    """Keep unique catalog dates, in the order the client sent them."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in dates or []:
        date = (raw or "").strip()
        if not DATE_RE.fullmatch(date) or date not in CATALOG_DATES or date in seen:
            continue
        seen.add(date)
        cleaned.append(date)
        if len(cleaned) >= limit:
            break
    return cleaned


def _format_known_shows(dates: list[str], limit: int = MAX_LIBRARY_PROMPT_DATES) -> str:
    bits = []
    for date in dates[:limit]:
        show = SHOWS_BY_DATE.get(date)
        if not show:
            continue
        bits.append(f"{date} ({show['era']})")
    extra = len(dates) - len(bits)
    text = ", ".join(bits)
    if extra > 0:
        text += f", and {extra} more"
    return text


def _wants_owner_list(prompt: str) -> bool:
    p = (
        (prompt or "")
        .lower()
        .replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("`", "'")
    )
    return any(phrase in p for phrase in _OWNER_LIST_PHRASES)


def _match_signals(prompt: str) -> dict:
    """Pull era, tag, year, and depth clues out of a vibe prompt."""
    p = prompt.lower()
    eras = []
    for needles, era_name in _ERA_HINTS:
        if any(n in p for n in needles):
            eras.append(era_name)
    tags = set()
    for word, tag in _TAG_HINTS.items():
        if word in p:
            tags.add(tag)
    years = set(re.findall(r"\b(19[6-9]\d)\b", p))
    if _wants_deep_catalog(prompt):
        depth = "deep"
    elif _wants_gateway(prompt):
        depth = "gateway"
    elif "classic" in p and "obscure" not in p:
        depth = "classic"
    else:
        depth = None
    return {"eras": eras, "tags": tags, "years": years, "depth": depth}


def _score_show(show: dict, signals: dict) -> int:
    """Higher means the show fits the prompt. Zero means we found no clue."""
    score = 0
    tags = set((show.get("tags") or "").split())
    era = (show.get("era") or "").lower()
    year = show["date"][:4]
    if year in signals["years"]:
        score += 8
    for era_name in signals["eras"]:
        if era_name in era:
            score += 7
    for tag in signals["tags"]:
        if tag in tags:
            score += 5
    depth = show.get("depth")
    if signals["depth"] == "deep":
        if depth == "gateway":
            score -= 8
        elif depth == "deep" and tags:
            score += 3
    elif signals["depth"] == "gateway":
        if depth == "gateway":
            score += 8
        elif depth == "classic":
            score += 4
    elif signals["depth"] == "classic":
        if depth in ("classic", "gateway"):
            score += 4
    return score


def _diversify(ranked: list[dict], pool_size: int) -> list[dict]:
    """Take the best shows, capping how many can come from one year."""
    picked = []
    per_year: dict[str, int] = {}
    overflow = []
    for show in ranked:
        year = show["date"][:4]
        if per_year.get(year, 0) >= MAX_PER_YEAR:
            overflow.append(show)
            continue
        picked.append(show)
        per_year[year] = per_year.get(year, 0) + 1
        if len(picked) >= pool_size:
            return picked
    for show in overflow:
        picked.append(show)
        if len(picked) >= pool_size:
            break
    return picked


def _stratified_sample(shows: list[dict], pool_size: int) -> list[dict]:
    """Spread a no-signal request across eras instead of one random clump."""
    by_era: dict[str, list[dict]] = {}
    for show in shows:
        by_era.setdefault(show.get("era") or "", []).append(show)
    eras = list(by_era)
    random.shuffle(eras)
    for bucket in by_era.values():
        random.shuffle(bucket)
    picked = []
    while len(picked) < pool_size and any(by_era.values()):
        for era in eras:
            bucket = by_era.get(era) or []
            if not bucket:
                continue
            picked.append(bucket.pop())
            if len(picked) >= pool_size:
                break
    return picked


def _select_candidates(
    prompt: str,
    era: Optional[str],
    pool_size: int = CANDIDATE_POOL_SIZE,
    exclude: Optional[set[str]] = None,
    min_keep: int = 3,
) -> tuple[list[dict], bool]:
    """Rank a shortlist the model is allowed to choose from.

    A random slice of 2,000 shows was why mood requests missed and why a
    request for specific favorites could not see those dates. Shows that
    match the prompt's era, year, tags, or depth are ranked first. A prompt
    with no such clues gets an even spread across eras.

    Returns the candidate list and whether already-heard dates were actually
    removed. A tiny remaining pool keeps those dates so the request still
    returns shows.
    """
    # Favorites are served only by the Personal Favorites button. A mood
    # search never sees those dates, so the model cannot swap in a favorite
    # or apologize for one that missed the shortlist.
    shows = [s for s in SHOW_CATALOG if s["date"] not in OWNER_DATES]
    if era:
        era_l = era.lower()
        filtered = [
            s for s in shows
            if era_l in (s.get("era") or "").lower() or era_l in (s.get("tags") or "")
        ]
        # Also match a bare year like "1977" against the date.
        year_hits = [
            s for s in shows
            if era_l[:4].isdigit() and s["date"].startswith(era_l[:4])
        ]
        shows = filtered or year_hits or shows

    deep = _wants_deep_catalog(prompt)
    gateway = _wants_gateway(prompt)
    if deep:
        preferred = [s for s in shows if s.get("depth") != "gateway"]
        shows = preferred or shows
    elif gateway:
        preferred = [s for s in shows if s.get("depth") in ("gateway", "classic")]
        shows = preferred or shows

    honored_exclude = False
    if exclude:
        filtered = [s for s in shows if s["date"] not in exclude]
        if len(filtered) >= min_keep:
            shows = filtered
            honored_exclude = True
    blocked = exclude if honored_exclude else set()

    signals = _match_signals(prompt)
    scored = [( _score_show(show, signals), show) for show in shows]
    best = max((score for score, _ in scored), default=0)
    if best <= 0:
        if len(shows) <= pool_size:
            random.shuffle(shows)
            return _fill_shortlist(shows, pool_size, blocked), honored_exclude
        return _stratified_sample(shows, pool_size), honored_exclude

    scored.sort(key=lambda pair: (-pair[0], pair[1]["date"]))
    ranked = [show for _, show in scored]
    if len(ranked) <= pool_size:
        return _fill_shortlist(ranked, pool_size, blocked), honored_exclude
    return _diversify(ranked, pool_size), honored_exclude


def _fill_shortlist(chosen: list[dict], pool_size: int, blocked: Optional[set[str]] = None) -> list[dict]:
    """Keep the best matches first, then other non-favorite, unheard shows.

    Dropping the owner's dates can leave a narrow year with only one or two
    remaining shows. The model still needs a full shortlist so a mood request
    can return three recommendations.
    """
    if len(chosen) >= pool_size:
        return chosen[:pool_size]
    skip = blocked or set()
    have = {show["date"] for show in chosen}
    rest = [
        show for show in SHOW_CATALOG
        if show["date"] not in have
        and show["date"] not in OWNER_DATES
        and show["date"] not in skip
    ]
    need = pool_size - len(chosen)
    if len(rest) <= need:
        return chosen + rest
    return chosen + _stratified_sample(rest, need)


def _format_candidates(candidates: list[dict]) -> str:
    lines = []
    for s in candidates:
        tags = (s.get("tags") or "").strip()
        extra = f" | {tags}" if tags else ""
        lines.append(f"- {s['date']} | {s['venue']} | {s['era']} | depth={s['depth']}{extra}")
    return "\n".join(lines)


def _owner_list_payload() -> dict:
    """The full essentials list, with no model call.

    Dates that are not in the catalog are still returned, with the venue from
    the owner's list, so a favorite cannot vanish just because the calendar
    has no row for it.
    """
    recommendations = []
    for index, pick in enumerate(OWNER_PICKS, start=1):
        show = SHOWS_BY_DATE.get(pick["date"])
        if show:
            pitch = f"One of the owner's essential shows, from the {show['era'].split('(')[0].strip()}."
            caveat = None
            venue = show["venue"]
            era = show["era"]
            archive_id = show.get("archive_org_id")
            verified = True
        else:
            pitch = (
                "On the owner's list. This exact date is not in the show catalog, "
                "so treat the venue as the owner's note rather than a confirmed bill."
            )
            caveat = "No catalog row for this date."
            venue = pick["venue"]
            era = ""
            archive_id = None
            verified = False
        recommendations.append({
            "rank": index,
            "date": pick["date"],
            "venue": venue,
            "era": era,
            "vibe_match": None,
            "pitch": pitch,
            "key_moments": [],
            "recording_type": "Catalog",
            "recording_quality": "Stream the transfer on archive.org",
            "caveats": caveat,
            "setlist": None,
            "date_verified": verified,
            "archive_org_id": archive_id,
        })
    return {
        "vibe_interpretation": (
            f"All {len(recommendations)} shows on the owner's essential list, "
            "in the order they were written down."
        ),
        "recommendations": recommendations,
        "deeper_cut": "Use Describe my mood when you want a show for tonight instead of the whole list.",
    }


def _ground_recommendations(
    data: dict,
    want: int,
    allowed: Optional[set[str]] = None,
    exclude: Optional[set[str]] = None,
) -> dict:
    """Keep only catalog dates and fill venue/era from the catalog.

    `allowed` is the shortlist we showed the model. Dates outside it are
    dropped so a mood request cannot wander back to an unrelated classic.
    `exclude` holds shows the listener has already heard.
    """
    blocked = exclude or set()
    kept = []
    seen = set()
    for rec in data.get("recommendations", []):
        date = (rec.get("date") or "").strip()
        if date not in CATALOG_DATES or date in seen or date in blocked:
            continue
        if allowed is not None and date not in allowed:
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
    prompt: str = ""
    era: Optional[str] = None         # e.g. "1972-1974", "1977", "1980s"
    mood: Optional[str] = None        # e.g. "dark and psychedelic", "upbeat and fun"
    num_results: int = 3              # 3–5 recommendations
    exclude_dates: list[str] = Field(default_factory=list)    # shows the listener has already heard
    favorite_dates: list[str] = Field(default_factory=list)   # shows they marked as favorites
    mode: Optional[str] = None        # "owner" returns the favorites list directly

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
    # The favorites button sends mode=owner. Older pages instead paste three
    # favorite dates into the prompt; those phrases take the same path. Either
    # way this returns before a model call, so a favorite cannot be dropped
    # for missing the mood shortlist.
    if (payload.mode or "").strip().lower() == "owner" or _wants_owner_list(payload.prompt):
        log.info("Owner list request — returning the full essentials list")
        return _owner_list_payload()

    if not payload.prompt.strip():
        raise HTTPException(400, "Prompt cannot be empty")
    if len(payload.prompt) > MAX_PROMPT_CHARS:
        raise HTTPException(413, f"Prompt too long (max {MAX_PROMPT_CHARS} characters)")

    want = max(3, min(5, payload.num_results))
    exclude = _clean_catalog_dates(payload.exclude_dates)
    favorites = _clean_catalog_dates(payload.favorite_dates)
    exclude_set = set(exclude)

    # Cache key — stable for identical vibe/era/count/library. Ranked
    # shortlists are deterministic, so a repeat of the same mood returns the
    # cached writeup. Empty library lists are omitted so older cached
    # responses still hit.
    cache_key = (
        f'Vibe request: "{payload.prompt}"\n'
        + (f"Preferred era: {payload.era}\n" if payload.era else "")
        + (f"Mood refinement: {payload.mood}\n" if payload.mood else "")
        + (f"Exclude: {','.join(sorted(exclude))}\n" if exclude else "")
        + (f"Favorites: {','.join(sorted(favorites))}\n" if favorites else "")
        + f"Number of recommendations requested: {want}"
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        log.info(f"Cache hit: {payload.prompt[:80]}...")
        return cached

    candidates, honored_exclude = _select_candidates(
        payload.prompt, payload.era, exclude=exclude_set, min_keep=want,
    )
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
    if favorites:
        parts.append(
            "The listener favorited these shows — lean toward the same era and "
            "energy, and do not repeat the dates: "
            + _format_known_shows(favorites)
        )
    if exclude and honored_exclude:
        parts.append(
            "The listener has already heard these shows — do not recommend them: "
            + _format_known_shows(exclude)
        )
    elif exclude:
        parts.append(
            "The listener has already heard many shows in this lane. Prefer dates "
            "they have not heard when the candidate list allows it."
        )
    parts.append(f"Number of recommendations requested: {want}")
    parts.append(
        "Pick ONLY from this candidate list, which is already ranked for the vibe "
        "(best fits first). date | venue | era | depth:"
    )
    parts.append(_format_candidates(candidates))
    user_message = "\n".join(parts)

    log.info(
        f"Vibe request ({RECOMMEND_MODEL}, {len(candidates)} candidates"
        f"{', deep' if _wants_deep_catalog(payload.prompt) else ''}"
        f"{f', exclude={len(exclude)}' if exclude else ''}"
        f"{f', favorites={len(favorites)}' if favorites else ''}"
        f"{', heard-skipped' if honored_exclude else ''}): "
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
    blocked = exclude_set if honored_exclude else set()
    data = _ground_recommendations(
        data, want, allowed={s["date"] for s in candidates}, exclude=blocked,
    )

    # If the model somehow returned fewer than wanted, top up from candidates
    # that weren't already chosen — still no second LLM call.
    have = {r["date"] for r in data.get("recommendations", [])}
    if len(have) < want:
        fillers = []
        for s in candidates:
            if s["date"] in have or s["date"] in blocked:
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
