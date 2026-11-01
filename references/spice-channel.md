# SPICE 通道：网表直喂引擎（`msim_spice`）

一句话：**把网表文本直接交给 Multisim 自己的 SPICE 引擎（`DoCommandLine`）跑，结果拿回表格 / CSV / PNG。不需要探针、不需要原理图、不需要窗口。**

---

## 1. 为什么需要这条通道

Multisim 的 COM 分析接口（`msim_analyze` → `DoDCOperatingPoint` / `DoACSweep` …）只认**「探针或仪器产生的命名输出」**。一个刚建出来、没有放任何探针和仪器的设计，`EnumOutputs` 返回空列表，于是：

| 现象 | 真实原因 |
| --- | --- |
| `msim_inspect what="outputs"` 返回空 | 设计里没有任何探针 / 仪器 |
| 任何 `msim_analyze` 都超时 | 没有命名输出可测，引擎无从下手 |
| 分析报 `输出未找到。` | 同上，中文打包版本的提示原文 |

这不是故障，是设计属性。**这类设计必须换通道**，而不是反复重试分析。

SPICE 通道绕开了整条链路：

```
网表文本 ──> circuit.cir
          └> run.cmd（source + 每个分析命令 + write plotN.raw）
                 │
                 ▼
          Multisim 文档的 DoCommandLine
                 │
                 ▼
          plot0.raw / plot1.raw …（ASCII SPICE3 raw）
                 │
                 ▼
          解析 ──> 表格 + CSV（%.12g）+ PNG（msim_raster 自绘）
```

**能做的**：任意线性 / 非线性模拟电路、数字逻辑（用 B 源写）、多分析连跑。
**不能做的**：展开 Multisim 库器件（见 §2.2）。

典型时机：

- 设计是空的 / 只有器件没有探针，但要验证行为。
- 想跑一组参数扫描、开环扫频、噪声、失真、极零点，而原理图里没布对应仪器。
- 只想拿数值（CSV）自己画。
- 想验证一段网表语法，不想先在原理图里摆元件。

---

## 2. 支持的语法

引擎吃的是标准 **SPICE3** 子集。接受的元件：`R C L V I D Q M E F G H`，外加 `B` 行为源、`.model`、`.subckt/.ends`、`.end`，独立源支持 `PULSE(...)` / `SIN(...)` / `DC` / `AC`。

### 2.1 常用要素

| 要素 | 写法 | 说明 |
| --- | --- | --- |
| 器件模型 | `.model DMOD D(IS=1e-14 N=1)` | 二极管/三极管/MOS 都靠 `.model`，不要指望库 |
| 激励 | `VPULSE in 0 PULSE(0 5 0 1n 1n 1m 2m)` | 上升/下降沿别写 0，写 `1n` |
| 正弦 | `VSIN in 0 SIN(0 1 1k)` | AC 扫描用 `VSIN in 0 DC 0 AC 1 SIN(0 1 1k)` |
| 行为源 | `B1 y 0 V = ...` | 表达式源，数字逻辑的唯一出路 |
| 子电路 | `X1 a b y NAND2` + `.subckt NAND2 … .ends` | 见 §2.3 |

### 2.2 数字门：用 B 源写，不要写库器件

**`74LS00`、`OPAMP_5T`、`D_CHIP` 这类 Multisim 库器件在 SPICE 域里不展开。** 写成 `XU1 a b y 74LS00` 时引擎不会报错，但 `y` 节点根本不会出现在结果里——看起来像"仿真成功了但没有输出"。

替代写法是 B 源行为模型。二输入 NAND：

```spice
* nand from behavioural source
VA a 0 DC 0
VB b 0 DC 0
B1 y 0 V = 5 - 5*( (V(a)>2.5) & (V(b)>2.5) )
```

`&`、`|`、`!`、`abs()`、`min()`、`max()` 都用实测过，AND / NAND / OR / XOR 一次跑通。NOT 就是 `B1 y 0 V = 5*(V(a)<2.3)`。

