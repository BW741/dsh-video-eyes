// 端到端冒烟：不装插件，直接调 apply() 拿到的 tool.execute()，
// 验证 "node → python(kit_api) → ffmpeg" 这条链和 render 的真实输出。
// 用法：node test/smoke.mjs <本地视频路径> [maxFrames]
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
// Windows 上动态 import 必须用 file:// URL，裸的 C:\... 会报 ERR_UNSUPPORTED_ESM_URL_SCHEME
const mod = await import(pathToFileURL(path.join(__dirname, '..', 'dsh', 'index.js')).href)

const target = process.argv[2]
const maxFrames = Number(process.argv[3] || 4)
if (!target) {
  console.error('用法：node test/smoke.mjs <视频路径> [maxFrames]')
  process.exit(2)
}

const tools = []
mod.apply({ effect() {}, tools: { register: (t) => tools.push(t) } }, {})
const [watch, tr] = tools
console.log('registered:', tools.map((t) => t.name).join(', '))

const value = await watch.execute({ target, maxFrames, transcript: false }, {})
console.log('--- value ---')
console.log(JSON.stringify({ ...value, frames: value.frames.slice(0, 2), frames_n: value.frames.length }, null, 2))
console.log('--- render ---')
console.log(watch.output.render({}, value)[0].text)
console.log('--- transcript tool schema ---')
console.log(JSON.stringify(tr.parameters.properties))
if (!value.ok || !value.frames.length) {
  console.error('SMOKE FAIL')
  process.exit(1)
}
console.log('SMOKE OK')
