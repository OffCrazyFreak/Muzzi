#!/usr/bin/env python3
"""Stage 7: write everything into ID3 on copies. Originals are never touched.

Two classes of tag, deliberately treated differently:

  * Audio-derived (BPM, key, Camelot, ReplayGain, quality, danceability) are
    written for EVERY track. They come from the waveform and cannot be wrong
    about which song this is.
  * Identity (artist, title, album, year) is only written when we are confident
    -- above the threshold, or confirmed by lyric verification. Below that the
    file keeps its original name and gets no identity tags, because a wrong
    artist is worse than a missing one.

Also writes a provenance stamp (TXXX:MUZZI_*) so re-ingesting a processed
library is a tag read rather than a re-run, and any file that slipped in
unprocessed is spotted immediately.

Usage: write_tags.py [--min-confidence 0.90] [--out DIR] [--dry-run]
"""
import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata

import mutagen

from mutagen.id3 import (APIC, COMM, TALB, TBPM, TCON, TDRC, TIT2, TKEY,
                         TLAN, TPE1, TPE2, TPE4, TPOS, TPUB, TRCK, TSRC, TXXX,
                         UFID, USLT, WOAS)

from mutagen.mp3 import MP3

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from pipeline.genres import canonical as canonical_genre  # noqa: E402
from pipeline import scenes  # noqa: E402
from pipeline import bpm_overrides  # noqa: E402
from pipeline import artist_names  # noqa: E402
from pipeline import lrc  # noqa: E402
from pipeline import lyric_align  # noqa: E402
from pipeline import silence  # noqa: E402

VERSION = "muzzi/1"
REVIEW = os.path.join(HERE, "cache", "review.json")
ANALYSIS = os.path.join(HERE, "cache", "analysis.json")
VERIFY = os.path.join(HERE, "cache", "lyric_verify.json")
LYRICS = os.path.join(HERE, "cache", "lyrics.json")
ENRICH = os.path.join(HERE, "cache", "enrich.json")
DUPES = os.path.join(HERE, "cache", "duplicates.json")
NAME_DUPES = os.path.join(HERE, "cache", "name_duplicates.json")
CASCADE = os.path.join(HERE, "cache", "cascade.json")
CANON = os.path.join(HERE, "cache", "artist_canon.json")
ART_INDEX = os.path.join(HERE, "cache", "art_index.json")
SILENCE = os.path.join(HERE, "cache", "silence.json")
ALIGN = os.path.join(HERE, "cache", "lyric_align.json")
YT_LINKS = os.path.join(HERE, "cache", "youtube_links.json")

# Re-timing a lyric sheet by less than this is not worth the write: it is
# inside the error of the measurement that produced it.
MIN_LRC_SHIFT = 0.15
# Two recorded trims closer than this are the same trim. Prevents a re-copy
# on every run from float noise in the cached figure.
TRIM_EPSILON = 0.005

# Reference loudness. gain = target - measured.
#
# Deliberately NOT the ReplayGain 2.0 default of -18 LUFS. That figure was
# calibrated in 2001 against pre-loudness-war masters; this library's median
# track is -9.7 LUFS, so -18 attenuates almost everything by 8-9 dB. Namida,
# the player this output is aimed at, applies ReplayGain as a plain volume
# multiplier -- 10^(gain/20), clamped to 1.0 on mobile and 1.3 on desktop --
# so -18 LUFS puts the median track at 35% of the volume slider. -14 LUFS is
# where Spotify, YouTube, TIDAL and Amazon all landed, and puts it at 55%.
RG_TARGET_LUFS = -14.0

# Ceiling for true peak after gain, per EBU R128 and loudgain's -k. A higher
# target boosts more tracks, and lossy decoding already overshoots full scale
# (peaks of 1.2-1.5 are ordinary here), so without this 80 files in this
# library would clip outright.
RG_MAX_TRUE_PEAK_DBTP = -1.0

_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Every ReplayGain-ish tag name a previous build, an upstream tagger or Apple
# might have left behind, matched case-insensitively. These are deleted before
# ours are written: two schemes disagreeing inside one file is worse than
# either alone, and ten files here carry a lowercase replaygain_track_gain
# whose value is the literal string "eplaygain", which a player that reads
# lowercase first will happily parse as garbage.
_RG_PREFIXES = ("REPLAYGAIN_", "MP3GAIN_")
_RG_EXACT = ("ITUNNORM",)


def _is_rg_tag(name):
    n = (name or "").upper().split(":")[-1]
    return n.startswith(_RG_PREFIXES) or n in _RG_EXACT


# A MusicBrainz identifier on a non-MP3 container, in any of the spellings
# taggers use: "MusicBrainz Track Id", MUSICBRAINZ_TRACKID,
# musicbrainz_releasetrackid. Nothing here writes one, so every one of them
# came in on the source file and states which recording this is on evidence
# we never saw.
_MB_ATOM = re.compile(r"musicbrainz[ _]?(?:track|recording|release|album|"
                      r"artist|work|releasegroup|releasetrack)", re.I)


def rg_gain(loudness_lufs, true_peak=None, target=RG_TARGET_LUFS):
    """ReplayGain for one measured loudness, capped so it cannot clip.

    `true_peak` is linear, 1.0 = 0 dBFS, as ReplayGain peak tags are. Without
    it there is nothing to cap against, so the raw gain is returned and the
    caller writes no peak tag -- which is honest: a player then knows it has
    no clipping information rather than being handed a fabricated peak.
    """
    if loudness_lufs is None:
        return None
    gain = target - float(loudness_lufs)
    if true_peak and float(true_peak) > 0:
        headroom = RG_MAX_TRUE_PEAK_DBTP - 20 * math.log10(float(true_peak))
        gain = min(gain, headroom)
    return gain


def album_loudness(tracks):
    """Duration-weighted album loudness, in the energy domain. -> LUFS.

    Averaging LUFS values directly averages logarithms, which by Jensen's
    inequality reads quieter than the album actually is and yields an album
    gain a few tenths too loud -- on a two-track -6/-20 LUFS album, 4.2 dB
    too loud. Proper ReplayGain measures the concatenated album; summing
    energy per second is the same quantity without a second decode.

    `tracks` is [(loudness_lufs, seconds), ...].
    """
    total = sum(w for _, w in tracks) or 1.0
    energy = sum(10 ** (float(l) / 10.0) * float(w) for l, w in tracks) / total
    return 10.0 * math.log10(energy) if energy > 0 else None

# Source folders that should share one output folder.
FOLDER_ALIASES = {
    "Music to download (yt)": "YouTube",
    "YT Music to download": "YouTube",
}

# Genre used to decide the BPM octave here (punk runs ~170 and is heard
# half-time; rap really is ~85). It was removed: a Last.fm "Melodic Death
# Metal" tag on a Serbian rap track doubled it wrongly, and the rule fought the
# octave choice made in analyze.py. Genuine octave errors that survive all
# three engines are now settled per track in config/bpm_overrides.json.


_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_LRC_STAMP = re.compile(r"\[\d+:\d+[.:]\d+\]")
# Below this many distinct words a Whisper transcript is not evidence of a
# language, it is noise that happens to be spelled in one.
MIN_DISTINCT_WORDS = 20

# Same lines the picker draws, imported rather than repeated so the write side
# cannot start disagreeing with the side that chose the entry. They live beside
# fit() in webmatch, which every consumer already imports: reading them from
# lyrics_fetch meant loading the HTTP client to get two floats.
from pipeline.webmatch import MIN_FIT, MAX_DURATION_DELTA  # noqa: E402


def lyrics_trustworthy(entry, verified, artist, title=None):
    """-> (ok, reason). False when these lyrics are probably not this song's.

    Three ways it goes wrong, all seen here:

      * The track has no words. Whisper found under three in a vocal-detected
        excerpt, yet LRCLIB happily returned 4180 characters for an
        instrumental -- so NCS tracks shipped with someone else's lyrics.
      * LRCLIB matched a different artist's song of the same name. Grse's
        "Mamba" got JoelB's, Rasta's "Kawasaki" got a Polish one. If the
        transcript then fails to match, there is nothing left arguing for it.
      * LRCLIB matched a different song by the *right* artist, which is the
        one this used to wave through. The language and the voice are both
        correct, so nothing downstream looks wrong; only the words are. 15
        sidecars here carried another of the artist's songs.

    A mismatch alone is not disqualifying: where a file's own tags named the
    artist "various" or "dj marchez", LRCLIB's answer is the correct one and
    the transcript confirms it. That escape hatch is kept, and now covers a
    title mismatch too, for the same reason -- a file titled with the album
    name is wrong about itself, not about the lyrics.

    An entry that never recorded which hit it took cannot be judged at all.
    Those used to pass silently, because the check was written as "if we know
    the artist and it disagrees". 139 entries here are that shape. Absence of
    evidence is not agreement, so they are untrusted unless the transcript
    speaks for them.
    """
    if not entry or not isinstance(entry, dict):
        return True, None
    if verified and verified.get("instrumental"):
        return False, "instrumental"
    confirmed = bool(verified and verified.get("verdict") == "confirmed")
    matched = entry.get("matched") or ""
    if " - " not in matched:
        if entry.get("synced") or entry.get("plain"):
            return (True, None) if confirmed else (False, "unverifiable_match")
        return True, None
    from pipeline.webmatch import fit
    ma, _, mt = matched.partition(" - ")
    # Prefer the fits the picker already stamped; fall back to computing them
    # so an entry written by an older selector is still judged.
    af = entry.get("artist_fit")
    tf = entry.get("title_fit")
    if artist and af is None:
        af = fit(artist, ma)
    if title and tf is None:
        tf = fit(title, mt)
    if artist and af is not None and af < MIN_FIT:
        return (True, None) if confirmed else (False, "wrong_artist")
    if title and tf is not None and tf < MIN_FIT:
        return (True, None) if confirmed else (False, "wrong_title")
    return True, None


