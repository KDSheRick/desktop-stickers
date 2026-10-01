#!/usr/bin/env python3
"""桌面系统信息贴纸：无边框毛玻璃卡片，实时显示系统状态。

运行：  python3 main.py

结构：  每张卡片是一张独立的贴纸（独立窗口）：
        时钟；处理器 / 内存；磁盘 / 网络；电池 / 系统
        默认排布在屏幕左上角（时钟横跨两列，其余两列成对）。

交互：  按住任意贴纸拖动即可移动它；右键菜单：复位全部贴纸 / 切换深浅玻璃 / 退出。
定位：  每张贴纸的位置会被单独记住（~/.config/sysstickers/position.json），
        下次启动自动恢复；「复位全部贴纸」可一键排回左上角。

说明：  GNOME 的 Wayland 协议不允许应用自己移动窗口，因此本程序默认通过
        XWayland（X11 后端）运行以支持定位。若想强制原生 Wayland：
        STICKERS_BACKEND=wayland python3 main.py （此时退化为单窗面板）
"""

from __future__ import annotations

import os
import subprocess
import sys

# ---- 后端选择：默认 XWayland（这样才能把窗口放到指定位置）----
# 想强制原生 Wayland：启动前设置 STICKERS_BACKEND=wayland（位置记忆将不可用）
from positioner import x11_available

_stickers_backend = os.environ.get("STICKERS_BACKEND", "").lower()
if _stickers_backend in ("x11", "wayland"):
    os.environ["GDK_BACKEND"] = _stickers_backend
elif os.environ.get("DISPLAY") and x11_available():
    os.environ["GDK_BACKEND"] = "x11"

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from collectors import SystemStats, fmt_bytes, fmt_uptime  # noqa: E402
from lyrics_tray import CONTROL_NAME  # noqa: E402
import settings  # noqa: E402
from positioner import (  # noqa: E402
    X11Mover,
    clamp_position,
    load_saved_positions,
    save_saved_positions,
    xid_of,
)
from widgets import (  # noqa: E402
    BLUE,
    ApiCard,
    PURPLE,
    TEAL,
    BatteryCard,
    BarCard,
    ClockCard,
    MusicCard,
    NetCard,
    ProcessCard,
    RecentFilesCard,
    RingCard,
    SystemCard,
    ThermalCard,
)

APP_ID = "com.loong.SysStickers"
ISLAND_NAME = "com.loong.DynamicIsland"  # 与 island.py 的 APP_ID 保持一致
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSS_FILE = os.path.join(BASE_DIR, "style.css")
REFRESH_INTERVAL_MS = 1000

# ---- 排布参数 ----
# 卡片宽度 / 圆角 / 不透明度 / 位置间距都可以用控制器（control.py）实时调整，
# 结果保存在 ~/.config/sysstickers/settings.json，默认值见 settings.py。
WINDOW_MARGIN = settings.WINDOW_MARGIN  # 每个窗口内部留给阴影的空白（固定）

# 两列成对排布的顺序（温度/风扇归到左侧，和电池一行）
PAIRS = (("cpu", "mem"), ("disk", "net"), ("battery", "thermal"))

# 左列里横跨两列的宽卡
WIDE_LEFT = ("sys",)

# 右上角一列（与宽卡同宽）：音乐 / 常用文件 / 进程 / API 用量
RIGHT_COLUMN = ("music", "files", "proc", "api")

# 所有横跨两列的宽卡
WIDE_IDS = ("clock", "sys") + RIGHT_COLUMN


