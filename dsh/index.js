// dsh-video-eyes — host half.
//
// 为什么要有这个插件：模型本身没有视频输入通道。这个插件把"看视频"拆成
// 模型真能消费的两样东西——
//   1) 一批带时间戳的静帧（模型用 read_image 逐张看，是"真看见"像素）；
//   2) 音轨的全文转写（faster-whisper small，本地 CPU，离线）。
// 相比之下，纯文本"信息层"方案把画面压成几行描述，模型看不到水印、字幕、
// 构图细节；纯逐帧方案又太贵。这里是可调采样率的折中。
//
// 引擎在外部 video-kit 目录里（不随插件打包模型，1GB 模型不该塞进 npm 包）：
//   video-kit/scripts/kit_api.py + video-kit/.venv + video-kit/models/faster-whisper-small
//
// 环境变量：
//   DSH_VIDEO_EYES_ROOT     video-kit 目录（默认 ./../../../ 相对本文件）
//   DSH_VIDEO_EYES_PYTHON   该 kit 的 venv python（默认 <root>/.venv/Scripts/python.exe）

import { spawn } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
// 宿主的工具注册是「标记校验」式的：ctx.tools.register() 只接受 defineTool() 产出的带标记对象，
// 裸对象会抛 `dynamic tool registration must use a tool returned by harness.defineTool(...)`。
// 这个包由宿主提供（better-sidebar 等第三方插件同样从这里 import），所以列进 peerDependencies。
let defineTool
try {
  ({ defineTool } = await import('@deepseek-ai/dsh-tools'))
} catch (error) {
  // 只有在仓库里手动跑 `node dsh/index.js --self-test` 时才允许退化成原对象；
  // 真正被宿主加载时解析失败必须炸出来，不能静默降级。
  if (!process.argv.includes('--self-test')) throw error
  defineTool = (options) => options
}

export const name = 'video-eyes'
export const inject = ['tools']

const __dirname = path.dirname(fileURLToPath(import.meta.url))

const FRAME_SCHEMA = {
  type: 'object',
  additionalProperties: true,
  properties: {
    index: { type: 'number' },
    timestamp: { type: 'number' },
    path: { type: 'string' },
  },
}

const TRANSCRIPT_SCHEMA = {
  type: 'object',
  additionalProperties: true,
  properties: {
    status: { type: 'string' },
    language: { type: 'string' },
    duration: { type: 'number' },
    coverage: { type: 'number' },
    cjk_ratio: { type: 'number' },
    chars: { type: 'number' },
    n_segments: { type: 'number' },
    text: { type: 'string' },
    srt: { oneOf: [{ type: 'string' }, { type: 'null' }] },
    transcript_json: { type: 'string' },
    reasons: { type: 'array', items: { type: 'string' } },
  },
}

const WATCH_SCHEMA = {
  type: 'object',
  additionalProperties: true,
  properties: {
    ok: { type: 'boolean' },
    job: { type: 'string' },
    job_dir: { type: 'string' },
    frames_dir: { type: 'string' },
    duration: { type: 'number' },
    sample_fps: { type: 'number' },
    frame_count: { type: 'number' },
    frames: { type: 'array', items: FRAME_SCHEMA },
    title: { oneOf: [{ type: 'string' }, { type: 'null' }] },
    url: { oneOf: [{ type: 'string' }, { type: 'null' }] },
    has_audio: { type: 'boolean' },
    report: { type: 'string' },
    transcript: TRANSCRIPT_SCHEMA,
  },
}

const TIMEOUT_MS = 20 * 60_000 // 下载 + 抽帧 + CPU 转写（1 小时视频可能十几分钟）

// 引擎位置可以用一个文本文件钉住：<包根>/engine/.kit-root 里写一行绝对路径。
// video_eyes_setup 装完会写这个文件，所以重装插件之后只要引擎还在，就不必再设环境变量。
function readKitRootFile() {
  try {
    const p = path.resolve(__dirname, '..', 'engine', '.kit-root')
    if (!fs.existsSync(p)) return null
    const v = fs.readFileSync(p, 'utf8').trim()
    return v || null
  } catch {
    return null
  }
}

