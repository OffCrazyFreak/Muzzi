# Muzzi

Takes a folder of messy music files and produces a folder of the same music
with correct, portable metadata: artist, title, album, year, BPM, key, genre,
cover art and synced lyrics, all written **inside the files**.

No library database. Everything that matters travels with the audio, so it
survives a copy to a phone. Built specifically to handle Croatian, Serbian and
Bosnian music, which MusicBrainz and Picard barely cover.

beets, Picard and wrtag all assume the catalogue knows your music. For this one
it does not, so Muzzi scores every match, sends what it cannot settle to a
review queue, remembers your answers permanently, and checks the audio against
the lyrics before it trusts a name. beets does the tag writing underneath.

Your source folders are never modified. Every stage writes to `cache/`, and
only the last stage writes audio -- as copies, into `out/_all`.

Licensed [AGPL-3.0](LICENSE).

---

## What it produces

```
out/_all/          one copy of every song, tagged, in the same subfolder
                   layout as your sources; a .lrc sidecar next to each
out/playlists/     .m3u by genre, BPM band, decade, language, mood, quality
review/            numbered spreadsheets, only what needs your eyes
hints.tsv          every answer you have ever given, kept permanently
```

---

## Target players

The output is aimed at Android. **Namida** sets the ceiling: it reads the most
metadata, so anything worth tagging is tagged the way it expects. **Samsung
Music** sets the floor: no BPM field, `.lrc` sidecars only, no webm. Every
other player (Poweramp, Musicolet, Retro Music, Symfonium, AIMP, YouTube Music
offline, stock OEM players) has to work too, so nothing here depends on one
app being installed.

That is why:

- Playlists use relative paths, which is what Namida and YouTube Music want.
  `export.py --absolute` adds a second set with absolute phone paths, for
  Poweramp and anything else that ignores relative ones.
- Filenames come out as `Artist - Title (ft. Other) [131 BPM].mp3`. The BPM is
  in the name because Samsung Music has no BPM field and won't show the tag.
- Lyrics are written twice: embedded (Namida and most players read this) and as
  a `.lrc` sidecar (Samsung Music reads **only** sidecars).

`AGENTS.md` has the rules that follow from this, for anyone changing the code.

---

## Requirements

- Linux (developed on Mint), Python 3.11 to 3.14. The pins in
  `requirements.txt` set the floor (numpy needs 3.11) and the ceiling (beets
  needs below 3.15). Built and verified on 3.12.
- `ffmpeg`, `yt-dlp`
- Node 22+ or Deno 2.3+, only for age-restricted YouTube videos: yt-dlp needs a
  JavaScript runtime to solve their challenge (`--js-runtimes node`, plus the
  `yt-dlp[default]` extra which brings the solver scripts)
- Everything else installs into a local venv; nothing touches system Python

Parallelism is detected at runtime from `os.cpu_count()` and available RAM, so
the same commands work on a laptop or a VPS. Override with `-j N`.

---

## First-time setup

```bash
cd Muzzi
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# Chromaprint fingerprinter
mkdir -p bin && curl -fsSL -o /tmp/fp.tgz \
  https://github.com/acoustid/chromaprint/releases/download/v1.5.1/chromaprint-fpcalc-1.5.1-linux-x86_64.tar.gz
tar xzf /tmp/fp.tgz -C /tmp && cp /tmp/chromaprint-fpcalc-*/fpcalc bin/ && chmod +x bin/fpcalc
```

### API keys

```bash
cp config/secrets.example.json config/secrets.json && chmod 600 config/secrets.json
cp config/config.example.yaml config/config.yaml
```

Fill both in. All free. Neither file is ever committed - both are in
`.gitignore`, and `config/config.yaml` matters as much as `secrets.json`
because beets has no indirection for the Discogs, Last.fm and Genius tokens
and takes them inline.

