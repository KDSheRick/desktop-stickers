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


# ---------------------------------------------------------------- EWMH 结构体
#
# 让悬浮窗（灵动岛）可以「置顶 / 不进任务栏 / 不抢键盘焦点」，并读取工作区。
# 都是尽力而为：失败时静默忽略，不影响窗口主体功能。

_XA_CARDINAL = 6
_CLIENT_MESSAGE = 33
_SUBSTRUCTURE_NOTIFY = 1 << 19
_SUBSTRUCTURE_REDIRECT = 1 << 20
_INPUT_HINT = 1


class _XClientMessage(ctypes.Structure):
    """XClientMessageEvent 的有效字段（XEvent 实际 192 字节，用缓冲区兜底）。"""

    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("message_type", ctypes.c_ulong),
        ("format", ctypes.c_int),
        ("data", ctypes.c_long * 5),
    ]


class _XWMHints(ctypes.Structure):
    """XWMHints（Xlib 结构，只用到 input 标志）。"""

    _fields_ = [
        ("flags", ctypes.c_long),
        ("input", ctypes.c_int),
        ("initial_state", ctypes.c_int),
        ("icon_pixmap", ctypes.c_ulong),
        ("icon_window", ctypes.c_ulong),
        ("icon_x", ctypes.c_int),
        ("icon_y", ctypes.c_int),
        ("icon_mask", ctypes.c_ulong),
        ("window_group", ctypes.c_ulong),
    ]


class _XRectangle(ctypes.Structure):
    """XRectangle（XShape 输入区域用）。"""

    _fields_ = [
        ("x", ctypes.c_short),
        ("y", ctypes.c_short),
        ("width", ctypes.c_ushort),
        ("height", ctypes.c_ushort),
    ]


# ---------------------------------------------------------------- 移动器

