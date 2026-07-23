#!/usr/bin/env python3
"""案件工作区控制台 — 启动入口"""
import os, sys, time, threading, subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from dashboard import app as flask_app

PORT = 5080

def _free_port():
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{PORT}"],
            capture_output=True, text=True, timeout=5,
        )
        pids = result.stdout.strip().split()
        for pid in pids:
            if pid:
                subprocess.run(["kill", "-9", pid], capture_output=True)
        if pids:
            time.sleep(0.6)
    except Exception:
        pass

def _start_flask():
    # threaded=True：语义索引/脱敏等耗时请求在独立线程处理，避免阻塞其余 UI 请求
    # （单线程默认下，一次慢的建索引会让切案件/开菜单等全部卡住"半天没反应"）
    flask_app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False, threaded=True)

def _activate_app():
    """Called by webview after the window is ready — bring it to front."""
    time.sleep(0.3)

    # Method 1: AppKit direct — no Automation permission needed
    try:
        import AppKit
        AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        return
    except Exception:
        pass

    # Method 2: osascript via PID
    pid = os.getpid()
    r = subprocess.run(
        ["osascript", "-e",
         f'tell application "System Events" to set frontmost of first process whose unix id is {pid} to true'],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        return

    # Method 3: activate Python by name
    subprocess.run(
        ["osascript", "-e", 'tell application "Python" to activate'],
        capture_output=True,
    )

def _open_fallback():
    """Browser fallback when pywebview is unavailable or fails."""
    subprocess.run(["open", f"http://localhost:{PORT}"])
    print(f"浏览器访问: http://localhost:{PORT}")
    print("按 Ctrl+C 停止服务")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("已停止。")

if __name__ == "__main__":
    print(f"[1/3] 清理端口 {PORT}...")
    _free_port()

    print(f"[2/3] 启动后端服务...")
    t = threading.Thread(target=_start_flask, daemon=True)
    t.start()
    time.sleep(1.2)

    print(f"[3/3] 打开控制台窗口...")
    try:
        import webview

        storage = Path(__file__).parent / ".webview_storage"
        storage.mkdir(exist_ok=True)

        window = webview.create_window(
            "Legal Auto Drive",
            f"http://localhost:{PORT}",
            width=1280,
            height=820,
            min_size=(900, 600),
        )
        # func= runs in a background thread after the window is fully ready
        webview.start(func=_activate_app, private_mode=False, storage_path=str(storage))
        print("窗口已关闭。")

    except ImportError:
        print("未安装 pywebview，在浏览器中打开...")
        _open_fallback()
    except Exception as e:
        print(f"pywebview 启动失败 ({e})，改用浏览器...")
        _open_fallback()
