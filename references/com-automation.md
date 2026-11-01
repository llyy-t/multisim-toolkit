# Multisim 14.3 COM 自动化参考

本文件描述 `msim_*` 工具族背后 **自动化通道** 的真实能力面与失败面。
所有结论来自 `bundle/worker/msim_com.py` 的实现与 `bundle/worker/PROTOCOL.md` §4.1/§5 的实测记录，不是推测。

配套阅读顺序：先读本文件第 1 节（能做什么），再读第 8 节（哪些一定失败），最后再决定要不要用 COM 通道。
**如果设计里没有探针/仪器，直接跳到第 9 节用 `msim_spice`，不要浪费时间调分析。**

---

## 1. 结论速查

| 事实 | 值 |
| --- | --- |
| ProgID | `MultisimInterface.MultisimApp`（`msim_com.py:65` `PROG_ID`） |
| CLSID | `{D9CBB7A1-6AD6-438B-AD1B-42FB2DB00CAE}` |
| AppID / TypeLib GUID | `{D360240D-8F60-4FBC-AEC2-B6E14C3042B8}`，TypeLib 版本 **1.0** |
| 服务器形态 | `InprocServer32` = `C:\Program Files (x86)\National Instruments\Circuit Design Suite 14.3\MSInterface.dll`，`ThreadingModel = apartment`，FileVersion `14.3.1482.0` |
| COM 进程 | `Connect()` 之后由 `MSInterface.dll` 拉起的 `multisim.exe`（**隐藏**，用户看不到窗口） |
| Python 位数 | **只能 32 位**。注册表只在 WOW64_32KEY 视图可见，64 位解释器 `Connect()` 得到 `E_ACCESSDENIED (-2147024891)` |
| 已实现的方法数 | `MultisimSession` 共 40 个公开方法（见第 3 节） |
| 可直接打开的格式 | **只有 `.ms14`**（`OPENABLE_EXTENSIONS`）；`.cir`/`.net` 会崩进程，必须走 `msim_gui` |
| 分析的前提 | 设计里必须有探针或仪器，否则 `EnumOutputs` 恒空、所有分析必失败 |
| 通道隔离 | 自动化实例看不到可见窗口里打开的设计——两个独立进程，不共享状态 |

**一句话判据**：`msim_inspect what="outputs"` 返回 `count: 0` → 这台机器上任何 `msim_analyze` 分析都不可能出波形，改走 `msim_spice`。

---

## 2. COM 生命周期（为什么必须常驻 worker）

```
DSH Host (node, Medium IL)
  └── index.js  →  spawn  →  <32-bit python> msim_worker.py --port N --token T
                              └── 常驻进程，独占 COM 公寓（STA），TCP 127.0.0.1:N 串行收命令
                                   └── MultisimSession → Dispatch → app.Connect() → 隐藏 multisim.exe
```

三条硬事实：

1. `Dispatch("MultisimInterface.MultisimApp")` + `app.Connect()` **永远新起一个隐藏实例**。
   附着不到用户屏幕上已打开的那个窗口。那个实例的所有窗口 `vis=False`，但 `GetCircuitImage` 仍然可用。
2. **客户端进程退出 → `multisim.exe` 立即消失。** COM 服务器生命周期绑死在第一个调用 `Connect()` 的进程上。
   所以不能"每条命令起一个短命进程"，必须常驻。
3. `app.Disconnect()` **会关掉 Multisim**。它只在 `MultisimSession.shutdown()` 里被调用一次，
   `__init__` / `__del__` / 自测收尾都不调。误用在用工作会直接丢。

懒激活规则（`msim_com.py` 类文档字符串，L561-562）：`__init__` 不做任何 COM 工作；
`ping()` 在 `_app is None` 时直接返回 `{"connected": False, "version": None}`，**不激活、不连接、无副作用**。
真正拉起 Multisim 的是 `connect` / `new_circuit` / `open_circuit` 或任何需要电路的操作。

`Connect()` 首次调用会偶发返回 `E_ACCESSDENIED`，所以实现里重试 `CONNECT_ATTEMPTS = 6` 次、
间隔 `CONNECT_RETRY_DELAY = 1.0` 秒（`msim_com.py:69-70`）。6 次全挂才抛：

```
app.Connect() failed after 6 attempts: <原文>
detail: E_ACCESSDENIED (-2147024891) means this process is not at Medium IL -- DSH needs danger-full-access.
```

激活本身失败（`gencache.EnsureDispatch` 抛 `AttributeError`）时会自动回退 `win32com.client.dynamic.Dispatch`，
覆盖 gen_py 缓存损坏的情况；两条路都失败才抛 `NO_MULTISIM`。

