# Muzzi - plan and status

Turn untagged MP3s into a self-describing, portable library. All metadata lives
**inside the files** (ID3), so it works on any device with no app or database.

See `README.md` for how to run it. This file is the plan and the reasoning.

---

## 1. Status (330-track test library)

**Identification: 289 / 330 = 88%**

| Tier | Count | Meaning |
|---|---:|---|
| auto (>= 0.90) | 89 | accepted without review |
| review | 155 | plausible, worth a glance |
| suspect | 45 | probably wrong, check first |
| unmatched | 41 | needs a hint from you |

Source split: AcoustID 157, MusicBrainz 75, Discogs 57. All three earn their
place - they find *different* tracks.

**Metadata:** artist+title 87%, album 87%, year 84%, cover-art ID 70%.
**Audio analysis: 330/330, zero errors** - BPM, key, Camelot, danceability,
loudness, dynamics, quality grade.

### Built

| Stage | File | Speed |
|---|---|---|
| Inventory | `survey.py` | instant |
| Fingerprint | `pipeline/fingerprint.py` | ~1.5 s/track |
| Identify (fast) | `pipeline/identify.py` | **0.40 s/track** |
| Identify (fallback) | `pipeline/textsearch.py` | 1.26 s/track |
| Audio analysis | `pipeline/analyze.py` | 1.08 s/track, all cores |
| Confidence + queues | `pipeline/review.py` | instant |
| Lyric verification | `pipeline/verify_lyrics.py` | 4.3 s/track (base) |
| Cruft cleaner | `config/myplugins/muzziclean.py` | beets plugin |

### Not built yet

YouTube backfill. Everything else once listed here is built: tag writing,
cover art fetch, language tagging, crate/M3U export, provenance stamping and
hint resolution all ship today.

---

## 2. Next steps, in order

**A. Write the tags** (the actual deliverable)
Everything computed so far lives in `cache/`. Write it into ID3 on copies in
`out/_all/`: artist, title, album, year, genre, BPM, TKEY, Camelot, ReplayGain
(track and album gain, true peak, and the reference loudness they were computed
against), quality grade, language. Originals never touched.

**B. Cover art**
We hold `release_group_id` for 231 tracks. Fetch from the Cover Art Archive by
release-group MBID, fall back to iTunes/Deezer (both free, keyless), embed as
APIC. Parallel, ~8 concurrent.

**C. Provenance stamping (idempotency)**
Write `TXXX:MUZZI_VERSION` + a field digest into each file. Re-ingesting a
processed library then costs a tag read (milliseconds/file) instead of a
re-run, and a file that slipped in unprocessed is detected immediately.
(Built as `MUZZI_*`, not the `TEMPO_*` this originally proposed.)

**D. Crate export**
`out/crates/` folders (BPM bands, language, mood, remixes) plus M3U playlists.
Folders are the guaranteed-portable option; M3U is nicer where the player
supports it. Then a 10-file test on the S24 FE before committing to either.

**E0. App UI requirement (noted, not built)**
The web UI needs a **"include BPM in filename" checkbox, OFF by default**, with a
worked example beside it (`BAM BAM.mp3` -> `BAM BAM [BPM 144].mp3`) so it is
obvious what it does before it is switched on. The pipeline already supports
this via `--name-template` / `--no-bpm-in-title`.

**E. Hint resolution**
`hint` column in the review queues accepts a YouTube URL or
`artist=...; title=...`. yt-dlp resolves the URL to a real title, which is
re-fed to identify/textsearch. This is the only route for the 41 unmatched.

**F. Lyric verification pass**
Run over the ~200 uncertain tracks to promote confirmed matches out of the
review queue, shrinking manual work.

**G. YouTube backfill**
~560 tracks not yet downloaded, incl. 120 from "Music To Download". Capture
playlist membership at download time into `grouping` - it is your own curation
and cannot be reconstructed later.

**H. The big drive** (~2000-3000 files) once A-G are proven here.

---

## 3. Measured facts that shaped the design

