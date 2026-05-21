"""Unified text normalization for music metadata matching.

All matching across Earwrym — MusicBrainz, ListenBrainz, Navidrome, Lidarr,
RYM, Last.fm, qBittorrent torrent names — flows through this module.

Two-pass strategy:
  1. normalize(text) — conservative: safe transforms that never discard meaning
  2. normalize(text, aggressive=True) — strips featured artists, edition
     suffixes, articles, and expands abbreviations

Typical use: try conservative match first, fall back to aggressive.
"""

import re
import unicodedata

_LIGATURES = str.maketrans({
    "Æ": "AE", "æ": "ae",
    "Œ": "OE", "œ": "oe",
    "ß": "ss",
    "Ð": "D",  "ð": "d",
    "Þ": "Th", "þ": "th",
    "Ĳ": "IJ", "ĳ": "ij",
    "Ł": "L",  "ł": "l",
    "Ŋ": "N",  "ŋ": "n",
})

_FULLWIDTH_OFFSET = 0xFEE0

_FEAT_RE = re.compile(
    r'\s*[\(\[]*\s*(?:feat\.?|ft\.?|featuring|with)\s+.*?[\)\]]*\s*$',
    re.IGNORECASE
)
_FEAT_MID_RE = re.compile(
    r'\s+(?:feat\.?|ft\.?|featuring|with)\s+.*',
    re.IGNORECASE
)

_EDITION_RE = re.compile(
    r'\s*[\(\[]\s*(?:deluxe|remaster(?:ed)?|expanded|anniversary|bonus|special|'
    r'collector|limited|super\s*deluxe|standard|original|edition|'
    r'\d+(?:th|st|nd|rd)\s*anniversary|japan(?:ese)?|uk|us|'
    r'complete|ultimate|definitive|mono|stereo|'
    r'\d{4}\s*(?:re)?master).*?[\)\]]\s*',
    re.IGNORECASE
)

_TRAILING_SUFFIX_RE = re.compile(
    r'\s*[-–—]\s*(?:deluxe|remaster(?:ed)?|expanded|anniversary|bonus|special|'
    r'collector|limited|super\s*deluxe|standard|original|'
    r'\d+(?:th|st|nd|rd)\s*anniversary)\s*(?:edition|version)?\s*$',
    re.IGNORECASE
)

_RELEASE_TYPE_RE = re.compile(
    r'\s*\b(?:ep|lp|single)\s*$',
    re.IGNORECASE
)

_ARTICLES_RE = re.compile(r'^(?:the|a|an|les?|la|los|las|el|der|die|das)\s+', re.IGNORECASE)

_ABBREVS = {
    "vol": "volume",
    "pt": "part",
    "mk": "mark",
    "nr": "number",
    "st": "saint",
}

_WHITESPACE_RE = re.compile(r'\s+')
_NON_ALNUM_RE = re.compile(r'[^\w]')

_QUOTES = str.maketrans({
    "‘": "'", "’": "'",
    "“": '"', "”": '"',
    "′": "'", "″": '"',
    "«": '"', "»": '"',
    "‹": "'", "›": "'",
})

_DASHES = str.maketrans({
    "–": "-", "—": "-",
    "―": "-", "‒": "-",
    "‐": "-", "﹘": "-",
    "﹣": "-", "－": "-",
})


def _fullwidth_to_halfwidth(text):
    out = []
    for ch in text:
        cp = ord(ch)
        if 0xFF01 <= cp <= 0xFF5E:
            out.append(chr(cp - _FULLWIDTH_OFFSET))
        elif cp == 0x3000:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def normalize(text, aggressive=False):
    """Normalize text for music metadata matching.

    Conservative mode (default): fullwidth conversion, ligature expansion,
    NFKD decomposition, lowercase, dash/quote normalization, &/+ → and,
    whitespace collapse, strip non-alphanumeric.

    Aggressive mode: also strips featured artists, edition suffixes,
    release types, leading articles, and expands abbreviations.
    """
    if not text:
        return ""

    text = _fullwidth_to_halfwidth(text)
    text = text.translate(_LIGATURES)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = text.lower()
    text = text.translate(_DASHES)
    text = text.translate(_QUOTES)
    text = text.replace("…", "...")
    text = text.replace("&", " and ").replace("+", " and ")
    text = _WHITESPACE_RE.sub(" ", text).strip()

    if aggressive:
        text = _FEAT_RE.sub("", text)
        text = _FEAT_MID_RE.sub("", text)
        text = _EDITION_RE.sub("", text)
        text = _TRAILING_SUFFIX_RE.sub("", text)
        text = _RELEASE_TYPE_RE.sub("", text)
        text = _ARTICLES_RE.sub("", text)
        words = text.split()
        words = [_ABBREVS.get(w, w) for w in words]
        text = " ".join(words)

    text = _NON_ALNUM_RE.sub("", text)
    return text


def normalize_artist(text):
    """Normalize an artist name — always strips featured artists."""
    if not text:
        return ""
    text = _fullwidth_to_halfwidth(text)
    text = text.translate(_LIGATURES)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = text.lower()
    text = text.translate(_DASHES)
    text = text.translate(_QUOTES)
    text = text.replace("&", " and ").replace("+", " and ")
    text = _WHITESPACE_RE.sub(" ", text).strip()
    text = _FEAT_RE.sub("", text)
    text = _FEAT_MID_RE.sub("", text)
    text = _NON_ALNUM_RE.sub("", text)
    return text


def music_match(text_a, text_b):
    """Two-pass match: conservative first, then aggressive.

    Returns True if the texts match under either normalization level.
    """
    na = normalize(text_a)
    nb = normalize(text_b)
    if na == nb:
        return True
    return normalize(text_a, aggressive=True) == normalize(text_b, aggressive=True)


def tokenize(text):
    """Split text into content tokens for fuzzy torrent matching."""
    _STOPWORDS = {"the", "a", "an", "of", "and", "in", "on", "at", "to", "for", "is", "by"}
    words = re.sub(r"[^\w\s]", "", text.lower()).split()
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}
