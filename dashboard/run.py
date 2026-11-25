#!/usr/bin/env python3
"""案件工作区控制台 — 启动入口。"""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from werkzeug.serving import make_server

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))
from dashboard.dashboard import app as flask_app

HOST = "127.0.0.1"
PORT = 5080
URL = f"http://{HOST}:{PORT}"


def _close_server(server, server_thread):
    """Stop the serving loop and release the bound port."""
    try:
        server.shutdown()
    finally:
        server_thread.join(timeout=5)
        server.server_close()


def _activate_app():
    """Called by webview after the window is ready — bring it to front."""
    time.sleep(0.3)

    # Method 1: AppKit direct — no Automation permission needed.
    try:
        import AppKit
        AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        return
    except Exception:
        pass

    # Method 2: osascript via PID.
    pid = os.getpid()
    r = subprocess.run(
        ["osascript", "-e",
         f'tell application "System Events" to set frontmost of first process whose unix id is {pid} to true'],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        return

    # Method 3: activate Python by name.
    subprocess.run(
        ["osascript", "-e", 'tell application "Python" to activate'],
        capture_output=True,
    )


def _open_fallback():
    """Open the already-bound server in a browser and wait for Ctrl+C.

    Returning ``False`` when the browser cannot be opened prevents the caller
    from reporting a successful launch for a service the user cannot reach.
    """
    try:
        import webbrowser
        opened = webbrowser.open(URL)
    except Exception as exc:
        print(f"❌ 浏览器回退失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return False
    if not opened:
        print(f"❌ 浏览器未能打开 {URL}，服务未就绪。", file=sys.stderr)
        return False

    print(f"浏览器已打开: {URL}")
    print("按 Ctrl+C 停止服务")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("已停止。")
    return True


def main():
    # make_server binds synchronously.  Do not create a GUI window or open a
    # browser until this call has succeeded; an occupied port is fail-closed.
    try:
        server = make_server(HOST, PORT, flask_app, threaded=True)
    except OSError as exc:
        print(f"❌ 控制台无法绑定 {URL}，服务未启动：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"❌ 控制台服务启动失败，服务未启动：{type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1

    server_thread = threading.Thread(
        target=server.serve_forever,
        name="dashboard-http-server",
        daemon=True,
    )
    try:
        server_thread.start()
    except Exception as exc:
        server.server_close()
        print(f"❌ 控制台服务线程启动失败，服务未启动：{type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1

    try:
        try:
            import webview
        except ImportError:
            print("未安装 pywebview，在浏览器中打开…")
            return 0 if _open_fallback() else 1

        try:
            storage = Path(__file__).parent / ".webview_storage"
            storage.mkdir(exist_ok=True)
            webview.create_window(
                "Legal Auto Drive",
                URL,
                width=1280,
                height=820,
                min_size=(900, 600),
            )
            # webview.start() returns after the window is closed.
            webview.start(
                func=_activate_app,
                private_mode=False,
                storage_path=str(storage),
            )
            print("窗口已关闭。")
            return 0
        except Exception as exc:
            print(f"pywebview 启动失败 ({exc})，改用浏览器…", file=sys.stderr)
            return 0 if _open_fallback() else 1
    except KeyboardInterrupt:
        print("\n控制台已退出")
        return 0
    finally:
        _close_server(server, server_thread)


if __name__ == "__main__":
    raise SystemExit(main())
