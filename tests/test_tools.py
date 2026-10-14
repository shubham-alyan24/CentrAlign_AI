"""Layer 2: tools work against the live sandbox with NO language model involved."""
from pathlib import Path

import pytest

from agent.config import ROOT
from agent.guard import Guard
from agent.tools.base import ToolRegistry
from agent.tools.browser import Browser
from agent.tools.files import Files
from agent.tools.human import ScriptedHuman
from sandbox.store import WORLD


def el(b: Browser, text: str, tag: str | None = None) -> int:
    for e in b.last_snapshot["elements"]:
        hay = " ".join([e["text"], e["label"], e.get("aria", ""), e.get("placeholder", "")]).lower()
        if text.lower() in hay and (tag is None or e["tag"] == tag):
            return e["id"]
    raise AssertionError(f"no element with {text!r}")


@pytest.fixture()
def kit(sandbox_live, tmp_path):
    sandbox_live.reset()
    human = ScriptedHuman(approve=True)
    guard = Guard(ROOT / "company" / "permissions.json")
    ws = tmp_path / "workspace"
    b = Browser(guard, human, ws, tmp_path / "shots")
    f = Files(tmp_path, ws, ROOT / "company")
    reg = ToolRegistry()
    for t in b.tools() + f.tools():
        reg.register(t)
    yield b, reg, human
    b.close()


def test_download_resume_and_read_pdf(kit):
    b, reg, _ = kit
    r = reg.execute("open_url", {"url": "http://127.0.0.1:8001/"})
    assert r.ok and "Inbox" in r.observation
    r = reg.execute("click", {"id": el(b, "Application - Backend Engineer - Ananya", "a")})
    assert r.ok and "Ananya_Sharma_Resume.pdf" in r.observation
    r = reg.execute("click", {"id": el(b, "Download Ananya", "a")})
    assert r.ok and "workspace/downloads/Ananya_Sharma_Resume.pdf" in r.output, r.output
    r = reg.execute("read_file", {"path": "workspace/downloads/Ananya_Sharma_Resume.pdf"})
    assert r.ok and "Notice period: 30 days" in r.observation


def test_login_with_secrets_and_password_never_shown(kit):
    b, reg, _ = kit
    r = reg.execute("open_url", {"url": "http://127.0.0.1:8002/dashboard"})
    assert "Sign in" in r.observation
    reg.execute("fill_secret", {"id": el(b, "Username"), "secret_name": "ATS_USERNAME"})
    reg.execute("fill_secret", {"id": el(b, "Password"), "secret_name": "ATS_PASSWORD"})
    obs = reg.execute("observe", {}).observation
    assert "Hire@2026" not in obs and "********" in obs
    r = reg.execute("click", {"id": el(b, "Sign in", "button")})
    assert r.ok and "Dashboard" in r.observation


def test_guardrails(kit):
    b, reg, human = kit
    r = reg.execute("open_url", {"url": "https://example.com"})
    assert not r.ok and "not an approved company system" in r.output
    r = reg.execute("open_url", {"url": "http://127.0.0.1:8002/__admin/state"})
    assert not r.ok and "reserved" in r.output
    r = reg.execute("read_file", {"path": "/etc/passwd"})
    assert not r.ok and "outside the allowed folders" in r.output
    r = reg.execute("write_file", {"path": "company/hiring-policy.md", "content": "x"})
    assert not r.ok
    # forbidden: offer button
    reg.execute("open_url", {"url": "http://127.0.0.1:8002/login"})
    reg.execute("fill_secret", {"id": el(b, "Username"), "secret_name": "ATS_USERNAME"})
    reg.execute("fill_secret", {"id": el(b, "Password"), "secret_name": "ATS_PASSWORD"})
    reg.execute("click", {"id": el(b, "Sign in", "button")})
    reg.execute("open_url", {"url": "http://127.0.0.1:8002/candidates/C-1001"})
    r = reg.execute("click", {"id": el(b, "Send offer letter", "button")})
    assert not r.ok and "forbidden" in r.output
    assert WORLD.candidates["C-1001"]["stage"] == "Applied"


