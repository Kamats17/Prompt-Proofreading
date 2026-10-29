"""Re-grade existing audits from their saved evidence (no fetch, no Claude call).

Used after the scoring rules change: findings, facts and the model's lead
context are already stored on each audit, so only the arithmetic is redone.
"""
import json
import sqlite3
from pathlib import Path

from app.audit.extractor import extract_features
from app.audit.grader import combine, score_lead
from app.db.models import Event
from app.db.repo import get_prospect, log_event


def rescore_audits(conn: sqlite3.Connection) -> list[dict]:
    """Rescore the latest audit of every prospect. Returns one change record each."""
    rows = conn.execute(
        """SELECT a.* FROM audits a
           WHERE a.id = (SELECT MAX(id) FROM audits WHERE prospect_id = a.prospect_id)
           ORDER BY a.id""").fetchall()
    changes = []
    for a in rows:
        p = get_prospect(conn, a["prospect_id"])
        meta = json.loads(a["meta_json"] or "{}")
        findings = json.loads(a["issues_json"] or "[]")
        html = a["html_cache_path"]
        features = (extract_features(html, base_url=meta.get("final_url") or p.url)
                    if html and Path(html).exists() else {})
        score = score_lead(findings=findings, meta=meta, features=features, industry=p.industry,
                           has_email=bool(p.email),
                           has_phone=bool(p.phone or features.get("phones_in_text")),
                           contact_name=p.contact_name)
        grade = "Reject" if score["reject_reason"] else combine(a["node_rating_snapshot"], score["icp"])
        meta["score"] = score
        conn.execute("UPDATE audits SET icp_score = ?, final_grade = ?, meta_json = ? WHERE id = ?",
                     (score["icp"], grade, json.dumps(meta, ensure_ascii=False), a["id"]))
        conn.commit()
        rec = {"prospect_id": p.id, "company": p.company, "old": (a["final_grade"], a["icp_score"]),
               "new": (grade, score["icp"]), "reasons": score["reasons"]}
        if rec["old"] != rec["new"]:
            log_event(conn, Event(prospect_id=p.id, event_type="rescored",
                                  payload_json=json.dumps({"old": rec["old"], "new": rec["new"]})))
        changes.append(rec)
    return changes
