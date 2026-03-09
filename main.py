"""

main.py - The main entry point. Ensures proper packaging via PyInstaller and launches the app.

"""


import sys
import os

# Ensure the root directory and the util module are reachable
if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    root_dir = sys._MEIPASS
else:
    root_dir = os.path.dirname(os.path.abspath(__file__))

if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from PyQt6.QtWidgets import QApplication
from main.quickadb import QuickADBApp

def main():
    app = QApplication(sys.argv)
    main_window = QuickADBApp()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
