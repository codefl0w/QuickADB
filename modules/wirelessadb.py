"""
wirelessadb.py - Wireless ADB Configuration and QR Pairing module for QuickADB.

Supports QR code pairing and manual pairing code entry with mDNS auto-connect.
"""
import os
import secrets
import subprocess
import time
from typing import Optional

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, 
    QMessageBox, QTabWidget, QWidget, QApplication
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QPixmap, QImage

import qrcode

try:
    from zeroconf import Zeroconf, ServiceBrowser, ServiceStateChange
except ImportError:
    Zeroconf = None


class AutoConnectWorker(QThread):
    """
    Lightweight worker used after a manual 'Pairing Code' success.
    Listens for the connection port broadcast from a specific IP and connects.
    """
    connected_success = pyqtSignal(str)
    error_occurred = pyqtSignal(str)

    def __init__(self, target_ip: str, adb_cmd: str):
        super().__init__()
        self.target_ip = target_ip
        self.adb_cmd = adb_cmd
        self.zeroconf = None
        self.is_connected = False
        self.stop_requested = False

    def run(self):
        if not Zeroconf: return
        self.zeroconf = Zeroconf()
        
        browser = ServiceBrowser(
            self.zeroconf, 
            "_adb-tls-connect._tcp.local.", 
            handlers=[self._on_service_added]
        )
        
        # Timeout safety (20 seconds)
        timeout = 40 
        while not self.stop_requested and not self.is_connected and timeout > 0:
            time.sleep(0.5)
            timeout -= 1
            
        self._cleanup()
        
        if not self.is_connected and not self.stop_requested:
            self.error_occurred.emit("Timed out waiting for device connection broadcast.")

    def stop(self):
        self.stop_requested = True

    def _cleanup(self):
        if self.zeroconf:
            self.zeroconf.close()

    def _on_service_added(self, zeroconf, service_type, name, state_change):
        if self.stop_requested or state_change != ServiceStateChange.Added or self.is_connected:
            return
            
        info = zeroconf.get_service_info(service_type, name)
        if not info or not info.addresses:
            return

        ip = info.parsed_addresses()[0]
        # Only auto-connect if the IP matches the one the user just paired with
        if ip == self.target_ip:
            address = f"{ip}:{info.port}"
            flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            
            try:
                subprocess.run([self.adb_cmd, "connect", address], capture_output=True, creationflags=flags)
                self.is_connected = True
                self.connected_success.emit(address)
            except Exception as e:
                self.error_occurred.emit(f"Connection Error: {str(e)}")


