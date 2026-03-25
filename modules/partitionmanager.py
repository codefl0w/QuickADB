'''
partitionmanager.py - View all device partitions by using "ls /dev/block/by-name". All listed partitions can be pulled from the device
as .img files for flashing. You can also write .img files to partition blocks, though it's not the best method to flash.
None of this can work without superuser rights for obvious reasons.

'''


from util.thememanager import ThemeManager
import sys
import os

from util.resource import get_root_dir, resource_path
from util.toolpaths import ToolPaths
from util.devicemanager import DeviceManager
root_dir = get_root_dir()
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)
import subprocess
import re
import datetime
import time
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSize, QTimer
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QTreeView, QFrame, QFileDialog, QMessageBox, 
    QProgressDialog, QTextEdit, QHeaderView, QStyledItemDelegate
)
from PyQt6.QtGui import QFont, QStandardItemModel, QStandardItem, QColor
from util.devicemanager import DeviceManager

class FormatSize:
    """Helper class to format file sizes in human-readable format."""
    @staticmethod
    def human_readable_size(size_bytes):
        """Convert bytes to human-readable format."""
        if size_bytes == 0:
            return "0 B"
        
        size_names = ("B", "KB", "MB", "GB", "TB")
        i = 0
        while size_bytes >= 1024 and i < len(size_names) - 1:
            size_bytes /= 1024
            i += 1
        
        return f"{size_bytes:.2f} {size_names[i]}"

class PartitionLoadWorker(QThread):
    """Worker thread for loading partitions without blocking the UI."""
    partitions_loaded = pyqtSignal(list)
    log_message = pyqtSignal(str)
    progress_update = pyqtSignal(int, int)  # current, total
    finished_loading = pyqtSignal()
    error_occurred = pyqtSignal(str)
    
    def __init__(self, platform_tools_path):
        super().__init__()
        self.platform_tools_path = platform_tools_path
        
    def run(self):
        """Load partitions from the device."""
        try:
            self.log_message.emit("Loading and analyzing partitions from device...")
            adb_exe = ToolPaths.instance().adb

            serial = DeviceManager.instance().serial_args()
            
            # Use cat /proc/partitions for accurate sizes and ls -lL for name mapping.
            remote_script = "cat /proc/partitions; echo '---SEP---'; ls -lL /dev/block/by-name"
            
            command = [
                adb_exe, *serial, 'shell', 'su', '-c', remote_script
            ]
            creationflags = 0
            if sys.platform == "win32":
                creationflags = (
                    subprocess.CREATE_NEW_PROCESS_GROUP |
                    subprocess.CREATE_NO_WINDOW
                )
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creationflags,
                text=True
            )
            
            if result.returncode != 0:
                self.error_occurred.emit(f"Failed to load partitions (RC {result.returncode}):\n{result.stderr.strip()}")
                return
            
            output = result.stdout.strip()
            if '---SEP---' not in output:
                self.error_occurred.emit("Device returned unexpected output format. Ensure 'su' is granted.")
                return

            proc_part_text, ls_text = output.split('---SEP---', 1)
            
            # 1. Parse /proc/partitions mapping (major, minor) -> size_bytes
            size_map = {}
            for line in proc_part_text.strip().splitlines():
                # Format: major minor  #blocks  name
                # Sample:   259    22    1048576  mmcblk0p22
                match = re.search(r'^\s*(\d+)\s+(\d+)\s+(\d+)\s+', line)
                if match:
                    major, minor, blocks = match.groups()
                    size_map[(int(major), int(minor))] = int(blocks) * 1024

            # 2. Parse ls -lL output for name mapping
            partitions_data = []
            ls_lines = ls_text.strip().splitlines()
            total_lines = len(ls_lines)
            
            for current, line in enumerate(ls_lines):
                line = line.strip()
                if not line or line.startswith('total'):
                    continue

                # Format: brw------- 1 root root  259,  22 2026-03-04 18:00 system
                # Regex looks for common block device pattern: major, minor, ..., name
                # Flexible on space after comma.
                match = re.search(r'([0-9]+),\s*([0-9]+)\s+.*?\s+([^/\s]+)$', line)
                if match:
                    major, minor, name = match.groups()
                    m_tuple = (int(major), int(minor))
                    
                    size_bytes = size_map.get(m_tuple, 0)
                    size_human = FormatSize.human_readable_size(size_bytes)
                    
                    # Periodic UI updates
                    if current % 10 == 0:
                        self.progress_update.emit(current + 1, total_lines)
                        self.log_message.emit(f"Mapping: {name} -> {m_tuple}")
                    
                    partitions_data.append({
                        'name': name,
                        'path': f"/dev/block/by-name/{name}",
                        'permissions': line.split()[0], 
                        'size_bytes': size_bytes,
                        'size_human': size_human
                    })
                else:
                    if line:
                        self.log_message.emit(f"Skipping line (no match): {line}")

            if not partitions_data:
                self.error_occurred.emit("No partitions found in /dev/block/by-name. System might be restricted.")
                return

            # Emit the loaded partitions
            self.partitions_loaded.emit(partitions_data)
            self.log_message.emit(f"Loaded {len(partitions_data)} partitions successfully.")
            self.finished_loading.emit()
        
        except Exception as e:
            self.error_occurred.emit(f"Error loading partitions:\n{str(e)}")

