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

None means "this source does not have it". ERROR means "this source could not
be asked", which is a different thing: an absence may be cached, a failure may
not, and the whole reason lyrics_fetch exists in its current form is that an
older version cached 132 failures as answers. Nothing here decides whether an
answer is good enough: that is the caller's job, and it uses the same fit()
comparison for every source so no provider can slip a different standard in.
"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from pipeline.webmatch import fit  # noqa: E402

# The same bar the LRCLIB picker uses. A source that cannot be checked as
# strictly as LRCLIB is not allowed a looser one.
MIN_FIT = 0.5
# How far a candidate's own duration may sit from the file before its
# timings are meaningless. Same 2s LRCLIB's signature match uses.
MAX_DRIFT = 2.0

# Returned instead of a candidate when the source could not be reached. It is
# a distinct object rather than a flag so a caller cannot mistake it for a
# real answer: compare with `is`.
ERROR = {"error": True}

_yt = None
_YT_STATE = {}


def _ytmusic():
    """One client, made on first use. Constructing it costs a request.

    Records *why* it has no client, because the two reasons need opposite
    handling: a missing package is a permanent absence and may be cached, a
    failed request is transient and may not.
    """
    global _yt
    if _yt is None and not _YT_STATE:
        try:
            from ytmusicapi import YTMusic
        except ImportError as e:                     # pragma: no cover
            _YT_STATE["missing"] = str(e)[:80]
            return None
        try:
            _yt = YTMusic()
        except Exception as e:                       # pragma: no cover
            _YT_STATE["failed"] = str(e)[:80]
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
    """-> a candidate from YouTube Music, None, or ERROR.

    Its catalogue is the reason this exists: it carries the ex-Yu releases
    LRCLIB has never been given, and it carries them timed. It publishes no
    duration for the lyrics themselves, so the *track's* duration is what the
    caller checks, which is why it is returned here.
    """
    if not (artist and title):
        return None
    # A source that is down must not be allowed to look like a source with
    # nothing. ERROR is never cached; None is cached for a month.
    from pipeline import health
    if health.blocked("ytmusic"):
        return ERROR
    yt = _ytmusic()
    if yt is None:
        return ERROR if "failed" in _YT_STATE else None
    try:
        hits = yt.search(f"{artist} {title}", filter="songs", limit=3)
    except Exception:
        return ERROR                      # a failure is not an absence
    failed = False
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
            # One candidate failing says nothing about the next, and the
            # right track is often not the first hit. Carry on, but remember
            # that this song was not fully asked.
            failed = True
            continue
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
    return ERROR if failed else None


_DZ_QUERY = """
query MuzziLyrics($id: String!) {
  track(trackId: $id) {
    id
    title
    lyrics { text synchronizedLines { lrcTimestamp line } }
  }
}"""

# The JWT the ARL buys lasts about six minutes. Refreshed on a timer well
# inside that, because a token that expires mid-sweep would turn every
# remaining track into an error, and errors are the one thing this file exists
# to keep separate from absences.
_DZ_TTL = 240
_dz = {"jwt": None, "at": 0.0}
# verify_lyrics fetches on eight threads, and this token is shared by all of
# them. Without the lock they race to refresh an expired one: harmless in
# effect, since the exchange is idempotent, but it spends eight logins on the
# credential most likely to be rate limited.
_dz_lock = threading.Lock()


def _deezer_jwt(session):
    """-> a bearer token, or None. The ARL cookie is exchanged for it."""
    with _dz_lock:
        if _dz["jwt"] and time.time() - _dz["at"] < _DZ_TTL:
            return _dz["jwt"]
        from pipeline.health import secret
        arl = secret("deezer_arl")
        if not arl:
            return None
        try:
            r = session.post("https://auth.deezer.com/login/arl",
                             params={"jo": "p", "rto": "c", "i": "c"},
                             cookies={"arl": arl}, timeout=25)
            if r.status_code != 200:
                return None
            _dz["jwt"] = (r.json() or {}).get("jwt")
        except Exception:
            return None
        _dz["at"] = time.time()
        return _dz["jwt"]


