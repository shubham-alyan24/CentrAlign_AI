"""Robustness of the model-output layer (no network)."""
import pytest

from agent.config import Settings
from agent.llm import LLM, LLMError, ScriptedLLM, extract_json
from agent.tools.base import Tool, ToolRegistry, ToolResult


def test_extract_json_variants():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! Here you go: {"a": {"b": "x}y"}} trailing') == {"a": {"b": "x}y"}}
    assert extract_json('{"a": [1, 2,],}') == {"a": [1, 2]}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_llm_retries_rate_limit_then_succeeds(monkeypatch):
    llm = LLM(Settings(provider="openai", api_key="sk-test"))
    calls = {"n": 0}

    class RateLimited(Exception):
        status_code = 429

    def fake_call(system, user):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RateLimited("slow down")
        return '{"actions": []}', 10, 5

    monkeypatch.setattr(llm, "_call", fake_call)
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert llm.json("s", "u", required=("actions",)) == {"actions": []}
    assert calls["n"] == 3 and llm.usage.calls == 1


def test_llm_repairs_invalid_json(monkeypatch):
    llm = LLM(Settings(provider="openai", api_key="sk-test"))
    replies = iter(["I think I should click", '{"thought": "ok", "actions": [{"tool": "observe"}]}'])
    seen = []
    monkeypatch.setattr(llm, "_call", lambda s, u: (seen.append(u) or next(replies), 1, 1))
    out = llm.json("s", "u", required=("actions",))
    assert out["actions"][0]["tool"] == "observe" and "PREVIOUS REPLY WAS INVALID" in seen[1]


def test_non_transient_error_is_raised(monkeypatch):
    llm = LLM(Settings(provider="openai", api_key="sk-test"))

    class BadKey(Exception):
        status_code = 401

    monkeypatch.setattr(llm, "_call", lambda s, u: (_ for _ in ()).throw(BadKey("invalid key")))
    with pytest.raises(LLMError):
        llm.complete("s", "u")


def test_registry_aliases_missing_and_unknown_tools():
    reg = ToolRegistry()
    reg.register(Tool("click", "x", {"id": "n"}, lambda id: ToolResult(True, f"clicked {id}")))
    assert reg.execute("click", {"element_id": 4}).output == "clicked 4"
    assert "missing required" in reg.execute("click", {}).output
    assert "unknown tool" in reg.execute("clik", {"id": 1}).output


def test_missing_key_message():
    with pytest.raises(LLMError):
        LLM(Settings(provider="gemini", api_key=""))


def test_normalize_action_shapes():
    from agent.state import normalize_actions
    assert normalize_actions({"tool": "click", "args": {"id": 3}}) == [("click", {"id": 3})]
    assert normalize_actions([{"name": "browser.click", "arguments": '{"id": 4}'}]) == [("click", {"id": 4})]
    assert normalize_actions([{"action": "open_url", "url": "http://x"}]) == [("open_url", {"url": "http://x"})]
    assert normalize_actions("nonsense") == []


def test_daily_limit_switches_to_fallback_model(monkeypatch):
    s = Settings(provider="groq", api_key="gsk-test")
    s.model, s.fallback_models = "openai/gpt-oss-120b", ["qwen/qwen3.8-27b", "openai/gpt-oss-20b"]
    llm = LLM(s)
    llm.models = [s.model] + s.fallback_models
    used = []

    class DailyLimit(Exception):
        status_code = 429

    def fake_call(system, user):
        used.append(llm.model)
        if llm.model == "openai/gpt-oss-120b":
            raise DailyLimit("Rate limit reached for model on tokens per day (TPD): Limit 200000")
        return '{"ok": true}', 10, 5

    monkeypatch.setattr(llm, "_call", fake_call)
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert llm.json("s", "u") == {"ok": True}
    assert used == ["openai/gpt-oss-120b", "qwen/qwen3.8-27b"] and llm.name == "groq:qwen/qwen3.8-27b"


def test_tpm_pacing_waits(monkeypatch):
    s = Settings(provider="openai", api_key="sk-test")
    s.tpm_limit = 1000
    llm = LLM(s)
    slept = []
    clock = {"t": 1000.0}
    monkeypatch.setattr("time.time", lambda: clock["t"])
    monkeypatch.setattr("time.sleep", lambda x: (slept.append(x), clock.__setitem__("t", clock["t"] + x)))
    llm._tpm_window = [(clock["t"] - 10, 900)]
    llm._pace_tokens(300)
    assert slept and 49 <= slept[0] <= 52


