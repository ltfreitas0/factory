from factory.sm import IllegalTransition, can, transition


def test_human_accepts_inbox():
    assert transition("inbox", "ready_to_plan", "human") == "ready_to_plan"


def test_runner_cannot_accept_inbox():
    assert not can("inbox", "ready_to_plan", "runner")


def test_plan_approve_is_human():
    assert can("plan_review", "implementing", "human")
    assert not can("plan_review", "implementing", "runner")


def test_proposed_is_human_only():
    assert transition("proposed", "ready_to_plan", "human") == "ready_to_plan"
    assert not can("proposed", "ready_to_plan", "runner")
    assert not can("proposed", "planning", "runner")


def test_validate_only_path():
    assert transition("inbox", "ready_to_validate", "human") == "ready_to_validate"
    assert transition("ready_to_validate", "validating", "runner") == "validating"
    assert transition("validating", "done", "runner") == "done"


def test_illegal_raises():
    try:
        transition("done", "inbox", "human")
    except IllegalTransition:
        return
    raise AssertionError("expected IllegalTransition")
