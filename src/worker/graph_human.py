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