class PartitionPullWorker(QThread):
    log_message = pyqtSignal(str)
    progress = pyqtSignal(str)
    finished_with_status = pyqtSignal(bool, int, int)  # success flag, success count, total count
    
    def __init__(self, platform_tools_path, selected_partitions, save_dir):
        super().__init__()
        self.platform_tools_path = platform_tools_path
        self.selected_partitions = selected_partitions
        self.save_dir = save_dir
    
    def run(self):
        try:
            total_partitions = len(self.selected_partitions)
            successful_pulls = 0
            adb_exe = ToolPaths.instance().adb
            serial = DeviceManager.instance().serial_args()
            
            for i, partition_info in enumerate(self.selected_partitions):
                partition_name = partition_info['name']
                total_bytes = partition_info.get('size_bytes', 0)
                current_num = i + 1
                
                # Step 1: Copy the partition to /sdcard with bs=4M
                self.progress.emit(f"({current_num}/{total_partitions}) Copying {partition_name}...")
                self.log_message.emit(f"Copying {partition_name} to device sdcard...")
                
                target_path = f"/sdcard/{partition_name}.img"
                dd_command = [
                    adb_exe, *serial,
                    'shell', 'su', '-c',
                    f'dd if=/dev/block/by-name/{partition_name} of={target_path} bs=4M'
                ]
                creationflags = 0
                if sys.platform == "win32":
                    creationflags = (
                        subprocess.CREATE_NEW_PROCESS_GROUP |
                        subprocess.CREATE_NO_WINDOW
                    )

                # Start dd process
                process = subprocess.Popen(
                    dd_command, 
                    stdout=subprocess.PIPE, 
                    stderr=subprocess.PIPE, 
                    text=True,
                    creationflags=creationflags
                )
                
                # Polling loop for progress monitoring
                last_size = 0
                start_time = time.time()
                while process.poll() is None:
                    # Run ls -l to get the current file size
                    ls_command = [
                        adb_exe, *serial,
                        'shell', 'su', '-c', f'ls -l {target_path}'
                    ]
                    ls_result = subprocess.run(ls_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=creationflags)
                    
                    if ls_result.returncode == 0:
                        # Parse ls -l output: -rw-rw---- 1 root sdcard_rw 67108864 2026-03-04 19:58 boot.img
                        parts = ls_result.stdout.strip().split()
                        if len(parts) >= 5:
                            try:
                                # Standard ls -l format usually has size at index 3 or 4
                                # On many Androids it's: permissions links user group size date time name
                                # In the example from my local test device: -rw-rw---- 1 u0_a155 media_rw 67108864 2026-02-28 20:38 boot.img
                                # The size is '67108864' at index 4
                                current_size = int(parts[4])
                                
                                # Calculate progress
                                progress_pct = (current_size / total_bytes * 100) if total_bytes > 0 else 0
                                
                                # Calculate speed
                                elapsed = time.time() - start_time
                                speed_mb = (current_size / (1024 * 1024)) / elapsed if elapsed > 0 else 0
                                
                                size_mb = current_size / (1024 * 1024)
                                self.progress.emit(f"({current_num}/{total_partitions}) {partition_name}: {size_mb:.1f}MB ({progress_pct:.1f}%) @ {speed_mb:.1f} MB/s")
                                last_size = current_size
                            except (ValueError, IndexError):
                                pass
                    
                    time.sleep(1) # Poll every second
                
                if process.returncode != 0:
                    err = process.stderr.read()
                    self.log_message.emit(f"Failed to copy {partition_name}: {err.strip()}")
                    continue
                
                # Step 2: Pull the image from the device
                self.progress.emit(f"({current_num}/{total_partitions}) Pulling {partition_name} to PC...")
                self.log_message.emit(f"Pulling {partition_name} from device (may take a while)...")
                
                pull_command = [
                    adb_exe, *serial,
                    'pull', target_path, self.save_dir
                ]
                result = subprocess.run(pull_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=creationflags)
                
                if result.returncode != 0:
                    self.log_message.emit(f"Failed to pull {partition_name}: {result.stderr.strip()}")
                    # Cleanup attempt
                    subprocess.run([adb_exe, *serial, 'shell', 'su', '-c', f'rm {target_path}'], creationflags=creationflags)
                    continue
                
                # Step 3: Remove the image from the device
                self.log_message.emit(f"Cleaning up {partition_name} from device...")
                rm_command = [
                    adb_exe, *serial,
                    'shell', 'su', '-c', f'rm {target_path}'
                ]
                subprocess.run(rm_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=creationflags)
                
                successful_pulls += 1
                self.log_message.emit(f"Pulled {partition_name} successfully")
                self.progress.emit(f"({current_num}/{total_partitions}) Done: {partition_name}")
            
            # Emit final status
            self.finished_with_status.emit(successful_pulls > 0, successful_pulls, total_partitions)
            
        except Exception as e:
            self.log_message.emit(f"Error during pull operation: {str(e)}")
            self.progress.emit(f"Error during pull operation")
            self.finished_with_status.emit(False, 0, len(self.selected_partitions))

