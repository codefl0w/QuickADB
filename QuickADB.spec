# quickadb.spec

# -*- mode: python ; coding: utf-8 -*-
import sys
from PyInstaller.utils.hooks import collect_submodules, collect_data_files, collect_dynamic_libs

block_cipher = None

hiddenimports = (
    collect_submodules('main') +
    collect_submodules('modules') +
    collect_submodules('res') +
    collect_submodules('themes') +
    collect_submodules('util') +
    collect_submodules('PIL')
)

datas = [
    ('platform-tools/*', 'platform-tools'),
    ('res/*', 'res'),
    ('themes/*', 'themes'),
    ('util/*', 'util'),
]
datas += collect_data_files('PIL')

binaries = []
binaries += collect_dynamic_libs('PIL')

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# Linux compatibility: Exclude libraries that often cause "symbol lookup error" on newer distros (like Arch)
# when the app is built on an older distro.
# The CI workflow builds on Ubuntu 22.04. A bit old, but usually helps with compatibility.
if sys.platform == 'linux':
    excluded_binaries = ['libreadline.so.8', 'libcrypt.so.1', 'libz.so.1', 'libgcc_s.so.1']
    a.binaries = [x for x in a.binaries if x[0] not in excluded_binaries]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='QuickADB',
    console=False,
    disable_windowed_traceback=False,
    hide_console='hide-early',
    debug=False,
    strip=False,
    upx=True,
    icon='res/toolicon.ico',
)
