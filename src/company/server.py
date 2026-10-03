"""Northwind Ops intranet: the simulated company the worker operates.

One FastAPI process serves four "tools" a real AP clerk would juggle:
  /mail    shared accounts-payable inbox (invoices arrive as PDF attachments)
  /portal  Bluepeak's vendor billing portal (separate login, invoices not emailed)
  /erp     internal ERP: vendor master, payables, a validated entry form, and a JSON API
  /wiki    company policies (also exposed as JSON for the worker's policy gate)
plus /admin, a read-only state dump used by the verifier and evals, and chaos switches.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path

import uvicorn
import yaml
from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from company import seed

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["inr"] = seed.inr
POLICIES = yaml.safe_load((HERE / "policies.yaml").read_text())["policies"]

app = FastAPI(title="Northwind Ops intranet")
SESSIONS: dict[str, dict] = {}       # token -> {"site": "erp"|"portal", "user": str, "views": int}
_slowed_paths: set[str] = set()
_erp_failures = {"done": False}


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(seed.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def chaos() -> dict:
    return json.loads(seed.CHAOS_PATH.read_text())


def page(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


@app.middleware("http")
async def chaos_slow(request: Request, call_next):
    path = request.url.path
    if request.method == "GET" and not path.startswith(("/admin", "/erp/api", "/wiki/api")):
        if chaos().get("slow_first_load") and path not in _slowed_paths:
            _slowed_paths.add(path)
            await asyncio.sleep(8)
    return await call_next(request)


# ---------------------------------------------------------------- sessions

def _session(request: Request, site: str) -> dict | None:
    s = SESSIONS.get(request.cookies.get(f"{site}_session", ""))
    return s if s and s["site"] == site else None


def _login(site: str, username: str, password: str, next_url: str) -> RedirectResponse | None:
    if (username, password) != seed.USERS[site]:
        return None
    token = secrets.token_hex(8)
    SESSIONS[token] = {"site": site, "user": username, "views": 0}
    resp = RedirectResponse(next_url, status_code=303)
    resp.set_cookie(f"{site}_session", token)
    return resp


# ---------------------------------------------------------------- home

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return page(request, "home.html")


# ---------------------------------------------------------------- mail

@app.get("/mail/", response_class=HTMLResponse)
def inbox(request: Request):
    emails = db().execute("SELECT * FROM emails ORDER BY received_at DESC").fetchall()
    return page(request, "inbox.html", emails=emails)


@app.get("/mail/{email_id}", response_class=HTMLResponse)
def read_email(request: Request, email_id: int):
    email = db().execute("SELECT * FROM emails WHERE id=?", (email_id,)).fetchone()
    if not email:
        raise HTTPException(404, "No such email")
    return page(request, "email.html", email=email)


@app.get("/files/{name}")
def attachment(name: str):
    attached = {r[0] for r in db().execute("SELECT attachment FROM emails WHERE attachment IS NOT NULL")}
    if name not in attached:
        raise HTTPException(404, "No such attachment")
    return FileResponse(seed.FILES_DIR / name, media_type="application/pdf")


# ---------------------------------------------------------------- vendor portal (Bluepeak)

@app.get("/portal/login", response_class=HTMLResponse)
def portal_login_form(request: Request, msg: str = ""):
    return page(request, "login.html", site="Bluepeak Billing Portal", action="/portal/login", msg=msg)


@app.post("/portal/login")
def portal_login(username: str = Form(...), password: str = Form(...)):
    resp = _login("portal", username, password, "/portal/")
    return resp or RedirectResponse("/portal/login?msg=Invalid+credentials", status_code=303)


def _portal_guard(request: Request) -> RedirectResponse | None:
    s = _session(request, "portal")
    if not s:
        return RedirectResponse("/portal/login?msg=Please+sign+in", status_code=303)
    s["views"] += 1
    if chaos().get("session_expiry") and s["views"] > 2:
        SESSIONS.pop(request.cookies.get("portal_session"), None)
        return RedirectResponse("/portal/login?msg=Your+session+has+expired.+Please+sign+in+again.", status_code=303)
    return None


@app.get("/portal/", response_class=HTMLResponse)
def portal_home(request: Request):
    if redirect := _portal_guard(request):
        return redirect
    invoices = db().execute("SELECT * FROM portal_invoices ORDER BY issue_date DESC").fetchall()
    return page(request, "portal.html", invoices=invoices)


@app.get("/portal/invoices/{invoice_no}", response_class=HTMLResponse)
def portal_invoice(request: Request, invoice_no: str):
    if redirect := _portal_guard(request):
        return redirect
    inv = db().execute("SELECT * FROM portal_invoices WHERE invoice_no=?", (invoice_no,)).fetchone()
    if not inv:
        raise HTTPException(404, "No such invoice")
    return page(request, "portal_invoice.html", inv=inv)


@app.get("/portal/download/{name}")
def portal_download(request: Request, name: str):
    if redirect := _portal_guard(request):
        return redirect
    if not db().execute("SELECT 1 FROM portal_invoices WHERE pdf=?", (name,)).fetchone():
        raise HTTPException(404, "No such file")
    return FileResponse(seed.FILES_DIR / name, media_type="application/pdf")


# ---------------------------------------------------------------- ERP

@app.get("/erp/login", response_class=HTMLResponse)
def erp_login_form(request: Request, msg: str = ""):
    return page(request, "login.html", site="Northwind ERP", action="/erp/login", msg=msg)


@app.post("/erp/login")
def erp_login(username: str = Form(...), password: str = Form(...)):
    resp = _login("erp", username, password, "/erp/")
    return resp or RedirectResponse("/erp/login?msg=Invalid+credentials", status_code=303)


def _erp_user(request: Request) -> str | None:
    s = _session(request, "erp")
    return s["user"] if s else None


def _erp_redirect():
    return RedirectResponse("/erp/login?msg=Please+sign+in", status_code=303)


@app.get("/erp/", response_class=HTMLResponse)
def erp_home(request: Request):
    if not _erp_user(request):
        return _erp_redirect()
    return page(request, "erp_home.html")


@app.get("/erp/vendors", response_class=HTMLResponse)
def erp_vendors(request: Request):
    if not _erp_user(request):
        return _erp_redirect()
    return page(request, "erp_vendors.html", vendors=db().execute("SELECT * FROM vendors ORDER BY name").fetchall())


def _payables_query():
    return db().execute(
        "SELECT p.*, v.name AS vendor FROM payables p JOIN vendors v ON v.id=p.vendor_id ORDER BY p.id"
    ).fetchall()


@app.get("/erp/payables", response_class=HTMLResponse)
def erp_payables(request: Request, created: int | None = None):
    if not _erp_user(request):
        return _erp_redirect()
    return page(request, "erp_payables.html", payables=_payables_query(), created=created)


@app.get("/erp/payables/new", response_class=HTMLResponse)
def erp_new_payable_form(request: Request):
    if not _erp_user(request):
        return _erp_redirect()
    vendors = db().execute("SELECT id, name FROM vendors ORDER BY name").fetchall()
    return page(request, "erp_new_payable.html", vendors=vendors, errors=[], form={},
                redesign=chaos().get("erp_redesign"))


def _create_payable(fields: dict, actor: str, via: str) -> tuple[int, dict]:
    """Shared by the form and the API. Returns (http_status, body)."""
    errors = []
    conn = db()
    try:
        vendor_id = int(fields.get("vendor_id") or 0)
    except ValueError:
        vendor_id = 0
    if not conn.execute("SELECT 1 FROM vendors WHERE id=?", (vendor_id,)).fetchone():
        errors.append("Select a valid vendor.")
    invoice_no = str(fields.get("invoice_no") or "").strip()
    if not invoice_no:
        errors.append("Invoice number is required.")
    raw_amount = str(fields.get("amount") or "").strip()
    amount = None
    if not re.fullmatch(r"\d+(\.\d{1,2})?", raw_amount):
        errors.append("Amount must be a plain number with up to 2 decimals, no commas or currency symbols.")
    else:
        amount = float(raw_amount)
    due = str(fields.get("due_date") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due):
        errors.append("Due date must be in YYYY-MM-DD format.")
    payee = str(fields.get("payee_account") or "").strip()
    if not payee.isdigit():
        errors.append("Payee account must contain digits only.")
    approval_ref = str(fields.get("approval_ref") or "").strip()
    if amount is not None and amount > 50000 and not approval_ref:
        errors.append("Payables above INR 50,000 need an approval reference (policy AP-01).")
    if errors:
        return 422, {"ok": False, "errors": errors}

    dup = conn.execute("SELECT id FROM payables WHERE vendor_id=? AND invoice_no=?", (vendor_id, invoice_no)).fetchone()
    if dup:
        return 409, {"ok": False, "errors": [f"Duplicate: payable #{dup['id']} already exists for this invoice."]}

    if chaos().get("erp_flaky") and not _erp_failures["done"]:
        _erp_failures["done"] = True
        return 503, {"ok": False, "errors": ["Service temporarily unavailable. Please retry."]}

    cur = conn.execute(
        "INSERT INTO payables (vendor_id, invoice_no, amount, due_date, payee_account, status, created_by, created_via,"
        " created_at, approval_ref) VALUES (?,?,?,?,?,'open',?,?,?,?)",
        (vendor_id, invoice_no, amount, due, payee, actor, via, datetime.now().isoformat(timespec="seconds"),
         approval_ref or None),
    )
    conn.execute("INSERT INTO erp_audit (actor, action, detail) VALUES (?,?,?)",
                 (actor, "create_payable", json.dumps({**fields, "id": cur.lastrowid, "via": via})))
    conn.commit()
    return 201, {"ok": True, "id": cur.lastrowid}


@app.post("/erp/payables/new", response_class=HTMLResponse)
async def erp_new_payable(request: Request):
    user = _erp_user(request)
    if not user:
        return _erp_redirect()
    form = dict(await request.form())
    status, body = _create_payable(form, user, "ui")
    if status == 201:
        return RedirectResponse(f"/erp/payables?created={body['id']}", status_code=303)
    vendors = db().execute("SELECT id, name FROM vendors ORDER BY name").fetchall()
    return templates.TemplateResponse(
        request, "erp_new_payable.html",
        {"vendors": vendors, "errors": body["errors"], "form": form, "redesign": chaos().get("erp_redesign")},
        status_code=status,
    )


def _check_token(token: str | None):
    if token != f"Bearer {seed.ERP_API_TOKEN}":
        raise HTTPException(401, "Invalid API token")


@app.get("/erp/api/vendors")
def api_vendors(authorization: str | None = Header(None)):
    _check_token(authorization)
    return [dict(r) for r in db().execute("SELECT * FROM vendors")]


@app.get("/erp/api/payables")
def api_payables(authorization: str | None = Header(None), vendor_id: int | None = None, invoice_no: str | None = None):
    _check_token(authorization)
    rows = [dict(r) for r in _payables_query()]
    return [r for r in rows if (vendor_id is None or r["vendor_id"] == vendor_id)
            and (invoice_no is None or r["invoice_no"] == invoice_no)]


@app.post("/erp/api/payables")
async def api_create_payable(request: Request, authorization: str | None = Header(None)):
    _check_token(authorization)
    status, body = _create_payable(await request.json(), "api-client", "api")
    return JSONResponse(body, status_code=status)


# ---------------------------------------------------------------- wiki

@app.get("/wiki/", response_class=HTMLResponse)
def wiki(request: Request):
    return page(request, "wiki.html", policies=POLICIES)


@app.get("/wiki/api/policies")
def wiki_policies():
    return POLICIES


# ---------------------------------------------------------------- admin (verifier / eval channel)

@app.get("/admin/state")
def admin_state():
    conn = db()
    return {
        "payables": [dict(r) for r in _payables_query()],
        "vendors": [dict(r) for r in conn.execute("SELECT * FROM vendors")],
        "audit": [dict(r) for r in conn.execute("SELECT * FROM erp_audit ORDER BY id")],
        "chaos": chaos(),
    }


@app.post("/admin/reset")
async def admin_reset(request: Request):
    body = await request.json() if (await request.body()) else {}
    seed.reset(chaos=body.get("chaos"))
    SESSIONS.clear()
    _slowed_paths.clear()
    _erp_failures["done"] = False
    return {"ok": True, "chaos": chaos()}


def main():
    if not seed.DB_PATH.exists():
        seed.reset()
    uvicorn.run(app, host="127.0.0.1", port=8800, log_level="warning")


if __name__ == "__main__":
    main()
