# AGENTS.md

Muzzi turns a folder of messy music files into the same music with correct,
portable metadata written inside the files. Python, local-first, no server and
no library database. `CLAUDE.md` imports this file, so Claude Code and Codex
read the same instructions.

Public repository under AGPL-3.0: the code is published, the library it
describes is not. See `README.md` for how to run it, `PLAN.md` for the
reasoning, `CONTRIBUTING.md` for the contributor-facing version of these rules.

## Before acting

These four are standing rules, not preferences for one task. They are here
because they had to be said out loud on three consecutive days.

1. **Search online first, every time.** Before choosing a tool, format,
   library, API or approach, look it up. Docs change and assumptions about
   what a service supports go stale. This covers artwork, downloads, tag
   formats, player behaviour, everything.
2. **Report before you change.** Investigate, present what you found, then
   wait. "Look into it and report back" means do not touch the files yet.
3. **Ask instead of deciding.** A question costs a minute; guessing wrong
   costs hours. Never decide alone on: which copy of a song to keep, what
   counts as the same artist, or anything that deletes.
4. **Do the work yourself.** No subagents unless asked for them.

Ask means ask: do not work around the question or narrow the task to avoid it.
If you cannot stop, do the parts that do not depend on the answer, then ask.

## Target players

The library is consumed on Android. Two players are the daily drivers:

- **Namida** sets the ceiling. Richest metadata support, so if a field is worth
  having, tag it the way Namida reads it.
- **Samsung Music** sets the floor. No BPM field, reads `.lrc` sidecars only
  (not embedded lyrics), plays mp3, m4a, mp4, 3gp, 3ga, ogg, oga, aac and flac
  but not webm.

Everything else (Poweramp, Musicolet, Retro Music, Symfonium, AIMP, YouTube
Music offline, stock OEM players) must still work. Not everyone uses Namida.

1. Tag for Namida, degrade for Samsung Music. Never pick a format only one of
   them understands when a format both understand exists.
2. No feature may depend on a single player. If Namida alone reads something,
   add a fallback everyone reads: embedded lyrics *and* an `.lrc` sidecar, a
   BPM tag *and* the BPM in the filename as `144 BPM`.
3. Prefer widely supported forms: standard ID3v2.3/2.4 frames over custom ones,
   one genre string, square 600x600 JPEG art, relative M3U paths.
4. When you add a tag or an output, state the support matrix in the commit or
   PR: what Namida does with it, what Samsung Music does with it, what a
   generic Android player does with it.

## Decisions already made

Settled. Implement them, do not relitigate them, and do not quietly invent a
different rule when the code is ambiguous.

- **Existing tags are hints, never truth.** Tags on downloaded files are
  frequently wrong. Use them to find a candidate, never to confirm one.
- **Never dedupe on filename.** Compare fingerprint, duration, lyrics and
  measured audio. A different fingerprint, duration or lyric sheet means a
  different song, whatever the names say.
- **One copy per song: the better file**, chosen by measured spectral cutoff
  rather than claimed bitrate. On a quality tie the shorter one wins, because
  past about fifteen seconds the difference is an intro, an outro or a spoken
  channel tag.
- **On BPM disagreement, take the lower value.**
- **Metadata lives inside the file**, and the output preserves the source
  subfolder structure.
- **A blank field beats a wrong one.** Never invent metadata, and an
  unparseable hint is a note, not a title.

## Never

- Write to a source folder. Sources are read-only. Stages write to `cache/`,
  `review` writes the spreadsheets in `review/`, `export` writes
  `out/playlists/`, and only `write_tags` writes audio, as copies into
  `out/_all`. Nothing else writes anywhere.
- Commit `hints.tsv`, `review/`, `cache/`, `out/`, `config/secrets.json`,
  `config/config.yaml`, or any audio, artwork or lyrics. They describe a
  personal library and are gitignored. `.gitignore` takes no inline comments:
  a trailing `# ...` becomes part of the pattern and the rule matches nothing.
- Run Essentia in a thread or across a fork. It segfaults and corrupts the
  heap. Give it a separate interpreter.
- Truncate a fingerprint to use as a cache key. Prefixes collide, silently.
- Run `dedupe` before `analyze`. dedupe picks the copy to keep by measured
  spectral cutoff, so it needs the current `analysis.json`.
- Cache a failed request as a real answer.
- Hardcode parallelism. Derive it from CPU count and available RAM at runtime:
  this runs on a laptop and on a VPS.
- Poll with a pattern that matches your own process. `pgrep -f` and `pkill -f`
  match the shell running them, so the loop never exits and the kill can take
  you with it. Background the long job instead.
