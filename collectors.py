"""系统信息采集：CPU、内存、磁盘、网络、电量、温度等。

只做数据采集与格式化，不依赖 GTK，方便单独测试。
"""

from __future__ import annotations

import os
import platform
import socket
import time

import psutil
from gi.repository import Gio, GLib

_KB = 1024.0
_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


# ---------------------------------------------------------------- 格式化工具

def fmt_bytes(value: float, suffix: str = "") -> str:
    """字节数 → 人类可读文本，例如 1536 → '1.5 KB'。"""
    value = float(max(value, 0.0))
    for unit in _UNITS:
        if value < _KB or unit == _UNITS[-1]:
            digits = 0 if unit == "B" else (1 if value < 100 else 0)
            return f"{value:.{digits}f} {unit}{suffix}"
        value /= _KB
    return f"{value:.1f} {_UNITS[-1]}{suffix}"


def fmt_speed(bytes_per_sec: float) -> str:
    """速率格式化，例如 1536 → '1.5 KB/s'。"""
    return fmt_bytes(bytes_per_sec, "/s")


def fmt_uptime(seconds: float) -> str:
    """运行时长，例如 '3 天 04:05' 或 '04:05'。"""
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} 天 {hours:02d}:{minutes:02d}"
    return f"{hours:02d}:{minutes:02d}"


def fmt_duration(seconds: float | None) -> str:
    """剩余时长；psutil 的未知值（负数）显示为 '--'。"""
    if seconds is None or seconds < 0:
        return "--"
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}:{minutes:02d}"
    return f"{minutes} 分钟"


def fmt_clock(seconds: float | None) -> str:
    """曲目时间，例如 3:45 / 1:02:03。"""
    if seconds is None or seconds < 0:
        return "--:--"
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# ---------------------------------------------------------------- 温度读取

def _read_thermal_zone() -> float | None:
    """从 /sys/class/thermal 读取 CPU 温度（lm-sensors 不可用时的兜底）。"""
    base = "/sys/class/thermal"
    try:
        zones = sorted(os.listdir(base))
    except OSError:
        return None
    for zone in zones:
        zone_dir = os.path.join(base, zone)
        try:
            with open(os.path.join(zone_dir, "type"), encoding="utf-8") as fh:
                zone_type = fh.read().strip()
            if zone_type not in ("x86_pkg_temp", "cpu-thermal", "cpu_thermal", "soc_thermal"):
                continue
            with open(os.path.join(zone_dir, "temp"), encoding="utf-8") as fh:
                return int(fh.read().strip()) / 1000.0
        except (OSError, ValueError):
            continue
    return None


def read_cpu_temp() -> float | None:
    """CPU 温度（摄氏度），读不到返回 None。"""
    try:
        temps = psutil.sensors_temperatures()
    except Exception:
        temps = {}
    # 优先常见的 CPU 传感器名称，找不到再退而求其次
    for name in ("k10temp", "zenpower", "coretemp", "cpu_thermal", "soc_thermal", "acpitz"):
        for entry in temps.get(name, []):
            if entry.current:
                return float(entry.current)
    for entries in temps.values():
        for entry in entries:
            if entry.current:
                return float(entry.current)
    return _read_thermal_zone()


def read_fans() -> float | None:
    """风扇转速（RPM），读不到返回 None；返回 0 表示风扇当前停转。"""
    try:
        fans = psutil.sensors_fans()
    except Exception:
        return None
    # 优先常见的笔记本/主板传感器名称
    for name in ("dell_smm", "thinkpad", "asus", "nct6775", "nct6798", "it87"):
        for entry in fans.get(name, []):
            if entry.current is not None:
                return float(entry.current)
    for entries in fans.values():
        for entry in entries:
            if entry.current is not None:
                return float(entry.current)
    return None


# ---------------------------------------------------------------- MPRIS 音乐

