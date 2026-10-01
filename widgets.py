"""贴纸卡片组件：毛玻璃卡片 + GTK4 原生自绘仪表（圆环、进度条、迷你曲线）。

绘制走 Gsk（GPU 渲染，无需 cairo / pycairo），数值图形用三次贝塞尔圆弧构建：
  - RingCard  : 圆环仪表（CPU / 内存）
  - BarCard   : 圆角进度条（磁盘 / 电池）
  - NetCard   : 双色迷你折线（网络速率，最近 40 秒）

每张卡片是一个 Gtk.Box（CSS 类 .card），配色与半透明度全部交给 style.css。
"""

from __future__ import annotations

import math
import os
import threading
import time
from collections import deque

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, Gio, GLib, Graphene, Gsk, Gtk, Pango

import api_usage
import covers
import recent
from collectors import fmt_clock, fmt_duration

# ---------------------------------------------------------------- 尺寸与配色

CARD_WIDTH = 156          # 普通卡片宽度
WIDE_WIDTH = CARD_WIDTH * 2 + 24   # 宽卡：时钟 / 系统 / 右列（音乐、文件夹、进程）
BAR_WIDTH = CARD_WIDTH - 26
BAR_HEIGHT = 7
SPARK_WIDTH = BAR_WIDTH
SPARK_HEIGHT = 26

# 音乐卡：大封面 + 文字 + 播放控制
MUSIC_COVER = 96
PLAY_SIZE = 36
SKIP_SIZE = 28
MUSIC_BAR_WIDTH = WIDE_WIDTH - 24 - MUSIC_COVER - 12
MUSIC_SEEK_HEIGHT = 16

# GNOME 调色板
BLUE = (0.208, 0.518, 0.894)     # #3584e4
ORANGE = (0.902, 0.380, 0.000)   # #e66100
PURPLE = (0.569, 0.255, 0.675)   # #9141ac
TEAL = (0.129, 0.565, 0.643)     # #2190a4
GREEN = (0.200, 0.820, 0.478)    # #33d17a
YELLOW = (0.965, 0.827, 0.176)   # #f6d32d
RED = (0.878, 0.106, 0.141)      # #e01b24
GREY = (0.560, 0.590, 0.650)

BLUE_HEX = "#3584e4"
PURPLE_HEX = "#9141ac"

_WEEKDAYS = "一二三四五六日"

_TRACK_RGBA = (0.55, 0.57, 0.63)  # 仪表底槽颜色（配合 alpha 使用）


def _mix(color_a: tuple, color_b: tuple, t: float) -> tuple:
    t = max(0.0, min(1.0, t))
    return tuple(color_a[i] + (color_b[i] - color_a[i]) * t for i in range(3))


def load_color(percent: float) -> tuple:
    """按占用率从绿→黄→红渐变（越高越危险）。"""
    if percent >= 90:
        return RED
    if percent >= 70:
        return _mix(YELLOW, RED, (percent - 70) / 20.0)
    if percent >= 45:
        return _mix(GREEN, YELLOW, (percent - 45) / 25.0)
    return GREEN


def battery_color(percent: float) -> tuple:
    """电量越低越红。"""
    if percent <= 15:
        return RED
    if percent <= 30:
        return _mix(YELLOW, RED, (30 - percent) / 15.0)
    return GREEN


def temp_color(celsius: float) -> tuple:
    """温度越高越红：55°C 以下绿，70°C 起偏红，85°C 以上全红。"""
    if celsius >= 85:
        return RED
    if celsius >= 70:
        return _mix(YELLOW, RED, (celsius - 70) / 15.0)
    if celsius >= 55:
        return _mix(GREEN, YELLOW, (celsius - 55) / 15.0)
    return GREEN


# ---------------------------------------------------------------- Gsk 绘制工具

_K = 0.5522847498307936  # 三次贝塞尔逼近圆弧的系数


def _rgba(color: tuple, alpha: float = 1.0) -> Gdk.RGBA:
    rgba = Gdk.RGBA()
    rgba.red, rgba.green, rgba.blue, rgba.alpha = color[0], color[1], color[2], alpha
    return rgba


def _arc(builder: Gsk.PathBuilder, cx: float, cy: float, radius: float,
         start: float, end: float, move: bool = True) -> None:
    """把 [start, end] 圆弧（弧度，顺时针）用三次贝塞尔拼进路径。"""
    sweep = end - start
    segments = max(1, math.ceil(abs(sweep) / (math.pi / 2)))
    step = sweep / segments
    angle = start
    for index in range(segments):
        a0, a1 = angle, angle + step
        angle = a1
        t = (4.0 / 3.0) * math.tan((a1 - a0) / 4.0)
        x0, y0 = cx + radius * math.cos(a0), cy + radius * math.sin(a0)
        x1, y1 = cx + radius * math.cos(a1), cy + radius * math.sin(a1)
        c0x, c0y = x0 - t * radius * math.sin(a0), y0 + t * radius * math.cos(a0)
        c1x, c1y = x1 + t * radius * math.sin(a1), y1 - t * radius * math.cos(a1)
        if index == 0 and move:
            builder.move_to(x0, y0)
        else:
            builder.line_to(x0, y0)
        builder.cubic_to(c0x, c0y, c1x, c1y, x1, y1)


def _rounded_rect(builder: Gsk.PathBuilder, x: float, y: float,
                  w: float, h: float, radius: float) -> None:
    """构建圆角矩形路径。"""
    radius = max(0.0, min(radius, w / 2.0, h / 2.0))
    k = radius * _K
    builder.move_to(x + radius, y)
    builder.line_to(x + w - radius, y)
    builder.cubic_to(x + w - radius + k, y, x + w, y + radius - k, x + w, y + radius)
    builder.line_to(x + w, y + h - radius)
    builder.cubic_to(x + w, y + h - radius + k, x + w - radius + k, y + h, x + w - radius, y + h)
    builder.line_to(x + radius, y + h)
    builder.cubic_to(x + radius - k, y + h, x, y + h - radius + k, x, y + h - radius)
    builder.line_to(x, y + radius)
    builder.cubic_to(x, y + radius - k, x + radius - k, y, x + radius, y)
    builder.close()


