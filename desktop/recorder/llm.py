"""AI 摘要 LLM 封装：统一 OpenAI 兼容接口。

支持三种 provider（通过配置切换）：
  - deepseek:  DeepSeek 官方 API （推荐，中文效果好且便宜）
  - ollama:    本地 Ollama （完全离线，零成本）
  - relay:     第三方 API 中转站 （通用 OpenAI 格式兼容）

所有 provider 都走 OpenAI 兼容的 /chat/completions 接口，
因此代码只需一套实现，差异化只体现在 base_url / api_key / model_name。
"""

from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ============================================================= 配置持久化


@dataclass
class LLMConfig:
    """LLM 调用配置。所有字段可由前端设置并保存到 JSON。"""
    # provider: deepseek | ollama | relay
    provider: str = "deepseek"
    # API 基础地址
    #   - deepseek: https://api.deepseek.com
    #   - ollama:   http://127.0.0.1:11434/v1
    #   - relay:    用户自定义
    base_url: str = "https://api.deepseek.com"
    api_key: str = ""
    # 模型名称
    #   - deepseek: deepseek-chat（推荐）/ deepseek-reasoner
    #   - ollama:   qwen2.5:7b / llama3.1:8b 等
    #   - relay:    中转站实际模型名
    model_name: str = "deepseek-chat"
    # 温度
    temperature: float = 0.3
    # 超时秒
    timeout: int = 120
    # 是否启用
    enabled: bool = False

    def as_safe_dict(self) -> Dict[str, Any]:
        """序列化，但把 api_key 打码，方便前端回显。"""
        d = asdict(self)
        if d["api_key"]:
            key = d["api_key"]
            if len(key) > 8:
                d["api_key_masked"] = key[:4] + "****" + key[-4:]
            else:
                d["api_key_masked"] = "****"
        return d


def _config_path(base_dir: Path) -> Path:
    return base_dir / ".llm_config.json"


def load_config(base_dir: Path) -> LLMConfig:
    """从 output_dir/.llm_config.json 读取；不存在返回默认（enabled=False）。"""
    p = _config_path(base_dir)
    if not p.is_file():
        return LLMConfig()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        cfg = LLMConfig()
        for k, v in raw.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        return cfg
    except Exception as exc:
        logger.warning("读取 LLM 配置失败，使用默认：%s", exc)
        return LLMConfig()


def save_config(base_dir: Path, cfg: LLMConfig) -> None:
    base_dir.mkdir(parents=True, exist_ok=True)
    p = _config_path(base_dir)
    p.write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=2),
                 encoding="utf-8")
    logger.info("LLM 配置已保存：provider=%s model=%s", cfg.provider, cfg.model_name)


# ============================================================= 统一调用


def _chat_completions_openai(cfg: LLMConfig, messages: list) -> str:
    """向 OpenAI 兼容 /chat/completions 发 POST，返回 assistant 文本内容。"""
    if not cfg.enabled:
        raise RuntimeError("AI 摘要未启用，请先在「摘要设置」中完成配置并开启")
    base = cfg.base_url.rstrip("/")
    url = f"{base}/chat/completions"
    body = {
        "model": cfg.model_name,
        "messages": messages,
        "temperature": cfg.temperature,
        "stream": False,
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"LLM HTTP {e.code}：{detail or e.reason}") from e
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", str(e))
        if "Connection refused" in str(reason):
            if cfg.provider == "ollama":
                raise RuntimeError(
                    "无法连接 Ollama。请确认已安装并启动：ollama serve") from e
            raise RuntimeError(
                f"无法连接 LLM 服务（{cfg.base_url}），请检查网络或 base_url") from e
        raise RuntimeError(f"LLM 网络错误：{reason}") from e
    try:
        j = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"LLM 响应不是合法 JSON：{raw[:200]}") from e
    # 标准结构：choices[0].message.content
    choices = j.get("choices") or []
    if not choices:
        err = j.get("error")
        raise RuntimeError(f"LLM 返回空 choices。错误信息：{err or j}")
    msg = choices[0].get("message", {})
    content = msg.get("content", "")
    if not isinstance(content, str) or not content.strip():
        # Ollama 可能返回其他结构
        raise RuntimeError(f"LLM 无文本内容返回：{json.dumps(j, ensure_ascii=False)[:300]}")
    return content.strip()


# ============================================================= 摘要提示词 & 结构化输出