function resolveKit(config) {
  const candidates = [
    config.kitRoot,
    process.env.DSH_VIDEO_EYES_ROOT,
    readKitRootFile(),
    path.resolve(__dirname, '..', '..', '..'), // video-kit/plugin/dsh-video-eyes/dsh -> video-kit
    'C:\\Users\\bjclo\\Documents\\deepseek-harness\\default-workspace\\video-kit', // 作者本机，发布版可删
  ].filter(Boolean)
  for (const c of candidates) {
    if (fs.existsSync(path.join(c, 'scripts', 'kit_api.py'))) return c
  }
  throw new Error(
    'video-kit 引擎未找到（需要含 scripts/kit_api.py 的目录）。已试：' + candidates.join(' | '),
  )
}

function resolvePython(config, kit) {
  const candidates = [
    config.pythonPath,
    process.env.DSH_VIDEO_EYES_PYTHON,
    path.join(kit, '.venv', 'Scripts', 'python.exe'),
    path.join(kit, '.venv', 'bin', 'python'),
  ].filter(Boolean)
  for (const c of candidates) {
    if (fs.existsSync(c)) return c
  }
  throw new Error(
    'video-kit 的 venv python 未找到（先跑 video-kit\\scripts\\phase_a.ps1 或按 README 建 venv）。已试：' +
      candidates.join(' | '),
  )
}

function runJson(python, script, args, signal, onLog) {
  return new Promise((resolve, reject) => {
    const proc = spawn(python, [script, ...args], {
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    })
    let stdout = ''
    let stderr = ''
    proc.stdout.on('data', (d) => (stdout += d))
    proc.stderr.on('data', (d) => {
      const s = d.toString()
      stderr += s
      for (const line of s.split(/\r?\n/)) if (line.trim()) onLog?.(line.trim())
    })
    proc.on('error', reject)
    proc.on('close', (code) => {
      const trimmed = stdout.trim()
      const line = trimmed.split(/\r?\n/).filter((l) => l.startsWith('{')).pop()
      if (!line) {
        reject(new Error(`kit_api 没有输出 JSON（exit ${code}）：${(stderr || trimmed).slice(-500)}`))
        return
      }
      try {
        resolve(JSON.parse(line))
      } catch (error) {
        reject(new Error(`kit_api JSON 解析失败：${error.message}；输出 ${line.slice(0, 300)}`))
      }
    })
    signal?.addEventListener('abort', () => proc.kill(), { once: true })
  })
}

function defaultJob(prefix) {
  const now = new Date()
  const pad = (n) => String(n).padStart(2, '0')
  return `${prefix}-${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`
}

function frameLines(value, limit = 40) {
  const frames = (value.frames || []).slice(0, limit)
  return frames.map((f) => `  [${f.timestamp}s] ${f.path}`)
}

function transcriptBlock(t) {
  if (!t) return []
  const lines = []
  if (t.text) {
    lines.push('', `📝 转写（${t.language}, ${t.n_segments} 段, 覆盖 ${(t.coverage * 100).toFixed(0)}%, ${t.chars} 字）`)
    lines.push(t.text.length > 4000 ? t.text.slice(0, 4000) + '…（截断，完整文本见 transcript_json）' : t.text)
    if (t.srt) lines.push(`SRT：${t.srt}`)
  } else if (t.status && t.status !== 'ok') {
    lines.push('', `📝 转写状态：${t.status}${t.reasons?.length ? '（' + t.reasons.join('; ') + '）' : ''}——这段音轨没有可用的语音内容`)
  }
  return lines
}

// 插件自带的引擎安装器（首次使用引导）。包根 = dsh/ 的上一级。
const BUNDLED_INSTALLER = path.resolve(__dirname, '..', 'engine', 'install.ps1')

