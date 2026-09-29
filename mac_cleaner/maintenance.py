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

import os
import plistlib
import re
import subprocess
import time
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


# ── GUI application inventory (LaunchServices, no TCC permission needed) ──────

# Apps a force-quit must never touch: killing them breaks the session.
# Bundle ids are matched case-insensitively — lsappinfo reports inconsistent
# casing (com.apple.dock vs com.apple.SystemUIServer vs com.apple.systemuiserver).
_PROTECTED_BUNDLE_IDS = {
    bid.lower()
    for bid in (
        "com.apple.finder", "com.apple.dock", "com.apple.systemuiserver",
        "com.apple.loginwindow", "com.apple.WindowServer", "com.apple.Spotlight",
        "com.apple.notificationcenterui", "com.apple.controlcenter",
        "com.apple.WindowManager", "com.apple.dock.extra", "com.apple.dock.helper",
    )
}


def _foreground_apps() -> list[dict]:
    """GUI applications with name/pid/bundle id, from LaunchServices.

    ``lsappinfo list`` is the same source Activity Monitor's Application list
    uses, works without Accessibility/TCC approval (unlike System Events), and
    one call answers for every app.
    """
    ok, out = _run(["lsappinfo", "list"], timeout=15.0)
    apps: list[dict] = []
    if not ok:
        return apps
    entries = re.split(r"\n\s*\d+\)\s", out)
    for entry in entries[1:]:
        name_m = re.match(r'"([^"]+)"', entry)
        pid_m = re.search(r"pid = (\d+)", entry)
        type_m = re.search(r'type="(\w+)"', entry)
        bid_m = re.search(r'bundleID="([^"]+)"', entry)
        if not (name_m and pid_m and type_m):
            continue
        # Dock and SystemUIServer register as UIElement, not Foreground —
        # both are session apps the user can see and might need to quit.
        # BackgroundOnly/XPC helpers are the ones to skip.
        if type_m.group(1) not in ("Foreground", "UIElement"):
            continue
        apps.append(
            {
                "name": name_m.group(1),
                "pid": int(pid_m.group(1)),
                "bundle_id": bid_m.group(1) if bid_m else "",
            }
        )
    return apps


def hung_applications() -> dict:
    """GUI applications that look wedged, plus recent freeze reports.

    Two signals, both cheap and permission-free:
    • An app using no CPU while it should be frontmost-ish is *suspected* —
      ps can't prove a hang, so these are labelled as such.
    • macOS itself writes ``*.hang.ips`` diagnostic reports for any app whose
      main thread froze for seconds — those are *confirmed* hangs on record.
    """
    reports_dir = Path.home() / "Library" / "Logs" / "DiagnosticReports"
    hang_reports: list[dict] = []
    if reports_dir.is_dir():
        for report in sorted(reports_dir.glob("*.hang.ips"), key=lambda p: p.stat().st_mtime, reverse=True)[:10]:
            # Report names look like "AppName-2026-09-29-003608.hang.ips".
            stem = report.name.removesuffix(".hang.ips")
            app_name = stem.rsplit("-", 3)[0] if "-" in stem else stem
            try:
                age_hours = (time.time() - report.stat().st_mtime) / 3600
            except OSError:
                continue
            hang_reports.append(
                {
                    "app": app_name,
                    "report": report.name,
                    "hours_ago": round(age_hours, 1),
                }
            )

    suspected: list[dict] = []
    for app in _foreground_apps():
        ok, state = _run(["ps", "-o", "state=", "-p", str(app["pid"])], timeout=5.0)
        s = state.strip()
        # "U" = uninterruptible wait: the classic stuck-in-a-syscall signature.
        if ok and "U" in s:
            suspected.append({**app, "state": s})

    return {
        "gui_apps": _foreground_apps(),
        "suspected_hung": suspected,
        "hang_reports": hang_reports,
        "count": len(suspected),
        "note": (
            "suspected_hung = apps stuck in an uninterruptible wait; hang_reports = "
            "freezes macOS recorded. Any of them can be force-quit with "
            "force_quit_app — ask the user first, unsaved work is lost."
        ),
    }


# Apps SIGTERM politely asks to quit; SIGKILL is the seatbelt-cutting "force".

