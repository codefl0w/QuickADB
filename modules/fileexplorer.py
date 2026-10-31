'''
fileexplorer.py - QuickADB file explorer. List and create directories, push / pull files, execute scripts, preview images,
edit text documents, manage UNIX permissions and view their properties, as well as the usual file explorer stuff like
cut / copy & paste and renaming.

'''

import sys
import os
import subprocess
import tempfile
import base64
import re
import shlex
import time

from util.apkapi import APKInstallOptions, install_remote_package
from util.resource import get_root_dir, resource_path
from util.toolpaths import ToolPaths
from util.adbclient import ADBClient
root_dir = get_root_dir()
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from util.thememanager import ThemeManager
from util.devicemanager import DeviceManager

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QVBoxLayout, QWidget, QLabel, QLineEdit,
    QPushButton, QHBoxLayout, QFrame, QMessageBox, QMenu, QInputDialog,
    QTableWidget, QTableWidgetItem, QHeaderView, QComboBox, QFileDialog,
    QPlainTextEdit, QToolBar, QStatusBar, QProgressDialog, QSizePolicy,
    QCheckBox, QDialog, QGridLayout
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSize
from PyQt6.QtGui import QAction, QPixmap, QFont


class ADBThread(QThread):
    """Run simple adb shell/pull/push commands without blocking UI."""
    command_finished = pyqtSignal(str, bool)  # (output, is_error)

    def __init__(self, adb_path, args):
        super().__init__()
        self.adb_path = adb_path or ToolPaths.instance().adb
        self.args = args  # list of args after the adb binary
        self.process = None
        self._stop_requested = False

    def stop(self):
        self._stop_requested = True
        if self.process:
            try:
                self.process.terminate()
            except Exception:
                pass

    def run(self):
        if self._stop_requested:
            return
        try:
            process = ADBClient.instance().popen(
                self.args,
                tool="adb",
                use_serial=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            self.process = process
            if self._stop_requested:
                if self.process:
                    try:
                        self.process.terminate()
                    except Exception:
                        pass
                return
            stdout, stderr = self.process.communicate()
            if self._stop_requested:
                return
            if self.process.returncode == 0:
                self.command_finished.emit(stdout, False)
            else:
                out = stdout if stdout else stderr
                self.command_finished.emit(out or f"Exit code {self.process.returncode}", True)
        except Exception as e:
            if not self._stop_requested:
                self.command_finished.emit(str(e), True)


class RemoteAPKInstallThread(QThread):
    """Install an APK that already exists on the device filesystem."""

    install_finished = pyqtSignal(object)
    install_failed = pyqtSignal(str)

    def __init__(self, remote_path: str, use_root: bool):
        super().__init__()
        self.remote_path = remote_path
        self.use_root = use_root
        self._stop_requested = False

    def stop(self):
        self._stop_requested = True

    def run(self):
        try:
            result = install_remote_package(
                self.remote_path,
                options=APKInstallOptions(reinstall=True),
                use_root=self.use_root,
            )
            if not self._stop_requested:
                self.install_finished.emit(result)
        except Exception as exc:
            if not self._stop_requested:
                self.install_failed.emit(str(exc))


class TransferRunner(QThread):
    """Run long-running adb pull/push in background so UI doesn't freeze."""
    transfer_finished = pyqtSignal(str, bool, str)  # output, is_error, local_dest (or src)
    transfer_progress = pyqtSignal(str)  # free-form progress line (not used for UI progress bar here)

    def __init__(self, adb_path, args, cwd=None, root_enabled=False):
        super().__init__()
        self.adb_path = adb_path or ToolPaths.instance().adb
        self.args = args
        self.cwd = cwd or os.getcwd()
        self.root_enabled = root_enabled
        self.process = None
        self._stop_requested = False

    def stop(self):
        self._stop_requested = True
        if self.process:
            try:
                self.process.terminate()
            except Exception:
                pass

    def pull_Wait(self, source_path, target_path):
        """Safely pull a file or folder without modifying original device permissions."""
        res = ADBClient.instance().run(['pull', source_path, target_path], tool="adb", use_serial=True)
        returncode = getattr(res, 'returncode', getattr(res, 'return_code', 0))
        if returncode != 0 and getattr(self, 'root_enabled', False):
            clean_src = source_path.replace('\\', '/').rstrip('/')
            base = os.path.basename(clean_src) or 'item'
            tmp_stage = f"/data/local/tmp/quickadb_pull_{int(time.time()*1000)}_{base}"
            try:
                stage_cmd = f'su -c "cp -r {shlex.quote(source_path)} {shlex.quote(tmp_stage)} && chmod -R 755 {shlex.quote(tmp_stage)}"'
                ADBClient.instance().run(['shell', stage_cmd], tool="adb", use_serial=True)
                res = ADBClient.instance().run(['pull', tmp_stage, target_path], tool="adb", use_serial=True)
            finally:
                clean_cmd = f'su -c "rm -rf {shlex.quote(tmp_stage)}"'
                ADBClient.instance().run(['shell', clean_cmd], tool="adb", use_serial=True)
        return res

    def run(self):
        if self._stop_requested:
            return
        try:
            process = ADBClient.instance().popen(
                self.args,
                tool="adb",
                use_serial=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=self.cwd,
                text=True,
                bufsize=1
            )
            self.process = process
            if self._stop_requested:
                if self.process:
                    try:
                        self.process.terminate()
                    except Exception:
                        pass
                return
            proc = self.process

            last_line = ""
            while True:
                if self._stop_requested:
                    break
                line = proc.stdout.readline()
                if not line and proc.poll() is not None:
                    break
                if line:
                    last_line = line.rstrip("\n")
                    self.transfer_progress.emit(last_line)

            ret = proc.wait()
            if self._stop_requested:
                return
            if ret == 0:
                local_target = self.args[-1] if len(self.args) >= 2 else ""
                self.transfer_finished.emit(last_line or "Transfer completed", False, local_target)
            else:
                self.transfer_finished.emit(last_line or f"Transfer failed (code {ret})", True, "")
        except Exception as e:
            if not self._stop_requested:
                self.transfer_finished.emit(str(e), True, "")
class TextEditorDialog(QDialog):
    """Inline text editor dialog with safe device saving via adb push."""

    def __init__(self, explorer, filename: str, content: str, full_path: str):
        super().__init__(explorer)
        self.explorer = explorer
        self.filename = filename
        self.full_path = full_path
        self.content = content
        self.setWindowTitle(f"Contents of {filename}")
        self.setMinimumSize(800, 600)
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        self.text_edit = QPlainTextEdit()
        self.text_edit.setPlainText(self.content)
        self.text_edit.setReadOnly(False)
        self.text_edit.setFont(QFont("Courier New", 10))
        layout.addWidget(self.text_edit)

        button_layout = QHBoxLayout()
        copy_button = QPushButton("Copy to Clipboard")
        copy_button.clicked.connect(lambda: QApplication.clipboard().setText(self.text_edit.toPlainText()))
        button_layout.addWidget(copy_button)

        save_as_button = QPushButton("Save As...")
        save_as_button.clicked.connect(self.save_as)
        button_layout.addWidget(save_as_button)

        save_device_button = QPushButton("Save to Device")
        save_device_button.clicked.connect(self.save_to_device)
        button_layout.addWidget(save_device_button)

        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)
        button_layout.addWidget(close_button)

        layout.addLayout(button_layout)

    def _show_status(self, msg: str):
        if not self.explorer:
            return
        sb = getattr(self.explorer, 'statusBar', None)
        if callable(sb):
            sb = sb()
        if sb and hasattr(sb, 'showMessage'):
            sb.showMessage(msg)

    def save_as(self):
        if hasattr(self.explorer, 'save_file_content'):
            self.explorer.save_file_content(self.filename, self.text_edit.toPlainText())

    def save_to_device(self):
        content = self.text_edit.toPlainText()
        reply = QMessageBox.question(
            self, "Save to Device",
            f"Are you sure you want to save changes to {self.full_path} on the device?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        remote_tmp = "/data/local/tmp/quickadb_edit_tmp"
        local_tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, mode="wb") as f:
                local_tmp_path = f.name
                f.write(content.encode('utf-8'))

            push_res = ADBClient.instance().run(['push', local_tmp_path, remote_tmp], tool="adb", use_serial=True)
            if push_res.returncode != 0:
                err = push_res.stderr or push_res.stdout or f"Exit code {push_res.returncode}"
                QMessageBox.critical(self, "Error", f"Could not push file to device: {err}")
                self._show_status("Error saving file")
                return

            copy_cmd = f"cp {shlex.quote(remote_tmp)} {shlex.quote(self.full_path)} && rm -f {shlex.quote(remote_tmp)}"
            if self.explorer and hasattr(self.explorer, '_root_cmd'):
                shell_cmd = self.explorer._root_cmd(copy_cmd)
            else:
                shell_cmd = copy_cmd

            res = ADBClient.instance().run(['shell', shell_cmd], tool="adb", use_serial=True)
            if res.returncode != 0:
                err = res.stderr or res.stdout or f"Exit code {res.returncode}"
                QMessageBox.critical(self, "Error", f"Could not save file to device: {err}")
                self._show_status("Error saving file")
            else:
                filename = os.path.basename(self.full_path)
                self._show_status(f"File {filename} saved successfully to device")
                if self.explorer and hasattr(self.explorer, 'refresh_file_list'):
                    self.explorer.refresh_file_list()
        finally:
            if local_tmp_path and os.path.exists(local_tmp_path):
                try:
                    os.remove(local_tmp_path)
                except OSError:
                    pass


