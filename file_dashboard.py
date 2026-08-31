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
import heapq
import subprocess
from collections import deque
from dataclasses import dataclass, field

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


@dataclass
class Item:
    """One file or folder plus its total size in bytes."""
    name: str
    path: str
    size: int
    is_dir: bool
    children: list = field(default_factory=list)


# ----------------------------- scanner ------------------------------------- #

# Populated on each scan: maps absolute path -> Item, so callbacks can look up
# any node by its path (used for drill-in navigation) without re-scanning.
INDEX: dict[str, Item] = {}
ROOT_PATH: str | None = None


def scan_directory(root_path: str) -> Item:
    """
    Walk a directory tree, returning an Item tree with cumulative sizes,
    and (re)build the global INDEX path -> Item.

    Symlinks are skipped (avoids loops / double counting). Permission errors
    and unreadable entries are skipped silently.
    """
    global INDEX, ROOT_PATH
    INDEX = {}
    ROOT_PATH = os.path.abspath(root_path)

    def walk(path: str, name: str) -> Item:
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
                            size = entry.stat(follow_symlinks=False).st_size
                            children.append(Item(entry.name, entry.path, size, False))
                            total += size
                    except (PermissionError, OSError):
                        continue
        except (PermissionError, OSError):
            pass
        node = Item(name, path, total, True, children)
        INDEX[os.path.abspath(path)] = node
        return node

    disp = os.path.basename(ROOT_PATH.rstrip("\\/")) or ROOT_PATH
    return walk(ROOT_PATH, disp)


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


def build_hierarchy(item: Item, max_nodes: int = 700, max_depth: int = 6):
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
    labels = [item.name]
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
        labels.append(node.name)
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


def treemap_fig(item: Item, th=THEMES["dark"]) -> go.Figure:
    """
    Hierarchical treemap of the current folder's subtree. Click a tile to zoom
    in, click the header (pathbar) to zoom back out. Colour = size vs. siblings
    (deep blue = small, red = biggest in that folder).
    """
    if not item.children:
        return empty_fig("Empty folder", th)
    ids, labels, parents, values, custom, node_colors = build_hierarchy(item)

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


def sunburst_fig(item: Item, th=THEMES["dark"]) -> go.Figure:
    """
    Radial hierarchy of the current folder's subtree. Rings = depth, angle =
    size; click a wedge to zoom in, click the centre to zoom out. Same data and
    size-vs-siblings colour scale as the treemap.
    """
    if not item.children:
        return empty_fig("Empty folder", th)
    ids, labels, parents, values, custom, node_colors = build_hierarchy(item)

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


def bar_fig(item: Item, th=THEMES["dark"]) -> go.Figure:
    """Horizontal bar of the biggest items, same size colour scale."""
    kids = sorted(item.children, key=lambda c: c.size, reverse=True)[:15]
    if not kids:
        return empty_fig("Empty folder", th)
    kids = kids[::-1]  # biggest on top
    names = [_trim(c.name, 30) for c in kids]
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


# ----------------------------- app / layout -------------------------------- #

