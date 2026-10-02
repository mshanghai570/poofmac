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
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from mac_cleaner import store

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
    "kilo": [
        ("kilo-auto/free", "Auto Free          — routes to a free model, recommended ★"),
        ("nvidia/nemotron-3-super-120b-a12b:free", "Nemotron 3 Super   — 120B MoE, strong"),
        ("qwen/qwen3.8-27b:free", "Qwen3.8 27B        — reliable tool-calls"),
        ("inclusionai/ling-3.0-flash-fin:free", "Ling 3.0 Flash Fin — fast"),
        ("stepfun/step-3.7-flash:free", "Step 3.7 Flash     — fast"),
        ("thinkingmachines/inkling-small:free", "Inkling Small      — compact"),
        ("cohere/north-mini-code:free", "North Mini Code    — Cohere"),
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
    "kilo": {
        "label": "Kilo Gateway (free)",
        "registry": "kilo",
        "litellm_prefix": "",
        "model_env": "KILO_MODEL",
        "model_attr": "kilo_model",
        "key_env": "KILO_API_KEY",
        "key_attr": "kilo_api_key",
        "key_placeholder": "optional — the free ids work with no key",
        "auth": "none",
        "cli": "",
        "install": "",
    },
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

# Custom OpenAI-compatible endpoints are not rows in PROVIDER_SPECS — the user
# can add any number of them, each with its own URL, key and model list — so
# every saved endpoint becomes a provider id of the form "custom:<endpoint id>".
# "openai_compat" survives as the alias for "whichever custom endpoint is
# active", which is what older .env files set.
CUSTOM_PREFIX = "custom:"
CUSTOM_ALIAS = "openai_compat"


def is_custom_provider(provider_id: str) -> bool:
    """Whether a provider id names a saved custom endpoint."""
    return provider_id.startswith(CUSTOM_PREFIX) and len(provider_id) > len(CUSTOM_PREFIX)


def custom_endpoint_id(provider_id: str) -> str:
    """The endpoint id inside a ``custom:<id>`` provider id."""
    return provider_id[len(CUSTOM_PREFIX):]


# Selection order in settings and the wizard. "auto" is the fallback mode and
# is not a provider row, so it lives outside the table.
PROVIDER_ORDER = [
    "github_copilot",
    "openai_codex",
    "openai_compat",
    "kilo",
    "anthropic",
    "openrouter",
    "openai",
    "ollama_cloud",
    "ollama_local",
]

def provider_label(provider_id: str) -> str:
    """Display name for any provider id, custom endpoints included."""
    if is_custom_provider(provider_id):
        endpoint = store.find_endpoint(custom_endpoint_id(provider_id))
        return endpoint["name"] if endpoint else "Custom endpoint"
    spec = PROVIDER_SPECS.get(provider_id)
    return spec["label"] if spec else provider_id


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


def _model_ids(payload: Any) -> list[str]:
    """Model ids from any of the shapes gateways use for a models listing."""
    items: Any = payload
    if isinstance(payload, dict):
        items = []
        for key in ("data", "models", "items", "result"):
            if isinstance(payload.get(key), list):
                items = payload[key]
                break
    if not isinstance(items, list):
        return []
    found: set[str] = set()
    for item in items:
        candidate = ""
        if isinstance(item, str):
            candidate = item
        elif isinstance(item, dict):
            for key in ("id", "model", "name", "slug"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    candidate = value
                    break
        if candidate.strip():
            found.add(candidate.strip())
    # Two ids differing only in case ("A" / "a") tie on casefold, and a set's
    # iteration order is not stable across runs — break the tie on the id
    # itself so the listing a user sees is always in the same order.
    return sorted(found, key=lambda model: (model.casefold(), model))


def discover_models(
    base_url: str, api_key: str = "", timeout: float = 8.0
) -> tuple[list[str], str]:
    """Ask an endpoint which models it hosts.

    Returns ``(model_ids, error)``. On success the error is empty — including
    when the endpoint answers with an empty list, which is a real (if useless)
    answer. The GUI needs the reason to tell the user *why* it showed nothing,
    which is how a mistyped base URL stops looking like a broken app.
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return [], "Add a Base URL first."
    # Most gateways serve /v1/models; some serve /models. Try both before
    # giving up, so either form of base URL works as typed.
    urls = [f"{base}/models"]
    if base.endswith("/v1"):
        # ...and some put the catalogue beside the OpenAI root rather than
        # under it: a Base URL of ".../api/gateway/v1" lists models at
        # ".../api/gateway/models". Gateways name that path in their own
        # 404s, so a user following the endpoint's advice is not left stuck
        # with an empty model list.
        parent = base[: -len("/v1")].rstrip("/")
        if parent:
            urls.append(f"{parent}/models")
    else:
        urls.append(f"{base}/v1/models")

    last_error = ""
    for url in urls:
        # A keyless gateway must receive NO Authorization header at all: Kilo
        # answers 401 to "Bearer not-needed" but 200 to a missing header, and
        # other gateways behave the same. Only send one when a key exists.
        headers = {"Accept": "application/json"}
        if (api_key or "").strip():
            headers["Authorization"] = f"Bearer {api_key.strip()}"
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except HTTPError as exc:
            last_error = f"HTTP {exc.code} from {url}"
        except URLError as exc:
            last_error = f"Cannot reach {url} — {exc.reason}"
        except TimeoutError:
            last_error = f"{url} did not answer within {timeout:.0f}s"
        except (OSError, ValueError) as exc:
            last_error = f"Unreadable response from {url} — {exc}"
        else:
            models = _model_ids(payload)
            if models:
                return models, ""
            last_error = f"{url} returned no model ids"
    return [], f"{last_error} — tried {len(urls)} paths from {base}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(store.env_file()),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    def __init__(self, **values: Any) -> None:
        # Resolve the .env at construction time, not at import time: the
        # bundled app's working directory is "/", where a relative path fails.
        values.setdefault("_env_file", str(store.env_file()))
        super().__init__(**values)

    active_provider: str = Field(default="auto", alias="ACTIVE_PROVIDER")

    # Per-provider model ids. The legacy PREFERRED_CLOUD_MODEL /
    # PREFERRED_LOCAL_MODEL keys still seed these when a provider's own key
    # is absent, so existing .env files keep working.
    kilo_model: str = Field(default="kilo-auto/free", alias="KILO_MODEL")
    kilo_api_key: str = Field(default="", alias="KILO_API_KEY")
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

    # ── Custom endpoint accessors ─────────────────────────────────────────────

    def endpoints(self, *, refresh: bool = False) -> list[dict]:
        """Every saved custom endpoint, in the order the user added them."""
        return store.list_endpoints(refresh=refresh)

    def endpoint_for(self, provider: str) -> Optional[dict]:
        """The saved custom endpoint a provider id refers to, if any."""
        if is_custom_provider(provider):
            return store.find_endpoint(custom_endpoint_id(provider))
        if provider == CUSTOM_ALIAS:
            return store.active_endpoint()
        return None

    def select_endpoint(self, endpoint_id: str) -> None:
        """Make a saved custom endpoint the one new requests use."""
        store.set_active_endpoint(endpoint_id)
        self.active_provider = f"{CUSTOM_PREFIX}{endpoint_id}"

    @staticmethod
    def endpoint_status(endpoint: Optional[dict]) -> tuple[bool, str]:
        """Whether the given endpoint record is usable, and why not.

        Shared by the settings page (for the record being edited) and by
        :meth:`provider_status`, so the two can never disagree.
        """
        if not endpoint:
            return False, "Add a custom endpoint"
        if not str(endpoint.get("base_url", "")).strip():
            return False, "Add a Base URL"
        if not str(endpoint.get("model", "")).strip():
            return False, "Choose a model"
        return True, f"Ready · {store.host_label(endpoint['base_url'])}"

    # ── Per-provider accessors ────────────────────────────────────────────────

    def model_for(self, provider: str) -> str:
        """Selected model id for a provider, falling back to the legacy keys."""
        endpoint = self.endpoint_for(provider)
        if endpoint is not None:
            return str(endpoint.get("model", "")).strip()
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
        endpoint = self.endpoint_for(provider)
        if endpoint is not None:
            store.upsert_endpoint({**endpoint, "model": model}, make_active=False)
            return
        spec = PROVIDER_SPECS.get(provider)
        if spec:
            setattr(self, spec["model_attr"], model)

    def api_key_for(self, provider: str) -> str:
        endpoint = self.endpoint_for(provider)
        if endpoint is not None:
            return str(endpoint.get("api_key", ""))
        spec = PROVIDER_SPECS.get(provider)
        return getattr(self, spec["key_attr"], "") if spec and spec["key_attr"] else ""

    def set_api_key(self, provider: str, key: str) -> None:
        endpoint = self.endpoint_for(provider)
        if endpoint is not None:
            store.upsert_endpoint({**endpoint, "api_key": key}, make_active=False)
            return
        spec = PROVIDER_SPECS.get(provider)
        if spec and spec["key_attr"]:
            setattr(self, spec["key_attr"], key)

    def provider_status(self, provider: str) -> tuple[bool, str]:
        """Whether a provider is usable, and a one-line reason when it is not.

        Drives both the per-row status in Settings and the preflight message
        the chat prints before a request, so the wording cannot drift apart.
        """
        spec = PROVIDER_SPECS.get(provider)
        if spec is None and not is_custom_provider(provider):
            return False, f"Unknown provider: {provider}"
        if spec is not None and spec["auth"] == "endpoint":
            return self.endpoint_status(self.endpoint_for(provider))
        if spec is None:  # a custom:<id> provider id
            return self.endpoint_status(self.endpoint_for(provider))
        if spec["auth"] == "signin":
            return True, f"Signs in with the {spec['cli']} CLI on first use"
        if spec["key_env"] and not self.api_key_for(provider):
            if provider == "kilo":
                return True, "Ready · free ids need no key"
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

    def _custom_endpoint_ready(self) -> bool:
        endpoint = store.active_endpoint()
        return bool(
            endpoint
            and str(endpoint.get("base_url", "")).strip()
            and str(endpoint.get("model", "")).strip()
        )

    def get_active_provider(self) -> str:
        if self.active_provider != "auto":
            # A pinned custom endpoint can be removed from another window; fall
            # back to automatic resolution rather than failing every request.
            if is_custom_provider(self.active_provider) and (
                self.endpoint_for(self.active_provider) is None
            ):
                return self._auto_provider()
            return self.active_provider
        return self._auto_provider()

    def _auto_provider(self) -> str:
        """First fully configured provider, in the order the UI documents."""
        if self.anthropic_api_key:
            return "anthropic"
        if self.openrouter_api_key:
            return "openrouter"
        if self.openai_api_key:
            return "openai"
        if self._custom_endpoint_ready():
            return CUSTOM_ALIAS
        local = self.preferred_local_model
        if local.endswith("-cloud") or ":cloud" in local:
            # Ollama Cloud needs a key; without one, Kilo still works.
            return "ollama_cloud" if self.ollama_api_key else "kilo"
        # Kilo's free ids need no key, so they are the one provider that is
        # always ready — a fresh install works before anything is configured,
        # and a Mac without Ollama no longer dead-ends.
        return "ollama_local" if self._detect_ollama_model() else "kilo"

    def set_model_override(self, model: str) -> None:
        self.set_model_for(self.get_active_provider(), model)

    def completion_kwargs(self) -> dict:
        """api_base/api_key for a custom endpoint; empty for every other provider.

        For a keyless endpoint (Kilo free ids, bare local servers) the dict
        carries ``extra_headers`` that blank the Authorization header: the
        OpenAI SDK always sends one, and Kilo rejects its "not-needed"
        placeholder with 401 while accepting a missing header with 200.
        """
        if self.get_active_provider() == "kilo":
            key = self.kilo_api_key.strip()
            if key:
                return {
                    "api_base": "https://api.kilo.ai/api/gateway",
                    "api_key": key,
                }
            return {
                "api_base": "https://api.kilo.ai/api/gateway",
                # LiteLLM refuses an empty api_key client-side ("Missing
                # credentials"), so a placeholder still travels — but the
                # blanked header wins at the HTTP layer.
                "api_key": "not-needed",
                "extra_headers": {"Authorization": ""},
            }
        endpoint = self.endpoint_for(self.get_active_provider())
        if endpoint is None:
            return {}
        key = str(endpoint.get("api_key", "")).strip()
        if key:
            return {
                "api_base": str(endpoint.get("base_url", "")).strip().rstrip("/"),
                "api_key": key,
            }
        return {
            "api_base": str(endpoint.get("base_url", "")).strip().rstrip("/"),
            "api_key": "not-needed",
            "extra_headers": {"Authorization": ""},
        }

    def get_active_model(self) -> tuple[str, str]:
        provider = self.get_active_provider()
        endpoint = self.endpoint_for(provider)
        if endpoint is not None:
            base_url = str(endpoint.get("base_url", "")).strip()
            model = str(endpoint.get("model", "")).strip()
            if not (base_url and model):
                name = endpoint.get("name") or "Custom endpoint"
                raise RuntimeError(
                    f"\"{name}\" needs a Base URL and a model. "
                    "Open Settings (⚙) → Custom endpoints to finish it."
                )
            # The model id reaches the endpoint verbatim. LiteLLM only uses the
            # "openai/" prefix to pick the OpenAI-compatible transport, so
            # gateway ids keep their slashes and suffixes exactly as typed
            # ("nex-agi/nex-n2.5-pro:free" → "nex-agi/nex-n2.5-pro:free").
            return f"openai/{model}", f"{model} ({endpoint.get('name') or store.host_label(base_url)})"
        if provider == "kilo":
            model = self.model_for("kilo") or "kilo-auto/free"
            return f"openai/{model}", f"{model} (Kilo Gateway)"
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
