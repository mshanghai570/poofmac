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

# ── Provider specifications ───────────────────────────────────────────────────
# One row per provider. The GUI, the CLI wizard and the model picker all read
# this table, so adding a provider means editing it here and nowhere else.
#
#   key            env key / settings attribute suffix
#   label          shown in settings, the picker and the wizard
#   registry       key into MODEL_REGISTRY
#   litellm_prefix prepended to the model id ("" = use the id as-is)
#   model_env      .env key holding the selected model
#   model_attr     Settings attribute holding the selected model
#   key_env        .env key holding the API key ("" = no API key)
#   key_attr       Settings attribute holding the API key
#   auth           "api_key" | "endpoint" | "signin" | "none"
#   cli            executable that signs the user in, for auth="signin"
#   install        shell command shown when that CLI is missing

PROVIDER_SPECS: dict[str, dict[str, str]] = {
    "anthropic": {
        "label": "Anthropic",
        "registry": "anthropic",
        "litellm_prefix": "",
        "model_env": "ANTHROPIC_MODEL",
        "model_attr": "anthropic_model",
        "key_env": "ANTHROPIC_API_KEY",
        "key_attr": "anthropic_api_key",
        "key_placeholder": "sk-ant-…  →  console.anthropic.com",
        "auth": "api_key",
        "cli": "",
        "install": "",
    },
    "openrouter": {
        "label": "OpenRouter",
        "registry": "openrouter",
        "litellm_prefix": "openrouter/",
        "model_env": "OPENROUTER_MODEL",
        "model_attr": "openrouter_model",
        "key_env": "OPENROUTER_API_KEY",
        "key_attr": "openrouter_api_key",
        "key_placeholder": "sk-or-…  →  openrouter.ai",
        "auth": "api_key",
        "cli": "",
        "install": "",
    },
    "openai": {
        "label": "OpenAI",
        "registry": "openai",
        "litellm_prefix": "",
        "model_env": "OPENAI_MODEL",
        "model_attr": "openai_model",
        "key_env": "OPENAI_API_KEY",
        "key_attr": "openai_api_key",
        "key_placeholder": "sk-…  →  platform.openai.com",
        "auth": "api_key",
        "cli": "",
        "install": "",
    },
    "github_copilot": {
        "label": "GitHub Copilot",
        "registry": "github_copilot",
        "litellm_prefix": "github_copilot/",
        "model_env": "GITHUB_COPILOT_MODEL",
        "model_attr": "github_copilot_model",
        "key_env": "",
        "key_attr": "",
        "key_placeholder": "",
        "auth": "signin",
        "cli": "copilot",
        "install": "npm install -g @github/copilot",
    },
    "openai_codex": {
        "label": "OpenAI Codex",
        "registry": "openai_codex",
        "litellm_prefix": "chatgpt/",
        "model_env": "OPENAI_CODEX_MODEL",
        "model_attr": "openai_codex_model",
        "key_env": "",
        "key_attr": "",
        "key_placeholder": "",
        "auth": "signin",
        "cli": "codex",
        "install": "npm install -g @openai/codex",
    },
    "openai_compat": {
        "label": "Custom endpoint",
        "registry": "",
        "litellm_prefix": "openai/",
        "model_env": "OPENAI_COMPAT_MODEL",
        "model_attr": "openai_compat_model",
        "key_env": "OPENAI_COMPAT_API_KEY",
        "key_attr": "openai_compat_api_key",
        "key_placeholder": "optional — local servers need none",
        "auth": "endpoint",
        "cli": "",
        "install": "",
    },
    "ollama_cloud": {
        "label": "Ollama Cloud",
        "registry": "ollama_cloud",
        "litellm_prefix": "ollama/",
        "model_env": "OLLAMA_CLOUD_MODEL",
        "model_attr": "ollama_cloud_model",
        "key_env": "OLLAMA_API_KEY",
        "key_attr": "ollama_api_key",
        "key_placeholder": "ollama.com subscription key",
        "auth": "api_key",
        "cli": "",
        "install": "",
    },
    "ollama_local": {
        "label": "Ollama Local",
        "registry": "ollama_local",
        "litellm_prefix": "ollama/",
        "model_env": "OLLAMA_LOCAL_MODEL",
        "model_attr": "ollama_local_model",
        "key_env": "",
        "key_attr": "",
        "key_placeholder": "",
        "auth": "none",
        "cli": "",
        "install": "",
    },
}

