#!/usr/bin/env python3
"""
bootanimcreator.py

QuickADB Boot Animation Creator — updated:
- Uses tempfile for work/extract dirs (created at startup, cleaned on exit)
- Extracted frames are loaded into RAM (QPixmap) and temporary frame folders are removed
- Extraction (ffmpeg / GIF) runs in background threads
- Create bootanimation.zip can build from the in-memory frame sequence (no reliance on extracted folder)
- Push flow shows dialog: push created zip OR select & push arbitrary zip
- Detailed logging for push steps returned and shown to user
"""
import os
import sys
import re
import shutil
import tempfile
import zipfile
import subprocess
import time
from datetime import datetime
from typing import Optional, Dict, List, Tuple

script_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(script_dir)
sys.path.insert(0, root_dir)

from util.thememanager import ThemeManager


from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFileDialog, QMessageBox, QSpinBox, QGroupBox, QLineEdit, QTextEdit, QGridLayout,
    QDialog, QFormLayout, QDialogButtonBox, QPlainTextEdit
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QPixmap

from PIL import Image, ImageSequence

ADB_BIN = "adb.exe" if os.name == "nt" else "adb"
PLATFORM_ADB = os.path.join(root_dir, "platform-tools", ADB_BIN)
ADB_CMD = PLATFORM_ADB if os.path.exists(PLATFORM_ADB) else ADB_BIN

BOOT_PATHS = [
    "/system/media/bootanimation.zip",
    "/system/product/media/bootanimation.zip",
    "/product/media/bootanimation.zip",
    "/vendor/media/bootanimation.zip",
]

# placeholders for globals that will be set by ensure_work_dirs()
WORK_DIR = None
EXTRACT_DIR = None
PULLED_ZIP = None
CREATED_ZIP_DEFAULT = None


class FuncThread(QThread):
    done = pyqtSignal(object, object)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs

    def run(self):
        try:
            res = self.fn(*self.args, **self.kwargs)
            self.done.emit(res, None)
        except Exception as e:
            self.done.emit(None, e)


def ensure_work_dirs(create_temp=True):
    """
    Initialize module-level WORK_DIR, EXTRACT_DIR, PULLED_ZIP, CREATED_ZIP_DEFAULT.
    If create_temp is True, make a tempdir; otherwise use a persistent folder under project root.
    """
    global WORK_DIR, EXTRACT_DIR, PULLED_ZIP, CREATED_ZIP_DEFAULT
    if create_temp:
        WORK_DIR = tempfile.mkdtemp(prefix="qadb_work_")
    else:
        WORK_DIR = os.path.join(root_dir, "bootanim_work")
        os.makedirs(WORK_DIR, exist_ok=True)
    EXTRACT_DIR = os.path.join(WORK_DIR, "extracted")
    PULLED_ZIP = os.path.join(WORK_DIR, "bootanimation.zip")
    CREATED_ZIP_DEFAULT = os.path.join(WORK_DIR, "new_bootanimation.zip")
    # ensure extract dir is clean
    shutil.rmtree(EXTRACT_DIR, ignore_errors=True)
    os.makedirs(EXTRACT_DIR, exist_ok=True)


def cleanup_work_dirs():
    global WORK_DIR
    try:
        if WORK_DIR and os.path.isdir(WORK_DIR):
            shutil.rmtree(WORK_DIR)
    except Exception:
        pass


def find_remote_path_sync(adb_cmd: str = ADB_CMD) -> Optional[str]:
    for p in BOOT_PATHS:
        try:
            proc = subprocess.run([adb_cmd, "shell", "su", "-c", f'ls "{p}"'],
                                  capture_output=True, text=True, timeout=6)
        except Exception:
            continue
        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode == 0 and "No such file or directory" not in out:
            return p
    return None


