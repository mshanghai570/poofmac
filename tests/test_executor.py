"""
The deletion engine.

executor.py is the only code in PoofMac that removes anything, so it gets
the most paranoid tests in the repo. Three gates must hold no matter what the
caller asks for: safety.assert_safe(), the dry-run/safe-mode flags, and the
audit log — every operation recorded, including the ones that were refused.

Sandboxing: validate_path() only trusts a fixed list of cache directories, so
the sandbox fixture adds the tmp dir to that list. Nothing here can therefore
delete anything outside pytest's tmp_path — and the tests that use real
protected paths only ever assert that the file SURVIVES.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from mac_cleaner import safety
from mac_cleaner.audit import AuditLogger
from mac_cleaner.executor import DeleteResult, Executor

DOCUMENTS = Path.home() / "Documents" / "executor-test-should-never-exist.txt"


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Make tmp_path a trusted target so assert_safe() lets the executor work."""
    root = tmp_path / "sandbox"
    root.mkdir()
    monkeypatch.setattr(safety, "SAFE_TARGETS", frozenset({str(root)}))
    monkeypatch.setattr(safety, "DELETE_CONTENTS", frozenset({str(root / "Caches")}))
    return root


@pytest.fixture
def audit(tmp_path):
    return AuditLogger(tmp_path / "audit.jsonl")


@pytest.fixture
def executor(audit):
    return Executor(safe_mode=False, audit=audit)


def _file(path: Path, size: int = 100) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def _entries(audit):
    lines = Path(audit.log_path).read_text().splitlines()
    return [json.loads(line) for line in lines]


# ── Gate 1: safety, before anything else ──────────────────────────────────────

def test_a_protected_path_is_refused_and_survives(executor):
    """The whole point: user data is never deleted, whatever the caller says."""
    # Make the file real so "refused" cannot be confused with "not found".
    DOCUMENTED = DOCUMENTS
    try:
        DOCUMENTED.parent.mkdir(parents=True, exist_ok=True)
        DOCUMENTED.write_text("irreplaceable")
    except OSError:  # pragma: no cover - read-only home in some CI images
        pytest.skip("cannot create a file under ~/Documents here")

    try:
        result = executor.delete(str(DOCUMENTED), dry_run=False)
        assert result.action == "blocked"
        assert not result.success
        assert "PROTECTED" in (result.error or "")
        assert DOCUMENTED.exists(), "protected file must still be on disk"
        assert DOCUMENTED.read_text() == "irreplaceable"
        assert _entries(executor.audit)[-1]["action"] == "BLOCKED"
    finally:
        DOCUMENTED.unlink(missing_ok=True)


def test_a_blocked_delete_frees_nothing(executor, sandbox):
    result = executor.delete(str(sandbox / "nope.txt"), dry_run=False)
    assert result.action == "not_found"
    assert result.size_freed == 0
    assert not result.success


def test_the_empty_path_is_blocked(executor):
    result = executor.delete("", dry_run=False)
    assert result.action in {"blocked", "not_found"}
    assert not result.success


# ── Gate 2: dry run and safe mode ─────────────────────────────────────────────

def test_dry_run_is_the_default_and_touches_nothing(executor, sandbox):
    target = _file(sandbox / "app-cache.bin", size=2048)
    result = executor.delete(str(target))
    assert result.action == "dry_run"
    assert result.dry_run is True
    assert result.success
    assert result.size_freed == 2048
    assert target.exists(), "a dry run must not delete anything"
    assert _entries(executor.audit)[-1]["action"] == "DRY_RUN"


def test_safe_mode_overrides_an_explicit_dry_run_false(audit, sandbox):
    """safe_mode is the user's "just show me, never touch" switch."""
    executor = Executor(safe_mode=True, audit=audit)
    target = _file(sandbox / "thing.bin")
    result = executor.delete(str(target), dry_run=False)
    assert result.action == "safe_mode"
    assert target.exists()
    assert _entries(executor.audit)[-1]["action"] == "SAFE_MODE"


def test_dry_run_reports_the_size_it_would_free(executor, sandbox):
    directory = sandbox / "build"
    _file(directory / "a.bin", 1024)
    _file(directory / "nested" / "b.bin", 2048)
    result = executor.delete(str(directory))
    assert result.size_freed == 3072
    assert result.size_freed_human == "3.0 KB"
    assert directory.exists()


# ── Gate 3: the audit log records everything ──────────────────────────────────

def test_every_outcome_is_written_to_the_audit_log(executor, sandbox, tmp_path):
    target = _file(sandbox / "logged.bin")
    outside = tmp_path / "not-in-the-sandbox"  # trusted by nothing
    executor.delete(str(target))                 # dry run
    executor.delete(str(target), dry_run=False)  # deleted
    executor.delete(str(outside), dry_run=False)  # blocked before existence
    actions = [e["action"] for e in _entries(executor.audit)]
    assert actions == ["DRY_RUN", "DELETED", "BLOCKED"]


def test_audit_entries_carry_the_fields_a_user_would_need(executor, sandbox):
    target = _file(sandbox / "record.bin", 512)
    executor.delete(str(target), dry_run=False)
    entry = _entries(executor.audit)[-1]
    for key in ("timestamp", "action", "path", "size_bytes", "note"):
        assert key in entry, f"audit entry missing {key}"
    assert entry["size_bytes"] == 512
    assert entry["path"] == str(target)