def compute_layout(heights: dict, card_width: int, gap: int,
                   margin_x: int, margin_top: int) -> dict:
    """计算默认排布：时钟一行，其余两列成对，锚定左上角。

    参数都来自设置（控制器可调）。这里的高度是「窗口高度」（卡片 + 上下留白）。
    """
    window_gap = max(0, gap - 2 * WINDOW_MARGIN)
    positions = {}
    y = margin_top
    positions["clock"] = (margin_x, y)
    y += heights.get("clock", 80) + window_gap
    for left, right in PAIRS:
        if left in heights and right in heights:
            positions[left] = (margin_x, y)
            positions[right] = (margin_x + card_width + gap, y)
            y += max(heights[left], heights[right]) + window_gap
        elif left in heights:
            positions[left] = (margin_x, y)
            y += heights[left] + window_gap
        elif right in heights:
            positions[right] = (margin_x, y)
            y += heights[right] + window_gap
    for sticker_id in WIDE_LEFT:
        if sticker_id in heights:
            positions[sticker_id] = (margin_x, y)
            y += heights[sticker_id] + window_gap
    return positions


def make_sticker_box(card: Gtk.Widget, margin: int = WINDOW_MARGIN) -> Gtk.Box:
    """给卡片套一层带边距的容器（边距用于显示阴影）。"""
    root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    root.add_css_class("dark")  # 默认深色玻璃，可右键切换
    root.set_margin_top(margin)
    root.set_margin_bottom(margin)
    root.set_margin_start(margin)
    root.set_margin_end(margin)
    root.append(card)
    return root


def attach_menu(window: Gtk.Window, root_box: Gtk.Widget) -> None:
    """右键菜单：复位 / 主题 / 退出（动作注册在应用上，所有贴纸共用）。"""
    menu = Gio.Menu()
    menu.append("复位全部贴纸", "app.reset-stickers")
    menu.append("切换深浅玻璃", "app.toggle-theme")
    menu.append("顶栏歌词", "app.toggle-lyrics")
    menu.append("灵动岛", "app.toggle-island")
    menu.append("贴纸设置…", "app.open-control")
    menu.append("退出", "app.quit")

    popover = Gtk.PopoverMenu.new_from_model(menu)
    popover.set_parent(root_box)
    window._popover = popover  # noqa: SLF001 (保持引用)

    def on_pressed(_gesture, _n_press, x, y):
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.popup()

    click = Gtk.GestureClick()
    click.set_button(3)  # 右键
    click.connect("pressed", on_pressed)
    root_box.add_controller(click)


def apply_x11_hints(window: Gtk.Window) -> None:
    """X11 提示：让贴纸不出现在任务栏 / 工作区切换器里。"""
    surface = window.get_surface()
    if surface is None:
        return
    import warnings

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            if hasattr(surface, "set_skip_taskbar_hint"):
                surface.set_skip_taskbar_hint(True)
            if hasattr(surface, "set_skip_pager_hint"):
                surface.set_skip_pager_hint(True)
    except Exception:
        pass


class StickerWindow(Gtk.ApplicationWindow):
    """单张贴纸窗口：一张卡片 + 可拖动 + 右键菜单。"""

    def __init__(self, app: Gtk.Application, sticker_id: str, card: Gtk.Widget):
        super().__init__(application=app, title=f"系统贴纸 · {sticker_id}")
        self.sticker_id = sticker_id
        self.set_decorated(False)
        self.set_resizable(False)

        self.root_box = make_sticker_box(card)
        attach_menu(self, self.root_box)

        handle = Gtk.WindowHandle()  # 按住任意位置即可拖动
        handle.set_child(self.root_box)
        self.set_child(handle)

        self.connect("map", lambda *_a: apply_x11_hints(self))