class WirelessADBWorker(QThread):
    """
    QR Worker: Handles two-stage discovery (Pairing service -> Connection service).
    """
    status_update = pyqtSignal(str)
    paired_success = pyqtSignal()
    connected_success = pyqtSignal(str)
    error_occurred = pyqtSignal(str)

    def __init__(self, service_name: str, password: str, adb_cmd: str):
        super().__init__()
        self.service_name = service_name
        self.password = password
        self.adb_cmd = adb_cmd
        
        self.zeroconf = None
        self.is_paired = False
        self.is_connected = False
        self.stop_requested = False

    def run(self):
        if not Zeroconf:
            self.error_occurred.emit("Dependency 'zeroconf' is missing.\nPlease install zeroconf.")
            return

        self.zeroconf = Zeroconf()
        self.status_update.emit("Listening for pairing service...")
        
        self.browser_pair = ServiceBrowser(
            self.zeroconf, 
            "_adb-tls-pairing._tcp.local.", 
            handlers=[self._on_service_added]
        )
        
        while not self.stop_requested and not self.is_connected:
            time.sleep(0.5)
            
        self._cleanup()

    def stop(self):
        self.stop_requested = True

    def _cleanup(self):
        if self.zeroconf:
            self.zeroconf.close()

    def _on_service_added(self, zeroconf, service_type, name, state_change):
        if self.stop_requested or state_change != ServiceStateChange.Added:
            return
            
        info = zeroconf.get_service_info(service_type, name)
        if not info:
            return

        if service_type == "_adb-tls-pairing._tcp.local." and not self.is_paired:
            self.status_update.emit("Found device! Pairing...")
            self._attempt_pair(info)
            
        elif service_type == "_adb-tls-connect._tcp.local." and self.is_paired:
            self.status_update.emit("Pairing confirmed! Connecting...")
            self._attempt_connect(info)

    def _attempt_pair(self, info):
        ip = info.parsed_addresses()[0] if info.addresses else None
        if not ip: return

        address = f"{ip}:{info.port}"
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        
        try:
            cmd = [self.adb_cmd, "pair", address, self.password]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=15, creationflags=flags)
            
            if "Successfully paired" in proc.stdout:
                self.is_paired = True
                self.paired_success.emit()
                ServiceBrowser(self.zeroconf, "_adb-tls-connect._tcp.local.", handlers=[self._on_service_added])
            else:
                self.error_occurred.emit(f"Pairing failed: {proc.stdout or proc.stderr}")
        except Exception as e:
            self.error_occurred.emit(f"Pairing Error: {str(e)}")

    def _attempt_connect(self, info):
        ip = info.parsed_addresses()[0] if info.addresses else None
        if not ip: return

        address = f"{ip}:{info.port}"
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        
        try:
            subprocess.run([self.adb_cmd, "connect", address], capture_output=True, creationflags=flags)
            self.is_connected = True
            self.connected_success.emit(address)
        except Exception as e:
            self.error_occurred.emit(f"Connection Error: {str(e)}")


class ManualPairWorker(QThread):
    """
    Handles manual 'Pairing Code' execution so the UI does not freeze.
    """
    finished = pyqtSignal(bool, str, str)  # success, output_msg, target_ip

    def __init__(self, adb_cmd: str, address: str, pin: str):
        super().__init__()
        self.adb_cmd = adb_cmd
        self.address = address
        self.pin = pin

    def run(self):
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        try:
            cmd = [self.adb_cmd, "pair", self.address, self.pin]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=16, creationflags=flags)
            
            if "Successfully paired" in proc.stdout:
                target_ip = self.address.split(':')[0]
                self.finished.emit(True, proc.stdout, target_ip)
            else:
                err = proc.stderr.strip() or proc.stdout.strip()
                self.finished.emit(False, err, "")
        except Exception as e:
            self.finished.emit(False, str(e), "")


