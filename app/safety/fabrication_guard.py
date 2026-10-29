"""Fabrication guard — the last line between a model claim and an outbound email.

v2 (`validate_findings`) checks each audit finding's evidence in code:
  * quote  -> must appear verbatim on the fetched page(s): case-sensitive, after
              visual-only normalization (curly vs straight quotes, nbsp, ...).
  * fact   -> the fact id must exist in the facts computed for this exact page.
  * finding types that assert something is MISSING or BROKEN (no booking, not
    mobile friendly, no https, stale content, ...) are only accepted with a
    matching fact — absence can't be quoted, so a quote "proving" it is invented.
  * typos  -> error_text must be inside the quote and the correction must differ.
Anything that fails is dropped (and logged by the caller), never "demoted".

`filter_issues` / `has_verified_quote` are the v1 API, kept for older callers.
"""
import re
from typing import Iterable

from app.audit.extractor import quote_in_html
from app.textnorm import normalize_text, contains_verbatim

# Finding types that require a fact whose id starts with one of these prefixes.
FACT_REQUIRED = {
    "phone_not_tappable": ("phone_not_tappable",),
    "pdf_menu": ("pdf_menu",),
    "no_https": ("no_https", "cert_expired", "cert_invalid", "cert_expiring_soon"),
    "no_mobile_layout": ("desktop_layout_on_phone", "no_viewport_meta",
                         "mobile_horizontal_scroll", "small_text_mobile"),
    "third_party_ordering": ("third_party_ordering:",),
    "no_booking_link": ("no_booking_link_found",),
    "no_order_link": ("no_order_link_found",),
    "dead_links": ("dead_links:",),
    "slow_mobile": ("heavy_page_mobile", "slow_main_content"),
    "stale_content": ("stale_copyright",),
}
QUOTE_MIN_WORDS = 2
QUOTE_MAX_WORDS = 25

# Grammar slips a dictionary can't see (every word is spelled correctly).
_CONSONANT_SOUND_VOWEL_START = ("uni", "use", "usu", "uti", "eu", "one", "once", "ur")
_VOWEL_SOUND_H = ("hour", "honest", "honor", "honour", "heir")
_GRAMMAR_PATTERNS = (
    re.compile(r"\byour (welcome|the best|open|closed)\b", re.I),
    re.compile(r"\b(should|could|would|must) of\b", re.I),
    re.compile(r"\balot\b", re.I),
    re.compile(r"\b(\w{2,}) \1\b", re.I),                       # doubled word
)
_spell = None


def _speller():
    global _spell
    if _spell is None:
        try:
            from spellchecker import SpellChecker
            _spell = SpellChecker()
        except ImportError:  # dependency missing: only grammar slips can pass
            _spell = False
    return _spell


def _article_slip(err: str) -> bool:
    m = re.search(r"\b(a|an) ([a-z]+)", err, re.I)
    if not m:
        return False
    art, word = m.group(1).lower(), m.group(2).lower()
    if art == "a":
        return word[0] in "aeiou" and not word.startswith(_CONSONANT_SOUND_VOWEL_START)
    return word[0] not in "aeiou" and not word.startswith(_VOWEL_SOUND_H)


def typo_rejection_reason(error_text: str, correction: str) -> str | None:
    """None if the claimed typo is real, else why it was rejected.

    Accepted when (a) it's a known grammar slip, or (b) an offline dictionary
    flags a word in error_text as misspelled AND every word of the correction is
    a real word. This stops "typos" that are brand names, dish names, regional
    spellings, or the model's taste.
    """
    if _article_slip(error_text) or any(p.search(error_text) for p in _GRAMMAR_PATTERNS):
        return None
    spell = _speller()
    if not spell:
        return "typo not verifiable (spell-check dictionary unavailable) and not a known grammar slip"
    err_words = [w.lower() for w in re.findall(r"[A-Za-z]{3,}", error_text)]
    corr_words = [w.lower() for w in re.findall(r"[A-Za-z]{2,}", correction)]
    misspelled = spell.unknown(err_words)
    if not misspelled:
        return "typo claim rejected: every word in error_text is a real word"
    if corr_words and spell.unknown(corr_words):
        return "typo correction contains non-dictionary words"
    for word in misspelled:
        if _regional_variant(word, corr_words):
            return f"{word!r} is a regional spelling, not a typo"
        # A real typo is a near miss: the correction must be one of the
        # dictionary's own suggestions (stops "Pho" -> "Phone" style claims).
        if not (spell.candidates(word) or set()) & set(corr_words):
            return f"correction for {word!r} is not a near-miss spelling fix"
    return None


