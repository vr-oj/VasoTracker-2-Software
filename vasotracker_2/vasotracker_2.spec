# -*- mode: python ; coding: utf-8 -*-

# -*- mode: python ; coding: utf-8 -*-
import sys
sys.setrecursionlimit(sys.getrecursionlimit() * 5)

import os
import sys

# Ensure the current directory of the spec file is the working directory
spec_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
sys.path.insert(0, spec_dir)


import version
from version import __version__

added_files = [("music", "music"), ("images", "images"), ("SampleData2", "SampleData2"), ('settings.toml', '.'), ('MMConfig.cfg', '.'), ('Basler.cfg', '.'), ('VasoTrackerblue.json', '.')]

a = Analysis(
    ['vasotracker_2.py'],
    pathex=[spec_dir],
    binaries=[],
    datas=added_files,
    hiddenimports=['PyDAQmx', 'scipy', 'version'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[os.path.join(spec_dir, "pyinstaller_hooks", "filter_pkg_resources_warning.py")],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=f"vasotracker_{__version__}",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=True,
    icon = os.path.join(spec_dir, "images", "vt_icon.ico")
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=f"vasotracker_{__version__}",
)
