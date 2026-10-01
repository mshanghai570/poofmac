# SPDX-License-Identifier: MIT
# Copyright (c) 2026 lesteroliver — https://poofmac.app
"""
Disk scanner — all scan functions used by the LLM tools.

Each function returns a list of ScanResult objects. The caller (tools.py)
serialises them to JSON for the LLM. No file is modified here — scanning only.

Design choices
──────────────
• Uses `du -sk` via subprocess for directory sizing. Python's os.walk is
  accurate but extremely slow on large trees; `du` uses kernel calls.
• Batches du calls where possible to reduce subprocess overhead.
• Caps result sets to avoid flooding the LLM context window.
• Dev-artifact search uses `find` for speed, then batches `du`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


# ── Utilities ─────────────────────────────────────────────────────────────────

def format_size(n: int) -> str:
    """Format bytes as a human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _du(path: str, timeout: int = 30) -> int:
    """Return directory/file size in bytes using `du -sk`."""
    try:
        result = subprocess.run(
            ["du", "-sk", path],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0 and result.stdout.strip():
            kb = int(result.stdout.split()[0])
            return kb * 1024
    except (subprocess.TimeoutExpired, ValueError, FileNotFoundError, OSError):
        pass
    return 0


def _du_batch(paths: list[str], timeout: int = 60) -> dict[str, int]:
    """Size multiple paths in one subprocess call. Much faster than N×_du()."""
    if not paths:
        return {}
    results: dict[str, int] = {}
    # du can fail with E2BIG if too many args; chunk at 50
    for chunk_start in range(0, len(paths), 50):
        chunk = paths[chunk_start : chunk_start + 50]
        try:
            proc = subprocess.run(
                ["du", "-sk"] + chunk,
                capture_output=True, text=True, timeout=timeout,
            )
            for line in proc.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split("\t", 1)
                if len(parts) == 2:
                    try:
                        results[parts[1]] = int(parts[0]) * 1024
                    except ValueError:
                        pass
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
    return results


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class ScanResult:
    category: str
    path: str
    size_bytes: int
    description: str
    safe_to_delete: bool
    risk_level: str          # SAFE | CAUTION | SKIP
    items: list[dict] = field(default_factory=list)

    def to_dict(self, max_items: int = 15) -> dict:
        return {
            "category": self.category,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "size_human": format_size(self.size_bytes),
            "description": self.description,
            "safe_to_delete": self.safe_to_delete,
            "risk_level": self.risk_level,
            "items": self.items[:max_items],
            "items_total": len(self.items),
        }


# ── Individual scanners ───────────────────────────────────────────────────────

def get_disk_usage() -> dict:
    """Overall disk usage for the volume that actually holds user data.

    APFS shares one container between the sealed system volume and
    /System/Volumes/Data, and ``df /`` reports only the system slice — a
    10 GB "Used" figure and ~9% full on a disk that is really 91% full, which
    is how this used to tell people their nearly-full Mac was fine.
    ``statfs`` on the Data volume reports the whole container, so that is the
    primary source; ``df`` remains the    fallback for platforms without it.
    """
    for path in ("/System/Volumes/Data", "/"):
        try:
            total, used, free = shutil.disk_usage(path)
        except OSError:
            continue
        if total <= 0:
            continue
        return {
            "total": total,
            "used": used,
            "free": free,
            "total_human": format_size(total),
            "used_human": format_size(used),
            "free_human": format_size(free),
            "used_percent": round(used / total * 100, 1),
            "measured": "statfs",
        }

    # Fallback: parse df. Only reached if statfs fails on every path.
    try:
        proc = subprocess.run(
            ["df", "-k", "/System/Volumes/Data"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        lines = proc.stdout.strip().splitlines()
        if len(lines) >= 2:
            parts = lines[1].split()
            total_kb = int(parts[1])
            used_kb = int(parts[2])
            free_kb = int(parts[3])
            total = total_kb * 1024
            used = used_kb * 1024
            free = free_kb * 1024
            return {
                "total": total,
                "used": used,
                "free": free,
                "total_human": format_size(total),
                "used_human": format_size(used),
                "free_human": format_size(free),
                "used_percent": round(used / total * 100, 1) if total else 0,
                "measured": "df",
            }
    except (subprocess.TimeoutExpired, ValueError, IndexError, FileNotFoundError):
        pass

    total, used, free = 0, 0, 0
    return {
        "total": total,
        "used": used,
        "free": free,
        "total_human": format_size(total),
        "used_human": format_size(used),
        "free_human": format_size(free),
        "used_percent": 0,
        "measured": "unavailable",
    }


def scan_user_caches() -> list[ScanResult]:
    """Scan ~/Library/Caches — the single biggest safe win on most Macs."""
    cache_dir = Path.home() / "Library" / "Caches"
    if not cache_dir.exists():
        return []

    try:
        entries = [e for e in cache_dir.iterdir() if not e.name.startswith(".")]
    except PermissionError:
        return []

    paths = [str(e) for e in entries]
    sizes = _du_batch(paths, timeout=45)

    items = []
    total = 0
    for entry in entries:
        sz = sizes.get(str(entry), 0)
        if sz > 0:
            items.append(
                {
                    "name": entry.name,
                    "path": str(entry),
                    "size_bytes": sz,
                    "size_human": format_size(sz),
                }
            )
            total += sz

    items.sort(key=lambda x: x["size_bytes"], reverse=True)

    if not items:
        return []

    return [
        ScanResult(
            category="Application Caches",
            path=str(cache_dir),
            size_bytes=total,
            description=(
                f"App caches in ~/Library/Caches ({len(items)} apps). "
                "Completely safe to delete — every app rebuilds its cache on next launch."
            ),
            safe_to_delete=True,
            risk_level="SAFE",
            items=items,
        )
    ]


def scan_system_logs() -> list[ScanResult]:
    """Scan ~/Library/Logs."""
    logs_dir = Path.home() / "Library" / "Logs"
    if not logs_dir.exists():
        return []

    size = _du(str(logs_dir), timeout=20)
    if size == 0:
        return []

    # Collect top-level subdirs for display
    items: list[dict] = []
    try:
        for entry in sorted(logs_dir.iterdir(), key=lambda e: e.name):
            sz = _du(str(entry), timeout=10)
            if sz > 0:
                items.append(
                    {
                        "name": entry.name,
                        "path": str(entry),
                        "size_bytes": sz,
                        "size_human": format_size(sz),
                    }
                )
    except (PermissionError, OSError):
        pass

    items.sort(key=lambda x: x["size_bytes"], reverse=True)

    return [
        ScanResult(
            category="Application & System Logs",
            path=str(logs_dir),
            size_bytes=size,
            description=(
                "Log files in ~/Library/Logs. Safe to delete — logs are "
                "regenerated automatically by the OS and apps."
            ),
            safe_to_delete=True,
            risk_level="SAFE",
            items=items,
        )
    ]


def scan_xcode_artifacts() -> list[ScanResult]:
    """Xcode DerivedData, iOS device support, and simulators."""
    base = Path.home() / "Library" / "Developer"
    targets = [
        (
            base / "Xcode" / "DerivedData",
            "Xcode DerivedData",
            "Xcode build cache. 100% safe to delete — rebuilt when you open any project.",
        ),
        (
            base / "Xcode" / "iOS DeviceSupport",
            "iOS Device Support Files",
            "Device support for physical iOS devices. Old iOS versions are safe to remove.",
        ),
        (
            base / "Xcode" / "watchOS DeviceSupport",
            "watchOS Device Support Files",
            "Device support for Apple Watch. Old versions are safe to remove.",
        ),
        (
            base / "CoreSimulator" / "Devices",
            "iOS/watchOS Simulators",
            "Simulator images. Delete old/unused ones — Xcode re-downloads on demand.",
        ),
    ]

    results = []
    for path, name, desc in targets:
        if path.exists():
            sz = _du(str(path), timeout=30)
            if sz > 0:
                results.append(
                    ScanResult(
                        category=name,
                        path=str(path),
                        size_bytes=sz,
                        description=desc,
                        safe_to_delete=True,
                        risk_level="SAFE",
                    )
                )
    return results


def scan_dev_artifacts() -> list[ScanResult]:
    """
    Find dev build artifacts (node_modules, .venv, __pycache__, etc.)
    in common development directories. Uses `find` for speed.
    """
    home = Path.home()

    # Candidate root directories to search
    search_roots: list[str] = []
    for candidate in [
        "Projects", "Project", "Code", "Developer", "dev",
        "workspace", "repos", "git", "src", "Development",
    ]:
        p = home / candidate
        if p.exists() and p.is_dir():
            search_roots.append(str(p))

    # Always include top-level of home (depth-1 only via maxdepth below)
    search_roots.append(str(home))

    artifact_meta: dict[str, tuple[str, str, str]] = {
        "node_modules": ("Node.js dependencies", "SAFE", "Recreate: npm install"),
        ".venv":        ("Python virtualenv", "SAFE", "Recreate: python -m venv .venv"),
        "venv":         ("Python virtualenv", "SAFE", "Recreate: python -m venv venv"),
        "__pycache__":  ("Python bytecode cache", "SAFE", "Auto-created by Python"),
        ".mypy_cache":  ("Mypy type-check cache", "SAFE", "Recreated by mypy"),
        ".pytest_cache":("Pytest cache", "SAFE", "Recreated by pytest"),
        ".ruff_cache":  ("Ruff linter cache", "SAFE", "Recreated by ruff"),
        ".next":        ("Next.js build cache", "SAFE", "Recreate: npm run build"),
        ".nuxt":        ("Nuxt.js build cache", "SAFE", "Recreate: npm run build"),
        "dist":         ("Build output (dist/)", "CAUTION", "Recreate with your build command"),
        "build":        ("Build output (build/)", "CAUTION", "Recreate with your build command"),
        "target":       ("Rust/Java build output", "CAUTION", "Recreate: cargo build / mvn package"),
    }

    found_paths: list[tuple[str, str]] = []

    for artifact in artifact_meta:
        for root in search_roots:
            maxdepth = "3" if root == str(home) else "6"
            try:
                proc = subprocess.run(
                    ["find", root, "-name", artifact, "-type", "d",
                     "-maxdepth", maxdepth, "-not", "-path", "*/.*/*"],
                    capture_output=True, text=True, timeout=15,
                )
                if proc.returncode == 0:
                    for line in proc.stdout.splitlines():
                        line = line.strip()
                        if line:
                            found_paths.append((line, artifact))
            except (subprocess.TimeoutExpired, FileNotFoundError):
                continue

    if not found_paths:
        return []

    # Deduplicate
    seen: set[str] = set()
    unique: list[tuple[str, str]] = []
    for p, a in found_paths:
        if p not in seen:
            seen.add(p)
            unique.append((p, a))

    # Batch size check
    just_paths = [p for p, _ in unique[:80]]
    sizes = _du_batch(just_paths, timeout=60)

    items = []
    for path, artifact in unique[:80]:
        sz = sizes.get(path, 0)
        if sz < 10 * 1024 * 1024:  # Skip <10 MB — not worth showing
            continue
        desc, risk, note = artifact_meta[artifact]
        items.append(
            {
                "name": artifact,
                "path": path,
                "size_bytes": sz,
                "size_human": format_size(sz),
                "description": desc,
                "risk_level": risk,
                "note": note,
            }
        )

    items.sort(key=lambda x: x["size_bytes"], reverse=True)

    if not items:
        return []

    total = sum(i["size_bytes"] for i in items)
    return [
        ScanResult(
            category="Development Artifacts",
            path=str(home),
            size_bytes=total,
            description=(
                f"Found {len(items)} dev artifact directories "
                f"({format_size(total)} total). Each can be recreated with "
                "your build tool — safe to delete when you don't need them."
            ),
            safe_to_delete=False,
            risk_level="CAUTION",
            items=items,
        )
    ]


def scan_homebrew_cache() -> list[ScanResult]:
    """Homebrew download cache."""
    try:
        proc = subprocess.run(
            ["brew", "--cache"], capture_output=True, text=True, timeout=5
        )
        if proc.returncode != 0:
            return []
        cache_path = proc.stdout.strip()
        if not os.path.exists(cache_path):
            return []
        sz = _du(cache_path, timeout=20)
        if sz == 0:
            return []
        return [
            ScanResult(
                category="Homebrew Download Cache",
                path=cache_path,
                size_bytes=sz,
                description=(
                    "Homebrew download cache. Safe to delete — Homebrew re-downloads "
                    "packages when needed. Tip: run `brew cleanup` for a managed clear."
                ),
                safe_to_delete=True,
                risk_level="SAFE",
            )
        ]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


def scan_trash() -> list[ScanResult]:
    """User Trash — ~/.Trash."""
    trash = Path.home() / ".Trash"
    if not trash.exists():
        return []

    sz = _du(str(trash), timeout=20)
    if sz == 0:
        return []

    try:
        item_count = sum(1 for _ in trash.iterdir())
    except PermissionError:
        item_count = 0

    return [
        ScanResult(
            category="Trash",
            path=str(trash),
            size_bytes=sz,
            description=(
                f"Your Trash contains {item_count} item(s) totalling "
                f"{format_size(sz)}. Emptying the Trash permanently removes them."
            ),
            safe_to_delete=True,
            risk_level="SAFE",
        )
    ]


def scan_downloads(min_size_mb: int = 50) -> list[ScanResult]:
    """Find large files/folders in ~/Downloads."""
    downloads = Path.home() / "Downloads"
    if not downloads.exists():
        return []

    items = []
    try:
        for entry in downloads.iterdir():
            try:
                if entry.is_file():
                    sz = entry.stat().st_size
                elif entry.is_dir():
                    sz = _du(str(entry), timeout=10)
                else:
                    continue
                if sz >= min_size_mb * 1024 * 1024:
                    items.append(
                        {
                            "name": entry.name,
                            "path": str(entry),
                            "size_bytes": sz,
                            "size_human": format_size(sz),
                            "type": "file" if entry.is_file() else "folder",
                        }
                    )
            except (PermissionError, OSError):
                continue
    except PermissionError:
        return []

    items.sort(key=lambda x: x["size_bytes"], reverse=True)

    if not items:
        return []

    total = sum(i["size_bytes"] for i in items)
    return [
        ScanResult(
            category="Large Downloads",
            path=str(downloads),
            size_bytes=total,
            description=(
                f"Found {len(items)} large items (≥{min_size_mb} MB) in "
                f"~/Downloads totalling {format_size(total)}. Review each "
                "item carefully before deleting."
            ),
            safe_to_delete=False,
            risk_level="CAUTION",
            items=items[:20],
        )
    ]


def scan_docker() -> list[ScanResult]:
    """Docker disk usage summary."""
    try:
        proc = subprocess.run(
            ["docker", "system", "df"],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            return []
        output = proc.stdout.strip()
        if not output:
            return []
        return [
            ScanResult(
                category="Docker",
                path="docker://",
                size_bytes=0,
                description=(
                    "Docker is using disk space for images, containers, and volumes. "
                    "Run `docker system prune` to clean unused resources. "
                    "Review carefully to avoid removing data you need."
                ),
                safe_to_delete=False,
                risk_level="CAUTION",
                items=[{"docker_df_output": output}],
            )
        ]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


# ── Full scan ─────────────────────────────────────────────────────────────────

def run_full_scan() -> dict:
    """
    Run every scanner and return a combined summary dict.
    This is the primary tool the LLM calls.
    """
    all_results: list[ScanResult] = []
    all_results.extend(scan_user_caches())
    all_results.extend(scan_system_logs())
    all_results.extend(scan_xcode_artifacts())
    all_results.extend(scan_homebrew_cache())
    all_results.extend(scan_trash())
    all_results.extend(scan_downloads())
    all_results.extend(scan_dev_artifacts())
    all_results.extend(scan_docker())

    disk = get_disk_usage()
    safe_total = sum(r.size_bytes for r in all_results if r.safe_to_delete)
    caution_total = sum(r.size_bytes for r in all_results if not r.safe_to_delete)
    grand_total = safe_total + caution_total

    return {
        "disk": disk,
        "results": [r.to_dict() for r in all_results],
        "summary": {
            "categories_found": len(all_results),
            "safe_recoverable_bytes": safe_total,
            "safe_recoverable_human": format_size(safe_total),
            "caution_review_bytes": caution_total,
            "caution_review_human": format_size(caution_total),
            "grand_total_bytes": grand_total,
            "grand_total_human": format_size(grand_total),
        },
    }
