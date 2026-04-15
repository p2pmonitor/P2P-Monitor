# P2P Monitor — Windows Build Guide

## Requirements

- Windows 10/11 (64-bit)
- Python 3.10+ for Windows: https://python.org/downloads
- PyInstaller: `pip install pyinstaller`
- Build dependencies: `pip install -r requirements-windows.txt`

## Build Steps

```
# 1. Clone or extract the source
cd P2P-Monitor

# 2. Install build dependencies
pip install -r requirements-windows.txt
pip install pyinstaller

# 3. Build the executable
pyinstaller p2p_monitor.spec

# 4. Output location
#    dist/P2P Monitor.exe
```

The output is a single `.exe` — no installer needed. Users can place it anywhere and run it directly.

## Known Limitations (Beta)

- **Paint hide/show**: Not supported on Windows. Screenshots are taken without hiding the paint overlay.
- **Auto-update**: Windows builds open the GitHub releases page instead of patching in place. Download and replace the `.exe` manually.
- **discord.py bot**: Must be bundled at build time. The bot feature will not self-install missing dependencies on Windows.

## Distribution

Upload `dist/P2P Monitor.exe` as a GitHub Release asset alongside the Linux zip.
Mark Windows releases as **Pre-release** until fully tested.

## Troubleshooting

**App launches but closes immediately**
Run from a terminal to see error output:
```
"P2P Monitor.exe"
```

**Screenshot capture fails**
Pillow is required for Windows screenshots. Verify the build includes PIL:
```
pyinstaller --collect-all PIL p2p_monitor.spec
```

**Discord bot not connecting**
Ensure discord.py is installed in your build environment before running PyInstaller:
```
pip install discord.py
pyinstaller p2p_monitor.spec
```
