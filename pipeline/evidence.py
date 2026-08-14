#!/usr/bin/env python3
"""Every answer every source gave, kept instead of collapsed into a winner.

The stage caches hold conclusions: one artist, one album, one lyric sheet, and
in `cascade.json` the single source that established each. That is enough to
tag a file and not enough to know how much to trust it. Three independent
catalogues agreeing and one catalogue answering alone look identical once the
loser has been discarded, and the second is worth far less than the first.

So this stores observations rather than conclusions:

    "Deezer, asked artist=Grse title=Kavasaki, said the album is 'Rimoholik'."

Nothing here decides anything. Conclusions are computed from these rows, never
written back, so a fact that arrives later cannot leave a stale decision behind
it. That is the one property the JSON caches cannot have.

Three things make a row mean what it says:

`family` is the independence unit, and it is not the endpoint. Cover Art
Archive is MusicBrainz, so a cover it supplies is not a second opinion on a
MusicBrainz album. Deezer's own API and a Deezer-derived lyric mirror are one
family. Counting endpoints rather than families is how "three sources agree"
comes to mean "one database was read three ways".

`audio_derived` is separate from family, because AcoustID is two different
things at once: the fingerprint match is evidence from the audio and owes
nothing to anyone, while the artist name it returns came out of MusicBrainz.
The mechanism is independent, the metadata is not, and one flag per source
cannot say both.

`query_key` is what the source was actually asked. A miss belongs to the query,
not to the song: Genius having nothing for "Djordje Balasevic" says nothing
about "Đorđe Balašević", and once an alias turns up, that is a new question and
the old miss must not suppress it. Without this the fixpoint is merely cheap;
with it, it is correct.

State is not a boolean. An absence may be cached, a failure may not: an earlier
version of the lyric fetcher cached 132 failed requests as real answers, and
every one of those tracks was then treated as having no lyrics forever.

Usage:
  evidence.py --backfill        # seed from the existing stage caches
  evidence.py --stats
"""
import argparse
import json
import os
import sqlite3
import sys
import threading
import time
import unicodedata

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

CACHE = os.path.join(HERE, "cache")
# Named separately because tools/relocate.py has to recognise this file among
# the other caches, and a second spelling of it there would be a spelling that
# can drift.
DB_NAME = "evidence.db"
DB = os.path.join(CACHE, DB_NAME)
TABLE = "observation"
PATH_COLUMN = "track_path"

# What a source can have answered. A typo would otherwise create a ninth state
# that no reader matches and no reader reports, so the column is constrained
# and a bad write fails at the write.
FOUND = "FOUND"                       # it has this, and here it is
NO_MATCH = "NO_MATCH"                 # it was asked, and it has nothing
AMBIGUOUS = "AMBIGUOUS"               # several answers, none of them decisive
RATE_LIMITED = "RATE_LIMITED"         # ask again after its window
TEMPORARY_FAILURE = "TEMPORARY_FAILURE"   # network, 5xx, timeout
AUTH_FAILURE = "AUTH_FAILURE"         # key missing, expired or rejected
SOURCE_CHANGED = "SOURCE_CHANGED"     # answered, in a shape we no longer parse
NOT_APPLICABLE = "NOT_APPLICABLE"     # cannot be asked: no key for this query

STATES = (FOUND, NO_MATCH, AMBIGUOUS, RATE_LIMITED, TEMPORARY_FAILURE,
          AUTH_FAILURE, SOURCE_CHANGED, NOT_APPLICABLE)

# How long an answer may be reused before the source is asked the same question
# again. None means forever.
#
# A FOUND never expires on time: what makes it worth re-asking is a better
# question, and that arrives as a new query_key rather than as a clock. A
# NO_MATCH does expire, because LRCLIB gains lyrics weekly and a permanent
# absence would freeze a track out of every future run. Everything else is a
# failure wearing an answer's clothes and is retried soon.
TTL = {
    FOUND: None,
    NOT_APPLICABLE: None,
    NO_MATCH: 30 * 86400,
    AMBIGUOUS: 7 * 86400,
    SOURCE_CHANGED: 6 * 3600,
    TEMPORARY_FAILURE: 6 * 3600,
    AUTH_FAILURE: 3600,
    RATE_LIMITED: 3600,
}

