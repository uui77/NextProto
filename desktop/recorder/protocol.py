"""录音笔 BLE 通讯协议：命令常量、帧构造、流式解析、字段解码。
通用帧格式（协议第 3 节）：
    [0]   MAGIC   1B  固定 0x5A
    [1]   SEQ     1B  0~255 循环递增
    [2:4] CRC     2B  LE, CRC-16/XMODEM(LEN原始2B + DATA)
    [4:6] LEN     2B  LE, DATA 真实字节数
    [6:]  DATA    LEN [TYPE:1B][CMD:1B][PARAMS...]；ACK 可仅含 TYPE
字节序：帧头 LEN/CRC 为小端；文件列表 count/time/size 为大端。
"""
from __future__ import annotations
import struct
from dataclasses import dataclass, field
from typing import Iterator, List, Optional
from .crc16 import crc16_xmodem
MAGIC = 0x5A
HEADER_LEN = 6
MAX_DATA_LEN = 8192  # LEN 字段合理上限，超出视为假帧头（参考厂家测试页）
# ---------------------------------------------------------------- DATA 类型
TYPE_CONTROL = 0    # 控制命令：时间、电量、容量、固件、授权码
TYPE_REALTIME = 1   # 实时音频 / 转写
TYPE_FILE = 2       # 文件操作
TYPE_KEY = 3        # 按键 / 录音控制
# --------------------------------------------------------- 控制命令 TYPE=0
CTRL_SYNC_TIME = 0        # App→Dev 同步时间: year:2B LE + M/D/h/m/s 各1B
CTRL_GET_CAPACITY = 1     # App→Dev 获取容量
CTRL_CAPACITY_RESP = 2    # Dev→App 容量应答: remain:4B LE + total:4B LE
CTRL_GET_BATTERY = 3      # App→Dev 获取电量
CTRL_BATTERY_RESP = 4     # Dev→App 电量应答: 1B 0~100; 110=充电中
CTRL_GET_VERSION = 10     # App→Dev 获取固件版本
CTRL_VERSION_RESP = 11    # Dev→App 固件版本应答: 6B ASCII
CTRL_GET_AUTH = 12        # App→Dev 获取授权码
CTRL_AUTH_RESP = 13       # Dev→App 授权码应答
BATTERY_CHARGING = 110
# ----------------------------------------------------- 实时音频命令 TYPE=1
RT_START = 0              # App→Dev 开始实时转写；Dev→App 本次录音文件名
RT_AUDIO_DATA = 1         # Dev→App 实时音频数据（OPUS 系码流）
RT_STOP = 2               # App→Dev 结束实时转写
RT_PAUSE_RESUME = 3       # App→Dev 暂停/继续: 1B 0=继续 1=暂停
RT_DEV_STATE = 4          # Dev→App 设备端状态: 0=继续 1=暂停 2=停止
# --------------------------------------------------------- 文件命令 TYPE=2
FILE_LIST_REQ = 0         # App→Dev 获取文件列表
FILE_LIST_DATA = 1        # Dev→App 文件列表数据: count:4B BE + N×28B
FILE_IMPORT_REQ = 2       # App→Dev 请求导入: offset:4B LE + filename:24B
FILE_IMPORT_START = 3     # Dev→App 开始导入（实际导入文件名）
FILE_DATA = 4             # Dev→App 文件数据分片
FILE_IMPORT_END = 5       # Dev→App 导入结束: 1B 状态码
FILE_IMPORT_ABORT = 7     # App→Dev 终止导入
FILE_DELETE_ONE = 8       # App→Dev 删除单个文件（28B条目）
FILE_DELETE_ALL = 9       # App→Dev 删除全部文件
FILE_DELETE_ALL_RESP = 10  # Dev→App 删除全部应答: 0成功 1失败
FILE_ABORT_RESP = 11      # Dev→App 终止导入应答
FILE_IMPORT_SEG = 12      # App→Dev 分段导入: start:4B LE + end:4B LE + name
FILE_DELETE_ONE_RESP = 13  # Dev→App 删除单个应答: 0成功 1失败
FILE_LIST_DONE = 18       # Dev→App 列表发送完毕: 1B 0=完成
# 导入结束状态码（2-5）
IMPORT_END_OK = 0          # 完成
IMPORT_END_NOT_FOUND = 1   # 文件不存在
IMPORT_END_BAD_OFFSET = 2  # offset 过大
IMPORT_END_STOPPED = 3     # 其他停止
# --------------------------------------------------- 按键/录音控制 TYPE=3
KEY_REC_START = 1         # 开始录音
KEY_REC_START_RESP = 2    # 开始结果: 1成功 2失败
KEY_REC_SAVE = 3          # 保存录音
KEY_REC_SAVE_RESP = 4
KEY_REC_PAUSE = 5         # 暂停录音
KEY_REC_PAUSE_RESP = 6
KEY_REC_RESUME = 7        # 继续录音
KEY_REC_RESUME_RESP = 8
KEY_GET_STATE = 19        # 获取录音状态
KEY_STATE_RESP = 20       # 1=录音中 2=未录音 3=暂停
KEY_GET_TIME = 21         # 获取录音时间
KEY_TIME_RESP = 22        # duration:2B LE + currentSize:4B LE
KEY_GET_FILENAME = 23     # 获取当前文件名
KEY_FILENAME_RESP = 24
KEY_GET_GAIN = 25         # 获取增益: 1低 2中 3高
KEY_GAIN_RESP = 26
KEY_SET_GAIN = 27         # 设置增益 1~3
KEY_SET_GAIN_RESP = 28    # 0成功 1失败
FILENAME_FIELD_LEN = 24   # 2-2 请求中 filename 字段固定长度
LIST_NAME_LEN = 20        # 列表条目 name 字段固定长度
LIST_ENTRY_LEN = 28       # 列表单条目长度: time4 + size4 + name20
# ================================================================ 帧构造
class SeqGenerator:
    """包序号发生器，0~255 循环递增。"""
    def __init__(self) -> None:
        self._seq = -1
    def next(self) -> int:
        self._seq = (self._seq + 1) & 0xFF
        return self._seq