def draw_progress_bar(snapshot, frac: float, color: tuple,
                      width: float = BAR_WIDTH, height: float = BAR_HEIGHT) -> None:
    """圆角进度条：半透明底槽 + 彩色已完成段（进度条 / 音乐进度共用）。"""
    w, h = float(width), float(height)
    radius = h / 2.0

    track = Gsk.PathBuilder()
    _rounded_rect(track, 0, 0, w, h, radius)
    snapshot.append_fill(track.to_path(), Gsk.FillRule.WINDING, _rgba(_TRACK_RGBA, 0.22))

    frac = max(0.0, min(1.0, frac))
    if frac > 0.004:
        fill_width = max(h, w * frac)
        fill = Gsk.PathBuilder()
        _rounded_rect(fill, 0, 0, fill_width, h, radius)
        snapshot.append_fill(fill.to_path(), Gsk.FillRule.WINDING, _rgba(color, 1.0))


class Gauge(Gtk.Widget):
    """轻量自绘部件：固定尺寸，通过 Gsk 快照绘制矢量图形。"""

    __gtype_name__ = "StickerGauge"

    def __init__(self, width: int, height: int):
        super().__init__()
        self.set_size_request(width, height)
        self._draw_func = None

    def set_draw_func(self, draw_func) -> None:
        self._draw_func = draw_func

    def do_snapshot(self, snapshot) -> None:  # noqa: N802 (GTK 虚函数命名)
        if self._draw_func is not None:
            self._draw_func(snapshot)


class SeekBar(Gauge):
    """可点击 / 拖动的进度条：拖动时预览目标位置，松开后回调上报。

    手势用法与 GTK 的滑块一致：点击手势与拖动手势分组，
    单击 = 跳到该位置，拖动 = 边拖边预览、松手生效。
    """

    def __init__(self, width: int, height: int, on_scrub, on_seek):
        super().__init__(width, height)
        self._track_width = float(width)
        self._track_height = float(BAR_HEIGHT)
        self._widget_height = float(height)
        self._frac = 0.0
        self._color = GREY
        self._scrub: float | None = None
        self._dragging = False
        self._on_scrub = on_scrub
        self._on_seek = on_seek
        self.set_draw_func(self._draw)
        self.set_cursor(Gdk.Cursor.new_from_name("pointer"))

        drag = Gtk.GestureDrag()
        drag.set_exclusive(True)
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.add_controller(drag)

        click = Gtk.GestureClick()
        click.set_exclusive(True)
        click.connect("released", self._click_released)
        self.add_controller(click)
        drag.group(click)

    def set_width(self, width: int) -> None:
        self._track_width = float(width)
        self.set_size_request(width, int(self._widget_height))
        self.queue_draw()

    def set_progress(self, fraction: float, color: tuple) -> None:
        """播放器上报的进度（拖动过程中不覆盖预览）。"""
        if self._scrub is None:
            self._frac = fraction
            self._color = color
            self.queue_draw()

    # ------------------------------------------------------------ 手势

    def _fraction_at(self, x: float) -> float:
        return max(0.0, min(1.0, x / self._track_width))

    def _drag_begin(self, _gesture, x: float, _y: float) -> None:
        self._dragging = True
        self._scrub = self._fraction_at(x)
        self._notify_scrub()
        self.queue_draw()

    def _drag_update(self, gesture, offset_x: float, _offset_y: float) -> None:
        start_x, _ = gesture.get_start_point()
        self._scrub = self._fraction_at(start_x + offset_x)
        self._notify_scrub()
        self.queue_draw()

    def _drag_end(self, _gesture, _offset_x: float, _offset_y: float) -> None:
        target = self._scrub
        self._scrub = None
        self._notify_scrub()
        if target is not None:
            self._on_seek(target)
        self.queue_draw()

    def _click_released(self, _gesture, _n_press: int, x: float, _y: float) -> None:
        if self._dragging:  # 拖动的收尾交给 drag-end
            self._dragging = False
            return
        self._on_seek(self._fraction_at(x))

    def _notify_scrub(self) -> None:
        if self._on_scrub is not None:
            self._on_scrub(self._scrub)

    # ------------------------------------------------------------ 绘制

    def _draw(self, snapshot) -> None:
        fraction = self._scrub if self._scrub is not None else self._frac
        top = (self._widget_height - self._track_height) / 2.0
        snapshot.save()
        snapshot.translate(Graphene.Point().init(0.0, top))
        draw_progress_bar(snapshot, fraction, self._color, self._track_width, self._track_height)
        snapshot.restore()

        if self._scrub is not None:  # 拖动时显示圆形把手
            radius = self._track_height * 1.15
            cx = max(radius, min(self._track_width - radius, fraction * self._track_width))
            cy = self._widget_height / 2.0
            thumb = Gsk.PathBuilder()
            _arc(thumb, cx, cy, radius, 0, 2 * math.pi)
            snapshot.append_fill(thumb.to_path(), Gsk.FillRule.WINDING, _rgba((1.0, 1.0, 1.0), 0.95))
            inner = Gsk.PathBuilder()
            _arc(inner, cx, cy, radius * 0.55, 0, 2 * math.pi)
            snapshot.append_fill(inner.to_path(), Gsk.FillRule.WINDING, _rgba(self._color, 1.0))


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


def _draw_dot(snapshot, size: float, color: tuple) -> None:
    builder = Gsk.PathBuilder()
    _arc(builder, size / 2.0, size / 2.0, size / 2.0 - 0.5, 0, 2 * math.pi)
    snapshot.append_fill(builder.to_path(), Gsk.FillRule.WINDING, _rgba(color, 0.95))


