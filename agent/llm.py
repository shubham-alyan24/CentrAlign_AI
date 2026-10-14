"""Provider-agnostic LLM client.

Design choice: the agent asks the model for ONE JSON object per turn (thought, plan,
actions) instead of relying on each vendor's native function-calling format. That keeps
the agent portable across Gemini / OpenAI / Groq / OpenRouter / Ollama (all via the
OpenAI-compatible API) and Anthropic, and lets us validate + repair the output ourselves.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from .config import Settings


class LLMError(Exception):
    pass


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    by_purpose: dict = field(default_factory=dict)

    def add(self, purpose: str, inp: int, out: int, secs: float) -> None:
        self.calls += 1
        self.input_tokens += inp
        self.output_tokens += out
        self.seconds += secs
        self.by_purpose[purpose] = self.by_purpose.get(purpose, 0) + 1


def extract_json(text: str) -> dict:
    """Parse the first JSON object in a model reply (tolerates ```json fences and chatter)."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S)
    start = t.find("{")
    if start == -1:
        raise ValueError("no JSON object found")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                chunk = t[start: i + 1]
                try:
                    return json.loads(chunk)
                except json.JSONDecodeError:
                    return json.loads(re.sub(r",\s*([}\]])", r"\1", chunk))   # trailing commas
    raise ValueError("unterminated JSON object")


class LLM:
    def __init__(self, settings: Settings):
        self.s = settings
        self.usage = Usage()
        self._last_call = 0.0
        self._json_mode = True
        self._send_temperature = True
        self._send_reasoning = True
        # model fallback chain: when a free tier's DAILY limit is hit, move to the next model (each has its own quota)
        self.models = [settings.model] + [m for m in settings.fallback_models if m and m != settings.model]
        self._model_idx = 0
        self._probe_target: int | None = None    # better model to re-try after an overload fallback
        self._fallback_idx = 0
        self._probing = False
        self._calls_since_switch = 0
        self._tpm_window: list[tuple[float, int]] = []    # (timestamp, tokens) for per-minute pacing
        if settings.provider == "anthropic":
            import anthropic
            self.client = anthropic.Anthropic(api_key=settings.api_key or None, base_url=settings.base_url or None,
                                              max_retries=0)
            import inspect
            self._anthropic_temperature = "temperature" in inspect.signature(self.client.messages.create).parameters
        else:
            import openai
            if not settings.api_key and settings.provider != "ollama":
                raise LLMError(f"No API key for provider '{settings.provider}'. Set LLM_API_KEY in .env")
            self.client = openai.OpenAI(api_key=settings.api_key or "ollama", base_url=settings.base_url or None,
                                        timeout=120, max_retries=0)

    @property
    def model(self) -> str:
        return self.models[self._model_idx]

    @property
    def name(self) -> str:
        return f"{self.s.provider}:{self.model}"

    def _after_success(self) -> None:
        """After falling back because of overload, periodically try the better model again."""
        if self._probing:
            print(f"   [llm] back on {self.model}")
            self._probing, self._probe_target = False, None
            return
        if self._probe_target is not None:
            self._calls_since_switch += 1
            if self._calls_since_switch >= 6:
                self._fallback_idx, self._model_idx = self._model_idx, self._probe_target
                self._probing, self._calls_since_switch = True, 0

    def _switch_model(self, reason: str) -> bool:
        if self._model_idx + 1 >= len(self.models):
            return False
        old = self.model
        self._model_idx += 1
        self._send_reasoning = True
        print(f"   [llm] {reason} on {old}; switching to {self.model}")
        return True

    def _pace_tokens(self, estimate: int) -> None:
        """Stay under a tokens-per-minute budget (free tiers like Groq's) instead of hammering 429s."""
        limit = self.s.tpm_limit
        if not limit:
            return
        while True:
            now = time.time()
            self._tpm_window = [(t, n) for t, n in self._tpm_window if now - t < 60]
            used = sum(n for _, n in self._tpm_window)
            if used + estimate <= limit or not self._tpm_window:
                return
            wait = 60 - (now - self._tpm_window[0][0]) + 0.5
            print(f"   [llm] pacing for the per-minute token limit: waiting {wait:.0f}s ...")
            time.sleep(max(wait, 1))

    # ------------------------------------------------------------- raw call
    def _call(self, system: str, user: str) -> tuple[str, int, int]:
        if self.s.provider == "anthropic":
            kwargs = dict(model=self.model, max_tokens=self.s.max_output_tokens or 4096, system=system,
                          messages=[{"role": "user", "content": user}])
            if self._anthropic_temperature:          # newer SDK/model versions dropped this parameter
                kwargs["temperature"] = self.s.temperature
            r = self.client.messages.create(**kwargs)
            text = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
            return text, r.usage.input_tokens, r.usage.output_tokens
        kwargs = dict(model=self.model,
                      messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
        if self._send_temperature:
            kwargs["temperature"] = self.s.temperature
        if self.s.reasoning_effort and self._send_reasoning:   # e.g. "low" to make thinking models faster/cheaper
            kwargs["reasoning_effort"] = self.s.reasoning_effort
        if self.s.max_output_tokens:
            kwargs["max_tokens"] = self.s.max_output_tokens
        if self._json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        for _ in range(3):
            try:
                r = self.client.chat.completions.create(**kwargs)
                break
            except Exception as e:  # some models reject optional params (JSON mode, temperature): drop and retry
                msg = str(e).lower()
                if getattr(e, "status_code", None) == 400 and "response_format" in msg and "response_format" in kwargs:
                    self._json_mode = False
                    kwargs.pop("response_format")
                elif getattr(e, "status_code", None) == 400 and "temperature" in msg and "temperature" in kwargs:
                    self._send_temperature = False
                    kwargs.pop("temperature")
                elif getattr(e, "status_code", None) == 400 and "reasoning" in msg and "reasoning_effort" in kwargs:
                    self._send_reasoning = False
                    kwargs.pop("reasoning_effort")
                elif getattr(e, "status_code", None) == 400 and "json_validate_failed" in msg and "response_format" in kwargs:
                    # provider-side JSON validation is brittle with some models (e.g. reasoning ran out of tokens);
                    # we validate/repair JSON ourselves, so switch provider JSON mode off and retry
                    print("   [llm] provider JSON mode rejected the reply; retrying without it")
                    self._json_mode = False
                    kwargs.pop("response_format")
                elif getattr(e, "status_code", None) == 400 and "tool_use_failed" in msg:
                    converted = self._convert_native_tool_call(e)
                    if converted is None:
                        raise
                    return converted, 0, 0
                else:
                    raise
        else:
            raise LLMError("model rejected the request parameters")
        text = r.choices[0].message.content or ""
        u = getattr(r, "usage", None)
        return text, getattr(u, "prompt_tokens", 0) or 0, getattr(u, "completion_tokens", 0) or 0

    @staticmethod
    def _convert_native_tool_call(e: Exception) -> str | None:
        """Some models (e.g. gpt-oss) sometimes emit a native tool call instead of our JSON text, which the API
        rejects with 'tool_use_failed'. The attempted call is in the error body: turn it into our action format
        instead of wasting a retry."""
        body = getattr(e, "body", None)
        fg = None
        if isinstance(body, dict):
            fg = body.get("failed_generation") or (body.get("error") or {}).get("failed_generation")
        if not fg:
            m = re.search(r"failed_generation['\"]?\s*:\s*'(.*?)'\s*}", str(e), re.S)
            fg = m.group(1) if m else None
        if not fg:
            return None
        try:
            call = extract_json(fg)
        except ValueError:
            return None
        if "actions" in call or "verdict" in call:          # it was our format after all
            return json.dumps(call)
        name = call.get("name") or call.get("tool") or ""
        args = call.get("arguments", call.get("args", {}))
        print(f"   [llm] model emitted a native tool call ({name}); converted to an action")
        return json.dumps({"thought": "(converted native tool call)", "actions": [{"tool": name, "args": args}]})

    def complete(self, system: str, user: str, purpose: str = "general", attempts: int = 7) -> str:
        """One completion with pacing + retry/backoff on rate limits and transient server errors."""
        delay = 2.0
        conn_failures = 0
        avg_out = (self.usage.output_tokens // self.usage.calls) if self.usage.calls else 600
        estimate = (len(system) + len(user)) // 4 + max(avg_out, 300)
        attempt = 0
        while attempt < attempts:
            attempt += 1
            wait = self.s.min_call_interval - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._pace_tokens(estimate)
            t0 = time.time()
            self._last_call = t0
            try:
                text, i, o = self._call(system, user)
                self._overloaded = 0
                self._after_success()
                self.usage.add(purpose, i, o, time.time() - t0)
                self._tpm_window.append((time.time(), (i + o) or estimate))
                return text
            except Exception as e:
                code = getattr(e, "status_code", None)
                msg = str(e).lower()
                daily = code == 429 and any(k in msg for k in ("per day", "(tpd)", "(rpd)", "daily"))
                gone = code in (403, 404) and "model" in msg
                if (daily or gone) and self._probing:
                    self._model_idx, self._probing, self._probe_target = self._fallback_idx, False, None
                    attempt -= 1
                    continue
                if (daily or gone) and self._switch_model("daily limit reached" if daily else "model unavailable"):
                    self._probe_target = None
                    attempt -= 1                              # a fresh model gets fresh attempts
                    continue
                if code == 429:
                    self._tpm_window.append((time.time(), estimate))
                if code in (503, 529):
                    if self._probing:                      # the better model is still busy: go back at once
                        self._model_idx, self._probing = self._fallback_idx, False
                        print(f"   [llm] {self.models[self._probe_target]} still overloaded; staying on {self.model}")
                        attempt -= 1
                        continue
                    overloaded = getattr(self, "_overloaded", 0) + 1
                    self._overloaded = overloaded
                    if overloaded >= 4:
                        before = self._model_idx
                        if self._switch_model("model overloaded (503)"):
                            if self._probe_target is None:
                                self._probe_target = before    # retry the better model again later
                            self._calls_since_switch = 0
                            self._overloaded = 0
                            attempt -= 1
                            continue
                transient = code in (408, 409, 429, 500, 502, 503, 504, 529) or \
                    type(e).__name__ in ("APIConnectionError", "APITimeoutError", "RateLimitError", "InternalServerError")
                if type(e).__name__ in ("APIConnectionError", "APITimeoutError"):
                    conn_failures += 1
                if not transient or attempt >= attempts or conn_failures >= 3:
                    raise LLMError(f"{type(e).__name__}: {str(e)[:400]}") from e
                retry_after = None
                try:
                    retry_after = float(e.response.headers.get("retry-after"))  # type: ignore[attr-defined]
                except Exception:
                    pass
                sleep = min(retry_after or delay, 65)
                print(f"   [llm] {type(e).__name__} (HTTP {code}); retrying in {sleep:.0f}s ...")
                time.sleep(sleep)
                delay = min(delay * 2, 60)
        raise LLMError("unreachable")

    def json(self, system: str, user: str, purpose: str = "general", required: tuple[str, ...] = (),
             attempts: int = 7) -> dict:
        """Completion that must be a JSON object containing `required` keys; repairs up to 2 times."""
        prompt = user
        last_err = ""
        for _ in range(3):
            try:
                text = self.complete(system, prompt, purpose, attempts)
            except LLMError as e:                 # a rejected (HTTP 400) reply: ask again instead of dying
                if "400" not in str(e) and "BadRequest" not in str(e):
                    raise
                last_err = str(e)[:200]
                prompt = (user + "\n\nYOUR PREVIOUS REPLY WAS REJECTED BY THE API. Reply with plain JSON text only "
                                 f"(no tool/function calls), with keys {list(required)}.")
                continue
            try:
                obj = extract_json(text)
                missing = [k for k in required if k not in obj]
                if missing:
                    raise ValueError(f"missing keys: {missing}")
                return obj
            except ValueError as e:
                last_err = f"{e}"
                prompt = (user + f"\n\nYOUR PREVIOUS REPLY WAS INVALID ({e}). Reply with ONLY one valid JSON object "
                                 f"with keys {list(required)}. Previous reply started with: {text[:300]!r}")
        raise LLMError(f"Model did not return valid JSON after 3 attempts: {last_err}")


class ScriptedLLM:
    """Test double: a function decides each reply from the prompt. Used to test the harness
    deterministically (no network); real runs use LLM."""

    def __init__(self, policy: Callable[[str, str, str], dict | str]):
        self.policy = policy
        self.usage = Usage()
        self.name = "scripted"

    def complete(self, system: str, user: str, purpose: str = "general", attempts: int = 1) -> str:
        out = self.policy(system, user, purpose)
        self.usage.add(purpose, len(system + user) // 4, 50, 0.0)
        return out if isinstance(out, str) else json.dumps(out)

    def json(self, system: str, user: str, purpose: str = "general", required: tuple[str, ...] = (),
             attempts: int = 1) -> dict:
        return extract_json(self.complete(system, user, purpose))
