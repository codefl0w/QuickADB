'''
Lists apps and sorts them by their package names. Can also sort per app type (user / system).
Lets the user select and delete or disable / enable apps, take APK backups of selected apps and restore them,
create and use presets and manage app permissions.

The backup restoring feature can also install .apkm or .xapk files since they're just base.apk + split APKs packed together,
just like QuickADB's backups.

 '''

import sys
import os
import re
import tempfile
import shutil
import zipfile
import threading
import subprocess
import json
import concurrent.futures
from typing import List

from util.thememanager import ThemeManager

script_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(script_dir)
sys.path.insert(0, root_dir)

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSize, QSortFilterProxyModel, QDir
from PyQt6.QtGui import QIcon, QStandardItemModel, QStandardItem, QFont
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QTextEdit, QTreeView, QFrame, QHeaderView,
    QMessageBox, QFileDialog, QLineEdit, QDialog, QCheckBox, QDialogButtonBox,
    QComboBox, QTabWidget, QGroupBox, QListWidget, QMenu, QToolButton,
    QProgressBar, QScrollArea, QListWidgetItem
)


class AppManagerWorker(QThread):
    """Worker thread for handling long-running ADB operations."""
    finished = pyqtSignal()
    log_message = pyqtSignal(str)
    apps_loaded = pyqtSignal(list)
    app_details_loaded = pyqtSignal(dict)
    permissions_loaded = pyqtSignal(list, list, list)
    backup_progress = pyqtSignal(str, str)

    def __init__(self, operation, parent=None, **kwargs):
        super().__init__()
        self.operation = operation
        self.kwargs = kwargs
        self.parent = parent
        # ROBUSTNESS: Get path from parent instance, not a global variable.
        self.platform_tools_path = parent.platform_tools_path if parent else "."

    def run(self):
        """Executes the requested operation in a background thread."""
        operations = {
            "load_apps": self.load_apps,
            "app_details": lambda: self.fetch_app_details(self.kwargs.get('package_name')),
            "permissions": lambda: self.fetch_permissions(self.kwargs.get('package_name')),
            "modify_app": lambda: self.modify_app(self.kwargs.get('action'), self.kwargs.get('package_name')),
            "backup_app": lambda: self.backup_app(self.kwargs.get('package_name'), self.kwargs.get('save_dir')),
            "restore_app": lambda: self.restore_apps([self.kwargs.get('file_path')]),
            "restore_apps": lambda: self.restore_apps(self.kwargs.get('file_paths')),
            "modify_permission": lambda: self.modify_permission(
                self.kwargs.get('package_name'),
                self.kwargs.get('permission'),
                self.kwargs.get('action')
            )
        }
        
        op_func = operations.get(self.operation)
        if op_func:
            op_func()

        self.finished.emit()
    
    def _run_adb_command(self, command_args, text=True):
        """Helper to run an ADB command consistently."""
        adb_exe = os.path.join(self.platform_tools_path, 'adb')
        full_command = [adb_exe] + command_args
        return subprocess.run(full_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=text, check=False)

    def load_apps(self):
        """Load all installed apps from the device."""
        self.log_message.emit("Fetching installed apps...")
        try:
            # Fetch all packages with their paths
            result = self._run_adb_command(['shell', 'pm', 'list', 'packages', '-f'])
            if result.returncode != 0:
                self.log_message.emit(f"Error fetching apps: {result.stderr.strip()}")
                return
            packages = result.stdout.splitlines()

            # Fetch disabled packages to determine status
            disabled_result = self._run_adb_command(['shell', 'pm', 'list', 'packages', '-d'])
            disabled_packages = {line.replace("package:", "").strip() for line in disabled_result.stdout.splitlines()}

            all_apps = []
            for package_info in packages:
                app_name, package_name, app_type = self.parse_app_info(package_info)
                if app_name and package_name:
                    status = "Disabled" if package_name in disabled_packages else "Enabled"
                    all_apps.append((app_name, package_name, status, app_type))

            self.log_message.emit(f"Loaded {len(all_apps)} apps successfully.")
            self.apps_loaded.emit(all_apps)
        except Exception as e:
            self.log_message.emit(f"Error loading apps: {e}")

    def parse_app_info(self, package_info: str):
            """
            Parses package information from 'pm list packages -f' output,
            correctly handling complex paths for user apps.
            """
            try:
                # The package name is always the last segment after the final '='.
                parts = package_info.split('=')
                package_name = parts[-1].strip()
    
                # The path is everything before the last '='.
                path_part = '='.join(parts[:-1])
                file_path = path_part.replace("package:", "").strip()
    
                if package_name:
                    app_type = "Unknown"
                    if "/data/app/" in file_path:
                        app_type = "User App"
                    elif "/system/app/" in file_path or "/system/priv-app/" in file_path:
                        app_type = "System App"
                    elif "/vendor/" in file_path:
                        app_type = "Vendor App"
    
                    # Use the last part of the package name as a readable app name
                    app_name = package_name.split('.')[-1].replace('_', ' ').capitalize()
                    return app_name, package_name, app_type
    
            except Exception as e:
                self.log_message.emit(f"Error parsing package info '{package_info}': {e}")
    
            return None, None, None

    def fetch_app_details(self, package_name):
        """Fetch detailed information about an app."""
        try:
            result = self._run_adb_command(['shell', f'dumpsys package {package_name}'])
            if result.returncode != 0 or not result.stdout.strip():
                self.log_message.emit(f"Failed to fetch details for {package_name}")
                return

            output = result.stdout
            details = {
                "App Name": package_name.split('.')[-1].replace("_", " ").capitalize(),
                "Package Name": package_name,
                "App Path": (re.search(r'codePath=(.*)', output).group(1) if re.search(r'codePath=(.*)', output) else "Unknown"),
                "App Version": f"{(re.search(r'versionName=([\S]+)', output).group(1) if re.search(r'versionName=([\S]+)', output) else '?')} "
                               f"(Code: {(re.search(r'versionCode=(\d+)', output).group(1) if re.search(r'versionCode=(\d+)', output) else '?')})",
                "Minimum SDK": (re.search(r'minSdk=(\d+)', output).group(1) if re.search(r'minSdk=(\d+)', output) else "Unknown"),
                "Target SDK": (re.search(r'targetSdk=(\d+)', output).group(1) if re.search(r'targetSdk=(\d+)', output) else "Unknown"),
            }
            self.app_details_loaded.emit(details)
        except Exception as e:
            self.log_message.emit(f"Error fetching details for {package_name}: {e}")

    def fetch_permissions(self, package_name):
        """Fetch permissions for the specified package."""
        try:
            result = self._run_adb_command(['shell', f'dumpsys package {package_name}'])
            if result.returncode != 0 or not result.stdout.strip():
                self.log_message.emit(f"Failed to fetch permissions for {package_name}.")
                return

            output = result.stdout
            
            def parse_permissions(section_header, text):
                match = re.search(section_header + r':\n((?:.+?\n)+?)(?:\n\S|\Z)', text, re.MULTILINE)
                if not match: return []
                lines = match.group(1).strip().splitlines()
                return [line.strip() for line in lines if line.strip()]

            declared_permissions = [p.split(':')[0] for p in parse_permissions(r'declared permissions', output)]
            requested_permissions = parse_permissions(r'requested permissions', output)
            
            runtime_permissions = []
            runtime_section = parse_permissions(r'runtime permissions', output)
            for line in runtime_section:
                match = re.match(r"(.+?): granted=(true|false)", line)
                if match:
                    perm, granted = match.groups()
                    runtime_permissions.append((perm.strip(), granted == "true"))

            self.log_message.emit(f"Fetched {len(declared_permissions)} declared, "
                                 f"{len(requested_permissions)} requested, "
                                 f"and {len(runtime_permissions)} runtime permissions.")
            self.permissions_loaded.emit(declared_permissions, requested_permissions, runtime_permissions)
        except Exception as e:
            self.log_message.emit(f"Error fetching permissions for {package_name}: {e}")

    def modify_app(self, action, package_name):
        """Modify an app (disable, enable, uninstall)."""
        try:
            self.log_message.emit(f"{action.capitalize()}ing {package_name}...")
            command_map = {
                "disable": ['shell', 'pm', 'disable-user', '--user', '0', package_name],
                "enable": ['shell', 'pm', 'enable', package_name],
                "uninstall": ['uninstall', '--user', '0', package_name]
            }
            command = command_map.get(action)
            if not command:
                self.log_message.emit(f"Unknown action: {action}")
                return
            
            result = self._run_adb_command(command)
            if result.returncode == 0:
                self.log_message.emit(f"Successfully {action}d {package_name}.")
            else:
                self.log_message.emit(f"Failed to {action} {package_name}: {result.stderr.strip()}")
        except Exception as e:
            self.log_message.emit(f"Error performing action '{action}': {e}")

    def modify_permission(self, package_name, permission, action):
        """Grant or revoke a permission."""
        try:
            result = self._run_adb_command(['shell', f'pm {action} {package_name} {permission}'])
            if "not a changeable permission type" in result.stderr:
                self.log_message.emit(f"Permission {permission} could not be {action}ed: Not changeable.")
            elif result.returncode == 0:
                self.log_message.emit(f"Permission {permission} successfully {action}ed.")
            else:
                self.log_message.emit(f"Failed to {action} permission {permission}: {result.stderr.strip()}")
        except Exception as e:
            self.log_message.emit(f"Error modifying permission: {e}")

    def backup_app(self, package_name: str, save_dir: str):
        """Backup an app to a ZIP file."""
        try:
            self.backup_progress.emit(package_name, "Fetching APK paths")
            result = self._run_adb_command(['shell', f'pm path {package_name}'])
            if result.returncode != 0 or not result.stdout.strip():
                self.log_message.emit(f"Failed to fetch APK paths for {package_name}.")
                return

            apk_paths = [line.replace("package:", "").strip() for line in result.stdout.strip().splitlines()]
            if not apk_paths:
                self.log_message.emit(f"No APK paths found for {package_name}.")
                return

            with tempfile.TemporaryDirectory(prefix=f"backup_{package_name}_") as temp_dir:
                self.backup_progress.emit(package_name, f"Pulling {len(apk_paths)} APK files")
                self._pull_apks_parallel(apk_paths, temp_dir)

                zip_filename = os.path.join(save_dir, f"backup_{package_name}.zip")
                self.backup_progress.emit(package_name, "Creating ZIP archive")
                shutil.make_archive(zip_filename.replace(".zip", ""), 'zip', temp_dir)

                self.log_message.emit(f"Backup completed for {package_name}. Saved as {zip_filename}")
                self.backup_progress.emit(package_name, "Backup completed")
        except Exception as e:
            self.log_message.emit(f"Error backing up {package_name}: {e}")

    def _pull_apks_parallel(self, apk_paths: List[str], temp_dir: str):
        """Pull multiple APK files in parallel using a thread pool."""
        def pull_single_apk(apk_path: str):
            try:
                self.log_message.emit(f"Pulling {os.path.basename(apk_path)}...")
                result = self._run_adb_command(['pull', apk_path, temp_dir])
                if result.returncode != 0:
                    self.log_message.emit(f"Failed to pull {apk_path}: {result.stderr.strip()}")
            except Exception as e:
                self.log_message.emit(f"Error pulling {apk_path}: {e}")
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(apk_paths), 5)) as executor:
            executor.map(pull_single_apk, apk_paths)

    def restore_apps(self, file_paths: List[str]):
        """Restore multiple apps from backup ZIP files sequentially."""
        if not file_paths:
            self.log_message.emit("No backup files selected for restore.")
            return

        self.backup_progress.emit("apps", f"Starting restore of {len(file_paths)} apps")
        self.log_message.emit(f"Starting sequential restore of {len(file_paths)} apps...")
        
        all_success = True
        for index, zip_path in enumerate(file_paths):
            current_app = os.path.basename(zip_path).replace("backup_", "").replace(".zip", "")
            self.backup_progress.emit(current_app, f"Restoring ({index + 1}/{len(file_paths)})")
            try:
                with tempfile.TemporaryDirectory(prefix=f"restore_{current_app}_") as temp_dir:
                    self.backup_progress.emit(current_app, "Extracting ZIP archive")
                    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                        zip_ref.extractall(temp_dir)

                    apk_files = [os.path.join(root, file) for root, _, files in os.walk(temp_dir) for file in files if file.endswith(".apk")]
                    if not apk_files:
                        self.log_message.emit(f"No APKs found in {zip_path}")
                        all_success = False
                        continue
                    
                    self.backup_progress.emit(current_app, f"Installing {len(apk_files)} APK files")
                    if not self._install_apks(apk_files, current_app):
                        all_success = False
                
                self.backup_progress.emit(current_app, "Restore completed")
                self.log_message.emit(f"Completed restore for {os.path.basename(zip_path)}")
            except Exception as e:
                self.log_message.emit(f"Error restoring {zip_path}: {e}")
                self.backup_progress.emit(current_app, f"Error: {str(e)}")
                all_success = False

        final_msg = "All backups restored successfully" if all_success else "Some backups failed to restore"
        self.backup_progress.emit("all_apps", final_msg)
        self.log_message.emit(final_msg)

    def _install_apks(self, apk_files: List[str], current_app: str = "") -> bool:
        """Install APK files, handling split APKs correctly."""
        if not apk_files:
            return False
        
        is_split_apk = len(apk_files) > 1 and any("base.apk" in apk.lower() for apk in apk_files)
        if is_split_apk:
            self.log_message.emit(f"Detected split APK with {len(apk_files)} components. Installing together...")
            self.backup_progress.emit(current_app, f"Installing split APK ({len(apk_files)} components)")
            command = ['install-multiple', '-r'] + apk_files
            return self._execute_install_command(command, current_app)
        else:
            self.log_message.emit(f"Installing {len(apk_files)} individual APK(s)...")
            success = True
            for i, apk in enumerate(apk_files):
                self.backup_progress.emit(current_app, f"Installing APK {i + 1}/{len(apk_files)}")
                command = ['install', '-r', apk]
                if not self._execute_install_command(command, current_app):
                    success = False
            return success

    def _execute_install_command(self, command_args: List[str], current_app: str = "") -> bool:
        """Execute an ADB install command and handle the output."""
        try:
            adb_exe = os.path.join(self.platform_tools_path, 'adb')
            process = subprocess.Popen([adb_exe] + command_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)

            for line in iter(process.stdout.readline, ''):
                if line.strip(): self.log_message.emit(line.strip())
            
            return_code = process.wait()
            if return_code != 0:
                stderr_output = process.stderr.read().strip()
                self.log_message.emit(f"Installation failed: {stderr_output}")
                if current_app: self.backup_progress.emit(current_app, f"Installation failed")
                return False
            else:
                self.log_message.emit("Installation completed successfully")
                if current_app: self.backup_progress.emit(current_app, "Installation successful")
                return True
        except Exception as e:
            self.log_message.emit(f"Error during APK installation: {e}")
            if current_app: self.backup_progress.emit(current_app, "Installation error")
            return False


