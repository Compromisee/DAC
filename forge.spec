# forge.spec
block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=[('dashboard.html', '.')],   # bundle dashboard.html alongside
    hiddenimports=[
        'flask', 'flask_cors', 'urllib.request', 'urllib.parse',
        'difflib', 'concurrent.futures', 'PIL', 'PIL.Image',
        'PIL.ImageDraw', 'PIL.ImageTk',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, a.binaries, a.zipfiles, a.datas,
    name='MKVTrackForge',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,        # True = show console window, False = GUI only
    icon=None,            # path to a .ico file if you have one
)