"""QA gate v2: style checks (blockers for model output, warnings after a human
edit) and hard checks (verbatim quotes, evidence, DNC, recontact, grade)."""
import json
import pytest

from app.db.migrate import migrate, connect
from app.db.models import DncEntry, Prospect, Audit, Draft
from app.db.repo import add_dnc, upsert_prospect, insert_audit, insert_draft
from app.draft.qa_gate import run_qa
from app.draft.voice import pick_subject

COMPANY = "Bob's Diner"
ISSUES = json.dumps([
    {"type": "typo", "evidence_kind": "quote", "quote": "Call to place a order",
     "confidence": "verified", "service": "content", "severity": 2},
    {"type": "phone_not_tappable", "evidence_kind": "fact", "fact_id": "phone_not_tappable",
     "quote": "", "confidence": "verified", "service": "website", "severity": 3},
])
SUBJECT = pick_subject(COMPANY, "phone_not_tappable")

GOOD_BODY = (
    "Hi Sarah,\n\n"
    "I pulled up Bob's Diner's site on my phone and the number at the top can't be tapped "
    "to call, so a hungry customer has to copy it by hand.\n\n"
    "I'd make it a one-tap call button and fix the line \"Call to place a order\" while I'm "
    "in there. I do this kind of fix for local restaurants all the time.\n\n"
    "Want me to send a free mockup of the header with tap-to-call?\n\n"
    "Thanks,\nChase"
)


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "qa.db"
    migrate(db)
    c = connect(db)
    yield c
    c.close()


def _qa(conn, **kw):
    args = dict(company=COMPANY, prospect_email="sarah@bobsdiner.com",
                prospect_url="https://bobsdiner.com", subject=SUBJECT, body_text=GOOD_BODY,
                audit_issues_json=ISSUES, final_grade="A",
                used_quotes=["Call to place a order"], used_fact_ids=["phone_not_tappable"])
    args.update(kw)
    return run_qa(conn, **args)


def test_clean_draft_passes(conn):
    r = _qa(conn)
    assert r["pass"], r["blockers"]


def test_paraphrased_quote_blocked(conn):
    # Auto-correcting the typo inside quotes is a false claim about their site.
    r = _qa(conn, body_text=GOOD_BODY.replace("place a order", "place an order"),
            used_quotes=["Call to place an order"])
    assert not r["pass"]
    assert any("copied exactly" in b for b in r["blockers"])


def test_recased_quote_blocked(conn):
    r = _qa(conn, body_text=GOOD_BODY.replace("Call to place a order", "CALL TO PLACE A ORDER"),
            used_quotes=[])
    assert not r["pass"]


def test_single_quoted_paraphrase_blocked(conn):
    body = GOOD_BODY.replace("I'd make it", "Your site says 'we have the best food in town'. I'd make it")
    r = _qa(conn, body_text=body)
    assert any("copied exactly" in b for b in r["blockers"])


def test_curly_apostrophe_quote_still_matches(conn):
    issues = json.dumps([{"type": "other", "evidence_kind": "quote", "quote": "We're open late",
                          "confidence": "verified", "service": "website", "severity": 1}])
    body = GOOD_BODY.replace("\"Call to place a order\"", "“We’re open late”")
    r = _qa(conn, body_text=body, audit_issues_json=issues, used_quotes=[], used_fact_ids=[])
    assert r["checks"]["quoted_spans_verbatim"], r["blockers"]


def test_unverified_fact_blocked(conn):
    r = _qa(conn, used_fact_ids=["no_https"])
    assert any("used_fact_ids" in b for b in r["blockers"])


def test_no_evidence_blocked(conn):
    body = ("Hi Sarah,\n\nI looked at your site and think it could be better on phones for "
            "customers who want to reach you quickly during busy lunch hours and evenings.\n\n"
            "I do this kind of fix for local restaurants and could show you what I mean.\n\n"
            "Want me to send a free mockup over?\n\nThanks,\nChase")
    r = _qa(conn, body_text=body, used_quotes=[], used_fact_ids=[])
    assert any("no verified quote or fact" in b for b in r["blockers"])


def test_style_issues_block_model_output(conn):
    body = GOOD_BODY.replace("all the time.", "all the time! See promptpages.com")
    r = _qa(conn, body_text=body)
    assert not r["pass"]
    assert any("exclamation" in b for b in r["blockers"])
    assert any("link" in b.lower() for b in r["blockers"])


