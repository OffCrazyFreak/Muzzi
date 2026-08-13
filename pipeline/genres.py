"""Canonicalise Last.fm tags into a small, browsable genre set.

Last.fm tags are folksonomy: "Melodic Death Metal", "Belarusian Metal",
"Serbia", "Croatian" all come back as "genres". Writing three of them per
track produced 113 distinct genre strings across 148 files -- almost one per
track, which makes the phone's genre list useless.

So: drop nationality/mood noise, map the rest onto a small parent set, and
write exactly ONE genre. The full list is kept in a TXXX frame for anything
that wants detail.
"""
import re
import unicodedata

# Nationality, country and non-genre chatter that Last.fm returns as tags.
_NOISE = re.compile(
    r"^(croatia\w*|serbia\w*|bosnia\w*|balkan\w*|yugoslav\w*|slovenia\w*|"
    r"macedonia\w*|montenegr\w*|austral\w*|canad\w*|american|british|english|"
    r"german|swedish|norwegian|finnish|french|italian|spanish|belarus\w*|"
    r"russian|ukrain\w*|polish|dutch|irish|scottish|japanese|korean|"
    r"female vocalist\w*|male vocalist\w*|singer.songwriter|instrumental|"
    r"\d{2,4}s?|seen live|favou?rite\w*|awesome|beautiful|love|catchy|"
    r"summer|chill|christian|cover|soundtrack|ex.?yu|srbija|hrvatska|"
    r"nederlandstalig|noisy.*|domaci|doma[cć]i)$", re.I)

# Ordered: the first match wins, so specific beats general
# ("melodic death metal" must hit Metal before it hits nothing, and
#  "pop punk" must hit Punk before Pop).
_RULES = [
    (r"drum\s*(and|n|&)\s*bass|dnb|jungle|breakcore", "Drum and Bass"),
    (r"dubstep|riddim", "Dubstep"),
    (r"hardstyle|gabber|hardcore techno|happy hardcore", "Hard Dance"),
    (r"psytrance|trance", "Trance"),
    (r"techno", "Techno"),
    (r"house|garage", "House"),
    (r"metal|metalcore|grindcore", "Metal"),
    (r"punk", "Punk"),
    (r"rap|hip.?hop|trap|drill|grime", "Hip-Hop"),
    # "R&B / Soul" was one string, but players split a genre on "&" and on
    # "/", so a phone showed a genre called "R" next to one called "Soul".
    (r"r&b|rnb|soul|motown|funk", "R&B"),
    (r"reggae|dancehall|ska|dub\b", "Reggae"),
    (r"jazz|blues|swing", "Jazz and Blues"),
    (r"classical|orchestral|opera", "Classical"),
    (r"country|americana|bluegrass", "Country"),
    (r"folk|acoustic|turbo.?folk|narodna|cajke|[cč]ajke|starogradsk\w*", "Folk"),
    (r"edm|electro|elektronic|synth|ambient|idm|downtempo|lo.?fi", "Electronic"),
    (r"indie", "Indie"),
    (r"rock|grunge|shoegaze", "Rock"),
    (r"dance|disco|eurodance", "Dance"),
    (r"pop", "Pop"),
    (r"alternative", "Alternative"),
    (r"easy listening|lounge", "Easy Listening"),
    (r"noise|noisecore", "Metal"),
]
_COMPILED = [(re.compile(p, re.I), name) for p, name in _RULES]


# ---------------------------------------------------------------- the gate
# The rules above canonicalise free text, but two sources bypass them: the
# Deezer genre and the shared-Last.fm-tag fallback in scenes.py both write
# whatever the API returned, verbatim. That is how 39 distinct genre strings
# accumulated across 1584 files, ten of them covering a single track each.
#
# So this is a whitelist, not a blocklist. A blocklist has to guess what an
# API will say next; a whitelist cannot be surprised. Everything reaching a
# file is one of these strings or nothing at all.
GENRES = frozenset({
    "Pop", "Rock", "Punk", "Metal", "Folk", "Indie", "Alternative",
    "Hip-Hop", "R&B", "Electronic", "House", "Techno", "Trance",
    "Drum and Bass", "Dubstep", "Reggae", "Jazz and Blues", "Country",
    "Classical", "Latin",
    # Scene names from config/scenes.json. They are genres as far as a
    # player is concerned, so they belong in the same vocabulary.
    "Croatian Trap", "Croatian Trash", "Ex-YU", "Turbofolk",
})