class MusicWatcher:
    """通过 MPRIS（org.mpris.MediaPlayer2.*）读取当前播放器状态。"""

    _IFACE = "org.mpris.MediaPlayer2.Player"
    _ROOT_IFACE = "org.mpris.MediaPlayer2"
    _PATH = "/org/mpris/MediaPlayer2"
    _PREFIX = "org.mpris.MediaPlayer2."
    _RANK = {"Playing": 0, "Paused": 1, "Stopped": 2}

    def __init__(self) -> None:
        self._bus = None
        self._identities: dict[str, str] = {}
        self._last_name: str | None = None
        try:
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error:
            self._bus = None

    def _get_all(self, name: str) -> dict:
        reply = self._bus.call_sync(
            name, self._PATH, "org.freedesktop.DBus.Properties", "GetAll",
            GLib.Variant("(s)", (self._IFACE,)),
            GLib.VariantType.new("(a{sv})"),
            Gio.DBusCallFlags.NONE, 400, None,
        )
        return reply.unpack()[0]

    def _get(self, name: str, iface: str, prop: str):
        reply = self._bus.call_sync(
            name, self._PATH, "org.freedesktop.DBus.Properties", "Get",
            GLib.Variant("(ss)", (iface, prop)),
            GLib.VariantType.new("(v)"),
            Gio.DBusCallFlags.NONE, 400, None,
        )
        return reply.unpack()[0]

    def _identity(self, name: str) -> str:
        if name not in self._identities:
            try:
                identity = str(self._get(name, self._ROOT_IFACE, "Identity") or "")
            except GLib.Error:
                identity = ""
            self._identities[name] = identity or name[len(self._PREFIX):]
        return self._identities[name]

    def _names(self) -> list[str]:
        try:
            reply = self._bus.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus",
                "org.freedesktop.DBus", "ListNames", None,
                GLib.VariantType.new("(as)"),
                Gio.DBusCallFlags.NONE, 400, None,
            )
        except GLib.Error:
            return []
        return [name for name in reply.unpack()[0] if name.startswith(self._PREFIX)]

    def sample(self) -> dict | None:
        """返回当前活跃播放器的信息；没有播放器时返回 None。"""
        if self._bus is None:
            return None
        candidates = []
        unreachable = []
        for name in self._names():
            try:
                props = self._get_all(name)
            except GLib.Error:
                unreachable.append(name)  # 例如 snap 的 AppArmor 策略拒绝调用
                continue
            status = str(props.get("PlaybackStatus") or "Stopped")
            candidates.append((self._RANK.get(status, 3), name != self._last_name, name, props, status))
        if not candidates:
            if unreachable:
                player = unreachable[0][len(self._PREFIX):].split(".")[0]
                return {"blocked": True, "player": player}
            return None

        candidates.sort(key=lambda item: (item[0], item[1]))
        _, _, name, props, status = candidates[0]
        self._last_name = name

        meta = props.get("Metadata") or {}
        artists = meta.get("xesam:artist")
        if isinstance(artists, (list, tuple)):
            artist = " / ".join(str(item) for item in artists)
        else:
            artist = str(artists or "")

        length = meta.get("mpris:length")
        length_sec = float(length) / 1_000_000.0 if length else None
        position = None
        if status in ("Playing", "Paused"):
            try:
                raw = self._get(name, self._IFACE, "Position")
                position = int(raw) / 1_000_000.0 if raw is not None else None
            except (GLib.Error, TypeError, ValueError):
                position = None

        return {
            "player": self._identity(name),
            "bus_name": name,
            "status": status,
            "title": str(meta.get("xesam:title") or ""),
            "artist": artist,
            "album": str(meta.get("xesam:album") or ""),
            "art": str(meta.get("mpris:artUrl") or ""),
            "track_id": str(meta.get("mpris:trackid") or ""),
            "can_go_next": bool(props.get("CanGoNext", True)),
            "can_go_previous": bool(props.get("CanGoPrevious", True)),
            "length": length_sec,
            "position": position,
        }


# ---------------------------------------------------------------- 采集器