app = Dash(__name__, title="Disk Space Dashboard")

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
        dcc.ConfirmDialog(id="confirm-del"),
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
                html.Button("◐  Light / Dark", id="theme-btn", n_clicks=0,
                            style={**BTN_STYLE, "backgroundColor": "#666"}),
                dcc.Loading(html.Div(id="scan-status"),
                            type="circle", color="#3a7ebf"),
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
                            "Largest items — click a folder name to open; "
                            "use the radio to pick a target for the buttons.",
                            style=dict(fontWeight="700", padding="6px 4px",
                                       fontSize="13px")),
                        html.Div(
                            style=dict(display="flex", gap="8px",
                                       margin="4px 4px 8px"),
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
                        dash_table.DataTable(
                            id="table",
                            columns=[
                                dict(name="Name", id="name"),
                                dict(name="Size", id="size"),
                                dict(name="%", id="pct"),
                                dict(name="Type", id="type"),
                            ],
                            data=[],
                            page_size=100,
                            row_selectable="single",
                            selected_rows=[],
                            style_table=dict(height=TABLE_H, overflowY="auto"),
                            style_cell_conditional=[
                                dict(if_=dict(column_id="name"), width="45%"),
                                dict(if_=dict(column_id="size"), width="20%"),
                                dict(if_=dict(column_id="pct"), width="15%"),
                                dict(if_=dict(column_id="type"), width="20%"),
                            ],
                            style_header=dict(backgroundColor="var(--head)",
                                              color="var(--fg)", fontWeight="700",
                                              border="none", textAlign="center"),
                            style_cell=dict(backgroundColor="var(--panel)",
                                            color="var(--fg)",
                                            border="none", padding="6px 10px",
                                            fontSize="13px", textAlign="center",
                                            maxWidth=0, overflow="hidden",
                                            textOverflow="ellipsis"),
                            style_data_conditional=[
                                dict(if_=dict(filter_query='{type} = "Folder"',
                                              column_id="name"),
                                     color="#6cb6ff", cursor="pointer"),
                            ],
                            cell_selectable=True,
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


# ----------------------------- callbacks ----------------------------------- #

@app.callback(
    Output("current-path", "data"),
    Output("scan-status", "children"),
    Input("browse", "n_clicks"),
    Input("up", "n_clicks"),
    Input("table", "active_cell"),
    State("current-path", "data"),
    State("table", "data"),
    prevent_initial_call=True,
)
def navigate(_b, _u, active_cell, cur_path, table_data):
    trigger = ctx.triggered_id

    if trigger == "browse":
        chosen = pick_folder_dialog()
        if not chosen:
            return no_update, ""
        scan_directory(chosen)          # rebuilds INDEX, sets ROOT_PATH
        return ROOT_PATH, ""

    if trigger == "up":
        if not cur_path or cur_path == ROOT_PATH:
            return no_update, no_update
        parent = os.path.dirname(cur_path)
        if parent in INDEX:
            return parent, no_update
        return no_update, no_update

    if trigger == "table" and active_cell and table_data:
        # only a click on the Name column drills in (radio handles selection)
        if active_cell.get("column_id") != "name":
            return no_update, no_update
        row = active_cell.get("row")
        if row is None or row >= len(table_data):
            return no_update, no_update
        node = INDEX.get(cur_path)
        if not node:
            return no_update, no_update
        name = table_data[row]["name"]
        for c in node.children:
            if c.name == name and c.is_dir and c.children:
                return c.path, no_update
        return no_update, no_update

    return no_update, no_update


def selected_item(cur_path, selected_rows, table_data) -> Item | None:
    """Resolve the table's selected radio row to its Item under the current node."""
    if not selected_rows or not table_data:
        return None
    i = selected_rows[0]
    if i >= len(table_data):
        return None
    node = INDEX.get(cur_path)
    if not node:
        return None
    name = table_data[i]["name"]
    for c in node.children:
        if c.name == name:
            return c
    return None


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
    Output("table", "data"),
    Output("footer", "children"),
    Input("current-path", "data"),
    Input("refresh", "data"),
    Input("theme", "data"),
    prevent_initial_call=True,
)
def render(cur_path, _refresh, theme_name):
    th = theme(theme_name)
    node = INDEX.get(cur_path) if cur_path else None
    if not node:
        return ("—", "—", "—", "—", "No folder selected",
                empty_fig(th=th), empty_fig(th=th), empty_fig(th=th),
                empty_fig(th=th), [], "Ready.")

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
        biggest_txt = f"{_trim(b.name, 20)}\n{human_size(b.size)}"

    total = node.size or 1
    rows = sorted(node.children, key=lambda c: c.size, reverse=True)
    data = [
        dict(name=c.name, size=human_size(c.size),
             pct=f"{100.0 * c.size / total:.1f}%",
             type="Folder" if c.is_dir else "File")
        for c in rows
    ]

    footer = (f"{cur_path}  —  {len(node.children)} items, "
              f"{human_size(node.size)} total")

    return (human_size(node.size), f"{files:,}", f"{dirs:,}", biggest_txt,
            cur_path, treemap_fig(node, th), sunburst_fig(node, th),
            bar_fig(node, th), filetype_fig(node, th=th), data, footer)


# ---- Open in Explorer ---- #
@app.callback(
    Output("footer", "children", allow_duplicate=True),
    Input("open-btn", "n_clicks"),
    State("current-path", "data"),
    State("table", "selected_rows"),
    State("table", "data"),
    prevent_initial_call=True,
)
def open_selected(_n, cur_path, selected_rows, table_data):
    item = selected_item(cur_path, selected_rows, table_data)
    if not item:
        return "Select a row (radio button) first, then click Open."
    ok = open_in_explorer(item.path)
    return (f"Opened in Explorer: {item.path}" if ok
            else f"Could not open: {item.path}")


# ---- Delete: step 1, ask for confirmation ---- #
@app.callback(
    Output("confirm-del", "displayed"),
    Output("confirm-del", "message"),
    Output("pending-delete", "data"),
    Input("del-btn", "n_clicks"),
    State("current-path", "data"),
    State("table", "selected_rows"),
    State("table", "data"),
    prevent_initial_call=True,
)
def ask_delete(_n, cur_path, selected_rows, table_data):
    item = selected_item(cur_path, selected_rows, table_data)
    if not item:
        return True, "Select a row (radio button) first, then click Delete.", None
    kind = "folder" if item.is_dir else "file"
    msg = (f"Send this {kind} to the Recycle Bin?\n\n"
           f"{item.name}\n{human_size(item.size)}\n\n{item.path}\n\n"
           f"(Reversible — you can restore it from the Recycle Bin.)")
    return True, msg, item.path


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
def do_delete(_submit, path, refresh):
    if not path:
        return no_update, no_update, no_update
    try:
        send2trash(os.path.normpath(path))
    except Exception as e:
        return no_update, f"Delete failed: {e}", no_update
    remove_item(path)
    return (refresh or 0) + 1, f"Sent to Recycle Bin: {path}", []


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8050"))
    print(f"Disk Space Dashboard running at  http://127.0.0.1:{port}", flush=True)
    print("Open that URL in your browser, then click 'Select Folder'.", flush=True)
    app.run(debug=False, host="127.0.0.1", port=port)
