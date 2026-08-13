#!/usr/bin/env python3
"""Turn YouTube-URL hints into real metadata.

A URL is the cheapest thing to supply for a track nothing could name: paste the
video you know it is and let the machine do the rest. Two steps:

  1. yt-dlp reads the video's own metadata. Auto-generated "Art Track" uploads
     carry a description beginning "Provided to YouTube by <distributor>"
     followed by "<title> - <artist>", which names the performer outright.
     Otherwise the video title is split on the usual separators and the
     uploader channel is the fallback artist.
  2. That artist/title is looked up on MusicBrainz so the track ends up with a
     real album, year and recording MBID rather than just two strings.

Step 2 is best-effort: if MusicBrainz has nothing, the YouTube-derived names
are still used. A hinted track should never come out worse than the hint.

Results cache to cache/hint_youtube.json keyed by video id, so re-running is
free and a mistyped URL can be corrected without re-fetching everything else.

Usage:
  hints_resolve.py            # resolve every URL hint in the review queues
  hints_resolve.py --limit 5
"""
import argparse
import json
import os
import re
import subprocess
import sys
from urllib.parse import unquote_plus

import requests

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from pipeline import review as review_mod  # noqa: E402
from pipeline.textsearch import (MB_RATE, MB_URL, RateLimiter,  # noqa: E402
                                 get_with_retry, mb_escape, best_release)

OUT = os.path.join(HERE, "cache", "hint_youtube.json")
from pipeline.useragent import UA  # noqa: E402

_VID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})")
# "Provided to YouTube by X\n\nTitle - Artist\n"
_PROVIDED = re.compile(r"\n\n(.+?)\s+·\s+(.+?)\n")
# "|" is a separator too: "Lucidious | Where'd You Go Remix" produced a title
# that repeated the artist inside itself.
_SPLIT = re.compile(r"\s+[-–—]\s+|\s*：\s*|\s+:\s+|\s*\|\s*")

# A left side wrapped in quotes: the uploader is naming the song, not the act.
_QUOTES = "\"'\u2018\u2019\u201c\u201d\u00ab\u00bb"
# A right side that describes the recording instead of naming it. "A ... parody
# of", "a cover of", "a remix of" and the bare forms of each.
_DESCRIBES = re.compile(
    r"^\s*(?:an?\s+[\w' ]{0,40}?)?"
    r"(?:parody|cover|remix|tribute|mashup|medley|rendition)\b", re.I)


def _quoted(s):
    s = (s or "").strip()
    return len(s) > 2 and s[0] in _QUOTES and s[-1] in _QUOTES


def _unquote(s):
    return (s or "").strip().strip(_QUOTES).strip()
# The same YouTube title cruft the filename parser strips.
_CRUFT = re.compile(
    r"""\s*[\(\[\|]?\s*
        (?:official\s*(?:music\s*)?(?:video|audio|lyric\s*video)
          |lyrics?\s*video|lyric\s*video|lyrics?|official|audio|video
          |hq|hd|4k|full\s*hd|visualizer|mv|remaster(?:ed)?(?:\s*\d{4})?
          |tekst|prevod|spot|u[zxž]ivo)
        \s*[\)\]\|]?\s*""", re.I | re.X)

# The same words as whole tokens, plus the ones that only ever appear inside a
# bracketed group. Used to decide whether a WHOLE group is decoration, which
# the per-token rule above cannot do: it removes "(lyrics" and "tekst)" one at
# a time and leaves the "/" between them behind, at confidence 1.0, in the
# title tag, and as "_" in the filename.
_CRUFT_TOKEN = re.compile(
    r"""^(?:official|music|video|audio|lyrics?|lyric|letra|text|tekst|prevod
         |spot|hq|hd|4k|uhd|visualizer|mv|remaster(?:ed)?|live|u[zxž]ivo
         |full|version|versi[oó]n|oficial|hq/hd|(?:19|20)\d{2})$""",
    re.I | re.X)
# One bracketed group with no nesting. Applied repeatedly, so "(a) [b]" both go.
_GROUP = re.compile(r"[\(\[]([^()\[\]]*)[\)\]]")
# What separates tokens inside a group. "/" is here, and only here: a slash
# between two cruft words is punctuation, while AC/DC and Love/Hate are names.
_IN_GROUP_SEP = re.compile(r"[\s/,;:|.–—_+-]+")


