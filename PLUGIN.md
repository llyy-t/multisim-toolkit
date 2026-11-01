<!--
  插件本体说明。本文件与 package.json / cordis.patch.yml / index.js / lib/ / worker/
  同属仓库根目录，所以 `dsh plugin add github:llyy-t/multisim-toolkit` 直接可装。
  面向模型的用法在 SKILL.md，安装与总览在 README.md。
-->

# dsh-multisim-toolkit

让 DeepSeek Harness 直接操控本机安装的 **Multisim 14.3**：建电路、改元件值、跑仿真分析、导出原理图与网表、抓可见窗口截图。

## 装了什么

九个模型可调用的工具，前缀 `msim_`：

| 工具 | 作用 |
| --- | --- |
| `msim_status` | 自检：找不找得到 32 位 Python、自动化会话连没连上、可见窗口在不在 |
| `msim_circuit` | 新建 / 打开 / 保存 / 另存 / 查询当前电路 |
| `msim_inspect` | 读：元件、仿真输入输出、变体、分区、电路参数、RLC 值 |
| `msim_edit` | 改：RLC 值、电路参数、活动变体、替换库元件 |
| `msim_analyze` | 仿真：瞬态、直流工作点、AC 扫描、单频 AC、命令文件；以及状态/停止/暂停/继续 |
| `msim_export` | 导：原理图图片（PNG/JPG/BMP）、网表、BOM、设计片段 |
| `msim_gui` | 可见窗口：启动/关闭、列窗口、置前、截图、拖放打开文件、关对话框、清数据库告警、另存为、发按键 |
| `msim_spice` | 纯 SPICE 网表引擎：网表 + 分析命令 → 表格 / CSV / 图（折线，单点分析自动改柱状图），不需要探针也不需要原理图 |
| `msim_design` | 离线读 `.ms14` 文件：解开 PKWare 容器，列出器件、参数值、连线/引脚数、保存版本，不启动 Multisim |

外加一段 system prompt 小节，告诉模型这几条通道互不共享状态、以及哪些事它做不到。

## 四条通道

```
DSH Host 插件 (index.js)
        │  TCP + JSON 行协议
        ▼
常驻 32 位 Python worker (worker/msim_worker.py)
        ├── com.*    → msim_com.py   → Multisim 自动化实例（隐藏的，多起一个）
        ├── gui.*    → msim_gui.py   → 可见的 multisim.exe 窗口（Win32 消息）
        ├── spice.*  → msim_deck.py  → COM 的 DoCommandLine → 纯 SPICE 引擎
        │                └── msim_spice.py（raw 解析）+ msim_raster.py（PNG 绘图）
        └── design.* → msim_ms14.py  → 直接读磁盘上的 .ms14（不碰 COM）
                          └── bin/blast_harness.exe（PKWare DCL 解压）
```

- **自动化通道**快、可脚本化、看不见。它打开的是**另一个隐藏的 Multisim 实例**，跟用户屏幕上那个无关。
- **可见窗口通道**用 Win32 消息操作真实窗口。它是**另一个进程**，在那边打开的电路，自动化工具看不到。
- **SPICE 通道**喂的是网表文本，Multisim 只负责算：不需要探针、不需要原理图、不需要窗口，一次调用可以连跑 op/dc/ac/tran 等多个分析并直接出图。
- **离线读取通道**根本不启动 Multisim，纯字节层面解析 `.ms14`，因此对「Multisim 打不开 / 不想弄乱前台」的设计同样有效。
- 前两条通道**不能混用一个实例**：COM `Connect()` 永远新起一个隐藏实例，附着不到用户手动打开的窗口（已实测）。


worker 必须常驻：Multisim 的 Automation COM 服务器与发起 `Connect()` 的客户端进程同生命周期，客户端一退出 `multisim.exe` 就消失，所以不能每条命令起一个短命进程。

## 依赖

