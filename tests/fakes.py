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
    """Bluepeak's invoice is INR 62,400, above the INR 50k threshold, so propose pauses for approval."""
    finish = [("finish", {"status": final_status, "summary": f"BPS-INV-2231 {final_status}"})] * 2
    return _goal("Bluepeak"), [
        ("open_document", {"url": f"{base}/portal/download/BPS-INV-2231.pdf"}),
        ("record_facts", _facts("obs1", "Bluepeak Software Pvt Ltd", "BPS-INV-2231", "62,400.00", "2026-10-15",
                                "912010045566778")),
        PROPOSE,
        *finish,
    ]
