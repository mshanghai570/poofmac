"""
Persisted state: the maintenance log, the schedule, and the disclaimer marker.

All of it lives in the per-user config dir, which conftest points at tmp_path
and (for the plist) replaces outright — no test here can install a real
LaunchAgent or write to a real ~/Library/Application Support.
"""

from __future__ import annotations

import json
import time

import pytest

from mac_cleaner import scheduler, store


# ── Disclaimer marker ─────────────────────────────────────────────────────────

def test_disclaimer_is_required_on_first_run(isolated_config):
    assert store.disclaimer_accepted() is False


def test_acceptance_is_recorded_and_sticks(isolated_config):
    store.mark_disclaimer_accepted()
    assert store.disclaimer_accepted() is True
    assert store.disclaimer_file().is_file()
    # A second launch must not re-ask: read it again from the same dir.
    assert store.disclaimer_accepted() is True


def test_marker_lives_in_the_config_dir(isolated_config):
    store.mark_disclaimer_accepted()
    assert store.disclaimer_file().parent == isolated_config


def test_unwritable_location_does_not_raise(isolated_config, monkeypatch):
    """Worst case the disclaimer reappears; it must never crash startup."""
    monkeypatch.setattr(
        store.Path, "write_text", lambda *a, **k: (_ for _ in ()).throw(OSError("ro"))
    )
    store.mark_disclaimer_accepted()  # must not raise
    assert store.disclaimer_accepted() is False


# ── Maintenance log ───────────────────────────────────────────────────────────

def _report(**overrides):
    report = {
        "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "success": True,
        "snapshots_thinned": 2,
        "large_files_top": [],
    }
    report.update(overrides)
    return report


def test_first_run_creates_the_log_file(isolated_config):
    assert scheduler.recent_runs() == []
    scheduler._append_log(_report())
    assert scheduler._log_path().is_file()


def test_log_round_trips_json(isolated_config):
    scheduler._append_log(_report(ran_at="2026-01-02 03:04:05"))
    runs = scheduler.recent_runs(3)
    assert len(runs) == 1
    assert runs[0]["ran_at"] == "2026-01-02 03:04:05"
    assert runs[0]["snapshots_thinned"] == 2


def test_runs_are_newest_first(isolated_config):
    for day in ("01", "02", "03"):
        scheduler._append_log(_report(ran_at=f"2026-01-{day} 00:00:00"))
    assert [r["ran_at"] for r in scheduler.recent_runs(3)] == [
        "2026-01-03 00:00:00",
        "2026-01-02 00:00:00",
        "2026-01-01 00:00:00",
    ]


def test_count_is_honoured(isolated_config):
    for day in range(1, 6):
        scheduler._append_log(_report(ran_at=f"2026-01-0{day} 00:00:00"))
    assert len(scheduler.recent_runs(2)) == 2


def test_a_truncated_line_does_not_break_the_history(isolated_config):
    """A crashed write can leave half a JSON line; skip it, keep the rest."""
    scheduler._append_log(_report(ran_at="2026-01-01 00:00:00"))
    with scheduler._log_path().open("a", encoding="utf-8") as handle:
        handle.write('{"ran_at": "2026-01-02 00:00')
    runs = scheduler.recent_runs(5)
    assert len(runs) == 1
    assert runs[0]["ran_at"] == "2026-01-01 00:00:00"


def test_log_stays_bounded(isolated_config):
    for i in range(scheduler._MAX_LOG_LINES + 40):
        scheduler._append_log(_report(ran_at=f"run-{i}"))
    lines = scheduler._log_path().read_text().splitlines()
    assert len(lines) <= scheduler._MAX_LOG_LINES
    # The newest entry survives the trim.
    assert scheduler.recent_runs(1)[0]["ran_at"] == f"run-{scheduler._MAX_LOG_LINES + 39}"


def test_history_summary_counts_successes(isolated_config):
    scheduler._append_log(_report(success=True))
    scheduler._append_log(_report(success=False))
    scheduler._append_log(_report(success=True))
    summary = scheduler.history_summary(10)
    assert summary["runs_found"] == 3
    assert summary["successful"] == 2
    assert summary["log_path"] == str(scheduler._log_path())


def test_history_summary_on_a_fresh_install(isolated_config):
    summary = scheduler.history_summary(10)
    assert summary["runs_found"] == 0
    assert summary["successful"] == 0
    assert summary["scheduled"] is False


def test_history_summary_never_claims_it_deletes(isolated_config):
    """The safe set reports; it must never delete. Keep the promise honest."""
    assert "never" in scheduler.history_summary()["note"].lower()


# ── Schedule ──────────────────────────────────────────────────────────────────

def test_schedule_reports_not_installed_by_default(fake_plist):
    status = scheduler.schedule_status()
    assert status["installed"] is False
    assert status["last_runs"] == []
    assert not fake_plist.exists()


def test_status_reads_the_installed_interval(fake_plist, isolated_config):
    scheduler.schedule_enable(interval_hours=168)
    status = scheduler.schedule_status()
    assert status["installed"] is True
    assert status["interval_hours"] == 168
    assert status["interval_label"] == "Weekly"


def test_disable_removes_the_plist(fake_plist):
    scheduler.schedule_enable(interval_hours=24)
    assert fake_plist.exists()
    assert scheduler.schedule_disable()["success"] is True
    assert not fake_plist.exists()
    assert scheduler.schedule_status()["installed"] is False


def test_disable_when_nothing_is_installed_is_not_an_error(fake_plist):
    assert scheduler.schedule_disable()["success"] is True


def test_installed_plist_points_at_this_interpreter(fake_plist):
    scheduler.schedule_enable(interval_hours=24)
    text = fake_plist.read_text()
    assert scheduler.LABEL in text
    assert "--maintain" in text, "the agent must run the headless safe set"
    assert "StartInterval" in text
    assert any(c[:2] == ["launchctl", "load"] for c in fake_plist.launchctl_calls)


@pytest.mark.parametrize("hours", [0, -1])
def test_a_zero_or_negative_interval_is_refused(fake_plist, hours):
    result = scheduler.schedule_enable(interval_hours=hours)
    assert result.get("error"), result
    assert not fake_plist.exists()


def test_the_schedule_needs_macos(fake_plist, monkeypatch):
    """Off macOS the tool must explain itself, not install a broken plist."""
    monkeypatch.setattr(scheduler.sys, "platform", "linux")
    result = scheduler.schedule_enable(interval_hours=24)
    assert "macOS" in result.get("error", "")
    assert not fake_plist.exists()


def test_corrupt_plist_reads_as_not_installed(fake_plist):
    fake_plist.write_text("this is not a plist")
    assert scheduler.schedule_status()["installed"] is False


def test_interval_labels_cover_the_offered_choices():
    for hours, label in scheduler.INTERVAL_CHOICES:
        assert scheduler._interval_label(hours) == label
    assert scheduler._interval_label(5) == "Every 5h"


def test_agent_command_is_a_runnable_module_invocation():
    command = scheduler._agent_command()
    assert command[1:] == ["-m", "mac_cleaner", "--maintain"], command
