#!/usr/bin/env python3
"""Stage 8: build crate folders and M3U playlists from the tagged output.

Two delivery formats on purpose:

  * Folders  - every Android player can browse a directory. No playlist import,
    no path rewriting, nothing to go wrong. This is the guaranteed path.
  * M3U      - nicer where the player supports it, but paths are the catch:
    Poweramp resolves songs by absolute path and ignores relative ones, while
    other players want relative. Relative is what gets written; --absolute
    adds a second set for Poweramp and anything else that insists.

Crates are built by hardlink where possible, so N crates cost no extra disk.
Falls back to copying across filesystems.

Usage: export.py [--out DIR] [--phone-root /storage/emulated/0/Music]
"""
import argparse
import json
import os
import re
import shutil
import sys
from collections import defaultdict


HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

# Bands chosen for mixing, not for tidiness: each spans roughly what you can
# beatmatch without obvious pitch damage.
BPM_BANDS = [(0, 90, "060-090"), (90, 100, "090-100"),
             (100, 110, "100-110"), (110, 120, "110-120"),
             (120, 128, "120-128 house"), (128, 135, "128-135"),
             (135, 150, "135-150"), (150, 999, "150+ fast")]

# Whisper does not cleanly separate the BCMS continuum and tends to label
# Serbian as Bulgarian on this library, so bg is bucketed here too. If you ever
# hold genuinely Bulgarian music, split it back out.
BALKAN_LANGS = {"hr", "sr", "bs", "sl", "mk", "bg"}

# A playlist below this is not worth the row it occupies in a player's list.
# Genre fragments produced 26 of them, twelve holding a single track.
MIN_PLAYLIST = 15

# Language is exempt. Montenegrin and Macedonian are real distinctions that
# happen to be small, the fold below already collapses the Whisper noise tail
# into Other, and Other is a catch-all: pruning it would silently drop the
# tracks it exists to hold.
FLOOR_EXEMPT = ("Language",)


def band_for(bpm):
    for lo, hi, name in BPM_BANDS:
        if lo <= bpm < hi:
            return name
    return None


