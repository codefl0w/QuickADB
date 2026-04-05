'''
quickadb.py - Main page. Handles some variables and can do simple adb / fastboot tasks, so it's esentially enough to run on its own.
Other adb tasks that require more code, such as the device spec dialog, are handled by the adbfunc.py module.

'''
import sys
import os
import threading
import subprocess
import time
from datetime import datetime
from functools import partial
from typing import Optional, List, Tuple, Callable

# Setup root dir to allow imports relative to project root
from util.resource import get_root_dir, resource_path, get_clean_env, open_url_safe
from util.toolpaths import ToolPaths
from util.devicemanager import DeviceManager
root_dir = get_root_dir()
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from modules.terminal import show_terminal_window
from modules.payloaddumper import show_payload_dumper_window
from modules.fossmarket import run_foss_market
from modules.gsiflasher import GSIFlasherUI
from modules.magiskmanager import run_root_manager
from modules.superdumper import show_super_img_dumper
from modules.fileexplorer import ADBFileExplorer
import modules.appmanager as appmanager
from modules.partitionmanager import PartitionManager
from util.thememanager import ThemeManager
from util.updater import UpdateManager
import main.adbfunc as adbfunc
import modules.bootanimcreator as bootanimcreator

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QPushButton, QLabel, QFrame,
    QGridLayout, QVBoxLayout, QHBoxLayout, QTextEdit, QMessageBox,
    QFileDialog, QScrollArea, QDialog, QTextBrowser, QComboBox, QSizePolicy
)
from PyQt6.QtGui import QTextCursor, QColor
from PyQt6.QtCore import Qt, QThread, pyqtSignal

try:
    from PyQt6.QtSvgWidgets import QSvgWidget
except ImportError:
    QSvgWidget = None


from main.adbfunc import CommandRunner


class DeviceRefreshWorker(QThread):
    """Background thread to refresh ADB and Fastboot devices without hanging the UI."""
    finished = pyqtSignal(list)

    def run(self):
        dm = DeviceManager.instance()
        devices = dm.refresh()
        self.finished.emit(devices)

