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
import json
import time
import sqlite3
import logging
import threading
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

# ── Anthropic Client ──────────────────────────────────────────────────────────
client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# ── Setlist.fm ────────────────────────────────────────────────────────────────
SETLIST_FM_KEY = os.environ.get("SETLIST_FM_API_KEY", "")

# Guard against oversized prompts blowing up token usage / cost.
MAX_PROMPT_CHARS = 2000

def fetch_setlist(date_str: str) -> dict | None:
    """Fetch real setlist from setlist.fm for a given YYYY-MM-DD date."""
    if not SETLIST_FM_KEY or not date_str:
        return None
    try:
        # Convert YYYY-MM-DD → DD-MM-YYYY (setlist.fm format)
        parts = date_str.split("-")
        if len(parts) != 3:
            return None
        sfm_date = f"{parts[2]}-{parts[1]}-{parts[0]}"

        resp = requests.get(
            "https://api.setlist.fm/rest/1.0/search/setlists",
            params={"artistName": "Grateful Dead", "date": sfm_date},
            headers={"x-api-key": SETLIST_FM_KEY, "Accept": "application/json"},
            timeout=5,
        )
        if resp.status_code != 200:
            return None

        data = resp.json()
        setlists = data.get("setlist", [])
        if not setlists:
            return None

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

        return {
            "sets": result,
            "venue": f"{venue_name}, {city}".strip(", "),
            "setlist_url": sl.get("url", ""),
        }
    except Exception as e:
        log.warning(f"Setlist.fm lookup failed for {date_str}: {e}")
        return None

def archive_id_exists(archive_id: str) -> bool:
    """Confirm an archive.org identifier resolves to a real item.

    Claude can hallucinate IDs, producing dead 'Listen' links. We fail OPEN
    (return True) on network errors so a transient archive.org hiccup doesn't
    strip a valid link.
    """
    if not archive_id:
        return False
    try:
        resp = requests.get(f"https://archive.org/metadata/{archive_id}", timeout=4)
        if resp.status_code != 200:
            return False
        return bool(resp.json().get("metadata"))
    except Exception as e:
        log.warning(f"Archive validation failed for {archive_id}: {e}")
        return True

def _enrich_recommendation(rec: dict) -> None:
    """Attach a real setlist and validate the archive.org id for one rec.

    Mutates only the passed-in dict, so it is safe to run concurrently across
    recommendations. The two network calls are the slow part of a /recommend
    response after the model finishes.
    """
    date = rec.get("date")
    if date:
        rec["setlist"] = fetch_setlist(date)  # None if not found — frontend handles gracefully
    # Drop hallucinated archive IDs so the frontend falls back to a search URL
    aid = rec.get("archive_org_id")
    if aid and not archive_id_exists(aid):
        log.info(f"Dropping invalid archive id: {aid}")
        rec["archive_org_id"] = None

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Dead Vibe Matcher", version="1.0.0")

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
# Identical requests (notably "Today in Dead History", which is the same for
# everyone on a given day) reuse a cached result instead of re-calling Claude.
CACHE_TTL_SECONDS = 6 * 60 * 60   # 6 hours
CACHE_MAX_ENTRIES = 500
_cache: dict[str, tuple[float, dict]] = {}

def _cache_get(key: str) -> dict | None:
    entry = _cache.get(key)
    if not entry:
        return None
    ts, value = entry
    if time.time() - ts > CACHE_TTL_SECONDS:
        _cache.pop(key, None)
        return None
    return value

def _cache_set(key: str, value: dict) -> None:
    if len(_cache) >= CACHE_MAX_ENTRIES:
        # Drop the oldest entry to bound memory.
        oldest = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest, None)
    _cache[key] = (time.time(), value)

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
HISTORY_MAX_TOKENS = 2048
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


