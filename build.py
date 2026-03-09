#!/usr/bin/env python3

"""
build.py - Build script for QuickADB. 
Downloads external dependancies and uses PyInstaller to compile the tool into a single binary.
Linux builds are also wrapped into AppImage files.

"""

import os
import sys
import platform
import urllib.request
import zipfile
import tarfile
import gzip
import shutil
import subprocess

# URLs for dependencies
PLATFORM_TOOLS_URLS = {
    "Windows": "https://dl.google.com/android/repository/platform-tools-latest-windows.zip",
    "Linux": "https://dl.google.com/android/repository/platform-tools-latest-linux.zip",
    "Darwin": "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip"
}

PAYLOAD_DUMPER_URLS = {
    "Windows": "https://github.com/ssut/payload-dumper-go/releases/download/1.3.0/payload-dumper-go_1.3.0_windows_amd64.tar.gz",
    "Linux": "https://github.com/ssut/payload-dumper-go/releases/download/1.3.0/payload-dumper-go_1.3.0_linux_amd64.tar.gz",
    "Darwin": "https://github.com/ssut/payload-dumper-go/releases/download/1.3.0/payload-dumper-go_1.3.0_darwin_amd64.tar.gz"
}

APPIMAGE_TOOL_URL = "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
UTIL_DIR = os.path.join(ROOT_DIR, "util")
DIST_DIR = os.path.join(ROOT_DIR, "dist")

def download_file(url, dest):
    print(f"Downloading {url}...")
    urllib.request.urlretrieve(url, dest)

def extract_zip(src, dest_dir):
    print(f"Extracting {src} to {dest_dir}...")
    with zipfile.ZipFile(src, 'r') as zip_ref:
        zip_ref.extractall(dest_dir)

def extract_targz_payload_dumper(src, dest_dir):
    print(f"Extracting {src} to {dest_dir}...")
    # First unpack .gz to a temporary .tar file
    tar_path = src.replace(".tar.gz", ".tar")
    with gzip.open(src, 'rb') as f_in:
        with open(tar_path, 'wb') as f_out:
            shutil.copyfileobj(f_in, f_out)
    
    # Then extract .tar
    with tarfile.open(tar_path, 'r') as tar_ref:
        tar_ref.extractall(dest_dir)
    
    # Cleanup temp .tar
    if os.path.exists(tar_path):
        os.remove(tar_path)

def create_linux_appimage():
    print("Creating Linux AppImage...")
    appdir = os.path.join(ROOT_DIR, "QuickADB.AppDir")
    if os.path.exists(appdir):
        shutil.rmtree(appdir)
    
    os.makedirs(os.path.join(appdir, "usr/bin"), exist_ok=True)
    
    # 1. Copy PyInstaller output to AppDir
    exe_src = os.path.join(DIST_DIR, "QuickADB")
    exe_dest = os.path.join(appdir, "usr/bin/QuickADB")
    if not os.path.exists(exe_src):
        print(f"Error: Bundled executable not found at {exe_src}")
        return
    shutil.copy2(exe_src, exe_dest)
    os.chmod(exe_dest, 0o755)

    # 2. Create Desktop file
    desktop_content = """[Desktop Entry]
Type=Application
Name=QuickADB
Exec=QuickADB
Icon=QuickADB
Categories=Utility;System;
Terminal=false
"""
    with open(os.path.join(appdir, "QuickADB.desktop"), "w") as f:
        f.write(desktop_content)

    # 3. Handle Icon (use logo.svg as QuickADB.svg)
    icon_src = os.path.join(ROOT_DIR, "res/toolicon.png")
    if os.path.exists(icon_src):
        shutil.copy2(icon_src, os.path.join(appdir, "QuickADB.png"))
    
    # 4. Create AppRun script
    apprun_content = """#!/bin/sh
SELF=$(readlink -f "$0")
HERE=$(dirname "$SELF")
export PATH="${HERE}/usr/bin:${PATH}"
exec QuickADB "$@"
"""
    apprun_path = os.path.join(appdir, "AppRun")
    with open(apprun_path, "w") as f:
        f.write(apprun_content)
    os.chmod(apprun_path, 0o755)

    # 5. Download appimagetool
    tool_path = os.path.join(ROOT_DIR, "appimagetool")
    if not os.path.exists(tool_path):
        download_file(APPIMAGE_TOOL_URL, tool_path)
        os.chmod(tool_path, 0o755)

    # 6. Build AppImage
    # ARCH=x86_64 is required by appimagetool
    env = os.environ.copy()
    env["ARCH"] = "x86_64"
    
    # Running with --appimage-extract-and-run for GitHub Actions support (FUSE-less environments)
    cmd = [tool_path, "--appimage-extract-and-run", "--comp", "zstd", appdir]
    try:
        subprocess.run(cmd, check=True, env=env, cwd=ROOT_DIR)
        print("AppImage created successfully!")
    except subprocess.CalledProcessError as e:
        print(f"AppImage creation failed with exit code {e.returncode}")

