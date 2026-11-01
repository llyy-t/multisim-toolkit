#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""msim_com.py -- Multisim COM layer for the msim worker.

Contract: PROTOCOL.md §4.1 (this file implements it verbatim; do not rename
methods or return fields without editing PROTOCOL.md first).

Environment facts baked in here (all measured on this machine, see §5):
  * 32-bit Python only.  A 64-bit interpreter cannot activate the COM server.
  * ProgID ``MultisimInterface.MultisimApp``.  ``app.Connect()`` spawns a
    **hidden** multisim.exe; that process lives exactly as long as the client
    process that called ``Connect()`` first.
  * ``app.Disconnect()`` **closes Multisim**.  It is called from
    ``MultisimSession.shutdown()`` and nowhere else -- not from ``__init__``,
    not from ``__del__``, not from the self-test teardown except through
    ``shutdown()``.
  * COM access is late/lazy: ``__init__`` does zero COM work so that importing
    or constructing the session never launches Multisim.
  * ``GetCircuitImage(fmt, path)`` ignores the extension and writes content
    determined by ``fmt``; the real format is verified from the magic bytes
    afterwards and the file is renamed to match.
  * ``Save()`` on a circuit that has never been saved (empty ``FileName``)
    corrupts Multisim's heap and kills the process (0xC0000374).  ``save()``
    therefore refuses in that state and asks for ``save_as()`` first.
  * Analysis members (``DoDCOperatingPoint`` / ``DoACSweep`` /
    ``DoACSingleFrequency`` / ``SetOutputRequest`` / ``OutputReady`` /
    ``GetOutputData`` / ``PauseSimulation`` / ``ResumeSimulation`` /
    ``StopSimulation``) all raise ``com_error(-2147467259, '未指定的错误')`` on a
    circuit with no probes/instruments, with ``LastErrorMessage`` ==
    "输出未找到。" ("output not found").  That is *not* a bug in this layer:
    the message is surfaced as ``MsimError("COM_ERROR", ...)`` with
    ``LastErrorMessage`` appended so the caller can tell it apart from a real
    failure.  ``stop`` / ``pause`` / ``resume`` deliberately swallow it
    (``pause`` must never raise, see contract).

No ``print()`` anywhere except inside ``_selftest()`` (guarded by
``__main__``): stdout of the worker is the crash-diagnostics channel.
"""

from __future__ import annotations

import json
import math
import os
import struct
import sys
import time
import traceback
from typing import Any, Iterable, List, Optional, Sequence

__all__ = [
    "MsimError",
    "MultisimSession",
    "clean_error",
    "bstr_array",
    "r8_array",
    "r8_matrix",
    "PROG_ID",
]

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

PROG_ID = "MultisimInterface.MultisimApp"

#: how long ``connect()`` keeps retrying; ``Connect()`` intermittently returns
#: E_ACCESSDENIED on the first attempt of a freshly spawned process.
CONNECT_ATTEMPTS = 6
CONNECT_RETRY_DELAY = 1.0

#: polling cadence while waiting for analysis outputs.
POLL_INTERVAL = 0.05
#: give up early (instead of burning the whole timeout) when ``OutputReady``
#: raises on every single poll -- happens on circuits without probes.
POLL_ERROR_GRACE = 2.0

MAX_LOG_CHARS = 20000
MAX_REPORT_CHARS = 20000

#: ``IMultisimCircuit.ReplaceComponent`` (DISPID 29) is not usable on this
#: build: the type library declares an 8th ``[out]`` parameter that pywin32's
#: generated stub never forwards, so Multisim writes through a pointer the
#: caller never supplied and the automation server dies with a heap corruption
#: (exit 0xC0000374).  Verified live against three designs with 7-argument
#: early binding, 7-argument late binding and 7-argument raw ``IDispatch``;
#: supplying the 8th parameter explicitly returns DISP_E_BADPARAMCOUNT.
#: The call is therefore refused unless the operator opts in.
REPLACE_COMPONENT_ALLOWED = os.environ.get(
    "MSIM_ALLOW_REPLACE_COMPONENT", ""
).strip().lower() in ("1", "true", "yes", "on")

# --- SimulationState -------------------------------------------------------
STATE_STOPPED = 0
STATE_RUNNING = 1
STATE_PAUSED = 2
STATE_NAMES = {
    STATE_STOPPED: "Stopped",
    STATE_RUNNING: "Running",
    STATE_PAUSED: "Paused",
}

# --- SimulationIOType ------------------------------------------------------
IO_ALL = 0
IO_VOLTAGE = 1
IO_CURRENT = 2
IO_POWER = 3
IO_DIGITAL = 4
IO_NAMES = {
    IO_ALL: "All",
    IO_VOLTAGE: "Voltage",
    IO_CURRENT: "Current",
    IO_POWER: "Power",
    IO_DIGITAL: "Digital",
}

# --- SimulationInterpolation ----------------------------------------------
INTERP_RAW = 0
INTERP_FORCE_STEP = 1
INTERP_LINEAR = 2
INTERP_SPLINE = 3
INTERP_COERCE = 4

# --- SweepType -------------------------------------------------------------
SWEEP_DECADE = 0
SWEEP_OCTAVE = 1
SWEEP_LINEAR = 2
SWEEP_NAMES = {SWEEP_DECADE: "decade", SWEEP_OCTAVE: "octave", SWEEP_LINEAR: "linear"}

# --- ExportFormat ----------------------------------------------------------
EXPORT_TEXT = 0
EXPORT_CSV = 1
EXPORT_FORMATS = {"text": EXPORT_TEXT, "txt": EXPORT_TEXT, "csv": EXPORT_CSV}

# --- ComponentType ---------------------------------------------------------
COMPONENT_ALL = 0
COMPONENT_ALL_RLC = 1
COMPONENT_RESISTOR = 2
COMPONENT_CAPACITOR = 3
COMPONENT_INDUCTOR = 4

# --- MultisimDB ------------------------------------------------------------
DB_MASTER = 0
DB_CORPORATE = 1
DB_USER = 2
DB_NAMES = {
    "master": DB_MASTER,
    "corporate": DB_CORPORATE,
    "user": DB_USER,
}

# --- CircuitImageFormat ----------------------------------------------------
IMAGE_PNG = 0
IMAGE_JPG = 1
IMAGE_BMP = 2
IMAGE_FORMATS = {
    "png": IMAGE_PNG,
    "jpg": IMAGE_JPG,
    "jpeg": IMAGE_JPG,
    "bmp": IMAGE_BMP,
}
IMAGE_EXTENSIONS = {IMAGE_PNG: "png", IMAGE_JPG: "jpg", IMAGE_BMP: "bmp"}

# --- CircuitParameterLevel -------------------------------------------------
PARAM_LEVEL_ALL = 0
PARAM_LEVEL_TOP = 1

#: extensions ``app.OpenFile()`` is reliable on.  Everything else goes through
#: the GUI (``msim_gui.open_file``): ``OpenFile`` crashes the process on a
#: plain ``.cir`` netlist.
OPENABLE_EXTENSIONS = (".ms14",)


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class MsimError(Exception):
    """Error with a stable ``code`` for the worker's JSON error object."""

    def __init__(self, code: str, message: str, detail: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.detail = None if detail is None else str(detail)

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}

    def __str__(self) -> str:  # keeps tracebacks readable
        return self.message