def lyrics_timing_ok(entry, decoded_secs, cut=0.0):
    """-> (ok, reason). False when the sheet was timed for a different edit.

    A lyric sheet is a list of moments. If it was written against a recording
    of a different length, every one of those moments is wrong, and wrong by
    more the further in you get. That is what makes lyrics run ahead of the
    song: the file is longer than the edit the sheet describes.

    Measured here, comparing LRCLIB's duration to the file's own: the median
    sheet is 0.6s out, which is nothing, but 315 of 1178 are more than 2s out
    and 112 are more than 10s out. One was a 108s sheet beside a 289s file.

    2s is the tolerance LRCLIB's own /api/get signature match uses, and what
    LRCLIBee and Music Assistant both require. Borrowing it keeps us from
    inventing a threshold nobody else honours.

    `cut` is subtracted, not added. `decoded_secs` is measured by analyze.py on
    the SOURCE file, which is the untrimmed original, so it already describes
    the length before any silence was removed. The copy we ship is `cut`
    seconds shorter than that, and LRCLIB timed a commercial master with no
    padding at all, so the length to compare against is the source minus what
    we take off the front. Adding it counted the padding twice, and did so on
    exactly the files trimming helps most: measured across the tracks with a
    real trim, 75 sheets passed today and failed once `cut` was added, and 47
    that failed passed once it was subtracted.

    `cut` is 0 on every untrimmed file, where both spellings are identical,
    which is why no aggregate check could see this.
    """
    md = entry.get("matched_duration") if isinstance(entry, dict) else None
    if not md or not decoded_secs:
        return True, None            # nothing to compare: not evidence of wrong
    if abs((decoded_secs - (cut or 0.0)) - md) > MAX_DURATION_DELTA:
        return False, "duration_mismatch"
    return True, None


def resolve_language(verified, lyrics):
    """-> (language, confidence) or (None, None).

    Whisper reports a language for anything, including silence, and reports it
    confidently: ten near-instrumental tracks came back as Khmer at up to 0.84,
    which put an entire "Language - km" playlist on the phone. Its own
    probability cannot filter that, so use better evidence where it exists.

    Fetched lyrics are real text written by a person, so detecting on them
    beats guessing from a garbled transcription -- two Colonia tracks Whisper
    called Khmer are Croatian at 0.9999 by their lyrics.
    """
    text = _LRC_STAMP.sub("", lyrics or "")
    if len(text.strip()) >= 60:
        try:
            from langdetect import detect_langs, DetectorFactory
            DetectorFactory.seed = 0
            best = detect_langs(text)[0]
            if best.prob >= 0.90:
                return best.lang, round(best.prob, 2)
        except Exception:
            pass
    if not verified:
        return None, None
    # Fall back to the transcript, but only where it was corroborated: the
    # lyrics matched (so the words are real) and there are enough of them.
    transcript = verified.get("transcript") or verified.get("text") or ""
    distinct = len(set(_WORD.findall(transcript.lower())))
    if verified.get("verdict") == "confirmed" and distinct >= MIN_DISTINCT_WORDS:
        return verified.get("language"), verified.get("language_prob")
    return None, None


def _muzzi_tag(path, name):
    """-> one MUZZI_* value already written into an output file, or None.

    ID3 and MP4 keep these in different places, and every caller wants the
    string rather than the frame.
    """
    try:
        m = mutagen.File(path)
        t = m.tags if m else None
        if t is None:
            return None
        if hasattr(t, "getall"):
            fr = t.getall(f"TXXX:{name}")
            return str(fr[0].text[0]) if fr and fr[0].text else None
        v = t.get(f"----:com.apple.iTunes:{name}")
        if v:
            return v[0].decode("utf-8", "ignore") if isinstance(v[0], bytes) else str(v[0])
    except Exception:
        pass
    return None


def _source_of(path):
    """-> the source filename this output was written from, if recorded."""
    return _muzzi_tag(path, "MUZZI_SOURCE_FILE")


def _trim_of(path):
    """-> how many seconds were cut off the front of an existing output file.

    Absent means it was written before trimming existed, which is not the same
    as zero: a file with no marker has to be re-made from the source before it
    can be trusted to be untrimmed-on-purpose.
    """
    raw = _muzzi_tag(path, "MUZZI_TRIM")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


# What a container says about itself, which -map_metadata 0 promotes into the
# output's tags. The first three are the MP4/DASH brand box; the rest are FLV
# onMetaData, carried by files that were downloaded as video. None of them
# describes the music, and "width: 320" on an audio file is a wrong field, not
# a spare one. The encoder fields ffmpeg adds (TSSE, TDEN, TENC) are left
# alone: those are true statements about the copy we just made.
_CONTAINER_FIELDS = {
    "major_brand", "minor_version", "compatible_brands",
    "width", "height", "framerate", "videodatarate", "audiodatarate",
    "totaldatarate", "duration", "totalduration", "starttime", "bytelength",
    "canseekontime", "sourcedata",
}


def _clean_remuxed_tags(path):
    """Undo what the container, rather than the music, contributed.

    ffmpeg also rewrites an ID3 comment as TXXX:comment, which loses it: the
    rest of this file reads COMM, and a source comment holding a YouTube URL
    is the only reason 124 files link to their video. Fold it back.
    """
    try:
        f = mutagen.File(path)
        t = f.tags if f else None
        if t is None:
            return
        if hasattr(t, "getall"):
            for fr in list(t.getall("TXXX")):
                if (fr.desc or "").lower() in _CONTAINER_FIELDS:
                    t.delall(f"TXXX:{fr.desc}")
            promoted = [fr for fr in t.getall("TXXX")
                        if (fr.desc or "").lower() == "comment"]
            if promoted and not t.getall("COMM"):
                text = _text_of(promoted[0].text)
                if text:
                    t.add(COMM(encoding=3, lang="eng", desc="", text=[text]))
            for fr in promoted:
                t.delall(f"TXXX:{fr.desc}")
        else:
            for k in [k for k in t.keys()
                      if k.split(":")[-1].lower() in _CONTAINER_FIELDS]:
                t.pop(k, None)
        f.save()
    except Exception:
        # A tag we could not clean is cosmetic; a track we failed to write is
        # not. The copy itself is already on disk and correct.
        pass


def _text_of(value):
    return str(value[0]) if isinstance(value, list) and value else str(value or "")


def copy_audio(src, dst, cut):
    """Put a copy of `src` at `dst`, minus `cut` seconds off the front.

    -c copy drops whole compressed frames instead of decoding, so this is
    lossless: the retained audio was verified sample-identical to the source
    (correlation 1.000000) on both MP3 and AAC, with bitrate, codec and cover
    art unchanged. Frame granularity means the real cut lands within ~26ms
    (MP3) or ~23ms (AAC) of the one asked for, which is invisible against the
    0.2s minimum this is ever used for.

    -> the seconds actually cut. A failure here falls back to a whole copy and
    returns 0.0: a song that is not trimmed is a cosmetic problem, and a song
    that is missing is not.
    """
    if cut <= 0:
        shutil.copy2(src, dst)
        return 0.0
    # Never stage inside the output tree: write_tags --prune deletes anything
    # under out/_all that this run did not intend to be there.
    tmp = os.path.join(tempfile.gettempdir(),
                       f"muzzi-trim-{os.getpid()}{os.path.splitext(src)[1]}")
    try:
        p = subprocess.run(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
             "-ss", f"{cut:.3f}", "-i", src, "-map", "0", "-c", "copy",
             "-map_metadata", "0", "-y", tmp],
            capture_output=True, text=True, timeout=180)
        if p.returncode == 0 and os.path.getsize(tmp) > 0:
            _clean_remuxed_tags(tmp)
            shutil.move(tmp, dst)
            shutil.copystat(src, dst)
            return cut
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    shutil.copy2(src, dst)
    return 0.0


def _song_key(basename):
    """-> a loose 'which song is this' key, ignoring extension and BPM.

    Two builds can disagree on tempo by a beat, so the [131 BPM] suffix has to
    come off before comparing or every re-measured track looks like a new song.
    """
    s = re.sub(r"\s*\[\d+ BPM\]$", "", os.path.splitext(basename)[0])
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def subdir_for(src, common_root):
    """Mirror the source folder layout under the output root.

    The sources are already partly sorted by hand ("Music Mine", "Music Other",
    "NCS Beat", "NCS Chill"), and that grouping is information we did not
    produce and cannot reconstruct. Everything keeps its position relative to
    the deepest directory all sources share, so the three libraries stay
    distinguishable and the hand-made groups survive.
    """
    if not common_root:
        return ""
    rel = os.path.relpath(os.path.dirname(src), common_root)
    if rel in (".", os.pardir) or rel.startswith(os.pardir):
        return ""
    parts = [safe_name(p, "unsorted") for p in rel.split(os.sep)]
    # Both YouTube source folders hold the same kind of thing -- one is the
    # earlier hand-grabbed batch, one the Namida playlist download -- so they
    # land in a single folder rather than an arbitrary split by when they
    # happened to be fetched.
    if parts and parts[0] in FOLDER_ALIASES:
        parts[0] = FOLDER_ALIASES[parts[0]]
    return os.path.join(*parts)


