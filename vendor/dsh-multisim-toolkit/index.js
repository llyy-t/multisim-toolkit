/**
 * dsh-multisim-toolkit — 让 DSH 直接操控本机 Multisim 14.3。
 *
 * 架构（细节见 worker/PROTOCOL.md）：
 *   Host 插件(本文件) --TCP/JSON行--> 常驻 32 位 Python worker --> Multisim
 *
 * 两条互不可及的通道，同一个 worker 里共存：
 *   - COM 通道（com.*）：多起一个**隐藏的** Multisim 实例，用来建模/查询/仿真/导图。
 *   - GUI 通道（gui.*）：操作**可见的** multisim.exe 窗口，用来截图、打开网表、另存为。
 *
 * worker 必须常驻：Multisim Automation 的 COM 服务器与发起 Connect() 的客户端进程同
 * 生命周期，客户端一退出 multisim.exe 就消失，所以不能每条命令起一个短命进程。
 */

import { readFile } from 'node:fs/promises'

import { WorkerBridge, describeWorkerError, resolvePython } from './lib/worker.js'

export const name = 'multisim-toolkit'

/**
 * The worker is started through the host `subprocess` service, so apply must not
 * run before that service exists. Tools and the prompt section are still injected
 * lazily inside apply, because they may appear later than the plugin itself.
 */
export const inject = ['subprocess']

/** 把 worker 抛出的错误变成一句自解释的工具失败信息。 */
function workerFailure(op, error) {
  const code = error?.code ? `[${error.code}] ` : ''
  throw new Error(`msim ${op} failed: ${code}${describeWorkerError(error)}`)
}

/**
 * 读图 → 走 attachments 落盘 → 记在 exec 上，供 projectContent 投影成 image 块。
 * 任何一步失败都静默放弃（结果是文本，工具不失败）。
 */
async function stageImage(ctx, exec, store, filePath, caption) {
  if (!filePath) return
  let data
  try {
    data = await readFile(filePath)
  } catch {
    return
  }
  if (data.length === 0) return
  const attachments = ctx.get('attachments')
  if (!attachments) return
  const routed = exec.agent?.session?.requestHeader?.()?.config
  const provider = routed?.provider ?? exec.agent?.options?.provider
  const model = routed?.model ?? exec.agent?.options?.model
  const llm = ctx.get('llm')
  if (provider === undefined || model === undefined || llm === undefined) return
  let info
  try {
    info = await llm.resolveModelInfo(provider, model, exec.signal)
  } catch {
    return
  }
  if (!Array.isArray(info?.inputModalities) || !info.inputModalities.includes('image')) return
  if (exec.signal?.aborted) return
  let ref
  try {
    ;[ref] = await attachments.saveImages([{ data, mediaType: 'image/png' }])
  } catch {
    return
  }
  if (!ref) return
  const staged = store.get(exec) ?? { items: [] }
  staged.items.push({ ref, caption })
  store.set(exec, staged)
}

/** 记住本次调用的参数，供 projectContent 复算文字部分。 */
function rememberArgs(store, exec, args) {
  const staged = store.get(exec) ?? { items: [] }
  staged.args = args
  store.set(exec, staged)
}

/**
 * 生成 projectContent：投影一旦返回就**替换**掉默认的 render 文本，
 * 所以这里必须自己把文字重新渲染一遍，再接上图片块。
 */
function imageProjection(store, renderText) {
  return (exec, result) => {
    const staged = store.get(exec)
    if (!staged) return undefined
    store.delete(exec)
    const items = staged.items ?? []
    if (items.length === 0) return undefined
    const value = result?.value ?? result
    const blocks = []
    try {
      const lines = renderText(staged.args ?? {}, value)
      const text = lines.map((block) => block?.text ?? '').join('\n')
      if (text.trim()) blocks.push({ type: 'text', text })
    } catch {
      // 渲染失败不阻塞图片
    }
    for (const item of items) {
      if (item.caption) blocks.push({ type: 'text', text: item.caption })
      blocks.push({ type: 'image', attachment: item.ref })
    }
    return blocks
  }
}

/* ------------------------------------------------------------------------- */
/* 渲染辅助                                                                    */
/* ------------------------------------------------------------------------- */

function fmt(value) {
  if (value === undefined || value === null) return ''
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return JSON.stringify(value)
}

/** 对象渲染成 "key: value" 行。 */
function kvLines(value, indent = '') {
  if (!value || typeof value !== 'object') return [`${indent}${fmt(value)}`]
  const out = []
  for (const [key, item] of Object.entries(value)) {
    if (item === undefined) continue
    if (Array.isArray(item)) out.push(`${indent}${key}: [${item.map(fmt).join(', ')}]`)
    else if (item && typeof item === 'object') out.push(`${indent}${key}: ${JSON.stringify(item)}`)
    else out.push(`${indent}${key}: ${fmt(item)}`)
  }
  return out
}

/** 采样数据表：只给形状 + 前几行，避免把几万个点塞进上下文。 */
function renderRows(entry, rows = 4) {
  if (!entry || typeof entry !== 'object') return [fmt(entry)]
  const head = [
    `output ${fmt(entry.output)}: ${fmt(entry.n_points)} pts, shape ${JSON.stringify(entry.shape)}, ` +
      `interpolation ${fmt(entry.method)}`,
  ]
  const data = Array.isArray(entry.rows) ? entry.rows : []
  for (const row of data.slice(0, rows)) {
    head.push(`  ${Array.isArray(row) ? row.map((cell) => fmt(cell)).join('\t') : fmt(row)}`)
  }
  if (data.length > rows) head.push(`  … ${data.length - rows} more rows (raw JSON in structuredContent)`)
  return head
}

/* ------------------------------------------------------------------------- */
/* 插件本体                                                                    */
/* ------------------------------------------------------------------------- */

/** msim_export 的文字部分（projectContent 投影图片时要复算，故独立成函数）。 */
function renderExport(args, value) {
  const lines = [`msim_export ${args?.kind}`]
  lines.push(`  path: ${fmt(value?.path)}`)
  if (value?.bytes !== undefined) lines.push(`  bytes: ${fmt(value.bytes)}`)
  if (value?.width !== undefined) lines.push(`  size: ${value.width}x${value.height}`)
  if (value?.format !== undefined) lines.push(`  format: ${fmt(value.format)}`)
  if (typeof args?.path === 'string' && typeof value?.path === 'string' && args.path !== value.path) {
    lines.push(`  note: Multisim wrote "${value.path}" instead of the requested name`)
  }
  if (typeof value?.text === 'string' && value.text.length > 0) {
    const trimmed = value.text.length > 4000 ? `${value.text.slice(0, 4000)}\n… truncated` : value.text
    lines.push('  ---', trimmed)
  }
  return [{ type: 'text', text: lines.join('\n') }]
}

/** msim_gui 的文字部分。 */
function renderGui(args, value) {
  const lines = [`msim_gui ${args?.action ?? value?.action}`]
  if (Array.isArray(value?.items)) {
    lines.push(`  ${value.items.length} window(s)`)
    for (const item of value.items.slice(0, 12)) {
      lines.push(`  - [${fmt(item.hwnd)}] ${fmt(item.class)} "${fmt(item.title)}" ${fmt(item.rect)}`)
    }
  }
  const rest = { ...(value ?? {}) }
  delete rest.items
  delete rest.action
  lines.push(...kvLines(rest, '  ').filter((line) => line.trim() !== ''))
  return [{ type: 'text', text: lines.join('\n') }]
}

