# scripts

两个脚本，都不需要 DSH 参与就能跑完。

| 脚本 | 用途 | 需要 |
| --- | --- | --- |
| `install.ps1` | 体检本机环境；打印该发给 DSH 的 `plugin_manager(...)` 调用 | Windows PowerShell 5.1+ |
| `selfcheck.py` | 逐层验证插件自带的 Python 代码（离线层 / 可选活体层） | 32 位 Python 3.11+ 与 pywin32 |

## install.ps1

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Action check
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Action install
```

`-Action`：`check`（默认）/ `install` / `enable` / `disable` / `reload` / `remove`。
`-Python <路径>` 指定解释器，不给就按顺序自动找：`MSIM_PYTHON` → `MULTISIM_MCP_PYTHON` →
`%DSH_HOME%\multisim-mcp\py32\python.exe` → `%USERPROFILE%\.dsh\multisim-mcp\py32\python.exe` →
`C:\Python313-32` / `312-32` / `311-32`。

**为什么脚本不自己装插件**：DSH 是独立进程，插件必须由 DSH 自己的 `plugin_manager` 工具安装。
脚本只负责体检 + 把要粘贴的那一行打出来。

> **维护提醒：`install.ps1` 必须保持纯 ASCII。**
> Windows PowerShell 5.1 读取**无 BOM** 的脚本时按 ANSI 解码（中文系统上是 GBK）。一个被截断的
> 多字节字符足以吞掉字符串的收尾引号，整个文件随即解析失败，报错却指向无关的行（实测过一次：
> 中文提示语被截断 → 报 `The term 'else' is not recognized`）。
> 要写中文提示就把文件存成 **UTF-8 with BOM**，或者把中文留在 `Write-Host` 之外的地方。

## selfcheck.py

```powershell
python scripts\selfcheck.py
python scripts\selfcheck.py --design "C:\path\to\some.ms14"
python scripts\selfcheck.py --live --design "C:\path\to\some.ms14"
```

三层，从便宜到贵：

1. **interpreter**：`sys.executable` 是不是 32 位、`win32com`/`pythoncom` 在不在；
2. **worker modules**：`msim_ms14` / `msim_raster` / `msim_spice` / `msim_deck` 能否 import，`.ms14` 解码器在不在；
3. **（`--design`）容器**：真解一个 `.ms14`，打印 block 数、解码字节数、前几个元件与其值；
4. **（`--live`）活体**：启动一只**隐藏**的 Multisim 实例（`Connect()`），列输出与元件，然后 `shutdown()`。

`--live` 会真的拉起 `multisim.exe`，占 4–8 秒；不传就不碰 Multisim。

`Connect()` 之后**没有打开的电路**，所以不带 `--design` 时 `enum_outputs` 会直接跳过——
"没有电路就没有输出可枚举"是 Multisim 的语义，不是故障。
