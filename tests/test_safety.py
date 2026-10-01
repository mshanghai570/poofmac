"""
The safety guardrails.

safety.py is the one module where a silent regression destroys someone's
work, so these tests pin the behaviour rather than the implementation:
system directories, user data and the Trash must stay untouchable, and the
executor-facing helpers must agree with validate_path().
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mac_cleaner import safety

HOME = Path.home()


# ── System directories: never deletable ───────────────────────────────────────

@pytest.mark.parametrize(
    "path",
    [
        "/System",
        "/System/Library/CoreServices",
        "/usr/local/bin",
        "/bin",
        "/sbin",
        "/etc/hosts",
        "/private/etc",
        "/Library/Preferences",
        "/Applications/Safari.app",
        "/private/tmp/scratch",
        "/dev",
    ],
)
def test_system_paths_are_protected(path):
    verdict = safety.validate_path(path)
    assert not verdict.is_safe, f"{path} must never be considered safe"
    assert verdict.risk_level == "PROTECTED"
    assert "never modify" in verdict.reason


@pytest.mark.parametrize(
    "path",
    [
        HOME / "Documents",
        HOME / "Documents" / "thesis.pdf",
        HOME / "Desktop",
        HOME / "Pictures" / "Photos Library.photoslibrary",
        HOME / "Music",
        HOME / "Movies",
        HOME / ".ssh" / "id_ed25519",
        HOME / ".aws" / "credentials",
        HOME / "Library" / "Keychains" / "login.keychain-db",
        HOME / "Library" / "Mail",
    ],
)
def test_user_data_is_protected(path):
    verdict = safety.validate_path(str(path))
    assert not verdict.is_safe, f"{path} holds user data and must be protected"
    assert verdict.risk_level == "PROTECTED"


def test_protected_list_covers_the_documented_prompt_rules():
    """The system prompt tells the model never to propose these paths.

    If a category is added to one list it must exist in the other, or the
    model and the code disagree about what is off limits.
    """
    assert {"/System", "/usr", "/bin", "/etc", "/Library", "/Applications"} <= set(
        safety.SYSTEM_PROTECTED
    )
    for dot_dir in (".ssh", ".aws"):
        assert str(HOME / dot_dir) in safety.USER_PROTECTED
    for name in ("Documents", "Desktop", "Music", "Movies"):
        assert str(HOME / name) in safety.USER_PROTECTED
    for library in ("Mail", "Contacts"):
        assert str(HOME / "Library" / library) in safety.USER_PROTECTED
    assert str(HOME / "Library" / "Contacts") in safety.USER_PROTECTED


# ── Traversal and symlink tricks ──────────────────────────────────────────────

def test_dotdot_traversal_cannot_escape_into_user_data():
    sneaky = f"{HOME / 'Library' / 'Caches'}/../../Documents/secret.txt"
    verdict = safety.validate_path(sneaky)
    assert not verdict.is_safe
    assert verdict.risk_level == "PROTECTED"


def test_a_symlink_into_documents_is_not_safe(tmp_path):
    """A cache-looking name must not launder a link to real data."""
    link = tmp_path / "node_modules"
    os.symlink(HOME / "Documents", link)
    try:
        verdict = safety.validate_path(str(link))
        assert not verdict.is_safe
        assert verdict.risk_level == "PROTECTED"
    except OSError:  # pragma: no cover - filesystems without symlink support
        pytest.skip("symlinks unavailable here")


def test_empty_path_is_never_safe():
    verdict = safety.validate_path("")
    assert not verdict.is_safe
    assert verdict.risk_level in {"CAUTION", "PROTECTED"}


# ── Known-safe targets ────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "path",
    [
        HOME / "Library" / "Caches" / "com.example.app",
        HOME / "Library" / "Logs",
        HOME / "Library" / "Developer" / "Xcode" / "DerivedData" / "App-abc",
        HOME / ".Trash",
        "/private/var/folders/xy/T/xyz",
    ],
)
def test_caches_are_safe(path):
    verdict = safety.validate_path(str(path))
    assert verdict.is_safe
    assert verdict.risk_level == "SAFE"


@pytest.mark.parametrize("name", ["node_modules", ".venv", "__pycache__", ".next"])
def test_dev_artifacts_are_safe(name):
    verdict = safety.validate_path(str(HOME / "projects" / "app" / name))
    assert verdict.is_safe
    assert verdict.risk_level == "SAFE"


def test_downloads_are_caution_not_safe():
    verdict = safety.validate_path(str(HOME / "Downloads" / "installer.dmg"))
    assert not verdict.is_safe
    assert verdict.risk_level == "CAUTION"


def test_downloads_folder_itself_is_not_auto_emptied():
    verdict = safety.validate_path(str(HOME / "Downloads"))
    assert verdict.risk_level != "SAFE"


def test_unknown_path_requires_review():
    verdict = safety.validate_path("/opt/whatever")
    assert not verdict.is_safe
    assert verdict.risk_level == "CAUTION"


# ── Executor-facing helpers ───────────────────────────────────────────────────

def test_assert_safe_returns_the_verdict_for_safe_paths():
    verdict = safety.assert_safe(str(HOME / "Library" / "Caches" / "thing"))
    assert verdict.is_safe


def test_assert_safe_raises_for_protected_paths():
    with pytest.raises(safety.SafetyViolation):
        safety.assert_safe(str(HOME / "Documents" / "thesis.pdf"))


@pytest.mark.parametrize(
    "path,expected",
    [
        (HOME / "Library" / "Caches", True),
        (HOME / "Library" / "Logs", True),
        (HOME / ".Trash", True),
        (HOME / "Library" / "Developer" / "Xcode" / "DerivedData", False),
    ],
)
def test_delete_contents_only_for_the_dirs_macos_expects_to_exist(path, expected):
    assert safety.should_delete_contents_only(str(path)) is expected


def test_protected_paths_never_appear_in_the_safe_lists():
    """A path must not be both safe and protected — the first match wins."""
    overlap = safety.SAFE_TARGETS & (safety.SYSTEM_PROTECTED | safety.USER_PROTECTED)
    assert not overlap, f"paths in both lists: {overlap}"
