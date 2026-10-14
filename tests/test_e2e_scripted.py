"""Layer 5: the full harness (understand -> act -> recover -> approve -> verify -> report -> learn) runs
end-to-end against the sandbox WITH failures injected, driven by a scripted policy instead of an LLM."""
import json

from agent.config import Settings
from agent.llm import ScriptedLLM
from agent.loop import Operator
from agent.tools.human import ScriptedHuman
from sandbox.store import WORLD
from tests.scripted_policy import ProcessApplicationsPolicy


def test_process_applications_end_to_end(sandbox_live, tmp_path):
    sandbox_live.reset(chaos=True)
    s = Settings(provider="gemini", api_key="unused")
    s.runs_dir, s.workspace_dir, s.memory_file = tmp_path / "runs", tmp_path / "workspace", tmp_path / "lessons.json"
    s.max_steps = 120
    policy = ProcessApplicationsPolicy()
    human = ScriptedHuman(approve=True, verbose=False)
    events = []
    state = Operator(s, ScriptedLLM(policy), human, on_event=lambda k, d: events.append(k)).run("process applications")

    assert state.status == "completed", (state.status, state.error)
    snap = WORLD.snapshot()
    by_email = {c["email"]: c for c in snap["candidates"].values()}
    # no duplicates, correct decisions, resumes attached
    assert sum(c["email"] == "karan.malhotra@mailbox.test" for c in snap["candidates"].values()) == 1
    assert by_email["rohit.verma@mailbox.test"]["stage"] == "Rejected"
    assert by_email["rohit.verma@mailbox.test"]["phone"] == "+91 91234 56780"            # validation fixed
    for e in ["ananya.sharma@mailbox.test", "karan.malhotra@mailbox.test"]:
        assert by_email[e]["stage"] == "Interview Scheduled" and by_email[e]["resume"]
    assert by_email["karan.malhotra@mailbox.test"]["source"] == "Referral"
    # interviews on the right panel, in free slots, no double booking
    ivs = [iv for iv in snap["interviews"].values() if iv["id"] != "IV-501"]
    assert len(ivs) == 2 and all(iv["interviewer"] in ("INT-1", "INT-2") for iv in ivs)
    assert len({(iv["interviewer"], iv["date"], iv["time"]) for iv in ivs}) == 2
    # chaos actually happened and was recovered from
    kinds = [e["kind"] for e in snap["events"]]
    assert "ats.outage" in kinds and "ats.session_expired" in kinds and "ats.slot_race" in kinds
    assert "retry-503" in policy.log and "fixed-phone" in policy.log and "slot-conflict" in policy.log
    # external emails required (and got) approval; both have the interviewer in Cc
    assert len(snap["sent"]) == 2 and all(m["cc"] and m["cc"][0].endswith("@acme.test") for m in snap["sent"])
    assert sum(h["kind"] == "approval" for h in human.log) == 2
    # verification, report, lessons
    assert state.verifications and state.verifications[-1]["verdict"] == "pass"
    run_dir = s.runs_dir / state.run_id
    assert (run_dir / "report.html").exists() and json.loads((run_dir / "trace.json").read_text())["status"] == "completed"
    assert list((run_dir / "evidence").glob("*.jpg"))
    assert state.new_lessons and json.loads(s.memory_file.read_text())
    assert "verify_done" in events
    assert any(k.startswith("document workspace/downloads/Rohit_Verma_CV.pdf") for k in state.notes)


def test_denied_approval_is_not_sent(sandbox_live, tmp_path):
    """If the human rejects the email, nothing is sent and the agent sees the rejection."""
    sandbox_live.reset(chaos=False)
    from agent.guard import Guard
    from agent.config import ROOT
    from agent.tools.browser import Browser
    human = ScriptedHuman(approve=False, verbose=False)
    b = Browser(Guard(ROOT / "company" / "permissions.json"), human, tmp_path / "workspace", tmp_path / "ev")
    try:
        b.open_url("http://127.0.0.1:8001/compose")
        ids = {e["label"]: e["id"] for e in b.last_snapshot["elements"] if e["label"]}
        b.type_text(ids["To"], "ananya.sharma@mailbox.test")
        b.type_text(ids["Subject"], "x")
        b.type_text(ids["Message"], "y")
        send = next(e["id"] for e in b.last_snapshot["elements"] if e["text"] == "Send")
        r = b.click(send)
        assert not r.ok and WORLD.sent == []
    finally:
        b.close()
