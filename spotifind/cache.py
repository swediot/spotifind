"""A SQLite cache that makes the second run nearly free.

This is a rate-limiting feature as much as a speed one. 1,400 books at one
request a second is about 25 minutes of talking to Spotify; doing that every
time you want to look at the list would be rude. With the cache, a re-run
only asks about books whose answer has gone stale, and a run that was
aborted (by you, or by the limiter deciding Spotify had had enough) picks up
exactly where it stopped.

Misses expire faster than hits, deliberately: Spotify's audiobook catalogue
grows, so "not there" is the answer most worth re-asking.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .matching import Candidate, Match

SCHEMA = """
CREATE TABLE IF NOT EXISTS lookups (
    book_key        TEXT NOT NULL,
    market          TEXT NOT NULL DEFAULT '',
    title           TEXT NOT NULL,
    authors         TEXT NOT NULL DEFAULT '',
    query           TEXT NOT NULL DEFAULT '',
    checked_at      TEXT NOT NULL,
    confidence      TEXT NOT NULL,
    title_score     REAL NOT NULL DEFAULT 0,
    author_matched  INTEGER NOT NULL DEFAULT 0,
    spotify_id      TEXT NOT NULL DEFAULT '',
    spotify_name    TEXT NOT NULL DEFAULT '',
    spotify_authors TEXT NOT NULL DEFAULT '',
    narrators       TEXT NOT NULL DEFAULT '',
    edition         TEXT NOT NULL DEFAULT '',
    chapters        INTEGER,
    url             TEXT NOT NULL DEFAULT '',
    languages       TEXT NOT NULL DEFAULT '',
    alternates      INTEGER NOT NULL DEFAULT 0,
    alt_languages   TEXT NOT NULL DEFAULT '',
    first_found_at  TEXT NOT NULL DEFAULT '',
    candidates      TEXT NOT NULL DEFAULT '[]',
    -- Keyed by market as well as book: checking the same list against the UK
    -- and US catalogues must not have the two overwrite each other.
    PRIMARY KEY (book_key, market)
);
CREATE INDEX IF NOT EXISTS lookups_conf ON lookups (confidence);

