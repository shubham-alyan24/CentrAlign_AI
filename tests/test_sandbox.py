"""Layer 1: the fake company apps behave like real systems (validation, duplicates, outages, races)."""
import os

os.environ.setdefault("SANDBOX_TODAY", "2026-10-14")

import pytest
from fastapi.testclient import TestClient

from sandbox import ats_app, mail_app
from sandbox.store import RESUME_DIR, WORLD


@pytest.fixture(autouse=True)
def fresh():
    WORLD.reset()
    yield


def login(c: TestClient):
    r = c.post("/login", data={"username": "recruiter", "password": "Hire@2026", "next": "/dashboard"},
               follow_redirects=False)
    assert r.status_code == 303
    return c


def candidate_form(**over):
    d = dict(name="Ananya Sharma", email="ananya.sharma@mailbox.test", phone="+91 98765 43210", job="JOB-101",
             years="3", notice="30", skills="Python, FastAPI", source="Email application")
    d.update(over)
    return d


def resume_file():
    return {"resume": ("Ananya_Sharma_Resume.pdf", (RESUME_DIR / "Ananya_Sharma_Resume.pdf").read_bytes(), "application/pdf")}


def test_login_required_and_bad_password():
    c = TestClient(ats_app.app)
    r = c.get("/dashboard", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    r = c.post("/login", data={"username": "recruiter", "password": "nope"})
    assert r.status_code == 401 and "Invalid username or password" in r.text


def test_add_candidate_outage_then_validation_then_success():
    c = login(TestClient(ats_app.app))
    r = c.post("/candidates/new", data=candidate_form(), files=resume_file())
    assert r.status_code == 503 and "temporarily unavailable" in r.text          # chaos: first submit fails
    r = c.post("/candidates/new", data=candidate_form(phone="9876543210"), files=resume_file())
    assert r.status_code == 422 and "+91" in r.text                              # validation
    r = c.post("/candidates/new", data=candidate_form(), files=resume_file(), follow_redirects=False)
    assert r.status_code == 303
    new = WORLD.find_candidate_by_email("ananya.sharma@mailbox.test")
    assert new["stage"] == "Applied" and new["resume"].endswith("Ananya_Sharma_Resume.pdf")


def test_duplicate_detected():
    WORLD.chaos["enabled"] = False
    c = login(TestClient(ats_app.app))
    r = c.post("/candidates/new", data=candidate_form(name="Karan Malhotra", email="karan.malhotra@mailbox.test"),
               files=resume_file())
    assert r.status_code == 422 and "already exists: C-1001" in r.text


def test_shortlist_requires_complete_profile_and_reject_requires_reason():
    WORLD.chaos["enabled"] = False
    c = login(TestClient(ats_app.app))
    r = c.post("/candidates/C-1001/stage", data={"stage": "Shortlisted", "note": ""})
    assert r.status_code == 422 and "Cannot shortlist" in r.text
    r = c.post("/candidates/C-1003/stage", data={"stage": "Rejected", "note": ""})
    assert r.status_code == 422 and "reason" in r.text


def test_schedule_busy_race_and_success():
    c = login(TestClient(ats_app.app))
    WORLD.candidates["C-1003"]["stage"] = "Shortlisted"
    d1 = WORLD.days[0].isoformat()
    r = c.post("/candidates/C-1003/schedule", data={"interviewer": "INT-1", "day": d1, "time": "10:00"})
    assert r.status_code == 422 and "not available" in r.text                    # busy slot
    r = c.post("/candidates/C-1003/schedule", data={"interviewer": "INT-3", "day": d1, "time": "14:00"})
    assert "not on the interview panel" in r.text                                # wrong panel
    r = c.post("/candidates/C-1003/schedule", data={"interviewer": "INT-2", "day": d1, "time": "11:30"})
    assert r.status_code == 409 and "Conflict" in r.text                         # chaos: race
    r = c.post("/candidates/C-1003/schedule", data={"interviewer": "INT-2", "day": d1, "time": "14:00"},
               follow_redirects=False)
    assert r.status_code == 303
    assert WORLD.candidates["C-1003"]["stage"] == "Interview Scheduled"


def test_session_expires_once():
    c = login(TestClient(ats_app.app))
    locs = [c.get("/dashboard", follow_redirects=False).headers.get("location", "") for _ in range(12)]
    assert sum("expired=1" in l for l in locs) == 1                              # expired exactly once
    assert locs[-1].startswith("/login")                                         # stays logged out until re-login
    login(c)
    assert all(c.get("/dashboard").status_code == 200 for _ in range(15))        # never expires again


def test_reschedule_and_mail_send():
    WORLD.chaos["enabled"] = False
    c = login(TestClient(ats_app.app))
    d3 = WORLD.days[2].isoformat()
    r = c.post("/interviews/IV-501/reschedule", data={"day": d3, "time": "14:00", "reason": "exam"})
    assert r.status_code == 422                                                  # Arjun busy
    r = c.post("/interviews/IV-501/reschedule", data={"day": d3, "time": "15:30", "reason": "exam"},
               follow_redirects=False)
    assert r.status_code == 303 and WORLD.interviews["IV-501"]["date"] == d3

    m = TestClient(mail_app.app)
    assert "Request to reschedule" in m.get("/").text
    r = m.get("/attachment/1/Ananya_Sharma_Resume.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    r = m.post("/send", data={"to": "not-an-email", "cc": "", "subject": "", "body": ""})
    assert r.status_code == 422
    r = m.post("/send", data={"to": "priya.nair@mailbox.test", "cc": "arjun.mehta@acme.test",
                              "subject": "Interview confirmation", "body": "Hi"}, follow_redirects=False)
    assert r.status_code == 303 and WORLD.sent[0]["cc"] == ["arjun.mehta@acme.test"]