def build_frame(seq: int, data: bytes) -> bytes:
    """构造完整协议帧：MAGIC + SEQ + CRC(LE) + LEN(LE) + DATA。
    CRC 计算范围为 LEN 原始 2 字节 + DATA。
    """
    length = struct.pack("<H", len(data))
    crc = crc16_xmodem(length + data)
    return bytes([MAGIC, seq & 0xFF]) + struct.pack("<H", crc) + length + data
def build_command(seq: int, type_: int, cmd: int, params: bytes = b"") -> bytes:
    """构造 DATA=[TYPE][CMD][PARAMS...] 的命令帧。"""
    return build_frame(seq, bytes([type_, cmd]) + params)
def encode_sync_time(year: int, month: int, day: int,
                     hour: int, minute: int, second: int) -> bytes:
    """0-0 同步时间参数：year:2B LE + month/day/hour/minute/second 各1B。"""
    return struct.pack("<H5B", year, month, day, hour, minute, second)
def encode_filename_24(name: str) -> bytes:
    """将文件名编码为固定 24B 字段，NUL 填充；超长截断。"""
    raw = name.encode("utf-8")[:FILENAME_FIELD_LEN]
    return raw.ljust(FILENAME_FIELD_LEN, b"\x00")
def build_import_request(seq: int, filename: str, offset: int = 0) -> bytes:
    """构造 2-2 文件导入请求帧（完整 36B，必须一次 GATT 写入）。"""
    params = struct.pack("<I", offset) + encode_filename_24(filename)
    return build_command(seq, TYPE_FILE, FILE_IMPORT_REQ, params)
def build_segment_request(seq: int, filename: str, start: int, end: int) -> bytes:
    """构造 2-12 分段导入请求帧：start:4B LE + end:4B LE + filename。"""
    params = struct.pack("<II", start, end) + encode_filename_24(filename)
    return build_command(seq, TYPE_FILE, FILE_IMPORT_SEG, params)
# ================================================================ 帧解析
@dataclass
class Frame:
    """解析后的一个完整协议帧。"""
    seq: int
    data: bytes
    @property
    def type(self) -> int:
        return self.data[0]
    @property
    def cmd(self) -> Optional[int]:
        # DATA 仅含 TYPE 一个字节时按 ACK 处理，无 CMD
        return self.data[1] if len(self.data) >= 2 else None
    @property
    def is_ack(self) -> bool:
        return len(self.data) == 1
    @property
    def body(self) -> bytes:
        """TYPE、CMD 之后的参数/载荷字节。"""
        return self.data[2:] if len(self.data) >= 2 else b""