class PartitionFlashWorker(QThread):
    log_message = pyqtSignal(str)
    progress = pyqtSignal(str)
    finished_with_status = pyqtSignal(bool)  # success flag
    
    def __init__(self, platform_tools_path, partition_info, image_path):
        super().__init__()
        self.platform_tools_path = platform_tools_path
        self.partition_info = partition_info
        self.image_path = image_path
    
    def run(self):
        try:
            partition_name = self.partition_info['name']
            partition_success = True
            adb_exe = ToolPaths.instance().adb

            serial = DeviceManager.instance().serial_args()
            
            # Step 1: Push the image file to /sdcard
            self.progress.emit(f"Pushing image file to device...")
            self.log_message.emit(f"Pushing image for {partition_name} to device...")
            push_command = [
                adb_exe, *serial,
                'push', self.image_path, f'/sdcard/{partition_name}.img'
            ]
            # Windows specific: Create a new process group and hide the console window.
            creationflags = 0
            if sys.platform == "win32":
                creationflags = (
                    subprocess.CREATE_NEW_PROCESS_GROUP |
                    subprocess.CREATE_NO_WINDOW
                )

            result = subprocess.run(push_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=creationflags)
            if result.returncode != 0:
                self.log_message.emit(f"Failed to push image to device: {result.stderr.strip()}")
                self.progress.emit(f"Failed to push image to device")
                self.finished_with_status.emit(False)
                return
            
            # Step 2: Flash the image to the partition
            self.progress.emit(f"Flashing {partition_name}...")
            self.log_message.emit(f"Flashing {partition_name}...")
            dd_command = [
                adb_exe, *serial,
                'shell', 'su', '-c',
                f'dd if=/sdcard/{partition_name}.img of=/dev/block/by-name/{partition_name} bs=4M'
            ]
            result = subprocess.run(dd_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=creationflags)
            if result.returncode != 0:
                self.log_message.emit(f"Failed to flash {partition_name}: {result.stderr.strip()}")
                self.progress.emit(f"Failed to flash {partition_name}")
                partition_success = False
            else:
                self.log_message.emit(f"Successfully flashed {partition_name}")
                self.progress.emit(f"Successfully flashed {partition_name}")
            
            # Step 3: Clean up the image from the device
            self.progress.emit(f"Cleaning up...")
            self.log_message.emit(f"Cleaning up temporary files...")
            rm_command = [
                adb_exe, *serial,
                'shell', 'su', '-c', f'rm /sdcard/{partition_name}.img'
            ]
            result = subprocess.run(rm_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=creationflags)
            if result.returncode != 0:
                self.log_message.emit(f"Failed to remove temporary file: {result.stderr.strip()}")
                self.progress.emit(f"Failed to clean up temporary file")
                # Not counting cleanup failures as a complete failure
            
            # Emit final status
            self.finished_with_status.emit(partition_success)
            
        except Exception as e:
            self.log_message.emit(f"Error during flash operation: {str(e)}")
            self.progress.emit(f"Error during flash operation")
            self.finished_with_status.emit(False)


