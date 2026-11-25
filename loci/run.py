#!/usr/bin/env python3
"""Loci — 启动入口"""
import os
import platform
import sys
import subprocess
import threading
from pathlib import Path

from werkzeug.serving import make_server

sys.path.insert(0, str(Path(__file__).parent))
if __name__ == "__main__":
    # Set this before importing the Flask application so all startup-created
    # files, worker outputs and child processes inherit the private mask.
    os.umask(0o077)

from app import _prepare_startup_security, app as flask_app

HOST = "127.0.0.1"
PORT = 5081


def _running_under_rosetta():
    """在 Apple Silicon 上阻止用 x86_64 Python 启动本地原生依赖。"""
    if sys.platform != "darwin" or platform.machine() != "x86_64":
        return False
    try:
        result = subprocess.run(
            ["sysctl", "-in", "sysctl.proc_translated"],
            capture_output=True, text=True, timeout=2,
        )
        return result.stdout.strip() == "1"
    except (OSError, subprocess.SubprocessError):
        return False


def _close_server(server, server_thread):
    """Stop the serving loop and release the bound port."""
    try:
        server.shutdown()
    finally:
        server_thread.join(timeout=5)
        server.server_close()


def main():
    if _running_under_rosetta():
        print(
            "❌ 当前 Loci 正在 Rosetta/x86_64 Python 下运行。\n"
            "   本机 PDF、OCR 等原生依赖是 arm64，继续启动会导致 PDF 读取失败。\n"
            "   请关闭当前进程，先运行 setup.sh，再使用 .venv/bin/python loci/run.py 启动。",
            file=sys.stderr,
        )
        return 1
    try:
        _prepare_startup_security()
        # make_server binds synchronously.  Do not create a GUI window until
        # this call has succeeded, otherwise the window can show a connection
        # error while another process owns the port.
        server = make_server(HOST, PORT, flask_app, threaded=True)
    except OSError as exc:
        print(f"❌ Loci 无法绑定 http://{HOST}:{PORT}，服务未启动：{exc}",
              file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"❌ Loci 服务启动失败，服务未启动：{type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1

    server_thread = threading.Thread(
        target=server.serve_forever,
        name="loci-http-server",
        daemon=True,
    )
    try:
        server_thread.start()
    except Exception as exc:
        server.server_close()
        print(f"❌ Loci 服务线程启动失败，服务未启动：{type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1

    try:
        try:
            import webview
        except ImportError:
            import webbrowser
            print("提示: 安装 pywebview 可作为独立 Mac 窗口运行")
            print("      pip3 install pywebview")
            print(f"\n已在浏览器中打开 Loci → http://{HOST}:{PORT}")
            webbrowser.open(f"http://{HOST}:{PORT}")
            while server_thread.is_alive():
                server_thread.join()
        else:
            webview.create_window(
                "Loci",
                f"http://{HOST}:{PORT}",
                width=1140,
                height=740,
                min_size=(860, 580),
                # pywebview defaults text_select to False.  The desensitization
                # workbench needs native text selection for manual补标.
                text_select=True,
            )
            # pywebview.start() returns after the window is closed.
            webview.start()
    except KeyboardInterrupt:
        print("\nLoci 已退出")
        return 0
    except Exception as exc:
        print(f"❌ Loci 窗口启动失败：{type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1
    finally:
        _close_server(server, server_thread)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
