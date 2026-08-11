#!/usr/bin/env python3
"""Derive a trustworthy artist/title seed from embedded tags.

Namida writes real metadata into its downloads, which is far better input than
a filename. But it takes the artist from the UPLOADING CHANNEL, and a channel
is not always an artist. Measured on the 444 downloads:

  * album is ALWAYS the channel name ("Blockstar", "IDJVideos.TV",
    "KDM Exclusive"), never a real album -> discarded outright.
  * 22 artist tags are wrong, most severely "Release", a label channel that
    covers 8 unrelated artists (Thousand Foot Krutch, Ivan Zak, Haiden...).
  * Others are squashed channel handles: "limpbizkit", "TheKillersMusic",
    "SimplePlan", "Psihomodopop Official".

The fix is that auto-generated uploads carry a description beginning
"Provided to YouTube by <distributor>" followed by "<title> · <artist>", which
names the real artist. 198 of the 444 have one, and it corrects all 22.

Where no description exists, a channel-looking artist is marked weak so
identify.py can lean on the fingerprint instead of letting a bad name vote.

Also lifts the YouTube video id out of the comment tag, which is a permanent
per-track identity the filename cannot give us.
"""
import os
import re

import mutagen

# Only markers that a real artist name would not contain. An earlier, wider
# rule (plus a "no spaces and 9+ chars" shape test) flagged 12 genuine artists
# out of 18 -- ANASTASIJA, Basshunter, Macklemore, OneRepublic, XXXTENTACION,
# KRANKSVESTER, and bands whose names legitimately contain a trigger word
# (Blind Channel, Lapsus Band). Name shape says nothing about whether a name is
# an artist, so that test is gone.
_CHANNELISH = re.compile(
    r"\blyrics?\b|\bvevo\b|\btopic\b|\bkanal\b|\brealmusic\b"
    r"|\bofficial\s+channel\b|\btv\+|\.tv\b|\.com\b", re.I)

# Channels with no tell-tale word in the name, confirmed by inspection.
_KNOWN_CHANNELS = {"custombuilding4"}

_TRACKNO = re.compile(r"^\s*\d{1,2}\s*[.)-]\s+")
_YT_ID = re.compile(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})")
# "\n\nTitle · Artist · Composer\n" in auto-generated descriptions.
_PROVIDED = re.compile(r"\n\n(.+?)\s+·\s+(.+?)\n")


def looks_like_channel(name):
    if not name:
        return False
    return bool(_CHANNELISH.search(name)) or name.strip().lower() in _KNOWN_CHANNELS


_OV = None


def _overrides():
    global _OV
    if _OV is None:
        import json
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        p = os.path.join(here, "config", "artist_overrides.json")
        _OV = json.load(open(p)) if os.path.exists(p) else {}
    return _OV


def _text(tags, *keys):
    for k in keys:
        if k in tags and tags[k]:
            v = tags[k][0]
            if isinstance(v, bytes):
                try:
                    return v.decode("utf-8", "ignore")
                except Exception:
                    continue
            s = str(v)
            if s.strip():
                return s.strip()
    return None


def extract(path):
    """-> dict or None. Never returns the album: it is a channel name."""
    try:
        f = mutagen.File(path)
    except Exception:
        return None
    if f is None or f.tags is None:
        return None
    t = f.tags

    if hasattr(t, "getall"):                       # ID3
        def first(frame):
            fr = t.getall(frame)
            return str(fr[0].text[0]) if fr and getattr(fr[0], "text", None) else None
        artist, title = first("TPE1"), first("TIT2")
        comment = " ".join(str(c.text[0]) for c in t.getall("COMM")
                           if getattr(c, "text", None))
        desc = " ".join(str(fr.text[0]) for fr in t.getall("TXXX")
                        if fr.desc.upper() == "DESCRIPTION" and fr.text)
    else:                                          # MP4 / Vorbis
        artist = _text(t, "\xa9ART", "artist")
        title = _text(t, "\xa9nam", "title")
        comment = _text(t, "\xa9cmt", "comment") or ""
        desc = _text(t, "----:com.apple.iTunes:DESCRIPTION", "description") or ""

    if not (artist or title):
        return None

    # Uploads titled "02. Artist - Track" leave the position glued to the
    # artist. It is never part of the name and it poisons name comparison.
    artist = _TRACKNO.sub("", artist) if artist else artist
    title = _TRACKNO.sub("", title) if title else title

    out = {"tag_artist": artist, "title": title}
    m = _YT_ID.search(comment or "")
    if m:
        out["youtube_id"] = m.group(1)

    # The description is authoritative when present.
    real = None
    if desc and "Provided to YouTube" in desc:
        pm = _PROVIDED.search(desc)
        if pm:
            # "Artist · Composer · Lyricist" - only the first field is the artist.
            real = pm.group(2).split("·")[0].strip()

    # Hand overrides, keyed by video id. The description is right 31 times out
    # of 35, but it also yields feature chains ("Calvin Harris and Example"),
    # a track name ("Mare Mdma"), a different act entirely ("PANTER"), and one
    # de-accented spelling. Those four are pinned by hand.
    ov = _overrides().get(out.get("youtube_id"))
    if ov:
        out["artist"], out["trust"] = ov["artist"], "override"
        return out

    if real:
        out["artist"], out["trust"] = real, "description"
    elif looks_like_channel(artist):
        out["artist"], out["trust"] = artist, "weak"
    else:
        out["artist"], out["trust"] = artist, "tag"
    return out


