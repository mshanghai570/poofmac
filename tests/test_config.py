"""Tests for provider selection, model resolution and credential handling."""

from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError

import pytest

from mac_cleaner import config as config_mod
from mac_cleaner import store
from mac_cleaner.config import (
    CUSTOM_ALIAS,
    CUSTOM_PREFIX,
    MODEL_REGISTRY,
    OLLAMA_PREFERRED_ORDER,
    PROVIDER_ORDER,
    PROVIDER_SPECS,
    Settings,
    _model_ids,
    custom_endpoint_id,
    discover_models,
    is_custom_provider,
    provider_label,
)

# Every .env key Settings knows about. Cleared before each test so a developer
# machine with real credentials in its environment cannot change the result.
ENV_KEYS = [
    "ACTIVE_PROVIDER",
    "KILO_MODEL",
    "KILO_API_KEY",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_MODEL",
    "OPENROUTER_API_KEY",
    "OPENAI_MODEL",
    "OPENAI_API_KEY",
    "GITHUB_COPILOT_MODEL",
    "OPENAI_CODEX_MODEL",
    "OPENAI_COMPAT_MODEL",
    "OPENAI_COMPAT_API_KEY",
    "OPENAI_COMPAT_BASE_URL",
    "OLLAMA_CLOUD_MODEL",
    "OLLAMA_LOCAL_MODEL",
    "OLLAMA_API_KEY",
    "PREFERRED_CLOUD_MODEL",
    "PREFERRED_LOCAL_MODEL",
    "SAFE_MODE",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def make_settings(tmp_path, monkeypatch):
    """Build a Settings from environment variables and no .env file."""

    def _make(**env) -> Settings:
        for key, value in env.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, str(value))
        return Settings(_env_file=str(tmp_path / "absent.env"))

    return _make


@pytest.fixture
def saved_endpoint():
    """A saved, fully configured custom endpoint."""
    return store.upsert_endpoint(
        store.new_endpoint(
            name="Gateway",
            base_url="https://gw.example.com/v1",
            api_key="ep-key",
            model="gw/model-a",
            models=["gw/model-a", "gw/model-b"],
        )
    )


# ── Provider table invariants ─────────────────────────────────────────────────


def test_every_spec_points_at_a_real_registry():
    for provider, spec in PROVIDER_SPECS.items():
        registry = spec["registry"]
        if registry:
            assert registry in MODEL_REGISTRY, provider
        assert spec["model_env"] and spec["model_attr"], provider


def test_spec_attributes_exist_on_settings():
    fields = Settings.model_fields
    for spec in PROVIDER_SPECS.values():
        assert spec["model_attr"] in fields
        if spec["key_attr"]:
            assert spec["key_attr"] in fields


def test_provider_order_lists_every_spec():
    assert sorted(PROVIDER_ORDER) == sorted(PROVIDER_SPECS)


def test_ollama_preferred_order_tracks_the_registry():
    assert OLLAMA_PREFERRED_ORDER == [m for m, _ in MODEL_REGISTRY["ollama_local"]]


# ── Provider ids ──────────────────────────────────────────────────────────────


def test_is_custom_provider():
    assert is_custom_provider("custom:abc")
    assert not is_custom_provider("custom:")
    assert not is_custom_provider("custom")
    assert not is_custom_provider("anthropic")


def test_custom_endpoint_id():
    assert custom_endpoint_id("custom:my-gw") == "my-gw"


def test_provider_label_for_known_unknown_and_custom(saved_endpoint):
    assert provider_label("anthropic") == "Anthropic"
    assert provider_label("nonsense") == "nonsense"
    assert provider_label(f"{CUSTOM_PREFIX}{saved_endpoint['id']}") == "Gateway"


def test_provider_label_for_missing_custom_endpoint():
    assert provider_label("custom:gone") == "Custom endpoint"


# ── Model id extraction ───────────────────────────────────────────────────────