---

## 3. `MultisimSession` 方法参考

约定：

- 每个方法都返回**可 JSON 序列化的 dict**（只含 str/int/float/bool/list/dict/None）。下表的"返回"字段是该 dict 的键。
- 失败一律抛 `MsimError(code, message, detail)`，`code` 取自固定集合：
  `BAD_ARGS` / `NO_CIRCUIT` / `NO_MULTISIM` / `COM_ERROR` / `TIMEOUT` / `PATH_MISSING` / `GUI_ERROR` / `INTERNAL`。
- 除特别注明，所有方法在未打开电路时抛 `NO_CIRCUIT`。

### 3.1 会话

| 方法 | 参数 | 返回 | 说明 |
| --- | --- | --- | --- |
| `ping()` | — | `{connected: bool, version: str\|None}` | 永不激活 COM；`self._app is None` 时立刻返回 `connected: false` |
| `connect()` | — | `{connected: bool, version: str, path: str}` | 激活 + `Connect()`（6 次重试）。`version` 来自 `app.VersionInfo`，`path` 来自 `app.Path` |
| `shutdown()` | — | 已断开 `{disconnected: true, was_connected: bool}`；失败 `{disconnected: false, was_connected: true, warning: str}` | **唯一**允许调 `app.Disconnect()` 的地方，它关闭隐藏实例 |

### 3.2 电路文件

| 方法 | 参数 | 返回 | COM 成员 |
| --- | --- | --- | --- |
| `new_circuit()` | — | `circuit_info()` 的形状 | `app.NewFile()`，返回值即新电路对象 |
| `open_circuit(path)` | `path: str`（绝对化、展开 `~`） | `circuit_info()` 的形状 | `app.OpenFile(path)`。**只允许 `.ms14`**，否则 `BAD_ARGS`；文件不存在 `PATH_MISSING` |
| `save()` | — | `{path: str}` | `circuit.Save()`。`FileName` 为空时**拒绝**并抛 `BAD_ARGS`（见 §8.2） |
| `save_as(path)` | `path: str` | `{path: str, bytes?: int}` | `circuit.SaveAs(path)`；父目录不存在会先创建；`bytes` 仅在文件确实落盘时出现 |
| `circuit_info()` | — | `{name, file, state, state_name, last_error}` | `CircuitName` / `FileName` / `SimulationState` |

`open_circuit` 的后缀检查用 `_design_suffix()`（`msim_com.py:352-369`），不是 `os.path.splitext`：
它容忍 Multisim AutoRecover 写出的 `"<name>.ms14 (Security copy)"` 形态。
所以带括号后缀的 `.ms14` 也能打开，但 `msim_circuit open` 的工具层另有一套白名单（见 §3.6）。

### 3.3 读电路

| 方法 | 参数 | 返回 | COM 成员 |
| --- | --- | --- | --- |
| `enum_components(component_type=0)` | `int`，见 `ComponentType` | `{items: [str], count: int}` | `EnumComponents(int)` |
| `enum_inputs(io_type=0)` | `int`，见 `SimulationIOType` | `{items: [str], count: int}` | `EnumInputs(int)` |
| `enum_outputs(io_type=0)` | `int`，见 `SimulationIOType` | `{items: [str], count: int}` | `EnumOutputs(int)` |
| `enum_variants()` | — | `{items: [str], count: int}` | `EnumVariants()` |
| `get_active_variant()` | — | `{variant: str}` | 属性 `ActiveVariant` |
| `set_active_variant(name)` | `name: str` | `get_active_variant()` 的形状 | 给 `ActiveVariant` 赋值，再回读确认 |
| `enum_sections(component)` | `component: str`（如 `"U1A"`） | `{items: [str], count: int}` | `EnumSections(str)` |
| `enum_parameters(level=1)` | `int`，见 `CircuitParameterLevel` | `{items: [str], count: int}` | `EnumCircuitParameters(int)` |
| `get_parameter(name)` | `name: str` | `{name: str, value: float}` | `CircuitParameterValue(str)` |
| `set_parameter(name, value)` | `value: float`（有限值，`NaN`/`inf` 抛 `BAD_ARGS`） | `{name, value}` | `SetCircuitParameterValue`；绑定里只有属性时退化为 `__setattr__` |
| `get_rlc(component)` | `component: str`（如 `"R1"`） | `{component: str, value: float}` | `RLCValue(str)` |
| `set_rlc(component, value)` | `value` 单位是 SI 基准单位：欧姆 / 法拉 / 亨 | `{component, value}` | `SetRLCValue(str, float)`（见 §7.3） |

