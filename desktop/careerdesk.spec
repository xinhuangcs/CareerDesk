# -*- mode: python ; coding: utf-8 -*-
"""Cross-platform, one-folder CareerDesk desktop bundle.

The package source and immutable resources come from an extracted CareerDesk
wheel prepared by ``package_desktop.py``.  Runtime dependencies come from the
locked build environment.  One-folder mode is deliberate: it avoids per-launch
temporary extraction and is easier to inspect, sign, and diagnose than onefile.
"""

import os
from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata


ROOT = Path(SPECPATH).parent
SITE = Path(os.environ["CAREERDESK_FROZEN_SITE"]).resolve()
PACKAGE = SITE / "careerdesk"
VERSION = os.environ["CAREERDESK_BUILD_VERSION"]
WINDOWS_VERSION_FILE = os.environ.get("CAREERDESK_WINDOWS_VERSION_FILE")
LEGAL = Path(os.environ["CAREERDESK_LEGAL_DIR"]).resolve()

if not (PACKAGE / "default.env").is_file():
    raise RuntimeError("staged wheel is missing careerdesk/default.env")
if not (PACKAGE / "frontend_dist" / "index.html").is_file():
    raise RuntimeError("staged wheel is missing the built frontend")
if not (PACKAGE / "frontend_dist" / "legal" / "node" / "index.json").is_file():
    raise RuntimeError("staged wheel is missing npm third-party notices")
if not (LEGAL / "CareerDesk" / "LICENSE").is_file():
    raise RuntimeError("legal staging is missing the CareerDesk license")
if not (LEGAL / "ThirdParty" / "Python" / "index.json").is_file():
    raise RuntimeError("legal staging is missing Python third-party notices")

datas = [
    (str(PACKAGE / "default.env"), "careerdesk"),
    (str(PACKAGE / "frontend_dist"), "careerdesk/frontend_dist"),
    (str(PACKAGE / "agentic" / "skills"), "careerdesk/agentic/skills"),
    (str(LEGAL), "Legal"),
]
datas += collect_data_files("magika")
datas += collect_data_files("sqlite_vec")
for distribution in ("careerdesk", "agentmaker", "keyring", "markitdown"):
    datas += copy_metadata(distribution)

hidden_imports = sorted(set(
    collect_submodules("careerdesk")
    + collect_submodules("agentmaker")
    + [
        "keyring.backends.chainer",
        "keyring.backends.fail",
        "keyring.backends.null",
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
    ]
))
if sys.platform == "darwin":
    hidden_imports += ["keyring.backends.macOS", "webview.platforms.cocoa"]
elif sys.platform == "win32":
    hidden_imports += [
        "keyring.backends.Windows",
        "webview.platforms.edgechromium",
        "webview.platforms.mshtml",
        "webview.platforms.winforms",
    ]
else:
    raise RuntimeError("formal desktop bundles currently support only macOS and Windows")

a = Analysis(
    [str(ROOT / "desktop" / "frozen_entry.py")],
    pathex=[str(SITE)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

icon = ROOT / "desktop" / "CareerDesk.icns" if sys.platform == "darwin" else ROOT / "careerdesk.ico"
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CareerDesk",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon),
    version=WINDOWS_VERSION_FILE if sys.platform == "win32" else None,
)
data_exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CareerDeskData",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon),
    version=WINDOWS_VERSION_FILE if sys.platform == "win32" else None,
)
bundle = COLLECT(
    exe,
    data_exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="CareerDesk",
)

if sys.platform == "darwin":
    app = BUNDLE(
        bundle,
        name="CareerDesk.app",
        icon=str(icon),
        bundle_identifier="com.careerdesk.desktop",
        version=VERSION,
        codesign_identity=None,
        info_plist={
            "CFBundleDisplayName": "CareerDesk",
            "CFBundleName": "CareerDesk",
            "CFBundleShortVersionString": VERSION,
            "NSHighResolutionCapable": True,
            "NSPrincipalClass": "NSApplication",
        },
    )