class WirelessADBDialog(QDialog):

    def __init__(self, parent_app, adb_cmd: str):
        super().__init__(parent_app)
        self.parent_app = parent_app
        self.adb_cmd = adb_cmd
        self.worker = None
        self.auto_worker = None  # New worker for Pairing Code auto-connect
        self._setup_ui()

    def _setup_ui(self):
        self.setWindowTitle("Wireless ADB")
        self.setFixedSize(450, 480)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)
        self.setModal(True)

        main_layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        
        # --- 1. QR Pair Tab ---
        self.qr_tab = QWidget()
        qr_layout = QVBoxLayout(self.qr_tab)
        
        self.qr_img_label = QLabel()
        self.qr_img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.qr_img_label.setFixedSize(300, 300)
        
        qr_center_layout = QHBoxLayout()
        qr_center_layout.addStretch()
        qr_center_layout.addWidget(self.qr_img_label)
        qr_center_layout.addStretch()
        qr_layout.addLayout(qr_center_layout)

        instructions = QLabel("Scan this QR Code in Settings -> Developer options -> Wireless debugging -> Pair device with QR code")
        instructions.setWordWrap(True)
        instructions.setAlignment(Qt.AlignmentFlag.AlignCenter)
        qr_layout.addWidget(instructions)

        self.status_label = QLabel("Initializing...")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet("font-weight: bold; color: #4A90E2;")
        qr_layout.addWidget(self.status_label)

        self.tabs.addTab(self.qr_tab, "QR Connect (Android 11+)")
        
        # --- 2. Pairing Code Tab ---
        self.code_tab = QWidget()
        code_layout = QVBoxLayout(self.code_tab)
        code_layout.addStretch()
        
        code_label = QLabel("Enter Pairing Code Details from Device:")
        code_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        code_layout.addWidget(code_label)

        self.code_address_entry = QLineEdit()
        self.code_address_entry.setPlaceholderText("IP Address & Port (e.g., 192.168.1.100:43215)")
        self.code_address_entry.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.code_address_entry.setFixedWidth(270)
        self.code_pin_entry = QLineEdit()
        self.code_pin_entry.setPlaceholderText("6-Digit Wi-Fi Pairing Code")
        self.code_pin_entry.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.code_pin_entry.setFixedWidth(270)
        
        code_input_layout = QVBoxLayout()
        code_input_layout.addWidget(self.code_address_entry)
        code_input_layout.addWidget(self.code_pin_entry)
        
        code_center_layout = QHBoxLayout()
        code_center_layout.addStretch()
        code_center_layout.addLayout(code_input_layout)
        code_center_layout.addStretch()
        code_layout.addLayout(code_center_layout)

        pair_code_button = QPushButton("Pair & Connect")
        pair_code_button.setFixedWidth(140)
        pair_code_button.clicked.connect(self._pair_with_code)
        
        code_btn_layout = QHBoxLayout()
        code_btn_layout.addStretch()
        code_btn_layout.addWidget(pair_code_button)
        code_btn_layout.addStretch()
        code_layout.addLayout(code_btn_layout)
        
        self.code_status_label = QLabel("")
        self.code_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        code_layout.addWidget(self.code_status_label)
        
        code_layout.addStretch()
        self.tabs.addTab(self.code_tab, "Pairing Code")

        # --- 3. Manual Connect Tab ---
        self.manual_tab = QWidget()
        manual_layout = QVBoxLayout(self.manual_tab)
        manual_layout.addStretch()
        
        manual_label = QLabel("Enter Device Address (HOST:PORT):")
        manual_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        manual_layout.addWidget(manual_label)

        self.address_entry = QLineEdit()
        self.address_entry.setPlaceholderText("e.g., 192.168.1.100:5555")
        self.address_entry.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.address_entry.returnPressed.connect(self._connect_wireless_adb_manual)
        
        manual_center_layout = QHBoxLayout()
        manual_center_layout.addStretch()
        manual_center_layout.addWidget(self.address_entry)
        manual_center_layout.addStretch()
        manual_layout.addLayout(manual_center_layout)

        connect_button = QPushButton("Connect")
        connect_button.setFixedWidth(120)
        connect_button.clicked.connect(self._connect_wireless_adb_manual)
        
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_layout.addWidget(connect_button)
        btn_layout.addStretch()
        manual_layout.addLayout(btn_layout)
        manual_layout.addStretch()

        self.tabs.addTab(self.manual_tab, "Manual Connect")
        
        main_layout.addWidget(self.tabs)
        
        # Bottom controls
        bottom_layout = QHBoxLayout()
        self.cancel_btn = QPushButton("Close")
        self.cancel_btn.clicked.connect(self.reject)
        bottom_layout.addStretch()
        bottom_layout.addWidget(self.cancel_btn)
        main_layout.addLayout(bottom_layout)

        self.tabs.currentChanged.connect(self._on_tab_changed)
        QTimer.singleShot(100, self._init_qr_pairing)

    def _init_qr_pairing(self):
        password = "".join([str(secrets.randbelow(10)) for _ in range(6)])
        service_name = f"quickadb-{secrets.token_hex(3)}"

        payload = f"WIFI:T:ADB;S:{service_name};P:{password};;"
        try:
            qr = qrcode.QRCode(version=1, box_size=10, border=4)
            qr.add_data(payload)
            qr.make(fit=True)
            pil_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
            
            data = pil_img.tobytes("raw", "RGB")
            qim = QImage(data, pil_img.width, pil_img.height, pil_img.width * 3, QImage.Format.Format_RGB888).copy()
            pix = QPixmap.fromImage(qim).scaled(280, 280, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            self.qr_img_label.setPixmap(pix)
            
            self.worker = WirelessADBWorker(service_name, password, self.adb_cmd)
            self.worker.status_update.connect(self.status_label.setText)
            self.worker.paired_success.connect(self._on_paired)
            self.worker.connected_success.connect(self._on_connected)
            self.worker.error_occurred.connect(self._on_error)
            self.worker.start()

        except Exception as e:
            self.status_label.setText("Failed to start QR generator")
            QMessageBox.critical(self, "QR Error", f"Could not generate QR code:\n{str(e)}")

    def _on_tab_changed(self, index):
        if index == 1:
            self.code_address_entry.setFocus()
        elif index == 2:
            self.address_entry.setFocus()

    def _pair_with_code(self):
        address = self.code_address_entry.text().strip()
        pin = self.code_pin_entry.text().strip()

        if ":" not in address or not pin:
            QMessageBox.critical(self, "Input Error", "Please provide a valid IP:PORT and Pairing Code.")
            return

        self.code_status_label.setStyleSheet("color: #4A90E2;")
        self.code_status_label.setText("Pairing...")

        self.manual_pair_worker = ManualPairWorker(self.adb_cmd, address, pin)
        self.manual_pair_worker.finished.connect(self._on_manual_pair_finished)
        self.manual_pair_worker.start()

    def _on_manual_pair_finished(self, success: bool, msg: str, target_ip: str):
        if success:
            self.code_status_label.setStyleSheet("color: #2E8B57; font-weight: bold;")
            self.code_status_label.setText("Paired! Auto-connecting...")
            
            # Start lightweight auto-connector
            self.auto_worker = AutoConnectWorker(target_ip, self.adb_cmd)
            self.auto_worker.connected_success.connect(self._on_connected)
            self.auto_worker.error_occurred.connect(self._on_error)
            self.auto_worker.start()
        else:
            self.code_status_label.setStyleSheet("color: #FF0000;")
            self.code_status_label.setText("Pairing Failed")
            QMessageBox.warning(self, "Pairing Failed", f"Failed to pair:\n{msg}")

    def _on_paired(self):
        self.status_label.setStyleSheet("font-weight: bold; color: #2E8B57;")

    def _on_connected(self, address):
        self.status_label.setText(f"Connected to {address}!")
        self.status_label.setStyleSheet("font-weight: bold; color: #008000;")
        self.code_status_label.setText(f"Connected to {address}!")
        self.code_status_label.setStyleSheet("font-weight: bold; color: #008000;")
        
        QMessageBox.information(self, "Wireless ADB", f"Successfully connected to {address}")
        
        if hasattr(self.parent_app, 'refresh_devices'):
            QTimer.singleShot(500, self.parent_app.refresh_devices)
        self.accept()

    def _on_error(self, message):
        self.status_label.setText("Error occurred.")
        self.status_label.setStyleSheet("font-weight: bold; color: #FF0000;")
        self.code_status_label.setText("Error occurred.")
        self.code_status_label.setStyleSheet("font-weight: bold; color: #FF0000;")
        QMessageBox.warning(self, "Error", message)

    def _connect_wireless_adb_manual(self):
        address = self.address_entry.text().strip()
        if ":" not in address:
            QMessageBox.critical(self, "Error", "Please enter a valid address (HOST:PORT).")
            return

        if hasattr(self.parent_app, 'run_command_async'):
            self.parent_app.run_command_async(f"adb connect {address}", f"Connecting to {address}", "ADB")
            self.accept()

    def closeEvent(self, event):
        if self.worker:
            self.worker.stop()
            self.worker.wait(1000)
        if self.auto_worker:
            self.auto_worker.stop()
            self.auto_worker.wait(1000)
        super().closeEvent(event)