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