class SystemStats:
    """周期性采样系统状态；网络速率通过两次采样之间的差值计算。"""

    def __init__(self) -> None:
        self.hostname = socket.gethostname().split(".")[0]
        try:
            self.os_name = platform.freedesktop_os_release().get("PRETTY_NAME", platform.system())
        except (OSError, AttributeError):
            self.os_name = platform.system()
        self.kernel = platform.release()
        self.cpu_cores = psutil.cpu_count(logical=True) or 1

        psutil.cpu_percent(interval=None)  # 预热：首次调用总返回 0
        self._net_prev = psutil.net_io_counters()
        self._net_prev_time = time.monotonic()
        self._disk_prev = psutil.disk_io_counters()
        self._total_mem = psutil.virtual_memory().total
        self._proc_prev: dict[int, tuple[float, float]] = {}
        self._proc_prev_time = time.monotonic()
        self._page_size = os.sysconf("SC_PAGE_SIZE")
        try:
            self._clk_tck = os.sysconf("SC_CLK_TCK")
        except (ValueError, OSError):
            self._clk_tck = 100
        self._music = MusicWatcher()

        # 进程扫描与温度读取较贵：每 2 个采样周期做一次，其余周期用缓存
        self._tick = 0
        self._cached_temp: float | None = None
        self._cached_procs: list[dict] = []

        self.has_battery = self._detect_battery()

    @staticmethod
    def _detect_battery() -> bool:
        try:
            return psutil.sensors_battery() is not None
        except Exception:
            return False

    def sample(self) -> dict:
        """采集一次系统状态。"""
        self._tick += 1
        slow = self._tick % 2 == 1  # 每两拍做一次昂贵采样（启动后第一拍立即采）
        cpu = psutil.cpu_percent(interval=None)

        cpu_freq = None
        try:
            freq = psutil.cpu_freq()
            if freq and freq.current:
                cpu_freq = float(freq.current)
        except Exception:
            cpu_freq = None

        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        disk = psutil.disk_usage("/")

        # 网络速率 = 字节增量 / 时间增量
        net = psutil.net_io_counters()
        now = time.monotonic()
        dt = max(now - self._net_prev_time, 1e-6)
        down = max(net.bytes_recv - self._net_prev.bytes_recv, 0) / dt
        up = max(net.bytes_sent - self._net_prev.bytes_sent, 0) / dt
        self._net_prev = net
        self._net_prev_time = now

        # 磁盘读写速率（与网络共用同一时间窗口）
        disk_io = psutil.disk_io_counters()
        if disk_io is not None and self._disk_prev is not None:
            read_rate = max(disk_io.read_bytes - self._disk_prev.read_bytes, 0) / dt
            write_rate = max(disk_io.write_bytes - self._disk_prev.write_bytes, 0) / dt
        else:
            read_rate = write_rate = 0.0
        self._disk_prev = disk_io

        battery = None
        try:
            bat = psutil.sensors_battery()
            if bat is not None:
                battery = {
                    "percent": float(bat.percent),
                    "plugged": bool(bat.power_plugged),
                    "secsleft": bat.secsleft,
                }
        except Exception:
            battery = None

        try:
            load1, load5, load15 = os.getloadavg()
        except OSError:
            load1 = load5 = load15 = 0.0

        if slow:
            self._cached_temp = read_cpu_temp()
            self._cached_procs = self._top_processes()

        return {
            "cpu": cpu,
            "cpu_temp": self._cached_temp,
            "cpu_freq": cpu_freq,
            "mem": {"percent": mem.percent, "used": mem.used, "total": mem.total},
            "swap": {"used": swap.used, "total": swap.total},
            "disk": {
                "percent": disk.percent,
                "used": disk.used,
                "total": disk.total,
                "free": disk.free,
            },
            "diskio": {"read": read_rate, "write": write_rate},
            "net": {"down": down, "up": up},
            "proc": self._cached_procs,
            "fan": read_fans(),
            "music": self._music.sample(),
            "battery": battery,
            "uptime": time.time() - psutil.boot_time(),
            "load": (load1, load5, load15),
        }

    def _top_processes(self) -> list[dict]:
        """按本次采样窗口内的 CPU 占用排出前三名；第一次采样时返回空列表。

        直接读 /proc（比 psutil.process_iter 快一个数量级），
        用 starttime 防止 PID 复用，名称优先取 cmdline 的可执行文件名。
        """
        now = time.monotonic()
        dt = max(now - self._proc_prev_time, 1e-6)
        self._proc_prev_time = now

        try:
            pids = [int(entry) for entry in os.listdir("/proc") if entry.isdigit()]
        except OSError:
            return []

        prev = self._proc_prev
        current: dict[int, tuple[float, float]] = {}
        rows = []
        for pid in pids:
            try:
                with open(f"/proc/{pid}/stat", "rb") as fh:
                    data = fh.read().decode("utf-8", "replace")
            except OSError:
                continue
            left, right = data.find("("), data.rfind(")")
            if left < 0 or right < 0:
                continue
            comm = data[left + 1:right]
            fields = data[right + 2:].split()
            try:
                cpu_total = (int(fields[11]) + int(fields[12])) / self._clk_tck
                starttime = int(fields[19])
            except (IndexError, ValueError):
                continue
            current[pid] = (starttime, cpu_total)

            old = prev.get(pid)
            if old is None or old[0] != starttime:
                continue  # 新进程：这一轮只记录，下一轮才有差值
            delta = max(cpu_total - old[1], 0.0)
            if delta > 0.0:
                rows.append((delta, pid, comm))
        self._proc_prev = current

        rows.sort(reverse=True)
        top = []
        for delta, pid, comm in rows[:8]:
            rss = 0
            try:
                with open(f"/proc/{pid}/statm", "rb") as fh:
                    rss = int(fh.read().split()[1]) * self._page_size
            except (OSError, IndexError, ValueError):
                pass
            top.append({
                "pid": pid,
                "name": self._proc_display_name(pid, comm),
                "cpu": min(delta / dt / self.cpu_cores * 100.0, 100.0),
                "mem": rss / self._total_mem * 100.0 if self._total_mem else 0.0,
            })
        top.sort(key=lambda row: (row["cpu"], row["mem"]), reverse=True)
        return top[:3]

    @staticmethod
    def _proc_display_name(pid: int, comm: str) -> str:
        """优先用 cmdline 里的可执行文件名。

        Electron/Chromium 会把整条命令行塞进 argv[0]（空格分隔），
        且内核 comm 只有 15 字节，所以取第一个空格前的文件名。
        """
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                argv0 = fh.read(4096).split(b"\0", 1)[0].split(b" ", 1)[0]
        except OSError:
            return comm
        name = os.path.basename(argv0.decode("utf-8", "replace")).strip()
        return name or comm


if __name__ == "__main__":  # 简单自测：python3 collectors.py
    import json

    stats = SystemStats()
    print("主机:", stats.hostname, "| 系统:", stats.os_name, "| 内核:", stats.kernel)
    print(json.dumps(stats.sample(), ensure_ascii=False, indent=2, default=str))
