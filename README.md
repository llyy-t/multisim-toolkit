# multisim-toolkit

一个把 **NI Multisim 14.3** 变成可编程对象的 DSH 插件 + skill。

它同时是两样东西，装在同一个仓库里：

1. **一个 DSH 插件**（Host 侧 Cordis bundle）—— 仓库根目录的 `package.json` / `cordis.patch.yml` / `index.js` / `lib/` / `worker/`，注册 9 个 `msim_*` 工具；
2. **一份 skill** —— `SKILL.md` + `references/`，教模型怎么正确使用这些工具，以及所有踩过的坑。

```
DSH Host (node)
  └── index.js                     注册 msim_* 工具 + 一段系统提示词
        └── spawn 32-bit python worker/msim_worker.py --port N --token T
              └── 常驻进程（独占 COM 公寓），TCP + 行 JSON
                    ├── com.*     msim_com.py     → Multisim Automation (COM)
                    ├── gui.*     msim_gui.py     → 可见窗口（Win32 消息）
                    ├── spice.*   msim_deck.py    → DoCommandLine + 网表
                    │              msim_spice.py / msim_raster.py
                    └── design.*  msim_ms14.py    → .ms14 容器（离线只读）
                                   └── bin/blast_harness.exe (PKWare DCL)
```

## 能力一览

| 工具 | 做什么 |
| --- | --- |
| `msim_status` | 自检：worker 位数、COM 版本、GUI 进程 |
| `msim_circuit` | 新建 / 打开 / 保存 `.ms14`，读当前电路信息 |
| `msim_inspect` | 读元件、输入、输出、变体、段落、电路参数、RLC 值 |
| `msim_edit` | 改 RLC 值、电路参数、活动变体，替换库器件 |
| `msim_analyze` | 瞬态 / DC / AC 扫频 / 单频 AC / 命令文件，以及启停仿真 |
| `msim_export` | 原理图图片、网表、BOM、设计片段 |
| `msim_gui` | 驱动可见窗口：启动、截图（四档裁剪）、拖入网表、清数据库告警、另存为、发按键、**跑分析**、**抓波形窗** |
| `msim_spice` | 把网表直接喂给 Multisim 的 SPICE 引擎，返回表格 + CSV + 图 |
| `msim_design` | 不启动 Multisim，离线读取 `.ms14` 里的元件、值、仪器设置 |

多数操作在首次 `Connect()`（约 6 秒，Multisim 启动开销）之后是**毫秒级**——实测 10 次连续读操作 median `0.000s`。

## 安装

前提：

- Windows，已安装 **Multisim 14.3**（`C:\Program Files (x86)\National Instruments\Circuit Design Suite 14.3\`）；
- 一个 **32 位** Python 3.11–3.13 并装了 pywin32（Multisim 的 Automation API 是 32 位 COM 服务器，64 位解释器 `Connect()` 会 `E_ACCESSDENIED`）；
- DSH（本插件是 Host-only bundle，不需要浏览器端代码）。

**方式一：直接从 GitHub 装**

```sh
dsh plugin --profile <你的 profile> add github:llyy-t/multisim-toolkit
```

**方式二：从本地 clone 装**

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Action check    # 体检
# 然后把 install.ps1 -Action install 打印出来的那行 plugin_manager(...) 贴进 DSH 会话
```

**验证**：`msim_status({ connect: true })` —— 期望 `worker.pythonBits: 32`、`com.version: Multisim 14.3`。

也可以不经过 DSH，直接跑离线自检：

```powershell
python scripts\selfcheck.py --design "C:\path\to\some.ms14"
```

插件配置（可选）：

```yaml
- id: multisim-toolkit
  name: 'dsh-multisim-toolkit'
  config:
    pythonPath: 'C:\Users\me\.dsh\multisim-mcp\py32\python.exe'
    startTimeoutMs: 30000
```

## 文档

