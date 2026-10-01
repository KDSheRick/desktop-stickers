"""顶栏歌词插件（Ubuntu AppIndicator 标签）。

独立的「插件 app」：可以单独启动/停止，不依赖贴纸主程序。

功能：
  - 在 GNOME 顶栏（托盘区域）显示当前歌词句，随播放进度滚动；
  - 托盘菜单：播放 / 暂停、上一首、下一首、关闭顶栏歌词；
  - 单击托盘图标 = 播放 / 暂停；
  - 没有播放内容时自动隐藏。

开关（会记忆状态，重启后保持）：
  python3 lyrics_tray.py --status    # 查看运行状态
  python3 lyrics_tray.py --start     # 打开
  python3 lyrics_tray.py --stop      # 关闭
  python3 lyrics_tray.py --toggle    # 切换
  python3 lyrics_tray.py             # 前台运行（调试用）

也可以从任意贴纸的右键菜单 →「顶栏歌词」直接开关。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

from gi.repository import Gio, GLib

import covers
import lyrics

# 控制名：既用于单实例判定，也用于 --stop/--status
CONTROL_NAME = "com.loong.SysStickersLyrics"
_CONTROL_PATH = "/Control"

_WATCHER_NAME = "org.kde.StatusNotifierWatcher"
_WATCHER_PATH = "/StatusNotifierWatcher"
_WATCHER_IFACE = "org.kde.StatusNotifierWatcher"

_ITEM_PATH = "/StatusNotifierItem"
_MENU_PATH = "/MenuBar"
_PLAYER_PATH = "/org/mpris/MediaPlayer2"
_PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"

_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "sysstickers")
_STATE_FILE = os.path.join(_CONFIG_DIR, "lyrics-tray.json")
_LOG_FILE = os.path.join(os.path.expanduser("~"), ".cache", "sysstickers", "lyrics-tray.log")

# 顶栏文字最大显示宽度（中文算 2），超出省略，避免把顶栏撑太长
MAX_CELLS = 44
_TICK_MS = 250

_ITEM_XML = """
<node>
  <interface name='org.kde.StatusNotifierItem'>
    <property name='Category' type='s' access='read'/>
    <property name='Id' type='s' access='read'/>
    <property name='Title' type='s' access='read'/>
    <property name='Status' type='s' access='read'/>
    <property name='IconName' type='s' access='read'/>
    <property name='IconPixmap' type='a(iiay)' access='read'/>
    <property name='IconThemePath' type='s' access='read'/>
    <property name='Menu' type='o' access='read'/>
    <property name='ItemIsMenu' type='b' access='read'/>
    <property name='XAyatanaLabel' type='s' access='read'/>
    <property name='XAyatanaLabelGuide' type='s' access='read'/>
    <method name='Activate'>
      <arg name='x' type='i' direction='in'/>
      <arg name='y' type='i' direction='in'/>
    </method>
    <method name='SecondaryActivate'>
      <arg name='x' type='i' direction='in'/>
      <arg name='y' type='i' direction='in'/>
    </method>
    <method name='ContextMenu'>
      <arg name='x' type='i' direction='in'/>
      <arg name='y' type='i' direction='in'/>
    </method>
    <method name='Scroll'>
      <arg name='delta' type='i' direction='in'/>
      <arg name='orientation' type='s' direction='in'/>
    </method>
    <signal name='NewTitle'/>
    <signal name='NewIcon'/>
    <signal name='NewStatus'>
      <arg name='status' type='s'/>
    </signal>
    <signal name='NewToolTip'/>
    <signal name='XAyatanaNewLabel'>
      <arg name='label' type='s'/>
      <arg name='guide' type='s'/>
    </signal>
  </interface>