class PartitionManager(QMainWindow):
    def __init__(self, platform_tools_path):
        super().__init__()
        
        self.platform_tools_path = platform_tools_path
        self.partitions_data = []  # To store complete partition data
        self.selected_partitions = []  # To store selected partitions for operations
        
        self.setWindowTitle("QuickADB Partition Manager")
        self.setMinimumSize(1000, 700)
        
        ThemeManager.apply_theme(self)
        self.init_ui()
        self.load_partitions()


         
    
    
    def init_ui(self):
        central_widget = QWidget()
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(8)
        
        # Header section
        header_label = QLabel("Android Partition Manager")
        header_font = QFont()
        header_font.setPointSize(16)
        header_font.setBold(True)
        header_label.setFont(header_font)
        main_layout.addWidget(header_label)
        
        # Refresh button
        self.refresh_button = QPushButton("Refresh Partitions")
        self.refresh_button.setIcon(QApplication.style().standardIcon(QApplication.style().StandardPixmap.SP_BrowserReload))
        self.refresh_button.clicked.connect(self.load_partitions)
        self.refresh_button.setFixedWidth(150)
        
        refresh_layout = QHBoxLayout()
        refresh_layout.addStretch()
        refresh_layout.addWidget(self.refresh_button)
        main_layout.addLayout(refresh_layout)
        
        # Tree view for partitions
        self.model = QStandardItemModel(0, 5)
        self.model.setHorizontalHeaderLabels(["Select", "Partition Name", "Path", "Permissions", "Size"])
        
        self.tree_view = QTreeView()
        self.tree_view.setModel(self.model)
        self.tree_view.setAlternatingRowColors(True)
        self.tree_view.setAllColumnsShowFocus(True)
        self.tree_view.setUniformRowHeights(True)
        self.tree_view.header().setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tree_view.setItemDelegate(QStyledItemDelegate())
        
        # Set column widths and make them non-resizable
        self.tree_view.setColumnWidth(0, 60)    # Checkbox column
        self.tree_view.setColumnWidth(1, 150)   # Partition Name
        self.tree_view.setColumnWidth(2, 300)   # Path
        self.tree_view.setColumnWidth(3, 150)   # Permissions
        self.tree_view.setColumnWidth(4, 100)   # Size
        
        # Make columns fixed width (non-resizable)
        for i in range(5):
            self.tree_view.header().setSectionResizeMode(i, QHeaderView.ResizeMode.Fixed)
        
        self.tree_view.setSortingEnabled(True)
        self.tree_view.clicked.connect(self.toggle_selection)
        self.tree_view.setSelectionMode(QTreeView.SelectionMode.ExtendedSelection)
        
        main_layout.addWidget(self.tree_view)
        
        # Button layout
        button_layout = QHBoxLayout()
        
        self.toggle_partition_selection_button = QPushButton("Select / Unselect All")
        self.toggle_partition_selection_button.clicked.connect(self.toggle_partition_selection)
        
        
        self.pull_button = QPushButton("Pull Selected Partitions")
        self.pull_button.clicked.connect(self.pull_partitions)

        self.flash_button = QPushButton("Flash Image to Partition")
        self.flash_button.clicked.connect(self.flash_partition)
        
        button_layout.addWidget(self.toggle_partition_selection_button)
        button_layout.addStretch()
        button_layout.addWidget(self.pull_button)
        button_layout.addWidget(self.flash_button)
        
        main_layout.addLayout(button_layout)
        
        # Log window
        self.log_window = QTextEdit()
        self.log_window.setReadOnly(True)
        self.log_window.setMaximumHeight(150)
        
        self.log_label = QLabel("Operation Log:")
        log_font = QFont()
        log_font.setBold(True)
        self.log_label.setFont(log_font)
        
        main_layout.addWidget(self.log_label)
        main_layout.addWidget(self.log_window)
        
        # Status bar
        self.statusBar().showMessage("Ready")
        
        self.setCentralWidget(central_widget)
    
    def toggle_partition_selection(self):
        """Toggle selection of all partitions: select if any are unselected, deselect otherwise."""
        select_all = any(
            self.model.item(row, 0).checkState() != Qt.CheckState.Checked
            for row in range(self.model.rowCount())
        )
    
        self.selected_partitions = []
    
        for row in range(self.model.rowCount()):
            checkbox_item = self.model.item(row, 0)
            if select_all:
                checkbox_item.setCheckState(Qt.CheckState.Checked)
                # Store data in list, retrieving it from the item's UserRole
                partition_info = checkbox_item.data(Qt.ItemDataRole.UserRole)
                if partition_info:
                    self.selected_partitions.append(partition_info)
            else:
                checkbox_item.setCheckState(Qt.CheckState.Unchecked)
    
        if select_all:
            self.log_message(f"Selected all {len(self.selected_partitions)} partitions")
            self.statusBar().showMessage(f"{len(self.selected_partitions)} partitions selected")
        else:
            self.log_message("Deselected all partitions")
            self.statusBar().showMessage("No partitions selected")
    

    def update_progress(self, message):
        """Update status bar and log label with pull progress."""
        self.statusBar().showMessage(message)
        if hasattr(self, 'log_label'):
            self.log_label.setText(f"Operation Log: {message}")
    
    def set_ui_enabled(self, state: bool):
        """Helper to toggle main UI elements during long operations."""
        self.refresh_button.setEnabled(state)
        self.tree_view.setEnabled(state)
        self.toggle_partition_selection_button.setEnabled(state)
        self.pull_button.setEnabled(state)
        self.flash_button.setEnabled(state)

    def load_partitions(self):
        """Start loading partitions in a worker thread."""
        self.set_ui_enabled(False)
        
        # Status updates
        self.statusBar().showMessage("Loading partitions...")
        
        # Create progress dialog
        self.loading_progress = QProgressDialog("Loading partitions... This may take a while.", "Cancel", 0, 100, self)
        self.loading_progress.setWindowTitle("Loading Partitions")
        self.loading_progress.setMinimumWidth(400)
        self.loading_progress.setCancelButton(None)  # Disable cancel button
        self.loading_progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.loading_progress.setAutoClose(True)
        self.loading_progress.setValue(0)
        self.loading_progress.show()
        
        # Create and start the worker thread
        self.loader_worker = PartitionLoadWorker(self.platform_tools_path)
        self.loader_worker.partitions_loaded.connect(self.on_partitions_loaded)
        self.loader_worker.log_message.connect(self.log_message)
        self.loader_worker.progress_update.connect(self.update_loading_progress)
        self.loader_worker.finished_loading.connect(self.on_loading_finished)
        self.loader_worker.error_occurred.connect(self.on_loading_error)
        self.loader_worker.start()
    
    def update_loading_progress(self, current, total):
        """Update the loading progress dialog."""
        if hasattr(self, 'loading_progress') and self.loading_progress is not None:
            progress_percent = int((current / total) * 100) if total > 0 else 0
            self.loading_progress.setValue(progress_percent)
            self.loading_progress.setLabelText(f"Loading partitions... {current}/{total} ({progress_percent}%)")
    
    def on_partitions_loaded(self, partitions_data):
        """Handle the loaded partitions data."""
        self.partitions_data = partitions_data
        self.selected_partitions = []  # Clear selected partitions
        
        # Update model with loaded data
        self.model.removeRows(0, self.model.rowCount())  # Clear existing items
        
        for partition_info in self.partitions_data:
            # Create checkbox item with a space as text - this ensures proper sizing
            select_item = QStandardItem(" ")
            select_item.setCheckable(True)
            select_item.setCheckState(Qt.CheckState.Unchecked)
            select_item.setEditable(False)
            # Store the partition data in the item so sorting doesn't break selection
            select_item.setData(partition_info, Qt.ItemDataRole.UserRole)
            
            # Set text properties for other columns (unchanged)
            name_item = QStandardItem(partition_info['name'])
            name_item.setEditable(False)
            
            path_item = QStandardItem(partition_info['path'])
            path_item.setEditable(False)
            
            permissions_item = QStandardItem(partition_info['permissions'])
            permissions_item.setEditable(False)
            
            size_item = QStandardItem(partition_info['size_human'])
            size_item.setEditable(False)
            size_item.setData(partition_info['size_bytes'], Qt.ItemDataRole.UserRole)
            
            # Add items to model
            row_items = [select_item, name_item, path_item, permissions_item, size_item]
            self.model.appendRow(row_items)
        
        # Sort by partition name
        self.tree_view.sortByColumn(1, Qt.SortOrder.AscendingOrder)
    
    def on_loading_finished(self):
        """Handle the completion of the loading operation."""
        # Close progress dialog
        if hasattr(self, 'loading_progress') and self.loading_progress is not None:
            self.loading_progress.close()
        
        # Re-enable UI elements
        self.set_ui_enabled(True)
        
        # Update status

        self.statusBar().showMessage(f"Loaded {len(self.partitions_data)} partitions")
    
    def on_loading_error(self, error_message):
        """Handle loading errors."""
        # Close progress dialog
        if hasattr(self, 'loading_progress') and self.loading_progress is not None:
            self.loading_progress.close()
        
        # Show error message
        QMessageBox.critical(self, "Error", error_message)
        
        # Re-enable UI elements
        self.set_ui_enabled(True)
        
        # Update status
        self.statusBar().showMessage("Error loading partitions")
    
    def toggle_selection(self, index):
        """Toggle the selection state of a partition."""
        # Use the proxy index to get the actual model item (handles sorting)
        row = index.row()
        checkbox_item = self.model.item(row, 0)
        
        if not checkbox_item:
            return
            
        # Toggle checkbox state regardless of which column was clicked
        new_state = Qt.CheckState.Unchecked if checkbox_item.checkState() == Qt.CheckState.Checked else Qt.CheckState.Checked
        checkbox_item.setCheckState(new_state)
        
        # Update the selection list based on the new state
        self.update_selection_from_checkbox(checkbox_item)
    
    def update_selection_from_checkbox(self, checkbox_item):
        """Update selected_partitions list based on checkbox state from the item itself."""
        partition_info = checkbox_item.data(Qt.ItemDataRole.UserRole)
        if not partition_info:
            return
            
        if checkbox_item.checkState() == Qt.CheckState.Checked:
            # Only add if not already in the list
            if not any(p['name'] == partition_info['name'] for p in self.selected_partitions):
                self.selected_partitions.append(partition_info)
                self.log_message(f"Selected partition: {partition_info['name']}")
        else:
            # Remove from selected partitions
            self.selected_partitions = [p for p in self.selected_partitions if p['name'] != partition_info['name']]
            self.log_message(f"Deselected partition: {partition_info['name']}")
        
        # Update status bar
        self.statusBar().showMessage(f"{len(self.selected_partitions)} partitions selected")
    
    def pull_partitions(self):
        """Pull selected partitions to the user's system."""
        if not self.selected_partitions:
            QMessageBox.warning(self, "No Selection", "No partitions selected for pulling.")
            return
        
        save_dir = QFileDialog.getExistingDirectory(self, "Select Directory to Save Partitions")
        if not save_dir:
            return
        
        # Start the pull operation in a separate thread
        self.worker = PartitionPullWorker(self.platform_tools_path, self.selected_partitions, save_dir)
        self.worker.log_message.connect(self.log_message)
        self.worker.progress.connect(self.update_progress)
        self.worker.finished_with_status.connect(self.pull_finished) 
        
        # Disable UI elements during operation
        self.set_ui_enabled(False)
        self.worker.start()

    def flash_partition(self):
        """Flash an image to a selected partition."""
        # Safety check: ensure only one partition is selected
        if len(self.selected_partitions) == 0:
            QMessageBox.warning(self, "No Selection", "No partition selected for flashing.")
            return
        elif len(self.selected_partitions) > 1:
            QMessageBox.critical(self, "Multiple Selections", 
                                "For safety reasons, you can only flash one partition at a time. "
                                "Please select only one partition.")
            return
        
        # Get the partition to flash
        partition_info = self.selected_partitions[0]
        partition_name = partition_info['name']
        
        # Show warning dialog with partition details
        warning_msg = (f"WARNING: You are about to flash the '{partition_name}' partition!\n\n"
                      f"This operation will OVERWRITE the existing partition data and cannot be undone.\n"
                      f"Flashing incorrect data or interrupting the process may lead to system instability "
                      f"or brick your device.\n\n"
                      f"Are you absolutely sure you want to continue?")
        
        reply = QMessageBox.warning(self, "Flash Partition - DANGER", warning_msg, 
                                 QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, 
                                 QMessageBox.StandardButton.No)
        
        if reply != QMessageBox.StandardButton.Yes:
            return
        
        # Select the image file to flash
        image_path, _ = QFileDialog.getOpenFileName(self, f"Select Image File for {partition_name}", "", 
                                                 "Image Files (*.img);;All Files (*)")
        if not image_path:
            return
        
        # Create and configure progress dialog
        self.progress_dialog = QProgressDialog("Preparing to flash partition...", "Cancel", 0, 100, self)
        self.progress_dialog.setWindowTitle(f"Flashing {partition_name}")
        self.progress_dialog.setCancelButton(None)  # Disable cancel button for safety
        self.progress_dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.progress_dialog.setMinimumWidth(400)
        self.progress_dialog.show()
        
        # Start the flash operation in a separate thread
        self.flash_worker = PartitionFlashWorker(self.platform_tools_path, partition_info, image_path)
        self.flash_worker.log_message.connect(self.log_message)
        self.flash_worker.progress.connect(self.update_progress)
        self.flash_worker.finished_with_status.connect(self.flash_finished)
        
        # Disable UI elements during operation
        self.set_ui_enabled(False)
        
        self.flash_worker.start()        

    def flash_finished(self, success):
        """Handle the completion of the flash operation."""
        if hasattr(self, 'progress_dialog') and self.progress_dialog is not None:
            self.progress_dialog.close()
        
        # Re-enable UI elements
        self.set_ui_enabled(True)
        
        partition_name = self.selected_partitions[0]['name'] if self.selected_partitions else "Unknown"
        
        # Show appropriate message based on success status
        if success:
            self.log_message(f"Partition flash operation completed successfully for {partition_name}.")
            self.statusBar().showMessage(f"Flash operation completed successfully for {partition_name}")
            QMessageBox.information(self, "Flash Complete", 
                                 f"Successfully flashed {partition_name} partition.")
        else:
            self.log_message(f"Partition flash operation failed for {partition_name}.")
            self.statusBar().showMessage(f"Flash operation failed for {partition_name}")
            QMessageBox.critical(self, "Flash Failed", 
                              f"Failed to flash {partition_name} partition. Check logs for details.")
        
        # Reset log label
        self.log_label.setText("Operation Log:")
            
                    
    
    def log_message(self, message):
        current_time = datetime.datetime.now().strftime("%H:%M:%S")
        formatted_message = f"[{current_time}] {message}"
        self.log_window.append(formatted_message)
        # Auto-scroll to the bottom
        self.log_window.verticalScrollBar().setValue(
            self.log_window.verticalScrollBar().maximum()
        )
        
    
    def pull_finished(self, success, success_count, total_count):
        """Handle the completion of the pull operation."""
        if hasattr(self, 'progress_dialog') and self.progress_dialog is not None:
            self.progress_dialog.close()
        
        # Re-enable UI elements
        self.set_ui_enabled(True)
        
        # Update status and show appropriate message based on success status
        if success and success_count == total_count:
            self.log_message(f"Partition pull operation completed successfully: {success_count}/{total_count} partitions pulled.")
            self.statusBar().showMessage(f"Pull operation completed: {success_count}/{total_count} successful")
            QMessageBox.information(self, "Operation Complete", 
                                 f"Successfully pulled {success_count} out of {total_count} partitions.")
        elif success and success_count < total_count:
            self.log_message(f"Partition pull operation completed with partial success: {success_count}/{total_count} partitions pulled.")
            self.statusBar().showMessage(f"Pull operation completed: {success_count}/{total_count} successful")
            QMessageBox.warning(self, "Operation Partial Complete", 
                                 f"Pulled {success_count} out of {total_count} partitions. Some errors occurred.")
        else:
            self.log_message(f"Partition pull operation failed: 0/{total_count} partitions pulled.")
            self.statusBar().showMessage("Pull operation failed")
            QMessageBox.critical(self, "Operation Failed", 
                               "Failed to pull any partitions. Check logs for details.")
                               
        # Reset log label
        self.log_label.setText("Operation Log:")




def main(platform_tools_path):
    app = QApplication(sys.argv)

    window = PartitionManager(platform_tools_path)
    window.show()
    sys.exit(app.exec())
