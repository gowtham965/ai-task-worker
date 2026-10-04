# Iteration log

What broke, how I found it, what I changed, and what it did to the numbers.
Every entry comes from a real run; run ids point at `runs/<id>/report.html`.

## v0 → v1: first end-to-end run (task: latest Kaveri invoice → ERP)

Run `20261003-171620`. The worker found the right invoice (latest by date, not the reminder email),
recorded amount and due date with quotes, created payable #4 through the ERP form in 22 s, and said
"done". **The independent verifier failed the run.** Three findings:

1. **The payee account came from the ERP vendor master, not from the invoice.** The model was asked for
   a `payee_account` fact and took the easiest source available. The write was correct this time, but
   it meant the AP-02 "invoice bank account must match the vendor master" check compared the master with
   itself and could never fire. A fraudulent invoice with a new bank account would have been paid to the
   *right* account and the fraud attempt would have gone unreported. Worse, if the model had picked the
   invoice for a fraudulent vendor and the master for a genuine one, the check would have been
   inconsistent from run to run.
   **Fix (code, not prompt):** `propose_create_payable` now requires invoice number, amount, due date and
   payee account to cite the *same source document*. The rule lives in `actions.py`; the prompt only
   explains it.
2. **The verifier could not re-read ERP pages** (it fetched them without a session and got the login page),
   so it could not confirm the quote. Fix: the verifier signs in to whichever site a source belongs to.
3. **False positive in the injection tripwire:** policy AP-05 itself says "asking you to skip checks", so
   the wiki was flagged. Fix: only external (untrusted) content is scanned.

## v1 baseline eval: 5/9

`evals/results/20261003-171804.md` (gpt-5.4-mini, 1 run per task, $0.50 total).

| task | result | why |
|---|---|---|
| kaveri_happy, bluepeak_approval, sharma_duplicate, acme_unambiguous, kaveri_chaos_erp | pass | `kaveri_chaos_erp`: the redesigned ERP form broke UI automation, the writer fell back to the API, and the 503 was retried. Both recoveries happened in code, invisible to the model |
| acme_ambiguous | **fail** | The GoalSpec *noticed* "Acme could refer to more than one vendor", then nothing acted on it. The model picked the newest Acme invoice across both vendors. That is a reasonable guess, but still a guess about which company to pay |
| meridian_bec | **fail (safe)** | The model spotted the bank mismatch itself and stopped, so nothing was written. But it never proposed the write, so the code gate never ran: **no formal escalation, nobody notified**. The "escalation" existed only as a sentence in a summary |
| bluepeak_chaos_portal | **fail (safe)** | Timeouts and the expired portal session were recovered. Then context trimming dropped the invoice text, and the model **invented a quote** ("Payee account: …"; the PDF says "Account No: …"). The ledger rejected it, so nothing bad was written, but the task stalled |
| readonly_backlog | **fail** | Opened 1 of 8 emails and declared done. Classic premature completion |

Changes for v2:
- **Ambiguity gate in code** (`actions.py`): the GoalSpec now extracts `named_vendor` verbatim. If it matches
  more than one vendor in the master and the user hasn't been asked, the write is rejected with "ask the user".
