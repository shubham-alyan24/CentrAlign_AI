"""A local fake LLM API (OpenAI- and Anthropic-compatible) backed by the scripted policy.

Lets us run the REAL CLI + REAL LLM client code (HTTP, SDK parsing, JSON mode, usage accounting)
end-to-end without internet access:

    python -m tests.fake_llm_server &        # serves http://127.0.0.1:8099
    LLM_PROVIDER=openai LLM_API_KEY=x LLM_BASE_URL=http://127.0.0.1:8099/v1 python main.py demo 1 --approve auto
"""
from __future__ import annotations

import json
import time

import uvicorn
from fastapi import FastAPI, Request

from tests.scripted_policy import ProcessApplicationsPolicy

app = FastAPI()
POLICY = {"p": ProcessApplicationsPolicy()}


def purpose_of(system: str) -> str:
    if "select relevant company documents" in system:
        return "understand"
    if "precise task briefs" in system:
        return "plan"
    if system.startswith("You are an independent verifier"):
        return "verify"
    if "reusable lessons" in system:
        return "reflect"
    if "Reply with JSON only." == system.strip():
        return "check"
    return "act"


def answer(system: str, user: str) -> str:
    p = purpose_of(system)
    if p == "check":
        return json.dumps({"ok": True, "model": "fake-scripted"})
    if p == "understand":                      # new run -> fresh scripted operator
        POLICY["p"] = ProcessApplicationsPolicy()
    return json.dumps(POLICY["p"](system, user, p))


@app.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    msgs = body["messages"]
    system = next((m["content"] for m in msgs if m["role"] == "system"), "")
    user = next((m["content"] for m in msgs if m["role"] == "user"), "")
    text = answer(system, user)
    return {"id": "fake", "object": "chat.completion", "created": int(time.time()), "model": body["model"],
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": len(system + user) // 4, "completion_tokens": len(text) // 4,
                      "total_tokens": (len(system + user) + len(text)) // 4}}


@app.post("/v1/messages")
async def messages(req: Request):
    body = await req.json()
    user = body["messages"][0]["content"]
    user = user if isinstance(user, str) else "".join(b.get("text", "") for b in user)
    system = body.get("system", "")
    system = system if isinstance(system, str) else "".join(b.get("text", "") for b in system)
    text = answer(system, user)
    return {"id": "msg_fake", "type": "message", "role": "assistant", "model": body["model"],
            "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": len(system + user) // 4, "output_tokens": len(text) // 4}}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8099, log_level="warning")
