# SPDX-License-Identifier: MIT
# Copyright (c) 2026 lesteroliver — https://poofmac.app
"""
PoofMac — Native PySide6 desktop GUI.

Follows Apple Human Interface Guidelines:
  • SF Pro / .AppleSystemUIFont — system font at correct sizes
  • HIG semantic colors — auto light / dark mode via QPalette
  • 8pt spacing grid throughout
  • Pill progress bar, risk chips, rounded controls
  • No custom-coloured header bars — inherits native window chrome

Window layout
─────────────
  ┌──────────────────────────────────────────────────────────────────┐
  │  PoofMac  (macOS title bar)                                      │
  ├──────────────────────────────────────────────────────────────────┤
  │  Model [gemma4:31b-cloud ▼]              ○ Safe Mode  ⚙         │  44px strip
  ├──────────────────────────────────────────────────────────────────┤
  │  Macintosh HD · 38.2 GB used · 189.8 GB free                     │
  │  ████████░░░░░░░░  20%                                           │  6px pill
  ├──────────────────────────────────────────────────────────────────┤
  │  Activity                 │  Cleanup Candidates                   │
  │  💾 overview()            │  ✓  App Caches    1.7 GB  ● Safe      │
  │     └ done                │  ○  Downloads     777 MB  ● Review    │
  │  📋 Plan ready            │  3 items · 2 selected · ~1.8 GB       │
  ├───────────────────────────┴──────────────────────────────────────┤
  │  ╭ Ask anything or press Scan… ╮  [ Scan ]  [ Clean 2 items ]    │
  └──────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import html
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QKeySequence,
    QPalette,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from mac_cleaner import store
from mac_cleaner.audit import AuditLogger
from mac_cleaner.config import (
    CUSTOM_ALIAS,
    CUSTOM_PREFIX,
    MODEL_REGISTRY,
    PROVIDER_ORDER,
    PROVIDER_SPECS,
    Settings,
    custom_endpoint_id,
    discover_models,
    is_custom_provider,
    provider_label,
)
from mac_cleaner.executor import Executor
from mac_cleaner.llm import CleanerAgent, explain_provider_error
from mac_cleaner.scanner import format_size, get_disk_usage

# ── Category visual metadata ──────────────────────────────────────────────────

CATEGORY_META: dict[str, tuple[str, str]] = {
    "Application Caches":          ("📦", "#5E5CE6"),  # indigo
    "Application & System Logs":   ("📋", "#30D158"),  # mint
    "Large Downloads":             ("📥", "#64D2FF"),  # sky
    "Xcode DerivedData":           ("🔨", "#FF9F0A"),  # orange
    "iOS Device Support Files":    ("📱", "#FF9F0A"),  # orange
    "watchOS Device Support Files":("⌚", "#BF5AF2"),  # purple
    "iOS/watchOS Simulators":      ("🖥", "#0A84FF"),  # blue
    "Development Artifacts":       ("⚙️",  "#FF6961"),  # coral
    "Homebrew Download Cache":     ("🍺", "#FF9F0A"),  # amber
    "Trash":                       ("🗑",  "#FF453A"),  # red
    "Docker":                      ("🐳", "#64D2FF"),  # blue
}

# ── Scan button spinner chars ─────────────────────────────────────────────────

_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

# ── Apple HIG theme system ────────────────────────────────────────────────────


@dataclass
class Theme:
    """Apple HIG semantic color values for one appearance (light or dark)."""

    window_bg: str
    panel_bg: str
    control_bg: str
    sidebar_bg: str
    toolbar_grad_start: str
    toolbar_grad_end: str
    text_primary: str
    text_secondary: str
    text_tertiary: str
    separator: str
    border: str
    accent: str
    green: str
    orange: str
    red: str
    green_bg: str
    orange_bg: str
    red_bg: str
    log_bg: str
    log_text: str
    is_dark: bool


LIGHT = Theme(
    window_bg="#ECECEC",
    panel_bg="#F5F5F7",
    control_bg="#FFFFFF",
    sidebar_bg="#F0F0F2",
    toolbar_grad_start="#E8EEFF",
    toolbar_grad_end="#F0F4FF",
    text_primary="rgba(0,0,0,0.85)",
    text_secondary="rgba(0,0,0,0.50)",
    text_tertiary="rgba(0,0,0,0.30)",
    separator="rgba(0,0,0,0.10)",
    border="rgba(0,0,0,0.12)",
    accent="#007AFF",
    green="#1A9E35",
    orange="#C56200",
    red="#D70015",
    green_bg="#D4F5DC",
    orange_bg="#FFE9CC",
    red_bg="#FFD5D8",
    log_bg="#FAFAFA",
    log_text="#1D1D1F",
    is_dark=False,
)

DARK = Theme(
    window_bg="#1E1E1E",
    panel_bg="#252528",
    control_bg="#323232",
    sidebar_bg="#2A2A2C",
    toolbar_grad_start="#1A1A2E",
    toolbar_grad_end="#1E2040",
    text_primary="rgba(255,255,255,0.88)",
    text_secondary="rgba(255,255,255,0.55)",
    text_tertiary="rgba(255,255,255,0.30)",
    separator="rgba(255,255,255,0.10)",
    border="rgba(255,255,255,0.12)",
    accent="#0A84FF",
    green="#32D74B",
    orange="#FF9F0A",
    red="#FF453A",
    green_bg="#0D3318",
    orange_bg="#3D2800",
    red_bg="#3D0A0A",
    log_bg="#1A1A1A",
    log_text="#E5E5EA",
    is_dark=True,
)


def _detect_dark() -> bool:
    """Return True when macOS is running in Dark Mode."""
    try:
        from PySide6.QtGui import QGuiApplication
        scheme = QGuiApplication.styleHints().colorScheme()
        return scheme == Qt.ColorScheme.Dark
    except Exception:
        pass
    palette = QApplication.palette()
    bg = palette.color(QPalette.ColorRole.Window)
    return bg.lightness() < 128


def get_theme() -> Theme:
    return DARK if _detect_dark() else LIGHT


def build_qss(t: Theme) -> str:
    """Generate a full QApplication stylesheet following Apple HIG."""
    accent_hover   = "#0071F0" if not t.is_dark else "#228AFF"
    accent_pressed = "#005ED6" if not t.is_dark else "#4A9FFF"
    btn_secondary_bg   = "rgba(0,0,0,0.06)" if not t.is_dark else "rgba(255,255,255,0.10)"
    btn_secondary_hover = "rgba(0,0,0,0.10)" if not t.is_dark else "rgba(255,255,255,0.15)"

    return f"""
/* ── Global ───────────────────────────────────────────────────────── */
QWidget {{
    font-family: ".AppleSystemUIFont", "SF Pro Text", "Helvetica Neue", sans-serif;
    font-size: 13px;
    color: {t.text_primary};
    background-color: {t.window_bg};
}}

/* ── Main window ──────────────────────────────────────────────────── */
QMainWindow {{
    background-color: {t.window_bg};
}}

/* ── Toolbar strip (gradient) ────────────────────────────────────── */
#toolbar {{
    background: qlineargradient(
        x1:0, y1:0, x2:1, y2:0,
        stop:0 {t.toolbar_grad_start},
        stop:1 {t.toolbar_grad_end}
    );
    border-bottom: 1px solid {t.separator};
    min-height: 44px;
    max-height: 44px;
}}
#toolbar QLabel {{
    color: {t.text_secondary};
    font-size: 12px;
    background: transparent;
}}
#toolbar QComboBox {{
    background-color: {t.control_bg};
    border: 1px solid {t.border};
    border-radius: 6px;
    padding: 4px 8px;
    min-width: 200px;
    font-size: 13px;
    color: {t.text_primary};
    selection-background-color: {t.accent};
}}
#toolbar QComboBox::drop-down {{
    width: 20px;
    border: none;
}}
#toolbar QComboBox QAbstractItemView {{
    background-color: {t.control_bg};
    border: 1px solid {t.border};
    border-radius: 6px;
    selection-background-color: {t.accent};
    selection-color: white;
    padding: 4px;
}}
#toolbar QCheckBox {{
    color: {t.text_secondary};
    font-size: 12px;
    background: transparent;
    spacing: 6px;
}}

/* ── Disk card ────────────────────────────────────────────────────── */
#disk_card {{
    background-color: {t.panel_bg};
    border-bottom: 1px solid {t.separator};
    padding: 12px 16px;
}}
#disk_title {{
    font-size: 13px;
    font-weight: 600;
    color: {t.text_primary};
    background: transparent;
}}
#disk_subtitle {{
    font-size: 11px;
    color: {t.text_secondary};
    background: transparent;
}}
#disk_pct {{
    font-size: 11px;
    font-weight: 600;
    color: {t.text_secondary};
    background: transparent;
}}

/* ── Summary banner ──────────────────────────────────────────────── */
#summary_banner {{
    background-color: {t.green_bg};
    color: {t.green};
    font-size: 12px;
    font-weight: 600;
    padding: 6px 16px;
    border-bottom: 1px solid {"rgba(26,158,53,0.20)" if not t.is_dark else "rgba(50,215,75,0.20)"};
}}

/* ── Progress bar (pill) ─────────────────────────────────────────── */
QProgressBar {{
    background-color: {t.separator};
    border: none;
    border-radius: 3px;
    max-height: 6px;
    min-height: 6px;
    text-align: center;
}}
QProgressBar::chunk {{
    border-radius: 3px;
    background-color: {t.accent};
}}

/* ── Section labels ──────────────────────────────────────────────── */
#section_label {{
    font-size: 11px;
    font-weight: 600;
    color: {t.text_secondary};
    letter-spacing: 0.5px;
    text-transform: uppercase;
    padding: 8px 16px 4px 16px;
    background-color: transparent;
}}

/* ── Activity log ────────────────────────────────────────────────── */
QTextBrowser {{
    background-color: {t.log_bg};
    color: {t.log_text};
    border: none;
    font-family: "SF Mono", "Menlo", "Monaco", "Courier New", monospace;
    font-size: 12px;
    padding: 8px 12px;
    selection-background-color: {t.accent};
}}

/* ── Splitter ────────────────────────────────────────────────────── */
QSplitter::handle:horizontal {{
    background-color: {t.separator};
    width: 1px;
}}

