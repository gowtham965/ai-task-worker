"""Runtime settings. Credentials live here (the "vault"), never in the model's context."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

# Minimal .env loader (KEY=VALUE lines) so `uv run worker` works without extra tooling.
if Path(".env").exists():
    for line in Path(".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"'))

BASE_URL = os.environ.get("WORKER_BASE_URL", "http://127.0.0.1:8800")
MODEL = os.environ.get("WORKER_MODEL", "gpt-5.4-mini")
MAX_STEPS = int(os.environ.get("WORKER_MAX_STEPS", "40"))
ENGINE = os.environ.get("WORKER_ENGINE", "loop")   # "loop" (agent.py) or "graph" (LangGraph, durable)
PAGE_TIMEOUT_MS = 5000

# Content from these path prefixes comes from Northwind's own systems. Everything else
# (mail, vendor portal, attachments) is external and treated as untrusted data.
TRUSTED_PREFIXES = ("/erp", "/wiki")


@dataclass(frozen=True)
class SiteLogin:
    login_url: str
    username: str
    password: str = field(repr=False)


VAULT = {
    "erp": SiteLogin(f"{BASE_URL}/erp/login", os.environ.get("ERP_USER", "ap.bot@northwind.in"),
                     os.environ.get("ERP_PASSWORD", "erp-demo-pass")),
    "portal": SiteLogin(f"{BASE_URL}/portal/login", os.environ.get("PORTAL_USER", "ap@northwind.in"),
                        os.environ.get("PORTAL_PASSWORD", "portal-demo-pass")),
}
ERP_API_TOKEN = os.environ.get("ERP_API_TOKEN", "erp-demo-token")


def is_trusted(url: str) -> bool:
    return urlparse(url).path.startswith(TRUSTED_PREFIXES)


def site_for(url: str) -> str | None:
    path = urlparse(url).path
    return "erp" if path.startswith("/erp") else "portal" if path.startswith("/portal") else None


def allowed(url: str) -> bool:
    """Navigation allowlist: the worker may only browse the company intranet."""
    return urlparse(url).netloc == urlparse(BASE_URL).netloc
