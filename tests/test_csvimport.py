"""Reading the export. Mostly about not silently losing books."""

from __future__ import annotations

import pytest

from spotifind.csvimport import CsvFormatError, parse_date, read_books

STORYGRAPH_HEADER = (
    "Title,Authors,Contributors,ISBN/UID,Format,Read Status,Date Added,"
    "Last Date Read,Dates Read,Read Count,Moods,Pace,Star Rating,Review,Tags,Owned?\n"
)

GOODREADS_HEADER = (
    "Book Id,Title,Author,Author l-f,Additional Authors,ISBN,ISBN13,"
    "My Rating,Number of Pages,Date Read,Date Added,Exclusive Shelf\n"
)


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def storygraph(tmp_path, rows: str, header: str = STORYGRAPH_HEADER):
    return write(tmp_path, "sg.csv", header + rows)


def test_reads_a_storygraph_to_read_list(tmp_path):
    path = storygraph(tmp_path, (
        "Piranesi,Susanna Clarke,,9781635575637,audio,to-read,2025/03/14,,,0,,,,,,No\n"
        "The Fifth Season,N.K. Jemisin,,9780316229296,,to-read,2024/11/02,,,0,,,,,,No\n"
    ))
    books, report = read_books(path)
    assert report["format"] == "storygraph"
    assert [b.title for b in books] == ["Piranesi", "The Fifth Season"]
    assert books[0].authors == ["Susanna Clarke"]
    assert books[0].added == "2025-03-14"


def test_only_the_to_read_shelf_by_default(tmp_path):
    path = storygraph(tmp_path, (
        "Kept,A Author,,,,to-read,2025/01/01,,,0,,,,,,No\n"
        "Finished,B Author,,,,read,2025/01/01,,,0,,,,,,No\n"
        "Started,C Author,,,,currently-reading,2025/01/01,,,0,,,,,,No\n"
    ))
    books, report = read_books(path)
    assert [b.title for b in books] == ["Kept"]
    assert report["skipped_not_to_read"] == 2

    everything, _ = read_books(path, only_to_read=False)
    assert len(everything) == 3


def test_goodreads_export(tmp_path):
    path = write(tmp_path, "gr.csv", GOODREADS_HEADER + (
        '1,"Piranesi","Susanna Clarke","Clarke, Susanna",,="1635575630",'
        '="9781635575637",0,272,,2025/03/14,to-read\n'
    ))
    books, report = read_books(path)
    assert report["format"] == "goodreads"
    assert books[0].authors == ["Susanna Clarke"]
    assert books[0].isbn == "9781635575637"


def test_duplicates_are_collapsed(tmp_path):
    """The same book twice must not become two requests."""
    path = storygraph(tmp_path, (
        "Piranesi,Susanna Clarke,,,,to-read,2025/01/01,,,0,,,,,,No\n"
        "Piranesi: A Novel,Susanna Clarke,,,,to-read,2025/02/01,,,0,,,,,,No\n"
    ))
    books, report = read_books(path)
    assert len(books) == 1
    assert report["duplicates"] == 1


def test_same_title_by_different_authors_is_not_a_duplicate(tmp_path):
    path = storygraph(tmp_path, (
        "The Fifth Season,N.K. Jemisin,,,,to-read,2025/01/01,,,0,,,,,,No\n"
        "The Fifth Season,Someone Else,,,,to-read,2025/01/01,,,0,,,,,,No\n"
    ))
    books, _ = read_books(path)
    assert len(books) == 2


def test_rows_without_a_title_are_skipped_not_queried(tmp_path):
    path = storygraph(tmp_path, (
        ",Nobody,,,,to-read,2025/01/01,,,0,,,,,,No\n"
        "Real Book,A Author,,,,to-read,2025/01/01,,,0,,,,,,No\n"
    ))
    books, report = read_books(path)
    assert [b.title for b in books] == ["Real Book"]
    assert report["skipped_untitled"] == 1


def test_a_book_with_no_author_is_kept_and_counted(tmp_path):
    path = storygraph(tmp_path, "Beowulf,,,,,to-read,2025/01/01,,,0,,,,,,No\n")
    books, report = read_books(path)
    assert books[0].authors == []
    assert report["without_author"] == 1
    assert books[0].query == "Beowulf"


def test_the_query_drops_the_subtitle_but_keeps_the_author(tmp_path):
    path = storygraph(
        tmp_path,
        '"Sapiens: A Brief History of Humankind",Yuval Noah Harari,,,,to-read,2025/01/01,,,0,,,,,,No\n',
    )
    books, _ = read_books(path)
    assert books[0].query == "Sapiens Yuval Noah Harari"


def test_utf8_bom_and_accents(tmp_path):
    path = tmp_path / "bom.csv"
    path.write_bytes(
        b"\xef\xbb\xbf" + (STORYGRAPH_HEADER +
        "Les Misérables,Victor Hugo,,,,to-read,2025/01/01,,,0,,,,,,No\n").encode("utf-8")
    )
    books, _ = read_books(path)
    assert books[0].title == "Les Misérables"


def test_an_unrecognisable_csv_says_so(tmp_path):
    path = write(tmp_path, "junk.csv", "alpha,beta\n1,2\n")
    with pytest.raises(CsvFormatError, match="Could not recognise"):
        read_books(path)


def test_an_empty_file_says_so(tmp_path):
    with pytest.raises(CsvFormatError):
        read_books(write(tmp_path, "empty.csv", ""))


@pytest.mark.parametrize("raw, expected", [
    ("2025/03/14", "2025-03-14"),
    ("2025-03-14", "2025-03-14"),
    ("14.03.2025", "2025-03-14"),
    ("2025-03-14T09:00:00Z", "2025-03-14"),
    ("not a date", ""),
    ("", ""),
])
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected
