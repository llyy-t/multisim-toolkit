#!/usr/bin/env python3
"""msim_deck.py -- 用 Multisim 的 SPICE 命令引擎跑一个网表。

不经过原理图、不需要探针、不需要可见窗口：把网表写成 .cir，写一个包含
``source`` / 分析命令 / ``write`` 的命令文件，交给 COM 的 ``DoCommandLine``，
再把 Multisim 写出的 SPICE3 raw 解析成表格 / CSV / PNG。

对外只有一个入口 :func:`run_deck`，参数与返回值都是 JSON 可序列化的字典，
实现细节（文件布局、抽样、出图）都在内部。
"""

from __future__ import annotations

import os
import tempfile
import time
from typing import Any, Dict, List, Optional, Sequence

import msim_raster
import msim_spice


class DeckError(Exception):
    """带错误码的失败（与 worker 的 OpError 形状一致）。"""

    def __init__(self, code: str, message: str, detail: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


def _resolve_dir(output_dir: Optional[str]) -> str:
    """选一个不含空格的目录放命令文件（Multisim 命令引擎拒绝带空格的路径）。"""
    if output_dir:
        target = os.path.abspath(os.path.expanduser(str(output_dir)))
        if " " in target:
            raise DeckError(
                "BAD_ARGS",
                "output_dir must not contain spaces: %s" % target,
                "Multisim's command engine rejects command-file paths that contain "
                "spaces. Use a path such as C:\\msim_deck\\run1.",
            )
        os.makedirs(target, exist_ok=True)
        return target
    base = os.environ.get("TEMP") or tempfile.gettempdir()
    if " " in base:
        base = "C:\\msim_deck"
    target = os.path.join(base, "msim_deck_%d" % int(time.time() * 1000))
    os.makedirs(target, exist_ok=True)
    return target


def _write(path: str, text: str) -> str:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return path


def _ensure_circuit(session: Any) -> bool:
    """Multisim's command engine hangs off a document, so a blank one is needed.

    Returns True when a document had to be created. An already open design is
    never replaced: the deck runs against it without touching its contents.
    """
    try:
        session.circuit_info()
        return False
    except Exception as exc:
        if getattr(exc, "code", "") != "NO_CIRCUIT":
            raise
    session.new_circuit()
    return True


def run_deck(
    session: Any,
    netlist: Optional[str] = None,
    netlist_path: Optional[str] = None,
    commands: Optional[Sequence[str]] = None,
    output_dir: Optional[str] = None,
    max_points: int = 200,
    plot: bool = True,
    timeout: float = 180.0,
    series: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """跑一次网表，返回每个分析的表格、CSV 与 PNG。

    ``session`` 是 ``msim_com.MultisimSession``（只为调用 ``run_command_file``）。
    ``series`` 只在出图时用：给就只画这些列（名字与表格里的 ``columns`` 一致）。
    """
    if netlist and netlist_path:
        raise DeckError("BAD_ARGS", "pass either netlist or netlist_path, not both")
    work = _resolve_dir(output_dir)

    if netlist_path:
        source = os.path.abspath(os.path.expanduser(str(netlist_path)))
        if not os.path.isfile(source):
            raise DeckError("PATH_MISSING", "netlist not found: %s" % source)
        if " " in source:
            raise DeckError(
                "BAD_ARGS",
                "netlist path must not contain spaces: %s" % source,
                "Copy the netlist somewhere without spaces, or pass it as netlist text.",
            )
        with open(source, "r", encoding="utf-8", errors="replace") as handle:
            netlist = handle.read()
    if netlist is None or not str(netlist).strip():
        raise DeckError("BAD_ARGS", "netlist text or netlist_path is required")

    deck = msim_spice.normalise_netlist(str(netlist))
    plan = msim_spice.normalise_commands(commands)
    cir = _write(os.path.join(work, "circuit.cir"), deck)
    raws = [os.path.join(work, "plot%d.raw" % index) for index in range(len(plan))]
    log = os.path.join(work, "run.log")
    commands_file = _write(
        os.path.join(work, "run.cmd"), msim_spice.build_command_file(cir, raws, plan)
    )

    created_circuit = _ensure_circuit(session)
    result = session.run_command_file(commands_file, log, timeout=float(timeout))
    elapsed = result.get("elapsed_seconds", result.get("elapsed"))
    if result.get("timed_out"):
        raise DeckError(
            "TIMEOUT",
            "Multisim did not finish the analysis in %.0fs" % float(timeout),
            str(result.get("log") or "")[-2000:],
        )
    if result.get("state") not in (0, None):
        raise DeckError(
            "COM_ERROR",
            "the SPICE engine did not stop cleanly (state=%s)" % result.get("state"),
            (str(result.get("last_error") or "") + "\n" + str(result.get("log") or ""))[-2000:],
        )

    analyses: List[Dict[str, Any]] = []
    missing: List[str] = []
    for command, raw in zip(plan, raws):
        if not os.path.isfile(raw) or os.path.getsize(raw) == 0:
            missing.append(command)
            continue
        with open(raw, "rb") as handle:
            payload = handle.read()
        try:
            parsed = msim_spice.parse_raw(payload)
        except msim_spice.SpiceError as exc:
            missing.append("%s (%s)" % (command, exc.message))
            continue
        entry: Dict[str, Any] = {
            "command": command,
            "plotname": parsed["plotname"],
            "columns": parsed["columns"],
            "n_points": parsed["n_points"],
        }
        rows, step = msim_spice.sample_rows(parsed["rows"], int(max_points))
        entry["sampled_step"] = step
        entry["rows"] = [
            [None if value is None else round(float(value), 9) for value in row] for row in rows
        ]
        entry["summary"] = msim_spice.summarise(parsed)
        try:
            entry["csv"] = msim_spice.write_csv(
                os.path.join(work, "plot%d.csv" % len(analyses)), parsed
            )
        except Exception as exc:  # never fail the whole run for a side artefact
            entry["csv"] = {"error": str(exc)}
        if plot:
            try:
                entry["png"] = msim_spice.write_plot_png(
                    os.path.join(work, "plot%d.png" % len(analyses)),
                    parsed,
                    series=series,
                    title=parsed["plotname"],
                    raster=msim_raster,
                )
            except Exception as exc:
                entry["png"] = {"error": str(exc)}
        analyses.append(entry)

    if not analyses:
        raise DeckError(
            "COM_ERROR",
            "the SPICE engine produced no usable plot",
            "commands: %s; log tail: %s"
            % ("; ".join(plan), str(result.get("log") or "")[-2000:]),
        )

    return {
        "directory": work,
        "netlist": cir,
        "command_file": commands_file,
        "log_file": log,
        "state": result.get("state"),
        "elapsed_seconds": elapsed,
        "created_circuit": created_circuit,
        "commands": plan,
        "analyses": analyses,
        "missing": missing,
    }


__all__ = ["DeckError", "run_deck"]
