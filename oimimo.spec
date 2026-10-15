# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.building.datastruct import Tree
from PyInstaller.utils.hooks import collect_all

playwright_datas, playwright_binaries, playwright_hiddenimports = collect_all('playwright')
datas = [
    ('templates', 'templates'),
    ('static', 'static'),
    ('requirements.txt', '.'),
] + playwright_datas
browser_dir = os.path.join(SPECPATH, '.playwright-browsers')
if os.path.isdir(browser_dir):
    datas += Tree(browser_dir, prefix='.playwright-browsers')


a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=playwright_binaries,
    datas=datas,
    hiddenimports=[
        'app', 'db', 'models', 'image_processor',
        'integrations', 'integrations.mihuashi',
        'integrations.mihuashi.browser', 'integrations.mihuashi.collector',
        'integrations.mihuashi.mapper', 'integrations.mihuashi.profile_lock',
        'integrations.mihuashi.schemas', 'integrations.mihuashi.status_store',
        'integrations.mihuashi.sync_manager', 'integrations.mihuashi.sync_service',
        'integrations.mihuashi.web_security',
    ] + playwright_hiddenimports,
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
    [],
    exclude_binaries=True,
    name='oimimo',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['static\\logo.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='oimimo',
)