SUMMARY_SYSTEM_PROMPT = """你是一名专业的会议纪要助手。你的任务是从用户提供的中文转写文本中，提取结构化摘要。

请严格按如下 JSON 格式输出，不要输出任何解释性文字、markdown 代码块或其他内容：
{
  "title": "一句话主题（不超过 25 字）",
  "summary": "2-5 句话的完整摘要，覆盖核心内容",
  "key_points": [
    "关键观点 1",
    "关键观点 2",
    "（3-8 条，用简洁短句）"
  ],
  "todos": [
    { "task": "待办事项描述", "owner": "负责人（如无则空字符串）", "due": "截止时间（如无则空字符串）" }
  ],
  "decisions": [
    "会议决定 1（如无则空数组）"
  ],
  "mindmap": "# 录音主题\\n## 核心内容\\n### 要点 A\\n### 要点 B\\n## 待办事项\\n## 会议决定"
}

要求：
1. 所有字段都必须是中文。
2. key_points 不要超过 10 条，选取真正重要的内容。
3. todos 中 action item 要具体，避免空话。
4. mindmap 字段必须是合法的 Markmap 语法（# 开头，使用 ## / ### 分层，不要用 - 或 * 列表），层级不超过 4 级。
5. 标题、摘要必须覆盖全文主要信息，不能漏核心结论。
"""


def _extract_json_block(text: str) -> Dict[str, Any]:
    """LLM 可能吐 ```json ... ``` 包裹或直接 JSON。尝试两种解析方式。"""
    s = text.strip()
    # 去掉 ```json / ``` 包裹
    if s.startswith("```"):
        # 找到第一行 ``` 结束位置
        first_nl = s.find("\n")
        if first_nl > 0:
            s = s[first_nl + 1:]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    # 有时模型前后还有废话，尝试截取第一个 { 到最后一个 }
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        s = s[start:end + 1]
    try:
        return json.loads(s)
    except json.JSONDecodeError as exc:
        # 做最后挽救：常见转义 / 尾逗号
        import re
        try:
            s2 = re.sub(r",\s*([}\]])", r"\1", s)
            return json.loads(s2)
        except Exception:
            raise RuntimeError(f"模型输出不是合法 JSON：{str(exc)}，原始内容：{text[:500]}")


def summarize(cfg: LLMConfig, transcript_text: str,
              source_filename: str = "") -> Dict[str, Any]:
    """调用 LLM 生成结构化摘要。

    返回 dict 结构见 SUMMARY_SYSTEM_PROMPT。失败会抛出 RuntimeError。
    """
    if not transcript_text or not transcript_text.strip():
        raise RuntimeError("转写文本为空，无法生成摘要")

    # 过长文本截断（DeepSeek 支持 128k，但为了省 token + 速度，3万字符约等于 2 万字够用）
    MAX_CHARS = 30000
    truncated = False
    text = transcript_text.strip()
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS]
        truncated = True

    user_msg = f"来源文件：{source_filename or '(未知)'}\n"
    if truncated:
        user_msg += f"（文本过长已截取前 {MAX_CHARS} 字，如不完整请分段摘要）\n"
    user_msg += "\n--- 转写内容开始 ---\n" + text + "\n--- 转写内容结束 ---"

    messages = [
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]
    raw = _chat_completions_openai(cfg, messages)
    data = _extract_json_block(raw)

    # ---- 兜底字段补全（防止模型某字段漏写导致前端渲染崩） ----
    if not isinstance(data.get("title"), str):
        data["title"] = source_filename or "录音摘要"
    if not isinstance(data.get("summary"), str):
        data["summary"] = "(模型未输出摘要)"
    for list_key in ("key_points", "decisions"):
        if not isinstance(data.get(list_key), list):
            data[list_key] = []
        data[list_key] = [str(x) for x in data[list_key] if str(x).strip()]
    if not isinstance(data.get("todos"), list):
        data["todos"] = []
    todos_norm = []
    for t in data["todos"]:
        if isinstance(t, dict):
            todos_norm.append({
                "task": str(t.get("task", "")).strip(),
                "owner": str(t.get("owner", "")).strip(),
                "due": str(t.get("due", "")).strip(),
            })
        elif isinstance(t, str) and t.strip():
            todos_norm.append({"task": t.strip(), "owner": "", "due": ""})
    data["todos"] = [t for t in todos_norm if t["task"]]

    # mindmap：模型没给就基于结构化结果自动拼一个
    if not isinstance(data.get("mindmap"), str) or not data["mindmap"].strip():
        data["mindmap"] = _build_default_mindmap(data)
    else:
        # 校验至少有 # 开头
        if not data["mindmap"].lstrip().startswith("#"):
            data["mindmap"] = _build_default_mindmap(data)

    return data


