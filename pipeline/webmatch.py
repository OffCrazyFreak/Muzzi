#!/usr/bin/env python3
"""Corroborate filename-derived names against the streaming catalogues.

MusicBrainz could not confirm 187 of these tracks, but that is a gap in
MusicBrainz, not evidence the names are wrong -- Balkan rap is thinly covered
there and very well covered on YouTube Music and Deezer. So this is NOT an
identification pass: a human already typed the artist and title into the
filename. All we want is a second opinion on that name, plus the album, year
and cover art that a filename cannot carry.

Sources, all free and keyless:

  YouTube Music  ytmusicapi, filter="songs" -- returns Art Tracks (the clean
                 single with its real album) rather than lyric videos
  Deezer         api.deezer.com/search, also gives cover art
  iTunes         itunes.apple.com/search, the tie-breaker
  YouTube        yt-dlp ytsearch. Holds everything the catalogues do not --
                 regional uploads, bootlegs, songs no distributor delivered.
                 A "- Topic" channel is an auto-generated Art Track, so the
                 channel name IS the artist and counts as real metadata; any
                 other upload is corroboration only.
  SoundCloud     yt-dlp scsearch, corroboration only (see below)

Grading:

  A  two or more catalogues agree on the name    0.90  auto
  B  exactly one confirms                        0.80  you confirm
  C  corroborated only, or nothing at all        0.70  unchanged

Two things learned from measuring this on the real library:

Duration is a tie-breaker, never a veto. These files are YouTube rips with
intros and outros the streaming single does not have, so `Elitni Odredi -
Nije Mi Zao` legitimately runs 21s longer than the Deezer track of the same
name. Gating on duration threw away a third of the correct answers.

SoundCloud never supplies metadata. Its "artist" is the uploader account
(`dageljic`, `Wave Music`, `Sara | VOICE TAGS`), so a SoundCloud hit only
says the song exists under this name. It raises nothing and overwrites
nothing; it just keeps the track out of the "nothing found" pile.

iTunes is called last and only when it can still change the grade, because
its rate limit (~20/min) is by far the tightest of the three.

Usage:
  webmatch.py                 # every filename-only and suspect track
  webmatch.py --limit 20
  webmatch.py --no-soundcloud
  webmatch.py --force         # ignore the cache
"""
import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from pipeline.identify import RateLimiter  # noqa: E402

REVIEW = os.path.join(HERE, "cache", "review.json")
GUESS = os.path.join(HERE, "cache", "filename_guess.json")
ANALYSIS = os.path.join(HERE, "cache", "analysis.json")
OUT = os.path.join(HERE, "cache", "webmatch.json")

CONF = {"A": 0.90, "B": 0.80, "C": 0.70}

# Deezer publishes 50 requests / 5s; YouTube Music publishes nothing, so it
# gets the same gentle rate. iTunes asks for about 20 a minute.
RATE_DEEZER = 8.0
RATE_YTMUSIC = 6.0
RATE_ITUNES = 0.33

_PAREN = re.compile(r"\s*[\(\[]\s*(?:rmx|remix|original mix|cover|live[^)\]]*|"
                    r"official[^)\]]*|prod\.?[^)\]]*|\d{4}[^)\]]*)\s*[\)\]]", re.I)
_PROD = re.compile(r"\s*\bprod\.?\s*(?:by)?\s+.*$", re.I)
_FEAT = re.compile(r"\s*(?:\(|\[)?\s*\b(?:feat|ft|featuring)\b\.?\s+.*$", re.I)


# ---------------------------------------------------------------- matching

