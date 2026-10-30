"""Routes for the dashboard.

Draft state machine (every edge is a conditional UPDATE via transition_draft):

  pending --approve--> approved --(Gmail ok)--> pushed --mark-sent--> sent
     |                    |                        |                    |
     |                    +--(Gmail fails)--> pending                   |
     +--reject--> rejected      pushed/sent --mark-replied--> replied   |
     +--regenerate--> rejected (+ new pending)   pushed/sent/replied --opted-out--> opted_out

`pp-agent sync` moves pushed -> sent/replied/opted_out/bounced automatically.
"""
import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates

from app.db.migrate import connect
from app.db.models import DncEntry
from app.db.repo import (
    get_prospect, get_audit, get_draft, latest_audit_for_prospect,
    list_drafts, list_dnc, add_dnc, remove_dnc,
    update_draft_body, transition_draft, stamp_prospect_contacted, mark_prospect_blocked,
    set_prospect_email, unblock_prospect, first_touch_pushes_today,
    list_events_for_prospect, list_prospects_with_state,
)
from app.draft.gmail_client import create_gmail_draft
from app.draft.generator import to_html_body
from app.draft.qa_gate import run_qa
from app.draft.voice import compliance_warnings
from app.events.logger import log
from app.mockup.builder import build_mockup, mockup_paths
from app.mockup.reply import default_reply_text, push_mockup_reply
from app.settings import DB_PATH, BRAND, CACHE_DIR, DAILY_NEW_CAP
from app.sources._normalize import normalize_email, domain_of

REJECT_CATEGORIES = ["wrong_claim", "tone", "bad_lead", "agency_site", "directory_site",
                     "duplicate", "other"]
EDITABLE = ("pending", "error")


def _conn() -> sqlite3.Connection:
    return connect(DB_PATH)


def _back(prospect_id: int, msg: str) -> RedirectResponse:
    return RedirectResponse(f"/prospect/{prospect_id}?msg={quote(msg)}", status_code=303)


_QUEUE_ORDER_SQL = """SELECT d.id, d.prospect_id FROM drafts d JOIN audits a ON a.id = d.audit_id
                       WHERE d.status = 'pending'
                       ORDER BY d.qa_pass DESC, a.icp_score DESC, d.id ASC"""


def _to_next(conn, msg: str) -> RedirectResponse:
    """After a draft leaves the queue, open the next pending one (queue order)."""
    row = conn.execute(_QUEUE_ORDER_SQL + " LIMIT 1").fetchone()
    if row:
        return _back(row["prospect_id"], msg)
    return RedirectResponse(f"/queue?msg={quote(msg + ' Queue is empty.')}", status_code=303)


def _qa_for_draft(conn, d, *, body_text=None, subject=None, human_edited=None) -> dict:
    """Re-run QA against CURRENT prospect/DNC/contact state."""
    p = get_prospect(conn, d.prospect_id)
    audit = get_audit(conn, d.audit_id)
    ev = json.loads(d.evidence_json or "{}")
    return run_qa(
        conn, company=p.company, prospect_email=p.email, prospect_url=p.url,
        subject=subject if subject is not None else d.subject,
        body_text=body_text if body_text is not None else d.body_text,
        audit_issues_json=audit.issues_json or "[]", final_grade=audit.final_grade,
        prospect_id=p.id, draft_id=d.id, touch=d.touch,
        human_edited=bool(d.human_edited if human_edited is None else human_edited),
        blocked_reason=p.blocked_reason,
        used_quotes=ev.get("used_quotes") or None, used_fact_ids=ev.get("used_fact_ids") or None,
        audit_facts=json.loads(audit.meta_json or "{}").get("facts"),
    )


def _store_qa(conn, draft_id: int, qa: dict) -> None:
    msgs = qa["blockers"] + qa["warnings"]
    conn.execute("UPDATE drafts SET qa_pass=?, qa_blockers_json=? WHERE id=?",
                 (1 if qa["pass"] else 0, json.dumps(msgs) if msgs else None, draft_id))
    conn.commit()


def _safe_png(path: str | None) -> Path | None:
    if not path:
        return None
    try:
        p = Path(path).resolve()
        p.relative_to(Path(CACHE_DIR).resolve())
    except (ValueError, OSError):
        return None
    return p if p.suffix.lower() == ".png" and p.exists() else None


