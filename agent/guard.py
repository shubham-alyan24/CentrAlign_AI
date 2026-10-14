"""Hard guardrails enforced by the harness, independent of what the LLM decides.

The LLM is told the policies, but we do not *trust* it to follow them. Every browser
action passes through this guard, which reads company/permissions.json:
  * allowed_origins / blocked_path_prefixes - the agent cannot browse anywhere else
  * forbidden_actions                       - always blocked (e.g. "Send offer letter")
  * approval_required                       - pauses for a human, who sees the REAL form
                                              contents read from the page (not the LLM's claim)
  * secrets                                 - values are filled by the harness, never shown to the LLM
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class GuardError(Exception):
    """Raised when an action is not permitted. The message is shown to the agent."""


@dataclass
class Verdict:
    decision: str          # allow | approve | forbid
    reason: str = ""


class Guard:
    def __init__(self, permissions_file: Path):
        self.cfg = json.loads(Path(permissions_file).read_text(encoding="utf-8"))
        self.allowed = [self.origin(o.rstrip("/")) for o in self.cfg.get("allowed_origins", [])]
        self.blocked_prefixes = self.cfg.get("blocked_path_prefixes", [])
        self.secret_names = set(self.cfg.get("secrets", []))
        self.read_only = False     # the verifier runs with read_only=True

    # ---------------------------------------------------------------- urls
    @staticmethod
    def origin(url: str) -> str:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}".replace("://localhost", "://127.0.0.1")

    def check_url(self, url: str) -> None:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            raise GuardError(f"Blocked: only http(s) URLs are allowed, got '{url}'.")
        if self.origin(url) not in self.allowed:
            raise GuardError(f"Blocked: {self.origin(url)} is not an approved company system. "
                             f"Allowed: {', '.join(self.allowed)}")
        if any(p.path.startswith(b) for b in self.blocked_prefixes):
            raise GuardError("Blocked: that path is reserved for system administrators.")

    # -------------------------------------------------------------- clicks
    def check_click(self, page_url: str, label: str, form_fields: dict[str, str], is_submit: bool) -> Verdict:
        label_l = (label or "").strip().lower()
        if self.read_only and is_submit and not any(
                re.search(rx, label_l) for rx in self.cfg.get("read_only_allowed_submits", [])):
            return Verdict("forbid", "Verifier is read-only: it may navigate, sign in and read, but not submit forms.")
        for rule in self.cfg.get("forbidden_actions", []):
            if re.search(rule["label_regex"], label_l):
                return Verdict("forbid", rule["reason"])
        origin = self.origin(page_url)
        for rule in self.cfg.get("approval_required", []):
            if rule.get("origin") and self.origin(rule["origin"].rstrip("/")) != origin:
                continue
            if not re.search(rule["label_regex"], label_l):
                continue
            conds = rule.get("when_field_regex")
            if conds and not any(
                re.search(rx, form_fields.get(fname, "") or "", re.I)
                for keys, rx in conds.items() for fname in keys.split("|")
            ):
                continue
            return Verdict("approve", rule["reason"])
        return Verdict("allow")

    # ------------------------------------------------------------- secrets
    def secret(self, name: str) -> str:
        if name not in self.secret_names:
            raise GuardError(f"Unknown secret '{name}'. Available: {', '.join(sorted(self.secret_names))}")
        val = os.getenv(name)
        if val is None:  # fall back to sandbox defaults so the demo works out of the box
            from sandbox.store import ATS_PASSWORD_DEFAULT, ATS_USERNAME_DEFAULT
            val = {"ATS_USERNAME": ATS_USERNAME_DEFAULT, "ATS_PASSWORD": ATS_PASSWORD_DEFAULT}.get(name)
        if not val:
            raise GuardError(f"Secret '{name}' is not configured.")
        return val

    def redact(self, text: str) -> str:
        for n in self.secret_names:
            try:
                v = self.secret(n)
            except GuardError:
                continue
            if v and len(v) >= 4 and n.endswith("PASSWORD"):
                text = text.replace(v, "********")
        return text