class PanelWindow(Gtk.ApplicationWindow):
    """纯 Wayland 兜底：无法逐张贴纸定位时，退化为单窗多卡片面板。"""

    def __init__(self, app: Gtk.Application, cards: dict):
        super().__init__(application=app, title="系统贴纸")
        self.set_decorated(False)
        self.set_resizable(False)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.add_css_class("dark")
        root.set_margin_top(14)
        root.set_margin_bottom(14)
        root.set_margin_start(14)
        root.set_margin_end(14)
        self.root_box = root

        grid = Gtk.Grid(column_spacing=8, row_spacing=8)
        root.append(grid)
        grid.attach(cards["clock"], 0, 0, 2, 1)
        grid.attach(cards["cpu"], 0, 1, 1, 1)
        grid.attach(cards["mem"], 1, 1, 1, 1)
        grid.attach(cards["disk"], 0, 2, 1, 1)
        grid.attach(cards["net"], 1, 2, 1, 1)
        if "battery" in cards:
            grid.attach(cards["battery"], 0, 3, 1, 1)
            grid.attach(cards["thermal"], 1, 3, 1, 1)
        else:
            grid.attach(cards["thermal"], 0, 3, 1, 1)
        grid.attach(cards["sys"], 0, 4, 2, 1)
        for row, sticker_id in enumerate(RIGHT_COLUMN):
            if sticker_id in cards:
                grid.attach(cards[sticker_id], 2, row, 1, 1)

        attach_menu(self, root)

        handle = Gtk.WindowHandle()
        handle.set_child(root)
        self.set_child(handle)


class StickerApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.stats = SystemStats()
        self.mover = X11Mover()
        self.cards: dict[str, Gtk.Widget] = {}
        self.windows: dict[str, StickerWindow] = {}
        self.panel_window: PanelWindow | None = None

        self._saved = load_saved_positions()
        self._last_seen: dict[str, tuple[int, int, int]] = {}
        self._last_sizes: dict[str, tuple[int, int]] = {}
        self._targets: dict[str, tuple[int, int]] = {}
        self._moving = False
        self._position_ready = False
        self._mapped_count = 0
        self._arrange_retries = 0
        self._place_attempts = 0
        self._stable_rounds = 0

        self._install_actions()

    # ------------------------------------------------------------ 启动

    def do_activate(self) -> None:
        if self.windows or self.panel_window is not None:
            if self.panel_window is not None:
                self.panel_window.present()
            for window in self.windows.values():
                window.present()
            return

        self.settings = settings.load()
        self._create_cards()
        self._load_css()
        self._init_settings()
        if self.mover.ok:
            self._create_sticker_windows()
        else:
            # 无 X11：单窗面板，窗口位置由桌面环境决定
            self.panel_window = PanelWindow(self, self.cards)
            self.panel_window.present()

        self._apply_theme_class()
        self._refresh_cards()
        GLib.timeout_add(REFRESH_INTERVAL_MS, self._on_tick)

    # ------------------------------------------------------------ 设置（控制器）

    def _init_settings(self) -> None:
        """加载「设置覆盖样式」并监听设置文件（控制器改设置后实时生效）。"""
        self._override_provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            self._override_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_USER + 2,
        )
        self._refresh_style_override()
        settings.sync_blur_radius(self.settings["radius"])

        self._settings_source = 0
        self._settings_monitor = None
        try:
            os.makedirs(settings.CONFIG_DIR, exist_ok=True)
            monitor = Gio.File.new_for_path(settings.CONFIG_DIR).monitor_directory(
                Gio.FileMonitorFlags.NONE, None)
            monitor.connect("changed", self._on_settings_changed)
            self._settings_monitor = monitor
        except GLib.Error:
            pass

    def _refresh_style_override(self) -> None:
        """把圆角 / 不透明度写进覆盖样式（优先级高于 style.css）。"""
        css = settings.style_css(self.settings)
        try:
            self._override_provider.load_from_string(css)
        except AttributeError:  # GTK < 4.12
            self._override_provider.load_from_data(css.encode("utf-8"))

    def _on_settings_changed(self, _monitor, _file, _other, event) -> None:
        if event not in (Gio.FileMonitorEvent.CHANGED, Gio.FileMonitorEvent.CREATED,
                         Gio.FileMonitorEvent.RENAMED, Gio.FileMonitorEvent.CHANGES_DONE_HINT):
            return
        if self._settings_source:
            GLib.source_remove(self._settings_source)
        # 合并连续写入（拖动滑块时避免抖动）
        self._settings_source = GLib.timeout_add(150, self._apply_settings_file)

    def _apply_settings_file(self) -> bool:
        self._settings_source = 0
        self._apply_settings(settings.load())
        return GLib.SOURCE_REMOVE

    def _apply_settings(self, new: dict) -> None:
        old = self.settings
        self.settings = new

        if (new["radius"], new["opacity"]) != (old["radius"], old["opacity"]):
            self._refresh_style_override()
            settings.sync_blur_radius(new["radius"])

        if new["dark"] != old["dark"]:
            self._apply_theme_class()

        size_changed = (new["card_width"], new["gap"]) != (old["card_width"], old["gap"])
        position_changed = (new["margin_x"], new["margin_top"]) != (old["margin_x"], old["margin_top"])
        if size_changed:
            self._resize_cards()
        if size_changed or position_changed:
            # 尺寸 / 位置参数变了：旧坐标作废，按新布局重排
            self._saved = {}
            save_saved_positions({})
            self._last_seen = {}
            if self.mover.ok and self.windows:
                # 等 GTK 完成窗口重新分配后再排布（否则坐标会按旧尺寸计算而偏移）
                GLib.timeout_add(250, self._arrange_after_resize)

        if new["reset_token"] != old["reset_token"]:
            self._on_reset()

    def _resize_cards(self) -> None:
        width = self.settings["card_width"]
        wide = width * 2 + self.settings["gap"]
        for sid, card in self.cards.items():
            card.resize(wide if sid in WIDE_IDS else width)

    def _arrange_after_resize(self) -> bool:
        self._arrange(use_saved=False)
        return GLib.SOURCE_REMOVE

    def _load_css(self) -> None:
        """把 style.css 加载到整个显示器（所有贴纸共用）。

        优先级要比 GTK 的「用户样式」（~/.config/gtk-4.0/gtk.css，USER=800）更高：
        否则第三方主题里针对 window / .card / button 这些通用名称的规则会覆盖贴纸样式。
        """
        provider = Gtk.CssProvider()
        try:
            provider.load_from_file(Gio.File.new_for_path(CSS_FILE))  # GTK >= 4.12
        except AttributeError:  # pragma: no cover
            provider.load_from_path(CSS_FILE)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_USER + 1,
        )

    def _create_cards(self) -> None:
        width = self.settings["card_width"]
        wide = width * 2 + self.settings["gap"]
        self.cards = {
            "clock": ClockCard(width=wide),
            "cpu": RingCard("处理器", BLUE, width=width),
            "mem": RingCard("内存", PURPLE, width=width),
            "disk": BarCard("磁盘 /", TEAL, width=width),
            "net": NetCard(width=width),
            "sys": SystemCard(width=wide),
            "music": MusicCard(width=wide),
            "thermal": ThermalCard(width=width),
            "files": RecentFilesCard(width=wide),
            "proc": ProcessCard(width=wide),
            "api": ApiCard(width=wide),
        }
        if self.stats.has_battery:
            self.cards["battery"] = BatteryCard(width=width)

    def _create_sticker_windows(self) -> None:
        for sticker_id, card in self.cards.items():
            window = StickerWindow(self, sticker_id, card)
            window.set_opacity(0.0)  # 排布完成前先隐藏，避免看到窗口跳动
            window.connect("map", self._on_window_mapped)
            self.windows[sticker_id] = window
        for window in self.windows.values():
            window.present()

    def _on_window_mapped(self, _window) -> None:
        self._mapped_count += 1
        if self._mapped_count == len(self.windows):
            self._arrange_retries = 0
            GLib.timeout_add(150, self._try_arrange)

    # ------------------------------------------------------------ 排布与定位

    def _try_arrange(self) -> bool:
        self._arrange_retries += 1
        ready = all(
            window.get_height() > 1 and window.get_width() > 1
            for window in self.windows.values()
        )
        if not ready and self._arrange_retries < 15:
            return GLib.SOURCE_CONTINUE
        self._arrange(use_saved=True)
        return GLib.SOURCE_REMOVE

    def _arrange(self, use_saved: bool) -> None:
        """计算目标位置并开始移动（成对贴纸高度对齐，看起来更整齐）。"""
        scale = max((w.get_scale_factor() for w in self.windows.values()), default=1)
        heights = {sid: w.get_height() for sid, w in self.windows.items()}

        # 同一行的两张贴纸取较高者，把矮的垫到同高
        # 说明：size_request 的高度是「含边框/内边距」的卡片总高，
        #       而窗口高度 = 卡片总高 + 上下各一个 WINDOW_MARGIN。
        final_heights = dict(heights)
        for left, right in PAIRS:
            if left in heights and right in heights:
                target = max(heights[left], heights[right])
                final_heights[left] = final_heights[right] = target
                card_height = target - 2 * WINDOW_MARGIN
                for sid in (left, right):
                    card = self.cards[sid]
                    req_w, _ = card.get_size_request()
                    card.set_size_request(req_w if req_w > 0 else self.settings["card_width"], card_height)

        layout = compute_layout(final_heights, self.settings["card_width"], self.settings["gap"],
                                self.settings["margin_x"], self.settings["margin_top"])
        self._stretch_api_card(final_heights, layout)
        screen_w, screen_h = self.mover.screen_size()
        right_anchors = self._right_anchors(final_heights, scale, screen_w)

        if os.environ.get("STICKERS_DEBUG"):
            for sid, window in self.windows.items():
                card = self.cards[sid]
                print(f"[debug] {sid}: win={window.get_width()}x{window.get_height()} "
                      f"card={card.get_width()}x{card.get_height()} "
                      f"req={card.get_size_request()} "
                      f"measure={card.measure(Gtk.Orientation.HORIZONTAL, -1)[:2]}/{card.measure(Gtk.Orientation.VERTICAL, -1)[:2]} "
                      f"heights(before)={heights[sid]} final={final_heights[sid]} "
                      f"layout={layout.get(sid)} scale={scale}")

        targets = {}
        for sid, window in self.windows.items():
            device_w = max(int(window.get_width() * scale), 1)
            device_h = max(int(window.get_height() * scale), 1)
            saved = self._saved.get(sid)
            # 宽度不一致（卡片改版/换缩放）时旧坐标作废，回到默认排布
            if use_saved and saved is not None and abs(saved[2] - device_w) <= 4:
                x, y = clamp_position(saved[0], saved[1], screen_w, screen_h, device_w, device_h)
            elif sid in right_anchors:
                x, y = right_anchors[sid]
            else:
                lx, ly = layout.get(sid, (self.settings["margin_x"], self.settings["margin_top"]))
                x, y = int(round(lx * scale)), int(round(ly * scale))
            targets[sid] = (x, y)

        self._targets = targets
        self._moving = True
        self._place_attempts = 0
        self._stable_rounds = 0
        GLib.timeout_add(150, self._place_step)

    def _stretch_api_card(self, heights: dict, layout: dict) -> None:
        """让右列最后一张（API 卡）纵向拉伸到与左列底部齐平。"""
        card = self.cards.get("api")
        if card is None or "api" not in heights:
            return
        window_gap = max(0, self.settings["gap"] - 2 * WINDOW_MARGIN)
        left_bottom = max(
            (y + heights[sid] for sid, (x, y) in layout.items() if sid not in RIGHT_COLUMN),
            default=0)
        api_top = self.settings["margin_top"]
        for sid in RIGHT_COLUMN:
            if sid == "api":
                break
            if sid in heights:
                api_top += heights[sid] + window_gap
        desired = int(left_bottom - api_top) - 2 * WINDOW_MARGIN
        natural = card.measure(Gtk.Orientation.VERTICAL, -1)[1]
        width = int(self.settings["card_width"] * 2 + self.settings["gap"])
        card.set_size_request(width, max(desired, natural, 1))

    def _right_anchors(self, heights: dict, scale: int, screen_w: int) -> dict:
        """右上角一列的目标坐标（设备像素）；仅用于没有位置记忆的贴纸。"""
        margin_x = self.settings["margin_x"]
        window_gap = max(0, self.settings["gap"] - 2 * WINDOW_MARGIN)
        anchors = {}
        y = self.settings["margin_top"]
        for sid in RIGHT_COLUMN:
            window = self.windows.get(sid)
            if window is None or sid not in heights:
                continue
            device_w = max(int(window.get_width() * scale), 1)
            x = screen_w - device_w - int(round(margin_x * scale))
            anchors[sid] = (x, int(round(y * scale)))
            y += heights[sid] + window_gap
        return anchors

    def _place_step(self) -> bool:
        """把每张贴纸移到目标位置；连续两轮稳定后显示出来。"""
        if not self._moving:
            return GLib.SOURCE_REMOVE
        self._place_attempts += 1
        all_stable = True
        for sid, window in self.windows.items():
            target = self._targets.get(sid)
            xid = xid_of(window)
            if not xid or target is None:
                all_stable = False
                continue
            geometry = self.mover.geometry(xid)
            if geometry is None:
                all_stable = False
                continue
            if abs(geometry[0] - target[0]) > 2 or abs(geometry[1] - target[1]) > 2:
                self.mover.move(xid, *target)
                all_stable = False
        self._stable_rounds = self._stable_rounds + 1 if all_stable else 0
        if self._stable_rounds >= 2 or self._place_attempts >= 15:
            self._finish_movement()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _finish_movement(self) -> None:
        self._moving = False
        self._position_ready = True
        if os.environ.get("STICKERS_DEBUG"):
            for sid, window in self.windows.items():
                card = self.cards[sid]
                print(f"[debug] final {sid}: win={window.get_width()}x{window.get_height()} "
                      f"card={card.get_width()}x{card.get_height()}")
        for window in self.windows.values():
            window.set_opacity(1.0)
        # 启动后内容（最近文件 / 封面等）可能让窗口宽度稍变，稍等片刻精确贴边一次
        GLib.timeout_add(1500, self._settle_anchors)

    def _settle_anchors(self) -> bool:
        """启动完成后把「仍然贴边」的卡片精确对齐（只校正接近锚点的卡片，不打扰拖动过的）。"""
        if not self.mover.ok:
            return GLib.SOURCE_REMOVE
        scale = max((w.get_scale_factor() for w in self.windows.values()), default=1)
        screen_w, _ = self.mover.screen_size()
        margin = int(round(self.settings["margin_x"] * scale))
        for sid, window in self.windows.items():
            xid = xid_of(window)
            geometry = self.mover.geometry(xid) if xid else None
            if geometry is None:
                continue
            x, y, w, _ = geometry
            if sid in RIGHT_COLUMN:
                want = screen_w - w - margin
            else:
                want = margin
            if x != want and abs(x - want) <= 60:
                self.mover.move(xid, want, y)
        return GLib.SOURCE_REMOVE

    def _track_positions(self) -> None:
        """每秒跟踪：拖动停下后记住位置；窗口尺寸变化时保持贴边对齐。"""
        if not self._position_ready or not self.mover.ok:
            return
        changed = False
        scale = max((w.get_scale_factor() for w in self.windows.values()), default=1)
        screen_w, _ = self.mover.screen_size()
        margin = int(round(self.settings["margin_x"] * scale))

        for sid, window in self.windows.items():
            xid = xid_of(window)
            geometry = self.mover.geometry(xid) if xid else None
            if geometry is None:
                continue
            x, y, w, h = geometry
            previous_size = self._last_sizes.get(sid)
            self._last_sizes[sid] = (w, h)

            # 内容变化导致窗口宽度变了：原本贴边的卡片重新贴回边缘，避免出现错位
            if previous_size is not None and previous_size != (w, h):
                old_x = self._last_seen.get(sid, (x, y, w))[0]
                if sid in RIGHT_COLUMN:
                    if abs((old_x + previous_size[0]) - (screen_w - margin)) <= 6:
                        want_x = screen_w - w - margin
                        if abs(x - want_x) > 2:
                            self.mover.move(xid, want_x, y)
                            x = want_x
                elif abs(old_x - margin) <= 6 and abs(x - margin) > 2:
                    self.mover.move(xid, margin, y)
                    x = margin

            position = (x, y, w)
            if position == self._last_seen.get(sid) and position != self._saved.get(sid):
                self._saved[sid] = position
                changed = True
            self._last_seen[sid] = position
        if changed:
            save_saved_positions(self._saved)

    # ------------------------------------------------------------ 动作

    def _install_actions(self) -> None:
        toggle = Gio.SimpleAction.new("toggle-theme", None)
        toggle.connect("activate", self._on_toggle_theme)
        self.add_action(toggle)

        reset = Gio.SimpleAction.new("reset-stickers", None)
        reset.connect("activate", self._on_reset)
        self.add_action(reset)

        control = Gio.SimpleAction.new("open-control", None)
        control.connect("activate", self._on_open_control)
        self.add_action(control)

        # 顶栏歌词插件开关（勾选状态跟随插件进程）
        self._lyrics_action = Gio.SimpleAction.new_stateful(
            "toggle-lyrics", None, GLib.Variant("b", False))
        self._lyrics_action.connect("activate", self._on_toggle_lyrics)
        self.add_action(self._lyrics_action)
        Gio.bus_watch_name(
            Gio.BusType.SESSION, CONTROL_NAME, Gio.BusNameWatcherFlags.NONE,
            self._on_lyrics_appeared, self._on_lyrics_vanished)

        # 灵动岛开关（勾选状态跟随灵动岛进程；名字与 island.py 的 APP_ID 一致）
        self._island_action = Gio.SimpleAction.new_stateful(
            "toggle-island", None, GLib.Variant("b", False))
        self._island_action.connect("activate", self._on_toggle_island)
        self.add_action(self._island_action)
        Gio.bus_watch_name(
            Gio.BusType.SESSION, ISLAND_NAME, Gio.BusNameWatcherFlags.NONE,
            self._on_island_appeared, self._on_island_vanished)

    def _on_lyrics_appeared(self, _conn, _name, _owner) -> None:
        self._lyrics_action.set_state(GLib.Variant("b", True))

    def _on_lyrics_vanished(self, _conn, _name) -> None:
        self._lyrics_action.set_state(GLib.Variant("b", False))

    def _on_toggle_lyrics(self, _action, _param) -> None:
        """在当前进程外切换顶栏歌词插件（独立进程、状态会被记住）。"""
        script = os.path.join(BASE_DIR, "lyrics_tray.py")
        subprocess.Popen(
            [sys.executable, script, "--toggle"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)

    def _on_island_appeared(self, _conn, _name, _owner) -> None:
        self._island_action.set_state(GLib.Variant("b", True))

    def _on_island_vanished(self, _conn, _name) -> None:
        self._island_action.set_state(GLib.Variant("b", False))

    def _on_toggle_island(self, _action, _param) -> None:
        """在当前进程外开 / 关灵动岛（独立进程、状态会被记住，自启也尊重它）。"""
        subprocess.Popen(
            [sys.executable, os.path.join(BASE_DIR, "island.py"), "--toggle"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)

    def _on_open_control(self, _action, _param) -> None:
        """打开设置控制器窗口（独立进程）。"""
        subprocess.Popen(
            [sys.executable, os.path.join(BASE_DIR, "control.py")],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)

    def _root_boxes(self) -> list[Gtk.Box]:
        roots = [window.root_box for window in self.windows.values()]
        if self.panel_window is not None:
            roots.append(self.panel_window.root_box)
        return roots

    def _on_toggle_theme(self, *_args) -> None:
        self.settings["dark"] = not self.settings.get("dark", True)
        settings.save(self.settings)
        self._apply_theme_class()

    def _apply_theme_class(self) -> None:
        """按设置给所有贴纸套上深色 / 浅色玻璃类。"""
        dark = bool(self.settings.get("dark", True))
        for root in self._root_boxes():
            root.remove_css_class("light" if dark else "dark")
            root.add_css_class("dark" if dark else "light")

    def _on_reset(self, *_args) -> None:
        """清空记忆，把全部贴纸排回左上角。"""
        self._saved = {}
        save_saved_positions({})
        self._last_seen = {}
        if not self.mover.ok or not self.windows:
            return
        self._arrange(use_saved=False)

    # ------------------------------------------------------------ 数据刷新

    def _on_tick(self) -> bool:
        self._refresh_cards()
        self._track_positions()
        return GLib.SOURCE_CONTINUE

    def _refresh_cards(self) -> None:
        data = self.stats.sample()
        self.cards["clock"].refresh()

        # CPU：圆环 + 温度 / 频率
        cpu = data["cpu"]
        parts = []
        if data["cpu_temp"] is not None:
            parts.append(f"{data['cpu_temp']:.0f} °C")
        if data["cpu_freq"]:
            parts.append(f"{data['cpu_freq'] / 1000:.2f} GHz")
        if not parts:
            parts.append(f"{self.stats.cpu_cores} 线程")
        self.cards["cpu"].refresh(cpu, f"{cpu:.0f}%", " · ".join(parts))

        # 内存：圆环 + 已用/总量 + 交换分区
        mem = data["mem"]
        swap = data["swap"]
        if swap["total"]:
            mem_sub = f"交换 {fmt_bytes(swap['used'])} / {fmt_bytes(swap['total'])}"
        else:
            mem_sub = "无交换分区"
        self.cards["mem"].refresh(
            mem["percent"],
            f"{fmt_bytes(mem['used'])} / {fmt_bytes(mem['total'])}",
            mem_sub,
        )

        # 磁盘
        disk = data["disk"]
        self.cards["disk"].refresh(
            disk["percent"],
            f"{fmt_bytes(disk['used'])} / {fmt_bytes(disk['total'])}",
            f"可用 {fmt_bytes(disk['free'])}",
        )

        # 网络
        self.cards["net"].refresh(data["net"]["down"], data["net"]["up"])

        # 电池
        if "battery" in self.cards and data["battery"] is not None:
            self.cards["battery"].refresh_battery(data["battery"])

        # 系统信息
        load1, load5, load15 = data["load"]
        self.cards["sys"].refresh(
            {
                "主机": self.stats.hostname,
                "系统": self.stats.os_name,
                "内核": self.stats.kernel,
                "运行": fmt_uptime(data["uptime"]),
                "负载": f"{load1:.2f} · {load5:.2f} · {load15:.2f}",
            }
        )

        # 音乐：MPRIS 当前播放（没有播放器时显示空闲态）
        self.cards["music"].refresh(data.get("music"))

        # 温度 / 风扇
        self.cards["thermal"].refresh_thermal(data.get("cpu_temp"), data.get("fan"))

        # 进程 Top 3
        self.cards["proc"].refresh(data.get("proc", []))

        # 常用文件（最近经常打开；内部有缓存，列表变了才重建）
        self.cards["files"].refresh()

        # API 用量（OpenCode 花费 / token + 厂商余额，内部有缓存与后台刷新）
        self.cards["api"].refresh(self.settings)


def main() -> int:
    GLib.set_application_name("系统贴纸")
    GLib.set_prgname("sysstickers")
    return StickerApp().run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