def pull_root_zip_sync(remote_path: str, adb_cmd: str = ADB_CMD, local_dest: str = None) -> bool:
    """
    Pull bootanimation.zip using root-copy to /data/local/tmp then adb pull.
    local_dest provided (module-level PULLED_ZIP expected to be set).
    """
    tmp_remote = "/data/local/tmp/bootanimation_quickadb.zip"
    if local_dest is None:
        raise RuntimeError("local_dest must be specified")
    try:
        cp_cmd = [adb_cmd, "shell", "su", "-c", f'cp "{remote_path}" "{tmp_remote}" && chmod 0644 "{tmp_remote}"']
        cp = subprocess.run(cp_cmd, capture_output=True, text=True, timeout=25)
        if cp.returncode == 0:
            pull = subprocess.run([adb_cmd, "pull", tmp_remote, local_dest], capture_output=True, text=True, timeout=90)
            if pull.returncode == 0 and os.path.exists(local_dest):
                subprocess.run([adb_cmd, "shell", "su", "-c", f'rm "{tmp_remote}"'], capture_output=True)
                return True
    except Exception:
        pass
    # fallback shell style
    try:
        proc = subprocess.run(f'{adb_cmd} shell su -c \'cp "{remote_path}" "{tmp_remote}" && chmod 0644 "{tmp_remote}"\'',
                              shell=True, capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            pull = subprocess.run([adb_cmd, "pull", tmp_remote, local_dest], capture_output=True, text=True, timeout=90)
            if pull.returncode == 0 and os.path.exists(local_dest):
                subprocess.run(f'{adb_cmd} shell "su -c rm \\"{tmp_remote}\\""', shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
    except Exception:
        pass
    try:
        proc1 = subprocess.run([adb_cmd, "shell", f'su -c cp "{remote_path}" "{tmp_remote}"'], capture_output=True, text=True, timeout=20)
        if proc1.returncode == 0:
            proc2 = subprocess.run([adb_cmd, "shell", f'su -c chmod 0644 "{tmp_remote}"'], capture_output=True, text=True, timeout=10)
            if proc2.returncode == 0:
                pull = subprocess.run([adb_cmd, "pull", tmp_remote, local_dest], capture_output=True, text=True, timeout=90)
                if pull.returncode == 0 and os.path.exists(local_dest):
                    subprocess.run([adb_cmd, "shell", "su", "-c", f'rm "{tmp_remote}"'], capture_output=True)
                    return True
    except Exception:
        pass
    return False


def parse_pulled_zip_sync(zip_path: str) -> Optional[Dict]:
    """
    Extract zip into EXTRACT_DIR and parse desc.txt. Returns info dict or None.
    """
    if not zip_path or not os.path.exists(zip_path):
        return None
    # ensure extract dir empty
    shutil.rmtree(EXTRACT_DIR, ignore_errors=True)
    os.makedirs(EXTRACT_DIR, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(EXTRACT_DIR)
    except Exception:
        return None
    desc = os.path.join(EXTRACT_DIR, "desc.txt")
    if not os.path.exists(desc):
        return None
    try:
        with open(desc, "r", encoding="utf-8", errors="ignore") as f:
            lines = [ln.strip() for ln in f.read().splitlines() if ln.strip()]
        w, h, fps = map(int, lines[0].split()[:3])
    except Exception:
        return None
    parts = []
    for ln in lines[1:]:
        tok = ln.split()
        if len(tok) < 3:
            continue
        mode = tok[0]
        loops = int(tok[1]) if tok[1].isdigit() else 1
        folder = tok[2]
        pause = int(tok[3]) if len(tok) > 3 and tok[3].isdigit() else 0
        candidate = os.path.join(EXTRACT_DIR, folder)
        folder_path = candidate if os.path.isdir(candidate) else None
        frame_count = 0
        if folder_path:
            frame_count = len([f for f in os.listdir(folder_path) if os.path.isfile(os.path.join(folder_path, f))])
        parts.append({
            "mode": mode, "loops": loops, "folder": folder, "pause": pause,
            "frame_count": frame_count, "folder_path": folder_path
        })
    return {"width": w, "height": h, "fps": fps, "parts": parts}


def extract_video_frames_sync(video_path: str, width: int, height: int, ffmpeg_path: str, fps: Optional[int] = None) -> str:
    """
    Extract frames from video using ffmpeg into a temporary directory and return that dir path.
    Raises on failure.
    """
    if not os.path.isfile(video_path):
        raise FileNotFoundError("Video file missing")
    # resolve ffmpeg path: prefer provided exact path, otherwise search PATH
    ff = ffmpeg_path.strip() if ffmpeg_path else ""
    if ff and os.path.isfile(ff):
        ffmpeg_bin = ff
    else:
        ffmpeg_bin = shutil.which(ff) or shutil.which("ffmpeg")
    if not ffmpeg_bin:
        raise FileNotFoundError("ffmpeg not found")
    out_dir = tempfile.mkdtemp(prefix="bootanim_vid_")
    out_pattern = os.path.join(out_dir, "%09d.png")
    args = [ffmpeg_bin, "-y", "-i", video_path]
    # force FPS if provided
    if fps and isinstance(fps, int) and fps > 0:
        args += ["-r", str(fps)]
    if width > 0 and height > 0:
        args += ["-vf", f"scale={width}:{height}:flags=lanczos"]
    args += [out_pattern]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        try:
            shutil.rmtree(out_dir)
        except Exception:
            pass
        raise RuntimeError(f"ffmpeg failed: {proc.stderr[:1000] or proc.stdout[:1000]}")
    return out_dir


def extract_gif_frames_sync(gif_path: str, width: int, height: int, fps: Optional[int] = None) -> str:
    """
    Extract frames from GIF into a temporary folder using Pillow and return that dir.
    """
    if not os.path.isfile(gif_path):
        raise FileNotFoundError("GIF file missing")
    out_dir = tempfile.mkdtemp(prefix="bootanim_gif_")
    im = Image.open(gif_path)
    i = 0
    for frame in ImageSequence.Iterator(im):
        frame = frame.convert("RGBA")
        if width > 0 and height > 0:
            frame = frame.resize((width, height), Image.Resampling.LANCZOS)
        frame.save(os.path.join(out_dir, f"{i:09d}.png"))
        i += 1
    if i == 0:
        shutil.rmtree(out_dir)
        raise RuntimeError("No frames extracted from GIF")
    return out_dir


# ---------- Module creation helpers ----------
def _make_zip_with_permissions(root_dir: str, out_zip: str, perms: Dict[str, int] = None):
    """
    Create zip file from root_dir and set unix permissions for files specified in perms (relpath->mode).
    perms is optional dict mapping relative path inside zip to unix mode (e.g. 0o755).
    """
    if perms is None:
        perms = {}
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rootp, dirs, files in os.walk(root_dir):
            for fn in files:
                full = os.path.join(rootp, fn)
                rel = os.path.relpath(full, root_dir)
                zf.write(full, rel)
                # set external attr for permissions if provided
                if rel in perms:
                    info = zf.getinfo(rel)
                    # mode in upper 16 bits
                    info.external_attr = (perms[rel] & 0xFFFF) << 16


def create_module_sync(module_meta: Dict[str, str], save_zip: str, created_boot_zip: str, remote_path: str, alias: str):
    """
    Build a flashable module zip that places created_boot_zip into the device remote_path.
    Returns success message or raises.
    """
    if not remote_path:
        raise RuntimeError("Remote path unknown. Find/pull the device bootanimation first.")
    if not created_boot_zip or not os.path.exists(created_boot_zip):
        raise RuntimeError("Created bootanimation.zip missing. Create it first.")

    tmp_mod = tempfile.mkdtemp(prefix="qadb_module_")
    try:
        # Determine internal module path components (drop leading slash)
        rp = remote_path.lstrip("/")
        parts = rp.split("/")[:-1]  # directory components (exclude filename)
        target_rel_dir = os.path.join(*parts) if parts else ""
        mod_target_dir = os.path.join(tmp_mod, target_rel_dir)
        os.makedirs(mod_target_dir, exist_ok=True)

        # Copy created bootanimation.zip into the module
        target_filename = os.path.basename(remote_path)
        shutil.copy2(created_boot_zip, os.path.join(mod_target_dir, target_filename))

        # module.prop generation
        mod_id = module_meta.get("id") or f"quickadb.bootanim.{int(time.time())}"
        mod_name = module_meta.get("name") or "QuickADB Boot Animation"
        mod_version = module_meta.get("version") or "1.0"
        mod_author = module_meta.get("author") or "QuickADB"
        mod_desc = module_meta.get("description", "") + f"\nMade with {alias}"

        mod_prop = [
            f"id={mod_id}",
            f"name={mod_name}",
            f"version={mod_version}",
            f"author={mod_author}",
            f"description={mod_desc}"
        ]
        with open(os.path.join(tmp_mod, "module.prop"), "w", encoding="utf-8") as f:
            f.write("\n".join(mod_prop))

        # post-fs-data.sh script to set permission for the installed file (executable)
        target_full = remote_path  # exact path to chmod on device
        post_sh = f"#!/sbin/sh\n# Ensure permission for bootanimation\nchmod 0644 \"{target_full}\" || true\nexit 0\n"
        sh_path = os.path.join(tmp_mod, "post-fs-data.sh")
        with open(sh_path, "w", encoding="utf-8") as f:
            f.write(post_sh)
        # create a short README
        readme = f"{mod_name}\n\nThis module installs a custom bootanimation at: {remote_path}\n{mod_desc}\n"
        with open(os.path.join(tmp_mod, "README.txt"), "w", encoding="utf-8") as f:
            f.write(readme)

        # create zip and set perms: post-fs-data.sh -> 0o755, bootanimation.zip -> 0o644
        perms = {}
        rel_post = os.path.relpath(sh_path, tmp_mod)
        rel_boot = os.path.join(target_rel_dir, target_filename) if target_rel_dir else target_filename
        perms[rel_post] = 0o755
        perms[rel_boot] = 0o644
        _make_zip_with_permissions(tmp_mod, save_zip, perms=perms)
        return f"Module created: {save_zip}"
    finally:
        try:
            shutil.rmtree(tmp_mod)
        except Exception:
            pass


# ---------- GUI and main widget ----------
class ModuleDialog(QDialog):
    """
    Dialog for module metadata; 'Create module' asks for save location and accepts.
    After exec(), dialog.save_path (str) will be set or None.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create Flashable Module - Module Properties")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.id_edit = QLineEdit()
        self.name_edit = QLineEdit("QuickADB Boot Animation Module")
        self.version_edit = QLineEdit("1.0")
        self.author_edit = QLineEdit("QuickADB")
        self.desc_edit = QPlainTextEdit("Install custom bootanimation created with QuickADB.")
        form.addRow("Module ID:", self.id_edit)
        form.addRow("Name:", self.name_edit)
        form.addRow("Version:", self.version_edit)
        form.addRow("Author:", self.author_edit)
        form.addRow("Description:", self.desc_edit)
        layout.addLayout(form)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)
        self.save_path: Optional[str] = None

    def _on_ok(self):
        # ask for save path before accepting
        fn, _ = QFileDialog.getSaveFileName(self, "Save module zip", "", "ZIP Files (*.zip)")
        if not fn:
            return  # don't accept, let user pick
        if not fn.lower().endswith(".zip"):
            fn += ".zip"
        self.save_path = fn
        self.accept()

    def get_meta(self):
        return {
            "id": self.id_edit.text().strip(),
            "name": self.name_edit.text().strip(),
            "version": self.version_edit.text().strip(),
            "author": self.author_edit.text().strip(),
            "description": self.desc_edit.toPlainText().strip(),
        }


class PushDialog(QDialog):
    """
    Minimal dialog presenting push options:
      - Push created ZIP (if available)
      - Select & push specific ZIP
    Returns tuple (choice, path) where choice is 'created' or 'select' or None.
    """
    def __init__(self, parent=None, created_path: Optional[str] = None):
        super().__init__(parent)
        self.setWindowTitle("Push bootanimation.zip")
        self.created_path = created_path
        self.choice = None
        self.path = None
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        info = QLabel("Choose which ZIP to push to device:")
        layout.addWidget(info)
        btn_row = QHBoxLayout()
        self.btn_created = QPushButton("Push created ZIP")
        self.btn_created.clicked.connect(self._on_created)
        self.btn_select = QPushButton("Select & Push ZIP")
        self.btn_select.clicked.connect(self._on_select)
        btn_row.addWidget(self.btn_created)
        btn_row.addWidget(self.btn_select)
        layout.addLayout(btn_row)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        layout.addWidget(self.cancel_btn)
        if not self.created_path or not os.path.exists(self.created_path):
            self.btn_created.setEnabled(False)

    def _on_created(self):
        self.choice = "created"
        self.path = self.created_path
        self.accept()

    def _on_select(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Select bootanimation.zip to push", "", "ZIP files (*.zip);;All Files (*)")
        if not fn:
            return
        self.choice = "select"
        self.path = fn
        self.accept()


class BootAnimWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Boot Animation Creator — QuickADB")
        self.setMinimumSize(980, 720)
        ThemeManager.apply_theme(self)

        # create temp work dirs
        ensure_work_dirs(create_temp=True)
        self.adb_cmd = ADB_CMD

        self.remote_path: Optional[str] = None
        self.current_info: Optional[Dict] = None
        self.created_zip_path = CREATED_ZIP_DEFAULT
        self.backup_dir = os.path.join(root_dir, "bootanim_backups")
        os.makedirs(self.backup_dir, exist_ok=True)

        self.seq_current: List[Tuple[QPixmap, int]] = []
        self.seq_created: List[Tuple[QPixmap, int]] = []

        self.timer_current = QTimer(self)
        self.timer_created = QTimer(self)
        self.timer_current.timeout.connect(self._tick_current)
        self.timer_created.timeout.connect(self._tick_created)
        self.play_index_current = 0
        self.play_index_created = 0
        self.playing_current = False
        self.playing_created = False

        self._threads: List[QThread] = []

        self.ffmpeg_resolved_path = shutil.which("ffmpeg") or ""
        self.ffmpeg_path = self.ffmpeg_resolved_path

        # selected animation file (video or gif) to be previewed / decoded
        self.selected_anim_file: Optional[str] = None

        # remember last frames dir briefly before cleanup (we will delete extracted frames after loading into RAM)
        self._last_created_frames_dir: Optional[str] = None

        self._build_ui()
        QTimer.singleShot(150, self._startup_ffmpeg_check)

    def _build_ui(self):
        root = QVBoxLayout(self)

        # --- Top row buttons ---
        top = QHBoxLayout()
        btn_size = (205, 30)  # bigger buttons

        self.find_btn = QPushButton("Find Device Bootanim (Root)")
        self.find_btn.setFixedSize(*btn_size)
        self.find_btn.clicked.connect(self.find_and_load)

        self.select_anim_btn = QPushButton("Select MP4/GIF for Create/Preview")
        self.select_anim_btn.setFixedSize(*btn_size)
        self.select_anim_btn.clicked.connect(self.select_animation_file)

        top.addWidget(self.find_btn)
        top.addWidget(self.select_anim_btn)
        top.addStretch()
        root.addLayout(top)

        # --- Mid section ---
        mid = QHBoxLayout()
        left_col = QVBoxLayout()

        # Animation Properties
        props_grp = QGroupBox("Animation Properties")
        props_layout = QHBoxLayout()
        self.width_spin = QSpinBox(); self.width_spin.setRange(16, 4096); self.width_spin.setValue(720)
        self.height_spin = QSpinBox(); self.height_spin.setRange(16, 4096); self.height_spin.setValue(240)
        self.fps_spin = QSpinBox(); self.fps_spin.setRange(1, 60); self.fps_spin.setValue(30)
        self.preview_btn = QPushButton("Preview Selected File"); self.preview_btn.setFixedSize(180, 30); self.preview_btn.clicked.connect(self.preview_selected_animation)
        props_layout.addWidget(QLabel("Width")); props_layout.addWidget(self.width_spin)
        props_layout.addWidget(QLabel("Height")); props_layout.addWidget(self.height_spin)
        props_layout.addWidget(QLabel("FPS")); props_layout.addWidget(self.fps_spin)
        props_layout.addWidget(self.preview_btn)
        props_grp.setLayout(props_layout)
        left_col.addWidget(props_grp)

        # FFmpeg section
        ff_grp = QGroupBox("FFmpeg")
        ff_layout = QHBoxLayout()
        self.ffmpeg_edit = QLineEdit(self.ffmpeg_resolved_path)
        self.ffmpeg_edit.setPlaceholderText("ffmpeg path (or leave blank to use PATH)")
        self.ff_browse = QPushButton("Browse"); self.ff_browse.setFixedSize(80, 28); self.ff_browse.clicked.connect(self.browse_ffmpeg)
        self.ff_check = QPushButton("Check"); self.ff_check.setFixedSize(80, 28); self.ff_check.clicked.connect(self.check_ffmpeg)
        ff_layout.addWidget(self.ffmpeg_edit); ff_layout.addWidget(self.ff_browse); ff_layout.addWidget(self.ff_check)
        ff_grp.setLayout(ff_layout)
        left_col.addWidget(ff_grp)

        # Created/Backup Paths section (3x2 grid: Create, Backup, 4 reserved)
        created_grp = QGroupBox("Created / Backup Actions (placeholders)")
        created_layout = QGridLayout()

        # Create button
        self.create_zip_btn = QPushButton("Create bootanimation.zip from frames")
        self.create_zip_btn.clicked.connect(self.create_bootanimation_zip)

        # Backup button (single backup flow)
        self.backup_btn = QPushButton("Backup stock boot animation")
        self.backup_btn.clicked.connect(self.backup_stock_bootanimation)

        # Push button (opens dialog)
        self.push_zip_btn = QPushButton("Push bootanimation.zip to device")
        self.push_zip_btn.clicked.connect(self.push_zip_flow)

        # reserved placeholder buttons (future features)
        self.reserved_btn1 = QPushButton("Create Flashable Module")
        self.reserved_btn1.clicked.connect(self.create_flashable_module_flow)
        self.reserved_btn2 = QPushButton("Reserved 2")
        self.reserved_btn3 = QPushButton("Reserved 3")

        # Layout the 3x2 grid
        created_layout.addWidget(self.create_zip_btn, 0, 0)
        created_layout.addWidget(self.backup_btn, 0, 1)
        created_layout.addWidget(self.push_zip_btn, 1, 0)
        created_layout.addWidget(self.reserved_btn1, 1, 1)
        created_layout.addWidget(self.reserved_btn2, 2, 0)
        created_layout.addWidget(self.reserved_btn3, 2, 1)

        created_grp.setLayout(created_layout)
        left_col.addWidget(created_grp)

        # Logging area
        self.log_text = QTextEdit(); self.log_text.setReadOnly(True); self.log_text.setFixedHeight(220)
        left_col.addWidget(self.log_text)

        mid.addLayout(left_col, stretch=0)

        # Right preview columns
        right_col = QHBoxLayout()
        preview_w = 220
        preview_h = int(preview_w * 20 / 9)

        # Current device preview
        cur_group = QVBoxLayout()
        cur_group.addWidget(QLabel("Current (device)", alignment=Qt.AlignmentFlag.AlignCenter))
        self.cur_label = QLabel(); self.cur_label.setFixedSize(preview_w, preview_h); self.cur_label.setStyleSheet("background:#0d0d0d;"); self.cur_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cur_group.addWidget(self.cur_label, alignment=Qt.AlignmentFlag.AlignCenter)
        cur_btn_row = QHBoxLayout()
        self.cur_play_btn = QPushButton("▶"); self.cur_play_btn.setFixedSize(40, 28); self.cur_play_btn.clicked.connect(self.toggle_current)
        cur_btn_row.addWidget(self.cur_play_btn); cur_btn_row.addStretch()
        cur_group.addLayout(cur_btn_row)
        right_col.addLayout(cur_group)

        # Created local preview
        new_group = QVBoxLayout()
        new_group.addWidget(QLabel("Created (local preview)", alignment=Qt.AlignmentFlag.AlignCenter))
        self.new_label = QLabel(); self.new_label.setFixedSize(preview_w, preview_h); self.new_label.setStyleSheet("background:#0d0d0d;"); self.new_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        new_group.addWidget(self.new_label, alignment=Qt.AlignmentFlag.AlignCenter)
        new_btn_row = QHBoxLayout()
        self.new_play_btn = QPushButton("▶"); self.new_play_btn.setFixedSize(40, 28); self.new_play_btn.clicked.connect(self.toggle_created)
        new_btn_row.addWidget(self.new_play_btn); new_btn_row.addStretch()
        new_group.addLayout(new_btn_row)
        right_col.addLayout(new_group)

        mid.addLayout(right_col, stretch=0)
        root.addLayout(mid)


    def log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.log_text.append(f"[{ts}] {msg}")
        self.log_text.ensureCursorVisible()

    def _startup_ffmpeg_check(self):
        path = shutil.which("ffmpeg")
        if path:
            self.log(f"ffmpeg found in PATH: {path}")
            self._check_ffmpeg_version(path)
            self.ffmpeg_resolved_path = path
            self.ffmpeg_path = path
            self.ffmpeg_edit.setText(path)
        else:
            self.log("ffmpeg not found in PATH")

    def browse_ffmpeg(self):
        p, _ = QFileDialog.getOpenFileName(self, "Locate ffmpeg binary")
        if not p:
            return
        self.ffmpeg_edit.setText(p)
        self.check_ffmpeg()

    def check_ffmpeg(self):
        p = self.ffmpeg_edit.text().strip() or shutil.which("ffmpeg") or ""
        if not p:
            QMessageBox.warning(self, "FFmpeg", "FFmpeg not set and not found in PATH.")
            return
        self._check_ffmpeg_version(p)

    def _check_ffmpeg_version(self, path: str):
        try:
            proc = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=6)
            out = proc.stdout or proc.stderr or ""
            first = out.splitlines()[0] if out.splitlines() else out.strip()
            self.log(f"ffmpeg: {first}")
            self.ffmpeg_resolved_path = path
            self.ffmpeg_path = path
            self.ffmpeg_edit.setText(path)
        except Exception as e:
            self.log(f"ffmpeg check failed: {e}")
            QMessageBox.critical(self, "FFmpeg", f"ffmpeg check failed: {e}")

    def _start_thread(self, t: QThread):
        self._threads.append(t)
        def cleanup():
            try:
                self._threads.remove(t)
            except Exception:
                pass
        try:
            t.finished.connect(cleanup)
        except Exception:
            pass
        try:
            t.done.connect(lambda *_: cleanup())
        except Exception:
            pass
        t.start()
        return t

    # ---------- find/pull/parse flow ----------
    def find_and_load(self):
        self.log("Detecting bootanimation on device (root-only)...")
        t = FuncThread(find_remote_path_sync, self.adb_cmd)
        t.done.connect(self._on_find_done)
        self._start_thread(t)

    def _on_find_done(self, result, error):
        if error:
            self.log(f"Find failed: {error}")
            QMessageBox.critical(self, "Find failed", str(error))
            return
        found = result
        if not found:
            self.log("No bootanimation found.")
            QMessageBox.information(self, "Not found", "No bootanimation found in common locations.")
            return
        self.remote_path = found
        self.log(f"Found: {found}")
        self.pull_remote()

    def pull_remote(self):
        if not getattr(self, "remote_path", None):
            QMessageBox.warning(self, "No target", "Run Find first.")
            return
        self.log(f"Pulling {self.remote_path} ...")
        t = FuncThread(pull_root_zip_sync, self.remote_path, self.adb_cmd, PULLED_ZIP)
        t.done.connect(self._on_pull_done)
        self._start_thread(t)

    def _on_pull_done(self, result, error):
        if error:
            self.log(f"Pull failed: {error}")
            QMessageBox.critical(self, "Pull failed", str(error))
            return
        ok = result
        if not ok:
            self.log("Pull reported failure.")
            QMessageBox.critical(self, "Pull failed", "Could not pull bootanimation.zip (root flow).")
            return
        self.log(f"Pulled: {PULLED_ZIP}")
        t = FuncThread(parse_pulled_zip_sync, PULLED_ZIP)
        t.done.connect(self._on_parse_done)
        self._start_thread(t)

    def _resolve_folder_path(self, folder_name: str) -> Optional[str]:
        if not folder_name:
            return None
        token = folder_name.strip().lstrip("./").rstrip("/")
        candidate = os.path.join(EXTRACT_DIR, token)
        if os.path.isdir(candidate):
            return candidate
        cand2 = os.path.normpath(os.path.join(EXTRACT_DIR, token))
        if os.path.isdir(cand2):
            return cand2
        if token.isdigit():
            for t in (f"part{token}", f"p{token}", token):
                c = os.path.join(EXTRACT_DIR, t)
                if os.path.isdir(c):
                    return c
        for rootp, dirs, _ in os.walk(EXTRACT_DIR):
            for d in dirs:
                if d == token:
                    return os.path.join(rootp, d)
        if token.isdigit():
            for rootp, dirs, _ in os.walk(EXTRACT_DIR):
                for d in dirs:
                    if d.endswith(token):
                        return os.path.join(rootp, d)
        for rootp, dirs, _ in os.walk(EXTRACT_DIR):
            for d in dirs:
                if token.lower() in d.lower():
                    return os.path.join(rootp, d)
        base = os.path.basename(token)
        if base:
            for rootp, dirs, _ in os.walk(EXTRACT_DIR):
                for d in dirs:
                    if d == base:
                        return os.path.join(rootp, d)
        return None

    def _on_parse_done(self, result, error):
        if error:
            self.log(f"Parse failed: {error}")
            QMessageBox.critical(self, "Parse failed", str(error))
            return
        info = result
        if not info:
            self.log("Parse returned no info (desc.txt missing?).")
            QMessageBox.critical(self, "Parse failed", "desc.txt missing or invalid in pulled zip.")
            return
        self.current_info = info
        self.log(f"Parsed boot animation: {info['width']}x{info['height']}@{info['fps']}, parts: {len(info['parts'])}")
        self.build_sequence_from_info(info, which="current")
        if self.seq_current:
            self.start_current()
        else:
            for p in info.get("parts", []):
                token = p.get("folder")
                resolved = p.get("folder_path") or self._resolve_folder_path(token or "")
                count = 0
                if resolved and os.path.isdir(resolved):
                    count = len([f for f in os.listdir(resolved) if os.path.isfile(os.path.join(resolved, f))])
                self.log(f"[DEBUG] Part '{token}' resolved to '{resolved}', files: {count}")
            self.log("[WARN] No current sequence to play. Check extracted folders and desc.txt entries.")
            QMessageBox.information(self, "No frames", "No playable frames were found in the extracted bootanimation. See log for details.")

    # ---------- sequence builder (unchanged flatten/ordering approach) ----------
    def build_sequence_from_info(self, info: Dict, which: str = "current"):
        fps = int(info.get("fps", 30) or 30)
        frame_ms = int(1000 / max(1, fps))
        def part_index(name: str):
            m = re.fullmatch(r"part(\d+)", name, flags=re.IGNORECASE)
            return int(m.group(1)) if m else None
        part_dirs = []
        if os.path.isdir(EXTRACT_DIR):
            for name in os.listdir(EXTRACT_DIR):
                idx = part_index(name)
                if idx is not None:
                    p = os.path.join(EXTRACT_DIR, name)
                    if os.path.isdir(p):
                        part_dirs.append((idx, p))
        part_dirs.sort(key=lambda t: t[0])
        if not part_dirs:
            self.log("[ERROR] No partN folders found under extract dir.")
            if which == "current":
                self.seq_current = []
            else:
                self.seq_created = []
            return
        def numeric_key(name: str):
            nums = re.findall(r"\d+", name)
            if nums:
                try:
                    return (int(nums[-1]), name.lower())
                except Exception:
                    pass
            return (10**12, name.lower())
        seq: List[Tuple[QPixmap, int]] = []
        total_files = 0
        target_w = self.cur_label.width() if which == "current" else self.new_label.width()
        target_h = self.cur_label.height() if which == "current" else self.new_label.height()
        for idx, folder in part_dirs:
            files = [f for f in os.listdir(folder)
                     if os.path.isfile(os.path.join(folder, f))
                     and f.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp"))]
            files.sort(key=numeric_key)
            total_files += len(files)
            for fname in files:
                path = os.path.join(folder, fname)
                pix = QPixmap(path)
                if pix.isNull():
                    self.log(f"[WARN] Null image skipped: {path}")
                    continue
                pix = pix.scaled(target_w, target_h,
                                 Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
                seq.append((pix, frame_ms))
        self.log(f"[INFO] Loaded {len(seq)} frames (actual files found: {total_files})")
        if which == "current":
            self.seq_current = seq
            self.play_index_current = 0
            self.frame_ms_current = frame_ms
            self.log(f"[INFO] Built current sequence: {len(seq)} frames @ {fps}fps")
        else:
            self.seq_created = seq
            self.play_index_created = 0
            self.frame_ms_created = frame_ms
            self.log(f"[INFO] Built created sequence: {len(seq)} frames @ {fps}fps")

    # ---------- select animation ----------
    def select_animation_file(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Select .mp4 or .gif", "", "Animations (*.mp4 *.mkv *.mov *.avi *.gif);;All Files (*)")
        if not fn:
            return
        self.selected_anim_file = fn
        self.log(f"Selected animation file: {fn}")
        QMessageBox.information(self, "Selected", f"Selected: {os.path.basename(fn)}\nClick Preview to render using current Width/Height/FPS")

    # ---------- Preview flow with threading; do NOT keep extracted folder (clean up) ----------
    def preview_selected_animation(self):
        """
        Re-render (decode) the currently selected animation file using width/height/fps.
        Extracts frames in a background thread, builds in-memory sequence, then removes temp frames dir.
        """
        if not getattr(self, "selected_anim_file", None):
            QMessageBox.warning(self, "No file", "No animation file selected. Use 'Select MP4/GIF' first.")
            return
        sel = self.selected_anim_file
        w = int(self.width_spin.value())
        h = int(self.height_spin.value())
        fps = int(self.fps_spin.value())

        is_gif = sel.lower().endswith(".gif")
        ffmpeg_path_field = self.ffmpeg_edit.text().strip() or self.ffmpeg_resolved_path or ""
        if not is_gif and not ffmpeg_path_field and not shutil.which("ffmpeg"):
            QMessageBox.critical(self, "FFmpeg missing", "FFmpeg not configured in the FFmpeg field and not found in PATH. Set it before previewing videos.")
            return

        self.log(f"[INFO] Extracting frames for preview: {sel} @ {w}x{h} @{fps}fps")
        if is_gif:
            t = FuncThread(extract_gif_frames_sync, sel, w, h, fps)
        else:
            t = FuncThread(extract_video_frames_sync, sel, w, h, ffmpeg_path_field, fps)
        t._preview_src = sel
        t.done.connect(self._on_preview_extraction_done)
        self._start_thread(t)
        self.log("[INFO] Extraction thread started...")

    def _on_preview_extraction_done(self, result, error):
        if error:
            self.log(f"[ERROR] Extraction failed: {error}")
            QMessageBox.critical(self, "Extraction failed", str(error))
            return
        frames_dir = result
        self.log(f"[INFO] Frames extracted to: {frames_dir}")
        # Build sequence from the flat frames (loads QPixmaps into RAM)
        self.build_sequence_from_flat_frames(frames_dir, which="created")
        if getattr(self, "seq_created", None):
            self.start_created()
            self.log("[INFO] Preview playing.")
        else:
            self.log("[WARN] No frames loaded for preview after extraction.")
            QMessageBox.information(self, "Preview", "No frames were loaded for preview. See log for details.")
        # Now cleanup extracted frames on disk (we kept frames in memory)
        try:
            shutil.rmtree(frames_dir)
            self.log(f"[INFO] Cleaned temp frames folder: {frames_dir}")
        except Exception as e:
            self.log(f"[WARN] Failed deleting temp frames: {e}")
        # clear last frames dir reference
        self._last_created_frames_dir = None

    def build_sequence_from_flat_frames(self, frames_dir: str, which: str = "created"):
        if not os.path.isdir(frames_dir):
            self.log(f"[ERROR] Frames folder missing: {frames_dir}")
            return
        try:
            fps = int(self.fps_spin.value()) if getattr(self, "fps_spin", None) else int(getattr(self, "current_info", {}).get("fps", 30) or 30)
        except Exception:
            fps = 30
        frame_ms = int(1000 / max(1, fps))
        files = [f for f in os.listdir(frames_dir) if f.lower().endswith(".png")]
        if not files:
            self.log("[ERROR] No PNG frames found for preview.")
            return
        def keyfn(nm):
            nums = re.findall(r"\d+", nm)
            if nums:
                try:
                    return int(nums[-1])
                except:
                    pass
            return nm.lower()
        files.sort(key=keyfn)
        target_w = self.cur_label.width() if which == "current" else self.new_label.width()
        target_h = self.cur_label.height() if which == "current" else self.new_label.height()
        seq = []
        loaded = 0
        for fname in files:
            path = os.path.join(frames_dir, fname)
            pix = QPixmap(path)
            if pix.isNull():
                self.log(f"[WARN] Skipping null image: {path}")
                continue
            pix = pix.scaled(target_w, target_h, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            seq.append((pix, frame_ms))
            loaded += 1
        self.log(f"[INFO] Loaded {loaded} frames from {frames_dir} @ {fps}fps")
        if which == "current":
            self.seq_current = seq
            self.play_index_current = 0
            self.frame_ms_current = frame_ms
        else:
            # replace any existing created sequence (we now hold frames in RAM)
            # remove previous created frames dir if any
            self.seq_created = seq
            self.play_index_created = 0
            self.frame_ms_created = frame_ms
            self._last_created_frames_dir = None

    # ---------- create / push ----------
    def _detect_created_frames_dir(self) -> Optional[str]:
        # prefer existing extracted folder if present (though we normally clean up after loading)
        if self._last_created_frames_dir and os.path.isdir(self._last_created_frames_dir):
            return self._last_created_frames_dir
        # fallback: nothing (we rely on seq_created in memory)
        return None

    def create_bootanimation_zip(self):
        """
        Create zip either from in-memory seq_created (preferred) or from a frames directory if available.
        Immediately prompt user for save location.
        """
        # prompt for save location first
        fn, _ = QFileDialog.getSaveFileName(self, "Save created bootanimation.zip", self.created_zip_path or "", "ZIP files (*.zip)")
        if not fn:
            self.log("[INFO] Create cancelled by user (no save location).")
            return
        if not fn.lower().endswith(".zip"):
            fn += ".zip"
        self.created_zip_path = fn

        tmp_build = tempfile.mkdtemp(prefix="qadb_build_")
        try:
            part0 = os.path.join(tmp_build, "part0")
            os.makedirs(part0, exist_ok=True)

            # If we have an on-disk frames_dir, prefer that
            frames_dir = self._detect_created_frames_dir()
            if frames_dir and os.path.isdir(frames_dir):
                files = [f for f in os.listdir(frames_dir) if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp"))]
                if not files:
                    QMessageBox.warning(self, "No frames", "No image frames found in selected frames directory.")
                    return
                def keyfn(nm):
                    nums = re.findall(r"\d+", nm)
                    if nums:
                        try:
                            return int(nums[-1])
                        except:
                            pass
                    return nm.lower()
                files.sort(key=keyfn)
                for i, fname in enumerate(files):
                    src = os.path.join(frames_dir, fname)
                    dstname = f"{i:05d}.png"
                    dst = os.path.join(part0, dstname)
                    if not fname.lower().endswith(".png"):
                        try:
                            im = Image.open(src).convert("RGBA")
                            im.save(dst, format="PNG")
                        except Exception as e:
                            self.log(f"[WARN] Failed convert {src}: {e}")
                            shutil.copy2(src, dst)
                    else:
                        shutil.copy2(src, dst)
            else:
                # Build from seq_created in memory (pixmaps)
                if not getattr(self, "seq_created", None):
                    QMessageBox.warning(self, "No frames", "No created frames in memory. Use 'Preview' first.")
                    return
                for i, (pix, _) in enumerate(self.seq_created):
                    dst = os.path.join(part0, f"{i:05d}.png")
                    try:
                        # QPixmap.save accepts PNG path
                        saved = pix.save(dst, "PNG")
                        if not saved:
                            raise RuntimeError("QPixmap.save returned False")
                    except Exception as e:
                        # fallback: create a small blank PNG to preserve ordering
                        self.log(f"[WARN] Failed saving frame {i}: {e}")
                        im = Image.new("RGBA", (self.width_spin.value(), self.height_spin.value()), (0, 0, 0, 0))
                        im.save(dst, "PNG")

            # write desc.txt based on current width/height/fps
            try:
                w = int(self.width_spin.value())
                h = int(self.height_spin.value())
                fps = int(self.fps_spin.value())
            except Exception:
                w, h, fps = 720, 240, 30
            desc = f"{w} {h} {fps}\n" + "p 1 0 part0\n"
            with open(os.path.join(tmp_build, "desc.txt"), "w", encoding="utf-8") as f:
                f.write(desc)

            # create zip
            zip_out = self.created_zip_path
            os.makedirs(os.path.dirname(zip_out) or ".", exist_ok=True)
            with zipfile.ZipFile(zip_out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for rootp, dirs, files2 in os.walk(tmp_build):
                    for fnm in files2:
                        full = os.path.join(rootp, fnm)
                        rel = os.path.relpath(full, tmp_build)
                        zf.write(full, rel)
            self.created_zip_path = zip_out
            self.log(f"[INFO] Created bootanimation.zip: {zip_out}")
            QMessageBox.information(self, "Created", f"Created: {zip_out}")
        except Exception as e:
            self.log(f"[ERROR] Create failed: {e}")
            QMessageBox.critical(self, "Create failed", str(e))
        finally:
            try:
                shutil.rmtree(tmp_build)
            except Exception:
                pass

    def push_zip_flow(self):
        """
        Replace older push_created_zip flow: show a dialog to either push the created zip or let user select one.
        """
        dlg = PushDialog(self, created_path=self.created_zip_path if self.created_zip_path and os.path.exists(self.created_zip_path) else None)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self.log("[INFO] Push cancelled by user.")
            return
        chosen = dlg.path
        if not chosen or not os.path.exists(chosen):
            QMessageBox.warning(self, "No ZIP", "No ZIP selected or found.")
            return
        # run push in background with verbose logging returned
        t = FuncThread(self._push_zip_verbose_sync, chosen, self.remote_path, self.adb_cmd)
        t.done.connect(self._on_push_done)
        self._start_thread(t)
        self.log("[INFO] Push thread started...")

    def _on_push_done(self, result, error):
        if error:
            self.log(f"[ERROR] Push failed: {error}")
            QMessageBox.critical(self, "Push failed", str(error))
            return
        ok_msg = result
        # result is a multiline log string
        for ln in ok_msg.splitlines():
            self.log(ln)
        QMessageBox.information(self, "Push result", ok_msg)
        # optionally re-pull the device boot animation to refresh current preview/state
        try:
            self.pull_remote()
        except Exception:
            pass

    def _push_zip_verbose_sync(self, local_zip: str, remote_path: Optional[str], adb_cmd: str = ADB_CMD) -> str:
        """
        Push the given local_zip to the device. Produce a verbose log describing each step.
        Returns a string (log).
        """
        logs = []
        if not os.path.exists(local_zip):
            raise RuntimeError("Local ZIP missing")
        if not remote_path:
            raise RuntimeError("Remote install path unknown. Run Find/Pull first.")
        tmp_remote = "/data/local/tmp/quickadb_push_bootanim.zip"
        logs.append(f"[PUSH] Pushing {local_zip} -> {tmp_remote} via adb push")
        push = subprocess.run([adb_cmd, "push", local_zip, tmp_remote], capture_output=True, text=True, timeout=300)
        logs.append(f"[PUSH] adb push rc={push.returncode}")
        if push.stdout:
            logs.append(f"[PUSH] stdout: {push.stdout.strip()}")
        if push.stderr:
            logs.append(f"[PUSH] stderr: {push.stderr.strip()}")
        if push.returncode != 0:
            raise RuntimeError("\n".join(logs + ["adb push failed"]))
        # attempt copy using root
        cp_cmd = f'su -c cp "{tmp_remote}" "{remote_path}" && su -c chmod 0644 "{remote_path}"'
        logs.append(f"[PUSH] Attempting root copy to {remote_path}")
        proc = subprocess.run([adb_cmd, "shell", cp_cmd], shell=False, capture_output=True, text=True, timeout=60)
        logs.append(f"[PUSH] root copy rc={proc.returncode}")
        if proc.stdout:
            logs.append(f"[PUSH] stdout: {proc.stdout.strip()}")
        if proc.stderr:
            logs.append(f"[PUSH] stderr: {proc.stderr.strip()}")
        # if success
        if proc.returncode == 0:
            # cleanup tmp remote
            subprocess.run([adb_cmd, "shell", "su", "-c", f'rm "{tmp_remote}"'], capture_output=True)
            logs.append(f"[PUSH] Installed to {remote_path} (root copy succeeded)")
            return "\n".join(logs)
        # try remount RW then copy
        logs.append("[PUSH] Root copy failed; attempting remount and retry")
        remount_cmd = [adb_cmd, "shell", "su", "-c", 'mount -o remount,rw /system || mount -o remount,rw /system_root || true']
        rem = subprocess.run(remount_cmd, capture_output=True, text=True, timeout=20)
        logs.append(f"[PUSH] remount rc={rem.returncode}")
        if rem.stdout:
            logs.append(f"[PUSH] remount stdout: {rem.stdout.strip()}")
        if rem.stderr:
            logs.append(f"[PUSH] remount stderr: {rem.stderr.strip()}")
        # retry copy
        proc2 = subprocess.run([adb_cmd, "shell", cp_cmd], shell=False, capture_output=True, text=True, timeout=60)
        logs.append(f"[PUSH] retry root copy rc={proc2.returncode}")
        if proc2.stdout:
            logs.append(f"[PUSH] stdout: {proc2.stdout.strip()}")
        if proc2.stderr:
            logs.append(f"[PUSH] stderr: {proc2.stderr.strip()}")
        if proc2.returncode == 0:
            subprocess.run([adb_cmd, "shell", "su", "-c", f'rm "{tmp_remote}"'], capture_output=True)
            logs.append(f"[PUSH] Installed to {remote_path} after remount")
            return "\n".join(logs)
        # final cleanup and error
        subprocess.run([adb_cmd, "shell", "su", "-c", f'rm "{tmp_remote}"'], capture_output=True)
        logs.append("[PUSH] All methods failed to copy to target.")
        raise RuntimeError("\n".join(logs))

    # ---------- backup stock bootanimation (separate action) ----------
    def backup_stock_bootanimation(self):
        if not os.path.exists(PULLED_ZIP):
            QMessageBox.warning(self, "No pulled zip", "No pulled bootanimation.zip found to backup. Pull from device first.")
            return
        d = QFileDialog.getExistingDirectory(self, "Select backup directory (Cancel to skip)", self.backup_dir)
        if not d:
            self.log("[INFO] Backup cancelled by user.")
            return
        try:
            os.makedirs(d, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            bak_name = os.path.join(d, f"bootanimation_backup_{ts}.zip")
            shutil.copy2(PULLED_ZIP, bak_name)
            self.backup_dir = d
            self.log(f"[INFO] Backed up pulled original to: {bak_name}")
            QMessageBox.information(self, "Backup", f"Backup created:\n{bak_name}")
        except Exception as e:
            self.log(f"[WARN] Failed backup copy: {e}")
            QMessageBox.critical(self, "Backup failed", str(e))

    # ---------- module creation flow ----------
    def create_flashable_module_flow(self):
        # Dialog to collect module metadata and save path
        if not getattr(self, "created_zip_path", None) or not os.path.exists(self.created_zip_path):
            QMessageBox.warning(self, "No created zip", "You need to create a bootanimation.zip first.")
            return
        if not getattr(self, "remote_path", None):
            QMessageBox.warning(self, "No remote path", "You must detect/pull the original bootanimation.zip first to know the install path.")
            return

        dlg = ModuleDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            self.log("[INFO] Module creation cancelled by user.")
            return
        save_path = dlg.save_path
        meta = dlg.get_meta()
        # start background thread to build module zip
        t = FuncThread(create_module_sync, meta, save_path, self.created_zip_path, self.remote_path, "fl0w's QuickADB")
        t.done.connect(self._on_module_created)
        self._start_thread(t)
        self.log("[INFO] Module build thread started...")

    def _on_module_created(self, result, error):
        if error:
            self.log(f"[ERROR] Module creation failed: {error}")
            QMessageBox.critical(self, "Module failed", str(error))
            return
        self.log(f"[INFO] {result}")
        QMessageBox.information(self, "Module created", result)

    # ---------- playback tickers / controls ----------
    def _tick_current(self):
        if not getattr(self, "seq_current", None):
            return
        total = len(self.seq_current)
        if total == 0:
            return
        now_ms = int(time.time() * 1000)
        start = getattr(self, "start_time_current", None)
        if start is None:
            return
        elapsed = now_ms - start
        frame_duration = getattr(self, "frame_ms_current", 33)
        frame_index_float = elapsed / frame_duration
        idx = int(frame_index_float) % total
        if idx != self.play_index_current:
            self.play_index_current = idx
            pix, _ = self.seq_current[idx]
            self.cur_label.setPixmap(pix)

    def _tick_created(self):
        if not getattr(self, "seq_created", None):
            return
        total = len(self.seq_created)
        if total == 0:
            return
        now_ms = int(time.time() * 1000)
        start = getattr(self, "start_time_created", None)
        if start is None:
            return
        elapsed = now_ms - start
        frame_duration = getattr(self, "frame_ms_created", 33)
        frame_index_float = elapsed / frame_duration
        idx = int(frame_index_float) % total
        if idx != self.play_index_created:
            self.play_index_created = idx
            pix, _ = self.seq_created[idx]
            self.new_label.setPixmap(pix)

    def start_current(self):
        if not getattr(self, "seq_current", None):
            self.log("No current sequence to play.")
            return
        self.start_time_current = int(time.time() * 1000)
        self.play_index_current = 0
        self._last_logged_current = -1
        self.timer_current.stop()
        self.timer_current.setInterval(16)
        try:
            self.timer_current.timeout.disconnect()
        except Exception:
            pass
        self.timer_current.timeout.connect(self._tick_current)
        self.timer_current.start()
        self.playing_current = True
        self.cur_play_btn.setText("⏸")
        if self.seq_current:
            pix, _ = self.seq_current[0]
            self.cur_label.setPixmap(pix)

    def start_created(self):
        if not getattr(self, "seq_created", None):
            self.log("No created sequence to play.")
            return
        self.start_time_created = int(time.time() * 1000)
        self.play_index_created = 0
        self._last_logged_created = -1
        self.timer_created.stop()
        self.timer_created.setInterval(16)
        try:
            self.timer_created.timeout.disconnect()
        except Exception:
            pass
        self.timer_created.timeout.connect(self._tick_created)
        self.timer_created.start()
        self.playing_created = True
        self.new_play_btn.setText("⏸")
        if self.seq_created:
            pix, _ = self.seq_created[0]
            self.new_label.setPixmap(pix)

    def toggle_current(self):
        if self.playing_current:
            self.timer_current.stop()
            self.playing_current = False
            self.cur_play_btn.setText("▶")
            self.log(f"[PAUSE] Paused at frame {self.play_index_current}")
        else:
            self.start_current()

    def toggle_created(self):
        if self.playing_created:
            self.timer_created.stop()
            self.playing_created = False
            self.new_play_btn.setText("▶")
            self.log(f"[PAUSE] Paused at frame {self.play_index_created}")
        else:
            self.start_created()

    # ---------- misc UI helpers ----------
    def choose_created_zip(self):
        fn, _ = QFileDialog.getSaveFileName(self, "Choose created bootanimation.zip", self.created_zip_path, "ZIP files (*.zip)")
        if not fn:
            return
        if not fn.lower().endswith(".zip"):
            fn += ".zip"
        self.created_zip_path = fn
        self.log(f"Created zip set to: {fn}")

    def closeEvent(self, ev):
        try:
            self.timer_current.stop(); self.timer_created.stop()
        except Exception:
            pass
        # try to quit running threads
        for t in list(self._threads):
            try:
                if t.isRunning():
                    t.quit()
                    t.wait(2000)
            except Exception:
                pass
        # cleanup temporary work dir
        try:
            cleanup_work_dirs()
            self.log("[INFO] Cleaned temporary work directory.")
        except Exception:
            pass
        super().closeEvent(ev)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = BootAnimWidget()
    w.show()
    sys.exit(app.exec())