# Which sources are the same evidence, however many endpoints they wear.
#
# Grouped by where the data ultimately comes from, not by who serves it:
#   musicbrainz  AcoustID resolves to MusicBrainz recordings, and the Cover Art
#                Archive is MusicBrainz's own artwork store. Neither
#                corroborates MusicBrainz.
#   google       YouTube Music is YouTube's catalogue with better metadata. An
#                Art Track and its own video are one upload seen twice.
#   local        What is already on disk: the file's tags, its name, and the
#                folder it arrived in. Frequently wrong, never independent of
#                each other, and the thing every lookup is trying to check.
FAMILY = {
    "acoustid": "musicbrainz",
    "musicbrainz": "musicbrainz",
    "coverartarchive": "musicbrainz",
    "listenbrainz": "musicbrainz",
    "discogs": "discogs",
    "deezer": "deezer",
    "itunes": "apple",
    "ytmusic": "google",
    "youtube": "google",
    "soundcloud": "soundcloud",
    "lastfm": "lastfm",
    "ncs": "ncs",
    "lrclib": "lrclib",
    "genius": "genius",
    "netease": "netease",
    "megalobiz": "megalobiz",
    "whisper": "audio",
    "analysis": "audio",
    "seed": "local",
    "filename": "local",
    "tags": "local",
    "folder": "local",
    "band": "local",
    "human": "human",
}

# Sources whose evidence comes from the audio itself. Independent of every
# catalogue by construction, which is what makes one of them worth more than
# another agreeing pair of databases. Separate from FAMILY because AcoustID is
# audio-derived about identity and MusicBrainz-derived about everything else.
AUDIO_DERIVED = {"acoustid", "whisper", "analysis"}

# A person's answer outranks every lookup, so it is the one family that may
# stand alone. Named rather than special-cased at each call site.
HUMAN = "human"

# review.json records agreement as a compound string: `web:deezer+ytmusic`,
# `musicbrainz+confirmed`, `filename+musicbrainz+confirmed`. That is this table
# flattened into a label, and unflattening it is most of what the backfill is
# for. The two names that are not catalogues:
#   confirmed     you said yes, or the value came from a link you gave
#   youtube-hint  resolved from a link you pasted
# Both are you, so both are the human family. Counting them as a catalogue
# would let your own answer corroborate itself.
_REVIEW_SOURCE = {"confirmed": HUMAN, "youtube-hint": HUMAN,
                  "filename-only": "filename", "filename": "filename"}