class QuickADBApp(QMainWindow):
    # Main window

    # Constants
    APP_VERSION = "V5.0.0"
    APP_SUFFIX = "Full"
    BUTTON_WIDTH = 150
    BUTTON_HEIGHT = 40
    GITHUB_URL = "https://github.com/codefl0w/QuickADB"
    XDA_URL = "https://xdaforums.com/t/new-quickadb-v4-adb-app-manager-file-explorer-gsi-flasher-and-more.4781847/"
    DONATE_URL = "https://buymeacoffee.com/fl0w" # please?
    CONTACT_URL = "https://codefl0w.xyz/contact"
    CHANGELOG_URL_TEMPLATE = "https://raw.githubusercontent.com/codefl0w/QuickADB/{ref}/res/whatsnew.html"

    def __init__(self):
        super().__init__()

        # State
        self.adb_version = "Unknown"
        self.fastboot_version = "Unknown"
        self.platform_tools_path = ToolPaths.instance().platform_tools_dir
        self.command_runner = None
        self.payload_dumper_window = None
        self.super_dumper_window = None
        self.terminal_window = None
        self.file_explorer_window = None
        self.app_manager_window = None
        self.gsi_window = None
        self.partition_manager = None
        self.bootanim_creator_window = None
        self.foss_market_window = None

        # Init
        self.init_ui()
        ThemeManager.apply_theme(self)
        self.fetch_versions()
        self.log_initial_info()
        self._init_updater()
        adbfunc.add_methods_to_class(self)
        self.check_for_update()

    # UI Setup

    def init_ui(self):
        self.setWindowTitle("QuickADB")
        self.setMinimumSize(1050, 620)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(15, 10, 15, 10)
        # Top section: Logo (center) and Device Selector (right)
        top_layout = QGridLayout()
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setColumnStretch(0, 1) # Left spacer stretch
        top_layout.setColumnStretch(1, 0) # Center logo
        top_layout.setColumnStretch(2, 1) # Right device selector stretch

        # Logo
        self.logo_container = QWidget()
        self.logo_layout = QVBoxLayout(self.logo_container)
        self.logo_layout.setContentsMargins(0, 0, 0, 0)
        self.logo_widget = None
        self._refresh_logo_for_current_theme()
        top_layout.addWidget(self.logo_container, 0, 1, alignment=Qt.AlignmentFlag.AlignCenter)

        # Device selector dropdown
        device_container = QWidget()
        device_layout = QHBoxLayout(device_container)
        device_layout.setContentsMargins(0, 0, 0, 0)
        device_layout.addWidget(QLabel("Selected Device:"))
        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(150)
        self.device_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.device_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.device_combo.setPlaceholderText("No devices, refresh to check")
        self.device_combo.currentIndexChanged.connect(self._on_device_selected)
        device_layout.addWidget(self.device_combo)

        self.refresh_devices_btn = QPushButton("⟳")
        self.refresh_devices_btn.setFixedWidth(30)
        self.refresh_devices_btn.setToolTip("Refresh device list")
        self.refresh_devices_btn.clicked.connect(self.check_devices)
        device_layout.addWidget(self.refresh_devices_btn)

        top_layout.addWidget(device_container, 0, 2, alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        main_layout.addLayout(top_layout)

        # Menu buttons
        menu_frame = QFrame()
        menu_layout = QHBoxLayout(menu_frame)
        menu_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(menu_frame)

        menu_buttons = {
            "ADB Operations": self.show_adb_commands, "Fastboot Operations": self.show_fastboot_commands,
            "Flashing Operations": self.show_flashing_commands, "Open Terminal": self.open_terminal,
            "Advanced": self.show_advanced, "Miscellaneous": self.show_misc
        }
        for text, callback in menu_buttons.items():
            menu_layout.addWidget(self.create_menu_button(text, callback))
        menu_layout.addStretch()

        # Main content area
        content_layout = QHBoxLayout()
        main_layout.addLayout(content_layout, 1)

        # Commands frame (left side)
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setMinimumWidth(690)
        commands_frame = QFrame()
        commands_frame.setFrameShape(QFrame.Shape.StyledPanel)
        self.commands_layout = QGridLayout(commands_frame)
        self.commands_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll_area.setWidget(commands_frame)
        content_layout.addWidget(scroll_area)

        # Logs section (right side)
        logs_frame = QFrame()
        logs_frame.setMaximumWidth(500)
        logs_layout = QVBoxLayout(logs_frame)
        logs_layout.addWidget(QLabel("Logs", alignment=Qt.AlignmentFlag.AlignCenter))
        self.logs_text = QTextEdit()
        self.logs_text.setObjectName("MainLog")
        self.logs_text.setReadOnly(True)
        self.logs_text.setMinimumSize(200, 370)
        logs_layout.addWidget(self.logs_text)
        self.extract_button = QPushButton("Export Logs")
        self.extract_button.clicked.connect(self.export_logs)
        logs_layout.addWidget(self.extract_button, alignment=Qt.AlignmentFlag.AlignCenter)
        content_layout.addWidget(logs_frame)

        # Bottom layout
        bottom_layout = QHBoxLayout()
        main_layout.addLayout(bottom_layout)

        # Bottom left links
        self.version_label = QLabel(f"{self.APP_VERSION} {self.APP_SUFFIX}")
        self.version_label.setStyleSheet("background: none; border: none;")
        bottom_layout.addWidget(self.version_label, alignment=Qt.AlignmentFlag.AlignLeft)
        self.about_label = QLabel("About")
        self.about_label.setStyleSheet("color: #0059ff; text-decoration: underline; background: none; border: none;")
        self.about_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.about_label.mousePressEvent = self.show_about
        bottom_layout.addWidget(self.about_label, alignment=Qt.AlignmentFlag.AlignLeft)
        bottom_layout.addStretch()

        # Bottom right buttons
        bottom_buttons = {
            "View on GitHub": self.view_github, "View XDA Thread": self.view_xda_thread,
            "Buy Me A Coffee": self.donate, "Contact Me": self.contact
        }
        for text, callback in bottom_buttons.items():
            btn = QPushButton(text)
            btn.clicked.connect(callback)
            bottom_layout.addWidget(btn)

        self.show()

    def _make_button(self, text: str, callback: Callable) -> QPushButton:
        """Creates a standard sized button and wires its click callback."""
        button = QPushButton(text)
        button.setFixedSize(self.BUTTON_WIDTH, self.BUTTON_HEIGHT)
        button.clicked.connect(callback)
        return button

    def create_menu_button(self, text: str, callback: Callable) -> QPushButton:
        """Creates a standard menu button."""
        return self._make_button(text, callback)

    def create_command_button(self, text: str, callback: Callable, row: int, column: int) -> QPushButton:
        """Creates a command button and adds it to the grid layout."""
        button = self._make_button(text, callback)
        self.commands_layout.addWidget(button, row, column)
        return button

    def clear_buttons(self):
        """Clears all widgets from the commands layout."""
        while self.commands_layout.count():
            item = self.commands_layout.takeAt(0)
            if widget := item.widget():
                widget.deleteLater()

    # --- Command Execution & Helpers ---

    def _get_executable_path(self, name: str) -> Optional[str]:
        """Returns the full path for an executable via the centralized ToolPaths."""
        tp = ToolPaths.instance()
        if not os.path.isdir(tp.platform_tools_dir):
            QMessageBox.critical(self, "Error", f"platform-tools folder not found at: {tp.platform_tools_dir}")
            return None
        return getattr(tp, name, None) or tp.adb  # fallback

    def run_command_async(self, command: str, description: str, command_type: str):
        """Executes a command asynchronously in a separate thread."""
        current_time = time.strftime("%H:%M:%S")
        self.log_action(f"[{current_time}] Executing {command_type} command: {description}", "#00ffff")

        # Resolve adb/fastboot to absolute paths and inject -s SERIAL for multi-device
        command_stripped = (command or "").lstrip()
        dm = DeviceManager.instance()
        for tool in ("adb", "fastboot"):
            if command_stripped == tool or command_stripped.startswith(tool + " "):
                exe_path = self._get_executable_path(tool)
                if exe_path:
                    remainder = command_stripped[len(tool):].lstrip()
                    # Inject -s SERIAL for ADB/Fastboot commands that target a device
                    serial_flag = ""
                    if tool == "adb" and not dm.is_global_adb_command(remainder):
                        serial_flag = dm.serial_flag()
                    elif tool == "fastboot" and not dm.is_global_fastboot_command(remainder):
                        serial_flag = dm.serial_flag()
                    command = f'"{exe_path}" {serial_flag}{remainder}'
                break

        self.command_runner = CommandRunner(command, self.platform_tools_path)
        self.command_runner.env = get_clean_env()
        self.command_runner.output_signal.connect(self.log_terminal_output)
        self.command_runner.start()


    def _populate_commands_grid(self, commands: List[Tuple[str, Callable]], items_per_row: int = 3):
        """Dynamically creates and places command buttons in a grid."""
        self.clear_buttons()
        for i, (text, callback) in enumerate(commands):
            row, col = divmod(i, items_per_row)
            self.create_command_button(text, callback, row, col)

    # --- Command Button Sections ---

    def show_adb_commands(self): # If a command can be executed without needing a path (e.g. adb devices), it's executed directly. Otherwise, it's executed by adbfunc.py.
        """Populates the grid with ADB command buttons."""
        commands = [
            ("Check for Devices", lambda: self.run_command_async("adb devices", "Check for Devices", "ADB")),
            ("Kill ADB Server", lambda: self.run_command_async("adb kill-server", "Kill ADB Server", "ADB")),
            ("Install APK", self.install_apk),
            ("Reboot Device", lambda: self.run_command_async("adb reboot", "Reboot Device", "ADB")),
            ("Restart ADB Server", lambda: self.run_command_async("adb reconnect", "Restart ADB Server", "ADB")),
            ("Uninstall App", self.uninstall_app),
            ("Reboot to Recovery", lambda: self.run_command_async("adb reboot recovery", "Reboot to Recovery", "ADB")),
            ("Authorize Devices", lambda: self.run_command_async("adb reconnect offline", "Authorize Devices", "ADB")),
            ("ADB Sideload", self.sideload_file),
            ("Reboot to Bootloader", lambda: self.run_command_async("adb reboot bootloader", "Reboot to Bootloader", "ADB")),
            ("ADB Pull", self.open_pull_window),
            ("Get Serial Number", lambda: self.run_command_async("adb get-serialno", "Get Serial Number", "ADB")),
            ("Reboot to EDL (⚠️)", lambda: self.run_command_async("adb reboot edl", "Reboot to EDL", "ADB")),
            ("ADB Push", self.open_push_window),
            ("Wireless ADB", self.show_wireless_adb_ui),
        ]
        self._populate_commands_grid(commands)

    def show_fastboot_commands(self):
        """Populates the grid with Fastboot command buttons."""
        commands = [
            ("List Devices", lambda: self.run_command_async("fastboot devices", "List Fastboot Devices", "Fastboot")),
            ("Get All Variables", lambda: self.run_command_async("fastboot getvar all", "Get All Variables", "Fastboot")),
            ("Check Active Slot", lambda: self.run_command_async("fastboot getvar current-slot", "Check Slot", "Fastboot")),
            ("Reboot to System", lambda: self.run_command_async("fastboot reboot", "Reboot to System", "Fastboot")),
            ("Get Device Codename", lambda: self.run_command_async("fastboot getvar product", "Get Codename", "Fastboot")),
            ("Activate Slot A", lambda: self.run_command_async("fastboot set_active a", "Activate Slot A", "Fastboot")),
            ("Reboot to Recovery", lambda: self.run_command_async("fastboot reboot recovery", "Reboot to Recovery", "Fastboot")),
            ("Check Antirollback", lambda: self.run_command_async("fastboot getvar anti", "Check Antirollback", "Fastboot")),
            ("Activate Slot B", lambda: self.run_command_async("fastboot set_active b", "Activate Slot B", "Fastboot")),
            ("Reboot to Bootloader", lambda: self.run_command_async("fastboot reboot bootloader", "Reboot to Bootloader", "Fastboot")),
            ("Check Unlockability", lambda: self.run_command_async("fastboot flashing get_unlock_ability", "Check Unlockability", "Fastboot")),
            ("Unlock Bootloader (⚠️)", lambda: self.run_command_async("fastboot flashing unlock", "Unlock Bootloader", "Fastboot")),
            ("Reboot to Fastbootd", lambda: self.run_command_async("fastboot reboot fastboot", "Reboot to Fastbootd", "Fastboot")),
            ("Get Token", lambda: self.run_command_async("fastboot getvar token", "Get Token", "Fastboot")),
            ("Lock Bootloader (⚠️)", lambda: self.run_command_async("fastboot flashing lock", "Lock Bootloader", "Fastboot")),
        ]
        self._populate_commands_grid(commands)

    def show_flashing_commands(self):
        """Populates the grid with partition flashing command buttons."""
        partitions = [
            "boot", "init_boot", "system", "vbmeta", "vbmeta_system", "vbmeta_vendor",
            "cust", "userdata", "preloader (⚠️)", "logo", "super", "recovery", "dtbo",
            "gz", "lk (⚠️)", "nvdata (⚠️)", "nvram (⚠️)", "tee", "md1img (⚠️)", "rescue",
            "dpm", "efuse", "scp", "spmfw", "modem (⚠️)", "abl (⚠️)", "xbl (⚠️)", "sbl (⚠️)"
        ]
        commands = [
            (f"Flash {p.split(' ')[0]}", partial(self.select_and_flash_image, p.split(' ')[0])) for p in partitions
        ]
        self._populate_commands_grid(commands, items_per_row=4)

    def show_advanced(self):
        """Populates the grid with advanced tool buttons."""
        commands = [
            ("Magisk Manager", self.launch_root_manager),
            ("App Manager", self.launch_app_manager),
            ("File Explorer", self.open_file_explorer),
            ("GSI Flasher", self.open_gsi_flasher),
            ("Partition Manager (#)", self.open_partition_manager),
            ("Dump super.img", self.launch_super_dumper),
            ("Dump payload.bin", self.show_payload_dumper),
        ]
        self._populate_commands_grid(commands)

    def show_misc(self):
        """Populates the grid with miscellaneous command buttons."""
        commands = [("Device Specifications", self.show_device_info),
                    ("Boot Animation Creator", self.launch_bootanim_creator),
                    ("App Market", self.launch_foss_market)]
        self._populate_commands_grid(commands)

    # --- Feature Implementations & Launchers ---

    def select_and_flash_image(self, partition_name: str):
        """Opens a file dialog to select an image and flash it to the specified partition."""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Image File", "", "Image Files (*.img);;Binary Files (*.bin);;All Files (*)"
        )
        if file_path:
            cmd = f'fastboot flash {partition_name} "{file_path}"'
            self.run_command_async(cmd, f"Flashing {partition_name}", "Fastboot")

    def open_file_explorer(self):
        def _make():
            w = ADBFileExplorer()
            w.show()
            return w
        self._focus_or_launch('file_explorer_window', _make)

    def launch_app_manager(self):
        self._focus_or_launch(
            'app_manager_window',
            lambda: appmanager.run_app_manager(self.platform_tools_path)
        )

    def open_gsi_flasher(self):
        def _make():
            w = GSIFlasherUI()
            w.show()
            return w
        self._focus_or_launch('gsi_window', _make)

    def open_partition_manager(self):
        def _make():
            w = PartitionManager(self.platform_tools_path)
            w.show()
            return w
        self._focus_or_launch('partition_manager', _make)

    def show_payload_dumper(self):
        self._focus_or_launch(
            'payload_dumper_window',
            lambda: show_payload_dumper_window(QApplication.instance())[1]
        )

    def launch_super_dumper(self):
        self._focus_or_launch(
            'super_dumper_window',
            lambda: show_super_img_dumper(self)
        )

    def launch_foss_market(self):
        self._focus_or_launch(
            'foss_market_window',
            lambda: run_foss_market()
        )

    def launch_root_manager(self):
        self._focus_or_launch(
            'root_manager_window',
            lambda: run_root_manager()
        )

    def _focus_or_launch(self, attr: str, factory: Callable):
        """Raise the existing window if visible, otherwise create it via factory."""
        try:
            window = getattr(self, attr, None)
            if window is not None and window.isVisible():
                window.raise_()
                window.activateWindow()
                return
        except RuntimeError:
            pass
        setattr(self, attr, factory())

    def open_terminal(self):
        self._focus_or_launch(
            'terminal_window',
            lambda: show_terminal_window(
                app_version=self.APP_VERSION, app_suffix=self.APP_SUFFIX,
                adb_version=self.adb_version, fastboot_version=self.fastboot_version,
            )
        )

    def launch_bootanim_creator(self):
        """Launches the Boot Animation Creator module."""
        self._focus_or_launch(
            'bootanim_creator_window',
            lambda: bootanimcreator.run_bootanim_creator(self)
        )

    # --- Placeholder Methods. Filled in by adbfunc.py ---
    def install_apk(self): QMessageBox.information(self, "Not Implemented", "Install APK feature coming soon.")
    def uninstall_app(self): QMessageBox.information(self, "Not Implemented", "Uninstall App feature coming soon.")
    def sideload_file(self): QMessageBox.information(self, "Not Implemented", "ADB Sideload feature coming soon.")
    def show_device_info(self): QMessageBox.information(self, "Not Implemented", "Device specifications feature coming soon.")
    def show_wireless_adb_ui(self):
        if callable(getattr(self, "_wireless_adb_impl", None)):
            self._wireless_adb_impl()
        else:
            QMessageBox.information(self, "Not Implemented", "Wireless ADB UI not yet implemented.")

    # --- Device Management ---

    # Pastel state colors for the device dropdown
    _STATE_COLORS = {
        "device":       "#77DD77",  # pastel green
        "unauthorized": "#FDFD96",  # pastel yellow
        "recovery":     "#89CFF0",  # pastel blue
        "offline":      "#FF6961",  # pastel red
        "fastboot":     "#FFB347",  # pastel orange
    }

    def check_devices(self):
        """Scan connected devices and populate the dropdown."""
        self.log_action("Scanning for devices...", "#00ffff")
        self.refresh_devices_btn.setEnabled(False)
        self.device_combo.setPlaceholderText("Scanning...")

        # Use background thread to avoid UI freeze
        self._device_worker = DeviceRefreshWorker()
        self._device_worker.finished.connect(self._on_devices_refreshed)
        self._device_worker.start()

    def _on_devices_refreshed(self, devices):
        self.refresh_devices_btn.setEnabled(True)
        self.device_combo.setPlaceholderText("No devices, refresh to check")

        self._populate_device_combo(devices)
        if not devices:
            self.log_action("No devices found.", "#ff6666")
        else:
            for d in devices:
                color = self._STATE_COLORS.get(d['state'], '#ffffff')
                self.log_action(f"  {d['name']}  ({d['serial']})  [{d['state']}]", color)
            self.log_action(f"{len(devices)} device(s) detected.", "#77DD77")

    def _populate_device_combo(self, devices):
        """Fill the device dropdown with detected devices and color-coded dots."""
        self.device_combo.blockSignals(True)
        self.device_combo.clear()

        dm = DeviceManager.instance()
        for dev in devices:
            state = dev["state"]
            color = self._STATE_COLORS.get(state, "#aaaaaa")
            dot = "●"
            label = f"{dot} {dev['name']}  [{state}]"
            self.device_combo.addItem(label, userData=dev["serial"])
            # Colorize the dot via the item's foreground
            idx = self.device_combo.count() - 1
            self.device_combo.setItemData(idx, QColor(color), Qt.ItemDataRole.ForegroundRole)

        # Restore selection
        if dm.selected_serial:
            for i in range(self.device_combo.count()):
                if self.device_combo.itemData(i) == dm.selected_serial:
                    self.device_combo.setCurrentIndex(i)
                    break
        elif self.device_combo.count() > 0:
            self.device_combo.setCurrentIndex(0)

        self.device_combo.blockSignals(False)

        # Trigger selection update
        if self.device_combo.count() > 0:
            self._on_device_selected(self.device_combo.currentIndex())

    def _on_device_selected(self, index):
        """Update DeviceManager when user picks a device from the dropdown."""
        if index < 0:
            return
        serial = self.device_combo.itemData(index)
        if serial:
            DeviceManager.instance().selected_serial = serial

    def refresh_devices(self):
        """Alias for check_devices, callable from other modules."""
        self.check_devices()

    # --- Versioning & Update Check ---
    def _init_updater(self):
        self.updater = UpdateManager(
            parent=self,
            current_version=self.APP_VERSION,
            app_name="QuickADB",
            repo_owner="codefl0w",
            repo_name="QuickADB",
            releases_url=f"{self.GITHUB_URL}/releases",
            changelog_url_template=self.CHANGELOG_URL_TEMPLATE,
            log_callback=self.log_action,
        )

    def fetch_versions(self):
        """Fetches ADB and Fastboot versions in separate threads."""
        threading.Thread(target=self._fetch_version, args=("adb", "version", "adb_version"), daemon=True).start()
        threading.Thread(target=self._fetch_version, args=("fastboot", "--version", "fastboot_version"), daemon=True).start()

    def _fetch_version(self, executable: str, command: str, version_attr: str):
        """Worker function to get version info."""
        try:
            exe_path = self._get_executable_path(executable)
            if not exe_path: return
            # Windows specific: Create a new process group and hide the console window.
            creationflags = 0
            if sys.platform == "win32":
                creationflags = (
                    subprocess.CREATE_NEW_PROCESS_GROUP |
                    subprocess.CREATE_NO_WINDOW
                )

            result = subprocess.run(
                [exe_path, command],
                capture_output=True,
                text=True,
                check=False,
                env=get_clean_env(),
                creationflags=creationflags
            )
            if result.returncode == 0:
                setattr(self, version_attr, result.stdout.splitlines()[0])
            else:
                setattr(self, version_attr, f"{executable} not found or error occurred")
        except Exception as e:
            setattr(self, version_attr, f"Error fetching version: {e}")

    def check_for_update(self, manual: bool = False):
        if hasattr(self, "updater"):
            self.updater.check_for_updates(manual=manual)

    # --- Logging ---

    def _append_log(self, text: str, color: Optional[str]):
        """Low-level helper: append colored text to the log widget."""
        self.logs_text.moveCursor(QTextCursor.MoveOperation.End)
        if color:
            self.logs_text.setTextColor(QColor(color))
        else:
            self.logs_text.setTextColor(QColor(ThemeManager.TEXT_COLOR_PRIMARY))
        self.logs_text.insertPlainText(text + "\n")
        self.logs_text.setTextColor(QColor(ThemeManager.TEXT_COLOR_PRIMARY))
        self.logs_text.ensureCursorVisible()

    def log_action(self, action: str, color: Optional[str] = None):
        self._append_log(action, color)

    def log_terminal_output(self, output: str, tag: str = ""):
        color = "#ff4d4d" if "error" in tag.lower() else "#2edf85"
        self._append_log(output, color)

    def log_initial_info(self):
        current_time = datetime.now().strftime("%d/%m/%Y, %H:%M")
        self.logs_text.setTextColor(QColor(ThemeManager.TEXT_COLOR_PRIMARY))
        self.logs_text.append(f"{current_time} - Current version: {self.APP_VERSION} {self.APP_SUFFIX}\n\n"
                              "Some features are experimental and are not expected to work on every device. "
                              "Look for the warning icon (⚠️) to distinguish them.\n")

    def export_logs(self):
        file_path, _ = QFileDialog.getSaveFileName(self, "Save Logs", "", "Text Files (*.txt)")
        if file_path:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(self.logs_text.toPlainText())

    # --- Event Handlers & Dialogs ---
    def _open_url(self, url: str): open_url_safe(url)
    def view_github(self): self._open_url(self.GITHUB_URL)
    def view_xda_thread(self): self._open_url(self.XDA_URL)
    def donate(self): self._open_url(self.DONATE_URL) # please?
    def contact(self): self._open_url(self.CONTACT_URL)

    def show_about(self, event=None):
        dialog = QDialog(self)
        dialog.setWindowTitle("About QuickADB")
        layout = QVBoxLayout(dialog)
        info_label = QLabel(
            f"<h2>QuickADB - ADB & Fastboot Utility</h2>"
            f"<p>Version: {self.APP_VERSION} {self.APP_SUFFIX}</p>"
            f"<p>{self.adb_version}</p><p>{self.fastboot_version}</p><hr>"
            f"<p style='font-size: 8pt;'>Credits:<br>"
            f"- payload-dumper-go by ssut — payload.bin extraction<br>"
            f"- SDK Platform Tools by Google - ADB and fastboot binaries<br>"
            f"- PyQt6 by Riverbank Computing - Python adaptation of Qt6<br>"
            f"- Magisk by topjohnwu - Magisk internals &amp; logo<br>"
            f"- All beta testers - Bug detection and improvement suggestions<br><br><br>"
            f"© 2026 fl0w</p>"
        )
        info_label.setTextFormat(Qt.TextFormat.RichText); info_label.setWordWrap(True)
        layout.addWidget(info_label)

        buttons = {"Select Theme": self.show_theme_selector, "What's New?": self.show_whats_new,
                   "Check for Updates": lambda: self.check_for_update(manual=True), "Close": dialog.close}
        for text, callback in buttons.items():
            btn = QPushButton(text); btn.clicked.connect(callback)
            layout.addWidget(btn)

        dialog.exec()

    def show_theme_selector(self):
        dialog = QDialog(self); dialog.setWindowTitle("Select Theme")
        layout = QVBoxLayout(dialog); layout.addWidget(QLabel("Available Themes:"))
        themes_path = resource_path("themes")
        found = False
        if os.path.exists(themes_path):
            for filename in os.listdir(themes_path):
                if filename.lower().endswith(".qss"):
                    theme_name = os.path.splitext(filename)[0].capitalize()
                    btn = QPushButton(theme_name)
                    btn.clicked.connect(partial(self._apply_selected_theme, filename))
                    layout.addWidget(btn)
                    found = True
        default_btn = QPushButton("Default"); default_btn.clicked.connect(partial(self._apply_selected_theme, "none"))
        layout.addWidget(default_btn)
        if not found: layout.addWidget(QLabel("(No .qss themes found)"))
        dialog.exec()

    def _apply_selected_theme(self, theme_name: str):
        ThemeManager.write_theme_name(theme_name)
        ThemeManager.apply_theme(self)
        self._refresh_logo_for_current_theme()
        self._recolor_existing_logs()

    def _refresh_logo_for_current_theme(self):
        if not QSvgWidget:
            return
        ThemeManager.ensure_default()
        try:
            theme_name = ThemeManager.read_theme_name().strip().lower()
        except Exception:
            theme_name = "dark.qss"
        dark_themes = {"dark.qss", "high_contrast.qss", "android.qss"}
        logo_name = "logo.svg" if theme_name in dark_themes else "logo_light.svg"
        svg_path = resource_path(os.path.join("res", logo_name))
        if not os.path.exists(svg_path):
            svg_path = resource_path("res/logo.svg")
        if self.logo_widget is not None:
            self.logo_layout.removeWidget(self.logo_widget)
            self.logo_widget.deleteLater()
        self.logo_widget = QSvgWidget(svg_path)
        self.logo_widget.setFixedSize(250, 65)
        self.logo_layout.addWidget(self.logo_widget, 0, Qt.AlignmentFlag.AlignCenter)

    def _recolor_existing_logs(self):
        existing_text = self.logs_text.toPlainText()
        self.logs_text.clear()
        self.logs_text.setTextColor(QColor(ThemeManager.TEXT_COLOR_PRIMARY))
        if existing_text:
            self.logs_text.setPlainText(existing_text)
            self.logs_text.moveCursor(QTextCursor.MoveOperation.End)

    def show_whats_new(self):
        html_path = resource_path("res/whatsnew.html")
        if not os.path.exists(html_path):
            QMessageBox.warning(self, "File Not Found", f"'whatsnew.html' not found at:\n{html_path}")
            return
        try:
            with open(html_path, 'r', encoding='utf-8') as f: html_content = f.read()
            dialog = QDialog(self); dialog.setWindowTitle("What's New?"); dialog.resize(700, 500)
            layout = QVBoxLayout(dialog)
            browser = QTextBrowser(); browser.setHtml(html_content); browser.setOpenExternalLinks(True)
            layout.addWidget(browser)
            close_button = QPushButton("Close"); close_button.clicked.connect(dialog.accept)
            button_layout = QHBoxLayout(); button_layout.addStretch(); button_layout.addWidget(close_button)
            layout.addLayout(button_layout)
            dialog.exec()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not load 'whatsnew.html':\n{e}")
