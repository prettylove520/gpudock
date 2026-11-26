#!/usr/bin/env python3
"""GPUDock runtime agent.

Uploaded to ``/content/gpudock/gpudock_agent.py`` on a Colab runtime and used in
two ways:

* imported in GPUDock's worker kernel, where ``run_cli(cmd, args)`` starts
  ``python -u gpudock_agent.py <cmd> <args.json>`` and relays its output. Every
  heavy step runs in its own process, so GPU memory is released when it ends and
  CUDA library paths can be set before anything loads;
* as that subprocess, where ``main()`` dispatches to the ``cmd_*`` functions.

Progress travels as stdout lines ``__GPUDOCK_EVT__{json}``. Only the standard
library is imported at module level: the local GPUDock server imports this file
too, to read RECIPES and reuse the translation prompt helpers.

Set ``GPUDOCK_AGENT_FAKE=1`` to replace models with tiny stand-ins (tests).
"""

from __future__ import annotations

import glob
import json
import math
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import time
import urllib.request
import wave
from pathlib import Path

VERSION = "2026.11.25-1"
ROOT = Path(os.environ.get("GPUDOCK_ROOT", "/content/gpudock"))
EVT = "__GPUDOCK_EVT__"
FAKE = os.environ.get("GPUDOCK_AGENT_FAKE") == "1"
OLLAMA = "http://127.0.0.1:11434"

# ---------------------------------------------------------------------------
# catalogue (also read by the local server for the model library page)
# ---------------------------------------------------------------------------
# The model catalogue lives in models.json next to this file (uploaded with it):
# deployable recipes, the image/video models and the ComfyUI template each mode
# uses, where each user parameter goes in a template once converted to API
# format (node ids are API ids; subgraph nodes are "<instance>:<inner>"), and
# the LLMs offered for translation. Add or change a model there, not here.
_CATALOG = json.loads(Path(__file__).with_name("models.json").read_text(encoding="utf-8"))
RECIPES: dict[str, dict] = _CATALOG["recipes"]
GEN_MODELS: dict[str, dict] = _CATALOG["gen_models"]
MODE_LABELS: dict[str, str] = _CATALOG["mode_labels"]
LLM_MODELS: list[dict] = _CATALOG["llm_models"]
TEMPLATE_PARAMS: dict[str, dict] = _CATALOG["template_params"]

LANGS: dict[str, dict] = {
    "zh": {"name": "中文", "kokoro": "z", "rate": 4.5, "unit": "个字"},
    "en": {"name": "英语", "kokoro": "a", "rate": 14.0, "unit": "个字母"},
    "ja": {"name": "日语", "kokoro": "j", "rate": 7.0, "unit": "个字"},
    "ko": {"name": "韩语", "kokoro": None, "rate": 6.0, "unit": "个字"},
    "es": {"name": "西班牙语", "kokoro": "e", "rate": 14.0, "unit": "个字母"},
    "fr": {"name": "法语", "kokoro": "f", "rate": 14.0, "unit": "个字母"},
    "de": {"name": "德语", "kokoro": None, "rate": 13.0, "unit": "个字母"},
    "it": {"name": "意大利语", "kokoro": "i", "rate": 14.0, "unit": "个字母"},
    "pt": {"name": "葡萄牙语", "kokoro": "p", "rate": 14.0, "unit": "个字母"},
    "ru": {"name": "俄语", "kokoro": None, "rate": 12.0, "unit": "个字母"},
    "hi": {"name": "印地语", "kokoro": "h", "rate": 12.0, "unit": "个字母"},
}

KOKORO_VOICES: dict[str, list[dict]] = {
    "z": [
        {"id": "zf_xiaoxiao", "label": "晓晓（女）"},
        {"id": "zf_xiaobei", "label": "晓北（女）"},
        {"id": "zf_xiaoni", "label": "晓妮（女）"},
        {"id": "zf_xiaoyi", "label": "晓伊（女）"},
        {"id": "zm_yunjian", "label": "云健（男）"},
        {"id": "zm_yunxi", "label": "云希（男）"},
        {"id": "zm_yunxia", "label": "云夏（男）"},
        {"id": "zm_yunyang", "label": "云扬（男）"},
    ],
    "a": [
        {"id": "af_heart", "label": "Heart (F)"},
        {"id": "af_bella", "label": "Bella (F)"},
        {"id": "af_nicole", "label": "Nicole (F)"},
        {"id": "af_sarah", "label": "Sarah (F)"},
        {"id": "am_michael", "label": "Michael (M)"},
        {"id": "am_adam", "label": "Adam (M)"},
        {"id": "am_eric", "label": "Eric (M)"},
        {"id": "am_onyx", "label": "Onyx (M)"},
    ],
    "j": [{"id": "jf_alpha", "label": "Alpha (F)"}, {"id": "jm_kumo", "label": "Kumo (M)"}],
    "e": [{"id": "ef_dora", "label": "Dora (F)"}, {"id": "em_alex", "label": "Alex (M)"}],
    "f": [{"id": "ff_siwis", "label": "Siwis (F)"}],
    "i": [{"id": "if_sara", "label": "Sara (F)"}, {"id": "im_nicola", "label": "Nicola (M)"}],
    "p": [{"id": "pf_dora", "label": "Dora (F)"}, {"id": "pm_alex", "label": "Alex (M)"}],
    "h": [{"id": "hf_alpha", "label": "Alpha (F)"}, {"id": "hm_omega", "label": "Omega (M)"}],
}


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------
def emit(kind: str, **data) -> None:
    print(EVT + json.dumps({"type": kind, **data}, ensure_ascii=False, default=str), flush=True)


def log(message: str) -> None:
    emit("log", message=message)


def prog(value: float, stage: str | None = None, item: str | None = None) -> None:
    emit("progress", value=round(max(0.0, min(1.0, value)), 4), stage=stage, item=item)


def result(**data) -> None:
    emit("result", **data)


# ---------------------------------------------------------------------------
# kernel side: run a command in a fresh process and relay its output
# ---------------------------------------------------------------------------
def _cuda_lib_dirs() -> list[str]:
    dirs = []
    for base in {p for p in sys.path if p.endswith("site-packages") or p.endswith("dist-packages")}:
        dirs += sorted(glob.glob(os.path.join(base, "nvidia", "*", "lib")))
    return dirs


def child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["GPUDOCK_ROOT"] = str(ROOT)
    env.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    env.setdefault("TOKENIZERS_PARALLELISM", "false")
    env.setdefault("GPUDOCK_ORIG_LD", env.get("LD_LIBRARY_PATH", ""))
    libs = _cuda_lib_dirs()
    if libs:
        env["LD_LIBRARY_PATH"] = os.pathsep.join(libs + [env["GPUDOCK_ORIG_LD"]]).strip(os.pathsep)
    return env


# Models whose dependencies don't support the runtime's Python (Colab moved to
# 3.13 in 2026; e.g. Kokoro's misaki/spacy pin <3.13) run in an isolated
# Python 3.12 environment managed with uv, with its own PyTorch.
ENV_PYTHON = "3.12"


def env_dir(name: str) -> Path:
    return ROOT / "envs" / name


def env_python(name: str) -> str | None:
    py = env_dir(name) / "bin" / "python"
    return str(py) if py.exists() else None


def env_vars() -> dict:
    """Environment for processes using an isolated env: drop CUDA libs we added for the system Python."""
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["GPUDOCK_ROOT"] = str(ROOT)
    env.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    if "GPUDOCK_ORIG_LD" in env:
        env["LD_LIBRARY_PATH"] = env["GPUDOCK_ORIG_LD"]
    return env


def ensure_env(name: str, *packages: str, torch: bool = True) -> str:
    """Create the env once (Python 3.12, plus PyTorch unless ``torch=False``) and install ``packages`` into it."""
    if not shutil.which("uv"):
        pip_install("uv")
    uv = shutil.which("uv") or "uv"
    py = env_python(name)
    if py is None:
        log(f"创建独立 Python {ENV_PYTHON} 环境（只在这个运行时第一次需要）")
        sh([uv, "venv", "--python", ENV_PYTHON, "--seed", str(env_dir(name))], quiet=True, env=env_vars())
        py = env_python(name)
        assert py
        if torch:
            log("安装 PyTorch（约 2 GB，Colab 网络通常 1 分钟内）")
            sh([uv, "pip", "install", "--python", py, "torch", "torchaudio", "numpy", "soundfile"], quiet=True, env=env_vars())
    if packages:
        log("安装 " + " ".join(packages))
        sh([uv, "pip", "install", "--python", py, *packages], quiet=True, env=env_vars())
    return py


