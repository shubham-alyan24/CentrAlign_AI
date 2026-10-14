"""Evidence pack for every run: runs/<run_id>/report.html (human-readable), trace.json (machine-readable),
evidence/*.jpg (screenshots taken after each submit / navigation)."""
from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path

from .state import RunState

CSS = """body{font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif;margin:0;background:#f5f6f8;color:#1c2230}
main{max-width:1100px;margin:0 auto;padding:24px}h1{font-size:22px;margin:0 0 6px}h2{font-size:17px;margin:26px 0 8px}
.card{background:#fff;border:1px solid #e1e5ec;border-radius:10px;padding:14px 18px;margin:10px 0}
.badge{display:inline-block;padding:2px 10px;border-radius:12px;font-weight:600;font-size:13px}
.completed,.pass,.ok{background:#e3f5e8;color:#17663a}.blocked,.unverified,.out_of_steps{background:#fff4dc;color:#7a5200}
.failed,.fail,.error,.bad,.interrupted{background:#fde8e8;color:#8f1d1d}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #edf0f4;padding:6px 8px;text-align:left;vertical-align:top}
.muted{color:#69707d}.step{border-left:3px solid #c9d3e6;padding-left:12px;margin:12px 0}.step.verify{border-color:#9b7be0}
.act{font-family:ui-monospace,Consolas,monospace;font-size:12.5px;margin:3px 0;white-space:pre-wrap;word-break:break-word}
.shots img{height:120px;border:1px solid #d6dbe4;border-radius:6px;margin:4px 6px 0 0}
pre{white-space:pre-wrap;margin:0}"""


def esc(x) -> str:
    return html.escape(str(x))


def write_report(state: RunState, run_dir: Path) -> Path:
    run_dir = Path(run_dir)
    (run_dir / "trace.json").write_text(json.dumps(state.to_json(), indent=2, ensure_ascii=False), encoding="utf-8")

    def rel(p: str) -> str:
        try:
            return str(Path(p).resolve().relative_to(run_dir.resolve())).replace("\\", "/")
        except ValueError:
            return p

    try:
        dur = (datetime.fromisoformat(state.finished_at) - datetime.fromisoformat(state.started_at)).seconds
    except ValueError:
        dur = 0
    v = state.verifications[-1] if state.verifications else None
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><title>Run report - {esc(state.run_id)}</title>"
             f"<style>{CSS}</style></head><body><main>",
             f"<h1>Run report <span class='badge {esc(state.status)}'>{esc(state.status.upper())}</span></h1>",
             f"<div class='muted'>{esc(state.run_id)} · model {esc(state.model)} · {dur}s · "
             f"{state.usage.get('calls', 0)} LLM calls · {state.usage.get('input_tokens', 0):,} in / "
             f"{state.usage.get('output_tokens', 0):,} out tokens</div>",
             f"<div class='card'><b>Request</b><pre>{esc(state.request)}</pre></div>",
             f"<div class='card'><b>Operator's summary</b><pre>{esc(state.summary or state.error or '(none)')}</pre></div>"]
    if v:
        rows = "".join(f"<tr><td>{'✅' if c.get('met') else '❌'}</td><td>{esc(c.get('criterion', ''))}</td>"
                       f"<td>{esc(c.get('evidence', ''))}</td></tr>" for c in v.get("criteria", []))
        issues = "".join(f"<li>{esc(i)}</li>" for i in v.get("issues", []))
        parts.append(f"<h2>Independent verification <span class='badge {esc(v.get('verdict'))}'>"
                     f"{esc(str(v.get('verdict')).upper())}</span> <span class='muted'>(round {len(state.verifications)})"
                     f"</span></h2><div class='card'><table><tr><th></th><th>Success criterion</th><th>Evidence seen by verifier"
                     f"</th></tr>{rows}</table>{'<b>Issues</b><ul>' + issues + '</ul>' if issues else ''}</div>")
    b = state.brief or {}
    parts.append("<h2>Understanding</h2><div class='card'>"
                 f"<div><b>Objective:</b> {esc(b.get('objective', ''))}</div>"
                 f"<div><b>Company documents used:</b> {esc(', '.join(state.docs))}</div>"
                 "<b>Success criteria</b><ul>" + "".join(f"<li>{esc(c)}</li>" for c in b.get("success_criteria", [])) + "</ul>"
                 "<b>Constraints</b><ul>" + "".join(f"<li>{esc(c)}</li>" for c in b.get("constraints", [])) + "</ul>"
                 "<b>Assumptions</b><ul>" + "".join(f"<li>{esc(c)}</li>" for c in b.get("assumptions", [])) + "</ul></div>")
    if state.human_log:
        rows = "".join(f"<tr><td>{esc(h['kind'])}</td><td><pre>{esc(h['prompt'])}</pre></td><td>{esc(h['answer'])}</td></tr>"
                       for h in state.human_log)
        parts.append(f"<h2>Human in the loop</h2><div class='card'><table><tr><th>Type</th><th>Agent asked</th>"
                     f"<th>Human</th></tr>{rows}</table></div>")
    parts.append("<h2>Final plan & working memory</h2><div class='card'><pre>" + esc(state.render_plan()) +
                 "</pre><hr><pre>" + esc(state.render_notes()) + "</pre></div>")
    if state.new_lessons:
        parts.append("<h2>Lessons learned (saved for future runs)</h2><div class='card'><ul>" +
                     "".join(f"<li>{esc(l)}</li>" for l in state.new_lessons) + "</ul></div>")
    parts.append("<h2>Timeline</h2>")
    for s in state.steps:
        acts = "".join(f"<div class='act'><span class='badge {'ok' if a.ok else 'bad'}'>{'OK' if a.ok else 'FAIL'}</span> "
                       f"<b>{esc(a.tool)}</b>({esc(json.dumps(a.args, ensure_ascii=False)[:300])})<br>"
                       f"<span class='muted'>&rarr; {esc(a.output[:600])}</span></div>" for a in s.actions)
        shots = "".join(f"<a href='{esc(rel(e))}' target='_blank'><img src='{esc(rel(e))}' loading='lazy'></a>"
                        for a in s.actions for e in a.evidence if e.endswith((".jpg", ".png")))
        parts.append(f"<div class='step {s.phase}'><b>{'Verifier' if s.phase == 'verify' else 'Step'} {s.n}</b> "
                     f"<span class='muted'>{esc(s.at)}</span><div>{esc(s.thought)}</div>{acts}"
                     f"{'<div class=shots>' + shots + '</div>' if shots else ''}</div>")
    if state.error:
        parts.append(f"<h2>Error</h2><div class='card'><pre>{esc(state.error)}</pre></div>")
    parts.append("</main></body></html>")
    out = run_dir / "report.html"
    out.write_text("".join(parts), encoding="utf-8")
    return out