`enum_*` 全部走 `_clean_list()`，每个字符串过 `clean_error()`（latin-1 → utf-8 回转），
所以中文元件名返回的是正确文本而不是乱码。

### 3.4 仿真控制

| 方法 | 参数 | 返回 | 备注 |
| --- | --- | --- | --- |
| `sim_state()` | — | `{state: int, state_name: str}` | 先碰一次电流电路，所以"没开电路"报 `NO_CIRCUIT` 而不是误导性的 `Stopped` |
| `stop()` | — | `{stopped: bool, state, state_name, warning?}` | 本来就没在跑时返回 `stopped: false` + `warning`，**不抛** |
| `pause()` | — | `{paused: bool, state, state_name, warning?}` | 契约要求**永不抛**，连 `NO_CIRCUIT` 也吞成 `paused: false` + `warning` |
| `resume()` | — | `{resumed: bool, state, state_name, warning?}` | 同 `pause()` |
| `set_output_request(output, method=0, sample_rate=1e6, num_samples=1000, repeat=False)` | 见签名 | `{output, method, sample_rate, num_samples, repeat, requested: true}` | `sample_rate`/`num_samples` 必须 > 0 |
| `clear_output_request(output)` | `output: str` | `{output, cleared: bool, warning?}` | 失败不抛 |
| `output_ready(output)` | `output: str` | `{output, ready: bool}` | 失败抛 `COM_ERROR`（探针不存在时就是这条路） |
| `get_output_data(output, max_points=2000)` | 见签名 | 见下 | 内部 `GetOutputData(name, Missing, Missing)` |

`get_output_data` 的返回：

```
{output: str, method: int|null, shape: [n_rows, n_cols], n_points: int,
 sampled_points: int, rows: [[float, ...], ...]}
```

`n_points` 取**最宽一行**的长度（安全数组理论上矩形，但半满缓冲区会给回参差行），
`rows` 按 `ceil(n_points / max_points)` 等距抽样。

### 3.5 分析

四个 `run_*` 返回**统一形状**：

```
{analysis: "transient"|"dc"|"ac_sweep"|"ac_single",
 outputs: {<name>: <get_output_data 形状 或 {..., error: str}>},
 elapsed: float, state: int, timed_out: bool, missing_outputs: [str],
 stop_succeeded?: bool, stop_warning?: str, poll_error?: str, last_error?: str}
```

| 方法 | 参数（默认值） | 内部 COM 调用 |
| --- | --- | --- |
| `run_transient(outputs, duration, sample_rate=1e6, num_samples=1000, repeat=False, timeout=60.0, max_points=2000)` | `duration` > 0 | 先 `ClearOutputRequest` + `StopSimulation`，再对每个输出 `SetOutputRequest(name, Raw=0, rate, samples, repeat)`，然后 `RunSimulation(duration, False)` |
| `run_dc(outputs, timeout=60.0, max_points=500)` | — | `DoDCOperatingPoint(VT_ARRAY\|VT_BSTR of names)` |
| `run_ac_sweep(outputs, sweep_type=0, num_points=10, start=100.0, stop=1e6, timeout=120.0, max_points=2000)` | `sweep_type ∈ {0,1,2}`；`0 < start < stop` | `DoACSweep(sweep_type, num_points, start, stop, bstr_array(names))` |
| `run_ac_single(outputs, frequency=1000.0, timeout=60.0, max_points=200)` | `frequency` > 0 | `DoACSingleFrequency(freq, bstr_array(names))` |

`outputs` 接受 `list[str]` 或单个 `str`；去重保序。空列表/空串抛 `BAD_ARGS`，detail 里写明原因：

```
Invalid argument for parameter 1 -> expected an array with 1 dimensions of type: string,
instead received an array of type: Empty
```

**超时等待策略**（`_wait_outputs`，`msim_com.py:1373-1416`）：每 `POLL_INTERVAL = 0.05` 秒轮询
`OutputReady(name)`。如果**每次**轮询都抛异常并持续 `POLL_ERROR_GRACE = 2.0` 秒，
立刻放弃（不烧光整个 timeout），把结果标成 `timed_out: true` 并附 `poll_error`。
这就是"没有探针时 `msim_analyze` 不会卡住整整一分钟"的原因。

### 3.6 导出

