# AI 录音卡 QS668 微信小程序

> 蓝牙连接 QS668 AI 录音笔，实现文件管理、实时录音、语音转写（ASR）和 AI 摘要生成的一站式微信小程序。

---

# AI Recording Card QS668 WeChat Mini Program

> A WeChat Mini Program that connects to the QS668 AI recording pen via Bluetooth, supporting file management, real-time recording, speech-to-text (ASR), and AI summary generation.

---

## 中文文档

### 功能概览

| 功能 | 说明 |
|---|---|
| 🔍 蓝牙扫描 | 自动识别 QS668 设备，支持兼容扫描模式 |
| 📡 设备连接 | GATT 连接、MTU 协商、AE22/AE23 双通道 Notify 订阅 |
| 🔋 设备信息 | 电量、存储容量、固件版本、时间同步 |
| 📁 文件管理 | 录音列表、下载（WAV/Opus 自动切换）、播放、分享、删除 |
| 🎙️ 实时录音 | 开始/暂停/继续/停止，实时接收 40B Opus 码流 |
| 📝 语音转写 | 阿里云百炼 Paraformer 录音文件识别 |
| 🤖 AI 摘要 | DeepSeek 大模型生成结构化会议纪要 |
| ⚙️ 配置管理 | API Key 本地存储，支持 ASR + LLM 双服务配置 |

### 技术架构

```
┌─────────────┐     BLE GATT      ┌──────────────┐
│  微信小程序   │ ←──────────────→ │  QS668 录音笔  │
│  (前端)      │   AE21 写入       │              │
│             │   AE22 Notify     │              │
│             │   AE23 Notify     │              │
└──────┬──────┘                   └──────────────┘
       │
       │ HTTPS API
       ├──────────────→ 阿里云百炼 (ASR 语音转写)
       │                 dashscope.aliyuncs.com
       │
       └──────────────→ DeepSeek (LLM AI 摘要)
                         api.deepseek.com
```

### BLE 通讯协议

基于 QS668 BLE 通讯协议 V1.0：

| 参数 | 值 |
|---|---|
| Service UUID | `0000AE20-0000-1000-8000-00805F9B34FB` |
| 写特征 (App→Dev) | `0000AE21-0000-1000-8000-00805F9B34FB` |
| 通知特征 1 (控制/音频/文件) | `0000AE22-0000-1000-8000-00805F9B34FB` |
| 通知特征 2 (按键事件) | `0000AE23-0000-1000-8000-00805F9B34FB` |
| MAGIC | `0x5A` |
| 帧格式 | `MAGIC + SEQ + CRC16(LE) + LEN(LE) + DATA` |
| CRC | CRC-16/XMODEM (Poly=0x1021, Init=0x0000) |
| 最大数据长度 | 8192B |

#### 命令类型

| TYPE | 说明 | 关键命令 |
|---|---|---|
| 0 (控制) | 时间同步、电量、容量、固件版本 | `0x00`~`0x0D` |
| 1 (实时音频) | 开始/数据/停止/暂停/状态 | `0x00`~`0x04` |
| 2 (文件) | 列表/导入/数据/结束/删除 | `0x00`~`0x12` |
| 3 (按键) | 录音控制、增益、状态查询 | `0x01`~`0x1C` |

### 项目结构

```
录音卡小程序/
├── app.js                  # 全局入口
├── app.json                # 页面路由与权限
├── app.wxss                # 全局样式（暗色主题）
├── project.config.json     # 微信开发者工具配置
├── sitemap.json
├── utils/
│   ├── crc16.js            # CRC-16/XMODEM 校验
│   ├── protocol.js         # 协议常量、帧构造、流式解析、字段解码
│   ├── ble.js              # BLE 传输层（扫描/连接/MTU/Notify/写入）
│   └── recorder.js         # 高层逻辑（请求/应答/文件/下载/实时音频）
└── pages/
    ├── scan/               # 扫描连接页
    │   ├── scan.js / scan.wxml / scan.wxss / scan.json
    ├── files/              # 文件管理页
    │   ├── files.js / files.wxml / files.wxss / files.json
    └── transcribe/         # 实时转写页
        ├── transcribe.js / transcribe.wxml / transcribe.wxss / transcribe.json
```

### 使用方法

#### 1. 环境准备

