# -*- coding: utf-8 -*-
"""
摘要模块：
- 优先调用 OpenAI 兼容接口生成结构化摘要（总体摘要 / 关键要点 / 章节 / 深度总结）。
- 未配置 API Key 时，使用基于 TF-IDF + 位置权重的抽取式摘要作为降级方案。
"""
from __future__ import annotations
import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any, Optional

import httpx

from config import settings


SUMMARY_PROMPT = """你是一名专业的视频内容总结助手。请根据下面的视频转写文本，输出结构化的总结。
要求：
1. 总体摘要：用 3-5 句话概括视频核心内容。
2. 关键要点：提取 5-8 条关键信息，每条一句话，不要与摘要重复。
3. 章节切分：按语义将视频划分为 3-8 个章节，给出每章的起始时间（秒）、标题和简述。
4. 无论转写文本是中文还是英文，所有输出一律使用简体中文。
请严格以 JSON 格式输出，键为 summary、key_points、chapters。
chapters 中每项包含 start（秒，整数）、title、description。

转写文本如下：
{text}"""

INSIGHT_SYSTEM = """你是资深内容编辑，擅长写有观点、有温度、有细节的视频观后感。只输出正文段落，不要标题、不要分条、不要 Markdown。"""

INSIGHT_PROMPT = """请根据下面的视频转写，写一段 400-600 字的观后感式总结。
要求：
1. 开头两三句交代视频讲了谁、经历了什么事，让没看过的人也能明白；
2. 中间结合至少两个转写中的具体细节或原话（数字、对话、转折），点出视频真正想表达的核心观点；
3. 结尾落到对普通观众的启示或警示，语气真诚、有温度，用一句有力的话收束；
4. 全文连贯成两到三段，不要分条、不要小标题、不要写成行动清单；
5. 不要编造转写中没有的人名、数字或情节；
6. 无论转写是中文还是英文，一律使用简体中文。

转写如下：
{text}"""


def generate_insight(text: str) -> str:
    """单独生成观后感式深度总结；未配置 LLM 时返回空字符串。"""
    if not settings.LLM_API_KEY:
        return ""
    raw = _call_llm_text(INSIGHT_PROMPT.format(text=text[:24000]),
                         system=INSIGHT_SYSTEM, temperature=0.6, max_tokens=1024)
    return (raw or "").strip()


