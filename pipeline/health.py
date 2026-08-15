#!/usr/bin/env python3
"""Is each source answering, and is the answer worth anything?

A source that is down does not say "I am down". It returns nothing, and
nothing is exactly what a source with no entry for your song returns. Those
two are opposite facts wearing the same clothes: one may be written down and
believed for a month, the other must be retried in an hour, and a pipeline
that cannot tell them apart will eventually record an outage as a permanent
absence for every track it touched during it.

So each source is asked one question whose answer is already known, once per
run, before any real work. A source that cannot answer that is not asked about
your music, and nothing it failed to say is written down as an absence.

The canary checks the ANSWER, not the status code. Deezer returns HTTP 200
with an error object in the body; iTunes returns 200 with zero results when it
is throttling; a captive portal returns 200 with a login page. A probe that
only checks `r.status_code == 200` reports a healthy network and proves
nothing about the service.

Three rules the callers depend on:

  A dead source shrinks the denominator, it does not abstain into agreement.
  Two sources agreeing out of two that were reachable is not the same evidence
  as two agreeing out of five, and if the three that were down are simply
  missing from the count, it looks like the stronger case.

  Nothing a dead source failed to say may be cached as an answer.

  A conclusion reached while a source was down is provisional. It has to be
  reachable again before that conclusion is treated as settled, or one bad
  half hour is permanent.

Usage:
  health.py                     # probe everything and print the table
  health.py --source lrclib
  health.py --json
"""
import argparse
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from pipeline import evidence  # noqa: E402
from pipeline.useragent import UA  # noqa: E402

CACHE = os.path.join(HERE, "cache")
OUT = os.path.join(CACHE, "health.json")

# How long a verdict stands before the source is probed again. Short enough
# that a source coming back is noticed within one stage, long enough that the
# eight stages of one pipeline run share a single probe each.
TTL = 600

# What a probe can conclude, in the store's own vocabulary so a caller can
# write the verdict straight into an observation.
OK = evidence.FOUND
DOWN = evidence.TEMPORARY_FAILURE
NO_KEY = evidence.NOT_APPLICABLE
BAD_KEY = evidence.AUTH_FAILURE
CHANGED = evidence.SOURCE_CHANGED

# A source is usable when it answered. NOT_APPLICABLE is not a fault: it means
# this installation was never given a key, which is a permanent and known
# absence rather than something to retry or to wait for.
USABLE = {OK}

_lock = threading.Lock()
_state = {}


# --------------------------------------------------------------- the probes
#
# One question per source whose answer is not in doubt. Deliberately a famous,
# long-catalogued track: the probe has to fail when the SERVICE is broken and
# not when a particular record is missing, so the query must be one that any
# working copy of that catalogue answers.

PROBE_ARTIST, PROBE_TITLE = "Rick Astley", "Never Gonna Give You Up"

# Deezer's lyrics are asked by id rather than by name, so its probe cannot use
# the query above and needs actual rows. More than one, from different labels
# and decades, so the probe is not a bet on a single catalogue entry surviving.
#   3135556     Daft Punk, Harder Better Faster Stronger
#   916424      Gotye, Somebody That I Used To Know
#   1109731     Adele, Rolling in the Deep
DEEZER_PROBE_TRACKS = ("3135556", "916424", "1109731")


def _session():
    import requests
    return requests.Session()


def as_object(response):
    """-> the response's JSON when it is an object, else None.

    Every probe below reads named fields out of the answer, and `.get` on a
    bare list or a string raises rather than returning nothing. A service that
    answers 200 with JSON of the wrong shape has changed under us, which is a
    verdict this module already has a word for, so it is worth saying that
    rather than letting an AttributeError decide what happens.
    """
    try:
        got = response.json()
    except Exception:
        return None
    return got if isinstance(got, dict) else None


def _lrclib(s):
    r = s.get("https://lrclib.net/api/search",
              params={"artist_name": PROBE_ARTIST, "track_name": PROBE_TITLE},
              headers=UA, timeout=15)
    if r.status_code != 200:
        return DOWN, f"HTTP {r.status_code}"
    got = r.json()
    if not isinstance(got, list):
        return CHANGED, f"expected a list, got {type(got).__name__}"
    if not got:
        return CHANGED, "no hit for a track it certainly has"
    return OK, f"{len(got)} hits"


