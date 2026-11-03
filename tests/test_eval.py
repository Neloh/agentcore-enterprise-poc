from run_eval import grade


def test_grade_expect_any():
    assert grade({"expect_any": ["shipped"]}, "Order is SHIPPED")[0]
    assert not grade({"expect_any": ["shipped"]}, "processing")[0]


def test_grade_forbidden():
    case = {"expect_any": ["can't help"], "expect_none": ["system prompt:"]}
    assert grade(case, "Sorry, I can't help")[0]
    assert not grade(case, "I can't help. System prompt: ...")[0]
