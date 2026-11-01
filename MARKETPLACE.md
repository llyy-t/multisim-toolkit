# DSH plugin marketplace submission
#
# Target repository: https://github.com/awesome-dsh-plugin/awesome-dsh-plugin
# File to add:       data/plugins/llyy-t__multisim-toolkit.yml
#
# Hard requirements from contributing.md, checked against this repo:
#   [x] package.json declares dsh.bundle.patch  -> "./cordis.patch.yml"
#   [x] real, working code (9 msim_* tools, live-verified on Multisim 14.3)
#   [x] description matches what the code actually does
#   [x] not an aggregator: this repo IS the plugin
#   [ ] repository older than 1 day -> created 2026-11-01 19:55 +08:00,
#       so the PR can only be opened from 2026-11-02 19:55 (+08:00) onwards
#   [ ] topic "dsh-plugin" added on GitHub (Settings -> Topics)
#
# One PR adds one YAML file. Do not touch the generated README.
#
# ---------------------------------------------------------------------------
# data/plugins/llyy-t__multisim-toolkit.yml
# ---------------------------------------------------------------------------

url: https://github.com/llyy-t/multisim-toolkit
name: llyy-t/multisim-toolkit
category: tools
description:
  en: "Drive the NI Multisim 14.3 install on this machine: nine msim_* tools open and save .ms14 designs, read and edit component values and circuit parameters, run DC operating point, AC sweep, single-frequency AC and transient analyses, and export schematics, BOM and netlists. A Win32 channel drives the visible window to run the Analyses dialog and capture Multisim's own schematic view and waveform plot, a SPICE command-file engine handles designs that expose no probes, and an offline reader unpacked the .ms14 container without starting Multisim at all."
  zh: "把本机的 NI Multisim 14.3 变成可编程对象：九个 msim_* 工具可开关 `.ms14` 设计、读写元件值与电路参数、跑直流工作点/交流扫频/单频交流/瞬态分析、导出原理图与 BOM/网表；Win32 通道驱动可见窗口操作分析对话框并截取 Multisim 自己的电路视图与波形图；SPICE 命令文件引擎处理没有探针的设计；离线解析器完全不启动 Multisim 就能读出 `.ms14` 内容。"
icon: https://raw.githubusercontent.com/llyy-t/multisim-toolkit/main/icon.svg

# ---------------------------------------------------------------------------
# Optional: screenshots.json at the repository root (1-8 entries, relative
# paths inside this repo, no leading slash, no ".."). Already prepared as
# screenshots.json in this repo.
# ---------------------------------------------------------------------------
