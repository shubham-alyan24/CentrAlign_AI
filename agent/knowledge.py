"""Company knowledge base: plain markdown files in company/, listed in company/INDEX.md.

Kept deliberately simple (a handful of documents). The interface - index(), resolve(), read() -
is what the agent depends on, so it can be swapped for search/RAG over a large wiki later."""
from __future__ import annotations

from pathlib import Path


class Knowledge:
    def __init__(self, folder: Path):
        self.folder = Path(folder)

    def index(self) -> str:
        return (self.folder / "INDEX.md").read_text(encoding="utf-8")

    def available(self) -> list[str]:
        return sorted(p.name for p in self.folder.glob("*.md") if p.name != "INDEX.md")

    def resolve(self, names: list) -> list[str]:
        avail = self.available()
        out = []
        for n in names or []:
            n = Path(str(n)).name
            if n in avail and n not in out:
                out.append(n)
        if "systems.md" in avail and "systems.md" not in out:   # always know where the systems are
            out.insert(0, "systems.md")
        return out

    def read(self, name: str) -> str:
        return (self.folder / Path(name).name).read_text(encoding="utf-8")