def review_sources(label):
    """-> the individual sources behind a review.json `source` string."""
    label = (label or "").replace("web:", "")
    out = []
    for part in label.split("+"):
        part = part.strip()
        if part:
            out.append(_REVIEW_SOURCE.get(part, part))
    return out

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS observation (
    track_path       TEXT    NOT NULL,
    field            TEXT    NOT NULL,
    source           TEXT    NOT NULL,
    family           TEXT    NOT NULL,
    query_key        TEXT    NOT NULL,
    state            TEXT    NOT NULL
        CHECK (state IN ({','.join('%r' % s for s in STATES)})),
    value            TEXT,
    value_norm       TEXT,
    matched_name     TEXT,
    matched_duration REAL,
    url              TEXT,
    first_seen       REAL    NOT NULL,
    seen_at          REAL    NOT NULL,
    PRIMARY KEY (track_path, field, source, query_key)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS observation_by_track ON observation (track_path);
CREATE INDEX IF NOT EXISTS observation_by_field ON observation (field, state);
"""

_local = threading.local()


def family_of(source):
    """-> the independence family a source belongs to.

    An unknown source becomes its own family rather than being folded into
    someone else's. Guessing wrong in the other direction would let a new
    source silently corroborate the one it was derived from.
    """
    return FAMILY.get(source, source)


def norm(value):
    """-> a form two answers can be compared on for identity.

    Deliberately blunt: case, accents, punctuation and runs of whitespace, and
    nothing else. Whether "Grše" and "Grse" are the same artist, or whether two
    lyric sheets differ only in a chorus repeat, is a question about a specific
    field and belongs with the code that answers it, not here. This exists so
    that byte-identical answers group, and nothing more.
    """
    if value is None:
        return None
    s = unicodedata.normalize("NFKD", str(value)).casefold()
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.split())


def connect(path=DB, readonly=False):
    """-> a connection this thread may use, made once per thread.

    One connection per thread is not a preference. Resolvers run in a
    ThreadPoolExecutor, and a connection shared across them is the documented
    way to get `database is locked` under exactly this load. WAL lets the
    readers carry on while one writer works, and busy_timeout absorbs the
    overlap that is left.
    """
    key = f"{path}:{readonly}"
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = _local.conns = {}
    conn = conns.get(key)
    if conn is not None:
        return conn

    os.makedirs(os.path.dirname(path), exist_ok=True)
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    else:
        conn = sqlite3.connect(path, timeout=30)
        conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conns[key] = conn
    return conn


def record(conn, track_path, field, source, query_key, state=FOUND,
           value=None, matched_name=None, matched_duration=None, url=None,
           now=None):
    """Write down what one source said when asked one question.

    A source asked the identical question can only hold one current answer, so
    re-asking replaces rather than accumulates: an answer that has since
    changed is stale, not extra evidence. `first_seen` moves only when the
    value actually changes, which is what makes a source that quietly started
    answering differently visible.

    A different question is a different row, always, even from the same source
    about the same field. That is the whole point of query_key.
    """
    if state not in STATES:
        raise ValueError(f"unknown state {state!r}; one of {STATES}")
    # A FOUND with nothing in it is a NO_MATCH that would read as an answer.
    if state == FOUND and value in (None, "", []):
        raise ValueError(f"FOUND with no value for {field!r} from {source!r}; "
                         f"use NO_MATCH")
    now = time.time() if now is None else now
    if isinstance(value, (list, dict, tuple)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    elif value is not None:
        value = str(value)

    with conn:                       # one implicit transaction, kept short
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO observation
                 (track_path, field, source, family, query_key, state, value,
                  value_norm, matched_name, matched_duration, url,
                  first_seen, seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (track_path, field, source, query_key) DO UPDATE SET
                 state            = excluded.state,
                 value            = excluded.value,
                 value_norm       = excluded.value_norm,
                 matched_name     = excluded.matched_name,
                 matched_duration = excluded.matched_duration,
                 url              = excluded.url,
                 seen_at          = excluded.seen_at,
                 first_seen       = CASE
                     WHEN observation.value IS excluded.value
                     THEN observation.first_seen ELSE excluded.first_seen END
            """,
            (track_path, field, source, family_of(source), query_key, state,
             value, norm(value), matched_name, matched_duration, url,
             now, now))


def fresh(row, now=None):
    """-> True while this answer may still be reused.

    Read off the state, never off the value. "LRCLIB has nothing" earns a month
    because LRCLIB really might not; "LRCLIB could not be reached" earns hours,
    because the song is not the thing that failed.
    """
    ttl = TTL.get(row["state"])
    if ttl is None:
        return True
    return (time.time() if now is None else now) - row["seen_at"] < ttl


def observations(conn, track_path=None, field=None, state=None):
    """-> [sqlite3.Row, ...], newest question last."""
    where, params = [], []
    for col, val in (("track_path", track_path), ("field", field),
                     ("state", state)):
        if val is not None:
            where.append(f"{col} = ?")
            params.append(val)
    sql = "SELECT * FROM observation"
    if where:
        sql += " WHERE " + " AND ".join(where)
    return conn.execute(sql + " ORDER BY field, family, source, first_seen",
                        params).fetchall()


def answered(conn, track_path, field):
    """-> the rows that actually carry a value, dropping absences and errors.

    Convenience with a point: a caller counting agreement must not count a
    NO_MATCH as a vote for nothing, and forgetting to filter is the easiest
    version of that mistake to make.
    """
    return [r for r in observations(conn, track_path, field)
            if r["state"] == FOUND]


def asked(conn, track_path, field, source, query_key):
    """-> the row for this exact question, or None.

    The gate a resolver checks before spending a request. A different
    query_key returns None on purpose: the question changed, so the old answer
    does not apply, whatever it said.
    """
    row = conn.execute(
        """SELECT * FROM observation
            WHERE track_path=? AND field=? AND source=? AND query_key=?""",
        (track_path, field, source, query_key)).fetchone()
    return row if (row is not None and fresh(row)) else None


# ----------------------------------------------------------------- relocating

class Collision(Exception):
    """Two tracks would become one. Refused rather than merged."""

    def __init__(self, a, b, new):
        super().__init__(f"{a!r} and {b!r} would both become {new!r}")


