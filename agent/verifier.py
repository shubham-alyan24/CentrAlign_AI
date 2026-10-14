"""Independent verification.

When the operator claims "completed", a separate verifier with a FRESH context (it never sees the
operator's reasoning or history, only the success criteria and the claim) inspects the real
systems with READ-ONLY tools (the guard blocks form submissions) and returns a verdict per
criterion. A failed verdict is fed back to the operator as actionable issues.
"""
from __future__ import annotations

import json

from . import prompts
from .state import ActionRecord, RunState, StepRecord, normalize_actions
from .tools.base import ToolRegistry

READ_ONLY_TOOLS = ["open_url", "click", "type_text", "select_option", "fill_secret", "read_page", "observe",
                   "screenshot", "list_files", "read_file", "read_doc"]


class Verifier:
    def __init__(self, settings, llm, guard, browser, files, emit, max_steps: int = 12):
        self.s, self.llm, self.guard, self.browser, self.files, self.emit = settings, llm, guard, browser, files, emit
        self.max_steps = max_steps
        reg = ToolRegistry()
        for t in browser.tools() + files.tools():
            reg.register(t)
        self.reg = reg.subset(READ_ONLY_TOOLS)

    def verify(self, state: RunState, claimed_evidence: list) -> dict:
        self.guard.read_only = True
        try:
            return self._verify(state, claimed_evidence)
        finally:
            self.guard.read_only = False

    def _verify(self, state: RunState, claimed_evidence: list) -> dict:
        system = prompts.VERIFIER_SYSTEM.format(tools=self.reg.describe())
        claim = state.summary + ("\nClaimed evidence: " + json.dumps(claimed_evidence, ensure_ascii=False)
                                 if claimed_evidence else "")
        history: list[str] = []
        last_output = "(none)"
        base = len([s for s in state.steps if s.phase == "verify"])
        for n in range(1, self.max_steps + 1):
            page = self.browser.snapshot() if self.browser.opened or self.browser.page.url.startswith("http") \
                else "(no page open)"
            prompt = prompts.VERIFIER_STEP.format(
                today=state.today, request=state.request,
                criteria="\n".join(f"{i + 1}. {c}" for i, c in enumerate(state.brief.get("success_criteria", []))),
                constraints="\n".join(f"- {c}" for c in state.brief.get("constraints", [])) or "(none)",
                facts="; ".join(state.brief.get("key_facts", []))[:1500] or "(none)",
                claim=claim, human=state.render_human(), history="\n".join(history[-10:]) or "(none yet)",
                last_output=last_output, page=page, step=n, max_steps=self.max_steps)
            if n == self.max_steps:
                prompt += "\nThis is your LAST step: give the verdict now based on what you have seen."
            d = self.llm.json(system, prompt, purpose="verify")
            step = StepRecord(n=base + n, phase="verify", thought=str(d.get("thought", "")))
            state.steps.append(step)
            if "verdict" in d:
                d["verdict"] = "pass" if str(d["verdict"]).lower() == "pass" and \
                    all(c.get("met") for c in d.get("criteria", [])) else "fail"
                d.setdefault("criteria", [])
                d.setdefault("issues", [])
                step.actions.append(ActionRecord("verdict", {}, True, d["verdict"]))
                return d
            last_output = "(none)"
            for name, args in normalize_actions(d.get("actions"))[:4]:
                res = self.reg.execute(name, args)
                step.actions.append(ActionRecord(name, args, res.ok, res.output, res.evidence, self.browser.page.url))
                state.evidence += res.evidence
                history.append(f"{name}({json.dumps(args, ensure_ascii=False)[:120]}) -> "
                               f"{'OK' if res.ok else 'FAILED'}: {res.output[:200]}")
                self.emit("verify_action", {"tool": name, "args": args, "ok": res.ok, "output": res.output})
                if res.observation and name in ("read_page", "read_file", "list_files", "read_doc"):
                    last_output = f"[{name}] {res.observation}"
                if not res.ok or res.page_changed:
                    break
        return {"verdict": "fail", "criteria": [], "issues": ["Verifier ran out of steps before reaching a verdict."]}
