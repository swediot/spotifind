"""Deciding whether a Spotify audiobook is the book you meant.

Spotify's search is fuzzy and its audiobook titles are noisy in specific,
predictable ways: "A Novel", "Unabridged", edition and series markers, a
narrator's name welded onto the end. The job here is to be generous about
that noise and strict about everything else, because a false positive here
is worse than a miss — it puts a book you still want to find into the
"already available" pile, where you will never look at it again.

The output is deliberately a *band*, not a boolean. Anything below `strong`
lands in a "check these" section of the report.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Iterable, Sequence

# Trailing decoration that Spotify (and publishers) add to titles. Stripped
# only when it appears at the *end*, so "A Novel Bookshop" survives intact.
TAIL_NOISE = re.compile(
    r"""\s*[:;,\-–—(\[]?\s*(
        a\s+novel(la)? | a\s+memoir | a\s+(true\s+)?story | a\s+thriller |
        a\s+mystery | a\s+romance | a\s+biography | a\s+history |
        unabridged | abridged | audiobook | audio\s+book |
        complete\s+and\s+unabridged |
        (the\s+)?(un)?abridged\s+edition |
        \d+(st|nd|rd|th)\s+anniversary\s+edition |
        (deluxe|special|revised|expanded|collector'?s|movie\s+tie[\s-]?in)\s+edition
    )\s*[)\]]?\s*$""",
    re.IGNORECASE | re.VERBOSE,
)

# Parenthetical series/volume markers anywhere in the title.
BRACKETED = re.compile(r"[\(\[\{][^)\]\}]*[\)\]\}]")

# "Book 3", "Volume II", "#2", "Part Two" — series positioning, not identity.
SERIES_NOISE = re.compile(
    r"\b(book|bk|volume|vol|part|pt|no|number)\b\.?\s*"
    r"(\d+|[ivxlc]+|one|two|three|four|five|six|seven|eight|nine|ten)\b",
    re.IGNORECASE,
)

LEADING_ARTICLE = re.compile(r"^(the|a|an|le|la|les|el|los|las|der|die|das)\s+", re.IGNORECASE)

PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
WHITESPACE = re.compile(r"\s+")

# Name particles that should not be mistaken for a surname.
PARTICLES = {
    "de", "del", "della", "di", "da", "van", "von", "der", "den", "ter",
    "le", "la", "du", "des", "bin", "ibn", "al", "el", "st", "san",
}

SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "md", "esq"}


def strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(ch)
    )


def normalise(text: str) -> str:
    """Fold a title down to comparable words."""
    if not text:
        return ""
    text = strip_accents(text)
    text = text.replace("&", " and ")
    text = text.replace("’", "'").replace("‘", "'")
    text = BRACKETED.sub(" ", text)
    # Tail noise can stack: "Title: A Novel (Unabridged)".
    for _ in range(3):
        stripped = TAIL_NOISE.sub("", text)
        if stripped == text:
            break
        text = stripped
    text = SERIES_NOISE.sub(" ", text)
    text = PUNCT.sub(" ", text)
    text = WHITESPACE.sub(" ", text).strip().lower()
    text = LEADING_ARTICLE.sub("", text)
    return text.strip()


def base_title(text: str) -> str:
    """The part before a subtitle colon, normalised.

    StoryGraph often carries the full subtitle where Spotify carries only the
    main title, or the other way round. Comparing both ways costs nothing.
    """
    head = text.split(":", 1)[0]
    normalised_head = normalise(head)
    return normalised_head or normalise(text)


def surname(name: str) -> str:
    """Best guess at the family name, from either name order."""
    name = strip_accents(name or "").strip()
    if not name:
        return ""
    if "," in name:
        # "Jemisin, N.K."
        candidate = name.split(",", 1)[0]
    else:
        candidate = name
    parts = [p for p in PUNCT.sub(" ", candidate).split() if p]
    parts = [p for p in parts if p.lower().strip(".") not in SUFFIXES]
    if not parts:
        return ""
    # Walk backwards past particles: "Ursula K. Le Guin" -> "le guin".
    idx = len(parts) - 1
    while idx > 0 and parts[idx - 1].lower() in PARTICLES:
        idx -= 1
    return " ".join(p.lower() for p in parts[idx:])


def split_authors(field: str) -> list[str]:
    """Split a StoryGraph/Goodreads author cell into individual names.

    StoryGraph separates multiple authors with ", " while Goodreads usually
    carries one name, sometimes as "Last, First". The heuristic: if there are
    exactly two comma-separated parts and the second looks like given names
    or initials, treat it as one inverted name.
    """
    field = (field or "").strip()
    if not field:
        return []
    parts = [p.strip() for p in field.split(",") if p.strip()]
    if len(parts) == 2:
        head_tokens = parts[0].split()
        # A surname on its own, or a particled one ("Le Guin", "van Gogh").
        head_is_surname = len(head_tokens) == 1 or (
            len(head_tokens) == 2 and head_tokens[0].lower() in PARTICLES
        )
        tail_ok = 1 <= len(parts[1].split()) <= 3 and " and " not in parts[1].lower()
        if head_is_surname and tail_ok:
            return [f"{parts[1]} {parts[0]}"]
    return parts


def ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def title_score(want: str, got: str) -> float:
    """Similarity of two titles, forgiving of subtitles on either side."""
    want_n, got_n = normalise(want), normalise(got)
    if not want_n or not got_n:
        return 0.0
    if want_n == got_n:
        return 1.0

    scores = [ratio(want_n, got_n)]

    # One side carrying a subtitle the other lacks is very common, so compare
    # the pre-colon heads too. This deliberately does NOT reward a bare prefix
    # match: "Dune" is a prefix of "Dune Messiah", and treating that as the
    # same book would file a sequel you have not read as already available.
    # Only an explicit subtitle separator earns the second comparison.
    want_b, got_b = base_title(want), base_title(got)
    if want_b and got_b and (":" in want or ":" in got):
        scores.append(ratio(want_b, got_b) * 0.99)
    return max(scores)


def author_overlap(wanted: Sequence[str], candidates: Iterable[str]) -> tuple[bool, str]:
    """Does any wanted author's surname appear among the candidate's authors?"""
    wanted_surnames = {s for s in (surname(a) for a in wanted) if len(s) > 2}
    if not wanted_surnames:
        return False, ""
    haystack = " | ".join(strip_accents(c or "").lower() for c in candidates)
    if not haystack.strip(" |"):
        return False, ""
    for s in wanted_surnames:
        if re.search(rf"(?<![a-z]){re.escape(s)}(?![a-z])", haystack):
            return True, s
    return False, ""


# Confidence bands. `strong` is what the report treats as "yes, it's on
# Spotify"; the rest are surfaced separately for a human glance.
STRONG_TITLE = 0.90
LIKELY_TITLE = 0.78
UNCONFIRMED_TITLE = 0.93  # required when the author does not match


@dataclass
class Candidate:
    """One audiobook returned by Spotify search, flattened."""
    id: str
    name: str
    authors: list[str]
    narrators: list[str]
    publisher: str = ""
    edition: str = ""
    total_chapters: int | None = None
    url: str = ""
    description: str = ""
    explicit: bool = False
    languages: list[str] = field(default_factory=list)
    raw: dict | None = None

    def language_codes(self) -> set[str]:
        """Two-letter codes, from whatever form Spotify used ('de-DE' → 'de')."""
        return {str(code).split("-")[0].lower() for code in self.languages if code}


@dataclass
class Match:
    candidate: Candidate | None
    confidence: str  # strong | likely | unconfirmed | none
    title_score: float
    author_matched: bool
    matched_on: str = ""
    #: Other acceptable editions of the same book on the same results page.
    alternates: int = 0
    #: Languages those other editions are in, for "also in German".
    alternate_languages: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.confidence in ("strong", "likely")

    @property
    def needs_eyes(self) -> bool:
        return self.confidence in ("likely", "unconfirmed")


CONFIDENCE_ORDER = {"strong": 3, "likely": 2, "unconfirmed": 1, "none": 0}


def _language_rank(cand: Candidate, prefer: str) -> float:
    """1 for the preferred language, 0.5 when unknown, 0 for another one.

    A market like Switzerland returns German, French and Italian editions
    alongside the English one, so which edition you are shown is a real
    choice rather than a tie-break. Unknown sits in the middle: Spotify does
    not always populate the field, and a missing value should not lose to a
    language we know is wrong.
    """
    codes = cand.language_codes()
    if not codes:
        return 0.5
    return 1.0 if prefer.lower() in codes else 0.0


def best_match(
    title: str,
    authors: Sequence[str],
    candidates: Sequence[Candidate],
    *,
    prefer_language: str = "en",
) -> Match:
    """Pick the best candidate, or decide none of them is the book."""
    scored: list[tuple[tuple, Match]] = []

    for cand in candidates:
        t = title_score(title, cand.name)
        hit, which = author_overlap(authors, cand.authors)

        if hit and t >= STRONG_TITLE:
            confidence = "strong"
        elif hit and t >= LIKELY_TITLE:
            confidence = "likely"
        elif not hit and t >= UNCONFIRMED_TITLE:
            confidence = "unconfirmed"
        else:
            confidence = "none"

        if confidence == "none":
            continue

        # Rank by confidence band first — a strong 0.91 beats an unconfirmed
        # 0.99, because the author agreeing is worth more than the last few
        # points of string overlap — then by language, then by title score.
        key = (
            CONFIDENCE_ORDER[confidence],
            _language_rank(cand, prefer_language),
            t,
        )
        scored.append((key, Match(cand, confidence, t, hit, which)))

    if not scored:
        return Match(None, "none", 0.0, False)

    scored.sort(key=lambda pair: pair[0], reverse=True)
    best = scored[0][1]

    # Other editions of the same book: same confidence band as the winner,
    # a different Spotify id. Reported, not chosen for you.
    others = [
        m.candidate for _, m in scored[1:]
        if m.confidence == best.confidence and m.candidate.id != best.candidate.id
    ]
    best.alternates = len(others)
    languages: list[str] = []
    for cand in others:
        for code in sorted(cand.language_codes()):
            if code not in languages:
                languages.append(code)
    best.alternate_languages = languages
    return best
