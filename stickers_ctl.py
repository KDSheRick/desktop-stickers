#!/usr/bin/env python3
"""桌面贴纸的进程控制：启动 / 关闭 / 状态查询。

- 供 stickers_switch.py（顶栏总开关）、control.py（设置窗口）调用
- 「关闭」= 让贴纸进程正常退出（省 CPU 与内存），并把状态记住；
  开机自启（main.py --autostart）会遵循该状态
- 状态文件：~/.config/sysstickers/stickers.json
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import gi

from gi.repository import Gio, GLib  # noqa: E402

APP_ID = "com.loong.SysStickers"  # 与 main.py 的 Gtk.Application ID 一致
OBJECT_PATH = "/com/loong/SysStickers"  # GApplication 在会话总线上的对象路径
ACTIONS_IFACE = "org.gtk.Actions"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAIN_SCRIPT = os.path.join(BASE_DIR, "main.py")
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "sysstickers")
STATE_FILE = os.path.join(CONFIG_DIR, "stickers.json")
LOG_FILE = os.path.join(os.path.expanduser("~"), ".cache", "sysstickers", "stickers.log")


# ---------------------------------------------------------------- 开关状态

def load_enabled() -> bool:
    """上次是否处于开启状态（默认开启）。"""
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return bool(json.load(fh).get("enabled", True))
    except (OSError, ValueError):
        return True


def store_enabled(enabled: bool) -> None:
    """记住开关状态（开机自启据此决定是否拉起贴纸）。"""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(STATE_FILE + ".tmp", "w", encoding="utf-8") as fh:
            json.dump({"enabled": bool(enabled)}, fh)
        os.replace(STATE_FILE + ".tmp", STATE_FILE)
    except OSError:
        pass


# ---------------------------------------------------------------- 进程控制

def _bus():
    return Gio.bus_get_sync(Gio.BusType.SESSION, None)


def _name_owner() -> bool:
    try:
        reply = _bus().call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "NameHasOwner", GLib.Variant("(s)", (APP_ID,)),
            GLib.VariantType.new("(b)"), Gio.DBusCallFlags.NONE, 800, None)
        return bool(reply.unpack()[0])
    except GLib.Error:
        return False


def is_running() -> bool:
    """贴纸主程序是否在运行（以会话总线上的应用名为准）。"""
    return _name_owner()


def _pid_of_app() -> int | None:
    try:
        reply = _bus().call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "GetConnectionUnixProcessID", GLib.Variant("(s)", (APP_ID,)),
            GLib.VariantType.new("(u)"), Gio.DBusCallFlags.NONE, 800, None)
        pid = int(reply.unpack()[0])
        return pid if pid > 0 else None
    except (GLib.Error, ValueError):
        return None


def _quit_via_action() -> bool:
    """调用主程序的 quit 动作，让它正常退出（会保存位置等状态）。"""
    try:
        _bus().call_sync(
            APP_ID, OBJECT_PATH, ACTIONS_IFACE, "Activate",
            GLib.Variant("(sava{sv})", ("quit", [], {})), None,
            Gio.DBusCallFlags.NONE, 1500, None)
        return True
    except GLib.Error:
        return False


def _wait_exit(timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_running():
            return True
        time.sleep(0.05)
    return not is_running()


def _terminate_process() -> None:
    """兜底：正常退出失败时按 PID 结束（先 TERM，再 KILL）。"""
    pid = _pid_of_app()
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    if _wait_exit(1.5):
        return
    pid = _pid_of_app()
    if pid is not None:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        _wait_exit(0.8)


def spawn() -> None:
    """后台拉起贴纸主程序（脱离当前进程组，日志写到缓存目录）。"""
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "ab") as log:
        subprocess.Popen(
            [sys.executable, MAIN_SCRIPT],
            cwd=BASE_DIR,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True)


def start(wait: float = 1.5) -> bool:
    """打开贴纸（记住开启状态），返回进程是否已就绪。"""
    store_enabled(True)
    if not is_running():
        spawn()
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if is_running():
                return True
            time.sleep(0.05)
    return is_running()


def stop() -> bool:
    """关闭贴纸（记住关闭状态），返回进程是否已退出。"""
    store_enabled(False)
    if not is_running():
        return True
    if _quit_via_action() and _wait_exit(2.0):
        return True
    _terminate_process()
    return not is_running()


def toggle() -> bool:
    """切换开关，返回切换后的状态（True = 开启）。"""
    if is_running():
        stop()
        return False
    return start()
