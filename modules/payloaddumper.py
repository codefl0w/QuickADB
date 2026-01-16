import sys
import os

script_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(script_dir)
sys.path.insert(0, root_dir)

import tempfile
import subprocess
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QPushButton, QLabel,
    QVBoxLayout, QHBoxLayout, QTextEdit, QFileDialog, QMessageBox,
    QFrame
)
from PyQt6.QtGui import QTextCursor, QColor
from PyQt6.QtCore import Qt, QThread, pyqtSignal


class PayloadDumperThread(QThread):
    output_signal = pyqtSignal(str)
    finished_signal = pyqtSignal(bool, str)
    
    def __init__(self, payload_bin_path, output_dir,):
        super().__init__()
        self.payload_bin_path = payload_bin_path
        self.output_dir = output_dir
        self.dumper_path =  os.path.join(root_dir, "util")
        
    def run(self):
        try:
            # Create the command for payload dumper
            if os.name == 'nt':  # Windows
                dumper_exec = os.path.join(self.dumper_path, "payload-dumper-go.exe")
            else:  # Linux/Mac
                dumper_exec = os.path.join(self.dumper_path, "payload-dumper-go")
            
            if not os.path.exists(dumper_exec):
                self.finished_signal.emit(False, f"Error: Payload dumper executable not found at {dumper_exec}")
                return
                
            command = [
                dumper_exec,
                "-output", self.output_dir,
                self.payload_bin_path
            ]
            
            # Run the process
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                universal_newlines=True,
                bufsize=1
            )
            
            # Process stdout in real-time
            for line in process.stdout:
                self.output_signal.emit(line.strip())
            
            # Process stderr in real-time
            for line in process.stderr:
                self.output_signal.emit(f"Error: {line.strip()}")
                
            # Wait for process to complete
            return_code = process.wait()
            
            if return_code == 0:
                self.finished_signal.emit(True, "Payload dumping completed successfully!")
            else:
                self.finished_signal.emit(False, f"Payload dumping failed with return code {return_code}")
                
        except Exception as e:
            self.finished_signal.emit(False, f"Error during payload dumping: {str(e)}")


