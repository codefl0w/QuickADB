'''
Main page. Handles some variables and can do simple adb / fastboot tasks, so it's esentially enough to run on its own.
Other adb tasks that require a bit more attention are handled by the adbfunc.py module.

'''
import sys
import os
import threading
import subprocess
import webbrowser
import requests
import time
from datetime import datetime
from functools import partial
from typing import Optional, List, Tuple, Callable


script_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(script_dir)
sys.path.insert(0, root_dir)

from modules.terminal import show_terminal_window
from modules.payloaddumper import show_payload_dumper_window
from modules.gsiflasher import GSIFlasherUI
from modules.superdumper import show_super_img_dumper
from modules.fileexplorer import ADBFileExplorer
import modules.appmanager as appmanager
from modules.partitionmanager import PartitionManager
from util.thememanager import ThemeManager
import adbfunc

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QPushButton, QLabel, QFrame,
    QGridLayout, QVBoxLayout, QHBoxLayout, QTextEdit, QMessageBox,
    QFileDialog, QScrollArea, QDialog, QTextBrowser
)
from PyQt6.QtGui import QIcon, QTextCursor, QColor
from PyQt6.QtCore import Qt, QThread, pyqtSignal

try:
    from PyQt6.QtSvgWidgets import QSvgWidget
except ImportError:
    QSvgWidget = None


