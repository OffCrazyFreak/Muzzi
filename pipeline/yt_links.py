#!/usr/bin/env python3
"""The YouTube video each file came from, so the link travels inside the file.

The pipeline learns a video id in four separate places and, until this stage,
wrote none of them. 124 m4a files carried one only by accident: write_tags
copies the source before tagging, so the yt-dlp comment survived untouched.
Every MP3 had nothing, and every link typed into hints.tsv was discarded at
tag-writing time.

Namida links a local track to its video by scanning the comment tag for a URL,
which is exactly what those 124 accidental files already do. tagseed.py reads
the same comment back on re-ingest, so writing it closes a loop that was
already half-built.

A wrong link is worse than none, so evidence is tiered and the tiers are never
blurred:

  1. origin -- this audio came from this video. The file's own comment, a link
     you typed, or a redownload we kept. Trustworthy enough to own the comment
     field, which is the one a player will act on.
  2. exact -- keyed by an identifier rather than a name: a MusicBrainz
     recording url-rel. It names the right recording, but it is not where these
     bytes came from, so it never touches the comment.
  3. search -- a name query (YouTube Music). It can land on the wrong song, so
     it must corroborate another source or pass a duration and version-marker
     gate; failing that the file goes to the review sheet instead of into a
     tag.

Two independent sources naming the same video is the strongest signal short of
origin, and is accepted outright -- the same logic webmatch.py already uses to
grade a match A when two catalogues agree.

Deliberately NOT reusing redownload.py's 8-second --max-drift as a link filter.
Measured against the 171 links in hints.tsv, 35 of them (20.5%) drift more than
8 seconds from the file and 14 more than 20, the worst by 120: these are
YouTube rips whose intros the streaming single does not have. That threshold is
right in redownload.py, where it stops "improve quality" becoming "change the
song", but as a link filter it would throw away a fifth of the answers you gave
by hand. Origin evidence is therefore never duration-checked at all, and the
search tier gets a deliberately loose window.

Odesli/song.link was measured and dropped: it answers by platform id (a bare
ISRC is a 400), and five lookups including mainstream tracks whose videos
certainly exist returned no YouTube entity at all. It is no longer a source of
YouTube links.

Writes cache/youtube_links.json, keyed by source path. write_tags.py reads it
and writes the URL into COMM / (c)cmt for origin, and YOUTUBE_ID into
TXXX/freeform for everything it accepts.

Usage:
  yt_links.py                    # report
  yt_links.py --apply
  yt_links.py --apply --offline  # caches only, no network
  yt_links.py --apply --limit 50 # cap the network lookups
"""
import argparse
import csv
import json
import os
import sys
from collections import Counter, defaultdict

import requests

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from pipeline.hints_resolve import video_id  # noqa: E402
from pipeline.identify import RateLimiter  # noqa: E402
from pipeline.review import (_hints_from_ods, _ods, load_hints,  # noqa: E402
                             load_links, save_hints)
from pipeline.textsearch import get_with_retry  # noqa: E402
from pipeline.useragent import UA  # noqa: E402
from pipeline.webmatch import fit, src_ytmusic, version_mismatch  # noqa: E402

REVIEW = os.path.join(HERE, "cache", "review.json")
CASCADE = os.path.join(HERE, "cache", "cascade.json")
ANALYSIS = os.path.join(HERE, "cache", "analysis.json")
TAGSEED = os.path.join(HERE, "cache", "tagseed.json")
HINT_YT = os.path.join(HERE, "cache", "hint_youtube.json")
WEBMATCH = os.path.join(HERE, "cache", "webmatch.json")
REDOWNLOAD = os.path.join(HERE, "cache", "redownload.json")
DUPES = os.path.join(HERE, "cache", "duplicates.json")
NAME_DUPES = os.path.join(HERE, "cache", "name_duplicates.json")
HINTS = os.path.join(HERE, "hints.tsv")
REVIEW_DIR = os.path.join(HERE, "review")
LOOKUP = os.path.join(HERE, "cache", "yt_lookup.json")
OUT = os.path.join(HERE, "cache", "youtube_links.json")
# What the sheet last offered, per file. The answer "y" means nothing without
# it, because the sheet that asked has been rebuilt by the time it is read.
PROPOSED = os.path.join(HERE, "cache", "yt_proposed.json")