# Names seen in the wild that mean something already in GENRES. Folding them
# is what turns five rap menu entries into one.
_MERGE = {
    "rap": "Hip-Hop",
    "trap": "Hip-Hop",
    "croatian rap": "Hip-Hop",
    "serbian rap": "Hip-Hop",
    "hip hop": "Hip-Hop",
    "dance": "Electronic",
    "edm": "Electronic",
    # Hardstyle, gabber and happy hardcore. The rule above emits this name,
    # so without the merge every one of those tags would silently vanish.
    "hard dance": "Electronic",
    "electro house": "Electronic",
    "eurodance": "Electronic",
    # Discogs styles. Both are Pop with a regional adjective on the front,
    # and both are emitted by the _DISCOGS table in scenes.py, so without
    # these two lines that table would lose every answer it gives.
    "ethno pop": "Pop",
    "europop": "Pop",
    "hard rock": "Rock",
    "progressive rock": "Rock",
    "pop punk": "Punk",
    "progressive metal": "Metal",
    # Novi val is the Yugoslav new wave, so it is the Ex-YU era by another
    # name rather than a genre of its own.
    "novi val": "Ex-YU",
    "r&b soul": "R&B",
    "rnb": "R&B",
    # Same reason as R&B: a player splitting on "&" turned one
    # genre into two. "and" is unambiguous and just as standard.
    "drum & bass": "Drum and Bass",
    "dnb": "Drum and Bass",
    "jazz & blues": "Jazz and Blues",
    # Deezer's Croatian genre names, which arrive alongside the English ones.
    "dzez": "Jazz and Blues",
    "latino": "Latin",
    "latin pop": "Latin",
    "soul": "R&B",
}

# Names that describe a mood, a format or the listener. No parent to merge
# into, so they produce no genre: a blank field beats a wrong one.
_DROP = {"easy listening", "lounge", "singer songwriter", "singer-songwriter",
         "experimental", "soundtrack", "other"}


def _key(s):
    """Fold a genre name for lookup: case, punctuation, spacing, diacritics.

    Deezer answers in the account's language, so a Croatian account gets
    "Dzez" for jazz. Without the NFKD pass the z-caron is simply deleted and
    the key comes out "d ez", which matches nothing and looks like a missing
    entry rather than a broken one.
    """
    s = unicodedata.normalize("NFKD", (s or "").strip().lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9&+]+", " ", s).strip()


_BY_KEY = {_key(g): g for g in GENRES}
_MERGE = {_key(k): v for k, v in _MERGE.items()}
_DROP = {_key(d) for d in _DROP}

# A merge target that is not itself allowed would silently drop everything
# routed through it, which is exactly the kind of quiet failure this file
# exists to stop. Fail at import instead, where it is obvious.
for _src, _dst in _MERGE.items():
    if _dst not in GENRES:
        raise ValueError(f"genres: {_src!r} merges into {_dst!r}, "
                         f"which is not in GENRES")


def allow(name):
    """-> the whitelisted genre this name means, or None.

    The single place that decides what may be written. Call it on anything
    headed for a TCON frame, whatever produced it.
    """
    k = _key(name)
    if not k or k in _DROP:
        return None
    got = _MERGE.get(k) or _BY_KEY.get(k)
    if got:
        return got
    # No exact name. Before giving up, run the canonicaliser above over the
    # string itself: an API answers "alternative rock", "dancefloor drum and
    # bass" and "festival progressive house", which are the genres already in
    # GENRES with an adjective bolted on. Enumerating those is a losing game,
    # and the rules already know how to read them. Anything they cannot read
    # is still nothing, which is the point.
    for rx, mapped in _COMPILED:
        if rx.search(k):
            merged = _MERGE.get(_key(mapped)) or _BY_KEY.get(_key(mapped))
            if merged:
                return merged
    return None


# Borrowed from beets-autogenre: when no tag maps, the title itself sometimes
# names the genre ("... (Techno Remix)", "Balkan Trap"). Weak evidence, so it
# is only consulted after every tag has failed.
def from_title(title):
    if not title:
        return None
    for rx, name in _COMPILED:
        if rx.search(title):
            return name
    return None


def canonical(tags, title=None):
    """Return (primary_genre or None, cleaned_tag_list).

    The primary is already through allow(), so callers do not have to gate it
    again. write_tags writes this one directly when no identity was resolved,
    which is a second way a genre reaches a file.
    """
    cleaned = [t.strip() for t in (tags or []) if t and not _NOISE.match(t.strip())]
    for tag in cleaned:
        # An exact allowed name is already the answer, so it must not be run
        # through the rules: "Turbofolk" matches the folk rule and came out as
        # "Folk", "Croatian Trap" matches the rap rule and came out as
        # "Hip-Hop", and "Ex-YU", "Croatian Trash" and "Latin" matched no rule
        # at all and were dropped. Five of the twenty-four names the whitelist
        # exists to protect, destroyed by the step meant to canonicalise them.
        got = allow(tag)
        if got:
            return got, cleaned
        for rx, name in _COMPILED:
            if not rx.search(tag):
                continue
            got = allow(name)
            # A tag that maps to a dropped name is not an answer, so keep
            # looking. Returning here would let one "lounge" tag suppress the
            # "rock" tag sitting behind it.
            if got:
                return got, cleaned
    # No tag mapped. Try the title before giving up.
    guessed = allow(from_title(title))
    if guessed:
        return guessed, cleaned
    # Still nothing: write NOTHING rather than an arbitrary folksonomy tag.
    # "Noisy Bitch" as a genre is worse than a blank field.
    return None, cleaned
