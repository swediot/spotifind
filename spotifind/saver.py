"""Put the books you found into your Spotify library.

Reading the report CSV rather than the cache is deliberate. It means the list
of things about to be written to your account is a file you can open, sort,
and delete rows from first — and what you see there is exactly what gets
saved. A tool that writes to someone's account should be easy to audit before
it runs, not after.

Everything it saves is recorded, so `spotifind save --undo` can take it all
back out again.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .cache import Cache
from .ratelimit import RateLimitAbort
from .spotify import (
    MAX_LIBRARY_URIS,
    Forbidden,
    SpotifyClient,
    SpotifyError,
    audiobook_uri,
)

#: Report statuses eligible for saving, in confidence order.
SAVEABLE = {"on_spotify": "strong", "probably": "likely", "check": "unconfirmed"}


class ReportError(ValueError):
    pass


@dataclass
class Savable:
    title: str
    authors: str
    uri: str
    status: str

    @property
    def label(self) -> str:
        return f"{self.title} — {self.authors}" if self.authors else self.title


@dataclass
class SaveSummary:
    requested: list[Savable] = field(default_factory=list)
    saved: list[Savable] = field(default_factory=list)
    already_there: list[Savable] = field(default_factory=list)
    status: str = "ok"
    note: str = ""

    @property
    def saved_count(self) -> int:
        return len(self.saved)


def read_report(path: str | Path, *, include: Sequence[str] = ("on_spotify",)) -> list[Savable]:
    """Load the rows worth saving out of a report CSV."""
    path = Path(path)
    if not path.exists():
        raise ReportError(f"No report at {path}. Run `spotifind check` first.")

    text = path.read_text(encoding="utf-8-sig", errors="replace")
    reader = csv.DictReader(text.splitlines())
    if not reader.fieldnames:
        raise ReportError(f"{path} is empty.")

    fields = {f.strip().lower() for f in reader.fieldnames if f}
    if "status" not in fields:
        raise ReportError(
            f"{path} does not look like a spotifind report (no 'status' column). "
            "It should be the CSV that `spotifind check` wrote."
        )
    if "spotify_id" not in fields and "spotify_url" not in fields:
        raise ReportError(
            f"{path} has no spotify_id or spotify_url column — it was probably "
            "written by an older version. Re-run `spotifind check` to refresh it."
        )

    wanted = set(include)
    out: list[Savable] = []
    seen: set[str] = set()
    for row in reader:
        status = (row.get("status") or "").strip()
        if status not in wanted:
            continue
        raw = (row.get("spotify_id") or row.get("spotify_url") or "").strip()
        uri = audiobook_uri(raw)
        if not uri or uri in seen:
            continue
        seen.add(uri)
        out.append(Savable(
            title=(row.get("title") or "").strip(),
            authors=(row.get("authors") or "").strip(),
            uri=uri,
            status=status,
        ))
    return out


def _chunks(items: list, size: int) -> Iterable[list]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def save_to_library(
    items: list[Savable],
    client: SpotifyClient,
    cache: Cache,
    *,
    chunk_size: int = 20,
    skip_existing: bool = True,
    on_progress: Callable[[int, int], None] | None = None,
) -> SaveSummary:
    """Save in small batches, skipping what is already in the library."""
    summary = SaveSummary(requested=list(items))
    chunk_size = max(1, min(chunk_size, MAX_LIBRARY_URIS))
    done = 0

    for chunk in _chunks(list(items), chunk_size):
        try:
            to_save = chunk
            if skip_existing:
                # Two cheap filters: what we know we already saved, and what
                # Spotify says is already there (saved by you, in the app).
                fresh = [s for s in chunk if not cache.already_saved(s.uri)]
                summary.already_there.extend(s for s in chunk if s not in fresh)
                if fresh:
                    present = client.library_contains([s.uri for s in fresh])
                    to_save = [s for s in fresh if not present.get(s.uri, False)]
                    summary.already_there.extend(
                        s for s in fresh if present.get(s.uri, False)
                    )
                else:
                    to_save = []

            if to_save:
                client.library_save([s.uri for s in to_save])
                cache.record_saves([(s.uri, s.title, s.authors) for s in to_save])
                summary.saved.extend(to_save)

        except RateLimitAbort as exc:
            summary.status = "aborted"
            summary.note = str(exc)
            break
        except Forbidden as exc:
            summary.status = "failed"
            summary.note = str(exc)
            break
        except SpotifyError as exc:
            summary.status = "failed"
            summary.note = str(exc)
            break

        done += len(chunk)
        if on_progress:
            on_progress(done, len(items))

    return summary


def undo(
    client: SpotifyClient,
    cache: Cache,
    *,
    chunk_size: int = 20,
    on_progress: Callable[[int, int], None] | None = None,
) -> SaveSummary:
    """Remove exactly what this tool put there, and nothing else."""
    rows = cache.saved_uris()
    items = [
        Savable(title=r["title"], authors=r["authors"], uri=r["uri"], status="saved")
        for r in rows
    ]
    summary = SaveSummary(requested=items)
    chunk_size = max(1, min(chunk_size, MAX_LIBRARY_URIS))
    done = 0

    for chunk in _chunks(items, chunk_size):
        try:
            client.library_remove([s.uri for s in chunk])
            cache.record_removals([s.uri for s in chunk])
            summary.saved.extend(chunk)
        except RateLimitAbort as exc:
            summary.status = "aborted"
            summary.note = str(exc)
            break
        except SpotifyError as exc:
            summary.status = "failed"
            summary.note = str(exc)
            break
        done += len(chunk)
        if on_progress:
            on_progress(done, len(items))

    return summary
