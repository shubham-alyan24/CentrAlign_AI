"""Runtime configuration (from environment / .env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:  # optional: load .env if python-dotenv is installed
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover
    pass

# provider -> (OpenAI-compatible base URL or None for native SDK, default model, env var for key)
PROVIDERS = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-2.5-flash", "GEMINI_API_KEY"),
    "openai": (None, "gpt-4.1-mini", "OPENAI_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", "GROQ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "google/gemini-2.5-flash", "OPENROUTER_API_KEY"),
    "ollama": ("http://localhost:11434/v1", "qwen2.5:14b", "OLLAMA_API_KEY"),
    "anthropic": (None, "claude-sonnet-4-5", "ANTHROPIC_API_KEY"),
}


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "gemini").lower())
    model: str = field(default_factory=lambda: os.getenv("LLM_MODEL", ""))
    api_key: str = field(default_factory=lambda: os.getenv("LLM_API_KEY", ""))
    base_url: str = field(default_factory=lambda: os.getenv("LLM_BASE_URL", ""))
    temperature: float = field(default_factory=lambda: float(os.getenv("LLM_TEMPERATURE", "0.1")))
    reasoning_effort: str = field(default_factory=lambda: os.getenv("LLM_REASONING_EFFORT", "").strip())
    fallback_models: list = field(default_factory=lambda: [m.strip() for m in os.getenv("LLM_FALLBACK_MODELS", "").split(",")
                                                           if m.strip()])
    tpm_limit: int = field(default_factory=lambda: int(os.getenv("LLM_TPM_LIMIT", "0") or 0))
    max_output_tokens: int = field(default_factory=lambda: int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "0") or 0))
    min_call_interval: float = field(default_factory=lambda: float(os.getenv("LLM_MIN_INTERVAL", "0")))
    max_steps: int = field(default_factory=lambda: int(os.getenv("AGENT_MAX_STEPS", "120")))
    max_verify_rounds: int = 2
    history_recent: int = field(default_factory=lambda: int(os.getenv("AGENT_HISTORY_RECENT", "5")))
    page_text_chars: int = field(default_factory=lambda: int(os.getenv("AGENT_PAGE_TEXT_CHARS", "2200")))
    headed: bool = field(default_factory=lambda: _bool("BROWSER_HEADED", False))
    slow_mo: int = field(default_factory=lambda: int(os.getenv("BROWSER_SLOW_MO", "0")))
    approval_mode: str = "ask"            # ask | auto | deny
    learn: bool = True                    # write lessons after each run
    company_dir: Path = ROOT / "company"
    workspace_dir: Path = ROOT / "workspace"
    runs_dir: Path = ROOT / "runs"
    memory_file: Path = ROOT / "memory" / "lessons.json"

    def __post_init__(self) -> None:
        if self.provider not in PROVIDERS:
            raise ValueError(f"Unknown LLM_PROVIDER '{self.provider}'. Choose one of: {', '.join(PROVIDERS)}")
        base, model, key_env = PROVIDERS[self.provider]
        self.model = self.model or model
        self.base_url = self.base_url or (base or "")
        self.api_key = self.api_key or os.getenv(key_env, "")
