#!/usr/bin/env python3
"""Draw and freeze the sample a fix gets verified against.

One track is not evidence. A fix verified against the track that prompted it
tells you nothing about the other 1700, and the two defects that keep reaching
this library are exactly the ones a single track cannot show: a fix that
silently did nothing to most of what it was meant to fix, and a fix that also
changed things it was never meant to touch.

So: draw 10% of the library, freeze it, and diff it before and after. The draw
is a hash of the track and the issue slug rather than a random number, which
means every rerun during the same task gets the identical set (a redraw
mid-task would make the before and after incomparable) while a different issue
gets a different set (a set frozen forever is a set fixes start to overfit).

Reads the caches. Writes only baseline/<issue>/sample.json.

Usage:
  sample.py --issue m4a-genres
  sample.py --issue m4a-genres --add "artist:Rasta" --add "folder:YouTube"
"""
import argparse
import fnmatch
import hashlib
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

SELECTORS = ("artist", "title", "album", "genre", "folder", "path", "file",
             "fp")


def load(cache, name):
    """-> a cache file, or an empty container. A cache that has never been
    built is normal; a cache read through a bare relative path is not, which
    is why every path here is built from --cache."""
    p = os.path.join(cache, name)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def population(cache):
    """-> [track, ...] every track the pipeline knows about.

    review.json is the list write_tags actually consumes, so it is the
    population that matters. analysis.json fills in tracks that never reached
    review, and supplies the fingerprint.
    """
    rows = load(cache, "review.json") or []
    analysis = load(cache, "analysis.json") or {}

    # A path can appear under more than one fingerprint (two of them here:
    # the same file analysed twice under slightly different decodes). Take
    # the lowest, deterministically, rather than whichever came last.
    fp_by_path = {}
    for fp, v in analysis.items():
        p = v.get("path")
        if p and (p not in fp_by_path or fp < fp_by_path[p]):
            fp_by_path[p] = fp

    enrich = load(cache, "enrich.json") or {}
    tracks, seen = [], set()
    for r in rows:
        p = r.get("path")
        if not p or p in seen:
            continue
        seen.add(p)
        tracks.append({
            "path": p,
            "file": r.get("file") or os.path.basename(p),
            "fp": fp_by_path.get(p),
            "artist": r.get("proposed_artist") or "",
            "title": r.get("proposed_title") or "",
            "album": r.get("proposed_album") or "",
            "genres": (enrich.get(p) or {}).get("genres") or [],
        })
    for fp, v in analysis.items():
        p = v.get("path")
        if not p or p in seen:
            continue
        seen.add(p)
        tracks.append({"path": p, "file": os.path.basename(p),
                       "fp": fp_by_path.get(p), "artist": "", "title": "",
                       "album": "", "genres": []})
    tracks.sort(key=lambda t: t["path"])
    return tracks


