"""
Shared pytest fixtures.

PoofMac keeps its state in a per-user config directory (settings, the
maintenance log, the disclaimer marker) and its schedule in the user's real
LaunchAgents folder. Tests must never touch either, so every test that cares
gets an isolated directory and a plist path inside tmp_path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Point every config-dir reader at a throwaway directory.

    The endpoint store caches its parsed contents in a module global, so the
    cache is dropped on both sides of the test — otherwise one test's saved
    endpoints would leak into the next one's config directory.
    """
    from mac_cleaner import store

    config = tmp_path / "config"
    config.mkdir()
    monkeypatch.setenv("POOFMAC_CONFIG_DIR", str(config))
    store.invalidate()
    yield config
    store.invalidate()


class _FakeLaunchAgents:
    """Stand-in for the scheduler's plist path, plus the launchctl calls made.

    Behaves like the Path it wraps (the scheduler only ever exists(), writes
    and unlinks it) and records every launchctl invocation for assertions.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self.launchctl_calls: list[list[str]] = []

    def __getattr__(self, name):
        return getattr(self._path, name)

    def __fspath__(self) -> str:
        return str(self._path)

    def __str__(self) -> str:
        return str(self._path)


@pytest.fixture
def fake_plist(tmp_path, monkeypatch):
    """Redirect the scheduler's LaunchAgent plist into tmp_path.

    Also pretends to be macOS and stubs launchctl, so the schedule can be
    tested on Linux CI without either installing a real LaunchAgent or
    shelling out to a binary that isn't there.
    """
    from mac_cleaner import scheduler

    path = tmp_path / "LaunchAgents" / f"{scheduler.LABEL}.plist"
    path.parent.mkdir(parents=True, exist_ok=True)
    fake = _FakeLaunchAgents(path)
    monkeypatch.setattr(scheduler, "_PLIST_PATH", fake)
    monkeypatch.setattr(scheduler.sys, "platform", "darwin")

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, *args, **kwargs):
        fake.launchctl_calls.append(list(cmd))
        return _Proc()

    monkeypatch.setattr(scheduler.subprocess, "run", fake_run)
    return fake
