"""窗口定位与位置记忆（XWayland）。

纯 Wayland 协议不允许应用自己移动窗口，因此贴纸应用运行在 X11
(XWayland) 后端下：从 GDK 取窗口 XID，再用 libX11 直接移动窗口。
拖动后的位置会保存到 ~/.config/sysstickers/position.json，下次启动恢复。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import os

_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "sysstickers")
_POSITION_FILE = os.path.join(_CONFIG_DIR, "position.json")


# ---------------------------------------------------------------- 可用性探测

def x11_available() -> bool:
    """探测 X11 / XWayland 是否可用（不依赖 GDK，供启动前选择后端）。"""
    library = ctypes.util.find_library("X11")
    if not library:
        return False
    try:
        lib = ctypes.CDLL(library)
        lib.XOpenDisplay.restype = ctypes.c_void_p
        lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        display = lib.XOpenDisplay(None)
        if not display:
            return False
        lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
        lib.XCloseDisplay(display)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- 位置持久化
#
# 文件格式（v3，每张贴纸一个坐标 + 窗口宽度）：
#   {"version": 3, "positions": {"clock": {"x": 36, "y": 92, "w": 708}, ...}}
# 卡片尺寸变化（例如布局调整）后，旧记录会自动作废、回到默认排布。
_SAVED_VERSION = 3


def load_saved_positions() -> dict[str, tuple[int, int, int]]:
    try:
        with open(_POSITION_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if int(data.get("version", 0)) < _SAVED_VERSION:
            return {}
        positions = data.get("positions", {})
        result: dict[str, tuple[int, int, int]] = {}
        for key, value in positions.items():
            result[str(key)] = (int(value["x"]), int(value["y"]), int(value["w"]))
        return result
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def save_saved_positions(positions: dict[str, tuple[int, int, int]]) -> None:
    try:
        os.makedirs(_CONFIG_DIR, exist_ok=True)
        data = {
            "version": _SAVED_VERSION,
            "positions": {
                key: {"x": int(x), "y": int(y), "w": int(w)}
                for key, (x, y, w) in positions.items()
            },
        }
        tmp = _POSITION_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, _POSITION_FILE)
    except OSError:
        pass


# ---------------------------------------------------------------- 坐标计算

def clamp_position(x: int, y: int, screen_w: int, screen_h: int,
                   win_w: int, win_h: int) -> tuple[int, int]:
    """保证窗口至少有 80px 留在屏幕内（显示器变化后也能找回）。"""
    min_x = 80 - win_w
    min_y = 0
    max_x = screen_w - 80
    max_y = screen_h - 80
    return min(max(x, min_x), max_x), min(max(y, min_y), max_y)


# ---------------------------------------------------------------- XID 获取

_gdk_x11 = None
_gdk_x11_tried = False


def xid_of(window) -> int:
    """取 GTK 窗口的 X11 XID（非 X11 后端返回 0）。"""
    global _gdk_x11, _gdk_x11_tried
    if not _gdk_x11_tried:
        _gdk_x11_tried = True
        try:
            import gi

            gi.require_version("Gdk", "4.0")
            gi.require_version("GdkX11", "4.0")
            from gi.repository import GdkX11

            _gdk_x11 = GdkX11
        except Exception:
            _gdk_x11 = None
    if _gdk_x11 is None:
        return 0
    surface = window.get_surface()
    if surface is None or not hasattr(surface, "get_xid"):
        return 0
    import warnings

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            return int(surface.get_xid())
    except Exception:
        return 0


# ---------------------------------------------------------------- 移动器

class X11Mover:
    """通过 libX11 移动窗口、读取几何信息。"""

    def __init__(self) -> None:
        self.ok = False
        self._lib = None
        self._display = None
        self._root = 0
        try:
            library = ctypes.util.find_library("X11")
            if not library:
                return
            lib = ctypes.CDLL(library)

            lib.XOpenDisplay.restype = ctypes.c_void_p
            lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
            display = lib.XOpenDisplay(None)
            if not display:
                return

            lib.XDefaultRootWindow.restype = ctypes.c_ulong
            lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
            lib.XMoveWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int]
            lib.XFlush.argtypes = [ctypes.c_void_p]
            lib.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
            lib.XDisplayWidth.argtypes = [ctypes.c_void_p, ctypes.c_int]
            lib.XDisplayHeight.argtypes = [ctypes.c_void_p, ctypes.c_int]
            lib.XTranslateCoordinates.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
                ctypes.c_int, ctypes.c_int,
                ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_ulong),
            ]
            lib.XGetGeometry.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong,
                ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_uint),
                ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint),
                ctypes.POINTER(ctypes.c_uint),
            ]

            self._lib = lib
            self._display = display
            self._root = lib.XDefaultRootWindow(display)
            self.ok = True
        except OSError:
            self.ok = False

    # ------------------------------------------------------------ 基本操作

    def screen_size(self) -> tuple[int, int]:
        return (int(self._lib.XDisplayWidth(self._display, 0)),
                int(self._lib.XDisplayHeight(self._display, 0)))

    def geometry(self, xid: int) -> tuple[int, int, int, int] | None:
        """返回窗口绝对坐标与尺寸 (x, y, w, h)，失败返回 None。"""
        if not self.ok or not xid:
            return None
        root_ret = ctypes.c_ulong()
        x, y = ctypes.c_int(), ctypes.c_int()
        w, h = ctypes.c_uint(), ctypes.c_uint()
        border, depth = ctypes.c_uint(), ctypes.c_uint()
        ok = self._lib.XGetGeometry(
            self._display, xid, ctypes.byref(root_ret),
            ctypes.byref(x), ctypes.byref(y), ctypes.byref(w), ctypes.byref(h),
            ctypes.byref(border), ctypes.byref(depth),
        )
        if not ok:
            return None
        abs_x, abs_y = ctypes.c_int(), ctypes.c_int()
        child = ctypes.c_ulong()
        if self._lib.XTranslateCoordinates(
            self._display, xid, self._root, 0, 0,
            ctypes.byref(abs_x), ctypes.byref(abs_y), ctypes.byref(child),
        ):
            return int(abs_x.value), int(abs_y.value), int(w.value), int(h.value)
        return int(x.value), int(y.value), int(w.value), int(h.value)

    def move(self, xid: int, x: int, y: int) -> None:
        if not self.ok or not xid:
            return
        self._lib.XMoveWindow(self._display, xid, int(x), int(y))
        self._lib.XSync(self._display, False)
