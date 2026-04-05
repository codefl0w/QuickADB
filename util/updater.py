"""
updater.py - QuickADB's cross-platform update helper.

Handles:
- checking GitHub releases for newer versions
- fetching remote changelog HTML
- downloading the correct release asset for the current platform
- verifying the downloaded file against GitHub's SHA-256 digest metadata
- applying the update via a small platform-specific helper script after restart
"""

from __future__ import annotations

import hashlib
import html
import os
import platform
import shlex
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Callable, Optional

import requests
from PyQt6.QtCore import QObject, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from util.resource import open_url_safe, resource_path


GITHUB_API_VERSION = "2026-03-10"


def _format_size(num_bytes: int) -> str:
    if num_bytes <= 0:
        return "0 B"
    value = float(num_bytes)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{int(num_bytes)} B"


def _normalize_version(version_text: str) -> tuple[int, ...]:
    cleaned = (version_text or "").strip().lstrip("vV")
    if not cleaned:
        return (0,)

    values = []
    for part in cleaned.split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        values.append(int(digits) if digits else 0)
    return tuple(values) if values else (0,)


def _is_newer_version(latest: str, current: str) -> bool:
    latest_tuple = _normalize_version(latest)
    current_tuple = _normalize_version(current)
    max_len = max(len(latest_tuple), len(current_tuple))
    latest_padded = latest_tuple + (0,) * (max_len - len(latest_tuple))
    current_padded = current_tuple + (0,) * (max_len - len(current_tuple))
    return latest_padded > current_padded


def _html_from_release_body(body: str) -> str:
    text = (body or "").strip()
    if not text:
        return "<p>No release notes were provided for this version.</p>"
    escaped = html.escape(text)
    return f"<h2>Release Notes</h2><pre>{escaped}</pre>"


def _load_local_changelog_html() -> str:
    html_path = resource_path("res/whatsnew.html")
    if not os.path.exists(html_path):
        return "<p>Changelog is unavailable.</p>"
    try:
        with open(html_path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return "<p>Changelog is unavailable.</p>"


def _bat_escape(value: str) -> str:
    return value.replace("%", "%%")


@dataclass
class ReleaseAsset:
    name: str
    download_url: str
    digest: str
    size: int


@dataclass
class ReleaseInfo:
    version: str
    html_url: str
    body: str
    assets: list[ReleaseAsset]


class UpdateCheckWorker(QThread):
    update_available = pyqtSignal(object, str)
    up_to_date = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, current_version: str, release_api_url: str, changelog_url_template: str):
        super().__init__()
        self.current_version = current_version
        self.release_api_url = release_api_url
        self.changelog_url_template = changelog_url_template

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "User-Agent": "QuickADB",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }

    def _fetch_remote_changelog(self, release_version: str) -> str:
        if "{ref}" in self.changelog_url_template:
            candidate_urls = [
                self.changelog_url_template.format(ref=release_version),
                self.changelog_url_template.format(ref="refs/heads/main"),
            ]
        else:
            candidate_urls = [self.changelog_url_template]

        last_error = None
        for url in candidate_urls:
            try:
                response = requests.get(url, timeout=8)
                response.raise_for_status()
                content = response.text.strip()
                if content:
                    return content
            except Exception as exc:
                last_error = exc

        if last_error:
            raise last_error
        raise ValueError("Remote changelog was empty.")

    def run(self):
        try:
            response = requests.get(self.release_api_url, headers=self._headers(), timeout=10)
            response.raise_for_status()
            data = response.json() or {}

            release = ReleaseInfo(
                version=(data.get("tag_name") or "").strip(),
                html_url=(data.get("html_url") or "").strip(),
                body=(data.get("body") or "").strip(),
                assets=[
                    ReleaseAsset(
                        name=(asset.get("name") or "").strip(),
                        download_url=(asset.get("browser_download_url") or "").strip(),
                        digest=(asset.get("digest") or "").strip(),
                        size=int(asset.get("size") or 0),
                    )
                    for asset in (data.get("assets") or [])
                    if asset.get("browser_download_url")
                ],
            )

            if not release.version:
                raise ValueError("GitHub did not return a release tag.")

            if _is_newer_version(release.version, self.current_version):
                try:
                    changelog_html = self._fetch_remote_changelog(release.version)
                except Exception:
                    changelog_html = _load_local_changelog_html()
                    if "unavailable" in changelog_html.lower():
                        changelog_html = _html_from_release_body(release.body)
                self.update_available.emit(release, changelog_html or _load_local_changelog_html())
            else:
                self.up_to_date.emit()
        except requests.RequestException as exc:
            self.error.emit(f"Could not check for updates: {exc}")
        except Exception as exc:
            self.error.emit(f"Update check failed: {exc}")