# --------------------------------------------------------------------------
# low level helpers
# --------------------------------------------------------------------------

_COM: dict = {}


def _com() -> tuple:
    """Import and cache the COM modules (never activates the server)."""
    if not _COM:
        try:
            import pythoncom  # type: ignore
            import win32com.client as win32_client  # type: ignore
        except Exception as exc:  # pragma: no cover - environment problem
            raise MsimError(
                "NO_MULTISIM",
                "pywin32 is not importable; the Multisim COM layer needs 32-bit pywin32",
                "%s: %s" % (type(exc).__name__, exc),
            ) from exc
        _COM["pythoncom"] = pythoncom
        _COM["win32"] = win32_client
    return _COM["pythoncom"], _COM["win32"]


def clean_error(text: Any) -> str:
    """Normalize a string coming out of Multisim.

    Multisim hands back UTF-8 bytes promoted to BSTR code points, so a Chinese
    message arrives as mojibake (``"è¾åºæªæ¾å°ã"``).  The ``latin-1 -> utf-8``
    round-trip fixes those, but it *fails* on strings that already carry real
    CJK code points (``latin-1`` cannot encode them) -- in that case the text
    is already correct and is returned untouched.  Same for ASCII.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return text
    try:
        return text.encode("latin-1").decode("utf-8", errors="replace")
    except Exception:
        return text


def _clean_list(values: Any) -> List[str]:
    if values is None:
        return []
    try:
        items = list(values)
    except TypeError:
        items = []
    out: List[str] = []
    for item in items:
        text = clean_error(item)
        out.append(text if isinstance(text, str) else str(text))
    return out


def bstr_array(values: Iterable[str]) -> Any:
    """Build the ``VT_ARRAY|VT_BSTR`` VARIANT Multisim expects for name lists."""
    pythoncom, win32_client = _com()
    return win32_client.VARIANT(
        pythoncom.VT_ARRAY | pythoncom.VT_BSTR, [str(v) for v in values]
    )


def r8_array(values: Iterable[float]) -> Any:
    """Build a 1-D ``VT_ARRAY|VT_R8`` VARIANT (``SetInputDataSampled``)."""
    pythoncom, win32_client = _com()
    return win32_client.VARIANT(
        pythoncom.VT_ARRAY | pythoncom.VT_R8, [float(v) for v in values]
    )


def r8_matrix(rows: Sequence[Sequence[float]]) -> Any:
    """Build a 2-D ``VT_ARRAY|VT_R8`` VARIANT (``SetInputDataRaw``).

    pywin32 3.13 has no ``SafeArray`` helper; a list of lists passed to
    ``VARIANT`` is marshalled as a 2-D safe array, which is exactly what
    ``SetInputDataRaw`` demands (a 1-D array fails with
    "expected an array with 2 dimensions ... instead received an array with
    1 dimensions").
    """
    pythoncom, win32_client = _com()
    nested = [[float(v) for v in row] for row in rows]
    return win32_client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, nested)


def _num(value: Any) -> Any:
    """Convert a COM cell to something JSON-serializable."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    try:
        return float(value)
    except Exception:
        return str(value)


def _as_float(value: Any, label: str) -> float:
    try:
        result = float(value)
    except Exception as exc:
        raise MsimError("BAD_ARGS", "%s must be a number" % label, str(exc)) from exc
    if math.isnan(result) or math.isinf(result):
        raise MsimError("BAD_ARGS", "%s must be a finite number" % label, repr(value))
    return result


def _as_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except Exception as exc:
        raise MsimError("BAD_ARGS", "%s must be an integer" % label, str(exc)) from exc


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off", "none")
    return bool(value)


def _as_str(value: Any, label: str, required: bool = True) -> str:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value
    else:
        text = str(value)
    text = text.strip()
    if required and not text:
        raise MsimError("BAD_ARGS", "%s is required" % label)
    return text


def _as_path(value: Any, label: str) -> str:
    text = _as_str(value, label)
    return os.path.abspath(os.path.expanduser(text))


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except Exception as exc:
            raise MsimError(
                "BAD_ARGS", "cannot create directory: %s" % parent, str(exc)
            ) from exc


def _state_name(state: int) -> str:
    return STATE_NAMES.get(int(state), "Unknown(%d)" % int(state))


def _design_suffix(path: str) -> str:
    """Return the Multisim design extension of ``path``, or ``""``.

    ``os.path.splitext`` is not enough: Multisim's AutoRecover writes files as
    ``"<name>.ms14 (Security copy)"``, for which ``splitext`` yields
    ``".ms14 (security copy)"`` and a plain equality test would reject a file
    that ``OpenFile`` opens perfectly well.  A qualifier in parentheses (or
    anything after whitespace starting with ``(``) is therefore tolerated.
    """
    base = os.path.basename(path).lower()
    for ext in OPENABLE_EXTENSIONS:
        index = base.rfind(ext)
        while index != -1:
            following = base[index + len(ext):]
            if not following or following.lstrip().startswith("("):
                return ext
            index = base.rfind(ext, 0, index)
    return ""


def _read_head(path: str, size: int = 262144) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read(size)
    except OSError:
        return b""


#: code pages Multisim may use when it writes text files.  The ANSI code page
#: of this machine is 936 (GBK), so a hard-coded utf-8 read would either raise
#: or silently substitute replacement characters.
_TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "gbk", "cp936", "latin-1")


