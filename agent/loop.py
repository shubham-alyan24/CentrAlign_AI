"""The operator: Goal -> Understand -> Plan -> Execute -> Observe -> Adapt -> Verify -> Complete.

    request ──► select docs ──► brief (objective, success criteria, constraints, plan)
                                    │
             ┌──────────────────────▼───────────────────────┐
             │ act loop: prompt(state + live page) ─► LLM ─► actions
             │   each action ─► guard ─► tool ─► observation ─► state
             │   errors / loops / approvals feed back into the next prompt
             └──────────────────────┬───────────────────────┘
                                    │ finish(completed)
                         independent verifier (read-only)
                          pass ─► done      fail ─► issues fed back, keep acting
                                    │
                    report (HTML + JSON trace) + lessons for next runs
"""
from __future__ import annotations

import json
import re
import traceback
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from . import prompts
from .config import Settings
from .guard import Guard
from .knowledge import Knowledge
from .lessons import LessonStore
from .llm import LLMError
from .state import ActionRecord, RunState, StepRecord, normalize_actions, short
from .tools.base import Tool, ToolRegistry, ToolResult
from .tools.browser import Browser
from .tools.files import Files
from .tools.human import HumanChannel

BROWSER_TOOLS = {"open_url", "click", "type_text", "select_option", "upload_file", "fill_secret", "press_key",
                 "go_back", "observe", "screenshot"}
MAX_ACTIONS_PER_STEP = 12

Event = Callable[[str, dict], None]


def slugify(text: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:n] or "run"


