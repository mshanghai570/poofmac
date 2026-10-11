# ── Developer Storage tab ─────────────────────────────────────────────────────
#
# Implemented in dev_storage_tab.py to keep gui.py manageable; this file
# defines a mixin consumed by MaintenanceDialog. Everything follows gui.py's
# existing conventions: _btn helpers, QTableWidget review tables, QThread
# workers, QMessageBox confirmations, and Trash-first cleanup.

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
    QComboBox,
)

from mac_cleaner.audit import AuditLogger
from mac_cleaner import devclassify, devcleanup, devscan

# Reuse gui.py's shared widget helpers without importing the whole module
# (a circular import at module scope would be): they are looked up lazily.

def _btn(text: str, cls: str):
    from mac_cleaner.gui import _btn as _host_btn
    return _host_btn(text, cls)


def _section_label(text: str):
    from mac_cleaner.gui import _section_label as _host_label
    return _host_label(text)


class DevScanWorker(QThread):
    """Deep developer-storage scan off the GUI thread."""

    scan_done = Signal(dict)

    def __init__(self, cancel_flag: list) -> None:
        super().__init__()
        self._cancel_flag = cancel_flag

    def run(self) -> None:
        from mac_cleaner.devscan import scan_developer_storage

        try:
            report = scan_developer_storage(cancel_flag=self._cancel_flag)
        except Exception as exc:  # noqa: BLE001 — surface, never crash
            report = {"items": [], "summary": {}, "error": str(exc)}
        self.scan_done.emit(report)


class DevCleanupWorker(QThread):
    """Runs the cleanup engine off the GUI thread with live progress."""

    item_progress = Signal(int, int, str)
    cleanup_done = Signal(object)

    def __init__(self, engine, plan_items, trash=True) -> None:
        super().__init__()
        self.engine = engine
        self.plan_items = plan_items
        self.trash = trash
        self._cancel: list = [False]

    def cancel(self) -> None:
        self._cancel[0] = True

    def run(self) -> None:
        report = self.engine.cleanup(
            self.plan_items,
            trash=self.trash,
            dry_run=False,
            progress=lambda i, n, p: self.item_progress.emit(i, n, p),
            cancel_flag=self._cancel,
        )
        self.cleanup_done.emit(report)


DEV_TABLE_COLS = ("", "Item", "Category", "Size", "Modified", "Path")
COL_DEV_CHECK, COL_DEV_ITEM, COL_DEV_CAT, COL_DEV_SIZE, COL_DEV_MOD, COL_DEV_PATH = range(6)

# HIG-ish colors (match gui.py's palette usage)
_CAT_COLORS = {
    devclassify.SAFE_CACHE: "#28CD41",                 # green
    devclassify.REVIEW_REQUIRED: "#FF9F0A",            # orange
    devclassify.POTENTIALLY_OBSOLETE: "#BF5AF2",       # purple
    devclassify.PROTECTED: "#8E8E93",                  # grey
}