def in_env(name: str, cmd: str, args: dict) -> bool:
    """Re-run this command inside env ``name`` unless we are already there. Returns True if delegated."""
    py = env_python(name)
    if FAKE or py is None or os.path.realpath(sys.executable) == os.path.realpath(py):
        return False
    args_path = ROOT / "jobs" / f"{cmd}-env-{time.time_ns()}.json"
    args_path.parent.mkdir(parents=True, exist_ok=True)
    args_path.write_text(json.dumps(args, ensure_ascii=False))
    code = subprocess.run([py, "-u", os.path.abspath(__file__), cmd, str(args_path)], env=env_vars()).returncode
    if code != 0:
        raise SystemExit(code)
    return True


SECRET_ENV = {"hf_token": "HF_TOKEN"}


def run_cli(cmd: str, args: dict | None = None) -> int:
    """Run ``cmd`` in a subprocess, streaming its stdout. Ctrl-C (kernel interrupt) stops it.

    ``args["_secrets"]`` (e.g. a Hugging Face token) goes to the child's environment
    only, never into the args file on disk.
    """
    args = dict(args or {})
    secrets = args.pop("_secrets", None) or {}
    jobs = ROOT / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    args_path = jobs / f"{cmd}-{time.time_ns()}.json"
    args_path.write_text(json.dumps(args, ensure_ascii=False))
    env = child_env()
    for key, value in secrets.items():
        if key in SECRET_ENV and value:
            env[SECRET_ENV[key]] = str(value)
    proc = subprocess.Popen(
        [sys.executable, "-u", os.path.abspath(__file__), cmd, str(args_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
        start_new_session=True,
    )
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            print(line, end="", flush=True)
        code = proc.wait()
    except KeyboardInterrupt:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=15)
        except Exception:
            os.killpg(proc.pid, signal.SIGKILL)
        emit("cancelled")
        raise
    if code != 0:
        raise RuntimeError(f"{cmd} 失败（退出码 {code}）")
    return code


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def sh(cmd: list[str] | str, *, quiet: bool = False, check: bool = True, env: dict | None = None) -> str:
    """Run a command, streaming its output as log events; return the output."""
    proc = subprocess.Popen(
        cmd, shell=isinstance(cmd, str), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=env
    )
    out = []
    assert proc.stdout is not None
    for line in proc.stdout:
        out.append(line)
        if line.startswith(EVT):
            print(line, end="", flush=True)
        elif not quiet and line.strip():
            log(line.rstrip()[-300:])
    code = proc.wait()
    text = "".join(out)
    if check and code != 0:
        raise RuntimeError(f"命令失败（{code}）：{cmd if isinstance(cmd, str) else ' '.join(cmd)}\n{text[-2000:]}")
    return text


def pip_install(*packages: str) -> None:
    log("pip install " + " ".join(packages))
    sh([sys.executable, "-m", "pip", "install", "-q", "--progress-bar", "off", *packages])


_apt_updated = False


def apt_install(*packages: str) -> None:
    global _apt_updated
    if not shutil.which("apt-get"):
        log("没有 apt-get，跳过系统包：" + " ".join(packages))
        return
    if not _apt_updated:
        sh(["apt-get", "-qq", "update"], quiet=True, check=False)
        _apt_updated = True
    sh(["apt-get", "-qq", "-y", "install", *packages], quiet=True, check=False)


def installed() -> dict:
    path = ROOT / "installed.json"
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def mark_installed(recipe: str, **meta) -> None:
    data = installed()
    data[recipe] = {"at": time.time(), "agent": VERSION, **meta}
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / "installed.json").write_text(json.dumps(data, indent=2))


def probe(path: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type", "-of", "json", path],
        capture_output=True,
        text=True,
    )
    data = json.loads(out.stdout or "{}")
    kinds = {s.get("codec_type") for s in data.get("streams", [])}
    return {
        "duration": float((data.get("format") or {}).get("duration") or 0),
        "has_video": "video" in kinds,
        "has_audio": "audio" in kinds,
    }


def to_wav(src: str, dst: str, *, sr: int, channels: int, start: float | None = None, duration: float | None = None) -> str:
    cmd = ["ffmpeg", "-v", "error", "-y"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", src]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-vn", "-ac", str(channels), "-ar", str(sr), "-c:a", "pcm_s16le", dst]
    subprocess.run(cmd, check=True)
    return dst


def wav_duration(path: str) -> float:
    with wave.open(path, "rb") as w:
        return w.getnframes() / float(w.getframerate())


def write_wav_int16(path: str, samples, sr: int) -> None:
    """``samples`` is a float sequence/array in [-1, 1], mono."""
    try:
        import numpy as np

        data = (np.clip(np.asarray(samples, dtype=np.float32), -1, 1) * 32767).astype("<i2").tobytes()
    except ImportError:
        data = b"".join(struct.pack("<h", int(max(-1, min(1, s)) * 32767)) for s in samples)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(data)


def fmt_ts(seconds: float, sep: str = ",") -> str:
    ms = int(round(max(0.0, seconds) * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def to_srt(segments: list[dict], key: str = "text", second: str | None = None) -> str:
    lines = []
    for i, seg in enumerate(segments, 1):
        text = (seg.get(key) or "").strip()
        if second and (seg.get(second) or "").strip():
            text = f"{text}\n{seg[second].strip()}"
        lines.append(f"{i}\n{fmt_ts(seg['start'])} --> {fmt_ts(seg['end'])}\n{text}\n")
    return "\n".join(lines)


def to_vtt(segments: list[dict]) -> str:
    body = "\n".join(f"{fmt_ts(s['start'], '.')} --> {fmt_ts(s['end'], '.')}\n{s['text'].strip()}\n" for s in segments)
    return "WEBVTT\n\n" + body


def gpu_name() -> str | None:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip().splitlines()[0] if out.stdout.strip() else None
    except Exception:
        return None


def has_cuda() -> bool:
    return gpu_name() is not None


SENT_END = set("。！？!?.…")
SOFT_BREAK = set("，,、；;：:")
_CJK = r"\u4e00-\u9fff"


def split_sentences(words: list[dict], max_seconds: float = 8.0) -> list[dict]:
    """Group word timings into subtitle-sized sentences.

    Breaks after sentence-ending punctuation; a sentence longer than
    ``max_seconds`` is also broken at a comma (or at the longest pause).
    """
    out: list[dict] = []
    cur: list[dict] = []

    def flush(upto: int | None = None) -> None:
        nonlocal cur
        part, cur = (cur, []) if upto is None else (cur[: upto + 1], cur[upto + 1 :])
        text = "".join(w["word"] for w in part).strip()
        if text:
            out.append({"start": part[0]["start"], "end": part[-1]["end"], "text": text})

    for w in words:
        cur.append(w)
        token = w["word"].strip()
        if token and token[-1] in SENT_END and not re.fullmatch(r"\d+\.", token):
            flush()
        elif cur[-1]["end"] - cur[0]["start"] > max_seconds and len(cur) > 1:
            soft = [i for i, x in enumerate(cur[:-1]) if x["word"].strip() and x["word"].strip()[-1] in SOFT_BREAK]
            if soft:
                flush(soft[-1])
            else:
                gaps = [(cur[i + 1]["start"] - cur[i]["end"], i) for i in range(len(cur) - 1)]
                flush(max(gaps)[1])
    if cur:
        flush()
    return out


def fix_zh_punct(text: str) -> str:
    """Whisper often emits ASCII punctuation in Chinese; use full-width marks next to CJK."""
    table = {",": "，", "?": "？", "!": "！", ":": "：", ";": "；"}
    for a, b in table.items():
        text = re.sub(rf"(?<=[{_CJK}])\s*{re.escape(a)}\s*", b, text)
        text = re.sub(rf"\s*{re.escape(a)}\s*(?=[{_CJK}])", b, text)
    return re.sub(rf"(?<=[{_CJK}])\.(?=\s|$|[{_CJK}])", "。", text)

# ---------------------------------------------------------------------------
# translation prompt (shared with the local server's external-API translator)
# ---------------------------------------------------------------------------
def translation_messages(batch: list[dict], src: str, tgt: str) -> list[dict]:
    tgt_info = LANGS.get(tgt, {"name": tgt, "rate": 12, "unit": "个字符"})
    src_name = LANGS.get(src, {}).get("name", "原文语言") if src and src != "auto" else "原文语言"
    system = (
        f"你是专业的视频字幕翻译，把字幕从{src_name}翻译成{tgt_info['name']}，译文会被用来配音。\n"
        "要求：\n"
        "1. 意译为主：口语化、地道，像母语者原创的台词，避免直译腔；专有名词保持一致。\n"
        f"2. 每条都给了原句时长（秒）。{tgt_info['name']}朗读大约每秒 {tgt_info['rate']} {tgt_info['unit']}，"
        "译文长度不要超过这个时长能读完的量，宁可精简也不要超长。\n"
        "3. 一条对应一条，不合并、不拆分、不遗漏，id 原样返回。\n"
        '4. 只输出 JSON：{"translations": [{"id": 1, "text": "..."}]}，不要任何解释。'
    )
    payload = [{"id": s["id"], "seconds": round(float(s.get("seconds", 0)), 1), "text": s["text"]} for s in batch]
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps({"segments": payload}, ensure_ascii=False)},
    ]


