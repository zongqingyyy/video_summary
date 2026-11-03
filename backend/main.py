# -*- coding: utf-8 -*-
"""
FastAPI 服务入口：上传、进度查询、任务详情、历史、删除、导出。
"""
import os
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import settings
import database
import processor
import downloader
from asr import format_timestamp

app = FastAPI(title="视频内容总结助手", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载前端静态目录
_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if _FRONTEND_DIR.exists():
    app.mount("/app", StaticFiles(directory=str(_FRONTEND_DIR), html=True), name="frontend")


@app.get("/api/health")
def health():
    cookies = "none"
    if settings.COOKIES_FILE and Path(settings.COOKIES_FILE).exists():
        cookies = "file"
    elif settings.COOKIES_FROM_BROWSER:
        cookies = f"browser:{settings.COOKIES_FROM_BROWSER}"
    return {
        "ok": True,
        "model": settings.WHISPER_MODEL,
        "llm_configured": bool(settings.LLM_API_KEY),
        "fallback": settings.USE_LOCAL_FALLBACK,
        "cookies": cookies,
    }


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    if not file.filename:
        raise HTTPException(400, "未选择文件")
    ext = Path(file.filename).suffix.lower()
    allowed = {".mp4", ".mov", ".avi", ".mkv", ".flv", ".webm", ".mp3", ".wav", ".m4a", ".aac"}
    if ext not in allowed:
        raise HTTPException(400, f"不支持的文件格式：{ext}")

    safe_name = f"{uuid.uuid4().hex[:8]}{ext}"
    save_path = settings.UPLOAD_DIR / safe_name
    content = await file.read()
    save_path.write_bytes(content)

    task_id = await processor.start_task(save_path, file.filename)
    return {"task_id": task_id, "filename": file.filename}


class UrlRequest(BaseModel):
    url: str


@app.post("/api/upload-url")
async def upload_url(body: UrlRequest):
    """通过视频链接（抖音、哔哩哔哩等）创建总结任务。"""
    try:
        url = downloader.normalize_url(body.url)
    except ValueError as e:
        raise HTTPException(400, str(e))

    platform = downloader.platform_of(url) or "视频平台"
    task_id = await processor.start_url_task(url)
    return {"task_id": task_id, "url": url, "platform": platform}


@app.get("/api/platforms")
def platforms():
    """返回支持的平台列表，供前端提示。"""
    names = sorted(set(downloader.SUPPORTED_HOSTS.values()))
    return {"platforms": names}


@app.get("/api/progress/{task_id}")
def progress(task_id: str):
    p = processor.get_progress(task_id)
    if not p:
        task = database.get_task(task_id)
        if task:
            return {"percent": 100.0 if task["status"] == "done" else 0.0,
                    "message": task["status"], "stage": task["status"],
                    "status": task["status"]}
        raise HTTPException(404, "任务不存在")
    task = database.get_task(task_id)
    p["status"] = task["status"] if task else "unknown"
    return p


@app.get("/api/task/{task_id}")
def task_detail(task_id: str):
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@app.get("/api/tasks")
def tasks(limit: int = Query(50, ge=1, le=200)):
    return {"items": database.list_tasks(limit)}


@app.delete("/api/task/{task_id}")
def delete_task(task_id: str):
    database.delete_task(task_id)
    processor._PROGRESS.pop(task_id, None)
    return {"ok": True}


@app.get("/api/export/{task_id}")
def export_task(task_id: str, fmt: str = Query("md", pattern="^(md|txt)$")):
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    content = _render(task, fmt)
    filename = f"{Path(task['filename']).stem}_summary.{fmt}"
    out_path = settings.OUTPUT_DIR / f"{task_id}_{filename}"
    out_path.write_text(content, encoding="utf-8")
    return FileResponse(str(out_path), filename=filename,
                        media_type="text/markdown" if fmt == "md" else "text/plain")


def _render(task: dict, fmt: str) -> str:
    title = task["filename"]
    summary = task.get("summary") or "（无）"
    points = task.get("key_points") or []
    chapters = task.get("chapters") or []
    insight = task.get("insight") or ""
    source_url = task.get("source_url") or ""

    if fmt == "txt":
        lines = [f"视频：{title}"]
        if source_url:
            lines.append(f"来源链接：{source_url}")
        lines += ["", "【总体摘要】", summary, "", "【关键要点】"]
        lines += [f"- {p}" for p in points]
        lines += ["", "【章节】"]
        for c in chapters:
            if isinstance(c, dict):
                lines.append(f"[{format_timestamp(c.get('start', 0))}] {c.get('title', '')}")
                lines.append(f"  {c.get('description', '')}")
        if insight:
            lines += ["", "【深度总结】", insight]
        if task.get("lines_text"):
            lines += ["", "【全部台词】", task["lines_text"]]
        return "\n".join(lines)

    # Markdown
    md = [f"# {title}", "", f"**时长**：{format_timestamp(task.get('duration', 0))}  ",
          f"**字数**：{task.get('word_count', 0)}  ",
          f"**语言**：{task.get('language', '-')}"]
    if source_url:
        md.append(f"**来源**：{source_url}")
    md += ["", "## 总体摘要", "", summary, "", "## 关键要点", ""]
    md += [f"- {p}" for p in points]
    md += ["", "## 章节", ""]
    for c in chapters:
        if isinstance(c, dict):
            md.append(f"### [{format_timestamp(c.get('start', 0))}] {c.get('title', '')}")
            md.append(c.get("description", ""))
            md.append("")
    if insight:
        md += ["", "## 深度总结", "", insight]
    if task.get("lines_text"):
        md += ["", "## 全部台词", "", task["lines_text"], ""]
    return "\n".join(md)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.HOST, port=settings.PORT)
