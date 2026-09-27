# SPDX-License-Identifier: MIT
# Copyright (c) 2026 lesteroliver — https://poofmac.app
"""
PoofMac — Textual TUI entry point.

Screen flow
───────────
  DisclaimerScreen (modal) → MainScreen

MainScreen layout
─────────────────
  ┌──────────────────────────────────────────────────────────────┐
  │ Header: PoofMac                              model  HH:MM:SS │
  ├─ [SAFE MODE BANNER if active] ───────────────────────────────┤
  │  Activity Log (left)      │  Disk Overview + Results (right) │
  │                           │                                  │
  │  • Live status updates    │  [Category] [Size] [Risk] [Path] │
  │  • Tool call trace        │  ✅ App Caches    8.2GB  SAFE    │
  │  • Agent messages         │  ⚪ Downloads     4.1GB  REVIEW  │
  │                           │  ❌ node_modules  600MB  SKIP    │
  ├───────────────────────────┴──────────────────────────────────┤
  │  Input: ask anything...    [🔍 Scan]  [🗑 Execute (N items)] │
  └──────────────────────────────────────────────────────────────┘

Keyboard shortcuts
──────────────────
  F5        Start scan
  Space     Toggle row approval (approve ↔ skip)
  Ctrl+A    Approve all SAFE rows
  Ctrl+U    Unapprove all rows
  Ctrl+X    Execute approved deletions
  Q         Quit
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Static,
)

from mac_cleaner import __version__
from mac_cleaner.tool_cli import run_tools as _run_tools_cli
from mac_cleaner.audit import AuditLogger
from mac_cleaner.config import Settings
from mac_cleaner.executor import Executor
from mac_cleaner.llm import CleanerAgent
from mac_cleaner.scanner import format_size, get_disk_usage

# ── Screens ───────────────────────────────────────────────────────────────────


class DisclaimerScreen(ModalScreen[bool]):
    """Full-screen disclaimer shown on every launch."""

    DEFAULT_CSS = """
    DisclaimerScreen {
        align: center middle;
    }
    #disclaimer-box {
        width: 72;
        height: auto;
        background: $surface;
        border: double $warning;
        padding: 1 2;
    }
    #disclaimer-title {
        text-align: center;
        text-style: bold;
        color: $warning;
        margin-bottom: 1;
    }
    #disclaimer-body {
        color: $text;
        margin-bottom: 1;
    }
    #disclaimer-buttons {
        layout: horizontal;
        align: center middle;
        height: 3;
    }
    #btn-accept {
        margin-right: 2;
    }
    #disclaimer-credits {
        text-align: center;
        color: $text-muted;
        margin-top: 1;
    }
    """

    DISCLAIMER_TEXT = """\
⚠️  PLEASE READ BEFORE CONTINUING

PoofMac uses an AI model to analyse your disk and suggest files for
deletion. While it has multiple safety layers, it is software — and software
can have bugs.

BY CONTINUING YOU AGREE THAT:

  • You understand this tool modifies your filesystem.
  • You are responsible for reviewing every item before approving deletion.
  • The authors are NOT liable for any data loss, system instability, or
    damage caused by using this software.
  • You have backups of important data (Time Machine, cloud backup, etc.).
  • You will use Safe Mode (--safe-mode) to scan without deleting if in doubt.

WHAT THIS TOOL WILL NEVER DELETE:
  /System, /usr, /bin, /etc, ~/.ssh, Keychain, Documents, Photos, Music,
  Mail, Messages, Contacts, Desktop, Movies — all hard-blocked in code.

WHAT THIS TOOL CAN DELETE (only with your explicit approval):
  App caches, logs, Xcode build artifacts, node_modules, Python venvs,
  Homebrew cache, Trash, and items you select in ~/Downloads.

