#!/usr/bin/env python3
"""Compare written tags against measured ground truth. Reports, changes nothing.

Inputs are the two measurement files produced by audit_truth.py (ffprobe +
ffmpeg ebur128) and audit_cutoff.py (essentia), plus the pipeline's own caches.

Usage: audit_compare.py [--truth cache/audit_truth.json]
                        [--cutoff cache/audit_cutoff.json]
                        [--json report.json]
"""
import argparse
import json
import math
import os
import re
import statistics
import sys
from collections import Counter

import mutagen

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

# Imported, never redeclared: a second copy of this number means the audit
# silently grades against a target the pipeline no longer uses.
from pipeline.write_tags import RG_TARGET_LUFS  # noqa: E402

YT = re.compile(r"(?:v=|youtu\.be/|/watch\?v=)([A-Za-z0-9_-]{11})")


def tags_of(path):
    """-> flat dict of the tags we care about, MP3 and MP4 alike."""
    try:
        f = mutagen.File(path)
    except Exception as e:
        return {"_error": f"{type(e).__name__}"}
    if f is None or f.tags is None:
        return {}
    t, out = f.tags, {}
    if hasattr(t, "getall"):                                   # ID3
        for fr in t.getall("TXXX"):
            if fr.text:
                out[fr.desc.upper()] = str(fr.text[0])
        tlen = t.getall("TLEN")
        if tlen and tlen[0].text:
            out["_TLEN"] = str(tlen[0].text[0])
        out["_comment"] = " ".join(str(fr.text[0]) for fr in t.getall("COMM")
                                   if fr.text)
    else:                                                      # MP4
        for k, v in t.items():
            if k.startswith("----:com.apple.iTunes:") and v:
                val = v[0]
                out[k.split(":")[-1].upper()] = (
                    val.decode("utf-8", "ignore") if isinstance(val, bytes)
                    else str(val))
        out["_comment"] = " ".join(str(x) for x in (t.get("\xa9cmt") or []))
    out["_len"] = getattr(getattr(f, "info", None), "length", None)
    out["_bitrate"] = getattr(getattr(f, "info", None), "bitrate", None)
    return out


def num(v):
    try:
        return float(str(v).replace(" dB", "").strip())
    except (TypeError, ValueError):
        return None


def pct(n, d):
    return f"{n} ({100.0 * n / d:.1f}%)" if d else str(n)


