# NextProto — AI 录音卡 QS668

> 蓝牙连接 QS668 AI 录音笔，支持文件管理、实时录音、语音转写（ASR）、AI 摘要生成。提供 PC 桌面版和微信小程序版。

---

# NextProto — AI Recording Card QS668

> Connect to QS668 AI recording pen via Bluetooth. Supports file management, real-time recording, speech-to-text (ASR), and AI summary generation. Available as both a PC desktop application and a WeChat Mini Program.

---

## 中文文档

### 项目结构

```
NextProto/
├── desktop/               # PC 桌面版（Python + Web）
│   ├── recorder/           # 核心模块
│   │   ├── ble.py         # BLE 蓝牙传输层
│   │   ├── protocol.py     # 通讯协议（帧构造/CRC/OggOpus）
│   │   ├── crc16.py        # CRC-16/XMODEM
│   │   ├── device.py       # 设备交互（连接/下载/实时录音）
│   │   ├── asr.py          # 语音转写（本地 ONNX 模型）
│   │   ├── llm.py          # AI 摘要（DeepSeek/Ollama）
│   │   ├── web.py          # Web 服务（FastAPI）
│   │   └── cli.py          # 命令行交互
│   ├── web/                # 前端页面（HTML/JS/CSS）
│   ├── tests/              # 测试
│   ├── tools/              # 工具脚本
│   ├── main.py             # 入口
│   ├── build.spec          # PyInstaller 打包配置
│   └── requirements.txt    # 依赖
├── miniprogram/            # 微信小程序版
│   ├── utils/
│   │   ├── crc16.js        # CRC-16/XMODEM
│   │   ├── protocol.js     # 通讯协议
│   │   ├── ble.js          # BLE 传输层
│   │   └── recorder.js     # 高层设备逻辑
│   ├── pages/
│   │   ├── scan/           # 扫描连接页
│   │   ├── files/          # 文件管理页
│   │   └── transcribe/     # 实时转写页
│   └── app.js              # 入口
└── README.md               # 本文件
```

### 两个版本对比

| 特性 | PC 桌面版 (desktop/) | 微信小程序 (miniprogram/) |
|---|---|---|
| 运行平台 | Windows / macOS | 微信（手机） |
| 蓝牙 | Windows BLE / bleak | 微信 BLE API |
| ASR 转写 | **本地模型** SenseVoice/Paraformer ONNX | **云端 API** 阿里云百炼 |
| AI 摘要 | DeepSeek API / 本地 Ollama | DeepSeek API |
| 实时录音 | ✅ Ogg Opus 流式写入 | ✅ Opus 码流接收 |
| 文件下载 | ✅ WAV/Opus | ✅ WAV/Opus |
| 思维导图 | ✅ markmap | ❌ |
| 打包分发 | PyInstaller exe | 扫码即用 |
| 月费 | 0 元（本地模型） | ~10 元（云 API） |
| 语言 | Python + HTML/JS | JavaScript (WXML/WXSS) |

### BLE 通讯协议（两版共用）

| 参数 | 值 |
|---|---|
| Service UUID | `0000AE20-0000-1000-8000-00805F9B34FB` |
| 写特征 (App→Dev) | `0000AE21-0000-1000-8000-00805F9B34FB` |
| 通知特征 1 | `0000AE22-0000-1000-8000-00805F9B34FB` |
| 通知特征 2 | `0000AE23-0000-1000-8000-00805F9B34FB` |
| MAGIC | `0x5A` |
| 帧格式 | `MAGIC + SEQ + CRC16(LE) + LEN(LE) + DATA` |
| CRC | CRC-16/XMODEM (Poly=0x1021, Init=0x0000) |

### PC 桌面版使用方法

```bash
cd desktop
pip install -r requirements.txt
python main.py
```

浏览器打开 `http://localhost:8000`

详细说明见 [desktop/README.md](desktop/README.md)

### 微信小程序使用方法