class FrameParser:
    """流式帧解析器。
    AE22 与 AE23 必须各用一个独立实例，避免两个通知特征的
    字节交织破坏半帧。一个通知可能含半帧，也可能含多帧。
    """
    def __init__(self, name: str = "") -> None:
        self.name = name
        self._buf = bytearray()
        self.crc_errors = 0
    def feed(self, chunk: bytes) -> Iterator[Frame]:
        """喂入一段通知字节，产出所有可完整解析的帧。"""
        self._buf.extend(chunk)
        while True:
            frame = self._try_parse_one()
            if frame is None:
                return
            yield frame
    def _try_parse_one(self) -> Optional[Frame]:
        buf = self._buf
        # 丢弃 MAGIC 之前的噪声字节
        while buf and buf[0] != MAGIC:
            buf.pop(0)
        if len(buf) < HEADER_LEN:
            return None
        seq = buf[1]
        crc_recv, length = struct.unpack_from("<HH", buf, 2)
        if length > MAX_DATA_LEN:
            # LEN 异常：当前 MAGIC 是假帧头，跳一字节重同步
            del buf[0]
            return self._try_parse_one()
        if len(buf) < HEADER_LEN + length:
            return None  # 等待后续通知补齐
        data = bytes(buf[HEADER_LEN:HEADER_LEN + length])
        crc_calc = crc16_xmodem(bytes(buf[4:6]) + data)
        if crc_calc != crc_recv:
            # CRC 错误：记录原始 hex，丢弃 MAGIC 字节后重新同步
            self.crc_errors += 1
            bad = bytes(buf[:HEADER_LEN + length])
            del buf[0]
            raise CrcError(self.name, seq, bad)
        del buf[:HEADER_LEN + length]
        if length == 0:
            return self._try_parse_one()  # 空 DATA 帧无意义，继续
        return Frame(seq=seq, data=data)
    def reset(self) -> None:
        self._buf.clear()
class CrcError(Exception):
    """CRC 校验失败；携带原始帧字节便于记录 hex。"""
    def __init__(self, source: str, seq: int, raw: bytes) -> None:
        super().__init__(
            f"CRC mismatch on {source or 'stream'} seq={seq}: {raw.hex(' ')}")
        self.source = source
        self.seq = seq
        self.raw = raw
# ================================================================ 字段解码
@dataclass
class FileEntry:
    """文件列表条目（7.1 节，28B，整数按大端）。"""
    duration: int   # 录音时长，秒（个别固件可能为绝对时间戳）
    size: int       # 设备内压缩文件大小，Byte
    name: str       # 20B 固定字段解出的截断名，如 note20260710-162938.
    raw: bytes = field(default=b"", repr=False)
    def candidate_names(self) -> List[str]:
        """下载候选文件名：优先 base.opus（体积小、BLE 传输快），其次 base.wav。"""
        base = self.name.rstrip(".")
        # 截断名形如 note20260710-162938. —— 需重建扩展名
        for known_ext in (".opus", ".wav", ".mp3"):
            if base.lower().endswith(known_ext):
                base = base[:-len(known_ext)]
                break
        return [base + ".opus", base + ".wav", self.name]
    @property
    def estimated_wav_size(self) -> int:
        """WAV 进度估算：时长 × 32000 B/s + 44（16kHz/16bit/mono）。"""
        return self.duration * 32000 + 44
def decode_file_list(body: bytes) -> List[FileEntry]:
    """解码 2-1 文件列表帧 body：count:4B BE + N×28B 条目。"""
    if len(body) < 4:
        return []
    (count,) = struct.unpack_from(">I", body, 0)
    entries: List[FileEntry] = []
    offset = 4
    for _ in range(count):
        if offset + LIST_ENTRY_LEN > len(body):
            break  # 帧内条目不足声明数量，保守截止
        raw = body[offset:offset + LIST_ENTRY_LEN]
        duration, size = struct.unpack_from(">II", raw, 0)
        name = raw[8:8 + LIST_NAME_LEN].split(b"\x00", 1)[0].decode(
            "utf-8", errors="replace")
        entries.append(FileEntry(duration=duration, size=size,
                                 name=name, raw=raw))
        offset += LIST_ENTRY_LEN
    return entries
def decode_capacity(body: bytes) -> tuple:
    """解码 0-2 容量应答：remain:4B LE + total:4B LE，单位按 1KB 显示。"""
    remain, total = struct.unpack_from("<II", body, 0)
    return remain, total
def decode_battery(body: bytes) -> int:
    """解码 0-4 电量应答：0~100，110 表示充电中。"""
    return body[0]
def decode_record_time(body: bytes) -> tuple:
    """解码 3-22 录音时间应答：duration:2B LE + currentSize:4B LE。"""
    duration, size = struct.unpack_from("<HI", body, 0)
    return duration, size
@dataclass
class WavInfo:
    """WAV 头检验结果（7.4 节验收项）。"""
    ok: bool                 # RIFF/WAVE 头有效且声明长度等于实际长度
    declared: int = 0        # RIFF 声明总长（含8B头）
    channels: int = 0
    sample_rate: int = 0
    bits_per_sample: int = 0
