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
import math
import time
import heapq
import base64
import hashlib
import datetime
import threading
import subprocess
from dataclasses import dataclass, field

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

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

import plotly.graph_objects as go
from send2trash import send2trash
from dash import Dash, dcc, html, dash_table, Input, Output, State, ctx, no_update
from dash.dash_table.Format import Format, Scheme


# ----------------------------- palette ------------------------------------- #

PALETTE = ["#173F5F", "#20639B", "#3CAEA3", "#F6D55C", "#ED553B"]
# Continuous colour scale: 0.0 (smallest) -> 1.0 (biggest = red)
COLOR_SCALE = [
    [0.00, "#173F5F"],
    [0.25, "#20639B"],
    [0.50, "#3CAEA3"],
    [0.75, "#F6D55C"],
    [1.00, "#ED553B"],
]
BG = "#1e1e1e"
PANEL = "#242424"
FG = "#e6e6e6"
MUTED = "#9a9a9a"

# Two themes. Container chrome flips instantly via CSS variables (see
# app.index_string); the Plotly figures carry baked-in colours, so they are
# rebuilt per theme in render(). The size colour-scale works on both grounds.
THEMES = {
    "dark": dict(bg="#1e1e1e", panel="#242424", fg="#e6e6e6", muted="#9a9a9a",
                 grid="#3a3a3a", line="#242424", hover="#2b2b2b"),
    "light": dict(bg="#ffffff", panel="#ffffff", fg="#1e1e1e", muted="#6b7280",
                  grid="#e2e2e6", line="#ffffff", hover="#ffffff"),
}


def theme(name):
    return THEMES.get(name or "dark", THEMES["dark"])


# Heights that autofit to the viewport (offsets = toolbar + cards + chrome).
CHART_H = "calc(100vh - 250px)"
TABLE_H = "calc(100vh - 320px)"


# ----------------------------- helpers ------------------------------------- #

def human_size(num_bytes: float) -> str:
    """Convert a byte count into a human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:3.1f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.1f} EB"


def _trim(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def mask_name(name: str, is_dir: bool, privacy: bool) -> str:
    """Privacy mode: hide the name but keep a file's extension so type reads."""
    if not privacy:
        return name
    if is_dir:
        return "•••••"
    return "•••••" + os.path.splitext(name)[1]


# ------------------------- Windows shell icons ----------------------------- #
# The real Explorer icon for each file type, extracted once per extension and
# cached as PNG bytes. Served as same-origin URLs (/icon/<key>) because the
# DataTable markdown renderer blocks data: image URIs. Windows only.

_ICON_PNG: dict[str, bytes] = {}     # key -> png bytes (b"" if no icon)


if sys.platform == "win32":
    class _SHFILEINFO(ctypes.Structure):
        _fields_ = [
            ("hIcon", wintypes.HICON),
            ("iIcon", ctypes.c_int),
            ("dwAttributes", wintypes.DWORD),
            ("szDisplayName", wintypes.WCHAR * 260),
            ("szTypeName", wintypes.WCHAR * 80),
        ]


def _extract_icon_png(dummy_name: str, is_dir: bool) -> bytes:
    import io
    import win32gui
    import win32ui
    from PIL import Image

    FILE_ATTRIBUTE_DIRECTORY = 0x10
    FILE_ATTRIBUTE_NORMAL = 0x80
    SHGFI_ICON = 0x000000100
    SHGFI_SMALLICON = 0x000000001
    SHGFI_USEFILEATTRIBUTES = 0x000000010

    info = _SHFILEINFO()
    attrs = FILE_ATTRIBUTE_DIRECTORY if is_dir else FILE_ATTRIBUTE_NORMAL
    flags = SHGFI_ICON | SHGFI_SMALLICON | SHGFI_USEFILEATTRIBUTES
    res = ctypes.windll.shell32.SHGetFileInfoW(
        dummy_name, attrs, ctypes.byref(info), ctypes.sizeof(info), flags)
    hicon = info.hIcon
    if not res or not hicon:
        return b""

    size = 16
    screen = memdc = hdc = None
    try:
        screen = win32gui.GetDC(0)
        hdc = win32ui.CreateDCFromHandle(screen)
        hbmp = win32ui.CreateBitmap()
        hbmp.CreateCompatibleBitmap(hdc, size, size)
        memdc = hdc.CreateCompatibleDC()
        memdc.SelectObject(hbmp)
        memdc.DrawIcon((0, 0), hicon)
        bits = hbmp.GetBitmapBits(True)
        img = Image.frombuffer("RGBA", (size, size), bits, "raw", "BGRA", 0, 1)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    finally:
        try:
            ctypes.windll.user32.DestroyIcon(hicon)
            if memdc:
                memdc.DeleteDC()
            if hdc:
                hdc.DeleteDC()
            if screen:
                win32gui.ReleaseDC(0, screen)
        except Exception:
            pass