def drawn(track, slug, pct):
    """-> True when this track is in the issue's deterministic draw.

    blake2s over the fingerprint (the identity that survives a rename) salted
    with the issue slug. Per mille rather than per cent so --pct takes a
    fraction without the comparison silently rounding it to zero.
    """
    key = f'{track["fp"] or track["path"]}:{slug}'
    h = hashlib.blake2s(key.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(h, "big") % 1000 < pct * 10


def matches(track, kind, value):
    v = value.lower()
    if kind == "fp":
        return track["fp"] == value
    if kind == "path":
        return fnmatch.fnmatch(track["path"].lower(), v)
    if kind == "file":
        return fnmatch.fnmatch(track["file"].lower(), v)
    if kind == "folder":
        return v in os.path.dirname(track["path"]).lower()
    if kind == "genre":
        return any(v in g.lower() for g in track["genres"])
    return v in (track.get(kind) or "").lower()


def coverage(tracks, cache):
    """Print what the sample covers before any work starts.

    "Hit every surface" is unenforceable if nobody can see which surfaces the
    sample contains. m4a is a quarter of this library and the MP4 tag path has
    silently written fewer tags than the MP3 one before, so a sample that
    happens to be all MP3 has to be visible as such.
    """
    ext = Counter(os.path.splitext(t["path"])[1].lower() for t in tracks)
    folders = Counter(os.path.basename(os.path.dirname(t["path"]))
                      for t in tracks)

    lyrics = load(cache, "lyrics.json") or {}
    enrich = load(cache, "enrich.json") or {}
    analysis = load(cache, "analysis.json") or {}
    by_path = {v["path"]: v for v in analysis.values() if v.get("path")}
    losers = set()
    for g in load(cache, "duplicates.json") or []:
        losers.update(g.get("drop") or [])
    losers.update((load(cache, "name_duplicates.json") or {}).get("losers")
                  or {})

    def lyric_entry(t):
        e = lyrics.get(f'{t["artist"]}|{t["title"]}'.lower())
        return {"plain": e} if isinstance(e, str) else (e or {})

    def count(pred):
        return sum(1 for t in tracks if pred(t))

    print(f"  {len(tracks)} tracks")
    print("    containers    " + ", ".join(
        f"{k or '(none)'} {v}" for k, v in ext.most_common()))
    print("    folders       " + ", ".join(
        f"{k} {v}" for k, v in folders.most_common(6)))
    print(f"    identified    {count(lambda t: t['artist'])}")
    # Both lyric carriers come from this one cache entry: the plain body is
    # embedded, the synced body is what becomes the .lrc sidecar. A sample
    # with no synced lyrics cannot show an .lrc regression.
    print(f"    any lyrics    {count(lambda t: lyric_entry(t).get('plain') or lyric_entry(t).get('synced'))}")
    print(f"    synced (.lrc) {count(lambda t: lyric_entry(t).get('synced'))}")
    print(f"    with art      "
          f"{count(lambda t: (enrich.get(t['path']) or {}).get('art_path'))}")
    print(f"    with genres   {count(lambda t: t['genres'])}")
    print(f"    with a BPM    "
          f"{count(lambda t: (by_path.get(t['path']) or {}).get('bpm'))}")
    print(f"    dedupe losers {count(lambda t: t['path'] in losers)}"
          "  (never written to out/_all)")


def main():
    ap = argparse.ArgumentParser(
        description="Freeze the sample an issue is verified against.")
    ap.add_argument("--issue", required=True,
                    help="issue slug; also the seed, so a different issue "
                         "gets a different draw")
    ap.add_argument("--pct", type=float, default=10.0,
                    help="percent of the library to draw (default 10)")
    ap.add_argument("--add", action="append", default=[], metavar="SELECTOR",
                    help="add tracks the issue is specifically about: "
                         + ", ".join(f"{s}:" for s in SELECTORS)
                         + ". path/file take globs, the rest are substrings")
    ap.add_argument("--cache", default=os.path.join(HERE, "cache"),
                    help="cache directory to read (never written)")
    ap.add_argument("--baseline", default=os.path.join(HERE, "baseline"))
    ap.add_argument("--redraw", action="store_true",
                    help="overwrite an existing sample. Redrawing after "
                         "seeing results is how this tool starts lying")
    args = ap.parse_args()

    out_dir = os.path.join(args.baseline, args.issue)
    out_path = os.path.join(out_dir, "sample.json")
    if os.path.exists(out_path) and not args.redraw:
        sys.exit(f"{out_path} already exists. The sample is frozen for the "
                 f"life of the issue; pass --redraw only if you have not yet "
                 f"seen any results from it.")

    tracks = population(args.cache)
    if not tracks:
        sys.exit(f"no tracks found in {args.cache}: run the pipeline first, "
                 f"or point --cache at a built cache directory")

    chosen, why = [], {}
    for t in tracks:
        if drawn(t, args.issue, args.pct):
            chosen.append(t)
            why[t["path"]] = "hash"

    for sel in args.add:
        if ":" not in sel:
            sys.exit(f"--add {sel!r} is not a selector; expected one of "
                     + ", ".join(f"{s}:VALUE" for s in SELECTORS))
        kind, _, value = sel.partition(":")
        kind = kind.strip().lower()
        if kind not in SELECTORS:
            sys.exit(f"unknown selector {kind!r}; expected one of "
                     + ", ".join(SELECTORS))
        hits = [t for t in tracks if matches(t, kind, value)]
        # An --add that matches nothing is the quiet way to verify a fix
        # against a sample that does not contain the thing being fixed.
        if not hits:
            sys.exit(f"--add {sel!r} matched no track")
        added = 0
        for t in hits:
            if t["path"] not in why:
                chosen.append(t)
                why[t["path"]] = sel
                added += 1
        print(f"  {sel}: {len(hits)} tracks ({added} not already in the draw)")

    # An empty sample is the worst outcome this tool has: it snapshots
    # nothing, diffs clean, and exits 0, which reads as proof that a change
    # is safe. Fail here, where the cause is still visible.
    if not chosen:
        sys.exit(f"--pct {args.pct} drew no track of {len(tracks)} and no "
                 f"--add named one. An empty sample verifies nothing and "
                 f"every check downstream of it would pass.")

    chosen.sort(key=lambda t: t["path"])
    for t in chosen:
        t["via"] = "hash" if why[t["path"]] == "hash" else "explicit"
        t["selector"] = None if t["via"] == "hash" else why[t["path"]]

    os.makedirs(out_dir, exist_ok=True)
    doc = {"issue": args.issue, "pct": args.pct, "add": args.add,
           "population": len(tracks), "tracks": chosen}
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False, sort_keys=True)
        fh.write("\n")

    # The same set as a plain list, because write_tags.py --only takes one
    # source path per line and nobody should have to convert this by hand.
    paths_path = os.path.join(out_dir, "paths.txt")
    with open(paths_path, "w", encoding="utf-8") as fh:
        for t in chosen:
            fh.write(t["path"] + "\n")

    print(f"\n  sample for {args.issue}: {len(chosen)} of {len(tracks)} "
          f"tracks ({100.0 * len(chosen) / len(tracks):.1f}%)")
    coverage(chosen, args.cache)
    print(f"\n  wrote {out_path} and {paths_path}")
    print(f"  next: write the tracks you expect to change to "
          f"{os.path.join(out_dir, 'targets.txt')}, before making the change")


if __name__ == "__main__":
    main()
