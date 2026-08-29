"""本地离线语音转文字（FunASR ONNX 版）。

依赖：pip install -r requirements-asr.txt
首次运行会自动下载 ONNX 量化模型（SenseVoiceSmall 约 242MB，Paraformer 约 889MB），
之后完全离线可用。模型下载使用标准库 urllib，不依赖 modelscope。
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import os
import ssl
import sys
import tarfile
import traceback
import urllib.request
import urllib.error
import zipfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _inject_ffmpeg_path_for_asr() -> None:
    """CLI/直接导入 asr 时，也把打包内的 ffmpeg 注入 PATH（与 web.py 同逻辑）。"""
    try:
        candidates = []
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            candidates.append(Path(sys._MEIPASS) / "ffmpeg")
        exe_dir = Path(sys.executable).resolve().parent
        if exe_dir.name == "录音笔控制台":
            candidates.append(exe_dir / "_internal" / "ffmpeg")
            candidates.append(exe_dir / "ffmpeg")
        for p in candidates:
            ext = ".exe" if sys.platform.startswith("win") else ""
            if (p / f"ffmpeg{ext}").is_file() and (p / f"ffprobe{ext}").is_file():
                ps = str(p)
                if ps not in os.environ.get("PATH", "").split(os.pathsep):
                    os.environ["PATH"] = ps + os.pathsep + os.environ.get("PATH", "")
                return
    except Exception:
        pass  # 注入失败不影响主流程


_inject_ffmpeg_path_for_asr()


MODELS = {
    "sensevoice": {
        "id": "danieldong/sensevoice-small-onnx-quant",
        "label": "SenseVoiceSmall（通用·快速·多语种，242MB ONNX）",
        "languages": ("auto", "zh", "en", "yue", "ja", "ko"),
        "onnx_files": ("model_quant.onnx", "config.yaml", "am.mvn",
                       "chn_jpn_yue_eng_ko_spectok.bpe.model"),
    },
    "paraformer": {
        "id": "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-onnx",
        "label": "Paraformer-zh（中文高精度，约 889MB ONNX）",
        "languages": ("auto", "zh", "en"),
        "onnx_files": ("model_quant.onnx", "config.yaml", "am.mvn",
                       "tokens.json"),
    },
}
LANGUAGES = MODELS["sensevoice"]["languages"]

VAD_MODEL_ID = "iic/speech_fsmn_vad_zh-cn-16k-common-onnx"
VAD_ONNX_FILES = ("model_quant.onnx", "config.yaml", "am.mvn")

SPK_MODEL_ID = "iic/speech_campplus_sv_zh-cn_16k-common"  # 说话人分离（需 funasr-onnx >= 1.x）

DEVICE_DEFAULT = "cpu"
SPK_MODEL_DEFAULT = None

_models: dict[str, object] = {}
_current_config: Optional[tuple] = None
_model_cache_dir: Optional[str] = None


class AsrNotAvailable(RuntimeError):
    """funasr-onnx 未安装或模型加载失败。"""


def is_loaded() -> bool:
    return bool(_models)


def _get_model_cache_dir() -> str:
    global _model_cache_dir
    if _model_cache_dir is None:
        _model_cache_dir = os.path.join(os.path.expanduser("~"),
                                        ".cache", "modelscope", "hub")
    return _model_cache_dir


def _model_path(model_id: str) -> str:
    return os.path.join(_get_model_cache_dir(), model_id.replace("/", os.sep))


def _onnx_spec(model_id: str) -> tuple[str, list[str]]:
    """根据 model_id 返回 (ModelScope 仓库 ID, 必下载文件列表)。
    返回的 ModelScope 仓库 ID 是已经打包了 ONNX 的版本，直接逐文件下载。
    """
    if model_id == MODELS["sensevoice"]["id"]:
        return (
            "danieldong/sensevoice-small-onnx-quant",
            ["model_quant.onnx", "config.yaml", "am.mvn",
             "chn_jpn_yue_eng_ko_spectok.bpe.model"],
        )
    if model_id == MODELS["paraformer"]["id"]:
        return (
            "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-onnx",
            ["model_quant.onnx", "config.yaml", "am.mvn", "tokens.json"],
        )
    if model_id == VAD_MODEL_ID:
        return (
            "iic/speech_fsmn_vad_zh-cn-16k-common-onnx",
            ["model_quant.onnx", "config.yaml", "am.mvn"],
        )
    if model_id == SPK_MODEL_ID:
        return (
            "iic/speech_campplus_sv_zh-cn_16k-common",
            ["campplus.onnx", "config.yaml"],
        )
    # 未知模型：把 model_id 当作 MS 仓库 ID，并兜底要求 .onnx 存在
    return (model_id, ["model_quant.onnx", "config.yaml", "am.mvn"])


def _modelscope_file_url(repo_id: str, filename: str, revision: str = "master") -> str:
    """返回 ModelScope 单文件直链（无需登录，带重定向到 CDN）。

    尝试两种 API 格式，第一种失败就由调用方回退：
      1) /resolve/{revision}/{filename}   —— 标准 resolve 端点
      2) /repo?RepoRevision=&File=         —— 旧端点（部分仓库仍可用）
    """
    import urllib.parse
    encoded_name = urllib.parse.quote(filename, safe="")
    return (f"https://www.modelscope.cn/api/v1/models/{repo_id}/"
            f"resolve/{revision}/{encoded_name}")


def _modelscope_file_url_fallback(repo_id: str, filename: str, revision: str = "master") -> str:
    """ModelScope 旧端点格式兜底。"""
    import urllib.parse
    encoded_name = urllib.parse.quote(filename, safe="")
    return (f"https://www.modelscope.cn/api/v1/models/{repo_id}/"
            f"repo?RepoRevision={revision}&File={encoded_name}")


def _hf_mirror_file_url(repo_id: str, filename: str) -> str:
    """返回 HuggingFace 镜像单文件直链（hf-mirror.com）。"""
    import urllib.parse
    encoded_name = urllib.parse.quote(filename, safe="")
    return f"https://hf-mirror.com/{repo_id}/resolve/main/{encoded_name}"


def _is_model_ready(local_dir: str, required_files: list[str]) -> bool:
    """判断模型目录是否已完整下载（所有 required_files 都存在）。"""
    if not os.path.isdir(local_dir):
        return False
    for f in required_files:
        p = os.path.join(local_dir, f)
        if not os.path.isfile(p) or os.path.getsize(p) == 0:
            return False
    return True


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.2f} GB"


def _http_download(url: str, target_path: str, desc: str = "") -> None:
    """用 Python 标准库下载文件，带进度显示和断点续传。"""
    import urllib.request
    logger.info("%s", desc)
    existing = os.path.getsize(target_path) if os.path.isfile(target_path) else 0
    req = urllib.request.Request(url)
    if existing > 0:
        req.add_header("Range", f"bytes={existing}-")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            mode = "ab" if (existing > 0 and resp.status == 206) else "wb"
            if mode == "wb" and existing > 0:
                existing = 0
            total = None
            cl = resp.headers.get("Content-Length")
            if cl is not None:
                total = int(cl) + (existing if mode == "ab" else 0)
            downloaded = existing
            last_pct = -1
            with open(target_path, mode) as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total and total > 0:
                        pct = int(downloaded * 100 / total)
                        if pct != last_pct and pct % 10 == 0:
                            logger.info("  %d%% (%s / %s)",
                                        pct, _fmt_size(downloaded), _fmt_size(total))
                            last_pct = pct
    except Exception:
        if os.path.isfile(target_path):
            os.remove(target_path)
        raise
    logger.info("  ✓ 下载完成：%s", _fmt_size(downloaded))


def _download_model(model_id: str) -> str:
    """下载模型到本地缓存，返回本地路径。"""
    repo_id, files = _onnx_spec(model_id)
    local_dir = _model_path(model_id)
    if _is_model_ready(local_dir, files):
        logger.info("模型已缓存：%s（%d 个文件）", local_dir, len(files))
        return local_dir

    os.makedirs(local_dir, exist_ok=True)

    # 优先用 modelscope（如可用）
    try:
        from modelscope.hub.snapshot_download import snapshot_download
        cache_dir = _get_model_cache_dir()
        logger.info("使用 modelscope 下载：%s", repo_id)
        local_dir = snapshot_download(repo_id, cache_dir=cache_dir)
        if _is_model_ready(local_dir, files):
            return local_dir
    except Exception as e:
        logger.info("modelscope 下载失败（%s），尝试直链逐文件下载…", e)

    # 兜底：逐文件下载（先试 ModelScope，失败回退 HF 镜像）
    # HF 镜像里 SenseVoice 是 FunAudioLLM/SenseVoiceSmall，cam++ 别名不同，
    # 所以这里只给 SenseVoice + VAD 的 HF 兜底（其他先试 MS 失败就提示手动）
    known_hf_aliases: dict[str, Optional[str]] = {
        "danieldong/sensevoice-small-onnx-quant": "FunAudioLLM/SenseVoiceSmall",
        "iic/speech_fsmn_vad_zh-cn-16k-common-onnx": None,
        "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-onnx": None,
        "iic/speech_campplus_sv_zh-cn_16k-common": "WenhaoZhao/campplus-onnx",
    }

    missing = [f for f in files if not os.path.isfile(os.path.join(local_dir, f))
               or os.path.getsize(os.path.join(local_dir, f)) == 0]
    if not missing:
        return local_dir

    last_err: Optional[Exception] = None
    total = len(missing)
    for idx, filename in enumerate(missing, start=1):
        target_path = os.path.join(local_dir, filename)
        urls = [
            _modelscope_file_url(repo_id, filename),
            _modelscope_file_url_fallback(repo_id, filename),
        ]
        hf = known_hf_aliases.get(repo_id)
        if hf:
            urls.append(_hf_mirror_file_url(hf, filename))
        ok = False
        for u_idx, url in enumerate(urls):
            try:
                desc = f"[{idx}/{total}] 下载模型 {repo_id} → {filename}（镜像 {u_idx+1}/{len(urls)}）"
                _http_download(url, target_path, desc)
                if os.path.isfile(target_path) and os.path.getsize(target_path) > 0:
                    ok = True
                    break
            except Exception as e:
                last_err = e
                logger.warning("文件 %s 镜像 %d 下载失败：%s", filename, u_idx+1, e)
                continue
        if not ok:
            # 列出手动下载指引
            ms_urls = [
                f"  - {f}: "
                f"https://www.modelscope.cn/models/{repo_id}/files "
                f"（页面上找到 {f} 点「下载」）"
                for f in missing
            ]
            manual_hint = (
                f"自动下载失败（最后错误：{last_err}）。\n"
                f"请手动从 ModelScope 下载 {repo_id} 以下文件，"
                f"全部放到目录：{local_dir}\n"
                + "\n".join(ms_urls)
            )
            raise AsrNotAvailable(manual_hint)

    # 校验一遍所有文件
    if _is_model_ready(local_dir, files):
        logger.info("模型 %s 下载完成：%s", repo_id, local_dir)
        return local_dir

    raise AsrNotAvailable(
        f"模型 {repo_id} 下载后缺少必要文件；请检查 {local_dir} 下是否包含：{files}")



def _make_model(model_key: str, device: str, spk_model: Optional[str]):
    """按配置构造 funasr_onnx 模型实例。"""
    # ① 基础模型 + VAD：必选，先导入（失败就直接报错）
    try:
        from funasr_onnx import SenseVoiceSmall, Paraformer, Fsmn_vad
    except Exception as exc:
        tb = traceback.format_exc()
        logger.error("ASR 基础模型导入失败：%s\n%s", exc, tb)
        raise AsrNotAvailable(
            "ASR 依赖加载失败（导入 SenseVoiceSmall/Paraformer/Fsmn_vad）："
            f"{type(exc).__name__}: {exc}\n"
            "请在 CMD 执行以下命令补全依赖后重试：\n"
            "  cd /d e:\\编程项目开发\\录音卡\\record\n"
            "  python.exe -m pip install -U funasr-onnx onnxruntime librosa "
            "sentencepiece kaldi-native-fbank soundfile PyYAML scipy jieba "
            "-i https://mirror.sjtu.edu.cn/pypi/web/simple "
            "--no-cache-dir --default-timeout=300"
        ) from exc

    # ② 说话人分离模型：可选，只有当用户启用时才尝试导入；
    #    如果当前 funasr-onnx 版本不含 Campplus，降级为仅警告，
    #    不阻断主流程（Paraformer 自带基础的 [preds:...] 说话人标注）。
    Campplus_cls = None
    if spk_model:
        try:
            from funasr_onnx import Campplus as Campplus_cls  # type: ignore
        except Exception as exc:
            logger.warning(
                "当前 funasr-onnx 版本（0.4.x）不含 Campplus 说话人分离类，"
                "完整说话人分离不可用；但 Paraformer 自带基础说话人分段标注。\n"
                "如需完整说话人分离，可升级到 funasr-onnx 1.x（需 Python 3.9+）：\n"
                "  python.exe -m pip install -U funasr-onnx\n"
                "当前将继续转写，仅跳过 Campplus 说话人分离。"
            )
            Campplus_cls = None

    spec = MODELS[model_key]
    logger.info("加载模型 %s（device=%s, spk=%s；首次运行会自动下载）...",
                spec["label"], device, spk_model or "off")

    model_dir = _download_model(spec["id"])
    vad_dir = _download_model(VAD_MODEL_ID)

    kwargs: dict = dict(batch_size=1, quantize=True)
    if device == "cpu":
        kwargs["device_id"] = -1
    else:
        kwargs["device_id"] = 0

    if model_key == "sensevoice":
        model = SenseVoiceSmall(model_dir, **kwargs)
    elif model_key == "paraformer":
        model = Paraformer(model_dir, **kwargs)
    else:
        raise AsrNotAvailable(f"未知模型：{model_key}")

    vad = Fsmn_vad(vad_dir, **kwargs)

    spk = None
    if spk_model and Campplus_cls is not None:
        spk_dir = _download_model(SPK_MODEL_ID)
        spk = Campplus_cls(spk_dir, **kwargs)

    return {"model": model, "vad": vad, "spk": spk, "model_key": model_key}


def _get_model(model_key: str, device: str, spk_model: Optional[str]):
    global _current_config
    cfg = (model_key, device, spk_model)
    if model_key not in _models or _current_config != cfg:
        if _current_config and _current_config != cfg:
            logger.warning("ASR 配置变更，重新加载模型（首次加载较慢）：%s -> %s",
                           _current_config, cfg)
        _models[model_key] = _make_model(model_key, device, spk_model)
        _current_config = cfg
    return _models[model_key]


def _ensure_audio_deps(audio_path: str) -> None:
    """提前检查 pydub/ffmpeg 依赖：对非 wav 音频 funasr-onnx 内部会用 pydub 转 wav。

    如果缺依赖就抛 AsrNotAvailable，并给出明确的安装步骤，避免用户看到
    "ModuleNotFoundError: No module named 'pydub'" 这样的底层报错。
    """
    import os
    suffix = os.path.splitext(audio_path)[1].lower()
    is_wav = suffix == ".wav"
    # 即使是 wav，也尽量在进 funasr 之前把 pydub 装上，因为内部调用链可能仍有兜底转换。

    # 1) 检查 pydub Python 包
    try:
        import pydub  # noqa: F401
    except ModuleNotFoundError as exc:
        raise AsrNotAvailable(
            "缺少 Python 依赖 pydub（用于把 mp3/m4a 等音频转成 wav）。\n"
            "请在 CMD 里运行：\n"
            "    pip install -U pydub\n"
            "安装后重新尝试转写。"
        ) from exc

    # 2) 非 wav 时再额外检查 ffmpeg.exe 在 PATH 里（pydub 调它做真正解码）
    if not is_wav:
        import shutil
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            raise AsrNotAvailable(
                "系统里找不到 ffmpeg（pydub 解码 mp3 等非 wav 格式需要它）。\n"
                "请按以下任一方式安装：\n"
                "  a. 若已安装 7-Zip/Chocolatey：在管理员 CMD 里运行 choco install ffmpeg\n"
                "  b. 手动下载 Windows 构建版（release essentials 版即可，体积小）：\n"
                "     https://www.gyan.dev/ffmpeg/builds/\n"
                "     下载后解压，把 bin 目录（包含 ffmpeg.exe、ffprobe.exe）加入系统环境变量 PATH，\n"
                "     打开新的 CMD 验证：ffmpeg -version 能出版本号即可。\n"
                "  c. 或者在 CMD 里先临时把已下载好的 bin 目录加到 PATH，例如：\n"
                "     set PATH=C:\\ffmpeg\\bin;%PATH%   &   python.exe main.py --web\n"
            )


def _strip_sensevoice_tokens(text: str) -> str:
    """清除 SenseVoice 输出里的特殊 token（<|zh|><|NEUTRAL|> 等），返回可读文本。"""
    import re
    text = re.sub(r"<\|[^>]+\|>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# 句子结束标点（中英文）：句号、问号、感叹号、分号
_SENTENCE_ENDS = "。！？；!?;"


def _split_segment_by_punctuation(text: str, start_ms: int, end_ms: int) -> list:
    """对一个 VAD 片段的转写文本做基于标点的二次断句。

    SenseVoice 输出的文本自带标点（。！？等），但 VAD 片段可能很长（10-20秒），
    包含多个句子。这里按句子结束标点做二次切分，生成更细粒度的子段，
    时间戳按字符长度比例线性插值。

    返回 [{start, end, text}, ...]，至少返回 1 条（原始段）。
    """
    text = (text or "").strip()
    if not text:
        return [{"start": start_ms, "end": end_ms, "text": ""}]

    duration = end_ms - start_ms
    if duration <= 0:
        return [{"start": start_ms, "end": end_ms, "text": text}]

    # 1. 按句子结束标点切分，保留标点在句尾
    import re
    # 在结束标点后面切分（标点保留在前一句）
    parts = re.split(r"(?<=[。！？；!?;])\s*", text)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) <= 1:
        return [{"start": start_ms, "end": end_ms, "text": text}]

    # 2. 合并过短的子句（<3 字符）到前一段，避免碎片
    merged = []
    for p in parts:
        if merged and len(p) < 3:
            merged[-1] = merged[-1] + p
        else:
            merged.append(p)
    if len(merged) <= 1:
        return [{"start": start_ms, "end": end_ms, "text": text}]

    # 3. 按字符长度比例插值时间戳
    total_chars = sum(len(p) for p in merged)
    if total_chars == 0:
        return [{"start": start_ms, "end": end_ms, "text": text}]

    result = []
    cursor = start_ms
    for i, p in enumerate(merged):
        if i == len(merged) - 1:
            # 最后一段直接到 end_ms，避免 rounding 误差
            sub_end = end_ms
        else:
            sub_end = start_ms + int(duration * (sum(len(x) for x in merged[:i+1]) / total_chars))
        result.append({
            "start": cursor,
            "end": max(sub_end, cursor + 50),  # 最小 50ms，防止 0 长段
            "text": p,
        })
        cursor = sub_end

    return result


def _strip_paraformer_preds(text: str) -> str:
    """清除 Paraformer 输出里的 [preds:...] 标记，把各段拼成干净文本。

    Paraformer 自带说话人分段标注，格式为：
        [preds:(说话人A的内容)(说话人B的内容) pred]
    这里去掉 preds 标记，把每段括号内容取出并拼接。
    """
    import re
    # 去掉首尾的 [preds: 和 pred]
    text = re.sub(r"\[preds:", "", text)
    text = re.sub(r"\s*pred\]", "", text)
    # 把剩余的 ( ... ) 段提取出来，去掉括号
    segments = re.findall(r"\(([^)]*)\)", text)
    if segments:
        text = " ".join(s.strip() for s in segments if s.strip())
    else:
        # 没识别到 preds 格式，直接当普通文本清理
        text = re.sub(r"\s+", " ", text).strip()
    return text


def _apply_volume_gain(data, gain: str = "auto"):
    """对音频数据（float64 numpy 数组，范围 [-1,1]）应用音量增益。

    gain:
      "auto"  — 峰值归一化到 0.9（自动放大到不爆音的最大音量）
      "2x"/"3x"/"5x" — 固定倍数增益，超过 ±1.0 会削波
      "off"/""/None — 不处理
    """
    import numpy as np

    if gain in ("off", "", None):
        return data
    if data is None or data.size == 0:
        return data

    if gain == "auto":
        peak = float(np.max(np.abs(data)))
        if peak < 1e-5:
            logger.info("音量增益：音频近乎静音（peak=%.6f），跳过", peak)
            return data
        target_peak = 0.9
        scale = target_peak / peak
        logger.info("音量增益（自动峰值归一化）：peak=%.4f → %.4f，缩放 %.1fx",
                    peak, target_peak, scale)
        data = data * scale
    else:
        try:
            factor = float(gain.replace("x", "").replace("X", ""))
        except (ValueError, AttributeError):
            logger.warning("音量增益参数无法解析：%r，跳过", gain)
            return data
        if factor <= 1.0:
            return data
        logger.info("音量增益（固定倍数）：%.1fx", factor)
        data = data * factor

    # 削波保护
    data = np.clip(data, -1.0, 1.0)
    return data


def _prepare_audio_16k(audio_path: str, gain: str = "auto") -> str:
    """确保音频是 16kHz 单声道 WAV；若不是则自动转换，返回转换后的路径。

    funasr-onnx 的 VAD/ASR 模型都是按 16kHz 训练的，非 16kHz 输入会被误判为静音。
    录音笔原始文件可能是 48kHz，这里自动重采样到 16kHz。

    gain: 音量增益 "auto"(峰值归一化) / "2x"/"3x"/"5x"(固定倍数) / "off"(不处理)
    """
    import os
    import tempfile
    import soundfile as sf
    import numpy as np

    need_gain = gain not in ("off", "", None)

    try:
        data, sr = sf.read(audio_path)
    except Exception:
        # raw Opus 码流（无 OggS 头）需要先包装为合法 Ogg Opus
        try:
            with open(audio_path, "rb") as _f:
                _head = _f.read(4)
            if audio_path.lower().endswith(".opus") and _head != b"OggS":
                from .protocol import wrap_raw_opus_file
                import tempfile as _tf
                _tmp = _tf.NamedTemporaryFile(
                    suffix=".ogg.opus", delete=False)
                _tmp.close()
                wrap_raw_opus_file(audio_path, _tmp.name)
                logger.info("raw Opus 已包装为 Ogg: %s", _tmp.name)
                audio_path = _tmp.name
        except Exception as _exc:
            logger.warning("raw Opus 包装失败: %s", _exc)
        logger.info("soundfile 读不了，用 pydub 解码再存 wav")
        from pydub import AudioSegment
        seg = AudioSegment.from_file(audio_path)
        if seg.frame_rate == 16000 and seg.channels == 1 and not need_gain:
            return audio_path
        seg16 = seg.set_frame_rate(16000).set_channels(1)
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        seg16.export(tmp.name, format="wav")
        logger.info("pydub 重采样 16k 单声道：%s -> %s (%.1fs)",
                    audio_path, tmp.name, len(seg16) / 1000.0)
        if not need_gain:
            return tmp.name
        # 需要增益：用 soundfile 重新读取 pydub 输出的 wav
        data, sr = sf.read(tmp.name)

    # soundfile 读取成功（或从 pydub 输出读回）
    if sr == 16000 and (data.ndim == 1 or data.shape[1] == 1) and not need_gain:
        return audio_path

    # 重采样到 16kHz + 单声道
    if data.ndim > 1:
        data = data.mean(axis=1)
    target_sr = 16000
    if sr != target_sr:
        # 线性插值重采样
        duration = len(data) / sr
        target_len = int(duration * target_sr)
        src_idx = np.linspace(0, len(data) - 1, target_len)
        data = np.interp(src_idx, np.arange(len(data)), data)
        logger.info("线性插值重采样：%dHz -> %dHz，%.1fs 音频", sr, target_sr, duration)

    # 音量增益
    if need_gain:
        data = _apply_volume_gain(data, gain)

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    sf.write(tmp.name, data, target_sr)
    return tmp.name


def _transcribe_once(
    audio_path: str,
    language: str,
    model_key: str,
    device: str,
    spk: bool,
    volume_gain: str = "auto",
) -> tuple[str, Optional[list], Optional[list], str]:
    """执行一次转写，返回 (文本, 说话人分段列表或 None, 片段列表或 None, spk_mode)。

    片段列表格式：[{"start": ms, "end": ms, "text": ..., "speaker": 0}, ...]
    spk_mode: "off" | "campplus" | "fallback"
    """
    _ensure_audio_deps(audio_path)

    # 自动重采样到 16kHz + 音量增益（funasr-onnx 模型要求）
    prepared_path = _prepare_audio_16k(audio_path, gain=volume_gain)
    if prepared_path != audio_path:
        logger.info("音频已预处理（重采样/增益）：%s", prepared_path)

    container = _get_model(model_key, device, SPK_MODEL_DEFAULT if not spk else "cam++")
    asr_model = container["model"]
    vad_model = container["vad"]
    spk_model = container["spk"]

    # VAD 切片：segments 是 [[start_ms, end_ms], ...] 毫秒列表
    logger.info("送入 VAD 的音频：%s", prepared_path)
    vad_result = vad_model([prepared_path])
    segments = vad_result[0] if vad_result else None
    logger.info("VAD 返回：%d 个片段", len(segments) if segments else 0)

    # 根据模型选择合适的输出清理函数
    if model_key == "paraformer":
        _clean = _strip_paraformer_preds
    else:
        _clean = _strip_sensevoice_tokens

    if not segments:
        text = asr_model([prepared_path])
        text = text[0] if text else ""
        if isinstance(text, list):
            text = " ".join(str(t) for t in text if t)
        return _clean(str(text)), None, None, "off"

    # 按 VAD 切片做 ASR：seg 是 [start_ms, end_ms]，需要切出音频片段存临时 wav
    import tempfile
    import soundfile as sf
    import numpy as np

    full_audio, full_sr = sf.read(prepared_path)
    if full_audio.ndim > 1:
        full_audio = full_audio.mean(axis=1)

    texts: list[str] = []
    seg_data: list[dict] = []
    for idx, seg in enumerate(segments):
        try:
            start_ms, end_ms = int(seg[0]), int(seg[1])
            start_sample = int(start_ms * full_sr / 1000)
            end_sample = int(end_ms * full_sr / 1000)
            chunk = full_audio[start_sample:end_sample]
            if len(chunk) == 0:
                texts.append("")
                seg_data.append({"start": start_ms, "end": end_ms, "text": ""})
                continue
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.close()
            sf.write(tmp.name, chunk, full_sr)
            result = asr_model([tmp.name])
            text = result[0] if result else ""
            if isinstance(text, list):
                text = " ".join(str(t) for t in text if t)
            text = _clean(str(text))
            texts.append(text)
            # 基于标点的二次断句：把长 VAD 段（可能含多句）拆成句子级子段
            sub_segs = _split_segment_by_punctuation(text, start_ms, end_ms)
            seg_data.extend(sub_segs)
            logger.debug("片段 %d (%d-%dms)：%s → 拆分为 %d 子段",
                         idx, start_ms, end_ms, text[:50], len(sub_segs))
        except Exception as exc:
            logger.warning("片段 %d 识别失败：%s", idx, exc)
            texts.append("")
            seg_data.append({"start": int(seg[0]), "end": int(seg[1]), "text": ""})

    full_text = " ".join(t for t in texts if t)

    # 说话人分离（可选）
    speaker_segments = None
    if spk and spk_model and segments:
        try:
            spk_result = spk_model([prepared_path])
            speaker_segments = spk_result[0] if spk_result else None
        except Exception as exc:
            logger.warning("说话人分离失败：%s", exc)

    # 将说话人标签融合到每段 seg_data，方便前端直接展示
    spk_mode = _merge_speaker_into_segments(seg_data, speaker_segments, spk)

    return full_text, speaker_segments, seg_data, spk_mode


def _merge_speaker_into_segments(
    seg_data: list[dict],
    speaker_segments: Optional[list],
    spk_enable: bool,
) -> str:
    """把说话人信息合并到 seg_data 里，每段加 `speaker` 字段（0 开始）。

    返回 spk_mode: "off" | "campplus" | "fallback"

    策略：
    1) 如果有 Campplus 输出（speaker_segments 非空）：按时间重叠度匹配，取重叠最大的说话人
    2) 否则如果用户打开了 spk 开关：退化方案——按段序号轮流分配 0/1（Paraformer
       自带分段的效果很弱，只能给出视觉上的"双说话人"区分，聊胜于无）
    3) 没开 spk：不加 speaker 字段
    """
    if not seg_data:
        return "off"
    if not spk_enable:
        return "off"
    if speaker_segments:
        # 标准做法：按时间重叠匹配
        try:
            spk_list = list(speaker_segments)
            if spk_list and isinstance(spk_list[0], (list, tuple)):
                for seg in seg_data:
                    s, e = seg["start"], seg["end"]
                    best_spk = None
                    best_overlap = -1
                    for spk_seg in spk_list:
                        try:
                            ss, se = int(spk_seg[0]), int(spk_seg[1])
                            spk_id = int(spk_seg[2]) if len(spk_seg) > 2 else 0
                        except (TypeError, ValueError):
                            continue
                        overlap = max(0, min(e, se) - max(s, ss))
                        if overlap > best_overlap:
                            best_overlap = overlap
                            best_spk = spk_id
                    if best_spk is not None:
                        seg["speaker"] = best_spk
                logger.info("说话人分配完成（Campplus），段数：%d", len(seg_data))
                return "campplus"
        except Exception as exc:
            logger.warning("说话人合并失败（Campplus），退化到按段序号交替：%s", exc)
    # 退化：按段序号轮流分配（0/1/0/1 ...），让用户看到视觉上的区分
    for idx, seg in enumerate(seg_data):
        seg["speaker"] = idx % 2
    logger.info("说话人分配：按段序号交替（退化方案，spk=%s）", spk_enable)
    return "fallback"


async def transcribe_file(
    path: Path,
    language: str = "auto",
    model: str = "sensevoice",
    spk: bool = False,
    device: str = DEVICE_DEFAULT,
    volume_gain: str = "auto",
) -> dict:
    """异步转写文件（内部用线程池执行阻塞推理）。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: transcribe_file_sync(path, language, model, spk, device, volume_gain))


def transcribe_file_sync(
    path: Path,
    language: str = "auto",
    model: str = "sensevoice",
    spk: bool = False,
    device: str = DEVICE_DEFAULT,
    volume_gain: str = "auto",
) -> dict:
    """同步转写文件，返回 {"text": ..., "speaker_segments": ..., "segments": ...}。"""
    if model not in MODELS:
        raise ValueError(f"未知 model={model}，可选：{list(MODELS.keys())}")

    if language not in MODELS[model]["languages"]:
        raise ValueError(
            f"language={language} 不被 model={model} 支持，"
            f"可选：{list(MODELS[model]['languages'])}")

    audio_path = str(path.resolve())
    text, speaker_segments, seg_data, spk_mode = _transcribe_once(
        audio_path, language, model, device, spk, volume_gain)

    result = {"text": text, "language": language, "model": model,
              "spk_mode": spk_mode}
    if speaker_segments:
        result["speaker_segments"] = speaker_segments
    if seg_data:
        result["segments"] = seg_data
    return result


def reset() -> None:
    """释放已加载的模型，清内存。"""
    global _models, _current_config
    _models.clear()
    _current_config = None
    logger.info("ASR 模型已释放")
