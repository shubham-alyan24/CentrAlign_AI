"""A scripted stand-in for the LLM, used ONLY to test the harness end-to-end without network access.

It reads the same prompt a real model would get (live page, last tool output) and replies with the
same JSON format. It is deliberately written like a careful operator: it re-checks pages after every
submit, re-logs in when the session expires, retries outages, fixes validation errors and re-plans
when a slot is taken - so the test proves the HARNESS supports all of that. Real runs use agent/llm.py.
"""
from __future__ import annotations

import re
from datetime import date

ATS = "http://127.0.0.1:8002"
MAIL = "http://127.0.0.1:8001"
EL_RE = re.compile(r'^\[(\d+)\] (link|button|select|file-upload|textarea|input\[\w+\]|checkbox|radio) "(.*?)"(.*)$')


class Page:
    def __init__(self, prompt: str):
        self.raw = prompt.split("CURRENT PAGE:\n", 1)[-1]
        self.url = (re.search(r"^URL: (\S+)", self.raw, re.M) or [None, ""])[1]
        self.text = self.raw.split("--- INTERACTIVE ELEMENTS ---")[0]
        self.els = []
        for line in self.raw.split("--- INTERACTIVE ELEMENTS ---")[-1].splitlines():
            m = EL_RE.match(line.strip())
            if m:
                self.els.append((int(m[1]), m[2], m[3], m[4]))
        lo = prompt.split("LAST TOOL OUTPUT:\n", 1)[-1].split("\n\nCURRENT PAGE:", 1)[0]
        self.last_output = lo

    def id(self, label: str, kind: str | None = None) -> int:
        for i, k, name, rest in self.els:
            if label.lower() in name.lower() and (kind is None or k.startswith(kind)):
                return i
        raise LookupError(f"no element {label!r} on {self.url}\n{self.raw[:1500]}")

    @property
    def is_login(self) -> bool:
        return "/login" in self.url


def A(tool, **args):
    return {"tool": tool, "args": args}


