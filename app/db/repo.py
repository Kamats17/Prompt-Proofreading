"""Plain SQL CRUD for the sales-agent SQLite DB. No ORM."""
import json
import sqlite3
from typing import Optional, Iterable
from urllib.parse import urlparse

from app.db.models import Prospect, Audit, Draft, DncEntry, Event
from app.safety.dnc import _normalize_company, _fuzzy_match
from app.sources._normalize import domain_of, clean_company


# ---------- prospects ----------

def _is_homepage(url: str) -> bool:
    u = urlparse(url or "")
    return u.path in ("", "/") and not u.query


def _same_business(existing: sqlite3.Row, p: Prospect) -> bool:
    """Two URLs on one domain are the same prospect only when it's clearly one
    business: the names match, or one of the URLs is that site's homepage.
    Never merge on the domain alone: multi-tenant hosts (booking, ordering and
    directory platforms) put many unrelated businesses on a single domain."""
    a, b = _normalize_company(existing["company"] or ""), _normalize_company(p.company or "")
    if a and b and _fuzzy_match(a, b):
        return True
    return _is_homepage(existing["url"]) or _is_homepage(p.url)


def upsert_prospect(conn: sqlite3.Connection, p: Prospect) -> int:
    """Insert a prospect, or merge into the existing row for the same business.

    Same business = same URL, or same canonical domain plus `_same_business`, so
    one site arriving from several lead sources (master CSV `joes.com`, node XLSX
    `joes.com/home`) stays one prospect instead of being emailed twice.

    Merging only FILLS EMPTY fields. A re-import or a second source never
    overwrites an existing email, name, or phone: a silently changed address is
    how an email ends up going to the wrong person."""
    canon = p.canonical_domain or domain_of(p.url)
    row = conn.execute("SELECT id, company, url FROM prospects WHERE url = ?", (p.url,)).fetchone()
    if not row and canon:
        for cand in conn.execute("SELECT id, company, url FROM prospects WHERE canonical_domain = ? "
                                 "ORDER BY id", (canon,)).fetchall():
            if _same_business(cand, p):
                row = cand
                break
    if row:
        # A name derived from the domain ("Joespizza") yields to a real one.
        placeholder = (row["company"] or "") == clean_company("", row["url"])
        conn.execute(
            """UPDATE prospects
               SET company = CASE WHEN ? AND NULLIF(?, '') IS NOT NULL THEN ? ELSE company END,
                   canonical_domain = COALESCE(canonical_domain, ?),
                   industry = COALESCE(industry, ?),
                   city = COALESCE(city, ?),
                   state = COALESCE(state, ?),
                   email_source = CASE WHEN email IS NULL OR email = '' THEN ? ELSE email_source END,
                   email = COALESCE(NULLIF(email, ''), ?),
                   contact_name = COALESCE(NULLIF(contact_name, ''), ?),
                   phone = COALESCE(NULLIF(phone, ''), ?),
                   node_rating = COALESCE(node_rating, ?),
                   node_issues_json = COALESCE(node_issues_json, ?),
                   updated_at = datetime('now')
               WHERE id = ?""",
            (1 if placeholder else 0, p.company, p.company, canon, p.industry, p.city, p.state,
             p.email_source, p.email, p.contact_name, p.phone, p.node_rating,
             p.node_issues_json, row["id"]),
        )
        conn.commit()
        return row["id"]
    cur = conn.execute(
        """INSERT INTO prospects (company, url, canonical_domain, industry, city, state,
                                  email, email_source, contact_name, phone, source,
                                  node_rating, node_issues_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (p.company, p.url, canon, p.industry, p.city, p.state, p.email, p.email_source,
         p.contact_name, p.phone, p.source, p.node_rating, p.node_issues_json),
    )
    conn.commit()
    return cur.lastrowid


def get_prospect(conn: sqlite3.Connection, prospect_id: int) -> Optional[Prospect]:
    row = conn.execute("SELECT * FROM prospects WHERE id = ?", (prospect_id,)).fetchone()
    return Prospect(**dict(row)) if row else None


def set_prospect_email(conn: sqlite3.Connection, prospect_id: int,
                       email: str, source: str, *, overwrite: bool = False) -> None:
    """Set the contact email. Only fills an empty field unless overwrite=True
    (manual entry from the dashboard). Un-blocks a prospect parked as 'no_email'."""
    where = "id = ?" if overwrite else "id = ? AND (email IS NULL OR email = '')"
    conn.execute(
        f"UPDATE prospects SET email = ?, email_source = ?, "
        f"blocked_reason = CASE WHEN blocked_reason = 'no_email' THEN NULL ELSE blocked_reason END, "
        f"updated_at = datetime('now') WHERE {where}",
        (email, source, prospect_id),
    )
    conn.commit()


def set_contact_name_if_empty(conn: sqlite3.Connection, prospect_id: int, name: str) -> None:
    conn.execute(
        "UPDATE prospects SET contact_name = ?, updated_at = datetime('now') "
        "WHERE id = ? AND (contact_name IS NULL OR contact_name = '')",
        (name, prospect_id),
    )
    conn.commit()


def unblock_prospect(conn: sqlite3.Connection, prospect_id: int) -> None:
    conn.execute(
        "UPDATE prospects SET blocked_reason = NULL, audit_attempts = 0, last_audit_error = NULL, "
        "last_audit_error_at = NULL, updated_at = datetime('now') WHERE id = ?", (prospect_id,))
    conn.commit()


def mark_prospect_blocked(conn: sqlite3.Connection, prospect_id: int, reason: str) -> None:
    conn.execute(
        "UPDATE prospects SET blocked_reason = ?, updated_at = datetime('now') WHERE id = ?",
        (reason, prospect_id),
    )
    conn.commit()


def stamp_prospect_contacted(conn: sqlite3.Connection, prospect_id: int) -> None:
    conn.execute(
        "UPDATE prospects SET last_contacted_at = datetime('now'), updated_at = datetime('now') "
        "WHERE id = ?",
        (prospect_id,),
    )
    conn.commit()


LIVE_CONTACT_STATUSES = ("approved", "pushed", "sent", "replied", "opted_out", "bounced")


def recently_contacted(conn: sqlite3.Connection, *, email: str | None, domain: str | None,
                       days: int = 90, exclude_prospect_id: int | None = None,
                       exclude_draft_id: int | None = None,
                       prospect_id: int | None = None) -> Optional[str]:
    """Return a reason string if this business was already contacted, else None.

    Matches ANY prospect (including `prospect_id` itself) that shares the email or
    canonical domain and either (a) was contacted within `days`, or (b) has a draft
    in flight or already delivered (approved/pushed/sent/replied/opted_out/bounced).
    `exclude_draft_id` lets the draft under review ignore itself.
    """
    email = (email or "").strip().lower() or None
    domain = (domain or "").strip().lower() or None
    if not email and not domain and prospect_id is None:
        return None
    exclude_p = exclude_prospect_id if exclude_prospect_id is not None else -1
    exclude_d = exclude_draft_id if exclude_draft_id is not None else -1
    placeholders = ",".join("?" for _ in LIVE_CONTACT_STATUSES)
    row = conn.execute(
        f"""SELECT p.id, p.company, p.last_contacted_at
            FROM prospects p
            WHERE p.id != ?
              AND ( (? IS NOT NULL AND lower(p.email) = ?)
                 OR (? IS NOT NULL AND lower(p.canonical_domain) = ?)
                 OR (? IS NOT NULL AND p.id = ?) )
              AND ( (p.last_contacted_at IS NOT NULL
                     AND p.last_contacted_at >= datetime('now', ?))
                 OR EXISTS (SELECT 1 FROM drafts d
                            WHERE d.prospect_id = p.id AND d.id != ?
                              AND d.status IN ({placeholders})) )
            LIMIT 1""",
        (exclude_p, email, email, domain, domain, prospect_id, prospect_id,
         f"-{int(days)} days", exclude_d, *LIVE_CONTACT_STATUSES),
    ).fetchone()
    if row:
        return (f"recently contacted: prospect #{row['id']} {row['company']} "
                f"(last_contacted_at={row['last_contacted_at']})")
    return None


def record_audit_failure(conn: sqlite3.Connection, prospect_id: int, error: str) -> None:
    conn.execute(
        "UPDATE prospects SET audit_attempts = audit_attempts + 1, last_audit_error = ?, "
        "last_audit_error_at = datetime('now'), updated_at = datetime('now') WHERE id = ?",
        ((error or "")[:300], prospect_id),
    )
    conn.commit()


def clear_audit_failure(conn: sqlite3.Connection, prospect_id: int) -> None:
    conn.execute(
        "UPDATE prospects SET last_audit_error = NULL, last_audit_error_at = NULL WHERE id = ?",
        (prospect_id,),
    )
    conn.commit()


def list_prospects(conn: sqlite3.Connection, limit: int = 200) -> list[Prospect]:
    rows = conn.execute(
        "SELECT * FROM prospects ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [Prospect(**dict(r)) for r in rows]


def list_prospects_with_state(conn: sqlite3.Connection,
                               *, status: str | None = None,
                               grade: str | None = None,
                               source: str | None = None,
                               has_email: str | None = None,
                               limit: int = 300) -> list[dict]:
    """Enriched list joining latest audit + draft for /prospects view.

    `status` values: ingested | audited | drafted | pending | approved | pushed
                     | rejected | error | sent | replied
    `has_email` values: 'yes' | 'no' | None

    The status filter is applied in SQL (before LIMIT) so it is correct at scale.
    """
    sql = """
        SELECT p.id AS prospect_id, p.company, p.url, p.industry, p.state,
               p.email, p.source, p.node_rating, p.created_at AS p_created_at,
               p.blocked_reason, p.audit_attempts, p.last_audit_error,
               (SELECT id FROM audits WHERE prospect_id = p.id ORDER BY id DESC LIMIT 1) AS audit_id,
               (SELECT final_grade FROM audits WHERE prospect_id = p.id ORDER BY id DESC LIMIT 1) AS final_grade,
               (SELECT icp_score FROM audits WHERE prospect_id = p.id ORDER BY id DESC LIMIT 1) AS icp_score,
               (SELECT id FROM drafts WHERE prospect_id = p.id ORDER BY id DESC LIMIT 1) AS draft_id,
               (SELECT status FROM drafts WHERE prospect_id = p.id ORDER BY id DESC LIMIT 1) AS draft_status,
               (SELECT qa_pass FROM drafts WHERE prospect_id = p.id ORDER BY id DESC LIMIT 1) AS qa_pass
        FROM prospects p
        WHERE 1=1
    """
    params: list = []
    if grade:
        sql += " AND (SELECT final_grade FROM audits WHERE prospect_id = p.id ORDER BY id DESC LIMIT 1) = ?"
        params.append(grade)
    if source:
        sql += " AND p.source = ?"
        params.append(source)
    if has_email == "yes":
        sql += " AND p.email IS NOT NULL AND p.email != ''"
    elif has_email == "no":
        sql += " AND (p.email IS NULL OR p.email = '')"

    # Derived-status filter, expressed in SQL so it runs before LIMIT.
    if status == "blocked":
        sql += " AND p.blocked_reason IS NOT NULL"
    elif status == "ingested":
        sql += (" AND p.blocked_reason IS NULL"
                " AND NOT EXISTS (SELECT 1 FROM audits a WHERE a.prospect_id = p.id)")
    elif status == "audited":
        sql += (" AND EXISTS (SELECT 1 FROM audits a WHERE a.prospect_id = p.id)"
                " AND NOT EXISTS (SELECT 1 FROM drafts d WHERE d.prospect_id = p.id)")
    elif status:  # any draft status: pending/approved/rejected/pushed/error/sent/replied
        sql += (" AND (SELECT status FROM drafts WHERE prospect_id = p.id"
                " ORDER BY id DESC LIMIT 1) = ?")
        params.append(status)

    sql += " ORDER BY p.id DESC LIMIT ?"
    params.append(limit)

    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    # Compute derived "status" for display (filtering already done in SQL).
    for r in rows:
        if r["draft_status"]:
            r["status"] = r["draft_status"]
        elif r["blocked_reason"]:
            r["status"] = "blocked"
        elif r["audit_id"]:
            r["status"] = "audited"
        else:
            r["status"] = "ingested"
    return rows


# ---------- audits ----------

def insert_audit(conn: sqlite3.Connection, a: Audit) -> int:
    cur = conn.execute(
        """INSERT INTO audits (prospect_id, screenshot_path, html_cache_path, issues_json,
                               meta_json, service_angle, demo_angle, icp_score,
                               node_rating_snapshot, final_grade, model_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (a.prospect_id, a.screenshot_path, a.html_cache_path, a.issues_json,
         a.meta_json, a.service_angle, a.demo_angle, a.icp_score,
         a.node_rating_snapshot, a.final_grade, a.model_id),
    )
    conn.commit()
    return cur.lastrowid


def get_audit(conn: sqlite3.Connection, audit_id: int) -> Optional[Audit]:
    row = conn.execute("SELECT * FROM audits WHERE id = ?", (audit_id,)).fetchone()
    return Audit(**dict(row)) if row else None


def latest_audit_for_prospect(conn: sqlite3.Connection, prospect_id: int) -> Optional[Audit]:
    row = conn.execute(
        "SELECT * FROM audits WHERE prospect_id = ? ORDER BY id DESC LIMIT 1",
        (prospect_id,),
    ).fetchone()
    return Audit(**dict(row)) if row else None


# ---------- drafts ----------

class DuplicateDraftError(Exception):
    """A live draft already exists for this prospect + touch (DB unique index)."""


def insert_draft(conn: sqlite3.Connection, d: Draft) -> int:
    try:
        cur = conn.execute(
            """INSERT INTO drafts (prospect_id, audit_id, touch, parent_draft_id, subject,
                                   subject_variant, finding_type, evidence_json, body_text,
                                   body_html, gmail_thread_id, rfc_message_id, qa_pass,
                                   qa_blockers_json, human_edited, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (d.prospect_id, d.audit_id, d.touch, d.parent_draft_id, d.subject,
             d.subject_variant, d.finding_type, d.evidence_json, d.body_text, d.body_html,
             d.gmail_thread_id, d.rfc_message_id, d.qa_pass, d.qa_blockers_json,
             d.human_edited, d.status),
        )
    except sqlite3.IntegrityError as e:
        conn.rollback()
        if "UNIQUE" in str(e).upper():
            raise DuplicateDraftError(
                f"prospect #{d.prospect_id} already has a live touch-{d.touch} draft") from e
        raise
    conn.commit()
    return cur.lastrowid


def get_draft(conn: sqlite3.Connection, draft_id: int) -> Optional[Draft]:
    row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    return Draft(**dict(row)) if row else None


def latest_live_draft_for_prospect(conn: sqlite3.Connection, prospect_id: int,
                                   touch: int | None = 1) -> Optional[Draft]:
    """Most recent non-rejected draft (it still occupies the pipeline slot).
    `touch=None` matches any touch."""
    sql = "SELECT * FROM drafts WHERE prospect_id = ? AND status != 'rejected'"
    params: list = [prospect_id]
    if touch is not None:
        sql += " AND touch = ?"
        params.append(touch)
    row = conn.execute(sql + " ORDER BY id DESC LIMIT 1", params).fetchone()
    return Draft(**dict(row)) if row else None


def claim_pending_draft(conn: sqlite3.Connection, draft_id: int) -> bool:
    """Atomically flip a pending draft to 'approved' (in-flight). Returns True if this
    call won the race. Guards against double-click / duplicate Gmail pushes."""
    n = conn.execute(
        "UPDATE drafts SET status = 'approved', updated_at = datetime('now') "
        "WHERE id = ? AND status = 'pending'",
        (draft_id,),
    ).rowcount
    conn.commit()
    return n == 1


def transition_draft(conn: sqlite3.Connection, draft_id: int, from_statuses: tuple[str, ...],
                     to_status: str, **fields) -> bool:
    """Atomically move a draft to `to_status` ONLY if it is currently in one of
    `from_statuses`. Extra column updates go in `fields` (value "now" = timestamp).
    Every dashboard status change goes through here, so a stale page, double
    click, or forged request can't push a draft through an illegal edge."""
    allowed = {"gmail_draft_id", "gmail_thread_id", "rfc_message_id", "reject_reason",
               "reject_category", "sent_at", "replied_at"}
    sets = ["status = ?", "updated_at = datetime('now')"]
    params: list = [to_status]
    for k, v in fields.items():
        if k not in allowed:
            raise ValueError(f"transition_draft: unsupported field {k}")
        if v == "now":
            sets.append(f"{k} = datetime('now')")
        else:
            sets.append(f"{k} = ?")
            params.append(v)
    ph = ",".join("?" for _ in from_statuses)
    params += [draft_id, *from_statuses]
    n = conn.execute(
        f"UPDATE drafts SET {', '.join(sets)} WHERE id = ? AND status IN ({ph})", params
    ).rowcount
    conn.commit()
    return n == 1


def update_draft_status(
    conn: sqlite3.Connection,
    draft_id: int,
    status: str,
    *,
    gmail_draft_id: Optional[str] = None,
    gmail_thread_id: Optional[str] = None,
    reject_reason: Optional[str] = None,
) -> None:
    conn.execute(
        """UPDATE drafts
           SET status = ?,
               gmail_draft_id = COALESCE(?, gmail_draft_id),
               gmail_thread_id = COALESCE(?, gmail_thread_id),
               reject_reason = COALESCE(?, reject_reason),
               updated_at = datetime('now')
           WHERE id = ?""",
        (status, gmail_draft_id, gmail_thread_id, reject_reason, draft_id),
    )
    conn.commit()


def mark_draft_sent(conn: sqlite3.Connection, draft_id: int) -> None:
    conn.execute(
        "UPDATE drafts SET status = 'sent', sent_at = datetime('now'), "
        "updated_at = datetime('now') WHERE id = ?", (draft_id,),
    )
    conn.commit()


def mark_draft_replied(conn: sqlite3.Connection, draft_id: int) -> None:
    conn.execute(
        "UPDATE drafts SET status = 'replied', replied_at = datetime('now'), "
        "updated_at = datetime('now') WHERE id = ?", (draft_id,),
    )
    conn.commit()


def update_draft_body(
    conn: sqlite3.Connection,
    draft_id: int,
    *,
    subject: Optional[str] = None,
    body_text: Optional[str] = None,
    body_html: Optional[str] = None,
    human_edited: bool = False,
) -> None:
    conn.execute(
        """UPDATE drafts
           SET subject = COALESCE(?, subject),
               body_text = COALESCE(?, body_text),
               body_html = COALESCE(?, body_html),
               human_edited = CASE WHEN ? THEN 1 ELSE human_edited END,
               updated_at = datetime('now')
           WHERE id = ?""",
        (subject, body_text, body_html, 1 if human_edited else 0, draft_id),
    )
    conn.commit()


def first_touch_pushes_today(conn: sqlite3.Connection) -> int:
    """First-touch drafts pushed to Gmail since local midnight (daily send cap)."""
    return conn.execute(
        "SELECT COUNT(*) FROM events e JOIN drafts d ON d.id = e.draft_id "
        "WHERE e.event_type = 'approved_pushed' AND d.touch = 1 "
        "AND e.created_at >= datetime('now', 'localtime', 'start of day', 'utc')"
    ).fetchone()[0]


def list_drafts(conn: sqlite3.Connection, status: Optional[str] = None,
                grade: Optional[str] = None, limit: int = 200) -> list[dict]:
    """Returns enriched rows joining prospects + audits for the dashboard list view."""
    sql = """
        SELECT d.id AS draft_id, d.subject, d.status, d.qa_pass, d.qa_blockers_json,
               d.gmail_draft_id, d.created_at AS draft_created_at, d.updated_at,
               p.id AS prospect_id, p.company, p.url, p.industry, p.state, p.email,
               a.id AS audit_id, a.final_grade, a.icp_score, a.service_angle
        FROM drafts d
        JOIN prospects p ON p.id = d.prospect_id
        JOIN audits a ON a.id = d.audit_id
        WHERE 1=1
    """
    params: list = []
    if status:
        sql += " AND d.status = ?"
        params.append(status)
    if grade:
        sql += " AND a.final_grade = ?"
        params.append(grade)
    sql += " ORDER BY d.id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ---------- dnc ----------

def add_dnc(conn: sqlite3.Connection, e: DncEntry) -> int:
    # Store canonical forms so every later comparison is apples to apples.
    domain = domain_of(e.domain) if e.domain else None
    email = (e.email or "").strip().lower() or None
    cur = conn.execute(
        "INSERT INTO dnc_list (company, domain, email, reason, added_by) VALUES (?, ?, ?, ?, ?)",
        ((e.company or "").strip() or None, domain, email, e.reason, e.added_by),
    )
    conn.commit()
    return cur.lastrowid


def list_dnc(conn: sqlite3.Connection) -> list[DncEntry]:
    rows = conn.execute("SELECT * FROM dnc_list ORDER BY id DESC").fetchall()
    return [DncEntry(**dict(r)) for r in rows]


def remove_dnc(conn: sqlite3.Connection, dnc_id: int) -> None:
    conn.execute("DELETE FROM dnc_list WHERE id = ?", (dnc_id,))
    conn.commit()


# ---------- events ----------

def log_event(conn: sqlite3.Connection, ev: Event) -> int:
    cur = conn.execute(
        "INSERT INTO events (prospect_id, draft_id, event_type, payload_json) VALUES (?, ?, ?, ?)",
        (ev.prospect_id, ev.draft_id, ev.event_type, ev.payload_json),
    )
    conn.commit()
    return cur.lastrowid


def list_events_for_prospect(conn: sqlite3.Connection, prospect_id: int) -> list[Event]:
    rows = conn.execute(
        "SELECT * FROM events WHERE prospect_id = ? ORDER BY id ASC",
        (prospect_id,),
    ).fetchall()
    return [Event(**dict(r)) for r in rows]