| Service | Needed for | Get one at |
|---|---|---|
| AcoustID | fingerprint lookups | acoustid.org/new-application |
| Discogs | ex-Yu / regional catalogue | discogs.com -> Settings -> Developers |
| Last.fm | cover art, genres | last.fm/api/account/create |
| Genius | lyrics (improves language detection) | genius.com/api-clients |

> AcoustID keys are **per application**. Do not reuse another project's key.

Also set `contact` in `secrets.json` - an email or a URL. MusicBrainz requires
a reachable contact in the User-Agent and throttles or blocks generic ones.
Left blank it falls back to this project's URL, which works but means you are
sharing an identity with everyone else who never set it.

YouTube Music, Deezer, iTunes, LRCLIB and Cover Art Archive need no key.

---

## Running it

```bash
./.venv/bin/python run.py "/path/to/music"
```

That's the whole thing. Point it at one folder or several; they get merged into
one output while keeping their subfolder structure.

```bash
run.py DIR --dry-run              # print the plan, do nothing
run.py DIR --from webmatch        # resume partway through
run.py DIR --skip verify_lyrics   # verify_lyrics is the slowest stage
run.py DIR -j 8
```

Every stage is idempotent -- it reads its own cache and does only what's
missing. Interrupting and re-running costs nothing, which is also how you add
music later: point it at the new folder and it processes only what's new.

Expect roughly 2-3 hours for ~2000 files on a normal desktop, most of it in
`verify_lyrics` (Whisper) and the 1-request-per-second MusicBrainz limit.

---

## How it works, end to end

Follow one file through. Say it arrives as `Zbog mene ne placi - PRLJAVO
KAZALISTE.mp3`, with no tags worth the name.

**1. It gets an acoustic fingerprint.** Chromaprint reduces the audio itself to
a signature. This is the only identifier that cannot lie: it survives
re-encoding, renaming and bad tags, and it is what lets two copies of a song be
recognised as one later.

**2. Two chains run at once, because they need nothing from each other.** One
asks the internet who this is: AcoustID by fingerprint, then MusicBrainz and
Discogs by name, then YouTube Music, Deezer and iTunes. The other reads the
waveform -- BPM from three engines, musical key, loudness, danceability, and
the spectral cutoff that says what the audio is *really* worth regardless of
what the bitrate claims. The second chain cannot be wrong about which file it
is describing, which is why its results are written for every track even when
nobody knows the song's name.

