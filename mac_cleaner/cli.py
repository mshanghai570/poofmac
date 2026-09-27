# SPDX-License-Identifier: MIT
# Copyright (c) 2026 lesteroliver — https://poofmac.app
"""
PoofMac — Non-interactive Rich CLI.

Runs the AI scan pipeline and renders results as a Rich table to stdout.
No keyboard interaction required — suitable for scripts, CI, SSH sessions,
and piped output.

Usage
─────
    mac-cleaner --cli                    # scan + show table (no deletions)
    mac-cleaner --cli --safe-mode        # explicit safe mode (same behaviour)
    mac-cleaner --cli --model qwen3.6:35b-a3b
    mac-cleaner --cli --json             # output raw JSON instead of table
    mac-cleaner --cli --execute          # delete approved (SAFE) items after review

    poofmac --cli                        # same, new command name

Output
──────
  ┌──────────────────────────────────────────────────────────────────┐
  │           PoofMac — Disk Analysis                                │
  ├──────────────────────────────────────────────────────────────────┤
  │  Macintosh HD  ·  38.2 GB used  ·  189.8 GB free  (20%)         │
  ├──────────────────────────────────────────────────────────────────┤
  │  Category       Size     Risk     Path                           │
  │  App Caches     1.7 GB   SAFE     ~/Library/Caches               │
  │  System Logs    127 MB   SAFE     ~/Library/Logs                 │
  │  Downloads      777 MB   CAUTION  ~/Downloads                    │
  ├──────────────────────────────────────────────────────────────────┤
  │  Total recoverable (SAFE only):  ~1.8 GB                        │
  │  Run with --execute to delete SAFE items                         │
  └──────────────────────────────────────────────────────────────────┘

Threading model
───────────────
  The CLI is synchronous — it drives CleanerAgent in the main thread and
  renders a live spinner while waiting for the LLM.  No Qt dependency.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

from rich import box
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from mac_cleaner import __version__
from mac_cleaner.audit import AuditLogger
from mac_cleaner.config import Settings, MODEL_REGISTRY
from mac_cleaner.executor import Executor
from mac_cleaner.llm import CleanerAgent
from mac_cleaner.scanner import format_size, get_disk_usage

console = Console()

# ── Risk styling ──────────────────────────────────────────────────────────────

RISK_STYLE = {
    "SAFE":    ("bold green",  "●"),
    "CAUTION": ("bold yellow", "◐"),
    "SKIP":    ("bold red",    "○"),
}


# ── Disk summary ──────────────────────────────────────────────────────────────

def _disk_summary() -> str:
    try:
        d = get_disk_usage()
        pct = int(d["used_percent"])
        bar_filled = int(pct / 5)
        bar = "█" * bar_filled + "░" * (20 - bar_filled)
        return (
            f"[bold]Macintosh HD[/bold]  ·  "
            f"[cyan]{d['used_human']}[/cyan] used  ·  "
            f"[green]{d['free_human']}[/green] free  ·  "
            f"[dim]{bar}[/dim]  {pct}%"
        )
    except Exception:  # noqa: BLE001
        return "[dim]Disk info unavailable[/dim]"


# ── Rich table renderer ───────────────────────────────────────────────────────

def _render_table(items: list[dict]) -> Table:
    table = Table(
        box=box.ROUNDED,
        show_header=True,
        header_style="bold dim",
        expand=True,
        padding=(0, 1),
    )
    table.add_column("Category",    style="bold", min_width=18)
    table.add_column("Size",        justify="right", min_width=8)
    table.add_column("Risk",        justify="center", min_width=9)
    table.add_column("Path / Notes", no_wrap=False)

    safe_total = 0

    for item in items:
        risk = item.get("risk_level", "CAUTION")
        style, icon = RISK_STYLE.get(risk, ("yellow", "◐"))
        path  = item.get("path", "")
        reason = item.get("reason", "")
        note  = f"[dim]{reason}[/dim]" if reason else path

        table.add_row(
            item.get("category", ""),
            item.get("size_human", "?"),
            f"[{style}]{icon} {risk}[/{style}]",
            note or path,
        )

        if risk == "SAFE":
            safe_total += _parse_size(item.get("size_human", "0 B"))

    return table, safe_total


def _parse_size(size_str: str) -> int:
    try:
        parts = size_str.strip().split()
        n = float(parts[0])
        unit = parts[1].upper() if len(parts) > 1 else "B"
        mult = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
        return int(n * mult.get(unit, 1))
    except (ValueError, IndexError):
        return 0


# ── Spinner states ────────────────────────────────────────────────────────────

TOOL_LABELS = {
    "get_disk_overview":    "Checking disk overview",
    "run_full_disk_scan":   "Full disk scan",
    "scan_category":        "Scanning category",
    "check_path_safety":    "Verifying path safety",
    "propose_cleanup_plan": "Building cleanup plan",
}

# ── Live progress renderer ─────────────────────────────────────────────────────

_CHECK  = "[bold green]✓[/bold green]"
_SPIN   = "[bold cyan]⠋[/bold cyan]"
_CIRCLE = "[dim]○[/dim]"


def _render_steps(steps: list[dict]) -> "Group":
    from rich.console import Group as RGroup
    lines: list = []
    for s in steps:
        if s["state"] == "done":
            detail = f"  [dim]{s['detail']}[/dim]" if s.get("detail") else ""
            lines.append(Text.from_markup(f"  {_CHECK}  {s['label']}{detail}"))
        elif s["state"] == "running":
            lines.append(Text.from_markup(f"  {_SPIN}  [cyan]{s['label']}…[/cyan]"))
        else:
            lines.append(Text.from_markup(f"  {_CIRCLE}  [dim]{s['label']}[/dim]"))
    return RGroup(*lines) if lines else Text("")


def _banner(model_display: str, safe_mode: bool) -> Panel:
    mode_line = (
        "[yellow]Safe scan — no deletions[/yellow]" if safe_mode
        else "[dim]Scan + ready to clean[/dim]"
    )
    return Panel(
        f"[bold cyan]💨  PoofMac[/bold cyan]  [dim]v{__version__}[/dim]\n"
        f"[dim]AI-powered Mac disk cleaner[/dim]\n\n"
        f"[dim]Model:[/dim]  {model_display}\n"
        f"[dim]Mode:[/dim]   {mode_line}",
        border_style="cyan",
        expand=False,
        padding=(1, 3),
    )


# ── Main CLI runner ───────────────────────────────────────────────────────────

_CREDITS = (
    "Made by [link=https://github.com/lesteroliver911]lesteroliver[/link]"
    "  ·  [link=https://linkedin.com/in/lesteroliver]LinkedIn[/link]"
    "  ·  [link=https://poofmac.app]poofmac.app[/link]"
)


def _print_credits(json_mode: bool = False) -> None:
    if not json_mode:
        console.print(Rule(f"[dim]{_CREDITS}[/dim]"))


# ── Chat mode ─────────────────────────────────────────────────────────────────

_CHAT_EXAMPLES = [
    "clean my app caches",
    "what's taking up the most space?",
    "find large files I haven't opened in months",
    "show me everything in Downloads over 100 MB",
    "is it safe to delete my Xcode DerivedData?",
    "scan everything and tell me what to delete",
]

_CHAT_COMMANDS = (
    "[bold]delete[/bold] · delete safe items  "
    "[bold]scan[/bold] · full disk scan  "
    "[bold]q[/bold] · quit"
)


def run_chat(
    settings: Settings,
    safe_mode: bool = False,
    model_override: Optional[str] = None,
) -> None:
    """
    Free-form AI chat mode. No auto-scan — the user types what they want
    in plain English and the AI decides what tools to call.
    """
    if model_override:
        settings.preferred_local_model = model_override

    if not _is_configured(settings):
        settings = run_setup_wizard(settings)
        if model_override:
            settings.preferred_local_model = model_override

    _, model_display = settings.get_active_model()

    console.print()
    console.print(_banner(model_display, safe_mode))
    console.print(f"  {_disk_summary()}\n")

    # Welcome panel with examples
    examples_text = "\n".join(f"  [dim]·[/dim]  {e}" for e in _CHAT_EXAMPLES)
    console.print(
        Panel(
            "[bold]Ask anything about your disk in plain English.[/bold]\n\n"
            + examples_text,
            title="[cyan]💨 PoofMac Chat[/cyan]",
            border_style="cyan",
            padding=(1, 2),
        )
    )
    console.print(f"\n  [dim]{_CHAT_COMMANDS}[/dim]\n")

    plan_items: list[dict] = []

    while True:
        try:
            user_input = Prompt.ask(
                "  [bold cyan]💨 >[/bold cyan]",
                console=console,
                default="",
            ).strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break

        if not user_input or user_input.lower() in ("q", "quit", "exit"):
            break

        elif user_input.lower() in ("delete", "clean", "d"):
            safe_items = [i for i in plan_items if i.get("risk_level") == "SAFE"]
            if safe_mode:
                console.print("  [yellow]Safe mode is on — deletions are disabled.[/yellow]\n")
            elif not safe_items:
                console.print(
                    "  [dim]No SAFE items queued yet. "
                    "Try: [bold]scan everything[/bold] first.[/dim]\n"
                )
            else:
                _do_execute(safe_items, settings)
            continue

        elif user_input.lower() in ("scan", "rescan", "full scan", "r"):
            user_input = "Analyse my Mac's disk usage and show me everything I can safely clean up."

        console.print()
        new_items = _run_agent_with_progress(settings, user_input)
        if new_items is not None:
            plan_items = new_items
            _print_results(new_items, safe_mode, False, settings)
        console.print()

    _print_credits()


# ── First-run setup wizard ─────────────────────────────────────────────────────

_ENV_PATH = Path(".env")

_PROVIDERS = [
    {
        "key":     "anthropic",
        "label":   "Anthropic (Claude)",
        "note":    "Fastest & most reliable  —  anthropic.com/api",
        "env_key": "ANTHROPIC_API_KEY",
        "models":  MODEL_REGISTRY["anthropic"],
        "default": "claude-sonnet-4-6",
        "setting": "PREFERRED_CLOUD_MODEL",
    },
    {
        "key":     "openai",
        "label":   "OpenAI (GPT)",
        "note":    "Reliable cloud option  —  platform.openai.com",
        "env_key": "OPENAI_API_KEY",
        "models":  MODEL_REGISTRY["openai"],
        "default": "gpt-4o",
        "setting": "PREFERRED_CLOUD_MODEL",
    },
    {
        "key":     "openrouter",
        "label":   "OpenRouter",
        "note":    "One key, many models  —  openrouter.ai",
        "env_key": "OPENROUTER_API_KEY",
        "models":  MODEL_REGISTRY["openrouter"],
        "default": "anthropic/claude-sonnet-4-6",
        "setting": "PREFERRED_CLOUD_MODEL",
    },
    {
        "key":     "ollama_cloud",
        "label":   "Ollama Cloud",
        "note":    "Your Ollama subscription  —  ollama.com",
        "env_key": "OLLAMA_API_KEY",
        "models":  MODEL_REGISTRY["ollama_cloud"],
        "default": "gemma4:31b-cloud",
        "setting": "PREFERRED_LOCAL_MODEL",
    },
    {
        "key":     "ollama_local",
        "label":   "Ollama Local (free, runs on this Mac)",
        "note":    "No API key needed — model downloads once  —  ollama.com",
        "env_key": None,
        "models":  MODEL_REGISTRY["ollama_local"],
        "default": "qwen3.6:8b",
        "setting": "PREFERRED_LOCAL_MODEL",
    },
]


def _write_env(key: str, value: str) -> None:
    """Upsert a key=value in the .env file (create if missing)."""
    try:
        from dotenv import set_key, find_dotenv
        env_file = find_dotenv(usecwd=True) or str(_ENV_PATH)
        _ENV_PATH.touch(exist_ok=True)
        set_key(env_file, key, value)
    except Exception:
        # Fallback: append manually
        with open(_ENV_PATH, "a") as f:
            f.write(f"\n{key}={value}\n")


def _is_configured(settings: Settings) -> bool:
    """Return True if at least one usable model is configured."""
    has_key = any([
        settings.anthropic_api_key,
        settings.openrouter_api_key,
        settings.openai_api_key,
        settings.ollama_api_key,
    ])
    # A local model is usable without a key (Ollama local)
    local = settings.preferred_local_model
    has_local = bool(local) and not local.endswith("-cloud") and "cloud" not in local
    return has_key or has_local


def run_setup_wizard(settings: Settings) -> Settings:
    """
    Interactive first-run wizard. Guides the user through picking a provider,
    choosing a model, entering an API key, and optionally saving to .env.
    Returns a reloaded Settings object ready for use.
    """
    console.print()
    console.print(
        Panel(
            "[bold cyan]Welcome to PoofMac 💨[/bold cyan]\n\n"
            "Before your first scan, let's connect an AI model.\n"
            "[dim]This takes about 30 seconds and only needs to be done once.[/dim]",
            border_style="cyan",
            expand=False,
        )
    )
    console.print()

    # ── Step 1: choose provider ───────────────────────────────────────────────
    console.print("[bold]Step 1 of 3 — Choose an AI provider[/bold]\n")
    for i, p in enumerate(_PROVIDERS, 1):
        console.print(f"  [cyan]{i}[/cyan]  [bold]{p['label']}[/bold]")
        console.print(f"     [dim]{p['note']}[/dim]")
    console.print()

    while True:
        choice = Prompt.ask(
            "  Enter a number",
            choices=[str(i) for i in range(1, len(_PROVIDERS) + 1)],
            console=console,
        )
        provider = _PROVIDERS[int(choice) - 1]
        break

    console.print()

    # ── Step 2: choose model ──────────────────────────────────────────────────
    console.print(f"[bold]Step 2 of 3 — Choose a model[/bold]\n")
    for i, (model_id, desc) in enumerate(provider["models"], 1):
        recommended = " [dim](recommended)[/dim]" if model_id == provider["default"] else ""
        console.print(f"  [cyan]{i}[/cyan]  {desc}{recommended}")
    console.print()

    while True:
        model_choice = Prompt.ask(
            "  Enter a number",
            default="1",
            choices=[str(i) for i in range(1, len(provider["models"]) + 1)],
            console=console,
        )
        chosen_model, chosen_desc = provider["models"][int(model_choice) - 1]
        break

    console.print(f"\n  [green]✓[/green]  Selected: [bold]{chosen_desc}[/bold]\n")

    # ── Step 3: API key (skip for local Ollama) ───────────────────────────────
    api_key = ""
    if provider["env_key"]:
        console.print(f"[bold]Step 3 of 3 — Enter your {provider['label']} API key[/bold]\n")
        console.print(f"  [dim]Get your key at: {provider['note'].split('—')[-1].strip()}[/dim]")
        console.print(f"  [dim]It will only be stored locally in your .env file.[/dim]\n")

        api_key = Prompt.ask(
            f"  Paste your {provider['env_key']}",
            password=True,
            console=console,
        ).strip()

        if not api_key:
            console.print("\n  [yellow]No key entered — skipping.[/yellow]")
            console.print(
                f"  [dim]Add it manually to .env:  {provider['env_key']}=your_key_here[/dim]\n"
            )
    else:
        console.print("[bold]Step 3 of 3 — Local model setup[/bold]\n")
        console.print(
            f"  Ollama runs entirely on your Mac. Pull the model once with:\n\n"
            f"  [bold cyan]  ollama pull {chosen_model}[/bold cyan]\n\n"
            f"  [dim](~5–20 GB download depending on model size)[/dim]\n"
        )
        console.print(
            "  [dim]Once downloaded it will be available offline forever.[/dim]\n"
        )

    # ── Save to .env ──────────────────────────────────────────────────────────
    save = Confirm.ask(
        "  Save this configuration to .env for future runs?",
        default=True,
        console=console,
    )

    if save:
        _write_env(provider["setting"], chosen_model)
        if api_key:
            _write_env(provider["env_key"], api_key)
        console.print(
            f"\n  [green]✓[/green]  Saved to [bold].env[/bold] — "
            "you won't need to do this again.\n"
        )
    else:
        # Set in-process env so this run still works
        os.environ[provider["setting"]] = chosen_model
        if api_key:
            os.environ[provider["env_key"]] = api_key

    # Reload settings so the new values take effect
    try:
        return Settings(_env_file=str(_ENV_PATH))  # type: ignore[call-arg]
    except Exception:
        return Settings()


def _print_error(msg: str) -> None:
    console.print(
        Panel(f"[bold red]Error[/bold red]\n\n{msg}", border_style="red", expand=False)
    )


def _handle_agent_exception(exc: Exception, json_mode: bool) -> None:
    exc_str = str(exc).lower()
    if "authentication" in exc_str or "api key" in exc_str or "unauthorized" in exc_str:
        _print_error(
            "Authentication failed.\n\n"
            "Check your API key in [bold].env[/bold]:\n"
            "  • Anthropic → ANTHROPIC_API_KEY\n"
            "  • OpenAI    → OPENAI_API_KEY\n"
            "  • OpenRouter → OPENROUTER_API_KEY\n"
            "  • Ollama Cloud → OLLAMA_API_KEY"
        )
    elif "connection refused" in exc_str or "ollama" in exc_str:
        _print_error(
            "Cannot reach Ollama.\n\n"
            "Start the Ollama server with:\n"
            "  [bold cyan]ollama serve[/bold cyan]\n\n"
            "Or set a cloud model in [bold].env[/bold]: PREFERRED_CLOUD_MODEL=claude-sonnet-4-6"
        )
    elif "no model" in exc_str or "model not found" in exc_str:
        _print_error(
            "No model configured.\n\n"
            "Add at least one of the following to [bold].env[/bold]:\n"
            "  PREFERRED_LOCAL_MODEL=qwen3.6:8b\n"
            "  PREFERRED_CLOUD_MODEL=claude-sonnet-4-6"
        )
    else:
        _print_error(f"Unexpected error: {exc}\n\nCheck ~/.poofmac-audit.jsonl for details.")
    _print_credits(json_mode)
    sys.exit(1)

def run_cli(
    settings: Settings,
    safe_mode: bool = False,
    execute: bool = False,
    output_json: bool = False,
    model_override: Optional[str] = None,
    message: str = "Analyse my Mac's disk usage and show me everything I can safely clean up.",
) -> None:
    """
    Run a complete scan and drop into an interactive follow-up loop.
    """
    if model_override:
        settings.preferred_local_model = model_override

    # First-run setup: no model configured and not in JSON/scripted mode
    if not output_json and not _is_configured(settings):
        settings = run_setup_wizard(settings)
        if model_override:
            settings.preferred_local_model = model_override

    model_name, model_display = settings.get_active_model()

    if output_json:
        _run_json(settings, model_display, message)
        return

    # ── Banner ────────────────────────────────────────────────────────────────
    console.print()
    console.print(_banner(model_display, safe_mode))
    console.print(f"  {_disk_summary()}\n")

    # ── First scan ────────────────────────────────────────────────────────────
    plan_items = _run_agent_with_progress(settings, message)

    if plan_items is None:
        # Error already printed inside _run_agent_with_progress
        _print_credits()
        return

    # ── Results ───────────────────────────────────────────────────────────────
    _print_results(plan_items, safe_mode, execute, settings)

    # ── Interactive follow-up loop ────────────────────────────────────────────
    if sys.stdin.isatty():
        _interactive_loop(settings, plan_items, safe_mode)

    _print_credits()


# ── Agent runner with step checklist ──────────────────────────────────────────

def _run_agent_with_progress(
    settings: Settings,
    message: str,
) -> Optional[list[dict]]:
    """
    Run the agent and show a live step-by-step progress checklist.
    Returns the plan items list, or None on error.
    """
    from rich.console import Group as RGroup
    from rich.live import Live

    steps: list[dict] = []
    agent_messages: list[str] = []
    plan_items: list[dict] = []
    current_step_idx: list[int] = [-1]

    def _add_step(label: str) -> int:
        steps.append({"state": "running", "label": label, "detail": ""})
        return len(steps) - 1

    def _finish_step(idx: int, detail: str = "") -> None:
        if 0 <= idx < len(steps):
            steps[idx]["state"] = "done"
            steps[idx]["detail"] = detail

    _frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    _fc = [0]

    def _live_render() -> "RGroup":
        _fc[0] = (_fc[0] + 1) % len(_frames)
        frame = _frames[_fc[0]]
        parts: list = []
        for s in steps:
            if s["state"] == "done":
                detail = (f"  [dim]{s['detail']}[/dim]") if s.get("detail") else ""
                parts.append(Text.from_markup(f"  {_CHECK}  {s['label']}{detail}"))
            elif s["state"] == "running":
                parts.append(Text.from_markup(
                    f"  [bold cyan]{frame}[/bold cyan]  [cyan]{s['label']}…[/cyan]"
                ))
        return RGroup(*parts) if parts else Text.from_markup("  [dim]Starting…[/dim]")

    try:
        with Live(
            _live_render(),
            console=console,
            refresh_per_second=12,
            transient=False,
        ) as live:
            agent = CleanerAgent(settings)
            step_idx = -1

            for event in agent.run(message):
                etype = event.get("type")

                if etype == "status":
                    if step_idx == -1:
                        step_idx = _add_step(event["text"])
                    else:
                        steps[step_idx]["label"] = event["text"]

                elif etype == "tool_call":
                    if step_idx >= 0:
                        _finish_step(step_idx)
                    label = TOOL_LABELS.get(event["name"], event["name"])
                    step_idx = _add_step(label)

                elif etype == "tool_result":
                    result_text = event.get("text", "")
                    # Extract a short size/count summary from the result if present
                    detail = ""
                    if result_text:
                        # Grab first meaningful line
                        first = result_text.strip().splitlines()[0][:60] if result_text.strip() else ""
                        detail = first
                    _finish_step(step_idx, detail)
                    step_idx = -1

                elif etype == "plan_ready":
                    plan_items = event["plan"].get("items", [])
                    if step_idx >= 0:
                        _finish_step(step_idx, f"{len(plan_items)} items found")
                        step_idx = -1
                    else:
                        steps.append({
                            "state": "done",
                            "label": "Cleanup plan ready",
                            "detail": f"{len(plan_items)} items found",
                        })

                elif etype == "message":
                    agent_messages.append(event["text"].strip())

                elif etype == "error":
                    if step_idx >= 0:
                        steps[step_idx]["state"] = "done"
                        steps[step_idx]["label"] = f"[red]{steps[step_idx]['label']}[/red]"
                    live.stop()
                    _print_error(event["text"])
                    return None

                live.update(_live_render())

    except KeyboardInterrupt:
        console.print("\n  [dim]Interrupted.[/dim]\n")
        return None
    except Exception as exc:  # noqa: BLE001
        _handle_agent_exception(exc, False)
        return None  # unreachable, but satisfies type checker

    console.print()

    # Stream the AI summary if present
    if agent_messages:
        _stream_text(agent_messages[0])

    return plan_items


def _stream_text(text: str) -> None:
    """Print AI response text with a typewriter streaming effect."""
    import time
    console.print()
    console.print("  [bold cyan]AI[/bold cyan]  ", end="")
    # Print word by word for a streaming feel
    words = text.split(" ")
    for i, word in enumerate(words):
        console.print(word + (" " if i < len(words) - 1 else ""), end="", highlight=False)
        if len(word) > 3:
            time.sleep(0.015)
    console.print("\n")


# ── Results printer ───────────────────────────────────────────────────────────

def _print_results(
    plan_items: list[dict],
    safe_mode: bool,
    execute: bool,
    settings: Settings,
) -> None:
    if not plan_items:
        console.print(
            Panel(
                "[bold green]✓  Your Mac looks clean![/bold green]\n\n"
                "[dim]No cleanup candidates were found.\n"
                "Try again after more usage, or ask a custom question below.[/dim]",
                border_style="green",
                expand=False,
            )
        )
        return

    table, safe_total = _render_table(plan_items)
    console.print(table)
    console.print()

    safe_items    = [i for i in plan_items if i.get("risk_level") == "SAFE"]
    caution_items = [i for i in plan_items if i.get("risk_level") == "CAUTION"]
    skip_items    = [i for i in plan_items if i.get("risk_level") == "SKIP"]

    console.print(
        f"  [bold]{len(plan_items)} items found[/bold]  ·  "
        f"[green]{len(safe_items)} safe[/green]  ·  "
        f"[yellow]{len(caution_items)} review[/yellow]  ·  "
        f"[red]{len(skip_items)} skip[/red]  ·  "
        f"[bold green]~{format_size(safe_total)} recoverable[/bold green]"
    )
    console.print()

    if execute and not safe_mode and safe_items:
        _do_execute(safe_items, settings)
    elif not execute:
        console.print(
            f"  [dim]Type [bold]delete[/bold] to remove the "
            f"{len(safe_items)} SAFE item(s), or ask any question below.[/dim]"
        )
        console.print()


# ── Interactive follow-up loop ─────────────────────────────────────────────────

def _interactive_loop(
    settings: Settings,
    plan_items: list[dict],
    safe_mode: bool,
) -> None:
    safe_items = [i for i in plan_items if i.get("risk_level") == "SAFE"]

    console.print(
        "  [dim]Commands:[/dim]  "
        "[bold]delete[/bold] · delete SAFE items  "
        "[bold]rescan[/bold] · fresh scan  "
        "[bold]q[/bold] · quit  "
        "[dim]or ask any question[/dim]"
    )
    console.print()

    while True:
        try:
            user_input = Prompt.ask(
                "  [bold cyan]💨 >[/bold cyan]",
                console=console,
                default="",
            ).strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not user_input or user_input.lower() in ("q", "quit", "exit"):
            break

        elif user_input.lower() in ("delete", "clean", "d"):
            if safe_mode:
                console.print("  [yellow]Safe mode is on — deletions disabled.[/yellow]\n")
            elif not safe_items:
                console.print("  [dim]No SAFE items to delete.[/dim]\n")
            else:
                _do_execute(safe_items, settings)
            break

        elif user_input.lower() in ("rescan", "scan", "r"):
            console.print()
            new_items = _run_agent_with_progress(
                settings,
                "Analyse my Mac's disk usage and show me everything I can safely clean up.",
            )
            if new_items is not None:
                plan_items[:] = new_items
                safe_items = [i for i in plan_items if i.get("risk_level") == "SAFE"]
                _print_results(new_items, safe_mode, False, settings)

        else:
            # Follow-up question
            console.print()
            new_items = _run_agent_with_progress(settings, user_input)
            if new_items is not None and new_items:
                plan_items[:] = new_items
                safe_items = [i for i in plan_items if i.get("risk_level") == "SAFE"]
                _print_results(new_items, safe_mode, False, settings)
            console.print()


# ── JSON output path ──────────────────────────────────────────────────────────

def _run_json(settings: Settings, model_display: str, message: str) -> None:
    plan_items: list[dict] = []
    agent_messages: list[str] = []
    try:
        agent = CleanerAgent(settings)
        for event in agent.run(message):
            if event.get("type") == "plan_ready":
                plan_items = event["plan"].get("items", [])
            elif event.get("type") == "message":
                agent_messages.append(event["text"].strip())
            elif event.get("type") == "error":
                sys.stdout.write(
                    json.dumps({"status": "error", "error": event["text"]}) + "\n"
                )
                sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        sys.stdout.write(json.dumps({"status": "error", "error": str(exc)}) + "\n")
        sys.exit(1)

    sys.stdout.write(
        json.dumps(
            {
                "model": model_display,
                "status": "clean" if not plan_items else "found",
                "items": plan_items,
                "messages": agent_messages,
            },
            indent=2,
        )
        + "\n"
    )


# ── Execute SAFE deletions ────────────────────────────────────────────────────

def _do_execute(safe_items: list[dict], settings: Settings) -> None:
    console.print(
        f"\n  [bold yellow]About to permanently delete "
        f"{len(safe_items)} SAFE item(s) "
        f"(~{format_size(sum(_parse_size(i.get('size_human','0')) for i in safe_items))})[/bold yellow]"
    )
    console.print(
        "  [dim]This cannot be undone. "
        "Audit log: ~/.poofmac-audit.jsonl[/dim]\n"
    )
    try:
        confirm = Prompt.ask(
            "  Type [bold]YES[/bold] to confirm, anything else to cancel",
            console=console,
        ).strip()
    except (EOFError, KeyboardInterrupt):
        console.print("\n  [dim]Cancelled.[/dim]\n")
        return

    if confirm != "YES":
        console.print("  [dim]Cancelled.[/dim]\n")
        return

    audit    = AuditLogger()
    executor = Executor(safe_mode=False, audit=audit)
    total_freed = 0

    with console.status("Deleting…"):
        for item in safe_items:
            path   = item.get("path", "")
            result = executor.delete(path, dry_run=False)
            if result.action == "deleted":
                total_freed += result.size_freed
                console.print(
                    f"  [green]✓[/green]  {path}  [dim]({result.size_freed_human})[/dim]"
                )
            else:
                console.print(f"  [red]✗[/red]  {path}  [dim]{result.error}[/dim]")

    console.print()
    console.print(
        f"  [bold green]Done — freed approximately "
        f"{format_size(total_freed)}[/bold green]"
    )
    console.print(f"  [dim]Audit log: {audit.log_path}[/dim]\n")
