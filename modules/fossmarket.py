"""
fossmarket.py - F-Droid-compatible repo browser and installer for QuickADB.


"""

from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial
import html
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse, urlunparse

import requests

from util.devicemanager import DeviceManager
from util.resource import get_root_dir, open_url_safe
from util.thememanager import ThemeManager
from util.toolpaths import ToolPaths

root_dir = get_root_dir()
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from PyQt6.QtCore import QObject, QSize, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap, QTextCursor
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTextBrowser,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


REQUEST_HEADERS = {"User-Agent": "QuickADB-FOSS-Market/1.0"}
PREFERRED_LOCALES = ("en-US", "en-GB", "en") # Since QuickADB is english-only, we can use other english locales and just fallback to en-US if needed
CACHE_DIR = os.path.join(tempfile.gettempdir(), "quickadb_fossmarket") # Cache screenshots and icons to use them on the next run
CACHE_TTL_SECONDS = 60 * 60 * 6
IMAGE_CACHE_TTL_SECONDS = 60 * 60 * 24 * 7
NETWORK_DEBUG_LOGS = False # When True, logs every attempt to fetch icons and screenshots, including cache hits and misses
                           # This is not an in-UI toggle, just for code debugging

@dataclass(slots=True)
class RepoDefinition:
    name: str
    index_url: str
    homepage: str
    enabled: bool = True
    custom: bool = False


@dataclass(slots=True)
class MarketApp:
    app_id: str
    title: str
    package_name: str
    repo_name: str
    repo_index_url: str
    repo_homepage: str
    repo_base_url: str
    summary: str = ""
    description_html: str = ""
    whats_new: str = ""
    version_name: str = "Unknown"
    version_code: str = ""
    license_name: str = "Unknown"
    categories: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    website: str = ""
    source_code: str = ""
    issue_tracker: str = ""
    locale: str = "en-US"
    icon_url: str = ""
    screenshot_urls: list[str] = field(default_factory=list)
    feature_graphic_url: str = ""
    download_url: str = ""
    apk_name: str = ""
    added_ms: int = 0
    updated_ms: int = 0


def _candidate_index_urls(repo_input: str) -> list[str]:
    raw = (repo_input or "").strip()
    if not raw:
        return []

    url = raw.rstrip("/")
    lower = url.lower()
    candidates: list[str] = []

    if lower.endswith(".json"):
        return [url]
    if lower.endswith(".jar"):
        return [url[:-4] + ".json"]

    candidates.append(url + "/index-v1.json")
    if not (lower.endswith("/repo") or lower.endswith("/archive")):
        candidates.append(url + "/repo/index-v1.json")

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate not in seen:
            deduped.append(candidate)
            seen.add(candidate)
    return deduped


def _repo_base_url(index_url: str) -> str:
    parsed = urlparse(index_url)
    path = parsed.path
    if path.endswith("/index-v1.json") or path.endswith("/index-v2.json"):
        path = path.rsplit("/", 1)[0]
    return urlunparse((parsed.scheme, parsed.netloc, path.rstrip("/"), "", "", ""))


def _pick_locale(localized: dict) -> tuple[str, dict]:
    if not isinstance(localized, dict):
        return "en-US", {}

    for locale in PREFERRED_LOCALES:
        value = localized.get(locale)
        if isinstance(value, dict):
            return locale, value

    for locale, value in localized.items():
        if isinstance(value, dict):
            return locale, value

    return "en-US", {}


def _format_timestamp(ms_value: int) -> str:
    if not ms_value:
        return "Unknown"
    try:
        return datetime.utcfromtimestamp(int(ms_value) / 1000).strftime("%Y-%m-%d")
    except Exception:
        return "Unknown"


def _build_asset_url(repo_base_url: str, package_name: str, locale: str, filename: str) -> str:
    return f"{repo_base_url.rstrip('/')}/{package_name}/{locale}/{filename}"


def _build_screenshot_urls(repo_base_url: str, package_name: str, locale: str, filenames: list[str]) -> list[str]:
    urls: list[str] = []
    for filename in filenames:
        name = (filename or "").strip()
        if not name:
            continue
        urls.append(
            f"{repo_base_url.rstrip('/')}/{package_name}/{locale}/phoneScreenshots/{name}"
        )
    return urls


def _screenshot_candidate_urls(url: str) -> list[str]:
    candidates = [url]
    if "/phoneScreenshots/" in url:
        candidates.append(url.replace("/phoneScreenshots/", "/"))

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate and candidate not in seen:
            deduped.append(candidate)
            seen.add(candidate)
    return deduped


def _select_v1_version(package_versions: list[dict], suggested_version_code: str) -> dict:
    if not package_versions:
        return {}

    suggested = str(suggested_version_code or "")
    if suggested:
        for version in package_versions:
            if str(version.get("versionCode", "")) == suggested:
                return version

    def _version_sort_key(entry: dict) -> tuple[int, str]:
        raw_code = entry.get("versionCode", 0)
        try:
            numeric_code = int(raw_code)
        except (TypeError, ValueError):
            numeric_code = 0
        return numeric_code, str(entry.get("versionName", ""))

    return max(package_versions, key=_version_sort_key)


def _extract_permissions(version_record: dict) -> list[str]:
    names: list[str] = []
    for key in ("usesPermission", "usesPermissionSdk23"):
        raw_permissions = version_record.get(key) or []
        for entry in raw_permissions:
            if isinstance(entry, str):
                names.append(entry)
            elif isinstance(entry, list) and entry:
                names.append(str(entry[0]))
            elif isinstance(entry, dict):
                permission_name = (
                    entry.get("name")
                    or entry.get("permission")
                    or entry.get("usesPermission")
                )
                if permission_name:
                    names.append(str(permission_name))

    deduped: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name not in seen:
            deduped.append(name)
            seen.add(name)
    return deduped


def _description_html(app_entry: MarketApp) -> str:
    parts: list[str] = []
    description = (app_entry.description_html or "").strip()
    if description:
        parts.append(description)
    else:
        parts.append("<p>No description published for this app.</p>")

    if app_entry.whats_new.strip():
        whats_new = html.escape(app_entry.whats_new).replace("\n", "<br>")
        parts.append(f"<hr><h3>What's New</h3><p>{whats_new}</p>")

    return "".join(parts)


def _ensure_cache_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


def _ensure_image_cache_dir(kind: str):
    os.makedirs(os.path.join(CACHE_DIR, kind), exist_ok=True)


def _repo_cache_path(repo: RepoDefinition) -> str:
    token_source = f"{repo.name}|{repo.index_url}".encode("utf-8", errors="ignore")
    token = hashlib.sha256(token_source).hexdigest()[:16]
    return os.path.join(CACHE_DIR, f"catalog_{token}.json")


def _image_cache_path(kind: str, url: str) -> str:
    suffix = os.path.splitext(urlparse(url).path)[1].lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
        suffix = ".img"
    token = hashlib.sha256(url.encode("utf-8", errors="ignore")).hexdigest()
    return os.path.join(CACHE_DIR, kind, f"{token}{suffix}")


def _write_repo_cache(repo: RepoDefinition, apps: list[MarketApp]):
    _ensure_cache_dir()
    cache_path = _repo_cache_path(repo)
    temp_path = cache_path + ".tmp"
    payload = {
        "timestamp": time.time(),
        "repo_name": repo.name,
        "repo_url": repo.index_url,
        "apps": [asdict(app) for app in apps],
    }
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    os.replace(temp_path, cache_path)


def _write_url_cache_bytes(kind: str, url: str, data: bytes):
    if not url or not data:
        return
    _ensure_image_cache_dir(kind)
    cache_path = _image_cache_path(kind, url)
    temp_path = cache_path + ".tmp"
    with open(temp_path, "wb") as handle:
        handle.write(data)
    os.replace(temp_path, cache_path)


