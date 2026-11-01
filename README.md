# multisim-toolkit

一个把 **NI Multisim 14.3** 变成可编程对象的 DSH skill。

它由两部分组成：

1. **`vendor/dsh-multisim-toolkit/`** —— 一个 DSH 插件（Host 侧 Cordis bundle），注册 9 个 `msim_*` 工具；
2. **本仓库的 `SKILL.md` + `references/`** —— 教模型怎么正确使用这些工具，以及所有踩过的坑。

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
| `msim_gui` | 驱动可见窗口：启动、截图、拖入网表、关对话框、另存为、发送按键 |
| `msim_spice` | 把网表直接喂给 Multisim 的 SPICE 引擎，返回表格 + CSV + 图 |
| `msim_design` | 不启动 Multisim，离线读取 `.ms14` 里的元件、值、仪器设置 |

多数操作在首次 `Connect()`（约 6 秒，Multisim 启动开销）之后是**毫秒级**——实测 10 次连续读操作 median `0.000s`。

## 安装

前提：

- Windows，已安装 **Multisim 14.3**（`C:\Program Files (x86)\National Instruments\Circuit Design Suite 14.3\`）；
- 一个 **32 位** Python 3.11–3.13 并装了 pywin32（Multisim 的 Automation API 是 32 位 COM 服务器，64 位解释器 `Connect()` 会 `E_ACCESSDENIED`）；
- DSH（本插件是 Host-only bundle，不需要浏览器端代码）。

```powershell
# 1) 体检
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Action check

# 2) 在 DSH 会话里装插件（install.ps1 会把这条指令原样打出来）
#    plugin_manager({ action: 'install_bundle', target: '<仓库绝对路径>\vendor\dsh-multisim-toolkit' })

# 3) 验证
#    msim_status({ connect: true })
```

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
| [`SKILL.md`](SKILL.md) | 面向模型：先选通道，再动手；五条最贵的判断；参数速查 |
| [`references/com-automation.md`](references/com-automation.md) | COM 面完整参考：每个方法、枚举值、8 类失败的症状与规避、类型库注册信息 |
| [`references/spice-channel.md`](references/spice-channel.md) | SPICE 通道：语法、用 B 源写数字门、子电路、4 个致命坑、9 种分析命令 |
| [`references/gui-and-files.md`](references/gui-and-files.md) | 可见窗口（含 Save As 三个真实根因）与 `.ms14` 容器格式、元件值三种形态 |
| [`vendor/dsh-multisim-toolkit/README.md`](vendor/dsh-multisim-toolkit/README.md) | 插件本体说明与 24 条实测边界 |
| [`vendor/dsh-multisim-toolkit/worker/PROTOCOL.md`](vendor/dsh-multisim-toolkit/worker/PROTOCOL.md) | 插件内部模块契约（要改代码时读） |

## 这个仓库为什么值得看

除了"又一层包装"，这里有几条别人不太容易自己撞出来的实测结论：

- **Multisim 的 COM 服务器与客户端同生命周期**——Python 进程一退出，`multisim.exe` 就消失。所以必须常驻 worker，不能每条命令起一个短命进程。
- **`app.OpenFile()` 对 SPICE 网表会让 Multisim 静默崩溃**（无任何可捕获错误）。网表只能通过 `WM_DROPFILES` 注入到可见窗口。
- **GUI 另存为不能靠 `WM_SETTEXT`**——它只改编辑框的显示文本，不改对话框内部状态，Multisim 仍按旧名字写盘，会静默覆盖同名文件。必须用 `SendInput` + `KEYEVENTF_UNICODE` 逐字符输入并在保存前回读校验。
- **数据库告警框的文字藏在跨进程 Edit 控件里**，`GetWindowText` 读到空串，必须用 `WM_GETTEXT`；这些模态框会吞掉之后所有按键。
- **`.ms14` 是 36 字节魔数 + `u64` 长度 + 若干段 PKWare DCL "implode" 数据**，zlib/bz2/lzma/cabinet.dll 全都解不开；用 Mark Adler 的 `blast.c` 编译出的薄壳解压，毫秒级。
- **`.ms14` 里元件值的"真正来源"是 `CiaParamList.parameters`**，另一条 `<doubles>` 数组是可能已过期的遗留初值（实测某电阻 `doubles` 还是 1000，`parameters` 已经是 `2.2k`）；`&UNI..._uc<hex>` 的 `_uc` 后缀是进程内指针号，不可逆。
- **截图 `crop=content` 的墨迹判据必须是"暗 或 有彩度"**——只按"暗"判断会漏掉全部蓝色导线（Multisim 的连线是纯蓝 `(0,0,255)`）。

## 能力边界

诚实地说，这个插件**不能**凭空在原理图上放元件、画连线——Multisim 的 Automation API 没有对应方法，Win32 合成鼠标画线也不可靠。新电路的可行路径只有两条：

1. 写网表，拖进可见窗口，Multisim 会自动还原成带真实库器件和连线的**可编辑原理图**；
2. 直接生成/修改 `.ms14`（容器格式已破解，但写回收尚未实现）。

其它边界见 `vendor/dsh-multisim-toolkit/README.md` 的「已知边界」一节（24 条，都是实测结论）。

## 许可

MIT。第三方组件的许可见 [`LICENSE`](LICENSE)：`worker/bin/blast.c` / `blast.h` 来自 zlib（zlib license，Mark Adler）。

本项目与 National Instruments 无关，不包含也不分发任何 NI 软件；它只是驱动你本机已装好的 Multisim。