class UpdateDownloadWorker(QThread):
    progress = pyqtSignal(int, int)
    completed = pyqtSignal(str, str)
    failed = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, asset: ReleaseAsset, destination_path: str, target_path: str):
        super().__init__()
        self.asset = asset
        self.destination_path = destination_path
        self.target_path = target_path
        self.partial_path = destination_path + ".part"
        self._cancel_requested = False

    def cancel(self):
        self._cancel_requested = True

    def _cleanup_partial(self):
        for path in (self.partial_path, self.destination_path):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass

    def _expected_sha256(self) -> Optional[str]:
        digest = (self.asset.digest or "").strip()
        if not digest:
            return None
        if ":" in digest:
            algo, value = digest.split(":", 1)
            if algo.lower() == "sha256" and value:
                return value.strip().lower()
            return None
        return digest.lower()

    def run(self):
        self._cleanup_partial()

        os.makedirs(os.path.dirname(self.destination_path), exist_ok=True)
        expected_sha256 = self._expected_sha256()
        actual_sha256 = hashlib.sha256()
        bytes_downloaded = 0

        try:
            with requests.get(self.asset.download_url, stream=True, timeout=(10, 60)) as response:
                response.raise_for_status()
                total_bytes = int(response.headers.get("Content-Length") or self.asset.size or 0)

                with open(self.partial_path, "wb") as handle:
                    for chunk in response.iter_content(chunk_size=262144):
                        if self._cancel_requested:
                            self._cleanup_partial()
                            self.cancelled.emit()
                            return
                        if not chunk:
                            continue
                        handle.write(chunk)
                        actual_sha256.update(chunk)
                        bytes_downloaded += len(chunk)
                        self.progress.emit(bytes_downloaded, total_bytes)

            actual_sha_text = actual_sha256.hexdigest().lower()
            if not expected_sha256:
                self._cleanup_partial()
                self.failed.emit("The release asset did not provide a SHA-256 digest, so the update could not be verified safely.")
                return

            if actual_sha_text != expected_sha256:
                self._cleanup_partial()
                self.failed.emit("SHA-256 verification failed. The downloaded update was deleted.")
                return

            os.replace(self.partial_path, self.destination_path)

            if os.name != "nt":
                try:
                    current_mode = os.stat(self.target_path).st_mode if os.path.exists(self.target_path) else None
                    if current_mode is not None:
                        os.chmod(self.destination_path, current_mode)
                    else:
                        os.chmod(self.destination_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
                except OSError:
                    pass

            self.completed.emit(self.destination_path, actual_sha_text)
        except requests.RequestException as exc:
            self._cleanup_partial()
            self.failed.emit(f"Download failed: {exc}")
        except OSError as exc:
            self._cleanup_partial()
            self.failed.emit(f"Could not write the update file: {exc}")
        except Exception as exc:
            self._cleanup_partial()
            self.failed.emit(f"Unexpected updater error: {exc}")


class UpdatePromptDialog(QDialog):
    def __init__(
        self,
        parent,
        current_version: str,
        release: ReleaseInfo,
        changelog_html: str,
        can_auto_apply: bool,
        disable_reason: str = "",
    ):
        super().__init__(parent)
        self.action = "later"
        self.release = release
        self.setWindowTitle("Update Available")
        self.resize(720, 560)

        layout = QVBoxLayout(self)

        title = QLabel(
            f"<h2>QuickADB {html.escape(release.version)} is available</h2>"
            f"<p>You are currently using {html.escape(current_version)}.</p>"
        )
        title.setWordWrap(True)
        layout.addWidget(title)

        if disable_reason:
            note = QLabel(disable_reason)
            note.setWordWrap(True)
            note.setStyleSheet("color: #d97706;")
            layout.addWidget(note)

        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.setHtml(changelog_html or _html_from_release_body(release.body))
        layout.addWidget(browser, 1)

        button_row = QHBoxLayout()
        button_row.addStretch()

        self.download_button = QPushButton("Download Update")
        self.download_button.setEnabled(can_auto_apply)
        self.download_button.clicked.connect(self._choose_download)
        button_row.addWidget(self.download_button)

        self.releases_button = QPushButton("Open Releases")
        self.releases_button.clicked.connect(self._choose_releases)
        button_row.addWidget(self.releases_button)

        self.later_button = QPushButton("Later")
        self.later_button.clicked.connect(self.reject)
        button_row.addWidget(self.later_button)

        layout.addLayout(button_row)

    def _choose_download(self):
        self.action = "download"
        self.accept()

    def _choose_releases(self):
        self.action = "releases"
        self.accept()


class UpdateProgressDialog(QDialog):
    cancel_requested = pyqtSignal()

    def __init__(self, parent, version_text: str):
        super().__init__(parent)
        self.setWindowTitle("Downloading Update")
        self.setModal(True)
        self.setFixedWidth(420)

        layout = QVBoxLayout(self)

        self.label = QLabel(f"Downloading QuickADB {version_text}...")
        self.label.setWordWrap(True)
        layout.addWidget(self.label)

        self.detail = QLabel("Preparing download...")
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        layout.addWidget(self.progress_bar)

        button_row = QHBoxLayout()
        button_row.addStretch()
        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.cancel_requested.emit)
        button_row.addWidget(cancel_button)
        layout.addLayout(button_row)

    def update_progress(self, downloaded: int, total: int):
        if total > 0:
            percent = max(0, min(100, int(downloaded * 100 / total)))
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(percent)
            self.detail.setText(f"{_format_size(downloaded)} / {_format_size(total)}")
        else:
            self.progress_bar.setRange(0, 0)
            self.detail.setText(f"{_format_size(downloaded)} downloaded")