def safe_name(s, fallback="Unknown"):
    s = unicodedata.normalize("NFC", (s or "").strip())
    s = _BAD.sub("_", s)
    # Collapse runs of whitespace left behind by removing an illegal
    # character, and any stray space before the extension or a bracket.
    s = re.sub(r"\s{2,}", " ", s)
    s = re.sub(r"\s+([)\]])", r"\1", s)
    s = re.sub(r"([(\[])\s+", r"\1", s).strip(" .")
    return s[:120] or fallback


# Featured artists arrive either joined into the artist field ("Ed Sheeran;
# Bring Me the Horizon", 37 of 41 cases here) or already inside the title.
#
# The split itself lives in artist_names.split_credit, because the alias rules
# have to be keyed on the same split this uses. It covers ";", "&", "x" and
# ",", the feature markers ("Buba Corelli Ft. Jala Brat & Coby" carries the
# marker in the artist field, and splitting only on "&" left a lead artist
# called "Buba Corelli Ft. Jala Brat"), and lower-case Croatian/Serbian "i"
# for "and" ("Ivana Selakov i Aca Lukas" is two artists; an upper-case "I" is
# far likelier to be part of a name).
#
# The marker has to start a word. Without the leading boundary the prefix is
# entirely optional, so the match can begin at the final "ft" of an ordinary
# word: "Defeat The Night" became "De (ft. The Night)", "Thrift Shop" became
# "Thri (ft. Shop)", and the invented artist reached TPE1, where it groups and
# searches like a real one. Gift, Soft, Left and Minecraft do the same.
_FEAT_IN_TITLE = re.compile(
    r"(?:^|[\s\(\[])\s*[\(\[]?\s*(?:feat\.?|ft\.?|featuring)\s+"
    r"([^)\]]+?)\s*[\)\]]?\s*$", re.I)


def _feat_and_tail(captured, matched):
    """-> (the names, whatever the credit ran into and should not have).

    A feature marker runs to the end of the title, so "Heroes ft. Tove Lo
    (Bvrnout Remix)" hands the whole tail to the credit and the artist becomes
    "Tove Lo (Bvrnout Remix", a name with an opening bracket and no closing
    one. The capture cannot contain a closing bracket, so any opening bracket
    in it is one it never closed: cut there, and give the group back to the
    title, where a remix note belongs.
    """
    opened = next((i for i, ch in enumerate(captured) if ch in "(["), None)
    if opened is None:
        return captured, ""
    tail = captured[opened:]
    if matched.rstrip().endswith((")", "]")):
        tail += ")" if captured[opened] == "(" else "]"
    return captured[:opened], tail


def split_credits(artist, title):
    """-> (lead, [featured], title_without_feat).

    Option B layout: lead artist owns the artist slot, everyone else moves
    into the title as "(ft. ...)" -- the convention streaming services use.
    """
    feats = []
    m = _FEAT_IN_TITLE.search(title or "")
    if m:
        names, tail = _feat_and_tail(m.group(1), m.group(0))
        feats += artist_names.split_credit(names)
        title = _FEAT_IN_TITLE.sub("", title).strip()
        if tail:
            title = f"{title} {tail}".strip()
    parts = artist_names.split_credit(artist)
    lead = parts[0] if parts else artist
    for p in parts[1:]:
        if p.lower() not in {f.lower() for f in feats}:
            feats.append(p)
    # One credit per person. "Cher Lloyd ft. Cher Lloyd" and two spellings of
    # one name both reached the title, because the title's own list was never
    # deduplicated and the lead was only compared against it afterwards.
    seen, unique = {lead.lower()} if lead else set(), []
    for f in feats:
        if f.lower() in seen:
            continue
        seen.add(f.lower())
        unique.append(f)
    return lead, unique, title


def compose_title(title, feats, bpm=None):
    """Merge into existing parentheses rather than stacking them:
    "OK (extended version) (ft. X)" reads badly; "(extended version, ft. X)"
    is what the convention actually is."""
    out = title
    if feats:
        tail = "ft. " + ", ".join(feats)
        m = re.search(r"\(([^()]*)\)\s*$", out)
        if m:
            out = out[:m.start()] + f"({m.group(1)}, {tail})"
        else:
            out = f"{out} ({tail})"
    if bpm:
        # Samsung Music has no BPM field at all, so the only way to see tempo
        # in that app is to put it somewhere it already displays.
        out = f"{out} [{int(round(bpm))} BPM]"
    return out


_URL = re.compile(r"https?://\S+")
_YT_IN_TEXT = re.compile(r"youtu\.be/|youtube\.com/")


def existing_comment(path):
    """-> the comment already on the file, for either container, or ""."""
    try:
        f = mutagen.File(path)
        t = f.tags if f else None
        if t is None:
            return ""
        if hasattr(t, "getall"):
            fr = t.getall("COMM")
            return str(fr[0].text[0]) if fr and fr[0].text else ""
        v = t.get("\xa9cmt") or t.get("comment")
        return str(v[0]) if v else ""
    except Exception:
        return ""


def previous_youtube(path):
    """-> (trust, prior_comment) this build's predecessor wrote, for m4a/FLAC."""
    try:
        f = mutagen.File(path)
        t = f.tags if f else None
        if t is None or hasattr(t, "getall"):
            return None, None

        def free(name):
            v = t.get(f"----:com.apple.iTunes:{name}") or t.get(name.lower())
            if not v:
                return None
            v = v[0]
            return v.decode("utf-8", "ignore") if isinstance(v, bytes) else str(v)
        return free("YOUTUBE_TRUST"), free("PRIOR_COMMENT")
    except Exception:
        return None, None


