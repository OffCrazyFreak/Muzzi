#!/usr/bin/env python3
"""Report hints that are a search query rather than a link to one track.

Reports only; writes nothing.

A hint like

    https://www.youtube.com/results?search_query=Elitni+Odredi+-+Krivi+Smo+Oboje

names no track. Whatever the pipeline scraped from it is the first result of a
search, but it is recorded as "from your link (site metadata)" and scored like
a confirmed link, which is how "Elitni Odredi - Krivi Smo Oboje.mp3" came to be
credited to Gospoda at confidence 1.0.

Usage:
  audit_hints.py --hints PATH [--review PATH]
"""
import argparse
import csv
import json
import re
import sys
from urllib.parse import parse_qs, urlparse

# A search endpoint names a query, not a recording. Anything here is a guess
# dressed as a citation.
SEARCH_MARKERS = (
    ("youtube.com", "/results"),
    ("music.youtube.com", "/search"),
    ("google.com", "/search"),
    ("deezer.com", "/search"),
    ("open.spotify.com", "/search"),
    ("soundcloud.com", "/search"),
    ("discogs.com", "/search"),
    ("musicbrainz.org", "/search"),
    ("last.fm", "/search"),
    ("bing.com", "/search"),
    ("duckduckgo.com", "/"),
)

# What a real track link looks like, for contrast in the summary.
TRACK_RE = re.compile(
    r"(youtu\.be/|youtube\.com/watch|music\.youtube\.com/watch|"
    r"deezer\.com/(\w\w/)?track/|open\.spotify\.com/track/|"
    r"musicbrainz\.org/recording/|discogs\.com/release/)", re.I)


def classify(hint):
    """-> 'search', 'track', 'other-url' or 'text'."""
    h = (hint or "").strip()
    if not h:
        return "text"
    if not h.lower().startswith(("http://", "https://")):
        return "text"
    u = urlparse(h)
    host = (u.netloc or "").lower().split(":")[0].removeprefix("www.")

    def is_host(dom):
        # On a label boundary, not a suffix: endswith() alone makes
        # "notyoutube.com" a YouTube host.
        dom = dom.removeprefix("www.")
        return host == dom or host.endswith("." + dom)

    for dom, path in SEARCH_MARKERS:
        if is_host(dom) and u.path.startswith(path):
            # duckduckgo puts the query in ?q= on the root path.
            if dom == "duckduckgo.com" and not parse_qs(u.query).get("q"):
                continue
            return "search"
    # Host and path, not the whole string: a query parameter containing
    # "youtube.com/watch" would otherwise make any URL a track link.
    if TRACK_RE.search(host + u.path):
        return "track"
    return "other-url"


def query_of(hint):
    q = parse_qs(urlparse(hint).query)
    for k in ("search_query", "q", "query"):
        if q.get(k):
            return q[k][0]
    return ""


def load_hints(path):
    """-> {filename: hint}. Sniff the delimiter the way review.py does."""
    with open(path, newline="", encoding="utf-8") as fh:
        sample = fh.read(8192)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters="\t,;")
        except csv.Error:
            dialect = csv.excel_tab
        rows = list(csv.reader(fh, dialect))
    out = {}
    for row in rows:
        if len(row) < 2 or not row[0].strip():
            continue
        if row[0].strip().lower() == "file":
            continue
        out[row[0].strip()] = row[1].strip()
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hints", required=True)
    ap.add_argument("--review", help="report the credit each search hint produced")
    args = ap.parse_args()

    hints = load_hints(args.hints)
    kinds = {}
    for name, hint in hints.items():
        kinds.setdefault(classify(hint), []).append((name, hint))

    print(f"\n  {len(hints)} hints\n")
    for kind in ("track", "search", "other-url", "text"):
        print(f"    {kind:10} {len(kinds.get(kind, [])):5}")

    rows = {}
    if args.review:
        for r in json.load(open(args.review)):
            rows[r["file"]] = r

    searches = sorted(kinds.get("search", []))
    print(f"\n  {len(searches)} hint(s) are a search, not a track:\n")
    for name, hint in searches:
        r = rows.get(name) or {}
        conf = r.get("confidence")
        print(f"    {name[:52]:52}")
        print(f"        searched: {query_of(hint)[:64]}")
        if r:
            print(f"        credited: {str(r.get('proposed_artist'))[:40]:40} "
                  f"| {str(r.get('proposed_title'))[:34]:34} "
                  f"| conf {conf}")
            for why in r.get("reasons") or []:
                print(f"        reason:   {why[:64]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
