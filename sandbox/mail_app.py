"""Acme Mail - a tiny webmail the agent operates like a human would (inbox, read, download, compose, send)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .store import EMAIL_RE, RESUME_DIR, WORLD

HERE = Path(__file__).parent
app = FastAPI(title="Acme Mail", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
tpl = Jinja2Templates(directory=HERE / "templates")


def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    ctx.setdefault("flash", request.query_params.get("flash"))
    return tpl.TemplateResponse(request, f"mail/{name}", {"owner": WORLD.mail_owner, **ctx}, status_code=status_code)


@app.get("/", response_class=HTMLResponse)
def inbox(request: Request, q: str = ""):
    msgs = sorted(WORLD.inbox, key=lambda m: m["date"], reverse=True)
    if q:
        ql = q.lower()
        msgs = [m for m in msgs if ql in (m["subject"] + m["from_name"] + m["from"] + m["body"]).lower()]
    return render(request, "inbox.html", msgs=msgs, q=q, folder="inbox")


@app.get("/message/{mid}", response_class=HTMLResponse)
def message(request: Request, mid: int):
    m = next((m for m in WORLD.inbox if m["id"] == mid), None)
    if not m:
        return render(request, "error.html", error=f"Message {mid} not found.", status_code=404)
    return render(request, "message.html", m=m, folder="inbox")


@app.get("/attachment/{mid}/{name}")
def attachment(mid: int, name: str):
    m = next((m for m in WORLD.inbox if m["id"] == mid), None)
    if not m or name not in m["attachments"]:
        return HTMLResponse("Attachment not found", status_code=404)
    WORLD.log("mail.download", message=mid, file=name)
    return FileResponse(RESUME_DIR / name, media_type="application/pdf", filename=name)


@app.get("/compose", response_class=HTMLResponse)
def compose(request: Request, reply_to: int | None = None):
    pre = {"to": "", "cc": "", "subject": "", "body": ""}
    if reply_to:
        m = next((m for m in WORLD.inbox if m["id"] == reply_to), None)
        if m:
            pre.update(to=m["from"], subject="Re: " + m["subject"])
    return render(request, "compose.html", pre=pre, errors=[], folder="compose")


@app.post("/send", response_class=HTMLResponse)
def send(request: Request, to: str = Form(""), cc: str = Form(""), subject: str = Form(""), body: str = Form("")):
    errors = []
    tos = [a.strip() for a in to.replace(";", ",").split(",") if a.strip()]
    ccs = [a.strip() for a in cc.replace(";", ",").split(",") if a.strip()]
    if not tos:
        errors.append("At least one recipient is required in To.")
    for a in tos + ccs:
        if not EMAIL_RE.match(a):
            errors.append(f"'{a}' is not a valid email address.")
    if not subject.strip():
        errors.append("Subject cannot be empty.")
    if not body.strip():
        errors.append("Message body cannot be empty.")
    if errors:
        return render(request, "compose.html", pre={"to": to, "cc": cc, "subject": subject, "body": body},
                      errors=errors, folder="compose", status_code=422)
    with WORLD.lock:
        sid = WORLD.next_sent
        WORLD.next_sent += 1
        WORLD.sent.append({"id": sid, "to": tos, "cc": ccs, "subject": subject.strip(), "body": body.strip(),
                           "date": datetime.now().strftime("%Y-%m-%d %H:%M")})
        WORLD.log("mail.sent", id=sid, to=tos, cc=ccs, subject=subject.strip())
    return RedirectResponse(f"/sent/{sid}?flash=Message+sent", status_code=303)


@app.get("/sent", response_class=HTMLResponse)
def sent(request: Request):
    return render(request, "sent.html", msgs=list(reversed(WORLD.sent)), folder="sent")


@app.get("/sent/{sid}", response_class=HTMLResponse)
def sent_one(request: Request, sid: int):
    m = next((m for m in WORLD.sent if m["id"] == sid), None)
    if not m:
        return render(request, "error.html", error=f"Sent message {sid} not found.", status_code=404)
    return render(request, "sent_message.html", m=m, folder="sent")


# ---- harness-only endpoints (the agent's guardrail blocks any /__ path) ----
@app.get("/__admin/state")
def admin_state():
    return JSONResponse(WORLD.snapshot())


@app.post("/__admin/reset")
def admin_reset(chaos: int | None = None):
    WORLD.reset(None if chaos is None else bool(chaos))
    return {"ok": True}