def _icon_key(name: str, is_dir: bool) -> str:
    if is_dir:
        return "dir"
    return os.path.splitext(name)[1].lower().lstrip(".") or "noext"


def _icon_png_for_key(key: str) -> bytes:
    """PNG bytes for an icon key, extracting + caching on first use."""
    if sys.platform != "win32":
        return b""
    if key not in _ICON_PNG:
        is_dir = key == "dir"
        ext = "" if key in ("dir", "noext") else "." + key
        try:
            _ICON_PNG[key] = _extract_icon_png(
                "folder" if is_dir else "file" + ext, is_dir)
        except Exception:
            _ICON_PNG[key] = b""
    return _ICON_PNG[key]


def icon_url(name: str, is_dir: bool) -> str:
    """Same-origin URL for this item's type icon ('' if none/unsupported)."""
    key = _icon_key(name, is_dir)
    return f"/icon/{key}" if _icon_png_for_key(key) else ""


@dataclass
class Item:
    """One file or folder plus its total size in bytes and modified time."""
    name: str
    path: str
    size: int
    is_dir: bool
    children: list = field(default_factory=list)
    mtime: float = 0.0


def fmt_date(ts: float) -> str:
    """Epoch seconds -> '23 Aug 2026' (or '—' when unknown)."""
    if not ts:
        return "—"
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%d %b %Y")
    except (OSError, OverflowError, ValueError):
        return "—"


def fmt_iso(ts: float) -> str:
    """Epoch seconds -> '2026-08-23' (sortable in the table); '' when unknown."""
    if not ts:
        return ""
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return ""


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


def scan_directory(root_path: str, state: dict | None = None) -> Item:
    """
    Walk a directory tree, returning an Item tree with cumulative sizes and
    modified times, and (re)build the global INDEX path -> Item.

    Symlinks are skipped (avoids loops / double counting). Permission errors and
    unreadable entries are skipped, counted in state["skipped"]. If `state` is
    given, progress counters are updated and state["cancel"] aborts the walk.
    """
    global INDEX, ROOT_PATH
    INDEX = {}
    ROOT_PATH = os.path.abspath(root_path)

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


# ----------------------------- figures ------------------------------------- #

def base_layout(th):
    return dict(
        paper_bgcolor=th["panel"], plot_bgcolor=th["panel"],
        font=dict(color=th["fg"]), margin=dict(l=10, r=10, t=40, b=10),
    )


def empty_fig(msg="Select a folder to begin", th=THEMES["dark"]):
    fig = go.Figure()
    fig.update_layout(**base_layout(th))
    fig.add_annotation(text=msg, showarrow=False,
                       font=dict(color=th["muted"], size=16), x=0.5, y=0.5)
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return fig