def test_an_unwritable_audit_log_does_not_stop_a_deletion(sandbox, tmp_path):
    """Losing the log must not become losing the user's approved action."""
    audit = AuditLogger(tmp_path / "no-such-dir" / "audit.jsonl")
    executor = Executor(safe_mode=False, audit=audit)
    target = _file(sandbox / "still-deleted.bin")
    assert executor.delete(str(target), dry_run=False).action == "deleted"
    assert not target.exists()


def test_session_summary_counts_only_real_deletions(executor, sandbox):
    keep = _file(sandbox / "kept.bin")
    gone = _file(sandbox / "gone.bin")
    executor.delete(str(keep))                 # dry run
    executor.delete(str(gone), dry_run=False)  # deleted
    summary = executor.audit.session_summary()
    assert summary["total_dry_run"] == 1
    assert summary["total_deleted"] == 1
    assert summary["bytes_freed"] == 100
    assert keep.exists()


# ── Real deletion ─────────────────────────────────────────────────────────────

def test_deleting_a_file_removes_it(executor, sandbox):
    target = _file(sandbox / "cache.dat", 42)
    result = executor.delete(str(target), dry_run=False)
    assert result.action == "deleted"
    assert result.size_freed == 42
    assert not target.exists()


def test_deleting_a_directory_removes_the_whole_tree(executor, sandbox):
    root = sandbox / "DerivedData"
    _file(root / "one.bin")
    _file(root / "deep" / "two.bin")
    _file(root / "deep" / "deeper" / "three.bin")
    result = executor.delete(str(root), dry_run=False)
    assert result.action == "deleted"
    assert result.size_freed == 300
    assert not root.exists()


def test_contents_only_directories_are_emptied_not_removed(executor, sandbox):
    """macOS expects ~/Library/Caches to exist; emptying it is the contract."""
    caches = sandbox / "Caches"
    _file(caches / "a.tmp")
    _file(caches / "inner" / "b.tmp")
    result = executor.delete(str(caches), dry_run=False)
    assert result.action == "deleted"
    assert caches.is_dir(), "the directory itself must survive"
    assert list(caches.iterdir()) == []


def test_a_symlink_is_unlinked_without_touching_its_target(executor, sandbox):
    precious = sandbox / "precious"
    _file(precious / "keep.txt", 10)
    link = sandbox / "cache-link"
    os.symlink(precious, link)
    result = executor.delete(str(link), dry_run=False)
    assert result.action == "deleted"
    assert not link.exists() or link.is_symlink() is False
    assert not os.path.lexists(str(link)), "the link itself must be gone"
    assert precious.exists(), "the symlink target must survive"
    assert (precious / "keep.txt").exists()


def test_permission_errors_are_reported_not_raised(executor, sandbox):
    if hasattr(os, "geteuid") and os.geteuid() == 0:  # pragma: no cover
        pytest.skip("root ignores permission bits")
    locked = sandbox / "locked"
    _file(locked / "file.bin")
    locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        result = executor.delete(str(locked), dry_run=False)
        assert result.action == "error"
        assert not result.success
        assert (locked / "file.bin").exists()
        assert _entries(executor.audit)[-1]["action"] == "ERROR"
    finally:
        locked.chmod(stat.S_IRWXU)


# ── Batches ───────────────────────────────────────────────────────────────────

def test_delete_many_reports_one_result_per_path(executor, sandbox):
    paths = [str(_file(sandbox / f"f{i}.bin")) for i in range(3)]
    results = executor.delete_many(paths, dry_run=False)
    assert len(results) == 3
    assert all(r.action == "deleted" for r in results)


def test_delete_many_continues_past_a_failure(executor, sandbox):
    """One bad path must not abandon the rest of the user's plan."""
    good = _file(sandbox / "good.bin")
    bad = sandbox / "missing.bin"
    other = _file(sandbox / "other.bin")
    results = executor.delete_many([str(bad), str(good), str(other)], dry_run=False)
    assert [r.action for r in results] == ["not_found", "deleted", "deleted"]
    assert not good.exists() and not other.exists()


def test_delete_many_defaults_to_dry_run(executor, sandbox):
    target = _file(sandbox / "survivor.bin")
    executor.delete_many([str(target)])
    assert target.exists()


# ── Result objects ────────────────────────────────────────────────────────────

def test_result_dict_shape():
    result = DeleteResult(path="/tmp/x", action="deleted", size_freed=2048)
    assert result.to_dict() == {
        "path": "/tmp/x",
        "action": "deleted",
        "size_freed": 2048,
        "size_freed_human": "2.0 KB",
        "error": None,
        "dry_run": False,
    }


@pytest.mark.parametrize(
    "action,success",
    [
        ("deleted", True),
        ("dry_run", True),
        ("safe_mode", True),
        ("blocked", False),
        ("not_found", False),
        ("error", False),
    ],
)
def test_only_real_outcomes_count_as_success(action, success):
    assert DeleteResult(path="/tmp/x", action=action).success is success


def test_zero_is_a_legitimate_size():
    result = DeleteResult(path="/tmp/empty", action="deleted", size_freed=0)
    assert result.size_freed_human == "0.0 B"
    assert result.success