def build(roots, cache_path=None):
    """Scan roots and return {path: seed}. Cheap: tag reads only."""
    import json
    seeds = {}
    exts = (".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav")
    for root in roots:
        for dp, _, names in os.walk(root):
            for n in sorted(names):
                if not n.lower().endswith(exts):
                    continue
                p = os.path.join(dp, n)
                s = extract(p)
                if s:
                    seeds[p] = s
    if cache_path:
        json.dump(seeds, open(cache_path + ".tmp", "w"),
                  ensure_ascii=False, indent=1)
        os.replace(cache_path + ".tmp", cache_path)
    return seeds


_CACHE = None


def load(here=None):
    global _CACHE
    if _CACHE is None:
        import json
        here = here or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        p = os.path.join(here, "cache", "tagseed.json")
        _CACHE = json.load(open(p)) if os.path.exists(p) else {}
    return _CACHE


_JUNK_ARTIST = re.compile(
    r"^(various(\s+artists)?|unknown(\s+artist)?|artist|va|no\s*artist|"
    r"untitled|n/?a|none|default|track|audio|music)$", re.I)


def seed_for(path, stem_fallback):
    """-> (artist, title, trust).

    Falls back to the filename parser when there is no usable tag, which is the
    case for every file in the original mp3 batch.
    """
    from pipeline.probe_match import split_name
    fa, ft = split_name(stem_fallback)
    s = load().get(path)
    if not s or not (s.get("artist") or s.get("title")):
        return fa, ft, "filename"

    # Tags are only better than the filename when we know where they came
    # from. A Namida download carries a video id and usually a distributor
    # description, and those tags are clean. Everything else is a rip of
    # unknown provenance: the phone library has a title tag on 1127 files, but
    # 1035 have no artist at all and 210 titles are encoding-damaged --
    # "Tisina" written "Tiina", "Drugi Covjek" written "Drugi Evjek". Those
    # filenames were curated by hand and are the better source, so tags there
    # only fill in what the filename could not supply.
    if not s.get("youtube_id"):
        return (fa or s.get("artist")), (ft or s.get("title")), "filename"

    a, ti = s.get("artist"), s.get("title")
    # Even a clean download can carry a placeholder where the artist should
    # be. "Various", "Unknown Artist" and a bare channel name are not artists,
    # and believing them puts a song under a name no player can group.
    if a and _JUNK_ARTIST.match(a.strip()):
        a = None
    return (a or fa), (ti or ft), s.get("trust", "tag")


def main():
    import argparse
    import json
    from collections import Counter
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("roots", nargs="+")
    args = ap.parse_args()
    out = os.path.join(here, "cache", "tagseed.json")
    seeds = build(args.roots, out)
    c = Counter(s["trust"] for s in seeds.values())
    fixed = [s for s in seeds.values()
             if s["trust"] == "description" and s.get("tag_artist")
             and s["artist"].lower() != s["tag_artist"].lower()]
    print(f"  {len(seeds)} files with usable tags -> {out}")
    print(f"    trust: {dict(c)}")
    print(f"    with a YouTube id: {sum(1 for s in seeds.values() if s.get('youtube_id'))}")
    print(f"    artist corrected from the description: {len(fixed)}")
    for s in fixed[:12]:
        print(f"      {s['tag_artist'][:26]:26} -> {s['artist'][:30]}")
    weak = [s for s in seeds.values() if s["trust"] == "weak"]
    print(f"    channel-looking artists left weak: {len(weak)}")
    for s in weak[:10]:
        print(f"      {str(s['artist'])[:34]:34} ({str(s['title'])[:30]})")


if __name__ == "__main__":
    main()