def build_hierarchy(item: Item, max_nodes: int = 700, max_depth: int = 6,
                    privacy: bool = False):
    """
    Flatten `item`'s subtree into the parallel arrays Plotly's treemap/sunburst
    want (ids/labels/parents/values/customdata), plus a per-node colour value.

    To stay responsive on huge trees, a size-priority walk keeps the most
    significant nodes first, up to `max_nodes`, with depth capped at `max_depth`.

    Colour is RELATIVE to siblings: the biggest item in any given folder = 1.0
    (red), the smallest -> 0.0 (deep blue). This shows the full palette spectrum
    at every level; absolute-size colouring just paints everything red at the top
    because all the top-level folders are large.

    Returns (ids, labels, parents, values, custom, node_colors).
    """
    ids = [item.path]
    labels = [mask_name(item.name, True, privacy)]
    parents = [""]
    values = [item.size]
    custom = [["Folder", human_size(item.size), ""]]

    counter = 0  # tie-breaker so the heap never compares Item objects
    heap = []
    for c in item.children:
        if c.size > 0:
            heapq.heappush(heap, (-c.size, counter, c, item.path, 1))
            counter += 1

    while heap and len(ids) < max_nodes:
        _, _, node, parent_path, depth = heapq.heappop(heap)
        ids.append(node.path)
        labels.append(mask_name(node.name, node.is_dir, privacy))
        parents.append(parent_path)
        values.append(node.size)
        kind = "Folder" if node.is_dir else "File"
        pctstr = f"{100.0 * node.size / (item.size or 1):.1f}% of folder"
        custom.append([kind, human_size(node.size), pctstr])
        if node.is_dir and depth < max_depth:
            for c in node.children:
                if c.size > 0:
                    heapq.heappush(heap, (-c.size, counter, c, node.path, depth + 1))
                    counter += 1

    # Fill truncated folders: when a folder's drawn children don't add up to its
    # own size (the size cap dropped its many tiny items), add a single synthetic
    # "(smaller items)" child for the remainder. Otherwise branchvalues="total"
    # reserves that space and it renders as an empty/black gap.
    value_of = dict(zip(ids, values))
    included_sum: dict[str, int] = {}
    for pid, v in zip(parents, values):
        if pid:
            included_sum[pid] = included_sum.get(pid, 0) + v
    for pid in list(included_sum):
        remainder = value_of.get(pid, 0) - included_sum[pid]
        if remainder > 0:
            ids.append(pid + "(other)")
            labels.append("(smaller items)")
            parents.append(pid)
            values.append(remainder)
            custom.append(["", human_size(remainder), "grouped small items"])

    maxchild: dict[str, int] = {}
    for pid, v in zip(parents, values):
        if pid and v > maxchild.get(pid, 0):
            maxchild[pid] = v
    node_colors = []
    for pid, v in zip(parents, values):
        if not pid:
            node_colors.append(0.0)             # root -> coolest colour
        else:
            m = maxchild.get(pid, v) or 1
            node_colors.append(v / m)

    return ids, labels, parents, values, custom, node_colors


def treemap_fig(item: Item, th=THEMES["dark"], privacy=False) -> go.Figure:
    """
    Hierarchical treemap of the current folder's subtree. Click a tile to zoom
    in, click the header (pathbar) to zoom back out. Colour = size vs. siblings
    (deep blue = small, red = biggest in that folder).
    """
    if not item.children:
        return empty_fig("Empty folder", th)
    ids, labels, parents, values, custom, node_colors = build_hierarchy(
        item, privacy=privacy)

    fig = go.Figure(go.Treemap(
        ids=ids,
        labels=labels,
        parents=parents,
        values=values,
        branchvalues="total",
        maxdepth=3,
        customdata=custom,
        marker=dict(
            colors=node_colors,
            colorscale=COLOR_SCALE,
            cmin=0.0, cmax=1.0,
            cornerradius=6,                     # modern rounded tiles
            pad=dict(t=28, l=4, r=4, b=4),
            colorbar=dict(
                title=dict(text="Size vs.<br>siblings", side="right",
                           font=dict(size=11)),
                tickvals=[0, 1], ticktext=["small", "biggest"],
                thickness=14, len=0.85, outlinewidth=0, tickfont=dict(size=11),
                bgcolor="rgba(0,0,0,0)",
            ),
            line=dict(width=1, color=th["line"]),
        ),
        text=[c[1] for c in custom],
        texttemplate="<b>%{label}</b><br>%{text}",
        textposition="middle center",
        insidetextfont=dict(family="Segoe UI, system-ui, sans-serif",
                            color="rgba(255,255,255,0.95)"),
        pathbar=dict(visible=True, side="top", thickness=26,
                     textfont=dict(color=th["fg"], size=13)),
        hovertemplate=(
            "<b>%{label}</b><br>%{customdata[0]}<br>"
            "Size: %{customdata[1]}<br>%{customdata[2]}<extra></extra>"
        ),
        tiling=dict(pad=3),
        root=dict(color="rgba(128,128,128,0.10)"),
    ))
    fig.update_layout(
        **base_layout(th),
        uniformtext=dict(minsize=11, mode="hide"),   # hide labels that won't fit
        hoverlabel=dict(bgcolor=th["hover"], bordercolor="#3a7ebf",
                        font=dict(color=th["fg"], family="Segoe UI, sans-serif",
                                  size=13)),
    )
    return fig