/* ── Table ───────────────────────────────────────────────────────── */
QTableWidget {{
    background-color: {t.control_bg};
    alternate-background-color: {"rgba(0,0,0,0.02)" if not t.is_dark else "rgba(255,255,255,0.02)"};
    gridline-color: transparent;
    border: none;
    selection-background-color: {"rgba(0,122,255,0.10)" if not t.is_dark else "rgba(10,132,255,0.15)"};
    selection-color: {t.text_primary};
    outline: none;
}}
QTableWidget::item {{
    padding: 0px 8px;
    border: none;
    color: {t.text_primary};
}}
QTableWidget::item:selected {{
    background-color: {"rgba(0,122,255,0.10)" if not t.is_dark else "rgba(10,132,255,0.15)"};
    color: {t.text_primary};
}}
QHeaderView {{
    background-color: {t.panel_bg};
    border: none;
    border-bottom: 1px solid {t.separator};
}}
QHeaderView::section {{
    background-color: {t.panel_bg};
    color: {t.text_secondary};
    font-size: 11px;
    font-weight: 600;
    padding: 0px 8px;
    border: none;
    border-right: 1px solid {t.separator};
    text-transform: uppercase;
    letter-spacing: 0.3px;
}}
QHeaderView::section:last {{
    border-right: none;
}}

/* ── Table footer ────────────────────────────────────────────────── */
#table_footer {{
    background-color: {t.panel_bg};
    border-top: 1px solid {t.separator};
    color: {t.text_secondary};
    font-size: 11px;
    padding: 4px 16px;
}}

/* ── Bottom input bar ────────────────────────────────────────────── */
#input_bar {{
    background-color: {t.panel_bg};
    border-top: 1px solid {t.separator};
    min-height: 56px;
    max-height: 56px;
}}

/* ── Credits bar ─────────────────────────────────────────────────── */
#credits_bar {{
    background-color: {t.panel_bg};
    border-top: 1px solid {t.separator};
    font-size: 11px;
    color: {t.text_secondary};
    padding: 3px 16px;
    min-height: 22px;
    max-height: 22px;
}}

/* ── Chat input ──────────────────────────────────────────────────── */
QLineEdit {{
    background-color: {t.control_bg};
    border: 1px solid {t.border};
    border-radius: 8px;
    padding: 6px 12px;
    font-size: 13px;
    color: {t.text_primary};
    min-height: 32px;
    max-height: 32px;
    selection-background-color: {t.accent};
}}
QLineEdit:focus {{
    border-color: {t.accent};
    border-width: 1.5px;
}}
QLineEdit::placeholder {{
    color: {t.text_tertiary};
}}

/* ── Primary button (accent blue) ───────────────────────────────── */
QPushButton[class="primary"] {{
    background-color: {t.accent};
    color: #FFFFFF;
    border: none;
    border-radius: 6px;
    padding: 5px 16px;
    font-size: 13px;
    font-weight: 500;
    min-height: 28px;
    max-height: 28px;
}}
QPushButton[class="primary"]:hover {{
    background-color: {accent_hover};
}}
QPushButton[class="primary"]:pressed {{
    background-color: {accent_pressed};
}}
QPushButton[class="primary"]:disabled {{
    background-color: {t.separator};
    color: {t.text_tertiary};
}}

/* ── Secondary button (plain) ────────────────────────────────────── */
QPushButton[class="secondary"] {{
    background-color: {btn_secondary_bg};
    color: {t.text_primary};
    border: 1px solid {t.border};
    border-radius: 6px;
    padding: 5px 16px;
    font-size: 13px;
    font-weight: 500;
    min-height: 28px;
    max-height: 28px;
}}
QPushButton[class="secondary"]:hover {{
    background-color: {btn_secondary_hover};
}}
QPushButton[class="secondary"]:disabled {{
    color: {t.text_tertiary};
    border-color: {t.separator};
}}

/* ── Destructive button (red) ────────────────────────────────────── */
QPushButton[class="destructive"] {{
    background-color: {t.red};
    color: #FFFFFF;
    border: none;
    border-radius: 6px;
    padding: 5px 16px;
    font-size: 13px;
    font-weight: 500;
    min-height: 28px;
    max-height: 28px;
}}
QPushButton[class="destructive"]:hover {{
    background-color: {"#E5001A" if not t.is_dark else "#FF6055"};
}}

/* ── Icon-only button (settings gear) ───────────────────────────── */
QPushButton[class="icon_btn"] {{
    background-color: transparent;
    color: {t.text_secondary};
    border: none;
    border-radius: 6px;
    padding: 4px 8px;
    font-size: 16px;
    min-height: 28px;
    max-height: 28px;
    min-width: 28px;
}}
QPushButton[class="icon_btn"]:hover {{
    background-color: {btn_secondary_bg};
}}

/* ── Dialog ──────────────────────────────────────────────────────── */
QDialog {{
    background-color: {t.window_bg};
}}

/* ── Tab widget ──────────────────────────────────────────────────── */
QTabWidget::pane {{
    border: 1px solid {t.border};
    border-radius: 6px;
    background-color: {t.panel_bg};
}}
QTabBar::tab {{
    background-color: transparent;
    color: {t.text_secondary};
    padding: 6px 16px;
    font-size: 13px;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{
    color: {t.accent};
    border-bottom: 2px solid {t.accent};
}}
QTabBar::tab:hover {{
    color: {t.text_primary};
}}

/* ── Scroll bars (minimal) ───────────────────────────────────────── */
QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {t.separator};
    border-radius: 4px;
    min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{
    background: {t.text_tertiary};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 8px;
}}
QScrollBar::handle:horizontal {{
    background: {t.separator};
    border-radius: 4px;
    min-width: 24px;
}}

/* ── Status bar ──────────────────────────────────────────────────── */
QStatusBar {{
    background-color: {t.panel_bg};
    color: {t.text_secondary};
    font-size: 11px;
    border-top: 1px solid {t.separator};
}}

/* ── Safe mode banner ────────────────────────────────────────────── */
#safe_banner {{
    background-color: {t.orange_bg};
    color: {t.orange};
    font-size: 12px;
    font-weight: 600;
    padding: 6px 16px;
    border-bottom: 1px solid {"rgba(197,98,0,0.20)" if not t.is_dark else "rgba(255,159,10,0.20)"};
}}

/* ── Empty state ─────────────────────────────────────────────────── */
#empty_state {{
    background-color: {t.control_bg};
    color: {t.text_tertiary};
    font-size: 13px;
    qproperty-alignment: AlignCenter;
}}
"""


def setup_theme(app: QApplication) -> Theme:
    """Detect dark/light mode and apply the HIG stylesheet to the app."""
    t = get_theme()
    app.setStyleSheet(build_qss(t))
    return t


# ── Tool icons & disclaimer text ─────────────────────────────────────────────

TOOL_ICONS = {
    "get_disk_overview":    "💾",
    "run_full_disk_scan":   "🔍",
    "scan_category":        "📂",
    "check_path_safety":    "🛡",
    "propose_cleanup_plan": "📋",
}

DISCLAIMER_HTML = """\
<p style="font-size:15px; font-weight:600; margin:0 0 12px 0;">
  Safety Disclaimer
</p>
<p style="margin:0 0 8px 0;">
  PoofMac uses an AI model to analyse your disk and suggest files
  for deletion. While it has multiple safety layers, software can have bugs.
</p>
<p style="font-weight:600; margin:0 0 4px 0;">By continuing you agree that:</p>
<ul style="margin:0 0 8px 0; padding-left:20px;">
  <li>You are responsible for reviewing every item before approving deletion.</li>
  <li>The authors are <b>not liable</b> for data loss or system instability.</li>
  <li>You have backups of important data (Time Machine, cloud, etc.).</li>
  <li>You will use <b>Safe Mode</b> if in doubt — it scans without deleting.</li>
</ul>
<p style="font-weight:600; margin:0 0 4px 0;">Never deleted automatically:</p>
<p style="margin:0 0 8px 0; font-family:monospace; font-size:12px;">
  /System &nbsp; /usr &nbsp; /bin &nbsp; ~/.ssh &nbsp; Keychain<br>
  ~/Documents &nbsp; ~/Photos &nbsp; ~/Music &nbsp; ~/Mail
</p>
<p style="font-size:11px; opacity:0.6; margin:0;">
  All paths above are hard-blocked in code — the LLM cannot override them.<br>
  PoofMac is open-source (MIT). Review safety.py before use.
