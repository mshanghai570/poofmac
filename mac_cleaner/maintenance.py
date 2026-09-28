# SPDX-License-Identifier: MIT
# Copyright (c) 2026 lesteroliver — https://poofmac.app
"""
Maintenance & optimization tools — the "extras" beyond disk cleanup.

Everything here runs WITHOUT sudo: it either only reads system state (ps,
vm_stat, tmutil list) or performs an action macOS allows a normal user to do
(thinning Time Machine local snapshots, toggling a Launch Agent in the user's
own ~/Library/LaunchAgents). Tools that genuinely need admin rights (flushing
the DNS cache, reindexing Spotlight, repairing disk permissions) are reported
as ready-to-copy commands instead — the app never elevates on its own.

Read-only reporting
    heavy_consumers     top processes by CPU and by RAM
    hung_applications   processes not responding to WindowServer pings
    launch_agents       user + system launch agents, disabled ones included
    memory_report       RAM pressure, swap, purgeable memory
    tm_snapshots        local Time Machine snapshots and their disk usage

User-approved actions (no sudo required)
    thin_tm_snapshots   ask Time Machine to reclaim snapshot space
    toggle_launch_agent disable / enable a user LaunchAgent by plist name

Guidance (needs Terminal; the LLM explains and the user runs it)
    flush DNS cache, reindex Spotlight, speed up boot (login items), etc.
    → MAINTENANCE_GUIDES, served through get_maintenance_guide
"""

from __future__ import annotations

import plistlib
import re
import subprocess
from pathlib import Path
from typing import Optional


