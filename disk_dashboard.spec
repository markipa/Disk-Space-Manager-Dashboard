# PyInstaller spec: freezes the whole pywebview desktop app into
# dist_backend/DiskSpaceDashboard/.
# Build:  python -m PyInstaller --noconfirm --clean --distpath dist_backend disk_dashboard.spec
# (or:    npm-free — just run that command directly)

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
for pkg in (
    "dash", "plotly", "dash_table", "dash_core_components",
    "dash_html_components", "send2trash", "flask", "werkzeug",
    "narwhals", "webview", "clr_loader",
):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# Pillow (icon rendering) — collect its data/binaries too.
for pkg in ("PIL",):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

# tkinter is only imported inside the --pick-folder branch; pythonnet/clr power
# the Windows WebView2 backend of pywebview; win32gui/win32ui extract the shell
# icons — name them explicitly.
hiddenimports += [
    "tkinter", "tkinter.filedialog",
    "pythonnet", "clr", "clr_loader", "webview.platforms.edgechromium",
    "win32gui", "win32ui", "win32con", "pywintypes",
    "PIL.Image",
    # local package split out of file_dashboard.py
    "dsd", "dsd.utils", "dsd.icons", "dsd.figures", "dsd.analysis",
]

a = Analysis(
    ["desktop.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Heavy libs pulled in transitively but never used. The charts are plain
    # Plotly graph_objects serialized to JSON — no scipy/pandas/numba needed.
    excludes=[
        "matplotlib", "scipy", "pandas", "numba", "llvmlite",
        "lxml", "IPython", "PyQt5", "PySide2", "PySide6",
        "notebook", "pyarrow", "sqlalchemy", "tables", "sympy",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="DiskSpaceDashboard",
    console=False,             # windowed app: no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    icon="build/icon.ico",
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False,
    name="DiskSpaceDashboard",
)
