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
