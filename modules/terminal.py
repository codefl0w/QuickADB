import sys
import os

script_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(script_dir)
sys.path.insert(0, root_dir)

import time
import platform
import shutil
import signal
import io
import tempfile
import subprocess
from datetime import datetime

from PyQt6.QtWidgets import (QMainWindow, QPlainTextEdit, QLineEdit, QPushButton, 
                             QFrame, QGridLayout, QVBoxLayout, QFileDialog, QInputDialog, QApplication, QCompleter)

from PyQt6.QtCore import Qt, QObject, pyqtSignal, QThread, QStringListModel, QTimer
from PyQt6.QtGui import QFont, QTextCursor, QColor, QTextCharFormat, QTextDocument

from util.thememanager import ThemeManager

class OutputReader(QObject):
    output_received = pyqtSignal(str)
    finished = pyqtSignal()
    
    def __init__(self, process, is_windows=True):
        super().__init__()
        self.process = process
        self.is_windows = is_windows
        self.in_shell = False
        self._running = True

        self.shell_prompt_patterns = [
            r'shell@.*:.*[$#]\s*$',
            r'.*@.*:.*[$#]\s*$',
            r'C:\\.*>$',
            r'/.*[$#]\s*$',
        ]
        
    def stop(self):
        """Signal the reader to stop"""
        self._running = False
        
    def read_output(self):
        import re
        try:
            while self._running and self.process.poll() is None:
                line = self.process.stdout.readline()
                if line:
                    stripped_line = line.rstrip('\r\n')
                    
                    for pattern in self.shell_prompt_patterns:
                        if re.search(pattern, stripped_line):
                            self.in_shell = True
                            break
                    
                    if self.in_shell:
                        if stripped_line and not stripped_line.endswith(' '):
                            if any(stripped_line.endswith(char) for char in ['$', '#', '>']):
                                stripped_line += ' '
                    else:
                        if self.is_windows:
                            if stripped_line.endswith(">") and not stripped_line.endswith("> "):
                                stripped_line += " "
                        else:
                            if (stripped_line.endswith("$") or stripped_line.endswith("#")) and not stripped_line.endswith(" "):
                                stripped_line += " "
                    
                    self.output_received.emit(stripped_line + "\n")
                
                time.sleep(0.01)
            
            # Read remaining output
            if self._running:
                for line in self.process.stdout:
                    if line:
                        self.output_received.emit(line.rstrip('\r\n') + "\n")
                    
        except Exception as e:
            if self._running:
                self.output_received.emit(f"Error reading process output: {str(e)}\n")
        finally:
            self.finished.emit()


