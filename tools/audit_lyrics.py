#!/usr/bin/env python3
"""Grade the lyric cache and the .lrc sidecars against the audio they describe.

verify.py reads tags and reports whether a file was processed. It says nothing
about whether the lyrics inside are the right song's, or whether their
timestamps line up with the audio. This does, and it does it offline: every
number below comes from cache/ plus the written output, so it is re-runnable
after any import instead of re-derived by hand.

Three defects it exists to count, all measured on this library before the fix:

  * duration mismatch - a sheet timed for a different edit. A 108 s lyric sheet
    sat beside a 289 s file; 315 of 1178 comparable sheets were more than 2 s
    out, which is what makes lyrics run ahead of the song.
  * wrong artist - 85 written sidecars carried another artist's words.
  * wrong title - 20 more carried a different song by the *right* artist, which
    a duration check alone cannot catch (only 58 of the 94 wrong entries also
    failed a 5 s duration gate).

Deliberately imports fit() from pipeline.webmatch rather than reimplementing
it, so this grades the pipeline's own notion of agreement and not a lookalike
that might disagree with it.

Usage: audit_lyrics.py [--out cache/audit_lyrics.json] [--lrc-dir out/_all]
                       [--gate 2.0] [--json]
"""
import argparse
import json
import os
import re
import statistics
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from pipeline.webmatch import fit  # noqa: E402

CACHE = os.path.join(HERE, "cache")
LYRICS = os.path.join(CACHE, "lyrics.json")
REVIEW = os.path.join(CACHE, "review.json")
ANALYSIS = os.path.join(CACHE, "analysis.json")
DEFAULT_OUT = os.path.join(CACHE, "audit_lyrics.json")
DEFAULT_LRC = os.path.join(HERE, "out", "_all")

# Same shape verify_lyrics._LRC_TS accepts, kept local so this stays runnable
# even if that module cannot import faster_whisper.
_TS = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]")

FIT_GATE = 0.5


def stamps(text):
    """-> [seconds] every timestamp in an LRC body, in file order."""
    out = []
    for m in _TS.finditer(text or ""):
        frac = m.group(3) or "0"
        out.append(int(m.group(1)) * 60 + int(m.group(2)) +
                   float(f"0.{frac}"))
    return out


def load(path):
    return json.load(open(path)) if os.path.exists(path) else {}


def grade(entry, artist, title, decoded_secs, gate):
    """-> dict of the three quality signals for one cache entry.

    Returns None values rather than guesses where the input is missing: a
    legacy entry with no 'matched' cannot be graded on artist or title, and
    saying so is the point (121 such entries skip every check today).
    """
    matched = entry.get("matched") or ""
    md = entry.get("matched_duration")
    art_fit = tit_fit = None
    if " - " in matched:
        ma, _, mt = matched.partition(" - ")
        art_fit, tit_fit = round(fit(artist, ma), 3), round(fit(title, mt), 3)
    delta = round(decoded_secs - md, 2) if (decoded_secs and md) else None
    reasons = []
    if art_fit is not None and art_fit < FIT_GATE:
        reasons.append("wrong_artist")
    elif tit_fit is not None and tit_fit < FIT_GATE:
        reasons.append("wrong_title")
    if art_fit is None and (entry.get("synced") or entry.get("plain")):
        reasons.append("ungradeable_legacy")
    if delta is not None and abs(delta) > gate:
        reasons.append("duration_mismatch")
    return {"artist_fit": art_fit, "title_fit": tit_fit,
            "matched": matched or None, "matched_duration": md,
            "decoded_secs": decoded_secs, "delta": delta,
            "synced": bool(entry.get("synced")),
            "plain_only": bool(entry.get("plain") and not entry.get("synced")),
            "status": entry.get("status"), "selector": entry.get("selector"),
            "reasons": reasons}