| 方法 | 参数 | 返回 | COM 成员 |
| --- | --- | --- | --- |
| `get_image(path, image_format="png")` | `image_format ∈ {png, jpg, bmp}` | `{path, bytes, format, width, height, renamed, requested_format, source?}` | `GetCircuitImage(int fmt, str path)` |
| `report_netlist(path, probes=False, fmt="text")` | `fmt ∈ {text, csv}` | `{path, bytes, text, chars, truncated, probes, format}` | `ReportNetlist(bool probes, int fmt, str path)` |
| `report_bom(path, real=False, fmt="text")` | `real` = 用真实而非虚拟元件值 | 同上（`real` 替换 `probes`） | `ReportBOM(bool real, int fmt, str path)` |
| `snippet(path, selection_only=False, zoom=1.0)` | `zoom` > 0 | `{path, bytes, selection_only, zoom, format?, width?, height?}` | `CreateSnippet(path, bool, float)` |

`get_image` 的格式是**以实际内容为准**的：写完读前 256 KB 头字节判魔数，
真格式回填到 `format`，文件名归一成 `path 去扩展名 + "." + 真格式`，改名成功则 `renamed: true`、
原名记在 `source`。所以传 `.png` 也可能得到 `.jpg` 文件——信 `format` 字段，别信传入的后缀。

文本报告用 `_read_text_file()` 读，依次尝试 `utf-8-sig` → `utf-8` → `gbk` → `cp936` → `latin-1`
（本机 ANSI 代码页是 936）。`text` 上限 `MAX_REPORT_CHARS = 20000` 字符，超出置 `truncated: true`、
`chars` 记原始长度。

### 3.7 输入与命令文件

| 方法 | 参数 | 返回 | 硬约束 |
| --- | --- | --- | --- |
| `set_input_raw(name, values, repeat=False)` | `values` 可以是扁平交错 `[t0,v0,t1,v1,...]` 或成对 `[[t0,v0],[t1,v1]]` | `{input, samples, repeat, dimensions: 2}` | 传给 COM 的是 **2 维** `VT_ARRAY\|VT_R8`，见 §8.6 |
| `set_input_sampled(name, sample_rate, values, repeat=False)` | `values: [float]` | `{input, samples, sample_rate, repeat, dimensions: 1}` | `sample_rate` > 0；1 维数组 |
| `clear_input(name)` | `name: str` | `{input, cleared, warning?}` | — |
| `run_command_file(command_file, log_file, timeout=120.0)` | 两个绝对路径 | `{state, timed_out, elapsed, log_exists, log, log_path, command_file, log_chars, log_truncated}` | 超时会先 `StopSimulation` 再返回 |

`set_input_raw` / `set_input_sampled` 都会先调 `ReserveInput(name)`。
`run_command_file` 里 `command_file` 不存在抛 `PATH_MISSING`；`log_file` 父目录不存在会被创建。

### 3.8 改库元件

| 方法 | 参数 | 行为 |
| --- | --- | --- |
| `replace_component(component, section, database, group, family, name, model)` | `database` 接受 `0/1/2` 或字符串 `"master"/"corporate"/"user"` | **默认直接抛 `COM_ERROR` 拒绝**，除非环境变量 `MSIM_ALLOW_REPLACE_COMPONENT=1`。见 §8.4 |

### 3.9 工具层与 COM 方法的对应

`msim_*` 工具经 worker 的 op 名转发（`msim_worker.py:120-160` 的 `COM_OPS`），op 名与方法名一一对应，前缀 `com.`。

| 工具调用 | COM op | 实现方法 |
| --- | --- | --- |
| `msim_status` | `com.ping` / `gui.probe` | `ping()` |
| `msim_circuit action=new` | `com.new_circuit` | `new_circuit()` |
| `msim_circuit action=open` | `open_any` | `.ms14/.msm/.ewb/.ms11/.ms12/.ms13` → `open_circuit()`；**其它后缀 → `msim_gui.open_file`（GUI 通道）** |
| `msim_circuit action=save` / `save_as` / `info` | `com.save` / `com.save_as` / `com.circuit_info` | 同名方法 |
| `msim_inspect what=...` | `com.enum_*` / `com.get_*` | 3.3 节 |
| `msim_edit action=set_rlc` | `com.set_rlc` | `set_rlc()` |
| `msim_edit action=set_parameter` | `com.set_parameter` | `set_parameter()` |
| `msim_edit action=set_variant` | `com.set_active_variant` | `set_active_variant()` |
| `msim_edit action=replace_component` | `com.replace_component` | `replace_component()`（默认被拒） |
| `msim_analyze analysis=transient/dc/ac_sweep/ac_single` | `com.run_*` | 3.5 节 |
| `msim_analyze analysis=state/stop/pause/resume` | `com.sim_state` / `com.stop` / `com.pause` / `com.resume` | 3.4 节 |
| `msim_analyze analysis=command_file` | `com.run_command_file` | 3.7 节 |
| `msim_export kind=image/netlist/bom/snippet` | `com.get_image` / `com.report_netlist` / `com.report_bom` / `com.snippet` | 3.6 节 |

