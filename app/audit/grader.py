"""Lead scoring — computed in code, never by the model.

icp = 0.6 * site_need + 0.4 * buyer_fit   (0-100)

  site_need : how much fixable, customer-visible trouble the site has, from the
              severities of findings that PASSED the fabrication guard
              (sev 3 = +30, sev 2 = +15, sev 1 = +5, capped at 100).
  buyer_fit : how likely this business is to pay for help (starts at 50):
              +15 higher-ticket industry, +10 signs of an active business,
              +10 an owner/contact name, -25 site run by an agency or a
              restaurant platform, -20 no way to contact them, -10 per disqualifier.

Not the business's own site (directory page, franchise) -> Reject outright.
The Node first-pass rating can still veto (Reject) or cap the grade; a strong
fresh audit lifts a stale Node 'C' one notch (see `combine`).
"""
from typing import Optional

from app.settings import ICP_MIN_FOR_A, ICP_MIN_FOR_B

SEVERITY_POINTS = {3: 30, 2: 15, 1: 5}
HIGH_TICKET = ("dent", "orthodont", "medical", "clinic", "chiro", "law", "legal", "attorney",
               "hvac", "plumb", "roof", "electric", "contractor", "med spa", "medspa", "auto",
               "veterin", "realt", "insurance", "account", "landscap", "remodel")
MANAGED_PLATFORMS = {"popmenu", "bentobox", "owner_com", "spothopper"}


def grade_from_icp(icp_score: int) -> str:
    if icp_score >= ICP_MIN_FOR_A:
        return "A"
    if icp_score >= ICP_MIN_FOR_B:
        return "B"
    if icp_score > 0:
        return "C"
    return "Reject"


_RANK = {"A": 4, "B": 3, "C": 2, "Reject": 1, None: 0, "": 0}


def combine(node_rating: Optional[str], icp_score: int) -> str:
    """Blend the (possibly stale) Node rating with the fresh audit grade.

      - Node 'Reject' is an absolute veto.
      - No Node rating -> trust the audit outright.
      - Otherwise the more conservative grade wins, EXCEPT a strong fresh audit
        (ICP >= icp_min_for_a) lifts a stale Node 'C' one notch to 'B'.
    """
    icp_grade = grade_from_icp(icp_score)
    nr = (node_rating or "").strip() or None
    if nr == "Reject":
        return "Reject"
    if not nr:
        return icp_grade
    conservative = nr if _RANK[nr] < _RANK[icp_grade] else icp_grade
    if conservative == "C" and icp_score >= ICP_MIN_FOR_A and _RANK[icp_grade] >= _RANK["B"]:
        return "B"
    return conservative


def score_lead(*, findings: list[dict], meta: dict, features: dict,
               industry: str | None, has_email: bool, has_phone: bool,
               contact_name: str | None) -> dict:
    """Deterministic score. Returns {icp, site_need, buyer_fit, reasons, reject_reason}."""
    reasons: list[str] = []
    ownership = meta.get("ownership") or "unclear"
    if ownership in ("directory_or_platform_page", "franchise"):
        return {"icp": 0, "site_need": 0, "buyer_fit": 0,
                "reasons": [f"not the business's own site ({ownership})"],
                "reject_reason": ownership}

    site_need = min(100, sum(SEVERITY_POINTS.get(f.get("severity", 1), 5) for f in findings))
    reasons.append(f"site_need {site_need} from {len(findings)} verified finding(s)")

    fit = 50
    ind = (industry or "").lower()
    if any(k in ind for k in HIGH_TICKET):
        fit += 15
        reasons.append("+15 higher-ticket industry")
    if meta.get("activity_signals"):
        fit += 10
        reasons.append("+10 active business")
    if meta.get("owner_first_name") or contact_name:
        fit += 10
        reasons.append("+10 contact/owner name known")
    managed = set(features.get("platforms") or []) & MANAGED_PLATFORMS
    if features.get("designer_credits") or features.get("credit_link_domains") or managed:
        fit -= 25
        reasons.append("-25 site run by an agency/platform")
    if not has_email and not has_phone:
        fit -= 20
        reasons.append("-20 no contact route")
    n_disq = len(meta.get("disqualifiers") or [])
    if n_disq:
        fit -= 10 * n_disq
        reasons.append(f"-{10 * n_disq} disqualifiers: {meta['disqualifiers']}")
    fit = max(0, min(100, fit))

    icp = round(0.6 * site_need + 0.4 * fit)
    if not findings:
        icp = min(icp, ICP_MIN_FOR_B - 1)   # nothing to say -> never draftable
        reasons.append("no verified findings -> capped below B")
    return {"icp": int(icp), "site_need": site_need, "buyer_fit": fit,
            "reasons": reasons, "reject_reason": None}