def inspect_wav(data: bytes) -> WavInfo:
    """校验 WAV：RIFF/WAVE 魔数、声明长度与实际长度一致，并提取音频参数。"""
    if len(data) < 44 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        return WavInfo(ok=False)
    declared = struct.unpack_from("<I", data, 4)[0] + 8
    channels = struct.unpack_from("<H", data, 22)[0]
    sample_rate = struct.unpack_from("<I", data, 24)[0]
    bits = struct.unpack_from("<H", data, 34)[0]
    return WavInfo(ok=declared == len(data), declared=declared,
                   channels=channels, sample_rate=sample_rate,
                   bits_per_sample=bits)
def is_wav(data: bytes) -> bool:
    """校验 WAV 头：bytes[0:4]=RIFF 且 bytes[8:12]=WAVE。"""
    return len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WAVE"
# 列表 time 字段启发式：个别固件用绝对时间戳而非时长（7.1 节）
_TS_MIN = 946684800    # 2000-01-01
_TS_MAX = 4102444800   # 2100-01-01
def is_epoch_timestamp(value: int) -> bool:
    """判断列表 time 字段是否更像 Unix 时间戳而非录音时长。"""
    return _TS_MIN < value < _TS_MAX


# ============================================================= QS668 Raw OPUS → Ogg/Opus 包装
# 设备实时推送的 RT_AUDIO_DATA(TYPE=1 CMD=1) 是「固定 40B 的 raw OPUS 帧」：
#   - Opus config=9，每 40B = 1 个 20ms 单声道帧，采样率等效 16kHz
#   - 直接把这些字节写磁盘是“裸码流”，ffmpeg/VLC/任何播放器都无法解码
#   - 必须按 RFC 7845 包装成 Ogg Opus 容器才是合法可播放的 .opus 文件
#
# 实现参考：厂家配套「解码 OPUS 文件脚本.py」
#   - 每 50 个 raw packet 合成 1 个 Ogg page（约 1s 音）
#   - Ogg granule 时间线固定使用 48kHz（RFC 7845 要求）：
#       20ms × 48000 = 960 samples / packet
#   - Ogg serial = 0x51533638（即 "QS68" 四字 HEX，保持与工具一致便于交叉验证）

_OGG_CRC_POLY = 0x04C11DB7


def _build_ogg_crc_table() -> list:
    table = []
    for value in range(256):
        reg = value << 24
        for _ in range(8):
            if reg & 0x80000000:
                reg = ((reg << 1) ^ _OGG_CRC_POLY) & 0xFFFFFFFF
            else:
                reg = (reg << 1) & 0xFFFFFFFF
        table.append(reg)
    return table


OGG_CRC_TABLE = _build_ogg_crc_table()


def ogg_crc32(data: bytes) -> int:
    """Ogg 页校验：CRC-32 / ISO 3309，初始 0，结果用同页 offset 22~25 小端覆盖。"""
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ OGG_CRC_TABLE[((crc >> 24) & 0xFF) ^ byte]
    return crc


def ogg_make_page(payloads: list, granule: int, serial: int,
                  seq: int, flags: int) -> bytes:
    """构建 1 个 Ogg page。

    payloads: 每个元素是 1 个 Opus packet 原始字节
    granule: 本 page 结束时的 48kHz granule 位置（RFC 7845）
    serial: Ogg logical bitstream serial number
    seq: page 序号（0 起）
    flags: 0x02=BOS(首页)  0x04=EOS(末页)  0x00=普通
    """
    body = b"".join(payloads)
    # lacing table：每个 packet 的长度按 255 进位表示
    laces = bytearray()
    for p in payloads:
        n = len(p)
        while n >= 255:
            laces.append(255)
            n -= 255
        laces.append(n)

    header = bytearray(27 + len(laces))
    header[0:4] = b"OggS"                           # capture_pattern
    header[4] = 0                                     # stream_structure_version
    header[5] = flags                                 # header_type_flag
    struct.pack_into("<Q", header, 6, granule)       # granule_position
    struct.pack_into("<I", header, 14, serial)       # bitstream_serial_number
    struct.pack_into("<I", header, 18, seq)          # page_sequence_number
    # header[22:26] page_checksum 先填 0，算完整个 page 再回写
    header[26] = len(laces)                           # number_page_segments
    header[27:27 + len(laces)] = bytes(laces)        # segment_table

    page = bytes(header) + body
    # 回写 CRC
    crc = ogg_crc32(page)
    page = page[:22] + struct.pack("<I", crc) + page[26:]
    return page