class ADBFileExplorer(QMainWindow):

    VIEWABLE_TEXT_EXTENSIONS = ("txt", "log", "json", "xml", "html", "csv", "md", "ini", "conf", "prop", "sh", "bat", "py", "js", "css", "cpp", "h", "hpp", "c", "rc", "")
    VIEWABLE_IMAGE_EXTENSIONS = ("png", "jpg", "jpeg", "gif", "bmp")

    def __init__(self):
        super().__init__()
        self.setWindowTitle("QuickADB File Explorer")
        self.setMinimumSize(1000, 600)

        self.platform_tools_path = ToolPaths.instance().platform_tools_dir
        self.adb_path = ToolPaths.instance().adb

        self.current_path = "/storage/emulated/0"
        self.history_stack = []
        self.forward_stack = []
        self.selected_items = []
        self.copy_mode = False
        self.threads = []
        self.symlink_targets = {}
        self._size_worker = None
        self.file_sizes = {}

        self.init_ui()
        ThemeManager.apply_theme(self)
        self.refresh_file_list()

    @property
    def is_root(self):
        """Returns True if the root access checkbox is checked."""
        if hasattr(self, 'root_access_checkbox') and self.root_access_checkbox is not None:
            return self.root_access_checkbox.isChecked()
        return getattr(self, '_root_enabled', False)

    @is_root.setter
    def is_root(self, val: bool):
        if hasattr(self, 'root_access_checkbox') and self.root_access_checkbox is not None:
            self.root_access_checkbox.setChecked(bool(val))
        self._root_enabled = bool(val)

    @property
    def root_enabled(self):
        """Returns True if the root access checkbox is checked."""
        return self.is_root

    @root_enabled.setter
    def root_enabled(self, val: bool):
        self.is_root = val

    # -----------------------------
    # Thread Management
    # -----------------------------
    def _start_thread(self, thread):
        """Starts a QThread and connects it to the cleanup slot."""
        thread.finished.connect(lambda: self._on_thread_finished(thread))
        self.threads.append(thread)
        thread.start()

    def _on_thread_finished(self, thread):
        """Removes a thread from the tracking list once it's finished."""
        try:
            self.threads.remove(thread)
        except ValueError:
            pass

    def _root_cmd(self, cmd: str) -> str:
        """Wrap cmd in su -c '...' when root access is active."""
        if self.root_enabled:
            return f"su -c {shlex.quote(cmd)}"
        return cmd

    def _dpath(self, *parts) -> str:
        """Join path parts and normalize to forward-slash device path."""
        return os.path.join(*parts).replace("\\", "/")

    def _make_progress(self, label: str) -> QProgressDialog:
        """Create a modal indeterminate progress dialog."""
        dlg = QProgressDialog(label, "Cancel", 0, 0, self)
        dlg.setWindowTitle("File Transfer")
        dlg.setModal(True)
        dlg.show()
        return dlg

    # -----------------------------
    #      UI Initialization
    # -----------------------------
    def init_ui(self):
        self.init_menu_bar()
        self.init_toolbar()
        self.init_path_bar()
        self.init_file_view()

        self.statusBar = QStatusBar()
        self.setStatusBar(self.statusBar)
        self.statusBar.showMessage("Ready")

        central_widget = QFrame()
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.addLayout(self.path_layout)
        main_layout.addWidget(self.table)
        self.setCentralWidget(central_widget)

    def init_menu_bar(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu("File")

        refresh_action = QAction("Refresh", self)
        refresh_action.triggered.connect(self.refresh_file_list)
        file_menu.addAction(refresh_action)
        file_menu.addSeparator()

        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        edit_menu = menubar.addMenu("Edit")
        new_folder_action = QAction("New Folder", self)
        new_folder_action.triggered.connect(self.create_new_folder)
        edit_menu.addAction(new_folder_action)

        new_file_action = QAction("New File", self)
        new_file_action.triggered.connect(self.create_new_file)
        edit_menu.addAction(new_file_action)

        edit_menu.addSeparator()

        copy_action = QAction("Copy", self)
        copy_action.triggered.connect(lambda: self.copy_selected_items(True))
        edit_menu.addAction(copy_action)

        move_action = QAction("Cut", self)
        move_action.triggered.connect(lambda: self.copy_selected_items(False))
        edit_menu.addAction(move_action)

        paste_action = QAction("Paste", self)
        paste_action.triggered.connect(self.paste_items)
        edit_menu.addAction(paste_action)

        delete_action = QAction("Delete", self)
        delete_action.triggered.connect(self.delete_selected_items)
        edit_menu.addAction(delete_action)

    def init_toolbar(self):
        toolbar = QToolBar("Main Toolbar")
        toolbar.setIconSize(QSize(24, 24))
        self.addToolBar(toolbar)

        self.back_button = QPushButton("◀ Back")
        self.back_button.clicked.connect(self.go_back)
        self.back_button.setEnabled(False)
        toolbar.addWidget(self.back_button)

        self.forward_button = QPushButton("Forward ▶")
        self.forward_button.clicked.connect(self.go_forward)
        self.forward_button.setEnabled(False)
        toolbar.addWidget(self.forward_button)

        toolbar.addSeparator()

        refresh_button = QPushButton("🔄 Refresh")
        refresh_button.clicked.connect(self.refresh_file_list)
        toolbar.addWidget(refresh_button)

        toolbar.addSeparator()
        new_folder_button = QPushButton("📁 New Folder")
        new_folder_button.clicked.connect(self.create_new_folder)
        toolbar.addWidget(new_folder_button)

        new_file_button = QPushButton("📄 New File")
        new_file_button.clicked.connect(self.create_new_file)
        toolbar.addWidget(new_file_button)

        pull_button = QPushButton("⬇ Pull")
        pull_button.clicked.connect(self.pull_selected_items)
        toolbar.addWidget(pull_button)

        push_button = QPushButton("⬆ Push")
        push_button.clicked.connect(self.push_file)
        toolbar.addWidget(push_button)

        delete_button = QPushButton("🗑️ Delete")
        delete_button.clicked.connect(self.delete_selected_items)
        toolbar.addWidget(delete_button)

        toolbar.addSeparator()
        toolbar.addWidget(QLabel("Sort by:"))

        self.sort_combo = QComboBox()
        self.sort_combo.addItems(["Name", "Type", "Size", "Date"])
        self.sort_combo.currentIndexChanged.connect(self._on_sort_combo_changed)
        toolbar.addWidget(self.sort_combo)

        spacer = QWidget()
        spacer.setObjectName("TBspacer")
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)

        self.root_access_checkbox = QCheckBox("🔓 Root Access")
        self.root_access_checkbox.setToolTip(
            "Use root access to view / modify root directories. Grant root access on your device first."
        )
        toolbar.addWidget(self.root_access_checkbox)

    def init_path_bar(self):
        self.path_layout = QHBoxLayout()
        self.path_layout.setSpacing(8)

        path_label = QLabel("Path:")
        self.path_layout.addWidget(path_label)

        self.path_field = QLineEdit(self.current_path)
        self.path_field.setObjectName("PathBar")
        self.path_field.returnPressed.connect(self.change_path)
        self.path_layout.addWidget(self.path_field)

        self.search_field = QLineEdit()
        self.search_field.setObjectName("SearchBar")
        self.search_field.setPlaceholderText("Search in current directory...")
        self.search_field.textChanged.connect(self.filter_table_by_search)
        self.path_layout.addWidget(self.search_field)

    def init_file_view(self):
        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Name", "Type", "Size", "Modified"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)

        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.open_context_menu)
        self.table.cellDoubleClicked.connect(self.handle_double_click)
        self.table.horizontalHeader().sectionClicked.connect(self.header_clicked)


        self._sort_order = Qt.SortOrder.AscendingOrder

    # -----------------------------
    # Navigation / path operations
    # -----------------------------
    def _navigate_to(self, new_path: str, push_history: bool = True):
        """Central navigation: update path, history and refresh.

        push_history=True (normal navigation): push current path, clear forward stack,
        disable forward button.
        push_history=False (go_back / go_forward): do not touch forward stack or forward
        button - the caller manages those.
        """
        if not new_path or new_path == self.current_path:
            return
        if push_history:
            self.history_stack.append(self.current_path)
            self.forward_stack.clear()
            self.forward_button.setEnabled(False)
        self.current_path = new_path
        self.path_field.setText(self.current_path)
        self.back_button.setEnabled(bool(self.history_stack))
        self.refresh_file_list()

    def change_path(self):
        """Navigates to the path entered in the path field."""
        self._navigate_to(self.path_field.text().strip())

    def go_back(self):
        """Navigates to the previous directory in the history."""
        if not self.history_stack:
            return
        self.forward_stack.append(self.current_path)
        self.forward_button.setEnabled(True)
        prev = self.history_stack.pop()
        self._navigate_to(prev, push_history=False)

    def go_forward(self):
        """Navigates to the next directory in the history."""
        if not self.forward_stack:
            return
        self.history_stack.append(self.current_path)
        self.back_button.setEnabled(True)
        nxt = self.forward_stack.pop()
        self.current_path = nxt
        self.path_field.setText(self.current_path)
        self.forward_button.setEnabled(bool(self.forward_stack))
        self.refresh_file_list()

    def go_to_parent_directory(self):
        """Navigates up to the parent directory."""
        self._navigate_to(os.path.dirname(self.current_path))

    # -----------------------------
    # Directory listing
    # -----------------------------
    def refresh_file_list(self):
        """Fetches and displays the file list for the current path."""
        self.search_field.clear()
        self.statusBar.showMessage("Loading directory contents...")
        self.table.setRowCount(0)
        self.symlink_targets.clear()

        if self.root_enabled:
            self.statusBar.showMessage("Loading directory contents with root access...")
        cmd = ['shell', self._root_cmd(f'ls -la {shlex.quote(self.current_path)} 2>&1')]

        ls_thread = ADBThread(self.adb_path, cmd)
        ls_thread.command_finished.connect(self.process_directory_listing)
        self._start_thread(ls_thread)

    def process_directory_listing(self, output, error):
        """Parses 'ls -la' output and populates the file table."""
        if error and not output.strip():
            QMessageBox.critical(self, "Error", output or "Unknown error listing directory")
            self.statusBar.showMessage("Error loading directory")
            return

        permission_issue = False
        if error and output.strip():
            self.statusBar.showMessage("Directory loaded with some warnings")

        self.table.setRowCount(0)
        rows = []

        for line in output.splitlines():
            if not line.strip() or line.startswith("total"):
                continue
            if "Permission denied" in line or line.startswith("ls:") or line.startswith("cannot access"):
                permission_issue = True
                continue

            entry = self._parse_ls_entry(line)
            if not entry:
                continue

            perms = entry["perms"]
            size = entry["size"]
            date_str = entry["modified"]
            name_part = entry["name"]

            name = ""
            target_path = ""
            is_symlink = "->" in name_part

            if is_symlink:
                name_target = name_part.split("->", 1)
                name = name_target[0].strip()
                if len(name_target) > 1:
                    target_path = name_target[1].strip()
            else:
                name = name_part

            if not name or name in (".", ".."):
                continue

            is_folder = perms.startswith('d') or perms.startswith('l')
            file_type = "Folder" if is_folder else self.detect_type(name)
            size_str = "-" if is_folder else self.format_size_safe(size)
            symlink_info = target_path if is_symlink else None
            rows.append((name, file_type, size_str, date_str, self.safe_int(size), is_folder, symlink_info))

        rows.sort(key=lambda x: (not x[5], x[0].lower()))

        self.table.setSortingEnabled(False)
        self.file_sizes.clear()

        for name, file_type, size_str, date_str, raw_size, is_folder, symlink_info in rows:
            row = self.table.rowCount()
            self.table.insertRow(row)
            item = QTableWidgetItem(name)
            item.size = raw_size
            self.file_sizes[name] = raw_size
            if symlink_info:
                self.symlink_targets[name] = symlink_info
                item.setToolTip(f"Symlink to: {symlink_info}")
            self.table.setItem(row, 0, item)
            self.table.setItem(row, 1, QTableWidgetItem(file_type))
            self.table.setItem(row, 2, QTableWidgetItem(size_str))
            self.table.setItem(row, 3, QTableWidgetItem(date_str))

        self.table.setSortingEnabled(True)

        self.sort_table()

        folder_count = sum(1 for r in rows if r[5])
        file_count = len(rows) - folder_count
        status_msg = f"Directory: {self.current_path} | {folder_count} folder(s), {file_count} file(s)"
        if permission_issue:
            status_msg += " (some items may be inaccessible)"
        self.statusBar.showMessage(status_msg)

    # -----------------------------
    # Helpers: parsing and formatting
    # -----------------------------
    def detect_type(self, name):
        return name.split(".")[-1].upper() if "." in name else "File"

    def _parse_ls_entry(self, line):
        """Parse one 'ls -la' row.

        Regular files use:
            perms links owner group size date time... name
        Device nodes under paths like /dev use:
            perms links owner group major, minor date time... name
        """
        import re
        text = (line or "").strip()
        if not text:
            return None

        all_parts = text.split()
        if len(all_parts) < 7:
            return None

        is_device_node = text.startswith("b") or text.startswith("c")
        perms = all_parts[0]
        owner = all_parts[2]
        group = all_parts[3]

        if is_device_node:
            if all_parts[4].endswith(','):
                size = ""
                date_idx = 6
            else:
                size = ""
                date_idx = 5
        else:
            size = all_parts[4]
            date_idx = 5

        if len(all_parts) <= date_idx:
            return None

        # Check ISO mode
        if re.match(r"^\d{4}-\d{2}-\d{2}$", all_parts[date_idx]):
            if len(all_parts) <= date_idx + 1:
                return None
            modified = f"{all_parts[date_idx]} {all_parts[date_idx+1]}"
            name_idx = date_idx + 2
        else:
            if len(all_parts) <= date_idx + 2:
                return None
            modified = f"{all_parts[date_idx]} {all_parts[date_idx+1]} {all_parts[date_idx+2]}"
            name_idx = date_idx + 3

        parts = text.split(maxsplit=name_idx)
        name = parts[name_idx] if len(parts) > name_idx else ""

        return {
            "perms": perms,
            "owner": owner,
            "group": group,
            "size": size,
            "modified": modified,
            "name": name,
        }

    def safe_int(self, s):
        try:
            return int(s.replace(',', ''))
        except (ValueError, AttributeError):
            return 0

    def format_size_safe(self, size_bytes_str):
        try:
            size = int(size_bytes_str)
            for unit in ['B', 'KB', 'MB', 'GB']:
                if size < 1024:
                    return f"{size:.1f} {unit}"
                size /= 1024
            return f"{size:.1f} TB"
        except (ValueError, TypeError):
            return "-"

    # -----------------------------
    # Interactions: double-click / context menu
    # -----------------------------
    def handle_double_click(self, row, column):
        name_item = self.table.item(row, 0)
        type_item = self.table.item(row, 1)
        if not name_item or not type_item:
            return

        selected_name = name_item.text()
        file_type = type_item.text().strip().upper()

        if selected_name == "..":
            self.go_to_parent_directory()
            return

        if file_type == "FOLDER":

            symlink_target = self.symlink_targets.get(selected_name)
            if symlink_target:
                new_path = symlink_target if symlink_target.startswith('/') else \
                    self._dpath(os.path.normpath(os.path.join(self.current_path, symlink_target)))
            else:
                new_path = self._dpath(self.current_path, selected_name)

            new_path = new_path.replace("//", "/")
            if selected_name == "sdcard":
                new_path = "/storage/emulated/0"

            self._navigate_to(new_path)
        else:
            self.view_or_pull_file(selected_name, name_item)

    def view_or_pull_file(self, filename, item=None):
        menu = QMenu()
        pull_action = menu.addAction("Pull File")
        file_ext = filename.split(".")[-1].lower() if "." in filename else ""
        viewable = file_ext in self.VIEWABLE_TEXT_EXTENSIONS
        image_ext = file_ext in self.VIEWABLE_IMAGE_EXTENSIONS
        view_action = menu.addAction("View Contents") if viewable or image_ext else None

        action = menu.exec(self.cursor().pos())
        if not action:
            return
        if action == pull_action:
            self.pull_file(filename)
        elif view_action and action == view_action:
            if image_ext:
                self.view_file_contents(filename, is_image=True)
            else:
                self.open_file(item if item is not None else filename)

    def open_file(self, item):
        """Open a file in the inline editor, checking size first."""
        size = getattr(item, 'size', 0)
        try:
            size = int(size) if size is not None else 0
        except (ValueError, TypeError):
            size = 0
        filename = item.text() if hasattr(item, 'text') else getattr(item, 'name', getattr(item, 'filename', str(item)))
        if size == 0 and hasattr(self, 'file_sizes'):
            size = self.file_sizes.get(filename, 0)
        if size > 5 * 1024 * 1024:
            QMessageBox.warning(
                self,
                "File Too Large",
                f"File '{filename}' is larger than 5 MB ({self.format_size_safe(size)}).\n"
                "Files over 5 MB cannot be opened in the inline text editor and should be pulled instead."
            )
            return
        self.view_file_contents(filename, is_image=False)

    # -----------------------------
    # Viewing files & images
    # -----------------------------
    def view_file_contents(self, filename, is_image=False):
        full_path = self._dpath(self.current_path, filename)
        if is_image:
            temp_dir = os.path.join(tempfile.gettempdir(), "adbexplorer")
            os.makedirs(temp_dir, exist_ok=True)
            temp_path = os.path.join(temp_dir, filename)
            self.statusBar.showMessage(f"Pulling image {filename} for viewing...")
            if self.root_enabled:
                device_temp = f"/data/local/tmp/{filename}"
                dd_cmd = self._root_cmd(f'dd if={shlex.quote(full_path)} of={shlex.quote(device_temp)} && chmod 644 {shlex.quote(device_temp)}')

                dd_thread = ADBThread(self.adb_path, ['shell', dd_cmd])
                dd_thread.command_finished.connect(
                    lambda out, err: self.pull_temp_image(out, err, filename, device_temp, temp_path)
                )
                self._start_thread(dd_thread)
            else:
                pull = TransferRunner(self.adb_path, ['pull', full_path, temp_path])
                pull.transfer_finished.connect(lambda out, err, dest: self.display_image(out, err, filename, dest))
                self._start_thread(pull)
        else:
            file_size = self.file_sizes.get(filename, 0) if hasattr(self, 'file_sizes') else 0
            if file_size > 5 * 1024 * 1024:
                QMessageBox.warning(
                    self,
                    "File Too Large",
                    f"File '{filename}' is larger than 5 MB ({self.format_size_safe(file_size)}).\n"
                    "Files over 5 MB cannot be opened in the inline text editor and should be pulled instead."
                )
                return
            self.statusBar.showMessage(f"Loading contents of {filename}...")
            shell_cmd = self._root_cmd(f'cat {shlex.quote(full_path)}')

            t = ADBThread(self.adb_path, ['shell', shell_cmd])
            t.command_finished.connect(lambda out, err: self.display_file_contents(filename, out, err))
            self._start_thread(t)

    def pull_temp_image(self, output, error, filename, device_temp, temp_path):
        if error:
            QMessageBox.critical(self, "Error", f"Could not copy image using root: {output}")
            self.statusBar.showMessage("Error copying image with root")
            return
        pull = TransferRunner(self.adb_path, ['pull', device_temp, temp_path])
        pull.transfer_finished.connect(lambda out, err, dest: self.finish_image_pull(out, err, filename, device_temp, dest))
        self._start_thread(pull)

    def finish_image_pull(self, output, error, filename, device_temp, temp_path):
        cleanup = ADBThread(self.adb_path, ['shell', self._root_cmd(f'rm -f {shlex.quote(device_temp)}')])
        self._start_thread(cleanup)
        if error:
            QMessageBox.critical(self, "Error", f"Could not pull temporary image: {output}")
            self.statusBar.showMessage("Error pulling image")
            return
        self.display_image("", False, filename, temp_path)

    def display_image(self, output, error, filename, temp_path):
        if error:
            QMessageBox.critical(self, "Error", f"Could not pull image: {output}")
            self.statusBar.showMessage("Error loading image")
            return
        try:
            dialog = QDialog(self)
            dialog.setWindowTitle(f"Image: {filename}")
            dialog.setMinimumSize(800, 600)
            layout = QVBoxLayout(dialog)
            image_label = QLabel()
            pixmap = QPixmap(temp_path)
            original_w, original_h = pixmap.width(), pixmap.height()
            if pixmap.width() > 780 or pixmap.height() > 580:
                pixmap = pixmap.scaled(780, 580, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            image_label.setPixmap(pixmap)
            image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(image_label)
            info_label = QLabel(f"Size: {original_w}x{original_h} | File: {filename}")
            info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(info_label)
            close_button = QPushButton("Close")
            close_button.clicked.connect(dialog.accept)
            layout.addWidget(close_button, alignment=Qt.AlignmentFlag.AlignCenter)
            self.statusBar.showMessage(f"Displaying image: {filename}")
            dialog.exec()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not display image: {str(e)}")
            self.statusBar.showMessage("Error displaying image")
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass

    def display_file_contents(self, filename, content, error):
        if error:
            QMessageBox.critical(self, "Error", f"Could not read file: {content}")
            self.statusBar.showMessage("Error reading file")
            return
        full_path = self._dpath(self.current_path, filename)
        dialog = TextEditorDialog(self, filename, content, full_path)
        self.statusBar.showMessage(f"Displaying contents of {filename}")
        dialog.exec()

    def save_file_content(self, filename, content):
        save_path, _ = QFileDialog.getSaveFileName(self, "Save File As", filename, "All Files (*)")
        if not save_path:
            return
        try:
            with open(save_path, 'w', encoding='utf-8') as f:
                f.write(content)
            self.statusBar.showMessage(f"File saved as {save_path}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not save file: {str(e)}")

    def save_to_device(self, filename, content):
        full_path = self._dpath(self.current_path, filename)
        dlg = TextEditorDialog(self, filename, content, full_path)
        dlg.save_to_device()

    def save_complete(self, output, error, filename):
        if error:
            QMessageBox.critical(self, "Error", f"Could not save file to device: {output}")
            self.statusBar.showMessage("Error saving file")
        else:
            self.statusBar.showMessage(f"File {filename} saved successfully to device")
            self.refresh_file_list()

    # -----------------------------
    # Pull / Push (threaded)
    # -----------------------------
    def pull_file(self, filename, save_path=None):
        full_path = self._dpath(self.current_path, filename)
        if save_path is None:
            save_path, _ = QFileDialog.getSaveFileName(self, "Save File As", filename, "All Files (*)")
            if not save_path:
                return
        self.statusBar.showMessage(f"Pulling file {filename}...")

        transfer = TransferRunner(self.adb_path, ['pull', full_path, save_path], root_enabled=self.root_enabled)
        transfer.transfer_finished.connect(
            lambda out, err, dest: self.handle_single_pull_result(out, err, filename, full_path, save_path)
        )
        transfer.transfer_progress.connect(self.handle_transfer_progress)
        self._start_thread(transfer)

    def handle_single_pull_result(self, output, error, filename, full_path, save_path):
        if error and self.root_enabled:
            clean_src = full_path.replace('\\', '/').rstrip('/')
            base = os.path.basename(clean_src) or filename or 'item'
            tmp_stage = f"/data/local/tmp/quickadb_pull_{int(time.time()*1000)}_{base}"
            stage_cmd = f'su -c "cp -r {shlex.quote(full_path)} {shlex.quote(tmp_stage)} && chmod -R 755 {shlex.quote(tmp_stage)}"'
            cp_thread = ADBThread(self.adb_path, ['shell', stage_cmd])

            def on_staged(out, err):
                if err:
                    QMessageBox.critical(self, "Error", f"Could not copy file using root: {out}")
                    self.statusBar.showMessage("Error copying file with root")
                    clean_cmd = f'su -c "rm -rf {shlex.quote(tmp_stage)}"'
                    cleanup = ADBThread(self.adb_path, ['shell', clean_cmd])
                    self._start_thread(cleanup)
                    return
                t = TransferRunner(self.adb_path, ['pull', tmp_stage, save_path])

                def on_done(last_line, is_err, dest):
                    clean_cmd = f'su -c "rm -rf {shlex.quote(tmp_stage)}"'
                    cleanup = ADBThread(self.adb_path, ['shell', clean_cmd])
                    self._start_thread(cleanup)
                    self.pull_complete(last_line, is_err, save_path)

                t.transfer_finished.connect(on_done)
                t.transfer_progress.connect(self.handle_transfer_progress)
                self._start_thread(t)

            cp_thread.command_finished.connect(on_staged)
            self._start_thread(cp_thread)
        else:
            self.pull_complete(output, error, save_path)

    def pull_temp_file(self, output, error, filename, device_temp, save_path):
        if error:
            QMessageBox.critical(self, "Error", f"Could not copy file using root: {output}")
            self.statusBar.showMessage("Error copying file with root")
            return
        transfer = TransferRunner(self.adb_path, ['pull', device_temp, save_path])
        transfer.transfer_finished.connect(lambda out, err, dest: self.finish_root_pull(out, err, filename, device_temp, dest))
        transfer.transfer_progress.connect(self.handle_transfer_progress)
        self._start_thread(transfer)

    def finish_root_pull(self, output, error, filename, device_temp, save_path):
        cleanup = ADBThread(self.adb_path, ['shell', self._root_cmd(f'rm -f {shlex.quote(device_temp)}')])
        self._start_thread(cleanup)
        if error:
            QMessageBox.critical(self, "Error", f"Could not pull temporary file: {output}")
            self.statusBar.showMessage("Error pulling file")
            return
        self.pull_complete(output, error, save_path)

    def pull_complete(self, output, error, save_path):
        if error:
            QMessageBox.critical(self, "Error", f"Failed to pull file: {output}")
            self.statusBar.showMessage("Error pulling file")
        else:
            filename = os.path.basename(save_path)
            self.statusBar.showMessage(f"Successfully pulled {filename} to {save_path}")

    def start_pull(self, source_path, dest_dir, name, is_directory):
        dest_path = os.path.join(dest_dir, name)
        progress = self._make_progress(f"Pulling {name}...")
        android_path = source_path.replace('\\', '/')
        transfer = TransferRunner(self.adb_path, ['pull', android_path, dest_path], root_enabled=self.root_enabled)
        transfer.transfer_finished.connect(
            lambda out, err, dest: self.handle_pull_result(out, err, name, progress, android_path, dest_path, is_directory)
        )
        transfer.transfer_progress.connect(self.handle_transfer_progress)
        self._start_thread(transfer)

    def handle_pull_result(self, output, error, name, progress, source_path, dest_path, is_directory=False):
        if error and self.root_enabled:
            self.statusBar.showMessage(f"Direct pull failed for {name}, trying root staging fallback...")
            progress.setLabelText(f"Direct pull failed, staging via /data/local/tmp for {name}...")
            self.stage_and_pull(source_path, dest_path, name, progress)
        elif error:
            progress.close()
            QMessageBox.critical(self, "Error", f"Failed to pull {name}: {output}")
            self.statusBar.showMessage(f"Failed to pull {name}")
        else:
            progress.close()
            self.statusBar.showMessage(f"Successfully pulled {name}")

    def stage_and_pull(self, source_path, dest_path, name, progress):
        clean_src = source_path.replace('\\', '/').rstrip('/')
        base = os.path.basename(clean_src) or name or 'item'
        tmp_stage = f"/data/local/tmp/quickadb_pull_{int(time.time()*1000)}_{base}"
        stage_cmd = f'su -c "cp -r {shlex.quote(source_path)} {shlex.quote(tmp_stage)} && chmod -R 755 {shlex.quote(tmp_stage)}"'
        cp_thread = ADBThread(self.adb_path, ['shell', stage_cmd])

        def on_staged(out, err):
            if err:
                progress.close()
                QMessageBox.critical(self, "Error", f"Failed to stage {name} using root: {out}")
                self.statusBar.showMessage(f"Failed to stage {name}")
                clean_cmd = f'su -c "rm -rf {shlex.quote(tmp_stage)}"'
                cleanup_thread = ADBThread(self.adb_path, ['shell', clean_cmd])
                self._start_thread(cleanup_thread)
                return
            progress.setLabelText(f"Pulling {name} from staged copy...")
            transfer = TransferRunner(self.adb_path, ['pull', tmp_stage, dest_path])

            def on_staged_pull_done(last_line, is_error, _):
                clean_cmd = f'su -c "rm -rf {shlex.quote(tmp_stage)}"'
                cleanup_thread = ADBThread(self.adb_path, ['shell', clean_cmd])
                self._start_thread(cleanup_thread)
                progress.close()
                if is_error:
                    QMessageBox.critical(self, "Error", f"Failed to pull {name} from staging: {last_line}")
                    self.statusBar.showMessage(f"Failed to pull {name}")
                else:
                    self.statusBar.showMessage(f"Successfully pulled {name} via root staging")

            transfer.transfer_finished.connect(on_staged_pull_done)
            transfer.transfer_progress.connect(self.handle_transfer_progress)
            self._start_thread(transfer)

        cp_thread.command_finished.connect(on_staged)
        self._start_thread(cp_thread)

    def pull_Wait(self, source_path, target_path):
        """Safely pull a file or directory without altering original permissions."""
        res = ADBClient.instance().run(['pull', source_path, target_path], tool="adb", use_serial=True)
        returncode = getattr(res, 'returncode', getattr(res, 'return_code', 0))
        if returncode != 0 and self.root_enabled:
            clean_src = source_path.replace('\\', '/').rstrip('/')
            base = os.path.basename(clean_src) or 'item'
            tmp_stage = f"/data/local/tmp/quickadb_pull_{int(time.time()*1000)}_{base}"
            try:
                stage_cmd = f'su -c "cp -r {shlex.quote(source_path)} {shlex.quote(tmp_stage)} && chmod -R 755 {shlex.quote(tmp_stage)}"'
                ADBClient.instance().run(['shell', stage_cmd], tool="adb", use_serial=True)
                res = ADBClient.instance().run(['pull', tmp_stage, target_path], tool="adb", use_serial=True)
            finally:
                clean_cmd = f'su -c "rm -rf {shlex.quote(tmp_stage)}"'
                ADBClient.instance().run(['shell', clean_cmd], tool="adb", use_serial=True)
        return res

    def pull_selected_items(self):
        selected_rows = set(index.row() for index in self.table.selectedIndexes())
        if not selected_rows:
            return
        items_to_pull = []
        for row in selected_rows:
            name_item = self.table.item(row, 0)
            type_item = self.table.item(row, 1)
            if name_item and name_item.text() != "..":
                is_directory = bool(type_item and type_item.text() == "Folder")
                items_to_pull.append((name_item.text(), is_directory))
        if not items_to_pull:
            return
        dest_dir = QFileDialog.getExistingDirectory(self, "Select Destination Folder")
        if not dest_dir:
            return
        for name, is_directory in items_to_pull:
            source_path = self._dpath(self.current_path, name)
            self.start_pull(source_path, dest_dir, name, is_directory)

    def push_file(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Select Files to Push")
        if not files:
            return
        for file_path in files:
            dest_path = self._dpath(self.current_path, os.path.basename(file_path))
            progress = self._make_progress(f"Pushing {os.path.basename(file_path)}...")
            transfer = TransferRunner(self.adb_path, ['push', file_path, dest_path])
            transfer.transfer_finished.connect(lambda out, err, dest, p=progress: self.handle_push_result(out, err, file_path, p))
            transfer.transfer_progress.connect(self.handle_transfer_progress)
            self._start_thread(transfer)

    def handle_push_result(self, output, error, file_path, progress):
        progress.close()
        if error:
            QMessageBox.critical(self, "Error", f"Failed to push {os.path.basename(file_path)}: {output}")
        else:
            self.statusBar.showMessage(f"Successfully pushed {os.path.basename(file_path)}")
            self.refresh_file_list()

    def handle_transfer_progress(self, line):
        self.statusBar.showMessage(line)

    # -----------------------------
    # Copy / Move / Paste
    # -----------------------------
    def copy_selected_items(self, copy_mode=True):
        selected_rows = set(index.row() for index in self.table.selectedIndexes())
        if not selected_rows:
            return
        self.selected_items = []
        for row in selected_rows:
            name_item = self.table.item(row, 0)
            if name_item and name_item.text() != "..":
                self.selected_items.append(self._dpath(self.current_path, name_item.text()))
        self.copy_mode = copy_mode
        self.statusBar.showMessage(f"{'Copied' if copy_mode else 'Cut'} {len(self.selected_items)} item(s)")

    def paste_items(self):
        if not self.selected_items:
            return
        for source_path in self.selected_items:
            filename = os.path.basename(source_path)
            dest_path = self._dpath(self.current_path, filename)
            if source_path == dest_path:
                continue
            if self.copy_mode:
                check_cmd = self._root_cmd(f'[ -d {shlex.quote(source_path)} ] && echo dir || echo file')
                check_thread = ADBThread(self.adb_path, ['shell', check_cmd])
                check_thread.command_finished.connect(lambda out, err, s=source_path, d=dest_path: self.perform_copy(s, d, "dir" in out.strip()))
                self._start_thread(check_thread)
            else:
                move_cmd = self._root_cmd(f'mv {shlex.quote(source_path)} {shlex.quote(dest_path)}')
                move_thread = ADBThread(self.adb_path, ['shell', move_cmd])
                move_thread.command_finished.connect(lambda out, err: self.handle_paste_result(out, err))
                self._start_thread(move_thread)
        self.selected_items = []

    def perform_copy(self, source_path, dest_path, is_directory):
        copy_cmd = self._root_cmd(
            f'cp -R {shlex.quote(source_path)} {shlex.quote(dest_path)}'
            if is_directory else
            f'cp {shlex.quote(source_path)} {shlex.quote(dest_path)}'
        )

        copy_thread = ADBThread(self.adb_path, ['shell', copy_cmd])
        copy_thread.command_finished.connect(lambda out, err: self.handle_paste_result(out, err))
        self._start_thread(copy_thread)

    def handle_paste_result(self, output, error):
        if error:
            QMessageBox.critical(self, "Error", f"Failed to paste: {output}")
        else:
            self.statusBar.showMessage("Paste operation completed")
            self.refresh_file_list()

    # -----------------------------
    # File/directory operations
    # -----------------------------
    def create_new_folder(self):
        folder_name, ok = QInputDialog.getText(self, "New Folder", "Enter folder name:")
        if not ok or not folder_name:
            return
        if '/' in folder_name or '\\' in folder_name:
            QMessageBox.critical(self, "Error", "Folder name cannot contain / or \\")
            return
        full_path = self._dpath(self.current_path, folder_name)
        mkdir_cmd = self._root_cmd(f'mkdir -p {shlex.quote(full_path)}')

        mkdir_thread = ADBThread(self.adb_path, ['shell', mkdir_cmd])
        mkdir_thread.command_finished.connect(lambda out, err: self.handle_mkdir_result(out, err, folder_name))
        self._start_thread(mkdir_thread)

    def handle_mkdir_result(self, output, error, folder_name):
        if error:
            QMessageBox.critical(self, "Error", f"Failed to create folder {folder_name}: {output}")
        else:
            self.statusBar.showMessage(f"Created folder {folder_name}")
            self.refresh_file_list()


    def create_new_file(self):
        file_name, ok = QInputDialog.getText(self, "New File", "Enter file name:")
        if not ok or not file_name:
            return
        if '/' in file_name or '\\' in file_name:
            QMessageBox.critical(self, "Error", "File name cannot contain / or \\")
            return
        full_path = self._dpath(self.current_path, file_name)
        touch_cmd = self._root_cmd(f'touch {shlex.quote(full_path)}')

        touch_thread = ADBThread(self.adb_path, ['shell', touch_cmd])
        touch_thread.command_finished.connect(lambda out, err: self.handle_touch_result(out, err, file_name))
        self._start_thread(touch_thread)

    def handle_touch_result(self, output, error, file_name):
        if error:
            QMessageBox.critical(self, "Error", f"Failed to create file {file_name}: {output}")
        else:
            self.statusBar.showMessage(f"Created file {file_name}")
            self.refresh_file_list()


    def install_apk(self, name):
        if not name.endswith('.apk'):
            return
        full_path = self._dpath(self.current_path, name)
        install_thread = RemoteAPKInstallThread(full_path, self.root_enabled)
        install_thread.install_finished.connect(
            lambda result: self._handle_install_apk_result(result, name)
        )
        install_thread.install_failed.connect(
            lambda error: self._handle_install_apk_worker_error(error, name)
        )
        self.statusBar.showMessage(f"Installing APK {name}...")
        self._start_thread(install_thread)

    def execute_shell_script(self, name):
        if not name.endswith('.sh'):
            return
        full_path = self._dpath(self.current_path, name)
        if self.root_enabled:
            chmod_cmd = self._root_cmd(f'chmod +x {shlex.quote(full_path)}')
            chmod_thread = ADBThread(self.adb_path, ['shell', chmod_cmd])
            chmod_thread.command_finished.connect(
                lambda out, err: self._handle_root_script_prepare(out, err, full_path, name)
            )
            self._start_thread(chmod_thread)
        else:
            self._run_live_command(
                ['shell', f'sh {shlex.quote(full_path)}'],
                title=f"Executing: {name}",
                header_html=f"Output of <b>{name}</b>",
                success_status=f"Script {name} executed successfully.",
                error_status=f"Script {name} finished with errors."
            )

    def _handle_root_script_prepare(self, output, error, full_path, name):
        if error:
            QMessageBox.critical(self, "Error", f"Failed to prepare script {name}: {output}")
            self.statusBar.showMessage(f"Script {name} could not be prepared.")
            return

        self._run_live_command(
            ['shell', self._root_cmd(f'sh {shlex.quote(full_path)}')],
            title=f"Executing: {name}",
            header_html=f"Output of <b>{name}</b>",
            success_status=f"Script {name} executed successfully.",
            error_status=f"Script {name} finished with errors."
        )

    def _handle_install_apk_result(self, result, name):
        if result.returncode != 0:
            output = (result.stderr or result.stdout or "").strip()
            QMessageBox.critical(self, "Install APK", f"Failed to install {name}:\n\n{output}")
            self.statusBar.showMessage(f"APK {name} failed to install.")
            return

        self.statusBar.showMessage(f"APK {name} installed successfully.")
        self.refresh_file_list()

    def _handle_install_apk_worker_error(self, error_message, name):
        QMessageBox.critical(self, "Install APK", f"Failed to install {name}:\n\n{error_message}")
        self.statusBar.showMessage(f"APK {name} failed to install.")

    def _run_live_command(self, cmd_args: list, title: str, header_html: str, success_status: str, error_status: str):
        """Open a live output dialog and stream command output into it."""
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setMinimumSize(700, 450)
        dialog.setModal(False)

        layout = QVBoxLayout(dialog)

        header = QLabel(header_html)
        header.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(header)

        output_view = QPlainTextEdit()
        output_view.setReadOnly(True)
        output_view.setFont(QFont("Courier New", 9))
        layout.addWidget(output_view)

        close_btn = QPushButton("Close")
        close_btn.setEnabled(False)
        close_btn.clicked.connect(dialog.accept)
        layout.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignRight)

        dialog.show()

        runner = TransferRunner(self.adb_path, cmd_args)

        def _on_line(line: str):
            output_view.appendPlainText(line)
            output_view.ensureCursorVisible()

        def _on_done(last_line: str, is_error: bool, _dest: str):
            if is_error:
                output_view.appendPlainText("\n[X] Command exited with an error.")
                self.statusBar.showMessage(error_status)
            else:
                output_view.appendPlainText("\n[OK] Done.")
                self.statusBar.showMessage(success_status)
            close_btn.setEnabled(True)
            self.refresh_file_list()

        runner.transfer_progress.connect(_on_line)
        runner.transfer_finished.connect(_on_done)
        self._start_thread(runner)

    def _run_script(self, cmd_args: list, name: str):
        self._run_live_command(
            cmd_args,
            title=f"Executing: {name}",
            header_html=f"Output of <b>{name}</b>",
            success_status=f"Script {name} executed successfully.",
            error_status=f"Script {name} finished with errors."
        )
        return

    # -----------------------------
    # Context menu and selections
    # -----------------------------
    def open_context_menu(self, position):
        indexes = self.table.selectedIndexes()
        if not indexes:
            return
        row = indexes[0].row()
        name_item = self.table.item(row, 0)
        type_item = self.table.item(row, 1)
        if not name_item or name_item.text() == "..":
            return

        selected_name = name_item.text()
        is_folder = bool(type_item and type_item.text().upper() == "FOLDER")

        menu = QMenu()
        if is_folder:
            menu.addAction("Open", lambda: self.handle_double_click(row, 0))
        else:
            menu.addAction("View", lambda: self.open_file(name_item or selected_name))

        menu.addSeparator()
        menu.addAction("Pull to PC", lambda: self.pull_file(selected_name))
        if not is_folder:
            menu.addAction("Push to Device", self.push_file)

        if not is_folder and selected_name.endswith('.sh'):
            menu.addAction("Execute Script", lambda: self.execute_shell_script(selected_name))

        if not is_folder and selected_name.endswith('.apk'):
            menu.addAction("Install APK", lambda: self.install_apk(selected_name))

        menu.addAction("Permissions (chmod)", lambda: self.show_chmod_dialog(selected_name, is_folder))
        menu.addSeparator()
        menu.addAction("Rename", lambda: self.rename_item(selected_name))
        menu.addAction("Delete", lambda: self.delete_item(selected_name))
        menu.addSeparator()
        menu.addAction("Copy", lambda: self.copy_selected_items(True))
        menu.addAction("Cut", lambda: self.copy_selected_items(False))
        if self.selected_items:
            menu.addAction("Paste Here", self.paste_items)
        menu.addSeparator()
        menu.addAction("Properties", lambda: self.show_properties(selected_name, is_folder))

        menu.exec(self.table.mapToGlobal(position))

    def rename_item(self, name):
        new_name, ok = QInputDialog.getText(self, "Rename", "New name:", text=name)
        if not ok or not new_name or new_name == name:
            return
        old_path = self._dpath(self.current_path, name)
        new_path = self._dpath(self.current_path, new_name)
        mv_cmd = self._root_cmd(f'mv {shlex.quote(old_path)} {shlex.quote(new_path)}')

        rename_thread = ADBThread(self.adb_path, ['shell', mv_cmd])
        rename_thread.command_finished.connect(lambda out, err: self.handle_rename_result(out, err, name, new_name))
        self._start_thread(rename_thread)

    def handle_rename_result(self, output, error, old_name, new_name):
        if error:
            QMessageBox.critical(self, "Error", f"Failed to rename {old_name}: {output}")
        else:
            self.statusBar.showMessage(f"Renamed {old_name} to {new_name}")
            self.refresh_file_list()

    def delete_item(self, name, confirm=True):
        if confirm:
            reply = QMessageBox.question(
                self, "Confirm Delete", f"Are you sure you want to delete '{name}'?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        path = self._dpath(self.current_path, name)
        check_cmd = self._root_cmd(f'[ -d {shlex.quote(path)} ] && echo dir || echo file')

        is_dir_thread = ADBThread(self.adb_path, ['shell', check_cmd])
        is_dir_thread.command_finished.connect(lambda out, err: self.perform_delete(path, name, "dir" in out.strip()))
        self._start_thread(is_dir_thread)

    def perform_delete(self, path, name, is_directory):
        flag = "-rf" if is_directory else "-f"
        delete_cmd = self._root_cmd(f'rm {flag} {shlex.quote(path)}')

        delete_thread = ADBThread(self.adb_path, ['shell', delete_cmd])
        delete_thread.command_finished.connect(lambda out, err: self.handle_delete_result(out, err, name))
        self._start_thread(delete_thread)

    def handle_delete_result(self, output, error, name):
        if error:
            QMessageBox.critical(self, "Error", f"Failed to delete {name}: {output}")
        else:
            self.statusBar.showMessage(f"Deleted {name}")
            self.refresh_file_list()

    def delete_selected_items(self):
        selected_rows = set(index.row() for index in self.table.selectedIndexes())
        if not selected_rows:
            return
        items_to_delete = [
            self.table.item(row, 0).text() for row in selected_rows
            if self.table.item(row, 0) and self.table.item(row, 0).text() != ".."
        ]
        if not items_to_delete:
            return

        confirm = QMessageBox.question(
            self, "Confirm Delete", f"Delete {len(items_to_delete)} selected item(s)?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        for name in items_to_delete:
            self.delete_item(name, confirm=False)

    # -----------------------------
    # Sorting, filtering, properties
    # -----------------------------
    def filter_table_by_search(self, search_text):
        search_text = search_text.strip().lower()
        for row in range(self.table.rowCount()):
            name_item = self.table.item(row, 0)
            if name_item:
                is_hidden = search_text not in name_item.text().lower()
                self.table.setRowHidden(row, is_hidden)

    def sort_table(self):

        sort_column = self.sort_combo.currentIndex()

        # Remove '..' before sorting so it is not displaced by sortByColumn().
        dotdot_row = None
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item and item.text() == "..":
                dotdot_row = r
                break
        if dotdot_row is not None:
            self.table.removeRow(dotdot_row)

        self.table.sortByColumn(sort_column, self._sort_order)

        # Re-insert '..' at the top for any directory that has a parent.
        if self.current_path != "/" and not self.current_path.endswith(":/"):
            self.table.insertRow(0)
            self.table.setItem(0, 0, QTableWidgetItem(".."))
            self.table.setItem(0, 1, QTableWidgetItem("Folder"))
            self.table.setItem(0, 2, QTableWidgetItem("-"))
            self.table.setItem(0, 3, QTableWidgetItem("-"))

    def _on_sort_combo_changed(self):
        """Slot for sort combo changes. Always resets to ascending for the new column."""
        self._sort_order = Qt.SortOrder.AscendingOrder
        self.sort_table()

    def header_clicked(self, column):

        current_column = self.sort_combo.currentIndex()
        if column == current_column:
            # Same column — toggle between ascending and descending.
            self._sort_order = (
                Qt.SortOrder.DescendingOrder
                if self._sort_order == Qt.SortOrder.AscendingOrder
                else Qt.SortOrder.AscendingOrder
            )
            self.sort_table()
        else:
            # Different column — reset order and update combo.
            # The combo's currentIndexChanged signal triggers _on_sort_combo_changed,
            # which resets _sort_order and calls sort_table() — no double-sort.
            self._sort_order = Qt.SortOrder.AscendingOrder
            self.sort_combo.setCurrentIndex(column)

    def _CalcFolderSize(self, full_path, name=None):
        if hasattr(self, '_size_worker') and self._size_worker and self._size_worker.isRunning():
            return
        cmd = self._root_cmd(f'du -sh {shlex.quote(full_path)}')
        self._size_worker = ADBThread(self.adb_path, ['shell', cmd])
        if name is not None:
            self._size_worker.command_finished.connect(
                lambda out, err: self.show_folder_properties(name, full_path, out, err)
            )
        self._start_thread(self._size_worker)

    _calc_folder_size = _CalcFolderSize

    def show_properties(self, name, is_folder):
        full_path = self._dpath(self.current_path, name)
        if is_folder:
            self._CalcFolderSize(full_path, name)
        else:
            stat_cmd = self._root_cmd(f'ls -la {shlex.quote(full_path)}')
            stat_thread = ADBThread(self.adb_path, ['shell', stat_cmd])
            stat_thread.command_finished.connect(lambda out, err: self.show_file_properties(name, full_path, out, err))
            self._start_thread(stat_thread)

    def show_folder_properties(self, name, path, size_output, error):
        if error:
            QMessageBox.critical(self, "Error", f"Failed to get folder properties: {size_output}")
            return
        size = "Unknown"
        if size_output.strip():
            size = size_output.strip().split()[0]

        stat_cmd = self._root_cmd(f'ls -lad {shlex.quote(path)}')
        stat_thread = ADBThread(self.adb_path, ['shell', stat_cmd])
        stat_thread.command_finished.connect(lambda out, err: self.display_properties(name, path, "Folder", size, out, err))
        self._start_thread(stat_thread)

    def show_file_properties(self, name, path, ls_output, error):
        if error:
            QMessageBox.critical(self, "Error", f"Failed to get file properties: {ls_output}")
            return

        lines = ls_output.strip().splitlines()
        if not lines:
            QMessageBox.critical(self, "Error", "No file information found")
            return

        entry = self._parse_ls_entry(lines[0])
        if not entry:
            QMessageBox.critical(self, "Error", "Invalid file information format")
            return

        size = self.format_size_safe(entry["size"])
        file_type = self.detect_type(name)
        self.display_properties(name, path, file_type, size, ls_output, False)

    def display_properties(self, name, path, file_type, size, ls_output, error):
        if error:
            QMessageBox.critical(self, "Error", f"Failed to get item properties: {ls_output}")
            return
        permissions = owner = modified = "Unknown"
        entry = self._parse_ls_entry(ls_output.strip().splitlines()[0] if ls_output.strip() else "")
        if entry:
            permissions = entry["perms"]
            owner = f"{entry['owner']}:{entry['group']}"
            modified = entry["modified"]
        properties = (
            f"Name: {name}\n"
            f"Type: {file_type}\n"
            f"Size: {size}\n"
            f"Path: {path}\n"
            f"Permissions: {permissions}\n"
            f"Owner: {owner}\n"
            f"Modified: {modified}"
        )
        QMessageBox.information(self, f"Properties of {name}", properties)

    def closeEvent(self, event):
        threads_to_close = list(getattr(self, 'threads', []))
        if hasattr(self, '_size_worker') and self._size_worker and self._size_worker not in threads_to_close:
            threads_to_close.append(self._size_worker)
        for thread in threads_to_close:
            if thread.isRunning():
                if hasattr(thread, 'stop'):
                    thread.stop()
                thread.wait(1000)
        event.accept()

    def show_chmod_dialog(self, name, is_folder):
        """Show a dialog with a 3x3 permission grid (Owner/Group/Other x R/W/X),
        live chmod preview, Apply and Revert buttons.
        Robust against early signals and works with PyQt6 enums.
        """
        full_path = self._dpath(self.current_path, name)

        dialog = QDialog(self)
        dialog.setWindowTitle(f"Permissions - {name}")
        dialog.setModal(True)
        layout = QVBoxLayout(dialog)

        grid = QGridLayout()
        grid.addWidget(QLabel(""), 0, 0)
        grid.addWidget(QLabel("Owner"), 0, 1, alignment=Qt.AlignmentFlag.AlignCenter)
        grid.addWidget(QLabel("Group"), 0, 2, alignment=Qt.AlignmentFlag.AlignCenter)
        grid.addWidget(QLabel("Other"), 0, 3, alignment=Qt.AlignmentFlag.AlignCenter)

        rows = [("Read", "r"), ("Write", "w"), ("Execute", "x")]
        checkboxes = {}

        for r_idx, (row_label, key) in enumerate(rows, start=1):
            grid.addWidget(QLabel(row_label), r_idx, 0)
            for c_idx, col in enumerate(["owner", "group", "other"], start=1):
                cb = QCheckBox()
                cb.setToolTip(f"{row_label} - {col}")
                grid.addWidget(cb, r_idx, c_idx, alignment=Qt.AlignmentFlag.AlignCenter)
                checkboxes[(col, key)] = cb

        layout.addLayout(grid)

        preview_label = QLabel("chmod: ")
        preview_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(preview_label)

        btn_layout = QHBoxLayout()
        apply_btn = QPushButton("Apply")
        revert_btn = QPushButton("Revert")
        close_btn = QPushButton("Close")
        btn_layout.addStretch()
        btn_layout.addWidget(revert_btn)
        btn_layout.addWidget(apply_btn)
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

        dialog._original_mode = None
        dialog._current_mode = None

        def perms_to_mode():
            def bits_for(col):
                r = 4 if checkboxes[(col, "r")].isChecked() else 0
                w = 2 if checkboxes[(col, "w")].isChecked() else 0
                x = 1 if checkboxes[(col, "x")].isChecked() else 0
                return r + w + x
            owner = bits_for("owner")
            group = bits_for("group")
            other = bits_for("other")
            return f"{owner}{group}{other}"

        def set_checkboxes_from_mode(mode_str):
            try:
                mode = mode_str.strip()
                if mode.startswith("0") and len(mode) == 4:
                    mode = mode[1:]
                if len(mode) != 3:
                    return
                owner, group, other = [int(ch) for ch in mode]
                def set_bits(col, val):
                    checkboxes[(col, "r")].setChecked(bool(val & 4))
                    checkboxes[(col, "w")].setChecked(bool(val & 2))
                    checkboxes[(col, "x")].setChecked(bool(val & 1))
                set_bits("owner", owner)
                set_bits("group", group)
                set_bits("other", other)
            except Exception:
                pass

        def update_preview():
            mode = perms_to_mode()
            dialog._current_mode = mode
            preview_label.setText(f'chmod {mode} "{full_path}"')

        for cb in checkboxes.values():
            cb.stateChanged.connect(update_preview)

        def handle_stat_result(output, err):
            out = (output or "").strip()
            mode_candidate = None
            if out:
                m = re.search(r'\b([0-7]{3,4})\b', out)
                if m:
                    mode_candidate = m.group(1)
                else:
                    m2 = re.search(r'^(?P<perm>[-drlxspsbtStT]{10,})', out, re.M)
                    if m2:
                        permstr = m2.group('perm')
                        mapping = {'r': 4, 'w': 2, 'x': 1, '-': 0}
                        triplets = [permstr[1:4], permstr[4:7], permstr[7:10]]
                        digits = []
                        for t in triplets:
                            s = 0
                            for ch in t:
                                s += mapping.get(ch, 0)
                            digits.append(str(s))
                        mode_candidate = ''.join(digits)

            if not mode_candidate:
                mode_candidate = "644" if not is_folder else "755"

            dialog._original_mode = mode_candidate
            set_checkboxes_from_mode(mode_candidate)
            update_preview()

        stat_cmd = self._root_cmd(f'stat -c %a {shlex.quote(full_path)}')
        ls_cmd = self._root_cmd(f'ls -ld {shlex.quote(full_path)}')

        stat_thread = ADBThread(self.adb_path, ['shell', stat_cmd])

        def stat_cb(out, err):
            if (out or "").strip():
                handle_stat_result(out, err)
            else:
                ls_thread = ADBThread(self.adb_path, ['shell', ls_cmd])
                ls_thread.command_finished.connect(handle_stat_result)
                self._start_thread(ls_thread)

        stat_thread.command_finished.connect(stat_cb)
        self._start_thread(stat_thread)

        def on_revert():
            if dialog._original_mode:
                set_checkboxes_from_mode(dialog._original_mode)
                update_preview()

        def on_apply():
            mode = dialog._current_mode or perms_to_mode()
            if not mode:
                QMessageBox.critical(dialog, "Error", "Invalid permission selection.")
                return

            emulated_path = (
                "/storage/emulated/" in full_path
                or full_path.startswith("/sdcard")
                or ("/storage/" in full_path and "/emulated/" in full_path)
            )

            if emulated_path:
                proceed = QMessageBox.warning(
                    dialog,
                    "Emulated storage - chmod may be ignored",
                    "Target appears to be on emulated/external storage. "
                    "On many Android devices, sdcardfs/FUSE prevents chmod from changing permissions.\n\n"
                    "Proceed anyway?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                )
                if proceed != QMessageBox.StandardButton.Yes:
                    return

            chmod_cmd = self._root_cmd(f'chmod {mode} {shlex.quote(full_path)}')
            apply_thread = ADBThread(self.adb_path, ['shell', chmod_cmd])

            def _on_chmod_finished(output, err):
                stat_verify_cmd = self._root_cmd(f'stat -c %a {shlex.quote(full_path)}')

                stat_verify_thread = ADBThread(self.adb_path, ['shell', stat_verify_cmd])

                def _on_stat_finished(stat_out, stat_err):
                    stat_text = (stat_out or "").strip()
                    m = re.search(r'([0-7]{3,4})', stat_text)
                    current_mode = m.group(1) if m else None

                    expected = mode.lstrip("0")
                    current = current_mode.lstrip("0") if current_mode else None

                    if current and current == expected:
                        self.statusBar.showMessage(f"Permissions set to {current} for {name}")
                        dialog.accept()
                        self.refresh_file_list()
                    else:
                        if emulated_path:
                            QMessageBox.warning(
                                dialog,
                                "Permissions Unchanged",
                                "chmod completed but permissions did not change (sdcardfs/FUSE likely ignores chmod on this path).\n\n"
                                "Move the file to a writable native partition (e.g. /data) to change UNIX permissions."
                            )
                        else:
                            QMessageBox.critical(
                                dialog,
                                "Failed to Apply Permissions",
                                f"Permissions did not change as expected. Current: {current_mode or '<unknown>'}\nRaw output:\n{stat_text}"
                            )
                        self.refresh_file_list()

                stat_verify_thread.command_finished.connect(_on_stat_finished)
                self._start_thread(stat_verify_thread)

            apply_thread.command_finished.connect(_on_chmod_finished)
            self._start_thread(apply_thread)

        apply_btn.clicked.connect(on_apply)
        revert_btn.clicked.connect(on_revert)
        close_btn.clicked.connect(dialog.reject)

        dialog.resize(460, 280)
        dialog.exec()


FileExplorer = ADBFileExplorer


def main():
    app = QApplication(sys.argv)
    window = ADBFileExplorer()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
