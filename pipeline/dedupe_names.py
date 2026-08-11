#!/usr/bin/env python3
"""Stage 3b: the duplicates that fingerprinting cannot see.

dedupe.py compares audio, which catches the same file twice. It does not catch
the same SONG twice, because two YouTube uploads of one track -- a lyric video
and an official audio, an .mp3 grabbed years ago and an .m4a pulled last week
-- are different recordings of the same master with different intros, loudness
and encoders. Their fingerprints legitimately disagree.

Once identification has run, the name is the better key: two files credited to
the same artist and title are the same song. This pass groups on that, then
keeps exactly one file per group.

What stops it merging things it should not:

  version markers must agree -- "Animals" and "Animals (Balkanik Remix)" are
    two songs and are never grouped, using the same check webmatch.py applies
    to catalogue results
  tempo must agree, allowing an octave -- The Kid LAROI's "WITHOUT YOU" and
    Avicii's are both ~182s and share a title, and only their 93 against 134
    BPM separates them
  the fingerprints must not be unrelated -- compared over a wide offset, since
    a different intro length moves two uploads far apart
  only identified tracks take part -- an unidentified file has no trustworthy
    name to group on, so it is left alone

The winner is the better file by measured spectral cutoff, then bitrate, then
size -- except where the copies differ in length by more than EDIT_GAP, when
the SHORTER one wins: past that point the extra is an intro, an outro or a
spoken channel tag rather than more song. Losers are recorded, not deleted;
write_tags.py skips them, so the output folder gets one copy and the originals
stay untouched until you say otherwise.

Usage:
  dedupe_names.py                    # report only
  dedupe_names.py --apply            # write cache/name_duplicates.json
  dedupe_names.py --max-gap 20       # seconds of duration slack (default 12)
"""
import argparse
import json
import os
import re
import sys
import unicodedata
from collections import defaultdict

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from pipeline.dedupe import popcount, quality_rank  # noqa: E402
from pipeline.webmatch import version_mismatch  # noqa: E402
from pipeline import artist_names  # noqa: E402

REVIEW = os.path.join(HERE, "cache", "review.json")
ANALYSIS = os.path.join(HERE, "cache", "analysis.json")
OUT = os.path.join(HERE, "cache", "name_duplicates.json")

# Cruft that a YouTube title carries and a song title does not.
_CRUFT = re.compile(
    r"\b(official|video|audio|lyrics?|tekst|prevod|spot|hd|hq|4k|1080p|720p|"
    r"visualizer|mv|full)\b", re.I)


def key_of(s):
    s = _CRUFT.sub(" ", s or "")
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _dur(row, analysis):
    return analysis.get(row["path"], {}).get("decoded_secs") or 0


# Above this, two files share no more than random chance would predict. The
# Kid LAROI's "WITHOUT YOU" against Avicii's scores 0.501; the least similar
# pair the name rules correctly merge scores 0.440, so this vetoes the
# unrelated without splitting genuine duplicates.
BER_UNRELATED = 0.47
# Above this difference in length, two copies are not the same edit, and the
# shorter one is the one worth keeping: the extra is an intro, an outro or a
# spoken channel tag rather than more song.
EDIT_GAP = 15.0
_FP_CACHE = {}


def _fingerprints():
    """-> {path: decoded fingerprint}. Loaded once, on first use."""
    if _FP_CACHE:
        return _FP_CACHE
    try:
        import chromaprint
    except ImportError:
        return _FP_CACHE
    path = os.path.join(HERE, "cache", "fingerprints.json")
    if not os.path.exists(path):
        return _FP_CACHE
    for v in json.load(open(path)).values():
        if v.get("fingerprint") and v.get("path"):
            try:
                _FP_CACHE[v["path"]] = chromaprint.decode_fingerprint(
                    v["fingerprint"].encode())[0]
            except Exception:
                pass
    return _FP_CACHE


def _unrelated_audio(a, b):
    """-> True when the two recordings share nothing acoustically.

    dedupe.py compares fingerprints too, but only within +/-8 frames, which is
    why it cannot pair two uploads of one song: a different intro length moves
    them far further apart than that. Searching a wide offset makes the same
    fingerprints usable here as a veto -- not to prove two files ARE the same
    song, but to catch the case where the names say they are and the audio
    flatly disagrees.
    """
    fps = _fingerprints()
    x, y = fps.get(a["path"]), fps.get(b["path"])
    if not x or not y:
        return False
    best = 1.0
    for off in range(-200, 201):
        aa, bb = x[max(0, off):], y[max(0, -off):]
        n = min(len(aa), len(bb))
        if n < 100:
            continue
        bits = sum(popcount(aa[i] ^ bb[i]) for i in range(n))
        best = min(best, bits / (32.0 * n))
        if best < 0.2:
            break                      # already clearly the same, stop early
    return best >= BER_UNRELATED


