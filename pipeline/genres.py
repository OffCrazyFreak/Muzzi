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
    (r"drum\s*(and|n|&)\s*bass|dnb|jungle|breakcore", "Drum & Bass"),
    (r"dubstep|riddim", "Dubstep"),
    (r"hardstyle|gabber|hardcore techno|happy hardcore", "Hard Dance"),
    (r"psytrance|trance", "Trance"),
    (r"techno", "Techno"),
    (r"house|garage", "House"),
    (r"metal|metalcore|grindcore", "Metal"),
    (r"punk", "Punk"),
    (r"rap|hip.?hop|trap|drill|grime", "Hip-Hop"),
    (r"r&b|rnb|soul|motown|funk", "R&B / Soul"),
    (r"reggae|dancehall|ska|dub\b", "Reggae"),
    (r"jazz|blues|swing", "Jazz & Blues"),
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
    """Return (primary_genre or None, cleaned_tag_list)."""
    cleaned = [t.strip() for t in (tags or []) if t and not _NOISE.match(t.strip())]
    for tag in cleaned:
        for rx, name in _COMPILED:
            if rx.search(tag):
                return name, cleaned
    # No tag mapped. Try the title before giving up.
    guessed = from_title(title)
    if guessed:
        return guessed, cleaned
    # Still nothing: write NOTHING rather than an arbitrary folksonomy tag.
    # "Noisy Bitch" as a genre is worse than a blank field.
    return None, cleaned
