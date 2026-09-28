"""Turning a run into a CSV and a single self-contained HTML page."""

from __future__ import annotations

import csv
import html
import json
from datetime import datetime, timezone
from pathlib import Path

from .checker import BookResult, RunSummary

CSV_COLUMNS = [
    "title", "authors", "status", "confidence", "spotify_title",
    "spotify_authors", "narrators", "language", "edition", "chapters",
    "spotify_id", "spotify_url", "other_editions", "other_languages",
    "title_score", "author_matched", "newly_found", "from_cache",
    "date_added", "error",
]

LANGUAGE_NAMES = {
    "en": "English", "de": "German", "fr": "French", "it": "Italian",
    "es": "Spanish", "nl": "Dutch", "pt": "Portuguese", "sv": "Swedish",
    "da": "Danish", "no": "Norwegian", "fi": "Finnish", "pl": "Polish",
}

STATUS_LABEL = {
    "strong": "on_spotify",
    "likely": "probably",
    "unconfirmed": "check",
    "none": "not_found",
}


def _row(result: BookResult) -> dict:
    cand = result.match.candidate
    return {
        "title": result.book.title,
        "authors": result.book.author_display,
        "status": "error" if result.error else STATUS_LABEL[result.match.confidence],
        "confidence": result.match.confidence,
        "spotify_title": cand.name if cand else "",
        "spotify_authors": "; ".join(cand.authors) if cand else "",
        "narrators": "; ".join(cand.narrators) if cand else "",
        "language": ", ".join(sorted(cand.language_codes())) if cand else "",
        "edition": cand.edition if cand else "",
        "chapters": cand.total_chapters if cand else "",
        "spotify_id": cand.id if cand else "",
        "spotify_url": cand.url if cand else "",
        "other_editions": result.match.alternates or "",
        "other_languages": ", ".join(result.match.alternate_languages),
        "title_score": f"{result.match.title_score:.3f}",
        "author_matched": "yes" if result.match.author_matched else "no",
        "newly_found": "yes" if result.newly_found else "",
        "from_cache": "yes" if result.from_cache else "",
        "date_added": result.book.added,
        "error": result.error,
    }