def test_model_ids_from_each_listing_shape():
    assert _model_ids(["b", "a"]) == ["a", "b"]
    assert _model_ids({"data": [{"id": "z"}]}) == ["z"]
    assert _model_ids({"models": ["m"]}) == ["m"]
    assert _model_ids({"items": [{"name": "n"}]}) == ["n"]
    assert _model_ids({"result": [{"slug": "s"}]}) == ["s"]
    assert _model_ids([{"model": "q"}]) == ["q"]


def test_model_ids_ignores_junk():
    assert _model_ids(None) == []
    assert _model_ids("nope") == []
    assert _model_ids({"other": ["x"]}) == []
    assert _model_ids([{"id": "  "}, "ok", 7, {}]) == ["ok"]
    assert _model_ids({"data": [{"id": "A"}, {"id": "a"}]}) == ["A", "a"]


def test_model_ids_sorted_case_insensitively():
    assert _model_ids(["beta", "Alpha", "gamma"]) == ["Alpha", "beta", "gamma"]


# ── discover_models ───────────────────────────────────────────────────────────


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def capture_open(monkeypatch):
    """Replace urlopen with a scripted responder; records each request."""
    seen: list = []

    def _install(responder):
        def fake_urlopen(request, timeout=None):
            seen.append(request)
            return responder(request)

        monkeypatch.setattr(config_mod, "urlopen", fake_urlopen)
        return seen

    return _install


def _listing(ids):
    def responder(request):
        return _Response({"data": [{"id": i} for i in ids]})

    return responder


def test_discover_models_needs_a_base_url():
    assert discover_models("") == ([], "Add a Base URL first.")
    assert discover_models("   ") == ([], "Add a Base URL first.")


def test_discover_models_returns_ids(capture_open):
    capture_open(_listing(["b", "a"]))
    models, error = discover_models("https://gw.example.com/v1/")
    assert models == ["a", "b"]
    assert error == ""


def test_discover_models_sends_authorization_only_with_a_key(capture_open):
    seen = capture_open(_listing(["m"]))
    discover_models("https://gw.example.com")
    assert "Authorization" not in seen[0].headers
    assert "Authorization" not in dict(seen[0].headers)

    seen.clear()
    discover_models("https://gw.example.com", api_key=" k ")
    headers = {k.lower(): v for k, v in seen[0].headers.items()}
    assert headers["authorization"] == "Bearer k"


def test_discover_models_tries_v1_suffix_after_plain_path(capture_open):
    seen = capture_open(lambda r: _Response({"data": []}) if r.full_url.endswith("/models") and "v1" not in r.full_url else _Response({"data": [{"id": "found"}]}))
    models, error = discover_models("https://gw.example.com")
    assert models == ["found"]
    assert error == ""
    assert [r.full_url for r in seen][0] == "https://gw.example.com/models"


def test_discover_models_tries_beside_v1(capture_open):
    seen = capture_open(lambda r: _Response({"data": [{"id": "beside"}]}) if r.full_url.endswith("/api/gateway/models") else _Response({"data": []}))
    models, error = discover_models("https://host/api/gateway/v1")
    assert models == ["beside"]
    assert error == ""


def test_discover_models_reports_an_empty_catalogue(capture_open):
    capture_open(_listing([]))
    models, error = discover_models("https://gw.example.com")
    assert models == []
    assert "returned no model ids" in error
    assert "tried 2 paths" in error


def test_discover_models_reports_http_errors(capture_open):
    def boom(request):
        raise HTTPError(request.full_url, 404, "Not Found", {}, None)

    capture_open(boom)
    models, error = discover_models("https://gw.example.com")
    assert models == []
    assert "HTTP 404" in error


def test_discover_models_reports_unreachable_endpoints(capture_open):
    capture_open(lambda r: (_ for _ in ()).throw(URLError("refused")))
    _, error = discover_models("https://gw.example.com")
    assert "Cannot reach" in error and "refused" in error


