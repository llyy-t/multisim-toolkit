/**
 * Worker bridge: 常驻 32 位 Python 进程的 TCP 客户端。
 *
 * 为什么常驻：Multisim Automation API 的 COM 服务器生命周期与第一个 Connect()
 * 的客户端进程绑定——客户端退出，multisim.exe 立刻消失。所以不能每条命令起一个
 * 短命进程。worker 挂在 DSH 会话上，随插件销毁而退出。
 *
 * 为什么 TCP 而不是 stdio：Host 的 subprocess 服务只给非消费式
 * collected.stdout.readFrom(offset)，没有数据到达事件；轮询行缓冲 JSON 容易撕裂。
 * TCP socket 有 data 事件，行协议简单可靠。
 */

import { connect } from 'node:net'
import { randomBytes } from 'node:crypto'
import { existsSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

export const PACKAGE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const WORKER_SCRIPT = join(PACKAGE_ROOT, 'worker', 'msim_worker.py')

/**
 * 去掉值为 undefined 的键。工具结果必须是 lossless JSON，`undefined` 会让
 * Host 在序列化阶段拒绝整个结果（"value is not lossless JSON"）。
 */
function compact(object) {
  const out = {}
  for (const [key, value] of Object.entries(object)) {
    if (value !== undefined) out[key] = value
  }
  return out
}

/** 32 位 Python 候选路径（Multisim COM 服务器是 32 位，必须 32 位解释器）。 */
function pythonCandidates(config) {
  const home = process.env.DSH_HOME || join(homedir(), '.dsh')
  const list = [
    config?.pythonPath,
    process.env.MSIM_PYTHON,
    process.env.MULTISIM_MCP_PYTHON,
    process.env.MULTISIM_MCP_WORKER_PYTHON,
    join(home, 'multisim-mcp', 'py32', 'python.exe'),
    join(home, 'multisim-mcp', 'py32', 'pythonw.exe'),
    'C:\\Python313-32\\python.exe',
    'C:\\Python312-32\\python.exe',
  ]
  return list.filter((item) => typeof item === 'string' && item.length > 0)
}

export function resolvePython(config) {
  for (const candidate of pythonCandidates(config)) {
    try {
      if (existsSync(candidate)) return candidate
    } catch {
      /* ignore */
    }
  }
  return undefined
}

class RequestTimeout extends Error {
  constructor(op, ms) {
    super(`Multisim worker did not answer "${op}" within ${ms} ms`)
    this.name = 'RequestTimeout'
    this.code = 'TIMEOUT'
  }
}

export class WorkerBridge {
  /** @type {any} */
  #ctx
  #config
  #handle = undefined
  #socket = undefined
  #buffer = ''
  #pending = new Map()
  #nextId = 1
  #starting = undefined
  #port = 0
  #token = ''
  #ready = undefined
  #disposed = false
  #lastError = undefined

  constructor(ctx, config) {
    this.#ctx = ctx
    this.#config = config || {}
  }

  get info() {
    // Tool results must be lossless JSON, so undefined fields have to be dropped
    // rather than serialised away silently.
    return compact({
      running: Boolean(this.#socket),
      port: this.#port,
      lastError: this.#lastError,
      script: WORKER_SCRIPT,
      scriptExists: existsSync(WORKER_SCRIPT),
      python: resolvePython(this.#config),
      workerPid: this.#ready?.pid,
      pythonVersion: this.#ready?.python,
      pythonBits: this.#ready?.python_bits,
      multisimMcp: this.#ready?.multisim_mcp,
    })
  }

  /** 确保 worker 活着并已握手；并发调用共享同一次启动。 */
  async ensure() {
    if (this.#disposed) throw new Error('Multisim plugin is shutting down')
    if (this.#socket && this.#ready) return this.#ready
    if (!this.#starting) {
      this.#starting = this.#start().finally(() => {
        this.#starting = undefined
      })
    }
    return this.#starting
  }

  async #start() {
    const python = resolvePython(this.#config)
    if (!python) {
      this.#lastError = 'no 32-bit Python found'
      throw new Error(
        '找不到 32 位 Python 解释器。Multisim 的 COM 服务器是 32 位，必须用 32 位 Python。'
        + '请设置插件 config.pythonPath 或环境变量 MSIM_PYTHON。',
      )
    }
    if (!existsSync(WORKER_SCRIPT)) {
      throw new Error(`worker 脚本缺失：${WORKER_SCRIPT}`)
    }

    const token = randomBytes(16).toString('hex')
    this.#token = token

    let handle
    try {
      handle = this.#ctx.subprocess.spawn({
        argv: [python, WORKER_SCRIPT, '--port', '0', '--token', token, '--parent-pid', String(process.pid)],
        cwd: PACKAGE_ROOT,
        stdio: {
          stdin: 'ignore',
          stdout: { maxBytes: 2 * 1024 * 1024 },
          stderr: { maxBytes: 8 * 1024 * 1024 },
        },
        graceMs: 5000,
      })
    } catch (error) {
      this.#lastError = String(error?.message ?? error)
      throw new Error(`无法启动 Multisim worker：${this.#lastError}`)
    }
    this.#handle = handle

    handle.done
      .then((outcome) => {
        this.#lastError = `worker exited (code=${outcome?.exitCode}, signal=${outcome?.signal})`
        this.#teardown()
      })
      .catch(() => {
        this.#teardown()
      })

    const line = await this.#awaitReady()
    let ready
    try {
      ready = JSON.parse(line)
    } catch (error) {
      this.#kill()
      throw new Error(`worker 就绪行不是合法 JSON：${line.slice(0, 500)}`)
    }
    if (!ready?.port) {
      this.#kill()
      throw new Error(`worker 就绪行缺少 port：${line.slice(0, 500)}`)
    }
    if (ready.python_bits !== 32) {
      this.#kill()
      throw new Error(
        `worker 跑在 ${ready.python_bits} 位 Python 上，Multisim COM 需要 32 位。`
        + '请把 config.pythonPath 指向 32 位解释器。',
      )
    }

    this.#port = ready.port
    await this.#connectSocket(ready.host || '127.0.0.1', ready.port)

    const hello = await this.#request('hello', { token }, 15000)
    this.#ready = { ...ready, hello }
    this.#lastError = undefined
    return this.#ready
  }

  /** 轮询 stdout 收集器，等首行 JSON（含 event:"ready"）。 */
  async #awaitReady() {
    const timeoutMs = Number(this.#config.startTimeoutMs ?? 30000)
    const started = Date.now()
    let offset = 0
    for (;;) {
      const handle = this.#handle
      if (!handle) throw new Error('worker handle 丢失')
      const read = handle.collected?.stdout?.readFrom(offset)
      if (read) {
        offset = read.nextOffset ?? offset
        const text = String(read.text ?? '')
        if (text.trim()) {
          const line = text.split(/\r?\n/).find((entry) => entry.trim().length > 0)
          if (line) return line.trim()
        }
      }
      if (Date.now() - started > timeoutMs) {
        const stderr = String(handle.collected?.stderr?.readFrom(0)?.text ?? '')
        this.#kill()
        throw new Error(
          `Multisim worker 在 ${timeoutMs} ms 内没有就绪。\n`
          + `stderr: ${stderr.slice(-3000) || '(empty)'}`,
        )
      }
      await new Promise((r) => setTimeout(r, 120))
    }
  }

  #connectSocket(host, port) {
    return new Promise((resolvePromise, rejectPromise) => {
      const socket = connect({ host, port })
      const timer = setTimeout(() => {
        socket.destroy()
        rejectPromise(new Error(`连接 worker ${host}:${port} 超时`))
      }, 15000)
      socket.setEncoding('utf8')
      socket.setNoDelay(true)
      socket.once('connect', () => {
        clearTimeout(timer)
        this.#socket = socket
        socket.on('data', (chunk) => this.#onData(chunk))
        socket.on('error', (error) => {
          this.#lastError = String(error?.message ?? error)
          this.#failAll(error)
        })
        socket.on('close', () => {
          this.#teardown()
        })
        resolvePromise(undefined)
      })
      socket.once('error', (error) => {
        clearTimeout(timer)
        rejectPromise(error)
      })
    })
  }

  #onData(chunk) {
    this.#buffer += chunk
    for (;;) {
      const index = this.#buffer.indexOf('\n')
      if (index < 0) {
        if (this.#buffer.length > 64 * 1024 * 1024) {
          this.#buffer = ''
          this.#kill()
        }
        return
      }
      const line = this.#buffer.slice(0, index).trim()
      this.#buffer = this.#buffer.slice(index + 1)
      if (!line) continue
      let message
      try {
        message = JSON.parse(line)
      } catch {
        continue
      }
      const pending = this.#pending.get(message?.id)
      if (!pending) continue
      this.#pending.delete(message.id)
      clearTimeout(pending.timer)
      if (message.ok) pending.resolve(message.result)
      else {
        const error = new Error(message?.error?.message || 'Multisim worker error')
        error.code = message?.error?.code || 'INTERNAL'
        error.detail = message?.error?.detail
        pending.reject(error)
      }
    }
  }

  #request(op, args, timeoutMs) {
    return new Promise((resolvePromise, rejectPromise) => {
      const socket = this.#socket
      if (!socket) {
        rejectPromise(new Error('Multisim worker 未运行'))
        return
      }
      const id = this.#nextId++
      const timer = setTimeout(() => {
        this.#pending.delete(id)
        rejectPromise(new RequestTimeout(op, timeoutMs))
      }, timeoutMs)
      this.#pending.set(id, { resolve: resolvePromise, reject: rejectPromise, timer, op })
      const payload = JSON.stringify({ id, op, args: args ?? {} }) + '\n'
      socket.write(payload)
    })
  }

  /** 调一个 op。返回 result；失败抛带 code 的 Error。 */
  async call(op, args, timeoutMs = 120000) {
    await this.ensure()
    return this.#request(op, args ?? {}, timeoutMs)
  }

  #failAll(error) {
    for (const [id, pending] of this.#pending) {
      clearTimeout(pending.timer)
      pending.reject(error)
      this.#pending.delete(id)
    }
  }

  #teardown() {
    this.#failAll(new Error('Multisim worker 连接已断开'))
    this.#socket = undefined
    this.#ready = undefined
  }

  #kill() {
    try {
      this.#handle?.terminate()
    } catch {
      /* ignore */
    }
    this.#handle = undefined
    this.#teardown()
  }

  /** 主动停止（插件销毁时）。先尝试优雅 shutdown，再杀进程。 */
  async dispose() {
    this.#disposed = true
    const handle = this.#handle
    if (this.#socket) {
      try {
        await this.#request('com.shutdown', {}, 8000)
      } catch {
        /* 忽略：Multisim 可能已经不在了 */
      }
    }
    try {
      handle?.terminate()
    } catch {
      /* ignore */
    }
    this.#kill()
    this.#pending.clear()
  }
}

export function describeWorkerError(error) {
  const code = error?.code
  const detail = error?.detail
  const message = String(error?.message ?? error)
  if (!detail) return message
  const text = String(detail)
  // COM_ERROR already carries the Multisim error text in the message; the
  // detail is a Python traceback that only restates it. Keep such noise out of
  // the model's context unless the message itself is uninformative.
  if (code === 'COM_ERROR' && text.startsWith('Traceback')) return message
  return `${message}\n\n${text.slice(-2000)}`
}

export { WORKER_SCRIPT }