注意工具层 `msim_circuit open` 的后缀白名单比 `open_circuit()` 宽：`_open_any`（`msim_worker.py:317-323`）
放行 `.ms14/.msm/.ewb/.ms11/.ms12/.ms13` 给 COM，其余全部落到 `msim_gui.open_file`。
直接调 `open_circuit()` 则只认 `.ms14`。

工具层默认值（`index.js`）：`msim_analyze` 的 `duration` 缺省 **0.001**；
`timeout` 缺省 transient/dc/ac_single 60、ac_sweep/command_file 120 秒。

---

## 4. 枚举整数值（严格照抄类型库）

写成代码就照下面的数字，不要试字符串——除 `image_format` / `fmt` / `database` 由模块内部映射外，
其余全部是整数入参。

```python
SimulationState          Stopped=0  Running=1  Paused=2
SimulationIOType         All=0      Voltage=1  Current=2  Power=3  Digital=4
SimulationInterpolation  Raw=0      ForceStep=1  Linear=2  Spline=3  Coerce=4
SweepType                Decade=0   Octave=1   Linear=2
ExportFormat             Text=0     CSV=1
ComponentType            All=0      AllRLC=1  Resistor=2  Capacitor=3  Inductor=4
MultisimDB               Master=0   Corporate=1  User=2
CircuitImageFormat       PNG=0      JPG=1     BMP=2
CircuitParameterLevel    All=0      TopLevel=1
```

工具层对应的字符串参数：

| 参数 | 接受值 | 映射 |
| --- | --- | --- |
| `msim_export image_format` | `png` / `jpg` / `jpeg` / `bmp` | 0 / 1 / 1 / 2 |
| `msim_export format` | `text` / `txt` / `csv` | 0 / 0 / 1 |
| `msim_edit database` | `0/1/2` 或 `"master"/"corporate"/"user"` | Master=0, Corporate=1, User=2 |

`msim_analyze sweep_type` 只接受 `0/1/2`，其它值抛 `sweep_type must be 0 (Decade), 1 (Octave) or 2 (Linear)`。

---

## 5. 一次性启动与自检

```powershell
$py = 'C:\Python313-32\python.exe'
& $py -m msim_com              # connect → new_circuit → circuit_info → enum_* → PNG → shutdown
& $py -m msim_com --open 'C:\path\design.ms14'
& $py -m msim_com --image 'D:\out\com_test.png'
```

自测输出每步一行 JSON：`{"step": "connect", "result": {...}}`，失败标 `{"step": ..., "result": {"error": {...}}}`，
末尾打印 `== OK ==` 或 `== FAILED: ... ==`。**这一步会新起一个隐藏 multisim.exe，结束时通过 `shutdown()` 关闭它。**

合规用法：

```python
from msim_com import MultisimSession
s = MultisimSession()      # 不碰 COM
s.connect()                # 拉隐藏实例
s.new_circuit()
print(s.enum_outputs())    # {'items': [], 'count': 0} —— 空电路就是这样
s.shutdown()               # 关掉它；不调会残留到进程退出
```

---

## 6. 术语与单位

| 词 | 含义 |
| --- | --- |
| 输出名（output） | 探针/仪器名，`EnumOutputs` 给出的字符串。**不是** `V(out)` 这种 SPICE 节点名 |
| 电路参数（circuit parameter） | 设计的全局参数，用 `EnumCircuitParameters` 列、`CircuitParameterValue` 读写 |
| 分区（section） | 多段器件里的一段，如 `U1A` 对应 `EnumComponents` 的 `U1` 加 `EnumSections("U1")` |
| RLC 值 | SI 基准单位：欧姆、法拉、亨利。写 `4.7e-6`，不要写 `"4.7u"` |
| `state` / `state_name` | `SimulationState` 整数与名字，`0=Stopped 1=Running 2=Paused`；未知值给 `Unknown(n)` |

---

## 7. 类型库事实

### 7.1 注册信息（实测）

```
ProgID             MultisimInterface.MultisimApp        (默认值: "MultisimApp Object")
CLSID              {D9CBB7A1-6AD6-438B-AD1B-42FB2DB00CAE}
AppID              {D360240D-8F60-4FBC-AEC2-B6E14C3042B8}
TypeLib            {D360240D-8F60-4FBC-AEC2-B6E14C3042B8} 1.0  ("NI Circuit Design Suite Multisim 14.3")
InprocServer32     C:\Program Files (x86)\National Instruments\Circuit Design Suite 14.3\MSInterface.dll
                   ThreadingModel = apartment,  FileVersion 14.3.1482.0
```