def test_discover_models_reports_timeouts(capture_open):
    capture_open(lambda r: (_ for _ in ()).throw(TimeoutError()))
    _, error = discover_models("https://gw.example.com", timeout=3)
    assert "did not answer within 3s" in error


def test_discover_models_reports_unreadable_bodies(capture_open):
    def bad_json(request):
        class _Bad:
            def read(self):
                return b"<html>nope</html>"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        return _Bad()

    capture_open(bad_json)
    _, error = discover_models("https://gw.example.com")
    assert "Unreadable response" in error


# ── Settings: defaults and .env ───────────────────────────────────────────────


def test_settings_defaults(make_settings):
    settings = make_settings()
    assert settings.active_provider == "auto"
    assert settings.model_for("kilo") == "kilo-auto/free"
    assert settings.model_for("github_copilot") == "gpt-5.2"
    assert settings.model_for("openai_codex") == "gpt-5.3-codex"
    assert settings.safe_mode is False


def test_settings_reads_the_env_file():
    store.write_env_values({"ACTIVE_PROVIDER": "anthropic", "ANTHROPIC_MODEL": "claude-x"})
    settings = Settings()
    assert settings.active_provider == "anthropic"
    assert settings.model_for("anthropic") == "claude-x"


def test_settings_env_wins_over_env_file():
    store.write_env_values({"ANTHROPIC_MODEL": "from-file"})
    settings = Settings(ANTHROPIC_MODEL="from-env")
    assert settings.model_for("anthropic") == "from-env"


# ── Per-provider model and key accessors ──────────────────────────────────────


def test_model_for_falls_back_to_legacy_keys(make_settings):
    settings = make_settings(ANTHROPIC_MODEL="", PREFERRED_CLOUD_MODEL="legacy-cloud")
    assert settings.model_for("anthropic") == "legacy-cloud"

    settings = make_settings(OLLAMA_LOCAL_MODEL="", PREFERRED_LOCAL_MODEL="legacy-local")
    assert settings.model_for("ollama_local") == "legacy-local"


def test_model_for_unknown_provider_is_empty(make_settings):
    assert make_settings().model_for("nope") == ""


def test_set_model_for_writes_the_provider_attribute(make_settings):
    settings = make_settings()
    settings.set_model_for("openai", "gpt-4o")
    assert settings.model_for("openai") == "gpt-4o"


def test_set_model_for_ignores_unknown_provider(make_settings):
    settings = make_settings()
    settings.set_model_for("nope", "x")  # must not raise


def test_api_key_for_and_set(make_settings):
    settings = make_settings(ANTHROPIC_API_KEY="sk-ant")
    assert settings.api_key_for("anthropic") == "sk-ant"
    settings.set_api_key("openrouter", "sk-or")
    assert settings.api_key_for("openrouter") == "sk-or"


def test_api_key_for_provider_without_a_key(make_settings):
    settings = make_settings()
    assert settings.api_key_for("github_copilot") == ""
    settings.set_api_key("github_copilot", "ignored")
    assert settings.api_key_for("github_copilot") == ""


# ── Custom endpoints ──────────────────────────────────────────────────────────


def test_endpoint_for_alias_and_custom(saved_endpoint):
    settings = Settings()
    assert settings.endpoint_for(CUSTOM_ALIAS)["id"] == saved_endpoint["id"]
    assert settings.endpoint_for(f"{CUSTOM_PREFIX}{saved_endpoint['id']}")["id"] == saved_endpoint["id"]
    assert settings.endpoint_for("anthropic") is None
    assert settings.model_for(f"{CUSTOM_PREFIX}{saved_endpoint['id']}") == "gw/model-a"
    assert settings.api_key_for(f"{CUSTOM_PREFIX}{saved_endpoint['id']}") == "ep-key"


