# -*- coding: utf-8 -*-
"""Win32 GUI layer for the DSH Multisim worker.

Drives the *visible* Multisim 14.3 instance through user32/gdi32/shell32:
window enumeration and focus, Explorer-style file drops (WM_DROPFILES
injection), modal-dialog dismissal, Save-As automation, GDI screenshots with
hand-written PNG encoding (no Pillow), keyboard chords and process
launch/kill.

Contract: ``worker/PROTOCOL.md`` section 4.2.  Every public function returns a
JSON-serialisable ``dict`` and raises ``GuiError(code, message, detail)``.
Nothing is written to stdout (stdout is the worker's crash-diagnostics
channel); diagnostics go to stderr via ``_log``.

Standalone self-test::

    C:\\Users\\30823\\.dsh\\multisim-mcp\\py32\\python.exe msim_gui.py
    ... msim_gui.py --open <file> --save-as <file> --write <dir> --keep
"""

import ctypes
import ctypes.wintypes as wintypes
import os
import struct
import subprocess
import sys
import time
import zlib

import pywintypes
import win32api
import win32con
import win32gui
import win32process

__all__ = [
    "GuiError",
    "MULTISIM_EXE",
    "launch",
    "windows",
    "main_window",
    "open_file",
    "screenshot",
    "focus",
    "close_dialogs",
    "save_as",
    "send_keys",
    "kill",
    "probe",
    "dismiss_database_dialogs",
    "run_analysis",
    "capture_graph",
    "graph_windows",
]

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

MULTISIM_EXE = (
    r"C:\Program Files (x86)\National Instruments\Circuit Design Suite 14.3"
    r"\multisim.exe"
)
DEFAULT_DOC_DIR = os.path.join(
    os.path.expanduser("~"), "Documents", "National Instruments",
    "Circuit Design Suite 14.3",
)
# Multisim names an untitled design "设计1"/"Design1"; when the Save As filename
# does not really take effect, Multisim writes that name into DEFAULT_DOC_DIR and
# overwrites a real file of the same name.  save_as verifies the typed path and
# reports any such side effect instead of claiming success.
_DEFAULT_UNTITLED_NAMES = ("设计1", "design1")

MAIN_CLASS = "Multisim14.3"
DIALOG_CLASS = "#32770"
# Hidden helper windows (all-zero rect) that must never be mistaken for a frame.
HIDDEN_CLASSES = frozenset({"lvframe", "#32770", "tooltips_class32", "gdi+ hook window class"})

# (left, top, width, height) of the window rect holding the drawing paper.
# At 1936x1048 that is (232, 130)-(1618, 800).
CANVAS_FRAC = (0.120, 0.124, 0.836, 0.763)

# "content" crop: bounding box of non-background pixels plus this padding.
CONTENT_PAD = 18

WM_CLOSE = 0x0010
WM_SETTEXT = 0x000C
WM_GETTEXT = 0x000D
WM_COMMAND = 0x0111
WM_DROPFILES = 0x0233
WM_MDIACTIVATE = 0x0222
BM_CLICK = 0x00F5
IDOK = 1
IDCANCEL = 2
IDYES = 6
IDNO = 7
ID_SAVE_AS_EDIT = 1001

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2

SW_RESTORE = 9
SW_MAXIMIZE = 3

SRCCOPY = 0x00CC0020
PW_RENDERFULLCONTENT = 0x0002
DIB_RGB_COLORS = 0

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
# SendInput record type for keyboard input.
INPUT_KEYBOARD = 1
VK_MENU = 0x12
VK_RETURN = 0x0D
VK_DOWN = 0x28

# --------------------------------------------------------------------------
# GUI simulation analysis: the 仿真(S) menu is a BCG popup (GetMenu returns 0
# and posting WM_COMMAND corrupts the process), but a real keyboard walk is
# deterministic.  Measured order: 运行(R) / 暂停(G) / 停止(S) /
# Analyses and simulation(A) / 仪器(I) ... so the analyses entry is four rows
# in, three VK_DOWN presses from the highlighted first row.
# --------------------------------------------------------------------------

ANALYSES_MENU_STEPS = 3
ANALYSES_TITLE = "Analyses and Simulation"
GRAPHER_TITLE = "\u56fe\u793a\u4eea\u89c6\u56fe"

ANALYSIS_NAMES = [
    "interactive", "dc", "ac", "transient", "dc_sweep", "ac_single",
    "parameter_sweep", "noise", "monte_carlo", "fourier", "temperature_sweep",
    "distortion", "sensitivity", "worst_case", "noise_figure", "pole_zero",
    "transfer_function", "trace_width", "batched", "custom",
]

ANALYSIS_ALIASES = {
    "interactive": ("interactive", "\u4ea4\u4e92\u5f0f"),
    "dc": ("\u76f4\u6d41\u5de5\u4f5c\u70b9", "dc operating", "operating point"),
    "ac": ("\u4ea4\u6d41\u5206\u6790", "ac analysis"),
    "transient": ("\u77ac\u6001\u5206\u6790", "transient"),
    "dc_sweep": ("\u76f4\u6d41\u626b\u63cf", "dc sweep"),
    "ac_single": ("\u5355\u9891\u4ea4\u6d41", "single frequency"),
    "parameter_sweep": ("\u53c2\u6570\u626b\u63cf", "parameter sweep"),
    "noise": ("\u566a\u58f0\u5206\u6790", "noise analysis"),
    "monte_carlo": ("\u8499\u7279\u5361\u7f57", "monte carlo"),
    "fourier": ("\u5085\u91cc\u53f6", "fourier"),
    "temperature_sweep": ("\u6e29\u5ea6\u626b\u63cf", "temperature sweep"),
    "distortion": ("\u5931\u771f\u5206\u6790", "distortion"),
    "sensitivity": ("\u7075\u654f\u5ea6", "sensitivity"),
    "worst_case": ("\u6700\u574f\u60c5\u51b5", "worst case"),
    "noise_figure": ("\u566a\u58f0\u56e0\u6570", "noise figure"),
    "pole_zero": ("\u6781-\u96f6", "\u6781\u96f6", "pole-zero", "pole zero"),
    "transfer_function": ("\u4f20\u9012\u51fd\u6570", "transfer function"),
    "trace_width": ("\u5149\u8ff9\u5bbd\u5ea6", "trace width"),
    "batched": ("batched",),
    "custom": ("\u7528\u6237\u81ea\u5b9a\u4e49", "user defined", "custom"),
}

LB_GETCOUNT = 0x018B
LB_GETTEXT = 0x0189
LB_GETTEXTLEN = 0x018A
LB_GETCURSEL = 0x0188
LB_GETITEMHEIGHT = 0x01A1
BM_CLICK = 0x00F5


# The 文件 menu is a BCG popup window (class "Afx:...:800:..."), not an HMENU:
# GetMenu(frame) is None and MN_GETHMENU on the popup returns 0, so no command
# id exists for 另存为.  The only deterministic trigger measured on this build
# is ALT+F followed by WM_KEYDOWN(VK_DOWN) steps and VK_RETURN.  另存为 is the
# 7th entry of the 文件 menu, hence six steps down.
SAVE_AS_MENU_STEPS = 6
MENU_POPUP_MARK = ":800:"

MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04
PROCESS_ALL_ACCESS = 0x1F0FFF
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
SMTO_ABORTIFHUNG = 0x0002

SM_CXSCREEN = 0
SM_CYSCREEN = 1
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

# A pixel belongs to the schematic when it is dark (black outlines, black text)
# or chromatic (the blue wires and labels, the tinted instrument bodies).
# Everything neutral and bright is background: white paper, the ~150 grey grid
# dots, the ~192 empty-workspace grey and the ~128 view border.  Judging by
# darkness alone drops every blue wire, which is most of a schematic.
INK_DARK_MAX = 100
INK_CHROMA_MIN = 16

# Chrome owned by the drawing view itself: a coordinate ruler along the top and
# left edge (numbered/lettered bands, ~23px) and a sunken border on all four
# sides (~3px), measured on a maximised frame whose view was 1688x688.
INK_INSET_TOP = 23
INK_INSET_LEFT = 23
INK_INSET_RIGHT = 3
INK_INSET_BOTTOM = 2

# The area to the right of the last sheet of paper is filled with a flat
# workspace grey.  It is brighter than the ~128 view border and darker than the
# ~240 pane background, and unlike the paper it never carries grid dots, so a
# column that is almost entirely this colour is the end of the drawing area.
# Measured 171 on a maximised 1936x1048 frame; the tolerance keeps it working
# under a different display profile.
WORKSPACE_GREY = 171
WORKSPACE_GREY_TOLERANCE = 6
# A column counts as workspace when this share of its sampled pixels match.
WORKSPACE_GREY_SHARE = 0.5
# Sample step down a column while testing for workspace grey.
WORKSPACE_SAMPLE_STEP = 4
# The sheet carries a soft drop shadow, a run of neutral grey columns darker
# than the workspace immediately left of it.  It is background, not drawing.
SHADOW_MIN = 95
SHADOW_MAX = 170
SHADOW_SPAN = 48

# Instances this module started. ``kill`` targets only these unless the caller
# explicitly widens the scope, so a hand-opened Multisim with unsaved work is
# never terminated by accident.
_LAUNCHED_PIDS = set()


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

class GuiError(Exception):
    """Failure raised by any public function of this module."""

    def __init__(self, code, message, detail=None):
        self.code = code
        self.message = message
        self.detail = detail
        super().__init__("%s: %s" % (code, message))

    def to_dict(self):
        return {"code": self.code, "message": self.message, "detail": self.detail}


def _error(code, message, detail=None):
    return GuiError(code, message, detail)


def _log(message):
    """Diagnostics only; stdout belongs to the worker's JSON protocol."""
    try:
        sys.stderr.write("msim_gui: %s\n" % (message,))
        sys.stderr.flush()
    except Exception:
        pass


# --------------------------------------------------------------------------
# Win32 bindings
# --------------------------------------------------------------------------

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def _make_bmi(width, height):
    """32bpp BI_RGB bottom-up BITMAPINFO (positive height => bottom-up rows)."""
    bmi = _BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = int(width)
    bmi.bmiHeader.biHeight = int(height)
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0
    bmi.bmiHeader.biSizeImage = int(width) * int(height) * 4
    return bmi


_user32.GetWindowDC.argtypes = [wintypes.HWND]
_user32.GetWindowDC.restype = wintypes.HDC
_user32.GetDC.argtypes = [wintypes.HWND]
_user32.GetDC.restype = wintypes.HDC
_user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_user32.ReleaseDC.restype = ctypes.c_int
_user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
_user32.PrintWindow.restype = wintypes.BOOL
_user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
_user32.SetWindowPos.restype = wintypes.BOOL
_user32.PostMessageW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
]
_user32.PostMessageW.restype = wintypes.BOOL
_user32.SendMessageW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
]
_user32.SendMessageW.restype = ctypes.c_ssize_t
_user32.SendMessageTimeoutW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
]
_user32.SendMessageTimeoutW.restype = wintypes.LPARAM
_user32.GetDlgItem.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.GetDlgItem.restype = wintypes.HWND
_user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.GetWindow.restype = wintypes.HWND
_user32.IsWindow.argtypes = [wintypes.HWND]
_user32.IsWindow.restype = wintypes.BOOL
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetWindowRect.restype = wintypes.BOOL
_user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetClientRect.restype = wintypes.BOOL
_user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
_user32.ClientToScreen.restype = wintypes.BOOL
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.SetForegroundWindow.restype = wintypes.BOOL
_user32.BringWindowToTop.argtypes = [wintypes.HWND]
_user32.BringWindowToTop.restype = wintypes.BOOL
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.ShowWindow.restype = wintypes.BOOL
_user32.keybd_event.argtypes = [
    wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t,
]
_user32.keybd_event.restype = None
_user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
_user32.SetCursorPos.restype = wintypes.BOOL
_user32.mouse_event.argtypes = [
    wintypes.DWORD, ctypes.c_int, ctypes.c_int, wintypes.DWORD, ctypes.c_size_t,
]
_user32.mouse_event.restype = None
_user32.GetSystemMetrics.argtypes = [ctypes.c_int]
_user32.GetSystemMetrics.restype = ctypes.c_int
_user32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
_user32.SendInput.restype = wintypes.UINT