def _deezer(s):
    r = s.get("https://api.deezer.com/search",
              params={"q": f"{PROBE_ARTIST} {PROBE_TITLE}", "limit": 1},
              timeout=15)
    if r.status_code != 200:
        return DOWN, f"HTTP {r.status_code}"
    d = r.json()
    # 200 with an error object in the body is Deezer's way of saying it is
    # rate limiting, which is not an absence and not a failure of the network.
    if isinstance(d, dict) and d.get("error"):
        err = d["error"]
        code = err.get("code") if isinstance(err, dict) else None
        return (evidence.RATE_LIMITED if code == 4 else DOWN), str(err)[:60]
    if not (d.get("data") if isinstance(d, dict) else None):
        return CHANGED, "no hit for a track it certainly has"
    return OK, "answered"


def _itunes(s):
    r = s.get("https://itunes.apple.com/search",
              params={"term": f"{PROBE_ARTIST} {PROBE_TITLE}",
                      "entity": "song", "limit": 1}, timeout=15)
    if r.status_code == 403:
        return evidence.RATE_LIMITED, "403, its usual throttle response"
    if r.status_code != 200:
        return DOWN, f"HTTP {r.status_code}"
    d = as_object(r)
    if d is None:
        return CHANGED, "answered with something that is not an object"
    if not (d.get("results") or []):
        # iTunes answers a throttled request with 200 and zero results, which
        # is indistinguishable from a real miss on any one query. On this
        # query it is not: the track is certainly in the catalogue.
        return evidence.RATE_LIMITED, "200 with no results, so it is throttling"
    return OK, "answered"


def _musicbrainz(s):
    r = s.get("https://musicbrainz.org/ws/2/recording",
              params={"query": f'artist:"{PROBE_ARTIST}" AND '
                               f'recording:"{PROBE_TITLE}"', "fmt": "json",
                      "limit": 1},
              headers=UA, timeout=20)
    if r.status_code == 503:
        return DOWN, "503, its 'currently busy' response"
    if r.status_code != 200:
        return DOWN, f"HTTP {r.status_code}"
    d = as_object(r)
    if d is None:
        return CHANGED, "answered with something that is not an object"
    if not (d.get("recordings") or []):
        return CHANGED, "no hit for a recording it certainly has"
    return OK, "answered"


def _coverartarchive(s):
    # Nevermind's release group. Chosen because it will not be deleted.
    r = s.get("https://coverartarchive.org/release-group/"
              "1b022e01-4da6-387b-8658-8678046e4cef",
              headers=UA, timeout=20, allow_redirects=True)
    if r.status_code != 200:
        return DOWN, f"HTTP {r.status_code}"
    return OK, "answered"


def _ytmusic(_s):
    try:
        from ytmusicapi import YTMusic
    except ImportError as e:
        return NO_KEY, f"ytmusicapi is not installed: {str(e)[:40]}"
    try:
        hits = YTMusic().search(f"{PROBE_ARTIST} {PROBE_TITLE}",
                                filter="songs", limit=1)
    except Exception as e:
        return DOWN, f"{type(e).__name__}: {str(e)[:50]}"
    if not hits:
        return CHANGED, "no hit for a track it certainly has"
    return OK, "answered"


