"""

adbfunc.py - Handles QuickADB's and File Explorer's CommandRunner, as well as
managing some of QuickADB's device info and other ADB/Fastboot related functions.

Also handles AppImage compatibility.

"""


import sys
import os

from util.resource import get_root_dir, resource_path, resolve_platform_tool
root_dir = get_root_dir()
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

import subprocess
import math
from PyQt6.QtWidgets import (QFileDialog, QDialog, QVBoxLayout, QHBoxLayout, QLabel, 
                            QLineEdit, QPushButton, QMessageBox, QTextEdit)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
import threading


class CommandRunner(QThread): 
    """
    Runs shell commands in a separate thread to avoid blocking the GUI.
    Streams stdout and stderr in real-time.

    Signals:
    output_signal(str, str): Emits each line of output with a tag ("Output" or "Error").

    """
    output_signal = pyqtSignal(str, str)

    def __init__(self, command: str, platform_tools_path: str, env: dict = None):
        super().__init__()
        self.command = command
        self.platform_tools_path = platform_tools_path
        self.env = env

    def _stream_reader(self, stream, tag: str):
        """Reads a stream line-by-line and emits lines via a signal."""
        try:
            for line in iter(stream.readline, ''):
                self.output_signal.emit(line.rstrip("\n"), tag)
        except Exception as e:
            self.output_signal.emit(f"Reader error: {e}", "Error")
        finally:
            try:
                stream.close()
            except IOError:
                pass

    def run(self):
        """Executes the command and starts threads to monitor its output."""
        try:
            # Windows specific: Create a new process group and hide the console window.
            creationflags = 0
            if sys.platform == "win32":
                creationflags = (
                    subprocess.CREATE_NEW_PROCESS_GROUP |
                    subprocess.CREATE_NO_WINDOW
                )

            process = subprocess.Popen(
                self.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=True,
                cwd=self.platform_tools_path,
                text=True,
                bufsize=1,  # Line-buffered
                env=self.env,
                creationflags=creationflags
            )

            # Start reader threads for stdout and stderr to prevent blocking
            t_out = threading.Thread(target=self._stream_reader, args=(process.stdout, "Output"), daemon=True)
            t_err = threading.Thread(target=self._stream_reader, args=(process.stderr, "Error"), daemon=True)

            t_out.start()
            t_err.start()

            process.wait()  # Wait for the subprocess to complete
            t_out.join()    # Ensure threads finish
            t_err.join()

        except FileNotFoundError:
            self.output_signal.emit(f"Error: Command not found. Is '{self.command.split()[0]}' in your PATH or platform-tools?", "Error")
        except Exception as e:
            self.output_signal.emit(f"Execution Error: {str(e)}", "Error")


class DeviceInfoWorker(QThread):
    info_ready = pyqtSignal(str)

    def __init__(self, platform_tools_path):
        super().__init__()
        self.platform_tools_path = platform_tools_path
        self.adb_path = resolve_platform_tool(platform_tools_path, "adb")

    def run(self):
        commands = self._get_device_commands()
        results = []
        
        for label, command in commands.items():
            try:
                result = subprocess.run(
                    command, 
                    stdout=subprocess.PIPE, 
                    stderr=subprocess.PIPE, 
                    text=True, 
                    shell=True,
                    timeout=10
                )
                
                if result.returncode == 0:
                    output = result.stdout.strip()
                    formatted_output = self._format_output(label, output)
                    results.append(f"{label}: {formatted_output}")
                else:
                    results.append(f"{label}: Error - {result.stderr.strip()}")
                    
            except subprocess.TimeoutExpired:
                results.append(f"{label}: Timeout")
            except Exception as e:
                results.append(f"{label}: Error - {str(e)}")

        self.info_ready.emit("\n".join(results))

    def _get_device_commands(self):
        adb_cmd = f'"{self.adb_path}"'
        return {
            "Fingerprint": f"{adb_cmd} shell getprop ro.build.fingerprint",
            "Board": f"{adb_cmd} shell getprop ro.product.board",
            "Build ID": f"{adb_cmd} shell getprop ro.build.id",
            "Android Version": f"{adb_cmd} shell getprop ro.build.version.release",
            "Manufacturer": f"{adb_cmd} shell getprop ro.product.manufacturer",
            "Model": f"{adb_cmd} shell getprop ro.product.model",
            "Product Name": f"{adb_cmd} shell getprop ro.product.name",
            "Architecture": f"{adb_cmd} shell getprop ro.product.cpu.abi",
            "Resolution": f"{adb_cmd} shell wm size",
            "Total RAM": f"{adb_cmd} shell cat /proc/meminfo",
            "Total Storage": f"{adb_cmd} shell df",
            "Root Method": f"{adb_cmd} shell su -v"
        }

    def _format_output(self, label, output):
        # Human-readable sizes
        formatters = {
            "Total RAM": lambda x: f"{math.ceil(self._parse_total_ram(x) / (1024 ** 2))} GB",
            "Total Storage": lambda x: f"{math.ceil(self._parse_total_storage(x) / (1024 ** 2))} GB",
        }
        
        formatter = formatters.get(label)
        return formatter(output) if formatter else output



    def _parse_total_ram(self, output):
        # Parse total RAM from /proc/meminfo
        for line in output.splitlines():
            if "MemTotal" in line:
                return int(line.split()[1])
        return 0

    def _parse_total_storage(self, output):
        # Parse total storage from df output
        storage_patterns = ["/data", "/data/media", "/storage/emulated"]
        
        for line in output.splitlines():
            if any(pattern in line for pattern in storage_patterns):
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    return int(parts[1])
        return 0


