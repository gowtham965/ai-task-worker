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


from fakes import bluepeak_script  # noqa: E402

TASK = "Bluepeak says our new invoice is ready. Get it into the ERP."


def test_paused_run_resumes_in_a_new_runner(fresh_company, tmp_path, monkeypatch):
    from worker.graph import Paused
    monkeypatch.chdir(tmp_path)
    human = ScriptedHuman(approve_all=True)
    script = bluepeak_script(fresh_company)
    paused = _graph_runner(human, tmp_path, script).run(TASK, run_id="resume-me", detach=True)
    assert isinstance(paused, Paused) and paused.pending["type"] == "approve"
    assert paused.pending["request"]["fields"]["invoice_no"] == "BPS-INV-2231"
    assert _rows(fresh_company, "BPS-INV-2231") == []                    # nothing written while waiting

    result = _graph_runner(human, tmp_path, script).resume("resume-me")   # a new runner = a new process
    assert result.outcome == "completed_verified"
    rows = _rows(fresh_company, "BPS-INV-2231")
    assert len(rows) == 1 and rows[0]["approval_ref"]
    assert rows[0]["created_via"] == "ui"         # Review Focus 1: fresh, signed-out browser still wrote via the UI
    assert len(human.approvals) == 1              # Review Focus 2: asked once despite node replay
    events = (tmp_path / "runs" / "resume-me" / "events.jsonl").read_text()
    assert events.count('"action": "payable created"') == 1
    assert '"kind": "resumed"' in events


def test_denied_after_restart_writes_nothing(fresh_company, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    script = bluepeak_script(fresh_company, final_status="escalated")
    _graph_runner(ScriptedHuman(), tmp_path, script).run(TASK, run_id="deny-me", detach=True)
    result = _graph_runner(ScriptedHuman(approve_all=False), tmp_path, script).resume("deny-me")
    assert result.outcome == "escalated_safely"   # Review Focus 4
    assert _rows(fresh_company, "BPS-INV-2231") == []


def test_resuming_a_finished_or_unknown_run_is_a_clear_error(fresh_company, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = _graph_runner(ScriptedHuman(), tmp_path, kaveri_script(fresh_company))
    runner.run("Kaveri", run_id="done-run")
    with pytest.raises(ValueError, match="not paused"):                  # Review Focus 3
        _graph_runner(ScriptedHuman(), tmp_path, kaveri_script(fresh_company)).resume("done-run")
    with pytest.raises(ValueError, match="not paused"):
        _graph_runner(ScriptedHuman(), tmp_path, kaveri_script(fresh_company)).resume("no-such-run")


class _CrashAfterWrite(FakeLLM):
    """Dies on the model call that follows the payable write: a process crash mid-run, not a human pause."""

    def chat(self, messages, tools=None, json_mode=False):
        if sum(1 for m in messages if m["role"] == "assistant") == 3:
            raise RuntimeError("process killed")
        return super().chat(messages, tools, json_mode)


def test_crashed_run_resumes_from_its_last_checkpoint(fresh_company, tmp_path, monkeypatch):
    from worker.graph import GraphRunner
    monkeypatch.chdir(tmp_path)
    goal, calls = kaveri_script(fresh_company)
    crashing = GraphRunner(ScriptedHuman(), quiet=True, db_path=tmp_path / "cp.sqlite",
                           llm_factory=lambda: _CrashAfterWrite(goal, calls))
    with pytest.raises(RuntimeError, match="process killed"):
        crashing.run("Kaveri", run_id="crashed")
    assert len(_rows(fresh_company, "KL/2026/0934")) == 1               # the write happened before the crash

    result = _graph_runner(ScriptedHuman(), tmp_path, kaveri_script(fresh_company)).resume("crashed")
    assert result.outcome == "completed_verified"
    assert len(_rows(fresh_company, "KL/2026/0934")) == 1               # resumed, not rewritten
