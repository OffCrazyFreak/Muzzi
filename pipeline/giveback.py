#!/usr/bin/env python3
"""Send back what this library learned, to the two places that can take it.

Everything here reads from free databases. AcoustID resolved 1302 of these
tracks and LRCLIB supplied most of the timed lyrics, and neither gets anything
back unless something sends it. So: fingerprints to AcoustID, sheets to
LRCLIB.

Nothing is submitted without `--submit`. The default prints what would go and
sends nothing, because a bad submission to a shared database is not a bad
run, it is somebody else's bad data.

What is eligible, and why it is narrow
--------------------------------------

Auto-accepted rows only. A row in review is a row this pipeline could not
settle, and publishing a guess is worse than publishing nothing: a wrong
fingerprint-to-recording link is repeated back to everyone who fingerprints
that song afterwards. `A blank field beats a wrong one` applies hardest when
the field is someone else's.

MusicBrainz is not here, and that is measured rather than skipped
----------------------------------------------------------------

The plan called for MusicBrainz edits. The API cannot take them: it accepts
tags, ratings, barcodes, ISRCs and collections, and the documentation says in
as many words that "for most data additions you should use the website
instead". Correcting an artist name, which is the thing this library actually
learned, is not submittable at any endpoint.

Of what it can take, this library has nothing worth giving. Measured across
all 1723 files: 2 ISRCs, 0 barcodes, 0 ratings. The genres are Last.fm
folksonomy that `genres.py` exists to clean up, and pushing that back into a
catalogue is the wrong direction entirely.

Usage:
  giveback.py                     # what would be sent, and to where
  giveback.py --acoustid --submit
  giveback.py --lrclib --submit --limit 20
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.parse

import requests

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from pipeline.useragent import UA  # noqa: E402

REVIEW = os.path.join(HERE, "cache", "review.json")
ANALYSIS = os.path.join(HERE, "cache", "analysis.json")
FINGERPRINTS = os.path.join(HERE, "cache", "fingerprints.json")
LYRICS = os.path.join(HERE, "cache", "lyrics.json")
SENT = os.path.join(HERE, "cache", "giveback.json")

ACOUSTID_SUBMIT = "https://api.acoustid.org/v2/submit"
# The application key, which identifies Muzzi rather than you. The `user` key
# is yours and comes out of secrets.json.
ACOUSTID_CLIENT = "acoustid_key"
LRCLIB_GET = "https://lrclib.net/api/get"
LRCLIB_CHALLENGE = "https://lrclib.net/api/request-challenge"
LRCLIB_PUBLISH = "https://lrclib.net/api/publish"

# AcoustID takes many fingerprints per request, suffixed .0 .1 .2. Kept
# well under any documented ceiling: a rejected batch of 500 is 500 tracks to
# work out the truth about, and a rejected batch of 50 is a retry.
BATCH = 50


def _load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def secret(name):
    """-> one value out of config/secrets.json, or None."""
    try:
        with open(os.path.join(HERE, "config", "secrets.json"),
                  encoding="utf-8") as fh:
            v = (json.load(fh).get(name) or "").strip()
        return v or None
    except (OSError, ValueError, AttributeError):
        return None


def sent(path=None):
    """-> what has already been given, so nothing is offered twice.

    Keyed by what was sent rather than by track, because the two givers send
    different things about the same file and a single 'done' flag would let
    one of them mark the other's work finished.
    """
    return _load(path or SENT)


def _save(state):
    os.makedirs(os.path.dirname(SENT), exist_ok=True)
    with open(SENT + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    os.replace(SENT + ".tmp", SENT)


# ------------------------------------------------------------------ AcoustID

def acoustid_candidates(rows=None, done=None):
    """-> [{path, fingerprint, duration, mbid, ...}] worth submitting.

    A fingerprint and the recording it belongs to, which is the whole of what
    AcoustID stores. The metadata goes with it because the documentation asks
    for it, and because a submission carrying only a fingerprint and an MBID
    is harder for a human to check later if it turns out to be wrong.
    """
    rows = rows if rows is not None else _load(REVIEW)
    if isinstance(rows, dict):
        rows = list(rows.values())
    done = done if done is not None else sent()
    fps = {v["path"]: v for v in _load(FINGERPRINTS).values()
           if isinstance(v, dict) and v.get("path")}
    an = {v["path"]: v for v in _load(ANALYSIS).values()
          if isinstance(v, dict) and v.get("path")}
    out = []
    for r in rows:
        p = r.get("path")
        if not p or r.get("tier") != "auto" or not r.get("recording_id"):
            continue
        if done.get(f"acoustid:{p}"):
            continue
        fp = (fps.get(p) or {}).get("fingerprint")
        dur = (fps.get(p) or {}).get("duration") or \
            (an.get(p) or {}).get("decoded_secs")
        if not fp or not dur:
            continue
        out.append({"path": p, "fingerprint": fp, "duration": int(round(dur)),
                    "mbid": r["recording_id"],
                    "track": r.get("proposed_title"),
                    "artist": r.get("proposed_artist"),
                    "album": r.get("proposed_album"),
                    "year": r.get("proposed_year")})
    return out


def acoustid_batch(items, user_key, client_key, session=None, submit=False):
    """-> (ok, detail). One request carrying up to BATCH fingerprints."""
    data = {"client": client_key, "user": user_key, "format": "json"}
    for i, it in enumerate(items):
        data[f"duration.{i}"] = it["duration"]
        data[f"fingerprint.{i}"] = it["fingerprint"]
        data[f"mbid.{i}"] = it["mbid"]
        for key, field in (("track", "track"), ("artist", "artist"),
                           ("album", "album"), ("year", "year")):
            if it.get(field):
                data[f"{key}.{i}"] = it[field]
    if not submit:
        return True, f"would send {len(items)} fingerprints"
    s = session or requests.Session()
    # Never raises. A submission run saves as it goes, so an exception here
    # would abandon a half-recorded run and lose the record of what did go.
    try:
        r = s.post(ACOUSTID_SUBMIT, data=data, headers=UA, timeout=60)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}: {r.text[:120]}"
        got = r.json()
    except requests.RequestException as e:
        return False, f"{type(e).__name__}: {str(e)[:80]}"
    except ValueError:
        return False, "answered with something that is not JSON"
    if got.get("status") != "ok":
        return False, f"status {got.get('status')}: {str(got)[:120]}"
    return True, f"accepted {len(got.get('submissions') or [])}"


# -------------------------------------------------------------------- LRCLIB

def lrclib_has(artist, title, album, duration, session=None):
    """-> True when LRCLIB already holds timed lyrics for this exact track.

    Asked before every publish, because most of these sheets came FROM
    LRCLIB. Sending them back would be a no-op at best, and at ~7 seconds of
    proof-of-work each it is a no-op that costs a fortnight of CPU across a
    library this size. Measured on a sample of 40: 29 were already there and
    4 were genuinely missing.
    """
    s = session or requests.Session()
    q = {"artist_name": artist or "", "track_name": title or "",
         "duration": int(round(duration or 0))}
    if album:
        q["album_name"] = album
    try:
        r = s.get(f"{LRCLIB_GET}?{urllib.parse.urlencode(q)}", headers=UA,
                  timeout=20)
    except Exception:
        return None                      # unknown, which is not "missing"
    if r.status_code == 404:
        return False
    if r.status_code != 200:
        return None
    try:
        return bool((r.json() or {}).get("syncedLyrics"))
    except ValueError:
        return None


def solve(prefix, target, cap=50_000_000):
    """-> the nonce whose SHA256 clears the target, or None within `cap`.

    LRCLIB gates publishing on proof of work, which is how it keeps a public
    write endpoint open without an account. The target seen in practice is
    `000000FF...`, about 12 million hashes and some 7 seconds of one core.
    The cap is there so a harder target than expected fails as a refusal
    rather than as a process that never comes back.
    """
    tb = bytes.fromhex(target)
    for n in range(cap):
        if hashlib.sha256(f"{prefix}{n}".encode()).digest() < tb:
            return n
    return None


def publish_token(session=None):
    """-> a solved `prefix:nonce` token, or None."""
    s = session or requests.Session()
    try:
        r = s.post(LRCLIB_CHALLENGE, headers=UA, timeout=20)
        if r.status_code != 200:
            return None
        got = r.json()
        prefix, target = got["prefix"], got["target"]
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None
    n = solve(prefix, target)
    return None if n is None else f"{prefix}:{n}"


def lrclib_candidates(rows=None, done=None):
    """-> [{path, artist, title, album, duration, synced, plain}] to offer.

    Eligibility is the same auto-accepted bar, plus a sheet this pipeline
    itself trusted enough to write: if `write_tags` would have dropped the
    timings, they are not fit to publish either.
    """
    rows = rows if rows is not None else _load(REVIEW)
    if isinstance(rows, dict):
        rows = list(rows.values())
    done = done if done is not None else sent()
    lyr = _load(LYRICS)
    an = {v["path"]: v for v in _load(ANALYSIS).values()
          if isinstance(v, dict) and v.get("path")}
    out = []
    for r in rows:
        p, a, t = r.get("path"), r.get("proposed_artist"), r.get("proposed_title")
        if not p or not a or not t or r.get("tier") != "auto":
            continue
        if done.get(f"lrclib:{p}"):
            continue
        e = lyr.get(f"{a}|{t}".lower())
        if not isinstance(e, dict) or not e.get("synced"):
            continue
        dur = (an.get(p) or {}).get("decoded_secs")
        if not dur:
            continue
        out.append({"path": p, "artist": a, "title": t,
                    "album": r.get("proposed_album") or "",
                    "duration": int(round(dur)),
                    "synced": e["synced"], "plain": e.get("plain") or ""})
    return out


def lrclib_publish(item, token, session=None, submit=False):
    """-> (ok, detail)."""
    if not submit:
        return True, "would publish"
    s = session or requests.Session()
    body = {"trackName": item["title"], "artistName": item["artist"],
            "albumName": item["album"], "duration": item["duration"],
            "plainLyrics": item["plain"], "syncedLyrics": item["synced"]}
    try:
        r = s.post(LRCLIB_PUBLISH, json=body, timeout=30,
                   headers={**UA, "X-Publish-Token": token})
    except requests.RequestException as e:
        return False, f"{type(e).__name__}: {str(e)[:80]}"
    if r.status_code in (200, 201):
        return True, "published"
    return False, f"HTTP {r.status_code}: {r.text[:120]}"


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--acoustid", action="store_true")
    ap.add_argument("--lrclib", action="store_true")
    ap.add_argument("--submit", action="store_true",
                    help="actually send. Without it nothing leaves this "
                         "machine, which is the default on purpose")
    ap.add_argument("--limit", type=int, help="stop after this many")
    args = ap.parse_args()
    both = not (args.acoustid or args.lrclib)
    # Reporting on everything is a good default; submitting to everything is
    # not. `--submit` on its own would send 1276 fingerprints and every
    # eligible sheet to two different databases from one keystroke, so it has
    # to say which.
    if args.submit and both:
        ap.error("--submit needs --acoustid or --lrclib: say where it goes")
    # `--limit 0` sends nothing and reads as a no-op, and `--limit -1` slices
    # off the last item and sends the other 1275, which is the opposite of
    # what anyone types it for.
    if args.limit is not None and args.limit < 1:
        ap.error("--limit must be 1 or more")

    done = sent()
    session = requests.Session()

    if args.acoustid or both:
        items = acoustid_candidates(done=done)
        if args.limit:
            items = items[:args.limit]
        print(f"\n  AcoustID: {len(items)} fingerprints with a confirmed "
              f"recording id")
        user = secret("acoustid_user_key")
        client = secret(ACOUSTID_CLIENT)
        if not items:
            print("    nothing to send")
        elif not args.submit:
            for it in items[:5]:
                print(f"    would send {os.path.basename(it['path'])[:52]:54} "
                      f"{it['mbid']}")
            if len(items) > 5:
                print(f"    ... and {len(items) - 5} more")
        elif not user or not client:
            print("    refusing: needs acoustid_key and acoustid_user_key in "
                  "config/secrets.json")
        else:
            for i in range(0, len(items), BATCH):
                chunk = items[i:i + BATCH]
                ok, detail = acoustid_batch(chunk, user, client, session, True)
                print(f"    batch {i // BATCH + 1}: {detail}")
                if not ok:
                    break
                for it in chunk:
                    done[f"acoustid:{it['path']}"] = {"mbid": it["mbid"]}
                _save(done)
                time.sleep(1)

    if args.lrclib or both:
        items = lrclib_candidates(done=done)
        if args.limit:
            items = items[:args.limit]
        print(f"\n  LRCLIB: {len(items)} timed sheets on auto-accepted rows")
        if not args.submit:
            print("    each one is checked against LRCLIB before it is sent, "
                  "because most came from there")
            for it in items[:5]:
                print(f"    would offer {it['artist'][:24]:26} "
                      f"{it['title'][:34]}")
            if len(items) > 5:
                print(f"    ... and {len(items) - 5} more to check")
        else:
            gave = skipped = unknown = 0
            for it in items:
                has = lrclib_has(it["artist"], it["title"], it["album"],
                                 it["duration"], session)
                if has is None:
                    # The check itself failed, which is not "LRCLIB has it".
                    # Marking it done here would retire the track on a network
                    # blip and never offer it again, which is caching a failure
                    # as an answer in the one place the answer is permanent.
                    unknown += 1
                    continue
                if has:
                    skipped += 1
                    done[f"lrclib:{it['path']}"] = {"skipped": "already there"}
                    # Written here and not only at the foot of the loop. The
                    # `continue` skips that save, so a run where everything is
                    # already on LRCLIB, which is the common one, recorded
                    # nothing and asked the same 1300 questions again next
                    # time.
                    _save(done)
                    continue
                token = publish_token(session)
                if not token:
                    print("    could not get a publish token, stopping")
                    break
                ok, detail = lrclib_publish(it, token, session, True)
                print(f"    {detail:12} {it['artist'][:22]:24} "
                      f"{it['title'][:30]}")
                if ok:
                    gave += 1
                    done[f"lrclib:{it['path']}"] = {"published": True}
                _save(done)
                time.sleep(1)
            print(f"    published {gave}, already there {skipped}, "
                  f"could not check {unknown} (offered again next run)")

    print("\n  MusicBrainz: nothing submittable. Its API takes tags, ratings, "
          "barcodes and ISRCs only,")
    print("  and this library has 2 ISRCs and no barcodes. Name corrections "
          "are website-only.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
