# msim worker protocol & module contract

这份文档是 **模块间契约**，写代码前先读完。改动契约必须先改本文件。

## 1. 进程模型

```
DSH Host (node, Medium IL)
  └── index.js  (Cordis 插件, 注册 msim_* 工具)
        └── spawn: <32-bit python> worker/msim_worker.py --port N --token T
              └── 常驻进程, 独占 COM 公寓, 通过 TCP 127.0.0.1:N 收命令
```

**为什么必须常驻**：Multisim Automation API 的 COM 服务器（`multisim.exe`）生命周期与
**第一个调用 `Connect()` 的客户端进程**绑定。客户端进程一退出，multisim.exe 就消失。
所以不能"每条命令起一个短命进程"。worker 必须活到 DSH 会话结束（或显式 shutdown）。

**为什么用 TCP 而不是 stdio**：Host 侧 `subprocess` 服务只暴露非消费式的
`collected.stdout.readFrom(offset)`，没有数据到达事件；轮询行缓冲的 JSON 协议容易撕裂。
TCP 的 `net.Socket` 有 `data` 事件，行协议简单可靠。stdout/stderr 仅用于崩溃诊断。

## 2. 传输格式

TCP，UTF-8，**一行一个 JSON 对象**（`\n` 分隔，禁止在 JSON 里放裸换行）。

请求：
```json
{"id": 7, "op": "run_transient", "args": {"outputs": ["V(out)"], "duration": 0.001}}
```

应答（成功）：
```json
{"id": 7, "ok": true, "result": {...}}
```

应答（失败）：
```json
{"id": 7, "ok": false, "error": {"code": "COM_ERROR", "message": "…", "detail": "…"}}
```

`id` 由客户端生成，单调递增；worker 原样回填。单连接、串行执行（一次一条请求）。

### 握手

连接后客户端第一件事：
```json
{"id": 0, "op": "hello", "args": {"token": "<spawn 时通过 --token 传入的 token>"}}
```
worker 校验 token，回：
```json
{"id": 0, "ok": true, "result": {"protocol": 1, "pid": 12345, "python_bits": 32, "python": "3.13.12"}}
```
token 不匹配 → 回 error 并关闭连接。服务端只绑 `127.0.0.1`。

### 错误码（固定集合）

`BAD_ARGS`（参数不合法）、`NO_CIRCUIT`（还没开电路）、`NO_MULTISIM`（COM 激活失败）、
`COM_ERROR`（COM 调用抛异常）、`TIMEOUT`（等输出超时）、`PATH_MISSING`（文件不存在）、
`GUI_ERROR`（Win32 层失败）、`INTERNAL`（未预期异常）。

worker **永不退出**（除了 `shutdown` op 或父进程消失）。单条 op 抛异常只影响该条应答。

## 3. Python 模块契约

`worker/` 下七个实现文件 + `bin/` 解码器 + 本文件：

| 文件 | 责任 | 归属 |
| --- | --- | --- |
| `msim_worker.py` | TCP 服务、参数校验、dispatch、COM 公寓初始化、父进程守护 | Lead |
| `msim_com.py` | Multisim COM 层，一个 `MultisimSession` 类 | com-worker |
| `msim_gui.py` | Win32 GUI 层（可见实例）、PNG 编码 | gui-worker |
| `msim_deck.py` | 纯 SPICE 通道：网表 + 分析命令 → `DoCommandLine` → 表格/CSV/PNG | Lead |
| `msim_spice.py` | SPICE3 raw 解析、CSV 写出、折线图绘制（无 COM 依赖） | Lead |
| `msim_raster.py` | 纯 stdlib 的 PNG 画布与点阵字体（py32 没有 PIL） | Lead |
| `msim_ms14.py` | `.ms14` 离线读取：PKWare 容器解压 + XML 摘要（无 COM 依赖） | Lead |
| `bin/blast_harness.exe` | PKWare DCL implode 解压器（+ `blast.c`/`blast.h`/`blast_harness.c` 源码） | Lead |

