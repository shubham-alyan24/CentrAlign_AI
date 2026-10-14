"""Browser tools: a real Chromium driven by Playwright.

Observation format: the page is turned into compact text - URL, title, visible text and
a numbered list of interactive elements. The model acts by element number
(click(id=7)), which is far more reliable than guessing CSS selectors or pixel
coordinates, and works on any website without site-specific code.
"""
from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import Error as PWError
from playwright.sync_api import sync_playwright

from ..guard import Guard, GuardError
from .base import Tool, ToolResult
from .human import HumanChannel

SNAPSHOT_JS = r"""
() => {
  document.querySelectorAll('[data-aid]').forEach(e => e.removeAttribute('data-aid'));
  const q = 'a[href], button, input, select, textarea, [role=button], [role=link]';
  const els = [...document.querySelectorAll(q)].filter(e => {
    if (e.type === 'hidden') return false;
    const r = e.getBoundingClientRect(), st = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
  });
  const out = els.map((e, i) => {
    const id = i + 1; e.setAttribute('data-aid', String(id));
    const tag = e.tagName.toLowerCase(), type = (e.type || '').toLowerCase();
    const it = {id, tag, type, text: (e.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 90),
      label: (e.labels && e.labels.length) ? e.labels[0].innerText.trim().replace(/\s+/g, ' ') : '',
      name: e.name || '', placeholder: e.placeholder || '', aria: e.getAttribute('aria-label') || '',
      href: tag === 'a' ? (e.getAttribute('href') || '') : '', disabled: !!e.disabled};
    if (tag === 'input' && type === 'file') it.value = e.files.length ? [...e.files].map(f => f.name).join(', ') : '';
    else if (tag === 'input' || tag === 'textarea') it.value = type === 'password' ? (e.value ? '********' : '') : (e.value || '').slice(0, 300);
    if (tag === 'input' && (type === 'submit' || type === 'button')) it.text = e.value || '';
    if (tag === 'select') { it.options = [...e.options].map(o => o.text.trim()).slice(0, 30);
                            it.value = e.selectedOptions[0] ? e.selectedOptions[0].text.trim() : ''; }
    return it;
  });
  return {url: location.href, title: document.title, text: document.body ? document.body.innerText : '', elements: out};
}
"""

ELEMENT_INFO_JS = r"""
(e) => {
  const tag = e.tagName.toLowerCase(), type = (e.type || '').toLowerCase();
  const label = ((e.innerText || e.value || '') + ' ' + (e.getAttribute('aria-label') || '')).trim().replace(/\s+/g, ' ');
  const isSubmit = (tag === 'button' && (type === 'submit' || type === '')) || (tag === 'input' && type === 'submit');
  const fields = {};
  if (e.form) for (const f of e.form.elements) {
    if (!f.name || f.type === 'hidden' || f.type === 'password') continue;
    if (f.type === 'file') fields[f.name] = f.files.length ? [...f.files].map(x => x.name).join(', ') : '';
    else if (f.tagName.toLowerCase() === 'select') fields[f.name] = f.selectedOptions[0] ? f.selectedOptions[0].text : '';
    else fields[f.name] = f.value;
  }
  const method = e.form ? (e.form.getAttribute('method') || 'get').toLowerCase() : '';
  return {tag, type, label, isSubmit: isSubmit && !!e.form, mutating: isSubmit && method === 'post', fields,
          href: e.getAttribute('href') || ''};
}
"""


def _describe(el: dict) -> str:
    name = el["label"] or el["aria"] or el["placeholder"] or el["text"] or el["name"]
    tag, typ = el["tag"], el["type"]
    dis = " (disabled)" if el.get("disabled") else ""
    if tag == "a":
        return f'[{el["id"]}] link "{el["text"] or name}" -> {el["href"]}'
    if tag == "button" or typ in ("submit", "button"):
        return f'[{el["id"]}] button "{el["text"] or name}"{dis}'
    if tag == "select":
        opts = " | ".join(el.get("options", []))
        return f'[{el["id"]}] select "{name}" selected="{el.get("value", "")}" options: {opts}{dis}'
    if typ == "file":
        return f'[{el["id"]}] file-upload "{name}" file="{el.get("value") or "none"}"'
    if typ in ("checkbox", "radio"):
        return f'[{el["id"]}] {typ} "{name}"'
    kind = "textarea" if tag == "textarea" else f"input[{typ or 'text'}]"
    return f'[{el["id"]}] {kind} "{name}" value="{el.get("value", "")}"{dis}'