class DevStorageTabMixin:
    """
    Mixin for MaintenanceDialog. Expects from the host:
        self._worker / self._worker_loose — optional shared QThread slot
        self._worker_lock semantics of gui.py's dialog (we keep our own)
        self._set_busy(True/False) optional
    """

    # ── Build ────────────────────────────────────────────────────────────────

    def _build_dev_storage_tab(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(4, 12, 4, 4)
        outer.setSpacing(10)

        controls = QHBoxLayout()
        self.dev_scan_btn = _btn("🔍  Scan Developer Storage", "secondary")
        self.dev_scan_btn.setToolTip(
            "Deep-scan Xcode DerivedData, Archives, DeviceSupport, simulator "
            "devices/runtimes and caches. Read-only — nothing is deleted."
        )
        self.dev_scan_btn.clicked.connect(self._on_dev_scan)
        controls.addWidget(self.dev_scan_btn)

        controls.addStretch()

        self.dev_search = QLineEdit()
        self.dev_search.setPlaceholderText("Search findings…")
        self.dev_search.setFixedWidth(200)
        self.dev_search.textChanged.connect(self._on_dev_filter_changed)
        controls.addWidget(self.dev_search)

        self.dev_cat_filter = QComboBox()
        self.dev_cat_filter.addItem("All categories", None)
        for key, label in devclassify.CATEGORY_LABELS.items():
            self.dev_cat_filter.addItem(label, key)
        self.dev_cat_filter.currentIndexChanged.connect(self._on_dev_filter_changed)
        controls.addWidget(self.dev_cat_filter)

        self.dev_sort_combo = QComboBox()
        self.dev_sort_combo.addItem("Largest first", "size")
        self.dev_sort_combo.addItem("Newest first", "mtime")
        self.dev_sort_combo.addItem("Category", "category")
        self.dev_sort_combo.currentIndexChanged.connect(self._on_dev_filter_changed)
        controls.addWidget(self.dev_sort_combo)

        outer.addLayout(controls)

        # Splitter: table (left) + explanation (right)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setHandleWidth(1)

        self.dev_table = QTableWidget(0, len(DEV_TABLE_COLS))
        self.dev_table.setHorizontalHeaderLabels(DEV_TABLE_COLS)
        hh = self.dev_table.horizontalHeader()
        hh.setSectionResizeMode(COL_DEV_CHECK, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(COL_DEV_ITEM, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(COL_DEV_CAT, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(COL_DEV_SIZE, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(COL_DEV_MOD, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(COL_DEV_PATH, QHeaderView.ResizeMode.Stretch)
        self.dev_table.setColumnWidth(COL_DEV_CHECK, 36)
        self.dev_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.dev_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.dev_table.setAlternatingRowColors(True)
        self.dev_table.verticalHeader().setVisible(False)
        self.dev_table.cellClicked.connect(self._on_dev_cell_clicked)
        split.addWidget(self.dev_table)

        explanation_wrap = QWidget()
        exp_layout = QVBoxLayout(explanation_wrap)
        exp_layout.setContentsMargins(8, 0, 0, 0)
        exp_label = _section_label("What is this?")
        exp_layout.addWidget(exp_label)
        self.dev_detail = QTextBrowser()
        self.dev_detail.setMinimumWidth(240)
        self.dev_detail.setOpenExternalLinks(False)
        self.dev_detail.setHtml(
            "<p style='color:gray'>Scan to see findings. Click a row for a "
            "plain-English explanation of what it is, whether it can be "
            "regenerated, and what could break if you remove it.</p>"
        )
        exp_layout.addWidget(self.dev_detail, stretch=1)
        split.addWidget(explanation_wrap)
        split.setSizes([640, 360])
        split.setStretchFactor(0, 3)
        outer.addWidget(split, stretch=1)

        btn_row = QHBoxLayout()
        self.dev_confirm_btn = _btn("🗑  Trash Selected…", "danger")
        self.dev_confirm_btn.setToolTip(
            "Move the checked items to the Trash. Nothing is deleted until "
            "you confirm on the next screen. Recoverable until the Trash is emptied."
        )
        self.dev_confirm_btn.setEnabled(False)
        self.dev_confirm_btn.clicked.connect(self._on_dev_cleanup_confirm)
        btn_row.addWidget(self.dev_confirm_btn)

        self.dev_cancel_btn = _btn("Stop", "secondary")
        self.dev_cancel_btn.setEnabled(False)
        self.dev_cancel_btn.clicked.connect(self._on_dev_cancel)
        btn_row.addWidget(self.dev_cancel_btn)
        btn_row.addStretch()

        self.dev_status = QLabel(
            "Nothing scanned yet. Scanning reads ~/Library/Developer only."
        )
        self.dev_status.setObjectName("table_footer")
        self.dev_status.setWordWrap(True)
        btn_row.addWidget(self.dev_status, stretch=1)
        outer.addLayout(btn_row)

        self._dev_items: list[dict] = []
        self._dev_checked: dict[str, dict] = {}
        self._dev_engine = devcleanup.DevCleanupEngine(AuditLogger())
        self._dev_cleanup_worker: Optional[DevCleanupWorker] = None
        self._dev_scan_worker: Optional[DevScanWorker] = None
        self._dev_cancel_flag: list = [False]
        return page

    # ── Scan ──────────────────────────────────────────────────────────────────

    def _on_dev_scan(self) -> None:
        if self._dev_scan_worker is not None and self._dev_scan_worker.isRunning():
            return
        self.dev_table.setRowCount(0)
        self._dev_items.clear()
        self._dev_checked.clear()
        self.dev_confirm_btn.setEnabled(False)
        self.dev_scan_btn.setEnabled(False)
        self.dev_cancel_btn.setEnabled(True)
        self.dev_status.setText("Scanning ~/Library/Developer…")
        self._dev_cancel_flag = [False]
        self._dev_scan_worker = DevScanWorker(self._dev_cancel_flag)
        self._dev_scan_worker.scan_done.connect(self._on_dev_scan_done)
        self._dev_scan_worker.start()

    def _on_dev_cancel(self) -> None:
        self._dev_cancel_flag[0] = True
        if self._dev_cleanup_worker is not None:
            self._dev_cleanup_worker.cancel()

    def _on_dev_scan_done(self, report: dict) -> None:
        self._dev_scan_worker = None
        self.dev_scan_btn.setEnabled(True)
        self.dev_cancel_btn.setEnabled(False)
        err = report.get("error")
        if err:
            self.dev_status.setText(f"Scan failed: {err}")
            return
        self._dev_items = self._classify_and_sort(report)
        self._populate_dev_table()
        s = report.get("summary", {})
        self.dev_status.setText(
            f"{s.get('item_count', 0)} finding(s) — measured "
            f"{s.get('measured_human', '0 B')}"
            + (f", ≥{s.get('estimated_human', '0 B')} more (partial)" if s.get("estimated_bytes") else "")
            + ". Review each row; only you decide what to remove."
        )

    def _classify_and_sort(self, report: dict) -> list[dict]:
        items = list(report.get("items", []))
        for item in items:
            item.update(devclassify.classify(item, report))
        key = self.dev_sort_combo.currentData() or "size"
        if key == "size":
            items.sort(key=lambda i: i.get("size_bytes", 0), reverse=True)
        elif key == "mtime":
            items.sort(key=lambda i: i.get("last_modified") or 0, reverse=True)
        else:
            items.sort(key=lambda i: (i.get("category", ""), -i.get("size_bytes", 0)))
        return items

    # ── Table population / filtering ──────────────────────────────────────────

    def _visible_dev_items(self) -> list[dict]:
        text = self.dev_search.text().strip().lower()
        cat = self.dev_cat_filter.currentData()
        out = []
        for item in self._dev_items:
            if cat and item.get("category") != cat:
                continue
            if text and text not in item.get("label", "").lower() \
                    and text not in item.get("path", "").lower():
                continue
            out.append(item)
        return out

    def _populate_dev_table(self) -> None:
        import time as _time

        self.dev_table.setRowCount(0)
        self._dev_checked.clear()
        for item in self._visible_dev_items():
            row = self.dev_table.rowCount()
            self.dev_table.insertRow(row)
            chk = QTableWidgetItem()
            if item.get("delete_eligible"):
                chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
                chk.setCheckState(Qt.CheckState.Unchecked)
            else:
                # Protected items: not selectable, pad with a lock glyph.
                chk.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.dev_table.setItem(row, COL_DEV_CHECK, chk)
            name = item.get("label", item.get("path", ""))
            if item.get("category") == devclassify.PROTECTED:
                name = f"🔒 {name}"
            self.dev_table.setItem(row, COL_DEV_ITEM, QTableWidgetItem(name))
            cat_item = QTableWidgetItem(item.get("category_label", ""))
            color = _CAT_COLORS.get(item.get("category"), "#8E8E93")
            cat_item.setForeground(QColor(color))
            self.dev_table.setItem(row, COL_DEV_CAT, cat_item)
            self.dev_table.setItem(
                row, COL_DEV_SIZE,
                QTableWidgetItem(devscan.format_size(item.get("size_bytes", 0)))
                if hasattr(devscan, "format_size")
                else QTableWidgetItem(f"{item.get('size_bytes', 0)} B"),
            )
            mod = item.get("last_modified")
            self.dev_table.setItem(
                row, COL_DEV_MOD,
                QTableWidgetItem(_time.strftime("%Y-%m-%d", _time.localtime(mod)) if mod else "—"),
            )
            self.dev_table.setItem(row, COL_DEV_PATH, QTableWidgetItem(item.get("path", "")))
        self.dev_confirm_btn.setEnabled(False)

    def _on_dev_filter_changed(self) -> None:
        self._populate_dev_table()

    def _on_dev_cell_clicked(self, row: int, _col: int) -> None:
        # Explanation panel first — the detail view must never be stale.
        visible = self._visible_dev_items()
        if 0 <= row < len(visible):
            self._show_dev_detail(visible[row])
        chk = self.dev_table.item(row, COL_DEV_CHECK)
        path_item = self.dev_table.item(row, COL_DEV_PATH)
        if chk is None or path_item is None:
            return
        if not (chk.flags() & Qt.ItemFlag.ItemIsUserCheckable):
            self.dev_status.setText("🔒 Protected — not selectable for cleanup.")
            return
        path = path_item.text()
        if chk.checkState() == Qt.CheckState.Checked:
            self._dev_checked[path] = visible[row] if row < len(visible) else {}
        else:
            self._dev_checked.pop(path, None)
        self._refresh_dev_totals()

    def _refresh_dev_totals(self) -> None:
        total = sum(i.get("size_bytes", 0) for i in self._dev_checked.values())
        self.dev_confirm_btn.setEnabled(bool(self._dev_checked))
        self.dev_status.setText(
            f"{len(self._dev_checked)} item(s) selected — "
            f"up to {devscan.format_size(total) if hasattr(devscan, 'format_size') else total} recoverable. "
            "Estimates; the Trash must still be emptied for space to be freed."
        )

    def _show_dev_detail(self, item: dict) -> None:
        from mac_cleaner.scanner import format_size as _fs

        lines = [f"<b>{item.get('label', '')}</b>"]
        lines.append(f"<p><b>Path:</b> <code>{item.get('path', '')}</code></p>")
        size = item.get("size_bytes", 0)
        if size:
            lines.append(f"<p><b>Size:</b> ~{_fs(size)} ({item.get('measurement', 'measured')})</p>")
        lines.append(
            f"<p><b>Classification:</b> {item.get('category_label', '')} — "
            f"{'removable after your approval' if item.get('delete_eligible') else 'not recommended for removal'}</p>"
        )
        reason = item.get("reason", "")
        if reason:
            lines.append(f"<p><b>Why:</b> {reason}</p>")
        expl = item.get("explanation", "")
        if expl:
            lines.append(f"<p>{html_escape(expl)}</p>")
        notes = item.get("notes") or {}
        if notes:
            pretty = "; ".join(
                f"{k}: {v}" for k, v in notes.items() if isinstance(v, (str, int, float, bool))
            )
            if pretty:
                lines.append(f"<p><b>Detected details:</b> {html_escape(pretty)}</p>")
        if item.get("category") == devclassify.PROTECTED:
            lines.append(
                "<p style='color:#FF9F0A'><b>PoofMac will not remove this.</b> "
                "It is protected: active development data, user archives, or "
                "outside the approved scope.</p>"
            )
        self.dev_detail.setHtml("<br>".join(lines))

    # ── Cleanup flow ─────────────────────────────────────────────────────────

    def _on_dev_cleanup_confirm(self) -> None:
        if not self._dev_checked:
            QMessageBox.information(self, "Developer Storage", "Check items to trash first.")
            return
        # Final confirmation screen before any destructive action.
        listing_rows = []
        for path, item in sorted(self._dev_checked.items()):
            listing_rows.append(f"  · {item.get('label', path)}  ({path})")
            if len(listing_rows) >= 15:
                break
        more = f"\n  … and {len(self._dev_checked) - 15} more" if len(self._dev_checked) > 15 else ""
        answer = QMessageBox.question(
            self,
            "Confirm cleanup",
            "Move the selected developer-storage items to the Trash?\n\n"
            + "\n".join(listing_rows) + more + "\n\n"
            "• Fits safely: Xcode can rebuild caches; old build products are recovered simply.\n"
            "• Not recoverable after the Trash is emptied.\n"
            "• PoofMac re-checks each item immediately before trashing and skips anything that looks unsafe.\n"
            "• Disk space is only freed once the Trash is emptied.\n\nProceed?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        plan = [
            {"path": p, **(meta or {})} for p, meta in self._dev_checked.items()
        ]
        self.dev_confirm_btn.setEnabled(False)
        self.dev_cancel_btn.setEnabled(True)
        self.dev_status.setText("Cleaning up… (revalidating each item before it is trashed)")
        self._dev_cleanup_worker = DevCleanupWorker(self._dev_engine, plan)
        self._dev_cleanup_worker.item_progress.connect(
            lambda i, n, p: self.dev_status.setText(f"[{i}/{n}] {p}")
        )
        self._dev_cleanup_worker.cleanup_done.connect(self._on_dev_cleanup_done)
        self._dev_cleanup_worker.start()

    def _on_dev_cleanup_done(self, report) -> None:
        self._dev_cleanup_worker = None
        self.dev_confirm_btn.setEnabled(False)
        self.dev_cancel_btn.setEnabled(False)
        s = report.summary()
        lines = [f"{s['trashed']} item(s) moved to Trash."]
        if s["errors"]:
            lines.append(f"{s['errors']} failed — see status below.")
        if s["blocked"]:
            lines.append(f"{s['blocked']} blocked by safety checks.")
        lines.append("Space is recovered once the Trash is emptied.")
        self.dev_status.setText(" ".join(lines))
        # Remove successful rows (path no longer exists) and keep failures.
        for row in range(self.dev_table.rowCount() - 1, -1, -1):
            item = self.dev_table.item(row, COL_DEV_PATH)
            if item and not Path(item.text()).exists():
                self.dev_table.removeRow(row)
        self._dev_checked.clear()
        for r in report.results:
            if r.action not in ("trashed",):
                self.dev_status.setText(
                    self.dev_status.text() + f"  {r.path}: {r.detail}"
                )


def html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
