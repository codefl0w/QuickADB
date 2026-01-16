'''
View all device partitions by using "ls /dev/block/by-name". All listed partitions can be pulled from the device
as .img files for flashing. You can also write .img files to partition blocks, though it's not the best method to flash.
None of this can work without superuser rights for obvious reasons.

'''


from util.thememanager import ThemeManager
import sys
import os
import subprocess
import re
import datetime
import tempfile
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSize, QTimer
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QTreeView, QFrame, QFileDialog, QMessageBox, 
    QProgressDialog, QTextEdit, QHeaderView, QStyledItemDelegate
)
from PyQt6.QtGui import QFont, QStandardItemModel, QStandardItem, QColor

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
        
    def get_partition_size(self, partition_name):
        """Get the size of a partition in bytes."""
        try:
            command = [
                os.path.join(self.platform_tools_path, 'adb'), 
                'shell', 'su', '-c', 
                f'blockdev --getsize64 /dev/block/by-name/{partition_name}'
            ]
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            
            if result.returncode == 0:
                size = result.stdout.strip()
                return int(size) if size.isdigit() else 0
            else:
                return 0
        except Exception:
            return 0
    
    def run(self):
        """Load partitions from the device."""
        try:
            self.log_message.emit("Loading partitions from device...")
            
            command = [os.path.join(self.platform_tools_path, 'adb'), 'shell',
                        'ls -l /dev/block/by-name']
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            
            if result.returncode != 0:
                self.error_occurred.emit(f"Failed to load partitions:\n{result.stderr.strip()}")
                return
            
            lines = result.stdout.splitlines()
            
            if not lines or len(lines) <= 1:  # No partitions found or only the "total" line
                self.error_occurred.emit("No partitions found or device not accessible.")
                return
            
            lines = lines[1:]  # Skip the "total 0" line
            partitions_data = []  # Store partitions data
            
            total_partitions = len(lines)
            for current, line in enumerate(lines):
                parts = line.split()
                if len(parts) >= 9:  # Ensure we have enough parts
                    try:
                        # Extract permissions, partition name and path
                        permissions = parts[0]  # e.g., "lrwxrwxrwx"
                        partition_name = parts[-3]  # Name before the "->"
                        partition_path = parts[-1]  # Path after the "->"
                        
                        # Skip lines that don't match the expected format
                        if "lrwxrwxrwx" not in permissions or "->" not in line:
                            continue
                        
                        # Report progress before potentially long operation
                        self.progress_update.emit(current + 1, total_partitions)
                        self.log_message.emit(f"Getting size for partition: {partition_name}")
                        
                        # Get partition size (this may take some time)
                        size_bytes = self.get_partition_size(partition_name)
                        size_human = FormatSize.human_readable_size(size_bytes)
                        
                        partition_info = {
                            'name': partition_name,
                            'path': partition_path,
                            'permissions': permissions,
                            'size_bytes': size_bytes,
                            'size_human': size_human
                        }
                        
                        partitions_data.append(partition_info)
                        
                    except Exception as e:
                        self.log_message.emit(f"Error processing partition: {str(e)}")
            
            # Emit the loaded partitions
            self.partitions_loaded.emit(partitions_data)
            self.log_message.emit(f"Loaded {len(partitions_data)} partitions from device")
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
            current_partition = 1
            successful_pulls = 0
            failed_pulls = 0
            
            for partition_info in self.selected_partitions:
                partition_name = partition_info['name']
                partition_success = True
                
                # Step 1: Copy the partition to /sdcard
                self.progress.emit(f"Processing {current_partition}/{total_partitions}: Copying {partition_name} to device...")
                self.log_message.emit(f"Copying {partition_name} to device...")
                dd_command = [
                    os.path.join(self.platform_tools_path, 'adb'),
                    'shell', 'su', '-c',
                    f'dd if=/dev/block/by-name/{partition_name} of=/sdcard/{partition_name}.img'
                ]
                result = subprocess.run(dd_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if result.returncode != 0:
                    self.log_message.emit(f"Failed to copy {partition_name} to device: {result.stderr.strip()}")
                    self.progress.emit(f"Failed to copy {partition_name} to device")
                    current_partition += 1
                    failed_pulls += 1
                    partition_success = False
                    continue
                
                # Step 2: Pull the image from the device
                self.progress.emit(f"Processing {current_partition}/{total_partitions}: Pulling {partition_name} from device...")
                self.log_message.emit(f"Pulling {partition_name} from device...")
                pull_command = [
                    os.path.join(self.platform_tools_path, 'adb'),
                    'pull', f'/sdcard/{partition_name}.img', self.save_dir
                ]
                result = subprocess.run(pull_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if result.returncode != 0:
                    self.log_message.emit(f"Failed to pull {partition_name}: {result.stderr.strip()}")
                    self.progress.emit(f"Failed to pull {partition_name}")
                    current_partition += 1
                    failed_pulls += 1
                    partition_success = False
                    continue
                
                # Step 3: Remove the image from the device
                self.progress.emit(f"Processing {current_partition}/{total_partitions}: Cleaning up {partition_name} from device...")
                self.log_message.emit(f"Cleaning up {partition_name} from device...")
                rm_command = [
                    os.path.join(self.platform_tools_path, 'adb'),
                    'shell', 'su', '-c', f'rm /sdcard/{partition_name}.img'
                ]
                result = subprocess.run(rm_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if result.returncode != 0:
                    self.log_message.emit(f"Failed to remove {partition_name}.img from device: {result.stderr.strip()}")
                    self.progress.emit(f"Failed to clean up {partition_name}")
                    # Not counting cleanup failures as a complete failure
                
                if partition_success:
                    successful_pulls += 1
                    self.progress.emit(f"Processed {current_partition}/{total_partitions}: Pulled {partition_name} successfully")
                    self.log_message.emit(f"Pulled {partition_name} successfully")
                
                current_partition += 1
            
            # Emit final status - success flag is True if at least one partition was successfully pulled
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
            
            # Step 1: Push the image file to /sdcard
            self.progress.emit(f"Pushing image file to device...")
            self.log_message.emit(f"Pushing image for {partition_name} to device...")
            push_command = [
                os.path.join(self.platform_tools_path, 'adb'),
                'push', self.image_path, f'/sdcard/{partition_name}.img'
            ]
            result = subprocess.run(push_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if result.returncode != 0:
                self.log_message.emit(f"Failed to push image to device: {result.stderr.strip()}")
                self.progress.emit(f"Failed to push image to device")
                self.finished_with_status.emit(False)
                return
            
            # Step 2: Flash the image to the partition
            self.progress.emit(f"Flashing {partition_name}...")
            self.log_message.emit(f"Flashing {partition_name}...")
            dd_command = [
                os.path.join(self.platform_tools_path, 'adb'),
                'shell', 'su', '-c',
                f'dd if=/sdcard/{partition_name}.img of=/dev/block/by-name/{partition_name}'
            ]
            result = subprocess.run(dd_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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
                os.path.join(self.platform_tools_path, 'adb'),
                'shell', 'su', '-c', f'rm /sdcard/{partition_name}.img'
            ]
            result = subprocess.run(rm_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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
        
        self.setWindowTitle("Partition Manager")
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
        
        log_label = QLabel("Operation Log:")
        log_font = QFont()
        log_font.setBold(True)
        log_label.setFont(log_font)
        
        main_layout.addWidget(log_label)
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
                self.selected_partitions.append(self.partitions_data[row])
            else:
                checkbox_item.setCheckState(Qt.CheckState.Unchecked)
    
        if select_all:
            self.log_message(f"Selected all {len(self.selected_partitions)} partitions")
            self.statusBar().showMessage(f"{len(self.selected_partitions)} partitions selected")
        else:
            self.log_message("Deselected all partitions")
            self.statusBar().showMessage("No partitions selected")
    

    def update_progress(self, message):
        """Update progress dialog with status message."""
        if hasattr(self, 'progress_dialog') and self.progress_dialog is not None:
            # Extract numerical progress if possible
            match = re.search(r'Processing (\d+)/(\d+)', message)
            if match:
                current, total = int(match.group(1)), int(match.group(2))
                self.progress_dialog.setValue(current)
            
            # Update message
            self.progress_dialog.setLabelText(message)        
    
    def load_partitions(self):
        """Start loading partitions in a worker thread."""
        # Disable the UI elements
        self.refresh_button.setEnabled(False)
        self.tree_view.setEnabled(False)
        self.toggle_partition_selection_button.setEnabled(False)
        self.pull_button.setEnabled(False)
        self.flash_button.setEnabled(False)
        
        # Status updates
        self.log_message("Starting to load partitions from device...")
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
        self.refresh_button.setEnabled(True)
        self.tree_view.setEnabled(True)
        self.toggle_partition_selection_button.setEnabled(True)
        self.pull_button.setEnabled(True)
        self.flash_button.setEnabled(True)
        
        # Update status
        self.log_message(f"Loaded {len(self.partitions_data)} partitions from device")
        self.statusBar().showMessage(f"Loaded {len(self.partitions_data)} partitions")
    
    def on_loading_error(self, error_message):
        """Handle loading errors."""
        # Close progress dialog
        if hasattr(self, 'loading_progress') and self.loading_progress is not None:
            self.loading_progress.close()
        
        # Show error message
        QMessageBox.critical(self, "Error", error_message)
        
        # Re-enable UI elements
        self.refresh_button.setEnabled(True)
        self.tree_view.setEnabled(True)
        self.toggle_partition_selection_button.setEnabled(True)
        self.pull_button.setEnabled(True)
        self.flash_button.setEnabled(True)
        
        # Update status
        self.statusBar().showMessage("Error loading partitions")
    
    def toggle_selection(self, index):
        """Toggle the selection state of a partition."""
        row = index.row()
        if row < 0 or row >= len(self.partitions_data):
            return
        
        # Get the checkbox item
        checkbox_item = self.model.item(row, 0)
        
        # Toggle checkbox state regardless of which column was clicked
        new_state = Qt.CheckState.Unchecked if checkbox_item.checkState() == Qt.CheckState.Checked else Qt.CheckState.Checked
        checkbox_item.setCheckState(new_state)
        
        # Update the selection list based on the new state
        self.update_selection_from_checkbox(row)
    
    def update_selection_from_checkbox(self, row):
        """Update selected_partitions list based on checkbox state."""
        checkbox_item = self.model.item(row, 0)
        partition_info = self.partitions_data[row]
        
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
        
        # Create and configure progress dialog
        self.progress_dialog = QProgressDialog("Preparing to pull partitions...", "Cancel", 0, len(self.selected_partitions), self)
        self.progress_dialog.setWindowTitle("Pulling Partitions")
        self.progress_dialog.setCancelButton(None)  # Disable cancel button for simplicity
        self.progress_dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.progress_dialog.setMinimumWidth(400)
        self.progress_dialog.show()
        
        # Disable UI elements during operation
        self.pull_button.setEnabled(False)
        self.toggle_partition_selection_button.setEnabled(False)
        self.tree_view.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.flash_button.setEnabled(False)
        
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
        self.pull_button.setEnabled(False)
        self.flash_button.setEnabled(False)
        self.toggle_partition_selection_button.setEnabled(False)
        self.tree_view.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.flash_button.setEnabled(False)
        
        self.flash_worker.start()        

    def flash_finished(self, success):
        """Handle the completion of the flash operation."""
        if hasattr(self, 'progress_dialog') and self.progress_dialog is not None:
            self.progress_dialog.close()
        
        # Re-enable UI elements
        self.pull_button.setEnabled(True)
        self.flash_button.setEnabled(True)
        self.toggle_partition_selection_button.setEnabled(True)
        self.tree_view.setEnabled(True)
        self.refresh_button.setEnabled(True)
        self.flash_button.setEnabled(True)
        
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
        self.pull_button.setEnabled(True)
        self.toggle_partition_selection_button.setEnabled(True)
        self.tree_view.setEnabled(True)
        self.refresh_button.setEnabled(True)
        self.flash_button.setEnabled(True)
        
        # Update status and show appropriate message based on success status
        if success and success_count == total_count:
            self.log_message(f"Partition pull operation completed successfully: {success_count}/{total_count} partitions pulled.")
            self.statusBar().showMessage(f"Pull operation completed: {success_count}/{total_count} successful")
            QMessageBox.information(self, "Operation Complete", 
                                 f"Successfully pulled {success_count} out of {total_count} partitions.")
        elif success and success_count < total_count:
            self.log_message(f"Partition pull operation completed with partial success: {success_count}/{total_count} partitions pulled.")
            self.statusBar().showMessage(f"Pull operation completed: {success_count}/{total_count} successful")
            QMessageBox.warning(self, "Operation Partially Complete", 
                             f"Pulled {success_count} out of {total_count} partitions. Some operations failed. Check logs for details.")
        else:
            self.log_message("Partition pull operation failed.")
            self.statusBar().showMessage("Pull operation failed")
            QMessageBox.critical(self, "Operation Failed", 
                              "Failed to pull partitions. Check logs for details.")




def main(platform_tools_path):
    app = QApplication(sys.argv)

    window = PartitionManager(platform_tools_path)
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()