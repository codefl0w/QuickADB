"""
devicemanager.py  –  Centralized multi-device ADB manager.

Singleton that parses `adb devices` and `fastboot devices`, resolves friendly product names (ADB only),
tracks the currently selected device, and provides serial args for
command injection.

Usage:
    from util.devicemanager import DeviceManager
    dm = DeviceManager.instance()
    dm.refresh()                                # parse `adb devices`
    dm.selected_serial = "192.168.1.100:5555"   # pick a device
    serial_args = dm.serial_args()              # ["-s", "192.168.1.100:5555"] or []
"""
import os
import subprocess
from typing import Optional, List, Dict

from util.toolpaths import ToolPaths

# States reported by `adb devices`
DEVICE_STATES = {"device", "unauthorized", "recovery", "offline", "authorizing", "no permissions"}

# Commands that should NOT have -s injected (they target the server, not a device)
ADB_GLOBAL_COMMANDS = frozenset({
    "devices", "kill-server", "start-server", "reconnect", "reconnect offline",
    "connect", "disconnect", "pair", "mdns",
})

FASTBOOT_GLOBAL_COMMANDS = frozenset({
    "devices", "help"
})


class DeviceManager:
    """Singleton that tracks connected devices and the user's selection."""

    _instance: Optional["DeviceManager"] = None

    def __init__(self):
        self.devices: List[Dict[str, str]] = []
        # [{serial: "abc123", state: "device", name: "Pixel 8"}, ...]
        self.selected_serial: Optional[str] = None

    @classmethod
    def instance(cls) -> "DeviceManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ---- public API ----

    def refresh(self) -> List[Dict[str, str]]:
        """Run `adb devices` and `fastboot devices`, parse results, resolve friendly names."""
        adb = ToolPaths.instance().adb
        raw_adb = self._run_silent([adb, "devices"])
        self.devices = self._parse_devices(raw_adb)

        # Resolve friendly product names for ADB devices
        for dev in self.devices:
            if dev["state"] == "device":
                dev["name"] = self._get_product_name(adb, dev["serial"])
            else:
                dev["name"] = dev["serial"]

        fastboot = ToolPaths.instance().fastboot
        raw_fb = self._run_silent([fastboot, "devices"])
        fb_devices = self._parse_fastboot_devices(raw_fb)

        # Only add fastboot devices that don't share a serial with a currently detected ADB device
        # which can happen on some recovery setups
        adb_serials = {d["serial"] for d in self.devices}
        for dev in fb_devices:
            if dev["serial"] not in adb_serials:
                self.devices.append(dev)

        # If selected serial is gone, reset
        known_serials = {d["serial"] for d in self.devices}
        if self.selected_serial and self.selected_serial not in known_serials:
            self.selected_serial = None
        # Auto-select if exactly one device
        if len(self.devices) == 1 and self.selected_serial is None:
            self.selected_serial = self.devices[0]["serial"]

        return self.devices

    def serial_args(self) -> List[str]:
        """Return ['-s', serial] if a device is selected, else []."""
        if self.selected_serial:
            return ["-s", self.selected_serial]
        return []

    def serial_flag(self) -> str:
        """Return '-s SERIAL ' string for injection into string-based commands, or ''."""
        if self.selected_serial:
            return f"-s {self.selected_serial} "
        return ""

    @staticmethod
    def is_global_adb_command(command_remainder: str) -> bool:
        """Return True if the ADB subcommand should NOT target a specific device."""
        first_token = command_remainder.strip().split()[0] if command_remainder.strip() else ""
        # Also check two-token combos like "reconnect offline"
        two_tokens = " ".join(command_remainder.strip().split()[:2])
        return first_token in ADB_GLOBAL_COMMANDS or two_tokens in ADB_GLOBAL_COMMANDS

    @staticmethod
    def is_global_fastboot_command(command_remainder: str) -> bool:
        """Return True if the fastboot subcommand should NOT target a specific device."""
        first_token = command_remainder.strip().split()[0] if command_remainder.strip() else ""
        return first_token in FASTBOOT_GLOBAL_COMMANDS

    # ---- internals ----

    @staticmethod
    def _parse_devices(raw_output: str) -> List[Dict[str, str]]:
        """Parse the output of `adb devices` into a list of device dicts."""
        devices = []
        for line in raw_output.splitlines():
            line = line.strip()
            if not line or line.startswith("List of devices") or line.startswith("*"):
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                serial = parts[0].strip()
                state = parts[1].strip()
                devices.append({"serial": serial, "state": state, "name": serial})
        return devices

    @staticmethod
    def _parse_fastboot_devices(raw_output: str) -> List[Dict[str, str]]:
        """Parse the output of `fastboot devices` into a list of device dicts."""
        devices = []
        for line in raw_output.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 2:
                serial = parts[0].strip()
                state = parts[1].strip().lower()
                if state in ("fastboot", "device", "recovery", "bootloader"):
                    if state in ("device", "bootloader"):
                        state = "fastboot" # Normalize state name for dropdown UI
                    devices.append({"serial": serial, "state": state, "name": serial})
        return devices

    @staticmethod
    def _get_product_name(adb: str, serial: str) -> str:
        """Query a device's friendly product name via getprop."""
        out = DeviceManager._run_silent(
            [adb, "-s", serial, "shell", "getprop", "ro.product.manufacturer", "&&", "getprop", "ro.product.model"]
        ).strip().replace("\n", " ")
        if out and "error" not in out.lower():
            return out
        return serial  # fallback to serial

    @staticmethod
    def _run_silent(cmd: list) -> str:
        """Run a command, return stdout, swallow errors."""
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10, creationflags=creationflags
            )
            return proc.stdout
        except Exception:
            return ""