`msim_deck` 只依赖 `msim_spice` / `msim_raster` 与被传入的 session，
**不得** import `msim_gui`；`msim_spice` / `msim_raster` / `msim_ms14` 不 import 任何 COM 模块。

铁律：
- `msim_com.py` **不得** import `msim_gui`，`msim_gui.py` **不得** import `msim_com`。
  两者只通过 `msim_worker.py` 组合。GUI 走**可见的 multisim.exe 进程**，COM 走
  **隐藏的自动化实例**，是两个不同的进程，互不可及（已实测，见第 5 节）。
- 两个模块都自己 `import pythoncom` / `win32com` / `win32gui` 等，不依赖对方初始化。
- 所有对外函数返回 **可 JSON 序列化的 dict**（只含 str/int/float/bool/list/dict/None）。
  bytes 一律转 base64 或直接落盘换算成 `{"path":…, "bytes":N}`。
- 抛异常用各自模块里的 `MsimError(code, message, detail=None)`；`msim_worker.py`
  捕获后转成第 2 节的 error 对象。**不要把 traceback 塞进 message**，放 `detail`。
- 中文：Multisim 把 UTF-8 字节当 BSTR code point 返回，任何来自 COM 的字符串都要过
  `clean_error()`（`text.encode("latin-1").decode("utf-8", errors="replace")`），否则中文电路名是乱码。
- 不要 `print()` 到 stdout（stdout 是 Host 的崩溃诊断通道，写脏日志会干扰）；需要日志用
  `sys.stderr`。

## 4. 各模块要实现的接口（签名即契约）

### 4.1 `msim_com.py`

```python
class MsimError(Exception):
    def __init__(self, code: str, message: str, detail: str | None = None) -> None: ...

class MultisimSession:
    def __init__(self) -> None: ...          # 不做任何 COM 激活
    # —— 会话 ——
    def ping(self) -> dict: ...              # {"connected":bool,"version":str|None}
    def connect(self) -> dict: ...           # {"connected":True,"version":…,"path":…}
    def shutdown(self) -> dict: ...          # 调 Disconnect，释放 app；{"disconnected":True}
    # —— 电路文件 ——
    def new_circuit(self) -> dict: ...       # circuit_info()
    def open_circuit(self, path: str) -> dict: ...   # 只允许 .ms14；别的后缀抛 BAD_ARGS
    def save(self) -> dict: ...              # {"path": str}
    def save_as(self, path: str) -> dict: ...
    def circuit_info(self) -> dict: ...      # {"name","file","state","state_name","last_error"}
    # —— 读电路 ——
    def enum_components(self, component_type: int = 0) -> dict: ...  # {"items": [str], "count": int}
    def enum_inputs(self, io_type: int = 0) -> dict: ...
    def enum_outputs(self, io_type: int = 0) -> dict: ...
    def enum_variants(self) -> dict: ...
    def get_active_variant(self) -> dict: ...     # {"variant": str}
    def set_active_variant(self, name: str) -> dict: ...
    def enum_sections(self, component: str) -> dict: ...
    def enum_parameters(self, level: int = 1) -> dict: ...   # {"items":[str]}
    def get_parameter(self, name: str) -> dict: ...          # {"name","value": float}
    def set_parameter(self, name: str, value: float) -> dict: ...
    def get_rlc(self, component: str) -> dict: ...           # {"component","value": float}
    def set_rlc(self, component: str, value: float) -> dict: ...
    def replace_component(self, component, section, database, group, family, name, model) -> dict: ...
    # —— 导出 ——
    def get_image(self, path: str, image_format: str = "png") -> dict:
        ...  # {"path","bytes","format","width","height"}；fmt 以实际内容为准并修正扩展名
    def report_netlist(self, path: str, probes: bool = False, fmt: str = "text") -> dict: ...
    def report_bom(self, path: str, real: bool = False, fmt: str = "text") -> dict: ...
    def snippet(self, path: str, selection_only: bool = False, zoom: float = 1.0) -> dict: ...
    # —— 仿真 ——
    def sim_state(self) -> dict: ...          # {"state": int, "state_name": str}
    def stop(self) -> dict: ...
    def pause(self) -> dict: ...              # 失败也返回 ok:false 语义的 dict，不抛
    def resume(self) -> dict: ...
    def set_output_request(self, output: str, method: int = 0, sample_rate: float = 1e6,
                           num_samples: int = 1000, repeat: bool = False) -> dict: ...
    def clear_output_request(self, output: str) -> dict: ...
    def output_ready(self, output: str) -> dict: ...          # {"output","ready": bool}
    def get_output_data(self, output: str, max_points: int = 2000) -> dict:
        ...  # {"output","method","shape":[nrow,ncol],"n_points","sampled_points","rows":[[...]]}
    def run_transient(self, outputs: list[str], duration: float, sample_rate: float = 1e6,
                      num_samples: int = 1000, repeat: bool = False, timeout: float = 60.0,
                      max_points: int = 2000) -> dict:
        ...  # {"outputs": {name: <get_output_data 的形状>}, "state": int, "elapsed": float}
    def run_dc(self, outputs: list[str], timeout: float = 60.0, max_points: int = 500) -> dict: ...
    def run_ac_sweep(self, outputs: list[str], sweep_type: int = 0, num_points: int = 10,
                     start: float = 100.0, stop: float = 1e6, timeout: float = 120.0,
                     max_points: int = 2000) -> dict: ...
    def run_ac_single(self, outputs: list[str], frequency: float = 1000.0,
                      timeout: float = 60.0, max_points: int = 200) -> dict: ...
    def set_input_raw(self, name: str, values: list[float], repeat: bool = False) -> dict: ...
    def set_input_sampled(self, name: str, sample_rate: float, values: list[float],
                          repeat: bool = False) -> dict: ...
    def clear_input(self, name: str) -> dict: ...
    def run_command_file(self, command_file: str, log_file: str, timeout: float = 120.0) -> dict:
        ...  # {"state","timed_out","elapsed","log_exists","log"(截断)}；超时会先 StopSimulation
```

