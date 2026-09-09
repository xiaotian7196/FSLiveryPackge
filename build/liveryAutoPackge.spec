# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['../server.py'],
    pathex=[],
    binaries=[('C:/Users/xiaotian/miniconda3/Library/bin/tcl90.dll', '.'), ('C:/Users/xiaotian/miniconda3/Library/bin/tcl9tk90.dll', '.'), ('C:/Users/xiaotian/miniconda3/Library/bin/libtommath.dll', '.'), ('C:/Users/xiaotian/miniconda3/Library/bin/libbz2.dll', '.'), ('C:/Users/xiaotian/miniconda3/Library/bin/zlib1.dll', '.'), ('C:/Users/xiaotian/miniconda3/Library/bin/libssl-3-x64.dll', '.'), ('C:/Users/xiaotian/miniconda3/Library/bin/libcrypto-3-x64.dll', '.'), ('C:/Users/xiaotian/miniconda3/Library/bin/ffi.dll', '.'), ('C:/Users/xiaotian/miniconda3/Library/bin/libexpat.dll', '.'), ('C:/Users/xiaotian/miniconda3/Library/bin/liblzma.dll', '.'), ('C:/Users/xiaotian/miniconda3/Library/bin/libmpdec-4.dll', '.'), ('C:/Users/xiaotian/miniconda3/Library/bin/zstd.dll', '.')],
    datas=[('e:/Project/liveryAutoPackge/web', 'web'), ('e:/Project/liveryAutoPackge/profiles', 'profiles'), ('C:/Users/xiaotian/miniconda3/Library/lib/tcl9.0', 'tcl9.0'), ('C:/Users/xiaotian/miniconda3/Library/lib/tk9.0', 'tk9.0')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='liveryAutoPackge',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
