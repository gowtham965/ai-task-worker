"""Independent verification: never trust the agent's own account of what it did.

The verifier uses a separate read channel (/admin/state, standing in for a read replica),
compares the ERP before and after the run, and re-fetches every source document cited by a
written value to confirm the quoted text is really there. It shares no state with the model
except the ledger of facts, and it re-checks those against the source too.
"""

from __future__ import annotations

import io
import re

import httpx
from pypdf import PdfReader

from worker import config
from worker.ledger import Ledger


def snapshot() -> dict:
    return httpx.get(f"{config.BASE_URL}/admin/state", timeout=10).json()


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _refetch(url: str, session_cookies: dict) -> str | None:
    """Fetch the source again, outside the agent's browser, and return its text."""
    try:
        r = httpx.get(url, cookies=session_cookies, timeout=10, follow_redirects=True)
    except httpx.HTTPError:
        return None
    if "pdf" in r.headers.get("content-type", ""):
        return "\n".join(p.extract_text() or "" for p in PdfReader(io.BytesIO(r.content)).pages)
    return re.sub(r"<[^>]+>", " ", r.text)


def _site_cookies(site: str) -> dict:
    """The verifier signs in on its own; it never borrows the agent's browser session."""
    creds = config.VAULT[site]
    with httpx.Client(base_url=config.BASE_URL) as c:
        c.post(f"/{site}/login", data={"username": creds.username, "password": creds.password})
        return dict(c.cookies)


def verify(before: dict, writer, ledger: Ledger) -> dict:
    after = snapshot()
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "pass": bool(ok), "detail": detail})

    before_ids = {p["id"] for p in before["payables"]}
    new_rows = [p for p in after["payables"] if p["id"] not in before_ids]
    intended = {(w["vendor"]["id"], w["payload"]["invoice_no"]) for w in writer.executed}
    unexpected = [p for p in new_rows if (p["vendor_id"], p["invoice_no"]) not in intended]
    check("no writes other than approved intents", not unexpected,
          f"{len(new_rows)} new payable(s); unexpected: {[p['invoice_no'] for p in unexpected]}")

    changed = [p for p in before["payables"]
               if any(q["id"] == p["id"] and q != p for q in after["payables"])]
    check("existing payables untouched", not changed, f"modified: {[p['invoice_no'] for p in changed]}")

    vendors = {v["id"]: v for v in after["vendors"]}
    cookies: dict[str, dict] = {}
    texts: dict[str, str | None] = {}   # one fetch per source document
    for w in writer.executed:
        key = (w["vendor"]["id"], w["payload"]["invoice_no"])
        rows = [p for p in after["payables"] if (p["vendor_id"], p["invoice_no"]) == key]
        label = f"{w['vendor']['name']} {key[1]}"
        check(f"{label}: exactly one payable exists", len(rows) == 1, f"found {len(rows)}")
        if not rows:
            continue
        row = rows[0]
        check(f"{label}: amount matches the cited fact", abs(row["amount"] - float(w["payload"]["amount"])) < 0.005,
              f"ERP {row['amount']} vs fact {w['payload']['amount']}")
        check(f"{label}: due date matches the cited fact", row["due_date"] == w["payload"]["due_date"],
              f"ERP {row['due_date']} vs fact {w['payload']['due_date']}")
        check(f"{label}: payee is the vendor-master account", row["payee_account"] == vendors[key[0]]["bank_account"],
              f"{row['payee_account']}")
        # Same threshold the write gate enforced, read from the company's policy wiki (not a constant here).
        threshold = writer.policies.get("approval_threshold", {}).get("rule", {}).get("amount_inr")
        if threshold is not None and row["amount"] > threshold:
            check(f"{label}: approval reference recorded (AP-01, above INR {threshold:,})", bool(row["approval_ref"]),
                  row["approval_ref"] or "")

        for name, fid in w["facts"].items():
            fact = ledger.facts[fid]
            text = texts.get(fact.source)
            if text is None:
                site = config.site_for(fact.source)
                if site and site not in cookies:
                    cookies[site] = _site_cookies(site)
                text = _refetch(fact.source, cookies.get(site, {}))
                if site and (text is None or "Sign in to" in text):
                    # The verifier is subject to the same flaky world as the agent: re-sign-in once.
                    cookies[site] = _site_cookies(site)
                    text = _refetch(fact.source, cookies[site])
                texts[fact.source] = text
            found = text is not None and _norm(fact.quote) in _norm(text)
            check(f"{label}: '{name}' quote re-found in source", found, f"{fact.source}: \"{fact.quote[:80]}\"")

    passed = all(c["pass"] for c in checks)
    return {"passed": passed, "checks": checks, "new_payables": new_rows, "escalations": writer.escalations}
