# dsh-video-eyes

让 DSH 里的模型**真的看到**一段视频：本地 ffmpeg 按时间均匀抽静帧 + faster-whisper 转写音轨，
把「带时间戳的帧路径」和「全文文字」一起交给模型，模型再用 `read_image` 逐张看图。

纯本地：不下载云端模型、不上传画面、不调用任何视觉 API。跑完一个 3 分钟视频约 2-3 分钟（CPU）。

## 它解决什么

模型本身没有视频输入通道。要"看视频"，只能把它变成模型能吃的东西：

| 方案 | 模型看到什么 | 代价 |
| --- | --- | --- |
| 只给字幕/转写 | 只有说了什么，看不到画面（水印、图表、演示、字幕板） | 便宜 |
| 只给静帧 | 只有画面，不知道在讲什么 | 中 |
| **本插件（帧 + 转写）** | 画面细节 + 语音内容，可对齐时间轴 | 中，全本地 |

采样率可控：`maxFrames` 决定抽多少帧（默认 24）。**采样帧不是连续观看**——快速闪过的动作、
精确到帧的时序，它看不到。

## 依赖（缺一不可）

1. **ffmpeg**：`<workspace>\.tools\bin\ffmpeg.exe`（本插件按 video-kit 的目录约定找它）。
   不需要 ffprobe：时长/分辨率解析 `ffmpeg -i` 的 stderr。
2. **video-kit 的 venv**：`<video-kit>\.venv\Scripts\python.exe`，里面装 `faster-whisper`、
   `numpy`、`yt-dlp`（B 站链接下载用）。
3. **whisper 模型**：`<video-kit>\models\faster-whisper-small`（约 483 MB，本地加载，不联网）。
4. 引擎脚本：`<video-kit>\scripts\kit_api.py` 与 `transcribe.py`（本插件的所有实际工作都在这里）。

**这些不用你手工装**：插件包里自带安装器（`engine/install.ps1`），首次调用会引导执行 `video_eyes_setup`。

## 首次使用：装引擎

新机器上第一次调 `video_watch` 会得到一段引导而不是干巴巴的报错，照做即可：

```js
video_eyes_setup({ confirm: true, root: "<会话工作区>\\video-kit" })
```

它会依次做：找 Python 3.10+ → 找 ffmpeg → 检查 ffmpeg 能否写入该目录 → 建独立 venv →
装 `faster-whisper`（约 200 MB 依赖）→ 下 483 MB 转写模型 → 合成 4 秒视频冒烟。
全程本地，约 700 MB，一次性。装完会把路径写进 `<包>/engine/.kit-root`，之后**不用重启**、不用再设环境变量。

不加 `confirm: true` 只返回预览（要下载多少、装到哪），不执行。

`root` 必须落在**会话工作区**内：在 DSH 沙箱里 ffmpeg 只能写工作区，写 `%LOCALAPPDATA%`／`%TEMP%` 会被拒。

## 配置（引擎已经装过时）

| 来源 | 作用 |
| --- | --- |
| 插件 config `kitRoot` | 最高优先级 |
| 环境变量 `DSH_VIDEO_EYES_ROOT` | 指向含 `scripts/kit_api.py` 的目录 |
| `<包>/engine/.kit-root` | 一行绝对路径；`video_eyes_setup` 成功后自动写入 |
| 相对位置 / 作者本机路径 | 最后兜底 |
| `DSH_VIDEO_EYES_PYTHON` / config `pythonPath` | 覆盖 `<kit>/.venv/Scripts/python.exe` |

## 工具

### `video_watch`

一次调用拿到帧 + 转写。

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `target` | string（必填） | B 站链接、`BV...` 号，或本地视频绝对路径 |
| `maxFrames` | number | 抽帧上限，默认 24；长视频建议 16-30，短视频可到 40 |
| `transcript` | boolean | 是否转写，默认 true |
| `language` | string | 转写语言，默认 `zh` |
| `job` | string | 作业名（默认按时间生成）；同名作业不可重复 |

### `video_eyes_setup`

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `confirm` | boolean（必填） | 确认执行安装（约 700 MB 下载） |
| `root` | string | 安装目录，绝对路径，必须在会话工作区内 |
| `skipModel` | boolean | 只装依赖与 venv，不下载模型（只能抽帧） |

返回（节选）：

