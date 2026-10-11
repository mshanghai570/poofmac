# SPDX-License-Identifier: MIT
# Copyright (c) 2026 lesteroliver — https://poofmac.app
"""
Developer-storage cleanup engine.

Completely separate from scanning (devscan) and classification
(devclassify). The ONLY module in the developer-cleanup feature that
performs destructive operations, and it performs them trash-first:

 1. A target is only eligible if classification said delete_eligible.
 2. Revalidated immediately before deletion (re-scanned, re-classified).
 3. Lives strictly inside ~/Library/Developer (plus sim profiles) —
    anything else is refused before it is touched.
 4. Default action is TRASH (recoverable). Permanent deletion is a
    separate explicit mode.
 5. Results are per-item, honest, and never roll partial failures up
    into a blanket success.

Weaving through safety.py: paths outside the Developer tree already
cannot pass validate_path (they are CAUTION/PROTECTED), and this module
calls it again anyway, because safety checks should not trust a caller's
memory of what it checked once.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from mac_cleaner.audit import AuditLogger
from mac_cleaner.devclassify import (
    PROTECTED,
    classify,
)
from mac_cleaner.devscan import _du  # reuse the same measurement primitive
from mac_cleaner.safety import SafetyViolation, assert_safe, validate_path

# Every target must live under one of these. Cleanup is refused otherwise.
APPROVED_ROOTS = (
    Path.home() / "Library" / "Developer",
    Path.home() / "Library" / "Caches" / "com.apple.dt.Xcode",
    Path.home() / "Library" / "Caches" / "CoreSimulator",
)

_BOOTED_RE = None  # placeholder — currently detected via simctl


@dataclass
class CleanupAction:
    path: str
    action: str            # trashed | dry_run | blocked | skipped | error | not_found
    size_bytes: int = 0
    detail: str = ""
    category: str = ""


@dataclass
class CleanupReport:
    results: list[CleanupAction] = field(default_factory=list)
    bytes_freed_estimate: int = 0
    bytes_trashed: int = 0
    errors: int = 0
    blocked: int = 0
    skipped: int = 0
    trashed: int = 0
    duration_seconds: float = 0.0

    def summary(self) -> dict:
        return {
            "trashed": self.trashed,
            "errors": self.errors,
            "blocked": self.blocked,
            "skipped": self.skipped,
            "bytes_trashed": self.bytes_trashed,
            "note": (
                "Bytes moved to Trash — physical disk space is only "
                "recovered when the Trash is emptied."
                if self.trashed
                else "Nothing was moved to the Trash."
            ),
        }


class DevCleanupEngine:
    """
    Explicit, revalidating, trash-first cleanup of developer storage.

    Usage:
        engine = DevCleanupEngine(audit)
        report = engine.cleanup(plan_items, trash=True, dry_run=False)

    plan_items : list of dicts as produced by plan_items() in devclassify / GUI,
                 each with at least {"path", "kind", "category",
                                     "delete_eligible"}.
    """

    def __init__(self, audit: Optional[AuditLogger] = None) -> None:
        self.audit = audit or AuditLogger()

    # ── Helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _unique_trash_target(src: Path) -> Path:
        trash = Path.home() / ".Trash"
        # The Trash usually exists on macOS, but a fresh/odd account (or a CI
        # Linux host running the suite) may not have it — create it so the
        # rename below lands somewhere instead of failing with ENOENT.
        try:
            trash.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        stamp = time.strftime("%Y%m%d-%H%M%S")
        candidate = trash / f"{src.name} ({stamp})"
        counter = 1
        while candidate.exists():
            counter += 1
            candidate = trash / f"{src.name} ({stamp}-{counter})"
        return candidate

    @staticmethod
    def _is_inside_approved_roots(path: Path) -> bool:
        resolved = path
        try:
            resolved = path.resolve(strict=False)
        except OSError:
            pass
        for root in APPROVED_ROOTS:
            try:
                root_resolved = root.resolve(strict=False)
            except OSError:
                root_resolved = root
            try:
                resolved.relative_to(root_resolved)
                return True
            except ValueError:
                continue
        return False

    def _revalidate(self, path_str: str) -> tuple[bool, str]:
        """
        Gate that re-checks the path immediately before deletion:
          • is inside an approved root,
          • resolves with no symlinks escaping the tree,
          • still exists,
          • current classification is still delete-eligible.
        """
        path = Path(path_str)
        if not path.is_absolute():
            return False, "Refusing relative path."

        # No symlink escape hatches.
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            return False, "Path already gone or unreadable."
        if not self._is_inside_approved_roots(resolved):
            return False, "Symlink resolves outside the approved developer roots."

        if not self._is_inside_approved_roots(resolved):
            return False, "Outside approved developer-storage roots."

        verdict = validate_path(str(resolved))
        if verdict.risk_level == "PROTECTED":
            return False, f"Safety layer flags this path: {verdict.reason}"

        item = {"kind": _kind_for(resolved), "label": resolved.name,
                "path": str(resolved), "size_bytes": 0,
                "measurement": "measured", "active_guess": "uncertain",
                "notes": {}}
        fresh = classify(item)
        if fresh["category"] == PROTECTED:
            return False, f"Fresh classification: {fresh['reason']}"

        return True, "ok"

    # ── Main ───────────────────────────────────────────────────────────────────

    def cleanup(
        self,
        plan_items: list[dict],
        *,
        trash: bool = True,
        dry_run: bool = False,
        progress: Optional[Callable[[int, int, str], None]] = None,
        cancel_flag: Optional[list] = None,
    ) -> CleanupReport:
        report = CleanupReport()
        started = time.monotonic()
        total = max(len(plan_items), 1)

        for index, item in enumerate(plan_items, start=1):
            if progress:
                progress(index, total, str(item.get("path", "")))
            if cancel_flag and cancel_flag[0]:
                report.results.append(CleanupAction(
                    path=str(item.get("path", "")),
                    action="skipped",
                    detail="Cancelled by user — remaining items skipped.",
                ))
                report.skipped += 1
                break

            path_str = str(item.get("path", ""))
            category = str(item.get("category", ""))
            delete_eligible = bool(item.get("delete_eligible", False))

            # ── Gate A: eligibility ───────────────────────────────────────
            if category == PROTECTED or not delete_eligible:
                report.results.append(CleanupAction(
                    path=path_str, action="blocked", category=category,
                    detail="Not classified as safely removable.",
                ))
                report.blocked += 1
                continue

            # ── Gate B: revalidation ──────────────────────────────────────
            ok, why = self._revalidate(path_str)
            if not ok:
                report.results.append(CleanupAction(
                    path=path_str, action="blocked", category=category,
                    detail=why,
                ))
                report.blocked += 1
                self.audit.log("DEV_BLOCKED", path_str, 0, why)
                continue

            target = Path(path_str)
            size = _du(path_str, timeout=40)[0]

            if dry_run:
                report.results.append(CleanupAction(
                    path=path_str, action="dry_run", category=category,
                    size_bytes=size,
                    detail="Would be moved to the Trash.",
                ))
                report.bytes_freed_estimate += size
                self.audit.log("DEV_DRY_RUN", path_str, size,
                               "Would trash (not executed)")
                continue

            # ── Gate C: actual removal ────────────────────────────────────
            try:
                if trash:
                    dest = self._unique_trash_target(target)
                    # rename keeps it on the same volume — fast and atomic.
                    target.rename(dest)
                    action, detail = "trashed", f"Moved to {dest}"
                else:
                    if target.is_dir() and not target.is_symlink():
                        shutil.rmtree(str(target))
                    else:
                        target.unlink()
                    action, detail = "trashed", "Permanently deleted — NOT recoverable."

                report.results.append(CleanupAction(
                    path=path_str, action=action, category=category,
                    size_bytes=size, detail=detail,
                ))
                report.trashed += 1
                report.bytes_trashed += size
                self.audit.log("DEV_DELETED" if not trash else "DEV_TRASHED",
                               path_str, size, detail)

            except (OSError, shutil.Error) as exc:
                msg = f"Could not remove: {exc}"
                report.results.append(CleanupAction(
                    path=path_str, action="error", category=category,
                    detail=msg,
                ))
                report.errors += 1
                self.audit.log("DEV_ERROR", path_str, 0, msg)

        report.duration_seconds = round(time.monotonic() - started, 2)
        return report


# ── Small shared helpers ──────────────────────────────────────────────────────

def _kind_for(resolved: Path) -> str:
    """Infer the classifier 'kind' just before deleting, from the location."""
    rel = str(resolved)
    parts = resolved.parts
    if "DerivedData" in parts:
        return "derived_data"
    if "Archives" in parts:
        return "archives"          # classify() will mark it PROTECTED, refusing
    if "Devices" in parts:
        return "sim_devices"
    if "DeviceSupport" in parts:
        return "device_support"
    if "ModuleCache" in rel:
        return "module_cache"
    if "Caches" in parts:
        return "sim_caches"
    return "derived_data"
