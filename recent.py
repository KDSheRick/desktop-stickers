"""最近经常打开的文件（GTK 的 recently-used.xbel）。

- 只收录仍然存在的本地文件
- 排序：打开次数 + 近期加权（最近打开过的优先）
- 解析结果按文件修改时间缓存，不会频繁重复解析

可单独测试：python3 recent.py
"""

from __future__ import annotations

import os
import time
import urllib.parse
import xml.etree.ElementTree as ET

_XBEL = os.path.join(os.path.expanduser("~"), ".local", "share", "recently-used.xbel")
_NS = {
    "b": "http://www.freedesktop.org/standards/desktop-bookmarks",
    "m": "http://www.freedesktop.org/standards/shared-mime-info",
}
_CHECK_INTERVAL = 2.0  # 秒：最多这么频繁地检查文件是否变化

_cache = {"checked": 0.0, "mtime": None, "items": []}


def _epoch(text: str) -> float:
    try:
        from datetime import datetime

        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def recent_files(limit: int = 6) -> list[dict]:
    """返回最近经常打开的文件列表。

    每项：{'name', 'path', 'mime', 'count', 'visited'}
    """
    now = time.monotonic()
    if now - _cache["checked"] < _CHECK_INTERVAL:
        return _cache["items"][:limit]
    _cache["checked"] = now

    try:
        mtime = os.stat(_XBEL).st_mtime
    except OSError:
        _cache["mtime"] = None
        _cache["items"] = []
        return []

    if mtime != _cache["mtime"]:
        _cache["mtime"] = mtime
        _cache["items"] = _parse()
    return _cache["items"][:limit]


def _parse() -> list[dict]:
    try:
        tree = ET.parse(_XBEL)
    except (OSError, ET.ParseError):
        return []

    epoch = time.time()
    seen: set[str] = set()
    rows: list[dict] = []
    for bookmark in tree.getroot().iter("bookmark"):
        href = bookmark.get("href") or ""
        if not href.startswith("file://"):
            continue
        path = urllib.parse.unquote(href[len("file://"):])
        if path in seen or not os.path.isfile(path):
            continue
        seen.add(path)

        mime_node = bookmark.find(".//m:mime-type", _NS)
        mime = mime_node.get("type", "") if mime_node is not None else ""
        count = sum(int(node.get("count") or 0)
                    for node in bookmark.findall(".//b:application", _NS))
        visited = _epoch(bookmark.get("visited") or bookmark.get("modified") or "")

        age_days = max((epoch - visited) / 86400.0, 0.0) if visited else 999.0
        if age_days <= 1:
            recency = 3.0
        elif age_days <= 7:
            recency = 2.0
        elif age_days <= 30:
            recency = 1.0
        else:
            recency = 0.0

        rows.append({
            "name": os.path.basename(path),
            "path": path,
            "mime": mime,
            "count": count,
            "visited": visited,
            "score": count * 2.0 + recency,
        })

    rows.sort(key=lambda row: (row["score"], row["visited"]), reverse=True)
    return rows[:24]


if __name__ == "__main__":
    for item in recent_files(10):
        print(f"[{item['count']}] {item['name']}  ({item['mime'] or '?'})  {item['path']}")