class Browser:
    def __init__(self, guard: Guard, human: HumanChannel, workspace: Path, evidence_dir: Path,
                 headed: bool = False, slow_mo: int = 0, max_text: int = 3500):
        self.guard, self.human = guard, human
        self.workspace = Path(workspace).resolve()
        self.downloads = self.workspace / "downloads"
        self.downloads.mkdir(parents=True, exist_ok=True)
        self.evidence_dir = Path(evidence_dir)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.max_text = max_text
        self.headed, self.slow_mo = headed, slow_mo
        self._pending_downloads: list = []
        self._doc_status: int | None = None          # HTTP status of the latest main-frame document
        self.shot_counter = 0
        self.last_snapshot: dict | None = None
        self._last_url = ""
        self._recovered_note = ""
        self._pw = sync_playwright().start()
        self._launch()

    # ---------------------------------------------------- lifecycle / recovery
    def _launch(self) -> None:
        self._browser = self._pw.chromium.launch(headless=not self.headed, slow_mo=self.slow_mo)
        self.context = self._browser.new_context(accept_downloads=True, viewport={"width": 1280, "height": 860})
        self._open_page()

    def _open_page(self) -> None:
        self.page = self.context.new_page()
        self.page.set_default_timeout(10_000)
        self.page.on("download", lambda d: self._pending_downloads.append(d))
        self.page.on("response", self._on_response)

    def ensure_alive(self) -> None:
        """If the browser window was closed or crashed, reopen it at the last URL and tell the agent."""
        try:
            if self._browser.is_connected() and not self.page.is_closed():
                return
        except PWError:
            pass
        try:
            if self._browser.is_connected():
                self._open_page()                       # page closed, browser alive: keep cookies
            else:
                raise PWError("browser disconnected")
        except PWError:
            try:
                self._browser.close()
            except PWError:
                pass
            self._launch()                              # full relaunch (session cookies are lost)
        if self._last_url:
            try:
                self.page.goto(self._last_url, wait_until="load")
            except PWError:
                pass
        self._recovered_note = ("NOTE: the browser window was closed or crashed and has been reopened "
                                f"at {self._last_url or 'a blank page'}. Re-check the page before continuing.\n")

    @property
    def opened(self) -> bool:
        return bool(self._last_url)

    def close(self) -> None:
        try:
            self.context.close()
            self._browser.close()
        except PWError:
            pass
        finally:
            self._pw.stop()

    # ------------------------------------------------------------ internals
    def _on_response(self, resp) -> None:
        try:
            if resp.request.is_navigation_request() and resp.frame == self.page.main_frame:
                self._doc_status = resp.status
        except PWError:
            pass

    def _settle(self) -> None:
        try:
            self.page.wait_for_load_state("load", timeout=10_000)
        except PWError:
            pass
        self.page.wait_for_timeout(150)

    def _collect_downloads(self) -> list[str]:
        saved = []
        self._download_text = ""
        self._download_docs: dict[str, str] = {}
        while self._pending_downloads:
            d = self._pending_downloads.pop(0)
            target = self.downloads / Path(d.suggested_filename).name
            d.save_as(str(target))
            rel = f"workspace/downloads/{target.name}"
            saved.append(rel)
            extracted = self._extract(target)
            self._download_text += extracted
            self._download_docs[rel] = extracted.split("---\n", 1)[-1].strip()
        return saved

    @staticmethod
    def _extract(path: Path, limit: int = 3000) -> str:
        """Read a downloaded document right away so the agent sees its content without an extra step."""
        try:
            if path.suffix.lower() == ".pdf":
                from pypdf import PdfReader
                text = "\n".join((pg.extract_text() or "") for pg in PdfReader(str(path)).pages)
            elif path.suffix.lower() in (".txt", ".csv", ".md", ".json"):
                text = path.read_text(encoding="utf-8", errors="replace")
            else:
                return ""
        except Exception as e:  # pragma: no cover
            return f"\n--- workspace/downloads/{path.name}: could not read ({e}) ---\n"
        return f"\n--- content of workspace/downloads/{path.name} ---\n{text[:limit]}\n"

    def snapshot(self) -> str:
        self.ensure_alive()
        snap = self.page.evaluate(SNAPSHOT_JS)
        if snap["url"].startswith("http"):
            self._last_url = snap["url"]
        self.last_snapshot = snap
        text = re.sub(r"\n{3,}", "\n\n", snap["text"]).strip()
        truncated = len(text) > self.max_text
        text = text[: self.max_text] + ("\n...[page text truncated - use read_page for the rest]" if truncated else "")
        els = "\n".join(_describe(e) for e in snap["elements"])
        obs = f"URL: {snap['url']}\nTITLE: {snap['title']}\n--- PAGE TEXT ---\n{text}\n--- INTERACTIVE ELEMENTS ---\n{els or '(none)'}"
        note, self._recovered_note = self._recovered_note, ""
        return self.guard.redact(note + obs)

    def _locator(self, id):
        try:
            n = int(str(id).strip("[] "))
        except ValueError:
            raise ValueError(f"element id must be a number from the INTERACTIVE ELEMENTS list, got {id!r}")
        self.ensure_alive()
        loc = self.page.locator(f'[data-aid="{n}"]')
        if loc.count() == 0:
            raise ValueError(f"element [{n}] is not on the current page (the page changed). Use the latest observation.")
        return loc

    def screenshot_file(self, label: str) -> str:
        self.ensure_alive()
        self.shot_counter += 1
        slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40] or "page"
        path = self.evidence_dir / f"{self.shot_counter:03d}_{slug}.jpg"
        self.page.screenshot(path=str(path), type="jpeg", quality=60, full_page=True)
        return str(path)

    def _after(self, msg: str, before_url: str, shot_label: str | None) -> ToolResult:
        self._settle()
        dls = self._collect_downloads()
        if dls:
            msg += f" Downloaded file(s): {', '.join(dls)}"
        changed = self.page.url != before_url
        code, self._doc_status = self._doc_status, None
        try:
            alerts = " | ".join(t.strip() for t in self.page.locator("[role=alert]").all_inner_texts() if t.strip())
        except PWError:
            alerts = ""
        ok = not (code and code >= 400)
        if not ok:
            msg += f" BUT the server rejected it (HTTP {code})."
        if alerts:
            msg += f" Page alert: {' '.join(alerts.split())[:400]}"
        ev = [self.screenshot_file(shot_label)] if shot_label else []
        data = {"download_text": self._download_text, "download_docs": dict(self._download_docs)} \
            if dls and getattr(self, "_download_text", "") else {}
        return ToolResult(ok, msg + f" Now at {self.page.url}", observation=self.snapshot(),
                          evidence=ev, page_changed=changed, data=data)

    # ---------------------------------------------------------------- tools
    def open_url(self, url: str) -> ToolResult:
        self.guard.check_url(url)
        self.ensure_alive()
        before = self.page.url
        self._doc_status = None
        try:
            resp = self.page.goto(url, wait_until="load")
            code = resp.status if resp else "?"
            return self._after(f"Opened {url} (HTTP {code}).", before, f"open {url.split('/', 3)[-1]}")
        except PWError as e:
            if "Download is starting" in str(e):
                self.page.wait_for_timeout(500)
                return self._after(f"{url} is a file download.", before, None)
            raise

    def click(self, id: int) -> ToolResult:
        loc = self._locator(id)
        info = loc.evaluate(ELEMENT_INFO_JS)
        if info["href"] and not info["href"].startswith(("javascript:", "#")):
            from urllib.parse import urljoin
            self.guard.check_url(urljoin(self.page.url, info["href"]))
        verdict = self.guard.check_click(self.page.url, info["label"], info["fields"], info["mutating"])
        if verdict.decision == "forbid":
            raise GuardError(f"'{info['label']}' is forbidden: {verdict.reason}")
        approval_note = ""
        if verdict.decision == "approve":
            details = "\n".join(f"  {k:>8}: {v}" for k, v in info["fields"].items()) or "  (no form fields)"
            ok, comment = self.human.approve(f"Agent wants to press '{info['label']}' on {self.page.url}\n"
                                             f"  Why approval is needed: {verdict.reason}", details)
            if not ok:
                return ToolResult(False, f"Human DID NOT approve pressing '{info['label']}'. Feedback: {comment}. "
                                         f"Nothing was submitted. Revise according to the feedback or ask the human.")
            approval_note = " (human approved)"
        before = self.page.url
        self._doc_status = None
        loc.click()
        return self._after(f"Clicked [{id}] '{info['label'][:60]}'{approval_note}.", before,
                           f"click {info['label']}" if info["isSubmit"] or info["tag"] == "a" else None)

    def type_text(self, id: int, text: str, press_enter: bool = False) -> ToolResult:
        m = re.fullmatch(r"\s*(?:\{\{\s*(\w+)\s*\}\}|\$\{?(\w+)\}?|(\w+))\s*", str(text))
        name = m and (m.group(1) or m.group(2) or m.group(3))
        if name and name in self.guard.secret_names:      # model wrote a secret's NAME/placeholder: fill the secret
            return self.fill_secret(id, name)
        loc = self._locator(id)
        loc.fill(str(text))
        if press_enter:
            before = self.page.url
            loc.press("Enter")
            return self._after(f"Typed into [{id}] and pressed Enter.", before, "submit")
        return ToolResult(True, f"Typed into [{id}]: {str(text)[:80]!r}")

    def select_option(self, id: int, option: str) -> ToolResult:
        loc = self._locator(id)
        opts = loc.evaluate("e => [...e.options].map(o => ({t: o.text.trim(), v: o.value}))")
        want = str(option).strip().lower()
        match = next((o for o in opts if o["t"].lower() == want or o["v"].lower() == want), None) \
            or next((o for o in opts if want and want in o["t"].lower()), None)
        if not match:
            return ToolResult(False, f"Option {option!r} not found in [{id}]. Options: {[o['t'] for o in opts]}")
        loc.select_option(value=match["v"])
        return ToolResult(True, f"Selected '{match['t']}' in [{id}].")

    def upload_file(self, id: int, path: str) -> ToolResult:
        norm = str(path).replace("\\", "/").lstrip("./")
        p = (self.workspace / norm[len("workspace/"):]).resolve() if norm.startswith("workspace/") \
            else Path(path).resolve()
        if self.workspace not in p.parents:
            raise GuardError("Only files inside workspace/ can be uploaded.")
        if not p.exists():
            return ToolResult(False, f"File not found: {path}. Check list_files('workspace/downloads').")
        self._locator(id).set_input_files(str(p))
        return ToolResult(True, f"Attached {p.name} to [{id}].")

    def fill_secret(self, id: int, secret_name: str) -> ToolResult:
        value = self.guard.secret(secret_name)
        self._locator(id).fill(value)
        return ToolResult(True, f"Filled [{id}] with secret {secret_name} (value hidden).")

    def press_key(self, key: str) -> ToolResult:
        self.ensure_alive()
        before = self.page.url
        self.page.keyboard.press(key)
        return self._after(f"Pressed {key}.", before, None)

    def go_back(self) -> ToolResult:
        self.ensure_alive()
        before = self.page.url
        self.page.go_back()
        return self._after("Went back.", before, None)

    def read_page(self, start: int = 0) -> ToolResult:
        text = self.page.inner_text("body")
        chunk = text[int(start): int(start) + 8000]
        more = len(text) > int(start) + 8000
        return ToolResult(True, f"Page text from char {start}" + (" (more available)" if more else ""),
                          observation=self.guard.redact(chunk))

    def observe(self) -> ToolResult:
        return ToolResult(True, f"Current page: {self.page.url}. (Not needed: CURRENT PAGE in the prompt is always "
                                f"the live page - act on it directly.)", observation=self.snapshot())

    def screenshot(self, label: str = "evidence") -> ToolResult:
        p = self.screenshot_file(label)
        return ToolResult(True, f"Saved screenshot {Path(p).name}", evidence=[p])

    # ---------------------------------------------------------------- registry
    def tools(self) -> list[Tool]:
        return [
            Tool("open_url", "Open a URL of an approved company system in the browser.",
                 {"url": "absolute URL"}, self.open_url),
            Tool("click", "Click an element (link, button, checkbox) by its [id] from INTERACTIVE ELEMENTS. "
                          "Submitting some forms pauses for human approval automatically.",
                 {"id": "element number"}, self.click, mutates=True),
            Tool("type_text", "Replace the content of an input/textarea with text.",
                 {"id": "element number", "text": "text to enter", "press_enter?": "true to press Enter after"},
                 self.type_text),
            Tool("select_option", "Choose an option in a <select> by its visible text (or value).",
                 {"id": "element number", "option": "option text"}, self.select_option),
            Tool("upload_file", "Attach a local file (under workspace/) to a file-upload input.",
                 {"id": "element number", "path": "e.g. workspace/downloads/x.pdf"}, self.upload_file),
            Tool("fill_secret", "Fill an input with a stored secret (e.g. a password) without seeing it.",
                 {"id": "element number", "secret_name": "e.g. ATS_PASSWORD"}, self.fill_secret),
            Tool("press_key", "Press a keyboard key on the page (e.g. Enter, Escape).", {"key": "key name"},
                 self.press_key),
            Tool("go_back", "Browser back button.", {}, self.go_back),
            Tool("read_page", "Read the full text of the current page (when the observation was truncated).",
                 {"start?": "character offset, default 0"}, self.read_page),
            Tool("observe", "Rarely needed: CURRENT PAGE is already live in every prompt.", {}, self.observe),
            Tool("screenshot", "Save a screenshot of the current page as evidence.",
                 {"label": "short description"}, self.screenshot),
        ]
