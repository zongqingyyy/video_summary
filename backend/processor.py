# -*- coding: utf-8 -*-
"""
处理流水线：上传文件 -> 抽取音频 -> 语音识别 -> 摘要 -> 落库。
使用后台任务 + 内存状态表提供进度查询。
"""
from __future__ import annotations
import asyncio
import os
import shutil
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, Any, Optional

from config import settings
import database
import asr as asr_mod
import summarizer
import downloader


# 内存进度表：task_id -> {percent, message, stage}
_PROGRESS: Dict[str, Dict[str, Any]] = {}


def get_progress(task_id: str) -> Optional[Dict[str, Any]]:
    return _PROGRESS.get(task_id)


def _set_progress(task_id: str, percent: float, message: str, stage: str = "") -> None:
    _PROGRESS[task_id] = {
        "percent": round(percent * 100, 1),
        "message": message,
        "stage": stage,
        "updated_at": time.time(),
    }


def _extract_audio(video_path: Path, task_id: str, mark=None, base: float = 0.0, span: float = 1.0) -> Path:
    """用 ffmpeg 从视频中抽取 16kHz 单声道 wav；若无 ffmpeg 则直接尝试用原文件作为音频。"""
    audio_path = video_path.with_suffix(".wav")
    if mark:
        mark(0.05, "正在抽取音频...", "extract")
    else:
        _set_progress(task_id, 0.05, "正在抽取音频...", "extract")
    try:
        cmd = [
            "ffmpeg", "-y", "-i", str(video_path),
            "-vn", "-ac", "1", "-ar", "16000",
            str(audio_path),
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        return audio_path
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        print(f"[ffmpeg 抽取失败] {e}，将直接对原文件进行识别")
        # 部分格式（如 mp3、wav）faster-whisper 可直接读取
        return video_path


def _run_pipeline(
    task_id: str,
    video_path: Path,
    progress_base: float = 0.0,
    progress_span: float = 1.0,
    cleanup_input: bool = False,
) -> None:
    def mark(pct: float, message: str, stage: str) -> None:
        _set_progress(task_id, min(progress_base + pct * progress_span, 1.0), message, stage)

    try:
        database.update_task(task_id, status="processing")
        mark(0.02, "任务已开始", "start")

        # 1. 抽取音频
        audio_path = _extract_audio(video_path, task_id, mark, progress_base, progress_span)

        # 2. ASR
        def on_progress(pct, msg):
            # 转写完成后仍需生成摘要，这里封顶 90%，避免进度提前到 100%
            mark(min(pct, 0.90), msg, "asr")

        asr_result = asr_mod.transcribe(audio_path, progress_cb=on_progress)
        segments = asr_result["segments"]
        full_text = asr_result["text"]

        # 2.5 同音字纠错（依赖 LLM，失败时自动保留原文；英文转写跳过）
        if settings.LLM_API_KEY and (asr_result.get("language") or "").startswith("zh"):
            def on_correct(i, total):
                mark(0.90, f"正在校正识别文本 {i + 1}/{total}...", "correct")

            segments = summarizer.correct_segments(segments, on_correct)
            full_text = " ".join(s["text"] for s in segments if s.get("text"))

        # 清理中间音频
        if audio_path != video_path and audio_path.exists():
            try:
                os.remove(audio_path)
            except OSError:
                pass

        mark(0.92, "正在生成摘要与台词...", "summarize")

        # 3. 摘要 + 台词稿：两者互不依赖，并发执行以缩短总耗时
        def on_polish(i, total):
            mark(0.92 + 0.03 * (i / max(1, total)),
                 f"正在整理台词 {i + 1}/{total}...", "lines")

        with ThreadPoolExecutor(max_workers=2) as pool:
            f_sum = pool.submit(summarizer.summarize, full_text, segments)
            f_lines = pool.submit(summarizer.polish_lines, segments, on_polish)
            summary_result = f_sum.result()
            lines_text = f_lines.result()

        # 4. 落库
        database.update_task(
            task_id,
            status="done",
            duration=asr_result["duration"],
            language=asr_result["language"],
            transcript=segments,
            summary=summary_result.get("summary", ""),
            key_points=summary_result.get("key_points", []),
            chapters=summary_result.get("chapters", []),
            insight=summary_result.get("insight", ""),
            lines_text=lines_text,
            word_count=len(full_text),
        )
        mark(1.0, "处理完成", "done")
        print(f"[任务完成] {task_id}")

    except Exception as e:
        import traceback
        traceback.print_exc()
        database.update_task(task_id, status="failed", error=str(e))
        mark(1.0, f"处理失败：{e}", "failed")
    finally:
        if cleanup_input:
            downloader.cleanup(video_path)


def _run_url_pipeline(task_id: str, url: str) -> None:
    """先下载链接视频，再走统一流水线。"""
    try:
        database.update_task(task_id, status="processing")
        _set_progress(task_id, 0.01, "正在解析链接...", "parse")

        def on_download(pct: float, msg: str) -> None:
            _set_progress(task_id, 0.02 + pct * 0.28, msg, "download")

        info = downloader.download(url, settings.UPLOAD_DIR, on_download)
        database.update_task(
            task_id,
            filename=info["title"],
            duration=info["duration"],
            source_url=info["webpage_url"],
        )
        _set_progress(task_id, 0.32, f"{info['platform']}视频下载完成", "downloaded")

        _run_pipeline(
            task_id,
            info["path"],
            progress_base=0.32,
            progress_span=0.68,
            cleanup_input=True,
        )

    except Exception as e:
        import traceback
        traceback.print_exc()
        database.update_task(task_id, status="failed", error=str(e))
        _set_progress(task_id, 1.0, f"处理失败：{e}", "failed")


async def start_task(video_path: Path, filename: str) -> str:
    task_id = uuid.uuid4().hex[:12]
    database.create_task(task_id, filename)
    _set_progress(task_id, 0.0, "排队中", "pending")
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run_pipeline, task_id, video_path)
    return task_id


async def start_url_task(url: str) -> str:
    """创建链接任务：先在后台下载视频，再进入识别与摘要流水线。"""
    task_id = uuid.uuid4().hex[:12]
    database.create_task(task_id, "链接任务（解析中）", source_url=url)
    _set_progress(task_id, 0.0, "排队中", "pending")
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run_url_pipeline, task_id, url)
    return task_id


def cleanup_old_files(max_age_hours: float = 24) -> None:
    """清理超过指定时长的上传文件（可选维护）。"""
    now = time.time()
    for p in settings.UPLOAD_DIR.iterdir():
        if p.is_file() and (now - p.stat().st_mtime) > max_age_hours * 3600:
            try:
                p.unlink()
            except OSError:
                pass