function runPowerShell(argv, timeoutMs = 30 * 60_000) {
  return new Promise((resolve) => {
    let child
    try {
      child = spawn('powershell.exe', argv, { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
    } catch (error) {
      resolve({ code: -1, out: `spawn 失败：${error.message}` })
      return
    }
    let out = ''
    child.stdout.on('data', (d) => (out += d.toString()))
    child.stderr.on('data', (d) => (out += d.toString()))
    const timer = setTimeout(() => {
      try { child.kill() } catch {}
      out += `\n[超时] 安装超过 ${Math.round(timeoutMs / 60000)} 分钟，已终止`
    }, timeoutMs)
    child.on('error', (error) => {
      clearTimeout(timer)
      resolve({ code: -1, out: `${out}\nspawn error: ${error.message}` })
    })
    child.on('close', (code) => {
      clearTimeout(timer)
      resolve({ code: code ?? -1, out })
    })
  })
}

export function apply(ctx, config = {}) {
  // 懒解析：apply() 阶段抛异常会让整个 profile 加载失败，而"引擎没装好"应当
  // 只在真正调用工具时报错。这里缓存一次成功的结果。
  let cached = null
  const env = () => {
    if (!cached) {
      const kit = resolveKit(config)
      cached = { kit, script: path.join(kit, 'scripts', 'kit_api.py') }
    }
    return cached
  }
  // 引擎没装好时，别丢一句干巴巴的"找不到路径"——直接告诉调用方下一步该干什么。
  const envOrGuide = () => {
    try {
      return env()
    } catch (error) {
      throw new Error(
        `${error.message}\n\n` +
          '引擎还没装。两种办法：\n' +
          '  A) 调用 video_eyes_setup({ confirm: true, root: "<会话工作区>\\video-kit" }) —— ' +
          '插件自带安装器（ffmpeg 检查 + 独立 venv + faster-whisper + 483MB 模型，约 700MB，一次性，纯本地）；\n' +
          '  B) 若本机已有装好的 video-kit 目录，设环境变量 DSH_VIDEO_EYES_ROOT 指向它后重启应用。\n' +
          '注意：在 DSH 沙箱里 ffmpeg 只能写会话工作区，root 必须落在工作区内。',
      )
    }
  }

  const watchTool = (toolName) => defineTool({
    name: toolName,
    description:
      '看一个视频：输入 B 站链接 / BV 号 / 本地视频路径，返回带时间戳的静帧路径 + 音轨全文转写。' +
      '拿到结果后**必须**用 read_image 逐张查看返回的 frames 路径——那才是真正的画面内容；转写文本是语音内容。' +
      '两者结合才能回答"这视频讲了什么/画面里有什么"。抽帧与转写全部本地完成（ffmpeg + faster-whisper small），不联网、不上传。' +
      '用户给出视频链接、说"看看这个视频/总结这个视频/视频里讲了什么"时使用。',
    // 注意：DSH 的 parameters 是「属性表 DSL」，不是 JSON Schema 外壳。
    // 根上不能写 { type: 'object', properties, required }，否则宿主报
    // "parameters.type must be a value schema object"；必填用属性级 required: true。
    parameters: {
      target: { type: 'string', required: true, description: 'B 站链接、BV 号或本地视频绝对路径' },
      maxFrames: { type: 'number', description: '最多抽多少帧（默认 24，短视频可到 40，长视频建议 16-30）' },
      transcript: { type: 'boolean', description: '是否转写音轨（默认 true）' },
      language: { type: 'string', description: '转写语言（默认 zh）' },
      job: { type: 'string', description: '可选：作业名（默认按时间生成），同名的作业不可重复' },
    },
    output: {
      schema: WATCH_SCHEMA,
      render: (_args, value) => {
        if (!value?.ok) {
          return [{ type: 'text', text: `❌ 抽帧失败：${value?.error || '未知错误'}` }]
        }
        const head = `🎬 ${value.title || value.url || value.job}（${value.duration}s，${value.frame_count} 帧 @ ${value.sample_fps}fps）`
        const lines = [head, `作业目录：${value.job_dir}`, '', '🖼 静帧（用 read_image 逐张查看）：']
        lines.push(...frameLines(value))
        lines.push(...transcriptBlock(value.transcript))
        lines.push('', '➡️ 下一步：read_image 逐张看上面的帧，再结合转写文本回答。')
        return [{ type: 'text', text: lines.join('\n') }]
      },
    },
    timeoutMs: TIMEOUT_MS,
    isConcurrencySafe: () => false, // ffmpeg + whisper 都是 CPU 重的
    presentCall: (args) => ({ card: 'generic', title: `${toolName} ${args.target}`, kind: 'read', rawInput: args }),
    async execute(args, exec) {
      if (typeof args?.target !== 'string' || !args.target.trim()) {
        throw new Error(`${toolName} 需要非空的 target`)
      }
      const { kit, script } = envOrGuide()
      const python = resolvePython(config, kit)
      const job = args.job || defaultJob('v')
      // BV 号自动补成链接（B 站页面两处都能用，但 yt-dlp 更认完整 URL）
      const target = /^BV[0-9A-Za-z]{10}$/.test(args.target.trim())
        ? `https://www.bilibili.com/video/${args.target.trim()}`
        : args.target.trim()
      const cli = ['prepare', '--source', target, '--job', job,
        '--max-frames', String(args.maxFrames || 24),
        '--language', args.language || 'zh', '--srt']
      if (args.transcript === false) cli.push('--no-transcript')
      return runJson(python, script, cli, exec.signal, (m) => console.error(`[video-eyes] ${m}`))
    },
  })

  const transcribeTool = (toolName) => defineTool({
    name: toolName,
    description:
      '只转写一个已抽帧的作业（或给 target 现场抽一帧够用的音频）。返回全文与 SRT 路径。' +
      '已经"看过"画面、只想要语音文字时用它，比重跑 watch 便宜。',
    parameters: {
      job: { type: 'string', description: '已有的作业名（jobs/<job> 且存在 meta.json）' },
      target: { type: 'string', description: '或者给一个视频路径/链接，插件会先抽 1 帧再用它的音轨' },
      language: { type: 'string', description: '转写语言（默认 zh）' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true, properties: { ...TRANSCRIPT_SCHEMA.properties, ok: { type: 'boolean' } } },
      render: (_args, value) => {
        const lines = []
        if (!value?.ok && !value?.text) {
          lines.push(`❌ 转写不可用：${value?.error || value?.status || '未知'}`)
          if (value?.reasons?.length) lines.push(`原因：${value.reasons.join('; ')}`)
          return [{ type: 'text', text: lines.join('\n') }]
        }
        lines.push(`📝 ${value.job}（${value.language}, ${value.n_segments} 段, 覆盖 ${(value.coverage * 100).toFixed(0)}%）`, '')
        lines.push(value.text || '')
        if (value.srt) lines.push('', `SRT：${value.srt}`)
        return [{ type: 'text', text: lines.join('\n') }]
      },
    },
    timeoutMs: TIMEOUT_MS,
    isConcurrencySafe: () => false,
    presentCall: (args) => ({ card: 'generic', title: `${toolName} ${args.job || args.target || ''}`, kind: 'read', rawInput: args }),
    async execute(args, exec) {
      const { kit, script } = envOrGuide()
      const python = resolvePython(config, kit)
      let job = args.job
      if (!job) {
        if (!args.target) throw new Error(`${toolName} 需要 job 或 target`)
        const created = await runJson(python, script, ['frames', '--source', args.target, '--job', defaultJob('a'), '--max-frames', '1'],
          exec.signal, (m) => console.error(`[video-eyes] ${m}`))
        if (!created.ok) throw new Error(created.error || '抽帧失败')
        job = created.job
      }
      const cli = ['transcript', '--job', job, '--language', args.language || 'zh', '--srt']
      return runJson(python, script, cli, exec.signal, (m) => console.error(`[video-eyes] ${m}`))
    },
  })

  // 首次使用引导：装引擎（自带安装器）。必须显式 confirm，因为要下载约 700MB。
  const setupTool = (toolName) => defineTool({
    name: toolName,
    description:
      '安装 video-kit 引擎：检查 Python/ffmpeg → 建独立 venv → 装 faster-whisper → 下 483MB 转写模型 → 合成视频冒烟。' +
      '只在 video_watch 报"引擎未找到"时用。必须带 confirm: true（要下载约 700MB，纯本地，一次性）。' +
      'root 必须是会话工作区内的绝对路径（DSH 沙箱里 ffmpeg 只能写工作区）。',
    parameters: {
      confirm: { type: 'boolean', required: true, description: '确认执行安装（约 700MB 下载）' },
      root: { type: 'string', description: '安装目录，绝对路径，建议 "<会话工作区>\\video-kit"' },
      skipModel: { type: 'boolean', description: '只装依赖与 venv，不下载 483MB 模型（此时只能抽帧、不能转写）' },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: true,
        properties: {
          ok: { type: 'boolean' },
          root: { type: 'string' },
          skipped: { type: 'boolean' },
          log: { type: 'string' },
        },
      },
      render: (_args, value) => {
        if (value?.skipped) {
          return [{ type: 'text', text: `⏸ 未执行安装。${value.log || ''}` }]
        }
        const head = value?.ok ? '✅ 引擎装好了' : '❌ 引擎安装失败'
        return [{ type: 'text', text: `${head}${value?.root ? `（${value.root}）` : ''}\n\n${value?.log || ''}` }]
      },
    },
    timeoutMs: 30 * 60_000,
    isConcurrencySafe: () => false,
    presentCall: (args) => ({ card: 'generic', title: `${toolName}`, kind: 'write', rawInput: args }),
    async execute(args, exec) {
      const root = (args.root || process.env.DSH_VIDEO_EYES_ROOT || '').trim()
      if (!args.confirm) {
        return {
          ok: false, skipped: true,
          log: `需要 confirm: true 才会执行。将下载约 700MB（依赖 ~200MB + 模型 483MB），全部本地。\n` +
            `安装器：${BUNDLED_INSTALLER}\n建议 root：<会话工作区>\\video-kit`,
        }
      }
      if (!root || !path.isAbsolute(root)) {
        throw new Error(`${toolName} 需要一个绝对路径的 root（建议 "<会话工作区>\\video-kit"）`)
      }
      if (!fs.existsSync(BUNDLED_INSTALLER)) {
        throw new Error(`插件里没有找到安装器：${BUNDLED_INSTALLER}（这个包可能被裁过）`)
      }
      const ffmpeg = process.env.DSH_VIDEO_EYES_FFMPEG
      const argv = ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', BUNDLED_INSTALLER, '-Root', root]
      if (args.skipModel) argv.push('-SkipModel')
      if (ffmpeg && fs.existsSync(ffmpeg)) argv.push('-FfmpegPath', ffmpeg)
      if (exec?.signal) exec.signal.addEventListener('abort', () => {}, { once: true })
      const r = await runPowerShell(argv)
      const ok = r.code === 0
      if (ok) {
        // 装完立刻切到这份引擎，后续调用无需重启
        process.env.DSH_VIDEO_EYES_ROOT = root
        cached = null
        try {
          fs.writeFileSync(path.resolve(__dirname, '..', 'engine', '.kit-root'), root + '\n', 'utf8')
        } catch {}
      }
      const log = r.out.trim().split(/\r?\n/).slice(-40).join('\n')
      return { ok, root, skipped: false, log: log || `（无输出，exit ${r.code}）` }
    },
  })

  const probe = (msg) => {
    try {
      fs.appendFileSync(path.join(process.env.TEMP || process.env.TMP || 'C:\\Windows\\Temp', 'vk-probe.log'),
        `[${new Date().toISOString()}] ${msg}\n`)
    } catch {}
  }

  try {
    ctx.tools.register(watchTool(config.toolName || 'video_watch'))
    ctx.tools.register(transcribeTool(config.transcribeToolName || 'video_transcript'))
    ctx.tools.register(setupTool(config.setupToolName || 'video_eyes_setup'))
    probe(`register-OK pid=${process.pid} tools=video_watch,video_transcript,video_eyes_setup`)
  } catch (error) {
    // 注册失败必须留下可查的痕迹：宿主的 console.error 在桌面端基本看不到。
    console.error(`[video-eyes] tool registration skipped: ${error}`)
    probe(`register-FAILED ${String((error && error.message) || error)}`)
  }
}

// --self-test：不依赖 ctx/网络/模型，只验证 schema 与 render 的各种形态。
const isMain = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href

if (isMain && process.argv.includes('--self-test')) {
  const registered = []
  const ctx = { effect: () => {}, tools: { register: (t) => registered.push(t) } }
  apply(ctx, {})
  if (registered.length !== 3) throw new Error(`expected 2 tools, got ${registered.length}`)
  const [watch, tr, setup] = registered
  const setupRequired = setup?.parameters?.required?.includes?.('confirm') || setup?.parameters?.confirm?.required === true
  if (!setupRequired) throw new Error('setup schema broken (confirm 应必填)')
  // 两种形态都要接受：裸跑（fallback）时是属性表 DSL；宿主 defineTool 里已被归一化成
  // JSON Schema（{type:'object', properties, required:['target']}）。这里统一成"target 必填"来断言。
  const declaresRequired = (tool, prop) => {
    const p = tool.parameters
    if (!p) return false
    if (Array.isArray(p.required)) return p.required.includes(prop)
    return p[prop]?.required === true
  }
  const declaresProp = (tool, prop) => {
    const p = tool.parameters
    if (!p) return false
    return Boolean(p[prop] ?? p.properties?.[prop])
  }
  if (!declaresRequired(watch, 'target')) throw new Error('watch schema broken (target 应当必填)')
  if (!declaresProp(tr, 'job')) throw new Error('transcript schema broken (job 缺失)')
  // 说明：parameters 的合法性由宿主 defineTool 自己校验（DSL 写错会直接抛），这里不再重复判断。

  const okRender = watch.output.render({}, {
    ok: true, job: 'v1', job_dir: 'C:/kit/jobs/v1', duration: 12.5, frame_count: 2,
    sample_fps: 0.16, title: 'demo',
    frames: [{ index: 1, timestamp: 3.1, path: 'C:/kit/jobs/v1/frames/f001.jpg' }],
    transcript: { status: 'ok', language: 'zh', n_segments: 3, coverage: 0.98, chars: 12, text: '你好世界' },
  })[0].text
  if (!okRender.includes('f001.jpg')) throw new Error('frame path missing in render')
  if (!okRender.includes('你好世界')) throw new Error('transcript missing in render')

  const noSpeech = watch.output.render({}, {
    ok: true, job: 'v2', job_dir: 'C:/kit/jobs/v2', duration: 3, frame_count: 1, sample_fps: 0.3,
    frames: [], transcript: { status: 'no_speech', reasons: ['segments=0<3'] },
  })[0].text
  if (!noSpeech.includes('no_speech')) throw new Error('no_speech render broken')

  const failed = watch.output.render({}, { ok: false, error: 'ffmpeg 缺失' })[0].text
  if (!failed.includes('ffmpeg 缺失')) throw new Error('failure render broken')

  console.log(`PASS: ${name} --self-test (3 tools, schema + render)`)
}