# Selection order in settings and the wizard. "auto" is the fallback mode and
# is not a provider row, so it lives outside the table.
PROVIDER_ORDER = [
    "github_copilot",
    "openai_codex",
    "openai_compat",
    "anthropic",
    "openrouter",
    "openai",
    "ollama_cloud",
    "ollama_local",
]

# Providers that predate per-provider model keys. Existing .env files still
# set PREFERRED_CLOUD_MODEL / PREFERRED_LOCAL_MODEL, so those seed the
# per-provider value when the specific key is absent.
_SHARED_MODEL_ATTRS = {
    "anthropic": "preferred_cloud_model",
    "openrouter": "preferred_cloud_model",
    "openai": "preferred_cloud_model",
    "ollama_cloud": "preferred_local_model",
    "ollama_local": "preferred_local_model",
}


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

    # Per-provider model ids. The legacy PREFERRED_CLOUD_MODEL /
    # PREFERRED_LOCAL_MODEL keys still seed these when a provider's own key
    # is absent, so existing .env files keep working.
    anthropic_model: str = Field(default="", alias="ANTHROPIC_MODEL")
    openrouter_model: str = Field(default="", alias="OPENROUTER_MODEL")
    openai_model: str = Field(default="", alias="OPENAI_MODEL")
    github_copilot_model: str = Field(default="gpt-5.2", alias="GITHUB_COPILOT_MODEL")
    openai_codex_model: str = Field(default="gpt-5.3-codex", alias="OPENAI_CODEX_MODEL")
    openai_compat_model: str = Field(default="", alias="OPENAI_COMPAT_MODEL")
    ollama_cloud_model: str = Field(default="", alias="OLLAMA_CLOUD_MODEL")
    ollama_local_model: str = Field(default="", alias="OLLAMA_LOCAL_MODEL")

    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    openrouter_api_key: str = Field(default="", alias="OPENROUTER_API_KEY")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    ollama_api_key: str = Field(default="", alias="OLLAMA_API_KEY")
    openai_compat_api_key: str = Field(default="", alias="OPENAI_COMPAT_API_KEY")

    openai_compat_base_url: str = Field(default="", alias="OPENAI_COMPAT_BASE_URL")

    preferred_cloud_model: str = Field(default="claude-sonnet-4-6", alias="PREFERRED_CLOUD_MODEL")
    preferred_local_model: str = Field(default="qwen2.5:14b", alias="PREFERRED_LOCAL_MODEL")
    safe_mode: bool = Field(default=False, alias="SAFE_MODE")

    # ── Per-provider accessors ────────────────────────────────────────────────

    def model_for(self, provider: str) -> str:
        """Selected model id for a provider, falling back to the legacy keys."""
        spec = PROVIDER_SPECS.get(provider)
        if spec is None:
            return ""
        model = getattr(self, spec["model_attr"], "").strip()
        if model:
            return model
        legacy = _SHARED_MODEL_ATTRS.get(provider)
        if legacy:
            return getattr(self, legacy, "").strip()
        return ""

    def set_model_for(self, provider: str, model: str) -> None:
        spec = PROVIDER_SPECS.get(provider)
        if spec:
            setattr(self, spec["model_attr"], model)

    def api_key_for(self, provider: str) -> str:
        spec = PROVIDER_SPECS.get(provider)
        return getattr(self, spec["key_attr"], "") if spec and spec["key_attr"] else ""

    def set_api_key(self, provider: str, key: str) -> None:
        spec = PROVIDER_SPECS.get(provider)
        if spec and spec["key_attr"]:
            setattr(self, spec["key_attr"], key)

    def provider_status(self, provider: str) -> tuple[bool, str]:
        """Whether a provider is usable, and a one-line reason when it is not.

        Drives both the per-row status in Settings and the preflight message
        the chat prints before a request, so the wording cannot drift apart.
        """
        spec = PROVIDER_SPECS.get(provider)
        if spec is None:
            return False, f"Unknown provider: {provider}"
        if spec["auth"] == "signin":
            return True, f"Signs in with the {spec['cli']} CLI on first use"
        if spec["auth"] == "endpoint":
            if not self.openai_compat_base_url.strip():
                return False, "Add a Base URL"
            if not self.model_for(provider):
                return False, "Add a model ID"
            return True, f"Ready · {self._compat_host()}"
        if spec["key_env"] and not self.api_key_for(provider):
            return False, f"Add {spec['key_env']}"
        if provider == "ollama_local":
            model = self._detect_ollama_model()
            if not model:
                return False, "No local Ollama model — run: ollama pull " + self.model_for(provider)
            return True, f"Ready · {model}"
        if not self.model_for(provider):
            return False, "Choose a model"
        return True, "Ready"

    # ── Active provider resolution ────────────────────────────────────────────

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
        self.set_model_for(self.get_active_provider(), model)

    def completion_kwargs(self) -> dict:
        if not self._use_openai_compat():
            return {}
        return {
            "api_base": self.openai_compat_base_url.strip().rstrip("/"),
            "api_key": self.openai_compat_api_key.strip() or "not-needed",
        }

    def get_active_model(self) -> tuple[str, str]:
        provider = self.get_active_provider()
        if provider == "openai_compat":
            if not (self.openai_compat_base_url.strip() and self.openai_compat_model.strip()):
                raise RuntimeError(
                    "Custom provider is selected, but OPENAI_COMPAT_BASE_URL and "
                    "OPENAI_COMPAT_MODEL must both be set."
                )
            model = self.openai_compat_model.strip()
            return f"openai/{model}", f"{model} (Custom · {self._compat_host()})"
        if provider in ("anthropic", "openrouter", "openai"):
            spec = PROVIDER_SPECS[provider]
            key = self.api_key_for(provider)
            if not key:
                raise RuntimeError(f"{spec['key_env']} is required for {spec['label']}.")
            import litellm
            if provider == "anthropic":
                litellm.anthropic_key = key
            elif provider == "openrouter":
                litellm.openrouter_key = key
            else:
                litellm.openai_key = key
            model = self.model_for(provider) or MODEL_REGISTRY[spec["registry"]][0][0]
            if provider == "openrouter":
                if "/" not in model:
                    model = f"openrouter/anthropic/{model}"
                elif not model.startswith("openrouter/"):
                    model = f"openrouter/{model}"
                return model, f"{model.split('/')[-1]} (OpenRouter)"
            return model, f"{model} ({spec['label']})"
        if provider in ("github_copilot", "openai_codex"):
            spec = PROVIDER_SPECS[provider]
            model = self.model_for(provider)
            label = "GitHub Copilot" if provider == "github_copilot" else "ChatGPT subscription"
            return f"{spec['litellm_prefix']}{model}", f"{model} ({label})"
        if provider == "ollama_cloud":
            if not self.ollama_api_key:
                raise RuntimeError("OLLAMA_API_KEY is required for Ollama Cloud.")
            model = self.model_for("ollama_cloud")
            return f"ollama/{model}", f"{model} (Ollama Cloud)"
        if provider == "ollama_local":
            model = self._detect_ollama_model()
            if not model or model.endswith("-cloud") or ":cloud" in model:
                raise RuntimeError("Cannot reach a local Ollama model. Start Ollama and pull a model.")
            return f"ollama/{model}", f"{model} (Ollama Local)"
        raise RuntimeError(f"Unknown active provider: {provider}")

    def _compat_host(self) -> str:
        from urllib.parse import urlparse
        raw = self.openai_compat_base_url.strip()
        try:
            return urlparse(raw if "://" in raw else f"//{raw}").netloc or raw
        except ValueError:
            return raw

    def _detect_ollama_model(self) -> Optional[str]:
        """Best local Ollama model: the saved one, else a known one, else any."""
        preferred = self.model_for("ollama_local") or self.preferred_local_model
        tag = preferred.split(":")[-1] if ":" in preferred else ""
        if tag == "cloud" or tag.endswith("-cloud"):
            return preferred
        try:
            proc = subprocess.run(
                ["ollama", "list"], capture_output=True, text=True, timeout=5
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None
        if proc.returncode != 0:
            return None
        available = [line.split()[0] for line in proc.stdout.strip().splitlines()[1:] if line.strip()]
        if not available:
            return None
        family = preferred.split(":")[0]
        for model in available:
            if preferred in model or model.startswith(family):
                return model
        for known in OLLAMA_PREFERRED_ORDER:
            for model in available:
                if known.split(":")[0] in model:
                    return model
        return available[0]

    def validate_model_access(self) -> tuple[bool, str]:
        try:
            _, display = self.get_active_model()
            return True, f"Model ready: {display}"
        except RuntimeError as exc:
            return False, str(exc)