# WM_SETTEXT carries a wide-string pointer in LPARAM; a dedicated prototype
# keeps ctypes from rejecting the Python str.
_SendMessageTextW = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, ctypes.c_wchar_p,
)(("SendMessageW", _user32))

_gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
_gdi32.CreateCompatibleDC.restype = wintypes.HDC
_gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
_gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
_gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_gdi32.SelectObject.restype = wintypes.HGDIOBJ
_gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi32.DeleteObject.restype = wintypes.BOOL
_gdi32.DeleteDC.argtypes = [wintypes.HDC]
_gdi32.DeleteDC.restype = wintypes.BOOL
_gdi32.BitBlt.argtypes = [
    wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
]
_gdi32.BitBlt.restype = wintypes.BOOL
_gdi32.GetDIBits.argtypes = [
    wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
    ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
]
_gdi32.GetDIBits.restype = ctypes.c_int

_shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
_shell32.DragAcceptFiles.restype = None

_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.VirtualAllocEx.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, wintypes.DWORD,
]
_kernel32.VirtualAllocEx.restype = ctypes.c_void_p
_kernel32.VirtualFreeEx.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD,
]
_kernel32.VirtualFreeEx.restype = wintypes.BOOL
_kernel32.WriteProcessMemory.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
_kernel32.WriteProcessMemory.restype = wintypes.BOOL
_kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
_kernel32.TerminateProcess.restype = wintypes.BOOL
_kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
]
_kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
try:
    _K32EnumProcesses = _kernel32.K32EnumProcesses
except AttributeError:  # pragma: no cover - pre-Vista
    _K32EnumProcesses = ctypes.WinDLL("psapi", use_last_error=True).EnumProcesses
_K32EnumProcesses.argtypes = [
    ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
]
_K32EnumProcesses.restype = wintypes.BOOL

_DPI_STATE = {"mode": None}


def _ensure_dpi():
    """Make window rects and GDI captures use physical pixels (best effort)."""
    if _DPI_STATE["mode"] is not None:
        return _DPI_STATE["mode"]
    _DPI_STATE["mode"] = "none"
    try:
        fn = _user32.SetProcessDpiAwarenessContext
        fn.argtypes = [ctypes.c_void_p]
        fn.restype = wintypes.BOOL
        if fn(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
            _DPI_STATE["mode"] = "per_monitor_v2"
            return _DPI_STATE["mode"]
    except Exception:
        pass
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        if shcore.SetProcessDpiAwareness(2) == 0:
            _DPI_STATE["mode"] = "per_monitor"
            return _DPI_STATE["mode"]
    except Exception:
        pass
    try:
        if _user32.SetProcessDPIAware():
            _DPI_STATE["mode"] = "system"
    except Exception:
        pass
    return _DPI_STATE["mode"]


# --------------------------------------------------------------------------
# Argument helpers (the worker passes raw JSON values through)
# --------------------------------------------------------------------------

def _as_int(value, name, default=None):
    if value is None:
        return default
    if isinstance(value, bool):
        raise _error("BAD_ARGS", "%s must be an integer, got a boolean" % name)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise _error("BAD_ARGS", "%s must be an integer, got %r" % (name, value))


def _as_float(value, name, default=None):
    if value is None:
        return default
    if isinstance(value, bool):
        raise _error("BAD_ARGS", "%s must be a number, got a boolean" % name)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise _error("BAD_ARGS", "%s must be a number, got %r" % (name, value))


def _as_str(value, name, default=None):
    if value is None:
        if default is None:
            raise _error("BAD_ARGS", "%s is required" % name)
        return default
    if not isinstance(value, str):
        raise _error("BAD_ARGS", "%s must be a string, got %r" % (name, value))
    return value


def _as_bool(value, name, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    raise _error("BAD_ARGS", "%s must be a boolean, got %r" % (name, value))


# --------------------------------------------------------------------------
# Window helpers
# --------------------------------------------------------------------------

def _class_name(hwnd):
    try:
        return win32gui.GetClassName(hwnd) or ""
    except pywintypes.error:
        return ""


def _window_text(hwnd):
    try:
        return win32gui.GetWindowText(hwnd) or ""
    except pywintypes.error:
        return ""


def _window_rect(hwnd):
    try:
        return tuple(int(v) for v in win32gui.GetWindowRect(hwnd))
    except pywintypes.error:
        return (0, 0, 0, 0)


def _client_rect(hwnd):
    rect = wintypes.RECT()
    if not _user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return (0, 0, 0, 0)
    return (0, 0, int(rect.right), int(rect.bottom))


def _is_visible(hwnd):
    try:
        return bool(win32gui.IsWindow(hwnd)) and bool(win32gui.IsWindowVisible(hwnd))
    except pywintypes.error:
        return False


def _is_iconic(hwnd):
    try:
        return bool(win32gui.IsIconic(hwnd))
    except pywintypes.error:
        return False


def _pid_of(hwnd):
    try:
        return int(win32process.GetWindowThreadProcessId(hwnd)[1])
    except pywintypes.error:
        return 0


def _rect_size(rect):
    return int(rect[2] - rect[0]), int(rect[3] - rect[1])


def _window_dict(hwnd):
    rect = _window_rect(hwnd)
    width, height = _rect_size(rect)
    return {
        "hwnd": int(hwnd),
        "class": _class_name(hwnd),
        "title": _window_text(hwnd),
        "rect": [int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3])],
        "visible": _is_visible(hwnd),
        "pid": _pid_of(hwnd),
        "w": int(width),
        "h": int(height),
    }


def _top_level_windows():
    found = []

    def _callback(hwnd, _param):
        found.append(int(hwnd))
        return True

    win32gui.EnumWindows(_callback, None)
    return found


def _iter_children(hwnd):
    """Yield every descendant HWND (depth first, nested containers included)."""
    stack = [int(hwnd)]
    while stack:
        parent = stack.pop()
        child = _user32.GetWindow(parent, win32con.GW_CHILD)
        while child:
            child = int(child)
            yield child
            stack.append(child)
            child = _user32.GetWindow(child, win32con.GW_HWNDNEXT)


def _pids_by_image(image_name="multisim.exe"):
    """PIDs of running processes whose executable basename matches."""
    result = []
    array = (wintypes.DWORD * 4096)()
    needed = wintypes.DWORD()
    if not _K32EnumProcesses(array, ctypes.sizeof(array), ctypes.byref(needed)):
        return result
    count = needed.value // ctypes.sizeof(wintypes.DWORD)
    target = image_name.lower()
    for index in range(count):
        pid = int(array[index])
        if pid <= 0:
            continue
        handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            continue
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buffer))
            if _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                if os.path.basename(buffer.value).lower() == target:
                    result.append(pid)
        finally:
            _kernel32.CloseHandle(handle)
    return result


def _find_main_window():
    """The visible ``Multisim14.3`` frame with the largest area (>400x300).

    The all-zero-rect ``LVFrame`` helper (title ``multisim``) is excluded by the
    class test; as a fallback for renamed skins a large window owned by a
    ``multisim.exe`` process is accepted.
    """
    pids = set(_pids_by_image())
    best = None
    best_area = 0
    fallback = None
    fallback_area = 0

    for hwnd in _top_level_windows():
        if not _is_visible(hwnd):
            continue
        rect = _window_rect(hwnd)
        width, height = _rect_size(rect)
        if width <= 0 or height <= 0:
            continue
        cls = _class_name(hwnd)
        if cls == MAIN_CLASS:
            if width > 400 and height > 300:
                area = width * height
                if area > best_area:
                    best, best_area = hwnd, area
            continue
        if pids and _pid_of(hwnd) in pids:
            if cls.lower() in HIDDEN_CLASSES:
                continue
            if width > 400 and height > 300:
                area = width * height
                if area > fallback_area:
                    fallback, fallback_area = hwnd, area

    return best if best is not None else fallback


def _wait_main_window(wait_seconds):
    deadline = time.time() + max(0.0, float(wait_seconds or 0.0))
    while True:
        hwnd = _find_main_window()
        if hwnd:
            return hwnd
        if time.time() >= deadline:
            return None
        time.sleep(0.4)


def _resolve_hwnd(hwnd=0, wait_seconds=0.0):
    hwnd = _as_int(hwnd, "hwnd", 0)
    if hwnd:
        if not _user32.IsWindow(hwnd):
            raise _error("GUI_ERROR", "window handle %d is not a valid window" % hwnd)
        return hwnd
    found = _wait_main_window(wait_seconds)
    if not found:
        raise _error(
            "NO_MULTISIM",
            "Multisim main window not found; start it with launch() or pass hwnd",
            "no visible %s window owned by multisim.exe" % MAIN_CLASS,
        )
    return found


# --------------------------------------------------------------------------
# Keyboard injection
# --------------------------------------------------------------------------

_VK = {
    "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B,
    "tab": 0x09, "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
    "insert": 0x2D, "ins": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "printscreen": 0x2C, "prtsc": 0x2C, "pause": 0x13,
    "capslock": 0x14, "numlock": 0x90, "scrolllock": 0x91,
    "menu": 0x5D, "apps": 0x5D,
}
for _index in range(1, 13):
    _VK["f%d" % _index] = 0x6F + _index
del _index

_MODS = {
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10,
    "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C,
}

_SYMBOLS = {
    "=": 0xBB, "+": 0xBB, "-": 0xBD, "_": 0xBD, ",": 0xBC, "<": 0xBC,
    ".": 0xBE, ">": 0xBE, "/": 0xBF, "?": 0xBF, ";": 0xBA, ":": 0xBA,
    "'": 0xDE, '"': 0xDE, "[": 0xDB, "{": 0xDB, "]": 0xDD, "}": 0xDD,
    "\\": 0xDC, "|": 0xDC, "`": 0xC0, "~": 0xC0,
}

_EXTENDED_VKS = frozenset({
    0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x24, 0x23, 0x21, 0x22,
    0x2C, 0x13, 0x90, 0x5B, 0x5C, 0x5D,
})


def _vk_for(name):
    key = name.strip().lower()
    if key in _VK:
        return _VK[key]
    if key in _SYMBOLS:
        return _SYMBOLS[key]
    if len(key) == 1:
        upper = key.upper()
        if ("A" <= upper <= "Z") or ("0" <= upper <= "9"):
            return ord(upper)
    raise _error(
        "BAD_ARGS",
        "unsupported key %r; use F1-F12, ENTER, ESC, TAB, SPACE, DELETE, "
        "BACKSPACE, arrows, a letter/digit, or ^s / %s / +s style chords" % (name, "ctrl+s"),
    )


def _parse_keys(keys):
    """Parse ``^s``, ``ctrl+shift+s``, ``%{F4}``-less tokens, ``enter``.

    Returns a list of (modifier vk list, main vk, token) chords.  Leading
    AutoIt-style modifiers (``^`` ctrl, ``%`` alt, ``+`` shift) are supported
    as well as ``ctrl+``/``alt+``/``shift+`` prefixes.
    """
    if not isinstance(keys, str):
        raise _error("BAD_ARGS", "keys must be a string")
    tokens = keys.replace("\r", " ").replace("\n", " ").replace("\t", " ").split()
    if not tokens:
        raise _error("BAD_ARGS", "keys is empty")
    chords = []
    for token in tokens:
        text = token
        mods = []
        while text[:1] in ("^", "%", "+"):
            prefix = text[0]
            mods.append({"^": 0x11, "%": 0x12, "+": 0x10}[prefix])
            text = text[1:]
        parts = [part for part in text.split("+") if part != ""]
        if not parts:
            raise _error("BAD_ARGS", "cannot parse key token %r" % token)
        main = parts[-1]
        for part in parts[:-1]:
            mod = part.strip().lower()
            if mod not in _MODS:
                raise _error("BAD_ARGS", "unknown modifier %r in token %r" % (part, token))
            mods.append(_MODS[mod])
        chords.append((mods, _vk_for(main), token))
    return chords


