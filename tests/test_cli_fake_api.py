"""The real CLI + real LLM client (OpenAI-compatible HTTP) end-to-end, against a local fake LLM API."""
import socket
import threading
import time

import pytest
import uvicorn


@pytest.fixture(scope="module")
def fake_api():
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", 8099)) == 0:
            yield "http://127.0.0.1:8099"
            return
    from tests.fake_llm_server import app
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8099, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    time.sleep(1.0)
    yield "http://127.0.0.1:8099"
    srv.should_exit = True


@pytest.mark.parametrize("provider,base", [("openai", "/v1"), ("anthropic", "")])
def test_cli_demo_1(fake_api, sandbox_live, monkeypatch, tmp_path, provider, base):
    monkeypatch.setenv("LLM_PROVIDER", provider)
    monkeypatch.setenv("LLM_API_KEY", "fake")
    monkeypatch.setenv("LLM_BASE_URL", fake_api + base)
    monkeypatch.setenv("LLM_MODEL", "fake-model")
    monkeypatch.setenv("AGENT_MAX_STEPS", "120")
    import main
    assert main.main(["check"]) == 0
    assert main.main(["demo", "1", "--approve", "auto", "--no-learn"]) == 0
