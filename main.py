"""Autonomous Company Operator - command line.

  python main.py demo 1                 run demo task 1 (see: python main.py tasks)
  python main.py run "your request"     run any request
  python main.py sandbox                just start the company apps to click around
  python main.py check                  test your LLM key/model with one tiny call

Options: --headed (watch the browser)  --slow 400 (ms between browser actions)
         --approve ask|auto|deny       --no-chaos (disable injected failures)
         --keep-state (don't reset the company data first)  --no-learn
"""
from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

ROOT = Path(__file__).resolve().parent
console = Console()


def printer(kind: str, d: dict) -> None:
    if kind == "start":
        console.print(Panel(f"[bold]{d['request']}[/bold]\nmodel: {d['model']}   run: {d['run_id']}",
                            title="New request", border_style="cyan"))
    elif kind == "docs":
        console.print(f"[magenta]UNDERSTAND[/magenta] reading company docs: {', '.join(d['docs'])}  [dim]{d.get('why', '')}[/dim]")
    elif kind == "brief":
        console.print(f"[magenta]GOAL[/magenta] {d.get('objective', '')}")
        for c in d.get("success_criteria", []):
            console.print(f"   [dim]done when:[/dim] {c}")
        for i, p in enumerate(d.get("plan", []), 1):
            console.print(f"   [dim]plan {i}:[/dim] {p}")
    elif kind == "thought":
        console.print(f"\n[bold cyan]Step {d['n']}[/bold cyan] [italic]{d['thought']}[/italic]")
    elif kind == "action":
        args = json.dumps(d["args"], ensure_ascii=False)
        args = args if len(args) < 110 else args[:107] + "..."
        mark = "[green]✓[/green]" if d["ok"] else "[red]✗[/red]"
        console.print(f"  {mark} {d['tool']} {args}\n     [dim]{d['output'][:220]}[/dim]")
    elif kind == "verify_start":
        console.print(f"\n[bold magenta]VERIFY[/bold magenta] independent check, round {d['round']} ...")
    elif kind == "verify_action":
        console.print(f"  [magenta]·[/magenta] {d['tool']} {json.dumps(d['args'], ensure_ascii=False)[:100]}")
    elif kind == "verify_done":
        color = "green" if d.get("verdict") == "pass" else "red"
        console.print(f"[bold {color}]VERDICT: {str(d.get('verdict')).upper()}[/bold {color}]")
        for c in d.get("criteria", []):
            console.print(f"   {'✅' if c.get('met') else '❌'} {c.get('criterion', '')}")
        for i in d.get("issues", []):
            console.print(f"   [red]issue:[/red] {i}")
    elif kind == "warn":
        console.print(f"[yellow]{d['message']}[/yellow]")
    elif kind == "done":
        color = {"completed": "green"}.get(d["status"], "yellow" if d["status"] in ("blocked", "unverified") else "red")
        console.print(Panel(f"{d.get('summary') or d.get('error') or ''}\n\nReport: {d['report']}",
                            title=f"[bold]{d['status'].upper()}[/bold]", border_style=color))


def load_tasks() -> list[dict]:
    return json.loads((ROOT / "tasks.json").read_text(encoding="utf-8"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Autonomous Company Operator")
    ap.add_argument("cmd", choices=["run", "demo", "sandbox", "tasks", "check"])
    ap.add_argument("arg", nargs="?", help="request text (run) or task id (demo)")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    ap.add_argument("--slow", type=int, default=None, help="slow down browser actions (ms)")
    ap.add_argument("--approve", choices=["ask", "auto", "deny"], default="ask")
    ap.add_argument("--no-chaos", action="store_true", help="disable injected failures")
    ap.add_argument("--keep-state", action="store_true", help="do not reset the sandbox data before the run")
    ap.add_argument("--no-learn", action="store_true", help="do not save lessons after the run")
    ap.add_argument("--open", action="store_true", help="open the HTML report when done")
    a = ap.parse_args(argv)

    from agent.config import Settings
    from sandbox import server

    if a.cmd == "tasks":
        for t in load_tasks():
            console.print(f"[bold]{t['id']}[/bold] {t['name']}: {t['request']}\n   [dim]shows: {t['shows']}[/dim]")
        return 0
    if a.cmd == "sandbox":
        server.start()
        server.reset(chaos=False if a.no_chaos else None)
        console.print(f"Acme Mail: {server.MAIL_URL}\nHireDesk : {server.ATS_URL}  (recruiter / Hire@2026)\nCtrl+C to stop.")
        import time
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return 0

    settings = Settings()
    settings.approval_mode = a.approve
    settings.learn = not a.no_learn
    if a.headed:
        settings.headed = True
        settings.slow_mo = settings.slow_mo or 250
    if a.slow is not None:
        settings.slow_mo = a.slow

    from agent.llm import LLM, LLMError
    try:
        llm = LLM(settings)
    except LLMError as e:
        console.print(f"[red]{e}[/red]  (copy .env.example to .env and fill it in)")
        return 2
    if a.cmd == "check":
        try:
            out = llm.json("Reply with JSON only.", 'Reply with {"ok": true, "model": "<your model name>"}', "check",
                           attempts=2)
            console.print(f"[green]LLM OK[/green] {llm.name} -> {out}")
            return 0
        except LLMError as e:
            console.print(f"[red]LLM check failed[/red] {llm.name}: {e}")
            return 1

    answers = None
    if a.cmd == "demo":
        task = next((t for t in load_tasks() if t["id"] == a.arg or t["name"] == a.arg), None)
        if not task:
            console.print("Unknown demo task. See: python main.py tasks")
            return 2
        request, answers = task["request"], task.get("answers")
    else:
        if not a.arg:
            console.print('Give a request: python main.py run "..."')
            return 2
        request = a.arg

    started = server.start()
    if not a.keep_state:
        server.reset(chaos=False if a.no_chaos else None)
    if started:
        console.print(f"[dim]Sandbox started: mail {server.MAIL_URL}  ·  HireDesk {server.ATS_URL}[/dim]")

    from agent.loop import Operator
    from agent.tools.human import ConsoleHuman, ScriptedHuman
    # --approve ask: a person answers in this terminal. auto/deny: unattended (pre-set answers for questions)
    human = ConsoleHuman("ask") if a.approve == "ask" else ScriptedHuman(answers, approve=a.approve == "auto")
    state = Operator(settings, llm, human, on_event=printer).run(request)
    if a.open:
        webbrowser.open((settings.runs_dir / state.run_id / "report.html").as_uri())
    return 0 if state.status == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