SHEET = "4 - confirm the youtube link"
SHEET_NOTE = ("A YouTube link was found for these, but not confidently enough "
              "to write it into the file. Put the right link in the 'link' "
              "column, or 'n' to refuse one. Blank means leave it alone.")

MB_URL = "https://musicbrainz.org/ws/2/recording/{}?inc=url-rels&fmt=json"
MB_RATE = 1.0
YT_RATE = 3.0

# Relations that mean "you can watch/listen to this recording here". "streaming"
# covers the paid services too, but a youtube.com URL under either is still a
# video of this exact recording, which is the only thing being claimed.
MB_RELS = ("free streaming", "streaming")

# The search tier only. Origin and exact evidence is never duration-checked --
# see the module docstring for the measurement behind that. 30s is wide enough
# for the long cold opens these rips have (the official video of "Prevarena" is
# 48s longer than the single) and still catches a search landing on a different
# song entirely.
MAX_SEARCH_DRIFT = 30.0

WATCH = "https://www.youtube.com/watch?v={}"

# Strongest first, for when one file has several origin claims.
ORIGIN_ORDER = ["file comment", "your link", "redownloaded audio"]


def _load(path, default):
    """-> the cache at path, or default. Missing caches are not an error: this
    stage degrades to whatever evidence exists rather than refusing to run."""
    return json.load(open(path)) if os.path.exists(path) else default


def link_hints():
    """-> {filename: link}, from hints.tsv and from the sheet you edited.

    A column of its own, never 'hint'. review.load_hints() reads the 'hint'
    column of every file in review/, so a bare "y" answering a link question
    would otherwise be parsed by parse_hint as confirming the artist and title.

    The sheet is read after hints.tsv, so an answer typed today wins over the
    same file's older one, and answers are written back into hints.tsv: a sheet
    is a view, and the next run rebuilds it.
    """
    out = dict(load_links())
    if os.path.isdir(REVIEW_DIR):
        for name in sorted(os.listdir(REVIEW_DIR)):
            path = os.path.join(REVIEW_DIR, name)
            if name.lower().endswith(".ods"):
                out.update(_hints_from_ods(path, column="link"))
            elif name.lower().endswith((".tsv", ".csv", ".txt")):
                out.update(_from_delimited(path))
    return out


def _from_delimited(path):
    """-> {filename: link} from a saved-as-text sheet."""
    out = {}
    with open(path, encoding="utf-8-sig", newline="") as fh:
        head = fh.readline()
        if not head or "link" not in head:
            return out
        fh.seek(0)
        for row in csv.DictReader(fh, delimiter=max(("\t", ",", ";"),
                                                    key=head.count)):
            name = (row.get("file") or "").strip()
            link = (row.get("link") or "").strip()
            if name and link:
                out[name] = link
    return out


def groups_of(dupes, name_dupes):
    """-> [{path, ...}, ...] every set of files that are the same song.

    Both dedupe stages, unioned. A link found on the copy that lost on spectral
    cutoff still names the right song, and without this it dies with that copy
    -- the same reasoning that makes write_tags inherit the group's best
    identity onto the keeper.
    """
    out = []
    for g in dupes or []:
        out.append({g["keep"], *(g.get("drop") or [])})
    for g in (name_dupes or {}).get("groups") or []:
        out.append({g["keep"], *(g.get("drop") or [])})
    return out