def _read_repo_cache(repo: RepoDefinition, max_age_seconds: Optional[int] = None) -> tuple[list[MarketApp], Optional[float]]:
    cache_path = _repo_cache_path(repo)
    if not os.path.exists(cache_path):
        return [], None

    try:
        with open(cache_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        timestamp = float(payload.get("timestamp") or 0.0)
        age_seconds = max(0.0, time.time() - timestamp) if timestamp else None
        if age_seconds is not None and max_age_seconds is not None and age_seconds > max_age_seconds:
            return [], age_seconds

        apps_raw = payload.get("apps") or []
        apps: list[MarketApp] = []
        for item in apps_raw:
            if isinstance(item, dict):
                apps.append(MarketApp(**item))
        return apps, age_seconds
    except Exception:
        return [], None


def _read_url_cache_bytes(kind: str, url: str, max_age_seconds: Optional[int] = None) -> Optional[bytes]:
    if not url:
        return None
    cache_path = _image_cache_path(kind, url)
    if not os.path.exists(cache_path):
        return None
    try:
        if max_age_seconds is not None:
            age_seconds = max(0.0, time.time() - os.path.getmtime(cache_path))
            if age_seconds > max_age_seconds:
                return None
        with open(cache_path, "rb") as handle:
            return handle.read()
    except Exception:
        return None


def _format_age(age_seconds: Optional[float]) -> str:
    if age_seconds is None:
        return "unknown age"
    if age_seconds < 60:
        return "moments old"
    if age_seconds < 3600:
        return f"{int(age_seconds // 60)}m old"
    if age_seconds < 86400:
        return f"{int(age_seconds // 3600)}h old"
    return f"{int(age_seconds // 86400)}d old"


def _short_url(url: str) -> str:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if not parts:
        return url
    if len(parts) <= 3:
        return "/".join(parts)
    return "/".join(parts[-3:])


class CatalogWorker(QThread):
    catalog_ready = pyqtSignal(list)
    log_message = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, repos: list[RepoDefinition]):
        super().__init__()
        self.repos = repos

    def run(self):
        try:
            enabled_repos = [repo for repo in self.repos if repo.enabled]
            if not enabled_repos:
                self.log_message.emit("No repositories are enabled.")
                self.catalog_ready.emit([])
                return

            all_apps: list[MarketApp] = []
            for repo in enabled_repos:
                try:
                    repo_apps = self._load_repo(repo)
                    _write_repo_cache(repo, repo_apps)
                    all_apps.extend(repo_apps)
                    self.log_message.emit(f"{repo.name}: loaded {len(repo_apps)} apps.")
                except Exception as exc:
                    cached_apps, cache_age = _read_repo_cache(repo)
                    if cached_apps:
                        all_apps.extend(cached_apps)
                        self.log_message.emit(
                            f"{repo.name}: refresh failed ({exc}), using cached catalog ({_format_age(cache_age)})."
                        )
                    else:
                        self.log_message.emit(f"{repo.name}: {exc}")

            all_apps.sort(key=lambda app: ((app.title or app.package_name).lower(), app.package_name.lower()))
            self.catalog_ready.emit(all_apps)
        except Exception as exc:
            self.failed.emit(f"Catalog loading failed: {exc}")

    def _load_repo(self, repo: RepoDefinition) -> list[MarketApp]:
        last_error: Optional[Exception] = None

        for candidate in _candidate_index_urls(repo.index_url):
            try:
                self.log_message.emit(f"{repo.name}: fetching {candidate}")
                response = requests.get(candidate, headers=REQUEST_HEADERS, timeout=(10, 90))
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise RuntimeError("Repo index did not return a JSON object.")

                apps = self._parse_v1_index(repo, candidate, payload)
                if apps:
                    return apps
                self.log_message.emit(f"{repo.name}: no installable single-APK entries found in {candidate}")
                return []
            except Exception as exc:
                last_error = exc

        if last_error is None:
            raise RuntimeError("No valid repo URL candidates were available.")
        raise RuntimeError(f"could not load index ({last_error})")

    def _parse_v1_index(self, repo: RepoDefinition, resolved_index_url: str, payload: dict) -> list[MarketApp]:
        apps_data = payload.get("apps")
        package_map = payload.get("packages")
        if not isinstance(apps_data, list) or not isinstance(package_map, dict):
            raise RuntimeError("Unsupported repo index format. Expected index-v1.json style data.")

        repo_base_url = _repo_base_url(resolved_index_url)
        results: list[MarketApp] = []
        for app_data in apps_data:
            if not isinstance(app_data, dict):
                continue

            package_name = str(app_data.get("packageName") or "").strip()
            if not package_name:
                continue

            package_versions = package_map.get(package_name) or []
            if not isinstance(package_versions, list):
                continue

            version_record = _select_v1_version(
                package_versions,
                str(app_data.get("suggestedVersionCode") or ""),
            )
            apk_name = str(version_record.get("apkName") or "").strip()
            if not apk_name.lower().endswith(".apk"):
                continue

            localized = app_data.get("localized") or {}
            locale, locale_data = _pick_locale(localized)

            icon_name = str(locale_data.get("icon") or "").strip()
            feature_graphic = str(locale_data.get("featureGraphic") or "").strip()
            screenshot_files = locale_data.get("phoneScreenshots") or []
            if not isinstance(screenshot_files, list):
                screenshot_files = []

            app_id = f"{repo.name}:{package_name}"
            results.append(
                MarketApp(
                    app_id=app_id,
                    title=str(locale_data.get("name") or package_name),
                    package_name=package_name,
                    repo_name=repo.name,
                    repo_index_url=resolved_index_url,
                    repo_homepage=repo.homepage or repo_base_url,
                    repo_base_url=repo_base_url,
                    summary=str(locale_data.get("summary") or ""),
                    description_html=str(locale_data.get("description") or ""),
                    whats_new=str(locale_data.get("whatsNew") or ""),
                    version_name=str(
                        app_data.get("suggestedVersionName")
                        or version_record.get("versionName")
                        or "Unknown"
                    ),
                    version_code=str(
                        app_data.get("suggestedVersionCode")
                        or version_record.get("versionCode")
                        or ""
                    ),
                    license_name=str(app_data.get("license") or "Unknown"),
                    categories=[str(value) for value in (app_data.get("categories") or []) if value],
                    permissions=_extract_permissions(version_record),
                    website=str(app_data.get("webSite") or ""),
                    source_code=str(app_data.get("sourceCode") or ""),
                    issue_tracker=str(app_data.get("issueTracker") or ""),
                    locale=locale,
                    icon_url=_build_asset_url(repo_base_url, package_name, locale, icon_name) if icon_name else "",
                    screenshot_urls=_build_screenshot_urls(repo_base_url, package_name, locale, screenshot_files),
                    feature_graphic_url=(
                        _build_asset_url(repo_base_url, package_name, locale, feature_graphic)
                        if feature_graphic else ""
                    ),
                    download_url=f"{repo_base_url.rstrip('/')}/{apk_name}",
                    apk_name=apk_name,
                    added_ms=int(app_data.get("added") or 0),
                    updated_ms=int(app_data.get("lastUpdated") or 0),
                )
            )

        return results