阈值取 2.5 V 是 TTL 判读的习惯值；判读结果时同口径：**≥2.4 V 记 1，≤0.4 V 记 0**，中间区（0.4–2.4 V）说明电路有问题，别硬当成某一态。

数字电路的整体做法：`dc` 扫描各输入（或 `op` 逐态跑），从工作点表里读出各节点电压，按上表判 0/1，拼真值表。

### 2.3 内联 `.subckt`

子电路可用，实测能实例化、能仿真：

```spice
* two nands make an SR latch
VCC vcc 0 DC 5
VGND gnd 0 DC 0
VS s 0 PULSE(0 5 0 1n 1n 0.5m 1m)
VR r 0 5
X1 s r q nq NAND2
X2 q nq nq2 NAND2

.subckt NAND2 a b y
B1 y 0 V = 5 - 5*( (V(a)>2.5) & (V(b)>2.5) )
.ends
.end
```

两条引用规则，容易踩：

| 想看的量 | 写法 | 说明 |
| --- | --- | --- |
| 子电路**内部节点** | `V(<节点名>)` | 内部节点不叫 `q`，而叫子电路里定义的那个名字，按节点名取 |
| 子电路**内部源电流** | `I(<源名>:<xinst名>)` | 形如 `I(B1:X1)`，冒号后面是实例化时的 X 名字 |

`X` 行的端口顺序就是 `.subckt` 的参数顺序，写反了不报错，只是结果不对。

### 2.4 网表的硬性要求

| 要求 | 违反的后果 |
| --- | --- |
| 必须有**接地元件**（`0` 节点） | 见 §3.2 |
| 正文必须 **ASCII** | 见 §3.1 |
| 首行是标题或注释 | 工具会自动补 `* msim_spice deck`，但自己写清楚更省事 |
| 以 `.end` 结束 | 工具会自动补，重复的不会叠加 |

---

## 3. 致命陷阱

### 3.1 UTF-8 BOM 或首行中文注释 → `No nodes present in circuit`

**症状原文**：引擎返回 `No nodes present in circuit`，无论网表内容多正确。

原因是引擎按 ASCII 读 deck：BOM 让第一张卡（通常是标题行）不可解析，整条第一条支路消失，于是"没有节点"。

**规避**：

- 网表正文保持 ASCII。中文解释写在工具结果里，不要写进网表。
- 工具会自动 `lstrip("\ufeff")` 剥掉 BOM，并把**注释行**（`*` 开头）里的非 ASCII 转写成 ASCII；首行注释会被替换成 `* msim_spice deck (comments were transliterated to ASCII)`。
- **正文（非注释卡）含非 ASCII 直接拒绝**，报：

  ```
  BAD_ARGS: netlist card 12 contains non-ASCII text: 'R1 顺流 0 1k'
  detail: Multisim's command engine reads the deck as ASCII. Keep card text ASCII;
          put any Chinese explanation in the tool result, not in the netlist.
  ```

### 3.2 没有接地元件 → 先 `No ground node was found`，再 `No nodes present in circuit`

**症状原文**：第一次报 `No ground node was found`；再跑就变成 `No nodes present in circuit`。两条消息容易让人以为是两个不同的问题，其实是同一个：缺 `0` 节点时整张网表被判为无效。

**规避**：每个连通片都要有到 `0` 的直流通路。数字电路常见的写法是显式补一个假地：

```spice
VCC vcc 0 DC 5
VGND gnd 0 DC 0
```

悬空的子电路（例如只接了输入没接输出的逻辑级）也会触发同一条消息。

### 3.3 命令文件路径不能含空格

**症状原文**：`BAD_ARGS: output_dir must not contain spaces: C:\Users\me\My Documents\run1`
详细里写：

```
Multisim's command engine rejects command-file paths that contain spaces.
```

这是 Multisim 命令引擎的限制，不是工具的偏好。`netlist_path` 同理。