class X11Mover:
    """通过 libX11 移动窗口、读取几何信息，并做少量 EWMH 设置。"""

    def __init__(self) -> None:
        self.ok = False
        self._lib = None
        self._display = None
        self._root = 0
        self._atoms: dict[str, int] = {}
        self._xext = None
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
            # EWMH / WM 辅助：置顶、窗口类型、不抢焦点、读工作区
            lib.XInternAtom.restype = ctypes.c_ulong
            lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
            lib.XGetWindowProperty.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
                ctypes.c_long, ctypes.c_long, ctypes.c_int, ctypes.c_ulong,
                ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
            ]
            lib.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                                       ctypes.c_long, ctypes.c_void_p]
            lib.XChangeProperty.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
                ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
            lib.XSetWMHints.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]
            lib.XGetWMHints.restype = ctypes.c_void_p
            lib.XGetWMHints.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            lib.XFree.argtypes = [ctypes.c_void_p]

            # XShape（设置输入区域）：在 libXext 里
            xext_name = ctypes.util.find_library("Xext")
            if xext_name:
                xext = ctypes.CDLL(xext_name)
                xext.XShapeCombineRectangles.argtypes = [
                    ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                    ctypes.c_int, ctypes.c_int,
                    ctypes.POINTER(_XRectangle), ctypes.c_int,
                    ctypes.c_int, ctypes.c_int]
                self._xext = xext

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

    # ------------------------------------------------------------ EWMH 辅助

    def _atom(self, name: str) -> int:
        if not self.ok:
            return 0
        if name not in self._atoms:
            self._atoms[name] = int(self._lib.XInternAtom(self._display, name.encode(), False))
        return self._atoms[name]

    def workarea(self) -> tuple[int, int, int, int] | None:
        """读取 _NET_WORKAREA 第一块（x, y, w, h，已避开 GNOME 顶栏 / 停靠栏）。"""
        if not self.ok:
            return None
        actual_type = ctypes.c_ulong()
        actual_format = ctypes.c_int()
        nitems = ctypes.c_ulong()
        after = ctypes.c_ulong()
        prop = ctypes.POINTER(ctypes.c_ubyte)()
        status = self._lib.XGetWindowProperty(
            self._display, self._root, self._atom("_NET_WORKAREA"),
            0, 4, False, _XA_CARDINAL,
            ctypes.byref(actual_type), ctypes.byref(actual_format),
            ctypes.byref(nitems), ctypes.byref(after), ctypes.byref(prop))
        if status != 0 or not prop:
            return None
        try:
            if nitems.value < 4:
                return None
            values = ctypes.cast(prop, ctypes.POINTER(ctypes.c_ulong))
            return (int(values[0]), int(values[1]), int(values[2]), int(values[3]))
        finally:
            self._lib.XFree(prop)

    def _send_state(self, xid: int, action: int, atoms: list[int]) -> None:
        """发送 _NET_WM_STATE 客户端消息（action：0 移除 / 1 添加 / 2 切换）。"""
        if not self.ok or not xid:
            return
        buf = ctypes.create_string_buffer(192)  # XEvent 大小，防止 Xlib 读越界
        event = ctypes.cast(buf, ctypes.POINTER(_XClientMessage)).contents
        event.type = _CLIENT_MESSAGE
        event.serial = 0
        event.send_event = 1
        event.display = self._display
        event.window = xid
        event.message_type = self._atom("_NET_WM_STATE")
        event.format = 32
        event.data[0] = action
        event.data[1] = atoms[0] if atoms else 0
        event.data[2] = atoms[1] if len(atoms) > 1 else 0
        event.data[3] = 1  # 来源：普通应用
        self._lib.XSendEvent(self._display, self._root, False,
                             _SUBSTRUCTURE_NOTIFY | _SUBSTRUCTURE_REDIRECT,
                             ctypes.byref(event))
        self._lib.XFlush(self._display)

    def set_above(self, xid: int, above: bool = True) -> None:
        """窗口置顶（_NET_WM_STATE_ABOVE）。"""
        self._send_state(xid, 1 if above else 0, [self._atom("_NET_WM_STATE_ABOVE")])

    def skip_taskbar(self, xid: int) -> None:
        """不出现在任务栏 / 工作区切换器。"""
        self._send_state(xid, 1, [self._atom("_NET_WM_STATE_SKIP_TASKBAR"),
                                  self._atom("_NET_WM_STATE_SKIP_PAGER")])

    def set_window_type(self, xid: int, kind: str = "dock") -> None:
        """设置 _NET_WM_WINDOW_TYPE（dock 让窗口默认浮在普通窗口之上，且不抢焦点）。"""
        if not self.ok or not xid:
            return
        atom = self._atom("_NET_WM_WINDOW_TYPE_" + kind.upper())
        value = ctypes.c_ulong(atom)
        self._lib.XChangeProperty(
            self._display, xid, self._atom("_NET_WM_WINDOW_TYPE"), self._atom("ATOM"),
            32, 0, ctypes.byref(value), 1)
        self._lib.XFlush(self._display)

    def set_input_hint(self, xid: int, enabled: bool = False) -> None:
        """WM_HINTS 的 input 标志：False = 点击窗口时 WM 不把键盘焦点给它。"""
        if not self.ok or not xid:
            return
        existing = self._lib.XGetWMHints(self._display, xid)
        hints = _XWMHints()
        if existing:
            hints = ctypes.cast(existing, ctypes.POINTER(_XWMHints)).contents
        hints.flags = hints.flags | _INPUT_HINT
        hints.input = 1 if enabled else 0
        self._lib.XSetWMHints(self._display, xid, ctypes.byref(hints))
        if existing:
            self._lib.XFree(existing)
        self._lib.XFlush(self._display)

    def set_input_shape(self, xid: int, rects: list[tuple[int, int, int, int]]) -> None:
        """XShape 输入区域：只有列出的矩形接收鼠标事件，其余区域点击穿透。

        直接调 libXext 而不是 Gdk.Surface.set_input_region，
        因为后者在没装 python3-gi-cairo 时不可用（PyGObject 缺 cairo 外部类型）。
        """
        if not self.ok or not xid or self._xext is None:
            return
        array = (_XRectangle * max(1, len(rects)))()
        for index, (x, y, width, height) in enumerate(rects):
            array[index].x = int(round(x))
            array[index].y = int(round(y))
            array[index].width = max(1, int(round(width)))
            array[index].height = max(1, int(round(height)))
        # ShapeInput=2, ShapeSet=0, Unsorted=0
        self._xext.XShapeCombineRectangles(
            self._display, xid, 2, 0, 0, array, len(rects), 0, 0)
        self._lib.XFlush(self._display)
