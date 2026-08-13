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


def _session():
    import requests
    return requests.Session()


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
    if not (r.json().get("results") or []):
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
    if not (r.json().get("recordings") or []):
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


def _genius(s):
    token = _genius_token()
    if not token:
        return NO_KEY, "no token in config/secrets.json"
    r = s.get("https://api.genius.com/search",
              params={"q": f"{PROBE_ARTIST} {PROBE_TITLE}"},
              headers={"Authorization": f"Bearer {token}", **UA}, timeout=20)
    if r.status_code in (401, 403):
        return BAD_KEY, f"HTTP {r.status_code}, the token was rejected"
    if r.status_code != 200:
        return DOWN, f"HTTP {r.status_code}"
    if not ((r.json().get("response") or {}).get("hits") or []):
        return CHANGED, "no hit for a song it certainly has"
    return OK, "answered"


def _genius_token():
    p = os.path.join(HERE, "config", "secrets.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    for k in ("genius", "genius_token", "GENIUS_TOKEN"):
        if d.get(k):
            return d[k]
    return None


PROBES = {
    "lrclib": _lrclib,
    "deezer": _deezer,
    "itunes": _itunes,
    "musicbrainz": _musicbrainz,
    "coverartarchive": _coverartarchive,
    "ytmusic": _ytmusic,
    "genius": _genius,
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