def describe(errs, unit="LU"):
    """-> a one-line distribution summary of a list of signed errors."""
    if not errs:
        return "no data"
    a = sorted(abs(e) for e in errs)
    # Nearest-rank percentiles. int(n*0.95) lands one rank high, which
    # overstates the tail on exactly the reports this tool exists to settle.
    def pctl(q):
        return a[max(0, math.ceil(len(a) * q) - 1)]
    return (f"n={len(errs)} mean={statistics.mean(errs):+.2f} "
            f"median={statistics.median(errs):+.2f} "
            f"|err| p50={pctl(0.50):.2f} p95={pctl(0.95):.2f} "
            f"max={a[-1]:.2f} {unit}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--truth", default="cache/audit_truth.json")
    ap.add_argument("--cutoff", default="cache/audit_cutoff.json")
    ap.add_argument("--json", default="cache/audit_report.json")
    a = ap.parse_args()

    truth = json.load(open(a.truth))
    cut = json.load(open(a.cutoff)) if os.path.exists(a.cutoff) else {}
    analysis = json.load(open("cache/analysis.json"))
    # MUZZI_SOURCE_FILE is a basename, so that is all there is to join on.
    # Two source folders can hold the same filename, and keeping the last
    # one seen would silently grade a file against another file's analysis.
    # Ambiguous names are dropped instead: no cache entry beats a wrong one.
    by_src, ambiguous = {}, set()
    for v in analysis.values():
        if not v.get("path"):
            continue
        name = os.path.basename(v["path"])
        if name in by_src and by_src[name].get("path") != v["path"]:
            ambiguous.add(name)
        by_src[name] = v
    for name in ambiguous:
        by_src.pop(name, None)
    if ambiguous:
        print(f"  note: {len(ambiguous)} source filenames occur in more than "
              f"one folder; those files are excluded from cache comparisons\n")

    rows = []
    for path, tr in truth.items():
        t = tags_of(path)
        src = t.get("MUZZI_SOURCE_FILE")
        rows.append({"path": path, "tags": t, "truth": tr,
                     "cut": cut.get(path, {}),
                     "cache": by_src.get(src or "", {}), "src": src})

    n = len(rows)
    R = {"files": n}
    print(f"=== {n} output files ===\n")

    # Every section below skips rows whose measurement is missing, so a file
    # that failed to probe or decode contributes to no mismatch count. Left
    # unsaid, a run where half the library failed reads exactly like a clean
    # one. Report the failures first, before any "0 mismatches" line.
    failed = {}
    for r in rows:
        why = [k for k in ("probe_error", "lufs_error", "mono_lufs_error")
               if r["truth"].get(k)]
        if r["cut"].get("error"):
            why.append("cutoff_error")
        if not r["cache"]:
            why.append("no_cache_entry")
        if why:
            failed[r["path"]] = why
    R["unmeasured"] = {"files": len(failed),
                       "reasons": dict(Counter(w for v in failed.values()
                                               for w in v)),
                       "worst": sorted(failed)[:25]}
    if failed:
        print(f"UNMEASURED  {pct(len(failed), n)} of files are missing at "
              f"least one measurement and are skipped below")
        for k, v in sorted(R["unmeasured"]["reasons"].items(),
                           key=lambda x: -x[1]):
            print(f"    {k:16s} {v}")
        print()

    # ffmpeg conceals decode errors and still exits 0, so a measurement can
    # come from partially reconstructed audio without any failure signal.
    # These are not withheld -- on this library every one is a single
    # "Header missing" frame at the start of an MP3, and dropping 44 sound
    # measurements over one bad frame would cost more than it protects --
    # but a run where the count climbs should not look identical to a clean
    # one.
    concealed = {r["path"]: r["truth"]["decode_errors"] for r in rows
                 if (r["truth"].get("decode_errors") or 0) > 0}
    R["decode_errors"] = {"files": len(concealed),
                          "max": max(concealed.values(), default=0)}
    if concealed:
        print(f"DECODE      {pct(len(concealed), n)} of files decoded with "
              f"concealed errors (max {max(concealed.values())} per file); "
              f"their measurements are kept\n")

    # ---------- loudness ----------
    e_true, e_mono, rg_internal, rg_true, clip = [], [], [], [], 0
    for r in rows:
        tag = num(r["tags"].get("LOUDNESS_LUFS"))
        tl, ml = r["truth"].get("true_lufs"), r["truth"].get("mono_lufs")
        if tag is None:
            continue
        if tl is not None:
            e_true.append(tag - tl)
        if ml is not None:
            e_mono.append(tag - ml)
        gain = num(r["tags"].get("REPLAYGAIN_TRACK_GAIN"))
        if gain is not None:
            rg_internal.append(gain - (RG_TARGET_LUFS - tag))
            if tl is not None:
                rg_true.append(gain - (RG_TARGET_LUFS - tl))
            pk = r["truth"].get("true_peak_db")
            if pk is not None and pk + gain > 0:
                clip += 1
    print("LOUDNESS")
    print("  tag vs real file (EBU R128):", describe(e_true))
    print("  tag vs mono downmix        :", describe(e_mono))
    print("  ReplayGain vs its own tag  :", describe(rg_internal, "dB"))
    print("  ReplayGain vs real file    :", describe(rg_true, "dB"))
    print("  files that clip after gain :", pct(clip, n),
          "(no REPLAYGAIN_TRACK_PEAK is written, so players cannot prevent it)")
    R["loudness"] = {"vs_real": describe(e_true), "vs_mono": describe(e_mono),
                     "rg_internal": describe(rg_internal, "dB"),
                     "rg_vs_real": describe(rg_true, "dB"), "clipping": clip}

    # ---------- duration ----------
    dur_err, big, tlen_bad = [], [], 0
    for r in rows:
        ps = r["truth"].get("probe_secs")
        cs = r["cache"].get("decoded_secs")
        if ps and cs:
            d = cs - ps
            dur_err.append(d)
            if abs(d) > 5:
                big.append((os.path.basename(r["path"]), round(cs, 1),
                            round(ps, 1), round(d, 1)))
        tl = num(r["tags"].get("_TLEN"))
        if tl and ps and abs(tl / 1000.0 - ps) > 2:
            tlen_bad += 1
    print("\nDURATION")
    print("  cached decoded_secs vs file:", describe(dur_err, "s"))
    print("  off by more than 5s        :", pct(len(big), n))
    for b in sorted(big, key=lambda x: -abs(x[3]))[:10]:
        print(f"    {b[0][:58]:60s} cache={b[1]}s file={b[2]}s ({b[3]:+}s)")
    print("  stale TLEN frames          :", tlen_bad)
    R["duration"] = {"summary": describe(dur_err, "s"), "over_5s": len(big),
                     "worst": sorted(big, key=lambda x: -abs(x[3]))[:25],
                     "tlen_bad": tlen_bad}

    # ---------- bitrate ----------
    br_err, br_big = [], []
    for r in rows:
        cb = r["cache"].get("bitrate_kbps")
        # Format level, not stream level. Neither ffprobe figure is clean:
        # the format one counts the ID3 tag, so a file with large embedded
        # art reads ~20 kbps high; the stream one is 4-8 kbps on the YouTube
        # AAC files, which lack the esds box ffprobe would read it from --
        # the same absence that makes essentia report 1. Format is wrong by
        # a bounded overhead, stream is wrong by 20x, so format wins.
        pb = r["truth"].get("probe_bitrate_kbps")
        if cb and pb:
            d = cb - pb
            br_err.append(d)
            if abs(d) > 16:
                br_big.append((os.path.basename(r["path"]), cb, pb))
    print("\nBITRATE  (no bitrate tag is written; this checks the cached value"
          " the quality grade is built from)")
    print("  cached vs file             :", describe(br_err, "kbps"))
    print("  off by more than 16 kbps   :", pct(len(br_big), n))
    for b in br_big[:10]:
        print(f"    {b[0][:58]:60s} cache={b[1]} file={b[2]}")
    R["bitrate"] = {"summary": describe(br_err, "kbps"), "over_16": len(br_big),
                    "worst": br_big[:25]}

    # ---------- quality ----------
    if cut:
        grade_mismatch, tagcut_err = [], []
        susp_mismatch, susp_checked = [], 0
        conf = Counter()
        for r in rows:
            c = r["cut"]
            if c.get("error"):
                continue
            tc = num(r["tags"].get("SPECTRAL_CUTOFF_HZ"))
            rc = c.get("true_cutoff_hz")
            if tc and rc:
                tagcut_err.append(tc - rc)
            tq, rq = r["tags"].get("QUALITY"), c.get("true_quality_grade")
            if tq and rq:
                conf[(tq, rq)] += 1
                if tq != rq:
                    grade_mismatch.append(
                        (os.path.basename(r["path"]), tq, rq,
                         int(tc) if tc else None, rc))
            # quality_suspect is cache-only -- no tag carries it -- and it is
            # the field the bitrate feeds, so a bitrate regression shows up
            # here and nowhere else. Grades alone would report all clear.
            cs, rs = r["cache"].get("quality_suspect"), c.get("true_quality_suspect")
            if r["cache"] and cs is not None and rs is not None:
                susp_checked += 1
                if bool(cs) != bool(rs):
                    susp_mismatch.append(
                        (os.path.basename(r["path"]), bool(cs), bool(rs),
                         r["cache"].get("bitrate_kbps"), c.get("true_bitrate_kbps")))
        print("\nQUALITY")
        print("  tagged cutoff vs remeasured:", describe(tagcut_err, "Hz"))
        print("  grade mismatches           :",
              pct(len(grade_mismatch), sum(conf.values())))
        print("  suspect-flag mismatches    :",
              pct(len(susp_mismatch), susp_checked))
        for m in susp_mismatch[:10]:
            print(f"    {m[0][:46]:48s} cache={m[1]!s:5s} actual={m[2]!s:5s} "
                  f"bitrate cache={m[3]} real={m[4]}")
        for k, v in conf.most_common():
            flag = "" if k[0] == k[1] else "   <-- mismatch"
            print(f"    tag={k[0]:8s} actual={k[1]:8s} {v}{flag}")
        for m in grade_mismatch[:10]:
            print(f"    {m[0][:52]:54s} tag={m[1]:6s} actual={m[2]:6s} "
                  f"cutoff tag={m[3]} real={m[4]}")
        R["quality"] = {"cutoff": describe(tagcut_err, "Hz"),
                        "mismatches": len(grade_mismatch),
                        "suspect_checked": susp_checked,
                        "suspect_mismatches": len(susp_mismatch),
                        "suspect_worst": susp_mismatch[:25],
                        "matrix": {f"{k[0]}->{k[1]}": v for k, v in conf.items()},
                        "worst": grade_mismatch[:25]}

    # ---------- youtube ----------
    # Guarded like every other cache read above. tagseed is not part of
    # run.py and hint_youtube only exists once hints have been resolved, so
    # a library that has never used either is normal, not an error.
    def load(name):
        path = os.path.join("cache", name)
        return json.load(open(path)) if os.path.exists(path) else {}

    ts = load("tagseed.json")
    seed_yt = {os.path.basename(k): v.get("youtube_id")
               for k, v in ts.items() if isinstance(v, dict) and v.get("youtube_id")}
    hy = load("hint_youtube.json")
    in_file, dur_mismatch, ok_dur = 0, [], 0
    for r in rows:
        m = YT.search(r["tags"].get("_comment") or "")
        vid = m.group(1) if m else None
        if vid:
            in_file += 1
        v = vid or seed_yt.get(r["src"] or "")
        h = hy.get(v or "")
        ps = r["truth"].get("probe_secs")
        if h and h.get("duration") and ps:
            d = ps - float(h["duration"])
            if abs(d) > 5:
                dur_mismatch.append((os.path.basename(r["path"]), v,
                                     round(ps), h["duration"], round(d)))
            else:
                ok_dur += 1
    print("\nYOUTUBE")
    print("  output files carrying a video id:", pct(in_file, n),
          "- all of them in an inherited comment, not a Muzzi tag")
    print("  source files the pipeline extracted an id from:", len(seed_yt))
    print("  ids known across all caches:",
          len(set(seed_yt.values()) | set(hy)))
    print("  duration cross-check vs YouTube: matched", ok_dur,
          "mismatched", len(dur_mismatch))
    for d in sorted(dur_mismatch, key=lambda x: -abs(x[4]))[:10]:
        print(f"    {d[0][:50]:52s} {d[1]} file={d[2]}s youtube={d[3]}s ({d[4]:+}s)")
    R["youtube"] = {"in_output": in_file, "extracted": len(seed_yt),
                    "duration_ok": ok_dur, "duration_mismatch": len(dur_mismatch),
                    "worst": sorted(dur_mismatch, key=lambda x: -abs(x[4]))[:25]}

    with open(a.json, "w") as fh:
        json.dump(R, fh, indent=1)
    print("\nwrote", a.json)


if __name__ == "__main__":
    main()
