# 可见窗口通道与 `.ms14` 文件格式

两条互不相干的东西放在一起，因为它们都不走 COM 自动化接口：

- **A 节**：用 Win32 消息操纵用户屏幕上那只 `multisim.exe`。
- **B 节**：不启动 Multisim，直接在字节层面读 `.ms14`。

---

# A. 可见窗口通道（Win32，`msim_gui`）

## A.0 一句话结论

| 目的 | 走哪条 |
| --- | --- |
| 打开 SPICE 网表（`.cir` / `.net`） | **只能走 GUI**，`WM_DROPFILES` 注入 |
| 把设计按指定路径存盘 | **走 COM 的 `msim_circuit save_as`**；GUI 的 `save_as` 只对未命名文档有效 |
| 看原理图整体长什么样 | GUI 截图 `crop: "paper"` |
| 看电路本身长什么样（四周去干净） | GUI 截图 `crop: "content"` |
| 读设计里的元件值 | 自动化通道（`msim_inspect`）或离线 `msim_design` |

---

## A.1 两条通道不共享实例

| | 自动化通道 | 可见窗口通道 |
| --- | --- | --- |
| 进程 | `app.Connect()` **新起**一只隐藏的 `multisim.exe` | 用户屏幕上那只 `multisim.exe` |
| 界面 | 无 | 有 |
| 能被另一条看到 | 否 | 否 |

`Connect()` 只能新起实例，**附着不到用户手动打开的窗口**（已实测）。因此：

- 在 GUI 里打开的电路，`msim_inspect` / `msim_analyze` / `msim_export` 全都看不到。
- 反向同理：COM 通道里打开的设计，GUI 截图里不会出现。
- 两条通道的电路**不能跨通道传递**，要传递就落盘成文件再在另一侧打开。

---

## A.2 打开网表必须走 GUI

**结论**：`msim_circuit open` 收到 `.cir` / `.net` / `.sp` 会**改道到可见窗口**，因为在自动化通道里 `app.OpenFile()` 打开 SPICE 网表会让 Multisim **静默崩溃**（进程消失，没有报错）。

**机制**：把文件路径写进目标进程的地址空间，再 `SendMessageTimeout(WM_DROPFILES)`，模拟一次 Explorer 拖放。这是唯一可靠的方式。

```c
// 载荷：DROPFILES 头 + UTF-16LE 路径 + 双零结尾
payload = pack("<LiiLL", 20, 0, 0, 0, 1)        // pFiles, pt.x, pt.y, fNC, fWide
        + absolute_path.encode("utf-16-le") + b"\x00\x00"
VirtualAllocEx → WriteProcessMemory → SendMessageTimeoutW(hwnd, WM_DROPFILES, ...)
```

注入前会先 `WM_CLOSE` 掉已有的 MDI 文档，并在等待期间持续点掉阻挡转换的模态框。返回里 `confirmed` 表示是否观察到新文档出现。

**拖进去之后发生什么**：Multisim 会把网表**还原成可编辑原理图**——R / C / V 等都有真实图形符号，器件是库里真实器件，连线是真的连线，不是一堆文本框。这可以用来把 AI 生成的网表变成人看得懂的原理图。

但记住 A.1：这张原理图在 GUI 进程里，自动化通道读不到它。

---

## A.3 Save As 的三个真实根因

这一节是事故的根源，三个坑会叠加：**做错任何一条，Multisim 都可能静默覆盖真实文件。**

### 根因 1：`WM_SETTEXT` 不更新对话框内部状态

`WM_SETTEXT` 只是把字符串塞进文件名编辑框，**对话框自己的文件状态还记着旧名字**。于是点了「保存」之后，Multisim 按旧名字写盘；未命名文档的旧名字解析成 `设计1`，最终落到：

```
C:\Users\<user>\Documents\National Instruments\Circuit Design Suite 14.3\设计1.ms14
```

**同名真实文件被静默覆盖，没有任何提示。** 本插件开发期就撞过一次。

**规避**（三件事缺一不可）：

