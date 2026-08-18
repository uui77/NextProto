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
from typing import Callable, List, Optional
from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
logger = logging.getLogger(__name__)
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
    """
    def __init__(self) -> None:
        self._client: Optional[BleakClient] = None
        self.on_main: Optional[Callable[[bytes], None]] = None
        self.on_key: Optional[Callable[[bytes], None]] = None
        self.on_disconnect: Optional[Callable[[], None]] = None
    # ------------------------------------------------------------ 扫描
    @staticmethod
    async def scan(timeout: float = 6.0,
                   compat: bool = False) -> List[BLEDevice]:
        """扫描广播含 AE20 服务或名称匹配 QS668/CB08 的设备。
        compat=True 时返回全部有名设备（兼容广播不携带服务 UUID
        的固件，对应厂家测试页的"兼容扫描"）。
        """
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
    # ------------------------------------------------------------ 连接
    CONNECT_TIMEOUT = 15.0       # BLE 连接/订阅显式超时（秒）。Windows 默认挂 30s 太长，15s 失败快一些。
    PAIR_TIMEOUT    = 20.0       # 配对超时（秒）：需要用户在系统弹窗里点确认，留久一点。

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
        import asyncio as _aio
        addr = getattr(device, "address", str(device))
        need_cool_down = False   # 配对过程是否真的建立/断开过 GATT 连接（需要冷却）

        # ---------- Step 1: Windows 优先走 WinRT 专属配对路径（不打扰 GATT 连接） ----------
        if sys.platform.startswith("win"):
            paired_winrt = await self._pair_via_winrt(addr, timeout=self.PAIR_TIMEOUT)
            if paired_winrt is True:
                logger.info("WinRT 配对成功（无 GATT 连接干扰）")
                return True
            if paired_winrt == "timed_out":
                raise RuntimeError(
                    f"配对等待超时（{self.PAIR_TIMEOUT:.0f}s）：请在系统弹出的配对对话框中"
                    f"确认（录音笔常见 PIN：0000 / 1234），再重试。"
                )
            # paired_winrt == False 或 "unsupported" → 继续走跨平台/兜底

        # ---------- Step 2: 跨平台 bleak pair()（会建立临时 GATT 连接，用完需冷却） ----------
        client = BleakClient(device)
        try:
            ok = await _aio.wait_for(
                client.pair(protection_level=None),
                timeout=self.PAIR_TIMEOUT,
            )
            if ok:
                need_cool_down = True
                return True
        except NotImplementedError:
            logger.info("bleak.pair() 后端不支持，最后尝试打开 Windows 设置页")
        except _aio.TimeoutError:
            logger.warning("bleak.pair() 超时 %ds", self.PAIR_TIMEOUT)
        except Exception as exc:
            logger.info("bleak.pair() 非致命失败（进入兜底）：%s", exc)
        finally:
            try:
                if getattr(client, "is_connected", False):
                    await client.disconnect()
                    need_cool_down = True
            except Exception:
                pass

        # ---------- Step 3: 兜底——打开 Windows 蓝牙设置页 ----------
        if sys.platform.startswith("win"):
            self._launch_windows_bluetooth_settings(addr)
            return "settings_opened"
        return False

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
                )
                from winsdk.windows.devices.bluetooth import BluetoothDevice
            except ModuleNotFoundError:
                # 旧包命名
                from winrt.windows.devices.enumeration import (
                    DeviceInformation, DevicePairingKinds, DevicePairingResultStatus,
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
            loop = _aio.get_running_loop()
            bt_dev = await _aio.wait_for(
                loop.run_in_executor(None, lambda: BluetoothDevice.from_bluetooth_address(mac_int)),
                timeout=10.0,
            )
            if bt_dev is None:
                logger.info("WinRT BluetoothDevice.from_bluetooth_address 返回 None")
                return False
        except Exception as exc:
            logger.info("获取 BluetoothDevice 对象失败：%s", exc)
            return False

        # 3) 取 DeviceInformation.Pairing 对象并调 PairAsync
        pairing = getattr(bt_dev.device_information, "pairing", None)
        if pairing is None:
            logger.info("DeviceInformation.Pairing 不可用，降级打开设置页")
            return "unsupported"

        # Just Works + Confirm + ProvidePin 三种最常见配对方式都勾上
        kinds = (
            int(DevicePairingKinds.CONFIRM_ONLY)
            | int(DevicePairingKinds.PROVIDE_PIN)
            | int(DevicePairingKinds.CONFIRM_PIN_MATCH)
        )
        try:
            pair_operation = pairing.custom.pair_async(kinds)
            # 转成可 await 的 Future（winsdk 返回 IAsyncOperation，用 asyncio.wrap_future）
            try:
                pair_fut = _aio.wrap_future(pair_operation)
            except Exception:
                # 有些 winsdk 版本用 __await__ 直接
                pair_fut = _aio.ensure_future(_aio.coroutine(lambda: pair_operation)())
            res = await _aio.wait_for(pair_fut, timeout=timeout)
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
            if int(status) == int(DevicePairingResultStatus.ALREADY_PAIRED):
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
        import asyncio as _aio
        max_retries = 2
        backoff = 2.0
        last_exc = None
        for attempt in range(max_retries + 1):
            client = BleakClient(
                device, disconnected_callback=self._handle_disconnect)
            unreachable_this_attempt = False
            try:
                await _aio.wait_for(client.connect(), timeout=self.CONNECT_TIMEOUT)
                self._client = client
                # 订阅 AE22（设备回报 AE22 经常需要先完成配对，否则 WinRT 层会挂住直到超时）
                await _aio.wait_for(
                    client.start_notify(CHAR_NOTIFY_MAIN, self._notify_main),
                    timeout=self.CONNECT_TIMEOUT,
                )
                try:
                    await _aio.wait_for(
                        client.start_notify(CHAR_NOTIFY_KEY, self._notify_key),
                        timeout=self.CONNECT_TIMEOUT,
                    )
                except Exception as exc:  # 部分固件可能无 AE23
                    logger.warning("AE23 订阅失败（忽略）：%s", exc)
                return  # 正常退出
            except _aio.TimeoutError as exc:
                last_exc = exc
                friendly = (
                    "蓝牙连接超时（订阅通知无响应）。这通常表示录音笔需要先"
                    "在 Windows「设置 → 蓝牙和其他设备」里手动完成"
                    "「配对」（常见配对码 0000 / 1234），配对成功后再回到本页面点"
                    "「连接」；也可以直接点设备旁边的「🔗 配对」按钮触发系统配对弹窗。"
                )
            except Exception as exc:
                last_exc = exc
                friendly = _describe_ble_error(exc)
                msg_lower = str(exc).lower()
                unreachable_this_attempt = (
                    "unreachable" in msg_lower or "could not get gatt services" in msg_lower
                )
            # 走到这儿 = 本 attempt 失败了，清理 client
            try:
                if client.is_connected:
                    await client.disconnect()
            except Exception:
                pass
            self._client = None
            # 是否要重试？只有 Unreachable 才重试，其他错误直接抛出（更符合直觉）
            should_retry = (
                unreachable_this_attempt and attempt < max_retries
            )
            if not should_retry:
                # friendly 如果是 None 就抛原始异常；否则抛中文友好版
                if "friendly" not in locals() or not friendly:
                    raise last_exc
                raise RuntimeError(friendly) from last_exc
            # 进入重试
            wait = backoff * (attempt + 1)
            logger.info(
                "连接遇到 GATT Unreachable，%ds 后进行第 %d/%d 次重试…",
                wait, attempt + 2, max_retries + 1,
            )
            await _aio.sleep(wait)
    async def disconnect(self) -> None:
        if self._client is not None:
            client, self._client = self._client, None
            try:
                await client.disconnect()
            except Exception:
                pass
    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected
    @property
    def mtu(self) -> int:
        """真实 ATT_MTU；bleak 在 Windows/WinRT 上自动协商。"""
        if self._client is not None:
            try:
                return self._client.mtu_size
            except Exception:
                pass
        return 23
    @property
    def payload_size(self) -> int:
        """常规命令的单次写入载荷上限：MTU-3。"""
        return max(self.mtu - 3, DEFAULT_CHUNK)
    # ------------------------------------------------------------ 写入
    async def write_frame(self, frame: bytes, *, atomic: bool = False) -> None:
        """向 AE21 写入完整协议帧。
        atomic=True 用于 2-2 等必须整帧单写的命令，超过载荷上限时
        直接报错而不是拆分，避免设备解析出错误文件名。
        """
        if self._client is None:
            raise RuntimeError("BLE 未连接")
        limit = self.payload_size
        if len(frame) <= limit:
            await self._client.write_gatt_char(
                CHAR_WRITE, frame, response=False)
            return
        if atomic:
            raise RuntimeError(
                f"帧长 {len(frame)}B 超过单写上限 {limit}B，"
                "该命令要求整帧单写，请确认 MTU 协商结果")
        # 普通长帧按 MTU-3 分片顺序写入
        for i in range(0, len(frame), limit):
            await self._client.write_gatt_char(
                CHAR_WRITE, frame[i:i + limit], response=False)
            await asyncio.sleep(0.01)
    # ------------------------------------------------------------ 回调
    def _notify_main(self, _sender, data: bytearray) -> None:
        if self.on_main is not None:
            self.on_main(bytes(data))
    def _notify_key(self, _sender, data: bytearray) -> None:
        if self.on_key is not None:
            self.on_key(bytes(data))
    def _handle_disconnect(self, _client) -> None:
        logger.info("设备已断开")
        self._client = None
        if self.on_disconnect is not None:
            self.on_disconnect()
