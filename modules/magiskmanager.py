"""
magiskmanager.py - Magisk manager UI for QuickADB. Handles the rooting  & module management frontend.
See magiskpatcher.py for the backend.
"""

from __future__ import annotations

import html
import os
import re
import sys
from typing import Optional

from util.magiskpatcher import (
    MagiskDeviceInfo,
    MagiskModuleInfo,
    MagiskPatchOptions,
    MagiskPatchResult,
    MagiskPatcher,
    MagiskReleaseInfo,
)
from util.resource import get_root_dir, resource_path
from util.thememanager import ThemeManager

root_dir = get_root_dir()
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from PyQt6.QtCore import QThread, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStatusBar,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)


def _version_sort_key(name: str) -> tuple[int, ...]:
    match = re.search(r"(\d+(?:\.\d+)*)", name or "")
    if not match:
        return (0,)
    return tuple(int(part) for part in match.group(1).split("."))


class PhoneInfoCard(QWidget):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setMinimumWidth(320)
        self.setMinimumHeight(520)

        self.content = QWidget(self)
        self.content.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        content_layout = QVBoxLayout(self.content)
        content_layout.setContentsMargins(20, 28, 20, 20)
        content_layout.setSpacing(12)

        self.badge_label = QLabel("Connected Device")
        self.badge_label.setStyleSheet(
            "background: rgba(76, 201, 240, 0.18); color: #8fe7ff; "
            "border-radius: 10px; padding: 6px 10px; font-weight: 600;"
        )
        content_layout.addWidget(self.badge_label, alignment=Qt.AlignmentFlag.AlignCenter)

        self.device_name_label = QLabel("No device selected")
        self.device_name_label.setWordWrap(True)
        self.device_name_label.setStyleSheet("font-size: 20px; font-weight: 700; color: #ffffff;")
        content_layout.addWidget(self.device_name_label)

        self.subtitle_label = QLabel("Device and Magisk status")
        self.subtitle_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setStyleSheet("color: #a9b3c1;")
        content_layout.addWidget(self.subtitle_label)

        self.rows: dict[str, QLabel] = {}
        for label_text in (
            "Android SDK",
            "Primary ABI",
            "ABI List",
            "Magisk ABI Match",
            "Root Method",
            "Root Version",
        ):
            pill_label = QLabel()
            pill_label.setTextFormat(Qt.TextFormat.RichText)
            pill_label.setWordWrap(True)
            pill_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pill_label.setMinimumHeight(40)
            pill_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
            pill_label.setStyleSheet(
                "background: rgba(255, 255, 255, 0.06); "
                "border-radius: 15px",
            )
            content_layout.addWidget(pill_label)
            self.rows[label_text] = pill_label

        content_layout.addStretch()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        margin_x = max(18, int(self.width() * 0.11))
        margin_top = max(24, int(self.height() * 0.085))
        screen_width = max(220, self.width() - (margin_x * 2))
        screen_height = max(360, self.height() - (margin_top * 2) - 18)
        self.content.setGeometry(margin_x, margin_top + 26, screen_width, screen_height - 44)

    def paintEvent(self, event):
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        outer = QRectF(10, 10, self.width() - 20, self.height() - 20)
        screen = QRectF(outer.left() + 16, outer.top() + 22, outer.width() - 32, outer.height() - 44)

        body_path = QPainterPath()
        body_path.addRoundedRect(outer, 34, 34)
        painter.fillPath(body_path, QColor("#121722"))
        painter.setPen(QPen(QColor("#2bc2f7"), 2))
        painter.drawPath(body_path)

        screen_path = QPainterPath()
        screen_path.addRoundedRect(screen, 24, 24)
        painter.fillPath(screen_path, QColor("#1a2332"))
        painter.setPen(QPen(QColor("#243146"), 1.2))
        painter.drawPath(screen_path)

        speaker = QRectF(self.width() / 2 - 34, outer.top() + 10, 68, 6)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#2a3445"))
        painter.drawRoundedRect(speaker, 3, 3)

        camera = QRectF(speaker.right() + 10, outer.top() + 8.5, 9, 9)
        painter.setBrush(QColor("#0c1118"))
        painter.drawEllipse(camera)
        painter.setBrush(QColor("#2bc2f7"))
        painter.drawEllipse(QRectF(camera.left() + 2.5, camera.top() + 2.5, 4, 4))



    def set_info(self, info: Optional[MagiskDeviceInfo]):
        if info is None:
            self.device_name_label.setText("No device selected")
            self.subtitle_label.setText("Connect a device to inspect root and module state.")
            for label_text, label in self.rows.items():
                self._set_row_text(label_text, "-")
            return

        self.device_name_label.setText(info.device_name or "Unknown Device")
        self.subtitle_label.setText("Device and Magisk status")
        self._set_row_text("Android SDK", info.sdk or "Unknown")
        self._set_row_text("Primary ABI", info.abi or "Unknown")
        self._set_row_text("ABI List", ", ".join(info.abi_list) if info.abi_list else "Unknown")
        if info.selected_abi:
            self._set_row_text("Magisk ABI Match", f"{info.selected_abi} (supported)")
        else:
            supported = ", ".join(info.supported_abis) if info.supported_abis else "None"
            self._set_row_text("Magisk ABI Match", f"No match. Magisk supports: {supported}")
        self._set_row_text("Root Method", info.root_method or "Not Available")
        self._set_row_text("Root Version", info.root_version or "Unavailable")

    def _set_row_text(self, title: str, value: str):
        label = self.rows.get(title)
        if label is None:
            return
        title_html = html.escape(title)
        value_html = html.escape(value or "-").replace("\n", "<br>")
        label.setText(
            f"<div style='text-align:center;'>"
            f"<span style='color:#8aa0b7; font-size:11px; font-weight:600;'>{title_html}</span><br>"
            f"<span style='color:#f4fbff; font-size:13px; font-weight:600;'>{value_html}</span>"
            f"</div>"
        )