class Qs668OggOpusWriter:
    """把 QS668 固定 40B raw OPUS packets 流式包装成合法 Ogg Opus 文件。

    用法：
        w = Qs668OggOpusWriter(fp, sample_rate=16000)
        w.write_packet(frame_body)   # 每次设备推送 RT_AUDIO_DATA 都写 1 次
        w.close()                   # 必须调用，否则最后一页没 EOS
    """

    # 每 packet 对应 48kHz granule 960 sample（20ms）
    GRANULE_PER_PACKET = 960
    # 每 50 个 packet 合成 1 个 Ogg data page（约 1 秒）
    PACKETS_PER_PAGE = 50
    # 与「解码 OPUS 文件脚本.py」一致：serial = "QS68"
    DEFAULT_SERIAL = 0x51533638

    def __init__(self, fp, sample_rate: int = 16000,
                 packets_per_page: int = PACKETS_PER_PAGE,
                 serial: int = DEFAULT_SERIAL) -> None:
        self._fp = fp
        self._sample_rate = int(sample_rate)
        self._packets_per_page = max(1, int(packets_per_page))
        self._serial = int(serial)
        self._buf: list = []
        self._seq = 0
        self._granule = 0
        self._closed = False
        self._write_header_pages()

    # ---------- internal ----------
    def _write_header_pages(self) -> None:
        sr = self._sample_rate
        head = (
            b"OpusHead"
            + bytes([1, 1])                         # Version=1, Channel count=1
            + struct.pack("<H", 312)                # Pre-skip (samples @48kHz)
            + struct.pack("<I", sr)                 # Input sample rate (Hz)
            + struct.pack("<h", 0)                  # Output gain (dB, Q8.8)
            + bytes([0])                            # Channel mapping family=0
        )
        tags = (
            b"OpusTags"
            + struct.pack("<I", len(b"QS668"))
            + b"QS668"
            + struct.pack("<I", 0)                  # user_comments 长度
        )
        self._fp.write(ogg_make_page([head], 0, self._serial, self._seq, 0x02))  # BOS
        self._seq += 1
        self._fp.write(ogg_make_page([tags], 0, self._serial, self._seq, 0x00))
        self._seq += 1

    def _flush_page(self, is_eos: bool = False) -> None:
        if not self._buf:
            if is_eos:
                # RFC 3533 允许 EOS page 空 body；这里保证必有 EOS 以便 demuxer 收尾
                self._fp.write(ogg_make_page([], self._granule, self._serial,
                                              self._seq, 0x04))
                self._seq += 1
            return
        self._granule += self.GRANULE_PER_PACKET * len(self._buf)
        flags = 0x04 if is_eos else 0x00
        self._fp.write(ogg_make_page(self._buf, self._granule, self._serial,
                                      self._seq, flags))
        self._seq += 1
        self._buf.clear()

    # ---------- public ----------
    def write_packet(self, packet: bytes) -> None:
        """写 1 个 raw Opus packet（QS668 通常是 40B；非 40B 也支持）。

        packet 长度不是 40 时也能写，但 GRANULE_PER_PACKET 仍按 960 累加，
        回放时间会偏移。因此仅在确定 packet 是 20ms frame 时使用。
        """
        if self._closed:
            raise RuntimeError("Qs668OggOpusWriter 已关闭，不能再写 packet")
        if not packet:
            return
        self._buf.append(bytes(packet))
        if len(self._buf) >= self._packets_per_page:
            self._flush_page(is_eos=False)

    def close(self) -> None:
        if self._closed:
            return
        self._flush_page(is_eos=True)
        try:
            self._fp.flush()
        finally:
            self._closed = True


def wrap_raw_opus_file(src_path, dst_path, sample_rate: int = 16000) -> tuple:
    """把历史写出来的「裸 40B 一帧的 raw .opus」就地转换为合法 Ogg Opus。

    Returns (packet_count, duration_seconds)
    """
    from pathlib import Path
    src = Path(src_path)
    dst = Path(dst_path)
    raw = src.read_bytes()
    if len(raw) % 40 != 0:
        # 兼容：允许最后一帧不足 40B（异常中断场景），但 granule 仍按 20ms 近似累加
        pass
    packets = [raw[i:i + 40] for i in range(0, len(raw), 40)]
    with open(dst, "wb") as f:
        w = Qs668OggOpusWriter(f, sample_rate=sample_rate)
        for p in packets:
            w.write_packet(p)
        w.close()
    return len(packets), len(packets) * 0.02