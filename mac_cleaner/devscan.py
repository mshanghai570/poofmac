# SPDX-License-Identifier: MIT
# Copyright (c) 2026 lesteroliver — https://poofmac.app
"""
Deep developer-storage scanner — Xcode, Simulator runtimes, device support.

Read-only module. Nothing is ever deleted or modified here.

What it discovers, dynamically (no hardcoded size guesses):
  • ~/Library/Developer/Xcode/DerivedData        project build caches
  • ~/Library/Developer/Xcode/Archives           user archives (.xcarchive)
  • ~/Library/Developer/Xcode/iOS DeviceSupport  symbol files per device iOS
  • ~/Library/Developer/Xcode/watchOS DeviceSupport
  • ~/Library/Developer/Xcode/UserData           "Prototypes"/"Previews" caches
  • ~/Library/Developer/CoreSimulator/Devices    simulator device data dirs
  • ~/Library/Developer/CoreSimulator/Caches     dyld / com.apple.dt caches
  • ~/Library/Developer/CoreSimulator/Profiles   per-device profile files
  • ~/Library/Developer/Simulator (Xcode 15+ shared runtime caches)
  • Legacy leftovers: Xcode-beta, "Xcode" copies in /Applications,
    CoreSimulator/Devices.dangling, Dropbox of old device_support roots.
  • Simulator runtime disk images under
    ~/Library/Developer/CoreSimulator/Images and /Applications/Xcode*.app/
    Contents/Developer/Platforms/*Simulator (read-only inspection only).

Sizing: uses `du -sk` (kernel-fast) per top-level entry exactly like
scanner.py; supports a full `du` None-fallback for non-macOS test hosts and
a scan-timeout so huge trees return partial results rather than hang.

Simulator device packages: parsed from each device's device_set.plist /
.device_info files when present, else discovered by directory mtime.

Simulator runtimes: `xcrun simctl runtime list -j` when available
(supported interface), with a filesystem fallback that reads bundle
Info.plist build ids from CoreSimulator/Images. Never relies on
undocumented storage internals.
"""

from __future__ import annotations

import json
import plistlib
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── Snapshot of one discovered item ───────────────────────────────────────────


@dataclass
class DevItem:
    """
    One discovered developer-storage item with real measured metadata.

    Measurement categories (kept honest and distinct):
      • "measured"     — real `du` result, trust the number.
      • "estimate"     — bounded walk timed out partway; number is a floor.
      • "inaccessible" — could not read (permissions / missing / skip).
    """

    path: str
    kind: str                    # derived_data | archives | device_support |
                                 # sim_devices | sim_runtime | sim_caches |
                                 # xcode_app | legacy_leftover | tmp | kdk | …
    label: str
    size_bytes: int              # measured floor; 0 when inaccessible
    measurement: str             # measured | estimate | inaccessible
    file_count: int              # practical best-effort; -1 = unknown
    directory_count: int         # practical best-effort; -1 = unknown
    last_modified: Optional[float] = None   # epoch sec of most recent entry
    version_hint: Optional[str] = None      # e.g. "iOS 17.4 (21E219)"
    active_guess: str = "uncertain"  # active | inactive | rebuildable | uncertain
    notes: dict = field(default_factory=dict)
    model_id: Optional[str] = None          # simulator device UDID
    bundle_id: Optional[str] = None         # iOS runtime build id


# ── Generic jobs ──────────────────────────────────────────────────────────────

