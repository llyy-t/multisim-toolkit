#!/usr/bin/env python3
"""msim_worker.py -- 常驻 32 位 Multisim worker（TCP + JSON lines）。

架构与契约见同目录 PROTOCOL.md。这个文件只做四件事：
  1. 起 TCP 服务并把端口/pid 报到 stdout（Host 靠这行判定就绪）；
  2. 独占 COM 公寓、串行派发 op 到 msim_com.MultisimSession 或 msim_gui 的函数；
  3. 把任何异常转成统一的 error 对象，保证 worker 不因单条 op 死掉；
  4. 父进程消失时自杀（避免残留一个握着 Multisim COM 会话的孤儿）。

用法：
    python msim_worker.py --port 0 --token <hex> [--parent-pid <pid>]
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

PROTOCOL = 1
MAX_LINE_BYTES = 64 * 1024 * 1024


def log(message: str) -> None:
    sys.stderr.write("[msim-worker] %s\n" % message)
    sys.stderr.flush()


class OpError(Exception):
    """带错误码的 op 失败。"""

    def __init__(self, code: str, message: str, detail: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


# --------------------------------------------------------------------------
# op 分发
# --------------------------------------------------------------------------

_state: dict = {
    "session": None,
    "com_error": None,
    "gui_error": None,
    "requests": 0,
    "started": time.time(),
}


def com_session():
    """惰性构造 MultisimSession；import 失败时给出可诊断的错误。"""
    session = _state.get("session")
    if session is not None:
        return session
    if _state.get("com_error"):
        raise OpError("INTERNAL", "COM layer unavailable", _state["com_error"])
    try:
        import msim_com  # noqa: F401  (同目录)
    except Exception as exc:
        _state["com_error"] = "%s: %s" % (type(exc).__name__, exc)
        raise OpError("INTERNAL", "COM layer unavailable", _state["com_error"]) from exc
    try:
        session = msim_com.MultisimSession()
    except Exception as exc:
        _state["com_error"] = "%s: %s" % (type(exc).__name__, exc)
        raise OpError("INTERNAL", "cannot construct MultisimSession", _state["com_error"]) from exc
    _state["session"] = session
    return session


def gui_module():
    if _state.get("gui_error"):
        raise OpError("INTERNAL", "GUI layer unavailable", _state["gui_error"])
    try:
        import msim_gui  # noqa: F401
    except Exception as exc:
        _state["gui_error"] = "%s: %s" % (type(exc).__name__, exc)
        raise OpError("INTERNAL", "GUI layer unavailable", _state["gui_error"]) from exc
    return msim_gui


def _wrap(exc: BaseException) -> dict:
    """把异常规范成 error 对象。"""
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None)
    detail = getattr(exc, "detail", None)
    if code and message:
        return {"code": str(code), "message": str(message), "detail": detail}
    if isinstance(exc, TypeError) and "unexpected keyword" in str(exc):
        return {"code": "BAD_ARGS", "message": str(exc), "detail": None}
    return {
        "code": "INTERNAL",
        "message": "%s: %s" % (type(exc).__name__, exc),
        "detail": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-6000:],
    }


def _balance(result):
    """确保返回值可 JSON 序列化；不可序列化的东西降级成字符串。"""
    try:
        json.dumps(result, ensure_ascii=False, allow_nan=False)
        return result
    except Exception:
        return json.loads(
            json.dumps(result, ensure_ascii=False, allow_nan=False, default=str)
        )


COM_OPS = {
    "ping",
    "connect",
    "shutdown",
    "new_circuit",
    "open_circuit",
    "save",
    "save_as",
    "circuit_info",
    "enum_components",
    "enum_inputs",
    "enum_outputs",
    "enum_variants",
    "get_active_variant",
    "set_active_variant",
    "enum_sections",
    "enum_parameters",
    "get_parameter",
    "set_parameter",
    "get_rlc",
    "set_rlc",
    "replace_component",
    "get_image",
    "report_netlist",
    "report_bom",
    "snippet",
    "sim_state",
    "stop",
    "pause",
    "resume",
    "set_output_request",
    "clear_output_request",
    "output_ready",
    "get_output_data",
    "run_transient",
    "run_dc",
    "run_ac_sweep",
    "run_ac_single",
    "set_input_raw",
    "set_input_sampled",
    "clear_input",
    "run_command_file",
}

GUI_OPS = {
    "probe",
    "launch",
    "windows",
    "main_window",
    "open_file",
    "screenshot",
    "focus",
    "close_dialogs",
    "dismiss_database_dialogs",
    "save_as",
    "send_keys",
    "kill",
    "run_analysis",
    "capture_graph",
    "graph_windows",
}


def dispatch(op: str, args: dict) -> object:
    if op == "worker.ping":
        return {
            "protocol": PROTOCOL,
            "pid": os.getpid(),
            "python_bits": 8 * __import__("struct").calcsize("P"),
            "python": sys.version.split()[0],
            "requests": _state["requests"],
            "uptime": round(time.time() - _state["started"], 3),
            "com_loaded": _state.get("session") is not None,
        }
    if op == "worker.status":
        session = _state.get("session")
        info = {"pid": os.getpid(), "requests": _state["requests"]}
        if session is not None:
            try:
                info["com"] = _balance(session.ping())
            except Exception as exc:
                info["com"] = _wrap(exc)
        return info
    if op == "open_any":
        return _open_any(args)
    if op == "spice.run":
        return _spice_run(args)
    if op == "spice.verify":
        return _spice_verify(args)
    if op == "design.read":
        return _design_read(args)
    if op.startswith("com."):
        name = op[4:]
        if name not in COM_OPS:
            raise OpError("BAD_ARGS", "unknown com op: %s" % name)
        method = getattr(com_session(), name, None)
        if method is None:
            raise OpError("BAD_ARGS", "com op not implemented: %s" % name)
        return method(**(args or {}))
    if op.startswith("gui."):
        name = op[4:]
        if name not in GUI_OPS:
            raise OpError("BAD_ARGS", "unknown gui op: %s" % name)
        function = getattr(gui_module(), name, None)
        if function is None:
            raise OpError("BAD_ARGS", "gui op not implemented: %s" % name)
        return function(**(args or {}))
    raise OpError("BAD_ARGS", "unknown op: %s" % op)


def _spice_run(args: dict):
    """纯 SPICE 通道：网表 -> DoCommandLine -> raw -> 表格/CSV/PNG。"""
    import msim_deck

    try:
        return msim_deck.run_deck(
            com_session(),
            netlist=args.get("netlist"),
            netlist_path=args.get("netlist_path"),
            commands=args.get("commands"),
            output_dir=args.get("output_dir"),
            max_points=int(args.get("max_points") or 200),
            plot=bool(args.get("plot", True)),
            timeout=float(args.get("timeout") or 180.0),
            series=args.get("series"),
        )
    except msim_deck.DeckError as exc:
        raise OpError(exc.code, exc.message, exc.detail) from exc
    except Exception as exc:  # msim_spice.SpiceError 也落在这里
        code = getattr(exc, "code", None) or "SPICE_ERROR"
        raise OpError(code, str(exc), getattr(exc, "detail", None)) from exc


def _spice_verify(args: dict):
    """不跑仿真，只回一份网表自检结果：行数、器件、分析命令、预计输出列。"""
    import msim_spice

    netlist = args.get("netlist")
    if not netlist and args.get("netlist_path"):
        path = os.path.abspath(str(args["netlist_path"]))
        if not os.path.isfile(path):
            raise OpError("PATH_MISSING", "netlist not found: %s" % path)
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            netlist = handle.read()
    if not netlist:
        raise OpError("BAD_ARGS", "netlist text or netlist_path is required")
    try:
        deck = msim_spice.normalise_netlist(str(netlist))
        plan = msim_spice.normalise_commands(args.get("commands"))
    except msim_spice.SpiceError as exc:
        raise OpError(exc.code, exc.message, exc.detail) from exc
    elements = []
    for line in deck.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("*") or stripped.startswith("."):
            continue
        head = stripped.split()[0]
        elements.append({"designator": head, "line": stripped})
    return {
        "lines": len([line for line in deck.split("\n") if line.strip()]),
        "elements": elements,
        "commands": plan,
        "notes": [
            "Multisim's command engine understands standard SPICE3 elements "
            "(R/C/L/V/I/D/Q/M/E/F/G/H and B behavioural sources).",
            "Inline .subckt works: an X line instantiates it, and its internal "
            "nodes appear as V(<node>) and its sources as I(<name>:<x>).",
            "Library parts addressed as X<name> ... <library model> (74LS00 etc.) "
            "are NOT expanded here; only the plain SPICE subset runs.",
            "The deck must be ASCII and must contain a ground node, otherwise the "
            "engine answers 'No nodes present in circuit'.",
        ],
    }


def _design_read(args: dict):
    """离线读 .ms14：解开 PKWare 容器，回结构摘要（不碰 COM、不看窗口）。"""
    import msim_ms14

    path = str(args.get("path") or "").strip()
    if not path:
        raise OpError("BAD_ARGS", "path is required")
    try:
        return msim_ms14.summary(
            path,
            include_xml=bool(args.get("include_xml")),
            xml_limit=int(args.get("xml_limit") or 200000),
        )
    except msim_ms14.Ms14Error as exc:
        raise OpError(exc.code, exc.message, exc.detail) from exc


def _open_any(args: dict):
    """统一入口：.ms14 走 COM OpenFile，其它后缀走 GUI 拖放（COM 对 .cir 会崩）。"""
    path = str(args.get("path") or "").strip()
    if not path:
        raise OpError("BAD_ARGS", "path is required")
    path = os.path.abspath(path)
    if not os.path.exists(path):
        raise OpError("PATH_MISSING", "file not found: %s" % path)
    suffix = os.path.splitext(path)[1].lower()
    if suffix in (".ms14", ".msm", ".ewb", ".ms11", ".ms12", ".ms13"):
        info = com_session().open_circuit(path)
        return {"route": "com", "path": path, "circuit": info}
    gui = gui_module()
    opened = gui.open_file(path=path)
    return {"route": "gui", "path": path, "gui": opened}


# --------------------------------------------------------------------------
# 传输层
# --------------------------------------------------------------------------


def handle_request(line: str, token: str, authorized: bool) -> tuple[dict, bool]:
    """返回 (应答对象, 是否仍然授权)。"""
    try:
        request = json.loads(line)
    except Exception as exc:
        return ({"id": None, "ok": False, "error": {"code": "BAD_ARGS",
                 "message": "invalid JSON", "detail": str(exc)}}, authorized)
    if not isinstance(request, dict):
        return ({"id": None, "ok": False, "error": {"code": "BAD_ARGS",
                 "message": "request must be a JSON object", "detail": None}}, authorized)

    request_id = request.get("id")
    op = request.get("op")
    args = request.get("args") or {}

    if op == "hello":
        supplied = str((args or {}).get("token") or "")
        if supplied != token:
            return ({"id": request_id, "ok": False, "error": {
                "code": "BAD_ARGS", "message": "bad token", "detail": None}}, False)
        return ({"id": request_id, "ok": True, "result": {
            "protocol": PROTOCOL,
            "pid": os.getpid(),
            "python_bits": 8 * __import__("struct").calcsize("P"),
            "python": sys.version.split()[0],
            "multisim_mcp": _multisim_mcp_version(),
        }}, True)

    if not authorized:
        return ({"id": request_id, "ok": False, "error": {
            "code": "BAD_ARGS", "message": "handshake required", "detail": None}}, False)

    if not isinstance(op, str) or not op:
        return ({"id": request_id, "ok": False, "error": {
            "code": "BAD_ARGS", "message": "op is required", "detail": None}}, True)

    _state["requests"] += 1
    try:
        result = dispatch(op, args if isinstance(args, dict) else {})
        return ({"id": request_id, "ok": True, "result": _balance(result)}, True)
    except Exception as exc:
        return ({"id": request_id, "ok": False, "error": _wrap(exc)}, True)


def _multisim_mcp_version() -> str | None:
    try:
        import multisim_mcp

        return getattr(multisim_mcp, "__version__", None)
    except Exception:
        return None


def serve(sock: socket.socket, token: str) -> None:
    while True:
        try:
            conn, peer = sock.accept()
        except OSError:
            return
        log("client %s" % (peer,))
        try:
            _serve_connection(conn, token)
        except Exception as exc:
            log("connection ended: %s: %s" % (type(exc).__name__, exc))
        finally:
            try:
                conn.close()
            except Exception:
                pass
            log("client gone")


def _serve_connection(conn: socket.socket, token: str) -> None:
    conn.settimeout(None)
    reader = conn.makefile("rb")
    writer = conn.makefile("wb")
    authorized = False
    try:
        while True:
            raw = reader.readline(MAX_LINE_BYTES)
            if not raw:
                return
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            if line == "\x00ping":
                writer.write(b"\x00pong\n")
                writer.flush()
                continue
            response, authorized = handle_request(line, token, authorized)
            payload = json.dumps(response, ensure_ascii=False, default=str)
            writer.write(payload.encode("utf-8") + b"\n")
            writer.flush()
            if not authorized:
                return
    finally:
        for handle in (reader, writer):
            try:
                handle.close()
            except Exception:
                pass


def _watch_parent(parent_pid: int) -> None:
    """父进程（DSH Host）消失后自杀，防止孤儿 worker 握着 COM 会话。"""
    if parent_pid <= 0:
        return

    def loop() -> None:
        while True:
            time.sleep(1.0)
            if not _pid_alive(parent_pid):
                log("parent %d is gone, exiting" % parent_pid)
                os._exit(0)

    threading.Thread(target=loop, name="parent-watch", daemon=True).start()


def _pid_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Multisim automation worker")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--token", default="")
    parser.add_argument("--parent-pid", type=int, default=0)
    parsed = parser.parse_args(argv)

    token = parsed.token or ""

    # COM 公寓必须在真正调用 COM 的线程里初始化（本进程的主线程，串行服务）。
    com_ready = False
    try:
        import pythoncom

        pythoncom.CoInitialize()
        com_ready = True
    except Exception as exc:
        log("CoInitialize failed: %s" % exc)

    _watch_parent(parsed.parent_pid)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((parsed.host, parsed.port))
    sock.listen(4)
    port = sock.getsockname()[1]

    ready = {
        "event": "ready",
        "protocol": PROTOCOL,
        "host": parsed.host,
        "port": port,
        "pid": os.getpid(),
        "python": sys.version.split()[0],
        "python_bits": 8 * __import__("struct").calcsize("P"),
        "com_apartment": com_ready,
        "multisim_mcp": _multisim_mcp_version(),
    }
    sys.stdout.write(json.dumps(ready, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    log("listening on %s:%d" % (parsed.host, port))

    try:
        serve(sock, token)
    finally:
        try:
            sock.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
