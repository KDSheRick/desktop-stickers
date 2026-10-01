"""歌词获取与解析：MPRIS 元数据 → LRC 时间轴（网易云优先，lrclib 兜底）。

不依赖 GTK，可单独测试：python3 lyrics.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import urllib.parse
import urllib.request

_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "sysstickers", "lyrics")
_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) sysstickers-lyrics/1.0"
_TIMEOUT = 8.0

# 过滤歌词开头的制作人员信息（作词、作曲、编曲……）
_CREDIT_RE = re.compile(
    r"^(作词|作曲|词曲|编曲|改编词曲|^词|^曲|演唱|原唱|制作人|监制|出品|发行|录音|录音师|混音|母带|"
    r"和声|合声|伴唱|配唱|吉他|贝斯|鼓|打击乐|键盘|弦乐|音乐总监|统筹|企划|封面|设计|人声编辑|"
    r"op|sp|producer|composer|lyricist|arranger|mixing|mastering)[^:：\n]{0,10}[:：]", re.I)

_STAMP_RE = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_OFFSET_RE = re.compile(r"^\s*\[offset:\s*(-?\d+)\s*\]\s*$", re.I)


# ---------------------------------------------------------------- LRC 解析

def parse_lrc(text: str) -> list[dict]:
    """LRC 文本 → [{'t': 秒, 'text': 歌词}]，过滤制作人员行，合并同时间戳。"""
    lines: list[dict] = []
    offset_ms = 0

    for raw in text.split("\n"):
        offset_match = _OFFSET_RE.match(raw)
        if offset_match:
            offset_ms = int(offset_match.group(1))
            continue

        stamps = list(_STAMP_RE.finditer(raw))
        if not stamps:
            continue
        content = _STAMP_RE.sub("", raw).strip()
        if not content or _CREDIT_RE.match(content):
            continue

        for stamp in stamps:
            minutes = int(stamp.group(1))
            seconds = int(stamp.group(2))
            frac_text = stamp.group(3) or "0"
            frac = int(frac_text) / 10 ** len(frac_text)
            lines.append({"t": minutes * 60 + seconds + frac, "text": content})

    lines.sort(key=lambda item: item["t"])
    if offset_ms:
        for line in lines:
            line["t"] += offset_ms / 1000.0

    merged: list[dict] = []
    for line in lines:
        if merged and abs(merged[-1]["t"] - line["t"]) < 0.01:
            merged[-1]["text"] += " / " + line["text"]
        else:
            merged.append(dict(line))
    return merged


def line_at(lines: list[dict], position: float) -> str:
    """取 position（秒）对应的歌词句；还没到第一句时返回空串。"""
    text = ""
    for line in lines:
        if line["t"] <= position + 0.15:
            text = line["text"]
        else:
            break
    return text


# ---------------------------------------------------------------- 文本宽度

def display_width(text: str) -> int:
    """按显示宽度计算（中文/全角算 2，半角算 1）。"""
    width = 0
    for char in text:
        code = ord(char)
        wide = (0x1100 <= code <= 0x115F) or (0x2E80 <= code <= 0xA4CF) or \
            (0xAC00 <= code <= 0xD7A3) or (0xF900 <= code <= 0xFAFF) or \
            (0xFE30 <= code <= 0xFE6F) or (0xFF00 <= code <= 0xFF60) or \
            (0xFFE0 <= code <= 0xFFE6) or (0x20000 <= code <= 0x3FFFD)
        width += 2 if wide else 1
    return width


def truncate_display(text: str, max_cells: int) -> str:
    """按显示宽度截断，超出部分用省略号。"""
    if display_width(text) <= max_cells:
        return text
    out = ""
    cells = 0
    for char in text:
        width = 2 if display_width(char) == 2 else 1
        if cells + width > max_cells - 1:
            break
        out += char
        cells += width
    return out + "…"


# ---------------------------------------------------------------- 网络与缓存

def _fetch(url: str, referer: str | None = None) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    if referer:
        request.add_header("Referer", referer)
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
        return response.read().decode("utf-8", "replace")


def _cache_path(title: str, artist: str) -> str:
    digest = hashlib.sha256(f"{title}|{artist}".encode("utf-8")).hexdigest()
    return os.path.join(_CACHE_DIR, digest + ".json")


def load_cached(title: str, artist: str) -> list[dict] | None:
    """读取缓存的歌词时间轴（旧版缓存也兼容）。"""
    track = load_cached_track(title, artist)
    return track["lines"] if track else None


def load_cached_track(title: str, artist: str) -> dict | None:
    """读取缓存的 {lines, cover}；没有缓存返回 None。"""
    try:
        with open(_cache_path(title, artist), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, list):  # 旧格式：只有歌词
            return {"lines": data, "cover": None} if data else None
        lines = data.get("lines")
        if isinstance(lines, list) and lines:
            return {"lines": lines, "cover": data.get("cover") or None}
    except (OSError, ValueError):
        pass
    return None


def _store(title: str, artist: str, lines: list[dict], cover: str | None = None) -> None:
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        path = _cache_path(title, artist)
        with open(path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump({"lines": lines, "cover": cover}, fh, ensure_ascii=False)
        os.replace(path + ".tmp", path)
    except OSError:
        pass


# ---------------------------------------------------------------- 歌词源

def _normalize_title(text: str) -> str:
    """去掉括号补充、副标题后归一化，用于匹配不同来源的歌名。"""
    text = re.sub(r"[\(\（\[【].*?[\)\）\]】]", "", text)
    text = text.split(" - ")[0]
    return text.strip().casefold()


def _primary_artist(artist: str) -> str:
    """取合作艺人里的第一位（网易云/浏览器常给 'A / B / C' 这种）。"""
    for sep in ("/", "、", "&", ",", "，", " feat.", " ft.", " with "):
        artist = artist.split(sep)[0]
    return artist.strip()


def netease_cover_url(pic_id) -> str | None:
    """由网易云 picId 推导专辑封面地址（经典的 XOR + MD5 + base64 算法）。"""
    try:
        key = "3go8&$8*3*3h0k(2)2"
        text = str(int(pic_id))
        magic = "".join(chr(ord(ch) ^ ord(key[i % len(key)])) for i, ch in enumerate(text))
        digest = hashlib.md5(magic.encode()).digest()
        encoded = base64.b64encode(digest).decode().replace("/", "_").replace("+", "-")
        return f"https://p1.music.126.net/{encoded}/{text}.jpg"
    except (TypeError, ValueError):
        return None


def _fetch_netease(title: str, artist: str) -> tuple[list[dict] | None, str | None]:
    try:
        query = urllib.parse.quote(f"{title} {_primary_artist(artist)}".strip())
        data = json.loads(_fetch(
            "https://music.163.com/api/search/get/web"
            f"?csrf_token=&type=1&offset=0&limit=10&s={query}",
            "https://music.163.com/"))
        songs = (data.get("result") or {}).get("songs") or []
        if not songs:
            return None, None

        want_title = _normalize_title(title)
        want_artist = _primary_artist(artist).casefold()
        best = None
        best_score = -1
        for song in songs:
            name = str(song.get("name", ""))
            if _normalize_title(name) != want_title:
                continue
            names = [str(item.get("name", "")) for item in song.get("artists") or []]
            joined = " ".join(names).casefold()
            if want_artist and want_artist not in joined:
                continue
            # 原唱优先：第一位艺人就匹配、歌名不带“翻自/Cover”、原始歌名一致
            score = 0
            if want_artist and names and want_artist in names[0].casefold():
                score += 3
            if name.strip() == title.strip():
                score += 1
            if not re.search(r"翻自|翻唱|cover|remix", name, re.I):
                score += 1
            if score > best_score:
                best, best_score = song, score

        if best is None:
            title_only = [s for s in songs if str(s.get("name", "")).strip() == title.strip()]
            best = title_only[0] if len(title_only) == 1 else None
        if best is None:
            return None, None

        cover = None
        pic_id = (best.get("album") or {}).get("picId")
        if pic_id:
            cover = netease_cover_url(pic_id)

        lyric = json.loads(_fetch(
            f"https://music.163.com/api/song/lyric?id={best['id']}&lv=-1&kv=-1&tv=-1",
            "https://music.163.com/"))
        lines = parse_lrc((lyric.get("lrc") or {}).get("lyric", ""))
        return (lines or None), cover
    except Exception:
        return None, None


def _fetch_lrclib(title: str, artist: str, album: str) -> tuple[list[dict] | None, str | None]:
    try:
        params = urllib.parse.urlencode({
            "track_name": title,
            "artist_name": _primary_artist(artist),
            "album_name": album,
        })
        results = json.loads(_fetch(f"https://lrclib.net/api/search?{params}", "https://lrclib.net/"))
        want_title = _normalize_title(title)
        want_artist = _primary_artist(artist).casefold()
        for item in results:
            if _normalize_title(str(item.get("trackName", ""))) != want_title:
                continue
            if want_artist and want_artist not in str(item.get("artistName", "")).casefold():
                continue
            synced = item.get("syncedLyrics")
            if not synced:
                continue
            lines = parse_lrc(synced)
            if lines:
                return lines, None
        return None, None
    except Exception:
        return None, None

def fetch_track(title: str, artist: str, album: str = "") -> dict | None:
    """同步获取 {lines, cover}（请在后台线程调用）；失败返回 None。"""
    if not title:
        return None
    cached = load_cached_track(title, artist)
    if cached is not None:
        return cached

    lines, cover = _fetch_netease(title, artist)
    if lines is None:
        lines, cover = _fetch_lrclib(title, artist, album)
    if lines:
        _store(title, artist, lines, cover)
        return {"lines": lines, "cover": cover}
    return None


def fetch_lyrics(title: str, artist: str, album: str = "") -> list[dict] | None:
    """只要歌词的便捷入口。"""
    track = fetch_track(title, artist, album)
    return track["lines"] if track else None


if __name__ == "__main__":  # 自测：python3 lyrics.py "唯一" "G.E.M.邓紫棋"
    import sys

    track = sys.argv[1] if len(sys.argv) > 1 else "唯一"
    singer = sys.argv[2] if len(sys.argv) > 2 else "G.E.M.邓紫棋"
    result = fetch_lyrics(track, singer)
    if not result:
        print("未找到歌词")
        raise SystemExit(1)
    print(f"共 {len(result)} 句，前 5 句：")
    for entry in result[:5]:
        print(f"  [{entry['t']:7.2f}] {entry['text']}")
