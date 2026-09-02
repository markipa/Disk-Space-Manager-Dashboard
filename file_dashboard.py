"""
Disk Space Dashboard  (Plotly / Dash edition)
---------------------------------------------
A local web dashboard to see which folders and files are eating your memory.

Pick a target folder, scan it, and explore:
  - Summary cards (total size, files, folders, biggest item)
  - A HIERARCHICAL Plotly treemap: click a tile to zoom into that folder,
    click the header to zoom back out. Colour-scale: small = deep blue,
    biggest file/folder = red.
  - A SUNBURST: radial rings by depth, click a wedge to zoom (same data/colour)
  - A bar chart of the biggest items (same colour scale)
  - A "File Types" chart: which extensions (.mp4, .zip, .psd…) eat the space
  - A sortable table; click a folder name to drill in, "Up" to go back.
    Pick a row's radio button, then "Open in Explorer" or "Delete to Recycle
    Bin" (reversible — files go to the Recycle Bin, with a confirm prompt).

Colour palette (small -> big):
  #173F5F  ->  #20639B  ->  #3CAEA3  ->  #F6D55C  ->  #ED553B (red = biggest)

Run:
  python file_dashboard.py
then open the URL it prints (default http://127.0.0.1:8050) in your browser.
A native "Browse" dialog is used to pick the folder.

Requires: dash, plotly  (see requirements.txt)
"""

import os
import sys
import threading
import subprocess
from dataclasses import dataclass, field

if sys.platform == "win32":
    import ctypes

# Folder-picker child process: handled before the (heavier) Dash imports so the
# dialog pops instantly. See pick_folder_dialog().
if "--pick-folder" in sys.argv:
    import tkinter as tk
    from tkinter import filedialog
    _r = tk.Tk()
    _r.withdraw()
    _r.attributes("-topmost", True)
    _p = filedialog.askdirectory(title="Select a folder to analyze")
    _r.destroy()
    print(_p or "")
    sys.exit(0)

import shutil
import string

import plotly.graph_objects as go
from send2trash import send2trash
from dash import (Dash, dcc, html, dash_table, Input, Output, State, ctx,
                  no_update, ALL)
from dash.dash_table.Format import Format, Scheme

# Pure logic lives in the dsd/ package; the stateful scanner/app/callbacks stay
# here. Re-exported names keep the tests and any external imports working.
from dsd.utils import (                                            # noqa: F401
    PALETTE, COLOR_SCALE, PALETTES, DEFAULT_PALETTE, palette_scale,
    BG, PANEL, FG, MUTED, THEMES, theme, CHART_H, TABLE_H,
    human_size, _trim, mask_name, fmt_date, fmt_iso, usage_color)
from dsd.icons import icon_png_for_key, icon_url                   # noqa: F401
from dsd.figures import (                                          # noqa: F401
    base_layout, empty_fig, build_hierarchy, treemap_fig, sunburst_fig,
    bar_fig, filetype_fig, age_fig, AGE_BUCKETS)
from dsd.analysis import find_duplicates, _file_hash              # noqa: F401
from dsd import mft

# Back-compat alias (tests / older references used the leading-underscore name).
_usage_color = usage_color


@dataclass
class Item:
    """One file or folder plus its total size in bytes and modified time."""
    name: str
    path: str
    size: int
    is_dir: bool
    children: list = field(default_factory=list)
    mtime: float = 0.0


# ----------------------------- scanner ------------------------------------- #

# Populated on each scan: maps absolute path -> Item, so callbacks can look up
# any node by its path (used for drill-in navigation) without re-scanning.
INDEX: dict[str, Item] = {}
ROOT_PATH: str | None = None

# Shared state for the background scan (progress polling + cancellation).
SCAN = {
    "running": False, "cancel": False, "done": False, "error": None,
    "count": 0, "bytes": 0, "skipped": 0, "current": "", "root": None,
}


class _Cancelled(Exception):
    """Raised inside the scan walk to abort a cancelled scan."""


def _is_drive_root(path: str) -> bool:
    """True for 'C:\\' style drive roots (where the MFT fast path applies)."""
    return (sys.platform == "win32" and len(path) == 3
            and path[1] == ":" and path[2] in "\\/")


def _item_factory(name, path, size, is_dir, mtime, children):
    return Item(name, path, size, is_dir, children, mtime)


def _iter_all_files(node):
    """Yield every file (non-dir) under node — cheap in-memory walk."""
    stack = [node]
    while stack:
        n = stack.pop()
        for c in n.children:
            if c.is_dir:
                stack.append(c)
            else:
                yield c


