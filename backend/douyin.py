# -*- coding: utf-8 -*-
"""
抖音专用解析模块。

抖音网页版的详情接口（/aweme/v1/web/aweme/detail/）带有 Argus 风控，
请求必须携带浏览器端 JS 生成的签名，直接 HTTP 请求会被 403
（"Blocked by ArgusSecurityPlugin"），yt-dlp 也因此取不到数据。

这里换一种思路：启动本机 Edge/Chrome 的无头实例，通过 DevTools
Protocol 打开视频页，页面自身会带着正确签名去请求详情接口，
我们直接读取该接口的响应体即可拿到标题、时长与音视频地址。
这种方式不需要登录，也不需要任何浏览器插件或 cookies。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import httpx

# 与浏览器保持一致，下载媒体流时同样需要
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "google-chrome",
    "chromium",
]

# 支持 /video/、/note/、/slides/ 以及分享页 /share/video/
_AWEME_ID_RE = re.compile(r"/(?:share/)?(?:video|note|slides)/(\d+)")


def extract_aweme_id(url: str) -> Optional[str]:
    """从抖音链接中提取作品 ID。"""
    m = _AWEME_ID_RE.search(url)
    return m.group(1) if m else None


def _browser_path() -> Optional[str]:
    """定位本机可用的 Chromium 内核浏览器。"""
    for candidate in _BROWSER_CANDIDATES:
        if os.path.isabs(candidate):
            if Path(candidate).exists():
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_devtools(port: int, timeout: float = 20.0) -> Optional[str]:
    """等待浏览器调试端口就绪，返回 browser 级 WebSocket 地址。"""
    deadline = time.time() + timeout
    async with httpx.AsyncClient(timeout=2) as client:
        while time.time() < deadline:
            try:
                resp = await client.get(f"http://127.0.0.1:{port}/json/version")
                if resp.status_code == 200:
                    ws = resp.json().get("webSocketDebuggerUrl")
                    if ws:
                        return ws
            except Exception:
                pass
            await asyncio.sleep(0.4)
    return None


async def _capture_detail(aweme_id: str, timeout: float = 30.0) -> tuple[Dict[str, Any], Optional[str]]:
    """
    无头浏览器打开视频页，捕获详情接口的响应 JSON。
    同时记录页面请求的纯音频流地址（DASH 音轨，体积只有视频的 1/8）。
    返回 (作品数据, 音频流地址或 None)。
    """
    try:
        import websockets
    except ImportError as e:
        raise RuntimeError("缺少 websockets 依赖，请执行 pip install websockets") from e

    browser = _browser_path()
    if not browser:
        raise RuntimeError("未找到 Edge/Chrome 浏览器，抖音链接需要借助浏览器解析")

    port = _free_port()
    profile = Path(tempfile.gettempdir()) / "video_summary_browser_profile"
    profile.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen(
        [
            browser,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--headless=new",
            "--no-first-run",
            "--no-default-browser-check",
            "--mute-audio",
            "--disable-gpu",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    detail: Optional[str] = None
    audio_url: Optional[str] = None
    try:
        ws_url = await _wait_devtools(port)
        if not ws_url:
            raise RuntimeError("浏览器启动超时，请确认 Edge 未被其他程序占用")

        async with websockets.connect(ws_url, max_size=40 * 1024 * 1024) as ws:
            seq = 0

            async def send(method: str, params: Optional[dict] = None, session: Optional[str] = None):
                nonlocal seq
                seq += 1
                payload = {"id": seq, "method": method, "params": params or {}}
                if session:
                    payload["sessionId"] = session
                await ws.send(json.dumps(payload))
                while True:
                    data = json.loads(await ws.recv())
                    if data.get("id") == seq:
                        return data

            target = (await send("Target.createTarget", {"url": "about:blank"}))["result"]["targetId"]
            session = (await send("Target.attachToTarget",
                                  {"targetId": target, "flatten": True}))["result"]["sessionId"]
            await send("Network.enable", {}, session)
            await send("Page.enable", {}, session)
            await send("Page.navigate", {"url": f"https://www.douyin.com/video/{aweme_id}"}, session)

            deadline = time.time() + timeout
            detail_at: Optional[float] = None
            while time.time() < deadline:
                if detail and (audio_url or time.time() - (detail_at or time.time()) > 8):
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1.5)
                except asyncio.TimeoutError:
                    continue
                event = json.loads(raw)
                method = event.get("method")
                if method == "Network.requestWillBeSent":
                    req_url = event["params"]["request"]["url"]
                    if audio_url is None and "media-audio" in req_url:
                        audio_url = req_url
                elif method == "Network.responseReceived" and detail is None:
                    if "/aweme/detail/" in event["params"]["response"]["url"]:
                        body = await send("Network.getResponseBody",
                                          {"requestId": event["params"]["requestId"]}, session)
                        detail = (body.get("result") or {}).get("body")
                        detail_at = time.time()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    if not detail:
        raise RuntimeError("未能获取抖音视频详情，视频可能已删除或需要登录")

    data = json.loads(detail)
    aweme = data.get("aweme_detail")
    if not aweme:
        raise RuntimeError("抖音返回的数据为空，视频可能已删除或仅限作者可见")
    return aweme, audio_url


def _pick_media(aweme: Dict[str, Any], audio_url: Optional[str]) -> tuple[str, str]:
    """
    选择下载地址：优先页面实际请求的纯音频音轨（体积约为视频的 1/8，
    且无需 ffmpeg 抽取，直接可送入语音识别）；没有则退回码率最低的视频流。
    返回 (地址, 扩展名)。
    """
    if audio_url:
        return audio_url, ".m4a"

    video = aweme.get("video") or {}
    for entry in video.get("bit_rate_audio") or []:
        urls = ((entry.get("play_addr") or {}).get("url_list")) or []
        if urls:
            return urls[0], ".m4a"

    mp4s = [e for e in (video.get("bit_rate") or []) if e.get("format") == "mp4"]
    mp4s.sort(key=lambda e: e.get("bit_rate") or 0)
    for entry in mp4s:
        urls = ((entry.get("play_addr") or {}).get("url_list")) or []
        if urls:
            return urls[0], ".mp4"

    for key in ("play_addr", "play_addr_h264", "download_addr"):
        urls = ((video.get(key) or {}).get("url_list")) or []
        if urls:
            return urls[0], ".mp4"

    raise RuntimeError("抖音未返回可用的视频地址")


def _safe_name(text: str, fallback: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", text or "").strip()
    return (name or fallback)[:60]


def resolve(aweme_id: str) -> Dict[str, Any]:
    """获取作品信息与媒体地址。"""
    aweme, audio_url = asyncio.run(_capture_detail(aweme_id))
    media_url, ext = _pick_media(aweme, audio_url)
    duration = float(aweme.get("duration") or aweme.get("video", {}).get("duration") or 0) / 1000.0
    return {
        "id": aweme_id,
        "title": aweme.get("desc") or f"抖音视频{aweme_id}",
        "duration": duration,
        "media_url": media_url,
        "ext": ext,
    }


def download(
    url: str,
    aweme_id: str,
    dest_dir: Path,
    progress_cb: Optional[Callable[[float, str], None]] = None,
) -> Dict[str, Any]:
    """
    解析并下载抖音作品，返回与 downloader.download 一致的结构。
    默认只下载音频轨道，显著减少体积与耗时。
    """
    def report(pct: float, msg: str) -> None:
        if progress_cb:
            progress_cb(pct, msg)

    report(0.0, "正在启动浏览器解析抖音视频")
    info = resolve(aweme_id)
    report(0.05, f"已获取视频信息（时长 {info['duration']:.0f} 秒），开始下载")

    target = dest_dir / f"{_safe_name(info['title'], aweme_id)}-{aweme_id}{info['ext']}"
    headers = {"User-Agent": UA, "Referer": "https://www.douyin.com/"}

    with httpx.Client(timeout=60, follow_redirects=True, headers=headers) as client:
        with client.stream("GET", info["media_url"]) as resp:
            if resp.status_code not in (200, 206):
                raise RuntimeError(f"媒体下载失败（HTTP {resp.status_code}）")
            total = int(resp.headers.get("content-length") or 0)
            done = 0
            tmp = target.with_suffix(target.suffix + ".part")
            with open(tmp, "wb") as f:
                for chunk in resp.iter_bytes(1024 * 256):
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        report(0.05 + 0.9 * done / total,
                               f"正在下载（{done / 1024 / 1024:.1f}MB / {total / 1024 / 1024:.1f}MB）")
            tmp.replace(target)

    report(1.0, "下载完成，准备识别")
    return {
        "path": target,
        "title": f"{info['title']}{target.suffix}",
        "duration": info["duration"],
        "webpage_url": f"https://www.douyin.com/video/{aweme_id}",
        "platform": "抖音",
    }