def _build_default_mindmap(d: Dict[str, Any]) -> str:
    """当模型没返回 mindmap 时，基于 key_points/todos/decisions 兜底拼一个。"""
    lines = [f"# {d.get('title') or '录音摘要'}"]
    lines.append("## 摘要")
    summary = d.get("summary") or ""
    # 摘要按句号拆成二级节点（不要太长）
    for sentence in [s.strip() for s in summary.replace("。", "。\n").split("\n") if s.strip()][:5]:
        lines.append(f"### {sentence[:40]}")
    if d.get("key_points"):
        lines.append("## 关键观点")
        for kp in d["key_points"][:10]:
            lines.append(f"### {str(kp)[:50]}")
    if d.get("todos"):
        lines.append("## 待办事项")
        for t in d["todos"][:10]:
            task = t["task"] if isinstance(t, dict) else str(t)
            lines.append(f"### {task[:50]}")
    if d.get("decisions"):
        lines.append("## 会议决定")
        for dc in d["decisions"][:8]:
            lines.append(f"### {str(dc)[:50]}")
    return "\n".join(lines)


# ============================================================= 摘要缓存（.summary.json）


def summary_cache_path(audio_path: Path) -> Path:
    """转写结果是 audio.transcript.json，摘要对应是 audio.summary.json。"""
    return audio_path.with_suffix(".summary.json")


def load_summary_cache(audio_path: Path) -> Optional[Dict[str, Any]]:
    p = summary_cache_path(audio_path)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("读取摘要缓存失败：%s", exc)
        return None


def save_summary_cache(audio_path: Path, data: Dict[str, Any]) -> None:
    p = summary_cache_path(audio_path)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ============================================================= 导出 Markdown


def export_markdown(audio_path: Path,
                    transcript: Dict[str, Any],
                    summary: Optional[Dict[str, Any]] = None) -> Path:
    """把转写 + 摘要导出为 Markdown，保存在同目录下 .md 文件。

    返回 Markdown 文件路径。
    """
    lines: list[str] = []
    title = (summary and summary.get("title")) or audio_path.stem
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"> 来源文件：`{audio_path.name}`")
    import datetime as _dt
    mtime = _dt.datetime.fromtimestamp(audio_path.stat().st_mtime) \
        if audio_path.exists() else _dt.datetime.now()
    lines.append(f"> 导出时间：{mtime.strftime('%Y-%m-%d %H:%M:%S')}")
    meta = []
    if transcript.get("model"):
        meta.append(f"转写模型：{transcript['model']}")
    if transcript.get("language"):
        meta.append(f"语言：{transcript['language']}")
    if transcript.get("spk"):
        meta.append(f"说话人分离：开（{transcript.get('spk_mode', '')}）")
    if meta:
        lines.append(f"> {' · '.join(meta)}")
    lines.append("")

    if summary:
        lines.append("## 🤖 AI 摘要")
        lines.append("")
        lines.append(f"**主题**：{summary.get('title', '')}")
        lines.append("")
        lines.append("**摘要**：")
        lines.append("")
        lines.append(summary.get("summary", ""))
        lines.append("")
        if summary.get("key_points"):
            lines.append("**🔑 关键观点**：")
            lines.append("")
            for kp in summary["key_points"]:
                lines.append(f"- {kp}")
            lines.append("")
        if summary.get("decisions"):
            lines.append("**✅ 会议决定**：")
            lines.append("")
            for dc in summary["decisions"]:
                lines.append(f"- {dc}")
            lines.append("")
        if summary.get("todos"):
            lines.append("**📋 待办事项**：")
            lines.append("")
            lines.append("| # | 事项 | 负责人 | 截止 |")
            lines.append("|---|------|--------|------|")
            for i, t in enumerate(summary["todos"], 1):
                task = t.get("task", "") if isinstance(t, dict) else str(t)
                owner = t.get("owner", "") if isinstance(t, dict) else ""
                due = t.get("due", "") if isinstance(t, dict) else ""
                lines.append(f"| {i} | {task} | {owner} | {due} |")
            lines.append("")
        lines.append("---")
        lines.append("")

    lines.append("## 📝 转写全文")
    lines.append("")
    segs = transcript.get("segments") or []
    if segs:
        # 带分段/时间戳/说话人
        for seg in segs:
            ts = seg.get("timestamp")
            spk = seg.get("speaker")
            text = seg.get("text") or ""
            parts = []
            if isinstance(ts, (list, tuple)) and len(ts) >= 2:
                def _fmt(sec: float) -> str:
                    sec = int(max(0, sec))
                    m, s = divmod(sec, 60)
                    h, m = divmod(m, 60)
                    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
                parts.append(f"`[{_fmt(ts[0])}-{_fmt(ts[1])}]`")
            if spk:
                parts.append(f"**{spk}**")
            prefix = " ".join(parts)
            if prefix:
                lines.append(f"{prefix}：{text}")
            else:
                lines.append(f"- {text}")
            lines.append("")
    else:
        lines.append(transcript.get("text") or "")
        lines.append("")

    md = "\n".join(lines)
    md_path = audio_path.with_suffix(".md")
    md_path.write_text(md, encoding="utf-8")
    logger.info("已导出 Markdown：%s", md_path.name)
    return md_path