def _regional_variant(word: str, corr_words: list[str]) -> bool:
    """colour/color, organise/organize, centre/center, travelled/traveled."""
    pairs = (("our", "or"), ("ise", "ize"), ("isation", "ization"), ("re", "er"),
             ("lled", "led"), ("lling", "ling"), ("ogue", "og"))
    for c in corr_words:
        for a, b in pairs:
            if (word.endswith(a) and c == word[: -len(a)] + b) or \
               (word.endswith(b) and c == word[: -len(b)] + a):
                return True
    return False


def validate_findings(findings: list[dict], *, quote_source_text: str,
                      facts: dict[str, str],
                      sources: dict[str, str] | None = None) -> tuple[list[dict], list[dict]]:
    """Return (kept, dropped). Kept findings get confidence='verified'."""
    kept: list[dict] = []
    dropped: list[dict] = []

    def drop(f: dict, reason: str) -> None:
        dropped.append({"finding": f, "reason": reason})

    for f in findings:
        kind = f.get("evidence_kind")
        ftype = f.get("type", "other")
        if kind == "fact":
            fid = f.get("fact_id", "")
            if fid not in facts:
                drop(f, f"unknown fact id {fid!r}")
                continue
            prefixes = FACT_REQUIRED.get(ftype)
            if prefixes and not fid.startswith(prefixes):
                drop(f, f"fact {fid!r} does not support finding type {ftype!r}")
                continue
            f = {**f, "quote": "", "fact_detail": facts[fid]}
        elif kind == "quote":
            if ftype in FACT_REQUIRED:
                drop(f, f"{ftype!r} asserts absence/breakage; needs a fact, not a quote")
                continue
            quote = normalize_text(f.get("quote", ""))
            n_words = len(quote.split())
            if not QUOTE_MIN_WORDS <= n_words <= QUOTE_MAX_WORDS:
                drop(f, f"quote length {n_words} words outside {QUOTE_MIN_WORDS}-{QUOTE_MAX_WORDS}")
                continue
            if "..." in quote:
                drop(f, "quote contains an ellipsis (not contiguous)")
                continue
            if not contains_verbatim(quote_source_text, quote):
                drop(f, "quote not found verbatim on the page")
                continue
            if ftype == "typo":
                err = normalize_text(f.get("error_text", ""))
                corr = normalize_text(f.get("correction", ""))
                if not err or err not in quote:
                    drop(f, "typo error_text missing or not inside the quote")
                    continue
                if not corr or corr.lower() == err.lower():
                    drop(f, "typo without a real correction")
                    continue
                why_not = typo_rejection_reason(err, corr)
                if why_not:
                    drop(f, why_not)
                    continue
            location = next((name for name, text in (sources or {}).items()
                             if text and contains_verbatim(text, quote)), "")
            f = {**f, "quote": quote, "fact_id": "", "quote_location": location}
        else:
            drop(f, f"invalid evidence_kind {kind!r}")
            continue
        kept.append({**f, "confidence": "verified",
                     "why_hurts": f.get("customer_moment", "")})
    return kept, dropped


def has_verified_evidence(issues: Iterable[dict]) -> bool:
    return any((i.get("confidence") or "").lower() == "verified"
               and ((i.get("quote") or "").strip() or (i.get("fact_id") or "").strip())
               for i in issues)


# ---- v1 API (kept for compatibility) ------------------------------------------

def filter_issues(issues: list[dict], html_path: str) -> tuple[list[dict], list[dict]]:
    """v1: verified quotes must be on the page (now case-sensitive); else demoted."""
    kept: list[dict] = []
    dropped: list[dict] = []
    for issue in issues:
        conf = (issue.get("confidence") or "").lower()
        quote = (issue.get("quote") or "").strip()
        if conf == "verified":
            if not quote:
                dropped.append({"issue": issue, "reason": "verified-without-quote"})
                continue
            if not quote_in_html(quote, html_path):
                kept.append({**issue, "confidence": "likely", "demoted_from": "verified",
                             "demote_reason": "quote not found in cached HTML"})
            else:
                kept.append(issue)
        elif conf in ("likely", "unknown"):
            kept.append(issue)
        else:
            dropped.append({"issue": issue, "reason": f"invalid-confidence:{conf}"})
    return kept, dropped


def has_verified_quote(issues: Iterable[dict]) -> bool:
    return any((i.get("confidence") or "").lower() == "verified"
               and (i.get("quote") or "").strip()
               for i in issues)
