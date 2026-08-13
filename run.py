#!/usr/bin/env python3
"""Run the whole pipeline against one or more music folders.

    run.py "/path/to/music"
    run.py "/path/to/music" "/path/to/more music"

Two kinds of stage, and pairing them is most of why this is not slow:

  * network-bound - identify, textsearch, webmatch, cascade, lyrics. Capped by
    MusicBrainz (1 req/s per IP), AcoustID (3/s) and Deezer, not by cores.
  * CPU-bound - fingerprint, analyze, verify_lyrics. Capped by cores.

Run them in sequence and each waits while the other resource sits idle. Run the
two chains concurrently and the wall clock is roughly the longer of the two
rather than their sum.

Order within a chain still matters: identify must finish before textsearch
(which only handles what identify missed), from_filename only sees what both
missed, and webmatch only sees what all three missed. Each stage narrows the
problem for the next one.

review runs several times on purpose. It is not a report -- it is the function
that turns evidence into a decision, so it re-runs every time new evidence
arrives, and every later stage reads its output rather than the raw caches.

Everything is idempotent: each stage reads its own cache and does only what is
missing. Interrupting and re-running costs nothing.

Usage:
  run.py MUSIC_DIR [MUSIC_DIR ...]
  run.py MUSIC_DIR --dry-run          # print the plan and exit
  run.py MUSIC_DIR --skip verify_lyrics enrich
  run.py MUSIC_DIR --from webmatch    # resume partway through
  run.py MUSIC_DIR -j 8
"""
import argparse
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(HERE, ".venv", "bin", "python")
BIN = os.path.join(HERE, "bin")
sys.path.insert(0, HERE)

from pipeline import sources  # noqa: E402
# Where music goes when you would rather not type a path. Anything in here is
# read-only to every stage, exactly like a folder named on the command line:
# putting a folder in input/ changes where we look, never what we may write.
INPUT = os.path.join(HERE, "input")


def default_roots():
    """-> the folders inside input/, symlinks resolved.

    A symlink is the point rather than a concession. The library lives on
    another partition and nobody should have to copy 8GB to run this, so
    `ln -s /media/.../Music/Whatever input/Whatever` is the intended use. The
    target is resolved here so that every stage downstream records the real
    path: a cache keyed on input/Whatever/song.mp3 would go stale the moment
    the link was renamed, and the entry would be unattributable.
    """
    if not os.path.isdir(INPUT):
        return []
    out, broken, loose = [], [], False
    for name in sorted(os.listdir(INPUT)):
        if name.startswith("."):
            continue
        here = os.path.join(INPUT, name)
        p = os.path.realpath(here)
        if os.path.isdir(p):
            out.append(p)
        elif os.path.isfile(p):
            # Audio dropped straight into input/ rather than in a folder.
            # Ignoring it silently is the failure this project has been bitten
            # by most: nothing errors and the track is simply absent from the
            # output. input/ itself becomes a root instead.
            loose = loose or name.lower().endswith(sources.AUDIO)
        else:
            broken.append(name)
    if broken:
        # Almost always an unmounted partition, which is the exact setup this
        # folder exists to support. Saying "input/ is empty" here, or worse
        # running against the roots that did resolve, would process half a
        # library and report success.
        sys.exit("these entries in input/ point at nothing:\n  " +
                 "\n  ".join(broken) +
                 "\nIs the drive mounted? Remove them to run without.")
    if loose:
        out.append(INPUT)
    return out


