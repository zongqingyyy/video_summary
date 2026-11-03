# -*- coding: utf-8 -*-
"""
配置模块：统一管理环境变量、路径与模型参数。
"""
import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 基础路径
    BASE_DIR: Path = Path(__file__).resolve().parent
    UPLOAD_DIR: Path = BASE_DIR / "uploads"
    OUTPUT_DIR: Path = BASE_DIR / "outputs"
    DB_PATH: Path = BASE_DIR / "video_summary.db"

    # ASR（语音识别）
    WHISPER_MODEL: str = "small"           # tiny / base / small / medium / large-v3（越大越准也越慢）
    WHISPER_DEVICE: str = "auto"           # auto（优先 GPU）/ cpu / cuda
    WHISPER_COMPUTE: str = "int8"          # int8 / float16 / float32
    LANGUAGE: str = "auto"                 # auto（自动识别，推荐）/ zh / en
    VAD_FILTER: bool = True                # 启用 VAD 过滤静音

    # 摘要（LLM）
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://api.openai.com/v1"
    LLM_MODEL: str = "gpt-4o-mini"
    LLM_TIMEOUT: int = 120
    # 未配置 API Key 时使用本地抽取式摘要
    USE_LOCAL_FALLBACK: bool = True

    # 链接下载（yt-dlp）
    # 抖音/B站部分视频需要登录态，可配置 cookies.txt 的绝对路径
    COOKIES_FILE: str = ""
    # 或者直接从本机浏览器读取 cookies（需已登录抖音），取值如：
    # chrome / edge / firefox，也可指定配置目录：chrome:Default
    COOKIES_FROM_BROWSER: str = ""

    # 服务
    HOST: str = "127.0.0.1"
    PORT: int = 8000

    def ensure_dirs(self) -> None:
        self.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        self.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