1. 用 `SendInput` + `KEYEVENTF_UNICODE` **逐字符**输入路径，每个字符一 down 一 up：

   ```python
   for char in text:
       for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
           record.type = INPUT_KEYBOARD
           record.u.ki.wVk = 0
           record.u.ki.wScan = ord(char)
           record.u.ki.dwFlags = flags
           SendInput(1, byref(record), sizeof(record))
           sleep(0.004)
   ```

2. 输入前 `ctrl+a` 清空编辑框。
3. **点保存之前用 `WM_GETTEXT` 回读校验**，不一致就放弃保存并报错：

   ```
   GUI_ERROR: Save As filename did not take the requested path; refusing to save
   detail: typed='C:\out\x.ms14' wanted='C:\out\y.ms14'
   ```

顺带两条实现细节：文件名编辑框**不能用 `GetDlgItem(dialog, 1001)`** 找——它嵌在 `DUIViewWndClassName` 子窗口里，返回 0。要遍历整棵树，取「靠下、宽度 > 200、高度 > 8」的那个 `Edit`（上面还一个搜索框，位置靠上）。

### 根因 2：数据库告警框的文字藏在跨进程 `Edit` 里

启动时常弹两个 `#32770`：

```
主数据库 无法访问
企业数据库 无法访问
```

这两条是关键：**它们是模态的，会吞掉之后发出的所有按键**——`Ctrl+S` 根本到不了文档。

而且它们的正文放在跨进程 `Edit` 控件里，`GetWindowText` **读出空串**，于是对话框看起来是空白的、认不出来。必须用 `WM_GETTEXT` 才拿得到：

```python
for child in children:
    text = GetWindowText(child)          # 空
    if not text and class_name(child) == "edit":
        text = SendMessage(child, WM_GETTEXT, size, buffer)   # 才有内容
```

**规避**：识别条件 = 文本同时含「数据库/database」和「无法访问/cannot be accessed/不可用」；然后用 `BM_CLICK` 点它的「确认/确定/OK」按钮关掉。`WM_CLOSE` 对这两个框**无效**，`close_dialogs` 里的 `WM_CLOSE` 收不掉它们，`dismiss_database` 才是正解。

### 根因 3：`WM_MDIACTIVATE` 不给键盘

`WM_MDIACTIVATE` 只把子窗标记成「活动的」，**在 MFC 这套构建上并不把键盘焦点交给它**，之后发的按键全被主框架吞掉。

**规避**：点击文档区域拿焦点，而且**点击之前必须先清掉模态告警框**——否则点击落在告警框上，仍然没到文档。顺序固定为：

```
置前主窗 → dismiss_database_dialogs() → 点击 MDI 文档中心 → 发 Ctrl+S
```

`SetForegroundWindow` 单独调用经常被前台锁拒掉，事先补一次合成 ALT 轻敲再置前才稳。

---

## A.4 已命名 vs 未命名文档

| 文档状态 | `Ctrl+S` 行为 |
| --- | --- |
| **未命名**（标题 `设计1` / `Design1`） | 弹「另存为」对话框 |
| **已命名**（有文件路径） | **静默原地保存**，不弹框 |

所以 GUI 的 `save_as` 只对未命名文档有效。对已命名文档它会**明确报错**，而不是假装成功：

```
GUI_ERROR: Multisim wrote the design in place under its own name instead of
           opening Save As for C:\out\x.ms14
detail: the document already has a file name, so Ctrl+S saves it there;
        open an untitled document, or write copies with msim_circuit save_as (COM)
        which really does honour the path
```

**要按指定路径落盘，一律用 COM 的 `msim_circuit save_as`。** 它接受真实路径，不用按键模拟，也不受上面三条根因影响。

另外，文件菜单是 BCG 自绘弹窗（类名 `Afx:...:800:...`），不是 `HMENU`——`GetMenu()` 返回 `None`，`MN_GETHMENU` 返回 0，**没有可用的命令 id**。唯一稳定的触发方式是 `ALT+F` 后按 6 次 `VK_DOWN` 再 `VK_RETURN`（「另存为」是文件菜单第 7 项）。

---