HISTORY_SYSTEM_PROMPT = """You are a Grateful Dead historian. Given a calendar month and day, return the best Grateful Dead live performances that occurred on that exact date in any year between 1965 and 1995.

Return ONLY valid JSON — no markdown fences, no commentary. Use this structure:

{
  "vibe_interpretation": "1-2 sentences on what makes this date notable in Dead history",
  "recommendations": [
    {
      "rank": 1,
      "date": "1977-05-08",
      "venue": "Barton Hall, Cornell University, Ithaca, NY",
      "era": "Hiatus & Return (1975–1977)",
      "vibe_match": 92,
      "pitch": "2-3 sentences. Lead with a specific song, jam, or setlist fact. No vague praise.",
      "key_moments": ["Three specific moments — name songs, name what happened"],
      "archive_org_id": "gd1977-05-08.bman-mx.fixed.104013.flac2496",
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
    _enrich_all(data.get("recommendations", []))
    return data


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
SYSTEM_PROMPT = """You are the Dead Vibe Matcher — an expert guide to the Grateful Dead's live catalog spanning 1965 to 1995. You have encyclopedic knowledge of every era, lineup, venue, and the musical character of the band's ~2,300 live performances.

Your job: a user describes a vibe, mood, feeling, moment, or scenario — and you match them to the specific Grateful Dead shows that best deliver that experience. You are not recommending studio albums. You are recommending specific live performances — real dates, real venues, real setlists.

## YOUR KNOWLEDGE DOMAINS

### Eras & Lineups
- **Primal Dead (1965–1968)**: Raw, electric, garage psychedelia. Pigpen's blues grit. Short, explosive sets. Acid Tests and the Haight.
- **Psychedelic Peak (1968–1970)**: The live Dead emerges. Extended Dark Stars, St. Stephens, long-form improvisation. Cosmic, searching, uncharted.
- **Americana Pivot (1970–1971)**: Workingman's Dead and American Beauty reshape the repertoire. Country, folk, and gospel threads. Pigpen's last great stretch.
- **Jazz Fusion Zenith (1972–1974)**: Keith Godchaux on keys. The Europe '72 tour. Fluid Playin' in the Band explorations, soaring Eyes of the World. The Wall of Sound in '74.
- **Hiatus & Return (1975–1977)**: The '75 hiatus. The '76 comeback. Then 1977 — the Cornell '77 year — sweet spot of composed power and exploratory freedom. Terrapin Station arrives.
- **Shakedown Street Era (1978–1979)**: Disco and funk influences. The Egypt shows. Shakedown Street and Fire on the Mountain become staples.
- **Brent Mydland Era (1979–1990)**: Brent's keyboards add grit, soul, and emotional edge. Early '80s are underrated and aggressive. Late '80s Brent shows can be transcendent.
- **Final Chapter (1990–1995)**: Vince Welnick and Bruce Hornsby rotate on keys. Capable of breathtaking moments. The 1995 run is bittersweet.

### Musical Dimensions You Evaluate
- **Jam Depth**: How far out does the improvisation go? Extended and exploratory, or tight and composed?
- **Energy Level**: Barn-burner or slow-build meditation? Is the crowd feeding the band?
- **Mood Spectrum**: Dark/psychedelic ↔ Light/joyful. Melancholy ↔ Euphoric. Cosmic ↔ Earthy.
- **Setlist Architecture**: How does the show flow? The ">" (segue) symbol matters — a Scarlet > Fire is different from a standard Help > Slip > Franklin's.
- **Song Selection**: Rare songs, bustouts, unusual pairings signal something special.
- **Recording Quality**: Soundboard (SBD) vs. audience (AUD) recordings.

### Vibe Translation
You are fluent in translating non-Dead language into Dead language:
- "Something chill for a rainy afternoon" → Mellow '77 shows, acoustic sets, Stella Blue > Morning Dew closers
- "I want my face melted" → '69 Dark Stars, '74 Wall of Sound Playin's, '89 Brent-fueled second sets
- "Road trip energy" → Upbeat '72–'73 shows, Truckin' > Smokestack Lightning jams, '77 Estimated > Eyes combos
- "I'm going through something heavy" → Wharf Rat performances, late Brent era emotional peaks, '72 He's Gone
- "Party music" → Shakedown Streets, '76–'77 Dancing in the Streets, '81 upbeat openers
- "I've never listened to the Dead before" → Gateway shows: Cornell 5/8/77, Veneta 8/27/72, Europe '72 highlights
- "Deep space exploration" → '68–'69 Dark Stars, '74 Seastones/space segments

