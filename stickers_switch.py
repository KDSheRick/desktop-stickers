#!/usr/bin/env python3
"""桌面贴纸总开关：顶栏上的一个「眼睛」图标，一键显示 / 隐藏全部贴纸。

用法：
  单击顶栏图标            # 一键显示 / 隐藏全部贴纸
  python3 stickers_switch.py --run      # 启动顶栏开关（开机自启使用）
  python3 stickers_switch.py --toggle   # 开 / 关贴纸
  python3 stickers_switch.py --start    # 打开贴纸（并确保顶栏开关在线）
  python3 stickers_switch.py --stop     # 关闭贴纸
  python3 stickers_switch.py --status   # 查看状态（running / stopped）
  python3 stickers_switch.py --quit     # 只退出顶栏开关（贴纸继续运行）

关闭 = 贴纸进程正常退出（省 CPU / 内存），状态记录在
~/.config/sysstickers/stickers.json；开机自启会遵循上次状态。
右键菜单里也可以切换、打开「贴纸设置」或退出顶栏开关。
"""

from __future__ import annotations

import os
import subprocess
import sys

import gi

from gi.repository import Gio, GLib  # noqa: E402

import stickers_ctl  # noqa: E402

SWITCH_NAME = "com.loong.SysStickersSwitch"
_CONTROL_PATH = "/Control"

_WATCHER_NAME = "org.kde.StatusNotifierWatcher"
_WATCHER_PATH = "/StatusNotifierWatcher"
_WATCHER_IFACE = "org.kde.StatusNotifierWatcher"

_ITEM_PATH = "/StatusNotifierItem"
_MENU_PATH = "/MenuBar"

_LOG_FILE = os.path.join(os.path.expanduser("~"), ".cache", "sysstickers", "switch.log")
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

_ICON_ON = "view-reveal-symbolic"
_ICON_OFF = "view-conceal-symbolic"

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
  <interface name='{SWITCH_NAME}'>
    <method name='Toggle'>
      <arg name='enabled' type='b' direction='out'/>
    </method>
    <method name='Quit'/>
  </interface>
