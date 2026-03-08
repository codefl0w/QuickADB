<img width="500" height="125" alt="QuickADB Logo" src="https://github.com/user-attachments/assets/2531e507-63f1-4b23-b48e-ad4edd99d6d9" />

A powerful GUI wrapper for ADB and Fastboot, built for Android developers and power users. Focused on reliability and speed, it provides one-click flows for common device tasks while keeping advanced tools available for power users.


![Downloads](https://codefl0w.xyz/gh-boards/out/codefl0w/badge/QuickADB/badge_downloads_2.svg)
![Stars](https://codefl0w.xyz/gh-boards/out/codefl0w/badge/QuickADB/badge_stars_2.svg)
![Platform](https://codefl0w.xyz/gh-boards/out/codefl0w/profile/badge_custom.svg)
[![License](https://codefl0w.xyz/gh-boards/out/codefl0w/badge/QuickADB/badge_license_2.svg)](LICENSE)
![Workflow](https://codefl0w.xyz/gh-boards/out/codefl0w/badge/QuickADB/badge_workflow_2_latest.svg)

---

## Table of Contents

- [Overview](#overview)
- [Screenshots](#screenshots)
- [Platform Support](#platform-support)
- [Requirements](#requirements)
- [Installation](#installation)
- [Building from Source](#building-from-source)
- [Usage](#usage)
- [Features](#features)
- [Giving Root Permissions](#giving-root-permissions)
- [Changelog](#changelog)
- [Contributing](#contributing)
- [Donate](#donate)
- [License](#license)

---

## Overview

QuickADB is a GUI wrapper for ADB and Fastboot that eliminates the need to memorize or type commands manually. 

> [!NOTE]
> QuickADB has been completely rebuilt. To see the old changelog, view [README_OLD](https://github.com/codefl0w/QuickADB/blob/main/README_OLD.md).

---

## Screenshots


<img width="2000" height="1500" alt="showcase" src="https://github.com/user-attachments/assets/59841fd7-ec8f-4618-a31d-a9a196a38ed1" />



---

## Platform Support

| Platform | Supported | Notes                    |
|----------|-----------|--------------------------|
| Windows  | Yes       | Primary target           |
| Linux    | Yes       | Some libs may mismatch with your distro and cause issues. If so, create an issue  |
| macOS    | Yes       | Untested, please make sure to provide feedback        |

---

## Requirements

- Python 3.10 or higher
- USB debugging enabled on the target device
- _(For root features)_ A rooted device with `su` access

---

## Installation

### Prebuilt Release

1. Download the latest release for your OS from the [Releases page](https://github.com/codefl0w/QuickADB/releases/latest).
2. Run the executable.

---

## Building from Source

```bash
# Clone the repository
git clone https://github.com/codefl0w/QuickADB.git
cd QuickADB

# Install dependencies
pip install -r requirements.txt

# Build standalone executable
python build.py  # (auto-downloads platform-tools and payload-dumper-go and builds AppImage on Linux. Recommended)
# or
pyinstaller QuickADB.spec # You must provide the platform-tools at the root of the project for commands to work, see developer.android.com/tools/releases/platform-tools#downloads
```

Output binary will be located in `dist/`.

---

## Usage

1. Connect your Android device via USB.
2. Enable **USB Debugging** in Developer Options.
3. Launch QuickADB.
4. Authorize your device's ADB connection by accepting the pop-up on your device (first connection only).

For wireless ADB, use the corresponding button in the ADB section of the tool.

---

## Features

### ADB
Execute the most common ADB commands with a single click: reboot options, wireless ADB pairing, sideloading, and more.

### Fastboot
Execute the most common Fastboot commands with a single click: reboot options, fetching device variables, flashing images, and more.

### Terminal
A custom terminal with autocomplete support. Search for keywords in output, export the output to a `.txt` file, kill running processes, drag-and-drop files, and navigate command history with arrow keys.

### Advanced

- **ADB App Manager** - Browse all installed apps. Uninstall, disable, view details, modify permissions, backup and restore APKs, and create or apply debloat presets.
- **ADB File Explorer** - Browse the full device filesystem. Create, rename, delete, copy, paste, pull and push files, manage `chmod` permissions, preview images, edit text files, execute shell scripts, and manage root directories.
- **GSI Flasher** — Detects device state and automatically flashes a GSI ROM, removing unneeded dynamic partitions on demand, without requiring a single manual command.
- **Partition Manager** - View device partitions, create backups, and flash partition images while the device is powered. _(Requires root)_
- **Super.img Dumper** - Extract individual partitions from a `super.img` file. Powered by [unsuper](https://github.com/codefl0w/unsuper).
- **Payload.bin Dumper** - Extract individual partitions from a `payload.bin` file. Powered by [ssut's payload-dumper-go](https://github.com/ssut/payload-dumper-go).

### Miscellaneous

- **Device Specifications** - View RAM, storage, Android version, root method, and more at a glance.
- **Boot Animation Creator** - View, back up, and modify your boot animation. Create a new animation from a GIF or video and flash it, or package it as a Magisk module.

---

## Giving Root Permissions

Features that require root access are marked with a `#` icon within the application. No root is required for standard ADB and Fastboot operations.

For root operations to work, you must grant root access to com.android.shell on device.

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for the full version history.

For the legacy changelog (pre-rebuild), see [README_OLD](https://github.com/codefl0w/QuickADB/blob/main/README_OLD.md).

---

## Contributing

Contributions are welcome. Please open an issue before submitting a pull request for non-trivial changes.

```
1. Fork the repository
2. Create a feature branch (git checkout -b feature/your-feature)
3. Commit your changes
4. Open a pull request against main
```

Please follow the existing code style and make sure the code runs on at least one platform.

---

## Credits

- [payload-dumper-go](https://github.com/ssut/payload-dumper-go) by ssut — Payload.bin extraction


---

## Donate


If QuickADB has been useful to you, please consider supporting its development via Buy Me a Coffee or GitHub Sponsors.


---

## License

This project is licensed under the [GNU General Public License v3.0](LICENSE).
