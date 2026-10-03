"""worker "<task>"  -- run one task against the Northwind intranet, with a human in the terminal.

Graph engine extras:
  worker --engine graph --detach "<task>"       stop at the first question/approval and exit
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
    p.add_argument("--resume", metavar="RUN_ID", help="continue a paused (or crashed) graph run")
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
