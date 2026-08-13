#!/usr/bin/env python3
"""Where lyrics come from when LRCLIB does not have them.

LRCLIB is the first and best source: free, synced, and the only one that
publishes a duration to match against. It is also thin outside the
English-language catalogue, which is most of this library. Measured on the
tracks here that LRCLIB has nothing for, YouTube Music had timed lyrics for
all six sampled, including songs LRCLIB has never heard of.

Each source answers the same question and returns the same shape, so
lyrics_fetch can try them in order and stop at the first one that clears the
gates:

    {"synced": lrc text or None, "plain": text or None,
     "matched": "Artist - Title", "matched_duration": seconds or None,
     "source": name}

None means "this source does not have it". Nothing here decides whether an
answer is good enough: that is the caller's job, and it uses the same fit()
comparison for every source so no provider can slip a different standard in.
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from pipeline.webmatch import fit  # noqa: E402

# The same bar the LRCLIB picker uses. A source that cannot be checked as
# strictly as LRCLIB is not allowed a looser one.
MIN_FIT = 0.5
# How far a candidate's own duration may sit from the file before its
# timings are meaningless. Same 2s LRCLIB's signature match uses.
MAX_DRIFT = 2.0

_yt = None
_YT_ERROR = []


def _ytmusic():
    """One client, made on first use. Constructing it costs a request."""
    global _yt
    if _yt is None and not _YT_ERROR:
        try:
            from ytmusicapi import YTMusic
            _yt = YTMusic()
        except Exception as e:                       # pragma: no cover
            _YT_ERROR.append(str(e)[:80])
    return _yt


def _lrc(lines):
    """-> LRC text from YouTube Music's timed lines.

    start_time is milliseconds. The two-digit centisecond field is what every
    player expects, and pipeline/lrc.py reads it back the same way.
    """
    out = []
    for ln in lines:
        ms = getattr(ln, "start_time", None)
        text = (getattr(ln, "text", "") or "").strip()
        if ms is None or not text:
            continue
        m, s = divmod(int(ms) // 10, 6000)
        out.append(f"[{m:02d}:{s // 100:02d}.{s % 100:02d}]{text}")
    return "\n".join(out) or None


def from_ytmusic(artist, title, duration=None):
    """-> a candidate from YouTube Music, or None.

    Its catalogue is the reason this exists: it carries the ex-Yu releases
    LRCLIB has never been given, and it carries them timed. It publishes no
    duration for the lyrics themselves, so the *track's* duration is what the
    caller checks, which is why it is returned here.
    """
    yt = _ytmusic()
    if not (yt and artist and title):
        return None
    try:
        hits = yt.search(f"{artist} {title}", filter="songs", limit=3)
    except Exception:
        return None                       # a failure is not an absence
    for h in hits or []:
        names = ", ".join(a["name"] for a in h.get("artists") or [])
        if fit(artist, names) < MIN_FIT or fit(title, h.get("title") or "") < MIN_FIT:
            continue
        try:
            watch = yt.get_watch_playlist(h["videoId"])
            browse = watch.get("lyrics")
            if not browse:
                continue
            timed = yt.get_lyrics(browse, timestamps=True) or {}
            plain = yt.get_lyrics(browse) or {}
        except Exception:
            return None
        lines = timed.get("lyrics") if timed.get("hasTimestamps") else None
        text = plain.get("lyrics")
        synced = _lrc(lines) if isinstance(lines, list) else None
        if not (synced or text):
            continue
        return {"synced": synced,
                "plain": text if isinstance(text, str) else None,
                "matched": f"{names} - {h.get('title')}",
                "matched_duration": h.get("duration_seconds"),
                "source": "ytmusic"}
    return None


def from_genius(artist, title, token=None, session=None):
    """-> a plain-text candidate from Genius, or None.

    Plain only: Genius has no timings. Worth having anyway, because untimed
    words beat timed words belonging to a different song, and because Genius
    carries regional catalogue LRCLIB does not.

    Only the search API is used, and only to confirm that Genius holds THIS
    song. The words themselves come from the page, which is why this returns
    None when the page cannot be read: a title match is not lyrics.
    """
    import requests
    from pipeline.useragent import UA
    if not (token and artist and title):
        return None
    s = session or requests.Session()
    try:
        r = s.get("https://api.genius.com/search", params={"q": f"{artist} {title}"},
                  headers={"Authorization": f"Bearer {token}", **UA}, timeout=20)
        if r.status_code != 200:
            return None
        hits = (r.json().get("response") or {}).get("hits") or []
    except Exception:
        return None
    for h in hits:
        res = h.get("result") or {}
        ga = (res.get("primary_artist") or {}).get("name") or ""
        gt = res.get("title") or ""
        if fit(artist, ga) < MIN_FIT or fit(title, gt) < MIN_FIT:
            continue
        text = _genius_page(s, res.get("url"))
        if not text:
            continue
        return {"synced": None, "plain": text,
                "matched": f"{ga} - {gt}", "matched_duration": None,
                "source": "genius"}
    return None


def _genius_page(session, url):
    """-> the words on a Genius song page, or None.

    Genius serves the words in the page rather than the API, so this reads
    the containers their front end renders them into. A layout change breaks
    it, which is why every failure returns None and lets the next source
    answer instead of recording an absence.
    """
    if not url:
        return None
    from pipeline.useragent import UA
    try:
        from bs4 import BeautifulSoup
        r = session.get(url, headers=UA, timeout=20)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "html.parser")
        blocks = soup.select('div[data-lyrics-container="true"]')
        if not blocks:
            return None
        parts = []
        for b in blocks:
            for br in b.find_all("br"):
                br.replace_with("\n")
            parts.append(b.get_text())
        text = "\n".join(parts).strip()
        return text or None
    except Exception:
        return None
