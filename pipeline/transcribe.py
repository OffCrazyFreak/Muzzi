#!/usr/bin/env python3
"""Ask Whisper what a file says, and retry when the filter ate the singing.

Both stages that listen to the audio, `verify_lyrics` and `lyric_align`, ran
Whisper behind Silero's voice-activity filter. The filter is trained on
speech. On a voice over instrumentation it frequently decides there is no
voice at all, and Whisper is then handed silence and answers accordingly.

Measured, same file and model, only the filter changing:

    GRŠE - OČE                              0 words -> 297
    Dara Bubamara - Opasan                  0 words -> 104
    Legica                                  7 words -> 122
    Sako Polumenta - E Što Nisam Sunce     11 words ->  54

It costs the Balkan half of the library most. `lyric_align` runs only on
tracks that already have a synced sheet, so none of them is an instrumental,
and it heard nothing on 17.8% of Balkan tracks against 2.3% of the rest.

Turning the filter off everywhere is the wrong fix: without it a full pass
costs 30 to 100 seconds against 1 to 20, and most of the library never needed
it. So the filter is used first and dropped only when the answer is
implausibly thin, which is the same shape as asking a second lyric source only
when the first one's answer has no timings. Only the starved tracks pay.

One helper rather than two call sites, because the two stages held different
`min_silence_duration_ms` values for the same job and neither comment said
why.
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

# Below this many words, assume we were handed silence rather than a track
# with nothing to say. Three is the same floor verify_lyrics already uses to
# call something instrumental, so a genuine instrumental still reads as one
# and simply pays for a second look.
MIN_PLAUSIBLE_WORDS = 3

# What the filter is told when it is used at all. One value now, rather than
# 400ms in one stage and 700ms in the other for the same question.
VAD_PARAMS = {"min_silence_duration_ms": 400, "speech_pad_ms": 200}


def listen(model, path, count, **kw):
    """-> (segments, info, used_vad). Filter first, then without it.

    `count` turns the segment iterator into a number of usable words, and is
    the caller's because only the caller knows what it is going to keep:
    `lyric_align` wants word timestamps, `verify_lyrics` wants text. It is
    called on the first attempt whatever happens, since faster-whisper's
    segments are a generator and nothing is transcribed until they are drawn.

    Returns what the second attempt found even when that is also nothing, so a
    track the model genuinely cannot hear is reported as such rather than as a
    filter problem. Those exist and no setting fixes them: Croatian and
    Serbian are tier 3 for Whisper before the singing is taken into account.
    """
    segs, info = model.transcribe(path, vad_filter=True,
                                  vad_parameters=dict(VAD_PARAMS), **kw)
    got, n = count(segs)
    if n >= MIN_PLAUSIBLE_WORDS:
        return got, info, True

    segs, info = model.transcribe(path, vad_filter=False, **kw)
    got, _n = count(segs)
    return got, info, False
