from app.draft.generator import _strip_signoff_paragraph, _to_html_body


def test_strips_thanks_first_name():
    body = "Hi Sarah,\n\nLine one.\n\nLine two.\n\nThanks,\nChase"
    result = _strip_signoff_paragraph(body)
    assert "Thanks" not in result
    assert "Chase" not in result
    assert "Line two." in result


def test_strips_best_regards_variants():
    for signoff in ["Thanks,\nChase", "Best,\nChase", "Cheers,\nChase",
                    "Regards,\nChase", "Sincerely,\nChase"]:
        body = f"Hi,\n\nbody.\n\n{signoff}"
        result = _strip_signoff_paragraph(body)
        assert "Chase" not in result, signoff


def test_keeps_body_when_no_signoff():
    body = "Hi Sarah,\n\nLine one.\n\nLine two."
    assert _strip_signoff_paragraph(body) == body


def test_html_conversion_appends_formal_sig():
    body = "Hi Sarah,\n\nLine one.\n\n- bullet a\n- bullet b\n\nThanks,\nChase"
    out = _to_html_body(body)
    # The plain single-paragraph signoff (no email link) must be gone — i.e.
    # we should NOT see a paragraph that's only "Thanks,<br>Chase</p>" with no links.
    # Detect this with a regex on the rendered HTML.
    import re
    bare_signoff = re.search(r"<p>Thanks,<br>Chase\s*</p>", out)
    assert bare_signoff is None
    # Formal sig present (uses promptproofreading.com link with mailto)
    assert "promptproofreading.com" in out.lower()
    assert "mailto:" in out
    # Bullets rendered as <ul><li>
    assert "<ul>" in out and "<li>bullet a</li>" in out
