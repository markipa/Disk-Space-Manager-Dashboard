"""Core-logic tests for the Disk Space Dashboard (no GUI/server needed).

Run:  python -m pytest -q
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import file_dashboard as fd


# ----------------------------- helpers ------------------------------------- #

def test_human_size():
    assert fd.human_size(0) == "0.0 B"
    assert fd.human_size(1024) == "1.0 KB"
    assert fd.human_size(1024 ** 3).endswith("GB")


def test_mask_name():
    assert fd.mask_name("secret.pdf", False, True) == "•••••.pdf"
    assert fd.mask_name("Folder", True, True) == "•••••"
    assert fd.mask_name("keep.txt", False, False) == "keep.txt"


def test_fmt_iso_sorts_chronologically():
    a = fd.fmt_iso(time.mktime((2024, 1, 1, 0, 0, 0, 0, 0, -1)))
    b = fd.fmt_iso(time.mktime((2026, 6, 1, 0, 0, 0, 0, 0, -1)))
    assert a < b                      # ISO strings sort as dates
    assert fd.fmt_iso(0) == ""


def test_usage_color_thresholds():
    assert fd._usage_color(0.5) == "#3CAEA3"     # green
    assert fd._usage_color(0.80) == "#F6D55C"    # amber
    assert fd._usage_color(0.95) == "#ED553B"    # red


def test_palette_scale():
    assert fd.palette_scale("Viridis") == "Viridis"
    assert fd.palette_scale("nonexistent") == fd.COLOR_SCALE


# ----------------------------- scanner ------------------------------------- #

def _make_tree(root):
    os.makedirs(os.path.join(root, "sub"), exist_ok=True)
    with open(os.path.join(root, "a.txt"), "wb") as f:
        f.write(b"x" * 1000)
    with open(os.path.join(root, "sub", "b.bin"), "wb") as f:
        f.write(b"y" * 2000)
    # a duplicate of a.txt
    with open(os.path.join(root, "sub", "a_copy.txt"), "wb") as f:
        f.write(b"x" * 1000)


def test_scan_sizes_and_mtime(tmp_path):
    _make_tree(str(tmp_path))
    tree = fd.scan_directory(str(tmp_path))
    assert tree.size == 4000                      # 1000 + 2000 + 1000
    assert tree.is_dir
    names = {c.name for c in tree.children}
    assert "a.txt" in names and "sub" in names
    a = next(c for c in tree.children if c.name == "a.txt")
    assert a.size == 1000 and a.mtime > 0


def test_remove_item_updates_ancestor_sizes(tmp_path):
    _make_tree(str(tmp_path))
    tree = fd.scan_directory(str(tmp_path))
    before = tree.size
    a = next(c for c in tree.children if c.name == "a.txt")
    assert fd.remove_item(a.path) is True
    assert fd.INDEX[fd.ROOT_PATH].size == before - 1000


def test_find_duplicates(tmp_path):
    _make_tree(str(tmp_path))
    fd.scan_directory(str(tmp_path))
    node = fd.INDEX[fd.ROOT_PATH]
    groups = fd.find_duplicates(node, min_size=100)
    # a.txt and sub/a_copy.txt are identical 1000-byte files
    assert any(len(g) == 2 and g[0].size == 1000 for g in groups)


# --------------------------- figures / table ------------------------------- #

def test_figures_build(tmp_path):
    _make_tree(str(tmp_path))
    tree = fd.scan_directory(str(tmp_path))
    th = fd.theme("dark")
    for fig in (fd.treemap_fig(tree, th), fd.sunburst_fig(tree, th),
                fd.bar_fig(tree, th), fd.filetype_fig(tree, th=th),
                fd.age_fig(tree, th)):
        assert fig is not None and len(fig.data) >= 1


def test_fill_table_search_recurses(tmp_path):
    _make_tree(str(tmp_path))
    fd.scan_directory(str(tmp_path))
    cp = fd.ROOT_PATH
    direct = fd.fill_table(cp, 0, "", 0, "all", False)
    found = fd.fill_table(cp, 0, ".bin", 0, "all", False)  # nested file
    assert len(direct) == 2                    # a.txt + sub
    assert any("b.bin" in r["name"] for r in found)  # recursive search hit


def test_list_drives_nonempty():
    drives = fd.list_drives()
    assert len(drives) >= 1
    root, total, used, free = drives[0]
    assert total > 0 and used + free <= total + total * 0.01