# ---------------------------------------------------------------- 卡片基类

class Card(Gtk.Box):
    """毛玻璃卡片基类：标题行（彩色圆点 + 标题）+ 自定义内容。"""

    def __init__(self, title: str = "", accent: tuple | None = None, spacing: int = 10,
                 width: int = CARD_WIDTH):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)
        self.add_css_class("card")
        self.set_size_request(width, -1)

        if title:
            head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
            if accent:
                dot = Gauge(8, 8)
                dot.set_valign(Gtk.Align.CENTER)
                dot.set_draw_func(lambda snapshot: _draw_dot(snapshot, 8, accent))
                head.append(dot)
            label = Gtk.Label(label=title)
            label.add_css_class("card-title")
            head.append(label)
            self.append(head)

    def resize(self, width: int) -> None:
        """调整卡片宽度（用户在控制器里改「卡片宽度」时由主程序调用）。"""
        self.set_size_request(width, -1)


class ClockCard(Card):
    """大号时钟 + 日期。"""

    def __init__(self, width: int = CARD_WIDTH):
        super().__init__(spacing=2, width=width)
        self.time_label = Gtk.Label(label="00:00:00")
        self.time_label.add_css_class("clock-time")
        self.time_label.set_halign(Gtk.Align.CENTER)
        self.date_label = Gtk.Label(label="")
        self.date_label.add_css_class("clock-date")
        self.date_label.set_halign(Gtk.Align.CENTER)
        self.append(self.time_label)
        self.append(self.date_label)

    def refresh(self) -> None:
        now = time.localtime()
        self.time_label.set_text(time.strftime("%H:%M:%S", now))
        self.date_label.set_text(f"{now.tm_mon}月{now.tm_mday}日 星期{_WEEKDAYS[now.tm_wday]}")


class RingCard(Card):
    """圆环仪表卡片（CPU / 内存）。"""

    RING_SIZE = 72
    RING_WIDTH = 6.5

    def __init__(self, title: str, accent: tuple = BLUE, width: int = CARD_WIDTH):
        super().__init__(title, accent, width=width)
        self._frac = 0.0
        self._color = accent

        self.ring = Gauge(self.RING_SIZE, self.RING_SIZE)
        self.ring.set_draw_func(self._draw_ring)

        self._overlay = Gtk.Overlay()
        self._overlay.set_child(self.ring)
        self._overlay.set_halign(Gtk.Align.CENTER)

        self.center_label = Gtk.Label(label="0%")
        self.center_label.add_css_class("ring-value")
        self.center_label.set_halign(Gtk.Align.CENTER)
        self.center_label.set_valign(Gtk.Align.CENTER)
        self._overlay.add_overlay(self.center_label)

        self.sub_label = Gtk.Label(label=" ")
        self.sub_label.add_css_class("muted")
        self.sub_label.set_halign(Gtk.Align.CENTER)
        self.sub_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.sub_label.set_max_width_chars(18)

        self.append(self._overlay)
        self.append(self.sub_label)

    def _draw_ring(self, snapshot) -> None:
        size = self.RING_SIZE
        cx = cy = size / 2.0
        radius = size / 2.0 - 9.0  # 预留发光与线宽
        line_width = self.RING_WIDTH

        # 底槽：整圈
        track = Gsk.PathBuilder()
        _arc(track, cx, cy, radius, 0, 2 * math.pi)
        snapshot.append_stroke(track.to_path(), Gsk.Stroke.new(line_width), _rgba(_TRACK_RGBA, 0.22))

        frac = max(0.0, min(1.0, self._frac))
        if frac > 0.005:
            start = -math.pi / 2
            end = start + 2 * math.pi * frac
            arc = Gsk.PathBuilder()
            _arc(arc, cx, cy, radius, start, end)
            path = arc.to_path()

            glow = Gsk.Stroke.new(line_width * 2.0)
            glow.set_line_cap(Gsk.LineCap.ROUND)
            snapshot.append_stroke(path, glow, _rgba(self._color, 0.13))

            main = Gsk.Stroke.new(line_width)
            main.set_line_cap(Gsk.LineCap.ROUND)
            snapshot.append_stroke(path, main, _rgba(self._color, 1.0))

    def refresh(self, percent: float, center_text: str, sub_text: str,
                color: tuple | None = None) -> None:
        self._frac = percent / 100.0
        self._color = color if color is not None else load_color(percent)
        self.center_label.set_text(center_text)
        self.sub_label.set_text(sub_text or " ")
        self.ring.queue_draw()


class ThermalCard(RingCard):
    """温度 / 风扇卡片：圆环显示 CPU 温度，下方显示风扇转速。"""

    def __init__(self, width: int = CARD_WIDTH):
        super().__init__("温度 / 风扇", TEAL, width=width)

    def refresh_thermal(self, temp_c: float | None, fan_rpm: float | None) -> None:
        if temp_c is None:
            self.refresh(0.0, "—", self._fan_text(fan_rpm), color=GREY)
            return
        self.refresh(min(max(temp_c, 0.0), 100.0), f"{temp_c:.0f}°",
                     self._fan_text(fan_rpm), color=temp_color(temp_c))

    def resize(self, width: int) -> None:
        super().resize(width)

    @staticmethod
    def _fan_text(fan_rpm: float | None) -> str:
        if fan_rpm is None:
            return "风扇 —"
        return f"风扇 {fan_rpm:.0f} 转/分"


