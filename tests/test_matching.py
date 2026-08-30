"""Matching: generous about publisher noise, strict about identity."""

from __future__ import annotations

import pytest

from spotifind.matching import (
    Candidate,
    base_title,
    best_match,
    normalise,
    split_authors,
    surname,
    title_score,
)


def book(name, authors, narrators=("N",)):
    return Candidate(id="x", name=name, authors=list(authors), narrators=list(narrators))


# -- normalisation ---------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("The Fifth Season", "fifth season"),
    ("A Little Life", "little life"),
    ("Piranesi", "piranesi"),
    ("Station Eleven: A Novel", "station eleven"),
    ("Babel (Unabridged)", "babel"),
    ("Dune [Book 1]", "dune"),
    ("Cloud Atlas: A Novel (Unabridged)", "cloud atlas"),
    ("Mrs. Dalloway", "mrs dalloway"),
    ("Kafka on the Shore", "kafka on the shore"),
    ("Les Misérables", "miserables"),
    ("War & Peace", "war and peace"),
    ("The Handmaid's Tale", "handmaid s tale"),
])
def test_normalise(raw, expected):
    assert normalise(raw) == expected


def test_a_novel_only_stripped_at_the_end():
    assert "novel" in normalise("The Novel Bookshop")
    assert normalise("Bookshop: A Novel") == "bookshop"


def test_base_title_drops_the_subtitle():
    assert base_title("Sapiens: A Brief History of Humankind") == "sapiens"
    # No colon: the whole title survives rather than becoming empty.
    assert base_title("Piranesi") == "piranesi"


# -- names -----------------------------------------------------------------

@pytest.mark.parametrize("name, expected", [
    ("N.K. Jemisin", "jemisin"),
    ("Jemisin, N.K.", "jemisin"),
    ("Ursula K. Le Guin", "le guin"),
    ("Gabriel García Márquez", "marquez"),
    ("Ludwig van Beethoven", "van beethoven"),
    ("Martin Luther King Jr.", "king"),
    ("Madonna", "madonna"),
    ("", ""),
])
def test_surname(name, expected):
    assert surname(name) == expected


@pytest.mark.parametrize("cell, expected", [
    ("Andy Weir", ["Andy Weir"]),
    ("Neil Gaiman, Terry Pratchett", ["Neil Gaiman", "Terry Pratchett"]),
    ("Jemisin, N.K.", ["N.K. Jemisin"]),
    ("", []),
])
def test_split_authors(cell, expected):
    assert split_authors(cell) == expected


def test_three_authors_are_not_mistaken_for_an_inverted_name():
    assert len(split_authors("A One, B Two, C Three")) == 3


# -- title scoring ---------------------------------------------------------

def test_identical_titles_score_one():
    assert title_score("Piranesi", "Piranesi") == 1.0


def test_subtitle_on_one_side_only_scores_high():
    assert title_score("Sapiens", "Sapiens: A Brief History of Humankind") >= 0.95
    assert title_score("Sapiens: A Brief History of Humankind", "Sapiens") >= 0.95


def test_publisher_noise_is_forgiven():
    assert title_score("Station Eleven", "Station Eleven: A Novel (Unabridged)") >= 0.95


def test_different_books_score_low():
    assert title_score("The Fifth Season", "The Fifth Risk") < 0.9
    assert title_score("Dune", "Dune Messiah") < 0.9


# -- the decision ----------------------------------------------------------

def test_exact_title_and_author_is_strong():
    match = best_match("Piranesi", ["Susanna Clarke"], [book("Piranesi", ["Susanna Clarke"])])
    assert match.confidence == "strong"
    assert match.found


def test_subtitle_difference_still_strong_when_the_author_agrees():
    match = best_match(
        "Sapiens", ["Yuval Noah Harari"],
        [book("Sapiens: A Brief History of Humankind", ["Yuval Noah Harari"])],
    )
    assert match.confidence == "strong"


def test_same_title_different_author_is_not_a_match():
    """The failure that matters: a same-named book by someone else."""
    match = best_match("The Fifth Season", ["N.K. Jemisin"],
                       [book("The Fifth Season", ["Someone Else"])])
    assert match.confidence == "unconfirmed"
    assert not match.found, "an unconfirmed author must not count as found"


def test_wrong_book_entirely_is_none():
    match = best_match("Piranesi", ["Susanna Clarke"],
                       [book("Jonathan Strange & Mr Norrell", ["Susanna Clarke"])])
    assert match.confidence == "none"
    assert match.candidate is None


def test_sequel_is_not_the_book():
    match = best_match("Dune", ["Frank Herbert"],
                       [book("Dune Messiah", ["Frank Herbert"])])
    assert not match.found


def test_the_right_candidate_wins_a_mixed_page():
    candidates = [
        book("Dune Messiah", ["Frank Herbert"]),
        book("The Dune Encyclopedia", ["Willis McNelly"]),
        book("Dune", ["Frank Herbert"]),
    ]
    match = best_match("Dune", ["Frank Herbert"], candidates)
    assert match.confidence == "strong"
    assert match.candidate.name == "Dune"


def test_author_agreement_beats_a_slightly_better_title():
    """A strong 0.91 must outrank an unconfirmed 0.99."""
    candidates = [
        book("The Fifth Season", ["Impostor Author"]),          # unconfirmed, ~1.0
        book("The Fifth Season: A Novel", ["N.K. Jemisin"]),     # strong, slightly lower
    ]
    match = best_match("The Fifth Season", ["N.K. Jemisin"], candidates)
    assert match.confidence == "strong"
    assert match.candidate.authors == ["N.K. Jemisin"]


def test_second_author_counts():
    match = best_match(
        "Good Omens", ["Terry Pratchett", "Neil Gaiman"],
        [book("Good Omens", ["Neil Gaiman"])],
    )
    assert match.confidence == "strong"


def test_accents_do_not_break_the_author_check():
    match = best_match(
        "One Hundred Years of Solitude", ["Gabriel García Márquez"],
        [book("One Hundred Years of Solitude", ["Gabriel Garcia Marquez"])],
    )
    assert match.confidence == "strong"


def test_a_book_with_no_author_can_only_reach_unconfirmed():
    match = best_match("Beowulf", [], [book("Beowulf", ["Unknown"])])
    assert match.confidence == "unconfirmed"
    assert not match.found


def test_no_candidates_at_all():
    match = best_match("Piranesi", ["Susanna Clarke"], [])
    assert match.confidence == "none"
    assert match.candidate is None
    assert not match.needs_eyes


def test_short_surnames_do_not_match_substrings():
    """'Li' must not match inside 'Delilah'."""
    match = best_match("Some Book", ["Wei Li"], [book("Some Book", ["Delilah Waters"])])
    assert not match.author_matched