def sunburst_fig(item: Item, th=THEMES["dark"], privacy=False) -> go.Figure:
    """
    Radial hierarchy of the current folder's subtree. Rings = depth, angle =
    size; click a wedge to zoom in, click the centre to zoom out. Same data and
    size-vs-siblings colour scale as the treemap.
    """
    if not item.children:
        return empty_fig("Empty folder", th)
    ids, labels, parents, values, custom, node_colors = build_hierarchy(
        item, privacy=privacy)

    fig = go.Figure(go.Sunburst(
        ids=ids,
        labels=labels,
        parents=parents,
        values=values,
        customdata=custom,
        branchvalues="total",
        maxdepth=3,
        insidetextorientation="radial",
        marker=dict(
            colors=node_colors,
            colorscale=COLOR_SCALE,
            cmin=0.0, cmax=1.0,
            colorbar=dict(
                title=dict(text="Size vs.<br>siblings", side="right",
                           font=dict(size=11)),
                tickvals=[0, 1], ticktext=["small", "biggest"],
                thickness=14, len=0.85, outlinewidth=0, tickfont=dict(size=11),
                bgcolor="rgba(0,0,0,0)",
            ),
            line=dict(width=1, color=th["line"]),
        ),
        texttemplate="%{label}",
        insidetextfont=dict(family="Segoe UI, system-ui, sans-serif",
                            color="rgba(255,255,255,0.95)"),
        hovertemplate=(
            "<b>%{label}</b><br>%{customdata[0]}<br>"
            "Size: %{customdata[1]}<br>%{customdata[2]}<extra></extra>"
        ),
    ))
    fig.update_layout(
        **base_layout(th),
        uniformtext=dict(minsize=10, mode="hide"),
        hoverlabel=dict(bgcolor=th["hover"], bordercolor="#3a7ebf",
                        font=dict(color=th["fg"], family="Segoe UI, sans-serif",
                                  size=13)),
    )
    # Generous margins shrink the circle so outer-ring labels never clip.
    fig.update_layout(margin=dict(t=36, b=36, l=40, r=40))
    return fig


def filetype_fig(item: Item, top: int = 15, th=THEMES["dark"]) -> go.Figure:
    """Aggregate the whole subtree by file extension; bar of the space hogs."""
    agg: dict[str, int] = {}
    cnt: dict[str, int] = {}
    stack = [item]
    while stack:
        cur = stack.pop()
        for c in cur.children:
            if c.is_dir:
                stack.append(c)
            else:
                ext = os.path.splitext(c.name)[1].lower() or "(no ext)"
                agg[ext] = agg.get(ext, 0) + c.size
                cnt[ext] = cnt.get(ext, 0) + 1

    pairs = sorted(agg.items(), key=lambda kv: kv[1], reverse=True)[:top]
    if not pairs:
        return empty_fig("No files", th)
    pairs = pairs[::-1]  # biggest on top
    exts = [e for e, _ in pairs]
    values = [v for _, v in pairs]
    labels = [f"{human_size(v)}  ·  {cnt[e]:,} files" for e, v in pairs]

    fig = go.Figure(go.Bar(
        x=values, y=exts, orientation="h",
        marker=dict(color=values, colorscale=COLOR_SCALE,
                    cmin=min(values), cmax=max(values), line=dict(width=0)),
        text=labels, textposition="outside",
        hovertemplate="<b>%{y}</b><br>%{text}<extra></extra>",
    ))
    fig.update_layout(
        **base_layout(th),
        title=dict(text="Space used by file type", font=dict(size=15)),
        xaxis=dict(showgrid=True, gridcolor=th["grid"], zeroline=False,
                   tickvals=[], title=""),
        yaxis=dict(showgrid=False),
        bargap=0.25,
    )
    return fig


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