def candidates_from_caches(rows, tagseed, hint_yt, webmatch, redl, hlinks):
    """-> {path: [candidate, ...]} from local evidence only.

    A candidate is {video_id, tier, source, duration, title}.
    """
    cand = defaultdict(list)

    def add(path, vid, tier, source, duration=None, title=None):
        if not path or not vid:
            return
        for c in cand[path]:
            if c["video_id"] == vid and c["source"] == source:
                return
        cand[path].append({"video_id": vid, "tier": tier, "source": source,
                           "duration": duration, "title": title})

    for path, seed in (tagseed or {}).items():
        if isinstance(seed, dict) and seed.get("youtube_id"):
            add(path, seed["youtube_id"], "origin", "file comment")

    # Links you typed. The hint column carries them inline with an identity
    # answer, the link column answers this stage's own sheet.
    for r in rows:
        vid = video_id(r.get("hint") or "")
        if vid:
            res = (hint_yt or {}).get(vid) or {}
            add(r["path"], vid, "origin", "your link",
                res.get("duration"), res.get("video_title"))
    for r in rows:
        link = hlinks.get(r["file"])
        vid = video_id(link or "")
        if vid:
            res = (hint_yt or {}).get(vid) or {}
            add(r["path"], vid, "origin", "your link",
                res.get("duration"), res.get("video_title"))

    # A kept redownload IS that video's audio stream. The report keys the row
    # by the original path and records the new file under "path", and either
    # can be the source a later run walks, so both are registered.
    for orig, row in (redl or {}).items():
        if row.get("status") == "kept" and row.get("video_id"):
            add(orig, row["video_id"], "origin", "redownloaded audio")
            add(row.get("path"), row["video_id"], "origin", "redownloaded audio")

    for path, row in (webmatch or {}).items():
        vid = row.get("video_id")
        if not vid:
            continue
        yt = (row.get("candidates") or {}).get("ytmusic") or {}
        add(path, vid, "search", "ytmusic", yt.get("duration"), yt.get("title"))
    return cand


def propagate(cand, groups):
    """Share candidates across every copy of a song. -> {path: from_path}."""
    inherited = {}
    for group in groups:
        pool = []
        for p in group:
            for c in cand.get(p) or []:
                pool.append((p, c))
        if not pool:
            continue
        for p in group:
            have = {(c["video_id"], c["source"]) for c in cand.get(p) or []}
            for src_path, c in pool:
                if src_path == p or (c["video_id"], c["source"]) in have:
                    continue
                cand[p].append({**c, "inherited": src_path})
                have.add((c["video_id"], c["source"]))
                inherited.setdefault(p, src_path)
    return inherited


def mb_youtube(recording_id, session, limiter, cache):
    """-> a video id from this recording's url relations, or None."""
    key = "mb:" + recording_id
    # A cached FAILURE is not an answer. AGENTS.md forbids caching a failed
    # request as a real one, and testing only for the key made one timeout or
    # one 503 into "this recording has no video", permanently, until --force.
    if key in cache and not cache[key].get("error"):
        return cache[key].get("video_id")
    vid = None
    try:
        r = get_with_retry(session, MB_URL.format(recording_id), limiter,
                           headers=UA)
        for rel in (r.json().get("relations") or []):
            if rel.get("type") not in MB_RELS:
                continue
            url = (rel.get("url") or {}).get("resource") or ""
            if "youtu" in url:
                vid = video_id(url)
                if vid:
                    break
    except Exception as e:
        # Recorded so a later run can say why, but not treated as an answer.
        cache[key] = {"video_id": None, "error": str(e)[:80]}
        return None
    cache[key] = {"video_id": vid}
    return vid


def yt_search(artist, title, limiter, cache):
    """-> {video_id, duration, title} for the best-fitting hit, or None."""
    key = f"search:{(artist or '').lower()}|{(title or '').lower()}"
    # Same rule as mb_youtube: a lookup that failed is retried, not believed.
    if key in cache and not cache[key].get("error"):
        return cache[key].get("hit")
    hits, err = src_ytmusic(artist or "", title or "", limiter)
    best = None
    for c in hits or []:
        if not c.get("video_id"):
            continue
        if fit(artist, c.get("artist")) < 0.6 or fit(title, c.get("title")) < 0.6:
            continue
        best = {"video_id": c["video_id"], "duration": c.get("duration"),
                "title": c.get("title")}
        break
    cache[key] = {"hit": best, "error": err}
    return best


