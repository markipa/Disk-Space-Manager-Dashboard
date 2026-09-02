"""Duplicate-file finder. Pure: takes a scanned Item tree, reads files off disk
to hash them, returns groups of byte-identical files. `Item` is duck-typed."""
from __future__ import annotations

import hashlib


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


def find_duplicates(item, min_size: int = 1_048_576) -> list[list]:
    """
    Find groups of byte-identical files at/under `item` (size >= min_size).

    Staged for performance: bucket by exact size, then by a 64 KB quick hash,
    then confirm with a full hash. Returns groups (>= 2 files) sorted by wasted
    space (size * (copies - 1)) descending.
    """
    by_size: dict[int, list] = {}
    stack = [item]
    while stack:
        cur = stack.pop()
        for c in cur.children:
            if c.is_dir:
                stack.append(c)
            elif c.size >= min_size:
                by_size.setdefault(c.size, []).append(c)

    groups: list[list] = []
    for size, items in by_size.items():
        if len(items) < 2:
            continue
        by_quick: dict[str, list] = {}
        for it in items:
            qh = _file_hash(it.path, full=False)
            if qh is not None:
                by_quick.setdefault(qh, []).append(it)
        for quick_group in by_quick.values():
            if len(quick_group) < 2:
                continue
            by_full: dict[str, list] = {}
            for it in quick_group:
                fh = _file_hash(it.path, full=True)
                if fh is not None:
                    by_full.setdefault(fh, []).append(it)
            for full_group in by_full.values():
                if len(full_group) >= 2:
                    groups.append(full_group)

    groups.sort(key=lambda g: g[0].size * (len(g) - 1), reverse=True)
    return groups
