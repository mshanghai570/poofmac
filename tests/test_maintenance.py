"""
Maintenance actions that touch the running system.

force_quit_app kills processes, thin_tm_snapshots asks Time Machine to delete
snapshots, toggle_launch_agent renames files in the user's LaunchAgents
folder. None of them are exercised against the real system here: the tests
monkeypatch the ``_run`` helper (the single choke point for shelling out) and
os.kill, then assert on the exact command that *would* have run.

The regression worth remembering: thin_tm_snapshots once passed an int where
subprocess.run wanted a list, so the scheduled maintenance set raised a
TypeError on every single run.
"""

from __future__ import annotations

import signal

import pytest

from mac_cleaner import maintenance


@pytest.fixture
def recorded(monkeypatch):
    """Capture every shell command maintenance.py would have run."""
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=10.0):
        calls.append(list(cmd))
        return True, ""

    monkeypatch.setattr(maintenance, "_run", fake_run)
    return calls


@pytest.fixture
def signals(monkeypatch):
    """Capture signals instead of delivering them to real processes."""
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(
        maintenance.os, "kill", lambda pid, sig: sent.append((pid, sig))
    )
    return sent


def _apps(monkeypatch, apps):
    monkeypatch.setattr(maintenance, "_foreground_apps", lambda: apps)


# ── force_quit_app ────────────────────────────────────────────────────────────

@pytest.fixture
def finder_running(monkeypatch):
    _apps(
        monkeypatch,
        [
            {"name": "Finder", "pid": 101, "bundle_id": "com.apple.finder"},
            {"name": "Safari", "pid": 202, "bundle_id": "com.apple.Safari"},
            {"name": "TextEdit", "pid": 303, "bundle_id": "com.apple.TextEdit"},
        ],
    )


@pytest.mark.parametrize(
    "bundle_id",
    [
        "com.apple.finder",
        "com.apple.dock",
        "com.apple.WindowServer",
        "com.apple.loginwindow",
        "com.apple.systemuiserver",
    ],
)
def test_session_apps_cannot_be_killed(monkeypatch, signals, bundle_id):
    """Killing Finder or the WindowServer would take the desktop with it."""
    _apps(monkeypatch, [{"name": "System", "pid": 1, "bundle_id": bundle_id}])
    result = maintenance.force_quit_app("System", force=True)
    assert result["success"] is False
    assert "session app" in result["error"]
    assert signals == [], "no signal may be sent to a protected app"


def test_protection_is_case_insensitive(monkeypatch, signals):
    """Bundle ids vary in case; the check must not be defeated by COM.APPLE."""
    _apps(monkeypatch, [{"name": "Finder", "pid": 1, "bundle_id": "COM.APPLE.FINDER"}])
    assert maintenance.force_quit_app("Finder", force=True)["success"] is False
    assert signals == []


def test_a_normal_app_is_asked_to_quit_politely_first(monkeypatch, signals):
    _apps(monkeypatch, [{"name": "Safari", "pid": 202, "bundle_id": "com.apple.Safari"}])
    result = maintenance.force_quit_app("Safari")
    assert result["success"] is True
    assert signals == [(202, signal.SIGTERM)], "default must be SIGTERM, not SIGKILL"


def test_force_escalates_to_sigkill(monkeypatch, signals):
    _apps(monkeypatch, [{"name": "Safari", "pid": 202, "bundle_id": "com.apple.Safari"}])
    maintenance.force_quit_app("Safari", force=True)
    assert signals == [(202, signal.SIGKILL)]


def test_lookup_works_by_name_bundle_id_and_pid(monkeypatch, signals, finder_running):
    maintenance.force_quit_app("TextEdit")
    maintenance.force_quit_app("com.apple.TextEdit")
    maintenance.force_quit_app("303")
    assert [pid for pid, _ in signals] == [303, 303, 303]


def test_an_unknown_app_is_reported_not_killed(monkeypatch, signals, finder_running):
    result = maintenance.force_quit_app("Photoshop")
    assert result["success"] is False
    assert "Photoshop" in result["error"]
    assert signals == []


def test_an_empty_identifier_is_refused(monkeypatch, signals):
    assert maintenance.force_quit_app("")["success"] is False
    assert signals == []


