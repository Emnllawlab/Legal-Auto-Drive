#!/usr/bin/env python3
"""Loci — 启动入口"""
import sys, time, threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from app import app as flask_app

PORT = 5081

def _start_flask():
    flask_app.run(port=PORT, debug=False, use_reloader=False)

if __name__ == "__main__":
    t = threading.Thread(target=_start_flask, daemon=True)
    t.start()
    time.sleep(0.9)

    try:
        import webview
        window = webview.create_window(
            "Legal Auto Drive",
            f"http://localhost:{PORT}",
            width=1140,
            height=740,
            min_size=(860, 580),
        )
        webview.start()
    except ImportError:
        import webbrowser
        print(f"提示: 安装 pywebview 可作为独立 Mac 窗口运行")
        print(f"      pip3 install pywebview")
        print(f"\n已在浏览器中打开 Loci → http://localhost:{PORT}")
        webbrowser.open(f"http://localhost:{PORT}")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nLoci 已退出")