def from_deezer(artist, title, deezer_id, session=None):
    """-> a candidate from Deezer, None, or ERROR.

    Asked by the exact track id the cascade already resolved, never by search.
    That is the whole reason this source is safe to use: NetEase was measured
    answering HTTP 200 with well-formed lyrics for a completely different song,
    and the only thing standing in its way was the name comparison. Here there
    is no search step to go wrong, so a wrong answer would require Deezer to
    return the wrong lyrics for a track id it named itself.

    Deezer's own timings come as `[mm:ss.xx]` strings, which is the LRC format
    write_tags and pipeline/lrc.py already read, so no conversion is needed and
    none is invented.

    Unofficial, so it is on the same footing as every other unofficial source
    here: probed before use, and never the sole support for anything.
    """
    if not deezer_id:
        return None
    from pipeline import health
    if health.blocked("deezer_lyrics"):
        return ERROR
    import requests
    s = session or requests.Session()
    token = _deezer_jwt(s)
    if not token:
        # No ARL configured is an absence of a capability, not of lyrics, and
        # the caller must not cache it as either.
        return ERROR
    try:
        r = s.post("https://pipe.deezer.com/api",
                   headers={"Authorization": f"Bearer {token}"},
                   json={"operationName": "MuzziLyrics",
                         "variables": {"id": str(deezer_id)},
                         "query": _DZ_QUERY}, timeout=25)
        if r.status_code != 200:
            return ERROR
        d = r.json()
    except Exception:
        return ERROR
    if d.get("errors"):
        return ERROR
    track = ((d.get("data") or {}).get("track") or {})
    ly = track.get("lyrics") or {}
    lines = [x for x in (ly.get("synchronizedLines") or [])
             if x.get("lrcTimestamp") and x.get("line")]
    synced = "\n".join(f"{x['lrcTimestamp']}{x['line']}" for x in lines) or None
    plain = (ly.get("text") or "").strip() or None
    if not (synced or plain):
        return None
    return {"synced": synced, "plain": plain,
            # Deezer's own title, so the caller's title comparison is a real
            # check: it catches this endpoint answering about a different
            # track than the id names.
            #
            # The artist is ours, not Deezer's, and it is worth being explicit
            # that this makes the caller's artist_fit a tautology for this
            # source. The artist was already compared where the id came from:
            # cascade's deezer-search requires fit >= 0.6 on artist AND title
            # before it records a deezer_id at all. Asking for it again here
            # would cost a second field in the query to re-check something
            # this track's id could not have without.
            "matched": f"{artist} - {track.get('title') or title}",
            "matched_duration": None,
            "source": "deezer"}


def from_genius(artist, title, token=None, session=None):
    """-> a plain-text candidate from Genius, None, or ERROR.

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
    from pipeline import health
    if health.blocked("genius"):
        return ERROR
    s = session or requests.Session()
    try:
        r = s.get("https://api.genius.com/search", params={"q": f"{artist} {title}"},
                  headers={"Authorization": f"Bearer {token}", **UA}, timeout=20)
        if r.status_code != 200:
            return ERROR                  # 429 and 5xx are not "no lyrics"
        hits = (r.json().get("response") or {}).get("hits") or []
    except Exception:
        return ERROR
    failed = False
    for h in hits:
        res = h.get("result") or {}
        ga = (res.get("primary_artist") or {}).get("name") or ""
        gt = res.get("title") or ""
        if fit(artist, ga) < MIN_FIT or fit(title, gt) < MIN_FIT:
            continue
        text = _genius_page(s, res.get("url"))
        if text is ERROR:
            failed = True
            continue
        if not text:
            continue
        return {"synced": None, "plain": text,
                "matched": f"{ga} - {gt}", "matched_duration": None,
                "source": "genius"}
    return ERROR if failed else None


def _genius_page(session, url):
    """-> the words on a Genius song page, None, or ERROR.

    Genius serves the words in the page rather than the API, so this reads
    the containers their front end renders them into. A page that cannot be
    fetched is ERROR; a page that fetches and holds no lyric container is a
    real absence, and the difference decides whether the miss may be cached.
    """
    if not url:
        return None
    from pipeline.useragent import UA
    try:
        from bs4 import BeautifulSoup
        r = session.get(url, headers=UA, timeout=20)
        if r.status_code != 200:
            return ERROR
    except Exception:
        return ERROR
    try:
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
        return None                       # the page loaded, we cannot read it