def link_or_copy(src, dst):
    """Hardlink so crates are free; copy if that is impossible."""
    if os.path.exists(dst):
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "out"))
    ap.add_argument("--crates",
                    default="BPM,Language,Decade,Mood,Quality,Unknown,Genre",
                    help="comma-separated crate groups to emit. Camelot (key) "
                         "is off by default: 24 harmonic-mixing crates buried "
                         "the 8 BPM ones that actually get used.")
    ap.add_argument("--phone-root", default="/storage/emulated/0/Music",
                    help="path the files will live at on the phone, for the "
                         "absolute-path M3U variant")
    ap.add_argument("--absolute", action="store_true",
                    help="also emit .abs.m3u with absolute phone paths, for "
                         "players like Poweramp that ignore relative ones")
    args = ap.parse_args()
    groups = {g.strip() for g in args.crates.split(',') if g.strip()}

    # --crates used to be parsed and then consulted for Camelot alone, so
    # every other group was emitted no matter what you passed. It is honoured
    # for all of them now.
    from pipeline import scenes

    # Only the hand-curated scenes get a genre playlist. Every other genre
    # crate was built from the TCON tag already inside the file, so a player's
    # own genre browser showed exactly the same thing, and could not disagree
    # by construction. Turbofolk is not in scenes.json (it comes from the
    # Last.fm and Discogs tables) but is browsed the same way, so it is named
    # here rather than left out.
    scene_genres = set(scenes.scene_names()) | {"Turbofolk"}

    allpath = os.path.join(args.out, "_all")
    if not os.path.isdir(allpath):
        sys.exit(f"no tagged output at {allpath} - run write_tags.py first")

    # Artist origin, for splitting the Balkan crate into real scenes. Keyed
    # case-folded because the tag's capitalisation and the resolver's need not
    # agree ("30zona" against "30Zona").
    op = os.path.join(HERE, "cache", "artist_origin.json")
    origin = {k.lower(): v for k, v in
              (json.load(open(op)) if os.path.exists(op) else {}).items()}

    def canon_artist(a):
        return (a or "").strip().lower()

    crates = defaultdict(list)
    # Recursive, and not MP3-only. The output mirrors the source folder
    # layout now, and a quarter of the library is m4a -- a flat *.mp3
    # listing found nothing and produced zero playlists.
    import mutagen
    files = []
    for dp, _, names in os.walk(allpath):
        for n in names:
            if n.lower().endswith((".mp3", ".m4a", ".flac", ".ogg", ".opus")):
                files.append(os.path.relpath(os.path.join(dp, n), allpath))
    files.sort()
    for fn in files:
        p = os.path.join(allpath, fn)
        try:
            mp3 = mutagen.File(p)
            t = mp3.tags if mp3 else None
        except Exception:
            continue
        if t is None:
            continue

        # One accessor for both containers: ID3 keeps custom fields in TXXX,
        # MP4 in "----:com.apple.iTunes:NAME" as raw bytes.
        is_id3 = hasattr(t, "getall")

        def txxx(desc):
            if is_id3:
                fr = t.getall(f"TXXX:{desc}")
                return str(fr[0].text[0]) if fr and fr[0].text else None
            k = f"----:com.apple.iTunes:{desc}"
            if k in t and t[k]:
                v = t[k][0]
                return v.decode("utf-8", "ignore") if isinstance(v, bytes) else str(v)
            return None

        def std(id3_frame, mp4_key):
            if is_id3:
                fr = t.get(id3_frame)
                return str(fr.text[0]) if fr else None
            if mp4_key not in t or not t[mp4_key]:
                return None
            v = t[mp4_key][0]
            # MP4 freeform atoms hold raw bytes. str() on those yields the
            # repr -- "b'hr'" -- which is not the language code, misses the
            # Balkan bucket, and creates a crate named after a Python literal.
            return v.decode("utf-8", "ignore") if isinstance(v, bytes) else str(v)

        bpm = None
        try:
            bpm = float(txxx("BPM_PRECISE") or std("TBPM", "tmpo") or 0)
        except Exception:
            pass
        if bpm and "BPM" in groups:
            b = band_for(bpm)
            if b:
                crates[os.path.join("BPM", b)].append(fn)

        # The tag only, with no fall back to the raw Whisper guess: that guess
        # is what produced a "km" (Khmer) playlist out of instrumental EDM.
        # write_tags.resolve_language decides this once, for both containers.
        lang = std("TLAN", "----:com.apple.iTunes:LANGUAGE")
        artist = std("TPE2", "aART") or std("TPE1", "\xa9ART")
        o = origin.get(canon_artist(artist))
        # Your own sorting is evidence nothing else can supply: 83% of "Music
        # Other" is Balkan against 33% of "Music Mine", so where no origin was
        # resolved the folder still says which shelf it came off.
        in_balkan_folder = "Music Other" in fn
        is_balkan = bool(o) or (lang in BALKAN_LANGS) or in_balkan_folder

        if "Language" in groups:
            if is_balkan:
                # Both, deliberately. The per-country crates are what makes
                # this browsable, but "all of it" is still a thing you want to
                # put on and the union costs nothing: a playlist references
                # files.
                crates[os.path.join("Language", "Balkan")].append(fn)
                if o:
                    crates[os.path.join("Language", o["label"])].append(fn)
            elif lang:
                crates[os.path.join("Language", lang)].append(fn)

        # Genre crates come from the tag written into the file, not from a
        # second list kept alongside it. The tag is what a player groups by,
        # so a playlist built from anything else can only disagree with it.
        if is_id3:
            fr = t.getall("TCON")
            genres = [str(x) for x in (fr[0].text if fr and fr[0].text else [])]
        else:
            genres = [str(x) for x in (t.get("\xa9gen") or [])]
        for g in genres:
            g = g.strip()
            if g and "Genre" in groups and g in scene_genres:
                crates[os.path.join("Genre", g)].append(fn)

        cam = txxx("CAMELOT")
        if cam and "Camelot" in groups:
            crates[os.path.join("Camelot", cam)].append(fn)

        q = txxx("QUALITY")
        if q == "low" and "Quality" in groups:
            crates["Check quality"].append(fn)

        # No identity was trusted for these; they still have BPM and key.
        if txxx("MUZZI_SOURCE") == "audio-only" and "Unknown" in groups:
            crates["Needs identification"].append(fn)

        try:
            d = float(txxx("DANCEABILITY") or 0)
            if d >= 1.2 and "Mood" in groups:
                crates["Mood/danceable"].append(fn)
        except Exception:
            pass

        yr = std("TDRC", "\xa9day")
        if yr and "Decade" in groups:
            try:
                y = int(str(yr)[:4])
                crates[os.path.join("Decade", f"{y//10*10}s")].append(fn)
            except Exception:
                pass

    # Whisper misfires on short or heavily-produced vocals, producing a tail of
    # one-track language crates (az, km, ko...). Fold anything tiny into Other
    # rather than presenting noise as structure.
    small = [c for c in crates
             if c.startswith("Language" + os.sep) and len(crates[c]) < 5
             and not c.endswith("Balkan")]
    for c in small:
        crates[os.path.join("Language", "Other")].extend(crates.pop(c))

    # Anything left that is too short to be worth a row in the player's
    # playlist list. Done after the fold above, so a language crate is judged
    # on its final size rather than its size before Other absorbed the tail.
    # Dropped, not merged: there is no parent to merge a four-track BPM band
    # into, and inventing one would be worse than not offering the playlist.
    tiny = [c for c in crates
            if len(crates[c]) < MIN_PLAYLIST
            and not c.startswith(tuple(g + os.sep for g in FLOOR_EXEMPT))
            and c not in FLOOR_EXEMPT]
    dropped = {c: len(crates.pop(c)) for c in tiny}

    # Crate FOLDERS are gone on purpose. A file lives in exactly one directory,
    # so multi-axis folders mean duplication -- and on a phone (FAT32/MTP) the
    # hardlinks that made them free on Linux become real copies. Playlists
    # reference files instead, so a track can appear in ten of them and still
    # exist once on disk.
    # ---- write playlists ----
    pldir = os.path.join(args.out, "playlists")
    os.makedirs(pldir, exist_ok=True)
    # Playlists are derived entirely from the tags, so the set of them is a
    # rebuild, not an accumulation. A crate that stops existing -- renamed,
    # merged into Other, or produced by a bug -- otherwise leaves a stale .m3u
    # behind that still lists real files and looks perfectly valid on a phone.
    for old in os.listdir(pldir):
        if old.endswith(".m3u"):
            os.remove(os.path.join(pldir, old))
    for crate, members in sorted(crates.items()):
        # A genre can contain the path separator ("R&B / Soul"), and swapping
        # it for " - " then leaves the spaces that were around it: "R&B  -
        # Soul". Collapse whatever the substitution produced.
        name = re.sub(r"\s{2,}", " ", crate.replace(os.sep, " - ")).strip()
        rel = os.path.join(pldir, f"{name}.m3u")
        with open(rel, "w", encoding="utf-8") as fh:
            fh.write("#EXTM3U\n")
            for fn in sorted(members):
                fh.write(f"../_all/{fn}\n")
        if args.absolute:
            with open(os.path.join(pldir, f"{name}.abs.m3u"), "w",
                      encoding="utf-8") as fh:
                fh.write("#EXTM3U\n")
                for fn in sorted(members):
                    fh.write(f"{args.phone_root.rstrip('/')}/_all/{fn}\n")

    total = len(files)
    print(f"\n  {total} tracks in {allpath}")
    n_pl = len(crates) * (2 if args.absolute else 1)
    print(f"  {n_pl} playlists -> {pldir}"
          f"{' (relative + absolute)' if args.absolute else ''}")
    print("  no crate folders: playlists reference files, so nothing duplicates")
    # Say what was pruned. A silent cap reads as "everything is here" when it
    # is not, and the tracks are still in _all either way.
    if dropped:
        print(f"  {len(dropped)} crates below {MIN_PLAYLIST} tracks, not written: "
              + ", ".join(f"{c.replace(os.sep, ' - ')} ({n})"
                          for c, n in sorted(dropped.items())))
    print()
    for crate, members in sorted(crates.items()):
        print(f"    {crate:34} {len(members):4}")
    du = sum(os.path.getsize(os.path.join(allpath, f)) for f in files)
    print(f"\n  _all size {du/1e6:.0f} MB, one copy of every track\n")


if __name__ == "__main__":
    main()
