"""Settings loaded from environment / .env (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv() -> None:
    """Minimal .env loader so the project runs without python-dotenv."""
    for p in (Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"):
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            return


_load_dotenv()


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "y", "on")


@dataclass(frozen=True)
class Settings:
    sn_mode: str = os.getenv("SERVICENOW_MODE", "live").lower()          # live | mock
    sn_instance: str = os.getenv("SERVICENOW_INSTANCE_URL", "").rstrip("/")
    sn_user: str = os.getenv("SERVICENOW_USERNAME", "")
    sn_password: str = os.getenv("SERVICENOW_PASSWORD", "")
    sn_timeout: float = float(os.getenv("SERVICENOW_TIMEOUT", "30"))
    sn_auth: str = os.getenv("SERVICENOW_AUTH", "basic").lower()             # basic | oauth
    sn_client_id: str = os.getenv("SERVICENOW_CLIENT_ID", "")
    sn_client_secret: str = os.getenv("SERVICENOW_CLIENT_SECRET", "")
    llm_model: str = os.getenv("LLM_MODEL", "openai:gpt-4o-mini")       # provider:model for init_chat_model
    llm_temperature: float = float(os.getenv("LLM_TEMPERATURE", "0"))
    require_approval: bool = _bool("REQUIRE_APPROVAL", True)            # ask before any write


settings = Settings()
