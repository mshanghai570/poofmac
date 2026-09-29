# SPDX-License-Identifier: MIT
# Copyright (c) 2026 lesteroliver — https://poofmac.app
"""Where PoofMac keeps user configuration on disk.

Two files make up a PoofMac install:

* ``.env``             — the selected provider, per-provider model ids and keys.
* ``endpoints.json``   — the user's custom OpenAI-compatible endpoints, each
  with its own base URL, key, and the models it hosts.

Both live in the *same* directory, so copying one directory moves a whole
configuration, and both are resolved by :func:`env_file` / :func:`endpoints_file`.

Why this module exists
──────────────────────
The bundled macOS app is launched by Finder, which starts the process with a
working directory of ``/``. A bare relative path such as ``".env"`` therefore
meant ``/.env`` — and because python-dotenv writes atomically by creating its
temporary file *next to* the target, every save failed with::

    OSError: [Errno 30] Read-only file system: '/.tmp_vyz11x87'

Nothing here may ever resolve to a bare relative path. The order is:

1. ``POOFMAC_ENV_FILE``  — explicit override (used by tests).
2. ``.env`` in the working directory itself — a source checkout, so a
   repository keeps its own configuration.
3. ``~/Library/Application Support/PoofMac/.env`` on macOS, or
   ``$XDG_CONFIG_HOME/poofmac/.env`` elsewhere — a per-user location the
   bundled app can always write to.

Note that step 2 deliberately does *not* walk up parent directories: dotenv's
``find_dotenv`` does, and an unrelated ``~/.env`` (holding, say, another
tool's API key) would then be adopted as PoofMac's settings file and have
PoofMac keys written into it.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlparse

APP_DIR_NAME = "PoofMac"
ENV_FILE_NAME = ".env"
ENDPOINTS_FILE_NAME = "endpoints.json"
_SCHEMA_VERSION = 1

# ── Locations ─────────────────────────────────────────────────────────────────


def config_dir() -> Path:
    """Per-user configuration directory, always writable by this user."""
    override = os.environ.get("POOFMAC_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / APP_DIR_NAME.lower()


def _discover_local_env() -> Optional[Path]:
    """``.env`` in the working directory, and nowhere else.

    Only the launch directory is considered — see the module docstring for why
    walking up to the filesystem root is not safe.
    """
    try:
        candidate = Path.cwd() / ENV_FILE_NAME
    except OSError:  # the working directory was deleted out from under us
        return None
    return candidate if candidate.is_file() else None


def env_file() -> Path:
    """The ``.env`` PoofMac reads and writes. Never a bare relative path."""
    override = os.environ.get("POOFMAC_ENV_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    local = _discover_local_env()
    if local is not None:
        return local
    return config_dir() / ENV_FILE_NAME


def endpoints_file() -> Path:
    """``endpoints.json``, kept beside the ``.env`` it belongs to."""
    return env_file().parent / ENDPOINTS_FILE_NAME


# ── Disclaimer acceptance ─────────────────────────────────────────────────────


def disclaimer_file() -> Path:
    """Marker file recording that the user accepted the safety disclaimer."""
    return config_dir() / "disclaimer_accepted"


def disclaimer_accepted() -> bool:
    """True once the user has accepted the safety disclaimer (first run only)."""
    try:
        return disclaimer_file().is_file()
    except OSError:
        return False


def mark_disclaimer_accepted() -> None:
    """Record disclaimer acceptance; the prompt then never shows again."""
    path = disclaimer_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
        _restrict(path)
    except OSError:
        pass  # worst case the disclaimer shows again next launch


def config_dir_is_writable() -> tuple[bool, str]:
    """Whether settings can actually be saved, and a human explanation."""
    path = env_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        probe = path.parent / f".poofmac-write-test-{os.getpid()}"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return False, f"Cannot write to {path.parent} ({exc.strerror or exc})"
    return True, str(path)


# ── .env access ───────────────────────────────────────────────────────────────


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _restrict(path: Path) -> None:
    """Keep config files owner-only — they hold API keys."""
    try:
        path.chmod(0o600)
    except OSError:
        pass


def read_env_values() -> dict[str, str]:
    """Current ``.env`` contents. Missing or unreadable files read as empty."""
    path = env_file()
    if not path.is_file():
        return {}
    try:
        from dotenv import dotenv_values

        values = dotenv_values(path)
    except (ImportError, OSError, ValueError):
        return {}
    return {key: value for key, value in values.items() if value is not None}


def write_env_values(values: Mapping[str, Any]) -> Path:
    """Upsert keys into the user's ``.env``, creating it when needed.

    Returns the file that was written so callers can tell the user where their
    settings actually live.
    """
    path = env_file()
    _ensure_parent(path)
    if not path.exists():
        path.touch(mode=0o600)
    from dotenv import set_key

    for key, value in values.items():
        set_key(str(path), str(key), "" if value is None else str(value))
    _restrict(path)
    return path


# ── Custom endpoints ──────────────────────────────────────────────────────────


def host_label(url: str) -> str:
    """``https://api.example.com/v1`` → ``api.example.com``."""
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        return urlparse(raw if "://" in raw else f"//{raw}").netloc or raw
    except ValueError:
        return raw


