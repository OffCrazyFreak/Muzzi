# AGENTS.md

Muzzi turns a folder of messy music files into the same music with correct,
portable metadata written inside the files. Python, local-first, no server and
no library database. `CLAUDE.md` imports this file, so Claude Code and Codex
read the same instructions.

Public repository under AGPL-3.0. The code is published; the library it
describes is not.

See `README.md` for how to run it, `PLAN.md` for the reasoning,
`CONTRIBUTING.md` for the contributor-facing version of these rules.

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
   BPM tag *and* the BPM in the filename.
3. Prefer widely supported forms: standard ID3v2.3/2.4 frames over custom ones,
   one genre string, square 600x600 JPEG art, relative M3U paths.
4. When you add a tag or an output, state the support matrix in the commit or
   PR: what Namida does with it, what Samsung Music does with it, what a
   generic Android player does with it.

## Never

- Write to a source folder. Sources are read-only; every stage writes to
  `cache/`, and only `write_tags` writes audio, as copies into `out/_all`.
- Commit `hints.tsv`, `review/`, `cache/`, `out/`, `config/secrets.json`,
  `config/config.yaml`, or any audio, artwork or lyrics. They describe a
  personal library and are gitignored.
- Invent metadata. A blank field beats a wrong one, and an unparseable hint is
  a note, not a title.
- Run Essentia in a thread or across a fork. It segfaults and corrupts the
  heap. Give it a separate interpreter.
- Truncate a fingerprint to use as a cache key. Prefixes collide, silently.
- Run `dedupe` before `analyze`. dedupe picks the copy to keep by measured
  spectral cutoff, so it needs the current `analysis.json`.
- Cache a failed request as a real answer.
- Commit or push unless asked. When asked, include only that task's changes.

## Ask first

- Adding a dependency, an API, or a new pipeline stage.
- Changing a tag's format, or what a stage writes to `cache/`.
- Anything with two plausible readings. Ask before you edit, do not pick one.

Ask means ask. Do not quietly work around the question.

## Commands

```bash
./.venv/bin/python run.py "/path/to/music"     # whole pipeline
run.py DIR --dry-run                           # print the plan, do nothing
run.py DIR --from webmatch --skip verify_lyrics
ruff check .                                   # what CI runs
```

Every stage is idempotent and reads its own cache, so re-running costs nothing
and is the normal way to work. There is no test suite: say so rather than
implying a change is verified.

## How I want you to work

- Deliver what was asked, at the scope asked. Make routine judgment calls
  yourself, and ask when two readings would mean materially different work. If
  a better approach exists, say so in a sentence and carry on with the task as
  asked rather than quietly widening it.
- Explain what you changed and why at the end. The why is the point.
- Match a document's length to what it needs. No filler sections, no redundant
  summaries.
- Delegate to a subagent only for genuinely independent, wide investigation.
  Never to verify your own work.
- Never use em dashes or en dashes, anywhere: chat, code comments, docs, commit
  messages, PR text. Use a comma, a colon, parentheses, or rewrite the line.
- Markdown here is hard-wrapped at 78 columns. Match it, do not reflow a file
  you are editing.
- Conventional commits scoped to the stage: `fix(dedupe): ...`. Say what
  changed, then why.