def test_set_model_and_key_for_a_custom_endpoint(saved_endpoint):
    settings = Settings()
    provider = f"{CUSTOM_PREFIX}{saved_endpoint['id']}"
    settings.set_model_for(provider, "gw/model-b")
    settings.set_api_key(provider, "new-key")
    store.invalidate()
    assert store.find_endpoint(saved_endpoint["id"])["model"] == "gw/model-b"
    assert store.find_endpoint(saved_endpoint["id"])["api_key"] == "new-key"
    # Editing an endpoint must not steal the active slot.
    assert store.active_endpoint_id() == saved_endpoint["id"]


def test_select_endpoint_pins_the_provider(saved_endpoint):
    settings = Settings()
    settings.select_endpoint(saved_endpoint["id"])
    assert settings.active_provider == f"{CUSTOM_PREFIX}{saved_endpoint['id']}"


def test_endpoints_lists_saved_records(saved_endpoint):
    assert [ep["id"] for ep in Settings().endpoints()] == [saved_endpoint["id"]]


def test_endpoint_status_explains_what_is_missing():
    assert Settings.endpoint_status(None) == (False, "Add a custom endpoint")
    assert Settings.endpoint_status({"base_url": "", "model": "m"}) == (False, "Add a Base URL")
    assert Settings.endpoint_status({"base_url": "https://x/v1", "model": " "}) == (False, "Choose a model")
    ok, message = Settings.endpoint_status({"base_url": "https://x.example.com/v1", "model": "m"})
    assert ok and message == "Ready · x.example.com"


# ── provider_status ───────────────────────────────────────────────────────────


def test_provider_status_unknown(make_settings):
    assert make_settings().provider_status("nope") == (False, "Unknown provider: nope")


def test_provider_status_signin_providers_need_no_key(make_settings):
    ok, message = make_settings().provider_status("github_copilot")
    assert ok and "copilot" in message
    ok, message = make_settings().provider_status("openai_codex")
    assert ok and "codex" in message


def test_provider_status_missing_api_key(make_settings):
    assert make_settings(ANTHROPIC_API_KEY=None).provider_status("anthropic") == (
        False,
        "Add ANTHROPIC_API_KEY",
    )


def test_provider_status_kilo_is_ready_without_a_key(make_settings):
    ok, message = make_settings(KILO_API_KEY=None).provider_status("kilo")
    assert ok and "no key" in message


def test_provider_status_needs_a_model(make_settings):
    settings = make_settings(ANTHROPIC_API_KEY="sk", ANTHROPIC_MODEL="", PREFERRED_CLOUD_MODEL="")
    assert settings.provider_status("anthropic") == (False, "Choose a model")


def test_provider_status_endpoint_auth_uses_the_endpoint(saved_endpoint):
    settings = Settings()
    assert settings.provider_status(CUSTOM_ALIAS)[0]
    store.remove_endpoint(saved_endpoint["id"])
    assert settings.provider_status(CUSTOM_ALIAS) == (False, "Add a custom endpoint")