## A.5 截图

### 四种 crop

| crop | 范围 | 实测尺寸（1936×1048 最大化框，设计3） |
| --- | --- | --- |
| `none` | 原样，不裁 | 1936×1048 |
| `canvas` | 原理图绘制视口（MDI 子窗里的 `AfxFrameOrView140u`），找不到时回退到固定比例 `CANVAS_FRAC = (0.120, 0.124, 0.836, 0.763)` | 1688×688 |
| `content` | 在 `canvas` 基础上再裁到「墨迹」包围盒，四周留 18 px | 799×345 |
| `paper` | 在 `canvas` 基础上裁掉标尺 chrome 和纸张右侧工作区灰，保留整张图纸（边框、行号列号、元件原位） | 937×663 |

`canvas` / `content` / `paper` 是三选一，不是叠加——三者都描述同一个绘制视口，不会裁两次。

怎么选：**要「图纸长什么样」用 `paper`，要「电路长什么样」用 `content`。** 一块只画出半张纸的电路，`content` 会把它裁到 799×345（正好框住元件），`paper` 会给出带 A–G / 0–8 标尺的整张图纸。

`mode` 另有 `window`（整框）/ `client`（客户区）/ `canvas`。任何需要视口的 crop 都会先抓整框再裁，因为 crop 坐标是框坐标系。

### `content` 的墨迹判据：暗 **或** 有彩度

```python
low  = min(r, g, b)
high = max(r, g, b)
ink  = (low < 100) or (high - low > 16)     # INK_DARK_MAX=100, INK_CHROMA_MIN=16
```

**只按「暗」判会漏掉全部蓝色接线。** 纯蓝导线 `(0, 0, 255)`：它的蓝通道是 255，任何「三通道都亮就算背景」的判法都会把它当背景——整个原理图就出来一张空白图。

按这个判据，以下都算**背景**（中性、够亮）：

| 元素 | 灰度 |
| --- | --- |
| 白纸 | 255 |
| 网格点 | ~150 |
| 纸张外的工作区 | 171（`WORKSPACE_GREY`） |
| 视口边框 | ~128 |

以下算**内容**：黑色元件轮廓与文字（暗）、蓝色导线与标注、带色的仪器本体（有彩度）。

坐标标尺带（上/左约 23 px）和视口凹陷边框（四边 2–3 px）是结构性 chrome，算内容前先排除掉，否则任何空白画布都会被判成"有内容"。

### 纸张右缘与软阴影（`paper` / `content` 共用的两个定位）

绘图视口右边通常有一大片 `(171,171,171)` 的工作区灰——图纸只有 975 px 宽而视口 1688 px 宽时，那 713 px 全是灰。两个函数负责找到真正的边界：

- `_paper_right_edge(rows, width, height)`：从右往左找第一列「≥50% 采样像素是 171±6」的列，返回纸张填充的起点。
- `_shadow_left_edge(rows, width, height, paper_right)`：纸张左缘外侧有 15 px 宽的下落阴影 `128,100,104,110,115,120,125,131,135,140,145,150,155,161,165`。这些中性灰落在 `[95,170]` 里，**既不满足"暗"也不满足"有彩度"**……唯一的例外是最左侧那一两列（100 < 105 处的阈值）会被判成墨，把包围盒一路撑到纸边。所以从 `paper_right-1` 向左最多 48 列找「三通道都在 [95,170]」的连续段，取其最左列。

两个函数都必须在**视口裁剪后**的 rows 上调用——传整窗 rows（1936 宽）会因为左边工具栏也是灰而定位错。

空画布（新建设计、没放元件）找不到包围盒时，**返回 `canvas` 尺寸的图并标 `crop_fallback: true`**，不报错；`paper` 找不到纸张边缘时同理。

---

## A.6 `kill` 的作用范围

`kill` 默认**只关本插件 `launch()` 起过的进程**。

```python
targets = [pid for pid in running if pid in _LAUNCHED_PIDS]
```

理由很直接：按映像名 `multisim.exe` 全杀，会连带杀掉**用户手工打开、带未保存工作**的实例，工作直接丢。

