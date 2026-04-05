"""
magiskpatcher.py - Backend module for Magisk rooting and module management.
"""

from __future__ import annotations

import hashlib
import os
import random
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass
from typing import Callable, Optional

import requests

from util.devicemanager import DeviceManager
from util.toolpaths import ToolPaths


MAGISK_RELEASE_API = "https://api.github.com/repos/topjohnwu/Magisk/releases/latest"
MAGISK_CACHE_DIR = os.path.join(tempfile.gettempdir(), "quickadb_magisk")
SUPPORTED_MAGISK_ABIS = ("arm64-v8a", "armeabi-v7a", "x86", "x86_64")
MAGISK_REQUIRED_ASSETS = ("boot_patch.sh", "util_functions.sh", "stub.apk")
MAGISK_MODULE_INSTALLER_PATH = "/data/adb/magisk/module_installer.sh"
MAGISK_MODULES_DIR = "/data/adb/modules"
MAGISK_REQUIRED_LIBS = {
    "libbusybox.so": "busybox",
    "libinit-ld.so": "init-ld",
    "libmagisk.so": "magisk",
    "libmagiskboot.so": "magiskboot",
    "libmagiskinit.so": "magiskinit",
    "libmagiskpolicy.so": "magiskpolicy",
}


@dataclass(slots=True)
class MagiskReleaseInfo:
    version: str
    tag_name: str
    asset_name: str
    download_url: str
    sha256: str = ""


@dataclass(slots=True)
class PreparedMagiskSource:
    root_path: str
    cleanup_dir: Optional[str]
    version: str
    supported_abis: list[str]


@dataclass(slots=True)
class MagiskDeviceInfo:
    serial: str
    device_name: str
    sdk: str
    abi: str
    abi_list: list[str]
    selected_abi: str
    supported_abis: list[str]
    root_method: str
    root_version: str


@dataclass(slots=True)
class MagiskModuleInfo:
    module_id: str
    name: str
    version: str
    state: str
    path: str


@dataclass(slots=True)
class MagiskPatchOptions:
    magisk_source: str
    boot_image_path: str
    output_dir: str
    keep_verity: bool = False
    keep_force_encrypt: bool = False
    patch_vbmeta_flag: bool = False
    recovery_mode: bool = False
    legacy_sar: bool = False


@dataclass(slots=True)
class MagiskPatchResult:
    output_path: str
    output_name: str
    magisk_version: str
    device_abi: str
    serial: str


def _bool_shell(value: bool) -> str:
    return "true" if value else "false"


def _sanitize_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (value or "").strip())
    return cleaned.strip("._") or "boot_image.img"


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_magisk_version(root_path: str) -> str:
    util_functions = os.path.join(root_path, "assets", "util_functions.sh")
    try:
        with open(util_functions, "r", encoding="utf-8") as handle:
            content = handle.read()
        match = re.search(r"MAGISK_VER='([^']+)'", content)
        if match:
            return match.group(1).strip()
    except Exception:
        pass

    base = os.path.basename(root_path.rstrip("\\/"))
    match = re.search(r"(\d+(?:\.\d+)*)", base)
    if match:
        return match.group(1)
    return "Unknown"


def _make_work_dir(base_dir: str, prefix: str) -> str:
    os.makedirs(base_dir, exist_ok=True)
    for _ in range(50):
        candidate = os.path.join(
            base_dir,
            f"{prefix}_{int(time.time() * 1000)}_{random.randint(1000, 9999)}",
        )
        try:
            os.makedirs(candidate)
            return candidate
        except FileExistsError:
            continue
    raise RuntimeError(f"Could not create a temporary work directory under {base_dir}.")


def _looks_like_magisk_root(path: str) -> bool:
    if not os.path.isdir(path):
        return False
    assets_dir = os.path.join(path, "assets")
    lib_dir = os.path.join(path, "lib")
    if not os.path.isdir(assets_dir) or not os.path.isdir(lib_dir):
        return False
    for asset_name in MAGISK_REQUIRED_ASSETS:
        if not os.path.isfile(os.path.join(assets_dir, asset_name)):
            return False
    return True


