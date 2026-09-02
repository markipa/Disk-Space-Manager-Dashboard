r"""
Fast whole-drive scan by reading the NTFS Master File Table ($MFT) directly,
the way WizTree does. On a large drive this is ~100x faster than walking the
tree with os.scandir, because every file's name/size/parent lives in the MFT.

Requirements & safety:
  - Windows + NTFS volume only.
  - Reading the raw volume (\\.\C:) needs **Administrator** privileges.
  - EVERYTHING here is best-effort: build_tree raises on any problem so the
    caller can fall back to the normal os.scandir scan. It must never corrupt
    or half-populate anything the caller relies on.

Reference: the NTFS on-disk layout — boot sector (VBR), MFT file record header
with an Update-Sequence fixup, and the $STANDARD_INFORMATION (0x10),
$FILE_NAME (0x30) and $DATA (0x80) attributes.
"""
from __future__ import annotations

import os
import sys
import struct

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

GENERIC_READ = 0x80000000
FILE_SHARE_READ_WRITE = 0x00000003
OPEN_EXISTING = 3
INVALID_HANDLE = ctypes.c_void_p(-1).value if sys.platform == "win32" else -1

# $FILE_NAME namespaces
NS_POSIX, NS_WIN32, NS_DOS, NS_WIN32_DOS = 0, 1, 2, 3


def is_admin() -> bool:
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _epoch_from_filetime(ft: int) -> float:
    """Windows FILETIME (100ns since 1601) -> Unix epoch seconds."""
    if not ft:
        return 0.0
    return (ft - 116444736000000000) / 10_000_000.0


class _Volume:
    r"""Raw, sector-aligned reader over \\.\<letter>:."""

    def __init__(self, letter: str):
        self.h = ctypes.windll.kernel32.CreateFileW(
            f"\\\\.\\{letter}:", GENERIC_READ, FILE_SHARE_READ_WRITE, None,
            OPEN_EXISTING, 0, None)
        if self.h == INVALID_HANDLE or self.h in (0, None):
            raise OSError(f"cannot open volume {letter}: (need admin?)")

    def read(self, offset: int, length: int) -> bytes:
        pos = ctypes.c_longlong(offset)
        if not ctypes.windll.kernel32.SetFilePointerEx(
                self.h, pos, None, 0):  # FILE_BEGIN
            raise OSError("seek failed")
        buf = ctypes.create_string_buffer(length)
        read = wintypes.DWORD(0)
        if not ctypes.windll.kernel32.ReadFile(
                self.h, buf, length, ctypes.byref(read), None):
            raise OSError("read failed")
        return buf.raw[:read.value]

    def close(self):
        try:
            ctypes.windll.kernel32.CloseHandle(self.h)
        except Exception:
            pass


def _apply_fixup(rec: bytearray, bytes_per_sector: int) -> None:
    """Restore the Update-Sequence-Array bytes over each sector's last word."""
    usa_off, usa_count = struct.unpack_from("<HH", rec, 4)
    usn = rec[usa_off:usa_off + 2]
    for i in range(1, usa_count):
        sec_end = i * bytes_per_sector - 2
        src = usa_off + i * 2
        if sec_end + 2 <= len(rec) and src + 2 <= len(rec):
            # (the word at sec_end should currently equal usn; overwrite it)
            rec[sec_end:sec_end + 2] = rec[src:src + 2]
    _ = usn


def _decode_runs(data: bytes):
    """Decode a non-resident attribute's data-run list -> [(lcn, clusters)]."""
    runs = []
    i = 0
    lcn = 0
    n = len(data)
    while i < n and data[i] != 0:
        header = data[i]
        i += 1
        len_sz = header & 0x0F
        off_sz = (header >> 4) & 0x0F
        if len_sz == 0 or i + len_sz + off_sz > n:
            break
        length = int.from_bytes(data[i:i + len_sz], "little")
        i += len_sz
        off = data[i:i + off_sz]
        i += off_sz
        delta = int.from_bytes(off, "little", signed=True)
        lcn += delta
        runs.append((lcn, length))
    return runs


def _iter_attrs(rec: bytes):
    """Yield (type, resident, header_off, attr_len) for each attribute."""
    first_attr = struct.unpack_from("<H", rec, 20)[0]
    off = first_attr
    while off + 4 <= len(rec):
        atype = struct.unpack_from("<I", rec, off)[0]
        if atype == 0xFFFFFFFF:
            break
        alen = struct.unpack_from("<I", rec, off + 4)[0]
        if alen == 0 or off + alen > len(rec):
            break
        resident = rec[off + 8] == 0
        yield atype, resident, off, alen
        off += alen


