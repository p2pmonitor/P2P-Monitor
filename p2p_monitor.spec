# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for P2P Monitor — Windows build
#
# Usage:
#   pip install pyinstaller
#   pip install -r requirements-windows.txt
#   python -c "import discord; print('discord.py OK')"   # verify before building
#   python -c "from PIL import Image, ImageGrab, ImageChops; print('Pillow OK')"
#   pyinstaller p2p_monitor.spec
#
# Output: dist/P2P Monitor.exe (single file, no console window)

block_cipher = None

a = Analysis(
    ['p2p_monitor.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=[
        # discord.py and all required submodules
        'discord',
        'discord.ext',
        'discord.ext.commands',
        'discord.gateway',
        'discord.http',
        'discord.state',
        'discord.ui',
        'discord.opus',
        'discord.voice_client',
        'discord.webhook',
        'discord.webhook.sync',
        'discord.webhook.async_',
        # aiohttp (discord.py dependency)
        'aiohttp',
        'aiohttp.connector',
        'aiohttp.resolver',
        'aiohttp.cookiejar',
        'aiohttp.client_exceptions',
        # psutil
        'psutil',
        'psutil._pswindows',
        'psutil._psutil_windows',
        # PIL / Pillow — all modules used by P2P Monitor
        'PIL',
        'PIL.Image',
        'PIL.ImageGrab',
        'PIL.ImageDraw',
        'PIL.ImageChops',
        # pystray (system tray)
        'pystray',
        'pystray._win32',
        # tkinter
        'tkinter',
        'tkinter.ttk',
        'tkinter.messagebox',
        'tkinter.filedialog',
        'tkinter.font',
        'tkinter.simpledialog',
        # stdlib modules PyInstaller sometimes misses
        'webbrowser',
        'urllib.request',
        'urllib.parse',
        'zipfile',
        'tempfile',
        'threading',
        'platform',
        'shlex',
        'json',
        'pathlib',
        'logging',
        'asyncio',
        'asyncio.events',
        'asyncio.tasks',
        'asyncio.futures',
        'asyncio.runners',
    ],
    excludes=[
        'curses',       # Linux-only
        'readline',     # Linux-only
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
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # icon='assets/icon.ico',  # uncomment and add icon file when available
)