`InprocServer32` 存在但**没有 `LocalServer32`**：这是个进程内公寓 DLL，
它自己去拉起/接上 `multisim.exe`。这解释了为什么 `ThreadingModel = apartment` 决定了 worker 必须
在一个 STA 线程上串行调用（`_ensure_com()` 里先 `pythoncom.CoInitialize()`）。

### 7.2 `RLCValue` 是 PROPERTYPUT

`IMultisimCircuit.RLCValue` 在类型库里是**属性**：读取用 `circuit.RLCValue("R1")`，
写入走 pywin32 生成的 `SetRLCValue(componentName, value)`（同名属性的 propget/propput 包装）。
**类型库层面没有独立的 `SetRLCValue` 方法**，不要按方法去 `GetIDsOfNames` 找——按方法找得到的是属性包装。
实现里 `set_rlc()`（`msim_com.py:914-927`）就是取 `getattr(circuit, "SetRLCValue")`，
取不到直接抛：

```
the COM binding exposes no SetRLCValue; cannot write RLC values
detail: expected IMultisimCircuit.SetRLCValue(componentName, value)
```

### 7.3 数组编组

pywin32 3.13 没有 `SafeArray` 辅助函数，实现里直接靠 `VARIANT(VT_ARRAY | VT_R8, payload)` 的编组行为：

| 函数 | 载荷 | 得到的维度 | 用在哪 |
| --- | --- | --- | --- |
| `bstr_array(values)` | `[str, ...]` | 1 维 `VT_BSTR` | 所有分析的输出名参数 |
| `r8_array(values)` | `[float, ...]` | 1 维 `VT_R8` | `SetInputDataSampled` |
| `r8_matrix(rows)` | `[[float, float], ...]` | **2 维** `VT_R8` | `SetInputDataRaw` |

### 7.4 错误文本清理

Multisim 把 UTF-8 字节当 BSTR code point 返回，中文会变成 `"è¾åºæªæ¾å°ã"` 这种乱码。
`clean_error(text)` 做 `text.encode("latin-1").decode("utf-8", errors="replace")` 回转；
字符串本身已是真 CJK 码点时 `latin-1` 编码会失败，此时原样返回。
**任何来自 COM 的字符串都必须过这一层**，否则报错信息全是乱码、无法判断原因。

`_guard(label, func, ...)` 把所有 COM 异常包成 `MsimError("COM_ERROR", ...)`，
并把 `LastErrorMessage` 拼进 message：

```
<label> failed: <clean_error(exc)> | LastErrorMessage: <circuit.LastErrorMessage>
```

---

## 8. 会失败的情况（症状 → 原因 → 规避）

这是本文件最重要的一节。每条都给出**原文症状**。

### 8.1 没有探针/仪器的设计：所有分析必失败

| 项 | 原文 |
| --- | --- |
| 症状 | 抛 `com_error(-2147467259, '未指定的错误')` |
| 真实原因 | `LastErrorMessage` = **`输出未找到。`** |
| `EnumOutputs` | 恒为 `{"items": [], "count": 0}`——空列表**不是错误** |
| 波及成员 | `DoDCOperatingPoint` / `DoACSweep` / `DoACSingleFrequency` / `SetOutputRequest` / `OutputReady` / `GetOutputData` / `PauseSimulation` / `ResumeSimulation` / `StopSimulation` 全部如此 |

工具层表现：`run_*` 不抛异常，而是返回 `timed_out: true` + `missing_outputs: [...]` + `poll_error`
（`OutputReady` 连续 2 秒每次都抛就放弃）；`stop`/`pause`/`resume` 把它吞成 `warning`。

**规避**：先 `msim_inspect what="outputs"`。拿到空列表就别再调 `msim_analyze`，
改用 `msim_spice` 拿网表直接喂 SPICE 引擎（第 9 节）。要在 COM 通路上出波形，必须先在设计里放探针/仪器并保存。

### 8.2 空电路上调 `save()` → Multisim 堆损坏

| 项 | 内容 |
| --- | --- |
| 症状 | `multisim.exe` 进程崩溃退出，退出码 **`0xC0000374`**（堆损坏，不是可捕获的 COM 异常） |
| 触发 | 从未保存过的电路（`FileName` 为空）调 `circuit.Save()` |
| 规避 | 实现里 `save()` 先读 `FileName`，为空**直接抛** `BAD_ARGS`：`circuit has no file name yet; call save_as(path) first`；detail 写明 `Multisim's Save() on a never-saved circuit crashes the process with a heap corruption (0xC0000374).` |
| 正确姿势 | 新电路一律先 `msim_circuit action="save_as"` 给路径，之后才能 `save` |

