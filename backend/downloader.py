# -*- coding: utf-8 -*-
"""
链接解析与下载模块：
支持抖音、哔哩哔哩等平台的视频链接，使用 yt-dlp 下载音频/视频，
下载后交由统一的语音识别与摘要流水线处理。
"""
from __future__ import annotations
import os
import re
import time
from pathlib import Path
from typing import Callable, Dict, Any, Optional
from urllib.parse import urlparse

import httpx

import douyin
from config import settings


# 支持的平台：域名 -> 平台名称
SUPPORTED_HOSTS = {
    "douyin.com": "抖音",
    "iesdouyin.com": "抖音",
    "bilibili.com": "哔哩哔哩",
    "b23.tv": "哔哩哔哩",
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "youku.com": "优酷",
    "v.qq.com": "腾讯视频",
    "ixigua.com": "西瓜视频",
    "weibo.com": "微博",
}

# 各平台的短链接域名，需要先跟随重定向拿到真实地址
SHORT_HOSTS = {"b23.tv", "v.douyin.com", "youtu.be"}


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def platform_of(url: str) -> Optional[str]:
    """根据域名判断所属平台，未收录返回 None。"""
    host = _host_of(url)
    for domain, name in SUPPORTED_HOSTS.items():
        if host == domain or host.endswith("." + domain):
            return name
    return None


def normalize_url(raw: str) -> str:
    """
    校验并规范化用户输入的链接：
    - 去掉首尾空白；
    - 自动补全 https://（用户常直接粘贴 www.bilibili.com/video/xxx）；
    - 只允许 http/https；
    - 校验域名是否在支持列表中。
    """
    url = (raw or "").strip()
    # 从分享文案中提取链接，如“【标题】 https://v.douyin.com/xxx/ 复制此链接”
    m = re.search(r"https?://[^\s\u4e00-\u9fff]+", url)
    if m:
        url = m.group(0)
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url.lstrip("/")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("链接格式不正确，请粘贴完整的视频地址")
    if not platform_of(url):
        raise ValueError(
            "暂不支持该链接。当前支持：抖音、哔哩哔哩、YouTube、优酷、腾讯视频、西瓜视频、微博"
        )
    return url


def resolve_short_url(url: str, timeout: float = 8.0) -> str:
    """解析 b23.tv / v.douyin.com 等短链接，失败时返回原链接。"""
    if _host_of(url) not in SHORT_HOSTS:
        return url
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True, headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            )
        }) as client:
            resp = client.get(url)
            final = str(resp.url)
            if final.startswith("http"):
                return final
    except Exception as e:
        print(f"[短链接解析失败] {url} -> {e}")
    return url


def _yt_dlp():
    try:
        import yt_dlp
    except ImportError as e:
        raise RuntimeError(
            "未安装 yt-dlp，请先执行 pip install yt-dlp"
        ) from e
    return yt_dlp


def _parse_cookie_browser(value: str) -> tuple:
    """
    解析浏览器 cookies 配置，如 "chrome" 或 "chrome:Profile 1"。
    yt-dlp 要求的格式为 (browser, profile, keyring, container)。
    """
    parts = [p.strip() for p in value.split(":", 1)]
    browser = parts[0].lower()
    profile = parts[1] if len(parts) > 1 and parts[1] else None
    return (browser, profile, None, None)


def download(
    url: str,
    dest_dir: Path,
    progress_cb: Optional[Callable[[float, str], None]] = None,
) -> Dict[str, Any]:
    """
    使用 yt-dlp 下载视频（优先只下载音频流以节省带宽，无 ffmpeg 也可识别）。
    返回 {path, title, duration, webpage_url, platform}
    """
    url = normalize_url(url)
    url = resolve_short_url(url)
    platform = platform_of(url) or "视频平台"

    # 抖音走浏览器解析：其详情接口带 JS 签名风控，yt-dlp 直连会被 403
    if platform == "抖音":
        aweme_id = douyin.extract_aweme_id(url)
        if aweme_id:
            return douyin.download(url, aweme_id, dest_dir, progress_cb)

    yt_dlp = _yt_dlp()

    base_opts: Dict[str, Any] = {
        # 优先纯音频流（m4a/webm），避免依赖 ffmpeg 做音视频合并
        "format": "bestaudio[ext=m4a]/bestaudio/best",
        "outtmpl": str(dest_dir / "%(title).80s-%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "socket_timeout": 30,
        "overwrites": True,
        # 部分平台需要额外的 Referer 才能通过防盗链校验
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
            "Referer": f"https://{_host_of(url)}/",
        },
    }

    # 可选：cookies 配置（优先使用显式 cookies 文件，其次直接读取本机浏览器）
    if settings.COOKIES_FILE and Path(settings.COOKIES_FILE).exists():
        base_opts["cookiefile"] = settings.COOKIES_FILE
    elif settings.COOKIES_FROM_BROWSER:
        base_opts["cookiesfrombrowser"] = _parse_cookie_browser(settings.COOKIES_FROM_BROWSER)

    def hook(d):
        if not progress_cb:
            return
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            pct = (done / total) if total else 0.0
            speed = d.get("speed") or 0
            speed_mb = speed / 1024 / 1024 if speed else 0
            progress_cb(pct, f"正在下载（{pct*100:.0f}%{f'，{speed_mb:.1f}MB/s' if speed_mb else ''}）")
        elif status == "finished":
            progress_cb(1.0, "下载完成，准备识别")

    base_opts["progress_hooks"] = [hook]

    info: Dict[str, Any] = {}
    started_at = time.time()
    try:
        with yt_dlp.YoutubeDL(base_opts) as ydl:
            info = ydl.extract_info(url, download=True) or {}
    except Exception as e:
        msg = str(e)
        low = msg.lower()
        if "cookie" in low or "login" in low or "decrypt" in low:
            raise RuntimeError(
                f"{platform}需要登录态或触发了风控校验。请在 backend/.env 中配置 "
                f"COOKIES_FROM_BROWSER（如 firefox / chrome / edge）或 COOKIES_FILE（cookies.txt 路径），"
                f"并确保浏览器已登录抖音后重试。原始错误：{msg}"
            ) from e
        raise RuntimeError(f"链接解析失败：{msg}") from e

    # 定位实际下载的文件
    file_path = None
    if info.get("requested_downloads"):
        fp = info["requested_downloads"][0].get("filepath")
        if fp and Path(fp).exists():
            file_path = Path(fp)
    if file_path is None:
        # 兜底：只挑选本次下载开始之后生成的媒体文件，避免误取其他任务的文件
        candidates = sorted(
            (p for p in dest_dir.iterdir()
             if p.is_file()
             and p.stat().st_mtime >= started_at - 1
             and p.suffix.lower() in {".m4a", ".mp4", ".webm", ".mp3", ".wav", ".mkv", ".flv"}),
            key=lambda p: p.stat().st_mtime,
        )
        file_path = candidates[-1] if candidates else None
    if file_path is None or not file_path.exists():
        raise RuntimeError("下载完成但未找到输出文件，请稍后重试")

    title = info.get("title") or file_path.stem
    ext = file_path.suffix
    if not title.endswith(ext):
        title = f"{title}{ext}"

    return {
        "path": file_path,
        "title": title,
        "duration": float(info.get("duration") or 0),
        "webpage_url": info.get("webpage_url") or url,
        "platform": platform,
    }


def cleanup(path: Path) -> None:
    """删除下载产生的临时文件。"""
    try:
        if path.exists():
            os.remove(path)
    except OSError:
        pass