def _key_event(vk, up=False):
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in _EXTENDED_VKS:
        flags |= KEYEVENTF_EXTENDEDKEY
    _user32.keybd_event(int(vk), 0, int(flags), 0)


def _send_chords(chords):
    for mods, vk, _token in chords:
        for mod in mods:
            _key_event(mod, up=False)
            time.sleep(0.015)
        _key_event(vk, up=False)
        time.sleep(0.03)
        _key_event(vk, up=True)
        for mod in reversed(mods):
            _key_event(mod, up=True)
            time.sleep(0.015)
        time.sleep(0.08)


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send_unicode_text(text):
    """Type ``text`` as real Unicode keystrokes.

    ``WM_SETTEXT`` fills the edit box but leaves the dialog's internal file
    state untouched, so Multisim then writes the *previous* name -- in a fresh
    instance that is the untitled ``设计1`` and it silently overwrites
    ``Documents\\National Instruments\\...\\设计1.ms14``.  Real keystrokes are
    the only way to make the dialog commit the requested path.
    """
    sent = 0
    for char in text:
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            record = _INPUT()
            record.type = INPUT_KEYBOARD
            record.u.ki.wVk = 0
            record.u.ki.wScan = ord(char)
            record.u.ki.dwFlags = flags
            if _user32.SendInput(1, ctypes.byref(record), ctypes.sizeof(_INPUT)) == 1:
                sent += 1
            time.sleep(0.004)
    time.sleep(0.15)
    return sent


def _click_window(hwnd):
    """Click the centre of ``hwnd`` so it owns the keyboard focus."""
    rect = _window_rect(hwnd)
    width, height = _rect_size(rect)
    if width <= 0 or height <= 0:
        return False
    point = wintypes.POINT((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
    _user32.SetCursorPos(int(point.x), int(point.y))
    time.sleep(0.2)
    _user32.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.08)
    _user32.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.25)
    return True


def _activate_document(hwnd):
    """Mark an MDI document active without moving the mouse.

    ``WM_MDIACTIVATE`` flags the child as active, but on this MFC build it does
    *not* hand it the keyboard -- :func:`_click_window` is what does that.  Kept
    for diagnostics and for callers that only need the active-child marker.
    """
    documents = _mdi_documents(hwnd)
    if not documents:
        return None
    client = _mdi_client(hwnd)
    document = int(documents[0])
    if client:
        _user32.SendMessageW(
            wintypes.HWND(client), WM_MDIACTIVATE, wintypes.WPARAM(document), 0)
        time.sleep(0.3)
    return document


# --------------------------------------------------------------------------
# Focus
# --------------------------------------------------------------------------

def _focus_window(hwnd):
    """Foreground + maximise a window, defeating the Windows foreground lock.

    ``SetForegroundWindow`` alone is routinely refused (only the foreground
    process may steal focus); a synthetic ALT tap releases the lock, then the
    topmost flash makes the window the active target before the final call.
    """
    if not _user32.IsWindow(hwnd):
        raise _error("GUI_ERROR", "window %d no longer exists" % hwnd)

    _user32.keybd_event(VK_MENU, 0, KEYEVENTF_EXTENDEDKEY, 0)
    time.sleep(0.05)
    _user32.keybd_event(VK_MENU, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
    time.sleep(0.05)

    flags = SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
    _user32.SetWindowPos(wintypes.HWND(hwnd), wintypes.HWND(HWND_TOPMOST), 0, 0, 0, 0, flags)
    time.sleep(0.2)
    _user32.SetWindowPos(wintypes.HWND(hwnd), wintypes.HWND(HWND_NOTOPMOST), 0, 0, 0, 0, flags)
    time.sleep(0.2)

    if _is_iconic(hwnd):
        _user32.ShowWindow(wintypes.HWND(hwnd), SW_RESTORE)
        time.sleep(0.2)
    _user32.BringWindowToTop(wintypes.HWND(hwnd))
    _user32.SetForegroundWindow(wintypes.HWND(hwnd))
    time.sleep(0.25)
    _user32.ShowWindow(wintypes.HWND(hwnd), SW_MAXIMIZE)
    time.sleep(0.35)

    try:
        return int(win32gui.GetForegroundWindow()) == int(hwnd) or \
            int(win32gui.GetForegroundWindow()) == 0
    except pywintypes.error:
        return False


def focus(hwnd=0):
    """Bring the Multisim frame to the foreground and maximise it."""
    _ensure_dpi()
    resolved = _resolve_hwnd(hwnd)
    foreground = _focus_window(resolved)
    if not foreground:
        time.sleep(0.3)
        try:
            foreground = int(win32gui.GetForegroundWindow()) == int(resolved)
        except pywintypes.error:
            foreground = False
    return {"hwnd": int(resolved), "foreground": bool(foreground)}


# --------------------------------------------------------------------------
# Dialogs
# --------------------------------------------------------------------------

def _dialog_dict(hwnd):
    return {
        "hwnd": int(hwnd),
        "title": _window_text(hwnd),
        "class": _class_name(hwnd),
    }


def _multisim_dialogs(visible_only=True):
    """Visible ``#32770`` dialogs owned by a ``multisim.exe`` process."""
    pids = set(_pids_by_image())
    found = []
    for hwnd in _top_level_windows():
        if _class_name(hwnd) != DIALOG_CLASS:
            continue
        if visible_only and not _is_visible(hwnd):
            continue
        if pids and _pid_of(hwnd) not in pids:
            continue
        found.append(hwnd)
    return found


def _dialog_text(hwnd):
    """Title plus every static/button/edit label inside the dialog.

    ``GetWindowText`` returns an empty string for controls owned by another
    process, and Multisim puts its warning text in an ``Edit`` box; those texts
    must be fetched with ``WM_GETTEXT`` or the dialog looks blank.
    """
    parts = [_window_text(hwnd)]
    for child in _iter_children(hwnd):
        text = _window_text(child)
        if not text and _class_name(child).lower() == "edit":
            text = _read_edit_text(child, 2048)
        if text:
            parts.append(text)
    return " ".join(parts)


def _find_child_by_class(dialog, class_name):
    target = class_name.lower()
    for child in _iter_children(dialog):
        if _class_name(child).lower() == target and _is_visible(child):
            return child
    for child in _iter_children(dialog):
        if _class_name(child).lower() == target:
            return child
    return None


def _find_button(dialog, words):
    """First visible button whose label contains one of ``words``."""
    lowered = [word.lower() for word in words]
    for child in _iter_children(dialog):
        if _class_name(child).lower() != "button":
            continue
        if not _is_visible(child):
            continue
        text = _window_text(child).lower()
        if not text:
            continue
        for word in lowered:
            if word in text:
                return child
    return None


_DATABASE_WORDS = ("数据库", "database")


def _is_database_dialog(text):
    """True for the "主数据库/企业数据库 无法访问" startup warnings.

    These are modal, refuse ``WM_CLOSE`` and swallow every later keystroke, so
    anything that sends keys must dismiss them first by clicking their button.
    """
    lowered = (text or "").lower()
    return any(word in lowered for word in _DATABASE_WORDS) and any(
        word in lowered for word in ("无法访问", "cannot be accessed", "不可用"))


def _dismiss_database_dialog(dialog):
    """Click the acknowledgement button of a database warning dialog."""
    title = _window_text(dialog)
    button = _find_button(dialog, ("确认", "确定", "ok", "是", "yes", "关闭", "close"))
    if button:
        _user32.PostMessageW(wintypes.HWND(int(button)), BM_CLICK, 0, 0)
        time.sleep(0.4)
        return title
    _user32.PostMessageW(wintypes.HWND(int(dialog)), WM_CLOSE, 0, 0)
    time.sleep(0.3)
    return title


def dismiss_database_dialogs(limit=8):
    """Click away every database warning dialog currently on screen."""
    dismissed = []
    for _ in range(max(1, int(limit))):
        found = None
        for dialog in _multisim_dialogs():
            if _is_database_dialog(_dialog_text(dialog)):
                found = dialog
                break
        if found is None:
            break
        dismissed.append(_dismiss_database_dialog(found))
    return {"dismissed": dismissed}


def _answer_save_prompt(word_sets=(("否", "no", "don't"), ("取消", "cancel")),
                        timeout=3.0):
    """Click "No"/"Cancel" on an unsaved-changes prompt, else close it.

    Never uses ESC: pressing ESC over a Multisim dialog can quit the whole
    application.
    """
    deadline = time.time() + max(0.2, timeout)
    while time.time() < deadline:
        for dialog in _multisim_dialogs():
            text = _dialog_text(dialog)
            if not any(word in text.lower() for word in
                       ("保存", "save", "是否", "discard", "放弃")):
                continue
            for words in word_sets:
                button = _find_button(dialog, words)
                if button:
                    title = _window_text(dialog)
                    _user32.PostMessageW(wintypes.HWND(button), BM_CLICK, 0, 0)
                    time.sleep(0.3)
                    return title
            # No matching button: fall back to closing the prompt.
            _user32.PostMessageW(wintypes.HWND(dialog), WM_CLOSE, 0, 0)
            time.sleep(0.3)
            return _window_text(dialog)
        time.sleep(0.25)
    return None


def close_dialogs(keep=""):
    """Post ``WM_CLOSE`` to every modal ``#32770`` dialog of Multisim.

    ``keep`` is a substring of a title that must be left alone.  ESC is never
    used: it can terminate the whole Multisim process.
    """
    _ensure_dpi()
    keep_lower = _as_str(keep, "keep", "").lower()
    closed = []
    for dialog in _multisim_dialogs():
        title = _window_text(dialog)
        if keep_lower and keep_lower in title.lower():
            continue
        info = _dialog_dict(dialog)
        _user32.PostMessageW(wintypes.HWND(dialog), WM_CLOSE, 0, 0)
        closed.append(info)
        time.sleep(0.3)
    return {"closed": closed}


# --------------------------------------------------------------------------
# MDI documents
# --------------------------------------------------------------------------

def _mdi_client(hwnd):
    try:
        return int(win32gui.FindWindowEx(hwnd, 0, "MDIClient", None) or 0)
    except pywintypes.error:
        return 0


def _mdi_documents(hwnd):
    client = _mdi_client(hwnd)
    if not client:
        return []
    documents = []
    child = _user32.GetWindow(wintypes.HWND(client), win32con.GW_CHILD)
    while child:
        child = int(child)
        width, height = _rect_size(_window_rect(child))
        if _is_visible(child) and width > 0 and height > 0:
            documents.append(child)
        child = _user32.GetWindow(wintypes.HWND(child), win32con.GW_HWNDNEXT)
    return documents


def _close_documents(hwnd, timeout=12.0):
    """Close every open MDI document, answering "No" to save prompts."""
    closed = []
    deadline = time.time() + max(1.0, timeout)
    while time.time() < deadline:
        documents = _mdi_documents(hwnd)
        if not documents:
            break
        for document in documents:
            info = _dialog_dict(document)
            _user32.PostMessageW(wintypes.HWND(document), WM_CLOSE, 0, 0)
            closed.append(info)
            time.sleep(0.35)
            _answer_save_prompt(timeout=1.2)
        time.sleep(0.3)
    return closed


# --------------------------------------------------------------------------
# Launch / kill / enumerate
# --------------------------------------------------------------------------

def launch(wait_seconds=25.0, maximize=True):
    """Start the visible Multisim instance and wait for its main window.

    ``multisim.exe`` is started directly: the MultisimLauncher.vbs wrapper was
    measured to behave unreliably at Medium integrity, while a plain Popen
    reaches a visible frame.
    """
    _ensure_dpi()
    wait_seconds = _as_float(wait_seconds, "wait_seconds", 25.0)
    maximize = _as_bool(maximize, "maximize", True)
    started = time.time()

    existing = _find_main_window()
    if existing:
        if maximize:
            _focus_window(existing)
        return {
            "pid": int(_pid_of(existing)),
            "hwnd": int(existing),
            "title": _window_text(existing),
            "elapsed": round(time.time() - started, 3),
            "already_running": True,
        }

    if not os.path.isfile(MULTISIM_EXE):
        raise _error(
            "PATH_MISSING",
            "Multisim executable not found: %s" % MULTISIM_EXE,
            "PATH_MISSING",
        )

    try:
        process = subprocess.Popen(
            [MULTISIM_EXE],
            cwd=os.path.dirname(MULTISIM_EXE) or None,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        raise _error("GUI_ERROR", "failed to start Multisim: %s" % (exc,),
                     "Popen(%s)" % MULTISIM_EXE)

    hwnd = _wait_main_window(wait_seconds)
    if not hwnd:
        raise _error(
            "GUI_ERROR",
            "Multisim started (pid %d) but no visible %s frame appeared within %.1fs"
            % (process.pid, MAIN_CLASS, wait_seconds),
            "pids=%s" % (_pids_by_image(),),
        )
    if maximize:
        _focus_window(hwnd)
    _LAUNCHED_PIDS.add(int(process.pid))
    _log("launched pid %d hwnd %d" % (process.pid, hwnd))
    return {
        "pid": int(process.pid),
        "hwnd": int(hwnd),
        "title": _window_text(hwnd),
        "elapsed": round(time.time() - started, 3),
        "already_running": False,
    }


def windows(visible_only=False):
    """Enumerate top-level windows; each rect is ``[left, top, right, bottom]``."""
    _ensure_dpi()
    visible_only = _as_bool(visible_only, "visible_only", False)
    items = []
    for hwnd in _top_level_windows():
        visible = _is_visible(hwnd)
        if visible_only and not visible:
            continue
        rect = _window_rect(hwnd)
        width, height = _rect_size(rect)
        if visible_only and (width <= 0 or height <= 0):
            continue
        items.append({
            "hwnd": int(hwnd),
            "class": _class_name(hwnd),
            "title": _window_text(hwnd),
            "rect": [int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3])],
            "visible": bool(visible),
            "pid": _pid_of(hwnd),
        })
    return {"items": items, "count": len(items)}