class CommandRunner(QThread):
    """
    Runs shell commands in a separate thread to avoid blocking the GUI.
    Streams stdout and stderr in real-time.

    Signals:
    output_signal(str, str): Emits each line of output with a tag ("Output" or "Error").

    """
    output_signal = pyqtSignal(str, str)

    def __init__(self, command: str, platform_tools_path: str):
        super().__init__()
        self.command = command
        self.platform_tools_path = platform_tools_path

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
            process = subprocess.Popen(
                self.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=True,
                cwd=self.platform_tools_path,
                text=True,
                bufsize=1  # Line-buffered
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


class QuickADBApp(QMainWindow):
    # Main window

    # Constants 
    APP_VERSION = "V4.0.0"
    APP_SUFFIX = "Full"
    BUTTON_WIDTH = 150
    BUTTON_HEIGHT = 40
    GITHUB_URL = "https://github.com/codefl0w/QuickADB"
    XDA_URL = "https://xdaforums.com/t/tool-quickadb-a-gui-to-execute-adb-fastboot-commands.4690673/"
    DONATE_URL = "https://buymeacoffee.com/fl0w" # please?
    CONTACT_EMAIL = "mailto:fl0w_dev@protonmail.com"

    def __init__(self):
        super().__init__()

        # State 
        self.adb_version = "Unknown"
        self.fastboot_version = "Unknown"
        self.platform_tools_path = os.path.join(root_dir, 'platform-tools')
        self.command_runner = None
        self.payload_dumper_window = None
        self.super_dumper_window = None
        self.terminal_window = None
        self.file_explorer_window = None
        self.app_manager_window = None
        self.gsi_window = None
        self.partition_manager = None

        # Init
        self.init_ui()
        self.fetch_versions()
        self.log_initial_info()
        ThemeManager.apply_theme(self)
        adbfunc.add_methods_to_class(self)
        self.check_for_update()

    # UI Setup 

    def init_ui(self):
        self.setWindowTitle("QuickADB")
        self.setMinimumSize(1050, 620)
        self.setWindowIcon(QIcon(os.path.join(script_dir, "toolicon.ico")))

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(15, 10, 15, 10)

        # Logo
        logo_container = QWidget()
        logo_layout = QVBoxLayout(logo_container)
        logo_layout.setContentsMargins(0, 0, 0, 0)
        svg_path = os.path.join(root_dir, "res", "logo.svg") # PNG sucks when it comes to scalability. The logo doesn't scale anyways but whatever.
        if QSvgWidget and os.path.exists(svg_path):
            logo_widget = QSvgWidget(svg_path)
            logo_widget.setFixedSize(250, 65)
            logo_layout.addWidget(logo_widget, 0, Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(logo_container)

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
        self.extract_button = QPushButton("Extract Logs")
        self.extract_button.clicked.connect(self.extract_logs)
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

    def create_menu_button(self, text: str, callback: Callable) -> QPushButton:
        """Creates a standard menu button."""
        button = QPushButton(text)
        button.setFixedSize(self.BUTTON_WIDTH, self.BUTTON_HEIGHT)
        button.clicked.connect(callback)
        return button

    def create_command_button(self, text: str, callback: Callable, row: int, column: int) -> QPushButton:
        """Creates a command button and adds it to the grid layout."""
        button = QPushButton(text)
        button.setFixedSize(self.BUTTON_WIDTH, self.BUTTON_HEIGHT)
        button.clicked.connect(callback)
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
        """Constructs the full path for an executable in platform-tools."""
        if not os.path.isdir(self.platform_tools_path):
            QMessageBox.critical(self, "Error", f"platform-tools folder not found at: {self.platform_tools_path}")
            return None
        return os.path.join(self.platform_tools_path, name)

    def run_command_async(self, command: str, description: str, command_type: str):
        """Executes a command asynchronously in a separate thread."""
        current_time = time.strftime("%H:%M:%S")
        self.log_action(f"[{current_time}] Executing {command_type} command: {description}", "#00ffff")

        self.command_runner = CommandRunner(command, self.platform_tools_path)
        self.command_runner.output_signal.connect(self.log_terminal_output)
        self.command_runner.start()

    def _populate_commands_grid(self, commands: List[Tuple[str, Callable]], items_per_row: int = 3):
        """Dynamically creates and places command buttons in a grid."""
        self.clear_buttons()
        for i, (text, callback) in enumerate(commands):
            row, col = divmod(i, items_per_row)
            self.create_command_button(text, callback, row, col)

    # --- Command Button Sections ---

    def show_adb_commands(self):
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
            ("Payload.bin Dumper", self.show_payload_dumper),
            ("ADB App Manager", self.launch_app_manager),
            ("GSI Flasher", self.open_gsi_flasher),
            ("Dump super.img", self.launch_super_dumper),
            ("Partition Manager", self.open_partition_manager),
            ("File Explorer", self.open_file_explorer),
        ]
        self._populate_commands_grid(commands)

    def show_misc(self):
        """Populates the grid with miscellaneous command buttons."""
        commands = [("Device Specifications", self.show_device_info)]
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

    def open_terminal(self):
        self.terminal_window = show_terminal_window(
            app_version=self.APP_VERSION, app_suffix=self.APP_SUFFIX,
            adb_version=self.adb_version, fastboot_version=self.fastboot_version,
        )

    def open_file_explorer(self):
        self.file_explorer_window = ADBFileExplorer(platform_tools_path=self.platform_tools_path)
        self.file_explorer_window.show()

    def launch_app_manager(self):
        self.app_manager_window = appmanager.run_app_manager(self.platform_tools_path)

    def open_gsi_flasher(self):
        self.gsi_window = GSIFlasherUI()
        self.gsi_window.show()

    def open_partition_manager(self):
        self.partition_manager = PartitionManager(self.platform_tools_path)
        self.partition_manager.show()

    def show_payload_dumper(self):
        _, self.payload_dumper_window = show_payload_dumper_window(QApplication.instance())

    def launch_super_dumper(self):
        self.super_dumper_window = show_super_img_dumper(self)

    # --- Placeholder Methods ---
    def open_pull_window(self): QMessageBox.information(self, "Not Implemented", "ADB Pull feature coming soon.")
    def open_push_window(self): QMessageBox.information(self, "Not Implemented", "ADB Push feature coming soon.")
    def install_apk(self): QMessageBox.information(self, "Not Implemented", "Install APK feature coming soon.")
    def uninstall_app(self): QMessageBox.information(self, "Not Implemented", "Uninstall App feature coming soon.")
    def sideload_file(self): QMessageBox.information(self, "Not Implemented", "ADB Sideload feature coming soon.")
    def show_device_info(self): QMessageBox.information(self, "Not Implemented", "Device specifications feature coming soon.")
    def show_wireless_adb_ui(self):
        if callable(getattr(self, "_wireless_adb_impl", None)):
            self._wireless_adb_impl()
        else:
            QMessageBox.information(self, "Not Implemented", "Wireless ADB UI not yet implemented.")

    # --- Versioning & Update Check ---
    class UpdateCheckerThread(QThread):
        update_available = pyqtSignal(str, str)
        no_update = pyqtSignal()
        error = pyqtSignal(str)
        def __init__(self, current_version): super().__init__(); self.current_version = current_version
        def run(self):
            try:
                response = requests.get(f"{QuickADBApp.GITHUB_URL}/releases/latest", headers={"Accept": "application/json"})
                response.raise_for_status()
                latest_version = response.json().get("tag_name", "")
                if latest_version and latest_version > self.current_version:
                    self.update_available.emit(latest_version, self.current_version)
                else: self.no_update.emit()
            except requests.RequestException as e: self.error.emit(f"Could not check for updates: {e}")
            except Exception as e: self.error.emit(f"An unexpected error occurred: {e}")

    def fetch_versions(self):
        """Fetches ADB and Fastboot versions in separate threads."""
        threading.Thread(target=self._fetch_version, args=("adb", "version", "adb_version"), daemon=True).start()
        threading.Thread(target=self._fetch_version, args=("fastboot", "--version", "fastboot_version"), daemon=True).start()

    def _fetch_version(self, executable: str, command: str, version_attr: str):
        """Worker function to get version info from a command-line tool."""
        try:
            exe_path = self._get_executable_path(executable)
            if not exe_path: return
            result = subprocess.run([exe_path, command], capture_output=True, text=True, check=False)
            if result.returncode == 0:
                setattr(self, version_attr, result.stdout.splitlines()[0])
            else:
                setattr(self, version_attr, f"{executable} not found or error occurred")
        except Exception as e:
            setattr(self, version_attr, f"Error fetching version: {e}")

    def check_for_update(self):
        self.update_thread = self.UpdateCheckerThread(self.APP_VERSION)
        self.update_thread.update_available.connect(self.on_update_available)
        self.update_thread.no_update.connect(lambda: self.log_action("You're using the latest version.\n"))
        self.update_thread.error.connect(lambda msg: QMessageBox.critical(self, "Update Check Failed", msg))
        self.update_thread.start()

    def on_update_available(self, latest_version: str, current_version: str):
        reply = QMessageBox.question(self, "Update Available",
            f"A new version ({latest_version}) is available! You are using {current_version}.\n\n"
            "Would you like to visit the GitHub releases page to download it?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            webbrowser.open(f"{self.GITHUB_URL}/releases")

    # --- Logging ---

    def log_action(self, action: str, color: Optional[str] = None):
        self.logs_text.moveCursor(QTextCursor.MoveOperation.End)
        if color: self.logs_text.setTextColor(QColor(color))
        self.logs_text.insertPlainText(action + "\n")
        self.logs_text.setTextColor(QColor("black")) # Reset to default
        self.logs_text.ensureCursorVisible()

    def log_terminal_output(self, output: str, tag: str = ""):
        self.logs_text.moveCursor(QTextCursor.MoveOperation.End)
        color = "#ff4d4d" if "error" in tag.lower() else "#2edf85"
        self.logs_text.setTextColor(QColor(color))
        self.logs_text.insertPlainText(output + "\n")
        self.logs_text.setTextColor(QColor("black"))
        self.logs_text.ensureCursorVisible()

    def log_initial_info(self):
        current_time = datetime.now().strftime("%d/%m/%Y, %H:%M")
        self.logs_text.setTextColor(QColor("#ffffff"))
        self.logs_text.append(f"{current_time} - Current version: {self.APP_VERSION} {self.APP_SUFFIX}\n\n"
                              "Some features are experimental and are not expected to work on every device. "
                              "Look for the warning icon (⚠️) to distinguish them.\n")

    def extract_logs(self):
        file_path, _ = QFileDialog.getSaveFileName(self, "Save Logs", "", "Text Files (*.txt)")
        if file_path:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(self.logs_text.toPlainText())

    # --- Event Handlers & Dialogs ---
    def view_github(self): webbrowser.open(self.GITHUB_URL)
    def view_xda_thread(self): webbrowser.open(self.XDA_URL)
    def donate(self): webbrowser.open(self.DONATE_URL)
    def contact(self): webbrowser.open(self.CONTACT_EMAIL)

    def show_about(self, event=None):
        dialog = QDialog(self)
        dialog.setWindowTitle("About QuickADB")
        layout = QVBoxLayout(dialog)
        info_label = QLabel(
            f"<h2>QuickADB - ADB & Fastboot Utility</h2>"
            f"<p>Version: {self.APP_VERSION} {self.APP_SUFFIX}</p>"
            f"<p>{self.adb_version}</p><p>{self.fastboot_version}</p><hr>"
            f"<p style='font-size: 8pt;'>Credits:<br>"
            f"- Uses payload-dumper-go by ssut (Apache 2.0)<br>"
            f"- Uses unsuper to dump super.img files<br>"
            f"- Developed by fl0w</p>"
        )
        info_label.setTextFormat(Qt.TextFormat.RichText); info_label.setWordWrap(True)
        layout.addWidget(info_label)
        
        buttons = {"Select Theme": self.show_theme_selector, "What's New?": self.show_whats_new,
                   "Check for Updates": self.check_for_update, "Close": dialog.close}
        for text, callback in buttons.items():
            btn = QPushButton(text); btn.clicked.connect(callback)
            layout.addWidget(btn)
        
        dialog.exec()

    def show_theme_selector(self):
        dialog = QDialog(self); dialog.setWindowTitle("Select Theme")
        layout = QVBoxLayout(dialog); layout.addWidget(QLabel("Available Themes:"))
        themes_path = os.path.join(os.getcwd(), "themes")
        found = False
        if os.path.exists(themes_path):
            for filename in os.listdir(themes_path):
                if filename.lower().endswith(".qss"):
                    theme_name = os.path.splitext(filename)[0].capitalize()
                    btn = QPushButton(theme_name)
                    btn.clicked.connect(partial(ThemeManager.load_qss, self, os.path.join(themes_path, filename)))
                    layout.addWidget(btn)
                    found = True
        classic_btn = QPushButton("Classic"); classic_btn.clicked.connect(lambda: ThemeManager.apply_classic(self))
        layout.addWidget(classic_btn)
        if not found: layout.addWidget(QLabel("(No .qss themes found)"))
        dialog.exec()

    def show_whats_new(self):
        html_path = os.path.join(root_dir, "res", "whatsnew.html")
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

if __name__ == "__main__":
    app = QApplication(sys.argv)
    main_window = QuickADBApp()
    sys.exit(app.exec())