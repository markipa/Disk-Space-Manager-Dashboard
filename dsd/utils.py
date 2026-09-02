"""Pure helpers, colour palettes and themes — no state, no Dash, no I/O."""
import os
import datetime


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

# Selectable size colour-scales. Values are anything Plotly accepts for
# marker.colorscale (a list of stops, or a built-in name). Small -> big.
PALETTES = {
    "Sunset (default)": COLOR_SCALE,
    "Ocean": [[0.0, "#0b2545"], [0.5, "#3a7ebf"], [1.0, "#8ecae6"]],
    "Viridis": "Viridis",
    "Plasma": "Plasma",
    "Turbo": "Turbo",
    "Cividis": "Cividis",
    "Blues": "Blues",
    "Greens": "Greens",
    "Hot": "Hot",
}
DEFAULT_PALETTE = "Sunset (default)"


def palette_scale(name):
    return PALETTES.get(name, COLOR_SCALE)


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


def usage_color(frac: float) -> str:
    """Green under 75% used, amber under 90%, red beyond — cleanup pressure."""
    if frac >= 0.90:
        return "#ED553B"
    if frac >= 0.75:
        return "#F6D55C"
    return "#3CAEA3"