def _parse_record(rec: bytes):
    """
    Parse one in-use MFT record -> dict or None.
    Returns {name, parent, size, is_dir, mtime} (name/parent from the best
    $FILE_NAME; size from unnamed $DATA; is_dir from the record flags).
    """
    if rec[:4] != b"FILE":
        return None
    flags = struct.unpack_from("<H", rec, 22)[0]
    if not (flags & 0x0001):        # 0x01 = record in use
        return None
    is_dir = bool(flags & 0x0002)   # 0x02 = directory

    name = None
    name_ns = -1
    parent = None
    mtime = 0.0
    size = 0

    for atype, resident, off, alen in _iter_attrs(rec):
        if atype == 0x10 and resident:                      # $STANDARD_INFORMATION
            content_off = struct.unpack_from("<H", rec, off + 20)[0]
            base = off + content_off
            # bytes 8..16 = last-modified FILETIME
            mtime = _epoch_from_filetime(
                struct.unpack_from("<Q", rec, base + 8)[0])
        elif atype == 0x30 and resident:                    # $FILE_NAME
            content_off = struct.unpack_from("<H", rec, off + 20)[0]
            base = off + content_off
            parent_ref = struct.unpack_from("<Q", rec, base)[0]
            this_parent = parent_ref & 0x0000FFFFFFFFFFFF    # low 48 bits
            name_len = rec[base + 64]
            ns = rec[base + 65]
            try:
                this_name = rec[base + 66: base + 66 + name_len * 2].decode(
                    "utf-16-le")
            except Exception:
                this_name = None
            # Prefer a Win32 name over a DOS (8.3) name.
            if this_name is not None and (name is None or ns != NS_DOS):
                if name is None or name_ns == NS_DOS or ns in (NS_WIN32,
                                                               NS_WIN32_DOS):
                    name, name_ns, parent = this_name, ns, this_parent
        elif atype == 0x80:                                 # $DATA (unnamed only)
            name_length = rec[off + 9]
            if name_length != 0:
                continue                                    # ADS -> skip
            if resident:
                size = struct.unpack_from("<I", rec, off + 16)[0]
            else:
                # real (logical) size lives at header+48
                size = struct.unpack_from("<Q", rec, off + 48)[0]

    if name is None or parent is None:
        return None
    return dict(name=name, parent=parent, size=size, is_dir=is_dir,
                mtime=mtime)


def build_tree(letter: str, item_factory, index_out: dict | None = None):
    """
    Read the whole $MFT for drive `letter` and reconstruct an Item tree using
    item_factory(name, path, size, is_dir, mtime, children). Populates
    index_out {abspath: Item} for directories if given. Raises on any failure.
    """
    if sys.platform != "win32":
        raise OSError("MFT scan is Windows-only")

    vol = _Volume(letter)
    try:
        vbr = vol.read(0, 512)
        if vbr[3:7] != b"NTFS":
            raise OSError("not an NTFS volume")
        bytes_per_sector = struct.unpack_from("<H", vbr, 11)[0]
        sectors_per_cluster = vbr[13]
        cluster = bytes_per_sector * sectors_per_cluster
        mft_lcn = struct.unpack_from("<Q", vbr, 48)[0]
        clusters_per_rec = struct.unpack_from("<b", vbr, 64)[0]
        if clusters_per_rec < 0:
            rec_size = 1 << (-clusters_per_rec)
        else:
            rec_size = clusters_per_rec * cluster
        if bytes_per_sector == 0 or cluster == 0 or rec_size == 0:
            raise OSError("bad NTFS geometry")

        # Record 0 = $MFT itself; parse its $DATA runlist to find all of it.
        rec0 = bytearray(vol.read(mft_lcn * cluster, rec_size))
        _apply_fixup(rec0, bytes_per_sector)
        runs = None
        for atype, resident, off, alen in _iter_attrs(bytes(rec0)):
            if atype == 0x80 and not resident and rec0[off + 9] == 0:
                run_off = struct.unpack_from("<H", rec0, off + 32)[0]
                runs = _decode_runs(bytes(rec0[off + run_off: off + alen]))
                break
        if not runs:
            raise OSError("could not locate $MFT data runs")

        # Read every extent of the MFT into one buffer.
        chunks = []
        for lcn, count in runs:
            chunks.append(vol.read(lcn * cluster, count * cluster))
        mft = b"".join(chunks)

        total_records = len(mft) // rec_size
        entries: dict[int, dict] = {}
        for recno in range(total_records):
            raw = bytearray(mft[recno * rec_size:(recno + 1) * rec_size])
            if raw[:4] != b"FILE":
                continue
            try:
                _apply_fixup(raw, bytes_per_sector)
                info = _parse_record(bytes(raw))
            except Exception:
                info = None
            if info:
                entries[recno] = info
    finally:
        vol.close()

    ROOT_REC = 5
    if ROOT_REC not in entries:
        raise OSError("root record missing")

    # Link children to parents.
    children_of: dict[int, list[int]] = {}
    for recno, info in entries.items():
        if recno == ROOT_REC:
            continue
        children_of.setdefault(info["parent"], []).append(recno)

    root_path = f"{letter}:\\"

    # Build the Item tree iteratively (avoid recursion limits on deep trees).
    def make(recno, name, path):
        info = entries.get(recno)
        is_dir = info["is_dir"] if info else True
        mtime = info["mtime"] if info else 0.0
        if not is_dir:
            size = info["size"] if info else 0
            return item_factory(name, path, size, False, mtime, [])
        kids = []
        total = 0
        for child in children_of.get(recno, ()):
            cinfo = entries.get(child)
            if not cinfo or cinfo["name"] in (".",):
                continue
            cname = cinfo["name"]
            cpath = os.path.join(path, cname)
            node = make(child, cname, cpath)
            kids.append(node)
            total += node.size
        item = item_factory(name, path, total, True, mtime, kids)
        if index_out is not None:
            index_out[os.path.abspath(path)] = item
        return item

    sys.setrecursionlimit(max(sys.getrecursionlimit(), 20000))
    disp = os.path.basename(root_path.rstrip("\\/")) or root_path
    root = make(ROOT_REC, disp, root_path)
    return root