class ProcessApplicationsPolicy:
    """Task 1: process Backend Engineer applications end to end."""

    def __init__(self):
        self.gen = self._script()
        self.page: Page | None = None
        self.started = False
        self.log: list[str] = []

    # -------------------------------------------------------------- llm api
    def __call__(self, system: str, user: str, purpose: str):
        if purpose == "understand":
            return {"relevant_docs": ["systems.md", "hiring-policy.md", "interview-procedure.md",
                                      "communication-policy.md", "org-directory.md"], "why": "hiring workflow"}
        if purpose == "plan":
            return {"objective": "All of this week's Backend Engineer applicants are in HireDesk with a screening "
                                 "decision, and shortlisted ones have a confirmed first-round interview.",
                    "success_criteria": [
                        "Ananya Sharma, Rohit Verma and Karan Malhotra exist exactly once in HireDesk for JOB-101 with resume attached",
                        "Candidates meeting all must-haves are Shortlisted/Interview Scheduled; others Rejected with a reason",
                        "Each shortlisted candidate has a Round 1 - Technical interview with a JOB-101 panel interviewer",
                        "Each shortlisted candidate received a confirmation email with the interviewer in Cc (approved by a human)"],
                    "constraints": ["External emails need human approval", "Do not email rejected candidates",
                                    "Do not create duplicates"],
                    "key_facts": [
                        "Acme Mail http://127.0.0.1:8001 (signed in); HireDesk http://127.0.0.1:8002, sign in with fill_secret ATS_USERNAME / ATS_PASSWORD",
                        "Search HireDesk by email before adding; complete existing (referral) profiles instead of duplicating",
                        "Record: name, email, phone +91 format, job JOB-101, years, notice days, skills, source Email application (keep Referral), resume PDF",
                        "JOB-101 shortlist only if >=2 years, Python + Django/FastAPI, notice <=60 days; else Rejected with reason note",
                        "Do not email rejected candidates",
                        "Round 1 - Technical, 45 min, panel Vikram Rao (vikram.rao@acme.test) / Sneha Kapoor (sneha.kapoor@acme.test), earliest free slot",
                        "Email candidate, Cc interviewer. Subject: Interview confirmation - <Job title> - Acme. Body: Hi <First name>, Your <Round> interview for the <Job title> role is scheduled on <Day, DD Mon YYYY> at <HH:MM> IST (45 minutes) with <Interviewer name>. A video link will be shared a day before. Please reply if you need to reschedule. Regards, Acme Talent Team",
                        "External emails need human approval (requested automatically on Send)"],
                    "assumptions": ["'This week' = applications received in the last 7 days"],
                    "blocking_question": None,
                    "plan": ["Read applications and resumes", "Record candidates in HireDesk", "Screen against policy",
                             "Book interviews", "Email confirmations", "Verify"]}
        if purpose == "verify":
            return self._verify(user)
        if purpose == "reflect":
            return {"lessons": ["HireDesk phone numbers must be entered as +91 followed by 10 digits."]}
        # act
        self.page = Page(user)
        if not self.started:
            self.started = True
            actions = next(self.gen)
        else:
            actions = self.gen.send(self.page)
        if isinstance(actions, dict):        # finish
            return {"thought": "All criteria visible in the systems.", "actions": [actions]}
        return {"thought": f"scripted step: {actions[0]['tool']}", "actions": actions}

    # ------------------------------------------------------------ helpers
    def _login(self, page: Page):
        page = yield [A("fill_secret", id=page.id("Username"), secret_name="ATS_USERNAME"),
                      A("fill_secret", id=page.id("Password"), secret_name="ATS_PASSWORD"),
                      A("click", id=page.id("Sign in", "button"))]
        self.log.append("login")
        return page

    def _goto(self, url: str):
        page = yield [A("open_url", url=url)]
        while page.is_login:
            page = yield from self._login(page)
        return page

    # ------------------------------------------------------------- script
    def _script(self):
        page = yield [A("open_url", url=MAIL + "/")]
        apps = []
        for label in ["Application - Backend Engineer - Ananya", "Backend Engineer application",
                      "Applying for Backend Engineer"]:
            page = yield from self._goto(MAIL + "/")
            page = yield [A("click", id=page.id(label, "link"))]
            sender = re.search(r"From: (.+?) <([^>]+)>", page.text)
            dl = next(name for i, k, name, _ in page.els if name.startswith("Download"))
            page = yield [A("click", id=page.id(dl, "link"))]
            fname = dl.replace("Download ", "")
            page = yield [A("read_file", path=f"workspace/downloads/{fname}")]
            t = page.last_output
            body_phone = re.search(r"(\+91 \d{5} \d{5}|\b\d{10}\b)", t)
            c = {"name": sender[1], "email": sender[2], "file": fname,
                 "phone": body_phone[1] if body_phone else "",
                 "years": re.search(r"experience: ([\d.]+) years", t)[1],
                 "notice": re.search(r"Notice period: (\d+) days", t)[1],
                 "skills": re.search(r"Skills\n(.+)", t)[1].strip()}
            yield [A("remember", key=f"applicant {c['name']}", value=str(c))]
            apps.append(c)

        results = {}
        for c in apps:
            # duplicate check first
            page = yield from self._goto(f"{ATS}/candidates?q={c['email']}")
            m = re.search(r"(C-\d+)\s+" + re.escape(c["name"]), page.text)
            cid = m[1] if m else None
            for attempt in range(5):
                if cid:
                    page = yield from self._goto(f"{ATS}/candidates/{cid}/edit")
                else:
                    page = yield from self._goto(f"{ATS}/candidates/new")
                acts = [A("type_text", id=page.id("Full name"), text=c["name"]),
                        A("type_text", id=page.id("Email"), text=c["email"]),
                        A("type_text", id=page.id("Phone"), text=c["phone"]),
                        A("select_option", id=page.id("Job", "select"), option="Backend Engineer"),
                        A("type_text", id=page.id("Years of professional"), text=c["years"]),
                        A("type_text", id=page.id("Notice period"), text=c["notice"]),
                        A("type_text", id=page.id("Key skills"), text=c["skills"]),
                        A("upload_file", id=page.id("Resume", "file"), path=f"workspace/downloads/{c['file']}")]
                if not cid:
                    acts.insert(7, A("select_option", id=page.id("Source", "select"), option="Email application"))
                acts.append(A("click", id=page.id("Save changes" if cid else "Add candidate", "button")))
                page = yield acts
                if page.is_login:
                    page = yield from self._login(page)
                    self.log.append("relogin-after-submit")
                    continue
                if "created" in page.text or "Candidate updated" in page.text:
                    cid = re.search(r"/candidates/(C-\d+)", page.url)[1]
                    break
                if "503" in page.text or "temporarily unavailable" in page.text:
                    self.log.append("retry-503")
                    continue
                if "Phone must be" in page.text:
                    digits = re.sub(r"\D", "", c["phone"])[-10:]
                    c["phone"] = f"+91 {digits[:5]} {digits[5:]}"
                    self.log.append("fixed-phone")
                    continue
                dup = re.search(r"already exists: (C-\d+)", page.text)
                if dup:
                    cid = dup[1]
                    continue
                raise AssertionError("unexpected page after submit:\n" + page.text[:1500])
            yield [A("remember", key=f"hiredesk id {c['name']}", value=cid)]
            ok = float(c["years"]) >= 2 and int(c["notice"]) <= 60 and "python" in c["skills"].lower() \
                and ("django" in c["skills"].lower() or "fastapi" in c["skills"].lower())
            stage = "Shortlisted" if ok else "Rejected"
            note = ("Meets all JOB-101 must-haves." if ok else
                    "Does not meet must-have: at least 2 years professional experience." if float(c["years"]) < 2 else
                    "Does not meet must-have: notice period <= 60 days.")
            for attempt in range(3):
                page = yield from self._goto(f"{ATS}/candidates/{cid}")
                page = yield [A("select_option", id=page.id("New stage", "select"), option=stage),
                              A("type_text", id=page.id("Note"), text=note),
                              A("click", id=page.id("Update stage", "button"))]
                if page.is_login:
                    page = yield from self._login(page)
                    continue
                if "Stage updated" in page.text:
                    break
            results[c["name"]] = {"id": cid, "stage": stage, **c}

        # calendars
        free = []
        for iv_id, iv_name in [("INT-1", "Vikram Rao"), ("INT-2", "Sneha Kapoor")]:
            page = yield from self._goto(f"{ATS}/calendar?interviewer={iv_id}")
            for line in page.text.splitlines():
                m = re.match(r"(\w{3} \d{2} \w{3} \d{4})\t(.+)", line)
                if m:
                    cells = m[2].split("\t")
                    for t, cell in zip(["10:00", "11:30", "14:00", "15:30"], cells):
                        if cell.strip() == "Free":
                            free.append((date.fromisoformat(_iso(m[1])), t, iv_name, m[1]))
        free.sort()
        booked = []
        for name, r in results.items():
            if r["stage"] != "Shortlisted":
                continue
            while True:
                d, t, iv_name, label = free.pop(0)
                page = yield from self._goto(f"{ATS}/candidates/{r['id']}/schedule")
                page = yield [A("select_option", id=page.id("Interviewer", "select"), option=iv_name),
                              A("select_option", id=page.id("Date", "select"), option=label),
                              A("select_option", id=page.id("Time", "select"), option=t),
                              A("click", id=page.id("Book interview", "button"))]
                if page.is_login:
                    page = yield from self._login(page)
                    free.insert(0, (d, t, iv_name, label))
                    continue
                if "scheduled" in page.text and "/interviews/IV-" in page.url:
                    booked.append((r, iv_name, label, t))
                    break
                self.log.append("slot-conflict")
        emails = {"Vikram Rao": "vikram.rao@acme.test", "Sneha Kapoor": "sneha.kapoor@acme.test"}
        for r, iv_name, label, t in booked:
            page = yield from self._goto(MAIL + "/compose")
            first = r["name"].split()[0]
            body = (f"Hi {first},\n\nYour Round 1 - Technical interview for the Backend Engineer role is scheduled on "
                    f"{label} at {t} IST (45 minutes) with {iv_name}. A video link will be shared a day before.\n\n"
                    "Please reply if you need to reschedule.\n\nRegards,\nAcme Talent Team")
            page = yield [A("type_text", id=page.id("To"), text=r["email"]),
                          A("type_text", id=page.id("Cc"), text=emails[iv_name]),
                          A("type_text", id=page.id("Subject"), text="Interview confirmation - Backend Engineer - Acme"),
                          A("type_text", id=page.id("Message"), text=body),
                          A("click", id=page.id("Send", "button"))]
            assert "Message sent" in page.text, page.text[:800]
        yield {"tool": "finish", "args": {"status": "completed",
               "summary": f"Processed {len(results)} applications; booked {len(booked)} interviews.",
               "evidence": [f"{n}: {r['id']} {r['stage']}" for n, r in results.items()]}}

    # ------------------------------------------------------------- verifier
    def _verify(self, user: str):
        page = Page(user)
        if not hasattr(self, "_vstate"):
            self._vstate = 0
        self._vstate += 1
        if page.is_login:
            return {"thought": "need to sign in", "actions": [
                A("fill_secret", id=page.id("Username"), secret_name="ATS_USERNAME"),
                A("fill_secret", id=page.id("Password"), secret_name="ATS_PASSWORD"),
                A("click", id=page.id("Sign in", "button"))]}
        if "/candidates?job=JOB-101" not in page.url and self._vstate < 6 and "candidates" not in getattr(self, "_seen", ""):
            self._seen = "candidates"
            return {"thought": "check candidates", "actions": [A("open_url", url=f"{ATS}/candidates?job=JOB-101")]}
        if self._seen == "candidates":
            self._cand_text = page.text
            self._seen = "interviews"
            return {"thought": "check interviews", "actions": [A("open_url", url=f"{ATS}/interviews")]}
        if self._seen == "interviews":
            self._iv_text = page.text
            self._seen = "sent"
            return {"thought": "check sent mail", "actions": [A("open_url", url=f"{MAIL}/sent")]}
        sent = page.text
        crit = [
            ("Ananya Sharma, Rohit Verma and Karan Malhotra exist exactly once", all(
                self._cand_text.count(n) == 1 for n in ["Ananya Sharma", "Rohit Verma", "Karan Malhotra"])),
            ("Rohit rejected", bool(re.search(r"Rohit Verma.*Rejected", self._cand_text, re.S))),
            ("Interviews booked", self._iv_text.count("Backend Engineer") >= 2),
            ("Emails sent", sent.count("Interview confirmation") >= 2),
        ]
        return {"thought": "checked", "verdict": "pass" if all(c[1] for c in crit) else "fail",
                "criteria": [{"criterion": c, "met": m, "evidence": "seen on page"} for c, m in crit],
                "issues": [c for c, m in crit if not m]}


def _iso(label: str) -> str:
    from datetime import datetime
    return datetime.strptime(label, "%a %d %b %Y").date().isoformat()