**3. Everything gets a confidence score, and the low ones become your queue.**
Not a report -- a decision. Each track carries explicit reasons ("artist
mismatch vs filename (similarity 0.38)"), and anything below the bar lands in
`review/`, worst first. Your answers are remembered in `hints.tsv` forever, so
the spreadsheets are disposable views rather than work you can lose.

**4. What was learned becomes new questions.** An artist and title buys a
Deezer lookup, which returns an ISRC, which unlocks exact lookups everywhere
else, which give an album, which gives a release group, which gives the
original year and the cover art. This loop keeps firing until a pass produces
nothing new. Meanwhile the lyrics are fetched and Whisper transcribes the audio
to check they match -- independent proof the file is the song we think it is.

**5. The library is made consistent with itself.** One spelling per artist
(Cyrillic transliterated, channel suffixes dropped, typos merged into the
confirmed spelling), one country per artist, one genre per track, and one copy
per song -- chosen on measured quality, or on length when the copies differ by
enough that one has an intro the other does not.

**6. Copies are written.** Originals are never touched. Every tag goes inside
the file, filenames get the BPM because Samsung Music has no BPM field, a
`.lrc` sidecar goes beside each track because Samsung Music reads only those,
and the playlists are built from the tags that were just written -- so what a
playlist says and what the player groups by cannot disagree.

Then `verify` reads the output back and reports what still needs you.

---

## The flow

```
1.  fingerprint
2.  identify -> textsearch          ║  analyze -> dedupe
3.  review
4.  from_filename -> review
5.  hints_resolve -> review
6.  webmatch -> review
7.  cascade -> fetch_art -> ...     ║  lyrics_fetch -> verify_lyrics
8.  review
9.  artist_names -> origin -> lastfm_tags -> dedupe_names
10. write_tags -> export -> verify
```

The `║` pairs are run concurrently: one chain is network-bound and the other is
CPU-bound, so pairing them costs nothing and roughly halves wall clock.

**Identification is a cascade of increasingly desperate methods**, each only
seeing what the previous one failed on:

| Stage | Method |
|---|---|
| `fingerprint` | Chromaprint acoustic fingerprint per file |
| `identify` | fingerprint -> AcoustID -> MusicBrainz. Authoritative, but weak on Balkan music |
| `textsearch` | search MusicBrainz and Discogs by name |
| `tagseed` | scores *where* a name came from, so a "Lyrics Channel HD" artist tag doesn't outrank a filename |
| `from_filename` | believe the filename, then ask MusicBrainz to confirm it |
| `webmatch` | YouTube Music, Deezer, iTunes, YouTube, SoundCloud |
| `hints_resolve` | turns links you paste into metadata, via yt-dlp |

**Analysis** is independent of all that, because it reads the waveform and
can't be wrong about which file it's describing:

| Stage | Produces |
|---|---|
| `analyze` | BPM (three engines), musical key -> Camelot, loudness, danceability, spectral cutoff |
| `dedupe` | files whose fingerprints match: the same recording twice |
| `verify_lyrics` | Whisper transcribes the audio and compares it to the fetched lyrics -- independent proof the file is the song we think, plus language detection |

**Enrichment** turns what we learned into new search keys:

| Stage | Produces |
|---|---|
| `cascade` | fixpoint loop: each resolver declares what it needs and gives, and keeps firing until nothing new appears. `artist+title -> Deezer track -> ISRC -> exact lookups -> album -> release-group -> artwork` |
| `enrich_release` | years, from album + artist |
| `fetch_art` | downloads artwork, once per album |
| `lyrics_fetch` | LRCLIB, preferring synced lyrics, choosing by duration match |
| `genres` | collapses multi-source genre soup into one canonical genre |
| `origin` | which country each artist is from, from Last.fm tags cross-checked against ISRC |
| `lastfm_tags` | community tags for every artist and track. Cached, so the ~20 minutes it takes happens once |
| `scenes` | picks the ONE genre each track gets, most specific evidence first |

**Repair passes** (not part of `run.py`; run them when you want them):

| Stage | Does |
|---|---|
| `redownload` | re-fetches files whose audio is worse than their container claims. Downloads into `redownloaded/`, keeps a candidate only if it is measurably better AND the same length, so "improve quality" can never quietly become "change the song" |
| `art_missing` | cover art for tracks with no album behind them -- bootleg remixes, regional uploads. Deezer/iTunes track search, then the YouTube thumbnail, then the artist photo |

**Deciding and writing:**

| Stage | Does |
|---|---|
| `review` | scores everything 0-1 with explicit reasons, applies your hints, sorts into auto / review / suspect, writes the spreadsheets |
| `artist_names` | one spelling per artist: Cyrillic to Latin, channel suffixes off, ALL CAPS down, and typos merged into the confirmed spelling |
| `dedupe_names` | duplicates fingerprinting can't see -- two YouTube uploads of one song are different recordings, so only the name pairs them |
| `write_tags` | copies to `out/_all` and writes every tag, plus `MUZZI_*` provenance stamps. `--prune` deletes output left over from an earlier build |
| `export` | playlists |
| `verify` | reads the output back and reports what needs attention |

`review` runs five times on purpose. It isn't a report, it's the function that
turns evidence into a decision, so it re-runs whenever new evidence arrives.

---

## The review workflow

The pipeline finishes and rebuilds `review/` from scratch, containing only what
it wasn't sure about. The sheets are numbered in the order worth working
through, and a sheet only exists if it has rows -- so an empty folder means
there is nothing left to answer.

| File | Contains | Answer with |
|---|---|---|
| `1 - needs a link.ods` | nothing found anywhere | a URL |
| `2 - confirm the name.ods` | a name was proposed but the filename disagrees | `y` or `n` |
| `3 - check the rest.ods` | everything else, least confident first | anything |

**Your answers are kept in `hints.tsv`, not in the sheets.** Delete a sheet
whenever you like; the answers in it are already remembered, and an answer
given on a file you later delete as a duplicate moves onto the copy that
survives.

Fill in the **`hint`** column and re-run:

```bash
run.py DIR --from hints_resolve
```

### What you can put in the hint column

| You type | Meaning |
|---|---|
| `y` | the proposed artist and title are correct |
| `n` | wrong, discard the proposal |
| a YouTube URL | use this video's metadata |
| any other link | Audiomack, SoundCloud, Bandcamp -- anything yt-dlp supports |
| a search-results URL | the first result is used |
| `artist: X; title: Y` | type it yourself (`=` works too, as does `version:`) |
| `<url> (Blackbear is the artist)` | a note next to a link overrides the link |
| `drop` | you already have this song; leave this copy out |
| anything else | treated as a note: recorded, changes nothing |

That last row matters: unparseable text used to be written into the title tag,
which turned notes into song names. Anything you type outranks anything a
lookup produced, but only when it's unambiguous what you meant.

### Reading the confidence column

| Score | Meaning |
|---|---|
| `1.00` | you confirmed it, or it came from a link you gave |
| `0.90+` | auto-accepted: fingerprint match, or two catalogues agreeing |
| `0.80` | one catalogue confirmed it -- worth a glance |
| `0.70` | the filename, uncorroborated. Kept, deliberately below the bar |
| below | no identity tags written; the file keeps its original name |

The bar is high on purpose: a wrong artist is worse than a missing one.

---

## Hand-maintained config

| File | For |
|---|---|
| `config/bpm_overrides.json` | per-track BPM corrections, so tag, filename and playlists can never disagree |
| `config/artist_aliases.json` | pinned artist spellings (`Joško Čagalj Jole` -> `Jole`) |
| `config/artist_overrides.json` | artist pins keyed by YouTube video id |
| `config/scenes.json` | the scenes no database has -- Croatian Trap, Croatian Trash, Ex-YU. An artist can belong to two |
| `config/config.yaml` | beets settings; the Discogs user token lives here, not in `secrets.json` |

`scenes.json`, `artist_aliases.json` and `artist_overrides.json` ship filled in,
because they are the part no API can give you and they are a working example of
each format. `bpm_overrides.json` does not: it is keyed by real filenames, so
publishing one is publishing a track listing. Copy
`config/bpm_overrides.example.json` and build your own.

---

## Design notes worth knowing

**BPM.** Three engines vote (Essentia RhythmExtractor2013 multifeature, degara,
PercivalBpmEstimator). Validated 20/20 within 4% against published values for
well-known tracks. On an octave disagreement the **lower** value wins, because
tempo-matching by feel is the point. Deezer publishes a tempo for about a third
of tracks; where it disagrees by something other than an octave it has been
right every time it could be checked by hand, so those are corrected outright.

**Version markers are never crossed.** `Animals` and `Animals (Balkanik Remix)`
are two songs. Searches that strip `(Remix)`, `(Cover)` or `prod. by` to widen
a query get their results checked for the marker before anything is believed --
otherwise the catalogue confidently returns the original and you tag a cover as
the real thing.

**Duration is a tie-breaker, never a veto.** These files are YouTube rips with
intros the streaming single doesn't have. A 20-second gap is normal, and gating
on it threw away a third of the correct matches.

**The "better file" is chosen by measured spectral cutoff**, not by claimed
bitrate -- a file can claim 320kbps and be a 128kbps transcode. On this library
the old MP3 rips beat the newer YouTube AAC 315 times out of 322. Because
quality and identification are independent, the surviving copy inherits the
best identity in its duplicate group.

**Fingerprints are never truncated for keying.** 330 files here share only 321
distinct 52-character prefixes, so a prefix-keyed cache silently overwrites
other tracks' data.

**Downloads are m4a, not `bestaudio`.** yt-dlp's best audio for these tracks is
Opus in a `.webm` container. Samsung Music plays mp3, m4a, mp4, 3gp, 3ga, ogg,
oga, aac and flac -- not webm -- and Essentia cannot decode it either, so the
files would be both unplayable and unmeasurable. The Opus stream is ~136k
against AAC's ~130k; six kilobits is not worth a file that will not open on the
device this exists for.

**Cover art is square, 600x600 JPEG.** Players crop or stretch anything else.
YouTube thumbnails are 16:9, so they are centre-cropped, and anything under
400px is discarded -- YouTube answers a missing `maxresdefault` with a grey
placeholder rather than a 404, so size is the only way to spot it.

**Essentia must not be used in threads or across a fork.** It segfaults in a
ThreadPoolExecutor and corrupts the heap when inherited by a forked worker
(`munmap_chunk(): invalid pointer`). Where it has to run concurrently, it runs
in genuinely separate interpreters.

**A hint that cannot be parsed is a note, not a title.** Free text used to
become the title tag, which turned "same as the one above, you didn't merge"
into a song name and lost three real titles entirely.

**The duplicate keeper inherits the group's best identity.** The keeper is
chosen on audio quality, which is independent of how well a track is
identified -- so without this, confirming a match on the lower-quality copy
silently did nothing and the survivor shipped with no identity tags at all.

**`analyze` runs before `dedupe`, never after.** dedupe chooses which copy of a
song to keep by measured spectral cutoff, so running it against an
`analysis.json` that predates the newly-added files gives every new file no
quality figure at all -- and it loses by default. This kept the worse copy of
69 songs, including a 10.3kHz mp3 over a 15.7kHz replacement.

**The output folder is rebuilt, not appended to.** `out/_all` has no memory of
which build wrote what, so a file whose source later became a duplicate loser
stayed there forever and the library quietly grew a second copy of 59 songs.
`write_tags` now reports leftovers every run and `--prune` removes them. It
refuses to delete a leftover that is the only copy of its song: that means
something upstream dropped a track, and hiding it would be the worse failure.

**Non-MP3 files get the same tags as MP3s.** m4a is a quarter of this library.
The MP4 path used to write identity and BPM but not `QUALITY`, `MUZZI_SOURCE`
or the rest, and the "Check quality" and "Needs identification" playlists are
built by reading exactly those two tags -- so a quarter of the library could
never appear in them. Freeform MP4 atoms also hold raw bytes: `str()` on one
yields `b'hr'`, which is not a language code and produced a crate named after a
Python literal.

**One genre per track, most specific evidence first.** Players show a single
genre, so this is a ranking problem: hand-maintained scene first, then Last.fm
scene tags, then Discogs *styles*, then the Deezer genre as a floor. Discogs
styles are the specific ones -- it calls Grse and Vojko V "Trap" where Last.fm
has no trap tag for either. Nothing is invented: a track with no evidence gets
a blank genre, because a blank field beats a wrong one.

A Last.fm tag has to clear three bars before it becomes a genre: enough weight
to be more than one person, at least a quarter of that artist's top tag, and
used on at least three artists in the library. Without the last one, free text
produced genres called "5 Stars", "Brcko", "Gazda Paja" and "Glee". Nationality
is filtered out entirely -- "German" is not a genre, and "Serbian" says nothing
the Language crate does not.

**Some scenes exist only as a list.** "Croatian Trap" and "Croatian Trash" are
things people book festivals around and record blogs catalogue, and nowhere in
any database. `config/scenes.json` holds them by hand. An artist can be in two:
Prljavo kazaliste are both Ex-YU and Croatian Trash, and ID3v2.4 lets one TCON
carry both values -- a player showing one genre takes the first, one showing
all gets both.

**Between two copies of a song, the shorter one wins.** Quality decides when
they are the same length, but past about fifteen seconds the difference is an
intro, an outro or a spoken channel tag rather than more song: "Prevarena" is
4:31 and 5:19, and the 5:19 is the official video with a long cold open. This
also means length alone no longer splits a group -- the fingerprint has already
said they are the same recording.

**"Croatian" and "Serbian" are artist origin, not language.** No language
detector will split BCMS, and it is right not to: it is one pluricentric
language, and every Balkan track here is tagged `hr`. Origin is a different
question and is answerable -- Last.fm tags artists by nationality, and the
ISRC we already hold names the label's country. So the crate says where the
artist is from, which is what makes it browsable; a Croatian artist singing in
English still lands in Croatian.

Last.fm is keyed by name and collides badly: "Rasta" returns a Belarusian
melodic death metal band, and our own MusicBrainz id for that artist points at
the same wrong band. A Last.fm answer is therefore only believed when it names
a Balkan country at all -- otherwise the name has landed on someone else and
the ISRC decides.

**Lyrics are matched on artist, not just title and length.** LRCLIB holds
several entries per title and ranking them by duration alone fetched JoelB's
"MAMBA" for Grse's and a Polish "Kawasaki" for Rasta's -- whole songs in the
wrong language, embedded as if they were right, and then used to decide the
track's language. Serbian and Croatian also transliterate foreign words
("Kawasaki" is filed as "Kavasaki"), so those spellings are tried before
giving up.

**Lyrics are not embedded when the audio argues against them.** An
instrumental with under three transcribed words still gets 4000 characters
offered to it, which is how NCS tracks shipped with someone else's words. A
mismatched artist alone is not disqualifying -- where a file's own tags said
"various", LRCLIB's answer was the correct one -- but a mismatch that
verification also fails to confirm is.

**Language comes from the lyrics, not from Whisper's guess.** Whisper reports a
language for anything, including silence, and reports it confidently -- ten
near-instrumental NCS tracks came back as Khmer at up to 0.84, which put a
`Language - km` playlist on the phone. Fetched lyrics are text a person wrote,
so detection on them is decisive where Whisper is not (two Colonia tracks it
called Khmer are Croatian at 0.9999). The transcript is used only when the
lyrics matched it and it holds enough distinct words to be evidence of
anything.

**A re-download must match what was asked for, and no video is used twice.**
A YouTube Music search for "Courtney Parker - Her Last Words" returned B-Mike's
"Baby Don't Cut" as its first hit. Nothing checked the result, so two unrelated
songs were both replaced by that one recording -- which made the files
byte-identical, chained the two songs into one duplicate group, and nearly
dropped one of them from the library entirely.

**Two songs can share a title and a running time.** Names are never enough to
merge on. dedupe checks length, tempo (allowing an octave), version markers and
the acoustic fingerprint: The Kid LAROI's "WITHOUT YOU" is 182.1s and Avicii's
"Without You" is 181.7s, and matching on filenames alone merged them. They are
93 and 134 BPM, and their fingerprints agree no better than chance.

dedupe.py compares fingerprints within +/-8 frames, which is why it cannot pair
two uploads of one song -- a different intro moves them much further apart. Over
a wide offset the same fingerprints separate cleanly: genuine duplicates here
score 0.02 to 0.44 bit-error, two different songs score 0.501, so 0.47 vetoes
the unrelated without splitting anything real.

**A link is worth a second attempt.** yt-dlp supports a site until the site
changes; its Audiomack extractor has returned 404 since that API moved, which
made a perfectly good link useless. When it fails, the page's OpenGraph and
schema.org metadata are read instead -- which is how "Daj mi snagu" was
resolved, duration and all. It reads only what a site states about itself in a
standard format: guessing at page layout would break at the next redesign,
which is the failure being worked around.

**A note is not an answer.** The sheets are read after `hints.tsv` so a fresh
answer overrides a stale one -- but that let "same as above, forgot to merge",
left in a sheet from before, throw away a link typed today. Free text can no
longer displace a real answer, in either direction.

**Playlists are rebuilt, not added to.** Same reasoning as the output folder: a
crate that stops existing leaves behind a stale `.m3u` that still lists real
files and looks entirely valid on the phone.

**Analysis is keyed by fingerprint AND path.** Two files can share a
fingerprint and still both need measuring -- the same song downloaded into two
source folders, or an mp3 and an m4a of one master. Keyed on the fingerprint
alone, the second file had no analysis under its own path, so dedupe could not
measure it, could not drop it, and shipped the song twice under two names.

**Long jobs need `setsid`.** Backgrounding a stage as a child of the shell means
session teardown kills it mid-run.

---

## Layout

```
run.py                  the only entry point you need
pipeline/               one file per stage, each runnable alone
tools/                  standalone helpers (export_cookies.py)
config/                 keys and hand-maintained corrections
cache/                  every stage's output; safe to delete, expensive to rebuild
out/_all                the result
out/playlists           .m3u playlists
review/                 numbered spreadsheets, only what needs your eyes
hints.tsv               every answer you have ever given
```

Everything below `run.py` in that list is created at runtime and none of it is
in the repository - `cache/`, `out/`, `review/`, `hints.tsv`, the venv, the
`fpcalc` binary and the Whisper weights are all gitignored. A clone is the code
and the curated config, nothing else. That is deliberate: `hints.tsv` and the
tagged output describe a specific person's music library.

## Re-running

Re-running over the same sources gives the same output: every stage reads its
own cache and redoes only what is missing, and the decisions are deterministic
(verified by running them under different hash seeds).

Deleting `cache/` is a different matter. The pipeline would rebuild, but not
identically: it asks services whose answers change. Last.fm tags are edited,
Deezer adds releases, LRCLIB gains lyrics. The result would be as good or
better, not byte-for-byte the same. `hints.tsv` is the part that must survive --
it holds every answer you have given, and nothing can reproduce it.

Each `pipeline/*.py` runs standalone with `--help` and its docstring explains
what it does and why it does it that way.

---

## Auditing what was written

`pipeline/verify.py` reads tags only. To check a tag against the audio it
claims to describe, `tools/` decodes the finished library and compares:

```bash
tools/audit_truth.py  out/_all cache/audit_truth.json    # ffprobe + ffmpeg ebur128
tools/audit_cutoff.py out/_all cache/audit_cutoff.json   # re-runs analyze.py's own code
tools/audit_compare.py                                   # joins both against the tags
```

They report and change nothing. `audit_cutoff.py` deliberately imports
`spectral_cutoff`, `grade_quality` and `real_bitrate` from `pipeline/analyze.py`
rather than reimplementing them, so it measures the pipeline instead of a
lookalike -- an earlier copy of the bitrate read reproduced the exact AAC bug
the audit was meant to catch.

If a container is found to misreport its bitrate, correct the cache without a
full re-analysis:

```bash
pipeline/analyze.py --refresh-bitrate   # header only, no decoding, seconds not hours
```

`dedupe` consumes `bitrate_kbps`, so run this before any later dedupe.

---

## Contributing

Issues and pull requests are welcome. [CONTRIBUTING.md](CONTRIBUTING.md) has
the setup, what is most useful to work on, and the rules. `AGENTS.md` holds the
project conventions, for humans and coding agents alike. Security reports go
through [SECURITY.md](SECURITY.md), not a public issue.

---

## License

[GNU AGPL-3.0](LICENSE). Use it, change it, share it - but anything you
distribute built on it stays under the same license and keeps the attribution,
and that extends to running a modified version as a network service: users of
that service are entitled to its source.

The metadata this tool fetches is not covered by that. MusicBrainz, Discogs,
Last.fm, LRCLIB and Cover Art Archive each carry their own terms, and cover art
in particular is generally still under copyright - embedding it in your own
files is one thing, redistributing it is another.
