"""
apkapi.py - Centralized APK and archive install backend for QuickADB.

Supports direct APK installs plus archive containers such as .zip, .xapk,
and .apkm when they contain one base.apk and optional split APKs.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from util.devicemanager import DeviceManager
from util.resource import get_clean_env
from util.toolpaths import ToolPaths


SUPPORTED_SINGLE_PACKAGE_EXTENSIONS = frozenset({".apk"})
SUPPORTED_ARCHIVE_EXTENSIONS = frozenset({".zip", ".xapk", ".apkm"})
SUPPORTED_PACKAGE_EXTENSIONS = SUPPORTED_SINGLE_PACKAGE_EXTENSIONS | SUPPORTED_ARCHIVE_EXTENSIONS

PACKAGE_DIALOG_FILTER = (
    "Android Packages (*.apk *.apkm *.xapk *.zip);;"
    "APK Files (*.apk);;"
    "Archive Packages (*.apkm *.xapk *.zip);;"
    "All Files (*)"
)


@dataclass(slots=True)
class APKInstallOptions:
    """Normalized install flags shared across QuickADB modules."""

    reinstall: bool = True
    allow_test: bool = False
    allow_downgrade: bool = False
    partial: bool = False
    grant_permissions: bool = False
    instant: bool = False
    no_streaming: bool = False
    streaming: bool = False
    abi: Optional[str] = None
    user: Optional[str] = None

    @classmethod
    def from_flag_dict(cls, flags: Optional[Mapping[str, object]]) -> "APKInstallOptions":
        flags = flags or {}
        return cls(
            reinstall=bool(flags.get("-r", True)),
            allow_test=bool(flags.get("-t", False)),
            allow_downgrade=bool(flags.get("-d", False)),
            partial=bool(flags.get("-p", False)),
            grant_permissions=bool(flags.get("-g", False)),
            instant=bool(flags.get("--instant", False)),
            no_streaming=bool(flags.get("--no-streaming", False)),
            streaming=bool(flags.get("--streaming", False)),
            abi=(str(flags["--abi"]).strip() if flags.get("--abi") else None),
            user=(str(flags["--user"]).strip() if flags.get("--user") else None),
        )

    def adb_flags(self) -> list[str]:
        """Flags supported by `adb install` / `adb install-multiple`."""
        if self.streaming and self.no_streaming:
            raise ValueError("Cannot use both --streaming and --no-streaming.")

        flags: list[str] = []
        if self.reinstall:
            flags.append("-r")
        if self.allow_test:
            flags.append("-t")
        if self.allow_downgrade:
            flags.append("-d")
        if self.partial:
            flags.append("-p")
        if self.grant_permissions:
            flags.append("-g")
        if self.instant:
            flags.append("--instant")
        if self.no_streaming:
            flags.append("--no-streaming")
        if self.streaming:
            flags.append("--streaming")
        if self.abi:
            flags.extend(["--abi", self.abi])
        if self.user:
            flags.extend(["--user", self.user])
        return flags

    def pm_flags(self) -> list[str]:
        """Flags safe to forward to `pm install` on-device."""
        flags: list[str] = []
        if self.reinstall:
            flags.append("-r")
        if self.allow_test:
            flags.append("-t")
        if self.allow_downgrade:
            flags.append("-d")
        if self.partial:
            flags.append("-p")
        if self.grant_permissions:
            flags.append("-g")
        if self.instant:
            flags.append("--instant")
        if self.abi:
            flags.extend(["--abi", self.abi])
        if self.user:
            flags.extend(["--user", self.user])
        return flags


@dataclass(slots=True)
class PreparedAPKPackage:
    """Prepared local package input ready for adb install execution."""

    source_path: str
    display_name: str
    apk_paths: list[str]
    install_mode: str
    extracted_dir: Optional[str] = None

    def cleanup(self):
        if self.extracted_dir and os.path.isdir(self.extracted_dir):
            shutil.rmtree(self.extracted_dir, ignore_errors=True)


@dataclass(slots=True)
class APKInstallResult:
    """Aggregate result for one or more install commands."""

    returncode: int
    stdout: str
    stderr: str
    install_mode: str
    package_paths: list[str] = field(default_factory=list)
    commands: list[list[str]] = field(default_factory=list)
    source_path: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.returncode == 0


def supported_package_dialog_filter() -> str:
    return PACKAGE_DIALOG_FILTER


def is_supported_package(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in SUPPORTED_PACKAGE_EXTENSIONS


def prepare_package_source(package_path: str) -> PreparedAPKPackage:
    """Prepare a local install source, extracting archives when needed."""
    if not package_path:
        raise ValueError("Package path is empty.")
    if not os.path.exists(package_path):
        raise FileNotFoundError(package_path)

    ext = os.path.splitext(package_path)[1].lower()
    if ext not in SUPPORTED_PACKAGE_EXTENSIONS:
        raise ValueError(f"Unsupported package type: {ext or '(no extension)'}")

    display_name = os.path.splitext(os.path.basename(package_path))[0]
    if ext in SUPPORTED_SINGLE_PACKAGE_EXTENSIONS:
        return prepare_apk_collection([package_path], source_path=package_path, display_name=display_name)

    if not zipfile.is_zipfile(package_path):
        raise ValueError(f"{os.path.basename(package_path)} is not a valid ZIP-based package.")

    extract_dir = tempfile.mkdtemp(prefix="quickadb_apkapi_")
    try:
        with zipfile.ZipFile(package_path, "r") as archive:
            archive.extractall(extract_dir)

        apk_paths = [
            os.path.join(root, file_name)
            for root, _, files in os.walk(extract_dir)
            for file_name in files
            if file_name.lower().endswith(".apk")
        ]
        if not apk_paths:
            raise ValueError(f"No APK files were found inside {os.path.basename(package_path)}.")

        prepared = prepare_apk_collection(
            apk_paths,
            source_path=package_path,
            display_name=display_name,
        )
        prepared.extracted_dir = extract_dir
        return prepared
    except Exception:
        shutil.rmtree(extract_dir, ignore_errors=True)
        raise


def prepare_apk_collection(
    apk_paths: Sequence[str],
    source_path: Optional[str] = None,
    display_name: Optional[str] = None,
) -> PreparedAPKPackage:
    """Prepare an already-available set of APK files for installation."""
    normalized_paths = _normalize_apk_paths(apk_paths)
    if not normalized_paths:
        raise ValueError("No APK files were provided for installation.")

    if len(normalized_paths) == 1:
        install_mode = "single"
    elif any(os.path.basename(path).lower() == "base.apk" for path in normalized_paths):
        install_mode = "multiple"
    else:
        install_mode = "sequential"

    name = display_name or os.path.splitext(os.path.basename(source_path or normalized_paths[0]))[0]
    return PreparedAPKPackage(
        source_path=source_path or normalized_paths[0],
        display_name=name,
        apk_paths=normalized_paths,
        install_mode=install_mode,
    )


def build_install_commands(
    prepared: PreparedAPKPackage,
    options: Optional[APKInstallOptions] = None,
    adb_path: Optional[str] = None,
    serial: Optional[str] = None,
) -> list[list[str]]:
    """Build adb install command(s) for a prepared package."""
    options = options or APKInstallOptions()
    flags = options.adb_flags()
    prefix = _adb_prefix(adb_path=adb_path, serial=serial)

    if prepared.install_mode == "single":
        return [prefix + ["install"] + flags + prepared.apk_paths]
    if prepared.install_mode == "multiple":
        return [prefix + ["install-multiple"] + flags + prepared.apk_paths]
    return [prefix + ["install"] + flags + [apk_path] for apk_path in prepared.apk_paths]


def install_prepared_package(
    prepared: PreparedAPKPackage,
    options: Optional[APKInstallOptions] = None,
    adb_path: Optional[str] = None,
    serial: Optional[str] = None,
    continue_on_error: bool = True,
) -> APKInstallResult:
    """Execute installation for a prepared package source."""
    commands = build_install_commands(prepared, options=options, adb_path=adb_path, serial=serial)
    processes: list[subprocess.CompletedProcess] = []

    for command in commands:
        process = subprocess.run(command, **_subprocess_kwargs())
        processes.append(process)
        if process.returncode != 0 and not continue_on_error:
            break

    return _combine_processes(
        processes,
        install_mode=prepared.install_mode,
        package_paths=prepared.apk_paths,
        source_path=prepared.source_path,
        commands=commands,
    )


def install_local_package(
    package_path: str,
    options: Optional[APKInstallOptions] = None,
    adb_path: Optional[str] = None,
    serial: Optional[str] = None,
    continue_on_error: bool = True,
) -> APKInstallResult:
    """Prepare and install a local APK or archive package."""
    prepared = prepare_package_source(package_path)
    try:
        return install_prepared_package(
            prepared,
            options=options,
            adb_path=adb_path,
            serial=serial,
            continue_on_error=continue_on_error,
        )
    finally:
        prepared.cleanup()


def build_remote_install_shell_command(
    remote_path: str,
    options: Optional[APKInstallOptions] = None,
    use_root: bool = False,
) -> str:
    """Build the remote shell command used to install an APK already on the device."""
    if not remote_path:
        raise ValueError("Remote package path is empty.")

    options = options or APKInstallOptions()
    pm_parts = ["pm", "install"] + options.pm_flags() + [shlex.quote(remote_path)]
    pm_command = " ".join(pm_parts)
    if use_root:
        return f"su -c {shlex.quote(pm_command)}"
    return pm_command


def install_remote_package(
    remote_path: str,
    options: Optional[APKInstallOptions] = None,
    use_root: bool = False,
    adb_path: Optional[str] = None,
    serial: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """Install an APK that already exists on the device filesystem."""
    shell_command = build_remote_install_shell_command(remote_path, options=options, use_root=use_root)
    command = _adb_prefix(adb_path=adb_path, serial=serial) + ["shell", shell_command]
    return subprocess.run(command, **_subprocess_kwargs())


def _normalize_apk_paths(apk_paths: Sequence[str]) -> list[str]:
    normalized = []
    seen = set()
    for path in apk_paths:
        if not path:
            continue
        abs_path = os.path.abspath(path)
        if abs_path in seen or not os.path.isfile(abs_path):
            continue
        if not abs_path.lower().endswith(".apk"):
            continue
        seen.add(abs_path)
        normalized.append(abs_path)

    def sort_key(path: str):
        filename = os.path.basename(path).lower()
        is_base = 0 if filename == "base.apk" else 1
        return (is_base, filename)

    return sorted(normalized, key=sort_key)


def _combine_processes(
    processes: Sequence[subprocess.CompletedProcess],
    install_mode: str,
    package_paths: Sequence[str],
    source_path: Optional[str],
    commands: Sequence[Sequence[str]],
) -> APKInstallResult:
    if not processes:
        return APKInstallResult(
            returncode=1,
            stdout="",
            stderr="No install commands were executed.",
            install_mode=install_mode,
            package_paths=list(package_paths),
            commands=[list(command) for command in commands],
            source_path=source_path,
        )

    stdout_chunks = [process.stdout.strip() for process in processes if (process.stdout or "").strip()]
    stderr_chunks = [process.stderr.strip() for process in processes if (process.stderr or "").strip()]
    returncode = next((process.returncode for process in processes if process.returncode != 0), processes[-1].returncode)

    return APKInstallResult(
        returncode=returncode,
        stdout="\n".join(stdout_chunks),
        stderr="\n".join(stderr_chunks),
        install_mode=install_mode,
        package_paths=list(package_paths),
        commands=[list(command) for command in commands],
        source_path=source_path,
    )


def _adb_prefix(adb_path: Optional[str] = None, serial: Optional[str] = None) -> list[str]:
    adb_exe = adb_path or ToolPaths.instance().adb
    command = [adb_exe]
    command.extend(_serial_args(serial))
    return command


def _serial_args(serial: Optional[str]) -> list[str]:
    if serial is None:
        return DeviceManager.instance().serial_args()
    if serial:
        return ["-s", serial]
    return []


def _subprocess_kwargs() -> dict:
    kwargs = {
        "capture_output": True,
        "text": True,
        "check": False,
        "env": get_clean_env(),
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    return kwargs
