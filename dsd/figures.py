"""Plotly figure builders. Pure: they take a scanned Item tree and a theme and
return go.Figure objects. `Item` is duck-typed (only .name/.path/.size/.is_dir/
.children/.mtime are used), so no import from the stateful scanner is needed."""
from __future__ import annotations

import os
import math
import time
import heapq

import plotly.graph_objects as go

from .utils import human_size, mask_name, _trim, COLOR_SCALE, THEMES


def base_layout(th):
    return dict(
        paper_bgcolor=th["panel"], plot_bgcolor=th["panel"],
        font=dict(color=th["fg"]), margin=dict(l=10, r=10, t=40, b=10),
    )


def empty_fig(msg="Select a folder to begin", th=THEMES["dark"]):
    fig = go.Figure()
    fig.update_layout(**base_layout(th))
    fig.add_annotation(text=msg, showarrow=False,
                       font=dict(color=th["muted"], size=16),
                       xref="paper", yref="paper", x=0.5, y=0.5,
                       xanchor="center", yanchor="middle")
    fig.update_xaxes(visible=False, range=[0, 1])
    fig.update_yaxes(visible=False, range=[0, 1])
    return fig


def build_hierarchy(item, max_nodes: int = 700, max_depth: int = 6,
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
            ids.append(pid + "(other)")
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


def treemap_fig(item, th=THEMES["dark"], privacy=False,
                scale=COLOR_SCALE) -> go.Figure:
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
            colorscale=scale,
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


def sunburst_fig(item, th=THEMES["dark"], privacy=False,
                 scale=COLOR_SCALE) -> go.Figure:
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
            colorscale=scale,
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


def filetype_fig(item, top: int = 15, th=THEMES["dark"],
                 scale=COLOR_SCALE) -> go.Figure:
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
        marker=dict(color=values, colorscale=scale,
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


def bar_fig(item, th=THEMES["dark"], privacy=False,
            scale=COLOR_SCALE) -> go.Figure:
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
            color=values, colorscale=scale,
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


def age_fig(item, th=THEMES["dark"], scale=COLOR_SCALE) -> go.Figure:
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
        marker=dict(color=colours, colorscale=scale, cmin=0, cmax=1,
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