# An uploader's release-channel slogan, tacked on after a pipe. NCS names its
# uploads "Title | Genre | NCS - Copyright Free Music", and only the title is
# the title. Recognised by the slogan itself, never by the pipe alone: a pipe
# is legal in a name and the genre segment beside it is not evidence of
# anything on its own.
#
# Every phrase here says something about licensing, which is what makes it a
# channel slogan rather than words. A bare "Free Music" is deliberately not
# one of them: it is ordinary enough to be part of a name, and a slogan that
# means it always carries "copyright", "royalty" or the channel's own name.
# The whole segment has to be the slogan, optionally after a short channel
# name and a dash, so a title segment that merely contains the words survives.
_SLOGAN = re.compile(
    r"^\s*(?:[^|]{1,24}?\s*[-–—]\s*)?"
    r"(?:copyright[\s-]*free|no[\s-]*copyright|ncs[\s-]*release"
    r"|royalty[\s-]*free|free[\s-]*download)"
    r"(?:\s*(?:music|download|release))?\s*$", re.I)


def _drop_slogan_tail(title):
    """-> the title with a pipe-separated uploader slogan removed.

    Everything from the FIRST pipe is dropped, but only when a whole segment
    after it is a slogan. "Titsepoken 2015 | Electro | NCS - Copyright Free
    Music" keeps "Titsepoken 2015", including the year, which is part of that
    track's real name. The genre segment goes with the slogan on purpose:
    "Title | Genre | Slogan" is one uploader's naming scheme, and the genre
    is no more part of the title than the slogan is.
    """
    parts = title.split("|")
    if len(parts) < 2:
        return title
    if any(_SLOGAN.match(p) for p in parts[1:]):
        return parts[0]
    return title


def _drop_cruft_groups(title):
    """-> the title with every wholly-decorative bracketed group removed.

    A group goes only when EVERY token in it is a known cruft word, a year, or
    a separator. "(Lyrics/Tekst)" and "[OFFICIAL HQ VIDEO / SPOT]" go;
    "(Payphone Parody)" and "(Bvrnout Remix)" stay, because they carry words
    this does not recognise and a title is not ours to invent.
    """
    def repl(m):
        toks = [t for t in _IN_GROUP_SEP.split(m.group(1)) if t]
        # "not toks" as well: a nested group whose inside was already removed
        # leaves an empty one behind, and "Song [ ]" is not a cleaner title
        # than "Song [(Lyrics/Tekst)]" was.
        if not toks or all(_CRUFT_TOKEN.match(t) for t in toks):
            return " "
        return m.group(0)

    prev = None
    while prev != title:
        prev, title = title, _GROUP.sub(repl, title)
    return title


def video_id(url):
    m = _VID.search(url or "")
    return m.group(1) if m else None


# "youtube.com/results?search_query=Elitni+Odredi+-+Krivi+Smo+Oboje" is a
# perfectly reasonable thing to paste when you could not find a single video.
_SEARCH = re.compile(r"youtube\.com/results\?[^ ]*search_query=([^&\s]+)", re.I)
# Any other http link -- Audiomack, Soundcloud, Bandcamp. yt-dlp knows about
# a thousand sites, so there is no reason to only accept YouTube.
_ANY_URL = re.compile(r"https?://\S+")


def search_query(text):
    m = _SEARCH.search(text or "")
    if not m:
        return None
    return unquote_plus(m.group(1))


def other_url(text):
    """First non-YouTube link in the hint, if there is no video id to use."""
    for u in _ANY_URL.findall(text or ""):
        if not _VID.search(u) and not _SEARCH.search(u):
            return u.rstrip(".,;)")
    return None


_META = re.compile(
    r'<meta[^>]+(?:property|name)="([^"]+)"[^>]+content="([^"]*)"', re.I)
_LD = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>',
                 re.S | re.I)
# "TITLE by ARTIST: Listen on Audiomack", "TITLE by ARTIST | Bandcamp"
_OG_BY = re.compile(r"^(.*?)\s+by\s+(.+?)(?:\s*[:|].*)?$", re.I)