def cleanup_processes():
    """Terminated known background processes to avoid file locking on Windows."""
    if platform.system() != "Windows":
        return
    
    processes = ["adb.exe", "fastboot.exe", "payload-dumper-go.exe", "QuickADB.exe"]
    print("Cleaning up background processes...")
    for proc in processes:
        try:
            # taskkill /F /IM <proc> /T
            subprocess.run(["taskkill", "/F", "/IM", proc, "/T"], 
                           capture_output=True, check=False)
        except Exception:
            pass

def main():
    os_name = platform.system()
    if os_name not in PLATFORM_TOOLS_URLS:
        print(f"Unsupported OS: {os_name}")
        sys.exit(1)

    print(f"Building for {os_name}...")

    # Pre-build cleanup
    cleanup_processes()

    #  Clean old build/dist and dependencies
    print("Cleaning environment...")
    dirs_to_clean = ["build", "dist", "QuickADB.AppDir", "platform-tools", "temp_pd"]
    for d in dirs_to_clean:
        full_d = os.path.join(ROOT_DIR, d)
        if os.path.exists(full_d):
            try:
                shutil.rmtree(full_d)
            except Exception as e:
                print(f"Warning: Could not remove {d}: {e}")

    # Clean specific binaries in util
    pd_binary = "payload-dumper-go"
    if os_name == "Windows":
        pd_binary += ".exe"
    pd_dest = os.path.join(UTIL_DIR, pd_binary)
    if os.path.exists(pd_dest):
        try:
            os.remove(pd_dest)
        except Exception as e:
            print(f"Warning: Could not remove {pd_binary}: {e}")

    # Clean appimagetool if it exists
    tool_path = os.path.join(ROOT_DIR, "appimagetool")
    if os.path.exists(tool_path):
        try:
            os.remove(tool_path)
        except Exception as e:
            print(f"Warning: Could not remove appimagetool: {e}")

    # 1. Download and Extract Platform Tools
    pt_zip = os.path.join(ROOT_DIR, "platform-tools.zip")
    download_file(PLATFORM_TOOLS_URLS[os_name], pt_zip)
    extract_zip(pt_zip, ROOT_DIR)
    os.remove(pt_zip)

    # 2. Download and Extract Payload Dumper Go
    pd_targz = os.path.join(ROOT_DIR, "payload-dumper-go.tar.gz")
    download_file(PAYLOAD_DUMPER_URLS[os_name], pd_targz)
    
    temp_pd_dir = os.path.join(ROOT_DIR, "temp_pd")
    os.makedirs(temp_pd_dir, exist_ok=True)
    extract_targz_payload_dumper(pd_targz, temp_pd_dir)
    
    binary_name = "payload-dumper-go"
    if os_name == "Windows":
        binary_name += ".exe"
    
    src_binary = os.path.join(temp_pd_dir, binary_name)
    if not os.path.exists(src_binary):
        for root, dirs, files in os.walk(temp_pd_dir):
            for f in files:
                if f.startswith("payload-dumper-go"):
                    src_binary = os.path.join(root, f)
                    break
    
    if os.path.exists(src_binary):
        dest_binary = os.path.join(UTIL_DIR, binary_name)
        os.makedirs(UTIL_DIR, exist_ok=True)
        shutil.move(src_binary, dest_binary)
        if os_name != "Windows":
            os.chmod(dest_binary, 0o755)
        print(f"Moved {binary_name} to {UTIL_DIR}")
    else:
        print(f"Error: Could not find {binary_name} in the archive.")

    shutil.rmtree(temp_pd_dir)
    os.remove(pd_targz)

    # 3. Handle platform-tools permissions
    pt_dir = os.path.join(ROOT_DIR, "platform-tools")
    if os_name != "Windows" and os.path.exists(pt_dir):
        for f in ["adb", "fastboot", "mke2fs"]:
            fpath = os.path.join(pt_dir, f)
            if os.path.exists(fpath):
                os.chmod(fpath, 0o755)

    # 4. Run PyInstaller
    print("Starting PyInstaller build...")
    spec_path = os.path.join(ROOT_DIR, "QuickADB.spec")
    build_cmd = ["pyinstaller", "--noconfirm", spec_path]
    
    try:
        subprocess.run(build_cmd, check=True, cwd=ROOT_DIR)
        print("PyInstaller build completed successfully!")
    except subprocess.CalledProcessError as e:
        print(f"PyInstaller build failed with exit code {e.returncode}")
        sys.exit(e.returncode)

    # 5. Linux AppImage Step
    if os_name == "Linux":
        create_linux_appimage()

if __name__ == "__main__":
    main()
