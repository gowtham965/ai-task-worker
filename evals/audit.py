"""Audit eval results for incorrect writes: wrong values, unexpected payables, writes in no-write tasks.

    uv run python evals/audit.py                       # all results
    uv run python evals/audit.py evals/results/X.json  # one run
"""

import glob
import json
import sys

files = sys.argv[1:] or sorted(glob.glob("evals/results/*.json"))
total, bad = 0, []
for f in files:
    for r in json.load(open(f))["rows"]:
        total += 1
        for name, ok, detail in r["checks"]:
            wrong_value = name.startswith("created") and detail != "missing"
            if not ok and (name in ("no unexpected writes", "no writes") or wrong_value):
                bad.append((f, r["task"], r["rep"], name, detail[:120]))
print(f"scored runs: {total}, incorrect writes: {len(bad)}")
for b in bad:
    print(b)
sys.exit(1 if bad else 0)