def _run(cmd: list[str], timeout: float = 10.0) -> tuple[bool, str]:
    """(ok, stdout) for a read-only helper command; never raises."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return False, f"{exc.__class__.__name__}: {exc}"
    return proc.returncode == 0, proc.stdout


def _human(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:,.0f} {unit}" if unit in ("B", "KB") else f"{value:,.1f} {unit}"
        value /= 1024
    return f"{value:,.1f} TB"


# ── Reports ───────────────────────────────────────────────────────────────────

def heavy_consumers(limit: int = 10) -> dict:
    """Top processes by CPU and by resident memory (ps, read-only)."""
    ok, out = _run(["ps", "-Ao", "pcpu,pmem,pid,comm", "-r"])
    rows: list[dict] = []
    if ok:
        for line in out.splitlines()[1:]:
            parts = line.strip().split(None, 3)
            if len(parts) == 4:
                try:
                    rows.append(
                        {
                            "cpu_percent": float(parts[0]),
                            "mem_percent": float(parts[1]),
                            "pid": int(parts[2]),
                            "command": Path(parts[3]).name,
                        }
                    )
                except ValueError:
                    continue
    by_cpu = sorted(rows, key=lambda r: r["cpu_percent"], reverse=True)[:limit]
    ok_mem, out_mem = _run(["ps", "-Ao", "rss,pid,comm"])
    mem_rows: list[dict] = []
    if ok_mem:
        for line in out_mem.splitlines()[1:]:
            parts = line.strip().split(None, 2)
            if len(parts) == 3:
                try:
                    mem_rows.append(
                        {
                            "rss_bytes": int(parts[0]) * 1024,
                            "pid": int(parts[1]),
                            "command": Path(parts[2]).name,
                        }
                    )
                except ValueError:
                    continue
    by_ram = sorted(mem_rows, key=lambda r: r["rss_bytes"], reverse=True)[:limit]
    for row in by_ram:
        row["rss_human"] = _human(row.pop("rss_bytes"))
    return {
        "top_by_cpu": by_cpu,
        "top_by_ram": by_ram,
        "note": "Percentages are instantaneous. WindowServer using CPU during UI work is normal.",
    }


def hung_applications() -> dict:
    """Apps whose process is not responding (same check Activity Monitor uses)."""
    ok, out = _run(["ps", "-Ao", "pid,stat,comm"])
    hung: list[dict] = []
    if ok:
        for line in out.splitlines()[1:]:
            parts = line.strip().split(None, 2)
            if len(parts) == 3:
                pid, stat, comm = parts
                # "!" in the process state flags = not responding
                if "!" in stat:
                    hung.append(
                        {"pid": int(pid) if pid.isdigit() else pid, "state": stat, "command": comm}
                    )
    return {
        "hung": hung,
        "count": len(hung),
        "note": (
            "A hung app can usually be forced to quit from the Apple menu "
            "(Force Quit), or killed with: kill <pid>"
        ) if hung else "Nothing is hung right now.",
    }


_USER_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
_SYSTEM_AGENTS_DIR = Path("/Library/LaunchAgents")


def launch_agents() -> dict:
    """User and system LaunchAgents with enabled/disabled state."""
    entries: list[dict] = []
    for scope, directory in (("user", _USER_AGENTS_DIR), ("system", _SYSTEM_AGENTS_DIR)):
        if not directory.is_dir():
            continue
        for plist in sorted(directory.glob("*.plist*")):
            disabled_marker = plist.name.endswith(".disabled")
            name = plist.name.removesuffix(".disabled")
            label = name.removesuffix(".plist")
            info: dict = {"label": label, "scope": scope, "enabled": not disabled_marker}
            data: dict = {}
            try:
                data = plistlib.loads(plist.read_bytes())
            except Exception:  # noqa: BLE001 — real Macs have malformed plists
                info["note"] = "unreadable plist"
            if data:
                if data.get("Disabled") is True:
                    info["enabled"] = False
                    info["note"] = "Disabled=true inside the plist"
                info["runs_at_login"] = bool(data.get("RunAtLoad"))
            entries.append(info)
    return {
        "agents": entries,
        "count": len(entries),
        "note": (
            "Only user agents (scope=user) can be toggled from PoofMac. "
            "System ones in /Library/LaunchAgents need an admin password in Terminal."
        ),
    }


def memory_report() -> dict:
    """RAM pressure and swap from vm_stat / memory_pressure (read-only)."""
    ok, out = _run(["vm_stat"])
    pages: dict[str, int] = {}
    page_size = 4096
    if ok:
        match = re.search(r"page size of (\d+)", out)
        if match:
            page_size = int(match.group(1))
        for key in ("free", "active", "inactive", "speculative", "wired down", "compressed", "purgeable"):
            m = re.search(rf"Pages {key}:\s+(\d+)", out)
            if m:
                pages[key] = int(m.group(1)) * page_size
    ok_mp, out_mp = _run(["memory_pressure", "-Q"])
    free_percent: Optional[int] = None
    if ok_mp:
        m = re.search(r"free percentage:\s*(\d+)%", out_mp)
        if m:
            free_percent = int(m.group(1))
    swap_files = sorted(Path("/private/var/vm").glob("swapfile*")) if Path("/private/var/vm").is_dir() else []
    swap_total = sum(f.stat().st_size for f in swap_files if f.is_file())
    purgeable = pages.get("purgeable", 0)
    return {
        "free_percent": free_percent,
        "pages_bytes": {k: _human(v) for k, v in pages.items()},
        "purgeable_memory": _human(purgeable),
        "swap_files": len(swap_files),
        "swap_total": _human(swap_total),
        "note": (
            "Compressed + inactive memory is reclaimed automatically under pressure; "
            "'purgeable' is cache macOS can drop at any time. macOS has no user-space "
            "equivalent of Linux's drop-caches — 'Free RAM' apps just ask the system to "
            "purge, which it already does on demand."
        ),
    }


def tm_snapshots() -> dict:
    """Local Time Machine snapshots on the boot volume."""
    ok, out = _run(["tmutil", "listlocalsnapshots", "/"])
    snaps: list[str] = []
    if ok:
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("com.apple.TimeMachine."):
                snaps.append(line.split("com.apple.TimeMachine.")[-1].split(".local")[0])
    return {
        "snapshots": snaps,
        "count": len(snaps),
        "note": (
            "Each snapshot can hold GBs of 'purgeable' disk space. macOS keeps 24h of "
            "them by default and thins automatically when disk space runs low, but "
            "thinning manually frees space immediately."
        ) if snaps else "No local snapshots — nothing to reclaim.",
    }


def purgeable_space() -> dict:
    """Purgeable disk space (caches + snapshots macOS can drop on demand)."""
    import shutil
    usage = shutil.disk_usage("/")
    # APFS reports purgeable via diskutil; fall back gracefully.
    ok, out = _run(["diskutil", "info", "/"])
    purgeable = ""
    if ok:
        m = re.search(r"Purgeable Space:\s*([0-9.,]+ [A-Z]+)", out)
        if m:
            purgeable = m.group(1)
    return {
        "disk_free": _human(usage.free),
        "purgeable_space": purgeable or "unknown",
        "note": (
            "Purgeable space is already counted as free by macOS. It shrinks on its "
            "own when apps need room; thinning Time Machine snapshots clears most of it."
        ),
    }


# ── Actions (no sudo) ─────────────────────────────────────────────────────────

def thin_tm_snapshots(keep_hours: int = 24) -> dict:
    """Ask Time Machine to thin local snapshots older than keep_hours."""
    ok, out = _run(
        ["tmutil", "thinlocalsnapshots", "/", str(keep_hours * 3600 * 1_000_000_000), 1],
        timeout=60.0,
    )
    reclaimed: list[str] = []
    for line in out.splitlines():
        if "Deleted" in line:
            reclaimed.append(line.strip())
    return {
        "success": ok,
        "thinned": reclaimed,
        "raw": out.strip()[:500],
        "note": (
            "Time Machine decides what it can safely delete; urgency 1 is its "
            "gentlest request. Empty output usually means nothing met the threshold — "
            "try again with a smaller keep_hours."
        ),
    }


def toggle_launch_agent(label: str, enable: bool) -> dict:
    """Enable or disable a USER LaunchAgent by renaming its plist.

    Renaming to ``.plist.disabled`` is the same trick launchctl's disable flag
    approximates, is trivially reversible, and survives macOS updates better
    than editing the plist's Disabled key on a signed file.
    """
    if not label or "/" in label or ".." in label:
        return {"success": False, "error": "A LaunchAgent label (plist filename without .plist) is required."}
    plist = _USER_AGENTS_DIR / f"{label}.plist"
    if not plist.is_file():
        return {"success": False, "error": f"No user LaunchAgent named {label!r} in {_USER_AGENTS_DIR}."}
    target = plist.with_name(plist.name + ("" if enable else ".disabled"))
    try:
        if enable:
            current = _USER_AGENTS_DIR / f"{label}.plist.disabled"
            if not current.is_file():
                return {"success": False, "error": f"{label!r} is not disabled."}
            current.rename(plist)
        else:
            if not plist.is_file():
                return {"success": False, "error": f"{label!r} is already disabled."}
            plist.rename(target)
    except OSError as exc:
        return {"success": False, "error": f"Could not rename: {exc}"}
    return {
        "success": True,
        "label": label,
        "enabled": enable,
        "note": (
            "Takes effect at next login. To stop it right now also run: "
            f"launchctl bootout gui/$(id -u)/{label}"
        ),
    }


# ── Guides for things that genuinely need the admin password ──────────────────

MAINTENANCE_GUIDES: dict[str, dict] = {
    "flush_dns_cache": {
        "title": "Flush DNS cache",
        "needs_sudo": True,
        "commands": [
            "sudo dscacheutil -flushcache",
            "sudo killall -HUP mDNSResponder",
        ],
        "explanation": (
            "Clears the local DNS resolver cache — useful after changing DNS servers "
            "or when a site resolves to a stale address. Harmless: the cache rebuilds "
            "itself in seconds."
        ),
    },
    "reindex_spotlight": {
        "title": "Reindex Spotlight",
        "needs_sudo": True,
        "commands": [
            "sudo mdutil -E /",
            "sudo mdutil -i on /",
        ],
        "explanation": (
            "Erases and rebuilds the Spotlight index when search returns stale or "
            "missing results. The rebuild takes from minutes to an hour on a full disk."
        ),
    },
    "speed_up_boot": {
        "title": "Speed up boot",
        "needs_sudo": False,
        "commands": [
            "osascript -e 'tell application \"System Events\" to get the name of every login item'",
            "# Remove one:  osascript -e 'tell application \"System Events\" to delete login item \"NAME\"'",
        ],
        "explanation": (
            "Login items are the biggest boot-time lever a user controls. This lists "
            "them with AppleScript (no sudo) so the LLM can walk through removing the "
            "unneeded ones. System daemons in /Library/LaunchDaemons need admin rights."
        ),
    },
    "repair_disk_permissions": {
        "title": "Repair disk permissions",
        "needs_sudo": False,
        "commands": [],
        "explanation": (
            "Not needed on modern macOS: since El Capitan, System Integrity "
            "Protection verifies system permissions on every boot, so the old "
            "'Repair Disk Permissions' step is obsolete. If apps misbehave, "
            "reinstall the app or run First Aid in Disk Utility instead."
        ),
    },
    "speed_up_mail": {
        "title": "Speed up Mail",
        "needs_sudo": False,
        "commands": [
            "# Quit Mail first, then rebuild its database index:",
            "rm -rf ~/Library/Mail/V*/MailData/Envelope\\ Index*",
        ],
        "explanation": (
            "Mail re-creates its search index on next launch, which fixes slow "
            "search and missing messages. Mail itself and all messages are untouched — "
            "only the index is rebuilt (it can take a while on large mailboxes)."
        ),
    },
}


def get_maintenance_guide(name: str) -> dict:
    return MAINTENANCE_GUIDES.get(
        name,
        {
            "title": name,
            "needs_sudo": True,
            "commands": [],
            "explanation": f"No guide for {name!r}. Available: {', '.join(MAINTENANCE_GUIDES)}",
        },
    )
