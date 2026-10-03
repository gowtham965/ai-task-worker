import dataclasses
import os
import tempfile
import threading
import time

import pytest

# Tests get their own company database so they never touch a running demo or eval.
os.environ.setdefault("COMPANY_DATA_DIR", tempfile.mkdtemp(prefix="northwind-test-"))

PORT = 8811


@pytest.fixture(scope="session")
def company_url():
    """The Northwind intranet, in-process on a spare port. Points config (and the vault) at it."""
    import uvicorn

    from company import seed
    from company.server import app
    from worker import config

    seed.reset()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{PORT}"
    old_base, old_vault = config.BASE_URL, dict(config.VAULT)
    config.BASE_URL = base
    for site, creds in old_vault.items():   # login URLs were built from the old BASE_URL at import time
        config.VAULT[site] = dataclasses.replace(creds, login_url=f"{base}/{site}/login")
    yield base
    config.BASE_URL = old_base
    config.VAULT.clear()
    config.VAULT.update(old_vault)
    server.should_exit = True


@pytest.fixture
def fresh_company(company_url):
    """Reset data, sessions and chaos before a test (through the server, so its in-memory state resets too)."""
    import httpx

    httpx.post(f"{company_url}/admin/reset", json={"chaos": {}}, timeout=10).raise_for_status()
    return company_url