class BarCard(Card):
    """横向圆角进度条卡片（磁盘 / 电池）。"""

    def __init__(self, title: str, accent: tuple = GREEN, width: int = CARD_WIDTH):
        super().__init__(title, accent, width=width)
        self._frac = 0.0
        self._color = accent
        self._bar_width = max(40.0, float(width) - 26.0)

        self.bar = Gauge(int(self._bar_width), BAR_HEIGHT)
        self.bar.set_margin_top(2)
        self.bar.set_halign(Gtk.Align.START)
        self.bar.set_draw_func(self._draw_bar)

        self.value_label = Gtk.Label(label="")
        self.value_label.add_css_class("value")
        self.value_label.set_halign(Gtk.Align.START)
        self.value_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.value_label.set_max_width_chars(18)

        self.sub_label = Gtk.Label(label=" ")
        self.sub_label.add_css_class("muted")
        self.sub_label.set_halign(Gtk.Align.START)
        self.sub_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.sub_label.set_max_width_chars(20)

        self.append(self.bar)
        self.append(self.value_label)
        self.append(self.sub_label)

    def _draw_bar(self, snapshot) -> None:
        draw_progress_bar(snapshot, self._frac, self._color, self._bar_width, BAR_HEIGHT)

    def resize(self, width: int) -> None:
        super().resize(width)
        self._bar_width = max(40.0, float(width) - 26.0)
        self.bar.set_size_request(int(self._bar_width), BAR_HEIGHT)
        self.bar.queue_draw()

    def refresh(self, percent: float, value_text: str, sub_text: str, color: tuple | None = None) -> None:
        self._frac = percent / 100.0
        self._color = color if color is not None else load_color(percent)
        self.value_label.set_text(value_text)
        self.sub_label.set_text(sub_text or " ")
        self.bar.queue_draw()


class BatteryCard(BarCard):
    """电池卡片：充电时蓝色 + ⚡，放电时显示剩余时间。"""

    def __init__(self, width: int = CARD_WIDTH):
        super().__init__("电池", GREEN, width=width)

    def refresh_battery(self, battery: dict) -> None:
        percent = battery["percent"]
        plugged = battery["plugged"]
        if plugged:
            value_text = f"{percent:.0f}% ⚡"
            sub_text = "充电中"
            color = BLUE
        else:
            value_text = f"{percent:.0f}%"
            sub_text = f"剩余 {fmt_duration(battery.get('secsleft'))}"
            color = battery_color(percent)
        self.refresh(percent, value_text, sub_text, color=color)


class CoverArt(Gtk.Widget):
    """圆角封面缩略图；没有封面时显示占位音符。"""

    __gtype_name__ = "StickerCoverArt"

    def __init__(self, size: int):
        super().__init__()
        self.set_size_request(size, size)
        self._size = size
        self._texture = None
        self._layout = None

    def set_texture(self, texture) -> None:
        self._texture = texture
        self.queue_draw()

    def do_snapshot(self, snapshot) -> None:  # noqa: N802 (GTK 虚函数命名)
        size = float(self._size)
        bounds = Graphene.Rect()
        bounds.init(0, 0, size, size)
        rounded = Gsk.RoundedRect()
        rounded.init_from_rect(bounds, 12.0)
        snapshot.push_rounded_clip(rounded)

        if self._texture is not None:
            snapshot.append_texture(self._texture, bounds)
        else:
            placeholder = Gsk.PathBuilder()
            _rounded_rect(placeholder, 0, 0, size, size, 12.0)
            snapshot.append_fill(placeholder.to_path(), Gsk.FillRule.WINDING,
                                 _rgba((1.0, 1.0, 1.0), 0.08))
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
            snapshot.append_layout(self._layout, _rgba((1.0, 1.0, 1.0), 0.5))
            snapshot.restore()

        snapshot.pop()


