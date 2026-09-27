# SPDX-License-Identifier: MIT
# Copyright (c) 2026 lesteroliver — https://poofmac.app
"""
Application configuration — model selection and runtime settings.

Priority order for model selection:
  1. ANTHROPIC_API_KEY       → Anthropic direct (fastest, most reliable)
  2. OPENROUTER_API_KEY      → OpenRouter (multi-provider gateway)
  3. OPENAI_API_KEY          → OpenAI direct
  4. OPENAI_COMPAT_BASE_URL  → any OpenAI-compatible endpoint
                                (vLLM, LM Studio, llama.cpp, Ollama /v1,
                                 Groq, DeepSeek, Together, OpenAI proxies …)
  5. Ollama local            → auto-detects best available model

Reads from .env file via pydantic-settings.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# ── Centralized model registry ────────────────────────────────────────────────
# Used by the GUI for the Settings dialog and model picker.
# Tuple format: (litellm_model_string, display_label)

MODEL_REGISTRY: dict[str, list[tuple[str, str]]] = {
    "anthropic": [
        ("claude-opus-4-7",           "Claude Opus 4.7     — most capable"),
        ("claude-sonnet-4-6",         "Claude Sonnet 4.6   — recommended ★"),
        ("claude-haiku-4-5",          "Claude Haiku 4.5    — fastest / cheapest"),
        ("claude-3-7-sonnet-20250219","Claude 3.7 Sonnet   — balanced"),
        ("claude-3-5-haiku-20241022", "Claude 3.5 Haiku    — legacy fast"),
    ],
    "openai": [
        ("gpt-5.5",         "GPT-5.5          — latest flagship"),
        ("gpt-5.4",         "GPT-5.4          — strong balance"),
        ("gpt-5.4-mini",    "GPT-5.4 mini     — fast / affordable"),
        ("gpt-4o",          "GPT-4o           — proven reliable"),
        ("o4-mini",         "o4-mini          — reasoning"),
    ],
    "openrouter": [
        ("anthropic/claude-sonnet-4-6",       "Claude Sonnet 4.6  (Anthropic)"),
        ("openai/gpt-5.4",                    "GPT-5.4            (OpenAI)"),
        ("moonshotai/kimi-k2",                "Kimi K2            (Moonshot)"),
        ("google/gemini-3-flash-preview",     "Gemini 3 Flash     (Google)"),
        ("deepseek/deepseek-r1",              "DeepSeek R1        (DeepSeek)"),
        ("meta-llama/llama-4-maverick",       "Llama 4 Maverick   (Meta)"),
    ],
    "ollama_cloud": [
        ("deepseek-v4-flash:cloud",   "DeepSeek V4 Flash   — fast, free tier"),
        ("deepseek-v4-pro:cloud",     "DeepSeek V4 Pro     — stronger"),
        ("gemma4:31b-cloud",          "Gemma 4 31B         — reliable tool-call"),
        ("qwen3.5:cloud",             "Qwen3.5             — great reasoning"),
        ("glm-5.1:cloud",             "GLM-5.1             — Chinese/English"),
        ("kimi-k2.6:cloud",           "Kimi K2.6           — 32B, strong"),
        ("nemotron-3-nano:cloud",     "Nemotron-3 Nano     — tiny & fast"),
        ("ministral-3:cloud",         "Ministral-3         — Mistral nano"),
    ],
    "ollama_local": [
        # 20–40 B range — reliably handle multi-step tool chains
        ("qwen3.6:35b-a3b",   "Qwen3.6 35B-A3B  (3B active MoE)  ~24 GB RAM"),
        ("qwen3.6:27b",       "Qwen3.6 27B                        ~17 GB RAM"),
        ("qwen2.5:32b",       "Qwen2.5 32B                        ~20 GB RAM"),
        ("mistral-small:24b", "Mistral Small 24B                  ~15 GB RAM"),
        # Minimum viable (14 B+)
        ("qwen2.5:14b",       "Qwen2.5 14B                         ~9 GB RAM"),
        ("llama3.1:8b",       "Llama 3.1 8B      (minimum)          ~5 GB RAM"),
    ],
}

# Fallback preference order for auto-detecting best LOCAL Ollama model
OLLAMA_PREFERRED_ORDER = [m for m, _ in MODEL_REGISTRY["ollama_local"]]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Cloud keys
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    openrouter_api_key: str = Field(default="", alias="OPENROUTER_API_KEY")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    ollama_api_key: str = Field(default="", alias="OLLAMA_API_KEY")

    # OpenAI-compatible endpoint (any service speaking the OpenAI
    # chat-completions API — vLLM, LM Studio, llama.cpp server, Ollama's
    # /v1, Groq, DeepSeek, Together, an OpenAI-base-URL proxy, …).
    # Only BASE_URL is required; the key may be a dummy for local servers.
    openai_compat_base_url: str = Field(default="", alias="OPENAI_COMPAT_BASE_URL")
    openai_compat_api_key: str = Field(default="", alias="OPENAI_COMPAT_API_KEY")
    openai_compat_model: str = Field(default="", alias="OPENAI_COMPAT_MODEL")

    # Model preferences
    preferred_cloud_model: str = Field(
        default="claude-sonnet-4-6", alias="PREFERRED_CLOUD_MODEL"
    )
    preferred_local_model: str = Field(
        default="qwen2.5:14b", alias="PREFERRED_LOCAL_MODEL"
    )

    # Safety
    safe_mode: bool = Field(default=False, alias="SAFE_MODE")

    def _use_openai_compat(self) -> bool:
        """True when the OpenAI-compatible endpoint is the selected provider."""
        return bool(
            self.openai_compat_base_url.strip()
            and self.openai_compat_model.strip()
            and not self.anthropic_api_key
            and not self.openrouter_api_key
            and not self.openai_api_key
        )

    def set_model_override(self, model: str) -> None:
        """
        Apply a one-run model override (--model). Routes to whichever setting
        the active provider reads its model from.
        """
        if self._use_openai_compat():
            self.openai_compat_model = model
        else:
            self.preferred_local_model = model

    def completion_kwargs(self) -> dict:
        """
        Extra keyword arguments for litellm.completion() for the active
        provider. Only the OpenAI-compatible endpoint needs any — it must be
        told where to connect. Returns {} for every other provider.
        """
        if not self._use_openai_compat():
            return {}
        return {
            "api_base": self.openai_compat_base_url.strip().rstrip("/"),
            # Local servers ignore the key, but litellm requires one for
            # openai/* models — send a harmless placeholder instead.
            "api_key": self.openai_compat_api_key.strip() or "not-needed",
        }

    def get_active_model(self) -> tuple[str, str]:
        """
        Returns (litellm_model_string, display_name).
        Raises RuntimeError if no model can be found.
        """
        if self.anthropic_api_key:
            import litellm
            litellm.anthropic_key = self.anthropic_api_key
            model = self.preferred_cloud_model
            return model, f"{model} (Anthropic)"

        if self.openrouter_api_key:
            import litellm
            litellm.openrouter_key = self.openrouter_api_key
            model = self.preferred_cloud_model
            # Ensure the openrouter/ prefix is present
            if "/" not in model:
                # Bare model name — wrap with anthropic/ as sensible default
                model = f"openrouter/anthropic/{model}"
            elif not model.startswith("openrouter/"):
                model = f"openrouter/{model}"
            display = model.split("/")[-1]
            return model, f"{display} (OpenRouter)"

        if self.openai_api_key:
            import litellm
            litellm.openai_key = self.openai_api_key
            model = self.preferred_cloud_model or "gpt-4o"
            return model, f"{model} (OpenAI)"

        if self._use_openai_compat():
            # litellm routes openai/<model> through the OpenAI-compatible
            # Chat Completions handler, so api_base/api_key from
            # completion_kwargs() point it at the custom endpoint.
            model = self.openai_compat_model.strip()
            host = self._compat_host()
            return f"openai/{model}", f"{model} (Custom · {host})"

        # Try Ollama
        local = self._detect_ollama_model()
        if local:
            return f"ollama/{local}", f"{local} (Ollama)"

        raise RuntimeError(
            "No model configured and Ollama not found.\n\n"
            "Options:\n"
            "  1. Add ANTHROPIC_API_KEY to .env  (recommended)\n"
            "  2. Add OPENROUTER_API_KEY to .env\n"
            "  3. Add OPENAI_COMPAT_BASE_URL + OPENAI_COMPAT_MODEL to .env\n"
            "     (any OpenAI-compatible server: vLLM, LM Studio, Groq, …)\n"
            "  4. Install Ollama: https://ollama.com  then:\n"
            f"     ollama pull {self.preferred_local_model}"
        )

    def _compat_host(self) -> str:
        """Host:port of the custom endpoint, for display purposes."""
        from urllib.parse import urlparse

        raw = self.openai_compat_base_url.strip()
        try:
            return urlparse(raw if "://" in raw else f"//{raw}").netloc or raw
        except ValueError:
            return raw

    def _detect_ollama_model(self) -> Optional[str]:
        """
        Detect the best available Ollama model, or None if Ollama is absent.

        Cloud models (e.g. deepseek-v4-pro:cloud) are served by Ollama's
        infrastructure and never appear in `ollama list`. We detect them by
        verifying the Ollama server is reachable, then trusting the preference.
        """
        try:
            subprocess.run(
                ["ollama", "list"], capture_output=True, text=True, timeout=5
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None

        preferred = self.preferred_local_model

        # Cloud model: tag is "cloud" or ends with "-cloud"
        tag = preferred.split(":")[-1] if ":" in preferred else ""
        if tag == "cloud" or tag.endswith("-cloud"):
            return preferred

        # Local model: look in ollama list
        try:
            proc = subprocess.run(
                ["ollama", "list"], capture_output=True, text=True, timeout=5
            )
            if proc.returncode != 0:
                return None
            lines = proc.stdout.strip().splitlines()[1:]
            available = [line.split()[0] for line in lines if line.strip()]
            if not available:
                return None

            for model in available:
                if preferred in model or model.startswith(preferred.split(":")[0]):
                    return model

            for fallback in OLLAMA_PREFERRED_ORDER:
                for model in available:
                    if fallback.split(":")[0] in model:
                        return model

            return available[0]
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None

    def validate_model_access(self) -> tuple[bool, str]:
        """Returns (ok: bool, message: str)."""
        try:
            _, display = self.get_active_model()
            return True, f"Model ready: {display}"
        except RuntimeError as e:
            return False, str(e)