# NFKD decomposes an accented letter into a base plus a combining mark, which
# is how c-caron and friends already fold to ASCII. These do not decompose:
# the stroke is part of the letter, not a mark on it. Without them, the letter
# survives NFKD and is then destroyed by the [^a-z0-9] pass, so a name is not
# merely mis-folded, it loses characters:
#
#   "Dorde Balasevic" written with strokes -> "or e balasevic"
#
# and fit() against the plain spelling scores 0.25, under the 0.5 gate every
# caller uses. 32 review rows carry the stroked d. Whisper's own text
# normalizer keeps the same table for the same reason.
# The replacement is the romanization the language actually uses, not the
# nearest single letter. Serbian and Croatian write d-with-stroke as "dj", so
# folding it to a bare "d" fixes the character loss and still misses: it turns
# the stroked spelling into "dorde" while the plain spelling everyone types is
# "djordje", and fit() scores that 0.33 -- under the gate, same as before.
#
# U+00F0 ETH is a different letter that looks identical in most fonts and is
# common mojibake for the stroked d. It stays "d" rather than "dj": it is
# genuinely Icelandic, and one library's mojibake is not a reason to mis-fold
# a letter. Measured cost of that choice here: one row, "A u Meðuvremenu",
# which folds to "meduvremenu" where the true spelling gives "medjuvremenu".
# If eth-for-d ever becomes common, fix the filenames, not this table.
_UNDECOMPOSED = str.maketrans({
    # Title case, not "DJ": norm() lowercases anyway, but lyrics_fetch reuses
    # this table to build search strings a human might have typed, and nobody
    # types "DJordje".
    "đ": "dj", "Đ": "Dj",   # d with stroke, Serbian/Croatian -> "dj"
    "ð": "d", "Ð": "D",     # eth, Icelandic
    "ł": "l", "Ł": "L",     # l with stroke, Polish
    "ø": "o", "Ø": "O",     # o with stroke, Norwegian/Danish
    "æ": "ae", "Æ": "AE",   # ash
    "œ": "oe", "Œ": "OE",   # ethel
    "ß": "ss",              # sharp s
    "þ": "th", "Þ": "TH",   # thorn
    # Same class, reached from Turkish and Maltese credits. Listed so the
    # character loss this table exists to stop cannot return through a letter
    # nobody thought of.
    "ı": "i", "İ": "I",     # dotless i / dotted I, Turkish
    "ħ": "h", "Ħ": "H",     # h with stroke, Maltese
    "ŧ": "t", "Ŧ": "T",     # t with stroke
})

# The agreement below which a name is a different name. It lives beside fit()
# because every caller that draws this line also calls fit(), and a copy in
# each of them is how the audit tool started grading on its own number.
MIN_FIT = 0.5

# How far a lyric sheet's own duration may sit from the recording's before its
# timings are refused. 2s is LRCLIB's /api/get signature tolerance, and what
# LRCLIBee and Music Assistant both require.
MAX_DURATION_DELTA = 2.0


# How a foreign word gets written in Serbian and Croatian. Not typos: these
# are the standard romanisations, so "Kawasaki" is filed as "Kavasaki" and
# "Kompleksi" as "Komplexi". lyrics_fetch imports this to build the search
# variants it asks catalogues for; it lives here so the questions and the
# comparison that judges the answers cannot drift apart, which is exactly what
# happened before: the query found the sheet under the other spelling and then
# fit() refused it for not matching the spelling we asked with.
_TRANSLIT = [("w", "v"), ("y", "j"), ("x", "ks"), ("qu", "kv"), ("ck", "k"),
             ("ph", "f"), ("th", "t")]


def norm(s):
    s = (s or "").lower().translate(_UNDECOMPOSED)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def translit(s):
    """-> the same normalised string romanised the Balkan way."""
    for a, b in _TRANSLIT:
        s = s.replace(a, b)
    return s


def _agree(w, g):
    if w == g:
        return 1.0
    if w in g or g in w:
        return 0.85
    ws, gs = set(w.split()), set(g.split())
    return len(ws & gs) / max(len(ws | gs), 1)


def fit(want, got):
    """0..1 agreement between two names, diacritic- and punctuation-blind.

    A second pass romanises both sides, but only when the first pass already
    says these are different names. Applied that way it can rescue a match and
    can never lower one, which matters because every caller that draws a line
    draws it here: raising a score is one more sheet kept, lowering one would
    silently change identity decisions across the pipeline.
    """
    w, g = norm(want), norm(got)
    if not (w and g):
        return 0.0
    score = _agree(w, g)
    if score < MIN_FIT:
        tw, tg = translit(w), translit(g)
        if (tw, tg) != (w, g):
            score = max(score, _agree(tw, tg))
    return score


def confirms(cand, artist, title):
    return fit(artist, cand.get("artist")) >= 0.6 and fit(title, cand.get("title")) >= 0.6


def artist_anywhere(cand, artist):
    """The filename may have put a featured artist in the lead slot, so look
    for it in the title and album too: `Lomi Mala (feat. DJ Buka)` is the
    right answer for a file named `DJ Buka - Lomi`."""
    a = norm(artist)
    hay = norm(f"{cand.get('artist')} {cand.get('title')} {cand.get('album')}")
    return bool(a) and a in hay


