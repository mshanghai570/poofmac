"""
Disk usage reporting.

Regression cover for a bug that shipped: get_disk_usage() ran ``df -k /``,
which on APFS reports only the sealed system volume. On a Mac whose Data
volume was 91% full, PoofMac's GUI header, both TUIs and the agent's
get_disk_overview tool all reported 9% full and green. The fix reads statfs
on /System/Volumes/Data, and these tests keep it fixed.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from mac_cleaner import scanner

GB = 1024**3
REQUIRED_KEYS = {
    "total",
    "used",
    "free",
    "total_human",
    "used_human",
    "free_human",
    "used_percent",
}


def _fake_disk_usage(table):
    """Build a shutil.disk_usage stand-in keyed by path."""
    calls: list[str] = []

    def fake(path):
        calls.append(str(path))
        if str(path) not in table:
            raise FileNotFoundError(path)
        return table[str(path)]

    fake.calls = calls
    return fake


# ── The APFS regression ───────────────────────────────────────────────────────

def test_reports_the_data_volume_not_the_system_slice(monkeypatch):
    """APFS shares one container; statfs on / is unreliable, Data is not."""
    monkeypatch.setattr(
        scanner.shutil,
        "disk_usage",
        _fake_disk_usage(
            {
                "/System/Volumes/Data": (100 * GB, 90 * GB, 10 * GB),
                "/": (100 * GB, 9 * GB, 91 * GB),  # the misleading system slice
            }
        ),
    )
    usage = scanner.get_disk_usage()
    assert usage["measured"] == "statfs"
    assert usage["used_percent"] == 90.0
    assert usage["used"] == 90 * GB
    assert usage["used_human"] != usage["free_human"]


def test_prefers_the_data_volume_before_falling_back_to_root(monkeypatch):
    fake = _fake_disk_usage(
        {
            "/System/Volumes/Data": (100 * GB, 42 * GB, 58 * GB),
            "/": (100 * GB, 1 * GB, 99 * GB),
        }
    )
    monkeypatch.setattr(scanner.shutil, "disk_usage", fake)
    usage = scanner.get_disk_usage()
    assert usage["used_percent"] == 42.0
    assert fake.calls[0] == "/System/Volumes/Data"
    assert "/" not in fake.calls  # never needed, and wrong when it is


def test_falls_back_to_root_when_there_is_no_data_volume(monkeypatch):
    """Linux CI has no /System/Volumes/Data; the function must still work."""
    fake = _fake_disk_usage({"/": (200 * GB, 50 * GB, 150 * GB)})
    monkeypatch.setattr(scanner.shutil, "disk_usage", fake)
    usage = scanner.get_disk_usage()
    assert usage["used_percent"] == 25.0
    assert usage["measured"] == "statfs"


def test_df_fallback_when_statfs_is_unavailable(monkeypatch):
    monkeypatch.setattr(
        scanner.shutil, "disk_usage", _fake_disk_usage({})  # every path raises
    )

    class _Proc:
        stdout = (
            "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
            "/dev/disk1s1 104857600 52428800 41943040 60% /System/Volumes/Data\n"
        )

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())
    usage = scanner.get_disk_usage()
    assert usage["measured"] == "df"
    assert usage["used_percent"] == 50.0


def test_unavailable_rather_than_a_wrong_number(monkeypatch):
    """Everything failing must say so, not invent 0% or crash the GUI."""
    monkeypatch.setattr(
        scanner.shutil, "disk_usage", _fake_disk_usage({})
    )

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("df", 10)

    monkeypatch.setattr(subprocess, "run", boom)
    usage = scanner.get_disk_usage()
    assert usage["measured"] == "unavailable"
    assert usage["used_percent"] == 0


def test_never_raises(monkeypatch):
    monkeypatch.setattr(
        scanner.shutil,
        "disk_usage",
        lambda p: (_ for _ in ()).throw(PermissionError("nope")),
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess("", 1, "", ""))
    usage = scanner.get_disk_usage()
    assert isinstance(usage, dict)


# ── Contract every caller depends on ──────────────────────────────────────────

def test_keys_and_types_the_gui_and_tui_rely_on():
    usage = scanner.get_disk_usage()
    assert REQUIRED_KEYS <= set(usage)
    assert isinstance(usage["used_percent"], float)
    assert 0 <= usage["used_percent"] <= 100
    assert usage["total"] > 0
    for key in ("total_human", "used_human", "free_human"):
        assert isinstance(usage[key], str) and usage[key]


def test_percent_agrees_with_the_real_volume_on_this_machine():
    """No mocking: compare against what df says about the Data volume."""
    usage = scanner.get_disk_usage()
    out = subprocess.run(
        ["df", "-k", "/System/Volumes/Data"], capture_output=True, text=True
    ).stdout.splitlines()
    if len(out) < 2:
        pytest.skip("df unavailable")
    parts = out[1].split()
    used_kb, avail_kb = int(parts[2]), int(parts[3])
    truth = used_kb / (used_kb + avail_kb) * 100
    assert abs(usage["used_percent"] - truth) <= 5, (
        f"statfs says {usage['used_percent']}%, df says {truth:.1f}%"
    )


# ── format_size ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "size,expected",
    [
        (0, "0.0 B"),
        (512, "512.0 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (1024**2, "1.0 MB"),
        (1024**3, "1.0 GB"),
        (1024**4, "1.0 TB"),
    ],
)
def test_format_size(size, expected):
    assert scanner.format_size(size) == expected