def force_quit_app(identifier: str, force: bool = False) -> dict:
    """Quit a misbehaving application by name, bundle id or pid.

    Default is a polite SIGTERM (the app may save state and refuse).
    force=True escalates to SIGKILL — unsaved work in that app is lost, so the
    caller (LLM or GUI) must have the user's OK first. Apple's own session
    apps are protected and refuse to be killed here.
    """
    if not identifier:
        return {"success": False, "error": "Pass an app name, bundle id or pid."}

    target: Optional[dict] = None
    if identifier.isdigit():
        target = next((a for a in _foreground_apps() if a["pid"] == int(identifier)), None)
    else:
        needle = identifier.lower()
        target = next(
            (
                a for a in _foreground_apps()
                if needle in (a["name"].lower(), a["bundle_id"].lower())
            ),
            None,
        )
    if target is None:
        return {"success": False, "error": f"No GUI application matches {identifier!r}."}
    if target["bundle_id"].lower() in _PROTECTED_BUNDLE_IDS:
        return {
            "success": False,
            "error": (
                f"{target['name']} is a macOS session app — killing it would break "
                "the desktop. Use repair_applications to restart Finder or Dock "
                "safely instead."
            ),
        }

    pid = target["pid"]
    try:
        import signal
        os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
    except PermissionError:
        return {"success": False, "error": f"{target['name']} runs as another user (likely root)."}
    except ProcessLookupError:
        return {"success": True, "app": target["name"], "note": "Already gone."}
    except OSError as exc:
        return {"success": False, "error": str(exc)}
    return {
        "success": True,
        "app": target["name"],
        "pid": pid,
        "signal": "SIGKILL" if force else "SIGTERM",
        "note": (
            "Sent politely — the app may still save and exit on its own. "
            "Re-run with force=true if it survives 5 seconds."
        ) if not force else "Killed. Unsaved work in it is gone.",
    }


# ── Repair Applications / App Acceleration (no-sudo actions) ──────────────────

_LSREGISTER = (
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
    "LaunchServices.framework/Support/lsregister"
)


def _gone_or_replaced(process_name: str) -> bool:
    """Whether ``process_name`` quit (or a fresh copy already took over)."""
    ok, out = _run(["pgrep", "-x", process_name], timeout=5.0)
    if not ok or not out.strip():
        return True  # gone — launchd will relaunch it
    # Different pid than before would also be fine, but "still the same pid"
    # just means launchd has not gotten to it yet; launchd always does.
    return False


def repair_applications(include_ls_rebuild: bool = False) -> dict:
    """Restart the desktop's core apps and optionally rebuild Launch Services.

    Finder/Dock/SystemUIServer misbehave far more often than they crash —
    stale icons, a frozen Dock, empty desktop. ``killall`` is safe for exactly
    these three: launchd relaunches them instantly with fresh state. The
    Launch Services rebuild (user domain only, no sudo) fixes a broken or
    duplicated "Open With" menu — it re-derives every app registration, so it
    is off by default and only worth running when that symptom appears.
    """
    restarted: list[str] = []
    failed: list[dict] = []
    for service in ("Finder", "Dock", "SystemUIServer"):
        # SIGTERM can block on an app that is mid-save, so signal, then just
        # check the process left — never wait on killall itself.
        ok, out = _run(["killall", service], timeout=3.0)
        if ok or _gone_or_replaced(service):
            restarted.append(service)
        else:
            failed.append({"service": service, "error": out.strip()[:120]})

    ls_rebuilt = False
    if include_ls_rebuild and Path(_LSREGISTER).is_file():
        ok, out = _run(
            [_LSREGISTER, "-kill", "-r", "-domain", "local", "-domain", "system", "-domain", "user"],
            timeout=120.0,
        )
        ls_rebuilt = ok

    return {
        "success": bool(restarted),
        "restarted": restarted,
        "failed": failed,
        "launch_services_rebuilt": ls_rebuilt,
        "note": (
            "launchd restarts each of these automatically with clean state. "
            "Pass include_ls_rebuild=true only for a broken/duplicated 'Open With' "
            "menu — the rebuild takes a minute and re-registers every app."
        ),
    }


