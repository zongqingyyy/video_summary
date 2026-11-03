# 视频内容总结助手

一个基于 **语音识别（ASR）+ 大语言模型（LLM）** 的视频内容自动总结工具。上传视频/音频，自动生成**总体摘要、关键要点、章节切分、深度总结**，并支持 Markdown/TXT 导出。

## ✨ 功能特性

- 🎬 支持视频（mp4/mov/avi/mkv/webm）和音频（mp3/wav/m4a/aac）上传
- 🔗 支持粘贴视频链接直接总结：抖音、哔哩哔哩、YouTube、优酷、腾讯视频、西瓜视频、微博（含短链接与分享文案）
- 🎙️ 基于 faster-whisper 的本地语音识别，带时间戳分段
- 📝 结构化摘要：总体摘要 / 关键要点 / 章节切分 / 深度总结
- 🔄 双模式摘要：配置 LLM API Key 时使用大模型生成；未配置时自动降级为本地抽取式摘要
- 📜 历史记录持久化（SQLite）
- 💾 一键导出 Markdown / TXT
- 🖥️ 简洁美观的 Web 界面，拖拽上传 + 实时进度

## 🚀 快速开始

### 环境要求
- Python 3.9+
- （推荐）ffmpeg：用于从视频中抽取音频。安装方法见下文。

### 一键启动

```bash
python start.py
```

首次运行会自动安装依赖，随后启动服务并自动打开浏览器：http://127.0.0.1:8000/app

### 手动启动

```bash
cd backend
pip install -r requirements.txt
python main.py
```

## 🔗 链接总结（抖音 / 哔哩哔哩）

在首页「粘贴视频链接」输入框中直接粘贴视频地址即可，支持：

- 完整链接：`https://www.bilibili.com/video/BV1xx411c7mD`
- 短链接：`https://b23.tv/xxxxxx`、`https://v.douyin.com/xxxx/`
- 分享文案：直接整段粘贴（系统会自动提取其中的链接）

系统会通过 yt-dlp 下载视频（优先只下载音频流），随后自动进入语音识别与摘要流程，结果页会保留来源链接。

### 抖音：自动调用本机浏览器解析

抖音网页版的详情接口带有 JS 签名风控，普通 HTTP 请求会被 403 拒绝，yt-dlp 也因此取不到数据。
系统会自动启动本机 Edge/Chrome 的**无头实例**，让页面自己完成签名请求，再读取接口返回，
因此抖音链接**无需登录、无需安装任何插件、无需配置 cookies**。

要求：
- 本机已安装 Edge 或 Chrome（程序会自动查找常见安装路径）；
- 解析期间会短暂启动一个不可见的浏览器进程，属于正常现象；
- 依赖 `websockets`，已包含在 `requirements.txt` 中。

抖音默认只下载**纯音频音轨**（体积约为视频的 1/8），既省流量，也可以直接送入语音识别。

### 其他平台需要登录态时

B 站等平台的部分视频（如会员、充电专属内容）需要登录态，可在 `backend/.env` 中配置 cookies：

```env
# 直接读取本机浏览器（需先完全退出浏览器）
COOKIES_FROM_BROWSER=edge

# 或使用导出的 Netscape 格式 cookies 文件（优先级更高）
COOKIES_FILE=d:\path\to\cookies.txt
```

配置后重启服务即可，`GET /api/health` 的 `cookies` 字段可确认是否生效。

## 🔧 配置 LLM（可选）

未配置时使用本地抽取式摘要。如需更高质量的生成式摘要（英文视频输出中文），在 `backend/.env` 中配置。默认已对接智谱 GLM-4-Flash：

```env
LLM_API_KEY=你的密钥            # https://open.bigmodel.cn/usercenter/apikeys
LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4
LLM_MODEL=glm-4-flash
# 识别模型：tiny < base < small < medium < large-v3（越大越准也越慢）
WHISPER_MODEL=small
# 识别语言：auto 自动判断（推荐）；也可强制 zh / en
LANGUAGE=auto
```

也可换用任意 OpenAI 兼容接口（OpenAI、DeepSeek、通义千问、Ollama 等），只需改 `LLM_BASE_URL` 与 `LLM_MODEL`。

> 语言识别为 `auto` 时，系统会先做一次轻量语言探测，再按实际语言转写：中文会附加"简体中文 + 自动标点"的提示词，英文视频则保持英文原文，不会出现"英文被硬解成中文乱码"。若希望英文视频的摘要也输出中文，配置 `LLM_API_KEY` 即可（提示词已要求所有输出统一使用简体中文）。

## 📦 ffmpeg 安装

- **Windows**：从 https://ffmpeg.org/download.html 下载，解压后将 `bin` 目录加入系统 PATH
- **macOS**：`brew install ffmpeg`
- **Ubuntu**：`sudo apt install ffmpeg`

> 注意：mp3/wav 等纯音频文件无需 ffmpeg 也可直接识别。

## 📁 项目结构

```
video_summary/
├── backend/
│   ├── main.py          # FastAPI 服务入口
│   ├── asr.py           # 语音识别（faster-whisper）
│   ├── downloader.py    # 链接解析与下载（yt-dlp）
│   ├── douyin.py        # 抖音解析（无头浏览器 + DevTools Protocol）
│   ├── summarizer.py    # 摘要生成（LLM + 本地降级）
│   ├── processor.py     # 处理流水线
│   ├── database.py      # SQLite 持久化
│   ├── config.py        # 配置管理
│   └── requirements.txt
├── frontend/
│   └── index.html       # 单页前端（Vue 3 CDN）
└── start.py             # 一键启动脚本
```

## 🛠️ 技术栈

| 模块 | 技术 |
|------|------|
| 后端框架 | FastAPI + Uvicorn |
| 语音识别 | faster-whisper (CTranslate2) |
| 链接解析 | yt-dlp |
| 音频处理 | ffmpeg |
| 摘要生成 | OpenAI 兼容 LLM / TF-IDF 抽取式 |
| 前端 | Vue 3 (CDN) + 原生 CSS |
| 存储 | SQLite |
