"""贴纸设置：卡片尺寸 / 位置 / 圆角 / 不透明度。

- 控制器（control.py）通过写这个文件来调整设置
- 主程序（main.py）监听文件变化并实时应用
- 文件位置：~/.config/sysstickers/settings.json
"""

from __future__ import annotations

import json
import os

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "sysstickers")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")

DEFAULTS: dict = {
    "card_width": 156,   # 普通卡片宽度；宽卡 = 2×宽 + 间距
    "radius": 16,        # 卡片圆角（像素）
    "opacity": 0.55,     # 深色玻璃不透明度（浅色自动 +0.13）
    "margin_x": 18,      # 贴纸组距屏幕左 / 右边缘
    "margin_top": 46,    # 距屏幕顶部（避开 GNOME 顶栏）
    "gap": 24,           # 卡片之间的间距
    "dark": True,        # 深色玻璃（False = 浅色）
    "reset_token": 0,    # +1 表示请求复位所有贴纸位置
}

_RANGES = {
    "card_width": (110, 260),
    "radius": (0, 40),
    "opacity": (0.15, 0.98),
    "margin_x": (0, 400),
    "margin_top": (0, 400),
    "gap": (0, 80),
}

WINDOW_MARGIN = 9  # 每个窗口留给阴影的空白（与 main.py 保持一致）


def load() -> dict:
    """读取设置（缺失或非法值自动回退默认值）。"""
    values = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return values

    for key, default in DEFAULTS.items():
        if key not in data:
            continue
        value = data[key]
        try:
            if isinstance(default, bool):
                values[key] = bool(value)
            elif isinstance(default, int):
                value = int(value)
                low, high = _RANGES.get(key, (None, None))
                if low is not None:
                    value = max(low, min(high, value))
                values[key] = value
            elif isinstance(default, float):
                value = float(value)
                low, high = _RANGES.get(key, (None, None))
                if low is not None:
                    value = max(low, min(high, value))
                values[key] = value
        except (TypeError, ValueError):
            continue
    values["reset_token"] = max(0, int(values.get("reset_token", 0)))
    return values


def save(values: dict) -> None:
    """原子写入设置文件。"""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(SETTINGS_FILE + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(values, fh, ensure_ascii=False, indent=1)
        os.replace(SETTINGS_FILE + ".tmp", SETTINGS_FILE)
    except OSError:
        pass


def bump_reset(values: dict) -> dict:
    values = dict(values)
    values["reset_token"] = int(values.get("reset_token", 0)) + 1
    return values


def style_css(values: dict) -> str:
    """生成覆盖 style.css 的少量规则：圆角 + 玻璃不透明度。"""
    radius = int(values.get("radius", DEFAULTS["radius"]))
    opacity = float(values.get("opacity", DEFAULTS["opacity"]))
    light = min(0.98, opacity + 0.13)
    return (
        f".card {{ border-radius: {radius}px; }}\n"
        f".dark .card {{ background-color: rgba(22, 24, 32, {opacity:.3f}); }}\n"
        f".light .card {{ background-color: rgba(255, 255, 255, {light:.3f}); }}\n"
    )


def sync_blur_radius(radius: int) -> bool:
    """把 Blur my Shell 的应用程序圆角同步成「卡片圆角 + 窗口留白」，保持同心。"""
    schema_dir = os.path.join(
        os.path.expanduser("~"),
        ".local/share/gnome-shell/extensions/blur-my-shell@aunetx/schemas")
    if not os.path.isdir(schema_dir):
        return False
    try:
        from gi.repository import Gio

        source = Gio.SettingsSchemaSource.new_from_directory(
            schema_dir, Gio.SettingsSchemaSource.get_default(), False)
        schema = source.lookup("org.gnome.shell.extensions.blur-my-shell.applications", True)
        if schema is None:
            return False
        settings = Gio.Settings.new_full(schema, None, None)
        settings.set_int("corner-radius", int(radius) + WINDOW_MARGIN)
        return True
    except Exception:
        return False


if __name__ == "__main__":  # 自测：python3 settings.py
    current = load()
    print("当前设置:", json.dumps(current, ensure_ascii=False))
    print("生成样式:")
    print(style_css(current))