def test_external_email_needs_approval_internal_does_not(kit):
    b, reg, human = kit
    human.approve_all = False
    reg.execute("open_url", {"url": "http://127.0.0.1:8001/compose"})
    reg.execute("type_text", {"id": el(b, "To"), "text": "priya.nair@mailbox.test"})
    reg.execute("type_text", {"id": el(b, "Subject"), "text": "Interview confirmation"})
    reg.execute("type_text", {"id": el(b, "Message"), "text": "Hi Priya"})
    r = reg.execute("click", {"id": el(b, "Send", "button")})
    assert not r.ok and "DID NOT approve" in r.output and len(WORLD.sent) == 0
    assert "priya.nair@mailbox.test" in human.log[-1]["prompt"]          # human saw the real field values
    human.approve_all = True
    r = reg.execute("click", {"id": el(b, "Send", "button")})
    assert r.ok and len(WORLD.sent) == 1 and "human approved" in r.output
    n = len(human.log)
    reg.execute("open_url", {"url": "http://127.0.0.1:8001/compose"})
    reg.execute("type_text", {"id": el(b, "To"), "text": "ritu.sinha@acme.test"})
    reg.execute("type_text", {"id": el(b, "Subject"), "text": "Escalation"})
    reg.execute("type_text", {"id": el(b, "Message"), "text": "FYI"})
    r = reg.execute("click", {"id": el(b, "Send", "button")})
    assert r.ok and len(human.log) == n and len(WORLD.sent) == 2         # internal: no approval needed


def test_upload_and_select_and_stale_ids(kit):
    b, reg, _ = kit
    WORLD.chaos["enabled"] = False
    reg.execute("open_url", {"url": "http://127.0.0.1:8001/message/2"})
    reg.execute("click", {"id": el(b, "Download Rohit", "a")})
    reg.execute("open_url", {"url": "http://127.0.0.1:8002/login"})
    reg.execute("fill_secret", {"id": el(b, "Username"), "secret_name": "ATS_USERNAME"})
    reg.execute("fill_secret", {"id": el(b, "Password"), "secret_name": "ATS_PASSWORD"})
    reg.execute("click", {"id": el(b, "Sign in", "button")})
    reg.execute("open_url", {"url": "http://127.0.0.1:8002/candidates/new"})
    r = reg.execute("select_option", {"id": el(b, "Job", "select"), "option": "Backend Engineer"})
    assert r.ok and "JOB-101" in r.output
    r = reg.execute("select_option", {"id": el(b, "Job", "select"), "option": "Astronaut"})
    assert not r.ok and "Options" in r.output
    r = reg.execute("upload_file", {"id": el(b, "Resume"), "path": "workspace/downloads/Rohit_Verma_CV.pdf"})
    assert r.ok
    obs = reg.execute("observe", {}).observation
    assert 'file="Rohit_Verma_CV.pdf"' in obs
    r = reg.execute("click", {"id": 999})
    assert not r.ok and "not on the current page" in r.output


def test_recovers_when_window_closed_or_browser_crashes(kit):
    b, reg, _ = kit
    reg.execute("open_url", {"url": "http://127.0.0.1:8001/message/1"})
    b.page.close()                                    # user closes the browser window
    obs = b.snapshot()
    assert "reopened" in obs and "/message/1" in obs
    assert reg.execute("click", {"id": el(b, "Download Ananya", "a")}).ok
    b._browser.close()                                # browser process dies
    obs = b.snapshot()
    assert "reopened" in obs and "Ananya" in obs


def test_secret_placeholder_in_type_text_is_filled_as_secret(kit):
    b, reg, _ = kit
    reg.execute("open_url", {"url": "http://127.0.0.1:8002/login"})
    r = reg.execute("type_text", {"id": el(b, "Username"), "text": "{{ATS_USERNAME}}"})
    assert r.ok and "secret ATS_USERNAME" in r.output
    r = reg.execute("type_text", {"id": el(b, "Username"), "text": "ATS_USERNAME"})
    assert r.ok and "secret ATS_USERNAME" in r.output
    reg.execute("fill_secret", {"id": el(b, "Password"), "secret_name": "ATS_PASSWORD"})
    assert "Dashboard" in reg.execute("click", {"id": el(b, "Sign in", "button")}).observation


def test_download_is_read_automatically(kit):
    b, reg, _ = kit
    reg.execute("open_url", {"url": "http://127.0.0.1:8001/message/2"})
    r = reg.execute("click", {"id": el(b, "Download Rohit", "a")})
    assert r.ok and "Total professional experience: 1.5 years" in r.data["download_text"]


def test_downloaded_documents_are_returned_for_working_memory(kit):
    b, reg, _ = kit
    reg.execute("open_url", {"url": "http://127.0.0.1:8001/message/2"})
    r = reg.execute("click", {"id": el(b, "Download Rohit", "a")})
    docs = r.data["download_docs"]
    assert "workspace/downloads/Rohit_Verma_CV.pdf" in docs
    assert "rohit.verma@mailbox.test" in docs["workspace/downloads/Rohit_Verma_CV.pdf"]
