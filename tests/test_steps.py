from worker.steps import finish_gate

GOAL_SINGLE = {"scope": "single", "success_criteria": ["payable exists", "amount matches"]}
GOAL_COLLECTION = {"scope": "collection", "success_criteria": ["every inbox invoice checked"]}
FRESH = {"self_checked": False, "pushed_back": False}
GAPS = ["/mail/ lists 8 items; you opened 2. Not opened: /mail/1"]


def test_collection_cannot_complete_with_gaps():
    d = finish_gate({"status": "completed", "summary": "done"}, GOAL_COLLECTION, GAPS, FRESH, 10)
    assert not d.done and d.reply.startswith("Not accepted") and "10 steps left" in d.reply
    assert d.flags == FRESH and d.log["check"] == "finish refused: collection not covered"


def test_failed_with_gaps_is_pushed_back_once():
    first = finish_gate({"status": "failed"}, GOAL_SINGLE, GAPS, FRESH, 5)
    assert not first.done and first.flags["pushed_back"] and first.reply.startswith("Before giving up")
    second = finish_gate({"status": "failed", "summary": "gave up"}, GOAL_SINGLE, GAPS, first.flags, 4)
    assert second.done and second.status == "failed" and second.summary == "gave up"


def test_first_completed_gets_self_check_with_criteria():
    first = finish_gate({"status": "completed", "summary": "x"}, GOAL_SINGLE, [], FRESH, 5)
    assert not first.done and first.flags["self_checked"]
    assert "- payable exists" in first.reply and "MET or NOT MET" in first.reply
    second = finish_gate({"status": "completed", "summary": "final"}, GOAL_SINGLE, [], first.flags, 4)
    assert second.done and second.status == "completed" and second.summary == "final"


def test_escalated_finishes_immediately():
    d = finish_gate({"status": "escalated", "summary": "held"}, GOAL_SINGLE, [], FRESH, 5)
    assert d.done and d.status == "escalated"


def test_missing_status_counts_as_failed():
    d = finish_gate({}, GOAL_SINGLE, [], {"self_checked": True, "pushed_back": True}, 5)
    assert d.done and d.status == "failed" and d.summary == ""
