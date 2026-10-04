"""
The Qt layer.

gui.py is the largest module in the repo and used to have no tests at all.
These run under the offscreen platform plugin, so no window ever appears:
they build the real widgets, read back what the user would see, and drive
the maintenance worker with the underlying maintenance functions stubbed —
nothing here runs `ps`, `du` or `tmutil` against the real machine.

Skipped wholesale when PySide6 is missing (the Linux job), so the rest of the
suite still runs without Qt.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="GUI tests need PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QTabWidget, QWidget  # noqa: E402

from mac_cleaner import gui, maintenance, store  # noqa: E402
from mac_cleaner.config import Settings  # noqa: E402
from mac_cleaner.scanner import get_disk_usage  # noqa: E402

# Selected by the macOS CI job (`pytest -m gui`); skipped by importorskip
# everywhere else.
pytestmark = pytest.mark.gui


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def stub_maintenance(monkeypatch):
    """Neutralise every maintenance call the widgets make while building."""
    monkeypatch.setattr(
        maintenance,
        "hung_applications",
        lambda: {
            "success": True,
            "gui_apps": [
                {"name": "Safari", "pid": 11, "bundle_id": "com.apple.Safari"},
                {"name": "Stuck", "pid": 22, "bundle_id": "com.example.Stuck"},
            ],
            "suspected_hung": [{"pid": 22, "name": "Stuck"}],
        },
    )
    monkeypatch.setattr(
        maintenance, "list_installed_apps", lambda: {"success": True, "apps": []}
    )
    monkeypatch.setattr(
        maintenance,
        "find_large_files",
        lambda **kw: {"success": True, "files": [], "partial_scan": False},
    )
    monkeypatch.setattr(
        maintenance,
        "find_duplicates",
        lambda **kw: {"success": True, "groups": [], "partial_scan": False},
    )
    return monkeypatch


@pytest.fixture
def window(app, stub_maintenance):
    win = gui.PoofMacWindow(Settings(), gui.DARK)
    yield win
    win.close()
    win.deleteLater()


@pytest.fixture
def dialog(app, stub_maintenance):
    parent = QWidget()
    dlg = gui.MaintenanceDialog(parent)
    yield dlg
    dlg.close()
    dlg.deleteLater()
    parent.deleteLater()


def _run_worker(action, **kwargs):
    """Run a MaintenanceWorker to completion and return its (ok, report).

    QThread signals are queued: worker.wait() blocks the main thread, so the
    event loop has to be spun afterwards to actually deliver them.
    """
    app = QApplication.instance()
    worker = gui.MaintenanceWorker(action, **kwargs)
    received: list[tuple] = []
    worker.action_done.connect(lambda a, ok, report: received.append((ok, report)))
    worker.start()
    assert worker.wait(20_000), f"{action!r} never finished"
    deadline = time.monotonic() + 10
    while not received and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)
    assert received, f"{action!r} produced no signal"
    return received[0]


# ── The window ────────────────────────────────────────────────────────────────

def test_the_window_constructs(window):
    assert window is not None
    assert window.disk_pct_lbl.text() != ""


def test_the_disk_header_shows_the_real_number(window, app):
    """Regression: this read 9% while the disk was 91% full.

    The label is filled in when the header refreshes, so a disk that moves
    between then and the fresh reading here can differ by a rounding step —
    allow one percent rather than demanding an identical string.
    """
    window._refresh_disk_overview()
    app.processEvents()
    usage = get_disk_usage()
    shown = int(window.disk_pct_lbl.text().rstrip("%"))
    assert abs(shown - usage["used_percent"]) <= 1
    assert window.disk_bar.value() == shown
    title = window.disk_title_lbl.text()
    assert "used" in title and "free" in title


def test_a_nearly_full_disk_turns_the_bar_red(window, app):
    window._refresh_disk_overview()
    app.processEvents()
    percent = int(window.disk_pct_lbl.text().rstrip("%"))
    expected = gui.DARK.red if percent > 85 else gui.DARK.orange if percent > 70 else gui.DARK.accent
    assert gui.DARK.red in window.disk_bar.styleSheet() or percent <= 70
    assert expected in window.disk_bar.styleSheet()


def test_logging_keeps_the_html_and_the_text(window):
    window._log("<b>hello</b> world")
    assert "hello" in window.log_view.toPlainText()


# ── The maintenance dialog ────────────────────────────────────────────────────

def test_all_five_tabs_exist(dialog):
    tabs = dialog.findChild(QTabWidget)
    assert tabs is not None, "the dialog has no tab widget"
    titles = [tabs.tabText(i) for i in range(tabs.count())]
    for expected in (
        "Maintenance",
        "App Uninstaller",
        "Large Files",
        "Duplicates",
        "Schedule",
    ):
        assert expected in titles, f"missing tab {expected!r} (have {titles})"


def test_clicking_through_every_tab_survives(dialog, app):
    tabs = dialog.findChild(QTabWidget)
    for index in range(tabs.count()):
        tabs.setCurrentIndex(index)
        app.processEvents()


def test_the_hung_app_table_is_filled_from_the_report(dialog):
    """_refresh_apps reads maintenance.hung_applications() at open time."""
    assert dialog.apps_table.rowCount() == 2
    first = dialog.apps_table.item(0, 0).text()
    assert first == "Safari", "rows must be sorted by name"
    statuses = {dialog.apps_table.item(r, 0).text(): dialog.apps_table.item(r, 2).text() for r in range(2)}
    assert "hung" in statuses["Stuck"]
    assert statuses["Safari"] == "running"


# ── The worker dispatch ───────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "action,function",
    [
        ("report", "hung_applications"),
        ("force_quit", "force_quit_app"),
        ("repair", "repair_applications"),
        ("accelerate", "app_acceleration"),
        ("thin_snapshots", "thin_tm_snapshots"),
        ("list_apps", "list_installed_apps"),
        ("residuals", "find_app_residuals"),
        ("uninstall", "uninstall_app"),
        ("large_files", "find_large_files"),
        ("duplicates", "find_duplicates"),
    ],
)
def test_every_tab_action_reaches_its_function(stub_maintenance, action, function):
    calls = []

    def stub(**kw):
        calls.append(kw)
        return {"success": True}

    stub_maintenance.setattr(maintenance, function, stub, raising=False)
    ok, _ = _run_worker(action)
    assert ok is True
    assert len(calls) == 1, f"{action!r} did not call {function}()"


def test_the_run_now_button_runs_the_safe_set(stub_maintenance, monkeypatch):
    """The Schedule tab's Run now must reach the scheduled runner."""
    called = []
    monkeypatch.setattr(
        gui,
        "_run_scheduled_set",
        lambda **kw: called.append(kw) or {"success": True},
    )
    ok, _ = _run_worker("scheduled_run")
    assert ok is True
    assert called, "Run now did not run anything"


