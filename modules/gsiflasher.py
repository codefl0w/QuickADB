"""

gsiflasher.py - QuickADB's automated GSI flasher module.
Detects device state via adb and fastboot, calculates partition sizes and automates the flashing process.

"""

import sys
import os
import subprocess
import time
import webbrowser

from util.resource import get_root_dir, resource_path
from util.toolpaths import ToolPaths
root_dir = get_root_dir()
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from util.thememanager import ThemeManager
from main.adbfunc import CommandRunner

from PyQt6.QtCore import Qt, pyqtSignal, QThread, QTimer
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QTextEdit,
    QFileDialog, QMessageBox, QFrame, QLabel, QApplication
)
from PyQt6.QtGui import QFont

# Optional pyusb. Will always be present when QuickADB is compiled.
# Used to detect connected devices that aren't detected by fastboot.
try:
    import usb.core
    import usb.util
    _PYUSB_AVAILABLE = True
except Exception:
    usb = None
    _PYUSB_AVAILABLE = False


class DeviceScannerWorker(QThread):
    """
    State machine worker for checking connected ADB and Fastboot devices securely in the background.
    Emits specific states and logs back to the UI.
    """
    log_msg = pyqtSignal(str)
    status_msg = pyqtSignal(str)

    # Emits (adb_devices_list, fastboot_devices_list)
    devices_found = pyqtSignal(list, list)

    def __init__(self, platform_tools_path):
        super().__init__()
        self.platform_tools_path = platform_tools_path
        self.running = True

    def run(self):
        adb_cmd = ToolPaths.instance().adb
        fastboot_cmd = ToolPaths.instance().fastboot

        self.status_msg.emit("Checking for connected devices...")
        self.log_msg.emit("[INFO] Starting device scan...")

        try:
            # Windows: suppress console window.
            creationflags = 0
            if os.name == "nt":
                creationflags = (
                    subprocess.CREATE_NEW_PROCESS_GROUP |
                    subprocess.CREATE_NO_WINDOW
                )

            # 1. Check ADB
            adb_proc = subprocess.run(
                [adb_cmd, "devices"],
                capture_output=True, text=True, creationflags=creationflags
            )
            adb_devs = [
                parts[0]
                for line in adb_proc.stdout.splitlines()[1:]
                for parts in [line.split()]
                if len(parts) >= 2 and parts[1].lower() == "device"
            ]

            # 2. Check Fastboot
            fastboot_proc = subprocess.run(
                [fastboot_cmd, "devices"],
                capture_output=True, text=True, creationflags=creationflags
            )
            fb_devs = [
                parts[0]
                for line in fastboot_proc.stdout.splitlines()
                for parts in [line.split()]
                if len(parts) >= 2 and parts[1].lower() in ("fastboot", "device", "recovery", "bootloader")
            ]

            if adb_devs or fb_devs:
                self.log_msg.emit(f"[INFO] Scan complete. ADB: {len(adb_devs)}, Fastboot: {len(fb_devs)}.")
                self.status_msg.emit("Waiting for user input...")
            else:
                self.log_msg.emit("[INFO] No devices detected across ADB or Fastboot.")
                self.status_msg.emit("No devices found. Please connect a device and try again.")

            self.devices_found.emit(adb_devs, fb_devs)

        except Exception as e:
            self.log_msg.emit(f"[ERROR] Device scan failed: {str(e)}")
            self.status_msg.emit("Error during device scan.")


