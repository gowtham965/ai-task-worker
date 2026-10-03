"""worker "<task>"  -- run one task against the Northwind intranet, with a human in the terminal."""

from __future__ import annotations

import argparse

import httpx
from rich.console import Console
from rich.panel import Panel

from worker import config
from worker.agent import Worker
from worker.human import ConsoleHuman


def main() -> None:
    p = argparse.ArgumentParser(description="Autonomous AP worker for the simulated Northwind Ops intranet.")
    p.add_argument("task", help="what you want done, in plain language")
    p.add_argument("--model", default=config.MODEL)
    p.add_argument("--headed", action="store_true", help="show the browser window")
    p.add_argument("--reset", action="store_true", help="reset the company to its seed state first")
    p.add_argument("--chaos", default="", help="comma-separated chaos switches to enable with --reset: "
                   "slow_first_load,session_expiry,erp_flaky,erp_redesign")
    args = p.parse_args()

    if args.reset:
        chaos = {c: True for c in args.chaos.split(",") if c}
        httpx.post(f"{config.BASE_URL}/admin/reset", json={"chaos": chaos}, timeout=10).raise_for_status()

    result = Worker(ConsoleHuman(), model=args.model, headless=not args.headed).run(args.task)
    ok = result.outcome in ("completed_verified", "completed_no_write", "escalated_safely")
    Console().print(Panel(f"{result.summary}\n\nOutcome: {result.outcome}  ·  verification "
                          f"{'passed' if result.verification['passed'] else 'FAILED'}\n"
                          f"Evidence: runs/{result.run_id}/report.html",
                          title="Done" if ok else "Not done", border_style="green" if ok else "red"))


if __name__ == "__main__":
    main()