def bar_fig(item: Item, th=THEMES["dark"], privacy=False) -> go.Figure:
    """Horizontal bar of the biggest items, same size colour scale."""
    kids = sorted(item.children, key=lambda c: c.size, reverse=True)[:15]
    if not kids:
        return empty_fig("Empty folder", th)
    kids = kids[::-1]  # biggest on top
    names = [_trim(mask_name(c.name, c.is_dir, privacy), 30) for c in kids]
    values = [c.size for c in kids]
    labels = [human_size(c.size) for c in kids]

    fig = go.Figure(go.Bar(
        x=values, y=names, orientation="h",
        marker=dict(
            color=values, colorscale=COLOR_SCALE,
            cmin=min(values), cmax=max(values),
            line=dict(width=0),
        ),
        text=labels, textposition="outside",
        hovertemplate="<b>%{y}</b><br>%{text}<extra></extra>",
    ))
    fig.update_layout(
        **base_layout(th),
        title=dict(text="Biggest items", font=dict(size=15)),
        xaxis=dict(showgrid=True, gridcolor=th["grid"], zeroline=False,
                   tickvals=[], title=""),
        yaxis=dict(showgrid=False),
        bargap=0.25,
    )
    return fig


# Age buckets: (label, upper bound in days). Files land in the first bucket
# whose bound they're within; the last bucket catches everything older.
AGE_BUCKETS = [
    ("Today", 1), ("≤ 7 days", 7), ("≤ 30 days", 30), ("≤ 90 days", 90),
    ("≤ 6 months", 182), ("≤ 1 year", 365), ("1–3 years", 1095),
    ("> 3 years", math.inf),
]


def age_fig(item: Item, th=THEMES["dark"]) -> go.Figure:
    """Bar of total file size by modified-age bucket (older = warmer colour)."""
    now = time.time()
    sizes = [0] * len(AGE_BUCKETS)
    counts = [0] * len(AGE_BUCKETS)
    stack = [item]
    while stack:
        cur = stack.pop()
        for c in cur.children:
            if c.is_dir:
                stack.append(c)
                continue
            age_days = (now - c.mtime) / 86400.0 if c.mtime else math.inf
            for i, (_, bound) in enumerate(AGE_BUCKETS):
                if age_days <= bound:
                    sizes[i] += c.size
                    counts[i] += 1
                    break
    if not any(sizes):
        return empty_fig("No files", th)

    labels = [b[0] for b in AGE_BUCKETS]
    colours = [i / (len(AGE_BUCKETS) - 1) for i in range(len(AGE_BUCKETS))]
    text = [human_size(s) if s else "" for s in sizes]
    fig = go.Figure(go.Bar(
        x=labels, y=sizes,
        marker=dict(color=colours, colorscale=COLOR_SCALE, cmin=0, cmax=1,
                    line=dict(width=0)),
        text=text, textposition="outside",
        customdata=[[human_size(s), n] for s, n in zip(sizes, counts)],
        hovertemplate=("<b>%{x}</b><br>%{customdata[0]} · "
                       "%{customdata[1]} files<extra></extra>"),
    ))
    fig.update_layout(
        **base_layout(th),
        title=dict(text="Storage by file age  (older → warmer)",
                   font=dict(size=15)),
        xaxis=dict(showgrid=False),
        yaxis=dict(showgrid=True, gridcolor=th["grid"], zeroline=False,
                   tickvals=[], title=""),
        bargap=0.3,
    )
    return fig


# ----------------------------- duplicates ---------------------------------- #

