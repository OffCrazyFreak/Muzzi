#!/usr/bin/env python3
"""Re-fetch the files whose audio is worse than their container claims.

628 tracks measure below 15.5 kHz of real bandwidth, which is a 128 kbps source
however the header is labelled. Most are YouTube grabs taken before better
sources existed, or transcodes of transcodes.

Nothing is replaced. Candidates are downloaded into `redownloaded/`, measured,
and kept only when they clear every one of these:

  * the spectral cutoff is at least MIN_GAIN Hz higher than the original
  * the duration is within tolerance of the original, so a radio edit or a
    live version cannot quietly take a studio track's place
  * the file decodes at all

A candidate that fails any check is deleted and the original stands. The point
is to end up with strictly better files or nothing, never with a library that
is different in ways nobody asked for.

Sources, in order of how much they can be trusted to be the right recording:

  1. the link recorded against this exact file, from the `link` column of
     hints.tsv or a URL typed as a hint. An answer about this file, not an
     inference about a name
  2. the YouTube Music video id the cascade already resolved for this track
     (an Art Track: the distributor's own audio, no video encode)
  3. a hint whose title matches this track's, which is a guess
  4. a fresh YouTube Music search for artist + title

Downloads run in batches so a long run can be watched, stopped and resumed.

`--requested` answers a third question: the files you asked for by writing
"redownload" in a review sheet. No bandwidth bar applies, because you listened
to it and that is a better measurement of "this sounds wrong" than a spectral
cutoff. Measured here, 28 files were asked for and several of them sit well
above the bar, one at 18.6 kHz, so nothing selecting on bandwidth was ever
going to fetch them.

The length check does not apply to a link you gave for that exact file, and
the reason is the point of giving it: the copy on disk is the one that is
wrong. These downloads are YouTube rips carrying label intros, and the clean
release you linked is shorter by exactly that intro. Measured, 13 of 28
requested refetches were refused on drifts of 10 to 40 seconds, which is the
size of an intro and not of a different song. The check stays everywhere else,
because everywhere else the video came from a search and the length is the
only thing between "improve quality" and "change the song". A candidate that
does not decode is still deleted, whoever named it.

`--missing` answers a different question with the same machinery: a source
file that has been deleted keeps erroring in write_tags on every run, and
those errors self-disable --prune, so stale output can never be cleaned
automatically. In that mode the rows selected are the ones whose source is
gone, the file is restored to the path it was at, and the bandwidth bar does
not apply, because there is nothing left to be better than. The duration check
still does: analysis.json recorded the length before the file went, so a
different recording cannot take its place.

Usage:
  redownload.py --dry-run            # show what would be fetched
  redownload.py                      # fetch everything below the bar
  redownload.py --batch 50 --limit 100
  redownload.py --min-cutoff 15500   # which files count as needing it
  redownload.py --missing --dry-run  # the sources that are gone
  redownload.py --requested          # the ones you asked for by name
  redownload.py --requested --retry --cookies /tmp/yt.txt
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from pipeline.hints_resolve import video_id  # noqa: E402
from pipeline.webmatch import fit, version_mismatch  # noqa: E402

REVIEW = os.path.join(HERE, "cache", "review.json")
ANALYSIS = os.path.join(HERE, "cache", "analysis.json")
CASCADE = os.path.join(HERE, "cache", "cascade.json")
YT_HINTS = os.path.join(HERE, "cache", "hint_youtube.json")
OUT_DIR = os.path.join(HERE, "redownloaded")
STAGE_DIR = os.path.join(HERE, "cache", "redl_staging")
REPORT = os.path.join(HERE, "cache", "redownload.json")

# A replacement has to be meaningfully better, not a rounding difference.
MIN_GAIN_HZ = 1200
# Seconds the new file may differ from the old before it is a different cut.
MAX_DRIFT = 8.0
MIN_BYTES = 200_000

# The one source that is your answer rather than an inference, and the only
# one exempt from the length check. Named so the exemption is greppable and
# cannot drift apart from the string find_source returns.
YOURS = "the link you gave"


def _done_key(path, args):
    """Where an attempt is recorded in the shared report.

    The two modes ask different questions about the same path, so they cannot
    share a completion key: a file this tool once judged for its bandwidth is
    exactly the file that may later go missing, and it would then be skipped
    forever as "already attempted".
    """
    if getattr(args, "requested", False):
        return f"requested:{path}"
    return f"missing:{path}" if getattr(args, "missing", False) else path


def _ytmusic():
    from ytmusicapi import YTMusic
    if not hasattr(_ytmusic, "c"):
        _ytmusic.c = YTMusic()
    return _ytmusic.c


_YT_LOCK = threading.Lock()


_CLAIMED = {}
_CLAIM_LOCK = threading.Lock()


def claim(vid, path):
    """-> True if this file may use this video.

    One video is one recording, so two different songs resolving to the same
    id means at least one of them is wrong. "B-Mike - Baby Don't Cut" and
    "Courtney Parker - Her Last Words" both matched -OF1mkxxb6c: the second
    search found the first song. Both files were then replaced by the same
    audio, which made them byte-identical, which chained two unrelated songs
    into one duplicate group and nearly dropped one of them from the library.
    """
    if not vid:
        return False
    with _CLAIM_LOCK:
        owner = _CLAIMED.get(vid)
        if owner and owner != path:
            return False
        _CLAIMED[vid] = path
        return True


def find_source(row, facts, hints, link=None):
    """-> (video_id, how) or (None, why).

    `link` is the URL recorded against this exact file, from the `link` column
    of hints.tsv or a URL typed as a hint. It wins outright: it is an answer
    about this file, while everything below is an inference about a name. The
    fallback that matches a hint by title equality is deliberately kept for
    files with no answer of their own, but it is a guess and this is not.
    """
    vid = video_id(link or "")
    if vid:
        return vid, YOURS
    vid = facts.get("video_id")
    if vid:
        return vid, "cascade art track"
    for rec in hints.values():
        if not isinstance(rec, dict) or rec.get("error"):
            continue
        if rec.get("video_id") and rec.get("title") and row.get("proposed_title"):
            if rec["title"].strip().lower() == row["proposed_title"].strip().lower():
                return rec["video_id"], "your link"
    artist, title = row.get("proposed_artist"), row.get("proposed_title")
    if not (artist and title):
        return None, "no name to search with"
    try:
        with _YT_LOCK:
            res = _ytmusic().search(f"{artist} {title}", filter="songs", limit=3)
    except Exception as e:
        return None, f"search failed: {str(e)[:40]}"
    if not res:
        return None, "nothing on YouTube Music"
    # Do not take res[0] on faith. A search for "Courtney Parker - Her Last
    # Words" returned B-Mike's "Baby Don't Cut" as its first hit, and because
    # nothing checked the result against what was asked for, two unrelated
    # songs were both replaced by that one recording.
    for x in res:
        names = ", ".join(a["name"] for a in x.get("artists") or [])
        if fit(artist, names) < 0.6 or fit(title, x.get("title")) < 0.6:
            continue
        if version_mismatch(title, x.get("title")):
            continue
        return x.get("videoId"), "youtube music search"
    return None, "no confident match on YouTube Music"


def download(vid, dest_stem, cookies=None):
    """-> path to the downloaded audio, or None."""
    tmpl = dest_stem + ".%(ext)s"
    # YouTube answers a share of anonymous requests with 403. A cookie jar
    # exported from a signed-in browser (tools/export_cookies.py) is what gets
    # those through; without one they are simply lost.
    #
    # Each download gets its own copy, because yt-dlp writes the jar back when
    # the server updates a cookie. Eight workers sharing one file rewrote it
    # under each other until it stopped being a cookie file at all, and every
    # download after that failed with "does not look like Netscape format".
    # mkstemp, not a name derived from the path: it is created 0600 and it is
    # unique, so a second process cannot land on the same jar and another
    # local account cannot read a browser session out of the staging
    # directory.
    jar = None
    if cookies:
        try:
            fd, jar = tempfile.mkstemp(dir=STAGE_DIR, suffix=".cookies.txt")
            with open(cookies, "rb") as fh, os.fdopen(fd, "wb") as out:
                shutil.copyfileobj(fh, out)
        except OSError:
            if jar and os.path.exists(jar):
                os.remove(jar)
            jar = None
    auth = ["--cookies", jar] if jar else []
    try:
        p = subprocess.run(
            # m4a on purpose, not "bestaudio". YouTube's best audio is Opus in
            # a .webm container, which Samsung Music cannot play at all
            # (mp3/m4a/ogg/flac only) and which Essentia cannot decode, so it
            # would be unmeasurable AND unplayable. The Opus stream is ~136k
            # against AAC's ~130k -- six kilobits is not worth a file that
            # does not open on the device this library exists for.
            # --embed-metadata so the file carries the video it came from. On
            # m4a that lands in the native comment atom, which is where
            # tagseed.py reads a video id back out of a source file -- so a
            # redownloaded track states its own provenance without depending on
            # this report still existing.
            ["yt-dlp", "--no-warnings", "--no-playlist", "--embed-metadata",
             "-f",
             "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio", "-o", tmpl,
             *auth,
             f"https://music.youtube.com/watch?v={vid}"],
            capture_output=True, text=True, timeout=300)
    except Exception as e:
        return None, str(e)[:60]
    finally:
        if jar and os.path.exists(jar):
            os.remove(jar)
    if p.returncode != 0:
        tail = (p.stderr or "").strip().splitlines()
        return None, tail[-1][:80] if tail else f"yt-dlp exit {p.returncode}"
    for ext in (".m4a", ".mp3", ".ogg", ".opus", ".webm"):
        if os.path.exists(dest_stem + ext):
            return dest_stem + ext, None
    return None, "yt-dlp produced no file"


_MEASURE_SRC = """
import json, sys
sys.path.insert(0, %r)
import essentia.standard as es
from pipeline.analyze import spectral_cutoff
try:
    a = es.MonoLoader(filename=sys.argv[1], sampleRate=44100)()
    d = len(a) / 44100.0
    c = spectral_cutoff(a) if len(a) >= 44100 else None
    print(json.dumps([c, d]))
