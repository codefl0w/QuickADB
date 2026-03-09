"""

resource.py - Handles resource management for QuickADB.

"""

import sys
import os

def get_root_dir() -> str:
    """
    Returns the root directory of the application.
    If running as a frozen PyInstaller bundle, returns the temporary _MEIPASS folder.
    Otherwise, returns the absolute path of the directory containing this module's parent.
    """
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return sys._MEIPASS
    else:
        # Since this is util/resource.py, root is one directory up
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def resource_path(relative_path: str) -> str:
    """
    Get the absolute path to a resource, works for both development and PyInstaller.
    """
    return os.path.join(get_root_dir(), relative_path)

def resolve_platform_tool(platform_tools_path: str, tool_name: str) -> str:
    """
    Resolve adb/fastboot executable path in a cross-platform way.
    On Windows, prefer `.exe` when present.
    """
    base_path = os.path.join(platform_tools_path, tool_name)
    if os.name == "nt" and not tool_name.lower().endswith(".exe"):
        exe_path = base_path + ".exe"
        if os.path.exists(exe_path):
            return exe_path
        return exe_path
    return base_path

def get_clean_env():
    """
    Returns a copy of the environment with LD_LIBRARY_PATH restored to its original state.
    This is critical for AppImages when calling system binaries (like /bin/sh or xdg-open)
    to avoid "symbol lookup" errors caused by bundled libraries.
    """
    env = os.environ.copy()
    if getattr(sys, 'frozen', False):
        # PyInstaller saves the original LD_LIBRARY_PATH here
        lp_orig = env.get('LD_LIBRARY_PATH_ORIG')
        if lp_orig is not None:
            env['LD_LIBRARY_PATH'] = lp_orig
        else:
            env.pop('LD_LIBRARY_PATH', None)
    return env

def open_url_safe(url: str):
    """
    Opens a URL in the host's web browser while ensuring the environment is clean.
    """
    import webbrowser
    # We must modify os.environ directly for webbrowser.open as it doesn't take an env param
    lp_key = 'LD_LIBRARY_PATH'
    lp_orig_key = 'LD_LIBRARY_PATH_ORIG'
    old_lp = os.environ.get(lp_key)
    
    try:
        if getattr(sys, 'frozen', False):
            if lp_orig_key in os.environ:
                os.environ[lp_key] = os.environ[lp_orig_key]
            else:
                os.environ.pop(lp_key, None)
        webbrowser.open(url)
    finally:
        if old_lp is not None:
            os.environ[lp_key] = old_lp
        else:
            os.environ.pop(lp_key, None)
