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
import os
import re
import statistics
import sys
from collections import Counter, defaultdict

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
    return (f"n={len(errs)} mean={statistics.mean(errs):+.2f} "
            f"median={statistics.median(errs):+.2f} "
            f"|err| p50={a[len(a)//2]:.2f} p95={a[int(len(a)*.95)]:.2f} "
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
    by_src = {}
    for v in analysis.values():
        if v.get("path"):
            by_src[os.path.basename(v["path"])] = v

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
        cut_err, grade_mismatch, tagcut_err = [], [], []
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
        print("\nQUALITY")
        print("  tagged cutoff vs remeasured:", describe(tagcut_err, "Hz"))
        print("  grade mismatches           :",
              pct(len(grade_mismatch), sum(conf.values())))
        for k, v in conf.most_common():
            flag = "" if k[0] == k[1] else "   <-- mismatch"
            print(f"    tag={k[0]:8s} actual={k[1]:8s} {v}{flag}")
        for m in grade_mismatch[:10]:
            print(f"    {m[0][:52]:54s} tag={m[1]:6s} actual={m[2]:6s} "
                  f"cutoff tag={m[3]} real={m[4]}")
        R["quality"] = {"cutoff": describe(tagcut_err, "Hz"),
                        "mismatches": len(grade_mismatch),
                        "matrix": {f"{k[0]}->{k[1]}": v for k, v in conf.items()},
                        "worst": grade_mismatch[:25]}

    # ---------- youtube ----------
    ts = json.load(open("cache/tagseed.json"))
    seed_yt = {os.path.basename(k): v.get("youtube_id")
               for k, v in ts.items() if isinstance(v, dict) and v.get("youtube_id")}
    hy = json.load(open("cache/hint_youtube.json"))
    in_file, could, dur_mismatch, ok_dur = 0, 0, [], 0
    for r in rows:
        m = YT.search(r["tags"].get("_comment") or "")
        vid = m.group(1) if m else None
        if vid:
            in_file += 1
        seed = seed_yt.get(r["src"] or "")
        if seed:
            could += 1
        v = vid or seed
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
