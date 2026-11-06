---
name: multisim-toolkit
description: 通过 DSH 插件驱动本机 NI Multisim 14.3：开关 `.ms14` 设计、读写元件值与电路参数、跑 DC/AC/瞬态/SPICE 分析、导出原理图与 BOM/网表、截取实机电路图与实机波形图、离线解析 `.ms14`。当任务涉及 Multisim、电路仿真、原理图截图、实机波形截图、真值表/伯德图/波形数据，或要在本机跑 SPICE 而手边没有 LTspice/ngspice 时使用。
---

# Multisim 14.3 操控（msim_* 工具）

> **非官方工具。** 本 skill 与 National Instruments / Emerson 无关，未获其授权、背书或赞助。它不包含、不分发任何 NI 软件，不绕过授权校验，只驱动你自己合法安装并授权的 Multisim 14.3。许可与免责的完整说明见仓库根目录的 [`README.md`](README.md) 与 [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md)。

插件 `dsh-multisim-toolkit` 装好后，DSH 里会多出 9 个 `msim_*` 工具。它们把「本机那只 Multisim」变成可编程对象，但**不是所有能力都走同一条路**——选错通道是这个插件最常见的失败原因。

## 先确认东西在不在

`msim_status({connect:true})` 一次问清三件事：

| 看什么 | 期望 | 不对怎么办 |
| --- | --- | --- |
| `worker.pythonBits` | `32` | 64 位解释器连不上 COM，去看 `references/com-automation.md` §8 边界表 |
| `com.version` | `Multisim 14.3` | 报 `NO_MULTISIM` = 本机没装或注册表缺 32 位视图 |
| `gui.running_pids` | 有值说明用户已经开着一只可见窗口 | 只做纯数据任务时可以不管它 |

没装插件时先跑 `scripts/install.ps1 -Action check`。

## 四条通道，先选对再动手

| 要做什么 | 用哪个工具 | 为什么 |
| --- | --- | --- |
| 打开/保存已有 `.ms14`、读元件、改值、跑分析、导出 | `msim_circuit` / `msim_inspect` / `msim_edit` / `msim_analyze` / `msim_export` | COM 通道最快，一次 `Connect()` 后每次操作毫秒级 |
| 手里只有**网表文本**，想立刻要波形/数值 | `msim_spice` | 不依赖探针、不依赖原理图、不依赖窗口 |
| 需要**可编辑的原理图**（新建设计、布线） | `msim_gui` 拖 `.cir` 进可见窗口 | COM 没有任何"放元件/画线"的 API |
| 要**实机电路截图 + 实机波形图**（Multisim 自己画的图） | `msim_gui` 的 `screenshot` / `run_analysis` + `capture_graph` | 只有可见窗口里的 Multisim 才画那些图 |
| 想在不启动 Multisim 的情况下看设计里有什么 | `msim_design` | 纯离线解 `.ms14` 容器 |

**前两条通道不共享实例**：COM 那边是隐藏的另一只 `multisim.exe`，GUI 那边是屏幕上的那只。COM 看不到 GUI 打开的电路，反之亦然。

## 五条会浪费很多时间的判断

1. **分析报「输出未找到。」不是工具坏了。** Multisim 只能测「探针或仪器产生的命名输出」。先 `msim_inspect({what:'outputs'})`；返回空就直接换 `msim_spice`，别重试。
2. **`.cir` 网表不要交给 `msim_circuit open`。** COM 的 `OpenFile` 对网表会让 Multisim 静默崩溃。工具层已经把非 `.ms14` 后缀路由到 GUI 拖放，但你自己写脚本时别绕过去。
3. **空电路上不要调 `save`。** 未命名电路 `save()` 会让 Multisim 堆损坏退出（`0xC0000374`），一律用 `save_as` 给路径。
4. **别在 GUI 通道覆盖已有文件。** `msim_gui save_as` 只对**未命名**文档有效；同名文件存在时有覆盖风险。写盘优先用 COM 的 `msim_circuit save_as`。
5. **导出图片是固定视口。** 元件在可视区外就不会出现在图里。要完整图就用 `msim_gui screenshot {crop:'paper'}`（整张图纸）或 `{crop:'content'}`（只框住电路）。

## 推荐工作流

**验证一个组合逻辑设计**

```
msim_spice({netlist: "...", commands: ["op","tran 1u 2m"]})   # 数字门用 B 源行为模型
```