def _file_hash(path: str, full: bool = False) -> str | None:
    """SHA-1 of a file: first 64 KB (quick) or the whole file (full)."""
    h = hashlib.sha1()
    try:
        with open(path, "rb") as f:
            if full:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            else:
                h.update(f.read(65536))
        return h.hexdigest()
    except (OSError, PermissionError):
        return None


def find_duplicates(item: Item, min_size: int = 1_048_576) -> list[list[Item]]:
    """
    Find groups of byte-identical files at/under `item` (size >= min_size).

    Staged for performance: bucket by exact size, then by a 64 KB quick hash,
    then confirm with a full hash. Returns groups (>= 2 files) sorted by wasted
    space (size * (copies - 1)) descending.
    """
    by_size: dict[int, list[Item]] = {}
    stack = [item]
    while stack:
        cur = stack.pop()
        for c in cur.children:
            if c.is_dir:
                stack.append(c)
            elif c.size >= min_size:
                by_size.setdefault(c.size, []).append(c)

    groups: list[list[Item]] = []
    for size, items in by_size.items():
        if len(items) < 2:
            continue
        by_quick: dict[str, list[Item]] = {}
        for it in items:
            qh = _file_hash(it.path, full=False)
            if qh is not None:
                by_quick.setdefault(qh, []).append(it)
        for quick_group in by_quick.values():
            if len(quick_group) < 2:
                continue
            by_full: dict[str, list[Item]] = {}
            for it in quick_group:
                fh = _file_hash(it.path, full=True)
                if fh is not None:
                    by_full.setdefault(fh, []).append(it)
            for full_group in by_full.values():
                if len(full_group) >= 2:
                    groups.append(full_group)

    groups.sort(key=lambda g: g[0].size * (len(g) - 1), reverse=True)
    return groups


# ----------------------------- app / layout -------------------------------- #

app = Dash(__name__, title="Disk Space Dashboard")


@app.server.route("/icon/<key>")
def _serve_icon(key):
    """Serve a cached Windows type-icon PNG (used by the table's Name column)."""
    from flask import Response
    png = _icon_png_for_key(key)
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
                                        style=dict(height=CHART_H))),
                            dcc.Tab(label="Sunburst", value="sun",
                                    style=_tab(), selected_style=_tab(True),
                                    children=dcc.Graph(
                                        id="sunburst", figure=empty_fig(),
                                        responsive=True,
                                        style=dict(height=CHART_H))),
                            dcc.Tab(label="Bar Chart", value="bar",
                                    style=_tab(), selected_style=_tab(True),
                                    children=dcc.Graph(
                                        id="bar", figure=empty_fig(),
                                        responsive=True,
                                        style=dict(height=CHART_H))),
                            dcc.Tab(label="File Types", value="ftype",
                                    style=_tab(), selected_style=_tab(True),
                                    children=dcc.Graph(
                                        id="filetype", figure=empty_fig(),
                                        responsive=True,
                                        style=dict(height=CHART_H))),
                            dcc.Tab(label="File Age", value="age",
                                    style=_tab(), selected_style=_tab(True),
                                    children=dcc.Graph(
                                        id="agechart", figure=empty_fig(),
                                        responsive=True,
                                        style=dict(height=CHART_H))),
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

# ---- Start a scan (native folder picker -> background thread + polling) ---- #
@app.callback(
    Output("scan-poll", "disabled"),
    Output("cancel-btn", "style"),
    Output("scan-status", "children"),
    Input("browse", "n_clicks"),
    prevent_initial_call=True,
)
def start_scan_cb(_n):
    chosen = pick_folder_dialog()
    if not chosen:
        return True, CANCEL_HIDDEN, ""
    start_scan(chosen)
    return False, CANCEL_SHOWN, "Starting scan…"


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
    prevent_initial_call=True,
)
def render(cur_path, _refresh, theme_name, privacy):
    th = theme(theme_name)
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
            shown_path, treemap_fig(node, th, privacy),
            sunburst_fig(node, th, privacy), bar_fig(node, th, privacy),
            filetype_fig(node, th=th), age_fig(node, th), footer)


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