- Commit or push unless asked. When asked, include only that task's changes.

## Ask first

- Adding a dependency, an API, or a new pipeline stage.
- Changing a tag's format, or what a stage writes to `cache/`.
- Anything with two plausible readings. Ask before you edit, do not pick one.

## Commands

```bash
./.venv/bin/python run.py "/path/to/music"
./.venv/bin/python run.py DIR --dry-run          # print the plan, do nothing
./.venv/bin/python run.py DIR --from webmatch --skip verify_lyrics
ruff check .                                     # what CI runs
```

Shell state does not persist between commands and the working directory
resets, so use absolute paths rather than relying on a previous `cd`.

Stages are idempotent and cache-backed, so re-running is the normal way to
work. It is not free: new files, an emptied cache and previously failed
requests all still cost time.

## Hit every surface

The most common defect here is a change that works on the path you tested and
is missing everywhere else. Before calling metadata work done, walk this list
and say which entries applied:

- **Containers.** m4a is a quarter of this library. The MP4 path has silently
  written fewer tags than the MP3 path before, which took two playlists with
  it. Freeform MP4 atoms also hold raw bytes, so `str()` on one gives `b'hr'`.
- **Both carriers.** Lyrics are embedded *and* an `.lrc` sidecar. BPM is a tag
  *and* part of the filename. Change one, change the other.
- **Both playlist forms.** Relative M3U, and the `--absolute` variant.
- **The reverse.** If a stage can add something, something has to be able to
  remove it. `out/_all` is rebuilt, not appended to, which is why `write_tags`
  reports leftovers and `--prune` exists. A one-way door is a bug.
- **The review queue.** A new field that a human must confirm needs a column in
  `review/`, a way to answer it, and a line in `hints.tsv`, or it is unusable.

## Verifying

Smallest proof that the change works: run the one stage you touched against a
few files, and `ruff check .`. Do not run the whole pipeline to check one
stage, and never run it against the real library.

**Count artifacts, not exit codes.** Most defects that reached this library
were silent: nothing errored, a file was simply absent, doubled or mis-tagged.
"It ran without errors" is worth nothing here. Count the files, the tags, the
playlist entries.

**Check the checker before you trust it.** Two verification scripts here were
themselves wrong, which is worse than not verifying: a passing check reads as
proof. There is no test suite. Say which commands you ran, which you did not,
and what is therefore still unproven. A green `ruff` never implies behaviour
works.

## Commits

Conventional, scoped to the pipeline stage or area:

```text
type(scope): Short summary in imperative mood

Changes:
- Specific change

Brief explanation of why the change was needed.
```

Add a `Notes:` section only when there is something a reviewer would otherwise
miss. Types: `fix`, `feat`, `docs`, `refactor`, `chore`, `style`, `perf`, `ci`,
`build`. Scopes match the stage or file where one exists: `identify`,
`analyze`, `dedupe`, `lyrics`, `export`, `write_tags`, `review`, `scenes`,
`agents`, `readme`, `deps`.

Never add a `Co-Authored-By` trailer. Use full 40-character SHAs when referring
to commits, and name the issues a commit or PR closes.

## Pull requests

- Never open a PR unless asked.
- Conventional title, same scopes: `fix(dedupe): keep the shorter copy`.
- Body: what changed, then why, then the support matrix if tags changed.
- One concern per PR. If the description says "also", split it.
- When babysitting: check for review comments newer than your last push, verify
  each bot finding against the source rather than trusting it, fix the real
  ones, and say plainly why you are dismissing the rest. Stay quiet when there
  is nothing new. Stop when the bots are green on the latest commit.

## How I want you to work

- Deliver what was asked, at the scope asked. If a better approach exists, say
  so in a sentence and carry on with the task as asked rather than quietly
  widening it.
- Explain what you changed and why at the end. The why is the point.
- Keep the README current in the same change that alters behaviour, not
  afterwards.
- Clean up after yourself in the same session: scratch scripts, old
  spreadsheets, stale caches.
- Match a document's length to what it needs. No filler sections, no redundant
  summaries.
- Never use em dashes or en dashes, anywhere: chat, code comments, docs, commit
  messages, PR text. Use a comma, a colon, parentheses, or rewrite the line.
  CI enforces this for Markdown; the rest is on you.
- Markdown here is hard-wrapped at 79 columns. Match it, do not reflow a file
  you are editing.
- Estimates here have only ever moved one way. Give a range, not a number, and
  revise it before being asked.
