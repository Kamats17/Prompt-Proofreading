"""Text normalization shared by the fabrication guard, the QA gate and extraction.

Quotes are compared CASE-SENSITIVELY after this normalization. Normalization only
erases differences a reader can't see (curly vs straight quotes, non-breaking
spaces, ellipsis glyphs, Unicode compatibility forms, runs of whitespace). It never
changes letters, so "a order" can't be "matched" by "an order", and "WELCOME" can't
be matched by "Welcome".
"""
import re
import unicodedata

_CHAR_MAP = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'", "´": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
    "«": '"', "»": '"',
    " ": " ", " ": " ", " ": " ", " ": " ", "　": " ",
    "​": "", "‌": "", "‍": "", "﻿": "",
})
_WS = re.compile(r"\s+")


def normalize_text(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)   # also turns "…" into "..."
    s = s.translate(_CHAR_MAP)
    return _WS.sub(" ", s).strip()


def contains_verbatim(haystack: str, needle: str) -> bool:
    """Case-sensitive containment after visual-only normalization."""
    n = normalize_text(needle)
    return bool(n) and n in normalize_text(haystack)