def plan_relocation(conn, rewrite):
    """-> [(old path, new path), ...] for a move, without touching anything.

    `rewrite` is the caller's prefix rule, taking a path and returning
    `(new, matched)`. Kept as a callback rather than reimplemented here so that
    the boundary matching, the longest-prefix ordering and every refusal
    tools/relocate.py already validates apply to this store identically. Two
    implementations of "did this path move" would eventually disagree, and the
    disagreement would look like a store that was simply missing some tracks.

    A collision is refused for the same reason the JSON walk refuses one:
    merging two tracks' evidence is a decision about which recording is which,
    not a mechanical rewrite, and last-wins would drop rows while the run still
    reported success.
    """
    paths = [r[0] for r in conn.execute(
        f"SELECT DISTINCT {PATH_COLUMN} FROM {TABLE}").fetchall()]
    moves, landing = [], {}
    unmoved = set(paths)
    for old in paths:
        new, hit = rewrite(old)
        if not hit:
            continue
        if new in landing:
            raise Collision(landing[new], old, new)
        landing[new] = old
        moves.append((old, new))
        unmoved.discard(old)
    # A path that did not move but is already sitting where a moved one is
    # headed is the same collision, one step removed.
    for new, old in landing.items():
        if new in unmoved:
            raise Collision(new, old, new)
    return moves


def apply_relocation(conn, moves, backup_to=None):
    """Rewrite the moved paths. -> the number of rows changed.

    Checkpointed before the backup on purpose: in WAL mode the newest rows can
    be sitting in `evidence.db-wal` rather than in `evidence.db`, so copying
    the one file without folding the log in first backs up a database that is
    missing exactly the work most likely to matter.
    """
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    if backup_to:
        import shutil
        shutil.copy2(conn.execute("PRAGMA database_list").fetchone()[2],
                     backup_to)
    changed = 0
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        for old, new in moves:
            cur = conn.execute(
                f"UPDATE {TABLE} SET {PATH_COLUMN} = ? WHERE {PATH_COLUMN} = ?",
                (new, old))
            changed += cur.rowcount
    return changed


# ------------------------------------------------------------------ backfill

def _load(name):
    p = os.path.join(CACHE, name)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


# The sources local_observations owns outright and rewrites in full. Named
# once so the delete above cannot fall behind the writes below: a source added
# to the writes and not to this tuple would keep its superseded rows for ever,
# which is the exact defect the delete exists to stop.
LOCAL_WRITTEN = ("tags", "band", "filename", "folder")


