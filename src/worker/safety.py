"""Detect instruction-like text inside external content.

This is a tripwire, not the defence. The defence is structural: external text can only become
a fact (a quoted value), facts can only reach the ERP through a typed intent, and the policy
gate checks that intent against the company's own records. The scanner's job is to make the
attempt visible: it labels the observation for the model, and taints any intent built on it so
a human must review it.
"""

from __future__ import annotations

import re

PATTERNS = [
    r"(ignore|disregard)\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+instructions",
    r"(note|message|instructions?)\s+(to|for)\s+(automated|ai|the\s+assistant|llm|bots?)\b[^.]*",
    r"do\s+not\s+(request|seek|ask\s+for|require)\s+approval",
    r"skip\s+(the\s+)?(approval|verification|checks?|review)",
    r"(post|pay|process)\s+(this|it)(\s+payable)?\s+(immediately|today|right\s+away|now)",
    r"(bank\s+(account|details)|account\s+details)\s+(has|have)\s+(been\s+)?(changed|updated)",
    r"(new|updated)\s+(bank\s+)?account\s+(printed|below|details)",
    r"verified\s+by\s+(the\s+)?[\w\s]{0,30}(controller|finance|cfo|manager)",
]
_RX = [re.compile(p, re.IGNORECASE) for p in PATTERNS]


def scan_for_injection(text: str) -> list[str]:
    hits = []
    for rx in _RX:
        if m := rx.search(text):
            hits.append(re.sub(r"\s+", " ", m.group(0))[:120])
    return hits
