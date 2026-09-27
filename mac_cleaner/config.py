# SPDX-License-Identifier: MIT
# Copyright (c) 2026 lesteroliver — https://poofmac.app
"""
Application configuration — model selection and runtime settings.

Provider selection can be automatic (legacy credential priority) or explicit.
Reads from .env file via pydantic-settings.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# ── Centralized model registry ────────────────────────────────────────────────
# Tuple format: (model id, display label).

MODEL_REGISTRY: dict[str, list[tuple[str, str]]] = {
    "anthropic": [
        ("claude-opus-4-7", "Claude Opus 4.7     — most capable"),
        ("claude-sonnet-4-6", "Claude Sonnet 4.6   — recommended ★"),
        ("claude-haiku-4-5", "Claude Haiku 4.5    — fastest / cheapest"),
        ("claude-3-7-sonnet-20250219", "Claude 3.7 Sonnet   — balanced"),
        ("claude-3-5-haiku-20241022", "Claude 3.5 Haiku    — legacy fast"),
    ],
    "openai": [
        ("gpt-5.5", "GPT-5.5          — latest flagship"),
        ("gpt-5.4", "GPT-5.4          — strong balance"),
        ("gpt-5.4-mini", "GPT-5.4 mini     — fast / affordable"),
        ("gpt-4o", "GPT-4o           — proven reliable"),
        ("o4-mini", "o4-mini          — reasoning"),
    ],
    "github_copilot": [
        ("gpt-5.2", "GPT-5.2  (GitHub Copilot subscription)"),
        ("claude-sonnet-4.6", "Claude Sonnet 4.6  (GitHub Copilot subscription)"),
        ("gemini-2.5-pro", "Gemini 2.5 Pro  (GitHub Copilot subscription)"),
    ],
    "openai_codex": [
        ("gpt-5.3-codex", "GPT-5.3 Codex  (ChatGPT subscription)"),
        ("gpt-5.3-codex-spark", "GPT-5.3 Codex Spark  (ChatGPT subscription)"),
        ("gpt-5.4", "GPT-5.4  (ChatGPT subscription)"),
    ],
    "openrouter": [
        ("anthropic/claude-sonnet-4-6", "Claude Sonnet 4.6  (Anthropic)"),
        ("openai/gpt-5.4", "GPT-5.4            (OpenAI)"),
        ("moonshotai/kimi-k2", "Kimi K2            (Moonshot)"),
        ("google/gemini-3-flash-preview", "Gemini 3 Flash     (Google)"),
        ("deepseek/deepseek-r1", "DeepSeek R1        (DeepSeek)"),
        ("meta-llama/llama-4-maverick", "Llama 4 Maverick   (Meta)"),
    ],
    "ollama_cloud": [
        ("deepseek-v4-flash:cloud", "DeepSeek V4 Flash   — fast, free tier"),
        ("deepseek-v4-pro:cloud", "DeepSeek V4 Pro     — stronger"),
        ("gemma4:31b-cloud", "Gemma 4 31B         — reliable tool-call"),
        ("qwen3.5:cloud", "Qwen3.5             — great reasoning"),
        ("glm-5.1:cloud", "GLM-5.1             — Chinese/English"),
        ("kimi-k2.6:cloud", "Kimi K2.6           — 32B, strong"),
        ("nemotron-3-nano:cloud", "Nemotron-3 Nano     — tiny & fast"),
        ("ministral-3:cloud", "Ministral-3         — Mistral nano"),
    ],
    "ollama_local": [
        ("qwen3.6:35b-a3b", "Qwen3.6 35B-A3B  (3B active MoE)  ~24 GB RAM"),
        ("qwen3.6:27b", "Qwen3.6 27B                        ~17 GB RAM"),
        ("qwen2.5:32b", "Qwen2.5 32B                        ~20 GB RAM"),
        ("mistral-small:24b", "Mistral Small 24B                  ~15 GB RAM"),
        ("qwen2.5:14b", "Qwen2.5 14B                         ~9 GB RAM"),
        ("llama3.1:8b", "Llama 3.1 8B      (minimum)          ~5 GB RAM"),
    ],
}

OLLAMA_PREFERRED_ORDER = [m for m, _ in MODEL_REGISTRY["ollama_local"]]


def discover_openai_compat_models(
    base_url: str, api_key: str = "", timeout: float = 8.0
) -> list[str]:
    """Return model IDs from an OpenAI-compatible models endpoint, if available."""
    base = base_url.strip().rstrip("/")
    if not base:
        return []
    request = Request(
        f"{base}/models",
        headers={
            "Authorization": f"Bearer {api_key.strip() or 'not-needed'}",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError, ValueError):
        return []
    data = payload.get("data", []) if isinstance(payload, dict) else []
    return sorted(
        {
            item["id"].strip()
            for item in data
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and item["id"].strip()
        },
        key=str.casefold,
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    active_provider: str = Field(default="auto", alias="ACTIVE_PROVIDER")
    github_copilot_model: str = Field(default="gpt-5.2", alias="GITHUB_COPILOT_MODEL")
    openai_codex_model: str = Field(default="gpt-5.3-codex", alias="OPENAI_CODEX_MODEL")

    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    openrouter_api_key: str = Field(default="", alias="OPENROUTER_API_KEY")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    ollama_api_key: str = Field(default="", alias="OLLAMA_API_KEY")

    openai_compat_base_url: str = Field(default="", alias="OPENAI_COMPAT_BASE_URL")
    openai_compat_api_key: str = Field(default="", alias="OPENAI_COMPAT_API_KEY")
    openai_compat_model: str = Field(default="", alias="OPENAI_COMPAT_MODEL")

    preferred_cloud_model: str = Field(default="claude-sonnet-4-6", alias="PREFERRED_CLOUD_MODEL")
    preferred_local_model: str = Field(default="qwen2.5:14b", alias="PREFERRED_LOCAL_MODEL")
    safe_mode: bool = Field(default=False, alias="SAFE_MODE")

    def _use_openai_compat(self) -> bool:
        configured = bool(self.openai_compat_base_url.strip() and self.openai_compat_model.strip())
        if self.active_provider == "openai_compat":
            return configured
        return bool(
            self.active_provider == "auto"
            and configured
            and not self.anthropic_api_key
            and not self.openrouter_api_key
            and not self.openai_api_key
        )

    def get_active_provider(self) -> str:
        if self.active_provider != "auto":
            return self.active_provider
        if self.anthropic_api_key:
            return "anthropic"
        if self.openrouter_api_key:
            return "openrouter"
        if self.openai_api_key:
            return "openai"
        if self._use_openai_compat():
            return "openai_compat"
        local = self.preferred_local_model
        return "ollama_cloud" if local.endswith("-cloud") or ":cloud" in local else "ollama_local"

    def set_model_override(self, model: str) -> None:
        provider = self.get_active_provider()
        if provider == "github_copilot":
            self.github_copilot_model = model
        elif provider == "openai_codex":
            self.openai_codex_model = model
        elif provider == "openai_compat":
            self.openai_compat_model = model
        elif provider in ("anthropic", "openrouter", "openai"):
            self.preferred_cloud_model = model
        else:
            self.preferred_local_model = model

    def completion_kwargs(self) -> dict:
        if not self._use_openai_compat():
            return {}
        return {
            "api_base": self.openai_compat_base_url.strip().rstrip("/"),
            "api_key": self.openai_compat_api_key.strip() or "not-needed",
        }

    def get_active_model(self) -> tuple[str, str]:
        provider = self.get_active_provider()
        if provider == "github_copilot":
            model = self.github_copilot_model.strip() or "gpt-5.2"
            return f"github_copilot/{model}", f"{model} (GitHub Copilot)"
        if provider == "openai_codex":
            model = self.openai_codex_model.strip() or "gpt-5.3-codex"
            return f"chatgpt/{model}", f"{model} (ChatGPT subscription)"
        if provider == "openai_compat":
            if not (self.openai_compat_base_url.strip() and self.openai_compat_model.strip()):
                raise RuntimeError(
                    "Custom provider is selected, but OPENAI_COMPAT_BASE_URL and "
                    "OPENAI_COMPAT_MODEL must both be set."
                )
            model = self.openai_compat_model.strip()
            return f"openai/{model}", f"{model} (Custom · {self._compat_host()})"
        if provider in ("anthropic", "openrouter", "openai"):
            key_name = {
                "anthropic": "ANTHROPIC_API_KEY",
                "openrouter": "OPENROUTER_API_KEY",
                "openai": "OPENAI_API_KEY",
            }[provider]
            key = {
                "anthropic": self.anthropic_api_key,
                "openrouter": self.openrouter_api_key,
                "openai": self.openai_api_key,
            }[provider]
            if not key:
                raise RuntimeError(f"{key_name} is required for the selected provider.")
            import litellm
            if provider == "anthropic":
                litellm.anthropic_key = key
            elif provider == "openrouter":
                litellm.openrouter_key = key
            else:
                litellm.openai_key = key
            model = self.preferred_cloud_model
            if provider == "openai" and not model:
                model = "gpt-4o"
            if provider == "openrouter":
                if "/" not in model:
                    model = f"openrouter/anthropic/{model}"
                elif not model.startswith("openrouter/"):
                    model = f"openrouter/{model}"
                return model, f"{model.split('/')[-1]} (OpenRouter)"
            return model, f"{model} ({provider.capitalize()})"
        if provider == "ollama_cloud":
            if not self.ollama_api_key:
                raise RuntimeError("OLLAMA_API_KEY is required for Ollama Cloud.")
            model = self.preferred_local_model
            return f"ollama/{model}", f"{model} (Ollama Cloud)"
        if provider == "ollama_local":
            model = self._detect_ollama_model()
            if not model or model.endswith("-cloud") or ":cloud" in model:
                raise RuntimeError("Cannot reach a local Ollama model. Start Ollama and pull a model.")
            return f"ollama/{model}", f"{model} (Ollama Local)"
        if provider != "auto":
            raise RuntimeError(f"Unknown active provider: {provider}")

        # Automatic uses the legacy priority: Anthropic, OpenRouter, OpenAI,
        # OpenAI-compatible endpoint, then Ollama.
        if self.anthropic_api_key:
            import litellm
            litellm.anthropic_key = self.anthropic_api_key
            return self.preferred_cloud_model, f"{self.preferred_cloud_model} (Anthropic)"
        if self.openrouter_api_key:
            import litellm
            litellm.openrouter_key = self.openrouter_api_key
            model = self.preferred_cloud_model
            if "/" not in model:
                model = f"openrouter/anthropic/{model}"
            elif not model.startswith("openrouter/"):
                model = f"openrouter/{model}"
            return model, f"{model.split('/')[-1]} (OpenRouter)"
        if self.openai_api_key:
            import litellm
            litellm.openai_key = self.openai_api_key
            model = self.preferred_cloud_model or "gpt-4o"
            return model, f"{model} (OpenAI)"
        if self._use_openai_compat():
            model = self.openai_compat_model.strip()
            return f"openai/{model}", f"{model} (Custom · {self._compat_host()})"
        model = self._detect_ollama_model()
        if model:
            return f"ollama/{model}", f"{model} (Ollama)"
        raise RuntimeError(
            "No model configured and Ollama not found.\n\n"
            "Options:\n"
            "  1. Add ANTHROPIC_API_KEY to .env\n"
            "  2. Add OPENROUTER_API_KEY or OPENAI_API_KEY to .env\n"
            "  3. Add OPENAI_COMPAT_BASE_URL + OPENAI_COMPAT_MODEL to .env\n"
            "  4. Install Ollama and pull a model: ollama pull "
            f"{self.preferred_local_model}"
        )

    def _compat_host(self) -> str:
        from urllib.parse import urlparse
        raw = self.openai_compat_base_url.strip()
        try:
            return urlparse(raw if "://" in raw else f"//{raw}").netloc or raw
        except ValueError:
            return raw

    def _detect_ollama_model(self) -> Optional[str]:
        try:
            subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=5)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None
        preferred = self.preferred_local_model
        tag = preferred.split(":")[-1] if ":" in preferred else ""
        if tag == "cloud" or tag.endswith("-cloud"):
            return preferred
        try:
            proc = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=5)
            if proc.returncode != 0:
                return None
            available = [line.split()[0] for line in proc.stdout.strip().splitlines()[1:] if line.strip()]
            for model in available:
                if preferred in model or model.startswith(preferred.split(":")[0]):
                    return model
            for fallback in OLLAMA_PREFERRED_ORDER:
                for model in available:
                    if fallback.split(":")[0] in model:
                        return model
            return available[0] if available else None
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None

    def validate_model_access(self) -> tuple[bool, str]:
        try:
            _, display = self.get_active_model()
            return True, f"Model ready: {display}"
        except RuntimeError as exc:
            return False, str(exc)