```json
{
  "ok": true,
  "job": "v-20261022-213045",
  "job_dir": ".../video-kit/jobs/v-20261022-213045",
  "duration": 219.68,
  "sample_fps": 0.136562,
  "frame_count": 30,
  "frames": [{ "index": 1, "timestamp": 3.66, "path": ".../frames/f001.jpg" }],
  "transcript": { "status": "ok", "language": "zh", "n_segments": 124, "coverage": 0.991, "text": "..." }
}
```

调用方（模型）拿到 `frames[].path` 后逐张 `read_image` 查看，结合 `transcript.text` 回答。

### `video_transcript`

只转写：给 `job`（已抽过帧的作业）或 `target`（会先抽 1 帧拿到音轨）。适合"已经看过画面、
只想要文字"的场景，比重新 `video_watch` 便宜。

## 装到 DSH

```powershell
# 1. 打包（在 plugin\dsh-video-eyes 目录）
npm pack                     # 产出 dsh-video-eyes-<version>.tgz

# 2. 安装到当前 desktop profile（需能写 ~/.dsh）
dsh plugin add <path-to-tgz> --profile desktop
```

本机实测：`dsh plugin add` 会写 `~\.dsh\profiles\desktop\package.json`，这在工作区沙箱外，
需要提权批准一次。

### 坑：必须「完全退出」应用，不是关窗口

profile 的工具表只在**宿主进程启动时**按 bundle 栈组一次。DSH 桌面端关窗口只会收到托盘，
进程还在跑（`Get-CimInstance Win32_Process -Filter "Name='DeepSeek Harness.exe'"` 可看启动时间）。
所以装完插件后：

1. 托盘图标 → 退出（或任务管理器结束 `DeepSeek Harness.exe`）；
2. 重新启动；
3. **在新会话**里试（旧会话的工具表不会热更新）。

验证装配是否生效（不启动界面也能查，换任意临时 profile 即可）：

```powershell
dsh --profile <tmp> --dump-config | Select-String -Pattern 'video-kit' -Context 0,1
# 期望看到：  - id: video-kit / name: dsh-video-eyes
```

### 坑：同版本号覆盖安装不生效

pnpm 对 `file:` 依赖按版本号复用解包结果。改完 `dsh/index.js` 再装同版本号，`node_modules` 里
还是旧代码 —— 必须 bump `package.json` 的 `version` 再打包安装。

自检（不需要装插件、不需要网络）：

```powershell
node dsh/index.js --self-test     # schema + render 三种形态
```

## 实测数据（本机，i5 级 CPU，无独显）

| 场景 | 结果 |
| --- | --- |
| 3 分 39 秒 B 站视频，抽 30 帧 | 25 秒（含下载 5.6 MB） |
| 同一视频，faster-whisper small int8 转写 | 约 2 分钟，124 段，覆盖率 0.991，1416 字 |
| 抽样帧能否回答问题 | 能识别画面里的工具名、代码、图表、字幕板（示例见 `video-kit/README.md`） |

## 已知限制

- **B 站匿名没有字幕接口**：`x/player/v2` 返回空 `subtitles`，所以文字只能靠转写。
- **长视频转写是线性成本**：1 小时约 15-25 分钟（CPU）。急的话先用 `transcript: false` 只看帧。
- **Smart App Control 会拦 PyAV**：本机用 `shims/av` 顶掉 `av` 的 import，音频解码走 ffmpeg。
  其他机器若未装 SAC 不受影响。
- **不支持实时/流**：只有"下载完的文件"（本地或 yt-dlp 能下的链接）。

## 与 dsh-video-understand 的区别

|  | dsh-video-eyes（本插件） | dsh-video-understand |
| --- | --- | --- |
| 模型看到画面像素 | ✅ 逐张静帧（可自行看细节） | ❌ 默认只有文本信息层；L1/L2 才把几帧上传 MiMo |
| 上传云端 | ❌ 全本地 | ✅ L1/L2 会上传帧到 MiMo |
| ASR | faster-whisper **small**（本地） | faster-whisper **tiny**（本地下载） |
| 时长探测 | 解析 `ffmpeg -i`（不需要 ffprobe） | 需要 **ffprobe**（Windows 常缺） |
| Windows 兼容 | 显式处理（SAC、路径、BOM） | Python 探测走 `python3`，Win 上默认失败 |
| 依赖完整度 | 自带 requirements + venv 引导 | `requirements.txt` 缺 pandas/torch，分层装 |

两者可以并存：`dsh-video-understand` 的"信息层 + 追问路由"在长视频批量归档上更省 token，
本插件在"要看清画面"的问题上更准。

## 许可

MIT。引擎复用同目录 `video-kit` 的脚本（同一作者、同一许可）。
