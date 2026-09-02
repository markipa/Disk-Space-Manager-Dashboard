# Disk Space Dashboard

Desktop app to see which folders and files eat your disk space.

- **Drives overview** — free-space gauge per drive; click one to scan it.
- **Charts** — interactive treemap, sunburst, bar, file-types, and file-age,
  with a selectable colour palette and light/dark themes.
- **Table** — recursive search, size/type filters, sortable columns, real
  Windows file icons, and a details panel.
- **Actions** — open in Explorer, multi-select delete to the Recycle Bin.
- **Duplicate finder** — staged size → quick-hash → full-hash, with a
  potential-savings summary.
- **Privacy mode** — hide file/folder names for screenshots.

Built with **Python + Dash + Plotly**, shown in a native window via
**pywebview** — which uses the OS's built-in web engine (Edge WebView2 on
Windows), so there's no Electron and no bundled Chromium.

## Tests

```bash
pip install pytest
python -m pytest -q
```

## Run

Needs **Python 3.10+**. On Windows the Edge **WebView2 runtime** is used for the
window (preinstalled on Windows 11; on older Windows install it once from
Microsoft's "Evergreen WebView2 Runtime" if the window is blank).

```bash
pip install -r requirements.txt
python desktop.py
```

Prefer a browser instead of a window? Run the backend directly and open the URL:

```bash
python file_dashboard.py        # then open http://127.0.0.1:8050
```

## Build a standalone executable

No Python needed on the target machine — everything is frozen with PyInstaller.

```bash
pip install pyinstaller
python -m PyInstaller --noconfirm --clean --distpath dist disk_dashboard.spec
```

Output: `dist/DiskSpaceDashboard/DiskSpaceDashboard.exe` (a self-contained
folder). Zip that folder to distribute; the recipient unzips and runs the exe.

### Notes
- The native folder picker re-invokes the app with `--pick-folder`, so it works
  both as a script and as the frozen executable.
- `console=False` in the spec makes it a windowed app (no console window).
- App icon is `build/icon.ico` (embedded in the exe).
- Unused heavy libs (scipy, pandas, numba, …) are excluded in the spec to keep
  the build lean.

## Files
| File | Purpose |
|------|---------|
| `file_dashboard.py` | The Dash app: scanner, figures, callbacks. |
| `desktop.py` | pywebview launcher (runs Dash in a thread, opens the window). |
| `disk_dashboard.spec` | PyInstaller build config. |
| `build/icon.ico` | App icon. |
