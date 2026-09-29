# SPDX-License-Identifier: MIT
# Copyright (c) 2026 lesteroliver — https://poofmac.app
"""
Scheduled maintenance — a user LaunchAgent that runs the safe set on a timer.

Installs ``~/Library/LaunchAgents/app.poofmac.maintenance.plist`` running
``poofmac --maintain`` (or the repo venv's python) every ``interval_hours``.
The safe set is exactly what the Maintenance tab already does by hand:

* ``app_acceleration()``  — clear clipboard, recent-items lists, restart
  Finder/Dock (macOS rebuilds all of it; no user data touched)
* ``thin_tm_snapshots()`` — ask Time Machine to thin old local snapshots
* ``find_large_files()``  — report only; the log records the top offenders

Nothing is deleted without the user asking for it elsewhere in the app —
the scheduled run never removes files.

Every run appends one JSON line to ``<config dir>/maintenance_log.jsonl``
so the Schedule tab and the agent can show history. A lock file prevents
overlapping runs (e.g. launchd fires while a run from boot is still going).
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
import sys
import time
from pathlib import Path

from mac_cleaner import store

LABEL = "app.poofmac.maintenance"
_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
_LOG_NAME = "maintenance_log.jsonl"
_LOCK_NAME = "maintenance.lock"
_MAX_LOG_LINES = 200  # keep the log bounded; a run per few hours fills slowly

# Interval presets shown in the Schedule tab (hours -> label).
INTERVAL_CHOICES = (
    (24, "Daily"),
    (24 * 7, "Weekly"),
    (24 * 14, "Every 2 weeks"),
)


# ── Locate the interpreter the agent should use ───────────────────────────────


def _agent_command() -> list[str]:
    """Command for the LaunchAgent.

    ``sys.executable`` is the right interpreter in every layout: the
    Briefcase bundle's python inside the .app, the venv python in a repo
    checkout. ``-m mac_cleaner`` works from both.
    """
    return [sys.executable, "-m", "mac_cleaner", "--maintain"]


# ── Install / status / remove ─────────────────────────────────────────────────


def _write_plist(interval_hours: int) -> None:
    plist = {
        "Label": LABEL,
        "ProgramArguments": _agent_command(),
        "StartInterval": int(interval_hours * 3600),
        "RunAtLoad": True,  # run once at login too — cheap, read-mostly set
        "StandardOutPath": str(_log_path().with_suffix(".out")),
        "StandardErrorPath": str(_log_path().with_suffix(".err")),
    }
    _PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PLIST_PATH.write_bytes(plistlib.dumps(plist))


def schedule_enable(interval_hours: int = 24 * 7) -> dict:
    """Install (or update) the maintenance LaunchAgent and load it."""
    interval_hours = int(interval_hours)
    if interval_hours < 1:
        return {"error": "interval_hours must be at least 1"}
    if sys.platform != "darwin":
        return {"error": "Scheduled maintenance needs macOS launchd."}
    try:
        _write_plist(interval_hours)
    except OSError as exc:
        return {"error": f"Could not write {_PLIST_PATH}: {exc}"}

    # Unload first so re-installing with a new interval takes effect.
    subprocess.run(["launchctl", "unload", str(_PLIST_PATH)],
                   capture_output=True, timeout=15)
    proc = subprocess.run(["launchctl", "load", str(_PLIST_PATH)],
                          capture_output=True, text=True, timeout=15)
    ok = proc.returncode == 0
    return {
        "success": ok,
        "label": LABEL,
        "plist": str(_PLIST_PATH),
        "interval_hours": interval_hours,
        "interval_label": _interval_label(interval_hours),
        "error": proc.stderr.strip()[:200] if not ok else "",
    }


def schedule_disable() -> dict:
    """Unload and delete the maintenance LaunchAgent."""
    if sys.platform != "darwin":
        return {"error": "Scheduled maintenance needs macOS launchd."}
    if not _PLIST_PATH.exists():
        return {"success": True, "note": "No schedule installed."}
    subprocess.run(["launchctl", "unload", str(_PLIST_PATH)],
                   capture_output=True, timeout=15)
    try:
        _PLIST_PATH.unlink()
    except OSError as exc:
        return {"success": False, "error": f"Could not delete plist: {exc}"}
    return {"success": True, "note": "Schedule removed."}


def schedule_status() -> dict:
    """Is the schedule installed, and what did recent runs do?"""
    installed = _PLIST_PATH.exists()
    interval_hours = 0
    if installed:
        try:
            data = plistlib.loads(_PLIST_PATH.read_bytes())
            interval_hours = int(data.get("StartInterval", 0)) // 3600
        except Exception:  # noqa: BLE001 — corrupt plist means broken schedule
            installed = False
    return {
        "installed": installed,
        "plist": str(_PLIST_PATH),
        "interval_hours": interval_hours,
        "interval_label": _interval_label(interval_hours) if interval_hours else "",
        "last_runs": recent_runs(5),
    }


def _interval_label(hours: int) -> str:
    for value, label in INTERVAL_CHOICES:
        if value == hours:
            return label
    return f"Every {hours}h"


# ── The actual scheduled run ──────────────────────────────────────────────────


def _log_path() -> Path:
    return store.config_dir() / _LOG_NAME


def _lock_path() -> Path:
    return store.config_dir() / _LOCK_NAME


def run_safe_set(vacuum_mail: bool = False) -> dict:
    """The maintenance set the timer runs. Never deletes user files.

    Runs with a lock so overlapping triggers (login + interval) queue out.
    """
    lock = _lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        if lock.exists() and time.time() - lock.stat().st_mtime < 3600:
            return {"skipped": True, "reason": "another run holds the lock"}
        lock.write_text(str(os.getpid()))
    except OSError:
        pass  # a broken lock must not stop maintenance

    try:
        from mac_cleaner import maintenance

        started = time.time()
        acceleration = maintenance.app_acceleration(vacuum_mail=False)
        snapshots = maintenance.thin_tm_snapshots(keep_hours=24)
        large = maintenance.find_large_files(min_size_mb=200, limit=10)

        report = {
            "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_seconds": round(time.time() - started, 1),
            "success": bool(
                acceleration.get("success") and snapshots.get("success")
            ),
            "acceleration_steps": [
                {"step": s.get("step"), "success": s.get("success")}
                for s in acceleration.get("steps", [])
            ],
            "snapshots_thinned": len(snapshots.get("thinned", [])),
            "large_files_top": [
                {"path": f["path"], "size_human": f["size_human"]}
                for f in large.get("files", [])[:5]
            ],
            "partial_scan": large.get("partial_scan", False),
        }
        _append_log(report)
        return report
    finally:
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass


def _append_log(report: dict) -> None:
    path = _log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            lines = path.read_text().splitlines()
        except FileNotFoundError:
            lines = []  # first-ever run
        lines = lines[-(_MAX_LOG_LINES - 1):]
        lines.append(json.dumps(report))
        path.write_text("\n".join(lines) + "\n")
    except OSError as exc:
        print(f"maintenance log write failed: {exc}", file=sys.stderr)  # must not fail the run


def recent_runs(count: int = 5) -> list[dict]:
    """The last ``count`` scheduled runs, newest first."""
    try:
        lines = _log_path().read_text().splitlines()
    except OSError:
        return []
    runs: list[dict] = []
    for line in reversed(lines):
        if len(runs) >= count:
            break
        try:
            runs.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a truncated tail line from a crashed write
    return runs