def pick(cands, artist, title, duration):
    """Best confirming candidate, duration only breaking ties."""
    best, best_rank = None, None
    for c in cands:
        if not confirms(c, artist, title):
            continue
        d = abs(duration - c["duration"]) if (duration and c.get("duration")) else 999
        rank = (fit(artist, c["artist"]) + fit(title, c["title"]), -d)
        if best_rank is None or rank > best_rank:
            best, best_rank = c, rank
    return best


# A file called "Animals (Balkanik Remix)" is not the recording called
# "Animals". The cleaned-query pass strips these words to widen the search,
# which makes the catalogues answer confidently with the ORIGINAL -- so
# whatever comes back has to be checked for the marker before it is believed.
_VERSION = re.compile(
    r"\b(remix|rmx|cover|parody|mashup|bootleg|flip|edit|acoustic|unplugged|"
    r"live|instrumental|karaoke|nightcore|sped\s*up|slowed|reverb|"
    r"vs\.?|refix|rework|mix)\b", re.I)


def version_words(s):
    return {m.group(1).lower().replace(".", "").replace(" ", "")
            for m in _VERSION.finditer(s or "")}


def version_mismatch(file_title, found_title):
    """-> the version words that only one side has, or None if they agree.

    Symmetric on purpose. A file named "Animals (Balkanik Remix)" matched to
    "Animals" is the wrong recording, and so is a file named "HONEY" matched
    to "Honey (Live 2022 Remix)". Either way the safe move is to keep the
    filename rather than inherit another recording's album."""
    want, got = version_words(file_title), version_words(found_title)
    # "Original Mix" labels the original, so it is not a version of its own.
    if "original" in (file_title or "").lower():
        want.discard("mix")
    if "original" in (found_title or "").lower():
        got.discard("mix")
    return sorted(want ^ got) or None


def variants(artist, title):
    """Alternative queries for when the plain one finds nothing."""
    t1 = _PROD.sub("", _PAREN.sub("", title)).strip(" -")
    a1 = _FEAT.sub("", artist).strip(" -")
    out, seen = [], {(artist, title)}
    for pair in ((a1, t1), (artist, t1), (a1, title)):
        if all(pair) and pair not in seen:
            seen.add(pair)
            out.append(pair)
    return out


# ---------------------------------------------------------------- sources

_YT = None
_YT_LOCK = threading.Lock()


def _ytmusic_client():
    global _YT
    with _YT_LOCK:
        if _YT is None:
            from ytmusicapi import YTMusic
            _YT = YTMusic()
        return _YT


def src_ytmusic(artist, title, lim):
    lim.wait()
    try:
        res = _ytmusic_client().search((artist + " " + title).strip(),
                                       filter="songs", limit=5)
    except Exception as e:
        return None, str(e)[:80]
    out = []
    for x in res[:5]:
        album = x.get("album")
        out.append({"artist": ", ".join(a["name"] for a in x.get("artists") or []),
                    "title": x.get("title"),
                    "album": album.get("name") if isinstance(album, dict) else None,
                    "year": x.get("year"),
                    "duration": x.get("duration_seconds"),
                    "video_id": x.get("videoId"),
                    "source": "ytmusic"})
    return out, None


def src_deezer(artist, title, lim, session):
    lim.wait()
    q = f'artist:"{artist}" track:"{title}"' if artist else title
    try:
        r = session.get("https://api.deezer.com/search",
                        params={"q": q, "limit": 5}, timeout=20)
        data = r.json().get("data") or []
    except Exception as e:
        return None, str(e)[:80]
    return [{"artist": x["artist"]["name"], "title": x["title"],
             "album": (x.get("album") or {}).get("title"),
             "year": None,
             "duration": x.get("duration"),
             "cover": (x.get("album") or {}).get("cover_xl"),
             "source": "deezer"} for x in data[:5]], None


def src_itunes(artist, title, lim, session):
    lim.wait()
    try:
        r = session.get("https://itunes.apple.com/search",
                        params={"term": f"{artist} {title}".strip(),
                                "media": "music", "entity": "song", "limit": 5},
                        timeout=20)
        data = r.json().get("results") or []
    except Exception as e:
        return None, str(e)[:80]
    return [{"artist": x.get("artistName"), "title": x.get("trackName"),
             "album": x.get("collectionName"),
             "year": (x.get("releaseDate") or "")[:4] or None,
             "duration": round((x.get("trackTimeMillis") or 0) / 1000) or None,
             "cover": (x.get("artworkUrl100") or "").replace("100x100", "600x600") or None,
             "source": "itunes"} for x in data[:5]], None


