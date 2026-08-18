# NextProto — AI Recording Card QS668

> A complete Bluetooth solution for the QS668 AI recording pen. Connect via BLE to manage files, record in real-time, transcribe speech to text (ASR), and generate AI meeting summaries. Available as both a **PC desktop application** (Python + Web) and a **WeChat Mini Program**.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20WeChat-blue)]()
[![Protocol](https://img.shields.io/badge/BLE%20Protocol-V1.0-green)]()

---

<div align="center">

**🇬🇧 English (Current)** &nbsp;&nbsp;|&nbsp;&nbsp; **[ 🇨🇳 中文 ](README.zh.md)**

</div>

---

## Table of Contents

- [Overview](#overview)
- [Features](#features)
- [Project Structure](#project-structure)
- [Version Comparison](#version-comparison)
- [BLE Communication Protocol](#ble-communication-protocol)
- [Desktop Version (Python)](#desktop-version-python)
  - [Installation](#installation)
  - [Quick Start](#quick-start)
  - [CLI Commands](#cli-commands)
  - [Web API Endpoints](#web-api-endpoints)
  - [ASR Configuration](#asr-configuration)
  - [AI Summary Configuration](#ai-summary-configuration)
  - [Volume Gain](#volume-gain)
  - [Ogg Opus Packaging](#ogg-opus-packaging)
  - [PyInstaller Build](#pyinstaller-build)
- [WeChat Mini Program](#wechat-mini-program)
  - [Setup](#setup)
  - [API Configuration](#api-configuration)
  - [Cost Estimation](#cost-estimation)
- [Protocol Implementation Details](#protocol-implementation-details)
- [Testing](#testing)
- [FAQ](#faq)
- [References](#references)

---

## Overview

NextProto is a full-stack Bluetooth solution for the QS668 (broadcast name CB08) AI recording pen. It implements the complete BLE Communication Protocol V1.0, covering all 4 command types (control, real-time audio, file operations, key/recording control). The project provides two independent front-ends sharing the same protocol layer:

1. **PC Desktop** (`desktop/`) — Python + [bleak](https://github.com/hbldh/bleak) + FastAPI Web UI, with local offline ASR (FunASR SenseVoiceSmall/Paraformer ONNX)
2. **WeChat Mini Program** (`miniprogram/`) — JavaScript (WXML/WXSS), with cloud ASR (Alibaba Cloud DashScope Paraformer)

Both versions have been verified on real hardware (QS668): scan → connect → inspect → list → download → transcribe → summarize.

## Features

| Category | Feature |
|---|---|
| Device Info | Battery / Storage capacity / Firmware version / Auth code / Time sync (auto on connect) |
| File Operations | File list, WAV/Opus download (auto candidate name fallback), resume, segmented download, delete (with confirmation) |
| Real-time Recording | Live audio streaming, Opus packet receive, start/pause/resume/stop |
| Recording Control | Remote start/save/pause/resume, status/time/filename query, gain query & set |
| Speech-to-Text | Desktop: local offline (SenseVoiceSmall + VAD) / Mini Program: cloud API (Paraformer) |
| AI Summary | Structured meeting notes via DeepSeek / local Ollama, with mind map (desktop) |
| Volume Gain | Auto peak normalization / 2x/3x/5x manual gain / off — improves transcription for low-volume recordings |
| Ogg Opus | Real-time 40B raw Opus packets → valid Ogg/Opus container (playable in VLC/ffprobe) |
| Debug | Read-only smoke test, raw command send, key event monitoring |

## Project Structure

```
NextProto/
├── desktop/                          # PC Desktop Version (Python)
│   ├── recorder/
│   │   ├── ble.py                    # BLE transport (bleak wrapper, MTU, Notify)
│   │   ├── protocol.py               # Protocol: frame build/parse, CRC, OggOpus writer
│   │   ├── crc16.py                  # CRC-16/XMODEM implementation
│   │   ├── device.py                 # Device logic: connect/download/realtime/convert
│   │   ├── asr.py                    # Speech-to-text (FunASR ONNX, volume gain)
│   │   ├── llm.py                    # AI summary (DeepSeek/Ollama, structured+mindmap)
│   │   ├── web.py                    # Web server (FastAPI + WebSocket)
│   │   └── cli.py                    # CLI REPL
│   ├── web/                          # Frontend static files
│   ├── tests/                        # Unit tests (40 tests, no hardware needed)
│   ├── main.py                       # Entry point
│   ├── build.spec / build.bat        # PyInstaller config
│   └── requirements*.txt             # Dependencies
├── miniprogram/                      # WeChat Mini Program Version
│   ├── utils/
│   │   ├── crc16.js                  # CRC-16/XMODEM (ported from Python)
│   │   ├── protocol.js               # Protocol: constants, frame, parse, decode
│   │   ├── ble.js                    # BLE transport (wx BLE API wrapper)
│   │   └── recorder.js               # High-level device logic
│   ├── pages/
│   │   ├── scan/                     # Scan & connect page
│   │   ├── files/                    # File management page
│   │   └── transcribe/               # Real-time transcribe & AI summary page
│   └── app.js                        # App entry
├── README.md                         # This file (English)
├── README.zh.md                      # Chinese version
└── LICENSE                           # MIT
```

## Version Comparison

| Feature | Desktop (`desktop/`) | Mini Program (`miniprogram/`) |
|---|---|---|
| Platform | Windows / macOS | WeChat (Mobile) |
| Bluetooth | Windows BLE / bleak | WeChat BLE API |
| ASR Engine | **Local ONNX** (SenseVoice/Paraformer) | **Cloud API** (Alibaba DashScope) |
| AI Summary | DeepSeek API / Local Ollama | DeepSeek API |
| Mind Map | ✅ markmap | ❌ |
| Real-time Recording | ✅ Ogg Opus streaming write | ✅ Opus packet receive |
| File Download | ✅ WAV/Opus with auto fallback | ✅ WAV/Opus with auto fallback |
| Volume Gain | ✅ Auto/2x/3x/5x/off | ❌ (handled server-side) |
| Raw OPUS Fix | ✅ `opus-fix` CLI command + API | ❌ |
| Distribution | PyInstaller exe (double-click) | Scan QR code (instant) |
| Monthly Cost | ¥0 (local models) | ~¥10 (cloud API) |
| Language | Python 3.10+ / HTML / JS | JavaScript (WXML/WXSS) |

## BLE Communication Protocol

Based on QS668 BLE Communication Protocol V1.0 (2026-07-10). Both desktop and mini program implement the same protocol.

### GATT Service & Characteristics

| Characteristic | UUID | Direction | Purpose |
|---|---|---|---|
| Service | `0000AE20-...-00805F9B34FB` | — | Primary BLE service |
| Write | `0000AE21-...-00805F9B34FB` | App → Device | Command write (no response) |
| Notify 1 | `0000AE22-...-00805F9B34FB` | Device → App | Control/Audio/File responses |
| Notify 2 | `0000AE23-...-00805F9B34FB` | Device → App | Key event notifications |

### Frame Format

```
┌────────┬─────┬────────────┬──────────┬───────────────────────┐
│ MAGIC  │ SEQ │  CRC16    │   LEN    │        DATA           │
│ 0x5A   │ 1B  │ 2B (LE)   │ 2B (LE)  │  TYPE+CMD+PARAMS      │
└────────┴─────┴────────────┴──────────┴───────────────────────┘
```

- **MAGIC**: `0x5A` — frame synchronization marker
- **SEQ**: 1 byte — sequence number (wraps 0xFF → 0x00)
- **CRC**: 2 bytes (little-endian) — CRC-16/XMODEM (Poly=0x1021, Init=0x0000)
  - CRC calculation scope: LEN original 2 bytes + DATA
- **LEN**: 2 bytes (little-endian) — DATA length; max 8192B; LEN > 8192 treated as false frame header
- **DATA**: TYPE (1 byte) + CMD (1 byte) + PARAMS (variable)

### Command Types

| TYPE | Category | Key Commands |
|---|---|---|
| 0 | Control | Time sync (0x00), Battery (0x01), Capacity (0x02), Firmware (0x03), Auth code (0x0D) |
| 1 | Real-time Audio | Start (0x00), Audio data (0x01), Stop (0x02), Pause (0x03), State (0x04) |
| 2 | File | List (0x00), Import (0x01), Data request (0x02), End (0x07), Delete (0x12) |
| 3 | Key/Recording | Rec start (0x01), Save (0x03), Pause (0x04), Resume (0x05), State (0x08), Gain (0x1C) |

### File Download Protocol (TYPE=2, CMD=2)

- Request params: `offset:4B (LE) + filename:24B (NUL-padded)` — total 36B
- **Must be sent as a single GATT write** (splitting causes device error code=1)
- Candidate name fallback: `base.wav → base.opus → original truncated name`
- Only switch name when device returns code=1 AND no data received yet

### File List Format (TYPE=2, CMD=0 response)

- `count:4B (big-endian) + N × 28B entries`
- Each entry: `time:4B (BE) + size:4B (BE) + name:20B (NUL-padded)`

### Timeout Strategy

| Operation | Timeout |
|---|---|
| Normal command | 5 seconds |
| File list (no CMD=0x18 end frame) | 1.2s idle return |
| File download | 12s idle |
| Delete response | 3s (old firmware may not respond) |

---

## Desktop Version (Python)

### Installation

Requires Python 3.10+, Windows / macOS / Linux (system Bluetooth with BLE support).

```bash
cd desktop

# Core functionality (connect/download/recording control)
pip install -r requirements.txt

# Optional: Speech-to-text (local offline ASR)
pip install -r requirements-asr.txt

# Optional: Web UI
pip install -r requirements-web.txt
```

> **ASR dependency note**: torchaudio major version must match torch (e.g., torch 2.9.x ↔ torchaudio 2.9.x). First `transcribe` run auto-downloads SenseVoiceSmall model (~900MB, cached at `~/.cache/modelscope`), then fully offline.

### Quick Start

```bash
# CLI REPL mode (downloads saved to ./downloads)
python main.py

# Specify download directory
python main.py -o /path/to/output

# Verbose debug logging (shows frame hex)
python main.py -v

# Web UI mode (default: http://127.0.0.1:8000)
python main.py --web

# Custom port
python main.py --web --port 9000
```

**Typical real-device flow (REPL):**

```
record> scan              # Scan for devices (try 'scan compat' if not found)
record> connect 0         # Connect to first device
record> smoke             # Read-only inspection (safe, no writes)
record> list              # Fetch file list
record> download 0        # Download as WAV (auto RIFF validation)
record> transcribe 0      # Speech-to-text (auto-downloads if needed)
```

### CLI Commands

```
Connection:
  scan [seconds] [compat]      Scan for recording pen (compat lists all named devices)
  connect <index|address>      Connect to scanned device
  disconnect / status          Disconnect / Show connection status & MTU

Device Info:
  smoke                        Read-only inspection (battery/capacity/firmware/auth/
                               recording state/time/filename/gain/file list)
  info | battery | capacity | version | auth | synctime

File Operations:
  list                         Fetch file list
  download <index|all> [offset] [filename]
                               Download (WAV preferred, auto candidate name fallback)
  seg <index> <start> <end>    Segmented download (2-12 byte range)
  transcribe <index|all|file> [auto|zh|en|yue|ja|ko]
                               Speech-to-text, saves .txt
  delete <index> / deleteall  Delete (type 'yes' to confirm)

Real-time Recording:
  rt start|stop|pause|resume   Real-time audio streaming (Opus saved to local file)

Recording Control:
  rec start|save|pause|resume|state|time|name
  gain [1|2|3]                 Query or set gain (1=low, 2=mid, 3=high)

OPUS Fix:
  opus-fix <filename> [--replace]
                               Convert raw 40B Opus packets to valid Ogg/Opus

Debug:
  raw <type> <cmd> [params_hex]    Send arbitrary protocol command
  rawframe <full_frame_hex>        Send raw frame (for packet capture replay)
```

### Web API Endpoints

When running `python main.py --web`:

| Method | Path | Description |
|---|---|---|
| GET | `/api/status` | Connection status & device info |
| GET | `/api/battery` | Battery level |
| GET | `/api/capacity` | Storage capacity |
| GET | `/api/version` | Firmware version |
| POST | `/api/scan` | Start BLE scan |
| POST | `/api/connect` | Connect to device |
| POST | `/api/disconnect` | Disconnect |
| POST | `/api/list` | Fetch file list |
| POST | `/api/download` | Download file |
| POST | `/api/delete` | Delete file |
| POST | `/api/transcribe` | Speech-to-text (params: `model`, `spk`, `language`, `volume_gain`) |
| POST | `/api/summary` | Generate AI summary |
| POST | `/api/save_summary` | Save edited summary to `.summary.json` |
| POST | `/api/convert_raw_opus` | Convert raw Opus to valid Ogg/Opus |
| POST | `/api/rt_start` | Start real-time recording |
| POST | `/api/rt_stop` | Stop real-time recording |
| WS | `/ws` | WebSocket for real-time events (download progress, key events) |

### ASR Configuration

Supported models (all run locally, no API key needed):

| Model | Size | Languages | Source |
|---|---|---|---|
| SenseVoiceSmall (default) | ~242MB ONNX | zh/ja/yue/en/ko | ModelScope `iic/` |
| Paraformer-zh | ~220MB ONNX | zh only | ModelScope `iic/` |

Model files needed:
- **SenseVoiceSmall**: `model_quant.onnx`, `config.yaml`, `am.mvn`, `chn_jpn_yue_eng_ko_spectok.bpe.model`
- **FSMN-VAD**: `model_quant.onnx`, `config.yaml`, `am.mvn`
- **Paraformer**: `model_quant.onnx`, `config.yaml`, `am.mvn`, `tokens.json`

Audio is automatically resampled to 16kHz mono before ASR.

### AI Summary Configuration

Three providers supported (API key stored in `downloads/.llm_config.json`):

| Provider | Endpoint | Model | Notes |
|---|---|---|---|
| DeepSeek API (default) | `api.deepseek.com` | `deepseek-chat` | Cloud, requires API key |
| API relay (OpenAI format) | Custom | Custom | OpenAI-compatible endpoint |
| Local Ollama | `localhost:11434` | Custom | Fully offline, requires Ollama installed |

Summary output includes structured meeting notes + optional mind map (via markmap).

### Volume Gain

Low-volume recordings can be amplified before transcription. Available in Web UI and API:

| Option | Description |
|---|---|
| `auto` (default) | Peak normalization to 0.9 (max without clipping) |
| `2x` / `3x` / `5x` | Fixed multiplier gain (may clip) |
| `off` | No processing |

### Ogg Opus Packaging

The QS668 pushes fixed 40-byte raw Opus packets during real-time recording. These are NOT playable directly — they need an Ogg container wrapper. The `Qs668OggOpusWriter` class in `protocol.py` handles this:

- Writes OpusHead (BOS) + OpusTags pages on file open
- Every 50 packets (~1 second): flush one Ogg data page with CRC
- On close: flush remaining buffer + write EOS flag
- Result: valid Ogg/Opus file playable in VLC, ffprobe, pydub

**Fix existing raw files:**
```bash
# CLI
record> opus-fix recording.opus
record> opus-fix recording.opus --replace   # Overwrite original (backup created)

# API
POST /api/convert_raw_opus
{"name": "recording.opus", "replace": false}
```

### PyInstaller Build

```bash
cd desktop
build.bat          # Windows: produces dist/AIRecorder.exe
```

- ffmpeg binaries are bundled with the application
- Downloads saved to `./downloads` next to the executable
- Separate builds required for Windows and macOS

---

## WeChat Mini Program

### Setup

1. Download [WeChat DevTools](https://developers.weixin.qq.com/miniprogram/dev/devtools/download.html)
2. Import project from `miniprogram/` directory
3. Enter your AppID (or use test ID)
4. Click "Preview" and scan QR code with phone WeChat
5. Grant Bluetooth permissions
6. Scan and connect to QS668 recording pen

### API Configuration

On the transcribe page, tap "⚙️ API Config":

**ASR (Alibaba Cloud DashScope):**
- Register at [dashscope.aliyuncs.com](https://dashscope.aliyuncs.com)
- Create API Key
- Default model: `paraformer-1`
- Billing: ¥0.288/hour (¥0.00008/sec), 10 free hours/month

**LLM (DeepSeek):**
- Register at [platform.deepseek.com](https://platform.deepseek.com)
- Create API Key
- Default model: `deepseek-v4-flash`
- Billing: ~¥3/M input tokens (off-peak 50% off)

### Cost Estimation

| Service | Billing | Unit Price | Monthly Est. (1hr/day) |
|---|---|---|---|
| Alibaba DashScope Paraformer | Per duration | ¥0.288/hour | ~¥9 |
| DeepSeek V4-Flash | Per token | ~¥3/M input tokens | ~¥1 |
| WeChat Mini Program | Free | ¥0 | ¥0 |
| **Total** | | | **~¥10/month** |

> Light users may incur zero cost due to the 10 free hours/month ASR quota.

---

## Protocol Implementation Details

- **Frame construction**: `[0x5A][SEQ][CRC:2B LE][LEN:2B LE][DATA=TYPE+CMD+PARAMS]`; CRC-16/XMODEM, scope = LEN original 2 bytes + DATA
- **GATT**: AE21 write (no response), AE22 main notify (subscribe first), AE23 key event notify; each has independent stream parse buffer with half-frame/multi-frame/noise resync; LEN > 8192 treated as false header
- **Download (2-2)**: `offset:4B LE + filename:24B NUL-padded` = 36B must be one GATT write; candidate name: `base.wav → base.opus → truncated name`; switch only on code=1 with no data received
- **File list**: `count:4B BE + N×28B(time:4B BE + size:4B BE + name:20B)`; some firmware uses Unix timestamp in time field, detected via heuristic
- **File safety**: Same-name files get `_duration_size` suffix, never overwrite
- **Ogg Opus**: 40B raw Opus packets → valid Ogg/Opus container via streaming writer (OpusHead + OpusTags + data pages with CRC + EOS)

Protocol implementation verified against the manufacturer's official test page ([nextproto.top/qs668/](https://nextproto.top/qs668/)).

## Testing

```bash
cd desktop
python -m unittest discover tests -v
```

40 tests, no hardware or Bluetooth needed:

- `test_protocol.py` — Frame construction/parsing, CRC verification vectors, 36B real-device frame byte-by-byte reproduction, big-endian file list decode, WAV validation, timestamp heuristic
- `test_device.py` — FakeTransport end-to-end: request-response matching, multi-frame list assembly, download session with candidate name fallback, resume, segmented download, delete compatibility
- `test_asr.py` — ASR module dependency-missing fallback
- `test_web.py` — Web REST API (status/params/path traversal protection/static files) and simulated device call chain

## FAQ

**Can't scan device**: Ensure pen isn't connected to phone app; try `scan compat` to list all devices
**`transcribe` reports funasr missing**: Run `pip install -r requirements-asr.txt`
**torchaudio WinError 127**: torch/torchaudio version mismatch; install matching torchaudio (e.g., `pip install torchaudio==2.9.1`)
**Bluetooth pairing timeout**: Phone may occupy the pairing slot; unpair from phone, restart pen, re-pair on PC
**Real-time recording file won't play**: Use `opus-fix` command to wrap raw Opus packets into valid Ogg/Opus
**Low audio volume**: Use volume gain feature (auto/2x/3x/5x) in transcribe settings

## References

- **Protocol source project**: [lomehong/record](https://github.com/lomehong/record/tree/master/) — QS668 BLE Communication Protocol V1.0 implementation
- **Manufacturer resources**: [nextproto.top/materials](https://nextproto.top/materials) — Protocol docs and tools (credential required)
- **Manufacturer test page**: [nextproto.top/qs668/](https://nextproto.top/qs668/) — Official online test tool
- **ASR models**: [ModelScope FunASR](https://github.com/modelscope/FunASR) — SenseVoiceSmall / Paraformer
- **BLE library**: [bleak](https://github.com/hbldh/bleak) — Bluetooth Low Energy platform-agnostic
- **WeChat Mini Program**: [Official docs](https://developers.weixin.qq.com/miniprogram/dev/framework/)

---

### License

MIT License - See [LICENSE](LICENSE)