class MusicCard(Card):
    """音乐主打卡：封面 + 歌曲 / 歌手 / 进度条；无播放器时显示空闲态。"""

    def __init__(self, width: int = WIDE_WIDTH):
        super().__init__("音乐", PURPLE, width=width)
        self._frac = 0.0
        self._color = GREY
        self._cover_key: tuple | None = None
        self._cover_token = 0
        # 播放控制状态
        self._player_bus = ""
        self._track_id = ""
        self._playing = False
        self._position = 0.0
        self._length = 0.0
        self._pending_seek: tuple | None = None
        self._play_icon_name = ""

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.cover = CoverArt(MUSIC_COVER)
        self.cover.set_valign(Gtk.Align.START)
        row.append(self.cover)

        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        column.set_hexpand(True)

        self.song_label = Gtk.Label(label="暂无播放")
        self.song_label.add_css_class("music-title")
        self.song_label.set_halign(Gtk.Align.START)
        self.song_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.song_label.set_max_width_chars(14)

        self.artist_label = Gtk.Label(label="播放器开始播放后自动显示")
        self.artist_label.add_css_class("music-sub")
        self.artist_label.set_halign(Gtk.Align.START)
        self.artist_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.artist_label.set_max_width_chars(20)

        spacer = Gtk.Box()
        spacer.set_vexpand(True)

        self._bar_width = max(60.0, float(width) - 24.0 - MUSIC_COVER - 12.0)
        self.bar = SeekBar(int(self._bar_width), MUSIC_SEEK_HEIGHT, self._on_scrub, self._on_seek)
        self.bar.set_halign(Gtk.Align.START)

        self.time_label = Gtk.Label(label=" ")
        self.time_label.add_css_class("music-sub")
        self.time_label.set_halign(Gtk.Align.START)
        self.time_label.set_hexpand(True)

        self.prev_button = self._make_skip_button(
            "media-skip-backward-symbolic", "上一曲", lambda _b: self._on_skip("Previous"))
        self.next_button = self._make_skip_button(
            "media-skip-forward-symbolic", "下一曲", lambda _b: self._on_skip("Next"))

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        controls.append(self.time_label)
        controls.append(self.prev_button)

        self.play_button = Gtk.Button()
        self.play_button.add_css_class("play-button")
        self.play_button.set_valign(Gtk.Align.CENTER)
        self.play_button.set_tooltip_text("播放 / 暂停")
        self.play_button.set_size_request(PLAY_SIZE, PLAY_SIZE)
        self.play_button.set_sensitive(False)
        self.play_button.connect("clicked", self._on_play_clicked)
        self._set_play_icon("media-playback-start-symbolic")
        controls.append(self.play_button)
        controls.append(self.next_button)

        column.append(self.song_label)
        column.append(self.artist_label)
        column.append(spacer)
        column.append(self.bar)
        column.append(controls)
        row.append(column)
        self.append(row)

    def resize(self, width: int) -> None:
        super().resize(width)
        self._bar_width = max(60.0, float(width) - 24.0 - MUSIC_COVER - 12.0)
        self.bar.set_width(int(self._bar_width))

    # ------------------------------------------------------------ 播放控制

    def _make_skip_button(self, icon_name: str, tooltip: str, callback) -> Gtk.Button:
        button = Gtk.Button()
        button.add_css_class("skip-button")
        button.set_valign(Gtk.Align.CENTER)
        button.set_tooltip_text(tooltip)
        button.set_size_request(SKIP_SIZE, SKIP_SIZE)
        button.set_sensitive(False)
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(14)
        button.set_child(icon)
        button.connect("clicked", callback)
        return button

    def _on_skip(self, method: str) -> None:
        _mpris_call(self._player_bus, method)

    # ------------------------------------------------------------ 播放控制

    def _set_play_icon(self, name: str) -> None:
        if name == self._play_icon_name:
            return
        self._play_icon_name = name
        icon = Gtk.Image.new_from_icon_name(name)
        icon.set_pixel_size(16)
        self.play_button.set_child(icon)

    def _on_play_clicked(self, _button) -> None:
        _mpris_call(self._player_bus, "PlayPause")

    def _on_scrub(self, fraction: float | None) -> None:
        """拖动预览：时间标签跟着手指走。"""
        if not self._length:
            return
        if fraction is None:
            self.time_label.set_text(f"{fmt_clock(self._position)} / {fmt_clock(self._length)}")
            return
        self.time_label.set_text(f"{fmt_clock(fraction * self._length)} / {fmt_clock(self._length)}")

    def _on_seek(self, fraction: float) -> None:
        """落到目标位置：优先 SetPosition（需要 trackid），否则用相对 Seek。"""
        if not self._player_bus or not self._length:
            return
        target = max(0.0, min(1.0, fraction)) * self._length
        self._pending_seek = (target, time.monotonic())
        self.time_label.set_text(f"{fmt_clock(target)} / {fmt_clock(self._length)}")
        self.bar.set_progress(target / self._length if self._length else 0.0, self._color)
        if self._track_id:
            _mpris_call(self._player_bus, "SetPosition",
                        GLib.Variant("(ox)", (self._track_id, int(target * 1_000_000))))
        else:
            delta = int((target - self._position) * 1_000_000)
            _mpris_call(self._player_bus, "Seek", GLib.Variant("(x)", (delta,)))

    # ------------------------------------------------------------ 封面

    def _update_cover(self, title: str, artist: str, album: str, art: str) -> None:
        key = (title, artist, album, art)
        if key == self._cover_key:
            return
        self._cover_key = key
        self._cover_token += 1
        if not title:
            self.cover.set_texture(None)
            return
        threading.Thread(
            target=self._cover_worker,
            args=(self._cover_token, title, artist, album, art), daemon=True).start()

    def _cover_worker(self, token: int, title: str, artist: str, album: str, art: str) -> None:
        path = covers.cover_for(title, artist, album, art or None)
        texture = None
        if path:
            try:
                texture = Gdk.Texture.new_from_filename(path)
            except Exception:
                texture = None
        GLib.idle_add(self._apply_cover, token, texture)

    def _apply_cover(self, token: int, texture) -> bool:
        if token == self._cover_token:
            self.cover.set_texture(texture)
        return GLib.SOURCE_REMOVE

    def refresh(self, music: dict | None) -> None:
        if music and music.get("blocked"):
            self.song_label.set_text("读不到播放器")
            self.song_label.set_tooltip_text("播放器在总线上，但读取被系统策略拦截")
            self.artist_label.set_text(f"检测到 {music.get('player', '播放器')} 但读取被限制")
            self.time_label.set_text("")
            self._frac, self._color = 0.0, GREY
            self._player_bus = ""
            self._playing = False
            self.play_button.set_sensitive(False)
            self.prev_button.set_sensitive(False)
            self.next_button.set_sensitive(False)
            self._set_play_icon("media-playback-start-symbolic")
            self._update_cover("", "", "", "")
            self.bar.set_progress(0.0, GREY)
            return

        if music is None:
            self.song_label.set_text("暂无播放")
            self.song_label.set_tooltip_text(None)
            self.artist_label.set_text("播放器开始播放后自动显示")
            self.time_label.set_text("")
            self._frac, self._color = 0.0, GREY
            self._player_bus = ""
            self._playing = False
            self.play_button.set_sensitive(False)
            self.prev_button.set_sensitive(False)
            self.next_button.set_sensitive(False)
            self._set_play_icon("media-playback-start-symbolic")
            self._update_cover("", "", "", "")
            self.bar.set_progress(0.0, GREY)
            return

        title = music.get("title") or "未知曲目"
        self.song_label.set_text(title)
        self.song_label.set_tooltip_text(f"{title} · {music.get('player', '')}".strip(" ·"))

        parts = [music.get("artist") or "", music.get("album") or ""]
        if music.get("status") == "Paused":
            parts.append("已暂停")
        elif music.get("status") == "Stopped":
            parts.append("已停止")
        self.artist_label.set_text(" · ".join(part for part in parts if part) or " ")

        # 播放控制状态
        self._player_bus = music.get("bus_name") or ""
        self._track_id = music.get("track_id") or ""
        self._playing = music.get("status") == "Playing"
        self._set_play_icon("media-playback-pause-symbolic" if self._playing
                            else "media-playback-start-symbolic")
        self.play_button.set_sensitive(bool(self._player_bus))
        self.prev_button.set_sensitive(bool(self._player_bus) and music.get("can_go_previous", True))
        self.next_button.set_sensitive(bool(self._player_bus) and music.get("can_go_next", True))

        position = music.get("position") or 0.0
        length = music.get("length") or 0.0
        self._length = length

        # 刚拖动过的目标位置：等播放器跟上，避免进度条来回跳
        if self._pending_seek is not None:
            target, at = self._pending_seek
            if abs(position - target) < 2.0 or time.monotonic() - at > 1.5:
                self._pending_seek = None
            else:
                position = target
        self._position = position

        if length:
            self._frac = max(0.0, min(1.0, position / length))
            self.time_label.set_text(f"{fmt_clock(position)} / {fmt_clock(length)}")
        else:
            self._frac = 0.0
            self.time_label.set_text(fmt_clock(position) if position else "")

        self._color = BLUE if self._playing else GREY
        self.bar.set_progress(self._frac, self._color)

        # 封面（异步：先显示占位，取到后替换）
        self._update_cover(music.get("title") or "", music.get("artist") or "",
                           music.get("album") or "", music.get("art") or "")