**规避**：

- 默认目录已经是 `%TEMP%\msim_deck_<毫秒>`，一般不会踩到；`%TEMP%` 里带空格时自动退到 `C:\msim_deck\msim_deck_<毫秒>`。
- 要自己指定输出，用 `C:\msim_deck\run1` 这类无空格路径。
- 网表里有空格？直接把内容作为 `netlist` 文本传，别落地成文件。

### 3.4 需要一个已打开的文档

`DoCommandLine` 挂在文档对象上。当前没有任何打开的设计时，工具会自动 `NewFile()` 建一个空白设计，返回里 `created_circuit: true`。

**已打开的设计不会被替换**——这条是硬保证：引擎只是借这个文档跑自己的命令文件，不会改写设计内容。但反过来说，**你看到的 `msim_inspect` 结果和这次网表没有任何关系**，别把两者混为一谈。

### 3.5 命令白名单

只有分析/绘图命令能传。`source` 和 `write` 由工具自己生成，`quit` / `shell` / `cd` / `exec` / `system` 会直接中断引擎——这些都会被拒：

```
BAD_ARGS: command 'quit' is not allowed here
```

允许的前缀：`op dc ac tran tf noise pz sens disto four meas measure let print plot set linearize fourier sp`。

---

## 4. 分析命令清单

一条命令 -> 一个 `plotN.raw` -> 一份表格 + CSV + PNG。一个调用里可以按顺序放多条。

| 命令 | 语法 | 典型用途 | 关键列 |
| --- | --- | --- | --- |
| `op` | `op` | 直流工作点：各处静态电压/电流，数字电路逐态读真值表 | 每变量一个值 |
| `dc` | `dc <源名> <起> <止> <步>` | 直流扫描：转移特性、输入输出曲线；也可扫 `temp` / `res` | 扫描量 |
| `ac` | `ac <dec\|oct\|lin> <点数> <f起> <f止>` | 频响、Bode 图、滤波器通带 | `frequency` + 各量 dB/相位 |
| `tran` | `tran <步长> <终止时间> [起始时间]` | 时域波形：阶跃响应、振荡、逻辑时序 | `time` |
| `tf` | `tf <输出名> <输入源>` | 传递函数：直流增益 + 输入/输出阻抗 | 标量 |
| `noise` | `noise V(<节点>) <输入源> <dec\|oct\|lin> <点数> <f起> <f止>` | 噪声谱、等效输入噪声 | `frequency` + 谱密度 |
| `sens` | `sens V(<节点>)` | 各元件对某量的灵敏度排序 | 每元件一个数 |
| `disto` | `disto <dec\|oct\|lin> <点数> <f起> <f止>` | 谐波失真 | 频率 + 各次谐波 |
| `pz` | `pz <输出节点> <输入源>` | 极点 / 零点：稳定性判据 | 复极点对 |

补充可用的：`four`（傅里叶分析）、`meas`（测量语句，从波形里抽最大值/上升时间等）、`let` / `print` / `plot` / `set` / `linearize`。

示例（一次调用连跑三个分析）：

```json
{
  "netlist": "* rc lowpass\nV1 in 0 DC 0 AC 1\nR1 in out 1k\nC1 out 0 100n\n.end",
  "commands": ["dc V1 0 5 0.1", "ac dec 20 1 1meg", "tran 1u 5m"]
}
```

---

## 5. 输出

### 5.1 结构