**3.1 One AcoustID call beats beets' search by 42x.** beets issues 6-8
rate-limited MusicBrainz calls per track (17 s). One AcoustID lookup with
`meta=recordings releasegroups releases` returns recording MBID, artists,
albums and years in a single request: 0.40 s/track, and it *fixes* the album
gap (99% vs 22% album coverage).

**3.2 Fingerprints and text search find different tracks.**
AcoustID-only 157, text-only 132, union 289. Fingerprints fail on ex-Yu music
because nobody submitted them; text search finds it. Neither alone is enough.

**3.3 Top fingerprint score is not the right answer.** A file named
`Arctic Monkeys - Do I Wanna Know` matched Hozier's live cover at 0.995.
Scoring is 35% fingerprint / 65% filename agreement.

**3.4 Identical recordings tie exactly.** Several MusicBrainz recordings of the
same track score identically; one linked only to a promo compilation. First-seen
was winning, so `Do I Wanna Know?` resolved to "Promo Only: Modern Rock Radio"
instead of `AM`. Album quality now breaks the tie.

**3.5 Filenames come in both orders.** `Zbog mene ne placi - PRLJAVO KAZALISTE`
is Title-Artist. Trying both orders rescued several correct matches from the
review queue. Featured artists in titles are used as artist evidence too.

**3.6 Two BPM engines, because one lies.** They agree 77%, split by an exact
octave 20%. Essentia's own confidence is *not* predictive (a 0.9 "low
confidence" track had both engines agree exactly). Agreement is the gate.

**3.7 Audio quality must be measured, not read.** The bitrate header lies about
transcoded files. Spectral cutoff reveals the truth: 258 of 330 sit exactly at
the 16 kHz wall of 128 kbps, and the genuine outliers (10.5 kHz) are the
damaged files worth replacing.

The header also lies about *itself*. Essentia reports 1 kbps and mutagen 0 for
the AAC files YouTube serves, which carry no `esds` bitrate box -- 132 of 261
m4a files here. `analyze.real_bitrate()` falls back to size over duration,
which matches ffprobe exactly on all 132 and to within 8 kbps across all 250
AAC files, the gap being the container overhead it counts. Left unfixed this
cost nothing visible, because the grade comes from the cutoff -- but
`quality_suspect` needs 256 kbps or more to trip, and so could never fire for
an AAC file. A field that is merely never *read* still has to be right, or the
check built on it is decoration.

**3.8 Lyric verification works, and small models suffice.** Transcribing 45 s
and measuring bigram containment against the expected lyrics scores 0.34
(base) on correct matches and **0.000** on wrong ones. Separation is total even
with `tiny`; bigger models only add margin. Canary-1b-v2 is better at Croatian
but needs 6 GB and omits Serbian/Bosnian, so it is not worth it here.

**3.9 Never auto-reject on a low lyric score.** An instrumental passage looks
identical to a wrong song. Low means "unverified", not "wrong".

---

## 4. Dead ends worth remembering

- **AcousticBrainz** shut down Feb 2022. Its beets plugins still exist and will
  look like they should work. There is no BPM lookup service worth trusting;
  it must be computed.
- **Beatport plugin** (bundled) is dead - the v3 API was killed.
- **tekstowo / musixmatch** lyrics sources block beets' user agent.
- **`incremental: yes`** marks *skipped* files as done, so a failed match is
  never retried. Off.
- **beets' default `strong_rec_thresh: 0.04`** is unreachable for singletons -
  nothing scores below 0.111 because missing album/year is a hard penalty
  floor.
- **Rotating IPs** to beat MusicBrainz's 1 req/s is abuse of a donated service.
  The legitimate answer is a local mirror (~100 GB db-only, 350 GB full).

---

## 5. Future

**Recommendations.** `features/` already stores per-track MFCC vectors from the
analysis pass, so similarity search later is cosine distance rather than 4
CPU-hours of re-analysis. Combine with Last.fm similar-artist data (key already
configured) for "sounds like this" plus "people who like this also like that".

**Serving.** A Subsonic-compatible server on the VPS would give phone streaming
via existing clients (Symfonium, DSub) with no app to write. Worth trying
before committing to a custom PWA.