</node>
"""

_MENU_IDS = (1, 2, 3, 4)  # 1 开关 / 2 设置 / 3 分隔线 / 4 退出开关


# ---------------------------------------------------------------- 状态查询

def _bus():
    return Gio.bus_get_sync(Gio.BusType.SESSION, None)


def _name_has_owner(name: str) -> bool:
    try:
        reply = _bus().call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "NameHasOwner", GLib.Variant("(s)", (name,)),
            GLib.VariantType.new("(b)"), Gio.DBusCallFlags.NONE, 800, None)
        return bool(reply.unpack()[0])
    except GLib.Error:
        return False


def _control_call(method: str):
    """调用正在运行的顶栏开关；Toggle 返回切换后的状态，Quit 返回 True。"""
    try:
        reply_type = GLib.VariantType.new("(b)") if method == "Toggle" else None
        reply = _bus().call_sync(
            SWITCH_NAME, _CONTROL_PATH, SWITCH_NAME, method, None, reply_type,
            Gio.DBusCallFlags.NONE, 1500, None)
        return bool(reply.unpack()[0]) if method == "Toggle" else True
    except GLib.Error:
        return None


def _ensure_daemon() -> None:
    """确保顶栏开关在运行（供命令行 / 应用菜单调用）。"""
    if _name_has_owner(SWITCH_NAME):
        return
    os.makedirs(os.path.dirname(_LOG_FILE), exist_ok=True)
    with open(_LOG_FILE, "ab") as log:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--run"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True)


# ---------------------------------------------------------------- 托盘本体

class StickersSwitch:
    """顶栏「眼睛」图标：显示 / 隐藏全部桌面贴纸。"""

    def __init__(self, on_quit=None) -> None:
        self.ok = False
        self.already_running = False
        self._on_quit = on_quit
        self._conn = None
        self._running = stickers_ctl.is_running()
        self._revision = 1
        self._bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self._watch_id = 0

        try:
            self._conn = _bus()

            # 单实例：已经有顶栏开关在跑就不再启动
            reply = self._conn.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                "RequestName", GLib.Variant("(su)", (SWITCH_NAME, 4)),  # 4 = DO_NOT_QUEUE
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

            # 贴纸被外部启动 / 退出时，图标同步更新
            self._watch_id = Gio.bus_watch_name(
                Gio.BusType.SESSION, stickers_ctl.APP_ID, Gio.BusNameWatcherFlags.NONE,
                self._on_app_appeared, self._on_app_vanished)
            self.ok = True
        except GLib.Error as exc:
            # 非 Ubuntu / 无 AppIndicator 宿主时静默降级
            print(f"[switch] 顶栏开关不可用：{exc.message}")

    # ------------------------------------------------------------ 状态同步

    def _on_app_appeared(self, _conn, _name, _owner) -> None:
        self._sync_state()

    def _on_app_vanished(self, _conn, _name) -> None:
        self._sync_state()

    def _sync_state(self) -> None:
        running = stickers_ctl.is_running()
        if running == self._running:
            return
        self._running = running
        self._revision += 1
        self._emit("NewIcon", None)
        self._emit("NewTitle", None)
        self._emit_menu_update()

    def _toggle(self) -> bool:
        stickers_ctl.toggle()
        self._sync_state()
        return self._running

    # ------------------------------------------------------------ SNI 属性

    def _on_item_property(self, _conn, _sender, _path, _iface, prop):
        title = "桌面贴纸：显示中" if self._running else "桌面贴纸：已隐藏"
        values = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", "sysstickers-switch"),
            "Title": GLib.Variant("s", title),
            "Status": GLib.Variant("s", "Active"),
            "IconName": GLib.Variant("s", _ICON_ON if self._running else _ICON_OFF),
            "IconPixmap": GLib.Variant("a(iiay)", []),
            "IconThemePath": GLib.Variant("s", ""),
            "Menu": GLib.Variant("o", _MENU_PATH),
            "ItemIsMenu": GLib.Variant("b", False),
            "XAyatanaLabel": GLib.Variant("s", ""),
            "XAyatanaLabelGuide": GLib.Variant("s", "left"),
        }
        return values.get(prop)

    def _on_item_method(self, _conn, _sender, _path, _iface, method, _params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            self._toggle()
        # ContextMenu 由 Shell 根据 Menu 属性自行弹出
        invocation.return_value(None)

    # ------------------------------------------------------------ 控制接口

    def _on_control_method(self, _conn, _sender, _path, _iface, method, _params, invocation):
        if method == "Toggle":
            invocation.return_value(GLib.Variant("(b)", (self._toggle(),)))
            return
        if method == "Quit" and self._on_quit is not None:
            self._on_quit()
        invocation.return_value(None)

    def request_quit(self) -> None:
        if self._on_quit is not None:
            self._on_quit()

    # ------------------------------------------------------------ 菜单实现

    def _menu_props(self, item_id: int) -> dict:
        if item_id == 1:
            return {
                "label": GLib.Variant("s", "显示桌面贴纸"),
                "enabled": GLib.Variant("b", True),
                "visible": GLib.Variant("b", True),
                "type": GLib.Variant("s", "standard"),
                "toggle-type": GLib.Variant("s", "checkmark"),
                "toggle-state": GLib.Variant("i", 1 if self._running else 0),
            }
        if item_id == 2:
            return {
                "label": GLib.Variant("s", "贴纸设置…"),
                "enabled": GLib.Variant("b", True),
                "visible": GLib.Variant("b", True),
            }
        if item_id == 3:
            return {"type": GLib.Variant("s", "separator")}
        if item_id == 4:
            return {
                "label": GLib.Variant("s", "退出顶栏开关"),
                "enabled": GLib.Variant("b", True),
                "visible": GLib.Variant("b", True),
            }
        return {}

    def _item_variant(self, item_id: int) -> GLib.Variant:
        return GLib.Variant.new_tuple(
            GLib.Variant("i", item_id),
            GLib.Variant("a{sv}", self._menu_props(item_id)),
            GLib.Variant("av", []))

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
                    self._item_variant(item_id) for item_id in _MENU_IDS])
                root = GLib.Variant.new_tuple(
                    GLib.Variant("i", 0),
                    GLib.Variant("a{sv}", {
                        "children-display": GLib.Variant("s", "submenu"),
                    }),
                    children)
                invocation.return_value(
                    GLib.Variant.new_tuple(GLib.Variant("u", self._revision), root))
            elif method == "GetGroupProperties":
                ids = params.unpack()[0]
                properties = [(item_id, self._menu_props(item_id))
                              for item_id in ids if item_id in _MENU_IDS]
                invocation.return_value(GLib.Variant("(a(ia{sv}))", (properties,)))
            elif method == "GetProperty":
                item_id, name = params.unpack()
                value = self._menu_props(item_id).get(name) if item_id in _MENU_IDS else None
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
            self._toggle()
        elif item_id == 2:
            subprocess.Popen(
                [sys.executable, os.path.join(_BASE_DIR, "control.py")],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True)
        elif item_id == 4:
            self.request_quit()

    # ------------------------------------------------------------ 信号

    def _emit(self, signal: str, params: GLib.Variant | None) -> None:
        if not self.ok:
            return
        try:
            self._conn.emit_signal(None, _ITEM_PATH, "org.kde.StatusNotifierItem",
                                   signal, params)
        except GLib.Error:
            pass

    def _emit_menu_update(self) -> None:
        try:
            self._conn.emit_signal(
                None, _MENU_PATH, "com.canonical.dbusmenu", "ItemsPropertiesUpdated",
                GLib.Variant("(a(ia{sv})a(ias))", ([(1, self._menu_props(1))], [])))
            self._conn.emit_signal(
                None, _MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated",
                GLib.Variant("(ui)", (self._revision, 0)))
        except GLib.Error:
            pass


# ---------------------------------------------------------------- 守护进程

def run_daemon() -> int:
    loop = GLib.MainLoop()
    switch = StickersSwitch(on_quit=loop.quit)
    if switch.already_running:
        print("[switch] 顶栏开关已经在运行")
        return 0
    if not switch.ok:
        return 1
    loop.run()
    return 0


def main(argv: list[str]) -> int:
    args = argv[1:]
    if not args or args[0] in ("--run", "--autostart"):
        return run_daemon()

    if args[0] == "--toggle":
        state = _control_call("Toggle") if _name_has_owner(SWITCH_NAME) else None
        if state is None:
            state = stickers_ctl.toggle()
            _ensure_daemon()
        print("已开启桌面贴纸" if state else "已关闭桌面贴纸")
        return 0

    if args[0] == "--start":
        ok = stickers_ctl.start()
        _ensure_daemon()
        if ok:
            print("已开启桌面贴纸")
            return 0
        print(f"贴纸启动失败，日志：{stickers_ctl.LOG_FILE}")
        return 1

    if args[0] == "--stop":
        stickers_ctl.stop()
        print("已关闭桌面贴纸")
        return 0

    if args[0] == "--status":
        running = stickers_ctl.is_running()
        print("running" if running else "stopped")
        return 0 if running else 1

    if args[0] == "--quit":
        if _name_has_owner(SWITCH_NAME):
            _control_call("Quit")
        print("已退出顶栏开关（贴纸不受影响）")
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
