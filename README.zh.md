# NextProto — AI 录音卡 QS668

> 蓝牙连接 QS668 AI 录音笔，支持文件管理、实时录音、语音转写（ASR）、AI 摘要生成。提供 PC 桌面版和微信小程序版。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20WeChat-blue)]()
[![Protocol](https://img.shields.io/badge/BLE%20Protocol-V1.0-green)]()

---

<div align="center">

**[ 🇬🇧 English ](README.md)** &nbsp;&nbsp;|&nbsp;&nbsp; **🇨🇳 中文（当前）**

</div>

---

## 目录

- [功能一览](#功能一览)
- [项目结构](#项目结构)
- [两个版本对比](#两个版本对比)
- [BLE 通讯协议](#ble-通讯协议)
- [PC 桌面版](#pc-桌面版)
  - [安装](#安装)
  - [快速开始](#快速开始)
  - [CLI 命令](#cli-命令)
  - [Web API 接口](#web-api-接口)
  - [ASR 配置](#asr-配置)
  - [AI 摘要配置](#ai-摘要配置)
  - [音量增益](#音量增益)
  - [Ogg Opus 打包](#ogg-opus-打包)
  - [PyInstaller 打包](#pyinstaller-打包)
- [微信小程序](#微信小程序)
  - [搭建](#搭建)
  - [API 配置](#api-配置)
  - [费用估算](#费用估算)
- [协议实现细节](#协议实现细节)
- [测试](#测试)
- [常见问题](#常见问题)
- [参考](#参考)

---

## 功能一览

| 分类 | 功能 |
|---|---|
| 设备信息 | 电量 / 存储容量 / 固件版本 / 授权码 / 时间同步（连接后自动） |
| 文件操作 | 文件列表、WAV/Opus 下载（候选名自动回退）、断点续传、分段下载、删除（二次确认） |
| 实时录音 | 实时音频推流（Opus 码流落盘）、暂停 / 继续 / 停止 |
| 录音控制 | 远程开始 / 保存 / 暂停 / 继续、状态 / 时长 / 文件名查询、增益查询与设置 |
| 语音转写 | 桌面版：本地离线（SenseVoiceSmall + VAD）/ 小程序：云端 API（Paraformer） |
| AI 摘要 | DeepSeek / 本地 Ollama 生成结构化会议纪要，附思维导图（桌面版） |
| 音量增益 | 自动峰值归一化 / 2x/3x/5x 手动增益 / 关闭 — 提升低音量录音的转写准确率 |
| Ogg Opus | 实时 40B 裸 Opus 码流 → 合法 Ogg/Opus 容器（VLC/ffprobe 可播放） |
| 调试 | 只读安全巡检、任意命令封包直发、机身按键事件监听 |

## 项目结构

```
NextProto/
├── desktop/                          # PC 桌面版（Python）
│   ├── recorder/
│   │   ├── ble.py                    # BLE 传输层
│   │   ├── protocol.py               # 协议：帧构造/CRC/OggOpus
│   │   ├── crc16.py                  # CRC-16/XMODEM
│   │   ├── device.py                 # 设备交互
│   │   ├── asr.py                    # 语音转写（本地 ONNX）
│   │   ├── llm.py                    # AI 摘要
│   │   ├── web.py                    # Web 服务（FastAPI）
│   │   └── cli.py                    # 命令行 REPL
│   ├── web/                          # 前端页面
│   ├── tests/                        # 测试
│   ├── main.py                       # 入口
│   └── requirements*.txt             # 依赖
├── miniprogram/                      # 微信小程序版
│   ├── utils/
│   │   ├── crc16.js                  # CRC-16/XMODEM
│   │   ├── protocol.js               # 通讯协议
│   │   ├── ble.js                    # BLE 传输层
│   │   └── recorder.js               # 高层设备逻辑
│   ├── pages/
│   │   ├── scan/                     # 扫描连接页
│   │   ├── files/                    # 文件管理页
│   │   └── transcribe/               # 实时转写页
│   └── app.js                        # 入口
├── README.md                         # 英文版
├── README.zh.md                      # 本文件
└── LICENSE
```

## 两个版本对比

| 特性 | PC 桌面版 (`desktop/`) | 微信小程序 (`miniprogram/`) |
|---|---|---|
| 运行平台 | Windows / macOS | 微信（手机） |
| 蓝牙 | Windows BLE / bleak | 微信 BLE API |
| ASR 转写 | **本地 ONNX**（SenseVoice/Paraformer） | **云端 API**（阿里云百炼） |
| AI 摘要 | DeepSeek API / 本地 Ollama | DeepSeek API |
| 思维导图 | ✅ markmap | ❌ |
| 实时录音 | ✅ Ogg Opus 流式写入 | ✅ Opus 码流接收 |
| 文件下载 | ✅ WAV/Opus 自动回退 | ✅ WAV/Opus 自动回退 |
| 音量增益 | ✅ 自动/2x/3x/5x/关闭 | ❌（服务端处理） |
| 裸 Opus 修复 | ✅ `opus-fix` 命令 + API | ❌ |
| 打包分发 | PyInstaller exe（双击即用） | 扫码即用 |
| 月费 | 0 元（本地模型） | ~10 元（云 API） |
| 语言 | Python 3.10+ / HTML / JS | JavaScript（WXML/WXSS） |

## BLE 通讯协议

基于 QS668 BLE 通讯协议 V1.0（2026-07-10），桌面版和小程序版共用同一协议层。

### GATT 服务与特征

| 特征 | UUID | 方向 | 用途 |
|---|---|---|---|
| Service | `0000AE20-...-00805F9B34FB` | — | 主 BLE 服务 |
| 写特征 | `0000AE21-...-00805F9B34FB` | App → 设备 | 命令写入（无应答） |
| 通知 1 | `0000AE22-...-00805F9B34FB` | 设备 → App | 控制/音频/文件响应 |
| 通知 2 | `0000AE23-...-00805F9B34FB` | 设备 → App | 按键事件通知 |

### 帧格式

```
┌────────┬─────┬────────────┬──────────┬───────────────────────┐
│ MAGIC  │ SEQ │  CRC16    │   LEN    │        DATA           │
│ 0x5A   │ 1B  │ 2B (LE)   │ 2B (LE)  │  TYPE+CMD+PARAMS      │
└────────┴─────┴────────────┴──────────┴───────────────────────┘
```

- **MAGIC**: `0x5A` — 帧同步标记
- **SEQ**: 1 字节 — 序列号（0xFF → 0x00 循环）
- **CRC**: 2 字节（小端）— CRC-16/XMODEM（Poly=0x1021, Init=0x0000），计算范围 = LEN 原始 2 字节 + DATA
- **LEN**: 2 字节（小端）— DATA 长度；最大 8192B；LEN > 8192 视为假帧头
- **DATA**: TYPE（1 字节）+ CMD（1 字节）+ PARAMS（变长）

### 命令类型

| TYPE | 分类 | 关键命令 |
|---|---|---|
| 0 | 控制 | 时间同步(0x00)、电量(0x01)、容量(0x02)、固件(0x03)、授权码(0x0D) |
| 1 | 实时音频 | 开始(0x00)、音频数据(0x01)、停止(0x02)、暂停(0x03)、状态(0x04) |
| 2 | 文件 | 列表(0x00)、导入(0x01)、数据请求(0x02)、结束(0x07)、删除(0x12) |
| 3 | 按键/录音 | 录音开始(0x01)、保存(0x03)、暂停(0x04)、继续(0x05)、状态(0x08)、增益(0x1C) |

### 文件下载协议（TYPE=2, CMD=2）

- 请求参数：`offset:4B (小端) + filename:24B (NUL 填充)` — 共 36B
- **必须一次 GATT 写入**（拆分发送设备返回 code=1）
- 候选名回退：`base.wav → base.opus → 原始截断名`
- 仅在设备返回 code=1 且未收到数据时切换文件名

### 文件列表格式（TYPE=2, CMD=0 响应）

- `count:4B (大端) + N × 28B 条目`
- 每条目：`time:4B (大端) + size:4B (大端) + name:20B (NUL 填充)`

### 超时策略

| 操作 | 超时 |
|---|---|
| 普通命令 | 5 秒 |
| 文件列表（无 CMD=0x18 收尾帧） | 1.2s 空闲返回 |
| 文件下载 | 12s 空闲 |
| 删除应答 | 3s（旧固件可能不回） |

---

## PC 桌面版

### 安装

需要 Python 3.10+，Windows / macOS / Linux（系统蓝牙适配器支持 BLE）。

```bash
cd desktop

# 核心功能（连接/下载/录音控制）
pip install -r requirements.txt

# 可选：语音转文字（本地离线 ASR）
pip install -r requirements-asr.txt

# 可选：Web 界面
pip install -r requirements-web.txt
```

> **ASR 依赖注意**：torchaudio 大版本必须与已安装的 torch 配对（如 torch 2.9.x ↔ torchaudio 2.9.x）。首次 `transcribe` 会从 ModelScope 自动下载 SenseVoiceSmall 模型（约 900MB，缓存于 `~/.cache/modelscope`），之后完全离线。

### 快速开始

```bash
# CLI REPL 模式（下载保存到 ./downloads）
python main.py

# 指定下载目录
python main.py -o /path/to/output

# 调试日志（显示收发帧 hex）
python main.py -v

# Web 界面模式（默认 http://127.0.0.1:8000）
python main.py --web

# 自定义端口
python main.py --web --port 9000
```

**真机典型流程（REPL）：**

```
record> scan              # 扫描（找不到时试 scan compat）
record> connect 0         # 连接
record> smoke             # 只读巡检（安全无写入）
record> list              # 文件列表
record> download 0        # 下载 WAV（自动校验 RIFF 长度）
record> transcribe 0      # 语音转文字（未下载会自动先下载）
```

### CLI 命令

```
连接管理：
  scan [秒] [compat]          扫描录音笔（compat 列出全部设备）
  connect <序号|地址>          连接
  disconnect / status         断开 / 查看状态

设备信息：
  smoke                       只读巡检
  info | battery | capacity | version | auth | synctime

文件操作：
  list                        文件列表
  download <序号|all> [offset] [文件名]  下载（候选名自动回退）
  seg <序号> <start> <end>    分段下载
  transcribe <序号|all|文件> [语言]      语音转文字
  delete <序号> / deleteall   删除（输入 yes 确认）

实时录音：
  rt start|stop|pause|resume  实时音频推流

录音控制：
  rec start|save|pause|resume|state|time|name
  gain [1|2|3]                查询或设置增益

OPUS 修复：
  opus-fix <文件名> [--replace]  裸 Opus → Ogg/Opus

调试：
  raw <type> <cmd> [hex]       任意命令封包
  rawframe <完整帧hex>         直接发送完整帧
```

### Web API 接口

运行 `python main.py --web` 时可用：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/status` | 连接状态与设备信息 |
| GET | `/api/battery` | 电量 |
| GET | `/api/capacity` | 存储容量 |
| GET | `/api/version` | 固件版本 |
| POST | `/api/scan` | 开始 BLE 扫描 |
| POST | `/api/connect` | 连接设备 |
| POST | `/api/disconnect` | 断开 |
| POST | `/api/list` | 文件列表 |
| POST | `/api/download` | 下载文件 |
| POST | `/api/delete` | 删除文件 |
| POST | `/api/transcribe` | 语音转文字（参数：`model`, `spk`, `language`, `volume_gain`） |
| POST | `/api/summary` | 生成 AI 摘要 |
| POST | `/api/save_summary` | 保存编辑后的摘要 |
| POST | `/api/convert_raw_opus` | 裸 Opus → Ogg/Opus |
| POST | `/api/rt_start` | 开始实时录音 |
| POST | `/api/rt_stop` | 停止实时录音 |
| WS | `/ws` | WebSocket 实时事件（下载进度、按键事件） |

### ASR 配置

支持模型（全部本地运行，无需 API Key）：

| 模型 | 大小 | 语言 | 来源 |
|---|---|---|---|
| SenseVoiceSmall（默认） | ~242MB ONNX | zh/ja/yue/en/ko | ModelScope `iic/` |
| Paraformer-zh | ~220MB ONNX | zh only | ModelScope `iic/` |

音频自动重采样到 16kHz 单声道后再转写。

### AI 摘要配置

支持三种方式（API Key 存于 `downloads/.llm_config.json`）：

| 提供商 | 端点 | 模型 | 备注 |
|---|---|---|---|
| DeepSeek API（默认） | `api.deepseek.com` | `deepseek-chat` | 云端，需 API Key |
| API 中转站（OpenAI 格式） | 自定义 | 自定义 | OpenAI 兼容端点 |
| 本地 Ollama | `localhost:11434` | 自定义 | 完全离线，需安装 Ollama |

### 音量增益

低音量录音可在转写前放大：

| 选项 | 说明 |
|---|---|
| `auto`（默认） | 峰值归一化到 0.9（不削波的最大音量） |
| `2x` / `3x` / `5x` | 固定倍数增益（可能削波） |
| `off` | 不处理 |

### Ogg Opus 打包

QS668 实时推送固定 40 字节裸 Opus 包，不可直接播放。`protocol.py` 中的 `Qs668OggOpusWriter` 类负责包装：

- 打开文件时写 OpusHead(BOS) + OpusTags 页
- 每 50 个包（约 1 秒）：刷一页 Ogg data page 带 CRC
- 关闭时：刷剩余 buffer + 写 EOS 标记
- 结果：合法 Ogg/Opus 文件，VLC/ffprobe/pydub 可播放

**修复历史文件：**
```bash
# CLI
record> opus-fix recording.opus
record> opus-fix recording.opus --replace   # 覆盖原文件（自动备份）
```

### PyInstaller 打包

```bash
cd desktop
build.bat          # Windows：生成 dist/AIRecorder.exe
```

- ffmpeg 二进制随程序分发
- 下载文件保存在 exe 同级 `downloads` 目录
- Windows 和 macOS 需分别打包

---

## 微信小程序

### 搭建

1. 下载[微信开发者工具](https://developers.weixin.qq.com/miniprogram/dev/devtools/download.html)
2. 导入 `miniprogram/` 目录
3. 填入 AppID（或使用测试号）
4. 点击「预览」，手机微信扫码
5. 授权蓝牙权限
6. 扫描并连接 QS668 录音笔

### API 配置

在转写页点击「⚙️ API 配置」：

**ASR（阿里云百炼）：**
- 注册地址：[dashscope.aliyuncs.com](https://dashscope.aliyuncs.com)
- 创建 API Key
- 默认模型：`paraformer-1`
- 计费：0.288 元/小时（0.00008 元/秒），每月免费 10 小时

**LLM（DeepSeek）：**
- 注册地址：[platform.deepseek.com](https://platform.deepseek.com)
- 创建 API Key
- 默认模型：`deepseek-v4-flash`
- 计费：输入约 3 元/百万 Token（闲时减半）

### 费用估算

| 服务 | 计费方式 | 单价 | 月费估算（每天 1 小时） |
|---|---|---|---|
| 阿里云百炼 Paraformer | 按时长 | 0.288 元/小时 | ~9 元 |
| DeepSeek V4-Flash | 按 Token | ~3 元/百万输入 Token | ~1 元 |
| 微信小程序 | 免费 | 0 元 | 0 元 |
| **合计** | | | **~10 元/月** |

> 轻度用户可能 0 成本（每月 10 小时 ASR 免费额度）。

---

## 协议实现细节

- **帧构造**：`[0x5A][SEQ][CRC:2B LE][LEN:2B LE][DATA=TYPE+CMD+PARAMS]`；CRC-16/XMODEM，范围 = LEN 原始 2 字节 + DATA
- **GATT**：AE21 写（无应答）、AE22 主通知（先订阅）、AE23 按键通知；各自独立流式解析缓存，支持半帧/多帧/噪声重同步；LEN>8192 视为假帧头
- **下载（2-2）**：`offset:4B LE + filename:24B NUL 填充` = 36B 必须一次 GATT 写入；候选名 `base.wav → base.opus → 截断名`；仅在 code=1 且未收到数据时换名
- **文件列表**：`count:4B BE + N×28B(time:4B BE + size:4B BE + name:20B)`；个别固件 time 字段为 Unix 时间戳，按启发式区分
- **落盘安全**：同名文件自动加 `_时长s_大小` 后缀，不覆盖已有文件
- **Ogg Opus**：40B 裸 Opus 包 → 流式包装为合法 Ogg/Opus 容器

协议实现已与厂家官方测试页（[nextproto.top/qs668/](https://nextproto.top/qs668/)）逐段比对验证。

## 测试

```bash
cd desktop
python -m unittest discover tests -v
```

40 项测试，无需真机、无需蓝牙：

- `test_protocol.py` — 帧构造/解析、CRC 校验向量、36B 真机 36B 帧逐字节复现、文件列表大端解码、WAV 检查、时间戳启发式
- `test_device.py` — FakeTransport 端到端：请求应答匹配、多帧列表组装、下载会话与候选名回退、续传不换名、分段下载、删除兼容
- `test_asr.py` — 转写模块依赖缺失兜底
- `test_web.py` — Web 层 REST 接口与模拟设备调用链

## 常见问题

**扫不到设备**：确认录音笔未被手机 App 占用；试 `scan compat` 列出全部设备手动选择
**`transcribe` 报未安装 funasr**：执行 `pip install -r requirements-asr.txt`
**torchaudio 导入报 WinError 127**：torch 与 torchaudio 版本不配对，安装同号 torchaudio
**蓝牙配对超时**：手机取消配对录音笔；重启录音笔；在 Windows 重新配对
**实时录音文件无法播放**：使用 `opus-fix` 命令包装裸 Opus 包
**录音声音小**：转写设置中使用音量增益功能（自动/2x/3x/5x）

## 参考

- **协议源项目**：[lomehong/record](https://github.com/lomehong/record/tree/master/) — QS668 BLE 通讯协议 V1.0 实现
- **厂家资料**：[nextproto.top/materials](https://nextproto.top/materials)（需凭证编号）
- **厂家测试页**：[nextproto.top/qs668/](https://nextproto.top/qs668/)
- **ASR 模型**：[ModelScope FunASR](https://github.com/modelscope/FunASR)
- **BLE 库**：[bleak](https://github.com/hbldh/bleak)
- **微信小程序**：[官方文档](https://developers.weixin.qq.com/miniprogram/dev/framework/)

---

### License

MIT License - 详见 [LICENSE](LICENSE)
