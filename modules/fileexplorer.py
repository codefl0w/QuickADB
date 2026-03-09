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

from util.resource import get_root_dir, resource_path, resolve_platform_tool
root_dir = get_root_dir()
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from util.thememanager import ThemeManager

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

    def __init__(self, adb_path, command, params=None):
        super().__init__()
        self.adb_path = adb_path
        # command: list or string. We'll normalize to list.
        self.command = command if isinstance(command, list) else [command]
        self.params = params or []

    def run(self):
        try:
            # Build final command
            full_command = self.command + self.params
            # If command begins with adb executable name omitted, prefix it
            if full_command[0] != self.adb_path:
                full_command = [self.adb_path] + full_command

            # Windows specific: Create a new process group and hide the console window.
            creationflags = 0
            if os.name == 'nt':
                creationflags = (
                    subprocess.CREATE_NEW_PROCESS_GROUP |
                    subprocess.CREATE_NO_WINDOW
                )

            # Run and capture output
            output = subprocess.check_output(full_command, text=True, stderr=subprocess.STDOUT, creationflags=creationflags)
            self.command_finished.emit(output, False)
        except subprocess.CalledProcessError as e:
            # Include output if available
            out = ""
            try:
                out = e.output
            except Exception:
                out = str(e)
            self.command_finished.emit(out or str(e), True)
        except Exception as e:
            self.command_finished.emit(str(e), True)


class TransferRunner(QThread):
    """Run long-running adb pull/push in background so UI doesn't freeze."""
    transfer_finished = pyqtSignal(str, bool, str)  # output, is_error, local_dest (or src)
    transfer_progress = pyqtSignal(str)  # free-form progress line (not used for UI progress bar here)

    def __init__(self, adb_path, args, cwd=None):
        super().__init__()
        self.adb_path = adb_path
        # args is a list representing the argv after adb (e.g. ['pull', device_path, local_path])
        self.args = args
        self.cwd = cwd or os.getcwd()

    def run(self):
        try:
            cmd = [self.adb_path] + self.args
            # Windows specific: Create a new process group and hide the console window.
            creationflags = 0
            if os.name == 'nt':
                creationflags = (
                    subprocess.CREATE_NEW_PROCESS_GROUP |
                    subprocess.CREATE_NO_WINDOW
                )

            # Use universal_newlines & line-buffering to allow streaming if adb prints progress
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=self.cwd,
                universal_newlines=True,
                bufsize=1,
                creationflags=creationflags
            )

            last_line = ""
            while True:
                line = proc.stdout.readline()
                if not line and proc.poll() is not None:
                    break
                if line:
                    last_line = line.rstrip("\n")
                    # emit progress lines so caller can optionally log them
                    self.transfer_progress.emit(last_line)

            ret = proc.wait()
            if ret == 0:
                # For pull: args = ['pull', device_path, local_path] -> local_path is args[-1]
                local_target = self.args[-1] if len(self.args) >= 2 else ""
                self.transfer_finished.emit(last_line or "Transfer completed", False, local_target)
            else:
                self.transfer_finished.emit(last_line or f"Transfer failed (code {ret})", True, "")
        except Exception as e:
            self.transfer_finished.emit(str(e), True, "")