def src_youtube(artist, title):
    """Plain YouTube search, which holds a great deal that YouTube Music does
    not -- regional uploads, bootlegs, songs no distributor ever delivered.

    Two very different kinds of result come back, and they are not worth the
    same. A "- Topic" channel is an auto-generated Art Track, so the channel
    name IS the artist and the result is real metadata. Everything else is
    somebody's upload, where the channel is a channel and only the video title
    says anything about who made the song."""
    try:
        p = subprocess.run(
            ["yt-dlp", "--skip-download", "--no-warnings", "--flat-playlist",
             "--dump-json", f"ytsearch6:{artist} {title}"],
            capture_output=True, text=True, timeout=120)
    except Exception as e:
        return None, str(e)[:80]
    out = []
    for line in (p.stdout or "").splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        channel = (d.get("channel") or d.get("uploader") or "").strip()
        topic = channel.endswith("- Topic")
        out.append({"artist": channel[:-len("- Topic")].strip() if topic else None,
                    "title": d.get("title"),
                    "album": None,
                    "duration": d.get("duration"),
                    "channel": channel,
                    "art_track": topic,
                    "video_id": d.get("id"),
                    "source": "youtube"})
    return out, None


def src_soundcloud(artist, title):
    try:
        p = subprocess.run(
            ["yt-dlp", "--skip-download", "--no-warnings", "--flat-playlist",
             "--dump-json", f"scsearch5:{artist} {title}"],
            capture_output=True, text=True, timeout=90)
    except Exception as e:
        return None, str(e)[:80]
    out = []
    for line in (p.stdout or "").splitlines():
        try:
            d = json.loads(line)
        except Exception:
            continue
        out.append({"artist": d.get("uploader") or d.get("channel"),
                    "title": d.get("title"), "album": None,
                    "duration": d.get("duration"), "source": "soundcloud"})
    return out, None


# ---------------------------------------------------------------- one track