`run_transient` / `run_dc` / `run_ac_sweep` / `run_ac_single` 的返回统一为
`{"analysis": "<name>", "outputs": {name: {...}}, "elapsed": float, "state": int, "timed_out": bool, "missing_outputs": [...]}`。

枚举取值（严格照抄，来自类型库）：
- `SimulationState`: Stopped=0, Running=1, Paused=2
- `SimulationIOType`: All=0, Voltage=1, Current=2, Power=3, Digital=4
- `SimulationInterpolation`: Raw=0, ForceStep=1, Linear=2, Spline=3, Coerce=4
- `SweepType`: Decade=0, Octave=1, Linear=2
- `ExportFormat`: Text=0, CSV=1
- `ComponentType`: All=0, AllRLC=1, Resistor=2, Capacitor=3, Inductor=4
- `MultisimDB`: Master=0, Corporate=1, User=2
- `CircuitImageFormat`: PNG=0, JPG=1, BMP=2
- `CircuitParameterLevel`: All=0, TopLevel=1

字符串入参（`image_format` / `fmt`）在模块内部映射成上面的整数。

### 4.3 `msim_deck.py` / 纯 SPICE 通道（`spice.run`、`spice.verify`）

```python
class DeckError(Exception):
    def __init__(self, code: str, message: str, detail: str | None = None) -> None: ...

def run_deck(session, netlist=None, netlist_path=None, commands=None, output_dir=None,
             max_points=200, plot=True, timeout=180.0, series=None) -> dict: ...
```

- 返回 `{"directory","netlist","command_file","log_file","state","elapsed_seconds",
  "created_circuit","commands","analyses":[{"command","plotname","columns","n_points",
  "sampled_step","rows","summary","csv","png"}],"missing"}`。
- `series` 只影响出图：给就只画这些列（名字与 `columns` 一致），不给按 `_pick_series` 的默认规则。
- 工作目录**不能含空格**（Multisim 命令引擎拒绝带空格的命令文件路径），
  含空格直接抛 `BAD_ARGS`；默认 `%TEMP%\msim_deck_<毫秒>`。
