"""
The agent's system prompt and its startup facts.

Importing mac_cleaner.llm pulls in litellm, which costs ~30s the first time
(LiteLLM fetches its model cost map). That is paid once per session here,
not per test, because pytest imports the module once.

The prompt is the agent's only steering mechanism, so its promises are
asserted rather than trusted: every tool it advertises must exist, and the
LIVE FACTS block must stay short and factual.
"""

from __future__ import annotations

import pytest

from mac_cleaner import llm, scheduler
from mac_cleaner.tools import TOOLS

TOOL_NAMES = {t["function"]["name"] for t in TOOLS}


# ── The prompt must not promise tools that do not exist ───────────────────────

@pytest.mark.parametrize("name", sorted(TOOL_NAMES))
def test_advertised_tools_are_implemented(name):
    assert name in llm._KNOWN_TOOLS


def test_the_prompt_documents_every_tool_it_advertises():
    """A tool the model is told about but never sees is dead weight;
    a tool it can call but the prompt never explains is worse."""
    undocumented = [n for n in sorted(TOOL_NAMES) if n not in llm.SYSTEM_PROMPT]
    assert not undocumented, f"tools missing from the system prompt: {undocumented}"


def test_the_prompt_never_contradicts_the_safety_rules():
    assert "NEVER" in llm.SYSTEM_PROMPT
    for forbidden in ("/System", "Documents", "Keychain"):
        assert forbidden in llm.SYSTEM_PROMPT


def test_the_prompt_forbids_shell_access():
    """The agent has tools, not a shell. The prompt has to say so."""
    assert "NEVER run shell commands" in llm.SYSTEM_PROMPT


def test_the_prompt_explains_startup_facts():
    assert "STARTUP FACTS" in llm.SYSTEM_PROMPT
    assert "LIVE FACTS" in llm.SYSTEM_PROMPT
    # A snapshot can go stale — the prompt must warn against trusting it blindly.
    assert "stale" in llm.SYSTEM_PROMPT


# ── Startup facts ─────────────────────────────────────────────────────────────

def _status(installed, runs):
    return {
        "installed": installed,
        "interval_hours": 168 if installed else 0,
        "interval_label": "Weekly" if installed else "",
        "last_runs": runs,
    }


def _run(ran_at, success, thinned=0):
    return {
        "ran_at": ran_at,
        "success": success,
        "snapshots_thinned": thinned,
        "large_files_top": [],
    }


def test_facts_report_disk_usage(monkeypatch):
    monkeypatch.setattr(
        llm,
        "get_disk_usage",
        lambda: {
            "total": 100,
            "used": 91,
            "free": 9,
            "total_human": "100 GB",
            "used_human": "91 GB",
            "free_human": "9 GB",
            "used_percent": 91.0,
        },
    )
    monkeypatch.setattr(scheduler, "schedule_status", lambda: _status(False, []))
    facts = llm._startup_facts()
    assert "91.0% full" in facts
    assert "9 GB free" in facts


def test_facts_say_when_the_timer_is_off(monkeypatch):
    monkeypatch.setattr(scheduler, "schedule_status", lambda: _status(False, []))
    assert "not installed" in llm._startup_facts()


def test_facts_report_the_last_run_outcome(monkeypatch):
    monkeypatch.setattr(
        scheduler,
        "schedule_status",
        lambda: _status(True, [_run("2026-09-30 03:00:00", True, thinned=3)]),
    )
    facts = llm._startup_facts()
    assert "Weekly" in facts
    assert "2026-09-30 03:00:00" in facts
    assert "ok" in facts
    assert "3 snapshot(s) thinned" in facts


def test_a_failed_run_is_called_out_explicitly(monkeypatch):
    """The point of the block: the user hears about it without asking."""
    monkeypatch.setattr(
        scheduler,
        "schedule_status",
        lambda: _status(True, [_run("2026-09-30 03:00:00", False)]),
    )
    assert "FAILED" in llm._startup_facts()


def test_an_installed_timer_with_no_history_says_so(monkeypatch):
    monkeypatch.setattr(scheduler, "schedule_status", lambda: _status(True, []))
    facts = llm._startup_facts()
    assert "no runs recorded" in facts


def test_history_survives_the_user_removing_the_timer(monkeypatch):
    monkeypatch.setattr(
        scheduler,
        "schedule_status",
        lambda: _status(False, [_run("2026-09-01 03:00:00", False)]),
    )
    facts = llm._startup_facts()
    assert "not installed" in facts
    assert "2026-09-01 03:00:00" in facts, "a recent failure must still surface"
    assert "FAILED" in facts


def test_facts_stay_short_enough_to_be_free(monkeypatch):
    monkeypatch.setattr(
        scheduler,
        "schedule_status",
        lambda: _status(True, [_run("2026-09-30 03:00:00", True, thinned=2)]),
    )
    assert len(llm._startup_facts()) < 400


def test_a_broken_fact_source_never_breaks_the_chat(monkeypatch):
    def boom():
        raise RuntimeError("no disk for you")

    monkeypatch.setattr(llm, "get_disk_usage", boom)

    def bad_status():
        raise RuntimeError("no schedule for you")

    monkeypatch.setattr(scheduler, "schedule_status", bad_status)
    assert llm._startup_facts() == ""  # empty block, not a crash


def test_the_agent_gets_one_system_message_with_the_facts_appended(monkeypatch):
    monkeypatch.setattr(
        llm,
        "get_disk_usage",
        lambda: {
            "total": 100,
            "used": 91,
            "free": 9,
            "total_human": "100 GB",
            "used_human": "91 GB",
            "free_human": "9 GB",
            "used_percent": 91.0,
        },
    )
    monkeypatch.setattr(
        scheduler, "schedule_status", lambda: _status(True, [_run("2026-09-30 03:00:00", False)])
    )
    prompt = llm._build_system_prompt()
    assert prompt.startswith(llm.SYSTEM_PROMPT)
    assert "LIVE FACTS — snapshot at" in prompt
    assert prompt.rstrip().endswith("thinned).")
    # One system message, one copy of the base prompt.
    assert prompt.count("You are PoofMac, a Mac maintenance assistant") == 1


def test_no_facts_means_the_plain_prompt(monkeypatch):
    monkeypatch.setattr(
        llm, "get_disk_usage", lambda: (_ for _ in ()).throw(RuntimeError("no disk"))
    )
    monkeypatch.setattr(
        scheduler,
        "schedule_status",
        lambda: (_ for _ in ()).throw(RuntimeError("no schedule")),
    )
    assert llm._build_system_prompt() == llm.SYSTEM_PROMPT


# ── Text-format tool calls (models that skip the tool_calls field) ────────────

def test_json_tool_calls_in_plain_text_are_recognised():
    text = (
        'Thought: I should look.\n'
        '```json\n{"name": "get_disk_overview", "arguments": {}}\n```'
    )
    calls = llm._extract_text_tool_calls(text)
    assert ("get_disk_overview", {}) in calls


def test_text_calls_for_unknown_tools_are_dropped():
    """Otherwise a hallucinated name could reach execute_tool unfiltered."""
    text = '{"name": "rm_rf_everything", "arguments": {}}'
    assert llm._extract_text_tool_calls(text) == []


def test_malformed_tool_call_json_is_ignored():
    assert llm._extract_text_tool_calls("not json at all") == []
    assert llm._extract_text_tool_calls("") == []