class IconFetchManager(QObject):
    icon_ready = pyqtSignal(str, object)
    icon_failed = pyqtSignal(str, object, str)
    log_message = pyqtSignal(str)
    CACHE_HITS_PER_TICK = 8

    def __init__(self, concurrency: int, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.concurrency = max(1, concurrency)
        self.network_manager = QNetworkAccessManager(self)
        self.pending_urls: deque[str] = deque()
        self.pending_map: dict[str, list[str]] = {}
        self.inflight_replies: dict[QNetworkReply, str] = {}
        self.inflight_url_map: dict[str, list[str]] = {}
        self.resume_timer = QTimer(self)
        self.resume_timer.setSingleShot(True)
        self.resume_timer.timeout.connect(self._start_next_requests)

    def replace_pending_requests(self, requests_to_load: list[tuple[str, str]]):
        pending_urls: deque[str] = deque()
        pending_map: dict[str, list[str]] = {}
        reused_inflight = 0

        for app_id, url in requests_to_load:
            if not url:
                continue

            inflight_ids = self.inflight_url_map.get(url)
            if inflight_ids is not None:
                if app_id not in inflight_ids:
                    inflight_ids.append(app_id)
                    reused_inflight += 1
                continue

            bucket = pending_map.setdefault(url, [])
            if app_id not in bucket:
                bucket.append(app_id)
            if len(bucket) == 1:
                pending_urls.append(url)

        self.pending_urls = pending_urls
        self.pending_map = pending_map
        if NETWORK_DEBUG_LOGS:
            self.log_message.emit(
                f"[Icons] Queue updated: {len(self.pending_urls)} pending URL(s), "
                f"{len(self.inflight_replies)} inflight, {reused_inflight} reused."
            )
        self._start_next_requests()

    def _start_next_requests(self):
        cache_hits_processed = 0
        while len(self.inflight_replies) < self.concurrency and self.pending_urls:
            url = self.pending_urls.popleft()
            app_ids = self.pending_map.pop(url, [])
            if not app_ids:
                continue

            cached_bytes = _read_url_cache_bytes("icons", url, IMAGE_CACHE_TTL_SECONDS)
            if cached_bytes is not None:
                if NETWORK_DEBUG_LOGS:
                    self.log_message.emit(
                        f"[Icons] Cache hit: {_short_url(url)} -> {len(app_ids)} tile(s)."
                    )
                for app_id in app_ids:
                    self.icon_ready.emit(app_id, cached_bytes)
                cache_hits_processed += 1
                if cache_hits_processed >= self.CACHE_HITS_PER_TICK:
                    if self.pending_urls and not self.resume_timer.isActive():
                        self.resume_timer.start(0)
                    break
                continue

            request = QNetworkRequest(QUrl(url))
            request.setRawHeader(b"User-Agent", REQUEST_HEADERS["User-Agent"].encode("utf-8"))
            reply = self.network_manager.get(request)
            reply.setProperty("debug_url", url)

            timeout = QTimer(reply)
            timeout.setSingleShot(True)
            timeout.setInterval(12000)
            timeout.timeout.connect(reply.abort)
            timeout.start()

            self.inflight_replies[reply] = url
            self.inflight_url_map[url] = list(app_ids)
            if NETWORK_DEBUG_LOGS:
                self.log_message.emit(
                    f"[Icons] Request started: {_short_url(url)} for {len(app_ids)} tile(s). "
                    f"In-flight: {len(self.inflight_replies)}/{self.concurrency}"
                )
            reply.finished.connect(lambda reply=reply: self._on_reply_finished(reply))

    def _on_reply_finished(self, reply: QNetworkReply):
        url = self.inflight_replies.pop(reply, "")
        app_ids = self.inflight_url_map.pop(url, [])

        try:
            if reply.error() == QNetworkReply.NetworkError.NoError:
                data = bytes(reply.readAll())
                if data:
                    _write_url_cache_bytes("icons", url, data)
                    if NETWORK_DEBUG_LOGS:
                        self.log_message.emit(
                            f"[Icons] Request finished: {_short_url(url)} ({len(data)} byte(s)) "
                            f"for {len(app_ids)} tile(s)."
                        )
                    for app_id in app_ids:
                        self.icon_ready.emit(app_id, data)
                elif NETWORK_DEBUG_LOGS:
                    self.log_message.emit(
                        f"[Icons] Empty response: {_short_url(url)}."
                    )
                if not data:
                    self.icon_failed.emit(url, app_ids, "Empty response")
            elif NETWORK_DEBUG_LOGS:
                self.log_message.emit(
                    f"[Icons] Request failed: {_short_url(url)} - {reply.errorString()}."
                )
                self.icon_failed.emit(url, app_ids, reply.errorString())
            else:
                self.icon_failed.emit(url, app_ids, reply.errorString())
        finally:
            reply.deleteLater()
            self._start_next_requests()


class ScreenshotLoaderWorker(QThread):
    screenshots_ready = pyqtSignal(str, object)
    log_message = pyqtSignal(str)

    def __init__(self, app_id: str, urls: list[str]):
        super().__init__()
        self.app_id = app_id
        self.urls = urls

    def run(self):
        payload_map: dict[int, bytes] = {}
        if NETWORK_DEBUG_LOGS:
            self.log_message.emit(
                f"[Screens] Starting screenshot load for {self.app_id} with {len(self.urls)} URL(s)."
            )

        if self.urls:
            with ThreadPoolExecutor(max_workers=max(1, len(self.urls))) as executor:
                future_map = {
                    executor.submit(self._download_bytes, url): index
                    for index, url in enumerate(self.urls)
                }
                for future in as_completed(future_map):
                    index = future_map[future]
                    try:
                        blob = future.result()
                    except Exception as exc:
                        if NETWORK_DEBUG_LOGS:
                            self.log_message.emit(
                                f"[Screens] Worker exception for {self.app_id}: {exc}"
                            )
                        blob = None
                    if blob is not None:
                        payload_map[index] = blob

        payloads = [payload_map[index] for index in range(len(self.urls)) if index in payload_map]

        if NETWORK_DEBUG_LOGS:
            self.log_message.emit(
                f"[Screens] Finished screenshot load for {self.app_id}: {len(payloads)} image(s) ready."
            )
        self.screenshots_ready.emit(self.app_id, payloads)

    def _download_bytes(self, url: str) -> Optional[bytes]:
        for candidate in _screenshot_candidate_urls(url):
            cached_bytes = _read_url_cache_bytes("screenshots", candidate, IMAGE_CACHE_TTL_SECONDS)
            if cached_bytes is not None:
                if NETWORK_DEBUG_LOGS:
                    self.log_message.emit(
                        f"[Screens] Cache hit: {_short_url(candidate)}."
                    )
                return cached_bytes
            try:
                if NETWORK_DEBUG_LOGS:
                    self.log_message.emit(
                        f"[Screens] Request started: {_short_url(candidate)}."
                    )
                response = requests.get(candidate, headers=REQUEST_HEADERS, timeout=(10, 35))
                response.raise_for_status()
                data = response.content
                _write_url_cache_bytes("screenshots", candidate, data)
                if NETWORK_DEBUG_LOGS:
                    self.log_message.emit(
                        f"[Screens] Request finished: {_short_url(candidate)} ({len(data)} byte(s))."
                    )
                return data
            except Exception as exc:
                if NETWORK_DEBUG_LOGS:
                    self.log_message.emit(
                        f"[Screens] Request failed: {_short_url(candidate)} - {exc}."
                    )
                continue
        return None


class InstallWorker(QThread):
    log_message = pyqtSignal(str)
    progress_changed = pyqtSignal(int)
    status_changed = pyqtSignal(str)
    finished_action = pyqtSignal(bool, str)

    def __init__(self, app_entry: MarketApp, download_dir: str, keep_apk: bool):
        super().__init__()
        self.app_entry = app_entry
        self.download_dir = download_dir
        self.keep_apk = keep_apk

    def run(self):
        apk_path = ""
        temporary_download = False

        try:
            if not self.app_entry.download_url:
                self.finished_action.emit(False, "No APK download URL was published for this app.")
                return

            if self.keep_apk:
                if not self.download_dir:
                    self.finished_action.emit(False, "Choose a download directory if you want to keep APKs.")
                    return
                os.makedirs(self.download_dir, exist_ok=True)
                apk_name = self.app_entry.apk_name or f"{self.app_entry.package_name}.apk"
                apk_path = os.path.join(self.download_dir, apk_name)
            else:
                suffix = os.path.splitext(self.app_entry.apk_name or self.app_entry.package_name)[1] or ".apk"
                fd, apk_path = tempfile.mkstemp(prefix="quickadb_market_", suffix=suffix)
                os.close(fd)
                temporary_download = True

            self.status_changed.emit("Downloading APK...")
            self.log_message.emit(f"Downloading {self.app_entry.title}...")
            self._download_apk(apk_path)

            self.status_changed.emit("Installing on device...")
            self.log_message.emit("Running adb install -r...")
            result = self._install_apk(apk_path)
            if result.returncode != 0:
                output = (result.stderr or result.stdout or "").strip()
                if temporary_download and os.path.exists(apk_path):
                    os.remove(apk_path)
                self.finished_action.emit(
                    False,
                    output or "adb install returned a non-zero exit code.",
                )
                return

            if temporary_download and os.path.exists(apk_path):
                os.remove(apk_path)

            if self.keep_apk:
                message = f"{self.app_entry.title} installed successfully. APK kept at {apk_path}"
            else:
                message = f"{self.app_entry.title} installed successfully. Temporary APK was cleaned up."
            self.finished_action.emit(True, message)
        except Exception as exc:
            if apk_path and os.path.exists(apk_path):
                try:
                    os.remove(apk_path)
                except OSError:
                    pass
            self.finished_action.emit(False, f"Install failed: {exc}")

    def _download_apk(self, apk_path: str):
        with requests.get(
            self.app_entry.download_url,
            headers=REQUEST_HEADERS,
            stream=True,
            timeout=(15, 180),
        ) as response:
            response.raise_for_status()
            total_bytes = int(response.headers.get("content-length") or 0)
            downloaded = 0

            if total_bytes > 0:
                self.progress_changed.emit(0)
            else:
                self.progress_changed.emit(-1)

            with open(apk_path, "wb") as handle:
                for chunk in response.iter_content(chunk_size=262144):
                    if not chunk:
                        continue
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if total_bytes > 0:
                        percent = min(100, int(downloaded * 100 / total_bytes))
                        self.progress_changed.emit(percent)

    def _install_apk(self, apk_path: str) -> subprocess.CompletedProcess:
        adb_exe = ToolPaths.instance().adb
        command = [adb_exe] + DeviceManager.instance().serial_args() + ["install", "-r", apk_path]

        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW

        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            creationflags=creationflags,
        )