def local_observations(conn, paths, seeds, now=None):
    """Record what the file itself says, as three observations and not one.

    The tags, the filename and the folder are separate things that happen to
    live in the same place, and the pipeline has always fused them before
    anything could look at them: `tagseed.seed_for` returns one artist, one
    title and one trust label built out of `filename_artist or tag_artist`, so
    by the time a match is scored there is no way to ask which of the two said
    what, or whether they ever disagreed. That is the fusion #60 is about.

    They stay separate here, and they are all in the `local` family, so they
    can be read side by side without any of them corroborating another. That
    matters more than it sounds: a filename and the tags written by the tool
    that produced it are the same claim seen twice, and counting it twice is
    how a wrong download name becomes a confident answer.

    Deliberately narrow about which fields it will take from tags. Only artist
    and title, because those are the only ones tagseed measured. The tag album
    on these files is ALWAYS the uploading channel ("IDJVideos.TV", "Blockstar")
    and never a real album, so recording it as an `album` observation would put
    a channel name in a field no catalogue is contesting, where it would then
    win unopposed. Channel and label deserve to be recorded, as provenance and
    under their own names, and that needs a richer tag cache than exists.

    Nothing here can change an identity. `local` is in confidence.IGNORE_DISSENT,
    so these observations raise no penalty and cast no vote; they are what the
    review sheet reads to show you the disagreement rather than average it out.

    -> {"tags": n, "band": n, "filename": n, "folder": n} written.
    """
    from pipeline.probe_match import split_name
    counts = {"tags": 0, "band": 0, "filename": 0, "folder": 0}
    for path in paths:
        # Everything this file said last time, cleared before it says it
        # again. `query_key` carries the value for these sources, which is
        # right for a catalogue (a miss belongs to the query, so a better
        # query is a new row) and wrong for a file: "what do this file's tags
        # say" is ONE question whose answer changes when the file is retagged.
        # Keyed on the answer, a rewritten tag left the old reading sitting
        # beside the new one as a current FOUND, so `local_claims` showed
        # whichever it reached first and `local_disagreement` reported a file
        # as permanently disagreeing with itself.
        #
        # Scoped to the local sources on purpose. A catalogue's old answers
        # are evidence and are kept for ever; a superseded reading of a file
        # that is sitting right there is not evidence of anything.
        # Its own transaction, opened the way record() opens one. A bare
        # execute starts an implicit transaction that record()'s explicit
        # BEGIN IMMEDIATE then cannot open inside.
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM observation WHERE track_path = ? AND source IN "
                f"({','.join('?' * len(LOCAL_WRITTEN))})",
                (path, *LOCAL_WRITTEN))
        stem = os.path.splitext(os.path.basename(path))[0]

        # The filename, read as a name and not as a fallback. Both fields go in
        # even when the parse is only half a guess, because "the filename gave
        # a title and no artist" is a fact about the row worth seeing.
        fa, ft = split_name(stem)
        qk = f"filename:{stem}"
        for field, value in (("artist", fa), ("title", ft)):
            if value:
                record(conn, path, field, "filename", qk, FOUND, value=value,
                       now=now)
                counts["filename"] += 1

        # The tags, raw. `tag_artist` and not `artist`: the latter is already
        # the description or an override where one exists, which are different
        # sources with their own trust and would be laundered into "the tags
        # said so" by being recorded under this name.
        s = (seeds or {}).get(path) or {}
        raw_artist, raw_title = s.get("tag_artist"), s.get("title")
        if raw_artist or raw_title:
            qk = f"tags:{raw_artist}|{raw_title}"
            for field, value in (("artist", raw_artist), ("title", raw_title)):
                if value:
                    record(conn, path, field, "tags", qk, FOUND, value=value,
                           now=now)
                    counts["tags"] += 1

        # The band frame, kept apart from the artist frame rather than filling
        # in for it. Measured over the library: TPE1 carries an artist on 92
        # files and TPE2 on 1039, so for 1031 files this is the only artist
        # the file itself states and nothing has ever read it.
        #
        # Its own source, not a second `tags` observation, because the two
        # disagree about 263 names and the disagreement is the useful part.
        # Almost all of it is the same mojibake the titles have ("Oliver
        # Dragojeviæ", "Duko Lokin"), which is why this does not feed the seed:
        # the ć survives as æ and can be undone, but the š and ž were dropped
        # outright and no transformation brings them back, so the filename is
        # genuinely the better name. Almost, though, is not all. "PSY" sits in
        # TPE2 of a file the filename calls "Gangam Style", where the tag is
        # right and the filename put the title in the artist's place. That row
        # is worth a person seeing, and until now nothing could show it.
        band = s.get("tag_band")
        if band:
            record(conn, path, "artist", "band", f"band:{band}", FOUND,
                   value=band, now=now)
            counts["band"] += 1

        # The folder, as the collection it is and not as identity. Measured
        # before writing this: all 1723 files here sit exactly one level below
        # the music root, in five buckets, and three of them ("Music Mine",
        # "Music Other", "YT Music to download") name nothing at all. The two
        # that do name something name a label, not an artist or a title, and
        # #60 is explicit that an NCS-looking folder must not prove an NCS
        # release. So it is recorded under its own field, where it is a search
        # trigger and a cohort label and can never be mistaken for a name.
        folder = os.path.basename(os.path.dirname(path))
        if folder:
            record(conn, path, "collection", "folder", f"folder:{folder}",
                   FOUND, value=folder, now=now)
            counts["folder"] += 1
    return counts