| 参数 | 效果 |
| --- | --- |
| 默认 | 只关插件自己起的；返回 `skipped` 是放过的进程数 |
| `pid: 1234` | 额外关掉这一个 |
| `all_instances: true` | 关掉所有 `multisim.exe`，**未保存工作会丢**，必须显式要求 |
| `force: false` | 先发 `WM_CLOSE` 并回答保存提示；`force: true` 直接 `TerminateProcess` |

---

## A.7 进程状态不可假设

**可见实例会随机自行退出**——不是被本插件 kill 的，也不是崩溃报告。

因此：

- 每次动作前**重新定位主窗**（`Multisim14.3` 类的可见顶层窗口），不缓存 hwnd。
- 找不到就报 `NO_MULTISIM: Multisim main window not found`，此时 `msim_gui launch` 重新拉起。
- 隐藏辅助窗口（全零矩形的 `lvframe` / `#32770` / `tooltips_class32` / `gdi+ hook window class`）不能当主框认。

`launch` 直接 `Popen(multisim.exe)`：`MultisimLauncher.vbs` 包装器在中等完整性级别下行为不可靠，直接起 exe 才能拿到可见主框。

---

# B. `.ms14` 文件格式

`.ms14` **不是 XML 文件，是压缩容器**。`msim_design` 在字节层面解开它，不启动 Multisim、不产生任何文件，因此对「Multisim 打不开 / 不想动前台」的设计同样有效。

## B.1 容器布局

```
偏移 0    36 字节   魔数  MSMCompressedElectronicsWorkbenchXML
偏移 36    8 字节   u64 解压后总长（小端）
偏移 44    循环     { u32 declen, u32 complen, complen 字节 PKWare DCL implode 数据 }
```

**定点验证**：`设计3.ms14` 文件 67286 字节，头里 `declen = 618589`、`complen = 67234`，

```
44 + 8 + 67234 = 67286    ✓ 正好等于文件长度
```

块数：`设计3` 只有 1 个块，块头在偏移 44，数据到文件末尾。多块文件按同样规则循环。

## B.2 解压必须用 PKWare DCL "implode"

**zlib / bz2 / lzma / cabinet.dll 全都解不开。** 魔数后面不是 gzip/zlib 头，是 PKWare DCL 的私有权流。

解法是一段 C：

| 件 | 说明 |
| --- | --- |
| `bin/blast.c` | Mark Adler 的 `blast.c`，17180 字节，收录了 DCL implode 的完整解码 |
| `bin/blast_harness.c` | 薄壳，I/O 走 Win32 `CreateFileA` / `ReadFile` / `WriteFile` |
| `bin/blast_harness.exe` | 成品，毫秒级 |

纯 Python 的 `pwexplode.py` 也能解，但慢了**三个数量级**：1661573 字节的设计要 118 秒。只留作测试对照，实际路径不用它。

重建命令（`vcvars64` 环境里）：

```bat
cl /nologo /O2 /Fe:blast_harness.exe blast_harness.c blast.c
```

**临时目录坑**：由 32 位 Python 拉起的 `blast_harness.exe` 对 `%TEMP%` 调 `CreateFileW` 会拿到 **error 5**（同一进程自己写 `%TEMP%` 却正常）。所以临时目录按 `_work_root()` 顺序挑第一个能写的：

```
MSIM_MS14_WORKDIR  →  <bundle>/_ms14_tmp  →  %TEMP%
```

## B.3 元件值的三种形态

| 形态 | 例子 | 可读结果 | 说明 |
| --- | --- | --- | --- |
| `&ASC<文本>` | `&ASC1k` | `1k` | 直接就是文本 |
| `&UNI<文本>_uc<hex>` | `&UNI1k_uc103a9` | `1k` | 去掉尾部 `_uc<hex>` 剩下的才是值 |
| `&UNI_uc<hex>`（去掉后为空） | `&UNI_uc103a9` | **无法还原** | 只表示「按默认格式显示」 |

`_uc<hex>` 是 Multisim 的**单位/格式指针，不可逆**——像进程内指针的十六进制，同一设计里数值连续。**不要试图反解它。**

