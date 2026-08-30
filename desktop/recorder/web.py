"""Web 界面后端（FastAPI）。

在浏览器中提供与命令行 REPL 等价的全部功能：
    python main.py --web [--host 127.0.0.1] [--port 8000]

可选功能，需先安装：pip install -r requirements-web.txt
架构：单例 Recorder（一条 BLE 连接）+ REST 命令接口 + WebSocket 推送
（下载进度 / 实时码流字节数 / 机身按键事件 / 日志）。

注意：本模块不可使用 `from __future__ import annotations` —— 字符串化注解
会让 FastAPI 无法解析 create_app 闭包内导入的 Request 类型（表现为 422）。
"""

import asyncio
import logging
import os
import re
import struct
import sys
import time
import zlib
from pathlib import Path
from typing import List, Optional

from . import asr
from . import llm as LLM
from . import protocol as P
from .cli import (GAIN_NAMES, RESULT_NAMES, RT_STATE_NAMES, STATE_NAMES,
                  fmt_duration, fmt_file_time, fmt_size)
from .device import Recorder, RecorderError
from .protocol import FileEntry

logger = logging.getLogger(__name__)


# ------------------------------------------------ 路径工具：支持日期子目录 + 防穿越


def _resolve_safe_name(output_dir: Path, name: str) -> Path:
    """把前端传来的 name（可能带日期子目录，如 "2026-08-15/xxx.wav"）
    解析为 output_dir 内的安全绝对路径。

    禁止：空名、绝对路径、含 .. 的目录穿越。
    """
    if not name:
        raise RecorderError("缺少文件 name")
    # 规范化：转反斜杠、去掉开头的斜杠
    clean = name.replace("\\", "/").lstrip("/")
    # 绝对路径（Windows 含盘符 C:）直接拒绝
    if Path(clean).is_absolute() or (len(clean) >= 2 and clean[1] == ":"):
        raise RecorderError("文件路径非法")
    # 任何一段含 ".." 都拒绝
    for part in clean.split("/"):
        if part in ("", ".", ".."):
            raise RecorderError("文件路径非法（禁止空段或目录穿越）")
    target = (output_dir / clean).resolve()
    base = output_dir.resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise RecorderError("文件路径越界")
    return target


def _iter_local_files(output_dir: Path):
    """遍历 output_dir（含日期子目录），返回音频等文件的相对路径。

    生成 (abs_path, rel_name) 元组，按 mtime 倒序。
    """
    # 双重保险：先把 base 归一化成绝对路径，避免 Path.relative_to 以
    # "downloads" 作相对 base 时在 Windows 抛
    # "does not start with 'downloads'"。
    base_dir = Path(output_dir).resolve()
    if not base_dir.exists():
        return
    candidates = []
    for p in base_dir.rglob("*"):
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(base_dir).as_posix()
        except ValueError:
            continue
        # 跳过隐藏文件（.llm_config.json 等）
        if rel.startswith("."):
            continue
        candidates.append((p, rel))
    candidates.sort(key=lambda t: t[0].stat().st_mtime, reverse=True)
    for item in candidates:
        yield item


def _today_folder() -> str:
    """今天的日期文件夹名，如 2026-08-15。"""
    import datetime as _dt
    return _dt.date.today().strftime("%Y-%m-%d")


def _resolve_web_dir() -> Path:
    """解析前端静态目录。

    打包后（PyInstaller），资源在 sys._MEIPASS/web。
    源码运行时：相对 recorder/web.py 的 ../web。
    """
    # PyInstaller 运行时：解压的临时目录（onedir 模式下等价于 _internal）
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "web"
    # onedir 的 _internal 路径（新版 PyInstaller 放在可执行文件旁的 _internal 里）
    exe_dir = Path(sys.executable).resolve().parent
    if exe_dir.name == "录音笔控制台" and (exe_dir / "_internal" / "web").is_dir():
        return exe_dir / "_internal" / "web"
    return Path(__file__).resolve().parents[1] / "web"


def _resolve_ffmpeg_dir() -> Optional[Path]:
    """解析打包后 ffmpeg/ffprobe 所在目录（存在就注入 PATH）。"""
    candidates = []
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        candidates.append(Path(sys._MEIPASS) / "ffmpeg")
    exe_dir = Path(sys.executable).resolve().parent
    if exe_dir.name == "录音笔控制台":
        candidates.append(exe_dir / "_internal" / "ffmpeg")
        candidates.append(exe_dir / "ffmpeg")
    for p in candidates:
        exe_suffix = ".exe" if sys.platform.startswith("win") else ""
        if (p / f"ffmpeg{exe_suffix}").is_file() and (p / f"ffprobe{exe_suffix}").is_file():
            return p
    return None


def _inject_ffmpeg_path() -> None:
    """将打包内的 ffmpeg 目录加到 PATH，pydub/funasr 能找到它。"""
    ffmpeg_dir = _resolve_ffmpeg_dir()
    if not ffmpeg_dir:
        return
    # 作为 PATH 第一个搜索项，优先使用
    ffmpeg_str = str(ffmpeg_dir)
    cur_path = os.environ.get("PATH", "")
    if ffmpeg_str not in cur_path.split(os.pathsep):
        os.environ["PATH"] = ffmpeg_str + os.pathsep + cur_path
        logger.info("已注入 ffmpeg 目录到 PATH：%s", ffmpeg_dir)


WEB_DIR = _resolve_web_dir()
_inject_ffmpeg_path()


_INSTALL_HINT = ("未安装 fastapi/uvicorn，Web 界面不可用；请先执行 "
                 "pip install -r requirements-web.txt")


# ================================================================= favicon.ico
def _crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xffffffff


def _png_chunk(ctype: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + ctype + data
            + struct.pack(">I", _crc32(ctype + data)))


def _make_png_32x32() -> bytes:
    """纯标准库生成一张 32x32 RGBA PNG：苹果蓝渐变+圆角+白麦克风+红点。"""
    W = H = 32
    # 颜色（RGBA）
    BG_TOP = (10, 132, 255, 255)
    BG_BOT = (0, 96, 223, 255)
    WHITE = (255, 255, 255, 255)
    RED = (255, 59, 48, 255)
    TRANSPARENT = (0, 0, 0, 0)

    def in_rounded(x, y):
        """圆角半径 7（32*14/64≈7）的矩形内部判定。"""
        r = 7
        if x < r and y < r:
            return (r - x) ** 2 + (r - y) ** 2 <= r * r
        if x >= W - r and y < r:
            return (x - (W - r - 1)) ** 2 + (r - y) ** 2 <= r * r
        if x < r and y >= H - r:
            return (r - x) ** 2 + (y - (H - r - 1)) ** 2 <= r * r
        if x >= W - r and y >= H - r:
            return (x - (W - r - 1)) ** 2 + (y - (H - r - 1)) ** 2 <= r * r
        return True

    def in_mic(x, y):
        """麦克风主体：胶囊（圆+方+圆）近似。"""
        cx = 16
        top = 6        # 12/64*32 = 6
        bot = 20       # 40/64*32 = 20
        half = 4       # 16/64*32/2 = 4
        if not (cx - half <= x <= cx + half - 1):
            return False
        if top <= y <= bot:
            # 检查顶部和底部半圆
            if y < top + half:
                dy = y - top
                dx = abs(x - cx)
                return dx * dx + dy * dy <= half * half
            if y > bot - half:
                dy = bot - y
                dx = abs(x - cx)
                return dx * dx + dy * dy <= half * half
            return True
        return False

    def in_stem(x, y):
        """底座杆。"""
        return 15 <= x <= 16 and 20 <= y <= 24

    def in_stands(x, y):
        """左右声波支架：以 (16,17) 为中心的两截圆环。"""
        cx, cy = 16, 17
        dx, dy = x - cx, y - cy
        d2 = dx * dx + dy * dy
        ro = 9      # 18/64*32/2
        ri = 7      # 14/64*32/2
        if not (ri * ri <= d2 <= ro * ro):
            return False
        # 角度：左侧 110~160 度，右侧 20~70 度（度=atan2*180/pi）
        import math
        ang = math.degrees(math.atan2(dy, dx))
        # 注意：y 向下为正，翻转一下
        ang = -ang
        # 左侧：110~160；右侧：20~70 （对称处理：取 abs）
        ang_a = abs(ang)
        if 20 <= ang_a <= 70:
            return True
        return False

    def in_red_dot(x, y):
        cx, cy = 16, 13
        r = 2
        return (x - cx) ** 2 + (y - cy) ** 2 <= r * r

    raw = bytearray()
    for y in range(H):
        raw.append(0)   # PNG filter: None
        for x in range(W):
            if not in_rounded(x, y):
                c = TRANSPARENT
            elif in_red_dot(x, y):
                c = RED
            elif in_mic(x, y) or in_stem(x, y):
                c = WHITE
            elif in_stands(x, y):
                c = WHITE
            else:
                t = y / max(1, H - 1)
                c = (
                    int(BG_TOP[0] * (1 - t) + BG_BOT[0] * t),
                    int(BG_TOP[1] * (1 - t) + BG_BOT[1] * t),
                    int(BG_TOP[2] * (1 - t) + BG_BOT[2] * t),
                    255,
                )
            raw.extend(c)
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", W, H, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(raw), 9)
    return (sig
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", idat)
            + _png_chunk(b"IEND", b""))


