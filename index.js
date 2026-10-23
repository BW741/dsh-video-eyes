// dsh-video-eyes —— 宿主入口（薄转发）
//
// 真正实现在 ./dsh/index.js。这里只做 re-export，**不重复实现**：
// 0.1.4 曾把整份代码又拷一份到这里，结果宿主走 exports["."] 读到的是旧副本，
// 报了 `parameters.type must be a value schema object`（旧 JSON-Schema 外壳写法）。
// 单一实现 + 薄入口，保证宿主加载到的永远是最新那份。
export { name, inject, apply } from './dsh/index.js'
