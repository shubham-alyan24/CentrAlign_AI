"""Tool abstraction: every capability the agent has is a Tool with a name, a description,
typed parameters and a function. New connectors (an API, a desktop app driver, a DB) are
added by registering more Tools - the agent loop does not change."""
from __future__ import annotations

import inspect
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ToolResult:
    ok: bool
    output: str                          # short, LLM-facing result
    observation: str | None = None       # full page/file observation (only the latest is kept in context)
    evidence: list[str] = field(default_factory=list)   # screenshot / file paths
    data: dict = field(default_factory=dict)            # structured extras (finish payload, etc.)
    page_changed: bool = False


@dataclass
class Tool:
    name: str
    description: str
    params: dict[str, str]               # name -> description ("?" suffix in name = optional)
    fn: Callable[..., ToolResult]
    mutates: bool = False                # changes the outside world (used for reporting/loop logic)

    def signature(self) -> str:
        """One compact line per tool (keeps the prompt small for free-tier token limits)."""
        ps = ", ".join(f"{k}" for k in self.params)
        hints = "; ".join(f"{k.rstrip('?')}={v}" for k, v in self.params.items() if len(v) <= 40)
        return f"- {self.name}({ps}): {self.description}" + (f" [{hints}]" if hints else "")


TOOL_ALIASES = {"open": "open_url", "navigate": "open_url", "goto": "open_url", "visit": "open_url",
                "browse": "open_url", "type": "type_text", "fill": "type_text", "input": "type_text",
                "select": "select_option", "upload": "upload_file", "back": "go_back", "find": "read_page",
                "read": "read_page", "get_page": "observe", "note": "remember", "ask": "ask_human"}

ARG_ALIASES = {"element_id": "id", "element": "id", "index": "id", "value": "text", "secret": "secret_name",
               "file": "path", "file_path": "path", "href": "url", "link": "url"}


class ToolRegistry:
    def __init__(self) -> None:
        self.tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def subset(self, names: list[str]) -> "ToolRegistry":
        r = ToolRegistry()
        for n in names:
            r.register(self.tools[n])
        return r

    def describe(self) -> str:
        return "\n".join(t.signature() for t in self.tools.values())

    def execute(self, name: str, args: dict[str, Any] | None) -> ToolResult:
        tool = self.tools.get(name) or self.tools.get(TOOL_ALIASES.get(str(name).lower(), ""))
        if not tool:
            return ToolResult(False, f"Error: unknown tool '{name}'. Available: {', '.join(self.tools)}")
        args = dict(args or {})
        accepted = {k.rstrip("?") for k in tool.params}
        for alias, real in ARG_ALIASES.items():          # tolerate common naming slips from models
            if alias in args and real in accepted and real not in args:
                args[real] = args.pop(alias)
        missing = [k for k in tool.params if not k.endswith("?") and k not in args]
        if missing:
            return ToolResult(False, f"Error: {name} is missing required argument(s): {', '.join(missing)}")
        unknown = [k for k in args if k not in accepted]
        for k in unknown:
            args.pop(k)
        try:
            sig = inspect.signature(tool.fn)
            res = tool.fn(**{k: v for k, v in args.items() if k in sig.parameters})
        except Exception as e:  # tool failures become observations, never crash the run
            from ..guard import GuardError
            if isinstance(e, GuardError):
                return ToolResult(False, f"Blocked by guardrail: {e}")
            tb = traceback.format_exception_only(type(e), e)[-1].strip()
            return ToolResult(False, f"Error in {name}: {tb[:500]}")
        if unknown:
            res.output += f" (ignored unknown args: {', '.join(unknown)})"
        return res
