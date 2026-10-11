# SPDX-License-Identifier: MIT
# Copyright (c) 2026 lesteroliver — https://poofmac.app
"""
Deterministic classification of developer-storage findings.

Four categories, decided ONLY by code — never by the LLM:

  SAFE_CACHE       Reproducible data well covered by PoofMac's safety rules
                   (DerivedData, CoreSimulator caches, simulator device data
                   directories that simctl can recreate).
  REVIEW_REQUIRED  Real purpose, unclear usage — needs an informed decision.
  POTENTIALLY_OBSOLETE  Old runtimes / stale support files / leftovers.
  PROTECTED        Active Xcode installs, booted simulators, running-device
                   support, user archives, and everything outside the
                   approved scope.

The LLM may explain items, but the classification and the delete-eligibility
flag below are produced here and re-checked at cleanup time.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

from mac_cleaner.devscan import DevItem

SAFE_CACHE = "safe_cache"
REVIEW_REQUIRED = "review_required"
POTENTIALLY_OBSOLETE = "potentially_obsolete"
PROTECTED = "protected"

CATEGORY_LABELS = {
    SAFE_CACHE: "Safe cache",
    REVIEW_REQUIRED: "Review required",
    POTENTIALLY_OBSOLETE: "Potentially obsolete",
    PROTECTED: "Protected / active",
}

# Device-support versions strictly older than Xcode's minimum deployment are
# effectively dead weight, but PoofMac never assumes: any device_support dir
# whose iOS version is at least this many majors/minors behind *today's
# latest, as reported by the newest installed runtime*, lands in
# POTENTIALLY_OBSOLETE; anything else in REVIEW_REQUIRED.
SUPPORT_STALE_MAJOR = 2

_BOOTED_RE = re.compile(r"(?i)(^|\s)booted(\s|$)")


def _runtime_versions(report: dict) -> list[str]:
    """Versions actually installed, from simctl when available."""
    out: list[str] = []
    for rt in report.get("runtimes") or []:
        v = rt.get("version") or ""
        if v:
            out.append(str(v))
    return out


def _newest_runtime_major(versions: list[str]) -> Optional[int]:
    most = 0
    for v in versions:
        m = re.match(r"(\d+)", v)
        if m:
            most = max(most, int(m.group(1)))
    return most or None


def _support_version_from_label(label: str) -> Optional[float]:
    m = re.search(r"(\d+)(?:\.(\d+))?", label)
    if not m:
        return None
    minor = m.group(2) or "0"
    return float(f"{m.group(1)}.{minor}")


def classify(item: dict, report: Optional[dict] = None) -> dict:
    """
    Decided deterministically. A parsed dict understood as one DevItem as input.
    Returns {category, category_label, delete_eligible, reason, explanation}.
    """
    kind = item.get("kind", "")
    label = item.get("label", "")
    path = item.get("path", "")
    active = item.get("active_guess", "uncertain")
    measurement = item.get("measurement", "measured")
    runtime_maj = _newest_runtime_major(_runtime_versions(report or {}))
    notes = item.get("notes") or {}

    explanation = _explain(item, report)
    reason = ""
    delete_eligible = False

    if kind == "xcode_app":
        reason = (
            "An installed Xcode application — required for development. "
            "Never touched by cleanup."
        )
        category = PROTECTED

    elif kind == "derived_data":
        reason = (
            "Fully reproducible build cache; Xcode recreates it when a "
            "project is next opened."
        )
        category = SAFE_CACHE
        delete_eligible = True

    elif kind == "module_cache":
        reason = (            "Module cache — rebuilt on demand by Swift/Clang.")
        category = SAFE_CACHE
        delete_eligible = True

    elif kind == "sim_caches" or kind == "sim_shared_caches" or kind == "docs_cache":
        reason = "Simulator/Xcode cache, rebuilt automatically when needed."
        category = SAFE_CACHE
        delete_eligible = True

    elif kind == "sim_profiles":
        reason = (
            "Simulator profile data. May hold personalisation; review it."
        )
        category = REVIEW_REQUIRED

    elif kind == "sim_devices":
        booted = bool(notes.get("booted"))
        if booted:
            reason = "Simulator device is currently booted — protected until shut down."
            category = PROTECTED
        else:
            reason = (
                "Simulator device data. Deleting loses device state; Xcode "
                "can create an equivalent fresh device."
            )
            category = REVIEW_REQUIRED
            delete_eligible = True   # still requires per-item user consent

    elif kind == "sim_runtime":
        reason = (
            "Simulator runtime bundles are re-downloadable from Xcode during "
            "component install. Removing one while projects target that OS "
            "version forces a re-download."
        )
        category = REVIEW_REQUIRED

    elif kind == "archives":
        reason = (            "Your own app archives — user data, not cache. Required to "
            "symbolicate or roll back a shipped build.")
        category = PROTECTED

    elif kind == "device_support":
        stale = _is_stale_support(label, runtime_maj)
        if stale:
            reason = (
                f"Device support for an iOS/watchOS version substantially "
                f"older than the newest runtime ({runtime_maj or 'unknown'}) — "
                "likely obsolete, but PoofMac cannot see which devices you "
                "still connect. Confirm before deleting."
            )
            category = POTENTIALLY_OBSOLETE
            delete_eligible = True
        else:
            reason = (
                "Device support files for debugging on a physical device of "
                "this OS version. Deleting them forces re-symbolication on "
                "next attach."
            )
            category = REVIEW_REQUIRED

    elif kind == "xcode_user_data":
        reason = (
            "Xcode working data (history, key bindings, IB support). Small; "
            "deleting may reset personalisation."
        )
        category = REVIEW_REQUIRED

    elif kind == "legacy_leftover":
        reason = (
            "Leftover from a previous Xcode installation. PoofMac reports it "
            "but cannot verify no tool still references it — review manually."
        )
        category = POTENTIALLY_OBSOLETE

    else:
        reason = "Unrecognised developer-storage finding — treat carefully."
        category = REVIEW_REQUIRED

    # Universal guard rails, applied last so no rule can override them.
    if active == "active" and category != PROTECTED:
        category = PROTECTED
        reason += " Marked active during the scan."
        delete_eligible = False
    if measurement == "inaccessible" and category != PROTECTED:
        category = REVIEW_REQUIRED
        reason += " Size could not be measured (permissions), so savings are unknown."
        delete_eligible = False

    return {
        "category": category,
        "category_label": CATEGORY_LABELS[category],
        "delete_eligible": delete_eligible,
        "reason": reason,
        "explanation": explanation,
    }


def _is_stale_support(label: str, newest_major: Optional[int]) -> bool:
    ver = _support_version_from_label(label)
    if ver is None or newest_major is None:
        return False
    return (newest_major - int(ver)) >= SUPPORT_STALE_MAJOR


def _explain(item: dict, report: Optional[dict]) -> str:
    """Local rule-based prose for the detail panel. Honest about limits."""
    kind = item.get("kind", "")
    size = item.get("size_bytes", 0)
    measurement = item.get("measurement", "measured")
    modified = item.get("last_modified")
    age_days: Optional[int] = None
    if modified:
        try:
            age_days = int((time.time() - float(modified)) / 86400)
        except (TypeError, ValueError):
            age_days = None

    size_line = (
        f"Holds about {_size_h(size)}. "
        if measurement == "measured"
        else "Its size could not be fully measured (permissions or a very large "
             "tree); the number shown is a lower bound. "
    )

    regen = {
        "derived_data":  "Xcode rebuilds it on next project open.",
        "module_cache":  "Swift/Clang re-create it during the next build.",
        "sim_caches":    "The simulator/cache layer rebuilds it on demand.",
        "sim_shared_caches": "Rebuilt by the simulator runtime when used.",
        "docs_cache":    "Xcode re-downloads documentation on demand.",
        "sim_devices":   "Xcode can create a new empty device, but this one's "
                         "stored app data, logs, and settings are gone for good.",
        "sim_runtime":   "Re-downloadable from Xcode's component manager, but "
                         "initial download is multi-GB.",
        "device_support":"Xcode re-collects symbols from the physical device "
                         "the next time you connect it.",
        "archives":      "Cannot be regenerated — they are your shipped builds.",
        "xcode_app":     "Re-downloading needs your Apple ID and a multi-GB "
                         "download — never done automatically.",
        "legacy_leftover":"Uncertain whether anything still references this.",
        "xcode_user_data":"Partly rebuilt by Xcode; personal settings may not be.",
        "sim_profiles":  "Some profile data is re-created; some is per-user.",
    }.get(kind, "Regeneration needs are unknown — treat as one-time data.")

    what = {
        "derived_data":   "a per-project build cache (indexes, intermediates, built products)",
        "module_cache":   "the shared Swift/Clang module cache",
        "archives":       "a User Xcode archive (.xcarchive)",
        "device_support": "device-support symbols for debugging on a real device",
        "sim_devices":    "the data directory of a simulator device",
        "sim_runtime":    "an installed simulator runtime bundle",
        "sim_caches":     "a CoreSimulator cache directory",
        "sim_shared_caches": "a shared simulator cache directory",
        "docs_cache":     "downloaded Xcode documentation",
        "xcode_app":      "the Xcode application itself",
        "xcode_user_data":"per-user Xcode working data",
        "legacy_leftover":"legacy/leftover developer data",
        "sim_profiles":   "simulator device profile data",
    }.get(kind, "a developer-storage item")

    lines = [f"This is {what}."]
    if size > 0:
        lines.append(f"It uses roughly {_size_h(size)}. {size_line}")
    if age_days is not None:
        if age_days > 365:
            lines.append(f"Last touched over a year ago ({age_days} days).")
        elif age_days > 45:
            lines.append(f"Last touched {age_days} days ago.")
    lines.append(f"Can it be regenerated? {regen}")

    if kind == "sim_devices":
        lines.append(
            "Removing a device directory while the simulator daemon is "
            "running can confuse Xcode's device list for a moment; Xcode "
            "recovers on its own."
        )
    if kind == "archives":
        lines.append(
            "PoofMac will not propose trashing this automatically; you must "
            "select it explicitly."
        )
    return " ".join(lines)


def _size_h(n: int) -> str:
    from mac_cleaner.scanner import format_size
    return format_size(n)
