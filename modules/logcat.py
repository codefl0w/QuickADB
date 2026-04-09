"""
logcat.py - Live adb logcat viewer for QuickADB. Filters by severity level and colorcodes automatically. Allows exporting output.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime
from typing import Optional

from util.resource import get_clean_env, get_root_dir
from util.toolpaths import ToolPaths
from util.devicemanager import DeviceManager
from util.thememanager import ThemeManager

root_dir = get_root_dir()
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QTextCharFormat, QTextCursor, QSyntaxHighlighter
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QComboBox,
    QVBoxLayout,
    QWidget,
)


THREADTIME_LEVEL_RE = re.compile(
    r"^\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+\s+\d+\s+\d+\s+([VDIWEAFS])\s+"
)
FALLBACK_LEVEL_RE = re.compile(r"\b([VDIWEAFS])/[^\s:]+")


class LogcatWorker(QThread):
    line_ready = pyqtSignal(str, str)
    status_changed = pyqtSignal(str)
    error_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(bool, str)
    START_TAIL_COUNT = "1"

    def __init__(self, adb_path: str, serial_args: list[str]):
        super().__init__()
        self.adb_path = adb_path
        self.serial_args = list(serial_args)
        self.process: Optional[subprocess.Popen] = None
        self._stop_requested = False

    def stop(self):
        self._stop_requested = True
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
            except Exception:
                pass

    def run(self):
        # Start from the latest entry so the viewer doesn't spend seconds replaying
        # the entire device log buffer before it becomes responsive.
        command = [
            self.adb_path,
            *self.serial_args,
            "logcat",
            "-T",
            self.START_TAIL_COUNT,
            "-v",
            "threadtime",
        ]
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW

        last_line = ""
        try:
            self.status_changed.emit("Starting adb logcat...")
            self.process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=get_clean_env(),
                creationflags=creationflags,
            )

            if self.process.stdout is None:
                raise RuntimeError("Could not read logcat output.")

            self.status_changed.emit("Logcat is running.")
            while not self._stop_requested:
                line = self.process.stdout.readline()
                if not line:
                    if self.process.poll() is not None:
                        break
                    continue

                text = line.rstrip("\r\n")
                if not text:
                    continue

                last_line = text
                self.line_ready.emit(text, self._parse_level(text))

            if self.process.poll() is None:
                try:
                    self.process.terminate()
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)

            if self._stop_requested:
                self.finished_signal.emit(True, "Logcat stopped.")
                return

            return_code = self.process.returncode if self.process else 0
            if return_code not in (0, None):
                raise RuntimeError(last_line or f"adb logcat exited with code {return_code}.")

            self.finished_signal.emit(False, "Logcat ended.")
        except Exception as exc:
            self.error_signal.emit(str(exc))
            self.finished_signal.emit(self._stop_requested, "Logcat stopped." if self._stop_requested else "Logcat failed.")
        finally:
            if self.process and self.process.stdout is not None:
                try:
                    self.process.stdout.close()
                except Exception:
                    pass
            self.process = None

    @staticmethod
    def _parse_level(line: str) -> str:
        match = THREADTIME_LEVEL_RE.search(line)
        if match:
            return match.group(1)
        match = FALLBACK_LEVEL_RE.search(line)
        if match:
            return match.group(1)
        return "U"


class LogcatHighlighter(QSyntaxHighlighter):
    LEVEL_COLORS = {
        "V": "#a9b3c1",
        "D": "#89CFF0",
        "I": "#77DD77",
        "W": "#FFB347",
        "E": "#FF6961",
        "F": "#ff66cc",
        "S": "#7d8590",
        "U": ThemeManager.TEXT_COLOR_PRIMARY,
    }

    def highlightBlock(self, text: str):
        level_code = LogcatWorker._parse_level(text)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(self.LEVEL_COLORS.get(level_code, ThemeManager.TEXT_COLOR_PRIMARY)))
        self.setFormat(0, len(text), fmt)


class LogcatWindow(QMainWindow):
    LEVEL_ORDER = {"V": 0, "D": 1, "I": 2, "W": 3, "E": 4, "F": 5, "S": 6}
    LEVEL_LABELS = {
        "V": "Verbose+",
        "D": "Debug+",
        "I": "Info+",
        "W": "Warning+",
        "E": "Error+",
        "F": "Fatal",
        "S": "Silent",
    }
    MAX_BUFFER_LINES = 8000

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("QuickADB Logcat")
        self.setMinimumSize(980, 620)

        self.adb_path = ToolPaths.instance().adb
        self.worker: Optional[LogcatWorker] = None
        self.entries: list[tuple[str, str]] = []

        self._build_ui()
        ThemeManager.apply_theme(self)
        self.highlighter = LogcatHighlighter(self.output_text.document())
        self._refresh_device_label()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(15, 10, 15, 10)
        layout.setSpacing(10)

        title = QLabel("Live Logcat")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_font = title.font()
        title_font.setPointSize(14)
        title_font.setBold(True)
        title.setFont(title_font)
        layout.addWidget(title)

        subtitle = QLabel("Stream adb logcat output from the selected device, filter by severity, and export the result.")
        subtitle.setWordWrap(True)
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(subtitle)

        info_frame = QFrame()
        info_layout = QHBoxLayout(info_frame)
        info_layout.setContentsMargins(8, 8, 8, 8)
        info_layout.setSpacing(12)

        self.device_label = QLabel("Selected Device: Unknown")
        self.device_label.setWordWrap(True)
        info_layout.addWidget(self.device_label, 1)

        info_layout.addWidget(QLabel("Minimum Level:"))
        self.level_combo = QComboBox()
        self.level_combo.addItem("All", None)
        for level_code in ("V", "D", "I", "W", "E", "F"):
            self.level_combo.addItem(self.LEVEL_LABELS[level_code], level_code)
        self.level_combo.currentIndexChanged.connect(self._rebuild_output)
        info_layout.addWidget(self.level_combo)
        layout.addWidget(info_frame)

        button_frame = QFrame()
        button_layout = QHBoxLayout(button_frame)
        button_layout.setContentsMargins(8, 8, 8, 8)
        button_layout.setSpacing(8)

        self.start_button = QPushButton("Start Logcat")
        self.start_button.clicked.connect(self.start_logcat)
        button_layout.addWidget(self.start_button)

        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_logcat)
        button_layout.addWidget(self.stop_button)

        self.clear_button = QPushButton("Clear Output")
        self.clear_button.clicked.connect(self.clear_output)
        button_layout.addWidget(self.clear_button)

        self.export_button = QPushButton("Export Output")
        self.export_button.clicked.connect(self.export_output)
        button_layout.addWidget(self.export_button)
        button_layout.addStretch()
        layout.addWidget(button_frame)

        self.output_text = QPlainTextEdit()
        self.output_text.setReadOnly(True)
        self.output_text.setUndoRedoEnabled(False)
        self.output_text.document().setMaximumBlockCount(self.MAX_BUFFER_LINES)
        layout.addWidget(self.output_text, 1)

        self.status_label = QLabel("Ready.")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

    def _refresh_device_label(self):
        device_text = "No device selected"
        dm = DeviceManager.instance()
        if dm.selected_serial:
            for device in dm.devices:
                if device.get("serial") == dm.selected_serial:
                    device_text = f"{device.get('name', dm.selected_serial)} ({dm.selected_serial})"
                    break
            else:
                device_text = dm.selected_serial
        elif len(dm.devices) == 1:
            only = dm.devices[0]
            device_text = f"{only.get('name', only.get('serial', 'Unknown'))} ({only.get('serial', 'Unknown')})"
        self.device_label.setText(f"Selected Device: {device_text}")

    def _minimum_level(self) -> Optional[int]:
        level_code = self.level_combo.currentData()
        if not level_code:
            return None
        return self.LEVEL_ORDER.get(str(level_code), None)

    def _passes_filter(self, level_code: str) -> bool:
        minimum = self._minimum_level()
        if minimum is None:
            return True
        level_value = self.LEVEL_ORDER.get(level_code, -1)
        return level_value >= minimum

    def _append_line(self, text: str):
        self.output_text.insertPlainText(text + "\n")
        self.output_text.moveCursor(QTextCursor.MoveOperation.End)
        self.output_text.ensureCursorVisible()

    def _rebuild_output(self):
        self.output_text.clear()
        visible_lines = [text for text, level_code in self.entries if self._passes_filter(level_code)]
        if visible_lines:
            self.output_text.setPlainText("\n".join(visible_lines) + "\n")

    def _set_running_state(self, running: bool):
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)

    def _ensure_device_ready(self) -> bool:
        dm = DeviceManager.instance()
        if not dm.devices:
            dm.refresh()
        self._refresh_device_label()

        if not dm.devices:
            QMessageBox.information(self, "Logcat", "No devices detected. Connect a device first.")
            return False
        if len(dm.devices) > 1 and not dm.selected_serial:
            QMessageBox.information(self, "Logcat", "Select a device from the main window first.")
            return False
        return True

    def start_logcat(self):
        if self.worker and self.worker.isRunning():
            return
        if not self._ensure_device_ready():
            return

        serial_args = DeviceManager.instance().serial_args()
        self.worker = LogcatWorker(self.adb_path, serial_args)
        self.worker.line_ready.connect(self._on_line_ready)
        self.worker.status_changed.connect(self._on_status_changed)
        self.worker.error_signal.connect(self._on_error)
        self.worker.finished_signal.connect(self._on_finished)
        self.worker.finished.connect(self._cleanup_worker)
        self._set_running_state(True)
        self.status_label.setText("Starting logcat...")
        self.worker.start()

    def stop_logcat(self):
        if self.worker and self.worker.isRunning():
            self.status_label.setText("Stopping logcat...")
            self.worker.stop()

    def clear_output(self):
        self.entries.clear()
        self.output_text.clear()
        self.status_label.setText("Output cleared.")

    def export_output(self):
        default_name = f"logcat_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Logcat Output",
            default_name,
            "Text Files (*.txt);;All Files (*)",
        )
        if not file_path:
            return
        try:
            with open(file_path, "w", encoding="utf-8") as handle:
                handle.write(self.output_text.toPlainText())
            self.status_label.setText(f"Exported logcat output to {file_path}")
        except OSError as exc:
            QMessageBox.critical(self, "Export Failed", str(exc))

    def _on_line_ready(self, text: str, level_code: str):
        self.entries.append((text, level_code))
        if len(self.entries) > self.MAX_BUFFER_LINES:
            self.entries = self.entries[-self.MAX_BUFFER_LINES :]
        if self._passes_filter(level_code):
            self._append_line(text)

    def _on_status_changed(self, message: str):
        self.status_label.setText(message)

    def _on_error(self, message: str):
        self.status_label.setText(message)
        self.entries.append((message, "E"))
        self._append_line(message)

    def _on_finished(self, _stopped_by_user: bool, message: str):
        self.status_label.setText(message)

    def _cleanup_worker(self):
        self._set_running_state(False)
        self.worker = None

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            self.worker.stop()
            self.worker.wait(2000)
        super().closeEvent(event)


def run_logcat_window():
    existing = QApplication.instance()
    if existing is None:
        app = QApplication(sys.argv)
        window = LogcatWindow()
        window.show()
        sys.exit(app.exec())

    window = LogcatWindow()
    window.show()
    return window


if __name__ == "__main__":
    run_logcat_window()
