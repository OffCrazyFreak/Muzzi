# AGENTS.md

Conventions for anyone (human or agent) working on Muzzi.
See `README.md` for how to run it, `PLAN.md` for the reasoning.

## Target players

The library is consumed on Android. Two players are the daily drivers:

- **Namida** - primary. Richest metadata support, so it sets the ceiling: if a
  field is worth having, tag it the way Namida reads it.
- **Samsung Music** - primary. Strict and limited, so it sets the floor:
  no BPM field, reads `.lrc` sidecars only (not embedded lyrics), limited
  container support (mp3, m4a, mp4, 3gp, 3ga, ogg, wav, flac).

Everything else (Poweramp, Musicolet, Retro Music, Symfonium, AIMP, YouTube
Music offline, stock OEM players) must still work. Not everyone uses Namida.

## Rules that follow

1. **Tag for Namida, degrade for Samsung Music.** Never pick a format only one
   of them understands when a format both understand exists.
2. **No feature may depend on a single player.** If Namida is the only one that
   reads something, add a fallback that everyone reads (embedded lyrics plus
   `.lrc` sidecar; BPM tag plus BPM in the filename).
3. **Metadata lives inside the file.** No sidecar database, no app-specific
   config. The only sidecar is `.lrc`, and only because Samsung Music needs it.
4. **Prefer widely supported forms.** ID3v2.3/2.4 standard frames over custom
   ones, one genre string over multi-value, square 600x600 JPEG cover art,
   relative M3U paths (with `export.py --absolute` for players that ignore them).
5. **When adding a tag or output, state the support matrix** in the commit or
   PR: what Namida does with it, what Samsung Music does with it, what a
   generic Android player does with it.