class DeviceInfoDialog(QDialog):
    
    def __init__(self, platform_tools_path, parent=None):
        super().__init__(parent)
        self._setup_ui()
        self._start_info_worker(platform_tools_path)

    def _setup_ui(self):
        self.setWindowTitle("Device Specifications")
        self.setMinimumSize(500, 400)
        self.setModal(True)

        layout = QVBoxLayout()
        
        # Text display area
        self.text_display = QTextEdit()
        self.text_display.setReadOnly(True)
        self.text_display.setPlainText("Loading device information...")
        self.text_display.setFont(self.text_display.font())  # Use monospace if needed
        layout.addWidget(self.text_display)

        # Close button
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        close_btn.setDefault(True)
        layout.addWidget(close_btn)

        self.setLayout(layout)

    def _start_info_worker(self, platform_tools_path):

        self.worker = DeviceInfoWorker(platform_tools_path)
        self.worker.info_ready.connect(self.text_display.setPlainText)
        self.worker.finished.connect(self.worker.deleteLater)  # Clean up
        self.worker.start()


class WirelessADBDialog(QDialog):
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_app = parent
        self._setup_ui()

    def _setup_ui(self):
        self.setWindowTitle("Wireless ADB Connection")
        self.setFixedSize(350, 120)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setModal(True)

        layout = QVBoxLayout()

        # Instructions
        label = QLabel("Enter Device Address (HOST:PORT):")
        layout.addWidget(label)

        # Address input
        self.address_entry = QLineEdit()
        self.address_entry.setPlaceholderText("e.g., 192.168.1.100:5555")
        self.address_entry.returnPressed.connect(self._connect_wireless_adb)
        layout.addWidget(self.address_entry)

        # Connect button
        connect_button = QPushButton("Connect")
        connect_button.clicked.connect(self._connect_wireless_adb)
        connect_button.setDefault(True)
        layout.addWidget(connect_button)

        self.setLayout(layout)
        self.address_entry.setFocus()

    def _connect_wireless_adb(self):
        address = self.address_entry.text().strip()
        
        if not self._validate_address(address):
            QMessageBox.critical(self, "Error", 
                               "Please enter a valid address (HOST:PORT).\n"
                               "Example: 192.168.1.100:5555")
            return

        if hasattr(self.parent_app, 'run_command_async'):
            self.parent_app.run_command_async(
                f"adb connect {address}", 
                f"Connecting to {address}", 
                "ADB"
            )
            self.accept()
        else:
            QMessageBox.critical(self, "Error", 
                               "Parent application method not found.")

    def _validate_address(self, address):
        if not address:
            return False
        
        # Basic validation for HOST:PORT format
        if ':' not in address:
            return False
            
        try:
            host, port = address.rsplit(':', 1)
            port_num = int(port)
            return len(host) > 0 and 1 <= port_num <= 65535
        except ValueError:
            return False

class InstallAPKDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_app = parent
        self._setup_ui()

    def _setup_ui(self):
        self.setWindowTitle("Install APK")
        self.setFixedSize(400, 150)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setModal(True)

        layout = QVBoxLayout()

        layout.addWidget(QLabel("Select APK file to install:"))

        # Path input and Browse button
        path_layout = QHBoxLayout()
        self.path_entry = QLineEdit()
        self.path_entry.setPlaceholderText("Select or enter APK path...")
        path_layout.addWidget(self.path_entry)

        browse_btn = QPushButton("Browse")
        browse_btn.setFixedWidth(80)
        browse_btn.clicked.connect(self._browse_apk)
        path_layout.addWidget(browse_btn)
        
        layout.addLayout(path_layout)

        # Install button
        install_btn = QPushButton("Install APK")
        install_btn.clicked.connect(self._install_apk)
        install_btn.setDefault(True)
        layout.addWidget(install_btn)

        self.setLayout(layout)

    def _browse_apk(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select APK File", "", "APK Files (*.apk);;All Files (*)"
        )
        if file_path:
            self.path_entry.setText(file_path)

    def _install_apk(self):
        apk_path = self.path_entry.text().strip()
        if not apk_path:
            QMessageBox.warning(self, "No APK", "Please select or enter an APK path.")
            return

        if not os.path.exists(apk_path):
            QMessageBox.warning(self, "Invalid Path", "The specified APK file does not exist.")
            return

        if hasattr(self.parent_app, 'run_command_async'):
            self.parent_app.run_command_async(
                f'adb install "{apk_path}"',
                f"Installing {os.path.basename(apk_path)}",
                "ADB"
            )
            self.accept()
        else:
            QMessageBox.critical(self, "Error", "Command execution method not found.")


class UninstallAppDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_app = parent
        self._setup_ui()

    def _setup_ui(self):
        self.setWindowTitle("Uninstall App")
        self.setFixedSize(350, 120)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setModal(True)

        layout = QVBoxLayout()

        layout.addWidget(QLabel("Enter Package Name (e.g., com.example.app):"))

        self.package_entry = QLineEdit()
        self.package_entry.setPlaceholderText("com.android.chrome")
        self.package_entry.returnPressed.connect(self._uninstall_app)
        layout.addWidget(self.package_entry)

        uninstall_btn = QPushButton("Uninstall")
        uninstall_btn.clicked.connect(self._uninstall_app)
        uninstall_btn.setDefault(True)
        layout.addWidget(uninstall_btn)

        self.setLayout(layout)
        self.package_entry.setFocus()

    def _uninstall_app(self):
        package_name = self.package_entry.text().strip()
        if not package_name:
            QMessageBox.warning(self, "No Package", "Please enter a package name.")
            return

        if hasattr(self.parent_app, 'run_command_async'):
            self.parent_app.run_command_async(
                f'adb uninstall "{package_name}"',
                f"Uninstalling {package_name}",
                "ADB"
            )
            self.accept()
        else:
            QMessageBox.critical(self, "Error", "Command execution method not found.")


class ADBPushDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_app = parent
        self._setup_ui()

    def _setup_ui(self):
        self.setWindowTitle("ADB Push File")
        self.setFixedSize(450, 200)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setModal(True)

        layout = QVBoxLayout()

        # Source file selection
        layout.addWidget(QLabel("Select file to push to device:"))
        source_layout = QHBoxLayout()
        self.source_path_entry = QLineEdit()
        self.source_path_entry.setPlaceholderText("Select or enter local file path...")
        source_layout.addWidget(self.source_path_entry)

        browse_source_btn = QPushButton("Browse")
        browse_source_btn.setFixedWidth(80)
        browse_source_btn.clicked.connect(self._browse_source_file)
        source_layout.addWidget(browse_source_btn)
        layout.addLayout(source_layout)

        # Destination path on device
        layout.addWidget(QLabel("Enter destination path on device (e.g., /sdcard/Download/):"))
        self.destination_path_entry = QLineEdit()
        self.destination_path_entry.setPlaceholderText("/sdcard/Download/")
        self.destination_path_entry.returnPressed.connect(self._push_file)
        layout.addWidget(self.destination_path_entry)

        # Push button
        push_btn = QPushButton("Push File")
        push_btn.clicked.connect(self._push_file)
        push_btn.setDefault(True)
        layout.addWidget(push_btn)

        self.setLayout(layout)
        self.source_path_entry.setFocus()

    def _browse_source_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select File to Push", "", "All Files (*)"
        )
        if file_path:
            self.source_path_entry.setText(file_path)

    def _push_file(self):
        source_path = self.source_path_entry.text().strip()
        destination_path = self.destination_path_entry.text().strip()

        if not source_path:
            QMessageBox.warning(self, "No Source File", "Please select or enter a local file path.")
            return
        if not os.path.exists(source_path):
            QMessageBox.warning(self, "Invalid Source Path", "The specified source file does not exist.")
            return
        if not destination_path:
            QMessageBox.warning(self, "No Destination Path", "Please enter a destination path on the device.")
            return

        if hasattr(self.parent_app, 'run_command_async'):
            self.parent_app.run_command_async(
                f'adb push "{source_path}" "{destination_path}"',
                f"Pushing {os.path.basename(source_path)} to {destination_path}",
                "ADB"
            )
            self.accept()
        else:
            QMessageBox.critical(self, "Error", "Command execution method not found.")


class ADBPullDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_app = parent
        self._setup_ui()

    def _setup_ui(self):
        self.setWindowTitle("ADB Pull File")
        self.setFixedSize(450, 200)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setModal(True)

        layout = QVBoxLayout()

        # Source path on device
        layout.addWidget(QLabel("Enter source path on device (e.g., /sdcard/Download/file.txt):"))
        self.source_path_entry = QLineEdit()
        self.source_path_entry.setPlaceholderText("/sdcard/Download/file.txt")
        layout.addWidget(self.source_path_entry)

        # Destination folder selection
        layout.addWidget(QLabel("Select local destination folder:"))
        destination_layout = QHBoxLayout()
        self.destination_folder_entry = QLineEdit()
        self.destination_folder_entry.setPlaceholderText("Select or enter local folder path...")
        destination_layout.addWidget(self.destination_folder_entry)

        browse_dest_btn = QPushButton("Browse")
        browse_dest_btn.setFixedWidth(80)
        browse_dest_btn.clicked.connect(self._browse_destination_folder)
        destination_layout.addWidget(browse_dest_btn)
        layout.addLayout(destination_layout)

        # Pull button
        pull_btn = QPushButton("Pull File")
        pull_btn.clicked.connect(self._pull_file)
        pull_btn.setDefault(True)
        layout.addWidget(pull_btn)

        self.setLayout(layout)
        self.source_path_entry.setFocus()

    def _browse_destination_folder(self):
        folder_path = QFileDialog.getExistingDirectory(
            self, "Select Destination Folder", ""
        )
        if folder_path:
            self.destination_folder_entry.setText(folder_path)

    def _pull_file(self):
        source_path = self.source_path_entry.text().strip()
        destination_folder = self.destination_folder_entry.text().strip()

        if not source_path:
            QMessageBox.warning(self, "No Source Path", "Please enter a source path on the device.")
            return
        if not destination_folder:
            QMessageBox.warning(self, "No Destination Folder", "Please select or enter a local destination folder.")
            return
        if not os.path.exists(destination_folder):
            QMessageBox.warning(self, "Invalid Destination Folder", "The specified destination folder does not exist.")
            return
        if not os.path.isdir(destination_folder):
            QMessageBox.warning(self, "Invalid Destination Folder", "The specified path is not a directory.")
            return

        if hasattr(self.parent_app, 'run_command_async'):
            self.parent_app.run_command_async(
                f'adb pull "{source_path}" "{destination_folder}"',
                f"Pulling {os.path.basename(source_path)} to {destination_folder}",
                "ADB"
            )
            self.accept()
        else:
            QMessageBox.critical(self, "Error", "Command execution method not found.")


# Extension methods for the main application class
def show_device_info(self):
    dialog = DeviceInfoDialog(self.platform_tools_path, self)
    dialog.exec()


def sideload_file(self):
    # adb sideload. What a surprise!
    file_path, _ = QFileDialog.getOpenFileName(
        self, 
        "Select File to Sideload", 
        "", 
        "Archive Files (*.zip *.7z *.rar);;ZIP Files (*.zip);;All Files (*)"
    )
    
    if file_path:
        if hasattr(self, 'run_command_async'):
            self.run_command_async(
                f'adb sideload "{file_path}"', 
                f"Sideloading {os.path.basename(file_path)}", 
                "ADB"
            )
        else:
            QMessageBox.critical(self, "Error", 
                               "Command execution method not found.")


def show_wireless_adb_ui(self):
    # Show wireless ADB connection dialog
    dialog = WirelessADBDialog(self)
    dialog.exec()


def show_install_apk_ui(self):
    # Show Install APK dialog
    dialog = InstallAPKDialog(self)
    dialog.exec()


def show_uninstall_app_ui(self):
    # Show Uninstall App dialog
    dialog = UninstallAppDialog(self)
    dialog.exec()


def show_adb_push_ui(self):
    # Show ADB Push dialog
    dialog = ADBPushDialog(self)
    dialog.exec()


def show_adb_pull_ui(self):
    # Show ADB Pull dialog
    dialog = ADBPullDialog(self)
    dialog.exec()



def add_methods_to_class(instance):
    # Bind methods to the instance
    instance.sideload_file = sideload_file.__get__(instance)
    instance.show_wireless_adb_ui = show_wireless_adb_ui.__get__(instance)
    instance.show_device_info = show_device_info.__get__(instance)
    instance.install_apk = show_install_apk_ui.__get__(instance)
    instance.uninstall_app = show_uninstall_app_ui.__get__(instance)
    instance.open_push_window = show_adb_push_ui.__get__(instance)
    instance.open_pull_window = show_adb_pull_ui.__get__(instance)
    
    return instance




if __name__ == "__main__":
    print("This module isn't made for standalone use. Call it from QuickADB instead.")