def slugify(name: str, taken: Iterable[str] = ()) -> str:
    """A stable, unique id derived from an endpoint's display name."""
    base = "".join(ch if ch.isalnum() else "-" for ch in (name or "").lower()).strip("-")
    base = "-".join(part for part in base.split("-") if part)[:40] or "endpoint"
    taken_set = set(taken)
    if base not in taken_set:
        return base
    index = 2
    while f"{base}-{index}" in taken_set:
        index += 1
    return f"{base}-{index}"


def new_endpoint(
    name: str = "",
    base_url: str = "",
    api_key: str = "",
    model: str = "",
    models: Optional[Iterable[str]] = None,
) -> dict:
    """A blank (unsaved) endpoint record. Give it an id with :func:`upsert_endpoint`."""
    known = [str(m).strip() for m in (models or []) if str(m).strip()]
    if model and model not in known:
        known.insert(0, model)
    return {
        "id": "",
        "name": (name or "").strip(),
        "base_url": (base_url or "").strip().rstrip("/"),
        "api_key": (api_key or "").strip(),
        "model": (model or "").strip(),
        "models": known,
    }


def _coerce(raw: Any, taken: set[str]) -> list[dict]:
    """Normalise whatever was on disk into clean endpoint records."""
    entries = raw if isinstance(raw, list) else []
    cleaned: list[dict] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        base_url = str(item.get("base_url", "") or "").strip().rstrip("/")
        if not base_url:
            continue  # an endpoint without a URL is not usable
        name = str(item.get("name", "") or "").strip() or host_label(base_url)
        model = str(item.get("model", "") or "").strip()
        models = [
            str(m).strip()
            for m in (item.get("models") or [])
            if str(m).strip()
        ]
        if model and model not in models:
            models.insert(0, model)
        ep_id = str(item.get("id", "") or "").strip() or slugify(name, taken)
        if ep_id in taken:
            ep_id = slugify(ep_id, taken)
        taken.add(ep_id)
        cleaned.append(
            {
                "id": ep_id,
                "name": name,
                "base_url": base_url,
                "api_key": str(item.get("api_key", "") or "").strip(),
                "model": model,
                "models": models,
            }
        )
    return cleaned


def _from_legacy_env(values: Mapping[str, str]) -> list[dict]:
    """Adopt a pre-endpoints.json ``OPENAI_COMPAT_*`` configuration, once."""
    base_url = (values.get("OPENAI_COMPAT_BASE_URL") or "").strip()
    if not base_url:
        return []
    model = (values.get("OPENAI_COMPAT_MODEL") or "").strip()
    endpoint = new_endpoint(
        name=f"{host_label(base_url)} (imported)",
        base_url=base_url,
        api_key=(values.get("OPENAI_COMPAT_API_KEY") or "").strip(),
        model=model,
    )
    endpoint["id"] = "default"
    return [endpoint]


_state: Optional[dict] = None


def invalidate() -> None:
    """Forget the cached endpoints, e.g. after an external edit."""
    global _state
    _state = None