**在已有设计上量一个节点**

```
msim_circuit({action:'open', path:'...'})
msim_inspect({what:'outputs'})            # 空 → 换通道
msim_analyze({analysis:'transient', outputs:['V(out)'], duration:0.002})
```

**做一份能放进报告的图**

```
msim_export({kind:'image', path:'...png'})              # 原理图
msim_analyze({analysis:'ac_sweep', outputs:[...]})       # 数据
msim_spice({...})                                        # 数据图（自行绘制，须注明"由仿真数据绘制"）
```

**把第三方网表变成可编辑原理图**

```
msim_gui({action:'launch'})
msim_gui({action:'dismiss_database'})     # 先清数据库告警框，否则它吞掉所有按键
msim_gui({action:'open_file', path:'x.cir'})
msim_export({kind:'snippet', path:'...'}) # 或让用户自己在窗口里 Ctrl+S
```

**要实机电路截图 + 实机波形图（做实验报告用这个）**

```
msim_gui({action:'launch'})
msim_gui({action:'dismiss_database'})
msim_gui({action:'open_file', path:'...设计.ms14'})        # 打开要跑的设计
msim_gui({action:'run_analysis', analysis:'transient'})    # 在 Multisim 里真跑一遍
msim_gui({action:'capture_graph', path:'波形.png'})        # 图示仪视图的曲线
msim_gui({action:'screenshot', crop:'content', path:'电路.png'})  # 电路本体
```

两张图**来自不同的窗口**：电路图在主窗的绘图视图里，波形在分析结束后弹出的 `图示仪视图` 里，必须分别截。`run_analysis` 全程约 11 秒。

## 参数速查

| 工具 | 关键参数 |
| --- | --- |
| `msim_status` | `connect` |
| `msim_circuit` | `action`: new / open / save / save_as / info；`path` |
| `msim_inspect` | `what`: components / inputs / outputs / variants / sections / parameters / rlc / info |
| `msim_edit` | `action`: set_rlc / set_parameter / set_variant / replace_component |
| `msim_analyze` | `analysis`: transient / dc / ac_sweep / ac_single / command_file / state / stop / pause / resume；`outputs` 必填 |
| `msim_export` | `kind`: image / netlist / bom / snippet；`image_format`、`format` |
| `msim_gui` | `action`: launch / windows / main_window / focus / screenshot / open_file / dismiss_database / close_dialogs / save_as / send_keys / **run_analysis** / **capture_graph** / kill；`crop`: none/canvas/paper/content；`analysis`: transient/dc/ac/… |
| `msim_spice` | `netlist` 或 `netlist_path`；`commands`；`plot`；`series` |
| `msim_design` | `action`: read；`path`；`include_xml` |

## 该读哪份参考

| 你卡在 | 读 |
| --- | --- |
| COM 报错、想知道某个 `MultisimSession` 方法怎么用 | `references/com-automation.md` |
| 网表报 `No nodes present in circuit`、想写数字门或子电路 | `references/spice-channel.md` |
| 窗口点不动、Save As 保存到错地方、截图裁错、要离线读 `.ms14` | `references/gui-and-files.md` |
| 插件装不上 / 自检不过 | `scripts/install.ps1 -Action check`、`scripts/selfcheck.py` |

## 边界（写结论时不要越过）

- 插件**不新建原理图里的元件和连线**。要新电路：写网表拖进 GUI，或直接编辑 `.ms14` 后再用。
- Multisim 库器件（`74LS00`、`OPAMP_5T`、`D_CHIP`…）在 SPICE 通道里**不展开**，必须换 B 源行为模型。
- `msim_export` 的 BOM/网表报告里 `_ucXXXX` 是 Multisim 内部编码，**不是可读标签**，别当成器件名引用。
- 从仿真数据画出来的图，图题要写「由仿真数据绘制」，不能写成「实机截图」。
- **`capture_graph` 出来的是 Multisim 自己画的波形窗口**，可以写成「实机波形截图」；`msim_spice` 生成的 PNG 是插件自己绘制的，只能写「由仿真数据绘制」。两者不要混。
- **仿真跑不动先看电路本身**：源悬空、仪器没接线的设计按 F5 或跑分析会让 Multisim 内部堆损坏直接退出（`0xC0000374`）。这不是工具问题，先确认电路有真正的连线与探针。