class NetCard(Card):
    """网络卡片：实时速率 + 双色迷你折线（最近 40 秒）。"""

    HISTORY = 40

    def __init__(self, width: int = CARD_WIDTH):
        super().__init__("网络", GREEN, width=width)
        self._spark_width = max(40.0, float(width) - 26.0)
        self._down = deque([0.0] * self.HISTORY, maxlen=self.HISTORY)
        self._up = deque([0.0] * self.HISTORY, maxlen=self.HISTORY)
        self._peak = 1024.0

        self.down_label = Gtk.Label(label=" ")
        self.down_label.add_css_class("net-line")
        self.down_label.set_halign(Gtk.Align.START)
        self.down_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.down_label.set_max_width_chars(16)
        self.up_label = Gtk.Label(label=" ")
        self.up_label.add_css_class("net-line")
        self.up_label.set_halign(Gtk.Align.START)
        self.up_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.up_label.set_max_width_chars(16)

        self.spark = Gauge(int(self._spark_width), SPARK_HEIGHT)
        self.spark.set_margin_top(2)
        self.spark.set_halign(Gtk.Align.START)
        self.spark.set_draw_func(self._draw_spark)

        self.append(self.down_label)
        self.append(self.up_label)
        self.append(self.spark)

    def resize(self, width: int) -> None:
        super().resize(width)
        self._spark_width = max(40.0, float(width) - 26.0)
        self.spark.set_size_request(int(self._spark_width), SPARK_HEIGHT)
        self.spark.queue_draw()

    def refresh(self, down_rate: float, up_rate: float) -> None:
        from collectors import fmt_speed

        self.down_label.set_markup(
            f'<span foreground="{BLUE_HEX}">↓</span> 下载 {fmt_speed(down_rate)}'
        )
        self.up_label.set_markup(
            f'<span foreground="{PURPLE_HEX}">↑</span> 上传 {fmt_speed(up_rate)}'
        )
        self._down.append(down_rate)
        self._up.append(up_rate)
        self.spark.queue_draw()

    def _draw_spark(self, snapshot) -> None:
        down = list(self._down)
        up = list(self._up)
        peak = max(max(down), max(up))
        # 量程平滑回落，避免折线剧烈跳动
        self._peak = max(peak, self._peak * 0.85, 1024.0)
        self._plot_series(snapshot, down, BLUE, self._spark_width, SPARK_HEIGHT, self._peak)
        self._plot_series(snapshot, up, PURPLE, self._spark_width, SPARK_HEIGHT, self._peak)

    @staticmethod
    def _plot_series(snapshot, values: list, color: tuple,
                     w: int, h: int, peak: float) -> None:
        n = len(values)
        if n < 2:
            return
        top, bottom = 4.0, h - 2.0
        points = [
            ((i / (n - 1)) * w, bottom - (v / peak) * (bottom - top))
            for i, v in enumerate(values)
        ]

        # 渐变填充
        fill = Gsk.PathBuilder()
        fill.move_to(0, h)
        for x, y in points:
            fill.line_to(x, y)
        fill.line_to(w, h)
        fill.close()
        snapshot.append_fill(fill.to_path(), Gsk.FillRule.WINDING, _rgba(color, 0.15))

        # 折线
        line = Gsk.PathBuilder()
        line.move_to(*points[0])
        for x, y in points[1:]:
            line.line_to(x, y)
        stroke = Gsk.Stroke.new(1.6)
        stroke.set_line_cap(Gsk.LineCap.ROUND)
        stroke.set_line_join(Gsk.LineJoin.ROUND)
        snapshot.append_stroke(line.to_path(), stroke, _rgba(color, 0.9))


class SystemCard(Card):
    """系统信息卡片（宽版）：两列展示 主机 / 系统 / 内核 / 运行 / 负载。"""

    _COLUMNS = (("主机", "内核", "负载"), ("系统", "运行"))

    def __init__(self, width: int = WIDE_WIDTH):
        super().__init__("系统", GREY, width=width)
        self._values: dict[str, Gtk.Label] = {}

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        row.set_homogeneous(True)
        for keys in self._COLUMNS:
            grid = Gtk.Grid(column_spacing=9, row_spacing=6)
            for line, key in enumerate(keys):
                label = Gtk.Label(label=key)
                label.add_css_class("row-label")
                label.set_halign(Gtk.Align.START)
                value = Gtk.Label(label="—")
                value.add_css_class("row-value")
                value.set_halign(Gtk.Align.END)
                value.set_hexpand(True)
                value.set_ellipsize(Pango.EllipsizeMode.END)
                value.set_max_width_chars(14)
                grid.attach(label, 0, line, 1, 1)
                grid.attach(value, 1, line, 1, 1)
                self._values[key] = value
            row.append(grid)
        self.append(row)

    def refresh(self, rows: dict) -> None:
        for key, value in self._values.items():
            value.set_text(rows.get(key, "—"))