- 本机装好 Multisim 14.3（默认路径 `C:\Program Files (x86)\National Instruments\Circuit Design Suite 14.3\multisim.exe`）。
- **32 位** Python + pywin32。32 位是硬要求：Multisim 的 COM 服务器是 32 位进程。
  查找顺序：插件 config 的 `pythonPath` → `MSIM_PYTHON` → `MULTISIM_MCP_PYTHON` → `MULTISIM_MCP_WORKER_PYTHON`
  → `%DSH_HOME%\multisim-mcp\py32\python.exe` → `C:\Python313-32\python.exe` → `C:\Python312-32\python.exe`。
- 不需要 node 侧任何依赖，不需要联网。

## 配置

```yaml
- id: multisim-toolkit
  name: 'dsh-multisim-toolkit'
  config:
    pythonPath: 'C:\Users\me\.dsh\multisim-mcp\py32\python.exe'   # 可选
    startTimeoutMs: 30000                                          # 可选，worker 启动超时
```

## 已知边界（都是实测结论，不是待办）

- `msim_circuit open` 对 `.cir` / `.net` 网表会**改走可见窗口**：自动化服务器的 `OpenFile` 打开 SPICE 网表会静默崩掉 Multisim。
- **没放探针/仪器的设计做不了 COM 分析**：`EnumOutputs` 返回空，`DoDCOperatingPoint` / `DoACSweep` 等只会回 `输出未找到。`。这类设计改用 `msim_spice`——它把网表直接喂给 Multisim 的 SPICE 引擎，不经过原理图；实测单次分析 0.25–0.55 秒。
- `msim_spice` 只认标准 SPICE3 元件（R/C/L/V/I/D/Q/M/E/F/G/H、`B` 行为源、`.model`、`PULSE`/`SIN`）。`XU1 ... 74LS00` 这类库器件在命令引擎里**不展开**，y 节点根本不会出现在结果里；要数字逻辑请用 `B` 源自己写表达式。
- **数字逻辑的可行做法（已实测）**：用 `B` 源 + 比较 + 逻辑运算符写门，例如 `B1 y 0 V = 5 - 5*( (V(a)>2.5) & (V(b)>2.5) )` 就是一个二输入 NAND。`&`、`|`、`abs()`、`min()`、`max()` 都实测可用，AND/NAND/OR/XOR 可一次跑通。
- **`msim_spice` 支持内联 `.subckt`（已实测）**：`X1 a b y NAND2` 配 `.subckt NAND2 ... .ends` 能正常实例化并仿真；子电路内部节点出现在结果里叫 `V(节点名)`，内部源电流叫 `I(源名:x实例名)`。所以复杂电路可以拆成子电路写。
- **网表必须是纯 ASCII、且必须有接地元件**：带 UTF-8 BOM 或首个注释行含中文，会让引擎报 `No nodes present in circuit`；没有任何接地元件则报 `No ground node was found`。插件现在会剥掉 BOM、把注释行转写为 ASCII，正文含非 ASCII 直接回 `BAD_ARGS` 并说明原因。
- **单点分析（`op`）自动画成柱状图**：直流工作点每个变量只有一个取值，连成折线没有意义；现在每个变量一根柱并标出数值，可用 `series` 只挑想看的量。
- 出图默认规则：给了 `series` 就只画那几列；没给时若存在 `(dB)` 列则只画 dB 列（dB 和相位混在同一根纵轴上读不出来）。
- **网表拖进可见窗口会变成真原理图（已实测）**：`msim_circuit open` 一个 `.cir` 会落到可见窗口，Multisim 会把网表还原成带真实库器件和连线的可编辑原理图（R/C/V 等都有图形符号，不是文本框）。但**这个原理图在 GUI 进程里，自动化通道看不到**——两条通道不共享实例，别指望拖完再用 `msim_inspect` 读它。
- `msim_spice` 的命令文件路径**不能含空格**（Multisim 命令引擎的限制）：`output_dir` / `netlist_path` 带空格会直接报 `BAD_ARGS`，默认落在 `%TEMP%\msim_deck_<毫秒>`。
- `msim_spice` 需要有一个打开的文档才能跑（`DoCommandLine` 挂在文档上），没有时会自动 `NewFile()` 建一个空白设计（返回 `created_circuit: true`）；**已打开的设计不会被替换**。
- 空电路上调 `save()` 会崩 Multisim，所以实现里直接抛 `BAD_ARGS`，请在保存前先 `save_as`。
- 没放探针/仪器的设计不暴露任何仿真输出（`EnumOutputs` 返回空），此时分析只会超时——这是设计属性，不是故障。
- `msim_export` 的图片由 Multisim 自己渲染，**视口固定**：滚动到画面外的元件不会出现在图里。要看全图，用可见窗口 + `msim_gui` 截图：`crop: "paper"` 是整张图纸（保留边框与行列号，元件在原位），`crop: "content"` 只框住电路本体（四周 18 px 留白）。
- BOM / 网表里的中文元件名是 Multisim 内部 `_ucXXXX` 编码，别当可读标签用。
- `msim_export kind="snippet"` 写出的是**设计片段**：一个 PNG 容器，设计数据嵌在 PNG 的私有块里。所以 Multisim 会自己给文件名**追加** `.png`（`GetCircuitImage` 是替换扩展名，`CreateSnippet` 是追加），返回的 `path` 是真实落盘路径。它不能被 `msim_circuit open` 当电路打开，只用于把片段拖回 Multisim。
- 分析报 `输出未找到。` 时，先 `msim_inspect what="outputs"`：空列表意味着这个设计没有任何探针/仪器输出，任何分析都拿不到波形。工具会直接把这句话附在错误后面。
- **GUI 通道的 `save_as` 只对「未命名」文档有效**：Multisim 14.3 的 Save As 对话框只在文档还没有文件名时才会弹出；已有文件名时 `Ctrl+S` / `Ctrl+Shift+S` 都是**原地保存**，此时代码直接报错而不是假装成功。**要按指定路径落盘，一律用 `msim_circuit save_as`（COM 通道，实测写盘成功）。**
- **文件名必须用真实按键输入，不能用 `WM_SETTEXT`**：`WM_SETTEXT` 只是把字打进编辑框，对话框内部状态仍记着旧名字，于是 Multisim 会把未命名设计存成 `我的文档\National Instruments\Circuit Design Suite 14.3\设计1.ms14`，**静默覆盖同名真实文件**（本插件开发期就撞过一次）。因此 `save_as` 走 `SendInput` + `KEYEVENTF_UNICODE` 逐字符输入，并在按保存前回读校验路径，不一致就放弃保存。
- 可见实例每次启动常弹「主数据库 无法访问」「企业数据库 无法访问」两个 `#32770`；它们会吞掉后续按键。这些告警的文字在跨进程 `Edit` 控件里，`GetWindowText` 读出来是空串，必须用 `WM_GETTEXT` 才认得出。`msim_gui` 的关键动作（`save_as`）内部会先点掉它们；其它动作若发现按键没反应，跑一次 `msim_gui dismiss_database`（点按钮，能真正关掉；`close_dialogs` 的 `WM_CLOSE` 对这两个框无效）。
- 让 MDI 文档拿到键盘焦点**只能靠点击文档区域**：`WM_MDIACTIVATE` 只改「活动子窗」标记，按键仍然送不到文档。点击前必须先把模态告警框清掉，否则点击会落在告警框上。
- 可见实例会随机自行退出（非本插件 kill 所致）。`msim_gui` 每次动作前都会重新定位主窗，找不到就报 `NO_MULTISIM`，此时用 `msim_gui launch` 重新拉起。
- `msim_gui kill` 默认**只关本插件 `launch` 启动的进程**；要关用户手工打开的实例必须显式给 `pid` 或 `all_instances: true`（后者会丢弃未保存工作）。
- **实机波形图走 `msim_gui` 的 `run_analysis` + `capture_graph`，这是唯一能拿到 Multisim 屏幕波形的路**：仿真菜单是 BCG 自绘的（`GetMenu` 返回 0，投递 `WM_COMMAND` 会把进程打崩），但 `alt+s` 打开菜单后按 3 次 `VK_DOWN` 再 `VK_RETURN` 就能稳定打开「Analyses and Simulation」；对话框里的 20 个分析项是标准 `ListBox`，用 `LB_GETTEXT`/`LB_GETCURSEL` 读、点 `(rect[0]+40, rect[1]+h//2+h*index)` 选中，再对 `Run` 按钮发 `BM_CLICK`。全程约 11 秒。
- 分析跑完后 Multisim 会弹一个独立顶层窗 **`图示仪视图`**（`class='#32770'`），里面才是真正的波形曲线、真值表、波特图。`capture_graph` 自动找这个窗并截图；它和电路图是两个不同的窗，要两张图就分别截。
- **跑仿真前设计必须真连线且有探针**：源悬空、仪器没接线的设计按 F5 会让 Multisim 内部堆损坏直接退出（`0xC0000374`，Windows 事件日志不留记录），看起来像「F5 必崩」其实是电路本身的问题。
- `msim_gui screenshot` 的 `crop` 四档：`none`（整窗）/`canvas`（绘图视图）/`paper`（整张图纸，保留边框与行列号）/`content`（只框电路本体 + 18px 留白）。图纸外的空白是 Multisim 的「工作区灰」`(171,171,171)`，`paper` 用这个颜色定位纸张右缘并裁掉。
- **`msim_design` 只读不写**：它把 `.ms14` 当压缩容器解开（36 字节魔数 + u64 总长 + 若干 `{declen, complen, 数据}` 块，数据是 PKWare DCL implode），不启动 Multisim、不产生任何文件。解压靠 `worker/bin/blast_harness.exe`（Mark Adler `blast.c` + 薄壳，源码同目录，`cl /nologo /O2 /Fe:blast_harness.exe blast_harness.c blast.c` 可重建）。
- `.ms14` 里的元件值有两种形态：`&ASC<文本>`（可读）和 `&UNI<可读部分>_uc<hex>`（尾部 `_uc` 是内部索引）。工具会把 `_uc` 后缀去掉；如果整个值都只是内部编码（如 `&UNI100_uc103a9`），会原样返回，因为比丢掉更有用。
- **本机 `%TEMP%` 对子进程写不进去**：由 32 位 Python 拉起的 `blast_harness.exe` 对 `%TEMP%` 调 `CreateFileW` 会拿到 error 5（32 位 Python 自己写同一路径却是正常的）。所以 `.ms14` 解码的临时目录**优先放在 `bundle/_ms14_tmp`**，可用环境变量 `MSIM_MS14_WORKDIR` 覆盖。
- NI 自带样例（`C:\Users\Public\Documents\National Instruments\Circuit Design Suite 14.3\samples\`）**没有 `<history>` 段**，所以 `created by:` 那行会是空的——这是文件本身如此，不是解析失败。

## 自测

worker 各模块都能单独跑：

```powershell
# COM 层
& $py32 -m msim_com
# GUI 层
& $py32 -m msim_gui --write out.png
# SPICE 通道（只解析与出图，不碰 COM，不需要已打开的电路）
& $py32 -c "import sys; sys.path.insert(0,'worker'); import msim_spice, msim_raster, msim_deck"
# .ms14 离线读取（同样不碰 COM）
& $py32 -c "import sys; sys.path.insert(0,'worker'); import msim_ms14; print(msim_ms14.summary(r'<某个>.ms14')['components'])"
# 整个 worker（会打印一行含端口的 JSON，等 TCP 客户端）
& $py32 worker\msim_worker.py --port 0 --token test
```

## 协议

`worker/PROTOCOL.md` 是模块之间的硬契约（TCP 格式、错误码、每个函数的签名、枚举值、环境事实）。
改任何一层之前先读它；改动了就同步改它。
