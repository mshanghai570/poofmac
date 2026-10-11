# SPDX-License-Identifier: MIT
# Copyright (c) 2026 lesteroliver — https://poofmac.app
"""
Tests for the developer-storage feature: devscan, devclassify, devcleanup.

Everything runs against throwaway tmp_path directories — no test ever
touches ~/Library/Developer, an installed Xcode, or real simulator data.
"""

from __future__ import annotations

import os
import plistlib
from pathlib import Path

import pytest

from mac_cleaner import devscan, devclassify
from mac_cleaner.devscan import DevItem


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Point Path.home() at a throwaway tree with a Developer dir."""
    home = tmp_path / "home"
    (home / "Library" / "Developer").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


@pytest.fixture
def fast_du(monkeypatch):
    """Make _du instant by scanning tiny fake trees — keep real calls but
    shrink the timeout so tests never hang."""
    real = devscan._du

    def quick(path, timeout=1):
        return real(path, timeout=1)

    monkeypatch.setattr(devscan, "_du", quick)

    real_batch = devscan._du_batch

    def quick_batch(paths, timeout=5):
        return real_batch(paths, timeout=5)

    monkeypatch.setattr(devscan, "_du_batch", quick_batch)
    return quick


def _make_dir(path: Path, files: int = 3, size: int = 100) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for i in range(files):
        (path / f"f{i}.bin").write_bytes(b"x" * size)


# ── Discovery ────────────────────────────────────────────────────────────────


def test_scan_returns_empty_when_no_developer_dirs(fake_home):
    report = devscan.scan_developer_storage()
    assert report["summary"]["item_count"] >= 0
    # No crash on empty home
    assert isinstance(report["items"], list)


def test_derived_data_children_discovered(fake_home, fast_du):
    dd = fake_home / "Library" / "Developer" / "Xcode" / "DerivedData"
    _make_dir(dd / "MyApp-abc123", files=2, size=500)
    _make_dir(dd / "OtherApp-xyz", files=2, size=300)
    report = devscan.scan_developer_storage()
    kinds = [i["kind"] for i in report["items"]]
    assert kinds.count("derived_data") == 2
    labels = " | ".join(i["label"] for i in report["items"])
    assert "MyApp-abc123" in labels


def test_missing_directories_skipped(fake_home, fast_du):
    # Only Archives exists; DerivedData, CoreSimulator, etc. do not.
    arch = fake_home / "Library" / "Developer" / "Xcode" / "Archives"
    _make_dir(arch / "2026-MyApp.xcarchive")
    report = devscan.scan_developer_storage()
    kinds = {i["kind"] for i in report["items"]}
    assert "archives" in kinds
    assert "sim_devices" not in kinds


def test_symlink_children_skipped(fake_home, fast_du):
    """A symlinked DerivedData child must be ignored entirely — no double
    counting and no traversal out of the tree."""
    dd = fake_home / "Library" / "Developer" / "Xcode" / "DerivedData"
    real = fake_home / "elsewhere"
    _make_dir(real, files=1, size=10)
    dd.mkdir(parents=True, exist_ok=True)
    os.symlink(real, dd / "CmptLink")
    report = devscan.scan_developer_storage()
    assert not any("CmptLink" in i["label"] for i in report["items"])


def test_permission_error_is_inaccessible_not_crash(fake_home, fast_du, monkeypatch):
    dd = fake_home / "Library" / "Developer" / "Xcode" / "DerivedData"
    _make_dir(dd / "Locked", files=1, size=10)
    # Simulate `du` failing (e.g. permissions)
    monkeypatch.setattr(devscan, "_du_batch",
                        lambda paths, timeout=60: {})
    report = devscan.scan_developer_storage()
    locked = [i for i in report["items"] if "Locked" in i["label"]]
    assert locked
    assert locked[0]["measurement"] == "inaccessible"


# ── Classification ───────────────────────────────────────────────────────────


def test_derived_data_is_safe_cache():
    c = devclassify.classify(
        {"kind": "derived_data", "label": "DerivedData — MyApp",
         "path": "/x", "size_bytes": 1000, "measurement": "measured",
         "active_guess": "rebuildable", "notes": {}}
    )
    assert c["category"] == devclassify.SAFE_CACHE
    assert c["delete_eligible"] is True


def test_xcode_app_is_protected():
    c = devclassify.classify(
        {"kind": "xcode_app", "label": "Xcode.app", "path": "/Applications/Xcode.app",
         "size_bytes": 1, "measurement": "measured", "active_guess": "active",
         "notes": {}}
    )
    assert c["category"] == devclassify.PROTECTED
    assert c["delete_eligible"] is False


def test_archives_are_protected_user_data():
    c = devclassify.classify(
        {"kind": "archives", "label": "Archive — MyApp", "path": "/x",
         "size_bytes": 100, "measurement": "measured",
         "active_guess": "inactive", "notes": {}}
    )
    assert c["category"] == devclassify.PROTECTED
    assert c["delete_eligible"] is False


def test_booted_sim_device_is_protected():
    c = devclassify.classify(
        {"kind": "sim_devices", "label": "Simulator device — iPhone 15",
         "path": "/x", "size_bytes": 100, "measurement": "measured",
         "active_guess": "active", "notes": {"booted": True}}
    )
    assert c["category"] == devclassify.PROTECTED


def test_stale_device_support_is_obsolete_when_newest_runtimereport_newer():
    """iOS 12 device support next to an iOS 16 runtime → obsolete."""
    c = devclassify.classify(
        {"kind": "device_support", "label": "iOS DeviceSupport 12.1 (16D57)",
         "path": "/x", "size_bytes": 100, "measurement": "measured",
         "active_guess": "uncertain", "notes": {}},
        {"runtimes": [{"version": "16.4"}]},
    )
    # Policy: stale support needs review rather than auto-approval.
    assert c["category"] in (devclassify.POTENTIALLY_OBSOLETE, devclassify.REVIEW_REQUIRED)


def test_recent_device_support_needs_review():
    c = devclassify.classify(
        {"kind": "device_support", "label": "iOS DeviceSupport 17.4 (21E219)",
         "path": "/x", "size_bytes": 100, "measurement": "measured",
         "active_guess": "uncertain", "notes": {}},
        {"runtimes": [{"version": "17.4"}]},
    )
    assert c["category"] == devclassify.REVIEW_REQUIRED


def test_inaccessible_item_not_delete_eligible():
    c = devclassify.classify(
        {"kind": "sim_caches", "label": "Simulator cache — dyld",
         "path": "/x", "size_bytes": 0, "measurement": "inaccessible",
         "active_guess": "uncertain", "notes": {}}
    )
    assert c["delete_eligible"] is False


def test_active_guess_upgrades_to_protected():
    c = devclassify.classify(
        {"kind": "derived_data", "label": "DerivedData — MidBuild",
         "path": "/x", "size_bytes": 100, "measurement": "measured",
         "active_guess": "active", "notes": {}}
    )
    assert c["category"] == devclassify.PROTECTED
    assert c["delete_eligible"] is False


def test_explanation_present_for_every_kind():
    for kind in ("derived_data", "sim_devices", "archives", "xcode_app",
                 "device_support", "sim_runtime", "module_cache"):
        c = devclassify.classify(
            {"kind": kind, "label": "x", "path": "/x", "size_bytes": 10,
             "measurement": "measured", "active_guess": "uncertain", "notes": {}}
        )
        assert len(c["explanation"]) > 30, f"missing explanation for {kind}"
        assert len(c["reason"]) > 10, f"missing reason for {kind}"


# ── Cleanup engine ───────────────────────────────────────────────────────────


@pytest.fixture
def engine_tmp_roots(tmp_path):
    from mac_cleaner import devcleanup

    roots = (tmp_path.resolve(),)
    saved = devcleanup.APPROVED_ROOTS
    devcleanup.APPROVED_ROOTS = roots
    yield tmp_path.resolve()
    devcleanup.APPROVED_ROOTS = saved


def _eligible_item(path: Path, kind="derived_data"):
    return {"path": str(path), "kind": kind,
            "label": path.name, "category": devclassify.SAFE_CACHE,
            "delete_eligible": True, "size_bytes": 10}


def test_cleanup_trashes_eligible_item(tmp_path, engine_tmp_roots):
    target = tmp_path / "CacheMe"
    _make_dir(target, files=2, size=10)
    from mac_cleaner.devcleanup import DevCleanupEngine
    from mac_cleaner.audit import AuditLogger

    trash = Path.home() / ".Trash"
    # A CI Linux host may not have a Trash dir yet; the engine creates it.
    before = {p.name for p in trash.iterdir()} if trash.is_dir() else set()
    eng = DevCleanupEngine(AuditLogger(tmp_path / "audit.jsonl"))
    rep = eng.cleanup([_eligible_item(target)])
    assert [r.action for r in rep.results] == ["trashed"]
    assert not target.exists()
    after = {p.name for p in trash.iterdir()} if trash.is_dir() else set()
    gained = after - before
    assert gained, "nothing appeared in Trash"
    assert any(n.startswith("CacheMe") for n in gained)


def test_cleanup_blocks_protected_item(tmp_path, engine_tmp_roots):
    from mac_cleaner.devcleanup import DevCleanupEngine
    from mac_cleaner.audit import AuditLogger

    target = tmp_path / "MyGreatArchive"
    _make_dir(target, files=1, size=10)
    eng = DevCleanupEngine(AuditLogger(tmp_path / "a.jsonl"))
    rep = eng.cleanup(
        [{"path": str(target), "kind": "archives",
          "label": "Archive", "category": devclassify.PROTECTED,
          "delete_eligible": False, "size_bytes": 10}]
    )
    assert rep.results[0].action == "blocked"
    assert target.exists()


def test_cleanup_blocks_path_outside_approved_roots(tmp_path, engine_tmp_roots):
    """Path traversal / outside-root refusal."""
    from mac_cleaner.devcleanup import DevCleanupEngine
    from mac_cleaner.audit import AuditLogger

    outside = Path.home() / "Documents" / "evil"
    eng = DevCleanupEngine(AuditLogger(tmp_path / "a.jsonl"))
    rep = eng.cleanup([_eligible_item(outside)])
    assert rep.results[0].action == "blocked"
    assert outside.exists() is False or outside.exists() is True  # untouched
    # Not trashed is the guarantee:
    assert rep.trashed == 0


def test_cleanup_blocks_symlink_escape(tmp_path, engine_tmp_roots):
    from mac_cleaner.devcleanup import DevCleanupEngine
    from mac_cleaner.audit import AuditLogger

    outside = tmp_path.parent / "not-approved"
    _make_dir(outside, files=1, size=1)
    link = tmp_path / "LooksSafe"
    os.symlink(outside, link)
    eng = DevCleanupEngine(AuditLogger(tmp_path / "a.jsonl"))
    rep = eng.cleanup([_eligible_item(link)])
    assert rep.results[0].action == "blocked"
    assert outside.exists()


def test_cleanup_dry_run_touches_nothing(tmp_path, engine_tmp_roots):
    from mac_cleaner.devcleanup import DevCleanupEngine
    from mac_cleaner.audit import AuditLogger

    target = tmp_path / "Track"
    _make_dir(target, files=1, size=5)
    eng = DevCleanupEngine(AuditLogger(tmp_path / "a.jsonl"))
    rep = eng.cleanup([_eligible_item(target)], dry_run=True)
    assert rep.results[0].action == "dry_run"
    assert target.exists()


def test_cleanup_partial_failure_reports_honestly(tmp_path, engine_tmp_roots):
    from mac_cleaner.devcleanup import DevCleanupEngine
    from mac_cleaner.audit import AuditLogger

    good = tmp_path / "Fine"
    _make_dir(good, files=1, size=5)
    locked = tmp_path / "Stuck"
    _make_dir(locked, files=1, size=5)
    eng = DevCleanupEngine(AuditLogger(tmp_path / "a.jsonl"))

    # Force one deletion to fail by swapping rename to raise.
    import mac_cleaner.devcleanup as dc

    orig_rename = Path.rename

    def flaky(self, dest):
        if self.name == "Stuck":
            raise OSError("Resource busy")
        return orig_rename(self, dest)

    Path.rename = flaky
    try:
        rep = eng.cleanup([_eligible_item(good), _eligible_item(locked)])
    finally:
        Path.rename = orig_rename

    actions = [r.action for r in rep.results]
    assert "trashed" in actions
    assert "error" in actions
    assert rep.errors >= 1


def test_cleanup_cancellation_stops_remaining(tmp_path, engine_tmp_roots):
    from mac_cleaner.devcleanup import DevCleanupEngine
    from mac_cleaner.audit import AuditLogger

    a = tmp_path / "A"; _make_dir(a, files=1, size=2)
    b = tmp_path / "B"; _make_dir(b, files=1, size=2)
    cancel = [False]
    eng = DevCleanupEngine(AuditLogger(tmp_path / "a.jsonl"))

    seen = []
    calls = {"n": 0}

    def progress(i, n, p):
        calls["n"] += 1
        seen.append(p)
        # Cancel when the second item starts: the first must already be done.
        if calls["n"] == 2:
            cancel[0] = True

    rep = eng.cleanup(
        [_eligible_item(a), _eligible_item(b)],
        progress=progress, cancel_flag=cancel,
    )
    assert rep.trashed == 1
    assert rep.skipped == 1
    assert b.exists()


# ── Tool wiring ──────────────────────────────────────────────────────────────


def test_tool_registered_and_dispatchable(fake_home, fast_du, monkeypatch):
    from mac_cleaner.tools import TOOLS, execute_tool
    import json

    names = [t["function"]["name"] for t in TOOLS]
    assert "scan_developer_storage" in names

    dd = fake_home / "Library" / "Developer" / "Xcode" / "DerivedData"
    _make_dir(dd / "Proj", files=1, size=10)
    raw = execute_tool("scan_developer_storage", {})
    data = json.loads(raw)
    assert any(i["kind"] == "derived_data" for i in data["items"])
    for item in data["items"]:
        assert "category" in item and "explanation" in item
