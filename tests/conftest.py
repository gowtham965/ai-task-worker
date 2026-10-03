import os
import tempfile

# Tests get their own company database so they never touch a running demo or eval.
os.environ.setdefault("COMPANY_DATA_DIR", tempfile.mkdtemp(prefix="northwind-test-"))
