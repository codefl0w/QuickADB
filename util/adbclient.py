"""
adbclient.py - Centralized ADB and Fastboot execution wrapper.

Provides a unified interface for invoking ADB and Fastboot commands, handling
tool paths, process creation flags, environment sanitization, and device serial targeting.
"""
import os
import sys
import subprocess
from typing import List, Optional, Union

from util.toolpaths import ToolPaths
from util.resource import get_clean_env

class ADBClient:

    _instance: Optional["ADBClient"] = None

    @classmethod
    def instance(cls) -> "ADBClient":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @staticmethod
    def get_creation_flags() -> int:
        # Hide blank cmd windows on command run on Windows. This was handled per-script before V5.3.0
        if sys.platform == "win32":
            return subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        return 0

    def build_command(
        self,
        args: List[str],
        tool: str = "adb",
        use_serial: bool = True
    ) -> List[str]:

        # Build the full command argument list including executable path and serial flag (if applicable)        
        tp = ToolPaths.instance()
        exe = tp.fastboot if tool == "fastboot" else tp.adb

        cmd = [exe]

        if use_serial:
            from util.devicemanager import DeviceManager
            dm = DeviceManager.instance()
            # Avoid injecting serial for global commands
            if tool == "adb":
                remainder = " ".join(args)
                if not dm.is_global_adb_command(remainder):
                    cmd.extend(dm.serial_args())
            elif tool == "fastboot":
                remainder = " ".join(args)
                if not dm.is_global_fastboot_command(remainder):
                    cmd.extend(dm.serial_args())

        cmd.extend(args)
        return cmd

    def run(
        self,
        args: List[str],
        tool: str = "adb",
        use_serial: bool = True,
        timeout: Optional[float] = None,
        text: bool = True,
        check: bool = False,
        cwd: Optional[str] = None,
        env: Optional[dict] = None
    ) -> subprocess.CompletedProcess:
        
        # Execute an ADB/Fastboot command synchronously and return CompletedProcess
        full_cmd = self.build_command(args, tool=tool, use_serial=use_serial)
        platform_tools_dir = cwd or ToolPaths.instance().platform_tools_dir

        return subprocess.run(
            full_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=self.get_creation_flags(),
            text=text,
            env=env or get_clean_env(),
            timeout=timeout,
            check=check,
            cwd=platform_tools_dir
        )

    def popen(
        self,
        args: List[str],
        tool: str = "adb",
        use_serial: bool = True,
        stdin=None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text: bool = True,
        bufsize: int = -1,
        cwd: Optional[str] = None,
        env: Optional[dict] = None
    ) -> subprocess.Popen:
        """Start an asynchronous ADB/Fastboot subprocess (e.g. streaming logcat, pull/push)."""
        full_cmd = self.build_command(args, tool=tool, use_serial=use_serial)
        platform_tools_dir = cwd or ToolPaths.instance().platform_tools_dir

        return subprocess.Popen(
            full_cmd,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            text=text,
            bufsize=bufsize,
            env=env or get_clean_env(),
            creationflags=self.get_creation_flags(),
            cwd=platform_tools_dir
        )

    def run_silent(
        self,
        args: List[str],
        tool: str = "adb",
        use_serial: bool = True,
        timeout: Optional[float] = 10.0
    ) -> str:
        # Run a command, return stripped stdout text, swallow exceptions
        try:
            res = self.run(args, tool=tool, use_serial=use_serial, timeout=timeout)
            return res.stdout if res.stdout else ""
        except Exception:
            return ""

    def run_shell(
        self,
        shell_cmd: Union[str, List[str]],
        use_serial: bool = True,
        timeout: Optional[float] = None
    ) -> subprocess.CompletedProcess:

        # Execute an 'adb shell' command cleanly 
        if isinstance(shell_cmd, str):
            args = ["shell", shell_cmd]
        else:
            args = ["shell"] + shell_cmd
        return self.run(args, tool="adb", use_serial=use_serial, timeout=timeout)

    def push(
        self,
        local_path: str,
        remote_path: str,
        compression: Optional[str] = None,
        use_serial: bool = True,
        timeout: Optional[float] = None
    ) -> subprocess.CompletedProcess:
        """Push a file or directory to device, with optional compression ('lz4', 'brotli', 'zstd', 'none')."""
        args = ["push"]
        if compression:
            args.extend(["-z", compression])
        args.extend([local_path, remote_path])
        return self.run(args, tool="adb", use_serial=use_serial, timeout=timeout)

    def pull(
        self,
        remote_path: str,
        local_path: str,
        compression: Optional[str] = None,
        use_serial: bool = True,
        timeout: Optional[float] = None
    ) -> subprocess.CompletedProcess:
        """Pull a file or directory from device, with optional compression ('lz4', 'brotli', 'zstd', 'none')."""
        args = ["pull"]
        if compression:
            args.extend(["-z", compression])
        args.extend([remote_path, local_path])
        return self.run(args, tool="adb", use_serial=use_serial, timeout=timeout)

    def reboot(
        self,
        target: str = "",
        tool: str = "adb",
        use_serial: bool = True,
        timeout: Optional[float] = None
    ) -> subprocess.CompletedProcess:
        """Reboot device (e.g. '', 'bootloader', 'recovery', 'fastboot', 'sideload')."""
        args = ["reboot", target] if target else ["reboot"]
        return self.run(args, tool=tool, use_serial=use_serial, timeout=timeout)

    def remount(
        self,
        use_serial: bool = True,
        timeout: Optional[float] = None
    ) -> subprocess.CompletedProcess:
        """Remount partitions as read-write."""
        return self.run(["remount"], tool="adb", use_serial=use_serial, timeout=timeout)

    def devices(
        self,
        tool: str = "adb",
        long: bool = False,
        timeout: Optional[float] = None
    ) -> subprocess.CompletedProcess:
        """List attached devices."""
        args = ["devices", "-l"] if long else ["devices"]
        return self.run(args, tool=tool, use_serial=False, timeout=timeout)

    def get_version(
        self,
        tool: str = "adb",
        timeout: Optional[float] = 5.0
    ) -> str:
        """Fetch version string of ADB or Fastboot."""
        cmd = ["--version"] if tool == "fastboot" else ["version"]
        try:
            res = self.run(cmd, tool=tool, use_serial=False, timeout=timeout)
            return res.stdout.splitlines()[0] if res.returncode == 0 and res.stdout else ""
        except Exception:
            return ""