- `DoCommandLine` 挂在文档上，所以没有打开的设计时先 `NewFile()`（`created_circuit: true`）；
  **已打开的设计绝不替换**。
- 一个命令对应一个 `write plotN.raw`，避免后一个分析覆盖前一个。
- Multisim 写的是 **ASCII 文本 raw**（`Flags: real|complex` 是文字，不是数字），
  复数列在解析阶段就归约成 `(dB)` + `(phase deg)` 两列。
- `commands` 里禁止 `source` / `write` / `quit` / `shell` / `cd` / `exec` / `system`（模块自己拥有）。
- 只支持标准 SPICE3 元件；`X... 74LS00` 这类库器件在命令引擎里**不会展开**。
- **内联 `.subckt` 可用**（实测）：`X1 a b y NAND2` + `.subckt NAND2 … .ends` 正常实例化；
  内部节点记为 `V(<node>)`，内部源电流记为 `I(<name>:<xinst>)`。
- **网表与命令文件必须纯 ASCII**：`normalise_netlist` 会剥 UTF-8 BOM、把注释行转写为 ASCII，
  正文含非 ASCII 抛 `BAD_ARGS`。缺接地元件时引擎报 `No ground node was found`，
  随后 `No nodes present in circuit`。

### 4.4 `msim_spice.py` / `msim_raster.py`

```python
# msim_spice.py —— 纯 stdlib，不碰 COM
normalise_netlist(netlist: str) -> str
normalise_commands(commands) -> list[str]
build_command_file(netlist_path, raw_paths, commands) -> str
parse_raw(data: bytes) -> dict          # {plotname, columns, n_points, rows, ...}
sample_rows(rows, max_points) -> tuple[list, int]
summarise(parsed) -> list[dict]         # 每列 count/first/last/min/max/mean
write_csv(path, parsed) -> dict
write_plot_png(path, parsed, series=None, title=None, x_column=None,
               width=900, height=520, log_x=None, raster=None) -> dict
_write_bar_png(path, parsed, title, width, height, raster, values=None) -> dict

# msim_raster.py —— zlib PNG 画布（py32 没有 PIL）
class Canvas(width, height, background=(255, 255, 255)):
    line(...), polyline(...), fill_rect(...), text(...), text_center(...), png() -> bytes
nice_ticks(low, high, count=5), decade_ticks(log_low, log_high), tick_label(value)
```

- 画图默认只画 `(dB)` 列（存在时）：dB 与角度混在同一根 y 轴无法阅读。
- `log_x` 默认在 x 列为 frequency/freq 且全为正时启用。
- `write_plot_png` 的 `raster` 参数就是传入 `msim_raster` 模块本体（依赖注入，便于换实现）。
- **只有 1 个采样点时自动改走 `_write_bar_png`**：直流工作点每个变量只有一个值，
  连成折线是空图；柱状图每根柱标出数值，`series` 用于挑列。返回里多一个 `"kind": "bar"`。

### 4.5 `msim_ms14.py`（`design.read`）

```python
MAGIC = b"MSMCompressedElectronicsWorkbenchXML"   # 36 字节
HEADER_LEN = len(MAGIC) + 8                        # 44；再加每块 8 字节 → 单块文件头 52
DECODER = <worker>/bin/blast_harness.exe
WORKDIR_ENV = "MSIM_MS14_WORKDIR"

class Ms14Error(Exception):
    def __init__(self, code: str, message: str, detail: str | None = None) -> None: ...

def decode(path: str) -> dict
    # {"path","compressed_bytes","declared_length","decoded_bytes","blocks","xml": bytes}
def inspect(xml_bytes: bytes) -> dict
    # {"root","schema","creator":{application,date,type,versionMajor,versionMinor,versionPatch},
    #  "components":[{"refdes","class","type","sections","value"}],
    #  "instruments":[str],"instrument_count","counts":{items,wires,symbols,pins},
    #  "top_tags","top_classes"}
def summary(path, include_xml=False, xml_limit=200000) -> dict   # decode + inspect 合并
def _work_root() -> str            # 挑一个 blast_harness 真能写的目录（带写探测）
def _explode(payload, work, index) -> bytes
def _human_value(text) -> str | None
```

