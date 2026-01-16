import sys
import os

script_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(script_dir)
sys.path.insert(0, root_dir)

import re
import subprocess
import math
from PyQt6.QtWidgets import (QFileDialog, QDialog, QVBoxLayout, QLabel, 
                            QLineEdit, QPushButton, QMessageBox, QTextEdit)
from PyQt6.QtCore import Qt, QThread, pyqtSignal


class DeviceInfoWorker(QThread):
    info_ready = pyqtSignal(str)

    def __init__(self, platform_tools_path):
        super().__init__()
        self.platform_tools_path = platform_tools_path
        self.adb_path = os.path.join(platform_tools_path, "adb")

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
        return {
            "IMEI": f"{self.adb_path} shell service call iphonesubinfo 3",
            "Fingerprint": f"{self.adb_path} shell getprop ro.build.fingerprint",
            "Board": f"{self.adb_path} shell getprop ro.product.board",
            "Build ID": f"{self.adb_path} shell getprop ro.build.id",
            "Android Version": f"{self.adb_path} shell getprop ro.build.version.release",
            "Manufacturer": f"{self.adb_path} shell getprop ro.product.manufacturer",
            "Model": f"{self.adb_path} shell getprop ro.product.model",
            "Product Name": f"{self.adb_path} shell getprop ro.product.name",
            "Architecture": f"{self.adb_path} shell getprop ro.product.cpu.abi",
            "Resolution": f"{self.adb_path} shell wm size",
            "Total RAM": f"{self.adb_path} shell cat /proc/meminfo",
            "Total Storage": f"{self.adb_path} shell df",
            "Root Method": f"{self.adb_path} shell su -v"
        }

    def _format_output(self, label, output):
        # Human-readable sizes
        formatters = {
            "Total RAM": lambda x: f"{math.ceil(self._parse_total_ram(x) / (1024 ** 2))} GB",
            "Total Storage": lambda x: f"{math.ceil(self._parse_total_storage(x) / (1024 ** 2))} GB",
            "IMEI": self._parse_imei
        }
        
        formatter = formatters.get(label)
        return formatter(output) if formatter else output

    def _parse_imei(self, raw_output):
        # Try decrypting the IMEI. Almost never works but whatever
        matches = re.findall(r"\d+", raw_output)
        if matches:
            imei = ''.join(matches)
            return imei[:15] if len(imei) >= 14 else "Unknown"
        return "Unknown"

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


def add_methods_to_class(instance):
    # Bind methods to the instance
    instance.sideload_file = sideload_file.__get__(instance)
    instance.show_wireless_adb_ui = show_wireless_adb_ui.__get__(instance)
    instance.show_device_info = show_device_info.__get__(instance)
    
    return instance




if __name__ == "__main__":
    print("This module isn't made for standalone use. Call it from QuickADB instead.")