def _bpm_agrees(a, b, analysis, tol=0.07):
    """-> True if two files share a tempo, allowing an octave.

    Unknown tempo counts as agreement: this only ever needs to VETO a merge
    the names already proposed, never to propose one.

    The tolerance is loose because an octave comparison doubles the estimation
    error too. Two copies of "The Lazy Song" measured 172.7 and 90.0 -- 4.2%
    apart once halved -- and a 4% gate split a genuine duplicate. It stays far
    away from the case this exists to catch: 93 against 134 BPM is no octave
    of anything.
    """
    x = analysis.get(a["path"], {}).get("bpm")
    y = analysis.get(b["path"], {}).get("bpm")
    if not x or not y:
        return True
    for mult in (0.5, 1.0, 2.0):
        if abs(x * mult - y) <= tol * y:
            return True
    return False


def groups(rows, analysis, mapping, max_gap):
    """-> list of [entry, ...] where every entry is the same song."""
    buckets = defaultdict(list)
    for r in rows:
        a, t = r.get("proposed_artist"), r.get("proposed_title")
        if not (a and t):
            continue
        lead = artist_names.canonical(a, mapping)
        buckets[(key_of(lead), key_of(t))].append(r)

    # Same song, two different credited artists. It happens when one copy was
    # named off a channel and the other off a note or a link: "Amazing Lyrics -
    # idfc x soap" and "idfc x soap" are one track, and no artist+title key can
    # see that. The filenames can: one is a suffix of the other. Duration still
    # has to agree, so two unrelated songs sharing a title stay apart.
    merged = {}
    for key, members in list(buckets.items()):
        for other, others in buckets.items():
            if other == key or other[1] != key[1] or not others:
                continue
            # The filenames are the evidence, not the credited artists:
            # "Amazing Lyrics - idfc x soap" and "idfc x soap" name one song
            # even though one says Blackbear and the other says the channel.
            f1 = key_of(os.path.splitext(members[0]["file"])[0])
            f2 = key_of(os.path.splitext(others[0]["file"])[0])
            if not (f1 and f2) or not (f1 in f2 or f2 in f1):
                continue
            d1 = _dur(members[0], analysis)
            d2 = _dur(others[0], analysis)
            # Tempo has to agree as well as length. Two unrelated songs can
            # share a title and a running time -- The Kid LAROI's "WITHOUT
            # YOU" is 182.1s and Avicii's "Without You" is 181.7s, and the
            # filename rule happily merged them because "without you" is
            # contained in "the kid laroi without you". They are 93 and 134
            # BPM. Tempo comes from the waveform, so no naming can fool it.
            if not _bpm_agrees(members[0], others[0], analysis):
                continue
            if _unrelated_audio(members[0], others[0]):
                continue
            if d1 and d2 and abs(d1 - d2) <= max(max_gap, 0.08 * max(d1, d2)):
                merged.setdefault(key[1], set()).update([key, other])
    for _title, keys in merged.items():
        keys = sorted(keys)
        target = keys[0]
        for k in keys[1:]:
            buckets[target] += buckets.pop(k, [])

    out = []
    for (_, _), members in buckets.items():
        if len(members) < 2:
            continue
        # Split by version, then by duration, so only genuinely interchangeable
        # files end up in the same group.
        by_version = defaultdict(list)
        for m in members:
            base = members[0]["proposed_title"]
            mism = version_mismatch(base, m["proposed_title"])
            by_version["|".join(mism) if mism else ""].append(m)
        for group in by_version.values():
            if len(group) < 2:
                continue
            group.sort(key=lambda r: (analysis.get(r["path"], {}).get("decoded_secs") or 0))
            run = [group[0]]
            for m in group[1:]:
                prev = analysis.get(run[-1]["path"], {}).get("decoded_secs") or 0
                cur = analysis.get(m["path"], {}).get("decoded_secs") or 0
                # Scale the tolerance with the song. Two YouTube uploads of one
                # track differ by however much intro each has -- 12.5s for
                # "GRŠE - Badr Hari", 13.9s for "Hladno pivo - Samo za taj
                # osjećaj" -- while an extended mix is half again as long, so a
                # percentage separates them where a fixed second count cannot.
                # Where the audio agrees, length no longer has to. Two copies
                # of one song can be a minute apart because one is a video rip
                # with a cold open, and splitting them on that left the song
                # in the library twice. The fingerprint has already said they
                # are the same recording; the keeper rule then prefers the
                # shorter, which is the one without the intro.
                allow = max(max_gap, 0.08 * max(prev, cur))
                if not _unrelated_audio(run[-1], m):
                    allow = max(allow, EDIT_GAP * 6)
                # Same artist, same title, plausible length -- and still worth
                # asking the audio. Two songs can share both.
                if _unrelated_audio(run[-1], m) or \
                        not _bpm_agrees(run[-1], m, analysis):
                    if len(run) > 1:
                        out.append(run)
                    run = [m]
                elif prev and cur and abs(cur - prev) > allow:
                    if len(run) > 1:
                        out.append(run)
                    run = [m]
                else:
                    run.append(m)
            if len(run) > 1:
                out.append(run)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--max-gap", type=float, default=12.0)
    # "hinted" belongs here too. Leaving it out meant every row carrying one
    # of your notes was invisible to grouping, so exactly the duplicates you
    # pointed at by hand were the ones that survived.
    ap.add_argument("--tiers", default="auto,review,hinted,suspect")
    args = ap.parse_args()

    tiers = {t.strip() for t in args.tiers.split(",") if t.strip()}
    rows = [r for r in json.load(open(REVIEW)) if r["tier"] in tiers]
    analysis = {v["path"]: v for v in json.load(open(ANALYSIS)).values()}
    mapping, _why, _groups = artist_names.build(rows)

    found = groups(rows, analysis, mapping, args.max_gap)
    losers, records = {}, []
    for g in found:
        # Quality decides between copies of the same length. Where they differ
        # by more than a bar or two, length decides first and the SHORTER one
        # wins: the extra is almost always an intro, an outro or a spoken tag
        # on a video rip, not more song. "Prevarena" is 4:31 and 5:19; the
        # 5:19 is the official video with a long cold open.
        durs = [_dur(r, analysis) for r in g if _dur(r, analysis)]
        spread = (max(durs) - min(durs)) if len(durs) > 1 else 0
        if spread > EDIT_GAP:
            ranked = sorted(g, key=lambda r: (_dur(r, analysis) or 1e9,
                                              [-x for x in quality_rank(r, analysis)]))
        else:
            ranked = sorted(g, key=lambda r: quality_rank(r, analysis), reverse=True)
        keep, drop = ranked[0], ranked[1:]
        records.append({
            "artist": keep.get("proposed_artist"),
            "title": keep.get("proposed_title"),
            "keep": keep["path"],
            "keep_cutoff": analysis.get(keep["path"], {}).get("spectral_cutoff_hz"),
            "drop": [d["path"] for d in drop],
        })
        for d in drop:
            losers[d["path"]] = {"duplicate_of": keep["path"],
                                 "artist": keep.get("proposed_artist"),
                                 "title": keep.get("proposed_title")}

    print(f"\n  {len(found)} songs exist more than once by name, "
          f"{len(losers)} redundant files\n")
    for rec in sorted(records, key=lambda r: -len(r["drop"]))[:30]:
        kc = rec["keep_cutoff"]
        print(f"    {str(rec['artist'])[:22]:22} | {str(rec['title'])[:28]:28}")
        print(f"        keep {os.path.basename(rec['keep'])[:56]:56} "
              f"{f'{kc}Hz' if kc else ''}")
        for d in rec["drop"]:
            dc = analysis.get(d, {}).get("spectral_cutoff_hz")
            print(f"        drop {os.path.basename(d)[:56]:56} "
                  f"{f'{dc}Hz' if dc else ''}")
    if len(records) > 30:
        print(f"\n    ... and {len(records)-30} more groups")

    if args.apply:
        json.dump({"losers": losers, "groups": records},
                  open(OUT + ".tmp", "w"), ensure_ascii=False, indent=1)
        os.replace(OUT + ".tmp", OUT)
        print(f"\n  -> {OUT}\n  write_tags.py will now emit one copy per song\n")
    else:
        print("\n  report only. Re-run with --apply to record the losers.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