class PayloadDumperApp(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # Initialize paths
        self.payload_bin_path = ""
        self.output_dir = ""
        self.script_dir = os.path.dirname(os.path.abspath(__file__))
        
        # Main window setup
        self.setWindowTitle("QuickADB payload.bin dumper")
        self.setMinimumSize(650, 500)
        self.setup_ui()
        self.check_and_apply_global_theme()
        
    def setup_ui(self):
        # Create central widget
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        # Main layout
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(15, 10, 15, 10)
        
        # Title label
        title_label = QLabel("Payload.bin Dumper")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_font = title_label.font()
        title_font.setPointSize(14)
        title_font.setBold(True)
        title_label.setFont(title_font)
        main_layout.addWidget(title_label)
        
        # Instructions label
        instructions_label = QLabel(
            "This is a graphical interface for ssut's payload-dumper-go tool to extract partitions from Android OTA payload.bin files. "
            "Select the payload.bin file and an output directory, then click 'Start Dumping'."
        )
        instructions_label.setWordWrap(True)
        instructions_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(instructions_label)
        
        # File selection frame
        file_frame = QFrame()
        file_frame.setFrameShape(QFrame.Shape.StyledPanel)
        file_layout = QVBoxLayout(file_frame)
        main_layout.addWidget(file_frame)
        
        # Payload.bin selection
        payload_layout = QHBoxLayout()
        payload_label = QLabel("Payload.bin:")
        self.payload_path_label = QLabel("Not selected")
        payload_button = QPushButton("Select File")
        payload_button.clicked.connect(self.select_payload_bin)
        
        payload_layout.addWidget(payload_label)
        payload_layout.addWidget(self.payload_path_label, 1)
        payload_layout.addWidget(payload_button)
        file_layout.addLayout(payload_layout)
        
        # Output directory selection
        output_layout = QHBoxLayout()
        output_label = QLabel("Output Directory:")
        self.output_dir_label = QLabel("Not selected")
        output_button = QPushButton("Select Directory")
        output_button.clicked.connect(self.select_output_dir)
        
        output_layout.addWidget(output_label)
        output_layout.addWidget(self.output_dir_label, 1)
        output_layout.addWidget(output_button)
        file_layout.addLayout(output_layout)
        
        # Start button
        self.start_button = QPushButton("Start Dumping")
        self.start_button.clicked.connect(self.start_dumping)
        self.start_button.setMinimumHeight(40)
        main_layout.addWidget(self.start_button)
        
        # Log output
        log_label = QLabel("Logs:")
        main_layout.addWidget(log_label)
        
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        main_layout.addWidget(self.log_output, 1)
        
        # Bottom buttons
        bottom_layout = QHBoxLayout()
        
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.close)
        
        self.clear_logs_button = QPushButton("Clear Logs")
        self.clear_logs_button.clicked.connect(self.clear_logs)
        
        bottom_layout.addWidget(self.clear_logs_button)
        bottom_layout.addStretch()
        bottom_layout.addWidget(self.close_button)
        
        main_layout.addLayout(bottom_layout)
        
    @staticmethod
    def get_theme_config_path():
        return os.path.join(tempfile.gettempdir(), "quickadb_theme_name")
    
    def check_and_apply_global_theme(self):
        config_path = self.get_theme_config_path()
        if not os.path.exists(config_path):
            with open(config_path, "w") as f:
                f.write("dark.qss")  # default theme
        
        with open(config_path, "r") as conf:
            value = conf.read().strip()
        
        if value == "none":
            self.apply_classic_theme()
        else:
            # Get absolute path to themes directory (go up one level from script location)
            script_dir = os.path.dirname(os.path.abspath(__file__))
            root_dir = os.path.dirname(script_dir)
            theme_path = os.path.join(root_dir, "themes", value)
            if os.path.exists(theme_path):
                self.load_theme_qss(theme_path)
            else:
                # fallback to dark.qss if the selected theme doesn't exist
                fallback = os.path.join(root_dir, "themes", "dark.qss")
                self.load_theme_qss(fallback)
                with open(config_path, "w") as f:
                    f.write("dark.qss")
    
    def load_theme_qss(self, path):
        with open(path, "r", encoding="utf-8") as f:
            self.setStyleSheet(f.read())
        
        # save theme name globally
        config_path = self.get_theme_config_path()
        with open(config_path, "w") as conf:
            conf.write(os.path.basename(path))
    
    def apply_classic_theme(self):
        # Use PyQt's default widgets
        self.setStyleSheet("")
        self.style().polish(self)
        for widget in self.findChildren(QWidget):
            widget.style().polish(widget)
        
        config_path = self.get_theme_config_path()
        with open(config_path, "w") as conf:
            conf.write("none")
    
    def apply_dark_theme(self):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        root_dir = os.path.dirname(script_dir)
        self.load_theme_qss(os.path.join(root_dir, "themes", "dark.qss"))                      
    
    def select_payload_bin(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, 
            "Select Payload.bin File",
            "",
            "Payload.bin Files (*.bin);;All Files (*)"
        )
        if file_path:
            self.payload_bin_path = file_path
            self.payload_path_label.setText(os.path.basename(file_path))
            self.log(f"Selected Payload.bin: {file_path}")
        
    def select_output_dir(self):
        dir_path = QFileDialog.getExistingDirectory(
            self,
            "Select Output Directory"
        )
        if dir_path:
            self.output_dir = dir_path
            self.output_dir_label.setText(os.path.basename(dir_path) or dir_path)
            self.log(f"Selected Output Directory: {dir_path}")
    
    def log(self, message, color=None):
        self.log_output.moveCursor(QTextCursor.MoveOperation.End)
        if color:
            self.log_output.setTextColor(QColor(color))
        self.log_output.insertPlainText(message + "\n")
        if color:
            # Reset to default text color
            self.log_output.setTextColor(QColor("#ffffff"))
        self.log_output.ensureCursorVisible()
    
    def clear_logs(self):
        self.log_output.clear()
    
    def start_dumping(self):
        if not self.payload_bin_path or not self.output_dir:
            QMessageBox.critical(self, "Error", "Please select both payload.bin file and output directory.")
            return
        
        self.log("Starting payload dumping process...", "#ff9100")
        self.start_button.setEnabled(False)
        
        # Create and start the dumper thread
        self.dumper_thread = PayloadDumperThread(self.payload_bin_path, self.output_dir)
        self.dumper_thread.output_signal.connect(self.log)
        self.dumper_thread.finished_signal.connect(self.dumping_finished)
        self.dumper_thread.start()
    
    def dumping_finished(self, success, message):
        if success:
            self.log(message, "#00ff00")  # Green color for success
        else:
            self.log(message, "#ff0000")  # Red color for error
            QMessageBox.critical(self, "Error", message)
        
        self.start_button.setEnabled(True)


def show_payload_dumper_window(parent_app=None):
    """
    Shows the payload dumper window.
    If parent_app is provided, it uses the existing QApplication instance.
    Otherwise, it creates a new one.
    
    Returns the app and window instance.
    """
    if parent_app is None:
        app = QApplication(sys.argv)
    else:
        app = parent_app
        
    window = PayloadDumperApp()
    window.show()
    
    # Only execute the app if called directly
    if parent_app is None:
        sys.exit(app.exec())
    
    return app, window


if __name__ == "__main__":
    show_payload_dumper_window()