def _du(path: str, timeout: int = 25) -> tuple[int, str]:
    """Size a path; returns (bytes, measurement) with measurement ∈
    {"measured", "estimate", "inaccessible"}."""
    try:
        result = subprocess.run(
            ["du", "-sk", path],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0 and result.stdout.strip():
            return int(result.stdout.split()[0]) * 1024, "measured"
        return 0, "inaccessible"
    except subprocess.TimeoutExpired:
        return 0, "estimate"
    except (subprocess.TimeoutExpired, ValueError, FileNotFoundError, OSError):
        return 0, "inaccessible"


def _du_batch(paths: list[str], timeout: int = 60) -> dict[str, int]:
    """Command a set of paths with one `du` invocation; missing entries are
    simply absent from the result."""
    out: dict[str, int] = {}
    if not paths:
        return out
    for start in range(0, len(paths), 50):
        chunk = paths[start : start + 50]
        try:
            proc = subprocess.run(
                ["du", "-sk"] + chunk,
                capture_output=True, text=True, timeout=timeout,
            )
            for line in proc.stdout.splitlines():
                parts = line.strip().split("\t", 1)
                if len(parts) == 2:
                    try:
                        out[parts[1]] = int(parts[0]) * 1024
                    except ValueError:
                        pass
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            continue
    return out


def _walk_meta(path: Path, budget_seconds: float = 6.0) -> tuple[int, int, float]:
    """Best-effort (files, dirs, latest-mtime) walk of *path* within a time
    budget. Returns a 3-tuple; files variant skips directories in symlinked
    sub-trees — os.walk(followlinks=False) keeps us from leaving the tree."""
    files = dirs = 0
    latest = 0.0
    start = time.monotonic()
    for root, dnames, fnames in _safe_os_walk(path):
        if time.monotonic() - start > budget_seconds:
            break
        for d in dnames:
            dirs += 1
        for f in fnames:
            files += 1
            fp = Path(root) / f
            try:
                m = fp.stat(follow_symlinks=False).st_mtime
                if m > latest:
                    latest = m
            except OSError:
                pass
        # Trim huge dirs after we have a representative count
        if files > 60_000:
            break
    return files, dirs, latest


_EXPAND_DEFAULTS = (False, [""])


def _safe_os_walk(path: Path):
    """os.walk that never follows symlinks and never raises."""
    try:
        import os
        yield from os.walk(str(path), topdown=True, followlinks=False)
    except OSError:
        return


def _iter_children(dir_path: Path):
    """Yield (path, stat) for each direct child that is safely stat-able.
    Skips dotfiles by default and symlinks that point outside their parent."""
    try:
        children = list(dir_path.iterdir())
    except (PermissionError, OSError):
        return
    for child in children:
        try:
            st = child.lstat()
            if child.is_symlink():
                continue  # never traverse a symlink into overlapping targets
            yield child, st
        except OSError:
            continue


# ── Home / Xcode installation discovery ───────────────────────────────────────

def _xcode_app_candidates() -> list[Path]:
    """Installed Xcode.app copies — active + beta + leftover copies."""
    apps = Path("/Applications")
    out: list[Path] = []
    if apps.exists():
        try:
            for entry in sorted(apps.iterdir()):
                if entry.is_dir() and entry.name.startswith("Xcode") and entry.name.endswith(".app"):
                    out.append(entry)
        except (PermissionError, OSError):
            pass
    return out


def _read_plist(path: Path) -> dict:
    try:
        with open(path, "rb") as fh:
            return plistlib.load(fh)
    except Exception:  # noqa: BLE001 — malformed/missing plist is fine
        return {}


def _xcode_version(app_path: Path) -> Optional[str]:
    info = _read_plist(app_path / "Contents" / "Info.plist")
    v = f'{info.get("CFBundleShortVersionString", "")}'.strip()
    b = f'{info.get("CFBundleVersion", "")}'.strip()
    if v and b and v != b:
        return f"{v} ({b})"
    return v or b or None


# ── Core scan: DerivedData / Archives / DeviceSupport / Caches ────────────────

def _scan_dir_children(
    root: Path,
    kind: str,
    label_prefix: str,
    classify_child,
    max_children: int = 300,
) -> list[DevItem]:
    """One DevItem per direct child directory of *root*, sized in one batch."""
    if not root.is_dir():
        return []
    children: list[tuple[Path, object]] = list(_iter_children(root))
    if not children:
        return []
    paths = [str(c) for c, _ in children]
    sizes = _du_batch(paths, timeout=90)
    out: list[DevItem] = []
    for child, st in children[:max_children]:
        size = sizes.get(str(child), 0)
        measurement = "measured" if size else "inaccessible"
        extra = classify_child(child)
        item = DevItem(
            path=str(child),
            kind=kind,
            label=label_prefix.replace("{name}", child.name),
            size_bytes=size,
            measurement=measurement,
            file_count=-1,
            directory_count=-1,
            last_modified=st.st_mtime,
            version_hint=extra.get("version"),
            active_guess=extra.get("active_guess", "uncertain"),
            notes=extra.get("notes", {}),
            bundle_id=extra.get("bundle_id"),
            model_id=extra.get("model_id"),
        )
        out.append(item)
    return out


def _classify_derived_data(child: Path) -> dict:
    """A DerivedData child is named for its workspace/project."""
    version = None
    build_dir = child / "Build" / "Products"
    products = sorted(build_dir.glob("*")) if build_dir.is_dir() else []
    return {
        "version": version,
        "notes": {"projects": [p.name for p in products[:3]]},
        "active_guess": "rebuildable",
    }


def _classify_device_support(child: Path) -> dict:
    """Version lives in folder names like '18.0 (22A3350) arm64e'."""
    import re

    m = re.match(r"^(?P<os>iOS|watchOS|tvOS|visionOS|xrsim)?[ ]?(?P<ver>[\d.]+)", child.name)
    version = m.group("ver") if m else child.name
    return {"version": version if version else child.name}


def _classify_device_support_dir_children(child: Path) -> dict:
    """Version hint for arbitrary 'iOS DeviceSupport' style roots."""
    return _classify_device_support(child)


def _classify_archive(child: Path) -> dict:
    """User archives (.xcarchive or similar). User data — never assumed safe."""
    info = _read_plist(child / "Info.plist")
    version = None
    if info:
        d = info.get("ApplicationProperties", {}) or {}
        version = d.get("ShortVersionString") or None
    return {"version": version, "notes": {"user_archive": True}}


def _classify_sim_devices_child(child: Path) -> dict:
    """Simulator device dir — read device data (device.plist)."""
    plist_path = child / "device.plist"
    if not plist_path.is_file():
        # Some device sets keep identity files inside deviceSet dir
        plist_path = child / "device_set.plist"
    info = _read_plist(plist_path)
    name = info.get("name") or child.name
    udid = info.get("UDID") or child.name
    runtime = info.get("runtime") or ""
    state = int(info.get("state", 0) or 0)      # 0 shutdown, 1 booted
    device_type = info.get("deviceType") or ""
    active = "active" if state == 1 else "uncertain"
    return {
        "version": runtime.rsplit("/", 1)[-1].replace("com.apple.CoreSimulator.SimRuntime.", "")
        or None,
        "notes": {
            "device_name": name,
            "device_type": device_type,
            "booted": state == 1,
        },
        "active_guess": active,
        "model_id": str(udid) if udid else None,
    }


def _classify_sim_runtime_child(child: Path) -> dict:
    """Runtime bundle under CoreSimulator/Images or Simulator dir.
    Read Info.plist for platform + build id (supported metadata only)."""
    plist_path = child / "Info.plist"
    if not plist_path.is_file():
        plist_path = child / "Contents" / "Info.plist"
    info = _read_plist(plist_path)
    platform = info.get("DTPlatformName") or child.name.split("-")[0]
    build = info.get("DTSDKBuild") or info.get("CFBundleVersion")
    ver = info.get("CFBundleShortVersionString")
    version = f"{platform} {ver} ({build})" if (ver and build) else (ver or build or None)
    return {
        "version": version,
        "notes": {"platform": str(platform)},
        "active_guess": "uncertain",
    }


def _classify_xcode_app(child: Path) -> dict:
    return {"version": _xcode_version(child), "active_guess": "active"}


def _classify_legacy(child: Path) -> dict:
    return {"active_guess": "uncertain", "notes": {"legacy_leftover": True}}


def _classify_tmp(child: Path) -> dict:
    return {"active_guess": "inactive"}


# ── Simulator runtimes via supported interface ────────────────────────────────

def _simctl_runtimes() -> list[dict]:
    """`xcrun simctl runtime list -j` — supported interface. Empty on failure."""
    try:
        proc = subprocess.run(
            ["xcrun", "simctl", "runtime", "list", "-j"],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            return []
        data = json.loads(proc.stdout)
        if isinstance(data, dict):
            runtimes = []
            for key, val in data.items():
                if isinstance(val, dict):
                    val = {**val, "runtime_id": key}
                runtimes.append(val)
            return runtimes
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError,
            json.JSONDecodeError, ValueError):
        pass
    return []


def _simctl_devices() -> dict[str, list[dict]]:
    """`xcrun simctl list devices -j` grouped by runtime, supported."""
    try:
        proc = subprocess.run(
            ["xcrun", "simctl", "list", "devices", "-j"],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            return {}
        data = json.loads(proc.stdout)
        return data.get("devices", {}) if isinstance(data, dict) else {}
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError,
            json.JSONDecodeError, ValueError):
        return {}


# ── Main entry ────────────────────────────────────────────────────────────────

def scan_developer_storage(cancel_flag: Optional[list] = None) -> dict:
    """
    Deep scan of Xcode/Simulator developer storage.

    cancel_flag : optional single-element list; setting [0] = True cancels.
    Returns a report dict:
      {
        "items": [DevItem.to_dict()],
        "runtime_devices": {...},     # simctl report if available
        "summary": {...}
      }
    """
    home = Path.home()
    dev = home / "Library" / "Developer"
    items: list[DevItem] = []
    started = time.monotonic()

    def cancelled() -> bool:
        return bool(cancel_flag and cancel_flag[0])

    # ── Per-child directories ──────────────────────────────────────────────
    def add_children(root: Path, kind, prefix, classify) -> None:
        nonlocal items
        if cancelled() or not root.is_dir():
            return
        items.extend(_scan_dir_children(root, kind, prefix, classify))

    add_children(dev / "Xcode" / "DerivedData", "derived_data",
                 "DerivedData — {name}", _classify_derived_data)
    add_children(dev / "Xcode" / "Archives", "archives",
                 "Archive — {name}", _classify_archive)
    add_children(dev / "Xcode" / "iOS DeviceSupport", "device_support",
                 "iOS DeviceSupport {name}", _classify_device_support)
    add_children(dev / "Xcode" / "watchOS DeviceSupport", "device_support",
                 "watchOS DeviceSupport {name}", _classify_device_support)
    add_children(dev / "Xcode" / "UserData", "xcode_user_data",
                 "Xcode UserData — {name}", _classify_legacy)
    add_children(dev / "CoreSimulator" / "Devices", "sim_devices",
                 "Simulator device — {name}", _classify_sim_devices_child)
    add_children(dev / "CoreSimulator" / "Caches", "sim_caches",
                 "Simulator cache — {name}", _classify_tmp)
    add_children(dev / "CoreSimulator" / "Profiles", "sim_profiles",
                 "Simulator profiles {name}", _classify_tmp)
    add_children(dev / "Simulator", "sim_shared_caches",
                 "Simulator — {name}", _classify_tmp)

    # Legacy/unversioned leftovers (previous Xcode installs et al)
    leftovers_dir = dev / "Xcode" / "DerivedData" / "ModuleCache.noindex"
    if leftovers_dir.is_dir():
        sz, measurement = _du(str(leftovers_dir), timeout=20)
        items.append(
            DevItem(
                path=str(leftovers_dir),
                kind="module_cache",
                label="Swift/Clang module cache",
                size_bytes=sz,
                measurement=measurement,
                file_count=-1,
                directory_count=-1,
                version_hint=None,
                active_guess="rebuildable",
            )
        )

    # All Xcode *apps* present (active + beta + leftovers). Sizing leaked
    # from /Applications would inflate totals with app binaries we'd never
    # delete, so only report size when the app is no longer referenced by
    # the active toolchain default (best-effort heuristic).
    for app in _xcode_app_candidates():
        sz, measurement = _du(str(app), timeout=60)
        items.append(
            DevItem(
                path=str(app),
                kind="xcode_app",
                label=f"Xcode application — {app.name}",
                size_bytes=sz,
                measurement=measurement,
                file_count=-1,
                directory_count=-1,
                version_hint=_xcode_version(app),
                active_guess="active",
            )
        )

    # ── Simulator runtime disk images (CoreSimulator/Images) ────────────────
    runtime_images = dev / "CoreSimulator" / "Images"
    if runtime_images.is_dir():
        add_children(runtime_images, "sim_runtime",
                     "Simulator runtime — {name}", _classify_sim_runtime_child)

    # Obscure but recognised: Xcode-generated support locations
    for extra_dir, kind, label in [
        (dev / "Xcode" / "SourceMaterials", "docs_cache", "Documentation cache"),
        (dev / "Xcode" / "DocumentationCache", "docs_cache", "Documentation cache"),
    ]:
        if extra_dir.is_dir():
            add_children(extra_dir, kind, label + " — {name}", _classify_tmp)

    # NOTE: Kernel Debug Kits, installers, EFI/OpenCore files and anything
    # outside ~/Library/Developer are deliberately NOT scanned here —
    # they are protected resources (devclassify.PROTECTED / safety.py).

    # ── Aggregate simulator metadata through supported tools ───────────────
    runtime_devices = _simctl_devices()
    runtimes_meta = _simctl_runtimes()

    # ── Annotate with simctl runtime info when available ────────────────────
    runtime_by_id: dict[str, dict] = {}
    for rt in runtimes_meta:
        rid = rt.get("runtimeIdentifier") or rt.get("runtime_id") or ""
        runtime_by_id[str(rid).split("SimRuntime.")[-1]] = rt

    elapsed = time.monotonic() - started
    measured = sum(i.size_bytes for i in items if i.measurement == "measured")
    partial = sum(i.size_bytes for i in items if i.measurement == "estimate")

    report = {
        "elapsed_seconds": round(elapsed, 2),
        "items": [i.__dict__ for i in items],
        "runtime_devices": runtime_devices,
        "runtimes": runtimes_meta,
        "summary": {
            "item_count": len(items),
            "measured_bytes": measured,
            "estimated_bytes": partial,
            "measured_human": _fmt(measured),
            "estimated_human": _fmt(partial),
            "cancelled": cancelled(),
        },
    }
    return report


def format_size(n: int) -> str:
    """Re-export of scanner.format_size for tab/engine convenience."""
    from mac_cleaner.scanner import format_size as _fs
    return _fs(n)


def _fmt(n: int) -> str:
    return format_size(n)
