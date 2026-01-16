#!/usr/bin/env python3
"""
gsiflasher.py - GSI Flasher UI (refactored, runnable)

Behavior preserved:
- Public method names kept for compatibility.
- Preferred detection: `fastboot devices` first. If none, fallback to pyusb enumeration.
- If pyusb sees Android-like USB devices but fastboot doesn't, user is prompted about drivers.
- Non-blocking command execution with realtime log streaming; final callback receives collected output.
- Exposes run_gsi_flasher() for standalone use and `if __name__ == "__main__"` entrypoint.
"""

import sys
import os
import subprocess
import threading
import time
import webbrowser
from pathlib import Path

# Keep same path behavior as other modules
script_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(script_dir)
sys.path.insert(0, root_dir)

from util.thememanager import ThemeManager

from PyQt6.QtCore import Qt, pyqtSignal, QObject, QTimer, QSize
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QTextEdit,
    QFileDialog, QMessageBox, QFrame, QLabel, QApplication, QFontDialog, QProgressBar
)
from PyQt6.QtGui import QFont

# Optional pyusb: handle absence gracefully
try:
    import usb.core
    import usb.util
    _PYUSB_AVAILABLE = True
except Exception:
    usb = None
    _PYUSB_AVAILABLE = False


# ----- Worker / Command runner (streams output) -----
class WorkerSignals(QObject):
    finished = pyqtSignal(str)   # Emitted with collected output when done
    log = pyqtSignal(str)        # Emitted for each streamed line


class CommandRunner:
    """
    Lightweight worker that runs a command and streams stdout/stderr lines via signals.
    Designed to be executed in a background Python thread (threading.Thread).
    """

    def __init__(self, command, platform_tools_path=None):
        """
        command: list (preferred) or string.
        platform_tools_path: directory where adb/fastboot executables live (optional).
        """
        self.command = command
        self.platform_tools_path = platform_tools_path
        self.signals = WorkerSignals()

    def _resolve_executable(self, exe_name):
        """
        If platform_tools_path provided and an executable exists there, return full path.
        Else return exe_name (allow PATH lookup).
        On Windows, append .exe when checking in platform-tools.
        """
        if not exe_name:
            return exe_name
        if self.platform_tools_path:
            candidate = os.path.join(self.platform_tools_path, exe_name)
            if os.name == "nt" and not candidate.lower().endswith(".exe"):
                candidate_exe = candidate + ".exe"
                if os.path.exists(candidate_exe):
                    return candidate_exe
            if os.path.exists(candidate):
                return candidate
        # fallback to exe_name (rely on PATH)
        return exe_name

    def run(self):
        """Execute the command, stream lines to log signal, emit finished with full captured output."""
        try:
            # Accept either list or string; prefer list
            if isinstance(self.command, (list, tuple)):
                cmd_list = list(self.command)
            else:
                # keep as string and run through shell
                cmd_list = self.command

            # If list, attempt to resolve first element in platform-tools folder
            if isinstance(cmd_list, list) and len(cmd_list) > 0:
                cmd_list[0] = self._resolve_executable(cmd_list[0])

            # Build subprocess.Popen args
            use_shell = not isinstance(cmd_list, list)
            proc = subprocess.Popen(
                cmd_list,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=self.platform_tools_path or None,
                shell=use_shell,
                text=True,
                bufsize=1,
                universal_newlines=True
            )

            collected = []
            # Stream lines as they arrive
            if proc.stdout:
                for line in proc.stdout:
                    if line is None:
                        continue
                    line = line.rstrip("\n")
                    collected.append(line)
                    try:
                        self.signals.log.emit(line)
                    except Exception:
                        # Signals may be disconnected; continue
                        pass

            rc = proc.wait()
            output = "\n".join(collected)
            # Mark finished (include rc info when helpful)
            final = output if output else ""
            self.signals.finished.emit(final)
        except Exception as e:
            # Emit whatever we have and log the exception
            try:
                self.signals.log.emit(f"[ERROR] {str(e)}")
                self.signals.finished.emit("")
            except Exception:
                pass