class UpdateManager(QObject):
    log_message = pyqtSignal(str)

    def __init__(
        self,
        parent,
        current_version: str,
        app_name: str,
        repo_owner: str,
        repo_name: str,
        releases_url: str,
        changelog_url_template: str,
        log_callback: Optional[Callable[[str], None]] = None,
    ):
        super().__init__(parent)
        self.parent = parent
        self.current_version = current_version
        self.app_name = app_name
        self.repo_owner = repo_owner
        self.repo_name = repo_name
        self.releases_url = releases_url
        self.changelog_url_template = changelog_url_template
        self.release_api_url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/releases/latest"
        self.log_callback = log_callback

        self.check_worker: Optional[UpdateCheckWorker] = None
        self.download_worker: Optional[UpdateDownloadWorker] = None
        self.progress_dialog: Optional[UpdateProgressDialog] = None
        self.active_release: Optional[ReleaseInfo] = None
        self.active_asset: Optional[ReleaseAsset] = None
        self.active_manual_check = False

        if self.log_callback:
            self.log_message.connect(self.log_callback)

    def check_for_updates(self, manual: bool = False):
        if self.check_worker and self.check_worker.isRunning():
            if manual:
                QMessageBox.information(self.parent, "Update Check", "An update check is already in progress.")
            return

        self.active_manual_check = manual
        self.check_worker = UpdateCheckWorker(self.current_version, self.release_api_url, self.changelog_url_template)
        self.check_worker.update_available.connect(self._on_update_available)
        self.check_worker.up_to_date.connect(self._on_up_to_date)
        self.check_worker.error.connect(self._on_check_error)
        self.check_worker.finished.connect(self._clear_check_worker)
        self.check_worker.start()

    def _clear_check_worker(self):
        self.check_worker = None

    def _clear_download_worker(self):
        self.download_worker = None

    def _on_up_to_date(self):
        if self.active_manual_check:
            QMessageBox.information(self.parent, "No Updates", "You're already using the latest version.")
        else:
            self.log_message.emit("You're using the latest version.")

    def _on_check_error(self, message: str):
        if self.active_manual_check:
            QMessageBox.critical(self.parent, "Update Check Failed", message)
        else:
            self.log_message.emit(f"[Updater] {message}")

    def _current_target_path(self) -> Optional[str]:
        if not getattr(sys, "frozen", False):
            return None
        executable = sys.executable or ""
        if not executable:
            return None
        return os.path.abspath(executable)

    def _can_self_update(self) -> tuple[bool, str]:
        target_path = self._current_target_path()
        if not target_path:
            return False, "Automatic updating is only available in packaged QuickADB releases."

        if not os.path.isfile(target_path):
            return False, "The current executable could not be located for automatic updating."

        target_dir = os.path.dirname(target_path)
        try:
            with tempfile.NamedTemporaryFile(dir=target_dir, prefix=".quickadb_write_test_", delete=True):
                pass
        except OSError:
            return False, "QuickADB does not have write access to its current install folder, so automatic replacement is unavailable here."

        return True, ""

    def _select_release_asset(self, release: ReleaseInfo) -> tuple[Optional[ReleaseAsset], str]:
        if not release.assets:
            return None, "No downloadable assets were attached to the latest GitHub release."

        current_target = self._current_target_path()
        current_name = os.path.basename(current_target).lower() if current_target else ""
        if current_name:
            for asset in release.assets:
                if asset.name.lower() == current_name:
                    return asset, ""

        system_name = platform.system()
        candidates: list[ReleaseAsset] = []
        for asset in release.assets:
            name = asset.name.lower()
            if system_name == "Windows" and name.endswith(".exe"):
                candidates.append(asset)
            elif system_name == "Linux" and name.endswith(".appimage"):
                candidates.append(asset)
            elif system_name == "Darwin" and not name.endswith(".exe") and not name.endswith(".appimage"):
                if "mac" in name or "darwin" in name or "." not in asset.name:
                    candidates.append(asset)

        if len(candidates) == 1:
            return candidates[0], ""
        if len(candidates) > 1:
            return candidates[0], ""

        return None, f"No matching release asset was found for {system_name}."

    def _download_root(self) -> str:
        return os.path.join(tempfile.gettempdir(), "quickadb-updater")

    def _download_destination(self, release: ReleaseInfo, asset: ReleaseAsset) -> str:
        release_dir = os.path.join(self._download_root(), release.version.lstrip("vV") or release.version)
        return os.path.join(release_dir, asset.name)

    def _on_update_available(self, release: ReleaseInfo, changelog_html: str):
        self.active_release = release

        can_update, disable_reason = self._can_self_update()
        asset, asset_reason = self._select_release_asset(release)
        if not asset and asset_reason:
            disable_reason = asset_reason if not disable_reason else f"{disable_reason}\n\n{asset_reason}"
        self.active_asset = asset

        self.log_message.emit(f"[Updater] Update available: {release.version}")

        dialog = UpdatePromptDialog(
            self.parent,
            self.current_version,
            release,
            changelog_html,
            can_auto_apply=bool(can_update and asset),
            disable_reason=disable_reason,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        if dialog.action == "releases":
            open_url_safe(release.html_url or self.releases_url)
            return

        if dialog.action == "download":
            if not asset:
                QMessageBox.warning(self.parent, "Update Unavailable", asset_reason or "No compatible download was found.")
                return
            self._start_download(release, asset)

    def _start_download(self, release: ReleaseInfo, asset: ReleaseAsset):
        if self.download_worker and self.download_worker.isRunning():
            return

        target_path = self._current_target_path()
        if not target_path:
            QMessageBox.warning(self.parent, "Automatic Update Unavailable", "Automatic updating only works from a packaged QuickADB build.")
            return

        destination_path = self._download_destination(release, asset)
        try:
            os.makedirs(os.path.dirname(destination_path), exist_ok=True)
        except OSError as exc:
            QMessageBox.critical(self.parent, "Updater Error", f"Could not prepare the update folder: {exc}")
            return

        self.log_message.emit(f"Updating to {release.version}...")
        self.progress_dialog = UpdateProgressDialog(self.parent, release.version)
        self.download_worker = UpdateDownloadWorker(asset, destination_path, target_path)
        self.download_worker.progress.connect(self.progress_dialog.update_progress)
        self.download_worker.completed.connect(self._on_download_complete)
        self.download_worker.failed.connect(self._on_download_failed)
        self.download_worker.cancelled.connect(self._on_download_cancelled)
        self.download_worker.finished.connect(self._clear_download_worker)
        self.progress_dialog.cancel_requested.connect(self.download_worker.cancel)
        self.download_worker.start()
        self.progress_dialog.exec()

    def _close_progress_dialog(self):
        if self.progress_dialog is not None:
            self.progress_dialog.close()
            self.progress_dialog.deleteLater()
            self.progress_dialog = None

    def _on_download_cancelled(self):
        self.download_worker = None
        self._close_progress_dialog()
        self.log_message.emit("[Updater] Update download cancelled.")

    def _on_download_failed(self, message: str):
        self.download_worker = None
        self._close_progress_dialog()
        self.log_message.emit(f"[Updater] {message}")
        retry = QMessageBox.question(
            self.parent,
            "Update Download Failed",
            f"{message}\n\nDo you want to try downloading the update again?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if retry == QMessageBox.StandardButton.Yes and self.active_release and self.active_asset:
            self._start_download(self.active_release, self.active_asset)

    def _on_download_complete(self, downloaded_path: str, sha256_text: str):
        self.download_worker = None
        self._close_progress_dialog()
        self.log_message.emit(f"[Updater] Download complete. SHA-256 verified: {sha256_text[:12]}...")

        restart = QMessageBox.question(
            self.parent,
            "Update Ready",
            f"QuickADB {self.active_release.version if self.active_release else ''} has been downloaded and verified.\n\n"
            "Restart now to finish installing the update?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if restart == QMessageBox.StandardButton.Yes:
            try:
                self._apply_downloaded_update(downloaded_path)
            except Exception as exc:
                QMessageBox.critical(self.parent, "Update Failed", f"Could not start the update helper:\n{exc}")
                self.log_message.emit(f"[Updater] Could not start the update helper: {exc}")
        else:
            self.log_message.emit("[Updater] Verified update downloaded but not applied because restart was declined.")

    def _apply_downloaded_update(self, downloaded_path: str):
        target_path = self._current_target_path()
        if not target_path:
            raise RuntimeError("QuickADB is not running from a packaged executable.")

        script_path = self._create_apply_script(target_path, downloaded_path)
        self._launch_apply_script(script_path)
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _create_apply_script(self, target_path: str, downloaded_path: str) -> str:
        helper_dir = os.path.join(self._download_root(), "helpers")
        os.makedirs(helper_dir, exist_ok=True)

        backup_path = target_path + ".bak"
        target_dir = os.path.dirname(target_path)
        if os.name == "nt":
            script_path = os.path.join(helper_dir, "apply_update.bat")
            script = f"""@echo off
setlocal EnableExtensions
set "TARGET={_bat_escape(target_path)}"
set "DOWNLOAD={_bat_escape(downloaded_path)}"
set "BACKUP={_bat_escape(backup_path)}"
set "TARGET_DIR={_bat_escape(target_dir)}"

for /L %%I in (1,1,60) do (
    move /Y "%TARGET%" "%BACKUP%" >nul 2>&1 && goto :moved
    timeout /t 1 /nobreak >nul
)
exit /b 1

:moved
move /Y "%DOWNLOAD%" "%TARGET%" >nul 2>&1 || goto :restore
timeout /t 1 /nobreak >nul
set "PYINSTALLER_RESET_ENVIRONMENT=1"
set "_PYI_APPLICATION_HOME_DIR="
set "_PYI_ARCHIVE_FILE="
set "_PYI_PARENT_PROCESS_LEVEL="
set "_MEIPASS2="
start "" /D "%TARGET_DIR%" "%TARGET%"
del /F /Q "%~f0" >nul 2>&1
exit /b 0

:restore
move /Y "%BACKUP%" "%TARGET%" >nul 2>&1
del /F /Q "%~f0" >nul 2>&1
exit /b 1
"""
        else:
            script_path = os.path.join(helper_dir, "apply_update.sh")
            target_q = shlex.quote(target_path)
            download_q = shlex.quote(downloaded_path)
            backup_q = shlex.quote(backup_path)
            target_dir_q = shlex.quote(target_dir)
            script = f"""#!/bin/sh
TARGET={target_q}
DOWNLOAD={download_q}
BACKUP={backup_q}
TARGET_DIR={target_dir_q}

i=0
while [ "$i" -lt 60 ]; do
    if mv -f "$TARGET" "$BACKUP" 2>/dev/null; then
        break
    fi
    i=$((i + 1))
    sleep 1
done

if [ ! -f "$BACKUP" ]; then
    exit 1
fi

if mv -f "$DOWNLOAD" "$TARGET"; then
    chmod +x "$TARGET" 2>/dev/null || true
    sleep 1
    export PYINSTALLER_RESET_ENVIRONMENT=1
    unset _PYI_APPLICATION_HOME_DIR
    unset _PYI_ARCHIVE_FILE
    unset _PYI_PARENT_PROCESS_LEVEL
    unset _MEIPASS2
    cd "$TARGET_DIR" || exit 1
    "$TARGET" >/dev/null 2>&1 &
    rm -f "$0"
    exit 0
fi

mv -f "$BACKUP" "$TARGET" 2>/dev/null
rm -f "$0"
exit 1
"""

        with open(script_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(script)

        if os.name != "nt":
            os.chmod(script_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

        return script_path

    def _launch_apply_script(self, script_path: str):
        env = os.environ.copy()
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        for key in ("_PYI_APPLICATION_HOME_DIR", "_PYI_ARCHIVE_FILE", "_PYI_PARENT_PROCESS_LEVEL", "_MEIPASS2"):
            env.pop(key, None)

        if os.name == "nt":
            creationflags = 0
            if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
                creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP
            if hasattr(subprocess, "DETACHED_PROCESS"):
                creationflags |= subprocess.DETACHED_PROCESS
            subprocess.Popen(
                ["cmd.exe", "/c", script_path],
                env=env,
                creationflags=creationflags,
                close_fds=True,
            )
        else:
            subprocess.Popen(
                ["/bin/sh", script_path],
                env=env,
                start_new_session=True,
                close_fds=True,
            )
