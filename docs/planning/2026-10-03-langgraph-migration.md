# LangGraph Migration Implementation Plan

**Goal:** Run the worker on LangGraph so a run can pause at a question or approval, the process can exit, and
`worker --resume <run_id>` continues it later, while the safety layer and the measured behaviour stay the same.

**Architecture:** Only the loop in `src/worker/agent.py` moves. First the loop's step logic (tool dispatch,
finish gate, context compaction, finalisation) is extracted into `steps.py` so both engines share one
implementation. Then `graph.py` wires the same steps as LangGraph nodes (`goal → agent ⇄ tools → finalize`),
with all run data in checkpointed state and live objects (browser, LLM client, trace file) in a per-process
`Runtime`. Human pauses become `interrupt()` calls via an `InterruptHuman`. `actions.py` policy logic, `ledger.record`,
and `verifier.py` are not rewritten; they only gain state export/import and one restart-safety rule.

**Tech Stack:** Python 3.13, uv, LangGraph 1.2.x, langgraph-checkpoint-sqlite 3.1.x, Playwright, pytest.

**Spec:** No separate spec. The decision is recorded in README "What I'd build next" item 1: move the loop to
LangGraph for durable checkpoints; only `agent.py` changes; the write gate, ledger and verifier are
framework-independent.

## Context an implementer needs

- Read first: `README.md` (architecture table), `src/worker/agent.py` (the loop being moved),
  `src/worker/actions.py` (`PayableWriter.propose`, the only write path), `docs/iteration-log.md` (why the
  finish gate, coverage rule and reconcile-before-retry exist; each came from a failed run).
- **LangGraph behaviour verified on 2026-10-03 with langgraph 1.2.12** (scratch tests, not assumptions):
  - `interrupt(value)` inside a node pauses the run; `invoke(Command(resume=x), cfg)` continues it, and
    `interrupt` returns `x`.
  - **On resume the interrupted node runs again from its first line.** With two interrupts in one node the
    node runs three times; code after the last interrupt runs once. So everything a node does before an
    interrupt must be safe to repeat. `PayableWriter.propose` qualifies: before asking it only reads.
  - A paused run resumes from a **new** `SqliteSaver` connection and a newly compiled graph, i.e. a new process.
  - `app.get_state(cfg)` for an unknown thread returns `values == {}` and `next == ()`.
  - `invoke` on an interrupted run returns the state dict plus `"__interrupt__": [Interrupt(value=...)]`.
- The OpenAI tool-calling setup uses `parallel_tool_calls=False`, so an assistant message has at most one tool
  call. The code still loops over `tool_calls` to stay correct if that ever changes.

## Global Constraints

- Python `>=3.13`; dependencies added: `langgraph>=1.2,<2` and `langgraph-checkpoint-sqlite>=3.1,<4`.
- No LangChain message classes and no `langchain-openai`: messages stay plain OpenAI-format dicts, and the LLM
  stays `worker.llm.LLM`.
- `PayableWriter.propose` policy order (same-document rule, normalisation, vendor, ambiguity gate, AP-03, AP-02,
  AP-01, execute, read back), `Ledger.record`, and `verifier.verify` are not modified except where a task says so.
- The loop engine stays available (`--engine loop`) until the graph engine meets the parity bar in Task 7.
- Checkpoints live at `runs/checkpoints.sqlite` (already git-ignored via `runs/`).
- All new tests run offline: no OpenAI key, using `tests/fakes.py::FakeLLM` and the in-process company server.
- Parity bar for switching the default engine: graph engine on `evals/run.py --repeat 3` scores **≥ 25/27 runs**,
  **0 incorrect writes** (audit script), and mean cost per task within **+15 %** of v5 ($0.042).

## Review Focus

