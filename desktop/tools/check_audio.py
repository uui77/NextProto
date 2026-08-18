"""音频诊断脚本（修正版，解决 Windows GBK 解码问题）

运行：python.exe tools\check_audio.py
"""
import sys
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    candidates = []
    d = ROOT / "downloads"
    if d.exists():
        for p in sorted(d.iterdir(), key=lambda x: -x.stat().st_mtime):
            if p.suffix.lower() in (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"):
                candidates.append(p)
    for p in sorted(ROOT.iterdir(), key=lambda x: -x.stat().st_mtime):
        if p.suffix.lower() in (".wav", ".mp3", ".m4a"):
            candidates.append(p)
    if not candidates:
        print("没找到音频，请在 downloads 里放一个")
        return 1
    audio = candidates[0]
    print(f"诊断文件：{audio}  ({audio.stat().st_size} bytes)\n")

    # 1) 用 pydub 读（它内部处理编码问题更稳）
    print("=" * 60)
    print("[1] pydub 解码后：采样率/声道/振幅")
    print("=" * 60)
    try:
        from pydub import AudioSegment
        seg = AudioSegment.from_file(str(audio))
        print(f"  duration_ms   = {len(seg)} ms")
        print(f"  frame_rate    = {seg.frame_rate} Hz")
        print(f"  channels      = {seg.channels}")
        print(f"  sample_width  = {seg.sample_width * 8} bit")
        print(f"  dBFS (avg)    = {seg.dBFS:.2f}")
        samples = seg.get_array_of_samples()
        import statistics
        peak = max(abs(s) for s in samples)
        rms = statistics.mean(abs(s) for s in samples)
        max_val = 2 ** (seg.sample_width * 8 - 1)
        print(f"  peak /max     = {peak} / {max_val}  ({peak/max_val*100:.2f}%)")
        print(f"  rms           = {rms:.2f}")
        print(f"  ratio (rms/max) = {rms/max_val*100:.2f}%")

        if seg.frame_rate != 16000 or seg.channels != 1:
            print("\n  ⚠ 采样率不是 16kHz 或是多声道，重采样到 16kHz 单声道并保存为 _16k.wav ...")
            seg16 = seg.set_frame_rate(16000).set_channels(1)
            out16 = audio.with_name(audio.stem + "_16k.wav")
            seg16.export(str(out16), format="wav")
            print(f"  已保存：{out16}  ({out16.stat().st_size} bytes)")
            print("\n  👉 请用这个 _16k.wav 跑转写，如果能出字说明是采样率问题")
    except Exception as e:
        print("pydub 解码失败：", type(e).__name__, e)
        import traceback
        traceback.print_exc()

    # 2) 直接用 soundfile 读（仅 wav）
    if audio.suffix.lower() == ".wav":
        print("\n" + "=" * 60)
        print("[2] soundfile 读取（检查 wav 真实采样率）")
        print("=" * 60)
        try:
            import soundfile as sf
            data, sr = sf.read(str(audio))
            print(f"  samplerate={sr}, shape={data.shape}, dtype={data.dtype}")
            if data.ndim == 1:
                print(f"  min={data.min():.4f}, max={data.max():.4f}, "
                      f"rms={(data.astype('float64')**2).mean()**0.5:.4f}")
            else:
                print(f"  多声道：shape={data.shape}")
                for ch in range(data.shape[1]):
                    d = data[:, ch]
                    print(f"    声道{ch}: min={d.min():.4f}, max={d.max():.4f}, "
                          f"rms={(d.astype('float64')**2).mean()**0.5:.4f}")
        except Exception as e:
            print("soundfile 读失败：", type(e).__name__, e)

    # 3) 读 WAV 文件头（最简单粗暴，直接看 header）
    if audio.suffix.lower() == ".wav":
        print("\n" + "=" * 60)
        print("[3] WAV 文件头解析（前 44 字节）")
        print("=" * 60)
        with open(str(audio), "rb") as f:
            header = f.read(44)
        if len(header) >= 44 and header[:4] == b"RIFF" and header[8:12] == b"WAVE":
            import struct
            channels = struct.unpack_from("<H", header, 22)[0]
            sample_rate = struct.unpack_from("<I", header, 24)[0]
            bits = struct.unpack_from("<H", header, 34)[0]
            print(f"  channels     = {channels}")
            print(f"  sample_rate  = {sample_rate} Hz")
            print(f"  bits_per_spl = {bits}")
        else:
            print(f"  不是标准 WAV（magic={header[:4]}）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