class AppDetailsDialog(QDialog):
    """Dialog showing detailed app information and permissions."""
    def __init__(self, parent, package_name):
        super().__init__(parent)
        self.parent = parent
        self.package_name = package_name
        self.platform_tools_path = parent.platform_tools_path
        self.active_workers = []

        self.setWindowTitle(f"Details: {package_name}")
        self.setFixedSize(850, 700)
        self.setModal(True)

        self.init_ui()
        self.load_app_details()

    def create_worker(self, operation, **kwargs):
        """Creates, tracks, and connects a worker thread for cleanup."""
        worker = AppManagerWorker(operation, parent=self, **kwargs)
        worker.finished.connect(lambda: self.cleanup_worker(worker))
        self.active_workers.append(worker)
        return worker

    def cleanup_worker(self, worker):
        """Removes a worker from the active list upon completion."""
        if worker in self.active_workers:
            self.active_workers.remove(worker)

    def closeEvent(self, event):
        """Ensures all background workers are finished before closing."""
        for worker in self.active_workers:
            worker.wait()
        event.accept()

    def init_ui(self):
        """Initialize the UI components."""
        main_layout = QVBoxLayout(self)
        self.tab_widget = QTabWidget()

        # Details Tab
        self.details_widget = QWidget()
        details_layout = QVBoxLayout(self.details_widget)
        self.details_text = QTextEdit()
        self.details_text.setReadOnly(True)
        details_layout.addWidget(self.details_text)
        self.tab_widget.addTab(self.details_widget, "App Details")

        # Permissions Tab
        self.permissions_widget = QWidget()
        permissions_layout = QVBoxLayout(self.permissions_widget)
        
        self.declared_list = self._create_permission_section(permissions_layout, "Declared Permissions (Read-Only)")
        self.requested_list = self._create_permission_section(permissions_layout, "Requested Permissions")
        self.runtime_list = self._create_permission_section(permissions_layout, "Runtime Permissions (Grant/Revoke)")
        
        permission_buttons = QHBoxLayout()
        self.grant_button = QPushButton("Grant Selected")
        self.revoke_button = QPushButton("Revoke Selected")
        self.grant_button.clicked.connect(lambda: self.modify_permissions("grant"))
        self.revoke_button.clicked.connect(lambda: self.modify_permissions("revoke"))
        permission_buttons.addWidget(self.grant_button)
        permission_buttons.addWidget(self.revoke_button)
        permissions_layout.addLayout(permission_buttons)
        self.tab_widget.addTab(self.permissions_widget, "Permissions")

        main_layout.addWidget(self.tab_widget)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.close)
        main_layout.addWidget(close_button)

    def _create_permission_section(self, parent_layout, title):
        """Helper to create a section for displaying permissions."""
        header_layout = QHBoxLayout()
        header_layout.addWidget(QLabel(title))
        select_all_button = QPushButton("Select / Deselect All")
        select_all_button.setFixedSize(130, 30)
        header_layout.addWidget(select_all_button)
        parent_layout.addLayout(header_layout)

        list_widget = QListWidget()
        list_widget.setMinimumHeight(100)
        list_widget.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        parent_layout.addWidget(list_widget)
        
        # CODE CLEANUP: Connect button to the refactored helper function.
        select_all_button.clicked.connect(lambda: self._toggle_select_all(list_widget))
        return list_widget

    def load_app_details(self):
        """Load app details and permissions using worker threads."""
        details_worker = self.create_worker("app_details", package_name=self.package_name)
        details_worker.app_details_loaded.connect(self.update_details)
        details_worker.log_message.connect(self.parent.log)
        details_worker.start()

        self.refresh_permissions()

    def update_details(self, details):
        """Update the details tab with app information."""
        self.details_text.clear()
        for key, value in details.items():
            self.details_text.append(f"<b>{key}:</b> {value}")

    def update_permissions(self, declared, requested, runtime):
        """Populate the permission lists."""
        self.declared_list.clear()
        for perm in declared:
            self._add_checkable_item(self.declared_list, perm)

        self.requested_list.clear()
        for perm in requested:
            self._add_checkable_item(self.requested_list, perm)

        self.runtime_list.clear()
        for perm, granted in runtime:
            self._add_checkable_item(self.runtime_list, f"{perm} (Granted: {granted})")

    def _add_checkable_item(self, list_widget, text):
        """Adds a checkable QListWidgetItem to a list."""
        item = QListWidgetItem(text)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Unchecked)
        list_widget.addItem(item)
    
    def _toggle_select_all(self, list_widget):
        """CODE CLEANUP: Replaces three separate methods with one generic helper."""
        # Determine if we should check or uncheck all items
        is_anything_unchecked = any(list_widget.item(i).checkState() != Qt.CheckState.Checked for i in range(list_widget.count()))
        new_state = Qt.CheckState.Checked if is_anything_unchecked else Qt.CheckState.Unchecked
        
        for i in range(list_widget.count()):
            list_widget.item(i).setCheckState(new_state)

    def modify_permissions(self, action):
        """Grant or revoke selected permissions."""
        selected_permissions = []
        for i in range(self.runtime_list.count()):
            item = self.runtime_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                selected_permissions.append(item.text().split(" (")[0])
        for i in range(self.requested_list.count()):
            item = self.requested_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                selected_permissions.append(item.text())

        if not selected_permissions:
            QMessageBox.warning(self, "No Selection", f"No permissions selected to {action}.")
            return

        if QMessageBox.question(self, "Confirm Action", f"Are you sure you want to {action} the selected permissions?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.No:
            return

        for permission in selected_permissions:
            worker = self.create_worker("modify_permission", package_name=self.package_name, permission=permission, action=action)
            worker.log_message.connect(self.parent.log)
            worker.finished.connect(self.refresh_permissions) # Refresh after each one
            worker.start()

    def refresh_permissions(self):
        """Refresh the permissions display."""
        permissions_worker = self.create_worker("permissions", package_name=self.package_name)
        permissions_worker.permissions_loaded.connect(self.update_permissions)
        permissions_worker.log_message.connect(self.parent.log)
        permissions_worker.start()


class CustomSortProxyModel(QSortFilterProxyModel):
    """Custom sort filter proxy model for special sorting rules."""
    STATUS_ORDER = {"Enabled": 0, "Disabled": 1}
    APP_TYPE_ORDER = {"User App": 0, "System App": 1, "Vendor App": 2, "Unknown": 3}

    def lessThan(self, left, right):
        """Custom comparison for sorting Status and App Type columns."""
        column = left.column()
        left_data = self.sourceModel().data(left)
        right_data = self.sourceModel().data(right)

        if column == 3:  # Status column
            return self.STATUS_ORDER.get(left_data, 99) < self.STATUS_ORDER.get(right_data, 99)
        elif column == 4:  # App Type column
            return self.APP_TYPE_ORDER.get(left_data, 99) < self.APP_TYPE_ORDER.get(right_data, 99)
        
        return super().lessThan(left, right)


class AppManagerUI(QMainWindow):
    """Main UI for the ADB App Manager."""
    def __init__(self, platform_tools_path_param=None):
        super().__init__()
        # ROBUSTNESS: Initialize platform_tools_path as an instance variable.
        self.platform_tools_path = platform_tools_path_param or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'platform-tools')
        
        self.setWindowTitle("ADB App Manager")
        self.setMinimumSize(1000, 700)
        
        self.selected_packages = set()
        self.active_workers = []
        
        self.init_ui()
        self.load_apps()

    def create_preset(self):
        """Open dialog to create a new preset."""
        if not self.selected_packages:
            QMessageBox.warning(self, "No Apps Selected", "Please select apps before creating a preset.")
            return

        dialog = self.CreatePresetDialog(self)
        self.apply_dialog_theme(dialog)
        if dialog.exec():
            preset_name = dialog.name_input.text().strip() or "New Preset"
            preset_data = {
                "name": preset_name,
                "author": dialog.author_input.text().strip(),
                "description": dialog.description_input.toPlainText().strip(),
                "selected_packages": sorted(list(self.selected_packages))
            }
            
            file_path, _ = QFileDialog.getSaveFileName(self, "Save Preset", preset_name + ".json", "JSON Files (*.json)")
            if not file_path: return

            try:
                with open(file_path, 'w') as f:
                    json.dump(preset_data, f, indent=4)
                self.log(f"Created preset '{preset_name}' with {len(preset_data['selected_packages'])} apps.")
            except Exception as e:
                QMessageBox.critical(self, "Error Saving Preset", f"Failed to save preset: {e}")

    def load_preset(self):
        """Open dialog to select and load a preset."""
        file_path, _ = QFileDialog.getOpenFileName(self, "Open Preset", QDir.homePath(), "JSON Files (*.json)")
        if file_path: self._apply_preset(file_path)

    def _apply_preset(self, preset_path):
        """Apply the selected preset."""
        try:
            with open(preset_path, 'r') as f:
                preset_data = json.load(f)

            packages_in_preset = set(preset_data.get("selected_packages", []))
            if not packages_in_preset:
                self.log("Preset is empty or invalid.")
                return

            self.deselect_all()
            for row in range(self.model.rowCount()):
                package_name = self.model.item(row, 2).text()
                if package_name in packages_in_preset:
                    self.model.item(row, 0).setCheckState(Qt.CheckState.Checked)
                    self.selected_packages.add(package_name)
            
            self.log(f"Loaded preset '{preset_data.get('name', 'Untitled')}' with {len(self.selected_packages)} apps selected.")
        except Exception as e:
            QMessageBox.critical(self, "Error Loading Preset", f"Failed to load preset: {e}")

    def apply_dialog_theme(self, dialog):
        dialog.setStyleSheet(ThemeManager.get_dialog_style())

    class CreatePresetDialog(QDialog):
        """Dialog for creating a new preset."""
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setWindowTitle("Create Preset")
            self.setFixedSize(400, 350)
            layout = QVBoxLayout(self)

            layout.addWidget(QLabel("Preset Name:"))
            self.name_input = QLineEdit()
            layout.addWidget(self.name_input)
            
            layout.addWidget(QLabel("Author (optional):"))
            self.author_input = QLineEdit()
            layout.addWidget(self.author_input)
            
            layout.addWidget(QLabel("Description (optional):"))
            self.description_input = QTextEdit()
            self.description_input.setMaximumHeight(100)
            layout.addWidget(self.description_input)

            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
            buttons.accepted.connect(self.accept)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)

    def create_worker(self, operation, **kwargs):
        """Creates, tracks, and connects a worker thread for cleanup."""
        worker = AppManagerWorker(operation, parent=self, **kwargs)
        worker.finished.connect(lambda: self.cleanup_worker(worker))
        self.active_workers.append(worker)
        return worker

    def cleanup_worker(self, worker):
        """Removes a worker from the active list upon completion."""
        if worker in self.active_workers:
            self.active_workers.remove(worker)

    def closeEvent(self, event):
        """Ensures all background workers are finished before closing."""
        for worker in self.active_workers:
            worker.wait()
        event.accept()

    def log(self, message):
        """Add a message to the log output."""
        self.log_output.append(message)
        self.log_output.verticalScrollBar().setValue(self.log_output.verticalScrollBar().maximum())

    def load_apps(self):
        """Load installed apps from the device."""
        self.model.removeRows(0, self.model.rowCount())
        self.selected_packages.clear()
        
        worker = self.create_worker("load_apps")
        worker.log_message.connect(self.log)
        worker.apps_loaded.connect(self.populate_app_list)
        worker.finished.connect(lambda: self.statusBar().showMessage("Ready", 5000))
        worker.start()
        self.statusBar().showMessage("Loading apps...")

    def populate_app_list(self, apps):
        """Populate the app list with fetched data efficiently."""
        # PERFORMANCE: Use begin/endResetModel for efficient bulk updates.
        self.model.beginResetModel()
        for app_name, package_name, status, app_type in apps:
            checkbox_item = QStandardItem()
            checkbox_item.setCheckable(True)
            self.model.appendRow([
                checkbox_item, QStandardItem(app_name), QStandardItem(package_name),
                QStandardItem(status), QStandardItem(app_type)
            ])
        self.model.endResetModel()
        
        self.statusBar().showMessage(f"Loaded {len(apps)} apps", 5000)

    def filter_apps(self):
        """Filter apps based on search text."""
        self.proxy_model.setFilterRegularExpression(self.search_input.text())

    def toggle_selection(self, index):
        """Toggle app selection when its row is clicked."""
        source_index = self.proxy_model.mapToSource(index)
        row = source_index.row()
        if row < 0: return

        checkbox_item = self.model.item(row, 0)
        package_name = self.model.item(row, 2).text()
        
        # Toggle checkbox state regardless of which column was clicked
        new_state = Qt.CheckState.Unchecked if checkbox_item.checkState() == Qt.CheckState.Checked else Qt.CheckState.Checked
        checkbox_item.setCheckState(new_state)
        
        if new_state == Qt.CheckState.Checked:
            self.selected_packages.add(package_name)
        else:
            self.selected_packages.discard(package_name)

    def deselect_all(self):
        """Deselect all apps."""
        for row in range(self.model.rowCount()):
            self.model.item(row, 0).setCheckState(Qt.CheckState.Unchecked)
        self.selected_packages.clear()
        self.log("Deselected all apps.")

    def perform_app_action(self, action):
        """Perform an action (uninstall, disable, enable) on selected apps."""
        if not self.selected_packages:
            QMessageBox.warning(self, "No Selection", "No apps selected for this action.")
            return

        if QMessageBox.question(self, f"Confirm {action.capitalize()}", f"Are you sure you want to {action} {len(self.selected_packages)} selected app(s)?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.No:
            return

        for package_name in list(self.selected_packages):
            worker = self.create_worker("modify_app", action=action, package_name=package_name)
            worker.log_message.connect(self.log)
            worker.finished.connect(self.load_apps) # Refresh list after action
            worker.start()

    def backup_apps(self):
        """Backup selected apps."""
        if not self.selected_packages:
            QMessageBox.warning(self, "No Selection", "No apps selected for backup.")
            return

        save_dir = QFileDialog.getExistingDirectory(self, "Select Backup Directory")
        if not save_dir: return

        for package_name in self.selected_packages:
            worker = self.create_worker("backup_app", package_name=package_name, save_dir=save_dir)
            worker.log_message.connect(self.log)
            worker.backup_progress.connect(lambda pkg, msg: self.statusBar().showMessage(f"Backing up {pkg}: {msg}"))
            worker.start()

    def restore_apps(self):
        """Restore app(s) from backup."""
        file_paths, _ = QFileDialog.getOpenFileNames(self, "Select Backup ZIP File(s)", "", "ZIP Files (*.zip)")
        if not file_paths: return

        worker = self.create_worker("restore_apps", file_paths=file_paths)
        worker.log_message.connect(self.log)
        worker.backup_progress.connect(lambda pkg, msg: self.statusBar().showMessage(f"Restoring {pkg}: {msg}"))
        worker.finished.connect(self.load_apps)
        worker.start()

    def show_app_details(self):
        """Show details for the selected app."""
        selected_indexes = self.tree_view.selectedIndexes()
        if not selected_indexes:
            QMessageBox.warning(self, "No Selection", "No app selected.")
            return
        
        source_index = self.proxy_model.mapToSource(selected_indexes[2]) # Column 2 is package name
        package_name = self.model.data(source_index)
        
        if package_name:
            dialog = AppDetailsDialog(self, package_name)
            self.apply_dialog_theme(dialog)
            dialog.exec()
            
    def init_ui(self):
        """Initialize the UI components."""
        central_widget = QWidget()
        main_layout = QVBoxLayout(central_widget)
        ThemeManager.apply_theme(self)

        # Search and actions
        top_layout = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search by app or package name...")
        self.search_input.textChanged.connect(self.filter_apps)
        top_layout.addWidget(QLabel("Search:"))
        top_layout.addWidget(self.search_input)
        self.refresh_button = QPushButton("Refresh List")
        self.refresh_button.clicked.connect(self.load_apps)
        top_layout.addWidget(self.refresh_button)
        main_layout.addLayout(top_layout)

        # App list view
        self.model = QStandardItemModel(0, 5)
        self.model.setHorizontalHeaderLabels(["", "App Name", "Package Name", "Status", "App Type"])
        self.proxy_model = CustomSortProxyModel()
        self.proxy_model.setSourceModel(self.model)
        self.proxy_model.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.proxy_model.setFilterKeyColumn(-1)
        
        self.tree_view = QTreeView()
        self.tree_view.setModel(self.proxy_model)
        self.tree_view.setSortingEnabled(True)
        self.tree_view.setEditTriggers(QTreeView.EditTrigger.NoEditTriggers)
        self.tree_view.setAlternatingRowColors(True)
        self.tree_view.clicked.connect(self.toggle_selection)
        self.tree_view.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tree_view.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.tree_view.setColumnWidth(0, 30)
        main_layout.addWidget(self.tree_view, 1)

        # Action buttons
        button_grid = QHBoxLayout()
        actions = {"Uninstall": "uninstall", "Disable": "disable", "Enable": "enable"}
        for name, action in actions.items():
            button = QPushButton(f"{name} Selected")
            button.clicked.connect(lambda _, a=action: self.perform_app_action(a))
            button_grid.addWidget(button)
        
        button_grid.addWidget(QPushButton("Deselect All", clicked=self.deselect_all))
        main_layout.addLayout(button_grid)

        # Backup/Restore/Details buttons
        io_button_layout = QHBoxLayout()
        io_button_layout.addWidget(QPushButton("Create Preset", clicked=self.create_preset))
        io_button_layout.addWidget(QPushButton("Load Preset", clicked=self.load_preset))
        io_button_layout.addStretch()
        io_button_layout.addWidget(QPushButton("Backup Selected", clicked=self.backup_apps))
        io_button_layout.addWidget(QPushButton("Restore from Backup", clicked=self.restore_apps))
        io_button_layout.addWidget(QPushButton("Show App Details", clicked=self.show_app_details))
        main_layout.addLayout(io_button_layout)
        
        # Log output
        main_layout.addWidget(QLabel("Log Output:"))
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMaximumHeight(150)
        main_layout.addWidget(self.log_output)
        
        self.setCentralWidget(central_widget)
        self.statusBar().showMessage("Ready")


def run_app_manager(custom_platform_tools_path=None):
    """Initializes and runs the AppManagerUI application."""
    app = QApplication.instance() or QApplication(sys.argv)
    window = AppManagerUI(platform_tools_path_param=custom_platform_tools_path)
    window.show()
    # Only call app.exec() if we created the QApplication instance in this function
    if not QApplication.instance():
        return app.exec()
    return window

if __name__ == "__main__":
    script_dir = os.path.dirname(os.path.abspath(__file__))
    platform_tools_path = os.path.join(script_dir, '..', 'platform-tools') # Adjust path if needed
    sys.exit(run_app_manager(custom_platform_tools_path=platform_tools_path))