def _make_ico_from_png(png_bytes: bytes, size: int = 32) -> bytes:
    """把一个 PNG 字节串包装成合法的 ICO 文件（单尺寸入口）。"""
    # ICONDIR: reserved(2) + type(2=1) + count(2)
    dir_hdr = struct.pack("<HHH", 0, 1, 1)
    # ICONDIRENTRY: w(1), h(1), colors(1), reserved(1), planes(2), bpp(2),
    #               bytesInRes(4), imageOffset(4)
    w = size if size < 256 else 0
    h = size if size < 256 else 0
    entry_size = struct.calcsize("<BBBBHHII")
    offset = struct.calcsize("<HHH") + entry_size
    entry = struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(png_bytes), offset)
    return dir_hdr + entry + png_bytes


def _ensure_favicon() -> None:
    """确保 WEB_DIR 下存在 favicon.ico；不存在则用标准库生成。"""
    target = WEB_DIR / "favicon.ico"
    if target.exists() and target.stat().st_size > 200:
        return
    try:
        png = _make_png_32x32()
        ico = _make_ico_from_png(png, 32)
        target.write_bytes(ico)
        logger.info("自动生成 favicon.ico：%s", target)
    except Exception as e:
        logger.warning("生成 favicon.ico 失败：%s", e)





