#!/usr/bin/env python3
"""桌面灵动岛（macOS Dynamic Island 风格 · GTK4 原型）。

顶部居中的纯黑胶囊：收起时显示时间（播放音乐时显示封面 + 跳动波形），
鼠标悬停展开、移开约 1.5 秒后收起，点击可以「钉住」保持展开。

展开态内容（三合一）：
  - 音乐：封面 / 歌名 / 歌手 / 可拖动进度条 / 上一曲 · 播放 · 下一曲 / 当前歌词
  - 时钟：时间 + 日期
  - API 用量：今日花费 / tokens / 近 7 天柱状图 / 厂商余额

实现要点：
  - 窗口是「固定大小 + 透明」的，胶囊在窗口内部自绘并逐帧改变大小 / 位置，
    再把 X11 输入区域收成胶囊本身，因此胶囊之外的点击会穿透到桌面；
  - 通过 XWayland 定位到顶部居中，并尽力置顶 / 不进任务栏 / 不抢键盘焦点；
  - 复用项目现有模块：collectors（MPRIS）、lyrics（歌词）、covers（封面）、
    api_usage（用量 / 余额）、positioner（X11 定位）、widgets（进度条 / 柱状图）。

运行：
    python3 island.py                     # 正常模式（跟随真实播放器）
    python3 island.py --demo              # 演示模式：假音乐 + 假数据，方便看效果
    python3 island.py --state expanded-music --snapshot docs/island.png   # 渲染截图
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time

# ---- 与贴纸一致：默认走 XWayland，才能给悬浮窗定位并置顶 ----
from positioner import x11_available  # noqa: E402

_backend = os.environ.get("STICKERS_BACKEND", "").lower()
if _backend in ("x11", "wayland"):
    os.environ["GDK_BACKEND"] = _backend
elif os.environ.get("DISPLAY") and x11_available():
    os.environ["GDK_BACKEND"] = "x11"

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gsk", "4.0")
from gi.repository import Gdk, Gio, GLib, Graphene, Gsk, Gtk, Pango  # noqa: E402

import api_usage  # noqa: E402
import covers  # noqa: E402
import lyrics  # noqa: E402
import settings  # noqa: E402
from collectors import MusicWatcher, fmt_clock  # noqa: E402
from positioner import X11Mover, xid_of  # noqa: E402
from widgets import ApiBars, SeekBar, _rgba, _rounded_rect  # noqa: E402

APP_ID = "com.loong.DynamicIsland"
OBJECT_PATH = "/" + APP_ID.replace(".", "/")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSS_FILE = os.path.join(BASE_DIR, "island.css")

# 开关状态（开机自启会读它；在应用菜单 / 贴纸右键菜单里切换也会更新）
STATE_FILE = os.path.join(settings.CONFIG_DIR, "island.json")
LOG_FILE = os.path.join(os.path.expanduser("~"), ".cache", "sysstickers", "island.log")

# ---- 几何参数（逻辑像素，X11 里会按缩放因子换算成物理像素）----
PAD = 18             # 窗口内留白（给阴影）
W_IDLE = 96          # 收起态宽度：无播放，显示时间
W_MUSIC = 124        # 收起态宽度：播放中，封面 + 波形
H_COLLAPSED = 34
W_EXPANDED = 424
H_EXPANDED = 172
GAP_BELOW_PANEL = 8  # 距 GNOME 顶栏下沿
COLLAPSE_DELAY_MS = 1500
TICK_MS = 100

_WEEKDAYS = "一二三四五六日"

# 演示模式的假歌词（自编占位内容，仅为展示排版）
DEMO_LINES = [
    {"t": 0.0, "text": "夜色漫过城市的天际线"},
    {"t": 8.0, "text": "灯光像星群落进海面"},
    {"t": 16.0, "text": "我把耳机调到最大声"},
    {"t": 24.0, "text": "让鼓点盖过所有杂念"},
    {"t": 32.0, "text": "风穿过街道 吹散了疲倦"},
    {"t": 40.0, "text": "此刻世界只剩下音乐"},
    {"t": 48.0, "text": "跟着节拍 走自己的路线"},
    {"t": 56.0, "text": "把每一秒都过成夏天"},
    {"t": 64.0, "text": "星光落在肩上是答案"},
    {"t": 72.0, "text": "我们在夜里放声歌唱"},
]

DEMO_USAGE = {
    "today_cost": 0.42, "today_tokens": 12300, "today_messages": 37,
    "total_cost": 12.40, "total_tokens": 3_400_000,
    "daily": [0.35, 1.20, 0.42, 2.10, 0.86, 1.62, 0.42],
}
DEMO_BALANCES = [{"id": "deepseek", "label": "DeepSeek", "balance": 8.07,
                  "currency": "CNY", "error": None}]


def _fmt_cost(value: float) -> str:
    value = float(value or 0.0)
    if value >= 0.01 or value <= 0:
        return f"${value:.2f}"
    return f"${value:.4f}"


def _debug(message: str) -> None:
    if os.environ.get("ISLAND_DEBUG"):
        print(f"[island {time.strftime('%H:%M:%S')}] {message}", flush=True)


def _set_text(label: Gtk.Label, text: str) -> None:
    if label.get_text() != text:
        label.set_text(text)


def _mpris_call(bus_name: str, method: str, params: GLib.Variant | None = None) -> None:
    """给播放器发一条 MPRIS 指令（异步，不阻塞界面）。"""
    if not bus_name:
        return
    try:
        conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        conn.call(
            bus_name, "/org/mpris/MediaPlayer2", "org.mpris.MediaPlayer2.Player",
            method, params, None, Gio.DBusCallFlags.NONE, 800, None, None)
    except GLib.Error:
        pass


# ---------------------------------------------------------------- 进程控制
#
# 供应用菜单 / 开机自启使用（单实例由 GApplication 保证）：
#   island.py --status / --start / --stop / --toggle / --autostart

def _bus_has_owner() -> bool:
    """灵动岛是否在运行（看 session bus 上有没有注册这个名字）。"""
    try:
        conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = conn.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "NameHasOwner", GLib.Variant("(s)", (APP_ID,)), None,
            Gio.DBusCallFlags.NONE, 500, None)
        return bool(reply.unpack()[0])
    except GLib.Error:
        return False


def _request_quit() -> None:
    """请正在运行的实例退出（走应用内的 quit 动作）。"""
    try:
        conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        conn.call_sync(APP_ID, OBJECT_PATH, "org.gtk.Actions", "Activate",
                       GLib.Variant("(sava{sv})", ("quit", [], {})), None,
                       Gio.DBusCallFlags.NONE, 800, None)
    except GLib.Error:
        pass


def _spawn() -> None:
    """后台启动一个实例（输出追加到日志文件）。"""
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        log = open(LOG_FILE, "ab")
    except OSError:
        log = subprocess.DEVNULL
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__)],
        stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)


def set_enabled(enabled: bool) -> None:
    """记住开关状态；开机自启读取它（关掉后重启系统不会自己回来）。"""
    try:
        os.makedirs(settings.CONFIG_DIR, exist_ok=True)
        with open(STATE_FILE + ".tmp", "w", encoding="utf-8") as fh:
            json.dump({"enabled": bool(enabled)}, fh)
        os.replace(STATE_FILE + ".tmp", STATE_FILE)
    except OSError:
        pass


def is_enabled() -> bool:
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return bool(json.load(fh).get("enabled", True))
    except (OSError, ValueError, AttributeError):
        return True


# ---------------------------------------------------------------- 自绘部件

class CoverArt(Gtk.Widget):
    """圆角封面缩略图（自定义圆角）；没有封面时显示占位音符。"""

    __gtype_name__ = "IslandCoverArt"

    def __init__(self, size: int, radius: float):
        super().__init__()
        self.set_size_request(size, size)
        self._size = float(size)
        self._radius = float(radius)
        self._texture = None
        self._layout = None

    def set_texture(self, texture) -> None:
        self._texture = texture
        self.queue_draw()

    def do_snapshot(self, snapshot) -> None:  # noqa: N802 (GTK 虚函数命名)
        size = self._size
        bounds = Graphene.Rect()
        bounds.init(0, 0, size, size)
        rounded = Gsk.RoundedRect()
        rounded.init_from_rect(bounds, self._radius)
        snapshot.push_rounded_clip(rounded)

        if self._texture is not None:
            snapshot.append_texture(self._texture, bounds)
        else:
            builder = Gsk.PathBuilder()
            _rounded_rect(builder, 0, 0, size, size, self._radius)
            snapshot.append_fill(builder.to_path(), Gsk.FillRule.WINDING, _rgba((1.0, 1.0, 1.0), 0.10))
            if self._layout is None:
                self._layout = self.create_pango_layout("♪")
                attrs = Pango.AttrList()
                attrs.insert(Pango.attr_size_new_absolute(int(size * 0.46) * Pango.SCALE))
                self._layout.set_attributes(attrs)
            width, height = self._layout.get_pixel_size()
            point = Graphene.Point()
            point.init((size - width) / 2.0, (size - height) / 2.0)
            snapshot.save()
            snapshot.translate(point)
            snapshot.append_layout(self._layout, _rgba((1.0, 1.0, 1.0), 0.55))
            snapshot.restore()

        snapshot.pop()


class EqBars(Gtk.Widget):
    """播放中的跳动小波形（4 根圆角竖条）。"""

    __gtype_name__ = "IslandEqBars"

    def __init__(self, width: int = 20, height: int = 14, bars: int = 4):
        super().__init__()
        self.set_size_request(width, height)
        self._width = float(width)
        self._height = float(height)
        self._bars = bars
        self._phase = 0.0
        self._playing = False

    def set_playing(self, playing: bool) -> None:
        if playing != self._playing:
            self._playing = playing
            self.queue_draw()

    def set_phase(self, phase: float) -> None:
        if not self._playing:
            return
        self._phase = phase
        self.queue_draw()

    def do_snapshot(self, snapshot) -> None:  # noqa: N802
        gap = 3.0
        bar_w = (self._width - gap * (self._bars - 1)) / self._bars
        for index in range(self._bars):
            if self._playing:
                level = 0.30 + 0.70 * (0.5 + 0.5 * math.sin(self._phase * 2.6 + index * 1.15))
            else:
                level = (0.34, 0.58, 0.42, 0.66)[index % 4]
            bar_h = max(3.0, self._height * level)
            x = index * (bar_w + gap)
            y = self._height - bar_h
            builder = Gsk.PathBuilder()
            _rounded_rect(builder, x, y, bar_w, bar_h, bar_w / 2.0)
            snapshot.append_fill(builder.to_path(), Gsk.FillRule.WINDING, _rgba((1.0, 1.0, 1.0), 0.92))


# ---------------------------------------------------------------- 灵动岛窗口

class IslandView(Gtk.ApplicationWindow):
    """灵动岛本体：固定大小的透明窗口 + 内部逐帧变形的黑色胶囊。"""

    def __init__(self, app: Gtk.Application, music_on: bool = False, expanded: bool = False):
        super().__init__(application=app, title="灵动岛")
        self.add_css_class("island-window")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_can_focus(False)

        # ---- 数据状态 ----
        self.music_on = music_on
        self.status = "Playing" if music_on else "Stopped"
        self.lines: list[dict] = []
        self.position = 0.0
        self.length = 0.0
        self.sampled_at = time.monotonic()
        self.bus_name = ""
        self.track_id = ""
        self.can_next = True
        self.can_prev = True
        self.scrubbing = False
        self.usage: dict = {}
        self.balances: list[dict] = []
        self._title = ""
        self._artist = ""
        self._album = ""

        # ---- 交互状态 ----
        self.expanded = expanded
        self.pinned = expanded
        self.hover = False
        self.menu_open = False
        self.mover = None  # 由 IslandApp 注入（X11Mover）
        self._collapse_source = 0
        self._morph_source = 0
        self._view_key: tuple | None = None
        self._pos_now = 0.0

        # ---- 固定尺寸的透明窗口 ----
        win_w = W_EXPANDED + 2 * PAD
        win_h = H_EXPANDED + 2 * PAD
        self.fixed = Gtk.Fixed()
        self.fixed.set_size_request(win_w, win_h)
        self.set_child(self.fixed)

        self.pill = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.pill.add_css_class("island")
        self.pill.set_overflow(Gtk.Overflow.HIDDEN)
        self.fixed.put(self.pill, 0, 0)

        self._build_views()
        self._cur_w, self._cur_h = self._size_for((expanded, music_on))
        self._apply_geometry(self._cur_w, self._cur_h)
        self._swap_view((expanded, music_on), 1.0)

        # ---- 悬停 / 点击 / 右键菜单 ----
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_: self._set_hover(True))
        motion.connect("leave", lambda *_: self._set_hover(False))
        if os.environ.get("ISLAND_DEBUG"):
            motion.connect("motion", lambda _c, x, y: _debug(f"motion {x:.0f},{y:.0f}"))
        self.pill.add_controller(motion)

        click = Gtk.GestureClick()
        click.connect("pressed", self._on_pressed)
        self.pill.add_controller(click)

        self._menu: Gtk.PopoverMenu | None = None
        self.connect("map", self._on_mapped)

    # ------------------------------------------------------------ 构建界面

    def _button(self, icon_name: str, css_class: str, pixel: int, callback) -> Gtk.Button:
        button = Gtk.Button()
        button.add_css_class(css_class)
        button.set_valign(Gtk.Align.CENTER)
        image = Gtk.Image.new_from_icon_name(icon_name)
        image.set_pixel_size(pixel)
        button.set_child(image)
        button.connect("clicked", lambda _button: callback())
        return button

    @staticmethod
    def _muted_label(text: str = "", css: str = "island-sub") -> Gtk.Label:
        label = Gtk.Label(label=text)
        label.add_css_class(css)
        label.set_halign(Gtk.Align.START)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        return label

    def _build_views(self) -> None:
        self._views: dict[tuple, Gtk.Widget] = {}

        # ---- 收起 · 无播放：时间 ----
        mini_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        mini_row.add_css_class("island-collapsed")
        mini_row.set_halign(Gtk.Align.CENTER)
        mini_row.set_valign(Gtk.Align.CENTER)
        self.label_mini = Gtk.Label(label="--:--")
        self.label_mini.add_css_class("island-mini")
        mini_row.append(self.label_mini)
        self._views[(False, False)] = mini_row

        # ---- 收起 · 播放中：封面 + 波形 ----
        music_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        music_row.add_css_class("island-collapsed")
        music_row.set_halign(Gtk.Align.CENTER)
        music_row.set_valign(Gtk.Align.CENTER)
        self.mini_cover = CoverArt(22, 6)
        self.eq = EqBars(20, 14)
        music_row.append(self.mini_cover)
        music_row.append(self.eq)
        self._views[(False, True)] = music_row

        self._views[(True, True)] = self._build_music_view()
        self._views[(True, False)] = self._build_idle_view()

    def _build_music_view(self) -> Gtk.Widget:
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
        body.add_css_class("island-expanded")
        body.set_vexpand(True)
        body.set_valign(Gtk.Align.CENTER)

        # 第一行：封面 + 歌名 / 歌手 + 控制按钮
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.cover = CoverArt(54, 14)

        meta = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        meta.set_hexpand(True)
        meta.set_valign(Gtk.Align.CENTER)
        self.title = Gtk.Label(label="暂无播放")
        self.title.add_css_class("island-title")
        self.title.set_halign(Gtk.Align.START)
        self.title.set_ellipsize(Pango.EllipsizeMode.END)
        self.artist = self._muted_label()
        meta.append(self.title)
        meta.append(self.artist)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        controls.set_valign(Gtk.Align.CENTER)
        self.btn_prev = self._button("media-skip-backward-symbolic", "island-btn", 15, self._on_prev)
        self.btn_play = self._button("media-playback-start-symbolic", "island-play", 15, self._on_play)
        self.btn_next = self._button("media-skip-forward-symbolic", "island-btn", 15, self._on_next)
        self._play_image: Gtk.Image = self.btn_play.get_child()
        controls.append(self.btn_prev)
        controls.append(self.btn_play)
        controls.append(self.btn_next)

        top.append(self.cover)
        top.append(meta)
        top.append(controls)

        # 第二行：进度条 + 时间
        seek_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        seek_row.set_valign(Gtk.Align.CENTER)
        self.pos_label = Gtk.Label(label="--:--")
        self.pos_label.add_css_class("island-foot")
        self.pos_label.set_size_request(38, -1)
        self.pos_label.set_xalign(1.0)
        self.len_label = Gtk.Label(label="--:--")
        self.len_label.add_css_class("island-foot")
        self.len_label.set_size_request(38, -1)
        seek_w = W_EXPANDED - 2 * 16 - 2 * 38 - 3 * 8
        self.seek = SeekBar(int(seek_w), 16, self._on_scrub, self._on_seek)
        seek_row.append(self.pos_label)
        seek_row.append(self.seek)
        seek_row.append(self.len_label)

        # 第三行：歌词
        lyric_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        icon = Gtk.Label(label="♪")
        icon.add_css_class("island-lyric-icon")
        self.lyric = Gtk.Label(label="")
        self.lyric.add_css_class("island-lyric")
        self.lyric.set_halign(Gtk.Align.START)
        self.lyric.set_hexpand(True)
        self.lyric.set_ellipsize(Pango.EllipsizeMode.END)
        lyric_row.append(icon)
        lyric_row.append(self.lyric)

        # 第四行：时钟 + API 用量 / 余额
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.foot_clock = Gtk.Label(label="")
        self.foot_clock.add_css_class("island-foot")
        self.foot_usage = Gtk.Label(label="")
        self.foot_usage.add_css_class("island-foot")
        self.foot_usage.set_hexpand(True)
        self.foot_usage.set_halign(Gtk.Align.END)
        self.foot_balance = Gtk.Label(label="")
        self.foot_balance.add_css_class("island-balance")
        self.foot_balance.set_halign(Gtk.Align.END)
        footer.append(self.foot_clock)
        footer.append(self.foot_usage)
        footer.append(self.foot_balance)

        body.append(top)
        body.append(seek_row)
        body.append(lyric_row)
        body.append(footer)
        return body

    def _build_idle_view(self) -> Gtk.Widget:
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.add_css_class("island-expanded")
        body.set_vexpand(True)
        body.set_valign(Gtk.Align.CENTER)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        left.set_hexpand(True)
        left.set_valign(Gtk.Align.CENTER)
        self.big_time = Gtk.Label(label="--:--")
        self.big_time.add_css_class("island-clock")
        self.big_time.set_halign(Gtk.Align.START)
        self.big_date = Gtk.Label(label="")
        self.big_date.add_css_class("island-date")
        self.big_date.set_halign(Gtk.Align.START)
        left.append(self.big_time)
        left.append(self.big_date)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        right.set_valign(Gtk.Align.CENTER)
        self.api_today = Gtk.Label(label="今日 —")
        self.api_today.add_css_class("island-api-today")
        self.api_today.set_halign(Gtk.Align.END)
        self.api_tokens = self._muted_label()
        self.api_tokens.set_halign(Gtk.Align.END)
        self.api_total = self._muted_label()
        self.api_total.set_halign(Gtk.Align.END)
        right.append(self.api_today)
        right.append(self.api_tokens)
        right.append(self.api_total)

        top.append(left)
        top.append(right)

        bars_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        bars_label = Gtk.Label(label="近 7 天")
        bars_label.add_css_class("island-foot")
        bars_label.set_valign(Gtk.Align.CENTER)
        self.bars = ApiBars(240, 30)
        bars_row.append(bars_label)
        bars_row.append(self.bars)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.idle_balance = Gtk.Label(label="")
        self.idle_balance.add_css_class("island-balance")
        self.idle_balance.set_halign(Gtk.Align.START)
        footer.append(self.idle_balance)

        body.append(top)
        body.append(bars_row)
        body.append(footer)
        return body

    # ------------------------------------------------------------ 几何 / 变形

    def _size_for(self, key: tuple) -> tuple[float, float]:
        expanded, music = key
        if expanded:
            return float(W_EXPANDED), float(H_EXPANDED)
        return float(W_MUSIC if music else W_IDLE), float(H_COLLAPSED)

    def _apply_geometry(self, width: float, height: float) -> None:
        self._cur_w, self._cur_h = width, height
        x = PAD + (W_EXPANDED - width) / 2.0
        self.pill.set_size_request(int(round(width)), int(round(height)))
        self.fixed.move(self.pill, int(round(x)), PAD)
        self._update_input_region(x, float(PAD), width, height)

    def _update_input_region(self, x: float, y: float, width: float, height: float) -> None:
        """把窗口输入区域收成胶囊本身，胶囊之外点击穿透到桌面（XShape）。"""
        mover = getattr(self, "mover", None)
        if mover is None or not mover.ok:
            return
        xid = xid_of(self)
        if not xid:
            return
        scale = float(self.get_scale_factor())
        rect = (int(round(x * scale)), int(round(y * scale)),
                int(round(width * scale)), int(round(height * scale)))
        try:
            if os.environ.get("ISLAND_DEBUG") and rect != getattr(self, "_last_shape", None):
                self._last_shape = rect
                _debug(f"input shape -> {rect}")
            mover.set_input_shape(xid, [rect])
        except Exception as exc:  # noqa: BLE001 - 不同后端能力不同，失败就退化为整窗可点
            if not getattr(self, "_input_region_warned", False):
                self._input_region_warned = True
                print(f"提示：输入区域设置失败（{exc}），胶囊外区域也会响应鼠标。", file=sys.stderr)

    def _swap_view(self, key: tuple, opacity: float = 1.0) -> None:
        view = self._views[key]
        current = self.pill.get_first_child()
        if current is not view:
            if current is not None:
                self.pill.remove(current)
            self.pill.append(view)
        self._view_key = key
        view.set_opacity(opacity)

    def _morph(self, to_expanded: bool, duration: int = 280) -> None:
        """展开 / 收起：逐帧改变胶囊尺寸，中途淡出旧内容、淡入新内容。"""
        target_key = (to_expanded, self.music_on)
        to_w, to_h = self._size_for(target_key)
        from_w, from_h = self._cur_w, self._cur_h
        self._cancel_morph()
        _debug(f"morph expanded={to_expanded} {from_w:.0f}x{from_h:.0f} -> {to_w:.0f}x{to_h:.0f}")

        if abs(from_w - to_w) < 0.5 and abs(from_h - to_h) < 0.5:
            self._swap_view(target_key, 1.0)
            return

        start = time.monotonic()
        state = {"swapped": False}

        def tick() -> bool:
            t = min(1.0, (time.monotonic() - start) * 1000.0 / duration)
            eased = 1.0 - (1.0 - t) ** 3
            self._apply_geometry(from_w + (to_w - from_w) * eased,
                                 from_h + (to_h - from_h) * eased)
            child = self.pill.get_first_child()
            if t < 0.35:
                if child is not None:
                    child.set_opacity(max(0.0, 1.0 - t / 0.35))
            else:
                if not state["swapped"]:
                    state["swapped"] = True
                    self._swap_view(target_key, 0.0)
                    child = self.pill.get_first_child()
                if child is not None:
                    child.set_opacity(min(1.0, (t - 0.35) / 0.5))
            if t >= 1.0:
                self._morph_source = 0
                self._apply_geometry(to_w, to_h)
                self._swap_view(target_key, 1.0)
                return False
            return True

        self._morph_source = GLib.timeout_add(15, tick)

    def _cancel_morph(self) -> None:
        if self._morph_source:
            GLib.source_remove(self._morph_source)
            self._morph_source = 0

    # ------------------------------------------------------------ 交互

    def _set_hover(self, hovering: bool) -> None:
        self.hover = hovering
        _debug(f"hover={hovering}")
        if hovering:
            self._cancel_collapse()
            self._morph(True)
        else:
            self._schedule_collapse()

    def _schedule_collapse(self, delay: int = COLLAPSE_DELAY_MS) -> None:
        self._cancel_collapse()
        if self.pinned or self.menu_open:
            return
        self._collapse_source = GLib.timeout_add(delay, self._do_collapse)

    def _cancel_collapse(self) -> None:
        if self._collapse_source:
            GLib.source_remove(self._collapse_source)
            self._collapse_source = 0

    def _do_collapse(self) -> bool:
        self._collapse_source = 0
        _debug(f"collapse timer: pinned={self.pinned} hover={self.hover} menu={self.menu_open}")
        if not self.pinned and not self.hover and not self.menu_open:
            self._morph(False)
        return GLib.SOURCE_REMOVE

    def _on_pressed(self, gesture: Gtk.GestureClick, _n_press: int, x: float, y: float) -> None:
        button = gesture.get_current_button()
        if button == 3:
            self._popup_menu(x, y)
            return
        if button != 1 or _n_press != 1:
            return
        # 单击胶囊空白处：钉住 / 取消钉住
        self.pinned = not self.pinned
        if self.pinned:
            self._cancel_collapse()
            self._morph(True)
        else:
            self._schedule_collapse(600)

    def _popup_menu(self, x: float, y: float) -> None:
        if self._menu is None:
            menu = Gio.Menu()
            menu.append("打开贴纸设置…", "app.open-control")
            menu.append("关闭灵动岛", "app.quit")
            self._menu = Gtk.PopoverMenu.new_from_model(menu)
            self._menu.set_parent(self.pill)
            self._menu.connect("show", self._on_menu_show)
            self._menu.connect("closed", self._on_menu_closed)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        self._menu.set_pointing_to(rect)
        self._menu.popup()

    def _on_menu_show(self, _popover) -> None:
        self.menu_open = True
        self._cancel_collapse()
        self._morph(True)

    def _on_menu_closed(self, _popover) -> None:
        self.menu_open = False
        self._schedule_collapse(400)

    # ------------------------------------------------------------ 播放控制

    def _on_play(self) -> None:
        _mpris_call(self.bus_name, "PlayPause")
        self.status = "Paused" if self.status == "Playing" else "Playing"
        self.sampled_at = time.monotonic()
        self._refresh_labels()

    def _on_prev(self) -> None:
        _mpris_call(self.bus_name, "Previous")

    def _on_next(self) -> None:
        _mpris_call(self.bus_name, "Next")

    def _on_scrub(self, fraction: float | None) -> None:
        if not self.music_on or self.length <= 0:
            return
        self.scrubbing = fraction is not None
        if fraction is None:
            self._refresh_labels()
        else:
            _set_text(self.pos_label, fmt_clock(fraction * self.length))

    def _on_seek(self, fraction: float) -> None:
        if not self.bus_name or self.length <= 0:
            return
        target = max(0.0, min(1.0, fraction)) * self.length
        delta = int((target - self._pos_now) * 1_000_000)
        self.position = target
        self.sampled_at = time.monotonic()
        self.scrubbing = False
        if self.track_id:
            _mpris_call(self.bus_name, "SetPosition",
                        GLib.Variant("(ox)", (self.track_id, int(target * 1_000_000))))
        else:
            _mpris_call(self.bus_name, "Seek", GLib.Variant("(x)", (delta,)))
        self._refresh_labels()

    # ------------------------------------------------------------ 数据更新

    def set_music(self, music: dict | None) -> None:
        playing = bool(music) and music.get("status") != "Stopped"
        if playing:
            self.status = str(music.get("status") or "Playing")
            self.bus_name = str(music.get("bus_name") or "")
            self.track_id = str(music.get("track_id") or "")
            self.can_next = bool(music.get("can_go_next", True))
            self.can_prev = bool(music.get("can_go_previous", True))
            self._title = str(music.get("title") or "")
            self._artist = str(music.get("artist") or "")
            self._album = str(music.get("album") or "")
            if music.get("position") is not None:
                self.position = float(music["position"])
                self.sampled_at = time.monotonic()
            if music.get("length"):
                self.length = float(music["length"])
        else:
            self.status = "Stopped"
            self.bus_name = ""
            self.track_id = ""
            self.position = 0.0
            self.length = 0.0

        if playing != self.music_on:
            self.music_on = playing
            if not self._morph_source:
                self._morph(self.expanded)
        self._refresh_labels()

    def set_lyrics(self, lines: list[dict]) -> None:
        self.lines = lines or []
        self._refresh_labels()

    def set_cover(self, texture) -> None:
        self.cover.set_texture(texture)
        self.mini_cover.set_texture(texture)

    def set_api(self, usage: dict | None, balances: list[dict] | None) -> None:
        self.usage = usage or {}
        self.balances = balances or []
        self._refresh_labels()

    # ------------------------------------------------------------ 每帧刷新

    def tick(self) -> None:
        now = time.monotonic()
        position = self.position
        if self.music_on and self.status == "Playing" and not self.scrubbing:
            position += max(0.0, now - self.sampled_at)
        self._pos_now = position

        if self.music_on and self.status == "Playing":
            self.eq.set_playing(True)
            self.eq.set_phase(now * 2.4)
        else:
            self.eq.set_playing(False)

        if self.music_on and self.length > 0 and not self.scrubbing:
            self.seek.set_progress(min(1.0, position / self.length), (1.0, 1.0, 1.0))

        self._refresh_labels()

    def _refresh_labels(self) -> None:
        now = time.localtime()
        clock = time.strftime("%H:%M", now)
        _set_text(self.label_mini, clock)
        _set_text(self.foot_clock, clock)
        _set_text(self.big_time, clock)
        _set_text(self.big_date, f"{now.tm_mon}月{now.tm_mday}日 周{_WEEKDAYS[now.tm_wday]}")

        # ---- 音乐视图 ----
        if self.music_on:
            title = getattr(self, "_title", "")
            artist = getattr(self, "_artist", "")
            album = getattr(self, "_album", "")
            _set_text(self.title, title or "未知歌曲")
            subtitle = " · ".join(part for part in (artist, album) if part)
            _set_text(self.artist, subtitle or "未知艺术家")
            _set_text(self.pos_label, fmt_clock(self._pos_now))
            _set_text(self.len_label, fmt_clock(self.length))
            playing = self.status == "Playing"
            icon = "media-playback-pause-symbolic" if playing else "media-playback-start-symbolic"
            self._play_image.set_from_icon_name(icon)
            self.btn_prev.set_sensitive(self.can_prev)
            self.btn_next.set_sensitive(self.can_next)
            line = lyrics.line_at(self.lines, self._pos_now) if self.lines else ""
            if not line:
                line = " · ".join(part for part in (title, artist) if part)
            _set_text(self.lyric, line)
        else:
            _set_text(self.lyric, "")

        # ---- API 用量 ----
        usage = self.usage or {}
        today_cost = _fmt_cost(usage.get("today_cost", 0.0))
        tokens = api_usage.fmt_tokens(int(usage.get("today_tokens", 0)))
        messages = int(usage.get("today_messages", 0))
        _set_text(self.api_today, f"今日 {today_cost}")
        _set_text(self.api_tokens, f"{tokens} tokens · {messages} 条回复")
        _set_text(self.api_total, f"累计 {_fmt_cost(usage.get('total_cost', 0.0))} · "
                                  f"{api_usage.fmt_tokens(int(usage.get('total_tokens', 0)))} tokens")
        _set_text(self.foot_usage, f"今日 {today_cost} · {tokens} tok")
        self.bars.set_values(usage.get("daily") or [0.0])

        if self.balances:
            text = " · ".join(
                f"{item.get('label', '')} "
                f"{api_usage.fmt_balance(item.get('balance'), item.get('currency'))}"
                if not item.get("error") else f"{item.get('label', '')} —"
                for item in self.balances)
        else:
            text = ""
        _set_text(self.foot_balance, text)
        _set_text(self.idle_balance, text)

    # ------------------------------------------------------------ 窗口定位

    def _on_mapped(self, _window) -> None:
        self._apply_geometry(self._cur_w, self._cur_h)
        self._place_window()
        GLib.timeout_add(500, self._place_window)  # WM 处理完成后校正一次

    def _place_window(self) -> bool:
        xid = xid_of(self)
        if not self.mover.ok or not xid:
            return GLib.SOURCE_REMOVE
        scale = float(self.get_scale_factor())
        screen_w, _ = self.mover.screen_size()
        win_w = (W_EXPANDED + 2 * PAD) * scale
        workarea = self.mover.workarea()
        top = workarea[1] if workarea else 40 * scale
        x = max(0.0, (screen_w - win_w) / 2.0)
        y = top + GAP_BELOW_PANEL * scale
        self.mover.move(xid, int(x), int(y))
        _debug(f"place xid=0x{xid:x} at {int(x)},{int(y)} scale={scale:g} workarea={workarea}")
        return GLib.SOURCE_REMOVE

    def native_x11_setup(self) -> None:
        """置顶 / 不进任务栏 / 不抢键盘焦点（最佳努力）。"""
        xid = xid_of(self)
        if not self.mover.ok or not xid:
            return
        self.mover.set_window_type(xid, "dock")
        self.mover.set_above(xid, True)
        self.mover.skip_taskbar(xid)
        self.mover.set_input_hint(xid, False)

    # ------------------------------------------------------------ 截图（写文档用）

    def snapshot_to_png(self, path: str, scale: float = 2.0, margin: float = 16.0) -> None:
        """把「胶囊 + 阴影余地」渲染成 PNG（绕开无法使用的 cairo 外部类型）。"""
        win_w = W_EXPANDED + 2 * PAD
        win_h = H_EXPANDED + 2 * PAD
        pill_x = PAD + (W_EXPANDED - self._cur_w) / 2.0
        crop_x = max(0.0, pill_x - margin)
        crop_y = max(0.0, PAD - margin)
        crop_w = min(win_w - crop_x, self._cur_w + 2 * margin)
        crop_h = min(win_h - crop_y, self._cur_h + 2 * margin)

        paintable = Gtk.WidgetPaintable.new(self.fixed)
        snapshot = Gtk.Snapshot.new()
        snapshot.scale(scale, scale)
        offset = Graphene.Point()
        offset.init(-crop_x, -crop_y)
        snapshot.translate(offset)
        paintable.snapshot(snapshot, win_w, win_h)
        node = snapshot.to_node()
        if node is None:
            raise RuntimeError("没有可渲染的内容")

        renderer = Gsk.CairoRenderer.new()
        # Cairo 渲染器不需要 GdkSurface（传 None 即可）
        if not renderer.realize(None):
            raise RuntimeError("Cairo 渲染器初始化失败")
        try:
            # 注意：不能走 renderer.render(node, cairo_surface)——本机没装
            # python3-gi-cairo，PyGObject 无法转换 cairo 外部类型；
            # 改用 render_texture 直接拿 Gdk.Texture 再存 PNG。
            texture = renderer.render_texture(node, None)
            if texture is None:
                raise RuntimeError("渲染纹理失败")
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            texture.save_to_png(path)
        finally:
            renderer.unrealize()


# ---------------------------------------------------------------- 应用

class IslandApp(Gtk.Application):
    """数据管线：MPRIS 采样、封面 / 歌词异步加载、API 用量与余额。"""

    def __init__(self, demo: bool = False, state: str | None = None,
                 snapshot: str | None = None):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.demo = demo or state is not None
        self.force_state = state
        self.snapshot_path = snapshot

        self.mover = X11Mover()
        self.music_watcher = MusicWatcher()
        self.settings = settings.load()

        self.view: IslandView | None = None
        self._track_key: tuple | None = None
        self._cover_token = 0
        self._fetching_balances = False
        self._demo_t0 = time.monotonic()
        self._install_actions()

    # ------------------------------------------------------------ 生命周期

    def _install_actions(self) -> None:
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", self._on_quit)
        self.add_action(quit_action)

        control_action = Gio.SimpleAction.new("open-control", None)
        control_action.connect("activate", self._open_control)
        self.add_action(control_action)

    def _on_quit(self, *_args) -> None:
        """关闭 = 不再自启（与顶栏歌词插件一致，重启系统不会自己回来）。"""
        set_enabled(False)
        self.quit()

    @staticmethod
    def _open_control(*_args) -> None:
        try:
            subprocess.Popen([sys.executable, os.path.join(BASE_DIR, "control.py")],
                             start_new_session=True)
        except OSError:
            pass

    def do_activate(self) -> None:
        if self.view is not None:
            self.view.present()
            return

        music_on = self.force_state is None or "music" in self.force_state
        expanded = bool(self.force_state and self.force_state.startswith("expanded"))
        view = IslandView(self, music_on=music_on, expanded=expanded)
        view.mover = self.mover
        self.view = view
        self._load_css()
        view.present()
        GLib.timeout_add(300, self._post_map_setup)

        if self.demo:
            view.set_lyrics(DEMO_LINES if music_on else [])
            view.set_api(DEMO_USAGE, DEMO_BALANCES)
            texture = self._demo_cover_texture()
            if texture is not None:
                view.set_cover(texture)

        GLib.timeout_add(TICK_MS, self._on_tick)
        GLib.timeout_add(1000, self._on_second)

        if self.snapshot_path:
            GLib.timeout_add(1400, self._snapshot_and_quit)

    def _post_map_setup(self) -> bool:
        if self.view is not None:
            self.view.native_x11_setup()
            if self.view.expanded:
                self.view.pinned = True
                self.view._morph(True, duration=1)
        return GLib.SOURCE_REMOVE

    def _load_css(self) -> None:
        provider = Gtk.CssProvider()
        try:
            provider.load_from_file(Gio.File.new_for_path(CSS_FILE))  # GTK >= 4.12
        except AttributeError:  # pragma: no cover
            provider.load_from_path(CSS_FILE)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)

    def _snapshot_and_quit(self) -> bool:
        view = self.view
        if view is None or not self.snapshot_path:
            return GLib.SOURCE_REMOVE
        try:
            view.snapshot_to_png(self.snapshot_path)
            print(f"截图已保存: {self.snapshot_path}")
        except Exception as exc:  # noqa: BLE001
            print(f"截图失败: {exc}", file=sys.stderr)
        self.quit()
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------ 定时刷新

    def _on_tick(self) -> bool:
        if self.view is not None:
            self.view.tick()
        return GLib.SOURCE_CONTINUE

    def _on_second(self) -> bool:
        view = self.view
        if view is None:
            return GLib.SOURCE_CONTINUE

        if self.demo:
            music = self._demo_music() if (self.force_state is None or "music" in self.force_state) else None
        else:
            try:
                music = self.music_watcher.sample()
            except Exception:  # noqa: BLE001 - 采样失败不应影响界面
                music = None
        view.set_music(music)

        if music and not music.get("blocked") and not self.demo:
            key = (music.get("title"), music.get("artist"),
                   music.get("album"), music.get("art"))
            if key != self._track_key:
                self._track_key = key
                view.set_lyrics([])
                self._load_track_async(*key)

        if self.demo:
            view.set_api(DEMO_USAGE, DEMO_BALANCES)
        else:
            view.set_api(api_usage.opencode_usage(), api_usage.balances_cached())
            if api_usage.balances_expired() and not self._fetching_balances:
                self._fetch_balances_async()
        return GLib.SOURCE_CONTINUE

    # ------------------------------------------------------------ 异步加载

    def _load_track_async(self, title: str, artist: str, album: str, art: str) -> None:
        self._cover_token += 1
        token = self._cover_token

        def work() -> None:
            lines, cover_path = covers.load_track(title, artist, album, art or None)
            texture = None
            if cover_path:
                try:
                    texture = Gdk.Texture.new_from_filename(cover_path)
                except Exception:  # noqa: BLE001
                    texture = None
            GLib.idle_add(self._apply_track, token, lines, texture)

        threading.Thread(target=work, daemon=True).start()

    def _apply_track(self, token: int, lines, texture) -> bool:
        view = self.view
        if view is None or token != self._cover_token:
            return GLib.SOURCE_REMOVE
        view.set_lyrics(lines or [])
        if texture is not None:
            view.set_cover(texture)
        return GLib.SOURCE_REMOVE

    def _fetch_balances_async(self) -> None:
        providers = api_usage.load_providers(self.settings)
        self._fetching_balances = True
        if not providers:
            self._fetching_balances = False
            return

        def work() -> None:
            result = api_usage.fetch_balances(providers)
            GLib.idle_add(self._apply_balances, result)

        threading.Thread(target=work, daemon=True).start()

    def _apply_balances(self, balances) -> bool:
        self._fetching_balances = False
        if self.view is not None:
            self.view.set_api(api_usage.opencode_usage(), balances)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------ 演示数据

    def _demo_music(self) -> dict:
        length = 254.0
        position = (time.monotonic() - self._demo_t0) % length
        return {
            "player": "演示播放器", "bus_name": "", "status": "Playing",
            "title": "夜航星", "artist": "不才", "album": "三体 电视剧原声带",
            "art": "", "track_id": "", "can_go_next": True, "can_go_previous": True,
            "length": length, "position": position,
        }

    @staticmethod
    def _demo_cover_texture():
        path = os.path.join(tempfile.gettempdir(), "island-demo-cover.png")
        if not os.path.exists(path):
            try:
                from PIL import Image, ImageDraw

                size = 160
                image = Image.new("RGB", (size, size))
                draw = ImageDraw.Draw(image)
                top, bottom = (108, 92, 231), (0, 184, 169)
                for y in range(size):
                    t = y / (size - 1)
                    color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
                    draw.line([(0, y), (size, y)], fill=color)
                image.save(path)
            except Exception:  # noqa: BLE001 - 没有 Pillow 就显示占位音符
                return None
        try:
            return Gdk.Texture.new_from_filename(path)
        except Exception:  # noqa: BLE001
            return None


def main() -> int:
    parser = argparse.ArgumentParser(description="桌面灵动岛（GTK4 原型）")
    parser.add_argument("--demo", action="store_true", help="演示模式：假音乐 / 假数据")
    parser.add_argument("--state", choices=["collapsed-idle", "collapsed-music",
                                            "expanded-idle", "expanded-music"],
                        help="强制某个视觉状态（会自动进入演示模式）")
    parser.add_argument("--snapshot", metavar="FILE", help="渲染截图到文件后退出（配合 --state）")
    # 进程控制（应用菜单 / 开机自启使用）
    parser.add_argument("--status", action="store_true", help="查看运行状态（running / stopped）")
    parser.add_argument("--start", action="store_true", help="启动，并记住「开启」")
    parser.add_argument("--stop", action="store_true", help="关闭，并记住「关闭」")
    parser.add_argument("--toggle", action="store_true", help="开 / 关切换")
    parser.add_argument("--autostart", action="store_true", help="开机自启入口（尊重上次开关）")
    args = parser.parse_args()

    running = _bus_has_owner()
    if args.status:
        print("running" if running else "stopped")
        return 0
    if args.start:
        set_enabled(True)
        if not running:
            _spawn()
        return 0
    if args.stop:
        set_enabled(False)
        if running:
            _request_quit()
        return 0
    if args.toggle:
        if running:
            set_enabled(False)
            _request_quit()
            print("灵动岛：已关闭（重启系统后也不会自启）")
        else:
            set_enabled(True)
            _spawn()
            print("灵动岛：已开启")
        return 0
    if args.autostart:
        if is_enabled() and not running:
            _spawn()
        return 0

    snapshot = args.snapshot
    if snapshot and not args.state:
        args.state = "expanded-music"

    app = IslandApp(demo=args.demo, state=args.state, snapshot=snapshot)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    raise SystemExit(main())