def digest(fields):
    """Short digest of the identity we wrote, for tamper/mismatch detection."""
    blob = "|".join(str(fields.get(k) or "") for k in
                    ("artist", "title", "album", "year", "bpm", "key"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def write_generic(dst, fields, art_path, lyrics):
    """FLAC / M4A / OGG. ID3 frames do not exist there, so map onto the
    container's own scheme. Roughly 5% of a mixed library is not MP3, and
    without this those files silently produce nothing."""
    import mutagen
    from mutagen.flac import FLAC, Picture
    from mutagen.mp4 import MP4, MP4Cover

    f = mutagen.File(dst)
    if f is None:
        raise ValueError("unrecognised audio container")

    if isinstance(f, MP4):
        # Same reasoning as the MP3 path: drop every capitalisation of any
        # ReplayGain atom already present, plus Apple's own Sound Check, before
        # writing ours. The loop below only overwrites the names it produces.
        for k in [k for k in f.keys() if _is_rg_tag(k)]:
            f.pop(k, None)
        # "comment" must map to the native atom. Left to the freeform loop
        # below it becomes ----:com.apple.iTunes:COMMENT, which is not the
        # field any player reads -- and reading a URL out of the comment is how
        # Namida links a local track to its video.
        m = {"title": "\xa9nam", "artist": "\xa9ART", "albumartist": "aART",
             "album": "\xa9alb", "date": "\xa9day", "genre": "\xa9gen",
             "bpm": "tmpo", "comment": "\xa9cmt"}
        for k, v in fields.items():
            if k not in m:
                continue
            if v in (None, ""):
                # Remove rather than skip: this writes over the previous
                # build, so a value we no longer stand behind survives unless
                # it is deleted -- which is how a stale genre outlived the
                # rule that stopped producing it.
                f.pop(m[k], None)
                continue
            f[m[k]] = [int(v)] if k == "bpm" else [str(v)]
        # Earlier builds had no "comment" in the map, so the freeform loop
        # below wrote ----:com.apple.iTunes:COMMENT. That loop now skips
        # "comment", and it only pops an atom whose key is in `fields`, so the
        # stale one would survive every later build and the file would carry
        # two comment fields free to disagree.
        f.pop("----:com.apple.iTunes:COMMENT", None)
        # A MusicBrainz ID we did not write is a claim about which recording
        # this is, made by whoever tagged the source. The MP3 path deletes its
        # UFID frame before writing; nothing here writes these atoms at all,
        # so without this a source's ID outlived every correction, including a
        # hint that replaced the song outright.
        for k in [k for k in f.keys() if _MB_ATOM.search(k)]:
            f.pop(k, None)
        # The track number goes in the native atom, which is what players and
        # tools/tagdump.py read. The freeform loop below would have written
        # ----:com.apple.iTunes:TRACKNUMBER, which almost nothing reads, and
        # left an inherited trkn beside it free to disagree. The MP3 path
        # writes TRCK and deletes it when unknown; this is the same rule.
        # disk always goes: nothing here knows a disc number, so any that is
        # here came in with the file.
        f.pop("disk", None)
        f.pop("trkn", None)
        f.pop("----:com.apple.iTunes:TRACKNUMBER", None)
        if fields.get("tracknumber"):
            try:
                f["trkn"] = [(int(fields["tracknumber"]),
                              int(fields.get("total_tracks") or 0))]
            except (TypeError, ValueError):
                pass
        for k, v in fields.items():
            if k in m or k in ("tracknumber", "total_tracks"):
                continue
            atom = f"----:com.apple.iTunes:{k.upper()}"
            if v in (None, ""):
                # Same reason the mapped loop above pops rather than skips:
                # this writes over the previous build, so a value we no longer
                # stand behind survives unless it is deleted. Skipping made
                # every freeform atom a one-way door.
                f.pop(atom, None)
                continue
            f[atom] = [str(v).encode()]
        if art_path and os.path.exists(art_path):
            with open(art_path, "rb") as fh:
                f["covr"] = [MP4Cover(fh.read(), imageformat=MP4Cover.FORMAT_JPEG)]
        if lyrics:
            f["\xa9lyr"] = [lyrics]
    else:
        # FLAC / OGG: Vorbis comments are free-form uppercase keys.
        for k, v in fields.items():
            key = k.upper()
            if v in (None, ""):
                # Remove rather than skip, for the same reason the MP4 branch
                # above pops: this writes over the previous build, so a genre
                # we no longer stand behind would otherwise outlive the rule
                # that stopped producing it.
                f.pop(key, None)
            else:
                f[key] = str(v)
        # Same reason as the MP4 branch: an ID nothing here writes is a claim
        # about which recording this is that we cannot stand behind.
        for k in [k for k in f.keys() if _MB_ATOM.search(k)]:
            f.pop(k, None)
        if lyrics:
            f["LYRICS"] = lyrics
        if art_path and os.path.exists(art_path) and isinstance(f, FLAC):
            pic = Picture()
            pic.type, pic.mime = 3, "image/jpeg"
            with open(art_path, "rb") as fh:
                pic.data = fh.read()
            f.clear_pictures()
            f.add_picture(pic)
    f.save()
    return "written"


def write_one(src, dst, ident, audio, verified, lyrics, extra, dry=False,
              cut=0.0, lrc_shift=0.0):
    """-> the seconds actually cut off the front of the output copy."""
    if dry:
        return 0.0
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    # The copy is normally made once and then re-tagged in place on later
    # runs. It also has to be re-made when the trim it carries is not the trim
    # it should carry -- which is what brings a library built before this
    # existed up to date, and what applies a changed threshold later. The
    # figure is always measured on the untrimmed source, so this converges
    # instead of eating the song a slice at a time.
    trimmed = 0.0
    if os.path.exists(dst):
        have = _trim_of(dst)
        if have is not None and abs(have - cut) <= TRIM_EPSILON:
            # Already the copy we want. Keep its real figure: re-stamping it
            # as 0.000 here would make the next run believe it was untrimmed
            # and re-make it, every run, forever.
            trimmed = have
        elif os.path.exists(src):
            os.remove(dst)
        else:
            # Wrong or unknown trim, but nothing to re-make it from. Report
            # what the file actually carries rather than what was wanted.
            trimmed = have or 0.0
    if not os.path.exists(dst):
        trimmed = copy_audio(src, dst, cut)

    if not dst.lower().endswith(".mp3"):
        primary, _ = canonical_genre(extra.get("genres"))
        if ident:
            chosen, _ = scenes.genre_for(
                ident.get("lead_artist") or ident.get("artist"),
                ident.get("title"), ident.get("year"),
                extra.get("discogs_styles"), primary)
            # Not "chosen or primary". The MP3 path clears the genre when the
            # ranking declines to pick one, and falling back here instead left
            # the same track tagged Pop as m4a and untagged as mp3. Both
            # containers get one answer or none.
            primary = chosen
        peak = audio.get("true_peak")
        gain = rg_gain(audio.get("loudness_lufs"), peak)
        yt = extra.get("youtube") or {}
        generic = {
            "title": (ident or {}).get("display_title"),
            "artist": "; ".join(ident["all_artists"]) if ident else None,
            "albumartist": (ident or {}).get("lead_artist"),
            "isrc": (ident or {}).get("isrc"),
            "label": (ident or {}).get("label"),
            "tracknumber": (ident or {}).get("track_number"),
            # Only ever read together with the one above, to fill the native
            # trkn atom's second slot. The MP3 path writes the same pair into
            # TRCK as "4/12".
            "total_tracks": (ident or {}).get("total_tracks"),
            "album": (ident or {}).get("album"),
            "date": (ident or {}).get("year"),
            "genre": primary,
            "bpm": int(round(audio["bpm"])) if audio.get("bpm") else None,
            "initialkey": audio.get("camelot"),
            "camelot": audio.get("camelot"),
            "replaygain_track_gain": f"{gain:.2f} dB" if gain is not None else None,
            "replaygain_track_peak": f"{float(peak):.6f}" if peak else None,
            # Album gain and peak used to be MP3-only: this branch returns
            # before the album block further down ever runs, so all 250 m4a
            # files carried no album levels at all, and a player set to album
            # mode fell back to nothing on a quarter of the library.
            "replaygain_album_gain": (f"{extra['album_gain']:.2f} dB"
                                      if extra.get("album_gain") is not None
                                      else None),
            "replaygain_album_peak": (f"{float(extra['album_peak']):.6f}"
                                      if extra.get("album_peak") else None),
            "replaygain_reference_loudness": (f"{RG_TARGET_LUFS:.2f} LUFS"
                                              if gain is not None else None),
            # The same provenance and audio facts the MP3 path writes. Without
            # them a quarter of the library (the m4a quarter) carried no
            # QUALITY and no MUZZI_SOURCE, so it could never appear in the
            # "Check quality" or "Needs identification" playlists -- which are
            # built by reading exactly those two tags -- and verify.py could
            # not tell a deliberately identity-less file from a failed one.
            "bpm_precise": audio.get("bpm"),
            "danceability": audio.get("danceability"),
            "quality": audio.get("quality_grade"),
            "spectral_cutoff_hz": audio.get("spectral_cutoff_hz"),
            "loudness_lufs": audio.get("loudness_lufs"),
            "language": resolve_language(verified, lyrics)[0],
            "muzzi_version": VERSION,
            "muzzi_confidence": (ident or {}).get("confidence") or 0,
            "muzzi_source": (ident or {}).get("source") or "audio-only",
            "muzzi_source_file": os.path.basename(src),
            "muzzi_lyric_verdict": (verified or {}).get("verdict"),
            "muzzi_lyric_score": (verified or {}).get("score"),
            "muzzi_lyric_withheld": extra.get("lyric_reason"),
            # Always written, including as "0.000", so that a file with no
            # marker at all can be told from one deliberately left whole.
            "muzzi_trim": f"{trimmed:.3f}",
            "muzzi_lrc_shift": (f"{lrc_shift:.2f}" if lrc_shift else None),
            "youtube_id": yt.get("video_id"),
            "youtube_trust": yt.get("trust"),
            "youtube_from": yt.get("from"),
        }
        # The comment only when the link says where this audio came from. A
        # reference link is a video OF the song, not the source of these bytes,
        # and the comment field is the one a player acts on -- Namida links a
        # local track to whatever URL it finds there. The key is omitted rather
        # than set empty, because write_generic deletes a mapped atom whose
        # value is empty and that would wipe comments it was never asked about.
        prev_trust, prior_saved = previous_youtube(dst)
        if yt.get("trust") == "origin":
            prior = existing_comment(dst)
            if prior and not _YT_IN_TEXT.search(prior) and _URL.search(prior):
                generic["prior_comment"] = prior
            generic["comment"] = yt["url"]
        elif prev_trust == "origin":
            # Ours to remove, and only ours: see the ID3 path for why a comment
            # that arrived on the source file is never touched.
            generic["comment"] = prior_saved or ""
            generic["prior_comment"] = ""
        write_generic(dst, generic, extra.get("art_path"), lyrics)
        return trimmed

    mp3 = MP3(dst)
    if mp3.tags is None:
        mp3.add_tags()
    t = mp3.tags

    def txxx(desc, value):
        if value is None or value == "":
            return
        t.setall(f"TXXX:{desc}", [TXXX(encoding=3, desc=desc, text=[str(value)])])

    # ---- identity (only when trusted) ----
    if ident:
        t.setall("TIT2", [TIT2(encoding=3, text=[ident["display_title"]])])
        # One joined string, NOT a multi-value frame: Samsung Music shows only
        # the first value of a multi-value artist frame, which would hide the
        # collaborator and break "find every Eminem track" searching.
        t.setall("TPE1", [TPE1(encoding=3, text=["; ".join(ident["all_artists"])])])
        # Machine-readable list for players that understand it.
        txxx("ARTISTS", "\u0000".join(ident["all_artists"])
             if len(ident["all_artists"]) > 1 else None)
        # Every identity frame below is written when we know the fact and
        # DELETED when we do not. Skipping instead of deleting left the
        # source's own album, year or MusicBrainz ID in place, and a hint that
        # changed the song could not take them back out, which is how a file
        # ended up carrying one song's name and another song's recording ID.
        if ident.get("album"):
            t.setall("TALB", [TALB(encoding=3, text=[ident["album"]])])
            # Album artist is the lead only, so albums group correctly.
            t.setall("TPE2", [TPE2(encoding=3, text=[ident["lead_artist"]])])
        else:
            # TPE2 is deliberately left alone: it is how players group, it is
            # the lead artist we write to TPE1 anyway, and it is not one of
            # the facts a rejected match hands down.
            t.delall("TALB")
        if ident.get("year"):
            t.setall("TDRC", [TDRC(encoding=3, text=[str(ident["year"])])])
        else:
            t.delall("TDRC")
        t.delall("UFID:http://musicbrainz.org")
        if ident.get("recording_id"):
            t.add(UFID(owner="http://musicbrainz.org",
                       data=str(ident["recording_id"]).encode()))
        # Remix credit belongs in TPE4 ("interpreted/remixed by"), which is the
        # standard frame players understand, not buried in the title.
        if ident.get("remixer"):
            t.setall("TPE4", [TPE4(encoding=3, text=[ident["remixer"]])])
        else:
            t.delall("TPE4")
        # Facts the cascade resolved. ISRC is the useful one: it identifies
        # this exact recording worldwide, so a future run can ask any service
        # an exact question instead of guessing from a name.
        if ident.get("isrc"):
            t.setall("TSRC", [TSRC(encoding=3, text=[ident["isrc"]])])
        else:
            t.delall("TSRC")
        if ident.get("label"):
            t.setall("TPUB", [TPUB(encoding=3, text=[ident["label"]])])
        else:
            t.delall("TPUB")
        if ident.get("track_number"):
            total = ident.get("total_tracks")
            t.setall("TRCK", [TRCK(encoding=3, text=[
                f'{ident["track_number"]}/{total}' if total
                else str(ident["track_number"])])])
        else:
            t.delall("TRCK")
        if ident.get("disc_number"):
            t.setall("TPOS", [TPOS(encoding=3, text=[str(ident["disc_number"])])])
        else:
            t.delall("TPOS")

    # ---- audio-derived (always) ----
    if audio.get("bpm"):
        bpm = float(audio["bpm"])
        t.setall("TBPM", [TBPM(encoding=3, text=[str(int(round(bpm)))])])
        txxx("BPM_PRECISE", bpm)
        txxx("BPM_VERDICT", audio.get("bpm_verdict"))
        txxx("BPM_VOTES", audio.get("bpm_votes"))
        # "disagree" is not an octave split -- the engines found genuinely
        # different tempos. Neither value is trustworthy without a listen.
        if audio.get("bpm_verdict") == "disagree":
            txxx("BPM_UNRELIABLE", "engines disagree: "
                 f"{audio.get('bpm_rhythm')}/{audio.get('bpm_degara')}/"
                 f"{audio.get('bpm_percival')}")
        # A hand-checked correction from config/bpm_overrides.json. The measured
        # value stays in the file, so a wrong correction can be undone without
        # re-analysing the audio.
        if audio.get("bpm_override"):
            txxx("BPM_OVERRIDE", audio["bpm_override"])
            txxx("BPM_OVERRIDE_REASON", audio.get("bpm_override_reason"))
            if audio.get("bpm_measured"):
                txxx("BPM_MEASURED", audio["bpm_measured"])
                txxx("BPM_VERIFIED",
                     "reference" if audio.get("bpm_override_verified") else "genre")
        # Cross-engine agreement cannot catch a half-time reading, because all
        # three engines halve together (Basket Case reads 88, not ~170). 70-100
        # is exactly where that is plausible, so publish the alternative and
        # flag it rather than silently picking one. An override has already
        # settled the question, so it suppresses the flag.
        if 70 <= bpm < 100 and not audio.get("bpm_override"):
            txxx("BPM_ALT", round(bpm * 2, 1))
            txxx("BPM_AMBIGUOUS", "half-or-double")
        elif bpm >= 160 and not audio.get("bpm_override"):
            txxx("BPM_ALT", round(bpm / 2, 1))
    if audio.get("key"):
        scale = (audio.get("scale") or "")[:3]
        t.setall("TKEY", [TKEY(encoding=3, text=[f"{audio['key']}{'m' if scale=='min' else ''}"])])
    txxx("CAMELOT", audio.get("camelot"))
    txxx("KEY_AGREEMENT", audio.get("key_agreement"))
    txxx("KEY_STRENGTH", audio.get("key_strength"))
    txxx("DANCEABILITY", audio.get("danceability"))
    txxx("QUALITY", audio.get("quality_grade"))
    if audio.get("truncated"):
        txxx("TRUNCATED", f"decoded {audio.get('decoded_secs')}s of "
                          f"{audio.get('header_secs')}s")
    txxx("SPECTRAL_CUTOFF_HZ", audio.get("spectral_cutoff_hz"))
    txxx("DYNAMIC_COMPLEXITY", audio.get("dynamic_complexity"))
    # Unconditionally, before the write. txxx() overwrites but never deletes,
    # and dst is not re-copied when it already exists, so anything a previous
    # build or an upstream tagger wrote outlives us unless it is removed by
    # name. RVA2 goes too: it is the other ID3 way to say the same thing, and
    # mp3gain and Quod Libet read it in preference to TXXX.
    #
    # Outside the loudness test on purpose. A file we cannot measure is
    # exactly the file most likely to be carrying somebody else's ReplayGain,
    # and gating the deletion on having a replacement left every one of them
    # with a foreign gain and no way to withdraw it.
    for frame in list(t.getall("TXXX")):
        if _is_rg_tag(frame.desc):
            t.delall(f"TXXX:{frame.desc}")
    t.delall("RVA2")
    # Apple writes Sound Check as a COMM frame, not TXXX, so the loop above
    # walks straight past it -- 23 files here carried one and kept it.
    # Leaving it means two normalisation schemes in one file disagreeing.
    for frame in list(t.getall("COMM")):
        if _is_rg_tag(frame.desc):
            t.delall(frame.HashKey)

    if audio.get("loudness_lufs") is not None:
        peak = audio.get("true_peak")
        gain = rg_gain(audio["loudness_lufs"], peak)
        txxx("REPLAYGAIN_TRACK_GAIN", f"{gain:.2f} dB")
        if peak:
            txxx("REPLAYGAIN_TRACK_PEAK", f"{float(peak):.6f}")
        # Nothing in the file recorded which target produced the gain, so a
        # later retag could not tell -14 LUFS values from -18 LUFS ones.
        txxx("REPLAYGAIN_REFERENCE_LOUDNESS", f"{RG_TARGET_LUFS:.2f} LUFS")
        txxx("LOUDNESS_LUFS", audio["loudness_lufs"])
        # Album gain keeps relative levels WITHIN an album intact, so a quiet
        # interlude stays quiet instead of being pushed up to match the singles.
        # Track gain alone flattens that out.
        if extra.get("album_gain") is not None:
            txxx("REPLAYGAIN_ALBUM_GAIN", f"{extra['album_gain']:.2f} dB")
            if extra.get("album_peak"):
                txxx("REPLAYGAIN_ALBUM_PEAK",
                     f"{float(extra['album_peak']):.6f}")

    # ---- language ----
    lang, lang_conf = resolve_language(verified, lyrics)
    if lang:
        t.setall("TLAN", [TLAN(encoding=3, text=[lang])])
        txxx("LANGUAGE_CONFIDENCE", lang_conf)

    # ---- genre + embedded art ----
    # ONE canonical genre. Writing three joined tags produced 113 distinct
    # genre strings across 148 files, which makes a phone genre list useless.
    primary, cleaned = canonical_genre(
        extra.get("genres") or (ident or {}).get("genres"),
        (ident or {}).get("title"))
    # scenes.py knows things no catalogue does -- that Grse and z++ are one
    # scene, that Prljavo kazaliste are two -- so it outranks the Deezer genre
    # it was given as a floor.
    if ident:
        chosen, _why = scenes.genre_for(
            ident.get("lead_artist") or ident.get("artist"),
            ident.get("title"), ident.get("year"),
            extra.get("discogs_styles"), primary)
        extra_scenes = scenes.scenes_for(
            ident.get("lead_artist") or ident.get("artist"), ident.get("year"))
        if chosen:
            # ID3v2.4 allows several values in one TCON. Players that show a
            # single genre take the first, which is why the primary leads;
            # players that show all of them get both scenes, which is what
            # "in both" actually needs.
            values = list(dict.fromkeys([chosen] + extra_scenes))
            t.setall("TCON", [TCON(encoding=3, text=values)])
            primary = chosen
        else:
            # Clear rather than leave: the output is written in place over the
            # previous build, so a genre we no longer stand behind survives
            # forever unless it is removed. That is how "Glee" and "Brcko"
            # outlived the rule that stopped producing them.
            t.delall("TCON")
    elif primary:
        t.setall("TCON", [TCON(encoding=3, text=[primary])])
    if cleaned:
        txxx("GENRES_ALL", "; ".join(cleaned))
    # enrich.py's art first, then the cover the cascade resolved.
    art = extra.get("art_path") or (ident or {}).get("art_path")
    if art and os.path.exists(art):
        t.delall("APIC")
        with open(art, "rb") as fh:
            t.add(APIC(encoding=3, mime="image/jpeg", type=3,
                       desc="Cover", data=fh.read()))

    if lyrics:
        # Embed the LRC-timestamped body when we have it: players that support
        # synced lyrics parse the timestamps out of USLT and highlight each
        # line. Plain text there is what produced a flat, unscrolling list.
        t.delall("USLT")
        t.add(USLT(encoding=3, lang="und", desc="", text=lyrics))

    # ---- where this audio came from ----
    # What the previous build claimed, read before it is cleared. A comment is
    # only ours to remove if we are the ones who wrote it: a URL that came in
    # on the source file is that file's own provenance and must survive
    # untouched, including when this stage has no opinion at all.
    was = t.getall("TXXX:YOUTUBE_TRUST")
    prev_trust = str(was[0].text[0]) if was and was[0].text else None
    # Cleared first, because txxx() no-ops on an empty value: a link we no
    # longer stand behind would otherwise survive every later build, the same
    # way a withdrawn genre outlived the rule that stopped producing it.
    for frame in ("YOUTUBE_ID", "YOUTUBE_TRUST", "YOUTUBE_FROM"):
        t.delall(f"TXXX:{frame}")
    yt = extra.get("youtube") or {}
    if yt.get("video_id"):
        txxx("YOUTUBE_ID", yt["video_id"])
        txxx("YOUTUBE_TRUST", yt.get("trust"))
        txxx("YOUTUBE_FROM", yt.get("from"))
    # Only origin evidence earns the comment field. Namida treats any URL it
    # finds there as this track's video, so a search result -- which is a video
    # OF the song rather than the source of these bytes -- must not go in it.
    if yt.get("trust") == "origin":
        prior = " ".join(str(c.text[0]) for c in t.getall("COMM")
                         if getattr(c, "text", None))
        # A comment that holds some other link is somebody's provenance note
        # (two files here point at SoundCloud). Keep it rather than overwrite
        # it; the 145 others say things like "converted by convert2mp3.net".
        if prior and not _YT_IN_TEXT.search(prior) and _URL.search(prior):
            txxx("PRIOR_COMMENT", prior)
        t.delall("COMM")
        t.add(COMM(encoding=3, lang="eng", desc="", text=[yt["url"]]))
        # The standards-correct home for the same fact. Nothing reads it today,
        # but it costs one frame and it is what WOAS is for.
        t.delall("WOAS")
        t.add(WOAS(url=yt["url"]))
    elif prev_trust == "origin":
        # An earlier build wrote that comment and this one no longer stands
        # behind it. Take it back out, and put back whatever it displaced.
        restore = t.getall("TXXX:PRIOR_COMMENT")
        t.delall("COMM")
        t.delall("WOAS")
        t.delall("TXXX:PRIOR_COMMENT")
        if restore and restore[0].text:
            t.add(COMM(encoding=3, lang="eng", desc="",
                       text=[str(restore[0].text[0])]))

    # ---- provenance ----
    fields = {**(ident or {}), "bpm": audio.get("bpm"), "key": audio.get("camelot")}
    txxx("MUZZI_VERSION", VERSION)
    txxx("MUZZI_DIGEST", digest(fields))
    txxx("MUZZI_CONFIDENCE", ident.get("confidence") if ident else 0)
    txxx("MUZZI_SOURCE", ident.get("source") if ident else "audio-only")
    # Which file this was made from. It is what lets --prune tell a rename
    # ("Amazing Lyrics - idfc x soap" becoming "Blackbear - idfc x soap") from
    # a song that has genuinely disappeared, without loosening the check that
    # stops the last copy of anything being deleted.
    txxx("MUZZI_SOURCE_FILE", os.path.basename(src))
    # Always written, including as "0.000": a file with no marker at all is one
    # written before trimming existed, and write_one has to be able to tell
    # that apart from a file deliberately left whole.
    txxx("MUZZI_TRIM", f"{trimmed:.3f}")
    if lrc_shift:
        txxx("MUZZI_LRC_SHIFT", f"{lrc_shift:.2f}")
    if verified and verified.get("verdict"):
        txxx("MUZZI_LYRIC_VERDICT", verified["verdict"])
        txxx("MUZZI_LYRIC_SCORE", verified.get("score"))
    # Why a track has no lyrics, or no timings. Without this, "LRCLIB has
    # nothing for this song" and "we refused what LRCLIB had" look identical
    # from the outside, and the coverage figures cannot be read back.
    #
    # Cleared first: txxx() returns early on an empty value, so a track that
    # was withheld on an earlier build and passes now would keep the old
    # marker and read as still-refused.
    t.delall("TXXX:MUZZI_LYRIC_WITHHELD")
    if extra.get("lyric_reason"):
        txxx("MUZZI_LYRIC_WITHHELD", extra["lyric_reason"])

    # ID3v2.4: Samsung's own guidance says v2.4 is reliably supported while
    # v2.3 "may encounter display issues, such as incorrect character
    # rendering" -- which matters for c/s/z with diacritics. v2.4 also carries
    # UTF-8 natively instead of needing UTF-16.
    mp3.save(v2_version=4)
    return trimmed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-confidence", type=float, default=0.90)
    ap.add_argument("--out", default=os.path.join(HERE, "out", "_all"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep-duplicates", action="store_true",
                    help="write every file, including copies dedupe "
                         "judged worse")
    ap.add_argument("--flat", action="store_true",
                    help="put everything in one directory instead of "
                         "mirroring the source folder layout")
    ap.add_argument("--no-lrc", action="store_true",
                    help="skip writing .lrc sidecar files")
    # Tags travel inside the file, so this is never required. It exists because
    # a filename is the one field EVERY player shows, including ones that
    # ignore TBPM entirely (Samsung Music being the obvious example).
    # On by default: Samsung Music has no BPM field and shows the title tag,
    # so this is the only place tempo can appear in that app.
    ap.add_argument("--no-bpm-in-title", dest="bpm_in_title",
                    action="store_false", default=True,
                    help="do not append [131] to titles/filenames")
    # The output folder accumulates. A file written by an earlier build stays
    # there forever, even once its source has become a duplicate loser or been
    # renamed by a better identification -- so out/_all silently grew a second
    # copy of 59 songs the moment the re-downloads won their groups. Nothing
    # here is irreplaceable: everything in out/ is derived from the sources.
    ap.add_argument("--prune", action="store_true",
                    help="delete output files this run did not write, i.e. "
                         "leftovers from an earlier build")
    ap.add_argument("--name-template", default="{artist} - {title}",
                    help="placeholders: {artist} {title} {album} {year} {bpm} "
                         "{camelot} {key} {quality} {lang}. "
                         'e.g. "{bpm} - {artist} - {title}"')
    # Rebuild a subset into a scratch output, so a change can be verified
    # against a sample without touching the real library or waiting for 1700
    # files. The filter is applied late, after album gain, identity
    # inheritance and the common root have all been computed from the full
    # set: applied early it would quietly produce different tags and a
    # different folder layout than a full build, and the diff against that
    # build would then be noise.
    ap.add_argument("--only", metavar="LIST",
                    help="write only the tracks listed in this file, one "
                         "source path or fingerprint per line")
    args = ap.parse_args()

    # --prune deletes everything this run did not write, and under --only that
    # is the rest of the library. Refuse rather than warn.
    if args.only and args.prune:
        sys.exit("--only with --prune would delete every file outside the "
                 "subset. Prune from a full build instead.")

    rows = json.load(open(REVIEW))
    analysis = {v["path"]: v for v in json.load(open(ANALYSIS)).values()
                if v.get("path")}

    # One copy of each song in the output, and it is the better copy. dedupe
    # clusters by fingerprint and picks the winner on measured spectral cutoff,
    # so the loser of every pair is dropped here rather than written twice.
    # Measured on this library: the old MP3 rips beat the 128kbps YouTube AAC
    # 315 times out of 322, so this is not simply "prefer the newest file".
    # Fingerprinting only sees the same recording twice. A lyric video and an
    # official audio of one song are different recordings of the same master,
    # so their fingerprints differ and dedupe.py cannot pair them -- that is
    # what dedupe_names.py catches, once identification has given both files
    # the same artist and title.
    losers = set()
    if not args.keep_duplicates:
        if os.path.exists(DUPES):
            for g in json.load(open(DUPES)):
                losers.update(g.get("drop") or [])
        if os.path.exists(NAME_DUPES):
            losers.update(json.load(open(NAME_DUPES)).get("losers") or {})

    # The two dedupe stages rank on the same measure, so they normally agree on
    # which copy to keep. When they are fed different data they can disagree --
    # and because this is a union of both loser sets, a file dropped by one and
    # a file dropped by the other leaves the song with no copy at all. It is
    # silent: the song simply stops appearing in the output. Rescue the best
    # member instead, and say so, because the real fault is upstream.
    if losers and os.path.exists(DUPES):
        rescued = 0
        # Where a copy went, according to the name pass. A fingerprint group
        # can be wiped out legitimately: all three copies of "Dean Lewis - Be
        # Alright" lose to a fourth file in another folder. That song is not
        # missing, so rescuing here would just write it twice.
        went_to = {p: v["duplicate_of"] for p, v in
                   (json.load(open(NAME_DUPES)).get("losers") or {}).items()} \
            if os.path.exists(NAME_DUPES) else {}
        for g in json.load(open(DUPES)):
            members = [g["keep"]] + (g.get("drop") or [])
            if any(went_to.get(m) and went_to[m] not in losers for m in members):
                continue
            if all(m in losers for m in members):
                keep = max(members, key=lambda p: (
                    analysis.get(p, {}).get("spectral_cutoff_hz") or 0))
                losers.discard(keep)
                rescued += 1
        if rescued:
            print(f"  {rescued} songs would have had every copy dropped "
                  f"(the two dedupe stages disagreed); kept the best of each")

    # The keeper is chosen on audio quality, which has nothing to do with how
    # well the track is identified -- so the better file can be the one you
    # never confirmed. Every member of a group is the same song, so the best
    # identity in the group belongs to whichever copy survives. Without this,
    # confirming "Lapsus Band - Hendikepiran" on one copy silently did nothing
    # because the other copy had the cleaner spectrum.
    by_path = {r["path"]: r for r in rows}
    inherited = 0
    if os.path.exists(NAME_DUPES):
        for g in json.load(open(NAME_DUPES)).get("groups") or []:
            members = [by_path.get(p) for p in [g["keep"]] + (g.get("drop") or [])]
            members = [m for m in members if m]
            if len(members) < 2:
                continue
            # Confidence first, then whichever copy you wrote something on.
            # Both copies of "idfc x soap" scored 1.00 -- one credited to the
            # uploading channel, one to Blackbear because you said so -- and a
            # plain max() keeps whichever came first, silently discarding the
            # note. Anything you typed outranks anything a lookup produced.
            def rank(r):
                why = " ".join(r.get("reasons") or [])
                # A note is strictly more specific than a link: both copies of
                # "idfc x soap" came from links you pasted, but only one had
                # you writing "Blackbear is the artist" next to it.
                return (r.get("confidence") or 0,
                        "your note" in why,
                        "from your link" in why or "confirmed by you" in why,
                        bool(r.get("proposed_album")), bool(r.get("proposed_year")))

            best = max(members, key=rank)
            keeper = by_path.get(g["keep"])
            if keeper is None or best is keeper:
                continue
            if rank(best) > rank(keeper):
                for k in ("confidence", "proposed_artist", "proposed_title",
                          "proposed_album", "proposed_year", "recording_id",
                          "source", "tier"):
                    keeper[k] = best.get(k)
                inherited += 1
    if inherited:
        print(f"  {inherited} kept files took the better identity from a "
              f"duplicate copy")
    if losers:
        before = len(rows)
        rows = [r for r in rows if r.get("path") not in losers]
        print(f"  skipping {before - len(rows)} duplicate copies "
              f"({len(losers)} losers known)")
    verify = json.load(open(VERIFY)) if os.path.exists(VERIFY) else {}
    enrich = json.load(open(ENRICH)) if os.path.exists(ENRICH) else {}
    lyric_cache = json.load(open(LYRICS)) if os.path.exists(LYRICS) else {}
    override_table = bpm_overrides.load()
    cascade = json.load(open(CASCADE)) if os.path.exists(CASCADE) else {}
    canon = json.load(open(CANON)).get("mapping", {}) \
        if os.path.exists(CANON) else {}
    art_index = json.load(open(ART_INDEX)) if os.path.exists(ART_INDEX) else {}
    sil = json.load(open(SILENCE)) if os.path.exists(SILENCE) else {}
    align = json.load(open(ALIGN)) if os.path.exists(ALIGN) else {}
    yt_links = json.load(open(YT_LINKS)) if os.path.exists(YT_LINKS) else {}

    # Album loudness, energy-weighted by duration -- see album_loudness().
    # Album peak is the loudest peak anywhere on the album, because one hot
    # track has to constrain the whole album's gain or the relative levels
    # album gain exists to preserve get broken again by the clipping cap.
    album_pool = {}
    for r in rows:
        alb = r.get("proposed_album")
        a = analysis.get(r["path"], {})
        if alb and a.get("loudness_lufs") is not None:
            key = f'{r.get("proposed_artist")}|{alb}'.lower()
            album_pool.setdefault(key, []).append(
                (float(a["loudness_lufs"]), float(a.get("decoded_secs") or 1),
                 a.get("true_peak")))
    album_gain, album_peak = {}, {}
    for key, vals in album_pool.items():
        if len(vals) < 2:
            continue
        mean = album_loudness([(l, w) for l, w, _ in vals])
        if mean is None:
            continue
        # One track without a peak means the album's true maximum is unknown,
        # and a max over the rest would understate it. Better no album peak
        # than one that licenses a gain the missing track cannot survive.
        peaks = [p for _, _, p in vals if p]
        peak = max(peaks) if len(peaks) == len(vals) else None
        if peak is None:
            # No album peak means no cap, and rg_gain returns the raw figure
            # when it has nothing to cap against. Writing that would hand the
            # album a gain the unmeasured track cannot survive, which is the
            # opposite of what the missing peak was telling us. Skip the album
            # entirely: track gain still applies, and it is capped per file.
            continue
        gain = rg_gain(mean, peak)
        if gain is None:
            continue
        album_gain[key] = gain
        if peak:
            album_peak[key] = peak

    # Deepest directory every source shares. Paths are mirrored relative to it,
    # so "<...>/Music library from phone/Music Mine/x.mp3" becomes
    # "out/_all/Music library from phone/Music Mine/x.mp3".
    common_root = ""
    if not args.flat:
        dirs = {os.path.dirname(r["path"]) for r in rows if r.get("path")}
        if dirs:
            common_root = os.path.commonpath(list(dirs)) if len(dirs) > 1 \
                else os.path.dirname(list(dirs)[0])

    # Everything above this line was computed from the whole library on
    # purpose: album gain is a mean over an album's tracks, the identity of a
    # kept file can come from a duplicate copy, and the output folder layout
    # is relative to the deepest directory all sources share. Narrow the set
    # any earlier and a subset build writes different tags to different paths
    # than the full build it is supposed to be compared against.
    if args.only:
        wanted, unknown = set(), []
        by_fp = {fp: v.get("path") for fp, v in
                 json.load(open(ANALYSIS)).items() if v.get("path")}
        known = {r["path"] for r in json.load(open(REVIEW)) if r.get("path")}
        with open(args.only, encoding="utf-8") as fh:
            for line in fh:
                # Leading # only: a song called "Song #1" is a real filename
                # and stripping from anywhere would drop it from the subset
                # without saying so.
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                line = line.strip()
                path = by_fp.get(line, line)
                if path in known:
                    wanted.add(path)
                else:
                    unknown.append(line)
        if unknown:
            print(f"  {len(unknown)} entries in {args.only} match no known "
                  f"track:")
            for u in unknown[:10]:
                print(f"    {u}")
        # A subset that silently came out empty writes nothing, reports no
        # error, and reads exactly like a clean run.
        writable = {r["path"] for r in rows}
        dropped = [p for p in wanted if p not in writable]
        rows = [r for r in rows if r.get("path") in wanted]
        if not rows:
            sys.exit(f"--only {args.only} selected no writable track "
                     f"({len(dropped)} of them are duplicate copies that are "
                     f"never written)")
        if dropped:
            print(f"  {len(dropped)} requested tracks are duplicate copies "
                  f"and are not written")
        print(f"  --only: writing {len(rows)} tracks")

    stats = {"identified": 0, "audio_only": 0, "promoted_by_lyrics": 0,
             "missing_audio": 0, "errors": 0, "with_art": 0, "with_genre": 0,
             "with_youtube": 0}
    intended, written_from = set(), set()
    for r in rows:
        src = r["path"]
        a = analysis.get(src, {})
        if not a or a.get("error"):
            stats["missing_audio"] += 1
        # Applied here, before anything reads a["bpm"], so the tag, the filename
        # and the playlists cannot disagree about the tempo.
        a, ov = bpm_overrides.apply(a, src, override_table)
        if ov:
            stats[f"bpm_{ov}"] = stats.get(f"bpm_{ov}", 0) + 1
        v = verify.get(src) or {}

        # Lyric confirmation is independent evidence: it lets a match below the
        # numeric bar through, because the audio itself agreed with the lyrics.
        confirmed = v.get("verdict") == "confirmed"
        trusted = bool(r.get("proposed_artist")) and (
            r["confidence"] >= args.min_confidence or confirmed)
        if trusted and r["confidence"] < args.min_confidence:
            stats["promoted_by_lyrics"] += 1

        ident = None
        if trusted:
            lead, feats, base_title = split_credits(
                r["proposed_artist"], r["proposed_title"])
            # One spelling per artist across the library, so a player that
            # groups by artist shows one Jole rather than three.
            lead = artist_names.canonical(lead, canon)
            all_artists = [lead] + [artist_names.canonical(f, canon) for f in feats]
            # Facts the cascade derived from keys the earlier stages produced:
            # album and year lead to a release, a release leads to artwork, an
            # ISRC leads to an exact track record. Never overrides an identity
            # field -- it only fills what identification left empty.
            cf = (cascade.get(src) or {}).get("facts") or {}
            ident = {"artist": r["proposed_artist"], "title": r["proposed_title"],
                     "album": r.get("proposed_album") or cf.get("album"),
                     "year": r.get("proposed_year") or cf.get("year"),
                     "recording_id": r.get("recording_id") or cf.get("recording_id"),
                     "confidence": r["confidence"],
                     "source": r.get("source") or "acoustid",
                     "lead_artist": lead, "all_artists": all_artists,
                     "isrc": cf.get("isrc"),
                     "label": cf.get("label"),
                     "genres": cf.get("genres"),
                     "track_number": cf.get("track_number"),
                     "total_tracks": cf.get("total_tracks"),
                     "disc_number": cf.get("disc_number"),
                     "cover_url": cf.get("cover_url"),
                     "art_path": art_index.get(cf.get("cover_url") or ""),
                     "release_group_id": cf.get("release_group_id")}
            # No genre-based doubling here. It used to fight the octave choice
            # made in analyze.py and could disagree with the TBPM frame, so the
            # filename showed one tempo and the player another.
            bpm_val = a.get("bpm")
            ident["display_title"] = compose_title(
                base_title, feats, bpm_val if args.bpm_in_title else None)
            fields = {
                # Same string as the title tag, so filename and in-app display
                # agree. Samsung Music shows the tag; a file manager shows this.
                "artist": lead, "title": ident["display_title"],
                "album": ident.get("album") or "", "year": ident.get("year") or "",
                "bpm": f"{int(round(bpm_val))}" if bpm_val else "",
                "camelot": a.get("camelot") or "", "key": a.get("key") or "",
                "quality": a.get("quality_grade") or "",
                "lang": (v.get("language") or ""),
            }
            try:
                stem = args.name_template.format(**fields)
            except KeyError as e:
                sys.exit(f"unknown placeholder in --name-template: {e}")
            # Keep the SOURCE extension. Hardcoding ".mp3" here renamed 86 AAC
            # files to .mp3, which then took the ID3 path in write_one and left
            # them with no Muzzi tags at all.
            name = (safe_name(re.sub(r"\s{2,}", " ", stem).strip(" -"))
                    + os.path.splitext(src)[1].lower())
            stats["identified"] += 1
        else:
            # No trusted identity, so there is no title tag; Android falls back
            # to the filename. Tempo still matters for mixing these, so it goes
            # on the end of the original name.
            stem, ext = os.path.splitext(os.path.basename(src))
            ab = a.get("bpm")
            if ab and args.bpm_in_title:
                stem = f"{stem} [{int(round(float(ab)))} BPM]"
            name = safe_name(stem) + ext
            stats["audio_only"] += 1

        # How much dead air comes off the front of this file, and what that
        # does to its lyric sheet.
        #
        #   lrc_shift = offset - cut
        #
        # where `offset` is how far the audio lags the sheet, measured on the
        # untrimmed source. An LRCLIB sheet is timed against the commercial
        # master; a YouTube rip carries extra padding at the head. So when the
        # padding is the whole story, offset == cut and the sheet needs no
        # shift at all -- cutting the silence is what puts the file back on the
        # sheet's timeline. Shifting by -cut "to compensate" would double the
        # error on exactly the tracks this is meant to fix.
        #
        # With no measurement the shift is zero rather than a guess: the file
        # is still trimmed, and lyric_align.py records why it could not be
        # checked so the review sheet can say so.
        cut = silence.cut_for(sil.get(src))

        lyrics, synced, lyric_reason = None, None, None
        entry = None
        if ident:
            entry = lyric_cache.get(f'{ident["artist"]}|{ident["title"]}'.lower())
            if isinstance(entry, dict):
                synced, lyrics = entry.get("synced"), entry.get("plain")
            elif isinstance(entry, str):
                lyrics = entry
            # Whether these are this song's words at all. Checked before the
            # shift, because re-timing the wrong song's sheet is wasted work.
            ok, why = lyrics_trustworthy(entry, v, ident.get("artist"),
                                         ident.get("title"))
            if not ok:
                lyrics, synced = None, None
                lyric_reason = why
                stats["lyrics_rejected"] = stats.get("lyrics_rejected", 0) + 1
            elif synced:
                # Whether the timings fit this recording. Separate question
                # from the one above and answered separately: a sheet can be
                # the right song and still be timed for a different edit, in
                # which case the words are worth keeping and the numbers are
                # not.
                ok, why = lyrics_timing_ok(entry, a.get("decoded_secs"), cut)
                if not ok:
                    synced = None
                    lyric_reason = why
                    stats["timing_rejected"] = stats.get("timing_rejected", 0) + 1

        # An offset only means anything next to the sheet it was measured
        # against, so the sheet goes in with the lookup. If lyrics_fetch has
        # since swapped in a different one, the measurement is refused rather
        # than applied to a body it never saw. Measured after the gates, so a
        # sheet we refused cannot be shifted.
        offset = lyric_align.offset_for(align.get(src), synced)
        lrc_shift = (offset - cut) if offset is not None else 0.0
        if abs(lrc_shift) < MIN_LRC_SHIFT:
            lrc_shift = 0.0

        if ident:
            if synced and lrc_shift:
                synced = lrc.shift(synced, lrc_shift)
                stats["lrc_shifted"] = stats.get("lrc_shifted", 0) + 1
            # Prefer the timestamped body for embedding. Re-read after the
            # shift so the embedded frame and the sidecar carry the same text.
            lyrics = synced or lyrics

        # Art and genre are only meaningful next to a trusted identity.
        extra = dict(enrich.get(src, {})) if trusted else {}
        if lyric_reason:
            extra["lyric_reason"] = lyric_reason
        if trusted and r.get("proposed_album"):
            akey = f'{r.get("proposed_artist")}|{r["proposed_album"]}'.lower()
            extra["album_gain"] = album_gain.get(akey)
            extra["album_peak"] = album_peak.get(akey)
        # Independent of identity, so it is attached whether or not the track
        # is trusted: where the audio came from is knowable even when we cannot
        # name the song, and an unidentified file is exactly the one a link
        # helps most.
        yt = yt_links.get(src)
        if yt:
            extra["youtube"] = yt
            stats["with_youtube"] += 1
        if extra.get("art_path"):
            stats["with_art"] += 1
        if extra.get("genres"):
            stats["with_genre"] += 1
        try:
            dst = os.path.join(args.out, subdir_for(src, common_root), name)
            lrc_path = os.path.splitext(dst)[0] + ".lrc"
            intended.add(os.path.realpath(dst))
            written_from.add(os.path.basename(src))
            trimmed = write_one(src, dst, ident, a, v, lyrics, extra,
                                dry=args.dry_run, cut=cut, lrc_shift=lrc_shift)
            if trimmed:
                stats["trimmed"] = stats.get("trimmed", 0) + 1
            elif cut and not args.dry_run:
                # The cut was wanted and did not happen: ffmpeg failed, or the
                # source is gone and the existing copy could not be re-made.
                stats["trim_failed"] = stats.get("trim_failed", 0) + 1
            # Sidecar .lrc as well: it is the most universally supported route
            # for synced lyrics on Android, and costs a couple of KB.
            #
            # The path is claimed in `intended` only when the file is actually
            # written. Claiming it unconditionally -- which is what this did --
            # meant a sidecar we decided NOT to write was still protected from
            # the prune sweep, so a stale one from an earlier run survived at
            # exactly that path. Per AGENTS.md the sidecar is the only lyrics
            # Samsung Music ever reads, so the rejected words were the ones
            # winning on a primary target player. Deleting has to happen here:
            # prune cannot do it, because prune only ever looks at what is
            # missing from `intended`, and it exempts .lrc from the last-copy
            # protection entirely.
            if args.no_lrc or args.dry_run:
                # Neither writing nor deleting. Claim the path anyway so a
                # sidecar written by an earlier run survives the prune sweep:
                # "skip writing" is what the flag says, not "clean up".
                intended.add(os.path.realpath(lrc_path))
            elif synced:
                with open(lrc_path, "w", encoding="utf-8") as fh:
                    fh.write(synced)
                intended.add(os.path.realpath(lrc_path))
                stats["lrc_files"] = stats.get("lrc_files", 0) + 1
            elif os.path.exists(lrc_path):
                os.remove(lrc_path)
                stats["lrc_removed"] = stats.get("lrc_removed", 0) + 1
        except Exception as e:
            stats["errors"] += 1
            print(f"  ERROR {os.path.basename(src)[:50]}: {type(e).__name__}: {e}")

    n = len(rows)
    print(f"\n  {'DRY RUN - ' if args.dry_run else ''}{n} tracks -> {args.out}\n")
    print(f"    full metadata (identity + audio)  {stats['identified']:4}"
          f"  ({100*stats['identified']/n:.0f}%)")
    print(f"      of which promoted by lyrics     {stats['promoted_by_lyrics']:4}")
    print(f"    audio-only (kept original name)   {stats['audio_only']:4}"
          f"  ({100*stats['audio_only']/n:.0f}%)")
    print(f"    missing audio analysis            {stats['missing_audio']:4}")
    print(f"    with cover art                    {stats['with_art']:4}")
    print(f"    with genre                        {stats['with_genre']:4}")
    print(f"    with a YouTube link               {stats['with_youtube']:4}")
    print(f"    synced .lrc sidecars              {stats.get('lrc_files', 0):4}")
    if stats.get("lrc_removed"):
        print(f"    stale .lrc removed                "
              f"{stats['lrc_removed']:4}")
    # A drop in sidecar count is the gates working, not a regression. Print
    # the reasons next to the total so it reads that way.
    if stats.get("lyrics_rejected"):
        print(f"    lyrics refused, wrong song        "
              f"{stats['lyrics_rejected']:4}")
    if stats.get("timing_rejected"):
        print(f"    timings refused, wrong edit       "
              f"{stats['timing_rejected']:4}   (words kept)")
    print(f"    leading silence trimmed           {stats.get('trimmed', 0):4}")
    if stats.get("trim_failed"):
        print(f"      wanted but not cut              {stats['trim_failed']:4}")
    print(f"    lyric sheets re-timed             {stats.get('lrc_shifted', 0):4}")
    print(f"    errors                            {stats['errors']:4}\n")

    # Report unconditionally, delete only when asked. A stale file is not
    # visibly wrong -- it is a correctly tagged copy of a song that also exists
    # under a better name -- so it will never be noticed on the phone except as
    # the same track appearing twice.
    # Under --only every file outside the subset is "not written by this run",
    # so the stale report would list the rest of the library as leftovers.
    # That is not a finding, it is the flag working, and printing it would
    # train people to ignore the one report that catches real duplicates.
    if args.only:
        print("  --only: stale-file report skipped, this run wrote a subset\n")
    elif not args.dry_run and stats["errors"] == 0:
        stale = []
        for dp, _, names in os.walk(args.out):
            for n in names:
                p = os.path.realpath(os.path.join(dp, n))
                if p not in intended:
                    stale.append(p)
        if stale:
            audio = [p for p in stale if not p.lower().endswith(".lrc")]
            print(f"  {len(stale)} files in {args.out} were not written by "
                  f"this run ({len(audio)} audio, {len(stale)-len(audio)} .lrc)")
            for p in sorted(audio)[:15]:
                print(f"    stale {os.path.relpath(p, args.out)}")
            if len(audio) > 15:
                print(f"    ... and {len(audio)-15} more")
            # A stale file should always be a second copy of a song that
            # survives under a better name. If its song is not in the output at
            # all, something upstream dropped a track and deleting here would
            # hide that -- so keep it and say so.
            keys = {_song_key(os.path.basename(p)) for p in intended
                    if not p.lower().endswith(".lrc")}
            orphans = [p for p in stale if not p.lower().endswith(".lrc")
                       and _song_key(os.path.basename(p)) not in keys
                       and _source_of(p) not in written_from]
            if orphans:
                print(f"\n  {len(orphans)} of them are the only copy of their "
                      f"song and will NOT be deleted:")
                for p in sorted(orphans)[:20]:
                    print(f"    keep  {os.path.relpath(p, args.out)}")
                stale = [p for p in stale if p not in set(orphans)]
            if args.prune:
                for p in stale:
                    os.remove(p)
                for dp, dirs, names in os.walk(args.out, topdown=False):
                    if not dirs and not names and dp != args.out:
                        os.rmdir(dp)
                print(f"\n  pruned {len(stale)} stale files\n")
            else:
                print("\n  re-run with --prune to delete them\n")
        else:
            print("  no stale files in the output\n")


if __name__ == "__main__":
    main()
