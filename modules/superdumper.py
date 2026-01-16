import sys
import os

script_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(script_dir)
sys.path.insert(0, root_dir)

from util.thememanager import ThemeManager
import subprocess
import threading
from pathlib import Path
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                           QHBoxLayout, QPushButton, QTextEdit, QFileDialog, 
                           QFrame, QMessageBox)
from PyQt6.QtCore import QObject, pyqtSignal, Qt, QSize, QEvent
from PyQt6.QtGui import QFont


class LogSignal(QObject):
    """Signal class to handle logging across threads"""
    signal = pyqtSignal(str)


class SuperImgDumperUI(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("QuickADB super.img dumper")
        self.setFixedSize(600, 450)

        
        # Setup instance variables
        self.super_img_path = None
        self.output_dir = None
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
        
        # Start extraction in a separate thread
        extraction_thread = threading.Thread(target=self.run_extraction)
        extraction_thread.daemon = True
        extraction_thread.start()
    
    def run_extraction(self):
        """Run unsuper as a subprocess to extract the super.img contents"""
        try:
            # Find unsuper.py - assuming it's in the same directory as this script
            unsuper_path = os.path.join(root_dir, "util", "unsuper.py")
            
            # Check if unsuper.py exists
            if not os.path.exists(unsuper_path):
                self.log(f"[ERROR] unsuper.py not found at: {unsuper_path}")
                return
                

            
            
            # Direct command line approach 
            command = [
                sys.executable, 
                "-u",  # Force unbuffered output
                unsuper_path, 
                self.super_img_path, 
                self.output_dir
            ]
            
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # Line buffered
                cwd=script_dir  # Set working directory to script directory
            )
            
            # Real-time output processing - read stdout and stderr in real-time
            while True:
                # Check if process has terminated
                if process.poll() is not None:
                    break
                
                # Read stdout line by line
                stdout_line = process.stdout.readline()
                if stdout_line:
                    self.log(stdout_line.strip())
                
                # Read stderr line by line
                stderr_line = process.stderr.readline()
                if stderr_line:
                    self.log(f"[ERROR] {stderr_line.strip()}")
            
            # Get any remaining output after process ends
            remaining_stdout, remaining_stderr = process.communicate()
            if remaining_stdout:
                for line in remaining_stdout.splitlines():
                    if line.strip():
                        self.log(line.strip())
            if remaining_stderr:
                for line in remaining_stderr.splitlines():
                    if line.strip():
                        self.log(f"[ERROR] {line.strip()}")
            
            # Get return code
            return_code = process.returncode
            
            if return_code == 0:
                self.log("[INFO] Extraction complete.")
                QApplication.instance().postEvent(
                    self,
                    ShowSuccessMessageEvent(f"Successfully extracted super.img to {self.output_dir}")
                )
            else:
                self.log(f"[ERROR] Process exited with code {return_code}.")

        
        except Exception as e:
            import traceback
            error_details = traceback.format_exc()
            self.log(f"[ERROR] An unexpected error occurred: {str(e)}")
            self.log(f"[ERROR] Details: {error_details}")
        
        finally:
            # Re-enable buttons on the main thread
            QApplication.instance().postEvent(
                self, 
                ReenableButtonsEvent()
            )


class ReenableButtonsEvent(QEvent):
    """Custom event to safely re-enable buttons from a non-GUI thread"""
    EVENT_TYPE = QEvent.Type(QEvent.Type.User + 1)
    
    def __init__(self):
        super().__init__(self.EVENT_TYPE)


class ShowSuccessMessageEvent(QEvent):
    """Custom event to show success message on the main thread"""
    EVENT_TYPE = QEvent.Type(QEvent.Type.User + 2)
    
    def __init__(self, message):
        super().__init__(self.EVENT_TYPE)
        self.message = message


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


# Handle custom events for re-enabling buttons
def event(self, event):
    if event.type() == ReenableButtonsEvent.EVENT_TYPE:
        self.super_img_button.setEnabled(True)
        self.output_dir_button.setEnabled(True)
        self.extract_button.setEnabled(True)
        return True
    elif event.type() == ShowSuccessMessageEvent.EVENT_TYPE:
        QMessageBox.information(self, "Success", event.message)
        return True
    return super(SuperImgDumperUI, self).event(event)


# Add the custom event handler to the SuperImgDumperUI class
SuperImgDumperUI.event = event


# For standalone execution
if __name__ == "__main__":
    show_super_img_dumper()