def create_app(output_dir: Path):
    try:
        from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
        from fastapi.responses import JSONResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as exc:
        raise RuntimeError(_INSTALL_HINT) from exc

    # 自动生成 favicon.ico（不存在时）
    _ensure_favicon()

    app = FastAPI(title="QS668 录音笔 Web 控制台")
    recorder = Recorder(output_dir=Path(output_dir))
    # 强制把 output_dir 归一化为绝对路径（resolve）：
    #   - 修复 relative_to 抛出 "does not start with 'downloads'"：
    #     当 recorder.output_dir 未 resolve（相对 Path('downloads')），而遍历文件路径被
    #     resolve 过，Path.relative_to 会以"相对字符串作 base"的形式抛 ValueError。
    #   - 同时保证所有 _resolve_safe_name / rename / archive / delete 中路径比较一致。
    recorder.output_dir = recorder.output_dir.resolve()
    S = {"devices": [], "files": []}   # 扫描结果 / 文件列表缓存
    sockets: set = set()
    op_lock = asyncio.Lock()           # 下载/转写/巡检等耗时操作互斥
    rt_state = {"bytes": 0, "ts": 0.0}
    prog_state = {"ts": 0.0}

    # ------------------------------------------------ WebSocket 推送

    async def _send(ws, payload: dict) -> None:
        try:
            await ws.send_json(payload)
        except Exception:
            sockets.discard(ws)

    def broadcast(payload: dict) -> None:
        """同步回调里调度异步群发（回调总在事件循环内触发）。"""
        if not sockets:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for ws in list(sockets):
            loop.create_task(_send(ws, payload))

    def log_push(level: str, text: str) -> None:
        broadcast({"type": "log", "level": level, "text": text})

    # ------------------------------------------------ Recorder 回调

    def on_progress(received: int, expected: int) -> None:
        now = time.monotonic()
        # 节流：进度最快 0.15s 推一次，避免刷爆 WebSocket
        if received and expected and received < expected \
                and now - prog_state["ts"] < 0.15:
            return
        prog_state["ts"] = now
        broadcast({"type": "progress", "received": received,
                   "expected": expected})

    def on_realtime(event: str, payload) -> None:
        if event == "audio":
            rt_state["bytes"] += len(payload)
            now = time.monotonic()
            if now - rt_state["ts"] < 0.2:
                return
            rt_state["ts"] = now
            broadcast({"type": "realtime", "event": "bytes",
                       "value": rt_state["bytes"],
                       "text": fmt_size(rt_state["bytes"])})
        elif event == "filename":
            broadcast({"type": "realtime", "event": "filename",
                       "value": str(payload)})
        elif event == "state":
            broadcast({"type": "realtime", "event": "state",
                       "value": RT_STATE_NAMES.get(payload, str(payload))})

    def on_device_event(frame) -> None:
        names = {P.KEY_REC_START: "开始录音", P.KEY_REC_SAVE: "保存录音",
                 P.KEY_REC_PAUSE: "暂停录音", P.KEY_REC_RESUME: "继续录音",
                 P.KEY_STATE_RESP: "录音状态"}
        desc = names.get(frame.cmd, f"cmd={frame.cmd}") \
            if frame.type == P.TYPE_KEY else f"type={frame.type} cmd={frame.cmd}"
        broadcast({"type": "device_event", "desc": desc,
                   "body": frame.body.hex(" ") or "-"})

    recorder.on_progress = on_progress
    recorder.on_realtime = on_realtime
    recorder.on_device_event = on_device_event

    # ------------------------------------------------ 工具函数

    async def read_json(req) -> dict:
        try:
            body = await req.json()
            return body if isinstance(body, dict) else {}
        except Exception:
            return {}

    def require_connected() -> None:
        if not recorder.is_connected:
            raise RecorderError("尚未连接设备，请先扫描并连接")

    def busy_guard() -> None:
        if op_lock.locked():
            raise RecorderError("有耗时操作正在进行，请稍候")

    def entry_by_index(idx) -> FileEntry:
        if not isinstance(idx, int) or not (0 <= idx < len(S["files"])):
            raise RecorderError("无效文件序号，请先刷新文件列表")
        return S["files"][idx]

    def file_json(i: int, f: FileEntry) -> dict:
        return {"index": i, "name": f.name, "size": f.size,
                "size_text": fmt_size(f.size),
                "time_text": fmt_file_time(f.duration)}

    def local_path_for(entry: FileEntry) -> Path:
        """与下载落盘一致的本地预期路径（用于转写复用已下载文件）。"""
        safe = re.sub(r'[\\/:*?"<>|]', "_",
                      entry.candidate_names()[0]).rstrip(". ")
        return recorder.output_dir / safe

    def download_json(result) -> dict:
        wav = None
        if result.is_wav and result.wav_info is not None:
            w = result.wav_info
            wav = {"ok": w.ok, "declared": w.declared,
                   "sample_rate": w.sample_rate, "bits": w.bits_per_sample,
                   "channels": w.channels}
        return {"filename": result.filename, "size": len(result.data),
                "size_text": fmt_size(len(result.data)),
                "local_name": result.path.name if result.path else None,
                "url": f"/downloads/{result.path.name}" if result.path else None,
                "is_wav": result.is_wav, "wav": wav,
                "converted_from": result.converted_from}

    # ------------------------------------------------ 异常统一处理

    @app.exception_handler(RecorderError)
    async def _recorder_error(_req, exc):
        return JSONResponse(status_code=400, content={"error": str(exc)})

    @app.exception_handler(asyncio.TimeoutError)
    async def _timeout_error(_req, _exc):
        return JSONResponse(status_code=504,
                            content={"error": "等待设备应答超时"})

    @app.exception_handler(Exception)
    async def _any_error(req: Request, exc):
        """兜底：捕获全部未处理异常。

        后端：logger.exception 完整记录 traceback 到控制台/日志文件。
        前端：
          - 已知友好型错误：直接把友好文字放在 HTTP 500 JSON.error 里，
            由前端 api() → run() catch 统一打一次（避免重复 3 遍）。
          - 未知错误：不再 mask 成"通用废话"，把「异常类型 + 消息摘要」
            返回给前端（最长 ~300 字），便于定位。同时仍推 WS 日志。
        """
        logger.exception("Unhandled error on %s", req.url.path)

        # -------- 1) 已知友好型 RuntimeError（由 ble._describe_ble_error 产出） --------
        if isinstance(exc, RuntimeError) and any(
                k in str(exc) for k in ("蓝牙", "适配器", "请先", "请在", "请确认")):
            msg = str(exc)
            return JSONResponse(status_code=500, content={"error": msg})

        # -------- 2) 匹配到的 OSError（Windows 蓝牙相关 winerror 等） --------
        if isinstance(exc, OSError):
            win_err = getattr(exc, "winerror", None)
            if win_err is not None:
                msg = (f"系统调用失败（错误码 {win_err}）。"
                       "如与蓝牙相关，请先在 Windows 设置中打开蓝牙并重试。")
            else:
                msg = f"系统调用失败：{exc}"
            return JSONResponse(status_code=500, content={"error": msg})

        # -------- 3) 其他未知错误：暴露类型+消息（截断到 320 字） --------
        import typing as _typing
        type_name = type(exc).__name__
        raw_msg = str(exc).strip() or "（异常无消息）"
        # 单条换行压成空格，避免前端日志错乱
        flat = raw_msg.replace("\r\n", " ").replace("\n", " ")
        if len(flat) > 300:
            flat = flat[:300] + "…"
        # 缺失依赖的统一友好提示（前端也可匹配）
        missing_dep_hint: dict[str, str] = {
            "ModuleNotFoundError": "缺少 Python 依赖。请在 venv/环境中执行 pip install 对应包后重启服务。",
            "ImportError":       "模块导入失败。请检查依赖版本或在 venv 中重新安装。",
        }
        extra = missing_dep_hint.get(type_name, "")
        msg = f"{type_name}: {flat}"
        if extra:
            msg = f"[DEP] {msg}。{extra}"
        # WS 推送完整消息（含 path），与之前的风格一致
        log_push("ERR", f"{req.url.path} 失败：{msg}")
        return JSONResponse(status_code=500, content={"error": msg})

    # ------------------------------------------------ 连接管理

    @app.get("/api/status")
    async def api_status():
        connected = recorder.is_connected
        t = recorder.transport
        return {"connected": connected,
                "mtu": t.mtu if connected else None,
                "payload": t.payload_size if connected else None,
                "asr_ready": asr.is_loaded(),
                "asr_options": {
                    "models": {k: {"label": v["label"],
                                   "languages": list(v["languages"])}
                               for k, v in asr.MODELS.items()},
                    "languages": list(asr.LANGUAGES),
                },
                "files": [file_json(i, f)
                          for i, f in enumerate(S["files"])]}

    @app.post("/api/scan")
    async def api_scan(req: Request):
        body = await read_json(req)
        timeout = float(body.get("timeout") or 6.0)
        compat = bool(body.get("compat"))
        # 先快速检查一下 bleak 是否能访问适配器，给出更明确的错误
        try:
            from bleak import BleakScanner
        except Exception as exc:
            raise RecorderError(f"bleak 蓝牙库不可用：{exc}") from exc
        S["devices"] = await recorder.scan(timeout, compat=compat)
        return [{"index": i, "name": d.name or "(无名称)",
                 "address": d.address}
                for i, d in enumerate(S["devices"])]

    def _resolve_device_from_body(body):
        target = body.get("target")
        if isinstance(target, int):
            if not (0 <= target < len(S["devices"])):
                raise RecorderError("无效设备序号，请重新扫描")
            return S["devices"][target]
        if isinstance(target, str) and target.strip():
            return target.strip()   # bleak 支持直接传地址
        raise RecorderError("缺少 target（扫描序号或 MAC 地址）")

    @app.post("/api/pair")
    async def api_pair(req: Request):
        """单独发起系统级蓝牙配对（QS668/CB08 常见 PIN：0000 / 1234）。

        body: { target } — 同 /api/connect
        返回: { paired: bool, settings_opened: bool, address: "...", note: "..." }
        """
        body = await read_json(req)
        busy_guard()
        device = _resolve_device_from_body(body)
        addr = getattr(device, "address", str(device))
        try:
            result = await recorder.pair(device)
        except asyncio.TimeoutError:
            # 被全局捕获器处理成「等待设备应答超时」——友好度不够，手动兜
            log_push("ERR", "配对超时：系统弹窗未在 20 秒内确认")
            return JSONResponse(status_code=504, content={
                "error": "配对超时：请在弹出的系统「添加设备」对话框里确认"
                         "配对（录音笔配对码通常是 0000 或 1234），然后再试。",
                "paired": False,
            })
        settings_opened = (result == "settings_opened")
        if result is True:
            log_push("OK", f"配对成功：{addr}，现在可直接点「连接」")
            note = "配对成功"
        elif settings_opened:
            log_push("INFO",
                     f"当前蓝牙环境不支持程序化自动配对（正常现象）——已帮你跳转到"
                     f" Windows「添加设备」向导，请在向导里选中录音笔并输入配对码 0000/1234，"
                     f"完成后回到本页面点「连接」。")
            note = ("已跳转 Windows 添加设备向导（没弹出来就按 Win+I → 蓝牙和其他设备"
                    " → 添加设备 → 选中录音笔 → 输入配对码 0000 或 1234），"
                    "配完再点本页面左侧的设备名/地址即可秒连。")
        else:
            log_push("INFO",
                     f"当前后端不支持程序化配对，请去 Windows 设置里手动配对：{addr}")
            note = "不支持程序化配对，按 Win+I → 蓝牙和其他设备手动添加。"
        return {"paired": bool(result is True),
                "settings_opened": settings_opened,
                "note": note,
                "address": addr}

    @app.post("/api/connect")
    async def api_connect(req: Request):
        body = await read_json(req)
        device = _resolve_device_from_body(body)
        auto_pair = bool(body.get("auto_pair"))
        addr = getattr(device, "address", str(device))

        # 如果用户要求自动配对：先尝试 pair 一次（失败不终止 connect 流程）
        if auto_pair and not recorder.is_connected:
            try:
                result = await recorder.pair(device)
                if result is True:
                    log_push("INFO", f"自动配对完成：{addr}；给设备 5 秒冷却后连接")
                    await asyncio.sleep(5.0)   # 配对完成（尤其 bleak cross-platform pair 会
                                              # 临时 connect→disconnect）后设备会 bond-reset，
                                              # 给 5s 避免"GATT services: Unreachable"
                elif result == "settings_opened":
                    log_push("WARN", "自动配对已跳转 Windows「添加设备」向导——请先在系统里"
                                     "完成配对（PIN 0000/1234），配完后再手动点连接。")
                    return JSONResponse(status_code=428, content={
                        "error": "请先在系统弹出的「添加设备」向导里完成蓝牙配对（录音笔配对码 0000 或 1234）"
                                 "，配对完成后回到此页面直接点左侧「设备名/地址」按钮即可连接。",
                        "settings_opened": True,
                    })
            except Exception as exc:
                logger.info("自动配对未完成（继续尝试连接）：%s", exc)

        # 连接进度推送到前端（从 MTA worker 线程 call_soon_threadsafe 回主线程）
        _main_loop = asyncio.get_running_loop()
        def _on_connect_progress(msg):
            _main_loop.call_soon_threadsafe(log_push, "INFO", msg)
        recorder.transport.on_connect_progress = _on_connect_progress
        await recorder.connect(device)
        synced = True
        try:
            await recorder.sync_time()   # 连接后自动同步时间（0-0）
        except Exception:
            synced = False
        log_push("OK", f"设备已连接，MTU={recorder.transport.mtu}")
        return {"connected": True, "mtu": recorder.transport.mtu,
                "payload": recorder.transport.payload_size,
                "time_synced": synced}

    @app.post("/api/disconnect")
    async def api_disconnect():
        await recorder.disconnect()
        log_push("INFO", "设备已断开")
        return {"connected": False}

    # ------------------------------------------------ 设备信息

    @app.get("/api/info")
    async def api_info():
        require_connected()
        battery = await recorder.get_battery()
        remain, total = await recorder.get_capacity()
        version = await recorder.get_version()
        return {"battery": "充电中" if battery == P.BATTERY_CHARGING
                else f"{battery}%",
                "capacity": f"剩余 {fmt_size(remain * 1024)} / "
                            f"共 {fmt_size(total * 1024)}",
                "version": version}

    @app.post("/api/synctime")
    async def api_synctime():
        require_connected()
        await recorder.sync_time()
        return {"message": "已按本机时间同步"}

    @app.post("/api/smoke")
    async def api_smoke():
        require_connected()
        busy_guard()
        results = []
        async with op_lock:
            async def get_files():
                S["files"] = await recorder.get_file_list()
                return S["files"]
            steps = [
                ("电量", recorder.get_battery,
                 lambda v: "充电中" if v == P.BATTERY_CHARGING else f"{v}%"),
                ("容量", recorder.get_capacity,
                 lambda v: f"剩余 {fmt_size(v[0] * 1024)} / "
                           f"共 {fmt_size(v[1] * 1024)}"),
                ("固件", recorder.get_version, str),
                ("授权码", recorder.get_auth_code, lambda v: v.hex(" ")),
                ("录音状态", recorder.record_state,
                 lambda v: STATE_NAMES.get(v, str(v))),
                ("录音时间", recorder.record_time,
                 lambda v: f"{fmt_duration(v[0])} / {fmt_size(v[1])}"),
                ("当前文件名", recorder.record_filename, str),
                ("增益", recorder.get_gain,
                 lambda v: GAIN_NAMES.get(v, str(v))),
                ("文件列表", get_files, lambda v: f"{len(v)} 个文件"),
            ]
            for label, coro_fn, render in steps:
                try:
                    value = await coro_fn()
                    results.append({"label": label, "value": render(value),
                                    "ok": True})
                except asyncio.TimeoutError:
                    results.append({"label": label, "value": "无应答（超时）",
                                    "ok": False})
                except Exception as exc:
                    results.append({"label": label, "value": f"失败 {exc}",
                                    "ok": False})
                await asyncio.sleep(0.26)   # 命令间隔，避免固件应接不暇
        return results

    # ------------------------------------------------ 文件操作

    @app.get("/api/files")
    async def api_files():
        require_connected()
        busy_guard()
        async with op_lock:
            S["files"] = await recorder.get_file_list()
        return [file_json(i, f) for i, f in enumerate(S["files"])]

    @app.post("/api/download")
    async def api_download(req: Request):
        require_connected()
        body = await read_json(req)
        busy_guard()
        entry = entry_by_index(body.get("index"))
        offset = int(body.get("offset") or 0)
        filename = body.get("filename") or None
        dl_name = filename or entry.candidate_names()[0]
        broadcast({"type": "download_start", "filename": dl_name,
                    "expected": entry.size if not dl_name.lower().endswith(".wav")
                    else entry.estimated_wav_size})
        async with op_lock:
            result = await recorder.download(entry, offset=offset,
                                             filename=filename)
        on_progress(len(result.data), len(result.data))   # 收尾推 100%
        # Opus 自动转 WAV（下载体积小速度快，本地用 ffmpeg 转）
        if not result.is_wav and result.path and \
                result.filename.lower().endswith(".opus"):
            try:
                raw_head = result.data[:16].hex()
                log_push("INFO", f"Opus→WAV 转换中（头部: {raw_head}，"
                                 f"大小: {fmt_size(len(result.data))}）")
                loop = asyncio.get_running_loop()
                wav_path = await loop.run_in_executor(
                    None, recorder._convert_opus_to_wav,
                    result.path, entry)
                result.converted_from = "opus"
                result.data = wav_path.read_bytes()
                result.path = wav_path
                result.is_wav = True
                result.wav_info = P.inspect_wav(result.data)
                log_push("OK", f"Opus→WAV 转换完成：{wav_path.name} "
                                f"({fmt_size(len(result.data))})")
            except Exception as exc:
                log_push("WARN", f"Opus→WAV 转换失败：{exc}，保留原始 Opus 文件")
        return download_json(result)

    @app.post("/api/segment")
    async def api_segment(req: Request):
        require_connected()
        body = await read_json(req)
        busy_guard()
        entry = entry_by_index(body.get("index"))
        start, end = int(body.get("start", 0)), int(body.get("end", 0))
        if not (0 <= start < end):
            raise RecorderError("字节范围无效（需 0 <= start < end）")
        async with op_lock:
            result = await recorder.download_segment(entry, start, end)
        return download_json(result)

    @app.post("/api/abort")
    async def api_abort():
        require_connected()
        await recorder.abort_download()
        return {"message": "已发送 2-7 终止导入"}

    @app.post("/api/delete")
    async def api_delete(req: Request):
        require_connected()
        body = await read_json(req)
        busy_guard()
        async with op_lock:
            entry = entry_by_index(body.get("index"))
            code = await recorder.delete_file(entry)
        if code is None:
            msg = "删除命令已发送（该固件不回应答），请刷新列表核对"
        elif code == 0:
            msg = "删除成功"
        else:
            msg = f"删除失败（code={code}），请刷新列表核对"
        return {"message": msg}

    @app.post("/api/deleteall")
    async def api_deleteall():
        require_connected()
        busy_guard()
        async with op_lock:
            code = await recorder.delete_all()
        if code is None:
            msg = "删除命令已发送（该固件不回应答），请刷新列表核对"
        elif code == 0:
            msg = "全部删除成功"
        else:
            msg = f"删除失败（code={code}），请刷新列表核对"
        return {"message": msg}

    @app.post("/api/convert_raw_opus")
    async def api_convert_raw_opus(req: Request):
        """把以前实时录音写出来的"裸 40B raw OPUS"（无法播放）转成合法 Ogg Opus。

        body: { "name": "相对 output_dir 文件名", "replace": bool }
        replace=False → 输出为 {stem}.oggified.opus（不碰原文件）
        replace=True  → 原文件重命名备份为 *.raw_opus_backup，成品覆盖原文件名
        """
        body = await read_json(req)
        busy_guard()
        name = str(body.get("name") or "")
        if not name:
            raise RecorderError("缺少 name")
        replace = bool(body.get("replace", False))
        try:
            result = recorder.convert_raw_opus_to_ogg(name, replace=replace)
        except FileNotFoundError as exc:
            raise RecorderError(str(exc)) from exc
        return result

    # ------------------------------------------------ 语音转文字

    @app.post("/api/transcribe")
    async def api_transcribe(req: Request):
        body = await read_json(req)
        busy_guard()
        language = str(body.get("language") or "auto")
        model = str(body.get("model") or "sensevoice").lower()
        spk = bool(body.get("spk"))
        volume_gain = str(body.get("volume_gain") or "auto")
        if model not in asr.MODELS:
            raise RecorderError(f"model 须为 {'/'.join(asr.MODELS.keys())}")
        valid_langs = asr.MODELS[model]["languages"]
        if language not in valid_langs:
            language = "auto"
        async with op_lock:
            if body.get("index") is not None:
                entry = entry_by_index(body.get("index"))
                path = local_path_for(entry)
                if path.exists():
                    log_push("INFO", f"复用已下载文件：{path.name}")
                else:
                    require_connected()
                    log_push("INFO", f"下载 {entry.name} ...")
                    result = await recorder.download(entry)
                    path = result.path
            else:
                # 支持带日期子目录的 name，防目录穿越
                name_raw = str(body.get("name") or "")
                path = _resolve_safe_name(recorder.output_dir, name_raw)
                if not path.is_file():
                    raise RecorderError("本地文件不存在")
            # 相对于 output_dir 的路径（含子目录），用于前后端一致地引用文件
            rel_name = path.relative_to(recorder.output_dir).as_posix()
            if not asr.is_loaded():
                spec = asr.MODELS[model]
                log_push("INFO", f"加载识别模型（{spec['label']}，"
                                 f"首次使用会自动下载，请耐心等待）...")
            label = (f"{asr.MODELS[model]['label'].split('（')[0]}，"
                     f"spk={'开' if spk else '关'}，lang={language}")
            log_push("INFO", f"转写中（{label}）...")
            try:
                result = await asr.transcribe_file(
                    path, language=language, model=model, spk=spk,
                    volume_gain=volume_gain)
                text = result.get("text", "") if isinstance(result, dict) else (result or "")
                segments = result.get("segments") if isinstance(result, dict) else None
                spk_mode = result.get("spk_mode") if isinstance(result, dict) else "off"
            except asr.AsrNotAvailable as exc:
                raise RecorderError(str(exc)) from exc
        txt_path = path.with_suffix(".txt")
        txt_path.write_text((text or "") + "\n", encoding="utf-8")
        # 结构化元数据：source/txt 都用相对路径便于跨目录迁移
        meta_path = path.with_suffix(".transcript.json")
        txt_rel = txt_path.relative_to(recorder.output_dir).as_posix()
        try:
            import json as _json
            meta_path.write_text(_json.dumps({
                "text": text or "",
                "segments": segments or [],
                "txt": txt_rel,
                "source": rel_name,
                "model": model, "spk": spk, "spk_mode": spk_mode,
                "language": language,
                "saved_at": int(__import__("time").time() * 1000),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("写入转写元数据失败：%s", exc)
        return {"text": text or "", "segments": segments or [],
                "txt": txt_rel,
                "source": rel_name,
                "model": model, "spk": spk, "spk_mode": spk_mode,
                "language": language}

    @app.post("/api/save_transcript")
    async def api_save_transcript(req: Request):
        """保存用户编辑后的转写文本，覆盖 .txt 文件。同时更新 .transcript.json。"""
        body = await read_json(req)
        name_raw = str(body.get("name") or "")
        text = str(body.get("text") or "")
        segments = body.get("segments")
        path = _resolve_safe_name(recorder.output_dir, name_raw)
        if not path.is_file():
            raise RecorderError("源文件不存在")
        rel_name = path.relative_to(recorder.output_dir).as_posix()
        txt_path = path.with_suffix(".txt")
        txt_rel = txt_path.relative_to(recorder.output_dir).as_posix()
        txt_path.write_text(text + ("\n" if not text.endswith("\n") else ""), encoding="utf-8")
        meta_path = path.with_suffix(".transcript.json")
        try:
            import json as _json
            if meta_path.is_file():
                try:
                    meta = _json.loads(meta_path.read_text(encoding="utf-8"))
                except Exception:
                    meta = {}
            else:
                meta = {}
            meta["text"] = text or ""
            meta["txt"] = txt_rel
            meta["source"] = rel_name
            if isinstance(segments, list) and segments:
                meta["segments"] = segments
            meta["edited_at"] = int(__import__("time").time() * 1000)
            meta_path.write_text(_json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("更新转写元数据失败：%s", exc)
        return {"ok": True, "txt": txt_rel, "length": len(text)}

    @app.post("/api/transcript")
    async def api_get_transcript(req: Request):
        """读取已存在的转写结果（.txt + .transcript.json），不重新转写。"""
        body = await read_json(req)
        name_raw = str(body.get("name") or "")
        path = _resolve_safe_name(recorder.output_dir, name_raw)
        if not path.is_file():
            raise RecorderError("源文件不存在")
        rel_name = path.relative_to(recorder.output_dir).as_posix()
        txt_path = path.with_suffix(".txt")
        txt_rel = txt_path.relative_to(recorder.output_dir).as_posix() if txt_path.is_file() else f"{Path(rel_name).stem}.txt"
        meta_path = path.with_suffix(".transcript.json")
        text = ""
        if txt_path.is_file():
            text = txt_path.read_text(encoding="utf-8").rstrip("\n")
        segments = []
        model = ""
        spk = False
        spk_mode = "off"
        language = "auto"
        if meta_path.is_file():
            try:
                import json as _json
                meta = _json.loads(meta_path.read_text(encoding="utf-8"))
                segments = meta.get("segments") or []
                model = meta.get("model") or ""
                spk = bool(meta.get("spk"))
                spk_mode = meta.get("spk_mode") or ("campplus" if spk else "off")
                language = meta.get("language") or "auto"
                if not text:
                    text = meta.get("text") or ""
            except Exception:
                pass
        return {"text": text or "", "segments": segments or [],
                "txt": txt_rel,
                "source": rel_name,
                "model": model, "spk": spk, "spk_mode": spk_mode,
                "language": language,
                "cached": True}

    @app.post("/api/delete_local")
    async def api_delete_local(req: Request):
        """删除本地文件（含对应的 .txt / .transcript.json / .summary.json / .md）。"""
        body = await read_json(req)
        name_raw = str(body.get("name") or "")
        path = _resolve_safe_name(recorder.output_dir, name_raw)
        if not path.is_file():
            raise RecorderError("文件不存在")
        rel = path.relative_to(recorder.output_dir).as_posix()
        parent = path.parent
        stem = path.stem
        suffix = path.suffix.lower()
        deleted = []
        # 主文件
        path.unlink(missing_ok=True)
        deleted.append(rel)
        # 同目录下的附属文件（统一 stem + 多种扩展名）
        for ext in [".txt", ".transcript.json", ".summary.json", ".mindmap.md", ".md"]:
            # .transcript.json 需要特殊处理：先去原 stem 再加 .transcript.json
            if ext == ".transcript.json":
                cand = parent / f"{stem}.transcript.json"
            elif ext == ".mindmap.md":
                cand = parent / f"{stem}.mindmap.md"
            else:
                cand = parent / f"{stem}{ext}"
            try:
                cand_rel = cand.relative_to(recorder.output_dir).as_posix()
            except ValueError:
                continue
            if cand.is_file() and cand_rel not in deleted:
                cand.unlink(missing_ok=True)
                deleted.append(cand_rel)
        return {"ok": True, "deleted": deleted}

    @app.post("/api/rename_local")
    async def api_rename_local(req: Request):
        """重命名本地文件（同目录附属文件一起改名）。"""
        body = await read_json(req)
        name_raw = str(body.get("name") or "")
        new_name = str(body.get("new_name") or "").strip()
        path = _resolve_safe_name(recorder.output_dir, name_raw)
        if not path.is_file():
            raise RecorderError("文件不存在")
        if not new_name:
            raise RecorderError("缺少 new_name")
        # 清理非法字符（不允许斜杠——重命名不能改目录）
        import re as _re
        new_base = _re.sub(r'[\\/:*?"<>|]', "_", new_name).rstrip(". ")
        if not new_base:
            raise RecorderError("新文件名不合法")
        parent = path.parent
        suffix = path.suffix
        stem = path.stem
        if "." not in new_base:
            new_main_base = new_base + suffix
        else:
            new_main_base = new_base
        new_path = parent / new_main_base
        if new_path.exists() and new_path.resolve() != path.resolve():
            raise RecorderError("同名文件已存在")
        rel_old = path.relative_to(recorder.output_dir).as_posix()
        rel_new = new_path.relative_to(recorder.output_dir).as_posix()
        renamed = []
        # 1) 主文件
        path.rename(new_path)
        renamed.append([rel_old, rel_new])
        new_stem = Path(new_main_base).stem
        # 2) 同目录附属文件：.txt / .transcript.json / .summary.json / .mindmap.md / .md
        for ext in [".txt", ".transcript.json", ".summary.json", ".mindmap.md", ".md"]:
            if ext == ".transcript.json":
                old_p = parent / f"{stem}.transcript.json"
                new_p = parent / f"{new_stem}.transcript.json"
            elif ext == ".mindmap.md":
                old_p = parent / f"{stem}.mindmap.md"
                new_p = parent / f"{new_stem}.mindmap.md"
            else:
                old_p = parent / f"{stem}{ext}"
                new_p = parent / f"{new_stem}{ext}"
            if old_p.is_file():
                if new_p.resolve() == old_p.resolve():
                    continue
                if new_p.exists():
                    new_p.unlink(missing_ok=True)
                old_p.rename(new_p)
                try:
                    renamed.append([
                        old_p.relative_to(recorder.output_dir).as_posix(),
                        new_p.relative_to(recorder.output_dir).as_posix(),
                    ])
                except ValueError:
                    pass
        return {"ok": True, "renamed": renamed, "new_name": rel_new}

    @app.post("/api/open_folder")
    async def api_open_folder(req: Request):
        """在资源管理器里打开文件所在目录（Windows），未指定则打开 output_dir。"""
        body = await read_json(req)
        name = body.get("name")
        target = None
        if isinstance(name, str) and name:
            try:
                cand = _resolve_safe_name(recorder.output_dir, name)
                if cand.is_file():
                    target = cand
            except RecorderError:
                target = None
        if target is not None:
            try:
                import subprocess
                subprocess.Popen(["explorer", "/select,", str(target)])
                return {"ok": True}
            except Exception as exc:
                raise RecorderError(f"打开失败：{exc}")
        try:
            import os, subprocess
            os.startfile(str(recorder.output_dir))  # type: ignore[attr-defined]
            return {"ok": True}
        except Exception as exc:
            raise RecorderError(f"打开失败：{exc}")

    # ------------------------------------------------ 音频剪辑
    @app.post("/api/clip_audio")
    async def api_clip_audio(req: Request):
        """剪辑音频：截取 [start_ms, end_ms] 导出到同目录下的新文件。"""
        body = await read_json(req)
        name_raw = str(body.get("name") or "")
        start_ms = int(body.get("start_ms") or 0)
        end_ms = int(body.get("end_ms") or 0)
        out_name = str(body.get("out_name") or "").strip()
        src = _resolve_safe_name(recorder.output_dir, name_raw)
        if not src.is_file():
            raise RecorderError("源文件不存在")
        if end_ms <= start_ms:
            raise RecorderError("结束时间必须大于开始时间")
        duration_ms = end_ms - start_ms
        parent = src.parent
        stem = src.stem
        suffix = src.suffix
        if not out_name:
            out_base = f"{stem}_clip_{start_ms//1000}s-{end_ms//1000}s{suffix}"
        else:
            import re as _re
            out_base = _re.sub(r'[\\/:*?"<>|]', "_", out_name).rstrip(". ")
            if not out_base:
                raise RecorderError("输出文件名不合法")
            if "." not in out_base:
                out_base = out_base + suffix
        out_path = parent / out_base
        if out_path.resolve() == src.resolve():
            raise RecorderError("输出文件名不能与源文件相同")
        out_rel = out_path.relative_to(recorder.output_dir).as_posix()
        try:
            from pydub import AudioSegment
        except ImportError:
            raise RecorderError("缺少 pydub，无法剪辑音频")
        try:
            audio = AudioSegment.from_file(str(src))
            if end_ms > len(audio):
                end_ms = len(audio)
            clipped = audio[start_ms:end_ms]
            fmt = suffix.lstrip(".") if suffix.lstrip(".") in \
                ("wav", "mp3", "flac", "ogg", "m4a") else "wav"
            clipped.export(str(out_path), format=fmt)
        except Exception as exc:
            raise RecorderError(f"剪辑失败：{exc}")
        logger.info("音频剪辑完成：%s [%d-%dms] -> %s (%dms)",
                    src.name, start_ms, end_ms, out_rel, duration_ms)
        return {
            "ok": True,
            "out_name": out_rel,
            "duration_ms": duration_ms,
            "size_text": fmt_size(out_path.stat().st_size),
        }

    @app.get("/api/local")
    async def api_local():
        """列出本地文件（含日期子目录）。返回的 name 都是相对路径（含子目录）。"""
        out = []
        # 两轮：先全部收集，再计算"已转写/已摘要"徽章（避免跨目录 stem 混淆）
        all_items = []   # [(abs_path, rel_name, stem_with_folder, suffix)]
        txt_keys = set()          # "dir/stem" 已经有 .txt
        meta_keys = set()         # "dir/stem" 已经有 .transcript.json
        summary_keys = set()      # "dir/stem" 已经有 .summary.json
        md_keys = set()           # "dir/stem" 已经有 .md
        AUDIO_KINDS = ("wav", "opus", "mp3", "m4a", "flac")
        for p, rel in _iter_local_files(recorder.output_dir):
            parent_rel = str(Path(rel).parent.as_posix()) if "/" in rel else ""
            suffix = p.suffix.lower().lstrip(".")
            # --- 附属文件：只登记 key，不入列表（不展示） ---
            if rel.endswith(".transcript.json"):
                # special: {stem}.transcript.json — 真实 stem 是 Path(p.stem).stem
                inner_stem = Path(p.stem).stem  # 去掉 .json 再去掉 .transcript
                key = f"{parent_rel}/{inner_stem}" if parent_rel else inner_stem
                meta_keys.add(key)
                continue
            if suffix == "txt":
                key = f"{parent_rel}/{p.stem}" if parent_rel else p.stem
                txt_keys.add(key)
                continue  # 不展示 txt 条目（附属文件）
            if suffix == "json" and p.name.endswith(".summary.json"):
                inner_stem = Path(p.stem).stem  # .summary -> stem
                key = f"{parent_rel}/{inner_stem}" if parent_rel else inner_stem
                summary_keys.add(key)
                continue  # 不展示 summary.json 条目
            if p.name.endswith(".mindmap.md"):
                # 思维导图用户编辑大纲（附属文件，不单独展示）
                inner_stem = p.name[:-len(".mindmap.md")]
                key = f"{parent_rel}/{inner_stem}" if parent_rel else inner_stem
                md_keys.add(key)  # 也算作"已导 MD"徽章的一部分
                continue
            if suffix == "md":
                key = f"{parent_rel}/{p.stem}" if parent_rel else p.stem
                md_keys.add(key)
                continue  # 不展示 md 条目（用户反馈"多出一个 MD 没必要"）
            # --- 其余类型：只保留音频（wav/opus/mp3/m4a/flac），其他一律丢弃 ---
            if suffix not in AUDIO_KINDS:
                continue
            stem = p.stem
            key_prefix = f"{parent_rel}/" if parent_rel else ""
            all_items.append((p, rel, key_prefix, stem, suffix))
        for (p, rel, key_prefix, stem, suffix) in all_items:
            key_base = key_prefix + stem
            item = {
                "name": rel,
                "display_name": p.name,
                "folder": key_prefix.rstrip("/"),  # "2026-08-15" 或 ""
                "size_text": fmt_size(p.stat().st_size),
                "url": f"/downloads/{rel}",
                "kind": suffix,
                "mtime": int(p.stat().st_mtime * 1000),
            }
            # 现在 all_items 里只剩音频，has_* 统一给
            has_txt_v = key_base in txt_keys
            has_meta_v = key_base in meta_keys
            item["has_txt"] = has_txt_v
            item["has_meta"] = has_meta_v
            item["has_transcript"] = has_txt_v or has_meta_v
            item["has_summary"] = key_base in summary_keys
            item["has_md"] = key_base in md_keys
            out.append(item)
        return out

    # ------------------------------------------------ 实时转写 / 录音控制

    @app.post("/api/rt")
    async def api_rt(req: Request):
        require_connected()
        action = (await read_json(req)).get("action")
        if action == "start":
            rt_state["bytes"] = 0
            await recorder.realtime_start()
            return {"message": "已发送开始实时转写，等待设备推流"}
        if action == "stop":
            session = await recorder.realtime_stop()
            if session is not None and session.path is not None:
                return {"message": f"实时码流已保存：{session.path.name}"
                                   f"（{fmt_size(session.received)}）",
                        "url": f"/downloads/{session.path.name}"}
            return {"message": "实时会话已结束"}
        if action in ("pause", "resume"):
            await recorder.realtime_pause(action == "pause")
            return {"message": "已发送"
                    + ("暂停" if action == "pause" else "继续")}
        raise RecorderError("action 须为 start/stop/pause/resume")

    @app.post("/api/rec")
    async def api_rec(req: Request):
        require_connected()
        action = (await read_json(req)).get("action")
        ops = {"start": (recorder.record_start, "开始录音"),
               "save": (recorder.record_save, "保存录音"),
               "pause": (recorder.record_pause, "暂停录音"),
               "resume": (recorder.record_resume, "继续录音")}
        if action in ops:
            fn, zh = ops[action]
            r = await fn()
            return {"message": f"{zh}：{RESULT_NAMES.get(r, r)}"}
        if action == "state":
            s = await recorder.record_state()
            return {"message": f"录音状态:{STATE_NAMES.get(s, s)}"}
        if action == "time":
            duration, size = await recorder.record_time()
            return {"message": f"录音时长 {fmt_duration(duration)}，"
                               f"当前大小 {fmt_size(size)}"}
        if action == "name":
            return {"message":
                    f"当前文件名：{await recorder.record_filename()}"}
        raise RecorderError("action 无效")

    @app.get("/api/gain")
    async def api_gain_get():
        require_connected()
        g = await recorder.get_gain()
        return {"gain": g, "text": GAIN_NAMES.get(g, str(g))}

    @app.post("/api/gain")
    async def api_gain_set(req: Request):
        require_connected()
        level = (await read_json(req)).get("level")
        if level not in (1, 2, 3):
            raise RecorderError("level 须为 1/2/3")
        r = await recorder.set_gain(level)
        return {"message": "设置增益成功" if r == 0
                else f"设置失败（code={r}）"}

    # ------------------------------------------------ 调试

    @app.post("/api/raw")
    async def api_raw(req: Request):
        require_connected()
        body = await read_json(req)
        try:
            type_, cmd = int(body.get("type")), int(body.get("cmd"))
            params = bytes.fromhex(
                str(body.get("params") or "").replace("0x", "")
                .replace(" ", ""))
        except (TypeError, ValueError):
            raise RecorderError("type/cmd 须为整数，params 须为 hex")
        await recorder.send_raw_command(type_, cmd, params)
        return {"message": f"已发送 {type_}-{cmd} "
                           f"params={params.hex(' ') or '-'}，"
                           "应答见日志设备事件"}

    @app.post("/api/rawframe")
    async def api_rawframe(req: Request):
        require_connected()
        raw = str((await read_json(req)).get("hex") or "")
        try:
            frame = bytes.fromhex(raw.replace("0x", "").replace(" ", ""))
        except ValueError:
            frame = b""
        if not frame:
            raise RecorderError("完整帧 hex 无效")
        await recorder.send_raw_frame(frame)
        return {"message": f"已直发 {len(frame)}B：{frame.hex(' ')}"}

    # ================================================ LLM 配置 + AI 摘要 + 归档

    # -------- LLM 配置（前端设置面板用） --------
    @app.get("/api/llm_config")
    async def api_llm_config_get():
        cfg = LLM.load_config(recorder.output_dir)
        return cfg.as_safe_dict()

    @app.post("/api/llm_config")
    async def api_llm_config_set(req: Request):
        body = await read_json(req)
        old = LLM.load_config(recorder.output_dir)
        # 基础字段
        provider = str(body.get("provider") or old.provider).strip().lower()
        if provider not in ("deepseek", "ollama", "relay"):
            raise RecorderError("provider 须为 deepseek / ollama / relay")
        base_url = str(body.get("base_url") or "").strip()
        # 如果用户没填 base_url，用默认
        if not base_url:
            if provider == "deepseek":
                base_url = "https://api.deepseek.com"
            elif provider == "ollama":
                base_url = "http://127.0.0.1:11434/v1"
            else:
                raise RecorderError("中转站需填写 base_url")
        # api_key：如果前端带 api_key_masked 说明用户不想改 key
        api_key = old.api_key
        if "api_key" in body and body["api_key"] is not None:
            raw_key = str(body["api_key"]).strip()
            # 只有当用户传入的不是 mask 时才更新（mask 形如 sk-****abcd）
            if raw_key and "****" not in raw_key:
                api_key = raw_key
        model_name = str(body.get("model_name") or "").strip()
        if not model_name:
            if provider == "deepseek":
                model_name = "deepseek-chat"
            elif provider == "ollama":
                model_name = "qwen2.5:7b"
            else:
                raise RecorderError("请填写模型名称")
        temperature = body.get("temperature")
        try:
            temperature = float(temperature) if temperature is not None else old.temperature
            temperature = max(0.0, min(1.5, temperature))
        except Exception:
            temperature = old.temperature
        timeout = body.get("timeout")
        try:
            timeout = int(timeout) if timeout is not None else old.timeout
            timeout = max(10, min(600, timeout))
        except Exception:
            timeout = old.timeout
        enabled = bool(body.get("enabled", False)) if "enabled" in body else old.enabled
        cfg = LLM.LLMConfig(
            provider=provider, base_url=base_url, api_key=api_key,
            model_name=model_name, temperature=temperature, timeout=timeout,
            enabled=enabled,
        )
        # 保存前做一次连通性试测？太耗时，只在真正摘要时测。这里直接保存。
        LLM.save_config(recorder.output_dir, cfg)
        log_push("OK", f"AI 摘要配置已保存：{cfg.provider}/{cfg.model_name}"
                 + ("（已启用）" if cfg.enabled else "（未启用）"))
        return cfg.as_safe_dict()

    @app.post("/api/llm_test")
    async def api_llm_test(req: Request):
        """测试 LLM 连通性：发一条极短的 ping。"""
        # 先用当前保存的配置，但可被 body 覆盖
        cfg = LLM.load_config(recorder.output_dir)
        body = await read_json(req)
        if body:
            # 允许临时覆盖，用于"测试连接"按钮
            try:
                for k in ("provider", "base_url", "model_name"):
                    if k in body and body[k]:
                        setattr(cfg, k, str(body[k]).strip())
                if "api_key" in body and body["api_key"] and "****" not in str(body["api_key"]):
                    cfg.api_key = str(body["api_key"]).strip()
                if "temperature" in body:
                    cfg.temperature = min(1.5, max(0.0, float(body["temperature"])))
                if "timeout" in body:
                    cfg.timeout = max(10, min(600, int(body["timeout"])))
                if "enabled" in body:
                    cfg.enabled = bool(body["enabled"])
            except Exception as exc:
                raise RecorderError(f"配置项无效：{exc}")
        if not cfg.enabled:
            cfg.enabled = True  # 测试时临时当作开启
        try:
            # 用统一调用发一条极短消息，验证握手/鉴权/模型存在
            out = LLM._chat_completions_openai(cfg, [
                {"role": "system", "content": "只回复 pong，不要其他文字。"},
                {"role": "user", "content": "ping"},
            ])
        except ModuleNotFoundError as exc:
            raise RecorderError(
                f"缺少 Python 依赖：{exc.name or exc}。请在当前环境执行 pip install 后重试。"
            ) from exc
        except RuntimeError as exc:
            raise RecorderError(str(exc)) from exc
        except ValueError as exc:
            raise RecorderError(f"配置不合法：{exc}") from exc
        except Exception as exc:
            raise RecorderError(f"连接失败：{type(exc).__name__}: {exc}") from exc
        return {"ok": True, "reply": (out or "")[:80]}

    # -------- AI 摘要 --------
    @app.post("/api/summarize")
    async def api_summarize(req: Request):
        """基于转写结果生成结构化 AI 摘要。
        body: {name: 源音频相对路径, force: bool（忽略缓存重算）}
        """
        body = await read_json(req)
        busy_guard()
        name_raw = str(body.get("name") or "")
        force = bool(body.get("force", False))
        audio_path = _resolve_safe_name(recorder.output_dir, name_raw)
        if not audio_path.is_file():
            raise RecorderError("源文件不存在")
        rel_name = audio_path.relative_to(recorder.output_dir).as_posix()
        # 读转写文本：优先 .transcript.json 里的 text，其次 .txt
        meta_path = audio_path.with_suffix(".transcript.json")
        txt_path = audio_path.with_suffix(".txt")
        text = ""
        transcript_for_export = {"text": "", "segments": [], "model": "",
                                 "language": "", "spk": False, "spk_mode": "off"}
        if meta_path.is_file():
            try:
                import json as _json
                meta = _json.loads(meta_path.read_text(encoding="utf-8"))
                text = meta.get("text") or ""
                transcript_for_export.update({
                    "text": text,
                    "segments": meta.get("segments") or [],
                    "model": meta.get("model") or "",
                    "language": meta.get("language") or "",
                    "spk": bool(meta.get("spk")),
                    "spk_mode": meta.get("spk_mode") or "",
                })
            except Exception:
                pass
        if not text and txt_path.is_file():
            try:
                text = txt_path.read_text(encoding="utf-8")
                transcript_for_export["text"] = text
            except Exception as exc:
                raise RecorderError(f"读取转写 txt 失败：{exc}") from exc
        if not isinstance(text, str) or not text.strip():
            raise RecorderError("该文件暂无转写内容，请先转写后再摘要")

        # 缓存（即使 LLM 模块损坏也给出清晰错误）
        try:
            cache_path = LLM.summary_cache_path(audio_path)
        except Exception as exc:
            raise RecorderError(
                f"LLM 模块异常（{type(exc).__name__}: {exc}）。请确认 recorder/llm.py 存在且无语法错误。"
            ) from exc
        if not force and cache_path.is_file():
            try:
                cached = LLM.load_summary_cache(audio_path)
            except Exception:
                cached = None
            if isinstance(cached, dict):
                log_push("INFO", f"摘要（缓存命中）：{rel_name}")
                return {"summary": cached, "from_cache": True,
                        "source": rel_name}

        cfg_raw = LLM.load_config(recorder.output_dir)
        if not isinstance(cfg_raw, LLM.LLMConfig):
            raise RecorderError("LLM 配置类型非法，请重新在「摘要设置」中保存一次。")
        cfg: LLM.LLMConfig = cfg_raw
        if not getattr(cfg, "enabled", False):
            raise RecorderError("AI 摘要未启用，请先在「摘要设置」中完成配置并开启开关")
        log_push("INFO", f"AI 摘要中（{cfg.provider}/{cfg.model_name}）…请稍候")
        async with op_lock:
            try:
                result = LLM.summarize(cfg, text, source_filename=audio_path.name)
            except ModuleNotFoundError as exc:
                raise RecorderError(
                    f"缺少 Python 依赖：{exc.name or exc}。请在当前环境执行 pip install 后重试。"
                ) from exc
            except RuntimeError as exc:
                raise RecorderError(str(exc)) from exc
            except ValueError as exc:
                raise RecorderError(f"AI 返回数据格式不对：{exc}") from exc
            except Exception as exc:
                logger.exception("摘要生成异常")
                raise RecorderError(
                    f"摘要生成失败（{type(exc).__name__}）：{exc}"
                ) from exc
        if not isinstance(result, dict):
            raise RecorderError(
                f"AI 摘要返回类型异常（{type(result).__name__}），请换个模型或重试。"
            )
        LLM.save_summary_cache(audio_path, result)
        title_preview = str(result.get("title", "") or "")[:20]
        log_push("OK", f"AI 摘要完成：{title_preview}…")
        return {"summary": result, "from_cache": False, "source": rel_name}

    @app.post("/api/summary")
    async def api_summary_get(req: Request):
        """读取已缓存的摘要（不调用 LLM）。"""
        body = await read_json(req)
        name_raw = str(body.get("name") or "")
        audio_path = _resolve_safe_name(recorder.output_dir, name_raw)
        if not audio_path.is_file():
            raise RecorderError("源文件不存在")
        data = LLM.load_summary_cache(audio_path)
        rel = audio_path.relative_to(recorder.output_dir).as_posix()
        return {"summary": data, "source": rel,
                "cached": data is not None}

    @app.post("/api/save_summary")
    async def api_save_summary(req: Request):
        """保存用户手动编辑过的摘要 / 思维导图大纲。

        body:
          name: 源音频相对路径
          summary: 结构化摘要对象（编辑后的 lastData）
          mindmap_md: 可选——用户单独编辑过的思维导图 Markdown

        保存策略：
          - summary 直接覆盖 .summary.json，并加 edited=true 标记（下次 load 即使 force=false 仍会命中）
          - mindmap_md 写入同目录的 .mindmap.md；读缓存时会一起透出
        """
        import json as _json
        body = await read_json(req)
        busy_guard()
        name_raw = str(body.get("name") or "")
        audio_path = _resolve_safe_name(recorder.output_dir, name_raw)
        if not audio_path.is_file():
            raise RecorderError("源文件不存在")
        rel = audio_path.relative_to(recorder.output_dir).as_posix()

        summary_payload = body.get("summary")
        if summary_payload is None:
            raise RecorderError("缺少 summary 参数")
        if not isinstance(summary_payload, dict):
            raise RecorderError("summary 必须是对象")

        # 保留原始缓存里可能存在的 AI 字段（如 _generated_at 等），只把用户传的覆盖写入
        cached = LLM.load_summary_cache(audio_path) or {}
        if not isinstance(cached, dict):
            cached = {}
        cached.update(summary_payload)
        # 明确标记为用户手动编辑过，下次即便"重新生成（force=true）"前也告知用户
        cached["edited"] = True
        cached["edited_at"] = int(__import__("time").time() * 1000)

        # 思维导图大纲：用户单独编辑过的 Markdown
        mindmap_md = body.get("mindmap_md")
        if isinstance(mindmap_md, str) and mindmap_md.strip():
            cached["mindmap_md"] = mindmap_md.strip()
        # 兼容写法：再额外保存一份独立的 .mindmap.md，方便用户直接打开看
        try:
            if isinstance(mindmap_md, str) and mindmap_md.strip():
                mm_path = audio_path.with_suffix(".mindmap.md")
                mm_path.write_text(mindmap_md.strip(), encoding="utf-8")
        except Exception as exc:
            logger.warning("写入思维导图大纲失败：%s", exc)

        # 写 .summary.json
        try:
            LLM.save_summary_cache(audio_path, cached)
        except Exception as exc:
            logger.exception("保存摘要失败")
            raise RecorderError(f"保存摘要失败：{type(exc).__name__}: {exc}") from exc

        log_push("OK", f"摘要已手动保存：{rel}")
        return {"ok": True, "path": rel,
                "has_mindmap_md": bool(isinstance(mindmap_md, str) and mindmap_md.strip())}

    # -------- Markdown 导出 --------
    async def _do_export_markdown(name_raw: str) -> dict:
        """把转写 + 摘要（如有）导出为 Markdown（内部实现）。"""
        audio_path = _resolve_safe_name(recorder.output_dir, name_raw)
        if not audio_path.is_file():
            raise RecorderError("源文件不存在")
        # 组装 transcript_for_export（尽量多的信息）
        transcript = {"text": "", "segments": [], "model": "",
                      "language": "", "spk": False, "spk_mode": "off"}
        meta_path = audio_path.with_suffix(".transcript.json")
        txt_path = audio_path.with_suffix(".txt")
        if meta_path.is_file():
            try:
                import json as _json
                meta = _json.loads(meta_path.read_text(encoding="utf-8"))
                transcript.update({
                    "text": meta.get("text") or "",
                    "segments": meta.get("segments") or [],
                    "model": meta.get("model") or "",
                    "language": meta.get("language") or "",
                    "spk": bool(meta.get("spk")),
                    "spk_mode": meta.get("spk_mode") or "",
                })
            except Exception:
                pass
        if not transcript["text"] and txt_path.is_file():
            transcript["text"] = txt_path.read_text(encoding="utf-8")
        summary = LLM.load_summary_cache(audio_path)
        try:
            md_path = LLM.export_markdown(audio_path, transcript, summary)
        except ModuleNotFoundError as exc:
            raise RecorderError(f"导出失败：缺少 Python 依赖。请先 pip install {exc.name or 'Markdown 相关依赖'} 后重启服务。")
        except Exception as exc:
            logger.exception("导出 Markdown 失败")
            raise RecorderError(f"导出失败：{exc}")
        rel = md_path.relative_to(recorder.output_dir).as_posix()
        log_push("OK", f"已导出 Markdown：{rel}")
        return {"ok": True,
                "md_url": f"/downloads/{rel}",
                "md_name": rel,
                "md_path": rel,          # 前端兼容字段
                "output_path": rel,      # 前端兼容字段
               }

    @app.post("/api/export_md")
    async def api_export_md(req: Request):
        body = await read_json(req)
        name_raw = str(body.get("name") or "")
        return await _do_export_markdown(name_raw)

    @app.post("/api/export_markdown")
    async def api_export_markdown(req: Request):
        body = await read_json(req)
        name_raw = str(body.get("name") or "")
        return await _do_export_markdown(name_raw)

    # -------- 归档（移动到日期子目录） --------
    @app.post("/api/archive")
    async def api_archive(req: Request):
        """把一个文件（及其附属 txt/transcript/summary/md）移入指定日期子目录，
        默认移入今天。body: {name, folder: '2026-08-15' 可选}"""
        body = await read_json(req)
        name_raw = str(body.get("name") or "")
        folder = str(body.get("folder") or "").strip() or _today_folder()
        # 校验 folder 格式：YYYY-MM-DD（宽松）
        import re as _re
        if not _re.match(r'^\d{4}-\d{2}-\d{2}$', folder):
            raise RecorderError(f"日期目录格式非法，需为 YYYY-MM-DD：{folder}")
        # 待移动的源文件（主文件）
        src = _resolve_safe_name(recorder.output_dir, name_raw)
        if not src.is_file():
            raise RecorderError("源文件不存在")
        # 如果已经在该目录下，跳过
        current_folder = src.parent.name
        if current_folder == folder:
            raise RecorderError("该文件已在对应日期目录下")
        # 目标目录
        dest_dir = recorder.output_dir / folder
        dest_dir.mkdir(parents=True, exist_ok=True)
        if dest_dir.resolve() == src.parent.resolve():
            raise RecorderError("源目录与目标目录相同")
        # 防止同名：若目标已存在同名，则加后缀
        dest_main = dest_dir / src.name
        n = 1
        while dest_main.is_file() and dest_main.resolve() != src.resolve():
            dest_main = dest_dir / f"{src.stem}_{n}{src.suffix}"
            n += 1
        new_stem = dest_main.stem
        moved = []
        # 1) 主文件
        src_rel = src.relative_to(recorder.output_dir).as_posix()
        src.rename(dest_main)
        dest_rel = dest_main.relative_to(recorder.output_dir).as_posix()
        moved.append([src_rel, dest_rel])
        # 2) 同目录附属文件：.txt / .transcript.json / .summary.json / .md
        old_parent = src.parent
        old_stem = src.stem
        new_parent = dest_dir
        for ext in [".txt", ".transcript.json", ".summary.json", ".md"]:
            if ext == ".transcript.json":
                old_p = old_parent / f"{old_stem}.transcript.json"
                new_p = new_parent / f"{new_stem}.transcript.json"
            else:
                old_p = old_parent / f"{old_stem}{ext}"
                new_p = new_parent / f"{new_stem}{ext}"
            if old_p.is_file():
                # 如果新的有同名冲突，直接覆盖（理论上不会有，因为我们主文件已改名）
                if new_p.resolve() == old_p.resolve():
                    continue
                if new_p.exists():
                    new_p.unlink(missing_ok=True)
                old_p.rename(new_p)
                try:
                    moved.append([
                        old_p.relative_to(recorder.output_dir).as_posix(),
                        new_p.relative_to(recorder.output_dir).as_posix(),
                    ])
                except ValueError:
                    pass
        log_push("OK", f"已归档 {len(moved)} 个文件到 {folder}/")
        return {"ok": True, "folder": folder,
                "target_folder": folder,   # 前端兼容字段
                "moved": moved,
                "new_main": dest_rel,
                "new_name": dest_rel,      # 前端兼容字段：更新 TX.source
               }

    # ------------------------------------------------ WebSocket / 静态资源

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        sockets.add(ws)
        try:
            while True:
                await ws.receive_text()   # 客户端不发数据，保持连接即可
        except WebSocketDisconnect:
            pass
        finally:
            sockets.discard(ws)

    recorder.output_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/downloads", StaticFiles(directory=str(recorder.output_dir)),
              name="downloads")
    if WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True),
                  name="web")
    return app


def run_server(output_dir: Path, host: str = "127.0.0.1",
               port: int = 8000, auto_open_browser: bool = False) -> None:
    """启动 Web 服务（阻塞直到 Ctrl+C）。"""
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError(_INSTALL_HINT) from exc
    app = create_app(output_dir)
    url = f"http://{host}:{port}/"
    print(f"Web 界面已启动：{url}  （Ctrl+C 退出）")
    if auto_open_browser:
        import threading, webbrowser
        def _open():
            # 等 1.5 秒让服务器先起来
            import time as _t
            _t.sleep(1.5)
            try:
                webbrowser.open(url)
                print("已自动打开浏览器。如果没有弹出，请手动访问：", url)
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
