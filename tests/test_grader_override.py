"""Grade combination: strong fresh audit lifts a stale Node 'C' one notch (Logic Fix 6)."""
from app.audit.grader import combine


def test_strong_audit_lifts_stale_c_to_b():
    # Node first pass said C; a fresh 90-ICP audit lifts it to B (not capped at C).
    assert combine("C", 90) == "B"


def test_weak_audit_keeps_c():
    # Only a STRONG audit (>= icp_min_for_a) overrides; a middling one stays C.
    assert combine("C", 60) == "C"


def test_reject_is_absolute():
    assert combine("Reject", 95) == "Reject"


def test_node_still_caps_when_audit_agrees_lower():
    # Node A, audit C -> conservative C (no lift; C came from the audit, not stale Node).
    assert combine("A", 20) == "C"