def _read_text_file(path: str) -> str:
    """Read a file Multisim wrote, tolerating its unknown encoding."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError:
        return ""
    for encoding in _TEXT_ENCODINGS:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _png_size(data: bytes) -> tuple:
    """width/height from the IHDR chunk (bytes 16..24, big endian)."""
    if len(data) >= 24 and data[:8] == PNG_MAGIC and data[12:16] == b"IHDR":
        width, height = struct.unpack_from(">II", data, 16)
        return int(width), int(height)
    return 0, 0


def _bmp_size(data: bytes) -> tuple:
    """width/height from the BITMAPINFOHEADER (offset 18/22, int32 LE)."""
    if len(data) >= 26 and data[:2] == b"BM":
        width, height = struct.unpack_from("<ii", data, 18)
        return int(width), abs(int(height))
    return 0, 0


#: start-of-frame markers that carry the JPEG dimensions.
_JPEG_SOF = {
    0xC0, 0xC1, 0xC2, 0xC3,
    0xC5, 0xC6, 0xC7,
    0xC9, 0xCA, 0xCB,
    0xCD, 0xCE, 0xCF,
}


def _jpeg_size(data: bytes) -> tuple:
    index = 2
    total = len(data)
    while index + 3 < total:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker == 0xFF:
            index += 1
            continue
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            index += 2
            continue
        if index + 4 > total:
            break
        segment = struct.unpack_from(">H", data, index + 2)[0]
        if marker in _JPEG_SOF:
            if index + 9 <= total:
                height = struct.unpack_from(">H", data, index + 5)[0]
                width = struct.unpack_from(">H", data, index + 7)[0]
                return int(width), int(height)
            break
        index += 2 + segment
    return 0, 0


def detect_image_format(data: bytes) -> Optional[str]:
    """Identify the real container from magic bytes; ``None`` when unknown."""
    if not data:
        return None
    if data[:8] == PNG_MAGIC:
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:2] == b"BM":
        return "bmp"
    return None


def image_size(path: str) -> tuple:
    """``(width, height)`` for png/jpg/bmp without PIL; ``(0, 0)`` if unknown."""
    data = _read_head(path)
    kind = detect_image_format(data)
    if kind == "png":
        size = _png_size(data)
        if size != (0, 0):
            return size
    elif kind == "jpg":
        return _jpeg_size(data)
    elif kind == "bmp":
        return _bmp_size(data)
    # fall back: try every parser, some exporters pad headers
    for parser in (_png_size, _bmp_size, _jpeg_size):
        size = parser(data)
        if size != (0, 0):
            return size
    return 0, 0


def _rows_to_dict(rows: Any, max_points: int = 2000) -> dict:
    """Shape ``GetOutputData``'s 2-D result into the contract dict.

    ``rows`` is normally a sequence of per-channel sequences; the number of
    points is the length of the first row and rows are decimated to at most
    ``max_points`` samples.  A flat numeric sequence is treated as one row so
    the caller still gets usable data instead of an exception.
    """
    max_points = max(1, int(max_points or 1))
    if rows is None:
        return {"rows": [], "shape": [0, 0], "n_points": 0, "sampled_points": 0}

    if isinstance(rows, (bytes, bytearray)):
        return {"rows": [], "shape": [0, 0], "n_points": 0, "sampled_points": 0}

    normalized: List[List[Any]] = []
    if isinstance(rows, (list, tuple)):
        scalar_only = True
        for item in rows:
            if isinstance(item, (list, tuple)):
                scalar_only = False
                break
        if scalar_only:
            normalized = [[_num(v) for v in rows]]
        else:
            for item in rows:
                if isinstance(item, (list, tuple)):
                    normalized.append([_num(v) for v in item])
                else:
                    normalized.append([_num(item)])
    else:
        normalized = [[_num(rows)]]

    if not normalized:
        return {"rows": [], "shape": [0, 0], "n_points": 0, "sampled_points": 0}

    n_rows = len(normalized)
    # COM safe arrays are rectangular, but a partially filled buffer can hand
    # back ragged rows; report the widest one instead of the first.
    n_cols = max(len(row) for row in normalized)
    n_points = n_cols
    step = max(1, int(math.ceil(n_points / float(max_points)))) if n_points else 1
    sampled = [row[::step] for row in normalized]
    return {
        "rows": sampled,
        "shape": [n_rows, n_cols],
        "n_points": n_points,
        "sampled_points": len(sampled[0]) if sampled else 0,
    }


def _truncate(text: str, limit: int = MAX_LOG_CHARS) -> tuple:
    """Return ``(text, truncated, original_length)``."""
    if text is None:
        return "", False, 0
    total = len(text)
    if total <= limit:
        return text, False, total
    return text[:limit], True, total


# --------------------------------------------------------------------------
# session
# --------------------------------------------------------------------------


class MultisimSession:
    """One hidden Multisim automation session.

    The instance is created cheaply; the COM server is only activated on the
    first call that actually needs it (``connect`` / ``new_circuit`` /
    ``open_circuit`` / any circuit operation).  That keeps worker start-up from
    launching Multisim and keeps ``ping`` free of side effects.
    """

    def __init__(self) -> None:
        self._app: Any = None
        self._circuit: Any = None
        self._com_initialised = False
        self._last_circuit_path: Optional[str] = None

    # -- plumbing ---------------------------------------------------------

    def _ensure_com(self) -> None:
        if self._com_initialised:
            return
        pythoncom, _ = _com()
        try:
            pythoncom.CoInitialize()
        except Exception:
            # already initialised on this thread (or an STA is inherited)
            pass
        self._com_initialised = True

    def _ensure_app(self) -> Any:
        """Activate (or reuse) the Multisim automation object."""
        if self._app is not None:
            return self._app
        self._ensure_com()
        _, win32_client = _com()
        cache_exc: Optional[BaseException] = None
        try:
            app = win32_client.gencache.EnsureDispatch(PROG_ID)
        except AttributeError as exc:
            # A stale/corrupt gen_py cache can make EnsureDispatch fail before
            # COM activation is even attempted (e.g. generated module missing
            # CLSIDToClassMap).  Dynamic dispatch talks to the same registered
            # local server without depending on that cache.
            cache_exc = exc
            try:
                app = win32_client.dynamic.Dispatch(PROG_ID)
            except Exception as dynamic_exc:
                raise MsimError(
                    "NO_MULTISIM",
                    "cannot activate %s (generated wrapper failed: %s; dynamic "
                    "dispatch failed: %s)" % (PROG_ID, exc, dynamic_exc),
                    traceback.format_exc()[-4000:],
                ) from dynamic_exc
        except Exception as exc:
            raise MsimError(
                "NO_MULTISIM",
                "cannot activate %s: %s" % (PROG_ID, clean_error(exc)),
                "%s\n%s" % (cache_exc, traceback.format_exc()[-4000:]),
            ) from exc
        self._app = app
        return app

    def _connect_app(self) -> Any:
        """Return a connected app object, retrying the flaky first Connect()."""
        app = self._ensure_app()
        try:
            if bool(app.IsConnected):
                return app
        except Exception:
            pass
        last: Optional[BaseException] = None
        for attempt in range(CONNECT_ATTEMPTS):
            try:
                app.Connect()
                last = None
                break
            except Exception as exc:
                last = exc
                if attempt + 1 < CONNECT_ATTEMPTS:
                    time.sleep(CONNECT_RETRY_DELAY)
        if last is not None:
            raise MsimError(
                "NO_MULTISIM",
                "app.Connect() failed after %d attempts: %s"
                % (CONNECT_ATTEMPTS, clean_error(last)),
                "E_ACCESSDENIED (-2147024891) means this process is not at "
                "Medium IL -- DSH needs danger-full-access.\n%s"
                % traceback.format_exc()[-4000:],
            ) from last
        try:
            connected = bool(app.IsConnected)
        except Exception:
            connected = True
        if not connected:
            raise MsimError(
                "NO_MULTISIM",
                "app.Connect() returned but IsConnected is still False",
                "the automation server may have refused the session",
            )
        return app

    @property
    def _c(self) -> Any:
        if self._circuit is None:
            raise MsimError(
                "NO_CIRCUIT",
                "no circuit is open; call new_circuit() or open_circuit() first",
            )
        return self._circuit

    def _last_error(self) -> str:
        circuits = []
        if self._circuit is not None:
            circuits.append(self._circuit)
        for circuit in circuits:
            try:
                text = clean_error(circuit.LastErrorMessage)
            except Exception:
                continue
            if text:
                return text
        return ""

    def _guard(self, label: str, func: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a COM call, turning failures into a diagnostic ``MsimError``.

        The Multisim ``LastErrorMessage`` is appended to the message so callers
        see e.g. "输出未找到。" instead of a bare HRESULT.  ``-2147467259``
        ("未指定的错误") on analysis members is the normal answer for circuits
        without probes/instruments.
        """
        try:
            return func(*args, **kwargs)
        except MsimError:
            raise
        except Exception as exc:
            last = self._last_error()
            message = "%s failed: %s" % (label, clean_error(exc))
            if last:
                message += " | LastErrorMessage: %s" % last
            raise MsimError(
                "COM_ERROR", message, traceback.format_exc()[-4000:]
            ) from exc

    def _state(self) -> int:
        try:
            return int(self._c.SimulationState)
        except Exception:
            return STATE_STOPPED

    # -- session ----------------------------------------------------------

    def ping(self) -> dict:
        """Cheap status probe; never activates or connects the server."""
        if self._app is None:
            return {"connected": False, "version": None}
        try:
            connected = bool(self._app.IsConnected)
        except Exception:
            return {"connected": False, "version": None}
        if not connected:
            return {"connected": False, "version": None}
        version: Optional[str] = None
        try:
            version = clean_error(self._app.VersionInfo)
        except Exception:
            version = None
        return {"connected": True, "version": version}

    def connect(self) -> dict:
        app = self._connect_app()
        version = ""
        path = ""
        try:
            version = clean_error(app.VersionInfo)
        except Exception:
            version = ""
        try:
            path = clean_error(app.Path)
        except Exception:
            path = ""
        return {"connected": bool(app.IsConnected), "version": version, "path": path}

    def shutdown(self) -> dict:
        """Disconnect and drop the server.

        This is the **only** method allowed to call ``app.Disconnect()``: it
        closes the hidden Multisim instance we own.
        """
        app = self._app
        self._app = None
        self._circuit = None
        if app is None:
            return {"disconnected": True, "was_connected": False}
        try:
            app.Disconnect()
        except Exception as exc:
            return {
                "disconnected": False,
                "was_connected": True,
                "warning": clean_error(exc),
            }
        return {"disconnected": True, "was_connected": True}

    # -- circuit files ----------------------------------------------------

    def new_circuit(self) -> dict:
        app = self._connect_app()
        self._circuit = self._guard("NewFile", app.NewFile)
        self._last_circuit_path = None
        return self.circuit_info()

    def open_circuit(self, path: str) -> dict:
        target = _as_path(path, "path")
        suffix = _design_suffix(target)
        if not suffix:
            guessed = os.path.splitext(target)[1].lower()
            raise MsimError(
                "BAD_ARGS",
                "open_circuit only supports %s files; got %r"
                % (", ".join(OPENABLE_EXTENSIONS), guessed or "<none>"),
                "OpenFile() crashes the Multisim process on .cir netlists; "
                "route other formats through the GUI (msim_gui.open_file).",
            )
        if not os.path.isfile(target):
            raise MsimError("PATH_MISSING", "file not found: %s" % target)
        app = self._connect_app()
        self._circuit = self._guard("OpenFile", app.OpenFile, target)
        self._last_circuit_path = target
        return self.circuit_info()

    def save(self) -> dict:
        circuit = self._c
        try:
            current = clean_error(circuit.FileName)
        except Exception:
            current = ""
        if not current:
            # Save() on an unnamed circuit corrupts Multisim's heap and takes
            # the whole process down (exit 0xC0000374) -- refuse instead.
            raise MsimError(
                "BAD_ARGS",
                "circuit has no file name yet; call save_as(path) first",
                "Multisim's Save() on a never-saved circuit crashes the "
                "process with a heap corruption (0xC0000374).",
            )
        result = self._guard("Save", circuit.Save)
        path = clean_error(result) if result else current
        return {"path": path}

    def save_as(self, path: str) -> dict:
        target = _as_path(path, "path")
        _ensure_parent(target)
        circuit = self._c
        result = self._guard("SaveAs", circuit.SaveAs, target)
        written = clean_error(result) if result else target
        if not os.path.isabs(written):
            written = os.path.abspath(written)
        self._last_circuit_path = written
        info = {"path": written}
        if os.path.isfile(written):
            info["bytes"] = os.path.getsize(written)
        return info

    def circuit_info(self) -> dict:
        circuit = self._c
        state = self._state()
        name = ""
        file_name = ""
        try:
            name = clean_error(circuit.CircuitName)
        except Exception:
            name = ""
        try:
            file_name = clean_error(circuit.FileName)
        except Exception:
            file_name = ""
        return {
            "name": name,
            "file": file_name,
            "state": state,
            "state_name": _state_name(state),
            "last_error": self._last_error(),
        }

    # -- reading the circuit ---------------------------------------------

    def _enum(self, label: str, call: Any, *args: Any) -> dict:
        values = self._guard(label, call, *args)
        items = _clean_list(values)
        return {"items": items, "count": len(items)}

    def enum_components(self, component_type: int = 0) -> dict:
        kind = _as_int(component_type, "component_type")
        return self._enum(
            "EnumComponents", self._c.EnumComponents, kind
        )

    def enum_inputs(self, io_type: int = 0) -> dict:
        kind = _as_int(io_type, "io_type")
        return self._enum("EnumInputs", self._c.EnumInputs, kind)

    def enum_outputs(self, io_type: int = 0) -> dict:
        kind = _as_int(io_type, "io_type")
        return self._enum("EnumOutputs", self._c.EnumOutputs, kind)

    def enum_variants(self) -> dict:
        return self._enum("EnumVariants", self._c.EnumVariants)

    def get_active_variant(self) -> dict:
        value = self._guard("ActiveVariant", lambda: self._c.ActiveVariant)
        return {"variant": clean_error(value)}

    def set_active_variant(self, name: str) -> dict:
        target = _as_str(name, "name")
        circuit = self._c
        self._guard("SetActiveVariant", lambda: setattr(circuit, "ActiveVariant", target))
        return self.get_active_variant()

    def enum_sections(self, component: str) -> dict:
        target = _as_str(component, "component")
        return self._enum("EnumSections", self._c.EnumSections, target)

    def enum_parameters(self, level: int = 1) -> dict:
        depth = _as_int(level, "level")
        return self._enum(
            "EnumCircuitParameters", self._c.EnumCircuitParameters, depth
        )

    def get_parameter(self, name: str) -> dict:
        target = _as_str(name, "name")
        circuit = self._c
        value = self._guard(
            "CircuitParameterValue",
            lambda: circuit.CircuitParameterValue(target),
        )
        return {"name": target, "value": _as_float(clean_error(value), "value")}

    def set_parameter(self, name: str, value: float) -> dict:
        target = _as_str(name, "name")
        numeric = _as_float(value, "value")
        circuit = self._c
        setter = getattr(circuit, "SetCircuitParameterValue", None)
        if callable(setter):
            self._guard(
                "SetCircuitParameterValue", setter, target, numeric
            )
        else:  # property-only binding
            self._guard(
                "SetCircuitParameterValue",
                lambda: circuit.__setattr__("CircuitParameterValue", numeric),
            )
        return {"name": target, "value": numeric}

    def get_rlc(self, component: str) -> dict:
        target = _as_str(component, "component")
        circuit = self._c
        value = self._guard("RLCValue", lambda: circuit.RLCValue(target))
        return {"component": target, "value": _as_float(value, "value")}

    def set_rlc(self, component: str, value: float) -> dict:
        target = _as_str(component, "component")
        numeric = _as_float(value, "value")
        circuit = self._c
        setter = getattr(circuit, "SetRLCValue", None)
        if callable(setter):
            self._guard("SetRLCValue", setter, target, numeric)
        else:  # pragma: no cover - early binding always exposes the setter
            raise MsimError(
                "COM_ERROR",
                "the COM binding exposes no SetRLCValue; cannot write RLC values",
                "expected IMultisimCircuit.SetRLCValue(componentName, value)",
            )
        return {"component": target, "value": numeric}

    def replace_component(
        self,
        component: str,
        section: str,
        database: Any,
        group: str,
        family: str,
        name: str,
        model: str,
    ) -> dict:
        target = _as_str(component, "component")
        section_name = "" if section is None else str(section)
        if isinstance(database, str) and database.strip().lower() in DB_NAMES:
            database_id = DB_NAMES[database.strip().lower()]
        else:
            database_id = _as_int(database, "database")
        if database_id not in (DB_MASTER, DB_CORPORATE, DB_USER):
            raise MsimError(
                "BAD_ARGS",
                "database must be 0 (Master), 1 (Corporate) or 2 (User)",
                repr(database),
            )
        circuit = self._c
        if not REPLACE_COMPONENT_ALLOWED:
            raise MsimError(
                "COM_ERROR",
                "replace_component is refused on this Multisim build: "
                "IMultisimCircuit.ReplaceComponent (DISPID 29) hard-crashes "
                "the automation server with a heap corruption "
                "(exit 0xC0000374) for every argument combination that can be "
                "expressed through its type library, which would take the "
                "whole worker process down.  Set the environment variable "
                "MSIM_ALLOW_REPLACE_COMPONENT=1 to attempt the call anyway.",
                "verified live on Multisim 14.3: 7-argument early binding, "
                "7-argument late binding and 7-argument raw IDispatch all kill "
                "multisim.exe; supplying the declared 8th [out] parameter "
                "yields DISP_E_BADPARAMCOUNT (-2147352562)",
            )
        result = self._guard(
            "ReplaceComponent",
            circuit.ReplaceComponent,
            target,
            section_name,
            database_id,
            str(group or ""),
            str(family or ""),
            str(name or ""),
            str(model or ""),
        )
        return {
            "component": target,
            "section": section_name,
            "database": database_id,
            "group": str(group or ""),
            "family": str(family or ""),
            "name": str(name or ""),
            "model": str(model or ""),
            "result": clean_error(result),
        }

    # -- exports ----------------------------------------------------------

    def _locate_output(self, requested: str, returned: Any) -> Optional[str]:
        """Find the file the exporter actually wrote.

        Multisim sometimes replaces the extension (``GetCircuitImage``) and
        sometimes appends one (``CreateSnippet``), and the returned string can
        be empty, so several candidates are probed in order.
        """
        candidates: List[str] = []
        for cand in (returned, requested):
            if not cand:
                continue
            if isinstance(cand, str):
                text = cand.strip()
            else:
                text = str(cand).strip()
            if not text:
                continue
            candidates.append(os.path.abspath(text))
        base = os.path.splitext(os.path.abspath(requested))[0]
        absolute = os.path.abspath(requested)
        for ext in (".png", ".jpg", ".jpeg", ".bmp", ".txt", ".csv", ".log"):
            # GetCircuitImage *replaces* the extension, CreateSnippet and
            # SaveDesignAsSnippet *append* one -- probe both forms.
            candidates.append(base + ext)
            candidates.append(absolute + ext)
        seen = set()
        for cand in candidates:
            if cand in seen:
                continue
            seen.add(cand)
            if os.path.isfile(cand):
                return cand
        return None

    def _finalize_image(self, requested: str, written: str) -> dict:
        """Verify the real format from magic bytes and fix the file name."""
        data = _read_head(written)
        actual = detect_image_format(data)
        size = os.path.getsize(written)
        result: dict = {
            "path": written,
            "bytes": size,
            "format": actual or "",
            "width": 0,
            "height": 0,
            "renamed": False,
        }
        if actual:
            width, height = image_size(written)
            result["width"] = int(width)
            result["height"] = int(height)
            final_path = os.path.splitext(os.path.abspath(requested))[0] + "." + actual
            if os.path.normcase(final_path) != os.path.normcase(written):
                try:
                    os.replace(written, final_path)
                    result["path"] = final_path
                    result["renamed"] = True
                    result["source"] = written
                except OSError:
                    # keep the file under the name Multisim chose
                    result["path"] = written
            else:
                result["path"] = written
        return result

    def get_image(self, path: str, image_format: str = "png") -> dict:
        requested = _as_path(path, "path")
        key = _as_str(image_format, "image_format").lower()
        if key not in IMAGE_FORMATS:
            raise MsimError(
                "BAD_ARGS",
                "image_format must be one of png, jpg, bmp",
                repr(image_format),
            )
        fmt = IMAGE_FORMATS[key]
        _ensure_parent(requested)
        circuit = self._c
        returned = self._guard(
            "GetCircuitImage", circuit.GetCircuitImage, fmt, requested
        )
        written = self._locate_output(requested, returned)
        if written is None:
            raise MsimError(
                "COM_ERROR",
                "GetCircuitImage reported success but wrote no file",
                "requested=%s returned=%r" % (requested, returned),
            )
        result = self._finalize_image(requested, written)
        result["requested_format"] = IMAGE_EXTENSIONS[fmt]
        return result

    def _write_report(
        self, label: str, call: Any, path: str, args: tuple
    ) -> dict:
        requested = _as_path(path, "path")
        _ensure_parent(requested)
        returned = self._guard(label, call, *args, requested)
        written = self._locate_output(requested, returned)
        if written is None:
            raise MsimError(
                "COM_ERROR",
                "%s reported success but wrote no file" % label,
                "requested=%s returned=%r" % (requested, returned),
            )
        size = os.path.getsize(written)
        text = _read_text_file(written)
        # Multisim encodes CJK names with its internal "_ucXXXX" scheme; the
        # text is handed to the caller as-is (decoding it is out of scope).
        body, truncated, total = _truncate(text, MAX_REPORT_CHARS)
        return {
            "path": written,
            "bytes": size,
            "text": body,
            "chars": total,
            "truncated": truncated,
        }

    def report_netlist(
        self, path: str, probes: bool = False, fmt: str = "text"
    ) -> dict:
        fmt_key = _as_str(fmt, "fmt").lower()
        if fmt_key not in EXPORT_FORMATS:
            raise MsimError(
                "BAD_ARGS", "fmt must be 'text' or 'csv'", repr(fmt)
            )
        circuit = self._c
        result = self._write_report(
            "ReportNetlist",
            circuit.ReportNetlist,
            path,
            (_as_bool(probes), EXPORT_FORMATS[fmt_key]),
        )
        result["probes"] = _as_bool(probes)
        result["format"] = fmt_key
        return result

    def report_bom(self, path: str, real: bool = False, fmt: str = "text") -> dict:
        fmt_key = _as_str(fmt, "fmt").lower()
        if fmt_key not in EXPORT_FORMATS:
            raise MsimError(
                "BAD_ARGS", "fmt must be 'text' or 'csv'", repr(fmt)
            )
        circuit = self._c
        result = self._write_report(
            "ReportBOM",
            circuit.ReportBOM,
            path,
            (_as_bool(real), EXPORT_FORMATS[fmt_key]),
        )
        result["real"] = _as_bool(real)
        result["format"] = fmt_key
        return result

    def snippet(
        self, path: str, selection_only: bool = False, zoom: float = 1.0
    ) -> dict:
        requested = _as_path(path, "path")
        _ensure_parent(requested)
        factor = _as_float(zoom, "zoom")
        if factor <= 0:
            raise MsimError("BAD_ARGS", "zoom must be greater than zero", repr(zoom))
        circuit = self._c
        returned = self._guard(
            "CreateSnippet",
            circuit.CreateSnippet,
            requested,
            _as_bool(selection_only),
            factor,
        )
        written = self._locate_output(requested, returned)
        if written is None:
            raise MsimError(
                "COM_ERROR",
                "CreateSnippet reported success but wrote no file",
                "requested=%s returned=%r" % (requested, returned),
            )
        result: dict = {
            "path": written,
            "bytes": os.path.getsize(written),
            "selection_only": _as_bool(selection_only),
            "zoom": factor,
        }
        data = _read_head(written)
        kind = detect_image_format(data)
        if kind:
            width, height = image_size(written)
            result["format"] = kind
            result["width"] = int(width)
            result["height"] = int(height)
        return result

    # -- simulation control ----------------------------------------------

    def sim_state(self) -> dict:
        # touches self._c first so "no circuit" is reported as NO_CIRCUIT
        # instead of a misleading "Stopped".
        self._c
        state = self._state()
        return {"state": state, "state_name": _state_name(state)}

    def stop(self) -> dict:
        circuit = self._c
        state = self._state()
        try:
            circuit.StopSimulation()
        except Exception as exc:
            # Expected when nothing is running: (-2147467259, 未指定的错误).
            return {
                "stopped": False,
                "state": self._state(),
                "state_name": _state_name(self._state()),
                "warning": clean_error(exc),
            }
        return {
            "stopped": True,
            "state": self._state(),
            "state_name": _state_name(self._state()),
        }

    def pause(self) -> dict:
        """Never raises: the contract requires a dict even when it fails."""
        try:
            circuit = self._c
        except MsimError as exc:
            return {"paused": False, "state": STATE_STOPPED,
                    "state_name": _state_name(STATE_STOPPED),
                    "warning": exc.message}
        try:
            circuit.PauseSimulation()
        except Exception as exc:
            return {
                "paused": False,
                "state": self._state(),
                "state_name": _state_name(self._state()),
                "warning": clean_error(exc),
            }
        return {
            "paused": True,
            "state": self._state(),
            "state_name": _state_name(self._state()),
        }

    def resume(self) -> dict:
        try:
            circuit = self._c
        except MsimError as exc:
            return {"resumed": False, "state": STATE_STOPPED,
                    "state_name": _state_name(STATE_STOPPED),
                    "warning": exc.message}
        try:
            circuit.ResumeSimulation()
        except Exception as exc:
            return {
                "resumed": False,
                "state": self._state(),
                "state_name": _state_name(self._state()),
                "warning": clean_error(exc),
            }
        return {
            "resumed": True,
            "state": self._state(),
            "state_name": _state_name(self._state()),
        }

    def set_output_request(
        self,
        output: str,
        method: int = 0,
        sample_rate: float = 1e6,
        num_samples: int = 1000,
        repeat: bool = False,
    ) -> dict:
        name = _as_str(output, "output")
        interp = _as_int(method, "method")
        rate = _as_float(sample_rate, "sample_rate")
        if rate <= 0:
            raise MsimError(
                "BAD_ARGS", "sample_rate must be greater than zero", repr(sample_rate)
            )
        samples = _as_int(num_samples, "num_samples")
        if samples <= 0:
            raise MsimError(
                "BAD_ARGS", "num_samples must be greater than zero", repr(num_samples)
            )
        circuit = self._c
        self._guard(
            "SetOutputRequest",
            circuit.SetOutputRequest,
            name,
            interp,
            rate,
            samples,
            _as_bool(repeat),
        )
        return {
            "output": name,
            "method": interp,
            "sample_rate": rate,
            "num_samples": samples,
            "repeat": _as_bool(repeat),
            "requested": True,
        }

    def clear_output_request(self, output: str) -> dict:
        name = _as_str(output, "output")
        circuit = self._c
        try:
            circuit.ClearOutputRequest(name)
        except Exception as exc:
            return {
                "output": name,
                "cleared": False,
                "warning": clean_error(exc),
            }
        return {"output": name, "cleared": True}

    def output_ready(self, output: str) -> dict:
        name = _as_str(output, "output")
        circuit = self._c
        ready = self._guard("OutputReady", circuit.OutputReady, name)
        return {"output": name, "ready": bool(ready)}

    def get_output_data(self, output: str, max_points: int = 2000) -> dict:
        name = _as_str(output, "output")
        return self._read_output(name, max_points)

    def _read_output(self, name: str, max_points: int) -> dict:
        circuit = self._c
        pythoncom, _ = _com()
        value = self._guard(
            "GetOutputData",
            circuit.GetOutputData,
            name,
            pythoncom.Missing,
            pythoncom.Missing,
        )
        rows: Any = value
        method: Any = None
        if isinstance(value, tuple):
            if len(value) == 2:
                rows, method = value
            elif len(value) == 1:
                rows = value[0]
        result = _rows_to_dict(rows, max_points)
        try:
            method_value: Any = int(method)
        except Exception:
            method_value = None
        result["method"] = method_value
        result["output"] = name
        return result

    # -- analyses ---------------------------------------------------------

    def _require_outputs(self, outputs: Any) -> List[str]:
        if outputs is None:
            raise MsimError("BAD_ARGS", "outputs is required")
        if isinstance(outputs, str):
            names = [outputs.strip()] if outputs.strip() else []
        else:
            try:
                raw = list(outputs)
            except TypeError as exc:
                raise MsimError(
                    "BAD_ARGS", "outputs must be a list of strings", str(exc)
                ) from exc
            names = []
            for item in raw:
                text = _as_str(item, "outputs[]")
                names.append(text)
        if not names:
            raise MsimError(
                "BAD_ARGS",
                "outputs must contain at least one output name",
                "Multisim needs a VT_ARRAY|VT_BSTR of probe/output names; an "
                "empty array is rejected with 'Invalid argument for parameter "
                "1 -> expected an array with 1 dimensions of type: string, "
                "instead received an array of type: Empty'",
            )
        # keep order, drop duplicates
        return list(dict.fromkeys(names))

    def _wait_outputs(self, names: Sequence[str], timeout: float) -> tuple:
        """Poll ``OutputReady`` until all names are ready or the timeout hits.

        Returns ``(ready, pending, poll_error)``.  ``poll_error`` is a message
        when ``OutputReady`` failed on *every* poll for at least
        ``POLL_ERROR_GRACE`` seconds -- the analysers turn that into the usual
        ``timed_out`` dict rather than an exception, because a circuit with no
        matching probe reports exactly this way and §4.1 promises a uniform
        result dict for every ``run_*`` call.
        """
        circuit = self._c
        pending = list(names)
        ready: List[str] = []
        deadline = time.monotonic() + max(0.0, timeout)
        silent_since: Optional[float] = None
        poll_error = ""
        while pending and time.monotonic() < deadline:
            answered = False
            for name in list(pending):
                try:
                    is_ready = bool(circuit.OutputReady(name))
                    answered = True
                except Exception:
                    continue
                if is_ready:
                    ready.append(name)
                    pending.remove(name)
            now = time.monotonic()
            if answered:
                silent_since = None
            else:
                if silent_since is None:
                    silent_since = now
                elif now - silent_since >= POLL_ERROR_GRACE:
                    # OutputReady raises on every poll: no probe exists for
                    # these outputs.  Do not burn the whole timeout.
                    poll_error = "OutputReady is unavailable for %s: %s" % (
                        ", ".join(pending),
                        self._last_error() or "COM error on every poll",
                    )
                    break
            if pending:
                time.sleep(POLL_INTERVAL)
        return ready, pending, poll_error

    def _stop_quiet(self) -> tuple:
        try:
            self._c.StopSimulation()
            return True, ""
        except Exception as exc:
            return False, clean_error(exc)

    def _collect(
        self, analysis: str, names: Sequence[str], timeout: float, max_points: int
    ) -> dict:
        started = time.monotonic()
        ready, pending, poll_error = self._wait_outputs(names, timeout)
        result: dict = {
            "analysis": analysis,
            "outputs": {},
            "elapsed": 0.0,
            "state": self._state(),
            "timed_out": False,
            "missing_outputs": [],
        }
        if pending:
            stop_ok, warning = self._stop_quiet()
            result["elapsed"] = round(time.monotonic() - started, 3)
            result["state"] = self._state()
            result["timed_out"] = True
            result["missing_outputs"] = list(pending)
            result["stop_succeeded"] = stop_ok
            if warning:
                result["stop_warning"] = warning
            if poll_error:
                result["poll_error"] = poll_error
            result["last_error"] = self._last_error()
            return result
        for name in names:
            try:
                result["outputs"][name] = self._read_output(name, max_points)
            except MsimError as exc:
                result["outputs"][name] = {
                    "output": name,
                    "method": None,
                    "shape": [0, 0],
                    "n_points": 0,
                    "sampled_points": 0,
                    "rows": [],
                    "error": exc.message,
                }
        result["elapsed"] = round(time.monotonic() - started, 3)
        result["state"] = self._state()
        return result

    def run_transient(
        self,
        outputs: Any,
        duration: float,
        sample_rate: float = 1e6,
        num_samples: int = 1000,
        repeat: bool = False,
        timeout: float = 60.0,
        max_points: int = 2000,
    ) -> dict:
        names = self._require_outputs(outputs)
        seconds = _as_float(duration, "duration")
        if seconds <= 0:
            raise MsimError(
                "BAD_ARGS", "duration must be greater than zero", repr(duration)
            )
        rate = _as_float(sample_rate, "sample_rate")
        if rate <= 0:
            raise MsimError(
                "BAD_ARGS", "sample_rate must be greater than zero", repr(sample_rate)
            )
        samples = _as_int(num_samples, "num_samples")
        if samples <= 0:
            raise MsimError(
                "BAD_ARGS", "num_samples must be greater than zero", repr(num_samples)
            )
        circuit = self._c
        for name in names:
            try:
                circuit.ClearOutputRequest(name)
            except Exception:
                pass
        try:
            circuit.StopSimulation()
        except Exception:
            pass
        for name in names:
            self._guard(
                "SetOutputRequest",
                circuit.SetOutputRequest,
                name,
                INTERP_RAW,
                rate,
                samples,
                _as_bool(repeat),
            )
        self._guard("RunSimulation", circuit.RunSimulation, seconds, False)
        return self._collect("transient", names, timeout, max_points)

    def run_dc(
        self, outputs: Any, timeout: float = 60.0, max_points: int = 500
    ) -> dict:
        names = self._require_outputs(outputs)
        circuit = self._c
        self._guard(
            "DoDCOperatingPoint", circuit.DoDCOperatingPoint, bstr_array(names)
        )
        return self._collect("dc", names, timeout, max_points)

    def run_ac_sweep(
        self,
        outputs: Any,
        sweep_type: int = 0,
        num_points: int = 10,
        start: float = 100.0,
        stop: float = 1e6,
        timeout: float = 120.0,
        max_points: int = 2000,
    ) -> dict:
        names = self._require_outputs(outputs)
        kind = _as_int(sweep_type, "sweep_type")
        if kind not in SWEEP_NAMES:
            raise MsimError(
                "BAD_ARGS",
                "sweep_type must be 0 (Decade), 1 (Octave) or 2 (Linear)",
                repr(sweep_type),
            )
        points = _as_int(num_points, "num_points")
        if points <= 0:
            raise MsimError(
                "BAD_ARGS", "num_points must be greater than zero", repr(num_points)
            )
        low = _as_float(start, "start")
        high = _as_float(stop, "stop")
        if low <= 0 or high <= 0:
            raise MsimError(
                "BAD_ARGS", "start and stop must be greater than zero"
            )
        if high <= low:
            raise MsimError("BAD_ARGS", "stop must be greater than start")
        circuit = self._c
        self._guard(
            "DoACSweep",
            circuit.DoACSweep,
            kind,
            points,
            low,
            high,
            bstr_array(names),
        )
        return self._collect("ac_sweep", names, timeout, max_points)

    def run_ac_single(
        self,
        outputs: Any,
        frequency: float = 1000.0,
        timeout: float = 60.0,
        max_points: int = 200,
    ) -> dict:
        names = self._require_outputs(outputs)
        hertz = _as_float(frequency, "frequency")
        if hertz <= 0:
            raise MsimError(
                "BAD_ARGS", "frequency must be greater than zero", repr(frequency)
            )
        circuit = self._c
        self._guard(
            "DoACSingleFrequency",
            circuit.DoACSingleFrequency,
            hertz,
            bstr_array(names),
        )
        return self._collect("ac_single", names, timeout, max_points)

    # -- simulation inputs -----------------------------------------------

    def set_input_raw(
        self, name: str, values: Any, repeat: bool = False
    ) -> dict:
        """Load an XSPICE input as (time, value) pairs.

        ``values`` accepts either a flat interleaved list
        ``[t0, v0, t1, v1, ...]`` or a list of pairs ``[[t0, v0], [t1, v1]]``.
        Multisim requires a **2-D** array here; a 1-D array is rejected with
        "expected an array with 2 dimensions ... instead received an array with
        1 dimensions".
        """
        target = _as_str(name, "name")
        if not isinstance(values, (list, tuple)):
            raise MsimError("BAD_ARGS", "values must be a list of numbers")
        if not values:
            raise MsimError("BAD_ARGS", "values must not be empty")
        pairs: List[List[float]] = []
        first = values[0]
        if isinstance(first, (list, tuple)):
            for row in values:
                if not isinstance(row, (list, tuple)) or len(row) != 2:
                    raise MsimError(
                        "BAD_ARGS",
                        "values must be a flat [t, v, ...] list or a list of [t, v] pairs",
                        repr(row)[:200],
                    )
                pairs.append([_as_float(row[0], "values[]"), _as_float(row[1], "values[]")])
        else:
            if len(values) % 2 != 0:
                raise MsimError(
                    "BAD_ARGS",
                    "flat values must have an even length (time/value pairs)",
                    "got %d numbers" % len(values),
                )
            for index in range(0, len(values), 2):
                pairs.append(
                    [
                        _as_float(values[index], "values[]"),
                        _as_float(values[index + 1], "values[]"),
                    ]
                )
        circuit = self._c
        self._guard("ReserveInput", circuit.ReserveInput, target)
        self._guard(
            "SetInputDataRaw",
            circuit.SetInputDataRaw,
            target,
            r8_matrix(pairs),
            _as_bool(repeat),
        )
        return {
            "input": target,
            "samples": len(pairs),
            "repeat": _as_bool(repeat),
            "dimensions": 2,
        }

    def set_input_sampled(
        self, name: str, sample_rate: float, values: Any, repeat: bool = False
    ) -> dict:
        target = _as_str(name, "name")
        rate = _as_float(sample_rate, "sample_rate")
        if rate <= 0:
            raise MsimError(
                "BAD_ARGS", "sample_rate must be greater than zero", repr(sample_rate)
            )
        if not isinstance(values, (list, tuple)):
            raise MsimError("BAD_ARGS", "values must be a list of numbers")
        if not values:
            raise MsimError("BAD_ARGS", "values must not be empty")
        numbers = [_as_float(value, "values[]") for value in values]
        circuit = self._c
        self._guard("ReserveInput", circuit.ReserveInput, target)
        self._guard(
            "SetInputDataSampled",
            circuit.SetInputDataSampled,
            target,
            rate,
            r8_array(numbers),
            _as_bool(repeat),
        )
        return {
            "input": target,
            "samples": len(numbers),
            "sample_rate": rate,
            "repeat": _as_bool(repeat),
            "dimensions": 1,
        }

    def clear_input(self, name: str) -> dict:
        target = _as_str(name, "name")
        circuit = self._c
        try:
            circuit.ClearInputData(target)
        except Exception as exc:
            return {
                "input": target,
                "cleared": False,
                "warning": clean_error(exc),
            }
        return {"input": target, "cleared": True}

    # -- command files ---------------------------------------------------

    def run_command_file(
        self, command_file: str, log_file: str, timeout: float = 120.0
    ) -> dict:
        commands = _as_path(command_file, "command_file")
        if not os.path.isfile(commands):
            raise MsimError("PATH_MISSING", "file not found: %s" % commands)
        log = _as_path(log_file, "log_file")
        _ensure_parent(log)
        limit = _as_float(timeout, "timeout")
        if limit <= 0:
            raise MsimError(
                "BAD_ARGS", "timeout must be greater than zero", repr(timeout)
            )
        circuit = self._c
        started = time.monotonic()
        self._guard("DoCommandLine", circuit.DoCommandLine, commands, log)
        deadline = started + limit
        state = self._state()
        timed_out = False
        while time.monotonic() < deadline:
            state = self._state()
            if state == STATE_STOPPED:
                break
            time.sleep(0.25)
        else:
            state = self._state()
            if state != STATE_STOPPED:
                timed_out = True
                self._stop_quiet()
                state = self._state()
        if state == STATE_STOPPED and not timed_out:
            # the loop can exit through the break without re-reading state
            state = self._state()
        elapsed = round(time.monotonic() - started, 3)
        exists = os.path.isfile(log)
        text = ""
        truncated = False
        chars = 0
        if exists:
            text, truncated, chars = _truncate(_read_text_file(log), MAX_LOG_CHARS)
        return {
            "state": state,
            "timed_out": timed_out,
            "elapsed": elapsed,
            "log_exists": exists,
            "log": text,
            "log_path": log,
            "command_file": commands,
            "log_chars": chars,
            "log_truncated": truncated,
        }