def test_an_unknown_action_fails_without_crashing():
    ok, report = _run_worker("definitely_not_an_action")
    assert ok is False
    assert "Unknown action" in report


def test_a_crashing_action_is_reported_not_fatal(stub_maintenance):
    def boom(**kw):
        raise RuntimeError("something went very wrong")

    stub_maintenance.setattr(maintenance, "thin_tm_snapshots", boom)
    ok, report = _run_worker("thin_snapshots")
    assert ok is False
    assert "went very wrong" in report


def test_a_report_only_tool_counts_as_success(stub_maintenance):
    """hung_applications has no success key — producing a report IS success."""
    stub_maintenance.setattr(maintenance, "hung_applications", lambda **kw: {"apps": []})
    assert _run_worker("report")[0] is True


def test_an_error_key_counts_as_failure(stub_maintenance):
    stub_maintenance.setattr(
        maintenance, "repair_applications", lambda **kw: {"error": "not permitted"}
    )
    assert _run_worker("repair")[0] is False


def test_the_report_the_dialog_receives_is_json(stub_maintenance):
    stub_maintenance.setattr(maintenance, "repair_applications", lambda **kw: {"success": True})
    _, report = _run_worker("repair")
    assert json.loads(report) == {"success": True}


# ── The Duplicates tab ────────────────────────────────────────────────────────

def test_the_duplicates_tab_pre_checks_the_extra_copies(dialog):
    report = {
        "success": True,
        "groups": [
            {
                "size_human": "4 KB",
                "files": [
                    {"path": "/h/keep.txt", "modified": "2020-01-01", "is_oldest": True},
                    {"path": "/h/dup.txt", "modified": "2021-01-01", "is_oldest": False},
                ],
            }
        ],
        "shown_groups": 1,
        "duplicate_files": 2,
        "reclaimable_human": "4 KB",
        "partial_scan": False,
        "files_scanned": 10,
        "elapsed_seconds": 0.1,
    }
    dialog._populate_duplicates(report)
    assert dialog.dup_table.rowCount() == 2
    # The kept (oldest) copy is not a checkbox; the extra one is pre-ticked.
    keep_chk = dialog.dup_table.item(0, 0)
    dup_chk = dialog.dup_table.item(1, 0)
    assert not (keep_chk.flags() & Qt.ItemFlag.ItemIsUserCheckable)
    assert dup_chk.checkState() == Qt.CheckState.Checked
    assert dialog._dup_checked == {"/h/dup.txt"}
    assert dialog.dup_trash_btn.isEnabled()


def test_the_unique_trash_target_never_reuses_a_name(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    src = tmp_path / "report.pdf"
    src.write_text("x")
    first = gui._unique_trash_target(src)
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_text("already here")
    second = gui._unique_trash_target(src)
    assert second != first, "a second trash of the same name would overwrite the first"
    assert not second.exists()


# ── The first-run disclaimer ──────────────────────────────────────────────────

def test_the_disclaimer_is_a_one_time_gate(isolated_config):
    assert store.disclaimer_accepted() is False
    store.mark_disclaimer_accepted()
    assert store.disclaimer_accepted() is True


def test_the_disclaimer_actually_says_something(app):
    dlg = gui.DisclaimerDialog(gui.DARK)
    try:
        parts = []
        for widget in dlg.findChildren(object):
            if hasattr(widget, "toPlainText"):
                parts.append(widget.toPlainText())
            elif hasattr(widget, "text"):
                parts.append(widget.text())
        text = " ".join(parts)
        assert "Safety Disclaimer" in text
        assert "MIT" in text, "the licence should be named in the disclaimer"
        assert "poofmac.app" in text
    finally:
        dlg.close()
        dlg.deleteLater()