class ADBFileExplorer(QMainWindow):
    # Constants for viewable file types
    VIEWABLE_TEXT_EXTENSIONS = ("txt", "log", "json", "xml", "html", "csv", "md", "ini", "conf", "prop", "sh")
    VIEWABLE_IMAGE_EXTENSIONS = ("png", "jpg", "jpeg", "gif", "bmp")

    def __init__(self):
        super().__init__()
        self.setWindowTitle("QuickADB File Explorer")
        self.setMinimumSize(1000, 600)

        # paths and adb
        self.platform_tools_path = resource_path('platform-tools')
        self.adb_path = resolve_platform_tool(self.platform_tools_path, 'adb')

        # state
        self.current_path = "/storage/emulated/0"
        self.history_stack = []
        self.forward_stack = []
        self.selected_items = []
        self.copy_mode = False
        self.threads = []  # Store running threads
        self.symlink_targets = {}

        # UI init
        self.init_ui()
        ThemeManager.apply_theme(self)
        self.refresh_file_list()

    @property
    def is_root(self):
        """Returns True if the root access checkbox is checked."""
        return hasattr(self, 'root_access_checkbox') and self.root_access_checkbox.isChecked()

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
        return f"su -c \"{cmd}\"" if self.is_root else cmd

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
    # UI Initialization (grouped)
    # -----------------------------
    def init_ui(self):
        self.init_menu_bar()
        self.init_toolbar()
        self.init_path_bar()
        self.init_file_view()

        # status bar
        self.statusBar = QStatusBar()
        self.setStatusBar(self.statusBar)
        self.statusBar.showMessage("Ready")

        # central layout
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

        # navigation
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
        self.sort_combo.currentIndexChanged.connect(self.sort_table)
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
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.open_context_menu)
        self.table.cellDoubleClicked.connect(self.handle_double_click)
        self.table.horizontalHeader().sectionClicked.connect(self.header_clicked)

    # -----------------------------
    # Navigation / path operations
    # -----------------------------
    def _navigate_to(self, new_path: str, push_history: bool = True):
        """Central navigation: update path, history and refresh."""
        if not new_path or new_path == self.current_path:
            return
        if push_history:
            self.history_stack.append(self.current_path)
        self.forward_stack.clear()
        self.current_path = new_path
        self.path_field.setText(self.current_path)
        self.back_button.setEnabled(bool(self.history_stack))
        self.forward_button.setEnabled(False)
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

        if self.is_root:
            cmd = ['shell', f'su -c "ls -la \\"{self.current_path}\\" 2>&1"']
            self.statusBar.showMessage("Loading directory contents with root access...")
        else:
            cmd = ['shell', f'ls -la "{self.current_path}" 2>&1']

        ls_thread = ADBThread(self.adb_path, cmd, [])
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
        
        # Reset table and prepare for new data
        self.table.setRowCount(0)
        rows = []

        for line in output.splitlines():
            if not line.strip() or line.startswith("total"):
                continue
            if "Permission denied" in line or line.startswith("ls:") or line.startswith("cannot access"):
                permission_issue = True
                continue

            # IMPROVEMENT: Use maxsplit=7 to correctly handle filenames with spaces.
            # The filename (and potential symlink) will be the last element.
            parts = line.split(maxsplit=7)
            if len(parts) < 8:
                continue

            perms = parts[0]
            size = parts[4]
            date_str = f"{parts[5]} {parts[6]}"
            name_part = parts[7]
            
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

        # Sort: folders first then by name
        rows.sort(key=lambda x: (not x[5], x[0].lower()))

        # PERFORMANCE: Disable sorting during population for speed
        self.table.setSortingEnabled(False)
        
        # Add parent directory entry ".."
        if self.current_path != "/" and not self.current_path.endswith(":/"):
            self.table.insertRow(0)
            self.table.setItem(0, 0, QTableWidgetItem(".."))
            self.table.setItem(0, 1, QTableWidgetItem("Folder"))
            self.table.setItem(0, 2, QTableWidgetItem("-"))
            self.table.setItem(0, 3, QTableWidgetItem("-"))

        # Populate table with file/folder data
        for name, file_type, size_str, date_str, _, is_folder, symlink_info in rows:
            row = self.table.rowCount()
            self.table.insertRow(row)
            item = QTableWidgetItem(name)
            if symlink_info:
                self.symlink_targets[row] = symlink_info
                item.setToolTip(f"Symlink to: {symlink_info}")
            self.table.setItem(row, 0, item)
            self.table.setItem(row, 1, QTableWidgetItem(file_type))
            self.table.setItem(row, 2, QTableWidgetItem(size_str))
            self.table.setItem(row, 3, QTableWidgetItem(date_str))

        # Re-enable sorting and apply the current sort preference
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
            symlink_target = self.symlink_targets.get(row)
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
            self.view_or_pull_file(selected_name)

    def view_or_pull_file(self, filename):
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
            self.view_file_contents(filename, image_ext)

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
            if self.is_root:
                device_temp = f"/data/local/tmp/{filename}"
                dd_cmd = f'su -c "dd if=\\"{full_path}\\" of=\\"{device_temp}\\" && chmod 644 \\"{device_temp}\\""'
                dd_thread = ADBThread(self.adb_path, ['shell'], [dd_cmd])
                dd_thread.command_finished.connect(
                    lambda out, err: self.pull_temp_image(out, err, filename, device_temp, temp_path)
                )
                self._start_thread(dd_thread)
            else:
                pull = TransferRunner(self.adb_path, ['pull', full_path, temp_path])
                pull.transfer_finished.connect(lambda out, err, dest: self.display_image(out, err, filename, dest))
                self._start_thread(pull)
        else:
            self.statusBar.showMessage(f"Loading contents of {filename}...")
            shell_cmd = f'su -c "cat \\"{full_path}\\""' if self.is_root else f'cat "{full_path}"'
            t = ADBThread(self.adb_path, ['shell', shell_cmd], [])
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
        cleanup = ADBThread(self.adb_path, ['shell'], [f'rm "{device_temp}"'])
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
            if pixmap.width() > 780 or pixmap.height() > 580:
                pixmap = pixmap.scaled(780, 580, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            image_label.setPixmap(pixmap)
            image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(image_label)
            info_label = QLabel(f"Size: {pixmap.width()}x{pixmap.height()} | File: {filename}")
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
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Contents of {filename}")
        dialog.setMinimumSize(800, 600)
        layout = QVBoxLayout(dialog)
        text_edit = QPlainTextEdit()
        text_edit.setPlainText(content)
        text_edit.setReadOnly(False)
        font = QFont("Courier New", 10)
        text_edit.setFont(font)
        layout.addWidget(text_edit)

        button_layout = QHBoxLayout()
        copy_button = QPushButton("Copy to Clipboard")
        copy_button.clicked.connect(lambda: QApplication.clipboard().setText(text_edit.toPlainText()))
        button_layout.addWidget(copy_button)

        save_as_button = QPushButton("Save As...")
        save_as_button.clicked.connect(lambda: self.save_file_content(filename, text_edit.toPlainText()))
        button_layout.addWidget(save_as_button)

        save_device_button = QPushButton("Save to Device")
        save_device_button.clicked.connect(lambda: self.save_to_device(filename, text_edit.toPlainText()))
        button_layout.addWidget(save_device_button)

        close_button = QPushButton("Close")
        close_button.clicked.connect(dialog.accept)
        button_layout.addWidget(close_button)

        layout.addLayout(button_layout)
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
        reply = QMessageBox.question(
            self, "Save to Device",
            f"Are you sure you want to save changes to {full_path} on the device?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.No:
            return

        content_b64 = base64.b64encode(content.encode('utf-8')).decode('ascii')
        self.statusBar.showMessage(f"Saving {filename} to device...")
        save_cmd = self._root_cmd(f'echo \'{content_b64}\' | base64 -d > \\"{full_path}\\"') \
            if self.is_root else f'echo \'{content_b64}\' | base64 -d > "{full_path}"'
        t = ADBThread(self.adb_path, ['shell'], [save_cmd])
        t.command_finished.connect(lambda out, err: self.save_complete(out, err, filename))
        self._start_thread(t)

    def save_complete(self, output, error, filename):
        if error:
            QMessageBox.critical(self, "Error", f"Could not save file to device: {output}")
            self.statusBar.showMessage("Error saving file")
        else:
            self.statusBar.showMessage(f"File {filename} saved successfully to device")
            self.refresh_file_list()

    # -----------------------------
    # Pull / Push (now threaded)
    # -----------------------------
    def pull_file(self, filename, save_path=None):
        full_path = self._dpath(self.current_path, filename)
        if save_path is None:
            save_path, _ = QFileDialog.getSaveFileName(self, "Save File As", filename, "All Files (*)")
            if not save_path:
                return
        self.statusBar.showMessage(f"Pulling file {filename}...")

        if self.is_root:
            device_temp = f"/data/local/tmp/{filename}"
            dd_cmd = f'su -c "dd if=\\"{full_path}\\" of=\\"{device_temp}\\" && chmod 644 \\"{device_temp}\\""'
            t = ADBThread(self.adb_path, ['shell'], [dd_cmd])
            t.command_finished.connect(lambda out, err: self.pull_temp_file(out, err, filename, device_temp, save_path))
            self._start_thread(t)
        else:
            transfer = TransferRunner(self.adb_path, ['pull', full_path, save_path])
            transfer.transfer_finished.connect(lambda out, err, dest: self.pull_complete(out, err, dest))
            transfer.transfer_progress.connect(self.handle_transfer_progress)
            self._start_thread(transfer)

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
        cleanup = ADBThread(self.adb_path, ['shell'], [f'rm "{device_temp}"'])
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
        if is_directory:
            self.chmod_and_pull(android_path, dest_path, name, progress)
        else:
            transfer = TransferRunner(self.adb_path, ['pull', android_path, dest_path])
            transfer.transfer_finished.connect(lambda out, err, dest: self.handle_pull_result(out, err, name, progress, android_path, dest))
            transfer.transfer_progress.connect(self.handle_transfer_progress)
            self._start_thread(transfer)

    def handle_pull_result(self, output, error, name, progress, source_path, dest_path, is_directory=False):
        if error:
            self.statusBar.showMessage(f"Regular pull failed for {name}, trying chmod fallback...")
            progress.setLabelText(f"Regular pull failed, trying chmod for {name}...")
            self.chmod_and_pull(source_path, dest_path, name, progress)
        else:
            progress.close()
            self.statusBar.showMessage(f"Successfully pulled {name}")

    def chmod_and_pull(self, source_path, dest_path, name, progress):
        chmod_thread = ADBThread(self.adb_path, ['shell'], [f'chmod -R 777 "{source_path}"'])
        chmod_thread.command_finished.connect(lambda out, err: self.perform_pull_after_chmod(out, err, source_path, dest_path, name, progress))
        self._start_thread(chmod_thread)

    def perform_pull_after_chmod(self, output, error, source_path, dest_path, name, progress):
        if error:
            progress.close()
            QMessageBox.critical(self, "Error", f"Failed to set permissions for {name}: {output}")
            self.statusBar.showMessage(f"Failed to set permissions for {name}")
            return
        progress.setLabelText(f"Pulling {name} after chmod...")
        transfer = TransferRunner(self.adb_path, ['pull', source_path, dest_path])
        transfer.transfer_finished.connect(lambda out, err, dest: self.finish_chmod_pull(out, err, name, progress))
        transfer.transfer_progress.connect(self.handle_transfer_progress)
        self._start_thread(transfer)

    def finish_chmod_pull(self, output, error, name, progress):
        progress.close()
        if error:
            QMessageBox.critical(self, "Error", f"Failed to pull {name} even after chmod: {output}")
            self.statusBar.showMessage(f"Failed to pull {name}")
        else:
            self.statusBar.showMessage(f"Successfully pulled {name} using chmod method")

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
                check_thread = ADBThread(self.adb_path, ['shell'], [f'[ -d "{source_path}" ] && echo "dir" || echo "file"'])
                check_thread.command_finished.connect(lambda out, err, s=source_path, d=dest_path: self.perform_copy(s, d, "dir" in out.strip()))
                self._start_thread(check_thread)
            else:
                move_thread = ADBThread(self.adb_path, ['shell'], [f'mv "{source_path}" "{dest_path}"'])
                move_thread.command_finished.connect(lambda out, err: self.handle_paste_result(out, err))
                self._start_thread(move_thread)
        self.selected_items = []

    def perform_copy(self, source_path, dest_path, is_directory):
        copy_cmd = f'cp -R "{source_path}" "{dest_path}"' if is_directory else f'cp "{source_path}" "{dest_path}"'
        copy_thread = ADBThread(self.adb_path, ['shell'], [copy_cmd])
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
        mkdir_cmd = self._root_cmd(f'mkdir -p \\"{full_path}\\"') if self.is_root else f'mkdir -p "{full_path}"'
        mkdir_thread = ADBThread(self.adb_path, ['shell'], [mkdir_cmd])
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
        touch_cmd = self._root_cmd(f'touch \\"{full_path}\\"') if self.is_root else f'touch "{full_path}"'
        touch_thread = ADBThread(self.adb_path, ['shell'], [touch_cmd])
        touch_thread.command_finished.connect(lambda out, err: self.handle_touch_result(out, err, file_name))
        self._start_thread(touch_thread)
    
    def handle_touch_result(self, output, error, file_name):
        if error:
            QMessageBox.critical(self, "Error", f"Failed to create file {file_name}: {output}")
        else:
            self.statusBar.showMessage(f"Created file {file_name}")
            self.refresh_file_list()   

    def execute_shell_script(self, name):
        if not name.endswith('.sh'):
            return
        full_path = f"{self.current_path.rstrip('/')}/{name}"
        if self.is_root:
            chmod_cmd = f'su -c "chmod +x \\"{full_path}\\""'
            chmod_thread = ADBThread(self.adb_path, ['shell'], [chmod_cmd])
            chmod_thread.command_finished.connect(
                lambda out, err: self._run_script(['shell', 'su', '-c', 'sh', full_path], name) if not err else None
            )
            self._start_thread(chmod_thread)
        else:
            self._run_script(['shell', 'sh', full_path], name)

    def _run_script(self, cmd_args: list, name: str):
        """Open a live output dialog and stream script execution into it."""
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Executing: {name}")
        dialog.setMinimumSize(700, 450)
        dialog.setModal(False)

        layout = QVBoxLayout(dialog)

        header = QLabel(f"Output of <b>{name}</b>")
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
                output_view.appendPlainText(f"\n[✗] Script exited with an error.")
                self.statusBar.showMessage(f"Script {name} finished with errors.")
            else:
                output_view.appendPlainText(f"\n[✓] Done.")
                self.statusBar.showMessage(f"Script {name} executed successfully.")
            close_btn.setEnabled(True)
            self.refresh_file_list()

        runner.transfer_progress.connect(_on_line)
        runner.transfer_finished.connect(_on_done)
        self._start_thread(runner)

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
            menu.addAction("View", lambda: self.view_file_contents(selected_name))
        
        menu.addSeparator()
        menu.addAction("Pull to PC", lambda: self.pull_file(selected_name))
        if not is_folder:
            menu.addAction("Push to Device", self.push_file)
        
        if not is_folder and selected_name.endswith('.sh'):
            menu.addAction("Execute Script", lambda: self.execute_shell_script(selected_name))
            
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
        mv_cmd = self._root_cmd(f'mv \\"{old_path}\\" \\"{new_path}\\"') \
            if self.is_root else f'mv "{old_path}" "{new_path}"'
        rename_thread = ADBThread(self.adb_path, ['shell'], [mv_cmd])
        rename_thread.command_finished.connect(lambda out, err: self.handle_rename_result(out, err, name, new_name))
        self._start_thread(rename_thread)

    def handle_rename_result(self, output, error, old_name, new_name):
        if error:
            QMessageBox.critical(self, "Error", f"Failed to rename {old_name}: {output}")
        else:
            self.statusBar.showMessage(f"Renamed {old_name} to {new_name}")
            self.refresh_file_list()

    def delete_item(self, name):
        confirm = QMessageBox.question(self, "Confirm Delete", f"Are you sure you want to delete '{name}'?",
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if confirm != QMessageBox.StandardButton.Yes:
            return
        path = self._dpath(self.current_path, name)
        check_cmd = self._root_cmd(f'[ -d \\"{path}\\" ] && echo \\"dir\\" || echo \\"file\\"') \
            if self.is_root else f'[ -d "{path}" ] && echo "dir" || echo "file"'
        is_dir_thread = ADBThread(self.adb_path, ['shell'], [check_cmd])
        is_dir_thread.command_finished.connect(lambda out, err: self.perform_delete(path, name, "dir" in out.strip()))
        self._start_thread(is_dir_thread)

    def perform_delete(self, path, name, is_directory):
        delete_command = f'rm -rf "{path}"' if is_directory else f'rm "{path}"'
        delete_cmd = self._root_cmd(delete_command.replace('"', '\\"')) \
            if self.is_root else delete_command
        delete_thread = ADBThread(self.adb_path, ['shell'], [delete_cmd])
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
        confirm = QMessageBox.question(self, "Confirm Delete", f"Delete {len(items_to_delete)} selected item(s)?",
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if confirm != QMessageBox.StandardButton.Yes:
            return
        for name in items_to_delete:
            self.delete_item(name)

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
        # The QTableWidget's default sorting is sufficient now that it's enabled.
        # This function can just trigger it if needed, or we can rely on header clicks.
        sort_column = self.sort_combo.currentIndex()
        self.table.sortByColumn(sort_column, Qt.SortOrder.AscendingOrder)

    def header_clicked(self, column):
        self.sort_combo.setCurrentIndex(column)

    def show_properties(self, name, is_folder):
        full_path = self._dpath(self.current_path, name)
        if is_folder:
            size_thread = ADBThread(self.adb_path, ['shell'], [f'du -sh "{full_path}"'])
            size_thread.command_finished.connect(lambda out, err: self.show_folder_properties(name, full_path, out, err))
            self._start_thread(size_thread)
        else:
            stat_thread = ADBThread(self.adb_path, ['shell'], [f'ls -la "{full_path}"'])
            stat_thread.command_finished.connect(lambda out, err: self.show_file_properties(name, full_path, out, err))
            self._start_thread(stat_thread)

    def show_folder_properties(self, name, path, size_output, error):
        if error:
            QMessageBox.critical(self, "Error", f"Failed to get folder properties: {size_output}")
            return
        size = "Unknown"
        if size_output.strip():
            size = size_output.strip().split()[0]
        
        stat_thread = ADBThread(self.adb_path, ['shell'], [f'ls -lad "{path}"'])
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
        
        parts = lines[0].split()
        if len(parts) < 5:
            QMessageBox.critical(self, "Error", "Invalid file information format")
            return
            
        size = self.format_size_safe(parts[4])
        file_type = self.detect_type(name)
        self.display_properties(name, path, file_type, size, ls_output, False)

    def display_properties(self, name, path, file_type, size, ls_output, error):
        if error:
            QMessageBox.critical(self, "Error", f"Failed to get item properties: {ls_output}")
            return
        permissions = owner = modified = "Unknown"
        try:
            parts = ls_output.strip().splitlines()[0].split(maxsplit=7)
            if len(parts) >= 8:
                permissions = parts[0]
                owner = f"{parts[2]}:{parts[3]}"
                modified = f"{parts[5]} {parts[6]}"
        except IndexError:
            pass
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
        for thread in self.threads:
            if thread.isRunning():
                thread.terminate()
                thread.wait(100)
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
    
        # Grid headers
        grid = QGridLayout()
        grid.addWidget(QLabel(""), 0, 0)  # spacer
        grid.addWidget(QLabel("Owner"), 0, 1, alignment=Qt.AlignmentFlag.AlignCenter)
        grid.addWidget(QLabel("Group"), 0, 2, alignment=Qt.AlignmentFlag.AlignCenter)
        grid.addWidget(QLabel("Other"), 0, 3, alignment=Qt.AlignmentFlag.AlignCenter)
    
        # Row labels and checkboxes storage (use a local dict to avoid races)
        # Row labels and checkboxes storage
        rows = [("Read", "r"), ("Write", "w"), ("Execute", "x")]
        checkboxes = {}  # keys like ("owner","r") etc for later access
        
        for r_idx, (row_label, key) in enumerate(rows, start=1):
            grid.addWidget(QLabel(row_label), r_idx, 0)
            for c_idx, col in enumerate(["owner", "group", "other"], start=1):
                cb = QCheckBox()
                cb.setToolTip(f"{row_label} - {col}")
                grid.addWidget(cb, r_idx, c_idx, alignment=Qt.AlignmentFlag.AlignCenter)
                checkboxes[(col, key)] = cb  # store 'r', 'w', 'x'
        
    
        layout.addLayout(grid)
    
        # Command preview (selectable)
        preview_label = QLabel("chmod: ")
        preview_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(preview_label)
    
        # Buttons
        btn_layout = QHBoxLayout()
        apply_btn = QPushButton("Apply")
        revert_btn = QPushButton("Revert")
        close_btn = QPushButton("Close")
        btn_layout.addStretch()
        btn_layout.addWidget(revert_btn)
        btn_layout.addWidget(apply_btn)
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)
    
        # State holders
        dialog._original_mode = None  # numeric string like '0755'
        dialog._current_mode = None
    
        # Helpers: read checkboxes and compute octal mode
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
    
        # Connect checkboxes to preview AFTER building the dictionary
        for cb in checkboxes.values():
            cb.stateChanged.connect(update_preview)
    
        # Fetch current permissions (try stat -c %a, then fall back to ls -ld)
        def handle_stat_result(output, err):
            out = (output or "").strip()
            mode_candidate = None
            if out:

                # try to find 3 or 4 digit octal in output
                m = re.search(r'\b([0-7]{3,4})\b', out)
                if m:
                    mode_candidate = m.group(1)
                else:
                    # fallback: parse ls -ld style permission string like -rwxr-xr-x
                    m2 = re.search(r'^(?P<perm>[-drlxspsbtStT]{10,})', out, re.M)
                    if m2:
                        permstr = m2.group('perm')
                        mapping = {'r':4,'w':2,'x':1,'-':0}
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
    
        # Try stat first (more reliable), fall back to ls -ld
        stat_cmd = f"stat -c %a \"{full_path}\""
        ls_cmd = f"ls -ld \"{full_path}\""
    
        stat_thread = ADBThread(self.adb_path, ['shell'], [stat_cmd])
    
        def stat_cb(out, err):
            if (out or "").strip():
                handle_stat_result(out, err)
            else:
                # fallback thread for ls -ld
                ls_thread = ADBThread(self.adb_path, ['shell'], [ls_cmd])
                ls_thread.command_finished.connect(handle_stat_result)
                self._start_thread(ls_thread)
    
        stat_thread.command_finished.connect(stat_cb)
        self._start_thread(stat_thread)
    
        # Revert handler
        def on_revert():
            if dialog._original_mode:
                set_checkboxes_from_mode(dialog._original_mode)
                update_preview()

        # Apply handler (use su -c when root checkbox is selected)
        def on_apply():
            mode = dialog._current_mode or perms_to_mode()
            if not mode:
                QMessageBox.critical(dialog, "Error", "Invalid permission selection.")
                return
        
            # Detect emulated/external storage where chmod is often ignored
            emulated_path = (
                "/storage/emulated/" in full_path
                or full_path.startswith("/sdcard")
                or ("/storage/" in full_path and "/emulated/" in full_path)
            )
        
            # If emulated, warn the user and require explicit confirmation to proceed
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
        
            # Build chmod command. Use su -c when root checkbox is selected.
            if self.is_root:
                chmod_cmd = f"su -c 'chmod {mode} \"{full_path}\"'"
            else:
                chmod_cmd = f'chmod {mode} "{full_path}"'
            apply_thread = ADBThread(self.adb_path, ["shell"], [chmod_cmd])
        
            # After chmod completes, run stat -c %a to confirm permission bits
            def _on_chmod_finished(output, err):
                stat_cmd = f'stat -c %a "{full_path}"'
                stat_thread = ADBThread(self.adb_path, ["shell"], [stat_cmd])
        
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
        
                stat_thread.command_finished.connect(_on_stat_finished)
                self._start_thread(stat_thread)
        
            apply_thread.command_finished.connect(_on_chmod_finished)
            self._start_thread(apply_thread)
        
    
        apply_btn.clicked.connect(on_apply)
        revert_btn.clicked.connect(on_revert)
        close_btn.clicked.connect(dialog.reject)
    
        # Show dialog
        dialog.resize(460, 280)
        dialog.exec()


def main():
    app = QApplication(sys.argv)
    window = ADBFileExplorer()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
