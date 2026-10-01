#!/usr/bin/env python3
"""贴纸设置控制器：实时调整卡片尺寸 / 位置 / 圆角 / 不透明度。

用法：
  - 任意贴纸右键菜单 →「贴纸设置…」
  - 应用菜单 →「贴纸设置」
  - 或命令行：python3 control.py

改动会即时写入 ~/.config/sysstickers/settings.json，
正在运行的贴纸会立刻应用（不需要重启）。
"""

from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

import lyrics_tray  # noqa: E402
import settings  # noqa: E402

APP_ID = "com.loong.SysStickersControl"

# (键, 名称, 最小, 最大, 步进, 小数位)
SLIDERS = (
    ("card_width", "卡片宽度", 110, 260, 2, 0),
    ("radius", "圆角", 0, 40, 1, 0),
    ("opacity", "玻璃不透明度", 0.15, 0.98, 0.01, 2),
    ("gap", "卡片间距", 0, 80, 1, 0),
    ("margin_x", "屏幕边距（左右）", 0, 300, 2, 0),
    ("margin_top", "屏幕顶部边距", 0, 300, 2, 0),
)


class ControlWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application):
        super().__init__(application=app, title="贴纸设置")
        self.set_resizable(False)
        self.values = settings.load()
        self._save_source = 0
        self._syncing = False

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for margin in ("top", "bottom", "start", "end"):
            getattr(root, f"set_margin_{margin}")(18)
        self.set_child(root)

        heading = Gtk.Label(label="卡片外观")
        heading.add_css_class("heading")
        heading.set_halign(Gtk.Align.START)
        root.append(heading)

        grid = Gtk.Grid(column_spacing=14, row_spacing=10)
        root.append(grid)

        for row, (key, label, low, high, step, digits) in enumerate(SLIDERS):
            name = Gtk.Label(label=label)
            name.set_halign(Gtk.Align.START)
            scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, low, high, step)
            scale.set_hexpand(True)
            scale.set_draw_value(True)
            scale.set_digits(digits)
            scale.set_value(float(self.values.get(key, settings.DEFAULTS[key])))
            scale.connect("value-changed", self._on_value_changed, key)
            grid.attach(name, 0, row, 1, 1)
            grid.attach(scale, 1, row, 1, 1)

        root.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        switches = Gtk.Grid(column_spacing=14, row_spacing=10)
        root.append(switches)

        dark_label = Gtk.Label(label="深色玻璃")
        dark_label.set_halign(Gtk.Align.START)
        self._dark_switch = Gtk.Switch()
        self._dark_switch.set_active(bool(self.values.get("dark", True)))
        self._dark_switch.set_halign(Gtk.Align.START)
        self._dark_switch.connect("notify::active", self._on_dark_toggled)
        switches.attach(dark_label, 0, 0, 1, 1)
        switches.attach(self._dark_switch, 1, 0, 1, 1)

        lyrics_label = Gtk.Label(label="顶栏歌词")
        lyrics_label.set_halign(Gtk.Align.START)
        self._lyrics_switch = Gtk.Switch()
        self._lyrics_switch.set_active(lyrics_tray.is_running())
        self._lyrics_switch.set_halign(Gtk.Align.START)
        self._lyrics_switch.connect("notify::active", self._on_lyrics_toggled)
        switches.attach(lyrics_label, 0, 1, 1, 1)
        switches.attach(self._lyrics_switch, 1, 1, 1, 1)

        reset = Gtk.Button(label="复位贴纸位置")
        reset.set_halign(Gtk.Align.START)
        reset.connect("clicked", self._on_reset)
        root.append(reset)

        hint = Gtk.Label(
            label="改动会实时生效，贴纸立即变化；拖动贴纸可以微调各自的位置。")
        hint.add_css_class("dim-label")
        hint.set_wrap(True)
        hint.set_halign(Gtk.Align.START)
        root.append(hint)

        # 跟随外部变化（比如贴纸右键切换了主题 / 插件菜单里关了歌词）
        self._watch_source = GLib.timeout_add_seconds(2, self._sync_external)
        self.connect("destroy", self._on_destroy)

    # ------------------------------------------------------------ 保存

    def _queue_save(self) -> None:
        if self._save_source:
            GLib.source_remove(self._save_source)
        self._save_source = GLib.timeout_add(150, self._save_now)

    def _save_now(self) -> bool:
        self._save_source = 0
        settings.save(self.values)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------ 控件回调

    def _on_value_changed(self, scale: Gtk.Scale, key: str) -> None:
        value = scale.get_value()
        if key in ("card_width", "radius", "gap", "margin_x", "margin_top"):
            value = int(round(value))
        self.values[key] = value
        self._queue_save()

    def _on_dark_toggled(self, switch: Gtk.Switch, _param) -> None:
        if self._syncing:
            return
        self.values["dark"] = bool(switch.get_active())
        self._queue_save()

    def _on_lyrics_toggled(self, switch: Gtk.Switch, _param) -> None:
        if self._syncing:
            return
        if switch.get_active():
            lyrics_tray.start_daemon()
        else:
            lyrics_tray.stop_daemon()

    def _on_reset(self, _button) -> None:
        self.values = settings.bump_reset(self.values)
        self._save_now()

    def _sync_external(self) -> bool:
        """同步外部变化（不打断正在拖动的滑块）。"""
        self._syncing = True
        try:
            running = lyrics_tray.is_running()
            if self._lyrics_switch.get_active() != running:
                self._lyrics_switch.set_active(running)
            current = settings.load()
            if bool(self._dark_switch.get_active()) != bool(current.get("dark", True)):
                self._dark_switch.set_active(bool(current.get("dark", True)))
                self.values["dark"] = bool(current.get("dark", True))
        finally:
            self._syncing = False
        return GLib.SOURCE_CONTINUE

    def _on_destroy(self, _window) -> None:
        if self._watch_source:
            GLib.source_remove(self._watch_source)
            self._watch_source = 0
        if self._save_source:
            GLib.source_remove(self._save_source)
            self._save_source = 0
            settings.save(self.values)


class ControlApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.window: ControlWindow | None = None

    def do_activate(self) -> None:
        if self.window is None:
            self.window = ControlWindow(self)
        self.window.present()


def main() -> int:
    return ControlApp().run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