def main_window():
    """Resolve the visible Multisim frame: ``{"hwnd", "title", "rect"}``."""
    _ensure_dpi()
    hwnd = _find_main_window()
    if not hwnd:
        raise _error(
            "NO_MULTISIM",
            "Multisim main window not found",
            "no visible %s frame owned by multisim.exe" % MAIN_CLASS,
        )
    rect = _window_rect(hwnd)
    return {
        "hwnd": int(hwnd),
        "title": _window_text(hwnd),
        "rect": [int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3])],
    }


def kill(force=False, pid=0, all_instances=False):
    """Close Multisim instances started by this toolkit.

    Only processes this module started are touched by default: an
    image-name-wide kill would take down a window the user opened by hand and
    silently discard their unsaved design.  ``pid`` names one extra instance to
    close; ``all_instances`` widens the target to every ``multisim.exe`` and is
    destructive for anything unsaved, so it has to be asked for explicitly.
    """
    _ensure_dpi()
    force = _as_bool(force, "force", False)
    all_instances = _as_bool(all_instances, "all_instances", False)
    pid = _as_int(pid, "pid", 0)

    running = _pids_by_image()
    if all_instances:
        targets = list(running)
    else:
        targets = [candidate for candidate in running
                   if candidate in _LAUNCHED_PIDS or (pid and candidate == pid)]
    skipped = len(running) - len(targets)
    killed = []

    if targets and not force:
        hwnd = _find_main_window()
        if hwnd and _pid_of(hwnd) in targets:
            _user32.PostMessageW(wintypes.HWND(hwnd), WM_CLOSE, 0, 0)
            deadline = time.time() + 8.0
            while time.time() < deadline:
                _answer_save_prompt(timeout=0.4)
                if not (set(targets) & set(_pids_by_image())):
                    break
                time.sleep(0.4)
        remaining_targets = [candidate for candidate in targets
                             if candidate in _pids_by_image()]
        killed = [candidate for candidate in targets
                  if candidate not in remaining_targets]
        targets = remaining_targets

    for candidate in targets:
        handle = _kernel32.OpenProcess(PROCESS_TERMINATE, False, int(candidate))
        if not handle:
            continue
        try:
            if _kernel32.TerminateProcess(handle, 1):
                killed.append(int(candidate))
        finally:
            _kernel32.CloseHandle(handle)

    for candidate in killed:
        _LAUNCHED_PIDS.discard(int(candidate))
    remaining = _pids_by_image()

    return {
        "killed": sorted({int(item) for item in killed}),
        "remaining": len(remaining),
        # Instances left alone because this toolkit did not start them.
        "skipped": int(skipped),
    }


def probe():
    """Environment self-check: exe present? any running instance? main window?"""
    _ensure_dpi()
    pids = _pids_by_image()
    hwnd = _find_main_window()
    return {
        "exe": MULTISIM_EXE,
        "exe_exists": bool(os.path.isfile(MULTISIM_EXE)),
        "running_pids": [int(pid) for pid in pids],
        "count": len(pids),
        "main_window": _window_dict(hwnd) if hwnd else None,
        "python_bits": struct.calcsize("P") * 8,
        "python": sys.version.split()[0],
        "dpi_awareness": _DPI_STATE["mode"],
        "screen": [
            int(_user32.GetSystemMetrics(SM_CXSCREEN)),
            int(_user32.GetSystemMetrics(SM_CYSCREEN)),
        ],
    }


# --------------------------------------------------------------------------
# Screenshot: GDI capture
# --------------------------------------------------------------------------