REFUSALS = {"n", "no", "none", "-"}
# "the link you proposed is the right one". The other half of REFUSALS, and it
# had no half: a `y` was written into hints.tsv and then read by nothing at
# all, because `video_id("y")` is None and only refusals were checked by name.
# Measured on this library, 29 answers said yes and 29 did nothing.
AFFIRMATIONS = {"y", "yes"}


def proposal_for(cands):
    """-> the candidate the review sheet shows, or None.

    One definition, used both to write the sheet and to resolve a `y` against
    it. Two copies of this rule would let the answer confirm a different video
    than the question offered.
    """
    if not cands:
        return None
    return sorted(cands, key=lambda c: c["tier"] != "exact")[0]


def decide(path, cands, row, secs, refused=False, confirmed=None):
    """-> (record, reason) for what to write, or (None, reason) to review it.

    A video you confirmed wins outright, then origin, and neither is gated.
    Otherwise two independent sources naming one video is enough; a lone exact
    source is enough; a lone search result has to prove itself on duration and
    version markers.
    """
    if refused:
        # "n" in the sheet is an answer, not a blank: stop proposing this one
        # and stop asking about it.
        return None, None
    if confirmed:
        # You looked at the video and said it was right. Nothing measured here
        # outranks that, which is the same standing a pasted link already has.
        #
        # Judged before the candidate list is checked, not after. A confirmed
        # id can come from the recorded proposal alone, and that is exactly the
        # case where the candidates have since lapsed: gating it on candidates
        # would drop the answer in the one situation the recorded proposal
        # exists to survive.
        src = next((c["source"] for c in cands or []
                    if c["video_id"] == confirmed), "your answer")
        return {"video_id": confirmed, "trust": "origin",
                "from": "you confirmed it", "sources": [src]}, None
    if not cands:
        return None, None
    origin = [c for c in cands if c["tier"] == "origin"]
    if origin:
        # The file's own comment outranks anything inherited from a copy: it is
        # the only one of these that is a statement about *this* file. Within
        # the same standing, an answer you typed beats a download we chose.
        origin.sort(key=lambda c: (bool(c.get("inherited")),
                                   ORIGIN_ORDER.index(c["source"])))
        c = origin[0]
        return {"video_id": c["video_id"], "trust": "origin",
                "from": c["source"],
                "sources": sorted({o["source"] for o in origin})}, None

    by_id = defaultdict(list)
    for c in cands:
        by_id[c["video_id"]].append(c)
    ranked = sorted(by_id.items(), key=lambda kv: -len({c["source"] for c in kv[1]}))
    top_id, top = ranked[0]
    n_sources = len({c["source"] for c in top})

    if len(ranked) > 1:
        runner = len({c["source"] for c in ranked[1][1]})
        if n_sources <= runner:
            return None, ("sources disagree: "
                          + ", ".join(f'{vid} ({len({c["source"] for c in cs})})'
                                      for vid, cs in ranked[:3]))

    if n_sources >= 2:
        return {"video_id": top_id, "trust": "reference",
                "from": top[0]["source"],
                "sources": sorted({c["source"] for c in top})}, None

    c = top[0]
    if c["tier"] == "exact":
        return {"video_id": top_id, "trust": "reference", "from": c["source"],
                "sources": [c["source"]]}, None

    # Lone search hit: prove it.
    title = row.get("proposed_title") or row.get("file")
    bad = version_mismatch(title, c.get("title"))
    if bad:
        return None, f"version markers differ ({', '.join(bad)})"
    if not c.get("duration") or not secs:
        # Nothing corroborates it and nothing can be measured against it. Every
        # search hit so far has carried a duration, so this costs no coverage
        # today; it is here so an unmeasurable one is never waved through.
        return None, "lone search hit, nothing to check it against"
    if abs(c["duration"] - secs) > MAX_SEARCH_DRIFT:
        return None, (f"search hit is {abs(c['duration'] - secs):.0f}s from "
                      f"the file")
    return {"video_id": top_id, "trust": "reference", "from": c["source"],
            "sources": [c["source"]]}, None