def parse_translations(content: str) -> dict[int, str]:
    text = content.strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("模型没有返回 JSON")
    data = json.loads(text[start : end + 1])
    items = data.get("translations", data if isinstance(data, list) else [])
    return {int(i["id"]): str(i.get("text", "")).strip() for i in items if "id" in i}


def shorten_messages(items: list[dict], lang: str) -> list[dict]:
    info = LANGS.get(lang, {"name": lang, "unit": "个字符"})
    system = (
        f"你是配音稿编辑。下面每条{info['name']}台词读出来太长，放不进原来的时间。"
        f"在不改变意思的前提下改写得更短，每条不超过 max_chars {info['unit']}；口语、自然、语句完整，保留标点，可以删去不重要的修饰。"
        '只输出 JSON：{"translations": [{"id": 1, "text": "..."}]}'
    )
    payload = [{"id": i["id"], "max_chars": int(i["max_chars"]), "text": i["text"]} for i in items]
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps({"segments": payload}, ensure_ascii=False)},
    ]


def batches(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_status(args: dict) -> None:
    info = {
        "agent": VERSION,
        "python": sys.version.split()[0],
        "gpu": gpu_name(),
        "installed": installed(),
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ollama": _ollama_up(),
    }
    result(**info)


def cmd_install(args: dict) -> None:
    recipe = args["recipe"]
    if recipe not in RECIPES:
        raise SystemExit(f"未知模型：{recipe}")
    prog(0.02, f"部署 {RECIPES[recipe]['name']}")
    if FAKE:
        time.sleep(0.2)
        mark_installed(recipe, fake=True)
        prog(1.0, "完成")
        result(recipe=recipe, ok=True)
        return
    {
        "whisper": _install_whisper,
        "demucs": _install_demucs,
        "kokoro": _install_kokoro,
        "voxcpm": _install_voxcpm,
        "qwen": _install_qwen,
        "comfyui": _install_comfyui,
    }[recipe](args)
    prog(1.0, "完成")
    result(recipe=recipe, ok=True)


def _install_whisper(args: dict) -> None:
    model = args.get("model") or "large-v3-turbo"
    pip_install("faster-whisper>=1.1")
    prog(0.4, "下载 Whisper 模型并自检")
    smoke = [sys.executable, "-u", os.path.abspath(__file__), "_whisper_smoke", model]
    try:
        sh(smoke, env=child_env())
    except RuntimeError as err:
        if not re.search(r"libcublas|libcudnn|cuda", str(err), re.I):
            raise
        log("CUDA 库版本不匹配，安装 CUDA 12 运行库后重试…")
        pip_install("nvidia-cublas-cu12", "nvidia-cudnn-cu12==9.*")
        sh(smoke, env=child_env())
    mark_installed("whisper", model=model)


def _whisper_smoke(model: str) -> None:
    import numpy as np
    from faster_whisper import WhisperModel

    device = "cuda" if has_cuda() else "cpu"
    m = WhisperModel(model, device=device, compute_type="float16" if device == "cuda" else "int8")
    segs, info = m.transcribe(np.zeros(16000, dtype=np.float32), language="en")
    list(segs)
    log(f"Whisper {model} 自检通过（{device}）")


def _install_demucs(args: dict) -> None:
    pip_install("demucs>=4.0", "soundfile")
    prog(0.6, "下载 htdemucs 权重")
    sh([sys.executable, "-c", "from demucs.pretrained import get_model; get_model('htdemucs'); print('ok')"], env=child_env())
    mark_installed("demucs")


def _install_kokoro(args: dict) -> None:
    apt_install("espeak-ng")
    prog(0.1, "准备独立 Python 环境")
    py = ensure_env("tts", "kokoro>=0.9.4", "misaki[zh]>=0.9.4")
    prog(0.7, "下载 Kokoro 模型与音色")
    code = (
        "from kokoro import KPipeline\n"
        "for lang, voice, text in [('z','zf_xiaoxiao','你好'),('a','af_heart','Hello')]:\n"
        "    p = KPipeline(lang_code=lang, repo_id='hexgrad/Kokoro-82M')\n"
        "    list(p(text, voice=voice))\n"
        "print('ok')\n"
    )
    sh([py, "-c", code], env=env_vars())
    mark_installed("kokoro", env="tts")


def _install_voxcpm(args: dict) -> None:
    prog(0.1, "准备独立 Python 环境")
    py = ensure_env("tts", "voxcpm>=2")
    prog(0.5, "下载 VoxCPM2 模型（约 4 GB）")
    code = "from voxcpm import VoxCPM\nVoxCPM.from_pretrained('openbmb/VoxCPM2', load_denoiser=False)\nprint('ok')\n"
    sh([py, "-c", code], env=env_vars())
    mark_installed("voxcpm", env="tts")


def _ollama_up() -> bool:
    try:
        with urllib.request.urlopen(OLLAMA + "/api/version", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_ollama() -> None:
    if _ollama_up():
        return
    if not shutil.which("ollama"):
        raise RuntimeError("还没有部署 Qwen（Ollama）")
    logf = open(ROOT / "ollama.log", "ab")
    env = dict(os.environ, OLLAMA_HOST="127.0.0.1:11434", OLLAMA_KEEP_ALIVE="5m")
    subprocess.Popen(["ollama", "serve"], stdout=logf, stderr=logf, env=env, start_new_session=True)
    for _ in range(60):
        if _ollama_up():
            return
        time.sleep(1)
    raise RuntimeError("Ollama 服务没有启动起来")


def _install_qwen(args: dict) -> None:
    model = args.get("model") or "qwen3:8b"
    if not shutil.which("ollama"):
        apt_install("zstd", "pciutils")
        prog(0.15, "安装 Ollama")
        sh("curl -fsSL https://ollama.com/install.sh | sh", quiet=True)
    ensure_ollama()
    prog(0.3, f"下载 {model}")
    proc = subprocess.Popen(["ollama", "pull", model], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    buf, last = b"", 0.0
    assert proc.stdout is not None
    while True:
        ch = proc.stdout.read(1)
        if not ch:
            break
        if ch in (b"\r", b"\n"):
            line = buf.decode(errors="ignore").strip()
            buf = b""
            m = re.search(r"(\d+)%", line)
            if m and time.time() - last > 2:
                last = time.time()
                prog(0.3 + 0.65 * int(m.group(1)) / 100, f"下载 {model} {m.group(1)}%")
        else:
            buf += ch
    if proc.wait() != 0:
        raise RuntimeError(f"拉取 {model} 失败")
    pulled = sorted(set(installed().get("qwen", {}).get("models", [])) | {model})
    mark_installed("qwen", model=model, models=pulled)


def cmd_transcribe(args: dict) -> None:
    """inputs: [{path, name}], out_dir, model, language, vad, formats -> per-item segments."""
    out_dir = Path(args["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    inputs = args["inputs"]
    language = args.get("language") or None
    if language == "auto":
        language = None
    formats = set(args.get("formats") or ["srt", "txt"])
    model = None
    if not FAKE:
        from faster_whisper import WhisperModel

        device = "cuda" if has_cuda() else "cpu"
        prog(0.01, "加载 Whisper 模型")
        model = WhisperModel(args.get("model") or "large-v3-turbo", device=device, compute_type="float16" if device == "cuda" else "int8")
    items = []
    for idx, item in enumerate(inputs):
        name = item["name"]
        stem = Path(name).stem
        base = idx / len(inputs)
        span = 1 / len(inputs)
        prog(base, "转写", name)
        emit("item", name=name, status="running")
        audio = str(out_dir / f".{stem}.16k.wav")
        to_wav(item["path"], audio, sr=16000, channels=1)
        total = wav_duration(audio) or 1.0
        segments: list[dict] = []
        detected = language
        if FAKE:
            t = 0.0
            while t < total - 0.3:
                end = min(total, t + 2.0)
                segments.append({"id": len(segments) + 1, "start": round(t, 3), "end": round(end, 3), "text": f"第{len(segments) + 1}句测试内容"})
                t = end
            detected = language or "zh"
        else:
            prompt = "以下是普通话的句子，使用简体中文和标点。" if language == "zh" else None
            samples = _decode(audio, 16000)
            segs, info = model.transcribe(
                samples,
                language=language,
                vad_filter=bool(args.get("vad", True)),
                beam_size=5,
                initial_prompt=prompt,
                word_timestamps=True,
            )
            detected = info.language
            words: list[dict] = []
            for seg in segs:
                if seg.words:
                    words += [{"start": w.start, "end": w.end, "word": w.word} for w in seg.words]
                elif seg.text.strip():
                    words.append({"start": seg.start, "end": seg.end, "word": seg.text})
                prog(base + span * min(1.0, seg.end / total), "转写", name)
            for i, s in enumerate(split_sentences(words, max_seconds=float(args.get("max_seconds", 8.0))), 1):
                text = fix_zh_punct(s["text"]) if detected == "zh" else s["text"]
                segments.append({"id": i, "start": round(s["start"], 3), "end": round(s["end"], 3), "text": text})
        outputs = []
        if "srt" in formats:
            (out_dir / f"{stem}.srt").write_text(to_srt(segments), encoding="utf-8")
            outputs.append(str(out_dir / f"{stem}.srt"))
        if "vtt" in formats:
            (out_dir / f"{stem}.vtt").write_text(to_vtt(segments), encoding="utf-8")
            outputs.append(str(out_dir / f"{stem}.vtt"))
        if "txt" in formats:
            (out_dir / f"{stem}.txt").write_text("\n".join(s["text"] for s in segments) + "\n", encoding="utf-8")
            outputs.append(str(out_dir / f"{stem}.txt"))
        seg_path = out_dir / f"{stem}.segments.json"
        seg_path.write_text(json.dumps({"language": detected, "duration": total, "segments": segments}, ensure_ascii=False, indent=1), encoding="utf-8")
        if "json" in formats:
            outputs.append(str(seg_path))
        os.remove(audio)
        emit("item", name=name, status="done", outputs=outputs, language=detected, segments=len(segments))
        items.append({"name": name, "language": detected, "duration": total, "segments_path": str(seg_path), "outputs": outputs})
    prog(1.0, "完成")
    result(items=items)


def cmd_separate(args: dict) -> None:
    """inputs: [{path, name}], out_dir -> vocals.wav / accompaniment.wav per item."""
    out_dir = Path(args["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    inputs = args["inputs"]
    model = None
    if not FAKE:
        import torch
        from demucs.apply import apply_model
        from demucs.pretrained import get_model

        prog(0.01, "加载 Demucs")
        model = get_model("htdemucs")
        model.eval()
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model.to(device)
    items = []
    for idx, item in enumerate(inputs):
        name, stem = item["name"], Path(item["name"]).stem
        prog(idx / len(inputs), "分离人声与背景", name)
        vocals = str(out_dir / f"{stem}.vocals.wav")
        accomp = str(out_dir / f"{stem}.accompaniment.wav")
        if FAKE:
            to_wav(item["path"], vocals, sr=44100, channels=2)
            dur = wav_duration(vocals)
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", f"{dur:.3f}", "-c:a", "pcm_s16le", accomp],
                check=True,
            )
        else:
            import numpy as np
            import soundfile as sf
            import torch

            src = str(out_dir / f".{stem}.44k.wav")
            to_wav(item["path"], src, sr=model.samplerate, channels=model.audio_channels)
            data, sr = sf.read(src, dtype="float32", always_2d=True)
            wav = torch.from_numpy(data.T.copy())
            ref = wav.mean(0)
            wav = (wav - ref.mean()) / (ref.std() + 1e-8)
            with torch.no_grad():
                sources = apply_model(model, wav[None].to(device), split=True, overlap=0.25, progress=False)[0]
            sources = sources.cpu() * (ref.std() + 1e-8) + ref.mean()
            names = list(model.sources)
            voc = sources[names.index("vocals")].numpy().T
            rest = sum(sources[i] for i, n in enumerate(names) if n != "vocals").numpy().T
            sf.write(vocals, np.clip(voc, -1, 1), sr, subtype="PCM_16")
            sf.write(accomp, np.clip(rest, -1, 1), sr, subtype="PCM_16")
            os.remove(src)
        emit("item", name=name, status="done")
        items.append({"name": name, "vocals": vocals, "accompaniment": accomp})
    prog(1.0, "完成")
    result(items=items)


def cmd_tts(args: dict) -> None:
    """engine, lang, voice, speed, reference{path,start,duration}, items:[{text, out}]."""
    engine = args.get("engine", "kokoro")
    if in_env("tts", "tts", args):
        return
    items = args["items"]
    lang = args.get("lang", "zh")
    out_info = []
    synth = _tts_engine(engine, lang, args)
    for idx, item in enumerate(items):
        prog(idx / max(1, len(items)), "合成语音", item.get("label") or str(idx + 1))
        text = (item.get("text") or "").strip()
        Path(item["out"]).parent.mkdir(parents=True, exist_ok=True)
        if not text:
            write_wav_int16(item["out"], [0.0] * 4800, 24000)
        else:
            synth(text, item["out"], item.get("speed"))
        out_info.append({"out": item["out"], "duration": round(wav_duration(item["out"]), 3)})
        if (idx + 1) % 10 == 0:
            log(f"已合成 {idx + 1}/{len(items)} 句")
    prog(1.0, "完成")
    result(items=out_info)


def _tts_engine(engine: str, lang: str, args: dict):
    speed = float(args.get("speed") or 1.0)
    if FAKE:

        def fake(text: str, out: str, item_speed: float | None = None) -> None:
            sr = 24000
            dur = max(0.4, min(6.0, 0.12 * len(text))) / (item_speed or speed)
            write_wav_int16(out, [0.2 * math.sin(2 * math.pi * 220 * i / sr) for i in range(int(sr * dur))], sr)

        return fake
    if engine == "kokoro":
        import numpy as np
        import soundfile as sf
        from kokoro import KPipeline

        code = LANGS.get(lang, {}).get("kokoro")
        if not code:
            raise RuntimeError(f"Kokoro 不支持{LANGS.get(lang, {}).get('name', lang)}，请改用 VoxCPM2。")
        pipe = KPipeline(lang_code=code, repo_id="hexgrad/Kokoro-82M")
        voice = args.get("voice") or KOKORO_VOICES[code][0]["id"]

        def kokoro(text: str, out: str, item_speed: float | None = None) -> None:
            s = item_speed or speed
            chunks = [np.asarray(audio, dtype=np.float32) for _, _, audio in pipe(text, voice=voice, speed=s) if audio is not None]
            sf.write(out, np.concatenate(chunks) if chunks else np.zeros(2400, dtype=np.float32), 24000, subtype="PCM_16")

        return kokoro
    if engine == "voxcpm":
        import soundfile as sf
        from voxcpm import VoxCPM

        model = VoxCPM.from_pretrained("openbmb/VoxCPM2", load_denoiser=False)
        ref = args.get("reference") or {}
        ref_path = None
        if ref.get("path"):
            ref_path = str(ROOT / "jobs" / f"ref-{time.time_ns()}.wav")
            to_wav(ref["path"], ref_path, sr=16000, channels=1, start=ref.get("start"), duration=ref.get("duration"))

        def voxcpm(text: str, out: str, item_speed: float | None = None) -> None:
            kwargs = {"text": text, "cfg_value": 2.0, "inference_timesteps": 10}
            if ref_path:
                kwargs["reference_wav_path"] = ref_path
            wav = model.generate(**kwargs)
            sf.write(out, wav, model.tts_model.sample_rate, subtype="PCM_16")

        return voxcpm
    raise RuntimeError(f"未知的语音引擎：{engine}")


def cmd_translate(args: dict) -> None:
    """segments:[{id,text,seconds}], src, tgt, model -> {translations:{id:text}} via Ollama."""
    segments = args["segments"]
    src, tgt = args.get("src", "auto"), args["tgt"]
    out: dict[int, str] = {}
    if FAKE:
        out = {int(s["id"]): f"[{tgt}] {s['text']}" for s in segments}
    else:
        ensure_ollama()
        model = args.get("model") or installed().get("qwen", {}).get("model") or "qwen3:8b"
        chunks = list(batches(segments, 25))
        for i, chunk in enumerate(chunks):
            prog(i / len(chunks), "翻译字幕")
            for attempt in range(3):
                try:
                    content = ollama_chat(model, translation_messages(chunk, src, tgt), json_mode=True)
                    got = parse_translations(content)
                    missing = [s for s in chunk if int(s["id"]) not in got]
                    if missing and attempt < 2:
                        log(f"有 {len(missing)} 条没翻译，重试")
                        continue
                    out.update(got)
                    break
                except Exception as err:
                    if attempt == 2:
                        raise
                    log(f"翻译重试：{err}")
        ollama_unload(model)
    prog(1.0, "完成")
    result(translations={str(k): v for k, v in out.items()})


def ollama_chat(model: str, messages: list[dict], *, json_mode: bool = False, timeout: int = 600) -> str:
    body = {"model": model, "messages": messages, "stream": False, "think": False, "options": {"temperature": 0.3, "num_ctx": 8192}}
    if json_mode:
        body["format"] = "json"
    req = urllib.request.Request(OLLAMA + "/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())["message"]["content"]


def ollama_unload(model: str) -> None:
    """Free the LLM's VRAM/RAM right away so the next step (TTS, separation) has room."""
    try:
        body = json.dumps({"model": model, "keep_alive": 0}).encode()
        req = urllib.request.Request(OLLAMA + "/api/generate", data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=60).read()
    except Exception as err:
        log(f"释放大模型显存失败（可忽略）：{err}")


def cmd_shorten(args: dict) -> None:
    """items:[{id,text,max_chars}], lang -> {translations:{id:text}} via Ollama."""
    items = args["items"]
    if FAKE:
        result(translations={str(i["id"]): i["text"][: max(2, int(i["max_chars"]))] for i in items})
        return
    ensure_ollama()
    model = args.get("model") or installed().get("qwen", {}).get("model") or "qwen3:8b"
    out: dict[int, str] = {}
    for chunk in batches(items, 20):
        try:
            out.update(parse_translations(ollama_chat(model, shorten_messages(chunk, args["lang"]), json_mode=True)))
        except Exception as err:
            log(f"缩短译文失败，保留原句：{err}")
    ollama_unload(model)
    result(translations={str(k): v for k, v in out.items()})


def cmd_chat(args: dict) -> None:
    if FAKE:
        result(reply=f"（测试）你说：{args['prompt']}")
        return
    ensure_ollama()
    model = args.get("model") or installed().get("qwen", {}).get("model") or "qwen3:8b"
    prog(0.1, "思考中")
    reply = ollama_chat(model, [{"role": "user", "content": args["prompt"]}])
    result(reply=reply.strip(), model=model)


def cmd_reference(args: dict) -> None:
    """Pick a clean ~10 s stretch of speech from segments to use as a cloning reference."""
    segs = args["segments"]
    best_start, best_len = 0.0, 0.0
    for i, s in enumerate(segs):
        start, end = s["start"], s["end"]
        j = i
        while j + 1 < len(segs) and segs[j + 1]["start"] - segs[j]["end"] < 0.6 and segs[j + 1]["end"] - start <= 12:
            j += 1
            end = segs[j]["end"]
        if end - start > best_len:
            best_start, best_len = start, end - start
        if best_len >= 8:
            break
    result(start=round(best_start, 3), duration=round(min(max(best_len, 3.0), 12.0), 3))


def _decode(path: str, sr: int, tempo: float = 1.0):
    import numpy as np

    cmd = ["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(sr)]
    if abs(tempo - 1.0) > 0.01:
        filters, t = [], tempo
        while t > 2.0:
            filters.append("atempo=2.0")
            t /= 2.0
        filters.append(f"atempo={t:.4f}")
        cmd += ["-filter:a", ",".join(filters)]
    cmd += ["-f", "f32le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32).copy()


def cmd_dub_render(args: dict) -> None:
    """Place each segment's TTS clip on the timeline, mix with the accompaniment, mux.

    args: name, duration, segments:[{start,end,tts,text,source}], accompaniment?, video?,
          out_dir, max_tempo, bgm_volume, voice_volume
    """
    import numpy as np

    sr = 44100
    out_dir = Path(args["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(args["name"]).stem
    segs = sorted(args["segments"], key=lambda s: s["start"])
    total = float(args["duration"])
    max_tempo = float(args.get("max_tempo", 1.35))
    track = np.zeros(int((total + 1.0) * sr), dtype=np.float32)
    max_drift = float(args.get("max_drift", 0.8))
    sped, clipped = 0, 0
    cursor = 0.0
    for i, seg in enumerate(segs):
        prog(i / max(1, len(segs)) * 0.7, "对齐配音", args["name"])
        if not seg.get("tts"):
            continue
        # A line may start late (the previous one ran long) and may borrow up
        # to ``max_drift`` seconds of the next line's start, so the schedule
        # bends a little instead of cutting words off; drift never compounds.
        start = max(float(seg["start"]), cursor)
        if i + 1 < len(segs):
            window_end = float(segs[i + 1]["start"]) + max_drift
        else:
            window_end = total
        slot = max(0.25, window_end - start - 0.05)
        clip = _decode(seg["tts"], sr)
        dur = len(clip) / sr
        if dur > slot * 1.02:
            tempo = min(dur / slot, max_tempo)
            clip = _decode(seg["tts"], sr, tempo)
            sped += 1
        limit = int(slot * sr)
        if len(clip) > limit:
            clipped += 1
            fade = min(int(0.06 * sr), limit)
            clip = clip[:limit].copy()
            if fade:
                clip[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)
        a = int(start * sr)
        b = min(len(track), a + len(clip))
        track[a:b] += clip[: b - a]
        seg["start"], seg["end"] = round(start, 3), round(start + len(clip) / sr, 3)
        cursor = start + len(clip) / sr + 0.08
    peak = float(np.max(np.abs(track))) if len(track) else 0.0
    if peak > 0.98:
        track *= 0.98 / peak
    track *= float(args.get("voice_volume", 1.0))
    track = track[: int(total * sr)]
    dub_wav = str(out_dir / f"{stem}.dub-voice.wav")
    write_wav_int16(dub_wav, track, sr)

    prog(0.75, "混音", args["name"])
    mixed = str(out_dir / f"{stem}.dubbed.wav")
    if args.get("accompaniment"):
        vol = float(args.get("bgm_volume", 0.9))
        subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y", "-i", dub_wav, "-i", args["accompaniment"],
                "-filter_complex", f"[1:a]volume={vol}[bg];[0:a][bg]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.97",
                "-ar", str(sr), "-ac", "2", "-c:a", "pcm_s16le", mixed,
            ],
            check=True,
        )
    else:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", dub_wav, "-ac", "2", "-c:a", "pcm_s16le", mixed], check=True)

    outputs = []
    if args.get("video"):
        prog(0.88, "合成视频", args["name"])
        out_video = str(out_dir / f"{stem}.dubbed.mp4")
        subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y", "-i", args["video"], "-i", mixed,
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", out_video,
            ],
            check=True,
        )
        outputs.append(out_video)
    outputs.append(mixed)
    srt_tgt = out_dir / f"{stem}.dubbed.srt"
    srt_tgt.write_text(to_srt(segs, key="text"), encoding="utf-8")
    srt_bi = out_dir / f"{stem}.bilingual.srt"
    srt_bi.write_text(to_srt(segs, key="text", second="source"), encoding="utf-8")
    outputs += [str(srt_tgt), str(srt_bi)]
    os.remove(dub_wav)
    prog(1.0, "完成", args["name"])
    result(outputs=outputs, sped_up=sped, trimmed=clipped)


def cmd_probe(args: dict) -> None:
    result(items=[{"path": p, **probe(p)} for p in args["paths"]])


def cmd_concat(args: dict) -> None:
    """Join wav files with a short pause between them: inputs, out, gap."""
    gap = float(args.get("gap", 0.35))
    sr = 24000
    pieces: list[bytes] = []
    silence = b"\x00\x00" * int(sr * gap)
    for path in args["inputs"]:
        tmp = path + ".24k.wav"
        to_wav(path, tmp, sr=sr, channels=1)
        with wave.open(tmp, "rb") as w:
            pieces.append(w.readframes(w.getnframes()))
        os.remove(tmp)
        pieces.append(silence)
    with wave.open(args["out"], "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"".join(pieces))
    result(out=args["out"], duration=round(wav_duration(args["out"]), 2))


# ---------------------------------------------------------------------------
# image / video generation: ComfyUI + the official workflow templates
# ---------------------------------------------------------------------------
COMFY_DIR = Path(os.environ.get("GPUDOCK_COMFY_DIR", "/content/ComfyUI"))
COMFY_PORT = int(os.environ.get("GPUDOCK_COMFY_PORT", "8188"))
COMFY = f"http://127.0.0.1:{COMFY_PORT}"
COMFY_REPO = "https://github.com/Comfy-Org/ComfyUI.git"
COMFY_CLI = "comfy-cli==1.29.0"  # only its UI→API workflow converter is used, in an isolated env
_SKIP_REQ = re.compile(r"^\s*(torch|torchvision|torchaudio|comfyui-workflow-templates|comfyui-embedded-docs)\b", re.I)
GB = 1024**3


def _http_json(url: str, data: dict | None = None, timeout: float = 30) -> dict:
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"} if body is not None else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def _comfy_up() -> bool:
    try:
        _http_json(COMFY + "/system_stats", timeout=2)
        return True
    except Exception:
        return False


def comfy_free() -> None:
    """Ask a running ComfyUI to drop its cached models (no-op when it isn't running)."""
    if _comfy_up():
        try:
            _http_json(COMFY + "/free", {"unload_models": True, "free_memory": True})
            log("已释放 ComfyUI 占用的显存")
        except Exception:
            pass


def ensure_comfy() -> None:
    if _comfy_up():
        return
    if not (COMFY_DIR / "main.py").exists():
        raise RuntimeError("还没有部署 ComfyUI")
    log_path = ROOT / "comfyui.log"
    logf = open(log_path, "ab")
    cmd = [sys.executable, "main.py", "--listen", "127.0.0.1", "--port", str(COMFY_PORT), "--disable-auto-launch"]
    if not has_cuda():
        cmd.append("--cpu")
    env = env_vars()
    env.pop("HF_TOKEN", None)
    proc = subprocess.Popen(cmd, cwd=COMFY_DIR, stdout=logf, stderr=logf, env=env, start_new_session=True)
    for _ in range(300):
        if _comfy_up():
            return
        if proc.poll() is not None:
            break
        time.sleep(1)
    tail = log_path.read_text(errors="ignore")[-1500:]
    raise RuntimeError("ComfyUI 没有启动起来：\n" + tail)


def _templates_pin(meta_version: str | None) -> str:
    """comfyui-workflow-templates is a meta package that also pulls GBs of preview media; install only its JSON part."""
    if not meta_version:
        return ""
    try:
        info = _http_json(f"https://pypi.org/pypi/comfyui-workflow-templates/{meta_version}/json", timeout=20)
        for dep in info["info"].get("requires_dist") or []:
            m = re.match(r"comfyui-workflow-templates-json\s*==\s*([\w.]+)\s*$", dep)
            if m:
                return "==" + m.group(1)
    except Exception as err:
        log(f"查询模板版本失败，使用最新版：{err}")
    return ""


def _install_comfyui(args: dict) -> None:
    if not (COMFY_DIR / ".git").exists():
        prog(0.05, "下载 ComfyUI")
        sh(["git", "clone", "--depth", "1", COMFY_REPO, str(COMFY_DIR)], quiet=True)
    commit = sh(["git", "-C", str(COMFY_DIR), "rev-parse", "--short", "HEAD"], quiet=True).strip()
    prog(0.2, "安装 ComfyUI 依赖")
    reqs = (COMFY_DIR / "requirements.txt").read_text().splitlines()
    meta = next((m.group(1) for line in reqs if (m := re.match(r"comfyui-workflow-templates==([\w.]+)", line.strip()))), None)
    keep = [line for line in reqs if line.strip() and not line.strip().startswith("#") and not _SKIP_REQ.match(line)]
    req_path = ROOT / "comfyui-requirements.txt"
    req_path.write_text("\n".join(keep) + "\n")
    pip_install("-r", str(req_path), f"comfyui-workflow-templates-json{_templates_pin(meta)}")
    prog(0.6, "准备工作流转换器")
    ensure_env("comfycli", COMFY_CLI, torch=False)
    prog(0.75, "启动 ComfyUI")
    ensure_comfy()
    info = _http_json(COMFY + "/system_stats")
    dev = ((info.get("devices") or [{}])[0]).get("name", "")
    log(f"ComfyUI {commit} 已启动 · {dev}")
    mark_installed("comfyui", commit=commit, templates=installed().get("comfyui", {}).get("templates", []))
    gens = args.get("gen_models") or []
    for i, gen in enumerate(gens):
        _download_gen_model(gen, 0.8 + 0.2 * i / len(gens), 0.2 / len(gens))


def template_file(name: str) -> Path:
    import comfyui_workflow_templates_json as t

    path = Path(t.__file__).parent / "templates" / f"{name}.json"
    if not path.exists():
        raise RuntimeError(f"找不到工作流模板 {name}：请在模型库里重新部署 ComfyUI")
    return path


def template_models(wf: dict) -> list[dict]:
    """Model files a template needs, from node.properties.models (muted/bypassed nodes excluded)."""
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []

    def add(m: dict) -> None:
        if not isinstance(m, dict) or not all(m.get(k) for k in ("name", "url", "directory")):
            return
        key = (m["directory"], m["name"])
        if key not in seen:
            seen.add(key)
            out.append(m)

    graphs = [wf, *((wf.get("definitions") or {}).get("subgraphs") or [])]
    for graph in graphs:
        for node in graph.get("nodes") or []:
            if node.get("mode") in (2, 4):
                continue
            for m in (node.get("properties") or {}).get("models") or []:
                add(m)
    if not out:
        for m in wf.get("models") or []:
            add(m)
    return out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def _open_url(url: str, *, method: str = "GET", headers: dict | None = None, timeout: float = 60):
    """Open a model URL, following redirects by hand so the HF token is only ever sent to huggingface.co."""
    import urllib.error
    import urllib.parse

    token = os.environ.get("HF_TOKEN")
    opener = urllib.request.build_opener(_NoRedirect)
    for _ in range(8):
        host = urllib.parse.urlparse(url).hostname or ""
        h = {"User-Agent": "gpudock-agent", **(headers or {})}
        if token and (host == "huggingface.co" or host.endswith(".huggingface.co")):
            h["Authorization"] = f"Bearer {token}"
        try:
            return opener.open(urllib.request.Request(url, method=method, headers=h), timeout=timeout)
        except urllib.error.HTTPError as err:
            if err.code in (301, 302, 303, 307, 308) and err.headers.get("Location"):
                if method == "HEAD" and err.headers.get("X-Linked-Size"):
                    return err  # HF answers HEAD with the LFS file size on the redirect itself
                url = urllib.parse.urljoin(url, err.headers["Location"])
                continue
            if err.code in (401, 403) and "huggingface.co" in host:
                repo = "/".join(urllib.parse.urlparse(url).path.strip("/").split("/")[:2])
                hint = "Token 没有这个仓库的权限" if token else "这个模型需要 Hugging Face Token"
                raise RuntimeError(
                    f"{hint}：请先打开 https://huggingface.co/{repo} 申请访问并同意许可，"
                    "再到 GPUDock 的“创作”页填写只读 Token。"
                ) from None
            if err.code == 404:
                raise RuntimeError(f"模型文件不存在（404）：{url}") from None
            raise
    raise RuntimeError(f"重定向次数过多：{url}")


def _remote_size(url: str) -> int:
    for attempt in range(4):
        try:
            with _open_url(url, method="HEAD", timeout=30) as r:
                return int(r.headers.get("X-Linked-Size") or r.headers.get("Content-Length") or 0)
        except RuntimeError:
            raise
        except Exception:
            if attempt == 3:
                raise
            time.sleep(1 + attempt * 2)
    return 0


def _download_file(url: str, dest: Path, on_bytes, size: int = 0) -> None:
    part = dest.with_name(dest.name + ".part")
    for attempt in range(6):
        have = part.stat().st_size if part.exists() else 0
        try:
            with _open_url(url, headers={"Range": f"bytes={have}-"} if have else None, timeout=120) as r:
                if have and r.status != 206:
                    on_bytes(-have)
                    have = 0
                with open(part, "ab" if have else "wb") as f:
                    while True:
                        chunk = r.read(8 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        on_bytes(len(chunk))
            got = part.stat().st_size
            if size and got < size:
                raise OSError(f"只收到 {got}/{size} 字节")
            os.replace(part, dest)
            return
        except RuntimeError:
            raise
        except Exception as err:
            if attempt == 5:
                raise RuntimeError(f"下载 {dest.name} 失败：{err}") from None
            log(f"下载 {dest.name} 中断，{2 ** attempt} 秒后续传：{err}")
            time.sleep(2**attempt)


def ensure_models(models: list[dict], base: float, span: float) -> None:
    """Download whatever template model files are missing into ComfyUI/models/<directory>/."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    root = (COMFY_DIR / "models").resolve()
    todo = []
    for m in models:
        dest = (root / m["directory"] / m["name"]).resolve()
        if root not in dest.parents:
            raise RuntimeError(f"模板里的模型路径不安全：{m['directory']}/{m['name']}")
        if not dest.exists():
            todo.append((m, dest))
    if not todo:
        prog(base + span, "模型已就绪")
        return
    prog(base, "查询模型大小")
    sizes = [_remote_size(m["url"]) for m, _ in todo]
    total = sum(sizes) or 1
    partial = sum(d.with_name(d.name + ".part").stat().st_size for _, d in todo if d.with_name(d.name + ".part").exists())
    free = shutil.disk_usage(COMFY_DIR).free
    if total - partial > free - 2 * GB:
        raise RuntimeError(
            f"运行时磁盘不够：这个模型还要下载约 {(total - partial) / GB:.0f} GB，只剩 {free / GB:.0f} GB。"
            "可以释放这张卡后重新启动一张（磁盘会清空），或换 A100 / H100 这类磁盘更大的运行时。"
        )
    log(f"下载 {len(todo)} 个模型文件，共 {total / GB:.1f} GB（只在这个运行时第一次需要）")
    lock = threading.Lock()
    state = {"done": partial, "t": time.time(), "last": 0.0, "mark": partial}

    def on_bytes(n: int) -> None:
        with lock:
            state["done"] += n
            now = time.time()
            if now - state["last"] >= 2:
                speed = (state["done"] - state["mark"]) / max(0.5, now - state["t"]) / 1024**2
                state["last"] = now
                pct = state["done"] / total
                prog(base + span * pct, f"下载模型 {state['done'] / GB:.1f}/{total / GB:.1f} GB · {speed:.0f} MB/s")

    def fetch(job):
        (m, dest), size = job
        dest.parent.mkdir(parents=True, exist_ok=True)
        _download_file(m["url"], dest, on_bytes, size)
        log(f"已下载 {m['directory']}/{m['name']}")

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(fetch, zip(todo, sizes, strict=True)))
    prog(base + span, "模型已就绪")


def _mark_templates(*templates: str) -> None:
    info = {k: v for k, v in installed().get("comfyui", {}).items() if k not in ("at", "agent")}
    info["templates"] = sorted(set(info.get("templates", [])) | set(templates))
    mark_installed("comfyui", **info)


def _download_gen_model(gen: str, base: float, span: float) -> None:
    """Pre-download every model file a generation model's templates use (all modes)."""
    spec = GEN_MODELS[gen]
    log(f"准备 {spec['name']} 的模型文件")
    files: list[dict] = []
    for mode, tpl in spec["templates"].items():
        wf = json.loads(template_file(tpl).read_text())
        _prepare_ui(wf, tpl, mode, [])
        files += [m for m in template_models(wf) if m not in files]
    ensure_models(files, base, span)
    _mark_templates(*spec["templates"].values())


def to_api(wf: dict) -> dict:
    """UI-format workflow → API prompt, using comfy-cli's converter against this server's /object_info."""
    py = env_python("comfycli") or ensure_env("comfycli", COMFY_CLI, torch=False)
    work = ROOT / "jobs" / f"convert-{time.time_ns()}"
    work.mkdir(parents=True, exist_ok=True)
    (work / "wf.json").write_text(json.dumps(wf))
    with urllib.request.urlopen(COMFY + "/object_info", timeout=120) as r:
        (work / "object_info.json").write_bytes(r.read())
    code = (
        "import json, sys\n"
        "from comfy_cli.workflow_to_api import convert_ui_to_api\n"
        "wf = json.load(open(sys.argv[1])); oi = json.load(open(sys.argv[2]))\n"
        "json.dump(convert_ui_to_api(wf, oi), open(sys.argv[3], 'w'))\n"
    )
    sh([py, "-c", code, str(work / "wf.json"), str(work / "object_info.json"), str(work / "api.json")], quiet=True, env=env_vars())
    api = json.loads((work / "api.json").read_text())
    shutil.rmtree(work, ignore_errors=True)
    return api


def _prepare_ui(wf: dict, template: str, mode: str, images: list[str]) -> None:
    """Template edits that must happen before conversion: (un)bypass nodes, set input image names."""
    spec = TEMPLATE_PARAMS.get(template, {})
    nodes = {str(n["id"]): n for n in wf.get("nodes") or []}
    for nid in spec.get("enable", {}).get(mode, []):
        nodes[nid]["mode"] = 0
    for nid, name in zip(spec.get("ui_images", {}).get(mode, []), images, strict=False):
        nodes[nid]["widgets_values"][0] = name


def _frames(seconds: float, fps: int, step: int) -> int:
    n = max(1, round(seconds * fps / step))
    return n * step + 1


def apply_params(api: dict, template: str, mode: str, p: dict, images: list[str]) -> None:
    spec = TEMPLATE_PARAMS[template]
    values = {
        "prompt": p["prompt"],
        "width": int(p["width"]),
        "height": int(p["height"]),
        "megapixels": round(int(p["width"]) * int(p["height"]) / 1e6, 2),
        "seed": int(p["seed"]),
        "fast": bool(p.get("fast", True)),
    }
    if p.get("seconds"):
        values["seconds"] = float(p["seconds"])
        if spec.get("fps"):
            values["frames"] = _frames(float(p["seconds"]), spec["fps"], spec.get("frame_step", 4))
        values["seconds_int"] = int(round(float(p["seconds"])))
    for key, targets in spec.items():
        if key not in values:
            continue
        for node, field in targets:
            if node not in api:
                raise RuntimeError(f"模板 {template} 结构变了（找不到节点 {node}），请更新 GPUDock")
            api[node]["inputs"][field] = values[key]
    # images: API-level slots get a LoadImage node each
    for node, prefix in spec.get("clear", {}).get(mode, []):
        api[node]["inputs"] = {k: v for k, v in api[node]["inputs"].items() if not k.startswith(prefix)}
    for node in spec.get("drop", {}).get(mode, []):
        api.pop(node, None)
    slots = spec.get("image_slots", {}).get(mode, [])
    for i, name in enumerate(images[: len(slots)]):
        node, field = slots[i]
        load_id = f"gpudock_img_{i}"
        api[load_id] = {"class_type": "LoadImage", "inputs": {"image": name}, "_meta": {"title": f"GPUDock image {i + 1}"}}
        api[node]["inputs"][field] = [load_id, 0]
    for node, field, value in spec.get("set", {}).get(mode, []):
        api[node]["inputs"][field] = value


def _prompt_error(body: dict) -> str:
    err = body.get("error") or {}
    lines = [err.get("message") or "ComfyUI 拒绝了这个工作流"]
    for nid, info in list((body.get("node_errors") or {}).items())[:4]:
        for e in (info.get("errors") or [])[:2]:
            lines.append(f"节点 {nid}（{info.get('class_type')}）：{e.get('message')} {e.get('details') or ''}".strip())
    return "\n".join(lines)


def _exec_error(data: dict, gpus: list[str]) -> str:
    msg = f"{data.get('exception_type', '')}: {data.get('exception_message', '')}".strip(": ")
    if re.search(r"out of memory|OutOfMemory|CUDA error: out of memory|allocat", msg, re.I):
        return f"显存不够：试试降低分辨率或时长，或换显存更大的卡（推荐 {' / '.join(gpus)}）。\n{msg[:300]}"
    return f"节点 {data.get('node_id')}（{data.get('node_type')}）出错：{msg[:600]}"


def _run_prompt(api: dict, gpus: list[str], base: float, span: float) -> tuple[str, dict]:
    import asyncio
    import uuid

    import aiohttp

    cid = uuid.uuid4().hex
    state = {"pid": None}

    async def go() -> str:
        timeout = aiohttp.ClientTimeout(total=None, sock_read=None)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.ws_connect(f"ws://127.0.0.1:{COMFY_PORT}/ws?clientId={cid}", heartbeat=30, max_msg_size=0) as ws:
                async with s.post(COMFY + "/prompt", json={"prompt": api, "client_id": cid}) as r:
                    body = await r.json(content_type=None)
                    if r.status != 200 or "prompt_id" not in body:
                        raise RuntimeError(_prompt_error(body))
                pid = state["pid"] = body["prompt_id"]
                total = max(1, len(api))
                done: set[str] = set()
                frac = 0.0
                stage = "生成中"
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue  # binary previews
                    evt = json.loads(msg.data)
                    kind, d = evt.get("type"), evt.get("data") or {}
                    if d.get("prompt_id") not in (None, pid):
                        continue
                    if kind == "execution_cached":
                        done.update(d.get("nodes") or [])
                    elif kind == "executing":
                        if d.get("node") is None:
                            return pid
                        done.add(d["node"])
                        frac, stage = 0.0, "加载模型 / 运行节点"
                    elif kind == "progress":
                        frac = float(d.get("value", 0)) / max(1, float(d.get("max", 1)))
                        stage = f"采样 {d.get('value')}/{d.get('max')} 步"
                    elif kind == "execution_error":
                        raise RuntimeError(_exec_error(d, gpus))
                    elif kind == "execution_interrupted":
                        raise RuntimeError("生成被中断")
                    elif kind == "execution_success":
                        return pid
                    prog(base + span * min(0.99, (max(0, len(done) - 1) + frac) / total), stage)
                if not _comfy_up():
                    raise RuntimeError("ComfyUI 进程退出了，多半是内存（RAM）不够：换高内存版或更大的卡再试。")
                raise RuntimeError("和 ComfyUI 的连接断开了")

    def interrupt(*_):
        try:
            _http_json(COMFY + "/interrupt", {})
            if state["pid"]:
                _http_json(COMFY + "/queue", {"delete": [state["pid"]]})
        except Exception:
            pass
        raise KeyboardInterrupt

    old = signal.signal(signal.SIGTERM, interrupt)
    try:
        pid = asyncio.run(go())
    finally:
        signal.signal(signal.SIGTERM, old)
    hist = _http_json(f"{COMFY}/history/{pid}").get(pid) or {}
    return pid, hist.get("outputs") or {}


def _collect_outputs(outputs: dict, out_dir: Path, stem: str) -> list[dict]:
    files = []
    for node_out in outputs.values():
        for items in node_out.values():
            if not isinstance(items, list):
                continue
            for it in items:
                if not (isinstance(it, dict) and it.get("filename") and it.get("type") == "output"):
                    continue
                src = COMFY_DIR / "output" / (it.get("subfolder") or "") / it["filename"]
                if not src.exists():
                    continue
                dst = out_dir / f"{stem}-{len(files) + 1}{src.suffix}" if files else out_dir / f"{stem}{src.suffix}"
                shutil.copy2(src, dst)
                files.append({"path": str(dst), "name": dst.name})
    return files


_PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d4944415478da63f8cfc0f01f0005000201a5f9d0a60000000049454e44ae426082"
)


def cmd_generate(args: dict) -> None:
    """model, mode, prompt, width, height, seconds, seed, images:[path], out_dir -> files."""
    import random

    spec = GEN_MODELS[args["model"]]
    mode = args["mode"]
    template = spec["templates"].get(mode)
    if not template:
        raise SystemExit(f"{spec['name']} 不支持这种模式：{mode}")
    out_dir = Path(args["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    seed = int(args["seed"]) if args.get("seed") not in (None, "") else random.randint(1, 2**48)
    stem = f"{args['model']}-{mode}-{seed}"
    if FAKE:
        prog(0.5, "生成中（测试）")
        ext = ".png" if spec["kind"] == "image" else ".mp4"
        path = out_dir / f"{stem}{ext}"
        path.write_bytes(_PNG_1PX if ext == ".png" else b"\x00\x00\x00\x18ftypmp42")
        result(files=[{"path": str(path), "name": path.name}], seed=seed, template=template, gen_seconds=0.5)
        return
    for loaded in _ollama_loaded():
        ollama_unload(loaded)
    prog(0.01, "启动 ComfyUI")
    ensure_comfy()
    images = []
    for i, src in enumerate(args.get("images") or []):
        name = f"gpudock_{int(time.time())}_{i}{Path(src).suffix.lower() or '.png'}"
        (COMFY_DIR / "input").mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, COMFY_DIR / "input" / name)
        images.append(name)
    wf = json.loads(template_file(template).read_text())
    _prepare_ui(wf, template, mode, images)
    prog(0.03, "检查模型文件")
    ensure_models(template_models(wf), 0.03, 0.42)
    _mark_templates(template)
    prog(0.46, "转换工作流")
    api = to_api(wf)
    apply_params(api, template, mode, {**args, "seed": seed}, images)
    api_path = out_dir / f"{stem}.workflow-api.json"
    api_path.write_text(json.dumps(api, ensure_ascii=False, indent=1))
    prog(0.48, "生成中")
    t0 = time.time()
    pid, outputs = _run_prompt(api, spec["gpus"], 0.48, 0.5)
    files = _collect_outputs(outputs, out_dir, stem)
    if not files:
        raise RuntimeError("ComfyUI 运行完了，但没有找到输出文件")
    files.append({"path": str(api_path), "name": api_path.name})
    gen_seconds = round(time.time() - t0, 1)
    log(f"生成完成，用时 {gen_seconds:.0f} 秒，种子 {seed}")
    result(files=files, seed=seed, template=template, prompt_id=pid, gen_seconds=gen_seconds)


def _ollama_loaded() -> list[str]:
    if not _ollama_up():
        return []
    try:
        return [m["name"] for m in _http_json(OLLAMA + "/api/ps", timeout=5).get("models") or []]
    except Exception:
        return []


COMMANDS = {
    "status": cmd_status,
    "install": cmd_install,
    "transcribe": cmd_transcribe,
    "separate": cmd_separate,
    "tts": cmd_tts,
    "translate": cmd_translate,
    "chat": cmd_chat,
    "shorten": cmd_shorten,
    "reference": cmd_reference,
    "dub_render": cmd_dub_render,
    "probe": cmd_probe,
    "concat": cmd_concat,
    "generate": cmd_generate,
}
# Commands that load their own models onto the GPU: ComfyUI's cached models are
# released first so they fit (generation keeps them loaded between runs).
GPU_COMMANDS = {"transcribe", "separate", "tts", "translate", "chat", "shorten"}


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "_whisper_smoke":
        _whisper_smoke(argv[2])
        return 0
    if len(argv) < 2 or argv[1] not in COMMANDS:
        print(f"usage: gpudock_agent.py {{{','.join(COMMANDS)}}} args.json", file=sys.stderr)
        return 2
    args = json.loads(Path(argv[2]).read_text()) if len(argv) > 2 else {}
    try:
        if argv[1] in GPU_COMMANDS and not FAKE:
            comfy_free()
        COMMANDS[argv[1]](args)
    except Exception as err:  # report, then fail the process
        emit("error", message=f"{type(err).__name__}: {err}")
        import traceback

        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