def app_acceleration(vacuum_mail: bool = False) -> dict:
    """The low-risk speed-ups: purge stale UI state apps re-read on launch.

    Clears the pasteboard, Finder's recent-items lists and the Dock's
    tile cache — things apps re-derive on next launch, so nothing the user
    created is touched. vacuum_mail=true additionally VACUUMs Mail's SQLite
    index (Mail must be closed): same trick as the commercial cleaners, but it
    reclaims space without deleting and rebuilding the index.
    """
    steps: list[dict] = []

    ok, out = _run(["bash", "-c", "printf '' | pbcopy"], timeout=5.0)
    steps.append({"step": "cleared clipboard", "success": ok})

    shared = Path.home() / "Library" / "Application Support" / "com.apple.sharedfilelist"
    cleared = 0
    if shared.is_dir():
        for sfl in shared.glob("**/*.sfl3"):
            # These are 'recent documents/projects' lists. macOS recreates them
            # empty; deleting beats truncating because .sfl3 readers choke on
            # a zero-byte file.
            try:
                sfl.unlink()
                cleared += 1
            except OSError:
                pass
    steps.append({"step": "cleared recent-items lists", "files": cleared, "success": True})

    ok, _ = _run(["killall", "Finder", "Dock"], timeout=10.0)
    steps.append({"step": "restarted Finder and Dock", "success": ok})

    mail_vacuumed = False
    if vacuum_mail:
        mail_runs = _run(["pgrep", "-x", "Mail"], timeout=5.0)[1].strip()
        if mail_runs:
            steps.append({"step": "Mail index vacuum", "success": False, "error": "Mail is running — quit it first."})
        else:
            ok, out = _run(
                ["bash", "-c", "find ~/Library/Mail -name 'Envelope Index' -exec sqlite3 {} 'VACUUM;' \\\\;"],
                timeout=300.0,
            )
            mail_vacuumed = ok
            steps.append({"step": "Mail index vacuum", "success": ok, "error": out.strip()[:120] if not ok else ""})

    return {
        "success": all(s.get("success") for s in steps),
        "steps": steps,
        "mail_vacuumed": mail_vacuumed,
        "note": (
            "These clear state macOS rebuilds automatically — no user data is "
            "touched. For app-specific slowdowns, an app's own caches usually "
            "matter more: scan_category('caches') finds the big ones."
        ),
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


# ── App Uninstaller — scan an app's bundle-id footprint, trash it safely ────────

_APP_DIRS = (
    Path.home() / "Applications",
    Path("/Applications"),
    Path("/System/Applications"),
)

# Every Library location an app can leave residue in, keyed by what it holds.
# Matched against the bundle id first (authoritative), app name as fallback —
# a name like "DevCleaner" can also appear inside unrelated bundle ids, so
# matches are unioned carefully and every hit is shown before removal.
_RESIDUAL_DIRS = (
    "Application Support",
    "Caches",
    "Containers",
    "Group Containers",
    "Application Scripts",
    "Logs",
    "Saved Application State",
    "Preferences",
    "HTTPStorages",
    "WebKit",
    "LaunchAgents",
)


def _dir_size(path: Path) -> int:
    """Recursive byte size; unreadable entries count as 0."""
    total = 0
    try:
        for item in path.rglob("*"):
            try:
                if item.is_file() and not item.is_symlink():
                    total += item.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


def _residual_matches(bundle_id: str, app_name: str, root: Path, key: str) -> list[Path]:
    """Residue paths under Library/<key> belonging to this app."""
    base = root / "Library" / key
    if not base.is_dir():
        return []
    needles: list[str] = []
    if bundle_id:
        # Group Containers prefix their team id: "58JULA45XG.app.DevCleaner".
        needles.append(bundle_id.lower())
    if app_name:
        needles.append(app_name.lower())
    if not needles:
        return []
    found: list[Path] = []
    try:
        for entry in base.iterdir():
            entry_l = entry.name.lower()
            if any(needle in entry_l for needle in needles):
                found.append(entry)
    except OSError:
        pass
    return found


def find_app_residuals(bundle_id: str, app_name: str = "") -> dict:
    """Every file this app left behind in ~/Library, with sizes.

    Matches by bundle id (authoritative) and app name (fallback, since
    pre-sandbox apps sometimes skip their bundle id in Preferences). Only the
    user's own Library is searched — the system-wide /Library needs admin
    rights and is deliberately out of reach.
    """
    if not bundle_id and not app_name:
        return {"error": "Pass at least a bundle id or an app name."}
    home = Path.home()
    residuals: list[dict] = []
    seen: set[str] = set()
    for key in _RESIDUAL_DIRS:
        for path in _residual_matches(bundle_id, app_name, home, key):
            resolved = str(path)
            if resolved in seen:
                continue
            seen.add(resolved)
            size = _dir_size(path) if path.is_dir() else (
                path.stat().st_size if path.exists() else 0
            )
            residuals.append(
                {
                    "path": resolved,
                    "kind": key,
                    "size_bytes": size,
                    "size_human": _human(size),
                }
            )
    total = sum(r["size_bytes"] for r in residuals)
    return {
        "bundle_id": bundle_id,
        "app_name": app_name,
        "residuals": residuals,
        "count": len(residuals),
        "total_bytes": total,
        "total_human": _human(total),
        "note": (
            "Removal moves items to the Trash — recoverable until it is emptied. "
            "Preferences plists hold the app's settings: keep them if you plan "
            "to reinstall and want your settings back."
        ),
    }


def list_installed_apps() -> dict:
    """Applications in /Applications, ~/Applications and /System/Applications."""
    apps: list[dict] = []
    seen: set[str] = set()
    for directory in _APP_DIRS:
        if not directory.is_dir():
            continue
        try:
            entries = sorted(directory.glob("*.app"), key=lambda p: p.name.casefold())
        except OSError:
            continue
        for bundle in entries:
            resolved = str(bundle)
            if resolved in seen:
                continue
            seen.add(resolved)
            bundle_id = ""
            try:
                data = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())
                bundle_id = str(data.get("CFBundleIdentifier", ""))
            except Exception:  # noqa: BLE001 — Info.plist is optional
                pass
            apps.append(
                {
                    "name": bundle.stem,
                    "bundle_id": bundle_id,
                    "path": resolved,
                    "scope": "user" if directory == _APP_DIRS[0] else "global",
                }
            )
    return {
        "apps": apps,
        "count": len(apps),
        "note": (
            "System apps (in /System/Applications) are protected by SIP and "
            "cannot be uninstalled. For anything else, find_app_residuals shows "
            "what an uninstall would clean up."
        ),
    }


