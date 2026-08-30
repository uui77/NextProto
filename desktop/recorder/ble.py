"""BLE 传输层：基于 bleak 的扫描、连接、Notify 订阅与 AE21 写入。
协议第 2 节：
    Service        0xAE20  录音笔业务服务
    Characteristic 0xAE21  WRITE_WITHOUT_RESPONSE  App→Dev 协议帧
    Characteristic 0xAE22  NOTIFY  Dev→App 控制应答、音频、列表、文件数据
    Characteristic 0xAE23  NOTIFY  Dev→App 机身按键及录音状态消息
关键约束：2-2 文件导入请求帧（36B）必须一次 GATT 写入，
不允许分包器拆分（拆成 20+16 会稳定返回"文件不存在"）。
"""
from __future__ import annotations
import asyncio
import logging
import sys
import threading
import concurrent.futures
from typing import Any, Awaitable, Callable, List, Optional, TypeVar
from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

logger = logging.getLogger(__name__)

# ============================================================================
# Windows + PyInstaller(打包后) 专用：MTA 工作线程。
#
# 现象：打包的 EXE 里调用 bleak（扫描/连接）时抛
#   BleakError: Thread is configured for Windows GUI but callbacks are not working.
#              Suspect unwanted side effects from importing 'pythoncom'.
#
# 根因：PyInstaller 的 pywin32 hook 会在程序入口前把主线程 COM 模型锁死在
# STA(APARTMENTTHREADED)/MAIN_STA，而 bleak 的 WinRT 后端需要
# COINIT_MULTITHREADED (MTA)；SetTimer 回调需要消息泵，STA 线程没泵就超时。
#
# 解决方案：所有 bleak 相关协程（scan/pair/connect/disconnect/write）统一丢
# 到单独的"BLE-MTA-Worker"线程里执行。该线程从未被任何代码初始化过 COM，
# bleak/winsdk 会自己用 MTA 初始化 → 回调正常 → 扫描连接均 OK。
#
# 跨线程回调：notify/disconnect 等 BLE 回调在 MTA 线程里被调用时，通过
# caller_loop.call_soon_threadsafe 投递回 API 调用方的事件循环。
# ============================================================================
_T = TypeVar("_T")


class _MtaWorker:
    """封装长期存活的 MTA 工作线程 + 专属 asyncio 事件循环。"""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # 只在 Windows 启用；Linux/macOS 直接在调用者 loop 里跑
        self._enabled = sys.platform.startswith("win")

    # ---- 生命周期 ----
    def _ensure_started(self) -> None:
        if self._thread is not None or not self._enabled:
            return
        self._thread = threading.Thread(
            target=self._thread_main, name="BLE-MTA-Worker", daemon=True)
        self._thread.start()
        if not self._ready_evt.wait(timeout=10):
            raise RuntimeError("BLE 工作线程启动超时（请重试）")

    def _thread_main(self) -> None:
        try:
            # 显式用 MTA 初始化 COM（bleak/winsdk 若未初始化会自己做，
            # 这里先把它锁死到 MTA，避免 bleak 多线程下的竞态）
            if sys.platform.startswith("win"):
                try:
                    import ctypes as _ct
                    _COINIT_MULTITHREADED = 0x0
                    _ct.WinDLL("ole32").CoInitializeEx(None, _COINIT_MULTITHREADED)
                except Exception:
                    pass
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
        finally:
            self._ready_evt.set()
        # 永久运行；程序退出 daemon=True 会自动杀
        try:
            self._loop.run_forever()
        finally:
            try:
                self._loop.close()
            except Exception:
                pass
            if sys.platform.startswith("win"):
                try:
                    import ctypes as _ct
                    _ct.WinDLL("ole32").CoUninitialize()
                except Exception:
                    pass

    def restart(self) -> None:
        """重启 MTA 工作线程（当 BLE 操作阻塞事件循环时用于恢复）。"""
        old_loop = self._loop
        old_thread = self._thread
        # 标记旧 loop 停止
        if old_loop:
            try:
                old_loop.call_soon_threadsafe(old_loop.stop)
            except Exception:
                pass
        # 重置状态，下次 run() 时会自动启动新线程
        self._loop = None
        self._thread = None
        self._ready_evt = threading.Event()

    # ---- 公共 API：把一个协程/工厂丢到 MTA loop 执行，对调用方表现为 awaitable ----
    async def run(self, coro_factory: Callable[[], Awaitable[_T]],
                  *, caller_loop: Optional[asyncio.AbstractEventLoop] = None) -> _T:
        """
        coro_factory: 一个 0 参的工厂函数，返回 awaitable（必须是工厂而不是直接传
            coroutine 对象，因为 coroutine 绑定创建 loop，跨线程跑会炸）。
        """
        if not self._enabled:
            # 非 Windows：直接在当前 loop 跑，保证行为一致
            return await coro_factory()
        self._ensure_started()
        assert self._loop is not None
        # 用 run_coroutine_threadsafe 把协程塞到 MTA loop，拿到 concurrent.futures.Future
        fut = asyncio.run_coroutine_threadsafe(coro_factory(), self._loop)
        # 对当前 caller_loop 暴露一个 asyncio Future
        return await asyncio.wrap_future(fut, loop=caller_loop)


