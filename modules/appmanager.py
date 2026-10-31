'''
appmanager.py - Lists apps and sorts them by their package names. Can also sort per app type (user / system).
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
import json
import shlex
import concurrent.futures
from typing import List, Optional, Tuple, Union

from util.apkapi import APKInstallOptions, install_prepared_package, prepare_package_source, supported_package_dialog_filter
from util.thememanager import ThemeManager
from util.adbclient import ADBClient

from util.resource import get_root_dir, resource_path
from util.toolpaths import ToolPaths
root_dir = get_root_dir()
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSortFilterProxyModel, QDir
from PyQt6.QtGui import QStandardItemModel, QStandardItem
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QTextEdit, QTreeView, QHeaderView,
    QMessageBox, QFileDialog, QLineEdit, QDialog, QDialogButtonBox,
    QComboBox, QTabWidget, QListWidget,
    QListWidgetItem
)


class AppManagerWorker(QThread):
    """Worker thread for handling long-running ADB operations."""
    finished = pyqtSignal()
    log_message = pyqtSignal(str)
    status_message = pyqtSignal(str, int)
    apps_loaded = pyqtSignal(list)
    app_details_loaded = pyqtSignal(dict)
    permissions_loaded = pyqtSignal(list, list, list)
    backup_progress = pyqtSignal(str, str)

    def __init__(self, operation, parent=None, **kwargs):
        super().__init__()
        self.operation = operation
        self.kwargs = kwargs
        self.parent = parent
        self.package_name = kwargs.get('package_name')
        self.package_names = kwargs.get('package_names')
        self.backup_items = kwargs.get('backup_items')
        self.permissions = kwargs.get('permissions')
        self.permission = kwargs.get('permission')
        self.action = kwargs.get('action')
        self.backup_path = kwargs.get('backup_path')
        self.save_dir = kwargs.get('save_dir')

        self.platform_tools_path = parent.platform_tools_path if parent else "."

    def run(self):
        """Executes the requested operation in a background thread."""
        packages = (
            self.kwargs.get('package_names') or
            getattr(self, 'package_names', None)
        )
        if not packages:
            pkg = getattr(self, 'package_name', None) or self.kwargs.get('package_name')
            if pkg:
                packages = [pkg]

        permissions = (
            self.kwargs.get('permissions') or
            getattr(self, 'permissions', None)
        )
        if not permissions:
            perm = getattr(self, 'permission', None) or self.kwargs.get('permission')
            if perm:
                permissions = [perm]

        items = (
            self.kwargs.get('backup_items') or
            getattr(self, 'backup_items', None) or
            packages
        )

        target_pkg = getattr(self, 'package_name', None) or self.kwargs.get('package_name')
        if not target_pkg and packages and len(packages) == 1:
            target_pkg = packages[0]
            self.package_name = target_pkg

        action = self.kwargs.get('action') or getattr(self, 'action', None)
        save_dir = self.kwargs.get('save_dir') or getattr(self, 'save_dir', None)
        backup_path = self.kwargs.get('backup_path') or getattr(self, 'backup_path', None)

        operations = {
            "load_apps": self.load_apps,
            "app_details": lambda: self.fetch_app_details(target_pkg),
            "permissions": lambda: self.fetch_permissions(target_pkg),
            "modify_app": lambda: (
                self.modify_apps(action, packages) if (packages and len(packages) > 1)
                else self.modify_app(action, target_pkg)
            ),
            "modify_apps": lambda: self.modify_apps(action, packages or []),
            "backup_app": lambda: (
                self.backup_apps_op(items, save_dir) if (items and len(items) > 1)
                else self.backup_app(target_pkg, save_dir, backup_path)
            ),
            "backup_apps": lambda: self.backup_apps_op(items or [], save_dir),
            "restore_app": lambda: self.restore_apps([self.kwargs.get('file_path')]),
            "restore_apps": lambda: self.restore_apps(self.kwargs.get('file_paths')),
            "modify_permission": lambda: (
                self.modify_permissions_op(target_pkg, permissions, action) if (permissions and len(permissions) > 1)
                else self.modify_permission(target_pkg, permissions[0] if permissions else self.kwargs.get('permission'), action)
            ),
            "modify_permissions": lambda: self.modify_permissions_op(
                target_pkg,
                permissions or [],
                action
            )
        }
        
        op_func = operations.get(self.operation)
        if op_func:
            op_func()

        self.finished.emit()
    
    def _run_adb_command(self, command_args, text=True):
        # Helper to run an ADB command consistently using ADBClient
        return ADBClient.instance().run(command_args, text=text)

    def _emit_status(self, message: str, timeout_ms: int = 5000):
        self.status_message.emit(message, timeout_ms)

    @staticmethod
    def _derive_app_name(package_name: str) -> str:
        """Derive a human-readable app name from a package name. In reality, this just filters out the last word from the package name and should be changed."""
        return package_name.split('.')[-1].replace('_', ' ').capitalize()

    @staticmethod
    def _parse_permissions(section_header: str, text: str) -> list:
        """Parse a named permission section from dumpsys package output."""
        match = re.search(section_header + r':\n((?:.+?\n)+?)(?:\n\S|\Z)', text, re.MULTILINE)
        if not match:
            return []
        return [line.strip() for line in match.group(1).strip().splitlines() if line.strip()]

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
        Parses package information from 'pm list packages -f' output
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
                app_name = self._derive_app_name(package_name)
                return app_name, package_name, app_type

        except Exception as e:
            self.log_message.emit(f"Error parsing package info '{package_info}': {e}")

        return None, None, None

    def fetch_app_details(self, package_name: Optional[str] = None):
        """Fetch detailed information about an app."""
        if package_name:
            self.package_name = package_name
        elif not getattr(self, 'package_name', None):
            self.package_name = self.kwargs.get('package_name')

        target_pkg = self.package_name
        if not target_pkg:
            self.log_message.emit("No package name specified for app details.")
            return

        try:
            result = self._run_adb_command(['shell', f'dumpsys package {shlex.quote(self.package_name)}'])
            if result.returncode != 0 or not result.stdout.strip():
                self.log_message.emit(f"Failed to fetch details for {target_pkg}")
                return

            output = result.stdout
            _m_code_path    = re.search(r'codePath=(.*)', output)
            _m_version_name = re.search(r'versionName=([\S]+)', output)
            _m_version_code = re.search(r'versionCode=(\d+)', output)
            _m_min_sdk      = re.search(r'minSdk=(\d+)', output)
            _m_target_sdk   = re.search(r'targetSdk=(\d+)', output)
            details = {
                "App Name":     self._derive_app_name(target_pkg),
                "Package Name": target_pkg,
                "App Path":     _m_code_path.group(1)    if _m_code_path    else "Unknown",
                "App Version":  f"{_m_version_name.group(1) if _m_version_name else '?'} "
                                f"(Code: {_m_version_code.group(1) if _m_version_code else '?'})",
                "Minimum SDK":  _m_min_sdk.group(1)      if _m_min_sdk      else "Unknown",
                "Target SDK":   _m_target_sdk.group(1)   if _m_target_sdk   else "Unknown",
            }
            self.app_details_loaded.emit(details)
        except Exception as e:
            self.log_message.emit(f"Error fetching details for {target_pkg}: {e}")

    def fetch_permissions(self, package_name: Optional[str] = None):
        """Fetch permissions for the specified package."""
        if package_name:
            self.package_name = package_name
        elif not getattr(self, 'package_name', None):
            self.package_name = self.kwargs.get('package_name')

        target_pkg = self.package_name
        if not target_pkg:
            self.log_message.emit("No package name specified for permissions.")
            return

        try:
            self._emit_status(f"Fetching permissions for {target_pkg}...", 0)
            result = self._run_adb_command(['shell', f'dumpsys package {shlex.quote(self.package_name)}'])
            if result.returncode != 0 or not result.stdout.strip():
                self.log_message.emit(f"Failed to fetch permissions for {target_pkg}.")
                self._emit_status(f"Failed to fetch permissions for {target_pkg}", 5000)
                return

            output = result.stdout
            
            declared_permissions = [p.split(':')[0] for p in self._parse_permissions(r'declared permissions', output)]
            requested_permissions = self._parse_permissions(r'requested permissions', output)
            
            runtime_permissions = []
            runtime_section = self._parse_permissions(r'runtime permissions', output)
            for line in runtime_section:
                match = re.match(r"(.+?): granted=(true|false)", line)
                if match:
                    perm, granted = match.groups()
                    runtime_permissions.append((perm.strip(), granted == "true"))

            self.log_message.emit(f"Fetched {len(declared_permissions)} declared, "
                                 f"{len(requested_permissions)} requested, "
                                 f"and {len(runtime_permissions)} runtime permissions.")
            self._emit_status(
                f"Fetched permissions for {target_pkg} "
                f"({len(runtime_permissions)} runtime, {len(requested_permissions)} requested).",
                5000
            )
            self.permissions_loaded.emit(declared_permissions, requested_permissions, runtime_permissions)
        except Exception as e:
            self.log_message.emit(f"Error fetching permissions for {target_pkg}: {e}")
            self._emit_status(f"Error fetching permissions for {target_pkg}", 5000)

    get_permissions = fetch_permissions

    def modify_app(self, action: Optional[str] = None, package_name: Optional[str] = None):
        """Modify an app (disable, enable, uninstall, stop)."""
        if package_name:
            self.package_name = package_name
        elif not getattr(self, 'package_name', None):
            self.package_name = self.kwargs.get('package_name')

        action = action or self.kwargs.get('action') or getattr(self, 'action', None)
        target_pkg = self.package_name
        if not target_pkg or not action:
            self.log_message.emit("Missing package name or action to modify app.")
            return

        try:
            self.log_message.emit(f"{action.capitalize()}ing {target_pkg}...")
            quoted_pkg = shlex.quote(self.package_name)
            command_map = {
                "stop": ['shell', 'am', 'stop-app', quoted_pkg],
                "disable": ['shell', 'pm', 'disable-user', '--user', '0', quoted_pkg],
                "enable": ['shell', 'pm', 'enable', quoted_pkg],
                "uninstall": ['uninstall', '--user', '0', target_pkg]
            }
            command = command_map.get(action)
            if not command:
                self.log_message.emit(f"Unknown action: {action}")
                return
            
            result = self._run_adb_command(command)
            if result.returncode == 0:
                past_tense = "stopped" if action == "stop" else f"{action}d"
                self.log_message.emit(f"Successfully {past_tense} {target_pkg}.")
            else:
                self.log_message.emit(f"Failed to {action} {target_pkg}: {result.stderr.strip()}")
        except Exception as e:
            self.log_message.emit(f"Error performing action '{action}': {e}")

    def modify_apps(self, action: str, package_names: List[str]):
        """Modify multiple apps sequentially."""
        if not package_names:
            self.log_message.emit("No apps specified to modify.")
            return
        total = len(package_names)
        for idx, pkg in enumerate(package_names, 1):
            if not pkg:
                continue
            self._emit_status(f"{action.capitalize()}ing {pkg} ({idx}/{total})...", 0)
            self.modify_app(action, pkg)
        self._emit_status(f"Finished {action} action on {total} app(s).", 5000)

    def modify_permission(self, package_name: Optional[str] = None, permission: Optional[str] = None, action: Optional[str] = None):
        """Grant or revoke a permission."""
        if package_name:
            self.package_name = package_name
        elif not getattr(self, 'package_name', None):
            self.package_name = self.kwargs.get('package_name')

        target_pkg = self.package_name
        action = action or self.kwargs.get('action') or getattr(self, 'action', None)
        permission = permission or self.kwargs.get('permission') or getattr(self, 'permission', None)

        if not target_pkg or not permission or not action:
            self.log_message.emit("Missing package, permission, or action for modify_permission.")
            return

        try:
            result = self._run_adb_command(['shell', 'pm', action, shlex.quote(self.package_name), shlex.quote(permission)])
            if "not a changeable permission type" in result.stderr:
                self.log_message.emit(f"Permission {permission} could not be {action}ed: Not changeable.")
            elif result.returncode == 0:
                self.log_message.emit(f"Permission {permission} successfully {action}ed.")
            else:
                self.log_message.emit(f"Failed to {action} permission {permission}: {result.stderr.strip()}")
        except Exception as e:
            self.log_message.emit(f"Error modifying permission: {e}")

    def grant_permission(self, arg1: str, arg2: Optional[str] = None):
        """Grant a permission. Supports (permission) or (package_name, permission)."""
        if arg2 is None:
            pkg = getattr(self, 'package_name', None) or self.kwargs.get('package_name')
            perm = arg1
        else:
            pkg = arg1
            perm = arg2
        if pkg:
            self.package_name = pkg
        return self.modify_permission(pkg, perm, "grant")

    def revoke_permission(self, arg1: str, arg2: Optional[str] = None):
        """Revoke a permission. Supports (permission) or (package_name, permission)."""
        if arg2 is None:
            pkg = getattr(self, 'package_name', None) or self.kwargs.get('package_name')
            perm = arg1
        else:
            pkg = arg1
            perm = arg2
        if pkg:
            self.package_name = pkg
        return self.modify_permission(pkg, perm, "revoke")

    def modify_permissions_op(self, package_name: str, permissions: List[str], action: str):
        """Grant or revoke multiple permissions sequentially."""
        if not permissions:
            self.log_message.emit(f"No permissions specified to {action}.")
            return
        total = len(permissions)
        for idx, perm in enumerate(permissions, 1):
            if not perm:
                continue
            self._emit_status(f"{action.capitalize()}ing permission {idx}/{total} for {package_name}...", 0)
            self.modify_permission(package_name, perm, action)
        self._emit_status(f"Finished {action} permissions for {package_name}.", 5000)

    def backup_app(self, package_name: Optional[str] = None, save_dir: Optional[str] = None, backup_path: Optional[str] = None):
        """Backup an app to a ZIP file."""
        if package_name:
            self.package_name = package_name
        elif not getattr(self, 'package_name', None):
            self.package_name = self.kwargs.get('package_name')

        if not self.package_name:
            self.log_message.emit("No package name specified for backup.")
            return

        if backup_path:
            self.backup_path = backup_path
        elif save_dir:
            self.backup_path = os.path.join(save_dir, f"backup_{self.package_name}.zip")
        elif not getattr(self, 'backup_path', None):
            effective_save_dir = getattr(self, 'save_dir', None) or self.kwargs.get('save_dir') or "."
            self.backup_path = os.path.join(effective_save_dir, f"backup_{self.package_name}.zip")

        if not self.backup_path.endswith(".zip"):
            self.backup_path = f"{self.backup_path}.zip"

        try:
            self.backup_progress.emit(self.package_name, "Fetching APK paths")
            result = self._run_adb_command(['shell', f'pm path {shlex.quote(self.package_name)}'])
            if result.returncode != 0 or not result.stdout.strip():
                self.log_message.emit(f"Failed to fetch APK paths for {self.package_name}.")
                self.backup_progress.emit(self.package_name, "Backup failed")
                return

            apk_paths = [line.replace("package:", "").strip() for line in result.stdout.strip().splitlines() if line.strip()]
            if not apk_paths:
                self.log_message.emit(f"No APK paths found for {self.package_name}.")
                self.backup_progress.emit(self.package_name, "Backup failed")
                return

            with tempfile.TemporaryDirectory(prefix=f"backup_{self.package_name}_") as temp_dir:
                self.backup_progress.emit(self.package_name, f"Pulling {len(apk_paths)} APK files")
                pull_success = self._pull_apks_parallel(apk_paths, temp_dir)

                apk_files = [f for f in os.listdir(temp_dir) if f.endswith('.apk')]
                if not pull_success or not apk_files:
                    if hasattr(self, 'backup_path') and self.backup_path and os.path.exists(self.backup_path):
                        try:
                            os.remove(self.backup_path)
                        except OSError:
                            pass
                    self.log_message.emit(f"[ERROR] Failed to pull all APK splits for {self.package_name}; backup aborted.")
                    self.backup_progress.emit(self.package_name, "Backup failed")
                    return

                self.backup_progress.emit(self.package_name, "Creating ZIP archive")
                archive_base = self.backup_path.rsplit(".zip", 1)[0]
                os.makedirs(os.path.dirname(os.path.abspath(self.backup_path)), exist_ok=True)
                shutil.make_archive(archive_base, 'zip', temp_dir)

                self.log_message.emit(f"Backup completed for {self.package_name}. Saved as {self.backup_path}")
                self.backup_progress.emit(self.package_name, "Backup completed")
        except Exception as e:
            if hasattr(self, 'backup_path') and self.backup_path and os.path.exists(self.backup_path):
                try:
                    os.remove(self.backup_path)
                except OSError:
                    pass
            self.log_message.emit(f"Error backing up {self.package_name}: {e}")
            self.backup_progress.emit(self.package_name, "Backup failed")

    def backup_apps_op(self, items, save_dir: Optional[str] = None):
        """Backup multiple apps sequentially.
        items can be a list of package names (str) or list of (pkg, backup_path/save_dir) tuples.
        """
        if not items:
            self.log_message.emit("No apps specified for backup.")
            return

        total = len(items)
        for idx, item in enumerate(items, 1):
            if isinstance(item, (tuple, list)):
                pkg = item[0]
                target = item[1] if len(item) > 1 else None
                if target and target.lower().endswith(".zip"):
                    self._emit_status(f"Backing up {pkg} ({idx}/{total})...", 0)
                    self.backup_app(package_name=pkg, backup_path=target)
                else:
                    target_dir = target or save_dir
                    self._emit_status(f"Backing up {pkg} ({idx}/{total})...", 0)
                    self.backup_app(package_name=pkg, save_dir=target_dir)
            else:
                pkg = item
                if not pkg:
                    continue
                self._emit_status(f"Backing up {pkg} ({idx}/{total})...", 0)
                self.backup_app(package_name=pkg, save_dir=save_dir)
        self._emit_status(f"Backup completed for {total} app(s).", 5000)

    def _pull_apks_parallel(self, apk_paths: List[str], temp_dir: str) -> bool:
        """Pull multiple APK files in parallel using a thread pool."""
        if not apk_paths:
            return False

        basenames = [os.path.basename(p) for p in apk_paths]
        dest_paths = []
        used_names = set()
        for idx, apk_path in enumerate(apk_paths):
            basename = os.path.basename(apk_path)
            if basenames.count(basename) > 1:
                filename = f"{idx}_{basename}"
            else:
                filename = basename
            if filename in used_names:
                filename = f"{idx}_{basename}"
                suffix = 1
                while filename in used_names:
                    filename = f"{idx}_{suffix}_{basename}"
                    suffix += 1
            used_names.add(filename)
            dest_paths.append(os.path.join(temp_dir, filename))

        def pull_single_apk(pair) -> bool:
            apk_path, dest_path = pair
            try:
                self.log_message.emit(f"Pulling {os.path.basename(dest_path)}...")
                result = self._run_adb_command(['pull', apk_path, dest_path])
                if result.returncode != 0:
                    self.log_message.emit(f"Failed to pull {apk_path}: {result.stderr.strip()}")
                    return False
                if not (os.path.exists(dest_path) and os.path.getsize(dest_path) > 0):
                    self.log_message.emit(f"Pulled file for {apk_path} is missing or empty.")
                    return False
                return True
            except Exception as e:
                self.log_message.emit(f"Error pulling {apk_path}: {e}")
                return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(apk_paths), 5)) as executor:
            results = list(executor.map(pull_single_apk, zip(apk_paths, dest_paths)))

        return all(results)

    def restore_apps(self, file_paths: List[str]):
        """Restore multiple apps from APK or archive package files sequentially."""
        if not file_paths:
            self.log_message.emit("No backup files selected for restore.")
            return

        self.backup_progress.emit("apps", f"Starting restore of {len(file_paths)} apps")
        self.log_message.emit(f"Starting sequential restore of {len(file_paths)} apps...")
        
        all_success = True
        for index, package_path in enumerate(file_paths):
            file_name = os.path.basename(package_path)
            current_app = os.path.splitext(file_name.replace("backup_", "", 1))[0]
            self.backup_progress.emit(current_app, f"Restoring ({index + 1}/{len(file_paths)})")
            try:
                prepared = prepare_package_source(package_path)
                try:
                    if not prepared.apk_paths:
                        self.log_message.emit(f"No APKs found in {package_path}")
                        all_success = False
                        continue

                    if prepared.install_mode == "multiple":
                        self.log_message.emit(f"Detected split APK with {len(prepared.apk_paths)} components. Installing together...")
                        self.backup_progress.emit(current_app, f"Installing split APK ({len(prepared.apk_paths)} components)")
                    elif prepared.install_mode == "sequential":
                        self.log_message.emit(f"Installing {len(prepared.apk_paths)} individual APK(s)...")
                        self.backup_progress.emit(current_app, f"Installing {len(prepared.apk_paths)} APK files")
                    else:
                        self.backup_progress.emit(current_app, "Installing APK")

                    if not self._install_prepared_package(prepared, current_app):
                        all_success = False
                finally:
                    prepared.cleanup()
                
                self.backup_progress.emit(current_app, "Restore completed")
                self.log_message.emit(f"Completed restore for {file_name}")
            except Exception as e:
                self.log_message.emit(f"Error restoring {package_path}: {e}")
                self.backup_progress.emit(current_app, f"Error: {str(e)}")
                all_success = False

        final_msg = "All backups restored successfully" if all_success else "Some backups failed to restore"
        self.backup_progress.emit("all_apps", final_msg)
        self.log_message.emit(final_msg)

    def _install_prepared_package(self, prepared, current_app: str = "") -> bool:
        """Execute a prepared package install and emit the same status/log format as before."""
        try:
            result = install_prepared_package(
                prepared,
                options=APKInstallOptions(reinstall=True),
            )
            
            for line in result.stdout.splitlines():
                if line.strip():
                    self.log_message.emit(line.strip())
            
            if result.returncode != 0:
                stderr_output = (result.stderr or result.stdout).strip()
                self.log_message.emit(f"Installation failed: {stderr_output}")
                if current_app:
                    self.backup_progress.emit(current_app, "Installation failed")
                return False

            self.log_message.emit("Installation completed successfully")
            if current_app:
                self.backup_progress.emit(current_app, "Installation successful")
            return True
        except Exception as e:
            self.log_message.emit(f"Error during APK installation: {e}")
            if current_app:
                self.backup_progress.emit(current_app, "Installation error")
            return False


class WorkerMixin:
    """Mixin providing worker thread lifecycle management."""

    def create_worker(self, operation, **kwargs):
        """Creates, tracks, and connects a worker thread for cleanup."""
        worker = AppManagerWorker(operation, parent=self, **kwargs)
        worker.finished.connect(lambda: self.cleanup_worker(worker))
        if hasattr(self, "_on_worker_status_message"):
            worker.status_message.connect(self._on_worker_status_message)
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


class AppDetailsDialog(WorkerMixin, QDialog):
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
        

        select_all_button.clicked.connect(lambda: self._toggle_select_all(list_widget))
        return list_widget

    def load_app_details(self):
        """Load app details and permissions using worker threads."""
        details_worker = self.create_worker("app_details", package_name=self.package_name)
        details_worker.app_details_loaded.connect(self.update_details)
        details_worker.log_message.connect(self.parent.log)
        details_worker.start()

        self.load_permissions()

    def update_details(self, details):
        """Update the details tab with app information."""
        self.details_text.clear()
        for key, value in details.items():
            self.details_text.append(f"<b>{key}:</b> {value}")

    def update_permissions(self, declared, requested, runtime):
        """Populate the permission lists."""
        def populate_list(widget, items, formatter=lambda x: x):
            widget.clear()
            for item in items:
                self._add_checkable_item(widget, formatter(item))

        populate_list(self.declared_list, declared)
        populate_list(self.requested_list, requested)
        populate_list(self.runtime_list, runtime, lambda r: f"{r[0]} (Granted: {r[1]})")

    def _add_checkable_item(self, list_widget, text):
        """Adds a checkable QListWidgetItem to a list."""
        item = QListWidgetItem(text)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Unchecked)
        list_widget.addItem(item)
    
    def _toggle_select_all(self, list_widget): 
        # Determine if we should check or uncheck all items
        is_anything_unchecked = any(list_widget.item(i).checkState() != Qt.CheckState.Checked for i in range(list_widget.count()))
        new_state = Qt.CheckState.Checked if is_anything_unchecked else Qt.CheckState.Unchecked
        
        for i in range(list_widget.count()):
            list_widget.item(i).setCheckState(new_state)

    @staticmethod
    def _get_checked_texts(list_widget) -> list:
        """Return the text of all checked items in a QListWidget."""
        return [
            list_widget.item(i).text()
            for i in range(list_widget.count())
            if list_widget.item(i).checkState() == Qt.CheckState.Checked
        ]

    def modify_permissions(self, action):
        """Grant or revoke selected permissions."""
        runtime_checked = [t.split(" (")[0] for t in self._get_checked_texts(self.runtime_list)]
        requested_checked = self._get_checked_texts(self.requested_list)
        selected_permissions = runtime_checked + requested_checked

        if not selected_permissions:
            QMessageBox.warning(self, "No Selection", f"No permissions selected to {action}.")
            return

        if QMessageBox.question(self, "Confirm Action", f"Are you sure you want to {action} the selected permissions?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.No:
            return

        worker = self.create_worker("modify_permissions", package_name=self.package_name, permissions=selected_permissions, action=action)
        worker.log_message.connect(self.parent.log)
        worker.finished.connect(self.load_permissions)
        worker.start()

    def load_permissions(self):
        """Refresh the permissions display."""
        permissions_worker = self.create_worker("permissions", package_name=self.package_name)
        permissions_worker.permissions_loaded.connect(self.update_permissions)
        permissions_worker.log_message.connect(self.parent.log)
        permissions_worker.start()

    refresh_permissions = load_permissions

    def _on_worker_status_message(self, message: str, timeout_ms: int = 5000):
        if hasattr(self.parent, "_on_worker_status_message"):
            self.parent._on_worker_status_message(message, timeout_ms)


PermissionDialog = AppDetailsDialog


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


class AppManagerUI(WorkerMixin, QMainWindow):
    """Main UI for the ADB App Manager."""
    def __init__(self, platform_tools_path_param=None):
        super().__init__()
        # Initialize platform_tools_path as an instance variable.
        self.platform_tools_path = platform_tools_path_param or ToolPaths.instance().platform_tools_dir
        
        self.setWindowTitle("QuickADB App Manager")
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
        self.tree_view.setSortingEnabled(False)
        self.model.beginResetModel()
        for app_name, package_name, status, app_type in apps:
            checkbox_item = QStandardItem()
            checkbox_item.setCheckable(True)
            self.model.appendRow([
                checkbox_item, QStandardItem(app_name), QStandardItem(package_name),
                QStandardItem(status), QStandardItem(app_type)
            ])
        self.model.endResetModel()
        self.tree_view.setSortingEnabled(True)
        
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
        
        # If the user clicked the actual checkbox column, Qt already toggled the visual state.
        # We just need to sync our `selected_packages` set.
        # If they clicked any other column, we manually toggle the visual state to match.
        if index.column() != 0:
            new_state = Qt.CheckState.Unchecked if checkbox_item.checkState() == Qt.CheckState.Checked else Qt.CheckState.Checked
            checkbox_item.setCheckState(new_state)
        
        # Sync the set with the final visual state
        if checkbox_item.checkState() == Qt.CheckState.Checked:
            self.selected_packages.add(package_name)
        else:
            self.selected_packages.discard(package_name)

    def deselect_all(self):
        """Deselect all apps."""
        for row in range(self.model.rowCount()):
            self.model.item(row, 0).setCheckState(Qt.CheckState.Unchecked)
        self.selected_packages.clear()
        self.log("Deselected all apps.")

    def perform_action(self, action):
        """Perform an action (uninstall, disable, enable, stop) on selected apps."""
        if not self.selected_packages:
            QMessageBox.warning(self, "No Selection", "No apps selected for this action.")
            return

        if QMessageBox.question(self, f"Confirm {action.capitalize()}", f"Are you sure you want to {action} {len(self.selected_packages)} selected app(s)?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.No:
            return

        packages = list(self.selected_packages)
        worker = self.create_worker("modify_apps", action=action, package_names=packages)
        worker.log_message.connect(self.log)
        worker.finished.connect(self.load_apps) # Refresh list once after batch finishes
        worker.start()

    perform_app_action = perform_action

    def backup_apps(self):
        """Backup selected apps."""
        if not self.selected_packages:
            QMessageBox.warning(self, "No Selection", "No apps selected for backup.")
            return

        save_dir = QFileDialog.getExistingDirectory(self, "Select Backup Directory")
        if not save_dir: return

        packages = list(self.selected_packages)
        worker = self.create_worker("backup_apps", package_names=packages, save_dir=save_dir)
        worker.log_message.connect(self.log)
        worker.backup_progress.connect(lambda pkg, msg: self.statusBar().showMessage(f"Backing up {pkg}: {msg}"))
        worker.finished.connect(lambda: self.statusBar().showMessage("Backup finished.", 5000))
        worker.start()

    def restore_apps(self):
        """Restore app(s) from backup."""
        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Select Backup or Package File(s)",
            "",
            supported_package_dialog_filter(),
        )
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
            dialog.exec()
            
    def init_ui(self):
        """Initialize the UI components."""
        central_widget = QWidget()
        main_layout = QVBoxLayout(central_widget)
        self.statusBar()
        ThemeManager.apply_theme(self)
        self._apply_statusbar_theme_color()

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
        self.model.setHorizontalHeaderLabels(["Check", "App Name", "Package Name", "Status", "App Type"])
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
        self.tree_view.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.tree_view.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.tree_view.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.tree_view.header().setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        self.tree_view.header().setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        self.tree_view.setColumnWidth(0, 60)
        self.tree_view.setColumnWidth(1, 200)
        self.tree_view.setColumnWidth(2, 500)
        self.tree_view.setColumnWidth(3, 70)
        self.tree_view.setColumnWidth(4, 70)

        main_layout.addWidget(self.tree_view, 1)

        # Action buttons
        button_grid = QHBoxLayout()
        actions = {"Stop": "stop", "Uninstall": "uninstall", "Disable": "disable", "Enable": "enable"}
        for name, action in actions.items():
            button = QPushButton(f"{name} Selected")
            button.clicked.connect(lambda _, a=action: self.perform_action(a))
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

    def _apply_statusbar_theme_color(self):
        self.statusBar().setStyleSheet(
            f"QStatusBar {{ color: {ThemeManager.TEXT_COLOR_PRIMARY}; }}"
            "QStatusBar::item { border: none; }"
        )

    def _on_worker_status_message(self, message: str, timeout_ms: int = 5000):
        bar = self.statusBar()
        bar.setVisible(True)
        bar.showMessage(message, max(0, timeout_ms))



def run_app_manager(custom_platform_tools_path=None):
    """Initializes and runs the AppManagerUI application."""
    existing = QApplication.instance()
    app = existing or QApplication(sys.argv)
    window = AppManagerUI(platform_tools_path_param=custom_platform_tools_path)
    window.show()
    if not existing:
        return app.exec()
    return window

if __name__ == "__main__":
    sys.exit(run_app_manager(custom_platform_tools_path=resource_path('platform-tools')))
