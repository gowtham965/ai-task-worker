"""The worker: goal -> plan -> act -> observe -> adapt -> verify -> report.

The loop engine. The step logic it runs (dispatch, finish gate, compaction, finalisation) lives in
steps.py and is shared with the LangGraph engine in graph.py:

  1. compile_goal   one JSON call turns the request into a GoalSpec (deliverable, success
                    criteria, facts needed, a first plan, open ambiguities)
  2. act/observe    tool-calling loop over browser, document, ledger and human tools; the
                    ledger summary is re-injected every turn as working memory
  3. propose        the only write path, validated and executed by actions.PayableWriter
  4. finish         the model's claim of done; the run outcome comes from verifier.verify
"""

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
