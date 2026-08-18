"""独立测试脚本：直接测 asr.transcribe_file，绕开 FastAPI，直接看真实 traceback。

运行（必须用 32 位 Python.exe）：
    cd /d e:\编程项目开发\录音卡\record
    python.exe tools\test_asr.py
"""
import os
import sys
import traceback
from pathlib import Path

# 让脚本单独也能 import recorder
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 强制让 asr 的 logger 直接 print 出完整堆栈（不要只看前端提示）
import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stderr,
)

from recorder import asr

def find_test_audio():
    # 与 main.py 默认一致：e:\编程项目开发\录音卡\record\downloads
    out = ROOT / "downloads"
    if out.exists():
        for p in sorted(out.iterdir(), key=lambda x: -x.stat().st_mtime):
            if p.suffix.lower() in (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"):
                return p
    # fallback：看一下 ROOT 目录本身
    for p in sorted(ROOT.iterdir(), key=lambda x: -x.stat().st_mtime):
        if p.suffix.lower() in (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"):
            return p
    return None

def main():
    audio = find_test_audio()
    if audio is None:
        print("没找到测试音频，请先在 output_dir（本地下载过的目录）里放至少一个 .wav / .mp3")
        print("或者在 output_dir 里手动放一个 16k 16bit mono 的 wav 叫 test.wav")
        return 2
    print(f"[STEP 1] 用音频文件做测试：{audio}  ({audio.stat().st_size} bytes)")

    print("\n[STEP 2] 先测试 asr._download_model 看看模型缓存路径和文件检查")
    try:
        mid = asr.MODELS["sensevoice"]["id"]
        local = asr._download_model(mid)
        print(f"  → 模型目录: {local}")
        files = list(Path(local).iterdir()) if Path(local).exists() else []
        print(f"  → 目录内文件 ({len(files)}):")
        for f in files:
            print(f"       - {f.name}  {f.stat().st_size} bytes" if f.is_file() else f"       [DIR] {f.name}")
    except Exception:
        print("  ✗ _download_model 出错：")
        traceback.print_exc()

    print("\n[STEP 3] 测试 VAD 模型下载")
    try:
        vad_dir = asr._download_model(asr.VAD_MODEL_ID)
        print(f"  → VAD 目录: {vad_dir}")
        files = list(Path(vad_dir).iterdir()) if Path(vad_dir).exists() else []
        print(f"  → 目录内文件 ({len(files)}):")
        for f in files:
            print(f"       - {f.name}  {f.stat().st_size} bytes" if f.is_file() else f"       [DIR] {f.name}")
    except Exception:
        print("  ✗ VAD _download_model 出错：")
        traceback.print_exc()

    print("\n[STEP 4] 调用 asr.transcribe_file（这就是 web /api/transcribe 里面的调用）")
    try:
        import asyncio
        result = asyncio.run(asr.transcribe_file(
            audio,
            language="auto",
            model="sensevoice",
            spk=False,
        ))
        print("  ✓ 转写成功！返回：")
        if isinstance(result, dict):
            for k, v in result.items():
                print(f"      {k}: {v!r}")
        else:
            print("     ", result)
        return 0
    except asr.AsrNotAvailable as e:
        print(f"  ✗ AsrNotAvailable（友好错误）：{e}")
        return 3
    except Exception:
        print("  ✗ 转写抛异常，完整堆栈如下：\n")
        traceback.print_exc()
        return 4

if __name__ == "__main__":
    sys.exit(main())
