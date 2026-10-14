"""In-memory "company world" shared by the two sandbox apps (Acme Mail + HireDesk ATS).

Everything is rebuilt from the seed on reset(), so every agent run starts from the
same state. Dates are generated relative to "today" so the scenario stays valid
whenever it is run (override with SANDBOX_TODAY=YYYY-MM-DD for deterministic tests).

Chaos (SANDBOX_CHAOS=1, default on) injects realistic failures that the agent must
recover from:
  * the HireDesk session expires once, mid-run
  * the first "Add candidate" submit returns HTTP 503
  * the first attempt to book an interview slot loses a race (slot gets taken)
"""
from __future__ import annotations

import copy
import os
import re
import secrets
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

SEED_DIR = Path(__file__).parent / "seed"
RESUME_DIR = SEED_DIR / "resumes"
UPLOAD_DIR = Path(__file__).parent / "_uploads"

TIMES = ["10:00", "11:30", "14:00", "15:30"]
STAGES = ["Applied", "Screening", "Shortlisted", "Interview Scheduled", "Rejected", "Offer"]

ATS_USERNAME_DEFAULT = "recruiter"
ATS_PASSWORD_DEFAULT = "Hire@2026"


def today() -> date:
    forced = os.getenv("SANDBOX_TODAY")
    return date.fromisoformat(forced) if forced else date.today()


def working_days(start: date, n: int) -> list[date]:
    """Next n working days strictly after `start`."""
    out, d = [], start
    while len(out) < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return out


def fmt_day(d: date) -> str:
    return d.strftime("%a %d %b %Y")


