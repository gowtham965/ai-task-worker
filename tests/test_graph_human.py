from typing import TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from worker.graph_human import InterruptHuman, answer_interrupt
from worker.human import ScriptedHuman


class S(TypedDict):
    done: bool


def _drive(app, cfg, human):
    out = app.invoke({"done": False}, cfg)
    while out.get("__interrupt__"):
        out = app.invoke(Command(resume=answer_interrupt(out["__interrupt__"][0].value, human)), cfg)
    return out


def test_ask_and_approve_round_trip_through_interrupts():
    seen = {}

    def node(state):
        h = InterruptHuman()
        seen["answer"] = h.ask("Which Acme?", ["Acme A", "Acme B"])
        seen["approval"] = h.approve({"policy": "AP-01", "summary": "s", "fields": {}, "evidence": []})
        return {"done": True}

    g = StateGraph(S)
    g.add_node("n", node)
    g.add_edge(START, "n")
    g.add_edge("n", END)
    human = ScriptedHuman(answers={"acme": "Acme A"}, approve_all=False)
    _drive(g.compile(checkpointer=InMemorySaver()), {"configurable": {"thread_id": "t"}}, human)
    assert seen == {"answer": "Acme A", "approval": (False, "scripted approver")}
    assert human.asked == ["Which Acme?"] and len(human.approvals) == 1   # asked once despite node replays


def test_notify_collects_without_pausing():
    h = InterruptHuman(["earlier"])
    h.notify("AP-02 hold")
    assert h.notifications == ["earlier", "AP-02 hold"]


def test_unknown_payload_is_rejected():
    with pytest.raises(ValueError, match="Unknown interrupt"):
        answer_interrupt({"type": "dance"}, ScriptedHuman())
