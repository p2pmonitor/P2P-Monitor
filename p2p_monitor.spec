# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for P2P Monitor — Windows build
#
# Usage:
#   pip install pyinstaller
#   pyinstaller p2p_monitor.spec
#
# Output: dist/P2P Monitor.exe (single file, no console window)
#
# Requirements (must be installed in your build environment):
#   pip install -r requirements-windows.txt

block_cipher = None

a = Analysis(
    ['p2p_monitor.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=[
        # discord.py and its dependencies
        'discord',
        'discord.ext',
        'discord.ext.commands',
        'discord.gateway',
        'discord.http',
        'discord.state',
        'discord.ui',
        'aiohttp',
        'aiohttp.connector',
        # psutil
        'psutil',
        'psutil._pswindows',
        # PIL / Pillow
        'PIL',
        'PIL.Image',
        'PIL.ImageGrab',
        'PIL.ImageDraw',
        # stdlib modules that PyInstaller sometimes misses
        'tkinter',
        'tkinter.ttk',
        'tkinter.messagebox',
        'tkinter.filedialog',
        'pystray',
        'webbrowser',
        'urllib.request',
        'zipfile',
        'tempfile',
        'threading',
        'platform',
    ],
    excludes=[
        # Linux-only — not needed in Windows build
        'curses',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='P2P Monitor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,       # no console window — Tkinter app
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon='assets/icon.ico',  # uncomment and add icon file when available
)
