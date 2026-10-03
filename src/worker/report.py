"""Render runs/<id>/report.html: the evidence a reviewer reads to trust (or not) a run."""

from __future__ import annotations

import html
import json
from pathlib import Path

BADGE = {"completed_verified": "#1e7d3a", "completed_no_write": "#1e7d3a", "escalated_safely": "#a15c00",
         "failed_verification": "#b00020", "failed": "#b00020"}


def _e(x) -> str:
    return html.escape(str(x))


def render(run_dir: Path) -> Path:
    result = json.loads((run_dir / "result.json").read_text())
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    v = result["verification"]
    color = BADGE.get(result["outcome"], "#555")

    checks = "".join(f"<tr><td>{'✅' if c['pass'] else '❌'}</td><td>{_e(c['check'])}</td><td>{_e(c['detail'])}</td></tr>"
                     for c in v["checks"])
    facts = "".join(f"<tr><td>{_e(f['id'])}</td><td>{_e(f['key'])}</td><td><b>{_e(f['value'])}</b></td>"
                    f"<td><q>{_e(f['quote'])}</q><br><small>{_e(f['source'])} · {_e(f['observation_id'])}</small></td></tr>"
                    for f in result["ledger"]["facts"])
    escalations = "".join(f"<li><pre>{_e(json.dumps(x, indent=1))}</pre></li>" for x in v.get("escalations", []))
    notable = [e for e in events if e["kind"] in ("recovery", "policy", "approval", "ask_user", "error")]
    decisions = "".join(
        f"<tr class='{_e(e['kind'])}'><td>{e['t']}s</td><td>{_e(e['kind'])}</td><td>"
        f"{_e(json.dumps({k: val for k, val in e.items() if k not in ('t', 'kind')}, ensure_ascii=False)[:400])}</td></tr>"
        for e in notable)
    injections = [e for e in events if e["kind"] == "injection"]
    inj = "".join(f"<li>{_e(e['source'])}: {_e('; '.join(e['matches']))}</li>" for e in injections)
    timeline = "".join(
        f"<tr class='{_e(e['kind'])}'><td>{e['t']}s</td><td>{_e(e['kind'])}</td><td><code>"
        f"{_e(json.dumps({k: val for k, val in e.items() if k not in ('t', 'kind')}, ensure_ascii=False)[:600])}"
        f"</code></td></tr>" for e in events)
    shots = "".join(f"<figure><img src='shots/{p.name}' loading='lazy'><figcaption>{_e(p.name)}</figcaption></figure>"
                    for p in sorted((run_dir / "shots").glob("*.png")))
    goal = result["goal"]

    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>Run {_e(result['run_id'])}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:1100px;margin:24px auto;padding:0 16px;color:#1d2330}}
.badge{{display:inline-block;padding:4px 12px;border-radius:14px;color:#fff;background:{color};font-weight:600}}
table{{border-collapse:collapse;width:100%;font-size:13px}}td,th{{border-bottom:1px solid #e3e6ea;padding:5px;vertical-align:top;text-align:left}}
code{{font-size:12px;word-break:break-all}}tr.recovery td,tr.injection td,tr.policy td,tr.approval td{{background:#fff7e0}}
tr.error td{{background:#fdecec}}figure{{display:inline-block;margin:6px;width:320px}}img{{width:100%;border:1px solid #ccc}}
pre{{white-space:pre-wrap;background:#f6f7f9;padding:8px}}</style></head><body>
<h1>Run {_e(result['run_id'])} <span class="badge">{_e(result['outcome'])}</span></h1>
<p><b>Task:</b> {_e(result['task'])}</p>
<p><b>Worker's summary:</b> {_e(result['summary'])}</p>
<p><small>{result['steps']} steps · {result['usage']['calls']} model calls · {result['usage']['input_tokens']:,} in /
{result['usage']['output_tokens']:,} out tokens · ≈${result['cost_usd']}</small></p>
<h2>Independent verification {'passed' if v['passed'] else 'FAILED'}</h2><table>{checks}</table>
{f'<h2>Recoveries, decisions and human touchpoints</h2><table>{decisions}</table>' if decisions else ''}
<h2>Goal as understood</h2><pre>{_e(json.dumps(goal, indent=1, ensure_ascii=False))}</pre>
<h2>Facts used (with provenance)</h2><table><tr><th>id</th><th>key</th><th>value</th><th>quote · source</th></tr>{facts}</table>
{f'<h2>Escalations</h2><ul>{escalations}</ul>' if escalations else ''}
{f'<h2>Instruction-like text found in external content</h2><ul>{inj}</ul>' if inj else ''}
<h2>Screenshots</h2>{shots}
<h2>Full trace</h2><table>{timeline}</table></body></html>"""
    out = run_dir / "report.html"
    out.write_text(page)
    return out
