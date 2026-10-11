"""
The three quick-action maintenance features: free_purgeable_space,
flush_dns_cache, free_ram.

Same convention as the rest of test_maintenance.py: ``_run`` is monkeypatched
(the single choke point for shelling out), so nothing here touches real
system state. Real-machine behaviour was verified once by hand.

The honest-macOS constraint being pinned down by these tests:
• free_purgeable_space must call TIME MACHINE's own thinning (tmutil), never
  rm anything, and report OS-provided numbers.
• flush_dns_cache runs only dscacheutil (no sudo) and surfaces the sudo hint
  separately.
• free_ram never kills a process — its only shell-out is memory_pressure.
"""

from __future__ import annotations

import pytest

from mac_cleaner import maintenance


@pytest.fixture
def recorded(monkeypatch):
    """Capture every shell command maintenance.py would have run."""
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=10.0):
        serial = " ".join(str(c) for c in cmd)
        calls.append([serial])
        if "tmutil" in serial and "thinlocalsnapshots" in serial:
            return True, "Deleted /System/Volumes/Data/.Purgeable 2026-01-01\n"
        if "diskutil info" in serial:
            return True, "Purgeable Space: 12.0 GB\n"
        if "memory_report" in serial:  # never happens; placeholder
            return True, ""
        if "vm_stat" in serial:
            return True, (
                "Mach virtual memory statistics (page size of 16384 bytes)\n"
                "Pages free: 100000.\nPages active: 200000.\n"
                "Pages inactive: 50000.\nPages speculative: 10000.\n"
                "Pages wired down: 300000.\nPages compressed: 40000.\n"
                "Pages purgeable: 8000.\n"
            )
        if "memory_pressure" in serial:
            return True, "free percentage: 60%"
        if "ps " in serial:
            return True, (
                "%CPU %MEM PID COMM\n 1.0 1.0 100 /Applications/App.app\n"
            )
        return True, ""

    monkeypatch.setattr(maintenance, "_run", fake_run)
    return calls


# ── free_purgeable_space ─────────────────────────────────────────────────────

def test_free_purgeable_uses_tmutil_and_never_rm(recorded):
    result = maintenance.free_purgeable_space()
    flat = " | ".join(c[0] for c in recorded)
    assert "tmutil thinlocalsnapshots" in flat
    assert "rm " not in flat and "unlink" not in flat
    assert result["success"] is True
    assert any(s.get("reclaimed") for s in result["steps"]), (
        "snapshot reclamation must be surfaced"
    )


def test_free_purgeable_reports_os_numbers_honestly(recorded):
    result = maintenance.free_purgeable_space()
    # before/after keys exist and are strings or None — never a fabricated int
    assert "purgeable_before" in result and "purgeable_after" in result
    assert result["disk_free"].endswith(("B", "KB", "MB", "GB"))


# ── flush_dns_cache ──────────────────────────────────────────────────────────

def test_flush_dns_runs_dscacheutil_without_sudo(recorded):
    result = maintenance.flush_dns_cache()
    flat = " | ".join(c[0] for c in recorded)
    assert "dscacheutil -flushcache" in flat
    assert "sudo" not in flat, "no command may require elevation"
    assert "killall" not in flat
    assert result["success"] is True
    assert result["needs_sudo_hint"] == []


def test_flush_dns_reports_sudo_hint_on_failure(monkeypatch):
    def failing_run(cmd, timeout=10.0):
        return False, "operation not permitted"

    monkeypatch.setattr(maintenance, "_run", failing_run)
    result = maintenance.flush_dns_cache()
    assert result["success"] is False
    assert result["needs_sudo_hint"], "failed flush must show escalation path"
    assert all(c.startswith("sudo ") for c in result["needs_sudo_hint"])
    assert not result["user_cache_flushed"]


# ── free_ram ─────────────────────────────────────────────────────────────────

def test_free_ram_never_kills_processes(recorded, monkeypatch):
    sent = []
    import os as _os
    monkeypatch.setattr(_os, "kill", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(maintenance.os, "kill", lambda *a, **k: sent.append(a))
    result = maintenance.free_ram()
    assert sent == [], "no process may be signalled"
    flat = " | ".join(c[0] for c in recorded)
    assert "memory_pressure" in flat
    assert "kill" not in flat.replace("memory_pressure", "")
    assert result["success"] is True
    assert "free_percent_before" in result and "free_percent_after" in result


def test_free_ram_aggressive_only_wakes_purgeable(recorded):
    result = maintenance.free_ram(aggressive=True)
    flat = " | ".join(c[0] for c in recorded)
    assert "memory_pressure" in flat
    assert result["success"] is True


# ── Tool wiring ──────────────────────────────────────────────────────────────

def test_tools_registered_for_all_three():
    from mac_cleaner.tools import TOOLS, execute_tool

    names = [t["function"]["name"] for t in TOOLS]
    for name in ("free_purgeable_space", "flush_dns_cache", "free_ram"):
        assert name in names, f"{name} missing from TOOLS"

    from mac_cleaner.llm import _KNOWN_TOOLS
    for name in ("free_purgeable_space", "flush_dns_cache", "free_ram"):
        assert name in _KNOWN_TOOLS, f"{name} missing from _KNOWN_TOOLS"


def test_execute_tool_dispatches(recorded):
    from mac_cleaner.tools import execute_tool
    import json

    dns = json.loads(execute_tool("flush_dns_cache", {}))
    assert "success" in dns
    ram = json.loads(execute_tool("free_ram", {"aggressive": False}))
    assert "free_percent_after" in ram
    purged = json.loads(execute_tool("free_purgeable_space", {}))
    assert "steps" in purged