# 全局单例：整个进程共享一个 MTA 线程
_MTA = _MtaWorker()


def _run_in_mta(coro_factory: Callable[[], Awaitable[_T]]) -> Awaitable[_T]:
    """顶层便利函数：等价于 _MTA.run()，自动获取当前 caller_loop。"""
    return _MTA.run(coro_factory)

def _uuid16(short: int) -> str:
    """16bit UUID 扩展为 128bit 标准形式。"""
    return f"0000{short:04x}-0000-1000-8000-00805f9b34fb"
SERVICE_UUID = _uuid16(0xAE20)
CHAR_WRITE = _uuid16(0xAE21)
CHAR_NOTIFY_MAIN = _uuid16(0xAE22)
CHAR_NOTIFY_KEY = _uuid16(0xAE23)
DEFAULT_CHUNK = 20  # 未协商 MTU 时的保守单包载荷
# 已知设备型号关键字（厂家测试页确认型号为 QS668，CB08 为早期叫法）
DEVICE_NAME_KEYWORDS = ("cb08", "qs668")

# WinError → 中文友好提示（对应 bleak 在 Windows 上常见的适配器错误）
#   -2147020577 = 0x800710DF = HRESULT_FROM_WIN32(ERROR_DEVICE_NOT_AVAILABLE /
#                                             OR ERROR_NOT_READY?)
#   -2147467259 = 0x80004005 = E_FAIL（一般也是蓝牙关了）
#   -2147024809 = 0x80070057 = E_INVALIDARG（极少数情况也是适配器没起来）
_WINERROR_HINTS = {
    -2147020577: (
        "蓝牙未开启或适配器未就绪。请在 Windows 设置 → 蓝牙和其他设备 中打开蓝牙，"
        "并确保以下服务正在运行：蓝牙音频网关服务 / Bluetooth Support Service / "
        "设备关联服务。然后重试。"
    ),
    -2147467259: (
        "蓝牙适配器未就绪（未启用或驱动异常）。请检查 Windows 设置中蓝牙是否已开启，"
        "或在设备管理器中确认蓝牙适配器工作正常后重试。"
    ),
    -2147023922: (
        "未找到蓝牙适配器。请确认电脑带有蓝牙功能，且在设备管理器中没有被禁用。"
    ),
    -2147467260: (
        "蓝牙接口不可用（E_NOINTERFACE）。这通常是 Windows 蓝牙服务异常导致的。\n"
        "请尝试：\n"
        "  1) 重启蓝牙服务：Win+R → services.msc → 找到 Bluetooth Support Service → 右键重启\n"
        "  2) 在 Windows 设置 → 蓝牙中删除录音笔配对记录，重新扫描配对\n"
        "  3) 如果频繁出现，重启电脑后重试"
    ),
}


