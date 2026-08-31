"""
Desktop launcher for the Disk Space Dashboard.

Runs the Dash app in a background thread and shows it in a native window using
pywebview — which borrows the operating system's built-in web engine (Edge
WebView2 on Windows, WebKit on macOS/Linux) instead of bundling a whole browser.
That keeps the app small: no Electron, no Chromium, one language (Python).

Dev:      python desktop.py
Packaged: a single windowed executable (see disk_dashboard.spec).
"""

import sys

# Folder-picker child process: file_dashboard's own top-level guard runs the Tk
# dialog and exits. Handle it here first so the (heavier) app never spins up.
if "--pick-folder" in sys.argv:
    import file_dashboard  # noqa: F401  (its import-time guard does the work)
    sys.exit(0)

import os
import socket
import threading

import webview
import file_dashboard as fd


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_until_up(port: int, timeout: float = 30.0) -> bool:
    """Block until the Dash server accepts connections (or timeout)."""
    end = threading.Event()
    deadline = timeout
    step = 0.1
    while deadline > 0:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.25):
                return True
        except OSError:
            end.wait(step)
            deadline -= step
    return False


def main() -> None:
    port = int(os.environ.get("PORT", "0")) or _free_port()

    def serve():
        # use_reloader=False: never fork a second process inside the thread.
        fd.app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

    threading.Thread(target=serve, daemon=True).start()
    _wait_until_up(port)

    webview.create_window(
        "Disk Space Dashboard",
        f"http://127.0.0.1:{port}",
        width=1440,
        height=900,
        min_size=(1000, 640),
        background_color="#1e1e1e",
    )
    webview.start()


if __name__ == "__main__":
    main()