```jsonc
{
  "directory": "C:\\Users\\...\\Temp\\msim_deck_1735000000000",
  "netlist": "...\\circuit.cir",
  "command_file": "...\\run.cmd",
  "log_file": "...\\run.log",
  "state": 0,
  "elapsed_seconds": 0.62,
  "created_circuit": true,
  "commands": ["dc V1 0 5 0.1", "ac dec 20 1 1meg"],
  "analyses": [
    {
      "command": "ac dec 20 1 1meg",
      "plotname": "AC Analysis",
      "columns": ["frequency", "V(out) (dB)", "V(out) (phase deg)"],
      "n_points": 121,
      "sampled_step": 2,
      "rows": [[1.0, -0.01, -0.03], "..."],
      "summary": [{"name": "V(out) (dB)", "count": 121,
                   "first": -0.01, "last": -60.2, "min": -60.2, "max": -0.01, "mean": -18.4}],
      "csv": {"path": "...\\plot0.csv", "rows": 121, "columns": 3},
      "png": {"path": "...\\plot0.png", "width": 900, "height": 520,
              "series": ["V(out) (dB)"], "log_x": true, "points": 121}
    }
  ],
  "missing": []
}
```

| 字段 | 含义 |
| --- | --- |
| `elapsed_seconds` | 引擎实际耗时 |
| `created_circuit` | `true` 表示为了跑这次仿真临时建了空白设计 |
| `sampled_step` | 抽样步长；`rows` 只回 ≤`max_points` 行，CSV 回全量 |
| `summary` | 每列的 count / first / last / min / max / mean（只统计有限值） |
| `missing` | 没能产出 raw 或解析失败的命令，形如 `"ac dec 20 1 1meg (raw file is empty)"` |
| `state` | 非 0 表示引擎没干净停下，报 `COM_ERROR` |

### 5.2 画图规则

| 情况 | 行为 |
| --- | --- |
| 单点分析（`op`，只有 1 行） | **自动改画柱状图**：每个变量一根柱、柱顶标数值，最多前 14 个变量 |
| AC / 复数图 | 解析阶段就把复数归约成 `(dB)` + `(phase deg)` 两列 |
| AC 图默认画哪几列 | **只画 `(dB)` 列**。dB 和角度混在同一根 y 轴上读不出来 |
| 加了 `series` | 只画列名匹配的列，其余不画 |
| x 轴 | 第一列；x 列名是 `frequency`/`freq` 且全为正时自动对数轴，轴标题附 `(LOG SCALE)` |
| 图例 | 最多 8 条曲线，超出不画 |

CSV 首列名会带 `grid=<点数>` 后缀，例如 `frequency grid=121`，这是 SPICE3 raw 的原始标法。

`series` 的名字必须与 `columns` 完全一致（含 `(dB)`、`(phase deg)` 后缀）。名字对不上时静默回落到默认列，不会报错——**画出来的图不是你要的列，就先回头看 `columns`**。

---

## 6. 实测性能

一次调用里连跑多个分析（含写 raw、解析、出 CSV、出 PNG），通常 **0.25–1.1 秒**。

| 场景 | 量级 |
| --- | --- |
| 单个 `op`（十几个节点） | ~0.25 s |
| 单个 `tran` / `ac`（100–1000 点） | 0.3–0.6 s |
| 三到四个分析一个调用 | ~1.1 s |

开销主要不在求解，而在 Multisim 的启动与命令文件往返。**一次调用放多个命令，比分多次调用快得多。**

---

## 7. 自检与报错对照

写完网表不确定能不能跑，先用 `action: "verify"`：它只解析、列元件和分析命令、返回预计输出列，**不启动仿真**。

| 错误码 | 触发条件 |
| --- | --- |
| `BAD_ARGS` | 网表正文非 ASCII / 路径含空格 / 命令不在白名单 / 同时给了 `netlist` 和 `netlist_path` / 网表为空 |
| `PATH_MISSING` | `netlist_path` 指向的文件不存在 |
| `TIMEOUT` | 超过 `timeout`（默认 180 s），detail 里带日志尾部 |
| `COM_ERROR` | 引擎 `state` 非 0，或所有命令都没产出可解析的 raw |
| `SPICE_ERROR` | raw 解析失败（二进制 raw、变量数对不上、无数据点） |

`msim_spice` 自身不碰可见窗口，因此不需要 `msim_gui` 配合；它借用的是自动化通道那个隐藏实例，**和用户屏幕上的 Multisim 无关**。