def test_an_app_that_exited_mid_lookup_is_not_an_error(monkeypatch, finder_running):
    def gone(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(maintenance.os, "kill", gone)
    result = maintenance.force_quit_app("TextEdit")
    assert result["success"] is True
    assert "gone" in result["note"].lower()


def test_another_users_app_is_reported_not_forced(monkeypatch, finder_running):
    def denied(pid, sig):
        raise PermissionError

    monkeypatch.setattr(maintenance.os, "kill", denied)
    result = maintenance.force_quit_app("TextEdit", force=True)
    assert result["success"] is False
    assert "another user" in result["error"]


# ── thin_tm_snapshots ─────────────────────────────────────────────────────────

def test_thinning_passes_a_command_not_a_bare_int(recorded):
    """Regression: subprocess.run(int) raised TypeError on every scheduled run."""
    result = maintenance.thin_tm_snapshots(keep_hours=24)
    assert result["success"] is True
    assert len(recorded) == 1
    cmd = recorded[0]
    assert cmd[:2] == ["tmutil", "thinlocalsnapshots"]
    assert all(isinstance(part, str) for part in cmd), cmd
    assert cmd[2] == "/"
    assert cmd[3] == str(24 * 3600 * 1_000_000_000), "keep_hours must reach tmutil"
    assert cmd[4] == "1", "urgency 1 is tmutil's gentlest request"


def test_thinning_reports_what_time_machine_deleted(monkeypatch):
    monkeypatch.setattr(
        maintenance,
        "_run",
        lambda cmd, timeout=10.0: (
            True,
            "Thinned local snapshots:\nDeleted 1 snapshot (12.5GB)\nNothing else\n",
        ),
    )
    result = maintenance.thin_tm_snapshots()
    assert result["thinned"] == ["Deleted 1 snapshot (12.5GB)"]
    assert "12.5GB" in json_blob(result)


def test_a_failing_tmutil_call_is_surfaced(monkeypatch):
    monkeypatch.setattr(
        maintenance, "_run", lambda cmd, timeout=10.0: (False, "Error: not permitted")
    )
    result = maintenance.thin_tm_snapshots()
    assert result["success"] is False
    assert "not permitted" in result["raw"]


def json_blob(payload) -> str:
    import json

    return json.dumps(payload)


# ── toggle_launch_agent ───────────────────────────────────────────────────────

@pytest.mark.parametrize("label", ["", "..", "../../etc/passwd", "a/b"])
def test_a_dodgy_launch_agent_label_is_refused(label):
    """A label becomes a filename; path traversal must never get through."""
    result = maintenance.toggle_launch_agent(label, enable=False)
    assert result["success"] is False


def test_disabling_and_re_enabling_a_user_agent(tmp_path, monkeypatch):
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    (agents / "com.example.agent.plist").write_text("<plist/>")
    monkeypatch.setattr(maintenance, "_USER_AGENTS_DIR", agents)

    assert maintenance.toggle_launch_agent("com.example.agent", enable=False)["success"]
    assert not (agents / "com.example.agent.plist").exists()
    assert (agents / "com.example.agent.plist.disabled").exists()

    assert maintenance.toggle_launch_agent("com.example.agent", enable=True)["success"]
    assert (agents / "com.example.agent.plist").exists()
    assert not (agents / "com.example.agent.plist.disabled").exists()


def test_disabling_twice_is_reported_not_done_again(tmp_path, monkeypatch):
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    (agents / "com.example.agent.plist").write_text("<plist/>")
    monkeypatch.setattr(maintenance, "_USER_AGENTS_DIR", agents)
    maintenance.toggle_launch_agent("com.example.agent", enable=False)
    second = maintenance.toggle_launch_agent("com.example.agent", enable=False)
    assert second["success"] is False
    assert "already disabled" in second["error"]


def test_an_unknown_agent_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "_USER_AGENTS_DIR", tmp_path)
    result = maintenance.toggle_launch_agent("com.nope", enable=False)
    assert result["success"] is False
    assert "com.nope" in result["error"]


def test_toggling_never_asks_for_admin_rights(tmp_path, monkeypatch):
    """User LaunchAgents only; system ones are not ours to touch."""
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    (agents / "com.example.agent.plist").write_text("<plist/>")
    monkeypatch.setattr(maintenance, "_USER_AGENTS_DIR", agents)
    result = maintenance.toggle_launch_agent("com.example.agent", enable=False)
    assert "sudo" not in result["note"]
    assert "next login" in result["note"]