`<doubles>` 是历史遗留的初值数组，可能已过期。实测：某电阻的 `doubles` 仍是 `1000`，而 `parameters` 已经是 `2.2k`。

**所以读值要以 `CiaParamList.parameters` 为准：**

```python
for params in element.findall("parameters"):
    for item in params.findall("Item"):
        text = item.attrib.get("Value")      # &ASC 前缀剥掉后才是值
```

`CiaParamList` 下两条平行数组：

| 数组 | 内容 |
| --- | --- |
| `<doubles>` | 历史遗留初值，**可能过期** |
| `<parameters>` | 真正用于显示的字符串，**权威** |

从 `parameters` 里挑"真正的值"：绝大多数位置是默认值（`0`、`27` 之类），器件的值通常是**唯一非零的那个**；多个非零时取第一个（例如源同时有 `1` 和 `5k`，第一位是幅度）。

## B.4 XML 结构

```
<MSMElectronicsWorkbench Schema="67">
  <history>
    <creator application="multisim" date=... versionMajor="14"
             versionMinor="3" versionPatch="0"/>
  </history>
  <Project Class="CProject">
    <Components>
      <Item Class="CIITSymbolComp"> ...
```

`<creator>` 嵌在 `<history>` 里，**直接 `root.iter("creator")` 找不到**。虽然 `iter()` 名义上是递归的，但这条路径实测必须走 `history` 再取子节点：

```python
for element in root.iter():
    if element.tag == "history":
        for child in element:
            if child.tag == "creator":
                creator = {...}
                break
        break
```

器件抽取的锚点是 `CIITSymbolComp`，位号来自子节点 `CIITSymTextCompName` 的 `Output` 属性，值来自 `CIITSymTextCompValue`、模型来自 `CIITSymTextCompModel`。

## B.5 仪器不在 `<Components>` 里

仪器在 `<InstrumentsData>` 下的 `CSourceSymbolCollectNode`。位号从 `CompLongName` 里抠：

```
CompLongName = "&UNIXLA1#1/_uc<hex>:"
                        ^^^^  ← XLA1 就是逻辑分析仪的位号
```

正则取 `UNI(X[A-Z]{2}\d+)`。设置项在 `NI_EWB_*` 属性里（`Element` 的 `Key` 去掉 `NI_EWB_` 前缀），例如：

| 仪器 | 位号前缀 | 典型设置键 |
| --- | --- | --- |
| 示波器 | `XSC` | `LOGICALTHRESHOLDVALUEUSER` |
| 逻辑分析仪 | `XLA` | `LOGIC_PRETRIGGERSAMPLECOUNT` |
| 万用表 | `XMM` | — |
| 函数发生器 | `XFG` | — |
| 波特图仪 | `XBP` | — |

含大数组的键（`PATTERNS` / `COLORS` / `TRACEDATAS`）会被过滤掉，只留能读懂、长度 ≤ 64 的设置。

仪器在原理图里同时也是 `CIITSymbolComp`（位号 `X…`），两处会合并成一张表，**值/类别以 `<InstrumentsData>` 为准**。

## B.6 NI 自带样例没有 `<history>`

`C:\Users\Public\Documents\National Instruments\Circuit Design Suite 14.3\samples\` 下的样例**没有 `<history>` 段**，所以 `created by:` 那行为空。

**这是文件本身如此，不是解析失败。** 不要据此判定"解码有问题"。

## B.7 边界

| 能做 | 不能做 |
| --- | --- |
| 读出器件位号、值、模型、分区数 | 修改设计 |
| 读出仪器位号与设置 | 判断谁连到谁（连线只给数量，不给拓扑） |
| 读出连线条数、引脚数、保存版本 | 生成可仿真的网表 |
| 返回原始 XML（大文件截断） | 替代 Multisim 的渲染结果 |

读出的连线条数来自 `Class="CODLineComp"` 的计数，引脚来自 `Class="CIITPinSymbolComp"`。这两个数是**规模指标**，不是连接关系。