| 文件 | 内容 |
| --- | --- |
| [`SKILL.md`](SKILL.md) | 面向模型：先选通道，再动手；最贵的几条判断；参数速查 |
| [`references/com-automation.md`](references/com-automation.md) | COM 面完整参考：每个方法、枚举值、8 类失败的症状与规避、类型库注册信息 |
| [`references/spice-channel.md`](references/spice-channel.md) | SPICE 通道：语法、用 B 源写数字门、子电路、4 个致命坑、9 种分析命令 |
| [`references/gui-and-files.md`](references/gui-and-files.md) | 可见窗口（含 Save As 三个真实根因、分析对话框操作链）与 `.ms14` 容器格式、元件值三种形态 |
| [`PLUGIN.md`](PLUGIN.md) | 插件本体说明与全部实测边界（安装/自测/协议入口） |
| [`worker/PROTOCOL.md`](worker/PROTOCOL.md) | 插件内部模块契约（要改代码时读） |
| [`docs/README.md`](docs/README.md) | 四档截图对比 + 实机电路图/波形图样例的生成命令 |

## 这个仓库为什么值得看

除了"又一层包装"，这里有几条别人不太容易自己撞出来的实测结论：

- **Multisim 的 COM 服务器与客户端同生命周期**——Python 进程一退出，`multisim.exe` 就消失。所以必须常驻 worker，不能每条命令起一个短命进程。
- **`app.OpenFile()` 对 SPICE 网表会让 Multisim 静默崩溃**（无任何可捕获错误）。网表只能通过 `WM_DROPFILES` 注入到可见窗口。
- **GUI 另存为不能靠 `WM_SETTEXT`**——它只改编辑框的显示文本，不改对话框内部状态，Multisim 仍按旧名字写盘，会静默覆盖同名文件。必须用 `SendInput` + `KEYEVENTF_UNICODE` 逐字符输入并在保存前回读校验。
- **数据库告警框的文字藏在跨进程 Edit 控件里**，`GetWindowText` 读到空串，必须用 `WM_GETTEXT`；这些模态框会吞掉之后所有按键。
- **`.ms14` 是 36 字节魔数 + `u64` 长度 + 若干段 PKWare DCL "implode" 数据**，zlib/bz2/lzma/cabinet.dll 全都解不开；用 Mark Adler 的 `blast.c` 编译出的薄壳解压，毫秒级。
- **`.ms14` 里元件值的"真正来源"是 `CiaParamList.parameters`**，另一条 `<doubles>` 数组是可能已过期的遗留初值（实测某电阻 `doubles` 还是 1000，`parameters` 已经是 `2.2k`）；`&UNI..._uc<hex>` 的 `_uc` 后缀是进程内指针号，不可逆。
- **截图 `crop=content` 的墨迹判据必须是"暗 或 有彩度"**——只按"暗"判断会漏掉全部蓝色导线（Multisim 的连线是纯蓝 `(0,0,255)`）。
- **Multisim 的仿真菜单是 BCG 自绘的**，`GetMenu()` 返回 0、投递 `WM_COMMAND` 会把进程打崩；但 `alt+s` 之后按 3 次 `VK_DOWN` 再回车能稳定打开「Analyses and Simulation」，进去之后里面的控件全是标准 Win32 控件，可以按消息操作。
- **仿真结果画在一个独立的 `图示仪视图` 窗口里**，跟电路图不是同一个窗口——要「实机电路截图 + 实机波形截图」就得分别截两次。

## 能力边界

诚实地说，这个插件**不能**凭空在原理图上放元件、画连线——Multisim 的 Automation API 没有对应方法，Win32 合成鼠标画线也不可靠。新电路的可行路径只有两条：

1. 写网表，拖进可见窗口，Multisim 会自动还原成带真实库器件和连线的**可编辑原理图**；
2. 直接生成/修改 `.ms14`（容器格式已破解，但写回收尚未实现）。

## 只要文件读写和仿真？用免费版

上面的能力里，有两块**不需要 DSH、不需要开 Multisim 窗口、不绑 Windows 宿主**：

- 离线读 `.ms14`（元件、值、仪器设置、创建版本）
- 把 SPICE 网表喂给本机 Multisim 跑仿真，拿回数据表 + CSV + 图

