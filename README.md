# ai-task-worker

An autonomous AI worker that takes a plain-language request ("find the latest invoice from
Kaveri Logistics, extract the amount and due date, enter it into our ERP, and tell me when it's done")
and does the work across a company's tools: an email inbox, a vendor portal behind a login,
PDF invoices, a policy wiki and an ERP. It checks its own result independently and hands back a
report with evidence.

It runs against **Northwind Ops**, a small simulated company included in this repo. Because the
environment is ours, failures can be switched on deliberately: slow pages, an expiring session, a 503,
a redesigned form, a duplicate invoice, an ambiguous vendor, and an invoice that tries to redirect
payment to a fraudster's bank account. The evals measure how the worker handles each of them.

> **Results:** see [Measured results](#measured-results) and [docs/iteration-log.md](docs/iteration-log.md).

## The idea in one line

**The model proposes, code decides.** The LLM reads, navigates, reasons and plans. It cannot write a
value into a company system directly. Every value it wants to write has to be a *fact*: a verbatim
quote from a page or document it actually opened. A write is a *typed intent* that references those
facts, and plain code checks the intent against the company's own policies and records before
anything changes. Trust comes from the deterministic layer around the model, not from the prompt.

## Architecture

```
 request ─▶ GoalSpec ─────────────▶ act / observe loop ─────────────────────▶ finish (a claim)
            goal, success criteria,  tools: goto · click · fill · login ·          │
            facts needed, plan,      open_document · record_fact ·                 ▼
            ambiguities              ask_user · update_plan              Independent verifier
                                       │                                 (separate read channel,
                                       │ propose_create_payable(fact ids)  before/after diff,
                                       ▼                                   re-fetches every source)
                              ┌──────────────────────────┐                         │
                              │ PayableWriter (code)     │                         ▼
                              │ 1 resolve facts          │               outcome + report.html
                              │ 2 same-document rule     │               (facts with provenance,
                              │ 3 vendor-master lookup   │                checks, screenshots,
                              │ 4 policy gate from wiki  │                full trace)
                              │   AP-03 duplicate → stop │
                              │   AP-02 bank ≠ master    │
                              │         → hold+escalate  │
                              │   AP-01 > ₹50k → approval│
                              │   tainted source→approval│
                              │ 5 execute: UI → API      │
                              │ 6 read back              │
                              └──────────────────────────┘
```

| Piece | File | What it does |
|---|---|---|
| Simulated company | `src/company/` | FastAPI intranet: `/mail`, `/portal` (Bluepeak's billing site, separate login), `/erp` (vendor master, payables, validated form, JSON API), `/wiki` (policies, also as JSON), `/admin` (read-only state for verification, reset, chaos switches) |
| Agent loop | `src/worker/agent.py` | Hand-written loop: GoalSpec → tool calls → finish. Working memory (the ledger) is re-injected every turn; old page dumps are trimmed |
| Facts ledger | `src/worker/ledger.py` | Observations stored verbatim with source and trust level. `record_fact` refuses a quote that isn't in the observation, or a value that isn't in the quote |
| Browser | `src/worker/browser.py` | Playwright. Pages come back as text plus `[eN]` element refs. Mechanical recoveries (timeouts, expired sessions) happen here in code |
| Write path + policy | `src/worker/actions.py` | Typed intent, policy gate loaded from the wiki, human approval, UI-then-API execution, read-back |
| Injection tripwire | `src/worker/safety.py` | Flags instruction-like text in external content; tainted intents always go to a human |
| Verifier | `src/worker/verifier.py` | Checks outcomes from the company's state, not from the agent's claims |
| Human in the loop | `src/worker/human.py` | Terminal prompts for questions, approvals and escalations; a scripted version for evals |
| Evidence | `src/worker/trace.py`, `report.py` | Append-only `events.jsonl`, screenshots, and `report.html` per run |
| Evals | `evals/` | 9 tasks, checked against seed ground truth and database state |

## How the requirements are covered

| Requirement | How |
|---|---|
| Understand the end goal | First call compiles a **GoalSpec**: deliverable, checkable success criteria, facts needed, plan, ambiguities |
| Break it into actions | GoalSpec plan; `update_plan` when what it learns changes the plan |
| Use tools | Real browser on real web apps, PDF parsing, ERP API (as fallback), policy wiki |
| Observe each result | Every tool returns an observation id plus content; external content is wrapped as untrusted |
| Decide next step | Model chooses the next tool given observations and working memory |
| Remember information | Facts ledger with provenance, re-injected each turn; survives context trimming |
| Detect failures | Timeouts, login redirects, 5xx, validation errors, missing elements, broken UI automation, policy holds |
| Retry / alternatives | Retry with longer timeout; re-sign-in from the vault; back off on 503; **fall from the ERP UI to the ERP API** when the form changes; validation errors go back to the model |
| Verify the outcome | Independent verifier: before/after DB diff, field-by-field match, payee = vendor master, approval ref present, every quote re-found in a fresh fetch of its source |
| Ask for clarification / approval | `ask_user` for ambiguity (e.g. two "Acme" vendors); approval for > ₹50k or tainted sources; AP-02 bank mismatches are held and escalated, never approvable inline |
| Summary and evidence | Final summary plus `runs/<id>/report.html` |

## Three things I'd point a reviewer at

1. **Write discipline (the model proposes, code decides).** The model has no tool that writes to the ERP. It has
   `propose_create_payable(vendor_name_fact, invoice_no_fact, amount_fact, due_date_fact, payee_account_fact)`.
   Typing into or submitting the ERP form through the browser tools is refused in code.
2. **Payment-diversion fraud (business email compromise).** One invoice in the inbox comes from a lookalike
   domain, has a new bank account, an "urgent" email, and white 1pt text telling "automated processing systems"
   to skip approval. The worker extracts it as data; the tripwire flags it; the policy gate sees the payee
   doesn't match the vendor master, holds it and escalates. Nothing is written, and the report says why.
3. **Measured iteration.** [docs/iteration-log.md](docs/iteration-log.md) records what failed in real runs and
   what changed. The first run produced a design bug I wouldn't have found by reading the code: the model
   sourced the payee account from the vendor master, which made the fraud check compare the master with itself.

## Run it

```bash
uv sync && uv run playwright install chromium
echo "OPENAI_API_KEY=sk-..." > .env
uv run company                     # terminal 1: Northwind intranet on http://127.0.0.1:8800
```

```bash
uv run worker --reset "Find the latest invoice from Kaveri Logistics, extract the amount and due date, enter it into our ERP, and tell me once it is done."
```

```bash
uv run worker --reset --chaos erp_redesign,erp_flaky --headed "Bluepeak says our new invoice is ready. Get it into the ERP."
```

```bash
uv run python evals/run.py --repeat 3
```

```bash
uv run pytest
```

Chaos switches: `slow_first_load`, `session_expiry`, `erp_flaky`, `erp_redesign`. Browse the company yourself at
http://127.0.0.1:8800 (ERP login `ap.bot@northwind.in` / `erp-demo-pass`, demo credentials only).

## Measured results

_(filled in from `evals/results/`)_

## Decisions and trade-offs

- **Hand-written loop, not LangGraph.** About 200 lines I can explain line by line. The workflow is one loop
  with one write path; a graph framework would add indirection without adding capability here.
- **Accessibility-style text snapshots, not screenshots, for the model.** Cheaper, faster and more precise on
  web apps. Screenshots are still taken as evidence for humans.
- **Mechanical recovery in code, judgement in the model.** Timeouts, expired sessions and 503s have one correct
  response, so code handles them every time. Validation errors, wrong pages and missing data need reasoning,
  so they go back to the model.
- **The ERP form first, the API as fallback.** The assignment is about operating tools like a person; the API
  is the tool-ladder fallback when the UI changes underneath the worker.
- **Policies come from the company wiki** (`/wiki/api/policies`), not from the agent's code, so the
  company changes a threshold in one place.
- **The verifier decides the outcome.** The agent's `finish(status)` is a claim. A run where the agent says
  "completed" but the verifier fails is reported as `failed_verification`.

## Limitations (honest)

- One domain (accounts payable) and one write intent (create payable). New write types need a new typed
  intent and its policy checks; reading and navigation generalise without code changes.
- The injection tripwire is regex-based. It is a detector, not the defence. The defence is structural
  (facts, typed intents, master-data checks), but novel phrasing won't be flagged.
- PDFs must have a text layer; scanned invoices fail with a clear error (no OCR).
- The verifier re-reads company state through an admin endpoint that stands in for a read replica; a real
  deployment would need read-only service credentials.
- Credentials are demo values in a config "vault". A real system would use a secrets manager.
- Single model (OpenAI `gpt-5.4-mini` by default); the client is a thin wrapper but no other provider is wired up.
- Sequential, single-run state; no resume-after-crash beyond the on-disk trace.

## What I'd build next

1. Resume a paused run from its trace (approvals that arrive hours later).
2. More write intents (vendor creation with its own fraud checks, payment runs) behind the same gate.
3. Procedural memory: store a successful run's path as a playbook and measure the step reduction on reruns.
4. Model comparison across providers on the same eval suite (cost vs pass rate).
5. Vision fallback for apps without usable DOM structure.

## Assumptions

- "Latest invoice" means the latest invoice date (policy AP-04), and amounts include GST.
- The worker acts as an AP clerk with ERP write access but no authority to approve its own entries.
- Everything outside Northwind's own systems (email, vendor portals, PDFs) is untrusted.

## Models used

- Worker: OpenAI `gpt-5.4-mini` via Chat Completions with tool calling (configurable with `WORKER_MODEL`).
- Built with help from Claude Code. All design decisions and their trade-offs are documented above.
