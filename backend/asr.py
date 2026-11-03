# -*- coding: utf-8 -*-
"""
语音识别模块：基于 faster-whisper 进行音频转写，输出带时间戳的分段。
若未安装 faster-whisper，则抛出明确错误供上层降级。
"""
from __future__ import annotations
import os
from typing import List, Dict, Any, Optional
from pathlib import Path

from config import settings

# 兼容补丁：faster-whisper 1.x 调用 av.open 时传入了 metadata_errors 参数，
# 但新版 PyAV(>=14) 已移除该参数，这里做一层包装以兼容。
try:
    import av as _av

    _orig_av_open = _av.open

    def _patched_av_open(*args, **kwargs):
        kwargs.pop("metadata_errors", None)
        return _orig_av_open(*args, **kwargs)

    _av.open = _patched_av_open
except Exception:
    pass

_model = None

# 中文提示词：引导 Whisper 输出简体中文并自动加标点，
# 可显著减少"繁体字 / 无标点 / 语义不通"的乱码式输出
_ZH_PROMPT = "以下是普通话的句子，请使用简体中文转写，并加上标点。"


def _probe_language(model, audio_path: str) -> Optional[str]:
    """
    先做一次轻量语言探测：只取第一段就停止，代价很小。
    避免"Forced 中文"把英语视频硬解成中英混杂的乱码。
    """
    try:
        seg_iter, info = model.transcribe(
            audio_path,
            language=None,
            vad_filter=settings.VAD_FILTER,
            condition_on_previous_text=False,
        )
        for _ in seg_iter:
            break
        return getattr(info, "language", None)
    except Exception as e:
        print(f"[语言探测失败] {e}")
        return None


def _setup_nvidia_dlls() -> None:
    """把 pip 安装的 nvidia CUDA 运行库（cublas/cudnn）目录加入 DLL 搜索路径。"""
    import site
    bases = []
    try:
        bases += [Path(p) for p in site.getsitepackages()]
    except Exception:
        pass
    try:
        # pip --user 安装的包会落在用户目录，需单独取
        bases.append(Path(site.getusersitepackages()))
    except Exception:
        pass
    for base in bases:
        for sub in ("nvidia/cublas/bin", "nvidia/cudnn/bin", "nvidia/cuda_nvrtc/bin"):
            d = base / sub
            if d.exists():
                try:
                    os.add_dll_directory(str(d))
                except (OSError, AttributeError):
                    pass


def _cuda_ready() -> bool:
    """检查 GPU 是否可用：CTranslate2 能识别显卡，且 cuBLAS 运行库可以加载。"""
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() < 1:
            return False
    except Exception:
        return False
    _setup_nvidia_dlls()
    if os.name == "nt":
        import ctypes
        for dll in ("cublas64_12.dll", "cublasLt64_12.dll"):
            try:
                ctypes.WinDLL(dll)
            except OSError:
                return False
    return True


def _get_model():
    global _model
    if _model is not None:
        return _model
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError(
            "未安装 faster-whisper，请先执行 pip install faster-whisper"
        ) from e

    device = settings.WHISPER_DEVICE
    compute_type = settings.WHISPER_COMPUTE
    # auto：优先 GPU（RTX 等独立显卡可快数倍），不可用时回退 CPU
    if device == "auto":
        device = "cuda" if _cuda_ready() else "cpu"
    if device == "cuda" and compute_type in ("int8", "auto", ""):
        compute_type = "float16"

    if device == "cuda":
        try:
            candidate = WhisperModel(settings.WHISPER_MODEL, device="cuda", compute_type=compute_type)
            # 用一小段静音做冒烟测试：cuDNN 等运行库缺失时会在推理阶段才报错
            import numpy as np
            list(candidate.transcribe(np.zeros(16000, dtype=np.float32),
                                      language="zh", vad_filter=False, beam_size=1)[0])
            _model = candidate
            print(f"[ASR] 使用 GPU（{compute_type}）")
            return _model
        except Exception as e:
            print(f"[ASR] GPU 初始化失败，回退 CPU：{e}")
            device, compute_type = "cpu", "int8"

    _model = WhisperModel(settings.WHISPER_MODEL, device=device, compute_type=compute_type)
    print(f"[ASR] 使用 {device}（{compute_type}）")
    return _model


def transcribe(
    audio_path: str | Path,
    language: Optional[str] = None,
    progress_cb=None,
) -> Dict[str, Any]:
    """
    转写音频文件。
    progress_cb(percent: float, message: str) 可选回调。
    返回 {segments: [...], language, duration, text}
    """
    audio_path = str(audio_path)
    model = _get_model()
    lang = (language or settings.LANGUAGE or "auto").strip().lower()

    if progress_cb:
        progress_cb(0.02, "正在加载音频并检测语言...")

    if lang == "auto":
        lang = _probe_language(model, audio_path)
        if progress_cb:
            progress_cb(0.04, f"检测到语言：{lang or '自动'}")

    # 中文场景附加提示词，统一简体并补标点
    prompt = _ZH_PROMPT if lang == "zh" else None

    segments_iter, info = model.transcribe(
        audio_path,
        language=lang,
        initial_prompt=prompt,
        vad_filter=settings.VAD_FILTER,
        # 关闭上文条件依赖，可显著减少长音频中"复读 / 幻觉出无意义中文"的问题
        condition_on_previous_text=False,
        beam_size=5,
    )

    segments: List[Dict[str, Any]] = []
    full_text_parts: List[str] = []
    duration = float(getattr(info, "duration", 0) or 0)

    for seg in segments_iter:
        seg_dict = {
            "start": round(float(seg.start), 2),
            "end": round(float(seg.end), 2),
            "text": seg.text.strip(),
        }
        if seg_dict["text"]:
            segments.append(seg_dict)
            full_text_parts.append(seg_dict["text"])
            if progress_cb and duration > 0:
                pct = min(0.95, 0.05 + (seg.end / duration) * 0.9)
                progress_cb(pct, f"识别中 {int(seg.end // 60)}:{int(seg.end % 60):02d}...")

    if progress_cb:
        progress_cb(1.0, "转写完成")

    return {
        "segments": segments,
        "language": getattr(info, "language", lang or "zh"),
        "duration": duration,
        "text": " ".join(full_text_parts),
    }


def format_timestamp(seconds: float) -> str:
    seconds = int(seconds)
    h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"