### 8.3 `app.OpenFile()` 对 `.cir` 网表会静默崩溃

| 项 | 内容 |
| --- | --- |
| 症状 | 进程无输出直接退出，没有任何可捕获的错误 |
| 触发 | `app.OpenFile("<x>.cir")` |
| 规避 | `open_circuit()` 只允许 `.ms14`（`OPENABLE_EXTENSIONS = (".ms14",)`），其它后缀抛 `BAD_ARGS`：`open_circuit only supports .ms14 files; got '.cir'`，detail 指向 GUI 通道 |
| 正确姿势 | `.cir` / `.net` 用 `msim_gui action="open_file"`（WM_DROPFILES 注入，唯一可靠的打开方式）。工具层 `msim_circuit open` 已自动路由 |

### 8.4 `ReplaceComponent` 引发同样的堆损坏，默认被拒

| 项 | 内容 |
| --- | --- |
| 症状 | 自动化服务器堆损坏，退出码 `0xC0000374`，整个 worker 跟着死 |
| 根因 | `IMultisimCircuit.ReplaceComponent`（**DISPID 29**）在类型库里声明了第 8 个 `[out]` 参数，pywin32 生成的桩从不转发它；Multisim 往调用方从没提供的指针上写 |
| 实测范围 | 7 参数早绑定、7 参数晚绑定、7 参数裸 `IDispatch` **全部**打死 `multisim.exe`；显式补第 8 个参数得到 `DISP_E_BADPARAMCOUNT (-2147352562)` |
| 规避 | `REPLACE_COMPONENT_ALLOWED` 默认 False，`replace_component()` 先抛 `COM_ERROR` 说明原因，**不进入 COM 调用** |
| 想试 | 设环境变量 `MSIM_ALLOW_REPLACE_COMPONENT=1`（值为 `1/true/yes/on` 之一）。**这会崩掉 worker，只在一次性的独立进程里试** |

替代方案：改值用 `set_rlc` / `set_parameter`；要换器件就重新画或换设计文件——没有安全的自动替换路线。

### 8.5 `SaveAs()` 之后 `FileName` 属性仍是空

| 项 | 内容 |
| --- | --- |
| 症状 | `save_as(path)` 成功、文件确实落盘（返回 `bytes` 就是证据），但紧接着 `circuit_info()` 的 `file` 字段仍是空串 |
| 含义 | **判断"存过没有"不能靠 `FileName`**，要靠 `circuit_info()` 的 `name`、或干脆自己记住 `save_as` 给的 `path` |
| 影响 | 这跟 8.2 是同一处属性，所以 `save()` 的"拒绝"判据对被 `SaveAs` 过的电路同样可能误判——`save()` 会继续抛 `BAD_ARGS`。用 `save_as` 再存一次是安全做法 |
| 规避 | 落盘用 `msim_circuit action="save_as"`（COM 通道，实测写盘成功）重复调用；不要指望 `save` |

### 8.6 `ReportNetlist` / `ReportBOM` 的返回值有两种形态

| 项 | 内容 |
| --- | --- |
| 形态 A | 传了 `FileName`（第三个参数给了路径）→ 返回**路径字符串**，报告正文写进文件 |
| 形态 B | 没传 `FileName` → 返回**报告正文**字符串，不落盘 |
| 实现 | `report_netlist()` / `report_bom()` 一律传路径（形态 A），但 `_locate_output()` 对"返回空串 / 返回别的扩展名 / 追加扩展名"都做了兜底探测；`GetCircuitImage` 是**替换**扩展名，`CreateSnippet` 是**追加**扩展名 |
| 未落盘时 | 抛 `COM_ERROR`：`ReportNetlist reported success but wrote no file`，detail 里带 `requested=... returned=...` |
| 内容可读性 | 网表里中文元件名是 Multisim 内部 `_ucXXXX` 编码，**不是干净 SPICE**。别把它当可读标签，也别把它当可仿真网表 |

### 8.7 `SetInputDataRaw` 需要 2 维 float 数组

| 项 | 内容 |
| --- | --- |
| 症状 | 传 1 维数组报原文：`expected an array with 2 dimensions ... instead received an array with 1 dimensions` |
| 规避 | `r8_matrix(pairs)` 传 `[[t0, v0], [t1, v1], ...]`。工具层 `set_input_raw` 两种输入都接受，内部一律转 2 维 |
| 配套 | 每个输入先 `ReserveInput(name)`；`set_input_sampled` 反过来要 1 维，别搞混 |

