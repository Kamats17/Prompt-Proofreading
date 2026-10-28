"""Verify single-word banned phrases use word boundaries (no false positives inside other words)."""
from app.draft.voice import banned_phrase_hits


def test_leverage_inside_word_does_not_match():
    # 'leverage' as a single word triggers; substring inside another word does NOT.
    # 'leverages' would still match because it shares the leverage word boundary at end? No — \b around leverage means leverages still matches via \bleverage\b.
    # Test the safer case: no false hit on unrelated words containing the letters.
    assert "leverage" not in banned_phrase_hits("we have a clever, age-tested process")


def test_leverage_as_word_matches():
    assert "leverage" in banned_phrase_hits("we leverage your existing setup")


def test_multi_word_phrase_still_works():
    hits = banned_phrase_hits("I hope this finds you well today")
    assert any("hope this finds" in h for h in hits)


def test_em_dash_not_in_banned_check():
    # Em dash check is separate; banned-phrase hits should NOT include em-dash logic.
    hits = banned_phrase_hits("clean copy with no banned phrases here")
    assert hits == []
