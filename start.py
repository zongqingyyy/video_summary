# -*- coding: utf-8 -*-
"""
一键启动脚本：
1. 检查并安装依赖（首次运行）
2. 启动 FastAPI 服务
3. 自动打开浏览器
"""
import os
import sys
import socket
import subprocess
import traceback
import webbrowser
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
REQ = BACKEND / "requirements.txt"
LOG = ROOT / "startup.log"


def check_python():
    if sys.version_info < (3, 9):
        print("错误：需要 Python 3.9 或更高版本")
        sys.exit(1)


def install_deps():
    print("正在检查依赖...")
    try:
        import fastapi, faster_whisper  # noqa
        print("依赖已安装")
        return
    except ImportError:
        pass
    print("首次运行，正在安装依赖（可能需要几分钟）...")
    base = [sys.executable, "-m", "pip", "install", "-r", str(REQ)]
    # 默认源可能因网络或镜像失效而失败（例如返回 403），失败后自动改用国内镜像重试
    for index_url in (None, "https://mirrors.aliyun.com/pypi/simple"):
        cmd = list(base)
        if index_url:
            cmd += ["-i", index_url, "--trusted-host", "mirrors.aliyun.com"]
            print(f"默认源安装失败，改用镜像源重试：{index_url}")
        if subprocess.run(cmd).returncode == 0:
            print("依赖安装完成")
            return
    print("依赖安装失败，请手动执行：pip install -r", REQ)
    print("若提示找不到包，可加镜像参数：pip install -r", REQ,
          "-i https://mirrors.aliyun.com/pypi/simple")
    sys.exit(1)


def check_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        print("[OK] 已检测到 ffmpeg")
    except (FileNotFoundError, subprocess.CalledProcessError):
        print("[警告] 未检测到 ffmpeg，部分视频格式可能无法处理。")
        print("       建议安装 ffmpeg：https://ffmpeg.org/download.html")
        print("       （mp3/wav 等纯音频文件无需 ffmpeg 也可识别）")


def find_free_port(start: int = 8000, tries: int = 5) -> int:
    """从 start 开始找一个没被占用的端口，避免 Errno 10048。"""
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


def wait_for_server(port: int, timeout: float = 60) -> bool:
    """
    轮询端口直到服务真正开始监听。
    首次运行时导入 fastapi / faster-whisper / jieba / yt-dlp 需要数秒，
    若按固定延迟打开浏览器，页面会在服务就绪前访问而被拒绝。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.3)
    return False


def main():
    check_python()
    install_deps()
    check_ffmpeg()

    os.chdir(str(BACKEND))
    sys.path.insert(0, str(BACKEND))

    port = find_free_port(8000)
    if port != 8000:
        print(f"[提示] 端口 8000 已被其它程序占用，本次改用 {port}")

    # 带末尾斜杠，避免 /app -> /app/ 的 307 跳转
    url = f"http://127.0.0.1:{port}/app/"
    print(f"\n服务即将启动，浏览器将自动打开：{url}")
    print("按 Ctrl+C 停止服务\n")

    # 等服务真正监听后再打开浏览器，而不是固定延迟
    def open_browser():
        if wait_for_server(port):
            print(f"[就绪] 服务已在运行：{url}")
            print("       终端停在这里是正常的（正在等待请求），不要重复点击“运行”。\n")
            webbrowser.open(url)
        else:
            print(f"[提示] 等待服务启动超时，请手动在浏览器打开：{url}")

    import threading
    threading.Thread(target=open_browser, daemon=True).start()

    try:
        import uvicorn
        from main import app
        uvicorn.run(app, host="127.0.0.1", port=port, reload=False)
        print("\n服务已停止（关闭窗口或按 Ctrl+C 结束）")
    except KeyboardInterrupt:
        print("\n服务已停止")


def _dump_error():
    """把异常写到 startup.log，并尽量让窗口停住，方便看清报错。"""
    text = traceback.format_exc()
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S]\n") + text + "\n")
        print(f"\n完整报错已写入：{LOG}")
    except OSError:
        pass
    if sys.stdin and sys.stdin.isatty():
        try:
            input("\n按回车键退出...")
        except (EOFError, KeyboardInterrupt):
            pass


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\n服务已停止")
    except Exception:
        traceback.print_exc()
        _dump_error()