## YOUR RESPONSE FORMAT

Return ONLY valid JSON — no markdown fences, no commentary before or after. Use this exact structure:

{
  "vibe_interpretation": "Your 1-2 sentence restatement of what the user is looking for, in Dead terms",
  "recommendations": [
    {
      "rank": 1,
      "date": "1977-05-08",
      "venue": "Barton Hall, Cornell University, Ithaca, NY",
      "era": "Hiatus & Return (1975–1977)",
      "vibe_match": 92,
      "pitch": "2-4 sentences. Lead with the most specific fact about this show — a song that ran long, a pairing that only happened once, a moment the band found something and chased it. Follow with a short punchy line about why that matters for this listener's vibe. No vague praise words — 'transcendent,' 'remarkable,' 'devastating,' 'stunning' are banned. Say what actually happened. Vary sentence length. One long, one short. Never the same rhythm twice in a row.",
      "key_moments": [
        "Be specific and uneven. One moment might be a single sentence, another might need two. Don't make them all the same length. Name the song, name what it did, skip the adjectives. 'Scarlet ran 11 minutes and never resolved the way you expect it to' beats 'a stunning Scarlet > Fire transition.' Three moments per show — no more."
      ],
      "archive_org_id": "gd1977-05-08.bman-mx.fixed.104013.flac2496",
      "recording_type": "Soundboard",
      "recording_quality": "Excellent — Betty Board, one of the best-sounding Dead tapes in existence",
      "caveats": null
    }
  ],
  "deeper_cut": "If you like these, you should also explore [brief suggestion for further listening]"
}

## RULES
1. Always recommend real shows with real dates. Never fabricate a show or setlist. If unsure of a specific detail, flag it.
2. Recommend 3–5 shows per query, ranked by vibe match. Lead with the strongest match.
3. Don't default to the obvious. Cornell '77, Veneta '72, and Europe '72 are great, but dig deeper when appropriate.
4. Respect the eras. Don't recommend a '89 show when someone explicitly wants early-'70s energy unless you explain why.
5. Be honest about weak spots — rough audio, weak first sets, divisive elements. Flag them.
6. Write like a knowledgeable friend who has heard this show 20 times, not a reviewer performing authority. Lead with what actually happened — a specific song, a specific moment, a specific quirk of this recording. Never use: transcendent, remarkable, devastating, crucial, noteworthy, stunning, or beautiful. Say what the music did, not how it made someone feel. Admit caveats plainly — "the first set is skippable" beats "the second set is where this show truly shines." Short sentences after long ones. Let the last line land without wrapping it up.
7. Return ONLY valid JSON. No markdown, no preamble, no explanation outside the JSON structure.
"""

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

# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "version": "1.0.0"}


@app.on_event("startup")
def _startup_prewarm_history() -> None:
    threading.Thread(target=_prewarm_today_history, daemon=True).start()


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

    # Build the user message with any filters the user chose
    parts = [f'Vibe request: "{payload.prompt}"']
    if payload.era:
        parts.append(f"Preferred era: {payload.era}")
    if payload.mood:
        parts.append(f"Mood refinement: {payload.mood}")
    parts.append(f"Number of recommendations requested: {payload.num_results}")
    user_message = "\n".join(parts)

    # Serve identical requests from cache (saves a Claude + setlist.fm round trip)
    cache_key = user_message
    cached = _cache_get(cache_key)
    if cached is not None:
        log.info(f"Cache hit: {payload.prompt[:80]}...")
        return cached

    log.info(f"Vibe request: {payload.prompt[:80]}...")

    data = _call_claude(
        SYSTEM_PROMPT,
        user_message,
        model="claude-opus-4-6",
        max_tokens=4096,
    )

    _enrich_all(data.get("recommendations", []))
    _cache_set(cache_key, data)
    return data

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
