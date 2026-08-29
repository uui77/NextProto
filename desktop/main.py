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


# ---------------------------------------------------------------------------
# Windows 打包 (PyInstaller) + bleak 兼容性修复
#
# 现象：打包后扫描 / 连接 BLE 时报
#   BleakError: Thread is configured for Windows GUI but callbacks are not working.
#              Suspect unwanted side effects from importing 'pythoncom'.
#
# 根因：PyInstaller 的 pywin32 hook / _pywin32_bootstrap 会在程序入口前
# 调用 pythoncom.CoInitialize(COINIT_APARTMENTTHREADED) 把主线程 COM 模型
# 设为 STA。而 bleak 的 winrt 后端需要 COINIT_MULTITHREADED (MTA)，
# 否则 SetTimer 回调依赖的消息泵不运行，导致 0.5s 超时后抛上述 BleakError。
#
# 修复：在任何业务代码（尤其 bleak / winsdk 导入）之前，手动撤销 STA
# 初始化并重新以 MTA 模式初始化 COM。随后从 sys.modules 中移除 pythoncom，
# 避免 bleak 的错误提示里把它列为"可疑副作用"（实际已经是 MTA 了）。
# ---------------------------------------------------------------------------
def _fix_com_threading_model() -> None:
    if not sys.platform.startswith("win"):
        return
    try:
        # 用 ctypes 直接调用 COM API，避免 import pythoncom 带来的副作用
        import ctypes
        from ctypes import wintypes

        ole32 = ctypes.WinDLL("ole32", use_last_error=True)
        COINIT_MULTITHREADED = 0x0
        RPC_E_CHANGED_MODE = -2147417850  # 0x80010106

        # 1) 先尝试卸载旧的 COM 初始化（不管之前是什么模式，CoUninitialize
        #    可以被重复调用，直到返回 S_FALSE 说明栈已清空）。
        #    注意：pythoncom 每次 CoInitialize 会让引用计数+1，必须清空才
        #    能换模式。
        max_loops = 16
        while max_loops > 0:
            hr = ole32.CoUninitialize()
            if hr == 1:  # S_FALSE：还有其他未卸载的引用，继续
                max_loops -= 1
                continue
            break

        # 2) 用 MTA 模式重新初始化
        hr = ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
        if hr == RPC_E_CHANGED_MODE:
            # 本线程 COM 模型已固定且不是 MTA——这种情况理论上在上面
            # CoUninitialize 清空栈后不会发生，只做日志记录。
            logging.getLogger(__name__).warning(
                "COM 模型无法切换到 MTA，BLE 可能会失败（请检查是否有其他"
                "入口代码提前锁死了 STA 模型）。")
        elif hr < 0:
            logging.getLogger(__name__).debug(
                "CoInitializeEx(MTA) 返回 0x%08X（非致命，bleak 会再自行初始化）", hr)
    except Exception as exc:
        logging.getLogger(__name__).debug("COM 模型修复跳过（非致命）: %s", exc)

    # 3) 尝试从 sys.modules 移除 pythoncom，让 bleak 的错误信息更准确
    #    （这里只是从字典移除，dll 实际上仍然被加载；只要线程在 MTA 就没问题）
    for _mod in ("pythoncom", "pywin32_bootstrap", "pywin32_system32"):
        sys.modules.pop(_mod, None)


_fix_com_threading_model()


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