</node>
"""

_MENU_XML = """
<node>
  <interface name='com.canonical.dbusmenu'>
    <property name='Version' type='u' access='read'/>
    <property name='TextDirection' type='s' access='read'/>
    <property name='Status' type='s' access='read'/>
    <method name='GetLayout'>
      <arg name='parentId' type='i' direction='in'/>
      <arg name='recursionDepth' type='i' direction='in'/>
      <arg name='propertyNames' type='as' direction='in'/>
      <arg name='revision' type='u' direction='out'/>
      <arg name='layout' type='(ia{sv}av)' direction='out'/>
    </method>
    <method name='GetGroupProperties'>
      <arg name='ids' type='ai' direction='in'/>
      <arg name='propertyNames' type='as' direction='in'/>
      <arg name='properties' type='a(ia{sv})' direction='out'/>
    </method>
    <method name='GetProperty'>
      <arg name='id' type='i' direction='in'/>
      <arg name='name' type='s' direction='in'/>
      <arg name='value' type='v' direction='out'/>
    </method>
    <method name='Event'>
      <arg name='id' type='i' direction='in'/>
      <arg name='eventId' type='s' direction='in'/>
      <arg name='data' type='v' direction='in'/>
      <arg name='timestamp' type='u' direction='in'/>
    </method>
    <method name='AboutToShow'>
      <arg name='id' type='i' direction='in'/>
      <arg name='needUpdate' type='b' direction='out'/>
    </method>
    <method name='AboutToShowGroup'>
      <arg name='ids' type='ai' direction='in'/>
      <arg name='updatesNeeded' type='ai' direction='out'/>
      <arg name='idErrors' type='ai' direction='out'/>
    </method>
    <signal name='ItemsPropertiesUpdated'>
      <arg name='updatedProps' type='a(ia{sv})'/>
      <arg name='removedProps' type='a(ias)'/>
    </signal>
    <signal name='LayoutUpdated'>
      <arg name='revision' type='u'/>
      <arg name='parent' type='i'/>
    </signal>
  </interface>
</node>
"""

_CONTROL_XML = f"""
<node>
  <interface name='{CONTROL_NAME}'>
    <method name='Quit'/>
    <method name='Ping'/>
  </interface>
