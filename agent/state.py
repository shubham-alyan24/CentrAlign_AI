"""Run state: everything the agent knows during a run, and how it is rendered into the prompt.

Context strategy: the prompt is rebuilt every step from explicit state (brief, plan, notes,
compressed history, the live page) instead of an ever-growing chat transcript. This keeps
token use bounded on long tasks and makes the agent's "memory" inspectable.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime


def normalize_actions(raw) -> list[tuple[str, dict]]:
    """Accept the common shapes models produce: list or single dict; tool/name/action; args/arguments/parameters."""
    if isinstance(raw, dict):
        raw = [raw]
    out = []
    for a in raw if isinstance(raw, list) else []:
        if not isinstance(a, dict):
            continue
        name = a.get("tool") or a.get("name") or a.get("action") or a.get("function") or ""
        args = a.get("args", a.get("arguments", a.get("parameters", a.get("input"))))
        if args is None:
            args = {k: v for k, v in a.items() if k not in ("tool", "name", "action", "function")}
        if isinstance(args, str):
            import json as _json
            try:
                args = _json.loads(args)
            except ValueError:
                args = {}
        out.append((str(name).split(".")[-1].strip(), args if isinstance(args, dict) else {}))
    return out


def short(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 3] + "..."


@dataclass
class ActionRecord:
    tool: str
    args: dict
    ok: bool
    output: str
    evidence: list[str] = field(default_factory=list)
    url: str = ""


@dataclass
class StepRecord:
    n: int
    phase: str                       # act | verify
    thought: str
    actions: list[ActionRecord] = field(default_factory=list)
    at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


@dataclass
class RunState:
    request: str
    run_id: str
    today: str
    model: str
    brief: dict = field(default_factory=dict)
    docs: dict[str, str] = field(default_factory=dict)
    lessons: list[str] = field(default_factory=list)
    plan: list[dict] = field(default_factory=list)
    notes: dict[str, str] = field(default_factory=dict)
    steps: list[StepRecord] = field(default_factory=list)
    verifications: list[dict] = field(default_factory=list)
    human_log: list[dict] = field(default_factory=list)
    status: str = "running"
    summary: str = ""
    evidence: list[str] = field(default_factory=list)
    new_lessons: list[str] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    finished_at: str = ""
    usage: dict = field(default_factory=dict)
    error: str = ""

    # ------------------------------------------------------------- render
    def render_plan(self) -> str:
        if not self.plan:
            return "(no plan yet)"
        icon = {"done": "[x]", "doing": "[>]", "todo": "[ ]", "skipped": "[-]", "failed": "[!]"}
        return "\n".join(f"{icon.get(p.get('status', 'todo'), '[ ]')} {short(p.get('step', ''), 140)}"
                         for p in self.plan[:15])

    def render_notes(self) -> str:
        return "\n".join(f"- {k}: {v}" for k, v in self.notes.items()) or "(empty)"

    def render_human(self) -> str:
        if not self.human_log:
            return "(none)"
        out = []
        for h in self.human_log:
            if h["kind"] == "question":
                out.append(f"- You asked: {short(h['prompt'], 200)} -> Human answered: {h['answer']}")
            else:
                out.append(f"- Approval for: {short(h['prompt'], 160)} -> {'APPROVED' if h['approved'] else 'NOT approved'}"
                           f" ({h['answer']})")
        return "\n".join(out)

    def render_history(self, phase: str = "act", recent: int = 8) -> str:
        steps = [s for s in self.steps if s.phase == phase]
        if not steps:
            return "(nothing yet)"
        lines = []
        cut = len(steps) - recent
        older = steps[:cut] if cut > 0 else []
        if len(older) > 15:                       # very old steps: just a count (NOTES keep the facts)
            lines.append(f"(#{older[0].n}-#{older[-16].n}: {len(older) - 15} earlier steps omitted)")
            older = older[-15:]
        for s in older:
            acts = "; ".join(f"{a.tool}{'' if a.ok else ' FAILED'}: {short(a.output, 70)}" for a in s.actions)
            lines.append(f"#{s.n} {acts}")
        for s in steps[max(cut, 0):]:
            lines.append(f"#{s.n} thought: {short(s.thought, 160)}")
            for a in s.actions:
                args = short(json.dumps(a.args, ensure_ascii=False), 140)
                lines.append(f"    {a.tool}({args}) -> {'OK' if a.ok else 'FAILED'}: {short(a.output, 220)}")
        return "\n".join(lines)

    def failing_repeats(self, phase: str = "act") -> list[str]:
        """Detect the agent hammering the same failing action (loop detection)."""
        seen: dict[str, int] = {}
        for s in [s for s in self.steps if s.phase == phase][-6:]:
            for a in s.actions:
                if not a.ok:
                    key = f"{a.tool}({json.dumps(a.args, sort_keys=True)}) on {a.url}"
                    seen[key] = seen.get(key, 0) + 1
        return [k for k, v in seen.items() if v >= 2]

    def circling(self, phase: str = "act") -> list[str]:
        """Detect the agent going in circles: the same (successful) navigation action repeated without progress."""
        recent = [s for s in self.steps if s.phase == phase][-8:]
        seen: dict[str, int] = {}
        for s in recent:
            for a in s.actions:
                if a.tool in ("open_url", "observe", "go_back", "read_page", "click"):
                    key = f"{a.tool}({json.dumps(a.args, sort_keys=True)})" + (f" on {a.url}" if a.tool == "click" else "")
                    seen[key] = seen.get(key, 0) + 1
        return [f"{k} x{v}" for k, v in seen.items() if v >= 3]

    def to_json(self) -> dict:
        return asdict(self)
