"""video-kit 的机器接口：给 DSH 插件（node）调，一个命令做一件事，只吐 JSON。

为什么要有这一层：
- phase_a.ps1 是人用的（彩色输出、退出码语义），从 node 里再起一层 PowerShell 既慢
  又多一层转义坑（`$`、引号、URL 里的 `&`）。这里把同样的流程用 Python 重写一遍，
  进程边界只有 "node -> python"。
- stdout 只放 JSON；所有进度日志走 stderr，调用方可以流式显示而不弄脏结果。

命令：
    frames      --source <path|url> --job <id> [--max-frames 40] [--width 640]
    transcript  --job <id> [--language zh] [--srt]
    prepare     --source <path|url> --job <id> [--max-frames 40] [--no-transcript]
    info        --source <path|url>

约定沿用 phase_a.ps1（都是踩过的坑）：
- yt-dlp 走 `python -m yt_dlp`，不用 .exe（本机 Smart App Control 拦截轮子生成的 exe）。
- 时长解析 `ffmpeg -i` 的 stderr（本机 ffmpeg essentials 构建没有 ffprobe）。
- URL 任务的 meta 记**相对**路径，因为临时目录随后改名。
- 先写临时目录、成功后 rename，避免半成品 job 目录。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import uuid

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WS = os.path.dirname(KIT)


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
VPY = os.path.join(KIT, ".venv", "Scripts", "python.exe")
JOBS = os.path.join(KIT, "jobs")
TRANSCRIBE = os.path.join(KIT, "scripts", "transcribe.py")

JOB_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
DUR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
RES_RE = re.compile(r"Stream #\d+:\d+.*?Video:.*?,\s*(\d{2,5})x(\d{2,5})")


class KitError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def emit(payload: dict, code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    return code


def child_env() -> dict:
    """子进程环境：把 shims 挂进 PYTHONPATH（PyAV 被 Smart App Control 拦，用 stub 顶掉）。"""
    env = dict(os.environ)
    shims = os.path.join(KIT, "shims")
    old = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = shims + (os.pathsep + old if old else "")
    return env


def run(argv: list[str], what: str) -> subprocess.CompletedProcess:
    r = subprocess.run(argv, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=child_env())
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip()[-800:]
        raise KitError(f"{what} 失败（退出码 {r.returncode}）：{tail}")
    return r


def probe(path: str) -> dict:
    """用 `ffmpeg -i` 的 stderr 取时长和分辨率（本机没有 ffprobe）。"""
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", path],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    out = (r.stderr or "") + (r.stdout or "")
    m = DUR_RE.search(out)
    if not m:
        raise KitError("解析不出时长，ffmpeg 输出片段：" + out.strip()[:400])
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    if dur <= 0:
        raise KitError(f"时长解析为 {dur} 秒")
    rm = RES_RE.search(out)
    width, height = (int(rm.group(1)), int(rm.group(2))) if rm else (0, 0)
    return {"duration": round(dur, 2), "width": width, "height": height,
            "has_audio": "Audio:" in out}


def download(url: str, dest_dir: str) -> tuple[str, dict]:
    """yt-dlp 匿名下载到 dest_dir，返回 (媒体文件, info)。"""
    tpl = os.path.join(dest_dir, "source.%(ext)s")
    argv = [sys.executable, "-m", "yt_dlp", "--no-config", "--no-playlist",
            "--no-warnings", "--no-progress", "--write-info-json",
            "-S", "res:480,abr:96", "-f", "bv*+ba/b",
            "--merge-output-format", "mp4", "--ffmpeg-location", FFMPEG,
            "-o", tpl, "--", url]
    log(f"下载（yt-dlp，匿名，最高 480p）：{url}")
    run(argv, "yt-dlp")
    media = [f for f in os.listdir(dest_dir)
             if not f.endswith((".json", ".part", ".ytdl"))]
    if len(media) != 1:
        raise KitError(f"下载产物应恰好 1 个媒体文件，实际 {len(media)}：{media}")
    info: dict = {}
    for f in os.listdir(dest_dir):
        if f.endswith(".info.json"):
            with open(os.path.join(dest_dir, f), encoding="utf-8") as fh:
                j = json.load(fh)
            info = {"title": j.get("title"), "uploader": j.get("uploader"),
                    "webpage_id": j.get("id")}
            break
    return os.path.join(dest_dir, media[0]), info


def frames_impl(a) -> dict:
    if not JOB_RE.fullmatch(a.job):
        raise KitError(f"非法 job 名：{a.job}")
    if not os.path.exists(FFMPEG):
        raise KitError(f"找不到 ffmpeg：{FFMPEG}")
    if not (1 <= a.max_frames <= 500):
        raise KitError("max-frames 应在 1..500")
    if a.width < 160 or a.width > 1920:
        raise KitError("width 应在 160..1920")

    final = os.path.join(JOBS, a.job)
    if os.path.exists(final):
        raise KitError(f"jobs/{a.job} 已存在，换一个 job 名或先删掉")

    is_url = a.source.startswith(("http://", "https://"))
    os.makedirs(JOBS, exist_ok=True)
    tmp = os.path.join(JOBS, f"_tmp_{a.job}_{uuid.uuid4().hex[:8]}")
    frame_dir = os.path.join(tmp, "frames")
    src_dir = os.path.join(tmp, "src")
    os.makedirs(frame_dir)
    os.makedirs(src_dir)
    try:
        info: dict = {}
        if is_url:
            src, info = download(a.source, src_dir)
        else:
            src = os.path.abspath(a.source)
            if not os.path.exists(src):
                raise KitError(f"输入文件不存在：{a.source}")
            if os.path.getsize(src) <= 0:
                raise KitError(f"输入文件是 0 字节：{a.source}")
            info = {"title": os.path.splitext(os.path.basename(src))[0]}

        p = probe(src)
        dur = p["duration"]
        fps = min(1.0, a.max_frames / dur)
        plan = min(a.max_frames, int(-(-dur * fps // 1)))
        log(f"时长 {dur:.2f}s；抽帧 fps={fps:.6f}（预计 {plan} 帧，宽 {a.width}）")
        run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", src,
             "-vf", f"fps={fps:.6f},scale={a.width}:-2", "-q:v", "3",
             os.path.join(frame_dir, "f%03d.jpg")], "抽帧")
        names = sorted(f for f in os.listdir(frame_dir) if f.endswith(".jpg"))
        if not names:
            raise KitError("没有产生任何静帧")

        frames = [{"index": i + 1, "file": os.path.join("frames", n),
                   "timestamp": round(min((i + 0.5) / fps, dur), 2)}
                  for i, n in enumerate(names)]
        meta = {
            "job": a.job, "kind": "url" if is_url else "file",
            "source_path": ("src/" + os.path.basename(src)) if is_url else src,
            "source_bytes": os.path.getsize(src),
            "duration": dur, "sample_fps": round(fps, 6),
            "frame_count": len(frames), "frames": frames,
            "width": p["width"], "height": p["height"],
            "has_audio": p["has_audio"],
        }
        if is_url:
            meta["url"] = a.source
        for k in ("title", "uploader", "webpage_id"):
            if info.get(k):
                meta[k] = info[k]
        meta["source_kept"] = bool(is_url)
        with open(os.path.join(tmp, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        lines = [f"# {a.job}", "",
                 f"- 来源：{meta.get('url') or meta['source_path']}"]
        if meta.get("title"):
            lines.append(f"- 标题：{meta['title']}")
        lines += [f"- 时长：{dur} 秒；采样 fps：{meta['sample_fps']}；帧数：{len(frames)}",
                  "", "## 时间轴"]
        lines += [f"- [{f['timestamp']}s] {f['file']}" for f in frames]
        lines += ["", "下一步：用 read_image 逐张看 frames/*.jpg，结合时间戳描述画面变化。"]
        with open(os.path.join(tmp, "report.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        os.replace(tmp, final)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    log(f"完成：{final}（{len(frames)} 帧）")
    return {
        "ok": True, "job": a.job, "job_dir": final,
        "frames_dir": os.path.join(final, "frames"),
        "duration": dur, "sample_fps": round(fps, 6),
        "frame_count": len(frames),
        "frames": [{"index": f["index"], "timestamp": f["timestamp"],
                    "path": os.path.join(final, f["file"].replace("/", os.sep))}
                   for f in frames],
        "title": meta.get("title"), "url": meta.get("url"),
        "has_audio": meta["has_audio"], "report": os.path.join(final, "report.md"),
    }


def cmd_frames(a) -> int:
    return emit(frames_impl(a))


def transcript_impl(a) -> tuple[dict, int]:
    if not JOB_RE.fullmatch(a.job):
        raise KitError(f"非法 job 名：{a.job}")
    job_dir = os.path.join(JOBS, a.job)
    if not os.path.exists(os.path.join(job_dir, "meta.json")):
        raise KitError(f"作业不存在或没有 meta.json：{job_dir}（先抽帧）")
    argv = [VPY, TRANSCRIBE, "--job", a.job]
    if a.language:
        argv += ["--language", a.language]
    if a.srt:
        argv.append("--srt")
    log(f"转写 {a.job}（faster-whisper small, cpu int8, 本地模型）…")
    r = subprocess.run(argv, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=child_env())
    tpath = os.path.join(job_dir, "transcript.json")
    if not os.path.exists(tpath):
        tail = (r.stderr or r.stdout or "").strip()[-800:]
        raise KitError(f"转写没有产出 transcript.json（退出码 {r.returncode}）：{tail}")
    with open(tpath, encoding="utf-8") as f:
        t = json.load(f)
    srt = os.path.join(job_dir, "transcript.srt")
    ok = t.get("status") == "ok"
    return {
        "ok": ok, "job": a.job, "status": t.get("status"),
        "reasons": t.get("reasons"), "language": t.get("language"),
        "duration": t.get("duration"), "coverage": t.get("coverage"),
        "cjk_ratio": t.get("cjk_ratio"), "chars": t.get("chars"),
        "n_segments": t.get("n_segments"), "text": t.get("text"),
        "segments": t.get("segments"),
        "transcript_json": tpath,
        "srt": srt if os.path.exists(srt) else None,
    }, (0 if ok else 3)


def cmd_transcript(a) -> int:
    payload, code = transcript_impl(a)
    return emit(payload, code)


def cmd_prepare(a) -> int:
    """一次调用拿到帧 + 文字：插件主入口。"""
    if not JOB_RE.fullmatch(a.job):
        raise KitError(f"非法 job 名：{a.job}")
    frames = frames_impl(a)
    if a.no_transcript:
        return emit(frames)
    tr, code = transcript_impl(argparse.Namespace(
        job=a.job, language=a.language, srt=a.srt))
    return emit({**frames, "transcript": tr}, 0 if tr.get("ok") else code)


def cmd_info(a) -> int:
    is_url = a.source.startswith(("http://", "https://"))
    if is_url:
        r = run([sys.executable, "-m", "yt_dlp", "--no-config", "--no-playlist",
                 "--no-warnings", "--skip-download", "--dump-single-json", "--", a.source],
                "yt-dlp --dump-single-json")
        j = json.loads(r.stdout)
        return emit({"ok": True, "kind": "url", "title": j.get("title"),
                     "uploader": j.get("uploader"), "duration": j.get("duration"),
                     "webpage_id": j.get("id"),
                     "thumbnail": j.get("thumbnail"),
                     "has_subtitles": bool(j.get("subtitles"))})
    p = probe(os.path.abspath(a.source))
    return emit({"ok": True, "kind": "file", "path": os.path.abspath(a.source), **p})


def main() -> int:
    # Windows 控制台编码兜底：命令行里中文标题常见
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="kit_api")
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("frames")
    f.add_argument("--source", required=True)
    f.add_argument("--job", required=True)
    f.add_argument("--max-frames", type=int, default=40)
    f.add_argument("--width", type=int, default=640)
    f.set_defaults(func=cmd_frames)

    t = sub.add_parser("transcript")
    t.add_argument("--job", required=True)
    t.add_argument("--language", default="zh")
    t.add_argument("--srt", action="store_true")
    t.set_defaults(func=cmd_transcript)

    p = sub.add_parser("prepare")
    p.add_argument("--source", required=True)
    p.add_argument("--job", required=True)
    p.add_argument("--max-frames", type=int, default=40)
    p.add_argument("--width", type=int, default=640)
    p.add_argument("--language", default="zh")
    p.add_argument("--srt", action="store_true")
    p.add_argument("--no-transcript", action="store_true")
    p.set_defaults(func=cmd_prepare)

    i = sub.add_parser("info")
    i.add_argument("--source", required=True)
    i.set_defaults(func=cmd_info)

    a = ap.parse_args()
    try:
        return a.func(a)
    except KitError as e:
        return emit({"ok": False, "error": str(e)}, 1)
    except Exception as e:  # noqa: BLE001 — 兜底，保证调用方永远拿到 JSON
        return emit({"ok": False, "error": f"{type(e).__name__}: {e}"}, 1)


if __name__ == "__main__":
    sys.exit(main())
