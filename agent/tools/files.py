"""File tools, sandboxed to the project's workspace/ (read-write) and company/ (read-only)."""
from __future__ import annotations

from pathlib import Path

from ..guard import GuardError
from .base import Tool, ToolResult


class Files:
    def __init__(self, root: Path, workspace: Path, company: Path, max_chars: int = 6000):
        self.root = Path(root).resolve()
        self.workspace = Path(workspace).resolve()
        self.company = Path(company).resolve()
        self.max_chars = max_chars

    def _resolve(self, path: str, write: bool = False) -> Path:
        """Logical paths: 'workspace/...' and 'company/...' map to the configured folders."""
        norm = str(path).replace("\\", "/").lstrip("./")
        if norm == "workspace" or norm.startswith("workspace/"):
            p = (self.workspace / norm[len("workspace"):].lstrip("/")).resolve()
        elif norm == "company" or norm.startswith("company/"):
            p = (self.company / norm[len("company"):].lstrip("/")).resolve()
        else:
            p = Path(path)
            p = (p if p.is_absolute() else self.root / p).resolve()
        roots = [self.workspace] if write else [self.workspace, self.company]
        if not any(p == r or r in p.parents for r in roots):
            raise GuardError(f"'{path}' is outside the allowed folders "
                             f"({'workspace/' if write else 'workspace/ and company/'}).")
        return p

    def _rel(self, p: Path) -> str:
        for base, name in ((self.workspace, "workspace"), (self.company, "company")):
            if p == base or base in p.parents:
                return (name + "/" + str(p.relative_to(base)).replace("\\", "/")).rstrip("/.")
        return str(p)

    def list_files(self, path: str = "workspace") -> ToolResult:
        p = self._resolve(path)
        if not p.exists():
            return ToolResult(False, f"{path} does not exist.")
        items = sorted(x for x in p.rglob("*") if x.is_file())
        listing = "\n".join(f"{self._rel(x)}  ({x.stat().st_size} bytes)" for x in items[:200]) or "(empty)"
        return ToolResult(True, f"{len(items)} file(s) under {path}", observation=listing)

    def read_file(self, path: str) -> ToolResult:
        p = self._resolve(path)
        if not p.exists():
            return ToolResult(False, f"File not found: {path}")
        if p.suffix.lower() == ".pdf":
            from pypdf import PdfReader
            text = "\n".join((pg.extract_text() or "") for pg in PdfReader(str(p)).pages)
            if not text.strip():
                return ToolResult(False, f"{path} has no extractable text (scanned PDF?). Ask a human.")
        else:
            text = p.read_text(encoding="utf-8", errors="replace")
        cut = len(text) > self.max_chars
        return ToolResult(True, f"Read {self._rel(p)} ({len(text)} chars{', truncated' if cut else ''})",
                          observation=f"--- {self._rel(p)} ---\n{text[: self.max_chars]}")

    def write_file(self, path: str, content: str) -> ToolResult:
        p = self._resolve(path, write=True)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(content), encoding="utf-8")
        return ToolResult(True, f"Wrote {self._rel(p)} ({len(str(content))} chars)", evidence=[str(p)])

    def read_doc(self, name: str) -> ToolResult:
        name = Path(name).name
        return self.read_file(str(self.company / name))

    def tools(self) -> list[Tool]:
        return [
            Tool("list_files", "List files in a folder (workspace/ or company/).", {"path?": "folder, default workspace"},
                 self.list_files),
            Tool("read_file", "Read a text or PDF file from workspace/ or company/.", {"path": "file path"},
                 self.read_file),
            Tool("write_file", "Write a text file under workspace/ (e.g. a report or CSV).",
                 {"path": "workspace/...", "content": "file content"}, self.write_file, mutates=True),
            Tool("read_doc", "Read a company knowledge-base document by file name (see the index).",
                 {"name": "e.g. hiring-policy.md"}, self.read_doc),
        ]