容器布局（已定量确认，勿再推翻）：

```
0   36  魔数 MSMCompressedElectronicsWorkbenchXML
36   8  u64 解压后总长（小端）
44  循环 { u32 declen, u32 complen, complen 字节 PKWare DCL implode 数据 }
```

- 校验链：魔数 → `declared` 合理性 → 逐块 `declen` 与实解长度比对 → 总长与 `declared` 比对 → 首字节是 `<`。
- **解压必须用 `bin/blast_harness.exe`**：纯 Python 的 `pwexplode.py` 能解但慢 3 个数量级
  （1661573B 的设计要 118 秒），只作交叉验证用（两者 sha1 一致）。
- **临时目录不能落在 `%TEMP%`**：32 位 Python 拉起的 exe 对 `%TEMP%` 调 `CreateFileW` 得到 error 5，
  而 32 位 Python 自己写同一路径正常。`_work_root()` 按 `MSIM_MS14_WORKDIR` → `<dsh ws>/_ms14_tmp` → `%TEMP%`
  顺序做真实写探测后选一个。
- `blast_harness.c` 的 I/O 走 Win32（`CreateFileA`/`ReadFile`/`WriteFile`），不是 stdio——
  stdio 版本在这台机器上会被拒。重建：
  `cl /nologo /O2 /Fe:blast_harness.exe blast_harness.c blast.c`（vcvars64）。

### 4.2 `msim_gui.py`

```python
class GuiError(Exception):
    def __init__(self, code: str, message: str, detail: str | None = None) -> None: ...

MULTISIM_EXE = r"C:\Program Files (x86)\National Instruments\Circuit Design Suite 14.3\multisim.exe"

def launch(wait_seconds: float = 25.0, maximize: bool = True) -> dict:
    ...  # {"pid": int, "hwnd": int, "title": str, "elapsed": float, "already_running": bool}
def windows(visible_only: bool = False) -> dict: ...   # {"items":[{"hwnd","class","title","rect","visible","pid"}], "count"}
def main_window() -> dict: ...                         # {"hwnd","title","rect"}
def open_file(path: str, close_existing: bool = True, wait_seconds: float = 30.0) -> dict:
    ...  # WM_DROPFILES 注入；{"opened":True,"path","hwnd","dialogs_closed":[...]}
def screenshot(path: str, mode: str = "window", hwnd: int = 0, crop: str = "none",
               target_width: int = 0) -> dict:
    ...  # {"path","bytes","width","height","hwnd","crop_fallback","view_fallback"}
def focus(hwnd: int = 0) -> dict: ...                  # {"hwnd","foreground":bool}
def close_dialogs(keep: str = "") -> dict: ...         # {"closed":[{"hwnd","title","class"}]}
def dismiss_database_dialogs(limit: int = 8) -> dict: ...   # {"dismissed":[title,...]}
def save_as(path: str, wait_seconds: float = 20.0) -> dict:
    ...  # {"path","saved","dialog_title","edit_hwnd","button_hwnd","chord","saved_in_place","dismissed"}
def send_keys(keys: str, hwnd: int = 0) -> dict: ...   # 支持 ^s 之类；也支持单键名
def kill(force: bool = False, pid: int = 0, all_instances: bool = False) -> dict:
    ...  # {"killed":[pid],"remaining":int,"skipped":int}；默认只杀 launch() 起的
def probe() -> dict: ...                               # 环境自检：exe 存在? 有没有在跑的实例?
```

- `crop` 取值：`"none"`、`"canvas"`（裁到 MDI 里的 `AfxFrameOrView140u` 绘图视口，定位失败才回退
  `CANVAS_FRAC = (0.120, 0.124, 0.836, 0.763)`）、`"content"`（在上述视口内按「暗像素或有彩度像素」的
  包围盒再裁空白，留 `CONTENT_PAD = 18`）。判断「内容」的规则是 `min(r,g,b) < 100` 或
  `max-min > 16`：白纸、≈150 网格点、≈192 工作区灰、≈128 边框都算背景。只用「暗」判会漏掉全部
  蓝色导线（BGRA 里纯蓝的 B=255），这是曾经裁不干净的根因。
