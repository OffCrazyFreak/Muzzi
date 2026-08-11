#!/usr/bin/env python3
"""Stage 2: fingerprint every file with Chromaprint. Optionally look up AcoustID.

Fingerprinting is purely local (fpcalc) and needs no network or API key.
Results cache to cache/fingerprints.json keyed by (path, size, mtime), so
re-runs are free. Fingerprints never change for a given file, which is what
makes the whole pipeline resumable.

AcoustID lookups are OPT-IN and require your own application API key
(https://acoustid.org/new-application - free). Per AcoustID's docs, keys are
per-application: do not borrow another project's key. Without a key this
script still fingerprints everything, and beets' chroma plugin performs the
lookups during import using its own registered key.

Usage:
  fingerprint.py <music_dir> [--limit N] [--acoustid-key KEY]
"""
import argparse
import json
import os
import re
import sys
import threading
import time
import unicodedata

import acoustid
from concurrent.futures import ThreadPoolExecutor, as_completed

# AcoustID documents a hard limit of 3 requests/second. Stay comfortably under.
RATE_LIMIT = 2.5

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(HERE, "cache", "fingerprints.json")

BCMS_CHARS = set("čćšžđČĆŠŽĐ")
CYRILLIC = re.compile(r"[Ѐ-ӿ]")
BCMS_WORDS = re.compile(
    r"\b(ne|je|se|sam|si|te|mi|ti|nas|vas|sve|samo|kad|ako|kazes|zena|zene|"
    r"kafana|nema|vise|jos|jedan|noc|noci|srce|ljubav|zivot|dusa|oci|bez|za|"
    r"sa|na|od|do|ili|ali|jer|tekst|uzivo|spot|prevod|domaci|balkan|moje|"
    r"tvoj|nocas|ujutru|ljeto|leto|nista|sta|gde|kako|volim|dodji|hocu)\b",
    re.I,
)


def fold(s):
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


def likely_balkan(text):
    """Heuristic. Filenames are often ASCII-folded, so diacritics alone miss most."""
    if CYRILLIC.search(text) or any(c in BCMS_CHARS for c in text):
        return True
    return bool(BCMS_WORDS.search(fold(text)))


def load_cache():
    if os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8") as fh:
            return json.load(fh)
    return {}


def save_cache(cache):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    tmp = CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, CACHE)  # atomic: a crash mid-write cannot corrupt the cache


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="+")
    ap.add_argument("--limit", type=int)
    ap.add_argument(
        "--acoustid-key",
        help="Your own AcoustID application key. Omit to fingerprint only.",
    )
    # fpcalc runs as a subprocess, so threads (not processes) are the right
    # tool: the GIL is released while we wait on it, and there is no pickling
    # cost. Scales with the host rather than being hardcoded.
    ap.add_argument("-j", "--workers", type=int,
                    default=max(2, (os.cpu_count() or 4) - 2))
    args = ap.parse_args()

    files = []
    for root in args.root:
     for dirpath, _, names in os.walk(root):
        for n in sorted(names):
            if n.lower().endswith((".mp3", ".m4a", ".flac", ".opus", ".ogg", ".wav")):
                files.append(os.path.join(dirpath, n))
    if args.limit:
        files = files[: args.limit]

    cache = load_cache()

    # Cache key includes size and mtime, so an edited file is re-fingerprinted
    # while an untouched one is skipped no matter how often this runs.
    todo_keyed = []
    for path in files:
        st = os.stat(path)
        key = f"{path}|{st.st_size}|{int(st.st_mtime)}"
        if key not in cache:
            todo_keyed.append((path, key))

    print(f"  {len(files) - len(todo_keyed)} cached, {len(todo_keyed)} to "
          f"fingerprint, {args.workers} workers")
    if not todo_keyed:
        print("  nothing to do\n")
        return

    last_call = 0.0
    done = 0
    lock = threading.Lock()

    def fingerprint_one(path):
        entry = {"path": path, "name": os.path.basename(path)}
        try:
            dur, fp = acoustid.fingerprint_file(path)
            entry["duration"] = round(dur, 1)
            entry["fingerprint"] = fp.decode() if isinstance(fp, bytes) else fp
        except acoustid.NoBackendError:
            raise
        except Exception as e:
            entry["error"] = str(e)[:200]
        entry["likely_balkan"] = likely_balkan(entry["name"])
        return entry

    def lookup_one(entry):
        """AcoustID lookup, serialised by the rate limiter (2.5 req/s)."""
        nonlocal last_call
        with lock:
            wait = (1.0 / RATE_LIMIT) - (time.time() - last_call)
            if wait > 0:
                time.sleep(wait)
            last_call = time.time()
        results = list(acoustid.match(args.acoustid_key, entry["path"], parse=True))
        if results:
            score, rid, title, artist = results[0]
            entry["match"] = {"score": round(score, 3), "recording_id": rid,
                              "title": title, "artist": artist}
        else:
            entry["match"] = None

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fingerprint_one, p): (p, k) for p, k in todo_keyed}
        for i, fut in enumerate(as_completed(futs), 1):
            path, key = futs[fut]
            try:
                entry = fut.result()
            except acoustid.NoBackendError:
                sys.exit("fpcalc not found on PATH")
            if args.acoustid_key and entry.get("fingerprint"):
                try:
                    lookup_one(entry)
                except Exception as e:
                    entry["error"] = f"lookup: {str(e)[:150]}"
            cache[key] = entry
            done += 1
            if done % 25 == 0:
                save_cache(cache)
                el = time.time() - t0
                print(f"  {i}/{len(todo_keyed)}  {el:.0f}s  {el/i:.2f}s/track",
                      flush=True)

    save_cache(cache)

    # ---- report ----
    rows = [v for v in cache.values()
            if any(v["path"].startswith(r) for r in args.root)]
    matched = [r for r in rows if r.get("match")]
    errs = [r for r in rows if r.get("error")]
    bal = [r for r in rows if r.get("likely_balkan")]
    west = [r for r in rows if not r.get("likely_balkan")]

    def rate(subset):
        if not subset:
            return "n/a"
        m = sum(1 for r in subset if r.get("match"))
        return f"{m}/{len(subset)} ({100*m/len(subset):.0f}%)"

    fp_ok = sum(1 for r in rows if r.get("fingerprint"))
    print("\n  FINGERPRINTING")
    print(f"    total files        {len(rows)}")
    print(f"    fingerprinted      {fp_ok}")
    print(f"    errors             {len(errs)}")
    print(f"    likely Balkan      {len(bal)}  /  other {len(west)}")

    if not args.acoustid_key:
        print("\n  AcoustID lookups skipped (no --acoustid-key).")
        print("  beets' chroma plugin will do them during import.\n")
        print(f"  cache -> {CACHE}\n")
        return

    print("\n  ACOUSTID MATCH RATE")
    print(f"    overall            {rate(rows)}")
    print(f"    likely Balkan      {rate(bal)}")
    print(f"    everything else    {rate(west)}")
    if matched:
        hi = sum(1 for r in matched if r["match"]["score"] >= 0.9)
        print(f"\n    high-confidence matches (score >= 0.90): "
              f"{hi}/{len(matched)} ({100*hi/len(matched):.0f}%)")
    print(f"\n  cache -> {CACHE}\n")


if __name__ == "__main__":
    main()