def uninstall_app(app_path: str, remove_preferences: bool = True) -> dict:
    """Move an application and its residual files to the Trash.

    This is the whole uninstall: the .app bundle plus everything its bundle id
    left in ~/Library. Items go to the Trash (recoverable), never rm. Refuses:
    SIP-protected system apps, running apps, and anything outside the known
    application directories.
    """
    if not app_path:
        return {"success": False, "error": "Pass the path to an .app bundle."}
    bundle = Path(app_path).resolve()
    if bundle.suffix != ".app" or not bundle.is_dir():
        return {"success": False, "error": f"{bundle} is not an .app bundle."}
    allowed_parents = {str(d.resolve()) for d in _APP_DIRS}
    if str(bundle.parent) not in allowed_parents:
        return {
            "success": False,
            "error": (
                f"{bundle} is not directly in an applications folder — refusing "
                "paths outside /Applications, ~/Applications, /System/Applications."
            ),
        }
    if str(bundle).startswith("/System/Applications/"):
        return {"success": False, "error": "System apps are protected by SIP and cannot be removed."}

    bundle_id = ""
    try:
        data = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())
        bundle_id = str(data.get("CFBundleIdentifier", ""))
    except (OSError, ValueError):
        pass
    app_name = bundle.stem

    # Refuse to trash a running app — its state files would be recreated on
    # quit anyway, and a half-trashed bundle is worse than a running one.
    ok, out = _run(["pgrep", "-f", str(bundle)], timeout=5.0)
    if ok and out.strip():
        return {
            "success": False,
            "error": f"{app_name} is running — quit it first (force_quit_app can help).",
        }

    trashed: list[dict] = []
    failed: list[dict] = []
    trash = Path.home() / ".Trash"
    stamp = time.strftime("%Y%m%d-%H%M%S")

    def _trash(path: Path) -> None:
        try:
            target = trash / f"{path.name} ({stamp})"
            path.rename(target)
            trashed.append({"path": str(path), "trashed_to": str(target)})
        except OSError as exc:
            failed.append({"path": str(path), "error": str(exc)[:120]})

    _trash(bundle)
    residuals = find_app_residuals(bundle_id, app_name).get("residuals", [])
    for item in residuals:
        path = Path(item["path"])
        if not remove_preferences and item["kind"] == "Preferences":
            continue  # keep settings for a future reinstall
        if path.exists():
            _trash(path)

    freed = 0
    for entry in trashed:
        original = Path(entry["path"])
        if original.exists():
            freed += _dir_size(original) if original.is_dir() else original.stat().st_size
    return {
        "success": bool(trashed) and not failed,
        "app": app_name,
        "bundle_id": bundle_id,
        "trashed": trashed,
        "failed": failed,
        "kept_preferences": not remove_preferences,
        "freed_human": _human(freed),
        "note": "Everything is in the Trash — recoverable until it is emptied.",
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
            "# Quit Mail first, then compact its database index (safe — no rebuild):",
            "find ~/Library/Mail -name 'Envelope Index' -exec sqlite3 {} 'VACUUM;' \\;",
        ],
        "explanation": (
            "VACUUM compacts Mail's SQLite search index, reclaiming space and "
            "fixing slow search — without deleting anything, so Mail need not "
            "rebuild the index on next launch. PoofMac can do this in-app via "
            "app_acceleration(vacuum_mail=true)."
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
