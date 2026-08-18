"""本地冒烟：验证 Ogg Opus 包装器。"""
import io
import json as _json
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recorder import protocol as P


def main():
    ok_all = True
    reports = []

    def tick(name, cond, detail=""):
        nonlocal ok_all
        if not cond:
            ok_all = False
        mark = "PASS" if cond else "FAIL"
        line = f"[{mark}] {name}"
        if detail:
            line += f" — {detail}"
        reports.append(line)
        print(line)

    # ====== 1. ogg_crc32 测试向量 ======
    tick("ogg_crc32 empty == 0", P.ogg_crc32(b"") == 0x00000000)
    tick("ogg_crc32('123456789') == 0xCBF43926 (ISO 3309)",
         P.ogg_crc32(b"123456789") == 0xCBF43926,
         f"got={hex(P.ogg_crc32(b'123456789'))}")

    # ====== 2. ogg_make_page CRC 自洽 ======
    head = (b"OpusHead" + bytes([1, 1])
            + struct.pack("<H", 312)
            + struct.pack("<I", 16000)
            + struct.pack("<h", 0) + bytes([0]))
    page = P.ogg_make_page([head], 0, 0x51533638, 0, 0x02)
    stored_crc = struct.unpack_from("<I", page, 22)[0]
    zeroed = page[:22] + b"\x00\x00\x00\x00" + page[26:]
    recalc_crc = P.ogg_crc32(zeroed)
    tick("ogg_make_page CRC 回写正确", stored_crc == recalc_crc,
         f"stored={hex(stored_crc)} recalc={hex(recalc_crc)}")
    tick("OggS 首 4B 标记", page[0:4] == b"OggS")

    # ====== 3. Qs668OggOpusWriter 结构正确性 ======
    raw_packets = [bytes([i & 0xFF] * 40) for i in range(123)]
    with tempfile.NamedTemporaryFile("wb", suffix=".opus", delete=False) as t:
        tmp_name = t.name
    with open(tmp_name, "wb") as f:
        w = P.Qs668OggOpusWriter(f, sample_rate=16000)
        for p in raw_packets:
            w.write_packet(p)
        w.close()

    data = Path(tmp_name).read_bytes()
    # 扫所有 Ogg page
    fp = io.BytesIO(data)
    page_count = 0
    bos_seen = eos_seen = False
    head_ok = tags_ok = False
    serials = set()
    seqs = []
    last_granule = 0
    crc_ok_all_pages = True
    while True:
        hdr = fp.read(27)
        if len(hdr) < 27:
            break
        if hdr[0:4] != b"OggS":
            crc_ok_all_pages = False
            break
        flags = hdr[5]
        granule = struct.unpack_from("<Q", hdr, 6)[0]
        serial = struct.unpack_from("<I", hdr, 14)[0]
        seq = struct.unpack_from("<I", hdr, 18)[0]
        crc = struct.unpack_from("<I", hdr, 22)[0]
        seg_cnt = hdr[26]
        segs = fp.read(seg_cnt)
        # 计算 body_len（255 = 延续，其他 = packet 末尾）
        body_len = 0
        partial = 0
        for b in segs:
            partial += b
            if b != 255:
                body_len += partial
                partial = 0
        body_len += partial
        body = fp.read(body_len)
        # CRC 校验
        full = bytearray()
        full += hdr
        full += segs
        full += body
        full[22:26] = b"\x00\x00\x00\x00"
        crc2 = P.ogg_crc32(bytes(full))
        if crc2 != crc:
            crc_ok_all_pages = False
        page_count += 1
        serials.add(serial)
        seqs.append(seq)
        last_granule = granule
        if flags & 0x02:
            bos_seen = True
        if flags & 0x04:
            eos_seen = True
        if page_count == 1:
            head_ok = body.startswith(b"OpusHead")
        if page_count == 2:
            tags_ok = body.startswith(b"OpusTags")

    tick("所有 Ogg page CRC 通过", crc_ok_all_pages)
    tick("BOS(0x02) 首标记", bos_seen)
    tick("EOS(0x04) 尾标记", eos_seen)
    tick("第 1 页 OpusHead 正确", head_ok)
    tick("第 2 页 OpusTags 正确", tags_ok)
    tick("Ogg Serial == 0x51533638 (QS68)",
         serials == {0x51533638},
         f"实际={[hex(s) for s in serials]}")
    tick("page_sequence 从 0 起连续",
         seqs == list(range(len(seqs))),
         f"len(seqs)={len(seqs)}  seqs[:3]={seqs[:3]}  seqs[-3:]={seqs[-3:]}")
    exp_granule = 123 * 960  # 每 packet = 960 samples@48kHz (20ms)
    tick(f"最终 granule={last_granule} == 123*960={exp_granule}",
         last_granule == exp_granule)

    # ====== 4. 与根目录「解码 OPUS 文件脚本.py」二进制一致 ======
    script_path = Path(__file__).resolve().parents[2] / "解码 OPUS 文件脚本.py"
    if script_path.is_file():
        spec_spec = importlib_util_spec("opus_orig_script", str(script_path))
        mod = importlib_util_module(spec_spec); spec_spec.loader.exec_module(mod)
        raw_bytes = b"".join(raw_packets)
        out_a = mod.wrap_qs668_raw_opus(raw_bytes, sample_rate=16000)
        raw_path = Path(tmp_name).with_suffix(".raw")
        raw_path.write_bytes(raw_bytes)
        out_b_path = raw_path.with_suffix(".wrap_compare.opus")
        P.wrap_raw_opus_file(raw_path, out_b_path, sample_rate=16000)
        out_b = out_b_path.read_bytes()
        tick("wrap 输出 与 原厂脚本 二进制逐字节一致",
             out_a == out_b,
             f"len_a={len(out_a)}  len_b={len(out_b)}")
        try:
            raw_path.unlink()
            out_b_path.unlink()
        except OSError:
            pass
    else:
        reports.append(f"[SKIP] 原厂脚本不存在：{script_path}")

    # ====== 5. ffprobe 解析（容器+编解码器合法）======
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        cand = Path(__file__).resolve().parents[2] / "ffmpeg-8.1.2-essentials_build" / "bin" / "ffprobe.exe"
        if cand.is_file():
            ffprobe = str(cand)
    if ffprobe:
        r = subprocess.run(
            [ffprobe, "-hide_banner", "-loglevel", "error",
             "-print_format", "json", "-show_streams", tmp_name],
            capture_output=True, text=True)
        try:
            info = _json.loads(r.stdout or "{}")
            streams = info.get("streams", [])
            tick("ffprobe 解析成功且正好 1 条流", len(streams) == 1,
                 f"n={len(streams)}")
            if streams:
                st = streams[0]
                tick("ffprobe: codec_name == opus",
                     st.get("codec_name") == "opus",
                     f"actual={st.get('codec_name')}")
                sr = int(st.get("sample_rate") or 0)
                tick(f"ffprobe: sample_rate == 48000 (OggOpus 时间线)", sr == 48000,
                     f"actual={sr}")
                ch = int(st.get("channels") or 0)
                tick(f"ffprobe: channels == 1", ch == 1, f"actual={ch}")
                dur = float(st.get("duration") or 0.0)
                # 123 packets * 20ms = 2.46s
                tick(f"ffprobe: duration ≈ 2.46s", abs(dur - 2.46) < 0.05,
                     f"actual={dur:.3f}s")
        except Exception as exc:
            tick("ffprobe 解析 JSON 失败", False, f"{type(exc).__name__}: {exc}")
    else:
        reports.append("[SKIP] 没有找到 ffprobe（未装或不在 PATH/项目根 ffmpeg 目录）")

    # ====== 6. soundfile 读取（若 libsndfile 支持 opus）======
    try:
        import soundfile as sf
        d, sr = sf.read(tmp_name)
        tick("soundfile 读取成功 sr=48000", sr == 48000, f"sr={sr}")
        # 2.46s * 48000 = 约 118080 samples
        expected_samples = round(2.46 * sr)
        tick(f"soundfile samples ≈ {expected_samples}",
             abs(len(d) - expected_samples) < 200,
             f"actual samples={len(d)}")
    except Exception as exc:
        reports.append(f"[SKIP] soundfile 不支持 opus（正常，走 ffmpeg 兜底已验证即可）: {type(exc).__name__}: {exc}")

    # cleanup
    try:
        Path(tmp_name).unlink()
    except OSError:
        pass

    print("\n===== 报告 =====")
    for r in reports:
        print(r)
    return 0 if ok_all else 1


def importlib_util_spec(name, loc):
    import importlib.util as _u
    return _u.spec_from_file_location(name, loc)


def importlib_util_module(spec):
    import importlib.util as _u
    return _u.module_from_spec(spec)


if __name__ == "__main__":
    sys.exit(main())
