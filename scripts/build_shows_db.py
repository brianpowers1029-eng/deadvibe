#!/usr/bin/env python3
"""
Build a local SQLite catalog of every Grateful Dead show (1965–1995).

Primary source: Deadgraph shows.jsonl (Hugging Face) — ~2,336 concerts.
Enrichment: archive.org venue/coverage fields + depth/tags from shows_catalog.py.

Usage:
    python3 scripts/build_shows_db.py
    python3 scripts/build_shows_db.py --out shows.db
    python3 scripts/build_shows_db.py --skip-archive   # Deadgraph + curated only

The resulting DB is what /recommend queries at runtime (see SHOWS_DB_PATH).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shows_catalog import SHOW_CATALOG  # noqa: E402

DEADGRAPH_URL = (
    "https://huggingface.co/datasets/myronkoch/deadgraph/"
    "resolve/main/data/shows.jsonl"
)
ARCHIVE_SEARCH_URL = "https://archive.org/advancedsearch.php"

ERA_RANGES = [
    ((1965, 1, 1), (1968, 6, 30), "Primal Dead (1965–1968)"),
    ((1968, 7, 1), (1970, 6, 30), "Psychedelic Peak (1968–1970)"),
    ((1970, 7, 1), (1971, 12, 31), "Americana Pivot (1970–1971)"),
    ((1972, 1, 1), (1974, 12, 31), "Jazz Fusion Zenith (1972–1974)"),
    ((1975, 1, 1), (1977, 12, 31), "Hiatus & Return (1975–1977)"),
    ((1978, 1, 1), (1979, 6, 30), "Shakedown Street Era (1978–1979)"),
    ((1979, 7, 1), (1990, 7, 31), "Brent Mydland Era (1979–1990)"),
    ((1990, 8, 1), (1995, 12, 31), "Final Chapter (1990–1995)"),
]


def era_for_date(date_str: str) -> str:
    try:
        y, m, d = (int(x) for x in date_str.split("-"))
    except ValueError:
        return "Unknown"
    for (y1, m1, d1), (y2, m2, d2), label in ERA_RANGES:
        if (y, m, d) < (y1, m1, d1):
            continue
        if (y, m, d) > (y2, m2, d2):
            continue
        return label
    if y < 1968:
        return "Primal Dead (1965–1968)"
    return "Final Chapter (1990–1995)"


def download_deadgraph(cache_path: Path) -> list[dict]:
    if cache_path.exists() and cache_path.stat().st_size > 0:
        print(f"Using cached Deadgraph file: {cache_path}")
    else:
        print(f"Downloading Deadgraph shows from Hugging Face…")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(DEADGRAPH_URL, timeout=120) as resp:
            cache_path.write_bytes(resp.read())
        print(f"  wrote {cache_path} ({cache_path.stat().st_size:,} bytes)")

    rows = []
    with cache_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    print(f"  loaded {len(rows)} Deadgraph shows")
    return rows


def _merge_archive_doc(by_date: dict[str, dict], doc: dict) -> None:
    raw_date = (doc.get("date") or "")[:10]
    if len(raw_date) != 10 or raw_date < "1965-01-01" or raw_date > "1995-12-31":
        return
    venue = (doc.get("venue") or "").strip()
    city = (doc.get("coverage") or "").strip()
    ident = doc.get("identifier") or ""
    existing = by_date.get(raw_date)
    if not existing:
        by_date[raw_date] = {"venue": venue, "city": city, "archive_org_id": ident}
        return
    if venue and not existing["venue"]:
        existing["venue"] = venue
    if city and not existing["city"]:
        existing["city"] = city
    if ident and not existing["archive_org_id"]:
        existing["archive_org_id"] = ident


def fetch_archive_venue_map() -> dict[str, dict]:
    """date -> {venue, city, archive_org_id} from archive.org etree collection.

    archive.org caps deep paging at 10k results, so we query year-by-year.
    """
    print("Fetching archive.org venue/coverage map (by year)…")
    by_date: dict[str, dict] = {}
    for year in range(1965, 1996):
        q = (
            f"collection:GratefulDead AND mediatype:etree "
            f"AND date:[{year}-01-01 TO {year}-12-31]"
        )
        params = (
            f"?q={urllib.parse.quote(q)}"
            f"&fl[]=date&fl[]=venue&fl[]=coverage&fl[]=identifier"
            f"&rows=10000&output=json"
        )
        with urllib.request.urlopen(ARCHIVE_SEARCH_URL + params, timeout=120) as resp:
            payload = json.loads(resp.read().decode())
        if "error" in payload:
            raise RuntimeError(f"archive.org error for {year}: {payload['error']}")
        docs = payload["response"].get("docs", [])
        for doc in docs:
            _merge_archive_doc(by_date, doc)
        print(f"  {year}: {len(docs)} items (running unique dates: {len(by_date)})")
    print(f"  unique show dates from archive.org: {len(by_date)}")
    return by_date


def curated_overlay() -> dict[str, dict]:
    return {s["date"]: s for s in SHOW_CATALOG}


def format_venue(name: str, city: str) -> str:
    name = (name or "").strip()
    city = (city or "").strip()
    if name and city and city.lower() not in name.lower():
        return f"{name}, {city}"
    return name or city or "Unknown venue"


def build_rows(deadgraph: list[dict], archive_map: dict[str, dict], curated: dict[str, dict]) -> list[dict]:
    rows_by_date: dict[str, dict] = {}

    for item in deadgraph:
        date = (item.get("date") or "")[:10]
        if len(date) != 10 or date < "1965-01-01" or date > "1995-12-31":
            continue
        venue_obj = item.get("venue") or {}
        venue_name = venue_obj.get("name") or ""
        if venue_name.lower() in ("various", "unknown", ""):
            venue_name = ""

        sources = item.get("sources") or []
        archive_id = None
        for src in sources:
            if src.get("kind") == "ia_item" and src.get("identifier"):
                archive_id = src["identifier"]
                break

        arch = archive_map.get(date, {})
        if not venue_name:
            venue_name = arch.get("venue") or ""
        city = arch.get("city") or ""
        if not archive_id:
            archive_id = arch.get("archive_org_id")

        cur = curated.get(date)
        depth = cur["depth"] if cur else "deep"
        tags = cur["tags"] if cur else ""
        # Prefer curated venue string when present (includes city).
        if cur and cur.get("venue"):
            venue = cur["venue"]
        else:
            venue = format_venue(venue_name, city)

        rows_by_date[date] = {
            "date": date,
            "venue": venue,
            "city": city,
            "era": cur["era"] if cur and cur.get("era") else era_for_date(date),
            "depth": depth,
            "tags": tags,
            "archive_org_id": archive_id,
            "source": "deadgraph",
        }

    # Ensure every curated show is present even if Deadgraph missed it.
    for date, cur in curated.items():
        if date in rows_by_date:
            continue
        arch = archive_map.get(date, {})
        rows_by_date[date] = {
            "date": date,
            "venue": cur["venue"],
            "city": arch.get("city") or "",
            "era": cur["era"],
            "depth": cur["depth"],
            "tags": cur["tags"],
            "archive_org_id": arch.get("archive_org_id"),
            "source": "curated",
        }

    return [rows_by_date[d] for d in sorted(rows_by_date)]


def write_db(rows: list[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    with sqlite3.connect(out_path) as conn:
        conn.execute(
            """
            CREATE TABLE shows (
                date           TEXT PRIMARY KEY,
                venue          TEXT NOT NULL,
                city           TEXT,
                era            TEXT NOT NULL,
                depth          TEXT NOT NULL DEFAULT 'deep',
                tags           TEXT NOT NULL DEFAULT '',
                archive_org_id TEXT,
                source         TEXT NOT NULL DEFAULT 'deadgraph'
            )
            """
        )
        conn.execute("CREATE INDEX idx_shows_era ON shows(era)")
        conn.execute("CREATE INDEX idx_shows_depth ON shows(depth)")
        conn.executemany(
            """
            INSERT INTO shows (date, venue, city, era, depth, tags, archive_org_id, source)
            VALUES (:date, :venue, :city, :era, :depth, :tags, :archive_org_id, :source)
            """,
            rows,
        )
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM shows").fetchone()[0]
        depths = dict(conn.execute("SELECT depth, COUNT(*) FROM shows GROUP BY depth").fetchall())
    print(f"Wrote {count} shows → {out_path}")
    print(f"  depths: {depths}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "shows.db",
        help="Output SQLite path (default: ./shows.db)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=ROOT / "data" / "cache",
        help="Where to cache downloaded Deadgraph JSONL",
    )
    parser.add_argument(
        "--skip-archive",
        action="store_true",
        help="Skip archive.org venue enrichment (faster, less city data)",
    )
    args = parser.parse_args()

    deadgraph = download_deadgraph(args.cache_dir / "deadgraph_shows.jsonl")
    archive_map = {} if args.skip_archive else fetch_archive_venue_map()
    curated = curated_overlay()
    rows = build_rows(deadgraph, archive_map, curated)
    write_db(rows, args.out)

    # Sanity: Cornell + Veneta present
    with sqlite3.connect(args.out) as conn:
        for d in ("1977-05-08", "1972-08-27"):
            row = conn.execute("SELECT venue, depth FROM shows WHERE date=?", (d,)).fetchone()
            print(f"  check {d}: {row}")


if __name__ == "__main__":
    main()
