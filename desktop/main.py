"""CB08 录音笔处理程序入口。
用法（源码运行）：
    python main.py            # 启动命令行交互
    python main.py -o out     # 指定下载目录
    python main.py -v         # 输出调试日志
    python main.py --web      # 启动 Web 界面（需 requirements-web.txt）
    python main.py --web --port 8000

打包后（exe / .app）运行：
    双击即自动启动 Web 服务 + 打开浏览器。
"""
import argparse
import logging
import sys
import webbrowser
from pathlib import Path


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def _resolve_output_dir(output_arg: str) -> Path:
    """源码：项目下的 downloads；打包后：exe 同级的 downloads。"""
    if _is_frozen():
        base = Path(sys.executable).resolve().parent
        return base / output_arg
    return Path(output_arg)


def main() -> None:
    parser = argparse.ArgumentParser(description="CB08 录音笔控制台")
    parser.add_argument("-o", "--output", default="downloads",
                        help="下载文件保存目录（默认 downloads）")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="输出调试日志")
    parser.add_argument("--web", action="store_true",
                        help="启动 Web 界面而非命令行 REPL（打包 exe 模式默认为 True）")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Web 监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8000,
                        help="Web 监听端口（默认 8000）")
    parser.add_argument("--no-auto-open", action="store_true",
                        help="打包 exe 运行时，不要自动打开浏览器（源码模式默认不开）")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    output_dir = _resolve_output_dir(args.output)

    # ============ 打包后的 exe：默认走 web + 自动打开浏览器 ============
    force_web = args.web or _is_frozen()
    auto_open = _is_frozen() and not args.no_auto_open

    if force_web:
        from recorder.web import run_server
        run_server(output_dir, host=args.host, port=args.port,
                   auto_open_browser=auto_open)
    else:
        import asyncio
        from recorder.cli import Cli
        try:
            asyncio.run(Cli(output_dir).run())
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
