#!/usr/bin/env python3
"""Record what the pipeline currently says about the sampled tracks.

Take one before the change and one after, then hand both to snapdiff.py. This
is golden master testing: nothing here knows what "correct" means, it only
knows what the answer was last time, which is the only thing that can tell a
fix from a side effect.

Two levels, because the fixes come in two shapes:

  cache  every stage before write_tags, read straight out of cache/. Seconds,
         and it needs no output built.
  out    what actually landed in out/_all: filenames, tags, the .lrc sidecar,
         the artwork and playlist membership. This is the only level that can
         see a defect in the MP4 tag path or a playlist that lost a track.

Reads caches and output. Writes only baseline/<issue>/<label>.json.

Usage:
  snapshot.py --issue m4a-genres --label before
  snapshot.py --issue m4a-genres --label after --level out
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from tools.tagdump import sidecar_lrc, tags_of  # noqa: E402

# Caches keyed by the source path, which is the one identifier every stage
# agrees on. analysis.json is keyed by fingerprint instead and is re-indexed
# below, exactly as write_tags.py does it.
BY_PATH = ("identity.json", "enrich.json", "lyric_verify.json",
           "tagseed.json", "cascade.json")

# What counts as an output file. Everything Samsung Music will play, which is
# the floor this library is tagged against, plus the formats write_tags can
# produce from these sources.
AUDIO_EXT = {".mp3", ".m4a", ".mp4", ".aac", ".flac", ".ogg", ".oga",
             ".opus", ".wav", ".3gp", ".3ga"}


def load(cache, name):
    p = os.path.join(cache, name)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def scalar(v):
    """-> something two snapshots can be compared on and a human can read.

    Containers become sorted JSON so that a reordered list is not reported as
    a change, and long bodies become a hash: the question is only ever "did
    this change", and a diff that prints two 4000-character lyric sheets is a
    diff nobody reads.
    """
    if v is None or isinstance(v, (bool, int, float)):
        return v
    if isinstance(v, (list, tuple, dict)):
        v = json.dumps(v, sort_keys=True, ensure_ascii=False)
    v = str(v)
    if len(v) > 120:
        import hashlib
        return (f"sha256:{hashlib.sha256(v.encode('utf-8')).hexdigest()[:16]}"
                f" chars={len(v)}")
    return v


def flatten(prefix, record, into):
    if not isinstance(record, dict):
        into[prefix] = scalar(record)
        return
    for k, v in record.items():
        into[f"{prefix}.{k}"] = scalar(v)


def cache_snapshot(tracks, cache):
    """-> {source path: {field: value}} for every stage before write_tags."""
    analysis = load(cache, "analysis.json") or {}
    by_path = {}
    for v in analysis.values():
        if v.get("path"):
            by_path[v["path"]] = v
    review = {r["path"]: r for r in (load(cache, "review.json") or [])
              if r.get("path")}
    others = {n: (load(cache, n) or {}) for n in BY_PATH}
    lyrics = load(cache, "lyrics.json") or {}
    canon = (load(cache, "artist_canon.json") or {}).get("mapping") or {}
    lastfm = (load(cache, "lastfm_tags.json") or {}).get("artist") or {}

    # Duplicate membership is a property of a group, not of a file, so it has
    # to be turned inside out before it can be diffed per track. This is the
    # field that shows a dedupe change dropping a copy that used to survive.
    dupe_role, dupe_of = {}, {}
    for g in load(cache, "duplicates.json") or []:
        dupe_role[g["keep"]] = "keep"
        for p in g.get("drop") or []:
            dupe_role[p] = "drop"
            dupe_of[p] = g["keep"]
    name_role, name_of = {}, {}
    nd = load(cache, "name_duplicates.json") or {}
    for p, v in (nd.get("losers") or {}).items():
        name_role[p] = "loser"
        name_of[p] = (v or {}).get("duplicate_of")

    try:
        from pipeline import artist_names
    except Exception:
        artist_names = None

    out = {}
    for t in tracks:
        p = t["path"]
        rec = {}
        flatten("analysis", by_path.get(p) or {}, rec)
        row = review.get(p) or {}
        flatten("review", row, rec)
        for name, table in others.items():
            flatten(name.replace(".json", ""), table.get(p) or {}, rec)

        # Which lyric sheet this track gets is decided by its identity, so a
        # change of artist or title silently changes the lyrics too. Look it
        # up the way write_tags.py does, and record the key alongside the
        # body: a changed key with an unchanged body means something else.
        artist = row.get("proposed_artist") or ""
        title = row.get("proposed_title") or ""
        key = f"{artist}|{title}".lower()
        entry = lyrics.get(key)
        if isinstance(entry, str):
            entry = {"plain": entry}
        entry = entry or {}
        rec["lyrics.key"] = key
        rec["lyrics.plain"] = scalar(entry.get("plain"))
        rec["lyrics.synced"] = scalar(entry.get("synced"))
        rec["lyrics.matched"] = scalar(entry.get("matched"))

        rec["duplicates.role"] = dupe_role.get(p)
        rec["duplicates.keeper"] = scalar(dupe_of.get(p))
        rec["name_duplicates.role"] = name_role.get(p)
        rec["name_duplicates.duplicate_of"] = scalar(name_of.get(p))

        if artist:
            rec["artist_canon.canonical"] = (
                artist_names.canonical(artist, canon) if artist_names
                else canon.get(artist.lower(), artist))
            tags = lastfm.get(artist.lower()) or lastfm.get(artist) or []
            rec["lastfm_tags.top"] = scalar(
                [x.get("name") for x in tags if isinstance(x, dict)][:5])
        out[p] = rec
    return out


def index_output(out_dir):
    """-> ({source basename: output path}, ambiguous, unstamped).

    Output files carry MUZZI_SOURCE_FILE, the basename of the file they were
    written from, and that is the only link back. Two source folders can hold
    the same basename (one pair does here), and keeping whichever was walked
    last would silently snapshot a different song, so ambiguous names are
    dropped and reported instead. A blank field beats a wrong one.
    """
    index, ambiguous, unstamped = {}, set(), []
    for dp, dirs, names in os.walk(out_dir):
        # Pruned from the walk rather than skipped per file: a playlist read
        # as audio yields no MUZZI_SOURCE_FILE and would be reported as an
        # unstamped output file, which is a real finding when it is true.
        dirs[:] = [d for d in dirs if d != "playlists"]
        for n in sorted(names):
            if os.path.splitext(n)[1].lower() not in AUDIO_EXT:
                continue
            p = os.path.join(dp, n)
            src = tags_of(p, hash_long=False).get("MUZZI_SOURCE_FILE")
            if not src:
                unstamped.append(p)
                continue
            if src in index and index[src] != p:
                ambiguous.add(src)
            index[src] = p
    for src in ambiguous:
        index.pop(src, None)
    return index, sorted(ambiguous), sorted(unstamped)


def playlist_dir(out_dir):
    """-> where export.py put the M3Us, or None.

    write_tags writes out/_all and export writes out/playlists, so the
    playlists are a sibling of the audio, not a child of it. Looking only
    inside out_dir finds nothing and reports every track as belonging to no
    playlist, which is indistinguishable from a real regression.
    """
    for cand in (os.path.join(out_dir, "playlists"),
                 os.path.join(os.path.dirname(os.path.realpath(out_dir)),
                              "playlists")):
        if os.path.isdir(cand):
            return cand
    return None


def read_playlists(out_dir):
    """-> ({output path: [playlist, ...]}, {relpath: [playlist, ...]}).

    Relative M3U first, then the --absolute variant, kept apart on purpose:
    they are two carriers of the same fact and a change that updates one and
    not the other is exactly the sort of half-done edit this tool exists to
    catch.
    """
    rel, absolute = {}, {}
    pldir = playlist_dir(out_dir)
    if not pldir:
        return rel, absolute
    for name in sorted(os.listdir(pldir)):
        if not name.endswith(".m3u"):
            continue
        target = absolute if name.endswith(".abs.m3u") else rel
        stem = name[:-8] if name.endswith(".abs.m3u") else name[:-4]
        with open(os.path.join(pldir, name), encoding="utf-8",
                  errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Relative entries resolve against the playlist directory.
                # Absolute ones point at a phone that is not this machine, so
                # they are matched on their path below the output root, which
                # is the part that has to agree; the phone root above it is
                # deliberately not this filesystem.
                if target is rel:
                    key = os.path.realpath(os.path.join(pldir, line))
                else:
                    marker = "/_all/"
                    key = line.split(marker, 1)[1] if marker in line \
                        else os.path.basename(line)
                target.setdefault(key, []).append(stem)
    return rel, absolute


def out_snapshot(tracks, out_dir):
    """-> ({source path: {field: value}}, notes)."""
    index, ambiguous, unstamped = index_output(out_dir)
    rel_pl, abs_pl = read_playlists(out_dir)
    out, missing = {}, []
    for t in tracks:
        src = os.path.basename(t["path"])
        dst = index.get(src)
        if not dst:
            # Recorded as absent rather than skipped. A track that stopped
            # being written is the failure this whole tool is for, and a
            # skipped key looks identical to a clean run.
            missing.append(t["path"])
            out[t["path"]] = {}
            continue
        rec = {}
        rec["out.relpath"] = os.path.relpath(dst, out_dir)
        for k, v in tags_of(dst).items():
            rec[f"out.{k}"] = v
        rec["out._lrc"] = sidecar_lrc(dst)
        rec["out._playlists"] = json.dumps(
            sorted(rel_pl.get(os.path.realpath(dst), [])), ensure_ascii=False)
        rec["out._playlists_abs"] = json.dumps(
            sorted(abs_pl.get(rec["out.relpath"],
                              abs_pl.get(os.path.basename(dst), []))),
            ensure_ascii=False)
        out[t["path"]] = rec
    notes = {"ambiguous_source_names": ambiguous,
             "output_without_muzzi_stamp": unstamped,
             "sampled_tracks_absent_from_output": sorted(missing)}
    return out, notes


def shared_checkout():
    """-> the main checkout, which every worktree shares an out/ and cache/
    with, or None when git cannot say."""
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=HERE, capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return None
    return os.path.dirname(common) if common.endswith(".git") else None


def inside(path, root):
    if not root:
        return False
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


def main():
    ap = argparse.ArgumentParser(
        description="Snapshot the sampled tracks before and after a change.")
    ap.add_argument("--issue", required=True)
    ap.add_argument("--label", required=True,
                    help="before or after (any name works; snapdiff.py "
                         "defaults to these two)")
    ap.add_argument("--level", default="cache",
                    choices=("cache", "out", "both"),
                    help="cache: every stage before write_tags (default). "
                         "out: what landed in out/_all. both: all of it")
    ap.add_argument("--cache", default=os.path.join(HERE, "cache"))
    ap.add_argument("--baseline", default=os.path.join(HERE, "baseline"))
    ap.add_argument("--out-dir", default=os.path.join(HERE, "out", "_all"))
    ap.add_argument("--allow-shared-read", action="store_true",
                    help="permit reading the main checkout's out/_all, which "
                         "every other session is also using")
    ap.add_argument("--overwrite", action="store_true",
                    help="replace an existing snapshot for this label")
    args = ap.parse_args()

    # Re-recording "before" after the change has landed is how a baseline
    # stops being one: the diff then compares the change against itself and
    # comes back clean. sample.json is guarded the same way, with --redraw.
    out_path = os.path.join(args.baseline, args.issue, f"{args.label}.json")
    if os.path.exists(out_path) and not args.overwrite:
        sys.exit(f"{out_path} already exists. Pass --overwrite only if this "
                 f"snapshot was taken at the wrong moment; taking it again "
                 f"after the change is what makes a diff read clean.")

    sample_path = os.path.join(args.baseline, args.issue, "sample.json")
    if not os.path.exists(sample_path):
        sys.exit(f"no sample at {sample_path}: run "
                 f"tools/sample.py --issue {args.issue} first")
    with open(sample_path, encoding="utf-8") as fh:
        tracks = json.load(fh)["tracks"]

    doc = {"issue": args.issue, "label": args.label, "level": args.level,
           "tracks": {}, "notes": {}}

    if args.level in ("cache", "both"):
        rec = cache_snapshot(tracks, args.cache)
        for p, fields in rec.items():
            doc["tracks"].setdefault(p, {}).update(fields)

    if args.level in ("out", "both"):
        shared = shared_checkout()
        # Said out loud rather than failing open in silence: with no answer
        # from git the guard below cannot fire, and a guard that quietly is
        # not running is worse than none.
        if shared is None:
            print("  note: git could not name the main checkout, so the "
                  "shared-output guard is not active")
        # Worktrees live under .claude/worktrees/ inside the main checkout, so
        # "inside the shared checkout" is true of your own tree as well. What
        # matters is being inside the shared checkout and outside your own.
        if inside(args.out_dir, shared) and not inside(args.out_dir, HERE):
            if not args.allow_shared_read:
                sys.exit(
                    f"{args.out_dir} is the shared library in {shared}, which "
                    f"every other session is also using. Build your own with "
                    f"write_tags.py --only, or pass --allow-shared-read if "
                    f"you really mean to read the shared one.")
            print(f"  reading the SHARED output at {args.out_dir}")
        if not os.path.isdir(args.out_dir):
            sys.exit(f"no output at {args.out_dir}: build one first with "
                     f"pipeline/write_tags.py --only <list> --out {args.out_dir}")
        doc["out_dir"] = os.path.realpath(args.out_dir)
        rec, notes = out_snapshot(tracks, args.out_dir)
        for p, fields in rec.items():
            doc["tracks"].setdefault(p, {}).update(fields)
        doc["notes"] = notes
        for k, v in notes.items():
            if v:
                print(f"  {k}: {len(v)}")
                for item in v[:5]:
                    print(f"    {item}")
                if len(v) > 5:
                    print(f"    ... and {len(v) - 5} more")

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
    fields = sum(len(v) for v in doc["tracks"].values())
    print(f"\n  {len(doc['tracks'])} tracks, {fields} fields -> {out_path}")


if __name__ == "__main__":
    main()
