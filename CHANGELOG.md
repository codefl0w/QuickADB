# Changelog (Starting from V4)
The changelog only includes what's new. To read feature descriptions, see [Features](https://github.com/codefl0w/QuickADB?tab=readme-ov-file#features).

For the old changelog, see [README_OLD](https://github.com/codefl0w/QuickADB/blob/main/README_OLD.md).


# [5.0.0] - 29.03.2026

### Added

- FOSS Market: fully functional F-Droid client with automatic app installations, custom repos and app data (Miscellaneous)
- Magisk Manager: fully automated device rooting & module management (Advanced)

### Changed

- Updater: move function from quickadb.py to standalone updater.py
- QuickADB: changed button order in the advanced section
- QuickADB: update & improve credits


### Improved

- Device Manager: include manufacturer names
- Device Manager: scale box size according to name length
- GSI Flasher: massive code cleanup & refactoring
- Updater: display latest changelog and add in-place updating
- File Explorer: Add Install APK option to the right click menu
- Overall code & text cleanup

# [4.1.0] - 26.03.2026

### Added

- Device selection support
- Centralized tool path management
- Install APK flags

### Changed

- QuickADB: Re-labeled "extract logs" to "export logs"
- QuickADB: Changed contact method from email to website
- GSI Flasher: Removed support for compressed GSI images (.img.gz, .img.xz)
- Wireless ADB: fixed freezing on 15-second timeout


### Improved

- File Explorer: Refactored code to eliminate duplication and redundancy


# [4.0.2] - 14.03.2026

### Added

- Wireless ADB: Pairing via QR or pairing code

### Changed

- Wireless ADB: Moved function from adbfunc.py to wirelessadb.py
- File Explorer: Changed header scaling method

### Improved

- File Explorer: Added many new viewable text file extensions



# [4.0.1] - 13.03.2026

### Added

- Boot Animation Creator: Reload frames from workdir button

### Changed

- Boot Animation Creator: Frame handling logic

### Fixed

- Boot Animation Creator: FFmpeg PATH detection



# [4.0.0] - 10.03.2026

### Added

- Theme Manager
- Boot Animation Creator
- File Explorer
- Custom terminal
- Partition Manager

### Changed

- Rebuilt application using PyQt6
- Debloater renamed and expanded into App Manager
- Replaced lpunpack with unsuper for faster super.img extraction
- Removed OS-specific code enabling Linux and macOS builds

### Improved

- Multithreading performance
- GSI flasher
- super.img dumper
- payload.bin dumper
- device specification detection
- version update detection
- logging system

### Removed

- Driver installers
- Magisk downloader
- Inactive Magisk Root button (planned for V5)