def match_one(artist, title, duration, ctx):
    """-> dict describing what the catalogues said about this name."""
    lims, session, use_sc = ctx["lims"], ctx["session"], ctx["soundcloud"]
    hits, errors, tried = {}, {}, []

    def try_source(name, cands, err, a, t):
        if err:
            errors[name] = err
            return
        tried.append(name)
        got = pick(cands or [], a, t, duration)
        if got and name not in hits:
            hits[name] = got

    yc, ye = src_ytmusic(artist, title, lims["ytmusic"])
    try_source("ytmusic", yc, ye, artist, title)
    dc, de = src_deezer(artist, title, lims["deezer"], session)
    try_source("deezer", dc, de, artist, title)

    # iTunes is the slowest and most rate-limited source, so only ask it when
    # its answer can still change the grade -- never to confirm an A twice.
    if len(hits) < 2:
        ic, ie = src_itunes(artist, title, lims["itunes"], session)
        try_source("itunes", ic, ie, artist, title)

    how = "direct"
    if not hits:
        # Cleaned-up queries: drop "(RMX)", "prod. by X", trailing "feat. Y".
        for va, vt in variants(artist, title):
            yc, ye = src_ytmusic(va, vt, lims["ytmusic"])
            try_source("ytmusic", yc, ye, va, vt)
            dc, de = src_deezer(va, vt, lims["deezer"], session)
            try_source("deezer", dc, de, va, vt)
            if hits:
                how = "cleaned query"
                break

    if not hits:
        # Title only, then demand the filename artist appear somewhere in the
        # credit. This is what catches a featured artist promoted to lead.
        plain = _PROD.sub("", _PAREN.sub("", title)).strip(" -")
        for name, fn in (("ytmusic", lambda: src_ytmusic("", plain, lims["ytmusic"])),
                         ("deezer", lambda: src_deezer("", plain, lims["deezer"], session))):
            cands, err = fn()
            if err:
                errors[name] = err
                continue
            for c in cands or []:
                if fit(plain, c.get("title")) >= 0.8 and artist_anywhere(c, artist):
                    hits[name] = c
                    how = "title only, artist found in credit"
                    break
            if hits:
                break

    # Plain YouTube, before SoundCloud: an Art Track there is as good as a
    # catalogue entry, and a plain upload is at least as good as a SoundCloud
    # one. This is where the regional and unofficial material lives.
    corroboration = None
    if not hits:
        cands, err = src_youtube(artist, title)
        if err:
            errors["youtube"] = err
        for c in cands or []:
            if c.get("art_track") and confirms(c, artist, title):
                hits["youtube"] = c          # a real artist credit
                how = "youtube art track"
                break
        if not hits:
            for c in cands or []:
                # No artist to check against, so the video title has to carry
                # both names itself before it counts for anything.
                whole = f"{c.get('title')} {c.get('channel')}"
                if fit(title, c.get("title")) >= 0.7 and artist_anywhere(
                        {"artist": whole, "title": "", "album": ""}, artist):
                    corroboration = c
                    break

    if not hits and corroboration is None and use_sc:
        cands, err = src_soundcloud(artist, title)
        if err:
            errors["soundcloud"] = err
        for c in cands or []:
            if confirms(c, artist, title) or (fit(title, c.get("title")) >= 0.7
                                              and artist_anywhere(c, artist)):
                corroboration = c
                break

    # Drop any hit that answered with a different version of the song. Doing
    # it here rather than at query time keeps the evidence in the cache.
    dropped = {}
    for name in list(hits):
        missing = version_mismatch(title, hits[name].get("title"))
        if missing:
            dropped[name] = {"title": hits[name].get("title"), "missing": missing}
            del hits[name]

    if len(hits) >= 2:
        grade = "A"
    elif len(hits) == 1:
        grade = "B"
    else:
        grade = "C"

    best = None
    for name in ("deezer", "itunes", "ytmusic"):   # album+cover first
        if name in hits:
            best = hits[name]
            break

    return {
        "grade": grade,
        "confidence": CONF[grade],
        "sources": sorted(hits),
        "how": how if hits else (
            f"{corroboration['source']} only" if corroboration else "nothing found"),
        # A confirming catalogue may spell the artist better than the
        # filename did, and on the title-only path it is outright more
        # correct, so the catalogue name wins whenever one confirmed.
        "artist": best["artist"] if best else artist,
        "title": best["title"] if best else title,
        "album": (best or {}).get("album"),
        "year": (best or {}).get("year"),
        "cover": next((h.get("cover") for n in ("deezer", "itunes")
                       for h in [hits.get(n) or {}] if h.get("cover")), None),
        "video_id": (hits.get("ytmusic") or {}).get("video_id"),
        "candidates": {k: v for k, v in hits.items()},
        "wrong_version": dropped or None,
        "corroboration": corroboration,
        "errors": errors or None,
        "queried": {"artist": artist, "title": title},
    }


# ---------------------------------------------------------------- driver

def targets(review, guess):
    """Every track whose name is still only as good as its filename."""
    out = []
    for path, g in guess.items():
        if g.get("status") == "filename-only":
            out.append({"path": path, "file": g["file"], "artist": g["artist"],
                        "title": g["title"], "why": "filename-only"})
    have = {t["path"] for t in out}
    for r in review:
        if r["path"] in have or r["tier"] != "suspect":
            continue
        if r.get("proposed_artist") and r.get("proposed_title"):
            out.append({"path": r["path"], "file": r["file"],
                        "artist": r["proposed_artist"],
                        "title": r["proposed_title"], "why": "suspect"})
    return out