This tool is open-source. Review the safety.py module before use.
"""

    def compose(self) -> ComposeResult:
        with Vertical(id="disclaimer-box"):
            yield Static("⚠  PoofMac — Safety Disclaimer", id="disclaimer-title")
            yield Static(self.DISCLAIMER_TEXT, id="disclaimer-body")
            with Horizontal(id="disclaimer-buttons"):
                yield Button("✅  I Understand & Accept", id="btn-accept", variant="warning")
                yield Button("❌  Exit", id="btn-decline", variant="error")
            yield Static(
                "Made by lesteroliver · github.com/lesteroliver911 · poofmac.app",
                id="disclaimer-credits",
            )

    @on(Button.Pressed, "#btn-accept")
    def accept(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#btn-decline")
    def decline(self) -> None:
        self.dismiss(False)


class ConfirmDeleteModal(ModalScreen[bool]):
    """Confirmation modal shown before executing deletions."""

    DEFAULT_CSS = """
    ConfirmDeleteModal {
        align: center middle;
    }
    #confirm-box {
        width: 60;
        height: auto;
        background: $surface;
        border: double $error;
        padding: 1 2;
    }
    #confirm-title {
        text-align: center;
        text-style: bold;
        color: $error;
        margin-bottom: 1;
    }
    #confirm-info {
        margin-bottom: 1;
    }
    #confirm-buttons {
        layout: horizontal;
        align: center middle;
        height: 3;
    }
    #btn-confirm {
        margin-right: 2;
    }
    """

    def __init__(self, count: int, total_human: str) -> None:
        super().__init__()
        self.count = count
        self.total_human = total_human

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static("⚠  Confirm Deletion", id="confirm-title")
            yield Static(
                f"You are about to permanently delete {self.count} item(s) "
                f"totalling approximately {self.total_human}.\n\n"
                "This CANNOT be undone. Make sure you have a backup.\n"
                "A full audit log will be written to ~/.poofmac-audit.jsonl",
                id="confirm-info",
            )
            with Horizontal(id="confirm-buttons"):
                yield Button(
                    f"🗑  Delete {self.count} Item(s)", id="btn-confirm", variant="error"
                )
                yield Button("Cancel", id="btn-cancel", variant="default")

    @on(Button.Pressed, "#btn-confirm")
    def confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#btn-cancel")
    def cancel(self) -> None:
        self.dismiss(False)


# ── Main application ──────────────────────────────────────────────────────────


class MacCleanerApp(App):
    TITLE = "PoofMac"
    SUB_TITLE = "Disk cleaner powered by LLM"

    CSS = """
    Screen {
        background: $background;
    }

    Header {
        background: $primary-darken-3;
    }

    #safe-banner {
        background: $warning;
        color: $background;
        text-style: bold;
        content-align: center middle;
        height: 1;
        padding: 0 1;
    }

    #body {
        layout: horizontal;
        height: 1fr;
    }

    #left-panel {
        width: 38%;
        layout: vertical;
        border-right: solid $primary-darken-2;
    }

    .panel-title {
        background: $primary-darken-2;
        color: $text;
        text-style: bold;
        padding: 0 1;
        height: 1;
    }

    #activity-log {
        height: 1fr;
        padding: 0 1;
    }

    #right-panel {
        width: 62%;
        layout: vertical;
    }

    #disk-overview {
        height: 3;
        background: $surface;
        padding: 0 1;
        border-bottom: solid $primary-darken-2;
        content-align: left middle;
    }

    #table-hint {
        height: 1;
        background: $surface-darken-1;
        color: $text-muted;
        padding: 0 1;
        content-align: left middle;
    }

    #results-table {
        height: 1fr;
    }

    #input-row {
        height: auto;
        layout: horizontal;
        padding: 1 1;
        border-top: solid $primary-darken-2;
        align-vertical: middle;
    }

    #chat-input {
        width: 1fr;
    }

    #btn-scan {
        width: 14;
        margin-left: 1;
    }

    #btn-execute {
        width: 24;
        margin-left: 1;
    }
    """

    BINDINGS = [
        Binding("f5", "scan", "Scan", show=True),
        Binding("ctrl+x", "execute", "Execute", show=True),
        Binding("space", "toggle_row", "Toggle", show=True),
        Binding("ctrl+a", "approve_all_safe", "Approve Safe", show=True),
        Binding("ctrl+u", "unapprove_all", "Clear", show=True),
        Binding("q", "quit", "Quit", show=True),
    ]

    def __init__(self, settings: Settings, safe_mode: bool = False) -> None:
        super().__init__()
        self.settings = settings
        self.safe_mode = safe_mode or settings.safe_mode
        self.audit = AuditLogger()
        self.executor = Executor(safe_mode=self.safe_mode, audit=self.audit)

        # Cleanup plan state
        self.cleanup_items: list[dict] = []
        self.approvals: dict[str, bool] = {}   # row_key → approved
        self.row_order: list[str] = []          # ordered row keys
        self.plan_size_bytes: dict[str, int] = {}  # row_key → estimated bytes

        self._agent: Optional[CleanerAgent] = None
        self._scanning = False

    # ── Layout ─────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        if self.safe_mode:
            yield Static(
                "🛡  SAFE MODE — Scanning only. No files will be deleted.",
                id="safe-banner",
            )

        with Horizontal(id="body"):
            with Vertical(id="left-panel"):
                yield Static(" Activity Log", classes="panel-title")
                yield RichLog(id="activity-log", highlight=True, markup=True, wrap=True)

            with Vertical(id="right-panel"):
                yield Static("", id="disk-overview")
                yield Static(
                    " [Space] toggle  [Ctrl+A] approve all safe  [Ctrl+U] clear",
                    id="table-hint",
                )
                yield DataTable(
                    id="results-table",
                    cursor_type="row",
                    zebra_stripes=True,
                )

        with Horizontal(id="input-row"):
            yield Input(
                placeholder="Ask about your disk, or press F5 / 'Scan'…",
                id="chat-input",
            )
            yield Button("🔍 Scan", id="btn-scan", variant="primary")
            yield Button(
                "🗑  Execute (0 items)",
                id="btn-execute",
                variant="success",
                disabled=True,
            )

        yield Footer()

    def on_mount(self) -> None:
        self._setup_table()
        self.push_screen(DisclaimerScreen(), self._after_disclaimer)

    def _after_disclaimer(self, accepted: bool) -> None:
        if not accepted:
            self.exit()
            return
        self._refresh_disk_overview()
        log = self.query_one("#activity-log", RichLog)
        model_ok, model_msg = self.settings.validate_model_access()
        if model_ok:
            log.write(f"[green]✅ {model_msg}[/green]")
        else:
            log.write(f"[red]❌ Model not configured:[/red]\n{model_msg}")
        log.write("\n[dim]Press [bold]F5[/bold] or click [bold]Scan[/bold] to analyse your disk.[/dim]")
        if self.safe_mode:
            log.write("[yellow]🛡  Safe mode is ON — no files will be deleted.[/yellow]")

    def _setup_table(self) -> None:
        table = self.query_one("#results-table", DataTable)
        table.add_column("Status",      key="status",   width=12)
        table.add_column("Category",    key="category", width=22)
        table.add_column("Size",        key="size",     width=10)
        table.add_column("Risk",        key="risk",     width=9)
        table.add_column("Path / Description", key="path")

    def _refresh_disk_overview(self) -> None:
        try:
            d = get_disk_usage()
            bar_filled = int(d["used_percent"] / 5)  # 20 chars = 100%
            bar = "█" * bar_filled + "░" * (20 - bar_filled)
            colour = "red" if d["used_percent"] > 85 else "yellow" if d["used_percent"] > 70 else "green"
            text = (
                f" 💾  [bold]{d['used_human']}[/bold] used of "
                f"[bold]{d['total_human']}[/bold]   "
                f"[{colour}]{bar}[/{colour}]  "
                f"[{colour}]{d['used_percent']}%[/{colour}]   "
                f"Free: [bold]{d['free_human']}[/bold]"
            )
            self.query_one("#disk-overview", Static).update(text)
        except Exception:  # noqa: BLE001
            pass

    # ── Agent worker ───────────────────────────────────────────────────────────

    @work(thread=True, exclusive=True)
    def _run_agent(self, message: str) -> None:
        self.call_from_thread(self._set_scanning, True)
        try:
            agent = CleanerAgent(self.settings)
            for event in agent.run(message):
                self.call_from_thread(self._handle_event, event)
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(
                self._handle_event, {"type": "error", "text": str(exc)}
            )
        finally:
            self.call_from_thread(self._set_scanning, False)

    def _set_scanning(self, scanning: bool) -> None:
        self._scanning = scanning
        btn = self.query_one("#btn-scan", Button)
        btn.disabled = scanning
        btn.label = "⏳ Scanning…" if scanning else "🔍 Scan"

    def _handle_event(self, event: dict) -> None:
        log = self.query_one("#activity-log", RichLog)
        etype = event.get("type")

        if etype == "status":
            log.write(f"[dim]{event['text']}[/dim]")

        elif etype == "tool_call":
            icons = {
                "get_disk_overview":   "💾",
                "run_full_disk_scan":  "🔍",
                "scan_category":       "📂",
                "check_path_safety":   "🛡 ",
                "propose_cleanup_plan":"📋",
            }
            icon = icons.get(event["name"], "🔧")
            log.write(f"[yellow]{icon} {event['name']}()[/yellow]")

        elif etype == "tool_result":
            log.write(f"[dim]   └─ done[/dim]")

        elif etype == "plan_ready":
            log.write("\n[bold cyan]📋 Cleanup plan ready — review items on the right.[/bold cyan]")
            self._populate_table(event["plan"])
            self._refresh_disk_overview()

        elif etype == "message":
            text = event["text"].strip()
            log.write(f"\n[bold green]AI:[/bold green] {text}\n")

        elif etype == "error":
            err_text = event["text"]
            err_lower = err_text.lower()
            if "authentication" in err_lower or "api key" in err_lower or "unauthorized" in err_lower:
                hint = "\n[dim]  → Check your API key in .env (ANTHROPIC_API_KEY / OPENAI_API_KEY / OLLAMA_API_KEY / OPENAI_COMPAT_API_KEY)[/dim]"
            elif "connection refused" in err_lower or ("ollama" in err_lower and "connect" in err_lower):
                hint = "\n[dim]  → Is the model server running? Try [bold]ollama serve[/bold], or check OPENAI_COMPAT_BASE_URL in .env[/dim]"
            elif "no model" in err_lower or "model not found" in err_lower:
                hint = "\n[dim]  → Set PREFERRED_LOCAL_MODEL or PREFERRED_CLOUD_MODEL in .env[/dim]"
            else:
                hint = "\n[dim]  → See ~/.poofmac-audit.jsonl for details[/dim]"
            log.write(f"\n[red bold]❌ Error:[/red bold] {err_text}{hint}\n")

    # ── Table management ────────────────────────────────────────────────────────

    def _populate_table(self, plan: dict) -> None:
        table = self.query_one("#results-table", DataTable)
        table.clear()
        self.cleanup_items = plan.get("items", [])
        self.approvals = {}
        self.row_order = []
        self.plan_size_bytes = {}

        if not self.cleanup_items:
            log = self.query_one("#activity-log", RichLog)
            log.write(
                "\n[bold green]✓  Your Mac looks clean![/bold green]\n"
                "[dim]No cleanup candidates found. "
                "Try again after more usage or use a custom prompt.[/dim]\n"
            )
            return

        for i, item in enumerate(self.cleanup_items):
            row_key = f"item_{i}"
            self.row_order.append(row_key)

            risk = item.get("risk_level", "CAUTION")
            path_str = item.get("path", "")
            size_str = item.get("size_human", "?")
            category = item.get("category", "")
            reason = item.get("reason", "")

            # Parse a rough size for the summary
            self.plan_size_bytes[row_key] = self._parse_size(size_str)

            if risk == "SAFE":
                self.approvals[row_key] = True
                status = Text("✅ APPROVE", style="bold green")
                risk_text = Text("SAFE", style="green")
            elif risk == "SKIP":
                self.approvals[row_key] = False
                status = Text("⛔ SKIP", style="dim red")
                risk_text = Text("SKIP", style="red")
            else:
                self.approvals[row_key] = False
                status = Text("⚪ REVIEW", style="bold yellow")
                risk_text = Text("CAUTION", style="yellow")

            display_path = f"{path_str}  [dim]{reason[:60]}[/dim]" if reason else path_str

            table.add_row(
                status,
                category,
                size_str,
                risk_text,
                display_path,
                key=row_key,
            )

        self._update_execute_button()

    def _toggle_row(self, row_key: str) -> None:
        if row_key not in self.approvals:
            return
        item = self.cleanup_items[self.row_order.index(row_key)]
        risk = item.get("risk_level", "CAUTION")
        if risk == "SKIP":
            return  # Never approve SKIP items

        table = self.query_one("#results-table", DataTable)
        currently_approved = self.approvals[row_key]
        if currently_approved:
            self.approvals[row_key] = False
            table.update_cell(row_key, "status", Text("⚪ REVIEW", style="bold yellow"))
        else:
            self.approvals[row_key] = True
            table.update_cell(row_key, "status", Text("✅ APPROVE", style="bold green"))

        self._update_execute_button()

    def _update_execute_button(self) -> None:
        approved = sum(1 for v in self.approvals.values() if v)
        btn = self.query_one("#btn-execute", Button)
        if approved == 0 or self.safe_mode:
            btn.disabled = True
            label = "🗑  Execute (0 items)" if not self.safe_mode else "🛡  Safe Mode — No Deletions"
        else:
            btn.disabled = False
            total = sum(
                self.plan_size_bytes.get(k, 0)
                for k, v in self.approvals.items()
                if v
            )
            label = f"🗑  Execute ({approved} items, ~{format_size(total)})"
        btn.label = label

    @staticmethod
    def _parse_size(size_str: str) -> int:
        """Very rough parse of '8.2 GB' → bytes for display purposes only."""
        try:
            parts = size_str.strip().split()
            n = float(parts[0])
            unit = parts[1].upper() if len(parts) > 1 else "B"
            mult = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
            return int(n * mult.get(unit, 1))
        except (ValueError, IndexError):
            return 0

    # ── Execution worker ────────────────────────────────────────────────────────

    @work(thread=True)
    def _run_execution(self, items: list[dict]) -> None:
        log_updates: list[str] = []
        total_freed = 0

        for item in items:
            path = item.get("path", "")
            result = self.executor.delete(path, dry_run=False)
            if result.action == "deleted":
                total_freed += result.size_freed
                self.call_from_thread(
                    self._log_line,
                    f"[green]✅ Deleted {path} ({result.size_freed_human})[/green]",
                )
            elif result.action in ("blocked", "not_found"):
                self.call_from_thread(
                    self._log_line,
                    f"[red]🚫 Blocked: {path} — {result.error}[/red]",
                )
            elif result.action == "error":
                self.call_from_thread(
                    self._log_line,
                    f"[red]❌ Error: {path} — {result.error}[/red]",
                )

        self.call_from_thread(
            self._log_line,
            f"\n[bold green]🎉 Done! Freed approximately {format_size(total_freed)}.[/bold green]",
        )
        self.call_from_thread(
            self._log_line,
            f"[dim]Audit log: {self.audit.log_path}[/dim]\n",
        )
        self.call_from_thread(self._refresh_disk_overview)

    def _log_line(self, text: str) -> None:
        self.query_one("#activity-log", RichLog).write(text)

    # ── Actions ────────────────────────────────────────────────────────────────

    def action_scan(self) -> None:
        if self._scanning:
            return
        log = self.query_one("#activity-log", RichLog)
        log.write("\n[bold cyan]── Starting disk analysis ──[/bold cyan]")
        self._run_agent(
            "Please analyse my Mac's disk usage and identify what's taking "
            "up the most space. Show me a full cleanup plan."
        )

    def action_toggle_row(self) -> None:
        table = self.query_one("#results-table", DataTable)
        if not table.row_count or not self.row_order:
            return
        idx = table.cursor_row
        if 0 <= idx < len(self.row_order):
            self._toggle_row(self.row_order[idx])

    def action_approve_all_safe(self) -> None:
        table = self.query_one("#results-table", DataTable)
        for row_key, item in zip(self.row_order, self.cleanup_items):
            if item.get("risk_level") == "SAFE":
                self.approvals[row_key] = True
                table.update_cell(row_key, "status", Text("✅ APPROVE", style="bold green"))
        self._update_execute_button()

    def action_unapprove_all(self) -> None:
        table = self.query_one("#results-table", DataTable)
        for row_key, item in zip(self.row_order, self.cleanup_items):
            if item.get("risk_level") != "SKIP":
                self.approvals[row_key] = False
                table.update_cell(row_key, "status", Text("⚪ REVIEW", style="bold yellow"))
        self._update_execute_button()

    def action_execute(self) -> None:
        approved_items = [
            self.cleanup_items[i]
            for i, key in enumerate(self.row_order)
            if self.approvals.get(key)
        ]
        if not approved_items or self.safe_mode:
            return

        total = sum(
            self.plan_size_bytes.get(k, 0)
            for k, v in self.approvals.items()
            if v
        )

        def _on_confirmed(confirmed: bool) -> None:
            if confirmed:
                self._run_execution(approved_items)

        self.push_screen(
            ConfirmDeleteModal(len(approved_items), format_size(total)),
            _on_confirmed,
        )

    # ── Input / button handlers ────────────────────────────────────────────────

    @on(Input.Submitted, "#chat-input")
    def on_chat_submit(self, event: Input.Submitted) -> None:
        msg = event.value.strip()
        if not msg:
            return
        event.input.clear()
        if self._scanning:
            return
        log = self.query_one("#activity-log", RichLog)
        log.write(f"\n[bold]You:[/bold] {msg}")
        self._run_agent(msg)

    @on(Button.Pressed, "#btn-scan")
    def on_btn_scan(self) -> None:
        self.action_scan()

    @on(Button.Pressed, "#btn-execute")
    def on_btn_execute(self) -> None:
        self.action_execute()


# ── Entry point ───────────────────────────────────────────────────────────────


def run() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "tools":
        sys.exit(_run_tools_cli(sys.argv[2:]))

    parser = argparse.ArgumentParser(
        prog="poofmac",
        description="PoofMac — AI-powered Mac disk cleaner",
    )
    parser.add_argument(
        "--safe-mode",
        action="store_true",
        help="Scan and report only — disable all file deletions.",
    )
    parser.add_argument(
        "--tui",
        action="store_true",
        help="Use the Textual terminal UI instead of the desktop GUI.",
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Non-interactive Rich CLI — scan, print table, then chat.",
    )
    parser.add_argument(
        "--chat",
        action="store_true",
        help="AI chat mode — skip auto-scan and type what you want in plain English.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="(--cli only) Delete all SAFE items after displaying the plan.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="output_json",
        help="(--cli only) Print raw JSON output instead of the Rich table.",
    )
    parser.add_argument(
        "--model",
        metavar="MODEL",
        default=None,
        help="Override the AI model for this run (e.g. qwen3.6:35b-a3b).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"poofmac {__version__}",
    )
    args = parser.parse_args()

    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001
        print(f"[config error] {exc}", file=sys.stderr)
        sys.exit(1)

    if args.cli:
        # Non-interactive Rich CLI (pipe/script/SSH friendly)
        from mac_cleaner.cli import run_cli
        run_cli(
            settings,
            safe_mode=args.safe_mode,
            execute=args.execute,
            output_json=args.output_json,
            model_override=args.model,
        )
    elif args.chat:
        # AI chat mode — skip auto-scan, free-form conversation
        from mac_cleaner.cli import run_chat
        run_chat(settings, safe_mode=args.safe_mode, model_override=args.model)
    elif args.tui:
        # Textual interactive terminal UI
        app = MacCleanerApp(settings=settings, safe_mode=args.safe_mode)
        app.run()
    else:
        # Native desktop GUI (default)
        try:
            from mac_cleaner.gui import run_gui
        except ImportError:
            print(
                "PySide6 is not installed. Falling back to terminal UI.\n"
                "Install it with:  pip install 'poofmac[gui]'",
                file=sys.stderr,
            )
            app = MacCleanerApp(settings=settings, safe_mode=args.safe_mode)
            app.run()
            return
        run_gui(settings, safe_mode=args.safe_mode)


if __name__ == "__main__":
    run()