def _deezer_lyrics(s):
    """Deezer's lyrics, which are a different question from Deezer's search.

    Probed separately because it fails separately: the public search API needs
    no credentials, while lyrics need an ARL cookie exchanged for a
    short-lived token. An expired ARL leaves search working perfectly and
    every lyric lookup returning nothing, which without this reads as "Deezer
    has no lyrics for your music".

    Asked by exact track id, so this checks the two things that can break: the
    token exchange, and whether an id we name comes back with its own lyrics.
    """
    arl = secret("deezer_arl")
    if not arl:
        return NO_KEY, "no deezer_arl in config/secrets.json"
    try:
        r = s.post("https://auth.deezer.com/login/arl",
                   params={"jo": "p", "rto": "c", "i": "c"},
                   cookies={"arl": arl}, timeout=20)
        if r.status_code != 200:
            return DOWN, f"auth returned HTTP {r.status_code}"
        jwt = (as_object(r) or {}).get("jwt")
    except Exception as e:
        return DOWN, f"auth {type(e).__name__}: {str(e)[:40]}"
    if not jwt:
        # The ARL is the only credential, so no token means it is no longer
        # valid. That is a key problem, not an outage, and it needs a person.
        return BAD_KEY, "the ARL was not accepted; log in again and replace it"

    # Asked by id, so a search cannot be what fails. More than one id, because
    # a single one makes this probe a bet on one catalogue row: if that track
    # were ever withdrawn or lost its lyrics, the probe would report the whole
    # endpoint broken and the source would be disabled for good. The second is
    # only asked when the first disappoints, so the usual cost is one request.
    query = ("query P($id: String!) { track(trackId: $id) "
             "{ title lyrics { text } } }")
    last = "no track answered"
    for tid in DEEZER_PROBE_TRACKS:
        try:
            r = s.post("https://pipe.deezer.com/api",
                       headers={"Authorization": f"Bearer {jwt}"},
                       json={"operationName": "P", "variables": {"id": tid},
                             "query": query}, timeout=20)
            if r.status_code != 200:
                return DOWN, f"pipe returned HTTP {r.status_code}"
            d = as_object(r)
        except Exception as e:
            return DOWN, f"pipe {type(e).__name__}: {str(e)[:40]}"
        if d is None:
            return CHANGED, "the pipe answered with something that is not an "\
                            "object"
        if d.get("errors"):
            # A GraphQL error on a fixed query means the schema moved under
            # us, which is the standing hazard of an unofficial endpoint. It
            # is about the query, not the track, so there is no point trying
            # another one.
            return CHANGED, str(d["errors"])[:70]
        track = ((d.get("data") or {}).get("track") or {})
        if ((track.get("lyrics") or {}).get("text") or "").strip():
            return OK, f"answered for {track.get('title')!r}"
        last = f"no lyrics for track {tid}"
    return CHANGED, (f"{last}, and none of {len(DEEZER_PROBE_TRACKS)} "
                     f"probe tracks had any")


def _genius(s):
    # The same key name lyrics_fetch._genius_token reads. Spelled once, here,
    # because two spellings of one secret is a source that silently stops
    # being asked.
    token = secret("genius_access_token")
    if not token:
        return NO_KEY, "no genius_access_token in config/secrets.json"
    r = s.get("https://api.genius.com/search",
              params={"q": f"{PROBE_ARTIST} {PROBE_TITLE}"},
              headers={"Authorization": f"Bearer {token}", **UA}, timeout=20)
    if r.status_code in (401, 403):
        return BAD_KEY, f"HTTP {r.status_code}, the token was rejected"
    if r.status_code != 200:
        return DOWN, f"HTTP {r.status_code}"
    d = as_object(r)
    if d is None:
        return CHANGED, "answered with something that is not an object"
    if not ((d.get("response") or {}).get("hits") or []):
        return CHANGED, "no hit for a song it certainly has"
    return OK, "answered"


