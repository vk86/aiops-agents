"""Chat model factory. LLM_MODEL is "provider:model", for example:

  openai:gpt-4o-mini              needs OPENAI_API_KEY
  azure_openai:<deployment-name>  needs AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY, OPENAI_API_VERSION
  anthropic:claude-sonnet-4-5     needs ANTHROPIC_API_KEY
  google_genai:gemini-2.5-flash   needs GOOGLE_API_KEY
  ollama:llama3.1                 local, needs a tool-calling model
"""
from __future__ import annotations

import os
from functools import lru_cache

from .config import settings


@lru_cache(maxsize=1)
def get_model():
    spec = settings.llm_model
    provider, _, name = spec.partition(":")
    try:
        from langchain.chat_models import init_chat_model
    except ImportError:  # langchain package unavailable: build the provider class directly
        return _direct(provider, name)
    if provider == "azure_openai":
        return init_chat_model(
            name, model_provider="azure_openai", azure_deployment=name,
            api_version=os.getenv("OPENAI_API_VERSION", "2024-10-21"), temperature=settings.llm_temperature,
        )
    return init_chat_model(spec, temperature=settings.llm_temperature)


def _direct(provider: str, name: str):
    t = settings.llm_temperature
    if provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=name, temperature=t)
    if provider == "azure_openai":
        from langchain_openai import AzureChatOpenAI
        return AzureChatOpenAI(azure_deployment=name, api_version=os.getenv("OPENAI_API_VERSION", "2024-10-21"), temperature=t)
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=name, temperature=t)
    raise ValueError(f"Unsupported LLM provider '{provider}' without the langchain package")


def text_of(message) -> str:
    """Message content as plain text (Anthropic returns a list of blocks)."""
    c = getattr(message, "content", message)
    if isinstance(c, str):
        return c
    return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in c)