def _describe_ble_error(exc: Exception) -> str:
    """把 bleak/Windows 抛的原始异常翻译成用户能看懂的中文提示。"""
    # 0) bleak 自己抛出的 BleakError（先从消息特征识别，比如 Unreachable）
    msg_lower = str(exc).lower()
    if ("unreachable" in msg_lower
            or "could not get gatt services" in msg_lower
            or "get services" in msg_lower and "unreachable" in msg_lower):
        return (
            "设备 GATT 服务暂不可达（设备瞬时 Unreachable）。常见原因：\n"
            "  1) 刚完成配对，设备在做 bond-reset（1~3 秒），请再点一次连接；\n"
            "  2) 录音笔已被其他设备（手机 / 另一台电脑）连接占用，请断开那边的蓝牙再试；\n"
            "  3) 距离过远 / 电量过低：请把录音笔靠近电脑或充电后重试；\n"
            "  4) Windows 蓝牙缓存异常：按 Win+I → 蓝牙 → 找到已配对的 CB08/QS668 → "
            "「删除设备」，然后回到本页重新扫描 + 配对（PIN 0000 / 1234）。"
        )
    # 1) OSError + winerror
    if isinstance(exc, OSError):
        win_err = getattr(exc, "winerror", None)
        if win_err is not None and win_err in _WINERROR_HINTS:
            return _WINERROR_HINTS[win_err]
        msg = str(exc)
        # 有些异常没有正确填 winerror，但在消息里写了
        if "device not ready" in msg.lower() or "设备未就绪" in msg:
            return _WINERROR_HINTS[-2147020577]
        if "no bluetooth adapter" in msg.lower():
            return _WINERROR_HINTS[-2147023922]
        if sys.platform.startswith("win"):
            return f"蓝牙调用失败（Windows 错误 {win_err or '未知'}）：请先确认蓝牙已开启后重试。"
    # 2) bleak 自己抛的异常（含特定关键词）
    if "no bluetooth adapter found" in msg_lower or "could not find any bluetooth adapter" in msg_lower:
        return "未检测到蓝牙适配器。请确认电脑已开启蓝牙功能，或外接蓝牙适配器。"
    if "adapter not found" in msg_lower or "adapter not ready" in msg_lower:
        return "蓝牙适配器未就绪。请打开 Windows 设置中的蓝牙开关后重试。"
    return None