/** msim_spice 的文字部分。 */
function renderSpice(args, value) {
  if (value?.action === 'verify' || args?.action === 'verify') {
    const lines = [`msim_spice verify — ${fmt(value?.lines)} line(s), ${(value?.elements ?? []).length} element(s)`]
    for (const element of (value?.elements ?? []).slice(0, 20)) {
      lines.push(`  ${fmt(element.designator)}: ${fmt(element.line)}`)
    }
    if ((value?.elements ?? []).length > 20) lines.push(`  … ${value.elements.length - 20} more`)
    lines.push(`  commands: ${(value?.commands ?? []).join('; ')}`)
    for (const note of value?.notes ?? []) lines.push(`  note: ${note}`)
    return [{ type: 'text', text: lines.join('\n') }]
  }
  const lines = [`msim_spice run — ${(value?.analyses ?? []).length} analysis result(s)`]
  if (value?.elapsed_seconds !== undefined) lines.push(`  elapsed: ${fmt(value.elapsed_seconds)}s`)
  if (value?.directory) lines.push(`  files: ${fmt(value.directory)}`)
  if (value?.created_circuit) lines.push('  created a blank document for the SPICE engine to run in')
  if ((value?.missing ?? []).length > 0) lines.push(`  missing: ${value.missing.join('; ')}`)
  for (const analysis of value?.analyses ?? []) {
    lines.push('  ---')
    lines.push(`  ${fmt(analysis.command)} → ${fmt(analysis.plotname)}: ${fmt(analysis.n_points)} pts, `
      + `${(analysis.columns ?? []).length} columns`)
    lines.push(`  columns: ${(analysis.columns ?? []).join(', ')}`)
    const rows = Array.isArray(analysis.rows) ? analysis.rows : []
    for (const row of rows.slice(0, 6)) {
      lines.push(`    ${Array.isArray(row) ? row.map((cell) => fmt(cell)).join('\t') : fmt(row)}`)
    }
    if (rows.length > 6) lines.push(`    … ${rows.length - 6} more rows in structuredContent`)
    if (analysis.csv?.path) lines.push(`  csv: ${analysis.csv.path}`)
    if (analysis.png?.path) lines.push(`  png: ${analysis.png.path}`)
  }
  if (lines.length === 1) lines.push('  (no analyses ran)')
  return [{ type: 'text', text: lines.join('\n') }]
}

function renderDesign(args, value) {
  const name = String(value?.path ?? '').split(/[\\/]/).pop()
  const lines = [`msim_design read — ${fmt(name)}`]
  if (value?.compressed_bytes !== undefined) {
    lines.push(`  container: ${fmt(value.blocks)} PKWare block(s), ${fmt(value.compressed_bytes)} -> `
      + `${fmt(value.decoded_bytes)} bytes`)
  }
  if (value?.root) lines.push(`  xml: ${fmt(value.root)} schema ${fmt(value.schema)}`)
  const creator = value?.creator ?? {}
  if (creator.application || creator.versionMajor) {
    lines.push(`  created by: ${fmt(creator.application)} ${fmt(creator.versionMajor)}.${fmt(creator.versionMinor)}`
      + `.${fmt(creator.versionPatch)}${creator.date ? ` at ${fmt(creator.date)}` : ''}`)
  }
  const counts = value?.counts ?? {}
  if (Object.keys(counts).length > 0) {
    lines.push(`  counts: ${Object.entries(counts).map(([key, item]) => `${key}=${item}`).join(', ')}`)
  }
  const components = Array.isArray(value?.components) ? value.components : []
  lines.push(`  components (${components.length}):`)
  for (const component of components.slice(0, 40)) {
    const kind = component?.kind ? ` [${component.kind}]` : ''
    const valueText = component?.value ? ` = ${component.value}` : ''
    const sections = Number(component?.sections) > 1 ? ` (${component.sections} sections)` : ''
    const settings = component?.settings ? Object.keys(component.settings).length : 0
    lines.push(`    ${fmt(component?.refdes)}${valueText}${sections}${kind}${settings ? ` (${settings} setting(s))` : ''}`)
  }
  if (components.length > 40) lines.push(`    … ${components.length - 40} more`)
  const instruments = Array.isArray(value?.instruments) ? value.instruments : []
  if (instruments.length > 0) {
    lines.push(`  instruments (${instruments.length}):`
      + ` ${instruments.map((item) => `${item.refdes}${item.kind ? `=${item.kind}` : ''}`).join(', ')}`)
  }
  if (components.length === 0) lines.push('  this design holds no components (it is a blank circuit)')
  if (typeof value?.xml === 'string') {
    lines.push(`  xml text: ${fmt(value.xml_chars)} chars${value.xml_truncated ? ', truncated' : ''}`)
  }
  return [{ type: 'text', text: lines.join('\n') }]
}

