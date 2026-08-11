#!/usr/bin/env python3
"""Stage 3: find duplicate recordings by audio, not by filename or tags.

Twenty years of downloads means the same song arrives repeatedly under
different names, bitrates and spellings. Metadata comparison cannot see this
(the files often have no metadata at all), so we compare the Chromaprint
fingerprints themselves: the same recording at 128kbps and 320kbps produces
near-identical fingerprints even when every tag differs.

Deliberately conservative. A remix, a live version and an acoustic take are
DIFFERENT songs that sound similar, and losing one would be worse than keeping
a duplicate -- so the bit-error threshold is tight and duration must match
closely. Nothing is ever deleted; losers are moved to a quarantine folder.

Usage:
  dedupe.py                      # report only
  dedupe.py --quarantine DIR     # move the losers there
"""
import argparse
import json
import os
import shutil
import sys
from collections import defaultdict

import chromaprint

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

FP_CACHE = os.path.join(HERE, "cache", "fingerprints.json")
ANALYSIS = os.path.join(HERE, "cache", "analysis.json")
OUT = os.path.join(HERE, "cache", "duplicates.json")

# Two encodes of one recording differ in a few percent of bits. Distinct songs
# differ in ~45%+. 0.12 sits well clear of both, and being wrong in the
# permissive direction is what loses a remix, so err tight.
BER_THRESHOLD = 0.12
# Same recording, different encoder => sub-second length differences. A remix
# or extended cut differs by much more.
DURATION_TOLERANCE = 2.5


def popcount(x):
    return bin(x).count("1")


def bit_error_rate(a, b, max_offset=8):
    """Fraction of differing bits over the aligned overlap, best of a few offsets.

    Encoders trim or pad silence at the start, so identical audio can be a
    frame or two out of phase. Trying small offsets fixes that.
    """
    best = 1.0
    for off in range(-max_offset, max_offset + 1):
        if off >= 0:
            x, y = a[off:], b
        else:
            x, y = a, b[-off:]
        n = min(len(x), len(y))
        if n < 20:                       # too little overlap to judge
            continue
        diff = sum(popcount(x[i] ^ y[i]) for i in range(n))
        ber = diff / (n * 32.0)
        if ber < best:
            best = ber
    return best


def quality_rank(entry, analysis):
    """Higher is better. Measured bandwidth beats the claimed bitrate.

    A file can claim 320kbps and be a 128kbps transcode; the spectral cutoff
    computed in stage 5 tells the truth, so it leads.
    """
    a = analysis.get(entry["path"], {})
    cutoff = a.get("spectral_cutoff_hz") or 0
    bitrate = a.get("bitrate_kbps") or 0
    size = 0
    try:
        size = os.path.getsize(entry["path"])
    except OSError:
        pass
    return (cutoff, bitrate, size)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quarantine", help="move non-winners into this folder")
    ap.add_argument("--threshold", type=float, default=BER_THRESHOLD)
    args = ap.parse_args()

    if not os.path.exists(FP_CACHE):
        sys.exit("no cache/fingerprints.json - run fingerprint.py first")
    entries = [v for v in json.load(open(FP_CACHE)).values()
               if v.get("fingerprint") and v.get("duration")]
    analysis = {v["path"]: v for v in
                (json.load(open(ANALYSIS)).values() if os.path.exists(ANALYSIS) else [])
                if v.get("path")}

    decoded = {}
    for e in entries:
        try:
            raw, _ = chromaprint.decode_fingerprint(e["fingerprint"].encode())
            decoded[e["path"]] = raw
        except Exception:
            continue

    # Bucket by duration so we compare ~tens of candidates instead of all N.
    # Each file is placed in its own bucket and the neighbours either side, so
    # a pair straddling a bucket edge is still compared.
    buckets = defaultdict(list)
    for e in entries:
        if e["path"] not in decoded:
            continue
        b = int(e["duration"] // DURATION_TOLERANCE)
        for k in (b - 1, b, b + 1):
            buckets[k].append(e)

    # Union-find over "these two are the same recording".
    parent = {e["path"]: e["path"] for e in entries}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    compared = set()
    pairs = 0
    for members in buckets.values():
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                if abs(a["duration"] - b["duration"]) > DURATION_TOLERANCE:
                    continue
                key = tuple(sorted((a["path"], b["path"])))
                if key in compared:
                    continue
                compared.add(key)
                pairs += 1
                ber = bit_error_rate(decoded[a["path"]], decoded[b["path"]])
                if ber <= args.threshold:
                    union(a["path"], b["path"])

    groups = defaultdict(list)
    by_path = {e["path"]: e for e in entries}
    for p in parent:
        if p in decoded:
            groups[find(p)].append(p)
    dupes = {k: v for k, v in groups.items() if len(v) > 1}

    report = []
    freed = 0
    for root, members in dupes.items():
        ranked = sorted(members, key=lambda p: quality_rank(by_path[p], analysis),
                        reverse=True)
        winner, losers = ranked[0], ranked[1:]
        for p in losers:
            try:
                freed += os.path.getsize(p)
            except OSError:
                pass
        report.append({
            "keep": winner,
            "keep_quality": analysis.get(winner, {}).get("quality_grade"),
            "keep_cutoff": analysis.get(winner, {}).get("spectral_cutoff_hz"),
            "drop": losers,
        })

    json.dump(report, open(OUT, "w"), ensure_ascii=False, indent=1)

    print(f"\n  {len(entries)} tracks, {pairs} pairs compared "
          f"(duration bucketing avoided ~{len(entries)**2//2 - pairs} comparisons)")
    print(f"  duplicate groups: {len(dupes)}   redundant files: "
          f"{sum(len(r['drop']) for r in report)}   would free {freed/1e6:.0f} MB\n")
    for r in report[:15]:
        print(f"    KEEP {os.path.basename(r['keep'])[:56]}"
              f"  ({r['keep_cutoff']}Hz)")
        for d in r["drop"]:
            c = analysis.get(d, {}).get("spectral_cutoff_hz")
            print(f"    drop {os.path.basename(d)[:56]}  ({c}Hz)")
        print()

    if args.quarantine and report:
        os.makedirs(args.quarantine, exist_ok=True)
        moved = 0
        for r in report:
            for d in r["drop"]:
                if not os.path.exists(d):
                    continue
                dst = os.path.join(args.quarantine, os.path.basename(d))
                n = 1
                while os.path.exists(dst):
                    stem, ext = os.path.splitext(os.path.basename(d))
                    dst = os.path.join(args.quarantine, f"{stem}.{n}{ext}")
                    n += 1
                shutil.move(d, dst)
                moved += 1
        print(f"  moved {moved} files to {args.quarantine} (nothing deleted)\n")
    print(f"  detail -> {OUT}\n")


if __name__ == "__main__":
    main()