def _sample_is_blank(rows, width, height):
    """True when the capture is a solid colour or almost entirely black.

    PrintWindow on MFC apps such as Multisim frequently yields an all-black
    bitmap; that must trigger the fallback rather than be returned.
    """
    if not rows or width <= 0 or height <= 0:
        return True
    step_x = max(1, width // 32)
    step_y = max(1, height // 32)
    first = bytes(rows[0][0:3])
    distinct = 0
    dark = 0
    total = 0
    seen = set()
    for y in range(0, height, step_y):
        row = rows[y]
        for x in range(0, width, step_x):
            offset = x * 4
            pixel = bytes(row[offset:offset + 3])
            total += 1
            if pixel == first:
                distinct += 1
            if pixel[0] < 8 and pixel[1] < 8 and pixel[2] < 8:
                dark += 1
            if len(seen) < 8:
                seen.add(pixel)
    if total == 0:
        return True
    if distinct == total:
        return True
    if dark >= total * 0.99:
        return True
    return len(seen) <= 1


def _rows_from_bitmap(mem_dc, bitmap, width, height):
    """GetDIBits -> top-down BGRA rows."""
    info = _make_bmi(width, height)
    buffer = ctypes.create_string_buffer(int(width) * int(height) * 4)
    lines = _gdi32.GetDIBits(
        mem_dc, bitmap, 0, int(height), buffer, ctypes.byref(info), DIB_RGB_COLORS
    )
    if lines != int(height):
        raise _error(
            "GUI_ERROR",
            "GetDIBits returned %d of %d scan lines" % (lines, height),
            "GetLastError=%d" % ctypes.get_last_error(),
        )
    raw = buffer.raw
    row_bytes = int(width) * 4
    rows = [raw[offset:offset + row_bytes] for offset in range(0, len(raw), row_bytes)]
    rows.reverse()  # DIB is bottom-up
    return rows


def _grab_with(source_dc, width, height, render):
    """Render into an offscreen bitmap and read it back as BGRA rows."""
    mem_dc = _gdi32.CreateCompatibleDC(source_dc)
    if not mem_dc:
        raise _error("GUI_ERROR", "CreateCompatibleDC failed",
                     "GetLastError=%d" % ctypes.get_last_error())
    bitmap = _gdi32.CreateCompatibleBitmap(source_dc, int(width), int(height))
    if not bitmap:
        _gdi32.DeleteDC(mem_dc)
        raise _error("GUI_ERROR", "CreateCompatibleBitmap failed",
                     "GetLastError=%d" % ctypes.get_last_error())
    old = None
    try:
        old = _gdi32.SelectObject(mem_dc, bitmap)
        if not render(mem_dc):
            return None
        _gdi32.SelectObject(mem_dc, old)
        old = None
        return _rows_from_bitmap(mem_dc, bitmap, width, height)
    finally:
        if old:
            _gdi32.SelectObject(mem_dc, old)
        _gdi32.DeleteObject(bitmap)
        _gdi32.DeleteDC(mem_dc)


def _grab_printwindow(hwnd, width, height, flag):
    source_dc = _user32.GetWindowDC(wintypes.HWND(hwnd))
    if not source_dc:
        return None
    try:
        return _grab_with(
            source_dc, width, height,
            lambda mem: bool(_user32.PrintWindow(wintypes.HWND(hwnd), mem, flag)),
        )
    finally:
        _user32.ReleaseDC(wintypes.HWND(hwnd), source_dc)


def _grab_window_dc(hwnd, width, height):
    source_dc = _user32.GetWindowDC(wintypes.HWND(hwnd))
    if not source_dc:
        return None
    try:
        return _grab_with(
            source_dc, width, height,
            lambda mem: bool(_gdi32.BitBlt(mem, 0, 0, width, height, source_dc, 0, 0, SRCCOPY)),
        )
    finally:
        _user32.ReleaseDC(wintypes.HWND(hwnd), source_dc)


def _grab_screen_region(left, top, width, height):
    """Copy an on-screen rectangle via the screen DC."""
    screen_dc = _user32.GetDC(wintypes.HWND(0))
    if not screen_dc:
        return None
    try:
        return _grab_with(
            screen_dc, width, height,
            lambda mem: bool(_gdi32.BitBlt(mem, 0, 0, width, height, screen_dc,
                                           int(left), int(top), SRCCOPY)),
        )
    finally:
        _user32.ReleaseDC(wintypes.HWND(0), screen_dc)


def _screen_grab_window(hwnd, width, height):
    """Fallback: focus the window, then copy its rectangle off the screen.

    Only the part inside the virtual screen can be read; the rest is filled
    white so the geometry (and therefore CANVAS_FRAC crops) stays valid.
    """
    _focus_window(hwnd)
    time.sleep(0.3)
    rect = _window_rect(hwnd)
    vx = int(_user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
    vy = int(_user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
    vw = int(_user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))
    vh = int(_user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))
    if vw <= 0:
        vw, vh = int(_user32.GetSystemMetrics(SM_CXSCREEN)), int(_user32.GetSystemMetrics(SM_CYSCREEN))
        vx = vy = 0
    left = max(rect[0], vx)
    top = max(rect[1], vy)
    right = min(rect[2], vx + vw)
    bottom = min(rect[3], vy + vh)
    if right <= left or bottom <= top:
        raise _error(
            "GUI_ERROR", "window %d is entirely off-screen" % hwnd,
            "window rect=%s virtual screen=(%d,%d)+%dx%d" % (list(rect), vx, vy, vw, vh),
        )
    grabbed = _grab_screen_region(left, top, right - left, bottom - top)
    if not grabbed:
        raise _error("GUI_ERROR", "screen copy of window %d failed" % hwnd,
                     "GetLastError=%d" % ctypes.get_last_error())
    blank_row = b"\xff\xff\xff\x00" * int(width)
    rows = [blank_row] * int(height)
    offset_x = left - rect[0]
    offset_y = top - rect[1]
    for index, row in enumerate(grabbed):
        y = offset_y + index
        if 0 <= y < height:
            target = bytearray(rows[y])
            target[offset_x * 4:(offset_x + len(row) // 4) * 4] = row
            rows[y] = bytes(target)
    return rows


def _capture(hwnd, area):
    """Capture a window or its client area as (width, height, top-down BGRA rows).

    Attempt order: PrintWindow(PW_RENDERFULLCONTENT) -> PrintWindow(0) ->
    BitBlt from the window DC -> focus + screen copy.  A black/solid result
    from an earlier attempt is rejected, which is what makes the Multisim
    black-PrintWindow problem survivable.
    """
    if _is_iconic(hwnd):
        _user32.ShowWindow(wintypes.HWND(hwnd), SW_RESTORE)
        time.sleep(0.4)

    rect = _window_rect(hwnd)
    width, height = _rect_size(rect)
    if width <= 0 or height <= 0:
        raise _error(
            "GUI_ERROR",
            "window %d has an empty rect (%dx%d); is it minimised?" % (hwnd, width, height),
            "rect=%s" % (list(rect),),
        )

    attempts = (
        ("printwindow-full", lambda: _grab_printwindow(hwnd, width, height, PW_RENDERFULLCONTENT)),
        ("printwindow", lambda: _grab_printwindow(hwnd, width, height, 0)),
        ("bitblt-window-dc", lambda: _grab_window_dc(hwnd, width, height)),
    )
    rows = None
    used = None
    for name, attempt in attempts:
        try:
            candidate = attempt()
        except GuiError:
            candidate = None
        if candidate and not _sample_is_blank(candidate, width, height):
            rows, used = candidate, name
            break
    if rows is None:
        rows = _screen_grab_window(hwnd, width, height)
        used = "screen-copy"

    _log("capture %dx%d via %s" % (width, height, used))

    if area == "client":
        point = wintypes.POINT(0, 0)
        if _user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(point)):
            offset_x = int(point.x - rect[0])
            offset_y = int(point.y - rect[1])
        else:
            offset_x = offset_y = 0
        client_w, client_h = _client_rect(hwnd)[2], _client_rect(hwnd)[3]
        if client_w <= 0 or client_h <= 0:
            client_w = max(1, width - offset_x)
            client_h = max(1, height - offset_y)
        width, height, rows = _crop_rows(
            rows, width, height, offset_x, offset_y, client_w, client_h
        )
    return width, height, rows


# --------------------------------------------------------------------------
# Screenshot: rows maths (crop / content / resize)
# --------------------------------------------------------------------------

def _crop_rows(rows, width, height, left, top, crop_w, crop_h):
    left = max(0, min(int(left), max(0, width - 1)))
    top = max(0, min(int(top), max(0, height - 1)))
    crop_w = max(1, min(int(crop_w), width - left))
    crop_h = max(1, min(int(crop_h), height - top))
    cropped = []
    for y in range(top, top + crop_h):
        start = left * 4
        cropped.append(rows[y][start:start + crop_w * 4])
    return crop_w, crop_h, cropped


def _canvas_crop(width, height, rows):
    frac_left, frac_top, frac_w, frac_h = CANVAS_FRAC
    return _crop_rows(
        rows, width, height,
        round(width * frac_left), round(height * frac_top),
        round(width * frac_w), round(height * frac_h),
    )


def _design_view_rect(hwnd):
    """Screen rect of the schematic drawing view, or None.

    The schematic lives in the MDI document view (``AfxFrameOrView140u``) that
    sits inside the MDI child frame inside ``MDIClient``.  Measuring it beats a
    hard-coded fraction of the frame: the fraction drifts with window size, DPI
    and whether the side panes are shown, and it happily swallows the ruler
    margin, the spreadsheet pane and the status bar.
    """
    client = _mdi_client(hwnd)
    if not client:
        return None
    fallback = None
    child = _user32.GetWindow(wintypes.HWND(client), win32con.GW_CHILD)
    while child:
        child = int(child)
        width, height = _rect_size(_window_rect(child))
        if _is_visible(child) and width > 200 and height > 150:
            view = _user32.GetWindow(wintypes.HWND(child), win32con.GW_CHILD)
            while view:
                view = int(view)
                if _is_visible(view) and _class_name(view) == "AfxFrameOrView140u":
                    vw, vh = _rect_size(_window_rect(view))
                    if vw > 200 and vh > 150:
                        return _window_rect(view)
                view = _user32.GetWindow(wintypes.HWND(view), win32con.GW_HWNDNEXT)
            if fallback is None:
                fallback = _window_rect(child)
        child = _user32.GetWindow(wintypes.HWND(child), win32con.GW_HWNDNEXT)
    return fallback


def _view_crop_box(frame_hwnd, capture_width, capture_height):
    """Crop box (left, top, w, h) of the drawing view inside a frame capture.

    ``_design_view_rect`` reports screen coordinates, so it is converted to
    frame-relative offsets and intersected with the MDI client area.  The view
    rect can overshoot the client area by a few pixels (measured 799 vs 823 for
    a maximised frame), and an overshoot would drag the spreadsheet pane or the
    status bar back into the picture.
    """
    view = _design_view_rect(frame_hwnd)
    if not view:
        return None
    frame = _window_rect(frame_hwnd)
    left = int(view[0] - frame[0])
    top = int(view[1] - frame[1])
    right = int(view[2] - frame[0])
    bottom = int(view[3] - frame[1])
    client = _mdi_client(frame_hwnd)
    if client:
        client_rect = _window_rect(client)
        left = max(left, int(client_rect[0] - frame[0]))
        top = max(top, int(client_rect[1] - frame[1]))
        right = min(right, int(client_rect[2] - frame[0]))
        bottom = min(bottom, int(client_rect[3] - frame[1]))
    left = max(0, left)
    top = max(0, top)
    right = min(int(capture_width), right)
    bottom = min(int(capture_height), bottom)
    if right - left <= 200 or bottom - top <= 150:
        return None
    return left, top, right - left, bottom - top


def _ink_mask(row, x0, x1):
    """Bitmask of schematic-ink pixels in ``row`` between columns x0 and x1.

    A pixel is ink when it is dark (black outlines, black text) or chromatic
    (blue wires and labels, tinted instrument bodies).  Bright neutral pixels
    are background: white paper, the ~150 grid dots, the ~192 workspace grey
    and the ~128 view border.  The old test ANDed three per-channel "bright"
    bits, which classifies a pure blue wire (0,0,255) as background because its
    blue channel is bright - that is how whole schematics came out blank.
    """
    bits = bytearray(b"0") * max(0, x1 - x0)  # ASCII "0"/"1" for int(..., 2)
    index = 0
    for offset in range(x0 * 4, x1 * 4, 4):
        blue = row[offset]
        green = row[offset + 1]
        red = row[offset + 2]
        low = red if red < green else green
        if blue < low:
            low = blue
        if low < INK_DARK_MAX:
            bits[index] = 49  # "1"
        else:
            high = red if red > green else green
            if blue > high:
                high = blue
            if high - low > INK_CHROMA_MIN:
                bits[index] = 49
        index += 1
    return int(bytes(bits), 2) if bits else 0


def _ink_bbox(rows, width, height, bounds=None):
    """Bounding box of ink pixels inside ``bounds`` (x0,y0,x1,y1)."""
    x0, y0, x1, y1 = bounds or (0, 0, width, height)
    x0 = max(0, min(int(x0), width - 1))
    x1 = max(x0 + 1, min(int(x1), width))
    y0 = max(0, min(int(y0), height - 1))
    y1 = max(y0 + 1, min(int(y1), height))
    span = x1 - x0
    min_x, min_y, max_x, max_y = x1, y1, -1, -1
    for y in range(y0, y1):
        mask = _ink_mask(rows[y], x0, x1)
        if not mask:
            continue
        left = span - mask.bit_length() + x0
        right = span - ((mask & -mask).bit_length() - 1) - 1 + x0
        if left < min_x:
            min_x = left
        if right > max_x:
            max_x = right
        if y < min_y:
            min_y = y
        max_y = y
    if max_x < 0 or max_y < 0:
        return None
    return min_x, min_y, max_x + 1, max_y + 1


def _is_workspace_grey(pixel):
    """True for the flat grey that fills the area right of the last sheet."""
    low = WORKSPACE_GREY - WORKSPACE_GREY_TOLERANCE
    high = WORKSPACE_GREY + WORKSPACE_GREY_TOLERANCE
    for channel in pixel:
        if channel < low or channel > high:
            return False
    return True


def _paper_right_edge(rows, width, height):
    """Column index (exclusive) where the drawing area stops, or None.

    Multisim draws the sheet of paper and then fills everything to its right
    with a flat workspace grey.  That fill is part of the view, not chrome, so
    it survives the ruler insets and a later ink-bounding-box pass would anchor
    its right border on the fill instead of on the schematic - which is how a
    trim that should have isolated six parts also captured 700px of empty
    workspace.  Scanning for the first column that is almost entirely that grey
    gives the real drawing extent; a design that fills the whole view has no
    such column and returns None.
    """
    step = max(1, WORKSPACE_SAMPLE_STEP)
    ys = list(range(0, height, step))
    if not ys:
        return None
    needed = max(1, int(len(ys) * WORKSPACE_GREY_SHARE))
    for x in range(int(width)):
        offset = x * 4
        hits = 0
        for y in ys:
            row = rows[y]
            if _is_workspace_grey((row[offset + 2], row[offset + 1], row[offset])):
                hits += 1
                if hits >= needed:
                    break
        if hits >= needed:
            return x
    return None


def _is_shadow_grey(pixel):
    """True for the neutral grey of the sheet's drop shadow."""
    for channel in pixel:
        if channel < SHADOW_MIN or channel > SHADOW_MAX:
            return False
    return True


def _shadow_left_edge(rows, width, height, paper_right):
    """First column of the sheet drop shadow, or None.

    The sheet is drawn with a soft shadow a few columns wide: neutral greys
    from ~100 up to ~161 that get lighter to the right and end at the workspace
    fill.  They are darker than ``INK_DARK_MAX``?  No - they sit between 100 and
    170, so the chroma test does not fire either, but the *dark* test fires for
    the first one or two columns, which is enough to push the bounding box right
    up against the sheet and grow a six-part schematic into a nearly blank
    sheet.  Walking left from the workspace edge while the columns stay neutral
    grey finds the leading column of the shadow; everything from there right is
    background.
    """
    if paper_right is None:
        return None
    step = max(1, WORKSPACE_SAMPLE_STEP)
    ys = list(range(0, height, step))
    if not ys:
        return None
    needed = max(1, int(len(ys) * WORKSPACE_GREY_SHARE))
    limit = max(INK_INSET_LEFT, int(paper_right) - SHADOW_SPAN)
    edge = None
    for x in range(int(paper_right) - 1, limit - 1, -1):
        offset = x * 4
        hits = 0
        for y in ys:
            row = rows[y]
            if _is_shadow_grey((row[offset + 2], row[offset + 1], row[offset])):
                hits += 1
                if hits >= needed:
                    break
        if hits >= needed:
            edge = x
        else:
            if edge is not None:
                return edge
            # A column that is not shadow grey yet: keep walking left, the
            # shadow may be separated from the fill by antialiased columns.
            continue
    return edge


def _content_bbox(rows, width, height):
    """Bounding box of the drawing itself, or None for a blank canvas.

    The coordinate ruler band along the top and left edge and the sunken view
    border on all four sides are structural chrome, not content, so the search
    starts inside them.  The workspace fill to the right of the sheet is cut
    away first so the bounding box can only be anchored on real ink.
    """
    left = min(INK_INSET_LEFT, max(0, width - 2))
    top = min(INK_INSET_TOP, max(0, height - 2))
    right = max(left + 1, width - INK_INSET_RIGHT)
    bottom = max(top + 1, height - INK_INSET_BOTTOM)
    paper = _paper_right_edge(rows, width, height)
    if paper is not None and paper > left:
        right = min(right, paper)
        # The sheet's drop shadow sits just left of the fill.  Its darkest
        # columns fail the dark test, so without this cut the bounding box
        # stretches to the shadow and a small circuit is reported as wide as
        # the whole sheet.
        shadow = _shadow_left_edge(rows, width, height, paper)
        if shadow is not None and shadow > left + 1 and shadow - 1 < right:
            right = min(right, shadow - 1)
    bbox = _ink_bbox(rows, width, height, (left, top, right, bottom))
    if bbox is None:
        # A view smaller than the ruler insets: retry without them rather than
        # declaring a small but non-empty canvas blank.
        bbox = _ink_bbox(rows, width, height, (0, 0, right, height))
    return bbox


def _paper_crop(width, height, rows):
    """Trim ``rows`` to the sheet of paper: ruler chrome and fill removed.

    ``content`` answers "where is the drawing", this answers "what does the
    sheet look like": the paper edges, its frame and every part stay exactly
    where Multisim drew them, and the flat workspace grey to the right of the
    sheet (plus the sheet's drop shadow) is dropped.  Falls back to the whole
    view when the sheet edge cannot be found, which is what a design zoomed in
    far enough to cover the entire sheet looks like.
    """
    left = min(INK_INSET_LEFT, max(0, width - 2))
    top = min(INK_INSET_TOP, max(0, height - 2))
    bottom = max(top + 1, height - INK_INSET_BOTTOM)
    paper = _paper_right_edge(rows, width, height)
    right = max(left + 1, width - INK_INSET_RIGHT)
    box = None
    if paper is not None and paper > left + 1:
        right = min(right, paper)
        shadow = _shadow_left_edge(rows, width, height, paper)
        if shadow is not None and shadow > left + 1 and shadow - 1 < right:
            right = min(right, shadow - 1)
        box = [int(left), int(top), int(right), int(bottom)]
    crop_w, crop_h, cropped = _crop_rows(rows, width, height, left, top,
                                         right - left, bottom - top)
    return crop_w, crop_h, cropped, box


def _content_crop(width, height, rows, pad=CONTENT_PAD):
    """Trim ``rows`` to the schematic plus a margin, or to the paper extent.

    A bounding box alone is not enough: the sheet of paper is drawn all the way
    down the view, so the ``V1..V4`` labels on the left put the bottom edge at
    the paper bottom and the picture ends up 85% empty paper.  Whenever the
    drawing does not reach both the left and the right paper edge, the caller
    asked for "the schematic", not "the sheet", so the result is the ink box
    grown by ``pad`` and clipped to the paper.
    """
    bbox = _content_bbox(rows, width, height)
    if bbox is None:
        # Nothing drawn: keep the whole paper rather than the whole view.
        paper = _paper_right_edge(rows, width, height)
        right = paper if paper is not None and paper > INK_INSET_LEFT else width
        crop_w, crop_h, cropped = _crop_rows(rows, width, height, 0, 0, right, height)
        return crop_w, crop_h, cropped, None
    paper = _paper_right_edge(rows, width, height)
    paper_right = paper if paper is not None and paper > bbox[2] else width
    ink_width = bbox[2] - bbox[0]
    ink_height = bbox[3] - bbox[1]
    # The sheet is drawn with a drop shadow a few columns wide to the left of
    # the workspace fill.  Those columns are neutral greys between 100 and 170,
    # so the chroma test flags them as ink and the bounding box then stretches
    # all the way to the shadow instead of stopping at the drawing.  Leaving
    # the shadow out keeps a small circuit small instead of growing the picture
    # until it fills the sheet.
    shadow = _shadow_left_edge(rows, width, height, paper_right)
    if shadow is not None and shadow > INK_INSET_LEFT and shadow - 1 > bbox[2]:
        trimmed = _ink_bbox(rows, width, height,
                            (INK_INSET_LEFT, INK_INSET_TOP, shadow - 1,
                             max(INK_INSET_TOP + 1, height - INK_INSET_BOTTOM)))
        if trimmed is not None:
            bbox = trimmed
            ink_width = bbox[2] - bbox[0]
            ink_height = bbox[3] - bbox[1]
    top_room = max(0, INK_INSET_TOP)
    bottom_limit = max(top_room + 1, height - INK_INSET_BOTTOM)
    left = max(INK_INSET_LEFT, bbox[0] - pad)
    top = max(top_room, bbox[1] - pad)
    right = min(paper_right - INK_INSET_RIGHT, bbox[2] + pad)
    bottom = min(bottom_limit, bbox[3] + pad)
    # Give the trimmed picture a useful shape.  A part placed high on the sheet
    # leaves the sheet empty underneath it, so the ink box alone can come out
    # as a thin strip; grow its short side towards the sheet while there is
    # paper to grow into.  A drawing that already uses most of the sheet is
    # left exactly as measured.  Bounds are strict inequalities so the ruler
    # band and the view border can never be pulled back in.
    if ink_width <= paper_right * 0.6 and ink_height <= height * 0.6:
        # Keep the region around the schematic but never cut the paper in the
        # picture: a simulated oscilloscope face or a wire running to the
        # sheet edge would look truncated.  The clip below stops at the sheet.
        want_w = paper_right if ink_width * 1.6 > paper_right * 0.75 else max(int(ink_width * 1.6), 640)
        want_h = max(int(round(want_w * 0.45)), int(ink_height * 1.8))
        room_w = (paper_right - INK_INSET_RIGHT) - INK_INSET_LEFT
        room_h = bottom_limit - top_room
        want_w = min(want_w, room_w)
        want_h = min(want_h, room_h)
        extra = want_w - (right - left)
        if extra > 0:
            grow_left = min(extra // 2, left - INK_INSET_LEFT)
            grow_right = min(extra - grow_left, paper_right - INK_INSET_RIGHT - right)
            left -= grow_left
            right += grow_right
        extra = want_h - (bottom - top)
        if extra > 0:
            grow_top = min(extra // 2, top - top_room)
            grow_bottom = min(extra - grow_top, bottom_limit - bottom)
            top -= grow_top
            bottom += grow_bottom
    left = max(0, min(left, bbox[0]))
    top = max(0, min(top, bbox[1]))
    right = min(width, max(right, bbox[2]))
    bottom = min(height, max(bottom, bbox[3]))
    crop_w, crop_h, cropped = _crop_rows(rows, width, height, left, top,
                                         right - left, bottom - top)
    return crop_w, crop_h, cropped, [int(left), int(top), int(right), int(bottom)]


def _resize_rows(rows, width, height, new_width):
    """Nearest-neighbour horizontal scale keeping the aspect ratio."""
    new_width = int(new_width)
    if new_width <= 0 or new_width == width:
        return width, height, rows
    new_height = max(1, int(round(height * new_width / float(width))))
    x_index = [min(width - 1, (x * width) // new_width) for x in range(new_width)]
    scaled = []
    for y in range(new_height):
        source_y = min(height - 1, (y * height) // new_height)
        source = rows[source_y]
        target = bytearray(new_width * 4)
        for x, source_x in enumerate(x_index):
            offset = source_x * 4
            target[x * 4:x * 4 + 4] = source[offset:offset + 4]
        scaled.append(bytes(target))
    return new_width, new_height, scaled


# --------------------------------------------------------------------------
# PNG / BMP encoding (no Pillow available in the 32-bit interpreter)
# --------------------------------------------------------------------------

def _png_chunk(tag, data):
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def _bgra_rows_to_rgb(rows, width):
    """BGRA/BGRX rows -> RGB rows (PNG wants R,G,B byte order)."""
    rgb_rows = []
    byte_count = int(width) * 3
    for row in rows:
        rgb = bytearray(byte_count)
        rgb[0::3] = row[2::4]
        rgb[1::3] = row[1::4]
        rgb[2::3] = row[0::4]
        rgb_rows.append(bytes(rgb))
    return rgb_rows


def _encode_png(width, height, rgb_rows, level=6):
    raw = bytearray()
    for row in rgb_rows:
        raw.append(0)  # filter type 0 (None)
        raw += row
    header = struct.pack(">IIBBBBB", int(width), int(height), 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), level))
        + _png_chunk(b"IEND", b"")
    )


def _encode_bmp(width, height, bgra_rows):
    image = b"".join(reversed(bgra_rows))
    info = struct.pack("<IiiHHIIiiII", 40, int(width), int(height), 1, 32, 0,
                       len(image), 2835, 2835, 0, 0)
    header = struct.pack("<2sIHHI", b"BM", 14 + 40 + len(image), 0, 0, 54)
    return header + info + image


def screenshot(path, mode="window", hwnd=0, crop="none", target_width=0):
    """Capture the visible Multisim window to a PNG file.

    ``mode``: ``window`` (whole frame), ``client`` (client area only),
    ``canvas`` (the schematic drawing view: the ``AfxFrameOrView140u`` viewport
    inside the MDI child, falling back to ``CANVAS_FRAC`` when that window is
    not there yet).
    ``crop``: ``none``, ``canvas`` (same viewport as ``mode: "canvas"``) or
    ``content`` (that viewport trimmed to the schematic-ink bounding box, 18px
    padding, searching inside the coordinate rulers).
    ``target_width``: optional nearest-neighbour down/up-scale.

    ``canvas`` and ``content`` are alternatives rather than cumulative: both
    describe the drawing view, so requesting ``mode: "canvas"`` together with
    ``crop: "content"`` trims the viewport once, not twice.
    """
    _ensure_dpi()
    path = os.path.abspath(_as_str(path, "path"))
    mode = _as_str(mode, "mode", "window").strip().lower()
    crop = _as_str(crop, "crop", "none").strip().lower()
    target_width = _as_int(target_width, "target_width", 0)
    if mode not in ("window", "client", "canvas"):
        raise _error("BAD_ARGS", "mode must be 'window', 'client' or 'canvas', got %r" % mode)
    if crop not in ("none", "canvas", "content", "paper"):
        raise _error("BAD_ARGS",
                     "crop must be 'none', 'canvas', 'content' or 'paper', got %r" % crop)

    resolved = _resolve_hwnd(hwnd)
    # Every crop mode is expressed in frame coordinates, so it needs the whole
    # frame even when the caller asked for "client".
    wants_view = mode == "canvas" or crop in ("canvas", "content", "paper")
    area = "window" if wants_view else mode
    width, height, rows = _capture(resolved, area)

    view_fallback = False
    if wants_view:
        # Prefer the real drawing viewport over the fixed CANVAS_FRAC guess:
        # CANVAS_FRAC was measured on one window size and drifts badly once the
        # frame is maximised, leaving workspace grey, the spreadsheet pane and
        # the status bar in the picture.
        box = _view_crop_box(resolved, width, height)
        if box:
            width, height, rows = _crop_rows(rows, width, height,
                                             box[0], box[1], box[2], box[3])
        else:
            view_fallback = True
            width, height, rows = _canvas_crop(width, height, rows)

    crop_fallback = False
    if crop == "content":
        width, height, rows, _box = _content_crop(width, height, rows)
        # An empty canvas (a brand new design with no parts) has nothing but
        # background; there is no bbox to trim to, so keep the canvas image
        # and say so instead of failing the operation.
        crop_fallback = _box is None
    elif crop == "paper":
        # The whole sheet of paper as drawn on screen, minus the ruler chrome
        # and the workspace fill to its right.  This is what "a screenshot of
        # the design" means for a report: the frame is visible, the parts sit
        # where the design puts them, and nothing of the application chrome or
        # of the empty workspace is included.
        width, height, rows, _box = _paper_crop(width, height, rows)
        crop_fallback = _box is None
    if target_width:
        width, height, rows = _resize_rows(rows, width, height, target_width)

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    extension = os.path.splitext(path)[1].lower()
    if extension == ".bmp":
        data = _encode_bmp(width, height, rows)
    else:
        data = _encode_png(width, height, _bgra_rows_to_rgb(rows, width))

    with open(path, "wb") as handle:
        handle.write(data)

    return {
        "path": path,
        "bytes": int(os.path.getsize(path)),
        "width": int(width),
        "height": int(height),
        "hwnd": int(resolved),
        # Extra field (additive, no contract signature change): true when
        # crop="content" found no schematic ink and the viewport crop was
        # written unchanged instead of raising.
        "crop_fallback": bool(crop_fallback),
        # Extra field (additive): true when the drawing viewport window was not
        # found and the fixed CANVAS_FRAC rectangle was used instead.
        "view_fallback": bool(view_fallback),
    }


# --------------------------------------------------------------------------
# File drop (WM_DROPFILES)
# --------------------------------------------------------------------------

def _drop_file(hwnd, pid, path):
    """Inject an Explorer file drop into ``hwnd`` (the only reliable way to
    open a ``.cir`` netlist; COM ``OpenFile`` crashes on netlists)."""
    absolute = os.path.abspath(path)
    _shell32.DragAcceptFiles(wintypes.HWND(hwnd), True)

    payload = struct.pack("<LiiLL", 20, 0, 0, 0, 1)  # pFiles, pt.x, pt.y, fNC, fWide
    payload += absolute.encode("utf-16-le") + b"\x00\x00"
    size = len(payload)

    process = _kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, int(pid))
    if not process:
        raise _error(
            "GUI_ERROR",
            "OpenProcess(0x%X) failed for pid %d" % (PROCESS_ALL_ACCESS, pid),
            "GetLastError=%d (integrity level mismatch?)" % ctypes.get_last_error(),
        )
    remote = None
    try:
        remote = _kernel32.VirtualAllocEx(
            process, None, size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE
        )
        if not remote:
            raise _error("GUI_ERROR", "VirtualAllocEx failed",
                         "GetLastError=%d" % ctypes.get_last_error())
        written = ctypes.c_size_t(0)
        buffer = ctypes.create_string_buffer(payload, size)
        if not _kernel32.WriteProcessMemory(
            process, ctypes.c_void_p(remote), buffer, size, ctypes.byref(written)
        ):
            raise _error("GUI_ERROR", "WriteProcessMemory failed",
                         "GetLastError=%d" % ctypes.get_last_error())
        if written.value != size:
            raise _error("GUI_ERROR",
                         "WriteProcessMemory wrote %d of %d bytes" % (written.value, size))

        result = ctypes.c_size_t(0)
        delivered = _user32.SendMessageTimeoutW(
            wintypes.HWND(hwnd), WM_DROPFILES, wintypes.WPARAM(remote),
            wintypes.LPARAM(0), SMTO_ABORTIFHUNG, 90000, ctypes.byref(result),
        )
        if not delivered:
            raise _error(
                "GUI_ERROR",
                "window %d did not acknowledge the file drop" % hwnd,
                "SendMessageTimeout failed, GetLastError=%d" % ctypes.get_last_error(),
            )
    finally:
        if remote:
            _kernel32.VirtualFreeEx(process, ctypes.c_void_p(remote), 0, MEM_RELEASE)
        _kernel32.CloseHandle(process)
    return {"hwnd": int(hwnd), "path": absolute, "sent": True}


def open_file(path, close_existing=True, wait_seconds=30.0):
    """Open a circuit/netlist by dropping it on the visible Multisim frame."""
    _ensure_dpi()
    path = os.path.abspath(_as_str(path, "path"))
    close_existing = _as_bool(close_existing, "close_existing", True)
    wait_seconds = _as_float(wait_seconds, "wait_seconds", 30.0)
    if not os.path.isfile(path):
        raise _error("PATH_MISSING", "file does not exist: %s" % path)

    hwnd = _resolve_hwnd(0, 5.0)
    pid = _pid_of(hwnd)
    if pid <= 0:
        raise _error("GUI_ERROR", "could not determine the process owning window %d" % hwnd)

    dialogs_closed = []
    if close_existing:
        dialogs_closed.extend(_close_documents(hwnd))
        for dialog in close_dialogs().get("closed", []):
            dialogs_closed.append(dialog)

    before = set(_mdi_documents(hwnd))
    _focus_window(hwnd)
    _drop_file(hwnd, pid, path)

    # Poll until a document shows up (or the wait budget expires), closing any
    # modal dialog that blocks the conversion.
    deadline = time.time() + max(1.0, wait_seconds)
    confirmed = False
    while time.time() < deadline:
        time.sleep(0.5)
        for dialog in _multisim_dialogs():
            title = _window_text(dialog).lower()
            # "saving changes"/"overwrite" prompts are answered, not re-dropped.
            if any(word in title for word in ("保存", "save", "确认", "confirm")):
                _answer_save_prompt(timeout=0.5)
        for dialog in close_dialogs().get("closed", []):
            dialogs_closed.append(dialog)
        documents = _mdi_documents(hwnd)
        new_documents = [doc for doc in documents if doc not in before]
        if new_documents or (documents and not before):
            confirmed = True
            time.sleep(1.0)
            break

    time.sleep(1.5)
    for dialog in close_dialogs().get("closed", []):
        dialogs_closed.append(dialog)
    if _mdi_documents(hwnd):
        confirmed = True

    return {
        "opened": True,
        "path": path,
        "hwnd": int(hwnd),
        "dialogs_closed": dialogs_closed,
        "confirmed": bool(confirmed),
    }


# --------------------------------------------------------------------------
# Save As
# --------------------------------------------------------------------------

def _click_canvas(hwnd):
    """Click the canvas centre so the MDI child owns the keyboard focus.

    Ctrl+S from the frame alone is swallowed by the main frame; the document
    must be the active child first.
    """
    rect = _window_rect(hwnd)
    width, height = _rect_size(rect)
    if width <= 0 or height <= 0:
        return False
    point = wintypes.POINT(width // 2, height // 2)
    _user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(point))
    _user32.SetCursorPos(int(point.x), int(point.y))
    time.sleep(0.25)
    _user32.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.08)
    _user32.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.4)
    return True


def _wait_dialog(predicate, wait_seconds):
    deadline = time.time() + max(0.5, float(wait_seconds))
    while time.time() < deadline:
        for dialog in _multisim_dialogs():
            if predicate(dialog):
                return dialog
        time.sleep(0.3)
    return None


def _is_save_dialog(dialog):
    title = _window_text(dialog).lower()
    if "另存" in title or "save as" in title:
        return True
    # Some builds title it just "保存"/"Save": accept a dialog that owns a
    # filename edit box plus a Save button.
    has_edit = _find_child_by_class(dialog, "edit") is not None
    has_button = _find_button(dialog, ("保存", "save", "确定", "ok")) is not None
    return bool(has_edit and has_button)


def _save_dialog_filename_edit(dialog):
    """The file-name edit box of a Save As dialog.

    ``GetDlgItem(dialog, 1001)`` returns 0 on Multisim 14.3 because the field
    lives inside a ``DUIViewWndClassName`` child.  Walk the tree and take the
    widest edit box low in the dialog -- that is the name field rather than the
    search box at the top.
    """
    candidates = []
    stack = [int(dialog)]
    seen = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        try:
            children = list(_iter_children(current))
        except pywintypes.error:
            children = []
        for child in children:
            handle = int(child)
            stack.append(handle)
            if _class_name(child).lower() != "edit":
                continue
            rect = _window_rect(child)
            width = rect[2] - rect[0]
            height = rect[3] - rect[1]
            if width > 200 and height > 8:
                candidates.append((rect[1], width, handle))
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1][2]


def _fill_and_submit_save_dialog(dialog, path):
    """Type the target path into the Save As dialog and press Save.

    The path is entered with real Unicode keystrokes and read back before the
    Save button is touched.  ``WM_SETTEXT`` would look correct in the edit box
    while leaving the dialog's internal file state alone; Multisim then saves
    under the previous name, and on an untitled design that name resolves to
    ``Documents\\National Instruments\\...\\设计1.ms14`` -- silently destroying a
    real file.  When the read-back does not match, the dialog is left open and
    no file is written.
    """
    edit = _save_dialog_filename_edit(dialog)
    if not edit:
        raise _error(
            "GUI_ERROR", "Save As dialog has no filename edit box",
            "dialog=%r class=%r" % (_window_text(dialog), _class_name(dialog)),
        )

    _user32.SetForegroundWindow(wintypes.HWND(dialog))
    time.sleep(0.2)
    _click_window(edit)
    _user32.SetFocus(wintypes.HWND(edit))
    time.sleep(0.25)

    _send_chords(_parse_keys("ctrl+a"))
    time.sleep(0.15)
    _send_unicode_text(path)
    time.sleep(0.35)

    typed = _read_edit_text(edit)
    wanted = path
    if typed.strip() != wanted:
        raise _error(
            "GUI_ERROR",
            "Save As filename did not take the requested path; refusing to save",
            "typed=%r wanted=%r" % (typed.strip(), wanted),
        )

    button = _find_button(dialog, ("保存", "save"))
    if not button:
        button = _user32.GetDlgItem(wintypes.HWND(dialog), IDOK)
    if not button:
        _user32.PostMessageW(wintypes.HWND(dialog), WM_COMMAND, wintypes.WPARAM(IDOK), 0)
    else:
        _user32.PostMessageW(wintypes.HWND(int(button)), BM_CLICK, 0, 0)
    return int(edit), int(button) if button else None


def _read_edit_text(edit, size=4096):
    buffer = ctypes.create_unicode_buffer(size)
    address = ctypes.cast(buffer, ctypes.c_void_p).value
    _user32.SendMessageW(
        wintypes.HWND(int(edit)),
        WM_GETTEXT,
        wintypes.WPARAM(size),
        address,
    )
    return buffer.value



def _document_is_untitled(hwnd):
    """True when the active MDI document has no file name yet.

    An untitled document is titled ``设计1``/``Design1``; ``Ctrl+S`` on it opens
    the Save As dialog, while on a named document it writes in place instead.
    """
    documents = _mdi_documents(hwnd)
    if not documents:
        return False
    title = _window_text(documents[0]).strip()
    return title.lower() in _DEFAULT_UNTITLED_NAMES


def save_as(path, wait_seconds=20.0):
    """Save the active document under ``path`` through the Save As dialog.

    Only an untitled document opens that dialog: on a named document both
    ``Ctrl+S`` and ``Ctrl+Shift+S`` write in place under the existing name, so
    the call refuses rather than silently overwriting the source design.  The
    target file is never deleted before the attempt, and a dialog that closes
    without producing the file is reported as an error instead of a success.

    The filename must be typed with real Unicode keystrokes: ``WM_SETTEXT``
    fills the edit box while leaving the dialog's own state on the previous
    name, and Multisim then writes the untitled default
    ``设计1.ms14`` into the documents folder -- silently destroying a real file.
    """
    _ensure_dpi()
    path = os.path.abspath(_as_str(path, "path"))
    wait_seconds = _as_float(wait_seconds, "wait_seconds", 20.0)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    hwnd = _resolve_hwnd(0, 5.0)
    if not _document_is_untitled(hwnd):
        raise _error(
            "GUI_ERROR",
            "Multisim wrote the design in place under its own name instead of "
            "opening Save As for %s" % path,
            "the document already has a file name, so Ctrl+S saves it there; "
            "open an untitled document, or write copies with msim_circuit "
            "save_as (COM) which really does honour the path",
        )
    _focus_window(hwnd)
    # The database warnings are modal and eat every keystroke; dismissing them
    # is what makes ctrl+s reach the document at all.
    dismissed = dismiss_database_dialogs().get("dismissed", [])
    # Clicking the document is what actually hands it the keyboard; the modal
    # database warnings have to be gone first or the click lands on them.
    documents = _mdi_documents(hwnd)
    if documents:
        _click_window(documents[0])

    dialog = None
    used_chord = None
    for chord in ("ctrl+s", "ctrl+shift+s"):
        _send_chords(_parse_keys(chord))
        dialog = _wait_dialog(_is_save_dialog, 6.0 if chord == "ctrl+s" else wait_seconds)
        used_chord = chord
        if dialog:
            break
        dismissed.extend(dismiss_database_dialogs().get("dismissed", []))

    if not dialog:
        # The document is untitled, so a missing Save As dialog means the keys
        # never reached it.  Never claim success: an old file may sit at :path.
        raise _error(
            "GUI_ERROR",
            "Multisim did not open the Save As dialog for %s" % path,
            "keystrokes %s reached the window but no dialog appeared; the "
            "document may have been saved in place, or a modal dialog is still "
            "eating input. Nothing was written to the requested path -- retry, "
            "or use msim_circuit save_as (COM) which really does honour the path"
            % (used_chord,),
        )

    dialog_title = _window_text(dialog)
    edit_hwnd, button_hwnd = _fill_and_submit_save_dialog(dialog, path)

    saved = False
    deadline = time.time() + max(3.0, wait_seconds)
    while time.time() < deadline:
        if not _user32.IsWindow(wintypes.HWND(dialog)) or not _is_visible(dialog):
            saved = True
            break
        for other in _multisim_dialogs():
            if int(other) == int(dialog):
                continue
            text = _dialog_text(other)
            if _is_database_dialog(text):
                _dismiss_database_dialog(other)
                continue
            if any(word in text.lower() for word in (
                    "替换", "overwrite", "是否替换", "已存在", "确认另存为", "确认保存")):
                confirm = _find_button(other, ("是", "yes", "确定", "ok", "替换", "replace"))
                if confirm:
                    _user32.PostMessageW(wintypes.HWND(int(confirm)), BM_CLICK, 0, 0)
        time.sleep(0.35)

    if saved:
        deadline = time.time() + 15.0
        while time.time() < deadline:
            if os.path.exists(path) and os.path.getsize(path) > 1000:
                break
            time.sleep(0.4)

    created = os.path.exists(path) and os.path.getsize(path) > 1000
    if saved and not created:
        raise _error(
            "GUI_ERROR",
            "the Save As dialog closed but %s was never written" % path,
            "Multisim ignored the typed path and may have written the untitled "
            "default under %s instead; use msim_circuit save_as (COM) for "
            "reliable writes" % DEFAULT_DOC_DIR,
        )

    return {
        "path": path,
        "saved": bool(saved and os.path.exists(path)),
        "dialog_title": dialog_title,
        "edit_hwnd": edit_hwnd,
        "button_hwnd": button_hwnd,
        "chord": used_chord,
        "saved_in_place": False,
        "dismissed": dismissed,
    }


# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------

def send_keys(keys, hwnd=0):
    """Send a chord sequence to the Multisim window (``^s``, ``ctrl+shift+s``)."""
    _ensure_dpi()
    chords = _parse_keys(_as_str(keys, "keys"))
    resolved = _resolve_hwnd(hwnd)
    _focus_window(resolved)
    _send_chords(chords)
    return {
        "hwnd": int(resolved),
        "sent": [token for _mods, _vk, token in chords],
    }


# --------------------------------------------------------------------------
# GUI simulation analysis
# --------------------------------------------------------------------------

class AnalysisError(Exception):
    """Raised when the GUI analysis flow cannot complete."""

    def __init__(self, code, message, detail=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def to_dict(self):
        return {"code": self.code, "message": self.message, "detail": self.detail}


def _lb_items(hwnd, limit=400):
    count = int(win32gui.SendMessage(hwnd, LB_GETCOUNT, 0, 0))
    out = []
    for index in range(min(count, limit)):
        length = int(win32gui.SendMessage(hwnd, LB_GETTEXTLEN, index, 0))
        if length <= 0:
            out.append("")
            continue
        buffer = ctypes.create_unicode_buffer(length + 2)
        _user32.SendMessageW(hwnd, LB_GETTEXT, index,
                            ctypes.cast(buffer, ctypes.c_void_p).value)
        out.append(buffer.value)
    return out


def _child_controls(hwnd):
    out = []

    def callback(child, _):
        try:
            out.append({
                "hwnd": int(child),
                "class": win32gui.GetClassName(child),
                "text": win32gui.GetWindowText(child),
                "rect": win32gui.GetWindowRect(child),
                "visible": bool(win32gui.IsWindowVisible(child)),
            })
        except Exception:
            pass
        return True

    win32gui.EnumChildWindows(hwnd, callback, None)
    return out


def _pid_dialogs(pid, title=None):
    found = []

    def callback(hwnd, _):
        try:
            if win32process.GetWindowThreadProcessId(hwnd)[1] != pid:
                return True
            if not win32gui.IsWindowVisible(hwnd):
                return True
            if win32gui.GetClassName(hwnd) != "#32770":
                return True
            text = win32gui.GetWindowText(hwnd)
            if title is not None and title not in text:
                return True
            found.append({"hwnd": int(hwnd), "title": text,
                          "rect": win32gui.GetWindowRect(hwnd)})
        except Exception:
            pass
        return True

    win32gui.EnumWindows(callback, None)
    return found


def _tap_key(vk, hold=0.05, gap=0.14):
    _user32.keybd_event(vk, 0, 0, 0)
    time.sleep(hold)
    _user32.keybd_event(vk, 0, 2, 0)
    time.sleep(gap)


def _escape_all(times=4):
    for _ in range(times):
        _user32.keybd_event(0x1B, 0, 0, 0)
        _user32.keybd_event(0x1B, 0, 2, 0)
        time.sleep(0.18)
    time.sleep(0.35)


def _click_at(x, y):
    _user32.SetCursorPos(int(x), int(y))
    time.sleep(0.18)
    _user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.06)
    _user32.mouse_event(0x0004, 0, 0, 0, 0)


def _analysis_key(name):
    key = str(name or "").strip().lower()
    if key in ANALYSIS_ALIASES:
        return key
    for canonical, aliases in ANALYSIS_ALIASES.items():
        if key and any(key == item.lower() for item in aliases):
            return canonical
    raise AnalysisError(
        "BAD_ARGS",
        "unknown analysis %r" % (name,),
        "known names: " + ", ".join(ANALYSIS_NAMES),
    )


def _open_analyses_dialog(frame, wait_seconds=15.0):
    _escape_all()
    _user32.SetCursorPos(1800, 1000)
    time.sleep(0.2)
    _focus_window(frame)
    time.sleep(0.5)
    # alt+s then MENU steps down then Return.
    _user32.keybd_event(0x12, 0, 0, 0)
    _user32.keybd_event(0x53, 0, 0, 0)
    time.sleep(0.08)
    _user32.keybd_event(0x53, 0, 2, 0)
    _user32.keybd_event(0x12, 0, 2, 0)
    time.sleep(1.2)
    for _ in range(ANALYSES_MENU_STEPS):
        _tap_key(0x28)
    time.sleep(0.35)
    _tap_key(0x0D)
    pid = win32process.GetWindowThreadProcessId(frame)[1]
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        hits = _pid_dialogs(pid, ANALYSES_TITLE)
        if hits:
            time.sleep(0.6)
            return hits[0]
        time.sleep(0.25)
    _escape_all()
    raise AnalysisError(
        "GUI_ERROR",
        "the Analyses and Simulation dialog did not open",
        "Multisim must be the foreground window with a design loaded, and no "
        "modal dialog may be open.",
    )


def _pick_analysis(dialog, key):
    lists = [item for item in _child_controls(dialog["hwnd"])
             if item["class"] == "ListBox"]
    lists.sort(key=lambda item: item["rect"][1])
    aliases = ANALYSIS_ALIASES[key]
    for item in lists:
        rect = item["rect"]
        if (rect[3] - rect[1]) < 300 or (rect[2] - rect[0]) < 120:
            continue
        labels = _lb_items(item["hwnd"])
        for index, label in enumerate(labels):
            lowered = label.lower()
            if any(alias.lower() in lowered for alias in aliases):
                height = int(win32gui.SendMessage(
                    item["hwnd"], LB_GETITEMHEIGHT, 0, 0)) or 23
                _click_at(rect[0] + 40, rect[1] + height // 2 + height * index)
                time.sleep(1.2)
                selected = int(win32gui.SendMessage(
                    item["hwnd"], LB_GETCURSEL, 0, 0))
                return {"listbox": int(item["hwnd"]), "index": index,
                        "label": label, "selected": selected,
                        "ok": selected == index, "available": labels}
    available = _lb_items(lists[0]["hwnd"]) if lists else []
    raise AnalysisError(
        "GUI_ERROR",
        "analysis %r is not in the dialog list" % (key,),
        "the dialog shows: " + ", ".join(available),
    )


def _press_run(dialog):
    for item in _child_controls(dialog["hwnd"]):
        if item["class"] != "Button":
            continue
        text = (item["text"] or "").replace("&", "").strip().lower()
        if text == "run":
            win32gui.SendMessage(item["hwnd"], BM_CLICK, 0, 0)
            return {"button": item["hwnd"], "label": item["text"]}
    raise AnalysisError("GUI_ERROR", "the Run button was not found")


def graph_windows():
    """List the grapher windows Multisim opened for this session."""
    items = [item for item in windows(False)["items"]
             if "\u56fe\u793a\u4eea" in item["title"]
             or "grapher" in item["title"].lower()]
    return {"windows": items, "count": len(items)}


def run_analysis(analysis="transient", hwnd=0, wait_seconds=30.0,
                 timeout=180.0):
    """Run one named analysis in the visible window and report the outcome."""
    resolved = _resolve_hwnd(hwnd)
    key = _analysis_key(analysis)
    _focus_window(resolved)
    time.sleep(0.4)
    dialog = _open_analyses_dialog(resolved, wait_seconds)
    selected = _pick_analysis(dialog, key)
    run = _press_run(dialog)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not win32gui.IsWindow(dialog["hwnd"]):
            break
        time.sleep(0.3)
    closed = not win32gui.IsWindow(dialog["hwnd"])
    time.sleep(1.5)
    return {
        "analysis": key,
        "dialog": dialog,
        "selected": selected,
        "run": run,
        "dialog_closed": closed,
        "graphs": graph_windows()["windows"],
        "title": win32gui.GetWindowText(resolved),
        "alive": bool(win32gui.IsWindow(resolved)),
    }


def capture_graph(path, hwnd=0, wait_seconds=20.0):
    """Screenshot the grapher window: the real Multisim waveform plot."""
    if hwnd:
        target = int(hwnd)
    else:
        target = 0
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            candidates = graph_windows()["windows"]
            if candidates:
                target = int(candidates[0]["hwnd"])
                break
            time.sleep(0.4)
        if not target:
            raise AnalysisError(
                "NO_MULTISIM",
                "no grapher window is open",
                "run an analysis first (msim_gui action=run_analysis) or pass "
                "hwnd explicitly.",
            )
    result = screenshot(path, mode="window", hwnd=target, crop="none")
    result["grapher"] = True
    result["title"] = win32gui.GetWindowText(target)
    return result


# --------------------------------------------------------------------------
# Self-test entry point (the only place allowed to print to stdout)
# --------------------------------------------------------------------------

def _self_test(argv):
    import argparse
    import json

    # The Windows console defaults to the ANSI code page (GBK here); window
    # titles and dialog texts are unicode, so force UTF-8 on the self-test
    # channel only.  During normal worker operation nothing prints at all.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser(
        description="msim_gui.py self-test: probe + launch + enumerate + screenshots")
    parser.add_argument("--open", default="", help="file to open via WM_DROPFILES")
    parser.add_argument("--save-as", default="", help="save the active design here")
    parser.add_argument("--write", default="", help="directory for screenshots")
    parser.add_argument("--wait", type=float, default=45.0, help="launch wait seconds")
    parser.add_argument("--keep", action="store_true", help="leave Multisim running")
    args = parser.parse_args(argv)

    image_dir = args.write or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))),
        "probe", "img",
    )
    os.makedirs(image_dir, exist_ok=True)

    def report(name, value):
        print("[%s] %s" % (name, json.dumps(value, ensure_ascii=False, default=str)))

    def call(name, function, *call_args, **call_kwargs):
        try:
            result = function(*call_args, **call_kwargs)
        except GuiError as exc:
            report(name, {"error": exc.to_dict()})
            return None
        report(name, result)
        return result

    if not args.keep:
        call("kill", kill)
    call("probe", probe)
    if not call("launch", launch, args.wait, True):
        return 1

    call("main_window", main_window)
    window_list = call("windows", windows, True)
    if window_list:
        classes = sorted({item["class"] for item in window_list["items"]})
        print("[windows.classes] %s" % json.dumps(classes, ensure_ascii=False))
    call("focus", focus)
    call("screenshot.window", screenshot,
         os.path.join(image_dir, "gui_window.png"), "window")
    call("screenshot.canvas", screenshot,
         os.path.join(image_dir, "gui_canvas.png"), "window", 0, "canvas")
    call("screenshot.content", screenshot,
         os.path.join(image_dir, "gui_content.png"), "window", 0, "content")
    call("screenshot.scaled", screenshot,
         os.path.join(image_dir, "gui_scaled.png"), "window", 0, "canvas", 1200)

    if args.open:
        call("open_file", open_file, args.open)
        time.sleep(2.0)
        call("close_dialogs", close_dialogs)
        call("screenshot.opened", screenshot,
             os.path.join(image_dir, "gui_opened.png"), "window", 0, "content")
    if args.save_as:
        call("save_as", save_as, args.save_as)

    call("close_dialogs", close_dialogs)
    call("probe.final", probe)
    if not args.keep:
        call("kill", kill)
    return 0


if __name__ == "__main__":
    sys.exit(_self_test(sys.argv[1:]))
