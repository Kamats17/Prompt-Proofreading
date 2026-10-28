from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Prospect:
    id: Optional[int] = None
    company: str = ""
    url: str = ""
    canonical_domain: Optional[str] = None
    industry: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    email: Optional[str] = None
    email_source: Optional[str] = None
    contact_name: Optional[str] = None
    phone: Optional[str] = None
    source: str = "manual"
    node_rating: Optional[str] = None
    node_issues_json: Optional[str] = None
    blocked_reason: Optional[str] = None
    audit_attempts: int = 0
    last_audit_error: Optional[str] = None
    last_audit_error_at: Optional[str] = None
    last_contacted_at: Optional[str] = None
    do_not_contact_until: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class AuditIssue:
    type: str
    quote: str
    confidence: str
    why_hurts: str
    service: str


@dataclass
class Audit:
    id: Optional[int] = None
    prospect_id: int = 0
    screenshot_path: Optional[str] = None
    html_cache_path: Optional[str] = None
    issues_json: str = "[]"
    meta_json: Optional[str] = None
    service_angle: Optional[str] = None
    demo_angle: Optional[str] = None
    icp_score: Optional[int] = None
    node_rating_snapshot: Optional[str] = None
    final_grade: Optional[str] = None
    model_id: Optional[str] = None
    created_at: Optional[str] = None


@dataclass
class Draft:
    id: Optional[int] = None
    prospect_id: int = 0
    audit_id: int = 0
    touch: int = 1
    parent_draft_id: Optional[int] = None
    subject: str = ""
    subject_variant: Optional[str] = None
    finding_type: Optional[str] = None
    evidence_json: Optional[str] = None
    body_text: str = ""
    body_html: str = ""
    gmail_draft_id: Optional[str] = None
    gmail_thread_id: Optional[str] = None
    rfc_message_id: Optional[str] = None
    qa_pass: int = 0
    qa_blockers_json: Optional[str] = None
    human_edited: int = 0
    status: str = "pending"
    reject_reason: Optional[str] = None
    reject_category: Optional[str] = None
    sent_at: Optional[str] = None
    replied_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class DncEntry:
    id: Optional[int] = None
    company: Optional[str] = None
    domain: Optional[str] = None
    email: Optional[str] = None
    reason: Optional[str] = None
    added_at: Optional[str] = None
    added_by: Optional[str] = None


@dataclass
class Event:
    id: Optional[int] = None
    prospect_id: Optional[int] = None
    draft_id: Optional[int] = None
    event_type: str = ""
    payload_json: Optional[str] = None
    created_at: Optional[str] = None
