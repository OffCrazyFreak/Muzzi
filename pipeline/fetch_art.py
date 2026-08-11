#!/usr/bin/env python3
"""Download the cover art the cascade found, once per album.

cascade.py resolved an artwork URL for 94% of the library, but a URL in a JSON
file is not a picture in a file. This fetches them into cache/art/, keyed by
the URL so an album shared by twelve tracks is downloaded once.

Existing art from enrich.py is left alone -- this only fills the gaps.

Usage:
  fetch_art.py
  fetch_art.py --limit 50
  fetch_art.py --force
"""
import argparse
import hashlib
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

CASCADE = os.path.join(HERE, "cache", "cascade.json")
ART_DIR = os.path.join(HERE, "cache", "art")
INDEX = os.path.join(HERE, "cache", "art_index.json")

# Anything smaller is a placeholder or an error page, not a cover.
MIN_BYTES = 3000


def art_path_for(url):
    return os.path.join(ART_DIR, hashlib.sha1(url.encode()).hexdigest()[:20] + ".jpg")


def fetch(url, session):
    dst = art_path_for(url)
    if os.path.exists(dst) and os.path.getsize(dst) >= MIN_BYTES:
        return dst, "cached"
    try:
        r = session.get(url, timeout=30)
    except Exception as e:
        return None, str(e)[:60]
    if r.status_code != 200 or len(r.content) < MIN_BYTES:
        return None, f"http {r.status_code}, {len(r.content)}B"
    tmp = dst + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(r.content)
    os.replace(tmp, dst)
    return dst, "fetched"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    os.makedirs(ART_DIR, exist_ok=True)
    cas = json.load(open(CASCADE))
    index = {} if args.force else (json.load(open(INDEX)) if os.path.exists(INDEX) else {})

    # One download per distinct URL, not per track.
    urls = {}
    for path, rec in cas.items():
        u = (rec.get("facts") or {}).get("cover_url")
        if u:
            urls.setdefault(u, []).append(path)
    todo = [u for u in urls if u not in index]
    if args.limit:
        todo = todo[: args.limit]
    if not todo:
        print(f"  no new artwork to fetch ({len(index)} known)\n")
        return 0

    workers = args.workers or min(12, (os.cpu_count() or 4) * 2)
    print(f"  {len(todo)} distinct covers for {len(cas)} tracks, {workers} workers\n")
    session = requests.Session()
    lock, n, ok, t0 = threading.Lock(), [0], [0], time.time()

    def work(u):
        return u, fetch(u, session)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for f in as_completed([ex.submit(work, u) for u in todo]):
            try:
                u, (dst, how) = f.result()
            except Exception:
                continue
            with lock:
                n[0] += 1
                if dst:
                    ok[0] += 1
                    index[u] = dst
                if n[0] % 100 == 0:
                    print(f"    {n[0]}/{len(todo)}  {ok[0]} ok", flush=True)
                    json.dump(index, open(INDEX + ".tmp", "w"), indent=1)
                    os.replace(INDEX + ".tmp", INDEX)

    json.dump(index, open(INDEX + ".tmp", "w"), indent=1)
    os.replace(INDEX + ".tmp", INDEX)
    covered = sum(len(urls[u]) for u in index if u in urls)
    print(f"\n  {ok[0]}/{n[0]} covers downloaded in {time.time()-t0:.0f}s")
    print(f"  covers {len(index)} albums -> {covered} tracks\n  -> {INDEX}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
