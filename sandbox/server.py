"""Start the sandbox apps (Acme Mail on :8001, HireDesk on :8002) in background threads.

Standalone:  python -m sandbox.server        (so you can click around yourself)
"""
from __future__ import annotations

import os
import socket
import threading
import time

import uvicorn

MAIL_PORT = int(os.getenv("MAIL_PORT", "8001"))
ATS_PORT = int(os.getenv("ATS_PORT", "8002"))
HOST = "127.0.0.1"
MAIL_URL = f"http://{HOST}:{MAIL_PORT}"
ATS_URL = f"http://{HOST}:{ATS_PORT}"

_servers: list[uvicorn.Server] = []


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex((HOST, port)) == 0


def _serve(app_path: str, port: int) -> uvicorn.Server:
    cfg = uvicorn.Config(app_path, host=HOST, port=port, log_level="warning", access_log=False)
    srv = uvicorn.Server(cfg)
    threading.Thread(target=srv.run, daemon=True, name=f"sandbox-{port}").start()
    _servers.append(srv)
    return srv


def start(wait: float = 15.0) -> bool:
    """Start both apps unless already running. Returns True if this process started them."""
    if _port_open(MAIL_PORT) and _port_open(ATS_PORT):
        return False
    _serve("sandbox.mail_app:app", MAIL_PORT)
    _serve("sandbox.ats_app:app", ATS_PORT)
    deadline = time.time() + wait
    while time.time() < deadline:
        if _port_open(MAIL_PORT) and _port_open(ATS_PORT):
            return True
        time.sleep(0.1)
    raise RuntimeError(f"Sandbox did not start on ports {MAIL_PORT}/{ATS_PORT}")


def stop() -> None:
    for s in _servers:
        s.should_exit = True
    _servers.clear()


def reset(chaos: bool | None = None) -> None:
    """Reset the world to the seed scenario (works whether the apps run in this process or another)."""
    import httpx
    params = {} if chaos is None else {"chaos": int(chaos)}
    httpx.post(f"{ATS_URL}/__admin/reset", params=params, timeout=5)  # mail + ATS share one WORLD


def state() -> dict:
    import httpx
    return httpx.get(f"{ATS_URL}/__admin/state", timeout=5).json()


if __name__ == "__main__":
    start()
    print(f"Acme Mail : {MAIL_URL}\nHireDesk  : {ATS_URL}  (login: recruiter / Hire@2026 unless overridden in .env)")
    print("Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop()
