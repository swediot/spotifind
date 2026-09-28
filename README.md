# spotifind

Which books on your StoryGraph to-read list are on Spotify as audiobooks — and
optionally, add them to your library.

A script, not an app. It reads a CSV export, asks Spotify about one book at a
time, slowly, and writes a CSV and a single self-contained HTML page. The
second run is nearly free because every answer is cached.

Written for a 1,400-book to-read list and a brief of "be respectful of the API
limits and don't get me banned", which is why the rate limiter gets a section
of its own below.

```bash
pip install -r requirements.txt
export SPOTIFY_CLIENT_ID=…                       # from developer.spotify.com
python -m spotifind login
python -m spotifind probe                        # one request: does this work?
python -m spotifind check ~/Downloads/storygraph_export.csv --limit 20
```

**Contents:** [markets](#read-this-first-markets-and-why-not-to-trust-the-docs)
· [setup](#setup) · [running it](#running-it) ·
[not getting banned](#not-getting-banned) ·
[what "found" means](#what-found-means) ·
[adding to your library](#adding-them-to-your-library) · [tests](#tests)

---

## Read this first: markets, and why not to trust the docs

**Do not infer audiobook availability from Spotify's documentation.** The Web
API reference page still says audiobooks exist in six markets — US, UK,
Canada, Ireland, New Zealand, Australia — and has not been updated for the
European expansion. Spotify launched audiobooks in **Germany, Austria,
Switzerland and Liechtenstein in April 2025**, with France, Belgium, the
Netherlands and Luxembourg before them. A Swiss Premium account sees a
catalogue of around 350,000 titles, in German, French, Italian and English.

The list in `cli.py` will go stale too. It is used only to soften a warning,
never to decide anything. The reliable answer costs one request:

```bash
export SPOTIFY_CLIENT_ID=…
python -m spotifind login
python -m spotifind probe
```

`probe` also tells you which **languages** your market returns, which matters
more than you'd expect — see below.

To ask about a catalogue you are *not* in, use an app token, which does
honour `--market` (a user token never does; Spotify always uses the account's
own country):

```bash
export SPOTIFY_CLIENT_SECRET=…
python -m spotifind probe --auth app --market US
```

### "On Spotify" ≠ unlimited

Premium includes a **monthly audiobook listening allowance** (12 hours at the
time of writing), with paid top-ups and an Audiobooks+ add-on beyond that. So
this tool answers "is it in the catalogue I can see", not "can I listen to all
of these this month". The report says so in its footer.

---

## Setup

1. Create an app at <https://developer.spotify.com/dashboard>. Any name.
2. Add `http://127.0.0.1:8888/callback` as a redirect URI.
   Spotify requires HTTPS *except* for loopback IP literals — `127.0.0.1`
   works, `localhost` is rejected.
3. Copy the client id. You only need the secret for `--auth app`.

```bash
pip install -r requirements.txt
export SPOTIFY_CLIENT_ID=your_client_id
python -m spotifind login          # opens a browser once
```

The refresh token is written to `~/.config/spotifind/token.json`, mode 0600.
No client secret is stored anywhere: the user flow uses PKCE, and no OAuth
scopes are requested, because searching the catalogue needs none.

## Running it

```bash
python -m spotifind check ~/Downloads/storygraph_export.csv
```

Writes `spotify-availability.csv` and `spotify-availability.html` next to the
export. Useful flags:

| Flag | Why |
| --- | --- |
| `--dry-run` | print the request count and time estimate, ask Spotify nothing |
| `--rate 0.25` | halve the request rate (default 0.5/second, one request every two seconds) |
| `--limit 50` | try it on the first 50 books before committing to the whole list |
| `--daily-budget 400` | send at most this many requests in any 24 hours (default 600; see below) |
| `--refresh` | ignore cached answers |
| `--fallback never` | one request per book instead of up to two (see below) |
| `--prefer-language de` | report the German edition when a book has several |
| `--auth app --market US` | query a market you are not in (needs the client secret) |
| `--all-shelves` | don't filter to the to-read shelf |

Expect, for a 1,400-book list at the default rate:

| | requests | time |
| --- | --- | --- |
| cold, `--fallback never` | 1,400 | ~47 min |
| cold, `--fallback empty` (default) | up to 2,800 | ~93 min |
| warm (nothing stale) | 0 | instant |

A cold list that size needs more requests than one day's budget, so it takes
several days: about three with `--fallback never`, up to five with the
default. Each run stops by itself at the budget and says when to come back;
the next run picks up where it stopped. `--dry-run` says how many days to
expect.

Stopping it with Ctrl-C is safe. Everything already checked is cached, and
re-running skips it.

## Not getting banned

This was the explicit brief, so it drove the design.

Spotify does not publish a rate limit. What it documents is the *shape*:
calls are counted in a **rolling 30-second window**, and crossing the line
returns **429** with a `Retry-After` header. That is not the limit that bites,
though: in practice a Development Mode app is cut off after about **700
requests a day**, whatever the rate (see below). So:

- **A daily request budget, 600 by default.** Every request is written down
  in the cache database, and before sending another the tool counts how many
  went out in the last 24 hours. At the budget the run stops by itself, before
  Spotify has to refuse anything, and says when the budget frees up. The
  window rolls: yesterday's requests free up one at a time as they turn 24
  hours old, and a run started while that is happening follows their pace
  instead of stopping. The budget covers `check`, `probe` and `save` alike.
- **A refusal measured in hours is remembered.** A `Retry-After` longer than
  five minutes is kept in the cache database, and until it has passed the tool
  will not contact Spotify at all — not even for `probe`.
- The limiter models that exact window and sits well under any plausible
  line — **one request every two seconds by default**, with a minimum gap
  between calls so a burst can never form even after a long idle pause.
- **One request at a time.** There is no concurrency anywhere in the project,
  and a test asserts it.
- On a 429 it waits out `Retry-After` in full plus a five-second pad, and
  then **permanently halves the rate for the rest of the run**, down to a floor
  of one request every ten seconds. A 429 is treated as
  evidence that the chosen rate was wrong, not as a speed bump.
- Repeated 429s **wait three times longer each time**, because Spotify's
  `Retry-After` can be shorter than the window that actually needs to drain.
- **Four 429s in a row, or eight in one run, and it stops** — four attempts
  spanning a couple of minutes. Backing off and continuing to knock is what gets
  an app's access pulled. Stopping is cheap here because the cache means
  resuming later costs nothing.
- A 403 stops the run immediately rather than repeating a configuration
  error 1,400 times.
- The User-Agent identifies the tool as a personal, single-threaded checker.
- The cache exists so you can look at the results as often as you like
  without ever asking Spotify again.

### What a real run actually showed

A 1,396-book run at what was then the default 1/s: **682 requests, ~694 books, 26 minutes,
then three 429s and a clean stop.** So one request per second is sustainable
for about 20 minutes and then it isn't — which no documentation anywhere
says, and which is consistent with a Development Mode app having a quota over
a longer window than the documented 30 seconds.

Three things changed as a result:

- **Waits between repeated 429s now escalate** (×3 each time, capped at 15
  minutes) instead of retrying at exactly `Retry-After`. Spotify's
  `Retry-After` can be shorter than the window that actually needs to drain,
  so retrying at exactly that value earns another refusal — and three of
  those in a row used to end a run over what was really one throttling event.
- **The abort threshold went from 3 consecutive to 4**, which is *more*
  patient rather than less: four attempts now span a couple of minutes where
  three used to span 14.
- **The default rate is now 0.5/s**, one request every two seconds, and the
  floor it halves down to after a 429 is one request every ten seconds. A
  full cold run takes about twice as long and is far more likely to finish in
  one go. The cache means a stopped run is never wasted work.

A second real run, on 26 September 2026 at the new 0.5/s, **was cut off after
697 requests** — fifteen more than the first, at half the speed — with a
`Retry-After` of 83,818 seconds, about 23 hours. Going slower bought nothing.
The limit is a count of about 700 requests a day, and the `Retry-After`
ended almost exactly 24 hours after that run's first request, which is what a
rolling 24-hour window would do. So:

- **The daily budget above now stops each run at 600 requests** in any 24
  hours, comfortably short of the cutoff. Lower it with `--daily-budget` if
  a run is ever refused anyway; `--daily-budget 0` turns it off, for an app
  with extended quota.
- **Upgrading counts what already happened.** A cache database from before
  the budget has no record of individual requests, so on first open the
  ledger is seeded from the run history — each run's requests spread evenly
  over its start and finish. The first run after upgrading therefore knows
  the day's quota is already spent.
- **A stopped run still reports every cached book.** It used to leave out
  everything after the point where it stopped, cached or not; now the report
  includes every book the cache has an answer for and counts the rest as
  "not checked yet".

## What "found" means

Each book gets one search for `Title Author`, and the results are scored on
title similarity *and* author agreement:

| Band | Rule | Where it lands |
| --- | --- | --- |
| **strong** | title ≥ 0.90 **and** an author surname matches | "On Spotify" |
| **likely** | title ≥ 0.78 and an author matches | "Worth a look" |
| **unconfirmed** | title ≥ 0.93 but no author match | "Worth a look" |
| **none** | anything else | "Couldn't find these" |

Only **strong** counts as available. The bias is deliberate: a false positive
puts a book you still want into a pile you'll never look at again, so the
awkward cases are surfaced for a glance instead of being decided for you.

Publisher noise is stripped before comparing — "A Novel", "Unabridged",
anniversary editions, bracketed series markers. Prefix matches are *not*
rewarded, which is what stops "Dune Messiah" being reported as "Dune".

## Adding them to your library

```bash
python -m spotifind login --for-saving       # once: asks for library permission
python -m spotifind save spotify-availability.csv --dry-run
python -m spotifind save spotify-availability.csv
```

It reads **the report CSV**, not the cache. That's deliberate: the list of
things about to be written to your account is a file you can open, sort and
delete rows from first, and what you see is exactly what gets saved.

- Only `on_spotify` rows by default. `--include-probably` and
  `--include-check` widen it.
- Skips anything already in your library — both what it saved before and what
  you saved yourself in the app.
- Batches of 20, through the same rate limiter as everything else.
- Asks for confirmation, and refuses outright if it isn't attached to a
  terminal and you didn't pass `--yes`.

**Undo:**

```bash
python -m spotifind save --undo
```

Removes exactly what spotifind added, tracked per URI in the cache. Books you
saved yourself are never touched, even if they're on the list.

`login --for-saving` is a separate command because scopes are fixed at
sign-in: `check` and `probe` request **no permissions at all**, and there's no
reason to hand a read-only tool write access to your library. If you try to
save with an old token you get told to sign in again rather than a 403
halfway through.

### One caveat I could not resolve

Spotify's docs confirm `PUT /me/library` accepts audiobooks, but the
per-request maximum and the exact scope for audiobooks specifically are not
documented, and one API surface reports audiobooks as `NOT_SAVEABLE`. So this
may simply not work. It's built defensively — `--dry-run` first, small
batches, everything recorded for undo, and a clear message on 403 — but the
first real run is the test. Try it with `--limit 3` before the whole list.

### Several editions of one book

In a market like Switzerland a search for one title comes back with several
editions — English and German, sometimes French and Italian, sometimes two
English ones with different narrators. So which edition you're shown is a
real choice, not a tie-break.

`--prefer-language` (default `en`) decides it, ranked *after* confidence: a
book whose author matches always beats one in your preferred language whose
author doesn't. An edition with no language field sits between "right
language" and "wrong language" — Spotify doesn't always populate it, and a
missing value shouldn't lose to one you know is wrong.

The other editions aren't discarded: the report says "2 other editions (also
in German)" and the CSV carries `other_editions` and `other_languages`.

One consequence worth knowing: a book on your list in English that Spotify
only carries as a **German translation under a German title** will be
reported as not found, because the titles don't match. That's a real gap, not
a bug I can fix from title matching alone.

### `--fallback`

If the first search comes back completely empty, the default (`empty`) spends
one more request on a title-only search. This catches books whose author cell
holds a translator or an editor. `never` skips it and halves the worst-case
request count; `always` retries whenever nothing matched.

## The cache

SQLite, at `~/.local/share/spotifind/spotifind.sqlite3`, keyed by
(book, market). Found books are trusted for 90 days, misses for 14 — the
catalogue grows, so "not there" is the answer worth re-asking. Run it monthly
and the report tells you what turned up since last time; the "new since the
last run" flag is suppressed on a first run, when everything would carry it.

The same database holds the daily budget's ledger: the time of every request
sent in the last week, and any long refusal from Spotify. Point two commands
at different `--cache` files and they keep separate budgets, so don't, unless
they use different Spotify apps.

## Tests

```bash
pip install pytest
python -m pytest tests/ -q          # 217 tests, no network
python tests/bench.py 1400 0.5      # simulate a full run on a fake clock
```

**Nothing here has ever spoken to the real api.spotify.com** — the sandbox
this was written in cannot reach it. The tests establish how *this* code
behaves: matching, caching, and above all what the client does when Spotify
pushes back. The response shapes come from the Web API reference as of the
February 2026 changes (search `limit` maxes at 10, batch audiobook fetch
removed, `available_markets` gone from objects).

The first real run is therefore the first real test. Do it with `--limit 20`.

## Known unknowns

- Whether a client-credentials (`--auth app`) token can search the audiobook
  catalogue at all. Spotify has restricted some catalogue endpoints for apps
  without extended quota, and I could not verify audiobook search specifically.
  If `--auth app` returns 403 or empty where `--auth user` works, that's why.
- Spotify's actual limits. Unpublished. Two real runs were cut off at 682
  and 697 requests, which puts the daily quota near 700, but whether it
  varies by day, by app or by endpoint is unknown, and so is whether the
  window is truly rolling. The budget of 600 and the rolling window are the
  cautious reading of two data points.
- Whether the `languages` field is reliably populated on audiobook objects.
  The code treats a missing value as "unknown" rather than "wrong", so the
  worst case is a less useful report, not a wrong one — but if it turns out
  to be empty everywhere, `--prefer-language` does nothing and the "also in
  German" note never appears.
- Whether Spotify's audiobook search behaves better with a different query
  form than `Title Author`. Worth an experiment against the real API.

## A correction, for the record

The first version of this tool asserted that Spotify audiobooks were
unavailable in Switzerland, and built a warning into the CLI and the report
saying so. That came from Spotify's own API reference page, which still lists
only the original six markets. It was wrong: audiobooks launched in
Switzerland in April 2025, and a live Swiss account returns playable
audiobooks today. The market list is now advisory only, `probe` reports what
a token can actually see, and nothing in the code decides availability from a
hard-coded list.