1. **Restart loses the browser session.** A resumed run has a fresh, signed-out browser; the ERP write after an
   approval must still succeed (via `_via_ui`'s sign-in). Pinned in Task 5: resumed payable has `created_via == "ui"`.
2. **Node replay repeats side effects.** Resuming re-runs the tools node; the human must be asked once and the
   payable written once. Pinned in Task 5: `len(human.approvals) == 1` and exactly one `"payable created"` event.
3. **Resuming a finished or unknown run.** Expect a clear `ValueError("… is not paused …")`, not a crash or a
   silent re-run. Pinned in Task 5.
4. **Approval denied after a restart.** Expect no write and a safe outcome. Pinned in Task 5.
5. **Crash between the ERP write and the checkpoint.** On replay `propose` finds the payable it already wrote
   and must adopt it, not report a duplicate (which the verifier would flag as an unexpected write). Pinned in Task 5.

---

## File structure

| File | Change | Responsibility |
|---|---|---|
| `src/worker/prompts.py` | Create | `ENVIRONMENT`, `GOAL_PROMPT`, `SYSTEM`, `TOOLS` (moved verbatim from `agent.py`) |
| `src/worker/steps.py` | Create | Engine-independent step logic: `Ctx`, `RunResult`, `parse_call`, `dispatch`, `finish_gate`, `compact`, `initial_messages`, `tool_message`, `memory_suffix`, `NUDGE`, `final_outcome`, `finalize_run` |
| `src/worker/agent.py` | Modify | Loop engine reduced to `Worker.run` + `Worker._loop`, using `steps.py`; gains `llm_factory` |
| `src/worker/ledger.py` | Modify | `to_state()` / `from_state()` |
| `src/worker/actions.py` | Modify | `state()` / `restore()`, `preexisting_ids`, adopt-own-write rule |
| `src/worker/browser.py` | Modify | `coverage_state()` / `restore_coverage()`, idempotent `close()` |
| `src/worker/trace.py` | Modify | `resume=` flag, idempotent `close()` |
| `src/worker/graph_human.py` | Create | `InterruptHuman`, `answer_interrupt` |
| `src/worker/graph.py` | Create | `State`, `Runtime`, `Paused`, `build_graph`, `GraphRunner` |
| `src/worker/config.py` | Modify | `ENGINE` setting |
| `src/worker/cli.py` | Modify | `--engine`, `--detach`, `--resume`, `--approve/--deny` |
| `evals/run.py` | Modify | `--engine` |
| `evals/audit.py` | Create | Incorrect-write audit across result files |
| `tests/conftest.py` | Modify | Session-scoped in-process company server fixture |
| `tests/fakes.py` | Create | `FakeLLM` and scripted runs |
| `tests/test_steps.py`, `test_state.py`, `test_graph_human.py`, `test_graph.py` | Create | Tests per task |

---

### Task 1: Extract engine-independent step logic

A pure refactor: the loop engine's behaviour must not change. Everything the graph engine will reuse moves out
of `agent.py`.

**Files:**
- Create: `src/worker/prompts.py`, `src/worker/steps.py`, `tests/test_steps.py`
- Modify: `src/worker/agent.py` (whole file), `src/worker/browser.py` (`close`), `src/worker/trace.py` (`close`)

**Interfaces:**
- Produces: `Ctx(trace, ledger, browser, writer, human)`; `parse_call(call: dict) -> tuple[str, dict]`;
  `dispatch(name: str, args: dict, ctx: Ctx) -> str`; `FinishDecision(done, status, summary, reply, flags, log)`;
  `finish_gate(args: dict, goal: dict, gaps: list[str], flags: dict, steps_left: int) -> FinishDecision`;
  `compact(messages: list[dict]) -> None`; `initial_messages(task: str, goal: dict) -> list[dict]`;
  `tool_message(call: dict, content: str) -> dict`; `memory_suffix(ledger) -> str`; `NUDGE: str`;
  `RunResult` (moved from agent.py, same fields); `final_outcome(agent_status, verification, writer) -> str`;
  `finalize_run(*, task, goal, before, status, summary, steps, trace, ledger, llm, browser, writer) -> RunResult`;
  `Worker(human, model=..., headless=True, quiet=False, max_steps=..., llm_factory=None)`.

- [ ] **Step 1: Write the failing tests for the finish gate**

`tests/test_steps.py`:

```python
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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_steps.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'worker.steps'`

- [ ] **Step 3: Create `src/worker/prompts.py`**

Move these four definitions from `agent.py` **verbatim** (cut, don't retype): `ENVIRONMENT`, `GOAL_PROMPT`,
`SYSTEM`, and `TOOLS` together with its helpers `_fn` and `S`. Header:

```python
"""Prompts and tool schemas shared by both engines (loop and graph)."""

from __future__ import annotations

from worker import config

# ENVIRONMENT = ...   (moved verbatim from agent.py)
# GOAL_PROMPT = ...   (moved verbatim)
# SYSTEM = ...        (moved verbatim)
# def _fn(...)        (moved verbatim)
# S = {"type": "string"}
# TOOLS = [...]       (moved verbatim)
```

- [ ] **Step 4: Create `src/worker/steps.py`**

```python
"""One worker step, independent of the engine that drives it (agent.py loop or graph.py LangGraph).

Both engines call the same dispatch, finish gate, compaction and finalisation, so a behaviour difference
between them can only come from wiring, never from duplicated logic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from worker.actions import IntentRejected, PayableWriter
from worker.ledger import Ledger, ProvenanceError
from worker.prompts import SYSTEM
from worker.verifier import verify

NUDGE = "If you are done, call finish now with your summary. Otherwise continue with a tool call."
ELIDED = "\n[older observation elided; facts are in working memory; use recall(obsN) to re-read it]"


@dataclass
class Ctx:
    trace: object
    ledger: Ledger
    browser: object
    writer: PayableWriter
    human: object


@dataclass
class RunResult:
    run_id: str
    task: str
    goal: dict
    agent_status: str
    summary: str
    outcome: str
    verification: dict
    steps: int
    usage: dict
    cost_usd: float
    ledger: dict
    writes: list[dict]


@dataclass
class FinishDecision:
    done: bool
    status: str | None
    summary: str
    reply: str
    flags: dict
    log: dict = field(default_factory=dict)


def initial_messages(task: str, goal: dict) -> list[dict]:
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"Request: {task}\n\nYour GoalSpec:\n{json.dumps(goal, indent=1)}"}]


def parse_call(call: dict) -> tuple[str, dict]:
    try:
        args = json.loads(call["function"]["arguments"] or "{}")
    except json.JSONDecodeError:
        args = {}
    return call["function"]["name"], args


def tool_message(call: dict, content: str) -> dict:
    return {"role": "tool", "tool_call_id": call["id"], "content": content}


def memory_suffix(ledger: Ledger) -> str:
    return f"\n\n[working memory]\n{ledger.summary()}"


def compact(messages: list[dict]) -> None:
    """Keep the last 6 tool results whole; older page dumps shrink to their header line.
    Facts survive in the ledger, so nothing the task depends on is lost."""
    for m in [m for m in messages if m["role"] == "tool"][:-6]:
        if not m["content"].endswith(ELIDED):
            m["content"] = m["content"].split("\n", 1)[0][:200] + ELIDED


def finish_gate(args: dict, goal: dict, gaps: list[str], flags: dict, steps_left: int) -> FinishDecision:
    """Decide whether a finish call ends the run. Same rules as the v5 loop, now a pure function."""
    status, flags = args.get("status", "failed"), dict(flags)
    if status == "completed" and goal.get("scope") == "collection" and gaps:
        # Code-enforced: a collection answer can't be "completed" while code measures unopened items.
        return FinishDecision(False, None, "", "Not accepted. Coverage measured by code:\n" + "\n".join(gaps)
                              + f"\nOpen the remaining items, then finish. ({steps_left} steps left.)", flags,
                              {"check": "finish refused: collection not covered", "gaps": gaps})
    if status == "failed" and gaps and not flags.get("pushed_back"):
        flags["pushed_back"] = True
        return FinishDecision(False, None, "", "Before giving up: these items were never opened:\n"
                              + "\n".join(gaps) + "\nUse goto on their URLs directly. If none can help, finish again.",
                              flags, {"check": "failure pushed back: unopened items remain", "gaps": gaps})
    if status == "completed" and not flags.get("self_checked"):
        flags["self_checked"] = True
        criteria = "\n".join(f"- {c}" for c in goal.get("success_criteria", []))
        return FinishDecision(False, None, "", (
            "Before finishing, prove each success criterion from what you observed. For every criterion, write one "
            "line: the criterion, MET or NOT MET, and the obsN/factN that shows it. An unchecked item counts as NOT "
            f"MET.\n{criteria}\nIf anything is NOT MET, keep working. Otherwise call finish again with a summary "
            "consistent with that evidence."), flags, {"check": "self-check requested before finish"})
    return FinishDecision(True, status, args.get("summary", ""), "", flags)


def dispatch(name: str, args: dict, ctx: Ctx) -> str:
    """Execute one non-finish tool call and return the observation text for the model."""
    trace, ledger, browser, writer, human = ctx.trace, ctx.ledger, ctx.browser, ctx.writer, ctx.human
    try:
        match name:
            case "goto":
                return browser.safe(browser.goto, args["url"])
            case "click":
                return browser.safe(browser.click, args["ref"])
            case "fill":
                return browser.safe(browser.fill, args["ref"], args["text"])
            case "login":
                return browser.safe(browser.login, args["site"])
            case "open_document":
                return browser.safe(browser.open_document, args["url"])
            case "recall":
                obs = ledger.observations.get(args["observation_id"])
                return browser.render(obs, limit=6000) if obs else f"ERROR: no observation {args['observation_id']}"
            case "record_fact":
                fact = ledger.record(args["key"], str(args["value"]), args["observation_id"], args["quote"])
                trace.log("fact", id=fact.id, key=fact.key, value=fact.value, source=fact.source)
                return f"Recorded {fact.id}: {fact.key} = {fact.value!r}"
            case "record_facts":
                lines = []
                for item in args.get("facts", []):
                    try:
                        fact = ledger.record(item["key"], str(item["value"]), item["observation_id"], item["quote"])
                        trace.log("fact", id=fact.id, key=fact.key, value=fact.value, source=fact.source)
                        lines.append(f"Recorded {fact.id}: {fact.key} = {fact.value!r}")
                    except (ProvenanceError, KeyError) as e:
                        trace.log("error", tool="record_facts", key=item.get("key"), error=str(e))
                        lines.append(f"REJECTED {item.get('key')}: {e}")
                return "\n".join(lines) or "ERROR: no facts given"
            case "propose_create_payable":
                outcome = writer.propose(**{k: args.get(k, "") for k in (
                    "vendor_name_fact", "invoice_no_fact", "amount_fact", "due_date_fact", "payee_account_fact")})
                trace.log("policy", result=outcome.status, message=outcome.message)
                return f"{outcome.status.upper()}: {outcome.message}" + (
                    f"\nStored record: {json.dumps(outcome.record)}" if outcome.record else "")
            case "ask_user":
                trace.log("ask_user", question=args.get("question"), options=args.get("options", []))
                answer = human.ask(args.get("question", ""), args.get("options", []))
                trace.log("ask_user", answer=answer)
                writer.clarifications.append({"question": args.get("question"), "answer": answer})
                return f"User answered: {answer}"
            case "escalate":
                item = {"policy": args.get("policy"), "reason": args.get("reason"),
                        "escalate_to": args.get("escalate_to"), "raised_by": "worker"}
                writer.escalations.append(item)
                trace.log("policy", decision="escalated by worker", **item)
                human.notify(f"{item['policy']}: {item['reason']} (to: {item['escalate_to']})")
                return "Escalation sent. Nothing further is required from you on this item."
            case "update_plan":
                trace.log("plan", plan=args.get("plan"), reason=args.get("reason"))
                return "Plan updated."
            case _:
                return f"ERROR: unknown tool {name}"
    except (ProvenanceError, IntentRejected) as e:
        trace.log("error", tool=name, error=str(e))
        return f"ERROR: {e}"
    except KeyError as e:
        return f"ERROR: missing argument {e}"


def final_outcome(agent_status: str, verification: dict, writer: PayableWriter) -> str:
    """The run's outcome is decided by the verifier, not by what the agent says."""
    if not verification["passed"]:
        return "failed_verification"
    if agent_status == "completed" and writer.executed:
        return "completed_verified"
    if agent_status == "completed":
        return "completed_no_write"
    if agent_status == "escalated":
        return "escalated_safely"
    return agent_status or "failed"


def finalize_run(*, task, goal, before, status, summary, steps, trace, ledger, llm, browser, writer) -> RunResult:
    """Screenshot, close the browser, verify independently, write result.json and report.html."""
    try:
        browser.screenshot("final")
    except Exception:  # noqa: BLE001 - evidence is best-effort
        pass
    browser.close()
    verification = verify(before, writer, ledger)
    for c in verification["checks"]:
        trace.log("verify", **c)
    outcome = final_outcome(status, verification, writer)
    trace.log("outcome", outcome=outcome, agent_status=status, summary=summary, cost_usd=llm.cost_usd(), **llm.usage)
    result = RunResult(trace.run_id, task, goal, status, summary, outcome, verification, steps, dict(llm.usage),
                       llm.cost_usd(), ledger.to_dict(), writer.executed)
    (trace.dir / "result.json").write_text(json.dumps(result.__dict__, default=str, indent=2))
    trace.close()
    from worker.report import render

    render(trace.dir)
    return result
```

- [ ] **Step 5: Make `Browser.close` and `Trace.close` idempotent**

Both engines (and `GraphRunner`'s cleanup) may call `close` more than once.

`src/worker/browser.py`: in `__init__`, after `self.page.set_default_timeout(...)`, add `self._closed = False`.
Replace `close`:

```python
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.context.close()
        self._browser.close()
        self._pw.stop()
```

`src/worker/trace.py`: replace `close`:

```python
    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()
```

- [ ] **Step 6: Rewrite `src/worker/agent.py` on top of `steps.py`**

Replace everything below the module docstring with:

```python
from __future__ import annotations

from typing import Callable

from worker import config
from worker.actions import PayableWriter
from worker.browser import Browser
from worker.human import Human
from worker.ledger import Ledger
from worker.llm import LLM
from worker.prompts import GOAL_PROMPT, TOOLS
from worker.steps import (NUDGE, Ctx, RunResult, compact, dispatch, finalize_run, finish_gate, initial_messages,
                          memory_suffix, parse_call, tool_message)
from worker.trace import Trace
from worker.verifier import snapshot

__all__ = ["Worker", "RunResult"]


class Worker:
    """Loop engine: one Python process drives the run from start to finish."""

    def __init__(self, human: Human, model: str = config.MODEL, headless: bool = True, quiet: bool = False,
                 max_steps: int = config.MAX_STEPS, llm_factory: Callable[[], LLM] | None = None) -> None:
        self.human, self.headless, self.quiet, self.max_steps = human, headless, quiet, max_steps
        self.llm_factory = llm_factory or (lambda: LLM(model))

    def run(self, task: str, run_id: str | None = None) -> RunResult:
        trace = Trace(task, run_id, quiet=self.quiet)
        ledger, llm = Ledger(), self.llm_factory()
        browser = Browser(ledger, trace, headless=self.headless)
        writer = PayableWriter(ledger, trace, self.human, browser)
        ctx = Ctx(trace, ledger, browser, writer, self.human)
        before = snapshot()
        try:
            goal = llm.json(GOAL_PROMPT, task)
            trace.log("goal", **goal)
            writer.named_vendor = goal.get("named_vendor")
            status, summary, steps = self._loop(task, goal, ctx, llm)
        except BaseException:
            browser.close()
            raise
        return finalize_run(task=task, goal=goal, before=before, status=status, summary=summary, steps=steps,
                            trace=trace, ledger=ledger, llm=llm, browser=browser, writer=writer)

    def _loop(self, task: str, goal: dict, ctx: Ctx, llm) -> tuple[str, str, int]:
        messages = initial_messages(task, goal)
        nudges, flags = 0, {"self_checked": False, "pushed_back": False}
        for step in range(1, self.max_steps + 1):
            compact(messages)
            reply = llm.chat(messages, TOOLS)
            messages.append(reply)
            if reply["content"]:
                ctx.trace.log("thought", text=reply["content"][:500])
            if not reply.get("tool_calls"):
                nudges += 1          # consecutive text-only replies (e.g. a written self-check)
                if nudges > 3:
                    return "failed", "Stopped: the model stopped calling tools.", step
                messages.append({"role": "user", "content": NUDGE})
                continue
            nudges = 0
            for call in reply["tool_calls"]:
                name, args = parse_call(call)
                ctx.trace.log("tool", step=step, name=name, args=args)
                if name == "finish":
                    decision = finish_gate(args, goal, ctx.browser.coverage(), flags, self.max_steps - step)
                    flags = decision.flags
                    if decision.done:
                        return decision.status, decision.summary, step
                    ctx.trace.log("verify", **decision.log)
                    messages.append(tool_message(call, decision.reply))
                    continue
                messages.append(tool_message(call, dispatch(name, args, ctx) + memory_suffix(ctx.ledger)))
        ctx.trace.log("error", error=f"step budget of {self.max_steps} exhausted")
        return "failed", f"Stopped after {self.max_steps} steps without finishing.", self.max_steps
```

Keep the existing module docstring at the top, updated to say the steps live in `steps.py`.

- [ ] **Step 7: Run all tests**

Run: `uv run pytest -q`
Expected: all pass (20 existing + 5 new).

- [ ] **Step 8: Live smoke test of the loop engine (needs the OpenAI key and `uv run company` running)**

Run: `uv run python evals/run.py --only kaveri_happy,meridian_bec --label "refactor smoke"`
Expected: both PASS (same as v5). If either fails, diff behaviour against `git show HEAD~1:src/worker/agent.py`
before moving on.

- [ ] **Step 9: Commit**

```bash
git add src/worker/prompts.py src/worker/steps.py src/worker/agent.py src/worker/browser.py src/worker/trace.py tests/test_steps.py
git commit -m "refactor: extract engine-independent step logic from the loop"
```

---

### Task 2: Make run state exportable and restorable

Everything a run depends on must round-trip through JSON so LangGraph can checkpoint it.

**Files:**
- Modify: `src/worker/ledger.py`, `src/worker/actions.py`, `src/worker/browser.py`, `src/worker/trace.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Produces: `Ledger.to_state() -> dict`, `Ledger.from_state(state: dict) -> Ledger`;
  `PayableWriter.state() -> dict`, `PayableWriter.restore(state: dict) -> None`,
  `PayableWriter.preexisting_ids: set[int] | None` (default `None`);
  `Browser.coverage_state() -> dict`, `Browser.restore_coverage(state: dict) -> None`;
  `Trace(task, run_id=None, quiet=False, resume=False)`.

- [ ] **Step 1: Write the failing tests**

`tests/test_state.py`:

```python
import json

from worker.ledger import Ledger

INVOICE = "TAX INVOICE\nInvoice No KL/2026/0934\nTotal Amount Payable (INR) 23,780.00\n" + "x" * 5000


def test_ledger_round_trip_keeps_full_text_and_ids():
    ledger = Ledger()
    obs = ledger.observe("http://x/inv.pdf", "document", INVOICE, trusted=False, flags=["skip approval"])
    ledger.record("amount", "23780.00", obs.id, "Total Amount Payable (INR) 23,780.00")
    restored = Ledger.from_state(json.loads(json.dumps(ledger.to_state())))
    assert restored.observations["obs1"].text == INVOICE          # to_dict() truncates; to_state() must not
    assert restored.observations["obs1"].flags == ["skip approval"]
    assert restored.get("fact1").value == "23780.00"
    nxt = restored.observe("http://x/2", "page", "p", trusted=True)
    assert nxt.id == "obs2"                                         # numbering continues after restore


class _FakeBrowser:
    pass


def test_writer_state_round_trip(monkeypatch):
    from worker import actions
    monkeypatch.setattr(actions, "load_policies", lambda: {})
    w = actions.PayableWriter(Ledger(), trace=None, human=None, browser=_FakeBrowser())
    w.executed.append({"intent": "create_payable", "payload": {"invoice_no": "X"}, "vendor": {"id": 1},
                       "facts": {"amount": "fact3"}, "record": {"id": 4}})
    w.escalations.append({"policy": "AP-02"})
    w.clarifications.append({"question": "Which Acme?", "answer": "A"})
    w.named_vendor, w._approvals = "Acme", 2
    w2 = actions.PayableWriter(Ledger(), trace=None, human=None, browser=_FakeBrowser())
    w2.restore(json.loads(json.dumps(w.state())))
    assert w2.state() == w.state()
    assert w2.preexisting_ids is None


def test_browser_coverage_round_trip():
    from worker.browser import Browser
    b = Browser.__new__(Browser)                # no Playwright needed for this state
    b.listings, b.visited = {"http://h/mail/": ["http://h/mail/1", "http://h/mail/2"]}, {"http://h/mail/1"}
    state = json.loads(json.dumps(Browser.coverage_state(b)))
    b2 = Browser.__new__(Browser)
    Browser.restore_coverage(b2, state)
    assert b2.listings == b.listings and b2.visited == b.visited
    assert len(Browser.coverage(b2)) == 1 and "/mail/2" in Browser.coverage(b2)[0]


def test_trace_resume_does_not_log_task_again(tmp_path, monkeypatch):
    from worker.trace import Trace
    monkeypatch.chdir(tmp_path)
    Trace("do x", "r1", quiet=True).close()
    t = Trace("do x", "r1", quiet=True, resume=True)
    t.close()
    lines = (tmp_path / "runs" / "r1" / "events.jsonl").read_text().splitlines()
    assert sum('"kind": "task"' in line for line in lines) == 1
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_state.py -v`
Expected: FAIL with `AttributeError: 'Ledger' object has no attribute 'to_state'` (and similar for the others).

- [ ] **Step 3: Implement `Ledger.to_state` / `from_state`**

Add to `class Ledger` in `src/worker/ledger.py`:

```python
    def to_state(self) -> dict:
        """Full JSON-safe copy for checkpointing. Unlike to_dict(), never truncates observation text."""
        return {"observations": [asdict(o) for o in self.observations.values()],
                "facts": [asdict(f) for f in self.facts.values()]}

    @classmethod
    def from_state(cls, state: dict) -> "Ledger":
        ledger = cls()
        for o in state.get("observations", []):
            ledger.observations[o["id"]] = Observation(**o)
        for f in state.get("facts", []):
            ledger.facts[f["id"]] = Fact(**f)
        return ledger
```

- [ ] **Step 4: Implement `PayableWriter.state` / `restore` and `preexisting_ids`**

In `src/worker/actions.py`, `PayableWriter.__init__`, after `self._approvals = 0` add:

```python
        # Payable ids that existed before this run (set by the graph engine). Used to recognise, after a crash
        # and replay, a payable this run already wrote. None means "unknown" (loop engine): no adoption.
        self.preexisting_ids: set[int] | None = None
```

Add methods to `PayableWriter`:

```python
    def state(self) -> dict:
        return {"executed": list(self.executed), "escalations": list(self.escalations),
                "clarifications": list(self.clarifications), "named_vendor": self.named_vendor,
                "approvals": self._approvals}

    def restore(self, state: dict) -> None:
        self.executed = list(state.get("executed", []))
        self.escalations = list(state.get("escalations", []))
        self.clarifications = list(state.get("clarifications", []))
        self.named_vendor = state.get("named_vendor")
        self._approvals = int(state.get("approvals", 0))
```

- [ ] **Step 5: Implement `Browser.coverage_state` / `restore_coverage`**

Add to `class Browser` in `src/worker/browser.py`:

```python
    def coverage_state(self) -> dict:
        return {"listings": {k: list(v) for k, v in self.listings.items()}, "visited": sorted(self.visited)}

    def restore_coverage(self, state: dict) -> None:
        self.listings = {k: list(v) for k, v in state.get("listings", {}).items()}
        self.visited = set(state.get("visited", []))
```

- [ ] **Step 6: Add `resume` to `Trace`**

In `src/worker/trace.py` change the signature and the last line of `__init__`:

```python
    def __init__(self, task: str, run_id: str | None = None, quiet: bool = False, resume: bool = False) -> None:
        ...
        if not resume:
            self.log("task", task=task)
```

(`t` in events restarts at 0 in a resumed process; the `resumed` event added in Task 4 marks the boundary.)

- [ ] **Step 7: Run tests**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/worker/ledger.py src/worker/actions.py src/worker/browser.py src/worker/trace.py tests/test_state.py
git commit -m "feat: exportable run state for ledger, writer, coverage and trace"
```

---

### Task 3: LangGraph dependency and interrupt-based human

**Files:**
- Modify: `pyproject.toml` (via `uv add`)
- Create: `src/worker/graph_human.py`, `tests/test_graph_human.py`

**Interfaces:**
- Produces: `InterruptHuman(notifications: list[str] | None = None)` implementing `Human` (`ask`, `approve`,
  `notify`; `.notifications` list); `answer_interrupt(payload: dict, human: Human) -> str | dict`.
  Payloads: `{"type": "ask", "question": str, "options": list[str]}` → resume value `str`;
  `{"type": "approve", "request": dict}` → resume value `{"approved": bool, "by": str}`.

- [ ] **Step 1: Add dependencies**

Run: `uv add "langgraph>=1.2,<2" "langgraph-checkpoint-sqlite>=3.1,<4"`
Expected: `pyproject.toml` lists both; `uv run python -c "import langgraph, langgraph.checkpoint.sqlite"` succeeds.

- [ ] **Step 2: Write the failing tests**

`tests/test_graph_human.py`:

```python
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
```

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run pytest tests/test_graph_human.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'worker.graph_human'`

- [ ] **Step 4: Implement `src/worker/graph_human.py`**

```python
"""The human in the loop, as LangGraph interrupts.

Inside the graph, PayableWriter and the ask_user tool talk to an InterruptHuman. Asking pauses the run
(LangGraph checkpoints it); whoever drives the graph answers with answer_interrupt() using a real Human
(console, scripted, or a later process via `worker --resume`).
"""

from __future__ import annotations

from langgraph.types import interrupt

from worker.human import Human


class InterruptHuman(Human):
    def __init__(self, notifications: list[str] | None = None) -> None:
        self.notifications = list(notifications or [])

    def ask(self, question: str, options: list[str]) -> str:
        return interrupt({"type": "ask", "question": question, "options": list(options)})

    def approve(self, request: dict) -> tuple[bool, str]:
        answer = interrupt({"type": "approve", "request": request})
        return bool(answer["approved"]), str(answer.get("by", "unknown"))

    def notify(self, message: str) -> None:
        # Not a pause: collected in state and delivered by the runner after the step.
        self.notifications.append(message)


def answer_interrupt(payload: dict, human: Human):
    """Turn a pending interrupt into the resume value, by asking a real human."""
    if payload.get("type") == "ask":
        return human.ask(payload["question"], payload.get("options", []))
    if payload.get("type") == "approve":
        ok, by = human.approve(payload["request"])
        return {"approved": ok, "by": by}
    raise ValueError(f"Unknown interrupt payload: {payload!r}")
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml uv.lock src/worker/graph_human.py tests/test_graph_human.py
git commit -m "feat: interrupt-based human for the LangGraph engine"
```

---

### Task 4: The graph engine (in-process run)

**Files:**
- Create: `src/worker/graph.py`, `tests/fakes.py`, `tests/test_graph.py`
- Modify: `tests/conftest.py`, `tests/test_policy_gate.py` (use the shared server fixture)

**Interfaces:**
- Consumes: everything from Tasks 1–3.
- Produces: `GraphRunner(human, model=config.MODEL, headless=True, quiet=False, max_steps=config.MAX_STEPS,
  db_path=CHECKPOINT_DB, llm_factory=None)` with `.run(task, run_id=None, detach=False) -> RunResult | Paused`
  and `.resume(run_id, detach=False) -> RunResult | Paused`; `Paused(run_id: str, pending: dict)`;
  `CHECKPOINT_DB: Path`. Test helpers: `FakeLLM(goal, calls)`, `kaveri_script(base)`,
  `bluepeak_script(base, final_status="completed")`, fixture `company_url`.

- [ ] **Step 1: Share the company server between test modules**

Replace `tests/conftest.py` with:

```python
import dataclasses
import os
import tempfile
import threading
import time

import pytest

# Tests get their own company database so they never touch a running demo or eval.
os.environ.setdefault("COMPANY_DATA_DIR", tempfile.mkdtemp(prefix="northwind-test-"))

PORT = 8811


@pytest.fixture(scope="session")
def company_url():
    """The Northwind intranet, in-process on a spare port. Points config (and the vault) at it."""
    import uvicorn

    from company import seed
    from company.server import app
    from worker import config

    seed.reset()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{PORT}"
    old_base, old_vault = config.BASE_URL, dict(config.VAULT)
    config.BASE_URL = base
    for site, creds in old_vault.items():   # login URLs were built from the old BASE_URL at import time
        config.VAULT[site] = dataclasses.replace(creds, login_url=f"{base}/{site}/login")
    yield base
    config.BASE_URL = old_base
    config.VAULT.clear()
    config.VAULT.update(old_vault)
    server.should_exit = True


@pytest.fixture
def fresh_company(company_url):
    """Reset data, sessions and chaos before a test (through the server, so its in-memory state resets too)."""
    import httpx

    httpx.post(f"{company_url}/admin/reset", json={"chaos": {}}, timeout=10).raise_for_status()
    return company_url
```

In `tests/test_policy_gate.py`: delete the module's own `company` fixture and its `PORT` / uvicorn / threading
imports, and add at the top of the fixtures:

```python
@pytest.fixture(autouse=True)
def _company(company_url):
    yield
```

Run: `uv run pytest -q` → Expected: all pass (the policy-gate tests now use the shared server).

- [ ] **Step 2: Write `tests/fakes.py`**

```python
"""A scripted stand-in for worker.llm.LLM so engine tests run offline and deterministically."""

from __future__ import annotations

import json


class FakeLLM:
    """Returns a fixed GoalSpec, then the scripted tool calls in order.

    The position in the script is the number of assistant messages already in the conversation, so the script
    continues correctly after a checkpoint/restore in a new process."""

    def __init__(self, goal: dict, calls: list[tuple[str, dict]]) -> None:
        self.goal, self.calls = goal, calls
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
        self.model = "fake"

    def json(self, system: str, user: str) -> dict:
        self.usage["calls"] += 1
        return dict(self.goal)

    def chat(self, messages: list[dict], tools=None, json_mode: bool = False) -> dict:
        self.usage["calls"] += 1
        i = sum(1 for m in messages if m["role"] == "assistant")
        name, args = self.calls[min(i, len(self.calls) - 1)]
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": f"call{i + 1}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}

    def cost_usd(self) -> float:
        return 0.0


def _facts(obs: str, vendor: str, no: str, amount: str, due: str, account: str) -> dict:
    return {"facts": [
        {"key": "vendor_name", "value": vendor, "observation_id": obs, "quote": vendor},
        {"key": "invoice_no", "value": no, "observation_id": obs, "quote": f"Invoice No {no}"},
        {"key": "amount", "value": amount, "observation_id": obs, "quote": f"Total Amount Payable (INR) {amount}"},
        {"key": "due_date", "value": due, "observation_id": obs, "quote": f"Due Date {due}"},
        {"key": "payee_account", "value": account, "observation_id": obs, "quote": f"Account No: {account}"}]}


PROPOSE = ("propose_create_payable", {"vendor_name_fact": "fact1", "invoice_no_fact": "fact2", "amount_fact": "fact3",
                                      "due_date_fact": "fact4", "payee_account_fact": "fact5"})


def _goal(vendor: str) -> dict:
    return {"goal": f"Enter the latest {vendor} invoice", "deliverable": "confirmation",
            "success_criteria": ["payable exists in the ERP"], "facts_needed": [], "plan": [],
            "scope": "single", "named_vendor": vendor, "ambiguities": [], "risky_actions": []}


def kaveri_script(base: str):
    return _goal("Kaveri Logistics"), [
        ("open_document", {"url": f"{base}/files/KL-2026-0934.pdf"}),
        ("record_facts", _facts("obs1", "Kaveri Logistics Pvt Ltd", "KL/2026/0934", "23,780.00", "2026-10-26",
                                "50200011223344")),
        PROPOSE,
        ("finish", {"status": "completed", "summary": "Entered KL/2026/0934"}),
        ("finish", {"status": "completed", "summary": "Entered KL/2026/0934 (self-checked)"}),
    ]


def bluepeak_script(base: str, final_status: str = "completed"):
    """Bluepeak's invoice is ₹62,400, above the ₹50k threshold, so propose pauses for approval."""
    finish = [("finish", {"status": final_status, "summary": f"BPS-INV-2231 {final_status}"})] * 2
    return _goal("Bluepeak"), [
        ("open_document", {"url": f"{base}/portal/download/BPS-INV-2231.pdf"}),
        ("record_facts", _facts("obs1", "Bluepeak Software Pvt Ltd", "BPS-INV-2231", "62,400.00", "2026-10-15",
                                "912010045566778")),
        PROPOSE,
        *finish,
    ]
```

- [ ] **Step 3: Write the failing engine test**

`tests/test_graph.py`:

```python
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
```

- [ ] **Step 4: Run it to verify the graph cases fail**

Run: `uv run pytest tests/test_graph.py -v`
Expected: the `loop` case PASSES (proves the fake script and fixture are right); the `graph` cases FAIL with
`ModuleNotFoundError: No module named 'worker.graph'`.

- [ ] **Step 5: Implement `src/worker/graph.py`**

```python
"""LangGraph engine: the same worker as agent.py, with durable checkpoints.

    goal ──▶ agent ⇄ tools ──▶ finalize

Live objects (Playwright browser, LLM client, trace file handle) can't be checkpointed, so they live in a
per-process Runtime rebuilt from state. Everything the run depends on (messages, ledger, writer state,
coverage, token usage) is in the checkpointed State. Questions and approvals are LangGraph interrupts, so a
run can pause, the process can exit, and GraphRunner.resume(run_id) continues it later.

Replay rule: when a run resumes, the interrupted node runs again from its first line. Nodes therefore copy
state before changing it, and the only node that can interrupt (tools) does nothing irreversible before the
interrupt. PayableWriter.propose only reads before it asks.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from worker import config
from worker.actions import PayableWriter
from worker.browser import Browser
from worker.graph_human import InterruptHuman, answer_interrupt
from worker.human import Human
from worker.ledger import Ledger
from worker.llm import LLM
from worker.prompts import GOAL_PROMPT, TOOLS
from worker.steps import (NUDGE, Ctx, RunResult, compact, dispatch, finalize_run, finish_gate, initial_messages,
                          memory_suffix, parse_call, tool_message)
from worker.trace import RUNS_DIR, Trace
from worker.verifier import snapshot

CHECKPOINT_DB = RUNS_DIR / "checkpoints.sqlite"


class State(TypedDict, total=False):
    task: str
    run_id: str
    goal: dict
    before: dict              # ERP snapshot at the start, for the verifier's before/after diff
    messages: list[dict]
    step: int
    nudges: int
    flags: dict               # finish-gate flags: self_checked, pushed_back
    status: str
    summary: str
    ledger: dict
    writer: dict
    coverage: dict
    usage: dict
    notifications: list[str]
    result: dict


@dataclass
class Paused:
    run_id: str
    pending: dict             # the interrupt payload: {"type": "ask" | "approve", ...}


class Runtime:
    """Per-process live objects, rebuilt from checkpointed state the first time a node runs."""

    def __init__(self, llm_factory: Callable[[], LLM], headless: bool, quiet: bool) -> None:
        self.llm_factory, self.headless, self.quiet = llm_factory, headless, quiet
        self.trace = self.browser = None

    def attach(self, state: State) -> None:
        if self.trace is not None:
            return
        resumed = "goal" in state
        self.trace = Trace(state["task"], state["run_id"], quiet=self.quiet, resume=resumed)
        self.ledger = Ledger.from_state(state["ledger"]) if state.get("ledger") else Ledger()
        self.llm = self.llm_factory()
        if state.get("usage"):
            self.llm.usage = dict(state["usage"])
        self.browser = Browser(self.ledger, self.trace, headless=self.headless)
        if state.get("coverage"):
            self.browser.restore_coverage(state["coverage"])
        self.human = InterruptHuman(state.get("notifications", []))
        self.writer = PayableWriter(self.ledger, self.trace, self.human, self.browser)
        if state.get("writer"):
            self.writer.restore(state["writer"])
        if state.get("before"):
            self.writer.preexisting_ids = {p["id"] for p in state["before"]["payables"]}
        if resumed:
            self.trace.log("resumed", step=state.get("step", 0))

    @property
    def ctx(self) -> Ctx:
        return Ctx(self.trace, self.ledger, self.browser, self.writer, self.human)

    def export(self) -> dict:
        return {"ledger": self.ledger.to_state(), "writer": self.writer.state(),
                "coverage": self.browser.coverage_state(), "usage": dict(self.llm.usage),
                "notifications": list(self.human.notifications)}

    def close(self) -> None:
        if self.browser is not None:
            self.browser.close()
        if self.trace is not None:
            self.trace.close()


def build_graph(rt: Runtime, max_steps: int) -> StateGraph:
    def goal_node(state: State) -> dict:
        rt.attach(state)
        before = snapshot()
        rt.writer.preexisting_ids = {p["id"] for p in before["payables"]}
        goal = rt.llm.json(GOAL_PROMPT, state["task"])
        rt.trace.log("goal", **goal)
        rt.writer.named_vendor = goal.get("named_vendor")
        return {"goal": goal, "before": before, "messages": initial_messages(state["task"], goal), "step": 0,
                "nudges": 0, "flags": {"self_checked": False, "pushed_back": False}, **rt.export()}

    def agent_node(state: State) -> dict:
        rt.attach(state)
        messages = [dict(m) for m in state["messages"]]
        compact(messages)
        reply = rt.llm.chat(messages, TOOLS)
        messages.append(reply)
        if reply["content"]:
            rt.trace.log("thought", text=reply["content"][:500])
        update = {"step": state["step"] + 1, "usage": dict(rt.llm.usage)}
        if reply.get("tool_calls"):
            return {**update, "messages": messages, "nudges": 0}
        nudges = state["nudges"] + 1           # consecutive text-only replies
        if nudges > 3:
            return {**update, "messages": messages, "nudges": nudges, "status": "failed",
                    "summary": "Stopped: the model stopped calling tools."}
        messages.append({"role": "user", "content": NUDGE})
        return {**update, "messages": messages, "nudges": nudges}

    def tools_node(state: State) -> dict:
        rt.attach(state)
        messages, flags = list(state["messages"]), dict(state["flags"])
        for call in messages[-1]["tool_calls"]:
            name, args = parse_call(call)
            rt.trace.log("tool", step=state["step"], name=name, args=args)
            if name == "finish":
                decision = finish_gate(args, state["goal"], rt.browser.coverage(), flags, max_steps - state["step"])
                flags = decision.flags
                if decision.done:
                    return {"messages": messages, "flags": flags, "status": decision.status,
                            "summary": decision.summary, **rt.export()}
                rt.trace.log("verify", **decision.log)
                messages.append(tool_message(call, decision.reply))
                continue
            messages.append(tool_message(call, dispatch(name, args, rt.ctx) + memory_suffix(rt.ledger)))
        return {"messages": messages, "flags": flags, **rt.export()}

    def finalize_node(state: State) -> dict:
        rt.attach(state)
        status, summary = state.get("status"), state.get("summary", "")
        if not status:
            rt.trace.log("error", error=f"step budget of {max_steps} exhausted")
            status, summary = "failed", f"Stopped after {max_steps} steps without finishing."
        result = finalize_run(task=state["task"], goal=state["goal"], before=state["before"], status=status,
                              summary=summary, steps=state["step"], trace=rt.trace, ledger=rt.ledger, llm=rt.llm,
                              browser=rt.browser, writer=rt.writer)
        return {"status": status, "summary": summary, "result": asdict(result), **rt.export()}

    def after_agent(state: State) -> str:
        if state.get("status"):
            return "finalize"
        if state["messages"][-1].get("tool_calls"):
            return "tools"
        return "agent" if state["step"] < max_steps else "finalize"

    def after_tools(state: State) -> str:
        return "finalize" if state.get("status") or state["step"] >= max_steps else "agent"

    g = StateGraph(State)
    g.add_node("goal", goal_node)
    g.add_node("agent", agent_node)
    g.add_node("tools", tools_node)
    g.add_node("finalize", finalize_node)
    g.add_edge(START, "goal")
    g.add_edge("goal", "agent")
    g.add_conditional_edges("agent", after_agent, ["tools", "agent", "finalize"])
    g.add_conditional_edges("tools", after_tools, ["agent", "finalize"])
    g.add_edge("finalize", END)
    return g


class GraphRunner:
    """Drives the graph: answers interrupts with a real Human, or stops (detach) and resumes later."""

    def __init__(self, human: Human, model: str = config.MODEL, headless: bool = True, quiet: bool = False,
                 max_steps: int = config.MAX_STEPS, db_path: Path = CHECKPOINT_DB,
                 llm_factory: Callable[[], LLM] | None = None) -> None:
        self.human, self.headless, self.quiet, self.max_steps = human, headless, quiet, max_steps
        self.db_path = Path(db_path)
        self.llm_factory = llm_factory or (lambda: LLM(model))

    def run(self, task: str, run_id: str | None = None, detach: bool = False) -> RunResult | Paused:
        run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
        return self._drive(run_id, {"task": task, "run_id": run_id}, detach)

    def resume(self, run_id: str, detach: bool = False) -> RunResult | Paused:
        app, rt, cfg = self._app(run_id)
        snap = app.get_state(cfg)
        pending = [i.value for t in snap.tasks for i in t.interrupts]
        if not snap.next or not pending:
            rt.close()
            raise ValueError(f"Run {run_id} is not paused (it finished, or no such run).")
        return self._drive(run_id, Command(resume=answer_interrupt(pending[0], self.human)), detach, (app, rt, cfg))

    def _app(self, run_id: str):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        rt = Runtime(self.llm_factory, self.headless, self.quiet)
        app = build_graph(rt, self.max_steps).compile(checkpointer=SqliteSaver(conn))
        # Each worker step is ~2 graph steps (agent + tools); set the limit explicitly rather than rely on defaults.
        cfg = {"configurable": {"thread_id": run_id}, "recursion_limit": self.max_steps * 3 + 10}
        return app, rt, cfg

    def _drive(self, run_id: str, payload, detach: bool, built=None) -> RunResult | Paused:
        app, rt, cfg = built or self._app(run_id)
        try:
            while True:
                delivered = len(app.get_state(cfg).values.get("notifications", []))
                out = app.invoke(payload, cfg)
                for message in out.get("notifications", [])[delivered:]:
                    self.human.notify(message)
                pending = out.get("__interrupt__")
                if not pending:
                    return RunResult(**out["result"])
                if detach:
                    return Paused(run_id, pending[0].value)
                payload = Command(resume=answer_interrupt(pending[0].value, self.human))
        finally:
            rt.close()
```

- [ ] **Step 6: Run the engine tests**

Run: `uv run pytest tests/test_graph.py -v`
Expected: 3 passed (`loop`, `graph`, budget).

- [ ] **Step 7: Run all tests and commit**

Run: `uv run pytest -q` → Expected: all pass.

```bash
git add src/worker/graph.py tests/fakes.py tests/test_graph.py tests/conftest.py tests/test_policy_gate.py
git commit -m "feat: LangGraph engine with checkpointed state, sharing step logic with the loop"
```

---

### Task 5: Durability: pause, exit, resume

This is the reason for the migration. The tests pin all five Review Focus items.

**Files:**
- Modify: `src/worker/actions.py` (`propose`, AP-03 branch)
- Test: `tests/test_graph.py`, `tests/test_policy_gate.py`

**Interfaces:**
- Consumes: `GraphRunner.run(..., detach=True)`, `GraphRunner.resume(run_id)`, `Paused`,
  `PayableWriter.preexisting_ids`, `bluepeak_script(base, final_status)`.

- [ ] **Step 1: Write the failing durability tests**

Append to `tests/test_graph.py`:

```python
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
```

Append to `tests/test_policy_gate.py`:

```python
def test_payable_written_before_a_crash_is_adopted_not_duplicated(writer):
    """Review Focus 5: the process died after the ERP write but before the checkpoint; replay must adopt it."""
    w, ledger, _ = writer
    before = httpx.get(f"{config.BASE_URL}/admin/state").json()
    w.preexisting_ids = {p["id"] for p in before["payables"]}
    httpx.post(f"{config.BASE_URL}/erp/api/payables", headers={"Authorization": "Bearer erp-demo-token"}, json={
        "vendor_id": 1, "invoice_no": "KL/2026/0934", "amount": "23780.00", "due_date": "2026-10-26",
        "payee_account": "50200011223344", "approval_ref": ""})          # the write that "happened before the crash"
    ids = _invoice_facts(ledger, "Kaveri Logistics Pvt Ltd", "KL/2026/0934", "23,780.00", "2026-10-26",
                         "50200011223344")
    out = w.propose(*ids)
    assert out.status == "created" and "adopted" in out.message
    assert len(w.executed) == 1
    rows = [p for p in httpx.get(f"{config.BASE_URL}/admin/state").json()["payables"]
            if p["invoice_no"] == "KL/2026/0934"]
    assert len(rows) == 1


def test_genuine_duplicate_still_reported_when_preexisting(writer):
    w, ledger, _ = writer
    w.preexisting_ids = {p["id"] for p in httpx.get(f"{config.BASE_URL}/admin/state").json()["payables"]}
    ids = _invoice_facts(ledger, "Sharma Office Supplies", "SOS-1187", "7,960.00", "2026-10-09", "30112233445")
    assert w.propose(*ids).status == "duplicate"
```

- [ ] **Step 2: Run them**

Run: `uv run pytest tests/test_graph.py tests/test_policy_gate.py -v`
Expected: the three graph durability tests PASS already if Task 4 is correct (they pin behaviour);
`test_payable_written_before_a_crash_is_adopted_not_duplicated` FAILS with `assert 'duplicate' == 'created'`.
If a graph durability test fails, fix `graph.py` before continuing; do not weaken the assertion.

- [ ] **Step 3: Implement adopt-own-write in `PayableWriter.propose`**

In `src/worker/actions.py`, replace the AP-03 block

```python
        if existing and "no_duplicates" in self.policies:
            rec = existing[0]
            self.trace.log("policy", policy="AP-03", decision="no write: duplicate", existing=rec)
```

with

```python
        if existing and "no_duplicates" in self.policies:
            rec = existing[0]
            already_ours = any(w["record"] and w["record"]["id"] == rec["id"] for w in self.executed)
            if (self.preexisting_ids is not None and rec["id"] not in self.preexisting_ids and not already_ours
                    and abs(rec["amount"] - fields["amount"]) < 0.005 and rec["payee_account"] == fields["payee_account"]):
                # Written by this run before a crash/restart, but never checkpointed. Adopt it; never write again.
                # (Prototype assumption: no one else enters this invoice during the run. See README limitations.)
                self.trace.log("recovery", failure="payable from this run found after restart", payable=rec,
                               strategy="adopt it; do not write again")
                self.executed.append({"intent": "create_payable", "vendor": vendor,
                                      "payload": {**fields, "amount": f"{fields['amount']:.2f}",
                                                  "approval_ref": rec.get("approval_ref") or ""},
                                      "facts": {k: f.id for k, f in facts.items()}, "record": rec})
                return Outcome("created", f"Payable #{rec['id']} was already written by this run before a restart; "
                               "adopted, not written again.", rec["id"], rec)
            self.trace.log("policy", policy="AP-03", decision="no write: duplicate", existing=rec)
```

(The rest of the AP-03 block, the `return Outcome("duplicate", ...)`, stays as is.)

- [ ] **Step 4: Run all tests**

Run: `uv run pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/worker/actions.py tests/test_graph.py tests/test_policy_gate.py
git commit -m "feat: durable pause/resume for approvals; adopt own writes after a crash"
```

---

### Task 6: CLI and eval harness

**Files:**
- Modify: `src/worker/config.py`, `src/worker/cli.py`, `evals/run.py`

**Interfaces:**
- Consumes: `GraphRunner`, `Paused`, `Worker`.
- Produces: `config.ENGINE` (`"loop"` default until Task 7); CLI `worker [task] [--engine loop|graph]
  [--detach] [--resume RUN_ID] [--approve | --deny]`; `evals/run.py --engine`.

- [ ] **Step 1: Add the engine setting**

`src/worker/config.py`, after `MAX_STEPS`:

```python
ENGINE = os.environ.get("WORKER_ENGINE", "loop")   # "loop" (agent.py) or "graph" (LangGraph, durable)
```

- [ ] **Step 2: Rewrite `src/worker/cli.py`**

```python
"""worker "<task>"  -- run one task against the Northwind intranet, with a human in the terminal.

Graph engine extras:
  worker --engine graph --detach "<task>"   stop at the first question/approval and exit
  worker --resume <run_id> [--approve|--deny]   continue a paused run (in a new process)
"""

from __future__ import annotations

import argparse

import httpx
from rich.console import Console
from rich.panel import Panel

from worker import config
from worker.agent import Worker
from worker.human import ConsoleHuman, ScriptedHuman


def main() -> None:
    p = argparse.ArgumentParser(description="Autonomous AP worker for the simulated Northwind Ops intranet.")
    p.add_argument("task", nargs="?", help="what you want done, in plain language")
    p.add_argument("--model", default=config.MODEL)
    p.add_argument("--headed", action="store_true", help="show the browser window")
    p.add_argument("--reset", action="store_true", help="reset the company to its seed state first")
    p.add_argument("--chaos", default="", help="comma-separated chaos switches to enable with --reset: "
                   "slow_first_load,session_expiry,erp_flaky,erp_redesign")
    p.add_argument("--engine", choices=["loop", "graph"], default=config.ENGINE)
    p.add_argument("--detach", action="store_true", help="graph engine: exit at the first question or approval")
    p.add_argument("--resume", metavar="RUN_ID", help="continue a paused graph run")
    decision = p.add_mutually_exclusive_group()
    decision.add_argument("--approve", action="store_true", help="with --resume: approve without prompting")
    decision.add_argument("--deny", action="store_true", help="with --resume: deny without prompting")
    args = p.parse_args()
    console = Console()

    if args.resume:
        from worker.graph import GraphRunner
        human = ScriptedHuman(approve_all=args.approve) if (args.approve or args.deny) else ConsoleHuman()
        out = GraphRunner(human, model=args.model, headless=not args.headed).resume(args.resume, detach=args.detach)
    else:
        if not args.task:
            p.error("a task is required unless --resume is given")
        if args.reset:
            chaos = {c: True for c in args.chaos.split(",") if c}
            httpx.post(f"{config.BASE_URL}/admin/reset", json={"chaos": chaos}, timeout=10).raise_for_status()
        if args.engine == "graph":
            from worker.graph import GraphRunner
            out = GraphRunner(ConsoleHuman(), model=args.model, headless=not args.headed).run(
                args.task, detach=args.detach)
        else:
            out = Worker(ConsoleHuman(), model=args.model, headless=not args.headed).run(args.task)

    from worker.graph import Paused
    if isinstance(out, Paused):
        what = out.pending.get("question") or out.pending.get("request", {}).get("summary", "")
        console.print(Panel(f"Waiting for a human: {out.pending['type']}\n{what}\n\n"
                            f"Resume later with:\n  uv run worker --resume {out.run_id}   (add --approve or --deny)",
                            title="Paused (state saved)", border_style="yellow"))
        return
    ok = out.outcome in ("completed_verified", "completed_no_write", "escalated_safely")
    console.print(Panel(f"{out.summary}\n\nOutcome: {out.outcome}  ·  verification "
                        f"{'passed' if out.verification['passed'] else 'FAILED'}\n"
                        f"Evidence: runs/{out.run_id}/report.html",
                        title="Done" if ok else "Not done", border_style="green" if ok else "red"))


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Add `--engine` to `evals/run.py`**

After `from worker.human import ScriptedHuman  # noqa: E402` add:

```python
from worker.graph import GraphRunner  # noqa: E402
```

In `main()`, after the `--label` argument:

```python
    ap.add_argument("--engine", choices=["loop", "graph"], default=config.ENGINE)
```

Replace the line constructing the worker inside the `try:` with:

```python
                engine = GraphRunner if args.engine == "graph" else Worker
                result = engine(human, model=args.model, quiet=True).run(
                    task["task"], run_id=f"eval-{stamp}-{task['id']}-{i}")
```

And record the engine in the results file and heading:

```python
    (out / f"{stamp}.json").write_text(json.dumps({"model": args.model, "engine": args.engine, "label": args.label,
                                                    "rows": rows}, indent=1))
```

```python
    lines = [f"# Eval {stamp} ({args.model}, {args.engine} engine) {args.label}", "",
```

- [ ] **Step 4: Manual check of the detach/resume flow (needs `uv run company` and the OpenAI key)**

Run: `uv run worker --reset --engine graph --detach "Bluepeak says our new invoice is ready. Get it into the ERP."`
Expected: a yellow "Paused (state saved)" panel naming the approval and a `--resume <run_id>` command; the ERP
payables page shows no BPS-INV-2231.

Run: `uv run worker --resume <run_id> --approve`
Expected: green "Done" panel, outcome `completed_verified`; `runs/<run_id>/report.html` shows a `resumed` event.

- [ ] **Step 5: Run all tests and commit**

Run: `uv run pytest -q` → Expected: all pass.

```bash
git add src/worker/config.py src/worker/cli.py evals/run.py
git commit -m "feat: --engine graph, --detach and --resume in the CLI; engine flag in evals"
```

---

### Task 7: Parity eval, then switch the default

**Files:**
- Create: `evals/audit.py`
- Modify: `src/worker/config.py` (default engine), `README.md`, `docs/iteration-log.md`

- [ ] **Step 1: Create the incorrect-write audit**

`evals/audit.py`:

```python
"""Audit eval results for incorrect writes: wrong values, unexpected payables, writes in no-write tasks.

    uv run python evals/audit.py                       # all results
    uv run python evals/audit.py evals/results/X.json  # one run
"""

import glob
import json
import sys

files = sys.argv[1:] or sorted(glob.glob("evals/results/*.json"))
total, bad = 0, []
for f in files:
    for r in json.load(open(f))["rows"]:
        total += 1
        for name, ok, detail in r["checks"]:
            wrong_value = name.startswith("created") and detail != "missing"
            if not ok and (name in ("no unexpected writes", "no writes") or wrong_value):
                bad.append((f, r["task"], r["rep"], name, detail[:120]))
print(f"scored runs: {total}, incorrect writes: {len(bad)}")
for b in bad:
    print(b)
sys.exit(1 if bad else 0)
```

Run: `uv run python evals/audit.py`
Expected: `scored runs: 110, incorrect writes: 0` (the existing v1–v5 results), exit code 0.

- [ ] **Step 2: Run the graph engine on the full suite**

Run: `uv run python evals/run.py --engine graph --repeat 3 --label "v6 graph"`
Then: `uv run python evals/audit.py evals/results/<new-stamp>.json`

Acceptance (all three): **≥ 25/27 runs pass**, **audit exit code 0**, mean cost per task **≤ $0.048**
(v5 mean $0.042 + 15 %).
If any fails: open the failing runs' `report.html`, compare with the same task's v5 run, and fix the wiring in
`graph.py` (not the prompts). Re-run only the failing tasks with `--only`, then the full suite again.

- [ ] **Step 3: Switch the default engine**

`src/worker/config.py`:

```python
ENGINE = os.environ.get("WORKER_ENGINE", "graph")  # "graph" (LangGraph, durable) or "loop" (agent.py)
```

Run: `uv run pytest -q` → Expected: all pass.

- [ ] **Step 4: Record it in the iteration log**

Append to `docs/iteration-log.md`, with the numbers from the v6 results file:

```markdown
## v6: LangGraph engine

Moved the loop to LangGraph (`src/worker/graph.py`) for durable runs: a question or approval pauses the run,
state is checkpointed to SQLite, and `worker --resume <run_id>` continues it in a new process. The step logic
(`steps.py`), write gate, ledger and verifier are shared with the loop engine, so behaviour can only differ in
wiring.

| | loop engine (v5) | graph engine (v6) |
|---|---|---|
| runs passed | 26/27 | <from results> |
| tasks pass^3 | 8/9 | <from results> |
| mean cost / task | $0.042 | <from results> |
| incorrect writes (audit) | 0 | <from audit> |

New behaviour pinned by tests: resume in a new process writes once and asks once; a denied approval after a
restart writes nothing; resuming a finished run is a clear error; a payable written just before a crash is
adopted on replay instead of being reported as a duplicate.
```

- [ ] **Step 5: Update the README**

In `README.md`:
1. In the architecture table, change the agent-loop row to:
   `| Agent loop | src/worker/graph.py (default), agent.py | LangGraph engine: goal → agent ⇄ tools → finalize, checkpointed to SQLite; questions and approvals are interrupts, so runs survive restarts. The original hand-written loop remains as --engine loop. Shared step logic lives in steps.py |`
2. Replace the "Hand-written loop, not LangGraph" decision bullet with:
   `- **LangGraph for durability, not by default.** v1–v5 used a hand-written loop so every transition was visible while iterating. v6 moved the wiring to LangGraph for checkpoints and interrupts; the step logic and safety layer are shared and framework-independent, and the eval suite showed parity before the default changed.`
3. In "Run it", add:
   ```bash
   uv run worker --engine graph --detach "Bluepeak says our new invoice is ready. Get it into the ERP."
   ```
   ```bash
   uv run worker --resume <run_id> --approve
   ```
4. Remove item 1 from "What I'd build next" (done) and renumber; remove the "Sequential, single-run state; no
   resume-after-crash" limitation; add the limitation "Adopting a payable after a crash assumes no one else
   entered the same invoice during the run."

- [ ] **Step 6: Commit**

```bash
git add evals/audit.py evals/results src/worker/config.py README.md docs/iteration-log.md
git commit -m "feat: LangGraph engine is the default after parity eval (v6)"
```

---

## After the plan

Delete the loop engine (`agent.py`'s `Worker`, `--engine loop`) one version later, once the graph engine has run
the suite twice with no regressions. Keep `steps.py`.
