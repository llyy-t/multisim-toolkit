# 实机截图样例（Multisim 14.3 可见窗口，1936×1048 最大化）

## 一、截图裁剪四档（设计3）

| 文件 | crop | 尺寸 | 内容 |
| --- | --- | --- | --- |
| `shot_paper.png` | `paper` | 937×663 | 整张图纸：边框、A–G 行号、0–8 列号，元件在原位，纸张右侧工作区灰已切除 |
| `shot_content.png` | `content` | 799×345 | 只框住电路本体：U1A/U2A（7420N）、V1–V4（1kHz 5V）、XLA1，四周 18px 留白 |

```text
msim_gui { action: "screenshot", mode: "canvas", crop: "paper",   path: "<...>/shot_paper.png" }
msim_gui { action: "screenshot", mode: "canvas", crop: "content", path: "<...>/shot_content.png" }
```

判据：`paper` 用工作区灰 `(171,171,171)` 定位纸张右缘，再用 `[95,170]` 中性灰连续段定位纸张软阴影；`content` 用「暗 `<100` 或彩度 `>16`」判墨迹包围盒。两者都在视口裁剪之后计算，不叠加。

## 二、实机电路截图 + 实机波形图（Folded Cascode Amplifier 样例）

这一对是「用 Multisim 做电路实验」的最终产物：左图是 Multisim 自己画的原理图，右图是 Multisim 自己画的波形，都不是插件重绘的。

| 文件 | 来自哪个窗口 | 尺寸 | 内容 |
| --- | --- | --- | --- |
| `tran_circuit.png` | 主窗绘图视图（`screenshot crop=content`） | 825×651 | 完整折叠共源共栅放大器原理图：8 个 MOSFET、探针标记 `Probe_M1source`…`Probe_M8gate`、`Vsig 1Vpk 1kHz`、`R1 100Ω`、`C1 1fF`、`C2 500fF`、`OUT` 节点 |
| `tran_graph.png` | **`图示仪视图`**（`capture_graph`） | 706×478 | 瞬态分析波形：5 条电流曲线，金色主曲线在 0.5 ms 处 3.0→3.4 V 跳变，横轴 `0→1.0m 次` |
| `ac_graph.png` | **`图示仪视图`**（`capture_graph`） | 706×478 | 交流分析的伯德图：上半幅频 `-50…+50 dB`，下半相频 `0…-360°`，横轴对数 `1 Hz → 10 GHz` |

```text
msim_gui { action: "launch" }
msim_gui { action: "dismiss_database" }
msim_gui { action: "open_file", path: "<样例>.ms14" }
msim_gui { action: "run_analysis", analysis: "transient" }     # 约 11 秒
msim_gui { action: "capture_graph", path: "<...>/tran_graph.png" }
msim_gui { action: "screenshot", crop: "content", path: "<...>/tran_circuit.png" }
```

注意两点：

1. **电路图和波形图在两个不同的窗口里**，必须分别截。波形只在分析结束后弹出的 `图示仪视图`（`class='#32770'`）里画出来。
2. 跑仿真前设计**必须真连线、有探针**；源悬空的设计会让 Multisim 内部堆损坏直接退出（`0xC0000374`），Windows 事件日志不留记录，看起来像「F5 必崩」，其实换一个连好线的设计就正常。

## 三、分析对话框操作链（`run_analysis` 内部）

| 文件 | 内容 |
| --- | --- |
| `menu_popup.png` | `alt+s` 打开的「仿真(S)」BCG 自绘菜单，第 4 项是 `Analyses and simulation(A)` |
| `analyses_dialog.png` | 「Analyses and Simulation」对话框，左侧 20 项分析列表 |
| `dialog_tran.png` | 选中「瞬态分析」后的参数面板（TSTART / TSTOP / 最大时间步长…） |

路径：`alt+s` → 3×`VK_DOWN` → `VK_RETURN` → 在左侧 `ListBox` 里点中分析项 → 对 `Run` 按钮发 `BM_CLICK`。菜单是 BCG 自绘的（`GetMenu` 返回 0），**投递 `WM_COMMAND` 会把进程打崩**，只能走真实键盘。