class World:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.reset()

    # ------------------------------------------------------------------ seed
    def reset(self, chaos: bool | None = None) -> None:
        with self.lock:
            t = today()
            self.today = t
            self.days = working_days(t, 5)
            d1, d2, d3, d4, d5 = self.days

            def ago(n: int, hm: str) -> str:
                return f"{(t - timedelta(days=n)).isoformat()} {hm}"

            self.chaos = {
                "enabled": (os.getenv("SANDBOX_CHAOS", "1") != "0") if chaos is None else chaos,
                "session_expire_after": 9,   # authenticated page loads before forced expiry
                "session_expired_once": False,
                "add_candidate_503_once": False,
                "slot_race_once": False,
            }
            self.events: list[dict] = []   # audit log of every state change (for tests / report)

            # ---------------- HireDesk ATS ----------------
            self.interviewers = {
                "INT-1": {"id": "INT-1", "name": "Vikram Rao", "email": "vikram.rao@acme.test", "jobs": ["JOB-101"]},
                "INT-2": {"id": "INT-2", "name": "Sneha Kapoor", "email": "sneha.kapoor@acme.test", "jobs": ["JOB-101"]},
                "INT-3": {"id": "INT-3", "name": "Arjun Mehta", "email": "arjun.mehta@acme.test", "jobs": ["JOB-102"]},
                "INT-4": {"id": "INT-4", "name": "Neha Gupta", "email": "neha.gupta@acme.test", "jobs": ["JOB-103"]},
            }
            self.jobs = {
                "JOB-101": {"id": "JOB-101", "title": "Backend Engineer", "team": "Platform", "status": "Open", "panel": ["INT-1", "INT-2"]},
                "JOB-102": {"id": "JOB-102", "title": "Data Analyst", "team": "Analytics", "status": "Open", "panel": ["INT-3"]},
                "JOB-103": {"id": "JOB-103", "title": "Sales Associate", "team": "Sales", "status": "Open", "panel": ["INT-4"]},
            }
            # busy blocks: (interviewer, iso-date, time) -> reason
            self.busy = {
                ("INT-1", d1.isoformat(), "10:00"): "Sprint planning",
                ("INT-1", d1.isoformat(), "11:30"): "Architecture review",
                ("INT-2", d1.isoformat(), "10:00"): "1:1 with manager",
                ("INT-2", d1.isoformat(), "15:30"): "On-call handover",
                ("INT-3", d3.isoformat(), "14:00"): "Quarterly review",
                ("INT-3", d4.isoformat(), "14:00"): "Team offsite",
            }
            self.candidates = {
                "C-1001": self._cand("C-1001", "Karan Malhotra", "karan.malhotra@mailbox.test", "+91 98110 22334",
                                      "JOB-101", "Applied", "Referral", years=None, notice=None, skills="",
                                      notes=[("HireDesk", ago(6, "10:12"), "Referred by Vikram Rao (Platform). Resume pending.")]),
                "C-1002": self._cand("C-1002", "Priya Nair", "priya.nair@mailbox.test", "+91 98220 44556",
                                      "JOB-102", "Interview Scheduled", "Job board", years=3, notice=30,
                                      skills="SQL, Python, Tableau, Excel",
                                      notes=[("recruiter", ago(5, "15:40"), "Strong SQL test. Moved to interview.")]),
                "C-1003": self._cand("C-1003", "Sameer Khan", "sameer.khan@mailbox.test", "+91 99887 66554",
                                      "JOB-101", "Applied", "Job board", years=4, notice=30, skills="Go, Python, Kubernetes"),
                "C-1004": self._cand("C-1004", "Sameer Joshi", "sameer.joshi@mailbox.test", "+91 97654 32109",
                                      "JOB-103", "Applied", "Job board", years=2, notice=15, skills="B2B sales, CRM"),
            }
            self.next_cand = 1005
            self.interviews = {
                "IV-501": {"id": "IV-501", "candidate": "C-1002", "interviewer": "INT-3", "date": d2.isoformat(),
                           "time": "10:00", "round": "Round 1 - Technical", "status": "Scheduled"},
            }
            self.next_iv = 502

            # ---------------- Acme Mail ----------------
            self.mail_owner = "hr@acme.test"
            self.inbox = [
                self._mail(1, "Ananya Sharma", "ananya.sharma@mailbox.test", ago(1, "09:14"),
                           "Application - Backend Engineer - Ananya Sharma",
                           "Hello Hiring Team,\n\nI would like to apply for the Backend Engineer position at Acme. "
                           "I have been building payment APIs in Python for the last few years. My resume is attached "
                           "with full details.\n\nThanks and regards,\nAnanya Sharma\n+91 98765 43210",
                           ["Ananya_Sharma_Resume.pdf"]),
                self._mail(2, "Rohit Verma", "rohit.verma@mailbox.test", ago(1, "11:52"),
                           "Backend Engineer application",
                           "Hi,\n\nPlease find my CV attached for the Backend Engineer opening. I am excited about "
                           "the role and can join quickly.\n\nRohit Verma\nPhone: 9123456780",
                           ["Rohit_Verma_CV.pdf"]),
                self._mail(3, "Karan Malhotra", "karan.malhotra@mailbox.test", ago(2, "18:05"),
                           "Applying for Backend Engineer (referred by Vikram)",
                           "Dear Acme HR,\n\nVikram Rao referred me for the Backend Engineer role. Sharing my resume "
                           "as requested.\n\nBest,\nKaran Malhotra\n+91 98110 22334",
                           ["Karan_Malhotra_Resume.pdf"]),
                self._mail(4, "Isha Bansal", "isha.bansal@mailbox.test", ago(2, "12:30"),
                           "Application for Sales Associate",
                           "Hello,\n\nI am applying for the Sales Associate role. Resume attached.\n\nIsha",
                           ["Isha_Bansal_Resume.pdf"]),
                self._mail(5, "Priya Nair", "priya.nair@mailbox.test", ago(0, "08:41"),
                           "Request to reschedule my interview",
                           f"Hi,\n\nThank you for scheduling my Data Analyst interview on {fmt_day(d2)} at 10:00. "
                           f"Unfortunately I have a university exam that morning. Could we please move it to an "
                           f"afternoon slot later that week? Any afternoon from {fmt_day(d3)} onwards works for me.\n\n"
                           "Apologies for the trouble,\nPriya Nair",
                           []),
                self._mail(6, "Acme Internal Comms", "comms@acme.test", ago(3, "10:00"),
                           "Acme Weekly: new canteen menu and Friday town hall",
                           "Hi all,\n\nThe canteen menu has been refreshed and the town hall moves to 4pm on Friday.\n\n- Comms",
                           []),
                self._mail(7, "Vikram Rao", "vikram.rao@acme.test", ago(6, "10:05"),
                           "Referral: Karan Malhotra for Backend Engineer",
                           "Hi HR,\n\nReferring Karan Malhotra for JOB-101. I've added him to HireDesk; he'll email his resume.\n\nVikram",
                           []),
            ]
            self.sent: list[dict] = []
            self.next_sent = 1

            # sessions
            self.sessions: dict[str, dict] = {}
            UPLOAD_DIR.mkdir(exist_ok=True)

    @staticmethod
    def _cand(cid, name, email, phone, job, stage, source, years, notice, skills, notes=None):
        return {"id": cid, "name": name, "email": email, "phone": phone, "job": job, "stage": stage,
                "source": source, "years": years, "notice": notice, "skills": skills, "resume": None,
                "notes": [{"by": b, "at": a, "text": t} for (b, a, t) in (notes or [])],
                "created": datetime.now().strftime("%Y-%m-%d %H:%M")}

    @staticmethod
    def _mail(mid, name, addr, when, subject, body, attachments):
        return {"id": mid, "from_name": name, "from": addr, "to": "hr@acme.test", "date": when,
                "subject": subject, "body": body, "attachments": attachments}

    # ------------------------------------------------------------- helpers
    def log(self, kind: str, **data) -> None:
        self.events.append({"at": datetime.now().isoformat(timespec="seconds"), "kind": kind, **data})

    def chaos_fire(self, key: str) -> bool:
        """Return True exactly once per reset for a given chaos key (when chaos is enabled)."""
        with self.lock:
            if not self.chaos["enabled"] or self.chaos.get(key):
                return False
            self.chaos[key] = True
            return True

    def new_session(self, user: str) -> str:
        tok = secrets.token_hex(16)
        self.sessions[tok] = {"user": user, "loads": 0}
        return tok

    def is_busy(self, iv_id_ignore: str | None, interviewer: str, day: str, time: str) -> str | None:
        if (interviewer, day, time) in self.busy:
            return self.busy[(interviewer, day, time)]
        for iv in self.interviews.values():
            if iv["id"] != iv_id_ignore and iv["status"] == "Scheduled" and iv["interviewer"] == interviewer \
                    and iv["date"] == day and iv["time"] == time:
                return f"Interview: {self.candidates[iv['candidate']]['name']}"
        return None

    def find_candidate_by_email(self, email: str) -> dict | None:
        email = email.strip().lower()
        for c in self.candidates.values():
            if c["email"].lower() == email:
                return c
        return None

    def snapshot(self) -> dict:
        """Ground-truth state, used by tests and the run report (never shown to the agent)."""
        with self.lock:
            return copy.deepcopy({
                "today": self.today.isoformat(),
                "days": [d.isoformat() for d in self.days],
                "candidates": self.candidates, "interviews": self.interviews,
                "sent": self.sent, "events": self.events,
                "busy": [{"interviewer": k[0], "date": k[1], "time": k[2], "reason": v} for k, v in self.busy.items()],
            })


PHONE_RE = re.compile(r"^\+91[\s-]?\d{5}[\s-]?\d{5}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)

WORLD = World()