这两块我单独拆成了一个可移植的免费 skill：[**llyy-t/multisim-file-tools**](https://github.com/llyy-t/multisim-file-tools)。它是一份 `SKILL.md` 加一个纯命令行工具 `cli.py`，Claude Code / Cursor / 任何吃 Markdown skill 的 agent 都能用，`design` 子命令甚至不要求目标机器装有 Multisim。

本仓库和它的关系：两边共用 `worker/` 下的同一批模块（`msim_ms14.py` / `msim_spice.py` / `msim_deck.py` / `msim_raster.py` / `msim_com.py`），改任意一边都要保证另一边还能跑。免费版里没有 `msim_gui.py`——**所有窗口自动化、截图、波形抓取只在这个仓库里**。

需要驱动 Multisim 的界面、截图、菜单、波形图，用本仓库；只要能读文件和跑仿真，用免费版。

## 许可与免责声明

**本仓库的代码**（`index.js`、`lib/`、`worker/`、`scripts/`、文档）以 **MIT** 发布，见 [`LICENSE`](LICENSE)。你可以自由使用、修改、商用、再分发，保留版权声明即可。

**第三方组件**见 [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md)：`worker/bin/blast.c` / `blast.h` 是 Mark Adler 的 PKWare DCL 解压器，zlib license（保留源码，方便你自行重编译 `blast_harness.exe`）。

### 与 National Instruments 的关系

本项目是**独立、非官方**的第三方工具，与 National Instruments / Emerson 没有任何隶属、授权、背书或赞助关系。它：

- **不包含、不分发** NI 的任何软件、二进制、模型库或数据库文件；
- **不绕过** Multisim 的任何授权、激活或序列号校验；
- 只是驱动**你自己电脑上已经合法安装并授权好的** Multisim 14.3；
- 使用 Multisim 需要你有合法的 Multisim 授权，这一点与本项目无关。

`Multisim`、`National Instruments`、`NI Circuit Design Suite` 是其各自所有者的商标，此处仅用于**指明本工具所兼容的产品**（nominative use），不主张任何商标权。

### 文件格式兼容性

`worker/msim_ms14.py` 能读取 `.ms14` 容器，做法是**逆向工程该容器现有的压缩封装**，目的是让工具能在不启动 Multisim 的情况下查看设计内容（元件清单、参数值、仪器设置）。

需要说明的是：

- 该格式的压缩层**没有加密、没有访问控制、没有技术保护措施**；本项目实现的是解压缩，不是破解任何保护机制，也不涉及授权绕过；
- 使用的是公开发表的 PKWare DCL "implode" 算法（Mark Adler `blast.c`），不是 NI 的私有算法；
- 部分元件值在文件里以进程内指针号（`_uc<hex>`）形式存在，**无法还原**——这也侧面说明该格式从未被设计成可移植的公开接口；
- 这个读取能力属于**互操作性用途**。如果你所在司法辖区或你的 Multisim 许可协议对此有额外限制，请自行判断是否使用；不使用时其余 8 个工具完全不受影响。

### 本机数据目录

工具运行时会访问你机器上已有的 NI 目录，但**只做必要的读写**：

| 路径 | 用途 | 权限 |
| --- | --- | --- |
| `C:\Program Files (x86)\National Instruments\Circuit Design Suite 14.3\` | 启动 `multisim.exe` | 只读 |
| `C:\ProgramData\National Instruments\Circuit Design Suite 14.3\database\` | 主数据库（Multisim 自己读写） | 本工具不直接写 |
| `%USERPROFILE%\Documents\National Instruments\Circuit Design Suite 14.3\` | 你保存的设计文件 | **会被读写**，见下 |

⚠️ **关于设计文件**：`msim_circuit({ action: 'save_as' })` 会覆盖你指定路径上的同名文件，Multisim 不会询问。**不要让另存为的目标指向你唯一的一份设计**。同理，GUI 通道的 `msim_gui({ action: 'save_as' })` 只在文档尚未命名时可靠，文档已命名时它会直接报错而不是原地保存（这是刻意的防护，详见 [`PLUGIN.md`](PLUGIN.md)）。

中途产生的解码临时文件写在 `MSIM_MS14_WORKDIR`（若设置）、`%LOCALAPPDATA%\msim-ms14-tmp`、或仓库旁的 `_ms14_tmp` 之一，取第一个真正可写的；用完即删。

MIT，见 [`LICENSE`](LICENSE)。
