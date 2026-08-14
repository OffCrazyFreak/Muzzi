#!/usr/bin/env python3
"""Where a person can go and look for themselves.

A review row says a track scored 0.62 and names the catalogues that disagreed.
Acting on that means opening a browser, retyping the artist and the title into
four sites, and hoping the spelling survives the retyping. The pipeline already
knows every identifier it resolved, so the retyping is work it can do.

Two kinds of link, and the difference matters:

  a record   built from an identifier a source actually returned, so it opens
             the exact track, album or recording that answered. If Deezer said
             72299954, `deezer.com/track/72299954` is that answer and nothing
             else.
  a search   built from the artist and title, for sources that answered
             without leaving an id behind, or that were asked and found
             nothing. It opens their search, which is where you would have
             gone anyway.

Records first, searches after, because one is evidence and the other is a
starting point.

Nothing here fetches anything. These are strings built from what is already in
the store, so a dossier costs nothing and cannot fail mid-run.

Every shape below was checked against the live site on 2026-08-14. Discogs
answers a scripted request with 403 (Cloudflare) while serving the same URL
normally in a browser, which is what these are for.
"""
import urllib.parse

# An identifier the pipeline stores, and the page it names. The field name is
# the key, so adding a resolver that stores a new id is a one-line change here
# and nothing else.
RECORD = {
    "deezer_id": ("deezer track", "https://www.deezer.com/track/{}"),
    "deezer_album_id": ("deezer album", "https://www.deezer.com/album/{}"),
    "recording_id": ("musicbrainz recording",
                     "https://musicbrainz.org/recording/{}"),
    "release_id": ("musicbrainz release",
                   "https://musicbrainz.org/release/{}"),
    "release_group_id": ("musicbrainz album",
                         "https://musicbrainz.org/release-group/{}"),
    "artist_id": ("musicbrainz artist", "https://musicbrainz.org/artist/{}"),
    # Not an id of a page but of a recording, and MusicBrainz publishes the
    # lookup: every recording anyone has filed under that ISRC, which is
    # exactly the question a contested identity is asking.
    "isrc": ("isrc", "https://musicbrainz.org/isrc/{}"),
}

# Where a source's own search lives. Keyed by source rather than by family:
# you look things up on a site, and Cover Art Archive has no search of its own
# even though MusicBrainz does.
SEARCH = {
    "deezer": "https://www.deezer.com/search/{q}",
    "musicbrainz": "https://musicbrainz.org/search?type=recording&query={q}",
    "lrclib": "https://lrclib.net/search/{q}",
    "discogs": "https://www.discogs.com/search/?q={q}",
    "genius": "https://genius.com/search?q={q}",
    "ytmusic": "https://music.youtube.com/search?q={q}",
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "itunes": "https://music.apple.com/search?term={q}",
    "lastfm": "https://www.last.fm/search?q={q}",
}

# Sources with no page worth opening. `seed`, `filename` and `tags` are the
# file itself; `human` is you; `analysis` and `whisper` are measurements of the
# audio. Listing them keeps `search()` honest: a missing entry in SEARCH means
# "not wired up yet", and these are "there is nothing there".
NO_SITE = {"seed", "filename", "tags", "human", "analysis", "whisper",
           "acoustid", "coverartarchive"}


def query(artist, title):
    """-> the text to search a site for, or None when there is nothing to ask.

    Artist and title, nothing else. Adding the album narrows a search that is
    already narrow enough and fails outright when the album is the thing that
    is wrong.

    A slash becomes a space. Half these templates put the query in the path,
    where an unescaped `/` is a different path and an escaped one is a 404:
    measured, `deezer.com/search/AC%2FDC...` answers 404 and Deezer has no
    query-parameter form that works instead. A slash carries no meaning in a
    search box in either case, and every one of these engines tokenises, so
    "AC DC" finds AC/DC.
    """
    words = " ".join(x for x in (artist, title) if x).replace("/", " ")
    return " ".join(words.split()) or None


def search(source, artist, title):
    """-> a URL onto this source's own search, or None.

    `safe=""` so nothing structural survives into a path-style template. The
    slash is already gone by here; this covers `?`, `#` and the rest.
    """
    template = SEARCH.get(source)
    q = query(artist, title)
    if not template or not q:
        return None
    return template.format(q=urllib.parse.quote(q, safe=""))


def records(found):
    """-> [(label, url), ...] for a {field: {value: [source, ...]}} mapping.

    Every distinct value gets a link, not the first one found. Two sources
    naming two different recordings is the disagreement, and picking one of
    them to link to would hide it behind a page that looks authoritative:
    silently dropping the loser is the habit this whole store exists to break.
    When a field has more than one, each label names the source that gave it,
    so the row says which record came from where instead of offering two links
    with the same name.

    Sorted by label so two runs over the same track produce the same dossier.
    A row that reorders every run is a row that cannot be diffed.
    """
    out = []
    for field, (label, template) in RECORD.items():
        values = found.get(field) or {}
        for value, sources in values.items():
            name = label if len(values) == 1 else f"{label} via {sources[0]}"
            out.append((name, template.format(
                urllib.parse.quote(str(value), safe=""))))
    return sorted(out)


def identifiers(conn, path):
    """-> {field: {value: [source, ...]}}: every identifier held for a track.

    Whoever answered. An id is a fact about the record it names, so which
    source found it does not change where it points, and the one that did find
    it is often not the one being questioned. The sources are kept anyway,
    because when two of them name different records that is the only thing
    that tells them apart.
    """
    from pipeline import evidence
    marks = ",".join("?" * len(RECORD))
    rows = conn.execute(
        f"SELECT field, value, source FROM observation WHERE track_path=? "
        f"AND state=? AND field IN ({marks}) AND value IS NOT NULL "
        f"ORDER BY field, value, source",
        (path, evidence.FOUND, *RECORD)).fetchall()
    out = {}
    for field, value, source in rows:
        out.setdefault(field, {}).setdefault(value, []).append(source)
    return out


def dossier(conn, path, artist=None, title=None, sources=()):
    """-> [(label, url), ...]: the records this track resolved, then searches.

    `sources` names which sites to offer a search for. Passing the ones that
    were actually asked keeps the list short and truthful: a link to a site
    nobody consulted invites you to do the pipeline's job by hand.
    """
    out = records(identifiers(conn, path))
    for source in sorted(set(sources) - NO_SITE):
        url = search(source, artist, title)
        if url:
            out.append((f"search {source}", url))
    return out