def from_page_metadata(url):
    """-> our shape from a page's OpenGraph/schema.org, or None.

    Deliberately conservative: it reads only what a site states about itself
    in a standard format. Anything requiring a guess about page layout would
    break the next time the site is redesigned, which is the failure this
    exists to work around in the first place.
    """
    try:
        import requests
        r = requests.get(url, timeout=25, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"})
        if r.status_code != 200:
            return None
        html = r.text
    except Exception:
        return None

    meta = {k.lower(): v for k, v in _META.findall(html)}
    title = artist = None
    for blob in _LD.findall(html):
        try:
            d = json.loads(blob)
        except Exception:
            continue
        for node in (d if isinstance(d, list) else [d]):
            if not isinstance(node, dict) or "Music" not in str(node.get("@type")):
                continue
            title = title or node.get("name")
            by = node.get("byArtist")
            if isinstance(by, dict):
                artist = artist or by.get("name")

    og = meta.get("og:title") or ""
    m = _OG_BY.match(og)
    if m:
        # "X by Y" names the real artist where byArtist often names the
        # uploading account instead.
        title = m.group(1).strip() or title
        artist = m.group(2).strip()
    if not (title and artist):
        return None
    dur = meta.get("music:duration")
    return {
        "video_title": title,
        "channel": artist,
        "description": meta.get("og:description") or "",
        "duration": float(dur) if (dur or "").isdigit() else None,
        "album": meta.get("music:album"),
        "from_page": True,
    }


def fetch_url(url):
    """yt-dlp against any supported site, or a search, returning our shape."""
    try:
        p = subprocess.run(
            ["yt-dlp", "--skip-download", "--no-warnings", "--no-playlist",
             "--dump-single-json", url],
            capture_output=True, text=True, timeout=180)
    except FileNotFoundError:
        return {"error": "yt-dlp is not installed"}
    except subprocess.TimeoutExpired:
        return {"error": "yt-dlp timed out"}
    if p.returncode != 0:
        # yt-dlp supports a site until the site changes. Its Audiomack
        # extractor has returned 404 since that API moved, which made a
        # perfectly good link unusable. Most music pages still publish the
        # same facts as OpenGraph and schema.org, so read those rather than
        # give up on the link.
        scraped = from_page_metadata(url)
        if scraped:
            return scraped
        return {"error": (p.stderr or "").strip().splitlines()[-1][:140]
                if p.stderr else f"yt-dlp exit {p.returncode}"}
    try:
        d = json.loads(p.stdout)
    except Exception:
        return {"error": "yt-dlp returned no JSON"}
    if d.get("_type") == "playlist":
        entries = [e for e in (d.get("entries") or []) if e]
        if not entries:
            return {"error": "search returned nothing"}
        d = entries[0]
    return {
        "video_id": d.get("id"),
        "video_title": d.get("title"),
        "channel": d.get("channel") or d.get("uploader"),
        "description": (d.get("description") or "")[:4000],
        "duration": d.get("duration"),
        # yt-dlp fills these from the site's own music metadata when it has
        # any, and they beat anything parsed out of a title.
        "meta_artist": d.get("artist") or d.get("creator"),
        "meta_title": d.get("track"),
        "meta_album": d.get("album"),
        "extractor": d.get("extractor_key"),
    }


# Trailing noise an uploader adds after the song name. "Oficial" is Spanish
# and Portuguese, and the English list above misses it entirely.
_TAIL = re.compile(
    r"\s*[\(\[]?\s*(?:oficial|oficjalne|officiel|resmi|napisy|"
    r"из\s+фильма|from\s+\S+.*)\s*[\)\]]?\s*$", re.I)


def clean_title(t):
    if not t:
        return t
    # Whole decorative groups first. Left to the per-token rule below, the
    # words inside "(lyrics/tekst)" vanish one at a time and the separator
    # between them survives.
    grouped = _drop_cruft_groups(t)
    out = _drop_slogan_tail(grouped)
    after_cruft = _CRUFT.sub(" ", out)
    # Did this title actually carry decoration? Recorded before the tidying
    # rules below run, because those change almost every title and would make
    # the test meaningless.
    cruft_removed = (grouped != t) or (after_cruft != out)
    out = _TAIL.sub("", after_cruft)
    # A release year tacked onto the title. Uploaders write "Drugu neću -
    # 2010", and it is enough to stop the song matching the same song without
    # it, which puts two copies in the library.
    out = re.sub(r"\s*[-–—(\[]\s*(?:19|20)\d{2}\s*[)\]]?\s*$", "", out)
    # A bare trailing year, but ONLY when cleaning this title just removed
    # decoration. "GASTTOZZ - ARITMIJE (OFFICIAL VIDEO) 2018" is an upload
    # year sitting where the group used to be; "Sarvagon 2015" and "Magla Bend
    # - Samo se nocas pojavi 2018" carry no cruft at all, and NCS really does
    # call that track "Sarvagon 2015". Without evidence that the year is
    # decoration, it stays: a wrong title is worse than an untidy one, and
    # deciding otherwise needs a catalogue lookup this stage does not do.
    if cruft_removed:
        out = re.sub(r"\s+(?:19|20)\d{2}\s*$", "", out)
    # Uploaders quote the song name -- "Búscame" -- and the quotes then become
    # underscores in the filename, because a quote is not legal in one.
    out = out.strip().strip('"“”«»‘’')
    # A closing bracket whose opener the cruft rules already removed.
    if out.count(")") > out.count("("):
        out = out.rstrip(") ")
    if out.count("]") > out.count("["):
        out = out.rstrip("] ")
    # "/" is stripped at the ENDS only, never from the middle: a slash left
    # dangling by the cleanup is punctuation, and AC/DC and Love/Hate are
    # names. Filename sanitising and metadata normalisation are different
    # jobs, and safe_name() mapping "/" to "_" only hid this one.
    out = re.sub(r"\s{2,}", " ", out).strip(" -_|.–—/")
    return out or t


def fetch_video(vid):
    """-> dict with title/channel/description/duration, or {'error': ...}."""
    url = f"https://www.youtube.com/watch?v={vid}"
    try:
        p = subprocess.run(
            ["yt-dlp", "--skip-download", "--no-warnings", "--dump-single-json", url],
            capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        return {"error": "yt-dlp is not installed"}
    except subprocess.TimeoutExpired:
        return {"error": "yt-dlp timed out"}
    if p.returncode != 0:
        return {"error": (p.stderr or "").strip().splitlines()[-1][:140]
                if p.stderr else f"yt-dlp exit {p.returncode}"}
    try:
        d = json.loads(p.stdout)
    except Exception:
        return {"error": "yt-dlp returned no JSON"}
    return {
        "video_id": vid,
        "video_title": d.get("title"),
        "channel": d.get("channel") or d.get("uploader"),
        "description": (d.get("description") or "")[:4000],
        "duration": d.get("duration"),
    }


def names_from(info):
    """-> (artist, title, how). The description is authoritative when present."""
    # yt-dlp's own artist/track fields come from the site's music metadata,
    # so when they exist they beat every heuristic below.
    if info.get("meta_artist") and info.get("meta_title"):
        return info["meta_artist"], info["meta_title"], "site metadata"

    desc = info.get("description") or ""
    if "Provided to YouTube" in desc:
        m = _PROVIDED.search(desc)
        if m:
            # "Artist - Composer - Lyricist": only the first field is the artist.
            return (m.group(2).split("·")[0].strip(),
                    m.group(1).strip(), "description")

    title = clean_title(info.get("video_title") or "")
    parts = [p.strip() for p in _SPLIT.split(title) if p.strip()]
    if len(parts) >= 2:
        # A quoted left side plus a description on the right is not a credit.
        # '"This is my Biome" - A Minecraft Parody of Maroon 5 Payphone' was
        # read as artist '"This is my Biome"' at confidence 1.0, which is the
        # song naming itself and the description becoming the title. The left
        # side is the title here; the artist has to come from somewhere that
        # actually knows it.
        # Tested on the RAW title: clean_title strips the opening quote, so by
        # here the left side reads 'This is my Biome"' and no longer looks
        # quoted at all. '"Weird Al" Yankovic' is not caught, because it does
        # not END on a quote: that really is the artist.
        raw = [p.strip() for p in
               _SPLIT.split(info.get("video_title") or "") if p.strip()]
        if (raw and _quoted(raw[0])
                and _DESCRIBES.match(" - ".join(parts[1:]))):
            said = _unquote(raw[0])
            chan = (info.get("channel") or "").replace(" - Topic", "").strip()
            if chan:
                return chan, said, "quoted title, uploader"
            # No channel to fall back on, so say the title and refuse to name
            # an artist. review.py scores a hint with no artist below the auto
            # bar, which is where a guess belongs.
            return None, said, "quoted title, artist unknown"
        return parts[0], " - ".join(parts[1:]), "video title"

    # No separator in the title. The channel is the last thing to believe --
    # it is how "Amazing Lyrics" and "Release" became artists -- so read the
    # description first, where uploaders routinely write the credit out.
    a, t, how = _from_description(desc, title)
    if a:
        return a, t or title, how

    # A dash with no spaces around it is still a separator. "Prljavo
    # Kazaliste-Zbog mene ne placi" fell all the way through to the channel,
    # so the band became "lusy1995" -- the uploader -- while its actual name
    # sat in the title. Guarded so hyphenated words are not torn in half:
    # exactly one dash, both sides substantial, at least one multi-word.
    bare = re.split(r"\s*[-–—]\s*", title)
    if len(bare) == 2:
        left, right = bare[0].strip(), bare[1].strip()
        if (len(left) >= 4 and len(right) >= 4
                and (" " in left or " " in right)):
            return left, right, "video title"

    channel = (info.get("channel") or "").replace(" - Topic", "").strip()
    return (channel or None), strip_artist(title, channel) or (title or None), \
        "channel + title"


def strip_artist(title, artist):
    """-> the title with a leading artist name removed.

    Uploaders write the artist into the title as well as the artist field --
    "ELDORADO   DAJ MI SNAGU by Eldorado" -- and without this the track ends
    up called "Eldorado Daj Mi Snagu" by Eldorado.
    """
    if not (title and artist):
        return title
    t, a = title.strip(), artist.strip()
    if len(t) > len(a) and t[:len(a)].casefold() == a.casefold():
        rest = t[len(a):].lstrip(" -–—:|.").strip()
        if len(rest) >= 3:
            return rest
    return t


# "Artist - Title", "Artist – Title", "Izvodjac: X", "Music: X", "Song: Y".
_DESC_PAIR = re.compile(r"^\s*(.{2,60}?)\s+[-–—]\s+(.{2,80}?)\s*$")
_DESC_FIELD = re.compile(
    r"^\s*(?:artist|izvo[dđ]a[cč]|izvodjac|pjeva|peva|performer|music|muzika|"
    r"vokal|song|pjesma|pesma|naslov|title|tekst)\s*[:\-]\s*(.{2,80}?)\s*$", re.I)
_ARTISTY = re.compile(
    r"^(?:artist|izvo[dđ]a[cč]|izvodjac|pjeva|peva|performer|music|muzika|vokal)",
    re.I)


def _from_description(desc, title):
    """-> (artist, title, how). Reads the credits uploaders type by hand."""
    lines = [ln.strip() for ln in (desc or "").splitlines() if ln.strip()][:25]
    field_artist = field_title = None
    for ln in lines:
        m = _DESC_FIELD.match(ln)
        if not m:
            continue
        if _ARTISTY.match(ln) and not field_artist:
            field_artist = m.group(1).strip()
        elif not field_title:
            field_title = m.group(1).strip()
    if field_artist:
        return field_artist, field_title, "description field"

    tnorm = clean_title(title or "").lower()
    for ln in lines:
        m = _DESC_PAIR.match(ln)
        if not m:
            continue
        left, right = m.group(1).strip(), m.group(2).strip()
        # Only trust the line when one side is the song we already know about,
        # otherwise a random "Mix - by DJ" line would become the credit.
        if tnorm and tnorm in right.lower():
            return left, right, "description line"
        if tnorm and tnorm in left.lower():
            return right, left, "description line"
    return None, None, None


def enrich_mb(artist, title, limiter, session):
    """Best-effort MusicBrainz lookup so a hint yields album/year/MBID too."""
    if not (artist and title):
        return None
    q = f'recording:"{mb_escape(title)}" AND artist:"{mb_escape(artist)}"'
    try:
        r = get_with_retry(session, MB_URL, limiter,
                           params={"query": q, "fmt": "json", "limit": 5},
                           headers=UA)
        if r.status_code != 200:
            return None
        recs = r.json().get("recordings") or []
    except Exception:
        return None
    if not recs:
        return None
    rec = recs[0]
    arts = [a["artist"]["name"] for a in rec.get("artist-credit", [])
            if isinstance(a, dict) and a.get("artist")]
    rel = best_release(rec) or {}
    return {
        "recording_id": rec.get("id"),
        "artist": "; ".join(arts) if arts else artist,
        "title": rec.get("title") or title,
        "release": rel or None,
    }


def cache_key(url):
    """The key resolve() files a hint under. review.py needs the same one."""
    vid = video_id(url)
    if vid:
        return vid
    query = search_query(url)
    return f"ytsearch1:{query}" if query else other_url(url)


def resolve(url, cache, limiter, session):
    """A hint is whatever was convenient to paste: a video link, a link with a
    note after it, two links, a search-results page, or a link to some other
    site entirely. All of them are answerable, so all of them are accepted."""
    vid = video_id(url)
    key, info, from_search = vid, None, False
    if vid:
        if vid in cache and not cache[vid].get("error"):
            return cache[vid]
        info = fetch_video(vid)
    else:
        query = search_query(url)
        target = f"ytsearch1:{query}" if query else other_url(url)
        if not target:
            return {"error": "no link or search in this hint"}
        from_search = bool(query)
        key = target
        if key in cache and not cache[key].get("error"):
            return cache[key]
        info = fetch_url(target)

    if info.get("error"):
        cache[key] = {"video_id": vid, "error": info["error"]}
        return cache[key]
    vid = info.get("video_id") or vid

    artist, title, how = names_from(info)
    out = {"video_id": vid, "video_title": info.get("video_title"),
           "channel": info.get("channel"), "duration": info.get("duration"),
           "artist": artist, "title": title, "derived_from": how,
           "site": info.get("extractor"), "album": info.get("meta_album"),
           # A search result is the first thing YouTube offered, not a video
           # anyone chose. review.py scores it below the auto bar for that
           # reason, so it lands in the queue instead of clearing it.
           "from_search": from_search}

    mb = enrich_mb(artist, title, limiter, session)
    if mb:
        out.update({"artist": mb["artist"], "title": mb["title"],
                    "recording_id": mb["recording_id"],
                    "release": mb["release"], "enriched": "musicbrainz"})
    else:
        out["enriched"] = None
    cache[key] = out
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true",
                    help="re-resolve URLs already in the cache")
    args = ap.parse_args()

    hints = review_mod.load_hints()
    urls = {}
    for name, hint in hints.items():
        kind, payload = review_mod.parse_hint(hint)
        if kind == "url":
            urls[name] = payload
    if not urls:
        print("  no YouTube-URL hints found in the review queues\n")
        return 0

    cache = json.load(open(OUT)) if os.path.exists(OUT) else {}
    if args.force:
        cache = {}
    todo = list(urls.items())
    if args.limit:
        todo = todo[: args.limit]
    print(f"  {len(todo)} URL hints to resolve ({len(cache)} cached)")

    limiter, session = RateLimiter(MB_RATE), requests.Session()
    ok = failed = 0
    for i, (name, url) in enumerate(todo, 1):
        res = resolve(url, cache, limiter, session)
        if res.get("error"):
            failed += 1
            print(f"    FAIL {name[:44]:44} {res['error'][:60]}")
        else:
            ok += 1
            print(f"    {res['artist'][:26]:26} | {str(res['title'])[:30]:30} "
                  f"({res['derived_from']}"
                  f"{', +mb' if res.get('enriched') else ''})  <- {name[:32]}")
        if i % 5 == 0 or i == len(todo):
            json.dump(cache, open(OUT + ".tmp", "w"), ensure_ascii=False, indent=1)
            os.replace(OUT + ".tmp", OUT)

    json.dump(cache, open(OUT + ".tmp", "w"), ensure_ascii=False, indent=1)
    os.replace(OUT + ".tmp", OUT)
    print(f"\n  resolved {ok}, failed {failed} -> {OUT}")
    print("  now re-run review.py to fold them in\n")
    return 1 if failed and not ok else 0


if __name__ == "__main__":
    sys.exit(main())
