"""Human-in-the-loop channel: clarifying questions and approvals.

ConsoleHuman asks in the terminal. ScriptedHuman is used for unattended runs/tests
(pre-set answers and an approval policy). Every interaction is logged for the report.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class HumanChannel:
    log: list[dict] = field(default_factory=list)

    def _record(self, kind: str, prompt: str, answer: str, approved: bool | None = None) -> None:
        self.log.append({"at": datetime.now().isoformat(timespec="seconds"), "kind": kind,
                         "prompt": prompt, "answer": answer, "approved": approved})

    def ask(self, question: str) -> str:
        raise NotImplementedError

    def approve(self, title: str, details: str) -> tuple[bool, str]:
        raise NotImplementedError


class ConsoleHuman(HumanChannel):
    def __init__(self, mode: str = "ask"):
        super().__init__()
        self.mode = mode  # ask | auto | deny

    def ask(self, question: str) -> str:
        print("\n" + "=" * 70 + f"\n  AGENT QUESTION: {question}\n" + "=" * 70)
        ans = input("  Your answer > ").strip() or "(no answer)"
        self._record("question", question, ans)
        return ans

    def approve(self, title: str, details: str) -> tuple[bool, str]:
        print("\n" + "#" * 70 + f"\n  APPROVAL NEEDED: {title}\n" + "#" * 70)
        print(details)
        print("#" * 70)
        if self.mode == "auto":
            print("  [auto-approved by --approve auto]")
            self._record("approval", f"{title}\n{details}", "auto-approved", True)
            return True, "auto-approved"
        if self.mode == "deny":
            print("  [auto-denied by --approve deny]")
            self._record("approval", f"{title}\n{details}", "auto-denied", False)
            return False, "auto-denied"
        ans = input("  Approve? [y = yes / n = no / or type feedback] > ").strip()
        ok = ans.lower() in ("y", "yes")
        comment = "approved" if ok else (ans if ans.lower() not in ("n", "no", "") else "rejected")
        self._record("approval", f"{title}\n{details}", comment, ok)
        return ok, comment


class ScriptedHuman(HumanChannel):
    """Non-interactive human for tests and unattended demo runs."""

    def __init__(self, answers: list[str] | None = None, approve: bool = True, verbose: bool = True):
        super().__init__()
        self.answers = list(answers or [])
        self.approve_all = approve
        self.verbose = verbose

    def ask(self, question: str) -> str:
        ans = self.answers.pop(0) if self.answers else "No human is available right now. Use your best judgement, " \
                                                         "state your assumption, and do not take irreversible actions."
        self._record("question", question, ans)
        if self.verbose:
            print(f"\n  AGENT QUESTION: {question}\n  [scripted answer] {ans}")
        return ans

    def approve(self, title: str, details: str) -> tuple[bool, str]:
        comment = "approved" if self.approve_all else "rejected (scripted)"
        if self.verbose:
            print(f"\n  APPROVAL NEEDED: {title}\n{details}\n  [scripted] {comment}")
        self._record("approval", f"{title}\n{details}", comment, self.approve_all)
        return self.approve_all, comment
