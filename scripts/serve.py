#!/usr/bin/env python3
"""本地预览用的静态服务器。

为什么需要它：页面用 fetch() 读取 web/data/*.json，而浏览器的同源策略会拦截
file:// 下的 fetch。双击打开 index.html 只会看到"加载失败"提示
（除非导出了内联包 data/data.js）。

用法：
    python scripts/serve.py            # http://127.0.0.1:8000
    python scripts/serve.py --port 8080 --open
"""

from __future__ import annotations

import argparse
import functools
import http.server
import os
import socketserver
import sys
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"


class Handler(http.server.SimpleHTTPRequestHandler):
    """加上 no-store，避免改了 JSON 之后浏览器还拿旧数据。"""

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:  # 安静一点
        if "404" in (fmt % args):
            sys.stderr.write("  404 %s\n" % (args[0] if args else ""))


def main() -> int:
    parser = argparse.ArgumentParser(description="预览 web/ 静态站点")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = parser.parse_args()

    if not WEB_DIR.exists():
        print(f"找不到 {WEB_DIR}", file=sys.stderr)
        return 1

    data_dir = WEB_DIR / "data"
    if not data_dir.exists() or not (data_dir / "index.json").exists():
        print("警告：web/data/index.json 不存在，页面会显示加载失败。")
        print("      请先运行：python scripts/run_all.py")
        print()

    handler = functools.partial(Handler, directory=str(WEB_DIR))
    with socketserver.TCPServer((args.host, args.port), handler) as httpd:
        url = f"http://{args.host}:{args.port}/"
        print(f"服务目录: {WEB_DIR}")
        print(f"预览地址: {url}")
        print("按 Ctrl+C 停止")
        if args.open:
            webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