class ProcessCard(Card):
    """进程 Top 3（宽版）：名称 + CPU / 内存两列，悬停看 PID。"""

    def __init__(self, width: int = WIDE_WIDTH):
        super().__init__("进程 Top 3", YELLOW, width=width)
        self._rows: list[tuple[Gtk.Label, Gtk.Label, Gtk.Label]] = []

        grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        for column, text in enumerate(("", "名称", "CPU", "内存")):
            head = Gtk.Label(label=text)
            head.add_css_class("row-label")
            head.set_halign(Gtk.Align.START if column < 2 else Gtk.Align.END)
            head.set_hexpand(column == 1)
            grid.attach(head, column, 0, 1, 1)

        for rank in range(3):
            number = Gtk.Label(label=str(rank + 1))
            number.add_css_class("row-label")
            number.set_valign(Gtk.Align.CENTER)

            name = Gtk.Label(label="—")
            name.add_css_class("row-value")
            name.set_halign(Gtk.Align.START)
            name.set_hexpand(True)
            name.set_ellipsize(Pango.EllipsizeMode.END)
            name.set_max_width_chars(22)

            cpu = Gtk.Label(label="")
            cpu.add_css_class("row-value")
            cpu.set_halign(Gtk.Align.END)
            cpu.set_width_chars(5)

            mem = Gtk.Label(label="")
            mem.add_css_class("row-value")
            mem.set_halign(Gtk.Align.END)
            mem.set_width_chars(5)

            grid.attach(number, 0, rank + 1, 1, 1)
            grid.attach(name, 1, rank + 1, 1, 1)
            grid.attach(cpu, 2, rank + 1, 1, 1)
            grid.attach(mem, 3, rank + 1, 1, 1)
            self._rows.append((name, cpu, mem))
        self.append(grid)

    def refresh(self, procs: list) -> None:
        if not procs:
            for index, (name, cpu, mem) in enumerate(self._rows):
                name.set_text("采样中…" if index == 0 else " ")
                name.set_tooltip_text(None)
                cpu.set_text("")
                mem.set_text("")
            return
        for index, (name, cpu, mem) in enumerate(self._rows):
            if index >= len(procs):
                name.set_text(" ")
                name.set_tooltip_text(None)
                cpu.set_text("")
                mem.set_text("")
                continue
            row = procs[index]
            name.set_text(row["name"])
            name.set_tooltip_text(f"{row['name']} · PID {row['pid']}")
            cpu.set_text(f"{row['cpu']:.0f}%" if row["cpu"] >= 10 else f"{row['cpu']:.1f}%")
            mem.set_text(f"{row['mem']:.1f}%")


# ---------------------------------------------------------------- 常用文件卡片

def _mime_gicon(mime: str, path: str):
    """按 MIME 类型拿图标（拿不到就返回 None）。"""
    try:
        if not mime:
            mime = Gio.content_type_guess(path, None)[0]
        return Gio.content_type_get_icon(mime)
    except (GLib.Error, TypeError):
        return None


class RecentFileRow(Gtk.Box):
    """一行最近文件：双击用默认程序打开；右键：打开 / 在文件管理器中显示 / 复制路径。"""

    def __init__(self, entry: dict):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        self.path = entry["path"]
        self.add_css_class("file-row")
        self.set_tooltip_text(f"{self.path}\n双击打开 · 右键更多")

        image = Gtk.Image()
        icon = _mime_gicon(entry.get("mime", ""), self.path)
        if icon is not None:
            image.set_from_gicon(icon)
        else:
            image.set_from_icon_name("text-x-generic")
        image.set_pixel_size(14)
        image.set_valign(Gtk.Align.CENTER)

        label = Gtk.Label(label=entry.get("name", ""))
        label.add_css_class("row-value")
        label.set_halign(Gtk.Align.START)
        label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        label.set_max_width_chars(16)
        label.set_hexpand(True)
        self.append(image)
        self.append(label)

        click = Gtk.GestureClick()
        click.connect("pressed", self._on_pressed)
        self.add_controller(click)

        self._menu = self._build_menu()

    # ------------------------------------------------------------ 交互

    def _on_pressed(self, gesture, n_press: int, x: float, y: float) -> None:
        button = gesture.get_current_button()
        if button == 1 and n_press == 2:
            self._open()
        elif button == 3:
            rect = Gdk.Rectangle()
            rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
            self._menu.set_pointing_to(rect)
            self._menu.popup()

    def _open(self) -> None:
        try:
            Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(self.path).get_uri(), None)
        except GLib.Error:
            pass

    def _reveal(self) -> None:
        """在文件管理器里定位该文件；不支持时退回打开所在目录。"""
        uri = Gio.File.new_for_path(self.path).get_uri()
        try:
            Gio.DBus.session.call_sync(
                "org.freedesktop.FileManager1", "/org/freedesktop/FileManager1",
                "org.freedesktop.FileManager1", "ShowItems",
                GLib.Variant("(ass)", ([uri], "")), None,
                Gio.DBusCallFlags.NONE, 800, None)
            return
        except GLib.Error:
            pass
        try:
            folder = Gio.File.new_for_path(os.path.dirname(self.path)).get_uri()
            Gio.AppInfo.launch_default_for_uri(folder, None)
        except GLib.Error:
            pass

    def _copy_path(self) -> None:
        self.get_clipboard().set(self.path)

    def _build_menu(self) -> Gtk.Popover:
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        for text, callback in (
            ("打开", self._open),
            ("在文件管理器中显示", self._reveal),
            ("复制路径", self._copy_path),
        ):
            button = Gtk.Button()
            button.add_css_class("flat")
            button.set_size_request(150, -1)
            label = Gtk.Label(label=text)
            label.set_halign(Gtk.Align.START)
            button.set_child(label)
            button.connect("clicked", lambda _btn, cb=callback: (popover.popdown(), cb()))
            box.append(button)
        popover.set_child(box)
        popover.set_parent(self)
        return popover


