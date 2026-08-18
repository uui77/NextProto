"""诊断 VAD 返回的 segments 结构，并直接把整段音频喂给 SenseVoice 验证模型本身。

运行：python.exe tools\debug_vad.py
"""
import sys
import asyncio
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")

from recorder import asr

def find_audio_16k():
    d = ROOT / "downloads"
    if d.exists():
        p16k = [p for p in d.iterdir() if p.name.endswith("_16k.wav")]
        if p16k:
            return p16k[0]
        for p in sorted(d.iterdir(), key=lambda x: -x.stat().st_mtime):
            if p.suffix.lower() in (".wav", ".mp3"):
                return p
    return None


def main():
    audio = find_audio_16k()
    if audio is None:
        print("没音频")
        return 1
    print(f"[1] 用音频：{audio}")

    # 手动加载模型
    print("\n[2] 加载 SenseVoice + VAD ...")
    cfg = asr._get_model("sensevoice", "cpu", None)
    asr_model = cfg["model"]
    vad_model = cfg["vad"]

    # 看 VAD 返回片段的真实类型
    print("\n[3] 调 VAD，看 segments 结构 ...")
    vad_result = vad_model([str(audio)])
    segments = vad_result[0] if vad_result else None
    print(f"  vad_result 类型：{type(vad_result)}")
    print(f"  vad_result 长度：{len(vad_result)}")
    print(f"  segments 类型：{type(segments)}")
    if segments:
        print(f"  segments 长度：{len(segments)}")
        for i, seg in enumerate(segments[:3]):
            print(f"\n  --- 片段 {i} ---")
            print(f"    type: {type(seg)}")
            try:
                if hasattr(seg, 'shape'):
                    print(f"    shape: {seg.shape}, dtype: {seg.dtype}")
                    print(f"    min/max: {seg.min():.4f}/{seg.max():.4f}")
                elif isinstance(seg, (list, tuple)):
                    print(f"    len: {len(seg)}")
                    print(f"    前 5 个元素：{list(seg)[:5]}")
                else:
                    print(f"    值: {repr(seg)[:300]}")
            except Exception as e:
                print(f"    打印失败：{e}")
    else:
        print("  segments 是空的！")

    # 4) 把整段音频直接喂给 SenseVoice，看模型本身能不能出字
    print("\n[4] 直接把整段音频路径传给 SenseVoice（跳过 VAD）...")
    try:
        text = asr_model([str(audio)])
        print(f"  asr_model 返回类型：{type(text)}")
        print(f"  asr_model 返回：{text!r}")
        if isinstance(text, (list, tuple)) and text:
            print(f"  第 0 个元素类型：{type(text[0])}")
            print(f"  第 0 个元素值：{text[0]!r}")
    except Exception as e:
        print(f"  整段识别失败：{type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()

    return 0


if __name__ == "__main__":
    sys.exit(main())