export function apply(ctx, config = {}) {
  const bridge = new WorkerBridge(ctx, config)
  /** 每个工具一张暂存表：execute 里存下已落盘的图，projectContent 里取走。 */
  const exportImages = new WeakMap()
  const guiImages = new WeakMap()

  const call = async (op, args, timeoutMs) => {
    try {
      return await bridge.call(op, args, timeoutMs)
    } catch (error) {
      return workerFailure(op, error)
    }
  }

  /**
   * 分析调用专用：Multisim 对"输出名不存在"只回一个笼统的 HRESULT，
   * 把真实原因（LastErrorMessage = 输出未找到。）翻译成可执行的下一步。
   */
  const analysisCall = async (op, payload, timeoutMs) => {
    try {
      return await call(op, payload, timeoutMs)
    } catch (error) {
      const text = String(error?.message ?? '')
      if (text.includes('输出未找到') || text.includes('output not found')) {
        throw new Error(
          `${text}\n\n`
            + 'That design exposes no simulation output with this name. Run msim_inspect what="outputs" to get the '
            + 'names the analyses accept. A design without probes or instruments has an empty output list, and no '
            + 'analysis can produce a trace from it — add a probe or an instrument first.',
        )
      }
      throw error
    }
  }

  /* ---- 1. 环境与连接 -------------------------------------------------- */

  const statusTool = {
    name: 'msim_status',
    description:
      'Report whether the local Multisim 14.3 toolchain is usable, and wire up the automation session on demand. '
      + 'Call this first when a Multisim task starts, or when any other msim tool reports that Multisim is missing. '
      + 'It starts the resident 32-bit worker if needed and reports: the Python interpreter in use, whether the '
      + 'Multisim Automation (COM) server connected, its version, and which visible multisim.exe windows exist. '
      + 'It never opens or edits a circuit.',
    parameters: {
      type: 'object',
      properties: {
        connect: {
          type: 'boolean',
          description:
            'Also bring up the automation (COM) session. Multisim launches as a hidden extra instance; this takes '
            + 'several seconds the first time. Default false.',
          default: false,
        },
      },
      required: [],
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          worker: { type: 'object' },
          com: { type: 'object' },
          gui: { type: 'object' },
          python: { type: 'string' },
        },
        required: ['worker'],
        additionalProperties: true,
      },
      render(_args, value) {
        const lines = ['Multisim toolkit status']
        lines.push(...kvLines(value.worker, '  worker '))
        if (value.python) lines.push(`  python: ${value.python}`)
        lines.push(...kvLines(value.com, '  com '))
        if (value.gui) {
          lines.push(`  gui exe exists: ${fmt(value.gui.exe_exists)}`)
          lines.push(`  gui running pids: ${JSON.stringify(value.gui.running_pids ?? [])}`)
          if (value.gui.main_window) {
            const win = value.gui.main_window
            lines.push(`  gui main window: "${fmt(win.title)}" ${fmt(win.w)}x${fmt(win.h)}`)
          }
        }
        return [{ type: 'text', text: lines.join('\n') }]
      },
    },
    timeoutMs: 120000,
    async execute(args) {
      const payload = {}
      if (args?.connect) {
        try {
          payload.com = await bridge.call('com.connect', {}, 120000)
        } catch (error) {
          payload.com = { connected: false, error: describeWorkerError(error) }
        }
      } else {
        try {
          payload.com = await bridge.call('com.ping', {}, 30000)
        } catch (error) {
          payload.com = { connected: false, error: describeWorkerError(error) }
        }
      }
      try {
        payload.gui = await bridge.call('gui.probe', {}, 60000)
      } catch (error) {
        payload.gui = { error: describeWorkerError(error) }
      }
      // Read worker info last: the calls above are what start the worker.
      payload.worker = bridge.info
      payload.python = resolvePython(config) ?? '(not found)'
      return payload
    },
  }

  /* ---- 2. 电路文件 ---------------------------------------------------- */

  const circuitTool = {
    name: 'msim_circuit',
    description:
      'Open, create, save, or describe the circuit file the automation session is editing. '
      + 'Exactly one circuit is current at a time; every other msim_* tool reads or writes that circuit. '
      + 'There is no "close" action: opening another file replaces the current circuit.',
    parameters: {
      type: 'object',
      properties: {
        action: {
          type: 'string',
          enum: ['new', 'open', 'save', 'save_as', 'info'],
          description:
            'new: create an empty untitled circuit. open: load a design file (see path). save: write back to the '
            + 'current file; fails when the circuit was never named. save_as: write to path and make it current. '
            + 'info: report name, file, simulation state and last error.',
        },
        path: {
          type: 'string',
          description:
            'Absolute path. Required for open and save_as. .ms14/.msm/.ewb/.ms11/.ms12/.ms13 load through the '
            + 'automation server. Any other extension (notably SPICE .cir/.net netlists) is routed to a visible '
            + 'Multisim window instead, because the automation server crashes on them — that window is a separate '
            + 'process, so the automation session keeps its own circuit.',
        },
      },
      required: ['action'],
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          action: { type: 'string' },
          route: { type: 'string' },
          circuit: { type: 'object' },
          path: { type: 'string' },
        },
        required: ['action'],
        additionalProperties: true,
      },
      render(args, value) {
        const lines = [`msim_circuit ${args?.action}`]
        if (value.route) lines.push(`  route: ${value.route}`)
        if (value.path) lines.push(`  path: ${value.path}`)
        lines.push(...kvLines(value.circuit ?? value.result, '  '))
        return [{ type: 'text', text: lines.join('\n') }]
      },
    },
    timeoutMs: 180000,
    isConcurrencySafe: () => false,
    async execute(args) {
      const action = String(args?.action ?? '')
      if (action === 'new') return { action, circuit: await call('com.new_circuit', {}, 120000) }
      if (action === 'info') return { action, circuit: await call('com.circuit_info', {}, 60000) }
      if (action === 'save') return { action, circuit: await call('com.save', {}, 180000) }
      if (action === 'save_as') {
        const path = String(args?.path ?? '').trim()
        if (!path) throw new Error('msim_circuit save_as requires path')
        return { action, path, circuit: await call('com.save_as', { path }, 180000) }
      }
      if (action === 'open') {
        const path = String(args?.path ?? '').trim()
        if (!path) throw new Error('msim_circuit open requires path')
        const result = await call('open_any', { path }, 180000)
        if (result?.route === 'com') return { action, route: 'com', path: result.path, circuit: result.circuit }
        return { action, route: 'gui', path: result.path, circuit: result.gui }
      }
      throw new Error(`msim_circuit: unknown action ${JSON.stringify(action)}`)
    },
  }

  /* ---- 3. 读取电路 ---------------------------------------------------- */

  const inspectTool = {
    name: 'msim_inspect',
    description:
      'Read the current circuit: its components, simulation inputs/outputs, variants, sections, circuit '
      + 'parameters, or one component/parameter value. Read-only. '
      + 'Most designs expose no simulation outputs until probes or instruments are placed, so an empty output '
      + 'list is normal and is not an error.',
    parameters: {
      type: 'object',
      properties: {
        what: {
          type: 'string',
          enum: [
            'components',
            'inputs',
            'outputs',
            'variants',
            'active_variant',
            'sections',
            'parameters',
            'parameter',
            'rlc',
            'info',
          ],
          description:
            'components: part names. inputs/outputs: names accepted by the analysis tools. variants and '
            + 'active_variant: design variants. sections: sections of one component (needs component). '
            + 'parameters: circuit parameter names. parameter: one value by name (needs name). rlc: the value of '
            + 'one R/L/C (needs component). info: circuit name/file/simulation state.',
        },
        component: {
          type: 'string',
          description: 'Component reference designator such as "R1" or "U1A". Required for sections and rlc.',
        },
        name: {
          type: 'string',
          description: 'Circuit parameter name. Required for what="parameter".',
        },
        io_type: {
          type: 'integer',
          description: 'Filter for inputs/outputs: 0 all, 1 voltage, 2 current, 3 power, 4 digital. Default 0.',
          enum: [0, 1, 2, 3, 4],
          default: 0,
        },
        component_type: {
          type: 'integer',
          description: 'Filter for components: 0 all, 1 all R/L/C, 2 resistor, 3 capacitor, 4 inductor. Default 0.',
          enum: [0, 1, 2, 3, 4],
          default: 0,
        },
        level: {
          type: 'integer',
          description: 'Scope for what="parameters": 0 all levels, 1 top level only. Default 1.',
          enum: [0, 1],
          default: 1,
        },
      },
      required: ['what'],
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          what: { type: 'string' },
          count: { type: 'integer' },
          items: { type: 'array', items: { type: 'string' } },
          value: {},
        },
        required: ['what'],
        additionalProperties: true,
      },
      render(args, value) {
        const what = args?.what ?? '?'
        if (Array.isArray(value.items)) {
          const body = value.items.length === 0 ? '  (empty)' : value.items.map((item) => `  ${fmt(item)}`).join('\n')
          const lines = [`${what} (${value.items.length}):`, body]
          if (what === 'outputs' && value.items.length === 0) {
            lines.push(
              '  no analysis can produce a trace from this design: Multisim only exposes outputs that a probe or '
                + 'an instrument writes. Place one, or open a design that already has them.',
            )
          }
          return [{ type: 'text', text: lines.join('\n') }]
        }
        return [{ type: 'text', text: `${what}:\n${kvLines(value, '  ').join('\n')}` }]
      },
    },
    timeoutMs: 90000,
    async execute(args) {
      const what = String(args?.what ?? '')
      const ioType = Number.isInteger(args?.io_type) ? args.io_type : 0
      const compType = Number.isInteger(args?.component_type) ? args.component_type : 0
      const level = Number.isInteger(args?.level) ? args.level : 1
      const component = String(args?.component ?? '').trim()
      const name = String(args?.name ?? '').trim()
      let result
      switch (what) {
        case 'components':
          result = await call('com.enum_components', { component_type: compType }, 60000)
          break
        case 'inputs':
          result = await call('com.enum_inputs', { io_type: ioType }, 60000)
          break
        case 'outputs':
          result = await call('com.enum_outputs', { io_type: ioType }, 60000)
          break
        case 'variants':
          result = await call('com.enum_variants', {}, 60000)
          break
        case 'active_variant':
          result = await call('com.get_active_variant', {}, 60000)
          break
        case 'sections':
          if (!component) throw new Error('msim_inspect sections requires component')
          result = await call('com.enum_sections', { component }, 60000)
          break
        case 'parameters':
          result = await call('com.enum_parameters', { level }, 60000)
          break
        case 'parameter':
          if (!name) throw new Error('msim_inspect parameter requires name')
          result = await call('com.get_parameter', { name }, 60000)
          break
        case 'rlc':
          if (!component) throw new Error('msim_inspect rlc requires component')
          result = await call('com.get_rlc', { component }, 60000)
          break
        case 'info':
          result = await call('com.circuit_info', {}, 60000)
          break
        default:
          throw new Error(`msim_inspect: unknown what ${JSON.stringify(what)}`)
      }
      return { what, ...(result ?? {}) }
    },
  }

  /* ---- 4. 修改电路 ---------------------------------------------------- */

  const editTool = {
    name: 'msim_edit',
    description:
      'Change component values, circuit parameters, the active variant, or swap a component for another library '
      + 'part in the current circuit. Changes are in memory until msim_circuit save or save_as runs.',
    parameters: {
      type: 'object',
      properties: {
        action: {
          type: 'string',
          enum: ['set_rlc', 'set_parameter', 'set_variant', 'replace_component'],
          description:
            'set_rlc: set one resistor/capacitor/inductor value. set_parameter: set one circuit parameter. '
            + 'set_variant: make a variant active. replace_component: swap a placed part for another library part.',
        },
        component: {
          type: 'string',
          description: 'Reference designator, for set_rlc and replace_component.',
        },
        value: {
          type: 'number',
          description: 'New numeric value in SI base units (ohms, farads, henries), for set_rlc and set_parameter.',
        },
        name: {
          type: 'string',
          description: 'Circuit parameter name for set_parameter, or new variant name for set_variant.',
        },
        section: {
          type: 'string',
          description: 'Section to replace within the component, for replace_component. Use msim_inspect sections first.',
        },
        database: {
          type: 'integer',
          enum: [0, 1, 2],
          description: 'Library database for replace_component: 0 master, 1 corporate, 2 user. Default 0.',
          default: 0,
        },
        group: { type: 'string', description: 'Library group for replace_component, e.g. "TTL".' },
        family: { type: 'string', description: 'Library family for replace_component.' },
        part: { type: 'string', description: 'Library part name for replace_component, e.g. "74LS86".' },
        model: { type: 'string', description: 'Model name for replace_component; empty string when the part has none.' },
      },
      required: ['action'],
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: { action: { type: 'string' } },
        required: ['action'],
        additionalProperties: true,
      },
      render(args, value) {
        return [{ type: 'text', text: `msim_edit ${args?.action}\n${kvLines(value, '  ').join('\n')}` }]
      },
    },
    timeoutMs: 120000,
    isConcurrencySafe: () => false,
    async execute(args) {
      const action = String(args?.action ?? '')
      if (action === 'set_rlc') {
        const component = String(args?.component ?? '').trim()
        if (!component) throw new Error('msim_edit set_rlc requires component')
        const value = Number(args?.value)
        if (!Number.isFinite(value)) throw new Error('msim_edit set_rlc requires a finite numeric value')
        return { action, ...(await call('com.set_rlc', { component, value }, 90000)) }
      }
      if (action === 'set_parameter') {
        const parameterName = String(args?.name ?? '').trim()
        if (!parameterName) throw new Error('msim_edit set_parameter requires name')
        const value = Number(args?.value)
        if (!Number.isFinite(value)) throw new Error('msim_edit set_parameter requires a finite numeric value')
        return { action, ...(await call('com.set_parameter', { name: parameterName, value }, 90000)) }
      }
      if (action === 'set_variant') {
        const variant = String(args?.name ?? '').trim()
        if (!variant) throw new Error('msim_edit set_variant requires name')
        return { action, ...(await call('com.set_active_variant', { name: variant }, 90000)) }
      }
      if (action === 'replace_component') {
        const component = String(args?.component ?? '').trim()
        const section = String(args?.section ?? '').trim()
        if (!component || !section) throw new Error('msim_edit replace_component requires component and section')
        return {
          action,
          ...(await call(
            'com.replace_component',
            {
              component,
              section,
              database: args?.database ?? 0,
              group: String(args?.group ?? ''),
              family: String(args?.family ?? ''),
              name: String(args?.part ?? ''),
              model: String(args?.model ?? ''),
            },
            120000,
          )),
        }
      }
      throw new Error(`msim_edit: unknown action ${JSON.stringify(action)}`)
    },
  }

  /* ---- 5. 仿真分析 ---------------------------------------------------- */

  const analysisTool = {
    name: 'msim_analyze',
    description:
      'Run a simulation analysis on the current circuit and return the collected traces. '
      + 'Also reports or controls the current simulation state. '
      + 'Analyses need named outputs: ask msim_inspect what="outputs" first. Many designs have no probe or '
      + 'instrument and therefore expose no outputs — then an analysis can only time out, so place probes or use '
      + 'a design that already has them. Traces are sampled down to max_points.',
    parameters: {
      type: 'object',
      properties: {
        analysis: {
          type: 'string',
          enum: ['transient', 'dc', 'ac_sweep', 'ac_single', 'command_file', 'state', 'stop', 'pause', 'resume'],
          description:
            'transient: time domain. dc: DC operating point. ac_sweep: swept AC. ac_single: one AC frequency. '
            + 'command_file: run a Multisim analysis command file and return its log. '
            + 'state/stop/pause/resume: query or control the running simulation.',
        },
        outputs: {
          type: 'array',
          items: { type: 'string' },
          description: 'Output names to measure, from msim_inspect what="outputs". Required for every analysis.',
        },
        duration: {
          type: 'number',
          description: 'Transient stop time in seconds. Default 0.001.',
        },
        sample_rate: {
          type: 'number',
          description: 'Transient sample rate in Hz. Default 1000000.',
        },
        num_samples: {
          type: 'integer',
          description: 'Transient sample count. Default 1000.',
        },
        repeat: {
          type: 'boolean',
          description: 'Keep the output request repeating across runs. Default false.',
          default: false,
        },
        sweep_type: {
          type: 'integer',
          enum: [0, 1, 2],
          description: 'AC sweep spacing: 0 decade, 1 octave, 2 linear. Default 0.',
          default: 0,
        },
        num_points: {
          type: 'integer',
          description: 'AC sweep point count. Default 10.',
        },
        start: { type: 'number', description: 'AC sweep start frequency in Hz. Default 100.' },
        stop: { type: 'number', description: 'AC sweep stop frequency in Hz. Default 1000000.' },
        frequency: { type: 'number', description: 'Single AC frequency in Hz. Default 1000.' },
        command_file: { type: 'string', description: 'Absolute path to a Multisim command file, for command_file.' },
        log_file: { type: 'string', description: 'Absolute path for the command file log, for command_file.' },
        timeout: {
          type: 'number',
          description: 'Seconds to wait for the analysis before stopping it. Default 60, use 120+ for sweeps.',
        },
        max_points: {
          type: 'integer',
          description: 'Upper bound on returned points per trace. Default 2000.',
        },
      },
      required: ['analysis'],
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          analysis: { type: 'string' },
          outputs: { type: 'object' },
          state: { type: 'integer' },
          timed_out: { type: 'boolean' },
          missing_outputs: { type: 'array', items: { type: 'string' } },
        },
        required: ['analysis'],
        additionalProperties: true,
      },
      render(args, value) {
        const analysis = args?.analysis ?? '?'
        if (value.outputs && typeof value.outputs === 'object') {
          const lines = [`msim_analyze ${analysis}`]
          for (const [name, entry] of Object.entries(value.outputs)) lines.push(...renderRows({ output: name, ...entry }))
          if (value.timed_out) lines.push(`  timed out; missing: ${JSON.stringify(value.missing_outputs ?? [])}`)
          lines.push(`  simulation state: ${fmt(value.state)}`)
          return [{ type: 'text', text: lines.join('\n') }]
        }
        return [{ type: 'text', text: `msim_analyze ${analysis}\n${kvLines(value, '  ').join('\n')}` }]
      },
    },
    timeoutMs: 400000,
    isConcurrencySafe: () => false,
    async execute(args) {
      const analysis = String(args?.analysis ?? '')
      const timeout = Number.isFinite(Number(args?.timeout)) ? Number(args.timeout) : undefined
      const maxPoints = Number.isInteger(args?.max_points) ? args.max_points : undefined
      const outputs = Array.isArray(args?.outputs) ? args.outputs.map(String) : undefined
      const needOutputs = () => {
        if (!outputs || outputs.length === 0) throw new Error(`msim_analyze ${analysis} requires outputs`)
        return outputs
      }
      switch (analysis) {
        case 'state':
          return { analysis, ...(await call('com.sim_state', {}, 60000)) }
        case 'stop':
          return { analysis, ...(await call('com.stop', {}, 90000)) }
        case 'pause':
          return { analysis, ...(await call('com.pause', {}, 90000)) }
        case 'resume':
          return { analysis, ...(await call('com.resume', {}, 90000)) }
        case 'transient':
          return {
            analysis,
            ...(await analysisCall(
              'com.run_transient',
              {
                outputs: needOutputs(),
                duration: Number.isFinite(Number(args?.duration)) ? Number(args.duration) : 0.001,
                ...(Number.isFinite(Number(args?.sample_rate)) ? { sample_rate: Number(args.sample_rate) } : {}),
                ...(Number.isInteger(args?.num_samples) ? { num_samples: args.num_samples } : {}),
                ...(typeof args?.repeat === 'boolean' ? { repeat: args.repeat } : {}),
                ...(timeout !== undefined ? { timeout } : {}),
                ...(maxPoints !== undefined ? { max_points: maxPoints } : {}),
              },
              (timeout ?? 60) * 1000 + 60000,
            )),
          }
        case 'dc':
          return {
            analysis,
            ...(await analysisCall(
              'com.run_dc',
              {
                outputs: needOutputs(),
                ...(timeout !== undefined ? { timeout } : {}),
                ...(maxPoints !== undefined ? { max_points: maxPoints } : {}),
              },
              (timeout ?? 60) * 1000 + 60000,
            )),
          }
        case 'ac_sweep':
          return {
            analysis,
            ...(await analysisCall(
              'com.run_ac_sweep',
              {
                outputs: needOutputs(),
                ...(Number.isInteger(args?.sweep_type) ? { sweep_type: args.sweep_type } : {}),
                ...(Number.isInteger(args?.num_points) ? { num_points: args.num_points } : {}),
                ...(Number.isFinite(Number(args?.start)) ? { start: Number(args.start) } : {}),
                ...(Number.isFinite(Number(args?.stop)) ? { stop: Number(args.stop) } : {}),
                ...(timeout !== undefined ? { timeout } : {}),
                ...(maxPoints !== undefined ? { max_points: maxPoints } : {}),
              },
              (timeout ?? 120) * 1000 + 60000,
            )),
          }
        case 'ac_single':
          return {
            analysis,
            ...(await analysisCall(
              'com.run_ac_single',
              {
                outputs: needOutputs(),
                ...(Number.isFinite(Number(args?.frequency)) ? { frequency: Number(args.frequency) } : {}),
                ...(timeout !== undefined ? { timeout } : {}),
                ...(maxPoints !== undefined ? { max_points: maxPoints } : {}),
              },
              (timeout ?? 60) * 1000 + 60000,
            )),
          }
        case 'command_file': {
          const commandFile = String(args?.command_file ?? '').trim()
          const logFile = String(args?.log_file ?? '').trim()
          if (!commandFile || !logFile) throw new Error('msim_analyze command_file requires command_file and log_file')
          return {
            analysis,
            ...(await analysisCall(
              'com.run_command_file',
              { command_file: commandFile, log_file: logFile, ...(timeout !== undefined ? { timeout } : {}) },
              (timeout ?? 120) * 1000 + 60000,
            )),
          }
        }
        default:
          throw new Error(`msim_analyze: unknown analysis ${JSON.stringify(analysis)}`)
      }
    },
  }

  /* ---- 6. 导出 -------------------------------------------------------- */

  const exportTool = {
    name: 'msim_export',
    description:
      'Export the current circuit: a schematic image (PNG/JPEG/BMP, rendered by Multisim itself at a fixed '
      + 'viewport — parts outside the visible area are omitted), a netlist, a bill of materials, or a design '
      + 'snippet. Everything is written to an absolute path and the tool returns that path plus size and, for '
      + 'images, pixel dimensions and the on-screen picture.',
    parameters: {
      type: 'object',
      properties: {
        kind: {
          type: 'string',
          enum: ['image', 'netlist', 'bom', 'snippet'],
          description:
            'image: schematic picture, also shown to you. netlist/bom: text report, returned inline and written '
            + 'to path. snippet: save the design as a Multisim snippet; a snippet is a PNG container with the design '
            + 'embedded, so the written file is path + ".png" and the returned path/format/dimensions say so.',
        },
        path: {
          type: 'string',
          description:
            'Absolute output path. For image the real format follows image_format, not the extension, so prefer '
            + 'matching extensions. For snippet Multisim appends ".png" itself, so give the name without it. Required.',
        },
        image_format: {
          type: 'string',
          enum: ['png', 'jpg', 'bmp'],
          description: 'Image format for kind="image". Default png.',
          default: 'png',
        },
        format: {
          type: 'string',
          enum: ['text', 'csv'],
          description: 'Report format for kind="netlist" and kind="bom". Default text.',
          default: 'text',
        },
        probes: {
          type: 'boolean',
          description: 'Include probe data in the netlist report. Default false.',
          default: false,
        },
        real: {
          type: 'boolean',
          description: 'Use real rather than virtual component values in the BOM. Default false.',
          default: false,
        },
        zoom: { type: 'number', description: 'Snippet zoom factor. Default 1.0.' },
        selection_only: { type: 'boolean', description: 'Export only the current selection as a snippet. Default false.' },
      },
      required: ['kind', 'path'],
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          kind: { type: 'string' },
          path: { type: 'string' },
          bytes: { type: 'integer' },
          width: { type: 'integer' },
          height: { type: 'integer' },
          format: { type: 'string' },
          text: { type: 'string' },
        },
        required: ['kind', 'path'],
        additionalProperties: true,
      },
      render: renderExport,
    },
    projectContent: imageProjection(exportImages, renderExport),
    timeoutMs: 180000,
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      rememberArgs(exportImages, exec, args)
      const kind = String(args?.kind ?? '')
      const path = String(args?.path ?? '').trim()
      if (!path) throw new Error('msim_export requires path')
      if (kind === 'image') {
        const imageFormat = String(args?.image_format ?? 'png').toLowerCase()
        const result = await call('com.get_image', { path, image_format: imageFormat }, 180000)
        await stageImage(ctx, exec, exportImages, result?.path ?? path, `schematic exported to ${result?.path ?? path}`)
        return { kind, ...(result ?? {}) }
      }
      if (kind === 'netlist') {
        const result = await call(
          'com.report_netlist',
          { path, probes: Boolean(args?.probes), fmt: String(args?.format ?? 'text') },
          120000,
        )
        return { kind, ...(result ?? {}) }
      }
      if (kind === 'bom') {
        const result = await call(
          'com.report_bom',
          { path, real: Boolean(args?.real), fmt: String(args?.format ?? 'text') },
          120000,
        )
        return { kind, ...(result ?? {}) }
      }
      if (kind === 'snippet') {
        const result = await call(
          'com.snippet',
          {
            path,
            selection_only: Boolean(args?.selection_only),
            ...(Number.isFinite(Number(args?.zoom)) ? { zoom: Number(args.zoom) } : {}),
          },
          120000,
        )
        return { kind, ...(result ?? {}) }
      }
      throw new Error(`msim_export: unknown kind ${JSON.stringify(kind)}`)
    },
  }

  /* ---- 7. 可见窗口 ---------------------------------------------------- */

  const guiTool = {
    name: 'msim_gui',
    description:
      'Drive the visible Multisim window with real Win32 calls: start or stop it, list its windows, bring it to '
      + 'the front, take a screenshot, drop a file onto it, dismiss dialogs, answer a Save As dialog, or send '
      + 'keystrokes. This window is a different process from the automation session, so circuits opened here are '
      + 'not visible to msim_inspect or msim_analyze. Use it for netlists, visual checks, and anything the '
      + 'automation API cannot reach.',
    parameters: {
      type: 'object',
      properties: {
        action: {
          type: 'string',
          enum: [
            'launch',
            'windows',
            'main_window',
            'focus',
            'screenshot',
            'open_file',
            'close_dialogs',
            'dismiss_database',
            'save_as',
            'send_keys',
            'run_analysis',
            'capture_graph',
            'kill',
          ],
          description:
            'launch: start multisim.exe if it is not running. windows: list top-level windows. main_window: '
            + 'locate the main frame. focus: raise it to the foreground. screenshot: capture a picture. open_file: '
            + 'inject the file into the window as a shell drop, which is the only reliable way to open SPICE '
            + 'netlists. close_dialogs: dismiss modal dialogs by WM_CLOSE, which does not work on every dialog. '
            + 'dismiss_database: click past the "database cannot be accessed" warnings that otherwise swallow every '
            + 'keystroke sent afterwards. save_as: drive the Save As dialog; only '
            + 'an untitled document opens it, a named one saves in place and this reports an error instead of '
            + 'overwriting the source (prefer msim_circuit save_as). '
            + 'send_keys: synthesize keystrokes. run_analysis: pick an analysis in the Simulation menu\'s '
            + '"Analyses and Simulation" dialog and press Run, which is how you get a real waveform out of the '
            + 'visible window. capture_graph: screenshot the "图示仪视图" plot window that a finished analysis '
            + 'opens, so the picture is Multisim\'s own waveform plot. '
            + 'kill: close Multisim instances this toolkit started (see pid and '
            + 'all_instances).',
        },
        path: {
          type: 'string',
          description: 'Absolute path, for screenshot and save_as (destination) and open_file (source).',
        },
        mode: {
          type: 'string',
          enum: ['window', 'client', 'canvas'],
          description: 'Capture area for screenshot: window with chrome, client area, or the schematic canvas. Default window.',
          default: 'window',
        },
        crop: {
          type: 'string',
          enum: ['none', 'canvas', 'content', 'paper'],
          description:
            'screenshot cropping: none keeps the capture as is, canvas crops to the schematic drawing view, content '
            + 'trims that view to the drawn schematic plus a small margin, paper trims to the sheet of paper '
            + '(frame and parts in place, workspace fill and ruler removed). Default none.',
          default: 'none',
        },
        target_width: {
          type: 'integer',
          description: 'Scale screenshots down to this pixel width; 0 keeps the original size. Default 0.',
        },
        hwnd: {
          type: 'integer',
          description: 'Target window handle. 0 means the main frame. Default 0.',
        },
        keys: {
          type: 'string',
          description: 'Keystrokes for send_keys, e.g. "ctrl+s" or "F5".',
        },
        analysis: {
          type: 'string',
          enum: [
            'transient',
            'dc',
            'ac',
            'dc_sweep',
            'ac_single',
            'parameter_sweep',
            'noise',
            'monte_carlo',
            'fourier',
            'temperature_sweep',
            'distortion',
            'sensitivity',
            'worst_case',
            'noise_figure',
            'pole_zero',
            'transfer_function',
            'trace_width',
            'batched',
            'custom',
          ],
          description:
            'For run_analysis: which analysis to run in the "Analyses and Simulation" dialog. The Chinese labels '
            + 'in the dialog are matched, so names are the stable identifiers. Default transient.',
          default: 'transient',
        },
        timeout: {
          type: 'number',
          description: 'For run_analysis: seconds to wait for the dialog to close after Run. Default 180.',
        },
        keep: {
          type: 'string',
          description: 'Dialog title substring to leave open when closing dialogs. Default keeps nothing.',
        },
        visible_only: {
          type: 'boolean',
          description: 'For windows: list only visible top-level windows. Default false.',
        },
        wait_seconds: {
          type: 'number',
          description: 'How long to wait for the window or dialog to appear. Default 25 for launch, 30 for open_file.',
        },
        force: { type: 'boolean', description: 'For kill: terminate without asking Multisim to close. Default false.' },
        pid: {
          type: 'integer',
          description: 'For kill: also close this specific Multisim process id. Default 0.',
        },
        all_instances: {
          type: 'boolean',
          description:
            'For kill: close every multisim.exe process, including ones the user opened by hand. '
            + 'Discards unsaved work in those windows. Default false.',
        },
      },
      required: ['action'],
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          action: { type: 'string' },
          path: { type: 'string' },
          width: { type: 'integer' },
          height: { type: 'integer' },
          bytes: { type: 'integer' },
        },
        required: ['action'],
        additionalProperties: true,
      },
      render: renderGui,
    },
    projectContent: imageProjection(guiImages, renderGui),
    timeoutMs: 200000,
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      rememberArgs(guiImages, exec, args)
      const action = String(args?.action ?? '')
      const hwnd = Number.isInteger(args?.hwnd) ? args.hwnd : 0
      switch (action) {
        case 'launch':
          return {
            action,
            ...(await call(
              'gui.launch',
              { wait_seconds: Number.isFinite(Number(args?.wait_seconds)) ? Number(args.wait_seconds) : 25 },
              120000,
            )),
          }
        case 'windows':
          return { action, ...(await call('gui.windows', { visible_only: Boolean(args?.visible_only) }, 60000)) }
        case 'main_window':
          return { action, ...(await call('gui.main_window', {}, 60000)) }
        case 'focus':
          return { action, ...(await call('gui.focus', { hwnd }, 60000)) }
        case 'close_dialogs':
          return { action, ...(await call('gui.close_dialogs', { keep: String(args?.keep ?? '') }, 90000)) }
        case 'dismiss_database':
          return { action, ...(await call('gui.dismiss_database_dialogs', {}, 90000)) }
        case 'kill':
          return {
            action,
            ...(await call(
              'gui.kill',
              { force: Boolean(args?.force), pid: Number(args?.pid ?? 0), all_instances: Boolean(args?.all_instances) },
              90000,
            )),
          }
        case 'send_keys': {
          const keys = String(args?.keys ?? '').trim()
          if (!keys) throw new Error('msim_gui send_keys requires keys')
          return { action, ...(await call('gui.send_keys', { keys, hwnd }, 90000)) }
        }
        case 'screenshot': {
          const path = String(args?.path ?? '').trim()
          if (!path) throw new Error('msim_gui screenshot requires path')
          const result = await call(
            'gui.screenshot',
            {
              path,
              mode: String(args?.mode ?? 'window'),
              hwnd,
              crop: String(args?.crop ?? 'none'),
              ...(Number.isInteger(args?.target_width) ? { target_width: args.target_width } : {}),
            },
            180000,
          )
          await stageImage(ctx, exec, guiImages, result?.path ?? path, `screenshot written to ${result?.path ?? path}`)
          return { action, ...(result ?? {}) }
        }
        case 'open_file': {
          const path = String(args?.path ?? '').trim()
          if (!path) throw new Error('msim_gui open_file requires path')
          return {
            action,
            ...(await call(
              'gui.open_file',
              { path, ...(Number.isFinite(Number(args?.wait_seconds)) ? { wait_seconds: Number(args.wait_seconds) } : {}) },
              180000,
            )),
          }
        }
        case 'save_as': {
          const path = String(args?.path ?? '').trim()
          if (!path) throw new Error('msim_gui save_as requires path')
          return {
            action,
            ...(await call(
              'gui.save_as',
              { path, ...(Number.isFinite(Number(args?.wait_seconds)) ? { wait_seconds: Number(args.wait_seconds) } : {}) },
              180000,
            )),
          }
        }
        case 'run_analysis': {
          return {
            action,
            ...(await call(
              'gui.run_analysis',
              {
                analysis: String(args?.analysis ?? 'transient'),
                hwnd,
                wait_seconds: Number.isFinite(Number(args?.wait_seconds)) ? Number(args.wait_seconds) : 30,
                timeout: Number.isFinite(Number(args?.timeout)) ? Number(args.timeout) : 180,
              },
              240000,
            )),
          }
        }
        case 'capture_graph': {
          const path = String(args?.path ?? '').trim()
          if (!path) throw new Error('msim_gui capture_graph requires path')
          const result = await call(
            'gui.capture_graph',
            {
              path,
              hwnd,
              wait_seconds: Number.isFinite(Number(args?.wait_seconds)) ? Number(args.wait_seconds) : 20,
            },
            180000,
          )
          await stageImage(ctx, exec, guiImages, result?.path ?? path, `waveform plot written to ${result?.path ?? path}`)
          return { action, ...(result ?? {}) }
        }
        default:
          throw new Error(`msim_gui: unknown action ${JSON.stringify(action)}`)
      }
    },
  }

  /* ---- 8. SPICE 网表引擎 ----------------------------------------------- */

  const spiceImages = new WeakMap()

  const spiceTool = {
    name: 'msim_spice',
    description:
      'Run a SPICE netlist through Multisim\'s own simulation engine and get the traces back as tables, CSV files '
      + 'and rendered plots. This is the path to use when a design has no probes: it needs no schematic, no '
      + 'instrument and no visible window, it accepts standard SPICE3 syntax (.model, PULSE/SIN, B sources), and one '
      + 'call can run several analyses at once (op, dc, ac, tran, tf, noise, sens, disto, pz). It cannot read '
      + 'Multisim library parts such as 74LS00 or OPAMP_5T — those exist only in the schematic domain.',
    parameters: {
      type: 'object',
      properties: {
        action: {
          type: 'string',
          enum: ['run', 'verify'],
          description:
            'run: simulate the netlist and return one table + CSV + PNG per analysis command. '
            + 'verify: parse the netlist, list its elements and analysis commands, and simulate nothing.',
          default: 'run',
        },
        netlist: {
          type: 'string',
          description:
            'The SPICE netlist as text. Must start with a title line or comment, and use .end at the bottom '
            + '(both are added for you if missing). Required unless netlist_path is given.',
        },
        netlist_path: {
          type: 'string',
          description:
            'Absolute path to a .cir/.net/.sp file to read instead of the netlist text. The path must not contain '
            + 'spaces, because Multisim\'s command engine rejects command files whose paths contain spaces.',
        },
        commands: {
          type: 'array',
          items: { type: 'string' },
          description:
            'Analysis commands, in order, for example ["op"] or ["dc V1 0 5 0.1", "ac dec 20 1 1meg", '
            + '"tran 1u 5m"]. Defaults to ["op"]. Do not include source/write/quit: the plugin owns those.',
        },
        output_dir: {
          type: 'string',
          description:
            'Absolute directory for the generated .cir/.cmd/.raw/.csv/.png files. Must not contain spaces. '
            + 'Defaults to a fresh folder under %TEMP%.',
        },
        max_points: {
          type: 'integer',
          description: 'Upper bound on rows returned per analysis; 0 means no sampling. Default 80.',
          default: 80,
        },
        series: {
          type: 'array',
          items: { type: 'string' },
          description:
            'Column names to plot, as they appear in the result (for example ["V(out)", "V(n1)"]). Omit to plot '
            + 'every magnitude column; on an AC plot the dB columns are preferred over the phase columns.',
        },
        plot: {
          type: 'boolean',
          description: 'Render a PNG per analysis and show it to you. Default true.',
          default: true,
        },
        timeout: {
          type: 'number',
          description: 'Seconds to wait for the whole run. Default 180.',
          default: 180,
        },
      },
      required: [],
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          action: { type: 'string' },
          directory: { type: 'string' },
          elapsed_seconds: { type: 'number' },
          created_circuit: { type: 'boolean' },
          commands: { type: 'array', items: { type: 'string' } },
          missing: { type: 'array', items: { type: 'string' } },
          analyses: { type: 'array', items: { type: 'object' } },
        },
        required: ['action'],
        additionalProperties: true,
      },
      render: renderSpice,
    },
    projectContent: imageProjection(spiceImages, renderSpice),
    timeoutMs: 400000,
    isConcurrencySafe: () => false,
    async execute(args, exec) {
      rememberArgs(spiceImages, exec, args)
      const action = String(args?.action ?? 'run')
      const shared = {
        ...(typeof args?.netlist === 'string' && args.netlist.trim() ? { netlist: args.netlist } : {}),
        ...(typeof args?.netlist_path === 'string' && args.netlist_path.trim() ? { netlist_path: args.netlist_path } : {}),
        ...(Array.isArray(args?.commands) ? { commands: args.commands.map((item) => String(item)) } : {}),
      }
      if (!shared.netlist && !shared.netlist_path) {
        throw new Error('msim_spice needs netlist text or netlist_path')
      }
      if (action === 'verify') {
        return { action, ...(await call('spice.verify', shared, 60000)) }
      }
      if (action !== 'run') throw new Error(`msim_spice: unknown action ${JSON.stringify(action)}`)
      const result = await call(
        'spice.run',
        {
          ...shared,
          ...(typeof args?.output_dir === 'string' && args.output_dir.trim() ? { output_dir: args.output_dir } : {}),
          ...(Array.isArray(args?.series) && args.series.length > 0
            ? { series: args.series.map((item) => String(item)) }
            : {}),
          max_points: Number.isInteger(args?.max_points) ? args.max_points : 80,
          plot: args?.plot !== false,
          timeout: Number.isFinite(Number(args?.timeout)) ? Number(args.timeout) : 180,
        },
        380000,
      )
      for (const analysis of result?.analyses ?? []) {
        const png = analysis?.png?.path
        if (!png) continue
        await stageImage(ctx, exec, spiceImages, png, `plot: ${analysis.command} → ${analysis.plotname}`)
      }
      return { action, ...(result ?? {}) }
    },
  }

  /* ---- 9. 离线读 .ms14 ------------------------------------------------- */

  const designTool = {
    name: 'msim_design',
    description:
      'Read a Multisim design file (.ms14/.msm/.ewb/.ms11/.ms12/.ms13) straight off disk, without Multisim and '
      + 'without opening anything. The file is a PKWare-DCL compressed container around an XML document; this tool '
      + 'unpacks it and reports the components, values, wire and pin counts and the creating version. Use it to see '
      + 'what a design contains when Multisim is closed, when the automation instance refuses the file, or to read '
      + 'a design whose internals you need to reason about. It only reads: nothing is simulated and nothing is written.',
    parameters: {
      type: 'object',
      properties: {
        action: {
          type: 'string',
          enum: ['read'],
          description: 'read: decode the container and summarise the design.',
          default: 'read',
        },
        path: {
          type: 'string',
          description: 'Absolute path to the design file.',
        },
        include_xml: {
          type: 'boolean',
          description:
            'Also return the decoded XML as text. The document is large (hundreds of KB to a few MB), so leave this '
            + 'off unless you need to inspect something the summary does not cover.',
          default: false,
        },
        xml_limit: {
          type: 'integer',
          description: 'Characters of XML to return when include_xml is true. Default 200000.',
          default: 200000,
        },
      },
      required: ['path'],
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          path: { type: 'string' },
          root: { type: 'string' },
          schema: { type: 'string' },
          blocks: { type: 'integer' },
          compressed_bytes: { type: 'integer' },
          decoded_bytes: { type: 'integer' },
          declared_length_ok: { type: 'boolean' },
          creator: { type: 'object' },
          counts: { type: 'object' },
          components: { type: 'array', items: { type: 'object' } },
          instruments: { type: 'array', items: { type: 'object' } },
          xml: { type: 'string' },
        },
        required: ['path'],
        additionalProperties: true,
      },
      render: renderDesign,
    },
    timeoutMs: 120000,
    isConcurrencySafe: () => true,
    async execute(args) {
      const action = String(args?.action ?? 'read')
      if (action !== 'read') throw new Error(`msim_design: unknown action ${JSON.stringify(action)}`)
      const path = String(args?.path ?? '').trim()
      if (!path) throw new Error('msim_design needs a path')
      return {
        action,
        ...(await call('design.read', {
          path,
          include_xml: args?.include_xml === true,
          ...(Number.isInteger(args?.xml_limit) ? { xml_limit: args.xml_limit } : {}),
        }, 110000)),
      }
    },
  }

  ctx.inject(['tools'], (inner) => {
    const definitions = [
      statusTool,
      circuitTool,
      inspectTool,
      editTool,
      analysisTool,
      exportTool,
      guiTool,
      spiceTool,
      designTool,
    ]
    for (const definition of definitions) {
      ctx.effect(() => inner.tools.register(definition), `multisim-toolkit.${definition.name}`)
    }
  })

  ctx.inject(['systemPrompt'], (inner) => {
    const order = inner.systemPrompt.getSectionOrder?.('MCP_SERVERS') ?? 100
    ctx.effect(
      () => inner.systemPrompt.section({
        name: 'multisim-toolkit',
        order,
        interpolate: false,
        text: () => SYSTEM_PROMPT,
      }),
      'multisim-toolkit.prompt',
    )
  })

  ctx.effect(
    () => () => {
      void bridge.dispose()
    },
    'multisim-toolkit.dispose',
  )
}