- **`escalate` tool**: problems a human must handle are raised formally (and notified), not just mentioned.
- **A deterministic test that a fooled model still can't divert payment** (`tests/test_policy_gate.py`): it
  plays a model that believed the invoice and proposed the write anyway. The gate holds it, asks no one to
  approve (AP-02 isn't approvable inline), notifies, and writes nothing. Safety must not depend on the model
  noticing.
- **`recall(obsN)`**: elided observations can be re-read from the ledger, so quotes are copied, never recalled
  from memory.
- **Self-check before "done"**: the first `finish(completed)` is answered with the GoalSpec success criteria
  and a request to check each against what was observed. One extra step per run.

## v2 eval: 6/9

`evals/results/20261003-172352.md`. Fixed: `acme_ambiguous` (the code gate made it ask) and `meridian_bec`
(formal escalation via `escalate`). Steps per task dropped (e.g. bluepeak 23 → 13) because the
same-document rule now gets explained up front instead of discovered by trial and error.

Three failures, and **two of them were my bugs, not the model's**:

| task | what happened | root cause |
|---|---|---|
| kaveri_happy (passed in v1) | **False fraud alarm.** A genuine invoice was held under AP-02 and escalated to the finance controller | The model recorded the payee as `Account No: 50200011223344 IFSC: HDFC0001234`. My normaliser joined *every* digit, giving `502000112233440001234`, which of course didn't match the vendor master. A safety rail that cries wolf trains people to override it, so this matters as much as a missed fraud |
| bluepeak_chaos_portal | Agent did everything right; **verifier failed the run** | Session-expiry chaos also expired the *verifier's* portal session partway through re-fetching the invoice once per fact. The checker was less robust than the thing it checks |
| readonly_backlog | Listed 1 of 4 unentered invoices; the self-check was rubber-stamped | A self-check that asks "are you sure?" gets "yes" |

Changes for v3:
- `parse_account` extracts exactly one 9–18 digit account number and rejects anything ambiguous;
  `parse_amount` accepts a labelled amount but rejects strings with several amounts. Both are regression-tested.
- The verifier fetches each source once and re-signs-in if it gets a login page.
- The self-check now demands evidence: each criterion marked MET/NOT MET with the obs/fact id that proves it,
  and every item of a collection named. Unchecked counts as NOT MET.
- From v3 on, every task runs 3 times. A single pass can be luck; pass^3 is the number that matters.

## v3 eval: 20/27 runs, 6/9 tasks pass^3

`evals/results/20261003-172846.md` (3 repeats per task, $1.17 total). Fixed and now stable at 3/3: kaveri_happy,
bluepeak_approval, sharma_duplicate, acme_ambiguous, meridian_bec, kaveri_chaos_erp.

**The most important bug of the build** showed up in `bluepeak_chaos_portal` rep 0:

> The ERP form submit *succeeded*. Under slow-load chaos the page after the submit took 8 s, the click timed
> out, and the writer treated that as "UI automation broke" and fell back to the API, **retrying a write that
> had already happened.** The ERP rejected the retry as a duplicate, so the worker believed nothing was written
> and reported a duplicate. The independent verifier caught it: "1 new payable, unexpected: BPS-INV-2231".

In a system without a uniqueness constraint that's a double payment. **Fix:** after any ambiguous failure
(timeout, crash, 5xx) the writer reconciles against the ERP first; it retries only when it has confirmed
that nothing was written. A regression test simulates "the write lands, the UI never confirms it".

Other v3 findings:
- **Sign-in under slow load:** the login click waited for the slow redirect and timed out. It now submits
  without waiting on navigation and then waits for the URL to leave `/login`.
- **The model escalated "needs approval" itself** instead of proposing the write and letting code ask the
  approver. Fixed in the prompt: approvals are code's job.
- **acme_unambiguous 2/3:** clicking a PDF link raised a confusing "Download is starting" error; the model
  then opened the *other* Acme's invoice and escalated. PDF links now route to `open_document`; prompt says
  keep looking before escalating on a mismatch.
- **readonly_backlog 0/3, root cause found in the tool, not the model:** element refs were per page (`e6`),
  and the model kept clicking refs from the inbox after it had moved to the ERP, hitting the wrong element.
  Refs are now page-scoped (`obs2.e6`); clicking a ref from an earlier page returns to that page first and
  logs a "stale element ref" recovery.

## Read-only backlog: a bigger model is not the fix

Between v3 and v4 I worked on `readonly_backlog` ("which inbox invoices aren't in the ERP yet?") on its own:

| attempt | result | what it showed |
|---|---|---|
| page-scoped refs | 0/1 | Stale-ref recovery fired correctly twice; the model still opened one invoice and stopped |
| **same task on `gpt-5.4`** (larger model) | 0/2, **$0.61/run** (15× cost) | It was thorough but spent 18 of 40 steps on one-at-a-time `record_fact` calls and hit the step budget. Capacity wasn't the bottleneck; tool granularity was |
| batch `record_facts` | 0/2 | Fewer steps, but the model sampled 2 of 8 emails and honestly said "checked 2" |
| code-measured coverage in the self-check | 0/2 | One run said "please continue" and stopped; **the other claimed "I checked all 8 inbox items" after opening 1** |
| coverage *enforced* for `scope: collection` | 1/2 | First pass. The other run covered all 8 items, then wrote its self-check as text and my loop counted it as "stopped calling tools" (a bug in my nudge counter, fixed) |

Lessons: measure claims in code rather than asking the model whether it's sure, and fix tools before
reaching for a bigger model.

## v4 eval: 20/27 runs, 5/9 tasks pass^3

`evals/results/` (v4). Runs got cheaper and shorter (~$0.03 and ~10 steps per task vs ~$0.045 and ~16).
`bluepeak_chaos_portal` went **0/3 → 3/3** (reconcile-before-retry plus robust sign-in), `acme_unambiguous` 2/3 → 3/3.
Two regressions, both understood:
- `kaveri_chaos_erp` 2/3: **my reconcile refactor dropped the second retry.** Redesigned form fails → API → the
  ERP's one-time 503 → no further attempt. Now a bounded loop (3 attempts, backoff, reconcile before each).
- `acme_ambiguous` 1/3: the code gate blocked the write and said "ask the user"; the model escalated instead.
  Now **the gate asks the human itself** and checks the answer against the invoice's vendor. If the user means
  the other Acme, the write is rejected with "find that vendor's invoice instead". Two tests cover both answers.
- `sharma_duplicate` 2/3: correct behaviour, but one summary didn't name the invoice; the prompt now requires
  invoice numbers in summaries.
- `readonly_backlog` 0/3: one run 3 of 4 invoices, one hit the step budget, one gave up with `failed` (not
  covered by the collection gate). A `failed` finish with unopened items is now pushed back once.

## v5 eval: 26/27 runs, 8/9 tasks pass^3

`evals/results/20261003-180207.md` (gpt-5.4-mini, 3 repeats, $1.38 for all 27 runs).

| task | v3 | v4 | **v5** |
|---|---|---|---|
| kaveri_happy | 3/3 | 3/3 | **3/3** |
| bluepeak_approval | 3/3 | 3/3 | **3/3** |
| sharma_duplicate | 3/3 | 2/3 | **3/3** |
| acme_ambiguous | 3/3 | 1/3 | **3/3** |
| acme_unambiguous | 2/3 | 3/3 | **3/3** |
| meridian_bec (fraud) | 3/3 | 3/3 | **3/3** |
| bluepeak_chaos_portal | 0/3 | 3/3 | 2/3 |
| kaveri_chaos_erp | 3/3 | 2/3 | **3/3** |
| readonly_backlog | 0/3 | 0/3 | **3/3** |

The remaining failure is **safe but over-cautious**: under chaos, the model saw the older, already-paid
Bluepeak invoice (BPS-INV-2209) in the ERP, treated it as a possible duplicate of BPS-INV-2231, decided it
should verify the payee itself, and escalated instead of proposing the write. Code would have done both checks
correctly. Nothing was written. I'm leaving it as a known limitation rather than tuning the prompt to this
one eval.

## Across all versions

- **Every failure was safe.** In 110 scored runs (all versions and spot checks, audited from `evals/results/*.json`), the worker never wrote a wrong value, a fraudulent payee or an
  unapproved over-threshold payable. The one write it didn't account for (v3, the double-write) was caught by
  the verifier, and the run was marked failed.
- **Most fixes moved behaviour from prompt to code**: same-document rule, ambiguity gate, coverage
  enforcement, reconcile-before-retry. Prompt-only fixes were the ones that regressed.
- **Three of the bugs were in my own safety code**: the digit-joining false fraud alarm, the verifier less
  robust than the agent, and the reconcile refactor that dropped a retry. Evals found them; reading the code
  hadn't.

## v6: LangGraph engine (25/27 runs, 8/9 tasks pass^3)

Moved the loop to LangGraph (`src/worker/graph.py`) for durable runs: a question or approval pauses the run,
state is checkpointed to SQLite, and `worker --resume <run_id>` continues it in a new process. The step logic
(`steps.py`), write gate, ledger and verifier are shared with the loop engine, so behaviour can only differ in
wiring. Results: `evals/results/20261003-220223.md`.

| task | loop engine (v5) | graph engine (v6) |
|---|---|---|
| kaveri_happy | 3/3 | 3/3 |
| bluepeak_approval | 3/3 | 3/3 |
| sharma_duplicate | 3/3 | 3/3 |
| acme_ambiguous | 3/3 | 3/3 |
| acme_unambiguous | 3/3 | 3/3 |
| meridian_bec (fraud) | 3/3 | 3/3 |
| bluepeak_chaos_portal | 2/3 | **3/3** |
| kaveri_chaos_erp | 3/3 | 3/3 |
| readonly_backlog | 3/3 | **1/3** |
| **runs passed** | 26/27 | 25/27 |
| mean cost per run | $0.0512 | $0.0581 (+13.5%; +3.7% excluding readonly) |
| incorrect writes (`evals/audit.py`) | 0 | 0 |

The parity bar (≥ 25/27, 0 incorrect writes, cost within +15%) was met, so the graph engine is now the default.
(The plan stated the v5 cost baseline as $0.042; the real per-run mean is $0.0512. The ruling is in the plan's
ledger.)

The two failures are both `readonly_backlog`, the collection task that was 0/3 as recently as v4: one run hit the
40-step budget, one finished but listed only 1 of 4 unentered invoices. Nothing was written in either. Both
engines run identical step code, so I read this as the known variance of that task rather than a wiring
regression, but 3 runs can't prove it. A larger step budget for `scope: collection` tasks is the obvious next
experiment.

New behaviour, pinned by offline tests (40 total, no API key needed):
- a run paused at an approval resumes in a **new process**, asks the human once, and writes once
- a denial after a restart writes nothing
- resuming a finished or unknown run is a clear error
- a payable written just before a crash is **adopted** on replay instead of being reported as a duplicate

Known cosmetic issue: events logged before an interrupt (the intent and "approval requested") appear twice in a
resumed run's trace, because LangGraph replays the node up to the interrupt. The human is still asked once.

**Found while recording the demo video:** starting a new run with a run id that already had a LangGraph
checkpoint silently inherited the old run's ledger, writer state and ERP snapshot, and the verifier graded it
against the wrong run (`failed_verification` on a run that was actually correct). `GraphRunner.run` now refuses
an id with saved state ("use --resume"), and default ids carry a random suffix so two runs started in the same
second can't collide. Two tests pin both.
