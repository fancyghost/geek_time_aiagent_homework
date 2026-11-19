"""可选的本地静态服务器（仅标准库）。

index.html 通过 <script src="data/traces.js"> 加载数据，双击（file://）即可运行；
若希望以 http:// 方式打开（便于浏览器工具/录屏），运行本脚本：

    python serve.py            # 默认 http://127.0.0.1:8770
    python serve.py --port 9000
"""
from __future__ import annotations

import argparse
import http.server
import os
import socketserver
import threading
import webbrowser
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(description="Trace Observatory 本地服务器")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    os.chdir(Path(__file__).resolve().parent)

    class Handler(http.server.SimpleHTTPRequestHandler):
        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def log_message(self, fmt, *a):  # 精简日志
            pass

    socketserver.TCPServer.allow_reuse_address = True
    url = f"http://127.0.0.1:{args.port}/index.html"
    with socketserver.TCPServer(("127.0.0.1", args.port), Handler) as httpd:
        print(f"[serve] Trace Observatory 已启动：{url}")
        print("[serve] 按 Ctrl+C 停止。")
        if not args.no_open:
            threading.Timer(0.7, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[serve] 已停止。")


if __name__ == "__main__":
    main()