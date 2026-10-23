"""从 hf-mirror 下载 faster-whisper small 模型（4 个文件）到 video-kit\\models。

- 纯 urllib，不依赖 huggingface_hub（避免回连被墙站点）
- 每个文件：timeout=30，最多 3 次，退避 3/6/9s
- 落地硬断言：model.bin 精确 483,546,902 B；两个 json 可解析；vocabulary.txt >1000 行
- 失败即删半成品并 exit 1，不留“看起来装了”的目录
"""
import argparse
import json
import os
import sys
import time
import urllib.request

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEST = os.environ.get("DSH_VIDEO_KIT_MODEL") or os.path.join(KIT, "models", "faster-whisper-small")
BASE = "https://hf-mirror.com/Systran/faster-whisper-small/resolve/main/"
FILES = ["model.bin", "config.json", "tokenizer.json", "vocabulary.txt"]
UA = {"User-Agent": "Mozilla/5.0 video-kit/4"}
EXPECT = {"model.bin": 483546902}


def fetch(name: str) -> str:
    path = os.path.join(DEST, name)
    part = path + ".part"
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(BASE + name, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r, open(part, "wb") as f:
                total = 0
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    total += len(chunk)
                    if total % (64 << 20) < (1 << 20):
                        print(f"  {name}: {total / 1048576:.0f} MB", flush=True)
            want = EXPECT.get(name)
            if want is not None and total != want:
                raise RuntimeError(f"{name} 长度不符：{total} != {want}")
            os.replace(part, path)
            print(f"OK {name} {total} B", flush=True)
            return path
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"retry {attempt + 1}/3 {name}: {type(e).__name__} {e}", flush=True)
            time.sleep(3 * (attempt + 1))
    raise SystemExit(f"下载失败 {name}: {last}")


def main() -> None:
    global DEST
    ap = argparse.ArgumentParser(description="下载 faster-whisper-small 模型")
    ap.add_argument("--dest", default=DEST, help="落地目录（默认 <kit>/models/faster-whisper-small）")
    DEST = ap.parse_args().dest
    os.makedirs(DEST, exist_ok=True)
    try:
        for f in FILES:
            fetch(f)
        with open(os.path.join(DEST, "config.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        with open(os.path.join(DEST, "tokenizer.json"), encoding="utf-8") as fh:
            json.load(fh)
        with open(os.path.join(DEST, "vocabulary.txt"), encoding="utf-8") as fh:
            lines = sum(1 for _ in fh)
        size = os.path.getsize(os.path.join(DEST, "model.bin"))
        assert size == EXPECT["model.bin"], size
        assert lines > 1000, lines
        print(json.dumps({"model.bin": size, "vocab_lines": lines,
                          "config_keys": sorted(cfg)[:6]}, ensure_ascii=False))
        with open(os.path.join(DEST, "MODEL.md"), "w", encoding="utf-8") as fh:
            fh.write(f"# faster-whisper-small\n\n- 来源：{BASE}\n"
                     f"- model.bin: {size} B\n- vocabulary.txt: {lines} 行\n"
                     "- 校验：长度精确匹配 + 两个 json 可解析\n")
        print("MODEL READY")
    except BaseException as e:  # noqa: BLE001
        print(f"FAILED: {type(e).__name__} {e}", flush=True)
        import shutil
        shutil.rmtree(DEST, ignore_errors=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