def scan_directory(root_path: str, state: dict | None = None) -> Item:
    """
    Build an Item tree with cumulative sizes and modified times, and (re)build
    the global INDEX path -> Item.

    Fast path: for a whole-drive scan on Windows *as Administrator*, read the
    NTFS $MFT directly (dsd.mft) — ~100x faster than walking. Any failure falls
    back to the os.scandir walk below. Symlinks are skipped; permission errors
    are counted in state["skipped"]; state["cancel"] aborts the walk.
    """
    global INDEX, ROOT_PATH
    abspath = os.path.abspath(root_path)

    if _is_drive_root(abspath) and mft.is_admin():
        try:
            INDEX = {}
            ROOT_PATH = abspath
            if state is not None:
                state["current"] = "Reading MFT (fast scan)…"
            root = mft.build_tree(abspath[0], _item_factory, INDEX)
            if state is not None:
                state["count"] = sum(1 for _ in _iter_all_files(root))
                state["bytes"] = root.size
            return root
        except Exception:
            INDEX = {}          # discard any partial index, fall back below

    INDEX = {}
    ROOT_PATH = abspath

    def walk(path: str, name: str) -> Item:
        if state and state["cancel"]:
            raise _Cancelled()
        total = 0
        children = []
        try:
            with os.scandir(path) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            child = walk(entry.path, entry.name)
                            children.append(child)
                            total += child.size
                        elif entry.is_file(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            children.append(Item(entry.name, entry.path,
                                                 st.st_size, False,
                                                 mtime=st.st_mtime))
                            total += st.st_size
                            if state is not None:
                                state["count"] += 1
                                state["bytes"] += st.st_size
                                if state["count"] % 400 == 0:
                                    state["current"] = path
                    except (PermissionError, OSError):
                        if state is not None:
                            state["skipped"] += 1
                        continue
        except (PermissionError, OSError):
            if state is not None:
                state["skipped"] += 1
        try:
            dmt = os.stat(path).st_mtime
        except OSError:
            dmt = 0.0
        node = Item(name, path, total, True, children, mtime=dmt)
        INDEX[os.path.abspath(path)] = node
        return node

    disp = os.path.basename(ROOT_PATH.rstrip("\\/")) or ROOT_PATH
    return walk(ROOT_PATH, disp)


def start_scan(path: str) -> None:
    """Kick off scan_directory in a daemon thread, updating SCAN as it goes."""
    SCAN.update(running=True, cancel=False, done=False, error=None,
                count=0, bytes=0, skipped=0, current=path, root=None)

    def worker():
        try:
            tree = scan_directory(path, SCAN)
            SCAN["root"] = tree
            SCAN["done"] = True
        except _Cancelled:
            SCAN["error"] = "cancelled"
        except Exception as e:               # noqa: BLE001 - surfaced in the UI
            SCAN["error"] = str(e)
        finally:
            SCAN["running"] = False

    threading.Thread(target=worker, daemon=True).start()


# ----------------------------- drives -------------------------------------- #

def list_drives() -> list[tuple[str, int, int, int]]:
    """Return [(mount, total, used, free)] for every ready drive."""
    out = []
    if sys.platform == "win32":
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for i, letter in enumerate(string.ascii_uppercase):
            if not (bitmask & (1 << i)):
                continue
            root = f"{letter}:\\"
            try:
                u = shutil.disk_usage(root)
                out.append((root, u.total, u.used, u.free))
            except OSError:
                continue
    else:
        for root in ("/",):
            try:
                u = shutil.disk_usage(root)
                out.append((root, u.total, u.used, u.free))
            except OSError:
                continue
    return out


def drives_cards(th):
    """Clickable free-space cards, one per drive (click a card to scan it)."""
    cards = []
    for root, total, used, free in list_drives():
        frac = used / total if total else 0
        col = _usage_color(frac)
        cards.append(html.Div(
            id={"type": "drive-card", "path": root},
            n_clicks=0,
            style=dict(backgroundColor=th["panel"], borderRadius="12px",
                       padding="18px 20px", width="260px", cursor="pointer",
                       border=f"1px solid {th['grid']}"),
            children=[
                html.Div([
                    html.Span("🖴  ", style=dict(fontSize="20px")),
                    html.Span(root, style=dict(fontSize="20px",
                                               fontWeight="700")),
                ]),
                html.Div(f"{human_size(free)} free",
                         style=dict(fontSize="15px", fontWeight="700",
                                    marginTop="8px", color=col)),
                html.Div(f"of {human_size(total)}",
                         style=dict(fontSize="12px", color=th["muted"])),
                # usage bar
                html.Div(
                    style=dict(height="10px", borderRadius="5px",
                               backgroundColor=th["grid"], marginTop="12px",
                               overflow="hidden"),
                    children=html.Div(style=dict(
                        width=f"{frac * 100:.1f}%", height="100%",
                        backgroundColor=col)),
                ),
                html.Div(f"{frac * 100:.0f}% used  ·  "
                         f"{human_size(used)} used",
                         style=dict(fontSize="12px", color=th["muted"],
                                    marginTop="6px")),
            ],
        ))
    return cards


def pick_folder_dialog() -> str | None:
    """
    Open the native folder-picker by re-invoking this program with --pick-folder
    in a child process. Works both as a plain script (python file_dashboard.py)
    and as a frozen PyInstaller/Electron executable (sys.frozen), where `python
    -c ...` would not be available. Returns the chosen path or None.
    """
    if getattr(sys, "frozen", False):
        cmd = [sys.executable, "--pick-folder"]
    else:
        cmd = [sys.executable, os.path.abspath(__file__), "--pick-folder"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        path = out.stdout.strip()
        return path or None
    except Exception:
        return None


# ----------------------------- mutations ----------------------------------- #

def remove_item(path: str) -> bool:
    """
    Drop `path` from the in-memory tree after it has been sent to the Recycle
    Bin: unlink it from its parent, subtract its size from every ancestor, and
    purge it (and its descendants) from INDEX. Returns True on success.
    """
    abspath = os.path.abspath(path)
    if abspath == ROOT_PATH:
        return False
    parent = INDEX.get(os.path.dirname(abspath))
    if parent is None:
        return False
    # Files are not kept in INDEX (only dirs), so locate the node via its parent.
    node = next((c for c in parent.children
                 if os.path.abspath(c.path) == abspath), None)
    if node is None:
        return False

    parent.children = [c for c in parent.children
                       if os.path.abspath(c.path) != abspath]

    p = parent
    while p is not None:
        p.size -= node.size
        p = None if os.path.abspath(p.path) == ROOT_PATH \
            else INDEX.get(os.path.dirname(os.path.abspath(p.path)))

    if node.is_dir:  # purge the removed dir's subtree from INDEX
        stack = [node]
        while stack:
            x = stack.pop()
            INDEX.pop(os.path.abspath(x.path), None)
            for c in x.children:
                if c.is_dir:
                    stack.append(c)
    return True


def open_in_explorer(path: str) -> bool:
    """Reveal a file/folder in Windows Explorer (selected)."""
    try:
        if not os.path.exists(path):
            return False
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
        return True
    except Exception:
        return False


# ----------------------------- app / layout -------------------------------- #

app = Dash(__name__, title="Disk Space Dashboard")


@app.server.route("/icon/<key>")
def _serve_icon(key):
    """Serve a cached Windows type-icon PNG (used by the table's Name column)."""
    from flask import Response
    png = icon_png_for_key(key)
    if not png:
        return Response(status=204)
    return Response(png, mimetype="image/png",
                    headers={"Cache-Control": "max-age=86400"})


# Container chrome is themed with CSS variables so the light/dark toggle flips
# instantly (clientside), no server round-trip. The two themes redefine these
# vars; html.light is toggled by the theme button.
app.index_string = """<!DOCTYPE html>
<html>
  <head>
    {%metas%}<title>{%title%}</title>{%favicon%}{%css%}
    <style>
      :root{
        --bg:#1e1e1e; --panel:#242424; --fg:#e6e6e6; --muted:#9a9a9a;
        --head:#333333; --line:#3a3a3a; --accent:#3a7ebf;
      }
      html.light{
        --bg:#ffffff; --panel:#f4f4f5; --fg:#1e1e1e; --muted:#6b7280;
        --head:#e5e7eb; --line:#d8d8dc; --accent:#3a7ebf;
      }
      html, body { background: var(--bg); margin:0; }
    </style>
  </head>
  <body>{%app_entry%}<footer>{%config%}{%scripts%}{%renderer%}</footer></body>
</html>"""

CARD_STYLE = dict(
    backgroundColor="var(--panel)", borderRadius="10px", padding="12px 16px",
    flex="1", minWidth="0",
)
BTN_STYLE = dict(
    backgroundColor="var(--accent)", color="white", border="none",
    borderRadius="8px", padding="10px 16px", cursor="pointer",
    fontSize="14px", fontWeight="600",
)
# Cancel button visibility (toggled by the scan callbacks).
CANCEL_SHOWN = {**BTN_STYLE, "backgroundColor": "#ED553B", "padding": "8px 14px",
                "fontSize": "13px"}
CANCEL_HIDDEN = {**CANCEL_SHOWN, "display": "none"}
INPUT_STYLE = dict(
    backgroundColor="var(--panel)", color="var(--fg)",
    border="1px solid var(--line)", borderRadius="6px", padding="6px 10px",
    fontSize="13px",
)


def card(card_id, title):
    return html.Div(
        [
            html.Div(title, style=dict(color="var(--muted)", fontSize="12px")),
            html.Div("—", id=card_id,
                     style=dict(fontSize="22px", fontWeight="700",
                                marginTop="4px", whiteSpace="pre-line")),
        ],
        style=CARD_STYLE,
    )


def _tab(selected=False):
    base = dict(backgroundColor="var(--panel)", color="var(--fg)",
                border="none", padding="8px 14px")
    if selected:
        base.update(borderBottom="2px solid var(--accent)", fontWeight="700")
    return base


def dupes_tab():
    """Duplicates tab: controls + a results table (populated on demand)."""
    return html.Div(
        style=dict(height=CHART_H, display="flex", flexDirection="column",
                   padding="4px"),
        children=[
            html.Div(
                style=dict(display="flex", gap="8px", alignItems="center",
                           marginBottom="8px", flexWrap="wrap"),
                children=[
                    html.Button("🔎  Find duplicates", id="dupe-btn",
                                n_clicks=0,
                                style={**BTN_STYLE, "padding": "8px 14px",
                                       "fontSize": "13px"}),
                    dcc.Dropdown(
                        id="dupe-min", clearable=False, value=1_048_576,
                        options=[
                            {"label": "≥ 100 KB", "value": 102_400},
                            {"label": "≥ 1 MB", "value": 1_048_576},
                            {"label": "≥ 10 MB", "value": 10_485_760},
                            {"label": "≥ 100 MB", "value": 104_857_600},
                        ],
                        style=dict(width="130px", fontSize="13px"),
                    ),
                    html.Div(id="dupe-summary",
                             style=dict(color="var(--muted)", fontSize="13px")),
                ],
            ),
            dcc.Loading(
                type="circle", color="#3a7ebf",
                children=dash_table.DataTable(
                    id="dupe-table",
                    columns=[
                        dict(name="File", id="name"),
                        dict(name="Size each", id="size"),
                        dict(name="Copies", id="copies"),
                        dict(name="Wasted", id="wasted"),
                        dict(name="Locations", id="locations"),
                    ],
                    data=[],
                    page_size=100,
                    style_table=dict(flex="1", overflowY="auto",
                                     height="calc(100vh - 340px)"),
                    style_header=dict(backgroundColor="var(--head)",
                                      color="var(--fg)", fontWeight="700",
                                      border="none"),
                    style_cell=dict(backgroundColor="var(--panel)",
                                    color="var(--fg)", border="none",
                                    padding="6px 10px", fontSize="13px",
                                    textAlign="left", maxWidth=0,
                                    overflow="hidden", textOverflow="ellipsis"),
                    style_cell_conditional=[
                        dict(if_=dict(column_id="size"), textAlign="right",
                             width="14%"),
                        dict(if_=dict(column_id="copies"), textAlign="center",
                             width="9%"),
                        dict(if_=dict(column_id="wasted"), textAlign="right",
                             width="12%"),
                    ],
                ),
            ),
        ],
    )


app.layout = html.Div(
    style=dict(backgroundColor="var(--bg)", color="var(--fg)", height="100vh",
               boxSizing="border-box", overflow="hidden", display="flex",
               flexDirection="column",
               fontFamily="Segoe UI, system-ui, sans-serif", padding="14px"),
    children=[
        dcc.Store(id="current-path"),
        dcc.Store(id="refresh", data=0),
        dcc.Store(id="pending-delete"),
        dcc.Store(id="theme", data="dark"),
        dcc.Store(id="privacy", data=False),
        dcc.ConfirmDialog(id="confirm-del"),
        dcc.Interval(id="scan-poll", interval=300, disabled=True),
        html.Div(
            style=dict(display="flex", alignItems="center", gap="10px",
                       marginBottom="12px"),
            children=[
                html.Button("📁  Select Folder", id="browse", n_clicks=0,
                            style=BTN_STYLE),
                html.Button("🖴  Drives", id="drives-btn", n_clicks=0,
                            style={**BTN_STYLE, "backgroundColor": "#666"}),
                html.Button("⬆  Up", id="up", n_clicks=0,
                            style={**BTN_STYLE, "backgroundColor": "#666"}),
                html.Div("No folder selected", id="path-label",
                         style=dict(color="var(--muted)", marginLeft="6px",
                                    overflow="hidden", textOverflow="ellipsis",
                                    whiteSpace="nowrap", flex="1")),
                html.Div(id="scan-status",
                         style=dict(color="var(--muted)", fontSize="12px",
                                    whiteSpace="nowrap", overflow="hidden",
                                    textOverflow="ellipsis", maxWidth="420px")),
                html.Button("✖  Cancel", id="cancel-btn", n_clicks=0,
                            style=CANCEL_HIDDEN),
                dcc.Dropdown(
                    id="palette", clearable=False, value=DEFAULT_PALETTE,
                    options=[{"label": f"🎨 {k}", "value": k} for k in PALETTES],
                    style=dict(width="170px", fontSize="13px"),
                ),
                html.Button("🕶  Hide names", id="privacy-btn", n_clicks=0,
                            style={**BTN_STYLE, "backgroundColor": "#666"}),
                html.Button("◐  Light / Dark", id="theme-btn", n_clicks=0,
                            style={**BTN_STYLE, "backgroundColor": "#666"}),
            ],
        ),
        html.Div(
            style=dict(display="flex", gap="10px", marginBottom="12px"),
            children=[
                card("card-total", "Total Size"),
                card("card-files", "Files"),
                card("card-dirs", "Folders"),
                card("card-biggest", "Biggest Item"),
            ],
        ),
        html.Div(
            style=dict(display="flex", gap="12px", alignItems="stretch",
                       flex="1", minHeight="0"),
            children=[
                html.Div(
                    style=dict(flex="1.15", backgroundColor="var(--panel)",
                               borderRadius="10px", padding="8px",
                               minHeight="0", display="flex",
                               flexDirection="column"),
                    children=dcc.Tabs(
                        id="tabs", value="treemap",
                        style=dict(height="34px"),
                        content_style=dict(flex="1", minHeight="0"),
                        parent_style=dict(flex="1", display="flex",
                                          flexDirection="column", minHeight="0"),
                        children=[
                            dcc.Tab(label="Treemap", value="treemap",
                                    style=_tab(), selected_style=_tab(True),
                                    children=dcc.Graph(
                                        id="treemap", figure=empty_fig(),
                                        responsive=True,
                                        style=dict(height="100%"))),
                            dcc.Tab(label="Sunburst", value="sun",
                                    style=_tab(), selected_style=_tab(True),
                                    children=dcc.Graph(
                                        id="sunburst", figure=empty_fig(),
                                        responsive=True,
                                        style=dict(height="100%"))),
                            dcc.Tab(label="Bar Chart", value="bar",
                                    style=_tab(), selected_style=_tab(True),
                                    children=dcc.Graph(
                                        id="bar", figure=empty_fig(),
                                        responsive=True,
                                        style=dict(height="100%"))),
                            dcc.Tab(label="File Types", value="ftype",
                                    style=_tab(), selected_style=_tab(True),
                                    children=dcc.Graph(
                                        id="filetype", figure=empty_fig(),
                                        responsive=True,
                                        style=dict(height="100%"))),
                            dcc.Tab(label="File Age", value="age",
                                    style=_tab(), selected_style=_tab(True),
                                    children=dcc.Graph(
                                        id="agechart", figure=empty_fig(),
                                        responsive=True,
                                        style=dict(height="100%"))),
                            dcc.Tab(label="Duplicates", value="dupes",
                                    style=_tab(), selected_style=_tab(True),
                                    children=dupes_tab()),
                        ],
                    ),
                ),
                html.Div(
                    style=dict(flex="1", backgroundColor="var(--panel)",
                               borderRadius="10px", padding="8px",
                               display="flex", flexDirection="column",
                               minHeight="0"),
                    children=[
                        html.Div(
                            "Largest items — click a folder name to open; tick "
                            "rows to target the buttons; click headers to sort. "
                            "Type in search to look through all subfolders.",
                            style=dict(fontWeight="700", padding="6px 4px",
                                       fontSize="13px")),
                        # search + filters
                        html.Div(
                            style=dict(display="flex", gap="8px",
                                       margin="2px 4px 8px", alignItems="center"),
                            children=[
                                dcc.Input(id="search", type="text", debounce=True,
                                          placeholder="🔍  Search all subfolders "
                                          "(name or .ext)…",
                                          style={**INPUT_STYLE, "flex": "1"}),
                                dcc.Dropdown(
                                    id="minsize", clearable=False, value=0,
                                    options=[
                                        {"label": "Any size", "value": 0},
                                        {"label": "≥ 1 MB", "value": 1_048_576},
                                        {"label": "≥ 10 MB", "value": 10_485_760},
                                        {"label": "≥ 100 MB", "value": 104_857_600},
                                        {"label": "≥ 1 GB", "value": 1_073_741_824},
                                    ],
                                    style=dict(width="130px", fontSize="13px"),
                                ),
                                dcc.RadioItems(
                                    id="kind", value="all",
                                    options=[{"label": " All", "value": "all"},
                                             {"label": " Files", "value": "files"},
                                             {"label": " Folders",
                                              "value": "folders"}],
                                    inline=True,
                                    style=dict(fontSize="13px"),
                                    inputStyle=dict(marginRight="3px",
                                                    marginLeft="8px"),
                                ),
                            ],
                        ),
                        # actions
                        html.Div(
                            style=dict(display="flex", gap="8px",
                                       margin="0 4px 8px"),
                            children=[
                                html.Button("📂  Open in Explorer", id="open-btn",
                                            n_clicks=0,
                                            style={**BTN_STYLE, "padding":
                                                   "8px 12px", "fontSize": "13px",
                                                   "backgroundColor": "#444"}),
                                html.Button("🗑  Delete to Recycle Bin",
                                            id="del-btn", n_clicks=0,
                                            style={**BTN_STYLE, "padding":
                                                   "8px 12px", "fontSize": "13px",
                                                   "backgroundColor": "#ED553B"}),
                            ],
                        ),
                        # table (grows to fill and scrolls; details sits below)
                        html.Div(
                            style=dict(flex="1", minHeight="0", overflowY="auto"),
                            children=dash_table.DataTable(
                                id="table",
                                columns=[
                                    dict(name="Name", id="name",
                                         presentation="markdown"),
                                    dict(name="Size", id="size"),
                                    dict(name="%", id="pct", type="numeric",
                                         format=Format(precision=1,
                                                       scheme=Scheme.percentage)),
                                    dict(name="Modified", id="modified"),
                                    dict(name="Type", id="type"),
                                ],
                                markdown_options={"html": False},
                                data=[],
                                page_size=200,
                                row_selectable="multi",
                                selected_rows=[],
                                sort_action="native",
                                style_table=dict(overflowY="auto"),
                                style_cell_conditional=[
                                    dict(if_=dict(column_id="name"), width="34%"),
                                    dict(if_=dict(column_id="size"), width="15%"),
                                    dict(if_=dict(column_id="pct"), width="11%"),
                                    dict(if_=dict(column_id="modified"),
                                         width="22%"),
                                    dict(if_=dict(column_id="type"), width="18%"),
                                ],
                                style_header=dict(backgroundColor="var(--head)",
                                                  color="var(--fg)",
                                                  fontWeight="700",
                                                  border="none",
                                                  textAlign="center"),
                                style_cell=dict(backgroundColor="var(--panel)",
                                                color="var(--fg)",
                                                border="none", padding="6px 10px",
                                                fontSize="13px",
                                                textAlign="center",
                                                maxWidth=0, overflow="hidden",
                                                textOverflow="ellipsis"),
                                style_data_conditional=[
                                    dict(if_=dict(
                                        filter_query='{type} = "Folder"',
                                        column_id="name"),
                                        color="#6cb6ff", cursor="pointer"),
                                ],
                                cell_selectable=True,
                            ),
                        ),
                        # selected-item details
                        html.Div(
                            id="details",
                            style=dict(borderTop="1px solid var(--line)",
                                       marginTop="6px", paddingTop="8px",
                                       minHeight="118px"),
                            children=html.Div(
                                "Select a row to see details.",
                                style=dict(color="var(--muted)",
                                           fontSize="13px", padding="10px 4px")),
                        ),
                    ],
                ),
            ],
        ),
        html.Div("Ready.", id="footer",
                 style=dict(color="var(--muted)", marginTop="10px",
                            fontSize="12px")),
        # Drives overview overlay (shown at start / via the Drives button)
        html.Div(
            id="drives-view",
            style=dict(position="fixed", top="72px", left="0", right="0",
                       bottom="0", backgroundColor="var(--bg)", zIndex="40",
                       padding="24px 40px", overflowY="auto", display="flex",
                       flexDirection="column", gap="14px"),
            children=[
                html.Div([
                    html.Span("Drives", style=dict(fontSize="20px",
                                                   fontWeight="700")),
                    html.Span("  — click a drive to scan it, or use "
                              "“Select Folder” for a specific folder.",
                              style=dict(color="var(--muted)",
                                         fontSize="14px")),
                    html.Button("✕  Close", id="drives-close", n_clicks=0,
                                style={**BTN_STYLE, "backgroundColor": "#666",
                                       "padding": "6px 12px", "fontSize": "13px",
                                       "float": "right"}),
                ]),
                html.Div(id="drives-cards",
                         style=dict(display="flex", flexWrap="wrap",
                                    gap="14px", marginTop="8px"),
                         children=drives_cards(THEMES["dark"])),
                html.Div(
                    ("⚡ Running as Administrator — whole-drive scans use the "
                     "NTFS MFT (near-instant)." if mft.is_admin() else
                     "💡 Tip: run this app as Administrator for near-instant "
                     "whole-drive scans (reads the NTFS MFT directly). Without "
                     "admin it falls back to a normal folder walk."),
                    style=dict(color="var(--muted)", fontSize="13px",
                               marginTop="14px")),
            ],
        ),
    ],
)


# Instant light/dark toggle: flip the html.light class (CSS vars do the rest)
# and remember the choice in the `theme` store so the figures rebuild to match.
app.clientside_callback(
    """
    function(n){
        var light = (n || 0) % 2 === 1;
        document.documentElement.classList.toggle('light', light);
        return light ? 'light' : 'dark';
    }
    """,
    Output("theme", "data"),
    Input("theme-btn", "n_clicks"),
    prevent_initial_call=True,
)


# Privacy mode: hide file/folder names across table, charts, cards, details.
@app.callback(
    Output("privacy", "data"),
    Output("privacy-btn", "children"),
    Input("privacy-btn", "n_clicks"),
    prevent_initial_call=True,
)
def toggle_privacy(n):
    on = (n or 0) % 2 == 1
    return on, ("🙈  Names hidden" if on else "🕶  Hide names")


# ----------------------------- callbacks ----------------------------------- #

# ---- Drives overview overlay (open / close / recolour on theme) ---- #
@app.callback(
    Output("drives-cards", "children"),
    Output("drives-view", "style"),
    Input("drives-btn", "n_clicks"),
    Input("drives-close", "n_clicks"),
    Input("theme", "data"),
    State("drives-view", "style"),
    prevent_initial_call=True,
)
def drives_overlay(_open, _close, theme_name, style):
    th = theme(theme_name)
    style = dict(style or {})
    trig = ctx.triggered_id
    if trig == "drives-btn":
        style["display"] = "flex"
    elif trig == "drives-close":
        style["display"] = "none"
    return drives_cards(th), style


# ---- Click a drive card -> scan that drive ---- #
@app.callback(
    Output("scan-poll", "disabled", allow_duplicate=True),
    Output("cancel-btn", "style", allow_duplicate=True),
    Output("scan-status", "children", allow_duplicate=True),
    Output("drives-view", "style", allow_duplicate=True),
    Input({"type": "drive-card", "path": ALL}, "n_clicks"),
    State("drives-view", "style"),
    prevent_initial_call=True,
)
def pick_drive(clicks, style):
    if not clicks or not any(clicks):
        return no_update, no_update, no_update, no_update
    tid = ctx.triggered_id
    path = tid["path"] if isinstance(tid, dict) else None
    if not path:
        return no_update, no_update, no_update, no_update
    start_scan(path)
    style = dict(style or {})
    style["display"] = "none"
    return False, CANCEL_SHOWN, "Starting scan…", style


# ---- Start a scan (native folder picker -> background thread + polling) ---- #
@app.callback(
    Output("scan-poll", "disabled"),
    Output("cancel-btn", "style"),
    Output("scan-status", "children"),
    Output("drives-view", "style", allow_duplicate=True),
    Input("browse", "n_clicks"),
    State("drives-view", "style"),
    prevent_initial_call=True,
)
def start_scan_cb(_n, style):
    chosen = pick_folder_dialog()
    if not chosen:
        return True, CANCEL_HIDDEN, "", no_update
    start_scan(chosen)
    style = dict(style or {})
    style["display"] = "none"
    return False, CANCEL_SHOWN, "Starting scan…", style


# ---- Poll scan progress; commit result when finished ---- #
@app.callback(
    Output("current-path", "data", allow_duplicate=True),
    Output("scan-poll", "disabled", allow_duplicate=True),
    Output("cancel-btn", "style", allow_duplicate=True),
    Output("scan-status", "children", allow_duplicate=True),
    Input("scan-poll", "n_intervals"),
    prevent_initial_call=True,
)
def poll_scan(_n):
    if SCAN["running"]:
        cur = SCAN["current"]
        short = cur if len(cur) < 60 else "…" + cur[-57:]
        txt = (f"Scanning… {SCAN['count']:,} files · "
               f"{human_size(SCAN['bytes'])} · {short}")
        return no_update, False, CANCEL_SHOWN, txt
    if SCAN["done"]:
        skipped = SCAN["skipped"]
        note = (f"  ⚠ {skipped:,} items skipped (permissions)"
                if skipped else "")
        return ROOT_PATH, True, CANCEL_HIDDEN, note or ""
    if SCAN["error"] == "cancelled":
        return no_update, True, CANCEL_HIDDEN, "Scan cancelled."
    if SCAN["error"]:
        return no_update, True, CANCEL_HIDDEN, f"Error: {SCAN['error']}"
    return no_update, True, CANCEL_HIDDEN, no_update


# ---- Cancel a running scan ---- #
@app.callback(
    Output("scan-status", "children", allow_duplicate=True),
    Input("cancel-btn", "n_clicks"),
    prevent_initial_call=True,
)
def cancel_scan_cb(_n):
    SCAN["cancel"] = True
    return "Cancelling…"


# ---- Navigate: Up, and drill-in by clicking a folder name ---- #
@app.callback(
    Output("current-path", "data"),
    Input("up", "n_clicks"),
    Input("table", "active_cell"),
    State("current-path", "data"),
    State("table", "derived_viewport_data"),   # sorted/filtered view (row maps here)
    prevent_initial_call=True,
)
def navigate(_u, active_cell, cur_path, view_data):
    trigger = ctx.triggered_id

    if trigger == "up":
        if not cur_path or cur_path == ROOT_PATH:
            return no_update
        parent = os.path.dirname(cur_path)
        return parent if parent in INDEX else no_update

    if trigger == "table" and active_cell and view_data:
        if active_cell.get("column_id") != "name":
            return no_update
        row = active_cell.get("row")
        if row is None or row >= len(view_data):
            return no_update
        path = view_data[row].get("path")
        node = INDEX.get(path)
        if node and node.is_dir and node.children:
            return path
        return no_update

    return no_update


def selected_items(cur_path, sel_rows, view_data) -> list[Item]:
    """Resolve the table's ticked rows to their Items (matched by path)."""
    node = INDEX.get(cur_path)
    if not node or not sel_rows or not view_data:
        return []
    paths = {view_data[i].get("path") for i in sel_rows if i < len(view_data)}
    return [c for c in node.children if c.path in paths]


@app.callback(
    Output("card-total", "children"),
    Output("card-files", "children"),
    Output("card-dirs", "children"),
    Output("card-biggest", "children"),
    Output("path-label", "children"),
    Output("treemap", "figure"),
    Output("sunburst", "figure"),
    Output("bar", "figure"),
    Output("filetype", "figure"),
    Output("agechart", "figure"),
    Output("footer", "children"),
    Input("current-path", "data"),
    Input("refresh", "data"),
    Input("theme", "data"),
    Input("privacy", "data"),
    Input("palette", "value"),
    prevent_initial_call=True,
)
def render(cur_path, _refresh, theme_name, privacy, palette):
    th = theme(theme_name)
    sc = palette_scale(palette)
    node = INDEX.get(cur_path) if cur_path else None
    if not node:
        return ("—", "—", "—", "—", "No folder selected",
                empty_fig(th=th), empty_fig(th=th), empty_fig(th=th),
                empty_fig(th=th), empty_fig(th=th), "Ready.")

    # counts across subtree
    files = dirs = 0
    stack = [node]
    while stack:
        cur = stack.pop()
        for c in cur.children:
            if c.is_dir:
                dirs += 1
                stack.append(c)
            else:
                files += 1

    biggest_txt = "—"
    if node.children:
        b = max(node.children, key=lambda c: c.size)
        biggest_txt = (f"{_trim(mask_name(b.name, b.is_dir, privacy), 20)}\n"
                       f"{human_size(b.size)}")

    shown_path = "🔒  path hidden" if privacy else cur_path
    footer = (f"{shown_path}  —  {len(node.children)} items, "
              f"{human_size(node.size)} total")

    return (human_size(node.size), f"{files:,}", f"{dirs:,}", biggest_txt,
            shown_path, treemap_fig(node, th, privacy, sc),
            sunburst_fig(node, th, privacy, sc), bar_fig(node, th, privacy, sc),
            filetype_fig(node, th=th, scale=sc), age_fig(node, th, sc), footer)


# ---- Table: filtered/searched list of the current folder's children ---- #
@app.callback(
    Output("table", "data"),
    Input("current-path", "data"),
    Input("refresh", "data"),
    Input("search", "value"),
    Input("minsize", "value"),
    Input("kind", "value"),
    Input("privacy", "data"),
    prevent_initial_call=True,
)
def fill_table(cur_path, _refresh, search, minsize, kind, privacy):
    node = INDEX.get(cur_path) if cur_path else None
    if not node:
        return []
    total = node.size or 1
    q = (search or "").lower().strip()
    try:
        mn = int(minsize or 0)
    except (TypeError, ValueError):
        mn = 0

    # With a search term, look through the WHOLE subtree (so you can find a file
    # or extension buried in any subfolder); otherwise list this folder's own
    # children. Recursive results are capped and sorted by size.
    if q:
        candidates = []
        stack = [node]
        while stack:
            cur = stack.pop()
            for c in cur.children:
                candidates.append(c)
                if c.is_dir:
                    stack.append(c)
        limit = 1000
    else:
        candidates = node.children
        limit = None

    matched = []
    for c in candidates:
        if q and q not in c.name.lower():   # search matches the real name
            continue
        if c.size < mn:
            continue
        if kind == "files" and c.is_dir:
            continue
        if kind == "folders" and not c.is_dir:
            continue
        matched.append(c)

    matched.sort(key=lambda c: c.size, reverse=True)
    if limit:
        matched = matched[:limit]

    rel_from = os.path.dirname(node.path)     # show where a hit lives, when searching
    rows = []
    for c in matched:
        display = mask_name(c.name, c.is_dir, privacy)
        if q and not privacy:
            parent = os.path.dirname(c.path)
            if parent and parent != node.path:
                sub = os.path.relpath(parent, rel_from)
                display = f"{display}   ·   {sub}"
        url = icon_url(c.name, c.is_dir)
        cell = f"![]({url}) {display}" if url else display
        rows.append(dict(
            name=cell,
            size=human_size(c.size),
            pct=c.size / total,                 # fraction -> percentage format
            modified=fmt_iso(c.mtime),          # ISO so the column sorts by date
            type="Folder" if c.is_dir else "File",
            path=c.path,          # hidden: used for selection + drill-in
        ))
    return rows


# ---- Selected-item details panel ---- #
@app.callback(
    Output("details", "children"),
    Input("table", "derived_virtual_selected_rows"),
    Input("current-path", "data"),
    Input("privacy", "data"),
    State("table", "derived_virtual_data"),
    prevent_initial_call=True,
)
def show_details(sel_rows, cur_path, privacy, view_data):
    items = selected_items(cur_path, sel_rows, view_data)
    if not items:
        return html.Div("Select one or more rows to see details.",
                        style=dict(color="var(--muted)", fontSize="13px",
                                   padding="10px 4px"))
    if len(items) > 1:
        total = sum(i.size for i in items)
        return html.Div([
            html.Div(f"{len(items)} items selected",
                     style=dict(fontWeight="700", fontSize="14px",
                                marginBottom="6px")),
            html.Div(f"Combined size: {human_size(total)}",
                     style=dict(fontSize="13px", color="var(--muted)")),
        ])

    item = items[0]

    def row(label, value):
        return html.Div(
            [html.Span(label, style=dict(color="var(--muted)", width="76px",
                                         display="inline-block",
                                         fontSize="12px")),
             html.Span(value, style=dict(fontSize="13px"))],
            style=dict(marginBottom="3px"),
        )

    kind = "Folder" if item.is_dir else (
        (os.path.splitext(item.name)[1].lstrip(".").upper() or "File") + " file")
    disp_name = mask_name(item.name, item.is_dir, privacy)
    disp_path = "🔒  hidden" if privacy else item.path
    url = icon_url(item.name, item.is_dir)
    header = [html.Span(_trim(disp_name, 58))]
    if url:
        header.insert(0, html.Img(src=url, height="16",
                                  style=dict(verticalAlign="-2px",
                                             marginRight="6px")))
    return html.Div([
        html.Div(header,
                 style=dict(fontWeight="700", fontSize="14px",
                            marginBottom="6px", wordBreak="break-all")),
        row("Size", human_size(item.size)),
        row("Modified", fmt_date(item.mtime)),
        row("Type", kind),
        row("Path", disp_path),
    ])


# ---- Open in Explorer (first selected) ---- #
@app.callback(
    Output("footer", "children", allow_duplicate=True),
    Input("open-btn", "n_clicks"),
    State("current-path", "data"),
    State("table", "derived_virtual_selected_rows"),
    State("table", "derived_virtual_data"),
    prevent_initial_call=True,
)
def open_selected(_n, cur_path, sel_rows, view_data):
    items = selected_items(cur_path, sel_rows, view_data)
    if not items:
        return "Tick a row first, then click Open."
    ok = open_in_explorer(items[0].path)
    return (f"Opened in Explorer: {items[0].path}" if ok
            else f"Could not open: {items[0].path}")


# ---- Delete: step 1, ask for confirmation (supports multi-select) ---- #
@app.callback(
    Output("confirm-del", "displayed"),
    Output("confirm-del", "message"),
    Output("pending-delete", "data"),
    Input("del-btn", "n_clicks"),
    State("current-path", "data"),
    State("table", "derived_virtual_selected_rows"),
    State("table", "derived_virtual_data"),
    prevent_initial_call=True,
)
def ask_delete(_n, cur_path, sel_rows, view_data):
    items = selected_items(cur_path, sel_rows, view_data)
    if not items:
        return True, "Tick one or more rows first, then click Delete.", None
    total = sum(i.size for i in items)
    if len(items) == 1:
        it = items[0]
        head = f"Send this {'folder' if it.is_dir else 'file'} to the Recycle Bin?"
        body = f"{it.name}\n{human_size(it.size)}\n\n{it.path}"
    else:
        head = f"Send {len(items)} items to the Recycle Bin?"
        body = f"Combined size: {human_size(total)}"
    msg = (f"{head}\n\n{body}\n\n"
           f"(Reversible — you can restore them from the Recycle Bin.)")
    return True, msg, [i.path for i in items]


# ---- Delete: step 2, user confirmed ---- #
@app.callback(
    Output("refresh", "data"),
    Output("footer", "children", allow_duplicate=True),
    Output("table", "selected_rows"),
    Input("confirm-del", "submit_n_clicks"),
    State("pending-delete", "data"),
    State("refresh", "data"),
    prevent_initial_call=True,
)
def do_delete(_submit, paths, refresh):
    if not paths:
        return no_update, no_update, no_update
    if isinstance(paths, str):
        paths = [paths]
    ok, failed = 0, []
    for p in paths:
        try:
            send2trash(os.path.normpath(p))
            remove_item(p)
            ok += 1
        except Exception as e:                   # noqa: BLE001
            failed.append(f"{os.path.basename(p)}: {e}")
    msg = f"Sent {ok} item(s) to Recycle Bin."
    if failed:
        msg += "  Failed: " + "; ".join(failed[:3])
    return (refresh or 0) + 1, msg, []


# ---- Duplicate finder (on demand) ---- #
@app.callback(
    Output("dupe-table", "data"),
    Output("dupe-summary", "children"),
    Input("dupe-btn", "n_clicks"),
    State("current-path", "data"),
    State("dupe-min", "value"),
    State("privacy", "data"),
    prevent_initial_call=True,
)
def find_dupes_cb(_n, cur_path, min_size, privacy):
    node = INDEX.get(cur_path) if cur_path else None
    if not node:
        return [], "Scan a folder first."
    groups = find_duplicates(node, int(min_size or 1_048_576))
    if not groups:
        return [], "No duplicate files found in this folder."
    wasted_total = sum(g[0].size * (len(g) - 1) for g in groups)
    rows = []
    for g in groups[:500]:
        rep = g[0]
        locs = ("🔒 hidden" if privacy
                else "   |   ".join(os.path.dirname(x.path) for x in g))
        rows.append(dict(
            name=mask_name(rep.name, False, privacy),
            size=human_size(rep.size),
            copies=len(g),
            wasted=human_size(rep.size * (len(g) - 1)),
            locations=locs,
        ))
    summary = (f"⚠ {len(groups):,} duplicate set(s) · potential savings "
               f"{human_size(wasted_total)}")
    return rows, summary


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8050"))
    print(f"Disk Space Dashboard running at  http://127.0.0.1:{port}", flush=True)
    print("Open that URL in your browser, then click 'Select Folder'.", flush=True)
    app.run(debug=False, host="127.0.0.1", port=port)
