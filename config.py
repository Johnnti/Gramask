"""Validated, environment-only runtime configuration. Never send secrets to the UI."""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


def boolean(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).lower().strip()
    if value not in {"true", "false", "yes", "no", "1", "0"}:
        raise ValueError(f"{name} must be true or false")
    return value in {"true", "yes", "1"}


def number(name: str, default, minimum, maximum, cast=float):
    try:
        value = cast(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass
class Settings:
    db_path: str
    quorum: int
    agreement: float
    timeout_min: float
    fact_ttl_hours: float
    demo_mode: bool
    app_env: str
    app_origin: str
    secure_cookies: bool
    session_days: int
    llm_provider: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_timeout: float
    allow_fallback: bool
    ask_rate_limit: int
    auth_rate_limit: int
    llm_max_concurrent: int = 2
    demo_broadcast_all: bool = False
    question_routing: str = 'knowledge_first'
    live_freshness_min: float = 5
    demo_live_llm: bool = False

    @property
    def simulated_llm(self):
        return self.demo_mode and not self.demo_live_llm

    @property
    def live_ttl_seconds(self):
        return min(self.fact_ttl_hours * 3600, self.live_freshness_min * 60)

    @property
    def broadcast_questions(self):
        return self.demo_mode and self.demo_broadcast_all

    @classmethod
    def from_env(cls):
        app_env = os.getenv("APP_ENV", "development").lower()
        if app_env not in {"development", "production", "test"}:
            raise ValueError("APP_ENV must be development, production, or test")
        origin = os.getenv("APP_ORIGIN", "").rstrip("/")
        if origin:
            parsed = urlparse(origin)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
                raise ValueError("APP_ORIGIN must be an HTTP(S) origin without a path or credentials")
        secure = boolean("SECURE_COOKIES", app_env == "production")
        demo = boolean("DEMO_MODE", False)
        routing = os.getenv("QUESTION_ROUTING", "knowledge_first").lower().strip()
        if routing not in {'community', 'knowledge_first'}:
            raise ValueError("QUESTION_ROUTING must be community or knowledge_first")
        if app_env == "production" and (not origin.startswith("https://") or not secure or demo):
            raise ValueError("Production requires HTTPS APP_ORIGIN, SECURE_COOKIES=true, and DEMO_MODE=0")
        provider = os.getenv("LLM_PROVIDER", "ollama").lower()
        if provider not in {"ollama", "openai"}:
            raise ValueError("LLM_PROVIDER must be ollama or openai (an API format, not an OpenAI model)")
        base = os.getenv("LLM_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
        parsed = urlparse(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("LLM_BASE_URL must be an HTTP(S) URL; put credentials in LLM_API_KEY")
        model = os.getenv("LLM_MODEL", "gemma3:4b").strip()
        if not re.search(r"gemma[-_]?3(?=[:/\-_]|$)", model.lower()):
            raise ValueError("LLM_MODEL must identify a Gemma 3 model, e.g. gemma3:4b or google/gemma-3-27b-it")
        return cls(
            db_path=os.getenv("DB_PATH", str(Path(__file__).parent / "gramask.db")),
            quorum=number("QUORUM", 3, 1, 100, int),
            agreement=number("AGREEMENT", 0.6, 0.51, 1),
            timeout_min=number("TIMEOUT_MIN", 15, 0.01, 1440),
            fact_ttl_hours=number("FACT_TTL_HOURS", 2, 0.001, 24),
            demo_mode=demo, app_env=app_env, app_origin=origin, secure_cookies=secure,
            session_days=number("SESSION_DAYS", 14, 1, 90, int),
            llm_provider=provider, llm_base_url=base,
            llm_api_key=os.getenv("LLM_API_KEY", ""), llm_model=model,
            llm_timeout=number("LLM_TIMEOUT", 45, 1, 300),
            allow_fallback=boolean("ALLOW_FALLBACK", False),
            ask_rate_limit=number("ASK_RATE_LIMIT", 20, 1, 1000, int),
            auth_rate_limit=number("AUTH_RATE_LIMIT", 15, 1, 1000, int),
            llm_max_concurrent=number("LLM_MAX_CONCURRENT", 2, 1, 16, int),
            demo_broadcast_all=boolean("DEMO_BROADCAST_ALL", False),
            question_routing=routing,
            live_freshness_min=number("LIVE_FRESHNESS_MIN", 5, 0.01, 1440),
            demo_live_llm=boolean("DEMO_LIVE_LLM", False),
        )


settings = Settings.from_env()
