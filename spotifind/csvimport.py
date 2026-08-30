"""Read a to-read list out of a StoryGraph (or Goodreads) CSV export."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .matching import normalise, split_authors, surname

DATE_FORMATS = ("%Y/%m/%d", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y")


@dataclass
class Book:
    title: str
    authors: list[str] = field(default_factory=list)
    isbn: str = ""
    added: str = ""
    source_row: int = 0

    @property
    def key(self) -> str:
        """Stable identity for caching: normalised title + first surname."""
        return f"{normalise(self.title)}|{surname(self.authors[0]) if self.authors else ''}"

    @property
    def author_display(self) -> str:
        return ", ".join(self.authors)

    @property
    def query(self) -> str:
        """What we hand to Spotify search.

        Plain words, no field filters: Spotify's ``author:`` style filters are
        music-oriented and audiobook search behaves better with a bare query.
        The title is trimmed of its subtitle because long subtitles push the
        relevant result off a 10-item page.
        """
        title = self.title.split(":", 1)[0].strip() or self.title
        first_author = self.authors[0] if self.authors else ""
        return f"{title} {first_author}".strip()


class CsvFormatError(ValueError):
    pass


def parse_date(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    # Some exports carry a full timestamp.
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return ""


def _pick(row: dict, *names: str) -> str:
    for name in names:
        for key in row:
            if key and key.strip().lower() == name.lower():
                return (row[key] or "").strip()
    return ""


def _clean_isbn(value: str) -> str:
    # Goodreads wraps ISBNs as ="9780…"
    return value.strip().lstrip("=").strip('"').strip()


def detect_format(fieldnames: list[str]) -> str:
    lowered = {f.strip().lower() for f in fieldnames if f}
    if "read status" in lowered:
        return "storygraph"
    if "exclusive shelf" in lowered:
        return "goodreads"
    if "title" in lowered and ("authors" in lowered or "author" in lowered):
        return "generic"
    raise CsvFormatError(
        "Could not recognise this CSV. Expected a StoryGraph export (with a "
        "'Read Status' column) or a Goodreads export (with an 'Exclusive "
        f"Shelf' column). Columns found: {sorted(lowered) or 'none'}"
    )


def read_books(path: str | Path, *, only_to_read: bool = True) -> tuple[list[Book], dict]:
    """Return the books plus a small report on what was skipped and why."""
    raw = Path(path).read_bytes()
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise CsvFormatError("The CSV appears to be empty.")

    fmt = detect_format(list(reader.fieldnames))
    books: list[Book] = []
    seen: dict[str, int] = {}
    skipped_status = 0
    skipped_untitled = 0
    duplicates = 0

    for i, row in enumerate(reader, start=2):
        title = _pick(row, "title")
        if not title:
            skipped_untitled += 1
            continue

        if only_to_read:
            status = _pick(row, "read status", "exclusive shelf").lower().replace("_", "-")
            if fmt in ("storygraph", "goodreads") and status and status != "to-read":
                skipped_status += 1
                continue

        author_cell = _pick(row, "authors", "author")
        authors = split_authors(author_cell)
        if not authors:
            inverted = _pick(row, "author l-f")
            if inverted:
                authors = split_authors(inverted)

        book = Book(
            title=title,
            authors=authors,
            isbn=_clean_isbn(_pick(row, "isbn/uid", "isbn13", "isbn")),
            added=parse_date(_pick(row, "date added")),
            source_row=i,
        )
        if book.key in seen:
            duplicates += 1
            continue
        seen[book.key] = i
        books.append(book)

    report = {
        "format": fmt,
        "kept": len(books),
        "skipped_not_to_read": skipped_status,
        "skipped_untitled": skipped_untitled,
        "duplicates": duplicates,
        "without_author": sum(1 for b in books if not b.authors),
    }
    return books, report
