# SPDX-License-Identifier: MIT
# Copyright (c) 2026 lesteroliver — https://poofmac.app
"""
LLM tool definitions and dispatch.

Each tool corresponds to a scanner function. The LLM calls these via
structured tool-calling; it never runs shell commands directly.

Tool flow
─────────
  LLM → tool_call JSON  →  execute_tool()  →  scanner / safety function
                        ←  JSON string result

The `propose_cleanup_plan` tool is special: the LLM calls it to present its
findings. The TUI intercepts this and renders the plan as an interactive table.
"""

from __future__ import annotations

import json

from mac_cleaner import maintenance
from mac_cleaner.safety import validate_path
from mac_cleaner.scanner import (
    get_disk_usage,
    run_full_scan,
    scan_user_caches,
    scan_system_logs,
    scan_xcode_artifacts,
    scan_dev_artifacts,
    scan_homebrew_cache,
    scan_trash,
    scan_downloads,
    scan_docker,
    format_size,
)


def _result_ok(payload: object) -> bool:
    """Whether a maintenance result dict reports success."""
    return isinstance(payload, dict) and payload.get("success") is True

# ── Tool schema ───────────────────────────────────────────────────────────────

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_disk_overview",
            "description": (
                "Get current disk usage: total capacity, used, and free space. "
                "Always call this first before any other scan."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_full_disk_scan",
            "description": (
                "Run a comprehensive scan of all common disk-space consumers: "
                "app caches, logs, Xcode artifacts, Homebrew cache, Trash, "
                "Downloads, dev artifacts (node_modules/.venv/etc.), and Docker. "
                "Returns a structured summary with sizes and safety ratings."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scan_category",
            "description": "Scan a single category in depth for more detail.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": [
                            "caches", "logs", "xcode", "dev_artifacts",
                            "trash", "downloads", "homebrew", "docker",
                        ],
                        "description": "Category to scan.",
                    }
                },
                "required": ["category"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_path_safety",
            "description": (
                "Check whether a specific path is safe to delete. "
                "Always call this for any path not returned by a scan function "
                "before adding it to a cleanup proposal."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute file or directory path to check.",
                    }
                },
                "required": ["path"],
            },
        },
    },
    # ── Maintenance & optimization (beyond disk cleanup) ────────────────────
    {
        "type": "function",
        "function": {
            "name": "heavy_consumers",
            "description": (
                "List the top processes by CPU and by RAM right now. Read-only. "
                "Use when the user asks what is slowing the Mac down or eating battery."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "How many per list (default 10)."}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hung_applications",
            "description": (
                "List running GUI applications, ones suspected of hanging (stuck in "
                "an uninterruptible wait) and recorded freeze reports. Read-only. "
                "Follow with force_quit_app — with the user's OK, unsaved work is lost."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "force_quit_app",
            "description": (
                "Quit a misbehaving application by name, bundle id or pid. Default "
                "asks politely (SIGTERM); force=true kills it hard (SIGKILL). ALWAYS "
                "confirm with the user first — unsaved work is lost, especially on "
                "force. macOS session apps (Finder, Dock) are refused; use "
                "repair_applications for those."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "identifier": {
                        "type": "string",
                        "description": "App name, bundle id, or pid (e.g. 'Safari', 'com.apple.Safari', '1234').",
                    },
                    "force": {
                        "type": "boolean",
                        "description": "true = SIGKILL immediately; false = polite SIGTERM first (default).",
                    },
                },
                "required": ["identifier"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "repair_applications",
            "description": (
                "Restart Finder, Dock and SystemUIServer — the fix for stale icons, a "
                "frozen Dock, desktop glitches. launchd relaunches them instantly, so "
                "nothing is lost. Optionally also rebuild the user-domain Launch "
                "Services database (include_ls_rebuild=true) for a broken or "
                "duplicated 'Open With' menu — slow, so only on that symptom."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "include_ls_rebuild": {
                        "type": "boolean",
                        "description": "Also rebuild the Launch Services app registry (default false).",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "app_acceleration",
            "description": (
                "Clear low-risk UI state that apps re-derive on launch: clipboard, "
                "recent-items lists, Finder/Dock caches (restarts both). Optionally "
                "compact Mail's search index with vacuum_mail=true (Mail must be "
                "quit). Tell the user clipboard contents will be lost."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "vacuum_mail": {
                        "type": "boolean",
                        "description": "Also VACUUM Mail's SQLite index (default false).",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "launch_agents",
            "description": (
                "List user and system LaunchAgents (background services that run at "
                "login) with their enabled state. Read-only. For 'speed up my boot'."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "toggle_launch_agent",
            "description": (
                "Enable or disable a USER LaunchAgent (one from ~/Library/LaunchAgents) "
                "by renaming its plist. Reversible — call launch_agents first to see the "
                "labels. System-scope agents cannot be toggled; give the user the "
                "Terminal command instead. Ask the user to confirm before disabling."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "Agent label, i.e. the plist filename without .plist."},
                    "enable": {"type": "boolean", "description": "true to enable, false to disable."},
                },
                "required": ["label", "enable"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_report",
            "description": (
                "Report RAM usage, memory pressure, purgeable memory and swap. "
                "Read-only. Use for 'memory optimization' or 'free RAM' questions."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tm_snapshots",
            "description": (
                "List local Time Machine snapshots, which can hold GBs of purgeable "
                "disk space. Read-only. Call before thinning them."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "thin_tm_snapshots",
            "description": (
                "Ask Time Machine to thin (delete) local snapshots older than the given "
                "age, reclaiming disk space. Time Machine decides what is safe. "
                "Ask the user to confirm first; call tm_snapshots beforehand."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keep_hours": {
                        "type": "integer",
                        "description": "Keep snapshots newer than this many hours (default 24).",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "purgeable_space",
            "description": "Report the disk's purgeable space. Read-only.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_maintenance_guide",
            "description": (
                "Get a ready-to-copy Terminal recipe for maintenance that needs admin "
                "rights (flush_dns_cache, reindex_spotlight, speed_up_boot, "
                "speed_up_mail, repair_disk_permissions). PoofMac never runs sudo "
                "itself — present the commands, explain them, and let the user run them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Guide name, e.g. flush_dns_cache."}
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_cleanup_plan",
            "description": (
                "Present the cleanup plan to the user as a dry-run report. "
                "Call this AFTER scanning, with all items you recommend reviewing. "
                "The UI will render this as an interactive table for user approval. "
                "Include a clear summary and an entry for every item found."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": (
                            "1-3 sentence summary of findings: disk status, "
                            "total recoverable space, and key observations."
                        ),
                    },
                    "items": {
                        "type": "array",
                        "description": "Cleanup candidates for the user to review.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {
                                    "type": "string",
                                    "description": "Absolute path to delete or empty.",
                                },
                                "size_human": {
                                    "type": "string",
                                    "description": "Human-readable size, e.g. '8.2 GB'.",
                                },
                                "category": {
                                    "type": "string",
                                    "description": "Category name, e.g. 'App Caches'.",
                                },
                                "reason": {
                                    "type": "string",
                                    "description": (
                                        "Why this is safe (or not) to delete. "
                                        "Be specific and honest."
                                    ),
                                },
                                "risk_level": {
                                    "type": "string",
                                    "enum": ["SAFE", "CAUTION", "SKIP"],
                                    "description": (
                                        "SAFE = auto-recreated, CAUTION = review first, "
                                        "SKIP = do not delete."
                                    ),
                                },
                            },
                            "required": ["path", "size_human", "category", "reason", "risk_level"],
                        },
                    },
                },
                "required": ["summary", "items"],
            },
        },
    },
]


# ── Tool dispatcher ───────────────────────────────────────────────────────────

_CATEGORY_MAP = {
    "caches":        scan_user_caches,
    "logs":          scan_system_logs,
    "xcode":         scan_xcode_artifacts,
    "dev_artifacts": scan_dev_artifacts,
    "trash":         scan_trash,
    "downloads":     scan_downloads,
    "homebrew":      scan_homebrew_cache,
    "docker":        scan_docker,
}


def execute_tool(name: str, args: dict) -> str:
    """
    Dispatch a tool call and return the result as a JSON string.
    This is the only path through which the LLM can trigger scanning.
    """

    if name == "get_disk_overview":
        return json.dumps(get_disk_usage())

    if name == "run_full_disk_scan":
        result = run_full_scan()
        # Limit items per category to keep context window reasonable
        for r in result.get("results", []):
            if len(r.get("items", [])) > 12:
                r["items"] = r["items"][:12]
                r["items_truncated_to"] = 12
        return json.dumps(result)

    if name == "scan_category":
        cat = args.get("category", "")
        scanner = _CATEGORY_MAP.get(cat)
        if scanner is None:
            return json.dumps({"error": f"Unknown category: {cat!r}"})
        results = scanner()
        return json.dumps([r.to_dict() for r in results])

    if name == "check_path_safety":
        path = args.get("path", "")
        if not path:
            return json.dumps({"error": "path argument is required"})
        verdict = validate_path(path)
        return json.dumps(
            {
                "path": verdict.path,
                "is_safe": verdict.is_safe,
                "reason": verdict.reason,
                "risk_level": verdict.risk_level,
            }
        )

    if name == "heavy_consumers":
        return json.dumps(maintenance.heavy_consumers(limit=int(args.get("limit", 10) or 10)))

    if name == "hung_applications":
        return json.dumps(maintenance.hung_applications())

    if name == "force_quit_app":
        return json.dumps(
            maintenance.force_quit_app(
                str(args.get("identifier", "")), bool(args.get("force", False))
            )
        )

    if name == "repair_applications":
        return json.dumps(
            maintenance.repair_applications(
                include_ls_rebuild=bool(args.get("include_ls_rebuild", False))
            )
        )

    if name == "app_acceleration":
        return json.dumps(
            maintenance.app_acceleration(vacuum_mail=bool(args.get("vacuum_mail", False)))
        )

    if name == "launch_agents":
        return json.dumps(maintenance.launch_agents())

    if name == "toggle_launch_agent":
        return json.dumps(
            maintenance.toggle_launch_agent(
                str(args.get("label", "")), bool(args.get("enable", False))
            )
        )

    if name == "memory_report":
        return json.dumps(maintenance.memory_report())

    if name == "tm_snapshots":
        return json.dumps(maintenance.tm_snapshots())

    if name == "thin_tm_snapshots":
        return json.dumps(
            maintenance.thin_tm_snapshots(keep_hours=int(args.get("keep_hours", 24) or 24))
        )

    if name == "purgeable_space":
        return json.dumps(maintenance.purgeable_space())

    if name == "get_maintenance_guide":
        return json.dumps(maintenance.get_maintenance_guide(str(args.get("name", ""))))

    if name == "propose_cleanup_plan":
        # The TUI intercepts and renders this; we just echo it back so the
        # LLM receives confirmation the tool was called successfully.
        return json.dumps(
            {
                "status": "plan_presented",
                "item_count": len(args.get("items", [])),
            }
        )

    return json.dumps({"error": f"Unknown tool: {name!r}"})
