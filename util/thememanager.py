'''

Simple theme manager for all modules. Looks for all .qss files in the set directory and saves the selected
theme's name in a temp file. The selected theme will then be applied to any PyQt6 widget when the theme manager is called.
Defaults to dark.qss if no temp file exists or the current temp file contains a name that doesn't exist.

'''



import os
import tempfile
from PyQt6.QtWidgets import QWidget

class ThemeManager:
    CONFIG_FILE = os.path.join(tempfile.gettempdir(), "quickadb_theme_name")

    @classmethod
    def get_config_path(cls):
        return cls.CONFIG_FILE

    @classmethod
    def ensure_default(cls):
        if not os.path.exists(cls.CONFIG_FILE):
            with open(cls.CONFIG_FILE, "w") as f:
                f.write("dark.qss")

    @classmethod
    def read_theme_name(cls):
        with open(cls.CONFIG_FILE, "r") as f:
            return f.read().strip()

    @classmethod
    def write_theme_name(cls, name):
        with open(cls.CONFIG_FILE, "w") as f:
            f.write(name)

    @classmethod
    def load_qss(cls, widget: QWidget, qss_path: str):
        with open(qss_path, "r", encoding="utf-8") as f:
            widget.setStyleSheet(f.read())
        cls.write_theme_name(os.path.basename(qss_path))

    @classmethod
    def apply_classic(cls, widget: QWidget):
        widget.setStyleSheet("")
        widget.style().polish(widget)
        for w in widget.findChildren(QWidget):
            w.style().polish(w)
        cls.write_theme_name("none")

    @classmethod
    def apply_theme(cls, widget: QWidget):
        cls.ensure_default()
        name = cls.read_theme_name()
        root = os.path.dirname(os.path.dirname(__file__))
        if name == "none":
            cls.apply_classic(widget)
            return
        path = os.path.join(root, "themes", name)
        if not os.path.exists(path):
            path = os.path.join(root, "themes", "dark.qss")
            cls.write_theme_name("dark.qss")
        cls.load_qss(widget, path)
