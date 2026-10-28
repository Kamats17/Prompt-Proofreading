from app.audit.grader import combine, grade_from_icp


def test_grade_from_icp_buckets():
    assert grade_from_icp(80) == "A"
    assert grade_from_icp(75) == "A"
    assert grade_from_icp(60) == "B"
    assert grade_from_icp(50) == "B"
    assert grade_from_icp(20) == "C"
    assert grade_from_icp(0) == "Reject"


def test_combine_picks_more_conservative():
    # Node says A, ICP says B → final B
    assert combine("A", 60) == "B"
    # Node says B, ICP says A → final B
    assert combine("B", 80) == "B"
    # Node says Reject → always Reject
    assert combine("Reject", 95) == "Reject"
    # No node rating → use ICP
    assert combine(None, 80) == "A"
    assert combine("", 30) == "C"