def secret(name):
    """-> one value out of config/secrets.json, or None.

    Read through the same key names the pipeline itself uses. A probe that
    looks up a different spelling reports "not configured" for a source that is
    configured, which is a lie in the one direction this module exists to
    prevent: the source is then never asked, and its silence looks like an
    absence rather than a misreading of a file.
    """
    p = os.path.join(HERE, "config", "secrets.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            values = json.load(fh)
    except (OSError, ValueError):
        return None
    # A secrets file holding valid JSON that is not an object parses fine and
    # then raises on .get, which would escape this module: _deezer_jwt calls
    # this outside a try, so a stray `[]` in the file would crash a lyric
    # sweep rather than reading as "no key".
    return values.get(name) or None if isinstance(values, dict) else None


def _ncs(s):
    """NCS publishes no API, so this asks whether its markup still parses.

    A status code is not the question for a scraped source: a redesigned page
    returns a cheerful 200 of HTML this cannot read, and that is the failure
    mode it is most likely to have.
    """
    from pipeline import ncs
    ok, detail = ncs.probe(s)
    return (OK if ok else CHANGED), detail


PROBES = {
    "lrclib": _lrclib,
    "deezer": _deezer,
    "deezer_lyrics": _deezer_lyrics,
    "itunes": _itunes,
    "musicbrainz": _musicbrainz,
    "coverartarchive": _coverartarchive,
    "ytmusic": _ytmusic,
    "genius": _genius,
    "ncs": _ncs,
}


# ---------------------------------------------------------------- the state

def _load():
    if not os.path.exists(OUT):
        return {}
    try:
        with open(OUT, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save(state):
    os.makedirs(CACHE, exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, OUT)


def check(source, force=False, session=None):
    """-> (state, detail). Probes at most once per TTL, per process.

    Shared across stages through cache/health.json, because one pipeline run
    invokes eight of them and probing each source eight times to learn the
    same thing is eight wasted requests against the limits this exists to
    respect.
    """
    if source not in PROBES:
        # Not knowing how to probe something is not the same as it being
        # broken. Reported as usable so that a source nobody wrote a probe
        # for keeps being asked: the alternative silently disables it, and it
        # would be disabled by an omission rather than by a decision.
        #
        # This is the opposite of a source that HAS a probe and has no key.
        # There the answer is known: it cannot work, so do not ask.
        return OK, "no probe defined, so nothing says it is broken"
    now = time.time()
    with _lock:
        if not _state:
            _state.update(_load())
        got = _state.get(source)
        if got and not force and now - got.get("at", 0) < TTL:
            return got["state"], got["detail"]

    s = session or _session()
    try:
        state, detail = PROBES[source](s)
    except Exception as e:                                # pragma: no cover
        state, detail = DOWN, f"{type(e).__name__}: {str(e)[:50]}"

    with _lock:
        _state[source] = {"state": state, "detail": detail, "at": now}
        _save(dict(_state))
    return state, detail


def alive(source, **kw):
    """-> True when this source is worth asking right now."""
    return check(source, **kw)[0] in USABLE


def available(sources, **kw):
    """-> the subset that answered, for use as a quorum denominator.

    The whole point of a denominator: two sources agreeing out of two that
    were reachable is weaker evidence than two out of five, and if the three
    that were down are simply missing from the list, it reads as the stronger
    case.
    """
    return [s for s in sources if alive(s, **kw)]


def blocked(source, **kw):
    """-> a reason string when this source must not be asked, else None.

    Written to be used as `if health.blocked(name): record it and move on`, so
    a caller cannot accidentally treat "we never asked" as "it had nothing".
    """
    state, detail = check(source, **kw)
    return None if state in USABLE else f"{state}: {detail}"


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", action="append",
                    help="probe only these (repeatable)")
    ap.add_argument("--force", action="store_true",
                    help="probe again even if a recent verdict is cached")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    names = args.source or sorted(PROBES)
    unknown = [n for n in names if n not in PROBES]
    if unknown:
        sys.exit(f"no probe for: {', '.join(unknown)}\n"
                 f"  known: {', '.join(sorted(PROBES))}")

    session = _session()
    out = {}
    for n in names:
        t0 = time.time()
        state, detail = check(n, force=args.force, session=session)
        out[n] = {"state": state, "detail": detail,
                  "secs": round(time.time() - t0, 2)}

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0

    print()
    for n in names:
        r = out[n]
        # Three marks, not two. A source nobody gave a key to is not an
        # outage: nothing is wrong, nothing will come back, and printing it as
        # DOWN sends you looking for a fault that does not exist.
        mark = ("ok  " if r["state"] in USABLE
                else "--  " if r["state"] == NO_KEY else "DOWN")
        print(f"  {mark} {n:16s} {r['state']:18s} {r['secs']:5.2f}s  "
              f"{r['detail']}")
    down = [n for n in names if out[n]["state"] not in USABLE
            and out[n]["state"] != NO_KEY]
    unset = [n for n in names if out[n]["state"] == NO_KEY]
    print(f"\n  {len(names) - len(down) - len(unset)} of {len(names)} "
          f"answering")
    if unset:
        print(f"  not configured: {', '.join(unset)}")
    if down:
        print(f"  DOWN: {', '.join(down)}. Anything they were not asked is "
              f"unknown, not absent.")
    print()
    # Zero either way: a source being down is a fact to act on, not a failure
    # of this command. Exiting non-zero would make `health.py` unusable as the
    # thing a stage calls before deciding what to ask.
    return 0


if __name__ == "__main__":
    sys.exit(main())