except Exception:
    print(json.dumps([None, None]))
""" % HERE


def measure(path):
    """-> (cutoff_hz, duration_s) or (None, None).

    Run in a genuinely separate interpreter, not a thread and not a forked
    worker. Essentia segfaults in a ThreadPoolExecutor and corrupts the heap
    ("munmap_chunk(): invalid pointer") when inherited across a fork, so the
    only reliable isolation is a fresh process.
    """
    try:
        p = subprocess.run([sys.executable, "-c", _MEASURE_SRC, path],
                           capture_output=True, text=True, timeout=180)
        out = (p.stdout or "").strip().splitlines()
        return tuple(json.loads(out[-1])) if out else (None, None)
    except Exception:
        return None, None


def fetch_one(task):
    """Download stage only. Network-bound, safe in threads."""
    row, an, facts, hints, cookies, link = task
    src = row["path"]
    vid, how = find_source(row, facts, hints, link)
    if not vid:
        return {"path": src, "file": row["file"], "status": "no source",
                "why": how}
    if not claim(vid, src):
        return {"path": src, "file": row["file"], "status": "no source",
                "why": f"video {vid} already used for another song",
                "video_id": vid}
    base = os.path.splitext(os.path.basename(src))[0][:80]
    stem = os.path.join(STAGE_DIR, f"{abs(hash(src)) % 10**10}_{base}")
    got, err = download(vid, stem, cookies)
    if not got:
        return {"path": src, "file": row["file"], "status": "download failed",
                "why": err, "video_id": vid}
    if os.path.getsize(got) < MIN_BYTES:
        os.remove(got)
        return {"path": src, "file": row["file"], "status": "download failed",
                "why": "file too small", "video_id": vid}
    return {"path": src, "file": row["file"], "status": "downloaded",
            "video_id": vid, "how": how, "staged": got}


def judge(res, an, args):
    """Decide whether a downloaded candidate replaces anything. Deletes it if
    not, so a run leaves nothing behind but improvements."""
    got = res["staged"]
    old_cut = an.get("spectral_cutoff_hz") or 0
    old_dur = an.get("decoded_secs") or 0
    new_cut, new_dur = res.get("new_cutoff_raw"), res.get("new_duration")

    def drop(status, why):
        try:
            os.remove(got)
        except OSError:
            pass
        return {k: res[k] for k in ("file", "video_id")} | {
            "status": status, "why": why}

    if not new_cut:
        return drop("rejected", "candidate did not decode")
    # A link you gave for this exact file is not checked against the file's
    # length, and the reason is the whole point of giving it: the copy on disk
    # is the one that is wrong. These downloads are YouTube rips carrying label
    # intros, and the clean release you linked is shorter by exactly that
    # intro. Measured, 13 of 28 requested refetches were refused on drifts of
    # 10 to 40 seconds, which is the size of an intro, not of a different song.
    #
    # The check stays everywhere else, because everywhere else the video was
    # chosen by a search and the length is the only thing standing between
    # "improve quality" and "change the song". Here you already did that
    # checking by looking at it.
    if res.get("how") != YOURS and old_dur and new_dur \
            and abs(new_dur - old_dur) > args.max_drift:
        return drop("rejected",
                    f"different length ({new_dur:.0f}s vs {old_dur:.0f}s)")

    if args.missing:
        # Restoring a source that was deleted, not improving one that is still
        # there. There is nothing to be better than, so the bandwidth bar does
        # not apply; the length check above is what stops a different
        # recording taking the place of the one that is gone. Analysis still
        # holds that length because it was measured before the file went.
        src = res["path"]
        dst = os.path.splitext(src)[0] + os.path.splitext(got)[1]
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst):
            return drop("rejected", "a file is already back at that path")
        shutil.move(got, dst)
        # YouTube serves m4a, so a row that named a .mp3 gets its audio back
        # under a different extension. That is a real restoration of the
        # music and NOT a restoration of the path: review.json still points
        # at a file that does not exist, and write_tags still errors on it,
        # until fingerprint, analyze and review have re-read the folder.
        # Reported separately so the summary cannot be read as "done".
        same_path = dst == src
        return {"file": res["file"],
                "status": "restored" if same_path else "restored elsewhere",
                "video_id": res["video_id"], "how": res["how"],
                "new_cutoff": round(new_cut), "path": dst,
                "was": None if same_path else src}

    if new_cut < old_cut + args.min_gain:
        return drop("not better", f"{new_cut:.0f}Hz vs {old_cut:.0f}Hz")

    # Keep the source folder layout so the result can be fed straight back in.
    src = res["path"]
    rel = os.path.relpath(os.path.dirname(src), args.common_root) \
        if args.common_root else ""
    dst_dir = os.path.join(OUT_DIR, rel) if rel not in (".", "") else OUT_DIR
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, os.path.basename(src).rsplit(".", 1)[0]
                       + os.path.splitext(got)[1])
    shutil.move(got, dst)
    return {"file": res["file"], "status": "kept", "video_id": res["video_id"],
            "how": res["how"], "old_cutoff": old_cut,
            "new_cutoff": round(new_cut), "gain": round(new_cut - old_cut),
            "path": dst}


def common_root(paths):
    try:
        return os.path.commonpath([os.path.dirname(p) for p in paths])
    except ValueError:
        return ""


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-cutoff", type=float, default=15500,
                    help="files measuring below this need replacing")
    ap.add_argument("--min-gain", type=float, default=MIN_GAIN_HZ)
    ap.add_argument("--max-drift", type=float, default=MAX_DRIFT)
    ap.add_argument("--batch", type=int, default=50)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--cookies", help="Netscape cookie file for yt-dlp; "
                                     "tools/export_cookies.py writes one")
    ap.add_argument("--missing", action="store_true",
                    help="fetch the rows whose source file is gone, back to "
                         "the path it was at, instead of the rows that "
                         "measure badly")
    ap.add_argument("--retry", action="store_true",
                    help="ignore the record of what has already been "
                         "attempted, so a run blocked by 403 or by a rule "
                         "that has since changed can be repeated")
    ap.add_argument("--requested", action="store_true",
                    help="fetch the rows you asked for by writing "
                         "'redownload' in a review sheet, whatever they "
                         "measure")
    args = ap.parse_args()

    rows = {r["path"]: r for r in json.load(open(REVIEW))}
    analysis = {v["path"]: v for v in json.load(open(ANALYSIS)).values()
                if v.get("path")}
    cascade = json.load(open(CASCADE)) if os.path.exists(CASCADE) else {}
    hints = json.load(open(YT_HINTS)) if os.path.exists(YT_HINTS) else {}
    done = json.load(open(REPORT)) if os.path.exists(REPORT) else {}
    if args.retry:
        # Only this mode's keys. Clearing the whole record would re-attempt
        # 628 files nobody asked about, and the modes are keyed apart exactly
        # so they can be repeated independently.
        prefix = _done_key("", args)
        skipped = {k for k in done if k.startswith(prefix)} if prefix else set()
        print(f"  --retry: forgetting {len(skipped)} earlier attempts")
        done = {k: v for k, v in done.items() if k not in skipped}

    # Every answer you have given about a file, so a request can be honoured
    # and so the exact link you gave for it beats a search.
    from pipeline.review import load_hints, load_links, parse_hint
    links = dict(load_links())
    asked_for, all_hints = set(), load_hints()
    for name, hint in all_hints.items():
        kind, _payload = parse_hint(hint)
        if kind == "refetch":
            asked_for.add(name)
        elif kind == "url":
            links.setdefault(name, hint)

    todo = []
    if args.requested:
        # No bandwidth bar. You listened to it, which is a better measurement
        # of "this sounds wrong" than a spectral cutoff, and a file can be a
        # bad rip at any bitrate.
        for path, row in rows.items():
            if row.get("file") not in asked_for:
                continue
            if f"requested:{path}" in done:
                continue
            todo.append((path, row, analysis.get(path) or {}))
        todo.sort(key=lambda x: x[0])
    elif args.missing:
        # A deleted source keeps erroring in write_tags on every run, and
        # those errors self-disable --prune, so stale output can never be
        # cleaned automatically until the file is back.
        # Keyed apart from the quality mode. done is shared, so a path this
        # tool once looked at for its bandwidth would otherwise be skipped
        # here forever, and that is exactly a file that has since gone.
        no_length = 0
        for path, row in rows.items():
            if os.path.exists(path) or f"missing:{path}" in done:
                continue
            an = analysis.get(path) or {}
            if not (an.get("decoded_secs") or 0) > 0:
                # The recorded length is the ONLY thing standing between this
                # and a different recording taking the missing one's place.
                # Without it there is no check left, so do not download.
                no_length += 1
                continue
            todo.append((path, row, an))
        todo.sort(key=lambda x: x[0])
        if no_length:
            print(f"  {no_length} skipped: no measured length to check a "
                  f"replacement against")
    else:
        for path, an in analysis.items():
            cut = an.get("spectral_cutoff_hz") or 0
            if cut >= args.min_cutoff:
                continue
            row = rows.get(path)
            if not row or path in done:
                continue
            todo.append((path, row, an))
        todo.sort(key=lambda x: x[2].get("spectral_cutoff_hz") or 0)
    if args.limit:
        todo = todo[: args.limit]

    args.common_root = common_root(list(analysis))

    if not todo:
        left = ("nothing left that you asked for" if args.requested
                else "no source file is missing" if args.missing
                else f"nothing below {args.min_cutoff:.0f}Hz left")
        print(f"  {left} ({len(done)} done)\n")
        return 0

    what = ("files you asked to be fetched again" if args.requested
            else "source files are missing" if args.missing
            else f"files measure below {args.min_cutoff:.0f}Hz")
    print(f"\n  {len(todo)} {what} ({len(done)} already attempted)")
    if args.dry_run:
        for path, row, an in todo[:40]:
            mark = ("  gone  " if args.missing
                    else f"{an.get('spectral_cutoff_hz'):>6}Hz")
            print(f"    {mark}  "
                  f"{str(row.get('proposed_artist'))[:22]:22} | "
                  f"{str(row.get('proposed_title'))[:34]}")
        print("\n  dry run; nothing downloaded\n")
        return 0

    os.makedirs(STAGE_DIR, exist_ok=True)
    os.makedirs(OUT_DIR, exist_ok=True)
    # Network-bound download, CPU-bound measure. The measure is serialised, so
    # oversubscribing the cores only helps the downloads.
    workers = args.workers or min(8, (os.cpu_count() or 4))
    # Decoding a whole track costs real RAM, so the measuring pool is sized to
    # the cores rather than oversubscribed like the downloads.
    cpu_workers = max(1, min(workers, (os.cpu_count() or 4) - 1))
    print(f"  {workers} download workers, {cpu_workers} measuring, "
          f"batches of {args.batch}\n")

    lock = threading.Lock()
    stats = {"kept": 0, "restored": 0, "restored elsewhere": 0,
             "not better": 0, "rejected": 0, "download failed": 0,
             "no source": 0}
    t0 = time.time()

    for start in range(0, len(todo), args.batch):
        batch = todo[start:start + args.batch]
        bn = start // args.batch + 1
        total_b = (len(todo) + args.batch - 1) // args.batch
        print(f"  --- batch {bn}/{total_b} ({len(batch)} files)", flush=True)

        # Phase 1: download, in threads.
        fetched = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(fetch_one, (row,
                                          analysis.get(p, {}),
                                          (cascade.get(p) or {}).get("facts") or {},
                                          hints, args.cookies,
                                          links.get(row["file"]))): p
                    for p, row, _a in batch}
            for f in as_completed(futs):
                p = futs[f]
                try:
                    res = f.result()
                except Exception as e:
                    res = {"path": p, "file": os.path.basename(p),
                           "status": "download failed", "why": str(e)[:70]}
                if res["status"] == "downloaded":
                    fetched.append(res)
                else:
                    with lock:
                        done[_done_key(p, args)] = res
                        stats[res["status"]] = stats.get(res["status"], 0) + 1

        # Phase 2: measure, in separate processes.
        if fetched:
            with ThreadPoolExecutor(max_workers=cpu_workers) as ex:
                futs = {ex.submit(measure, r["staged"]): r for r in fetched}
                for f in as_completed(futs):
                    r = futs[f]
                    try:
                        r["new_cutoff_raw"], r["new_duration"] = f.result()
                    except Exception:
                        r["new_cutoff_raw"], r["new_duration"] = None, None

        # Phase 3: keep or delete.
        for r in fetched:
            res = judge(r, analysis.get(r["path"], {}), args)
            with lock:
                done[_done_key(r["path"], args)] = res
                stats[res["status"]] = stats.get(res["status"], 0) + 1
                if res["status"] == "kept":
                    print(f"    KEPT  +{res['gain']:>5}Hz  "
                          f"{res['old_cutoff']:>5}->{res['new_cutoff']:<5} "
                          f"{res['file'][:44]}", flush=True)
                elif res["status"].startswith("restored"):
                    print(f"    BACK  {res['new_cutoff']:>5}Hz  "
                          f"{res['how'][:20]:20} {res['file'][:44]}",
                          flush=True)
        json.dump(done, open(REPORT + ".tmp", "w"), ensure_ascii=False, indent=1)
        os.replace(REPORT + ".tmp", REPORT)
        got = (f"restored {stats['restored'] + stats['restored elsewhere']}"
               if args.missing
               else f"kept {stats['kept']}, not better {stats['not better']}")
        print(f"      {got}, failed {stats['download failed']}", flush=True)

    shutil.rmtree(STAGE_DIR, ignore_errors=True)
    print(f"\n  {len(todo)} attempted in {(time.time()-t0)/60:.1f} min\n")
    for k, v in sorted(stats.items(), key=lambda x: -x[1]):
        print(f"    {k:18} {v}")
    if args.missing:
        moved = stats["restored elsewhere"]
        print(f"\n  {stats['restored']} sources back at their own path.")
        if moved:
            print(f"  {moved} back under a different extension, because "
                  f"YouTube serves m4a. Those rows still read as missing.")
        print("  Re-run fingerprint and analyze over the folder, then review, "
              "before write_tags --prune.\n")
    else:
        print(f"\n  better files -> {OUT_DIR}")
        print("  originals untouched. Feed the folder back through run.py "
              "when you are happy with it.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