class BleTransport:
    """封装一台录音笔的 BLE 连接与收发。
    on_main / on_key 回调分别收到 AE22 / AE23 的原始通知字节，
    上层各自用独立 FrameParser 处理。

    实现注意（Windows 打包）：
    所有 BLE 操作都通过 _run_in_mta 跳到「BLE-MTA-Worker」线程执行。
    self._client 只能在 MTA 线程里访问；用户代码里设置的 on_main/on_key/
    on_disconnect 回调会通过 caller_loop.call_soon_threadsafe 回到调用方
    线程的事件循环，保证上层 FrameParser 等逻辑的线程归属与之前一致。
    """

    CONNECT_TIMEOUT = 12.0       # BLE 连接/订阅显式超时（秒）
    PAIR_TIMEOUT    = 20.0       # 配对超时（秒）：需要用户在系统弹窗里点确认，留久一点。

    def __init__(self) -> None:
        self._client: Optional[BleakClient] = None
        self.on_main: Optional[Callable[[bytes], None]] = None
        self.on_key: Optional[Callable[[bytes], None]] = None
        self.on_disconnect: Optional[Callable[[], None]] = None
        self.on_connect_progress: Optional[Callable[[str], None]] = None
        # 调用方事件循环：回调时用 call_soon_threadsafe 把事件投递回去
        self._caller_loop: Optional[asyncio.AbstractEventLoop] = None

    # ============================================================== 扫描
    @staticmethod
    async def scan(timeout: float = 6.0,
                   compat: bool = False) -> List[BLEDevice]:
        """扫描广播含 AE20 服务或名称匹配 QS668/CB08 的设备。
        compat=True 时返回全部有名设备（兼容广播不携带服务 UUID
        的固件，对应厂家测试页的"兼容扫描"）。
        """
        def factory(timeout=timeout, compat=compat):
            async def _do():
                found: List[BLEDevice] = []
                try:
                    devices = await BleakScanner.discover(
                        timeout=timeout, return_adv=True)
                except Exception as exc:
                    friendly = _describe_ble_error(exc)
                    if friendly:
                        raise RuntimeError(friendly) from exc
                    raise
                for device, adv in devices.values():
                    name = (device.name or adv.local_name or "")
                    uuids = [u.lower() for u in (adv.service_uuids or [])]
                    if SERVICE_UUID in uuids or \
                            any(k in name.lower() for k in DEVICE_NAME_KEYWORDS):
                        found.append(device)
                    elif compat and name:
                        found.append(device)
                return found
            return _do()
        return await _run_in_mta(factory)

    # ============================================================== 连接

    async def pair(self, device) -> bool:
        """请求与设备配对（尽力而为）。

        策略（按顺序尝试）：
          1) 跨平台 bleak client.pair()——通常只有 Linux BlueZ、部分 macOS 会成功；
             Windows/WinRT 的 bleak pair() 在 0.22 里常抛 NotImplementedError。
             注意：这一步会真正建立 BLE 连接（pair() 需要一个 GATT 连接上下文），
             成功后我们会立即 disconnect 并留给上层 3s 冷却再 connect，避免
             "Could not get GATT services: Unreachable" 设备 bond-reset 瞬时不可达。
          2) Windows 专属：直接调 WinRT 的 DeviceInformation.Pairing.PairAsync()
             搭配 RequestedPairingKinds=ConfirmOnly | ProvidePin（Just Works
             或 4 位 PIN 模式），这是官方文档推荐的程序化配对入口，一般会弹系统
             级的"确认配对码"/"输入 PIN"对话框，用户点确认即可绑定。
             **这条路径不需要先 GATT connect，也不会影响后续 connect。**
          3) 以上都不行：调用 `os.startfile(...)` 帮用户直接打开 Windows
             「蓝牙和其他设备」设置页。这种 100% 能用，只是用户多点一下。

        返回 True/"settings_opened"/False 见 device.py.Recorder.pair 的 docstring。
        """
        # 保存调用方事件循环（回调时跨线程用）
        try:
            self._caller_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._caller_loop = None

        _self = self   # 闭包里避免直接写 self 引起歧义
        PAIR_TIMEOUT = self.PAIR_TIMEOUT
        addr = getattr(device, "address", str(device))

        def factory():
            async def _do():
                import asyncio as _aio
                need_cool_down = False

                # Step 1: Windows 优先 WinRT 专属配对
                if sys.platform.startswith("win"):
                    paired_winrt = await _self._pair_via_winrt(addr, timeout=PAIR_TIMEOUT)
                    if paired_winrt is True:
                        logger.info("WinRT 配对成功（无 GATT 连接干扰）")
                        return True
                    if paired_winrt == "timed_out":
                        raise RuntimeError(
                            f"配对等待超时（{PAIR_TIMEOUT:.0f}s）：请在系统弹出的配对对话框中"
                            f"确认（录音笔常见 PIN：0000 / 1234），再重试。"
                        )

                # Step 2: 跨平台 bleak pair()（会建立临时 GATT 连接）
                client = BleakClient(device)
                try:
                    ok = await _aio.wait_for(
                        client.pair(protection_level=None),
                        timeout=PAIR_TIMEOUT,
                    )
                    if ok:
                        need_cool_down = True
                        return True
                except NotImplementedError:
                    logger.info("bleak.pair() 后端不支持，最后尝试打开 Windows 设置页")
                except _aio.TimeoutError:
                    logger.warning("bleak.pair() 超时 %ds", PAIR_TIMEOUT)
                except Exception as exc:
                    logger.info("bleak.pair() 非致命失败（进入兜底）：%s", exc)
                finally:
                    try:
                        if getattr(client, "is_connected", False):
                            await client.disconnect()
                            need_cool_down = True
                    except Exception:
                        pass

                # Step 3: 兜底——打开 Windows 蓝牙设置页
                if sys.platform.startswith("win"):
                    _self._launch_windows_bluetooth_settings(addr)
                    return "settings_opened"
                return False
            return _do()
        return await _run_in_mta(factory)

    async def _pair_via_winrt(self, address: str, timeout: float):
        """Windows 专属：用 WinRT 的 DeviceInformation.Pairing.PairAsync() 做程序化配对。

        返回：
          True            配对成功或已配对
          "timed_out"     用户没在 timeout 秒内点系统弹窗确认
          False           此环境缺 winsdk/无法调用（需要走兜底打开设置页）
          "unsupported"   其他不支持情况
        """
        import asyncio as _aio
        try:
            # bleak 0.22+ 依赖 winrt / winsdk 系列安装包（通常都在）
            try:
                from winsdk.windows.devices.enumeration import (
                    DeviceInformation, DevicePairingKinds, DevicePairingResultStatus,
                    DevicePairingRequestedEventArgs,
                )
                from winsdk.windows.devices.bluetooth import BluetoothDevice
            except ModuleNotFoundError:
                # 旧包命名
                from winrt.windows.devices.enumeration import (
                    DeviceInformation, DevicePairingKinds, DevicePairingResultStatus,
                    DevicePairingRequestedEventArgs,
                )
                from winrt.windows.devices.bluetooth import BluetoothDevice
        except Exception as exc:
            logger.warning("未安装 winsdk/winrt 配对模块：%s，跳到打开设置页", exc)
            return False

        # 1) 将 MAC 字符串解析成 64 位整数
        try:
            mac_int = int(address.replace(":", "").replace("-", ""), 16)
        except Exception as exc:
            logger.warning("无法解析 MAC 地址 %r：%s", address, exc)
            return False

        # 2) 从 MAC 获取 BluetoothDevice 对象（异步）
        try:
            # winsdk 的异步工厂方法名带 _async 后缀，且返回 IAsyncOperation，
            # 可直接 await（不应丢进 run_in_executor）。
            bt_dev = await _aio.wait_for(
                BluetoothDevice.from_bluetooth_address_async(mac_int),
                timeout=10.0,
            )
            if bt_dev is None:
                logger.info("WinRT BluetoothDevice.from_bluetooth_address_async 返回 None")
                return False
        except Exception as exc:
            logger.info("获取 BluetoothDevice 对象失败：%s", exc)
            return False

        # 3) 取 DeviceInformation.Pairing 对象并调 PairAsync
        pairing = getattr(bt_dev.device_information, "pairing", None)
        if pairing is None:
            logger.info("DeviceInformation.Pairing 不可用，降级打开设置页")
            return "unsupported"

        # Just Works（CONFIRM_ONLY）：注册 pairing_requested 处理器并在回调里自动
        # accept()，从而等效于官方网页 Web Bluetooth 的"点一下就自动配对"，无需用户
        # 进 Windows 蓝牙设置页。此前不注册处理器报 REQUIRED_HANDLER_NOT_REGISTERED。
        kinds = int(DevicePairingKinds.CONFIRM_ONLY)
        custom = pairing.custom

        def _on_pairing_requested(_sender, args) -> None:
            logger.info("收到系统配对请求（自动接受）：%s", args.pairing_kind)
            try:
                args.accept()
            except Exception as exc:  # 设备可能已被占用/disconnect，捕获避免打断
                logger.info("自动接受配对回调异常：%s", exc)

        custom.add_pairing_requested(_on_pairing_requested)
        try:
            pair_operation = custom.pair_async(kinds)
            # winsdk 的 IAsyncOperation 可直接 await；事件回调会在同一线程派发
            res = await _aio.wait_for(pair_operation, timeout=timeout)
            status = getattr(res, "status", None)
            paired_statuses = {
                int(DevicePairingResultStatus.PAIRED),
                int(DevicePairingResultStatus.ALREADY_PAIRED),
                int(DevicePairingResultStatus.ACCESS_DENIED),  # 拒绝也算已处理
            }
            ok = (status is not None) and (int(status) in paired_statuses)
            if ok and int(status) != int(DevicePairingResultStatus.ACCESS_DENIED):
                logger.info("WinRT PairAsync 成功 status=%s", status)
                return True
            if status is not None and \
                    int(status) == int(DevicePairingResultStatus.ALREADY_PAIRED):
                logger.info("WinRT 提示已配对，直接返回成功")
                return True
            logger.info("WinRT PairAsync 返回 status=%s（失败），打开设置页兜底", status)
            return "unsupported"
        except _aio.TimeoutError:
            return "timed_out"
        except NotImplementedError:
            return "unsupported"
        except Exception as exc:
            logger.info("WinRT PairAsync 异常：%s", exc)
            return False
        finally:
            try:
                custom.remove_pairing_requested(_on_pairing_requested)
            except Exception:
                pass

    @staticmethod
    def _launch_windows_bluetooth_settings(address: str) -> None:
        """帮用户打开 Windows 蓝牙/添加设备设置页。

        尝试顺序：
          1) ms-settings-connectabledevices:devicediscovery  → 新版"添加设备"直达
          2) ms-settings:bluetooth                              → 蓝牙和其他设备页
          3) control bthprops.cpl                                → 旧版 Bluetooth 设置对话框
        """
        import os, subprocess, shlex
        opened = False
        for uri in (
            "ms-settings-connectabledevices:devicediscovery",
            "ms-settings:bluetooth",
        ):
            try:
                # Windows 上 os.startfile 可启动 URI Scheme
                if hasattr(os, "startfile"):
                    os.startfile(uri)  # type: ignore[attr-defined]
                else:
                    subprocess.Popen(["start", "", uri], shell=True)
                opened = True
                break
            except Exception:
                continue
        if not opened:
            try:
                subprocess.Popen(["control", "bthprops.cpl"])
                opened = True
            except Exception as exc:
                logger.warning("打开 Windows 蓝牙设置页失败：%s", exc)
        if opened:
            logger.info(
                "已打开 Windows 蓝牙设置页（设备 MAC %s）：请点击「添加设备 → 选录音笔 → "
                "配对码输入 0000 或 1234」，配对成功后回到页面点击连接。",
                address,
            )

    async def connect(self, device) -> None:
        """连接并订阅 AE22（必须）与 AE23（按键，尽力订阅）。

        说明：
          - 显式加 connect+notify 的总超时 CONNECT_TIMEOUT；
          - 捕获常见 OSError / TimeoutError → 带"先配对/蓝牙是否关了"的友好提示；
          - 对「Unreachable / 服务发现失败」做最多 2 次带 2s backoff 的自动重试
            （Windows 下刚配对完设备常会瞬时不可达，重试后基本成功）。
        """
        try:
            self._caller_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._caller_loop = None

        _self = self
        CONNECT_TIMEOUT = self.CONNECT_TIMEOUT

        def factory():
            async def _do():
                import asyncio as _aio
                max_retries = 1
                backoff = 2.0
                last_exc = None
                friendly: Optional[str] = None
                addr = getattr(device, "address", str(device))
                name = getattr(device, "name", "") or ""
                logger.info("[connect] 开始连接 %s (%s) [MTA worker 线程]", addr, name)
                for attempt in range(max_retries + 1):
                    # 重试时改用纯 MAC 地址，强制 bleak 重新发现设备（避免 GATT 缓存过期）
                    connect_target = device if attempt == 0 else addr
                    client = BleakClient(
                        connect_target, disconnected_callback=_self._handle_disconnect)
                    unreachable_this_attempt = False
                    timeout_this_attempt = False
                    current_step = "connect"

                    def _progress(msg):
                        if _self.on_connect_progress:
                            try:
                                _self.on_connect_progress(msg)
                            except Exception:
                                pass

                    try:
                        _progress(f"正在建立 BLE 连接…（第 {attempt+1}/{max_retries+1} 次）")
                        logger.info("[connect] attempt %d/%d: client.connect() ...",
                                    attempt + 1, max_retries + 1)
                        await _aio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
                        logger.info("[connect] connect() OK, is_connected=%s", client.is_connected)
                        _self._client = client
                        # ---- 诊断：打印设备暴露的所有 GATT 服务和特征值 ----
                        current_step = "get_services"
                        _progress("正在发现 GATT 服务…")
                        try:
                            svcs = await _aio.wait_for(client.get_services(),
                                                       timeout=CONNECT_TIMEOUT)
                            svc_uuids = [str(s.uuid).lower() for s in svcs]
                            char_uuids = []
                            for s in svcs:
                                for c in s.characteristics:
                                    char_uuids.append(str(c.uuid).lower())
                            logger.info("[connect] 发现 %d 个服务, %d 个特征值",
                                        len(svcs), len(char_uuids))
                            logger.info("[connect] services: %s", svc_uuids)
                            has_ae20 = SERVICE_UUID in svc_uuids
                            has_ae22 = CHAR_NOTIFY_MAIN in char_uuids
                            logger.info("[connect] AE20 服务存在=%s  AE22 特征值存在=%s",
                                        has_ae20, has_ae22)
                            if not has_ae20:
                                logger.warning(
                                    "[connect] 警告：AE20 服务不在设备暴露的服务列表里！"
                                    "可能 Windows GATT 缓存了旧的服务列表。"
                                    "解决：Windows 设置 → 蓝牙 → 找到 CB08 → 删除设备"
                                    "（移除配对）→ 重新配对 → 再连接。")
                            if not has_ae22:
                                logger.warning(
                                    "[connect] 警告：AE22 特征值不在设备暴露的特征列表里！")
                        except _aio.TimeoutError:
                            logger.warning("[connect] get_services() 超时")
                            raise
                        except Exception as exc:
                            logger.warning("[connect] get_services() 异常: %s", exc)
                        # ---- 订阅 AE22 ----
                        current_step = "start_notify(AE22)"
                        _progress("正在订阅通知（AE22）…")
                        logger.info("[connect] start_notify(AE22) ...")
                        await _aio.wait_for(
                            client.start_notify(CHAR_NOTIFY_MAIN, _self._notify_main),
                            timeout=CONNECT_TIMEOUT,
                        )
                        logger.info("[connect] AE22 订阅成功")
                        try:
                            current_step = "start_notify(AE23)"
                            logger.info("[connect] start_notify(AE23) ...")
                            await _aio.wait_for(
                                client.start_notify(CHAR_NOTIFY_KEY, _self._notify_key),
                                timeout=CONNECT_TIMEOUT,
                            )
                            logger.info("[connect] AE23 订阅成功")
                        except Exception as exc:
                            logger.warning("AE23 订阅失败（忽略）：%s", exc)
                        logger.info("[connect] 全部完成, 连接就绪")
                        return  # 正常退出
                    except _aio.TimeoutError as exc:
                        last_exc = exc
                        timeout_this_attempt = True
                        logger.warning("[connect] 超时 (attempt %d, step=%s): %s",
                                       attempt + 1, current_step, exc)
                        step_hint = {
                            "connect": "BLE 连接建立",
                            "get_services": "GATT 服务发现",
                            "start_notify(AE22)": "AE22 通知订阅",
                            "start_notify(AE23)": "AE23 通知订阅",
                        }.get(current_step, current_step)
                        friendly = (
                            f"蓝牙连接超时（{step_hint} 步骤无响应）。"
                            "这通常表示录音笔需要先在 Windows「设置 → 蓝牙和其他设备」里"
                            "手动完成「配对」（常见配对码 0000 / 1234），配对成功后再回到"
                            "本页面点「连接」；也可以直接点设备旁边的「🔗 配对」按钮触发"
                            "系统配对弹窗。如果已配对仍超时，尝试在 Windows 蓝牙设置中"
                            "删除设备后重新配对。"
                        )
                    except Exception as exc:
                        last_exc = exc
                        friendly = _describe_ble_error(exc)
                        msg_lower = str(exc).lower()
                        logger.warning("[connect] 异常 (attempt %d): %s: %s",
                                       attempt + 1, type(exc).__name__, exc)
                        unreachable_this_attempt = (
                            "unreachable" in msg_lower or
                            "could not get gatt services" in msg_lower
                        )
                    # 失败清理
                    try:
                        if client.is_connected:
                            await client.disconnect()
                    except Exception:
                        pass
                    _self._client = None
                    should_retry = (
                        (unreachable_this_attempt or timeout_this_attempt)
                        and attempt < max_retries
                    )
                    if not should_retry:
                        if not friendly:
                            raise last_exc
                        raise RuntimeError(friendly) from last_exc
                    wait = (3.0 * (attempt + 1) if timeout_this_attempt
                            else backoff * (attempt + 1))
                    _progress(f"{'订阅/连接超时' if timeout_this_attempt else 'GATT 不可达'}，{wait:g}s 后重试…")
                    logger.info(
                        "连接遇到 %s，%ds 后进行第 %d/%d 次重试（若浏览器官方测试页仍"
                        "开着，请先关闭以释放设备连接）…",
                        "订阅/连接超时" if timeout_this_attempt else "GATT Unreachable",
                        wait, attempt + 2, max_retries + 1,
                    )
                    await _aio.sleep(wait)
            return _do()
        # 外层超时（主线程事件循环）：当 MTA worker 事件循环被 BLE 操作
        # 阻塞时，内层 wait_for 无法触发，需要主线程的外层超时来兜底
        OUTER_TIMEOUT = 20.0  # 主线程外层超时：MTA worker 阻塞时兜底
        try:
            return await asyncio.wait_for(_run_in_mta(factory), timeout=OUTER_TIMEOUT)
        except asyncio.TimeoutError:
            # MTA worker 事件循环被阻塞，重启以恢复
            if self.on_connect_progress:
                try:
                    self.on_connect_progress("连接超时，正在恢复 BLE 线程…")
                except Exception:
                    pass
            _MTA.restart()
            raise RuntimeError(
                "蓝牙连接超时（BLE 操作无响应，已自动恢复）。"
                "请尝试：1) 在 Windows「设置 → 蓝牙」中删除设备后重新配对；"
                "2) 确认设备已开机且在附近；3) 重试连接。"
            )

    async def disconnect(self) -> None:
        _self = self

        def factory():
            async def _do():
                if _self._client is not None:
                    client, _self._client = _self._client, None
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
            return _do()
        return await _run_in_mta(factory)

    @property
    def is_connected(self) -> bool:
        # 注意：_client 只在 MTA 线程里访问/更新，但 bool 引用是原子的，
        # 读端（主线程）直接检查不会出问题。
        c = self._client
        return c is not None and getattr(c, "is_connected", False)

    @property
    def mtu(self) -> int:
        """真实 ATT_MTU；bleak 在 Windows/WinRT 上自动协商。"""
        c = self._client
        if c is not None:
            try:
                return c.mtu_size
            except Exception:
                pass
        return 23

    @property
    def payload_size(self) -> int:
        """常规命令的单次写入载荷上限：MTU-3。"""
        return max(self.mtu - 3, DEFAULT_CHUNK)

    # ============================================================== 写入
    async def write_frame(self, frame: bytes, *, atomic: bool = False) -> None:
        """向 AE21 写入完整协议帧。
        atomic=True 用于 2-2 等必须整帧单写的命令，超过载荷上限时
        直接报错而不是拆分，避免设备解析出错误文件名。
        """
        if self._client is None:
            raise RuntimeError("BLE 未连接")
        limit = self.payload_size
        if atomic and len(frame) > limit:
            raise RuntimeError(
                f"帧长 {len(frame)}B 超过单写上限 {limit}B，"
                "该命令要求整帧单写，请确认 MTU 协商结果")
        _self = self
        _frame = frame
        _atomic = atomic
        _limit = limit

        def factory():
            async def _do():
                import asyncio as _aio
                if _self._client is None:
                    raise RuntimeError("BLE 未连接")
                if len(_frame) <= _limit:
                    await _self._client.write_gatt_char(
                        CHAR_WRITE, _frame, response=False)
                    return
                if _atomic:
                    raise RuntimeError(
                        f"帧长 {len(_frame)}B 超过单写上限 {_limit}B，"
                        "该命令要求整帧单写，请确认 MTU 协商结果")
                for i in range(0, len(_frame), _limit):
                    await _self._client.write_gatt_char(
                        CHAR_WRITE, _frame[i:i + _limit], response=False)
                    await _aio.sleep(0.01)
            return _do()
        return await _run_in_mta(factory)

    # ============================================================== 回调
    # 说明：回调发生在 MTA 线程。为了保持上层（FrameParser/Recorder 等）
    # 逻辑线程归属不变，这里用 caller_loop.call_soon_threadsafe 把用户回调
    # 投递回 connect/pair 时保存的调用方事件循环。若 caller_loop 不存在
    # （源码 CLI 模式等），则直接在本线程调用，行为与改造前一致。
    def _dispatch(self, cb, *args) -> None:
        if cb is None:
            return
        loop = self._caller_loop
        if loop is None or loop.is_closed():
            try:
                cb(*args)
            except Exception:
                logger.exception("BLE 回调异常（直调模式）")
            return
        try:
            loop.call_soon_threadsafe(cb, *args)
        except RuntimeError:
            # loop 已停/关，退化直调
            try:
                cb(*args)
            except Exception:
                logger.exception("BLE 回调异常（call_soon_threadsafe 失败直调）")

    def _notify_main(self, _sender, data: bytearray) -> None:
        self._dispatch(self.on_main, bytes(data))

    def _notify_key(self, _sender, data: bytearray) -> None:
        self._dispatch(self.on_key, bytes(data))

    def _handle_disconnect(self, _client) -> None:
        logger.info("设备已断开")
        self._client = None
        self._dispatch(self.on_disconnect)