def write_csv(summary: RunSummary, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    order = {"strong": 0, "likely": 1, "unconfirmed": 2, "none": 3}
    rows = sorted(
        summary.results,
        key=lambda r: (bool(r.error), order[r.match.confidence], r.book.title.lower()),
    )
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for result in rows:
            writer.writerow(_row(result))
    return path


# --------------------------------------------------------------------------


def _card(result: BookResult, *, show_candidate: bool = True) -> str:
    cand = result.match.candidate
    title = html.escape(result.book.title)
    authors = html.escape(result.book.author_display or "unknown author")
    bits = [f'<div class="t">{title}</div><div class="a">{authors}</div>']

    if cand and show_candidate:
        meta = []
        if cand.narrators:
            meta.append("read by " + html.escape(", ".join(cand.narrators)))
        codes = sorted(cand.language_codes())
        if codes:
            meta.append("in " + html.escape(LANGUAGE_NAMES.get(codes[0], codes[0])))
        if cand.edition:
            meta.append(html.escape(cand.edition))
        if cand.total_chapters:
            meta.append(f"{cand.total_chapters} chapters")
        differs = cand.name.strip().lower() != result.book.title.strip().lower()
        if differs:
            meta.insert(0, "listed as “" + html.escape(cand.name) + "”")
        if meta:
            bits.append('<div class="m">' + " · ".join(meta) + "</div>")
        if cand.url:
            bits.append(f'<a class="lk" href="{html.escape(cand.url)}" target="_blank" rel="noreferrer">Open in Spotify</a>')
        if result.match.alternates:
            langs = [LANGUAGE_NAMES.get(c, c) for c in result.match.alternate_languages]
            extra = f" (also in {html.escape(', '.join(langs))})" if langs else ""
            plural = "s" if result.match.alternates > 1 else ""
            bits.append(f'<div class="m">{result.match.alternates} other edition{plural}{extra}</div>')
        if result.match.confidence != "strong":
            why = ("author didn’t match" if not result.match.author_matched
                   else f"title match {result.match.title_score:.0%}")
            bits.append(f'<div class="w">{why}</div>')
    if result.error:
        bits.append(f'<div class="w">{html.escape(result.error[:200])}</div>')
    if result.newly_found:
        bits.append('<div class="new">new since the last run</div>')

    search = html.escape(f"{result.book.title} {result.book.author_display}".lower(), quote=True)
    return f'<li class="card" data-s="{search}">' + "".join(bits) + "</li>"


def _section(title: str, blurb: str, results: list[BookResult], *, show_candidate: bool = True) -> str:
    if not results:
        return ""
    cards = "\n".join(_card(r, show_candidate=show_candidate) for r in results)
    return f"""
<section>
  <h2>{html.escape(title)} <span class="count">{len(results)}</span></h2>
  <p class="blurb">{html.escape(blurb)}</p>
  <ul class="cards">{cards}</ul>
</section>"""


def write_html(summary: RunSummary, path: str | Path, *, meta: dict | None = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = meta or {}
    generated = datetime.now(timezone.utc).astimezone().strftime("%d %B %Y, %H:%M")

    total = len(summary.results)
    strong = summary.strong
    maybe = summary.likely + summary.unconfirmed
    missing = summary.missing
    failed = summary.failed

    market = meta.get("market") or "your account’s market"
    subtitle = (
        f"{len(strong)} of {total} books on your to-read list are on Spotify as audiobooks "
        f"in {html.escape(str(market))}."
    )
    banner = ""
    if summary.status == "aborted":
        why = ("it reached its daily request budget, before Spotify had to refuse anything"
               if summary.stopped_by_budget else "Spotify asked it to slow down")
        left = (f" {summary.unchecked} books are not checked yet and are not listed here."
                if summary.unchecked else "")
        banner = (f'<div class="banner">This run stopped early because {why}.{left} '
                  'Everything checked so far is here and cached — re-running picks up where '
                  'it left off.</div>')
    elif summary.status == "failed":
        banner = f'<div class="banner">This run stopped: {html.escape(summary.note[:300])}</div>'
    elif not strong and total:
        banner = ('<div class="banner">Nothing matched. Before assuming the catalogue is '
                  'empty, run <code>spotifind probe</code> — one request, and it says whether '
                  'this token can see audiobooks at all. If it can, the problem is the '
                  'matching, not the market.</div>')

    stats = [
        ("On Spotify", len(strong)),
        ("Worth a look", len(maybe)),
        ("Not found", len(missing)),
    ]
    if summary.newly_found:
        stats.append(("New since last run", len(summary.newly_found)))
    if failed:
        stats.append(("Errors", len(failed)))
    stat_html = "".join(
        f'<div class="stat"><b>{v}</b><span>{html.escape(k)}</span></div>' for k, v in stats
    )

    body = "\n".join([
        _section("On Spotify", "Title and author both matched.", strong),
        _section("Worth a look", "Close, but something didn’t line up — glance at these.", maybe),
        _section("Couldn’t find these", "No audiobook came back for the title and author.", missing, show_candidate=False),
        _section("Errors", "These books were not checked.", failed, show_candidate=False),
    ])

    footer_bits = []
    if meta.get("requests") is not None:
        footer_bits.append(f"{meta['requests']} requests to Spotify")
    if meta.get("from_cache"):
        footer_bits.append(f"{meta['from_cache']} answers from cache")
    if meta.get("rate"):
        footer_bits.append(f"about {meta['rate']}")
    if meta.get("elapsed"):
        footer_bits.append(f"took {meta['elapsed']}")
    footer = html.escape(" · ".join(footer_bits))
    # "On Spotify" means in the catalogue. Premium includes a monthly
    # audiobook allowance rather than unlimited listening, so a long list here
    # is not a long list you can finish this month.
    footer += ('<br>“On Spotify” means the audiobook is in the catalogue your account can '
               'see. Premium includes a monthly listening allowance, not unlimited '
               'audiobooks.')

    return _write(path, TEMPLATE.format(
        generated=html.escape(generated),
        subtitle=subtitle,
        banner=banner,
        stats=stat_html,
        body=body,
        footer=footer,
        payload=html.escape(json.dumps({"total": total, "found": len(strong)}), quote=True),
    ))


def _write(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    return path


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Your to-read list on Spotify</title>
<style>
:root {{
  color-scheme: light dark;
  --bg: #faf9f7; --panel: #ffffff; --ink: #1b1a18; --muted: #6b6862;
  --line: #e6e3dd; --accent: #1a7f47; --warn: #8a6a12; --new: #1a5f7f;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #14161a; --panel: #1b1e24; --ink: #eceff3; --muted: #9aa1ad;
    --line: #2b3038; --accent: #4ecb7d; --warn: #e0b64a; --new: #62b6e0;
  }}
}}
* {{ box-sizing: border-box; }}
/* Beats any display rule set below — the filter relies on it. */
[hidden] {{ display: none !important; }}
body {{ margin:0; background:var(--bg); color:var(--ink);
  font:16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }}
.wrap {{ max-width: 980px; margin: 0 auto; padding: 2.5rem 1.25rem 5rem; }}
h1 {{ font-size: 1.6rem; margin: 0 0 .35rem; letter-spacing: -.01em; }}
.sub {{ color: var(--muted); margin: 0 0 1.5rem; }}
.banner {{ background: var(--panel); border:1px solid var(--line);
  border-left: 3px solid var(--warn); padding: .85rem 1rem; border-radius: 8px;
  margin-bottom: 1.5rem; color: var(--ink); }}
.banner code {{ background: rgba(128,128,128,.15); padding: .1em .35em; border-radius: 4px; }}
.stats {{ display:flex; flex-wrap:wrap; gap:.75rem; margin-bottom: 1.75rem; }}
.stat {{ background: var(--panel); border:1px solid var(--line); border-radius:10px;
  padding:.7rem 1rem; min-width: 7.5rem; }}
.stat b {{ display:block; font-size:1.5rem; line-height:1.1; }}
.stat span {{ color: var(--muted); font-size:.82rem; }}
#filter {{ width:100%; padding:.7rem .9rem; border-radius:9px; border:1px solid var(--line);
  background: var(--panel); color: var(--ink); font-size:1rem; margin-bottom: 2rem; }}