class DeviceInfoWorker(QThread):
    info_ready = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, source_path: str):
        super().__init__()
        self.source_path = source_path

    def run(self):
        try:
            patcher = MagiskPatcher()
            info = patcher.detect_device_info(self.source_path or None)
            self.info_ready.emit(info)
        except Exception as exc:
            self.failed.emit(str(exc))


class ModuleListWorker(QThread):
    modules_ready = pyqtSignal(object)
    failed = pyqtSignal(str)

    def run(self):
        try:
            patcher = MagiskPatcher()
            modules = patcher.list_modules()
            self.modules_ready.emit(modules)
        except Exception as exc:
            self.failed.emit(str(exc))


class ModuleActionWorker(QThread):
    status_changed = pyqtSignal(str)
    log_message = pyqtSignal(str, str)
    action_finished = pyqtSignal(str, str)
    failed = pyqtSignal(str)

    def __init__(
        self,
        action: str,
        module_path: str = "",
        module_name: str = "",
        zip_path: str = "",
    ):
        super().__init__()
        self.action = action
        self.module_path = module_path
        self.module_name = module_name
        self.zip_path = zip_path

    def run(self):
        try:
            patcher = MagiskPatcher()
            if self.action == "install":
                installed_path = patcher.install_module_zip(
                    self.zip_path,
                    log_callback=lambda message: self.log_message.emit(message, "#89CFF0"),
                    status_callback=self.status_changed.emit,
                )
                zip_name = os.path.basename(installed_path)
                self.action_finished.emit(
                    self.action,
                    f"Installed module package {zip_name}. Reboot may be required before it becomes active.",
                )
                return

            if self.action == "enable":
                self.status_changed.emit("Enabling module...")
                patcher.set_module_enabled(self.module_path, True)
                name = self.module_name or os.path.basename(self.module_path.rstrip("/"))
                self.action_finished.emit(self.action, f"Enabled module {name}. Reboot may be required.")
                return

            if self.action == "disable":
                self.status_changed.emit("Disabling module...")
                patcher.set_module_enabled(self.module_path, False)
                name = self.module_name or os.path.basename(self.module_path.rstrip("/"))
                self.action_finished.emit(self.action, f"Disabled module {name}. Reboot may be required.")
                return

            raise RuntimeError(f"Unsupported module action: {self.action}")
        except Exception as exc:
            self.failed.emit(str(exc))


class MagiskDownloadWorker(QThread):
    progress_changed = pyqtSignal(int)
    log_message = pyqtSignal(str, str)
    finished_download = pyqtSignal(str, object)
    failed = pyqtSignal(str)

    def __init__(self, release: Optional[MagiskReleaseInfo]):
        super().__init__()
        self.release = release

    def run(self):
        try:
            patcher = MagiskPatcher()
            release = self.release or patcher.fetch_latest_release_info()
            apk_path = patcher.download_release(
                release,
                progress_callback=self.progress_changed.emit,
                log_callback=lambda message: self.log_message.emit(message, "#89CFF0"),
            )
            self.finished_download.emit(apk_path, release)
        except Exception as exc:
            self.failed.emit(str(exc))