class ApiCard(Card):
    """API 用量卡片：OpenCode 花费 / token + 各厂商余额（厂商可配置）。"""

    def __init__(self, width: int = WIDE_WIDTH):
        super().__init__("API 用量", ORANGE, width=width)
        self._grid = Gtk.Grid(column_spacing=10, row_spacing=6)
        self.append(self._grid)
        self._rows: dict[str, Gtk.Label] = {}
        self._provider_key: tuple = ()
        self._providers: list[dict] = []
        self._fetching = False
        self._token = 0
        self._rebuild()

    # ------------------------------------------------------------ 对外接口

    def refresh(self, settings_values: dict | None = None) -> None:
        self._render_usage(api_usage.opencode_usage())

        providers = api_usage.load_providers(settings_values or {})
        ids = tuple(provider["id"] for provider in providers)
        if ids != self._provider_key:
            self._provider_key = ids
            self._providers = providers
            self._token += 1
            self._rebuild()
            self._spawn_fetch(force=True)
        elif api_usage.balances_expired():
            self._spawn_fetch(force=False)

        self._render_balances(api_usage.balances_cached())

    # ------------------------------------------------------------ 渲染

    def _rebuild(self) -> None:
        child = self._grid.get_first_child()
        while child is not None:
            self._grid.remove(child)
            child = self._grid.get_first_child()
        self._rows = {}

        def add_row(row: int, name: str) -> Gtk.Label:
            label = Gtk.Label(label=name)
            label.add_css_class("row-label")
            label.set_halign(Gtk.Align.START)
            value = Gtk.Label(label="—")
            value.add_css_class("row-value")
            value.set_halign(Gtk.Align.END)
            value.set_hexpand(True)
            value.set_ellipsize(Pango.EllipsizeMode.END)
            value.set_max_width_chars(30)
            self._grid.attach(label, 0, row, 1, 1)
            self._grid.attach(value, 1, row, 1, 1)
            return value

        self._rows["today"] = add_row(0, "OpenCode 今日")
        self._rows["total"] = add_row(1, "OpenCode 累计")
        row = 2
        for provider in self._providers:
            self._rows[provider["id"]] = add_row(row, f"{provider['label']} 余额")
            row += 1
        if not self._providers:
            hint = Gtk.Label(label="未配置余额查询（见 README）")
            hint.add_css_class("muted")
            hint.set_halign(Gtk.Align.START)
            self._grid.attach(hint, 0, row, 2, 1)

    def _render_usage(self, usage: dict | None) -> None:
        if not usage:
            text_today = text_total = "—"
        else:
            text_today = (f"${usage['today_cost']:.2f} · "
                          f"{api_usage.fmt_tokens(usage['today_tokens'])} tok")
            text_total = (f"${usage['total_cost']:.2f} · "
                          f"{api_usage.fmt_tokens(usage['total_tokens'])} tok")
        label_today = self._rows.get("today")
        label_total = self._rows.get("total")
        if label_today is not None:
            label_today.set_text(text_today)
            label_today.set_tooltip_text(f"共 {usage['sessions']} 个会话" if usage else None)
        if label_total is not None:
            label_total.set_text(text_total)

    def _render_balances(self, items: list[dict] | None) -> None:
        for item in items or []:
            label = self._rows.get(item["id"])
            if label is None:
                continue
            if item.get("error") or item.get("balance") is None:
                label.set_text("—")
                label.set_tooltip_text(str(item.get("error") or "查询失败")[:200])
            else:
                label.set_text(api_usage.fmt_balance(item["balance"], item["currency"]))
                label.set_tooltip_text(f"{item['label']} 余额")

    # ------------------------------------------------------------ 余额查询（后台线程）

    def _spawn_fetch(self, force: bool) -> None:
        if self._fetching:
            return
        self._fetching = True
        providers = list(self._providers)
        token = self._token

        def worker() -> None:
            items = api_usage.fetch_balances(providers, force=force)
            GLib.idle_add(self._apply_balances, token, items)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_balances(self, token: int, items: list[dict]) -> bool:
        self._fetching = False
        if token == self._token:
            self._render_balances(items)
        return GLib.SOURCE_REMOVE


class RecentFilesCard(Card):
    """常用文件卡片（宽版，两列）：最近经常打开的文件。"""

    def __init__(self, width: int = WIDE_WIDTH):
        super().__init__("常用文件", BLUE, width=width)
        self._grid = Gtk.Grid(column_spacing=14, row_spacing=7)
        self._grid.set_column_homogeneous(True)
        self.append(self._grid)

        hint = Gtk.Label(label="双击打开 · 右键更多")
        hint.add_css_class("muted")
        hint.set_halign(Gtk.Align.START)
        self.append(hint)
        self._keys: tuple = ()

    def refresh(self, entries: list[dict] | None = None) -> None:
        """传入文件列表或留空自动读取；列表没变化就不重建。"""
        if entries is None:
            entries = recent.recent_files(6)
        keys = tuple(entry["path"] for entry in entries)
        if keys == self._keys:
            return
        self._keys = keys

        child = self._grid.get_first_child()
        while child is not None:
            self._grid.remove(child)
            child = self._grid.get_first_child()

        if not entries:
            empty = Gtk.Label(label="暂无最近文件")
            empty.add_css_class("muted")
            empty.set_halign(Gtk.Align.START)
            self._grid.attach(empty, 0, 0, 2, 1)
            return

        for index, entry in enumerate(entries):
            row = RecentFileRow(entry)
            row.set_hexpand(True)
            self._grid.attach(row, index % 2, index // 2, 1, 1)