section {{ margin-bottom: 2.75rem; }}
h2 {{ font-size:1.05rem; margin:0 0 .2rem; display:flex; align-items:center; gap:.5rem; }}
.count {{ font-weight:500; font-size:.8rem; color:var(--muted); background:var(--panel);
  border:1px solid var(--line); border-radius:999px; padding:.05rem .55rem; }}
.blurb {{ color:var(--muted); font-size:.88rem; margin:0 0 .9rem; }}
.cards {{ list-style:none; margin:0; padding:0; display:grid; gap:.6rem;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:.8rem .9rem; }}
.t {{ font-weight:600; line-height:1.3; }}
.a {{ color:var(--muted); font-size:.88rem; margin-top:.1rem; }}
.m {{ font-size:.82rem; margin-top:.45rem; color:var(--muted); }}
.w {{ font-size:.78rem; margin-top:.4rem; color:var(--warn); }}
.new {{ font-size:.78rem; margin-top:.4rem; color:var(--new); font-weight:600; }}
.lk {{ display:inline-block; margin-top:.5rem; font-size:.85rem; color:var(--accent);
  text-decoration:none; font-weight:600; }}
.lk:hover {{ text-decoration:underline; }}
.empty {{ color:var(--muted); font-style:italic; }}
footer {{ color:var(--muted); font-size:.82rem; border-top:1px solid var(--line);
  padding-top:1rem; margin-top:2rem; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>Your to-read list on Spotify</h1>
  <p class="sub">{subtitle} Generated {generated}.</p>
  {banner}
  <div class="stats">{stats}</div>
  <input id="filter" type="search" placeholder="Filter by title or author…" autocomplete="off">
  {body}
  <footer>{footer}</footer>
</div>
<script>
const input = document.getElementById('filter');
const cards = Array.from(document.querySelectorAll('.card'));
input.addEventListener('input', () => {{
  const terms = input.value.toLowerCase().split(/\\s+/).filter(Boolean);
  cards.forEach(c => {{
    const hay = c.dataset.s || '';
    c.hidden = !terms.every(t => hay.includes(t));
  }});
  document.querySelectorAll('section').forEach(s => {{
    const any = Array.from(s.querySelectorAll('.card')).some(c => !c.hidden);
    s.hidden = !any;
  }});
}});
</script>
</body>
</html>
"""