</p>
"""


# ── Background workers ────────────────────────────────────────────────────────


class AgentWorker(QThread):
    """Runs CleanerAgent in a background thread; emits event dicts to the UI."""

    event_emitted = Signal(dict)

    def __init__(self, settings: Settings, message: str, agent: "CleanerAgent | None" = None) -> None:
        super().__init__()
        self.settings = settings
        self.message = message
        self._agent = agent  # reuse existing agent for chat continuity

    def run(self) -> None:
        try:
            if self._agent is None:
                self._agent = CleanerAgent(self.settings)
            for event in self._agent.run(self.message):
                self.event_emitted.emit(event)
        except Exception as exc:  # noqa: BLE001
            self.event_emitted.emit({"type": "error", "text": str(exc)})


class OpenAICompatModelsWorker(QThread):
    """Ask an endpoint which models it hosts, without blocking the GUI."""

    # (model ids, error) — the error is empty on success, and carries the
    # reason (HTTP 404, unreachable host, no ids) when there is nothing to show.
    models_ready = Signal(list, str)

    def __init__(self, base_url: str, api_key: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.base_url = base_url
        self.api_key = api_key

    def run(self) -> None:
        models, error = discover_models(self.base_url, self.api_key)
        self.models_ready.emit(models, error)


# Background threads must outlive the dialog that started them: the settings
# window can be closed while a sign-in or a test request is still in flight,
# and collecting a running QThread aborts the process.
_inflight_workers: list[QThread] = []


def _keep_alive(worker: QThread) -> None:
    """Park a background thread until it finishes."""
    if worker in _inflight_workers:
        return
    _inflight_workers.append(worker)

    def _drop() -> None:
        if worker in _inflight_workers:
            _inflight_workers.remove(worker)

    worker.finished.connect(_drop)


class EndpointTestWorker(QThread):
    """Send one tiny real request, so an endpoint's verdict is not a guess.

    Fetch models only proves that a listing answered. This proves the endpoint
    will serve the chosen model with the chosen key — the difference between
    "looks configured" and "works".
    """

    result = Signal(bool, str)

    def __init__(
        self, base_url: str, api_key: str, model: str, label: str
    ) -> None:
        super().__init__()
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.label = label

    def run(self) -> None:
        import time

        import litellm

        started = time.monotonic()
        try:
            litellm.completion(
                model=f"openai/{self.model}",
                messages=[{"role": "user", "content": "Reply with the single word: ok"}],
                api_base=self.base_url,
                api_key=self.api_key or "not-needed",
                max_tokens=5,
                temperature=0,
                timeout=20,
            )
        except Exception as exc:  # noqa: BLE001
            self.result.emit(
                False,
                explain_provider_error(
                    exc, model=self.model, display=self.label, is_custom_endpoint=True
                ),
            )
            return
        elapsed = time.monotonic() - started
        self.result.emit(
            True,
            f"✓ {self.label} answered in {elapsed:.1f}s — this endpoint works. "
            "Requests will be sent here.",
        )


class ExecutionWorker(QThread):
    """Runs file deletions in a background thread."""

    log_line = Signal(str)
    done = Signal(str)

    def __init__(self, executor: Executor, items: list[dict]) -> None:
        super().__init__()
        self.executor = executor
        self.items = items

    def run(self) -> None:
        total_freed = 0
        for item in self.items:
            path = item.get("path", "")
            result = self.executor.delete(path, dry_run=False)
            if result.action == "deleted":
                total_freed += result.size_freed
                self.log_line.emit(
                    f'<span style="color:#28CD41;">✓ Deleted '
                    f'<code>{path}</code> ({result.size_freed_human})</span>'
                )
            elif result.action in ("blocked", "not_found", "error"):
                self.log_line.emit(
                    f'<span style="color:#FF3B30;">✗ {result.error}</span>'
                )
        self.done.emit(f"Done — freed approximately {format_size(total_freed)}.")


# ── Helper widgets ────────────────────────────────────────────────────────────


def _make_pill(text: str, color: str, bg: str) -> QLabel:
    """Create a small colored pill label for the risk column."""
    pill = QLabel(text)
    pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
    pill.setStyleSheet(
        f"background-color: {bg}; color: {color};"
        " border-radius: 4px; padding: 2px 8px;"
        " font-size: 11px; font-weight: 600;"
        " font-family: '.AppleSystemUIFont', 'SF Pro Text', sans-serif;"
    )
    return pill


def _btn(text: str, cls: str) -> QPushButton:
    """Create a styled QPushButton with the given CSS class."""
    b = QPushButton(text)
    b.setProperty("class", cls)
    b.setFixedHeight(28)
    b.style().unpolish(b)
    b.style().polish(b)
    return b


def _section_label(text: str) -> QLabel:
    lbl = QLabel(text.upper())
    lbl.setObjectName("section_label")
    return lbl


def _h_separator(t: Theme) -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Plain)
    line.setStyleSheet(f"color: {t.separator}; margin: 0;")
    return line


# ── Dialogs ───────────────────────────────────────────────────────────────────


class DisclaimerDialog(QDialog):
    def __init__(self, t: Theme, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("PoofMac")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setFixedSize(520, 420)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 20)
        root.setSpacing(0)

        header = QHBoxLayout()
        header.setSpacing(12)
        icon = QLabel("💨")
        icon.setStyleSheet("font-size: 36px; background: transparent;")
        icon.setFixedSize(48, 48)
        header.addWidget(icon)

        title = QLabel("PoofMac")
        title.setStyleSheet(
            f"font-size: 17px; font-weight: 700; color: {t.text_primary}; background: transparent;"
        )
        header.addWidget(title)
        header.addStretch()
        root.addLayout(header)
        root.addSpacing(16)

        body = QTextBrowser()
        body.setHtml(DISCLAIMER_HTML)
        body.setReadOnly(True)
        body.setStyleSheet(
            f"background-color: {t.panel_bg}; border-radius: 8px;"
            f" border: 1px solid {t.border}; padding: 12px;"
            " font-size: 13px;"
        )
        body.setOpenExternalLinks(False)
        root.addWidget(body, stretch=1)
        root.addSpacing(8)

        # Author credit
        author_lbl = QLabel(
            'Made by <a href="https://github.com/lesteroliver911" style="color:#0A84FF;">lesteroliver</a>'
            ' · <a href="https://poofmac.app" style="color:#0A84FF;">poofmac.app</a>'
        )
        author_lbl.setOpenExternalLinks(True)
        author_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        author_lbl.setStyleSheet(f"font-size: 11px; color: {t.text_secondary}; background: transparent;")
        root.addWidget(author_lbl)
        root.addSpacing(8)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()

        exit_btn = _btn("Exit", "secondary")
        exit_btn.clicked.connect(self.reject)
        btn_row.addWidget(exit_btn)

        accept_btn = _btn("I Understand & Accept", "primary")
        accept_btn.setDefault(True)
        accept_btn.setFixedWidth(190)
        accept_btn.setEnabled(False)  # disabled until backup checkbox is ticked
        accept_btn.clicked.connect(self.accept)
        btn_row.addWidget(accept_btn)

        # Backup confirmation — must be checked before Accept is enabled
        backup_check = QCheckBox("I have a current backup (Time Machine or cloud backup)")
        backup_check.setStyleSheet(
            f"color: {t.text_secondary}; font-size: 13px; padding-top: 4px;"
        )
        backup_check.toggled.connect(accept_btn.setEnabled)
        root.addWidget(backup_check)
        root.addSpacing(8)

        root.addLayout(btn_row)


class ConfirmDialog(QDialog):
    def __init__(
        self,
        count: int,
        total_human: str,
        t: Theme,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Confirm Deletion")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setFixedSize(440, 200)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 20)
        root.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(12)
        icon = QLabel("🗑")
        icon.setStyleSheet("font-size: 28px; background: transparent;")
        icon.setFixedSize(40, 40)
        header.addWidget(icon)

        msg = QVBoxLayout()
        msg.setSpacing(2)
        title = QLabel(f"Delete {count} item{'' if count == 1 else 's'}?")
        title.setStyleSheet(
            f"font-size: 15px; font-weight: 700; color: {t.text_primary}; background: transparent;"
        )
        msg.addWidget(title)
        sub = QLabel(
            f"~{total_human} will be permanently removed. This cannot be undone."
        )
        sub.setStyleSheet(f"font-size: 12px; color: {t.text_secondary}; background: transparent;")
        sub.setWordWrap(True)
        msg.addWidget(sub)
        header.addLayout(msg)
        root.addLayout(header)

        audit_note = QLabel(
            "A full audit log will be written to ~/.poofmac-audit.jsonl"
        )
        audit_note.setStyleSheet(f"font-size: 11px; color: {t.text_tertiary}; background: transparent;")
        root.addWidget(audit_note)

        root.addStretch()

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()

        cancel_btn = _btn("Cancel", "secondary")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)

        del_btn = _btn(f"Delete {count} item{'' if count == 1 else 's'}", "destructive")
        del_btn.setDefault(True)
        del_btn.setFixedWidth(140)
        del_btn.clicked.connect(self.accept)
        btn_row.addWidget(del_btn)

        root.addLayout(btn_row)


class SignInWorker(QThread):
    """Run a provider CLI's sign-in flow and stream its output to the UI."""

    output = Signal(str)
    completed = Signal(bool)

    def __init__(self, command: list[str]) -> None:
        super().__init__()
        self.command = command

    def run(self) -> None:
        try:
            proc = subprocess.run(
                self.command, capture_output=True, text=True, timeout=300
            )
        except subprocess.TimeoutExpired:
            self.output.emit("Sign-in timed out after 5 minutes.")
            self.completed.emit(False)
            return
        except OSError as exc:  # includes FileNotFoundError
            self.output.emit(f"Could not run {self.command[0]}: {exc}")
            self.completed.emit(False)
            return
        for line in (proc.stdout + proc.stderr).splitlines():
            if line.strip():
                self.output.emit(line)
        self.completed.emit(proc.returncode == 0)