def regrade():
    """Re-apply the grading rules to cached candidates. Every source's pick is
    kept in the cache, so a rule change costs nothing to re-run."""
    cache = json.load(open(OUT))
    moved, before = [], {"A": 0, "B": 0, "C": 0}
    after = dict(before)
    for path, w in cache.items():
        before[w["grade"]] += 1
        title = w["queried"]["title"]
        hits = dict(w.get("candidates") or {})
        dropped = {}
        for name in list(hits):
            missing = version_mismatch(title, hits[name].get("title"))
            if missing:
                dropped[name] = {"title": hits[name].get("title"), "missing": missing}
                del hits[name]
        grade = "A" if len(hits) >= 2 else ("B" if len(hits) == 1 else "C")
        best = next((hits[n] for n in ("deezer", "itunes", "ytmusic") if n in hits), None)
        if grade != w["grade"]:
            moved.append((w["file"], w["grade"], grade,
                          "/".join(sorted(dropped)),
                          "; ".join(f"{v['title']} (missing {','.join(v['missing'])})"
                                    for v in dropped.values())))
        w.update({
            "grade": grade, "confidence": CONF[grade], "sources": sorted(hits),
            "candidates": hits, "wrong_version": dropped or None,
            "artist": best["artist"] if best else w["queried"]["artist"],
            "title": best["title"] if best else title,
            "album": (best or {}).get("album"),
            "year": (best or {}).get("year"),
            "cover": next((hits[n].get("cover") for n in ("deezer", "itunes")
                           if hits.get(n, {}).get("cover")), None),
            "video_id": (hits.get("ytmusic") or {}).get("video_id"),
        })
        after[grade] += 1
    json.dump(cache, open(OUT + ".tmp", "w"), ensure_ascii=False, indent=1)
    os.replace(OUT + ".tmp", OUT)
    print(f"\n  {len(moved)} tracks re-graded because the catalogue answered "
          f"with a different version:\n")
    for f, old, new, srcs, why in sorted(moved):
        print(f"    {old}->{new}  {f[:52]:52} {why[:60]}")
    print(f"\n  before  A {before['A']}  B {before['B']}  C {before['C']}")
    print(f"  after   A {after['A']}  B {after['B']}  C {after['C']}\n")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-soundcloud", action="store_true")
    ap.add_argument("--workers", type=int)
    ap.add_argument("--regrade", action="store_true",
                    help="re-apply grading to the cache without re-querying")
    args = ap.parse_args()

    if args.regrade:
        return regrade()

    review = json.load(open(REVIEW))
    guess = json.load(open(GUESS)) if os.path.exists(GUESS) else {}
    dur = {v["path"]: v.get("decoded_secs")
           for v in json.load(open(ANALYSIS)).values()}

    todo = targets(review, guess)
    cache = {} if args.force else (json.load(open(OUT)) if os.path.exists(OUT) else {})
    todo = [t for t in todo if t["path"] not in cache]
    if args.limit:
        todo = todo[: args.limit]
    if not todo:
        print(f"  nothing to do ({len(cache)} already cached)\n")
        return 0

    # Network-bound, and every source has its own limiter, so oversubscribe
    # the cores rather than matching them.
    workers = args.workers or min(12, (os.cpu_count() or 4) * 2)
    ctx = {"lims": {"ytmusic": RateLimiter(RATE_YTMUSIC),
                    "deezer": RateLimiter(RATE_DEEZER),
                    "itunes": RateLimiter(RATE_ITUNES)},
           "session": requests.Session(),
           "soundcloud": not args.no_soundcloud}

    print(f"  {len(todo)} tracks to corroborate, {workers} workers "
          f"({len(cache)} cached)\n")
    lock = threading.Lock()
    done = {"n": 0, "A": 0, "B": 0, "C": 0}
    t0 = time.time()

    def work(t):
        res = match_one(t["artist"], t["title"], dur.get(t["path"]), ctx)
        res.update({"file": t["file"], "why": t["why"]})
        return t["path"], res

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(work, t) for t in todo]
        for f in as_completed(futs):
            try:
                path, res = f.result()
            except Exception as e:
                print(f"    worker failed: {str(e)[:100]}", flush=True)
                continue
            with lock:
                cache[path] = res
                done["n"] += 1
                done[res["grade"]] += 1
                n = done["n"]
                print(f"    [{n}/{len(todo)}] {res['grade']} {res['confidence']} "
                      f"{res['artist'][:26]:26} | {str(res['title'])[:30]:30} "
                      f"{'+'.join(res['sources']) or res['how']}", flush=True)
                if n % 20 == 0:
                    json.dump(cache, open(OUT + ".tmp", "w"), ensure_ascii=False, indent=1)
                    os.replace(OUT + ".tmp", OUT)

    json.dump(cache, open(OUT + ".tmp", "w"), ensure_ascii=False, indent=1)
    os.replace(OUT + ".tmp", OUT)
    n = max(done["n"], 1)
    print(f"\n  {done['n']} tracks in {time.time()-t0:.0f}s\n")
    print(f"  A  two or more catalogues agree : {done['A']:3}  ({100*done['A']/n:.0f}%)  auto")
    print(f"  B  one catalogue confirms       : {done['B']:3}  ({100*done['B']/n:.0f}%)  you confirm")
    print(f"  C  no catalogue confirms        : {done['C']:3}  ({100*done['C']/n:.0f}%)  filename kept")
    print(f"\n  -> {OUT}\n  now re-run review.py\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