class CommandAutocompleter:
    def __init__(self):
        self.adb_commands = [
            "devices", "help", "version", "connect", "disconnect", "pair", 
            "forward", "reverse", "mdns", "push", "pull", "sync", "shell", "emu",
            "install", "install-multiple", "install-multi-package", "uninstall",
            "bugreport", "jdwp", "logcat", "disable-verity", "enable-verity", "keygen",
            "wait-for", "get-state", "get-serialno", "get-devpath", "remount", 
            "reboot", "sideload", "root", "unroot", "usb", "tcpip", "start-server",
            "kill-server", "reconnect", "attach", "detach"
        ]
        
        self.adb_subcommands = {
            "devices": ["-l"],
            "forward": ["--list", "--no-rebind", "--remove", "--remove-all"],
            "reverse": ["--list", "--no-rebind", "--remove", "--remove-all"],
            "mdns": ["check", "services"],
            "push": ["--sync", "-z", "-Z", "-n", "-q"],
            "pull": ["-a", "-z", "-Z", "-q"],
            "sync": ["-l", "-z", "-Z", "-n", "-q", "all", "data", "odm", "oem", 
                     "product", "system", "system_ext", "vendor"],
            "shell": ["-e", "-n", "-T", "-t", "-x"],
            "install": ["-l", "-r", "-t", "-s", "-d", "-g", "--instant", "--no-streaming", 
                        "--streaming", "--fastdeploy", "--no-fastdeploy"],
            "install-multiple": ["-l", "-r", "-t", "-s", "-d", "-p", "-g", "--instant"],
            "install-multi-package": ["-l", "-r", "-t", "-s", "-d", "-p", "-g", "--instant"],
            "uninstall": ["-k"],
            "remount": ["-R"],
            "reboot": ["bootloader", "recovery", "sideload", "sideload-auto-reboot"],
            "reconnect": ["device", "offline"]
        }
        
        self.fastboot_commands = [
            "update", "flashall", "flash", "devices", "getvar", "reboot",
            "flashing", "erase", "format", "set_active", "oem", "gsi", 
            "wipe-super", "create-logical-partition", "delete-logical-partition",
            "resize-logical-partition", "snapshot-update", "fetch", "boot", "--help", "-h"
        ]
        
        self.fastboot_subcommands = {
            "devices": ["-l"],
            "reboot": ["bootloader"],
            "flashing": ["lock", "unlock", "lock_critical", "unlock_critical", "get_unlock_ability"],
            "gsi": ["wipe", "disable", "status"],
            "snapshot-update": ["cancel", "merge"],
        }
        
        self.adb_global_options = [
            "-a", "-d", "-e", "-s", "-t", "-H", "-P", "-L", "--one-device", "--exit-on-write-error"
        ]
        
        self.fastboot_global_options = [
            "-w", "-s", "-S", "--force", "--slot", "--set-active", "--skip-secondary",
            "--skip-reboot", "--disable-verity", "--disable-verification",
            "--disable-super-optimization", "--disable-fastboot-info", "--fs-options",
            "--unbuffered", "--verbose", "-v", "--version", "--help", "-h"
        ]

    def get_matches(self, text):
        parts = text.split()
        
        if not text or text.isspace():
            return ["adb ", "fastboot "]
        
        if len(parts) == 1 and not text.endswith(" "):
            if "adb".startswith(parts[0].lower()):
                return ["adb "]
            elif "fastboot".startswith(parts[0].lower()):
                return ["fastboot "]
            return []
            
        if parts[0].lower() == "adb":
            return self._get_adb_matches(parts, text)
        elif parts[0].lower() == "fastboot":
            return self._get_fastboot_matches(parts, text)
            
        return []
    
    def _get_adb_matches(self, parts, text):
        if len(parts) == 1 or (len(parts) == 2 and not text.endswith(" ")):
            search_text = parts[1].lower() if len(parts) > 1 else ""
            matches = [cmd for cmd in self.adb_commands if cmd.startswith(search_text)]
            return [f"adb {match} " for match in matches]
            
        cmd = parts[1].lower()
        if cmd in self.adb_subcommands:
            if len(parts) == 2 or (len(parts) == 3 and not text.endswith(" ")):
                search_text = parts[2].lower() if len(parts) > 2 else ""
                if search_text.startswith("-"):
                    option_matches = [opt for opt in self.adb_global_options if opt.startswith(search_text)]
                    option_matches.extend([opt for opt in self.adb_subcommands[cmd] 
                                         if opt.startswith(search_text) and opt.startswith("-")])
                    return [f"adb {cmd} {match} " for match in option_matches]
                else:
                    subcmd_matches = [subcmd for subcmd in self.adb_subcommands[cmd] 
                                     if subcmd.startswith(search_text) and not subcmd.startswith("-")]
                    return [f"adb {cmd} {match} " for match in subcmd_matches]
        
        if parts[-1].startswith("-") and not text.endswith(" "):
            option_matches = [opt for opt in self.adb_global_options if opt.startswith(parts[-1])]
            prefix = " ".join(parts[:-1])
            return [f"{prefix} {match} " for match in option_matches]
            
        return []
    
    def _get_fastboot_matches(self, parts, text):
        if len(parts) == 1 or (len(parts) == 2 and not text.endswith(" ")):
            search_text = parts[1].lower() if len(parts) > 1 else ""
            matches = [cmd for cmd in self.fastboot_commands if cmd.startswith(search_text)]
            return [f"fastboot {match} " for match in matches]
            
        cmd = parts[1].lower()
        if cmd in self.fastboot_subcommands:
            if len(parts) == 2 or (len(parts) == 3 and not text.endswith(" ")):
                search_text = parts[2].lower() if len(parts) > 2 else ""
                if search_text.startswith("-"):
                    option_matches = [opt for opt in self.fastboot_global_options if opt.startswith(search_text)]
                    return [f"fastboot {cmd} {match} " for match in option_matches]
                else:
                    subcmd_matches = [subcmd for subcmd in self.fastboot_subcommands[cmd] 
                                     if subcmd.startswith(search_text)]
                    return [f"fastboot {cmd} {match} " for match in subcmd_matches]
        
        if parts[-1].startswith("-") and not text.endswith(" "):
            option_matches = [opt for opt in self.fastboot_global_options if opt.startswith(parts[-1])]
            prefix = " ".join(parts[:-1])
            return [f"{prefix} {match} " for match in option_matches]
            
        return []