class MagiskPatcher:
    def __init__(self, adb_path: Optional[str] = None, serial_args: Optional[list[str]] = None):
        self.adb_path = adb_path or ToolPaths.instance().adb
        self.serial_args = list(serial_args) if serial_args is not None else DeviceManager.instance().serial_args()

    @staticmethod
    def default_cache_dir() -> str:
        os.makedirs(MAGISK_CACHE_DIR, exist_ok=True)
        return MAGISK_CACHE_DIR

    def fetch_latest_release_info(self) -> MagiskReleaseInfo:
        response = requests.get(
            MAGISK_RELEASE_API,
            headers={"User-Agent": "QuickADB-RootManager/1.0"},
            timeout=(10, 60),
        )
        response.raise_for_status()
        payload = response.json()
        assets = payload.get("assets") or []
        release_asset = None
        for asset in assets:
            name = str(asset.get("name") or "")
            if name.startswith("Magisk-v") and name.endswith(".apk"):
                release_asset = asset
                break
        if release_asset is None:
            raise RuntimeError("Could not find the official Magisk APK asset in the latest release.")

        digest = str(release_asset.get("digest") or "")
        if digest.startswith("sha256:"):
            digest = digest.split(":", 1)[1]

        tag_name = str(payload.get("tag_name") or "").strip()
        version = tag_name[1:] if tag_name.startswith("v") else tag_name
        return MagiskReleaseInfo(
            version=version or "Unknown",
            tag_name=tag_name or "latest",
            asset_name=str(release_asset.get("name") or "Magisk.apk"),
            download_url=str(release_asset.get("browser_download_url") or ""),
            sha256=digest,
        )

    @staticmethod
    def release_info_for_version(version: str) -> MagiskReleaseInfo:
        clean_version = (version or "").strip().lstrip("v")
        if not clean_version:
            raise RuntimeError("A Magisk version is required.")
        tag_name = f"v{clean_version}"
        asset_name = f"Magisk-v{clean_version}.apk"
        return MagiskReleaseInfo(
            version=clean_version,
            tag_name=tag_name,
            asset_name=asset_name,
            download_url=f"https://github.com/topjohnwu/Magisk/releases/download/{tag_name}/{asset_name}",
            sha256="",
        )

    def download_release(
        self,
        release: MagiskReleaseInfo,
        dest_dir: Optional[str] = None,
        progress_callback: Optional[Callable[[int], None]] = None,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        target_dir = dest_dir or self.default_cache_dir()
        os.makedirs(target_dir, exist_ok=True)
        output_path = os.path.join(target_dir, release.asset_name)

        if os.path.isfile(output_path) and release.sha256:
            if _sha256_file(output_path).lower() == release.sha256.lower():
                if log_callback:
                    log_callback(f"Using cached Magisk {release.version} at {output_path}")
                if progress_callback:
                    progress_callback(100)
                return output_path

        if log_callback:
            log_callback(f"Downloading Magisk {release.version}...")

        response = requests.get(
            release.download_url,
            headers={"User-Agent": "QuickADB-RootManager/1.0"},
            stream=True,
            timeout=(10, 180),
        )
        response.raise_for_status()
        total_size = int(response.headers.get("Content-Length") or 0)
        downloaded = 0
        temp_path = output_path + ".tmp"
        try:
            with open(temp_path, "wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback and total_size > 0:
                        progress_callback(min(100, int(downloaded * 100 / total_size)))

            if release.sha256:
                actual_hash = _sha256_file(temp_path)
                if actual_hash.lower() != release.sha256.lower():
                    raise RuntimeError(
                        f"Downloaded Magisk checksum mismatch. Expected {release.sha256}, got {actual_hash}."
                    )

            os.replace(temp_path, output_path)
            if progress_callback:
                progress_callback(100)
            if log_callback:
                log_callback(f"Downloaded Magisk to {output_path}")
            return output_path
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    def inspect_magisk_source(self, source_path: str) -> PreparedMagiskSource:
        return self.prepare_magisk_source(source_path)

    def prepare_magisk_source(self, source_path: str) -> PreparedMagiskSource:
        raw_path = os.path.abspath(os.path.expanduser((source_path or "").strip()))
        if not raw_path:
            raise RuntimeError("Select a Magisk folder or APK first.")

        if os.path.isdir(raw_path) and _looks_like_magisk_root(raw_path):
            return PreparedMagiskSource(
                root_path=raw_path,
                cleanup_dir=None,
                version=_parse_magisk_version(raw_path),
                supported_abis=self.supported_abis(raw_path),
            )

        if os.path.isfile(raw_path) and raw_path.lower().endswith((".apk", ".zip")):
            extract_dir = _make_work_dir(self.default_cache_dir(), "extract")
            with zipfile.ZipFile(raw_path, "r") as archive:
                archive.extractall(extract_dir)

            root_path = extract_dir
            if not _looks_like_magisk_root(root_path):
                children = [os.path.join(root_path, name) for name in os.listdir(root_path)]
                for child in children:
                    if _looks_like_magisk_root(child):
                        root_path = child
                        break

            if not _looks_like_magisk_root(root_path):
                shutil.rmtree(extract_dir, ignore_errors=True)
                raise RuntimeError("The selected file does not contain a valid Magisk package layout.")

            return PreparedMagiskSource(
                root_path=root_path,
                cleanup_dir=extract_dir,
                version=_parse_magisk_version(root_path),
                supported_abis=self.supported_abis(root_path),
            )

        raise RuntimeError("Select either an extracted Magisk folder or a Magisk APK.")

    def supported_abis(self, root_path: str) -> list[str]:
        lib_root = os.path.join(root_path, "lib")
        supported: list[str] = []
        if not os.path.isdir(lib_root):
            return supported

        for abi in SUPPORTED_MAGISK_ABIS:
            abi_dir = os.path.join(lib_root, abi)
            if not os.path.isdir(abi_dir):
                continue
            required = os.path.join(abi_dir, "libmagiskboot.so")
            if os.path.isfile(required):
                supported.append(abi)
        return supported

    def detect_device_info(self, source_path: Optional[str] = None) -> MagiskDeviceInfo:
        abi_list_raw = self._run_adb_text(["shell", "getprop", "ro.product.cpu.abilist"]).strip()
        primary_abi = self._run_adb_text(["shell", "getprop", "ro.product.cpu.abi"]).strip()
        sdk = self._run_adb_text(["shell", "getprop", "ro.build.version.sdk"]).strip()
        manufacturer = self._safe_adb_text(["shell", "getprop", "ro.product.manufacturer"]).strip()
        model = self._safe_adb_text(["shell", "getprop", "ro.product.model"]).strip()
        root_version = self._safe_adb_text(["shell", "su", "-v"], timeout=15).strip()
        root_method = self._detect_root_method(root_version)

        abi_list: list[str] = []
        for chunk in abi_list_raw.split(","):
            abi = chunk.strip()
            if abi and abi not in abi_list:
                abi_list.append(abi)
        if primary_abi and primary_abi not in abi_list:
            abi_list.insert(0, primary_abi)

        if not abi_list:
            raise RuntimeError("Could not detect the connected device ABI via adb.")

        supported_abis = list(SUPPORTED_MAGISK_ABIS)
        if source_path:
            try:
                prepared = self.prepare_magisk_source(source_path)
                supported_abis = prepared.supported_abis or supported_abis
            finally:
                if "prepared" in locals() and prepared.cleanup_dir:
                    shutil.rmtree(prepared.cleanup_dir, ignore_errors=True)

        selected_abi = ""
        for abi in abi_list:
            if abi in supported_abis:
                selected_abi = abi
                break

        serial = self.serial_args[1] if len(self.serial_args) >= 2 else ""
        return MagiskDeviceInfo(
            serial=serial,
            device_name=" ".join(part for part in (manufacturer, model) if part).strip() or serial or "Unknown Device",
            sdk=sdk or "Unknown",
            abi=primary_abi or (abi_list[0] if abi_list else ""),
            abi_list=abi_list,
            selected_abi=selected_abi,
            supported_abis=supported_abis,
            root_method=root_method,
            root_version=root_version or "Unavailable",
        )

    def list_modules(self) -> list[MagiskModuleInfo]:
        modules: list[MagiskModuleInfo] = []
        output = self._run_root_shell(f"ls -p {shlex.quote(MAGISK_MODULES_DIR)}", timeout=30)
        for raw_line in output.splitlines():
            folder_name = raw_line.strip()
            if not folder_name or not folder_name.endswith("/"):
                continue
            folder_name = folder_name.rstrip("/")
            if not folder_name or folder_name in {".", ".."}:
                continue

            path = f"{MAGISK_MODULES_DIR}/{folder_name}"
            prop_path = f"{path}/module.prop"

            prop_text = self._safe_root_shell(f'cat "{prop_path}"', timeout=15).strip()
            if not prop_text:
                continue

            prop_values = self._parse_module_prop(prop_text)
            module_id = prop_values.get("id") or folder_name
            name = prop_values.get("name") or module_id or "Unnamed Module"
            version = prop_values.get("version") or "Unknown"
            state = "enabled"
            if self._root_path_exists(f"{path}/disable", "f"):
                state = "disabled"
            if self._root_path_exists(f"{path}/remove", "f"):
                state = "remove"
            if self._root_path_exists(f"{path}/update", "f"):
                state = f"{state}+update"

            modules.append(
                MagiskModuleInfo(
                    module_id=module_id,
                    name=name,
                    version=version,
                    state=state,
                    path=path,
                )
            )
        modules.sort(key=lambda item: item.name.lower())
        return modules

    def magisk_cli_available(self) -> bool:
        return self._root_path_exists(MAGISK_MODULE_INSTALLER_PATH, "f")

    def install_module_zip(
        self,
        zip_path: str,
        log_callback: Optional[Callable[[str], None]] = None,
        status_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> str:
        local_zip = os.path.abspath(os.path.expanduser((zip_path or "").strip()))
        if not os.path.isfile(local_zip):
            raise RuntimeError("Select a valid Magisk module ZIP first.")
        if not self.magisk_cli_available():
            raise RuntimeError(f"Magisk module installer was not found at {MAGISK_MODULE_INSTALLER_PATH}.")

        remote_zip = (
            f"/data/local/tmp/quickadb_module_{int(time.time() * 1000)}_"
            f"{random.randint(1000, 9999)}_{_sanitize_filename(os.path.basename(local_zip))}"
        )

        try:
            self._emit(status_callback, "Preparing module install...")
            self._emit_progress(progress_callback, 10)

            self._emit_log(log_callback, f"Pushing module ZIP to {remote_zip}")
            self._emit(status_callback, "Pushing module ZIP to the device...")
            self._emit_progress(progress_callback, 35)
            self._run_adb_checked(["push", local_zip, remote_zip], timeout=300)

            self._emit(status_callback, "Installing module with Magisk CLI...")
            self._emit_progress(progress_callback, 60)
            self._run_magisk_module_install(remote_zip, log_callback=log_callback)

            self._emit(status_callback, "Cleaning up temporary module files...")
            self._emit_progress(progress_callback, 90)
            self._run_adb_best_effort(["shell", "rm", "-f", remote_zip])
            self._emit_progress(progress_callback, 100)
            return local_zip
        finally:
            self._run_adb_best_effort(["shell", "rm", "-f", remote_zip])

    def set_module_enabled(self, module_path: str, enabled: bool) -> str:
        clean_path = (module_path or "").strip().rstrip("/")
        if not clean_path.startswith(f"{MAGISK_MODULES_DIR}/"):
            raise RuntimeError("Invalid module path.")
        if not self._root_path_exists(clean_path, "d"):
            raise RuntimeError(f"Module path does not exist: {clean_path}")

        disable_path = f"{clean_path}/disable"
        if enabled:
            self._run_root_shell(f"rm -f {shlex.quote(disable_path)}", timeout=20)
            return "enabled"

        self._run_root_shell(f"touch {shlex.quote(disable_path)}", timeout=20)
        return "disabled"

    def _run_magisk_module_install(
        self,
        remote_zip: str,
        log_callback: Optional[Callable[[str], None]] = None,
    ):
        quoted_zip = shlex.quote(remote_zip)
        command_text = (
            f"chmod 755 {shlex.quote(MAGISK_MODULE_INSTALLER_PATH)} && "
            f"sh {shlex.quote(MAGISK_MODULE_INSTALLER_PATH)} dummy 1 {quoted_zip}"
        )
        self._emit_log(log_callback, f"Installing module via {MAGISK_MODULE_INSTALLER_PATH}...")
        self._run_adb_stream(["shell", "su", "-c", command_text], log_callback=log_callback)

    def patch_image(
        self,
        options: MagiskPatchOptions,
        log_callback: Optional[Callable[[str], None]] = None,
        status_callback: Optional[Callable[[str], None]] = None,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> MagiskPatchResult:
        if not os.path.isfile(options.boot_image_path):
            raise RuntimeError("Select a valid boot/init_boot/vendor_boot image first.")
        if not os.path.exists(options.output_dir):
            os.makedirs(options.output_dir, exist_ok=True)

        prepared = self.prepare_magisk_source(options.magisk_source)
        local_stage_dir: Optional[str] = None
        remote_workdir = ""
        try:
            self._emit(status_callback, "Detecting device ABI...")
            self._emit_progress(progress_callback, 10)
            device_info = self.detect_device_info(prepared.root_path)
            if not device_info.selected_abi:
                supported = ", ".join(prepared.supported_abis) or "none"
                available = ", ".join(device_info.abi_list) or "unknown"
                raise RuntimeError(
                    f"No compatible Magisk ABI found. Device offers {available}; Magisk source supports {supported}."
                )
            self._emit_log(
                log_callback,
                f"Using device ABI {device_info.selected_abi} (device ABI list: {', '.join(device_info.abi_list)})",
            )

            self._emit(status_callback, "Preparing Magisk workspace...")
            self._emit_progress(progress_callback, 22)
            local_stage_dir, staged_image_name = self._create_local_stage(
                prepared.root_path,
                device_info.selected_abi,
                options.boot_image_path,
            )

            remote_parent = "/data/local/tmp"
            remote_workdir = f"{remote_parent}/{os.path.basename(local_stage_dir)}"
            self._emit_log(log_callback, f"Pushing Magisk workspace to {remote_workdir}")
            self._emit(status_callback, "Pushing files to the device...")
            self._emit_progress(progress_callback, 40)
            self._run_adb_checked(["push", local_stage_dir, remote_parent])

            self._emit(status_callback, "Running Magisk boot patch...")
            self._emit_progress(progress_callback, -1)
            patch_command = (
                f"cd {shlex.quote(remote_workdir)} && "
                f"chmod -R 755 . && "
                f"KEEPVERITY={_bool_shell(options.keep_verity)} "
                f"KEEPFORCEENCRYPT={_bool_shell(options.keep_force_encrypt)} "
                f"PATCHVBMETAFLAG={_bool_shell(options.patch_vbmeta_flag)} "
                f"RECOVERYMODE={_bool_shell(options.recovery_mode)} "
                f"LEGACYSAR={_bool_shell(options.legacy_sar)} "
                f"ASH_STANDALONE=1 "
                f"./busybox sh ./boot_patch.sh {shlex.quote(staged_image_name)}"
            )
            self._run_adb_stream(["shell", patch_command], log_callback=log_callback)

            output_name = f"magisk_patched_{_sanitize_filename(os.path.basename(options.boot_image_path))}"
            local_output_path = os.path.join(options.output_dir, output_name)
            remote_output_path = f"{remote_workdir}/new-boot.img"

            self._emit(status_callback, "Pulling patched image back to the PC...")
            self._emit_progress(progress_callback, 84)
            self._run_adb_checked(["pull", remote_output_path, local_output_path])

            self._emit(status_callback, "Cleaning up temporary files...")
            self._emit_progress(progress_callback, 95)
            self._run_adb_best_effort(["shell", f"rm -rf {shlex.quote(remote_workdir)}"])

            self._emit_progress(progress_callback, 100)
            self._emit(status_callback, "Patch completed.")
            self._emit_log(log_callback, f"Patched image saved to {local_output_path}")
            return MagiskPatchResult(
                output_path=local_output_path,
                output_name=output_name,
                magisk_version=prepared.version,
                device_abi=device_info.selected_abi,
                serial=device_info.serial,
            )
        finally:
            if remote_workdir:
                self._run_adb_best_effort(["shell", f"rm -rf {shlex.quote(remote_workdir)}"])
            if local_stage_dir:
                shutil.rmtree(local_stage_dir, ignore_errors=True)
            if prepared.cleanup_dir:
                shutil.rmtree(prepared.cleanup_dir, ignore_errors=True)

    def _create_local_stage(self, root_path: str, abi: str, boot_image_path: str) -> tuple[str, str]:
        stage_dir = _make_work_dir(self.default_cache_dir(), "stage")
        assets_dir = os.path.join(root_path, "assets")
        abi_dir = os.path.join(root_path, "lib", abi)
        if not os.path.isdir(abi_dir):
            raise RuntimeError(f"Magisk package does not contain the ABI folder {abi}.")

        for asset_name in MAGISK_REQUIRED_ASSETS:
            source_asset = os.path.join(assets_dir, asset_name)
            if not os.path.isfile(source_asset):
                raise RuntimeError(f"Magisk asset is missing: {asset_name}")
            shutil.copy2(source_asset, os.path.join(stage_dir, asset_name))

        chromeos_dir = os.path.join(assets_dir, "chromeos")
        if os.path.isdir(chromeos_dir):
            shutil.copytree(chromeos_dir, os.path.join(stage_dir, "chromeos"))

        for source_name, target_name in MAGISK_REQUIRED_LIBS.items():
            source_lib = os.path.join(abi_dir, source_name)
            if os.path.isfile(source_lib):
                shutil.copy2(source_lib, os.path.join(stage_dir, target_name))

        for target_name in ("busybox", "init-ld", "magisk", "magiskboot", "magiskinit"):
            if not os.path.isfile(os.path.join(stage_dir, target_name)):
                raise RuntimeError(f"Required Magisk binary is missing for {abi}: {target_name}")

        image_name = _sanitize_filename(os.path.basename(boot_image_path))
        shutil.copy2(boot_image_path, os.path.join(stage_dir, image_name))
        return stage_dir, image_name

    def _run_adb_text(self, args: list[str], timeout: int = 30) -> str:
        command = [self.adb_path] + self.serial_args + args
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            creationflags=self._creationflags(),
        )
        if completed.returncode != 0:
            error_text = (completed.stdout or completed.stderr or "").strip()
            raise RuntimeError(error_text or f"ADB command failed: {' '.join(args)}")
        return completed.stdout

    def _safe_adb_text(self, args: list[str], timeout: int = 30) -> str:
        try:
            return self._run_adb_text(args, timeout=timeout)
        except Exception:
            return ""

    def _run_root_shell(self, command: str, timeout: int = 30) -> str:
        return self._run_adb_checked(["shell", "su", "-c", command], timeout=timeout)

    def _safe_root_shell(self, command: str, timeout: int = 30) -> str:
        try:
            return self._run_root_shell(command, timeout=timeout)
        except Exception:
            return ""

    def _root_path_exists(self, path: str, entry_type: str = "f") -> bool:
        check = self._safe_adb_text(
            ["shell", "su", "-c", f'[ -{entry_type} "{path}" ] && echo yes'],
            timeout=15,
        ).strip()
        return check == "yes"

    @staticmethod
    def _parse_module_prop(prop_text: str) -> dict[str, str]:
        values: dict[str, str] = {}
        for raw_line in (prop_text or "").splitlines():
            line = raw_line.strip()
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key:
                values[key] = value.strip()
        return values

    def _run_adb_checked(self, args: list[str], timeout: int = 300) -> str:
        command = [self.adb_path] + self.serial_args + args
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            creationflags=self._creationflags(),
        )
        if completed.returncode != 0:
            output = (completed.stdout or completed.stderr or "").strip()
            raise RuntimeError(output or f"ADB command failed: {' '.join(args)}")
        return completed.stdout or ""

    def _run_adb_stream(self, args: list[str], log_callback: Optional[Callable[[str], None]] = None):
        command = [self.adb_path] + self.serial_args + args
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=self._creationflags(),
        )

        last_output = ""
        try:
            assert process.stdout is not None
            for raw_line in process.stdout:
                line = raw_line.rstrip("\r\n")
                if not line:
                    continue
                last_output = line
                self._emit_log(log_callback, line)
            return_code = process.wait()
            if return_code != 0:
                raise RuntimeError(last_output or f"ADB shell patch command failed with exit code {return_code}.")
        finally:
            if process.stdout is not None:
                process.stdout.close()

    def _run_adb_best_effort(self, args: list[str]):
        try:
            self._run_adb_checked(args, timeout=30)
        except Exception:
            pass

    @staticmethod
    def _detect_root_method(root_version: str) -> str:
        text = (root_version or "").strip()
        if not text:
            return "Not Available"
        upper = text.upper()
        if "MAGISK" in upper:
            return "MagiskSU"
        if "KERNELSU" in upper:
            return "KernelSU"
        if "APATCH" in upper:
            return "APatch"
        return "su"

    @staticmethod
    def _emit(callback: Optional[Callable[[str], None]], message: str):
        if callback:
            callback(message)

    @staticmethod
    def _emit_log(callback: Optional[Callable[[str], None]], message: str):
        if callback and message:
            callback(message)

    @staticmethod
    def _emit_progress(callback: Optional[Callable[[int], None]], value: int):
        if callback:
            callback(value)

    @staticmethod
    def _creationflags() -> int:
        if os.name == "nt":
            return subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        return 0