</node>
"""

_REVISION = 1
_MENU_LABELS = {
    1: "播放 / 暂停",
    2: "上一首",
    3: "下一首",
    4: "关闭顶栏歌词",
}


# ---------------------------------------------------------------- 开关状态

def load_enabled() -> bool:
    """插件是否处于开启状态（默认开启）。"""
    try:
        with open(_STATE_FILE, encoding="utf-8") as fh:
            return bool(json.load(fh).get("enabled", True))
    except (OSError, ValueError):
        return True


def store_enabled(enabled: bool) -> None:
    try:
        os.makedirs(_CONFIG_DIR, exist_ok=True)
        with open(_STATE_FILE + ".tmp", "w", encoding="utf-8") as fh:
            json.dump({"enabled": bool(enabled)}, fh)
        os.replace(_STATE_FILE + ".tmp", _STATE_FILE)
    except OSError:
        pass


def _control_call(method: str) -> bool:
    """调用正在运行的插件（Quit / Ping），成功返回 True。"""
    try:
        conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        conn.call_sync(CONTROL_NAME, _CONTROL_PATH, CONTROL_NAME, method, None, None,
                       Gio.DBusCallFlags.NONE, 1000, None)
        return True
    except GLib.Error:
        return False


def is_running() -> bool:
    try:
        conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = conn.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "NameHasOwner", GLib.Variant("(s)", (CONTROL_NAME,)),
            GLib.VariantType.new("(b)"), Gio.DBusCallFlags.NONE, 1000, None)
        return bool(reply.unpack()[0])
    except GLib.Error:
        return False


def spawn_daemon() -> None:
    """后台拉起插件进程（脱离当前进程组，日志写到缓存目录）。"""
    os.makedirs(os.path.dirname(_LOG_FILE), exist_ok=True)
    with open(_LOG_FILE, "ab") as log:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--run"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True)


def start_daemon() -> None:
    """打开插件（记住开启状态）。"""
    store_enabled(True)
    if not is_running():
        spawn_daemon()


def stop_daemon() -> None:
    """关闭插件（记住关闭状态）。"""
    if is_running():
        _control_call("Quit")
    store_enabled(False)


# ---------------------------------------------------------------- 托盘本体

def _item_props(item_id: int) -> dict:
    return {
        "label": GLib.Variant("s", _MENU_LABELS[item_id]),
        "type": GLib.Variant("s", "standard"),
        "enabled": GLib.Variant("b", True),
        "visible": GLib.Variant("b", True),
    }


def _item_variant(item_id: int) -> GLib.Variant:
    return GLib.Variant.new_tuple(
        GLib.Variant("i", item_id),
        GLib.Variant("a{sv}", _item_props(item_id)),
        GLib.Variant("av", []))


class LyricsTray:
    """把 MPRIS 播放信息变成顶栏上的歌词标签。"""

    def __init__(self, on_quit=None) -> None:
        self.ok = False
        self.already_running = False
        self._on_quit = on_quit
        self._conn = None
        self._label = ""
        self._status = "Passive"
        self._player_bus: str | None = None
        self._title = ""
        self._artist = ""
        self._lines: list[dict] = []
        self._track_key: tuple | None = None
        self._position = 0.0
        self._sampled_at = 0.0
        self._playing = False
        self._fetch_token = 0
        # 始终提供非空图标（无封面时是占位音符），避免 Shell 端拿到空 IconPixmap
        self._icon_pixmaps = covers.icon_pixmaps(None)
        self._timer = 0
        self._bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"

        try:
            self._conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)

            # 控制名（单实例）：已经有实例在跑就不再启动
            reply = self._conn.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                "RequestName", GLib.Variant("(su)", (CONTROL_NAME, 4)),  # 4 = DO_NOT_QUEUE
                GLib.VariantType.new("(u)"), Gio.DBusCallFlags.NONE, 1000, None)
            if reply.unpack()[0] != 1:  # 1 = PRIMARY_OWNER
                self.already_running = True
                return

            self._conn.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                "RequestName", GLib.Variant("(su)", (self._bus_name, 0)),
                GLib.VariantType.new("(u)"), Gio.DBusCallFlags.NONE, 1000, None)

            self._conn.register_object(
                _ITEM_PATH, Gio.DBusNodeInfo.new_for_xml(_ITEM_XML).interfaces[0],
                self._on_item_method, self._on_item_property, None)
            self._conn.register_object(
                _MENU_PATH, Gio.DBusNodeInfo.new_for_xml(_MENU_XML).interfaces[0],
                self._on_menu_method, self._on_menu_property, None)
            self._conn.register_object(
                _CONTROL_PATH, Gio.DBusNodeInfo.new_for_xml(_CONTROL_XML).interfaces[0],
                self._on_control_method, None, None)
            self._conn.call_sync(
                _WATCHER_NAME, _WATCHER_PATH, _WATCHER_IFACE, "RegisterStatusNotifierItem",
                GLib.Variant("(s)", (self._bus_name,)), None,
                Gio.DBusCallFlags.NONE, 1000, None)
            self._timer = GLib.timeout_add(_TICK_MS, self._tick)
            self.ok = True
        except GLib.Error as exc:
            # 非 Ubuntu/无 AppIndicator 宿主时静默降级为不可用
            print(f"[lyrics] 顶栏歌词不可用：{exc.message}")

    # ------------------------------------------------------------ 对外接口

    def update(self, music: dict | None) -> None:
        """每秒调用一次，传入 MusicWatcher 的结果（没有则 None）。"""
        if not self.ok:
            return

        if music is None or music.get("blocked"):
            self._player_bus = None
            self._title = ""
            self._artist = ""
            self._playing = False
            self._set_status("Passive")
            self._set_label("")
            return

        self._player_bus = music.get("bus_name") or self._player_bus
        self._playing = music.get("status") == "Playing"
        position = music.get("position")
        if position is not None:
            self._position = float(position)
            self._sampled_at = GLib.get_monotonic_time() / 1e6

        title = music.get("title") or ""
        artist = music.get("artist") or ""
        album = music.get("album") or ""
        art = music.get("art") or ""
        key = (title, artist, album, art)
        if key != self._track_key:
            self._track_key = key
            self._lines = []
            if title:
                self._fetch_token += 1
                threading.Thread(
                    target=self._fetch_worker,
                    args=(self._fetch_token, title, artist, album, art),
                    daemon=True,
                ).start()

        self._title = title
        self._artist = artist
        # 没有歌词也要显示“♪ 歌名”，所以标题存在就算活跃
        self._set_status("Active" if title and music.get("status") != "Stopped" else "Passive")
        self._refresh_label()

    def request_quit(self) -> None:
        """关闭插件（托盘菜单 / 控制接口调用）。"""
        if self._on_quit is not None:
            self._on_quit()

    # ------------------------------------------------------------ 内部逻辑

    def _fetch_worker(self, token: int, title: str, artist: str, album: str, art: str) -> None:
        lines, cover_path = covers.load_track(title, artist, album, art or None)
        pixmaps = covers.icon_pixmaps(cover_path)
        GLib.idle_add(self._apply_track, token, lines or [], pixmaps)

    def _apply_track(self, token: int, lines: list[dict], pixmaps) -> bool:
        if token != self._fetch_token:
            return GLib.SOURCE_REMOVE
        self._lines = lines
        if pixmaps and pixmaps != self._icon_pixmaps:
            self._icon_pixmaps = pixmaps
            self._emit("NewIcon", None)
        self._refresh_label()
        return GLib.SOURCE_REMOVE

    def _tick(self) -> bool:
        self._refresh_label()
        return GLib.SOURCE_CONTINUE

    def _current_text(self) -> str:
        if not self._title:
            return ""
        line = ""
        if self._lines:
            position = self._position
            if self._playing:
                position += GLib.get_monotonic_time() / 1e6 - self._sampled_at
            line = lyrics.line_at(self._lines, position)
        if line:
            return line
        return self._title + (f" · {self._artist}" if self._artist else "")

    def _refresh_label(self) -> None:
        self._set_label(lyrics.truncate_display(self._current_text(), MAX_CELLS))

    def _set_label(self, text: str) -> None:
        if text == self._label:
            return
        self._label = text
        self._emit("XAyatanaNewLabel", GLib.Variant("(ss)", (text, "left")))

    def _set_status(self, status: str) -> None:
        if status == self._status:
            return
        self._status = status
        self._emit("NewStatus", GLib.Variant("(s)", (status,)))

    def _emit(self, signal: str, params: GLib.Variant) -> None:
        if not self.ok:
            return
        try:
            self._conn.emit_signal(None, _ITEM_PATH, "org.kde.StatusNotifierItem", signal, params)
        except GLib.Error:
            pass

    def _player_call(self, method: str) -> None:
        if not self.ok or not self._player_bus:
            return
        try:
            self._conn.call_sync(
                self._player_bus, _PLAYER_PATH, _PLAYER_IFACE, method, None, None,
                Gio.DBusCallFlags.NONE, 800, None)
        except GLib.Error:
            pass

    # ------------------------------------------------------------ SNI 属性

    def _on_item_property(self, _conn, _sender, _path, _iface, prop):
        values = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", "sysstickers-lyrics"),
            "Title": GLib.Variant("s", "歌词"),
            "Status": GLib.Variant("s", self._status),
            # IconName 留空：否则 AppIndicators 会优先用主题图标，忽略我们的封面位图
            "IconName": GLib.Variant("s", ""),
            "IconPixmap": GLib.Variant("a(iiay)", self._icon_pixmaps or []),
            "IconThemePath": GLib.Variant("s", ""),
            "Menu": GLib.Variant("o", _MENU_PATH),
            "ItemIsMenu": GLib.Variant("b", False),
            "XAyatanaLabel": GLib.Variant("s", self._label),
            "XAyatanaLabelGuide": GLib.Variant("s", "left"),
        }
        return values.get(prop)

    def _on_item_method(self, _conn, _sender, _path, _iface, method, _params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            self._player_call("PlayPause")
        # ContextMenu / Scroll 暂不处理
        invocation.return_value(None)

    # ------------------------------------------------------------ 控制接口

    def _on_control_method(self, _conn, _sender, _path, _iface, method, _params, invocation):
        if method == "Quit":
            self.request_quit()
        invocation.return_value(None)

    # ------------------------------------------------------------ 菜单实现

    def _on_menu_property(self, _conn, _sender, _path, _iface, prop):
        values = {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
        }
        return values.get(prop)

    def _on_menu_method(self, _conn, _sender, _path, _iface, method, params, invocation):
        try:
            if method == "GetLayout":
                children = GLib.Variant("av", [
                    _item_variant(item_id) for item_id in (1, 2, 3, 4)])
                root = GLib.Variant.new_tuple(
                    GLib.Variant("i", 0),
                    GLib.Variant("a{sv}", {
                        "children-display": GLib.Variant("s", "submenu"),
                    }),
                    children)
                invocation.return_value(
                    GLib.Variant.new_tuple(GLib.Variant("u", _REVISION), root))
            elif method == "GetGroupProperties":
                ids = params.unpack()[0]
                properties = [(item_id, _item_props(item_id))
                              for item_id in ids if item_id in _MENU_LABELS]
                invocation.return_value(GLib.Variant("(a(ia{sv}))", (properties,)))
            elif method == "GetProperty":
                item_id, name = params.unpack()
                value = _item_props(item_id).get(name) if item_id in _MENU_LABELS else None
                if value is None:
                    raise KeyError(name)
                invocation.return_value(GLib.Variant("(v)", (value,)))
            elif method == "Event":
                item_id, event_id = params.unpack()[0], params.unpack()[1]
                if event_id == "clicked":
                    self._menu_action(item_id)
                invocation.return_value(None)
            elif method == "AboutToShow":
                invocation.return_value(GLib.Variant("(b)", (False,)))
            elif method == "AboutToShowGroup":
                invocation.return_value(GLib.Variant("(aiai)", ([], [])))
            else:
                invocation.return_dbus_error(
                    "org.freedesktop.DBus.Error.UnknownMethod", method)
        except Exception:  # noqa: BLE001 - DBus 回调里必须兜底，避免打断 Shell
            invocation.return_dbus_error(
                "org.freedesktop.DBus.Error.Failed", "bad request")

    def _menu_action(self, item_id: int) -> None:
        if item_id == 1:
            self._player_call("PlayPause")
        elif item_id == 2:
            self._player_call("Previous")
        elif item_id == 3:
            self._player_call("Next")
        elif item_id == 4:
            self.request_quit()


# ---------------------------------------------------------------- 守护进程

def run_daemon(check_enabled: bool = False) -> int:
    if check_enabled and not load_enabled():
        return 0

    loop = GLib.MainLoop()

    def on_quit() -> None:
        store_enabled(False)  # 菜单里关闭 = 记住“关”
        loop.quit()

    tray = LyricsTray(on_quit=on_quit)
    if tray.already_running:
        print("[lyrics] 插件已经在运行")
        return 0
    if not tray.ok:
        return 1

    from collectors import MusicWatcher  # 延迟导入，避免无谓依赖

    watcher = MusicWatcher()

    def poll() -> bool:
        tray.update(watcher.sample())
        return GLib.SOURCE_CONTINUE

    poll()
    GLib.timeout_add_seconds(1, poll)
    loop.run()
    return 0


def main(argv: list[str]) -> int:
    args = argv[1:]
    if not args or args[0] == "--run":
        return run_daemon()
    if args[0] == "--autostart":
        return run_daemon(check_enabled=True)
    if args[0] == "--status":
        running = is_running()
        print("running" if running else "stopped")
        return 0 if running else 1
    if args[0] == "--stop":
        if is_running():
            _control_call("Quit")
        store_enabled(False)
        print("已关闭顶栏歌词")
        return 0
    if args[0] == "--start":
        store_enabled(True)
        if not is_running():
            spawn_daemon()
            print("已打开顶栏歌词")
        else:
            print("顶栏歌词已经在运行")
        return 0
    if args[0] == "--toggle":
        if is_running():
            _control_call("Quit")
            store_enabled(False)
            print("已关闭顶栏歌词")
        else:
            store_enabled(True)
            spawn_daemon()
            print("已打开顶栏歌词")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