def _load(refresh: bool = False) -> dict:
    """``{"endpoints": [...], "active": "<id>"}``, cached between calls."""
    global _state
    if _state is not None and not refresh:
        return _state

    path = endpoints_file()
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if isinstance(raw, dict):
            entries, active = raw.get("endpoints", []), str(raw.get("active", "") or "")
        else:  # a bare list still loads
            entries, active = raw, ""
        endpoints = _coerce(entries, set())
    else:
        endpoints = _from_legacy_env(read_env_values())
        active = endpoints[0]["id"] if endpoints else ""
        if endpoints:
            # Persist the import so the legacy keys are only ever read once.
            _write(endpoints, active)

    if not any(ep["id"] == active for ep in endpoints):
        active = endpoints[0]["id"] if endpoints else ""
    _state = {"endpoints": endpoints, "active": active}
    return _state


def _write(endpoints: list[dict], active: str) -> None:
    path = endpoints_file()
    _ensure_parent(path)
    payload = {"version": _SCHEMA_VERSION, "active": active, "endpoints": endpoints}
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    _restrict(tmp)
    os.replace(tmp, path)
    _restrict(path)


def list_endpoints(*, refresh: bool = False) -> list[dict]:
    """Every saved custom endpoint, in the order the user added them."""
    return [dict(ep) for ep in _load(refresh)["endpoints"]]


def active_endpoint_id(*, refresh: bool = False) -> str:
    return str(_load(refresh)["active"])


def find_endpoint(endpoint_id: str, *, refresh: bool = False) -> Optional[dict]:
    for endpoint in _load(refresh)["endpoints"]:
        if endpoint["id"] == endpoint_id:
            return dict(endpoint)
    return None


def active_endpoint(*, refresh: bool = False) -> Optional[dict]:
    """The endpoint new requests use — the last one selected, else the first."""
    state = _load(refresh)
    for endpoint in state["endpoints"]:
        if endpoint["id"] == state["active"]:
            return dict(endpoint)
    return dict(state["endpoints"][0]) if state["endpoints"] else None


def set_active_endpoint(endpoint_id: str) -> None:
    state = _load()
    if any(ep["id"] == endpoint_id for ep in state["endpoints"]):
        state["active"] = endpoint_id
        _write(state["endpoints"], endpoint_id)


def upsert_endpoint(endpoint: Mapping[str, Any], *, make_active: bool = True) -> Optional[dict]:
    """Add or update an endpoint, returning the saved record.

    An endpoint with no base URL is never stored, so a half-typed row cannot
    shadow a working configuration.
    """
    base_url = str(endpoint.get("base_url", "") or "").strip().rstrip("/")
    if not base_url:
        return None

    state = _load()
    endpoints = state["endpoints"]
    ep_id = str(endpoint.get("id", "") or "").strip()
    name = str(endpoint.get("name", "") or "").strip() or host_label(base_url)

    if not ep_id:
        ep_id = slugify(name, [ep["id"] for ep in endpoints])

    model = str(endpoint.get("model", "") or "").strip()
    models = [str(m).strip() for m in (endpoint.get("models") or []) if str(m).strip()]
    if model and model not in models:
        models.insert(0, model)

    record = {
        "id": ep_id,
        "name": name,
        "base_url": base_url,
        "api_key": str(endpoint.get("api_key", "") or "").strip(),
        "model": model,
        "models": models,
    }

    for index, existing in enumerate(endpoints):
        if existing["id"] == ep_id:
            endpoints[index] = record
            break
    else:
        endpoints.append(record)

    if make_active or not any(ep["id"] == state["active"] for ep in endpoints):
        state["active"] = ep_id
    _write(endpoints, state["active"])
    return dict(record)


def remove_endpoint(endpoint_id: str) -> list[dict]:
    """Delete an endpoint and return what is left."""
    state = _load()
    remaining = [ep for ep in state["endpoints"] if ep["id"] != endpoint_id]
    state["endpoints"] = remaining
    if state["active"] == endpoint_id:
        state["active"] = remaining[0]["id"] if remaining else ""
    _write(remaining, state["active"])
    return [dict(ep) for ep in remaining]