# ----- GSIFlasherUI -----
class GSIFlasherUI(QMainWindow):

    def __init__(self, platform_tools_path=None, parent=None):
        super().__init__(parent)

        # platform-tools path (default preserves original global behavior)
        self.platform_tools_path = platform_tools_path or os.path.join(root_dir, "platform-tools")
        self.gsi_image_path = None
        self.system_partition_available = False
        self.fastbootd_confirmed = False
        self.command_threads = []   # keep Python Thread refs to avoid GC
        self._last_fastboot_output = ""
        self.adb_prompt_shown = False


        self.setWindowTitle("GSI Flasher")
        self.setMinimumSize(700, 500)
        self.setup_ui()
        ThemeManager.apply_theme(self)

        # Timer used for repeated checks (only active when checking)
        self.usb_timer = QTimer(self)
        self.usb_timer.timeout.connect(self.check_device_state)

        # Kick off initial check shortly after UI shows
        QTimer.singleShot(150, self.check_device_state)

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

        # Buttons
        btn_frame = QFrame()
        btn_layout = QHBoxLayout(btn_frame)
        btn_layout.setSpacing(8)

        self.recheck_btn = QPushButton("Re-check Devices")
        self.recheck_btn.clicked.connect(self.on_recheck_devices_clicked)
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

        # Progress bar (used loosely for long tasks)
        self.progress = QProgressBar()
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        # Initial guidance logs
        self.log("[INFO] This method may not work on Samsung devices unless using custom recovery.")
        self.log("[INFO] Use GSI images equal or higher Android version than stock OS.")

    # ---- Logging helper ----
    def log(self, message: str):
        ts = time.strftime("%H:%M:%S")
        try:
            self.log_output.append(f"[{ts}] {message}")
            self.log_output.ensureCursorVisible()
        except Exception:
            # fallback: print to stdout if UI fails
            print(f"[{ts}] {message}")

    # ---- Command execution helper ----
    def run_command_async(self, command, callback=None):
        """
        command: list (recommended) or string.
        callback: callable receiving a single string argument (collected output) when finished.
        """
        runner = CommandRunner(command, self.platform_tools_path)
        runner.signals.log.connect(self.log)

        def _on_finished(output):
            try:
                if callback:
                    callback(output)
            except Exception as e:
                self.log(f"[ERROR] Callback raised: {e}")

        runner.signals.finished.connect(_on_finished)

        t = threading.Thread(target=runner.run, daemon=True)
        t.start()
        self.command_threads.append(t)
        return t

    # ---- Device detection flow (fastboot preferred) ----

    def on_recheck_devices_clicked(self): # reset the adb reboot prompt with each button click
        self.adb_prompt_shown = False
        self.check_device_state()

    def check_device_state(self):
        """Public entry: start a device check. Uses adb pre-check, then fastboot devices, then pyusb fallback."""
        if not hasattr(self, "retry_count") or self.retry_count <= 0:
            self.retry_count = 5

        self.log("[INFO] Checking for connected devices via ADB...")
        adb_cmd = "adb.exe" if os.name == "nt" else "adb"
        self.run_command_async([adb_cmd, "devices"], self._adb_devices_callback)

    def _adb_devices_callback(self, output: str):
        """If ADB shows a connected device, optionally reboot to fastboot."""
        devices = []
        for ln in (output or "").splitlines()[1:]:  # skip header
            ln = ln.strip()
            if not ln:
                continue
            parts = ln.split()
            if len(parts) >= 2 and parts[1].lower() == "device":
                devices.append(parts[0])

            if devices and not self.adb_prompt_shown:
                self.adb_prompt_shown = True
                self.log(f"[INFO] ADB device(s) detected: {', '.join(devices)}")
                reply = QMessageBox.question(
                    self,
                    "ADB Device Detected",
                    "One or more devices are connected via ADB.\n"
                    "Do you want to reboot them into fastboot mode now?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                )
                if reply == QMessageBox.StandardButton.Yes:
                    adb_cmd = "adb.exe" if os.name == "nt" else "adb"
                    self.run_command_async([adb_cmd, "reboot", "fastboot"], lambda o: self._start_fastboot_check())
                else:
                    self._start_fastboot_check()
            else:
                self._start_fastboot_check()
            

    def _start_fastboot_check(self):
        """Starts repeated fastboot checking with retry counter."""
        if not self.usb_timer.isActive():
            self.usb_timer.start(1000)  # check every 1 sec
        self._do_fastboot_check()

    def _do_fastboot_check(self):
        """Single iteration of fastboot check."""
        if self.retry_count <= 0:
            self.log("[INFO] No devices detected after retries. Stopping automatic checks.")
            self.usb_timer.stop()
            return

        self.log("[INFO] Checking for fastboot devices (preferred).")
        fastboot_cmd = "fastboot.exe" if os.name == "nt" else "fastboot"
        self.run_command_async([fastboot_cmd, "devices"], self._fastboot_devices_callback)

        # decrement retry count after scheduling
        self.retry_count -= 1
        if self.retry_count < 0:
            self.log("[INFO] No devices detected after retries. Stopping automatic checks.")
            self.usb_timer.stop()
            return


    def _fastboot_devices_callback(self, output: str):
        """Same as before but with retry countdown logging."""
        self._last_fastboot_output = output or ""
        out_text = (output or "").strip()
        if out_text:
            device_lines = []
            for ln in out_text.splitlines():
                ln = ln.strip()
                if not ln or "waiting for" in ln.lower():
                    continue
                parts = ln.split()
                if len(parts) >= 2 and parts[1].lower() in ("fastboot", "device", "recovery", "bootloader"):
                    device_lines.append(parts[0])
            if device_lines:
                self.log(f"[INFO] fastboot device(s) detected: {', '.join(device_lines)}")
                self.usb_timer.stop()
                self.fetch_fastboot_info()
                return

        # No fastboot device
        self.log("[INFO] No fastboot devices found. Probing USB descriptors (pyusb fallback)...")
        self._probe_pyusb_and_prompt_if_needed()

        if self.retry_count > 0:
            self.log(f"[INFO] No fastboot or Android-like USB devices detected. Retrying... ({self.retry_count})")
        if self.retry_count == 0:
            self.log("[INFO] No devices detected after retries. Stopping automatic checks.")
            self.usb_timer.stop()

    def _probe_pyusb_and_prompt_if_needed(self):
        if not _PYUSB_AVAILABLE:
            self.log("[INFO] pyusb not available; will rely on fastboot command fallback.")
            return

        try:
            devices = list(usb.core.find(find_all=True))
        except Exception as e:
            self.log(f"[ERROR] pyusb enumeration failed: {e}")
            return

        android_like = []
        for dev in devices:
            try:
                manuf = ""
                prod = ""
                try:
                    if dev.iManufacturer:
                        manuf = usb.util.get_string(dev, dev.iManufacturer) or ""
                except Exception:
                    manuf = ""
                try:
                    if dev.iProduct:
                        prod = usb.util.get_string(dev, dev.iProduct) or ""
                except Exception:
                    prod = ""
                check = (manuf + " " + prod).lower()
                if any(k in check for k in ("android", "fastboot", "adb", "bootloader")):
                    android_like.append((dev.idVendor, dev.idProduct, manuf.strip(), prod.strip()))
            except Exception:
                continue

        if android_like:
            if self.usb_timer.isActive():
                self.usb_timer.stop()
            self.log("[WARN] USB device(s) resembling Android were found but not enumerated by fastboot.")
            for vid, pid, manuf, prod in android_like:
                self.log(f"USB: {vid:04x}:{pid:04x} - {manuf} {prod}")
            self._prompt_driver_issue(android_like)
            return

        

    def _prompt_driver_issue(self, android_like):
        details = (
            "A USB device resembling an Android device was detected by the system, but 'fastboot' did not enumerate it.\n\n"
            "Possible causes:\n"
            "- Missing or incorrect USB/fastboot drivers (Windows: install OEM or Google USB drivers).\n"
            "- Device in an unexpected USB mode.\n\n"
            "Action: Install/update drivers, replug device, then click 'Re-check Devices'."
        )
        QMessageBox.critical(self, "Device Not Detected by fastboot", details)


    # Provide a compatibility wrapper used elsewhere
    def run_fastboot_devices_fallback(self):
        """Compatibility wrapper: call check_device_state which prefers fastboot then falls back."""
        self.check_device_state()

    # ---- Partition & flash helpers ----
    def fetch_fastboot_info(self, output=None):
        """Query device for partition info using fastboot getvar all."""
        self.log("[INFO] Gathering fastboot partition info (fastboot getvar all).")
        fastboot_cmd = "fastboot.exe" if os.name == "nt" else "fastboot"
        self.run_command_async([fastboot_cmd, "getvar", "all"], self.parse_partition_info)

    def parse_partition_info(self, output: str):
        """
        Inspect output of `fastboot getvar all` for 'super' or 'system' info.
        Enable flash button when appropriate.
        """
        out = (output or "").lower()
        super_found = False
        system_found = False

        # Look for keywords in the raw output; original script parsed hex sizes, but simple detection suffices.
        if "partition-size:super" in out or "(bootloader) super" in out or "super" in out and "partition" in out:
            super_found = True
        if "partition-size:system" in out or "(bootloader) system" in out or "system" in out and "partition" in out:
            system_found = True

        if system_found:
            self.system_partition_available = True
            self.log("[INFO] System partition detected.")
            if self.gsi_image_path:
                self.flash_gsi_btn.setEnabled(True)
        elif super_found:
            self.log("[INFO] Super partition detected; fastbootd may be required.")
            # Ask user to reboot to fastbootd
            self.ask_for_fastbootd_reboot()
        else:
            self.log("[ERROR] Could not detect 'system' or 'super' partitions. Check device connection.")

    def ask_for_fastbootd_reboot(self, retry=False):
        result = QMessageBox.question(
            self, "Reboot to fastbootd",
            "Device has a 'super' partition. Reboot to fastbootd? (Required for some devices)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if result == QMessageBox.StandardButton.Yes:
            self.log("[INFO] Rebooting device to fastbootd...")
            fastboot_cmd = "fastboot.exe" if os.name == "nt" else "fastboot"
            # Non-blocking reboot call
            self.run_command_async([fastboot_cmd, "reboot", "fastboot"], lambda o: self.verify_fastbootd_mode())
            # Start timer to detect fastbootd mode
            if not self.usb_timer.isActive():
                self.usb_timer.start(1200)
        elif not retry:
            QMessageBox.critical(self, "Error", "GSI flash requires fastbootd in many cases. Reboot to fastbootd and try again.")
            QTimer.singleShot(100, lambda: self.ask_for_fastbootd_reboot(retry=True))

    def verify_fastbootd_mode(self, output=None):
        # After attempting reboot to fastbootd, prompt the user to confirm
        if self.usb_timer.isActive():
            self.usb_timer.stop()
        self.log("[INFO] Please confirm device is in fastbootd mode.")
        result = QMessageBox.question(self, "Confirm fastbootd", "Are you now in fastbootd mode?",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if result == QMessageBox.StandardButton.Yes:
            self.fastbootd_confirmed = True
            fastboot_cmd = "fastboot.exe" if os.name == "nt" else "fastboot"
            self.run_command_async([fastboot_cmd, "devices"], self.check_fastbootd_response)
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
        dlg.setNameFilter("Image files (*.img *.img.gz *.img.xz);;All Files (*)")
        if dlg.exec():
            files = dlg.selectedFiles()
            if files:
                self.gsi_image_path = files[0]
                self.log(f"[INFO] Selected GSI image: {self.gsi_image_path}")
                # allow flash if system partition or fastbootd is confirmed
                if self.system_partition_available or self.fastbootd_confirmed:
                    self.flash_gsi_btn.setEnabled(True)
            else:
                self.log("[WARN] No file selected.")

    def flash_gsi_image(self):
        if not self.gsi_image_path:
            QMessageBox.critical(self, "Error", "No GSI image loaded.")
            return
        answer = QMessageBox.question(self, "Confirm Flash",
                                      f"Flash image?\n{self.gsi_image_path}",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return

        self.log("[INFO] Starting flash (fastboot).")
        self.flash_gsi_btn.setEnabled(False)
        self.progress.setValue(0)

        fastboot_cmd = "fastboot.exe" if os.name == "nt" else "fastboot"
        # Default conservative target: 'system' (original scripts may vary)
        cmd = [fastboot_cmd, "flash", "system", self.gsi_image_path]
        # Stream logs and handle completion
        runner = CommandRunner(cmd, self.platform_tools_path)
        runner.signals.log.connect(self.log)
        # simple progress heuristic: bump a little on each line
        def on_line_bump(line):
            v = self.progress.value()
            v = min(95, v + 5)
            self.progress.setValue(v)
        runner.signals.log.connect(on_line_bump)

        def on_finished(output):
            self.progress.setValue(100)
            # heuristic check for success message
            if output and ("finished" in output.lower() or "success" in output.lower()):
                self.log("[INFO] Flash finished (heuristic success).")
                QMessageBox.information(self, "Flash Complete", "Flashing appears to have completed. Consider wiping data if required.")
            else:
                self.log("[ERROR] Flash finished; inspect logs for errors.")
                QMessageBox.critical(self, "Flash Completed", "Flash ended. Check logs for success/failure.")
            self.flash_gsi_btn.setEnabled(True)

        t = threading.Thread(target=runner.run, daemon=True)
        runner.signals.finished.connect(on_finished)
        t.start()
        self.command_threads.append(t)

    def post_flash_actions(self, output):
        # kept for compatibility; older code used this callback style
        if output and "finished" in output.lower():
            self.log("[INFO] Flash successful.")
        else:
            self.log("[ERROR] Flash may have failed.")

    # ---- Partition deletion and utilities ----
    def delete_partition(self, partition_name):
        ok = QMessageBox.question(self, "Delete Partition",
                                  f"Delete logical partitions {partition_name}_a and {partition_name}_b? This is destructive.",
                                  QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ok == QMessageBox.StandardButton.Yes:
            self.log(f"[INFO] Deleting {partition_name}_a and {partition_name}_b...")
            fastboot_cmd = "fastboot.exe" if os.name == "nt" else "fastboot"
            # Chain commands: run delete for _a then _b (simple sequential callbacks)
            def cb1(_):
                self.run_command_async([fastboot_cmd, "delete-logical-partition", f"{partition_name}_b"], lambda o: self.log(f"[INFO] Deletion finished for {partition_name}."))
            self.run_command_async([fastboot_cmd, "delete-logical-partition", f"{partition_name}_a"], cb1)

    def open_treble_info_app(self):
        webbrowser.open("https://f-droid.org/packages/tk.hack5.treblecheck/")
        self.log("[INFO] Opened Treble Info App link.")

    def open_more_info(self):
        webbrowser.open("https://gist.github.com/codefl0w/f81105122ffc4699506dc742fccb8b90")
        self.log("[INFO] Opened GSI flashing guide.")

    def reboot_device(self):
        ans = QMessageBox.question(self, "Reboot Device", "Reboot the device now?",
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ans == QMessageBox.StandardButton.Yes:
            self.log("[INFO] Rebooting device via fastboot.")
            fastboot_cmd = "fastboot.exe" if os.name == "nt" else "fastboot"
            self.run_command_async([fastboot_cmd, "reboot"], lambda o: self.log("[INFO] Reboot command issued."))

    # ---- Cleanup ----
    def closeEvent(self, event):
        # Threads are daemonized; ensure timers stopped
        try:
            if self.usb_timer.isActive():
                self.usb_timer.stop()
        except Exception:
            pass
        super().closeEvent(event)


# ---- Standalone runner ----
def run_gsi_flasher(platform_tools_path=None):
    app = QApplication(sys.argv)
    win = GSIFlasherUI(platform_tools_path=platform_tools_path)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(run_gsi_flasher())
