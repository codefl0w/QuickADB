"""
toolpaths.py  –  Single source path for platform-tools executables.

Every module should import from here instead of resolving paths locally.

Usage:
    from util.toolpaths import ToolPaths
    tp = ToolPaths.instance()
    adb   = tp.adb        # absolute path to adb / adb.exe
    fb    = tp.fastboot    # absolute path to fastboot / fastboot.exe
    ptdir = tp.platform_tools_dir  # the directory itself
"""
from util.resource import resource_path, resolve_platform_tool


class ToolPaths:
    """Singleton that lazily resolves and caches platform-tool paths."""

    _instance = None

    def __init__(self):
        self._platform_tools_dir = resource_path("platform-tools")
        self._adb = None
        self._fastboot = None

    # --- singleton accessor ---
    @classmethod
    def instance(cls) -> "ToolPaths":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # --- public properties ---
    @property
    def platform_tools_dir(self) -> str:
        return self._platform_tools_dir

    @property
    def adb(self) -> str:
        if self._adb is None:
            self._adb = resolve_platform_tool(self._platform_tools_dir, "adb")
        return self._adb

    @property
    def fastboot(self) -> str:
        if self._fastboot is None:
            self._fastboot = resolve_platform_tool(self._platform_tools_dir, "fastboot")
        return self._fastboot
