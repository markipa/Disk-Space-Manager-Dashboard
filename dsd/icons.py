"""Windows shell icons: the real Explorer icon for each file type, extracted
once per extension and cached as PNG bytes. Served as same-origin URLs
(/icon/<key>) because the DataTable markdown renderer blocks data: image URIs.
Windows only; any failure -> b"" / "" (no icon)."""
import os
import sys

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

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


def icon_png_for_key(key: str) -> bytes:
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
    return f"/icon/{key}" if icon_png_for_key(key) else ""