def run_stage(name, cmd, results, quiet=False):
    env = dict(os.environ, PATH=f"{BIN}:{os.environ.get('PATH','')}")
    t0 = time.time()
    print(f"  [{name}] start", flush=True)
    proc = subprocess.run(cmd, env=env, cwd=HERE,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True)
    el = time.time() - t0
    results[name] = {"rc": proc.returncode, "secs": round(el, 1),
                     "output": proc.stdout}
    status = "ok" if proc.returncode == 0 else f"FAILED rc={proc.returncode}"
    print(f"  [{name}] {status} in {el:.0f}s", flush=True)
    if proc.returncode != 0 and not quiet:
        print("\n".join(proc.stdout.splitlines()[-12:]))
    return proc.returncode == 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", nargs="*", help="folder(s) of music to process; "
                    "defaults to whatever is in input/")
    ap.add_argument("--skip", nargs="*", default=[], metavar="STAGE",
                    help="stage names to skip")
    ap.add_argument("--from", dest="start_at", metavar="STAGE",
                    help="skip everything before this stage")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-j", "--workers", type=int)
    args = ap.parse_args()

    if not args.root:
        args.root = default_roots()
        if not args.root:
            sys.exit(f"nothing to do: {INPUT} is empty.\n"
                     "Put your music folders in there (a symlink to each is "
                     "enough), or name them on the command line.")
        print(f"  reading {len(args.root)} folder(s) from "
              f"{os.path.basename(INPUT)}/")

    for r in args.root:
        if not os.path.isdir(r):
            sys.exit(f"not a folder: {r}")

    jflag = ["-j", str(args.workers)] if args.workers else []
    results = {}
    skip = set(args.skip)

    def stage(name, *cmd):
        return (name, [PY, os.path.join(HERE, "pipeline", f"{name}.py"), *cmd])

    # ---------------------------------------------------------------- plan
    #
    # A phase is either ("serial", [stages]) or ("parallel", [chain, chain]),
    # where a chain is a list of stages run in order. Phases run one after
    # another; the boundary between two phases is a barrier.

    review = stage("review")

    phases = [
        # Everything downstream needs a fingerprint, including dedupe.
        ("serial", [stage("fingerprint", *args.root, *jflag)]),

        ("parallel", [
            [stage("identify", *jflag), stage("textsearch")],
            # analyze BEFORE dedupe, not after. dedupe picks the copy to keep
            # by measured spectral cutoff, so running it against a stale
            # analysis.json means every newly-added file has no quality figure
            # and loses by default -- which silently kept the worse copy of 69
            # songs the first time re-downloads went through.
            # silence measures the SOURCE files, so it needs nothing but the
            # track list -- and measuring the source is what makes trimming
            # idempotent, since the thing measured never changes.
            [stage("analyze", *args.root, *jflag), stage("silence", *jflag),
             stage("dedupe")],
        ]),

        # First decision point: who is still unidentified.
        ("serial", [review]),

        # Believe the filename, then try to confirm it. Only sees what the
        # databases missed.
        ("serial", [stage("from_filename"), review]),

        # Any links or corrections left in the review spreadsheets. A no-op
        # on a first run, which is why it sits in the normal flow.
        ("serial", [stage("hints_resolve"), review]),

        # The streaming catalogues, for the tracks MusicBrainz does not carry.
        ("serial", [stage("webmatch"), review]),

        # Enrichment. Network work on one side, the GPU/CPU lyric work on the
        # other. cascade must precede fetch_art (it produces the URLs) and
        # artist_names (it produces the artist MBIDs).
        ("parallel", [
            [stage("cascade"), stage("fetch_art"), stage("enrich_release"),
             stage("enrich", *jflag)],
            # lyric_align needs the sheets lyrics_fetch downloads, and shares
            # verify_lyrics' Whisper models, so it follows both rather than
            # competing with them for cores.
            [stage("lyrics_fetch"), stage("verify_lyrics"),
             stage("lyric_align")],
        ]),

        # Lyric verification can promote a match, and cascade can fill an
        # album, so decide once more before anything is written.
        ("serial", [review]),

        # Tidy-up that depends on final identities: one spelling per artist,
        # then one copy per song.
        # origin needs the canonical spelling to key on, and produces the
        # artist-country map that splits the Balkan crate into real scenes.
        # origin needs the canonical spelling to key on; lastfm_tags feeds the
        # genre choice and caches, so a re-run costs nothing.
        ("serial", [stage("artist_names", "--apply"),
                    stage("origin", "--apply"),
                    stage("lastfm_tags"),
                    stage("dedupe_names", "--apply"),
                    # After dedupe_names on purpose: a link found on the copy
                    # that lost is inherited by the one that ships, so this has
                    # to know which copy that is.
                    stage("yt_links", "--apply")]),

        # The only stage that writes audio, and it writes copies.
        # --prune because output/_all is a rebuild, not an accumulation: without
        # it, a file whose source has since become a duplicate loser stays in
        # the output forever and the library grows a second copy of the song.
        # It will not delete the only copy of anything -- see write_tags.py.
        ("serial", [stage("write_tags", "--prune"), stage("export"),
                    stage("verify", os.path.join(HERE, "output", "_all"))]),
    ]

    # --from: drop every phase before the one containing that stage.
    if args.start_at:
        names = [n for _, chains in phases for c in chains
                 for n, _ in (c if isinstance(c, list) else [c])]
        if args.start_at not in names:
            sys.exit(f"unknown stage: {args.start_at}\n  known: {sorted(set(names))}")
        for i, (kind, chains) in enumerate(phases):
            flat = [n for c in chains
                    for n, _ in (c if isinstance(c, list) else [c])]
            if args.start_at in flat:
                phases = phases[i:]
                break

    def keep(chain):
        return [(n, c) for n, c in chain if n not in skip]

    if args.dry_run:
        print("\n  PLAN\n")
        for i, (kind, chains) in enumerate(phases, 1):
            if kind == "serial":
                got = keep(chains)
                if got:
                    print(f"  {i}. " + " -> ".join(n for n, _ in got))
            else:
                print(f"  {i}. concurrently:")
                for c in chains:
                    got = keep(c)
                    if got:
                        print("       " + " -> ".join(n for n, _ in got))
        print()
        return

    t0 = time.time()

    def run_chain(chain, flags, label):
        for name, cmd in chain:
            if not run_stage(name, cmd, results):
                flags[label] = False
                return
        flags[label] = True

    for kind, chains in phases:
        if kind == "serial":
            for name, cmd in keep(chains):
                ok = run_stage(name, cmd, results)
                # fingerprint is the one stage nothing can proceed without.
                if not ok and name == "fingerprint":
                    sys.exit("fingerprint failed; nothing downstream can run")
        else:
            flags, threads = {}, []
            for i, c in enumerate(chains):
                got = keep(c)
                if got:
                    threads.append(threading.Thread(
                        target=run_chain, args=(got, flags, i)))
            for t in threads:
                t.start()
            for t in threads:
                t.join()

    total = time.time() - t0
    serial = sum(v["secs"] for v in results.values())
    print(f"\n  wall clock {total/60:.1f} min")
    print(f"  sum of stages {serial/60:.1f} min "
          f"(overlap saved {(serial-total)/60:.1f} min)")
    failed = [k for k, v in results.items() if v["rc"] != 0]
    print(f"  failed stages: {failed or 'none'}")
    sheets = sorted(n for n in os.listdir(os.path.join(HERE, "review"))
                    if n.endswith(".ods")) if os.path.isdir(
                        os.path.join(HERE, "review")) else []
    if sheets:
        print("\n  Next: work through review/ in order --")
        for n in sheets:
            print(f"    {n}")
        print("  fill the hint column, then re-run:")
        print(f"    run.py {args.root[0]!r} --from hints_resolve\n")
    else:
        print("\n  Nothing left to review.\n")


if __name__ == "__main__":
    main()