def publish_sheet(items):
    """Write the review sheet for links that could not be settled.

    The proposals go to a cache of their own at the same time. A `y` answers a
    question, and the question was "is this video the right one" about one
    specific video; the sheet is rebuilt from scratch every run, so without
    this the video is gone by the time the answer is read and the `y` points at
    nothing. That is exactly what happened to 29 answers here.
    """
    os.makedirs(REVIEW_DIR, exist_ok=True)
    proposed = {r["file"]: r["proposed_link"] for r in items
                if r.get("file") and r.get("proposed_link")}
    if proposed:
        json.dump(proposed, open(PROPOSED + ".tmp", "w"),
                  ensure_ascii=False, indent=1)
        os.replace(PROPOSED + ".tmp", PROPOSED)
    path = os.path.join(REVIEW_DIR, SHEET + ".ods")
    cols = ["rank", "file", "artist", "title", "proposed_link", "why", "link"]

    def cells(r, i):
        return [i, r["file"], r.get("artist") or "", r.get("title") or "",
                r.get("proposed_link") or "", r.get("why") or "", ""]

    _ods(path, SHEET, SHEET_NOTE, items, cols=cols, cells=cells)
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, help="cap the network lookups")
    ap.add_argument("--offline", action="store_true",
                    help="local caches only, no MusicBrainz or YouTube Music")
    ap.add_argument("--force", action="store_true",
                    help="ignore cache/yt_lookup.json and ask again")
    args = ap.parse_args()

    rows = _load(REVIEW, [])
    if not rows:
        sys.exit("no cache/review.json -- run review.py first")
    cascade = _load(CASCADE, {})
    analysis = {v["path"]: v for v in _load(ANALYSIS, {}).values() if v.get("path")}
    lookup = {} if args.force else _load(LOOKUP, {})

    answers = link_hints()
    refused = {f for f, v in answers.items() if v.strip().lower() in REFUSALS}
    affirmed = {f for f, v in answers.items()
                if v.strip().lower() in AFFIRMATIONS}
    # What the sheet offered when it asked. Answers given before this cache
    # existed have no record of the question, so those fall back to re-deriving
    # the proposal from the same caches by the same rule. It is the same video
    # unless a later run found a better candidate, and the count is printed
    # rather than buried, because a re-derived confirmation is weaker evidence
    # than one checked against the question that was actually asked.
    was_offered = _load(PROPOSED, {})
    cand = candidates_from_caches(
        rows, _load(TAGSEED, {}), _load(HINT_YT, {}), _load(WEBMATCH, {}),
        _load(REDOWNLOAD, {}), answers)
    local_files = sum(1 for r in rows if cand.get(r["path"]))
    inherited = propagate(cand, groups_of(_load(DUPES, []),
                                          _load(NAME_DUPES, {})))
    print(f"\n  {local_files} files have a video id in the caches, "
          f"{len(inherited)} more inherit one from their duplicate")

    # Refuse a link on a file we could not name. Without an artist and title
    # there is nothing to check a search result against, and a wrong link is
    # worse than none.
    todo = [r for r in rows
            if not cand.get(r["path"])
            and r.get("proposed_artist") and r.get("proposed_title")]
    if not args.offline and todo:
        if args.limit:
            todo = todo[:args.limit]
        session = requests.Session()
        mb_lim, yt_lim = RateLimiter(MB_RATE), RateLimiter(YT_RATE)
        print(f"  {len(todo)} files with no id yet, looking them up "
              f"(MusicBrainz at {MB_RATE:.0f}/s, then YouTube Music)\n")
        for i, r in enumerate(todo, 1):
            facts = (cascade.get(r["path"]) or {}).get("facts") or {}
            rec = r.get("recording_id") or facts.get("recording_id")
            if rec:
                vid = mb_youtube(rec, session, mb_lim, lookup)
                if vid:
                    cand[r["path"]].append(
                        {"video_id": vid, "tier": "exact",
                         "source": "musicbrainz", "duration": None,
                         "title": None})
            hit = yt_search(r["proposed_artist"], r["proposed_title"],
                            yt_lim, lookup)
            if hit:
                cand[r["path"]].append(
                    {"video_id": hit["video_id"], "tier": "search",
                     "source": "ytmusic search", "duration": hit.get("duration"),
                     "title": hit.get("title")})
            if i % 25 == 0 or i == len(todo):
                print(f"    {i}/{len(todo)}  {sum(1 for c in cand.values() if c)} "
                      f"files with a candidate", flush=True)
                # Written with or without --apply. The lookups happen either
                # way, MusicBrainz at one request per second, and throwing the
                # answers away made a report run cost the same as a real one
                # every time. This file is a cache of questions already asked;
                # it changes no output.
                json.dump(lookup, open(LOOKUP + ".tmp", "w"),
                          ensure_ascii=False, indent=1)
                os.replace(LOOKUP + ".tmp", LOOKUP)

    out, sheet, stats, why = {}, [], Counter(), Counter()
    confirmed_from = Counter()
    for r in rows:
        path = r["path"]
        secs = (analysis.get(path) or {}).get("decoded_secs")
        cands = cand.get(path) or []
        yes = None
        if r["file"] in affirmed:
            yes = video_id(was_offered.get(r["file"]) or "")
            if yes:
                confirmed_from["against the link you were shown"] += 1
            else:
                best = proposal_for(cands)
                yes = best["video_id"] if best else None
                confirmed_from["re-derived, the sheet was already rebuilt"
                               if yes else "no candidate left to confirm"] += 1
        if yes:
            # Write the confirmation back as the URL it resolved to, replacing
            # the bare "y". A "y" is only meaningful next to the question that
            # asked it; the URL is meaningful anywhere, and everything
            # downstream already understands one. hints_resolve reads it as a
            # link you gave and fetches the video's metadata from it,
            # redownload reads it as the source to fetch audio from, and this
            # stage reads it as origin evidence. None of that reached a "y".
            answers[r["file"]] = WATCH.format(yes)
        rec, reason = decide(path, cands, r, secs,
                             refused=r["file"] in refused, confirmed=yes)
        if rec:
            rec.update({"url": WATCH.format(rec["video_id"]),
                        "inherited_from": inherited.get(path),
                        "file": r["file"]})
            out[path] = rec
            stats[rec["trust"]] += 1
            why[rec["from"]] += 1
        elif reason:
            best = proposal_for(cands)
            sheet.append({"file": r["file"], "artist": r.get("proposed_artist"),
                          "title": r.get("proposed_title"),
                          "proposed_link": WATCH.format(best["video_id"]),
                          "why": reason})

    print(f"\n  {len(out)} files get a link, {len(sheet)} need your eyes\n")
    for tier in ("origin", "reference"):
        if stats[tier]:
            print(f"    {tier:12} {stats[tier]:5}")
    print()
    for src, n in why.most_common():
        print(f"    {src[:34]:34} {n:5}")
    if confirmed_from:
        print(f"\n    {sum(confirmed_from.values())} of your 'y' answers "
              f"applied:")
        for how, n in confirmed_from.most_common():
            print(f"      {how[:44]:44} {n:5}")

    if not args.apply:
        json.dump(lookup, open(LOOKUP + ".tmp", "w"),
                  ensure_ascii=False, indent=1)
        os.replace(LOOKUP + ".tmp", LOOKUP)
        print(f"\n  {len(out)} links resolved. Re-run with --apply.\n")
        return 0

    # Answers typed into the sheet move into hints.tsv, which is the record
    # that survives: the sheet is rebuilt from scratch on the next run.
    if answers != load_links():
        save_hints(load_hints(), answers)
        print(f"\n  {len(answers)} link answers kept in {HINTS}")

    json.dump(out, open(OUT + ".tmp", "w"), ensure_ascii=False, indent=1)
    os.replace(OUT + ".tmp", OUT)
    json.dump(lookup, open(LOOKUP + ".tmp", "w"), ensure_ascii=False, indent=1)
    os.replace(LOOKUP + ".tmp", LOOKUP)
    print(f"\n  -> {OUT}  ({len(out)} links)")
    if sheet:
        print(f"  -> {publish_sheet(sheet)}  ({len(sheet)} rows)")
    print("  write_tags.py will now put them in the files\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
