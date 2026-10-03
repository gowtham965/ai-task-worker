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