class FossMarketWindow(QMainWindow):
    ICON_SIZE = QSize(96, 96)
    GRID_SIZE = QSize(168, 170)
    DETAIL_ICON_SIZE = QSize(124, 124)
    SCREENSHOT_HEIGHT = 240
    ICON_PREFETCH_LIMIT = 180
    ICON_CONCURRENCY = 25 # Amount of how many icons will be fetched per batch
    ICON_DECODE_BATCH_SIZE = 18
    ICON_VIEWPORT_REFRESH_MS = 3000 # Minimum delay between automatic icon loading batches triggered by viewport changes (scrolling, resizing)
    ICON_FAILURE_COOLDOWN_SECONDS = 45
    SCREENSHOT_LIMIT = 6

    def __init__(self, auto_refresh: bool = True):
        super().__init__()

        self.repos: list[RepoDefinition] = self._default_repos()
        self.all_apps: list[MarketApp] = []
        self.filtered_apps: list[MarketApp] = []
        self.app_lookup: dict[str, MarketApp] = {}
        self.grid_items: dict[str, QListWidgetItem] = {}

        self.icon_cache: dict[str, QIcon] = {}
        self.screenshot_cache: dict[str, list[QPixmap]] = {}
        self.pending_icon_payloads: dict[str, bytes] = {}
        self.pending_icon_targets: dict[str, set[str]] = {}
        self.pending_icon_decodes: deque[str] = deque()

        self.catalog_thread: Optional[CatalogWorker] = None
        self.icon_fetcher = IconFetchManager(self.ICON_CONCURRENCY, self)
        self.screenshot_thread: Optional[ScreenshotLoaderWorker] = None
        self.install_thread: Optional[InstallWorker] = None

        self.pending_screenshot_app_id: Optional[str] = None
        self.clicked_screenshot_app_ids: set[str] = set()
        self.icon_retry_after: dict[str, float] = {}
        self.icon_failure_counts: dict[str, int] = {}

        self.download_dir = ""
        self.placeholder_icon = self._build_placeholder_icon()
        self.icon_queue_timer = QTimer(self)
        self.icon_queue_timer.setSingleShot(True)
        self.icon_queue_timer.setInterval(self.ICON_VIEWPORT_REFRESH_MS)
        self.icon_queue_timer.timeout.connect(self._queue_icon_loading)
        self.icon_decode_timer = QTimer(self)
        self.icon_decode_timer.setSingleShot(True)
        self.icon_decode_timer.timeout.connect(self._flush_icon_decode_queue)
        self.icon_fetcher.icon_ready.connect(self._on_icon_ready)
        self.icon_fetcher.icon_failed.connect(self._on_icon_failed)
        self.icon_fetcher.log_message.connect(partial(self.log, color="#89CFF0"))

        self._build_ui()
        ThemeManager.apply_theme(self)
        self._populate_repo_list()
        self._apply_download_mode_ui()
        self._auto_refresh_requested = auto_refresh
        QTimer.singleShot(0, self._start_initial_load)

    def _build_ui(self):
        self.setWindowTitle("QuickADB - FOSS App Market")
        self.resize(1350, 750)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        root_layout = QVBoxLayout(central_widget)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.setSpacing(10)

        header_layout = QHBoxLayout()
        title_label = QLabel("FOSS App Market")
        title_label.setStyleSheet("font-size: 20px; font-weight: 600;")
        header_layout.addWidget(title_label)

        header_layout.addStretch()

        self.result_label = QLabel("No catalog loaded")
        header_layout.addWidget(self.result_label)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search apps, packages or categories")
        self.search_input.setMinimumWidth(300)
        self.search_input.textChanged.connect(self.apply_filters)
        header_layout.addWidget(self.search_input)

        self.refresh_button = QPushButton("Refresh Catalog")
        self.refresh_button.clicked.connect(self.refresh_catalog)
        header_layout.addWidget(self.refresh_button)

        root_layout.addLayout(header_layout)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        root_layout.addWidget(splitter, 1)

        left_splitter = QSplitter(Qt.Orientation.Vertical)
        left_splitter.setChildrenCollapsible(False)
        splitter.addWidget(left_splitter)

        controls_panel = QWidget()
        controls_layout = QVBoxLayout(controls_panel)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(10)

        repo_group = QGroupBox("Repositories")
        repo_layout = QVBoxLayout(repo_group)
        self.repo_list = QListWidget()
        self.repo_list.itemChanged.connect(self._on_repo_item_changed)
        repo_layout.addWidget(self.repo_list)

        repo_buttons = QHBoxLayout()
        add_repo_button = QPushButton("Add Repo")
        add_repo_button.clicked.connect(self.add_custom_repo)
        repo_buttons.addWidget(add_repo_button)

        remove_repo_button = QPushButton("Remove Repo")
        remove_repo_button.clicked.connect(self.remove_selected_repo)
        repo_buttons.addWidget(remove_repo_button)

        reload_repo_button = QPushButton("Reload")
        reload_repo_button.clicked.connect(self.refresh_catalog)
        repo_buttons.addWidget(reload_repo_button)
        repo_layout.addLayout(repo_buttons)
        controls_layout.addWidget(repo_group)

        install_group = QGroupBox("Install Behavior")
        install_layout = QVBoxLayout(install_group)

        self.keep_apk_checkbox = QCheckBox("Keep downloaded APK")
        self.keep_apk_checkbox.toggled.connect(self._apply_download_mode_ui)
        install_layout.addWidget(self.keep_apk_checkbox)

        self.download_path_label = QLabel("")
        self.download_path_label.setWordWrap(True)
        install_layout.addWidget(self.download_path_label)

        download_buttons = QHBoxLayout()
        self.choose_download_button = QPushButton("Choose Folder")
        self.choose_download_button.clicked.connect(self.select_download_dir)
        download_buttons.addWidget(self.choose_download_button)

        clear_download_button = QPushButton("Clear Folder")
        clear_download_button.clicked.connect(self.clear_download_dir)
        download_buttons.addWidget(clear_download_button)
        install_layout.addLayout(download_buttons)

        self.install_progress = QProgressBar()
        self.install_progress.setVisible(False)
        self.install_progress.setTextVisible(True)
        self.install_progress.setRange(0, 100)
        install_layout.addWidget(self.install_progress)


        controls_layout.addWidget(install_group)

        left_splitter.addWidget(controls_panel)

        details_scroll = QScrollArea()
        details_scroll.setWidgetResizable(True)
        details_container = QWidget()
        details_scroll.setWidget(details_container)
        details_layout = QVBoxLayout(details_container)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setSpacing(10)

        header_card = QFrame()
        header_card_layout = QHBoxLayout(header_card)
        header_card_layout.setContentsMargins(8, 8, 8, 8)

        self.detail_icon_label = QLabel()
        self.detail_icon_label.setFixedSize(self.DETAIL_ICON_SIZE)
        self.detail_icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_card_layout.addWidget(self.detail_icon_label)

        title_block = QVBoxLayout()
        self.title_label = QLabel("Select an app")
        self.title_label.setStyleSheet("font-size: 18px; font-weight: 600;")
        self.title_label.setWordWrap(True)
        title_block.addWidget(self.title_label)

        self.summary_label = QLabel("Browse the catalog on the right to see app details here.")
        self.summary_label.setWordWrap(True)
        title_block.addWidget(self.summary_label)
        header_card_layout.addLayout(title_block, 1)
        details_layout.addWidget(header_card)

        metadata_group = QGroupBox("Details")
        metadata_layout = QFormLayout(metadata_group)
        metadata_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.package_value = QLabel("-")
        self.package_value.setWordWrap(True)
        metadata_layout.addRow("Package", self.package_value)

        self.repo_value = QLabel("-")
        self.repo_value.setWordWrap(True)
        metadata_layout.addRow("Repository", self.repo_value)

        self.version_value = QLabel("-")
        self.version_value.setWordWrap(True)
        metadata_layout.addRow("Version", self.version_value)

        self.updated_value = QLabel("-")
        metadata_layout.addRow("Updated", self.updated_value)

        self.license_value = QLabel("-")
        self.license_value.setWordWrap(True)
        metadata_layout.addRow("License", self.license_value)

        self.categories_value = QLabel("-")
        self.categories_value.setWordWrap(True)
        metadata_layout.addRow("Categories", self.categories_value)

        self.permissions_value = QLabel("-")
        self.permissions_value.setWordWrap(True)
        metadata_layout.addRow("Permissions", self.permissions_value)

        details_layout.addWidget(metadata_group)

        action_row = QHBoxLayout()
        self.install_button = QPushButton("Download && Install")
        self.install_button.clicked.connect(self.install_selected_app)
        self.install_button.setEnabled(False)
        action_row.addWidget(self.install_button)

        self.website_button = QPushButton("Website")
        self.website_button.clicked.connect(partial(self._open_app_url, "website"))
        self.website_button.setEnabled(False)
        action_row.addWidget(self.website_button)

        self.source_button = QPushButton("Source")
        self.source_button.clicked.connect(partial(self._open_app_url, "source_code"))
        self.source_button.setEnabled(False)
        action_row.addWidget(self.source_button)

        self.issues_button = QPushButton("Issues")
        self.issues_button.clicked.connect(partial(self._open_app_url, "issue_tracker"))
        self.issues_button.setEnabled(False)
        action_row.addWidget(self.issues_button)
        details_layout.addLayout(action_row)

        self.description_browser = QTextBrowser()
        self.description_browser.setOpenExternalLinks(True)
        self.description_browser.setMinimumHeight(220)
        details_layout.addWidget(self.description_browser)

        screenshot_group = QGroupBox("Screenshots")
        screenshot_layout = QVBoxLayout(screenshot_group)
        self.screenshot_scroll = QScrollArea()
        self.screenshot_scroll.setWidgetResizable(True)
        self.screenshot_scroll.setMinimumHeight(self.SCREENSHOT_HEIGHT + 28)
        self.screenshot_container = QWidget()
        self.screenshot_layout = QHBoxLayout(self.screenshot_container)
        self.screenshot_layout.setContentsMargins(4, 4, 4, 4)
        self.screenshot_layout.setSpacing(10)
        self.screenshot_scroll.setWidget(self.screenshot_container)
        screenshot_layout.addWidget(self.screenshot_scroll)
        details_layout.addWidget(screenshot_group)
        details_layout.addStretch()

        left_splitter.addWidget(details_scroll)

        log_group = QGroupBox("Market Log")
        log_layout = QVBoxLayout(log_group)
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMinimumHeight(140)
        log_layout.addWidget(self.log_output)
        left_splitter.addWidget(log_group)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        grid_hint = QLabel("Apps")
        grid_hint.setStyleSheet("font-size: 16px; font-weight: 600;")
        right_layout.addWidget(grid_hint)

        self.app_grid = QListWidget()
        self.app_grid.setViewMode(QListView.ViewMode.IconMode)
        self.app_grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.app_grid.setMovement(QListView.Movement.Static)
        self.app_grid.setWrapping(True)
        self.app_grid.setWordWrap(True)
        self.app_grid.setSpacing(10)
        self.app_grid.setIconSize(self.ICON_SIZE)
        self.app_grid.setGridSize(self.GRID_SIZE)
        self.app_grid.setUniformItemSizes(True)
        self.app_grid.currentItemChanged.connect(self._on_app_selection_changed)
        self.app_grid.itemClicked.connect(self._on_app_clicked)
        self.app_grid.verticalScrollBar().valueChanged.connect(self._schedule_icon_queue)
        self.app_grid.horizontalScrollBar().valueChanged.connect(self._schedule_icon_queue)
        right_layout.addWidget(self.app_grid, 1)

        splitter.addWidget(right_panel)
        splitter.setSizes([420, 980])
        left_splitter.setSizes([270, 470, 180])

        self.statusBar().showMessage("Ready")
        self._reset_details()

    def _default_repos(self) -> list[RepoDefinition]:
        return [
            RepoDefinition(
                name="F-Droid",
                index_url="https://f-droid.org/repo/index-v1.json",
                homepage="https://f-droid.org/",
                enabled=True,
            ),
            RepoDefinition(
                name="IzzyOnDroid",
                index_url="https://apt.izzysoft.de/fdroid/repo/index-v1.json",
                homepage="https://apt.izzysoft.de/fdroid/",
                enabled=False, # Server issues? IzzyOnDroid usually returns errors on icons or even the index itself
            ),                 # Almost all apps are also on the main repo anyways, so we'll keep this here but disable it as default
        ]

    def _build_placeholder_icon(self) -> QIcon:
        pixmap = QPixmap(96, 96)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor("#2b4259"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(8, 8, 80, 80, 18, 18)
        painter.setPen(QColor("#f3f7fb"))
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "APK\n(No icon)")
        painter.end()
        return QIcon(pixmap)

    def _populate_repo_list(self):
        self.repo_list.blockSignals(True)
        self.repo_list.clear()
        for repo in self.repos:
            item = QListWidgetItem(repo.name)
            item.setData(Qt.ItemDataRole.UserRole, repo.name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsSelectable)
            item.setCheckState(Qt.CheckState.Checked if repo.enabled else Qt.CheckState.Unchecked)
            item.setToolTip(repo.index_url)
            self.repo_list.addItem(item)
        self.repo_list.blockSignals(False)

    def _repo_by_name(self, name: str) -> Optional[RepoDefinition]:
        for repo in self.repos:
            if repo.name == name:
                return repo
        return None

    def _on_repo_item_changed(self, item: QListWidgetItem):
        repo = self._repo_by_name(str(item.data(Qt.ItemDataRole.UserRole)))
        if repo is None:
            return
        repo.enabled = item.checkState() == Qt.CheckState.Checked

    def add_custom_repo(self):
        name, accepted = QInputDialog.getText(self, "Add Repository", "Display name:")
        if not accepted or not name.strip():
            return

        url, accepted = QInputDialog.getText(
            self,
            "Add Repository",
            "Repo base URL or index-v1.json URL:",
        )
        if not accepted or not url.strip():
            return

        clean_name = name.strip()
        if any(repo.name.lower() == clean_name.lower() for repo in self.repos):
            QMessageBox.warning(self, "Repository Exists", "A repository with this name already exists.")
            return

        self.repos.append(
            RepoDefinition(
                name=clean_name,
                index_url=url.strip(),
                homepage=url.strip(),
                enabled=True,
                custom=True,
            )
        )
        self._populate_repo_list()
        self.refresh_catalog()

    def _start_initial_load(self):
        self.statusBar().showMessage("Loading cached market data...", 0)
        self._load_cached_catalog()
        if self._auto_refresh_requested:
            QTimer.singleShot(0, self.refresh_catalog)

    def remove_selected_repo(self):
        item = self.repo_list.currentItem()
        if item is None:
            return

        repo = self._repo_by_name(str(item.data(Qt.ItemDataRole.UserRole)))
        if repo is None:
            return
        if not repo.custom:
            QMessageBox.information(self, "Remove Repository", "Default repositories cannot be removed.")
            return

        self.repos = [entry for entry in self.repos if entry.name != repo.name]
        self._populate_repo_list()
        self.refresh_catalog()

    def _load_cached_catalog(self):
        cached_apps: list[MarketApp] = []
        freshest_age: Optional[float] = None

        for repo in self.repos:
            if not repo.enabled:
                continue
            repo_apps, cache_age = _read_repo_cache(repo)
            if not repo_apps:
                continue
            cached_apps.extend(repo_apps)
            if cache_age is not None and (freshest_age is None or cache_age < freshest_age):
                freshest_age = cache_age

        if not cached_apps:
            return

        cached_apps.sort(key=lambda app: ((app.title or app.package_name).lower(), app.package_name.lower()))
        self.all_apps = cached_apps
        self.app_lookup = {app.app_id: app for app in cached_apps}
        self.apply_filters()
        message = f"Loaded {len(cached_apps)} cached app(s) from temp ({_format_age(freshest_age)})."
        self.log(message, "#89CFF0")
        self.statusBar().showMessage(message, 4000)

    def refresh_catalog(self):
        if self.catalog_thread and self.catalog_thread.isRunning():
            return

        self.refresh_button.setEnabled(False)
        self.statusBar().showMessage("Loading repository catalogs...", 0)
        self.log("Refreshing catalog...")

        self.catalog_thread = CatalogWorker(list(self.repos))
        self.catalog_thread.log_message.connect(self.log)
        self.catalog_thread.catalog_ready.connect(self._on_catalog_ready)
        self.catalog_thread.failed.connect(self._on_catalog_failed)
        self.catalog_thread.finished.connect(self._cleanup_catalog_thread)
        self.catalog_thread.start()

    def _cleanup_catalog_thread(self):
        self.refresh_button.setEnabled(True)
        self.catalog_thread = None

    def _on_catalog_failed(self, message: str):
        self.log(message, "#ff6961")
        self.statusBar().showMessage(message, 5000)
        QMessageBox.warning(self, "Catalog Error", message)

    def _on_catalog_ready(self, apps: list[MarketApp]):
        self.all_apps = apps
        self.app_lookup = {app.app_id: app for app in apps}
        self.log(f"Catalog ready. {len(apps)} app(s) available.", "#77DD77")
        self.apply_filters()
        self.statusBar().showMessage(f"Loaded {len(apps)} apps.", 5000)

    def apply_filters(self):
        query = self.search_input.text().strip().lower()
        if not query:
            self.filtered_apps = list(self.all_apps)
        else:
            self.filtered_apps = [
                app for app in self.all_apps
                if query in (app.title or "").lower()
                or query in app.package_name.lower()
                or query in (app.summary or "").lower()
                or any(query in category.lower() for category in app.categories)
            ]

        self.result_label.setText(f"{len(self.filtered_apps)} app(s)")
        self._populate_app_grid()

    def _populate_app_grid(self):
        current_app = self.current_app()
        current_id = current_app.app_id if current_app else None

        self.grid_items.clear()
        self.app_grid.blockSignals(True)
        self.app_grid.clear()

        for index, app_entry in enumerate(self.filtered_apps):
            item = QListWidgetItem(app_entry.title)
            item.setToolTip(app_entry.summary or app_entry.package_name)
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            item.setData(Qt.ItemDataRole.UserRole, app_entry.app_id)
            item.setSizeHint(self.GRID_SIZE)
            if app_entry.icon_url and app_entry.icon_url in self.icon_cache:
                item.setIcon(self.icon_cache[app_entry.icon_url])
            else:
                item.setIcon(self.placeholder_icon)
            self.app_grid.addItem(item)
            self.grid_items[app_entry.app_id] = item

        self.app_grid.blockSignals(False)

        if current_id and current_id in self.grid_items:
            self.app_grid.setCurrentItem(self.grid_items[current_id])
        elif self.app_grid.count() > 0:
            self.app_grid.setCurrentRow(0)
        else:
            self._reset_details()

        self._queue_icon_loading()

    def _schedule_icon_queue(self, *_args):
        self.icon_queue_timer.start()

    def _visible_priority_app_ids(self) -> list[str]:
        if self.app_grid.count() == 0:
            return []

        viewport_rect = self.app_grid.viewport().rect().adjusted(
            -self.GRID_SIZE.width(),
            -(self.GRID_SIZE.height() * 2),
            self.GRID_SIZE.width(),
            self.GRID_SIZE.height() * 2,
        )
        prioritized_ids: list[str] = []
        for index in range(self.app_grid.count()):
            item = self.app_grid.item(index)
            if item is None:
                continue
            item_rect = self.app_grid.visualItemRect(item)
            if item_rect.isValid() and item_rect.intersects(viewport_rect):
                prioritized_ids.append(str(item.data(Qt.ItemDataRole.UserRole)))

        if prioritized_ids:
            return prioritized_ids

        fallback_count = min(self.ICON_PREFETCH_LIMIT, self.app_grid.count())
        fallback_ids: list[str] = []
        for index in range(fallback_count):
            item = self.app_grid.item(index)
            if item is not None:
                fallback_ids.append(str(item.data(Qt.ItemDataRole.UserRole)))
        return fallback_ids

    def _queue_icon_loading(self):
        now = time.time()
        expired_retries = [url for url, retry_after in self.icon_retry_after.items() if retry_after <= now]
        for url in expired_retries:
            self.icon_retry_after.pop(url, None)

        current = self.current_app()
        requests_to_load: list[tuple[str, str]] = []
        seen_urls: set[str] = set()
        if (
            current
            and current.icon_url
            and current.icon_url not in self.icon_cache
            and current.icon_url not in self.icon_retry_after
            and current.icon_url not in self.pending_icon_payloads
        ):
            requests_to_load.append((current.app_id, current.icon_url))
            seen_urls.add(current.icon_url)

        prioritized_ids = self._visible_priority_app_ids()
        ordered_app_ids = dict.fromkeys(prioritized_ids + [app.app_id for app in self.filtered_apps])

        for app_id in ordered_app_ids:
            app_entry = self.app_lookup.get(app_id)
            if app_entry is None:
                continue
            if (
                not app_entry.icon_url
                or app_entry.icon_url in self.icon_cache
                or app_entry.icon_url in self.icon_retry_after
                or app_entry.icon_url in self.pending_icon_payloads
            ):
                continue
            if app_entry.icon_url in seen_urls:
                continue
            requests_to_load.append((app_entry.app_id, app_entry.icon_url))
            seen_urls.add(app_entry.icon_url)

        if not requests_to_load:
            if NETWORK_DEBUG_LOGS and self.icon_retry_after:
                self.log(
                    f"[Icons] No eligible URLs right now. {len(self.icon_retry_after)} URL(s) cooling down.",
                    "#89CFF0",
                )
            return

        if NETWORK_DEBUG_LOGS:
            self.log(f"[Icons] Scheduling {len(requests_to_load)} URL request(s).", "#89CFF0")
        self.icon_fetcher.replace_pending_requests(requests_to_load)

    def _on_icon_ready(self, app_id: str, raw_data: bytes):
        app_entry = self.app_lookup.get(app_id)
        if app_entry is None or not app_entry.icon_url or app_entry.icon_url in self.icon_cache:
            return

        targets = self.pending_icon_targets.setdefault(app_entry.icon_url, set())
        targets.add(app_id)
        if app_entry.icon_url not in self.pending_icon_payloads:
            self.pending_icon_payloads[app_entry.icon_url] = raw_data
            self.pending_icon_decodes.append(app_entry.icon_url)

        if not self.icon_decode_timer.isActive():
            self.icon_decode_timer.start(0)

    def _flush_icon_decode_queue(self):
        processed = 0
        while self.pending_icon_decodes and processed < self.ICON_DECODE_BATCH_SIZE:
            url = self.pending_icon_decodes.popleft()
            raw_data = self.pending_icon_payloads.pop(url, None)
            app_ids = self.pending_icon_targets.pop(url, set())
            if not raw_data:
                continue

            pixmap = QPixmap()
            if not pixmap.loadFromData(raw_data):
                if NETWORK_DEBUG_LOGS:
                    self.log(
                        f"[Icons] Pixmap decode failed for {_short_url(url)}.",
                        "#ffb347",
                    )
                continue

            icon = QIcon(pixmap)
            self.icon_cache[url] = icon
            self.icon_retry_after.pop(url, None)
            self.icon_failure_counts.pop(url, None)

            for app_id in app_ids:
                item = self.grid_items.get(app_id)
                if item is not None:
                    item.setIcon(icon)

            current = self.current_app()
            if current and current.icon_url == url:
                self._update_detail_icon(current)

            if NETWORK_DEBUG_LOGS and app_ids:
                self.log(
                    f"[Icons] Applied icon for {_short_url(url)} to {len(app_ids)} tile(s).",
                    "#89CFF0",
                )
            processed += 1

        if self.pending_icon_decodes and not self.icon_decode_timer.isActive():
            self.icon_decode_timer.start(0)

    def _on_icon_failed(self, url: str, app_ids: list[str], error_text: str):
        failure_count = self.icon_failure_counts.get(url, 0) + 1
        self.icon_failure_counts[url] = failure_count
        cooldown_seconds = min(300, self.ICON_FAILURE_COOLDOWN_SECONDS * failure_count)
        self.icon_retry_after[url] = time.time() + cooldown_seconds
        if NETWORK_DEBUG_LOGS:
            self.log(
                f"[Icons] Cooldown {int(cooldown_seconds)}s for {_short_url(url)} "
                f"after failure affecting {len(app_ids)} tile(s): {error_text}",
                "#ffb347",
            )

    def _on_app_selection_changed(self, current: Optional[QListWidgetItem], previous: Optional[QListWidgetItem]):
        del previous
        if current is None:
            self._reset_details()
            return

        app_id = str(current.data(Qt.ItemDataRole.UserRole))
        app_entry = self.app_lookup.get(app_id)
        if app_entry is None:
            self._reset_details()
            return

        self._populate_details(app_entry)
        if app_entry.app_id in self.clicked_screenshot_app_ids:
            self._request_screenshots(app_entry)
        elif app_entry.screenshot_urls:
            self._set_screenshot_message("Click the app tile to load screenshots.")

    def _on_app_clicked(self, item: QListWidgetItem):
        app_id = str(item.data(Qt.ItemDataRole.UserRole))
        app_entry = self.app_lookup.get(app_id)
        if app_entry is None:
            return
        self.clicked_screenshot_app_ids.add(app_id)
        self._request_screenshots(app_entry)

    def _populate_details(self, app_entry: MarketApp):
        self.title_label.setText(app_entry.title)
        self.summary_label.setText(app_entry.summary or "No summary published for this app.")
        self.package_value.setText(app_entry.package_name)
        self.repo_value.setText(app_entry.repo_name)

        version_text = app_entry.version_name or "Unknown"
        if app_entry.version_code:
            version_text = f"{version_text} (code {app_entry.version_code})"
        self.version_value.setText(version_text)
        self.updated_value.setText(_format_timestamp(app_entry.updated_ms))
        self.license_value.setText(app_entry.license_name or "Unknown")
        self.categories_value.setText(", ".join(app_entry.categories) if app_entry.categories else "None listed")
        self.permissions_value.setText(
            ", ".join(app_entry.permissions) if app_entry.permissions else "Not published in repo metadata"
        )
        self.description_browser.setHtml(_description_html(app_entry))

        self.install_button.setEnabled(bool(app_entry.download_url))
        self.website_button.setEnabled(bool(app_entry.website))
        self.source_button.setEnabled(bool(app_entry.source_code))
        self.issues_button.setEnabled(bool(app_entry.issue_tracker))

        self._update_detail_icon(app_entry)
        if not app_entry.screenshot_urls:
            self._set_screenshot_message("No screenshots published.")
        elif app_entry.app_id in self.clicked_screenshot_app_ids:
            self._set_screenshot_message("Loading screenshots...")
        else:
            self._set_screenshot_message("Click the app tile to load screenshots.")

    def _reset_details(self):
        self.title_label.setText("Select an app")
        self.summary_label.setText("Browse the catalog on the right to see app details here.")
        self.package_value.setText("-")
        self.repo_value.setText("-")
        self.version_value.setText("-")
        self.updated_value.setText("-")
        self.license_value.setText("-")
        self.categories_value.setText("-")
        self.permissions_value.setText("-")
        self.description_browser.setHtml("<p>Select an app to see its description, permissions and screenshots.</p>")
        self.install_button.setEnabled(False)
        self.website_button.setEnabled(False)
        self.source_button.setEnabled(False)
        self.issues_button.setEnabled(False)
        placeholder = self.placeholder_icon.pixmap(self.DETAIL_ICON_SIZE)
        self.detail_icon_label.setPixmap(placeholder)
        self._set_screenshot_message("No screenshots loaded.")

    def _update_detail_icon(self, app_entry: MarketApp):
        if app_entry.icon_url and app_entry.icon_url in self.icon_cache:
            pixmap = self.icon_cache[app_entry.icon_url].pixmap(self.DETAIL_ICON_SIZE)
        else:
            pixmap = self.placeholder_icon.pixmap(self.DETAIL_ICON_SIZE)
            if app_entry.icon_url:
                self._queue_icon_loading()
        self.detail_icon_label.setPixmap(pixmap)

    def _clear_screenshots(self):
        while self.screenshot_layout.count():
            item = self.screenshot_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _set_screenshot_message(self, message: str):
        self._clear_screenshots()
        label = QLabel(message)
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setMinimumHeight(self.SCREENSHOT_HEIGHT - 30)
        self.screenshot_layout.addWidget(label)
        self.screenshot_layout.addStretch()

    def _load_screenshot_payloads_from_disk(self, app_entry: MarketApp) -> Optional[list[bytes]]:
        payloads: list[bytes] = []
        for url in app_entry.screenshot_urls[:self.SCREENSHOT_LIMIT]:
            cached_bytes = None
            for candidate in _screenshot_candidate_urls(url):
                cached_bytes = _read_url_cache_bytes("screenshots", candidate, IMAGE_CACHE_TTL_SECONDS)
                if cached_bytes is not None:
                    break
            if cached_bytes is None:
                return None
            payloads.append(cached_bytes)
        return payloads

    def _decode_screenshot_payloads(self, app_id: str, raw_images: list[bytes]) -> list[QPixmap]:
        pixmaps: list[QPixmap] = []
        for raw_data in raw_images:
            pixmap = QPixmap()
            if pixmap.loadFromData(raw_data):
                pixmaps.append(pixmap)

        self.screenshot_cache[app_id] = pixmaps
        if NETWORK_DEBUG_LOGS:
            self.log(f"[Screens] Decoded {len(pixmaps)} screenshot(s) for {app_id}.", "#89CFF0")
        return pixmaps

    def _request_screenshots(self, app_entry: MarketApp):
        if not app_entry.screenshot_urls:
            self._set_screenshot_message("No screenshots published.")
            return

        cached = self.screenshot_cache.get(app_entry.app_id)
        if cached is not None:
            if NETWORK_DEBUG_LOGS:
                self.log(
                    f"[Screens] Using in-memory cache for {app_entry.package_name}: {len(cached)} image(s)."
                    , "#89CFF0"
                )
            self._render_screenshots(cached)
            return

        disk_cached_payloads = self._load_screenshot_payloads_from_disk(app_entry)
        if disk_cached_payloads is not None:
            if NETWORK_DEBUG_LOGS:
                self.log(
                    f"[Screens] Using disk cache for {app_entry.package_name}: {len(disk_cached_payloads)} image(s).",
                    "#89CFF0",
                )
            self._render_screenshots(self._decode_screenshot_payloads(app_entry.app_id, disk_cached_payloads))
            return

        if self.screenshot_thread and self.screenshot_thread.isRunning():
            self.pending_screenshot_app_id = app_entry.app_id
            self._set_screenshot_message("Loading screenshots...")
            if NETWORK_DEBUG_LOGS:
                self.log(
                    f"[Screens] Screenshot worker busy, queued {app_entry.package_name}.",
                    "#89CFF0",
                )
            return

        self._set_screenshot_message("Loading screenshots...")
        if NETWORK_DEBUG_LOGS:
            self.log(
                f"[Screens] Launching screenshot worker for {app_entry.package_name} "
                f"with {min(len(app_entry.screenshot_urls), self.SCREENSHOT_LIMIT)} URL(s).",
                "#89CFF0",
            )
        self.screenshot_thread = ScreenshotLoaderWorker(
            app_entry.app_id,
            app_entry.screenshot_urls[:self.SCREENSHOT_LIMIT],
        )
        self.screenshot_thread.log_message.connect(partial(self.log, color="#89CFF0"))
        self.screenshot_thread.screenshots_ready.connect(self._on_screenshots_ready)
        self.screenshot_thread.finished.connect(self._cleanup_screenshot_thread)
        self.screenshot_thread.start()

    def _cleanup_screenshot_thread(self):
        self.screenshot_thread = None
        if self.pending_screenshot_app_id:
            app_id = self.pending_screenshot_app_id
            self.pending_screenshot_app_id = None
            app_entry = self.app_lookup.get(app_id)
            current = self.current_app()
            if app_entry is not None and current and current.app_id == app_id:
                self._request_screenshots(app_entry)

    def _on_screenshots_ready(self, app_id: str, raw_images: list[bytes]):
        pixmaps = self._decode_screenshot_payloads(app_id, raw_images)
        current = self.current_app()
        if current and current.app_id == app_id:
            self._render_screenshots(pixmaps)

    def _render_screenshots(self, pixmaps: list[QPixmap]):
        if not pixmaps:
            self._set_screenshot_message("No screenshots could be loaded.")
            return

        self._clear_screenshots()
        for pixmap in pixmaps:
            label = QLabel()
            scaled = pixmap.scaledToHeight(
                self.SCREENSHOT_HEIGHT,
                Qt.TransformationMode.SmoothTransformation,
            )
            label.setPixmap(scaled)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.screenshot_layout.addWidget(label)
        self.screenshot_layout.addStretch()

    def _apply_download_mode_ui(self):
        keep_downloads = self.keep_apk_checkbox.isChecked()
        self.choose_download_button.setEnabled(keep_downloads)

        if keep_downloads:
            if self.download_dir:
                self.download_path_label.setText(f"Session download folder: {self.download_dir}")
            else:
                self.download_path_label.setText("Session download folder: not selected yet.")
        else:
            self.download_path_label.setText(
                "Downloads are stored in the system temp folder and removed after install."
            )

    def select_download_dir(self):
        directory = QFileDialog.getExistingDirectory(self, "Select APK Download Folder")
        if not directory:
            return
        self.download_dir = directory
        self._apply_download_mode_ui()

    def clear_download_dir(self):
        self.download_dir = ""
        self._apply_download_mode_ui()

    def current_app(self) -> Optional[MarketApp]:
        item = self.app_grid.currentItem()
        if item is None:
            return None
        return self.app_lookup.get(str(item.data(Qt.ItemDataRole.UserRole)))

    def install_selected_app(self):
        app_entry = self.current_app()
        if app_entry is None:
            QMessageBox.information(self, "Install App", "Select an app to install first.")
            return

        if self.install_thread and self.install_thread.isRunning():
            return

        if self.keep_apk_checkbox.isChecked() and not self.download_dir:
            self.select_download_dir()
            if not self.download_dir:
                QMessageBox.information(
                    self,
                    "Download Folder",
                    "Choose a download folder if you want to keep APKs after installation.",
                )
                return

        self.install_button.setEnabled(False)
        self.install_progress.setVisible(True)
        self.install_progress.setRange(0, 100)
        self.install_progress.setValue(0)
        self.statusBar().showMessage(f"Installing {app_entry.title}...", 0)

        self.install_thread = InstallWorker(
            app_entry,
            self.download_dir,
            self.keep_apk_checkbox.isChecked(),
        )
        self.install_thread.log_message.connect(self.log)
        self.install_thread.progress_changed.connect(self._on_install_progress)
        self.install_thread.status_changed.connect(self.statusBar().showMessage)
        self.install_thread.finished_action.connect(self._on_install_finished)
        self.install_thread.finished.connect(self._cleanup_install_thread)
        self.install_thread.start()

    def _on_install_progress(self, value: int):
        self.install_progress.setVisible(True)
        if value < 0:
            self.install_progress.setRange(0, 0)
            return
        if self.install_progress.maximum() == 0:
            self.install_progress.setRange(0, 100)
        self.install_progress.setValue(value)

    def _cleanup_install_thread(self):
        self.install_thread = None
        self.install_progress.setVisible(False)
        self.install_progress.setRange(0, 100)
        self.install_progress.setValue(0)
        self.install_button.setEnabled(self.current_app() is not None)

    def _on_install_finished(self, success: bool, message: str):
        color = "#77DD77" if success else "#ff6961"
        self.log(message, color)
        self.statusBar().showMessage(message, 6000)
        if not success:
            QMessageBox.warning(self, "Install Result", message)

    def _open_app_url(self, field_name: str, _checked: bool = False):
        app_entry = self.current_app()
        if not app_entry:
            return
        url = str(getattr(app_entry, field_name, "") or "")
        if url:
            open_url_safe(url)

    def log(self, message: str, color: Optional[str] = None):
        self.log_output.moveCursor(QTextCursor.MoveOperation.End)
        self.log_output.setTextColor(QColor(color or ThemeManager.TEXT_COLOR_PRIMARY))
        self.log_output.insertPlainText(message + "\n")
        self.log_output.setTextColor(QColor(ThemeManager.TEXT_COLOR_PRIMARY))
        self.log_output.ensureCursorVisible()


def run_foss_market():
    existing = QApplication.instance()
    app = existing or QApplication(sys.argv)
    window = FossMarketWindow()
    window.show()
    if not existing:
        return app.exec()
    return window


if __name__ == "__main__":
    run_foss_market()