def test_style_issues_only_warn_after_human_edit(conn):
    short = ("Hi Sarah,\n\nYour number at the top can't be tapped to call on a phone.\n\n"
             "Want me to send a free mockup?\n\nThanks,\nChase")
    r = _qa(conn, body_text=short, subject="Hey Bob", human_edited=True,
            used_quotes=[], used_fact_ids=["phone_not_tappable"])
    assert r["pass"], r["blockers"]
    assert any("words" in w for w in r["warnings"])
    assert any("subject" in w for w in r["warnings"])


def test_banned_phrase_outside_quotes_blocks(conn):
    r = _qa(conn, body_text=GOOD_BODY.replace("I'd make it", "I hope this finds you well. I'd make it"))
    assert any("banned" in b for b in r["blockers"])


def test_banned_phrase_inside_verbatim_quote_is_fine(conn):
    issues = json.dumps([{"type": "other", "evidence_kind": "quote",
                          "quote": "Satisfaction guaranteed every time", "confidence": "verified",
                          "service": "content", "severity": 1}])
    body = GOOD_BODY.replace("\"Call to place a order\"", "\"Satisfaction guaranteed every time\"")
    r = _qa(conn, body_text=body, audit_issues_json=issues, used_quotes=[], used_fact_ids=[])
    assert r["checks"]["no_banned_phrases"], r["blockers"]


def test_em_dash_blocks(conn):
    r = _qa(conn, body_text=GOOD_BODY.replace("call, so", "call — so"))
    assert any("dash" in b for b in r["blockers"])


def test_template_brace_blocks(conn):
    r = _qa(conn, body_text=GOOD_BODY.replace("Hi Sarah", "Hi {name}"))
    assert any("placeholder" in b for b in r["blockers"])


def test_dnc_blocks(conn):
    add_dnc(conn, DncEntry(domain="bobsdiner.com", reason="customer"))
    r = _qa(conn)
    assert any("DNC" in b for b in r["blockers"])


def test_bad_recipient_blocks(conn):
    r = _qa(conn, prospect_email="owner@biz.com,partner@other.com")
    assert any("not a valid single email" in b for b in r["blockers"])


def test_grade_c_blocks(conn):
    r = _qa(conn, final_grade="C")
    assert any("grade" in b for b in r["blockers"])


def test_blocked_prospect_blocks(conn):
    r = _qa(conn, blocked_reason="opted_out")
    assert any("blocked" in b for b in r["blockers"])


def test_recontact_includes_own_history(conn):
    pid = upsert_prospect(conn, Prospect(company=COMPANY, url="https://bobsdiner.com",
                                         source="manual", email="sarah@bobsdiner.com"))
    aid = insert_audit(conn, Audit(prospect_id=pid, issues_json=ISSUES, final_grade="A"))
    insert_draft(conn, Draft(prospect_id=pid, audit_id=aid, subject="s", body_text="b",
                             body_html="<p>b</p>", status="pushed"))
    # The SAME prospect: a rejected-then-redrafted email must not go out again.
    r = _qa(conn, prospect_id=pid)
    assert any("recently contacted" in b for b in r["blockers"])


def test_in_flight_approval_counts_as_contacted(conn):
    a = upsert_prospect(conn, Prospect(company="Foo", url="https://foo.com", source="manual",
                                       email="a@foo.com"))
    aid = insert_audit(conn, Audit(prospect_id=a, issues_json=ISSUES, final_grade="A"))
    insert_draft(conn, Draft(prospect_id=a, audit_id=aid, subject="s", body_text="b",
                             body_html="<p>b</p>", status="approved"))
    b = upsert_prospect(conn, Prospect(company="Foo Downtown", url="https://foo.com/downtown",
                                       source="manual", email="b@foo.com"))
    r = _qa(conn, company="Foo Downtown", prospect_url="https://foo.com/downtown",
            prospect_email="b@foo.com", prospect_id=b, subject=pick_subject("Foo Downtown", "x"))
    assert any("recently contacted" in x for x in r["blockers"])


def test_followup_skips_subject_and_recontact(conn):
    body = ("Hi Sarah,\n\nOne more thing I noticed: the menu opens as a download on phones. "
            "Happy to send the free mockup if it helps.\n\nWant it?\n\nThanks,\nChase")
    r = _qa(conn, body_text=body, subject="Re: something", touch=2, used_quotes=[], used_fact_ids=[])
    assert "subject_allowed" not in r["checks"]
    assert "not_recently_contacted" not in r["checks"]
    assert r["pass"], r["blockers"]