1. 下载 [微信开发者工具](https://developers.weixin.qq.com/miniprogram/dev/devtools/download.html)
2. 导入项目，目录选 `miniprogram/`
3. 填入自己的 AppID
4. 真机预览，扫码连接录音笔

详细说明见 [miniprogram/README.md](miniprogram/README.md)

### 协议参考

- QS668 BLE 通讯协议 V1.0（2026-07-10）
- 获取地址：https://nextproto.top/materials

---

## English Documentation

### Project Structure

```
NextProto/
├── desktop/               # PC Desktop (Python + Web)
│   ├── recorder/           # Core modules
│   │   ├── ble.py          # BLE transport
│   │   ├── protocol.py     # Protocol (frame/CRC/OggOpus)
│   │   ├── crc16.py        # CRC-16/XMODEM
│   │   ├── device.py       # Device interaction
│   │   ├── asr.py          # Speech-to-text (local ONNX)
│   │   ├── llm.py          # AI summary (DeepSeek/Ollama)
│   │   ├── web.py          # Web server (FastAPI)
│   │   └── cli.py          # CLI
│   ├── web/                # Frontend (HTML/JS/CSS)
│   ├── tests/              # Tests
│   ├── tools/              # Utilities
│   ├── main.py             # Entry point
│   ├── build.spec          # PyInstaller config
│   └── requirements.txt    # Dependencies
├── miniprogram/            # WeChat Mini Program
│   ├── utils/
│   │   ├── crc16.js        # CRC-16/XMODEM
│   │   ├── protocol.js     # Protocol
│   │   ├── ble.js          # BLE transport
│   │   └── recorder.js     # High-level logic
│   ├── pages/
│   │   ├── scan/           # Scan & connect
│   │   ├── files/          # File management
│   │   └── transcribe/     # Real-time transcribe
│   └── app.js              # Entry
└── README.md               # This file
```

### Version Comparison

| Feature | Desktop (desktop/) | Mini Program (miniprogram/) |
|---|---|---|
| Platform | Windows / macOS | WeChat (Mobile) |
| Bluetooth | Windows BLE / bleak | WeChat BLE API |
| ASR | **Local model** SenseVoice/Paraformer ONNX | **Cloud API** Alibaba DashScope |
| AI Summary | DeepSeek API / Local Ollama | DeepSeek API |
| Real-time Recording | ✅ Ogg Opus streaming | ✅ Opus packet receive |
| File Download | ✅ WAV/Opus | ✅ WAV/Opus |
| Mind Map | ✅ markmap | ❌ |
| Distribution | PyInstaller exe | Scan QR code |
| Monthly Cost | ¥0 (local model) | ~¥10 (cloud API) |
| Language | Python + HTML/JS | JavaScript (WXML/WXSS) |

### BLE Protocol (shared)

| Parameter | Value |
|---|---|
| Service UUID | `0000AE20-0000-1000-8000-00805F9B34FB` |
| Write Char (App→Dev) | `0000AE21-0000-1000-8000-00805F9B34FB` |
| Notify Char 1 | `0000AE22-0000-1000-8000-00805F9B34FB` |
| Notify Char 2 | `0000AE23-0000-1000-8000-00805F9B34FB` |
| MAGIC | `0x5A` |
| Frame Format | `MAGIC + SEQ + CRC16(LE) + LEN(LE) + DATA` |
| CRC | CRC-16/XMODEM (Poly=0x1021, Init=0x0000) |

### Desktop Usage

```bash
cd desktop
pip install -r requirements.txt
python main.py
```

Open browser at `http://localhost:8000`

See [desktop/README.md](desktop/README.md) for details.

### Mini Program Usage

1. Download [WeChat DevTools](https://developers.weixin.qq.com/miniprogram/dev/devtools/download.html)
2. Import project from `miniprogram/`
3. Enter your AppID
4. Preview on real device, scan to connect pen

See [miniprogram/README.md](miniprogram/README.md) for details.

### Protocol Reference

- QS668 BLE Communication Protocol V1.0 (2026-07-10)
- Available at: https://nextproto.top/materials

---

### License

MIT License - See [LICENSE](LICENSE)