class SettingsDialog(QDialog):
    """Settings: pick a provider on the left, configure it on the right.

    Every provider in PROVIDER_ORDER gets its own row with its own model and
    credential fields, so nothing is hidden behind a second tab.
    """

    # "auto" first, then every real provider — the row order of the left list.
    # Custom endpoints are not rows: the "openai_compat" row is their manager,
    # because the user can have any number of them.
    ROWS = ["auto", *PROVIDER_ORDER]

    def __init__(self, settings: Settings, t: Theme, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.t = t
        # Set after a save when the active provider/model actually changed, so
        # the main window can drop its cached agent and pick up the new model.
        self.provider_changed = False
        self._model_combos: dict[str, QComboBox] = {}
        self._initial_models: dict[str, str] = {}
        self._key_edits: dict[str, QLineEdit] = {}
        self._signin: dict[str, dict] = {}
        self._models_worker: Optional[OpenAICompatModelsWorker] = None
        self._signin_worker: Optional[SignInWorker] = None
        # Custom endpoint page state
        self._endpoint_combo: Optional[QComboBox] = None
        self._endpoint_name_edit: Optional[QLineEdit] = None
        self._endpoint_base_edit: Optional[QLineEdit] = None
        self._endpoint_status: Optional[QLabel] = None
        self._endpoint_remove_btn: Optional[QPushButton] = None
        self._test_btn: Optional[QPushButton] = None
        self._test_worker: Optional[EndpointTestWorker] = None
        self._current_endpoint_id = ""
        self._endpoint_note = ""

        self.setWindowTitle("PoofMac Settings")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumSize(780, 580)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 16)
        root.setSpacing(12)

        title = QLabel("Settings")
        title.setStyleSheet(
            f"font-size: 17px; font-weight: 700; color: {t.text_primary}; background: transparent;"
        )
        root.addWidget(title)

        tabs = QTabWidget()
        tabs.addTab(self._build_providers_tab(), "Providers")
        tabs.addTab(self._build_safety_tab(), "Safety")
        root.addWidget(tabs, stretch=1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close_btn = _btn("Close", "secondary")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        save_btn = _btn("Save & Close", "primary")
        save_btn.setFixedWidth(130)
        save_btn.clicked.connect(self._save_and_close)
        btn_row.addWidget(save_btn)
        root.addLayout(btn_row)

    # ── Providers tab ──────────────────────────────────────────────────────────

    def _build_providers_tab(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(16)

        sidebar = QWidget()
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(6)
        side.addWidget(_section_label("Provider"))

        self._provider_list = QListWidget()
        self._provider_list.setFixedWidth(210)
        self._provider_list.addItem("Automatic")
        for provider_id in PROVIDER_ORDER:
            self._provider_list.addItem(
                "Custom endpoints"
                if provider_id == CUSTOM_ALIAS
                else PROVIDER_SPECS[provider_id]["label"]
            )
        side.addWidget(self._provider_list)

        self._provider_status = QLabel()
        self._provider_status.setWordWrap(True)
        self._provider_status.setStyleSheet(
            f"font-size: 11px; color: {self.t.text_tertiary}; background: transparent;"
        )
        side.addWidget(self._provider_status)
        side.addStretch()
        layout.addWidget(sidebar)

        self._detail_stack = QStackedWidget()
        for provider_id in self.ROWS:
            self._detail_stack.addWidget(self._build_provider_page(provider_id))
        layout.addWidget(self._detail_stack, stretch=1)

        # Open on whatever the user actually has selected, so the page they
        # need is already showing. A custom:<id> selection belongs to the
        # custom endpoints page.
        active = self.settings.active_provider
        if is_custom_provider(active):
            active = CUSTOM_ALIAS
        row = self.ROWS.index(active) if active in self.ROWS else 0
        self._provider_list.currentRowChanged.connect(self._on_provider_row_changed)
        self._provider_list.setCurrentRow(row)
        return page

    def _build_provider_page(self, provider_id: str) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(10)

        if provider_id == "auto":
            note = QLabel(
                "Automatic picks the first provider that is fully configured, in this "
                "order:\n\n"
                "Anthropic → OpenRouter → OpenAI → Custom endpoint → "
                "Ollama Cloud → Ollama Local.\n\n"
                "Choose a provider on the left to pin it explicitly."
            )
            note.setWordWrap(True)
            note.setStyleSheet(
                f"font-size: 12px; color: {self.t.text_secondary}; background: transparent;"
            )
            layout.addWidget(note)
            layout.addStretch()
            return page

        spec = PROVIDER_SPECS[provider_id]

        if spec["auth"] == "endpoint":
            # The whole page is an endpoint manager — see _build_endpoint_page.
            return self._build_endpoint_page(provider_id, spec)

        if spec["key_env"]:
            layout.addWidget(QLabel(f"{spec['label']} API key"))
            edit = QLineEdit(self.settings.api_key_for(provider_id))
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            edit.setPlaceholderText(spec["key_placeholder"])
            self._key_edits[provider_id] = edit
            layout.addWidget(edit)

        layout.addWidget(QLabel("Model"))
        model_row = QHBoxLayout()
        combo = QComboBox()
        combo.setEditable(True)
        for model_id, label in MODEL_REGISTRY.get(spec["registry"], []):
            combo.addItem(f"{model_id}  —  {label}", model_id)
        # Show what would actually be used, but only persist a value the user
        # picks — otherwise saving would freeze a legacy shared model onto
        # whichever provider happened to be displaying it.
        current = self.settings.model_for(provider_id)
        combo.setCurrentText(current)
        self._model_combos[provider_id] = combo
        self._initial_models[provider_id] = current
        model_row.addWidget(combo, stretch=1)
        layout.addLayout(model_row)

        if spec["auth"] == "signin":
            layout.addWidget(self._build_signin_block(provider_id, spec))

        layout.addStretch()
        return page

    def _build_endpoint_page(self, provider_id: str, spec: dict) -> QWidget:
        """Manager for the user's custom OpenAI-compatible endpoints.

        One row here is one saved endpoint — its own name, base URL, key and the
        models it hosts. Switching rows swaps every field, so a local server and
        a work gateway can coexist without re-typing anything.
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(10)

        intro = QLabel(
            "Any service that speaks the OpenAI chat-completions API: LM Studio, "
            "vLLM, llama.cpp, Ollama's /v1, Groq, a company gateway. Add as many as "
            "you like — each keeps its own URL, key and model list."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(
            f"font-size: 12px; color: {self.t.text_secondary}; background: transparent;"
        )
        layout.addWidget(intro)

        pick_row = QHBoxLayout()
        self._endpoint_combo = QComboBox()
        self._endpoint_combo.setToolTip("Saved endpoints — pick one to edit")
        self._endpoint_combo.currentIndexChanged.connect(self._on_endpoint_selected)
        pick_row.addWidget(self._endpoint_combo, stretch=1)

        new_btn = _btn("＋ New", "secondary")
        new_btn.setToolTip("Start a new endpoint")
        new_btn.clicked.connect(self._new_endpoint)
        pick_row.addWidget(new_btn)

        self._endpoint_remove_btn = _btn("Remove", "secondary")
        self._endpoint_remove_btn.clicked.connect(self._remove_endpoint)
        pick_row.addWidget(self._endpoint_remove_btn)
        layout.addLayout(pick_row)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(8)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self._endpoint_name_edit = QLineEdit()
        self._endpoint_name_edit.setPlaceholderText("Nex AGI · LM Studio · work gateway")
        form.addRow("Name", self._endpoint_name_edit)

        self._endpoint_base_edit = QLineEdit()
        self._endpoint_base_edit.setPlaceholderText(
            "http://localhost:11434/v1  ·  http://localhost:1234/v1  ·  "
            "https://api.groq.com/openai/v1"
        )
        form.addRow("Base URL", self._endpoint_base_edit)

        key_edit = QLineEdit()
        key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        key_edit.setPlaceholderText(spec["key_placeholder"])
        self._key_edits[provider_id] = key_edit
        form.addRow("API key", key_edit)

        model_row = QHBoxLayout()
        combo = QComboBox()
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        combo.setPlaceholderText("model id, exactly as the endpoint lists it")
        self._model_combos[provider_id] = combo
        self._initial_models[provider_id] = ""
        model_row.addWidget(combo, stretch=1)

        fetch_btn = _btn("Fetch models", "secondary")
        fetch_btn.setToolTip("Ask this endpoint which models it hosts")
        fetch_btn.clicked.connect(self._fetch_compat_models)
        model_row.addWidget(fetch_btn)
        form.addRow("Model", model_row)
        layout.addLayout(form)

        self._endpoint_status = QLabel()
        self._endpoint_status.setWordWrap(True)
        layout.addWidget(self._endpoint_status)

        action_row = QHBoxLayout()
        action_row.addStretch()
        self._test_btn = _btn("Test connection", "secondary")
        self._test_btn.setToolTip(
            "Send one small real request — proves the URL, key and model work"
        )
        self._test_btn.clicked.connect(self._test_endpoint)
        action_row.addWidget(self._test_btn)

        save_btn = _btn("Save endpoint", "primary")
        save_btn.setToolTip("Keep these details for reuse")
        save_btn.clicked.connect(self._save_endpoint_clicked)
        action_row.addWidget(save_btn)
        layout.addLayout(action_row)

        # Live feedback: the status line reacts as fields are typed, so the page
        # is never silently wrong about what will be used.
        for widget in (self._endpoint_name_edit, self._endpoint_base_edit, key_edit):
            widget.textChanged.connect(self._refresh_endpoint_status)
        combo.currentTextChanged.connect(self._refresh_endpoint_status)

        self._reload_endpoints()
        layout.addStretch()
        return page

    def _build_signin_block(self, provider_id: str, spec: dict) -> QWidget:
        block = QWidget()
        layout = QVBoxLayout(block)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(6)

        btn = _btn(f"Sign in with {spec['cli']}", "primary")
        btn.clicked.connect(lambda _checked=False, p=provider_id: self._start_signin(p))
        layout.addWidget(btn)

        hint = QLabel(
            "Signs in through the command line. PoofMac uses the saved credentials, "
            "so you only have to do this once."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(
            f"font-size: 11px; color: {self.t.text_tertiary}; background: transparent;"
        )
        layout.addWidget(hint)

        output = QTextBrowser()
        output.setMaximumHeight(120)
        output.setVisible(False)
        layout.addWidget(output)

        self._signin[provider_id] = {"button": btn, "output": output}
        return block

    # ── Provider selection ─────────────────────────────────────────────────────

    @property
    def _selected_provider(self) -> str:
        return self.ROWS[self._provider_list.currentRow()]

    def _on_provider_row_changed(self, _row: int) -> None:
        self._detail_stack.setCurrentIndex(self._provider_list.currentRow())
        self._refresh_provider_status()

    def _refresh_provider_status(self) -> None:
        """One line telling the user whether the highlighted provider is usable."""
        selected = self._selected_provider
        # On the custom page, report the endpoint being edited rather than the
        # last saved one, so the sidebar cannot contradict the page.
        if selected == CUSTOM_ALIAS and self._endpoint_status is not None:
            ok, message = Settings.endpoint_status(self._endpoint_from_widgets())
            self._provider_status.setText(
                f"Custom endpoints — {'✓' if ok else '⚠'} {message}"
            )
            return
        provider_id = (
            self.settings.get_active_provider() if selected == "auto" else selected
        )
        if is_custom_provider(provider_id):
            ok, message = self.settings.provider_status(provider_id)
            self._provider_status.setText(
                f"{provider_label(provider_id)} — {'✓' if ok else '⚠'} {message}"
            )
            return
        if provider_id not in PROVIDER_SPECS:
            self._provider_status.setText("No provider is configured yet.")
            return
        ok, message = self.settings.provider_status(provider_id)
        label = PROVIDER_SPECS[provider_id]["label"]
        where = f"Automatic resolves to {label}" if selected == "auto" else label
        self._provider_status.setText(f"{where} — {'✓' if ok else '⚠'} {message}")

    # ── Custom endpoints: save, fetch and remove ──────────────────────────────

    def _reload_endpoints(self, select: str = "") -> None:
        """Rebuild the endpoint picker from disk, keeping a sensible selection."""
        if self._endpoint_combo is None:
            return
        endpoints = store.list_endpoints(refresh=True)
        wanted = select
        if is_custom_provider(wanted):
            wanted = custom_endpoint_id(wanted)
        elif wanted == CUSTOM_ALIAS:
            wanted = ""
        if not wanted:
            wanted = self._current_endpoint_id or store.active_endpoint_id()
        ids = [endpoint["id"] for endpoint in endpoints]
        if wanted not in ids:
            wanted = ids[0] if ids else ""

        combo = self._endpoint_combo
        combo.blockSignals(True)
        combo.clear()
        for endpoint in endpoints:
            combo.addItem(
                f"{endpoint['name']}  —  {store.host_label(endpoint['base_url'])}",
                endpoint["id"],
            )
        combo.addItem("＋ New endpoint…", "")
        index = combo.findData(wanted)
        combo.setCurrentIndex(index if index >= 0 else combo.count() - 1)
        combo.blockSignals(False)
        self._load_endpoint_into_widgets(wanted)

    def _load_endpoint_into_widgets(self, endpoint_id: str) -> None:
        endpoint = store.find_endpoint(endpoint_id) if endpoint_id else None
        self._current_endpoint_id = endpoint["id"] if endpoint else ""
        self._endpoint_note = ""
        self._endpoint_name_edit.setText(endpoint["name"] if endpoint else "")
        self._endpoint_base_edit.setText(endpoint["base_url"] if endpoint else "")
        self._key_edits["openai_compat"].setText(endpoint["api_key"] if endpoint else "")
        combo = self._model_combos["openai_compat"]
        combo.blockSignals(True)
        combo.clear()
        for model_id in (endpoint["models"] if endpoint else []):
            combo.addItem(model_id, model_id)
        combo.setCurrentText(endpoint["model"] if endpoint else "")
        combo.blockSignals(False)
        if self._endpoint_remove_btn is not None:
            self._endpoint_remove_btn.setEnabled(endpoint is not None)
        self._refresh_endpoint_status()

    def _endpoint_from_widgets(self) -> dict:
        """The endpoint the form currently describes, saved or not."""
        combo = self._model_combos["openai_compat"]
        draft = store.new_endpoint(
            name=self._endpoint_name_edit.text(),
            base_url=self._endpoint_base_edit.text(),
            api_key=self._key_edits["openai_compat"].text(),
            model=combo.currentText(),
            models=[combo.itemText(i) for i in range(combo.count())],
        )
        draft["id"] = self._current_endpoint_id
        return draft

    def _refresh_endpoint_status(self, *_args: Any) -> None:
        if self._endpoint_status is None:
            return
        draft = self._endpoint_from_widgets()
        ok, message = Settings.endpoint_status(draft)
        count = len(draft["models"])
        summary = (
            f"{count} model{'s' if count != 1 else ''} known for this endpoint"
            if count
            else "no models listed yet — press Fetch models"
        )
        text = f"{'✓' if ok else '⚠'} {message} · {summary}"
        if self._endpoint_note:
            text = f"{text}\n{self._endpoint_note}"
        self._endpoint_status.setText(text)
        self._endpoint_status.setStyleSheet(
            "font-size: 11px; background: transparent;"
            f"color: {self.t.green if ok else self.t.orange};"
        )

    def _commit_endpoint(self, quiet: bool = False) -> Optional[dict]:
        """Persist the endpoint the form describes, if it is usable."""
        draft = self._endpoint_from_widgets()
        if not draft["base_url"] or not draft["model"]:
            if not quiet:
                missing = "Base URL" if not draft["base_url"] else "model id"
                QMessageBox.warning(
                    self,
                    "Nothing saved yet",
                    f"Add the endpoint's {missing} first.\n\n"
                    "Base URL example:  http://localhost:11434/v1\n"
                    "Press Fetch models to list the ids this endpoint hosts.",
                )
            return None
        try:
            saved = store.upsert_endpoint(draft, make_active=True)
        except OSError as exc:
            if not quiet:
                QMessageBox.warning(
                    self,
                    "Could not save endpoint",
                    f"{exc}\n\nPoofMac tried to write:\n{store.endpoints_file()}",
                )
            return None
        if saved is not None:
            self._current_endpoint_id = saved["id"]
        return saved

    def _on_endpoint_selected(self, index: int) -> None:
        if self._endpoint_combo is None:
            return
        target = self._endpoint_combo.itemData(index) or ""
        if not target:
            self._new_endpoint()
            return
        # Persist the row being left, so switching endpoints never loses edits.
        self._commit_endpoint(quiet=True)
        self._reload_endpoints(select=target)

    def _new_endpoint(self, *_args: Any) -> None:
        """Clear the form for a new endpoint, keeping any complete draft."""
        self._commit_endpoint(quiet=True)
        self._current_endpoint_id = ""
        self._endpoint_note = ""
        self._endpoint_name_edit.clear()
        self._endpoint_base_edit.clear()
        self._key_edits["openai_compat"].clear()
        combo = self._model_combos["openai_compat"]
        combo.blockSignals(True)
        combo.clear()
        combo.setCurrentText("")
        combo.blockSignals(False)
        if self._endpoint_combo is not None:
            self._endpoint_combo.blockSignals(True)
            self._endpoint_combo.setCurrentIndex(self._endpoint_combo.count() - 1)
            self._endpoint_combo.blockSignals(False)
        if self._endpoint_remove_btn is not None:
            self._endpoint_remove_btn.setEnabled(False)
        self._endpoint_base_edit.setFocus()
        self._refresh_endpoint_status()

    def _save_endpoint_clicked(self) -> None:
        saved = self._commit_endpoint(quiet=False)
        if saved is None:
            return
        self._reload_endpoints(select=saved["id"])
        self._endpoint_note = (
            f"✓ Saved “{saved['name']}”. Press Save & Close to use it right away."
        )
        self._refresh_endpoint_status()
        self._refresh_provider_status()

    def _remove_endpoint(self) -> None:
        endpoint_id = self._current_endpoint_id
        if not endpoint_id:
            return
        endpoint = store.find_endpoint(endpoint_id) or {}
        name = endpoint.get("name") or store.host_label(endpoint.get("base_url", ""))
        answer = QMessageBox.question(
            self,
            "Remove endpoint",
            f"Remove “{name}”?\n\nIts URL, key and model list are deleted from this Mac.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            remaining = store.remove_endpoint(endpoint_id)
        except OSError as exc:
            QMessageBox.warning(self, "Could not remove endpoint", str(exc))
            return
        if is_custom_provider(self.settings.active_provider) or (
            self.settings.active_provider == CUSTOM_ALIAS
        ):
            self.settings.active_provider = CUSTOM_ALIAS if remaining else "auto"
        self._current_endpoint_id = ""
        self._reload_endpoints(select=remaining[0]["id"] if remaining else "")
        self._refresh_provider_status()

    def _fetch_compat_models(self) -> None:
        base_url = self._endpoint_base_edit.text().strip()
        if not base_url:
            self._endpoint_status.setText("⚠ Add a Base URL first, then press Fetch models.")
            return
        if self._models_worker is not None and self._models_worker.isRunning():
            return
        self._endpoint_status.setText(f"Contacting {store.host_label(base_url)}…")
        worker = OpenAICompatModelsWorker(
            base_url, self._key_edits["openai_compat"].text(), self
        )
        self._models_worker = worker
        worker.models_ready.connect(self._set_compat_models)
        worker.start()

    def _set_compat_models(self, models: list, error: str) -> None:
        """Fold a models listing (or the reason there is none) into the page."""
        if self._endpoint_status is None:
            return
        combo = self._model_combos["openai_compat"]
        current = combo.currentText().strip()
        host = store.host_label(self._endpoint_base_edit.text())
        if not models:
            self._endpoint_note = (
                f"⚠ {error or 'The endpoint returned no models'}.\n"
                "Type the model id exactly as the endpoint lists it, then press "
                "Save endpoint."
            )
            self._refresh_endpoint_status()
            return
        combo.blockSignals(True)
        combo.clear()
        for model_id in models:
            combo.addItem(model_id, model_id)
        if current and current not in models:
            # A hand-typed id stays selectable — /models is often incomplete.
            combo.addItem(current, current)
            combo.setCurrentText(current)
        else:
            combo.setCurrentText(current or models[0])
        combo.blockSignals(False)

        plural = "s" if len(models) != 1 else ""
        self._endpoint_note = f"✓ {len(models)} model{plural} found on {host}"
        saved = self._commit_endpoint(quiet=True)
        if saved is not None:
            self._endpoint_note += f" — saved as “{saved['name']}”."
        self._refresh_endpoint_status()
        self._refresh_provider_status()

    def _test_endpoint(self) -> None:
        """Ask the endpoint to serve the selected model, once."""
        draft = self._endpoint_from_widgets()
        ok, message = Settings.endpoint_status(draft)
        if not ok:
            self._endpoint_status.setText(f"⚠ {message} — fill that in before testing.")
            return
        if self._test_worker is not None and self._test_worker.isRunning():
            return
        label = draft["name"] or store.host_label(draft["base_url"])
        self._endpoint_note = ""
        self._endpoint_status.setText(
            f"Testing {label} — sending one small request to “{draft['model']}”…"
        )
        if self._test_btn is not None:
            self._test_btn.setEnabled(False)
        worker = EndpointTestWorker(
            draft["base_url"], draft["api_key"], draft["model"], f"{draft['model']} ({label})"
        )
        self._test_worker = worker
        _keep_alive(worker)
        worker.result.connect(self._on_test_finished)
        worker.start()

    def _on_test_finished(self, ok: bool, message: str) -> None:
        if self._test_btn is not None:
            self._test_btn.setEnabled(True)
        self._endpoint_note = message
        self._refresh_endpoint_status()

    # ── CLI sign-in ────────────────────────────────────────────────────────────

    def _start_signin(self, provider_id: str) -> None:
        spec = PROVIDER_SPECS[provider_id]
        widgets = self._signin[provider_id]
        executable = shutil.which(spec["cli"])
        output = widgets["output"]
        output.setVisible(True)
        if not executable:
            output.setPlainText(
                f"{spec['cli']} is not installed.\n\nInstall it, then sign in again:\n"
                f"  {spec['install']}"
            )
            return
        widgets["button"].setEnabled(False)
        output.setPlainText(f"Running {executable} login — follow the instructions below.")
        self._signin_worker = SignInWorker([executable, "login"])
        _keep_alive(self._signin_worker)
        self._signin_worker.output.connect(output.append)
        self._signin_worker.completed.connect(
            lambda ok, p=provider_id: self._on_signin_finished(p, ok)
        )
        self._signin_worker.start()

    def _on_signin_finished(self, provider_id: str, ok: bool) -> None:
        widgets = self._signin[provider_id]
        widgets["button"].setEnabled(True)
        widgets["output"].append(
            f"\n{'✓ Signed in.' if ok else '✗ Sign-in did not complete.'}"
        )
        self._refresh_provider_status()

    def _build_safety_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        self._safe_mode_check = QCheckBox("Safe Mode (scan only — no files will be deleted)")
        self._safe_mode_check.setChecked(self.settings.safe_mode)
        layout.addWidget(self._safe_mode_check)

        desc = QLabel(
            "When Safe Mode is on, PoofMac will analyse your disk and show you the cleanup "
            "plan, but the Clean button is disabled. No files can be deleted in any way.\n\n"
            "Recommended for first-time users and when sharing access with others."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(
            f"font-size: 12px; color: {self.t.text_secondary}; background: transparent;"
        )
        layout.addWidget(desc)
        layout.addStretch()
        return w

    def closeEvent(self, event) -> None:
        # The sign-in flow can wait on a browser for minutes; never block the
        # close on it. Model discovery is bounded, so waiting on it is safe.
        if self._models_worker and self._models_worker.isRunning():
            self._models_worker.wait()
        super().closeEvent(event)

    # ── Save ───────────────────────────────────────────────────────────────────

    def _save_and_close(self) -> None:
        try:
            prev_active = self.settings.get_active_model()
        except RuntimeError:
            prev_active = None

        self._apply_widgets_to_settings()

        provider_id = self.settings.active_provider
        if provider_id != "auto":
            ok, message = self.settings.provider_status(provider_id)
            if not ok:
                QMessageBox.warning(
                    self,
                    f"{provider_label(provider_id)} is not ready",
                    f"{message}\n\nFix it, or pick a different provider.",
                )
                return

        try:
            # One writer, one location: store.env_file() is a per-user path the
            # bundled app can always write to (its working directory is "/").
            values: dict[str, Any] = {
                "ACTIVE_PROVIDER": provider_id,
                "SAFE_MODE": str(self._safe_mode_check.isChecked()).lower(),
            }
            for pid, spec in PROVIDER_SPECS.items():
                if spec["auth"] == "endpoint":
                    continue  # custom endpoints live in endpoints.json
                # Write the raw per-provider value, not model_for(): an unset
                # provider must stay empty rather than inherit the legacy
                # shared key, or the next save would pin it to someone else's
                # model.
                model = getattr(self.settings, spec["model_attr"], "")
                if spec["model_env"] and model:
                    values[spec["model_env"]] = model
                if spec["key_env"]:
                    values[spec["key_env"]] = self.settings.api_key_for(pid)
            written = store.write_env_values(values)
            self.settings.safe_mode = self._safe_mode_check.isChecked()

            new_active = self.settings.get_active_model()
            self.provider_changed = prev_active != new_active
            self._saved_to = written
        except Exception as exc:  # noqa: BLE001
            # Settings still applied in memory — surface it instead of failing
            # silently, then let the user close the dialog normally.
            QMessageBox.warning(
                self,
                "Could not save settings",
                f"{exc}\n\nPoofMac tried to write:\n{store.env_file()}",
            )

        self.accept()

    def _apply_widgets_to_settings(self) -> None:
        """Copy every widget into Settings, then validate the chosen provider."""
        # The custom page edits a record in the endpoint store rather than a
        # Settings attribute, so it is committed first.
        self._commit_endpoint(quiet=True)
        for pid, edit in self._key_edits.items():
            if pid == CUSTOM_ALIAS:
                continue
            self.settings.set_api_key(pid, edit.text().strip())
        for pid, combo in self._model_combos.items():
            if pid == CUSTOM_ALIAS:
                continue
            model = combo.currentText().strip()
            if model and model != self._initial_models.get(pid, ""):
                self.settings.set_model_for(pid, model)
        provider_id = self._selected_provider
        if provider_id == CUSTOM_ALIAS and self._current_endpoint_id:
            # Pin the endpoint the user was just working on, not the alias.
            provider_id = f"{CUSTOM_PREFIX}{self._current_endpoint_id}"
        self.settings.active_provider = provider_id


# ── Table column indices ──────────────────────────────────────────────────────

COL_CHECK    = 0
COL_CATEGORY = 1
COL_SIZE     = 2
COL_RISK     = 3
COL_PATH     = 4


# ── Main window ───────────────────────────────────────────────────────────────


class PoofMacWindow(QMainWindow):
    def __init__(self, settings: Settings, t: Theme, safe_mode: bool = False) -> None:
        super().__init__()
        self.settings  = settings
        self.t         = t
        self.safe_mode = safe_mode or settings.safe_mode
        self.audit     = AuditLogger()
        self.executor  = Executor(safe_mode=self.safe_mode, audit=self.audit)

        self.cleanup_items: list[dict] = []
        self._worker: Optional[AgentWorker] = None
        self._exec_worker: Optional[ExecutionWorker] = None
        self._scanning = False
        self._spinner_idx = 0
        self._spinner_timer = QTimer(self)
        self._spinner_timer.timeout.connect(self._tick_spinner)
        # Persistent chat agent — reused across follow-up messages so the AI
        # remembers previous context ("now delete those", "what about logs?")
        self._chat_agent: Optional[CleanerAgent] = None
        self._logged_status = False

        self.setWindowTitle("PoofMac")
        self.setMinimumSize(960, 640)
        self.resize(1120, 740)

        self._build_ui()
        self._refresh_disk_overview()
        self._populate_model_picker()
        self._setup_shortcuts()

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_toolbar())

        if self.safe_mode:
            banner = QLabel("  🛡  SAFE MODE — Scanning only. No files will be deleted.")
            banner.setObjectName("safe_banner")
            banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
            root.addWidget(banner)

        root.addWidget(self._build_disk_card())

        # Summary banner (hidden until scan completes)
        self._summary_banner = QLabel("")
        self._summary_banner.setObjectName("summary_banner")
        self._summary_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._summary_banner.setVisible(False)
        root.addWidget(self._summary_banner)

        root.addWidget(self._build_content(), stretch=1)
        root.addWidget(self._build_input_bar())

        # Credits bar
        credits_bar = QLabel(
            'Made by <a href="https://github.com/lesteroliver911" style="color:#0A84FF;">lesteroliver</a>'
            ' &nbsp;·&nbsp; '
            '<a href="https://linkedin.com/in/lesteroliver" style="color:#0A84FF;">LinkedIn</a>'
            ' &nbsp;·&nbsp; '
            '<a href="https://poofmac.app" style="color:#0A84FF;">poofmac.app</a>'
        )
        credits_bar.setOpenExternalLinks(True)
        credits_bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        credits_bar.setObjectName("credits_bar")
        root.addWidget(credits_bar)

    # ── Toolbar ────────────────────────────────────────────────────────────────

    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("toolbar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(8)

        model_lbl = QLabel("Model")
        layout.addWidget(model_lbl)

        self.model_combo = QComboBox()
        self.model_combo.setToolTip("Select the AI model for disk analysis")
        self.model_combo.currentTextChanged.connect(self._on_model_changed)
        layout.addWidget(self.model_combo)

        layout.addStretch()

        self.safe_check = QCheckBox("Safe Mode")
        self.safe_check.setChecked(self.safe_mode)
        self.safe_check.setToolTip("Scan and report only — no files will be deleted")
        self.safe_check.toggled.connect(self._on_safe_mode_toggled)
        layout.addWidget(self.safe_check)

        settings_btn = _btn("⚙", "icon_btn")
        settings_btn.setToolTip("Settings — API keys, models, safety")
        settings_btn.clicked.connect(self._open_settings)
        layout.addWidget(settings_btn)
        return bar

    # ── Disk card ──────────────────────────────────────────────────────────────

    def _build_disk_card(self) -> QWidget:
        card = QWidget()
        card.setObjectName("disk_card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(6)

        top_row = QHBoxLayout()
        top_row.setSpacing(0)

        self.disk_title_lbl = QLabel("Checking disk…")
        self.disk_title_lbl.setObjectName("disk_title")
        top_row.addWidget(self.disk_title_lbl)
        top_row.addStretch()
        self.disk_pct_lbl = QLabel("")
        self.disk_pct_lbl.setObjectName("disk_pct")
        top_row.addWidget(self.disk_pct_lbl)
        layout.addLayout(top_row)

        self.disk_bar = QProgressBar()
        self.disk_bar.setMinimum(0)
        self.disk_bar.setMaximum(100)
        self.disk_bar.setTextVisible(False)
        layout.addWidget(self.disk_bar)

        self.disk_sub_lbl = QLabel("")
        self.disk_sub_lbl.setObjectName("disk_subtitle")
        layout.addWidget(self.disk_sub_lbl)
        return card

    # ── Content splitter ───────────────────────────────────────────────────────

    def _build_content(self) -> QSplitter:
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(1)

        # ── Left: activity log ─────────────────────────────────────────────────
        log_widget = QWidget()
        log_widget.setStyleSheet(f"background-color: {self.t.sidebar_bg};")
        log_layout = QVBoxLayout(log_widget)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(0)

        log_layout.addWidget(_section_label("Activity"))
        log_layout.addWidget(_h_separator(self.t))

        self.log_view = QTextBrowser()
        self.log_view.setOpenExternalLinks(False)
        log_layout.addWidget(self.log_view)

        splitter.addWidget(log_widget)

        # ── Right: cleanup table ───────────────────────────────────────────────
        table_widget = QWidget()
        table_widget.setStyleSheet(f"background-color: {self.t.control_bg};")
        table_layout = QVBoxLayout(table_widget)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.setSpacing(0)

        table_layout.addWidget(_section_label("Cleanup Candidates"))
        table_layout.addWidget(_h_separator(self.t))

        # Stacked: empty state vs table
        self._stack = QStackedWidget()

        # Page 0: empty / error / clean state (text updated dynamically)
        self._empty_label = QLabel(
            "💨  Press  Scan  to analyse your Mac's disk\n\n"
            "PoofMac will find caches, build artifacts,\n"
            "Xcode data, logs, and more — safely."
        )
        self._empty_label.setObjectName("empty_state")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._stack.addWidget(self._empty_label)

        # Page 1: results table
        table_page = QWidget()
        tp_layout = QVBoxLayout(table_page)
        tp_layout.setContentsMargins(0, 0, 0, 0)
        tp_layout.setSpacing(0)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["", "Category", "Size", "Risk", "Path / Reason"]
        )
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(COL_CHECK,    QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(COL_CATEGORY, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(COL_SIZE,     QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(COL_RISK,     QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(COL_PATH,     QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(COL_CHECK, 36)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.cellClicked.connect(self._on_cell_clicked)
        tp_layout.addWidget(self.table)

        self.table_footer = QLabel("  No scan results yet.")
        self.table_footer.setObjectName("table_footer")
        tp_layout.addWidget(self.table_footer)

        self._stack.addWidget(table_page)
        table_layout.addWidget(self._stack, stretch=1)

        splitter.addWidget(table_widget)
        splitter.setSizes([320, 800])
        splitter.setStretchFactor(1, 3)
        return splitter

    # ── Input bar ──────────────────────────────────────────────────────────────

    def _build_input_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("input_bar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        self.chat_input = QLineEdit()
        self.chat_input.setPlaceholderText(
            "Chat with AI — ask anything, or try: 'delete the Xcode caches'…"
        )
        self.chat_input.returnPressed.connect(self._on_chat_submit)
        layout.addWidget(self.chat_input, stretch=1)

        self.scan_btn = _btn("Scan", "secondary")
        self.scan_btn.setFixedWidth(72)
        self.scan_btn.clicked.connect(self._on_scan)
        layout.addWidget(self.scan_btn)

        self.exec_btn = _btn("Clean", "primary")
        self.exec_btn.setFixedWidth(180)
        self.exec_btn.setEnabled(False)
        self.exec_btn.clicked.connect(self._on_execute)
        layout.addWidget(self.exec_btn)
        return bar

    def _setup_shortcuts(self) -> None:
        QShortcut(QKeySequence("F5"), self).activated.connect(self._on_scan)
        QShortcut(QKeySequence("Ctrl+Return"), self).activated.connect(self._on_execute)
        QShortcut(QKeySequence("Ctrl+A"), self).activated.connect(self._approve_all_safe)
        QShortcut(QKeySequence("Ctrl+U"), self).activated.connect(self._unapprove_all)

    # ── Model picker ───────────────────────────────────────────────────────────

    def _provider_groups(self) -> list[tuple[str, str]]:
        """Picker sections: each saved custom endpoint, then the built-ins.

        One group per endpoint, so the picker lists exactly the models that
        endpoint hosts — saved when the user pressed Fetch models in Settings,
        which means opening the window never waits on the network.
        """
        groups: list[tuple[str, str]] = []
        for provider_id in PROVIDER_ORDER:
            if provider_id == CUSTOM_ALIAS:
                for endpoint in self.settings.endpoints():
                    name = endpoint["name"] or store.host_label(endpoint["base_url"])
                    groups.append((name, f"{CUSTOM_PREFIX}{endpoint['id']}"))
                continue
            groups.append((PROVIDER_SPECS[provider_id]["label"], provider_id))
        return groups

    def _populate_model_picker(self) -> None:
        self.settings.endpoints(refresh=True)
        active_provider = self.settings.get_active_provider()

        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        model = self.model_combo.model()
        selected_row = -1
        for label, provider_id in self._provider_groups():
            self.model_combo.addItem(f"── {label} ──")
            model.item(self.model_combo.count() - 1).setEnabled(False)
            current = self.settings.model_for(provider_id)
            for model_id in self._models_for(provider_id):
                self.model_combo.addItem(model_id, (provider_id, model_id))
                if active_provider == provider_id and current == model_id:
                    selected_row = self.model_combo.count() - 1
        if selected_row >= 0:
            self.model_combo.setCurrentIndex(selected_row)
        elif self.model_combo.count() > 1:
            # Never leave a disabled group header as the visible selection.
            self.model_combo.setCurrentIndex(1)
        self.model_combo.blockSignals(False)

    def _models_for(self, provider_id: str) -> list[str]:
        """Model ids to offer for a provider in the toolbar picker."""
        endpoint = self.settings.endpoint_for(provider_id)
        if endpoint is not None:
            known = list(endpoint.get("models") or [])
            current = str(endpoint.get("model", "")).strip()
            if current and current not in known:
                known.insert(0, current)
            return known
        spec = PROVIDER_SPECS[provider_id]
        known = [model_id for model_id, _ in MODEL_REGISTRY.get(spec["registry"], [])]
        current = self.settings.model_for(provider_id)
        if provider_id == "ollama_local":
            known += [m for m in self._list_local_ollama_models() if m not in known]
        if current and current not in known:
            known.append(current)
        return known

    @staticmethod
    def _list_local_ollama_models() -> list[str]:
        try:
            proc = subprocess.run(
                ["ollama", "list"], capture_output=True, text=True, timeout=5
            )
            if proc.returncode != 0:
                return []
            lines = proc.stdout.strip().splitlines()[1:]
            return [ln.split()[0] for ln in lines if ln.strip()]
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return []

    def _on_model_changed(self, text: str) -> None:
        if not text or text.startswith("──"):
            return
        selected = self.model_combo.currentData()
        if not (isinstance(selected, tuple) and len(selected) == 2):
            return
        provider_id, model_name = selected
        self.settings.active_provider = provider_id
        self.settings.set_model_for(provider_id, model_name)
        if is_custom_provider(provider_id):
            # Picking a model from an endpoint also selects that endpoint, so
            # the choice survives a restart as "<endpoint> · <model>".
            store.set_active_endpoint(custom_endpoint_id(provider_id))
        try:
            values: dict[str, Any] = {"ACTIVE_PROVIDER": provider_id}
            spec = PROVIDER_SPECS.get(provider_id)
            if spec is not None and spec["model_env"]:
                values[spec["model_env"]] = model_name
            store.write_env_values(values)
        except Exception:  # noqa: BLE001
            pass
        self._chat_agent = None
        self._log(
            f'<span style="color:{self.t.accent};">Model → <b>{model_name}</b> '
            f'({provider_label(provider_id)})</span>'
        )

    # ── Disk overview ──────────────────────────────────────────────────────────

    def _refresh_disk_overview(self) -> None:
        try:
            d = get_disk_usage()
            pct = int(d["used_percent"])
            self.disk_title_lbl.setText(
                f"{d['used_human']} used  ·  {d['free_human']} free"
            )
            self.disk_sub_lbl.setText(
                f"Macintosh HD  ·  {d['total_human']} total"
            )
            self.disk_pct_lbl.setText(f"{pct}%")
            self.disk_bar.setValue(pct)

            if pct > 85:
                color = self.t.red
            elif pct > 70:
                color = self.t.orange
            else:
                color = self.t.accent
            self.disk_bar.setStyleSheet(
                f"QProgressBar {{ background-color: {self.t.separator}; border: none;"
                f"  border-radius: 3px; max-height: 6px; min-height: 6px; }}"
                f"QProgressBar::chunk {{ border-radius: 3px; background-color: {color}; }}"
            )
        except Exception:  # noqa: BLE001
            pass

    # ── Scan spinner ───────────────────────────────────────────────────────────

    def _tick_spinner(self) -> None:
        self._spinner_idx = (self._spinner_idx + 1) % len(_SPINNER_FRAMES)
        self.scan_btn.setText(_SPINNER_FRAMES[self._spinner_idx])

    # ── Activity log ───────────────────────────────────────────────────────────

    def _log(self, html: str) -> None:
        self.log_view.append(html)
        sb = self.log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ── Agent worker ───────────────────────────────────────────────────────────

    def _start_scan(self, message: str, fresh: bool = True) -> None:
        """Start the agent.

        fresh=True  → Scan button: reset agent, clear table, full scan UI.
        fresh=False → Chat input: reuse persistent agent with conversation history.
        """
        if self._scanning:
            return
        # Resolve the provider before touching the UI, so a misconfigured
        # provider reports itself instead of failing silently in the worker.
        try:
            self._chat_agent = self._chat_agent or CleanerAgent(self.settings)
            model_str, model_display = self.settings.get_active_model()
        except RuntimeError as exc:
            self._log(
                f'<span style="color:{self.t.red};"><b>Cannot send:</b> '
                f'{str(exc).splitlines()[0]}<br>'
                f'<span style="color:{self.t.accent};">Open Settings (⚙) to fix it.</span></span>'
            )
            return

        self._scanning = True
        self.scan_btn.setEnabled(False)
        self._spinner_idx = 0
        self._spinner_timer.start(100)
        self._log(
            f'<span style="color:{self.t.text_tertiary};">→ Sent to '
            f'<b>{model_display}</b></span>'
        )
        self.statusBar().showMessage(f"Contacting {model_display}…")

        if fresh:
            # Full scan — reset everything including the persistent chat agent
            self._chat_agent = CleanerAgent(self.settings)
            self.table.setRowCount(0)
            self.cleanup_items.clear()
            self._empty_label.setText(
                "💨  Scanning your Mac…\n\nAI is analysing your disk — this may take a moment."
            )
            self._stack.setCurrentIndex(0)
            self._summary_banner.setVisible(False)
            self._update_exec_button()

        self._worker = AgentWorker(self.settings, message, agent=self._chat_agent)
        self._worker.event_emitted.connect(self._on_agent_event)
        self._worker.finished.connect(self._on_agent_finished)
        self._worker.start()

    def _on_agent_event(self, event: dict) -> None:
        etype = event.get("type")
        t = self.t

        if etype == "status":
            self.statusBar().showMessage(event["text"])
            if not self._logged_status:
                # One line per request so the log shows the request was picked
                # up, without repeating "Thinking…" on every tool round-trip.
                self._logged_status = True
                self._log(
                    f'<span style="color:{t.text_tertiary};">'
                    f'· {event["text"]}</span>'
                )

        elif etype == "tool_call":
            icon = TOOL_ICONS.get(event["name"], "🔧")
            self._log(
                f'<span style="color:{t.orange};">{icon}&nbsp;'
                f'<b>{event["name"]}()</b></span>'
            )

        elif etype == "tool_result":
            self._log(
                f'<span style="color:{t.text_tertiary};">&nbsp;&nbsp;└─ done</span>'
            )

        elif etype == "plan_ready":
            self._log(
                f'<span style="color:{t.green};"><b>📋 Cleanup plan ready</b></span>'
            )
            self._populate_table(event["plan"])
            self._refresh_disk_overview()

        elif etype == "message":
            text = event["text"].strip().replace("\n", "<br>")
            self._log(
                f'<br><span style="color:{t.green};"><b>AI</b></span>'
                f'&nbsp;<span style="color:{t.text_primary};">{text}</span><br>'
            )

        elif etype == "error":
            # Provider errors are untrusted text rendered as HTML — escape it.
            text = html.escape(str(event["text"])).replace("\n", "<br>")
            self._log(
                f'<span style="color:{t.red};">'
                f'<b>❌ Error:</b> {text}</span>'
            )
            provider_id = self.settings.get_active_provider()
            ready, detail = self.settings.provider_status(provider_id)
            if not ready:
                self._log(
                    f'<span style="color:{t.orange};">{detail} — '
                    f'open Settings (⚙) to fix it.</span>'
                )

    def _on_agent_finished(self) -> None:
        self._scanning = False
        self._logged_status = False
        self._spinner_timer.stop()
        self.scan_btn.setEnabled(True)
        self.scan_btn.setText("Scan")
        self.statusBar().clearMessage()
        # If the stack is still on the scanning message and no results came in, show idle state
        if self._stack.currentIndex() == 0 and not self.cleanup_items:
            current_text = self._empty_label.text()
            if "Scanning" in current_text:
                self._empty_label.setText(
                    "💨  Press  Scan  to analyse your Mac's disk\n\n"
                    "PoofMac will find caches, build artifacts,\n"
                    "Xcode data, logs, and more — safely."
                )

    # ── Cleanup table ──────────────────────────────────────────────────────────

    def _populate_table(self, plan: dict) -> None:
        t = self.t
        self.cleanup_items = plan.get("items", [])
        self.table.setRowCount(0)

        if not self.cleanup_items:
            self._empty_label.setText(
                "✅  Your Mac looks clean!\n\n"
                "No cleanup candidates were found.\n"
                "Try again after more usage, or ask a custom question below."
            )
            self._stack.setCurrentIndex(0)
            self._summary_banner.setVisible(False)
            return

        RISK_CFG = {
            "SAFE":    (t.green,  t.green_bg,  "Safe"),
            "CAUTION": (t.orange, t.orange_bg, "Review"),
            "SKIP":    (t.red,    t.red_bg,    "Skip"),
        }

        safe_total = 0

        for item in self.cleanup_items:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setRowHeight(row, 36)

            risk = item.get("risk_level", "CAUTION")
            color, bg, label = RISK_CFG.get(risk, RISK_CFG["CAUTION"])

            # Checkbox
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            if risk == "SAFE":
                chk.setCheckState(Qt.CheckState.Checked)
                safe_total += self._parse_size(item.get("size_human", "0 B"))
            elif risk == "SKIP":
                chk.setCheckState(Qt.CheckState.Unchecked)
                chk.setFlags(Qt.ItemFlag.ItemIsEnabled)
            else:
                chk.setCheckState(Qt.CheckState.Unchecked)
            self.table.setItem(row, COL_CHECK, chk)

            # Category with icon
            category_str = item.get("category", "")
            cat_icon, cat_color = CATEGORY_META.get(category_str, ("📁", t.text_secondary))
            cat_lbl = QLabel(f"  {cat_icon}  {category_str}")
            cat_lbl.setStyleSheet(
                f"color: {cat_color}; background: transparent; font-size: 12px; font-weight: 500;"
            )
            cat_wrapper = QWidget()
            cat_wrapper.setStyleSheet("background: transparent;")
            cat_wlayout = QHBoxLayout(cat_wrapper)
            cat_wlayout.setContentsMargins(0, 0, 0, 0)
            cat_wlayout.addWidget(cat_lbl)
            cat_wlayout.addStretch()
            self.table.setCellWidget(row, COL_CATEGORY, cat_wrapper)

            # Size
            size_item = QTableWidgetItem(item.get("size_human", "?"))
            size_item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            self.table.setItem(row, COL_SIZE, size_item)

            # Risk pill
            pill_wrapper = QWidget()
            pill_wrapper.setStyleSheet("background: transparent;")
            pill_layout = QHBoxLayout(pill_wrapper)
            pill_layout.setContentsMargins(6, 0, 6, 0)
            pill_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pill_layout.addWidget(_make_pill(label, color, bg))
            self.table.setCellWidget(row, COL_RISK, pill_wrapper)

            # Path / reason
            path = item.get("path", "")
            reason = item.get("reason", "")
            path_item = QTableWidgetItem(path)
            path_item.setToolTip(reason if reason else path)
            self.table.setItem(row, COL_PATH, path_item)

        self._stack.setCurrentIndex(1)
        self._update_exec_button()

        # Post-scan summary banner
        if safe_total > 0:
            self._summary_banner.setText(
                f"  💨  You could free ~{format_size(safe_total)} today  —  "
                f"select items below and click Clean"
            )
            self._summary_banner.setVisible(True)

    def _on_cell_clicked(self, row: int, col: int) -> None:
        chk = self.table.item(row, COL_CHECK)
        if chk is None:
            return
        if not (chk.flags() & Qt.ItemFlag.ItemIsUserCheckable):
            return
        new_state = (
            Qt.CheckState.Unchecked
            if chk.checkState() == Qt.CheckState.Checked
            else Qt.CheckState.Checked
        )
        chk.setCheckState(new_state)
        self._update_exec_button()

    def _approve_all_safe(self) -> None:
        for row, item in enumerate(self.cleanup_items):
            if item.get("risk_level") == "SAFE":
                chk = self.table.item(row, COL_CHECK)
                if chk:
                    chk.setCheckState(Qt.CheckState.Checked)
        self._update_exec_button()

    def _unapprove_all(self) -> None:
        for row in range(self.table.rowCount()):
            chk = self.table.item(row, COL_CHECK)
            if chk and (chk.flags() & Qt.ItemFlag.ItemIsUserCheckable):
                chk.setCheckState(Qt.CheckState.Unchecked)
        self._update_exec_button()

    def _update_exec_button(self) -> None:
        approved = self._get_approved_items()
        count = len(approved)
        total = sum(self._parse_size(i.get("size_human", "0 B")) for i in approved)

        if count == 0 or self.safe_mode:
            self.exec_btn.setEnabled(False)
            label = "Safe Mode" if self.safe_mode else "Clean"
        else:
            self.exec_btn.setEnabled(True)
            label = f"Clean {count} items  (~{format_size(total)})"
        self.exec_btn.setText(label)

        safe_n    = sum(1 for i in self.cleanup_items if i.get("risk_level") == "SAFE")
        caution_n = sum(1 for i in self.cleanup_items if i.get("risk_level") == "CAUTION")
        skip_n    = sum(1 for i in self.cleanup_items if i.get("risk_level") == "SKIP")
        if self.cleanup_items:
            self.table_footer.setText(
                f"  {len(self.cleanup_items)} items  ·  "
                f"{safe_n} safe  ·  {caution_n} review  ·  {skip_n} skip"
                + (f"  ·  {count} selected" if count else "")
            )

    def _get_approved_items(self) -> list[dict]:
        approved = []
        for row, item in enumerate(self.cleanup_items):
            chk = self.table.item(row, COL_CHECK)
            if chk and chk.checkState() == Qt.CheckState.Checked:
                approved.append(item)
        return approved

    @staticmethod
    def _parse_size(size_str: str) -> int:
        try:
            parts = size_str.strip().split()
            n = float(parts[0])
            unit = parts[1].upper() if len(parts) > 1 else "B"
            mult = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
            return int(n * mult.get(unit, 1))
        except (ValueError, IndexError):
            return 0

    # ── Execution ──────────────────────────────────────────────────────────────

    def _on_execute(self) -> None:
        approved = self._get_approved_items()
        if not approved or self.safe_mode:
            return

        total = sum(self._parse_size(i.get("size_human", "0 B")) for i in approved)
        dlg = ConfirmDialog(len(approved), format_size(total), self.t, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        self.exec_btn.setEnabled(False)
        self._log(
            f'<br><span style="color:{self.t.orange};">'
            f'<b>Deleting {len(approved)} item(s)…</b></span>'
        )

        self._exec_worker = ExecutionWorker(self.executor, approved)
        self._exec_worker.log_line.connect(self._log)
        self._exec_worker.done.connect(self._on_exec_done)
        self._exec_worker.start()

    def _on_exec_done(self, summary: str) -> None:
        self._log(
            f'<br><span style="color:{self.t.green};">'
            f'<b>{summary}</b></span>'
            f'<br><span style="color:{self.t.text_tertiary};">'
            f'Audit log: {self.audit.log_path}</span><br>'
        )
        self._refresh_disk_overview()
        self._update_exec_button()
        self._summary_banner.setVisible(False)

    # ── Input handlers ─────────────────────────────────────────────────────────

    def _on_scan(self) -> None:
        if self._scanning:
            return
        self._log(
            f'<br><span style="color:{self.t.accent};">'
            f'<b>Starting disk analysis…</b></span>'
        )
        self._start_scan(
            "Analyse my Mac's disk usage and show me everything I can safely clean up.",
            fresh=True,
        )

    def _on_chat_submit(self) -> None:
        msg = self.chat_input.text().strip()
        if not msg:
            return
        if self._scanning:
            self._log(
                f'<span style="color:{self.t.orange};">Still working on the '
                f'previous request — one moment.</span>'
            )
            return
        self.chat_input.clear()
        self._log(
            f'<br><span style="color:{self.t.text_secondary};"><b>You</b></span>'
            f'&nbsp;{msg}'
        )
        # Use fresh=False so the persistent agent remembers previous context
        self._start_scan(msg, fresh=False)

    def _on_safe_mode_toggled(self, checked: bool) -> None:
        self.safe_mode = checked
        self.executor.safe_mode = checked
        self._update_exec_button()
        state = "on" if checked else "off"
        self._log(
            f'<span style="color:{self.t.orange};">Safe mode <b>{state}</b></span>'
        )

    def closeEvent(self, event) -> None:
        super().closeEvent(event)

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.settings, self.t, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._log(
                f'<span style="color:{self.t.accent};">Settings saved.</span>'
            )
            if dlg.provider_changed:
                # Drop the cached agent so the new provider/model applies now
                self._chat_agent = None
                try:
                    _, display = self.settings.get_active_model()
                    self._log(
                        f'<span style="color:{self.t.accent};">'
                        f'Model \u2192 <b>{display}</b></span>'
                    )
                except RuntimeError:
                    pass
            # Refresh model picker after potential key changes
            self._populate_model_picker()
            # Apply safe mode from settings
            self.safe_mode = self.settings.safe_mode
            self.safe_check.setChecked(self.safe_mode)
            self.executor.safe_mode = self.safe_mode
            self._update_exec_button()


# ── Entry point ───────────────────────────────────────────────────────────────


def run_gui(settings: Settings, safe_mode: bool = False) -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("PoofMac")
    app.setOrganizationName("PoofMac")

    t = setup_theme(app)

    disclaimer = DisclaimerDialog(t)
    if disclaimer.exec() != QDialog.DialogCode.Accepted:
        sys.exit(0)

    window = PoofMacWindow(settings, t, safe_mode=safe_mode)
    window.show()

    window._log(
        f'<span style="color:{t.text_secondary};">'
        f'Press <b>F5</b> or click <b>Scan</b> to analyse your disk.</span>'
    )

    ok, msg = settings.validate_model_access()
    if ok:
        window._log(f'<span style="color:{t.green};">✓ {msg}</span>')
    else:
        window._log(
            f'<span style="color:{t.orange};">⚠ Model not configured: '
            f'{msg.splitlines()[0]}<br>'
            f'<a style="color:{t.accent};">Click ⚙ to add your API key.</a></span>'
        )

    if safe_mode or settings.safe_mode:
        window._log(
            f'<span style="color:{t.orange};">🛡 Safe mode is on — '
            f'no files will be deleted.</span>'
        )

    sys.exit(app.exec())