def backfill(conn, verbose=True):
    """Seed the store from the caches that already know who said what.

    Read-only over `cache/`. Only the caches that record a source per value are
    worth importing: a conclusion with no provenance cannot become evidence,
    and inventing one would be worse than leaving the field empty.

    The query keys written here are reconstructed, not remembered, because the
    old caches never stored the question. They are marked so, so a later run
    cannot mistake a reconstruction for the real thing and skip a lookup that
    was never actually made.
    """
    counts = {"cascade": 0, "webmatch": 0, "lyrics": 0, "review": 0}

    # cascade.json is the closest thing to this store that already exists: one
    # source per field, per track, and a trail of how each arrived.
    for path, entry in (_load("cascade.json") or {}).items():
        facts = entry.get("facts") or {}
        sources = entry.get("sources") or {}
        seed = entry.get("seed") or {}
        qk = f"backfill:cascade:{seed.get('artist')}|{seed.get('title')}"
        for field, value in facts.items():
            src = sources.get(field) or "seed"
            if value in (None, "", []):
                continue
            record(conn, path, field, src, qk, FOUND, value=value)
            counts["cascade"] += 1

    # webmatch.json already stores every candidate per source alongside the
    # query that produced it, which is this table in miniature.
    for path, entry in (_load("webmatch.json") or {}).items():
        q = entry.get("queried") or {}
        qk = f"webmatch:{q.get('artist')}|{q.get('title')}"
        for src, cand in (entry.get("candidates") or {}).items():
            if not isinstance(cand, dict):
                continue
            for field in ("artist", "title", "album", "year"):
                if cand.get(field) in (None, "", []):
                    continue
                record(conn, path, field, src, qk, FOUND,
                       value=cand[field],
                       matched_name=f"{cand.get('artist')} - "
                                    f"{cand.get('title')}",
                       matched_duration=cand.get("duration"))
                counts["webmatch"] += 1
        # An error is not an absence, and webmatch already kept them apart.
        for src in (entry.get("errors") or {}):
            record(conn, path, "artist", src, qk, TEMPORARY_FAILURE)
            counts["webmatch"] += 1

    # lyrics.json is keyed by artist|title rather than by path, which is
    # exactly the query key this table wants, so it maps across cleanly. The
    # track it belongs to is whichever review row asks that question.
    by_key = {}
    for row in (_load("review.json") or []):
        a = row.get("proposed_artist") or ""
        t = row.get("proposed_title") or ""
        if row.get("path"):
            by_key.setdefault(f"{a}|{t}".lower(), []).append(row["path"])
    for key, entry in (_load("lyrics.json") or {}).items():
        if not isinstance(entry, dict):
            continue
        # Entries predate the `source` field, and every one of them came from
        # LRCLIB because nothing else was asked yet. Recorded as lrclib rather
        # than as unknown, because "unknown" would be a family of its own and
        # would corroborate everything.
        src = entry.get("source") or "lrclib"
        state = NO_MATCH if entry.get("status") == "absent" else FOUND
        for path in by_key.get(key, []):
            for field, val in (("lyrics_synced", entry.get("synced")),
                               ("lyrics_plain", entry.get("plain"))):
                if state == FOUND and not val:
                    continue
                record(conn, path, field, src, f"lyrics:{key}", state,
                       value=val if state == FOUND else None,
                       matched_name=entry.get("matched"),
                       matched_duration=entry.get("matched_duration"))
                counts["lyrics"] += 1

    # The identity the pipeline settled on, and every source that agreed on it.
    # One row per source rather than one per row: `musicbrainz+confirmed` is
    # two independent things saying the same name, and collapsing it back to a
    # single label is what this whole table exists to stop.
    for row in (_load("review.json") or []):
        path = row.get("path")
        srcs = review_sources(row.get("source"))
        if not path or not srcs:
            continue
        qk = f"review:{row.get('file')}"
        for field, val in (("artist", row.get("proposed_artist")),
                           ("title", row.get("proposed_title")),
                           ("album", row.get("proposed_album")),
                           ("year", row.get("proposed_year"))):
            if val in (None, "", []):
                continue
            for src in srcs:
                record(conn, path, field, src, qk, FOUND, value=val)
                counts["review"] += 1

    if verbose:
        for k, v in counts.items():
            print(f"    {k:10s} {v:7d} observations")
    return counts


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backfill", action="store_true",
                    help="seed from the existing stage caches, read-only")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    conn = connect(args.db)
    if args.backfill:
        print(f"\n  backfilling {args.db} from {CACHE}\n")
        backfill(conn)

    if args.stats or args.backfill:
        n = conn.execute("SELECT COUNT(*) FROM observation").fetchone()[0]
        tracks = conn.execute(
            "SELECT COUNT(DISTINCT track_path) FROM observation").fetchone()[0]
        print(f"\n  {n} observations over {tracks} tracks\n")
        print("  by family:")
        for r in conn.execute(
                """SELECT family, COUNT(*) n, COUNT(DISTINCT source) srcs
                     FROM observation GROUP BY family ORDER BY n DESC"""):
            print(f"    {r['family']:14s} {r['n']:7d}  "
                  f"({r['srcs']} source{'s' if r['srcs'] > 1 else ''})")
        print("\n  by state:")
        for r in conn.execute(
                """SELECT state, COUNT(*) n FROM observation
                    GROUP BY state ORDER BY n DESC"""):
            print(f"    {r['state']:20s} {r['n']:7d}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
