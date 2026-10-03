"""The human in the loop: clarifying questions and approvals.

ConsoleHuman asks in the terminal. ScriptedHuman answers from a fixture so evals can exercise
the same pauses deterministically (and record whether the worker asked at all).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.console import Console
from rich.panel import Panel


class Human:
    def ask(self, question: str, options: list[str]) -> str:
        raise NotImplementedError

    def approve(self, request: dict) -> tuple[bool, str]:
        raise NotImplementedError

    def notify(self, message: str) -> None:
        pass


class ConsoleHuman(Human):
    def __init__(self) -> None:
        self.console = Console()

    def ask(self, question: str, options: list[str]) -> str:
        body = question + ("\n\n" + "\n".join(f"  {i + 1}. {o}" for i, o in enumerate(options)) if options else "")
        self.console.print(Panel(body, title="Worker needs your input", border_style="magenta"))
        answer = input("> ").strip()
        if answer.isdigit() and options and 0 < int(answer) <= len(options):
            return options[int(answer) - 1]
        return answer

    def approve(self, request: dict) -> tuple[bool, str]:
        lines = [request["summary"], ""]
        lines += [f"  {k}: {v}" for k, v in request["fields"].items()]
        lines += ["", "Evidence:"] + [f"  - {e}" for e in request["evidence"]]
        if request.get("warnings"):
            lines += ["", "Warnings:"] + [f"  ! {w}" for w in request["warnings"]]
        self.console.print(Panel("\n".join(lines), title=f"Approval required ({request['policy']})",
                                 border_style="yellow"))
        answer = input("Approve? [y/N] ").strip().lower()
        return answer in ("y", "yes"), "console approver"

    def notify(self, message: str) -> None:
        self.console.print(Panel(message, title="Escalation", border_style="red"))


@dataclass
class ScriptedHuman(Human):
    answers: dict[str, str] = field(default_factory=dict)   # substring of question -> answer
    approve_all: bool = True
    asked: list[str] = field(default_factory=list)
    approvals: list[dict] = field(default_factory=list)
    notifications: list[str] = field(default_factory=list)

    def ask(self, question: str, options: list[str]) -> str:
        self.asked.append(question)
        for key, answer in self.answers.items():
            if key.lower() in (question + " " + " ".join(options)).lower():
                return answer
        return "I don't know. Use your best judgement, or stop and tell me what you need."

    def approve(self, request: dict) -> tuple[bool, str]:
        self.approvals.append(request)
        return self.approve_all, "scripted approver"

    def notify(self, message: str) -> None:
        self.notifications.append(message)