def test_native_tool_call_is_converted(monkeypatch):
    llm = LLM(Settings(provider="groq", api_key="gsk-test"))

    class ToolUseFailed(Exception):
        status_code = 400
        body = {"error": {"message": "Tool choice is none, but model called a tool", "code": "tool_use_failed",
                          "failed_generation": '{"name": "browser.open", "arguments": {"url": "http://127.0.0.1:8001"}}'}}

        def __str__(self):
            return "Error code: 400 - tool_use_failed"

    def boom(**kw):
        raise ToolUseFailed()

    monkeypatch.setattr(llm.client.chat.completions, "create", boom)
    out = llm.json("s", "u", required=("actions",))
    from agent.state import normalize_actions
    acts = normalize_actions(out["actions"])
    reg = ToolRegistry()
    reg.register(Tool("open_url", "x", {"url": "u"}, lambda url: ToolResult(True, f"opened {url}")))
    assert reg.execute(*acts[0]).output == "opened http://127.0.0.1:8001"


def test_json_validate_failed_falls_back_to_plain_text(monkeypatch):
    llm = LLM(Settings(provider="groq", api_key="gsk-test"))
    calls = []

    class JsonFailed(Exception):
        status_code = 400

        def __str__(self):
            return "Error code: 400 - json_validate_failed"

    class Msg:
        content = '{"thought": "ok", "actions": [{"tool": "observe", "args": {}}]}'

    class Choice:
        message = Msg()

    class Resp:
        choices = [Choice()]
        usage = None

    def create(**kw):
        calls.append("response_format" in kw)
        if "response_format" in kw:
            raise JsonFailed()
        return Resp()

    monkeypatch.setattr(llm.client.chat.completions, "create", create)
    out = llm.json("s", "u", required=("actions",))
    assert out["actions"][0]["tool"] == "observe" and calls == [True, False]


def test_rejected_reply_is_reasked_not_fatal(monkeypatch):
    llm = LLM(Settings(provider="openai", api_key="sk-test"))
    n = {"i": 0}

    class Bad(Exception):
        status_code = 400

    def fake_call(system, user):
        n["i"] += 1
        if n["i"] == 1:
            raise Bad("weird 400")
        return '{"actions": []}', 1, 1

    monkeypatch.setattr(llm, "_call", fake_call)
    assert llm.json("s", "u", required=("actions",)) == {"actions": []}


def test_overloaded_model_switches_after_repeated_503s(monkeypatch):
    s = Settings(provider="gemini", api_key="k")
    s.fallback_models = ["gemini-flash-latest"]
    llm = LLM(s)
    llm.models = [s.model, "gemini-flash-latest"]

    class Busy(Exception):
        status_code = 503

    def fake_call(system, user):
        if llm.model != "gemini-flash-latest":
            raise Busy("high demand")
        return '{"ok": 1}', 1, 1

    monkeypatch.setattr(llm, "_call", fake_call)
    monkeypatch.setattr("time.sleep", lambda x: None)
    assert llm.json("s", "u") == {"ok": 1} and llm.model == "gemini-flash-latest"


def test_circling_detection():
    from agent.state import ActionRecord, RunState, StepRecord
    st = RunState(request="r", run_id="x", today="t", model="m")
    for i in range(4):
        s = StepRecord(n=i + 1, phase="act", thought="")
        s.actions.append(ActionRecord("open_url", {"url": "http://127.0.0.1:8001/message/2"}, True, "ok"))
        st.steps.append(s)
    assert st.circling() and "open_url" in st.circling()[0]


def test_overload_fallback_probes_primary_again(monkeypatch):
    s = Settings(provider="gemini", api_key="k")
    s.model, s.fallback_models = "best", ["lite"]
    llm = LLM(s)
    llm.models = ["best", "lite"]
    state = {"busy": True}
    used = []

    class Busy(Exception):
        status_code = 503

    def fake_call(system, user):
        used.append(llm.model)
        if llm.model == "best" and state["busy"]:
            raise Busy("high demand")
        return '{"ok": 1}', 1, 1

    monkeypatch.setattr(llm, "_call", fake_call)
    monkeypatch.setattr("time.sleep", lambda x: None)
    for _ in range(7):
        llm.json("s", "u")
    assert llm.model == "lite"                       # fell back, a probe of "best" failed, still on lite
    state["busy"] = False
    for _ in range(8):
        llm.json("s", "u")
    assert llm.model == "best"                       # probed again later and moved back to the better model