const SYSTEM_PROMPT = `# Multisim 14.3 (msim_* tools)

You can drive the Multisim 14.3 installed on this computer. Two independent channels exist and they do not share state:

- **Automation channel** (\`msim_circuit\`, \`msim_inspect\`, \`msim_edit\`, \`msim_analyze\`, \`msim_export\`) runs against a hidden extra Multisim instance that the plugin starts. Fast, scriptable, and invisible.
- **Visible window channel** (\`msim_gui\`) drives the multisim.exe window the user can see. Separate process — a design loaded there is NOT the circuit the automation tools see.

Workflow: \`msim_status\` → \`msim_circuit\` open/new → \`msim_inspect\` → \`msim_edit\` / \`msim_analyze\` / \`msim_export\` → \`msim_circuit\` save.

**When a design has no probes, skip the schematic channel and use \`msim_spice\`.** It feeds a SPICE netlist straight to Multisim's simulation engine and returns tables, CSV files and plotted curves in one call — no probes, no instruments, no visible window. Analyses in the automation channel only work on named outputs; a design without probes or instruments has none.

To find out what a \`.ms14\` holds without starting Multisim, use \`msim_design\`: it unpacks the file on disk and lists components, values and the saving version. It is read-only and works even for a design the automation server refuses.

Constraints worth knowing before you promise anything:

- The automation server is 32-bit; the plugin needs a 32-bit Python. \`msim_status\` reports which interpreter it found.
- Opening a SPICE netlist (\`.cir\`, \`.net\`) through the automation server crashes Multisim. \`msim_circuit open\` routes those to the visible window instead, which is the only reliable path.
- Analyses in the automation channel need named outputs (\`msim_inspect what="outputs"\`). A design without probes or instruments exposes none, and analyses then time out — that is a property of the design, not a fault. For those designs use \`msim_spice\` instead.
- \`msim_spice\` runs the plain SPICE3 subset. Multisim library parts (\`74LS00\`, \`OPAMP_5T\`, \`D_CHIP\` …) are not expanded there — they exist only in the schematic domain; behavioural sources (\`B1 out 0 V = ...\`) are the substitute.
- Export images come from Multisim's own renderer at a fixed viewport: parts scrolled out of view are missing from the picture. For a faithful picture of the whole schematic, open the design in the visible window and use \`msim_gui\` screenshot with \`crop: "content"\` (just the drawing) or \`crop: "paper"\` (the sheet, parts in place).
- Component names in exported BOM/netlist reports are encoded in Multisim's internal \`_ucXXXX\` scheme; do not present that text as readable labels.
- \`msim_design\` reads the same \`.ms14\` the editor writes, but it only summarises: it cannot modify a design, and the XML it can return is Multisim's own schema, not a netlist you can simulate.
`