class Operator:
    def __init__(self, settings: Settings, llm, human: HumanChannel, on_event: Event | None = None):
        self.s = settings
        self.llm = llm
        self.human = human
        self.emit: Event = on_event or (lambda kind, data: None)
        self.knowledge = Knowledge(settings.company_dir)
        self.lessons = LessonStore(settings.memory_file)

    # ================================================================ public
    def run(self, request: str) -> RunState:
        from .report import write_report
        from .verifier import Verifier

        run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + slugify(request, 30)
        run_dir = self.s.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        today = self._today()
        state = RunState(request=request, run_id=run_id, today=today, model=getattr(self.llm, "name", "?"))
        state.lessons = self.lessons.load()
        self.human.log = state.human_log            # share the log so the prompt sees every interaction

        guard = Guard(self.s.company_dir / "permissions.json")
        browser = Browser(guard, self.human, self.s.workspace_dir, run_dir / "evidence",
                          headed=self.s.headed, slow_mo=self.s.slow_mo, max_text=self.s.page_text_chars)
        files = Files(self.s.company_dir.parent, self.s.workspace_dir, self.s.company_dir)
        registry = self._registry(state, browser, files)
        self.emit("start", {"run_id": run_id, "request": request, "model": state.model})
        try:
            self._understand(state)
            self._act(state, registry, browser, Verifier(self.s, self.llm, guard, browser, files, self.emit))
        except KeyboardInterrupt:
            state.status, state.error = "interrupted", "Stopped by user (Ctrl+C)."
        except LLMError as e:
            state.status, state.error = "error", f"LLM error: {e}"
        except Exception as e:  # never lose the trace
            state.status, state.error = "error", "".join(traceback.format_exception(e))[-2000:]
        finally:
            try:
                state.evidence.append(browser.screenshot_file("final page"))
            except Exception:
                pass
            browser.close()
        if self.s.learn and state.steps:
            self._reflect(state)
        state.finished_at = datetime.now().isoformat(timespec="seconds")
        state.usage = {"calls": self.llm.usage.calls, "input_tokens": self.llm.usage.input_tokens,
                       "output_tokens": self.llm.usage.output_tokens, "llm_seconds": round(self.llm.usage.seconds, 1),
                       "by_purpose": self.llm.usage.by_purpose}
        report = write_report(state, run_dir)
        self.emit("done", {"status": state.status, "summary": state.summary, "report": str(report),
                           "error": state.error})
        return state

    # ============================================================ understand
    def _today(self) -> str:
        import os
        d = date.fromisoformat(os.getenv("SANDBOX_TODAY")) if os.getenv("SANDBOX_TODAY") else date.today()
        return d.strftime("%A %d %B %Y")

    def _understand(self, state: RunState) -> None:
        sel = self.llm.json("You select relevant company documents. Reply with JSON only.",
                            prompts.SELECT_DOCS.format(request=state.request, index=self.knowledge.index()),
                            purpose="understand", required=("relevant_docs",))
        names = self.knowledge.resolve(sel.get("relevant_docs", []))
        state.docs = {n: self.knowledge.read(n) for n in names}
        self.emit("docs", {"docs": names, "why": sel.get("why", "")})
        state.brief = self._brief(state, state.request)
        q = state.brief.get("blocking_question")
        if q:
            ans = self.human.ask(q)
            state.notes["clarification from requester"] = ans
            state.brief = self._brief(state, f"{state.request}\n(Requester clarified: {q} -> {ans})")
        state.plan = [{"step": p, "status": "todo"} for p in state.brief.get("plan", [])]
        self.emit("brief", state.brief)

    def _brief(self, state: RunState, request: str) -> dict:
        b = self.llm.json("You write precise task briefs. Reply with JSON only.",
                          prompts.BRIEF.format(today=state.today, request=request, docs=self._docs_text(state),
                                               lessons=self._lessons_text(state)),
                          purpose="plan", required=("objective", "success_criteria", "plan"))
        for key in ("success_criteria", "constraints", "key_facts", "assumptions", "plan"):
            v = b.get(key) or []
            b[key] = [v] if isinstance(v, str) else [str(x) if not isinstance(x, str) else x for x in v]
        return b

    @staticmethod
    def _brief_text(state: RunState) -> str:
        b = state.brief
        out = [f"Objective: {b.get('objective', '')}"]
        for key, title in (("success_criteria", "Done when"), ("constraints", "Constraints"),
                           ("key_facts", "Key facts"), ("assumptions", "Assumptions")):
            items = b.get(key) or []
            if items:
                out.append(f"{title}:\n" + "\n".join(f"- {x}" for x in items))
        return "\n".join(out)

    def _docs_text(self, state: RunState) -> str:
        return "\n\n".join(f"### {n}\n{t.strip()}" for n, t in state.docs.items()) or "(none)"

    def _lessons_text(self, state: RunState) -> str:
        return "\n".join(f"- {l}" for l in state.lessons) or "(none yet)"

    # ================================================================== act
    def _registry(self, state: RunState, browser: Browser, files: Files) -> ToolRegistry:
        reg = ToolRegistry()
        for t in browser.tools() + files.tools():
            reg.register(t)

        def remember(key: str, value: str) -> ToolResult:
            state.notes[str(key)] = str(value)
            return ToolResult(True, f"Noted '{key}'.")

        def ask_human(question: str) -> ToolResult:
            self.emit("ask", {"question": question})
            return ToolResult(True, f"Human answered: {self.human.ask(question)}")

        def request_approval(action: str, details: str = "") -> ToolResult:
            ok, comment = self.human.approve(f"Agent requests approval: {action}", details or "(no details)")
            return ToolResult(True, f"{'APPROVED' if ok else 'NOT APPROVED'} by human. Comment: {comment}")

        reg.register(Tool("remember", "Save a fact to working memory (kept for the whole run).",
                          {"key": "short name", "value": "the fact"}, remember))
        reg.register(Tool("ask_human", "Ask the requester a question and wait for the answer.",
                          {"question": "clear, specific question"}, ask_human))
        reg.register(Tool("request_approval", "Ask a human to approve something policy says needs approval "
                                              "(button-level approvals happen automatically).",
                          {"action": "what you want to do", "details?": "exact content"}, request_approval))
        return reg

    def _act(self, state: RunState, reg: ToolRegistry, browser: Browser, verifier) -> None:
        system = prompts.OPERATOR_SYSTEM.format(tools=reg.describe())
        last_output = "(none)"
        warnings: list[str] = []
        verify_rounds = 0
        for n in range(1, self.s.max_steps + 1):
            page = browser.snapshot() if browser.opened or browser.page.url.startswith("http") \
                else "(browser not opened yet)"
            repeats = state.failing_repeats()
            if repeats:
                warnings.append("You are repeating actions that keep failing: " + "; ".join(repeats[:3]) +
                                ". Do something different (re-read the page, fix the input, use another path, or ask the human).")
            circles = state.circling()
            if circles:
                warnings.append("You are going in circles (" + "; ".join(circles[:3]) + "). Stop re-opening pages: "
                                "use what you already have (NOTES, LAST TOOL OUTPUT, CURRENT PAGE) and take the next "
                                "concrete step of the plan, e.g. fill and submit the form. remember key facts first.")
            if n == self.s.max_steps - 5:
                warnings.append("Only 5 steps left. Wrap up: complete what is essential, then finish with an honest status.")
            prompt = prompts.STEP.format(
                today=state.today, request=state.request, brief=self._brief_text(state),
                doc_names=", ".join(state.docs) or "(none)", lessons=self._lessons_text(state),
                plan=state.render_plan(), notes=state.render_notes(), human=state.render_human(),
                history=state.render_history(recent=self.s.history_recent),
                warnings=("\nWARNINGS:\n" + "\n".join(f"! {w}" for w in warnings) + "\n") if warnings else "",
                last_output=last_output, page=page, step=n, max_steps=self.s.max_steps)
            warnings = []
            decision = self.llm.json(system, prompt, purpose="act", required=("actions",))
            step = StepRecord(n=n, phase="act", thought=str(decision.get("thought", "")))
            state.steps.append(step)
            if isinstance(decision.get("plan"), list) and decision["plan"]:
                state.plan = [p if isinstance(p, dict) else {"step": str(p), "status": "todo"} for p in decision["plan"]]
            self.emit("thought", {"n": n, "thought": step.thought, "plan": state.plan})
            actions = normalize_actions(decision.get("actions"))
            if not actions:
                warnings.append("Your reply had no actions. Always include at least one action (or finish).")
                continue
            last_output = "(none)"
            finish_args = None
            for name, args in actions[:MAX_ACTIONS_PER_STEP]:
                if name == "finish":
                    finish_args = args
                    step.actions.append(ActionRecord("finish", args, True, f"finish: {args.get('status')}"))
                    break
                url = browser.page.url
                res = reg.execute(name, args)
                rec = ActionRecord(name, args, res.ok, res.output, res.evidence, url)
                step.actions.append(rec)
                state.evidence += res.evidence
                self.emit("action", {"n": n, "tool": name, "args": args, "ok": res.ok, "output": res.output})
                # documents the agent has seen are pinned in working memory, so later steps copy real values
                # instead of guessing them once the text has scrolled out of the history
                for path, text in (res.data.get("download_docs") or {}).items():
                    if text:
                        state.notes[f"document {path}"] = short(text, 900)
                if name == "read_file" and res.ok and res.observation and "/downloads/" in str(args.get("path", "")):
                    state.notes[f"document {args.get('path')}"] = short(res.observation.split("---\n", 1)[-1], 900)
                if res.data.get("download_text"):
                    last_output = f"[{name}] {res.output}\nThe downloaded file was read for you:{res.data['download_text']}"
                elif res.observation and name not in BROWSER_TOOLS:
                    last_output = f"[{name}] {res.output}\n{res.observation}"
                elif not res.ok:
                    last_output = f"[{name}] FAILED: {res.output}"
                if not res.ok or res.page_changed:
                    break
            if len(actions) > MAX_ACTIONS_PER_STEP:
                warnings.append(f"Only the first {MAX_ACTIONS_PER_STEP} actions were executed.")
            if finish_args is None:
                continue

            # --------------------------------------------------- finish + verify
            status = str(finish_args.get("status", "completed")).lower()
            state.summary = str(finish_args.get("summary", ""))
            claimed = finish_args.get("evidence") or []
            if status != "completed":
                state.status = status if status in ("blocked", "failed") else "blocked"
                return
            self.emit("verify_start", {"round": verify_rounds + 1})
            v = verifier.verify(state, claimed)
            state.verifications.append(v)
            self.emit("verify_done", v)
            if v.get("verdict") == "pass":
                state.status = "completed"
                return
            verify_rounds += 1
            if verify_rounds > self.s.max_verify_rounds:
                state.status = "unverified"
                return
            issues = "; ".join(v.get("issues") or []) or "see criteria"
            failed = [c for c in v.get("criteria", []) if not c.get("met")]
            warnings.append("INDEPENDENT VERIFICATION FAILED - the task is NOT done. Issues: " + issues +
                            (". Unmet: " + "; ".join(short(c.get("criterion", ""), 120) for c in failed) if failed else "") +
                            ". Fix these, then finish again.")
            last_output = "[verifier] " + json.dumps(v, ensure_ascii=False)[:2500]
        state.status = "out_of_steps"

    # ============================================================== reflect
    def _reflect(self, state: RunState) -> None:
        errors = []
        for s in state.steps:
            for a in s.actions:
                if not a.ok:
                    errors.append(f"{a.tool}: {short(a.output, 200)}")
        for v in state.verifications:
            errors += [f"verifier: {i}" for i in v.get("issues", [])]
        if not errors:
            return
        try:
            out = self.llm.json("You extract reusable lessons. Reply with JSON only.",
                                prompts.REFLECT.format(summary=f"status={state.status}; {state.summary}",
                                                       errors="\n".join(errors[:25]),
                                                       existing=self._lessons_text(state)),
                                purpose="reflect", required=("lessons",))
            state.new_lessons = self.lessons.add([str(l) for l in out.get("lessons", [])][:3], state.run_id)
        except Exception as e:  # learning is best-effort
            state.new_lessons = []
            self.emit("warn", {"message": f"reflection skipped: {e}"})