class TerminalWindow(QMainWindow):
    def __init__(self, parent=None, app_version="BLANK", app_suffix="BLANK", 
                 adb_version=None, fastboot_version=None):
        super().__init__(parent)
        self.setWindowTitle("QuickADB Terminal")
        self.setGeometry(100, 100, 900, 500)
        self.setMinimumSize(900, 500)
        
        self.app_version = app_version
        self.app_suffix = app_suffix
        self.platform_tools_path = os.path.join(root_dir, 'platform-tools')
        
        self.system = platform.system()
        self.is_windows = self.system == "Windows"
        self.line_ending = "\r\n" if self.is_windows else "\n"
        
        # Initialize process-related attributes
        self.process = None
        self.reader = None
        self.reader_thread = None
        self.custom_rc_path = None

        ThemeManager.apply_theme(self)
        self.setup_ui()
        
        # Get versions
        self.get_binary_versions(adb_version, fastboot_version)
        
        # Command history
        self.command_history = []
        self.history_index = -1
        
        # Use QTimer to delay process start until after the window is shown
        QTimer.singleShot(100, self.start_cmd_process)

    def setup_ui(self):
        main_layout = QVBoxLayout()
        
        # Output text box
        self.output_text = QPlainTextEdit()
        self.output_text.setReadOnly(True)
        self.terminal_font = QFont("Consolas", 10)
        self.output_text.setFont(self.terminal_font)
        main_layout.addWidget(self.output_text)
        
        # Input entry box
        self.input_entry = QLineEdit()
        self.input_entry.setFont(self.terminal_font)
        self.input_entry.returnPressed.connect(self.send_command)
        self.input_entry.installEventFilter(self)
        main_layout.addWidget(self.input_entry)
        
        self.setup_autocomplete()
        
        # Button frame
        button_frame = QFrame()
        button_layout = QGridLayout(button_frame)
        
        buttons = [
            ("Clear Output", self.clear_output),
            ("Extract Output", self.extract_output),
            ("Switch to Terminal" if not self.is_windows else "Switch to CMD", self.open_terminal),
            ("Kill Process", self.kill_process),
            ("Help", self.help),
            ("Search", self.prompt_search)
        ]
        
        for i, (text, handler) in enumerate(buttons):
            btn = QPushButton(text)
            btn.clicked.connect(handler)
            button_layout.addWidget(btn, 0, i)
            button_layout.setColumnStretch(i, 1)
        
        main_layout.addWidget(button_frame)
        
        central_widget = QFrame()
        central_widget.setLayout(main_layout)
        self.setCentralWidget(central_widget)
        
        # Initialize highlight format
        self.highlight_format = QTextCharFormat()
        self.highlight_format.setBackground(QColor("#58a6ff"))
        self.highlight_format.setForeground(QColor("black"))

    def get_binary_versions(self, adb_version, fastboot_version):
        """Get ADB and Fastboot versions"""
        def get_version(command, fallback):
            if command:
                return command
            try:
                result = subprocess.run(
                    command.split() if isinstance(command, str) else command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8" if not self.is_windows else "latin-1",
                    errors="replace",
                    timeout=5
                )
                return result.stdout.strip() if result.returncode == 0 else fallback
            except:
                return fallback
        
        self.adb_version = get_version(adb_version or ["adb", "version"], 
                                       "ADB version information not available")
        self.fastboot_version = get_version(fastboot_version or ["fastboot", "--version"],
                                            "Fastboot version information not available")

    def setup_autocomplete(self):
        self.autocompleter = CommandAutocompleter()
        self.completer = QCompleter()
        self.completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        
        self.completion_model = QStringListModel()
        self.completer.setModel(self.completion_model)
        
        popup = self.completer.popup()
        if hasattr(self, "_active_theme_qss") and self._active_theme_qss:
            popup.setStyleSheet(self._active_theme_qss)
        
        self.input_entry.setCompleter(self.completer)
        self.input_entry.textChanged.connect(self.update_completions)
        self.completer.activated.connect(self.handle_completion)

    def handle_completion(self, text):
        self.input_entry.setText(text)
        self.input_entry.setCursorPosition(len(text))
        if text.endswith(" "):
            self.update_completions(text)

    def update_completions(self, text):
        if not text:
            return
        matches = self.autocompleter.get_matches(text)
        self.completion_model.setStringList(matches)
        if matches:
            self.completer.complete()


    def eventFilter(self, obj, event):
        if obj is self.input_entry and event.type() == event.Type.KeyPress:
            if event.key() == Qt.Key.Key_Up:
                self.previous_command()
                return True
            elif event.key() == Qt.Key.Key_Down:
                self.next_command()
                return True
        return super().eventFilter(obj, event)

    def cleanup_process_and_threads(self):
        """Properly cleanup process and threads"""
        # Stop the reader first
        if self.reader:
            self.reader.stop()
        
        # Disconnect signals to prevent any callbacks during cleanup
        if self.reader:
            try:
                self.reader.output_received.disconnect()
                self.reader.finished.disconnect()
            except:
                pass
        
        # Terminate the thread
        if self.reader_thread and self.reader_thread.isRunning():
            self.reader_thread.quit()
            if not self.reader_thread.wait(2000):  # Wait up to 2 seconds
                self.reader_thread.terminate()
                self.reader_thread.wait(100)
        
        # Kill the process
        if self.process and self.process.poll() is None:
            try:
                if self.is_windows:
                    subprocess.run(f"taskkill /F /PID {self.process.pid} /T", 
                                  shell=True, capture_output=True, timeout=2)
                else:
                    try:
                        os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                        time.sleep(0.2)
                        if self.process.poll() is None:
                            os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                    except (ProcessLookupError, OSError):
                        self.process.terminate()
                        time.sleep(0.2)
                        if self.process.poll() is None:
                            self.process.kill()
            except Exception:
                try:
                    self.process.kill()
                except:
                    pass
        
        # Clean up RC file
        if self.custom_rc_path and os.path.exists(self.custom_rc_path):
            try:
                os.remove(self.custom_rc_path)
            except:
                pass
        
        # Clear references
        self.process = None
        self.reader = None
        self.reader_thread = None

    def start_cmd_process(self):
        # Prevent multiple simultaneous starts
        if hasattr(self, '_starting_process') and self._starting_process:
            return
        
        self._starting_process = True
        
        try:
            # Clean up any existing process
            self.cleanup_process_and_threads()

            if not os.path.exists(self.platform_tools_path):
                self.log_output(f"Warning: Platform tools directory not found at {self.platform_tools_path}\n")
                self.platform_tools_path = os.getcwd()

            if self.system == "Windows":
                launch_cmd = ["cmd.exe", "/K"]
                encoding = "latin-1"
            elif self.system == "Linux":
                shell = shutil.which("bash") or shutil.which("sh")
                if not shell:
                    self.log_output("No shell found on Linux system.\n")
                    return
                encoding = "utf-8"
                self.custom_rc_path = os.path.join(self.platform_tools_path, ".quickadb_rc")
                with open(self.custom_rc_path, "w", encoding="utf-8") as f:
                    f.write("# Custom QuickADB RC\n")
                    f.write("export PS1='$ '\n")
                    f.write("stty -echo 2>/dev/null || true\n")
                launch_cmd = [shell, "--rcfile", self.custom_rc_path]
            elif self.system == "Darwin":
                launch_cmd = ["/bin/zsh", "-i"]
                encoding = "utf-8"
            else:
                self.log_output("Unsupported OS for terminal.\n")
                return

            env = os.environ.copy()
            if not self.is_windows:
                env['PS1'] = '$ '
                env['TERM'] = 'xterm-256color'
                env['PYTHONUNBUFFERED'] = '1'
                current_path = env.get('PATH', '')
                env['PATH'] = f".:{self.platform_tools_path}:{current_path}"

            self.process = subprocess.Popen(
                launch_cmd,
                cwd=self.platform_tools_path,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
                shell=False,
                bufsize=0,
                env=env if not self.is_windows else None,
                preexec_fn=os.setsid if not self.is_windows else None,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if self.is_windows else 0
            )

            self.process.stdout = io.TextIOWrapper(
                self.process.stdout, 
                encoding=encoding, 
                errors='replace',
                line_buffering=True
            )
            
            self.process.stdin = io.TextIOWrapper(
                self.process.stdin,
                encoding=encoding,
                errors='replace',
                line_buffering=True
            )

            self.reader = OutputReader(self.process, self.is_windows)
            self.reader.output_received.connect(self.log_output)

            self.reader_thread = QThread()
            self.reader.moveToThread(self.reader_thread)
            
            # Connect finished signal for cleanup
            self.reader.finished.connect(self.reader_thread.quit)
            self.reader_thread.started.connect(self.reader.read_output)
            self.reader_thread.start()
            
            # Log initial info
            self.log_output(f"QuickADB Version: {self.app_version} {self.app_suffix}\n")
            self.log_output(f"{self.adb_version}\n")
            self.log_output(f"{self.fastboot_version}\n")
            self.log_output(f"OS: {self.system}\n\n")

        except Exception as e:
            self.log_output(f"Error starting terminal process: {str(e)}\n")
        finally:
            self._starting_process = False

    def send_command(self):
        self.clear_highlight()
        command = self.input_entry.text().strip()
        
        if command:
            try:
                if command.lower() in ['exit', 'quit'] and hasattr(self.reader, 'in_shell') and self.reader.in_shell:
                    self.reader.in_shell = False
                
                if any(cmd in command.lower() for cmd in ['adb shell', 'fastboot shell', 'ssh', 'telnet']):
                    if hasattr(self.reader, 'in_shell'):
                        self.reader.in_shell = True
                
                try:
                    command_bytes = (command + self.line_ending).encode(
                        'latin-1' if self.is_windows else 'utf-8', 
                        errors='replace'
                    )
                    self.process.stdin.buffer.write(command_bytes)
                    self.process.stdin.buffer.flush()
                except (BrokenPipeError, OSError) as e:
                    self.log_output(f"Connection lost: {str(e)}\n")
                    self.log_output("Restarting terminal process...\n")
                    self.start_cmd_process()
                    return
                
                self.log_output(f"\n> {command}\n")
                self.command_history.append(command)
                self.history_index = len(self.command_history)
                
            except Exception as e:
                self.log_output(f"Error: Failed to send command. {str(e)}\n")
                if "stdin" in str(e).lower() or "broken pipe" in str(e).lower():
                    self.log_output("Restarting terminal process...\n")
                    self.start_cmd_process()
        
        self.input_entry.clear()

    def previous_command(self):

        if not self.command_history:

            return

            

        if self.history_index > 0:

            # Get the current command if we're on a valid index

            current_command = self.command_history[self.history_index] if self.history_index < len(self.command_history) else None

            

            # Move back one position initially

            self.history_index -= 1

            

            # Skip consecutive duplicates

            while self.history_index > 0 and self.command_history[self.history_index] == current_command:

                self.history_index -= 1

                

            # Set the text to the new command

            self.input_entry.setText(self.command_history[self.history_index])

            # Place cursor at the end of the text

            self.input_entry.setCursorPosition(len(self.input_entry.text()))

    

    def next_command(self):

        if not self.command_history:

            return

            

        if self.history_index < len(self.command_history) - 1:

            # Get the current command

            current_command = self.command_history[self.history_index]

            

            # Move forward one position initially

            self.history_index += 1

            

            # Skip consecutive duplicates

            while (self.history_index < len(self.command_history) - 1 and 

                   self.command_history[self.history_index] == current_command):

                self.history_index += 1

                

            # Set the text to the new command

            self.input_entry.setText(self.command_history[self.history_index])

            # Place cursor at the end of the text

            self.input_entry.setCursorPosition(len(self.input_entry.text()))

        elif self.history_index == len(self.command_history) - 1:

            # If at the last history item, move to empty command

            self.history_index += 1

            self.input_entry.clear()


        
        self.input_entry.setCursorPosition(len(self.input_entry.text()))

    def log_output(self, message: str):
        self.output_text.insertPlainText(message)
        self.output_text.moveCursor(QTextCursor.MoveOperation.End)
        self.output_text.ensureCursorVisible()

    def clear_output(self):
        self.output_text.clear()
        self.clear_highlight()
        self.log_output(f"QuickADB Version: {self.app_version} {self.app_suffix}\n")
        self.log_output(f"{self.adb_version}\n")
        self.log_output(f"{self.fastboot_version}\n")
        self.log_output(f"OS: {self.system}\n\n")

    def extract_output(self):
        try:
            current_time = datetime.now().strftime("%d/%m/%Y, %H:%M")
            extracted_output = self.output_text.toPlainText().strip()
            formatted_output = (
                f"{current_time} - Current version:\n"
                f"{self.app_version} {self.app_suffix}\n"
                f"=====================================\n\n"
                f"{extracted_output}\n\n"
                f"====================================="
            )
            
            file_path, _ = QFileDialog.getSaveFileName(
                self, "Save Extracted Output", "", "Text files (*.txt)"
            )
            
            if file_path:
                with open(file_path, "w", encoding="utf-8") as file:
                    file.write(formatted_output)
                self.log_output(f"Output successfully extracted as {file_path}.\n")
        except Exception as e:
            self.log_output(f"Error: Failed to extract output. {str(e)}\n")

    def kill_process(self):
        self.log_output("Terminating process...\n")
        self.cleanup_process_and_threads()
        self.log_output("Process terminated successfully.\n\n")
        # Restart after a short delay
        QTimer.singleShot(200, self.start_cmd_process)

    def help(self):
        self.log_output("This minimal terminal replicates regular terminal; you can navigate between older inputs with the arrow buttons.\n")
        self.log_output("Try using 'adb help' or 'fastboot help' to see their official documentations.\n")
        self.log_output("Use the 'Kill Process' button if the terminal becomes unresponsive.\n")
        self.log_output("You can drag and drop files onto the input box to easily insert file paths.\n\n")

    def prompt_search(self):
        keyword, ok = QInputDialog.getText(self, "Search", "Enter keyword to search:")
        if ok and keyword:
            self.search_text(keyword)
        else:
            self.clear_highlight()

    def search_text(self, keyword):
        self.clear_highlight()
        document = self.output_text.document()
        cursor = document.find(keyword, 0, QTextDocument.FindFlag.FindCaseSensitively)
        
        while not cursor.isNull():
            cursor.mergeCharFormat(self.highlight_format)
            cursor = document.find(keyword, cursor, QTextDocument.FindFlag.FindCaseSensitively)

    def clear_highlight(self):
        cursor = QTextCursor(self.output_text.document())
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.mergeCharFormat(QTextCharFormat())

    def open_terminal(self):
        try:
            if self.is_windows:
                subprocess.Popen(f"start cmd /K cd /D \"{self.platform_tools_path}\"", shell=True)
                self.log_output("Opened standard CMD in platform-tools directory.\n")
            elif self.system == "Linux":
                for terminal in ["gnome-terminal", "konsole", "xterm", "x-terminal-emulator"]:
                    if shutil.which(terminal):
                        if terminal == "gnome-terminal":
                            subprocess.Popen([terminal, f"--working-directory={self.platform_tools_path}"])
                        elif terminal == "konsole":
                            subprocess.Popen([terminal, f"--workdir={self.platform_tools_path}"])
                        else:
                            subprocess.Popen([terminal, "-e", f"bash -c 'cd \"{self.platform_tools_path}\"; bash'"])
                        self.log_output(f"Opened {terminal} in platform-tools directory.\n")
                        return
                self.log_output("No suitable terminal emulator found.\n")
            elif self.system == "Darwin":
                subprocess.Popen([
                    "osascript", "-e", 
                    f'tell application "Terminal" to do script "cd \\"{self.platform_tools_path}\\"; clear"'
                ])
                self.log_output("Opened Terminal in platform-tools directory.\n")
        except Exception as e:
            self.log_output(f"Error opening terminal: {str(e)}\n")

    def closeEvent(self, event):
        self.cleanup_process_and_threads()
        event.accept()
        super().closeEvent(event)

    # Drag and drop support
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            self.input_entry.setStyleSheet("border: 2px solid #58a6ff; border-radius: 4px; padding: 5px;")
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self.input_entry.setStyleSheet("")

    def dropEvent(self, event):
        self.input_entry.setStyleSheet("")
        file_paths = [f'"{url.toLocalFile()}"' for url in event.mimeData().urls()]
        current_text = self.input_entry.text()
        if current_text and not current_text.endswith(" "):
            current_text += " "
        self.input_entry.setText(current_text + " ".join(file_paths))
        self.input_entry.setFocus()
        self.input_entry.setCursorPosition(len(self.input_entry.text()))
        event.acceptProposedAction()


def show_terminal_window(app_version="Unknown", app_suffix="", 
                        adb_version=None, fastboot_version=None, parent_app=None):
    # Auto-detect if we're running standalone or embedded
    existing_app = QApplication.instance()
    
    if existing_app is None:
        # Running standalone - create new QApplication
        import sys
        app = QApplication(sys.argv)
        terminal = TerminalWindow(app_version=app_version, app_suffix=app_suffix,
                                 adb_version=adb_version, fastboot_version=fastboot_version)
        terminal.show()
        sys.exit(app.exec())
    else:
        # Running embedded - use existing QApplication
        terminal = TerminalWindow(app_version=app_version, app_suffix=app_suffix,
                                 adb_version=adb_version, fastboot_version=fastboot_version)
        terminal.show()
        return terminal


if __name__ == "__main__":
    show_terminal_window()