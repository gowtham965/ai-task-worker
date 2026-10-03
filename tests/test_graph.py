import httpx
import pytest

from fakes import FakeLLM, kaveri_script
from worker.agent import Worker
from worker.human import ScriptedHuman


def _rows(base: str, invoice_no: str) -> list[dict]:
    return [p for p in httpx.get(f"{base}/admin/state").json()["payables"] if p["invoice_no"] == invoice_no]


def _graph_runner(human, tmp_path, script, **kw):
    from worker.graph import GraphRunner
    goal, calls = script
    return GraphRunner(human, quiet=True, db_path=tmp_path / "cp.sqlite", llm_factory=lambda: FakeLLM(goal, calls), **kw)


def _loop_runner(human, tmp_path, script, **kw):
    goal, calls = script
    return Worker(human, quiet=True, llm_factory=lambda: FakeLLM(goal, calls), **kw)


@pytest.mark.parametrize("make", [_loop_runner, _graph_runner], ids=["loop", "graph"])
def test_both_engines_complete_the_happy_path(make, fresh_company, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = make(ScriptedHuman(), tmp_path, kaveri_script(fresh_company)).run(
        "Find the latest invoice from Kaveri Logistics and enter it into the ERP.", run_id="happy")
    assert result.outcome == "completed_verified"
    assert result.verification["passed"]
    assert len(_rows(fresh_company, "KL/2026/0934")) == 1
    events = (tmp_path / "runs" / "happy" / "events.jsonl").read_text()
    assert '"kind": "goal"' in events and '"kind": "outcome"' in events
    assert events.count('"check": "self-check requested before finish"') == 1


def test_graph_stops_at_the_step_budget(fresh_company, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    goal, _ = kaveri_script(fresh_company)
    script = (goal, [("goto", {"url": f"{fresh_company}/wiki/"})])   # FakeLLM repeats the last call forever
    result = _graph_runner(ScriptedHuman(), tmp_path, script, max_steps=3).run("loop forever", run_id="budget")
    assert result.outcome == "failed" and result.steps == 3
    assert "Stopped after 3 steps" in result.summary
