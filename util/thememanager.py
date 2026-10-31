'''

thememanager.py - Simple theme manager for all QuickADB modules. Looks for all .qss files in the set directory and saves the selected
theme's name in a temp file. The selected theme will then be applied to any PyQt6 widget when the theme manager is called.
Defaults to dark.qss if no temp file exists or if the current temp file contains a name that doesn't exist.

Also applies the icon to every window. Since this is not a QuickADB-specific module, it could be adapted to any PyQt6 UI.

'''



import os
import tempfile
from PyQt6.QtWidgets import QWidget
from PyQt6.QtGui import QIcon

def _get_config_file() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    target_dir = os.path.join(base, "QuickADB")
    try:
        os.makedirs(target_dir, exist_ok=True)
        return os.path.join(target_dir, "quickadb_theme_name")
    except OSError:
        return os.path.join(tempfile.gettempdir(), "quickadb_theme_name")


def resolve_theme_path(name: str) -> str:
    from util.resource import resource_path
    name = os.path.basename(name)
    if not name or name in (".", ".."):
        name = "dark.qss"
    return resource_path(os.path.join("themes", name))


class ThemeManager:
    CONFIG_FILE = _get_config_file()
    TEXT_COLOR_PRIMARY = "#ffffff"
    TEXT_COLOR_SECONDARY = "#8b949e"

    @classmethod
    def get_config_path(cls):
        return getattr(cls, "CONFIG_FILE", None) or _get_config_file()

    @classmethod
    def ensure_default(cls):
        config_path = cls.get_config_path()
        if not os.path.exists(config_path):
            try:
                with open(config_path, "w") as f:
                    f.write("dark.qss")
            except OSError:
                pass

    @classmethod
    def read_theme_name(cls):
        cls.ensure_default()
        try:
            with open(cls.get_config_path(), "r") as f:
                content = f.read().strip()
                return content if content else "dark.qss"
        except OSError:
            return "dark.qss"

    @classmethod
    def write_theme_name(cls, name):
        try:
            with open(cls.get_config_path(), "w") as f:
                f.write(name)
        except OSError:
            pass

    @classmethod
    def _set_text_colors_for_theme(cls, theme_name: str):
        name = (theme_name or "").strip().lower()
        if name in ("none", "light.qss"):
            cls.TEXT_COLOR_PRIMARY = "#1f2328"
            cls.TEXT_COLOR_SECONDARY = "#606060"
        elif name == "high_contrast.qss":
            cls.TEXT_COLOR_PRIMARY = "#ffffff"
            cls.TEXT_COLOR_SECONDARY = "#aaaaaa"
        elif name == "test.qss":
            cls.TEXT_COLOR_PRIMARY = "#333333"
            cls.TEXT_COLOR_SECONDARY = "#8a8a83"
        else:
            cls.TEXT_COLOR_PRIMARY = "#ffffff"
            cls.TEXT_COLOR_SECONDARY = "#8b949e"

    @classmethod
    def _is_hex_color(cls, value: str) -> bool:
        if not isinstance(value, str):
            return False
        text = value.strip()
        if not text.startswith("#"):
            return False
        hex_part = text[1:]
        if len(hex_part) not in (6, 8):
            return False
        return all(ch in "0123456789abcdefABCDEF" for ch in hex_part)

    @classmethod
    def _extract_metadata_text_colors(cls, qss_content: str):
        primary = None
        secondary = None
        for raw_line in qss_content.splitlines():
            line = raw_line.strip().lstrip("*").strip()
            if not line:
                continue
            line_lower = line.lower()
            if line_lower.startswith("quickadb_log_primary:"):
                value = line.split(":", 1)[1].split("//", 1)[0].replace("*/", "").strip()
                if cls._is_hex_color(value):
                    primary = value
            elif line_lower.startswith("quickadb_log_secondary:"):
                value = line.split(":", 1)[1].split("//", 1)[0].replace("*/", "").strip()
                if cls._is_hex_color(value):
                    secondary = value
        if primary is None:
            return None
        if secondary is None:
            secondary = cls.TEXT_COLOR_SECONDARY
        return primary, secondary

    @classmethod
    def load_qss(cls, widget: QWidget, qss_path: str):
        theme_name = os.path.basename(qss_path)
        with open(qss_path, "r", encoding="utf-8") as f:
            qss_content = f.read()
        metadata_colors = cls._extract_metadata_text_colors(qss_content)
        if metadata_colors:
            cls.TEXT_COLOR_PRIMARY, cls.TEXT_COLOR_SECONDARY = metadata_colors
        else:
            cls._set_text_colors_for_theme(theme_name)
        widget.setStyleSheet(qss_content)
        cls.write_theme_name(theme_name)

    @classmethod
    def apply_default(cls, widget: QWidget):
        cls._set_text_colors_for_theme("none")
        widget.setStyleSheet("")
        widget.style().polish(widget)
        for w in widget.findChildren(QWidget):
            w.style().polish(w)
        cls.write_theme_name("none")

    @classmethod
    def resolve_theme_path(cls, name: str) -> str:
        return resolve_theme_path(name)

    @classmethod
    def apply_theme(cls, widget: QWidget):
        cls.ensure_default()
        name = cls.read_theme_name()
        from util.resource import resource_path
        
        # Apply Global Icon
        icon_path = resource_path(os.path.join("res", "toolicon.ico"))
        if os.path.exists(icon_path):
            widget.setWindowIcon(QIcon(icon_path))

        if name == "none":
            cls.apply_default(widget)
            return
        cls._set_text_colors_for_theme(name)

        name = os.path.basename(name)
        path = resolve_theme_path(name)
        if not os.path.exists(path) or not os.path.isfile(path):
            name = "dark.qss"
            path = resolve_theme_path("dark.qss")
            cls._set_text_colors_for_theme(name)
        cls.load_qss(widget, path)
