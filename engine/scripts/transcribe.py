"""Phase B：把一段音轨转成文字。

设计要点（都是踩过的坑）：
1. `import faster_whisper` 之前先把 `HF_*` 环境变量设好，否则它会回连 huggingface.co（本机不可达）。
2. 一律 `local_files_only=True`：只用本地模型，不联网、不落 `~\\.cache\\huggingface`。
3. `model.transcribe()` 返回的 `segments` 是**生成器**，必须 `list()`，否则 `len()` 直接 TypeError。
4. 音频解码走系统 ffmpeg → 16k 单声道 wav；**不用 PyAV**（本机被 Smart App Control 拦截）。
5. 断言"真的听到了话"：时间覆盖率 + 中文字符占比，而不是只看"有没有 segments"。

用法：
    python transcribe.py --job <id> [--language zh] [--srt]
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import wave

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def find_ffmpeg(kit: str) -> str:
    """按优先级找 ffmpeg：环境变量 → kit 自带 → 工作区 .tools → PATH。"""
    for cand in (
        os.environ.get("DSH_VIDEO_KIT_FFMPEG"),
        os.path.join(kit, "tools", "ffmpeg.exe"),
        os.path.join(os.path.dirname(kit), ".tools", "bin", "ffmpeg.exe"),
    ):
        if cand and os.path.isfile(cand):
            return cand
    found = shutil.which("ffmpeg")
    return found or os.path.join(kit, "tools", "ffmpeg.exe")


FFMPEG = find_ffmpeg(KIT)
MODEL_DIR = os.environ.get("DSH_VIDEO_KIT_MODEL") or os.path.join(KIT, "models", "faster-whisper-small")

# —— 必须在 import faster_whisper 之前完成 ——
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ["HF_HOME"] = os.path.join(KIT, "models")
os.environ["HUGGINGFACE_HUB_CACHE"] = os.path.join(KIT, "models", "hub")
os.environ["HF_HUB_OFFLINE"] = "1"

# —— PyAV 兜底：本机 Smart App Control 拦了 av 的 DLL，用 shims/av 顶掉 ——
# 插件从 node 起 python 时不会带上 PYTHONPATH，所以这里自己把 shims 插到最前面。
SHIMS = os.path.join(KIT, "shims")
if os.path.isdir(SHIMS) and SHIMS not in sys.path:
    sys.path.insert(0, SHIMS)

CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def read_wav(path: str):
    with wave.open(path, "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise RuntimeError(f"需要 16-bit 单声道 wav：{path}")
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    import numpy as np

    audio = np.frombuffer(raw, dtype=np.int16).astype("float32") / 32768.0
    return audio, len(audio) / float(rate)


def ensure_wav(src: str, dst: str) -> None:
    if os.path.exists(dst) and os.path.getsize(dst) > 44:
        return
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
           "-i", src, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", dst]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg 解码音频失败：{r.stderr.strip()[:500]}")


def fmt_ts(t: float) -> str:
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    ms = int(round((t - int(t)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--language", default="zh")
    ap.add_argument("--srt", action="store_true")
    ap.add_argument("--initial-prompt", default=None,
                    help="引导句。中文默认给简体提示，否则 small 模型常输出繁体")
    ap.add_argument("--min-coverage", type=float, default=0.5)
    ap.add_argument("--min-cjk", type=float, default=0.5)
    args = ap.parse_args()

    if args.initial_prompt is None and (args.language or "").startswith("zh"):
        args.initial_prompt = "以下是普通话的句子，请用简体中文转写。"

    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", args.job):
        raise SystemExit(f"非法 job 名：{args.job}")

    job_dir = os.path.join(KIT, "jobs", args.job)
    meta_path = os.path.join(job_dir, "meta.json")
    if not os.path.exists(meta_path):
        raise SystemExit(f"缺少 {meta_path}，先跑 phase_a")
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)

    src_dir = os.path.join(job_dir, "src")
    os.makedirs(src_dir, exist_ok=True)

    wav = os.path.join(src_dir, "audio.wav")
    if os.path.exists(wav) and os.path.getsize(wav) > 44:
        print(f"[1/3] 复用已解码的 {wav}", flush=True)
    else:
        src = meta.get("source_path") or ""
        if src and not os.path.isabs(src):  # URL 任务记的是相对路径（src\source.mp4）
            src = os.path.join(job_dir, src)
        if not (src and os.path.exists(src)):
            url = meta.get("url")
            if not url:
                raise SystemExit(f"源文件不存在且 meta 里没有 url：{src!r}")
            print(f"[0/3] 源文件不在，按需只下载音轨：{url}", flush=True)
            tgt = os.path.join(src_dir, "audio_source.%(ext)s")
            argv = [sys.executable, "-m", "yt_dlp", "--no-config", "--no-playlist",
                    "--no-warnings", "--no-progress", "-f", "ba/b", "-S", "abr:96",
                    "-o", tgt, "--", url]
            r = subprocess.run(argv, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            if r.returncode != 0:
                raise SystemExit(f"yt-dlp 取音轨失败（{r.returncode}）：{(r.stderr or r.stdout).strip()[-800:]}")
            cands = [f for f in os.listdir(src_dir)
                     if f.startswith("audio_source.") and not f.endswith((".part", ".ytdl"))]
            if not cands:
                raise SystemExit("yt-dlp 没产出音轨文件")
            src = os.path.join(src_dir, sorted(cands)[0])
        print(f"[1/3] 解码音频 -> {wav}", flush=True)
        ensure_wav(src, wav)
    audio, dur = read_wav(wav)

    print(f"[2/3] 加载模型 {MODEL_DIR}（本地，不联网）", flush=True)
    from faster_whisper import WhisperModel  # 延迟导入，确保上面的 env 生效

    model = WhisperModel(MODEL_DIR, device="cpu", compute_type="int8",
                         local_files_only=True)
    print(f"[3/3] 转写 {dur:.1f}s 音频（device=cpu, int8）…", flush=True)
    seg_iter, info = model.transcribe(
        audio, language=args.language or None, beam_size=5,
        vad_filter=True, condition_on_previous_text=False,
        initial_prompt=args.initial_prompt,
    )
    segments = list(seg_iter)  # ← 关键：生成器必须物化

    rows = [{"start": round(s.start, 2), "end": round(s.end, 2),
             "text": (s.text or "").strip()} for s in segments]
    text = "".join(r["text"] for r in rows).strip()
    non_blank = len(re.sub(r"\s", "", text))
    cjk = len(CJK.findall(text))
    cjk_ratio = (cjk / non_blank) if non_blank else 0.0
    coverage = (max((r["end"] for r in rows), default=0.0) / dur) if dur else 0.0

    status = "ok"
    reasons = []
    if len(rows) < 3:
        status = "no_speech"
        reasons.append(f"segments={len(rows)}<3")
    if coverage < args.min_coverage:
        status = "no_speech" if status == "ok" else status
        reasons.append(f"coverage={coverage:.2f}<{args.min_coverage}")
    if non_blank and cjk_ratio < args.min_cjk:
        reasons.append(f"cjk_ratio={cjk_ratio:.2f}<{args.min_cjk}")
        if status == "ok":
            status = "suspect"

    out = {
        "status": status, "reasons": reasons, "language": info.language,
        "language_probability": round(float(info.language_probability), 3),
        "duration": round(dur, 2), "n_segments": len(rows),
        "coverage": round(coverage, 3), "cjk_ratio": round(cjk_ratio, 3),
        "chars": non_blank, "text": text, "segments": rows,
        "verified": status == "ok",
    }
    with open(os.path.join(job_dir, "transcript.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    if args.srt:
        with open(os.path.join(job_dir, "transcript.srt"), "w", encoding="utf-8") as f:
            for i, r in enumerate(rows, 1):
                f.write(f"{i}\n{fmt_ts(r['start'])} --> {fmt_ts(r['end'])}\n{r['text']}\n\n")

    print(json.dumps({k: out[k] for k in
                      ("status", "reasons", "language", "duration", "n_segments",
                       "coverage", "cjk_ratio", "chars")}, ensure_ascii=False))
    print(f"文本前 120 字：{text[:120]}")
    return 0 if status == "ok" else 2


if __name__ == "__main__":
    sys.exit(main())