- 截图是**白底图**，PNG 用 `zlib` 自己编（本机 py32 **没有 PIL**）。窗口抓图用
  `PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT=2)`，失败回退 `BitBlt`。
- `save_as` 只对**未命名**文档有效（已命名的文档 `Ctrl+S` 是原地保存，此时抛 `GUI_ERROR` 而不是
  覆盖源文件）。文件名必须用 `SendInput` + `KEYEVENTF_UNICODE` 逐字符输入并回读校验：`WM_SETTEXT`
  只填编辑框、不改对话框内部状态，会让 Multisim 把未命名设计写到默认目录覆盖同名文件。
- `open_file` 的 WM_DROPFILES 注入是唯一可靠的打开方式（COM `OpenFile` 对 `.cir` 会崩，
  见第 5 节）。注入前若已有文档打开，先 `WM_CLOSE` 关 MDI 文档，否则弹模态框。

## 5. 环境事实（已实测，别推翻重探）

- 32 位 Python：`C:\Python313-32\python.exe`（3.13.12, 32-bit），
  有 pywin32-312（`win32gui/win32api/win32ui/win32process/win32com/pythoncom` 全在）、
  `zlib`、`multisim_mcp`；**没有 PIL / numpy / pandas**。
- `C:\Program Files (x86)\National Instruments\Circuit Design Suite 14.3\multisim.exe` 存在。
- 精度级别：DSH 需要 `danger-full-access`（Medium IL）。Low IL 下 COM `Connect()` 返回
  `E_ACCESSDENIED (-2147024891)`，且主数据库 `MSCOMP_S.ldb` 永不出现。
- COM ProgID `MultisimInterface.MultisimApp`；`app.Connect()` 会**新起一个隐藏的
  multisim.exe**（不能附着用户手动打开的实例，那个实例所有窗口 `vis=False`，
  `GetCircuitImage` 仍然可用）。
- 客户端进程退出 → multisim.exe 随之消失。`app.Disconnect()` 会**关掉 Multisim**，只能由
  显式 shutdown 调用。
- `GetCircuitImage(fmt, path)`：0=PNG、1=JPG、2=BMP，**扩展名无用，内容才准**；BMP 960×720 32bpp
  = 2764854 字节；图像只有固定视口，视口外元件不出现。
- `app.OpenFile()` 只对 `.ms14` 可靠；对 `.cir` 网表会**静默崩溃**（进程无输出退出）→
  `.cir` 必须走 GUI WM_DROPFILES。
- `EnumOutputs` 常为空，除非电路里放了探针/仪器；空列表不是错误。
- `ReportNetlist` 返回 Multisim 内部 `_ucXXXX` 编码串，不是干净 SPICE。真正的网表路线是
  「自己写 `.cir` → GUI 拖入 → 自动生成原理图 → 仿真」。
- 截图/窗口经验在 `D:\Multisim ds-skill\multisim-digital-lab\scripts\mslab.py`（549 行，可参考
  但它的 `shot()` 依赖 PIL，不能直接抄）。
- 主数据库目录 `C:\ProgramData\National Instruments\Circuit Design Suite\14.3\database\`，
  默认保存目录 `C:\Users\<user>\Documents\National Instruments\Circuit Design Suite 14.3\`。

## 6. 自测要求

各自模块都要能被单独跑起来自测（不经 worker）：

```powershell
$py = 'C:\Python313-32\python.exe'
& $py -c "import sys; sys.path.insert(0, '<bundle>\worker'); import msim_com as m; print(m.MultisimSession().connect())"
```

`msim_gui.py` 里放 `if __name__ == "__main__":` 自测入口，跑 `probe()` + 列窗口。
`msim_com.py` 同样放自测入口：connect → new_circuit → circuit_info → 导出 PNG → shutdown。

**实机验证是硬要求**：两个模块都必须在本机真跑通指定用例，把输出贴进完成报告。