def scan_sidecars(root):
    """-> {stem: [seconds]} for every .lrc under root."""
    out = {}
    for dp, _, names in os.walk(root):
        for n in names:
            if not n.lower().endswith(".lrc"):
                continue
            p = os.path.join(dp, n)
            try:
                with open(p, encoding="utf-8") as fh:
                    out[os.path.splitext(p)[0]] = stamps(fh.read())
            except Exception:
                out[os.path.splitext(p)[0]] = []
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--lrc-dir", default=DEFAULT_LRC)
    ap.add_argument("--gate", type=float, default=2.0,
                    help="duration tolerance in seconds (default 2.0, the "
                         "value LRCLIB /api/get, LRCLIBee and Music Assistant "
                         "all use)")
    ap.add_argument("--json", action="store_true", help="print the JSON too")
    args = ap.parse_args()

    lyr, review, ana = load(LYRICS), load(REVIEW), load(ANALYSIS)
    if not lyr:
        sys.exit(f"no lyric cache at {LYRICS}")
    dur = {v["path"]: v.get("decoded_secs")
           for v in ana.values() if v.get("path")}

    # First review row wins, matching how lyrics_fetch keys its cache.
    row_for = {}
    for r in review:
        if r.get("proposed_artist") and r.get("proposed_title"):
            k = f'{r["proposed_artist"]}|{r["proposed_title"]}'.lower()
            row_for.setdefault(k, r)

    graded, cov = {}, {"synced": 0, "plain_only": 0, "absent": 0,
                       "never_queried": 0, "no_identification": 0}
    for r in review:
        if not (r.get("proposed_artist") and r.get("proposed_title")):
            cov["no_identification"] += 1
            continue
        k = f'{r["proposed_artist"]}|{r["proposed_title"]}'.lower()
        e = lyr.get(k)
        if not isinstance(e, dict):
            cov["never_queried"] += 1
            continue
        if e.get("synced"):
            cov["synced"] += 1
        elif e.get("plain"):
            cov["plain_only"] += 1
        else:
            cov["absent"] += 1

    for k, e in lyr.items():
        if not isinstance(e, dict):
            continue
        artist, _, title = k.rpartition("|")
        r = row_for.get(k)
        g = grade(e, artist, title, dur.get(r["path"]) if r else None, args.gate)
        g["key"], g["balkan"] = k, bool(r and r.get("balkan"))
        g["path"] = r["path"] if r else None
        graded[k] = g

    sidecars = scan_sidecars(args.lrc_dir) if os.path.isdir(args.lrc_dir) else {}
    # A sidecar whose last timestamp lands past the end of the audio can never
    # fire. Matching stems back to a review row is unreliable after renaming,
    # so this is counted against the sheet's own duration evidence instead.
    past_end = 0
    for g in graded.values():
        if not (g["synced"] and g["decoded_secs"]):
            continue
        e = lyr.get(g["key"], {})
        st = stamps(e.get("synced"))
        if st and max(st) > g["decoded_secs"]:
            past_end += 1

    syn = [g for g in graded.values() if g["synced"]]
    deltas = [abs(g["delta"]) for g in syn if g["delta"] is not None]
    checks = {
        "1_duration_mismatch": sum(1 for g in syn if g["delta"] is not None
                                   and abs(g["delta"]) > args.gate),
        "2_wrong_artist": sum(1 for g in syn if g["artist_fit"] is not None
                              and g["artist_fit"] < FIT_GATE),
        "3_wrong_title": sum(1 for g in syn
                             if g["artist_fit"] is not None
                             and g["artist_fit"] >= FIT_GATE
                             and g["title_fit"] is not None
                             and g["title_fit"] < FIT_GATE),
        "4_stamps_past_end": past_end,
        "5_ungradeable_legacy": sum(1 for g in syn if g["artist_fit"] is None),
        "6_synced_coverage": cov["synced"],
        "7_lrc_on_disk": len(sidecars),
    }
    bal = [g for g in syn if g["balkan"] and g["delta"] is not None]
    oth = [g for g in syn if not g["balkan"] and g["delta"] is not None]

    def share(rows, th):
        return (round(100.0 * sum(1 for g in rows
                                  if abs(g["delta"]) > th) / len(rows), 1)
                if rows else None)

    report = {
        "gate_secs": args.gate,
        "fit_gate": FIT_GATE,
        "entries": len(graded),
        "coverage": cov,
        "checks": checks,
        "timing": {
            "comparable": len(deltas),
            "median_abs_delta": round(statistics.median(deltas), 2) if deltas else None,
            "early_lyrics_file_longer": sum(1 for g in syn if g["delta"] is not None
                                            and g["delta"] > args.gate),
            "late_lyrics_file_shorter": sum(1 for g in syn if g["delta"] is not None
                                            and g["delta"] < -args.gate),
            "over_gate_pct_balkan": share(bal, args.gate),
            "over_gate_pct_other": share(oth, args.gate),
        },
        "selector_versions": {},
    }
    for g in graded.values():
        s = str(g.get("selector"))
        report["selector_versions"][s] = report["selector_versions"].get(s, 0) + 1

    with open(args.out, "w") as fh:
        json.dump({"report": report, "entries": graded}, fh,
                  ensure_ascii=False, indent=1)

    print(f"\n  lyric cache entries              {len(graded):5}")
    print(f"  duration gate                    {args.gate} s\n")
    print("  coverage")
    for k, v in cov.items():
        print(f"    {k:28} {v:5}")
    print("\n  acceptance checks (target 0 unless noted)")
    for k, v in checks.items():
        print(f"    {k:28} {v:5}")
    t = report["timing"]
    print(f"\n  timing  median |delta| {t['median_abs_delta']} s over "
          f"{t['comparable']} comparable")
    print(f"    lyrics early (file longer)     {t['early_lyrics_file_longer']:5}")
    print(f"    lyrics late  (file shorter)    {t['late_lyrics_file_shorter']:5}")
    print(f"    over gate, balkan / other      "
          f"{t['over_gate_pct_balkan']}% / {t['over_gate_pct_other']}%")
    print(f"\n  selector versions {report['selector_versions']}")
    print(f"\n  wrote {args.out}\n")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