class GSIFlasherUI(QMainWindow):

    def __init__(self, platform_tools_path=None, parent=None):
        super().__init__(parent)

        self.platform_tools_path = platform_tools_path or os.path.join(root_dir, "platform-tools")
        self.gsi_image_path = None
        self.system_partition_available = False
        self.fastbootd_confirmed = False
        self.command_threads = []  # Keep QThread refs alive to avoid GC
        self.scanner_thread = None

        self.setWindowTitle("QuickADB GSI Flasher")
        self.setMinimumSize(700, 500)
        self.setup_ui()
        ThemeManager.apply_theme(self)

    # ---- Properties ----

    @property
    def _adb(self) -> str:
        return self._tool_path("adb")

    @property
    def _fastboot(self) -> str:
        return self._tool_path("fastboot")

    # ---- UI Setup ----

    def setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        title = QLabel("GSI Flasher")
        f = QFont()
        f.setPointSize(14)
        f.setBold(True)
        title.setFont(f)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        # Log output
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMinimumHeight(260)
        layout.addWidget(self.log_output)

        # Top buttons
        btn_frame = QFrame()
        btn_layout = QHBoxLayout(btn_frame)
        btn_layout.setSpacing(8)

        self.recheck_btn = QPushButton("Check Devices")
        self.recheck_btn.clicked.connect(self.on_check_devices_clicked)
        btn_layout.addWidget(self.recheck_btn)

        self.treble_info_btn = QPushButton("Treble Info App")
        self.treble_info_btn.clicked.connect(self.open_treble_info_app)
        btn_layout.addWidget(self.treble_info_btn)

        self.load_gsi_btn = QPushButton("Load GSI Image")
        self.load_gsi_btn.clicked.connect(self.load_gsi_image)
        btn_layout.addWidget(self.load_gsi_btn)

        self.flash_gsi_btn = QPushButton("Flash GSI Image")
        self.flash_gsi_btn.setEnabled(False)
        self.flash_gsi_btn.clicked.connect(self.flash_gsi_image)
        btn_layout.addWidget(self.flash_gsi_btn)

        layout.addWidget(btn_frame)

        # Bottom buttons
        bottom_frame = QFrame()
        bottom_layout = QHBoxLayout(bottom_frame)
        bottom_layout.setSpacing(8)

        self.delete_product_btn = QPushButton("Delete product")
        self.delete_product_btn.clicked.connect(lambda: self.delete_partition("product"))
        bottom_layout.addWidget(self.delete_product_btn)

        self.delete_sys_ext_btn = QPushButton("Delete system_ext")
        self.delete_sys_ext_btn.clicked.connect(lambda: self.delete_partition("system_ext"))
        bottom_layout.addWidget(self.delete_sys_ext_btn)

        self.more_info_btn = QPushButton("More Info")
        self.more_info_btn.clicked.connect(self.open_more_info)
        bottom_layout.addWidget(self.more_info_btn)

        self.reboot_btn = QPushButton("Reboot Device")
        self.reboot_btn.clicked.connect(self.reboot_device)
        bottom_layout.addWidget(self.reboot_btn)

        layout.addWidget(bottom_frame)

        # Status label
        self.status_label = QLabel("Ready. Click 'Check Devices' to begin.")
        f_status = QFont()
        f_status.setItalic(True)
        self.status_label.setFont(f_status)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.status_label)

        # Initial guidance
        self.log("[INFO] This method may not work on Samsung devices unless using custom recovery.")
        self.log("[INFO] Use GSI images equal or higher Android version than stock OS.")

    # ---- Logging ----

    def log(self, message: str):
        ts = time.strftime("%H:%M:%S")
        try:
            self.log_output.append(f"[{ts}] {message}")
            self.log_output.ensureCursorVisible()
        except Exception:
            print(f"[{ts}] {message}")

    # ---- Command execution ----

    def run_command_async(self, command, callback=None):
        """Execute an ADB/Fastboot command via CommandRunner, log output, call callback on finish."""
        from util.devicemanager import DeviceManager
        serial_args = DeviceManager.instance().serial_args()

        if isinstance(command, list):
            if command and ("adb" in command[0] or "fastboot" in command[0]):
                command = [command[0]] + serial_args + command[1:]
            cmd_str = " ".join(f'"{c}"' if " " in c else c for c in command)
        else:
            cmd_str = command

        thread = CommandRunner(cmd_str, self.platform_tools_path)
        captured_output = []

        def handle_output(text, tag):
            self.log(text)
            captured_output.append(text)

        def handle_finished():
            if callback:
                callback("\n".join(captured_output))
            if thread in self.command_threads:
                self.command_threads.remove(thread)

        thread.output_signal.connect(handle_output)
        thread.finished.connect(handle_finished)
        self.command_threads.append(thread)
        thread.start()

    def _tool_path(self, tool_name: str) -> str:
        return getattr(ToolPaths.instance(), tool_name, ToolPaths.instance().adb)

    # ---- Device detection ----

    def on_check_devices_clicked(self):
        self.recheck_btn.setEnabled(False)
        self.flash_gsi_btn.setEnabled(False)
        self.status_label.setText("Starting device scan...")

        if self.scanner_thread and self.scanner_thread.isRunning():
            self.scanner_thread.running = False
            self.scanner_thread.wait()

        self.scanner_thread = DeviceScannerWorker(self.platform_tools_path)
        self.scanner_thread.log_msg.connect(self.log)
        self.scanner_thread.status_msg.connect(self.status_label.setText)
        self.scanner_thread.devices_found.connect(self._handle_scanned_devices)
        self.scanner_thread.finished.connect(lambda: self.recheck_btn.setEnabled(True))
        self.scanner_thread.start()

    def _warn_multiple_devices(self, protocol: str):
        """Warn the user that multiple devices of the given protocol are connected."""
        self.log(f"[WARN] Multiple {protocol} devices detected.")
        QMessageBox.warning(
            self, "Multiple Devices",
            f"More than one {protocol} device detected and none are selected.\n"
            "Please select your target device from the dropdown in the QuickADB main window."
        )
        self.status_label.setText("Select a device in main window.")

    def _handle_scanned_devices(self, adb_devs: list, fb_devs: list):
        """Receive scan results and route to the correct scenario."""
        from util.devicemanager import DeviceManager
        selected = DeviceManager.instance().selected_serial

        if selected:
            if selected in adb_devs:
                adb_devs, fb_devs = [selected], []
            elif selected in fb_devs:
                adb_devs, fb_devs = [], [selected]
            else:
                self.log(f"[WARN] Selected device {selected} not found connected.")
                adb_devs, fb_devs = [], []

        num_adb = len(adb_devs)
        num_fb = len(fb_devs)

        if num_adb == 0 and num_fb == 0:
            if selected:
                self.status_label.setText(f"Device {selected} not found.")
            return

        # Scenario 1: Multiple ADB devices
        if num_adb > 1:
            self._warn_multiple_devices("ADB")
            return

        # Scenario 2: Multiple Fastboot devices
        if num_fb > 1:
            self._warn_multiple_devices("Fastboot")
            return

        # Scenario 3: 1 ADB and 1 Fastboot device simultaneously
        if num_adb == 1 and num_fb == 1:
            self.log("[INFO] Conflict: 1 ADB and 1 Fastboot device found simultaneously.")
            reply = QMessageBox.question(
                self, "Device Conflict Detected",
                "QuickADB detected one device in ADB mode, and another in Fastboot mode.\n\n"
                "Which device would you like to target?\n"
                "Yes = Fastboot Device (Proceed with flash setup)\n"
                "No = ADB Device (Reboot it to Fastboot mode)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.log("[INFO] User elected to proceed with the Fastboot device.")
                self.status_label.setText("Analyzing fastboot partition...")
                self.fetch_fastboot_info()
            else:
                self.log("[INFO] User elected to reboot the ADB device to Fastboot.")
                self.status_label.setText("Rebooting ADB device to fastboot...")
                self.run_command_async(
                    [self._adb, "reboot", "bootloader"],
                    lambda o: self.status_label.setText("Reboot triggered. Please wait 15s, then Check Devices again.")
                )
            return

        # Scenario 4: Exactly 1 ADB device, 0 Fastboot
        if num_adb == 1 and num_fb == 0:
            reply = QMessageBox.question(
                self, "ADB Device Detected",
                "One device is connected via ADB.\nDo you want to reboot it into fastboot mode now?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.log("[INFO] Rebooting device to fastboot...")
                self.status_label.setText("Rebooting device to fastboot...")
                self.run_command_async(
                    [self._adb, "reboot", "fastboot"],
                    lambda o: self.status_label.setText("Reboot triggered. Please wait 15s, then Check Devices again.")
                )
            return

        # Scenario 5: Exactly 1 Fastboot device, 0 ADB
        if num_fb == 1 and num_adb == 0:
            self.log("[INFO] One Fastboot device detected. Proceeding...")
            self.status_label.setText("Analyzing fastboot partition...")
            self.fetch_fastboot_info()

    # ---- Partition & flash helpers ----

    def fetch_fastboot_info(self, output=None):
        """Query device for partition info using fastboot getvar all."""
        self.log("[INFO] Gathering fastboot partition info (fastboot getvar all).")
        self.run_command_async([self._fastboot, "getvar", "all"], self.parse_partition_info)

    def parse_partition_info(self, output: str):
        super_partition_size = None
        system_partition_size = None

        for line in output.splitlines():
            out_line = line.lower()
            if "partition-size:super" in out_line:
                try:
                    super_partition_size = int(out_line.split(":")[-1].strip(), 16) / (1024 ** 3)
                except ValueError:
                    super_partition_size = 1.0
            elif "partition-size:system" in out_line:
                try:
                    system_partition_size = int(out_line.split(":")[-1].strip(), 16) / (1024 ** 3)
                except ValueError:
                    system_partition_size = 1.0

        if system_partition_size:
            self.system_partition_available = True
            self.log("[INFO] System partition detected, ignoring the super partition...")
            self.log(f"[INFO] Detected system partition with size: {system_partition_size:.2f} GB")
            self.status_label.setText("System partition detected. Ready to flash.")
            if self.gsi_image_path:
                self.flash_gsi_btn.setEnabled(True)
        elif super_partition_size:
            self.log(f"[INFO] Detected super partition with size: {super_partition_size:.2f} GB")
            self.status_label.setText("Super partition detected. Fastbootd reboot required.")
            self.ask_for_fastbootd_reboot()
        else:
            self.log("[ERROR] Neither 'super' nor 'system' partitions detected.")
            self.status_label.setText("Partition check failed. See log.")

    def ask_for_fastbootd_reboot(self, retry=False):
        result = QMessageBox.question(
            self, "Reboot to fastbootd",
            "Device has a 'super' partition. Reboot to fastbootd? (Required for some devices)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if result == QMessageBox.StandardButton.Yes:
            self.log("[INFO] Rebooting device to fastbootd...")
            self.run_command_async(
                [self._fastboot, "reboot", "fastboot"],
                lambda o: self.verify_fastbootd_mode()
            )
        elif not retry:
            QMessageBox.critical(self, "Error", "GSI flash requires fastbootd in many cases. Reboot to fastbootd and try again.")
            QTimer.singleShot(100, lambda: self.ask_for_fastbootd_reboot(retry=True))

    def verify_fastbootd_mode(self, output=None):
        self.log("[INFO] Please confirm device is in fastbootd mode.")
        result = QMessageBox.question(
            self, "Confirm fastbootd",
            "Are you now in fastbootd mode?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if result == QMessageBox.StandardButton.Yes:
            self.fastbootd_confirmed = True
            self.run_command_async([self._fastboot, "devices"], self.check_fastbootd_response)
            if self.gsi_image_path:
                self.flash_gsi_btn.setEnabled(True)
        else:
            self.log("[ERROR] fastbootd not confirmed. Re-check connection or reboot manually.")

    def check_fastbootd_response(self, output: str):
        out = (output or "").strip().lower()
        if not out or "waiting" in out:
            self.log("[ERROR] Device not detected in fastbootd mode.")
        else:
            self.log("[INFO] Device detected in fastbootd mode.")
            self.fetch_fastboot_info()

    # ---- Image selection and flashing ----

    def load_gsi_image(self):
        dlg = QFileDialog(self)
        dlg.setFileMode(QFileDialog.FileMode.ExistingFile)
        dlg.setNameFilter("Image files (*.img);;All Files (*)")
        if dlg.exec():
            files = dlg.selectedFiles()
            if files:
                self.gsi_image_path = files[0]
                self.log(f"[INFO] Selected GSI image: {self.gsi_image_path}")
                if self.system_partition_available or self.fastbootd_confirmed:
                    self.flash_gsi_btn.setEnabled(True)
            else:
                self.log("[WARN] No file selected.")

    def flash_gsi_image(self):
        if not self.gsi_image_path:
            QMessageBox.critical(self, "Error", "No GSI image loaded.")
            return
        answer = QMessageBox.question(
            self, "Confirm Flash",
            f"Flash image?\n{self.gsi_image_path}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        self.log("[INFO] Starting flash (fastboot).")
        self.flash_gsi_btn.setEnabled(False)

        def on_finished(output):
            if "finished" in output.lower() or "success" in output.lower():
                self.log("[INFO] Flash finished (heuristic success).")
                QMessageBox.information(self, "Flash Complete", "Flashing appears to have completed. Consider wiping data if required.")
            else:
                self.log("[ERROR] Flash finished; inspect logs for errors.")
                QMessageBox.critical(self, "Flash Completed", "Flash ended. Check logs for success/failure.")
            self.flash_gsi_btn.setEnabled(True)

        self.run_command_async([self._fastboot, "flash", "system", self.gsi_image_path], on_finished)

    # ---- Partition deletion and utilities ----

    def delete_partition(self, partition_name):
        ok = QMessageBox.question(
            self, "Delete Partition",
            f"Delete logical partitions {partition_name}_a and {partition_name}_b? This is destructive.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if ok == QMessageBox.StandardButton.Yes:
            self.log(f"[INFO] Deleting {partition_name}_a and {partition_name}_b...")
            self.run_command_async(
                [self._fastboot, "delete-logical-partition", f"{partition_name}_a"],
                lambda o: self.run_command_async(
                    [self._fastboot, "delete-logical-partition", f"{partition_name}_b"],
                    lambda o2: self.log(f"[INFO] Deletion finished for {partition_name}.")
                )
            )

    def open_treble_info_app(self):
        webbrowser.open("https://f-droid.org/packages/tk.hack5.treblecheck/")
        self.log("[INFO] Opened Treble Info App link.")

    def open_more_info(self):
        webbrowser.open("https://gist.github.com/codefl0w/f81105122ffc4699506dc742fccb8b90")
        self.log("[INFO] Opened GSI flashing guide.")

    def reboot_device(self):
        ans = QMessageBox.question(
            self, "Reboot Device",
            "Reboot the device now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if ans == QMessageBox.StandardButton.Yes:
            self.log("[INFO] Rebooting device via fastboot.")
            self.run_command_async(
                [self._fastboot, "reboot"],
                lambda o: self.log("[INFO] Reboot command issued.")
            )

    # ---- Cleanup ----

    def closeEvent(self, event):
        if hasattr(self, 'usb_timer') and self.usb_timer.isActive():
            self.usb_timer.stop()
        super().closeEvent(event)