class MagiskPatchWorker(QThread):
    progress_changed = pyqtSignal(int)
    status_changed = pyqtSignal(str)
    log_message = pyqtSignal(str, str)
    patch_finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, options: MagiskPatchOptions):
        super().__init__()
        self.options = options

    def run(self):
        try:
            patcher = MagiskPatcher()
            result = patcher.patch_image(
                self.options,
                log_callback=lambda message: self.log_message.emit(message, "#89CFF0"),
                status_callback=self.status_changed.emit,
                progress_callback=self.progress_changed.emit,
            )
            self.patch_finished.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class RootManagerWindow(QMainWindow):
    MAGISK_VERSION_CHOICES = (
        ("Latest (API)", None),
        ("v30.7", "30.7"),
        ("v30.6", "30.6"),
        ("v30.5", "30.5"),
        ("v30.4", "30.4"),
        ("v29.0", "29.0"),
    )

    def __init__(self):
        super().__init__()
        self.setWindowTitle("QuickADB Magisk Manager")
        self.resize(1200, 800)

        self.current_device_info: Optional[MagiskDeviceInfo] = None
        self.current_modules: list[MagiskModuleInfo] = []
        self.module_map: dict[str, MagiskModuleInfo] = {}
        self.device_worker: Optional[DeviceInfoWorker] = None
        self.module_worker: Optional[ModuleListWorker] = None
        self.module_action_worker: Optional[ModuleActionWorker] = None
        self.download_worker: Optional[MagiskDownloadWorker] = None
        self.patch_worker: Optional[MagiskPatchWorker] = None
        self.phone_cards: list[PhoneInfoCard] = []

        self._build_ui()
        ThemeManager.apply_theme(self)
        self._set_default_paths()
        self._refresh_device_info()
        self._refresh_modules()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(14, 14, 14, 14)
        root_layout.setSpacing(12)

        header = QFrame()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(6, 4, 6, 4)
        header_layout.setSpacing(12)

        self.logo_label = QLabel()
        self.logo_label.setFixedSize(42, 42)
        self.logo_label.setPixmap(self._load_header_logo())
        header_layout.addWidget(self.logo_label)

        title_layout = QVBoxLayout()
        title_layout.setSpacing(2)
        title_label = QLabel("Magisk Manager")
        title_label.setStyleSheet("font-size: 24px; font-weight: 700;")
        subtitle_label = QLabel("Root your device with Magisk and manage installed modules.")
        subtitle_label.setStyleSheet("color: #8aa0b7;")
        title_layout.addWidget(title_label)
        title_layout.addWidget(subtitle_label)
        header_layout.addLayout(title_layout)
        header_layout.addStretch()

        root_layout.addWidget(header)

        self.stack = QStackedWidget()
        root_layout.addWidget(self.stack, 1)

        self.home_page = self._build_home_page()
        self.patch_page = self._build_patch_page()
        self.stack.addWidget(self.home_page)
        self.stack.addWidget(self.patch_page)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("Ready")

    def _build_home_page(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        left_column = QFrame()
        left_layout = QVBoxLayout(left_column)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)

        module_header = QHBoxLayout()
        module_title_layout = QVBoxLayout()
        module_title_layout.setSpacing(2)
        module_title = QLabel("Installed Modules")
        module_title.setStyleSheet("font-size: 18px; font-weight: 700;")
        module_subtitle = QLabel("Loaded from /data/adb/modules on the connected rooted device.")
        module_subtitle.setStyleSheet("color: #8aa0b7;")
        module_title_layout.addWidget(module_title)
        module_title_layout.addWidget(module_subtitle)
        module_header.addLayout(module_title_layout)
        module_header.addStretch()

        self.refresh_modules_button = QPushButton("Refresh Modules")
        self.refresh_modules_button.clicked.connect(self._refresh_modules)
        module_header.addWidget(self.refresh_modules_button)
        left_layout.addLayout(module_header)

        self.module_tree = QTreeWidget()
        self.module_tree.setObjectName("moduleTree")
        self.module_tree.setHeaderLabels(["Module", "Version", "State", "ID"])
        self.module_tree.setRootIsDecorated(False)
        self.module_tree.setAlternatingRowColors(True)
        self.module_tree.setUniformRowHeights(True)
        self.module_tree.setColumnWidth(0, 280)
        self.module_tree.setColumnWidth(1, 130)
        self.module_tree.setColumnWidth(2, 120)
        self.module_tree.setMinimumWidth(620)
        self.module_tree.itemSelectionChanged.connect(self._update_module_action_state)
        left_layout.addWidget(self.module_tree, 1)

        module_actions = QHBoxLayout()
        module_actions.setSpacing(8)

        self.install_module_button = QPushButton("Install Module ZIP")
        self.install_module_button.clicked.connect(self._install_module_zip)
        self.install_module_button.setToolTip("Install a Magisk module ZIP using the on-device Magisk CLI.")
        module_actions.addWidget(self.install_module_button)

        self.enable_module_button = QPushButton("Enable")
        self.enable_module_button.clicked.connect(lambda: self._toggle_selected_module(True))
        module_actions.addWidget(self.enable_module_button)

        self.disable_module_button = QPushButton("Disable")
        self.disable_module_button.clicked.connect(lambda: self._toggle_selected_module(False))
        module_actions.addWidget(self.disable_module_button)
        module_actions.addStretch()
        left_layout.addLayout(module_actions)

        self.module_status_label = QLabel("Reading module list...")
        self.module_status_label.setWordWrap(True)
        self.module_status_label.setStyleSheet("color: #8aa0b7;")
        left_layout.addWidget(self.module_status_label)

        sidebar = QFrame()
        sidebar.setFixedWidth(360)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(12)

        self.home_phone_card = self._create_phone_card()
        sidebar_layout.addWidget(self.home_phone_card, 1)

        self.root_device_button = QPushButton("Root Device")
        self.root_device_button.setMinimumHeight(32)
        self.root_device_button.setStyleSheet("font-size: 14px; font-weight: 700;")
        self.root_device_button.clicked.connect(self._show_patch_page)
        sidebar_layout.addWidget(self.root_device_button)

        self.refresh_device_button = QPushButton("Refresh Device Info")
        self.refresh_device_button.clicked.connect(self._refresh_device_info)
        sidebar_layout.addWidget(self.refresh_device_button)

        layout.addWidget(left_column, 1)
        layout.addWidget(sidebar)
        return page

    def _build_patch_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        top_row = QHBoxLayout()
        self.back_button = QPushButton("Back to Modules")
        self.back_button.clicked.connect(self._show_home_page)
        top_row.addWidget(self.back_button)

        patch_title = QLabel("Patch Boot Images")
        patch_title.setStyleSheet("font-size: 18px; font-weight: 700;")
        top_row.addWidget(patch_title)
        top_row.addStretch()

        version_label = QLabel("Magisk Version")
        top_row.addWidget(version_label)

        self.version_combo = QComboBox()
        for label, value in self.MAGISK_VERSION_CHOICES:
            self.version_combo.addItem(label, value)
        top_row.addWidget(self.version_combo)

        self.download_button = QPushButton("Download Selected")
        self.download_button.clicked.connect(self._download_selected_magisk)
        top_row.addWidget(self.download_button)
        layout.addLayout(top_row)

        content_row = QHBoxLayout()
        content_row.setSpacing(14)

        left_column = QWidget()
        left_layout = QVBoxLayout(left_column)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)

        source_group = QGroupBox("Magisk Source")
        source_layout = QFormLayout(source_group)
        self.magisk_source_edit = QLineEdit()
        self.magisk_source_edit.textChanged.connect(self._on_source_path_changed)

        source_buttons = QHBoxLayout()
        folder_button = QPushButton("Select Folder")
        folder_button.clicked.connect(self._select_magisk_folder)
        apk_button = QPushButton("Select APK")
        apk_button.clicked.connect(self._select_magisk_apk)
        source_buttons.addWidget(folder_button)
        source_buttons.addWidget(apk_button)
        source_buttons.addStretch()
        source_buttons_widget = QWidget()
        source_buttons_widget.setLayout(source_buttons)

        self.source_summary_label = QLabel("Select an extracted Magisk folder or an official Magisk APK.")
        self.source_summary_label.setWordWrap(True)

        source_layout.addRow("Path", self.magisk_source_edit)
        source_layout.addRow("", source_buttons_widget)
        source_layout.addRow("Summary", self.source_summary_label)
        left_layout.addWidget(source_group)

        image_group = QGroupBox("Patch Target")
        image_layout = QFormLayout(image_group)

        self.boot_image_edit = QLineEdit()
        image_button = QPushButton("Choose Image")
        image_button.clicked.connect(self._select_boot_image)
        image_row = QHBoxLayout()
        image_row.addWidget(self.boot_image_edit)
        image_row.addWidget(image_button)
        image_widget = QWidget()
        image_widget.setLayout(image_row)

        self.output_dir_edit = QLineEdit()
        output_button = QPushButton("Choose Folder")
        output_button.clicked.connect(self._select_output_dir)
        output_row = QHBoxLayout()
        output_row.addWidget(self.output_dir_edit)
        output_row.addWidget(output_button)
        output_widget = QWidget()
        output_widget.setLayout(output_row)

        image_layout.addRow("Boot Image", image_widget)
        image_layout.addRow("Output Folder", output_widget)
        left_layout.addWidget(image_group)

        flags_group = QGroupBox("Patch Flags")
        flags_layout = QHBoxLayout(flags_group)
        self.keep_verity_checkbox = QCheckBox("Keep Verity")
        self.keep_force_encrypt_checkbox = QCheckBox("Keep Force Encrypt")
        self.patch_vbmeta_checkbox = QCheckBox("Patch vbmeta Flags")
        self.recovery_mode_checkbox = QCheckBox("Recovery Mode")
        self.legacy_sar_checkbox = QCheckBox("Legacy SAR")
        flags_layout.addWidget(self.keep_verity_checkbox)
        flags_layout.addWidget(self.keep_force_encrypt_checkbox)
        flags_layout.addWidget(self.patch_vbmeta_checkbox)
        flags_layout.addWidget(self.recovery_mode_checkbox)
        flags_layout.addWidget(self.legacy_sar_checkbox)
        left_layout.addWidget(flags_group)

        action_row = QHBoxLayout()
        self.patch_button = QPushButton("Patch with Magisk")
        self.patch_button.setMinimumHeight(46)
        self.patch_button.setStyleSheet("font-size: 14px; font-weight: 700;")
        self.patch_button.clicked.connect(self._start_patch)
        action_row.addWidget(self.patch_button)
        action_row.addStretch()
        left_layout.addLayout(action_row)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        left_layout.addWidget(self.progress_bar)

        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        left_layout.addWidget(self.log_output, 1)

        right_sidebar = QFrame()
        right_sidebar.setFixedWidth(360)
        right_layout = QVBoxLayout(right_sidebar)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(12)

        self.patch_phone_card = self._create_phone_card()
        right_layout.addWidget(self.patch_phone_card, 1)

        patch_hint = QLabel(
            "Patch images here, then flash the generated output from the flashing section when you are ready."
        )
        patch_hint.setWordWrap(True)
        patch_hint.setStyleSheet("color: #8aa0b7;")
        right_layout.addWidget(patch_hint)

        content_row.addWidget(left_column, 1)
        content_row.addWidget(right_sidebar)
        layout.addLayout(content_row, 1)
        return page

    def _create_phone_card(self) -> PhoneInfoCard:
        card = PhoneInfoCard()
        self.phone_cards.append(card)
        return card

    def _load_header_logo(self):
        candidates = [
            resource_path(os.path.join("res", "Magisk_Logo.png")),
            resource_path(os.path.join("res", "Magisk_logo.svg")),
            resource_path(os.path.join("res", "logo.svg")),
            resource_path(os.path.join("res", "logo_light.svg")), # Placeholder
        ]
        for candidate in candidates:
            if os.path.exists(candidate):
                return QIcon(candidate).pixmap(42, 42)
        return QIcon(resource_path(os.path.join("res", "toolicon.ico"))).pixmap(42, 42)

    def _set_default_paths(self):
        default_source = self._find_default_magisk_source()
        if default_source:
            self.magisk_source_edit.setText(default_source)
            self.source_summary_label.setText(f"Detected local Magisk source: {os.path.basename(default_source)}")
        self.output_dir_edit.setText(root_dir)

    def _find_default_magisk_source(self) -> str:
        candidates: list[str] = []
        try:
            for name in os.listdir(root_dir):
                full_path = os.path.join(root_dir, name)
                if os.path.isdir(full_path) and name.startswith("Magisk-v"):
                    candidates.append(full_path)
                elif os.path.isfile(full_path) and name.startswith("Magisk-v") and name.endswith(".apk"):
                    candidates.append(full_path)
        except OSError:
            return ""
        if not candidates:
            return ""
        candidates.sort(key=lambda path: _version_sort_key(os.path.basename(path)), reverse=True)
        return candidates[0]

    def _on_source_path_changed(self, path: str):
        text = (path or "").strip()
        if not text:
            self.source_summary_label.setText("Select an extracted Magisk folder or an official Magisk APK.")
            return

        base = os.path.basename(text)
        if os.path.isdir(text):
            self.source_summary_label.setText(f"Using extracted Magisk folder: {base}")
        elif os.path.isfile(text):
            self.source_summary_label.setText(f"Using Magisk package: {base}")
        else:
            self.source_summary_label.setText(base)

    def _select_magisk_folder(self):
        directory = QFileDialog.getExistingDirectory(self, "Select Extracted Magisk Folder", root_dir)
        if directory:
            self.magisk_source_edit.setText(directory)
            self._refresh_device_info()

    def _select_magisk_apk(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Magisk APK",
            root_dir,
            "Magisk APK (*.apk *.zip);;All Files (*)",
        )
        if file_path:
            self.magisk_source_edit.setText(file_path)
            self._refresh_device_info()

    def _select_boot_image(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Boot Image",
            root_dir,
            "Image Files (*.img *.bin);;All Files (*)",
        )
        if file_path:
            self.boot_image_edit.setText(file_path)
            if not self.output_dir_edit.text().strip():
                self.output_dir_edit.setText(os.path.dirname(file_path))

    def _select_output_dir(self):
        directory = QFileDialog.getExistingDirectory(
            self,
            "Select Output Folder",
            self.output_dir_edit.text().strip() or root_dir,
        )
        if directory:
            self.output_dir_edit.setText(directory)

    def _refresh_device_info(self):
        if self.device_worker and self.device_worker.isRunning():
            return
        self.statusBar().showMessage("Detecting connected device...")
        self.refresh_device_button.setEnabled(False)
        self.device_worker = DeviceInfoWorker(self.magisk_source_edit.text().strip())
        self.device_worker.info_ready.connect(self._on_device_info_ready)
        self.device_worker.failed.connect(self._on_device_info_failed)
        self.device_worker.finished.connect(self._cleanup_device_worker)
        self.device_worker.start()

    def _cleanup_device_worker(self):
        self.refresh_device_button.setEnabled(True)
        self.device_worker = None

    def _on_device_info_ready(self, info: MagiskDeviceInfo):
        self.current_device_info = info
        for card in self.phone_cards:
            card.set_info(info)
        self._update_module_action_state()
        self.statusBar().showMessage("Device information refreshed.", 4000)

    def _on_device_info_failed(self, message: str):
        self.current_device_info = None
        for card in self.phone_cards:
            card.set_info(None)
        self._update_module_action_state()
        self.log(message, "#ff6961")
        self.statusBar().showMessage(message, 6000)

    def _refresh_modules(self):
        if self.module_worker and self.module_worker.isRunning():
            return
        self.refresh_modules_button.setEnabled(False)
        self.module_status_label.setText("Reading /data/adb/modules...")
        self.current_modules = []
        self.module_map = {}
        self.module_tree.clear()
        self._update_module_action_state()
        self.module_worker = ModuleListWorker()
        self.module_worker.modules_ready.connect(self._on_modules_ready)
        self.module_worker.failed.connect(self._on_modules_failed)
        self.module_worker.finished.connect(self._cleanup_module_worker)
        self.module_worker.start()

    def _cleanup_module_worker(self):
        self.refresh_modules_button.setEnabled(True)
        self.module_worker = None

    def _on_modules_ready(self, modules: list[MagiskModuleInfo]):
        self.current_modules = modules
        self.module_map = {module.path: module for module in modules}
        self.module_tree.clear()
        if not modules:
            self.module_status_label.setText("No modules found in /data/adb/modules.")
            self._update_module_action_state()
            return

        for module in modules:
            item = QTreeWidgetItem([
                module.name,
                module.version,
                module.state,
                module.module_id,
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, module.path)
            item.setToolTip(0, module.path)
            item.setToolTip(3, module.path)
            self.module_tree.addTopLevelItem(item)

        self.module_status_label.setText(f"Loaded {len(modules)} module(s).")
        self.module_tree.resizeColumnToContents(0)
        self._update_module_action_state()

    def _on_modules_failed(self, message: str):
        self.current_modules = []
        self.module_map = {}
        self.module_tree.clear()
        self.module_status_label.setText(
            "Could not read /data/adb/modules. A rooted Magisk shell may not be available yet."
        )
        self._update_module_action_state()
        self.log(message, "#ffb347")

    def _show_patch_page(self):
        self.stack.setCurrentWidget(self.patch_page)

    def _show_home_page(self):
        self.stack.setCurrentWidget(self.home_page)

    def _selected_release(self) -> Optional[MagiskReleaseInfo]:
        version = self.version_combo.currentData()
        if not version:
            return None
        return MagiskPatcher.release_info_for_version(str(version))

    def _download_selected_magisk(self):
        if self.download_worker and self.download_worker.isRunning():
            return
        self.download_button.setEnabled(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.statusBar().showMessage("Downloading Magisk...")
        self.download_worker = MagiskDownloadWorker(self._selected_release())
        self.download_worker.progress_changed.connect(self._set_progress_value)
        self.download_worker.log_message.connect(self.log)
        self.download_worker.finished_download.connect(self._on_download_finished)
        self.download_worker.failed.connect(self._on_download_failed)
        self.download_worker.finished.connect(self._cleanup_download_worker)
        self.download_worker.start()

    def _cleanup_download_worker(self):
        self.download_button.setEnabled(True)
        self.download_worker = None

    def _on_download_finished(self, apk_path: str, release: MagiskReleaseInfo):
        self.magisk_source_edit.setText(apk_path)
        self.log(f"Magisk {release.version} is ready at {apk_path}", "#77DD77")
        self.statusBar().showMessage(f"Downloaded Magisk {release.version}.", 5000)
        self._refresh_device_info()

    def _on_download_failed(self, message: str):
        self.log(message, "#ff6961")
        self.statusBar().showMessage(message, 6000)
        QMessageBox.warning(self, "Magisk Download Failed", message)

    def _selected_module(self) -> Optional[MagiskModuleInfo]:
        item = self.module_tree.currentItem()
        if item is None:
            return None
        module_path = str(item.data(0, Qt.ItemDataRole.UserRole) or "")
        if not module_path:
            return None
        return self.module_map.get(module_path)

    def _supports_magisk_cli(self) -> bool:
        method = (self.current_device_info.root_method if self.current_device_info else "").strip().upper()
        return method == "MAGISKSU"

    def _update_module_action_state(self):
        module = self._selected_module()
        action_running = bool(self.module_action_worker and self.module_action_worker.isRunning())
        module_loading = bool(self.module_worker and self.module_worker.isRunning())
        install_allowed = self._supports_magisk_cli() and not action_running and not module_loading
        can_toggle = module is not None and not action_running and not module_loading
        is_disabled = bool(module and "disabled" in module.state)
        is_pending_remove = bool(module and "remove" in module.state)

        self.install_module_button.setEnabled(install_allowed)
        if self._supports_magisk_cli():
            self.install_module_button.setToolTip("Install a Magisk module ZIP using the on-device Magisk CLI.")
        else:
            self.install_module_button.setToolTip("Module ZIP installation is only enabled when the detected root method is MagiskSU.")

        self.enable_module_button.setEnabled(can_toggle and is_disabled and not is_pending_remove)
        self.disable_module_button.setEnabled(can_toggle and not is_disabled and not is_pending_remove)

    def _install_module_zip(self):
        if not self._supports_magisk_cli():
            QMessageBox.information(
                self,
                "Magisk Manager",
                "Module ZIP installation is only available when the detected root method is MagiskSU.",
            )
            return
        if self.module_action_worker and self.module_action_worker.isRunning():
            return

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Magisk Module ZIP",
            root_dir,
            "Magisk Module (*.zip);;All Files (*)",
        )
        if not file_path:
            return

        self.module_status_label.setText(f"Installing module ZIP: {os.path.basename(file_path)}")
        self._start_module_action(ModuleActionWorker("install", zip_path=file_path))

    def _toggle_selected_module(self, enabled: bool):
        module = self._selected_module()
        if module is None:
            QMessageBox.information(self, "Magisk Manager", "Select a module first.")
            return
        if self.module_action_worker and self.module_action_worker.isRunning():
            return

        action_name = "enable" if enabled else "disable"
        self.module_status_label.setText(f"{action_name.title()}ing module {module.name}...")
        self._start_module_action(
            ModuleActionWorker(
                action_name,
                module_path=module.path,
                module_name=module.name,
            )
        )

    def _start_module_action(self, worker: ModuleActionWorker):
        self.module_action_worker = worker
        self.refresh_modules_button.setEnabled(False)
        self.install_module_button.setEnabled(False)
        self.enable_module_button.setEnabled(False)
        self.disable_module_button.setEnabled(False)
        worker.status_changed.connect(self._on_module_action_status)
        worker.log_message.connect(self.log)
        worker.action_finished.connect(self._on_module_action_finished)
        worker.failed.connect(self._on_module_action_failed)
        worker.finished.connect(self._cleanup_module_action_worker)
        worker.start()

    def _cleanup_module_action_worker(self):
        self.module_action_worker = None
        if not (self.module_worker and self.module_worker.isRunning()):
            self.refresh_modules_button.setEnabled(True)
        self._update_module_action_state()

    def _on_module_action_status(self, message: str):
        self.module_status_label.setText(message)
        self.statusBar().showMessage(message)

    def _on_module_action_finished(self, action: str, message: str):
        self.log(message, "#77DD77")
        self.module_status_label.setText(message)
        self.statusBar().showMessage(message, 8000)
        self._refresh_modules()

    def _on_module_action_failed(self, message: str):
        self.log(message, "#ff6961")
        self.module_status_label.setText(message)
        self.statusBar().showMessage(message, 8000)
        QMessageBox.warning(self, "Module Action Failed", message)
        self._refresh_modules()

    def _start_patch(self):
        source_path = self.magisk_source_edit.text().strip()
        boot_image_path = self.boot_image_edit.text().strip()
        output_dir = self.output_dir_edit.text().strip()

        if not source_path:
            QMessageBox.information(self, "Magisk Manager", "Select a Magisk folder or APK first.")
            return
        if not boot_image_path:
            QMessageBox.information(self, "Magisk Manager", "Select a boot image first.")
            return
        if not output_dir:
            QMessageBox.information(self, "Magisk Manager", "Select an output folder first.")
            return
        if self.patch_worker and self.patch_worker.isRunning():
            return

        options = MagiskPatchOptions(
            magisk_source=source_path,
            boot_image_path=boot_image_path,
            output_dir=output_dir,
            keep_verity=self.keep_verity_checkbox.isChecked(),
            keep_force_encrypt=self.keep_force_encrypt_checkbox.isChecked(),
            patch_vbmeta_flag=self.patch_vbmeta_checkbox.isChecked(),
            recovery_mode=self.recovery_mode_checkbox.isChecked(),
            legacy_sar=self.legacy_sar_checkbox.isChecked(),
        )

        self.patch_button.setEnabled(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.log("Starting Magisk patch flow...", "#89CFF0")
        self.patch_worker = MagiskPatchWorker(options)
        self.patch_worker.progress_changed.connect(self._set_progress_value)
        self.patch_worker.status_changed.connect(self._on_worker_status_changed)
        self.patch_worker.log_message.connect(self.log)
        self.patch_worker.patch_finished.connect(self._on_patch_finished)
        self.patch_worker.failed.connect(self._on_patch_failed)
        self.patch_worker.finished.connect(self._cleanup_patch_worker)
        self.patch_worker.start()

    def _cleanup_patch_worker(self):
        self.patch_button.setEnabled(True)
        self.patch_worker = None
        if self.progress_bar.maximum() == 0:
            self.progress_bar.setRange(0, 100)

    def _on_worker_status_changed(self, message: str):
        self.statusBar().showMessage(message)
        self.log(message, "#d7e1ed")

    def _set_progress_value(self, value: int):
        if value < 0:
            self.progress_bar.setRange(0, 0)
            return
        if self.progress_bar.maximum() == 0:
            self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(value)

    def _on_patch_finished(self, result: MagiskPatchResult):
        self.log(
            f"Patched image saved to {result.output_path} using Magisk {result.magisk_version} ({result.device_abi}).",
            "#77DD77",
        )
        self.statusBar().showMessage("Magisk patch completed.", 8000)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        QMessageBox.information(
            self,
            "Patch Complete",
            f"Patched image created:\n\n{result.output_path}\n\nFlash it from the flashing section when you are ready.",
        )

    def _on_patch_failed(self, message: str):
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.log(message, "#ff6961")
        self.statusBar().showMessage("Magisk patch failed.", 8000)
        QMessageBox.warning(self, "Patch Failed", message)

    def log(self, message: str, color: Optional[str] = None):
        self.log_output.moveCursor(QTextCursor.MoveOperation.End)
        self.log_output.setTextColor(QColor(color or ThemeManager.TEXT_COLOR_PRIMARY))
        self.log_output.insertPlainText(message + "\n")
        self.log_output.setTextColor(QColor(ThemeManager.TEXT_COLOR_PRIMARY))
        self.log_output.ensureCursorVisible()


def run_root_manager():
    existing = QApplication.instance()
    app = existing or QApplication(sys.argv)
    window = RootManagerWindow()
    window.show()
    if not existing:
        return app.exec()
    return window


if __name__ == "__main__":
    run_root_manager()