-- What this tool put in the library, so it can take it back out again.
-- Adding 200 audiobooks by hand is tedious; removing 200 by hand is worse,
-- so nothing gets saved without a record of it.
CREATE TABLE IF NOT EXISTS saves (
    uri         TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    authors     TEXT NOT NULL DEFAULT '',
    saved_at    TEXT NOT NULL DEFAULT '',
    removed_at  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT '',
    market      TEXT NOT NULL DEFAULT '',
    mode        TEXT NOT NULL DEFAULT '',
    csv_path    TEXT NOT NULL DEFAULT '',
    books       INTEGER NOT NULL DEFAULT 0,
    requests    INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'running',
    note        TEXT NOT NULL DEFAULT ''
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class CachedLookup:
    book_key: str
    confidence: str
    checked_at: str
    match: Match
    first_found_at: str = ""

    @property
    def found(self) -> bool:
        return self.match.found


class Cache:
    def __init__(self, path: str | Path, *, hit_ttl_days: int = 90, miss_ttl_days: int = 14) -> None:
        self.path = str(path)
        self.hit_ttl = timedelta(days=hit_ttl_days)
        self.miss_ttl = timedelta(days=miss_ttl_days)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Add columns a database made by an earlier version is missing.

        `CREATE TABLE IF NOT EXISTS` does nothing to an existing table, so
        new columns have to be added by hand or an upgrade crashes on the
        first read.
        """
        have = {row["name"] for row in self.conn.execute("PRAGMA table_info(lookups)")}
        added = [
            ("languages", "TEXT NOT NULL DEFAULT ''"),
            ("alternates", "INTEGER NOT NULL DEFAULT 0"),
            ("alt_languages", "TEXT NOT NULL DEFAULT ''"),
        ]
        for name, decl in added:
            if name not in have:
                self.conn.execute(f"ALTER TABLE lookups ADD COLUMN {name} {decl}")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Cache":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- reading -----------------------------------------------------------

    def get(self, book_key: str, market: str) -> CachedLookup | None:
        row = self.conn.execute(
            "SELECT * FROM lookups WHERE book_key = ? AND market = ?",
            (book_key, market),
        ).fetchone()
        return _row_to_lookup(row) if row else None

    def is_fresh(self, entry: CachedLookup, *, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        try:
            checked = datetime.fromisoformat(entry.checked_at)
        except ValueError:
            return False
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        ttl = self.hit_ttl if entry.found else self.miss_ttl
        return now - checked < ttl

    def count_for_market(self, market: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM lookups WHERE market = ?", (market,)
        ).fetchone()
        return int(row["n"])

    def all_for_market(self, market: str) -> dict[str, CachedLookup]:
        rows = self.conn.execute("SELECT * FROM lookups WHERE market = ?", (market,))
        return {row["book_key"]: _row_to_lookup(row) for row in rows}

    # -- writing -----------------------------------------------------------

    def put(
        self,
        *,
        book_key: str,
        market: str,
        title: str,
        authors: str,
        query: str,
        match: Match,
        candidates: list[Candidate],
    ) -> bool:
        """Store a result. Returns True if this is a book newly turned up."""
        existing = self.get(book_key, market)
        was_found = bool(existing and existing.found)
        now = _now()
        first_found = ""
        if match.found:
            first_found = (existing.first_found_at if existing and existing.first_found_at else now)

        cand = match.candidate
        self.conn.execute(
            """
            INSERT INTO lookups (
                book_key, market, title, authors, query, checked_at, confidence,
                title_score, author_matched, spotify_id, spotify_name,
                spotify_authors, narrators, edition, chapters, url,
                first_found_at, candidates, languages, alternates, alt_languages
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(book_key, market) DO UPDATE SET
                market=excluded.market, title=excluded.title,
                authors=excluded.authors, query=excluded.query,
                checked_at=excluded.checked_at, confidence=excluded.confidence,
                title_score=excluded.title_score,
                author_matched=excluded.author_matched,
                spotify_id=excluded.spotify_id, spotify_name=excluded.spotify_name,
                spotify_authors=excluded.spotify_authors,
                narrators=excluded.narrators, edition=excluded.edition,
                chapters=excluded.chapters, url=excluded.url,
                first_found_at=excluded.first_found_at,
                languages=excluded.languages, alternates=excluded.alternates,
                alt_languages=excluded.alt_languages,
                candidates=excluded.candidates
            """,
            (
                book_key, market, title, authors, query, now, match.confidence,
                float(match.title_score), int(match.author_matched),
                cand.id if cand else "", cand.name if cand else "",
                "; ".join(cand.authors) if cand else "",
                "; ".join(cand.narrators) if cand else "",
                cand.edition if cand else "",
                cand.total_chapters if cand else None,
                cand.url if cand else "",
                first_found,
                json.dumps([
                    {"id": c.id, "name": c.name, "authors": c.authors,
                     "narrators": c.narrators, "url": c.url,
                     "languages": c.languages}
                    for c in candidates[:5]
                ]),
                "; ".join(cand.languages) if cand else "",
                int(match.alternates),
                "; ".join(match.alternate_languages),
            ),
        )
        self.conn.commit()
        return bool(match.found and not was_found)

    # -- what we put in the library ----------------------------------------

    def record_saves(self, items: list[tuple[str, str, str]]) -> None:
        """items: (uri, title, authors). Re-saving clears any removal mark."""
        now = _now()
        self.conn.executemany(
            """
            INSERT INTO saves (uri, title, authors, saved_at, removed_at)
            VALUES (?,?,?,?,'')
            ON CONFLICT(uri) DO UPDATE SET
                saved_at=excluded.saved_at, removed_at='',
                title=excluded.title, authors=excluded.authors
            """,
            [(uri, title, authors, now) for uri, title, authors in items],
        )
        self.conn.commit()

    def record_removals(self, uris: list[str]) -> None:
        now = _now()
        self.conn.executemany(
            "UPDATE saves SET removed_at = ? WHERE uri = ?",
            [(now, uri) for uri in uris],
        )
        self.conn.commit()

    def saved_uris(self) -> list[sqlite3.Row]:
        """Everything this tool saved and has not since removed."""
        return self.conn.execute(
            "SELECT * FROM saves WHERE removed_at = '' ORDER BY saved_at, title"
        ).fetchall()

    def already_saved(self, uri: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM saves WHERE uri = ? AND removed_at = ''", (uri,)
        ).fetchone()
        return row is not None

    # -- run bookkeeping ---------------------------------------------------

    def start_run(self, *, market: str, mode: str, csv_path: str, books: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at, market, mode, csv_path, books) VALUES (?,?,?,?,?)",
            (_now(), market, mode, csv_path, books),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, *, status: str, requests: int, note: str = "") -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=?, status=?, requests=?, note=? WHERE id=?",
            (_now(), status, requests, note[:500], run_id),
        )
        self.conn.commit()

    def previous_runs(self, limit: int = 5) -> list[sqlite3.Row]:
        with closing(self.conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        )) as cur:
            return cur.fetchall()


def _get(row: sqlite3.Row, name: str):
    """Read a column that a database from an older version may not have."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


def _row_to_lookup(row: sqlite3.Row) -> CachedLookup:
    cand = None
    if row["spotify_id"]:
        cand = Candidate(
            id=row["spotify_id"],
            name=row["spotify_name"],
            authors=[a for a in (row["spotify_authors"] or "").split("; ") if a],
            narrators=[n for n in (row["narrators"] or "").split("; ") if n],
            edition=row["edition"] or "",
            total_chapters=row["chapters"],
            url=row["url"] or "",
            languages=[c for c in (_get(row, "languages") or "").split("; ") if c],
        )
    match = Match(
        candidate=cand,
        confidence=row["confidence"],
        title_score=float(row["title_score"] or 0),
        author_matched=bool(row["author_matched"]),
        alternates=int(_get(row, "alternates") or 0),
        alternate_languages=[c for c in (_get(row, "alt_languages") or "").split("; ") if c],
    )
    return CachedLookup(
        book_key=row["book_key"],
        confidence=row["confidence"],
        checked_at=row["checked_at"],
        match=match,
        first_found_at=row["first_found_at"] or "",
    )
