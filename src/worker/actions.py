"""Write path: the model proposes, code disposes.

The model never types into the ERP. It proposes a typed intent whose fields are *fact ids*
from the ledger. This module then, in plain code:

  1. resolves every fact (each one is a verbatim quote from a page or document)
  2. normalises values (amount, ISO date) and resolves the vendor against the ERP vendor master
  3. runs the policy gate, with rules loaded from the company wiki:
       AP-03 duplicate          -> no write; tell the model
       AP-02 payee != master    -> hold and escalate; no inline approval possible
       AP-01 above threshold    -> human approval, with the evidence attached
       tainted source           -> human approval even below the threshold
  4. executes on the tool ladder: ERP web form first, ERP API if the UI breaks,
     one retry with backoff on 5xx
  5. reads the record back and returns what was actually stored
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime

import httpx
from playwright.sync_api import Error as PlaywrightError

from worker import config
from worker.ledger import Fact, Ledger, ProvenanceError
from worker.trace import Trace


class IntentRejected(Exception):
    """The intent is malformed or ambiguous. The model can fix it and propose again."""


@dataclass
class Outcome:
    status: str                   # created | duplicate | held | denied | failed
    message: str
    payable_id: int | None = None
    record: dict | None = None
    details: dict = field(default_factory=dict)


def _api() -> httpx.Client:
    return httpx.Client(base_url=config.BASE_URL, timeout=10,
                        headers={"Authorization": f"Bearer {config.ERP_API_TOKEN}"})


def load_policies() -> dict[str, dict]:
    with _api() as c:
        return {p["rule"]["kind"]: p for p in c.get("/wiki/api/policies").json()}


def parse_amount(value: str) -> float:
    cleaned = re.sub(r"[\s,₹]|INR|Rs\.?", "", value, flags=re.IGNORECASE)
    if not re.fullmatch(r"\d+(\.\d{1,2})?", cleaned):
        raise IntentRejected(f"Amount fact '{value}' is not a plain amount.")
    return float(cleaned)


def parse_date(value: str) -> str:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise IntentRejected(f"Due date fact '{value}' is not a recognisable date.")


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower())


def resolve_vendor(name: str, vendors: list[dict]) -> dict:
    exact = [v for v in vendors if _norm_name(v["name"]) == _norm_name(name)]
    if len(exact) == 1:
        return exact[0]
    partial = [v for v in vendors if _norm_name(name) in _norm_name(v["name"])
               or _norm_name(v["name"]) in _norm_name(name)]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise IntentRejected(f"No vendor in the ERP vendor master matches '{name}'.")
    raise IntentRejected(f"'{name}' matches several vendors: {[v['name'] for v in partial]}. "
                         "Ask the user which one is meant.")


class PayableWriter:
    def __init__(self, ledger: Ledger, trace: Trace, human, browser) -> None:
        self.ledger, self.trace, self.human, self.browser = ledger, trace, human, browser
        self.policies = load_policies()
        self.executed: list[dict] = []     # what the verifier will check
        self.escalations: list[dict] = []
        self._approvals = 0

    # ------------------------------------------------------------ propose

    def propose(self, vendor_name_fact: str, invoice_no_fact: str, amount_fact: str, due_date_fact: str,
                payee_account_fact: str) -> Outcome:
        facts: dict[str, Fact] = {}
        try:
            for name, fid in [("vendor_name", vendor_name_fact), ("invoice_no", invoice_no_fact),
                              ("amount", amount_fact), ("due_date", due_date_fact),
                              ("payee_account", payee_account_fact)]:
                facts[name] = self.ledger.get(fid)
        except ProvenanceError as e:
            raise IntentRejected(str(e)) from e

        # All invoice fields must come from one source document. Otherwise "payee account" can be
        # lifted from the vendor master and the AP-02 bank-mismatch check compares the master with itself.
        invoice_fields = ("invoice_no", "amount", "due_date", "payee_account")
        sources = {facts[k].source for k in invoice_fields}
        if len(sources) > 1:
            raise IntentRejected(
                "invoice_no, amount, due_date and payee_account must all be quoted from the same invoice document "
                f"(got {sorted(sources)}). Record the payee account exactly as printed on the invoice; the vendor "
                "master comparison is done by code.")

        with _api() as c:
            vendors = c.get("/erp/api/vendors").json()
        vendor = resolve_vendor(facts["vendor_name"].value, vendors)
        fields = {
            "vendor_id": vendor["id"],
            "invoice_no": facts["invoice_no"].value.strip(),
            "amount": parse_amount(facts["amount"].value),
            "due_date": parse_date(facts["due_date"].value),
            "payee_account": re.sub(r"\D", "", facts["payee_account"].value),
        }
        evidence = [f"{k} = {f.value!r} quoted from {f.source}: \"{f.quote}\"" for k, f in facts.items()]
        tainted = sorted({flag for f in facts.values() for flag in self.ledger.observations[f.observation_id].flags})
        self.trace.log("intent", intent="create_payable", vendor=vendor["name"], fields=fields,
                       facts={k: f.id for k, f in facts.items()}, tainted=tainted)

        # AP-03: duplicates
        with _api() as c:
            existing = c.get("/erp/api/payables", params={"vendor_id": vendor["id"],
                                                         "invoice_no": fields["invoice_no"]}).json()
        if existing and "no_duplicates" in self.policies:
            rec = existing[0]
            self.trace.log("policy", policy="AP-03", decision="no write: duplicate", existing=rec)
            return Outcome("duplicate", f"Payable #{rec['id']} already exists for {vendor['name']} "
                           f"{fields['invoice_no']} (amount {rec['amount']}, status {rec['status']}). Nothing written.",
                           rec["id"], rec)

        # AP-02: payee must match the vendor master
        if "payee_must_match_master" in self.policies and fields["payee_account"] != vendor["bank_account"]:
            pol = self.policies["payee_must_match_master"]
            detail = {"vendor": vendor["name"], "invoice_no": fields["invoice_no"], "amount": fields["amount"],
                      "account_on_invoice": fields["payee_account"], "account_in_master": vendor["bank_account"],
                      "suspicious_text": tainted, "source": facts["payee_account"].source}
            self.trace.log("policy", policy=pol["id"], decision="HOLD and escalate: payee account mismatch", **detail)
            self.escalations.append({"policy": pol["id"], **detail})
            self.human.notify(
                f"{pol['id']}: invoice {fields['invoice_no']} from {vendor['name']} asks for payment to account "
                f"{fields['payee_account']}, but the vendor master has {vendor['bank_account']}. Possible payment "
                f"diversion. Nothing was entered. Escalate to the {pol['rule']['escalate_to']} for phone verification."
                + (f" Suspicious text in the document: {tainted}" if tainted else ""))
            return Outcome("held", f"Policy {pol['id']}: payee account {fields['payee_account']} does not match the "
                           f"vendor master ({vendor['bank_account']}). Invoice held, nothing written, escalated to the "
                           f"{pol['rule']['escalate_to']}. This cannot be approved inline; do not retry.",
                           details=detail)

        # AP-01: approval threshold (and tainted sources always get a human)
        approval_ref = ""
        threshold = self.policies.get("approval_threshold")
        over = threshold and fields["amount"] > threshold["rule"]["amount_inr"]
        if over or tainted:
            reasons = []
            if over:
                reasons.append(f"{threshold['id']}: amount INR {fields['amount']:,.2f} exceeds "
                               f"INR {threshold['rule']['amount_inr']:,}")
            if tainted:
                reasons.append(f"source document contains instruction-like text: {tainted}")
            request = {"policy": threshold["id"] if over else "AP-05", "summary": "; ".join(reasons),
                       "fields": {**fields, "vendor": vendor["name"]}, "evidence": evidence,
                       "warnings": tainted}
            self.trace.log("approval", requested=request)
            ok, who = self.human.approve(request)
            self.trace.log("approval", granted=ok, by=who)
            if not ok:
                return Outcome("denied", "The approver declined. Nothing was written.")
            self._approvals += 1
            approval_ref = f"APR-{self.trace.run_id}-{self._approvals}"

        payload = {**fields, "amount": f"{fields['amount']:.2f}", "approval_ref": approval_ref}
        return self._execute(payload, vendor, facts)

    # ------------------------------------------------------------ execute

    def _execute(self, payload: dict, vendor: dict, facts: dict[str, Fact]) -> Outcome:
        try:
            status, info = self._via_ui(payload, vendor)
        except (PlaywrightError, LookupError) as e:
            self.trace.log("recovery", failure="ERP form automation broke", error=str(e).splitlines()[0][:200],
                           strategy="fall down the tool ladder: UI -> ERP API")
            status, info = self._via_api(payload)

        if status == 503:
            self.trace.log("recovery", failure="ERP returned 503", strategy="back off 2s, retry once via API")
            time.sleep(2)
            status, info = self._via_api(payload)

        if status == 409:
            return Outcome("duplicate", f"ERP reported a duplicate: {info}")
        if status not in (200, 201):
            return Outcome("failed", f"ERP rejected the payable (HTTP {status}): {info}")

        with _api() as c:
            stored = c.get("/erp/api/payables", params={"vendor_id": vendor["id"],
                                                       "invoice_no": payload["invoice_no"]}).json()
        record = stored[0] if stored else None
        self.executed.append({"intent": "create_payable", "payload": payload, "vendor": vendor,
                              "facts": {k: f.id for k, f in facts.items()}, "record": record})
        self.trace.log("tool", action="payable created", payable=record, channel=info.get("via") if isinstance(info, dict) else None)
        return Outcome("created", f"Payable #{record['id']} created and read back from the ERP.", record["id"], record)

    def _via_ui(self, payload: dict, vendor: dict) -> tuple[int, dict | str]:
        page = self.browser.context.new_page()
        page.set_default_timeout(3000)
        try:
            page.goto(f"{config.BASE_URL}/erp/payables/new", timeout=config.PAGE_TIMEOUT_MS * 3)
            if page.url.endswith("/login") or "/login?" in page.url:
                self.browser.sign_in("erp")
                page.goto(f"{config.BASE_URL}/erp/payables/new", timeout=config.PAGE_TIMEOUT_MS * 3)
            page.get_by_label("Vendor", exact=True).select_option(str(payload["vendor_id"]))
            page.get_by_label("Invoice number", exact=True).fill(payload["invoice_no"])
            page.get_by_label("Amount (INR)", exact=True).fill(payload["amount"])
            page.get_by_label("Due date", exact=True).fill(payload["due_date"])
            page.get_by_label("Payee account", exact=True).fill(payload["payee_account"])
            page.get_by_label("Approval reference", exact=False).fill(payload["approval_ref"])
            path = self.trace.shot_path("erp-form-filled")
            page.screenshot(path=path, full_page=True)
            page.get_by_role("button", name="Create payable").click()
            page.wait_for_load_state()
            if m := re.search(r"created=(\d+)", page.url):
                page.screenshot(path=self.trace.shot_path("erp-created"), full_page=True)
                return 201, {"id": int(m.group(1)), "via": "ui"}
            errors = page.locator(".err").all_inner_texts()
            page.screenshot(path=self.trace.shot_path("erp-rejected"), full_page=True)
            if any("temporarily unavailable" in e for e in errors):
                return 503, "; ".join(errors)
            if any("Duplicate" in e for e in errors):
                return 409, "; ".join(errors)
            return 422, "; ".join(errors) or f"unexpected page {page.url}"
        finally:
            page.close()

    def _via_api(self, payload: dict) -> tuple[int, dict | str]:
        with _api() as c:
            r = c.post("/erp/api/payables", json=payload)
        body = r.json()
        return r.status_code, ({**body, "via": "api"} if body.get("ok") else "; ".join(body.get("errors", [])))
