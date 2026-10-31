"""

superdumper.py - Handles QuickADB's super.img dumper functionality.
Essentially, just a GUI for unsuper.py.

"""

import sys
import os

script_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(script_dir)
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from util.resource import get_root_dir, resource_path
root_dir = get_root_dir()
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from util.thememanager import ThemeManager
from util.unsuper import OptimizedLpUnpack
from pathlib import Path
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                           QHBoxLayout, QPushButton, QTextEdit, QFileDialog, 
                           QFrame, QMessageBox)
from PyQt6.QtCore import QObject, QThread, pyqtSignal, Qt, QSize
from PyQt6.QtGui import QFont


class LogSignal(QObject):
    """Signal class to handle logging across threads"""
    signal = pyqtSignal(str)


class ExtractionWorker(QThread):
    """Worker thread for running super.img extraction in-process via OptimizedLpUnpack."""
    log_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(bool, str)

    def __init__(self, super_img_path: str, output_dir: str):
        super().__init__()
        self.super_img_path = super_img_path
        self.output_dir = output_dir

    def run(self):
        try:
            unpacker = OptimizedLpUnpack(
                super_image=self.super_img_path,
                output_dir=self.output_dir,
                log_callback=self.log_signal.emit
            )
            return_code = unpacker.run()
            if return_code == 0:
                self.finished_signal.emit(True, f"Successfully extracted super.img to {self.output_dir}")
            else:
                self.finished_signal.emit(False, f"Extraction failed with exit code {return_code}")
        except Exception as e:
            self.finished_signal.emit(False, f"Extraction failed with error: {str(e)}")


class SuperImgDumperUI(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("QuickADB super.img dumper")
        self.setFixedSize(600, 450)

        # Setup instance variables
        self.super_img_path = None
        self.output_dir = None
        self.extraction_worker = None
        self.log_signal = LogSignal()
        self.log_signal.signal.connect(self.update_log)
        ThemeManager.apply_theme(self)
        
        # Create the central widget and main layout
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(10, 10, 10, 10)
        
        # Create and set up the log output area
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setFixedHeight(350)
        main_layout.addWidget(self.log_output)
        
        # Create button frame
        button_frame = QFrame()
        button_layout = QHBoxLayout(button_frame)
        button_layout.setContentsMargins(5, 5, 5, 5)
        button_layout.setSpacing(10)
        main_layout.addWidget(button_frame)
        
        # Create buttons
        self.super_img_button = QPushButton("Select Super.img")
        self.super_img_button.setFixedSize(QSize(150, 40))
        self.super_img_button.clicked.connect(self.select_super_img)
        
        self.output_dir_button = QPushButton("Select Output Folder")
        self.output_dir_button.setFixedSize(QSize(150, 40))
        self.output_dir_button.clicked.connect(self.select_output_folder)
        self.output_dir_button.setEnabled(False)
        
        self.extract_button = QPushButton("Dump Image Contents")
        self.extract_button.setFixedSize(QSize(150, 40))
        self.extract_button.clicked.connect(self.extract_super_img)
        self.extract_button.setEnabled(False)
        
        # Add buttons to layout
        button_layout.addWidget(self.super_img_button)
        button_layout.addWidget(self.output_dir_button)
        button_layout.addWidget(self.extract_button)
        
        # Log initial message
        self.log("This function is powered by unsuper.")
        self.log("https://github.com/codefl0w/unsuper")

    def log(self, message):
        """Thread-safe logging function that emits a signal for the GUI thread"""
        self.log_signal.signal.emit(message)
        
    def update_log(self, message):
        """Update the log widget with new text (called in the GUI thread)"""
        cursor = self.log_output.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(message + "\n")
        self.log_output.setTextCursor(cursor)
        self.log_output.ensureCursorVisible()
    
    def select_super_img(self):
        """Open file dialog to select the super.img file"""
        self.show()  # Need to refresh the window after changing flags
        
        file_path, _ = QFileDialog.getOpenFileName(
            self, 
            "Select Super.img File", 
            "", 
            "Image Files (*.img)"
        )
        
        self.show()
        
        if file_path:
            self.super_img_path = file_path
            self.log(f"[INFO] Super.img selected: {self.super_img_path}")
            self.output_dir_button.setEnabled(True)
        else:
            self.log("[WARNING] No super.img file selected.")
    
    def select_output_folder(self):
        """Open dialog to select output directory"""
        self.show()
        
        folder_path = QFileDialog.getExistingDirectory(
            self, 
            "Select Output Directory"
        )

        self.show()
        
        if folder_path:
            self.output_dir = folder_path
            self.log(f"[INFO] Output directory selected: {self.output_dir}")
            self.extract_button.setEnabled(True)
        else:
            self.log("[WARNING] No output directory selected.")
    
    def extract_super_img(self):
        """Extract the super.img file to the output directory"""
        if not self.super_img_path or not self.output_dir:
            QMessageBox.critical(
                self, 
                "Error", 
                "Please select both a super.img file and an output directory."
            )
            return
        
        # Verify file access and size
        try:
            file_size = os.path.getsize(self.super_img_path)
            self.log(f"[INFO] Super.img file size: {file_size} bytes")
            
            # Test reading the first few bytes to verify access
            with open(self.super_img_path, 'rb') as test_read:
                test_data = test_read.read(16)  # Just read a small chunk
                self.log(f"[INFO] File appears readable. First few bytes: {test_data.hex()[:24]}...")
        except Exception as e:
            self.log(f"[ERROR] File access error: {e}")
            QMessageBox.critical(self, "Error", f"Unable to access the super.img file: {e}")
            return
            
        self.log(f"[INFO] Extracting contents to {self.output_dir}")
        # Disable buttons during extraction
        self.super_img_button.setEnabled(False)
        self.output_dir_button.setEnabled(False)
        self.extract_button.setEnabled(False)
        
        # Start extraction in a worker thread
        self.extraction_worker = ExtractionWorker(self.super_img_path, self.output_dir)
        self.extraction_worker.log_signal.connect(self.update_log)
        self.extraction_worker.finished_signal.connect(self.extraction_finished)
        self.extraction_worker.start()

    def extraction_finished(self, success, message):
        """Handle completion of the extraction worker thread."""
        self.super_img_button.setEnabled(True)
        self.output_dir_button.setEnabled(True)
        self.extract_button.setEnabled(True)
        if success:
            self.log("[INFO] Extraction complete.")
            QMessageBox.information(self, "Success", message)
        else:
            self.log(f"[ERROR] {message}")
            QMessageBox.critical(self, "Error", message)


def show_super_img_dumper(parent=None):
    # Create the application if it doesn't exist
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
        standalone = True
    else:
        standalone = False
    
    # Create and show the window
    window = SuperImgDumperUI(parent)
    window.show()
    
    # Run the event loop if this is a standalone app
    if standalone:
        return app.exec()
    else:
        return window


# For standalone execution
if __name__ == "__main__":
    show_super_img_dumper()