def _call_llm(prompt: str) -> Optional[str]:
    if not settings.LLM_API_KEY:
        return None
    headers = {
        "Authorization": f"Bearer {settings.LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.LLM_MODEL,
        "messages": [
            {"role": "system", "content": "你是严谨的视频内容总结助手，只输出 JSON。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 2048,
    }
    url = f"{settings.LLM_BASE_URL.rstrip('/')}/chat/completions"
    # 优先带上 response_format 强制 JSON；部分兼容接口不支持该参数，失败后去掉重试一次
    for use_json_mode in (True, False):
        body = dict(payload)
        if use_json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            with httpx.Client(timeout=settings.LLM_TIMEOUT) as client:
                resp = client.post(url, headers=headers, json=body)
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"[LLM 调用失败{'（JSON 模式）' if use_json_mode else ''}] {e}")
    return None


def _as_text(value: Any) -> str:
    """把 LLM 返回的各种形态（字符串 / 对象 / 列表）统一成一段文本。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("text", "content", "summary", "value", "description"):
            if isinstance(value.get(key), str):
                return value[key].strip()
        return " ".join(str(v).strip() for v in value.values() if isinstance(v, str)).strip()
    if isinstance(value, list):
        return " ".join(_as_text(v) for v in value if _as_text(v))
    return str(value).strip() if value is not None else ""


def _parse_llm_result(raw: str, total_duration: float = 0) -> Dict[str, Any]:
    """尽量稳健地从 LLM 输出中解析 JSON。"""
    # 去掉代码块包裹
    raw = re.sub(r"```(?:json)?", "", raw).strip()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        # 尝试截取第一个 { 到最后一个 }
        s, e = raw.find("{"), raw.rfind("}")
        if s != -1 and e != -1 and e > s:
            try:
                obj = json.loads(raw[s:e + 1])
            except json.JSONDecodeError:
                obj = {}
        else:
            obj = {}
    if not isinstance(obj, dict):
        obj = {}

    # 部分模型会把 summary 写成 {"text": "..."}，或把要点写成对象数组，这里统一归一化
    key_points = [_as_text(p) for p in (obj.get("key_points") or [])]
    insight = _as_text(obj.get("insight") or obj.get("review") or obj.get("comment"))
    chapters = []
    for c in (obj.get("chapters") or []):
        if not isinstance(c, dict):
            continue
        start = c.get("start", 0)
        try:
            start = int(float(start))
        except (TypeError, ValueError):
            start = 0
        chapters.append({
            "start": start,
            "title": _as_text(c.get("title") or c.get("name") or c.get("heading"))[:40],
            "description": _as_text(c.get("description") or c.get("summary") or c.get("desc"))[:120],
        })
    chapters.sort(key=lambda c: c["start"])
    if len(chapters) > 8:
        step = len(chapters) / 8
        chapters = [chapters[int(i * step)] for i in range(8)]

    # 输入的时间戳是 [mm:ss]，部分模型会把"分钟"直接填进 start 字段。
    # 若所有 start 都落在总分钟数之内，说明单位错了，统一换算成秒。
    if chapters and total_duration > 0:
        max_start = max(c["start"] for c in chapters)
        if max_start > 0 and max_start <= total_duration / 60:
            for c in chapters:
                c["start"] *= 60

    return {
        "summary": _as_text(obj.get("summary")),
        "key_points": [p for p in key_points if p],
        "chapters": [c for c in chapters if c["title"]],
        "insight": insight,
    }


def _text_with_timestamps(text: str, segments: Optional[List[Dict[str, Any]]], max_chars: int = 24000) -> str:
    """给转写文本带上 [mm:ss] 时间戳，让模型划分章节时有真实依据。"""
    if not segments:
        return text[:max_chars]
    lines = []
    for seg in segments:
        t = (seg.get("text") or "").strip()
        if not t:
            continue
        s = int(float(seg.get("start", 0)))
        lines.append(f"[{s // 60:02d}:{s % 60:02d}] {t}")
    return "\n".join(lines)[:max_chars]


def summarize_with_llm(text: str, segments: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict[str, Any]]:
    """使用 LLM 生成结构化摘要；失败返回 None。"""
    if not settings.LLM_API_KEY:
        return None
    # 超长文本截断，避免超出上下文
    truncated = _text_with_timestamps(text, segments)
    prompt = SUMMARY_PROMPT.format(text=truncated)
    raw = _call_llm(prompt)
    if not raw:
        return None
    total_duration = float(segments[-1].get("end", 0)) if segments else 0.0
    return _parse_llm_result(raw, total_duration)


# ---------------- 本地抽取式降级 ----------------

import jieba
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_END_RE = re.compile(r"[。！？!?；;]$")


def _join(a: str, b: str) -> str:
    """中英文混排拼接：两侧都是中文时不加空格。"""
    if not a:
        return b
    if _CJK_RE.search(a[-1]) or _CJK_RE.search(b[0]):
        return a + b
    return a + " " + b


def _build_units(segments: List[Dict[str, Any]], max_len: int = 60, gap: float = 1.0) -> List[Dict[str, Any]]:
    """
    把 ASR 分段合并成"句级单元"。

    Whisper 输出的分段通常没有句号，直接按标点断句会把整篇当成一句，
    导致摘要、要点、建议三块内容完全一样。这里改为按长度与停顿间隔合并：
    - 累积长度超过 max_len 或与上一段间隔超过 gap 秒，就切分为一个单元；
    - 遇到句末标点则立即断句。
    """
    units: List[Dict[str, Any]] = []
    buf, buf_start, last_end = "", None, None
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        if buf and (len(buf) + len(text) > max_len
                    or (last_end is not None and float(seg.get("start", 0)) - last_end > gap)):
            units.append({"start": buf_start, "end": last_end, "text": buf})
            buf = ""
        if not buf:
            buf_start = float(seg.get("start", 0))
        buf = _join(buf, text)
        last_end = float(seg.get("end", 0))
        if _END_RE.search(buf):
            units.append({"start": buf_start, "end": last_end, "text": buf})
            buf, buf_start = "", None
    if buf:
        units.append({"start": buf_start, "end": last_end, "text": buf})
    return [u for u in units if len(u["text"]) >= 8]


def _units_from_text(text: str, max_len: int = 80) -> List[Dict[str, Any]]:
    """没有分段信息时的兜底：先按标点切句，过长的再按长度切块。"""
    parts = [p.strip() for p in re.split(r"[。！？!?；;]|\n", text) if p.strip()]
    units: List[Dict[str, Any]] = []
    for part in parts:
        for i in range(0, len(part), max_len):
            chunk = part[i:i + max_len].strip()
            if len(chunk) >= 8:
                units.append({"start": 0, "end": 0, "text": chunk})
    return units


def _score_units(units: List[Dict[str, Any]]) -> np.ndarray:
    """用 TF-IDF 给每个单元打分，并叠加轻微的位置权重。"""
    texts = [u["text"] for u in units]
    try:
        vec = TfidfVectorizer(tokenizer=jieba.lcut, token_pattern=None)
        scores = np.asarray(vec.fit_transform(texts).sum(axis=1)).flatten()
    except Exception:
        scores = np.array([len(t) for t in texts], dtype=float)
    n = len(texts)
    pos_weight = np.array([1.0 + 0.2 * (1 - i / n) for i in range(n)])
    return scores * pos_weight


def _split_chapters(units: List[Dict[str, Any]], max_chapters: int = 6) -> List[Dict[str, Any]]:
    """按时间顺序把句子单元均匀切分为章节。"""
    if not units:
        return []
    per = max(1, (len(units) + max_chapters - 1) // max_chapters)
    chapters = []
    for i in range(0, len(units), per):
        chunk = units[i:i + per]
        if not chunk:
            continue
        first = chunk[0]["text"]
        chapters.append({
            "start": int(chunk[0]["start"] or 0),
            "title": first[:20] + ("..." if len(first) > 20 else ""),
            "description": _join(chunk[0]["text"], chunk[1]["text"])[:80] if len(chunk) > 1 else first[:80],
        })
    return chapters


def summarize_local(text: str, segments: List[Dict[str, Any]]) -> Dict[str, Any]:
    """本地抽取式摘要（无 LLM 时使用）：摘要与要点各取不同句子，避免重复。"""
    units = _build_units(segments) or _units_from_text(text)
    if not units:
        return {"summary": text[:200], "key_points": [], "chapters": [], "insight": ""}

    scores = _score_units(units)
    ranked = scores.argsort()[::-1].tolist()
    n = len(units)

    # 短文本时少取几句，避免摘要把全部单元吃掉、要点只剩空数组
    summary_k = 1 if n <= 6 else (2 if n <= 15 else 3)
    summary_idx = sorted(ranked[:summary_k])
    used = set(summary_idx)

    point_idx = [i for i in ranked if i not in used][:6]
    point_idx.sort()

    summary = ""
    for i in summary_idx:
        summary = _join(summary, units[i]["text"])

    return {
        "summary": summary,
        "key_points": [units[i]["text"] for i in point_idx],
        "chapters": _split_chapters(units),
        # 观后感式总结需要 LLM 才能生成，本地降级时不编造
        "insight": "",
    }


# ---------------- 转写纠错（同音字/近音字误识别） ----------------

_CORRECT_SYSTEM = "你是语音识别纠错助手。严格按输入的行数与序号输出，每行格式为「序号. 修正后文本」，不输出任何解释。"

_CORRECT_PROMPT = """下面是语音识别（ASR）的转写结果，其中可能存在同音字或近音字的识别错误，例如把“每个月”误识别为“美国约”、把“细算账”误识别为“西川藏”。
请结合上下文修正这些明显的识别错误，要求：
1. 只修改明显听错的字词，保持原句的结构、语气和标点不变；
2. 输出的行数、序号必须与输入完全一致，不得合并或拆分行；
3. 拿不准的人名、数字、专有名词保持原样，不要猜测；
4. 直接输出带序号的修正结果，不要输出任何解释。

转写如下：
{text}"""

_CORRECT_LINE_RE = re.compile(r"^\s*(\d+)\s*[.、:：]\s*(.*)$")


def correct_segments(segments: List[Dict[str, Any]], progress_cb=None) -> List[Dict[str, Any]]:
    """
    用 LLM 修正 ASR 的同音字/近音字误识别（如"美国约"->"每个月"）。
    逐块处理并保留时间戳；未配置 LLM 或某块解析失败时，该块保留原文。
    """
    if not settings.LLM_API_KEY or not segments:
        return segments

    # 按 ~1000 字分块，记录每块包含的段索引
    blocks: List[List[int]] = []
    cur: List[int] = []
    cur_len = 0
    for idx, seg in enumerate(segments):
        t = (seg.get("text") or "").strip()
        if not t:
            continue
        if cur and cur_len + len(t) > _CHUNK_CHARS:
            blocks.append(cur)
            cur, cur_len = [], 0
        cur.append(idx)
        cur_len += len(t)
    if cur:
        blocks.append(cur)

    corrected = [dict(s) for s in segments]

    def fix_block(bi: int, idxs: List[int]) -> None:
        numbered = "\n".join(f"{j + 1}. {segments[i]['text'].strip()}" for j, i in enumerate(idxs))
        raw = _call_llm_text(_CORRECT_PROMPT.format(text=numbered),
                             system=_CORRECT_SYSTEM, temperature=0.1, max_tokens=3000)
        if not raw:
            return
        parsed: Dict[int, str] = {}
        for line in raw.splitlines():
            m = _CORRECT_LINE_RE.match(line.strip())
            if m and m.group(2).strip():
                parsed[int(m.group(1))] = m.group(2).strip()
        # 行数对不上说明输出不可靠，整块放弃，避免句序错位
        if len(parsed) < len(idxs):
            print(f"[纠错跳过] 第{bi + 1}块输出行数不符：{len(parsed)}/{len(idxs)}")
            return
        for j, i in enumerate(idxs):
            if parsed.get(j + 1):
                corrected[i]["text"] = parsed[j + 1]

    # 各块互不依赖，并发请求以缩短总耗时
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(fix_block, bi, idxs) for bi, idxs in enumerate(blocks)]
        for done, fut in enumerate(futures, 1):
            fut.result()
            if progress_cb:
                progress_cb(done, len(blocks))
    return corrected


# ---------------- 台词稿整理（简体中文 + 标点 + 翻译） ----------------

_POLISH_SYSTEM = """你是视频字幕整理员，只输出整理后的台词正文，不要输出“原文/输出/翻译”等任何标签。
规则：
1. 补全标点符号，按语义合理断句，不要改变原意；
2. 繁体中文字符必须转换为简体中文；
3. 中文句子只输出一次简体中文，不要重复输出同一句；
4. 句中夹带的英文单词或短句保留原文，并在其后用括号给出中文意思；
5. 结合上下文修正明显的语音识别同音字错误（如“美国约”应为“每个月”），拿不准的保持原样。

英文台词的正确输出格式：
We bought the whole store.
我们买下了整个商店。
It is crazy.
这太疯狂了。"""

_EN_SYSTEM = """你是字幕翻译员。输出必须是双语对照：每个英文句子单独一行并保留英文原文，下一行给出该句的简体中文翻译。
严格要求：
1. 不允许只输出中文，也不允许省略英文原文；
2. 不要合并句子，不要输出“原文/翻译”等标签或任何解释；
3. 英文原句只补标点，不改写内容。"""

_POLISH_PROMPT = "待整理文本：\n{text}"
_EN_PROMPT = "英文台词：\n{text}"

_CHUNK_CHARS = 1000


def _is_english(text: str) -> bool:
    """粗判文本主体语言：以拉丁字母为主且汉字很少时视为英文。"""
    cjk = len(_CJK_RE.findall(text))
    letters = len(re.findall(r"[A-Za-z]", text))
    return letters >= 30 and cjk * 3 < letters


def _call_llm_text(prompt: str, system: str = _POLISH_SYSTEM,
                   temperature: float = 0.2, max_tokens: Optional[int] = None) -> Optional[str]:
    """调用 LLM 并返回纯文本。"""
    if not settings.LLM_API_KEY:
        return None
    headers = {
        "Authorization": f"Bearer {settings.LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.LLM_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    try:
        with httpx.Client(timeout=settings.LLM_TIMEOUT) as client:
            resp = client.post(
                f"{settings.LLM_BASE_URL.rstrip('/')}/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[台词整理失败] {e}")
        return None


def _chunk_segments(segments: List[Dict[str, Any]], max_chars: int = _CHUNK_CHARS) -> List[str]:
    """按字符数把转写分段切成若干块，逐块送 LLM，避免单次输出过长被截断。"""
    chunks, buf = [], ""
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        if buf and len(buf) + len(text) > max_chars:
            chunks.append(buf)
            buf = ""
        buf = _join(buf, text)
    if buf:
        chunks.append(buf)
    return chunks


def polish_lines(segments: List[Dict[str, Any]], progress_cb=None) -> Optional[str]:
    """
    生成台词稿：简体中文、带标点，英文内容翻译成中文。
    未配置 LLM 时返回 None（前端回退显示原始转写）。
    """
    if not settings.LLM_API_KEY:
        return None
    chunks = _chunk_segments(segments)
    if not chunks:
        return None

    def polish(chunk: str) -> str:
        if _is_english(chunk):
            out = _call_llm_text(_EN_PROMPT.format(text=chunk), _EN_SYSTEM)
        else:
            out = _call_llm_text(_POLISH_PROMPT.format(text=chunk))
        # 单块失败时保留该块原文，不影响整体结果
        return (out or chunk).strip()

    # 分块并行处理，显著缩短长视频的台词整理耗时
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(polish, c) for c in chunks]
        parts = []
        for done, fut in enumerate(futures, 1):
            parts.append(fut.result())
            if progress_cb:
                progress_cb(done, len(chunks))
    return "\n\n".join(p for p in parts if p)


def summarize(text: str, segments: List[Dict[str, Any]]) -> Dict[str, Any]:
    """统一入口：结构化摘要与深度总结互不依赖，并发请求以缩短耗时。"""

    def structured() -> Dict[str, Any]:
        result = summarize_with_llm(text, segments)
        if not (result and result.get("summary")):
            result = summarize_local(text, segments) if settings.USE_LOCAL_FALLBACK else {
                "summary": "", "key_points": [], "chapters": [], "insight": ""}
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_struct = pool.submit(structured)
        f_insight = pool.submit(generate_insight, text)
        result = f_struct.result()
        result["insight"] = f_insight.result()
    return result
