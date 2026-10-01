"""
The agent tool surface.

The model is only ever offered the tools in TOOLS, but it can only actually
run what execute_tool() dispatches. When those two lists drift, the agent
offers the user a button that does nothing — or worse, a name that reaches a
branch nobody wrote. llm._KNOWN_TOOLS is the third list (what the text-call
parser will accept), so all three are checked against each other here.
"""

from __future__ import annotations

import json

import pytest

from mac_cleaner.tools import TOOLS, execute_tool


def _names() -> list[str]:
    return [t["function"]["name"] for t in TOOLS]


def test_every_tool_has_a_function_wrapper():
    for tool in TOOLS:
        assert tool["type"] == "function", tool
        fn = tool["function"]
        assert fn["name"] and fn["description"].strip(), fn
        assert "parameters" in fn
        assert fn["parameters"].get("type") == "object", fn["name"]


def test_tool_names_are_unique():
    names = _names()
    duplicates = {n for n in names if names.count(n) > 1}
    assert not duplicates, f"duplicate tool names: {duplicates}"


def test_tool_parameters_are_json_serialisable_schemas():
    for tool in TOOLS:
        json.dumps(tool)  # a non-serialisable schema breaks the API call


def test_advertised_tools_match_the_dispatch_and_parser():
    """TOOLS == what execute_tool handles == what llm._KNOWN_TOOLS accepts."""
    from mac_cleaner import llm

    advertised = set(_names())
    assert advertised == llm._KNOWN_TOOLS, {
        "only advertised": advertised - llm._KNOWN_TOOLS,
        "only implemented": llm._KNOWN_TOOLS - advertised,
    }


def test_unknown_tool_is_an_error_not_a_crash():
    result = execute_tool("definitely_not_a_tool", {})
    payload = json.loads(result)
    assert payload.get("error") or "unknown" in result.lower()


@pytest.mark.parametrize("bad_args", ["not-a-dict", ["a", "list"], None, 42])
def test_tool_call_with_bad_arguments_returns_json(bad_args):
    """Models emit malformed arguments; that must not raise through the loop."""
    result = execute_tool("check_path_safety", bad_args)
    payload = json.loads(result)
    assert payload.get("error"), f"{bad_args!r} should be rejected, not crash"


def test_check_path_safety_refuses_protected_paths():
    payload = json.loads(execute_tool("check_path_safety", {"path": "/System/Library"}))
    assert payload.get("is_safe") is False, payload


def test_check_path_safety_allows_a_cache_path():
    import pathlib

    cache = pathlib.Path.home() / "Library" / "Caches" / "com.example"
    payload = json.loads(execute_tool("check_path_safety", {"path": str(cache)}))
    assert payload.get("is_safe") is True, payload


def test_disk_overview_is_the_number_the_ui_shows():
    from mac_cleaner.scanner import get_disk_usage

    payload = json.loads(execute_tool("get_disk_overview", {}))
    assert payload["used_percent"] == get_disk_usage()["used_percent"]


def test_maintenance_guide_recipes_have_commands_and_explanations():
    payload = json.loads(
        execute_tool("get_maintenance_guide", {"name": "flush_dns_cache"})
    )
    assert payload["commands"], "guides must show the exact command to run"
    assert payload["explanation"], "guides must explain what the command does"
    assert payload["needs_sudo"] is True


@pytest.mark.parametrize(
    "recipe",
    [
        "flush_dns_cache",
        "reindex_spotlight",
        "speed_up_boot",
        "speed_up_mail",
        "repair_disk_permissions",
    ],
)
def test_every_recipe_named_in_the_system_prompt_exists(recipe):
    """The prompt advertises these; a missing one is a broken promise."""
    from mac_cleaner import llm, maintenance

    assert recipe in llm.SYSTEM_PROMPT, f"{recipe} is not documented to the model"
    assert recipe in maintenance.MAINTENANCE_GUIDES


def test_unknown_recipe_lists_the_real_ones_instead_of_guessing():
    payload = json.loads(
        execute_tool("get_maintenance_guide", {"name": "reboot_my_life"})
    )
    assert payload["commands"] == [], "an unknown guide must not invent commands"
    assert "flush_dns_cache" in payload["explanation"]


def test_every_guide_agrees_with_itself_about_sudo():
    """If a guide's commands need sudo, the guide must admit it."""
    from mac_cleaner import maintenance

    for name, guide in maintenance.MAINTENANCE_GUIDES.items():
        assert guide["explanation"].strip(), f"{name} needs an explanation"
        assert isinstance(guide["needs_sudo"], bool), name
        if any(cmd.strip().startswith("sudo") for cmd in guide["commands"]):
            assert guide["needs_sudo"] is True, (
                f"{name} shows sudo commands but claims it needs no admin rights"
            )
