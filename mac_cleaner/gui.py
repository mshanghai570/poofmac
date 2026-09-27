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

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

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
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from mac_cleaner.audit import AuditLogger
from mac_cleaner.config import MODEL_REGISTRY, Settings
from mac_cleaner.executor import Executor
from mac_cleaner.llm import CleanerAgent
from mac_cleaner.scanner import format_size, get_disk_usage

# ── Convenience list helpers for the model picker ─────────────────────────────

CLOUD_MODELS = [m for m, _ in MODEL_REGISTRY["ollama_cloud"]]
LOCAL_MODELS_KNOWN = [m for m, _ in MODEL_REGISTRY["ollama_local"]]

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


class SettingsDialog(QDialog):
    """Settings panel: API Keys, Models, Safety — saves to .env."""

    def __init__(self, settings: Settings, t: Theme, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.t = t
        # Set after a save when the active provider/model actually changed, so
        # the main window can drop its cached agent and pick up the new model.
        self.provider_changed = False
        self.setWindowTitle("PoofMac Settings")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setFixedSize(560, 540)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 16)
        root.setSpacing(12)

        title = QLabel("Settings")
        title.setStyleSheet(
            f"font-size: 17px; font-weight: 700; color: {t.text_primary}; background: transparent;"
        )
        root.addWidget(title)

        tabs = QTabWidget()
        tabs.addTab(self._build_api_tab(), "API Keys")
        tabs.addTab(self._build_compat_tab(), "Custom")
        tabs.addTab(self._build_models_tab(), "Models")
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

    # ── Tab builders ───────────────────────────────────────────────────────────

    def _build_api_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        def _key_row(label: str, value: str, placeholder: str) -> QLineEdit:
            layout.addWidget(QLabel(label))
            edit = QLineEdit(value)
            edit.setPlaceholderText(placeholder)
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            layout.addWidget(edit)
            return edit

        self._anthropic_edit = _key_row(
            "Anthropic API Key",
            self.settings.anthropic_api_key,
            "sk-ant-…  →  console.anthropic.com",
        )
        self._openrouter_edit = _key_row(
            "OpenRouter API Key",
            self.settings.openrouter_api_key,
            "sk-or-…  →  openrouter.ai",
        )
        self._openai_edit = _key_row(
            "OpenAI API Key",
            self.settings.openai_api_key,
            "sk-…  →  platform.openai.com",
        )

        note = QLabel(
            "Keys are saved to your .env file. They are never sent anywhere except "
            "the provider you select.\n"
            "Using vLLM, LM Studio, Groq or another OpenAI-compatible server? "
            "See the Custom tab."
        )
        note.setWordWrap(True)
        note.setStyleSheet(
            f"font-size: 11px; color: {self.t.text_tertiary}; background: transparent;"
        )
        layout.addWidget(note)
        layout.addStretch()
        return w

    def _build_compat_tab(self) -> QWidget:
        """Any OpenAI-compatible endpoint — vLLM, LM Studio, llama.cpp,
        Ollama /v1, Groq, DeepSeek, Together, OpenAI base-URL proxies …"""
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        intro = QLabel(
            "Point PoofMac at any service that speaks the OpenAI "
            "chat-completions API."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(
            f"font-size: 12px; color: {self.t.text_secondary}; background: transparent;"
        )
        layout.addWidget(intro)

        layout.addWidget(QLabel("Base URL"))
        self._compat_base_edit = QLineEdit(self.settings.openai_compat_base_url)
        self._compat_base_edit.setPlaceholderText(
            "http://localhost:11434/v1  ·  http://localhost:1234/v1  ·  "
            "https://api.groq.com/openai/v1"
        )
        self._compat_base_edit.setEchoMode(QLineEdit.EchoMode.Normal)
        layout.addWidget(self._compat_base_edit)

        layout.addWidget(QLabel("API key (optional — local servers need none)"))
        self._compat_key_edit = QLineEdit(self.settings.openai_compat_api_key)
        self._compat_key_edit.setPlaceholderText("leave blank for local servers")
        self._compat_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        layout.addWidget(self._compat_key_edit)

        layout.addWidget(QLabel("Model name (exactly as the endpoint lists it)"))
        self._compat_model_edit = QLineEdit(self.settings.openai_compat_model)
        self._compat_model_edit.setPlaceholderText(
            "qwen3.6:27b  ·  llama3.1-8b-instruct  ·  deepseek-chat"
        )
        layout.addWidget(self._compat_model_edit)

        note = QLabel(
            "Saved to .env as OPENAI_COMPAT_BASE_URL / OPENAI_COMPAT_API_KEY / "
            "OPENAI_COMPAT_MODEL. This provider is used only when the Anthropic, "
            "OpenRouter and OpenAI keys are all empty."
        )
        note.setWordWrap(True)
        note.setStyleSheet(
            f"font-size: 11px; color: {self.t.text_tertiary}; background: transparent;"
        )
        layout.addWidget(note)
        layout.addStretch()
        return w

    def _build_models_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        layout.addWidget(QLabel("Cloud model (Anthropic / OpenRouter / OpenAI)"))
        self._cloud_model_combo = QComboBox()
        all_cloud: list[str] = []
        for provider_key in ("anthropic", "openai", "openrouter"):
            self._cloud_model_combo.addItem(f"── {provider_key.capitalize()} ──")
            model_count = self._cloud_model_combo.count()
            self._cloud_model_combo.model().item(model_count - 1).setEnabled(False)
            for m, label in MODEL_REGISTRY[provider_key]:
                self._cloud_model_combo.addItem(f"{m}  —  {label.split('—')[-1].strip()}", m)
                all_cloud.append(m)
                if m == self.settings.preferred_cloud_model:
                    self._cloud_model_combo.setCurrentIndex(self._cloud_model_combo.count() - 1)
        layout.addWidget(self._cloud_model_combo)

        layout.addSpacing(8)
        layout.addWidget(QLabel("Ollama cloud model (requires Ollama subscription)"))
        self._ollama_cloud_combo = QComboBox()
        for m, label in MODEL_REGISTRY["ollama_cloud"]:
            self._ollama_cloud_combo.addItem(f"{m}  —  {label.split('—')[-1].strip()}", m)
            if m == self.settings.preferred_local_model:
                self._ollama_cloud_combo.setCurrentIndex(self._ollama_cloud_combo.count() - 1)
        layout.addWidget(self._ollama_cloud_combo)

        layout.addSpacing(8)
        layout.addWidget(QLabel("Ollama local model (runs on your Mac, no internet)"))
        self._ollama_local_combo = QComboBox()
        for m, label in MODEL_REGISTRY["ollama_local"]:
            self._ollama_local_combo.addItem(f"{m}  —  {label.split('(')[0].strip()}", m)
            if m == self.settings.preferred_local_model:
                self._ollama_local_combo.setCurrentIndex(self._ollama_local_combo.count() - 1)
        layout.addWidget(self._ollama_local_combo)

        note = QLabel(
            "The active model is chosen automatically based on which API key is set. "
            "If no key is set, the Ollama model is used."
        )
        note.setWordWrap(True)
        note.setStyleSheet(
            f"font-size: 11px; color: {self.t.text_tertiary}; background: transparent;"
        )
        layout.addWidget(note)
        layout.addStretch()
        return w

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

    # ── Save ───────────────────────────────────────────────────────────────────

    def _save_and_close(self) -> None:
        try:
            try:
                prev_active = self.settings.get_active_model()
            except Exception:  # noqa: BLE001
                prev_active = None

            from dotenv import find_dotenv, set_key
            env_path = find_dotenv(usecwd=True) or ".env"

            # API keys
            set_key(env_path, "ANTHROPIC_API_KEY", self._anthropic_edit.text().strip())
            set_key(env_path, "OPENROUTER_API_KEY", self._openrouter_edit.text().strip())
            set_key(env_path, "OPENAI_API_KEY", self._openai_edit.text().strip())

            # OpenAI-compatible endpoint (Custom tab)
            compat_base = self._compat_base_edit.text().strip().rstrip("/")
            compat_key = self._compat_key_edit.text().strip()
            compat_model = self._compat_model_edit.text().strip()
            set_key(env_path, "OPENAI_COMPAT_BASE_URL", compat_base)
            set_key(env_path, "OPENAI_COMPAT_API_KEY", compat_key)
            set_key(env_path, "OPENAI_COMPAT_MODEL", compat_model)
            self.settings.openai_compat_base_url = compat_base
            self.settings.openai_compat_api_key = compat_key
            self.settings.openai_compat_model = compat_model

            # Cloud model
            cloud_idx = self._cloud_model_combo.currentIndex()
            cloud_data = self._cloud_model_combo.itemData(cloud_idx)
            if cloud_data:
                set_key(env_path, "PREFERRED_CLOUD_MODEL", cloud_data)
                self.settings.preferred_cloud_model = cloud_data

            # Local model (prefer cloud tab, fallback to local tab)
            ollama_cloud_data = self._ollama_cloud_combo.itemData(
                self._ollama_cloud_combo.currentIndex()
            )
            ollama_local_data = self._ollama_local_combo.itemData(
                self._ollama_local_combo.currentIndex()
            )
            preferred_local = ollama_cloud_data or ollama_local_data or ""
            if preferred_local:
                set_key(env_path, "PREFERRED_LOCAL_MODEL", preferred_local)
                self.settings.preferred_local_model = preferred_local

            # Safe mode
            set_key(env_path, "SAFE_MODE", str(self._safe_mode_check.isChecked()).lower())
            self.settings.safe_mode = self._safe_mode_check.isChecked()

            # Update API keys on the settings object
            if self._anthropic_edit.text().strip():
                self.settings.anthropic_api_key = self._anthropic_edit.text().strip()
            if self._openrouter_edit.text().strip():
                self.settings.openrouter_api_key = self._openrouter_edit.text().strip()
            if self._openai_edit.text().strip():
                self.settings.openai_api_key = self._openai_edit.text().strip()

            try:
                new_active = self.settings.get_active_model()
            except Exception:  # noqa: BLE001
                new_active = None
            self.provider_changed = prev_active != new_active

        except Exception as exc:  # noqa: BLE001
            # Non-fatal — settings still applied in memory
            pass

        self.accept()


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

    def _populate_model_picker(self) -> None:
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        mdl = self.model_combo.model()

        try:
            _, display = self.settings.get_active_model()
            if any(x in display for x in ("(Anthropic)", "(OpenRouter)", "(OpenAI)", "(Custom")):
                self.model_combo.addItem(f"✓ {display}")
                self.model_combo.insertSeparator(1)
        except RuntimeError:
            pass

        current = self.settings.preferred_local_model

        self.model_combo.addItem("── Ollama Cloud ──")
        mdl.item(self.model_combo.count() - 1).setEnabled(False)
        for m in CLOUD_MODELS:
            self.model_combo.addItem(m)
            if m == current:
                self.model_combo.setCurrentIndex(self.model_combo.count() - 1)

        self.model_combo.insertSeparator(self.model_combo.count())
        self.model_combo.addItem("── Ollama Local (recommended) ──")
        mdl.item(self.model_combo.count() - 1).setEnabled(False)
        for m in LOCAL_MODELS_KNOWN:
            self.model_combo.addItem(m)
            if m == current:
                self.model_combo.setCurrentIndex(self.model_combo.count() - 1)

        local_installed = [
            m for m in self._list_local_ollama_models()
            if m not in LOCAL_MODELS_KNOWN
        ]
        if local_installed:
            self.model_combo.insertSeparator(self.model_combo.count())
            self.model_combo.addItem("── Ollama Local (installed) ──")
            mdl.item(self.model_combo.count() - 1).setEnabled(False)
            for m in local_installed:
                self.model_combo.addItem(m)
                if m == current:
                    self.model_combo.setCurrentIndex(self.model_combo.count() - 1)

        self.model_combo.blockSignals(False)

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
        if not text or text.startswith("──") or text.startswith("✓"):
            return
        model_name = text.split(" (")[0].strip()
        self.settings.preferred_local_model = model_name
        self._log(
            f'<span style="color:{self.t.accent};">Model → <b>{model_name}</b></span>'
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
        self._scanning = True
        self.scan_btn.setEnabled(False)
        self._spinner_idx = 0
        self._spinner_timer.start(100)

        if fresh:
            # Full scan — reset everything including the persistent chat agent
            self._chat_agent = None
            self.table.setRowCount(0)
            self.cleanup_items.clear()
            self._empty_label.setText(
                "💨  Scanning your Mac…\n\nAI is analysing your disk — this may take a moment."
            )
            self._stack.setCurrentIndex(0)
            self._summary_banner.setVisible(False)
            self._update_exec_button()

        # Create a new agent for fresh scans; reuse persistent agent for chat
        if self._chat_agent is None:
            self._chat_agent = CleanerAgent(self.settings)

        self._worker = AgentWorker(self.settings, message, agent=self._chat_agent)
        self._worker.event_emitted.connect(self._on_agent_event)
        self._worker.finished.connect(self._on_agent_finished)
        self._worker.start()

    def _on_agent_event(self, event: dict) -> None:
        etype = event.get("type")
        t = self.t

        if etype == "status":
            self.statusBar().showMessage(event["text"])

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
            text = event["text"]
            text_lower = text.lower()
            self._log(
                f'<span style="color:{t.red};">'
                f'<b>❌ Error:</b> {text.replace(chr(10), "<br>")}</span>'
            )
            # Build a user-friendly hint
            if "authentication" in text_lower or "api key" in text_lower or "unauthorized" in text_lower:
                hint = (
                    "Authentication failed.\n\n"
                    "Check your API key in .env:\n"
                    "  ANTHROPIC_API_KEY / OPENAI_API_KEY / OLLAMA_API_KEY\n"
                    "  OPENAI_COMPAT_API_KEY (custom endpoint)"
                )
            elif "connection refused" in text_lower or ("ollama" in text_lower and "connect" in text_lower):
                hint = (
                    "Cannot reach the model server.\n\n"
                    "Start the server first:\n"
                    "  ollama serve\n\n"
                    "Or check OPENAI_COMPAT_BASE_URL in .env if you use a "
                    "custom endpoint, or set PREFERRED_CLOUD_MODEL."
                )
            elif "no model" in text_lower or "model not found" in text_lower:
                hint = (
                    "No model configured.\n\n"
                    "Add to .env:\n"
                    "  PREFERRED_LOCAL_MODEL=qwen3.6:8b\n"
                    "  PREFERRED_CLOUD_MODEL=claude-sonnet-4-6"
                )
            else:
                hint = f"An error occurred:\n\n{text}\n\nCheck ~/.poofmac-audit.jsonl for details."
            self._empty_label.setText(f"⚠️  {hint}")
            self._stack.setCurrentIndex(0)

    def _on_agent_finished(self) -> None:
        self._scanning = False
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
        if not msg or self._scanning:
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
