"""Append-only run trace: every step, tool call, recovery and decision, plus screenshots.

Written as runs/<run_id>/events.jsonl while the run is in progress, so a crash still leaves
evidence behind. `report.py` renders it into a single HTML page at the end.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

RUNS_DIR = Path("runs")


class Trace:
    def __init__(self, task: str, run_id: str | None = None, quiet: bool = False) -> None:
        self.run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir = RUNS_DIR / self.run_id
        (self.dir / "shots").mkdir(parents=True, exist_ok=True)
        self.events: list[dict] = []
        self.started = time.time()
        self.quiet = quiet
        self._fh = open(self.dir / "events.jsonl", "a")
        self.log("task", task=task)

    def log(self, kind: str, **data) -> dict:
        event = {"t": round(time.time() - self.started, 2), "kind": kind, **data}
        self.events.append(event)
        self._fh.write(json.dumps(event, default=str) + "\n")
        self._fh.flush()
        if not self.quiet:
            _print(event)
        return event

    def shot_path(self, label: str) -> Path:
        n = sum(1 for _ in (self.dir / "shots").iterdir()) + 1
        return self.dir / "shots" / f"{n:02d}-{label}.png"

    def close(self) -> None:
        self._fh.close()


_COLORS = {"tool": "cyan", "recovery": "yellow", "policy": "magenta", "approval": "magenta", "ask_user": "magenta",
           "verify": "green", "error": "red", "outcome": "bold green", "goal": "bold blue", "plan": "blue",
           "injection": "bold red", "fact": "white", "intent": "bold magenta"}


def _print(event: dict) -> None:
    from rich.console import Console
    from rich.text import Text

    kind = event["kind"]
    if kind in ("llm",):
        return
    body = {k: v for k, v in event.items() if k not in ("t", "kind")}
    text = json.dumps(body, default=str, ensure_ascii=False)
    if len(text) > 300:
        text = text[:300] + "…"
    line = Text.assemble((f"{event['t']:>6}s ", "dim"), (f"{kind:<9} ", _COLORS.get(kind, "white")), text)
    Console().print(line, highlight=False, soft_wrap=True)
