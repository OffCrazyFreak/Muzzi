#!/usr/bin/env python3
"""Why a track holds the values it holds.

`review/` says a track scored 0.78 and lists reasons in prose. That is enough
to decide whether to look, and not enough to decide anything else: it cannot
say that two catalogues agreed and a third disagreed, or that the only source
which answered was the file's own tags.

This prints the evidence instead. Read-only, over `cache/evidence.db`.

Agreement is counted in independence families, never in sources. Cover Art
Archive agreeing with MusicBrainz is MusicBrainz agreeing with itself, and a
count of endpoints would report that as corroboration.

Usage:
  explain.py "Colonia - Najbolje"          # substring of the filename
  explain.py /full/path/to/file.mp3
  explain.py "Colonia" --field year
  explain.py "Colonia" --all               # absences and failures too
"""
import argparse
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from pipeline import evidence, links  # noqa: E402

# Long values are the ones worth truncating hardest: a lyric sheet printed in
# full buries the one line that says where it came from.
WIDTH = 62


def resolve(conn, wanted):
    """-> [track_path, ...] matching a path, a basename or a substring.

    Ambiguity is reported rather than resolved. Picking the first of several
    matches is how you end up reading one track's evidence while thinking
    about another.
    """
    paths = [r[0] for r in conn.execute(
        "SELECT DISTINCT track_path FROM observation").fetchall()]
    if wanted in paths:
        return [wanted]
    exact = [p for p in paths if os.path.basename(p) == wanted]
    if exact:
        return exact
    low = wanted.casefold()
    return sorted(p for p in paths if low in os.path.basename(p).casefold())


def clip(value):
    if value is None:
        return "-"
    v = " ".join(str(value).split())
    return v if len(v) <= WIDTH else v[:WIDTH - 1] + "…"


def verdict(rows):
    """-> a one-line summary of who agrees with whom, in families.

    Ties are reported as ties. A field where two families say one thing and two
    say another is the case this tool exists for, and calling it for either
    side would hide exactly that.
    """
    answered = [r for r in rows if r["state"] == evidence.FOUND]
    if not answered:
        states = sorted({r["state"] for r in rows})
        return f"nothing answered ({', '.join(states)})"

    by_value = defaultdict(set)
    for r in answered:
        by_value[r["value_norm"]].add(r["family"])
    ranked = sorted(by_value.items(), key=lambda kv: -len(kv[1]))

    human = [v for v, fams in by_value.items() if evidence.HUMAN in fams]
    shown = {r["value_norm"]: r["value"] for r in answered}
    top, fams = ranked[0]
    line = (f"{len(fams)} famil{'y' if len(fams) == 1 else 'ies'} "
            f"on {clip(shown[top])!r}: {', '.join(sorted(fams))}")
    if len(ranked) > 1 and len(ranked[1][1]) == len(fams):
        line = "TIE, " + line + f"  vs {clip(shown[ranked[1][0]])!r}"
    elif len(ranked) > 1:
        line += f"  ({len(ranked) - 1} other value(s) disagree)"
    if human:
        line += "  [you answered this]"
    return line


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("track", help="path, filename or a substring of one")
    ap.add_argument("--field", help="only this field")
    ap.add_argument("--all", action="store_true",
                    help="include absences and failures, not just answers")
    ap.add_argument("--db", default=evidence.DB)
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit(f"no store at {args.db}; run "
                 f"pipeline/evidence.py --backfill first")
    conn = evidence.connect(args.db, readonly=True)

    found = resolve(conn, args.track)
    if not found:
        sys.exit(f"nothing in the store matches {args.track!r}")
    if len(found) > 1:
        print(f"\n  {len(found)} tracks match {args.track!r}:\n")
        for p in found[:20]:
            print(f"    {p}")
        if len(found) > 20:
            print(f"    ... and {len(found) - 20} more")
        sys.exit("\n  narrow it down\n")

    path = found[0]
    rows = evidence.observations(conn, path, args.field)
    if not rows:
        sys.exit(f"no observations for {os.path.basename(path)}"
                 + (f" field {args.field!r}" if args.field else ""))

    by_field = defaultdict(list)
    for r in rows:
        by_field[r["field"]].append(r)

    print(f"\n  {os.path.basename(path)}")
    print(f"  {path}\n")
    for field in sorted(by_field):
        group = by_field[field]
        show = group if args.all else [r for r in group
                                       if r["state"] == evidence.FOUND]
        if not show:
            # Every source was asked and none answered. Worth printing: it is
            # the difference between "nobody knows" and "nobody was asked".
            show = group
        print(f"  {field}")
        for r in sorted(show, key=lambda r: (r["family"], r["source"])):
            mark = "*" if r["source"] in evidence.AUDIO_DERIVED else " "
            state = "" if r["state"] == evidence.FOUND else f"  {r['state']}"
            print(f"    {mark} {clip(r['value']):{WIDTH}s}  "
                  f"{r['source']:<14s} [{r['family']}]{state}")
            if r["url"]:
                print(f"      {r['url']}")
        print(f"      -> {verdict(group)}\n")

    print("  * evidence from the audio itself, independent of every "
          "catalogue\n")

    # The same records the review sheets link to, so the answer to "where did
    # this come from" is one command rather than a spreadsheet.
    found_links = links.records(links.identifiers(conn, path))
    if found_links:
        print("  records\n")
        for label, url in found_links:
            print(f"    {label:24s} {url}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
