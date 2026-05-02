# P2P Monitor — Windows Build Guide

## Requirements

- Windows 10/11 (64-bit)
- Python 3.10+ for Windows: https://python.org/downloads
  - During install, check **"Add Python to PATH"**
- Git (optional): https://git-scm.com

## Build Steps

```
# 1. Clone or extract the source
cd P2P-Monitor

# 2. Install PyInstaller first
pip install pyinstaller

# 3. Install all build dependencies
#    This includes discord.py, Pillow, psutil, and pystray.
#    discord.py is REQUIRED for the bot feature and cannot be installed at runtime.
pip install -r requirements-windows.txt

# 4. Verify discord.py is importable — critical before building
python -c "import discord; print('discord.py OK:', discord.__version__)"

# 5. Verify Pillow is importable
python -c "from PIL import Image, ImageGrab, ImageChops; print('Pillow OK')"

# 6. Build the executable
pyinstaller p2p_monitor.spec

# 7. Output location
#    dist/P2P Monitor.exe
```

The output is a single `.exe` — no installer needed. Place it anywhere and run directly.

> **Important:** Steps 4 and 5 are not optional. If either check fails, fix the
> missing dependency before running PyInstaller. A build that starts without
> discord.py installed will produce an exe that cannot use the Discord bot feature.

## Known Limitations (Beta)

- **Auto-update**: Windows builds open the GitHub releases page instead of patching
  in place. Download and replace the `.exe` manually.
- **discord.py bot**: Must be bundled at build time. The bot will not self-install
  missing dependencies in a packaged build.
- **Windows offline detection**: If DreamBot closes unexpectedly, the monitor may
  not immediately detect the account as offline. Status will update within 2 minutes
  or on next monitor restart.

## Distribution

Upload `dist/P2P Monitor.exe` as a GitHub Release asset alongside the Linux zip.

## Troubleshooting

**App launches but closes immediately**
Run from a terminal to see error output:
```
cd "dist"
"P2P Monitor.exe"
```

**Discord bot not connecting / "discord.py not bundled" error**
discord.py was not installed when PyInstaller ran. Fix:
```
pip install discord.py
pyinstaller p2p_monitor.spec
```
Then verify with step 4 above before rebuilding.

**Screenshot capture fails**
Pillow was not installed correctly. Fix:
```
pip install Pillow
pyinstaller p2p_monitor.spec
```
Then verify with step 5 above before rebuilding.

**UPX not found warning during build**
Safe to ignore — UPX is optional compression. The exe will still build correctly.

**Antivirus flags the exe**
PyInstaller executables are sometimes flagged by antivirus due to how they package
Python. This is a false positive. You can verify the exe at https://virustotal.com
before distributing.
