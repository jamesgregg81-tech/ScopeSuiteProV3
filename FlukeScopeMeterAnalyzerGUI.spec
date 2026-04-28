# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files

datas = []
datas += collect_data_files('matplotlib')


a = Analysis(
    ['PythonProjects/FlukeScopeSuite/FlukeScopeSuite_Pro_v3.py'],
    pathex=['PythonProjects/FlukeScopeSuite'],
    binaries=[],
    datas=datas,
    hiddenimports=[
        'serial',
        'serial.tools.list_ports',
        'numpy',
        'matplotlib',
        'matplotlib.backends.backend_agg',
        'PIL',
        'PIL.Image',
        'PIL.ImageTk',
        'scopesuite_v3',
        'scopesuite_v3.app',
        'scopesuite_v3.serial_client',
        'scopesuite_v3.waveform_protocol',
        'scopesuite_v3.professional_report',
        'scopesuite_v3.generator_report',
    ],
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
    name='FlukeScopeMeterAnalyzerGUI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