# --------------------------------------------------------------------------
# self test (never runs on import)
# --------------------------------------------------------------------------

_DEFAULT_IMAGE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "probe", "img", "com_test.png"
)


def _emit(step: str, payload: Any) -> None:
    try:
        line = json.dumps(
            {"step": step, "result": payload}, ensure_ascii=False, default=str
        )
    except Exception:
        line = '{"step": %r, "result": "<unserializable>"}' % step
    print(line)
    sys.stdout.flush()


def _selftest(argv: Optional[List[str]] = None) -> int:
    """connect -> new_circuit -> circuit_info -> enum_components -> PNG -> shutdown."""
    args = list(sys.argv[1:] if argv is None else argv)
    image_path = _DEFAULT_IMAGE
    open_path: Optional[str] = None
    for index, item in enumerate(args):
        if item == "--image" and index + 1 < len(args):
            image_path = args[index + 1]
        elif item == "--open" and index + 1 < len(args):
            open_path = args[index + 1]

    session = MultisimSession()
    failures: List[str] = []

    def step(name: str, func: Any) -> Any:
        try:
            payload = func()
        except MsimError as exc:
            failures.append("%s: %s" % (name, exc.message))
            _emit(name, {"error": exc.to_dict()})
            return None
        except Exception as exc:  # pragma: no cover - visible in the log
            failures.append("%s: %s" % (name, exc))
            _emit(name, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return None
        _emit(name, payload)
        return payload

    print("== msim_com self test (python %s, %d-bit) =="
          % (sys.version.split()[0], 8 * struct.calcsize("P")))
    sys.stdout.flush()

    if step("ping_before", session.ping) is None and failures:
        return 1
    if step("connect", session.connect) is None:
        return 1

    if open_path:
        if step("open_circuit", lambda: session.open_circuit(open_path)) is None:
            return 1
    elif step("new_circuit", session.new_circuit) is None:
        return 1

    step("circuit_info", session.circuit_info)
    step("enum_components", session.enum_components)
    step("enum_inputs", session.enum_inputs)
    step("enum_outputs", session.enum_outputs)
    step("enum_variants", session.enum_variants)
    step("get_image", lambda: session.get_image(image_path, "png"))
    step("sim_state", session.sim_state)
    step("ping", session.ping)
    # shutdown() is the single sanctioned place that calls Disconnect().
    step("shutdown", session.shutdown)
    step("ping_after", session.ping)

    if failures:
        print("== FAILED: %s ==" % "; ".join(failures))
        sys.stdout.flush()
        return 1
    print("== OK ==")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