def register_routes(app: FastAPI, templates: Jinja2Templates) -> None:

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, status: str | None = None, grade: str | None = None):
        with closing(_conn()) as conn:
            drafts = list_drafts(conn, status=status, grade=grade, limit=300)
            prospect_count = conn.execute("SELECT COUNT(*) FROM prospects").fetchone()[0]
            pushed_today = first_touch_pushes_today(conn)
        return templates.TemplateResponse(request, "list.html", {
            "drafts": drafts, "filter_status": status or "", "filter_grade": grade or "",
            "prospect_count": prospect_count, "brand": BRAND,
            "pushed_today": pushed_today, "daily_cap": DAILY_NEW_CAP,
            "compliance": compliance_warnings(),
        })

    @app.get("/queue", response_class=HTMLResponse)
    def queue(request: Request, msg: str | None = None):
        """Review queue: every pending draft with its evidence, one screen each."""
        with closing(_conn()) as conn:
            rows = conn.execute(_QUEUE_ORDER_SQL + " LIMIT 200").fetchall()
            total = conn.execute("SELECT COUNT(*) FROM drafts WHERE status = 'pending'").fetchone()[0]
            items = []
            for r in rows:
                d = get_draft(conn, r["id"])
                p = get_prospect(conn, d.prospect_id)
                a = get_audit(conn, d.audit_id)
                items.append({"draft": d, "prospect": p, "audit": a,
                              "issues": json.loads(a.issues_json or "[]"),
                              "meta": json.loads(a.meta_json or "{}")})
            pushed_today = first_touch_pushes_today(conn)
        return templates.TemplateResponse(request, "queue.html", {
            "items": items, "total_pending": total, "brand": BRAND, "pushed_today": pushed_today,
            "daily_cap": DAILY_NEW_CAP, "reject_categories": REJECT_CATEGORIES,
            "compliance": compliance_warnings(), "flash": msg or "",
        })

    @app.get("/prospect/{prospect_id}", response_class=HTMLResponse)
    def prospect_detail(prospect_id: int, request: Request, msg: str | None = None):
        with closing(_conn()) as conn:
            p = get_prospect(conn, prospect_id)
            if not p:
                raise HTTPException(404, "prospect not found")
            audit = latest_audit_for_prospect(conn, prospect_id)
            issues = json.loads(audit.issues_json) if audit and audit.issues_json else []
            meta = json.loads(audit.meta_json) if audit and audit.meta_json else {}
            events = list_events_for_prospect(conn, prospect_id)
            rows = conn.execute(
                "SELECT * FROM drafts WHERE prospect_id = ? ORDER BY id DESC LIMIT 6",
                (prospect_id,)).fetchall()
            has_mockup = mockup_paths(prospect_id)["png"].exists()
            mockup_reply = default_reply_text(conn, prospect_id) if has_mockup and audit else ""
        return templates.TemplateResponse(request, "detail.html", {
            "has_mockup": has_mockup, "mockup_reply": mockup_reply,
            "prospect": p, "audit": audit, "meta": meta, "issues": issues,
            "drafts": [dict(r) for r in rows], "events": events, "flash": msg or "",
            "brand": BRAND, "reject_categories": REJECT_CATEGORIES,
            "compliance": compliance_warnings(),
        })

    @app.get("/shot/{audit_id}/{kind}")
    def screenshot(audit_id: int, kind: str):
        """Serve only screenshot PNGs from the cache — never cached third-party HTML."""
        with closing(_conn()) as conn:
            a = get_audit(conn, audit_id)
        if not a or kind not in ("desktop", "mobile"):
            raise HTTPException(404)
        path = a.screenshot_path if kind == "desktop" else \
            json.loads(a.meta_json or "{}").get("mobile_screenshot_path")
        png = _safe_png(path)
        if not png:
            raise HTTPException(404)
        return FileResponse(str(png), media_type="image/png")

    @app.get("/mockup/{prospect_id}/{kind}")
    def mockup_image(prospect_id: int, kind: str):
        """Serve only the rendered mockup PNGs (never the HTML)."""
        key = {"sheet": "png", "phone": "proposed"}.get(kind)
        if not key:
            raise HTTPException(404)
        path = mockup_paths(prospect_id)[key]
        if not path.exists():
            raise HTTPException(404)
        return FileResponse(str(path), media_type="image/png",
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/prospect/{prospect_id}/mockup")
    async def api_build_mockup(prospect_id: int):
        def _do():
            with closing(_conn()) as conn:
                return build_mockup(conn, prospect_id)

        res = await asyncio.to_thread(_do)
        msg = ("Mockup ready: review it below, then push the reply to Gmail." if res.get("ok")
               else f"Mockup failed: {res.get('error')}")
        return _back(prospect_id, msg)

    @app.post("/api/prospect/{prospect_id}/mockup/send")
    async def api_send_mockup(prospect_id: int, body_text: str = Form(...)):
        def _do():
            with closing(_conn()) as conn:
                return push_mockup_reply(conn, prospect_id, body_text)

        res = await asyncio.to_thread(_do)
        if res.get("ok"):
            where = "in their thread" if res.get("threaded") else "as a new email"
            return _back(prospect_id, f"Mockup reply is in Gmail Drafts ({where}). Review and send it from Gmail.")
        return _back(prospect_id, f"Not pushed: {res.get('error')}")

    @app.get("/prospects", response_class=HTMLResponse)
    def prospects_index(request: Request, status: str | None = None, grade: str | None = None,
                        source: str | None = None, has_email: str | None = None):
        with closing(_conn()) as conn:
            rows = list_prospects_with_state(conn, status=status, grade=grade, source=source,
                                             has_email=has_email, limit=500)
            total = conn.execute("SELECT COUNT(*) FROM prospects").fetchone()[0]
        return templates.TemplateResponse(request, "prospects.html", {
            "rows": rows, "filter_status": status or "", "filter_grade": grade or "",
            "filter_source": source or "", "filter_has_email": has_email or "",
            "total": total, "brand": BRAND,
        })

    @app.get("/dnc", response_class=HTMLResponse)
    def dnc_page(request: Request):
        with closing(_conn()) as conn:
            entries = list_dnc(conn)
        return templates.TemplateResponse(request, "dnc.html", {"entries": entries, "brand": BRAND})

    # ---- draft actions ------------------------------------------------------------

    @app.post("/api/draft/{draft_id}/edit")
    def api_edit_draft(draft_id: int, subject: str = Form(...), body_text: str = Form(...)):
        with closing(_conn()) as conn:
            d = get_draft(conn, draft_id)
            if not d:
                raise HTTPException(404, "draft not found")
            if d.status not in EDITABLE:
                return _back(d.prospect_id, f"Draft is '{d.status}' and can't be edited here.")
            update_draft_body(conn, draft_id, subject=subject, body_text=body_text,
                              body_html=to_html_body(body_text, first_touch=d.touch == 1),
                              human_edited=True)
            d = get_draft(conn, draft_id)
            qa = _qa_for_draft(conn, d)
            _store_qa(conn, draft_id, qa)
            log(conn, "draft_edited", prospect_id=d.prospect_id, draft_id=draft_id,
                payload={"subject": subject, "qa_pass": qa["pass"]})
            msg = "Edits saved. QA " + ("passes." if qa["pass"] else f"blocked: {qa['blockers']}")
            if qa["warnings"]:
                msg += f" Warnings: {qa['warnings']}"
        return _back(d.prospect_id, msg)

    @app.post("/api/draft/{draft_id}/approve")
    async def api_approve_draft(draft_id: int):
        with closing(_conn()) as conn:
            d = get_draft(conn, draft_id)
            if not d:
                raise HTTPException(404, "draft not found")
            prospect_id = d.prospect_id
            if d.status != "pending":
                return _back(prospect_id, f"Draft is '{d.status}', not pending — refusing to push.")
            # Re-check EVERYTHING against current state: DNC added since drafting,
            # email changed by a re-ingest, another draft for the domain pushed, etc.
            qa = _qa_for_draft(conn, d)
            _store_qa(conn, draft_id, qa)
            if not qa["pass"]:
                return _back(prospect_id, f"Not pushed — QA blockers: {qa['blockers']}")
            p = get_prospect(conn, prospect_id)
            to = normalize_email(p.email)
            if not to:
                return _back(prospect_id, "Prospect has no valid email — add one first.")
            if d.touch == 1 and first_touch_pushes_today(conn) >= DAILY_NEW_CAP:
                return _back(prospect_id, f"Daily cap reached ({DAILY_NEW_CAP} new emails). "
                                          "Try again tomorrow or raise sending.daily_new_cap.")
            in_reply_to = None
            thread_id = d.gmail_thread_id if d.touch > 1 else None
            if d.touch > 1 and d.parent_draft_id:
                parent = get_draft(conn, d.parent_draft_id)
                in_reply_to = parent.rfc_message_id if parent else None
            if not transition_draft(conn, draft_id, ("pending",), "approved"):
                return _back(prospect_id, "Draft is already being approved.")
            payload = {"to_email": to, "to_name": p.company, "subject": d.subject,
                       "body_text": d.body_text, "body_html": d.body_html,
                       "thread_id": thread_id, "in_reply_to": in_reply_to}

        result = {"success": False, "error": "unexpected failure"}
        try:
            result = await asyncio.to_thread(create_gmail_draft, **payload)
        finally:
            with closing(_conn()) as conn:
                if not result.get("success"):
                    transition_draft(conn, draft_id, ("approved",), "pending",
                                     reject_reason=f"push failed: {result.get('error')}"[:300])
                    log(conn, "draft_push_error", prospect_id=prospect_id, draft_id=draft_id,
                        payload=result)
                else:
                    moved = transition_draft(conn, draft_id, ("approved",), "pushed",
                                             gmail_draft_id=result.get("draft_id"),
                                             gmail_thread_id=result.get("thread_id"))
                    stamp_prospect_contacted(conn, prospect_id)
                    log(conn, "approved_pushed", prospect_id=prospect_id, draft_id=draft_id,
                        payload={"gmail_draft_id": result.get("draft_id"), "state_ok": moved})
        if not result.get("success"):
            return _back(prospect_id, f"Gmail push failed (draft back to pending): {result.get('error')}")
        with closing(_conn()) as conn:
            company = get_prospect(conn, prospect_id).company
            return _to_next(conn, f"{company} pushed to Gmail Drafts. Review and send it from Gmail.")

    @app.post("/api/draft/{draft_id}/reject")
    def api_reject_draft(draft_id: int, reason: str = Form(""), category: str = Form("other")):
        with closing(_conn()) as conn:
            d = get_draft(conn, draft_id)
            if not d:
                raise HTTPException(404, "draft not found")
            cat = category if category in REJECT_CATEGORIES else "other"
            if not transition_draft(conn, draft_id, EDITABLE, "rejected",
                                    reject_reason=reason or cat, reject_category=cat):
                return _back(d.prospect_id, f"Draft is '{d.status}' and can't be rejected.")
            if cat in ("bad_lead", "agency_site", "directory_site"):
                mark_prospect_blocked(conn, d.prospect_id, f"rejected:{cat}")
            log(conn, "rejected", prospect_id=d.prospect_id, draft_id=draft_id,
                payload={"reason": reason, "category": cat})
            return _to_next(conn, f"{get_prospect(conn, d.prospect_id).company} draft rejected.")

    @app.post("/api/draft/{draft_id}/regenerate")
    async def api_regenerate_draft(draft_id: int):
        with closing(_conn()) as conn:
            d = get_draft(conn, draft_id)
            if not d:
                raise HTTPException(404, "draft not found")
            prospect_id = d.prospect_id
            if d.touch != 1:
                return _back(prospect_id, "Regenerate is for first-touch drafts; reject follow-ups instead.")
            if not transition_draft(conn, draft_id, EDITABLE, "rejected",
                                    reject_reason="regenerated", reject_category="other"):
                return _back(prospect_id, f"Draft is '{d.status}' — cannot regenerate.")

        from app.draft.pipeline import draft_for_prospect

        def _do():
            with closing(_conn()) as conn:
                return draft_for_prospect(conn, prospect_id)

        result = await asyncio.to_thread(_do)
        if result.get("ok"):
            msg = "Regenerated. QA " + ("passes." if result.get("qa_pass") else
                                        f"blocked: {result.get('blockers')}")
        else:
            msg = f"Regenerate failed: {result.get('error') or result.get('skipped')}"
        return _back(prospect_id, msg)

    @app.post("/api/draft/{draft_id}/mark-sent")
    def api_mark_sent(draft_id: int):
        with closing(_conn()) as conn:
            d = get_draft(conn, draft_id)
            if not d:
                raise HTTPException(404, "draft not found")
            if not transition_draft(conn, draft_id, ("pushed",), "sent", sent_at="now"):
                return _back(d.prospect_id, f"Only pushed drafts can be marked sent (is '{d.status}').")
            stamp_prospect_contacted(conn, d.prospect_id)
            log(conn, "marked_sent", prospect_id=d.prospect_id, draft_id=draft_id)
        return _back(d.prospect_id, "Marked as sent.")

    @app.post("/api/draft/{draft_id}/mark-replied")
    def api_mark_replied(draft_id: int):
        with closing(_conn()) as conn:
            d = get_draft(conn, draft_id)
            if not d:
                raise HTTPException(404, "draft not found")
            if not transition_draft(conn, draft_id, ("pushed", "sent"), "replied", replied_at="now"):
                return _back(d.prospect_id, f"Can't mark a '{d.status}' draft as replied.")
            log(conn, "marked_replied", prospect_id=d.prospect_id, draft_id=draft_id)
        return _back(d.prospect_id, "Marked as replied.")

    @app.post("/api/draft/{draft_id}/opted-out")
    def api_opted_out(draft_id: int):
        """Prospect said no / unsubscribe: DNC the email AND the domain, close the draft."""
        with closing(_conn()) as conn:
            d = get_draft(conn, draft_id)
            if not d:
                raise HTTPException(404, "draft not found")
            p = get_prospect(conn, d.prospect_id)
            transition_draft(conn, draft_id, ("pushed", "sent", "replied"), "opted_out",
                             replied_at="now")
            add_dnc(conn, DncEntry(company=p.company, domain=domain_of(p.url), email=p.email,
                                   reason="opted out (reply)", added_by="dashboard"))
            mark_prospect_blocked(conn, p.id, "opted_out")
            log(conn, "opted_out", prospect_id=p.id, draft_id=draft_id)
        return _back(d.prospect_id, "Opt-out recorded: email + domain added to DNC.")

    # ---- prospect actions ---------------------------------------------------------

    @app.post("/api/prospect/{prospect_id}/email")
    def api_set_email(prospect_id: int, email: str = Form(...)):
        e = normalize_email(email)
        with closing(_conn()) as conn:
            if not get_prospect(conn, prospect_id):
                raise HTTPException(404, "prospect not found")
            if not e:
                return _back(prospect_id, f"{email!r} is not a valid email address.")
            set_prospect_email(conn, prospect_id, e, "manual", overwrite=True)
            log(conn, "email_set_manually", prospect_id=prospect_id, payload={"email": e})
        return _back(prospect_id, f"Email set to {e}.")

    @app.post("/api/prospect/{prospect_id}/unblock")
    def api_unblock(prospect_id: int):
        with closing(_conn()) as conn:
            p = get_prospect(conn, prospect_id)
            if not p:
                raise HTTPException(404, "prospect not found")
            if p.blocked_reason in ("dnc", "opted_out"):
                return _back(prospect_id, "DNC/opt-out blocks can only be lifted by removing the DNC entry.")
            unblock_prospect(conn, prospect_id)
            log(conn, "unblocked", prospect_id=prospect_id, payload={"was": p.blocked_reason})
        return _back(prospect_id, "Unblocked. Run the audit again.")

    @app.post("/api/dnc/add")
    def api_dnc_add(company: str = Form(""), domain: str = Form(""),
                    email: str = Form(""), reason: str = Form("")):
        with closing(_conn()) as conn:
            add_dnc(conn, DncEntry(company=company.strip() or None, domain=domain.strip() or None,
                                   email=email.strip().lower() or None,
                                   reason=reason.strip() or None, added_by="dashboard"))
            log(conn, "dnc_added", payload={"company": company, "domain": domain, "email": email})
        return RedirectResponse("/dnc", status_code=303)

    @app.post("/api/dnc/{dnc_id}/remove")
    def api_dnc_remove(dnc_id: int):
        with closing(_conn()) as conn:
            remove_dnc(conn, dnc_id)
            log(conn, "dnc_removed", payload={"dnc_id": dnc_id})
        return RedirectResponse("/dnc", status_code=303)

    @app.post("/api/audit/{prospect_id}")
    async def api_audit_prospect(prospect_id: int):
        from app.audit.pipeline import audit_prospect

        def _do():
            with closing(_conn()) as conn:
                return audit_prospect(conn, prospect_id)

        result = await asyncio.to_thread(_do)
        if result.get("ok"):
            msg = f"Audit done: grade {result.get('final_grade')} (score {result.get('icp_score')})."
        else:
            msg = f"Audit not completed: {result.get('error') or result.get('blocked')} {result.get('reason') or ''}"
        return _back(prospect_id, msg)

    @app.post("/api/draft/from-prospect/{prospect_id}")
    async def api_draft_from_prospect(prospect_id: int):
        from app.draft.pipeline import draft_for_prospect

        def _do():
            with closing(_conn()) as conn:
                return draft_for_prospect(conn, prospect_id)

        result = await asyncio.to_thread(_do)
        if result.get("ok"):
            msg = "Draft created. QA " + ("passes." if result.get("qa_pass") else
                                          f"blocked: {result.get('blockers')}")
        else:
            msg = f"Draft not created: {result.get('error') or result.get('skipped')}"
        return _back(prospect_id, msg)