1. 下载并安装 [微信开发者工具](https://developers.weixin.qq.com/miniprogram/dev/devtools/download.html)
2. 申请微信小程序 AppID（或使用测试号）
3. 手机打开蓝牙

#### 2. 导入项目

1. 打开微信开发者工具
2. 选择「导入项目」
3. 项目目录选择 `录音卡小程序/` 文件夹
4. 填入自己的 AppID
5. 点击确定

#### 3. 配置 API Key

在转写页点击「⚙️ API 配置」：

**ASR（阿里云百炼）：**
- 前往 [dashscope.aliyuncs.com](https://dashscope.aliyuncs.com) 注册
- 创建 API Key
- 填入 Key，默认模型 `paraformer-1`

**LLM（DeepSeek）：**
- 前往 [platform.deepseek.com](https://platform.deepseek.com) 注册
- 创建 API Key
- 填入 Key，默认模型 `deepseek-v4-flash`

#### 4. 真机预览

1. 点击开发者工具的「预览」按钮
2. 用手机微信扫码打开小程序
3. 允许蓝牙权限
4. 扫描并连接 QS668 录音笔

### 费用估算

| 服务 | 计费方式 | 单价 | 月费估算（每天1小时） |
|---|---|---|---|
| 阿里云百炼 Paraformer | 按时长 | 0.288 元/小时 | ~9 元 |
| DeepSeek V4-Flash | 按 Token | ~3 元/百万输入 Token | ~1 元 |
| 微信小程序 | 免费 | 0 元 | 0 元 |
| **合计** | | | **~10 元/月** |

> 注：阿里云百炼每月免费 10 小时转写额度，轻度用户可能 0 成本。

### 与 Python 桌面版的关系

本项目是 [AI 录音卡桌面版](../record/) 的微信小程序移植版，协议层完全一致：

| Python 桌面版 | 小程序版 | 说明 |
|---|---|---|
| `crc16.py` | `utils/crc16.js` | CRC-16/XMODEM |
| `protocol.py` | `utils/protocol.js` | 帧构造与解析 |
| `ble.py` | `utils/ble.js` | BLE 传输层 |
| `device.py` | `utils/recorder.js` | 高层设备逻辑 |
| `web.py` | 各 page | 用户界面 |

**核心差异：** 桌面版 ASR 跑本地模型（SenseVoice/Paraformer ONNX），小程序版走云端 API。

---

## English Documentation

### Feature Overview

| Feature | Description |
|---|---|
| 🔍 BLE Scanning | Auto-detect QS668 devices with compatibility scan mode |
| 📡 Connection | GATT connect, MTU negotiation, AE22/AE23 dual-channel Notify |
| 🔋 Device Info | Battery, storage capacity, firmware version, time sync |
| 📁 File Management | List, download (WAV/Opus auto-switch), play, share, delete |
| 🎙️ Real-time Recording | Start/pause/resume/stop, receive 40B Opus packets |
| 📝 Speech-to-Text | Alibaba Cloud DashScope Paraformer ASR |
| 🤖 AI Summary | DeepSeek LLM for structured meeting notes |
| ⚙️ Configuration | API Key local storage, dual-service (ASR + LLM) config |

### Technical Architecture

```
┌─────────────┐     BLE GATT      ┌──────────────┐
│  WeChat      │ ←──────────────→ │  QS668        │
│  Mini Program│   AE21 Write      │  Recording    │
│  (Frontend)  │   AE22 Notify     │  Pen           │
│              │   AE23 Notify     │               │
└──────┬───────┘                   └──────────────┘
       │
       │ HTTPS API
       ├──────────────→ Alibaba Cloud DashScope (ASR)
       │                 dashscope.aliyuncs.com
       │
       └──────────────→ DeepSeek (LLM AI Summary)
                         api.deepseek.com
```

### BLE Protocol

Based on QS668 BLE Communication Protocol V1.0:

| Parameter | Value |
|---|---|
| Service UUID | `0000AE20-0000-1000-8000-00805F9B34FB` |
| Write Char (App→Dev) | `0000AE21-0000-1000-8000-00805F9B34FB` |
| Notify Char 1 (Ctrl/Audio/File) | `0000AE22-0000-1000-8000-00805F9B34FB` |
| Notify Char 2 (Key Events) | `0000AE23-0000-1000-8000-00805F9B34FB` |
| MAGIC | `0x5A` |
| Frame Format | `MAGIC + SEQ + CRC16(LE) + LEN(LE) + DATA` |
| CRC | CRC-16/XMODEM (Poly=0x1021, Init=0x0000) |
| Max Data Length | 8192B |

#### Command Types

| TYPE | Description | Key Commands |
|---|---|---|
| 0 (Control) | Time sync, battery, capacity, version | `0x00`~`0x0D` |
| 1 (Realtime Audio) | Start/data/stop/pause/state | `0x00`~`0x04` |
| 2 (File) | List/import/data/end/delete | `0x00`~`0x12` |
| 3 (Key) | Recording control, gain, state query | `0x01`~`0x1C` |

### Project Structure

```
录音卡小程序/
├── app.js                  # App entry
├── app.json                # Routes & permissions
├── app.wxss                # Global styles (dark theme)
├── project.config.json     # DevTools config
├── sitemap.json
├── utils/
│   ├── crc16.js            # CRC-16/XMODEM
│   ├── protocol.js         # Constants, frame build/parse, field decode
│   ├── ble.js              # BLE transport (scan/connect/MTU/Notify/write)
│   └── recorder.js         # High-level logic (req/resp/file/download/realtime)
└── pages/
    ├── scan/               # Scan & connect page
    │   ├── scan.js / scan.wxml / scan.wxss / scan.json
    ├── files/              # File management page
    │   ├── files.js / files.wxml / files.wxss / files.json
    └── transcribe/         # Real-time transcribe page
        ├── transcribe.js / transcribe.wxml / transcribe.wxss / transcribe.json
```

### Usage

#### 1. Prerequisites

1. Download [WeChat DevTools](https://developers.weixin.qq.com/miniprogram/dev/devtools/download.html)
2. Register a WeChat Mini Program AppID (or use test ID)
3. Enable Bluetooth on your phone

#### 2. Import Project

1. Open WeChat DevTools
2. Select "Import Project"
3. Choose the `录音卡小程序/` folder
4. Enter your AppID
5. Click OK

#### 3. Configure API Keys

On the transcribe page, tap "⚙️ API Config":

**ASR (Alibaba Cloud DashScope):**
- Register at [dashscope.aliyuncs.com](https://dashscope.aliyuncs.com)
- Create an API Key
- Enter the key, default model `paraformer-1`

**LLM (DeepSeek):**
- Register at [platform.deepseek.com](https://platform.deepseek.com)
- Create an API Key
- Enter the key, default model `deepseek-v4-flash`

#### 4. Real Device Preview

1. Click "Preview" in DevTools
2. Scan the QR code with WeChat on your phone
3. Grant Bluetooth permissions
4. Scan and connect to your QS668 recording pen

### Cost Estimation

| Service | Billing | Unit Price | Monthly Est. (1hr/day) |
|---|---|---|---|
| Alibaba DashScope Paraformer | Per duration | ¥0.288/hour | ~¥9 |
| DeepSeek V4-Flash | Per token | ~¥3/M input tokens | ~¥1 |
| WeChat Mini Program | Free | ¥0 | ¥0 |
| **Total** | | | **~¥10/month** |

> Note: Alibaba Cloud offers 10 free hours of ASR per month. Light users may incur zero cost.

### Relationship with Python Desktop Version

This project is the WeChat Mini Program port of the [AI Recording Card Desktop Version](../record/). The protocol layer is identical:

| Python Desktop | Mini Program | Description |
|---|---|---|
| `crc16.py` | `utils/crc16.js` | CRC-16/XMODEM |
| `protocol.py` | `utils/protocol.js` | Frame build & parse |
| `ble.py` | `utils/ble.js` | BLE transport |
| `device.py` | `utils/recorder.js` | High-level device logic |
| `web.py` | Pages | UI |

**Key Difference:** Desktop runs local ASR models (SenseVoice/Paraformer ONNX), while the Mini Program uses cloud API.

---

### License

MIT License - See [LICENSE](LICENSE)

### Related

- [QS668 BLE Communication Protocol V1.0](https://nextproto.top/materials)
- [AI Recording Card Desktop Version (Python)](../record/)
- [WeChat Mini Program Documentation](https://developers.weixin.qq.com/miniprogram/dev/framework/)
