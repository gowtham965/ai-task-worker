import json

from worker.ledger import Ledger

INVOICE = "TAX INVOICE\nInvoice No KL/2026/0934\nTotal Amount Payable (INR) 23,780.00\n" + "x" * 5000


def test_ledger_round_trip_keeps_full_text_and_ids():
    ledger = Ledger()
    obs = ledger.observe("http://x/inv.pdf", "document", INVOICE, trusted=False, flags=["skip approval"])
    ledger.record("amount", "23780.00", obs.id, "Total Amount Payable (INR) 23,780.00")
    restored = Ledger.from_state(json.loads(json.dumps(ledger.to_state())))
    assert restored.observations["obs1"].text == INVOICE          # to_dict() truncates; to_state() must not
    assert restored.observations["obs1"].flags == ["skip approval"]
    assert restored.get("fact1").value == "23780.00"
    nxt = restored.observe("http://x/2", "page", "p", trusted=True)
    assert nxt.id == "obs2"                                         # numbering continues after restore


class _FakeBrowser:
    pass


def test_writer_state_round_trip(monkeypatch):
    from worker import actions
    monkeypatch.setattr(actions, "load_policies", lambda: {})
    w = actions.PayableWriter(Ledger(), trace=None, human=None, browser=_FakeBrowser())
    w.executed.append({"intent": "create_payable", "payload": {"invoice_no": "X"}, "vendor": {"id": 1},
                       "facts": {"amount": "fact3"}, "record": {"id": 4}})
    w.escalations.append({"policy": "AP-02"})
    w.clarifications.append({"question": "Which Acme?", "answer": "A"})
    w.named_vendor, w._approvals = "Acme", 2
    w2 = actions.PayableWriter(Ledger(), trace=None, human=None, browser=_FakeBrowser())
    w2.restore(json.loads(json.dumps(w.state())))
    assert w2.state() == w.state()
    assert w2.preexisting_ids is None


def test_browser_coverage_round_trip():
    from worker.browser import Browser
    b = Browser.__new__(Browser)                # no Playwright needed for this state
    b.listings, b.visited = {"http://h/mail/": ["http://h/mail/1", "http://h/mail/2"]}, {"http://h/mail/1"}
    state = json.loads(json.dumps(Browser.coverage_state(b)))
    b2 = Browser.__new__(Browser)
    Browser.restore_coverage(b2, state)
    assert b2.listings == b.listings and b2.visited == b.visited
    assert len(Browser.coverage(b2)) == 1 and "/mail/2" in Browser.coverage(b2)[0]


def test_trace_resume_does_not_log_task_again(tmp_path, monkeypatch):
    from worker.trace import Trace
    monkeypatch.chdir(tmp_path)
    Trace("do x", "r1", quiet=True).close()
    t = Trace("do x", "r1", quiet=True, resume=True)
    t.close()
    lines = (tmp_path / "runs" / "r1" / "events.jsonl").read_text().splitlines()
    assert sum('"kind": "task"' in line for line in lines) == 1
