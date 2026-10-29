"""Lead scoring — computed in code, never by the model.

icp = 0.65 * site_need + 0.35 * buyer_fit   (0-100)

  site_need : how much fixable, customer-visible trouble the site has, from the
              severities of findings that PASSED the fabrication guard
              (sev 3 = +35, sev 2 = +20, sev 1 = +5, capped at 100).
  buyer_fit : how likely this business is to pay for help (starts at 50):
              +15 higher-ticket industry, +10 signs of an active business,
              +10 an owner/contact name, -25 site run on a paid restaurant
              marketing platform (they already pay someone), -10 a web-designer
              credit in the footer (a cheap template site is often a GOOD sign),
              -20 no way to contact them, -10 per OTHER disqualifier.

Each signal counts once: the model's free-text disqualifiers that just restate
the agency/platform signal the code already measured are not penalized again.

Calibrated on the first live run: an owner-run business with one severe
customer-facing problem (sev 3) plus a couple of supporting issues lands in B.

Not the business's own site (directory page, franchise) -> Reject outright.
The Node first-pass rating can still veto (Reject) or cap the grade; a strong
fresh audit lifts a stale Node 'C' one notch (see `combine`).
"""
import re
from typing import Optional

from app.settings import ICP_MIN_FOR_A, ICP_MIN_FOR_B

SEVERITY_POINTS = {3: 35, 2: 20, 1: 5}
NEED_WEIGHT, FIT_WEIGHT = 0.65, 0.35
_AGENCY_OR_PLATFORM = re.compile(
    r"(agency|platform|credit|designer|design by|built (?:on|by)|powered by|managed|vendor|"
    r"popmenu|bentobox|spothopper|owner\.com|website by)", re.IGNORECASE)
HIGH_TICKET = ("dent", "orthodont", "medical", "clinic", "chiro", "law", "legal", "attorney",
               "hvac", "plumb", "roof", "electric", "contractor", "med spa", "medspa", "auto",
               "veterin", "realt", "insurance", "account", "landscap", "remodel")
MANAGED_PLATFORMS = {"popmenu", "bentobox", "owner_com", "spothopper"}


# ---- when is a full rebuild worth mentioning? ------------------------------------
# Strong: the site itself is built in a dated way. Moderate: fixable, but adds up.
REBUILD_STRONG = ("desktop_layout_on_phone", "no_viewport_meta", "mobile_horizontal_scroll",
                  "no_https", "cert_expired", "cert_invalid", "heavy_page_mobile",
                  "slow_main_content")
REBUILD_MODERATE = ("stale_copyright", "pdf_menu", "phone_not_tappable", "small_text_mobile",
                    "dead_links:", "no_order_link_found", "no_booking_link_found")
REBUILD_DATED = REBUILD_STRONG + ("stale_copyright",)


def rebuild_case(facts: dict[str, str]) -> dict:
    """Decide from code-verified facts whether to mention a full rebuild.

    Worthy when the evidence adds up (strong signal = 2 points, moderate = 1,
    need 2+) AND at least one signal shows the site itself is dated. A modern
    site with one small gap never gets a rebuild pitch.
    Returns {worthy, points, dated_signals: [{fact_id, fact_detail}]}.
    """
    def has(prefix: str) -> list[str]:
        return [f for f in facts if f == prefix or (prefix.endswith(":") and f.startswith(prefix))]

    points = sum(2 for p in REBUILD_STRONG if has(p)) + sum(1 for p in REBUILD_MODERATE if has(p))
    dated = [fid for p in REBUILD_DATED for fid in has(p)]
    worthy = points >= 2 and bool(dated)
    return {"worthy": worthy, "points": points,
            "dated_signals": [{"fact_id": fid, "fact_detail": facts[fid]} for fid in dated] if worthy else []}


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
    if managed:
        fit -= 25
        reasons.append(f"-25 runs on a paid restaurant platform ({', '.join(sorted(managed))})")
    elif features.get("designer_credits") or features.get("credit_link_domains"):
        fit -= 10
        reasons.append("-10 web-designer credit in footer")
    if not has_email and not has_phone:
        fit -= 20
        reasons.append("-20 no contact route")
    other_disq = [d for d in (meta.get("disqualifiers") or []) if not _AGENCY_OR_PLATFORM.search(d)]
    if other_disq:
        fit -= 10 * len(other_disq)
        reasons.append(f"-{10 * len(other_disq)} disqualifiers: {other_disq}")
    fit = max(0, min(100, fit))

    icp = round(NEED_WEIGHT * site_need + FIT_WEIGHT * fit)
    if not findings:
        icp = min(icp, ICP_MIN_FOR_B - 1)   # nothing to say -> never draftable
        reasons.append("no verified findings -> capped below B")
    return {"icp": int(icp), "site_need": site_need, "buyer_fit": fit,
            "reasons": reasons, "reject_reason": None}