def test_provider_status_local_ollama(make_settings, monkeypatch):
    settings = make_settings(OLLAMA_LOCAL_MODEL="", PREFERRED_LOCAL_MODEL="qwen2.5:14b")

    def no_ollama(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(config_mod.subprocess, "run", no_ollama)
    assert settings.provider_status("ollama_local")[0] is False

    monkeypatch.setattr(config_mod.subprocess, "run", _ollama_list("qwen2.5:14b\t8 GB"))
    ok, message = settings.provider_status("ollama_local")
    assert ok and message == "Ready · qwen2.5:14b"


# ── Active provider resolution ────────────────────────────────────────────────


def _ollama_list(*lines):
    class _Proc:
        returncode = 0
        stdout = "NAME\tSIZE\n" + "".join(f"{line}\n" for line in lines)
        stderr = ""

    def fake_run(cmd, *args, **kwargs):
        assert cmd[:2] == ["ollama", "list"]
        return _Proc()

    return fake_run


def test_get_active_provider_honours_a_pin(make_settings):
    assert make_settings(ACTIVE_PROVIDER="openrouter").get_active_provider() == "openrouter"


def test_get_active_provider_falls_back_when_a_pinned_endpoint_disappears(
    make_settings, monkeypatch
):
    monkeypatch.setattr(
        config_mod.Settings, "_detect_ollama_model", lambda self: None, raising=True
    )
    settings = make_settings(ACTIVE_PROVIDER="custom:gone")
    assert settings.get_active_provider() == "kilo"


def test_auto_provider_prefers_real_credentials(make_settings):
    assert make_settings()._auto_provider() == "kilo"
    assert make_settings(OPENAI_API_KEY="sk")._auto_provider() == "openai"
    assert make_settings(OPENROUTER_API_KEY="sk-or")._auto_provider() == "openrouter"
    assert (
        make_settings(ANTHROPIC_API_KEY="sk", OPENAI_API_KEY="sk")._auto_provider()
        == "anthropic"
    )


def test_auto_provider_uses_a_ready_custom_endpoint(make_settings, saved_endpoint):
    assert make_settings()._auto_provider() == CUSTOM_ALIAS


def test_auto_provider_ignores_an_incomplete_custom_endpoint(make_settings):
    store.upsert_endpoint(store.new_endpoint(name="Half", base_url="https://h/v1"))
    assert make_settings()._auto_provider() == "kilo"


def test_auto_provider_cloud_local_model(make_settings):
    assert make_settings(PREFERRED_LOCAL_MODEL="deepseek-v4-pro:cloud")._auto_provider() == "kilo"
    assert (
        make_settings(PREFERRED_LOCAL_MODEL="deepseek-v4-pro:cloud", OLLAMA_API_KEY="ok")._auto_provider()
        == "ollama_cloud"
    )


def test_auto_provider_local_ollama_when_installed(make_settings, monkeypatch):
    monkeypatch.setattr(config_mod.subprocess, "run", _ollama_list("qwen2.5:14b\t8 GB"))
    assert make_settings()._auto_provider() == "ollama_local"


# ── _detect_ollama_model ──────────────────────────────────────────────────────


def test_detect_ollama_model_prefers_a_cloud_tag(make_settings, monkeypatch):
    monkeypatch.setattr(
        config_mod.subprocess,
        "run",
        lambda *a, **k: pytest.fail("must not shell out for a cloud tag"),
    )
    settings = make_settings(OLLAMA_LOCAL_MODEL="deepseek-v4-pro:cloud")
    assert settings._detect_ollama_model() == "deepseek-v4-pro:cloud"


def test_detect_ollama_model_matches_the_preferred_family(make_settings, monkeypatch):
    monkeypatch.setattr(config_mod.subprocess, "run", _ollama_list("llama3.1:8b\t5 GB", "qwen3.6:27b\t17 GB"))
    settings = make_settings(OLLAMA_LOCAL_MODEL="qwen3.6:27b")
    assert settings._detect_ollama_model() == "qwen3.6:27b"


def test_detect_ollama_model_falls_back_to_a_known_model(make_settings, monkeypatch):
    monkeypatch.setattr(config_mod.subprocess, "run", _ollama_list("mystery:1b\t2 GB", "qwen2.5:32b\t20 GB"))
    settings = make_settings(OLLAMA_LOCAL_MODEL="notlisted:9b")
    assert settings._detect_ollama_model() == "qwen2.5:32b"


def test_detect_ollama_model_returns_the_first_available(make_settings, monkeypatch):
    monkeypatch.setattr(config_mod.subprocess, "run", _ollama_list("zzz:1b\t2 GB", "aaa:1b\t2 GB"))
    settings = make_settings(OLLAMA_LOCAL_MODEL="notlisted:9b")
    assert settings._detect_ollama_model() == "zzz:1b"


def test_detect_ollama_model_handles_failures(make_settings, monkeypatch):
    settings = make_settings()

    class _Failing:
        returncode = 1
        stdout = ""
        stderr = ""

    monkeypatch.setattr(config_mod.subprocess, "run", lambda *a, **k: _Failing())
    assert settings._detect_ollama_model() is None

    monkeypatch.setattr(config_mod.subprocess, "run", _ollama_list())
    assert settings._detect_ollama_model() is None

    import subprocess

    for exc in (
        FileNotFoundError("ollama"),
        OSError("boom"),
        subprocess.TimeoutExpired(["ollama", "list"], 5),
    ):
        def raiser(*args, _exc=exc, **kwargs):
            raise _exc

        monkeypatch.setattr(config_mod.subprocess, "run", raiser)
        assert settings._detect_ollama_model() is None


# ── completion_kwargs ─────────────────────────────────────────────────────────


def test_completion_kwargs_for_kilo(make_settings):
    keyed = make_settings(ACTIVE_PROVIDER="kilo", KILO_API_KEY="kilo-key")
    assert keyed.completion_kwargs() == {
        "api_base": "https://api.kilo.ai/api/gateway",
        "api_key": "kilo-key",
    }

    keyless = make_settings(ACTIVE_PROVIDER="kilo", KILO_API_KEY=None)
    kwargs = keyless.completion_kwargs()
    assert kwargs["api_key"] == "not-needed"
    assert kwargs["extra_headers"] == {"Authorization": ""}


def test_completion_kwargs_for_a_custom_endpoint(make_settings, saved_endpoint):
    settings = make_settings(ACTIVE_PROVIDER=f"{CUSTOM_PREFIX}{saved_endpoint['id']}")
    assert settings.completion_kwargs() == {
        "api_base": "https://gw.example.com/v1",
        "api_key": "ep-key",
    }

    settings.set_api_key(f"{CUSTOM_PREFIX}{saved_endpoint['id']}", "")
    store.invalidate()
    kwargs = settings.completion_kwargs()
    assert kwargs["api_key"] == "not-needed"
    assert kwargs["extra_headers"] == {"Authorization": ""}


def test_completion_kwargs_empty_for_a_hosted_provider(make_settings):
    assert make_settings(ACTIVE_PROVIDER="anthropic").completion_kwargs() == {}


# ── get_active_model ──────────────────────────────────────────────────────────


def test_active_model_for_a_custom_endpoint(make_settings, saved_endpoint):
    settings = make_settings(ACTIVE_PROVIDER=f"{CUSTOM_PREFIX}{saved_endpoint['id']}")
    assert settings.get_active_model() == ("openai/gw/model-a", "gw/model-a (Gateway)")


def test_active_model_labels_an_unnamed_endpoint(make_settings, saved_endpoint):
    record = {**saved_endpoint, "name": "", "model": ""}
    store.upsert_endpoint(record)
    settings = make_settings(ACTIVE_PROVIDER=f"{CUSTOM_PREFIX}{saved_endpoint['id']}")
    with pytest.raises(RuntimeError, match="Base URL and a model"):
        settings.get_active_model()


def test_active_model_for_kilo(make_settings):
    settings = make_settings(ACTIVE_PROVIDER="kilo", KILO_MODEL="")
    assert settings.get_active_model() == ("openai/kilo-auto/free", "kilo-auto/free (Kilo Gateway)")


def test_active_model_for_anthropic_and_openai(make_settings):
    settings = make_settings(ACTIVE_PROVIDER="anthropic", ANTHROPIC_API_KEY="sk", ANTHROPIC_MODEL="claude-x")
    assert settings.get_active_model() == ("claude-x", "claude-x (Anthropic)")

    settings = make_settings(ACTIVE_PROVIDER="openai", OPENAI_API_KEY="sk", OPENAI_MODEL="", PREFERRED_CLOUD_MODEL="")
    assert settings.get_active_model() == (MODEL_REGISTRY["openai"][0][0], f"{MODEL_REGISTRY['openai'][0][0]} (OpenAI)")


def test_active_model_requires_the_api_key(make_settings):
    settings = make_settings(ACTIVE_PROVIDER="openai", OPENAI_API_KEY=None)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY is required"):
        settings.get_active_model()


def test_active_model_prefixes_openrouter_ids(make_settings):
    settings = make_settings(ACTIVE_PROVIDER="openrouter", OPENROUTER_API_KEY="sk", OPENROUTER_MODEL="deepseek/deepseek-r1")
    assert settings.get_active_model() == ("openrouter/deepseek/deepseek-r1", "deepseek-r1 (OpenRouter)")

    settings = make_settings(ACTIVE_PROVIDER="openrouter", OPENROUTER_API_KEY="sk", OPENROUTER_MODEL="openrouter/x/y")
    assert settings.get_active_model()[0] == "openrouter/x/y"

    settings = make_settings(ACTIVE_PROVIDER="openrouter", OPENROUTER_API_KEY="sk", OPENROUTER_MODEL="plain-model")
    assert settings.get_active_model()[0] == "openrouter/anthropic/plain-model"


def test_active_model_for_subscription_providers(make_settings):
    assert make_settings(ACTIVE_PROVIDER="github_copilot").get_active_model()[0] == "github_copilot/gpt-5.2"
    assert make_settings(ACTIVE_PROVIDER="openai_codex").get_active_model()[0] == "chatgpt/gpt-5.3-codex"


def test_active_model_for_ollama_cloud(make_settings):
    settings = make_settings(ACTIVE_PROVIDER="ollama_cloud", OLLAMA_API_KEY=None)
    with pytest.raises(RuntimeError, match="OLLAMA_API_KEY is required"):
        settings.get_active_model()

    settings = make_settings(ACTIVE_PROVIDER="ollama_cloud", OLLAMA_API_KEY="ok", OLLAMA_CLOUD_MODEL="qwen3.5:cloud")
    assert settings.get_active_model() == ("ollama/qwen3.5:cloud", "qwen3.5:cloud (Ollama Cloud)")


def test_active_model_for_ollama_local(make_settings, monkeypatch):
    settings = make_settings(ACTIVE_PROVIDER="ollama_local", OLLAMA_LOCAL_MODEL="qwen2.5:14b")
    monkeypatch.setattr(config_mod.subprocess, "run", _ollama_list("qwen2.5:14b\t8 GB"))
    assert settings.get_active_model() == ("ollama/qwen2.5:14b", "qwen2.5:14b (Ollama Local)")

    monkeypatch.setattr(
        config_mod.subprocess, "run", lambda *a, **k: pytest.fail("no shell for a cloud tag")
    )
    settings = make_settings(ACTIVE_PROVIDER="ollama_local", OLLAMA_LOCAL_MODEL="gemma4:31b-cloud")
    with pytest.raises(RuntimeError, match="Cannot reach a local Ollama model"):
        settings.get_active_model()


def test_active_model_for_unknown_provider(make_settings):
    settings = make_settings(ACTIVE_PROVIDER="nope")
    with pytest.raises(RuntimeError, match="Unknown active provider"):
        settings.get_active_model()


def test_validate_model_access(make_settings):
    ok, message = make_settings(ACTIVE_PROVIDER="kilo").validate_model_access()
    assert ok and "Model ready" in message

    ok, message = make_settings(ACTIVE_PROVIDER="openai", OPENAI_API_KEY=None).validate_model_access()
    assert not ok and "OPENAI_API_KEY" in message


def test_set_model_override_targets_the_active_provider(make_settings):
    settings = make_settings(ACTIVE_PROVIDER="anthropic")
    settings.set_model_override("claude-override")
    assert settings.model_for("anthropic") == "claude-override"


def test_endpoints_file_lives_beside_the_env_file(isolated_config):
    assert store.endpoints_file().parent == isolated_config
    assert os.environ["POOFMAC_CONFIG_DIR"].endswith("config")