### 8.8 其它必须在用前知道的边界

| 现象 | 原文 / 数值 | 规避 |
| --- | --- | --- |
| 64 位 Python 连不上 | `E_ACCESSDENIED (-2147024891)`；主数据库 `MSCOMP_S.ldb` 永不出现 | 必须 32 位 Python + 32 位 pywin32 |
| 权限级别过低 | 同上 `E_ACCESSDENIED` | DSH 需要 `danger-full-access`（Medium IL）；Low IL 下 `Connect()` 直接失败 |
| 首次 `Connect()` 偶发失败 | `app.Connect() failed after 6 attempts` | 实现已重试 6 次 / 间隔 1 s；仍失败就查位数与 IL |
| gen_py 缓存损坏 | `AttributeError`（生成模块缺 `CLSIDToClassMap` 等） | 已自动回退 `win32com.client.dynamic.Dispatch`；两条都失败才 `NO_MULTISIM` |
| `IsConnected` 仍为 False | `app.Connect() returned but IsConnected is still False` | 服务器拒了会话，重开 worker |
| 图像视口固定 | `BMP 960×720 32bpp = 2764854` 字节；视口外元件不出现在图里 | 要全图用可见窗口 + `msim_gui screenshot crop="content"` |
| 扩展名无用 | `GetCircuitImage` 按传入 `fmt` 决定内容，忽略后缀 | 信返回的 `format` 字段；实现会自动改名 |
| 没开电路就操作 | `NO_CIRCUIT`: `no circuit is open; call new_circuit() or open_circuit() first` | 先 `msim_circuit new` / `open` |
| 文件不存在 | `PATH_MISSING`: `file not found: <path>` | 用绝对路径 |
| 输出名给空 | `BAD_ARGS`: `outputs must contain at least one output name` | 先 `msim_inspect what="outputs"` |
| 传 `NaN` / `inf` 数值 | `BAD_ARGS`: `<label> must be a finite number` | 用有限值 |
| 采样率/点数非正 | `BAD_ARGS`: `sample_rate must be greater than zero` / `num_samples must be greater than zero` | 用正数 |
| AC 扫描范围非法 | `BAD_ARGS`: `start and stop must be greater than zero` / `stop must be greater than start` | 保证 `0 < start < stop` |
| 命令文件路径含空格 | Multisim 命令引擎拒绝带空格的命令文件路径（`msim_spice` 会直接 `BAD_ARGS`） | 输出目录别带空格 |
| 在可见窗口打开的设计 | 自动化通道看不到——两个独立进程 | 两条通道不共享状态，别混用 |

---

## 9. 什么时候不要用 COM 通道

COM 通道的价值是：**能改设计、能存盘、能导出、能跑带探针的分析**。
它的成本是：每次都要一个隐藏 Multisim 实例、分析依赖探针、崩溃会带走 worker。

按需求选路：

| 需求 | 走哪条 |
| --- | --- |
| 有探针/仪器的设计要做瞬态/直流/AC 分析 | COM：`msim_analyze` |
| 没有探针的设计要拿波形 | **`msim_spice`**（网表直接喂 SPICE 引擎，不需要探针、不需要原理图，实测单次分析 0.25–0.55 s） |
| 要改元件值 / 变体 / 电路参数并保存 | COM：`msim_edit` + `msim_circuit save_as` |
| 打开 `.cir` 网表 | GUI：`msim_gui open_file`（COM `OpenFile` 会崩） |
| 只想看设计里有什么 | `msim_design read`（解 `.ms14` 容器，不启动 Multisim） |
| 要原理图全图 | `msim_gui screenshot crop="content"`（COM 图像视口固定） |
| 要能仿真的干净网表 | 自己写 `.cir` 走 `msim_spice`；`ReportNetlist` 给的 `_ucXXXX` 编码串不是网表 |
| 数字逻辑库器件（`74LS00` 等） | COM 通道里的原理图有，`msim_spice` 里**不展开**；要 SPICE 里跑逻辑就用 `B` 源写表达式（如 `B1 y 0 V = 5 - 5*((V(a)>2.5) & (V(b)>2.5))` 是二输入 NAND） |

**诚实边界**：`msim_spice` 不经过 COM，它无法读原理图里的库器件模型；COM 分析无法在没探针的设计上工作。
两条路互不替代，别把"没有输出"当成故障去反复重试。
