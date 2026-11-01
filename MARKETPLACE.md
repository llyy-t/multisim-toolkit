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
  en: "Drive the NI Multisim 14.3 install on this machine: nine msim_* tools cover COM automation (open/save .ms14, read and edit component values, run DC/AC/transient sweeps, export schematics, BOM and netlists), a Win32 channel for the visible window (drive the Analyses dialog, capture the schematic and the waveform plot), a SPICE command-file engine for designs without probes, and an offline .ms14 reader that needs no Multisim running at all."
  zh: "把本机的 NI Multisim 14.3 变成可编程对象：九个 msim_* 工具覆盖 COM 自动化（开/存 .ms14、读写元件值、跑 DC/AC/瞬态分析、导出原理图/BOM/网表）、可见窗口的 Win32 通道（操作分析对话框、截取电路图与实机波形图）、无需探针的 SPICE 命令文件引擎，以及完全不启动 Multisim 的 .ms14 离线解析。"
icon: https://raw.githubusercontent.com/llyy-t/multisim-toolkit/main/icon.svg

# ---------------------------------------------------------------------------
# Optional: screenshots.json at the repository root (1-8 entries, relative
# paths inside this repo, no leading slash, no ".."). Already prepared as
# screenshots.json in this repo.
# ---------------------------------------------------------------------------
