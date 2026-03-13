
# Why?
Due to several ongoing builds being available, FFmpeg is not packaged into QuickADB as of V4. Thus, creating boot animations out of videos and rendering the current animation won't be possible unless you manually supply a working FFmpeg binary yourself.
This could change in the future but is not guaranteed.

# Getting FFmpeg
## Globally
At start, the Boot Animation Creator will check PATH for FFmpeg. To keep everything smooth, the easy way to supply FFmpeg is through your package manager.

**Windows:**
```
winget install Gyan.FFmpeg
```
> [!NOTE]
> [Winget](https://github.com/microsoft/winget-cli) may not exist natively and should be installed first.

**Ubuntu / Debian:**
```
sudo apt install ffmpeg
```

**Arch / Manjaro:**
```
sudo pacman -S ffmpeg
```

**Fedora:**
```
sudo dnf install ffmpeg
```

**openSUSE:**
```
sudo zypper install ffmpeg
```

**macOS:**
```
brew install ffmpeg
```
> [!NOTE]
> [Homebrew](https://brew.sh) must be installed first.

## As a downloaded binary
Find an FFmpeg build for your platform and download it. Many sources are available, so it may take a while to find a suitable one.

For Windows and Linux, [BtbN's automated builds](https://github.com/BtbN/FFmpeg-Builds) can help.
For macOS, pre-built binaries are available at [evermeet.cx](https://evermeet.cx/ffmpeg/).

After downloading, unzip the archive and point QuickADB to the FFmpeg binary located at `bin/ffmpeg` (Linux/macOS) or `bin/ffmpeg.exe` (Windows). If the binary is valid, QuickADB will automatically log its version string.
